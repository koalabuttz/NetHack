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
from test_auto import (CLOSED, HELLO, WireHarness, _line, obs,  # noqa: E402
                       obs_menu, page, row)

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

    NEED = {"kind": "menu", "menu": "m7"}
    # Delivered page rows: row 14 is a single item (no count prefix, no
    # positive stack count); row 7 displays a two-item stack; row 5 carries a
    # positive stack count with no text prefix.
    ROWS = [
        {"r": 14, "text": "a Valkyrie", "initial": None},
        {"r": 7, "text": "2 uncursed food rations", "initial": None},
        {"r": 5, "text": "uncursed scrolls", "initial": 3},
    ]

    def test_menu_generation_id_is_ignored(self):
        a = {"menu": "m7", "commit": [[14, -1]]}
        b = {"menu": "m42", "commit": [[14, -1]]}    # different generation
        self.assertEqual(evaluate.canonical_action(self.NEED, a),
                         evaluate.canonical_action(self.NEED, b))

    def test_proven_single_item_counts_normalize(self):
        # delivered metadata proves row 14 is one item: -1 and 1 agree
        a = {"menu": "m7", "commit": [[14, -1]]}
        b = {"menu": "m7", "commit": [[14, 1]]}
        self.assertEqual(evaluate.canonical_action(self.NEED, a, self.ROWS),
                         evaluate.canonical_action(self.NEED, b, self.ROWS))

    def test_counts_are_preserved_without_metadata(self):
        # no delivered metadata: -1 vs 1 cannot be proven equal -> disagree
        a = {"menu": "m7", "commit": [[14, -1]]}
        b = {"menu": "m7", "commit": [[14, 1]]}
        self.assertNotEqual(evaluate.canonical_action(self.NEED, a),
                            evaluate.canonical_action(self.NEED, b))

    def test_different_counts_disagree(self):
        a = {"menu": "m7", "commit": [[14, 1]]}
        b = {"menu": "m7", "commit": [[14, 2]]}
        ca = evaluate.canonical_action(self.NEED, a, self.ROWS)
        cb = evaluate.canonical_action(self.NEED, b, self.ROWS)
        self.assertNotEqual(ca, cb)

    def test_stack_counts_stay_distinct(self):
        # row 7 is a two-item stack (-1 = whole stack, 1 = one item) ...
        a = {"menu": "m7", "commit": [[7, -1]]}
        b = {"menu": "m7", "commit": [[7, 1]]}
        ca = evaluate.canonical_action(self.NEED, a, self.ROWS)
        cb = evaluate.canonical_action(self.NEED, b, self.ROWS)
        self.assertNotEqual(ca, cb)
        # ... and a positive declared stack count also proves a stack
        c = {"menu": "m7", "commit": [[5, -1]]}
        d = {"menu": "m7", "commit": [[5, 1]]}
        cc = evaluate.canonical_action(self.NEED, c, self.ROWS)
        cd = evaluate.canonical_action(self.NEED, d, self.ROWS)
        self.assertNotEqual(cc, cd)

    def test_menu_final_set_is_order_insensitive(self):
        a = {"menu": "m7", "commit": [[3, -1], [1, -1]]}
        b = {"menu": "m7", "commit": [[1, -1], [3, -1]]}
        self.assertEqual(evaluate.canonical_action(self.NEED, a),
                         evaluate.canonical_action(self.NEED, b))

    def test_different_rows_disagree(self):
        a = {"menu": "m7", "commit": [[3, -1]]}
        b = {"menu": "m7", "commit": [[4, -1]]}
        self.assertNotEqual(evaluate.canonical_action(self.NEED, a),
                            evaluate.canonical_action(self.NEED, b))

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


# --------------------------------------------------- validation authority

class EvaluatorValidationTest(unittest.TestCase):
    """Low 3: the evaluator shares the validation authority with live play.

    An out-of-range continuity setting, a zero byte ceiling or a malformed
    tariff is rejected with exit 2 and a concise stderr message *before* any
    pass is constructed or output written -- mirroring live autoplay.
    """

    def _reject(self, *flags):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = _run([STARTUP, "--reflex", "scripted",
                           "--strategy", "off"] + list(flags)
                          + ["--output", out])
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(out))
            return err.getvalue()

    def test_out_of_range_history_pairs_is_rejected(self):
        for bad in ("-1", "65"):
            with self.subTest(bad=bad):
                err = self._reject("--deepseek-history-pairs", bad)
                self.assertIn("deepseek-history-pairs", err)

    def test_zero_context_ceiling_is_rejected(self):
        err = self._reject("--deepseek-context-max-bytes", "0")
        self.assertIn("deepseek-context-max-bytes", err)

    def test_malformed_tariff_combo_is_rejected(self):
        err = self._reject("--deepseek-price-in", "1",
                           "--deepseek-price-cache-hit", "2")
        self.assertIn("cache-hit", err)

    def test_a_partial_tariff_is_accepted(self):
        # a partial (no USD cap) tariff is a valid configuration: the run
        # proceeds offline and writes its artifact
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "e.jsonl")
            rc = _run([STARTUP, "--reflex", "scripted", "--strategy", "off",
                       "--deepseek-price-in", "1", "--output", out])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out))


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


# ----------------------------------------------- retry ground truth (M4)

class RetryGroundTruthTest(unittest.TestCase):
    """Medium 4: a rejected attempt is never the accepted ground truth."""

    def _wire(self, with_invalid=True):
        lines = [
            _line(HELLO),
            _line(obs(1, {"kind": "command", "id": 1})),
        ]
        if with_invalid:
            lines.append(_line({"v": 1, "ch": "control", "type": "invalid",
                                "d": 1, "code": "stale-id"}))
        lines.append(_line(obs(2, {"kind": "command", "id": 2})))
        lines.append(_line(CLOSED))
        return lines

    def _index(self, attempts):
        idx = evaluate._ActionsIndex()
        for a in attempts:
            idx.add((1, 1), a)
        return idx

    def _pass(self, index=None, with_invalid=True):
        cfg = ProviderConfig(reflex="scripted", strategy="off")
        p = evaluate.ReplayPass(self._wire(with_invalid=with_invalid), cfg,
                                "scripted", "off", actions_index=index)
        p.run()
        return p

    def _answered(self, p, nid):
        return [d for d in p.decisions
                if d.get("record") == "need"
                and d.get("need", {}).get("id") == nid
                and d.get("selected") is not None]

    def _other_key(self, sel):
        k = sel.get("key") if isinstance(sel, dict) else None
        return {"key": 46 if k != 46 else 47}

    def _probe(self):
        return self._answered(self._pass(), 1)[0]["selected"]

    def test_the_retry_is_the_accepted_ground_truth(self):
        sel = self._probe()
        other = self._other_key(sel)
        p = self._pass(self._index([sel, other]))
        rec = self._answered(p, 1)
        self.assertEqual(len(rec), 1)             # no duplicate need records
        d = rec[0]
        self.assertEqual(d["actual_action"], other)
        self.assertEqual(d["actual_action_source"], "sidecar")
        self.assertEqual(d["rejected_attempts"], [sel])
        self.assertEqual(d["rejected_count"], 1)
        self.assertFalse(d["agreement"],
                         "the rejected first attempt must not be the ground "
                         "truth")

    def test_each_rejected_attempt_is_labelled_at_its_invalid(self):
        sel = self._probe()
        other = self._other_key(sel)
        p = self._pass(self._index([sel, other]))
        invalids = [d for d in p.decisions
                    if str(d.get("reason", "")).startswith("invalid:")]
        self.assertEqual(len(invalids), 1)
        self.assertEqual(invalids[0]["rejected_action"], sel)
        self.assertEqual(invalids[0]["need"]["id"], 1)

    def test_single_attempt_without_invalid_is_the_ground_truth(self):
        sel = self._probe()
        p = self._pass(self._index([sel]), with_invalid=False)
        d = self._answered(p, 1)[0]
        self.assertEqual(d["actual_action"], sel)
        self.assertTrue(d["agreement"])
        self.assertEqual(d["rejected_attempts"], [])

    def test_every_attempt_rejected_leaves_the_action_unknown(self):
        sel = self._probe()
        p = self._pass(self._index([sel]))          # one attempt, rejected
        d = self._answered(p, 1)[0]
        self.assertIsNone(d["actual_action"])
        self.assertEqual(d["actual_action_source"], "unknown")
        self.assertEqual(d["rejected_attempts"], [sel])

    def test_cli_coverage_uses_the_accepted_action(self):
        sel = self._probe()
        other = self._other_key(sel)
        with tempfile.TemporaryDirectory() as d:
            wire = os.path.join(d, "ep.wire.jsonl")
            acts = os.path.join(d, "ep.actions.jsonl")
            out = os.path.join(d, "e.jsonl")
            with open(wire, "wb") as fh:
                fh.writelines(self._wire())
            with open(acts, "w") as fh:
                for a in (sel, other):
                    fh.write(json.dumps({
                        "kind": "act", "status": "sent",
                        "need": {"episode": 1, "seq": 1, "id": 1},
                        "action": a}) + "\n")
            rc = _run([wire, "--reflex", "scripted", "--strategy", "off",
                       "--actions", acts, "--output", out])
            self.assertEqual(rc, 0)
            records = _read(out)
        summary = [r for r in records if r.get("record") == "summary"][-1]
        # the accepted (second) action is the only known ground truth for the
        # retried need, and it disagrees with the scripted pick
        self.assertEqual(summary["actual_known"], 1)
        ag = summary["agreement"]["scripted"]
        self.assertEqual(ag["total"], 1)
        self.assertEqual(ag["agree"], 0)

    def test_retry_rows_do_not_shadow_the_answered_row(self):
        """Medium: rejected retry rows must not overwrite the answered row
        in the per-need merge, for comparison providers and recorded
        decisions alike."""
        sel = self._probe()
        other = self._other_key(sel)
        with tempfile.TemporaryDirectory() as d:
            wire = os.path.join(d, "ep.wire.jsonl")
            acts = os.path.join(d, "ep.actions.jsonl")
            decs = os.path.join(d, "ep.decisions.jsonl")
            out = os.path.join(d, "e.jsonl")
            with open(wire, "wb") as fh:
                fh.writelines(self._wire())
            with open(acts, "w") as fh:
                for a in (sel, other):
                    fh.write(json.dumps({
                        "kind": "act", "status": "sent",
                        "need": {"episode": 1, "seq": 1, "id": 1},
                        "action": a}) + "\n")
            with open(decs, "w") as fh:
                fh.write(json.dumps({
                    "record": "decision", "provider": "scripted",
                    "need": {"episode": 1, "seq": 1, "id": 1},
                    "selected": other}) + "\n")
            rc = _run([wire, "--reflex", "scripted", "--strategy", "off",
                       "--actions", acts, "--decisions", decs,
                       "--provider", "jev", "--output", out])
            self.assertEqual(rc, 0)
            records = _read(out)
        # two needs on this wire (id 1 rejected then re-issued as id 2):
        # both answered, neither shadowed by the rejected retry row
        answered = {r["need"]["id"]: r for r in records
                    if r.get("record") == "need"
                    and r.get("selected") is not None}
        rejected = [r for r in records if r.get("record") == "need"
                    and str(r.get("reason", "")).startswith("invalid:")]
        self.assertEqual(len(answered), 2)
        self.assertEqual(len(rejected), 1)
        a1 = answered[1]                         # the retried need
        a2 = answered[2]
        r = rejected[0]
        # (1) need 1's ground truth is the accepted (second) attempt
        self.assertEqual(a1["actual_action"], other)
        self.assertEqual(a1["actual_action_source"], "sidecar")
        # (2) the answered rows -- not the invalid row -- carry the
        # comparison candidate; need 1 also carries the recorded decision
        self.assertIn("jev", a1.get("candidates", {}))
        self.assertIn("jev", a2.get("candidates", {}))
        self.assertIn("recorded", a1)
        # the recorded decision (the accepted 46) honestly disagrees with
        # the scripted candidate's search (115) on this synthetic wire
        self.assertEqual(a1["recorded"]["selected"], other)
        self.assertFalse(a1["recorded"]["agreement"])
        self.assertNotIn("recorded", a2)         # no sidecar truth for id 2
        # (3) the rejected row keeps its own metadata, no attachments
        self.assertEqual(r["rejected_action"], sel)
        self.assertNotIn("candidates", r)
        self.assertNotIn("recorded", r)
        # (4) aggregates: one known actual (need 1), one agreement entry
        summary = [x for x in records if x.get("record") == "summary"][-1]
        self.assertEqual(summary["actual_known"], 1)
        self.assertEqual(summary["agreement"]["scripted"]["total"], 1)


# ------------------------------------------------- page strictness (L5)

class PageCollectionStrictnessTest(unittest.TestCase):
    """Low 5: page collection mirrors the live Request's strict checks."""

    ROWS = [row(14, "a Valkyrie")]

    def _wire(self, pages, declared=2, content="c10"):
        lines = [_line(HELLO),
                 _line(obs_menu(1, 10, "m10", content, "Pick",
                                pages=declared))]
        lines.extend(_line(p) for p in pages)
        lines.append(_line(CLOSED))
        return lines

    def _replay(self, pages, declared=2, content="c10"):
        cfg = ProviderConfig(reflex="scripted", strategy="off")
        p = evaluate.ReplayPass(self._wire(pages, declared=declared,
                                           content=content),
                                cfg, "scripted", "off")
        p.run()
        return p

    def test_valid_page_stream_is_accepted(self):
        p = self._replay([page("c10", 0, 2, self.ROWS),
                          page("c10", 1, 2, self.ROWS)])
        self.assertIsNone(p.protocol_failure)
        self.assertEqual(p.answered, 1)

    def test_out_of_order_page_fails(self):
        p = self._replay([page("c10", 1, 2, self.ROWS),
                          page("c10", 0, 2, self.ROWS)])
        self.assertIsNotNone(p.protocol_failure)

    def test_duplicate_page_fails(self):
        p = self._replay([page("c10", 0, 2, self.ROWS),
                          page("c10", 0, 2, self.ROWS)])
        self.assertIsNotNone(p.protocol_failure)

    def test_total_mismatch_fails(self):
        p = self._replay([page("c10", 0, 3, self.ROWS)])   # need declares 2
        self.assertIsNotNone(p.protocol_failure)

    def test_wrong_content_fails(self):
        p = self._replay([page("cOTHER", 0, 2, self.ROWS)])
        self.assertIsNotNone(p.protocol_failure)

    def test_extra_page_after_completion_fails(self):
        p = self._replay([page("c10", 0, 2, self.ROWS),
                          page("c10", 1, 2, self.ROWS),
                          page("c10", 1, 2, self.ROWS)])
        self.assertIsNotNone(p.protocol_failure)


class PageParityTest(WireHarness):
    """Low 5: the evaluator and the live controller accept/reject alike."""

    ROWS = PageCollectionStrictnessTest.ROWS

    def _cases(self):
        return [
            ("valid", [page("c10", 0, 2, self.ROWS),
                       page("c10", 1, 2, self.ROWS)], 2, "c10"),
            ("out-of-order", [page("c10", 1, 2, self.ROWS),
                              page("c10", 0, 2, self.ROWS)], 2, "c10"),
            ("duplicate", [page("c10", 0, 2, self.ROWS),
                           page("c10", 0, 2, self.ROWS)], 2, "c10"),
            ("total-mismatch", [page("c10", 0, 3, self.ROWS)], 2, "c10"),
            ("wrong-content", [page("cOTHER", 0, 2, self.ROWS)], 2, "c10"),
        ]

    def _records(self, pages, declared, content):
        recs = [_line(HELLO),
                _line(obs_menu(1, 10, "m10", content, "Pick",
                               pages=declared))]
        recs.extend(_line(p) for p in pages)
        recs.append(_line(CLOSED))
        return recs

    def test_controller_and_replay_agree(self):
        for name, pages, declared, content in self._cases():
            with self.subTest(case=name):
                recs = self._records(pages, declared, content)
                result, _actions = self.run_scenario(b"".join(recs))
                live_failed = (
                    result.protocol_failure is not None
                    or result.stop_reason == "protocol-failure")
                cfg = ProviderConfig(reflex="scripted", strategy="off")
                p = evaluate.ReplayPass(recs, cfg, "scripted", "off")
                p.run()
                replay_failed = p.protocol_failure is not None
                self.assertEqual(live_failed, replay_failed,
                                 "%s: live=%s replay=%s" %
                                 (name, live_failed, replay_failed))


# ------------------------------------------------- campaign summary (M2/L6)

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
                              "estimated_usd": 0.0, "unknown_price_calls": 0,
                              "unknown_exposure_calls": 0,
                              "unknown_exposure_tokens": 0,
                              "unknown_exposure_usd": 0.0}}
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

    def test_unknown_exposure_and_config_bounds_are_preserved(self):
        from tools.agent import controller
        reported = self._result(1)
        unreported = self._result(2)
        unreported.budget = {"usage": {
            "prompt_tokens": 0, "completion_tokens": 0,
            "estimated_usd": 0.0, "unknown_price_calls": 0,
            "unknown_exposure_calls": 2, "unknown_exposure_tokens": 500,
            "unknown_exposure_usd": 0.25}}
        cfg = ProviderConfig(deepseek_max_tokens=4096,
                             deepseek_max_bytes=65536)
        summary = controller.campaign_summary([reported, unreported], cfg,
                                              300.0)
        totals = summary["totals"]
        # reported usage and unknown exposure are totalled separately
        self.assertEqual(totals["prompt_tokens"], 100)
        self.assertEqual(totals["completion_tokens"], 40)
        self.assertEqual(totals["unknown_price_calls"], 0)
        self.assertEqual(totals["unknown_exposure_calls"], 2)
        self.assertEqual(totals["unknown_exposure_tokens"], 500)
        self.assertAlmostEqual(totals["unknown_exposure_usd"], 0.25)
        per_ep = summary["results"][1]["usage"]
        self.assertEqual(per_ep["unknown_exposure_calls"], 2)
        self.assertEqual(per_ep["unknown_exposure_tokens"], 500)
        # the non-secret config bounds are carried
        self.assertEqual(summary["config"]["deepseek_max_tokens"], 4096)
        self.assertEqual(summary["config"]["deepseek_max_bytes"], 65536)
        for secret in ("deepseek_key_file", "jev_key_file",
                       "deepseek_api_key"):
            self.assertNotIn(secret, summary["config"])

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

    def test_summary_write_failure_is_recorded_not_swallowed(self):
        from tools.agent import controller
        with tempfile.TemporaryDirectory() as d:
            ctl = controller.Controller(
                ProviderConfig(),
                controller.ControllerPaths("w", "r", "d", "s"), d,
                episode_timeout=10.0)
            ctl.run_episode = lambda i: self._result(i)
            with mock.patch.object(controller, "write_campaign_summary",
                                   side_effect=OSError("disk full")):
                results = ctl.run_campaign(1)
            # episode results survive; the failure is recorded, path is None
            self.assertEqual(len(results), 1)
            self.assertIsNone(ctl.summary_path)
            self.assertEqual(ctl.summary_error, "disk full")

    def test_cli_reports_the_summary_failure(self):
        from tools.agent import __main__ as cli
        from tools.agent import controller
        with tempfile.TemporaryDirectory() as d:
            def fake_run_campaign(_self, episodes):
                _self.summary_path = None
                _self.summary_error = "disk full"
                return []

            err = io.StringIO()
            with mock.patch.object(controller.Controller, "run_campaign",
                                   fake_run_campaign):
                with contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(err):
                    rc = cli.main(["auto", "--episodes", "1",
                                   "--reflex", "scripted",
                                   "--strategy", "off",
                                   "--output-dir", d])
            self.assertEqual(rc, 0)
            self.assertIn("NOT WRITTEN", err.getvalue())
            self.assertNotIn("campaign.json", out.getvalue())


# --------------------------------------------------------------- cache rollup

class CacheCampaignTotalsTest(unittest.TestCase):
    """Cache usage rolls up by summed tokens, never averaged percentages."""

    def _result(self, idx, hit=0, miss=0, unclassified=0, reasoning=0):
        from tools.agent.controller import EpisodeResult
        r = EpisodeResult(index=idx)
        r.spawn_ok = True
        r.closed = True
        r.returncode = 0
        r.recording_complete = True
        classified = hit + miss
        r.budget = {"usage": {
            "prompt_tokens": classified + unclassified,
            "completion_tokens": 0, "estimated_usd": 0.0,
            "unknown_price_calls": 0, "unknown_exposure_calls": 0,
            "unknown_exposure_tokens": 0, "unknown_exposure_usd": 0.0,
            "cache_hit_tokens": hit, "cache_miss_tokens": miss,
            "cache_unclassified_tokens": unclassified,
            "cache_hit_rate": (round(hit / float(classified), 6)
                               if classified else None),
            "reasoning_tokens": reasoning}}
        return r

    def test_campaign_rate_uses_summed_tokens(self):
        from tools.agent import controller
        # ep1 is 90% over 100 classified tokens, ep2 is 0% over 900: the
        # weighted rate is 9%, not the naive 45% average of the two rates
        a = self._result(1, hit=90, miss=10)
        b = self._result(2, hit=0, miss=900)
        summary = controller.campaign_summary([a, b], ProviderConfig(), 300.0)
        totals = summary["totals"]
        self.assertEqual(totals["cache_hit_tokens"], 90)
        self.assertEqual(totals["cache_miss_tokens"], 910)
        self.assertEqual(totals["cache_unclassified_tokens"], 0)
        self.assertAlmostEqual(totals["cache_hit_rate"], 0.09)
        self.assertNotAlmostEqual(totals["cache_hit_rate"], 0.45)

    def test_zero_classified_tokens_is_a_null_campaign_rate(self):
        from tools.agent import controller
        summary = controller.campaign_summary(
            [self._result(1, unclassified=500)], ProviderConfig(), 1.0)
        self.assertIsNone(summary["totals"]["cache_hit_rate"])
        per = summary["results"][0]["usage"]
        self.assertEqual(per["cache_unclassified_tokens"], 500)
        self.assertIsNone(per["cache_hit_rate"])

    def test_reasoning_tokens_are_totalled(self):
        from tools.agent import controller
        summary = controller.campaign_summary(
            [self._result(1, hit=10, miss=10, reasoning=7),
             self._result(2, hit=10, miss=10, reasoning=3)],
            ProviderConfig(), 1.0)
        self.assertEqual(summary["totals"]["reasoning_tokens"], 10)

    def test_legacy_usage_without_cache_fields_still_summarizes(self):
        from tools.agent import controller
        from tools.agent.controller import EpisodeResult
        r = EpisodeResult(index=1)
        r.budget = {"usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        summary = controller.campaign_summary([r], ProviderConfig(), 1.0)
        per = summary["results"][0]["usage"]
        self.assertEqual(per["cache_hit_tokens"], 0)
        self.assertEqual(per["cache_miss_tokens"], 0)
        self.assertEqual(per["cache_unclassified_tokens"], 0)
        self.assertIsNone(per["cache_hit_rate"])
        self.assertEqual(per["reasoning_tokens"], 0)

    def test_safe_config_carries_the_cache_controls(self):
        from tools.agent import controller
        cfg = ProviderConfig(deepseek_history_pairs=4,
                             deepseek_context_max_bytes=1024,
                             deepseek_price_in=1.0, deepseek_price_out=2.0,
                             deepseek_price_cache_hit=0.5)
        summary = controller.campaign_summary([], cfg, 1.0)
        c = summary["config"]
        self.assertEqual(c["deepseek_history_pairs"], 4)
        self.assertEqual(c["deepseek_context_max_bytes"], 1024)
        self.assertEqual(c["deepseek_price_in"], 1.0)
        self.assertEqual(c["deepseek_price_out"], 2.0)
        self.assertEqual(c["deepseek_price_cache_hit"], 0.5)
        for secret in ("deepseek_key_file", "jev_key_file"):
            self.assertNotIn(secret, c)


# -------------------------------------------------- canned strategy replay

class _CannedStrategy(object):
    """A deterministic in-process strategy double for a replay pass."""

    name = "canned"

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def deliberate(self, context, deadline=0.0):
        from tools.agent import directives
        from tools.agent.providers import StrategyResult
        self.calls.append(context.prepared_request)
        i = len(self.calls) - 1
        payload = self.payloads[min(i, len(self.payloads) - 1)]
        if payload is None:
            return StrategyResult(provider="canned",
                                  reason="canned-failure", ok=False)
        dset, why = directives.validate_directive_set(payload)
        if dset is None:
            return StrategyResult(provider="canned",
                                  reason="invalid: %s" % why, ok=False)
        return StrategyResult(
            directives=[dset], provider="canned", reason="directives",
            ok=True,
            usage={"prompt_tokens": 100, "completion_tokens": 20,
                   "prompt_cache_hit_tokens": 60,
                   "prompt_cache_miss_tokens": 40, "reported": True},
            assistant_content=json.dumps(payload, sort_keys=True))

    def cancel(self):
        pass


class StrategyReplayContinuityTest(WireHarness):
    """A canned multi-turn replay is byte-identical across two runs."""

    PAYLOADS = [
        {"schema_version": 1, "goals": ["explore_frontier"], "ttl": 5},
        {"schema_version": 1, "goals": ["survive"], "ttl": 5},
        None,                                        # one canned failure
        {"schema_version": 1, "goals": ["recover", "survive"], "ttl": 5},
        {"schema_version": 1, "goals": ["descend_known_stairs"], "ttl": 5},
        {"schema_version": 1, "goals": ["inspect_inventory"], "ttl": 5},
    ]

    def _wire(self):
        from test_auto_providers import command_need, st_obs
        recs = [HELLO]
        for i in range(1, 8):
            recs.append(st_obs(i, command_need(i), dlvl="Dlvl:%d" % i, hp=10,
                               hp_max=20))
        recs.append(CLOSED)
        return [_line(r) for r in recs]

    def _pass(self, strategy):
        cfg = ProviderConfig(strategy="deepseek", deepseek_history_pairs=1,
                             max_ticks=200, strategy_call_cap=8,
                             postmortem_reserve=0, boundary_cooldown_ticks=0,
                             boundary_cooldown_wall=0.0,
                             boundary_emergency_wall=0.0)
        p = evaluate.ReplayPass(self._wire(), cfg, "scripted", "deepseek",
                                allow_network=True)
        p.strategy = strategy
        p.strategy_live = True
        p.run()
        return p

    def test_two_runs_agree_on_requests_and_cleaned_artifacts(self):
        sa = _CannedStrategy(self.PAYLOADS)
        sb = _CannedStrategy(self.PAYLOADS)
        pa, pb = self._pass(sa), self._pass(sb)
        self.assertGreaterEqual(len(sa.calls), 4)
        # identical request sequence, message for message
        self.assertEqual([c.messages for c in sa.calls],
                         [c.messages for c in sb.calls])
        # identical cleaned strategy decisions and ledger
        self.assertEqual(
            [d for d in pa.decisions if d.get("record") == "strategy"],
            [d for d in pb.decisions if d.get("record") == "strategy"])
        self.assertEqual(pa.ledger.as_dict(), pb.ledger.as_dict())
        self.assertEqual(pa.event_records_clean(),
                         pb.event_records_clean())

    def test_history_is_bounded_and_a_failure_commits_nothing(self):
        strategy = _CannedStrategy(self.PAYLOADS)
        p = self._pass(strategy)
        counts = [len(c.messages) for c in strategy.calls]
        # K=1: the first call is stateless, a successful call adds one pair
        self.assertEqual(counts[0], 2)
        self.assertEqual(counts[1], 4)
        # the committed assistant history is the canned verbatim JSON
        self.assertEqual(strategy.calls[1].messages[2],
                         ("assistant",
                          json.dumps(self.PAYLOADS[0], sort_keys=True)))
        # the canned *failure* commits nothing: the failed exchange never
        # appears as a user turn of any later request
        failed_user = strategy.calls[2].messages[-1][1]
        self.assertGreaterEqual(len(strategy.calls), 4)
        for later in strategy.calls[3:]:
            self.assertNotIn(("user", failed_user), later.messages)
        reasons = [d["reason"] for d in p.decisions
                   if d.get("record") == "strategy"]
        self.assertIn("canned-failure", reasons)

    def test_eviction_keeps_only_the_tail_pair(self):
        strategy = _CannedStrategy(self.PAYLOADS)
        self._pass(strategy)
        # with K=1 every multi-turn request carries exactly one prior pair
        for prepared in strategy.calls:
            self.assertLessEqual(len(prepared.retained), 1)


if __name__ == "__main__":
    unittest.main()
