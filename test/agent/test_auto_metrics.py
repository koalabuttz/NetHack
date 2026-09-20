#!/usr/bin/env python3
"""Tests for the streaming exploration metrics (section 10.2).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Uses the bounded replay fixture under ``test/agent/fixtures/auto`` so the
metrics run against a real recording, not a hand-built dict.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import exploration_metrics as M  # noqa: E402
from tools.agent import protocol  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures", "auto")
_LEGACY = os.path.join(_FIX, "legacy-ep3.wire.jsonl")
_SHORT = os.path.join(_FIX, "short.wire.jsonl")


def confirmed_moves(wire_path):
    """The sequence of confirmed hero cells, stationary duplicates removed.

    A *confirmed move* is a hero cell that differs from the previous
    confirmation; a repeated confirmation (a stationary frame) adds no entry.
    This is the report-local evidence the plan asks for, distinct from the
    stationary ``(hero, displayed time)`` loop-span metric.
    """
    cells = []
    with open(wire_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if '"obs"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            snap = protocol.Snapshot()
            snap.apply(obj)
            hero = M.hero_of(snap)
            if hero is None:
                continue
            if cells and cells[-1] == hero:
                continue
            cells.append(tuple(hero))
    return cells


def alternating_moves(wire_path):
    """Count consecutive confirmed AB alternations in *wire_path*.

    A move to a cell whose predecessor-of-predecessor is the current cell (and
    which is not the immediately previous cell) is an alternation: the
    successive ``A-B-A`` and ``B-A-B`` confirmations a two-cell oscillation
    shows.  Returns ``(alternations, longest_run, moves)``.
    """
    cells = confirmed_moves(wire_path)
    alternations = longest = run = 0
    for i in range(2, len(cells)):
        if cells[i] == cells[i - 2] and cells[i] != cells[i - 1]:
            run += 1
            alternations += 1
            longest = max(longest, run)
        else:
            run = 0
    return alternations, longest, len(cells)


def alternation_share(wire_path):
    """The fraction of confirmed moves that continue a period-2 alternation.

    ``(alternations / (moves - 2), longest_run)`` -- the movers that *had* a
    two-back predecessor, so a period-3 walk scores 0 while a perfect ABAB
    walk scores 1.0.  A short wire cannot score above 0.
    """
    alternations, longest, moves = alternating_moves(wire_path)
    return alternations / float(max(1, moves - 2)), longest


def write_controlled_wire(directory, name, cells):
    """Write one obs line per *cells* entry: a full-snapshot hero at each cell.

    A minimal but real ``.wire.jsonl``: every frame is a complete ``base:null``
    snapshot whose only painted cell is the hero, so the confirmed-move
    sequence over the file is exactly ``cells`` (duplicates preserved).  This
    is the controlled evidence a mutated ``alternating_moves`` cannot survive.
    """
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        for i, (x, y) in enumerate(cells, start=1):
            fh.write(json.dumps({
                "v": 1, "ch": "player", "type": "obs", "seq": i,
                "base": None, "s": {}, "cond": [],
                "pal": [[0, " ", "none", 0, "none"],
                        [1, "@", "gray", 0, "none"]],
                "map": [[x, y, 1]], "cur": None,
                "msg": [], "hist": [], "windows": []}) + "\n")
    return path


class ParseDlvl(unittest.TestCase):
    def test_parses_the_displayed_depth(self):
        self.assertEqual(M.parse_dlvl("Dlvl:3"), 3)
        self.assertEqual(M.parse_dlvl("1"), 1)
        self.assertEqual(M.parse_dlvl("Dlvl:12 "), 12)

    def test_unparseable_is_none_not_zero(self):
        self.assertIsNone(M.parse_dlvl(""))
        self.assertIsNone(M.parse_dlvl("Dlvl:-"))
        self.assertIsNone(M.parse_dlvl("somewhere"))


class HeroExtraction(unittest.TestCase):
    def _snap(self, cells):
        snap = protocol.Snapshot()
        pal = [(0, " ", "none", 0, "none")]
        mp = []
        for i, (pos, glyph) in enumerate(cells, start=1):
            pal.append((i, glyph, "gray", 0, "none"))
            mp.append([pos[0], pos[1], i])
        snap.apply({"base": None, "pal": pal, "map": mp, "cur": None,
                    "seq": 1})
        return snap

    def test_unique_hero(self):
        snap = self._snap((((5, 5), "@"),))
        self.assertEqual(M.hero_of(snap), (5, 5))

    def test_multiple_at_is_not_a_hero(self):
        snap = self._snap((((5, 5), "@"), ((6, 6), "@")))
        self.assertIsNone(M.hero_of(snap))


class EpisodeMetricsTests(unittest.TestCase):
    def test_streams_the_bounded_fixture(self):
        out = M.episode_metrics(_LEGACY)
        self.assertEqual(out["observations"], 12)
        for key in ("discovered_cells_instance_scoped",
                    "entered_cells_instance_scoped",
                    "stairs_from_map_triples", "depth_max",
                    "displayed_turns", "longest_loop_span"):
            self.assertIn(key, out)
        self.assertEqual(out["depth_max"], 1)

    def test_stairs_require_a_map_triple(self):
        # the fixture draws no '>' tile, so the triple-derived stair count is
        # zero even though the level has a staircase somewhere
        out = M.episode_metrics(_LEGACY)
        self.assertEqual(out["stairs_from_map_triples"], 0)

    def test_streaming_state_is_bounded(self):
        em = M.EpisodeMetrics()
        self.assertIsInstance(em.cells, set)
        self.assertIsInstance(em.entered, set)
        # the only growing list is one entry per *ended* loop span
        self.assertIsInstance(em.loop_spans, list)

    def test_campaign_metrics_scans_the_fixture_dir(self):
        out = M.campaign_metrics(_FIX)
        self.assertEqual(out["episode_count"], 3)
        self.assertTrue(all("observations" in e for e in out["episodes"]))


class AlternatingMotionMetrics(unittest.TestCase):
    """Confirmed movement alternation is distinct from stationary loop spans."""

    def test_alternating_motion_is_distinct_from_stationary_loop_spans(self):
        # the legacy loop-span metric measures identical (hero, time) frames
        # only; it cannot see a two-cell oscillation, which the confirmed-move
        # helper counts.  They are different quantities over the same wire.
        loop = M.episode_metrics(_SHORT)
        alternations, longest, moves = alternating_moves(_SHORT)
        self.assertGreaterEqual(alternations, 0)
        self.assertGreaterEqual(longest, 0)
        self.assertGreaterEqual(moves, 0)
        # the stationary loop-span field is still present and unchanged in
        # meaning (a count of duplicate frames, not of movement)
        self.assertIn("longest_loop_span", loop)
        self.assertIn("loop_spans_ge_2", loop)
        # a stationary run is not counted as an alternation
        self.assertLessEqual(longest, max(0, moves - 2))

    def test_validation_report_counts_confirmed_alternating_moves(self):
        # the report-local helper counts confirmed AB alternations and reports
        # the confirmed-move total alongside them, scoped to the recording
        alternations, longest, moves = alternating_moves(_SHORT)
        self.assertEqual(moves, len(confirmed_moves(_SHORT)))
        self.assertGreater(moves, 20)
        self.assertGreaterEqual(alternations, longest)
        # the legacy fixture has no recorded movement sidecar but is still
        # scannable: the helper returns a bounded, nonnegative triple
        alt2, long2, moves2 = alternating_moves(_LEGACY)
        self.assertGreaterEqual(min(alt2, long2, moves2), 0)


class ControlledAlternationFixtures(unittest.TestCase):
    """Known wire fixtures pin the exact alternation measurement.

    A mutated ``alternating_moves`` that returned ``(0, 0, moves)`` for every
    input would still satisfy the loose bounds above; these fixtures carry a
    known answer so any such mutation fails.
    """

    _A = (5, 5)
    _B = (6, 5)
    _C = (7, 5)

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="altmoves-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _wire(self, name, cells):
        return write_controlled_wire(self.dir, name, cells)

    def test_exact_counts_ababa_stationary_and_period_three(self):
        ababa = self._wire("ababa.wire.jsonl", [self._A, self._B] * 2 + [self._A])
        self.assertEqual(confirmed_moves(ababa),
                         [self._A, self._B, self._A, self._B, self._A])
        # three AB-A / BA-B continuations, longest run three, five moves
        self.assertEqual(alternating_moves(ababa), (3, 3, 5))

        stationary = self._wire("stationary.wire.jsonl", [self._A] * 6)
        # a stationary run collapses to one confirmed move, no alternation
        self.assertEqual(alternating_moves(stationary), (0, 0, 1))

        abc = self._wire("abc.wire.jsonl", [self._A, self._B, self._C] * 2)
        # period-3 is not a period-2 alternation: zero, but six confirmed moves
        self.assertEqual(alternating_moves(abc), (0, 0, 6))

    def test_exact_alternation_share(self):
        ababa = self._wire("ababa.wire.jsonl", [self._A, self._B] * 2 + [self._A])
        self.assertEqual(alternation_share(ababa), (1.0, 3))

        abc = self._wire("abc.wire.jsonl", [self._A, self._B, self._C] * 2)
        self.assertEqual(alternation_share(abc), (0.0, 0))

        stationary = self._wire("stationary.wire.jsonl", [self._A] * 6)
        self.assertEqual(alternation_share(stationary), (0.0, 0))

    def test_measurement_is_independent_of_stationary_loop_spans(self):
        # a perfect two-cell oscillation has no duplicate (hero, time) frames,
        # so the legacy stationary loop-span field is zero while alternation is
        # at its maximum -- the two quantities are genuinely different
        ababa = self._wire("ababa.wire.jsonl", [self._A, self._B] * 2 + [self._A])
        loop = M.episode_metrics(ababa)
        self.assertEqual(loop["longest_loop_span"], 0)
        self.assertEqual(loop["loop_spans_ge_2"], 0)
        self.assertEqual(alternating_moves(ababa), (3, 3, 5))

        # conversely, a stationary run shows a positive loop span yet zero
        # alternation: the alternation count is not derived from loop spans
        stationary = self._wire("stationary.wire.jsonl", [self._A] * 6)
        loop2 = M.episode_metrics(stationary)
        self.assertGreaterEqual(loop2["longest_loop_span"], 2)
        self.assertGreaterEqual(loop2["loop_spans_ge_2"], 1)
        self.assertEqual(alternating_moves(stationary), (0, 0, 1))


class LifecycleMetrics(unittest.TestCase):
    """AC15: the additive lifecycle metric definitions (plan section 5)."""

    def test_activation_and_override_execution_metrics_are_distinct(self):
        from tools.agent import lifecycle_metrics as LM
        rec = LM.LifecycleRecorder()
        # generation 1 is activated and resolved but never executes
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_ELIGIBLE, generation=1)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_RESOLVED, generation=1)
        s = rec.summarize()
        self.assertEqual(s["directive_activations"], 1)
        self.assertEqual(s["directive_executions"], 0)
        self.assertEqual(s["directive_override_execution_rate"], 0.0)
        self.assertEqual(s["directive_unresolved_or_expired_before_action"],
                         [1])
        # generation 2 executes: activation and override execution differ
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_ELIGIBLE, generation=2)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_RESOLVED, generation=2)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_FIRST_ACTION, generation=2)
        s2 = rec.summarize()
        self.assertEqual(s2["directive_activations"], 2)
        self.assertEqual(s2["directive_executions"], 1)
        self.assertEqual(s2["directive_override_execution_rate"], 0.5)
        self.assertNotEqual(s2["directive_activations"],
                            s2["directive_executions"])

    def test_pickup_attempt_and_confirmed_outcome_metrics_are_distinct(self):
        from tools.agent import lifecycle_metrics as LM
        rec = LM.LifecycleRecorder()
        rec.record(LM.KIND_PICKUP, LM.PICKUP_OFFERED, token="t1")
        rec.record(LM.KIND_PICKUP, LM.PICKUP_ATTEMPTED, token="t1")
        rec.record(LM.KIND_PICKUP, LM.PICKUP_ATTEMPTED, token="t1")
        rec.record(LM.KIND_PICKUP, LM.PICKUP_SUCCEEDED, token="t1")
        rec.record(LM.KIND_PICKUP, LM.PICKUP_NO_ITEMS, token="t2")
        s = rec.summarize()
        self.assertEqual(s["pickup_attempts"], 2)
        self.assertEqual(s["pickup_repeated_sites"], 1)
        self.assertEqual(s["pickup_outcomes"][LM.PICKUP_SUCCEEDED], 1)
        self.assertEqual(s["pickup_outcomes"][LM.PICKUP_NO_ITEMS], 1)
        # a pickup *attempt* is not a confirmed *outcome*
        self.assertNotEqual(s["pickup_attempts"],
                            s["pickup_outcomes"][LM.PICKUP_SUCCEEDED])

    def test_override_execution_rate_uses_eligible_resolved_denominator(self):
        from tools.agent import lifecycle_metrics as LM
        rec = LM.LifecycleRecorder()
        # generation 1 is eligible but never resolves
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_ELIGIBLE, generation=1)
        # generation 2 is eligible, resolves and executes
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_ELIGIBLE, generation=2)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_RESOLVED, generation=2)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_FIRST_ACTION, generation=2)
        s = rec.summarize()
        self.assertEqual(s["directive_activations"], 2)
        # the denominator is the eligible *resolved* set, not all eligible
        self.assertEqual(s["directive_override_execution_rate"], 1.0)
        self.assertEqual(s["directive_executions"], 1)
        # the eligible-never-resolved generation is listed separately
        self.assertEqual(s["directive_unresolved_or_expired_before_action"],
                         [1])
        # an expired-before-action generation is listed too
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_ELIGIBLE, generation=3)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_RESOLVED, generation=3)
        rec.record(LM.KIND_DIRECTIVE, LM.DIR_TERMINAL, generation=3,
                   reason="expired")
        s2 = rec.summarize()
        self.assertEqual(s2["directive_override_execution_rate"], 0.5)
        self.assertEqual(s2["directive_unresolved_or_expired_before_action"],
                         [1, 3])

    def test_legacy_missing_commitment_metrics_report_unavailable(self):
        from tools.agent import lifecycle_metrics as LM
        s = LM.summarize([])
        self.assertFalse(s["available"])
        for field in ("commitment_length_median", "commitment_length_p90",
                      "terminal_reasons", "destination_switch_rate",
                      "directive_activations", "directive_executions",
                      "directive_override_execution_rate",
                      "directive_unresolved_or_expired_before_action",
                      "target_reach_rate", "pickup_outcomes",
                      "pickup_attempts", "pickup_repeated_sites",
                      "pickup_unresolved_inspections"):
            self.assertIsNone(s[field], field)
        self.assertNotEqual(s["commitment_length_median"], 0)

    def test_lifecycle_metrics_flag_incomplete_legacy_streams(self):
        from tools.agent import lifecycle_metrics as LM
        # a schema-only (legacy) stream carries no additive schema_version: its
        # metrics are reported unavailable rather than a manufactured zero
        legacy = [{"schema": 1, "kind": LM.KIND_DESTINATION,
                   "outcome": LM.DEST_ACQUIRED, "serial": 1}]
        s = LM.summarize(legacy)
        self.assertTrue(s["legacy_stream"])
        self.assertFalse(s["available"])
        self.assertIsNone(s["destination_switch_rate"])
        # a current stream is not flagged and carries the additive version
        cur = LM.LifecycleRecorder()
        cur.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=1)
        s2 = cur.summarize()
        self.assertFalse(s2["legacy_stream"])
        self.assertEqual(s2["schema_version"], LM.SCHEMA_VERSION)
        self.assertTrue(s2["available"])

    def test_commitment_length_and_reach_rate_from_a_full_stream(self):
        from tools.agent import lifecycle_metrics as LM
        rec = LM.LifecycleRecorder()
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=1)
        for _ in range(4):
            rec.record(LM.KIND_DESTINATION, LM.DEST_ACTION, serial=1)
        rec.record(LM.KIND_DESTINATION, LM.DEST_REACHED, serial=1,
                   reason="reached")
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=2)
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACTION, serial=2)
        rec.record(LM.KIND_DESTINATION, LM.DEST_FAILED, serial=2,
                   reason="stalled")
        s = rec.summarize()
        self.assertEqual(s["commitment_length_median"], 4)
        self.assertEqual(s["commitment_length_p90"], 4)
        self.assertEqual(s["target_reach_rate"], 0.5)
        self.assertEqual(s["terminal_reasons"], {"reached": 1, "stalled": 1})
        self.assertEqual(s["destination_switch_rate"], 0.0)


class LifecycleRecording(unittest.TestCase):
    """AC15: the live controller and the evaluator persist the same events."""

    def _drivers(self):
        import test_auto_navigation as nav
        from tools.agent import controller, evaluate, policy
        from tools.agent.providers import ProviderConfig
        live = object.__new__(controller._EpisodeRunner)
        live.reflex = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        rp = evaluate.ReplayPass([], ProviderConfig(reflex="scripted",
                                                    strategy="off"),
                                 "scripted", "off")
        out = []
        for ref in (live.reflex, rp.reflex):
            mem = nav.mem_with({(x, 10): nav.FLOOR for x in range(1, 8)},
                               (1, 10))
            out.append((ref, mem, nav))
        return out

    def _persisted_summary(self, ref, tmp):
        """Persist the recorder's stream through the real sidecar and read it.

        Returns the metric summary derived from the *produced artifact*, so an
        assertion is on the persisted file rather than on in-process state.
        """
        import glob
        from tools.agent import events, lifecycle_metrics, recording
        rec = recording.EpisodeRecorder(tmp, 1)
        for ev in ref.lifecycle.events:
            rec.record_event(events.lifecycle_event(ev))
        rec.finalize({})                      # drain the sidecar writers
        paths = glob.glob(os.path.join(tmp, "*.events.jsonl"))
        self.assertTrue(paths, "no events sidecar was written")
        return lifecycle_metrics.summarize_artifact(paths[0]), paths[0]

    def test_live_and_evaluator_persist_destination_lifecycle_events(self):
        for ref, mem, nav in self._drivers():
            cand = ref.prepare(nav.ctx(mem)).table.scripted()
            self.assertEqual(cand.effect_payload[1], "acquire")
            ref.commit_effect(cand.proposed_effect, cand.semantic_label, 1,
                              mem, observed_kind="moved",
                              payload=cand.effect_payload)
            cont = ref._dest_payload("continue", ref.targets.held())
            ref.commit_effect("navigate", "navigate", 2, mem,
                              observed_kind="moved", payload=cont)
            dest = [e["outcome"] for e in ref.lifecycle.events
                    if e["kind"] == "destination"]
            self.assertEqual(dest[:2], ["acquired", "action"])
            # additive and schema-versioned
            self.assertTrue(all("schema" in e for e in ref.lifecycle.events))
            with tempfile.TemporaryDirectory() as tmp:
                summary, path = self._persisted_summary(ref, tmp)
            # the persisted artifact yields the same non-unavailable metrics
            self.assertTrue(summary["available"])
            self.assertIsNotNone(summary["commitment_length_median"])
            self.assertEqual(summary["destination_switch_rate"], 0.0)

    def test_live_and_evaluator_persist_pickup_lifecycle_events(self):
        from tools.agent.directives import DirectiveSet, DirectiveView
        for ref, mem, nav in self._drivers():
            ref.floor.observe_item(ref.instance_id, (1, 10), "coin appearance")
            dset = DirectiveSet(schema_version=2, goals=("collect_items",),
                                target=(1, 10))
            table = ref.prepare(nav.ctx(mem, directives=[
                DirectiveView(dset, 1)])).table
            cand = table.scripted()
            self.assertEqual(cand.semantic_label, "pick-up")
            ref.arm_pickup(cand.effect_payload)      # the send boundary
            mem.messages.append("There is nothing here to pick up.")
            ref.note_observation(mem)                # the result
            events = [(e["kind"], e["outcome"]) for e in ref.lifecycle.events]
            self.assertIn(("pickup", "offered"), events)
            self.assertIn(("pickup", "attempted"), events)
            self.assertIn(("pickup", "no-items"), events)
            with tempfile.TemporaryDirectory() as tmp:
                summary, _path = self._persisted_summary(ref, tmp)
            self.assertTrue(summary["available"])
            self.assertEqual(summary["pickup_attempts"], 1)
            self.assertEqual(summary["pickup_outcomes"]["no-items"], 1)

    def _pickup_attempt(self, rp):
        """Arm a real on-square collection pickup attempt on *rp*."""
        import test_auto_navigation as nav
        from tools.agent.directives import DirectiveSet, DirectiveView
        mem = nav.mem_with({(x, 10): nav.FLOOR for x in range(1, 8)}, (1, 10))
        ev = rp.reflex.floor.observe_item(rp.reflex.instance_id, (1, 10),
                                         "coin appearance")
        dset = DirectiveSet(schema_version=2, goals=("collect_items",),
                            target=(1, 10))
        cand = rp.reflex.prepare(nav.ctx(mem, directives=[
            DirectiveView(dset, 1)])).table.scripted()
        self.assertTrue(rp.reflex.arm_pickup(cand.effect_payload,
                                             identity=("pickup", 99), tick=2))
        return mem, ev, cand

    def test_evaluator_invalid_cancels_the_rejected_pickup_freeze(self):
        """Round-5: the evaluator mirrors live invalid ownership (plan 3.3)."""
        from tools.agent import evaluate
        from tools.agent.providers import ProviderConfig
        rp = evaluate.ReplayPass([], ProviderConfig(reflex="scripted",
                                                    strategy="off"),
                                 "scripted", "off")
        mem, ev, cand = self._pickup_attempt(rp)
        self.assertIsNotNone(rp.reflex.pickup_pending)
        self.assertIsNotNone(rp.reflex.targets.held())   # on-square acquired
        rp._pending_effect = ("pickup", "pick-up", tuple(cand.effect_payload))
        rp._last_key = None
        rp._last_need_kind = "command"
        before = len(rp.reflex.lifecycle.events)
        rp._on_invalid({"code": "kind"})
        # the rejected freeze is gone before any observation
        self.assertIsNone(rp.reflex.pickup_pending)
        self.assertIsNone(rp.reflex.pickup_attempt_identity)
        self.assertIsNone(rp._pending_effect)
        # ... so a following observation attributes nothing to it
        rp.tick = 3
        rp.reflex.note_observation(mem)
        self.assertIsNone(rp.reflex.floor.outcome(ev))
        outcomes = [e["outcome"] for e in rp.reflex.lifecycle.events[before:]]
        self.assertNotIn("reached", outcomes)
        self.assertNotIn("failed", outcomes)             # no terminal event

    def test_evaluator_retry_pickup_arms_a_fresh_identity(self):
        from tools.agent import candidates, evaluate
        from tools.agent.providers import ProviderConfig
        rp = evaluate.ReplayPass([], ProviderConfig(reflex="scripted",
                                                    strategy="off"),
                                 "scripted", "off")
        _mem, _ev, cand = self._pickup_attempt(rp)
        retry = candidates.candidate_to_wire(cand)

        class _Idx(object):
            def accepted(self, key, n):
                return None if n == 0 else retry      # rejected, then retry

        rp.actions_index = _Idx()
        rp._last_key = ("k", 1, 1)
        rp._last_need_kind = "command"
        rp.reflex.last_candidate = cand               # the retry matches it
        rp._on_invalid({"code": "kind"})
        # the retry pickup action armed a *fresh* identity, not the rejected one
        self.assertIsNotNone(rp.reflex.pickup_pending)
        self.assertNotEqual(rp.reflex.pickup_attempt_identity, ("pickup", 99))
        self.assertEqual(rp.reflex.pickup_attempt_identity,
                         ("pickup", rp._sent_ordinal))

    def test_live_and_evaluator_invalid_cancel_parity(self):
        """The same invalid leaves identical reflexive pickup state."""
        from tools.agent import controller, evaluate, policy
        from tools.agent.providers import ProviderConfig
        # live
        live = object.__new__(controller._EpisodeRunner)
        live.reflex = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        rp = evaluate.ReplayPass([], ProviderConfig(reflex="scripted",
                                                    strategy="off"),
                                 "scripted", "off")
        for ref, run in ((live.reflex, None), (rp.reflex, rp)):
            _mem, _ev, _cand = None, None, None
            mem = self._arm_on(ref)
            if run is not None:
                run._pending_effect = None
                run._last_key = None
                run._last_need_kind = "command"
                run._on_invalid({"code": "kind"})
            else:
                ref.cancel_pickup()
            self.assertIsNone(ref.pickup_pending)
            self.assertIsNone(ref.pickup_attempt_identity)
            self.assertEqual(ref.intent, "")
            self.assertEqual(ref.targets.held().phase, "interacting")

    def _arm_on(self, ref):
        import test_auto_navigation as nav
        from tools.agent.directives import DirectiveSet, DirectiveView
        mem = nav.mem_with({(x, 10): nav.FLOOR for x in range(1, 8)}, (1, 10))
        ref.floor.observe_item(ref.instance_id, (1, 10), "coin appearance")
        dset = DirectiveSet(schema_version=2, goals=("collect_items",),
                            target=(1, 10))
        cand = ref.prepare(nav.ctx(mem, directives=[
            DirectiveView(dset, 1)])).table.scripted()
        ref.arm_pickup(cand.effect_payload, identity=("pickup", 1), tick=2)
        return mem

    def test_over_cap_lifecycle_stream_persists_completely(self):
        # an episode emitting more events than the retained window must still
        # persist every one of them in the sidecar (incremental sink, no
        # truncation, no duplicates)
        import glob
        from tools.agent import events, lifecycle_metrics, policy, recording
        from tools.agent.providers import ProviderConfig
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        total = ref.lifecycle.EVENT_CAP + 100
        with tempfile.TemporaryDirectory() as tmp:
            rec = recording.EpisodeRecorder(tmp, 1)
            ref.lifecycle.sink = lambda ev: rec.record_event(
                events.lifecycle_event(ev))          # the real sidecar writer
            for _ in range(total):
                ref.lifecycle.record(lifecycle_metrics.KIND_DESTINATION,
                                     lifecycle_metrics.DEST_ACTION, serial=1)
            # the incremental sink emitted everything: nothing is replayed
            self.assertEqual(ref.lifecycle.drain_pending(), [])
            rec.finalize({})
            paths = glob.glob(os.path.join(tmp, "*.events.jsonl"))
            self.assertTrue(paths, "no events sidecar was written")
            with open(paths[0]) as fh:
                lines = [json.loads(ln) for ln in fh if ln.strip()]
        persisted = [ln for ln in lines if ln.get("record") == "lifecycle"]
        # every emitted event survived into the artifact, even past the cap
        self.assertEqual(len(persisted), total)
        # the in-memory window is bounded...
        self.assertEqual(len(ref.lifecycle.events), ref.lifecycle.EVENT_CAP)
        # ... but the metrics derive from the complete persisted stream
        summary = lifecycle_metrics.summarize(
            lifecycle_metrics.load_events(persisted))
        self.assertTrue(summary["available"])
        self.assertEqual(summary["destination_switch_rate"], 0.0)

    def test_legacy_sidecar_without_lifecycle_events_reads_unavailable(self):
        from tools.agent import lifecycle_metrics
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ep-1.events.jsonl")
            lines = [
                {"schema": 1, "record": "boundary", "eid": "b1",
                 "terminal": {"state": "applied"}},
                {"schema": 1, "record": "directive", "state": "applied"},
            ]
            with open(path, "w") as fh:
                for rec in lines:
                    fh.write(json.dumps(rec) + "\n")
            summary = lifecycle_metrics.summarize_artifact(path)
        # an artifact predating the field is unavailable, never a zero
        self.assertFalse(summary["available"])
        self.assertIsNone(summary["commitment_length_median"])
        self.assertIsNone(summary["pickup_outcomes"])
        self.assertIsNone(summary["directive_override_execution_rate"])
        self.assertIsNone(lifecycle_metrics.summarize_artifact(
            os.path.join(tmp, "missing.events.jsonl"))["target_reach_rate"])


class Phase4Reports(unittest.TestCase):
    """AC9: stratified consultation/acceptance reporting and clean coverage."""

    _PAL = [[0, " ", "none", 0, "none"], [1, "@", "white", 0, "none"],
            [2, ".", "gray", 0, "none"]]

    def _wire(self, path, frames, closed_after=None):
        from test_auto import obs
        with open(path, "w") as fh:
            for i, (hero, t) in enumerate(frames, start=1):
                rec = obs(i, None,
                          map_=[[hero[0], hero[1], 1],
                                [hero[0] + 1, hero[1], 2]], pal=self._PAL)
                rec["s"] = {"time": {"text": str(t)}}
                fh.write(json.dumps(rec) + "\n")
                if closed_after is not None and i == closed_after:
                    fh.write(json.dumps({"type": "closed"}) + "\n")

    def test_confidence_report_stratifies_binary_ternary_singleton_timeout(
            self):
        from tools.agent import exploration_metrics as E
        recs = [
            {"record": "need", "need": {"seq": 1, "id": 1, "kind": "command"},
             "provider": "jev", "reason": "jev choice: navigate"},
            {"record": "need", "need": {"seq": 2, "id": 2, "kind": "command"},
             "provider": "scripted", "reason": "jev rejected: confidence"},
            {"record": "need", "need": {"seq": 3, "id": 3, "kind": "menu"},
             "provider": "scripted", "reason": "jev skipped: singleton"},
            {"record": "need", "need": {"seq": 4, "id": 4, "kind": "command"},
             "provider": "scripted", "reason": "jev paid-reflex cap reached"},
            {"record": "need", "need": {"seq": 5, "id": 5, "kind": "command"},
             "provider": "scripted", "reason": "jev fallback: deadline exceeded"},
        ]
        n = {(1, 1): 2, (2, 2): 3, (3, 3): 1, (4, 4): 4, (5, 5): 2}
        rep = E.confidence_report(recs, n_by_key=n)
        # accepted / rejected / skipped-singleton / cap-unavailable / timeout
        # are each enumerated by phase and N -- a singleton bypass and a spent
        # cap are never conflated with a genuine confidence rejection
        self.assertEqual(rep["command|n=2|accepted"], 1)
        self.assertEqual(rep["command|n=3|rejected"], 1)
        self.assertEqual(rep["menu|n=1|skipped-singleton"], 1)
        self.assertEqual(rep["command|n=4|cap-unavailable"], 1)
        self.assertEqual(rep["command|n=2|timeout"], 1)
        self.assertEqual(set(E.CONSULTATION_OUTCOMES),
                         {"accepted", "rejected", "skipped-singleton",
                          "skipped-unsupported", "cap-unavailable",
                          "unavailable", "timeout", "scripted", "other"})

    def test_report_attempts_vs_time_advances_and_stationary_span(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.wire.jsonl")
            self._wire(p, [((5, 5), 100), ((6, 5), 100), ((7, 5), 101),
                           ((7, 5), 101), ((7, 5), 101)])
            m = M.episode_metrics(p)
        # two hero moves are two attempts; only one displayed-time advance
        self.assertEqual(m["attempts"], 2)
        self.assertEqual(m["time_advances"], 1)
        self.assertEqual(m["stationary_span_max"], 2)

    def test_report_coverage_excludes_teardown(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.wire.jsonl")
            self._wire(p, [((5, 5), 100), ((6, 5), 101), ((7, 5), 102)],
                       closed_after=2)
            m = M.episode_metrics(p)
        # the post-close teardown frame is counted but never folded into
        # coverage or the stationary span
        self.assertEqual(m["observations"], 2)
        self.assertEqual(m["teardown_frames_excluded"], 1)
        self.assertEqual(m["entered_cells_instance_scoped"], 2)

    def test_report_terminal_completeness_and_serviced_reopens(self):
        from tools.agent import lifecycle_metrics as LM
        rec = LM.LifecycleRecorder()
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=1)
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACTION, serial=1)
        rec.record(LM.KIND_DESTINATION, LM.DEST_REACHED, serial=1,
                   reason="reached")
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=2)  # open
        rec.record(LM.KIND_DESTINATION, LM.DEST_ACQUIRED, serial=3)
        rec.record(LM.KIND_DESTINATION, LM.DEST_REPLACED, serial=3,
                   reason="replaced", replacement_serial=4)
        s = rec.summarize()
        # three acquired serials; 1 and 3 carry a terminal, 2 does not
        self.assertAlmostEqual(s["terminal_completeness"], 2.0 / 3.0)
        self.assertEqual(s["serviced_reopens"], 1)


class ValidationReport(unittest.TestCase):
    """AC16/AC18: the validation report carries every required field."""

    def test_validation_report_contains_required_fields(self):
        import mutation_checks
        import pickup_shapes
        self.assertTrue(os.path.exists(mutation_checks.REPORT_PATH),
                        mutation_checks.REPORT_PATH)
        with open(mutation_checks.REPORT_PATH) as fh:
            report = json.load(fh)
        for field in mutation_checks.REQUIRED_FIELDS:
            self.assertIn(field, report)
        # commit/config identifiers
        self.assertTrue(report["commits"])
        for c in report["commits"]:
            self.assertTrue(c.get("hash") and c.get("subject"))
        # suite and named-mutation results
        self.assertTrue(report["gates"])
        names = {m["name"] for m in report["named_mutations"]}
        for m in list(mutation_checks.MUTATIONS) \
                + list(mutation_checks.NOT_PERFORMED):
            self.assertIn(m["name"], names)
        # the native-vs-manual pickup-shape disposition is exhaustive
        disp = report["pickup_shape_disposition"]
        pickup_shapes.validate_partition(disp["native_passed"],
                                         disp["manual_required"])
        # live claims are explicitly labeled, never inferred
        self.assertFalse(report["live_claims"]["measured"])
        self.assertIn("note", report["live_claims"])


class PresentationV3Metadata(unittest.TestCase):
    """AC13: the `/3` presentation version is metadata only."""

    def test_presentation_v3_recorded_only_in_allowlisted_metadata(self):
        from tools.agent import presentation, providers
        self.assertEqual(presentation.PRESENTATION_VERSION, "jev-presentation/3")
        self.assertEqual(providers.JEV_PRESENTATION_VERSION,
                         presentation.PRESENTATION_VERSION)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "jev_golden_request.json")
        with open(path) as fh:
            golden = json.load(fh)
        self.assertEqual(golden["capture"]["presentation_version"],
                         "jev-presentation/3")
        self.assertIs(golden["capture"]["dirty"], False)
        # the version is allowlisted metadata, never a wire field
        self.assertNotIn("jev-presentation/3",
                         json.dumps(golden["request_body"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
