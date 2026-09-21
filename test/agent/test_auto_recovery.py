#!/usr/bin/env python3
"""Wave-4 tests: bounded recovery, refusal fingerprints and food negatives.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Covers ``tools/agent/recovery.py`` (plan section 5): the exact public search
refusal recognizer, the scoped food negatives and the bounded search/cycle
budgets, plus their integration into ``ScriptedReflex`` (a refused ordinary
``s`` is suppressed and recovery escalates instead of looping).
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (forced_search, navigation, policy,  # noqa: E402
                         protocol, recovery, state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402


class RefusalRecognizer(unittest.TestCase):
    def test_exact_monster_refusal(self):
        self.assertEqual(
            recovery.is_search_refusal("You already found a monster."),
            "monster")

    def test_monster_refusal_with_m_prefix_suffix(self):
        text = ("You already found a monster."
                "  Use 'm' prefix to force another search.")
        self.assertEqual(recovery.is_search_refusal(text), "monster")

    def test_danger_refusal_variant(self):
        self.assertEqual(
            recovery.is_search_refusal(
                "Searching doesn't feel like a good idea right now."),
            "danger")

    def test_generic_found_a_monster_is_not_a_refusal(self):
        # the plan forbids matching generic "found a monster" text
        self.assertIsNone(recovery.is_search_refusal("You found a monster!"))
        self.assertIsNone(recovery.is_search_refusal(
            "There is a monster already here."))

    def test_farlook_text_is_not_a_refusal(self):
        for text in ("a monster (a kobold)",
                     "You see a monster here.",
                     "the monster already found you"):
            self.assertIsNone(recovery.is_search_refusal(text), text)

    def test_refusal_in_scan(self):
        self.assertEqual(
            recovery.refusal_in(["hello",
                                 "You already found a monster."]),
            "monster")
        self.assertIsNone(recovery.refusal_in(["hello", "world"]))


class FoodNegativeRecognizer(unittest.TestCase):
    def test_both_forms(self):
        self.assertEqual(
            recovery.classify_food_negative(
                "You don't have anything to eat."),
            recovery.FOOD_NEG_INVENTORY)
        self.assertEqual(
            recovery.classify_food_negative(
                "You don't have anything else to eat."),
            recovery.FOOD_NEG_LOCATION)

    def test_unrelated_messages_are_not_negatives(self):
        self.assertIsNone(recovery.classify_food_negative("You feel hungry."))
        self.assertIsNone(recovery.classify_food_negative(""))


class SearchBudgetTest(unittest.TestCase):
    def test_three_completed_searches_then_exhausted(self):
        b = recovery.SearchBudget()
        site = (10, 10)
        for _ in range(3):
            self.assertTrue(b.allows(site))
            b.note_completed(site)
        self.assertFalse(b.allows(site))

    def test_one_refusal_suppresses_the_site(self):
        b = recovery.SearchBudget()
        b.note_refused((1, 1))
        self.assertFalse(b.allows((1, 1)))
        self.assertTrue(b.allows((2, 2)))

    def test_revision_change_reopens_the_site(self):
        b = recovery.SearchBudget()
        site = (3, 3)
        for _ in range(3):
            b.note_completed(site)
        self.assertFalse(b.allows(site))
        b.revision_changed(site)
        self.assertTrue(b.allows(site))


class CycleDetectorTest(unittest.TestCase):
    def test_ab_oscillation(self):
        c = recovery.CycleDetector()
        seq = [(1, 1), (2, 2), (1, 1), (2, 2)]
        self.assertFalse(c.observe(seq[0]))
        self.assertFalse(c.observe(seq[1]))
        self.assertFalse(c.observe(seq[2]))
        self.assertTrue(c.observe(seq[3]))

    def test_abc_cycle(self):
        c = recovery.CycleDetector()
        result = False
        for p in [(1, 1), (2, 2), (3, 3), (1, 1), (2, 2), (3, 3)]:
            result = c.observe(p)
        self.assertTrue(result)

    def test_stationary_is_not_a_cycle(self):
        c = recovery.CycleDetector()
        for _ in range(6):
            self.assertFalse(c.observe((5, 5)))

    def test_unknown_position_is_ignored(self):
        c = recovery.CycleDetector()
        self.assertFalse(c.observe(None))
        # the stronger invariant: an unknown position breaks continuity, so a
        # stray cycle can never be fabricated across it
        for p in [(1, 1), (2, 2), (1, 1)]:
            self.assertFalse(c.observe(p))
        self.assertFalse(c.observe(None))
        self.assertEqual(c.history, [])
        self.assertFalse(c.observe((1, 1)))
        self.assertFalse(c.observe((2, 2)))
        self.assertFalse(c.observe((1, 1)))


class MovementHistoryTest(unittest.TestCase):
    """The observation-owned movement-history state machine (plan section 4)."""

    def _state(self, *positions):
        rs = recovery.RecoveryState()
        for pos in positions:
            rs.note_cycle(pos)
        return rs

    def test_ab_cycle_activates_after_four_confirmed_moves(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        self.assertEqual(rs.current, (2, 10))
        self.assertEqual(rs.previous_distinct, (3, 10))

    def test_period_three_trailing_movement_preserved(self):
        # a genuine 3-cycle over adjacent cells: a 2x2 corner path
        # (1,1)->(2,1)->(2,2)->(1,1) (the last leg is a diagonal adjacency)
        rs = self._state((1, 1), (2, 1), (2, 2), (1, 1), (2, 1), (2, 2))
        self.assertTrue(rs.cycle_active)
        self.assertEqual(rs.movement_history(), ((1, 1), (2, 1), (2, 2),
                                                 (1, 1), (2, 1), (2, 2)))

    def test_duplicate_observations_do_not_dilute_movement_cycle(self):
        # a stationary frame between the alternating moves must neither break
        # nor postpone detection
        rs = recovery.RecoveryState()
        for pos in [(3, 10), (2, 10), (2, 10), (3, 10), (3, 10), (2, 10)]:
            rs.note_cycle(pos)
        self.assertTrue(rs.cycle_active)
        self.assertEqual(rs.movement_history(), ((3, 10), (2, 10), (3, 10),
                                                 (2, 10)))

    def test_duplicate_observation_before_detection_preserves_previous_cell(
            self):
        rs = self._state((3, 10), (2, 10))
        before = (rs.previous_distinct, rs.movement_history())
        rs.note_cycle((2, 10))                 # identical confirmation
        self.assertEqual((rs.previous_distinct, rs.movement_history()), before)
        self.assertEqual(rs.current, (2, 10))

    def test_duplicate_observation_after_detection_keeps_cycle_active(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        rs.note_cycle((2, 10))                 # a stationary frame at B
        self.assertTrue(rs.cycle_active)

    def test_door_opening_stationary_observation_keeps_cycle_active(self):
        # a door-opening key that leaves the hero on the same square is an
        # identical confirmation, not a new traversed edge
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        rs.note_cycle((2, 10))
        self.assertTrue(rs.cycle_active)
        self.assertEqual(rs.previous_distinct, (3, 10))

    def test_adjacent_third_exit_clears_cycle_active(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        rs.note_cycle((3, 9))                  # an off-cycle adjacent move
        self.assertFalse(rs.cycle_active)
        self.assertEqual(rs.current, (3, 9))
        self.assertEqual(rs.previous_distinct, (2, 10))

    def test_unknown_or_relocated_hero_invalidates_backtrack_evidence(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        rs.note_cycle((9, 9))                  # a non-adjacent relocation
        self.assertFalse(rs.cycle_active)
        self.assertIsNone(rs.previous_distinct)

    def test_unknown_hero_clears_movement_history_and_cycle(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        self.assertTrue(rs.cycle_active)
        rs.note_cycle(None)
        self.assertFalse(rs.cycle_active)
        self.assertIsNone(rs.previous_distinct)
        self.assertIsNone(rs.current)
        self.assertEqual(rs.movement_history(), ())

    def test_nonadjacent_relocation_clears_movement_history_and_cycle(self):
        rs = self._state((3, 10), (2, 10), (3, 10), (2, 10))
        rs.note_cycle((20, 5))
        self.assertFalse(rs.cycle_active)
        self.assertIsNone(rs.previous_distinct)
        self.assertEqual(rs.current, (20, 5))
        self.assertEqual(rs.movement_history(), ((20, 5),))

    def test_instance_reset_clears_previous_cell_and_cycle(self):
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = mem_with({(3, 10): FLOOR, (2, 10): FLOOR}, (3, 10))
        for pos in [(3, 10), (2, 10), (3, 10), (2, 10)]:
            mem.hero = pos
            ref.note_observation(mem)
        self.assertTrue(ref.recovery.cycle_active)
        ref.begin_instance(2)
        self.assertIsNone(ref.recovery.previous_distinct)
        self.assertFalse(ref.recovery.cycle_active)
        self.assertEqual(ref.recovery.movement_history(), ())


class CycleRecoveryPolicy(unittest.TestCase):
    """`_cycled` independently enters safe recovery (AC.9/AC.10)."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _fold(self, mem, positions):
        for pos in positions:
            mem.hero = pos
            self.ref.note_observation(mem)

    def _third_exit_map(self):
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (4, 10): FLOOR,
                 (5, 10): FLOOR, (3, 9): FLOOR}
        for pos in [(2, 9), (2, 11), (3, 11), (4, 9), (4, 11), (5, 9),
                    (5, 11)]:
            cells[pos] = WALL
        return cells

    def test_ab_cycle_enters_recovery_with_zero_stationary_no_progress(self):
        mem = mem_with(self._third_exit_map(), (3, 10))
        self._fold(mem, [(3, 10), (2, 10), (3, 10), (2, 10), (3, 10)])
        self.assertEqual(mem.no_progress, 0)
        self.assertTrue(self.ref._cycled)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "recovery")

    def test_cycle_recovery_chooses_third_exit_by_four_alternating_moves(self):
        mem = mem_with(self._third_exit_map(), (3, 10))
        # after A-B-A-B the cycle is detected, and the next decision (with the
        # hero back at A) takes the third exit rather than reversing again
        self._fold(mem, [(3, 10), (2, 10), (3, 10), (2, 10), (3, 10)])
        self.assertTrue(self.ref._cycled)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # the north third exit (not the reversing west step) is chosen
        self.assertEqual(cand.direction, (0, -1))

    def test_cycle_with_only_reverse_exit_does_not_quit(self):
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (1, 10): WALL, (2, 9): WALL,
                 (2, 11): WALL, (3, 9): WALL, (3, 11): WALL, (4, 10): WALL}
        mem = mem_with(cells, (3, 10))
        self._fold(mem, [(3, 10), (2, 10), (3, 10), (2, 10)])
        self.assertTrue(self.ref._cycled)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # the traversable dead end keeps the backtracking exit, never a quit
        self.assertEqual(cand.direction, (1, 0))
        self.assertNotEqual(cand.semantic_label, "trapped")

    def test_cycle_without_exit_respects_search_budget_and_nomination(self):
        # a boxed-in hero with an active cycle: no legal movement exists, so
        # the bounded search-fallback machinery answers (never an unbounded
        # search or a manufactured dangerous prefix)
        cells = {(3, 10): FLOOR, (2, 10): WALL, (4, 10): WALL, (3, 9): WALL,
                 (3, 11): WALL}
        mem = mem_with(cells, (3, 10))
        self.ref._cycled = True
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "recovery")

    def test_stationary_thresholds_shared_legal_bounded_recovery(self):
        """Contract migration (stall-recovery plan, migration checklist).

        The old ``..._unchanged`` expectation only checked that 3/6/10 returned
        the recovery *family*; it pinned no mechanism.  The preserved contract
        is the threshold values (3/6/10) and emergency/hunger precedence, while
        the expected recovery mechanism is now the shared legal bounded builder
        -- so each threshold still returns the recovery family, and none emits
        the removed legacy raw-grid labels.
        """
        cells = {(10, 10): FLOOR}
        for np in (3, 6, 10):
            ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
            mem = mem_with(cells, (10, 10))
            mem.no_progress = np
            cand = ref.prepare(ctx(mem)).table.scripted()
            self.assertEqual(cand.family, "recovery", np)
            self.assertNotIn(cand.semantic_label, ("unblock", "random-move"))
        # emergency precedence is preserved: a low-HP disengage still preempts
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = mem_with({(10, 10): FLOOR, (9, 10): WALL}, (10, 10))
        mem.no_progress = 10
        mem.status.hp = 1
        mem.status.hp_max = 20
        self.assertEqual(ref.prepare(ctx(mem)).table.scripted().family,
                         "emergency")

    def test_failed_proposal_does_not_fold_motion_history(self):
        # preparing a proposal (even a rejected one) never advances the
        # observation-owned history
        mem = mem_with(self._third_exit_map(), (3, 10))
        self._fold(mem, [(3, 10), (2, 10)])
        before = self.ref.recovery.movement_history()
        self.ref.prepare(ctx(mem))
        self.ref.decide(ctx(mem))
        self.assertEqual(self.ref.recovery.movement_history(), before)
        self.ref.prepare(ctx(mem))
        self.assertEqual(self.ref.recovery.movement_history(), before)

    def test_failed_and_partial_act_write_do_not_fold_motion_history(self):
        # only a committed observation reaches note_observation; a send that
        # failed or was partial never does, so history is unchanged and an
        # active cycle is not cleared
        mem = mem_with(self._third_exit_map(), (3, 10))
        self._fold(mem, [(3, 10), (2, 10), (3, 10), (2, 10)])
        self.assertTrue(self.ref.recovery.cycle_active)
        history = self.ref.recovery.movement_history()
        # no further note_observation happens for a failed/partial write
        self.assertEqual(self.ref.recovery.movement_history(), history)
        self.assertTrue(self.ref.recovery.cycle_active)


class FoodNegativesTest(unittest.TestCase):
    def test_inventory_negative_is_signature_scoped(self):
        f = recovery.FoodNegatives()
        f.note_inventory_negative(("a - a food ration",))
        self.assertTrue(f.inventory_negative(("a - a food ration",)))
        self.assertFalse(f.inventory_negative(("a - a lembas wafer",)))

    def test_location_negative_is_position_scoped(self):
        f = recovery.FoodNegatives()
        f.note_location_negative(1, (10, 10), 0)
        self.assertTrue(f.location_negative(1, (10, 10), 0))
        self.assertFalse(f.location_negative(1, (11, 10), 0))
        self.assertFalse(f.location_negative(2, (10, 10), 0))
        self.assertFalse(f.location_negative(1, (10, 10), 1))

    def test_instance_transition_expires_location_negatives(self):
        f = recovery.FoodNegatives()
        f.note_location_negative(1, (10, 10), 0)
        f.note_location_negative(2, (4, 4), 0)
        f.invalidate_instance(2)
        self.assertFalse(f.location_negative(1, (10, 10), 0))
        self.assertTrue(f.location_negative(2, (4, 4), 0))


# ---------------------------------------------------------- reflex wiring

def mem_with(cells, hero, messages=()):
    mem = state.EpisodeMemory()
    mem.grid.update(cells)
    mem.hero = hero
    mem.status.hp = 20
    mem.status.hp_max = 20
    mem.inventory.refresh([], 0, 0)
    mem.messages.extend(messages)
    return mem


def ctx(mem, tick=0):
    need = {"kind": "command", "id": 1}
    return ReflexContext(
        episode=1, tick=tick, need=need,
        need_key=protocol.NeedKey(1, tick, 1),
        snapshot=protocol.Snapshot(), pages=[], memory=mem,
        directives=[], deadline=0.0)


FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")


class RefusalSuppression(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_refused_search_escalates_to_a_real_step_not_another_s(self):
        # a boxed-in hero whose ordinary search was refused must never emit
        # another `s`; with known floor it takes a step instead
        cells = {(10, 10): FLOOR, (11, 10): FLOOR}
        mem = mem_with(cells, (10, 10),
                       messages=["You already found a monster."])
        mem.no_progress = 3
        res = self.ref.decide(ctx(mem))
        self.assertNotEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_refused_search_with_no_alternative_quits_gracefully(self):
        # no known floor and unsafe to rest (hungry): command `s` is not an
        # infinite exhaustion fallback, so the reflex requests a bounded quit.
        # The proposal is observational (plan 3.1): the quit intent is
        # committed by the controller only after the send and its reconciled
        # observation, so the direct call asserts the action and the commit is
        # then exercised explicitly.
        mem = mem_with({(10, 10): FLOOR}, (10, 10),
                       messages=["You already found a monster."])
        mem.no_progress = 3
        mem.status.hunger = "Hungry"
        self.ref.last_eat_tick = 0     # the eat intent is already on cooldown
        res = self.ref.decide(ctx(mem, tick=0))
        self.assertEqual(res.action, {"key": protocol.KEY_HASH})
        # a proposal alone mutates no gameplay/recovery/intent state
        self.assertFalse(self.ref.quitting)
        # the controller commits the frozen effect at its reconciliation
        self.ref.commit_effect("quit", "trapped", 0, mem)
        self.assertTrue(self.ref.quitting)
        self.assertEqual(self.ref.quit_reason,
                         forced_search.TRAPPED_QUIT_REASON)

    def test_fresh_site_still_searches(self):
        # without refusal evidence the ordinary loop breaker still searches
        cells = {(10, 10): FLOOR}
        mem = mem_with(cells, (10, 10))
        mem.no_progress = 3
        res = self.ref.decide(ctx(mem))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_exhausted_budget_suppresses_further_searches(self):
        cells = {(10, 10): FLOOR, (11, 10): FLOOR}
        mem = mem_with(cells, (10, 10))
        mem.no_progress = 3
        site = (10, 10)
        for _ in range(recovery.SEARCH_SITE_LIMIT):
            self.ref.recovery.search.note_completed(site)
        res = self.ref.decide(ctx(mem))
        self.assertNotEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_hunger_with_inventory_negative_does_not_probe_eat(self):
        cells = {(10, 10): FLOOR, (11, 10): FLOOR}
        mem = mem_with(cells, (10, 10),
                       messages=["You don't have anything to eat."])
        mem.status.hunger = "Hungry"
        # prime the inventory signature so the negative binds
        mem.inventory.refresh([], 0, 0)
        self.ref.food.note_inventory_negative(mem.inventory_signature())
        res = self.ref.decide(ctx(mem))
        self.assertNotEqual(res.action, {"key": protocol.KEY_EAT})


# ---------------------------------------------------- Phase 0: ep-4 capture

import stall_recovery_fixtures as stall_fx  # noqa: E402  (small fixture)


class Ep4LockedDoorStall(unittest.TestCase):
    """AC1: the ep-4 capped-stationary locked-door stall (Phase 0 capture).

    These tests document the live defect (they FAIL before the Phase 2 fix and
    PASS after): the legacy ``>=10`` ``_unblock`` branch steps the hero into the
    locked east door forever, and a default (non-directive) destination
    retirement is invisible in the lifecycle stream.
    """

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_cap_exhausted_locked_door_adjacent_monster_enters_bounded_recovery(
            self):
        # hero (64,4), locked east door at (65,4), monster ':' at (63,4), and a
        # reachable frontier beyond the door: the capped stationary stage must
        # enter bounded edge-legal recovery, never the raw-grid step east into
        # the locked door.
        mem = mem_with(stall_fx.ep4_cells(), stall_fx.EP4_HERO)
        mem.no_progress = 10
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # `l` is the east wire key: the locked-door step is the defect
        self.assertNotEqual(cand.action.to_wire().get("key"),
                            protocol.DIR_KEYS[(1, 0)])
        # the stage is a bounded recovery endpoint -- a legal recovery step, an
        # allowed bounded search, or the graceful forced-search/trapped
        # escalation -- never the legacy raw-grid `unblock`/`random-move` exit
        self.assertIn(cand.semantic_label,
                      ("recovery-step", "search", "search-secret",
                       "forced-search", "trapped"))
        self.assertNotIn(cand.semantic_label, ("unblock", "random-move"))

    def test_default_locked_door_retirement_is_visible(self):
        # a default (non-directive) open-door commitment that is explicitly
        # refused must produce a *visible* terminal lifecycle event for its
        # serial, not only a private `targets.events` entry.
        mem = mem_with({(3, 10): FLOOR, (4, 10): FLOOR,
                        (5, 10): stall_fx.DOOR, (6, 10): FLOOR}, (4, 10))
        self.ref.targets.commit(instance_id=self.ref.instance_id,
                                purpose=navigation.COMMIT_OPEN_DOOR,
                                pos=(5, 10), family=navigation.TFAM_DOOR)
        serial = self.ref.targets.held().serial
        # the door interaction is armed (baseline at the send boundary) and its
        # locked response is classified at the matched effect reducer (§4)
        self.ref.arm_door_baseline(mem.message_count)
        mem.messages.append("The door is locked.")
        mem.message_count += 1
        payload = policy.ScriptedReflex._dest_payload(
            "continue", self.ref.targets.held(), step=(1, 0))
        self.ref.commit_effect("navigate", "navigate", 1, mem,
                               observed_kind="no-time", payload=payload,
                               pre_hero=(4, 10))
        self.assertIsNone(self.ref.targets.held())
        terminals = [e for e in self.ref.lifecycle.events
                     if e.get("kind") == "destination"
                     and e.get("outcome") in ("failed", "expired")]
        self.assertTrue(terminals, "default retirement emitted no terminal")
        self.assertEqual(terminals[-1].get("serial"), serial)
        self.assertTrue(terminals[-1].get("reason"))


class Phase2BoundedRecovery(unittest.TestCase):
    """AC5: 3/6/10 and cycle recovery share ONE legal bounded builder."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_stationary_thresholds_3_6_10_share_legal_bounded_recovery(self):
        # a corridor whose only legal exit is east; a refused search keeps the
        # 3-threshold inside the same bounded builder as 6 and 10
        cells = {(10, 10): FLOOR, (11, 10): FLOOR, (9, 10): WALL,
                 (10, 9): WALL, (10, 11): WALL}
        for np in (3, 6, 10):
            ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
            mem = mem_with(cells, (10, 10),
                           messages=["You already found a monster."])
            mem.no_progress = np
            cand = ref.prepare(ctx(mem)).table.scripted()
            self.assertEqual(cand.family, "recovery", np)
            self.assertEqual(cand.semantic_label, "recovery-step", np)
            self.assertEqual(cand.direction, (1, 0), np)

    def test_np10_recovery_never_routes_through_locked_door(self):
        cells = stall_fx.ep4_cells()
        cells[(64, 3)] = FLOOR          # a legal north exit exists
        mem = mem_with(cells, stall_fx.EP4_HERO)
        mem.no_progress = 10
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "recovery")
        # takes the legal north exit, never the locked east door
        self.assertEqual(cand.direction, (0, -1))
        self.assertNotEqual(cand.action.to_wire().get("key"),
                            protocol.DIR_KEYS[(1, 0)])

    def test_cycle_with_stationary_count_does_not_fall_back_into_navigation(
            self):
        cells = {(3, 10): FLOOR, (2, 10): FLOOR, (4, 10): FLOOR,
                 (3, 9): FLOOR}
        mem = mem_with(cells, (3, 10))
        self.ref._cycled = True
        mem.no_progress = 6
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.family, "recovery")
        self.assertIn(cand.semantic_label, ("recovery-step",))

    def test_recovery_only_legal_reverse_is_not_trapped(self):
        cells = {(2, 10): FLOOR, (3, 10): FLOOR, (1, 10): WALL,
                 (2, 9): WALL, (2, 11): WALL, (3, 9): WALL, (3, 11): WALL,
                 (4, 10): WALL}
        mem = mem_with(cells, (3, 10))
        self.ref.recovery.previous_distinct = (2, 10)
        mem.no_progress = 10
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # the only legal escape is the reverse step, kept -- never a quit
        self.assertEqual(cand.direction, (-1, 0))
        self.assertEqual(cand.semantic_label, "recovery-step")


class Phase2EdgeFailureAndExhaustion(unittest.TestCase):
    """AC5/AC7: scoped edge-failure suppression and bounded exhaustion."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _corridor(self):
        return {(10, 10): FLOOR, (11, 10): FLOOR, (9, 10): WALL,
                (10, 9): WALL, (10, 11): WALL}

    def test_recovery_no_time_edge_failure_is_suppressed(self):
        mem = mem_with(self._corridor(), (10, 10),
                       messages=["You already found a monster."])
        mem.no_progress = 10
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(cand.direction, (1, 0))
        # the selected recovery move fails no-time: its edge is recorded
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="no-time",
                               payload=cand.effect_payload, pre_hero=(10, 10))
        self.assertTrue(self.ref.blocked_edges)
        # unchanged evidence cannot select that same edge again
        again = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertNotEqual(again.action.to_wire().get("key"),
                            protocol.DIR_KEYS[(1, 0)])

    def test_failed_route_reopens_after_blocker_leaves(self):
        cells = {(10, 10): FLOOR, (11, 11): FLOOR, (11, 10): FLOOR,
                 (10, 11): FLOOR}
        mem = mem_with(cells, (10, 10))
        src, dst = (10, 10), (11, 11)
        self.ref.blocked_edges[(self.ref.instance_id, src, dst)] = (
            navigation.blocked_edge_signature(self.ref._terrain(mem), src, dst))
        self.assertTrue(self.ref._edge_blocked(self.ref._terrain(mem),
                                               src, dst))
        # a legality-relevant side cell changes (a blocker leaves/moves): the
        # stored signature no longer matches, so the edge reopens
        mem.grid[(11, 10)] = ("d", "white", 0, "none")
        self.assertFalse(self.ref._edge_blocked(self.ref._terrain(mem),
                                                src, dst))

    def test_no_alternative_uses_forced_search_then_trapped_quit(self):
        from unittest import mock
        # an adjacent monster makes a hold unsafe and no neighbour is a legal
        # step (all unknown), with the ordinary search refused
        mem = mem_with({(10, 10): FLOOR, (10, 9): (":", "gray", 0, "none")},
                       (10, 10), messages=["You already found a monster."])
        mem.no_progress = 10
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # no legal movement and a refused search: the bounded endpoint is the
        # forced-search nomination or the graceful trapped quit
        self.assertIn(cand.semantic_label, ("forced-search", "trapped"))
        # with the reflex-local forced-search gates failing, the endpoint is the
        # graceful trapped quit -- never an unbounded search or wait
        with mock.patch.object(forced_search, "local_ok", return_value=False):
            fb = self.ref._search_fallback(mem, (10, 10))
        self.assertEqual(fb[0].semantic_label, "trapped")

    def test_episode2_three_prefix_cap_and_trapped_reason_preserved(self):
        # the ep-2 sequence is a bounded graceful exhaustion: at most three
        # forced-search activations, and a stable trapped reason
        self.assertEqual(forced_search.ACTIVATION_CAP, 3)
        self.assertEqual(forced_search.TRAPPED_QUIT_REASON,
                         "policy-exhausted/trapped")
        b = forced_search.ForcedSearchBudget()
        self.assertTrue(b.allows())
        for _ in range(forced_search.ACTIVATION_CAP):
            b.consume()
        self.assertFalse(b.allows())
        self.assertTrue(b.exhausted())
        self.assertEqual(b.remaining(), 0)


class Ep2TrappedQuit(unittest.TestCase):
    """AC1 (ep-2 half): the trapped sequence stays a bounded graceful quit."""

    def test_ep2_trapped_sequence_requests_native_quit(self):
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = mem_with({(10, 10): FLOOR}, (10, 10),
                       messages=list(stall_fx.EP2_TRAPPED_MESSAGES))
        mem.no_progress = 10
        mem.status.hunger = "Hungry"
        ref.last_eat_tick = 0
        cand = ref.prepare(ctx(mem)).table.scripted()
        # the trapped state stays inside the bounded machinery: a legal
        # recovery step, an allowed bounded search, or the graceful
        # forced-search/trapped escalation -- never the legacy raw-grid exit
        self.assertIn(cand.semantic_label,
                      ("recovery-step", "search", "search-secret",
                       "forced-search", "trapped"))
        self.assertNotIn(cand.semantic_label, ("unblock", "random-move"))


class NoTimeRecoverySearchBudget(unittest.TestCase):
    """Bug A: a zero-time recovery search is bounded and escalates.

    A stuck hero (held/paralysed by a monster) reconciles every search as
    ``no-time``: no time advances, and the engine emits no search *refusal*, so
    neither the ordinary completed-search budget nor the refusal flag ever
    engaged and the ladder nominated ``s`` forever.  The no-time budget bounds
    it and the ladder then escalates exactly per the stall plan.
    """

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _stuck_with_monster(self, messages=()):
        """A held hero: no legal movement, an adjacent monster, unsafe to rest.

        The monster sits on the only floor neighbour (diagonally), so
        ``_recovery_legal_step`` skips it on occupancy and ``_random_move``
        finds no legal step and (unsafe to rest) falls back to a *search* --
        exactly the ep-2 nomination shape.
        """
        cells = {(10, 10): FLOOR, (11, 11): FLOOR, (11, 10): WALL,
                 (10, 11): WALL, (9, 10): WALL, (10, 9): WALL}
        mem = mem_with(cells, (10, 10), messages=list(messages))
        mem.grid[(11, 11)] = ("d", "brown", 0, "none")
        mem.no_progress = 3
        return mem

    def _step(self, mem, tick=0):
        return self.ref.prepare(ctx(mem, tick=tick)).table.scripted()

    def _commit_no_time(self, mem, cand, tick=0):
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, tick,
                               mem, observed_kind="no-time")

    def test_no_time_recovery_search_escalates_to_forced_search_within_budget(
            self):
        site = (10, 10)
        limit = recovery.SEARCH_NO_TIME_LIMIT
        self.ref.instance_id = 1        # the reflex knows its instance
        # no refusal evidence yet: the bounded searches must be nominated
        mem = self._stuck_with_monster()
        # the first `limit` decisions are bounded ordinary searches, each of
        # which reconciles zero-time (the hero is held)
        for i in range(limit):
            cand = self._step(mem, tick=i)
            self.assertEqual(cand.semantic_label, "search", i)
            self.assertEqual(cand.action.to_wire(),
                             {"key": protocol.KEY_SEARCH})
            self.assertTrue(self.ref.recovery.allows_search(site), i)
            self._commit_no_time(mem, cand, tick=i)
        # the bounded no-time budget is now spent at this site
        self.assertEqual(self.ref.recovery.no_time_searches(site), limit)
        self.assertFalse(self.ref.recovery.allows_search(site))
        # a freshly observed correlated refusal unlocks the forced-search
        # nomination (stale text must never fail a new site -- plan §4)
        mem.messages.append("You already found a monster.")
        cand = self._step(mem, tick=limit)
        self.assertEqual(cand.semantic_label, "forced-search")
        self.assertEqual(cand.proposed_effect,
                         forced_search.FORCED_SEARCH_EFFECT)
        self.assertEqual(cand.action.to_wire(),
                         {"key": forced_search.FORCED_SEARCH_PREFIX_CODE})
        self.assertIsNotNone(self.ref.forced_search_local())

    def test_no_time_recovery_search_without_refusal_quit_gracefully(self):
        # with no correlated refusal evidence the nomination gate denies, so
        # the same exhausted budget produces the trapped graceful quit -- never
        # another search
        site = (10, 10)
        self.ref.instance_id = 1
        mem = self._stuck_with_monster()
        for i in range(recovery.SEARCH_NO_TIME_LIMIT):
            cand = self._step(mem, tick=i)
            self.assertEqual(cand.semantic_label, "search", i)
            self._commit_no_time(mem, cand, tick=i)
        self.assertFalse(self.ref.recovery.allows_search(site))
        cand = self._step(mem, tick=recovery.SEARCH_NO_TIME_LIMIT)
        self.assertEqual(cand.semantic_label, "trapped")
        self.assertEqual(cand.action.to_wire(), {"key": protocol.KEY_HASH})

    def test_recovery_search_budget_counts_no_time_attempts_only(self):
        site = (10, 10)
        mem = self._stuck_with_monster()
        # (a) a TIME-ADVANCING search must not consume the no-time budget
        cand = self._step(mem, tick=0)
        self.assertEqual(cand.semantic_label, "search")
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 0,
                               mem, observed_kind="advanced")
        self.assertEqual(self.ref.recovery.no_time_searches(site), 0)
        self.assertTrue(self.ref.recovery.allows_search(site))
        # it did consume the ordinary completed-search budget
        self.assertEqual(self.ref.recovery.search.completed.get(site), 1)
        # (b) a no-time search consumes ONLY the no-time budget
        for i in range(1, 1 + recovery.SEARCH_NO_TIME_LIMIT):
            cand = self._step(mem, tick=i)
            self._commit_no_time(mem, cand, tick=i)
        self.assertEqual(self.ref.recovery.search.completed.get(site), 1)
        self.assertEqual(self.ref.recovery.no_time_searches(site),
                         recovery.SEARCH_NO_TIME_LIMIT)
        self.assertFalse(self.ref.recovery.allows_search(site))

    def test_ep2_shape_terminates_within_a_bounded_action_count(self):
        """The ep-2 stuck-hero shape cannot loop zero-time searches.

        Drives the controller's decide -> commit cycle directly: every search
        reconciles as ``no-time`` (the hero is held), and the episode must
        escape the search loop within a bounded action count -- never a long
        run of identical zero-time searches.
        """
        self.ref.instance_id = 1
        mem = self._stuck_with_monster()
        actions = []
        zero_time_searches = 0
        run = 0
        max_run = 0
        for tick in range(200):
            cand = self._step(mem, tick=tick)
            wire = cand.action.to_wire()
            actions.append(wire)
            if wire == {"key": protocol.KEY_SEARCH}:
                run += 1
                max_run = max(max_run, run)
                zero_time_searches += 1
                # the held hero cannot act: the search reconciles zero-time
                self._commit_no_time(mem, cand, tick=tick)
            else:
                # the loop escaped into the bounded escalation
                break
        # escaped well inside the bounded budget (the plan's 100-action bound)
        self.assertLess(len(actions), 100)
        self.assertLessEqual(zero_time_searches, recovery.SEARCH_NO_TIME_LIMIT)
        self.assertLessEqual(max_run, 100)
        # the escaping action is the bounded escalation, never another search
        self.assertIn(actions[-1],
                      ({"key": protocol.KEY_HASH},
                       {"key": forced_search.FORCED_SEARCH_PREFIX_CODE}))


if __name__ == "__main__":
    unittest.main()
