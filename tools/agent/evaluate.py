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
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import arbitration, candidates, instances, navigation, protocol, \
    recording, state
from .budget import BudgetLedger
from .codec import AssemblerLimit, ChunkError, IncrementalAssembler
from .controller import (_EpisodeRunner, _classified_terrain,
                         _crossed_dispatch_boundary)
from .directives import DirectiveBook, PreconditionState
from .events import (BoundaryQueue, EventLedger, directive_event, hunger_index,
                     lifecycle_event)
from .policy import INV_STALE_TICKS, ScriptedReflex
from .protocol import NeedKey, Request, Snapshot
from .providers import (NullStrategy, ProviderConfig, ReflexContext,
                        ScriptedReflexProvider, StrategyContext,
                        StrategyConversation, StrategyExchange,
                        prepare_strategy_request, reflex_provider,
                        strategy_provider, tariff_from_config)

EVAL_SCHEMA = 1

# Boundary history window kept for the strategy prompt, mirroring the live
# controller so a replay's prepared requests match what play would send.
_BOUNDARY_HISTORY_MAX = 16

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


# A displayed stack of N identical items is rendered with a leading count
# ("2 uncursed food rations"); a single item has no such prefix
# ("a +1 spear").
_COUNT_PREFIX = re.compile(r"^\s*\d+\s")


def _menu_row_meta(rows) -> Dict[int, dict]:
    """Delivered menu-row metadata keyed by row id (page ``r`` field)."""
    meta: Dict[int, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("r")
        if _is_int(rid):
            meta[int(rid)] = row
    return meta


def _row_is_single_item(meta, rid) -> bool:
    """True only when the delivered metadata *proves* the row is one item.

    ``-1`` ("the whole stack") and ``1`` ("one item") select the same thing
    only for a row that is not a stack.  That is proven from the delivered
    page metadata: the row's displayed text must carry no count prefix and its
    ``initial`` must not be a positive stack count.  With no delivered
    metadata the row cannot be proven single, so the counts stay distinct.
    """
    row = meta.get(rid)
    if row is None:
        return False
    text = row.get("text")
    if not isinstance(text, str) or _COUNT_PREFIX.match(text):
        return False
    initial = row.get("initial")
    if _is_int(initial) and initial > 1:
        return False
    return True


def _canonical_count(rid, count, meta):
    """The semantic count of one committed menu row.

    ``-1`` and ``1`` collapse to the same value only when the delivered
    metadata proves the row is a single item; otherwise they stay distinct.  A
    non-integer count is preserved as a tagged value so it never compares
    equal to a real count.
    """
    if not _is_int(count):
        return ("nonint", repr(count))
    c = int(count)
    if c in (-1, 1) and _row_is_single_item(meta, rid):
        return 1
    return c


def _count_sort_key(rc):
    c = rc[1]
    return (rc[0], 0, c) if _is_int(c) else (rc[0], 1, repr(c))


def canonical_action(need, action, rows=None) -> Tuple:
    """A semantic key for one action, comparable across two encodings.

    This is what an agreement check compares -- **not** the JSON.  The rules:

      * a ``cancel`` and an ``ack`` are their own shapes;
      * ``key``/``yn``/``position``/``text`` compare by value;
      * a ``menu`` commit compares as the **final set of rows with their
        semantic counts**, ignoring the menu *generation id* (scoped to this
        one need and equal for both sides).  Row order does not matter.  The
        count does: two commits that select the same rows with different
        counts are different actions.  ``-1`` and ``1`` normalize equal
        **only** when the delivered menu metadata (``rows``, the page rows
        carrying ``text``/``initial``) proves the row is a single item -- a
        displayed text with no count prefix and an ``initial`` that is not a
        positive stack count.  On a multi-item stack (or with no delivered
        metadata) they are preserved as distinct.

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
        meta = _menu_row_meta(rows)
        canon = []
        for row in commit:
            if isinstance(row, (list, tuple)) and len(row) == 2 \
                    and _is_int(row[0]):
                rid = int(row[0])
                canon.append((rid, _canonical_count(rid, row[1], meta)))
            else:
                return ("other", "menu")
        return ("menu", tuple(sorted(canon, key=_count_sort_key)))
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

class _ActionsIndex(object):
    """The actions sidecar as ordered per-need original attempts.

    Each ``(seq, id)`` maps to the original actions for that need **in the
    order they were sent**.  A need whose first attempt(s) the wire rejected
    with ``invalid`` therefore has more than one: the accepted candidate is
    the first attempt the wire did not reject, and the earlier ones are
    labelled rejected rather than silently treated as ground truth (see
    :func:`_scan_invalids` and ``ReplayPass._decide_pending``).
    """

    def __init__(self) -> None:
        self._attempts: Dict[Tuple[int, int], List[dict]] = {}

    def add(self, key: Tuple[int, int], action: dict) -> None:
        self._attempts.setdefault(key, []).append(action)

    def attempts(self, key: Tuple[int, int]) -> List[dict]:
        return list(self._attempts.get(key, ()))

    def rejected(self, key: Tuple[int, int], rejected: int) -> List[dict]:
        """The first *rejected* ordered attempts for a need."""
        return self.attempts(key)[:max(0, rejected)]

    def accepted(self, key: Tuple[int, int], rejected: int) -> Optional[dict]:
        """The first attempt the wire did not reject, or None if unknown.

        The wire rejected ``rejected`` attempts for this need, so the accepted
        candidate is the attempt that follows them.  With no recorded attempts
        beyond the rejected ones, the original action is unknown.
        """
        att = self._attempts.get(key, ())
        if 0 <= rejected < len(att):
            return att[rejected]
        return None

    def ground_truth(self, key: Tuple[int, int]) -> Optional[dict]:
        """The last recorded attempt for a need (its accepted candidate)."""
        att = self._attempts.get(key, ())
        return att[-1] if att else None

    def __bool__(self) -> bool:
        return bool(self._attempts)

    def __contains__(self, key) -> bool:
        return key in self._attempts


# ------------------------------------------------------------ sidecars

def load_actions_index(path: Optional[str]) -> _ActionsIndex:
    """The ordered original actions per ``(seq, id)`` from an actions sidecar.

    Only ``kind == "act"`` records are ground truth; ``get_page`` and
    ``ack_chunk`` are transport, not gameplay.  Every sent attempt is kept in
    send order, so a retry after an ``invalid`` is visible as a second attempt
    rather than silently overwriting (or being overwritten by) the first.
    """
    index = _ActionsIndex()
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
            index.add((seq, nid), action)
    return index


def _scan_invalids(wire_lines: List[bytes]) -> Dict[Tuple[int, int], int]:
    """Count wire ``invalid`` records per need key, before the pass runs.

    An ``invalid`` rejects the most recent need's most recent action, so it
    belongs to the last need seen.  Counting them up front lets a decision
    point know how many of a need's recorded attempts were rejected *before*
    it labels the accepted candidate -- the wire itself is not consulted for
    the label at decision time, only this deterministic pre-scan.  The scan is
    best-effort: a malformed stream stops it, and the pass then reports the
    protocol failure as usual.
    """
    counts: Dict[Tuple[int, int], int] = {}
    asm = IncrementalAssembler(
        max_retained_bytes=protocol.MAX_RETAINED_BYTES,
        max_chunks=protocol.MAX_CHUNKS, max_streams=protocol.MAX_STREAMS,
        max_line_bytes=protocol.MAX_PHYSICAL_LINE)
    current: Optional[Tuple[int, int]] = None
    try:
        for line in wire_lines:
            for rec in asm.feed(line):
                if not isinstance(rec, dict):
                    continue
                t = rec.get("type")
                if t == "obs":
                    need = rec.get("need")
                    seq = rec.get("seq")
                    if isinstance(need, dict) and _is_int(seq) \
                            and _is_int(need.get("id")):
                        current = (seq, need["id"])
                elif t == "invalid" and current is not None:
                    counts[current] = counts.get(current, 0) + 1
    except (AssemblerLimit, ChunkError, ValueError, KeyError, TypeError):
        pass
    return counts


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
                 actions_index: Optional[_ActionsIndex] = None) -> None:
        self.wire_lines = wire_lines
        self.config = config
        self.provider_name = provider_name
        self.strategy_name = strategy_name
        self.allow_network = allow_network
        self.actions_index = (actions_index if actions_index is not None
                              else _ActionsIndex())

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
        # Incremental lifecycle persistence (plan section 5), exactly as in the
        # live controller: each event is appended to the evaluator's event
        # records as it is recorded.
        self.reflex.lifecycle.sink = self._lifecycle_sink
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
        # The level instance the pending advice was produced for (plan 2.2
        # evaluator directive-scoping parity).
        self._strategy_instance = None
        self._strategy_prepared = None
        # Every pass starts empty; the conversation and boundary history are
        # pass-local, exactly like the live episode's.
        self._conversation = StrategyConversation(
            identity=(config.deepseek_model, config.deepseek_base_url),
            max_pairs=config.deepseek_history_pairs)
        self._boundary_history = deque(maxlen=_BOUNDARY_HISTORY_MAX)
        # Ground-truth correlation: the wire-rejected attempts per need are
        # known up front, the live count of invalid records seen so far labels
        # each rejected attempt, and the delivered page rows are kept so an
        # agreement check can prove a menu row is (or is not) a single item.
        self.invalids_by_key: Dict[Tuple[int, int], int] = {}
        self._last_key: Optional[Tuple[int, int]] = None
        self._last_need_kind: Optional[str] = None
        self._invalid_seen: Dict[Tuple[int, int], int] = {}
        self._rows_by_key: Dict[Tuple[int, int], list] = {}
        # Shared-arbitration migration (plan 6.2): the replay owns the same
        # instance/hero scope and the same "propose, then commit the selected
        # effect only after the reconciled observation" lifecycle as the live
        # controller.  The instance automaton, classified terrain and hero
        # possibility set are per pass, exactly like the live memory.
        self.instance = instances.LevelInstanceAutomaton()
        self.terrain = instances.TerrainMemory()
        self.herores = None
        self._resolved_hero = None
        self.observation_generation = 0
        # The modeled *sent* action (plan 6.2: only recorded/modeled sent
        # actions drive recorded-state reconciliation) and its frozen effect.
        self._sent_before: Optional[dict] = None
        self._sent_action = None
        self._sent_stair = False
        self._pending_effect = None
        self._last_observed_kind = ""
        # The modeled sent ordinal (plan 6.2/3.5).  Every need's answer is
        # one act, and an invalid retry is a second act, so this counts
        # exactly what the live controller's ``action_ordinal`` counts: the
        # ordinals stay aligned with the recording instead of collapsing a
        # rejected attempt into its predecessor.
        self._sent_ordinal = 0
        # The exact candidate the final selection resolved to (stall-recovery
        # plan §2), mirroring the live controller's selected-decision record:
        # populated equally by scripted selection and an accepted non-scripted
        # (Jev) choice, and cleared by a substitution.  The frozen effect is
        # taken from here, never re-derived from the wire action.
        self._selected_candidate = None

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
        # Correlation data is derived from the whole wire before any decision
        # point is evaluated: which advances may follow an invalid is a
        # property of the recording, not of arrival order.
        self.invalids_by_key = _scan_invalids(self.wire_lines)
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
            staged = self.mem.stage(self.snap)
        except protocol.ProtocolError as exc:
            self.protocol_failure = "invalid snapshot: %s" % exc
            self.closed = True
            return
        # Parse is not commit (plan 3.4/6.2): the hero possibility set and the
        # level-instance scope are settled from the staged frame and the
        # modeled sent action first, then memory commits exactly once, then
        # the committed observation is folded into the recovery evidence and
        # the frozen effect of the modeled send is committed.
        self._reconcile(staged)
        self.mem.commit(staged, hero=self._resolved_hero)
        self.reflex.note_observation(self.mem)
        self._commit_effect()
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
        self._last_key = (seq, need.get("id"))
        self._last_need_kind = need.get("kind")
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
        # Strict, exactly as the live controller: a page is only ever the
        # response to the one owed get_page.  The replay has no outbound side,
        # so it *models* that send: the owed request becomes the in-flight
        # one, and the incoming page must be that exact page, for this need's
        # content, declaring this need's total.  Any violation is an
        # evaluation protocol failure, not a silently dropped page.
        need = self._pending
        if need is None or need.decided or not self.req.need:
            self._fail("page delivered with no outstanding need")
            return
        if rec.get("content") != self.req.content:
            self._fail("page for unexpected content %r"
                       % (rec.get("content"),))
            return
        if self.req.in_flight is None:
            preq = self.req.next_page_request()
            if preq is None:
                self._fail("page delivered with no outstanding request")
                return
            self.req.mark_page_requested(preq["page"])
        idx = rec.get("page")
        if idx != self.req.in_flight:
            self._fail("page %r is not the outstanding page %r"
                       % (idx, self.req.in_flight))
            return
        total = rec.get("pages")
        if total != self.req.pages_declared:
            self._fail("page declares %r pages but the need declares %r"
                       % (total, self.req.pages_declared))
            return
        self.req.note_page(rec)
        need.delivered_pages.setdefault(idx, rec.get("rows") or [])
        if self.req.pages_complete():
            self._decide_pending()

    def _fail(self, reason: str) -> None:
        self.protocol_failure = reason
        self.closed = True

    # -- shared-arbitration reconciliation (plan 6.2) --------------------
    def _reconcile(self, staged) -> None:
        """Reconcile the modeled sent action and the instance before commit.

        Mirrors the live controller's order exactly (plan 4.1/4.2): classify
        the modeled attempt's outcome, reconcile the hero possibility set from
        the prior set and that attempt, let the instance automaton decide, and
        only then merge the arrival cells into the (possibly fresh) scope.
        """
        self.observation_generation += 1
        at_cells = tuple(staged.hero_cells)
        before = self._sent_before or {}
        kind = ""
        signals = []
        if before.get("dlvl") is not None \
                and before.get("dlvl") != staged.status.dlvl:
            signals.append(instances.S_LABEL)
        if self._sent_stair:
            signals.append(instances.S_STAIR)
        if arbitration.arrival_outcome(staged.messages):
            signals.append(instances.S_OUTCOME)
        if self._structural_conflict(staged):
            signals.append(instances.S_DISCONT)
        if before:
            same = (staged.hero is not None
                    and tuple(staged.hero) == tuple(before.get("hero") or ()))
            bt = before.get("time")
            nt = staged.status.time
            delta = (nt - bt) if (bt is not None and nt is not None) else None
            _outcome, kind = arbitration.classify_outcome(
                same, staged.hero is not None, delta)
        ev = self._movement_evidence(before, kind)
        prior = self.herores
        if prior is None:
            herores = instances.bootstrap_hero(
                at_cells, True, self.observation_generation)
        else:
            herores = instances.reconcile_hero(
                prior, ev, at_cells, self.observation_generation)
        if not signals and herores.resolved and kind != "moved":
            signals = (instances.S_NOARRIVAL,)
        was = self.instance.current()
        state_ = self.instance.observe(tuple(signals), herores.resolved)
        fresh = (state_.instance_id is not None and state_.instance_id != was
                 and self.instance.active())
        if fresh:
            self.mem.begin_instance(state_.instance_id)
            self.terrain = instances.TerrainMemory()
            self.book.on_instance_change(state_.instance_id, self.tick)
            self.reflex.begin_instance(state_.instance_id)
            herores = instances.bootstrap_hero(
                at_cells, bool(at_cells), self.observation_generation)
        self.herores = herores
        self._resolved_hero = herores.confirmed if herores.resolved else None
        self._last_observed_kind = kind
        self.terrain.merge(staged.cells)

    def _movement_evidence(self, before, kind):
        """Public movement evidence of the modeled attempt (plan 4.2)."""
        if not before:
            return instances.MovementEvidence()
        expected = None
        hero = before.get("hero")
        delta = arbitration.direction_delta(self._sent_action,
                                            protocol.DIR_KEYS)
        if delta is not None and hero is not None:
            expected = (hero[0] + delta[0], hero[1] + delta[1])
        return instances.MovementEvidence(
            nonmovement=(kind == "no-time"),
            time_advanced=(kind == "stationary-time-advanced"),
            expected=expected,
            unexpected=bool(kind == "moved" and expected is None),
            coherent=True)

    def _structural_conflict(self, staged) -> bool:
        for pos, raw in staged.cells.items():
            klass = _classified_terrain(raw)
            if klass not in instances.FIXED_TERRAIN:
                continue
            old = self.terrain.terrain.get(pos)
            if old in instances.FIXED_TERRAIN and old != klass:
                return True
        return False

    def _commit_effect(self) -> None:
        """Commit the frozen effect of the modeled send (plan 3.1/6.2)."""
        if self._pending_effect is None:
            return
        effect, label, payload = self._pending_effect
        # The frozen pre-send hero square of the modeled attempt is the
        # acquisition/continuation baseline (stall-recovery plan §2A), exactly
        # as the live controller passes it.
        pre_hero = (self._sent_before or {}).get("hero")
        self.reflex.commit_effect(effect, label, self.tick, self.mem,
                                  observed_kind=self._last_observed_kind,
                                  payload=payload, pre_hero=pre_hero)
        self._pending_effect = None

    def _on_invalid(self, rec) -> None:
        self.invalids += 1
        self.ledger.reflex_invalid += 1
        # `invalid` leaves the same request outstanding: a rejected *attempt*,
        # not a completed decision.  The modeled send it rejected is discarded
        # exactly as the live controller discards its in-flight attempt (3.1):
        # no frozen effect may be committed at the next observation and no
        # reconciliation evidence comes from the rejected action.
        self._pending_effect = None
        self._sent_action = None
        self._sent_stair = False
        self._sent_before = None
        # Mirror the live controller's invalid ownership: an invalid that
        # discards a modelled pickup send also cancels its in-flight freeze, so
        # the rejected attempt's evidence/baseline can never be classified by a
        # later observation (plan 3.3/1.5).  The evaluator models every invalid
        # as a terminal non-repair rejection (it has no delivery-repair branch),
        # so the freeze is always cancelled here rather than re-armed.
        self.reflex.cancel_pickup()
        # It is recorded as its own decision row, and -- when the attempt it
        # rejected is known -- that rejected action is labelled here, distinct
        # from the accepted ground truth.  The invalid count for this NeedKey
        # advances, so the accepted retry is the recorded attempt that follows
        # the rejected ones, and it becomes the modeled send the next
        # observation reconciles against (3.5).  An exhausted sidecar leaves
        # the retry unknown rather than re-using the rejected winner.
        key = self._last_key
        rejected_action = None
        retry_action = None
        retry_ordinal = None
        if key is not None:
            n = self._invalid_seen.get(key, 0)
            self._invalid_seen[key] = n + 1
            rejected_action = self.actions_index.accepted(key, n)
            retry_action = self.actions_index.accepted(key, n + 1)
            if retry_action is not None and retry_action != rejected_action:
                gameplay = self._last_need_kind in ("command", "key",
                                                    "direction")
                retry_ordinal = self._model_send(retry_action,
                                                 gameplay=gameplay)
                # A retry pickup action arms a *fresh* identity: the rejected
                # attempt's freeze was cancelled above, so the retry is a new
                # logical attempt (matching live's retry-send arming).
                cand = getattr(self.reflex, "last_candidate", None)
                if (cand is not None
                        and candidates.candidate_to_wire(cand) == retry_action
                        and cand.proposed_effect == "pickup"):
                    self._pending_effect = (
                        cand.proposed_effect, cand.semantic_label,
                        tuple(getattr(cand, "effect_payload", ())))
                    self.reflex.arm_pickup(self._pending_effect[2],
                                           identity=("pickup", retry_ordinal),
                                           tick=self.tick)
        self.decisions.append({
            "schema": EVAL_SCHEMA, "record": "need", "index": self.needs,
            "need": {"seq": (key[0] if key is not None else self.last_seq),
                     "id": (key[1] if key is not None else None),
                     "kind": self._last_need_kind},
            "proposal": None, "selected": None, "provider": "controller",
            "reason": "invalid:%s" % (rec.get("code"),),
            "legal": None, "fallback": True, "low_confidence": True,
            "agreement": None, "actual_action": None,
            "actual_action_source": "unknown",
            "rejected_action": rejected_action,
            "retry_action": retry_action,
            "sent_ordinal": retry_ordinal,
            "boundaries": [], "directives": [],
        })

    def _on_closed(self, rec) -> None:
        self.closed = True
        self.eof = False
        self.provider.on_closed()
        # A closed episode stops further gameplay commits: the in-flight
        # modeled attempt is discarded without crediting an outcome, and the
        # instance automaton stops -- exactly the live controller's close path
        # (3.4), so the terminal lifecycle matches live.
        self.instance.stop()
        self._pending_effect = None
        self._sent_action = None
        self._sent_stair = False
        self._sent_before = None
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
            self._boundary_history.append(
                {"eid": b.eid, "reason": b.reason, "tick": self.tick,
                 "level": self.mem.status.dlvl or ""})

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
        prepared = prepare_strategy_request(self.config, ctx,
                                            self._conversation)
        ctx.prepared_request = prepared
        if not prepared.fits:
            self._strategy_prepared = None
            self.boundary_queue.suppress("strategy-context-too-large")
            return
        if not self.ledger.reserve_strategy(
                prompt_tokens=prepared.prompt_bound,
                completion_tokens=prepared.completion_bound):
            self._strategy_prepared = None
            self.boundary_queue.suppress("strategy-cap")
            return
        self._strategy_prepared = prepared
        self.boundary_queue.mark_dispatched(self.tick, self.clock())
        self._strategy_level = self.mem.status.dlvl
        self._strategy_instance = self.instance.current()
        try:
            res = self.strategy.deliberate(ctx, self.clock()
                                           + self.config.strategy_deadline)
        except Exception:                    # noqa: BLE001 - bounded policy
            res = None
        usage = res.usage if res is not None else None
        # The same settlement rule as live play: a result that may have
        # reached the wire is committed, a known local refusal is released,
        # and an ambiguous None keeps its conservative exposure.
        if res is None or _crossed_dispatch_boundary(res):
            self.ledger.commit_strategy(usage)
        else:
            self.ledger.release_strategy()
        if res is not None and res.ok and res.directives:
            self._strategy_pending = res.directives[0]
            self._commit_history(prepared, res)
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

    def _commit_history(self, prepared, res) -> None:
        """Install the retained slice plus the new pair, capped to K.

        The committed assistant text is the verbatim validated content when
        present, else a stable serialization of the validated set -- the same
        rule as the live controller's, so a canned-response replay produces
        the same request bytes as play.
        """
        verbatim = getattr(res, "assistant_content", "")
        if not verbatim:
            first = res.directives[0]
            to_dict = getattr(first, "to_dict", None)
            if to_dict is not None:
                verbatim = json.dumps(to_dict(), sort_keys=True)
            elif isinstance(first, dict):
                verbatim = json.dumps(first, sort_keys=True)
        self._conversation.install(
            prepared.retained,
            StrategyExchange(user=prepared.user_text, assistant=verbatim))

    def _build_strategy_context(self, pending):
        st = self.mem.status
        bits = []
        if st.hp is not None and st.hp_max:
            bits.append("HP %d/%d" % (st.hp, st.hp_max))
        if st.hunger:
            bits.append("Hunger %s" % st.hunger)
        if st.dlvl:
            bits.append("Dlvl %s" % st.dlvl)
        view = self.book.view(self.tick, st.dlvl, self._precondition_state(),
                              instance=self.instance.current())
        return StrategyContext(
            episode=1, tick=self.tick,
            summary={"hp": st.hp, "hp_max": st.hp_max, "dlvl": st.dlvl},
            boundaries=list(pending.eids),
            map_text=state.render_map(self.mem),
            status_text=", ".join(bits),
            recent_messages=self.mem.recent_messages(6),
            inventory=[(r.get("text") or "")
                       for r in self.mem.inventory.rows],
            history=list(self._boundary_history),
            remaining_budget=self._remaining_budget(), level=st.dlvl,
            role=self.config.role,
            directives=[view.dset] if view.active else [],
            inventory_age_text=self._inventory_age_text(),
            conditions=self._visible_conditions(),
            item_evidence=self._strategy_item_evidence(),
            commitment=self._commitment_record())

    def _inventory_age_text(self) -> str:
        """The cached-inventory freshness (plan 2.3), or ``unknown``."""
        inv = getattr(self.mem, "inventory", None)
        seen = getattr(inv, "seen_tick", None)
        if seen is None:
            return "unknown (never read)"
        return "%d ticks ago" % max(0, self.tick - int(seen))

    def _visible_conditions(self):
        """The displayed condition names from the current snapshot."""
        out = []
        for entry in getattr(self.snap, "cond", ()) or ():
            text = ""
            if isinstance(entry, dict):
                text = (entry.get("text") or "").strip()
            if text:
                out.append(text)
        return out

    def _strategy_item_evidence(self):
        """Player-visible floor item evidence (plan 2.3)."""
        floor = getattr(self.reflex, "floor", None)
        if floor is None:
            return []
        out = []
        for pos in floor.evidence_positions()[:40]:
            ev = floor.evidence(pos)
            if ev is not None:
                out.append([int(ev.pos[0]), int(ev.pos[1]),
                            str(ev.appearance), int(ev.source_epoch)])
        return out

    def _commitment_record(self):
        """The active destination commitment for the strategy prompt (§2.3)."""
        store = getattr(self.reflex, "targets", None)
        commitment = store.held() if store is not None else None
        if commitment is None:
            return None
        return {"purpose": commitment.purpose,
                "pos": [int(commitment.pos[0]), int(commitment.pos[1])],
                "phase": commitment.phase,
                "source": commitment.source,
                "generation": int(commitment.generation)}

    def _remaining_budget(self):
        spendable = self.ledger.strategy_cap - self.ledger.postmortem_reserve
        spent = (self.ledger.strategy_dispatched
                 + self.ledger.strategy_reserved)
        return max(0, spendable - spent)

    def _activate_directives(self, need) -> None:
        if self._strategy_pending is None:
            return
        kind = need.get("kind")
        if kind not in ("command", "key", "direction"):
            return
        dset = self._strategy_pending
        # The one application rule (plan 1.5, evaluator parity): a v2
        # destination set activates only on a genuine command need.
        if getattr(dset, "schema_version", 1) >= 2 and kind != "command":
            return
        self._strategy_pending = None
        level = self.mem.status.dlvl
        if self._strategy_level is not None and level is not None \
                and level != self._strategy_level:
            self.boundary_queue.finish(False, "stale-level")
            return
        # Instance scoping parity with live (plan 2.2): advice produced for a
        # different level instance is stale and never activates.
        current_instance = self.instance.current()
        source_instance = self._strategy_instance
        self._strategy_instance = None
        if source_instance is not None and current_instance is not None \
                and source_instance != current_instance:
            self.boundary_queue.finish(False, "stale-instance")
            return
        self.book.activate(dset, self.tick, level, instance=current_instance)
        self._record_directive_eligible()
        self.boundary_queue.finish(True)

    def _lifecycle_sink(self, ev) -> None:
        """Incremental lifecycle persistence into the event records (§5)."""
        self.event_records.append(lifecycle_event(ev))

    def _record_directive_eligible(self) -> None:
        """Record an explicit-destination directive's eligibility (§5).

        Mirrors the live controller: emitted at the command boundary where the
        advice activates, and only for destination-bearing advice.
        """
        from . import directives as directives_mod
        from . import lifecycle_metrics as lm
        dset = self.book.active(self.tick, self.mem.status.dlvl,
                                self._precondition_state(),
                                instance=self.instance.current())
        if dset is None:
            return
        goals = tuple(getattr(dset, "goals", ()) or ())
        if getattr(dset, "target", None) is None and not any(
                g in directives_mod.POSITIONAL_GOALS for g in goals):
            return
        self.reflex.lifecycle.record(lm.KIND_DIRECTIVE, lm.DIR_ELIGIBLE,
                                     generation=self.book.generation)

    # -- decision --------------------------------------------------------
    def _decide_pending(self) -> None:
        need = self._pending
        if need is None or need.decided:
            return
        need.decided = True
        # The selected-decision record is rebuilt for this decision only (plan
        # §2): a stale candidate can never arm this effect.
        self._selected_candidate = None
        rows = []
        for k in range(need.declared_pages):
            rows.extend(need.delivered_pages.get(k, []))

        if self.strategy_live:
            self._service_strategy()
        self._activate_directives(need.need)

        view = self.book.view(self.tick, self.mem.status.dlvl,
                              self._precondition_state(),
                              instance=self.instance.current())
        ctx = ReflexContext(
            episode=1, tick=self.tick, need=need.need,
            need_key=NeedKey(1, need.seq, need.nid), snapshot=self.snap,
            pages=rows, memory=self.mem,
            # Same wiring as the live controller: the persistent classified
            # terrain and the scripted reflex's pending intent.
            terrain=self.terrain,
            intent=getattr(self.reflex, "intent", "") or "",
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
            # a substitution is not the selected candidate (plan §2)
            self._selected_candidate = None
        else:
            selected = proposal

        legal = protocol.validate_action(need.need, selected) is None
        low = fallback or provider_label != self.provider_name

        key = (need.seq, need.nid)
        self._rows_by_key[key] = rows
        # Ground truth is the *accepted* attempt: the wire rejected the first
        # ``rejected_n`` recorded actions (each labelled at its own invalid
        # record), so the accepted candidate is the next one.  With every
        # attempt rejected, the original action is unknown -- never the first
        # rejected one.
        rejected_n = self.invalids_by_key.get(key, 0)
        rejected_actions = self.actions_index.rejected(key, rejected_n)
        actual = self.actions_index.accepted(key, rejected_n)
        if actual is None:
            agreement = None
            actual_source = "unknown"
        else:
            agreement = (canonical_action(need.need, actual, rows)
                         == canonical_action(need.need, selected, rows))
            actual_source = "sidecar"

        self._note_low_conf(low)
        # Model the send (plan 6.2): the selected action is treated as sent,
        # so its frozen effect is committed at the NEXT reconciled
        # observation -- never here.  A gameplay command additionally models
        # the SentAttempt (motion/tick); a non-command need freezes only its
        # effect (and payload), exactly as the live controller arms no attempt
        # for menu/prompt/ack/line/extcmd/position.
        self._pending_effect = None
        self._sent_action = None
        self._sent_stair = False
        self._sent_before = None
        kind = need.need.get("kind")
        # The exact selected candidate -- scripted or accepted Jev alike --
        # owns the frozen effect; the wire action only proves agreement (§2).
        cand = self._selected_candidate
        matched = (cand is not None
                   and candidates.candidate_to_wire(cand) == selected)
        # Every need's answer is one act, so it takes the next sent ordinal --
        # whichever kind it is -- exactly as the live controller's single send
        # advances ``action_ordinal``.
        ordinal = self._model_send(
            selected, gameplay=kind in ("command", "key", "direction"))
        if kind in ("command", "key", "direction"):
            # Bind door refusal to this modeled attempt's message baseline
            # (plan §4), exactly as the live controller does at its send
            # boundary, so live/evaluator refusal classification agrees.
            payload_tuple = tuple(getattr(cand, "effect_payload", ())) \
                if matched and cand is not None else ()
            purpose = (payload_tuple[3]
                       if payload_tuple and payload_tuple[0] == "dest" else None)
            arm_baseline = getattr(self.reflex, "arm_door_baseline", None)
            if arm_baseline is not None:
                arm_baseline(self.mem.message_count
                             if purpose == navigation.COMMIT_OPEN_DOOR
                             else None)
            if matched:
                self._pending_effect = (
                    cand.proposed_effect, cand.semantic_label,
                    tuple(getattr(cand, "effect_payload", ())))
                if cand.proposed_effect == "pickup":
                    # Freeze the pickup attempt at the send boundary (1.5/3.3)
                    self.reflex.arm_pickup(self._pending_effect[2],
                                           tick=self.tick)
        elif matched and getattr(cand, "proposed_effect", ""):
            self._pending_effect = (
                cand.proposed_effect, cand.semantic_label,
                tuple(getattr(cand, "effect_payload", ())))
        self.decisions.append({
            "schema": EVAL_SCHEMA, "record": "need", "index": need.index,
            "need": {"seq": need.seq, "id": need.nid,
                     "kind": need.need.get("kind")},
            "provider": provider_label,
            "proposal": proposal, "selected": selected,
            "reason": reason, "legal": legal, "fallback": fallback,
            "low_confidence": low,
            "actual_action": actual, "actual_action_source": actual_source,
            "rejected_attempts": rejected_actions,
            "rejected_count": len(rejected_actions),
            "agreement": agreement, "sent_ordinal": ordinal,
            "boundaries": list(need.boundaries),
            "directives": [view.dset.to_dict()] if view.active else [],
        })
        self.answered += 1
        if kind in ("command", "key", "direction"):
            self.tick += 1
        self._pending = None

    def _model_send(self, action, gameplay: bool = True) -> int:
        """Model one *sent* act and bind its reconciliation evidence (6.2).

        The ordinal advances for every answer -- a gameplay command and a
        non-command prompt alike -- and is advanced again by
        :meth:`_on_invalid` for the correlated retry, so the modeled
        ordinals match the live controller's ``action_ordinal`` rather than
        collapsing a rejected attempt.  Only a gameplay send models the
        SentAttempt's motion baseline; a non-command send freezes no movement
        evidence, exactly as the live controller arms no attempt for it.
        """
        self._sent_ordinal += 1
        if gameplay:
            self._sent_action = candidates.wire_to_action(action)
            self._sent_stair = (self._sent_action.tag == "key"
                                and self._sent_action.payload[0]
                                in (ord(">"), ord("<")))
            self._sent_before = {"hero": self.mem.hero,
                                 "time": self.mem.status.time,
                                 "dlvl": self.mem.status.dlvl}
        return self._sent_ordinal

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
                self._selected_candidate = None
                return None, "controller", "reflex returned no result", True
            self.ledger.reflex_successful += 1
            # The scripted decision populates the selected-decision record
            # exactly as an accepted Jev choice does (plan §2).
            self._selected_candidate = getattr(self.reflex, "last_candidate",
                                               None)
            return res.action, res.provider or "scripted", res.reason, False
        # jev (or any other paid reflex)
        scripted = self.reflex.fallback(ctx)
        fb_action = scripted.action if scripted is not None else None
        avail = self.provider.available(self.config)
        if not avail.enabled:
            self.ledger.reflex_fallback += 1
            self._selected_candidate = self._scripted_candidate_for(fb_action)
            return (fb_action, "scripted",
                    "jev unavailable: %s" % avail.reason, True)
        if not self.allow_network:
            self.ledger.reflex_fallback += 1
            self._selected_candidate = self._scripted_candidate_for(fb_action)
            return (fb_action, "scripted",
                    "jev not evaluated offline", True)
        res = self.provider.decide(
            ctx, self.clock() + self.config.reflex_deadline)
        usage = res.usage if res is not None else {}
        self.ledger.add_usage(usage)
        if res is None or res.action is None:
            self.ledger.reflex_fallback += 1
            why = (res.reason if res is not None else None) or "no answer"
            self._selected_candidate = self._scripted_candidate_for(fb_action)
            return (fb_action, "scripted", "jev fallback: %s" % why, True)
        self.ledger.reflex_successful += 1
        # The accepted member owns the frozen effect (plan §2): use the
        # provider-exposed candidate when it has one, else the reflex's
        # retained candidate only when its wire action is exactly the sent one.
        self._selected_candidate = (
            getattr(res, "candidate", None)
            or self._scripted_candidate_for(res.action))
        return res.action, res.provider or "jev", res.reason, False

    def _scripted_candidate_for(self, action):
        """The reflex's retained candidate when its wire action matches."""
        cand = getattr(self.reflex, "last_candidate", None)
        try:
            if cand is not None and candidates.candidate_to_wire(cand) == action:
                return cand
        except Exception:                    # noqa: BLE001 - defensive
            return None
        return None

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
        # The destination/pickup lifecycle stream is persisted additively in
        # the same event sidecar (plan section 5), in the evaluator exactly as
        # in the live controller.  With the incremental sink active this drains
        # nothing (no duplicates).
        for ev in self.reflex.lifecycle.drain_pending():
            self.event_records.append(lifecycle_event(ev))
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
    p.add_argument("--jev-confidence-mode", choices=["relative", "absolute"],
                   default="relative")
    p.add_argument("--jev-relative-factor", type=float, default=1.5)
    p.add_argument("--deepseek-model", default="deepseek-v4-flash")
    p.add_argument("--deepseek-key-file", default=None)
    p.add_argument("--deepseek-base-url",
                   default="https://api.deepseek.com")
    p.add_argument("--deepseek-history-pairs", type=int, default=8,
                   help="bounded episode-local conversation pairs (0..64; 0 "
                        "is the stateless rollback)")
    p.add_argument("--deepseek-context-max-bytes", type=int, default=262144,
                   help="payload byte ceiling for the prepared request")
    p.add_argument("--deepseek-price-in", type=float, default=None,
                   help="operator-configured USD per Mtok prompt tokens")
    p.add_argument("--deepseek-price-out", type=float, default=None,
                   help="operator-configured USD per Mtok completion tokens")
    p.add_argument("--deepseek-price-cache-hit", type=float, default=None,
                   help="operator-configured USD per Mtok prompt cache hits")
    p.add_argument("--token-cap", type=int, default=0)
    p.add_argument("--usd-cap", type=float, default=None)
    return p


def _config_from_args(a) -> ProviderConfig:
    return ProviderConfig(
        reflex=a.reflex, strategy=a.strategy, role=a.role,
        max_ticks=a.max_ticks, confidence_threshold=a.confidence_threshold,
        jev_confidence_mode=a.jev_confidence_mode,
        jev_relative_factor=a.jev_relative_factor,
        strategy_call_cap=a.strategy_call_cap,
        postmortem_reserve=a.postmortem_reserve,
        deepseek_model=a.deepseek_model,
        deepseek_base_url=a.deepseek_base_url,
        deepseek_key_file=a.deepseek_key_file,
        deepseek_history_pairs=a.deepseek_history_pairs,
        deepseek_context_max_bytes=a.deepseek_context_max_bytes,
        deepseek_price_in=a.deepseek_price_in,
        deepseek_price_out=a.deepseek_price_out,
        deepseek_price_cache_hit=a.deepseek_price_cache_hit,
        token_cap=a.token_cap, usd_cap=a.usd_cap)


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
    if a.reflex == "jev" and allow_network:
        # The Jev reflex is replay-incompatible: an offline replay of a Jev
        # episode is a scripted fallback with zero paid calls, and a live
        # networked Jev evaluation is refused outright (the adapter's Wave-A
        # dispatch barrier and accounting/evaluator gates must pass first).
        print("error: --reflex jev with --allow-network is not supported "
              "(offline Jev replay falls back to the scripted tier)",
              file=sys.stderr)
        return 2
    wire_lines = _read_wire(a.wire)
    actions_index = load_actions_index(a.actions)
    decisions_file = load_decisions(a.decisions)
    config = _config_from_args(a)
    # The evaluator shares the single validation authority with live autoplay
    # (``ProviderConfig.validate``, which handles the campaign fields it does
    # not own as ``None``): an out-of-range history pair count, a zero byte
    # ceiling or a malformed tariff is rejected here, before any pass is
    # constructed or any provider call is made, with no output artifact.
    problem = config.validate()
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return 2
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
        # Only *answered* rows index a need: rejected retry attempts share
        # the same (seq,id) and would otherwise overwrite the answered row,
        # losing its comparison/recorded-decision attachments.
        if d.get("record") == "need" \
                and d.get("selected") is not None \
                and d.get("need", {}).get("id") is not None:
            key = (d["need"]["seq"], d["need"]["id"])
            if key in by_key:
                continue        # one answered row per need
            rec["candidates"] = {}
            by_key[key] = rec
            ordered_keys.append(key)
        out_records.append(rec)

    for name, p in passes.items():
        if name == providers[0]:
            continue
        for d in p.decisions:
            if d.get("record") != "need" or d.get("selected") is None:
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
            rows = primary._rows_by_key.get(key)
            rec["recorded"] = {
                "provider": rd.get("provider"),
                "selected": rd.get("selected"),
                "boundaries": rd.get("boundaries"),
                "agreement": (canonical_action(rec["need"], rd["selected"],
                                               rows)
                              == canonical_action(rec["need"],
                                                  rec["selected"], rows)),
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
