"""Bounded recovery, refusal fingerprints and scoped negatives (wave 4).

This is the pure heart of the plan's section 5: the bounded ordinary recovery
budgets (5.1), the exact public search-refusal recognizer (5.1) and the scoped
food negatives (5.2).  Everything here is derived from public messages and
positions and is deterministic -- no wall clock, no RNG.

The rule it exists to enforce: an equivalent-action loop must be bounded.  A
refused ordinary search at a site suppresses the next ordinary ``s`` there,
and recovery escalates through a deterministic ladder instead of searching
forever.  The dangerous ``m``-prefix forced search of section 5.3 is
deliberately **not** implemented here (wave 5).

It imports the standard library only, so any layer may depend on it.
"""

import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

# -- exact refusal recognizer (plan 5.1) -----------------------------------

#: The only current public ``cmd_safety_prevention`` refusals for a sent
#: ordinary search (src/do.c:2333-2353, src/detect.c:2095-2103).  The
#: ``act`` text is followed by the engine's optional ``"  Use 'm' prefix to
#: force another search."`` suffix.  Generic "found a monster", farlook text,
#: quoted/history text and unrelated messages are deliberately NOT matched.
_REFUSAL_MONSTER = "you already found a monster."
_REFUSAL_SUFFIX = "use 'm' prefix to force another search."
_REFUSAL_DANGER = "searching doesn't feel like a good idea right now."

_WS = re.compile(r"\s+")


def normalize_message(text: str) -> str:
    """Lowercase and collapse whitespace for exact matching."""
    return _WS.sub(" ", (text or "").strip().lower())


def is_search_refusal(text: str) -> Optional[str]:
    """Classify a current public search refusal, or ``None`` (5.1).

    Returns ``"monster"`` for the ``You already found a monster.`` form (with
    or without the engine's ``Use 'm' prefix`` suffix) and ``"danger"`` for
    the ``Searching doesn't feel like a good idea right now.`` variant.  A
    message that merely contains "found a monster" is not a refusal.
    """
    norm = normalize_message(text)
    if not norm:
        return None
    if norm == _REFUSAL_MONSTER:
        return "monster"
    if norm == _REFUSAL_MONSTER + " " + _REFUSAL_SUFFIX:
        return "monster"
    if norm.startswith(_REFUSAL_MONSTER + " " + _REFUSAL_SUFFIX):
        return "monster"
    if norm == _REFUSAL_DANGER:
        return "danger"
    return None


def refusal_in(messages: Sequence[str]) -> Optional[str]:
    """The first recognised refusal among *messages*, or ``None``."""
    for m in messages:
        kind = is_search_refusal(m)
        if kind is not None:
            return kind
    return None


# -- food negatives (plan 5.2) ---------------------------------------------

#: The two engine message forms.  ``anything else`` is the floor-food-declined
#: variant (src/eat.c:3595-3598, getobj_else), ``anything to`` the plain one.
_FOOD_INV_RE = re.compile(r"you don't have anything to eat")
_FOOD_LOC_RE = re.compile(r"you don't have anything else to eat")

FOOD_NEG_INVENTORY = "inventory"
FOOD_NEG_LOCATION = "location"


def classify_food_negative(text: str) -> Optional[str]:
    """Recognise a matched eat outcome as inventory- or location-negative.

    Only these two exact phrases count; a substring a look or a quote happens
    to contain is not an eat outcome.
    """
    norm = normalize_message(text)
    if _FOOD_LOC_RE.search(norm):
        return FOOD_NEG_LOCATION
    if _FOOD_INV_RE.search(norm):
        return FOOD_NEG_INVENTORY
    return None


class FoodNegatives(object):
    """Scoped inventory- and location-negative food evidence (5.2).

    Inventory-negative is tied to the inventory *signature* (it is re-assessed
    when the signature changes); location-negative is tied to
    ``(instance, confirmed_position, floor_revision)`` so it never becomes a
    global "there is no food anywhere" claim.  A fresh instance clears the
    location evidence; retaining inventory observations across levels means an
    unchanged signature stays negative, but clearing a location suppression is
    not proof that food appeared.
    """

    def __init__(self) -> None:
        self.inventory_signature: Optional[tuple] = None
        self.locations: Set[Tuple[int, Tuple[int, int], int]] = set()

    def note_inventory_negative(self, signature: Optional[tuple]) -> None:
        self.inventory_signature = signature

    def note_location_negative(self, instance: int, pos: Tuple[int, int],
                               floor_revision: int) -> None:
        self.locations.add((int(instance), tuple(pos), int(floor_revision)))

    def inventory_negative(self, signature: Optional[tuple]) -> bool:
        """True only while the *same* inventory signature stays negative."""
        return (self.inventory_signature is not None
                and signature is not None
                and tuple(signature) == tuple(self.inventory_signature))

    def location_negative(self, instance: int, pos: Tuple[int, int],
                          floor_revision: int) -> bool:
        return (int(instance), tuple(pos), int(floor_revision)) \
            in self.locations

    def invalidate_instance(self, instance: int) -> None:
        """A fresh instance expires old-instance location negatives (6)."""
        self.locations = {loc for loc in self.locations
                          if loc[0] == instance}


# -- bounded ordinary search budgets (plan 5.1) ----------------------------

#: Three completed ordinary searches per plausible site/topology signature;
#: one refused/equivalent no-time search suppresses the site immediately.
SEARCH_SITE_LIMIT = 3

#: The per-site budget for **no-time** recovery searches (plan §1/§2A).
#:
#: A search that reconciles as ``no-time`` advanced nothing: the hero is stuck
#: (held/paralysed by an adjacent monster, say), so it is neither a *completed*
#: search (the ordinary budget only spends on a time-advancing outcome) nor a
#: recognised *refusal*.  Without its own bound the ladder would nominate ``s``
#: forever.  Two zero-time recovery searches at one stationary site exhaust it,
#: and the ladder then escalates exactly per the stall plan (forced-search
#: nomination -> controller gates -> trapped graceful quit).
SEARCH_NO_TIME_LIMIT = 2


class SearchBudget(object):
    """Per-site completed/**no-time**/rejected ordinary-search accounting.

    A *site* is a deterministic key (e.g. hero position).  The ordinary budget
    counts observed outcomes: a local validation or write failure must not
    consume it, so only a completed or refused search is recorded.  A separate
    **no-time** budget (plan §1/§2A) bounds a zero-time recovery search, which
    is a matched gameplay attempt that made no progress.
    """

    def __init__(self, limit: int = SEARCH_SITE_LIMIT,
                 no_time_limit: int = SEARCH_NO_TIME_LIMIT) -> None:
        self.limit = int(limit)
        self.no_time_limit = int(no_time_limit)
        self.completed: Dict[object, int] = {}
        self.no_time: Dict[object, int] = {}
        #: site -> the recognised refusal kind observed by that site's own
        #: search (plan §4 site-correlated binding)
        self.refused: Dict[object, str] = {}

    def note_completed(self, site) -> None:
        self.completed[site] = self.completed.get(site, 0) + 1

    def note_no_time(self, site) -> None:
        """Record one *zero-time* recovery search attempt at *site*."""
        self.no_time[site] = self.no_time.get(site, 0) + 1

    def note_refused(self, site, kind: str) -> None:
        """Record a refusal newly observed by this site's own search."""
        self.refused[site] = kind

    def refusal_kind_for(self, site):
        """The recognised refusal kind recorded for *site*, or ``None``."""
        return self.refused.get(site)

    def no_time_count(self, site) -> int:
        return self.no_time.get(site, 0)

    def allows(self, site) -> bool:
        """True while a justified ordinary search is still bounded here."""
        if site in self.refused:
            return False
        if self.no_time.get(site, 0) >= self.no_time_limit:
            # zero-time recovery searches are exhausted at this site
            return False
        return self.completed.get(site, 0) < self.limit

    def revision_changed(self, site) -> None:
        """A relevant site change reopens the budget (4.3)."""
        self.completed.pop(site, None)
        self.no_time.pop(site, None)
        self.refused.pop(site, None)

    def reset(self) -> None:
        self.completed.clear()
        self.no_time.clear()
        self.refused.clear()


# -- deterministic cycle detection (plan 5.1) ------------------------------

class CycleDetector(object):
    """Detect an A<->B oscillation (period 2) or an A-B-C cycle (period 3).

    Deterministic and bounded: it keeps a short trailing window of confirmed
    hero positions and reports a cycle when the trailing window is exactly a
    repetition of a shorter block.  No RNG, no wall clock.  Cycle recovery
    invalidates the current target so a stale destination cannot drive it.

    The trailing window is **deduplicated**: a repeated confirmation equal to
    the last retained position adds no movement and is skipped, so a stationary
    frame cannot dilute or postpone a genuine oscillation.  A ``None`` position
    (unknown hero) breaks continuity and clears the window.
    """

    def __init__(self, window: int = 6) -> None:
        self.window = int(window)
        self.history: List[Tuple[int, int]] = []
        self.cycles = 0

    def observe(self, pos: Optional[Tuple[int, int]]) -> bool:
        """Fold one confirmed position; True on a 2- or 3-cycle."""
        if pos is None:
            self.history = []
            return False
        pos = tuple(pos)
        if self.history and self.history[-1] == pos:
            # a duplicate confirmation is not new movement: it neither breaks
            # nor advances the trailing movement history
            return False
        self.history.append(pos)
        if len(self.history) > self.window:
            del self.history[:len(self.history) - self.window]
        for period in (2, 3):
            if len(self.history) >= 2 * period:
                tail = self.history[-period:]
                prev = self.history[-2 * period:-period]
                # a genuine oscillation needs at least two distinct squares;
                # a stationary hero is not a cycle
                if tail == prev and len(set(tail)) > 1:
                    self.cycles += 1
                    return True
        return False

    def reset(self) -> None:
        self.history = []


def chebyshev(a, b) -> int:
    """The Chebyshev distance between two grid cells."""
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


# -- recovery state (the wave-4 integration surface) -----------------------

class RecoveryState(object):
    """The reflex-local bounded recovery bookkeeping.

    Combines the search budget, the cycle detector, a "search refused at this
    site" flag and an explicit **movement-history state machine** over the
    confirmed positions, so a decision can suppress a repeated ordinary ``s``,
    prefer not to immediately backtrack, and escalate per the ladder:
    justified search -> deterministic safe alternative step -> invalidate/
    reselect target -> graceful termination.

    Movement history (plan section 4):

    * ``current`` is the last confirmed position;
    * ``previous_distinct`` is the last confirmed position distinct from
      ``current`` (the anti-backtrack reference);
    * the trailing movement history (``cycle.history``) is the deduplicated
      window used for period-2/period-3 detection;
    * ``cycle_active`` is whether that trailing movement is currently a
      detected period-2/period-3 cycle.

    The four transitions are: an *identical* confirmation preserves everything;
    an *adjacent distinct* move appends and recomputes (clearing
    ``cycle_active`` only when off-cycle); an *unknown or non-adjacent*
    relocation clears the history, the previous-cell evidence and
    ``cycle_active``; a fresh instance is a new object.
    """

    def __init__(self) -> None:
        self.search = SearchBudget()
        self.cycle = CycleDetector()
        self.refused_site: Optional[object] = None
        self.current: Optional[Tuple[int, int]] = None
        self.previous_distinct: Optional[Tuple[int, int]] = None
        self.cycle_active = False

    def observe(self, messages: Sequence[str], pos: Optional[Tuple[int, int]],
                site) -> None:
        """Fold the confirmed position into the state.

        Refusal attribution deliberately does NOT happen here: a refusal may
        only be recorded by the reconciliation fold of the search whose own
        newly observed messages contain it (plan §4 site-correlated binding).
        """

    def allows_search(self, site) -> bool:
        return self.search.allows(site)

    def note_search_completed(self, site) -> None:
        self.search.note_completed(site)

    def note_no_time_search(self, site) -> None:
        """Record one zero-time recovery search attempt at *site* (plan §2A)."""
        self.search.note_no_time(site)

    def note_refused(self, site, kind: str) -> None:
        """Record a site-correlated search refusal (plan §4)."""
        self.search.note_refused(site, kind)

    def refusal_kind_for(self, site):
        """The recognised refusal kind recorded for *site*, or ``None``."""
        return self.search.refusal_kind_for(site)

    def no_time_searches(self, site) -> int:
        """How many zero-time recovery searches this site has spent."""
        return self.search.no_time_count(site)

    def movement_history(self) -> Tuple[Tuple[int, int], ...]:
        """The deduplicated trailing confirmed-movement window."""
        return tuple(self.cycle.history)

    def _clear_movement(self) -> None:
        self.current = None
        self.previous_distinct = None
        self.cycle.reset()
        self.cycle_active = False

    def note_cycle(self, pos: Optional[Tuple[int, int]]) -> bool:
        """Fold one confirmed position; return whether a cycle is active.

        Only a *confirmed* observation reaches here (the controller calls it
        once per committed snapshot), so a proposal, a rejected candidate or a
        failed write can never fabricate motion.
        """
        self.cycle_active = self._fold_movement(pos)
        return self.cycle_active

    def _fold_movement(self, pos: Optional[Tuple[int, int]]) -> bool:
        if pos is None:
            # unknown hero: no continuity, and no false cycle
            self._clear_movement()
            return False
        pos = tuple(pos)
        if self.current is not None and pos == self.current:
            # identical confirmation: preserve the previous cell, the trailing
            # history and the active-cycle state unchanged
            return self.cycle_active
        if self.current is None:
            # first confirmed position: seeds the history with no predecessor
            self.previous_distinct = None
            self.current = pos
            self.cycle.reset()
            self.cycle_active = self.cycle.observe(pos)
            return self.cycle_active
        if chebyshev(pos, self.current) > 1:
            # an unknown-continuity relocation (teleport, level change without
            # an instance transition): every stale cell reference is invalid
            self._clear_movement()
            self.current = pos
            self.cycle_active = self.cycle.observe(pos)
            return self.cycle_active
        # a genuine adjacent move: it becomes the new anti-backtrack reference
        self.previous_distinct = self.current
        self.current = pos
        self.cycle_active = self.cycle.observe(pos)
        return self.cycle_active


__all__ = [
    "normalize_message", "is_search_refusal", "refusal_in",
    "classify_food_negative", "FoodNegatives",
    "FOOD_NEG_INVENTORY", "FOOD_NEG_LOCATION",
    "SEARCH_SITE_LIMIT", "SEARCH_NO_TIME_LIMIT", "SearchBudget", "CycleDetector",
    "RecoveryState",
    "chebyshev",
]
