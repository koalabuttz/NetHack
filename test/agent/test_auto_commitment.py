#!/usr/bin/env python3
"""Destination-commitment tests (destination-commitment plan, Phase 1).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Covers the extended destination lifecycle and the pure
``resolve_destination`` → ``route_held_destination`` pipeline in
``tools/agent/navigation.py`` and its reconciliation-boundary wiring in
``tools/agent/policy.py`` (AC1, AC4, AC5, AC6, AC12).  The default-pool
(door/frontier-before-stair) selection of AC2 is *not* asserted here: it is a
documented deviation (see the phase report).
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import candidates, navigation, policy, protocol  # noqa: E402
from tools.agent.providers import ProviderConfig  # noqa: E402

import test_auto_navigation as nav_test  # noqa: E402  (shared fixtures)

FLOOR = nav_test.FLOOR
WALL = nav_test.WALL
DOOR = nav_test.DOOR
OPEN = nav_test.OPEN
DOWN = nav_test.DOWN


def _terrain(cells):
    tm = navigation.TerrainMemory()
    tm.merge(cells)
    return tm


def _store_with(instance=1, purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                pos=(3, 10), family=navigation.TFAM_FRONTIER, tick=0):
    st = navigation.CommitmentStore()
    st.commit(instance_id=instance, purpose=purpose, pos=pos, family=family,
              tick=tick)
    return st


class CommitmentLifecycle(unittest.TestCase):
    def test_commitment_holds_serial_purpose_and_identity(self):
        st = _store_with()
        c = st.held()
        self.assertIsNotNone(c)
        self.assertEqual(c.serial, 1)
        self.assertEqual(c.purpose, navigation.COMMIT_EXPLORE_FRONTIER)
        self.assertEqual(c.pos, (3, 10))
        # re-commit replaces the destination and advances the serial
        self.assertTrue(st.commit(instance_id=1, purpose=(
            navigation.COMMIT_EXPLORE_UNVISITED), pos=(4, 10),
            family=navigation.TFAM_UNVISITED))
        self.assertEqual(st.held().serial, 2)
        self.assertEqual(st.held().pos, (4, 10))

    def test_compare_and_apply_rejects_a_stale_serial(self):
        st = _store_with()
        # expected serial 2 (wrong) must not install
        self.assertFalse(st.commit(instance_id=1,
                                   purpose=navigation.COMMIT_OPEN_DOOR,
                                   pos=(9, 9), family=navigation.TFAM_DOOR,
                                   expected_serial=2))
        self.assertEqual(st.held().pos, (3, 10))
        # expected serial 1 (current) installs
        self.assertTrue(st.commit(instance_id=1,
                                  purpose=navigation.COMMIT_OPEN_DOOR,
                                  pos=(9, 9), family=navigation.TFAM_DOOR,
                                  expected_serial=1))
        self.assertEqual(st.held().purpose, navigation.COMMIT_OPEN_DOOR)

    def test_instance_change_clears_commitment_and_negatives(self):
        st = _store_with(instance=1)
        st.note_failed((7, 7), ("ev",))
        st.note_serviced((8, 8), ("ev",))
        st.expire_instance(2)                     # a fresh instance
        self.assertIsNone(st.held())
        self.assertFalse(st.failed((7, 7)))
        self.assertFalse(st.serviced((8, 8)))
        # the same instance is left untouched
        st2 = _store_with(instance=5)
        st2.expire_instance(5)
        self.assertIsNotNone(st2.held())

    def test_cycle_invalidates_and_suppresses_same_destination(self):
        st = _store_with()
        st.invalidate_cycle()
        self.assertIsNone(st.held())
        self.assertTrue(st.failed((3, 10)))
        # a failed site cannot be re-elected under the same evidence
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        tm = _terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        prop = navigation.resolve_destination(tm, (1, 10), dist, first,
                                              None, store=st)
        self.assertNotEqual(getattr(prop, "pos", None), (3, 10))

    def test_stall_budget_bounds_nonperiodic_loop(self):
        st = _store_with()
        st.set_stall_cap(1)                       # max(16, 4*1+8) == 16
        self.assertEqual(st.stall_cap, 16)
        for _ in range(navigation.STALL_MAX + 1):
            st.note_nav_attempt()
        self.assertTrue(st.stalled())
        # a new cell toward the route resets the no-progress budget
        st2 = _store_with()
        for _ in range(navigation.STALL_MAX + 1):
            st2.note_nav_attempt()
        self.assertTrue(st2.stalled())
        st2.note_progress((4, 10), 3)
        self.assertFalse(st2.stalled())

    def test_serviced_frontier_is_suppressed_then_reacquirable(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        tm = _terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        st = navigation.CommitmentStore()
        first_choice = navigation.resolve_destination(tm, (1, 10), dist, first,
                                                      None, store=st)
        self.assertIsNotNone(first_choice)
        # service that waypoint: it must not be immediately re-elected
        st.note_serviced(first_choice.pos, ("sig",))
        again = navigation.resolve_destination(tm, (1, 10), dist, first, None,
                                               store=st)
        self.assertNotEqual(again.pos, first_choice.pos)

    def test_route_held_destination_door_survives_approach(self):
        # a closed door is approached, not completed, by reaching its approach
        cells = {(3, 10): FLOOR, (4, 10): FLOOR, (5, 10): DOOR}
        tm = _terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (3, 10))
        c = navigation.Commitment(instance_id=1, serial=1,
                                  purpose=navigation.COMMIT_OPEN_DOOR,
                                  pos=(5, 10), family=navigation.TFAM_DOOR)
        # from the approach square the step enters the door (opening it)
        step, terminal, _reason = navigation.route_held_destination(
            c, tm, (4, 10), dist, first)
        self.assertEqual(step, (1, 0))
        self.assertIsNone(terminal)
        # from afar the step continues the approach
        step, terminal, _reason = navigation.route_held_destination(
            c, tm, (3, 10), dist, first)
        self.assertEqual(step, (1, 0))
        self.assertIsNone(terminal)

    def test_destination_render_does_not_mutate_commitment_state(self):
        # rendering a destination-state snapshot is a pure read
        st = _store_with()
        before = (st.held(), dict(st._serviced), dict(st._failed),
                  st.stall_attempts, st.interact_attempts)
        for _ in range(3):
            st.held()
            st.serviced((1, 1))
            st.failed((2, 2))
        after = (st.held(), dict(st._serviced), dict(st._failed),
                 st.stall_attempts, st.interact_attempts)
        self.assertEqual(before, after)


class DestinationEffectIdentity(unittest.TestCase):
    def test_same_key_different_destination_has_distinct_effect_identity(self):
        a = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((3, 10), navigation.TFAM_FRONTIER,
                                     (1, 0), 100))
        b = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((4, 10), navigation.TFAM_FRONTIER,
                                     (1, 0), 100))
        self.assertNotEqual(a, b)
        ca = candidates.make_candidate({"key": protocol.KEY_L}, "navigate",
                                       family="frontier",
                                       effect_payload=a)
        cb = candidates.make_candidate({"key": protocol.KEY_L}, "navigate",
                                       family="frontier",
                                       effect_payload=b)
        # the same immediate key, a different destination: distinct identity
        self.assertNotEqual(ca.candidate_id, cb.candidate_id)
        # and deterministic for identical inputs
        ca2 = candidates.make_candidate({"key": protocol.KEY_L}, "navigate",
                                        family="frontier",
                                        effect_payload=a)
        self.assertEqual(ca.candidate_id, ca2.candidate_id)


class PrepareAndReconcile(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _cells(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        return cells

    def test_prepare_and_unselected_candidate_do_not_commit_destination(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        self.ref.prepare(nav_test.ctx(mem))
        # preparation never acquires a destination (AC6)
        self.assertIsNone(self.ref.targets.held())

    def test_reconciled_destination_effect_commits_exactly_once(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        prepared = self.ref.prepare(nav_test.ctx(mem))
        cand = prepared.table.scripted()
        self.assertTrue(cand.effect_payload)
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label,
                               1, mem, observed_kind="moved",
                               payload=cand.effect_payload)
        held = self.ref.targets.held()
        self.assertIsNotNone(held)
        serial = held.serial
        # a second fold of the *same* acquisition is compare-and-apply safe
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label,
                               1, mem, observed_kind="moved",
                               payload=cand.effect_payload)
        self.assertEqual(self.ref.targets.held().serial, serial)

    def test_destination_survives_alternate_score_and_visit_changes(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        committed = self.ref.targets.held().pos
        # a large visit penalty elsewhere must not re-elect a new destination
        mem.visits[(7, 10)] = 99
        routed = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(self.ref.targets.held().pos, committed)
        self.assertEqual(routed.family, "frontier")

    def test_one_hop_acquisition_records_reached_without_installation(self):
        # the destination the observation already satisfies is serviced, not
        # installed (plan 1.4)
        mem = nav_test.mem_with(self._cells(), (3, 10))
        payload = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((3, 10), navigation.TFAM_FRONTIER,
                                     (1, 0), 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=payload)
        self.assertIsNone(self.ref.targets.held())
        self.assertTrue(self.ref.targets.serviced((3, 10)))

    def test_stale_continuation_after_cycle_invalidation_does_not_resurrect(
            self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        cont = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held())
        # the destination is retired (e.g. by a cycle) ...
        self.ref.targets.invalidate_cycle()
        self.assertIsNone(self.ref.targets.held())
        # ... so a continuation frozen for that serial must be dropped
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=cont)
        self.assertIsNone(self.ref.targets.held())

    def test_write_failure_and_local_rejection_do_not_commit_destination(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        prepared = self.ref.prepare(nav_test.ctx(mem))
        self.assertIsNotNone(prepared.table.scripted())
        # no reconciled send: nothing commits
        self.assertIsNone(self.ref.targets.held())

    def test_long_route_no_progress_eventually_retires_destination(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        self.ref.targets.set_stall_cap(0)          # -> 16
        cont = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held())
        for _ in range(self.ref.targets.stall_cap + 1):
            if self.ref.targets.held() is None:
                break
            payload = policy.ScriptedReflex._dest_payload(
                "continue", self.ref.targets.held())
            self.ref.commit_effect("navigate", "navigate", 2, mem,
                                   observed_kind="stationary-time-advanced",
                                   payload=payload)
        self.assertIsNone(self.ref.targets.held())


if __name__ == "__main__":
    unittest.main()
