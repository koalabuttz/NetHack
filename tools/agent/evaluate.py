"""Offline replay and comparison CLI for recorded episodes.

    python3 -m tools.agent.evaluate WIRE_JSONL --reflex scripted \\
        --strategy off --output OUT.jsonl

This is the evaluator described in ``doc/agent-autoplay-plan.md`` section
"Recording and offline evaluation".  It runs **without a game and, by
default, without a network**: it feeds the raw inbound physical lines of a
recording through the *same* assembly, public-state, boundary and decision
machinery the live controller uses (``codec``/``protocol``/``state``/
``events``/``directives``/``policy``/``providers``) and never re-implements
them.  Provider decisions are observations only: they are recorded and
compared, but they are **never injected** back into the trajectory, and a
hypothetical effect is never claimed.

What one replay does:

  * assemble every physical line (chunk streams included) exactly as the live
    controller does;
  * apply each ``obs`` atomically to a public :class:`~tools.agent.protocol.
    Snapshot` and fold it into :class:`~tools.agent.state.EpisodeMemory`;
  * run the deterministic boundary detector once per snapshot, and drive the
    boundary queue / directive book / budget ledger when a strategy tier is
    enabled;
  * at each need, collect *all* required pages first, then ask each candidate
    provider for a proposal, validate it, and (when an actions sidecar is
    supplied) compare it to the recorded original action by **semantics**, not
    JSON bytes;
  * advance only along the recorded trajectory -- the evaluated action is
    never sent.

Determinism: a replay is a pure function of the wire bytes (and the candidate
configuration).  Wall timing is confined to a separate ``wall`` map in the
event ledger, which is dropped from every emitted record, so two runs produce
byte-identical output.

The default paths make no network calls.  A real provider evaluation requires
an explicit ``--allow-network`` opt-in *and* ``--strategy deepseek``; the
presence of a key alone never opts a user in, mirroring the live harness.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import protocol, recording, state
from .budget import BudgetLedger
from .codec import AssemblerLimit, ChunkError, IncrementalAssembler
from .controller import _EpisodeRunner
from .directives import DirectiveBook, PreconditionState
from .events import BoundaryQueue, EventLedger, directive_event, hunger_index
from .policy import INV_STALE_TICKS, ScriptedReflex
from .protocol import NeedKey, Request, Snapshot
from .providers import (NullStrategy, ProviderConfig, ReflexContext,
                        ScriptedReflexProvider, StrategyContext,
                        reflex_provider, strategy_provider,
                        strategy_token_bound, tariff_from_config)

EVAL_SCHEMA = 1

# Physical/retention bounds mirror the live controller's, so a recording that
# the live path accepted cannot fail a replay for a different reason.
_LOGICAL_KEYS = ("type", "seq", "id", "kind", "menu", "content", "page",
                 "pages", "rows", "code")


# ------------------------------------------------------------------ clock

class _Clock(object):
    """A deterministic monotonic-style clock for the offline boundary queue.

    The live controller coalesces and rate-limits on ``time.monotonic``; a
    replay has no wall clock and must not invent one.  This advances by a
    fixed step per *physical record*, so the ``ticks``/``level`` fields of the
    event ledger are a reproducible function of the recording rather than of
    how fast the replay host happened to run.
    """

    def __init__(self, start: float = 1000.0, step: float = 1.0) -> None:
        self.t = float(start)
        self.step = float(step)

    def __call__(self) -> float:
        return self.t

    def advance(self) -> None:
        self.t += self.step


# ------------------------------------------------------ action semantics

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def canonical_action(need, action) -> Tuple:
    """A semantic key for one action, comparable across two encodings.

    This is what an agreement check compares -- **not** the JSON.  The rules:

      * a ``cancel`` and an ``ack`` are their own shapes;
      * ``key``/``yn``/``position``/``text`` compare by value;
      * a ``menu`` commit compares as the **final set of rows**, ignoring the
        per-row count (``-1`` means "the whole stack" and ``1`` means "one
        item"; for a single item those select the same thing) and ignoring the
        menu *generation id*, which is scoped to this one need and is the same
        for both sides.

    A non-object or unrecognised action is a distinct ``("other", ...)`` key.
    """
    if not isinstance(action, dict):
        return ("none", repr(action))
    if action.get("cancel"):
        return ("cancel",)
    if action.get("ack"):
        return ("ack",)
    if "key" in action and _is_int(action["key"]):
        return ("key", int(action["key"]))
    if "yn" in action and _is_int(action["yn"]):
        return ("yn", int(action["yn"]))
    if "position" in action:
        pos = action["position"]
        if isinstance(pos, (list, tuple)) and len(pos) == 2 \
                and _is_int(pos[0]) and _is_int(pos[1]):
            return ("position", (int(pos[0]), int(pos[1])))
        return ("other", "position")
    if "menu" in action:
        commit = action.get("commit")
        if not isinstance(commit, list):
            return ("other", "menu")
        rows = []
        for row in commit:
            if isinstance(row, (list, tuple)) and len(row) == 2 \
                    and _is_int(row[0]):
                rows.append(int(row[0]))
            else:
                return ("other", "menu")
        return ("menu", tuple(sorted(rows)))
    if "text" in action:
        return ("text", action["text"])
    return ("other", sorted(action))


def _action_of(record_action) -> Optional[dict]:
    """The inner action of an ``actions`` sidecar entry, or None."""
    if not isinstance(record_action, dict):
        return None
    inner = record_action.get("action")
    if isinstance(inner, dict):
        return inner
    return record_action


# ------------------------------------------------------------ sidecars

def load_actions_index(path: Optional[str]) -> Dict[Tuple[int, int], dict]:
    """Map ``(seq, id)`` -> the *first sent* original action for that need.

    Only ``kind == "act"`` records are ground truth; ``get_page`` and
    ``ack_chunk`` are transport, not gameplay.  A retry (a second ``act`` for
    the same need after an ``invalid``) does not overwrite the first answer --
    the first sent action is what the trajectory actually followed.
    """
    index: Dict[Tuple[int, int], dict] = {}
    if not path:
        return index
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("kind") != "act":
                continue
            need = obj.get("need") or {}
            seq, nid = need.get("seq"), need.get("id")
            if not (_is_int(seq) and _is_int(nid)):
                continue
            if obj.get("status") not in (None, "sent"):
                continue
            action = _action_of(obj.get("action"))
            if action is None:
                continue
            index.setdefault((seq, nid), action)
    return index


def load_decisions(path: Optional[str]) -> List[dict]:
    """The ordered list of decision records from a decisions sidecar."""
    out: List[dict] = []
    if not path:
        return out
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def safe_fallback(need) -> dict:
    """The controller's per-kind safe fallback, reused rather than copied.

    ``_EpisodeRunner._safe_fallback`` is stateless (it never reads ``self``),
    so the evaluator calls the very same implementation the live controller
    does -- there is no second copy of the per-kind fallback table to drift.
    """
    return _EpisodeRunner._safe_fallback(None, need)


def _strip_wall(rec: dict) -> dict:
    """A copy of an event record with its wall-timing map removed."""
    out = {k: v for k, v in rec.items() if k != "wall"}
    return out


# ------------------------------------------------------------- the pass

@dataclass
class _Need:
    index: int
    seq: int
    nid: int
    need: dict
    rows: List[Any] = field(default_factory=list)
    declared_pages: int = 0
    delivered_pages: Dict[int, List[Any]] = field(default_factory=dict)
    boundaries: List[str] = field(default_factory=list)
    decided: bool = False


class ReplayPass(object):
    """One provider's independent replay of a recording.

    Every pass owns its own snapshot, episode memory, reflex instance,
    boundary detector, event ledger and budget ledger, so two candidate
    providers can never influence each other's memory or accounting -- the
    isolation the plan requires ("each candidate provider gets its own episode
    memory and isolated budgets").
    """

    def __init__(self, wire_lines: List[bytes], config: ProviderConfig,
                 provider_name: str, strategy_name: str,
                 allow_network: bool = False,
                 actions_index: Optional[Dict] = None) -> None:
        self.wire_lines = wire_lines
        self.config = config
        self.provider_name = provider_name
        self.strategy_name = strategy_name
        self.allow_network = allow_network
        self.actions_index = actions_index or {}

        self.asm = IncrementalAssembler(
            max_retained_bytes=protocol.MAX_RETAINED_BYTES,
            max_chunks=protocol.MAX_CHUNKS, max_streams=protocol.MAX_STREAMS,
            max_line_bytes=protocol.MAX_PHYSICAL_LINE)
        self.snap = Snapshot()
        self.mem = state.EpisodeMemory()
        self.req = Request()
        self.clock = _Clock()
        self.event_records: List[dict] = []
        self.event_ledger = EventLedger(sink=self.event_records.append)
        self.ledger = BudgetLedger(
            strategy_cap=config.strategy_call_cap,
            postmortem_reserve=config.postmortem_reserve,
            usd_cap=config.usd_cap,
            tariff=tariff_from_config(config),
            reflex_cap=config.reflex_call_cap,
            token_cap=config.token_cap)
        self.boundary_queue = BoundaryQueue(
            cooldown_ticks=config.boundary_cooldown_ticks,
            cooldown_wall=config.boundary_cooldown_wall,
            emergency_wall=config.boundary_emergency_wall,
            ledger=self.ledger, event_ledger=self.event_ledger,
            clock=self.clock)
        self.book = DirectiveBook()

        self.reflex = ScriptedReflex(config)
        self.reflex.max_ticks = config.max_ticks
        self.provider = self._build_reflex(provider_name)
        self.strategy = self._build_strategy(strategy_name)
        self.strategy_live = (self.strategy_name == "deepseek"
                              and allow_network
                              and not isinstance(self.strategy, NullStrategy))

        self.decisions: List[dict] = []
        self.tick = 0
        self.last_seq = 0
        self.hello_seen = False
        self.closed = False
        self.eof = True
        self.invalids = 0
        self.needs = 0
        self.answered = 0
        self.need_unanswered = 0
        self.low_conf_streak = 0
        self.protocol_failure: Optional[str] = None
        self._pending: Optional[_Need] = None
        self._strategy_pending = None
        self._strategy_level = None

    # -- provider construction ------------------------------------------
    def _build_reflex(self, name):
        if name == "scripted":
            return ScriptedReflexProvider(reflex=self.reflex)
        cfg = ProviderConfig(**{**self.config.__dict__, "reflex": name})
        return reflex_provider(cfg)

    def _build_strategy(self, name):
        if name != "deepseek" or not self.allow_network:
            return NullStrategy()
        cfg = ProviderConfig(**{**self.config.__dict__, "strategy": name})
        return strategy_provider(cfg)

    # -- entry -----------------------------------------------------------
    def run(self) -> None:
        try:
            for line in self.wire_lines:
                self.clock.advance()
                self._feed_line(line)
                if self.closed:
                    break
        except (AssemblerLimit, ChunkError, ValueError, KeyError,
                TypeError) as exc:
            self.protocol_failure = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self._finalize()

    def _feed_line(self, line: bytes) -> None:
        try:
            logical = self.asm.feed(line)
        except AssemblerLimit as exc:
            self.protocol_failure = "assembler retention limit: %s" % exc
            self.closed = True
            return
        except ChunkError as exc:
            self.protocol_failure = "chunk stream: %s" % exc
            self.closed = True
            return
        except (ValueError, KeyError, TypeError) as exc:
            self.protocol_failure = "malformed record: %s" % exc
            self.closed = True
            return
        for rec in logical:
            if self.closed:
                break
            self._handle(rec)

    def _handle(self, rec) -> None:
        if not isinstance(rec, dict):
            self.protocol_failure = "record is not an object"
            self.closed = True
            return
        t = rec.get("type")
        if t != "hello" and not self.hello_seen:
            self.protocol_failure = "%r record before hello" % (t,)
            self.closed = True
            return
        if t == "hello":
            reason = protocol.validate_hello(rec)
            if reason:
                self.protocol_failure = "incompatible hello: %s" % reason
                self.closed = True
                return
            self.hello_seen = True
        elif t == "obs":
            self._on_obs(rec)
        elif t == "page":
            self._on_page(rec)
        elif t == "invalid":
            self._on_invalid(rec)
        elif t == "closed":
            self._on_closed(rec)
        else:
            self.protocol_failure = "unknown record type %r" % (t,)
            self.closed = True

    # -- records ---------------------------------------------------------
    def _on_obs(self, rec) -> None:
        seq = rec.get("seq")
        if not _is_int(seq) or seq <= self.last_seq:
            self.protocol_failure = "non-monotonic seq %r after %r" \
                % (seq, self.last_seq)
            self.closed = True
            return
        self.last_seq = seq
        try:
            self.snap.apply(rec)
            self.mem.observe(self.snap)
        except protocol.ProtocolError as exc:
            self.protocol_failure = "invalid snapshot: %s" % exc
            self.closed = True
            return
        # a new obs supersedes any need still awaiting pages: that need was
        # never answered in the recorded trajectory
        if self._pending is not None and not self._pending.decided:
            self.need_unanswered += 1
            self._pending = None

        detected = self._detect_boundaries()
        need = rec.get("need")
        if need is not None:
            reason = protocol.validate_need(need)
            if reason:
                self.protocol_failure = "malformed need: %s" % reason
                self.closed = True
                return
        self.req.begin(need, seq)
        if need is None:
            return
        self.needs += 1
        self._pending = _Need(
            index=self.needs, seq=seq, nid=need.get("id"), need=need,
            declared_pages=need.get("pages", 0) or 0,
            boundaries=[b.eid for b in detected])
        if self.strategy_live:
            self._submit_boundaries(detected)
        # a page-less need is decided immediately; a paged need waits for its
        # pages (collected below in arrival order)
        if self._pending.declared_pages <= 0:
            self._decide_pending()

    def _on_page(self, rec) -> None:
        need = self._pending
        if need is None or need.decided:
            return
        if rec.get("content") != need.need.get("content"):
            return
        idx = rec.get("page")
        if not _is_int(idx) or idx < 0:
            return
        need.delivered_pages.setdefault(idx, rec.get("rows") or [])
        if all(k in need.delivered_pages
               for k in range(need.declared_pages)):
            self._decide_pending()

    def _on_invalid(self, rec) -> None:
        self.invalids += 1
        self.ledger.reflex_invalid += 1
        # `invalid` leaves the same request outstanding: a rejected *attempt*,
        # not a completed decision.  It is recorded as its own decision row.
        self.decisions.append({
            "schema": EVAL_SCHEMA, "record": "need", "index": self.needs,
            "need": {"seq": self.last_seq,
                     "id": (self._pending.need.get("id")
                            if self._pending else None),
                     "kind": (self._pending.need.get("kind")
                              if self._pending else None)},
            "proposal": None, "selected": None, "provider": "controller",
            "reason": "invalid:%s" % (rec.get("code"),),
            "legal": None, "fallback": True, "low_confidence": True,
            "agreement": None, "actual_action": None,
            "actual_action_source": "unknown",
            "boundaries": [], "directives": [],
        })

    def _on_closed(self, rec) -> None:
        self.closed = True
        self.eof = False
        self.provider.on_closed()
        detected = self.mem.boundary.check(self.mem.status, closed=True)
        self.ledger.note_boundary("detected", len(detected))
        self._note_detected(detected)
        if self._pending is not None and not self._pending.decided:
            self.need_unanswered += 1
            self._pending = None
        if self.strategy_live:
            self.event_ledger.flush_unqueued()

    # -- boundary machinery ---------------------------------------------
    def _detect_boundaries(self):
        st = self.mem.status
        detected = self.mem.boundary.check(
            st, classes=self.mem.visible_classes(),
            messages=self.mem.recent_messages(10),
            inventory_sig=self.mem.inventory_signature(),
            failed_food=self.mem.failed_food_count(),
            low_conf_streak=self.low_conf_streak,
            low_conf_threshold=self.config.low_confidence_needs)
        if detected:
            self.ledger.note_boundary("detected", len(detected))
        self._note_detected(detected)
        self.event_ledger.flush_unqueued()
        return detected

    def _note_detected(self, detected) -> None:
        for b in detected:
            self.event_ledger.detect(b, self.tick,
                                     self.mem.status.dlvl or "")

    def _submit_boundaries(self, detected) -> None:
        if not detected:
            return
        self.boundary_queue.submit(detected, self.tick,
                                   self.mem.status.dlvl or "")

    # -- strategy (only under --allow-network) --------------------------
    def _service_strategy(self) -> None:
        if not self.strategy_live:
            return
        pending = self.boundary_queue.ready(self.tick, self.clock())
        if pending is None:
            return
        ctx = self._build_strategy_context(pending)
        prompt, completion = strategy_token_bound(self.config, ctx)
        if not self.ledger.reserve_strategy(prompt_tokens=prompt,
                                            completion_tokens=completion):
            self.boundary_queue.suppress("strategy-cap")
            return
        self.boundary_queue.mark_dispatched(self.tick, self.clock())
        self._strategy_level = self.mem.status.dlvl
        try:
            res = self.strategy.deliberate(ctx, self.clock()
                                           + self.config.strategy_deadline)
        except Exception:                    # noqa: BLE001 - bounded policy
            res = None
        usage = res.usage if res is not None else None
        self.ledger.commit_strategy(usage)
        if res is not None and res.ok and res.directives:
            self._strategy_pending = res.directives[0]
        else:
            self.boundary_queue.finish(False, "strategy-failed")
        self.decisions.append({
            "schema": EVAL_SCHEMA, "record": "strategy",
            "provider": res.provider if res is not None else "strategy",
            "reason": res.reason if res is not None else "no result",
            "boundaries": list(pending.eids),
            "usage": usage or {},
            "directives": [d.to_dict() for d in
                           (res.directives if res is not None else [])],
        })

    def _build_strategy_context(self, pending):
        st = self.mem.status
        bits = []
        if st.hp is not None and st.hp_max:
            bits.append("HP %d/%d" % (st.hp, st.hp_max))
        if st.hunger:
            bits.append("Hunger %s" % st.hunger)
        if st.dlvl:
            bits.append("Dlvl %s" % st.dlvl)
        return StrategyContext(
            episode=1, tick=self.tick,
            summary={"hp": st.hp, "hp_max": st.hp_max, "dlvl": st.dlvl},
            boundaries=list(pending.eids),
            map_text=state.render_map(self.mem),
            status_text=", ".join(bits),
            recent_messages=self.mem.recent_messages(6),
            inventory=[(r.get("text") or "")
                       for r in self.mem.inventory.rows],
            remaining_budget=self._remaining_budget(), level=st.dlvl)

    def _remaining_budget(self):
        spendable = self.ledger.strategy_cap - self.ledger.postmortem_reserve
        spent = (self.ledger.strategy_dispatched
                 + self.ledger.strategy_reserved)
        return max(0, spendable - spent)

    def _activate_directives(self, need) -> None:
        if self._strategy_pending is None:
            return
        if need.get("kind") not in ("command", "key", "direction"):
            return
        dset = self._strategy_pending
        self._strategy_pending = None
        level = self.mem.status.dlvl
        if self._strategy_level is not None and level is not None \
                and level != self._strategy_level:
            self.boundary_queue.finish(False, "stale-level")
            return
        self.book.activate(dset, self.tick, level)
        self.boundary_queue.finish(True)

    # -- decision --------------------------------------------------------
    def _decide_pending(self) -> None:
        need = self._pending
        if need is None or need.decided:
            return
        need.decided = True
        rows = []
        for k in range(need.declared_pages):
            rows.extend(need.delivered_pages.get(k, []))

        if self.strategy_live:
            self._service_strategy()
        self._activate_directives(need.need)

        view = self.book.view(self.tick, self.mem.status.dlvl,
                              self._precondition_state())
        ctx = ReflexContext(
            episode=1, tick=self.tick, need=need.need,
            need_key=NeedKey(1, need.seq, need.nid), snapshot=self.snap,
            pages=rows, memory=self.mem,
            directives=[view] if view.active else [], deadline=0.0)
        proposal, provider_label, reason, fallback = \
            self._propose(ctx)

        err = None
        if proposal is None:
            err = "no proposal"
        else:
            err = protocol.validate_action(need.need, proposal)
        if proposal is None or err:
            selected = safe_fallback(need.need)
            fallback = True
            reason = "%s (%s)" % (reason, err) if err else reason
        else:
            selected = proposal

        legal = protocol.validate_action(need.need, selected) is None
        low = fallback or provider_label != self.provider_name

        actual = self.actions_index.get((need.seq, need.nid))
        if actual is None:
            agreement = None
            actual_source = "unknown"
        else:
            agreement = (canonical_action(need.need, actual)
                         == canonical_action(need.need, selected))
            actual_source = "sidecar"

        self._note_low_conf(low)
        self.decisions.append({
            "schema": EVAL_SCHEMA, "record": "need", "index": need.index,
            "need": {"seq": need.seq, "id": need.nid,
                     "kind": need.need.get("kind")},
            "provider": provider_label,
            "proposal": proposal, "selected": selected,
            "reason": reason, "legal": legal, "fallback": fallback,
            "low_confidence": low,
            "actual_action": actual, "actual_action_source": actual_source,
            "agreement": agreement,
            "boundaries": list(need.boundaries),
            "directives": [view.dset.to_dict()] if view.active else [],
        })
        self.answered += 1
        if need.need.get("kind") in ("command", "key", "direction"):
            self.tick += 1
        self._pending = None

    def _propose(self, ctx) -> Tuple[Optional[dict], str, str, bool]:
        """Run the candidate provider; return (action, label, reason, fb).

        A paid tier that is locally unavailable (the offline default) or
        disabled for a network-free replay degrades to the scripted reflex --
        the same bounded-fallback contract the controller enforces.
        """
        if self.provider_name == "scripted":
            self.ledger.reflex_attempted += 1
            res = self.provider.decide(ctx, 0.0)
            if res is None:
                self.ledger.reflex_fallback += 1
                return None, "controller", "reflex returned no result", True
            self.ledger.reflex_successful += 1
            return res.action, res.provider or "scripted", res.reason, False
        # jev (or any other paid reflex)
        scripted = self.reflex.fallback(ctx)
        fb_action = scripted.action if scripted is not None else None
        avail = self.provider.available(self.config)
        if not avail.enabled:
            self.ledger.reflex_fallback += 1
            return (fb_action, "scripted",
                    "jev unavailable: %s" % avail.reason, True)
        if not self.allow_network:
            self.ledger.reflex_fallback += 1
            return (fb_action, "scripted",
                    "jev not evaluated offline", True)
        res = self.provider.decide(
            ctx, self.clock() + self.config.reflex_deadline)
        usage = res.usage if res is not None else {}
        self.ledger.add_usage(usage)
        if res is None or res.action is None:
            self.ledger.reflex_fallback += 1
            why = (res.reason if res is not None else None) or "no answer"
            return (fb_action, "scripted", "jev fallback: %s" % why, True)
        self.ledger.reflex_successful += 1
        return res.action, res.provider or "jev", res.reason, False

    def _precondition_state(self) -> PreconditionState:
        st = self.mem.status
        frac = None
        if st.hp is not None and st.hp_max:
            frac = st.hp / float(st.hp_max)
        fresh = self.mem.inventory.seen_tick is not None and \
            (self.tick - self.mem.inventory.seen_tick) <= INV_STALE_TICKS
        return PreconditionState(
            hero_known=self.mem.hero is not None, hp_known=frac is not None,
            hp_frac=frac, hungry=hunger_index(st.hunger) >= 0,
            inventory_fresh=fresh)

    def _note_low_conf(self, low: bool) -> None:
        if not low:
            self.low_conf_streak = 0
            return
        self.low_conf_streak += 1
        self.ledger.reflex_low_confidence += 1

    # -- end -------------------------------------------------------------
    def _finalize(self) -> None:
        self.event_ledger.flush()
        for ev in self.book.events:
            self.event_records.append(directive_event(ev))
        if self._pending is not None and not self._pending.decided:
            self.need_unanswered += 1
            self._pending = None

    def event_records_clean(self) -> List[dict]:
        return [_strip_wall(r) for r in self.event_records]

    def outcome(self) -> str:
        return recording.infer_outcome(self.mem.messages)


# ------------------------------------------------------------------ merge

def _agreement_counts(decisions):
    """Agreement over this pass's answered needs with a known original.

    Counts are per *pass* (one candidate provider), not per provider *label*:
    an offline paid tier degrades to a scripted proposal, so its label is
    ``scripted`` even though the pass is the ``jev`` candidate.
    """
    agree = total = 0
    for d in decisions:
        if d.get("record") != "need" or d.get("selected") is None:
            continue
        if d.get("actual_action_source") != "sidecar":
            continue
        total += 1
        if d.get("agreement"):
            agree += 1
    return agree, total


def _legality_counts(decisions):
    legal = total = 0
    for d in decisions:
        if d.get("record") != "need" or d.get("selected") is None:
            continue
        total += 1
        if d.get("legal"):
            legal += 1
    return legal, total


def _fallback_count(decisions):
    return sum(1 for d in decisions
               if d.get("record") == "need" and d.get("fallback"))


# ------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.agent.evaluate",
        description="Offline replay and comparison of a recorded episode")
    p.add_argument("wire", help="the recorded inbound wire (ep-N.wire.jsonl)")
    p.add_argument("--reflex", choices=["scripted", "jev"],
                   default="scripted",
                   help="the primary reflex tier to evaluate (default "
                        "scripted)")
    p.add_argument("--strategy", choices=["off", "deepseek"], default="off",
                   help="strategy tier (off is network-free)")
    p.add_argument("--provider", action="append", default=None,
                   choices=["scripted", "jev"],
                   help="add a candidate provider to compare (repeatable); "
                        "the --reflex tier is always included")
    p.add_argument("--actions", default=None,
                   help="a companion ep-N.actions.jsonl for ground-truth "
                        "comparison")
    p.add_argument("--decisions", default=None,
                   help="a companion ep-N.decisions.jsonl recording")
    p.add_argument("--output", required=True,
                   help="the per-need JSONL report")
    p.add_argument("--offline", action="store_true", default=False,
                   help="force a network-free evaluation (the default; "
                        "overrides --allow-network)")
    p.add_argument("--allow-network", action="store_true",
                   help="opt in to a real provider evaluation "
                        "(required for --strategy deepseek)")
    p.add_argument("--role", default="Valkyrie")
    p.add_argument("--max-ticks", type=int, default=2000)
    p.add_argument("--strategy-call-cap", type=int, default=8)
    p.add_argument("--postmortem-reserve", type=int, default=0)
    p.add_argument("--confidence-threshold", type=float, default=0.8)
    p.add_argument("--deepseek-model", default="deepseek-v4-flash")
    p.add_argument("--deepseek-key-file", default=None)
    p.add_argument("--deepseek-base-url",
                   default="https://api.deepseek.com")
    return p


def _config_from_args(a) -> ProviderConfig:
    return ProviderConfig(
        reflex=a.reflex, strategy=a.strategy, role=a.role,
        max_ticks=a.max_ticks, confidence_threshold=a.confidence_threshold,
        strategy_call_cap=a.strategy_call_cap,
        postmortem_reserve=a.postmortem_reserve,
        deepseek_model=a.deepseek_model,
        deepseek_base_url=a.deepseek_base_url,
        deepseek_key_file=a.deepseek_key_file)


def _read_wire(path: str) -> List[bytes]:
    with open(path, "rb") as fh:
        return fh.readlines()


def _providers_for(a) -> List[str]:
    order: List[str] = [a.reflex]
    for name in (a.provider or []):
        if name not in order:
            order.append(name)
    return order


def run_evaluation(a) -> int:
    allow_network = bool(a.allow_network) and not bool(a.offline)
    if a.strategy == "deepseek" and not allow_network:
        print("error: --strategy deepseek requires --allow-network "
              "(offline evaluation makes no provider calls)", file=sys.stderr)
        return 2
    wire_lines = _read_wire(a.wire)
    actions_index = load_actions_index(a.actions)
    decisions_file = load_decisions(a.decisions)
    config = _config_from_args(a)
    providers = _providers_for(a)

    passes: Dict[str, ReplayPass] = {}
    for name in providers:
        # Only the primary tier runs a strategy; comparison passes are
        # network-free so an N-provider comparison never multiplies paid
        # calls.
        strategy = a.strategy if name == providers[0] else "off"
        allow = allow_network and name == providers[0]
        p = ReplayPass(wire_lines, config, name, strategy,
                       allow_network=allow, actions_index=actions_index)
        p.run()
        passes[name] = p

    primary = passes[providers[0]]
    _recorded_selecteds = [d for d in decisions_file
                           if isinstance(d.get("selected"), dict)]

    out_records: List[dict] = []
    # per-need records come from the primary pass; each carries the primary
    # candidate, and the merge below adds every other provider's proposal for
    # the same need key.
    by_key: Dict[Tuple[int, int], dict] = {}
    ordered_keys: List[Tuple[int, int]] = []
    for d in primary.decisions:
        rec = dict(d)
        if d.get("record") == "need" \
                and d.get("need", {}).get("id") is not None:
            key = (d["need"]["seq"], d["need"]["id"])
            rec["candidates"] = {}
            by_key[key] = rec
            ordered_keys.append(key)
        out_records.append(rec)

    for name, p in passes.items():
        if name == providers[0]:
            continue
        for d in p.decisions:
            if d.get("record") != "need":
                continue
            key = (d.get("need", {}).get("seq"), d.get("need", {}).get("id"))
            rec = by_key.get(key)
            if rec is None:
                continue
            rec["candidates"][name] = {
                "provider": d.get("provider"), "proposal": d.get("proposal"),
                "selected": d.get("selected"),
                "legal": d.get("legal"), "fallback": d.get("fallback"),
                "agreement": d.get("agreement"),
                "reason": d.get("reason")}

    # attach recorded decisions (order-aligned to answered needs) when given
    if _recorded_selecteds:
        answer_index = 0
        for key in ordered_keys:
            rec = by_key[key]
            if rec.get("selected") is None:
                continue
            if answer_index >= len(_recorded_selecteds):
                break
            rd = _recorded_selecteds[answer_index]
            answer_index += 1
            rec["recorded"] = {
                "provider": rd.get("provider"),
                "selected": rd.get("selected"),
                "boundaries": rd.get("boundaries"),
                "agreement": (canonical_action(rec["need"], rd["selected"])
                              == canonical_action(rec["need"],
                                                  rec["selected"])),
            }

    # emit: per-need records, then the deterministic event ledger, then a
    # summary.  Wall timing has been dropped from every event record.
    for rec in out_records:
        rec.pop("schema", None)
        rec["schema"] = EVAL_SCHEMA
    events_clean = primary.event_records_clean()
    summary = _summarize(a, primary, passes, providers, actions_index,
                         decisions_file, len(wire_lines), events_clean)

    with open(a.output, "w") as fh:
        for rec in out_records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        for ev in events_clean:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
        fh.write(json.dumps(summary, sort_keys=True) + "\n")

    _print_summary(a, summary)
    return 0


def _summarize(a, primary, passes, providers, actions_index, decisions_file,
               wire_lines, events_clean):
    reason_tally: Dict[str, int] = {}
    boundary_events = 0
    directive_events = 0
    for ev in events_clean:
        if ev.get("record") == "boundary":
            boundary_events += 1
            k = ev.get("kind") or "?"
            reason_tally[k] = reason_tally.get(k, 0) + 1
        elif ev.get("record") == "directive":
            directive_events += 1

    agreements = {}
    legality = {}
    fallbacks = {}
    for name, p in passes.items():
        ag, tot = _agreement_counts(p.decisions)
        agreements[name] = {"agree": ag, "total": tot,
                            "rate": (round(ag / tot, 6) if tot else None)}
        lg, lt = _legality_counts(p.decisions)
        legality[name] = {"legal": lg, "total": lt,
                          "rate": (round(lg / lt, 6) if lt else None)}
        fallbacks[name] = _fallback_count(p.decisions)

    needs_total = primary.needs
    answered = primary.answered
    actual_known = sum(1 for d in primary.decisions
                       if d.get("record") == "need"
                       and d.get("actual_action_source") == "sidecar")
    notes = []
    if not actions_index:
        notes.append("no actions sidecar: actual_action=unknown, "
                     "agreement coverage is 0")
    if primary.protocol_failure:
        notes.append("protocol failure: %s" % primary.protocol_failure)
    if not primary.closed:
        notes.append("the replay reached EOF without a closed record")
    summary = {
        "schema": EVAL_SCHEMA, "record": "summary",
        "wire": os.path.basename(a.wire), "wire_lines": wire_lines,
        "reflex": a.reflex, "strategy": a.strategy,
        "providers": providers,
        "needs_total": needs_total, "needs_answered": answered,
        "decision_coverage": (round(answered / needs_total, 6)
                              if needs_total else None),
        "unanswered_needs": primary.need_unanswered,
        "invalids": primary.invalids,
        "closed": primary.closed, "eof": primary.eof,
        "protocol_failure": primary.protocol_failure,
        "outcome": primary.outcome(),
        "actual_known": actual_known,
        "actual_coverage": (round(actual_known / answered, 6)
                            if answered else None),
        "agreement": agreements, "legality": legality,
        "provider_fallbacks": fallbacks,
        "boundary_detections": boundary_events,
        "boundary_reasons": reason_tally,
        "directive_applications": primary.ledger.boundaries_applied,
        "directive_events": directive_events,
        "decisions_recorded": len(decisions_file),
        "budget": primary.ledger.as_dict(),
        "notes": notes,
    }
    return summary


def _print_summary(a, summary) -> None:
    print("evaluate: %s" % a.wire)
    print("  needs=%d answered=%d coverage=%s actual_known=%d" %
          (summary["needs_total"], summary["needs_answered"],
           summary["decision_coverage"], summary["actual_known"]))
    for name in summary["providers"]:
        ag = summary["agreement"][name]
        lg = summary["legality"][name]
        print("  %s: agreement=%s (%s/%s) legal=%s fallbacks=%d" %
              (name, ag["rate"], ag["agree"], ag["total"], lg["rate"],
               summary["provider_fallbacks"][name]))
    print("  boundaries=%d directives_applied=%d closed=%s outcome=%s" %
          (summary["boundary_detections"], summary["directive_applications"],
           summary["closed"], summary["outcome"]))
    for note in summary["notes"]:
        print("  note: %s" % note)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    a = build_parser().parse_args(argv)
    return run_evaluation(a)


if __name__ == "__main__":
    sys.exit(main())
