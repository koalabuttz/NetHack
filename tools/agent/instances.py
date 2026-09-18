"""Deterministic level-instance, terrain and hero memory (wave 2 core).

This module is the pure heart of the plan's section 4: the level-instance
automaton (4.1), :class:`HeroResolution` sets (4.2) and the full-cell
terrain/occupancy split (4.3).  Everything here is derived from public
encodings and is deterministic -- no wall clock, no RNG, no engine identity
invention.

The decisive rule it exists to enforce: a transition signal never merges two
levels.  Arrival that cannot be disproved fails closed to a **fresh**
instance scope, and a fresh scope expires the old instance's targets,
directives and continuations rather than carrying them across.

It imports the standard library only, so any layer may use it.
"""

from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Tuple

# -- full-cell terrain classification (section 4.3, M01/M02/M03) -----------

T_FLOOR = "floor"
T_CORRIDOR = "corridor"
T_WALL = "wall"
T_DOORWAY = "doorway"
T_OPEN_DOOR = "open-door"
T_CLOSED_DOOR = "closed-door"
T_STAIRS_DOWN = "stairs-down"
T_STAIRS_UP = "stairs-up"
T_BARS = "bars"
T_TREE = "tree"
T_WATER = "water"
T_LAVA = "lava"
T_TRAP = "trap"
T_BOULDER = "boulder"
T_FOUNTAIN = "fountain"
T_ALTAR = "altar"
T_UNKNOWN = "unknown"

#: Terrain the hero may stand on without a prior action.  A closed door is
#: deliberately absent: it must be opened first (4.5).
WALKABLE = frozenset((T_FLOOR, T_CORRIDOR, T_DOORWAY, T_OPEN_DOOR,
                      T_STAIRS_DOWN, T_STAIRS_UP))

#: Terrain that is remembered even while an occupant covers it.
FIXED_TERRAIN = frozenset((T_WALL, T_STAIRS_DOWN, T_STAIRS_UP, T_CLOSED_DOOR,
                           T_TREE, T_BARS, T_FOUNTAIN, T_ALTAR))

# Monster classes drawn with punctuation (include/defsym.h MONSYM entries).
MONSTER_PUNCTUATION = frozenset("'&;:~]")

_ALPHA = frozenset("abcdefghijklmnopqrstuvwxyz"
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ")

OCC_NONE = "none"
OCC_MONSTER = "monster"
OCC_HERO_OR_HUMAN = "hero-or-human"


def is_monster_glyph(glyph: str) -> bool:
    """True for a public monster-class glyph (letters or punctuation)."""
    if not glyph or glyph == "@":
        return False
    return glyph in _ALPHA or glyph in MONSTER_PUNCTUATION


@dataclass(frozen=True)
class Cell(object):
    """One classified public cell: terrain plus current occupant.

    ``terrain`` is what this observation *proves about the ground*;
    ``occupant`` is who is standing on it.  An occupant never erases the
    remembered terrain (4.3), and an unknown glyph fails closed to
    :data:`T_UNKNOWN` rather than assuming floor.
    """

    terrain: str
    occupant: str = OCC_NONE

    @property
    def walkable(self) -> bool:
        return self.terrain in WALKABLE and self.occupant == OCC_NONE

    @property
    def hazardous(self) -> bool:
        return self.occupant != OCC_NONE


def classify_cell(glyph: str, color: str = "", style: str = "",
                  other: str = "") -> Cell:
    """Classify one ``(glyph, color, style, other)`` tuple under the pin.

    Implements the reviewer-confirmed door semantics (plan 2.1, corroborated
    by ``include/defsym.h``): gray ``-``/``|`` are walls, **brown**
    ``-``/``|``
    are open doors and **brown** ``+`` is a closed door.  A glyph-only
    classification would reverse important behaviour, so the color is load
    bearing and an unrecognised variant fails closed.
    """
    if not glyph or glyph == " ":
        # a blank cell is unknown, never assumed floor (M02)
        return Cell(T_UNKNOWN)
    if glyph == "@":
        return Cell(T_UNKNOWN, OCC_HERO_OR_HUMAN)
    if is_monster_glyph(glyph):
        return Cell(T_UNKNOWN, OCC_MONSTER)
    if glyph == ">":
        return Cell(T_STAIRS_DOWN)
    if glyph == "<":
        return Cell(T_STAIRS_UP)
    if glyph in "-|":
        # color decides wall vs open door (M01)
        if color == "brown":
            return Cell(T_OPEN_DOOR)
        return Cell(T_WALL)
    if glyph == "+":
        # brown is the closed door the profile pins (M01)
        if color == "brown":
            return Cell(T_CLOSED_DOOR)
        return Cell(T_WALL)
    if glyph == ".":
        # room floor and a gray doorway are both walkable
        return Cell(T_FLOOR)
    if glyph == "#":
        if color == "green":
            return Cell(T_TREE)
        if color in ("gray", "brown", "metal", "none", ""):
            return Cell(T_CORRIDOR)
        return Cell(T_UNKNOWN)
    if glyph == "{":
        # white sink; otherwise a fountain (bright blue).  Neither is safe
        # footing, so anything else fails closed.
        if color in ("brightblue", "brightcyan", "cyan", "blue"):
            return Cell(T_FOUNTAIN)
        return Cell(T_UNKNOWN)
    if glyph == "}":
        if color == "red":
            return Cell(T_LAVA)
        if color == "orange":
            return Cell(T_LAVA)
        return Cell(T_WATER)
    if glyph == "_":
        return Cell(T_ALTAR)
    if glyph == "`":
        return Cell(T_BOULDER)
    if glyph == "^":
        return Cell(T_TRAP)
    # anything else is not proved traversable
    return Cell(T_UNKNOWN)


# -- terrain memory (section 4.3) ------------------------------------------

class TerrainMemory(object):
    """Per-instance persistent terrain, current occupancy and revisions.

    Terrain is remembered across observations; occupancy is replaced every
    observation.  ``map_revision`` increments only for changed *structural or
    unknown* evidence, and ``occupancy_generation`` tracks dynamic blockers
    (4.3), so unrelated discoveries do not churn the map identity.
    """

    def __init__(self) -> None:
        self.terrain: Dict[Tuple[int, int], str] = {}
        self.occupancy: Dict[Tuple[int, int], str] = {}
        self.floor_objects: Dict[Tuple[int, int], str] = {}
        self.map_revision = 0
        self.occupancy_generation = 0

    def merge(self, cells: Dict[Tuple[int, int], tuple]) -> None:
        """Fold one observation's ``{pos: (glyph, color, style, other)}``.

        Structure changes bump :attr:`map_revision`; an occupant overlay
        changes only the occupancy generation and never erases remembered
        terrain.
        """
        new_occ: Dict[Tuple[int, int], str] = {}
        structural = False
        for pos, raw in cells.items():
            glyph = raw[0] if raw else " "
            color = raw[1] if len(raw) > 1 else ""
            style = raw[2] if len(raw) > 2 else ""
            other = raw[3] if len(raw) > 3 else ""
            c = classify_cell(glyph, color, style, other)
            if c.occupant != OCC_NONE:
                new_occ[pos] = c.occupant
            # Unknown-from-occupancy keeps the remembered terrain; a proven
            # terrain (including an unknown *blank*) updates it.
            if c.terrain != T_UNKNOWN or pos not in self.terrain:
                if self.terrain.get(pos) != c.terrain:
                    structural = True
                self.terrain[pos] = c.terrain
        if new_occ != self.occupancy:
            self.occupancy = new_occ
            self.occupancy_generation += 1
        if structural:
            self.map_revision += 1

    def ter(self, pos: Tuple[int, int]) -> str:
        return self.terrain.get(pos, T_UNKNOWN)

    def occupant(self, pos: Tuple[int, int]) -> str:
        return self.occupancy.get(pos, OCC_NONE)

    def walkable(self, pos: Tuple[int, int]) -> bool:
        """True only for proved walkable ground with no occupant."""
        terrain = self.terrain.get(pos, T_UNKNOWN)
        return (terrain in WALKABLE
                and self.occupancy.get(pos, OCC_NONE) == OCC_NONE)

    def stairs_down(self) -> FrozenSet[Tuple[int, int]]:
        return frozenset(p for p, t in self.terrain.items()
                         if t == T_STAIRS_DOWN)

    def stairs_up(self) -> FrozenSet[Tuple[int, int]]:
        return frozenset(p for p, t in self.terrain.items()
                         if t == T_STAIRS_UP)


def structural_delta(old: TerrainMemory,
                     new_terrain: Dict[Tuple[int, int], str]
                     ) -> Tuple[bool, Tuple[Tuple[int, int], str], ...]:
    """The stable-terrain conflicts between two scopes (D signal, 4.1).

    Compares only reviewed fixed terrain (walls, doors, stairs, trees, bars)
    so occupants, lighting and expected local open-door changes are ignored.
    Any unexplained conflict is sufficient to suspect a fresh instance; there
    is no arbitrary percentage threshold.
    """
    conflicts: List[Tuple[Tuple[int, int], str]] = []
    for pos, klass in new_terrain.items():
        if klass not in FIXED_TERRAIN:
            continue
        if pos in old.terrain and old.terrain[pos] in FIXED_TERRAIN \
                and old.terrain[pos] != klass:
            conflicts.append((pos, klass))
    conflicts.sort()
    return (bool(conflicts), tuple(conflicts))


# -- hero resolution (section 4.2) -----------------------------------------

H_STATUS_CONFIRMED = "confirmed"
H_STATUS_POSSIBLE_SET = "possible-set"
H_STATUS_UNKNOWN = "unknown"
H_STATUS_OUTSIDE = "outside-candidates"


@dataclass(frozen=True)
class HeroResolution(object):
    """The set of positions the hero may occupy, never a single first-``@``.

    ``confirmed`` is set only by positively supported singleton continuity;
    ``possible`` is always the full evidence-supported set.  ``outside``
    records an explicit "could be somewhere not in the set" possibility, so
    an empty set never masquerades as certainty.
    """

    status: str
    confirmed: Optional[Tuple[int, int]] = None
    possible: FrozenSet[Tuple[int, int]] = frozenset()
    outside: bool = False
    evidence: str = ""
    observation_generation: int = 0

    @property
    def resolved(self) -> bool:
        if self.status != H_STATUS_CONFIRMED:
            return False
        return self.confirmed is not None

    @property
    def suppress_movement(self) -> bool:
        """Unresolved identity suppresses movement, stairs, forced search."""
        return not self.resolved


def kind_at_cells(glyph_counts: Dict[str, int]) -> str:
    """Classify a frame's ``@`` population for bootstrap decisions."""
    n = glyph_counts.get("@", 0)
    if n == 0:
        return "zero"
    if n == 1:
        return "unique"
    return "multiple"


def bootstrap_hero(at_cells: Tuple[Tuple[int, int], ...],
                   coherent: bool = True,
                   generation: int = 0) -> HeroResolution:
    """Allocate a hero resolution from an initial observation (4.2).

    A unique ``@`` on a coherent command boundary bootstraps a confirmed
    singleton; zero or multiple ``@`` cells remain a set (or unknown) and
    suppress movement.  There is deliberately no "nearest ``@``" fallback.
    """
    cells = frozenset(at_cells)
    if not coherent:
        return HeroResolution(H_STATUS_UNKNOWN, None, cells, True,
                              "incoherent presentation", generation)
    if len(cells) == 0:
        return HeroResolution(H_STATUS_UNKNOWN, None, frozenset(), True,
                              "no @ cell", generation)
    if len(cells) == 1:
        only = next(iter(cells))
        return HeroResolution(H_STATUS_CONFIRMED, only, cells, False,
                              "unique @ at a coherent boundary", generation)
    return HeroResolution(H_STATUS_POSSIBLE_SET, None, cells, False,
                          "multiple @ cells", generation)


@dataclass(frozen=True)
class MovementEvidence(object):
    """Public evidence about one matched attempt's movement (4.2)."""

    nonmovement: bool = False        # explicit rejected/no-time nonmovement
    time_advanced: bool = False      # same position, time spent
    expected: Optional[Tuple[int, int]] = None
    unexpected: bool = False         # teleport / incompatible presentation
    coherent: bool = True


def reconcile_hero(prior: HeroResolution, ev: MovementEvidence,
                   at_cells: Tuple[Tuple[int, int], ...],
                   generation: int) -> HeroResolution:
    """Reconcile the prior hero set with one observation (4.2).

    Preserves the old confirmed position on explicit nonmovement, confirms
    an expected destination only with consistent evidence, and otherwise
    *expands or retains* the possibility set instead of forcing a winner.
    """
    cells = frozenset(at_cells)
    union = frozenset(prior.possible) | cells
    if ev.expected is not None:
        union = union | {ev.expected}
    if ev.nonmovement and ev.coherent and prior.confirmed is not None:
        return replace(prior, evidence="rejected/no-time nonmovement",
                       observation_generation=generation)
    if not ev.coherent or ev.unexpected:
        # an unexpected square/teleport invalidates the expected-move
        # inference and expands the possibilities
        return HeroResolution(H_STATUS_POSSIBLE_SET, None, union, True,
                              "unexpected relocation", generation)
    if ev.expected is not None and frozenset((ev.expected,)) == cells:
        return HeroResolution(H_STATUS_CONFIRMED, ev.expected, cells, False,
                              "expected destination confirmed", generation)
    if len(cells) == 1:
        # A unique @ in a coherent frame is positive support for the hero
        # (plan 4.2: a unique @ at a coherent boundary bootstraps).  This also
        # re-confirms after a transient unresolved frame, but never picks a
        # first/nearest @ out of several -- the multiple-@ case above stays a
        # possibility set.
        only = next(iter(cells))
        return HeroResolution(H_STATUS_CONFIRMED, only, cells, False,
                              "single consistent @", generation)
    status = H_STATUS_POSSIBLE_SET if union else H_STATUS_UNKNOWN
    return HeroResolution(status, None, union, prior.outside,
                          "unresolved movement", generation)


# -- level-instance automaton (section 4.1) --------------------------------

# Automaton states.
UNBOUND = "UNBOUND"
ACTIVE = "ACTIVE"
PENDING = "PENDING"
FRESH_UNRESOLVED = "FRESH_UNRESOLVED"
STOPPED = "STOPPED"

# Transition signals.
S_STAIR = "S"      # stair/ladder ascend or descend successfully sent
S_OUTCOME = "O"    # public trapdoor/hole/levelport arrival outcome
S_LABEL = "L"      # displayed-level label changed
S_DISCONT = "D"    # structural/position discontinuity
S_NOARRIVAL = "N"  # affirmative no-arrival proof


@dataclass(frozen=True)
class InstanceState(object):
    """The externally visible state of the automaton."""

    state: str
    instance_id: Optional[int]
    token: str
    evidence: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingTransition(object):
    """An unsettled possible arrival: old scope frozen, token open."""

    old_id: int
    token: str
    evidence: Tuple[str, ...]
    hero_resolved: bool = True


class LevelInstanceAutomaton(object):
    """Episode-local monotonic level-instance allocation (4.1).

    Displayed ``Dlvl`` is metadata, never an identity.  There is **no**
    ``strong_public_match``, archived-map reuse or cross-instance merge in
    implementation 1.  Signals are supplied by the caller from a matched
    sent attempt plus the temporary observation -- never from a historical
    substring scan.
    """

    def __init__(self) -> None:
        self.state = UNBOUND
        self.instance_id: Optional[int] = None
        self.pending: Optional[PendingTransition] = None
        self._next_id = 1
        self._token_seq = 0
        self.events: List[Tuple[str, int, str]] = []

    # -- helpers ---------------------------------------------------------
    def _token(self) -> str:
        self._token_seq += 1
        return "t%d" % self._token_seq

    def _allocate(self) -> int:
        self.instance_id = self._next_id
        self._next_id += 1
        return self.instance_id

    def _note(self, what: str, iid: int, why: str) -> None:
        self.events.append((what, iid, why))

    # -- lifecycle -------------------------------------------------------
    def begin_playable(self) -> int:
        """From UNBOUND, the first valid playable observation allocates."""
        if self.state not in (UNBOUND,):
            raise ValueError("begin_playable in state %r" % (self.state,))
        iid = self._allocate()
        self.state = ACTIVE
        self._note("allocate", iid, "first playable observation")
        return iid

    def stop(self) -> None:
        """No further gameplay commits."""
        self.state = STOPPED

    def current(self) -> Optional[int]:
        """The current gameplay instance, or ``None`` while unresolved."""
        if self.state in (ACTIVE, FRESH_UNRESOLVED):
            return self.instance_id
        return None

    def active(self) -> bool:
        return self.state in (ACTIVE, FRESH_UNRESOLVED)

    # -- signals ---------------------------------------------------------
    def note_transition_sent(self, transition: bool) -> Optional[str]:
        """`S`: a stair/ladder action was successfully sent (4.1 rule 3).

        Creates PENDING after a successful send; no old-map outcome effects
        are committed yet.  A *cancelled* proposal that was never sent never
        reaches here and therefore creates no transition.
        """
        if not transition or self.state == STOPPED:
            return None
        self.pending = PendingTransition(self.instance_id, self._token(),
                                         (S_STAIR,))
        self.state = PENDING
        return self.pending.token

    def observe(self, signals: Tuple[str, ...], hero_usable: bool,
                prior_id: Optional[int] = None) -> InstanceState:
        """Apply one post-action observation and settle any transition.

        Implements the priority rules of 4.1 rules 4-8.  ``signals`` is the
        subset of ``{S, O, L, D}`` present, plus ``N`` for affirmative
        no-arrival.  Arrival that cannot be disproved fails closed to a
        fresh instance.
        """
        if self.state == STOPPED:
            return self.snapshot()
        positive = tuple(s for s in signals if s in
                         (S_STAIR, S_OUTCOME, S_LABEL, S_DISCONT))
        no_arrival = S_NOARRIVAL in signals
        if self.state == UNBOUND:
            if hero_usable:
                self.begin_playable()
            return self.snapshot()
        if self.state == FRESH_UNRESOLVED:
            # a follow-up observation completes the same fresh arrival; a new
            # independent signal opens a new token instead of reallocating
            if positive and not no_arrival:
                if self.pending is None:
                    self.pending = PendingTransition(self.instance_id,
                                                     self._token(), positive)
            if hero_usable:
                self.state = ACTIVE
                self.pending = None
            return self.snapshot()
        # ACTIVE or PENDING
        if self.pending is None and positive:
            self.pending = PendingTransition(self.instance_id, self._token(),
                                             positive)
            self.state = PENDING
        if self.pending is None:
            return self.snapshot()
        # There is a possible arrival; settle it now.
        if no_arrival and not positive:
            old = self.pending.old_id
            self.pending = None
            self.state = ACTIVE
            self._note("no-arrival", old, "N established")
            return self.snapshot()
        # Otherwise allocate one fresh instance, even if evidence is
        # contradictory, the label is unchanged, or there are zero/multiple
        # @ cells: arrival that cannot be disproved fails closed.
        old = self.pending.old_id
        iid = self._allocate()
        self.pending = PendingTransition(old, self._token(), positive)
        self.state = ACTIVE if hero_usable else FRESH_UNRESOLVED
        self._note("fresh", iid, "arrival not disproved")
        return self.snapshot()

    def timeout(self) -> InstanceState:
        """Bounded timeout with no affirmative no-arrival proof (rule 8).

        Retires the old active scope: a later resumption does so in a newly
        allocated unresolved instance before any observation commit, and a
        stale callback cannot reactivate the old scope.
        """
        if self.state == STOPPED:
            return self.snapshot()
        iid = self._allocate()
        self.pending = PendingTransition(
            self.instance_id if self.instance_id is not None else 0,
            self._token(), ("timeout",))
        self.state = FRESH_UNRESOLVED
        self._note("fresh", iid, "timeout without no-arrival proof")
        return self.snapshot()

    def snapshot(self) -> InstanceState:
        ev = self.pending.evidence if self.pending else ()
        return InstanceState(self.state, self.instance_id,
                             self.pending.token if self.pending else "", ev)
