"""Jev presentation: semantic option keys, grounded criteria, state payload.

This module is the pure presentation layer of the Jev adapter.  It owns three
things and nothing else:

  * **option keys** -- a need-aware, semantic kebab-case key derived from a
    *frozen* retained candidate (``needs`` x ``candidate``), with a closed
    normalization/alias contract and ``--N`` collision suffixes;
  * **criterion strings** -- per-family grounded natural-language templates
    rendered from the persistent classified terrain, the current snapshot and
    the frozen candidate, with an explicit refusal code when a member cannot
    be rendered faithfully; and
  * **the state payload** -- the compact remembered-state JSON object.

It never mutates memory, never re-dedups or reorders a table, never touches
``policy`` and never invents a candidate.  ``policy.py`` remains the only
authority on *what* candidates exist; this module decides only how an existing
candidate is named and described.  ``key_index`` is authoritative and a
returned key is never parsed back.

The refusal vocabulary is closed and recorded in the decision sidecar *before*
any reservation:

===============  =====================================================
code             meaning
===============  =====================================================
unsupported-need need kind the presentation renderer does not support
singleton        table below the multi-choice threshold
invalid-label    normalization rejected a semantic label
unsupported-      decoded action has no faithful template at this need
semantic         boundary
missing-required- a required item/menu-row/direction binding is absent
binding
too-many-options the frozen table exceeds the bounded key count
===============  =====================================================

``too-many-options`` is an implementation addition: a
:class:`tools.agent.candidates.CandidateTable` is already capped at
``MAX_CANDIDATES`` by construction, and the wire contract requires the emitted
key count to stay within the same bound, so the guard is defensive -- but it
must refuse (whole request) rather than silently truncate.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import protocol, state
from .instances import (T_CLOSED_DOOR, T_CORRIDOR, T_DOORWAY, T_FLOOR,
                        T_OPEN_DOOR, T_STAIRS_DOWN, T_STAIRS_UP)

#: Bumped whenever the rendered keys, criteria or state payload change meaning,
#: so an artifact can name the presentation that produced it.  Recorded in
#: allowlisted metadata only -- never as a wire field.
PRESENTATION_VERSION = "jev-presentation/1"

# -- refusal codes ---------------------------------------------------------

REFUSAL_UNSUPPORTED_NEED = "unsupported-need"
REFUSAL_SINGLETON = "singleton"
REFUSAL_INVALID_LABEL = "invalid-label"
REFUSAL_UNSUPPORTED_SEMANTIC = "unsupported-semantic"
REFUSAL_MISSING_BINDING = "missing-required-binding"
REFUSAL_TOO_MANY = "too-many-options"

REFUSAL_CODES = (REFUSAL_UNSUPPORTED_NEED, REFUSAL_SINGLETON,
                 REFUSAL_INVALID_LABEL, REFUSAL_UNSUPPORTED_SEMANTIC,
                 REFUSAL_MISSING_BINDING, REFUSAL_TOO_MANY)

#: Needs the presentation renderer supports.  Everything else (menus, yn,
#: line, extcmd, position, ack) stays wholly on the scripted tier.
SUPPORTED_KINDS = ("command", "key", "direction")

#: One frozen table may never present more options than a single bounded
#: choice holds (``protocol.KEY_MIN..KEY_MAX`` / ``candidates.MAX_CANDIDATES``).
MAX_OPTION_KEYS = 255

# -- keys ------------------------------------------------------------------

#: The eight compass moves, keyed by the (dx, dy) the engine's ``DIR_KEYS``
#: uses.  ``(0, 0)`` is deliberately absent: it is not a compass move.
COMPASS = {(0, -1): "north", (1, -1): "northeast", (1, 0): "east",
           (1, 1): "southeast", (0, 1): "south", (-1, 1): "southwest",
           (-1, 0): "west", (-1, -1): "northwest"}
DIR_BY_NAME = {name: step for step, name in COMPASS.items()}

#: Reverse of ``protocol.DIR_KEYS``: a movement key code -> its compass name.
KEY_COMPASS = {code: COMPASS[step] for step, code in protocol.DIR_KEYS.items()}

#: Command-need aliases.  Applied before normalization, and only for the
#: ``command`` need: the same policy label may be reused across needs, so a
#: direction or key need must never inherit a command wording.  ``forced-
#: search`` is deliberately absent -- it is never conflated with an ordinary
#: search.
COMMAND_ALIASES = {
    "search": "search-in-place",
    "search-secret": "search-in-place",
    "eat": "eat-food",
    "descend": "descend-stairs",
    "go-upstairs": "go-upstairs",
    "ascend-stairs": "go-upstairs",
    "inspect-inventory": "inventory",
    "refresh-inventory": "inventory",
    "rest": "wait",
}

#: Key-need stems: what a decoded key code establishes about itself.  A code
#: absent here cannot be described neutrally, so that member refuses.
KEY_STEMS = {
    protocol.KEY_SEARCH: "search",
    protocol.KEY_ESC: "escape",
    protocol.KEY_EAT: "eat",
    protocol.KEY_WAIT: "wait",
    protocol.KEY_INV: "inventory",
    protocol.KEY_PICKUP: "pick-up",
    protocol.KEY_HASH: "quit",
}
KEY_STEMS.update(KEY_COMPASS)


def normalize_stem(label: Any) -> Optional[str]:
    """The closed normalization of a semantic label into a key stem.

    ASCII-only, lowercase, every run of non-alphanumerics collapsed to a
    single ``-``, leading/trailing ``-`` stripped.  An empty result, any
    non-ASCII character, or a pre-existing ``--`` (the reserved suffix
    separator) is **rejected** rather than lossily aliased.
    """
    if not isinstance(label, str) or not label:
        return None
    for ch in label:
        if ord(ch) > 127:
            return None
    if "--" in label:
        return None
    out: List[str] = []
    for ch in label.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    stem = "".join(out).strip("-")
    if not stem or "--" in stem:
        return None
    return stem


def command_stem(label: Any) -> Optional[str]:
    """The command-need stem of a policy label (alias, then normalize)."""
    stem = COMMAND_ALIASES.get(label)
    if stem is not None:
        return stem
    return normalize_stem(label)


def movement_of(candidate) -> Optional[str]:
    """The compass move a frozen candidate's canonical action encodes."""
    if candidate.action.tag != "key":
        return None
    return KEY_COMPASS.get(candidate.action.payload[0])


def base_key(need_kind: str, candidate) -> Tuple[Optional[str], str]:
    """The collision-free base key of one frozen member, or a refusal code.

    The canonical wire action is authoritative: a ``command``-need movement
    action is a ``navigate-<compass>`` walk, a ``direction`` need accepts only
    an established direction and a ``key`` need accepts only a key whose
    semantics the pending need and action establish.  Whether the decoded
    action has a faithful *template* is decided by :func:`render_criterion`,
    not here.
    """
    if need_kind not in SUPPORTED_KINDS:
        return (None, REFUSAL_UNSUPPORTED_NEED)
    if need_kind == "command":
        compass = movement_of(candidate)
        if compass is not None:
            return ("navigate-" + compass, "")
        stem = command_stem(candidate.semantic_label)
        if stem is None:
            return (None, REFUSAL_INVALID_LABEL)
        return (stem, "")
    if candidate.action.tag != "key":
        return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
    compass = movement_of(candidate)
    if need_kind == "direction":
        if compass is None:
            return (None, REFUSAL_MISSING_BINDING)
        return ("direction-" + compass, "")
    # key need
    stem = KEY_STEMS.get(candidate.action.payload[0])
    if stem is None:
        return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
    return ("key-" + stem, "")


def option_keys(need_kind: str,
                ordered_candidates: Sequence[Any]
                ) -> Tuple[Optional[List[str]], Optional[Dict[str, int]], str]:
    """``(keys, key_index, refusal)`` for one frozen retained table.

    Keys are computed over the *already retained ordered table*: a unique base
    key is unchanged and every member of a collision group receives a ``--N``
    suffix, numbered ``1..k`` in retained-table order.  The double hyphen is
    reserved for exactly that suffix.  A base key that cannot be formed, or a
    table larger than :data:`MAX_OPTION_KEYS`, refuses the whole request.
    """
    members = list(ordered_candidates)
    if need_kind not in SUPPORTED_KINDS:
        return (None, None, REFUSAL_UNSUPPORTED_NEED)
    if len(members) > MAX_OPTION_KEYS:
        return (None, None, REFUSAL_TOO_MANY)
    bases: List[str] = []
    for candidate in members:
        stem, refusal = base_key(need_kind, candidate)
        if stem is None:
            return (None, None, refusal)
        bases.append(stem)
    counts: Dict[str, int] = {}
    for stem in bases:
        counts[stem] = counts.get(stem, 0) + 1
    seen: Dict[str, int] = {}
    keys: List[str] = []
    for stem in bases:
        if counts[stem] == 1:
            keys.append(stem)
            continue
        seen[stem] = seen.get(stem, 0) + 1
        keys.append("%s--%d" % (stem, seen[stem]))
    if len(set(keys)) != len(keys):
        return (None, None, REFUSAL_INVALID_LABEL)
    return (keys, {key: i for i, key in enumerate(keys)}, "")


# -- criteria --------------------------------------------------------------

DIRECTION_TEMPLATE = "Choose %s for the pending action."
SEARCH_TEMPLATE = "Search for hidden passages or doors here."
LOOP_BREAKER_CLAUSE = "Try to break the recent lack of progress."
EAT_INITIATE_TEMPLATE = ("Begin eating; choose a food item at the next "
                         "prompt.")
DESCEND_TEMPLATE = ("Descend the staircase here, going deeper into the "
                    "dungeon.")
DESCEND_FALLBACK = "Attempt to go down here."
ASCEND_TEMPLATE = ("Ascend the staircase here to the previous dungeon "
                   "level.")
ASCEND_LEAVE_DUNGEON = "Go up here; this may leave the dungeon."
INVENTORY_TEMPLATE = "Review your inventory."
INVENTORY_FOOD_CLAUSE = "Check what food is available."
QUIT_INITIATE_TEMPLATE = ("Begin the quit sequence; this will end the run if "
                          "confirmed.")
QUIT_BOUND_TEMPLATE = "Quit the game, ending this run."
WAIT_TEMPLATE = "Wait one turn in place."
WAIT_HOLD_CLAUSE = "Allow time to pass while holding position."
PICKUP_TEMPLATE = "Pick up items here; select among them if prompted."
FORCED_SEARCH_TEMPLATE = ("Begin the exceptional forced-search sequence; "
                          "ordinary searching has been refused.")
DOOR_TOWARD_TEMPLATE = ("Move %s toward the adjacent closed door; it may "
                        "block movement.")
DOOR_OPEN_TEMPLATE = "Try to open the door to the %s."
DOOR_OPEN_TYPED_TEMPLATE = "Try to open the %s door to the %s."
DOOR_INITIATE_TEMPLATE = ("Begin opening a door; choose its direction at the "
                          "next prompt.")
OCCUPANT_CLAUSE = ("A creature is shown on that square; its disposition is "
                   "unknown.")
WITHDRAW_CLAUSE = "Withdraw from danger."

#: Only these classifications are named in model-facing text; every other
#: class (wall, tree, water, lava, trap, bars, boulder, fountain, altar,
#: unknown) omits the terrain clause entirely rather than over-claiming.
TERRAIN_PHRASES = {
    T_FLOOR: "remembered room floor",
    T_CORRIDOR: "a remembered corridor",
    T_DOORWAY: "a remembered open doorway",
    T_OPEN_DOOR: "a remembered open doorway",
    T_STAIRS_DOWN: "the remembered down staircase",
    T_STAIRS_UP: "the remembered up staircase",
}

#: Recognized policy reasons supply route purpose only.  Anything else is
#: omitted -- raw metadata is never dumped into model-facing text.
PURPOSES = {
    "reachable down stairs": "Follow a route toward known stairs down.",
    "observation frontier": ("Approach the edge of explored terrain to "
                             "reveal more of the map."),
    "unvisited known cell": "Explore a known square not yet visited.",
    "approach a closed door": "Approach a known closed door.",
}


def _join(*fragments: str) -> str:
    return " ".join(f for f in fragments if f)


def purpose_of(reason: Any) -> str:
    """The recognized route purpose of a policy reason, or ``""``.

    Tolerates the existing ``navigate`` prefix (with and without its
    ``(<goal>)`` qualifier); an unrecognized reason yields no purpose at all.
    """
    text = (reason or "").strip()
    if text.startswith("navigate"):
        text = text[len("navigate"):].strip()
    if text.startswith("(") and ")" in text:
        text = text[text.index(")") + 1:].strip()
    text = text.lstrip(":").strip()
    return PURPOSES.get(text, "")


def _hero(context) -> Optional[Tuple[int, int]]:
    """The controller-resolved hero square, never a first ``@`` scan."""
    hero = getattr(getattr(context, "memory", None), "hero", None)
    if hero is None or len(tuple(hero)) != 2:
        return None
    return (int(hero[0]), int(hero[1]))


def terrain_class(context, pos) -> Optional[str]:
    """The remembered classification of *pos* from the persistent memory.

    ``None`` means no classified-terrain reference was supplied at all -- the
    caller degrades to the shorter template rather than assuming a class.
    """
    terrain = getattr(context, "terrain", None)
    if terrain is None or not hasattr(terrain, "ter"):
        return None
    return terrain.ter(pos)


def currently_occupied(context, pos, hero) -> bool:
    """True when the *current* snapshot shows a non-hero creature at *pos*.

    Occupancy has exactly one source -- the current full snapshot -- so a
    stale remembered monster is never described as currently present.
    """
    snap = getattr(context, "snapshot", None)
    cell = getattr(snap, "map", None) if snap is not None else None
    if not cell:
        return False
    entry = cell.get(pos)
    if not entry:
        return False
    return state.monster_cell(entry[0], hero, pos)


def _bound_item(candidate) -> str:
    """The exact frozen item binding a candidate carries, or ``""``.

    Only an exact binding frozen on the candidate counts: the cached
    inventory is never sufficient to bind a command to an item.
    """
    payload = getattr(candidate, "effect_payload", ()) or ()
    for item in payload:
        if isinstance(item, dict):
            text = item.get("item") or item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _walk_text(candidate, compass: str, context) -> Tuple[str, str]:
    """The navigate template for one movement member."""
    purpose = purpose_of(candidate.reason)
    hero = _hero(context)
    if hero is None:
        return (_join("Walk %s." % compass, purpose), "")
    step = DIR_BY_NAME[compass]
    dest = (hero[0] + step[0], hero[1] + step[1])
    klass = terrain_class(context, dest)
    occupant = OCCUPANT_CLAUSE if currently_occupied(context, dest, hero) \
        else ""
    if klass in (T_CLOSED_DOOR, T_DOORWAY):
        return (_join(DOOR_TOWARD_TEMPLATE % compass, occupant, purpose), "")
    phrase = TERRAIN_PHRASES.get(klass) if klass is not None else None
    if phrase is None:
        return (_join("Walk %s." % compass, occupant, purpose), "")
    return (_join("Walk %s onto %s." % (compass, phrase), occupant, purpose),
            "")


def _command_text(candidate, stem: str, context) -> Tuple[str, str]:
    """The command-need template of one non-movement member."""
    label = candidate.semantic_label
    reason = candidate.reason or ""
    loop_breaker = "loop breaker" in reason
    if stem == "search-in-place":
        return (_join(SEARCH_TEMPLATE,
                      LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
    if stem == "eat-food":
        item = _bound_item(candidate)
        if item:
            return ("Eat %s." % item, "")
        return (EAT_INITIATE_TEMPLATE, "")
    if stem == "descend-stairs":
        hero = _hero(context)
        on_stairs = (hero is not None
                     and terrain_class(context, hero) == T_STAIRS_DOWN)
        return (DESCEND_TEMPLATE if on_stairs else DESCEND_FALLBACK, "")
    if stem == "go-upstairs":
        hero = _hero(context)
        on_stairs = (hero is not None
                     and terrain_class(context, hero) == T_STAIRS_UP)
        return (_join(ASCEND_TEMPLATE,
                      ASCEND_LEAVE_DUNGEON if on_stairs else ""), "")
    if stem == "inventory":
        wants_food = any(_wants_food(view)
                         for view in getattr(context, "directives", ()) or ())
        return (_join(INVENTORY_TEMPLATE,
                      INVENTORY_FOOD_CLAUSE if wants_food else ""), "")
    if stem == "quit":
        if label == "quit" or getattr(context, "intent", "") == "quit":
            return (QUIT_INITIATE_TEMPLATE, "")
        return (QUIT_BOUND_TEMPLATE, "")
    if stem == "wait":
        return (_join(WAIT_TEMPLATE, WAIT_HOLD_CLAUSE), "")
    if stem in ("pick-up", "pickup"):
        item = _bound_item(candidate)
        if item:
            return ("Pick up %s." % item, "")
        return (PICKUP_TEMPLATE, "")
    if stem == "unblock":
        if candidate.action.payload[0] == protocol.KEY_WAIT:
            return (_join(WAIT_TEMPLATE, WAIT_HOLD_CLAUSE), "")
        return (_join(SEARCH_TEMPLATE,
                      LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
    if stem == "random-move":
        compass = movement_of(candidate)
        if compass is not None:
            return (_join("Move %s as a recovery step." % compass,
                          LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
        return (LOOP_BREAKER_CLAUSE, "")
    if stem == "escape":
        compass = movement_of(candidate)
        if compass is not None:
            return ("Move %s, %s" % (compass, WITHDRAW_CLAUSE.lower()), "")
        return (WITHDRAW_CLAUSE, "")
    if stem in ("open", "open-door"):
        return (DOOR_INITIATE_TEMPLATE, "")
    if label == "forced-search" or stem == "forced-search":
        return (FORCED_SEARCH_TEMPLATE, "")
    return (None, REFUSAL_UNSUPPORTED_SEMANTIC)


def _wants_food(view) -> bool:
    wants = getattr(view, "wants_food", None)
    if callable(wants):
        try:
            return bool(wants())
        except Exception:                       # noqa: BLE001
            return False
    return False


def render_criterion(candidate, need_kind: str, context) -> Tuple[
        Optional[str], str]:
    """``(text, refusal)`` for one frozen member.

    The canonical wire action is authoritative.  A ``direction`` or ``key``
    need is never described as walking or opening: it is a direction/key
    choice, and its exact prompt is carried in ``state.need``.
    """
    if need_kind not in SUPPORTED_KINDS:
        return (None, REFUSAL_UNSUPPORTED_NEED)
    if need_kind == "direction":
        if movement_of(candidate) is None:
            return (None, REFUSAL_MISSING_BINDING)
        return (DIRECTION_TEMPLATE % movement_of(candidate), "")
    if need_kind == "key":
        if candidate.action.tag != "key":
            return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
        stem = KEY_STEMS.get(candidate.action.payload[0])
        if stem is None:
            return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
        return (DIRECTION_TEMPLATE % stem, "")
    # command need: a non-key action is only renderable when it is the exact
    # bound quit command (the scripted tier's own textual quit); every other
    # non-key shape has no faithful template here.
    if candidate.action.tag == "text":
        if candidate.action.payload[0] == "quit":
            return (QUIT_BOUND_TEMPLATE, "")
        return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
    if candidate.action.tag != "key":
        return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
    compass = movement_of(candidate)
    if compass is not None:
        return _walk_text(candidate, compass, context)
    stem = command_stem(candidate.semantic_label)
    if stem is None:
        return (None, REFUSAL_INVALID_LABEL)
    if candidate.action.payload[0] == protocol.KEY_SEARCH \
            and stem != "forced-search":
        return _command_text(candidate, "search-in-place", context)
    return _command_text(candidate, stem, context)


# -- state payload ---------------------------------------------------------

GAME = "NetHack"
OBJECTIVE = "Survive, explore safely, and descend when prepared."

#: The fixed, fully inlined glyph legend.  Keys and values are verbatim and
#: snapshot-tested, and every glyph the map renderer can emit is covered.
LEGEND = {
    " ": "unknown or unobserved",
    ".": "floor or doorway",
    "#": "corridor or tree; color distinguishes",
    "-": "wall or open door",
    "|": "wall or open door",
    "+": "closed door or wall; color distinguishes",
    ">": "stairs down",
    "<": "stairs up",
    "@": "your hero (from state.hero)",
    "*": "a creature; species unknown",
    "^": "trap",
    "}": "water or lava; color distinguishes",
    "0": "boulder",
    "{": "fountain",
    "_": "altar",
}

#: Deterministic summaries for the nine strategy goals, in the fixed priority
#: order the directive vocabulary already has.
DIRECTIVE_SUMMARIES = {
    "survive": "Prioritize survival.",
    "acquire_food": "Acquire food.",
    "eat_known_safe_food": "Eat known safe food when hungry.",
    "recover": "Recover to a safe state.",
    "explore_frontier": "Explore the edge of known terrain.",
    "search_dead_ends": "Search dead ends for hidden passages.",
    "descend_known_stairs": "Head toward known stairs down.",
    "inspect_inventory": "Review inventory when information is stale.",
    "disengage": "Withdraw from danger.",
}

#: The number of recent messages the payload carries, and the inventory row
#: cap whose overflow sets ``truncated``.
MESSAGE_LIMIT = 6
INVENTORY_LIMIT = 40


def _conditions(snapshot):
    """The displayed condition names, or ``None`` on extraction failure."""
    try:
        from .policy import condition_texts
        return list(condition_texts(snapshot))
    except Exception:                           # noqa: BLE001
        return None


def _int_or_none(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _directive_summaries(context) -> List[str]:
    """Deterministic directive summaries in existing priority order.

    The nine goals map to fixed strings and keep their order; only the
    controlled ``target`` / ``risk`` / ``preconditions`` clauses are appended,
    and only when present.  An ``explanation`` is never passed through as an
    instruction and a ``ttl`` is never emitted.
    """
    out: List[str] = []
    seen = set()
    for view in getattr(context, "directives", ()) or ():
        dset = getattr(view, "dset", None)
        if dset is None:
            continue
        for goal in getattr(dset, "goals", ()) or ():
            text = DIRECTIVE_SUMMARIES.get(goal)
            if text is not None and goal not in seen:
                out.append(text)
                seen.add(goal)
        target = getattr(dset, "target", None)
        if target is not None:
            out.append("Target: %d,%d." % (int(target[0]), int(target[1])))
        risk = getattr(dset, "risk", 0.0) or 0.0
        if risk:
            out.append("Risk level %s." % ("%g" % float(risk)))
        for precondition in getattr(dset, "preconditions", ()) or ():
            out.append("`%s` must hold." % precondition)
    return out


def _inventory_payload(mem, st) -> Dict[str, Any]:
    inv = getattr(mem, "inventory", None)
    if inv is None or getattr(inv, "seen_tick", None) is None:
        return {"items": None, "cached": False, "age_turns": None,
                "truncated": False}
    rendered = []
    for row in getattr(inv, "rows", None) or []:
        text = row.get("text") if isinstance(row, dict) else row
        if isinstance(text, str) and text:
            rendered.append(text)
    age = None
    game_time = _int_or_none(getattr(st, "time", None))
    seen_time = _int_or_none(getattr(inv, "seen_time", None))
    if game_time is not None and seen_time is not None:
        age = max(0, game_time - seen_time)
    return {"items": rendered[:INVENTORY_LIMIT], "cached": True,
            "age_turns": age, "truncated": len(rendered) > INVENTORY_LIMIT}


def _messages_payload(mem):
    recent = getattr(mem, "recent_messages", None)
    if not callable(recent):
        return None
    try:
        return [str(m) for m in recent(MESSAGE_LIMIT)]
    except Exception:                           # noqa: BLE001
        return None


def _stairs_payload(mem):
    try:
        down = sorted(tuple(p) for p in getattr(mem, "stairs_down", ()) or ())
        up = sorted(tuple(p) for p in getattr(mem, "stairs_up", ()) or ())
    except Exception:                           # noqa: BLE001
        return None
    if not down and not up:
        return None
    return {"down": [[int(p[0]), int(p[1])] for p in down],
            "up": [[int(p[0]), int(p[1])] for p in up]}


def _map_payload(context, mem):
    try:
        return state.bounded_map(
            getattr(context, "terrain", None),
            getattr(mem, "hero", None),
            getattr(context, "snapshot", None),
            getattr(mem, "stairs_down", ()) or (),
            getattr(mem, "stairs_up", ()) or ())
    except Exception:                           # noqa: BLE001
        return None


def _intent_payload(context) -> Optional[str]:
    """The pending operation, only when it adds to directives/need.

    Boilerplate ``navigate`` (already conveyed by the need and the criteria)
    is never repeated.
    """
    intent = getattr(context, "intent", "") or ""
    if not intent or intent == "navigate":
        return None
    return intent


def render_state(context) -> Dict[str, Any]:
    """The compact remembered-state payload sent with every request.

    One JSON object; every required field is present with ``null`` when
    unavailable, a list is ``null`` when unavailable and ``[]`` only when
    genuinely empty, and zero values are preserved.  Rendering is pure: no
    helper here mutates memory, the terrain memory, the snapshot or the
    directive book.
    """
    mem = getattr(context, "memory", None)
    st = getattr(mem, "status", None)
    need = context.need or {}
    hero = getattr(mem, "hero", None)
    return {
        "game": GAME,
        "objective": OBJECTIVE,
        "legend": dict(LEGEND),
        "status": {
            "hp": _int_or_none(getattr(st, "hp", None)),
            "hp_max": _int_or_none(getattr(st, "hp_max", None)),
            "hunger": getattr(st, "hunger", "") or "",
            "dungeon_level": getattr(st, "dlvl", "") or "",
            "experience_level": _int_or_none(getattr(st, "level", None)),
            "conditions": _conditions(getattr(context, "snapshot", None)),
        },
        "hero": [int(hero[0]), int(hero[1])] if hero is not None else None,
        "inventory": _inventory_payload(mem, st),
        "directives": _directive_summaries(context),
        "intent": _intent_payload(context),
        "messages": _messages_payload(mem),
        "need": {"kind": need.get("kind") or "",
                 "prompt": need.get("prompt") or None},
        "map": _map_payload(context, mem),
        "stairs": _stairs_payload(mem),
    }


# -- build result ----------------------------------------------------------

@dataclass(frozen=True)
class JevPresentation(object):
    """The frozen presentation of one retained table (keys + criteria)."""

    keys: Tuple[str, ...]
    key_index: Dict[str, int]
    criteria: Dict[str, str]


def present(need_kind: str, ordered_candidates: Sequence[Any], context
            ) -> Tuple[Optional[JevPresentation], str]:
    """The complete presentation of a frozen table, or a refusal code.

    The members are examined in retained-table order and the first member that
    cannot be presented faithfully decides the whole request's refusal: a
    refusal is never a selective dropping of individual candidates.
    """
    keys, key_index, refusal = option_keys(need_kind, ordered_candidates)
    if keys is None:
        return (None, refusal)
    criteria: Dict[str, str] = {}
    for key, candidate in zip(keys, ordered_candidates):
        text, refusal = render_criterion(candidate, need_kind, context)
        if text is None:
            return (None, refusal)
        criteria[key] = text
    return (JevPresentation(tuple(keys), dict(key_index), criteria), "")
