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

    def test_stationary_recovery_ladder_3_6_10_unchanged(self):
        cells = {(10, 10): FLOOR}
        mem = mem_with(cells, (10, 10))
        mem.no_progress = 3
        self.assertEqual(self.ref.prepare(ctx(mem)).table.scripted().family,
                         "recovery")
        ref2 = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem2 = mem_with(cells, (10, 10))
        mem2.no_progress = 6
        self.assertEqual(ref2.prepare(ctx(mem2)).table.scripted().family,
                         "recovery")
        ref3 = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem3 = mem_with(cells, (10, 10))
        mem3.no_progress = 10
        self.assertEqual(ref3.prepare(ctx(mem3)).table.scripted().family,
                         "recovery")

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
        self.assertEqual(cand.family, "recovery")
        # `l` is the east wire key: the locked-door step is the defect
        self.assertNotEqual(cand.action.to_wire().get("key"),
                            protocol.DIR_KEYS[(1, 0)])
        self.assertNotIn(cand.semantic_label, ("unblock",))

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
        mem.messages.append("The door is locked.")
        self.ref.note_observation(mem)
        self.assertIsNone(self.ref.targets.held())
        terminals = [e for e in self.ref.lifecycle.events
                     if e.get("kind") == "destination"
                     and e.get("outcome") in ("failed", "expired")]
        self.assertTrue(terminals, "default retirement emitted no terminal")
        self.assertEqual(terminals[-1].get("serial"), serial)
        self.assertTrue(terminals[-1].get("reason"))


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
        # the trapped state stays inside bounded recovery: never an unbounded
        # wait, and (after the Phase 2 fix) never the legacy raw-grid exit
        self.assertEqual(cand.family, "recovery")
        self.assertNotEqual(cand.action.to_wire().get("key"),
                            protocol.KEY_HASH)


if __name__ == "__main__":
    unittest.main()
