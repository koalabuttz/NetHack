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


if __name__ == "__main__":
    unittest.main(verbosity=2)
