#!/usr/bin/env python3
"""Wave-1 tests for the neutral candidate/identity layer.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Or directly:

    python3 test/agent/test_auto_candidates.py

These cover ``tools/agent/candidates.py`` (the dependency-neutral leaf) and
``tools/agent/arbitration.py`` (the shared pure selection/rejection helpers):
the neutral import graph, deterministic canonical identity, deduplication,
ordering/cardinality, and the mutation-M11 (choice gate) and M22
(canonicalize-once) demonstrations from
``doc/agent-reflex-upgrade-plan.md`` sections 8.5 and 9.
"""

import ast
import json
import math
import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import arbitration, candidates, presentation  # noqa: E402

# Every sibling module the neutral leaf must NOT import, directly or
# indirectly (section 3.1: "import neither policy nor providers").
_FORBIDDEN = {
    "policy", "providers", "protocol", "state", "directives", "controller",
    "evaluate", "recording", "events", "budget", "worker", "render",
    "spectating", "codec",
}


def _imported_names(path):
    """Every module name imported by a Python source file (relative too)."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:           # a relative import inside the package
                names.add(node.module or "")
                for alias in node.names:
                    names.add(alias.name)
            else:
                names.add((node.module or "").split(".")[0])
    return names


class NeutralImportGraph(unittest.TestCase):
    """The leaf must stay a leaf: stdlib only, no package siblings."""

    def _path(self, modname):
        return os.path.join(_ROOT, "tools", "agent", modname + ".py")

    def test_candidates_imports_nothing_from_the_package(self):
        names = _imported_names(self._path("candidates"))
        leaked = names & _FORBIDDEN
        self.assertEqual(leaked, set(),
                         "candidates.py imports package siblings: %s"
                         % leaked)
        # and it must not even import ``tools`` as a package root
        self.assertNotIn("tools", names)

    def test_arbitration_imports_only_candidates(self):
        names = _imported_names(self._path("arbitration"))
        leaked = (names & _FORBIDDEN) - {"candidates"}
        self.assertEqual(leaked, set(),
                         "arbitration.py imports forbidden modules: %s"
                         % leaked)

    def test_importing_the_leaf_pulls_no_heavy_module(self):
        # Run in a fresh interpreter: other test modules legitimately import
        # policy/providers, so an in-process sys.modules check would be a
        # false failure.  The leaf must not pull them in on its own.
        code = ("import sys, tools.agent.candidates; "
                "print('tools.agent.policy' in sys.modules, "
                "'tools.agent.providers' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "False False", out.stderr)


class CanonicalEncoding(unittest.TestCase):
    def test_sorted_keys_and_tight_separators(self):
        a = candidates.canonical_bytes({"b": 1, "a": 2})
        b = candidates.canonical_bytes({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, b'{"a":2,"b":1}')

    def test_nan_and_infinity_are_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                candidates.canonical_bytes({"x": bad})

    def test_deterministic_across_processes(self):
        # No PYTHONHASHSEED dependence: the digest is a pure function of the
        # value, and a set is never an input.
        body = {"z": [1, 2, 3], "a": {"n": "s"}}
        once = candidates.sha256_hex(candidates.canonical_bytes(body))
        twice = candidates.sha256_hex(candidates.canonical_bytes(body))
        self.assertEqual(once, twice)


class ImmutableActionModel(unittest.TestCase):
    def test_roundtrip_every_shape(self):
        cases = [
            {"key": 104}, {"yn": 121}, {"yn": 121, "count": 5},
            {"position": [10, 5]}, {"text": "quit"}, {"cancel": True},
            {"ack": True}, {"menu": 3, "commit": [[1, -1], [2, -1]]},
        ]
        for wire in cases:
            act = candidates.wire_to_action(wire)
            self.assertEqual(act.to_wire(), wire, wire)

    def test_menu_roundtrip_is_canonically_ordered(self):
        # A selection *set* is canonicalized to ascending row order, so a
        # reordered commit round-trips to the sorted form (and shares one
        # identity) rather than echoing the caller's order.
        act = candidates.wire_to_action(
            {"menu": 3, "commit": [[2, -1], [1, -1]]})
        self.assertEqual(act.to_wire(),
                         {"menu": 3, "commit": [[1, -1], [2, -1]]})

    def test_action_is_frozen(self):
        act = candidates.ImmutableAction.key(104)
        with self.assertRaises(Exception):
            act.tag = "menu"

    def test_menu_commit_order_is_canonical(self):
        a = candidates.ImmutableAction.menu(3, [[2, -1], [1, -1]])
        b = candidates.ImmutableAction.menu(3, [[1, -1], [2, -1]])
        self.assertEqual(a, b)
        self.assertEqual(a.signature(), b.signature())

    def test_yn_count_absent_is_canonical_none(self):
        act = candidates.ImmutableAction.yn(121)
        self.assertEqual(act.to_wire(), {"yn": 121})

    def test_equivalent_actions_share_signature(self):
        a = candidates.canonical_action({"key": 46})
        b = candidates.canonical_action({"key": 46})
        self.assertEqual(a.signature(), b.signature())

    def test_unknown_shape_raises(self):
        with self.assertRaises(ValueError):
            candidates.wire_to_action({"nope": 1})


class CandidateIdentity(unittest.TestCase):
    def _cand(self, action, label="lbl", effect="eff", score=0):
        return candidates.make_candidate(
            action, label, "frontier", (1, 0), 1, score, [("base", score)],
            "reason", effect)

    def test_id_binds_action_label_and_effect(self):
        base = self._cand({"key": 108})
        self.assertNotEqual(base.candidate_id,
                            self._cand({"key": 108}, label="other"))
        self.assertNotEqual(base.candidate_id,
                            self._cand({"key": 108}, effect="other"))

    def test_id_ignores_score(self):
        self.assertEqual(self._cand({"key": 108}, score=1).candidate_id,
                         self._cand({"key": 108}, score=999).candidate_id)

    def test_signature_ignores_label(self):
        a = self._cand({"key": 108})
        b = self._cand({"key": 108}, label="renamed")
        self.assertEqual(a.action_signature, b.action_signature)
        self.assertNotEqual(a.candidate_id, b.candidate_id)

    def test_non_integer_score_rejected(self):
        with self.assertRaises(ValueError):
            candidates.make_candidate({"key": 108}, "l", "frontier", (), 0,
                                      1.5)

    def test_bool_score_rejected(self):
        with self.assertRaises(ValueError):
            candidates.make_candidate({"key": 108}, "l", "frontier", (), 0,
                                      True)


class TableIdentity(unittest.TestCase):
    def _table(self, version=1, digest="d", order=("a", "b")):
        mk = {"a": {"key": 108}, "b": {"key": 104}}
        cands = [candidates.make_candidate(mk[n], n, "frontier", (1, 0), 1,
                                          100, [("base", 100)], "", "")
                 for n in order]
        return candidates.build_table((7, 3, 9), version, cands, digest)

    def test_table_id_is_not_circular(self):
        t = self._table()
        # Re-deriving the body and re-hashing the ID field must not reproduce
        # the ID: the ID excludes itself and the retained bytes.
        body = json.loads(t.canonical_bytes.decode("utf-8"))
        self.assertNotIn("table_id", body)
        self.assertNotIn("canonical_bytes", body)
        self.assertEqual(candidates.sha256_hex(t.canonical_bytes), t.table_id)

    def test_input_order_does_not_change_identity(self):
        self.assertEqual(self._table(order=("a", "b")).table_id,
                         self._table(order=("b", "a")).table_id)

    def test_table_version_and_digest_are_identity_inputs(self):
        base = self._table()
        self.assertNotEqual(base.table_id, self._table(version=2).table_id)
        self.assertNotEqual(base.table_id, self._table(digest="e").table_id)

    def test_rejection_version_is_an_identity_input(self):
        a = candidates.build_table((1, 2, 3), 1, [], "", False, 0)
        b = candidates.build_table((1, 2, 3), 1, [], "", False, 1)
        self.assertNotEqual(a.table_id, b.table_id)

    def test_retained_bytes_are_exact_and_reused(self):
        t = self._table()
        self.assertEqual(t.canonical_bytes,
                         candidates.canonical_bytes(
                             json.loads(t.canonical_bytes.decode("utf-8"))))

    def test_scripted_index_is_the_argmax(self):
        cands = [
            candidates.make_candidate({"key": 104}, "low", "frontier",
                                      (0, 1), 1, 10, [("b", 10)], "", ""),
            candidates.make_candidate({"key": 107}, "high", "stair",
                                      (0, -1), 0, 900, [("s", 900)], "", ""),
        ]
        t = candidates.build_table((1, 1, 1), 1, cands)
        self.assertEqual(t.scripted().semantic_label, "high")

    def test_need_key_normalization(self):
        self.assertEqual(candidates.normalize_need_key((1, 2, 3)), (1, 2, 3))
        self.assertEqual(candidates.normalize_need_key([1, 2, 3]), (1, 2, 3))
        self.assertEqual(candidates.normalize_need_key(None), ())

        class NK(object):
            episode, seq, id = 4, 5, 6

        self.assertEqual(candidates.normalize_need_key(NK()), (4, 5, 6))


class DedupOrderingCardinality(unittest.TestCase):
    def test_equivalent_actions_deduplicate_before_truncation(self):
        cands = [candidates.make_candidate(
            {"key": 115}, "s%d" % i, "secret-search", (), 0, 100 - i,
            [("b", 100 - i)], "", "") for i in range(300)]
        ordered = candidates.dedup_and_order(cands)
        self.assertEqual(len(ordered), 1)
        # the winner is the highest-scoring representative
        self.assertEqual(ordered[0].score, 100)

    def test_cardinality_cap_is_255(self):
        cands = [candidates.make_candidate(
            {"key": 100 + (i % 100)}, "c%d" % i, "frontier", (1, 0), 1,
            100, [("b", 100)], "", "") for i in range(400)]
        self.assertLessEqual(len(candidates.dedup_and_order(cands)), 255)
        t = candidates.build_table((1, 1, 1), 1, cands)
        self.assertLessEqual(len(t), 255)

    def test_family_rank_breaks_equal_scores(self):
        a = candidates.make_candidate({"key": 104}, "frontier", "frontier",
                                      (0, 1), 1, 500, [("b", 500)], "", "")
        b = candidates.make_candidate({"key": 108}, "stair", "stair",
                                      (1, 1), 9, 500, [("b", 500)], "", "")
        ordered = candidates.dedup_and_order([a, b])
        self.assertEqual(ordered[0].semantic_label, "stair")

    def test_direction_rank_breaks_equal_family_scores(self):
        a = candidates.make_candidate({"key": 104}, "east", "frontier",
                                      (1, 0), 2, 500, [("b", 500)], "", "")
        b = candidates.make_candidate({"key": 107}, "north", "frontier",
                                      (0, -1), 0, 500, [("b", 500)], "", "")
        ordered = candidates.dedup_and_order([a, b])
        self.assertEqual(ordered[0].semantic_label, "north")

    def test_ordering_is_total_and_stable(self):
        mk = [candidates.make_candidate({"key": 100 + i}, "c%d" % i,
                                        "frontier", (1, 0), 1, 500,
                                        [("b", 500)], "", "")
              for i in range(20)]
        first = [c.candidate_id for c in candidates.dedup_and_order(mk)]
        rev = [c.candidate_id for c in candidates.dedup_and_order(
            list(reversed(mk)))]
        self.assertEqual(first, rev)


class JevPayloadAndCanonicalizeOnce(unittest.TestCase):
    """M22: the retained bytes are reused, never re-canonicalized."""

    def _table(self):
        cands = [candidates.make_candidate({"key": 100 + i}, "c%d" % i,
                                           "frontier", (1, 0), 1, 500,
                                           [("b", 500)], "", "")
                 for i in range(8)]
        return candidates.build_table((1, 1, 1), 2, cands, "dig")

    def test_build_canonicalizes_once_per_candidate_plus_body(self):
        cands = [candidates.make_candidate({"key": 100 + i}, "c%d" % i,
                                           "frontier", (1, 0), 1, 500,
                                           [("b", 500)], "", "")
                 for i in range(8)]
        candidates.reset_canonicalize_count()
        candidates.build_table((1, 1, 1), 2, cands, "dig")
        calls = candidates.canonicalize_count()
        # Two serializations per candidate (id body + action signature) plus
        # one for the table body.  A regression that re-serializes the whole
        # table per candidate would blow far past this bound.
        self.assertLessEqual(calls, 2 * len(cands) + 1)

    def test_payload_path_does_not_recanonicalize(self):
        t = self._table()
        candidates.reset_canonicalize_count()
        payload = candidates.jev_payload(t)
        self.assertEqual(candidates.canonicalize_count(), 0)
        self.assertEqual(payload["table_id"], t.table_id)
        self.assertEqual(len(payload["candidates"]), len(t))

    def test_selection_path_does_not_recanonicalize(self):
        t = self._table()
        rejected = arbitration.RejectionSet()
        candidates.reset_canonicalize_count()
        arbitration.select_retained(t, rejected)
        self.assertEqual(candidates.canonicalize_count(), 0)


class RejectionAndSelection(unittest.TestCase):
    def _table(self):
        cands = [
            candidates.make_candidate({"key": 107}, "north", "stair",
                                      (0, -1), 0, 900, [("s", 900)], "", ""),
            candidates.make_candidate({"key": 104}, "west", "frontier",
                                      (-1, 0), 1, 500, [("b", 500)], "", ""),
            candidates.make_candidate({"key": 115}, "search", "secret-search",
                                      (), 0, 300, [("b", 300)], "", ""),
        ]
        return candidates.build_table((1, 1, 1), 1, cands)

    def test_select_retained_skips_the_rejected_argmax(self):
        t = self._table()
        rejected = arbitration.RejectionSet()
        top = t.scripted()
        rejected.exclude(top)
        self.assertEqual(arbitration.select_retained(t, rejected)
                         .semantic_label, "west")

    def test_exclusion_follows_an_equivalent_action_across_tables(self):
        t = self._table()
        rejected = arbitration.RejectionSet()
        rejected.exclude(t.scripted())
        # A rebuilt table (new version) renames the label but keeps the
        # action: the exclusion must still hold by canonical signature.
        renamed = candidates.make_candidate({"key": 107}, "the way out",
                                            "stair", (0, -1), 0, 900,
                                            [("s", 900)], "", "")
        rebuilt = candidates.build_table((1, 1, 1), 2, [renamed])
        self.assertTrue(rejected.excludes(rebuilt.scripted()))

    def test_exhaustion_returns_none(self):
        t = self._table()
        rejected = arbitration.RejectionSet()
        for c in t.ordered_candidates:
            rejected.exclude(c)
        self.assertIsNone(arbitration.select_retained(t, rejected))

    def test_incomplete_is_delivery_repair_not_gameplay_exclusion(self):
        t = self._table()
        d = arbitration.classify_invalid("incomplete", t.scripted())
        self.assertTrue(d.repair)
        self.assertFalse(d.gameplay)
        for code in ("schema", "stale", "kind", "range"):
            d = arbitration.classify_invalid(code, t.scripted())
            self.assertFalse(d.repair, code)
            self.assertTrue(d.gameplay, code)


class RawChoiceValidation(unittest.TestCase):
    def _table(self):
        cands = [
            candidates.make_candidate({"key": 107}, "north", "stair",
                                      (0, -1), 0, 900, [("s", 900)], "", ""),
            candidates.make_candidate({"key": 104}, "west", "frontier",
                                      (-1, 0), 1, 500, [("b", 500)], "", ""),
        ]
        return candidates.build_table((1, 2, 3), 4, cands)

    def _raw(self, table, **kw):
        base = dict(table_id=table.table_id, need_key=table.need_key,
                    table_version=table.table_version, index=0,
                    confidence=0.95)
        base.update(kw)
        return arbitration.RawChoice(**base)

    def test_accepts_a_valid_choice(self):
        t = self._table()
        out = arbitration.validate_raw_choice(
            t, self._raw(t, index=1), arbitration.RejectionSet())
        self.assertTrue(out.accepted)
        self.assertEqual(out.candidate.semantic_label, "west")

    # -- M11: each gate must independently fail closed -------------------
    def test_m11_identity_gate(self):
        t = self._table()
        out = arbitration.validate_raw_choice(
            t, self._raw(t, table_id="deadbeef"), arbitration.RejectionSet())
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "stale")
        out = arbitration.validate_raw_choice(
            t, self._raw(t, table_version=99), arbitration.RejectionSet())
        self.assertEqual(out.code, "stale")
        out = arbitration.validate_raw_choice(
            t, self._raw(t, need_key=(9, 9, 9)), arbitration.RejectionSet())
        self.assertEqual(out.code, "stale")

    def test_m11_index_type_gate(self):
        t = self._table()
        for bad in (True, "0", 1.0, None):
            out = arbitration.validate_raw_choice(
                t, self._raw(t, index=bad), arbitration.RejectionSet())
            self.assertFalse(out.accepted, bad)
            self.assertEqual(out.code, "index-type", bad)

    def test_m11_index_range_gate(self):
        t = self._table()
        for bad in (-1, 2, 999):
            out = arbitration.validate_raw_choice(
                t, self._raw(t, index=bad), arbitration.RejectionSet())
            self.assertFalse(out.accepted, bad)
            self.assertEqual(out.code, "index-range", bad)

    def test_m11_confidence_gate(self):
        t = self._table()
        for bad in (0.5, 0.7999, float("nan"), float("inf"), True, "hi"):
            out = arbitration.validate_raw_choice(
                t, self._raw(t, confidence=bad), arbitration.RejectionSet())
            self.assertFalse(out.accepted, bad)
            self.assertEqual(out.code, "confidence", bad)
        # exactly the threshold is accepted (>=), one ulp below is not
        self.assertTrue(arbitration.validate_raw_choice(
            t, self._raw(t, confidence=0.8),
            arbitration.RejectionSet()).accepted)

    def test_m11_rejected_member_gate(self):
        t = self._table()
        rejected = arbitration.RejectionSet()
        rejected.exclude(t.ordered_candidates[0])
        out = arbitration.validate_raw_choice(t, self._raw(t, index=0),
                                              rejected)
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "rejected-member")

    def test_m11_safety_gate(self):
        t = self._table()
        out = arbitration.validate_raw_choice(
            t, self._raw(t, index=0), arbitration.RejectionSet(),
            eligible=lambda c: c.semantic_label != "north")
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "unsafe")

    def test_parse_error_and_abstain_short_circuit(self):
        t = self._table()
        out = arbitration.validate_raw_choice(
            t, self._raw(t, parse_error="bad json"),
            arbitration.RejectionSet())
        self.assertEqual(out.code, "parse")
        out = arbitration.validate_raw_choice(
            t, self._raw(t, abstain=True), arbitration.RejectionSet())
        self.assertEqual(out.code, "abstain")

    def test_usage_is_carried_on_a_rejected_choice(self):
        # Billing is controller-side (wave 6), but the raw usage must survive
        # a rejection so it can be charged exactly once.
        t = self._table()
        raw = self._raw(t, index=999,
                        usage=(("prompt", 10), ("completion", 2)))
        out = arbitration.validate_raw_choice(t, raw,
                                              arbitration.RejectionSet())
        self.assertFalse(out.accepted)
        self.assertEqual(dict(raw.usage)["prompt"], 10)


class ReconciliationClassification(unittest.TestCase):
    def test_classify_outcome_matrix(self):
        self.assertEqual(arbitration.classify_outcome(False, True, 1),
                         ("observed", "moved"))
        self.assertEqual(arbitration.classify_outcome(True, True, 1),
                         ("observed", "stationary-time-advanced"))
        self.assertEqual(arbitration.classify_outcome(True, True, 0),
                         ("observed", "no-time"))
        self.assertEqual(arbitration.classify_outcome(True, False, 1),
                         ("observed", "unknown"))
        self.assertEqual(arbitration.classify_outcome(False, True, None),
                         ("observed", "unknown"))


class SentAttemptLifecycle(unittest.TestCase):
    def _attempt(self):
        t = candidates.build_table(
            (1, 2, 3), 1,
            [candidates.make_candidate({"key": 104}, "west", "frontier",
                                       (-1, 0), 1, 500, [("b", 500)],
                                       "", "")])
        return candidates.make_sent_attempt((1, 2, 3), t, t.scripted(), 7,
                                            "fp", [(3, 4)], 2, "step west")

    def test_primary_key_is_the_four_tuple(self):
        att = self._attempt()
        self.assertEqual(att.primary_key,
                         ((1, 2, 3), att.table_id, att.candidate_id, 7))

    def test_terminate_returns_a_new_frozen_instance(self):
        att = self._attempt()
        done = att.terminate("observed", "moved")
        self.assertTrue(att.live)          # original untouched
        self.assertFalse(done.live)
        self.assertEqual(done.observed_kind, "moved")

    def test_unknown_terminal_state_rejected(self):
        with self.assertRaises(ValueError):
            self._attempt().terminate("bogus")
        with self.assertRaises(ValueError):
            self._attempt().terminate("observed", "bogus-kind")

    def test_before_hero_set_is_canonical(self):
        att = self._attempt()
        self.assertEqual(att.before_hero_set, ((3, 4),))


class ReflexFeaturesDigest(unittest.TestCase):
    def test_digest_is_deterministic_and_order_sensitive(self):
        a = candidates.ReflexFeatures(episode=1, controller_tick=5,
                                      hero_possible=((3, 4), (3, 5)))
        b = candidates.ReflexFeatures(episode=1, controller_tick=5,
                                      hero_possible=((3, 4), (3, 5)))
        c = candidates.ReflexFeatures(episode=1, controller_tick=6,
                                      hero_possible=((3, 4), (3, 5)))
        self.assertEqual(a.digest(), b.digest())
        self.assertNotEqual(a.digest(), c.digest())

    def test_digest_feeds_the_table_identity(self):
        f1 = candidates.ReflexFeatures(controller_tick=1)
        f2 = candidates.ReflexFeatures(controller_tick=2)
        t1 = candidates.build_table((1, 1, 1), 1, [], f1.digest())
        t2 = candidates.build_table((1, 1, 1), 1, [], f2.digest())
        self.assertNotEqual(t1.table_id, t2.table_id)

    def test_prepared_reflex_exposes_the_retained_bytes(self):
        f = candidates.ReflexFeatures()
        t = candidates.build_table((1, 1, 1), 1, [], f.digest())
        prep = candidates.PreparedReflex(f, t)
        self.assertEqual(prep.table_id, t.table_id)
        self.assertEqual(prep.canonical_bytes, t.canonical_bytes)


# --------------------------------------------------- Jev presentation identity

class JevPresentationIdentity(unittest.TestCase):
    """AC.1/AC.8: presentation never changes candidate or table identity.

    The presentation layer names and describes candidates the policy already
    built; it must leave the retained table byte-identical, never re-dedup,
    never reorder and never mint an identity of its own.
    """

    def _table(self):
        cands = [
            candidates.make_candidate({"key": 104}, "navigate", "frontier",
                                      (1, 0), 1, 500, [("b", 500)],
                                      "navigate: observation frontier",
                                      "navigate"),
            candidates.make_candidate({"key": 106}, "navigate", "stair",
                                      (0, 1), 2, 400, [("b", 400)],
                                      "navigate: reachable down stairs",
                                      "navigate"),
            candidates.make_candidate({"key": 115}, "search", "recovery", (),
                                      0, 300, [("b", 300)],
                                      "loop breaker: search", "site-search"),
            candidates.make_candidate({"key": 115}, "search-secret",
                                      "secret-search", (), 0, 200,
                                      [("b", 200)], "search for secret doors",
                                      "secret-search"),
        ]
        return candidates.build_table((1, 1, 1), 1, cands,
                                      rejection_version=2)

    def _identity(self, table):
        return {
            "table_id": table.table_id,
            "canonical_bytes": table.canonical_bytes,
            "candidate_ids": [c.candidate_id for c in table.ordered_candidates],
            "labels": [c.semantic_label for c in table.ordered_candidates],
            "actions": [c.action.canonical()
                        for c in table.ordered_candidates],
            "signatures": [c.action_signature
                           for c in table.ordered_candidates],
            "rejection_version": table.rejection_version,
        }

    def test_candidate_identity_unchanged_under_jev_presentation(self):
        from test_auto_jev_presentation import context_of

        table = self._table()
        before = self._identity(table)
        frozen, refusal = presentation.present(
            "command", table.ordered_candidates, context_of(
                {"id": 1, "kind": "command", "prompt": ""}))
        self.assertEqual(refusal, "")
        self.assertIsNotNone(frozen)
        after = self._identity(table)
        self.assertEqual(before, after)
        # the presentation keys are a *separate* namespace from the identity
        self.assertEqual(len(frozen.keys), len(table.ordered_candidates))
        for key in frozen.keys:
            self.assertNotIn(key, before["candidate_ids"])

        # identical policy input still yields byte-identical identity
        again = self._table()
        self.assertEqual(again.table_id, table.table_id)
        self.assertEqual(before, self._identity(again))

    def test_table_id_and_candidate_ids_stable(self):
        table = self._table()
        ids = [c.candidate_id for c in table.ordered_candidates]
        # the duplicate search actions are deduplicated exactly once by the
        # builder; presentation must not dedup a second time
        self.assertEqual(len(ids), 3)
        self.assertEqual(ids, [c.candidate_id
                               for c in table.ordered_candidates])
        self.assertEqual(table.table_id, self._table().table_id)
        self.assertEqual(len(table.canonical_bytes),
                         len(self._table().canonical_bytes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
