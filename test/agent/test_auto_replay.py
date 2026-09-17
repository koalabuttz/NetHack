#!/usr/bin/env python3
"""Replay and evaluation tests for the offline evaluator.

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

These drive :mod:`tools.agent.evaluate` over the bounded committed fixtures in
``test/agent/fixtures/auto``.  They assert the *deterministic structure* of a
replay -- coverage, agreement, action labels, legal fallbacks -- and the
offline guarantee, rather than a golden transcript that a legitimate policy
change would invalidate.
"""

import contextlib
import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import evaluate  # noqa: E402
from tools.agent.providers import ProviderConfig  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures", "auto")
STARTUP = os.path.join(_FIX, "startup.wire.jsonl")
LEGACY = os.path.join(_FIX, "legacy-ep3.wire.jsonl")
SHORT_WIRE = os.path.join(_FIX, "short.wire.jsonl")
SHORT_ACTIONS = os.path.join(_FIX, "short.actions.jsonl")
SHORT_DECISIONS = os.path.join(_FIX, "short.decisions.jsonl")

# The short recording was captured with this tick cap; a replay must use the
# matching value or the scripted tick-cap quit diverges (see the fixture
# README and ShortFixtureTest.test_max_ticks_must_match).
SHORT_MAX_TICKS = 30

_BUDGET_BYTES = 256 * 1024


def _read(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _wire_lines(path):
    with open(path, "rb") as fh:
        return fh.readlines()


def _run(argv):
    """Run the evaluator CLI, swallowing its stdout summary."""
    with contextlib.redirect_stdout(io.StringIO()):
        return evaluate.main(argv)


# --------------------------------------------------------------- semantics

class CanonicalActionTest(unittest.TestCase):
    """Agreement compares action *semantics*, never JSON bytes."""

    def test_menu_final_set_ignores_count_and_generation(self):
        need = {"kind": "menu", "menu": "m7"}
        a = {"menu": "m7", "commit": [[14, -1]]}
        b = {"menu": "m7", "commit": [[14, 1]]}       # same single row
        self.assertEqual(evaluate.canonical_action(need, a),
                         evaluate.canonical_action(need, b))

    def test_menu_final_set_is_order_insensitive(self):
        need = {"kind": "menu", "menu": "m7"}
        a = {"menu": "m7", "commit": [[3, -1], [1, -1]]}
        b = {"menu": "m7", "commit": [[1, -1], [3, -1]]}
        self.assertEqual(evaluate.canonical_action(need, a),
                         evaluate.canonical_action(need, b))

    def test_different_rows_disagree(self):
        need = {"kind": "menu", "menu": "m7"}
        a = {"menu": "m7", "commit": [[3, -1]]}
        b = {"menu": "m7", "commit": [[4, -1]]}
        self.assertNotEqual(evaluate.canonical_action(need, a),
                            evaluate.canonical_action(need, b))

    def test_shapes_are_distinct(self):
        need = {"kind": "command"}
        self.assertNotEqual(evaluate.canonical_action(need, {"cancel": True}),
                            evaluate.canonical_action(need, {"ack": True}))
        self.assertNotEqual(evaluate.canonical_action(need, {"key": 104}),
                            evaluate.canonical_action(need, {"key": 106}))
        self.assertEqual(evaluate.canonical_action(need, {"key": 104}),
                         evaluate.canonical_action(need, {"key": 104}))


# --------------------------------------------------------- offline guarantee

class OfflineGuaranteeTest(unittest.TestCase):
    def test_module_makes_no_direct_network_imports(self):
        with open(evaluate.__file__) as fh:
            src = fh.read()
        for needle in ("import socket", "import urllib", "import requests",
                       "http.client", "from urllib", "socket."):
            self.assertNotIn(needle, src,
                             "evaluate.py must not reach the network "
                             "directly (%r)" % needle)

    def test_default_path_opens_no_socket(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            boom = mock.Mock(side_effect=AssertionError("network attempted"))
            with mock.patch("socket.socket", boom), \
                    mock.patch("socket.getaddrinfo", boom):
                rc = _run([STARTUP, "--reflex", "scripted",
                           "--strategy", "off", "--output", out])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))

    def test_deepseek_requires_network_opt_in(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            rc = _run([STARTUP, "--strategy", "deepseek", "--output", out])
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(out))


# ------------------------------------------------------------ startup fixture

class StartupFixtureTest(unittest.TestCase):
    def test_covers_startup_and_is_deterministic(self):
        lines = _wire_lines(STARTUP)
        cfg = ProviderConfig(reflex="scripted", strategy="off")
        a = evaluate.ReplayPass(lines, cfg, "scripted", "off")
        a.run()
        b = evaluate.ReplayPass(lines, cfg, "scripted", "off")
        b.run()
        self.assertEqual(a.decisions, b.decisions)
        self.assertEqual(a.event_records_clean(), b.event_records_clean())
        self.assertTrue(a.closed or a.eof)
        self.assertEqual(a.answered, a.needs)
        self.assertGreaterEqual(a.needs, 8)
        kinds = {d["need"]["kind"] for d in a.decisions
                 if d.get("record") == "need"}
        # selection (yn), a menu, an ack and gameplay commands
        self.assertIn("yn", kinds)
        self.assertIn("menu", kinds)
        self.assertIn("ack", kinds)
        self.assertIn("command", kinds)

    def test_no_sidecar_labels_every_action_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(_run([STARTUP, "--reflex", "scripted",
                                   "--strategy", "off", "--output", out]), 0)
            records = _read(out)
        needs = [r for r in records if r.get("record") == "need"]
        self.assertTrue(needs)
        for r in needs:
            self.assertEqual(r["actual_action"], None)
            self.assertEqual(r["actual_action_source"], "unknown")
            self.assertIsNone(r["agreement"])
        summary = [r for r in records if r.get("record") == "summary"][-1]
        self.assertEqual(summary["actual_known"], 0)
        self.assertEqual(summary["agreement"]["scripted"]["total"], 0)

    def test_replay_is_byte_identical_across_runs(self):
        with tempfile.TemporaryDirectory() as d:
            o1 = os.path.join(d, "a.jsonl")
            o2 = os.path.join(d, "b.jsonl")
            for out in (o1, o2):
                self.assertEqual(_run([STARTUP, "--reflex", "scripted",
                                       "--strategy", "off",
                                       "--output", out]), 0)
            with open(o1, "rb") as fh1, open(o2, "rb") as fh2:
                self.assertEqual(fh1.read(), fh2.read())


# ------------------------------------------------------------- legacy fixture

class LegacyFixtureTest(unittest.TestCase):
    def test_legacy_excerpt_labels_actions_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(_run([LEGACY, "--reflex", "scripted",
                                   "--strategy", "off", "--output", out]), 0)
            records = _read(out)
        summary = [r for r in records if r.get("record") == "summary"][-1]
        self.assertEqual(summary["actual_known"], 0)
        self.assertGreaterEqual(summary["needs_answered"], 1)
        for r in records:
            if r.get("record") == "need":
                self.assertEqual(r["actual_action_source"], "unknown")


# -------------------------------------------------------------- short fixture

class ShortFixtureTest(unittest.TestCase):
    def _eval(self, out, max_ticks=SHORT_MAX_TICKS, with_actions=True):
        argv = [SHORT_WIRE, "--reflex", "scripted", "--strategy", "off",
                "--max-ticks", str(max_ticks), "--output", out]
        if with_actions:
            argv += ["--actions", SHORT_ACTIONS,
                     "--decisions", SHORT_DECISIONS]
        return _run(argv)

    def test_ground_truth_agreement_is_total(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(self._eval(out), 0)
            records = _read(out)
        summary = [r for r in records if r.get("record") == "summary"][-1]
        ag = summary["agreement"]["scripted"]
        self.assertEqual(summary["actual_known"], summary["needs_answered"])
        self.assertGreater(summary["needs_answered"], 20)
        self.assertEqual(ag["total"], summary["needs_answered"])
        self.assertEqual(ag["agree"], ag["total"],
                         "the scripted replay must reproduce the scripted "
                         "recording exactly")
        self.assertEqual(ag["rate"], 1.0)
        self.assertEqual(summary["legality"]["scripted"]["rate"], 1.0)
        self.assertEqual(summary["provider_fallbacks"]["scripted"], 0)

    def test_max_ticks_must_match_the_recording(self):
        # A different tick cap changes the scripted quit, so agreement is no
        # longer total: the evaluator must be configured like the recorder.
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(self._eval(out, max_ticks=2000), 0)
            summary = [r for r in _read(out)
                       if r.get("record") == "summary"][-1]
        ag = summary["agreement"]["scripted"]
        self.assertLess(ag["agree"], ag["total"])

    def test_replay_matches_recorded_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(self._eval(out), 0)
            records = _read(out)
        agreed = [r for r in records
                  if r.get("record") == "need" and "recorded" in r]
        self.assertTrue(agreed)
        for r in agreed:
            self.assertTrue(r["recorded"]["agreement"],
                            "selection disagrees with the recorded decision: "
                            "%r" % (r["need"],))


# ---------------------------------------------------------- provider compare

class ProviderCompareTest(unittest.TestCase):
    def test_jev_offline_is_a_scripted_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            self.assertEqual(_run([SHORT_WIRE, "--reflex", "scripted",
                                   "--provider", "jev",
                                   "--strategy", "off",
                                   "--max-ticks", str(SHORT_MAX_TICKS),
                                   "--actions", SHORT_ACTIONS,
                                   "--output", out]), 0)
            records = _read(out)
        summary = [r for r in records if r.get("record") == "summary"][-1]
        self.assertIn("jev", summary["providers"])
        # every jev candidate falls back to scripted offline
        self.assertEqual(summary["provider_fallbacks"]["jev"],
                         summary["needs_answered"])
        # but it still agrees with the recorded scripted trajectory
        self.assertEqual(summary["agreement"]["jev"]["total"],
                         summary["needs_answered"])
        self.assertEqual(summary["agreement"]["jev"]["agree"],
                         summary["needs_answered"])
        flagged = [r for r in records if r.get("record") == "need"
                   and r.get("candidates", {}).get("jev")]
        self.assertTrue(flagged)
        self.assertTrue(all(r["candidates"]["jev"]["fallback"]
                            for r in flagged))


# ------------------------------------------------------------- fixture budget

class FixtureIntegrityTest(unittest.TestCase):
    # sha256 recorded in fixtures/auto/README.md (integrity of committed
    # snapshots; the upstream ephemeral recordings are not in the repo)
    EXPECTED = {
        "startup.wire.jsonl": (
          "60a7572334ca58ea06c09c39d2c7c998b1b7ff18993123e7ff141174de93e6ca"),
        "short.wire.jsonl": (
          "02d779f44dbda793d9c7945da00d6b56b7fe66ea5cf422def4583fa3e78114b2"),
        "short.actions.jsonl": (
          "9d5011acb959664803138571f65adb7a331206038eb84bbc91cc6fde89021384"),
        "short.decisions.jsonl": (
          "4e0b33f38cc05f2fc13552bd0cb25bd7a560c191228e8d417c58001133730a7e"),
        "legacy-ep3.wire.jsonl": (
          "f59639a98d043c95c315cf4ab2bd93aa1fb656e7bb150e30a5dbf3294f4d00e7"),
    }

    def test_total_budget_is_bounded(self):
        total = 0
        for name in os.listdir(_FIX):
            total += os.path.getsize(os.path.join(_FIX, name))
        self.assertLessEqual(total, _BUDGET_BYTES,
                             "fixture corpus exceeds the 256 KiB budget")

    def test_committed_hashes_match_readme(self):
        with open(os.path.join(_FIX, "README.md")) as fh:
            readme = fh.read()
        for name, digest in self.EXPECTED.items():
            path = os.path.join(_FIX, name)
            with open(path, "rb") as fh:
                got = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(got, digest, "%s hash drifted" % name)
            self.assertIn(digest, readme,
                          "%s hash missing from the fixture README" % name)

    def test_all_expected_files_present(self):
        for name in self.EXPECTED:
            self.assertTrue(os.path.exists(os.path.join(_FIX, name)), name)


class CampaignSummaryTest(unittest.TestCase):
    """The compact campaign.json rollup is written next to the recordings."""

    def _result(self, idx, **kw):
        from tools.agent.controller import EpisodeResult
        r = EpisodeResult(index=idx)
        r.spawn_ok = kw.get("spawn_ok", True)
        r.closed = kw.get("closed", True)
        r.returncode = kw.get("returncode", 0)
        r.recording_complete = kw.get("recording_complete", True)
        r.ticks = kw.get("ticks", 10)
        r.needs = kw.get("needs", 20)
        r.actions = kw.get("actions", 25)
        r.boundaries = kw.get("boundaries", 3)
        r.strategy_calls = kw.get("strategy_calls", 2)
        r.directives_applied = kw.get("directives_applied", 2)
        r.budget = {"usage": {"prompt_tokens": 100, "completion_tokens": 40,
                              "estimated_usd": 0.0, "unknown_price_calls": 0}}
        return r

    def test_rollup_counts_and_totals(self):
        from tools.agent import controller
        results = [self._result(1), self._result(2, closed=False,
                                                 returncode=1)]
        summary = controller.campaign_summary(
            results, ProviderConfig(), 300.0)
        self.assertEqual(summary["episodes"], 2)
        self.assertEqual(summary["episodes_success"], 1)
        self.assertEqual(summary["episodes_failed"], 1)
        self.assertEqual(summary["totals"]["ticks"], 20)
        self.assertEqual(summary["totals"]["needs"], 40)
        self.assertEqual(summary["totals"]["strategy_calls"], 4)
        self.assertEqual(summary["totals"]["prompt_tokens"], 200)
        # no secrets, only the allowlisted config
        self.assertIn("config", summary)
        self.assertNotIn("deepseek_key_file", summary["config"])

    def test_run_campaign_writes_campaign_json(self):
        from tools.agent import controller
        with tempfile.TemporaryDirectory() as d:
            path = controller.write_campaign_summary(
                os.path.join(d, "out"), [self._result(1)],
                ProviderConfig(), 300.0)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
            with open(path) as fh:
                data = json.load(fh)
            self.assertEqual(data["episodes"], 1)
            self.assertEqual(data["results"][0]["index"], 1)


if __name__ == "__main__":
    unittest.main()
