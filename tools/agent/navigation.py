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
from dataclasses import dataclass
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


__all__ = [
    "DIRECTIONS", "DIR_RANK", "BASE_STEP", "VISIT_PENALTY", "FAILED_PENALTY",
    "TFAM_STAIR", "TFAM_DOOR", "TFAM_FRONTIER", "TFAM_UNVISITED",
    "edge_legal", "one_dijkstra", "enumerate_targets", "is_frontier",
    "Target", "NavPlan", "plan", "PersistedTarget", "TargetStore",
]
