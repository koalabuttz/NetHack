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
from dataclasses import replace as _dc_replace
from typing import Dict, List, Optional, Tuple

from . import (arbitration, candidates, forced_search, instances,
               lifecycle_metrics, navigation, pickup, protocol, recovery,
               state)
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
        # Offer/first-action dedup sets (plan 5): an offer is recorded once per
        # evidence token at its first real dispatch, and a directive
        # first-action once per generation.
        self._pickup_offered = set()
        self._directive_first_action = set()
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
        # The message baseline captured when a door-open candidate is *armed*
        # (stall-recovery plan §4): refusal is classified only from messages
        # committed after it, so a stale locked message can never fail a newly
        # acquired door.  ``None`` means no door attempt was armed.
        self.door_attempt_baseline = None
        # The scoped failed-edge ledger (plan §1/§4): keyed by
        # ``(instance_id, src, dst)`` -> the edge's blocked signature.  A
        # zero-time failure of a selected recovery move suppresses that edge
        # under unchanged evidence; a change to the edge's signature reopens it.
        self.blocked_edges = {}
        # The scoped *prompt-declined* edge ledger (prompt-edge plan §D): keyed
        # by ``(instance_id, src, dst, movement_action_class)`` -> a bounded
        # ``(blocked_edge_signature, normalized_prompt_text)`` record.  One
        # record per directed edge/action class (overwritten), cleared on an
        # instance reset.  It is queried *per action class*, so a normal
        # navigation decline never suppresses the same edge for the emergency
        # class (a sole legal escape stays available).
        self.prompt_declined_edges = {}
        # Exit-(b) capability (prompt-edge plan §D/Phase 0): the cloud encoding
        # is indistinguishable, so a prompt-declined record suppresses
        # **regardless of local signature changes**.  Signature-based reopening
        # is gated behind this explicit positive-reopening capability, which
        # stays disabled until the mandatory operator-gated trace; an
        # instance/scope reset remains the only reopening.
        self.positive_reopening_enabled = False
        # The frozen origin of the pending confirmation (prompt-edge plan §A/F6):
        # held only for the bounded prompt window so the internal-only
        # ReflexContext seam can carry an immutable origin alongside the pending
        # context.  The in-flight *send* origin is otherwise transient controller
        # state consumed at the next observation.
        self._prompt_origin = None
        # The one bounded pending movement-confirmation context (plan §C).  It
        # is held on the reflex (which the controller and evaluator each own)
        # and driven by them: armed at the prompt's arrival, bound to the answer
        # send, and resolved only when the post-answer observation proves the
        # confirmation dismissed with an unchanged hero.
        self._pending_prompt = None

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
                      observed_kind="", payload=(), pre_hero=None) -> None:
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
            self._commit_destination(payload, tick, mem, pre_hero=pre_hero,
                                     observed_kind=observed_kind)
            # The refusal is classified *after* the destination effect, so a
            # fresh door acquisition whose first response is "This door is
            # locked." is installed and failed on that same response (§4/item 3).
            self._classify_door_refusal(mem)
            self.intent = ""
            return
        if payload and payload[0] == "recovery":
            # A selected *and reconciled* recovery move records its scoped edge
            # outcome and, when it advanced, retires the nominated destination
            # at this boundary (plan §1/§4).  Nothing else is committed here.
            self._note_recovery_outcome(
                payload[1], observed_kind, mem, pre_hero,
                held_serial=(payload[2] if len(payload) > 2 else None),
                reason=(payload[3] if len(payload) > 3 else ""))
            self.intent = ""
            return
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

    def arm_pickup(self, payload, identity=None, tick=0) -> bool:
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
        # The offer is recorded once per evidence token at the first *real
        # dispatch* -- never during pure candidate preparation, which may
        # discard or re-prepare the same offer many times (plan 3.3/5).
        if ev.token not in self._pickup_offered:
            self._pickup_offered.add(ev.token)
            self.lifecycle.record(lifecycle_metrics.KIND_PICKUP,
                                  lifecycle_metrics.PICKUP_OFFERED,
                                  token=ev.token, purpose=mode)
        self.lifecycle.record(lifecycle_metrics.KIND_PICKUP,
                              lifecycle_metrics.PICKUP_ATTEMPTED,
                              token=ev.token, purpose=mode)
        self.intent = "pickup"
        self.pickup_purpose = mode
        self.pickup_evidence = ev
        self.pickup_init_inventory = init_sig
        # An on-square collection's first dispatch atomically acquires its
        # directive-owned interacting commitment here, at the send boundary.
        if len(payload) > 9 and payload[9]:
            self._acquire_on_square(payload[9], pos, tick)
        return True

    def _acquire_on_square(self, spec, pos, tick) -> None:
        """Acquire the directive-owned interacting commitment for an on-square
        collection (plan 1.5/3.3).

        Idempotent per (generation, coordinate): emits ``DEST_ACQUIRED`` and
        ``DIR_RESOLVED`` exactly once for that acquisition, so the eventual
        pickup outcome settles the same serial rather than leaving the
        directive active.
        """
        purpose, family, source, generation = spec[:4]
        held = self.targets.held()
        if (held is not None and held.source == navigation.SRC_DIRECTIVE
                and held.generation == int(generation)
                and tuple(held.pos) == tuple(pos)):
            return                          # already acquired
        self.targets.commit(instance_id=self.instance_id, purpose=purpose,
                            pos=tuple(pos), family=family, source=source,
                            generation=int(generation), tick=int(tick),
                            phase=navigation.PHASE_INTERACTING)
        serial = self.targets.held().serial
        self.lifecycle.record(lifecycle_metrics.KIND_DESTINATION,
                              lifecycle_metrics.DEST_ACQUIRED, serial=serial,
                              purpose=purpose, source=source,
                              generation=int(generation),
                              reason="on-square collection")
        self._record_directive_resolved(source, generation,
                                        "collect the items here",
                                        resolved=True)

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

    @staticmethod
    def _door_interaction_targets(pre_hero, hero, door_pos, dirdata) -> bool:
        """True only when the pre-send action targets the door (§2A rule 6).

        With a known selected pre-send direction (a policy-built continuation),
        the action must step from an *approach* square (Chebyshev 1 of the door)
        exactly onto the door cell; merely ending a move adjacent to the door is
        not an interaction.  A payload carrying no direction -- a legacy/direct
        unit caller -- keeps the legacy adjacency test so those callers are
        unaffected.
        """
        dp = tuple(door_pos)
        if not dirdata:
            return hero is not None and recovery.chebyshev(hero, dp) <= 1
        if pre_hero is None:
            return False
        ph = tuple(pre_hero)
        if recovery.chebyshev(ph, dp) != 1:
            return False
        return (ph[0] + int(dirdata[0]), ph[1] + int(dirdata[1])) == dp

    def _edge_blocked(self, terrain, src, dst) -> bool:
        """True while a recorded failure holds this edge under unchanged (§1/§4).

        The scoped ledger stores the edge's :func:`blocked_edge_signature`; the
        edge remains suppressed only while the *current* signature is identical,
        so a change to the edge's legality-relevant cells (a blocker leaving)
        reopens it.
        """
        key = (self.instance_id, tuple(src), tuple(dst))
        stored = self.blocked_edges.get(key)
        if stored is None:
            return False
        return stored == navigation.blocked_edge_signature(terrain, tuple(src),
                                                           tuple(dst))

    # -- prompt-declined edge evidence (prompt-edge plan §C/§D) -----------
    @staticmethod
    def normalize_action_class(action_class) -> str:
        """Fold a movement action class into the two-class ledger (plan §D/§E).

        Destination navigation, random movement and bounded recovery all share
        the **normal** class (a decline suppresses the edge for every ordinary
        movement); only the emergency escape class is distinct, so a sole legal
        emergency escape is never suppressed by a normal decline.
        """
        return (navigation.ACTION_EMERGENCY
                if str(action_class) == navigation.ACTION_EMERGENCY
                else navigation.ACTION_NORMAL)

    def prompt_decline_record_key(self, instance, src, dst, action_class):
        """The bounded prompt-declined ledger key: edge + action class (§D)."""
        return (int(instance), tuple(src), tuple(dst),
                self.normalize_action_class(action_class))

    def record_prompt_decline(self, instance, src, dst, action_class, mem,
                              prompt_text) -> None:
        """Write the one bounded ``prompt-declined`` record for a directed edge.

        Keyed by ``(instance, src, dst, action_class)``; the value is the edge's
        current blocked-edge signature plus the normalized prompt text, so one
        record per directed edge/action class is retained (overwritten) and an
        unrelated change can never reopen it.
        """
        terrain = self._terrain(mem)
        key = self.prompt_decline_record_key(instance, src, dst, action_class)
        self.prompt_declined_edges[key] = (
            navigation.blocked_edge_signature(terrain, tuple(src), tuple(dst)),
            " ".join(str(prompt_text or "").split()))

    def _prompt_edge_suppressed(self, terrain, src, dst, action_class) -> bool:
        """True while a prompt-decline record holds this edge for *action_class*.

        Under exit (b) the record suppresses **regardless of any local
        signature change** (terrain corridor/floor transitions, occupant
        presence, side cells, time, visits or remote cells): the stored
        signature and prompt text are retained for diagnostics only.
        Signature-based reopening is gated behind
        :attr:`positive_reopening_enabled`, which stays disabled until the
        mandatory operator-gated cloud trace.
        """
        rec = self.prompt_declined_edges.get(
            self.prompt_decline_record_key(self.instance_id, src, dst,
                                           action_class))
        if rec is None:
            return False
        if not self.positive_reopening_enabled:
            return True
        return rec[0] == navigation.blocked_edge_signature(
            terrain, tuple(src), tuple(dst))

    def edge_suppressed(self, terrain, src, dst,
                        action_class=navigation.ACTION_NORMAL) -> bool:
        """The immutable blocked-edge view for one directed edge (plan §E).

        Consults the recovery zero-time ledger (normal/recovery classes only)
        and the prompt-decline ledger *for the requested action class*.  The
        emergency class therefore never inherits a normal-navigation prompt
        suppression, so a sole legal escape stays available.
        """
        src = tuple(src)
        dst = tuple(dst)
        if action_class != navigation.ACTION_EMERGENCY \
                and self._edge_blocked(terrain, src, dst):
            return True
        return self._prompt_edge_suppressed(
            terrain, src, dst, self.normalize_action_class(action_class))

    def _edge_admissible(self, terrain, action_class):
        """A pure, directed edge-admissibility predicate for one plan (§E)."""
        def pred(src, dst, _cls):
            return not self.edge_suppressed(terrain, src, dst, action_class)
        return pred

    # -- the one bounded pending prompt context (prompt-edge plan §C) -----
    def arm_movement_prompt(self, origin, response_need, instance,
                            confirmed_hero, response_need_key=()) -> bool:
        """Arm the pending prompt context for a matched confirmation (§A/§C).

        Returns ``True`` only when a new context was created -- the
        identity-bound term the caller folds into ``advance_stationary``.  A
        re-presented confirmation (an existing pending context) creates no
        second context and therefore earns no second count.  The context is
        bound to the **validated ``yn`` response NeedKey**.
        """
        if self._pending_prompt is not None:
            return False
        pending = arbitration.matched_movement_prompt(
            origin, response_need, instance, confirmed_hero,
            response_need_key)
        if pending is None:
            return False
        self._pending_prompt = pending
        self._prompt_origin = origin
        return True

    @property
    def pending_prompt(self):
        """The current bounded pending movement-confirmation context, or None."""
        return self._pending_prompt

    @property
    def prompt_origin(self):
        """The frozen origin edge of the pending confirmation, or ``None``."""
        return self._prompt_origin

    def note_prompt_answer_sent(self, ordinal, need_key=(), answer_byte=None):
        """Bind a sent answer, or discard on a non-decline / replacement (§C).

        The answer binds only when its need key is the pending context's
        validated response NeedKey **and** the byte is exactly ``KEY_N``; a
        `y`/ESC answer or an answer to a different need clears the context and
        writes nothing (the confirmation was not declined).
        """
        p = self._pending_prompt
        if p is None:
            return
        if not arbitration.answer_binds_to_prompt(p, need_key, answer_byte):
            self._pending_prompt = None
            self._prompt_origin = None
            return
        if p.answer_sent:
            return
        self._pending_prompt = _dc_replace(
            p, answer_sent=True, answer_ordinal=int(ordinal),
            answer_byte=int(answer_byte))

    def resolve_prompt_decline(self, mem, instance, confirmed_hero,
                               dismissed) -> bool:
        """Write the decline record iff the post-answer observation proves it."""
        p = self._pending_prompt
        if not arbitration.prompt_decline_confirmed(
                p, instance, confirmed_hero, dismissed):
            return False
        self.record_prompt_decline(p.instance, p.src, p.dst, p.action_class,
                                   mem, p.prompt_text)
        self._pending_prompt = None
        self._prompt_origin = None
        return True

    def clear_pending_prompt(self) -> None:
        """Discard the pending context (reset / transition / replacement)."""
        self._pending_prompt = None
        self._prompt_origin = None

    def _recovery_payload(self, step) -> tuple:
        """The frozen recovery effect payload (plan §1).

        Carries the recovery *nomination*: when a destination is held, its
        serial and the nomination reason (``cycle`` or ``stalled``) ride the
        selected effect.  Retirement therefore happens only when that exact
        effect is sent and reconciled as *moved* -- never at nomination time,
        and never for a preemption, override or write failure.
        """
        held = self.targets.held()
        if held is None:
            return ("recovery", tuple(step))
        reason = "cycle" if self._cycled else "stalled"
        return ("recovery", tuple(step), held.serial, reason)

    def _note_recovery_outcome(self, step, observed_kind, mem, pre_hero,
                               held_serial=None, reason="") -> None:
        """Record one *selected and reconciled* recovery move's edge outcome.

        A zero-time failure becomes scoped edge/action failure evidence keyed by
        ``(instance, src, dst)`` so unchanged evidence cannot select that edge
        forever (§1); a move that advanced clears it.  Only a recovery move that
        was actually selected, sent and reconciled reaches here.
        """
        if not step or pre_hero is None:
            return
        src = tuple(pre_hero)
        dst = (src[0] + int(step[0]), src[1] + int(step[1]))
        key = (self.instance_id, src, dst)
        if observed_kind in ("no-time", "stationary-time-advanced"):
            # A zero-time failure is *only* scoped edge evidence (§1): it never
            # retires the destination, spends no counter and emits no terminal.
            self.blocked_edges[key] = navigation.blocked_edge_signature(
                self._terrain(mem), src, dst)
            return
        self.blocked_edges.pop(key, None)
        if observed_kind != "moved":
            # unknown outcome: ownership stays with the destination
            return
        # The recovery move actually advanced the hero: this is the reconciled
        # recovery-effect boundary at which the nominated destination is
        # retired exactly once (§1).  A serial that has already been retired or
        # replaced is never resurrected.
        if held_serial is None:
            return
        cur = self.targets.held()
        if cur is None or cur.serial != int(held_serial):
            return
        # retirement records the site suppression under its exploration
        # signature, so the unchanged site is not re-elected (no reacquisition)
        self._retire_owned(reason or "recovery",
                           outcome=lifecycle_metrics.DEST_EXPIRED,
                           pos=cur.pos,
                           signature=navigation.service_signature(
                               self._terrain(mem), cur.pos))

    @staticmethod
    def _hero_moved(pre_hero, mem, observed_kind) -> bool:
        """True when the reconciled hero advanced from the frozen pre-send
        square (stall-recovery plan §2A).

        Prefers the exact frozen-vs-reconciled comparison; a direct caller
        without a pre-send baseline falls back to the reconciled outcome kind,
        so a ``moved`` acquisition stays a moved acquisition.
        """
        if pre_hero is not None and mem.hero is not None:
            return tuple(mem.hero) != tuple(pre_hero)
        return observed_kind == "moved"

    def _commit_destination(self, payload, tick, mem, pre_hero=None,
                            observed_kind="") -> None:
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
        vals = tuple(payload[:11])
        if len(vals) >= 11:
            (_tag, op, iid, purpose, x, y, family, source, generation,
             expected, reason) = vals
        else:
            (_tag, op, iid, purpose, x, y, family, source, generation,
             expected) = vals
            reason = ""
        # Additive selected pre-send direction (§2A rule 6): present on a
        # continuation candidate built by the policy, empty for a legacy/direct
        # caller.  It lets the door-interaction count require the action to
        # actually target the door from an approach square.
        dirdata = tuple(payload[11]) if len(payload) > 11 and payload[11] else ()
        # The initial route hop count of a fresh acquisition (plan §2): carried
        # into the total navigation cap rather than the default cap.
        hops = payload[12] if len(payload) > 12 else None
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
                    serial = self.targets.held().serial
                    self.lifecycle.record(
                        lifecycle_metrics.KIND_DESTINATION,
                        lifecycle_metrics.DEST_ACQUIRED, serial=serial,
                        purpose=purpose, source=source,
                        generation=int(generation), reason=reason)
                    self._record_directive_resolved(source, generation, reason)
                    return
                # the observation that justified acquisition already satisfied
                # it: record reached instead of installing (plan 1.4).  A
                # one-hop acquisition emits a coherent acquired -> terminal pair
                # with exactly one real attempt (plan §2A/§3); a directive-owned
                # one additionally settles its generation (item 4).
                if source == navigation.SRC_DIRECTIVE:
                    self._note_serviced(mem, pos)
                    serial = self.targets._serial + 1
                    self._emit_destination_terminal(
                        lifecycle_metrics.DEST_REACHED, reason or "reached",
                        serial, purpose=purpose, source=source,
                        generation=int(generation))
                    self._settle_directive("reached", generation, reason)
                    return
                self._note_serviced(mem, pos)
                serial = self.targets._serial + 1
                self.lifecycle.record(
                    lifecycle_metrics.KIND_DESTINATION,
                    lifecycle_metrics.DEST_ACQUIRED, serial=serial,
                    purpose=purpose, source=source, generation=int(generation),
                    reason=reason)
                self._emit_destination_terminal(
                    lifecycle_metrics.DEST_REACHED, "reached", serial,
                    purpose=purpose, source=source, generation=int(generation))
                return
            cur = self.targets.held()
            if (cur is not None and cur.pos == pos
                    and cur.purpose == purpose and cur.instance_id == iid):
                return                      # idempotent: already committed
            replaced = cur is not None
            # Emit the explicit site-level *reopen* fact (review item 6): the
            # target was previously serviced and is being re-acquired under a
            # different service signature.  This is deliberately distinct from
            # the serial `replaced` terminal, which is only a replacement.
            prev_sig = self.targets.serviced_signature(pos)
            if prev_sig is not None:
                now_sig = navigation.service_signature(self._terrain(mem), pos)
                if tuple(prev_sig) != tuple(now_sig):
                    self.lifecycle.record(
                        lifecycle_metrics.KIND_DESTINATION,
                        lifecycle_metrics.DEST_REOPENED,
                        serial=self.targets._serial + 1, purpose=purpose,
                        source=source, generation=int(generation),
                        reason="service-signature-changed")
            self.targets.commit(instance_id=iid or self.instance_id,
                                purpose=purpose, pos=pos, family=family,
                                source=source, generation=int(generation),
                                tick=tick, hero=mem.hero, hops=hops)
            # §2A: a no-time acquisition (the hero did not move from the
            # frozen pre-send square) installs the target and counts as
            # no-progress attempt 1 of 3; a moved acquisition charges only the
            # route cap and starts stall progress from the reconciled hero.
            if not self._hero_moved(pre_hero, mem, observed_kind):
                self.targets.note_nav_attempt()
            serial = self.targets.held().serial
            if replaced:
                # §3: the superseded serial is terminated exactly once with
                # ``reason=replaced`` and its replacement serial; the new
                # serial's acquisition is a separate, distinct event -- the two
                # are never combined, and the pairing is the sole input to the
                # replacement switch count.
                self._emit_destination_terminal(
                    lifecycle_metrics.DEST_REPLACED, "replaced", cur.serial,
                    purpose=cur.purpose, source=cur.source,
                    generation=cur.generation, replacement_serial=serial)
            self.lifecycle.record(
                lifecycle_metrics.KIND_DESTINATION,
                lifecycle_metrics.DEST_ACQUIRED,
                serial=serial, purpose=purpose, source=source,
                generation=int(generation), reason=reason)
            self._record_directive_resolved(source, generation, reason)
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
            # the owner emits the destination terminal for every source and
            # settles a directive-owned generation (item 4)
            self._retire_owned("reached", outcome=lifecycle_metrics.DEST_REACHED,
                               settle_reason=reason)
            return
        # continue / interact
        self.targets.note_nav_attempt()
        self.lifecycle.record(lifecycle_metrics.KIND_DESTINATION,
                              lifecycle_metrics.DEST_ACTION,
                              serial=cur.serial, purpose=cur.purpose)
        if cur.source == navigation.SRC_DIRECTIVE \
                and cur.generation not in self._directive_first_action:
            # the first reconciled destination action of this generation
            self._directive_first_action.add(cur.generation)
            self.lifecycle.record(lifecycle_metrics.KIND_DIRECTIVE,
                                  lifecycle_metrics.DIR_FIRST_ACTION,
                                  generation=int(cur.generation),
                                  serial=cur.serial)
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
            if self._door_interaction_targets(pre_hero, hero, cur.pos, dirdata):
                self.targets.note_interact_attempt()
            if navigation.door_open(self._terrain(mem), pos):
                self._note_serviced(mem, pos)
                # the door-open branch retires through the owner, which settles a
                # directive-owned generation too (item 4)
                self._retire_owned("door-opened",
                                   outcome=lifecycle_metrics.DEST_REACHED)
                return
            sig = self._door_failure_signature(mem, cur.pos)
            if self.targets.door_attempts_exhausted:
                self._retire_owned("door-ineffective",
                                   outcome=lifecycle_metrics.DEST_FAILED,
                                   pos=cur.pos, signature=sig)
                return
        if self.targets.stalled():
            held = self.targets.held()
            if held is not None:
                self._retire_owned(
                    "stalled", outcome=lifecycle_metrics.DEST_FAILED,
                    pos=held.pos,
                    signature=navigation.service_signature(
                        self._terrain(mem), held.pos))

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
            self._record_directive_resolved(held.source, held.generation,
                                            reason, resolved=False)
            self._retire_owned(
                "unreachable" if reason == "unreachable"
                else "directive-unresolved",
                outcome=lifecycle_metrics.DEST_FAILED, pos=held.pos,
                signature=navigation.service_signature(
                    self._terrain(mem), held.pos),
                settle_reason=reason)
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

        # A recognized blocking movement-entry confirmation (prompt-edge plan
        # §A/AC2) is always declined, *before* the native default and the
        # conservative visible-choice selection, so a `yes` native default can
        # never accept an unlearned cloud entry.  Declining is the safe action
        # and the fix counts on the decline being recorded, never on a blind
        # acceptance.
        if arbitration.is_movement_entry_confirmation(prompt):
            return {"yn": KEY.KEY_N}, "decline cloud entry", "prompt", ()

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
        # 3. loop breakers: every stationary threshold (and cycle recovery)
        #    shares ONE bounded, edge-legal recovery builder (plan §1), so no
        #    threshold can route a raw-grid frontier step into a locked door or
        #    an unbounded search/wait.  The 3/6/10 escalation concept is kept;
        #    only the recovery mechanism is the shared legal builder.
        np = mem.no_progress
        if np >= 10:
            return self._bounded_recovery(
                mem, hero, "loop breaker: bounded escape (>=10)")
        if np >= 6:
            return self._bounded_recovery(
                mem, hero, "loop breaker: bounded escape (>=6)")
        if np >= 3:
            if self._allows_search(mem, hero) and not self._cycled:
                return (self._cand({"key": KEY.KEY_SEARCH}, "search",
                                   "recovery", 0, "loop breaker: search",
                                   "site-search"),)
            # a refused or exhausted ordinary search at this site is
            # suppressed (5.1): proceed to *legal* bounded recovery, never back
            # into an unchanged failed route or another raw-grid step
            return self._bounded_recovery(
                mem, hero, "loop breaker: bounded recovery (search refused)")
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
        plan = navigation.plan(
            terrain, hero, mem.visits, None, self._check_deadline,
            edge_admissible=self._edge_admissible(terrain,
                                                  navigation.ACTION_NORMAL))
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
            payload = self._dest_payload("continue", held, step=step)
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
                # Split evidence (§4): a successfully serviced site is
                # suppressed under its exploration-only signature, and a closed
                # door's failure under its target-bound door signature -- so a
                # neighbouring creature's movement reopens neither.
                pos = t.pos
                serviced_sig = navigation.service_signature(terrain, pos)
                failed_sig = (navigation.door_failure_signature(terrain, pos)
                              if terrain.ter(pos) == instances.T_CLOSED_DOOR
                              else serviced_sig)
                return (not self.targets.serviced_under_evidence(
                            pos, serviced_sig)
                        and not self.targets.failed_under_evidence(
                            pos, failed_sig))

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

    def _record_directive_resolved(self, source, generation, reason,
                                   resolved=True) -> None:
        """Record a directive destination's resolution outcome (plan 5).

        Emitted at the reconcile fold where the resolved destination is
        committed (or its failure settled) -- the destination's actual
        resolution boundary; a proposal that is never sent/reconciled is not a
        resolution.
        """
        if source != navigation.SRC_DIRECTIVE:
            return
        self.lifecycle.record(
            lifecycle_metrics.KIND_DIRECTIVE, lifecycle_metrics.DIR_RESOLVED,
            generation=int(generation), resolved=bool(resolved), reason=reason)

    def on_directive_expired(self, generation=None) -> None:
        """Terminal for a held directive-owned destination whose advice lapsed.

        Directive expiry / precondition failure is a *directive-owned* terminal
        (plan §3): it is never laundered into a default destination, and it is
        emitted exactly once before the destination is cleared.
        """
        held = self.targets.held()
        if held is None or held.source != navigation.SRC_DIRECTIVE:
            return
        if generation is not None and held.generation != int(generation):
            return
        self._retire_owned("directive_expired",
                           outcome=lifecycle_metrics.DEST_EXPIRED,
                           settle_reason="directive_expired")

    def episode_close(self) -> None:
        """Emit the episode-close terminal for a held destination (plan §3).

        Emitted *before* episode state is cleared, so a destination still held
        at episode end cannot silently vanish from the lifecycle stream.
        """
        # the owner emits exactly one terminal for every source and settles a
        # directive-owned generation; clearing it makes a second close a no-op
        self._retire_owned("episode_close",
                           outcome=lifecycle_metrics.DEST_EXPIRED,
                           settle_reason="episode_close")

    def _emit_destination_terminal(self, outcome, reason, serial, *,
                                   purpose=None, source=None, generation=None,
                                   replacement_serial=None) -> None:
        """Emit exactly one terminal destination lifecycle event (plan §3).

        The single emission primitive every retirement path funnels through, so
        a default and a directive-owned destination are terminated by the same
        code and a terminal can never be silently omitted or doubled.
        """
        fields = {"serial": serial, "reason": reason}
        if purpose is not None:
            fields["purpose"] = purpose
        if source is not None:
            fields["source"] = source
        if generation is not None:
            fields["generation"] = int(generation)
        if replacement_serial is not None:
            fields["replacement_serial"] = int(replacement_serial)
        self.lifecycle.record(lifecycle_metrics.KIND_DESTINATION, outcome,
                              **fields)

    def _retire_owned(self, reason, *, outcome=None, pos=None, signature=None,
                      settle_reason=None):
        """The single destination retirement owner (plan §3, review item 4).

        Captures ``(instance, serial, source, purpose, generation)`` *before*
        clearing state and emits **exactly one** terminal destination event for
        **every** source -- default and directive-owned alike.  A directive
        settlement is then layered on as a separate, once-only side effect (it
        records the directive-generation terminal and queues the book expiry)
        and never owns or duplicates the destination terminal.  Settling here,
        rather than in each caller, is what stops a directive-owned retirement
        from being reasserted on the next unchanged command boundary.  Returns
        the retired commitment, or ``None`` when nothing was held.
        """
        held = self.targets.held()
        if held is None:
            return None
        term = outcome or lifecycle_metrics.DEST_FAILED
        self.targets.retire(reason, pos=pos, signature=signature)
        self._emit_destination_terminal(
            term, reason, held.serial, purpose=held.purpose,
            source=held.source, generation=held.generation)
        if held.source == navigation.SRC_DIRECTIVE:
            self._settle_directive(
                "reached" if term == lifecycle_metrics.DEST_REACHED
                else "failed",
                held.generation, settle_reason or reason, serial=held.serial)
        return held

    def _retire_cycle_owned(self, signature=None):
        """Cycle/recovery invalidation through the one terminal owner (§1/§3).

        Cycle detection may *nominate* recovery, but the destination is
        invalidated here, at the reconciled observation fold, exactly once and
        with a visible terminal for **every** source (review item 4).
        """
        held = self.targets.held()
        if held is None:
            self.targets.invalidate_cycle(signature)
            return None
        self.targets.invalidate_cycle(signature)
        self._emit_destination_terminal(
            lifecycle_metrics.DEST_EXPIRED, "cycle", held.serial,
            purpose=held.purpose, source=held.source,
            generation=held.generation)
        if held.source == navigation.SRC_DIRECTIVE:
            # a directive-owned cycle termination settles its generation too,
            # so the advice is not reasserted on the next command boundary
            self._settle_directive("failed", held.generation, "cycle",
                                   serial=held.serial)
        return held

    def _settle_directive(self, outcome, generation, reason, serial=None) -> None:
        """Queue a directive-owned destination settlement (plan 1.5/item 4).

        A *separate, once-only side effect*: it records the directive-generation
        terminal and queues the book expiry.  It does **not** emit the
        destination terminal -- that is owned solely by the retirement owner
        (:meth:`_retire_owned` / :meth:`_retire_cycle_owned`), which emits it
        for every source, so the terminal is never duplicated or omitted.
        *serial* is retained for the callers that name the retired serial.
        """
        self.directive_settlement = (outcome, int(generation), str(reason))
        self.lifecycle.record(
            lifecycle_metrics.KIND_DIRECTIVE,
            lifecycle_metrics.DIR_TERMINAL, generation=int(generation),
            outcome_detail=outcome, reason=reason)

    @staticmethod
    def _dest_payload(op, held, target=None, purpose=None, source=None,
                      generation=None, reason=None, step=None):
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
        # Additive 12th field: the selected pre-send direction for a
        # continuation (§2A rule 6), empty when the caller does not supply one.
        # Additive 13th field: the initial route hop count of a fresh
        # acquisition (plan §2), derived from the target's Dijkstra cost.
        hops = None
        if held is None and target is not None:
            # the TRUE edge count when the caller has it (review item 5); the
            # weighted Dijkstra cost folds in visit/failure penalties and is
            # only a last-resort fallback for a caller that did not supply it
            true_hops = getattr(target, "hops", None)
            if true_hops is not None:
                hops = max(1, int(true_hops))
            elif getattr(target, "cost", None):
                hops = max(1, int(target.cost) // navigation.BASE_STEP)
        return ("dest", op, int(iid), purpose, int(pos[0]), int(pos[1]),
                family, source, int(generation), int(expected),
                str(reason or ""),
                tuple(step) if step else (),
                hops)

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
        onsquare = None
        if self.on_square_collect and mode == "collect":
            # The first on-square pickup dispatch is *also* the destination
            # acquisition: the frozen pickup payload carries the acquisition
            # identity so arm_pickup can install the directive-owned
            # interacting commitment at the same sent/reconciled boundary
            # (plan 1.5/3.3), without any mutation during preparation.
            onsquare = (navigation.COMMIT_COLLECT_ITEMS,
                        navigation.TFAM_UNVISITED,
                        navigation.SRC_DIRECTIVE,
                        int(self.directives.generation))
        payload = ("pickup", mode, int(ev.instance), int(ev.pos[0]),
                   int(ev.pos[1]), int(ev.source_epoch),
                   int(self.directives.generation), str(ev.appearance),
                   mem.inventory_signature(), onsquare)
        reason = "pick up the items here (%s)" % mode
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

        Delegates to the one shared bounded recovery builder (§1), so cycle
        recovery and every stationary threshold use the *same* legal mechanism.
        """
        return self._bounded_recovery(
            mem, hero, "cycle recovery: leave the repeating movement")

    def _bounded_recovery(self, mem, hero, why):
        """The one shared bounded, edge-legal recovery candidate (plan §1).

        Used by the 3/6/10 stationary thresholds and by cycle recovery.  It
        enumerates known-safe neighbours with the same classified terrain and
        :func:`navigation.edge_legal` checks planning uses (so no raw-grid
        frontier step, locked-door entry, unsafe diagonal or monster step leaks
        in), and prefers a non-reversing exit by a deterministic
        visit-count/direction-rank order.  A traversable dead end whose only
        legal escape is backtracking keeps that reversal -- it is never
        misreported as trapped.  With no legal movement at all it reuses the
        existing bounded search-fallback / forced-search nomination machinery
        rather than manufacturing an unbudgeted search, a dangerous prefix or
        an indefinite wait.  Returns a one-member candidate tuple.
        """
        step = self._recovery_legal_step(mem, hero)
        if step is not None:
            return (self._cand(
                {"key": KEY.DIR_KEYS[step]}, "recovery-step", "recovery", 0,
                why, "recovery",
                direction=step, direction_rank=navigation.DIR_RANK[step],
                effect_payload=self._recovery_payload(step)),)
        # No legal movement exists: a *bounded* ordinary search first (its own
        # per-site budget still caps it), and only once that is refused or
        # exhausted does the bounded search-fallback / forced-search / trapped
        # escalation run -- so `s` is still never an unbounded fallback.
        if self._allows_search(mem, hero):
            return (self._cand({"key": KEY.KEY_SEARCH}, "search", "recovery",
                               0, why, "site-search"),)
        return self._search_fallback(mem, hero)

    def _recovery_legal_step(self, mem, hero):
        """The best legal, non-reversing escape step, or ``None`` (§1).

        The single enumeration shared by every stationary threshold and cycle
        recovery; ``None`` means no legal movement exists, so the caller
        escalates to the bounded search-fallback machinery.
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
            # Skip an edge whose scoped failure still holds under the *same*
            # blocked-edge signature (plan §1/§4): unchanged evidence cannot
            # select a zero-time-failed recovery edge forever, while a change
            # to that edge's legality/occupancy signature reopens it.
            if self.edge_suppressed(terrain, hero, dest,
                                    navigation.ACTION_RECOVERY):
                continue
            reversing = self._is_reverse(step, hero, previous)
            options.append((0 if not reversing else 1,
                            mem.visits.get(dest, 0),
                            navigation.DIR_RANK[step], step))
        if not options:
            return None
        options.sort()
        return options[0][3]

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
        # Cycle detection only *nominates* recovery (plan §1): it does NOT
        # retire the held destination here.  The nomination (held serial +
        # reason) rides the selected recovery effect, and retirement happens at
        # that effect's reconciled boundary -- so an emergency/maintenance
        # preemption that replaces the recovery move can neither retire the
        # destination nor spend its counters.
        self._fold_floor(mem)
        self._fold_pickup_outcome(mem)
        # Door refusal is NOT classified here: the pre-commit fold runs before
        # the destination effect is applied, so a fresh door acquisition would
        # not yet be installed.  It is classified at the matched reducer
        # (commit_effect -> _classify_door_refusal) instead (§4/item 3).
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
        """Service a site under its current *exploration* signature (§4).

        The service signature excludes occupancy, time and visits, so a
        serviced waypoint is reopened only by a genuine local exploration
        change -- never by a neighbouring creature merely moving.
        """
        self.targets.note_serviced(
            tuple(pos),
            navigation.service_signature(self._terrain(mem), tuple(pos)))

    def _door_failure_signature(self, mem, pos) -> tuple:
        """The target-bound door failure signature at *pos* (§4)."""
        return navigation.door_failure_signature(self._terrain(mem),
                                                 tuple(pos))

    def _unreachable_destination_candidate(self, held):
        """A frozen failure for a held destination with no route (plan 1.5)."""
        payload = ("destfail", "unreachable", int(held.generation))
        return self._cand({"key": KEY.KEY_SEARCH}, "unresolved-destination",
                          "recovery", 0,
                          "the held destination is unreachable",
                          "dest-unresolved", effect_payload=payload)

    def arm_door_baseline(self, mark) -> None:
        """Capture the message baseline of an armed door-open attempt (§4).

        Called by the controller at the *send* boundary of a door interaction;
        ``None`` clears the binding for a non-door attempt.  :meth:`arm_door_
        baseline` never mutates gameplay memory, so an unarmed/discarded
        proposal leaves it untouched.
        """
        self.door_attempt_baseline = None if mark is None else int(mark)

    def _classify_door_refusal(self, mem) -> None:
        """Classify an explicit door refusal at the matched reducer (§4/item 3).

        The refusal is consumed only from messages committed *after* the frozen
        sent attempt's baseline, and only when a door is actually held at this
        reconciled boundary.  A missing baseline means *no matching refusal
        evidence* -- never a scan of the general recent-message window.  Running
        here (after :meth:`_commit_destination`) means a fresh door acquisition
        whose *first* response is "This door is locked." is installed and then
        failed exactly once on that same response.
        """
        baseline = getattr(self, "door_attempt_baseline", None)
        if baseline is None:
            return
        held = self.targets.held()
        if held is None or held.purpose != navigation.COMMIT_OPEN_DOOR:
            return
        joined = " ".join(t.lower() for t in mem.messages_since(baseline, 6))
        if not any(k in joined for k in ("is locked", "it's locked",
                                         "locked door", "resists",
                                         "you cannot open")):
            return
        self._retire_owned(
            "locked-door", outcome=lifecycle_metrics.DEST_FAILED,
            pos=held.pos, signature=self._door_failure_signature(mem,
                                                                 held.pos),
            settle_reason="locked-door")

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
        held = self.targets.held()
        if (held is not None and held.source == navigation.SRC_DIRECTIVE
                and held.purpose == navigation.COMMIT_COLLECT_ITEMS
                and held.generation not in self._directive_first_action):
            # the reconciled pickup action is this generation's first
            # destination action (plan 5)
            self._directive_first_action.add(held.generation)
            self.lifecycle.record(lifecycle_metrics.KIND_DIRECTIVE,
                                  lifecycle_metrics.DIR_FIRST_ACTION,
                                  generation=int(held.generation),
                                  serial=held.serial)
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
            # the owner emits the destination terminal and settles the
            # directive generation once (item 4)
            self._retire_owned("collected",
                               outcome=lifecycle_metrics.DEST_REACHED,
                               settle_reason="pickup-success")
        else:
            self._retire_owned("pickup-" + outcome,
                               outcome=lifecycle_metrics.DEST_FAILED,
                               settle_reason="pickup-" + outcome)

    def begin_instance(self, iid) -> None:
        """Start a fresh level-instance scope (plan 4.1 rule 6).

        The old instance's reflex-local recovery budgets, scoped food
        negatives, cycle history and pending intent are expired -- only the
        episode's committed inventory observations legitimately survive an
        arrival.
        """
        # An instance change terminates the held destination *before* the scope
        # is cleared (plan §3): a terminal with a stable reason is emitted
        # exactly once, so a default destination cannot silently vanish.
        held = self.targets.held()
        if held is not None:
            self._emit_destination_terminal(
                lifecycle_metrics.DEST_EXPIRED, "instance_change", held.serial,
                purpose=held.purpose, source=held.source,
                generation=held.generation)
        self.instance_id = int(iid or 0)
        self.recovery = recovery.RecoveryState()
        self.food = recovery.FoodNegatives()
        # A fresh level-instance scope clears the scoped failed-edge ledger and
        # the prompt-declined edge ledger, and discards any pending prompt
        # context bound to the old instance (prompt-edge plan §C/§D).
        self.blocked_edges = {}
        self.prompt_declined_edges = {}
        self.clear_pending_prompt()
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
        self._pickup_offered = set()
        self._directive_first_action = set()
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
        """Disengage at low HP with the classified, edge-legal emergency view.

        The emergency branch keeps its precedence (it is chosen before every
        other command candidate), but its movement *selection and routing* now
        use the classified :class:`navigation.TerrainMemory`,
        :func:`navigation.edge_legal` and the **same immutable blocked-edge
        view** ordinary planning uses, with the exact prompt-edge-plan §E
        fallback order: (1) a legal emergency edge not suppressed for the
        emergency class, preferred retreat first; (2) another legal emergency
        edge; (3) a sole geometrically legal edge suppressed only by a
        normal-navigation prompt record; (4) stairs; (5) safe rest; (6) search.
        """
        terrain = self._terrain(mem)
        threats = self._adjacent_monsters(mem, hero)
        away = {(-int(dx), -int(dy)) for dx, dy in threats}
        step = self._emergency_move(mem, terrain, hero, threats)
        if step is not None:
            why = ("low HP: retreat" if step in away else "low HP: sidestep")
            return {"key": KEY.DIR_KEYS[step]}, why
        if hero in mem.stairs_up:
            return {"key": ord("<")}, "low HP: withdraw upstairs"
        target = self._nearest(mem.stairs_up, hero)
        if target is not None:
            step = self._emergency_step_toward(mem, terrain, hero, target)
            if step is not None:
                return {"key": KEY.DIR_KEYS[step]}, "low HP: flee upstairs"
        if self._safe_to_rest(mem, mem.status, hero):
            return {"key": KEY.KEY_WAIT}, "low HP: hold position"
        return {"key": KEY.KEY_SEARCH}, "low HP: search for an exit"

    def _emergency_move(self, mem, terrain, hero, threats):
        """The best legal emergency-class step, or ``None`` (§E fallback 1-3)."""
        away = {(-int(dx), -int(dy)) for dx, dy in threats}
        geometric = []
        options = []
        for step in navigation.DIRECTIONS:
            dest = (hero[0] + step[0], hero[1] + step[1])
            if dest == hero:
                continue
            if not navigation.edge_legal(terrain, hero, dest):
                continue
            if state.monster_cell(mem.tile(dest), hero, dest):
                continue
            geometric.append((step, dest))
            if self.edge_suppressed(terrain, hero, dest,
                                    navigation.ACTION_EMERGENCY):
                continue
            options.append((0 if step in away else 1, mem.visits.get(dest, 0),
                            navigation.DIR_RANK[step], step))
        if options:
            options.sort()
            return options[0][3]
        # Step 3: a *sole* geometrically legal edge suppressed **only** by a
        # normal-navigation prompt record remains eligible as an emergency-class
        # move, so a declined normal edge can never trap the hero.  An
        # emergency-class record (or a recovery zero-time failure) is never
        # overridden here: step 3 requires the suppression to be normal-only,
        # so a previously emergency-declined edge is never retried and the
        # policy proceeds to stairs/rest/search instead.
        if len(geometric) == 1:
            step, dest = geometric[0]
            normal_only = (
                self._prompt_edge_suppressed(terrain, hero, dest,
                                             navigation.ACTION_NORMAL)
                and not self._prompt_edge_suppressed(
                    terrain, hero, dest, navigation.ACTION_EMERGENCY)
                and not self._edge_blocked(terrain, hero, dest))
            if normal_only:
                return step
        return None

    def _emergency_step_toward(self, mem, terrain, hero, target):
        """The first filtered step toward *target*, or ``None`` (§E step 4)."""
        plan = navigation.plan(
            terrain, hero, mem.visits, None, self._check_deadline,
            edge_admissible=self._edge_admissible(terrain,
                                                  navigation.ACTION_EMERGENCY),
            action_class=navigation.ACTION_EMERGENCY)
        target = tuple(target)
        if target in plan.dist:
            return plan.first.get(target)
        return None

    def _safe_to_rest(self, mem, st, hero) -> bool:
        if self._hungry(st):
            return False
        if not self._adequate_hp(st):
            return False
        return not self._adjacent_monsters(mem, hero)

    # NOTE: the legacy raw-grid ``_unblock`` (frontier target + first step) and
    # ``_frontier_target`` helpers were removed by the stall-recovery plan §1:
    # they used raw ``state.passable`` (which permits a locked ``+``) and no
    # longer had any live caller.  Every stationary threshold and cycle
    # recovery now route through :meth:`_bounded_recovery`.

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
        terrain = self._terrain(mem)
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        for d in dirs:
            dest = (hero[0] + d[0], hero[1] + d[1])
            # the classified edge legality helper (plan §1): never a raw
            # passability check, so a closed door or an illegal diagonal can
            # no longer be chosen
            if not navigation.edge_legal(terrain, hero, dest):
                continue
            # a scoped failure suppresses the edge under unchanged evidence too
            if self.edge_suppressed(terrain, hero, dest,
                                    navigation.ACTION_NORMAL):
                continue
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

    # NOTE: the legacy raw-grid ``_first_step`` Dijkstra was removed by the
    # prompt-edge plan §E: emergency flee-upstairs routing now goes through the
    # classified terrain, ``navigation.edge_legal`` and the same immutable
    # blocked-edge view ordinary planning uses
    # (:meth:`_emergency_step_toward`), so it is no longer a second, unfiltered
    # grid search.

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

