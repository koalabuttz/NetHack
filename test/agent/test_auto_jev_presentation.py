#!/usr/bin/env python3
"""Presentation tests for the Jev adapter migration.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

These lock in the *presentation* contract only: need-aware semantic option
keys and their closed normalization/alias tables (AC.1), the grounded
criterion templates with their refusal codes (AC.3, AC.4), the version
traceability (AC.9) and the confidence-gate semantics (AC.11).  No wire
request, no socket and no candidate policy is exercised here; the wire body
itself is covered by ``test_auto_providers.TestJevWireContract``.
"""

import json
import os
import sys
import time
import unittest
from dataclasses import replace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_auto import WireHarness, hello  # noqa: E402
from test_auto_providers import paced  # noqa: E402
from tools.agent import (candidates, controller, instances,  # noqa: E402
                         presentation, protocol, providers, recording, state)
from tools.agent.providers import (JevBuild, ProviderConfig,  # noqa: E402
                                   ReflexContext)

KEY = protocol


def cand(code, label, **kw):
    """One key-action candidate with a semantic label."""
    return candidates.make_candidate({"key": code}, label, **kw)


def table_of(cands, need_key=(1, 1, 1), reject=0):
    return candidates.build_table(need_key, 1, cands, rejection_version=reject)


def prepared_of(cands, need_key=(1, 1, 1)):
    return candidates.PreparedReflex(
        immutable_features=candidates.ReflexFeatures(),
        table=table_of(cands, need_key=need_key))


def context_of(need, *, hero=(5, 5), terrain=(), snapshot=(), intent="",
               directives=(), memory=None, tick=10, terrain_memory=None):
    """A live ReflexContext with the given remembered/current evidence."""
    snap = protocol.Snapshot()
    snap.map = {pos: cell for pos, cell in snapshot}
    if terrain_memory is not None:
        tm = terrain_memory
    else:
        tm = instances.TerrainMemory()
        items = terrain.items() if isinstance(terrain, dict) else terrain
        for pos, klass in items:
            tm.terrain[pos] = klass
    mem = memory if memory is not None else state.EpisodeMemory()
    mem.hero = hero
    return ReflexContext(
        episode=1, tick=tick, need=need,
        need_key=protocol.NeedKey(1, 1, need.get("id")), snapshot=snap,
        pages=[], memory=mem, terrain=tm, intent=intent,
        directives=list(directives), deadline=0.0)


COMMAND = {"id": 1, "kind": "command", "prompt": ""}
DIRECTION = {"id": 2, "kind": "direction", "prompt": "In what direction?"}
KEY_NEED = {"id": 3, "kind": "key", "prompt": ""}


def command_stem(label):
    return presentation.command_stem(label)


# ------------------------------------------------------------------ AC.1

class TestJevKeys(unittest.TestCase):
    """Need-aware semantic option keys and their closed contracts."""

    def test_keys_deterministic_and_unique(self):
        cands = [cand(KEY.KEY_H, "navigate"), cand(KEY.KEY_L, "navigate"),
                 cand(KEY.KEY_J, "search"), cand(KEY.KEY_K, "search")]
        first = presentation.option_keys("command", cands)
        second = presentation.option_keys("command", cands)
        self.assertEqual(first, second)
        keys, key_index, refusal = first
        self.assertEqual(refusal, "")
        self.assertEqual(len(keys), len(cands))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(sorted(key_index.values()), [0, 1, 2, 3])
        self.assertEqual([key_index[k] for k in keys], [0, 1, 2, 3])

    def test_alias_table_and_collision_suffixes(self):
        # the closed alias table maps each documented label to one stem
        self.assertEqual(command_stem("eat"), "eat-food")
        self.assertEqual(command_stem("descend"), "descend-stairs")
        self.assertEqual(command_stem("go-upstairs"), "go-upstairs")
        self.assertEqual(command_stem("ascend-stairs"), "go-upstairs")
        self.assertEqual(command_stem("inspect-inventory"), "inventory")
        self.assertEqual(command_stem("refresh-inventory"), "inventory")
        self.assertEqual(command_stem("rest"), "wait")
        self.assertEqual(command_stem("search"), "search-in-place")
        self.assertEqual(command_stem("search-secret"), "search-in-place")
        # never conflate the exceptional forced search with ordinary search
        self.assertEqual(command_stem("forced-search"), "forced-search")

        # a unique base key is unchanged; a collision group gets --1, --2 in
        # retained-table order
        pre = cand(KEY.KEY_SEARCH, "search")
        post = cand(KEY.KEY_SEARCH, "search")
        keys, key_index, refusal = presentation.option_keys(
            "command", [pre, post])
        self.assertEqual(refusal, "")
        self.assertEqual(keys, ["search-in-place--1", "search-in-place--2"])
        self.assertEqual(key_index, {"search-in-place--1": 0,
                                     "search-in-place--2": 1})
        alone = presentation.option_keys("command", [pre])[0]
        self.assertEqual(alone, ["search-in-place"])

        # the closed normalization contract: empty, non-ASCII and a
        # pre-existing "--" are rejected, never lossily aliased
        for label in ("", "!!!", "caf\u00e9", "food--safe", "-", "--"):
            self.assertIsNone(command_stem(label), label)
        self.assertEqual(command_stem("  Food Ration  "), "food-ration")
        self.assertEqual(command_stem("open door  south"), "open-door-south")

        # an unnormalizable label refuses the whole request
        bad = [cand(KEY.KEY_SEARCH, "!!"), cand(KEY.KEY_EAT, "??")]
        self.assertEqual(presentation.option_keys("command", bad)[:1], (None,))
        self.assertEqual(presentation.option_keys("command", bad)[2],
                         presentation.REFUSAL_INVALID_LABEL)

    def test_need_aware_direction_and_key_neutrality(self):
        moves = [cand(KEY.KEY_H, "navigate"), cand(KEY.KEY_L, "navigate"),
                 cand(KEY.KEY_J, "navigate"), cand(KEY.KEY_K, "navigate")]
        command_keys = presentation.option_keys("command", moves)[0]
        self.assertTrue(all(k.startswith("navigate-") for k in command_keys))
        direction_keys = presentation.option_keys("direction", moves)[0]
        self.assertTrue(all(k.startswith("direction-")
                            for k in direction_keys))
        key_keys = presentation.option_keys("key", moves)[0]
        self.assertTrue(all(k.startswith("key-") for k in key_keys))
        for keys in (direction_keys, key_keys):
            for k in keys:
                self.assertNotIn("navigate", k)
        # an unestablished direction binding refuses rather than fabricating
        nonmove = [cand(KEY.KEY_SEARCH, "search"), cand(KEY.KEY_EAT, "eat")]
        self.assertEqual(
            presentation.option_keys("direction", nonmove)[2],
            presentation.REFUSAL_MISSING_BINDING)
        # a key whose semantics cannot be established refuses too
        unknown = [cand(200, "x"), cand(201, "y")]
        self.assertEqual(presentation.option_keys("key", unknown)[2],
                         presentation.REFUSAL_UNSUPPORTED_SEMANTIC)

    def test_key_index_round_trip_after_json(self):
        cands = [cand(KEY.KEY_H, "inspect-inventory"), cand(KEY.KEY_L, "eat"),
                 cand(KEY.KEY_J, "search"), cand(KEY.KEY_K, "search")]
        frozen, refusal = presentation.present(
            "command", cands, context_of(COMMAND))
        self.assertEqual(refusal, "")
        criteria = dict(frozen.criteria)
        payload = {"questions": {"action": {"criteria": criteria}}}
        # the retained order survives an exact serialization round trip
        wire = json.dumps(payload)
        back = json.loads(wire)
        keys = list(back["questions"]["action"]["criteria"].keys())
        self.assertEqual(keys, list(frozen.keys))
        for key, index in frozen.key_index.items():
            self.assertEqual(keys.index(key), index)
            self.assertEqual(index, frozen.key_index[key])
        # and the same object identity binding is used for a returned key
        for key in keys:
            self.assertIn(key, frozen.key_index)

    def test_over_255_refused(self):
        many = [cand(KEY.KEY_H, "label-%d" % i) for i in range(256)]
        keys, key_index, refusal = presentation.option_keys("command", many)
        self.assertIsNone(keys)
        self.assertIsNone(key_index)
        self.assertEqual(refusal, presentation.REFUSAL_TOO_MANY)
        # the bound itself is inclusive
        ok = [cand(KEY.KEY_H, "label-%d" % i) for i in range(255)]
        self.assertEqual(presentation.option_keys("command", ok)[0] is not None,
                         True)
        # and a frozen table never silently truncates: it refuses
        frozen, why = presentation.present(
            "command", many, context_of(COMMAND))
        self.assertIsNone(frozen)
        self.assertEqual(why, presentation.REFUSAL_TOO_MANY)


# ------------------------------------------------------------------ AC.3

class TestJevRenderingRefusal(WireHarness):
    """Refusals are coded, whole-request, pre-reservation and endpoint-free."""

    def _reflex(self, base_url):
        cfg = ProviderConfig(reflex="jev", jev_accept_terms=True,
                             reflex_deadline=5.0, jev_base_url=base_url)
        return providers.JevReflex(cfg, jev_dispatch_enabled=True)

    def _runner(self, fake):
        cfg = ProviderConfig(max_ticks=200, reflex="jev", reflex_call_cap=5,
                             jev_accept_terms=True, postmortem_reserve=0,
                             confidence_threshold=0.8)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_reflex_provider = lambda reflex: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        runner.pending_key = protocol.NeedKey(1, 1, 1)
        runner.pending_seq = 1
        return runner, rec

    def _refusal_cases(self):
        """One (label, table, need) triple per closed refusal code."""
        one = [cand(KEY.KEY_H, "navigate")]
        bad_label = [cand(KEY.KEY_SEARCH, "!!"), cand(KEY.KEY_EAT, "??")]
        semantics = [candidates.make_candidate({"yn": ord("n")}, "prompt"),
                     candidates.make_candidate({"yn": ord("y")}, "prompt")]
        no_direction = [cand(KEY.KEY_SEARCH, "search"),
                        cand(KEY.KEY_EAT, "eat")]
        # A real CandidateTable is capped at 255 by construction, so the
        # over-bound guard is exercised against a directly-constructed frozen
        # table rather than one that could never be built.
        oversized = replace(table_of(one), ordered_candidates=tuple(one * 256))
        return [
            ("unsupported-need", table_of(one), {"id": 9, "kind": "menu",
                                                 "menu": "m", "mode": "one",
                                                 "content": "c", "pages": 1}),
            ("singleton", table_of(one), COMMAND),
            ("invalid-label", table_of(bad_label), COMMAND),
            ("unsupported-semantic", table_of(semantics), COMMAND),
            ("missing-required-binding", table_of(no_direction), DIRECTION),
            ("too-many-options", oversized, COMMAND),
        ]

    def test_each_refusal_code_recorded_pre_reservation(self):
        from test_auto_providers import FakeEndpoint

        with mock.patch.dict(os.environ, {"JEV_API_KEY": "jev-refusal"}):
            for code, table, need in self._refusal_cases():
                with self.subTest(code=code):
                    ep = FakeEndpoint()
                    self.addCleanup(ep.close)
                    fake = self._reflex(ep.base_url)
                    runner, rec = self._runner(fake)
                    runner.reflex.prepare = lambda ctx, t=table: (
                        candidates.PreparedReflex(
                            immutable_features=candidates.ReflexFeatures(),
                            table=t))
                    runner.pending_need = need
                    ctx = runner._reflex_context(need, None)
                    out = runner._decide_jev(ctx, None, time.monotonic())
                    _, provider, reason, _, _, low = out
                    rec.finalize({})
                    # the code reaches the decision record's reason, and it
                    # is the distinct code -- never the old generic message
                    self.assertEqual(provider, "scripted")
                    self.assertTrue(low)
                    self.assertEqual(reason, "jev skipped: %s" % code)
                    # zero reservation, zero dispatch, zero endpoint request
                    self.assertEqual(runner.ledger.reflex_paid_dispatched, 0)
                    self.assertEqual(runner.ledger.reflex_attempted, 0)
                    self.assertEqual(ep.requests, [])
                    self.assertEqual(fake.last_refusal, code)
                    fake.cancel()

    def test_refusal_zero_reservation_zero_endpoint_requests(self):
        from test_auto_providers import FakeEndpoint

        with mock.patch.dict(os.environ, {"JEV_API_KEY": "jev-refusal"}):
            ep = FakeEndpoint()
            self.addCleanup(ep.close)
            fake = self._reflex(ep.base_url)
            table = table_of([cand(KEY.KEY_H, "navigate")])
            runner, _ = self._runner(fake)
            runner.reflex.prepare = lambda ctx: candidates.PreparedReflex(
                immutable_features=candidates.ReflexFeatures(), table=table)
            runner.pending_need = COMMAND
            ctx = runner._reflex_context(COMMAND, None)
            runner._decide_jev(ctx, None, time.monotonic())
            self.assertEqual(fake.last_refusal, "singleton")
            self.assertEqual(ep.requests, [])
            self.assertEqual(runner.ledger.reflex_paid_dispatched, 0)
            fake.cancel()

    def test_optional_evidence_missing_degrades_without_refusal(self):
        # absent terrain classification, route purpose and occupant data are
        # *optional*: the shorter template is used and nothing refuses
        ctx = context_of(COMMAND, terrain_memory=None)
        move = cand(KEY.KEY_L, "navigate", reason="navigate: something else")
        ctx.terrain = None
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east.")
        # with a classified terrain but no occupied evidence, still no clause
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_FLOOR})
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east onto remembered room floor.")
        # and a full table with optional evidence missing still builds
        frozen, refusal = presentation.present(
            "command", [move, cand(KEY.KEY_H, "navigate")], ctx)
        self.assertEqual(refusal, "")
        self.assertIsNotNone(frozen)


# ------------------------------------------------------------------ AC.4

class TestJevCriteria(unittest.TestCase):
    """The criterion templates are grounded in the right evidence."""

    def test_terrain_under_current_occupant_preserved(self):
        # remembered floor, currently covered by a visible creature: the
        # *ground* comes from the persistent classification and survives,
        # while occupancy comes only from the current snapshot
        ctx = context_of(
            COMMAND, terrain={(6, 5): instances.T_FLOOR},
            snapshot={((6, 5), ("d", "brown", "", ""))})
        # the raw EpisodeMemory grid is deliberately *not* the glyph source
        ctx.memory.grid[(6, 5)] = ("d", "brown", "", "")
        move = cand(KEY.KEY_L, "navigate",
                    reason="navigate: observation frontier")
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertIn("Walk east onto remembered room floor.", text)
        self.assertIn(presentation.OCCUPANT_CLAUSE, text)
        self.assertIn("Approach the edge of explored terrain", text)
        # no raw monster glyph is emitted as a creature name or glyph token,
        # and no hostility is inferred from the appearance
        for glyph in ("'", "&", ";", ":", "~", "]", "d", "D"):
            self.assertNotRegex(text, r"(?<![A-Za-z])%s(?![A-Za-z])"
                                % glyph)
        for claim in ("hostile", "tame", "peaceful", "friendly"):
            self.assertNotIn(claim, text)

    def test_stale_remembered_monster_not_current(self):
        # a remembered occupant absent from the current snapshot is never
        # described as currently present
        remembered = instances.TerrainMemory()
        remembered.terrain[(6, 5)] = instances.T_FLOOR
        remembered.occupancy[(6, 5)] = instances.OCC_MONSTER
        ctx = context_of(COMMAND, terrain_memory=remembered)
        move = cand(KEY.KEY_L, "navigate")
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east onto remembered room floor.")
        self.assertNotIn(presentation.OCCUPANT_CLAUSE, text)

    def test_eat_without_binding(self):
        ctx = context_of(COMMAND)
        # even with a fresh cached inventory, the command is never bound to
        # an item: the cache is not exact evidence
        ctx.memory.inventory.rows = [{"text": "a food ration",
                                      "selectable": True}]
        ctx.memory.inventory.seen_tick = 1
        eat = cand(KEY.KEY_EAT, "eat", reason="hungry: attempt to eat")
        text, refusal = presentation.render_criterion(eat, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, presentation.EAT_INITIATE_TEMPLATE)
        self.assertNotIn("ration", text)
        for claim in ("safe", "uncursed", "blessed", "fresh", "identified"):
            self.assertNotIn(claim, text)
        # an exact frozen binding does name the item
        bound = cand(KEY.KEY_EAT, "eat",
                     effect_payload=({"item": "a food ration"},))
        text, _ = presentation.render_criterion(bound, "command", ctx)
        self.assertEqual(text, "Eat a food ration.")

    def test_direction_need_never_claims_walking(self):
        ctx = context_of(DIRECTION, terrain={(6, 5): instances.T_FLOOR})
        move = cand(KEY.KEY_L, "navigate", reason="navigate: observation "
                                                   "frontier")
        text, refusal = presentation.render_criterion(move, "direction", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Choose east for the pending action.")
        self.assertNotIn("Walk", text)
        self.assertNotIn("open", text.lower())
        key_text, refusal = presentation.render_criterion(move, "key", ctx)
        self.assertEqual(refusal, "")
        self.assertNotIn("Walk", key_text)
        self.assertNotIn("open", key_text.lower())

    def test_adjacent_door_as_movement(self):
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_CLOSED_DOOR})
        move = cand(KEY.KEY_L, "navigate", reason="navigate: approach a "
                                                   "closed door")
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertTrue(text.startswith(
            "Move east toward the adjacent closed door; it may block "
            "movement."))
        # never a promise of an explicit open command
        self.assertNotIn("open the door", text)
        self.assertNotIn("locked", text)
        self.assertIn("Approach a known closed door.", text)

    def test_unknown_reason_omitted(self):
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_FLOOR})
        move = cand(KEY.KEY_L, "navigate", reason="navigate: raw metadata 12")
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east onto remembered room floor.")
        self.assertNotIn("metadata", text)
        # the recognized reason under the existing navigate(<goal>) prefix
        recognized = cand(KEY.KEY_L, "navigate",
                          reason="navigate (survive): reachable down stairs")
        text, _ = presentation.render_criterion(recognized, "command", ctx)
        self.assertIn("Follow a route toward known stairs down.", text)

    def test_quit_conspicuous(self):
        ctx = context_of(COMMAND, intent="quit")
        quit_cand = cand(KEY.KEY_HASH, "quit", reason="quit")
        text, refusal = presentation.render_criterion(quit_cand, "command",
                                                      ctx)
        self.assertEqual(refusal, "")
        self.assertIn("this will end the run if confirmed", text)
        # a direct bound quit action is equally explicit
        bound = candidates.make_candidate({"text": "quit"}, "quit")
        text, _ = presentation.render_criterion(bound, "command", ctx)
        self.assertEqual(text, presentation.QUIT_BOUND_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
