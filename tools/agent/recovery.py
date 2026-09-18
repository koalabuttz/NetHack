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


class SearchBudget(object):
    """Per-site completed/rejected ordinary-search accounting (5.1).

    A *site* is a deterministic key (e.g. hero position plus map revision).
    The budget counts observed outcomes: a local validation or write failure
    must not consume it, so only a completed or refused search is recorded.
    """

    def __init__(self, limit: int = SEARCH_SITE_LIMIT) -> None:
        self.limit = int(limit)
        self.completed: Dict[object, int] = {}
        self.refused: Set[object] = set()

    def note_completed(self, site) -> None:
        self.completed[site] = self.completed.get(site, 0) + 1

    def note_refused(self, site) -> None:
        self.refused.add(site)

    def allows(self, site) -> bool:
        """True while a justified ordinary search is still bounded here."""
        if site in self.refused:
            return False
        return self.completed.get(site, 0) < self.limit

    def revision_changed(self, site) -> None:
        """A relevant site change reopens the budget (4.3)."""
        self.completed.pop(site, None)
        self.refused.discard(site)

    def reset(self) -> None:
        self.completed.clear()
        self.refused.clear()


# -- deterministic cycle detection (plan 5.1) ------------------------------

class CycleDetector(object):
    """Detect an A<->B oscillation (period 2) or an A-B-C cycle (period 3).

    Deterministic and bounded: it keeps a short trailing window of confirmed
    hero positions and reports a cycle when the trailing window is exactly a
    repetition of a shorter block.  No RNG, no wall clock.  Cycle recovery
    invalidates the current target so a stale destination cannot drive it.
    """

    def __init__(self, window: int = 6) -> None:
        self.window = int(window)
        self.history: List[Tuple[int, int]] = []
        self.cycles = 0

    def observe(self, pos: Optional[Tuple[int, int]]) -> bool:
        """Fold one confirmed position; True on a 2- or 3-cycle."""
        if pos is None:
            return False
        pos = tuple(pos)
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


# -- recovery state (the wave-4 integration surface) -----------------------

class RecoveryState(object):
    """The reflex-local bounded recovery bookkeeping.

    Combines the search budget, the cycle detector and a "search refused at
    this site" flag so a decision can suppress a repeated ordinary ``s`` and
    escalate per the ladder: justified search -> deterministic safe
    alternative step -> invalidate/reselect target -> graceful termination.
    """

    def __init__(self) -> None:
        self.search = SearchBudget()
        self.cycle = CycleDetector()
        self.refused_site: Optional[object] = None

    def observe(self, messages: Sequence[str], pos: Optional[Tuple[int, int]],
                site) -> None:
        """Fold public messages and the confirmed position into the state."""
        if refusal_in(messages) is not None:
            self.refused_site = site
            self.search.note_refused(site)

    def allows_search(self, site) -> bool:
        return self.search.allows(site)

    def note_search_completed(self, site) -> None:
        self.search.note_completed(site)

    def note_cycle(self, pos: Optional[Tuple[int, int]]) -> bool:
        return self.cycle.observe(pos)


__all__ = [
    "normalize_message", "is_search_refusal", "refusal_in",
    "classify_food_negative", "FoodNegatives",
    "FOOD_NEG_INVENTORY", "FOOD_NEG_LOCATION",
    "SEARCH_SITE_LIMIT", "SearchBudget", "CycleDetector", "RecoveryState",
]
