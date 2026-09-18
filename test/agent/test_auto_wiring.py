#!/usr/bin/env python3
"""Wave-3 controller-wiring tests: the deferred wave-2 activation.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

These lock in the controller-owned lifecycle that makes the candidate
layer real: the staged parse/commit split of
:class:`tools.agent.state.EpisodeMemory`, single SentAttempt ownership,
pre-observe reconciliation before any memory commit, rejection exclusion
on a same-ID ordinary invalid (with the ``incomplete`` delivery-repair
exception), the level-instance automaton and instance-scoped directives.
"""

import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (arbitration, candidates,  # noqa: E402
                         controller, directives, instances, policy,
                         protocol, state)
from tools.agent.providers import ProviderConfig  # noqa: E402
from test_auto import (CLOSED, HELLO, WireHarness,  # noqa: E402
                       _line, obs)

PAL = [[0, " ", "none", 0, "none"],
       [1, ".", "gray", 0, "none"],
       [2, "@", "white", 0, "none"]]
MAP = [[10, 10, 2], [11, 10, 1]]
EAST = protocol.DIR_KEYS[(1, 0)]


def snap_of(rec):
    snap = protocol.Snapshot()
    snap.apply(rec)
    return snap


class StateStaging(unittest.TestCase):
    def test_stage_is_pure_and_commit_matches_observe(self):
        rec = obs(1, {"kind": "command", "id": 1}, map_=MAP, pal=PAL)
        a = state.EpisodeMemory()
        b = state.EpisodeMemory()
        a.observe(snap_of(rec))
        staged = b.stage(snap_of(rec))
        # staging does not mutate durable memory
        self.assertEqual(b.grid, {})
        self.assertIsNone(b.hero)
        self.assertEqual(b.messages, [])
        b.commit(staged)
        self.assertEqual(a.grid, b.grid)
        self.assertEqual(a.hero, b.hero)
        self.assertEqual(a.status.hp, b.status.hp)
        self.assertEqual(a.messages, b.messages)

    def test_messages_stay_event_id_deduplicated(self):
        msg = [{"e": 7, "text": "hello"}]
        m = state.EpisodeMemory()
        m.commit(m.stage(snap_of(obs(1, map_=MAP, pal=PAL, msg=msg))))
        m.commit(m.stage(snap_of(obs(2, map_=MAP, pal=PAL, msg=msg))))
        self.assertEqual(m.messages, ["hello"])


def bare_runner():
    r = object.__new__(controller._EpisodeRunner)
    r.attempt = None
    r.attempt_before = None
    r.rejections = {}
    r.instance = instances.LevelInstanceAutomaton()
    r.terrain = instances.TerrainMemory()
    r.observation_generation = 0
    r.attempts_armed = 0
    r.reconciliations = 0
    r._last_table_id = ""
    r.mem = state.EpisodeMemory()
    r.reflex = policy.ScriptedReflex(ProviderConfig())
    r.pending_key = protocol.NeedKey(1, 1, 1)
    r.result = types.SimpleNamespace(index=1)
    return r


class AttemptLifecycle(unittest.TestCase):
    def test_send_arms_one_attempt(self):
        r = bare_runner()
        r._arm_attempt(3, {"key": EAST})
        self.assertIsNotNone(r.attempt)
        self.assertEqual(r.attempt.sent_ordinal, 3)
        self.assertEqual(r.attempt.action,
                         candidates.ImmutableAction.key(EAST))
        self.assertTrue(r.attempt.live)
        self.assertEqual(r.attempts_armed, 1)

    def test_reconciliation_releases_the_attempt_once(self):
        r = bare_runner()
        r._arm_attempt(1, {"key": EAST})
        rec = obs(2, map_=MAP, pal=PAL)
        staged = r.mem.stage(snap_of(rec))
        r._reconcile_observation(staged)
        self.assertIsNone(r.attempt)
        self.assertEqual(r.reconciliations, 1)
        # a second observation with no in-flight attempt adds nothing
        r._reconcile_observation(r.mem.stage(snap_of(rec)))
        self.assertEqual(r.reconciliations, 1)

    def test_observation_commits_hero_only_after_reconcile(self):
        r = bare_runner()
        r._arm_attempt(1, {"key": EAST})
        staged = r.mem.stage(snap_of(obs(2, map_=MAP, pal=PAL)))
        r._reconcile_observation(staged)
        self.assertIsNone(r.mem.hero)      # parse is not commit
        r.mem.commit(staged)
        self.assertEqual(r.mem.hero, (10, 10))

    def test_automaton_allocates_on_first_playable_observation(self):
        r = bare_runner()
        r._reconcile_observation(r.mem.stage(snap_of(obs(1, map_=MAP,
                                                         pal=PAL))))
        self.assertEqual(r.instance.current(), 1)
        self.assertEqual(r.terrain.ter((10, 10)), instances.T_UNKNOWN)
        self.assertEqual(r.terrain.ter((11, 10)), instances.T_FLOOR)

    def test_closed_discards_the_in_flight_attempt(self):
        r = bare_runner()
        r._arm_attempt(1, {"key": EAST})
        r.instance.begin_playable()
        r.closed = False
        r.reflex_provider = types.SimpleNamespace(on_closed=lambda: None)
        r.mem = state.EpisodeMemory()
        r.need_boundaries = []
        r.detected_boundaries = []
        r.ledger = types.SimpleNamespace(note_boundary=lambda *a: None)
        r._note_detected = lambda *a: None
        r._pending_directives = None
        r.boundary_queue = types.SimpleNamespace(finish=lambda *a: None)
        controller._EpisodeRunner._on_closed(r, {})
        self.assertIsNone(r.attempt)
        self.assertEqual(r.instance.state, instances.STOPPED)


class RejectionExclusion(WireHarness):
    """Same-ID ordinary invalid reselects a different retained member."""

    def _scen(self, invalid_code):
        return b"".join([
            _line(HELLO),
            _line(obs(1, {"kind": "command", "id": 1}, map_=MAP,
                      pal=PAL)),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 1,
                   "code": invalid_code}),
            _line(obs(2, {"kind": "command", "id": 2}, map_=MAP, pal=PAL)),
            _line(CLOSED),
        ])

    def test_ordinary_invalid_excludes_the_exact_winner(self):
        result, actions = self.run_scenario(self._scen("kind"))
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertEqual(result.invalids, 1)
        # the retry must never recompute and resend the same winner
        self.assertNotEqual(acts[0]["action"], acts[1]["action"])

    def test_incomplete_is_delivery_repair_not_gameplay_exclusion(self):
        # `incomplete` is the sole delivery exception: it repairs transport
        # without marking the candidate gameplay-rejected (3.5)
        cand = candidates.make_candidate({"key": EAST}, "east")
        self.assertFalse(
            arbitration.classify_invalid("incomplete", cand).gameplay)
        self.assertTrue(
            arbitration.classify_invalid("incomplete", cand).repair)
        self.assertTrue(
            arbitration.classify_invalid("kind", cand).gameplay)
        # and the controller excludes only on an ordinary invalid
        r = bare_runner()
        r._arm_attempt(1, {"key": EAST})
        sig = r.attempt.action.signature()
        r._exclude_attempt()
        rs = r.rejections[candidates.normalize_need_key(r.pending_key)]
        self.assertTrue(rs.excludes_signature(sig))


class DirectiveInstanceScope(unittest.TestCase):
    def _st(self):
        return directives.PreconditionState(hero_known=True)

    def test_instance_mismatch_is_inactive(self):
        book = directives.DirectiveBook()
        dset = directives.DirectiveSet(goals=("explore_frontier",), ttl=50)
        book.activate(dset, tick=1, level="1", instance=1)
        # same displayed level, different instance: stale advice is rejected
        self.assertFalse(book.view(2, "1", self._st(), instance=2).active)
        self.assertFalse(book.has_active)          # expired exactly once
        self.assertEqual(book.generation, 1)

    def test_peek_view_never_settles_instance_expiry(self):
        book = directives.DirectiveBook()
        dset = directives.DirectiveSet(goals=("survive",), ttl=50)
        book.activate(dset, tick=1, level="1", instance=1)
        view = book.peek_view(2, "1", self._st(), instance=2)
        self.assertFalse(view.active)
        self.assertTrue(book.has_active)           # display is pure
        self.assertTrue(book.view(2, "1", self._st(), instance=1).active)

    def test_same_instance_stays_active(self):
        book = directives.DirectiveBook()
        dset = directives.DirectiveSet(goals=("survive",), ttl=50)
        book.activate(dset, tick=1, level="1", instance=3)
        self.assertTrue(book.view(2, "1", self._st(), instance=3).active)


class RejectionSetUnit(unittest.TestCase):
    def test_excluded_member_is_skipped_by_retained_selection(self):
        cands = [candidates.make_candidate({"key": EAST}, "east"),
                 candidates.make_candidate({"key": protocol.KEY_L}, "x"),
                 candidates.make_candidate({"key": protocol.KEY_SEARCH},
                                           "search")]
        table = candidates.build_table((1, 1, 1), 1, cands)
        rs = arbitration.RejectionSet()
        rs.exclude(table.ordered_candidates[0])
        chosen = arbitration.select_retained(table, rs)
        self.assertNotEqual(chosen.candidate_id,
                            table.ordered_candidates[0].candidate_id)


if __name__ == "__main__":
    unittest.main()
