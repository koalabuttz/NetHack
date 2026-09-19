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

from . import candidates, forced_search, navigation, protocol, recovery, state
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
                           "refresh-inventory-menu")

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
              direction=(), direction_rank=0):
        """Build one content-addressed candidate with its semantic label."""
        return candidates.make_candidate(
            action, label, family=family, score=score, reason=reason,
            proposed_effect=effect, direction=direction,
            direction_rank=direction_rank)

    def _navigation_candidates(self, context, mem, hero):
        """One-Dijkstra navigation candidates over all reachable targets.

        This replaces the old priority-return navigation: a single Dijkstra
        enumerates every reachable down stair, closed-door approach, frontier
        and unvisited cell, and the retained argmax picks the first step.  A
        directive only reorders candidates the reflex already knows how to
        build; it never supplies a key.

        Before candidates are constructed, the scored targets pass through the
        bounded same-family anti-backtrack preference (:meth:`_antibacktrack`),
        which suppresses a *comparable* immediate reversal without ever
        forbidding an edge or dropping a uniquely required retreat.
        """
        explore_first = self.directives.prefers_frontier() \
            and not self.directives.prefers_stairs()
        if hero in mem.stairs_down and not explore_first:
            return (self._cand({"key": ord(">")}, "descend", "descend", 900,
                               "descend the known stairs", "descend"),)
        terrain = self._terrain(mem)
        plan = navigation.plan(terrain, hero, mem.visits, None,
                               self._check_deadline)
        scored = []
        for target in plan.targets:
            key = KEY.DIR_KEYS[target.first_step]
            score = _target_score(target.family, target.cost, explore_first)
            score += self._directive_component(target)
            scored.append((target, _NAV_FAMILY[target.family], key, score,
                           target.first_step))
        kept = self._antibacktrack(scored, hero, getattr(context, "rejected",
                                                         None))
        cands = []
        for target, family, key, score, step, extra in kept:
            cands.append(self._cand(
                {"key": key}, "navigate", family, score,
                "%s: %s" % (self._nav_reason(extra), target.reason), "navigate",
                direction=step, direction_rank=navigation.DIR_RANK[step]))
        if cands:
            return tuple(cands)
        if mem.searches_since_progress < 3 \
                and self._allows_search(mem, hero) \
                and not self._cycled:
            return (self._cand({"key": KEY.KEY_SEARCH}, "search-secret",
                               "secret-search", 300,
                               "search for secret doors",
                               "secret-search"),)
        return self._search_fallback(mem, hero)

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
        instance = getattr(mem, "instance", None)
        if instance is None:
            instance = self.instance_id
        for text in recent:
            kind = recovery.classify_food_negative(text)
            if kind == recovery.FOOD_NEG_INVENTORY:
                self.food.note_inventory_negative(mem.inventory_signature())
            elif kind == recovery.FOOD_NEG_LOCATION and hero is not None:
                self.food.note_location_negative(instance or 0, hero, 0)

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

    def _terrain(self, mem):
        """Build one classified terrain view from remembered raw cells."""
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

