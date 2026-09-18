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

from tools.agent import policy, protocol, recovery, state  # noqa: E402
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
        # infinite exhaustion fallback, so the reflex requests a bounded quit
        mem = mem_with({(10, 10): FLOOR}, (10, 10),
                       messages=["You already found a monster."])
        mem.no_progress = 3
        mem.status.hunger = "Hungry"
        self.ref.last_eat_tick = 0     # the eat intent is already on cooldown
        res = self.ref.decide(ctx(mem, tick=0))
        self.assertEqual(res.action, {"key": protocol.KEY_HASH})
        self.assertTrue(self.ref.quitting)

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


if __name__ == "__main__":
    unittest.main()
