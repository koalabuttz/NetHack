"""One-Dijkstra navigation and persisted navigation targets (wave 3).

This is the pure heart of the plan's section 4.5.  It replaces the old
nearest-stair loop in ``policy.py`` (a Manhattan prefilter plus an
eight-target cap) with **one** Dijkstra from the confirmed hero over
known-safe legal edges, after which *every* reachable target is considered
before any action candidate
is bounded.  Nothing here sends a wire action, mutates gameplay memory or
imports a provider: it is a derived value computed from a
:class:`tools.agent.instances.TerrainMemory`.

Design rules taken directly from the plan:

* positive integer base cost with capped visit/failed-edge penalties, so the
  search is deterministic and never depends on a wall clock or RNG;
* closed doors are not traversable -- a closed door is an *approach* target
  whose future floor is not in the graph until it is observed;
* diagonal door entry/exit and unsupported corner squeezing are illegal edges;
* a target persists only while its instance, approach and blockers still hold,
  and cycle recovery invalidates it.

The module imports the standard library and :mod:`tools.agent.instances` only,
so :mod:`tools.agent.policy` (and any later layer) may depend on it without a
cycle.
"""

import heapq
from dataclasses import dataclass, replace
from typing import Dict, FrozenSet, Optional, Sequence, Tuple

from .instances import (OCC_NONE, T_CLOSED_DOOR, T_DOORWAY, T_OPEN_DOOR,
                        T_STAIRS_DOWN, T_UNKNOWN, WALKABLE, TerrainMemory)

#: The eight wire directions in exactly the ``protocol.DIR_KEYS`` order.  The
#: rank is the deterministic tie-break for two equal-cost paths.
DIRECTIONS = ((-1, -1), (0, -1), (1, -1), (-1, 0),
              (1, 0), (-1, 1), (0, 1), (1, 1))
DIR_RANK = {d: i for i, d in enumerate(DIRECTIONS)}

#: Integer edge costs.  ``BASE_STEP`` is the positive base; the penalties are
#: capped so a repeatedly visited or previously failed cell is discouraged but
#: can never dominate a genuinely new route (section 3.3).
BASE_STEP = 100
VISIT_PENALTY = 30
VISIT_CAP = 3
FAILED_PENALTY = 20
FAILED_CAP = 2

INF = float("inf")

# Target families, in the plan's precedence order for navigation.
TFAM_STAIR = "stair"
TFAM_DOOR = "door"
TFAM_FRONTIER = "frontier"
TFAM_UNVISITED = "unvisited"

_DOOR_TERRAIN = (T_DOORWAY, T_OPEN_DOOR)


def _is_diagonal(step: Tuple[int, int]) -> bool:
    return step[0] != 0 and step[1] != 0


def door_open(terrain: "TerrainMemory", pos: Tuple[int, int]) -> bool:
    """True when *pos* is a doorway or an open door (the door opened)."""
    return terrain.ter(pos) in _DOOR_TERRAIN


def local_evidence_signature(terrain: "TerrainMemory",
                             pos: Tuple[int, int]) -> tuple:
    """A bounded *local* evidence signature at *pos* (plan 1.5).

    Player-visible classified terrain, occupancy and door state in the
    immediate neighbourhood only.  Suppression is compared under this
    signature, so an unrelated global-map change (a room discovered elsewhere)
    can never reopen a serviced or failed site, while a relevant local change
    (the ground, an occupant or a door here) does.
    """
    pos = tuple(pos)
    out = [terrain.ter(pos)]
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        nb = (pos[0] + dx, pos[1] + dy)
        out.append(terrain.ter(nb))
        out.append("occ" if terrain.occupant(nb) != OCC_NONE else "")
    return tuple(out)


def service_signature(terrain: "TerrainMemory", pos: Tuple[int, int]) -> tuple:
    """Target-relevant *exploration* evidence at *pos* (stall-recovery §4).

    Classified terrain (including unknown/door state) at the site and its four
    cardinals -- but **never** occupancy, time or visit counts.  A serviced
    waypoint therefore reopens only on an actual exploration-relevant local
    change, not because a monster wandered past it.
    """
    pos = tuple(pos)
    out = [terrain.ter(pos)]
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        out.append(terrain.ter((pos[0] + dx, pos[1] + dy)))
    return tuple(out)


def door_failure_signature(terrain: "TerrainMemory", pos: Tuple[int, int],
                           refusal: str = "") -> tuple:
    """The target door's own terrain and its target-bound refusal evidence.

    Never neighbouring occupancy (stall-recovery §4): a wandering nearby
    monster must not reset an identical closed-door failure.
    """
    pos = tuple(pos)
    return (terrain.ter(pos), str(refusal or ""))


def blocked_edge_signature(terrain: "TerrainMemory", src: Tuple[int, int],
                           dst: Tuple[int, int]) -> tuple:
    """The exact failed edge/action and its legality-relevant cells (§4).

    The destination's terrain and occupancy, plus -- for a diagonal edge -- the
    two orthogonal side cells :func:`edge_legal` reads, so the edge reopens
    only when that specific edge or its legality cells change, not on unrelated
    occupancy movement elsewhere.
    """
    src = tuple(src)
    dst = tuple(dst)
    step = (dst[0] - src[0], dst[1] - src[1])
    out = [terrain.ter(dst), terrain.occupant(dst)]
    if step[0] != 0 and step[1] != 0:
        side_a = (src[0] + step[0], src[1])
        side_b = (src[0], src[1] + step[1])
        out.extend([terrain.ter(side_a), terrain.occupant(side_a),
                    terrain.ter(side_b), terrain.occupant(side_b)])
    return tuple(out)


def edge_legal(terrain: TerrainMemory, a: Tuple[int, int],
               b: Tuple[int, int]) -> bool:
    """True when a *known-safe* edge exists from *a* to *b* (4.5).

    ``b`` must be proved walkable ground with no occupant.  A diagonal move is
    additionally illegal when it enters or leaves a doorway/open-door cell, or
    when both orthogonal side cells are non-walkable (a corner squeeze the
    engine will not allow).  The check is deliberately stricter than the glyph
    table: an unknown neighbour is never a legal side.
    """
    if not terrain.walkable(b):
        return False
    step = (b[0] - a[0], b[1] - a[1])
    if not _is_diagonal(step):
        return True
    if terrain.ter(a) in _DOOR_TERRAIN or terrain.ter(b) in _DOOR_TERRAIN:
        return False
    side_a = (a[0] + step[0], a[1])
    side_b = (a[0], a[1] + step[1])
    if not terrain.walkable(side_a) and not terrain.walkable(side_b):
        return False
    return True


def _edge_cost(terrain: TerrainMemory, pos: Tuple[int, int],
               visits: Dict[Tuple[int, int], int],
               failed: Dict[Tuple[int, int], int]) -> int:
    """The positive integer cost of *entering* *pos* (section 3.3)."""
    cost = BASE_STEP
    cost += min(visits.get(pos, 0), VISIT_CAP) * VISIT_PENALTY
    cost += min(failed.get(pos, 0), FAILED_CAP) * FAILED_PENALTY
    return cost


def one_dijkstra(terrain: TerrainMemory, hero: Tuple[int, int],
                 visits: Optional[Dict[Tuple[int, int], int]] = None,
                 failed: Optional[Dict[Tuple[int, int], int]] = None,
                 deadline_check=None):
    """One Dijkstra from *hero* over known-safe legal edges (4.5).

    Returns ``(dist, first)`` where ``dist[pos]`` is the integer cost from the
    hero and ``first[pos]`` is the first step leaving the hero on a shortest
    path to *pos*.  The hero itself is ``dist[hero] == 0`` with no first step.
    The traversal is bounded by *deadline_check* (a callable that may raise),
    so a single preparation cannot overrun the reflex allowance.
    """
    visits = visits or {}
    failed = failed or {}
    dist: Dict[Tuple[int, int], int] = {hero: 0}
    first: Dict[Tuple[int, int], Tuple[int, int]] = {}
    heap = []
    for step in DIRECTIONS:
        nb = (hero[0] + step[0], hero[1] + step[1])
        if not edge_legal(terrain, hero, nb):
            continue
        c = _edge_cost(terrain, nb, visits, failed)
        if c < dist.get(nb, INF):
            dist[nb] = c
            first[nb] = step
            heapq.heappush(heap, (c, DIR_RANK[step], nb))
    while heap:
        d, _rank, pos = heapq.heappop(heap)
        if d > dist.get(pos, INF):
            continue
        if deadline_check is not None:
            deadline_check()
        fstep = first[pos]
        for step in DIRECTIONS:
            nb = (pos[0] + step[0], pos[1] + step[1])
            if not edge_legal(terrain, pos, nb):
                continue
            nd = d + _edge_cost(terrain, nb, visits, failed)
            if nd < dist.get(nb, INF):
                dist[nb] = nd
                first[nb] = fstep
                heapq.heappush(heap, (nd, DIR_RANK[fstep], nb))
    return dist, first


# -- targets ---------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """One reachable navigation target and the first step toward it."""

    pos: Tuple[int, int]
    family: str
    first_step: Tuple[int, int]
    cost: int
    reason: str = ""

    def order_key(self) -> tuple:
        return (self.cost, DIR_RANK.get(self.first_step, 99), self.pos)


def is_frontier(terrain: TerrainMemory, pos: Tuple[int, int]) -> bool:
    """True when *pos* borders a cell whose ground is not yet observed."""
    if not terrain.walkable(pos):
        return False
    x, y = pos
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        nb = (x + dx, y + dy)
        if terrain.ter(nb) == T_UNKNOWN:
            return True
    return False


def _approach_of(terrain: TerrainMemory, door: Tuple[int, int],
                 dist: Dict[Tuple[int, int], int]
                 ) -> Optional[Tuple[int, int]]:
    """The cheapest cardinal walkable neighbour of a closed *door*."""
    best = None
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        nb = (door[0] + dx, door[1] + dy)
        if nb not in dist or not terrain.walkable(nb):
            continue
        if best is None or (dist[nb], nb) < (dist[best], best):
            best = nb
    return best


def enumerate_targets(terrain: TerrainMemory, hero: Tuple[int, int],
                      dist: Dict[Tuple[int, int], int],
                      first: Dict[Tuple[int, int], Tuple[int, int]],
                      visits: Optional[Dict[Tuple[int, int], int]] = None
                      ) -> Tuple[Target, ...]:
    """Every reachable target, with no ``[:8]`` prefilter (4.5, M08).

    Enumerates, deterministically: all reachable down stairs; every cardinal
    closed-door approach; every known-safe observation frontier; and every
    unvisited reachable known cell.  Unreachable stairs remain remembered in
    terrain memory but are simply absent here, so a Manhattan-nearest stair
    that is walled off can no longer suppress a reachable farther one (M09).
    """
    visits = visits or {}
    out = []
    hero_set = (hero,)
    # 1. reachable down stairs (a monster-covered stair is not reachable)
    for pos in sorted(terrain.stairs_down()):
        if pos in hero_set or pos not in dist:
            continue
        out.append(Target(pos, TFAM_STAIR, first[pos], dist[pos],
                          "reachable down stairs"))
    # 2. cardinal closed-door approaches: the door itself is not walkable, so
    #    the target is the approach cell and the effect is opening the door
    for door in sorted(p for p, t in terrain.terrain.items()
                       if t == T_CLOSED_DOOR):
        approach = _approach_of(terrain, door, dist)
        if approach is None:
            continue
        step = first.get(approach)
        if step is None:
            # hero is already on the approach: open the door directly
            step = (door[0] - hero[0], door[1] - hero[1])
        out.append(Target(door, TFAM_DOOR, step, dist.get(approach, 0),
                          "approach a closed door"))
    # 3. frontiers and unvisited known cells
    for pos in sorted(dist):
        if pos in hero_set:
            continue
        if is_frontier(terrain, pos):
            out.append(Target(pos, TFAM_FRONTIER, first[pos], dist[pos],
                              "observation frontier"))
        elif visits.get(pos, 0) == 0:
            out.append(Target(pos, TFAM_UNVISITED, first[pos], dist[pos],
                              "unvisited known cell"))
    return tuple(sorted(out, key=lambda t: t.order_key()))


@dataclass
class NavPlan:
    """One Dijkstra result plus its enumerated targets."""

    hero: Tuple[int, int]
    dist: Dict[Tuple[int, int], int]
    first: Dict[Tuple[int, int], Tuple[int, int]]
    targets: Tuple[Target, ...]


def plan(terrain: TerrainMemory, hero: Tuple[int, int],
         visits: Optional[Dict[Tuple[int, int], int]] = None,
         failed: Optional[Dict[Tuple[int, int], int]] = None,
         deadline_check=None) -> NavPlan:
    """One Dijkstra and all-reachable-target enumeration (the 4.5 gate)."""
    dist, first = one_dijkstra(terrain, hero, visits, failed, deadline_check)
    targets = enumerate_targets(terrain, hero, dist, first, visits)
    return NavPlan(hero=hero, dist=dist, first=first, targets=targets)


# -- target persistence (4.3) ----------------------------------------------

@dataclass(frozen=True)
class PersistedTarget:
    """A navigation target bound to the instance that produced it."""

    pos: Tuple[int, int]
    family: str
    instance_id: int


class TargetStore(object):
    """Persist one navigation target under the section 4.3 conditions.

    A target persists only while it is in the same instance, still walkable
    with no blocking occupant, and its evidence is unexhausted.  A fresh
    instance (4.1 rule 6) expires it, and cycle recovery invalidates it so a
    loop cannot be driven by a stale destination.
    """

    def __init__(self) -> None:
        self.current: Optional[PersistedTarget] = None

    def consider(self, target: Optional[Target], instance_id: int,
                 terrain: TerrainMemory, hero: Tuple[int, int]) -> bool:
        """Adopt *target* if it is valid now; return whether one is held."""
        if target is None:
            return self.current is not None
        if not self._valid(target.pos, instance_id, terrain, hero):
            return self.current is not None
        self.current = PersistedTarget(target.pos, target.family,
                                       instance_id)
        return True

    def _valid(self, pos, instance_id, terrain, hero) -> bool:
        if pos == hero:
            return True
        if terrain.ter(pos) in _DOOR_TERRAIN or terrain.walkable(pos):
            return terrain.occupant(pos) == OCC_NONE
        # a closed door is approached, not stood on
        return terrain.ter(pos) == T_CLOSED_DOOR

    def holds(self, instance_id: int, terrain: TerrainMemory,
              hero: Tuple[int, int]) -> bool:
        """False when persistence no longer holds (and drop the target)."""
        t = self.current
        if t is None:
            return False
        if t.instance_id != instance_id:
            self.current = None
            return False
        if not self._valid(t.pos, instance_id, terrain, hero):
            self.current = None
            return False
        return True

    def invalidate_cycle(self) -> None:
        """Cycle recovery invalidates the target (4.3)."""
        self.current = None

    def expire_instance(self, instance_id: int) -> None:
        cur = self.current
        if cur is not None and cur.instance_id != instance_id:
            self.current = None


# -- destination commitment lifecycle (plan section 1) ---------------------
#
# A destination is elected once and then *held*: the reflex recomputes its
# route every command turn but does not re-elect a new destination merely
# because another target now scores higher, a frontier reclassifies, or a
# visit penalty rises.  Commitment is enforced here, in the pure layer, and is
# only ever mutated at a reconcile/effect boundary -- never during a render or
# a preparation (AC6).

# Purposes (the plan's destination record taxonomy).
COMMIT_EXPLORE_FRONTIER = "explore-frontier"
COMMIT_EXPLORE_UNVISITED = "explore-unvisited"
COMMIT_OPEN_DOOR = "open-door"
COMMIT_COLLECT_ITEMS = "collect-items"
COMMIT_FLEE_UPSTAIRS = "flee-upstairs"
COMMIT_STAIR = "stair"

# Phases.
PHASE_TRAVELLING = "travelling"
PHASE_INTERACTING = "interacting"

# Sources.
SRC_DEFAULT = "default"
SRC_DIRECTIVE = "directive"

#: Ineffective door-interaction attempts allowed per unchanged evidence.
DOOR_INTERACT_MAX = 2
#: Navigation attempts without a new cell toward the route (stall budget).
STALL_MAX = 3
#: A generous total selected-navigation cap: ``max(16, 4*hops + 8)``.
STALL_TOTAL_MIN = 16
STALL_TOTAL_FACTOR = 4
STALL_TOTAL_SLACK = 8

_PURPOSE_BY_FAMILY = {
    TFAM_FRONTIER: COMMIT_EXPLORE_FRONTIER,
    TFAM_UNVISITED: COMMIT_EXPLORE_UNVISITED,
    TFAM_DOOR: COMMIT_OPEN_DOOR,
    TFAM_STAIR: COMMIT_STAIR,
}


@dataclass(frozen=True)
class Commitment:
    """One persistent committed destination (plan section 1.1)."""

    instance_id: int
    serial: int
    purpose: str
    pos: Tuple[int, int]
    family: str
    approach: Optional[Tuple[int, int]] = None
    source: str = SRC_DEFAULT
    generation: int = 0
    evidence_token: tuple = ()
    acquisition_tick: int = 0
    phase: str = PHASE_TRAVELLING


def commitment_for(target: Target, *, instance_id: int, serial: int,
                   source: str = SRC_DEFAULT, generation: int = 0,
                   tick: int = 0, evidence_token: tuple = (),
                   purpose: Optional[str] = None) -> Commitment:
    """Build a :class:`Commitment` from an elected :class:`Target`."""
    return Commitment(
        instance_id=int(instance_id), serial=int(serial),
        purpose=purpose or _PURPOSE_BY_FAMILY.get(target.family,
                                                  COMMIT_EXPLORE_FRONTIER),
        pos=tuple(target.pos), family=target.family,
        approach=None, source=source, generation=int(generation),
        evidence_token=tuple(evidence_token), acquisition_tick=int(tick))


class CommitmentStore(object):
    """The instance-scoped destination commitment and its scoped ledgers.

    Pure: it holds no reference to policy, providers or the wire.  Every
    mutating method is a *fold* operation the caller performs only at a
    reconcile/effect boundary or a genuine observation fold; the read methods
    are side-effect free.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.current: Optional[Commitment] = None
        self._serial = 0
        self._serviced: Dict[Tuple[int, int], tuple] = {}
        self._failed: Dict[Tuple[int, int], tuple] = {}
        self.stall_attempts = 0
        self.interact_attempts = 0
        self.total_attempts = 0
        self.progress_pos: Optional[Tuple[int, int]] = None
        self.last_progress_tick: Optional[int] = None
        self.stall_cap = STALL_TOTAL_MIN
        self.events: list = []

    # -- reads (pure) ----------------------------------------------------
    def held(self) -> Optional[Commitment]:
        return self.current

    def holds(self, instance_id: int, terrain: "TerrainMemory",
              hero: Tuple[int, int]) -> bool:
        """False when the commitment is no longer valid for this state."""
        c = self.current
        if c is None:
            return False
        if c.instance_id != instance_id:
            return False
        if tuple(hero) == c.pos:
            return False                    # arrived: the caller is done
        if c.purpose == COMMIT_OPEN_DOOR:
            return terrain.ter(c.pos) == T_CLOSED_DOOR
        if terrain.ter(c.pos) in _DOOR_TERRAIN:
            return True
        return terrain.walkable(c.pos) and terrain.occupant(c.pos) == OCC_NONE

    def serviced(self, pos: Tuple[int, int]) -> bool:
        return tuple(pos) in self._serviced

    def serviced_signature(self, pos: Tuple[int, int]) -> Optional[tuple]:
        return self._serviced.get(tuple(pos))

    def failed(self, pos: Tuple[int, int]) -> bool:
        return tuple(pos) in self._failed

    def failed_signature(self, pos: Tuple[int, int]) -> Optional[tuple]:
        return self._failed.get(tuple(pos))

    def serviced_under_evidence(self, pos: Tuple[int, int],
                                signature: tuple) -> bool:
        """True when *pos* is serviced under the *unchanged* local evidence."""
        cur = self._serviced.get(tuple(pos))
        return cur is not None and tuple(cur) == tuple(signature)

    def failed_under_evidence(self, pos: Tuple[int, int],
                              signature: tuple) -> bool:
        """True when *pos* failed under the *unchanged* local evidence."""
        cur = self._failed.get(tuple(pos))
        return cur is not None and tuple(cur) == tuple(signature)

    # -- folds -----------------------------------------------------------
    def commit(self, *, instance_id: int, purpose: str, pos: Tuple[int, int],
               family: str, source: str = SRC_DEFAULT, generation: int = 0,
               evidence_token: tuple = (), tick: int = 0,
               approach: Optional[Tuple[int, int]] = None,
               expected_serial: Optional[int] = None,
               hops: Optional[int] = None,
               hero: Optional[Tuple[int, int]] = None,
               phase: str = PHASE_TRAVELLING) -> bool:
        """Install a new commitment (compare-and-apply).

        ``expected_serial`` implements the compare-and-apply contract: when it
        is given and the active serial has changed since the proposal was
        frozen, the effect is stale and is dropped.  ``None`` means "fresh
        acquisition" and always installs.

        ``hero`` is the reconciled hero square of the acquisition's own
        observation; route progress is seeded from it -- never from the
        destination position -- so the first genuinely no-progress
        continuation is counted rather than masked (stall-recovery plan §2A).
        """
        if expected_serial is not None:
            cur = self.current
            if cur is None or cur.serial != int(expected_serial):
                return False
        self._serial += 1
        self.current = Commitment(
            instance_id=int(instance_id), serial=self._serial,
            purpose=purpose, pos=tuple(pos), family=family,
            approach=None if approach is None else tuple(approach),
            source=source, generation=int(generation),
            evidence_token=tuple(evidence_token), acquisition_tick=int(tick),
            phase=phase)
        self.stall_attempts = 0
        self.interact_attempts = 0
        self.total_attempts = 0
        # Seed route progress from the hero baseline, never the destination
        # position: seeding at the target made the first no-progress
        # continuation look like progress (plan §2A off-by-one).
        self.progress_pos = (tuple(hero) if hero is not None else tuple(pos))
        self.last_progress_tick = int(tick)
        if hops is not None:
            self.set_stall_cap(hops)
        return True

    def set_stall_cap(self, hops: int) -> None:
        self.stall_cap = max(STALL_TOTAL_MIN,
                             STALL_TOTAL_FACTOR * int(hops) + STALL_TOTAL_SLACK)

    def set_phase(self, phase: str) -> None:
        """Transition the active commitment's phase, keeping every identity.

        The serial, evidence token, purpose and originating generation are
        preserved, so an arrival that begins an interaction is not a new
        commitment and its later terminal outcome settles the same target.
        """
        cur = self.current
        if cur is None or cur.phase == phase:
            return
        self.current = replace(cur, phase=phase)
        self.events.append({"event": "phase", "phase": phase, "pos": cur.pos})

    def note_nav_attempt(self) -> None:
        self.total_attempts += 1
        self.stall_attempts += 1

    def note_progress(self, pos: Tuple[int, int], tick: int) -> None:
        """A new cell toward the route: reset the no-progress budget."""
        if self.current is None:
            return
        if self.progress_pos is None or tuple(pos) != self.progress_pos:
            self.progress_pos = tuple(pos)
            self.last_progress_tick = int(tick)
            self.stall_attempts = 0

    def stalled(self) -> bool:
        """At most three no-progress attempts, plus the total-navigation cap.

        Both caps are inclusive (``>=``): the plan permits *at most* three
        reconciled navigation attempts without a new cell toward the route, and
        a generous total selected-navigation cap.
        """
        return (self.stall_attempts >= STALL_MAX
                or self.total_attempts >= self.stall_cap)

    @property
    def door_attempts_exhausted(self) -> bool:
        return self.interact_attempts >= DOOR_INTERACT_MAX

    def note_interact_attempt(self) -> None:
        self.interact_attempts += 1

    def note_serviced(self, pos: Tuple[int, int], signature: tuple) -> None:
        self._serviced[tuple(pos)] = tuple(signature)

    def note_failed(self, pos: Tuple[int, int], signature: tuple) -> None:
        self._failed[tuple(pos)] = tuple(signature)

    def retire(self, reason: str, *, pos: Optional[Tuple[int, int]] = None,
               signature: Optional[tuple] = None) -> None:
        """Clear the active commitment, optionally recording its failure.

        A failure is recorded only when an explicit local evidence *signature*
        is supplied, so a successful completion (reached/collected) never
        writes a spurious suppression, and a recorded failure can be reopened
        exactly when its local evidence changes.
        """
        c = self.current
        target_pos = c.pos if (pos is None and c is not None) else pos
        if signature is not None and target_pos is not None:
            self._failed[tuple(target_pos)] = tuple(signature)
        self.current = None
        self.events.append({"event": "retired", "reason": reason,
                            "pos": target_pos})

    def invalidate_cycle(self, signature: Optional[tuple] = None) -> None:
        """Cycle recovery invalidates and suppresses the held destination."""
        c = self.current
        if c is None:
            return
        if signature is not None:
            self._failed[tuple(c.pos)] = tuple(signature)
        self.current = None
        self.events.append({"event": "cycle-invalidated", "pos": c.pos})

    def expire_instance(self, instance_id: int) -> None:
        """A fresh instance clears the commitment and the scoped ledgers."""
        c = self.current
        if c is not None and c.instance_id != int(instance_id):
            self.current = None
        self._serviced.clear()
        self._failed.clear()


def resolve_destination(terrain: "TerrainMemory", hero: Tuple[int, int],
                        dist: Dict[Tuple[int, int], int],
                        first: Dict[Tuple[int, int], Tuple[int, int]],
                        visits: Optional[Dict[Tuple[int, int], int]] = None,
                        store: Optional[CommitmentStore] = None,
                        *, prefer_stairs: bool = False,
                        directive_pos: Optional[Tuple[int, int]] = None,
                        directive_purpose: Optional[str] = None,
                        source: str = SRC_DEFAULT,
                        generation: int = 0, tick: int = 0,
                        hops: Optional[int] = None) -> Optional[Commitment]:
    """Elect a destination from the current plan, purely (plan 1.2).

    Ordering: an explicit directive target first; then reachable doors and
    frontiers (the default pool) before down-stairs; then unvisited known
    cells; and only then stairs.  Serviced and failed sites under their local
    evidence signature are suppressed.  Returns a *proposed* commitment; the
    caller installs it through :meth:`CommitmentStore.commit` at a reconcile
    boundary -- this function never mutates the store.
    """
    targets = enumerate_targets(terrain, hero, dist, first, visits)
    store = store or CommitmentStore()

    def eligible(t):
        # Split evidence (plan §4): successful servicing is suppressed under
        # its exploration-only signature, while a door failure is suppressed
        # under its target-bound door signature.  Neighbouring occupancy can
        # reopen neither by itself.
        pos = t.pos
        serviced_sig = service_signature(terrain, pos)
        if terrain.ter(pos) == T_CLOSED_DOOR:
            failed_sig = door_failure_signature(terrain, pos)
        else:
            failed_sig = serviced_sig
        return (not store.serviced_under_evidence(pos, serviced_sig)
                and not store.failed_under_evidence(pos, failed_sig))

    chosen = None
    if directive_pos is not None:
        for t in targets:
            if tuple(t.pos) == tuple(directive_pos):
                chosen = t
                break
    else:
        stair = [t for t in targets if t.family == TFAM_STAIR and eligible(t)]
        explore = [t for t in targets
                   if t.family in (TFAM_DOOR, TFAM_FRONTIER) and eligible(t)]
        unvisited = [t for t in targets
                     if t.family == TFAM_UNVISITED and eligible(t)]
        if prefer_stairs and stair:
            chosen = stair[0]
        elif explore:
            chosen = explore[0]
        elif unvisited:
            chosen = unvisited[0]
        elif stair:
            chosen = stair[0]
    if chosen is None:
        return None
    serial = store._serial + 1
    c = commitment_for(chosen, instance_id=store.current.instance_id
                       if store.current else 0, serial=serial,
                       source=source, generation=generation, tick=tick,
                       purpose=directive_purpose)
    return c


def route_held_destination(c: Commitment, terrain: "TerrainMemory",
                           hero: Tuple[int, int],
                           dist: Dict[Tuple[int, int], int],
                           first: Dict[Tuple[int, int], Tuple[int, int]]
                           ) -> Tuple[Optional[Tuple[int, int]], Optional[str],
                                      str]:
    """The next step/terminal for a held commitment (plan 1.3).

    Returns ``(step, terminal, reason)``: exactly one of *step* (a wire
    direction) or *terminal* (``"arrive"`` for a reached target or
    ``"interact"`` for a door) is set.  The semantic door coordinate stays
    stable; a closed door is not completed by reaching its approach.
    """
    pos = c.pos
    if c.purpose == COMMIT_OPEN_DOOR:
        if tuple(hero) == pos:
            return None, "interact", "already at the closed door"
        # a closed door is not walkable, so it has no Dijkstra entry: the route
        # is to its cheapest reachable cardinal approach, then into the door
        approach = None
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nb = (pos[0] + dx, pos[1] + dy)
            if nb in dist and terrain.walkable(nb):
                if approach is None or (dist[nb], nb) < (dist[approach],
                                                         approach):
                    approach = nb
        if approach is not None:
            step = first.get(approach)
            if step is not None:
                return step, None, "approach the closed door"
            # the hero stands on the approach: step into the door to open it
            d = (pos[0] - hero[0], pos[1] - hero[1])
            if d != (0, 0):
                return d, None, "open the door"
        return None, None, "no route to the closed door"
    if tuple(hero) == pos:
        return None, "arrive", "reached the destination"
    if pos in first:
        return first[pos], None, "continue to the destination"
    return None, None, "no route to the destination"


def _target_at(pos: Tuple[int, int], dist: Dict[Tuple[int, int], int],
               first: Dict[Tuple[int, int], Tuple[int, int]], family: str,
               reason: str) -> Target:
    """A reachable target record at *pos* (the caller proved reachability)."""
    return Target(tuple(pos), family, first[pos], dist[pos], reason)


def resolve_semantic_destination(
        terrain: "TerrainMemory", hero: Tuple[int, int],
        dist: Dict[Tuple[int, int], int],
        first: Dict[Tuple[int, int], Tuple[int, int]], *,
        purpose: str,
        target: Optional[Tuple[int, int]] = None,
        upstairs: Sequence[Tuple[int, int]] = (),
        evidence_positions: Sequence[Tuple[int, int]] = ()
) -> Tuple[Optional[Target], str]:
    """Resolve a coordinate-bearing v2 goal to a legal, *observed* target.

    Returns ``(Target, "")`` on success and ``(None, reason)`` on a structured
    failure -- an explicit destination is never silently re-resolved against
    generic exploration enumeration (plan 2.1/1.5).  ``flee_to_upstairs``
    accepts only an observed upstairs square (a supplied coordinate that is not
    known upstairs is rejected), and a null target deterministically selects
    the nearest *reachable* observed upstairs.  ``collect_items`` resolves only
    against a matching current floor-evidence token with a reachable route, so
    a visited non-frontier floor square without item evidence is not a target.
    """
    hero = tuple(hero)
    if purpose == COMMIT_FLEE_UPSTAIRS:
        known = {tuple(p) for p in upstairs} | set(terrain.stairs_up())
        if target is not None:
            t = tuple(target)
            if t not in known:
                return None, "flee target is not an observed upstairs"
            if t == hero:
                return None, "already on the flee target"
            if t not in dist:
                return None, "flee target is not reachable"
            return _target_at(t, dist, first, TFAM_STAIR,
                              "reachable known up stairs"), ""
        reachable = sorted((p for p in known if p in dist and p != hero),
                           key=lambda p: (dist[p], p))
        if not reachable:
            return None, "no reachable observed upstairs"
        return _target_at(reachable[0], dist, first, TFAM_STAIR,
                          "nearest reachable known up stairs"), ""
    if purpose == COMMIT_COLLECT_ITEMS:
        if target is None:
            return None, "collect_items requires a target"
        t = tuple(target)
        if t not in {tuple(p) for p in evidence_positions}:
            return None, "no floor item evidence at the target"
        if t == hero:
            # the target is already under the hero: this is not a failure, it
            # is the on-square collection case -- the caller emits the pickup
            # action rather than a movement step (plan 1.5/3.3)
            return Target(t, TFAM_UNVISITED, (0, 0), 0,
                          "collect the items here"), ""
        if t not in dist:
            return None, "the collection site is not reachable"
        return _target_at(t, dist, first, TFAM_UNVISITED,
                          "collect the observed items here"), ""
    return None, "no coordinate-bearing destination goal"


__all__ = [
    "DIRECTIONS", "DIR_RANK", "BASE_STEP", "VISIT_PENALTY", "FAILED_PENALTY",
    "TFAM_STAIR", "TFAM_DOOR", "TFAM_FRONTIER", "TFAM_UNVISITED",
    "edge_legal", "one_dijkstra", "enumerate_targets", "is_frontier",
    "door_open", "local_evidence_signature", "service_signature",
    "door_failure_signature", "blocked_edge_signature",
    "Target", "NavPlan", "plan", "PersistedTarget", "TargetStore",
    "COMMIT_EXPLORE_FRONTIER", "COMMIT_EXPLORE_UNVISITED", "COMMIT_OPEN_DOOR",
    "COMMIT_COLLECT_ITEMS", "COMMIT_FLEE_UPSTAIRS", "COMMIT_STAIR",
    "PHASE_TRAVELLING", "PHASE_INTERACTING", "SRC_DEFAULT", "SRC_DIRECTIVE",
    "DOOR_INTERACT_MAX", "STALL_MAX", "STALL_TOTAL_MIN", "STALL_TOTAL_FACTOR",
    "STALL_TOTAL_SLACK", "Commitment", "CommitmentStore", "commitment_for",
    "resolve_destination", "route_held_destination",
    "resolve_semantic_destination",
]
