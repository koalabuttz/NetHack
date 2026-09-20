#!/usr/bin/env python3
"""Offline tests for the advisory Jev bench judge (no network).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Every test uses a fake transport or a fake worker supervisor: no credential is
read, no socket is opened and no paid call is made.
"""

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import bench_judge as J     # noqa: E402
from tools.agent import bench_metrics as M   # noqa: E402


CARD = {
    "schema_version": "episode-scorecard/1",
    "episode_id": "ep-1",
    "integrity": {"status": "complete", "recording_complete": True,
                  "operational_ok": True},
    "terminal_class": "horizon-completion",
    "exploration": {"entered_cells_instance_scoped": 12,
                    "discovered_cells_instance_scoped": 40,
                    "entered_per_100_attempts": 8.0, "attempts": 150,
                    "attempts_source": "actions", "depth_max": 4,
                    "depth_final": 3, "time_advances": 90,
                    "longest_loop_span": 3, "stationary_span_max": 4},
    "activity": {"ticks": 400, "actions": 150, "invalids": 0},
    "termination": {"stop_reason": "tick-cap-graceful-quit",
                    "outcome": "unknown"},
    "lifecycle": {"available": True, "legacy_stream": False,
                  "terminal_completeness": 1.0},
    "availability": {},
}


def body(noul=0.1, prod=2.5, sane=0.9, model="jev-1.13.0",
         input_tokens=300, output_tokens=20, levels=J.PRODUCTIVITY_LEVELS):
    legend = {str(i): t for i, t in enumerate(levels)}
    return {
        "model": model,
        "answers": {
            "degenerate_loop": {"type": "noul", "noul": noul},
            "exploration_productivity": {
                "type": "score", "score": prod, "confidence": 0.9,
                "legend": legend,
                "probabilities": {str(i): 0.0 for i in range(len(levels))}},
            "termination_sanity": {"type": "noul", "noul": sane},
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class FakeSupervisor(object):
    def __init__(self, result):
        self.result = result
        self.jobs = []

    def run(self, job, deadline):
        self.jobs.append(job)
        return self.result


def _judge(**kw):
    kw.setdefault("model", "jev-latest")
    return J.BenchJudge(**kw)


# ==========================================================================
# AC6 - allowlist, typed answers, accounting
# ==========================================================================

class JudgeContract(unittest.TestCase):
    def test_judge_payload_contains_only_scorecard_allowlist(self):
        state = J.judge_state(CARD)
        self.assertIn("exploration.entered_cells_instance_scoped",
                      state["metrics"])
        # raw transcripts / paths / identity are never present
        blob = json.dumps(state)
        self.assertNotIn("wire_path", blob)
        self.assertNotIn("episode_id", blob)
        self.assertNotIn("/home/", blob)
        self.assertNotIn("baseline", blob)
        # a metric omitted for size is still named in the availability map
        self.assertIsInstance(state["unavailable"], dict)

    def test_judge_cache_key_and_hit_do_not_dispatch(self):
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return body()

        judge = _judge(transport=transport)
        first = judge.evaluate(CARD)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(calls["n"], 1)
        second = judge.evaluate(CARD)
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["dispatched"])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(judge.ledger.cache_hits, 1)
        # the cache key is on scorecard + rubric + model hashes only
        self.assertEqual(J.judge_cache_key(CARD, model="jev-latest"),
                         J.judge_cache_key(CARD, model="jev-latest"))

    def test_rejudge_is_new_recorded_budgeted_call(self):
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return body()

        judge = _judge(transport=transport)
        judge.evaluate(CARD)
        judge.evaluate(CARD, force=True)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(judge.ledger.rejudges, 1)
        self.assertEqual(judge.ledger.dispatched, 2)

    def test_bundled_judge_counts_one_dispatch_and_rejects_partial_answers(
            self):
        judge = _judge(transport=lambda p: body())
        result = judge.evaluate(CARD)
        self.assertEqual(result["dispatches"], 1)
        self.assertEqual(judge.ledger.dispatched, 1)
        # a partial answer (missing one id) is invalid, never partially credited
        partial = body()
        del partial["answers"]["termination_sanity"]
        bad = _judge(transport=lambda p: partial).evaluate(CARD)
        self.assertEqual(bad["status"], "invalid")
        self.assertNotIn("answers", bad)
        self.assertEqual(bad["dispatches"], 1)

    def test_single_question_judge_counts_three_dispatches_and_handles_partial_failure(
            self):
        # happy path: three independently budgeted single-question calls
        happy = _judge(request_shape="single", transport=lambda p: {
            "model": "jev-1.13.0",
            "answers": {next(iter(p["questions"])):
                        body()["answers"][next(iter(p["questions"]))]},
            "usage": {"input_tokens": 100, "output_tokens": 7}})
        result = happy.evaluate(CARD)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["dispatches"], 3)
        self.assertEqual(happy.ledger.dispatched, 3)
        # partial failure: the second call fails and the rest are not spent
        seen = []

        def transport(payload):
            seen.append(set(payload["questions"]))
            if len(seen) == 2:
                return None
            qid = next(iter(payload["questions"]))
            return {"model": "jev-1.13.0",
                    "answers": {qid: body()["answers"][qid]},
                    "usage": {"input_tokens": 100, "output_tokens": 7}}

        judge = _judge(transport=transport, request_shape="single")
        failed = judge.evaluate(CARD)
        self.assertEqual(failed["status"], "timeout")
        self.assertEqual(len(seen), 2)
        self.assertEqual(judge.ledger.dispatched, 2)

    def test_judge_typed_answers_missing_nan_extra_questions(self):
        # missing answer id
        with self.assertRaises(J.JudgeError):
            J.parse_answers({"answers": {
                "degenerate_loop": {"type": "noul", "noul": 0.1}}})
        # NaN score
        bad = body()
        bad["answers"]["exploration_productivity"]["score"] = float("nan")
        with self.assertRaises(J.JudgeError):
            J.parse_answers(bad)
        # out-of-scale score (levels = 5 -> top = 4)
        bad = body(prod=9.0)
        with self.assertRaises(J.JudgeError):
            J.parse_answers(bad)
        # extra question
        extra = body()
        extra["answers"]["mystery"] = {"type": "noul", "noul": 0.5}
        with self.assertRaises(J.JudgeError):
            J.parse_answers(extra)
        # wrong type
        wrong = body()
        wrong["answers"]["degenerate_loop"] = {"type": "score", "score": 1.0}
        with self.assertRaises(J.JudgeError):
            J.parse_answers(wrong)

    def test_no_score_on_malformed_output(self):
        judge = _judge(transport=lambda p: {"model": "x", "answers": {}})
        result = judge.evaluate(CARD)
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("answers", result)
        self.assertEqual(judge.ledger.invalid, 1)

    def test_judge_disagreement_does_not_change_objective_or_gate(self):
        # a judge that flags everything is still advisory: the objective and
        # the gate are unchanged.
        def transport(payload):
            return body(noul=0.99, prod=0.0, sane=0.0)

        judge = _judge(transport=transport)
        flagged = judge.evaluate(CARD)
        self.assertTrue(flagged["flags"])
        self.assertTrue(flagged["advisory"])
        # the flags select postmortem packages but never gate
        selected = J.select_postmortem_packages([flagged])
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["advisory"])
        self.assertNotIn("verdict", flagged)
        self.assertNotIn("admission", flagged)

    def test_rubric_only_change_leaves_deterministic_comparison_unchanged_and_forces_rejudgment(
            self):
        # deterministic comparison unchanged by an advisory-only difference
        base = [{"episode_id": "b%d" % i,
                 "terminal_class": M.HORIZON_COMPLETION,
                 "integrity": {"status": "complete"},
                 "exploration": {"entered_cells_instance_scoped": 10.0,
                                 "entered_per_100_attempts": 5.0,
                                 "depth_max": 3, "time_advances": 30,
                                 "longest_loop_span": 1,
                                 "stationary_span_max": 1,
                                 "attempts_source": "actions"},
                 "activity": {"ticks": 100},
                 "gates": {"hard_failure": False}} for i in range(6)]
        cand = [dict(c, exploration=dict(c["exploration"],
                                         entered_cells_instance_scoped=30.0))
                for c in base]
        policy = {"target_metric":
                  "exploration.entered_cells_instance_scoped",
                  "min_improvement": 0.0, "min_samples": 3, "resamples": 200,
                  "resampling_seed": 1, "confidence_level": 0.95,
                  "confirmation_episodes_per_arm": 10,
                  "min_completed_episodes_per_arm": 2}
        p1 = M.provenance_manifest(commit="c1",
                                   imported_code={"p": "x"},
                                   judge_modules={"bench_judge": "r1"},
                                   rubric_version="r1")
        p2 = M.provenance_manifest(commit="c2",
                                   imported_code={"p": "x"},
                                   judge_modules={"bench_judge": "r2"},
                                   rubric_version="r2")
        a = M.compare_arms(base, cand, policy, base_provenance=p1,
                           cand_provenance=p1)
        b = M.compare_arms(base, cand, policy, base_provenance=p1,
                           cand_provenance=p2)
        self.assertEqual(a["verdict"], b["verdict"])
        self.assertTrue(b["provenance"]["comparable"])
        self.assertFalse(b["provenance"]["advisory_equal"])
        # the rubric change forces rejudgment: a different cache key
        key1 = J.judge_cache_key(CARD, model="jev-latest", rubric="rubric-1")
        key2 = J.judge_cache_key(CARD, model="jev-latest", rubric="rubric-2")
        self.assertNotEqual(key1, key2)

    def test_judge_timeout_no_retry_and_no_secret_in_artifacts(self):
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return None          # the deadline elapsed

        judge = _judge(transport=transport)
        result = judge.evaluate(CARD)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(calls["n"], 1)          # no automatic retry
        self.assertEqual(judge.ledger.timeouts, 1)
        self.assertEqual(judge.ledger.dispatched, 1)

    def test_worker_transport_keeps_secret_out_of_artifacts(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        keyfile = os.path.join(tmp, "jev.key")
        with open(keyfile, "w") as fh:
            fh.write("sk-supersecret-value\n")
        os.chmod(keyfile, 0o600)
        supervisor = FakeSupervisor({"ok": True, "json": body()})
        transport = J.make_worker_transport(
            ["unused"], key_file=keyfile,
            supervisor_factory=lambda: supervisor)
        judge = _judge(transport=transport)
        result = judge.evaluate(CARD)
        self.assertEqual(result["status"], "ok")
        # the secret reached the worker job (as required) ...
        self.assertEqual(supervisor.jobs[0]["api_key"], "sk-supersecret-value")
        # ... but never the judge result, ledger or cache
        blob = json.dumps({"result": result, "ledger": judge.ledger.as_dict(),
                           "cache": judge.cache})
        self.assertNotIn("sk-supersecret-value", blob)

    def test_judge_calls_total_budget_is_enforced(self):
        judge = _judge(transport=lambda p: body(), calls_total=1)
        judge.evaluate(CARD, force=True)
        with self.assertRaises(J.JudgeError):
            judge.evaluate(CARD, force=True)


class RubricDispatch(unittest.TestCase):
    """Item 6: the dispatched instructions are exactly the recorded rubric."""

    def test_dispatched_rubric_matches_recorded_hash_and_differs(self):
        seen = []

        def transport(payload):
            seen.append(payload)
            return body()

        r1 = "RUBRIC-ONE: rate the episode's exploration."
        r2 = "RUBRIC-TWO: a deliberately different rubric text."
        first = _judge(transport=transport, rubric=r1)
        res1 = first.evaluate(CARD)
        second = _judge(transport=transport, rubric=r2)
        res2 = second.evaluate(CARD)
        # payload bytes differ
        p1 = json.dumps(seen[0], sort_keys=True)
        p2 = json.dumps(seen[1], sort_keys=True)
        self.assertNotEqual(p1, p2)
        # each dispatched payload carries its own rubric text
        self.assertIn(r1, p1)
        self.assertNotIn(r2, p1)
        self.assertIn(r2, p2)
        # cache keys differ
        self.assertNotEqual(res1["cache_key"], res2["cache_key"])
        # each result's recorded rubric hash matches the dispatched text
        self.assertEqual(res1["rubric_hash"], J.rubric_hash(r1))
        self.assertEqual(res2["rubric_hash"], J.rubric_hash(r2))
        self.assertNotEqual(res1["rubric_hash"], res2["rubric_hash"])

    def test_build_questions_embeds_the_given_rubric_instance(self):
        q = J.build_questions(rubric="CUSTOM-RUBRIC-TEXT")
        for qid in ("degenerate_loop", "exploration_productivity",
                    "termination_sanity"):
            self.assertIn("CUSTOM-RUBRIC-TEXT", q[qid]["instructions"])
        # the module constant is not used when a rubric is supplied
        self.assertNotEqual(
            J.rubric_hash("CUSTOM-RUBRIC-TEXT"),
            J.rubric_hash(J.RUBRIC_TEXT))


if __name__ == "__main__":
    unittest.main()
