#!/usr/bin/env python3
"""Pickup protocol-shape vocabulary and the pure pickup row model (Phase 0).

This module is the *engine-free* half of the Phase 0 native pickup-shape
infrastructure of ``doc/agent-destination-commitment-plan.md`` (section 6
Phase 0, AC17, AC18).  It holds three things and imports nothing but the
standard library:

  * :data:`SHAPES` -- the eight protocol shapes the plan names, each with a
    stable key and a human title;
  * the native-vs-manual *disposition* partition (which shapes the native
    engine-side probe proves, and which require the mandatory operator-gated
    manual probe), with :func:`validate_partition` asserting that every shape
    is dispositioned **exactly once** and that the union is exhaustive;
  * a small **pure row model** for a pickup menu (:class:`PickupRow` and the
    filtering helpers) used by ``tools.agent.pickup`` once the pickup intent
    exists and by ``test_auto_pickup.py`` now.

The row model checks only the *shape of a menu row and the local filtering
rules* (uniquely-authorized exact-row selection, unpaid-row refusal, broad
pile cancellation, capacity/burden prompt recognition).  It never claims to
verify the wire protocol shape -- that is AC17's native probe or AC18's
mandatory manual probe.
"""

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

# -- the eight protocol shapes (stable keys, exact plan titles) ------------

SHAPES: Tuple[Tuple[str, str], ...] = (
    ("no_object", "no object present"),
    ("autoselect_single",
     "single object entry/stack AUTOSELECT_SINGLE (full quantity)"),
    ("multi_row_pick_any", "multi-row PICK_ANY menu (title, mode, rows)"),
    ("cancellation", "cancellation returns to the command need"),
    ("success_inventory_delta", "success with the resulting inventory delta"),
    ("unpaid_shop_annotation", "unpaid/shop row annotation"),
    ("capacity_prompt", "capacity/burden prompt"),
    ("stale_menu_generation", "stale menu generation"),
)

SHAPE_KEYS: Tuple[str, ...] = tuple(k for k, _ in SHAPES)


def shape_title(key: str) -> str:
    for k, title in SHAPES:
        if k == key:
            return title
    raise KeyError(key)


# -- native-vs-manual disposition -----------------------------------------
#
# Phase 0 investigation result for this harness: the agent worker takes no
# fixed RNG seed and exposes no deterministic object-construction hook, so the
# level layout and its floor objects differ on every run.  The only shape that
# is *deterministically* constructible through the real adapter is the
# no-object case (a pickup attempt on a bare floor square).  Every other shape
# needs a specific floor object or pile, which cannot be produced
# deterministically here, so each is explicitly **manual-required** (AC18)
# rather than silently skipped.

NATIVE_SHAPES: Tuple[str, ...] = ("no_object",)

MANUAL_SHAPES: Tuple[str, ...] = tuple(
    k for k in SHAPE_KEYS if k not in NATIVE_SHAPES)


def validate_partition(native: Sequence[str] = NATIVE_SHAPES,
                       manual: Sequence[str] = MANUAL_SHAPES) -> None:
    """Assert the disposition is exhaustive and each shape appears once.

    Raises ``ValueError`` with the offending shape(s) when a shape is missing
    from both sets, present in both, or unknown, or when a set has a
    duplicate.
    """
    seen = list(native) + list(manual)
    for key in seen:
        if key not in SHAPE_KEYS:
            raise ValueError("unknown pickup shape %r" % (key,))
    if len(set(native)) != len(native):
        raise ValueError("duplicate shape in the native set")
    if len(set(manual)) != len(manual):
        raise ValueError("duplicate shape in the manual set")
    overlap = set(native) & set(manual)
    if overlap:
        raise ValueError("shape(s) in both sets: %s" % sorted(overlap))
    missing = set(SHAPE_KEYS) - set(seen)
    if missing:
        raise ValueError("shape(s) with no disposition: %s" % sorted(missing))


def disposition(native: Sequence[str] = NATIVE_SHAPES,
                manual: Sequence[str] = MANUAL_SHAPES) -> dict:
    """The machine-readable native-vs-manual disposition of all eight shapes."""
    validate_partition(native, manual)
    return {
        "native_passed": list(native),
        "manual_required": list(manual),
        "shapes": [{"key": k, "title": t,
                    "disposition": ("native" if k in native else "manual")}
                   for k, t in SHAPES],
    }


# -- pure pickup row model -------------------------------------------------

#: Row text fragments that mark a row the intent must never select or acquire.
_UNPAID_MARKERS = ("unpaid",)
#: Prompt fragments that mark a capacity/encumbrance question (refuse it).
_CAPACITY_MARKERS = ("burden", "carry", "capacity", "stressed",
                     "overloaded", "encumber")


@dataclass(frozen=True)
class PickupRow:
    """One pickup-menu row as the wire publishes it.

    ``index`` is the native row number (``r``); ``text`` the displayed row
    text; ``selectable`` whether the menu marks it choosable; ``count`` its
    displayed stack count when the menu carries one (else ``None``).
    """

    index: int
    text: str
    selectable: bool = True
    count: Optional[int] = None


def parse_rows(raw: Iterable[dict]) -> Tuple[PickupRow, ...]:
    """Coerce published menu row dicts into the frozen row model."""
    out = []
    for row in raw:
        out.append(PickupRow(
            index=int(row.get("r")),
            text=str(row.get("text") or ""),
            selectable=bool(row.get("selectable")),
            count=(None if row.get("count") is None else int(row["count"])),
        ))
    return tuple(out)


def is_unpaid_row(row: PickupRow) -> bool:
    """True when a row is explicitly annotated as unpaid (never selected)."""
    low = row.text.lower()
    return any(m in low for m in _UNPAID_MARKERS)


def selectable_rows(rows: Sequence[PickupRow]) -> Tuple[PickupRow, ...]:
    """Rows that are selectable and not explicitly unpaid."""
    return tuple(r for r in rows if r.selectable and not is_unpaid_row(r))


def authorized_rows(rows: Sequence[PickupRow], pred) -> Tuple[PickupRow, ...]:
    """Selectable, non-unpaid rows whose text satisfies *pred*."""
    return tuple(r for r in selectable_rows(rows) if pred(r.text))


def unique_authorized_row(rows: Sequence[PickupRow],
                          pred) -> Tuple[Optional[PickupRow], str]:
    """The single authorized row, or ``(None, reason)``.

    A uniquely authorized exact row is selected; an empty match is a decline
    and a multi-match is a conservative pile cancellation -- the intent never
    model-chooses among several row candidates (AC11, section 3.3).
    """
    matches = authorized_rows(rows, pred)
    if not matches:
        return None, "no authorized row"
    if len(matches) > 1:
        return None, "ambiguous pile: %d authorized rows" % len(matches)
    return matches[0], "unique authorized row"


def is_capacity_prompt(prompt: str) -> bool:
    """True when a yes/no prompt is a capacity/encumbrance question."""
    low = (prompt or "").lower()
    return any(m in low for m in _CAPACITY_MARKERS)


__all__ = [
    "SHAPES", "SHAPE_KEYS", "shape_title", "NATIVE_SHAPES", "MANUAL_SHAPES",
    "validate_partition", "disposition", "PickupRow", "parse_rows",
    "is_unpaid_row", "selectable_rows", "authorized_rows",
    "unique_authorized_row", "is_capacity_prompt",
]
