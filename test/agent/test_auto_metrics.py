#!/usr/bin/env python3
"""Tests for the streaming exploration metrics (section 10.2).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Uses the bounded replay fixture under ``test/agent/fixtures/auto`` so the
metrics run against a real recording, not a hand-built dict.
"""

import os
import sys
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
