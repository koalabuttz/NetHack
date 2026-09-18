"""Per-episode public-state memory: terrain, status, inventory, boundaries.

Everything here is derived from public snapshots only.  Current observations
are complete ``base:null`` snapshots, so the *presentation* is rebuilt every
observation by :class:`tools.agent.protocol.Snapshot`; this module keeps a
separate, persistent *memory* of terrain the hero has already seen (the map is
the whole level, and unseen cells arrive blank -- unknown blanks are not
freely traversable floor).

The boundary detector is deterministic and separate from any model call.  For
Wave 1 the reflex uses it only for HP and hunger signals; the stable episode-
local event ids are what a later strategy tier would coalesce and dispatch.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from . import protocol
from .events import Boundary, BoundaryDetector, HUNGER_STAGES
from .instances import (T_ALTAR, T_BARS, T_BOULDER, T_CLOSED_DOOR,
                        T_CORRIDOR, T_DOORWAY, T_FLOOR, T_FOUNTAIN, T_LAVA,
                        T_OPEN_DOOR, T_STAIRS_DOWN, T_STAIRS_UP, T_TRAP,
                        T_TREE, T_UNKNOWN, T_WALL, T_WATER)

# Monster classes drawn with punctuation rather than a letter.  Taken from the
# engine's public glyph table (include/defsym.h MONSYM entries): golem ('),
# major demon (&), sea monster (;), lizard (:), long-worm tail (~) and mimic
# (]).  The ghost class is a space (i.e. a blank/omitted cell) and the human
# class is '@' (the hero or another human), so neither is a distinct monster
# glyph here.  '@' is classified per cell instead -- see monster_cell(): an
# '@' anywhere but the known hero square is a monster-class hazard.
MONSTER_PUNCTUATION = set("'&;:~]")

# Cells the hero provably cannot stand on.  Blank/unpainted is unknown, not
# floor; every monster class (letters and the punctuation classes above) is
# excluded so pathfinding never walks into an attack; boulders/statues ('`')
# and visible traps ('^') are avoided too.
NON_WALKABLE = set("|- ~@`^") | MONSTER_PUNCTUATION
for _ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
    NON_WALKABLE.add(_ch)

STAIRS_DOWN = ">"
STAIRS_UP = "<"

# ``HUNGER_STAGES`` and its ordering live in :mod:`tools.agent.events` with
# the rest of the boundary logic; the name is re-exported here for callers
# that already import it from state.

# Food the reflex is willing to eat.  This is a *known-safe allowlist*, not a
# keyword soup: a corpse, a tinned or simply unrecognised item is not assumed
# edible (corpse safety is uncertain, and unseen quantities are unknown), and
# a qualified item that only *contains* a safe word is rejected (a
# "cockatrice egg" is lethal, and is not the bare "egg" this list names).
#
# The names are the *exact canonical engine object names* from the FOOD_CLASS
# block of include/objects.h, never an abbreviation of them: the engine prints
# "lembas wafer", not "lembas", "cram ration", not "cram", and "kelp frond",
# not "kelp".  Matching the engine's own spelling is what keeps a hungry hero
# from refusing the only safe food it carries.
KNOWN_SAFE_FOOD = ("food ration", "tripe ration", "cram ration",
                   "lembas wafer", "kelp frond", "apple", "banana", "orange",
                   "melon", "fortune cookie", "candy bar", "cream pie",
                   "meatball", "egg")
UNSAFE_FOOD_MARKERS = ("corpse", "tinned", "unknown", "glop")

# An inventory row is built by the engine's doname(): an optional inventory
# selector ("d - ", "a) ", "f: "), a quantity or article, the blessed/cursed/
# uncursed and "partly eaten" qualifiers, the canonical name, an optional user
# " named <text>" suffix and an optional parenthesised shop annotation
# (" (unpaid, 45 zorkmids)").  Only these *recognised* wrappers are peeled
# off; whatever is left must then be exactly a canonical name or its exact
# plural.  The user text after " named " is removed whole before matching, so
# a name is never accepted because a *personal* name happens to contain a safe
# word, while a known-safe base keeps matching even when it is so named.
_FOOD_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][\s.\-)*:]{1,3}")
_FOOD_COUNT_RE = re.compile(r"^\d+\s+")
_FOOD_ARTICLES = ("a ", "an ", "the ", "some ")
_FOOD_QUALIFIERS = ("blessed ", "cursed ", "uncursed ", "partly eaten ")
_FOOD_NAMED_RE = re.compile(r"\s+named\s+.*\Z")
_FOOD_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*\Z")


def _food_forms() -> frozenset:
    """The allowlist extended with each item's exact displayed plural.

    Every canonical name pluralises with a plain trailing "s" in the engine's
    makeplural() ("food rations", "lembas wafers"), so only that exact form is
    added and a name is never singularised by a suffix rule -- an arbitrary
    string that merely contains a safe word still stays rejected.  The egg
    stays singular on purpose: only the bare "egg" is ever assumed edible.
    """
    forms = set(KNOWN_SAFE_FOOD)
    for name in KNOWN_SAFE_FOOD:
        if name != "egg":
            forms.add(name + "s")
    return frozenset(forms)


_KNOWN_SAFE_FORMS = _food_forms()


def _food_name(text) -> str:
    """The bare canonical name: no row prefix, quantity, qualifier or suffix.

    The recognised engine wrappers are peeled off in the order doname() builds
    them -- a trailing shop annotation, then the " named <text>" suffix, then
    the leading selector, quantity, article and blessed/cursed/partly-eaten
    qualifiers -- and the survivors are matched exactly against the allowlist.
    """
    low = (text or "").strip().lower()
    while _FOOD_PAREN_RE.search(low):
        low = _FOOD_PAREN_RE.sub("", low, count=1).strip()
    low = _FOOD_NAMED_RE.sub("", low, count=1).strip()
    low = _FOOD_SELECTOR_RE.sub("", low, count=1).strip()
    while True:
        shorter = _FOOD_COUNT_RE.sub("", low, count=1)
        for word in _FOOD_ARTICLES + _FOOD_QUALIFIERS:
            if shorter.startswith(word):
                shorter = shorter[len(word):]
                break
        shorter = shorter.strip()
        if shorter == low:
            return low
        low = shorter


def is_known_safe_food(text) -> bool:
    """True only for a recognised, unqualified, safe food name."""
    name = _food_name(text)
    if not name:
        return False
    if any(marker in name for marker in UNSAFE_FOOD_MARKERS):
        return False
    # an egg is safe only as the bare item: any egg qualified by a monster
    # name (a cockatrice egg, say) is potentially lethal and never assumed
    # edible, and even the bare item is not matched in its plural form
    if "egg" in name:
        return name == "egg"
    # what is left after the recognised metadata is peeled off must be exactly
    # a canonical name or its exact plural -- never a mere substring
    return name in _KNOWN_SAFE_FORMS


def passable(ch: str) -> bool:
    return bool(ch) and ch not in NON_WALKABLE


def monster_glyph(ch: str) -> bool:
    """True for any public monster-class glyph, letters or punctuation.

    Public appearance is ambiguous, so the reflex treats every monster class
    as a hazard: the alphabetic classes plus the punctuation classes in
    :data:`MONSTER_PUNCTUATION`.  '@' is the hero (and other humans) and the
    blank cell is not drawn, so neither is reported here.
    """
    if not ch or ch == "@":
        return False
    return ch.isalpha() or ch in MONSTER_PUNCTUATION


def monster_cell(ch: str, hero, pos) -> bool:
    """True for a monster-class hazard occupying the cell at *pos*.

    The hero is identified by the *known hero square*, never by the glyph:
    '@' is the hero and also every other human, so :func:`monster_glyph`
    deliberately does not report it.  An '@' on any cell other than the
    hero's own square, however, is a visible human-class monster and is a
    hazard exactly like any other monster class -- a hostile human standing
    beside the hero must reach the safety rules.
    """
    if ch == "@":
        return pos != hero
    return monster_glyph(ch)


def glyph_is_pet(ch: str) -> bool:
    """A deliberately conservative pet set.  Public appearance is ambiguous;
    the reflex treats every monster as a hazard and never walks into one."""
    return ch in ("d", "f", "u", "c")  # dog / feline / horse / pony-ish


@dataclass(frozen=True)
class Boundary(object):
    reason: str
    eid: str


class Status(object):
    def __init__(self) -> None:
        self.hp: Optional[int] = None
        self.hp_max: Optional[int] = None
        self.hunger: str = ""
        self.dlvl: str = ""
        self.time: Optional[int] = None
        self.gold: Optional[int] = None
        self.level: Optional[int] = None


def parse_status(snap: protocol.Snapshot) -> Status:
    text = snap.status_text()
    st = Status()
    st.hp = _int(text.get("hitpoints"))
    st.hp_max = _int(text.get("hitpoints-max"))
    st.hunger = (text.get("hunger") or "").strip()
    st.dlvl = (text.get("dungeon-level") or "").strip()
    st.time = _int(text.get("time"))
    gold = text.get("gold") or ""
    if gold.startswith("$:"):
        st.gold = _int(gold[2:])
    st.level = _int(text.get("experience-level"))
    return st


def _int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(s.strip())
    except (TypeError, ValueError):
        return None


def hero_position(snap: protocol.Snapshot) -> Optional[Tuple[int, int]]:
    """Locate the hero from the map ('@'), never from an arbitrary cursor."""
    for (x, y), cell in snap.map.items():
        if cell and cell[0] == "@":
            return (x, y)
    return None


def render_map(mem: "EpisodeMemory", legend: bool = True) -> str:
    """The remembered map as a 79x21 text block, with a coordinate legend.

    Only *publicly observed* terrain is drawn; an unseen cell is a blank.
    The legend pins the coordinate system so a strategy cannot misread a
    glyph position (the map is ``engine-map``: x=1..79, y=0..20).
    """
    lines = []
    for y in range(protocol.MAP_MIN_Y, protocol.MAP_MAX_Y + 1):
        row = "".join(mem.tile((x, y))
                      for x in range(protocol.MAP_MIN_X,
                                     protocol.MAP_MAX_X + 1))
        lines.append("%2d %s" % (y, row))
    if legend:
        lines.append("x=%d..%d y=%d..%d (x right, y down; '>' down stairs, "
                     "'<' up stairs, '@' hero)"
                     % (protocol.MAP_MIN_X, protocol.MAP_MAX_X,
                        protocol.MAP_MIN_Y, protocol.MAP_MAX_Y))
    return "\n".join(lines)


#: The exhaustive, snapshot-exact terrain-class -> canonical glyph table.
#: ``instances.TerrainMemory.terrain`` stores classification strings; every
#: class maps to exactly one glyph so the payload's one-key-per-emitted-
#: character legend claim stays true.  Orientation is lost for ``|-`` (the
#: legend wording already covers both orientations) and a doorway whose open
#: state is unknown renders as the closed-door glyph.
TERRAIN_GLYPHS = {
    T_FLOOR: ".",
    T_CORRIDOR: "#",
    T_WALL: "|",
    T_OPEN_DOOR: "-",
    T_CLOSED_DOOR: "+",
    T_DOORWAY: "+",
    T_STAIRS_DOWN: ">",
    T_STAIRS_UP: "<",
    T_TREE: "#",
    T_WATER: "}",
    T_LAVA: "}",
    T_TRAP: "^",
    T_BARS: "|",
    T_BOULDER: "0",
    T_FOUNTAIN: "{",
    T_ALTAR: "_",
    T_UNKNOWN: " ",
}

#: The single canonical marker every currently observed non-hero creature
#: renders as, whatever its raw glyph.  Raw monster glyphs are never emitted.
OCCUPANT_MARKER = "*"

#: The confirmed hero's own marker (final precedence).
HERO_MARKER = "@"


def bounded_map(terrain,
                hero: Optional[Tuple[int, int]],
                snapshot,
                stairs_down=(),
                stairs_up=()) -> Optional[dict]:
    """The bounded remembered-map crop for the presentation payload.

    Read-only, pure and deterministic.  The glyph of a cell comes from the
    *persistent classified* ``terrain`` (``instances.TerrainMemory``), so
    remembered ground survives under a current occupant; dynamic occupancy
    comes **only** from the current ``snapshot``, with every observed non-hero
    creature rendered as the single canonical :data:`OCCUPANT_MARKER`, and
    the confirmed hero cell rendered as :data:`HERO_MARKER` with final
    precedence.  ``EpisodeMemory.grid`` is never a glyph source (its raw cells
    are overwritten by occupants), and a stale remembered monster is never
    drawn as current.

    The crop is the union of the nonblank rendered glyphs, the confirmed hero
    and the remembered stairs, expanded by one blank margin and clamped to the
    protocol rectangle.  With no evidence at all the result is ``None``.
    """
    glyphs: Dict[Tuple[int, int], str] = {}
    if terrain is not None:
        for pos, klass in terrain.terrain.items():
            glyph = TERRAIN_GLYPHS.get(klass, " ")
            if glyph != " ":
                glyphs[pos] = glyph
    for pos in stairs_down:
        glyphs.setdefault(tuple(pos), ">")
    for pos in stairs_up:
        glyphs.setdefault(tuple(pos), "<")
    hero_pos = tuple(hero) if hero is not None else None
    cells = getattr(snapshot, "map", None) or {}
    for pos, cell in cells.items():
        glyph = cell[0] if cell else ""
        if not glyph or pos == hero_pos:
            continue
        if monster_cell(glyph, hero_pos, pos):
            glyphs[pos] = OCCUPANT_MARKER
    if hero_pos is not None:
        glyphs[hero_pos] = HERO_MARKER
    evidence = [pos for pos, glyph in glyphs.items() if glyph != " "]
    if not evidence:
        return None
    xs = [pos[0] for pos in evidence]
    ys = [pos[1] for pos in evidence]
    x_min = max(protocol.MAP_MIN_X, min(xs) - 1)
    x_max = min(protocol.MAP_MAX_X, max(xs) + 1)
    y_min = max(protocol.MAP_MIN_Y, min(ys) - 1)
    y_max = min(protocol.MAP_MAX_Y, max(ys) + 1)
    lines = []
    for y in range(y_min, y_max + 1):
        row = "".join(glyphs.get((x, y), " ")
                      for x in range(x_min, x_max + 1))
        lines.append("%2d %s" % (y, row))
    return {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max,
            "text": "\n".join(lines)}


class Inventory(object):
    """A cached, timestamped view of the last inventory rows the hero read."""

    def __init__(self) -> None:
        self.rows: List[dict] = []
        self.seen_tick: Optional[int] = None
        self.seen_time: Optional[int] = None

    def refresh(self, rows: List[dict], tick: int, game_time: Optional[int]):
        self.rows = list(rows)
        self.seen_tick = tick
        self.seen_time = game_time

    def stale(self, tick: int, max_age: int) -> bool:
        if self.seen_tick is None:
            return True
        return (tick - self.seen_tick) > max_age

    def food_rows(self) -> List[dict]:
        out = []
        for r in self.rows:
            if r.get("selectable") and is_known_safe_food(r.get("text")):
                out.append(r)
        return out

    def food_letters(self) -> List[str]:
        """Inventory letters (the first token of each row) for food rows."""
        letters = []
        for r in self.food_rows():
            text = r.get("text") or ""
            if text and text[0].isalnum():
                letters.append(text[0])
        return letters


@dataclass(frozen=True)
class StagedObservation(object):
    """A parsed-but-uncommitted observation (plan 3.4: parse is not commit).

    Produced by :meth:`EpisodeMemory.stage` and consumed exactly once by
    :meth:`EpisodeMemory.commit`.  Every field is a plain immutable value, so
    the controller can reconcile an in-flight attempt against it before any of
    it reaches durable memory.  ``hero_cells`` is *every* ``@`` cell of the
    frame, not just the first: hero identity is a possible-position set
    (plan 4.2), so the several-``@`` case must reach the resolver intact.
    """

    cells: Dict[Tuple[int, int], tuple]
    stairs_down: frozenset
    stairs_up: frozenset
    hero: Optional[Tuple[int, int]]
    status: "Status"
    messages: tuple
    hero_cells: tuple = ()


#: Sentinel for :meth:`EpisodeMemory.commit`: derive the hero square from the
#: staged observation (the compatibility behaviour for direct callers).  The
#: controller passes its reconciled :class:`instances.HeroResolution` instead,
#: so an unresolved/ambiguous frame clears ``mem.hero`` rather than adopting a
#: first ``@`` (plan 4.2).
DERIVED_HERO = object()


class _InstanceScope(object):
    """One level instance's *map-local* memory (plan 4.1 rule 6).

    A fresh allocation gets a brand-new empty scope: terrain, visits, stairs
    and the progress/recovery counters are all local, so no old-instance
    coordinate, visit or budget can leak across an arrival.
    """

    def __init__(self) -> None:
        self.grid: Dict[Tuple[int, int], tuple] = {}
        self.visits: Dict[Tuple[int, int], int] = {}
        self.stairs_down: Set[Tuple[int, int]] = set()
        self.stairs_up: Set[Tuple[int, int]] = set()
        self.hero: Optional[Tuple[int, int]] = None
        self.last_hero: Optional[Tuple[int, int]] = None
        self.no_progress = 0
        self.searches_since_progress = 0


class EpisodeMemory(object):
    """All mutable per-episode public memory.  Reset wholesale per episode.

    The map-local fields (terrain, visits, stairs, hero, progress counters)
    are
    *per level instance*: :meth:`begin_instance` swaps in a fresh empty scope
    (plan 4.1 rule 6).  The episode-scoped evidence that legitimately survives
    an arrival -- the inventory cache and message history -- stays on the
    object itself, because the engine's own inventory is unchanged by moving
    levels (plan 5.2).
    """

    def __init__(self) -> None:
        self.reset()
        self.tick = 0

    def reset(self) -> None:
        self.instance: Optional[int] = None
        self._scope = _InstanceScope()
        self.inventory = Inventory()
        self.status = Status()
        self.seen_msgs: Set[int] = set()
        self.boundary = BoundaryDetector()
        self.rejected_food = 0
        self.failed_moves = 0
        self.messages: List[str] = []

    def begin_instance(self, instance: Optional[int]) -> None:
        """Start a fresh level-instance scope with empty map-local memory."""
        self.instance = instance
        self._scope = _InstanceScope()

    # -- per-instance map-local memory (transparent to callers) ----------
    @property
    def grid(self) -> Dict[Tuple[int, int], tuple]:
        return self._scope.grid

    @grid.setter
    def grid(self, value: Dict[Tuple[int, int], tuple]) -> None:
        self._scope.grid = value

    @property
    def visits(self) -> Dict[Tuple[int, int], int]:
        return self._scope.visits

    @visits.setter
    def visits(self, value: Dict[Tuple[int, int], int]) -> None:
        self._scope.visits = value

    @property
    def stairs_down(self) -> Set[Tuple[int, int]]:
        return self._scope.stairs_down

    @stairs_down.setter
    def stairs_down(self, value: Set[Tuple[int, int]]) -> None:
        self._scope.stairs_down = value

    @property
    def stairs_up(self) -> Set[Tuple[int, int]]:
        return self._scope.stairs_up

    @stairs_up.setter
    def stairs_up(self, value: Set[Tuple[int, int]]) -> None:
        self._scope.stairs_up = value

    @property
    def hero(self) -> Optional[Tuple[int, int]]:
        return self._scope.hero

    @hero.setter
    def hero(self, value: Optional[Tuple[int, int]]) -> None:
        self._scope.hero = value

    @property
    def last_hero(self) -> Optional[Tuple[int, int]]:
        return self._scope.last_hero

    @last_hero.setter
    def last_hero(self, value: Optional[Tuple[int, int]]) -> None:
        self._scope.last_hero = value

    @property
    def no_progress(self) -> int:
        return self._scope.no_progress

    @no_progress.setter
    def no_progress(self, value: int) -> None:
        self._scope.no_progress = value

    @property
    def searches_since_progress(self) -> int:
        return self._scope.searches_since_progress

    @searches_since_progress.setter
    def searches_since_progress(self, value: int) -> None:
        self._scope.searches_since_progress = value

    def observe(self, snap: protocol.Snapshot) -> None:
        """Fold one applied snapshot into durable memory.

        Kept as the composition of :meth:`stage` and :meth:`commit` so a
        caller (the controller) that must reconcile an in-flight attempt
        *before* memory commits can stage the parse, reconcile, then commit
        (plan 3.4).  Messages stay event-id deduplicated.
        """
        self.commit(self.stage(snap))

    def stage(self, snap: protocol.Snapshot) -> "StagedObservation":
        """Parse one snapshot into a temporary presentation (no mutation).

        Everything here is pure: terrain cells, stairs, the hero square, the
        parsed status and the *new* event-id-deduplicated messages are read
        out without touching durable memory, so the caller can reconcile the
        in-flight attempt and only then commit.
        """
        cells: Dict[Tuple[int, int], tuple] = {}
        stairs_down: Set[Tuple[int, int]] = set()
        stairs_up: Set[Tuple[int, int]] = set()
        hero_cells: List[Tuple[int, int]] = []
        for pos, cell in snap.map.items():
            cells[pos] = cell
            if cell and cell[0] == STAIRS_DOWN:
                stairs_down.add(pos)
            elif cell and cell[0] == STAIRS_UP:
                stairs_up.add(pos)
            if cell and cell[0] == "@":
                hero_cells.append(pos)
        messages = []
        for m in snap.msg or []:
            e = m.get("e")
            if e is not None and e not in self.seen_msgs:
                messages.append((e, m.get("text") or ""))
        return StagedObservation(
            cells=cells, stairs_down=frozenset(stairs_down),
            stairs_up=frozenset(stairs_up), hero=hero_position(snap),
            status=parse_status(snap), messages=tuple(messages),
            hero_cells=tuple(sorted(hero_cells)))

    def commit(self, staged: "StagedObservation",
               hero=DERIVED_HERO) -> None:
        """Fold a staged observation into durable memory (the one commit).

        *hero* is the reconciled confirmed singleton the controller resolved
        from the prior position set and the matched attempt (plan 4.2); the
        sentinel :data:`DERIVED_HERO` keeps the direct-caller compatibility of
        deriving it from the staged frame.  An explicit ``None`` -- an
        unresolved or ambiguous frame -- *clears* ``mem.hero`` so movement,
        stair/door actions and forced search stay suppressed until identity is
        positively confirmed, and an unknown hero earns no visit credit.
        """
        for pos, cell in staged.cells.items():
            self.grid[pos] = cell
        self.stairs_down |= set(staged.stairs_down)
        self.stairs_up |= set(staged.stairs_up)
        hero = staged.hero if hero is DERIVED_HERO else hero
        self.hero = hero
        if hero is not None:
            if hero == self.last_hero:
                self.no_progress += 1
            else:
                self.no_progress = 0
                self.searches_since_progress = 0
            self.last_hero = hero
            self.visits[hero] = self.visits.get(hero, 0) + 1
        self.status = staged.status
        for e, text in staged.messages:
            if e not in self.seen_msgs:
                self.seen_msgs.add(e)
                self.messages.append(text)
        if len(self.messages) > 200:
            self.messages = self.messages[-200:]

    def recent_messages(self, n: int = 4) -> List[str]:
        return self.messages[-n:]

    def tile(self, pos: Tuple[int, int]) -> str:
        cell = self.grid.get(pos)
        return cell[0] if cell else " "

    def known_passable(self, pos: Tuple[int, int]) -> bool:
        return pos in self.grid and passable(self.tile(pos))

    # -- Wave-2 boundary inputs (all derived from public state only) ------
    def visible_classes(self) -> Set[str]:
        """Monster-class glyphs currently visible on the presented map.

        The *presentation* is per observation, so this is what the hero can
        see right now; the detector turns a first sighting into one stable
        episode-local novelty event.
        """
        out: Set[str] = set()
        for cell in self.grid.values():
            ch = cell[0] if cell else ""
            if ch and ch != "@" and monster_glyph(ch):
                out.add(ch)
        return out

    def inventory_signature(self) -> Optional[Tuple[str, ...]]:
        """A stable signature of the last inventory list the hero read.

        ``None`` until an inventory has actually been read, so the first read
        is a baseline rather than a change.
        """
        if self.inventory.seen_tick is None:
            return None
        names = []
        for r in self.inventory.rows:
            text = (r.get("text") or "").strip().lower()
            if text:
                names.append(" ".join(text.split()))
        return tuple(names)

    def failed_food_count(self) -> int:
        """How many times the engine rejected a food intent this episode."""
        return sum(1 for m in self.messages
                   if "don't have that object" in m.lower())

