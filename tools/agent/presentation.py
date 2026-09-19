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
===============  =====================================================

The set is closed by the plan.  A frozen table larger than the bounded key
count is the same failure as any other table shape the renderer cannot present
faithfully, so it maps to ``unsupported-semantic`` -- the whole request is
refused, never silently truncated -- rather than adding a code outside the
approved vocabulary.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import instances, protocol, state
from .instances import (T_ALTAR, T_BOULDER, T_CLOSED_DOOR, T_CORRIDOR,
                        T_DOORWAY, T_FLOOR, T_FOUNTAIN, T_LAVA, T_OPEN_DOOR,
                        T_STAIRS_DOWN, T_STAIRS_UP, T_TRAP, T_TREE, T_UNKNOWN,
                        T_WATER)

#: Bumped whenever the rendered keys, criteria or state payload change meaning,
#: so an artifact can name the presentation that produced it.  Recorded in
#: allowlisted metadata only -- never as a wire field.
#:
#: ``/2`` adds the room-awareness enrichment (plan §5): original item/creature/
#: unclassified foreground markers, a corrected fixed legend and the ``room``
#: state field with its destination-appearance clause.
PRESENTATION_VERSION = "jev-presentation/2"

# -- refusal codes ---------------------------------------------------------

REFUSAL_UNSUPPORTED_NEED = "unsupported-need"
REFUSAL_SINGLETON = "singleton"
REFUSAL_INVALID_LABEL = "invalid-label"
REFUSAL_UNSUPPORTED_SEMANTIC = "unsupported-semantic"
REFUSAL_MISSING_BINDING = "missing-required-binding"

REFUSAL_CODES = (REFUSAL_UNSUPPORTED_NEED, REFUSAL_SINGLETON,
                 REFUSAL_INVALID_LABEL, REFUSAL_UNSUPPORTED_SEMANTIC,
                 REFUSAL_MISSING_BINDING)

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
    # A table above the bounded key count cannot be presented faithfully, so
    # it refuses the whole request as an unsupported table shape (the closed
    # vocabulary has no separate bound code) -- never silent truncation.
    if len(members) > MAX_OPTION_KEYS:
        return (None, None, REFUSAL_UNSUPPORTED_SEMANTIC)
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
ASCEND_FALLBACK = "Attempt to go up here."
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


#: Command labels whose decoded *purpose* outranks a plain walk reading: a
#: withdrawal or recovery step keeps its own sentence even when its canonical
#: action happens to be a movement key.
MOVEMENT_PURPOSE_STEMS = ("escape", "random-move", "recovery-step", "unblock")

#: The base stems of the approved open-door family (a compass suffix is added
#: by the label, e.g. ``open-door-south``).
OPEN_DOOR_STEMS = ("open", "open-door")

#: The canonical open-door command: the native ``o`` key (``src/cmd.c`` binds
#: ``"open"`` to ``doopen``; ``#open`` is the same operation).  Only a key
#: action carrying this code *establishes opening semantics*: the semantic
#: label may choose the key *family*, but it can never turn another action
#: (Eat, Wait, Search, a movement key, an arbitrary key) into an open-door
#: command.
OPEN_DOOR_KEY = protocol.KEY_OPEN


def open_door_command(candidate) -> bool:
    """True only when the frozen canonical action *is* the open-door command.

    The action is authoritative and the label is not: opening semantics need a
    ``key`` action whose code is exactly the native open command.  Every other
    action shape -- including a movement key, whose immediate effect is a walk
    -- does not establish opening and must be refused rather than described as
    opening a door.
    """
    action = getattr(candidate, "action", None)
    if action is None or getattr(action, "tag", None) != "key":
        return False
    payload = getattr(action, "payload", ()) or ()
    return bool(payload) and payload[0] == OPEN_DOOR_KEY


def _bound_compass(candidate) -> Optional[str]:
    """The compass bound to a frozen candidate, decoded from the candidate.

    The canonical action and the frozen ``direction`` field are authoritative;
    an emitted key string is never parsed back.  ``None`` means the candidate
    carries no bound direction.
    """
    step = tuple(getattr(candidate, "direction", ()) or ())
    if step in COMPASS:
        return COMPASS[step]
    return movement_of(candidate)


def open_door_direction(stem: str) -> Optional[str]:
    """The compass name bound in an open-door stem, or ``None``."""
    for name in DIR_BY_NAME:
        if stem in ("open-%s" % name, "open-door-%s" % name):
            return name
    return None


def open_door_stem(stem: str) -> bool:
    """True for any member of the approved open-door command family."""
    return stem in OPEN_DOOR_STEMS or open_door_direction(stem) is not None


def _bound_door_type(candidate) -> str:
    """The exact frozen door-type binding, or ``""`` (never fabricated)."""
    payload = getattr(candidate, "effect_payload", ()) or ()
    for item in payload:
        if isinstance(item, dict):
            text = item.get("door_type") or item.get("door")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _door_open_text(candidate, stem: str) -> Tuple[str, str]:
    """The open-door template for a frozen *open command*, or a refusal.

    Opening semantics come from the canonical action alone: a member whose
    frozen action is not the native open command refuses the whole request
    (``unsupported-semantic``) however its label reads, so Eat, Wait, Search,
    movement and arbitrary keys are never described as opening a door.  For the
    canonical command a bound direction -- the frozen candidate's ``direction``
    field, or failing that a compass named by the label stem -- selects the
    directional template, typed when an exact door-type binding exists and
    untyped otherwise.  With no bound direction the member is the initiation
    form, which chooses its direction at the next prompt: that direction is a
    later choice, not a binding required *here*, so the initiation renders
    rather than refusing.  ``locked`` is never fabricated.
    """
    if not open_door_command(candidate):
        return (None, REFUSAL_UNSUPPORTED_SEMANTIC)
    direction = _bound_compass(candidate) or open_door_direction(stem)
    if direction is None:
        return (DOOR_INITIATE_TEMPLATE, "")
    door_type = _bound_door_type(candidate)
    if door_type:
        return (DOOR_OPEN_TYPED_TEMPLATE % (door_type, direction), "")
    return (DOOR_OPEN_TEMPLATE % direction, "")


def _walk_text(candidate, compass: str, context) -> Tuple[str, str]:
    """The navigate template for one movement member.

    Only a *confirmed* ``T_CLOSED_DOOR`` adjacent cell takes the blocking-door
    branch: a remembered doorway or open door is not a closed door and never
    acquires a "closed / may block movement" claim.  Those use their approved
    remembered-terrain phrases instead.  When a current known terrain class at
    the destination contradicts the remembered class, the stale remembered
    phrase (floor *or* door) is omitted rather than presented as fact, and the
    current-class clause carries the displacement instead.
    """
    purpose = purpose_of(candidate.reason)
    hero = _hero(context)
    if hero is None:
        return (_join("Walk %s." % compass, purpose), "")
    step = DIR_BY_NAME[compass]
    dest = (hero[0] + step[0], hero[1] + step[1])
    klass = terrain_class(context, dest)
    clause = destination_appearance_clause(candidate, "command", context)
    current = _current_terrain_at(context, dest)
    stale = (current is not None and klass is not None and current != klass)
    if klass == T_CLOSED_DOOR and not stale:
        return (_join(DOOR_TOWARD_TEMPLATE % compass, clause, purpose), "")
    phrase = (TERRAIN_PHRASES.get(klass)
              if (klass is not None and not stale) else None)
    if phrase is None:
        return (_join("Walk %s." % compass, clause, purpose), "")
    return (_join("Walk %s onto %s." % (compass, phrase), clause, purpose),
            "")


def _dungeon_level(context) -> Optional[int]:
    """The displayed dungeon level as an int, or ``None`` when unavailable."""
    st = getattr(getattr(context, "memory", None), "status", None)
    raw = getattr(st, "dlvl", None)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _exit_possible(context) -> bool:
    """True only when the up-staircase could leave the dungeon.

    The dungeon exit is reachable only from level 1; from any deeper level an
    up-staircase leads to the previous level.  Unknown level evidence is never
    treated as an exit, so the caveat is dropped rather than over-claimed.
    """
    return _dungeon_level(context) == 1


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
        if not on_stairs:
            return (ASCEND_FALLBACK, "")
        caveat = ASCEND_LEAVE_DUNGEON if _exit_possible(context) else ""
        return (_join(ASCEND_TEMPLATE, caveat), "")
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
        compass = movement_of(candidate)
        if compass is not None:
            return (_join("Move %s as a recovery step." % compass,
                          destination_appearance_clause(candidate, "command",
                                                        context),
                          LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
        return (_join(SEARCH_TEMPLATE,
                      LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
    if stem in ("random-move", "recovery-step"):
        compass = movement_of(candidate)
        if compass is not None:
            return (_join("Move %s as a recovery step." % compass,
                          destination_appearance_clause(candidate, "command",
                                                        context),
                          LOOP_BREAKER_CLAUSE if loop_breaker else ""), "")
        return (LOOP_BREAKER_CLAUSE, "")
    if stem == "escape":
        compass = movement_of(candidate)
        if compass is not None:
            return (_join("Move %s, %s" % (compass, WITHDRAW_CLAUSE.lower()),
                          destination_appearance_clause(candidate, "command",
                                                        context)), "")
        return (WITHDRAW_CLAUSE, "")
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
    stem = command_stem(candidate.semantic_label)
    compass = movement_of(candidate)
    # The open-door family is judged on the *canonical action*, ahead of every
    # label- or movement-driven reading: an ``open-door*`` label on a frozen
    # action that is not the open command refuses the whole request instead of
    # being described as opening a door -- or, when it is a movement key,
    # instead of being lossily re-read as a plain walk.
    if stem is not None and open_door_stem(stem):
        return _door_open_text(candidate, stem)
    # A withdrawal/recovery label keeps its decoded purpose even when the
    # canonical action is a movement key: the purpose outranks a plain walk.
    if stem is not None and stem in MOVEMENT_PURPOSE_STEMS:
        return _command_text(candidate, stem, context)
    if compass is not None:
        # A walk action is a navigate member: its option key is ``navigate-*``
        # and its criterion is the walk template (which may name an adjacent
        # *confirmed* closed door, never a remembered doorway).
        return _walk_text(candidate, compass, context)
    # Non-movement command members: the labelled command templates.  A label
    # that cannot be normalized refuses rather than being lossily aliased.
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
#: snapshot-tested; every glyph the map renderer can emit is covered.  The map
#: carries **no color**, so no entry claims a color distinction; ``*``/``&``/``?``
#: are the closed current-screen foreground markers.
LEGEND = {
    " ": "unknown or unobserved",
    ".": "classified floor",
    "#": "classified corridor or tree",
    "-": "classified open door; orientation omitted",
    "|": "classified wall or bars; orientation omitted",
    "+": "classified closed door or doorway of unknown state",
    ">": "classified stairs down",
    "<": "classified stairs up",
    "@": "your confirmed hero",
    "*": ("creature appearance shown on the current screen; disposition "
          "unknown"),
    "&": "item appearance shown on the current screen; see room.contents",
    "?": "unclassified nonblank display shown on the current screen",
    "^": "classified trap",
    "}": "classified water or lava",
    "0": "classified boulder",
    "{": "classified fountain",
    "_": "classified altar",
}

#: The exact fixed room-scope sentence (plan §5.5).
ROOM_SCOPE = ("Current screen within map bounds, not a segmented room. Screen "
              "appearances may be remembered by the game; contents are not "
              "exhaustive. Terrain may be remembered. Openings are landmarks, "
              "not verified exits or routes.")

#: The room list caps (plan §5.5).
CONTENTS_LIMIT = 16
OPENINGS_LIMIT = 12

#: The classes eligible for ``room.openings`` (corridor/door/stair landmarks).
OPENING_CLASSES = (T_CORRIDOR, T_DOORWAY, T_OPEN_DOOR, T_CLOSED_DOOR,
                   T_STAIRS_UP, T_STAIRS_DOWN)

#: Door/stair landmarks precede plain corridor ones in the openings list.
_OPENING_PRIORITY = {T_DOORWAY: 0, T_OPEN_DOOR: 0, T_CLOSED_DOOR: 0,
                     T_STAIRS_UP: 0, T_STAIRS_DOWN: 0, T_CORRIDOR: 1}

#: The current classified features that ``room.contents`` records (plan §5.5).
CONTENTS_FEATURES = (T_TREE, T_WATER, T_LAVA, T_TRAP, T_BOULDER, T_FOUNTAIN,
                     T_ALTAR)

#: The eight compass names by sign-based direction from the hero (north is
#: decreasing y).
COMPASS_NAMES = {(0, 1): "south", (0, -1): "north", (1, 0): "east",
                 (-1, 0): "west", (1, 1): "southeast", (1, -1): "northeast",
                 (-1, 1): "southwest", (-1, -1): "northwest"}

#: Deterministic summaries for the strategy goals, in the fixed priority
#: order the directive vocabulary already has.  Schema v2 adds exactly two
#: destination goals (plan 2.2); the map must match the validator exactly.
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
    "collect_items": "Collect the items visible at the directed location.",
    "flee_to_upstairs": "Reach the known up staircase (do not ascend).",
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


def _text(value) -> Optional[str]:
    """A displayed string: ``None`` when unavailable, ``""`` when known empty.

    Presence- and type-sensitive: an absent attribute (``None``) or a non-str
    value is *unavailable* and renders ``null``; a present ``str`` -- including
    the empty string -- is preserved exactly.  Truthiness is never consulted,
    so a known-empty value is never confused with a missing one.
    """
    return value if isinstance(value, str) else None


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


def _row_text(row) -> Optional[str]:
    """The displayed text of one cached inventory row, or ``None``.

    A row that cannot be rendered as a nonempty string is skipped, but it
    still occupies its source position: the 40-row window is applied to the
    *observed listing*, not to the filtered strings, so a skipped row never
    lets a later row slide into the emitted listing.
    """
    text = row.get("text") if isinstance(row, dict) else row
    if isinstance(text, str) and text:
        return text
    return None


def _inventory_payload(mem, st) -> Dict[str, Any]:
    inv = getattr(mem, "inventory", None)
    if inv is None or getattr(inv, "seen_tick", None) is None:
        return {"items": None, "cached": False, "age_turns": None,
                "truncated": False}
    rows = list(getattr(inv, "rows", None) or [])
    window = rows[:INVENTORY_LIMIT]
    items = [text for text in map(_row_text, window) if text is not None]
    # ``truncated`` is true only when the observed cache holds rows beyond the
    # window that a full listing would actually show.  A skipped row *inside*
    # the window displaces that content (so it truncates); a skipped row
    # *beyond* the window omits nothing, so it does not.
    beyond = any(_row_text(row) is not None
                 for row in rows[INVENTORY_LIMIT:])
    age = None
    game_time = _int_or_none(getattr(st, "time", None))
    seen_time = _int_or_none(getattr(inv, "seen_time", None))
    if game_time is not None and seen_time is not None:
        age = max(0, game_time - seen_time)
    return {"items": items, "cached": True,
            "age_turns": age, "truncated": bool(beyond)}


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


# -- room awareness (plan section 5) ---------------------------------------

def _cell_tuple(entry):
    """``(glyph, color, style, other)`` from one snapshot map cell."""
    if not entry:
        return None
    return (entry[0],
            entry[1] if len(entry) > 1 else "",
            entry[2] if len(entry) > 2 else "",
            entry[3] if len(entry) > 3 else "")


def _appearance_of(entry, pos, hero):
    cell = _cell_tuple(entry)
    if cell is None or not cell[0]:
        return None
    return instances.display_appearance(cell[0], cell[1], cell[2], cell[3],
                                        pos, hero)


def _chebyshev(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _direction_name(hero, pos):
    """The sign-based compass name from *hero* to *pos* (§5.5).

    ``here`` when equal, ``None`` with no hero.  North is decreasing y.
    """
    if hero is None:
        return None
    d = (pos[0] - hero[0], pos[1] - hero[1])
    if d == (0, 0):
        return "here"
    def _sign(v):
        return 0 if v == 0 else (1 if v > 0 else -1)
    return COMPASS_NAMES.get((_sign(d[0]), _sign(d[1])))


def _shown_of(entry, pos, hero):
    """The ``shown`` label for one coordinate (plan §5.5)."""
    app = _appearance_of(entry, pos, hero)
    if app is None or app.kind == instances.APP_BLANK:
        return "none"
    if app.kind == instances.APP_HERO:
        return "hero"
    if app.kind == instances.APP_CREATURE:
        return "creature"
    if app.kind == instances.APP_ITEM:
        return "item"
    if app.kind == instances.APP_UNCLASSIFIED:
        return "unclassified"
    return "none"        # a classified feature is not a foreground appearance


def _room_contents(context, mem, bounds):
    """The bounded ``room.contents`` list, or ``None`` when unavailable.

    Source: only the current ``snapshot.map``, within the returned map
    rectangle.  One record per non-hero position that shows a creature or item
    appearance, an unclassified nonblank display, or a current classified
    feature in :data:`CONTENTS_FEATURES`.
    """
    snap = getattr(context, "snapshot", None)
    cells = state._current_map(snap)
    if cells is None or bounds is None:
        return None
    hero = getattr(mem, "hero", None)
    hero_pos = tuple(hero) if hero is not None else None
    x_min, x_max, y_min, y_max = bounds
    records = []
    for pos, entry in cells.items():
        pos = tuple(pos)
        if not (x_min <= pos[0] <= x_max and y_min <= pos[1] <= y_max):
            continue
        if hero_pos is not None and pos == hero_pos:
            continue
        app = _appearance_of(entry, pos, hero_pos)
        if app is None or app.kind == instances.APP_BLANK:
            continue
        if app.kind == instances.APP_CREATURE:
            kind, category = "creature", "creature"
        elif app.kind == instances.APP_ITEM:
            kind, category = "item", app.category
        elif app.kind == instances.APP_UNCLASSIFIED:
            kind, category = "unclassified", app.category
        elif app.kind == instances.APP_FEATURE \
                and app.terrain in CONTENTS_FEATURES:
            kind, category = "feature", app.terrain
        else:
            continue
        records.append((pos, {"at": [pos[0], pos[1]], "kind": kind,
                              "category": category}))
    return records


def _room_openings(context, mem, bounds):
    """The bounded ``room.openings`` list, or ``None`` when unavailable.

    Source: the current display tuple classified with
    :func:`instances.classify_cell` when an available current snapshot supplies
    one, else the remembered class through ``TerrainMemory.ter``.  A current
    known class controls the record; a currently shown wall suppresses a stale
    remembered door, and a current item/creature over remembered stairs keeps
    ``source="memory"`` (the foreground appearance is not the terrain).
    """
    snap = getattr(context, "snapshot", None)
    cells = state._current_map(snap)
    terrain = getattr(context, "terrain", None)
    has_terrain = terrain is not None and hasattr(terrain, "ter")
    if cells is None and not has_terrain:
        return None
    if bounds is None:
        return None
    hero = getattr(mem, "hero", None)
    hero_pos = tuple(hero) if hero is not None else None
    x_min, x_max, y_min, y_max = bounds
    records = []
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            pos = (x, y)
            entry = cells.get(pos) if cells else None
            current = None
            if entry:
                cell = _cell_tuple(entry)
                if cell and cell[0]:
                    current = instances.classify_cell(cell[0], cell[1], cell[2],
                                                      cell[3])
            remembered = terrain.ter(pos) if has_terrain else T_UNKNOWN
            if current is not None and current.terrain != T_UNKNOWN:
                if current.terrain not in OPENING_CLASSES:
                    continue
                klass, source = current.terrain, "screen"
            elif remembered in OPENING_CLASSES:
                klass, source = remembered, "memory"
            else:
                continue
            shown = _shown_of(entry, pos, hero_pos) if cells is not None \
                else None
            records.append((pos, {"at": [x, y],
                                  "direction": _direction_name(hero_pos, pos),
                                  "terrain": klass, "source": source,
                                  "shown": shown}))
    return records


def _order_and_cap(records, hero, limit, priority=None):
    """Deterministic ordering + cap; returns ``(kept, omitted)``."""
    hero_pos = tuple(hero) if hero is not None else None

    def key(item):
        pos, record = item
        rank = 0 if priority is None else priority.get(record.get("terrain"), 0)
        if hero_pos is not None:
            return (rank, _chebyshev(hero_pos, pos), pos[1], pos[0])
        return (rank, 0, pos[1], pos[0])

    ordered = sorted(records, key=key)
    return ordered[:limit], max(0, len(ordered) - limit)


def room_payload(context, mem, bounds):
    """The required ``room`` state object (plan §5.5).

    Pure and read-only.  Each list is ``null`` when its required source is
    unavailable (and its omitted count is then ``null``); ``[]`` with count 0
    means the available source contains no matching records, not that the room
    is empty.
    """
    contents = _room_contents(context, mem, bounds)
    openings = _room_openings(context, mem, bounds)
    hero = getattr(mem, "hero", None)
    if contents is None:
        contents_list, contents_omitted = None, None
    else:
        kept, omitted = _order_and_cap(contents, hero, CONTENTS_LIMIT)
        contents_list = [rec for _pos, rec in kept]
        contents_omitted = omitted
    if openings is None:
        openings_list, openings_omitted = None, None
    else:
        kept, omitted = _order_and_cap(openings, hero, OPENINGS_LIMIT,
                                       _OPENING_PRIORITY)
        openings_list = [rec for _pos, rec in kept]
        openings_omitted = omitted
    return {"scope": ROOM_SCOPE,
            "contents": contents_list,
            "contents_omitted": contents_omitted,
            "openings": openings_list,
            "openings_omitted": openings_omitted}


# -- destination appearance clause (plan section 5.6) ----------------------

ITEM_CLAUSE = "An item with %s is shown on that square."
UNCLASSIFIED_CLAUSE = "An unclassified display is shown on that square."
FEATURE_CLAUSE = "The current screen classifies that square as %s."


def destination_appearance_clause(candidate, need_kind: str, context) -> str:
    """At most one appearance clause for a *command-need movement action*.

    Returns an empty string for a non-command need, a non-movement action, an
    unavailable current snapshot, an absent destination, or no applicable
    appearance -- so direction/key answers stay neutral and nonmovement
    recovery/search never acquires a destination claim.  The clause describes
    the actual adjacent destination encoded by the frozen candidate's own
    direction, never a route target or a capped-list entry.
    """
    if need_kind != "command":
        return ""
    if movement_of(candidate) is None:
        return ""
    hero = _hero(context)
    if hero is None:
        return ""
    compass = movement_of(candidate)
    step = DIR_BY_NAME.get(compass)
    if step is None:
        return ""
    dest = (hero[0] + step[0], hero[1] + step[1])
    snap = getattr(context, "snapshot", None)
    cells = state._current_map(snap)
    if cells is None:
        return ""
    entry = cells.get(dest)
    if not entry:
        return ""
    app = _appearance_of(entry, dest, hero)
    if app is None or app.kind == instances.APP_BLANK:
        return ""
    if app.kind == instances.APP_CREATURE:
        return OCCUPANT_CLAUSE
    if app.kind == instances.APP_ITEM:
        return ITEM_CLAUSE % app.category
    if app.kind == instances.APP_UNCLASSIFIED:
        return UNCLASSIFIED_CLAUSE
    if app.kind == instances.APP_FEATURE:
        return FEATURE_CLAUSE % app.terrain
    return ""


def _current_terrain_at(context, dest):
    """The current classified terrain at *dest*, or ``None`` when unknown."""
    snap = getattr(context, "snapshot", None)
    cells = state._current_map(snap)
    if not cells:
        return None
    entry = cells.get(dest)
    cell = _cell_tuple(entry)
    if cell is None or not cell[0]:
        return None
    classified = instances.classify_cell(cell[0], cell[1], cell[2], cell[3])
    if classified.terrain == T_UNKNOWN:
        return None
    return classified.terrain


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
    # The map is rendered once; the room lists reuse the *same* rectangle so a
    # later crop or cap can never disagree with what the map shows.
    game_map = _map_payload(context, mem)
    if game_map is None:
        bounds = None
    else:
        bounds = (game_map["x_min"], game_map["x_max"],
                  game_map["y_min"], game_map["y_max"])
    room = _room_payload_safe(context, mem, bounds)
    return {
        "game": GAME,
        "objective": OBJECTIVE,
        "legend": dict(LEGEND),
        "status": {
            "hp": _int_or_none(getattr(st, "hp", None)),
            "hp_max": _int_or_none(getattr(st, "hp_max", None)),
            "hunger": _text(getattr(st, "hunger", None)),
            "dungeon_level": _text(getattr(st, "dlvl", None)),
            "experience_level": _int_or_none(getattr(st, "level", None)),
            "conditions": _conditions(getattr(context, "snapshot", None)),
        },
        "hero": [int(hero[0]), int(hero[1])] if hero is not None else None,
        "inventory": _inventory_payload(mem, st),
        "directives": _directive_summaries(context),
        "intent": _intent_payload(context),
        "messages": _messages_payload(mem),
        "need": {"kind": _text(need.get("kind")),
                 "prompt": _text(need.get("prompt"))},
        "map": game_map,
        "room": room,
        "stairs": _stairs_payload(mem),
    }


def _room_payload_safe(context, mem, bounds):
    """``room`` with the required shape even if a helper degrades."""
    try:
        return room_payload(context, mem, bounds)
    except Exception:                           # noqa: BLE001
        return {"scope": ROOM_SCOPE, "contents": None, "contents_omitted": None,
                "openings": None, "openings_omitted": None}


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
