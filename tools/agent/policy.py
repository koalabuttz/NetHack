"""ScriptedReflex: the always-available scripted policy.

The play agent's inline policy was not recoverable from the tree, so the
campaign's working heuristics are reconstructed here explicitly (see
``doc/agent-autoplay-plan.md`` section "ScriptedReflex"):

  * startup: pick the configured role / race / alignment and explicitly
    decline the tutorial from a *recognised* menu; an unrecognised menu is
    cancelled rather than blindly confirmed (a bare "first selectable row"
    could accept a tutorial or a destructive choice);
  * safety: explicit low-HP disengagement, and a monster is never walked into
    -- public appearance is ambiguous, so getting unblocked means searching,
    routing around or changing plan, never attacking a possible pet;
  * navigation: Dijkstra over remembered, publicly observed terrain with a
    visit-count penalty, preferring known down-stairs, then reachable
    frontiers, with bounded searching to break loops; **unknown cells are not
    walkable in the fallback** -- exploration happens only through deliberate
    observed edges (frontier cells border known floor);
  * hunger: a known-safe food allowlist (corpses and unrecognised items are
    not assumed safe), with a mandatory loop breaker after two equivalent
    rejected food intents;
  * rest: only when provably safe -- no adjacent monster, adequate HP and not
    hungry;
  * inventory: refreshed from recognised inventory content and by a bounded
    periodic ``i`` when the cache goes stale;
  * every need has a total fallback path so no request is ever left
    unanswered.

Confidence is a documented heuristic uncertainty score, not calibrated
probability.  Structural validity is separate from safety confidence.
"""

import heapq
import random
import time
from typing import Dict, List, Optional, Tuple

from . import (candidates, forced_search, instances, lifecycle_metrics,
               navigation, pickup, protocol, recovery, state)
from .arbitration import select_retained
from .directives import DirectiveView
from .providers import ReflexContext, ReflexResult, ReflexTimeout

KEY = protocol

# Safety thresholds (heuristic policy constants, not calibrated values).
LOW_HP_FRACTION = 0.30          # disengage at or below this HP fraction
DISENGAGE_HP_FRACTION = 0.50    # ... raised while a disengage directive holds
ADEQUATE_HP_FRACTION = 0.50     # rest is only considered above this
INV_STALE_TICKS = 240           # refresh the inventory cache after this many
INV_REFRESH_COOLDOWN = 40       # ... but never more often than this
EAT_RETRY_INTERVAL = 25
EAT_DIRECTIVE_INTERVAL = 10     # eat more eagerly under acquire_food

# Recognised startup menu kinds.  Only these are answered with a selection;
# anything else is cancelled.
_MENU_KEYWORDS = (("tutorial", "tutorial"),
                  ("role", "role"),
                  ("profession", "role"),
                  ("race", "race"),
                  ("alignment", "alignment"),
                  ("creed", "alignment"),
                  ("start game", "ok"),
                  ("ok", "ok"))


def bracket_info(prompt: str) -> Tuple[List[str], bool, bool]:
    """Extract the bracketed answer hints from a native prompt.

    Returns (letters, has_star, has_qmark).  Connector words ("or", "and") are
    removed before single alphanumerics are collected, so "[d or ?*]" yields
    (["d"], True, True) and "[ynaq]" yields (["y","n","a","q"], False, False).
    """
    i = prompt.rfind("[")
    j = prompt.rfind("]")
    if i < 0 or j <= i:
        return [], False, False
    content = prompt[i + 1:j]
    content = content.replace(" or ", " ").replace(" and ", " ")
    letters = [c for c in content if c.isalnum()]
    return letters, "*" in content, "?" in content


def menu_kind(title: str) -> str:
    """Classify a menu by its displayed title; "" means unrecognised."""
    low = (title or "").lower()
    for needle, kind in _MENU_KEYWORDS:
        if needle in low:
            return kind
    return ""


def _join_effects(tags) -> str:
    """Canonical effect tag for a decision's frozen effect set (plan 3.1).

    One non-command decision can commit more than one effect (an inventory
    refresh together with an intent transition), so the tags are joined by
    ``+`` in their deterministic construction order; an empty set is the
    no-op ``"prompt"``.
    """
    tags = [t for t in tags if t]
    return "+".join(tags) if tags else "prompt"


def condition_texts(snapshot) -> Tuple[str, ...]:
    """The displayed condition names from an observation (public evidence).

    ``snap.cond`` is the engine's ordered condition list of
    ``{text, color, style}`` records (``doc/agent-v1.schema.json``, the
    ``cond_entry`` definition).  Only the ``text`` field is a condition name;
    an entry without one is skipped rather than guessed at.
    """
    out = []
    for entry in getattr(snapshot, "cond", ()) or ():
        if isinstance(entry, dict):
            text = (entry.get("text") or "").strip()
        else:
            text = ""
        if text:
            out.append(text)
    return tuple(out)


class ScriptedReflex(object):
    """Deterministic, always-available scripted decision policy."""

    def __init__(self, config) -> None:
        self.config = config
        self.intent = ""            # "", "eat", "quit"
        self.selection_done = False
        self.eat_reject_base = 0
        self.eat_forced_menu = False
        self.last_eat_tick = -1000
        self.last_inv_tick = -1000
        self.quitting = False
        self.quit_reason = ""
        self.last_hero: Optional[Tuple[int, int]] = None
        self.stuck = 0
        self.rng = random.Random(0)
        self.max_ticks = 2000
        # The active directive view the reflex may consult (never an action
        # source: a directive can only bias a decision the reflex already
        # knows how to build).
        self.directives = DirectiveView(None)
        # Absolute monotonic deadline for the decision in flight (0 = none).
        # The controller sets it via ReflexContext.deadline; it is checked at
        # every loop boundary so a single scripted decision cannot overrun its
        # allowance.
        self.deadline = 0.0
        # Wave-3 identity/bookkeeping.  The table version and the
        # controller-owned rejection version are canonical table inputs, and
        # the active level-instance id scopes directive settlement; none is a
        # gameplay-memory mutation.
        self._table_version = 1
        self.rejection_version = 0
        self.instance_id = 0
        # Wave-4 bounded recovery: the per-site search budget, refusal
        # fingerprint, cycle detector and scoped food negatives.  Reflex-local
        # only; a proposal never mutates gameplay memory here.
        self.recovery = recovery.RecoveryState()
        self.food = recovery.FoodNegatives()
        # The instance-scoped destination commitment (plan section 1): one
        # persistent committed destination plus its serviced/failed ledgers.
        # Mutated only at a reconcile/effect boundary, never during prepare.
        self.targets = navigation.CommitmentStore()
        # The instance-scoped floor-item evidence ledger (plan section 3):
        # source-epoch tokens, bounded attempts, declines and negatives.
        self.floor = pickup.FloorLedger()
        # The most recent unresolved explicit-destination reason (reflex-local
        # policy bookkeeping, set during preparation, never gameplay memory).
        self.last_unresolved = ""
        # The directive-owned destination settlement the controller applies at
        # its reconcile boundary (plan 1.5 "directive"): a
        # ``(outcome, generation, reason)`` tuple or ``None``.
        self.directive_settlement = None
        # The in-flight pickup intent (plan 3.3): its purpose, the frozen
        # evidence token it was authorized against, the inventory signature at
        # initiation (for the success delta) and the originating generation.
        self.pickup_purpose = ""
        self.pickup_evidence = None
        self.pickup_init_inventory = None
        self.pickup_generation = 0
        # The frozen in-flight pickup attempt (plan 1.5/3.3), installed at the
        # *send* boundary so the observation that reports the result is folded
        # against a pre-send baseline rather than a post-result one.
        self.pickup_pending = None
        # The stable identity of the in-flight pickup attempt (the original
        # accepted decision's send), so a delivery repair that resends the
        # frozen action cannot consume a second attempt (plan 3.3).
        self.pickup_attempt_identity = None
        # True when an active collect destination already sits under the hero,
        # so the acquired action is the pickup initiation (plan 1.5/3.3).
        self.on_square_collect = False
        # The additive, schema-versioned lifecycle event stream (plan section
        # 5): recorded at the reconcile folds, so the live controller and the
        # evaluator -- which both drive this reflex -- persist the same events.
        self.lifecycle = lifecycle_metrics.LifecycleRecorder()
        self._cycled = False
        # The last prepared table and retained candidate, exposed for the
        # controller-owned SentAttempt lifecycle (set in decide()).
        self.last_prepared = None
        self.last_candidate = None
        # The reflex-local activation evidence (gates 1-5) of the most recent
        # forced-search proposal, exposed for the controller to merge with its
        # own transport/lifecycle gates (wave 5).  None when no proposal was
        # made.  The reflex never sends anything: it only nominates.
        self.forced_template = None
        # The current need's kind and the displayed condition names, bound
        # from the context at the start of each decision so the forced-search
        # template reads the same public evidence the decision itself did.
        self._pending_kind = ""
        self._conditions = ()

    # -- provider surface ------------------------------------------------
    def _check_deadline(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            raise ReflexTimeout("scripted reflex exceeded its deadline")

    def decide(self, context: ReflexContext) -> ReflexResult:
        self.deadline = float(getattr(context, "deadline", 0.0) or 0.0)
        self.directives = _directive_view(context)
        self._pending_kind = (context.need or {}).get("kind")
        self._conditions = condition_texts(context.snapshot)
        self._check_deadline()
        kind = (context.need or {}).get("kind")
        prepared = getattr(context, "prepared", None)
        if prepared is None:
            prepared = self.prepare(context)
        self.last_prepared = prepared
        cand = self._select(prepared, getattr(context, "rejected", None))
        if cand is None:
            # member exhaustion: a reviewed per-kind structural fallback,
            # never an infinite `s` (3.5).  A non-command table always holds
            # its single scripted member, so only a gameplay command can
            # empty out under rejection.
            self.last_candidate = None
            return ReflexResult(action=self._exhausted_action(kind),
                                confidence=0.5, provider="scripted",
                                reason="exhausted: structural fallback")
        # The candidate's proposed effect is *frozen* on the candidate (and so
        # on the armed SentAttempt); it is never committed here.  The
        # controller commits it only after a complete send and the
        # corresponding reconciled observation (plan 3.1), so preparation and
        # proposal stay observational -- for gameplay commands *and* for every
        # non-command need (menu/prompt/ack/line/extcmd/position).
        self.last_candidate = cand
        return ReflexResult(action=candidates.candidate_to_wire(cand),
                            confidence=0.5, provider="scripted",
                            reason=cand.reason or cand.semantic_label)

    @staticmethod
    def _exhausted_action(kind) -> dict:
        """The structural fallback for a fully-rejected gameplay table."""
        if kind in ("command", "key", "direction"):
            return {"key": KEY.KEY_SEARCH}
        return {"key": KEY.KEY_ESC}

    def prepare(self, context: ReflexContext) -> "candidates.PreparedReflex":
        """Pure multi-candidate preparation into one immutable table (3.1).

        Builds the single retained table of at most
        :data:`candidates.MAX_CANDIDATES` deterministic candidates for this
        decision.  Only reflex-local bookkeeping can change here; gameplay
        memory is never mutated, so a proposal can never become an outcome.
        Command/key/direction needs are fully candidate-based; menus, prompts
        and other kinds stay scripted and degrade to a one-row table.
        """
        self.deadline = float(getattr(context, "deadline", 0.0) or 0.0)
        self.directives = _directive_view(context)
        self._pending_kind = (context.need or {}).get("kind")
        self._conditions = condition_texts(context.snapshot)
        kind = (context.need or {}).get("kind")
        if kind in ("command", "key", "direction"):
            cands = self._command_candidates(context)
        else:
            cands = self._noncommand_candidate(context, kind)
        features = self._features(context)
        table = candidates.build_table(
            self._need_key(context), self._table_version, cands,
            features_digest=features.digest(), jev_eligibility=False,
            rejection_version=self.rejection_version)
        return candidates.PreparedReflex(immutable_features=features,
                                         table=table)

    def _decide_scripted_kind(self, context, kind):
        """Compatibility wrapper: the single non-command scripted proposal.

        Production reaches non-command needs through :meth:`prepare` /
        :meth:`decide`, which freeze the effect on the candidate; this
        wrapper is kept for direct callers and returns the same action.
        """
        action, reason, _effect, _payload = self._noncommand(context, kind)
        return ReflexResult(action=action, confidence=0.5,
                            provider="scripted", reason=reason)

    def _noncommand_candidate(self, context, kind):
        """The frozen non-command candidate (plan 3.1).

        A menu/prompt/ack/line/extcmd/position need degrades to a one-row
        table whose member carries the *proposed effect* -- and, for an
        inventory listing, the frozen payload -- that
        :meth:`commit_effect` applies only after the controller observes the
        result of a complete send.  Nothing here mutates reflex or gameplay
        state.
        """
        action, reason, effect, payload = self._noncommand(context, kind)
        return (candidates.make_candidate(
            action, "prompt", family="prompt", score=0, reason=reason,
            proposed_effect=effect, effect_payload=payload),)

    def _noncommand(self, context, kind):
        """A pure non-command proposal: ``(action, reason, effect, payload)``.

        ``effect`` is the canonical effect tag (several tags joined by ``+``
        when one decision both refreshes the inventory cache and transitions
        an intent); ``payload`` is the frozen data an inventory refresh needs
        at its commit boundary.  No state is mutated here (plan 3.1).
        """
        if kind == "menu":
            return self._menu(context)
        if kind == "yn":
            return self._yn(context)
        if kind == "ack":
            return {"ack": True}, "acknowledge display", "prompt", ()
        if kind in ("line", "extcmd"):
            action, reason = self._textish(context)
            return action, reason, "prompt", ()
        if kind == "position":
            return {"key": KEY.KEY_ESC}, "cancel position request", \
                "prompt", ()
        return {"key": KEY.KEY_ESC}, "unknown kind fallback", "prompt", ()

    def _select(self, prepared, rejected):
        """The retained argmax among eligible, unrejected members (3.5)."""
        if rejected is not None:
            return select_retained(prepared.table, rejected)
        return prepared.table.scripted()

    def _note_selection(self, cand, context):
        """Compatibility wrapper: commit a chosen candidate's effect.

        Production no longer reaches here from :meth:`decide`; the controller
        commits the frozen effect at its reconciliation boundary through
        :meth:`commit_effect` (plan 3.1).  Kept for direct callers.
        """
        self.commit_effect(cand.proposed_effect, cand.semantic_label,
                           getattr(context, "tick", 0), context.memory)

    #: The non-command effect tags :meth:`commit_effect` applies from the
    #: frozen proposal rather than during preparation (plan 3.1).
    _NONCOMMAND_EFFECTS = ("selection-done", "eat-menu", "eat-forced-menu",
                           "refresh-inventory-menu", "pickup-menu",
                           "pickup-menu-cancel", "pickup-refuse")

    def commit_effect(self, effect, semantic_label, tick, mem,
                      observed_kind="", payload=()) -> None:
        """Commit one *selected, sent and reconciled* effect (plan 3.1).

        This is the single mutation point for reflex-local gameplay/recovery
        bookkeeping.  The controller calls it only after a complete send
        *and* the corresponding reconciled observation, so a local-invalid,
        write-failed, discarded or Jev-unselected candidate leaves every
        field here untouched.  A forced-search *nomination* and a prompt
        continuation commit nothing: the controller owns the dangerous prefix
        and only a completed suffix search consumes a search budget.

        ``effect`` is the candidate's frozen tag; a non-command decision may
        carry several joined by ``+`` (for example an inventory-cache refresh
        together with the eat-intent transition).  Such an effect set is
        applied from the frozen tags and ``payload`` here, never during
        preparation, and -- unlike a gameplay command -- it does not clear a
        pending intent except through its own ``eat-menu`` tag.
        """
        effect = effect or ""
        if effect in ("", "prompt", forced_search.FORCED_SEARCH_EFFECT):
            return
        tags = tuple(t for t in effect.split("+") if t)
        if tags and all(t in self._NONCOMMAND_EFFECTS for t in tags):
            self._commit_noncommand(tags, payload, mem)
            return
        if payload and payload[0] == "destfail":
            self._commit_destination_failure(payload, tick, mem)
            return
        if payload and payload[0] == "pickup":
            self._commit_pickup(payload, tick, mem)
            return
        if payload and payload[0] == "dest":
            self._commit_destination(payload, tick, mem)
        self.intent = ""
        no_time = (observed_kind == "no-time")
        if effect == "quit":
            self.quitting = True
            if self.quit_reason == "":
                if semantic_label == "trapped":
                    # the exhaustion fallback is the plan's graceful
                    # `policy-exhausted/trapped` quit, not a generic quit
                    self.quit_reason = forced_search.TRAPPED_QUIT_REASON
                else:
                    self.quit_reason = ("tick-cap" if tick
                                        >= self.max_ticks else "quit")
        elif effect == "schedule-eat":
            self.last_eat_tick = tick
            self.intent = "eat"
            self.eat_reject_base = sum(
                1 for m in mem.messages if "don't have that object" in m)
            self.eat_forced_menu = False
        elif effect in ("refresh-inventory", "refresh-inventory-periodic"):
            self.last_inv_tick = tick
        elif effect in ("secret-search", "site-search") and not no_time:
            # a completed search consumes its budget; a no-time outcome is a
            # refusal/no-progress (recorded by note_observation), never a
            # completed search (plan 5.1)
            if effect == "secret-search":
                mem.searches_since_progress += 1
            self.recovery.note_search_completed(self._search_site(mem.hero))

    def _commit_noncommand(self, tags, payload, mem) -> None:
        """Apply a frozen non-command effect set (plan 3.1).

        Ordering is deterministic and each tag is independent; an inventory
        refresh commits the exact rows, tick and game time frozen on the
        candidate, so a stale or discarded proposal can never refresh the
        cache.
        """
        for tag in tags:
            if tag == "selection-done":
                self.selection_done = True
            elif tag == "eat-menu":
                self.intent = ""
                self.eat_forced_menu = False
            elif tag == "eat-forced-menu":
                self.eat_forced_menu = True
            elif tag == "refresh-inventory-menu":
                rows, seen_tick, game_time = self._refresh_payload(payload)
                mem.inventory.refresh(rows, seen_tick, game_time)
            elif tag == "pickup-menu":
                self.intent = ""
            elif tag == "pickup-menu-cancel":
                # a deliberate cancellation terminates the site's acquisition
                self.intent = ""
                if mem.hero is not None:
                    self.floor.note_negative(self.instance_id, mem.hero,
                                             pickup.OUTCOME_CANCELED)
            elif tag == "pickup-refuse":
                # a declined capacity/burden prompt is a terminal refusal
                self.intent = ""
                self.pickup_purpose = ""
                self.pickup_pending = None
                if mem.hero is not None:
                    self.floor.note_negative(self.instance_id, mem.hero,
                                             pickup.OUTCOME_REFUSED)
                self._settle_pickup_target(pickup.OUTCOME_REFUSED, mem)

    def arm_pickup(self, payload, identity=None) -> bool:
        """Freeze a pickup attempt at the *send* boundary (plan 1.5/3.3).

        The evidence identity, purpose, generation and the **pre-send**
        inventory signature are captured here, before the observation that
        reports the result, so that observation is classified against a true
        pre-send baseline on its own reconciliation boundary.  The bounded
        initiation is counted here, and only here, for a sent attempt.

        *identity* is the **stable identity of the logical attempt** (the
        original accepted decision's send).  A delivery repair resends the
        frozen action for that same decision, so ``arm_pickup`` is called again
        with the *same* identity: the frozen attempt, its pre-send baseline and
        its counted initiation are preserved and nothing is counted or emitted
        twice.  A genuinely new accepted pickup decision carries a new identity
        and consumes the next attempt.  Returns whether a new attempt was
        armed.
        """
        if identity is not None and identity == self.pickup_attempt_identity:
            return False                # delivery repair of the same attempt
        if not payload or payload[0] != "pickup":
            return False
        (_tag, mode, iid, x, y, epoch) = payload[:6]
        pos = (int(x), int(y))
        ev = self.floor.evidence(pos)
        if ev is None or ev.source_epoch != int(epoch):
            return False                # stale evidence token
        if self.floor.declined(ev) or self.floor.negative(ev.instance, pos):
            return False
        if not self.floor.budget_available(ev):
            return False
        init_sig = payload[8] if len(payload) > 8 else None
        if init_sig is not None:
            init_sig = tuple(init_sig) if not isinstance(init_sig, tuple) \
                else init_sig
        self.floor.note_initiation(ev)
        self.pickup_pending = {
            "evidence": ev,
            "purpose": mode,
            "generation": (int(payload[6]) if len(payload) > 6
                           else int(self.directives.generation)),
            "init_inventory": init_sig,
        }
        self.pickup_attempt_identity = identity
        self.lifecycle.record(lifecycle_metrics.KIND_PICKUP,
                              lifecycle_metrics.PICKUP_ATTEMPTED,
                              token=ev.token, purpose=mode)
        self.intent = "pickup"
        self.pickup_purpose = mode
        self.pickup_evidence = ev
        self.pickup_init_inventory = init_sig
        return True

    def cancel_pickup(self) -> None:
        """Cancel a pending pickup freeze on a terminal non-repair invalid.

        The rejected attempt's action is excluded and the retry reselects, so
        the in-flight pickup freeze (and any pending intent) must not survive
        into the retry.
        """
        self.pickup_pending = None
        self.pickup_attempt_identity = None
        if self.intent == "pickup":
            self.intent = ""
            self.pickup_purpose = ""
        self.pickup_evidence = None
        self.pickup_init_inventory = None

    def _commit_pickup(self, payload, tick, mem) -> None:
        """Commit one selected, sent and reconciled pickup initiation (3.3).

        Applies only against the unchanged evidence token: a stale token, an
        already-declined token, an existing negative or an exhausted budget
        drops the effect.  The initiation is counted here, at the reconcile
        boundary, so preparation and selection spend no attempt.
        """
        if not payload or payload[0] != "pickup":
            return
        if self.pickup_pending is not None:
            return                      # already frozen at the send boundary
        (_tag, mode, iid, x, y, epoch) = payload[:6]
        pos = (int(x), int(y))
        ev = self.floor.evidence(pos)
        if ev is None or ev.source_epoch != int(epoch):
            return                      # stale evidence token
        if self.floor.declined(ev) or self.floor.negative(ev.instance, pos):
            return
        if not self.floor.budget_available(ev):
            return
        self.floor.note_initiation(ev)
        init_sig = payload[8] if len(payload) > 8 else None
        self.pickup_pending = {
            "evidence": ev, "purpose": mode,
            "generation": (int(payload[6]) if len(payload) > 6
                           else int(self.directives.generation)),
            "init_inventory": (None if init_sig is None else tuple(init_sig)),
        }
        self.intent = "pickup"
        self.pickup_purpose = mode
        self.pickup_evidence = ev
        self.pickup_init_inventory = (None if init_sig is None
                                      else tuple(init_sig))
        self.pickup_generation = self.pickup_pending["generation"]

    def _commit_destination(self, payload, tick, mem) -> None:
        """Apply one frozen destination effect (compare-and-apply, plan 1.4).

        Reached only through :meth:`commit_effect`, i.e. only after a complete
        send and its reconciled observation, so a discarded, unselected or
        write-failed proposal can never acquire, continue or retire a
        destination.  A fresh acquisition installs; a continuation applies
        only against the unchanged expected serial; an arrival services and
        retires exactly once.
        """
        if not payload or payload[0] != "dest":
            return
        if len(payload) >= 11:
            (_tag, op, iid, purpose, x, y, family, source, generation,
             expected, reason) = payload
        else:
            (_tag, op, iid, purpose, x, y, family, source, generation,
             expected) = payload
            reason = ""
        pos = (int(x), int(y))
        hero = mem.hero
        if op == "acquire":
            if hero is not None and tuple(hero) == pos:
                if purpose == navigation.COMMIT_COLLECT_ITEMS:
                    # the collection site is already under the hero: install
                    # the commitment in its interacting phase rather than
                    # servicing it, so the pickup outcome settles it (1.5/3.3)
                    self.targets.commit(
                        instance_id=iid or self.instance_id, purpose=purpose,
                        pos=pos, family=family, source=source,
                        generation=int(generation), tick=tick,
                        phase=navigation.PHASE_INTERACTING)
                    return
                # the observation that justified acquisition already satisfied
                # it: record reached instead of installing (plan 1.4)
                self._note_serviced(mem, pos)
                if source == navigation.SRC_DIRECTIVE:
                    self._settle_directive("reached", generation, reason)
                return
            cur = self.targets.held()
            if (cur is not None and cur.pos == pos
                    and cur.purpose == purpose and cur.instance_id == iid):
                return                      # idempotent: already committed
            replaced = cur is not None
            self.targets.commit(instance_id=iid or self.instance_id,
                                purpose=purpose, pos=pos, family=family,
                                source=source, generation=int(generation),
                                tick=tick)
            serial = self.targets.held().serial
            self.lifecycle.record(
                lifecycle_metrics.KIND_DESTINATION,
                lifecycle_metrics.DEST_REPLACED if replaced
                else lifecycle_metrics.DEST_ACQUIRED,
                serial=serial, purpose=purpose, source=source,
                generation=int(generation), reason=reason)
            return
        cur = self.targets.held()
        if cur is None or cur.serial != int(expected):
            return                          # stale continuation: drop it
        if op == "arrive" or (hero is not None and tuple(hero) == pos):
            if cur.purpose == navigation.COMMIT_COLLECT_ITEMS:
                # Arrival at a collection site *begins* the pickup phase: the
                # target is not settled here -- only a pickup terminal outcome
                # settles it and its directive generation (plan 1.5/3.3).
                self.targets.set_phase(navigation.PHASE_INTERACTING)
                return
            self._note_serviced(mem, pos)
            self.targets.retire("reached")
            if cur.source == navigation.SRC_DIRECTIVE:
                self._settle_directive("reached", cur.generation, reason)
            return
        # continue / interact
        self.targets.note_nav_attempt()
        self.lifecycle.record(lifecycle_metrics.KIND_DESTINATION,
                              lifecycle_metrics.DEST_ACTION,
                              serial=cur.serial, purpose=cur.purpose)
        if hero is not None:
            self.targets.note_progress(tuple(hero), tick)
            # A continuation chosen while a pickup was offered declines that
            # evidence token for the current visit, so it is not re-offered
            # every tick (plan 3.3).
            ev = self.floor.evidence(hero)
            if (ev is not None and not self.floor.declined(ev)
                    and not self.floor.negative(self.instance_id, hero)
                    and not self.directives.wants_flee_upstairs()
                    and self.floor.budget_available(ev)):
                self.floor.note_declined(ev)
        if cur.purpose == navigation.COMMIT_OPEN_DOOR:
            if hero is not None and recovery.chebyshev(hero, pos) <= 1:
                self.targets.note_interact_attempt()
            if navigation.door_open(self._terrain(mem), pos):
                self._note_serviced(mem, pos)
                self.targets.retire("door-opened")
                return
            sig = navigation.local_evidence_signature(self._terrain(mem),
                                                      cur.pos)
            if self.targets.door_attempts_exhausted:
                self.targets.retire("door-ineffective", pos=cur.pos,
                                    signature=sig)
                if cur.source == navigation.SRC_DIRECTIVE:
                    self._settle_directive("failed", cur.generation,
                                           "door-ineffective")
                return
        if self.targets.stalled():
            held = self.targets.held()
            if held is not None:
                self.targets.retire(
                    "stalled", pos=held.pos,
                    signature=navigation.local_evidence_signature(
                        self._terrain(mem), held.pos))
                if held.source == navigation.SRC_DIRECTIVE:
                    self._settle_directive("failed", held.generation,
                                           "stalled")

    def _commit_destination_failure(self, payload, tick, mem) -> None:
        """Settle an unresolved explicit destination (plan 1.5).

        The failure is a fold: the directive-owned destination is retired and
        suppressed under its evidence, and the settlement is queued for the
        controller's ``DirectiveBook.expire`` so the same generation is not
        reasserted every tick.
        """
        if not payload or payload[0] != "destfail":
            return
        (_tag, reason, generation) = payload
        held = self.targets.held()
        if held is not None:
            self.targets.retire(
                "unreachable" if reason == "unreachable"
                else "directive-unresolved", pos=held.pos,
                signature=navigation.local_evidence_signature(
                    self._terrain(mem), held.pos))
            if held.source == navigation.SRC_DIRECTIVE:
                self._settle_directive("failed", held.generation, reason)
        else:
            self._settle_directive("failed", generation, reason)

    @staticmethod
    def _refresh_payload(payload):
        """Unpack a frozen inventory-refresh payload into ``refresh`` args."""
        if not payload:
            return [], 0, None
        rows, seen_tick, game_time = payload
        return list(rows), int(seen_tick), game_time

    def _need_key(self, context):
        nk = getattr(context, "need_key", None)
        if nk is None:
            return ()
        return candidates.normalize_need_key(nk)

    def _features(self, context):
        """The immutable public-state subset this preparation binds to."""
        mem = context.memory
        st = mem.status
        hero = mem.hero
        conditions = (st.hunger,) if st.hunger else ()
        return candidates.ReflexFeatures(
            episode=getattr(context, "episode", 0) or 0,
            controller_tick=getattr(context, "tick", 0) or 0,
            need_key=self._need_key(context),
            level_instance_id=self.instance_id,
            displayed_level=st.dlvl or "",
            hero_confirmed=tuple(hero) if hero else (),
            hero_status=("confirmed" if hero else "unknown"),
            hp=st.hp, hp_max=st.hp_max, game_time=st.time,
            conditions=conditions,
            inventory_signature=mem.inventory_signature() or (),
            directive_generation=self.directives.generation,
            rejection_version=self.rejection_version)

    def fallback(self, context: ReflexContext) -> ReflexResult:
        return self.decide(context)

    # -- startup / selection menus --------------------------------------
    def _menu(self, context: ReflexContext):
        need = context.need or {}
        if self.intent == "quit" or self.quitting:
            return {"cancel": True}, "dismiss endgame display", "prompt", ()
        rows = [r for r in context.pages if r.get("selectable")]
        title = context.snapshot.window_title(need.get("content")) or ""
        refresh = self._inventory_refresh(context, context.pages, title)
        tags = [] if refresh is None else ["refresh-inventory-menu"]
        payload = refresh or ()

        if self.intent == "pickup":
            # The pickup intent locally filters the real menu: a uniquely
            # authorized exact row (urgent food) or a single bound row
            # (collect_items), else a conservative cancellation -- never a
            # model-chosen row (plan 3.3).
            mem = context.memory
            hero = mem.hero
            ev = self.floor.evidence(hero) if hero is not None else None
            urgent = bool(ev and pickup.urgent_food_fallback(
                hungry=self._hungry(mem.status),
                usable_cached_food=bool(mem.inventory.food_rows()),
                exact_ration_name=ev.ration_name))
            decision, row, why = pickup.menu_decision(
                pickup.parse_rows(context.pages),
                purpose=self.pickup_purpose or "opportunistic",
                urgent_food=urgent)
            if decision == "select" and row is not None:
                return ({"menu": need.get("menu"), "commit": [[row.index, -1]]},
                        why, "pickup-menu", ())
            return {"cancel": True}, why, "pickup-menu-cancel", ()

        if self.intent == "eat":
            # the eat-intent transition is a frozen effect, not a mutation
            tags = tags + ["eat-menu"]
            food = [r for r in rows
                    if state.is_known_safe_food(r.get("text"))]
            if food:
                return ({"menu": need.get("menu"),
                         "commit": [[food[0]["r"], -1]]},
                        "eat a safe food row", _join_effects(tags), payload)
            return ({"cancel": True}, "no known-safe food row: cancel",
                    _join_effects(tags), payload)

        kind = menu_kind(title)
        if not kind:
            # an unmatched menu is never blindly confirmed
            return ({"cancel": True}, "unrecognised menu title: cancel",
                    _join_effects(tags), payload)
        if kind in ("tutorial", "ok"):
            tags = tags + ["selection-done"]
        pick = self._preferred_row(rows, kind)
        if pick is None:
            return ({"cancel": True},
                    "no matching row in a %s menu: cancel" % kind,
                    _join_effects(tags), payload)
        return ({"menu": need.get("menu"),
                 "commit": [[pick["r"], -1]]},
                "select the %s row" % kind, _join_effects(tags), payload)

    def _preferred_row(self, rows: List[dict], kind: str) -> Optional[dict]:
        wants: List[str] = []
        if kind == "tutorial":
            wants = ["no, just start play", "no"]
        elif kind == "ok":
            wants = ["yes; start game", "yes"]
        elif kind == "role":
            wants = [self.config.role.lower()]
        elif kind == "race":
            wants = ["human"]
        elif kind == "alignment":
            wants = ["lawful"]
        for want in wants:
            for r in rows:
                if want and want in (r.get("text") or "").lower():
                    return r
        return None

    # -- inventory cache maintenance ------------------------------------
    def _inventory_refresh(self, context: ReflexContext, rows, title):
        """The frozen inventory-refresh payload, or ``None`` (plan 3.1).

        Pure: the observed rows, the controller tick and the displayed game
        time are returned as a frozen payload and applied by
        :meth:`commit_effect` only after a complete send and its reconciled
        observation.  Preparation never mutates the cache.
        """
        if not title or "inventory" not in title.lower():
            return None
        return (tuple(rows), int(context.tick),
                context.snapshot.time_value())

    # -- yes/no (including the unrestricted getobj prompt) --------------
    def _yn(self, context: ReflexContext):
        need = context.need or {}
        prompt = need.get("prompt") or ""
        low = prompt.lower()
        letters, has_star, _has_q = bracket_info(prompt)

        if "shall i pick" in low:
            return {"yn": KEY.KEY_N}, "decline auto-pick", "prompt", ()
        if pickup.is_capacity_prompt(prompt):
            # a capacity/burden question is always declined (plan 3.5); under an
            # active pickup intent the decline is a terminal refusal, not a
            # generic prompt continuation
            if self.intent == "pickup":
                return ({"yn": KEY.KEY_N}, "decline capacity prompt",
                        "pickup-refuse", ())
            return {"yn": KEY.KEY_N}, "decline capacity prompt", "prompt", ()
        if "really quit" in low or "quit without saving" in low:
            return {"yn": KEY.KEY_Y}, "confirm quit", "prompt", ()
        if "save" in low and "really" in low:
            return {"yn": KEY.KEY_Y}, "confirm save", "prompt", ()

        if self.intent == "eat" and "eat" in low:
            action, reason, effect = self._eat_answer(context, letters,
                                                      has_star)
            return action, reason, effect, ()

        if need.get("default") is not None:
            return ({"yn": int(need["default"])}, "native default",
                    "prompt", ())
        choices = need.get("choices")
        if choices:
            key = KEY.KEY_N if "n" in choices else ord(choices[0])
            return {"yn": key}, "visible choice", "prompt", ()
        if "y" in letters and "n" in letters:
            return {"yn": KEY.KEY_N}, "decline yes/no", "prompt", ()
        if letters:
            return {"yn": ord(letters[0])}, "bracketed letter", "prompt", ()
        return {"yn": KEY.KEY_N}, "safe decline", "prompt", ()

    def _eat_answer(self, context: ReflexContext, letters: List[str],
                    has_star: bool):
        """A pure getobj answer: ``(action, reason, effect)`` (plan 3.1)."""
        rejected = sum(1 for m in context.memory.messages
                       if "don't have that object" in m) \
            - self.eat_reject_base
        if rejected >= 2 or self.eat_forced_menu:
            # the forced-menu transition is a frozen effect: applied only
            # once this answer is sent and its result observed
            return ({"yn": ord("*")},
                    "eat loop breaker: open inventory menu",
                    "eat-forced-menu")
        # only answer a letter the cached inventory already confirmed is safe
        known = {l.lower() for l in context.memory.inventory.food_letters()}
        safe = [c for c in letters if c.lower() in known]
        if safe:
            return ({"yn": ord(safe[0])},
                    "eat a known-safe cached food letter", "prompt")
        if has_star:
            return {"yn": ord("*")}, "open the food menu", "prompt"
        return ({"yn": KEY.KEY_ESC},
                "no known-safe food answer: cancel eat", "prompt")

    # -- gameplay commands: candidate generation ------------------------
    def _command_candidates(self, context: ReflexContext):
        """Every command candidate, in the documented precedence order.

        Safety emergencies, mandatory continuations and maintenance actions
        take precedence through *priority* (they become the sole candidate),
        not through an unbounded score bonus (3.3).  Only when none applies
        does navigation generate a scored multi-candidate set.
        """
        mem = context.memory
        st = mem.status
        hero = mem.hero
        if self.quitting or context.tick >= self.max_ticks:
            why = ("tick cap: request quit" if context.tick >= self.max_ticks
                   else "quit")
            return (self._cand({"key": KEY.KEY_HASH}, "quit", "other", 0,
                               why, "quit"),)
        # 1. low-HP disengagement: escape before any other action
        if hero is not None and self._low_hp(st):
            action, why = self._escape(mem, hero)
            return (self._cand(action, "escape", "emergency", 0, why,
                               "emergency"),)
        # 2. hunger: schedule a known-safe food intent, unless scoped
        #    evidence already shows there is nothing to eat here
        if self._hungry(st) and \
                (context.tick - self.last_eat_tick) > self._eat_interval() \
                and self._may_eat(mem, hero):
            return (self._cand({"key": KEY.KEY_EAT}, "eat", "food", 0,
                               "hungry: attempt to eat", "schedule-eat"),)
        if self.directives.wants_food() and hero is not None \
                and not mem.inventory.food_rows() \
                and mem.inventory.stale(context.tick, INV_STALE_TICKS) \
                and (context.tick - self.last_inv_tick) \
                > INV_REFRESH_COOLDOWN:
            reason = ("directive %s: inspect inventory for food"
                      % self.directives.top_goal())
            return (self._cand({"key": KEY.KEY_INV}, "inspect-inventory",
                               "inventory", 0, reason,
                               "refresh-inventory"),)
        if hero is None:
            # The hero's square is unknown, so every direction leads into
            # unknown space and adjacency cannot be evaluated: never move
            # blind, hold the turn with a search instead.
            return (self._cand({"key": KEY.KEY_SEARCH}, "search-in-place",
                               "recovery", 0,
                               "no hero fix: search in place", "recovery"),)
        # 3. loop breakers: progress without ever walking into a monster
        np = mem.no_progress
        if np >= 10:
            action = self._unblock(mem, hero, st)
            return (self._cand(action, "unblock", "recovery", 0,
                               "loop breaker: unblock", "recovery"),)
        if np >= 6:
            key, why = self._random_move(mem, hero)
            return (self._cand({"key": key}, "random-move", "recovery", 0,
                               "loop breaker: %s" % why, "recovery"),)
        if np >= 3:
            if self._allows_search(mem, hero) and not self._cycled:
                return (self._cand({"key": KEY.KEY_SEARCH}, "search",
                                   "recovery", 0, "loop breaker: search",
                                   "site-search"),)
            # a refused search at this site is suppressed (5.1): fall through
            # to navigation / a non-search recovery step, never another `s`
            return self._navigation_candidates(context, mem, hero)
        # 3b. an active detected movement cycle (period-2 or period-3) enters
        #     recovery even with no stationary no_progress: `_cycled` is an
        #     independent condition, not merely a search suppressor
        if self._cycled:
            return self._cycle_candidate(mem, hero)
        # 4. inventory cache maintenance (never preempts safety or progress)
        if mem.inventory.stale(context.tick, INV_STALE_TICKS) \
                and (context.tick - self.last_inv_tick) \
                > INV_REFRESH_COOLDOWN:
            return (self._cand({"key": KEY.KEY_INV}, "refresh-inventory",
                               "inventory", 0,
                               "refresh the inventory cache",
                               "refresh-inventory-periodic"),)
        return self._navigation_candidates(context, mem, hero)

    @staticmethod
    def _cand(action, label, family, score, reason, effect,
              direction=(), direction_rank=0, effect_payload=()):
        """Build one content-addressed candidate with its semantic label."""
        return candidates.make_candidate(
            action, label, family=family, score=score, reason=reason,
            proposed_effect=effect, direction=direction,
            direction_rank=direction_rank, effect_payload=effect_payload)

    def _navigation_candidates(self, context, mem, hero):
        """The commitment pipeline: resolve → route → build (plan 1.3).

        Each command boundary recomputes at most one navigation Dijkstra.  A
        *held* destination is routed, not re-elected: only its next step is
        offered, so a newly higher-scoring target, a frontier reclassification
        or a visit-penalty change cannot cancel progress.  With no held
        destination the reflex elects one from the default pool (doors and
        frontiers before down-stairs, unvisited cells as fallback) and offers
        the retained scored multi-candidate set so the controller (or Jev)
        selects one; the elected destination is committed only at the
        reconcile boundary, never here.
        """
        explore_first = self.directives.prefers_frontier() \
            and not self.directives.prefers_stairs()
        prefer_stairs = self.directives.prefers_stairs()
        if hero in mem.stairs_down and not explore_first:
            return (self._cand({"key": ord(">")}, "descend", "descend", 900,
                               "descend the known stairs", "descend"),)
        terrain = self._terrain(mem, context)
        plan = navigation.plan(terrain, hero, mem.visits, None,
                               self._check_deadline)
        held = self.targets.held()
        # A newly activated explicit destination replaces the old one at the
        # next genuine command decision (plan 1.5); the superseded default
        # commitment is not routed.
        superseded = (held is not None
                      and held.generation != self.directives.generation
                      and self._directive_bears_destination())
        if held is not None and not superseded \
                and self.targets.holds(self.instance_id, terrain, hero):
            routed = self._route_committed(held, terrain, hero, plan)
            if routed is not None:
                return self._with_pickup((routed,), mem, hero)
            # the held route became unreachable: emit a frozen failure
            # operation settled at reconciliation rather than silently
            # selecting around the active record (plan 1.5)
            return (self._unreachable_destination_candidate(held),)
        cands = self._acquisition_candidates(context, mem, hero, plan,
                                             prefer_stairs, explore_first)
        if cands:
            return self._with_pickup(tuple(cands), mem, hero)
        if self.on_square_collect:
            # an active collect destination already under the hero: the
            # command decision is the pickup initiation (plan 1.5/3.3)
            alt = self._pickup_alternative(mem, hero)
            if alt is not None:
                return (alt,)
            self.last_unresolved = "collection site under the hero"
            return (self._unresolved_destination_candidate(),)
        if self.last_unresolved:
            # An unresolved explicit destination is a structured failure: it is
            # never silently replaced by default exploration (plan 1.5).
            return (self._unresolved_destination_candidate(),)
        if mem.searches_since_progress < 3 \
                and self._allows_search(mem, hero) \
                and not self._cycled:
            return (self._cand({"key": KEY.KEY_SEARCH}, "search-secret",
                               "secret-search", 300,
                               "search for secret doors",
                               "secret-search"),)
        return self._search_fallback(mem, hero)

    def _route_committed(self, held, terrain, hero, plan):
        """The held destination's single next step, or ``None`` (plan 1.3)."""
        step, terminal, reason = navigation.route_held_destination(
            held, terrain, hero, plan.dist, plan.first)
        if step is not None:
            payload = self._dest_payload("continue", held)
            return self._cand({"key": KEY.DIR_KEYS[step]}, "navigate",
                              _NAV_FAMILY[held.family], 0, reason, "navigate",
                              direction=step,
                              direction_rank=navigation.DIR_RANK[step],
                              effect_payload=payload)
        if terminal == "arrive":
            payload = self._dest_payload("arrive", held)
            return self._cand({"key": KEY.KEY_SEARCH}, "navigate",
                              _NAV_FAMILY[held.family], 0, reason, "navigate",
                              effect_payload=payload)
        return None

    def _acquisition_candidates(self, context, mem, hero, plan, prefer_stairs,
                                explore_first):
        """The default/directive destination pool, scored (plan 1.2).

        An explicit, coordinate-bearing v2 destination is resolved by the
        dedicated semantic resolver *before* any acquisition: it is never
        re-resolved against generic exploration enumeration, and an unresolved
        explicit destination yields no candidates (the caller emits a
        structured failure instead of silently exploring).
        """
        targets = plan.targets
        terrain = self._terrain(mem, context)
        directive_purpose = None
        self.last_unresolved = ""
        self.on_square_collect = False
        if self._directive_bears_destination() \
                and (self.directives.wants_collect()
                     or self.directives.wants_flee_upstairs()):
            target, directive_purpose, reason = \
                self._resolve_semantic_destination(context, mem, hero, plan)
            if target is None:
                self.last_unresolved = reason
                return []
            if tuple(target.pos) == tuple(hero):
                # the collection site is under the hero: the acquired action is
                # the pickup initiation, not a movement step
                self.on_square_collect = True
                return []
            pool = [target]
        elif self.directives.target is not None:
            # a coordinate-bearing goal we do not resolve semantically
            # (e.g. descend_known_stairs) keeps its exact-coordinate pool
            pool = [t for t in targets
                    if tuple(t.pos) == tuple(self.directives.target)]
        else:
            def ok(t):
                sig = navigation.local_evidence_signature(terrain, t.pos)
                return (not self.targets.serviced_under_evidence(t.pos, sig)
                        and not self.targets.failed_under_evidence(t.pos, sig))

            # The default destination pool (plan 1.2, AC2): reachable doors and
            # frontiers are committed before down-stairs; unvisited known cells
            # are the fallback; stairs compete only under explicit stair advice
            # (``descend_known_stairs``) or when nothing else is available.
            stair = [t for t in targets
                     if t.family == navigation.TFAM_STAIR and ok(t)]
            explore = [t for t in targets
                       if t.family in (navigation.TFAM_DOOR,
                                       navigation.TFAM_FRONTIER) and ok(t)]
            unvisited = [t for t in targets
                         if t.family == navigation.TFAM_UNVISITED and ok(t)]
            if prefer_stairs and stair:
                pool = stair
            else:
                pool = explore or unvisited or stair
        if not pool:
            return []
        scored = []
        for target in pool:
            key = KEY.DIR_KEYS[target.first_step]
            score = _target_score(target.family, target.cost, explore_first)
            score += self._directive_component(target)
            scored.append((target, _NAV_FAMILY[target.family], key, score,
                           target.first_step))
        kept = self._antibacktrack(scored, hero,
                                   getattr(context, "rejected", None))
        cands = []
        directive_pool = (directive_purpose is not None
                          or (self.directives.active
                              and self.directives.target is not None))
        for target, family, key, score, step, extra in kept:
            payload = self._dest_payload(
                "acquire", None, target=target, purpose=directive_purpose,
                source=(navigation.SRC_DIRECTIVE
                        if directive_pool
                        else navigation.SRC_DEFAULT),
                generation=self.directives.generation,
                reason=target.reason)
            cands.append(self._cand(
                {"key": key}, "navigate", family, score,
                "%s: %s" % (self._nav_reason(extra), target.reason), "navigate",
                direction=step, direction_rank=navigation.DIR_RANK[step],
                effect_payload=payload))
        return cands

    def _resolve_semantic_destination(self, context, mem, hero, plan):
        """Resolve a coordinate-bearing v2 destination goal (plan 2.1, item 1).

        Returns ``(Target|None, purpose|None, reason)``.  ``target`` is ``None``
        for a structured failure; the caller then emits a frozen failure
        operation instead of falling through to default exploration.
        """
        terrain = self._terrain(mem, context)
        target = self.directives.target
        if self.directives.wants_flee_upstairs():
            known = set(mem.stairs_up)
            t, why = navigation.resolve_semantic_destination(
                terrain, hero, plan.dist, plan.first,
                purpose=navigation.COMMIT_FLEE_UPSTAIRS, target=target,
                upstairs=known)
            return t, navigation.COMMIT_FLEE_UPSTAIRS, why
        if self.directives.wants_collect():
            t, why = navigation.resolve_semantic_destination(
                terrain, hero, plan.dist, plan.first,
                purpose=navigation.COMMIT_COLLECT_ITEMS, target=target,
                evidence_positions=self.floor.evidence_positions())
            return t, navigation.COMMIT_COLLECT_ITEMS, why
        return None, None, "no coordinate-bearing destination goal"

    def _unresolved_destination_candidate(self):
        """A frozen structured failure for an unresolved explicit destination.

        The directive-owned destination is neither routed nor replaced by
        default exploration: the turn is held with a search-in-place while the
        failure effect retires/suppresses the destination and settles its
        generation at the reconcile boundary (plan 1.5).
        """
        payload = ("destfail", self.last_unresolved,
                   int(self.directives.generation))
        return self._cand({"key": KEY.KEY_SEARCH}, "unresolved-destination",
                          "recovery", 0,
                          "explicit destination unresolved: %s"
                          % self.last_unresolved, "dest-unresolved",
                          effect_payload=payload)

    def _settle_directive(self, outcome, generation, reason) -> None:
        """Queue a directive-owned destination settlement (plan 1.5)."""
        self.directive_settlement = (outcome, int(generation), str(reason))
        self.lifecycle.record(
            lifecycle_metrics.KIND_DIRECTIVE,
            lifecycle_metrics.DIR_TERMINAL, generation=int(generation),
            outcome_detail=outcome, reason=reason)
        held = self.targets.held()
        self.lifecycle.record(
            lifecycle_metrics.KIND_DESTINATION,
            lifecycle_metrics.DEST_REACHED if outcome == "reached"
            else lifecycle_metrics.DEST_FAILED,
            serial=(held.serial if held is not None else None),
            reason=reason)

    @staticmethod
    def _dest_payload(op, held, target=None, purpose=None, source=None,
                      generation=None, reason=None):
        """The frozen destination effect payload (plan 1.4).
        Binds the operation (``acquire``/``continue``/``arrive``), the level
        instance, the semantic destination coordinate and family, the source
        and originating generation, and -- for a continuation -- the expected
        commitment serial, so the reconcile fold can compare-and-apply.  A
        fresh acquisition carries ``-1`` (no expected serial).
        """
        if held is not None:
            iid, purpose = held.instance_id, held.purpose
            pos, family = held.pos, held.family
            source, generation, expected = (held.source, held.generation,
                                            held.serial)
        else:
            iid = 0
            purpose = purpose or navigation._PURPOSE_BY_FAMILY.get(
                target.family, navigation.COMMIT_EXPLORE_FRONTIER)
            pos, family = tuple(target.pos), target.family
            source = source or navigation.SRC_DEFAULT
            generation = 0 if generation is None else int(generation)
            expected = -1
        return ("dest", op, int(iid), purpose, int(pos[0]), int(pos[1]),
                family, source, int(generation), int(expected),
                str(reason or ""))

    def _directive_bears_destination(self) -> bool:
        """True when the active advice names or selects a destination (1.5)."""
        if not self.directives.active:
            return False
        if self.directives.target is not None:
            return True
        return bool(self.directives.wants_collect()
                    or self.directives.wants_flee_upstairs()
                    or self.directives.prefers_stairs()
                    or self.directives.destination_selecting())

    def _with_pickup(self, base, mem, hero):
        """Append the opportunistic pickup alternative at a supported site.

        The held-route continuation (or the acquisition set) is never dropped
        or reordered: the pickup candidate is only appended, so the Choice
        criterion order and N are decided entirely here (plan 3.3).
        """
        alt = self._pickup_alternative(mem, hero)
        if alt is None:
            return base
        return tuple(base) + (alt,)

    def _pickup_alternative(self, mem, hero):
        """The priority-bound pickup candidate, or ``None`` (plan 3.1/3.3).

        Offered only when the hero stands on a supported item site (a floor
        evidence token exists) with budget remaining and no negative.  An
        explicit ``flee_to_upstairs`` suppresses opportunistic pickup
        entirely.  The score keeps the scripted fallback on the committed
        route unless an explicit ``collect_items`` directive or the narrow
        urgent-food rule authorizes a reflex pickup; Jev may still choose
        ``pick-up`` over the continuation among the offered criteria.
        """
        if hero is None:
            return None
        if self.directives.wants_flee_upstairs():
            return None
        instance = self.instance_id
        ev = self.floor.evidence(hero)
        if ev is None or self.floor.declined(ev) \
                or self.floor.negative(instance, hero) \
                or not self.floor.budget_available(ev):
            return None
        collect = self.directives.wants_collect() and (
            self.directives.target is None
            or tuple(self.directives.target) == tuple(hero))
        urgent = pickup.urgent_food_fallback(
            hungry=self._hungry(mem.status),
            usable_cached_food=bool(mem.inventory.food_rows()),
            exact_ration_name=ev.ration_name)
        if collect:
            mode, score = "collect", 900
        elif urgent:
            mode, score = "urgent", 900
        else:
            mode, score = "opportunistic", -1
        payload = ("pickup", mode, int(ev.instance), int(ev.pos[0]),
                   int(ev.pos[1]), int(ev.source_epoch),
                   int(self.directives.generation), str(ev.appearance),
                   mem.inventory_signature())
        reason = "pick up the items here (%s)" % mode
        self.lifecycle.record(lifecycle_metrics.KIND_PICKUP,
                              lifecycle_metrics.PICKUP_OFFERED,
                              token=ev.token, purpose=mode)
        return self._cand({"key": KEY.KEY_PICKUP}, "pick-up", "pickup", score,
                          reason, "pickup", effect_payload=payload)

    # The bounded same-family margin: a reversal is kept only when it scores
    # strictly more than 40 above the best non-reversing alternative in its
    # family (the +30 directive contribution participates in both scores).
    ANTIBACKTRACK_MARGIN = 40

    @staticmethod
    def _is_reverse(step, hero, previous) -> bool:
        """True when *step* returns to the previous distinct confirmed cell."""
        if previous is None:
            return False
        return (hero[0] + step[0], hero[1] + step[1]) == tuple(previous)

    def _antibacktrack(self, scored, hero, rejected):
        """Bounded, same-family preference against an immediate reversal.

        A pure operation over the *scored target/action representatives*,
        applied while target metadata is still available and before candidate
        construction.  Entries are grouped by ``(candidate family, canonical
        first-step action signature)`` and each group keeps the deterministic
        best representative dedup would keep.  Rejected action signatures are
        removed from consideration first, so a rejected alternative can never
        suppress the only usable retreat.  Within a family, a reversing
        representative is suppressed unless no non-reversing alternative
        exists, or the reversal scores strictly more than
        :data:`ANTIBACKTRACK_MARGIN` above the family's best non-reversing
        representative.  Directives never cross a family boundary: the
        comparison is made within a family.
        """
        previous = self.recovery.previous_distinct

        def sig_of(key):
            return candidates.ImmutableAction.key(key).signature()

        groups = {}
        for entry in scored:
            target, family, key, score, step = entry
            gkey = (family, sig_of(key))
            best = groups.get(gkey)
            # deterministic best representative: highest score (a same-key,
            # same-family tie has an identical candidate identity)
            if best is None or score > best[3]:
                groups[gkey] = entry
        reps = list(groups.items())
        # best non-reversing representative score per family, excluding any
        # already-rejected action signature
        best_alt = {}
        for (family, sig), entry in reps:
            if rejected is not None and rejected.excludes_signature(sig):
                continue
            if not self._is_reverse(entry[4], hero, previous):
                best_alt[family] = max(best_alt.get(family, entry[3]), entry[3])
        suppressed = set()
        for (family, sig), entry in reps:
            if not self._is_reverse(entry[4], hero, previous):
                continue
            if rejected is not None and rejected.excludes_signature(sig):
                continue
            alt = best_alt.get(family)
            if alt is None:
                continue        # no comparable alternative: keep the reversal
            if not (entry[3] - alt > self.ANTIBACKTRACK_MARGIN):
                suppressed.add((family, sig))
        kept = []
        for (family, sig), entry in reps:
            if (family, sig) in suppressed:
                continue
            target, fam, key, score, step = entry
            extra = ""
            if any(f == fam for f, _ in suppressed) \
                    and not self._is_reverse(step, hero, previous):
                extra = "avoiding an immediate backtrack"
            kept.append((target, fam, key, score, step, extra))
        kept.sort(key=lambda e: e[0].order_key())
        return kept

    def _cycle_candidate(self, mem, hero):
        """A singleton, edge-legal escape from an active movement cycle.

        Enumerates known-safe neighbours with the same terrain and
        :func:`navigation.edge_legal` checks planning uses (so no unsafe
        diagonal or door entry leaks in), excludes monster/unknown
        destinations, and prefers a non-reversing exit by a deterministic
        visit-count/direction-rank order.  A traversable dead end whose only
        legal escape is backtracking keeps that reversal -- it is never
        misreported as trapped merely because of the preference.  With no legal
        movement at all it reuses the existing bounded search-fallback /
        forced-search nomination machinery rather than manufacturing an
        unbudgeted search, a dangerous prefix or an indefinite wait.  Returns a
        one-member candidate tuple, as the loop-breaker branches do.
        """
        terrain = self._terrain(mem)
        previous = self.recovery.previous_distinct
        options = []
        for step in navigation.DIRECTIONS:
            dest = (hero[0] + step[0], hero[1] + step[1])
            if dest == hero:
                continue
            if not navigation.edge_legal(terrain, hero, dest):
                continue
            if state.monster_cell(mem.tile(dest), hero, dest):
                continue
            reversing = self._is_reverse(step, hero, previous)
            options.append((0 if not reversing else 1,
                            mem.visits.get(dest, 0),
                            navigation.DIR_RANK[step], step))
        if options:
            options.sort()
            step = options[0][3]
            return (self._cand(
                {"key": KEY.DIR_KEYS[step]}, "recovery-step", "recovery", 0,
                "cycle recovery: leave the repeating movement", "recovery",
                direction=step, direction_rank=navigation.DIR_RANK[step]),)
        return self._search_fallback(mem, hero)

    def _search_site(self, hero):
        """The deterministic site key for the ordinary-search budget."""
        return tuple(hero) if hero is not None else None

    def _search_fallback(self, mem, hero):
        """A non-search recovery step, or a graceful quit when none exists.

        Command ``s`` is never an exhaustion fallback (3.5): once the site's
        ordinary search is suppressed, recovery uses a deterministic safe
        alternative step, and when even that is unavailable the reflex
        nominates the single caller-approved dangerous exception (plan 5.3)
        if its reflex-local gates hold, and otherwise requests a bounded
        graceful quit instead of looping.  The nomination is only a
        *proposal*: the controller owns every controller-side gate and the
        actual two-send transaction (5.4).
        """
        key, why = self._random_move(mem, hero)
        if key != KEY.KEY_SEARCH:
            return (self._cand({"key": key}, "recovery-step", "recovery", 0,
                               "recovery: %s" % why, "recovery"),)
        template = self._forced_template(mem, hero)
        if forced_search.local_ok(
                forced_search.evaluate_activation_gates(template)):
            self.forced_template = template
            return (self._cand(
                {"key": forced_search.FORCED_SEARCH_PREFIX_CODE},
                "forced-search", "recovery", 0,
                "trapped: nominate the dangerous forced search",
                forced_search.FORCED_SEARCH_EFFECT),)
        self.forced_template = None
        return (self._cand({"key": KEY.KEY_HASH}, "trapped", "other", 0,
                           "search suppressed and no safe alternative: "
                           "request quit", "quit"),)

    def _forced_template(self, mem, hero):
        """The reflex-local activation evidence (gates 1-5) of a nomination.

        Every gate the reflex alone can judge is filled from public evidence;
        the controller-owned gates (transport, cap, binding) are left
        fail-closed so a caller cannot mistake the template for a full
        activation report.  :func:`forced_search.merge_controller_fields`
        overlays them before the controller evaluates the gates.
        """
        st = mem.status
        kind = (self._pending_kind or "")
        commandish = kind in ("command", "key", "direction")
        refusal = recovery.refusal_in(mem.recent_messages(6))
        return forced_search.ForcedSearchContext(
            hero_confirmed=hero is not None,
            command_need_coherent=commandish,
            instance_resolved=bool(self.instance_id),
            transition_pending=False,
            hp=st.hp, hp_max=st.hp_max,
            hunger=st.hunger,
            conditions=self._conditions,
            conditions_complete=True,
            refusal_kind=refusal or "",
            alternatives_exhausted=True,
            no_pending_intent=False,
            transport_healthy=False,
            prefix_contract_verified=False,
            activations_used=0,
            bound_suffix_need=(),
            following_need=(),
            planned_suffix=forced_search.FORCED_SEARCH_SUFFIX,
        )

    def forced_search_local(self):
        """The most recent nomination's reflex-local template, or ``None``."""
        return self.forced_template

    def _observe(self, mem, hero):
        """Fold public messages into the bounded-recovery scoped evidence.

        Retained as the compatibility entry point; production folds once per
        *committed observation* through :meth:`note_observation` at the
        controller's reconciliation boundary, never during candidate
        construction (plan 3.1)."""
        self.note_observation(mem)

    def note_observation(self, mem) -> None:
        """Fold one *committed* observation into the scoped recovery evidence.

        Called exactly once per applied snapshot by the controller, after the
        observation has been reconciled and committed.  Candidate construction
        and proposal never reach here, so a discarded proposal, an invalid or
        write-failed candidate or a Jev-unselected member cannot advance the
        cycle/refusal/food state (plan 3.1).
        """
        hero = mem.hero
        recent = mem.recent_messages(6)
        self.recovery.observe(recent, hero, self._search_site(hero))
        self._cycled = self.recovery.note_cycle(hero)
        if self._cycled:
            # Cycle recovery invalidates the active destination before the
            # existing edge-legal recovery singleton runs, so a stale
            # destination cannot re-drive the loop (plan 1.5 "Cycle").
            held = self.targets.held()
            sig = (navigation.local_evidence_signature(self._terrain(mem),
                                                       held.pos)
                   if held is not None else None)
            self.targets.invalidate_cycle(sig)
        self._fold_floor(mem)
        self._fold_pickup_outcome(mem)
        self._fold_door_outcome(mem)
        instance = getattr(mem, "instance", None)
        if instance is None:
            instance = self.instance_id
        for text in recent:
            kind = recovery.classify_food_negative(text)
            if kind == recovery.FOOD_NEG_INVENTORY:
                self.food.note_inventory_negative(mem.inventory_signature())
            elif kind == recovery.FOOD_NEG_LOCATION and hero is not None:
                self.food.note_location_negative(instance or 0, hero, 0)

    def _fold_floor(self, mem) -> None:
        """Fold one committed observation into the floor-item ledger (3.2).

        A displayed item appearance at a non-hero cell creates (or materially
        refreshes) a source-epoch token.  When the confirmed hero stands on a
        square whose ledger record already exists, that record is retained as
        *last-seen* evidence with its **unchanged** epoch -- the hero overlay
        hides the glyph but must not fabricate a new token (and so must not
        reset the bounded attempt budget).
        """
        instance = getattr(mem, "instance", None)
        if instance is None:
            instance = self.instance_id
        hero = mem.hero
        for pos, cell in mem.grid.items():
            if not cell:
                continue
            ch = cell[0]
            if not ch or ch in (" ", "@"):
                continue
            color = cell[1] if len(cell) > 1 else ""
            style = cell[2] if len(cell) > 2 else ""
            other = cell[3] if len(cell) > 3 else ""
            app = instances.display_appearance(ch, color, style, other, pos,
                                               hero)
            if app.kind == instances.APP_ITEM:
                self.floor.observe_item(instance, pos, app.category)
        if hero is not None and self.floor.evidence(hero) is not None:
            self.floor.retain_on_arrival(instance, hero)
            # A location-bound floor message names an item at the hero's *own*
            # reconciled square: bind it here so the narrow urgent-food rule
            # can consult an exact recognized ration name (plan 3.1).
            for text in mem.recent_messages(6):
                name = pickup.ration_name_in(text)
                if name:
                    self.floor.bind_ration_name(instance, tuple(hero), name,
                                                tuple(hero))
                    break

    def _note_serviced(self, mem, pos) -> None:
        """Service a site under its current *local evidence* signature."""
        self.targets.note_serviced(
            tuple(pos),
            navigation.local_evidence_signature(self._terrain(mem), tuple(pos)))

    def _unreachable_destination_candidate(self, held):
        """A frozen failure for a held destination with no route (plan 1.5)."""
        payload = ("destfail", "unreachable", int(held.generation))
        return self._cand({"key": KEY.KEY_SEARCH}, "unresolved-destination",
                          "recovery", 0,
                          "the held destination is unreachable",
                          "dest-unresolved", effect_payload=payload)

    def _fold_door_outcome(self, mem) -> None:
        """Retire a held door commitment on an explicit refusal (plan 1.5).

        Player-visible evidence only: a locked/refused door line fails the
        commitment immediately instead of the agent repeatedly trying it.
        """
        held = self.targets.held()
        if held is None or held.purpose != navigation.COMMIT_OPEN_DOOR:
            return
        joined = " ".join(t.lower() for t in mem.recent_messages(6))
        if any(k in joined for k in ("is locked", "it's locked", "locked door",
                                     "resists", "you cannot open")):
            self.targets.retire(
                "locked-door", pos=held.pos,
                signature=navigation.local_evidence_signature(
                    self._terrain(mem), held.pos))
            if held.source == navigation.SRC_DIRECTIVE:
                self._settle_directive("failed", held.generation,
                                       "locked-door")

    def _fold_pickup_outcome(self, mem) -> None:
        """Classify one reconciled pickup attempt into an outcome (plan 3.3).

        The attempt was frozen at the *send* boundary, so this observation --
        the one that reports the result -- is classified against the pre-send
        inventory baseline and the frozen evidence token.  An ``unknown``
        classification consumes the bounded attempt but **preserves** the
        interaction phase and the collection target until a terminal outcome
        or exhaustion; only a terminal outcome settles the target and its
        directive generation.
        """
        attempt = self.pickup_pending
        if attempt is None:
            return
        ev = attempt.get("evidence")
        init_sig = attempt.get("init_inventory")
        joined = " ".join(t.lower() for t in mem.recent_messages(6))
        sig = mem.inventory_signature()
        if (sig is not None and init_sig is not None
                and tuple(sig) != tuple(init_sig)):
            outcome = pickup.OUTCOME_SUCCESS
        elif "nothing here to pick up" in joined:
            outcome = pickup.OUTCOME_NO_ITEMS
        elif any(k in joined for k in ("unpaid", "you can't pick",
                                       "you cannot pick",
                                       "don't have enough")):
            outcome = pickup.OUTCOME_REFUSED
        else:
            outcome = pickup.OUTCOME_UNKNOWN
        if not pickup.terminates_target(outcome):
            # unknown: the attempt is spent but the interaction phase and the
            # collection target survive until a terminal outcome/exhaustion
            if ev is not None:
                self.floor.note_outcome(ev, outcome)
            if ev is not None and not self.floor.budget_available(ev):
                self._finish_pickup(pickup.OUTCOME_EXHAUSTED, mem, ev)
            return
        self._finish_pickup(outcome, mem, ev)

    def _finish_pickup(self, outcome, mem, ev) -> None:
        """Apply a terminal pickup outcome and settle the collection target."""
        self.intent = ""
        self.pickup_purpose = ""
        self.pickup_init_inventory = None
        self.pickup_pending = None
        self.pickup_attempt_identity = None
        hero = mem.hero
        self.lifecycle.record(
            lifecycle_metrics.KIND_PICKUP,
            _PICKUP_EVENT_OUTCOME.get(outcome, lifecycle_metrics.PICKUP_UNKNOWN),
            token=(ev.token if ev is not None else None), outcome_detail=outcome)
        if ev is not None:
            self.floor.note_outcome(ev, outcome)
            if hero is not None:
                self.floor.note_negative(self.instance_id, tuple(hero),
                                         outcome)
        self._settle_pickup_target(outcome, mem)

    def _settle_pickup_target(self, outcome: str, mem=None) -> None:
        """Settle a directive-owned collection target after an outcome (3.3)."""
        held = self.targets.held()
        self.pickup_evidence = None
        if held is None or held.source != navigation.SRC_DIRECTIVE:
            return
        if outcome == pickup.OUTCOME_SUCCESS:
            if mem is not None:
                self._note_serviced(mem, held.pos)
            else:
                self.targets.note_serviced(held.pos, ())
            self.targets.retire("collected")
            self._settle_directive("reached", held.generation,
                                   "pickup-success")
        else:
            self.targets.retire("pickup-" + outcome)
            self._settle_directive("failed", held.generation,
                                   "pickup-" + outcome)

    def begin_instance(self, iid) -> None:
        """Start a fresh level-instance scope (plan 4.1 rule 6).

        The old instance's reflex-local recovery budgets, scoped food
        negatives, cycle history and pending intent are expired -- only the
        episode's committed inventory observations legitimately survive an
        arrival.
        """
        self.instance_id = int(iid or 0)
        self.recovery = recovery.RecoveryState()
        self.food = recovery.FoodNegatives()
        # A fresh level-instance scope clears the commitment and the scoped
        # serviced/failed ledgers (plan 1.5 "Level instance change").
        self.targets.reset()
        self.floor.reset()
        self.pickup_purpose = ""
        self.pickup_evidence = None
        self.pickup_init_inventory = None
        self.pickup_generation = 0
        self.pickup_pending = None
        self.pickup_attempt_identity = None
        self.directive_settlement = None
        self._cycled = False
        self.stuck = 0
        self.last_hero = None
        self.intent = ""
        self.eat_forced_menu = False
        self.forced_template = None
        self.last_prepared = None
        self.last_candidate = None

    def _may_eat(self, mem, hero) -> bool:
        """True unless scoped negatives already prove there is nothing to eat.

        An inventory-negative for the current signature blocks a blind eat;
        a known floor ration may still authorise a location-specific eat while
        the inventory stays negative (5.2).  The evidence is derived purely
        from the committed messages and the persisted negative, so the answer
        never depends on a fold performed during candidate construction.
        """
        sig = mem.inventory_signature()
        if self._food_negative(mem, sig):
            return bool(mem.inventory.food_rows())
        return True

    def _food_negative(self, mem, sig) -> bool:
        """Current inventory-negative evidence, pure (persisted + current)."""
        if self.food.inventory_negative(sig):
            return True
        if sig is None:
            return False
        for text in mem.recent_messages(6):
            if recovery.classify_food_negative(text) \
                    == recovery.FOOD_NEG_INVENTORY:
                return True
        return False

    def _allows_search(self, mem, hero) -> bool:
        """True only while a justified ordinary search is still bounded here.

        Combines the persisted per-site budget with the *current* refusal
        evidence derived purely from the recent messages (plan 5.1), so the
        decision is a pure function of public memory and needs no fold during
        candidate construction (plan 3.1).
        """
        site = self._search_site(hero)
        if recovery.refusal_in(mem.recent_messages(6)) is not None:
            return False
        return self.recovery.allows_search(site)

    def _terrain(self, mem, context=None):
        """The classified terrain used for routing (plan 1.1, AC12).

        Prefers the runner-owned **persistent classified terrain** carried on
        the reflex context, so known ground survives beneath a current
        item/creature overlay -- ``EpisodeMemory.grid`` is overwritten by the
        overlay, so rebuilding from it would lose the ground under a glyph.  A
        direct unit caller without that reference keeps a conservative rebuild
        from the remembered raw cells.
        """
        persistent = getattr(context, "terrain", None)
        if persistent is not None and hasattr(persistent, "ter"):
            return persistent
        terrain = navigation.TerrainMemory()
        terrain.merge(mem.grid)
        return terrain

    def _directive_component(self, target) -> int:
        """A bounded, logged directive contribution to a target's score."""
        if not self.directives.active:
            return 0
        want = self.directives.target
        if want is not None and tuple(want) == target.pos:
            return 30
        return 0

    def _hungry(self, st: state.Status) -> bool:
        return st.hunger.startswith(("Hungry", "Weak", "Fainting"))

    def _eat_interval(self) -> int:
        return EAT_DIRECTIVE_INTERVAL if self.directives.wants_food() \
            else EAT_RETRY_INTERVAL

    def _flee_fraction(self) -> float:
        return DISENGAGE_HP_FRACTION if self.directives.disengage() \
            else LOW_HP_FRACTION

    def _low_hp(self, st: state.Status) -> bool:
        if st.hp is None or not st.hp_max:
            return False
        return st.hp / float(st.hp_max) <= self._flee_fraction()

    def _adequate_hp(self, st: state.Status) -> bool:
        if st.hp is None or not st.hp_max:
            return True
        return st.hp / float(st.hp_max) > ADEQUATE_HP_FRACTION

    def _adjacent_monsters(self, mem, hero):
        out = []
        for d in KEY.DIR_KEYS:
            dest = (hero[0] + d[0], hero[1] + d[1])
            if state.monster_cell(mem.tile(dest), hero, dest):
                out.append(d)
        return out

    # -- safety: escape ------------------------------------------------
    def _escape(self, mem, hero):
        """Disengage at low HP: never toward a monster."""
        threats = self._adjacent_monsters(mem, hero)
        for dx, dy in threats:
            away = (-dx, -dy)
            if mem.known_passable((hero[0] + away[0], hero[1] + away[1])):
                return {"key": KEY.DIR_KEYS[away]}, "low HP: retreat"
        for d, k in KEY.DIR_KEYS.items():
            dest = (hero[0] + d[0], hero[1] + d[1])
            if d not in threats and mem.known_passable(dest):
                return {"key": k}, "low HP: sidestep"
        if hero in mem.stairs_up:
            return {"key": ord("<")}, "low HP: withdraw upstairs"
        target = self._nearest(mem.stairs_up, hero)
        if target is not None:
            step = self._first_step(mem, hero, target)
            if step is not None:
                return {"key": KEY.DIR_KEYS[step]}, "low HP: flee upstairs"
        if self._safe_to_rest(mem, mem.status, hero):
            return {"key": KEY.KEY_WAIT}, "low HP: hold position"
        return {"key": KEY.KEY_SEARCH}, "low HP: search for an exit"

    def _safe_to_rest(self, mem, st, hero) -> bool:
        if self._hungry(st):
            return False
        if not self._adequate_hp(st):
            return False
        return not self._adjacent_monsters(mem, hero)

    def _unblock(self, mem, hero, st):
        """Break a stuck state without walking into an ambiguous monster."""
        if self._safe_to_rest(mem, st, hero):
            return {"key": KEY.KEY_WAIT}
        if self._adjacent_monsters(mem, hero):
            target = self._frontier_target(mem, hero)
            if target is not None:
                step = self._first_step(mem, hero, target)
                if step is not None:
                    dest = (hero[0] + step[0], hero[1] + step[1])
                    if not state.monster_cell(mem.tile(dest), hero, dest):
                        return {"key": KEY.DIR_KEYS[step]}
            return {"key": KEY.KEY_SEARCH}
        return {"key": KEY.KEY_SEARCH}

    # -- navigation ------------------------------------------------------
    def _nav_reason(self, extra: str = "") -> str:
        """The navigate reason, optionally qualified by an additive note.

        The extra note rides the existing parenthesized qualifier slot, so the
        presentation's recognized-purpose mapping still resolves the route
        purpose (a raw diagnostic is never exposed as an instruction).
        """
        if self.directives.active:
            return "navigate (%s)" % self.directives.top_goal()
        if extra:
            return "navigate (%s)" % extra
        return "navigate"

    def _frontier_target(self, mem, hero):
        best = None
        for pos in mem.grid:
            if pos == hero or not state.passable(mem.tile(pos)):
                continue
            if self._is_frontier(mem, pos):
                if best is None or _manhattan(pos, hero) < \
                        _manhattan(best, hero):
                    best = pos
        return best

    def _nearest(self, cells, hero):
        cells = list(cells)
        if not cells:
            return None
        return min(cells, key=lambda p: _manhattan(p, hero))

    def _random_move(self, mem, hero):
        """A fallback move over *known* floor only.

        Unknown blanks are not freely traversable: a step into an unpainted
        cell is only ever taken by a deliberate navigation path that ends on a
        remembered frontier cell, never by this fallback.  When no known floor
        is available this never simply waits -- a wait is only safe when the
        hero is not hungry, not at low HP and has no adjacent monster -- so an
        unsafe hold becomes a search instead.
        """
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        for d in dirs:
            dest = (hero[0] + d[0], hero[1] + d[1])
            if mem.known_passable(dest):
                return KEY.DIR_KEYS[d], "random walk (known floor)"
        if hero is not None and self._safe_to_rest(mem, mem.status, hero):
            return KEY.KEY_WAIT, "no known floor: wait"
        return KEY.KEY_SEARCH, "no known floor and unsafe to rest: search"

    def _is_frontier(self, mem: state.EpisodeMemory, pos) -> bool:
        x, y = pos
        for nx, ny in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y)):
            if nx < protocol.MAP_MIN_X or nx > protocol.MAP_MAX_X:
                continue
            if ny < protocol.MAP_MIN_Y or ny > protocol.MAP_MAX_Y:
                continue
            if (nx, ny) not in mem.grid:
                return True
        return False

    def _first_step(self, mem, hero, target):
        """Dijkstra over remembered passable cells with a visit penalty."""
        if target == hero:
            return None
        dist = {hero: 0.0}
        parent: Dict[Tuple[int, int], Tuple[int, int]] = {}
        pq = [(0.0, hero)]
        found = False
        while pq:
            self._check_deadline()
            d, pos = heapq.heappop(pq)
            if d > dist.get(pos, float("inf")):
                continue
            if pos == target:
                found = True
                break
            x, y = pos
            for dx, dy in KEY.DIR_KEYS:
                nx, ny = x + dx, y + dy
                nxt = (nx, ny)
                if nxt not in mem.grid or not state.passable(
                        mem.tile(nxt)):
                    continue
                cost = 1.0 + 0.5 * mem.visits.get(nxt, 0)
                nd = d + cost
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    parent[nxt] = pos
                    heapq.heappush(pq, (nd, nxt))
        if not found:
            return None
        node = target
        while parent.get(node) != hero and node in parent:
            node = parent[node]
        if parent.get(node) != hero:
            return None
        return (node[0] - hero[0], node[1] - hero[1])

    # -- text / extcmd ---------------------------------------------------
    def _textish(self, context: ReflexContext):
        kind = (context.need or {}).get("kind")
        if kind == "extcmd" and (self.quitting or
                                 context.tick >= self.max_ticks):
            return {"text": "quit"}, "quit through the native prompt"
        if kind == "extcmd":
            return {"cancel": True}, "dismiss extended command prompt"
        return {"cancel": True}, "cancel line prompt"

    def on_closed(self):
        self.intent = ""
        self.quitting = True


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


# Navigation target families mapped to candidate families (candidates.py),
# and their initial ordinary base scores (plan 3.3).  The bounded path-length
# adjustment never crosses a family boundary (gaps are >= 100, adjustment is
# capped at 40), so a nearer stair cannot be suppressed indefinitely by a
# distant frontier.
#: The lifecycle outcome recorded for each pickup outcome (plan section 5).
_PICKUP_EVENT_OUTCOME = {
    pickup.OUTCOME_SUCCESS: lifecycle_metrics.PICKUP_SUCCEEDED,
    pickup.OUTCOME_NO_ITEMS: lifecycle_metrics.PICKUP_NO_ITEMS,
    pickup.OUTCOME_REFUSED: lifecycle_metrics.PICKUP_REFUSED,
    pickup.OUTCOME_CANCELED: lifecycle_metrics.PICKUP_CANCELED,
    pickup.OUTCOME_DECLINED: lifecycle_metrics.PICKUP_DECLINED,
    pickup.OUTCOME_EXHAUSTED: lifecycle_metrics.PICKUP_UNKNOWN,
    pickup.OUTCOME_UNKNOWN: lifecycle_metrics.PICKUP_UNKNOWN,
}

_NAV_FAMILY = {navigation.TFAM_STAIR: "stair",
               navigation.TFAM_DOOR: "door",
               navigation.TFAM_FRONTIER: "frontier",
               navigation.TFAM_UNVISITED: "unvisited"}
_NAV_BASE = {"stair": 800, "door": 700, "frontier": 500, "unvisited": 400}
_EXPLORE_BOOST = 450    # explore_frontier/search_dead_ends: hold stairs back
_MAX_STEP_UNITS = 40    # bounded path-length adjustment


def _target_score(family: str, cost: int, explore_first: bool) -> int:
    """The integer score of one reachable navigation target (3.3).

    *cost* is the target's integer Dijkstra distance (section 4.5), so the
    within-family ordering prefers the nearest reachable target.  The bounded
    adjustment never crosses a family boundary (gaps are >= 100, cap 40), so
    ordinary frontier bias cannot suppress a newly reachable staircase.
    """
    score = _NAV_BASE[family] - min(cost // navigation.BASE_STEP,
                                    _MAX_STEP_UNITS)
    if explore_first and family in ("frontier", "unvisited"):
        score += _EXPLORE_BOOST
    return score


def _directive_view(context) -> DirectiveView:
    """Extract the active directive view from a reflex context.

    The controller passes the view the :class:`DirectiveBook` produced for
    this decision; anything else (an empty list, a mock, a raw dict) degrades
    to the no-op view, so a directive can never inject an action.
    """
    raw = getattr(context, "directives", None)
    if isinstance(raw, (list, tuple)) and raw:
        view = raw[0]
        if isinstance(view, DirectiveView):
            return view
    if isinstance(raw, DirectiveView):
        return raw
    return DirectiveView(None)

