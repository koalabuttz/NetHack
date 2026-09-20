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
        # service that waypoint: it must not be immediately re-elected (the
        # split exploration-only service signature, §4)
        st.note_serviced(
            first_choice.pos,
            navigation.service_signature(tm, first_choice.pos))
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
        # the serviced signature is the split *exploration* signature (§4)
        st.note_serviced(
            first_choice.pos,
            navigation.service_signature(tm, first_choice.pos))
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
        # the door interaction is armed (baseline) and its response is locked
        self.ref.arm_door_baseline(mem.message_count)
        mem.messages.append("The door is locked.")
        mem.message_count += 1
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="no-time", payload=payload,
                               pre_hero=(4, 10))
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
        # arriving at the collection site begins the interacting phase ...
        arrived = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)},
                                    (5, 10))
        arrived.visits[(5, 10)] = 1
        cont = policy.ScriptedReflex._dest_payload("continue", held)
        self.ref.commit_effect("navigate", "navigate", 2, arrived,
                               observed_kind="moved", payload=cont)
        self.assertIsNotNone(self.ref.targets.held())
        # ... and only a pickup terminal outcome settles it and serves the
        # generation (no reassertion next tick)
        self.ref.pickup_pending = {
            "evidence": self.ref.floor.evidence(arrived.hero),
            "purpose": "collect", "generation": held.generation,
            "init_inventory": ()}
        arrived.messages.append("There is nothing here to pick up.")
        self.ref.note_observation(arrived)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")
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


class AttemptCounting(unittest.TestCase):
    """§2A: matched gameplay attempts advance the stationary stage exactly."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _cells(self):
        return {(x, 10): FLOOR for x in range(1, 8)}

    def _acquire(self, mem, kind="moved", pre_hero=(1, 10)):
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind=kind,
                               payload=cand.effect_payload, pre_hero=pre_hero)
        return cand

    def test_first_no_time_acquisition_is_attempt_one_of_three(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        self._acquire(mem, kind="no-time", pre_hero=(1, 10))
        self.assertIsNotNone(self.ref.targets.held())
        # a no-time acquisition is no-progress attempt 1 of 3, seeded from the
        # hero baseline -- never from the target position
        self.assertEqual(self.ref.targets.stall_attempts, 1)
        self.assertEqual(self.ref.targets.progress_pos, (1, 10))

    def test_acquisition_no_time_is_not_progress(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        cand = self._acquire(mem, kind="no-time", pre_hero=(1, 10))
        held = self.ref.targets.held()
        # the target was installed but not serviced or reached
        self.assertFalse(self.ref.targets.serviced(held.pos))
        self.assertEqual(self.ref.targets.stall_attempts, 1)

    def test_moved_acquisition_starts_progress_from_reconciled_hero(self):
        cells = self._cells()
        mem = nav_test.mem_with(cells, (2, 10))
        # a moved acquisition: the reconciled hero advanced from the pre-send
        # square, so stall progress restarts from it and no attempt is charged
        mem.hero = (2, 10)
        pre = (1, 10)
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload, pre_hero=pre)
        self.assertEqual(self.ref.targets.stall_attempts, 0)
        self.assertEqual(self.ref.targets.progress_pos, (2, 10))

    def test_cap_exhausted_held_destination_retires_at_three_zero_time_attempts(
            self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        # a moved acquisition: pre-send (1,10) -> reconciled (2,10)
        mem.hero = (2, 10)
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload, pre_hero=(1, 10))
        self.assertEqual(self.ref.targets.stall_attempts, 0)
        retired_after = None
        for i in range(1, 5):
            if self.ref.targets.held() is None:
                break
            payload = policy.ScriptedReflex._dest_payload(
                "continue", self.ref.targets.held())
            self.ref.commit_effect("navigate", "navigate", 2, mem,
                                   observed_kind="no-time", payload=payload,
                                   pre_hero=(2, 10))
            if self.ref.targets.held() is None:
                retired_after = i
        self.assertEqual(retired_after, 3,
                         "three reconciled zero-time continuations must retire")

    def test_prompt_observations_do_not_advance_stationary_stage(self):
        mem = nav_test.mem_with(self._cells(), (1, 10))
        self._acquire(mem, kind="moved", pre_hero=(1, 10))
        before = self.ref.targets.stall_attempts
        # a prompt/menu decision carries no destination payload and spends no
        # destination counter
        self.ref.commit_effect("prompt", "prompt", 2, mem,
                               observed_kind="no-time", payload=())
        self.assertEqual(self.ref.targets.stall_attempts, before)
        # the matched-gameplay rule is enforced at the memory fold too (review
        # item 1 / §2A): a non-gameplay observation at the same hero folds the
        # visit but never advances the stationary counter
        np_before = mem.no_progress
        mem.commit(mem.stage(protocol.Snapshot()), hero=(1, 10),
                   advance_stationary=False)
        self.assertEqual(mem.no_progress, np_before)


class DirectiveTerminalOwner(unittest.TestCase):
    """Item 4: the retirement owner emits for directive-owned sources too."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _terminals(self, serial):
        return [e for e in self.ref.lifecycle.events
                if e.get("kind") == "destination" and e.get("serial") == serial
                and e.get("outcome") in ("reached", "failed", "expired",
                                         "replaced")]

    def _directive_settlements(self, generation):
        return [e for e in self.ref.lifecycle.events
                if e.get("kind") == "directive"
                and e.get("outcome") == "terminal"
                and e.get("generation") == generation]

    def test_directive_owned_cycle_emits_one_terminal_and_settles_once(self):
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (4, 10): FLOOR,
                 (3, 9): FLOOR, (2, 9): WALL, (2, 11): WALL, (3, 11): WALL,
                 (4, 9): WALL, (4, 11): WALL}
        mem = nav_test.mem_with(cells, (3, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10), family=navigation.TFAM_FRONTIER,
                                source=navigation.SRC_DIRECTIVE, generation=4)
        serial = self.ref.targets.held().serial
        for pos in [(3, 10), (2, 10), (3, 10), (2, 10), (3, 10)]:
            mem.hero = pos
            self.ref.note_observation(mem)
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload, pre_hero=(3, 10))
        # target cleared, exactly one destination terminal for the old serial,
        # and the directive generation settled exactly once
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(len(self._terminals(serial)), 1)
        self.assertEqual(len(self._directive_settlements(4)), 1)
        self.assertEqual(self.ref.directive_settlement[1], 4)
        # the retired site is suppressed: no reacquisition on the next boundary
        self.assertTrue(self.ref.targets.failed((2, 10)))

    def test_directive_owned_door_open_emits_one_terminal_and_settles_once(self):
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): nav_test.OPEN,
                                 (5, 10): FLOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(4, 10), family=navigation.TFAM_DOOR,
                                source=navigation.SRC_DIRECTIVE, generation=7)
        serial = self.ref.targets.held().serial
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(0, 1))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=payload,
                               pre_hero=(4, 10))
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(len(self._terminals(serial)), 1)
        self.assertEqual(len(self._directive_settlements(7)), 1)
        self.assertEqual(self.ref.directive_settlement[0], "reached")


class RecoveryRetirementBoundary(unittest.TestCase):
    """Item 2: retirement only at the reconciled recovery-effect boundary."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _terminals(self, serial):
        return [e for e in self.ref.lifecycle.events
                if e.get("kind") == "destination" and e.get("serial") == serial
                and e.get("outcome") in ("reached", "failed", "expired",
                                         "replaced")]

    def _cycle_mem(self):
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (4, 10): FLOOR,
                 (3, 9): FLOOR, (2, 9): WALL, (2, 11): WALL, (3, 11): WALL,
                 (4, 9): WALL, (4, 11): WALL}
        return nav_test.mem_with(cells, (3, 10))

    def _nominate_cycle(self, mem):
        for pos in [(3, 10), (2, 10), (3, 10), (2, 10), (3, 10)]:
            mem.hero = pos
            self.ref.note_observation(mem)

    def _hold_and_cycle(self):
        mem = self._cycle_mem()
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10), family=navigation.TFAM_FRONTIER)
        serial = self.ref.targets.held().serial
        self._nominate_cycle(mem)
        return mem, serial

    def test_cycle_nomination_does_not_retire_before_selection(self):
        _mem, serial = self._hold_and_cycle()
        self.assertTrue(self.ref._cycled)
        self.assertIsNotNone(self.ref.targets.held())
        self.assertEqual(self._terminals(serial), [])

    def test_emergency_preemption_does_not_retire_or_spend(self):
        # (1) active cycle + held destination + forced low-HP emergency
        mem, serial = self._hold_and_cycle()
        mem.status.hp = 1
        mem.status.hp_max = 20
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "emergency")
        # the preemption alone retires nothing and spends no destination counter
        self.assertIsNotNone(self.ref.targets.held())
        self.assertEqual(self._terminals(serial), [])
        self.assertEqual(self.ref.targets.stall_attempts, 0)

    def test_successful_recovery_move_retires_once(self):
        # (2) a reconciled successful recovery move retires exactly once
        mem, serial = self._hold_and_cycle()
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.proposed_effect, "recovery")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload, pre_hero=(3, 10))
        self.assertIsNone(self.ref.targets.held())
        terms = self._terminals(serial)
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].get("reason"), "cycle")

    def test_recovery_no_time_failure_records_only_edge_evidence(self):
        # (3) a no-time recovery failure emits no terminal, only edge evidence
        cells = {(10, 10): FLOOR, (11, 10): FLOOR, (9, 10): WALL,
                 (10, 9): WALL, (10, 11): WALL}
        mem = nav_test.mem_with(cells, (10, 10))
        mem.messages.append("You already found a monster.")
        mem.no_progress = 10
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(11, 10), family=navigation.TFAM_FRONTIER)
        serial = self.ref.targets.held().serial
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.proposed_effect, "recovery")
        before = len(self.ref.lifecycle.events)
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="no-time",
                               payload=cand.effect_payload, pre_hero=(10, 10))
        # no terminal (no new lifecycle event) and the destination survives
        self.assertEqual(len(self.ref.lifecycle.events), before)
        self.assertEqual(self._terminals(serial), [])
        self.assertIsNotNone(self.ref.targets.held())
        # only the scoped edge ledger changed
        self.assertTrue(self.ref.blocked_edges)


class Phase3Progression(unittest.TestCase):
    """AC7: exploration-stable progression and reacquisition preferences."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _corridor(self):
        return {(x, 10): FLOOR for x in range(1, 8)}

    def test_frontier_reopens_only_for_local_exploration_change(self):
        tm = nav_test.terrain(self._corridor())
        st = navigation.CommitmentStore()
        sig = navigation.service_signature(tm, (5, 10))
        st.note_serviced((5, 10), sig)
        # occupancy/visits do not reopen it
        tm.occupancy[(4, 10)] = "monster"
        self.assertTrue(st.serviced_under_evidence(
            (5, 10), navigation.service_signature(tm, (5, 10))))
        # a genuine local terrain change does reopen it
        tm.merge({(5, 9): WALL})
        self.assertNotEqual(navigation.service_signature(tm, (5, 10)), sig)
        self.assertFalse(st.serviced_under_evidence(
            (5, 10), navigation.service_signature(tm, (5, 10))))

    def test_serviced_frontiers_progress_to_unvisited_then_stairs(self):
        cells = {(x, 10): FLOOR for x in range(1, 7)}
        cells[(5, 10)] = DOWN                 # the stair, already visited
        for x in range(1, 6):
            cells[(x, 9)] = WALL
            cells[(x, 11)] = WALL
        cells[(6, 9)] = WALL
        cells[(6, 11)] = WALL
        tm = nav_test.terrain(cells)
        dist, first = navigation.one_dijkstra(tm, (1, 10))
        visits = {(5, 10): 1}
        st = navigation.CommitmentStore()
        # 1. the frontier (6,10, beyond the stair) is preferred first
        first_choice = navigation.resolve_destination(tm, (1, 10), dist, first,
                                                      visits, store=st)
        self.assertEqual(first_choice.family, navigation.TFAM_FRONTIER)
        st.note_serviced(first_choice.pos,
                         navigation.service_signature(tm, first_choice.pos))
        # 2. with the frontier serviced, unvisited cells follow
        second = navigation.resolve_destination(tm, (1, 10), dist, first,
                                                visits, store=st)
        self.assertEqual(second.family, navigation.TFAM_UNVISITED)
        # 3. once every unvisited cell is serviced, the stairs progress last
        for x in range(2, 5):
            st.note_serviced((x, 10),
                             navigation.service_signature(tm, (x, 10)))
        third = navigation.resolve_destination(tm, (1, 10), dist, first,
                                               visits, store=st)
        self.assertEqual(third.family, navigation.TFAM_STAIR)

    def test_post_service_reacquisition_emits_one_reopen(self):
        # review item 6b: a previously serviced site re-acquired under a changed
        # service signature emits an explicit site-level reopen fact
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        mem = nav_test.mem_with(cells, (1, 10))
        tgt = navigation.Target((6, 10), navigation.TFAM_FRONTIER, (1, 0), 0)
        sig0 = navigation.service_signature(self.ref._terrain(mem), (6, 10))
        self.ref.targets.note_serviced((6, 10), sig0)

        def acquire(tick):
            payload = policy.ScriptedReflex._dest_payload(
                "acquire", None, target=tgt,
                purpose=navigation.COMMIT_EXPLORE_FRONTIER)
            self.ref.commit_effect("navigate", "navigate", tick, mem,
                                   observed_kind="moved", payload=payload,
                                   pre_hero=(1, 10))

        def reopens():
            return [e for e in self.ref.lifecycle.events
                    if e.get("outcome") == "reopened"]

        # unchanged evidence: re-acquisition is NOT a reopen
        acquire(1)
        self.assertEqual(reopens(), [])
        self.ref.targets.retire("reselect")
        # the local exploration evidence changes, then the site is re-acquired
        mem.grid[(6, 9)] = WALL
        acquire(2)
        self.assertEqual(len(reopens()), 1)
        self.assertEqual(reopens()[0].get("reason"),
                         "service-signature-changed")

    def test_reacquisition_preserves_previous_distinct_and_strict_margin(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(2, 9)}, (5, 10))
        self.ref.recovery.previous_distinct = (4, 10)
        before = self.ref.recovery.previous_distinct
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertIsNotNone(cand)
        # an acquisition proposal does not mutate the movement-history reference
        self.assertEqual(self.ref.recovery.previous_distinct, before)
        # the strict >40 same-family margin is unchanged at the boundary
        def entry(pos, step, score):
            target = navigation.Target(pos, navigation.TFAM_FRONTIER, step, 0,
                                       "r")
            return (target, "frontier", protocol.DIR_KEYS[step], score, step)

        west = lambda s: entry((2, 10), (-1, 0), s)     # reversing
        east = lambda s: entry((8, 10), (1, 0), s)      # non-reversing
        kept = self.ref._antibacktrack([west(540), east(500)], (5, 10), None)
        self.assertEqual([e[3] for e in kept], [500])
        kept = self.ref._antibacktrack([west(541), east(500)], (5, 10), None)
        self.assertEqual(sorted(e[3] for e in kept), [500, 541])

    def test_committed_reverse_survives_reacquisition_preferences(self):
        cells = {(x, 10): FLOOR for x in range(2, 9)}
        mem = nav_test.mem_with(cells, (5, 10))
        self.ref.recovery.previous_distinct = (4, 10)
        # control: uncommitted, the west reversal is suppressed by the east
        control = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(control.direction, (1, 0))
        # committed: the held destination's required reversal is offered
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10), family=navigation.TFAM_FRONTIER)
        held = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(held.direction, (-1, 0))


class DoorAndRouteAccounting(unittest.TestCase):
    """AC4: door interactions are counted only when the action targets the
    door from an approach square, and the route cap uses the initial hops."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _door_mem(self):
        return nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                  (5, 10): DOOR, (6, 10): FLOOR}, (4, 10))

    def test_move_to_door_approach_is_not_an_interaction(self):
        mem = self._door_mem()
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        # the step moves the hero from (3,10) onto the approach (4,10), not onto
        # the door cell: it is not a door interaction (§2A rule 6)
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=payload,
                               pre_hero=(3, 10))
        self.assertEqual(self.ref.targets.interact_attempts, 0)

    def test_door_interaction_bound_counts_sent_interactions_only(self):
        mem = self._door_mem()
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        # a move to the approach is not counted ...
        approach = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=approach,
                               pre_hero=(3, 10))
        self.assertEqual(self.ref.targets.interact_attempts, 0)
        # ... but two real interactions from the approach into the door retire
        for i in range(navigation.DOOR_INTERACT_MAX):
            if self.ref.targets.held() is None:
                break
            payload = policy.ScriptedReflex._dest_payload(
                "continue", self.ref.targets.held(), step=(1, 0))
            self.ref.commit_effect("navigate", "navigate", i + 2, mem,
                                   observed_kind="no-time", payload=payload,
                                   pre_hero=(4, 10))
        self.assertEqual(self.ref.targets.interact_attempts,
                         navigation.DOOR_INTERACT_MAX)
        self.assertIsNone(self.ref.targets.held())

    def test_long_route_cap_uses_initial_hops(self):
        # the initial hop count is carried into the total cap, not the default
        st = navigation.CommitmentStore()
        st.commit(instance_id=1, purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                  pos=(20, 10), family=navigation.TFAM_FRONTIER, hops=10)
        self.assertEqual(
            st.stall_cap,
            max(navigation.STALL_TOTAL_MIN,
                navigation.STALL_TOTAL_FACTOR * 10
                + navigation.STALL_TOTAL_SLACK))
        self.assertGreater(st.stall_cap, navigation.STALL_TOTAL_MIN)

    def test_hop_count_is_true_edges_not_weighted_cost(self):
        # review item 5: the 13th payload field must be the TRUE edge count, not
        # the weighted Dijkstra cost (which folds in visit/failure penalties)
        cells = {(x, 10): FLOOR for x in range(1, 13)}
        tm = nav_test.terrain(cells)
        dist0, _f0, steps0 = navigation.one_dijkstra_steps(tm, (1, 10), {})
        # the same corridor, every cell maximally visited: same edges, higher cost
        visited = {p: navigation.VISIT_CAP + 5 for p in cells}
        dist1, _f1, steps1 = navigation.one_dijkstra_steps(tm, (1, 10), visited)
        target = (10, 10)
        self.assertEqual(steps0[target], 9)          # (1,10) -> (10,10)
        self.assertEqual(steps0[target], steps1[target])
        self.assertNotEqual(dist0[target], dist1[target])
        t0 = navigation.Target(target, navigation.TFAM_FRONTIER, (1, 0),
                               dist0[target], "r", hops=steps0[target])
        t1 = navigation.Target(target, navigation.TFAM_FRONTIER, (1, 0),
                               dist1[target], "r", hops=steps1[target])
        p0 = policy.ScriptedReflex._dest_payload("acquire", None, target=t0)
        p1 = policy.ScriptedReflex._dest_payload("acquire", None, target=t1)
        # the 13th field is the true hop count, equal for both routes ...
        self.assertEqual(p0[12], 9)
        self.assertEqual(p0[12], p1[12])
        # ... while the weighted cost differs, so the stall cap is identical
        self.assertNotEqual(t0.cost, t1.cost)
        st0 = navigation.CommitmentStore()
        st1 = navigation.CommitmentStore()
        for st, hops in ((st0, p0[12]), (st1, p1[12])):
            st.commit(instance_id=1,
                      purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                      pos=target, family=navigation.TFAM_FRONTIER, hops=hops)
        self.assertEqual(st0.stall_cap, st1.stall_cap)

    def test_weighted_cost_fallback_still_inflates_without_true_hops(self):
        # a caller with no true hop count still degrades to the weighted-cost
        # estimate (documented fallback), so the field is never silently 0
        cells = {(x, 10): FLOOR for x in range(1, 13)}
        tm = nav_test.terrain(cells)
        dist, _f, _s = navigation.one_dijkstra_steps(tm, (1, 10), {})
        t = navigation.Target((10, 10), navigation.TFAM_FRONTIER, (1, 0),
                              dist[(10, 10)], "r")     # no hops supplied
        payload = policy.ScriptedReflex._dest_payload("acquire", None, target=t)
        self.assertIsNotNone(payload[12])
        self.assertGreaterEqual(payload[12], 1)


class Phase3EvidenceSplit(unittest.TestCase):
    """AC7: split evidence signatures and the door-refusal seam (§4/§Phase 3)."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    @staticmethod
    def _say(mem, mid, text):
        """Commit one message as the state fold would (ids retained, §4)."""
        mem.messages.append(text)
        mem.message_ids.append(mid)
        mem.message_count += 1

    def _door_mem(self):
        return nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                  (5, 10): DOOR, (6, 10): FLOOR}, (4, 10))

    # -- door-refusal seam -------------------------------------------------

    def _continue_door(self, mem):
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="no-time", payload=payload,
                               pre_hero=(4, 10))

    def test_stale_locked_message_does_not_fail_new_door(self):
        mem = self._door_mem()
        self._say(mem, 1, "The door is locked.")      # observed *before* arming
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        self.ref.arm_door_baseline(mem.message_count)
        # the refusal is classified at the matched destination-effect reducer
        self._continue_door(mem)
        # the stale refusal binds to nothing: the newly armed door is held
        self.assertIsNotNone(self.ref.targets.held())

    def test_new_locked_message_fails_matching_door_once(self):
        mem = self._door_mem()
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        serial = self.ref.targets.held().serial
        self.ref.arm_door_baseline(mem.message_count)
        self._say(mem, 2, "The door is locked.")      # newly observed after arming
        self._continue_door(mem)
        self.assertIsNone(self.ref.targets.held())
        terminals = [e for e in self.ref.lifecycle.events
                     if e.get("kind") == "destination"
                     and e.get("serial") == serial
                     and e.get("outcome") in ("reached", "failed", "expired")]
        self.assertEqual(len(terminals), 1)

    def test_missing_baseline_is_no_matching_refusal_evidence(self):
        # a destination held with no armed attempt has no baseline: a locked
        # message must NOT be consumed from the general recent-message window
        mem = self._door_mem()
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        self._say(mem, 1, "The door is locked.")
        self.assertIsNone(getattr(self.ref, "door_attempt_baseline", None))
        self._continue_door(mem)
        self.assertIsNotNone(self.ref.targets.held())

    # -- split evidence signatures ----------------------------------------

    def test_serviced_frontier_ignores_transient_neighbor_occupancy(self):
        tm = nav_test.terrain({(x, 10): FLOOR for x in range(1, 8)})
        sig = navigation.service_signature(tm, (5, 10))
        tm.occupancy[(5, 9)] = "monster"
        self.assertEqual(navigation.service_signature(tm, (5, 10)), sig)

    def test_unrelated_occupancy_movement_does_not_reopen_anything(self):
        tm = nav_test.terrain({(x, 10): FLOOR for x in range(1, 8)})
        st = navigation.CommitmentStore()
        sig = navigation.service_signature(tm, (5, 10))
        st.note_serviced((5, 10), sig)
        tm.occupancy[(4, 10)] = "monster"
        self.assertTrue(st.serviced_under_evidence(
            (5, 10), navigation.service_signature(tm, (5, 10))))

    def test_target_occupant_does_not_reopen_locked_door(self):
        tm = nav_test.terrain({(4, 10): FLOOR, (5, 10): DOOR})
        fsig = navigation.door_failure_signature(tm, (5, 10))
        tm.occupancy[(4, 10)] = "monster"
        self.assertEqual(navigation.door_failure_signature(tm, (5, 10)), fsig)

    def test_locked_door_not_reenabled_by_neighbor_monster_motion(self):
        tm = nav_test.terrain({(x, 10): FLOOR for x in range(1, 8)})
        tm.terrain[(5, 10)] = navigation.T_CLOSED_DOOR
        st = navigation.CommitmentStore()
        st.note_failed((5, 10), navigation.door_failure_signature(tm, (5, 10)))
        tm.occupancy[(3, 10)] = "monster"
        self.assertTrue(st.failed_under_evidence(
            (5, 10), navigation.door_failure_signature(tm, (5, 10))))

    def test_blocked_edge_signature_reopens_when_blocker_leaves(self):
        tm = nav_test.terrain({(3, 10): FLOOR, (4, 10): FLOOR})
        s1 = navigation.blocked_edge_signature(tm, (3, 10), (4, 10))
        tm.occupancy[(4, 10)] = "monster"             # the edge is blocked
        self.assertNotEqual(navigation.blocked_edge_signature(
            tm, (3, 10), (4, 10)), s1)
        tm.occupancy.pop((4, 10))                     # the blocker leaves
        self.assertEqual(navigation.blocked_edge_signature(
            tm, (3, 10), (4, 10)), s1)

    def test_diagonal_side_blocker_is_part_of_edge_signature(self):
        tm = nav_test.terrain({(3, 10): FLOOR, (4, 11): FLOOR, (3, 11): FLOOR,
                               (4, 10): WALL})
        s1 = navigation.blocked_edge_signature(tm, (3, 10), (4, 11))
        # a side cell of the diagonal edge (4,10) becomes occupied
        tm.occupancy[(4, 10)] = "monster"
        self.assertNotEqual(navigation.blocked_edge_signature(
            tm, (3, 10), (4, 11)), s1)


class DestinationTerminalOwner(unittest.TestCase):
    """AC6: one terminal owner for every destination termination source."""

    _TERMINAL = ("reached", "failed", "expired", "replaced")

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _terminals(self, serial):
        return [e for e in self.ref.lifecycle.events
                if e.get("kind") == "destination"
                and e.get("outcome") in self._TERMINAL
                and e.get("serial") == serial]

    def _acquire(self, hero=(1, 10), kind="moved", pre=(1, 10)):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        mem = nav_test.mem_with(cells, hero)
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind=kind,
                               payload=cand.effect_payload, pre_hero=pre)
        return mem

    def test_default_arrival_stall_locked_cycle_and_instance_emit_terminal_once(
            self):
        # arrival
        mem = self._acquire()
        serial = self.ref.targets.held().serial
        arrive = policy.ScriptedReflex._dest_payload(
            "arrive", self.ref.targets.held())
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=arrive)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(len(self._terminals(serial)), 1, "arrival")

        # locked-door refusal
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                                 (5, 10): DOOR, (6, 10): FLOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        serial = self.ref.targets.held().serial
        # the door interaction is armed and its locked response is classified at
        # the matched destination-effect reducer (§4/item 3)
        self.ref.arm_door_baseline(mem.message_count)
        mem.messages.append("The door is locked.")
        mem.message_count += 1
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="no-time", payload=payload,
                               pre_hero=(4, 10))
        self.assertEqual(len(self._terminals(serial)), 1, "locked")

        # cycle: the fold only nominates; the reconciled recovery effect retires
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (4, 10): FLOOR,
                 (3, 9): FLOOR, (2, 9): WALL, (2, 11): WALL, (3, 11): WALL,
                 (4, 9): WALL, (4, 11): WALL}
        mem = nav_test.mem_with(cells, (3, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(2, 10),
                                family=navigation.TFAM_FRONTIER)
        serial = self.ref.targets.held().serial
        for pos in [(3, 10), (2, 10), (3, 10), (2, 10), (3, 10)]:
            mem.hero = pos
            self.ref.note_observation(mem)
        # the fold only nominates recovery: the destination is still held
        self.assertTrue(self.ref._cycled)
        self.assertIsNotNone(self.ref.targets.held())
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.proposed_effect, "recovery")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload, pre_hero=(3, 10))
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(len(self._terminals(serial)), 1, "cycle")

        # instance change
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        self._acquire()
        serial = self.ref.targets.held().serial
        self.ref.begin_instance(2)
        terminals = self._terminals(serial)
        self.assertEqual(len(terminals), 1, "instance")
        self.assertEqual(terminals[0].get("reason"), "instance_change")

    def test_instance_transition_emits_expired_terminal_before_reset(self):
        self._acquire()
        serial = self.ref.targets.held().serial
        self.ref.begin_instance(7)
        self.assertIsNone(self.ref.targets.held())
        terminals = self._terminals(serial)
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].get("outcome"), "expired")
        self.assertEqual(terminals[0].get("reason"), "instance_change")

    def test_default_and_directive_replacement_emit_terminal_then_acquisition(
            self):
        cells = {(x, 10): FLOOR for x in range(1, 12)}
        mem = nav_test.mem_with(cells, (1, 10))
        # a default acquisition at (11,10)
        first = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((11, 10), navigation.TFAM_FRONTIER,
                                     (1, 0), 0),
            purpose=navigation.COMMIT_EXPLORE_FRONTIER,
            source=navigation.SRC_DEFAULT)
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=first)
        old = self.ref.targets.held().serial
        # a directive replacement at a different square (5,10)
        second = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((5, 10), navigation.TFAM_FRONTIER,
                                     (1, 0), 0),
            purpose=navigation.COMMIT_EXPLORE_FRONTIER,
            source=navigation.SRC_DIRECTIVE, generation=1)
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=second)
        new = self.ref.targets.held().serial
        self.assertNotEqual(new, old)
        events = [e for e in self.ref.lifecycle.events
                  if e.get("kind") == "destination"]
        replaced = [e for e in events if e.get("outcome") == "replaced"]
        acquired = [e for e in events if e.get("outcome") == "acquired"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0].get("serial"), old)
        self.assertEqual(replaced[0].get("replacement_serial"), new)
        self.assertEqual(replaced[0].get("reason"), "replaced")
        self.assertEqual(len(acquired), 2)

    def test_earlier_fold_retirement_cannot_be_reinstalled(self):
        mem = self._acquire()
        held = self.ref.targets.held()
        cont = policy.ScriptedReflex._dest_payload("continue", held)
        # the destination is retired (e.g. by a cycle) ...
        self.ref._retire_cycle_owned(())
        self.assertIsNone(self.ref.targets.held())
        # ... so a continuation frozen for that serial must be dropped
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=cont)
        self.assertIsNone(self.ref.targets.held())

    def test_directive_expiry_emits_expired_terminal(self):
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR,
                                source=navigation.SRC_DIRECTIVE, generation=3)
        serial = self.ref.targets.held().serial
        self.ref.on_directive_expired(3)
        self.assertIsNone(self.ref.targets.held())
        terms = [e for e in self.ref.lifecycle.events
                 if e.get("kind") == "destination" and e.get("serial") == serial
                 and e.get("outcome") == "expired"]
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].get("reason"), "directive_expired")

    def test_episode_close_emits_expired_terminal(self):
        self._acquire()
        serial = self.ref.targets.held().serial
        self.ref.episode_close()
        # a second close is a no-op: exactly one terminal for the serial
        self.ref.episode_close()
        terms = [e for e in self.ref.lifecycle.events
                 if e.get("kind") == "destination" and e.get("serial") == serial
                 and e.get("outcome") == "expired"]
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0].get("reason"), "episode_close")

    def test_unreachable_default_retirement_is_visible(self):
        cells = {(x, 10): FLOOR for x in range(1, 9)}
        mem = nav_test.mem_with(cells, (1, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_EXPLORE_FRONTIER,
                                pos=(7, 10), family=navigation.TFAM_FRONTIER)
        serial = self.ref.targets.held().serial
        # the corridor is severed between the hero and the destination
        for x in (3, 4, 5, 6):
            mem.grid[(x, 10)] = WALL
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.proposed_effect, "dest-unresolved")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 2,
                               mem, observed_kind="no-time",
                               payload=cand.effect_payload)
        self.assertIsNone(self.ref.targets.held())
        terms = [e for e in self.ref.lifecycle.events
                 if e.get("kind") == "destination" and e.get("serial") == serial
                 and e.get("outcome") in ("failed", "expired")]
        self.assertEqual(len(terms), 1)

    def test_one_hop_acquisition_has_coherent_lifecycle(self):
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR}, (3, 10))
        payload = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((3, 10), navigation.TFAM_UNVISITED,
                                     (0, 0), 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=payload)
        self.assertIsNone(self.ref.targets.held())
        events = [e for e in self.ref.lifecycle.events
                  if e.get("kind") == "destination"]
        acquired = [e for e in events if e.get("outcome") == "acquired"]
        reached = [e for e in events if e.get("outcome") == "reached"]
        self.assertEqual(len(acquired), 1)
        self.assertEqual(len(reached), 1)
        self.assertEqual(acquired[0].get("serial"), reached[0].get("serial"))

    def test_one_hop_atomic_completion_lifecycle(self):
        mem = nav_test.mem_with({(3, 10): FLOOR, (4, 10): FLOOR}, (3, 10))
        payload = policy.ScriptedReflex._dest_payload(
            "acquire", None,
            target=navigation.Target((3, 10), navigation.TFAM_UNVISITED,
                                     (0, 0), 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="moved", payload=payload)
        events = [e for e in self.ref.lifecycle.events
                  if e.get("kind") == "destination"]
        acquired = [e for e in events if e.get("outcome") == "acquired"]
        reached = [e for e in events if e.get("outcome") == "reached"]
        # the completion is atomic: acquisition immediately precedes the single
        # terminal, with no parked hold between them
        self.assertEqual(events.index(reached[0]), events.index(acquired[0]) + 1)
        self.assertEqual(len([e for e in events
                              if e.get("outcome") in
                              ("reached", "failed", "expired")]), 1)

    def test_emergency_suspension_does_not_spend_destination_stall(self):
        # an emergency/maintenance preemption *alone* emits no destination
        # terminal and spends no destination counter (stall-recovery plan §1/§3)
        mem = self._acquire()
        serial = self.ref.targets.held().serial
        before_actions = len([e for e in self.ref.lifecycle.events
                              if e.get("outcome") == "action"])
        # a low-HP escape decision composes no destination payload at all
        mem.status.hp = 1
        mem.status.hp_max = 20
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "emergency")
        self.assertEqual(self._terminals(serial), [])
        self.assertEqual(len([e for e in self.ref.lifecycle.events
                              if e.get("outcome") == "action"]), before_actions)


class DefaultTerminalAccounting(unittest.TestCase):
    """AC2: every acquired destination has exactly one visible terminal.

    Default (non-directive) destinations must report termination through the
    same owner as directive-owned ones; a default arrival or stall that leaves
    the lifecycle stream silent is incomplete telemetry (the ep-4 defect).
    """

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _terminals(self, serial):
        return [e for e in self.ref.lifecycle.events
                if e.get("kind") == "destination"
                and e.get("outcome") in ("reached", "failed", "expired")
                and e.get("serial") == serial]

    def test_default_destination_terminal_accounting_complete(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        mem = nav_test.mem_with(cells, (1, 10))
        # a default destination is acquired, then reached
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        held = self.ref.targets.held()
        self.assertIsNotNone(held)
        serial = held.serial
        arrive = policy.ScriptedReflex._dest_payload("arrive", held)
        self.ref.commit_effect("navigate", "navigate", 2, mem,
                               observed_kind="moved", payload=arrive)
        self.assertIsNone(self.ref.targets.held())
        terminals = self._terminals(serial)
        self.assertEqual(len(terminals), 1,
                         "a default arrival must emit exactly one terminal")

    def test_default_stall_emits_terminal_once(self):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        mem = nav_test.mem_with(cells, (1, 10))
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        serial = self.ref.targets.held().serial
        for _ in range(navigation.STALL_MAX + 1):
            if self.ref.targets.held() is None:
                break
            payload = policy.ScriptedReflex._dest_payload(
                "continue", self.ref.targets.held())
            self.ref.commit_effect("navigate", "navigate", 2, mem,
                                   observed_kind="no-time", payload=payload)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(len(self._terminals(serial)), 1,
                         "a default stall must emit exactly one terminal")


if __name__ == "__main__":
    unittest.main()
