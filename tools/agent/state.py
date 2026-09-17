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

HUNGER_STAGES = ("Hungry", "Weak", "Fainting", "Fainted", "Starved")

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


class EpisodeMemory(object):
    """All mutable per-episode public memory.  Reset wholesale per episode."""

    def __init__(self) -> None:
        self.reset()
        self.tick = 0

    def reset(self) -> None:
        self.grid: Dict[Tuple[int, int], tuple] = {}
        self.visits: Dict[Tuple[int, int], int] = {}
        self.hero: Optional[Tuple[int, int]] = None
        self.stairs_down: Set[Tuple[int, int]] = set()
        self.stairs_up: Set[Tuple[int, int]] = set()
        self.inventory = Inventory()
        self.status = Status()
        self.seen_msgs: Set[int] = set()
        self.boundary = BoundaryDetector()
        self.rejected_food = 0
        self.failed_moves = 0
        self.searches_since_progress = 0
        self.last_hero: Optional[Tuple[int, int]] = None
        self.no_progress = 0
        self.messages: List[str] = []

    def observe(self, snap: protocol.Snapshot) -> None:
        """Fold one applied snapshot into durable memory."""
        for pos, cell in snap.map.items():
            self.grid[pos] = cell
            if cell and cell[0] == STAIRS_DOWN:
                self.stairs_down.add(pos)
            elif cell and cell[0] == STAIRS_UP:
                self.stairs_up.add(pos)
        hero = hero_position(snap)
        if hero is not None:
            if hero == self.last_hero:
                self.no_progress += 1
            else:
                self.no_progress = 0
                self.searches_since_progress = 0
            self.last_hero = hero
            self.hero = hero
            self.visits[hero] = self.visits.get(hero, 0) + 1
        self.status = parse_status(snap)
        for m in snap.msg or []:
            e = m.get("e")
            if e is not None and e not in self.seen_msgs:
                self.seen_msgs.add(e)
                self.messages.append(m.get("text") or "")
        if len(self.messages) > 200:
            self.messages = self.messages[-200:]

    def recent_messages(self, n: int = 4) -> List[str]:
        return self.messages[-n:]

    def tile(self, pos: Tuple[int, int]) -> str:
        cell = self.grid.get(pos)
        return cell[0] if cell else " "

    def known_passable(self, pos: Tuple[int, int]) -> bool:
        return pos in self.grid and passable(self.tile(pos))


class BoundaryDetector(object):
    """Deterministic boundary events with stable episode-local ids."""

    HP_CRISIS_LOW = 0.30
    HP_CRISIS_HIGH = 0.50

    def __init__(self) -> None:
        self._armed_hp = True
        self._last_level: Optional[str] = None
        self._last_hunger = ""

    def check(self, st: Status, closed: bool = False) -> List[Boundary]:
        out: List[Boundary] = []
        if st.dlvl and st.dlvl != self._last_level:
            reason = "initial-level" if self._last_level is None \
                else "level-change"
            out.append(Boundary(reason, "level:%s" % st.dlvl))
            self._last_level = st.dlvl
        if st.hp is not None and st.hp_max:
            frac = st.hp / float(st.hp_max)
            if self._armed_hp and frac <= self.HP_CRISIS_LOW:
                out.append(Boundary("hp-crisis", "hp-crisis:%d" % st.hp_max))
                self._armed_hp = False
            elif not self._armed_hp and frac > self.HP_CRISIS_HIGH:
                self._armed_hp = True
        stage = self._hunger_stage(st.hunger)
        if stage and stage != self._last_hunger:
            out.append(Boundary("hunger-%s" % stage.lower(),
                                "hunger:%s" % stage))
            self._last_hunger = stage
        if closed:
            out.append(Boundary("closed", "closed"))
        return out

    @staticmethod
    def _hunger_stage(hunger: str) -> str:
        for stage in HUNGER_STAGES:
            if hunger.startswith(stage):
                return stage
        return ""
