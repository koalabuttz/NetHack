#!/usr/bin/env python3
"""Wave-3 tests: one-Dijkstra navigation and retained-table selection.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Covers ``tools/agent/navigation.py`` (the one-Dijkstra all-target planner of
plan section 4.5, its door/corner edge legality and target persistence) and
the ``ScriptedReflex.prepare`` candidate path in ``tools/agent/policy.py``:
integer scoring, deterministic table identity, reachable-target selection and
the named mutation defects M08 (frontier prefilter), M09 (Manhattan-only
stair) and M10 (illegal diagonal door/corner edge).
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (candidates, navigation,  # noqa: E402
                         policy, protocol, state)
from tools.agent.providers import (ProviderConfig,  # noqa: E402
                                   ReflexContext)


FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")
DOOR = ("+", "brown", 0, "none")
OPEN = ("-", "brown", 0, "none")
DOWN = (">", "white", 0, "none")


def terrain(cells):
    tm = navigation.TerrainMemory()
    tm.merge(cells)
    return tm


class OneDijkstra(unittest.TestCase):
    def test_corridor_distances_and_first_step(self):
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        tm = terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        self.assertEqual(dist[(5, 10)], 4 * navigation.BASE_STEP)
        self.assertEqual(first[(5, 10)], (1, 0))

    def test_unknown_cells_are_not_traversed(self):
        cells = {(1, 10): FLOOR, (5, 10): FLOOR}
        tm = terrain(cells)
        dist, _ = navigation.one_dijkstra(tm, (1, 10))
        self.assertNotIn((5, 10), dist)

    def test_visit_penalty_is_capped(self):
        cells = {(x, 10): FLOOR for x in range(1, 4)}
        tm = terrain(cells)
        d0, _ = navigation.one_dijkstra(tm, (1, 10))
        d1, _ = navigation.one_dijkstra(tm, (1, 10), {(3, 10): 100})
        # a heavily visited cell is discouraged but never unbounded
        self.assertLess(d1[(3, 10)] - d0[(3, 10)],
                        navigation.BASE_STEP * 3)


class EdgeLegality(unittest.TestCase):
    def test_diagonal_door_entry_is_illegal(self):
        # M10: a diagonal step into or out of a doorway/open door is refused
        cells = {(3, 10): FLOOR, (4, 11): OPEN, (3, 11): WALL, (4, 10): WALL}
        tm = terrain(cells)
        self.assertFalse(navigation.edge_legal(tm, (3, 10), (4, 11)))

    def test_corner_squeeze_is_illegal(self):
        # M10: a diagonal squeezed between two blocked orthogonal sides
        cells = {(3, 10): FLOOR, (4, 11): FLOOR,
                 (4, 10): WALL, (3, 11): WALL}
        tm = terrain(cells)
        self.assertFalse(navigation.edge_legal(tm, (3, 10), (4, 11)))

    def test_open_diagonal_is_legal(self):
        cells = {(3, 10): FLOOR, (4, 11): FLOOR,
                 (4, 10): FLOOR, (3, 11): FLOOR}
        tm = terrain(cells)
        self.assertTrue(navigation.edge_legal(tm, (3, 10), (4, 11)))


class TargetEnumeration(unittest.TestCase):
    def test_no_eight_target_prefilter(self):
        # M08: the old planner took only the nearest eight frontiers before
        # reachability.  Every reachable frontier must be enumerated.
        cells = {(x, 10): FLOOR for x in range(1, 15)}
        cells[(15, 10)] = DOWN
        tm = terrain(cells)
        plan = navigation.plan(tm, (1, 10))
        frontiers = [t for t in plan.targets
                     if t.family == navigation.TFAM_FRONTIER]
        self.assertGreater(len(frontiers), 8)
        self.assertIn((14, 10), [t.pos for t in plan.targets])

    def test_reachable_farther_stair_beats_unreachable_nearer(self):
        # M09: a Manhattan-nearer stair that is walled off must not suppress a
        # reachable farther one.
        cells = {(x, 10): FLOOR for x in range(1, 12)}
        cells[(11, 10)] = DOWN                 # far, reachable
        cells[(3, 12)] = DOWN                  # nearer... but walled off
        cells[(2, 12)] = WALL
        cells[(4, 12)] = WALL
        cells[(3, 11)] = WALL
        cells[(3, 13)] = WALL
        tm = terrain(cells)
        plan = navigation.plan(tm, (6, 10))
        stairs = [t.pos for t in plan.targets
                  if t.family == navigation.TFAM_STAIR]
        self.assertIn((11, 10), stairs)
        self.assertNotIn((3, 12), stairs)      # unreachable => absent

    def test_closed_door_is_an_approach_not_a_step(self):
        cells = {(x, 10): FLOOR for x in range(1, 5)}
        cells[(3, 9)] = DOOR
        tm = terrain(cells)
        plan = navigation.plan(tm, (1, 10))
        doors = [t for t in plan.targets if t.family == navigation.TFAM_DOOR]
        self.assertTrue(doors)
        self.assertEqual(doors[0].pos, (3, 9))
        # the hero must approach, not stand on the door square
        self.assertNotEqual(doors[0].first_step, (0, 0))


class TargetPersistence(unittest.TestCase):
    def test_instance_transition_expires_the_target(self):
        store = navigation.TargetStore()
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        tm = terrain(cells)
        target = navigation.Target((5, 10), navigation.TFAM_STAIR, (1, 0), 0)
        store.consider(target, 1, tm, (1, 10))
        self.assertTrue(store.holds(1, tm, (1, 10)))
        self.assertFalse(store.holds(2, tm, (1, 10)))
        self.assertIsNone(store.current)

    def test_cycle_recovery_invalidates_the_target(self):
        store = navigation.TargetStore()
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        tm = terrain(cells)
        target = navigation.Target((5, 10), navigation.TFAM_STAIR, (1, 0), 0)
        store.consider(target, 1, tm, (1, 10))
        store.invalidate_cycle()
        self.assertIsNone(store.current)


# ------------------------------------------------------------------ policy

def mem_with(cells, hero):
    mem = state.EpisodeMemory()
    mem.grid.update(cells)
    mem.hero = hero
    mem.status.hp = 20
    mem.status.hp_max = 20
    # a fresh, current inventory cache so the periodic inventory-refresh
    # maintenance branch does not preempt navigation (legacy behaviour)
    mem.inventory.refresh([], 0, 0)
    return mem


def ctx(mem, tick=0, need=None, directives=()):
    need = need or {"kind": "command", "id": 1}
    return ReflexContext(
        episode=1, tick=tick, need=need,
        need_key=protocol.NeedKey(1, tick, need.get("id")),
        snapshot=protocol.Snapshot(), pages=[], memory=mem,
        directives=list(directives), deadline=0.0)


class PrepareAndSelection(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_prepare_builds_one_immutable_table(self):
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        mem = mem_with(cells, (1, 10))
        prepared = self.ref.prepare(ctx(mem))
        self.assertIsInstance(prepared, candidates.PreparedReflex)
        self.assertLessEqual(len(prepared.table), candidates.MAX_CANDIDATES)
        # deterministic identity: the same preparation repeats byte for byte
        again = self.ref.prepare(ctx(mem_with(cells, (1, 10))))
        self.assertEqual(prepared.table_id, again.table_id)
        self.assertEqual(prepared.canonical_bytes, again.canonical_bytes)

    def test_reachable_stair_is_selected_over_frontier(self):
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        mem = mem_with(cells, (1, 10))
        res = self.ref.decide(ctx(mem))
        self.assertEqual(res.action, {"key": protocol.DIR_KEYS[(1, 0)]})

    def test_farther_reachable_stair_is_chosen_when_nearer_is_isolated(self):
        cells = {(x, 10): FLOOR for x in range(1, 12)}
        cells[(11, 10)] = DOWN
        cells[(3, 12)] = DOWN
        cells[(2, 12)] = WALL
        cells[(4, 12)] = WALL
        cells[(3, 11)] = WALL
        cells[(3, 13)] = WALL
        mem = mem_with(cells, (6, 10))
        prepared = self.ref.prepare(ctx(mem))
        chosen = prepared.table.scripted()
        self.assertEqual(chosen.family, "stair")
        self.assertIn("down stairs", chosen.reason)

    def test_explore_frontier_directive_holds_the_stair_back(self):
        from tools.agent.directives import DirectiveSet, DirectiveView
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        cells[(7, 10)] = DOWN
        mem = mem_with(cells, (3, 10))
        view = DirectiveView(DirectiveSet(goals=("explore_frontier",)), 1)
        prepared = self.ref.prepare(ctx(mem, directives=[view]))
        chosen = prepared.table.scripted()
        self.assertEqual(chosen.family, "frontier")

    def test_hero_on_stairs_descends(self):
        cells = {(x, 10): FLOOR for x in range(1, 4)}
        cells[(3, 10)] = DOWN
        mem = mem_with(cells, (3, 10))
        mem.stairs_down.add((3, 10))
        res = self.ref.decide(ctx(mem))
        self.assertEqual(res.action, {"key": ord(">")})

    def test_direct_call_and_prepared_path_agree(self):
        cells = {(x, 10): FLOOR for x in range(1, 7)}
        cells[(6, 10)] = DOWN
        mem = mem_with(cells, (1, 10))
        direct = self.ref.decide(ctx(mem))
        prepared = self.ref.prepare(ctx(mem))
        self.assertEqual(direct.action,
                         candidates.candidate_to_wire(
                             prepared.table.scripted()))

    def test_no_progress_still_searches_in_place(self):
        # the safety loop breaker is preserved through the candidate path
        mem = mem_with({(1, 10): FLOOR}, (1, 10))
        mem.no_progress = 3
        res = self.ref.decide(ctx(mem))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_missing_hero_searches(self):
        mem = state.EpisodeMemory()
        res = self.ref.decide(ctx(mem))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_rejected_member_is_excluded(self):
        from tools.agent.arbitration import RejectionSet
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        cells[(5, 10)] = DOWN
        mem = mem_with(cells, (1, 10))
        prepared = self.ref.prepare(ctx(mem))
        chosen = prepared.table.scripted()
        rejected = RejectionSet()
        rejected.exclude(chosen)
        context = ctx(mem)
        context.rejected = rejected
        res = self.ref.decide(context)
        self.assertNotEqual(res.action, chosen.action.to_wire())


class ScoreBoundaries(unittest.TestCase):
    def test_family_gaps_exceed_the_path_adjustment(self):
        # ordinary frontier bias can never suppress a reachable staircase
        for cost in (0, 100, 10000, 100000):
            stair = policy._target_score("stair", cost, False)
            frontier = policy._target_score("frontier", 0, False)
            self.assertGreater(stair, frontier)

    def test_explore_first_orders_frontier_above_stair(self):
        stair = policy._target_score("stair", 0, True)
        frontier = policy._target_score("frontier", 0, True)
        unvisited = policy._target_score("unvisited", 0, True)
        self.assertGreater(frontier, stair)
        self.assertGreater(unvisited, stair)


class AntiBacktrackPreference(unittest.TestCase):
    """Bounded same-family anti-backtrack preference (plan section 4, AC.8)."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _prime(self, cells, path):
        mem = mem_with(cells, path[0])
        for pos in path:
            mem.hero = pos
            self.ref.note_observation(mem)
        return mem

    def _corridor(self, lo=2, hi=8, y=10):
        cells = {}
        for x in range(lo, hi + 1):
            cells[(x, y)] = FLOOR
            cells[(x, y - 1)] = WALL
            cells[(x, y + 1)] = WALL
        return cells

    def test_navigation_prefers_comparable_nonbacktracking_frontier(self):
        # the hero arrived at (5,10) from (4,10) (west); a comparable west
        # frontier (a reversal) and an east frontier are both reachable, so the
        # reversal is suppressed and the east route kept
        cells = self._corridor()
        mem = self._prime(cells, [(4, 10), (5, 10)])
        self.assertEqual(self.ref.recovery.previous_distinct, (4, 10))
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.direction, (1, 0))
        self.assertEqual(cand.family, "frontier")

    def test_navigation_backtrack_filter_applies_before_action_deduplication(
            self):
        # the suppressed reversal is removed before candidates are built, so
        # no deduplicated representative can resurrect it
        cells = self._corridor()
        mem = self._prime(cells, [(4, 10), (5, 10)])
        table = self.ref.prepare(ctx(mem)).table
        directions = {c.direction for c in table.ordered_candidates}
        self.assertNotIn((-1, 0), directions)
        self.assertIn((1, 0), directions)

    def test_navigation_ignores_rejected_nonbacktracking_alternative(self):
        # if the non-reversing alternative is already rejected it cannot
        # suppress the only usable retreat: the reversal is kept
        from tools.agent.arbitration import RejectionSet
        cells = self._corridor()
        mem = self._prime(cells, [(4, 10), (5, 10)])
        rejected = RejectionSet()
        rejected.signatures.add(
            candidates.ImmutableAction.key(protocol.DIR_KEYS[(1, 0)])
            .signature())
        context = ctx(mem)
        context.rejected = rejected
        cand = self.ref.prepare(context).table.scripted()
        self.assertEqual(cand.direction, (-1, 0))

    def test_navigation_preserves_only_dead_end_exit(self):
        # a pocket whose only exit is back the way the hero came keeps that
        # reversal: it is a legitimate retreat, not an avoidable oscillation
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (1, 10): WALL, (2, 9): WALL,
                 (2, 11): WALL, (3, 9): WALL, (3, 11): WALL, (4, 10): WALL}
        mem = self._prime(cells, [(3, 10), (2, 10)])
        self.assertEqual(self.ref.recovery.previous_distinct, (3, 10))
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.direction, (1, 0))

    def test_navigation_preserves_uniquely_best_reverse_stair_and_door_routes(
            self):
        # a uniquely reachable down stair behind a comparable frontier is not
        # suppressed by the anti-backtrack preference (cross-family comparison
        # is forbidden)
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (4, 10): FLOOR,
                 (5, 10): FLOOR, (6, 10): DOWN}
        for pos in [(2, 9), (2, 11), (3, 9), (3, 11), (4, 9), (4, 11),
                    (5, 9), (5, 11)]:
            cells[pos] = WALL
        mem = self._prime(cells, [(5, 10), (4, 10)])
        self.assertEqual(self.ref.recovery.previous_distinct, (5, 10))
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # the stair is east (forward), never suppressed by the west reversal
        self.assertEqual(cand.family, "stair")

    def test_antibacktrack_score_exception_at_40_41_and_directive_bonus(self):
        from tools.agent.directives import DirectiveSet, DirectiveView
        self.ref.recovery.previous_distinct = (4, 10)
        hero = (5, 10)

        def entry(pos, step, score):
            target = navigation.Target(pos, navigation.TFAM_FRONTIER, step, 0,
                                       "r")
            return (target, "frontier", protocol.DIR_KEYS[step], score, step)

        west = lambda s: entry((2, 10), (-1, 0), s)     # reversing
        east = lambda s: entry((8, 10), (1, 0), s)      # non-reversing
        # a 40-point margin does NOT qualify: the reversal is suppressed
        kept = self.ref._antibacktrack([west(540), east(500)], hero, None)
        self.assertEqual([e[3] for e in kept], [500])
        # a 41-point margin keeps it
        kept = self.ref._antibacktrack([west(541), east(500)], hero, None)
        self.assertEqual(sorted(e[3] for e in kept), [500, 541])
        # the +30 directive contribution participates: boosting the
        # alternative to 530 drops the margin to 11, so the reversal is
        # suppressed again
        kept = self.ref._antibacktrack([west(541), east(530)], hero, None)
        self.assertEqual([e[3] for e in kept], [530])
        # and the directive component is exactly the +30 the plan names
        self.ref.directives = DirectiveView(
            DirectiveSet(goals=("explore_frontier",), target=(8, 10)), 1)
        self.assertEqual(
            self.ref._directive_component(
                navigation.Target((8, 10), navigation.TFAM_FRONTIER, (1, 0), 0)),
            30)

    def test_navigation_preparation_does_not_advance_movement_history(self):
        cells = self._corridor()
        mem = self._prime(cells, [(4, 10), (5, 10)])
        before = (self.ref.recovery.current,
                  self.ref.recovery.previous_distinct,
                  self.ref.recovery.movement_history())
        for _ in range(3):
            self.ref.prepare(ctx(mem))
            self.ref.decide(ctx(mem))
        self.assertEqual((self.ref.recovery.current,
                          self.ref.recovery.previous_distinct,
                          self.ref.recovery.movement_history()), before)


class CycleRecoveryLegality(unittest.TestCase):
    """Cycle recovery obeys the planner's edge legality (AC.10)."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_cycle_recovery_obeys_door_diagonal_and_corner_legality(self):
        # the only apparent escape is a diagonal corner squeeze between two
        # walls, which the planner forbids; the legal cardinal exit is taken
        cells = {(3, 10): FLOOR, (4, 11): FLOOR, (4, 10): WALL,
                 (3, 11): WALL, (2, 10): FLOOR}
        for pos in [(2, 9), (2, 11), (3, 9)]:
            cells[pos] = WALL
        mem = mem_with(cells, (3, 10))
        # the hero came from (2,10); the (4,11) diagonal is illegal
        self.ref.recovery.previous_distinct = (2, 10)
        self.ref._cycled = True
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertNotEqual(cand.direction, (1, 1))
        self.assertEqual(cand.direction, (-1, 0))

    def test_emergency_disengagement_may_reverse(self):
        # low HP with an adjacent monster: the emergency branch is untouched
        # and may step straight back toward where the hero just came from
        cells = {(10, 10): FLOOR, (11, 10): FLOOR, (12, 10): FLOOR}
        mem = mem_with(cells, (11, 10))
        mem.status.hp = 1
        mem.status.hp_max = 20
        self.ref.recovery.previous_distinct = (12, 10)   # arrived from the east
        self.ref._cycled = True                          # a cycle is active too
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "emergency")


if __name__ == "__main__":
    unittest.main()
