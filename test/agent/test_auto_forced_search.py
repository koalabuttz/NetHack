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
sys.path.insert(0, HERE)

from tools.agent import forced_search as fs          # noqa: E402
from tools.agent import recovery                     # noqa: E402
from test_auto import (CLOSED, HELLO, WireHarness,   # noqa: E402
                       _line, obs)


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


# -- live wiring through the real controller harness (plan 8.4) ------------
#
# These drive the *real* ``_EpisodeRunner`` with a synthetic wire, so the
# two-send transaction is exercised end to end: the reflex nominates, the
# controller validates the ten gates, sends the ``m`` prefix, binds the exact
# following command need's ``s`` suffix, consumes the cap at the first sent
# prefix, cancels with native double-``m`` and reports ``policy-exhausted``.
# The local gates are arranged by observation content (HP, an adjacent monster
# and the engine's exact search-refusal message).

_LIVE_PAL = [[0, " ", "none", 0, "none"], [1, ".", "gray", 0, "none"],
             [2, "@", "white", 0, "none"], [3, "a", "brown", 0, "none"]]
# hero at (10,10) with a monster on the only neighbouring cell, so ordinary
# search is refused and every fallback move is unsafe -- genuinely trapped.
_LIVE_MAP = [[10, 10, 2], [11, 10, 3]]
_REFUSAL = ("You already found a monster.  "
            "Use 'm' prefix to force another search.")


def _status(hp, hp_max, t):
    return {"hitpoints": {"text": str(hp)},
            "hitpoints-max": {"text": str(hp_max)},
            "time": {"text": str(t)},
            "dungeon-level": {"text": "1"}}


def _live_obs(seq, i, hp=10, hp_max=10, t=100, msg=_REFUSAL,
              kind="command", prompt=None):
    if i is None:
        need = None
    else:
        need = {"kind": kind, "id": i}
        if kind == "yn":
            need.update({"prompt": prompt or "Continue?",
                         "choices": None, "default": None, "numeric": False})
    rec = obs(seq, need, map_=_LIVE_MAP, pal=_LIVE_PAL,
              msg=[{"e": seq, "text": msg}] if msg else [])
    rec["s"] = _status(hp, hp_max, t)
    return rec


def _act_keys(actions):
    """The outbound action labels in wire order (keys / prompt kinds)."""
    out = []
    for a in actions:
        if a.get("type") != "act":
            continue
        action = a.get("action") or {}
        if "key" in action:
            out.append(chr(action["key"]))
        else:
            out.append(next(iter(action)))
    return out


class LiveWiring(WireHarness):
    """The real runner proposes, binds, cancels and caps the transaction."""

    def _run(self, recs, max_ticks=200):
        scenario = b"".join([_line(HELLO)] + [_line(r) for r in recs]
                            + [_line(CLOSED)])
        result, actions = self.run_scenario(scenario, max_ticks=max_ticks)
        return result, _act_keys(actions)

    def test_prefix_then_bound_suffix_succeeds_once(self):
        # The prefix is expected to consume no game time (plan 5.4): the
        # post-prefix observation must carry the *same* displayed time as the
        # transaction origin, so the following command need is at t=100 and
        # the suffix's outcome frame advances to t=101.
        recs = [_live_obs(1, 1, t=100), _live_obs(2, 2, t=100),
                _live_obs(3, 3, t=100), _live_obs(4, None, t=101)]
        result, keys = self._run(recs)
        self.assertIn("m", keys)
        self.assertIn("s", keys)
        self.assertEqual(result.forced_activations, 1)
        self.assertEqual(result.forced_suffixes, 1)
        self.assertEqual(result.forced_successes, 1)
        # the suffix is the single s bound to the immediately following need
        self.assertEqual(keys.count("s"), 1)

    def test_activation_cap_persists_and_denies_the_fourth(self):
        # enough identical trapped command needs to attempt four activations
        recs = [_live_obs(i, i, t=100 + i) for i in range(1, 12)]
        result, keys = self._run(recs)
        self.assertLessEqual(result.forced_activations, fs.ACTIVATION_CAP)
        self.assertEqual(result.forced_activations, fs.ACTIVATION_CAP)
        # the fourth attempt is denied by gate 7 and degrades to the trapped
        # graceful quit, not another search
        self.assertIn("#", keys)
        self.assertGreaterEqual(result.forced_denials, 1)
        self.assertEqual(result.stop_reason, "policy-exhausted")

    def test_hp_exactly_half_denies_the_exception(self):
        # HP strictly above 50% is required; equality must fail locally, so
        # the reflex never even nominates and the fallback is the trapped quit
        recs = [_live_obs(1, 1, hp=5, hp_max=10, t=100),
                _live_obs(2, 2, hp=5, hp_max=10, t=100)]
        result, keys = self._run(recs)
        self.assertNotIn("m", keys)
        self.assertIn("#", keys)
        self.assertEqual(result.forced_activations, 0)

    def test_generic_monster_text_does_not_activate(self):
        # a lookalike that is not the exact correlated refusal never activates
        recs = [_live_obs(1, 1, msg="You found a monster!", t=100),
                _live_obs(2, 2, msg="You found a monster!", t=100),
                _live_obs(3, 3, msg="You found a monster!", t=100)]
        result, _keys = self._run(recs)
        self.assertEqual(result.forced_activations, 0)

    def test_no_prefix_leak_when_the_binding_gate_fails(self):
        # the prefix is armed, but the following need's HP has dropped to 50%:
        # the suffix cannot bind, so the controller cancels with double-m and
        # never sends the prefixed search
        recs = [_live_obs(1, 1, t=100), _live_obs(2, 2, t=100),
                _live_obs(3, 3, hp=5, hp_max=10, t=100)]
        result, keys = self._run(recs)
        self.assertIn("m", keys)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)
        self.assertGreaterEqual(result.forced_cancels, 1)
        # the cancellation is the native double-m (two m keys, no time)
        self.assertEqual(keys.count("m"), 2)

    def test_intervening_prompt_never_lets_a_prefixed_action_through(self):
        # Plan 5.4: cancel on any intervening prompt.  A ``yn`` prompt cannot
        # carry the native double-``m``, so the armed prefix cannot be safely
        # cleared: the transport is terminated and *nothing* is sent through
        # the prefix -- no prompt answer, no later command, no suffix.
        recs = [_live_obs(1, 1, t=100), _live_obs(2, 2, t=100),
                _live_obs(3, 3, kind="yn", t=100),
                _live_obs(4, 4, t=100), _live_obs(5, None, t=100)]
        result, keys = self._run(recs)
        self.assertIn("m", keys)
        # the prompt is never answered while the prefix is armed
        self.assertNotIn("yn", keys)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)
        self.assertGreaterEqual(result.forced_uncleared, 1)
        self.assertEqual(result.stop_reason, "transport-failure-write")

    def test_suffix_without_time_advance_fails_and_does_not_repeat(self):
        recs = [_live_obs(1, 1, t=100), _live_obs(2, 2, t=100),
                _live_obs(3, 3, t=100), _live_obs(4, 4, t=100),
                _live_obs(5, 5, t=100)]
        result, keys = self._run(recs)
        self.assertIn("s", keys)
        self.assertEqual(result.forced_successes, 0)
        self.assertEqual(result.forced_suffixes, 1)
        # gate 10 refuses to repeat the unchanged failed activation
        self.assertIn("#", keys)

    def test_tick_cap_after_prefix_cancels_never_quits_prefixed(self):
        # max_ticks reached after the prefix: the transaction is cancelled
        # first, and no prefixed quit is ever sent
        recs = [_live_obs(1, 1, t=100), _live_obs(2, 2, t=100),
                _live_obs(3, 3, t=100)]
        result, keys = self._run(recs, max_ticks=2)
        self.assertIn("m", keys)
        self.assertNotIn("s", keys)
        self.assertNotIn("#", keys)


class CycleRecoveryOwnership(WireHarness):
    """An active movement cycle never steals the forced-search suffix."""

    def test_cycle_recovery_does_not_steal_forced_suffix_ownership(self):
        from tools.agent import (controller, policy, protocol,  # noqa: F401
                                 recording, state)
        from tools.agent.providers import ProviderConfig
        from test_auto import hello
        from test_auto_providers import paced
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        r = controller._EpisodeRunner(ctl, proc, rec, result)
        r.pending_key = protocol.NeedKey(1, 1, 1)
        r.pending_seq = 1
        r.pending_need = {"id": 1, "kind": "command", "prompt": ""}
        # a confirmed A-B-A-B oscillation drives the reflex into cycle recovery
        for pos in [(3, 10), (2, 10), (3, 10), (2, 10)]:
            r.mem.grid[pos] = "."
            r.mem.hero = pos
            r.reflex.note_observation(r.mem)
        for nb in [(2, 9), (2, 11), (3, 9), (3, 11), (1, 10)]:
            r.mem.grid[nb] = "|"
        r.mem.status.hp = 10
        r.mem.status.hp_max = 10
        r.mem.inventory.refresh([], 0, 0)
        r.req.begin({"id": 1, "kind": "command", "prompt": ""}, 1)
        self.assertTrue(r.reflex._cycled)
        # the controller-owned forced-search override outranks the cycle
        # recovery proposal after final selection
        r._forced_override = lambda need, selected: (
            {"key": protocol.KEY_SEARCH}, "forced search suffix", "")
        sent = {}
        real_emit = r._emit

        def capture(kind, obj, need_key=None, write_deadline=None,
                    need_kind=None):
            sent["obj"] = obj
            return real_emit(kind, obj, need_key=need_key,
                             write_deadline=write_deadline,
                             need_kind=need_kind)

        r._emit = capture
        r._answer_now(None)
        rec.finalize({})
        self.assertEqual(sent["obj"]["action"], {"key": protocol.KEY_SEARCH})


if __name__ == "__main__":
    unittest.main()
