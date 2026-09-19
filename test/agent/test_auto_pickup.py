#!/usr/bin/env python3
"""Pickup intent tests (destination-commitment plan, Phase 0 + Phase 3).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Phase 0 (this file, engine-free): the pickup-shape vocabulary, the
native-vs-manual disposition partition (AC17/AC18) and the pure pickup row
model.  Protocol-shape verification itself is the native probe
(``make -C test/agent native-pickup``) or the mandatory operator-gated manual
probe for a non-constructible shape; the row-model tests here never claim it.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pickup_shapes  # noqa: E402
from tools.agent import navigation, pickup, policy, presentation  # noqa: E402
from tools.agent.directives import DirectiveSet, DirectiveView  # noqa: E402
from tools.agent.providers import ProviderConfig  # noqa: E402

import test_auto_navigation as nav_test  # noqa: E402

FLOOR = nav_test.FLOOR


class PickupPolicyWiring(unittest.TestCase):
    """AC11/AC13: the policy pickup intent over a supported item site."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _mem(self, hero=(1, 10)):
        cells = {(x, 10): FLOOR for x in range(1, 8)}
        return nav_test.mem_with(cells, hero)

    def _labels(self, table):
        return [c.semantic_label for c in table.ordered_candidates]

    def _observe(self, mem, appearance="% food appearance"):
        return self.ref.floor.observe_item(self.ref.instance_id, mem.hero,
                                           appearance)

    def test_opportunistic_pickup_preserves_exploration_destination(self):
        mem = self._mem()
        self._observe(mem)
        table = self.ref.prepare(nav_test.ctx(mem)).table
        labels = self._labels(table)
        self.assertIn("navigate", labels)
        self.assertIn("pick-up", labels)
        # the committed-route continuation stays the scripted argmax
        self.assertEqual(table.scripted().semantic_label, "navigate")

    def test_retreat_suppresses_opportunistic_pickup(self):
        mem = self._mem()
        self._observe(mem)
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("flee_to_upstairs",)), 1)
        table = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table
        self.assertNotIn("pick-up", self._labels(table))

    def test_adjacent_ambiguous_item_does_not_redirect_default_route(self):
        mem = self._mem(hero=(3, 10))
        # an item appearance adjacent to (not under) the hero is context only
        mem.grid[(3, 9)] = ("%", "brown", 0, "none")
        table = self.ref.prepare(nav_test.ctx(mem)).table
        self.assertNotIn("pick-up", self._labels(table))
        self.assertEqual(table.scripted().semantic_label, "navigate")

    def test_hungry_exact_ration_promotes_pickup_above_route(self):
        mem = self._mem()
        self._observe(mem)
        self.ref.floor.bind_ration_name(self.ref.instance_id, mem.hero,
                                        "food ration", mem.hero)
        mem.status.hunger = "Hungry"
        # the scheduled-eat path is on cooldown, so navigation is reached and
        # the narrow urgent-food rule authorizes the reflex pickup
        self.ref.last_eat_tick = 1000
        table = self.ref.prepare(nav_test.ctx(mem)).table
        self.assertEqual(table.scripted().semantic_label, "pick-up")

    def test_declined_token_not_reoffered(self):
        mem = self._mem()
        ev = self._observe(mem)
        self.ref.floor.note_declined(ev)
        table = self.ref.prepare(nav_test.ctx(mem)).table
        self.assertNotIn("pick-up", self._labels(table))

    def test_bounded_attempts_suppress_pickup_offer(self):
        mem = self._mem()
        ev = self._observe(mem)
        self.ref.floor.note_initiation(ev)
        self.ref.floor.note_initiation(ev)
        table = self.ref.prepare(nav_test.ctx(mem)).table
        self.assertNotIn("pick-up", self._labels(table))

    def test_commit_pickup_counts_reconciled_initiation(self):
        mem = self._mem()
        ev = self._observe(mem)
        payload = ("pickup", "opportunistic", ev.instance, ev.pos[0],
                   ev.pos[1], ev.source_epoch)
        self.ref.commit_effect("pickup", "pick-up", 1, mem,
                               observed_kind="moved", payload=payload)
        self.assertEqual(self.ref.floor.attempts(ev), 1)
        self.assertEqual(self.ref.intent, "pickup")

    def test_stale_pickup_token_is_dropped(self):
        mem = self._mem()
        ev = self._observe(mem)
        payload = ("pickup", "opportunistic", ev.instance, ev.pos[0],
                   ev.pos[1], ev.source_epoch + 99)
        self.ref.commit_effect("pickup", "pick-up", 1, mem,
                               observed_kind="moved", payload=payload)
        self.assertEqual(self.ref.floor.attempts(ev), 0)
        self.assertEqual(self.ref.intent, "")

    def test_continuation_over_offer_declines_the_token(self):
        mem = self._mem()
        self._observe(mem)
        # acquire the destination, then send one continuation over the offered
        # pickup: that continuation declines the token (plan 3.3)
        first = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.ref.commit_effect(first.proposed_effect, first.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=first.effect_payload)
        self.assertIsNotNone(self.ref.targets.held())
        cont = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertEqual(cont.effect_payload[1], "continue")
        self.ref.commit_effect(cont.proposed_effect, cont.semantic_label, 2,
                               mem, observed_kind="moved",
                               payload=cont.effect_payload)
        ev = self.ref.floor.evidence(mem.hero)
        self.assertTrue(self.ref.floor.declined(ev))
        table = self.ref.prepare(nav_test.ctx(mem)).table
        self.assertNotIn("pick-up", self._labels(table))

    def test_destination_and_pickup_presentation_never_mutate_retained_table(
            self):
        mem = self._mem()
        self._observe(mem)
        table = self.ref.prepare(nav_test.ctx(mem)).table
        before_bytes = table.canonical_bytes
        before_ids = [c.candidate_id for c in table.ordered_candidates]
        first, r1 = presentation.present("command", table.ordered_candidates,
                                         nav_test.ctx(mem))
        second, r2 = presentation.present("command", table.ordered_candidates,
                                          nav_test.ctx(mem))
        self.assertEqual((r1, r2), ("", ""))
        self.assertEqual(list(first.keys), list(second.keys))
        self.assertEqual(first.criteria, second.criteria)
        # the retained table is untouched by rendering
        self.assertEqual(table.canonical_bytes, before_bytes)
        self.assertEqual([c.candidate_id for c in table.ordered_candidates],
                         before_ids)

    def _collect_site(self, appearance="coin appearance"):
        """A mem with item evidence under the hero and an interacting target."""
        mem = self._mem()
        ev = self.ref.floor.observe_item(self.ref.instance_id, mem.hero,
                                         appearance)
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=mem.hero), 1)
        self.ref.targets.commit(
            instance_id=self.ref.instance_id,
            purpose=navigation.COMMIT_COLLECT_ITEMS, pos=tuple(mem.hero),
            family=navigation.TFAM_FRONTIER,
            source=navigation.SRC_DIRECTIVE,
            phase=navigation.PHASE_INTERACTING)
        return mem, ev, view

    def _pickup_candidate(self, mem, view):
        """The production pickup candidate for the given collect advice."""
        table = self.ref.prepare(nav_test.ctx(mem, directives=[view])).table
        cand = table.scripted()
        self.assertEqual(cand.semantic_label, "pick-up")
        return cand

    def test_on_square_collect_advice_produces_pickup_action(self):
        mem = self._mem()
        self.ref.floor.observe_item(self.ref.instance_id, (1, 10),
                                    "coin appearance")
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(1, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        # the on-square collect target yields the pickup action, not a failure
        self.assertEqual(cand.semantic_label, "pick-up")
        self.assertEqual(cand.action.to_wire(), {"key": ord(",")})
        self.assertEqual(cand.effect_payload[0], "pickup")
        self.assertEqual(cand.effect_payload[1], "collect")
        self.assertEqual(tuple(cand.effect_payload[3:5]), (1, 10))

    def test_collection_arrival_begins_pickup_phase_not_settlement(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)},
                                (1, 10))
        self.ref.floor.observe_item(self.ref.instance_id, (4, 10),
                                    "coin appearance")
        view = DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(4, 10)), 1)
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                               mem, observed_kind="moved",
                               payload=cand.effect_payload)
        held = self.ref.targets.held()
        self.assertEqual(held.purpose, navigation.COMMIT_COLLECT_ITEMS)
        # arrival at the site: the target is NOT settled; the phase turns
        arrived = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)},
                                    (4, 10))
        self.ref.directive_settlement = None
        cont = policy.ScriptedReflex._dest_payload("continue", held)
        self.ref.commit_effect("navigate", "navigate", 2, arrived,
                               observed_kind="moved", payload=cont)
        self.assertIsNotNone(self.ref.targets.held())
        self.assertEqual(self.ref.targets.held().phase,
                         navigation.PHASE_INTERACTING)
        self.assertIsNone(self.ref.directive_settlement)
        # the on-square collect now offers the pickup initiation ...
        self.ref.floor.observe_item(self.ref.instance_id, (4, 10),
                                    "coin appearance")
        cand2 = self._pickup_candidate(arrived, view)
        self.ref.arm_pickup(cand2.effect_payload)       # the send boundary
        # ... and only the pickup outcome settles it and its generation
        arrived.messages.append("There is nothing here to pick up.")
        self.ref.note_observation(arrived)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")

    def test_direct_no_items_terminates_target(self):
        mem, ev, view = self._collect_site()
        cand = self._pickup_candidate(mem, view)
        self.ref.arm_pickup(cand.effect_payload)        # the send boundary
        self.assertEqual(self.ref.intent, "pickup")
        self.assertEqual(self.ref.pickup_purpose, "collect")
        self.ref.directive_settlement = None
        mem.messages.append("There is nothing here to pick up.")
        self.ref.note_observation(mem)                  # the result
        self.assertEqual(self.ref.intent, "")
        self.assertIsNone(self.ref.pickup_pending)
        self.assertEqual(self.ref.floor.outcome(ev), pickup.OUTCOME_NO_ITEMS)
        self.assertEqual(self.ref.floor.negative(self.ref.instance_id,
                                                 mem.hero),
                         pickup.OUTCOME_NO_ITEMS)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")
        labels = [c.semantic_label for c in self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.ordered_candidates]
        self.assertNotIn("pick-up", labels)             # no reoffer

    def test_capacity_refusal_settles_the_target(self):
        mem, _ev, view = self._collect_site()
        cand = self._pickup_candidate(mem, view)
        self.ref.arm_pickup(cand.effect_payload)
        ctx = nav_test.ctx(mem)
        ctx.need = {"kind": "yn",
                    "prompt": "Your backpack is getting hard to carry, "
                              "continue? [yn]"}
        action, _reason, effect, _payload = self.ref._noncommand(ctx, "yn")
        self.assertEqual(action, {"yn": ord("n")})
        self.assertEqual(effect, "pickup-refuse")
        self.ref.directive_settlement = None
        self.ref.commit_effect(effect, "prompt", 2, mem,
                               observed_kind="prompt-opened", payload=())
        self.assertEqual(self.ref.intent, "")
        self.assertIsNone(self.ref.pickup_pending)
        self.assertEqual(self.ref.floor.negative(self.ref.instance_id,
                                                 mem.hero),
                         pickup.OUTCOME_REFUSED)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")

    def test_autoselect_unknown_then_inventory_confirmed_success(self):
        mem, ev, view = self._collect_site()
        mem.inventory.refresh([{"text": "a dagger"}], 1, 100)
        cand = self._pickup_candidate(mem, view)
        self.ref.arm_pickup(cand.effect_payload)        # pre-send baseline
        self.ref.directive_settlement = None
        # an autoselect with no visible message is `unknown`: the bounded
        # attempt is spent but the interaction phase and the target survive
        self.ref.note_observation(mem)
        self.assertEqual(self.ref.floor.outcome(ev), pickup.OUTCOME_UNKNOWN)
        self.assertEqual(self.ref.intent, "pickup")
        self.assertIsNotNone(self.ref.targets.held())
        self.assertIsNone(self.ref.directive_settlement)
        # a second bounded attempt, then the inventory delta confirms success
        cand2 = self._pickup_candidate(mem, view)
        self.ref.arm_pickup(cand2.effect_payload)
        mem.inventory.refresh([{"text": "a dagger"},
                               {"text": "some gold pieces"}], 2, 101)
        self.ref.note_observation(mem)
        self.assertEqual(self.ref.floor.outcome(ev), pickup.OUTCOME_SUCCESS)
        self.assertEqual(self.ref.intent, "")
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "reached")

    def test_delivery_repair_does_not_double_consume_pickup_attempt(self):
        mem, ev, view = self._collect_site()
        cand = self._pickup_candidate(mem, view)
        identity = ("pickup", 7)                    # the original decision
        self.assertTrue(self.ref.arm_pickup(cand.effect_payload,
                                            identity=identity))
        self.assertEqual(self.ref.floor.attempts(ev), 1)          # one charge
        init_sig = self.ref.pickup_pending["init_inventory"]
        self.assertEqual(len([e for e in self.ref.lifecycle.events
                              if e.get("outcome") == "attempted"]), 1)
        # the delivery repair resends the frozen action for the SAME identity
        self.assertFalse(self.ref.arm_pickup(cand.effect_payload,
                                             identity=identity))
        self.assertEqual(self.ref.floor.attempts(ev), 1)   # still one charge
        self.assertEqual(len([e for e in self.ref.lifecycle.events
                              if e.get("outcome") == "attempted"]), 1)
        self.assertEqual(self.ref.pickup_pending["init_inventory"], init_sig)
        self.assertTrue(self.ref.floor.budget_available(ev))      # one left
        # the repaired attempt's unknown result is NOT converted to exhaustion
        self.ref.directive_settlement = None
        self.ref.note_observation(mem)
        self.assertEqual(self.ref.floor.outcome(ev), pickup.OUTCOME_UNKNOWN)
        self.assertIsNotNone(self.ref.targets.held())             # still held
        self.assertIsNone(self.ref.directive_settlement)
        self.assertEqual(self.ref.intent, "pickup")
        # a genuinely new accepted decision creates a new identity and
        # consumes the second (final) attempt
        cand2 = self._pickup_candidate(mem, view)
        self.assertTrue(self.ref.arm_pickup(cand2.effect_payload,
                                            identity=("pickup", 11)))
        self.assertEqual(self.ref.floor.attempts(ev), 2)
        self.assertFalse(self.ref.floor.budget_available(ev))

    def test_repair_identity_is_the_original_send_ordinal(self):
        # the controller derives the stable identity from the *original*
        # decision's send, so a repair cannot re-charge the attempt (3.3)
        repair = {"ordinal": 42}
        self.assertEqual(("pickup", repair.get("ordinal")), ("pickup", 42))
        # a fresh send uses its own ordinal
        self.assertNotEqual(("pickup", 43), ("pickup", 42))

    def test_terminal_invalid_cancels_the_pending_pickup_freeze(self):
        mem, ev, view = self._collect_site()
        cand = self._pickup_candidate(mem, view)
        self.ref.arm_pickup(cand.effect_payload, identity=("pickup", 7))
        self.assertIsNotNone(self.ref.pickup_pending)
        # a terminal non-repair invalid cancels the freeze and the intent
        self.ref.cancel_pickup()
        self.assertIsNone(self.ref.pickup_pending)
        self.assertIsNone(self.ref.pickup_attempt_identity)
        self.assertEqual(self.ref.intent, "")

    def test_pickup_choice_criteria_object_key_index_and_n_frozen(self):
        mem = self._mem()
        self._observe(mem)
        table = self.ref.prepare(nav_test.ctx(mem)).table
        n = len(table.ordered_candidates)
        pres, refusal = presentation.present(
            "command", table.ordered_candidates, nav_test.ctx(mem))
        self.assertEqual(refusal, "")
        # the criteria object is insertion-ordered, one key per retained member
        self.assertEqual(len(pres.keys), n)
        self.assertEqual(list(pres.criteria), list(pres.keys))
        self.assertEqual(sorted(pres.key_index.values()), list(range(n)))
        # presentation neither adds, drops nor reorders: the pick-up member is
        # present with a stable key
        self.assertIn("pick-up", pres.criteria)
        self.assertEqual(pres.keys[-1], "pick-up")


class PickupEvidenceAndIntent(unittest.TestCase):
    """AC10/AC11: the floor evidence ledger, trust rules and menu model."""

    def test_hungry_exact_ration_allows_narrow_pickup_fallback(self):
        self.assertTrue(pickup.urgent_food_fallback(
            hungry=True, usable_cached_food=False,
            exact_ration_name="food ration"))
        self.assertFalse(pickup.urgent_food_fallback(
            hungry=False, usable_cached_food=False,
            exact_ration_name="food ration"))
        self.assertFalse(pickup.urgent_food_fallback(
            hungry=True, usable_cached_food=True,
            exact_ration_name="food ration"))
        self.assertFalse(pickup.urgent_food_fallback(
            hungry=True, usable_cached_food=False, exact_ration_name=""))

    def test_food_appearance_alone_never_asserts_safe_food(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        self.assertTrue(pickup.appearance_authorizes_inspection(ev))
        self.assertFalse(pickup.appearance_proves_safety(ev))
        self.assertIn("no safety guarantee",
                      pickup.RECOGNIZED_FOOD_WORDING.lower())

    def test_hero_overlay_retains_last_seen_evidence_without_claiming_presence(
            self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        retained = led.retain_on_arrival(1, (5, 5))
        self.assertTrue(retained.last_seen)
        # the source epoch is unchanged by the move onto the item
        self.assertEqual(retained.source_epoch, ev.source_epoch)
        self.assertEqual(retained.token, ev.token)

    def test_stationary_frames_do_not_reset_attempt_budget(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        led.note_initiation(ev)
        again = led.observe_item(1, (5, 5), "food")     # same appearance
        self.assertEqual(again.source_epoch, ev.source_epoch)
        self.assertEqual(led.attempts(again), 1)

    def test_inventory_refresh_does_not_reset_attempt_budget(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        led.note_initiation(ev)
        led.note_initiation(ev)
        self.assertFalse(led.budget_available(ev))
        # an inventory refresh / global map revision is not a floor observation
        self.assertEqual(led.attempts(ev), pickup.MAX_PICKUP_ATTEMPTS)

    def test_pickup_attempt_limit_counts_reconciled_initiations_only(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        self.assertTrue(led.budget_available(ev))
        led.note_initiation(ev)
        self.assertTrue(led.budget_available(ev))
        led.note_initiation(ev)
        self.assertFalse(led.budget_available(ev))
        self.assertEqual(led.attempts(ev), pickup.MAX_PICKUP_ATTEMPTS)

    def test_new_item_evidence_reopens_bounded_attempts(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        led.note_initiation(ev)
        led.note_initiation(ev)
        self.assertFalse(led.budget_available(ev))
        fresh = led.observe_item(1, (5, 5), "food ration")   # material change
        self.assertNotEqual(fresh.source_epoch, ev.source_epoch)
        self.assertTrue(led.budget_available(fresh))

    def test_declined_pickup_not_reoffered_for_unchanged_evidence(self):
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "gold")
        self.assertFalse(led.declined(ev))
        led.note_declined(ev)
        self.assertTrue(led.declined(ev))
        again = led.observe_item(1, (5, 5), "gold")
        self.assertTrue(led.declined(again))

    def test_no_items_negative_survives_repeated_directive(self):
        led = pickup.FloorLedger()
        led.note_negative(1, (5, 5), pickup.OUTCOME_NO_ITEMS)
        self.assertEqual(led.negative(1, (5, 5)), pickup.OUTCOME_NO_ITEMS)
        # a repeated directive does not clear the negative
        self.assertEqual(led.negative(1, (5, 5)), pickup.OUTCOME_NO_ITEMS)

    def test_current_arrival_message_binds_item_to_location(self):
        led = pickup.FloorLedger()
        led.observe_item(1, (5, 5), "food")
        ev = led.bind_ration_name(1, (5, 5), "food ration", (5, 5))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.ration_name, "food ration")

    def test_old_floor_message_cannot_bind_after_movement(self):
        led = pickup.FloorLedger()
        led.observe_item(1, (5, 5), "food")
        # the hero has moved away, so a recent message cannot bind here
        ev = led.bind_ration_name(1, (5, 5), "food ration", (6, 5))
        self.assertIsNone(ev)

    def test_pickup_menu_selects_unique_authorized_row(self):
        rows = pickup.parse_rows([
            {"r": 1, "text": "a dagger", "selectable": True},
            {"r": 2, "text": "2 food rations", "selectable": True},
        ])
        kind, row, _why = pickup.menu_decision(rows, urgent_food=True)
        self.assertEqual(kind, "select")
        self.assertEqual(row.index, 2)

    def test_pickup_menu_selects_only_bound_authorized_row(self):
        rows = pickup.parse_rows([
            {"r": 1, "text": "a dagger", "selectable": True},
            {"r": 2, "text": "some gold pieces", "selectable": True},
        ])
        kind, row, _why = pickup.menu_decision(rows, bound_row_index=1)
        self.assertEqual(kind, "select")
        self.assertEqual(row.index, 1)

    def test_broad_ambiguous_pile_is_cancelled_not_model_chosen(self):
        rows = pickup.parse_rows([
            {"r": 1, "text": "food ration", "selectable": True},
            {"r": 2, "text": "cram ration", "selectable": True},
        ])
        kind, row, why = pickup.menu_decision(rows, urgent_food=True)
        self.assertEqual(kind, "cancel")
        self.assertIsNone(row)
        self.assertIn("ambiguous", why)

    def test_unpaid_rows_and_capacity_prompts_are_declined(self):
        rows = pickup.parse_rows([
            {"r": 1, "text": "food ration (unpaid)", "selectable": True},
        ])
        kind, row, _why = pickup.menu_decision(rows, urgent_food=True)
        self.assertEqual(kind, "cancel")
        self.assertIsNone(row)
        self.assertTrue(pickup.is_unpaid_row(rows[0]))
        self.assertTrue(pickup.is_capacity_prompt(
            "Your backpack is getting hard to carry, continue? [yn]"))
        self.assertFalse(pickup.is_capacity_prompt("Really quit? [yn]"))

    def test_yes_no_refusal_is_decline_not_failure(self):
        # a refused acquisition is a decline-class outcome, never a success
        self.assertNotEqual(pickup.OUTCOME_REFUSED, pickup.OUTCOME_SUCCESS)
        self.assertTrue(pickup.terminates_target(pickup.OUTCOME_REFUSED))
        self.assertFalse(pickup.terminates_target(pickup.OUTCOME_UNKNOWN))

    def test_deliberate_cancellation_terminates_site(self):
        self.assertTrue(pickup.terminates_target(pickup.OUTCOME_CANCELED))

    def test_pickup_initiation_recorded_distinct_from_outcome(self):
        # initiation counting is separate from the outcome vocabulary
        led = pickup.FloorLedger()
        ev = led.observe_item(1, (5, 5), "food")
        led.note_initiation(ev)
        self.assertEqual(led.attempts(ev), 1)
        self.assertIsNone(led.negative(1, (5, 5)))

    def test_single_object_entry_autoselect_is_acknowledged_uncertainty(self):
        residual = pickup.single_entry_autoselect_uncertainty()
        self.assertEqual(residual["path"], "autoselect-single")
        self.assertFalse(residual["menu_observed"])
        self.assertFalse(residual["claimed"])


class PickupShapeVocabulary(unittest.TestCase):
    def test_pickup_shape_disposition_is_exhaustive(self):
        """Every one of the eight shapes is dispositioned exactly once."""
        pickup_shapes.validate_partition()          # raises on a gap/overlap
        disp = pickup_shapes.disposition()
        native = disp["native_passed"]
        manual = disp["manual_required"]
        self.assertEqual(sorted(native + manual),
                         sorted(pickup_shapes.SHAPE_KEYS))
        self.assertEqual(len(set(native) & set(manual)), 0)
        # each shape's own record agrees with the sets
        for row in disp["shapes"]:
            expected = "native" if row["key"] in native else "manual"
            self.assertEqual(row["disposition"], expected)
        # the eight stable keys are distinct and titled
        self.assertEqual(len(set(pickup_shapes.SHAPE_KEYS)), 8)
        for key in pickup_shapes.SHAPE_KEYS:
            self.assertTrue(pickup_shapes.shape_title(key))

    def test_disposition_rejects_gap_and_overlap(self):
        with self.assertRaises(ValueError):
            pickup_shapes.validate_partition(native=("no_object",),
                                             manual=("multi_row_pick_any",))
        with self.assertRaises(ValueError):
            pickup_shapes.validate_partition(
                native=("no_object",), manual=("no_object",) +
                tuple(k for k in pickup_shapes.SHAPE_KEYS
                      if k != "no_object"))

    def test_pickup_protocol_shape_requires_native_or_manual_probe(self):
        """A shape is proven natively or requires the manual probe."""
        disp = pickup_shapes.disposition()
        self.assertEqual(disp["native_passed"], ["no_object"])
        # every non-native shape is explicitly manual-required, never dropped
        for key in pickup_shapes.SHAPE_KEYS:
            if key not in disp["native_passed"]:
                self.assertIn(key, disp["manual_required"])


class PickupRowModel(unittest.TestCase):
    """Pure row-model checks; they verify the row model, not the wire shape."""

    RAW_PILE = [
        {"r": 1, "text": "a dagger", "selectable": True},
        {"r": 2, "text": "2 food rations", "selectable": True},
        {"r": 3, "text": "some gold pieces", "selectable": True},
        {"r": 4, "text": "a shop item (unpaid)", "selectable": True},
        {"r": 5, "text": "", "selectable": False},
    ]

    def test_pickup_row_model_shapes_are_self_consistent(self):
        rows = pickup_shapes.parse_rows(self.RAW_PILE)
        self.assertEqual(len(rows), 5)
        # an unpaid row is never selectable for acquisition
        self.assertTrue(pickup_shapes.is_unpaid_row(rows[3]))
        self.assertNotIn(rows[3], pickup_shapes.selectable_rows(rows))
        # a uniquely authorized exact row is selected
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: "food ration" in t)
        self.assertIsNotNone(row)
        self.assertEqual(row.index, 2)
        # an empty predicate match is a decline
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: "no such item" in t)
        self.assertIsNone(row)
        # a broad predicate is a conservative pile cancellation
        row, why = pickup_shapes.unique_authorized_row(
            rows, lambda t: True)
        self.assertIsNone(row)
        self.assertIn("ambiguous", why)

    def test_capacity_prompt_classification(self):
        self.assertTrue(pickup_shapes.is_capacity_prompt(
            "Your backpack is getting hard to carry, continue? [yn]"))
        self.assertTrue(pickup_shapes.is_capacity_prompt(
            "You are now burdened.  Continue? [yn]"))
        self.assertFalse(pickup_shapes.is_capacity_prompt(
            "Really quit without saving? [yn]"))
        self.assertFalse(pickup_shapes.is_capacity_prompt(
            "Do you want your possessions identified? [ynq]"))


class OnSquareCollectionLifecycle(unittest.TestCase):
    """Round-4: an on-square collect acquires at its pickup dispatch.

    The first on-square pickup send *atomically* establishes the
    directive-owned interacting commitment, emits exactly one acquisition, one
    resolved generation and one first action, and its terminal outcome retires
    that same serial and settles the directive generation.
    """

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _view(self):
        return DirectiveView(DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(1, 10)), 1)

    def _mem(self):
        mem = nav_test.mem_with({(x, 10): FLOOR for x in range(1, 8)}, (1, 10))
        self.ref.floor.observe_item(self.ref.instance_id, (1, 10),
                                    "coin appearance")
        mem.inventory.refresh([{"text": "a dagger"}], 1, 100)  # pre-send base
        return mem

    def _cand(self, mem, view):
        cand = self.ref.prepare(
            nav_test.ctx(mem, directives=[view])).table.scripted()
        self.assertEqual(cand.semantic_label, "pick-up")
        return cand

    def _events(self, kind, outcome):
        return [e for e in self.ref.lifecycle.events
                if e["kind"] == kind and e["outcome"] == outcome]

    def _arm(self, cand):
        return self.ref.arm_pickup(cand.effect_payload,
                                   identity=("pickup", 1), tick=3)

    def _assert_no_reassertion(self, mem, view):
        # the settled generation is expired at the book, exactly as the
        # controller does; the next prepare is plain navigation, not a
        # destfail/search reassertion
        from tools.agent import directives as DSMOD
        book = DSMOD.DirectiveBook()
        dset, _ = DSMOD.validate_directive_set(
            {"schema_version": 2, "goals": ["collect_items"],
             "target": [1, 10], "ttl": 50})
        book.activate(dset, 1, "1")
        outcome, _gen, reason = self.ref.directive_settlement
        book.expire("destination-%s: %s" % (outcome, reason), 2, "1")
        self.assertFalse(book.has_active)
        cand = self.ref.prepare(nav_test.ctx(mem)).table.scripted()
        self.assertNotEqual(cand.semantic_label, "unresolved-destination")

    def _drive(self, *, message=None, delta=None):
        mem = self._mem()
        view = self._view()
        cand = self._cand(mem, view)
        self.assertIsNotNone(cand.effect_payload[9])       # the acquire spec
        self._arm(cand)
        # the acquisition is installed at the same send boundary
        self.assertEqual(len(self._events("destination", "acquired")), 1)
        held = self.ref.targets.held()
        self.assertIsNotNone(held)
        self.assertEqual(held.purpose, navigation.COMMIT_COLLECT_ITEMS)
        self.assertEqual(held.phase, navigation.PHASE_INTERACTING)
        self.assertEqual(held.source, navigation.SRC_DIRECTIVE)
        self.assertEqual(len(self._events("directive", "resolved")), 1)
        serial = held.serial
        if delta is not None:
            mem.inventory.refresh(delta, 2, 101)
        if message:
            mem.messages.append(message)
        self.ref.note_observation(mem)
        return mem, view, serial

    def test_on_square_success_acquires_resolves_and_settles_the_same_serial(
            self):
        mem, view, serial = self._drive(
            delta=[{"text": "a dagger"}, {"text": "some gold pieces"}])
        self.assertEqual(len(self._events("directive", "first-action")), 1)
        self.assertEqual(len(self._events("destination", "reached")), 1)
        self.assertEqual(self._events("destination", "reached")[0]["serial"],
                         serial)                       # the same serial
        self.assertEqual(len(self._events("directive", "terminal")), 1)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "reached")
        self._assert_no_reassertion(mem, view)

    def test_on_square_no_items_terminates_the_same_serial(self):
        mem, view, serial = self._drive(
            message="There is nothing here to pick up.")
        self.assertEqual(len(self._events("directive", "first-action")), 1)
        self.assertEqual(len(self._events("destination", "failed")), 1)
        self.assertEqual(self._events("destination", "failed")[0]["serial"],
                         serial)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")
        self._assert_no_reassertion(mem, view)

    def test_on_square_refusal_terminates_the_same_serial(self):
        mem, view, serial = self._drive(message="That item is unpaid.")
        self.assertEqual(len(self._events("destination", "failed")), 1)
        self.assertEqual(self._events("destination", "failed")[0]["serial"],
                         serial)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self.ref.directive_settlement[0], "failed")
        self._assert_no_reassertion(mem, view)

    def test_prepare_and_unselected_candidate_install_no_acquisition(self):
        mem = self._mem()
        view = self._view()
        self._cand(mem, view)                       # prepared, never armed
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self._events("destination", "acquired"), [])
        self.assertEqual(self._events("directive", "resolved"), [])
        self.assertIsNone(self.ref.directive_settlement)

    def test_failed_write_and_unselected_pickup_install_no_acquisition(self):
        # a failed write never reaches arm_pickup; an unselected candidate never
        # reaches it either, so neither mutates the commitment (plan 1.4/3.3)
        mem = self._mem()
        view = self._view()
        cand = self._cand(mem, view)
        self.assertIsNotNone(cand.effect_payload[9])
        # no arm == no send (a write failure or an unselected member)
        self.assertIsNone(self.ref.targets.held())
        self.assertEqual(self._events("destination", "acquired"), [])
        self.assertIsNone(self.ref.directive_settlement)


if __name__ == "__main__":
    unittest.main()
