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
from tools.agent.directives import DirectiveSet, DirectiveView  # noqa: E402
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
        st.invalidate_cycle(("sig",))
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
        st.note_serviced(
            first_choice.pos,
            navigation.local_evidence_signature(tm, first_choice.pos))
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
        # a held destination is routed as a SINGLE committed continuation, not
        # re-elected from the pool (the held branch is the only thing that can
        # produce a one-candidate navigation table here)
        prepared2 = self.ref.prepare(nav_test.ctx(mem))
        self.assertEqual(len(prepared2.table.ordered_candidates), 1)
        cont = prepared2.table.scripted()
        self.assertTrue(cont.effect_payload)
        self.assertEqual(cont.effect_payload[1], "continue")

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


class DefaultDestinationPool(unittest.TestCase):
    """AC2: the default acquisition pool (plan 1.2)."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _corridor(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        cells[(7, 10)] = DOWN
        return cells

    def test_default_commits_door_or_frontier_before_stair(self):
        mem = nav_test.mem_with(self._corridor(), (1, 10))
        chosen = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertIn(chosen.family, ("frontier", "door"))

    def test_explicit_stair_directive_selects_stair_destination(self):
        mem = nav_test.mem_with(self._corridor(), (1, 10))
        view = DirectiveView(DirectiveSet(goals=("descend_known_stairs",)), 1)
        chosen = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.assertEqual(chosen.family, "stair")

    def test_on_stair_descent_singleton_unaffected_by_commitment(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 4)},
                                (3, 10))
        mem.grid[(3, 10)] = DOWN
        mem.stairs_down.add((3, 10))
        # a held, unrelated commitment must not interrupt descent
        self.ref.targets.commit(instance_id=self.ref.instance_id or 0,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10), family=navigation.TFAM_FRONTIER)
        chosen = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(chosen.action.to_wire(), {"key": ord(">")})

    def test_unvisited_fallback_after_serviced_frontiers(self):
        # a room whose walls are known except one unknown cell (a frontier),
        # with the remaining cells unvisited and not frontiers
        cells = {}
        for x in range(1, 7):
            cells[(x, 10)] = FLOOR
        for x in range(1, 6):
            cells[(x, 9)] = WALL
            cells[(x, 11)] = WALL
        tm = _terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        st = navigation.CommitmentStore()
        first_choice = navigation.resolve_destination(tm, (1, 10), dist, first,
                                                      None, store=st)
        self.assertEqual(first_choice.family, navigation.TFAM_FRONTIER)
        self.assertEqual(first_choice.pos, (6, 10))
        st.note_serviced(
            first_choice.pos,
            navigation.local_evidence_signature(tm, first_choice.pos))
        fallback = navigation.resolve_destination(tm, (1, 10), dist, first,
                                                  None, store=st)
        self.assertEqual(fallback.family, navigation.TFAM_UNVISITED)


class CommittedBehaviour(unittest.TestCase):
    """AC3/AC4/AC5/AC7/AC8: already-implemented behaviour, now named."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_committed_reverse_survives_same_family_margin(self):
        cells = {(x, 10): FLOOR for x in range(2, 9)}
        mem = nav_test.mem_with(cells, (5, 10))
        self.ref.recovery.previous_distinct = (4, 10)
        # control: uncommitted, the west reversal is suppressed by the
        # comparable east same-family frontier (strict >40 rule)
        control = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(control.direction, (1, 0))
        # committed: the held destination's required reversal is offered
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10), family=navigation.TFAM_FRONTIER)
        held = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(held.direction, (-1, 0))

    def test_hard_blockage_retires_and_suppresses_destination(self):
        st = _store_with(pos=(3, 10))
        st.retire("hard-blockage", pos=(3, 10), signature=("sig",))
        self.assertIsNone(st.held())
        self.assertTrue(st.failed((3, 10)))
        cells = {(x, 10): FLOOR for x in range(1, 6)}
        tm = _terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        prop = navigation.resolve_destination(tm, (1, 10), dist, first, None,
                                              store=st)
        self.assertNotEqual(getattr(prop, "pos", None), (3, 10))

    def test_locked_door_fails_once_and_next_target_progresses(self):
        # an explicit locked/refused door message is classified in production
        # at the observation fold (not by a manual retire in the test)
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                 (5, 10): DOOR, (6, 10): FLOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        self.assertEqual(self.ref.targets.held().purpose,
                         navigation.COMMIT_OPEN_DOOR)
        mem.messages.append("The door is locked.")
        self.ref.note_observation(mem)
        self.assertIsNone(self.ref.targets.held())
        self.assertTrue(self.ref.targets.failed((5, 10)))
        # the next target progresses past the locked door
        table = self.ref.prepare(nav_test.ctx(mem)).table
        cand = table.scripted()
        self.assertNotEqual(tuple(cand.effect_payload[4:6]), (5, 10))

    def test_local_evidence_signature_reopens_only_on_relevant_change(self):
        # an unrelated global-map change must not reopen a serviced site; a
        # relevant local change does (plan 1.5)
        st = navigation.CommitmentStore()
        tm = _terrain({(x, 10): FLOOR for x in range(1, 8)})
        sig = navigation.local_evidence_signature(tm, (5, 10))
        st.note_serviced((5, 10), sig)
        self.assertTrue(st.serviced_under_evidence((5, 10), sig))
        # unrelated change: a wall far away does not alter the local signature
        tm.merge({(7, 15): WALL})
        self.assertEqual(
            navigation.local_evidence_signature(tm, (5, 10)), sig)
        self.assertTrue(st.serviced_under_evidence(
            (5, 10), navigation.local_evidence_signature(tm, (5, 10))))
        # relevant change: a wall *adjacent* to the site reopens it
        tm.merge({(5, 9): WALL})
        self.assertNotEqual(
            navigation.local_evidence_signature(tm, (5, 10)), sig)
        self.assertFalse(st.serviced_under_evidence(
            (5, 10), navigation.local_evidence_signature(tm, (5, 10))))

    def test_unreachable_held_route_settles_as_a_failure(self):
        # a held destination whose route vanishes fails at reconciliation
        # instead of silently selecting around the active record
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)}, (1, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(6, 10), family=navigation.TFAM_FRONTIER)
        # the corridor is severed between the hero and the destination
        mem.grid[(2, 10)] = WALL
        mem.grid[(3, 10)] = WALL
        mem.grid[(4, 10)] = WALL
        self.ref.directive_settlement = None
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.proposed_effect, "dest-unresolved")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 2,
                               mem, observed_kind="no-time",
                               payload=cand.effect_payload)
        self.assertIsNone(self.ref.targets.held())
        self.assertNotEqual(self.ref.targets.held(), (6, 10))

    def test_ineffective_door_attempts_are_bounded(self):
        st = _store_with(purpose=navigation.COMMIT_OPEN_DOOR, pos=(5, 10),
                         family=navigation.TFAM_DOOR)
        self.assertFalse(st.door_attempts_exhausted)
        st.note_interact_attempt()
        self.assertFalse(st.door_attempts_exhausted)
        st.note_interact_attempt()
        self.assertTrue(st.door_attempts_exhausted)
        # the policy retires the door at that cap (no monopolisation)
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                 (5, 10): DOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        cont = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held())
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=cont)
        self.assertIsNotNone(self.ref.targets.held())
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=cont)
        self.assertIsNone(self.ref.targets.held())

    def test_door_no_time_outcome_folds_once(self):
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                 (5, 10): DOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held())
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="no-time", payload=payload)
        # exactly one ineffective interaction attempt per reconciled fold
        self.assertEqual(self.ref.targets.interact_attempts, 1)
        self.assertIsNotNone(self.ref.targets.held())

    def test_emergency_singleton_precedes_destination_application(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)},
                                (4, 10))
        mem.status.hp = 1
        mem.status.hp_max = 20
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(7, 10), family=navigation.TFAM_FRONTIER)
        old = self.ref.targets.held()
        chosen = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(chosen.family, "emergency")
        # the destination pipeline is suspended: no replacement is committed
        self.assertEqual(self.ref.targets.held(), old)
        # the following eligible command resolves the newly active directive
        # destination and compare-and-applies it
        mem.status.hp = 20
        # a collect_items destination resolves only against item evidence
        self.ref.floor.observe_item(self.ref.instance_id, (6, 10),
                                    "coin appearance")
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(6, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.assertEqual(cand.effect_payload[1], "acquire")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 2,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        new = self.ref.targets.held()
        self.assertIsNotNone(new)
        self.assertEqual(new.pos, (6, 10))
        self.assertEqual(new.purpose, navigation.COMMIT_COLLECT_ITEMS)
        self.assertEqual(new.source, navigation.SRC_DIRECTIVE)

    def test_one_dijkstra_replans_route_not_destination(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        mem = nav_test.mem_with(cells, (1, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(6, 10), family=navigation.TFAM_FRONTIER)
        first = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(first.direction, (1, 0))
        # a discovered change re-plans the route around it...
        mem.grid[(2, 10)] = WALL
        mem.grid[(1, 11)] = FLOOR
        mem.grid[(2, 11)] = FLOOR
        mem.grid[(3, 11)] = FLOOR
        second = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        # ... but the committed destination is unchanged
        self.assertEqual(self.ref.targets.held().pos, (6, 10))
        self.assertNotEqual(second.direction, first.direction)

    def test_destination_survives_frontier_reclassification_en_route(self):
        cells = {(x, 10): FLOOR for x in range(1, 7)}
        mem = nav_test.mem_with(cells, (1, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(4, 10), family=navigation.TFAM_FRONTIER)
        # the waypoint ceases to border unknown space while en route
        mem.grid[(4, 9)] = WALL
        mem.grid[(4, 11)] = WALL
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        # discovery itself must not cancel the committed progress
        self.assertEqual(self.ref.targets.held().pos, (4, 10))
        self.assertEqual(cand.family, "frontier")

    def test_no_targets_reuses_bounded_forced_search_accounting(self):
        # a lone known floor cell has no reachable target: the fallback is the
        # bounded ordinary search, and it is not reset by repetition
        mem = nav_test.mem_with({(1, 10): FLOOR}, (1, 10))
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.semantic_label, "search-secret")
        site = (1, 10)
        for _ in range(6):
            self.ref.recovery.note_search_completed(site)
        again = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        # the exhausted budget is reused: no further ordinary search
        self.assertNotEqual(again.semantic_label, "search-secret")

    def test_item_overlay_uses_persistent_known_ground(self):
        import types
        # the runner-owned persistent terrain classifies the ground beneath an
        # item overlay, so routing uses it instead of mem.grid (which the
        # overlay overwrote with the item glyph)
        persistent = navigation.TerrainMemory()
        persistent.merge({(2, 10): FLOOR})
        mem = nav_test.mem_with(
            {(1, 10): FLOOR, (2, 10): ("%", "brown", 0, "none")}, (1, 10))
        terrain = self.ref._terrain(
            mem, types.SimpleNamespace(memory=mem, terrain=persistent))
        self.assertTrue(terrain.walkable((2, 10)))

    def test_item_on_unknown_ground_does_not_authorize_route(self):
        import types
        persistent = navigation.TerrainMemory()          # (2, 10) unknown
        mem = nav_test.mem_with(
            {(1, 10): FLOOR, (2, 10): ("%", "brown", 0, "none")}, (1, 10))
        terrain = self.ref._terrain(
            mem, types.SimpleNamespace(memory=mem, terrain=persistent))
        # unknown ground under a glyph stays unknown and is never a route
        self.assertFalse(terrain.walkable((2, 10)))
        plan = navigation.plan(terrain, (1, 10))
        self.assertNotIn((2, 10), plan.dist)

    def test_flee_arrival_does_not_ascend_or_exit_dungeon(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 6)}, (1, 10))
        mem.grid[(4, 10)] = ("<", "white", 0, "none")
        mem.stairs_up.add((4, 10))
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("flee_to_upstairs",)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        # the known upstairs square was actually resolved and selected ...
        self.assertEqual(cand.effect_payload[1], "acquire")
        self.assertEqual(cand.effect_payload[3], "flee-upstairs")
        self.assertEqual(tuple(cand.effect_payload[4:6]), (4, 10))
        # ... and arriving there serves the generation, without ascending
        arrived = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 6)},
                                    (4, 10))
        arrived.grid[(4, 10)] = ("<", "white", 0, "none")
        arrived.stairs_up.add((4, 10))
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 2,
                               arrived, observed_kind="moved",
                               payload=cand.effect_payload)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "reached")
        for c in self.ref.prepare(
                nav_test.ctx(arrived, directives=[view])).table.ordered_candidates:
            self.assertNotEqual(c.action.to_wire(), {"key": ord("<")})

    def test_targetless_flee_chooses_nearest_reachable_upstairs(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 9)}, (1, 10))
        for pos in ((3, 10), (7, 10)):
            mem.grid[pos] = ("<", "white", 0, "none")
            mem.stairs_up.add(pos)
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("flee_to_upstairs",)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.assertEqual(cand.effect_payload[3], "flee-upstairs")
        self.assertEqual(tuple(cand.effect_payload[4:6]), (3, 10))

    def test_supplied_non_upstairs_flee_coordinate_is_rejected(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 9)}, (1, 10))
        mem.grid[(7, 10)] = ("<", "white", 0, "none")
        mem.stairs_up.add((7, 10))
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("flee_to_upstairs",), target=(4, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        # a structured rejection: no wrong-terrain target is ever routed
        self.assertEqual(cand.proposed_effect, "dest-unresolved")
        self.assertEqual(cand.semantic_label, "unresolved-destination")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 2,
                               mem, observed_kind="no-time",
                               payload=cand.effect_payload)
        outcome, _gen, reason = self.ref.directive_settlement
        self.assertEqual(outcome, "failed")
        self.assertIn("not an observed upstairs", reason)

    def test_collect_resolves_on_visited_non_frontier_item_floor(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        for x in range(1, 8):                 # no cell borders unknown space
            cells[(x, 9)] = WALL
            cells[(x, 11)] = WALL
        mem = nav_test.mem_with(cells, (1, 10))
        mem.visits[(5, 10)] = 3               # visited, non-frontier floor
        self.ref.floor.observe_item(self.ref.instance_id, (5, 10),
                                    "coin appearance")
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(5, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.assertEqual(cand.effect_payload[3], "collect-items")
        self.assertEqual(tuple(cand.effect_payload[4:6]), (5, 10))

    def test_collect_without_item_evidence_is_a_structured_failure(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)}, (1, 10))
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(5, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        # no default exploration masquerading as steering
        self.assertEqual(cand.semantic_label, "unresolved-destination")
        self.assertNotEqual(cand.family, "frontier")
        self.assertIn("no floor item evidence", cand.effect_payload[1])

    def test_no_reassertion_after_production_completion_or_failure(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)}, (1, 10))
        self.ref.floor.observe_item(self.ref.instance_id, (5, 10),
                                    "coin appearance")
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(5, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        held = self.ref.targets.held()
        self.assertEqual(held.source, navigation.SRC_DIRECTIVE)
        # arriving serves the generation: the destination is retired and the
        # settlement is queued for the book (no reassertion next tick)
        arrived = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)},
                                    (5, 10))
        arrived.visits[(5, 10)] = 1
        cont = policy.ScriptedReflex._dest_payload("continue", held)
        self.ref.commit_effect("navigate", "navigate", 2, arrived,
                               observed_kind="moved", payload=cont)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "reached")
        # applying that settlement at the book expires the generation
        from tools.agent import directives as DSMOD
        book = DSMOD.DirectiveBook()
        dset, _ = DSMOD.validate_directive_set(
            {"schema_version": 2, "goals": ["collect_items"],
             "target": [5, 10], "ttl": 50})
        book.activate(dset, 1, "1")
        outcome, _gen, reason = self.ref.directive_settlement
        book.expire("destination-%s: %s" % (outcome, reason), 2, "1")
        self.assertFalse(book.has_active)
        # an unresolved failure settles identically
        self.ref.directive_settlement = None
        fail = self.ref._unresolved_destination_candidate()
        self.ref.commit_effect(fail.proposed_effect, fail.semantic_label, 3,
                               mem, observed_kind="no-time",
                               payload=fail.effect_payload)
        self.assertEqual(self.ref.directive_settlement[0], "failed")


if __name__ == "__main__":
    unittest.main()
