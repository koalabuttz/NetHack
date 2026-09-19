"""Floor-item evidence, bounded pickup attempts and pickup-menu filtering.

This is the pure heart of the situational-pickup design
(``doc/agent-destination-commitment-plan.md`` section 3).  It imports the
standard library and :mod:`tools.agent.instances` only -- no engine, no
provider, no wire -- so the reflex may depend on it without a cycle.

Three responsibilities:

  * :class:`FloorEvidence` / :class:`FloorLedger` -- the instance-scoped floor
    ledger.  A *source epoch* is created or advanced only by an observation
    that establishes or **materially refreshes** the item evidence; the hero
    overlay, time, inventory refresh, unrelated observations and the global map
    revision must never reset it (section 3.2), so the two-attempt bound cannot
    be reset by them either.
  * the narrow scripted **urgent-food fallback** rule (section 3.1): only a
    hungry hero with no usable cached food and a fresh location-bound exact
    recognized ration name may acquire food reflexively -- a `%` glyph alone
    never establishes edible/safe food.
  * the **pickup-menu row model** (sections 3.3, 3.5): an appearance authorizes
    at most inspection; the intent selects a *uniquely authorized exact* row,
    cancels a broad or ambiguous pile rather than model-choosing, and never
    selects a row explicitly marked unpaid or answers a capacity/burden prompt.
"""

from dataclasses import dataclass, replace
from typing import Dict, Iterable, Optional, Sequence, Tuple

#: At most two fully sent and reconciled pickup initiations per unchanged
#: evidence token (section 3.3).  Prompt continuations are not initiations.
MAX_PICKUP_ATTEMPTS = 2

#: An appearance authorizes at most inspection/acquisition consideration.
APPEARANCE_AUTHORIZES = "inspect-only"

#: The model-facing wording for a recognized food appearance (section 3.2).
RECOGNIZED_FOOD_WORDING = "Recognized food; no safety guarantee"

# Outcome vocabulary (section 3.3/3.5).
OUTCOME_SUCCESS = "success"
OUTCOME_NO_ITEMS = "no-items"
OUTCOME_REFUSED = "refused"
OUTCOME_CANCELED = "canceled"
OUTCOME_DECLINED = "declined"
OUTCOME_EXHAUSTED = "exhausted"
OUTCOME_UNKNOWN = "unknown"

#: Outcomes that terminate a target/directive-owned pickup destination.
TERMINAL_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_NO_ITEMS, OUTCOME_REFUSED,
                     OUTCOME_CANCELED, OUTCOME_EXHAUSTED)

#: Prompt fragments marking an explicitly unpaid row (never selected).
_UNPAID_MARKERS = ("unpaid",)
#: Prompt fragments marking a capacity/encumbrance question (always declined).
_CAPACITY_MARKERS = ("burden", "carry", "capacity", "stressed",
                     "overloaded", "encumber")


@dataclass(frozen=True)
class FloorEvidence:
    """One floor-item evidence token bound to its instance and source epoch."""

    instance: int
    pos: Tuple[int, int]
    appearance: str
    source_epoch: int
    count: Optional[int] = None
    #: True when the glyph is hidden by the hero overlay and this is retained
    #: last-seen evidence rather than a fresh observation.
    last_seen: bool = False
    #: A previously observed recognized ration name bound to this location.
    ration_name: str = ""

    @property
    def token(self) -> tuple:
        return (self.instance, tuple(self.pos), self.source_epoch)


class FloorLedger(object):
    """Instance-scoped floor evidence, attempts, declines and negatives."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._groups: Dict[tuple, FloorEvidence] = {}
        self._attempts: Dict[tuple, int] = {}
        self._declined: Dict[tuple, bool] = {}
        self._negatives: Dict[tuple, str] = {}
        self._outcomes: Dict[tuple, str] = {}
        self._epoch = 0

    # -- observations ----------------------------------------------------
    def observe_item(self, instance: int, pos: Tuple[int, int],
                     appearance: str, count: Optional[int] = None
                     ) -> FloorEvidence:
        """Record a *displayed* item, refreshing the epoch only on a material
        change (a new appearance, a changed count, or a new instance)."""
        pos = tuple(pos)
        cur = self._groups.get(pos)
        material = (cur is None or cur.instance != instance
                    or cur.appearance != appearance or cur.count != count)
        if material:
            self._epoch += 1
            ev = FloorEvidence(instance=instance, pos=pos,
                               appearance=appearance, source_epoch=self._epoch,
                               count=count)
        else:
            ev = replace(cur, last_seen=False)
        self._groups[pos] = ev
        return ev

    def retain_on_arrival(self, instance: int, pos: Tuple[int, int]
                          ) -> Optional[FloorEvidence]:
        """The hero overlay: keep the last-seen evidence with its *unchanged*
        source epoch (a move onto the item must not refresh the token)."""
        pos = tuple(pos)
        cur = self._groups.get(pos)
        if cur is None or cur.instance != instance:
            return None
        ev = replace(cur, last_seen=True)
        self._groups[pos] = ev
        return ev

    def bind_ration_name(self, instance: int, pos: Tuple[int, int],
                         ration_name: str, hero_pos: Optional[Tuple[int, int]]
                         ) -> Optional[FloorEvidence]:
        """Bind a location-bound recognized ration name only at the hero's own
        current square, with the *unchanged* source epoch."""
        pos = tuple(pos)
        cur = self._groups.get(pos)
        if cur is None or cur.instance != instance:
            return None
        if hero_pos is None or tuple(hero_pos) != pos:
            return None                       # stale message cannot bind
        ev = replace(cur, ration_name=ration_name)
        self._groups[pos] = ev
        return ev

    def evidence(self, pos: Tuple[int, int]) -> Optional[FloorEvidence]:
        return self._groups.get(tuple(pos))

    def evidence_positions(self) -> Tuple[Tuple[int, int], ...]:
        """Every position with current floor evidence (deterministic order)."""
        return tuple(sorted(self._groups))

    # -- attempts --------------------------------------------------------
    def attempts(self, ev: FloorEvidence) -> int:
        return self._attempts.get(ev.token, 0)

    def budget_available(self, ev: FloorEvidence) -> bool:
        return self.attempts(ev) < MAX_PICKUP_ATTEMPTS

    def note_initiation(self, ev: FloorEvidence) -> int:
        """Count one fully sent and reconciled pickup initiation."""
        n = self._attempts.get(ev.token, 0) + 1
        self._attempts[ev.token] = n
        return n

    def note_declined(self, ev: FloorEvidence) -> None:
        self._declined[ev.token] = True

    def declined(self, ev: FloorEvidence) -> bool:
        return self._declined.get(ev.token, False)

    def note_negative(self, instance: int, pos: Tuple[int, int],
                      reason: str) -> None:
        self._negatives[(int(instance), tuple(pos))] = reason

    def negative(self, instance: int, pos: Tuple[int, int]) -> Optional[str]:
        return self._negatives.get((int(instance), tuple(pos)))

    def note_outcome(self, ev: FloorEvidence, outcome: str) -> None:
        """Record the classified outcome of a reconciled pickup attempt."""
        self._outcomes[ev.token] = outcome
        if terminates_target(outcome):
            # a terminal outcome also closes the site's acquisition
            self._declined[ev.token] = True

    def outcome(self, ev: FloorEvidence) -> Optional[str]:
        return self._outcomes.get(ev.token)

    def expire_instance(self, instance: int) -> None:
        for key in [k for k in self._groups if k[0] != int(instance)]:
            del self._groups[key]


# -- trust rules -----------------------------------------------------------

def appearance_authorizes_inspection(ev: Optional[FloorEvidence]) -> bool:
    """An appearance authorizes at most inspection/acquisition consideration."""
    return ev is not None


def appearance_proves_safety(ev: Optional[FloorEvidence]) -> bool:
    """An appearance NEVER proves BUC, safety, ownership or exact type."""
    return False


def urgent_food_fallback(*, hungry: bool, usable_cached_food: bool,
                         exact_ration_name: str = "") -> bool:
    """The narrow scripted urgent-food rule (section 3.1).

    True only when the hero is hungry, no usable cached food can serve the
    need, and a fresh location-bound *exact recognized ration name* supports
    acquisition.  A `%` glyph alone is not sufficient.
    """
    return bool(hungry and not usable_cached_food and exact_ration_name)


def terminates_target(outcome: str) -> bool:
    """True when an outcome terminates the pickup target/directive."""
    return outcome in TERMINAL_OUTCOMES


# -- pickup-menu row model -------------------------------------------------

@dataclass(frozen=True)
class PickupRow:
    """One pickup-menu row: native index, text, selectable, optional count."""

    index: int
    text: str
    selectable: bool = True
    count: Optional[int] = None


def parse_rows(raw: Iterable[dict]) -> Tuple[PickupRow, ...]:
    out = []
    for row in raw:
        out.append(PickupRow(
            index=int(row.get("r")), text=str(row.get("text") or ""),
            selectable=bool(row.get("selectable")),
            count=None if row.get("count") is None else int(row["count"])))
    return tuple(out)


def is_unpaid_row(row: PickupRow) -> bool:
    low = row.text.lower()
    return any(m in low for m in _UNPAID_MARKERS)


def is_capacity_prompt(prompt: str) -> bool:
    low = (prompt or "").lower()
    return any(m in low for m in _CAPACITY_MARKERS)


def selectable_rows(rows: Sequence[PickupRow]) -> Tuple[PickupRow, ...]:
    return tuple(r for r in rows if r.selectable and not is_unpaid_row(r))


def authorized_rows(rows: Sequence[PickupRow], pred
                    ) -> Tuple[PickupRow, ...]:
    return tuple(r for r in selectable_rows(rows) if pred(r.text))


def unique_authorized_row(rows: Sequence[PickupRow], pred
                          ) -> Tuple[Optional[PickupRow], str]:
    """The single authorized row, or ``(None, reason)`` (unique or cancel)."""
    matches = authorized_rows(rows, pred)
    if not matches:
        return None, "no authorized row"
    if len(matches) > 1:
        return None, "ambiguous pile: %d authorized rows" % len(matches)
    return matches[0], "unique authorized row"


def menu_decision(rows: Sequence[PickupRow], *, purpose: str = "opportunistic",
                  urgent_food: bool = False,
                  bound_row_index: Optional[int] = None
                  ) -> Tuple[str, Optional[PickupRow], str]:
    """The conservative menu decision (``select``/``cancel``, row, reason).

    A targeted ``collect_items`` pickup selects the **single bound row** (a
    uniquely authorized exact row); an urgent-food pickup selects the uniquely
    authorized exact recognized food row; an explicit ``bound_row_index``
    binds that one row.  Anything broad or ambiguous is cancelled rather than
    model-chosen, and a row explicitly marked unpaid is never selected.
    """
    if bound_row_index is not None:
        row = next((r for r in selectable_rows(rows)
                    if r.index == bound_row_index), None)
        if row is None:
            return "cancel", None, "the bound row is not selectable"
        return "select", row, "the bound authorized row"
    if purpose == "collect":
        candidates = selectable_rows(rows)
        if len(candidates) == 1:
            return "select", candidates[0], "the single bound row"
        if not candidates:
            return "cancel", None, "no authorized row"
        return "cancel", None, ("ambiguous pile: %d authorized rows"
                                % len(candidates))
    if urgent_food:
        row, why = unique_authorized_row(rows, is_recognized_food_text)
        if row is None:
            return "cancel", None, why
        return "select", row, why
    return "cancel", None, "no authorization for this pile"


def ration_name_in(text: str) -> str:
    """The recognized ration name a location-bound floor message names.

    Only a current-location floor observation ("You see here ...") names an
    item at the hero's square; any other message yields ``""`` so a stale or
    unrelated line can never bind a ration to the current location.
    """
    low = (text or "").lower()
    if "you see here" not in low:
        return ""
    for marker in _FOOD_MARKERS:
        if marker.lower() in low:
            return marker
    return ""


#: Recognized-food row text fragments (an appearance, not a safety claim).
_FOOD_MARKERS = ("food ration", "cram ration", "lembas wafer", "fortune cookie",
                 "apple", "banana", "orange", "carrot", "pear", "melon",
                 "slime mold", "kelp frond", "eucalyptus leaf", "meatball",
                 "meat stick", "cream pie", "candy bar", "lump of royal jelly",
                 "K-ration", "C-ration")


def is_recognized_food_text(text: str) -> bool:
    """True when a row's displayed text names a recognized food.

    This is an *appearance* match: it never asserts the item is safe to eat.
    """
    low = (text or "").lower()
    return any(m.lower() in low for m in _FOOD_MARKERS)


def single_entry_autoselect_uncertainty() -> dict:
    """The acknowledged residual uncertainty for ``AUTOSELECT_SINGLE``.

    A lone object entry/stack on the hero's square can be auto-selected at its
    full quantity with no menu, and no public shop/ownership evidence exists
    before the command is sent, so the intent cannot guarantee declining an
    unpaid entry/stack on that path (section 3.5).  Recorded as unproven, never
    a success claim.
    """
    return {"path": "autoselect-single", "menu_observed": False,
            "evidence": OUTCOME_UNKNOWN, "claimed": False}


__all__ = [
    "MAX_PICKUP_ATTEMPTS", "APPEARANCE_AUTHORIZES", "RECOGNIZED_FOOD_WORDING",
    "OUTCOME_SUCCESS", "OUTCOME_NO_ITEMS", "OUTCOME_REFUSED",
    "OUTCOME_CANCELED", "OUTCOME_DECLINED", "OUTCOME_EXHAUSTED",
    "OUTCOME_UNKNOWN", "TERMINAL_OUTCOMES", "FloorEvidence", "FloorLedger",
    "appearance_authorizes_inspection", "appearance_proves_safety",
    "urgent_food_fallback", "terminates_target", "PickupRow", "parse_rows",
    "is_unpaid_row", "is_capacity_prompt", "selectable_rows",
    "authorized_rows", "unique_authorized_row", "menu_decision",
    "is_recognized_food_text", "ration_name_in",
    "single_entry_autoselect_uncertainty",
]
