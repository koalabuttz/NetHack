#!/usr/bin/env python3
"""Exhaustive forced-search cases (plan section 8.4).

One independent false test per gate (including HP exactly 50%% and unknown,
every published Hungry-or-worse state, dangerous/unknown conditions, each
legal-alternative type and missing refusal evidence), plus the transaction
interleavings: prefix write failure, prefix sent then suffix local-invalid /
write-failed / engine-invalid / no-time, an intervening prompt, the tick-cap
after the prefix, a successful time-advanced suffix exactly once, and a fourth
activation leading to the ``policy-exhausted/trapped`` graceful quit.

    python3 test/agent/test_auto_forced_search.py
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from tools.agent import forced_search as fs          # noqa: E402
from tools.agent import recovery                     # noqa: E402


def good_ctx(**over):
    """A context for which all ten gates pass, then apply overrides."""
    base = dict(
        hero_confirmed=True, command_need_coherent=True,
        instance_resolved=True, transition_pending=False,
        hp=11, hp_max=20,
        hunger="", conditions=(), conditions_complete=True,
        refusal_kind="monster",
        alternatives_exhausted=True,
        no_pending_intent=True, transport_healthy=True,
        prefix_contract_verified=True,
        activations_used=0,
        bound_suffix_need=(), following_need=(), planned_suffix="s",
        suffix_single=True, suffix_observed=True, suffix_time_advanced=True,
        reassessed=True, unchanged_failed_retry=False)
    base.update(over)
    return fs.ForcedSearchContext(**base)


def act(ctx):
    return fs.evaluate_activation_gates(ctx)


def gate(report, gid):
    return report.get(gid)


class GateBaseline(unittest.TestCase):
    def test_all_ten_gates_pass_on_good_context(self):
        rep = fs.evaluate_all(good_ctx())
        self.assertTrue(rep.ok(), rep.as_dict())
        self.assertEqual(len(rep.gates), 10)
        self.assertEqual([g.gate for g in rep.gates],
                         [fs.GATE_HERO, fs.GATE_HP, fs.GATE_CONDITIONS,
                          fs.GATE_REFUSAL, fs.GATE_EXHAUSTION, fs.GATE_READY,
                          fs.GATE_CAP, fs.GATE_BINDING, fs.GATE_OUTCOME,
                          fs.GATE_REASSESS])


class Gate1Identity(unittest.TestCase):
    def test_hero_not_confirmed_fails(self):
        self.assertFalse(gate(act(good_ctx(hero_confirmed=False)),
                              fs.GATE_HERO).ok)
        self.assertTrue(act(good_ctx()).ok())

    def test_need_not_coherent_fails(self):
        self.assertFalse(gate(act(good_ctx(command_need_coherent=False)),
                              fs.GATE_HERO).ok)

    def test_instance_not_resolved_fails(self):
        self.assertFalse(gate(act(good_ctx(instance_resolved=False)),
                              fs.GATE_HERO).ok)

    def test_transition_pending_fails(self):
        self.assertFalse(gate(act(good_ctx(transition_pending=True)),
                              fs.GATE_HERO).ok)


class Gate2Hp(unittest.TestCase):
    def test_hp_exactly_50_percent_fails(self):
        self.assertFalse(gate(act(good_ctx(hp=10, hp_max=20)),
                              fs.GATE_HP).ok)

    def test_hp_unknown_fails(self):
        self.assertFalse(gate(act(good_ctx(hp=None)), fs.GATE_HP).ok)

    def test_hp_max_unknown_fails(self):
        self.assertFalse(gate(act(good_ctx(hp_max=None)), fs.GATE_HP).ok)

    def test_hp_above_50_percent_passes(self):
        self.assertTrue(gate(act(good_ctx(hp=11, hp_max=20)),
                             fs.GATE_HP).ok)

    def test_hp_below_50_percent_fails(self):
        self.assertFalse(gate(act(good_ctx(hp=3, hp_max=20)),
                              fs.GATE_HP).ok)


class Gate3Conditions(unittest.TestCase):
    def test_every_hungry_or_worse_state_fails(self):
        for stage in ("Hungry", "Weak", "Fainting", "Fainted", "Starved"):
            with self.subTest(stage=stage):
                rep = act(good_ctx(hunger=stage))
                self.assertFalse(gate(rep, fs.GATE_CONDITIONS).ok)

    def test_satiated_hunger_ok(self):
        self.assertTrue(gate(act(good_ctx(hunger="Satiated")),
                             fs.GATE_CONDITIONS).ok)

    def test_each_dangerous_condition_fails(self):
        for name in sorted(fs.DANGEROUS_CONDITIONS):
            with self.subTest(cond=name):
                rep = act(good_ctx(conditions=(name,)))
                self.assertFalse(gate(rep, fs.GATE_CONDITIONS).ok,
                                 "condition %s must deny" % name)

    def test_each_benign_condition_passes(self):
        for name in sorted(fs.BENIGN_CONDITIONS):
            with self.subTest(cond=name):
                self.assertTrue(gate(act(good_ctx(conditions=(name,))),
                                     fs.GATE_CONDITIONS).ok)

    def test_unknown_condition_fails_closed(self):
        rep = act(good_ctx(conditions=("Quirky",)))
        self.assertFalse(gate(rep, fs.GATE_CONDITIONS).ok)

    def test_incomplete_condition_interpretation_fails(self):
        rep = act(good_ctx(conditions_complete=False))
        self.assertFalse(gate(rep, fs.GATE_CONDITIONS).ok)


class Gate4Refusal(unittest.TestCase):
    def test_missing_refusal_fails(self):
        self.assertFalse(gate(act(good_ctx(refusal_kind="")),
                              fs.GATE_REFUSAL).ok)

    def test_generic_found_a_monster_is_not_a_refusal(self):
        # the recognizer must not treat the generic phrase as a refusal
        self.assertIsNone(recovery.is_search_refusal("You found a monster!"))
        self.assertIsNone(recovery.is_search_refusal(
            "There is a monster here."))
        self.assertFalse(gate(act(good_ctx(refusal_kind="")),
                              fs.GATE_REFUSAL).ok)

    def test_exact_monster_refusal_passes(self):
        kind = recovery.is_search_refusal("You already found a monster.")
        self.assertEqual(kind, "monster")
        self.assertTrue(gate(act(good_ctx(refusal_kind=kind)),
                             fs.GATE_REFUSAL).ok)

    def test_danger_refusal_passes(self):
        kind = recovery.is_search_refusal(
            "Searching doesn't feel like a good idea right now.")
        self.assertEqual(kind, "danger")
        self.assertTrue(gate(act(good_ctx(refusal_kind=kind)),
                             fs.GATE_REFUSAL).ok)


class Gate5Exhaustion(unittest.TestCase):
    def test_legal_alternative_remaining_fails(self):
        self.assertFalse(gate(act(good_ctx(alternatives_exhausted=False)),
                              fs.GATE_EXHAUSTION).ok)


class Gate6Ready(unittest.TestCase):
    def test_each_readiness_component_false_fails(self):
        for field in ("no_pending_intent", "transport_healthy",
                      "prefix_contract_verified"):
            with self.subTest(field=field):
                rep = act(good_ctx(**{field: False}))
                self.assertFalse(gate(rep, fs.GATE_READY).ok)


class Gate7Cap(unittest.TestCase):
    def test_two_activations_still_allowed(self):
        self.assertTrue(gate(act(good_ctx(activations_used=2)),
                             fs.GATE_CAP).ok)

    def test_third_activation_denied_at_cap(self):
        self.assertFalse(gate(act(good_ctx(activations_used=3)),
                              fs.GATE_CAP).ok)


class Gate8Binding(unittest.TestCase):
    def test_non_search_suffix_fails(self):
        self.assertFalse(gate(act(good_ctx(planned_suffix="m")),
                              fs.GATE_BINDING).ok)

    def test_mismatched_bound_need_fails(self):
        rep = act(good_ctx(bound_suffix_need=(1, "s"),
                           following_need=(2, "s")))
        self.assertFalse(gate(rep, fs.GATE_BINDING).ok)

    def test_matching_bound_need_passes(self):
        rep = act(good_ctx(bound_suffix_need=(1, "s"),
                           following_need=(1, "s")))
        self.assertTrue(gate(rep, fs.GATE_BINDING).ok)


class Gate9Outcome(unittest.TestCase):
    def test_each_outcome_condition_false_fails(self):
        for field in ("suffix_single", "suffix_observed",
                      "suffix_time_advanced"):
            with self.subTest(field=field):
                rep = fs.evaluate_success_gates(good_ctx(**{field: False}))
                self.assertFalse(rep.ok())

    def test_observed_time_advanced_passes(self):
        self.assertTrue(fs.evaluate_success_gates(good_ctx()).ok())


class Gate10Reassess(unittest.TestCase):
    def test_unreassessed_fails(self):
        self.assertFalse(
            fs.evaluate_reassess(good_ctx(reassessed=False)).ok())

    def test_unchanged_failed_retry_fails(self):
        rep = fs.evaluate_reassess(good_ctx(unchanged_failed_retry=True))
        self.assertFalse(rep.ok())


class TransactionLifecycle(unittest.TestCase):
    def _txn(self, **over):
        ctx = good_ctx(**over)
        budget = fs.ForcedSearchBudget()
        budget.activations = ctx.activations_used
        rep = act(ctx)
        return fs.ForcedSearchTransaction(
            rep, (1, "s"), 3, (10, 5), "fp", budget), budget

    def test_cannot_propose_with_a_failed_gate(self):
        ctx = good_ctx(hp=10, hp_max=20)
        budget = fs.ForcedSearchBudget()
        with self.assertRaises(fs.ForcedSearchTransactionError):
            fs.ForcedSearchTransaction(act(ctx), (1, "s"), 3, (10, 5), "fp",
                                       budget)

    def test_prefix_send_consumes_cap_exactly_once(self):
        txn, budget = self._txn()
        self.assertEqual(budget.activations, 0)
        txn.on_prefix_sent("p1")
        self.assertEqual(budget.activations, 1)
        self.assertEqual(txn.state, fs.STATE_PREFIX_SENT)

    def test_prefix_local_invalid_or_write_failure_consumes_nothing(self):
        txn, budget = self._txn()
        txn.on_prefix_failed("write-failed")
        self.assertEqual(budget.activations, 0)
        self.assertEqual(txn.state, fs.STATE_FAILED)

    def test_prefix_cancellation_never_refunds(self):
        txn, budget = self._txn()
        txn.on_prefix_sent("p1")
        txn.cancel("intervening prompt")
        self.assertEqual(budget.activations, 1)

    def test_bind_suffix_requires_same_instance_and_evidence(self):
        txn, _ = self._txn()
        txn.on_prefix_sent("p1")
        self.assertFalse(txn.bind_suffix((2, "s"), same_instance=False,
                                         evidence_unchanged=True))
        self.assertFalse(txn.bind_suffix((2, "s"), same_instance=True,
                                         evidence_unchanged=False))
        self.assertFalse(txn.bind_suffix((), same_instance=True,
                                         evidence_unchanged=True))
        self.assertTrue(txn.bind_suffix((2, "s"), same_instance=True,
                                        evidence_unchanged=True))

    def test_suffix_send_requires_a_bound_need(self):
        txn, _ = self._txn()
        txn.on_prefix_sent("p1")
        with self.assertRaises(fs.ForcedSearchTransactionError):
            txn.on_suffix_sent("s1")

    def test_suffix_time_advanced_is_success(self):
        txn, _ = self._txn()
        txn.on_prefix_sent("p1")
        self.assertTrue(txn.bind_suffix((2, "s"), True, True))
        txn.on_suffix_sent("s1")
        state = txn.on_suffix_outcome(observed=True, time_advanced=True,
                                      after_time=9, after_hp=10)
        self.assertEqual(state, fs.STATE_SUCCEEDED)

    def test_suffix_no_time_is_failure(self):
        for observed, advanced in ((True, False), (False, True),
                                   (False, False)):
            with self.subTest(observed=observed, advanced=advanced):
                txn, _ = self._txn()
                txn.on_prefix_sent("p1")
                txn.bind_suffix((2, "s"), True, True)
                txn.on_suffix_sent("s1")
                state = txn.on_suffix_outcome(observed=observed,
                                              time_advanced=advanced)
                self.assertEqual(state, fs.STATE_FAILED)

    def test_engine_invalid_suffix_fails_and_is_not_refunded(self):
        txn, budget = self._txn()
        txn.on_prefix_sent("p1")
        txn.bind_suffix((2, "s"), True, True)
        # an engine invalid on the suffix ends the transaction as failure
        txn.cancel("suffix rejected: invalid")
        self.assertEqual(txn.state, fs.STATE_CANCELLED)
        self.assertEqual(budget.activations, 1)

    def test_cancel_from_any_live_state(self):
        for target in (fs.STATE_PROPOSED, fs.STATE_PREFIX_SENT,
                       fs.STATE_SUFFIX_SENT):
            with self.subTest(state=target):
                txn, _ = self._txn()
                if target in (fs.STATE_PREFIX_SENT, fs.STATE_SUFFIX_SENT):
                    txn.on_prefix_sent("p1")
                if target == fs.STATE_SUFFIX_SENT:
                    txn.bind_suffix((2, "s"), True, True)
                    txn.on_suffix_sent("s1")
                txn.cancel("any reason")
                self.assertEqual(txn.state, fs.STATE_CANCELLED)
                self.assertFalse(txn.prefix_armed())

    def test_terminal_event_exactly_once(self):
        txn, _ = self._txn()
        txn.on_prefix_sent("p1")
        txn.cancel("stop")
        state, tele = txn.terminal_event()
        self.assertEqual(state, fs.STATE_CANCELLED)
        with self.assertRaises(fs.ForcedSearchTransactionError):
            txn.terminal_event()

    def test_fourth_activation_is_trapped_quit(self):
        budget = fs.ForcedSearchBudget()
        for _ in range(fs.ACTIVATION_CAP):
            ctx = good_ctx(activations_used=budget.activations)
            txn = fs.ForcedSearchTransaction(act(ctx), (1, "s"), 3, (10, 5),
                                             "fp", budget)
            txn.on_prefix_sent("p")
            txn.cancel("reset")
        self.assertTrue(budget.exhausted())
        rep = act(good_ctx(activations_used=budget.activations))
        self.assertFalse(gate(rep, fs.GATE_CAP).ok)
        self.assertEqual(fs.TRAPPED_QUIT_REASON, "policy-exhausted/trapped")

    def test_telemetry_records_before_after(self):
        txn, _ = self._txn()
        txn.note_before(hp=11, hp_max=20, time_value=7)
        txn.on_prefix_sent("p1")
        txn.bind_suffix((2, "s"), True, True)
        txn.on_suffix_sent("s1")
        txn.on_suffix_outcome(observed=True, time_advanced=True,
                              after_time=8, after_hp=9)
        _state, tele = txn.terminal_event()
        self.assertEqual(tele.before_time, 7)
        self.assertEqual(tele.after_time, 8)
        self.assertEqual(tele.time_delta(), 1)
        self.assertEqual(tele.hp_delta(), -2)
        self.assertEqual(tele.risk_label, fs.RISK_LABEL)

    def test_prefix_is_armed_only_while_prefix_sent(self):
        txn, _ = self._txn()
        self.assertFalse(txn.prefix_armed())
        txn.on_prefix_sent("p1")
        self.assertTrue(txn.prefix_armed())
        txn.bind_suffix((2, "s"), True, True)
        txn.on_suffix_sent("s1")
        self.assertFalse(txn.prefix_armed())


class BudgetUnit(unittest.TestCase):
    def test_cap_boundaries(self):
        b = fs.ForcedSearchBudget(cap=3)
        self.assertTrue(b.allows())
        b.consume()
        b.consume()
        self.assertEqual(b.remaining(), 1)
        b.consume()
        self.assertFalse(b.allows())
        self.assertTrue(b.exhausted())


if __name__ == "__main__":
    unittest.main()
