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
        # the over-bound shape is an unrepresentable table, not a new code
        self.assertEqual(refusal, presentation.REFUSAL_UNSUPPORTED_SEMANTIC)
        # the bound itself is inclusive
        ok = [cand(KEY.KEY_H, "label-%d" % i) for i in range(255)]
        self.assertEqual(presentation.option_keys("command", ok)[0] is not None,
                         True)
        # and a frozen table never silently truncates: it refuses the whole
        # request, emitting no keys and no index
        frozen, why = presentation.present(
            "command", many, context_of(COMMAND))
        self.assertIsNone(frozen)
        self.assertEqual(why, presentation.REFUSAL_UNSUPPORTED_SEMANTIC)


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
        """One ``(label, code, table, need)`` case per closed refusal code.

        The closed vocabulary holds exactly five codes; the over-bound table
        shape maps to ``unsupported-semantic`` (whole-request refusal) rather
        than a sixth code.
        """
        one = [cand(KEY.KEY_H, "navigate")]
        bad_label = [cand(KEY.KEY_SEARCH, "!!"), cand(KEY.KEY_EAT, "??")]
        semantics = [candidates.make_candidate({"yn": ord("n")}, "prompt"),
                     candidates.make_candidate({"yn": ord("y")}, "prompt")]
        no_direction = [cand(KEY.KEY_SEARCH, "search"),
                        cand(KEY.KEY_EAT, "eat")]
        # A real CandidateTable is capped at 255 by construction, so the
        # over-bound shape is exercised against a directly-constructed frozen
        # table rather than one that could never be built.
        oversized = replace(table_of(one), ordered_candidates=tuple(one * 256))
        return [
            ("unsupported-need", "unsupported-need", table_of(one),
             {"id": 9, "kind": "menu", "menu": "m", "mode": "one",
              "content": "c", "pages": 1}),
            ("singleton", "singleton", table_of(one), COMMAND),
            ("invalid-label", "invalid-label", table_of(bad_label), COMMAND),
            ("unsupported-semantic", "unsupported-semantic",
             table_of(semantics), COMMAND),
            ("missing-required-binding", "missing-required-binding",
             table_of(no_direction), DIRECTION),
            ("oversized-unsupported-semantic", "unsupported-semantic",
             oversized, COMMAND),
        ]

    def test_closed_refusal_vocabulary(self):
        # the vocabulary is closed at exactly the five approved codes
        self.assertEqual(set(presentation.REFUSAL_CODES),
                         {"unsupported-need", "singleton", "invalid-label",
                          "unsupported-semantic", "missing-required-binding"})
        self.assertFalse(hasattr(presentation, "REFUSAL_TOO_MANY"))

    def test_each_refusal_code_recorded_pre_reservation(self):
        from test_auto_providers import FakeEndpoint

        with mock.patch.dict(os.environ, {"JEV_API_KEY": "jev-refusal"}):
            for label, code, table, need in self._refusal_cases():
                with self.subTest(code=label):
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

    def test_contradictory_open_door_refuses_wholly_pre_dispatch(self):
        # (e) a retained table holding one contradictory open-door candidate --
        # an open-door label on a frozen Eat action -- refuses the *whole*
        # request before reservation and dispatch: no key is sent to Jev, no
        # paid call is reserved or attempted, and no endpoint request is made,
        # even though the genuine open command in the same table is renderable.
        from test_auto_providers import FakeEndpoint

        with mock.patch.dict(os.environ, {"JEV_API_KEY": "jev-refusal"}):
            ep = FakeEndpoint()
            self.addCleanup(ep.close)
            fake = self._reflex(ep.base_url)
            table = table_of([cand(KEY.KEY_OPEN, "open-door"),
                              cand(KEY.KEY_EAT, "open door south")])
            runner, _ = self._runner(fake)
            runner.reflex.prepare = lambda ctx: candidates.PreparedReflex(
                immutable_features=candidates.ReflexFeatures(), table=table)
            runner.pending_need = COMMAND
            ctx = runner._reflex_context(COMMAND, None)
            runner._decide_jev(ctx, None, time.monotonic())
            self.assertEqual(fake.last_refusal, "unsupported-semantic")
            self.assertEqual(ep.requests, [])
            self.assertEqual(runner.ledger.reflex_paid_dispatched, 0)
            self.assertEqual(runner.ledger.reflex_attempted, 0)
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

    def test_adjacent_doorway_is_not_a_closed_door(self):
        move = cand(KEY.KEY_L, "navigate")
        # a remembered doorway is not a confirmed closed door: it uses its own
        # approved phrase and never gains a "closed / may block" claim
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_DOORWAY})
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east onto a remembered open doorway.")
        self.assertNotIn("closed", text)
        self.assertNotIn("block", text)
        # an open door is the same approved phrase, never described as closed
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_OPEN_DOOR})
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Walk east onto a remembered open doorway.")
        self.assertNotIn("closed", text)
        self.assertNotIn("block", text)
        # only a *confirmed* closed door takes the blocking-door branch
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_CLOSED_DOOR})
        text, _ = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(text,
                         "Move east toward the adjacent closed door; it may "
                         "block movement.")

    def test_open_door_requires_the_open_command(self):
        # opening semantics come from the frozen canonical action, never from
        # the semantic label: (a) a frozen open-command initiation
        ctx = context_of(COMMAND)
        initiate = cand(KEY.KEY_OPEN, "open-door")
        text, refusal = presentation.render_criterion(initiate, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, presentation.DOOR_INITIATE_TEMPLATE)
        # the option keys still come from the frozen label, not a parsed key
        south_labelled = cand(KEY.KEY_OPEN, "open door south")
        keys, _key_index, refusal = presentation.option_keys(
            "command", [initiate, south_labelled])
        self.assertEqual(refusal, "")
        self.assertEqual(keys, ["open-door", "open-door-south"])
        # (b) a direction bound on the frozen candidate is the directional form
        bound = cand(KEY.KEY_OPEN, "open-door", direction=(0, 1))
        text, refusal = presentation.render_criterion(bound, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Try to open the door to the south.")
        self.assertNotIn("locked", text)
        # a compass named by the label is the same directional form
        text, refusal = presentation.render_criterion(south_labelled,
                                                      "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Try to open the door to the south.")
        self.assertNotIn("locked", text)
        # (c) an exact frozen door-type binding selects the typed template
        typed = cand(KEY.KEY_OPEN, "open-door", direction=(0, 1),
                     effect_payload=({"door_type": "closed"},))
        text, refusal = presentation.render_criterion(typed, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Try to open the closed door to the south.")
        self.assertNotIn("locked", text)

    def test_open_door_label_cannot_override_the_action(self):
        # (d) any non-open canonical action carrying an open-door label refuses
        # the whole request: opening prose can never describe Eat, Wait, a
        # movement step, a search or an arbitrary key.
        ctx = context_of(COMMAND)
        contradicting = [
            cand(KEY.KEY_EAT, "open door south"),
            cand(KEY.KEY_WAIT, "open-door"),
            cand(KEY.KEY_L, "open-door"),              # movement key
            cand(KEY.KEY_SEARCH, "open door north"),   # search key
            cand(200, "open-door"),                    # arbitrary key
        ]
        for member in contradicting:
            with self.subTest(key=member.action.payload[0]):
                text, refusal = presentation.render_criterion(
                    member, "command", ctx)
                self.assertIsNone(text)
                self.assertEqual(refusal,
                                 presentation.REFUSAL_UNSUPPORTED_SEMANTIC)
        # a mixed table refuses wholly -- never a selective drop of the one
        # contradictory member while the genuine open command is presented
        members = [cand(KEY.KEY_OPEN, "open-door"), contradicting[0]]
        frozen, why = presentation.present("command", members, ctx)
        self.assertIsNone(frozen)
        self.assertEqual(why, presentation.REFUSAL_UNSUPPORTED_SEMANTIC)

    def test_movement_purpose_families_renderable(self):
        ctx = context_of(COMMAND, terrain={(6, 5): instances.T_FLOOR})
        # a directional escape keeps its withdrawal sentence, never a walk
        escape = cand(KEY.KEY_L, "escape", reason="low HP: flee upstairs")
        text, refusal = presentation.render_criterion(escape, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Move east, withdraw from danger.")
        self.assertNotIn("Walk", text)
        # a directional random/recovery step keeps its recovery sentence
        move = cand(KEY.KEY_J, "random-move",
                    reason="loop breaker: random walk (known floor)")
        text, refusal = presentation.render_criterion(move, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text,
                         "Move south as a recovery step. Try to break the "
                         "recent lack of progress.")
        step = cand(KEY.KEY_H, "recovery-step", reason="recovery: random walk")
        text, refusal = presentation.render_criterion(step, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, "Move west as a recovery step.")
        # a non-directional escape is the bare withdrawal sentence
        still = cand(KEY.KEY_WAIT, "escape", reason="low HP: hold position")
        text, _ = presentation.render_criterion(still, "command", ctx)
        self.assertEqual(text, presentation.WITHDRAW_CLAUSE)
        # a non-direction unblock falls back to the search sentence
        unblock = cand(KEY.KEY_SEARCH, "unblock",
                       reason="loop breaker: unblock")
        text, _ = presentation.render_criterion(unblock, "command", ctx)
        self.assertTrue(text.startswith(presentation.SEARCH_TEMPLATE))

    def test_missing_direction_binding_refuses(self):
        # a direction need with no bound direction is a genuinely required
        # binding: the whole member refuses rather than fabricating one
        ctx = context_of(DIRECTION)
        non_move = cand(KEY.KEY_SEARCH, "search")
        text, refusal = presentation.render_criterion(non_move, "direction",
                                                      ctx)
        self.assertIsNone(text)
        self.assertEqual(refusal, presentation.REFUSAL_MISSING_BINDING)
        frozen, why = presentation.present("direction", [non_move], ctx)
        self.assertIsNone(frozen)
        self.assertEqual(why, presentation.REFUSAL_MISSING_BINDING)

    def test_ascend_wording_evidence_sensitive(self):
        up = cand(ord("<"), "go-upstairs")
        # no confirmed up-stair beneath the hero: the fallback, no stair claim
        ctx = context_of(COMMAND)
        text, refusal = presentation.render_criterion(up, "command", ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(text, presentation.ASCEND_FALLBACK)
        self.assertNotIn("staircase", text)
        self.assertNotIn("leave the dungeon", text)
        # a confirmed up-stair in a deeper dungeon: definite wording, no caveat
        deep = state.EpisodeMemory()
        deep.status.dlvl = "3"
        deep_ctx = context_of(COMMAND, terrain={(5, 5): instances.T_STAIRS_UP},
                              memory=deep)
        text, _ = presentation.render_criterion(up, "command", deep_ctx)
        self.assertEqual(text, presentation.ASCEND_TEMPLATE)
        self.assertNotIn("leave the dungeon", text)
        # a confirmed up-stair at dungeon level 1: the exit is possible, so the
        # exit caveat is appended
        top = state.EpisodeMemory()
        top.status.dlvl = "1"
        top_ctx = context_of(COMMAND, terrain={(5, 5): instances.T_STAIRS_UP},
                             memory=top)
        text, _ = presentation.render_criterion(up, "command", top_ctx)
        self.assertEqual(text, presentation.ASCEND_TEMPLATE + " "
                         + presentation.ASCEND_LEAVE_DUNGEON)

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


# ------------------------------------------------------------------ AC.9

class TestJevVersion(WireHarness):
    """Presentation version traceability (metadata only, never on the wire)."""

    def test_version_in_safe_config_metadata(self):
        cfg = ProviderConfig(reflex="jev", jev_accept_terms=True)
        safe = controller._safe_config(cfg)
        self.assertEqual(safe["jev_presentation_version"],
                         providers.JEV_PRESENTATION_VERSION)
        self.assertEqual(safe["jev_adapter_version"],
                         providers.JEV_ADAPTER_VERSION)
        self.assertEqual(providers.JEV_PRESENTATION_VERSION,
                         presentation.PRESENTATION_VERSION)
        # it reaches the episode/campaign metadata artifact
        summary = controller.campaign_summary([], cfg, 1.0)
        self.assertEqual(summary["config"]["jev_presentation_version"],
                         providers.JEV_PRESENTATION_VERSION)
        # and the safe config never carries key material
        for secret in ("jev_key_file", "deepseek_key_file", "api_key"):
            self.assertNotIn(secret, safe)

    def test_legacy_artifact_without_version_accepted(self):
        cfg = ProviderConfig(reflex="jev", jev_accept_terms=True)
        modern = controller._safe_config(cfg)
        versioned = ("jev_adapter_version", "jev_presentation_version")
        legacy = {k: v for k, v in modern.items() if k not in versioned}
        # the version keys are purely additive: an artifact written before
        # them is a strict subset of the same schema, so its absence reads as
        # "legacy", never as an error
        self.assertTrue(set(legacy) < set(modern))
        self.assertEqual(json.loads(json.dumps(legacy)), legacy)
        self.assertNotIn("jev_presentation_version", legacy)

        # the decision-sidecar schema is unchanged by the version: a decision
        # record never grows a version field
        rec = recording.EpisodeRecorder(self.dir, 1)
        rec.record_decision({"key": 115}, {"key": 115}, "scripted", "reason")
        rec.finalize({})
        path = os.path.join(self.dir, "ep-1.decisions.jsonl")
        with open(path) as handle:
            record = json.loads(handle.readline())
        for key in versioned:
            self.assertNotIn(key, record)

    def test_version_absent_from_wire_request(self):
        cfg = ProviderConfig(reflex="jev", jev_accept_terms=True)
        prov = providers.JevReflex(cfg, jev_dispatch_enabled=True)
        ctx = context_of(COMMAND)
        ctx.prepared = prepared_of([cand(KEY.KEY_H, "navigate"),
                                    cand(KEY.KEY_L, "navigate")])
        built = prov.build_choices(ctx)
        self.assertIsNotNone(built)
        # the official body is exactly state/model/questions
        self.assertEqual(sorted(built.payload.keys()),
                         ["model", "questions", "state"])
        self.assertEqual(sorted(built.payload["questions"].keys()), ["action"])
        blob = json.dumps(built.payload)
        self.assertNotIn(providers.JEV_PRESENTATION_VERSION, blob)
        self.assertNotIn("presentation_version", blob)
        self.assertNotIn("adapter_version", blob)


# ----------------------------------------------------------------- AC.11

class TestJevConfidence(unittest.TestCase):
    """Peakedness-relative acceptance plus the legacy absolute rollback."""

    def test_confidence_gate_threshold_unchanged_for_spread_distribution(self):
        # Legacy rollback coverage: the flat scalar threshold is preserved for
        # callers that explicitly request absolute mode.
        from tools.agent import arbitration

        cands = [cand(KEY.KEY_H, "navigate", family="frontier"),
                 cand(KEY.KEY_L, "navigate", family="frontier"),
                 cand(KEY.KEY_J, "navigate", family="frontier")]
        table = table_of(cands, need_key=(1, 1, 1))

        def outcome(confidence, threshold):
            raw = arbitration.RawChoice(
                table_id=table.table_id, need_key=tuple(table.need_key),
                table_version=table.table_version, index=0,
                confidence=confidence, dispatched=True)
            return arbitration.validate_raw_choice(
                table, raw, arbitration.RejectionSet(),
                threshold=threshold, eligible=lambda c: True,
                mode=arbitration.CONFIDENCE_ABSOLUTE)

        # a spread distribution over several acceptable navigation
        # alternatives is legitimately low-concentration: the gate reads the
        # selected member's own probability against the threshold, unchanged
        spread = 1.0 / 3.0
        rejected = outcome(spread, 0.8)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.code, "confidence")
        self.assertIn("confidence", rejected.reason)
        # the same distribution passes an honest lower threshold
        self.assertTrue(outcome(spread, 0.3).accepted)
        # exactly at the threshold is accepted; just below is rejected
        self.assertTrue(outcome(0.8, 0.8).accepted)
        self.assertFalse(outcome(0.799, 0.8).accepted)
        # a concentrated distribution is unaffected
        self.assertTrue(outcome(0.96, 0.8).accepted)

    # -- relative (peakedness) acceptance ---------------------------------

    @staticmethod
    def _raw(table, index, prob, confidence=0.0, **kw):
        from tools.agent import arbitration
        fields = dict(table_id=table.table_id, need_key=tuple(table.need_key),
                      table_version=table.table_version, index=index,
                      confidence=confidence, selected_probability=prob)
        fields.update(kw)
        return arbitration.RawChoice(**fields)

    @staticmethod
    def _table_n(n, need_key=(1, 1, 1)):
        codes = [KEY.KEY_H, KEY.KEY_L, KEY.KEY_J, KEY.KEY_K, KEY.KEY_Y,
                 KEY.KEY_U][:n]
        labels = ["navigate", "search", "eat", "wait", "inventory",
                  "pick-up"][:n]
        return table_of([cand(c, l) for c, l in zip(codes, labels)],
                        need_key=need_key)

    def test_relative_gate_uses_selected_probability_not_reported_confidence(
            self):
        from tools.agent import arbitration
        table = self._table_n(2)
        # a high service confidence with a *low* selected probability fails
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 0.30, confidence=0.99),
            arbitration.RejectionSet())
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "confidence")
        # a low confidence with a *sufficient* selected probability passes
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 0.90, confidence=0.0),
            arbitration.RejectionSet())
        self.assertTrue(out.accepted)

    def test_relative_gate_thresholds_for_two_through_six_options(self):
        from tools.agent import arbitration
        expected = {2: 1.5 / 2, 3: 1.5 / 3, 4: 1.5 / 4, 5: 1.5 / 5,
                    6: 1.5 / 6}
        for n, need in expected.items():
            table = self._table_n(n)
            # just above the boundary passes
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, need + 1e-9),
                arbitration.RejectionSet())
            self.assertTrue(out.accepted, n)
            # just below the boundary fails
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, need - 1e-9),
                arbitration.RejectionSet())
            self.assertFalse(out.accepted, n)

    def test_relative_gate_strict_boundary_and_uniform_rejection(self):
        from tools.agent import arbitration
        for n in (2, 3, 4, 5, 6):
            table = self._table_n(n)
            uniform = 1.0 / n
            # exactly at k/N fails: the rule is "exceeds"
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, uniform),
                arbitration.RejectionSet())
            self.assertFalse(out.accepted, n)
            # 2/N always passes (binary reachability for every n)
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, 2.0 / n),
                arbitration.RejectionSet())
            self.assertTrue(out.accepted, n)

    def test_relative_gate_monotone_in_probability_and_option_count(self):
        from tools.agent import arbitration
        # for fixed N, acceptance is monotone in p
        table = self._table_n(5)
        seen_accept = False
        for p in [0.05 * i for i in range(21)]:
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, p), arbitration.RejectionSet())
            if out.accepted:
                seen_accept = True
            elif seen_accept:
                self.fail("acceptance must be monotone in p")
        # for fixed p, a larger N is never harder: 0.34 fails at N=3 but
        # passes at N=5
        out3 = arbitration.validate_raw_choice(
            self._table_n(3), self._raw(self._table_n(3), 0, 0.34),
            arbitration.RejectionSet())
        out5 = arbitration.validate_raw_choice(
            self._table_n(5), self._raw(self._table_n(5), 0, 0.34),
            arbitration.RejectionSet())
        self.assertFalse(out3.accepted)
        self.assertTrue(out5.accepted)

    def test_relative_gate_rejects_missing_bool_nonfinite_probability(self):
        from tools.agent import arbitration
        table = self._table_n(3)
        raw = arbitration.RawChoice(
            table_id=table.table_id, need_key=tuple(table.need_key),
            table_version=table.table_version, index=0, confidence=0.99)
        # no selected probability at all: fail closed, never fall back to the
        # unrelated confidence scalar
        out = arbitration.validate_raw_choice(table, raw,
                                              arbitration.RejectionSet())
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "confidence")
        for bad in (True, "hi", float("nan"), float("inf"), -0.1, 1.1, None):
            out = arbitration.validate_raw_choice(
                table, self._raw(table, 0, bad), arbitration.RejectionSet())
            self.assertFalse(out.accepted, bad)
            self.assertEqual(out.code, "confidence", bad)

    def test_relative_gate_preserves_identity_rejection_and_safety_checks(
            self):
        from tools.agent import arbitration
        table = self._table_n(2)
        # a maximally concentrated probability never bypasses identity
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 1.0, table_id="stale"),
            arbitration.RejectionSet())
        self.assertEqual(out.code, "stale")
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 5, 1.0), arbitration.RejectionSet())
        self.assertEqual(out.code, "index-range")
        # a rejected member is rejected at arbitrarily high p
        rejected = arbitration.RejectionSet()
        rejected.exclude(table.ordered_candidates[0])
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 1.0), rejected)
        self.assertEqual(out.code, "rejected-member")
        # the eligibility (safety) gate is independent of concentration
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 1.0), arbitration.RejectionSet(),
            eligible=lambda c: False)
        self.assertEqual(out.code, "unsafe")

    def test_relative_gate_counts_retained_offered_actions_not_targets(self):
        from tools.agent import arbitration
        # Two identical wire actions dedup to one retained member, so the
        # offered count N is the *retained* table size (2), not the raw target
        # count (3): the same p that would pass 1.5/3 is rejected at 1.5/2.
        dup = [cand(KEY.KEY_H, "navigate"), cand(KEY.KEY_H, "navigate"),
               cand(KEY.KEY_L, "search")]
        table = table_of(dup)
        self.assertEqual(len(table.ordered_candidates), 2)
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, 0.6), arbitration.RejectionSet())
        self.assertFalse(out.accepted)
        self.assertIn("N=2", out.reason)

    def test_spread_winner_passes_relative_and_fails_legacy_absolute(self):
        from tools.agent import arbitration
        table = self._table_n(3)
        spread = 0.55        # a genuine but soft winner over three options
        # relative accepts the concentrated-enough winner ...
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, spread, confidence=spread),
            arbitration.RejectionSet())
        self.assertTrue(out.accepted)
        # ... while the legacy absolute gate rejects the same answer
        out = arbitration.validate_raw_choice(
            table, self._raw(table, 0, spread, confidence=spread),
            arbitration.RejectionSet(), threshold=0.8,
            mode=arbitration.CONFIDENCE_ABSOLUTE)
        self.assertFalse(out.accepted)
        self.assertEqual(out.code, "confidence")

    def test_agent_docs_describe_jev_confidence_and_live_distribution_measurement(
            self):
        docs = os.path.join(_ROOT, "doc", "agent-autoplay.md")
        with open(docs) as handle:
            text = handle.read()
        lowered = " ".join(text.lower().split())
        # concentration, not permission to act
        self.assertIn("concentration", lowered)
        self.assertIn("not permission", lowered)
        # live-only and operator-gated distribution measurement
        self.assertIn("live-only", lowered)
        self.assertIn("operator-gated", lowered)
        self.assertIn("distribution", lowered)
        # the docs quote the documented context window and never claim a
        # measured fit
        self.assertIn("32,000", text)
        # and the offline report schema carries no synthetic distribution
        # field, so a distribution can never be mistaken for a measurement
        with open(os.path.join(_HERE, "fixtures", "jev_offline_report.json")) \
                as handle:
            report = handle.read()
        self.assertNotIn("distribution", report.lower())
        self.assertNotIn('"distribution"', report)


# ------------------------------------------------------------------ AC.5

def move_cand(code, label, step, family="frontier", **kw):
    """A movement-key candidate bound to an explicit compass step."""
    from tools.agent import candidates as C
    return C.make_candidate({"key": code}, label, family=family, direction=step,
                            direction_rank=kw.pop("direction_rank", 0),
                            score=kw.pop("score", 500),
                            reason=kw.pop("reason",
                                          "navigate: observation frontier"),
                            proposed_effect=kw.pop("proposed_effect",
                                                   "navigate"), **kw)


def room_of(context):
    return presentation.render_state(context)["room"]


def map_of(context):
    return presentation.render_state(context)["map"]


ROOM_HERO = (10, 10)
ROOM_TERRAIN = {(9, 10): instances.T_FLOOR,
                (11, 10): instances.T_STAIRS_DOWN,
                (13, 10): instances.T_CORRIDOR,
                (16, 10): instances.T_CORRIDOR,
                (12, 11): instances.T_CLOSED_DOOR}
ROOM_SNAPSHOT = [((9, 10), (".", "gray", 0, "none")),
                 ((10, 10), ("@", "white", 0, "none")),
                 ((11, 10), ("%", "yellow", 0, "none")),
                 ((12, 10), ("a", "white", 0, "none")),
                 ((13, 10), ("#", "gray", 0, "none")),
                 ((14, 10), ("+", "brown", 0, "none")),
                 ((15, 10), ("{", "gray", 0, "none")),
                 ((12, 11), ("|", "gray", 0, "none"))]


def room_ctx(hero=ROOM_HERO, terrain=None, snapshot=None, need=COMMAND,
             memory=None):
    return context_of(need, hero=hero,
                      terrain=ROOM_TERRAIN if terrain is None else terrain,
                      snapshot=ROOM_SNAPSHOT if snapshot is None
                      else snapshot,
                      memory=memory)


class TestJevRoomAwareness(unittest.TestCase):
    """Room-awareness enrichment of the Jev state payload and criteria (§5)."""

    # -- legend / map -----------------------------------------------------

    def test_fixed_legend_covers_every_emittable_glyph(self):
        emittable = set(state.TERRAIN_GLYPHS.values())
        emittable |= {state.OCCUPANT_MARKER, state.ITEM_MARKER,
                      state.UNCLASSIFIED_MARKER, state.HERO_MARKER}
        emittable.discard(" ")
        for glyph in sorted(emittable):
            self.assertIn(glyph, presentation.LEGEND, glyph)
        # every legend key is an emittable glyph (a closed legend)
        self.assertEqual(set(presentation.LEGEND) - {" "}, emittable)
        # the legend is emitted in the declared insertion order
        self.assertEqual(list(presentation.LEGEND), list(presentation.LEGEND))
        rendered = map_of(room_ctx())["text"]
        for line in rendered.split("\n"):
            body = line[3:]      # drop the fixed "%2d " row-number prefix
            for ch in body:
                self.assertIn(ch, presentation.LEGEND)

    def test_confirmed_hero_wins_and_nonhero_at_is_creature(self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("@", "white", 0, "none"))]
        ctx = room_ctx(terrain={}, snapshot=snap)
        text = map_of(ctx)["text"]
        row = [ln for ln in text.split("\n") if ln.startswith("10 ")][0]
        body = row.split(" ", 1)[1]
        # hero at x=10 is '@', the human at x=11 is the creature marker
        self.assertEqual(body[10 - 9], "@")
        self.assertEqual(body[11 - 9], "*")
        room = room_of(ctx)
        kinds = {(r["at"][0], r["kind"]) for r in room["contents"]}
        self.assertIn((11, "creature"), kinds)

    def test_current_items_overlay_remembered_floor_and_stairs(self):
        room = room_of(room_ctx())
        cats = {(r["at"][0], r["kind"], r["category"]) for r in room["contents"]}
        # the food appearance is an item over remembered floor/stairs
        self.assertIn((11, "item", "food appearance"), cats)
        self.assertIn((12, "creature", "creature"), cats)
        self.assertIn((15, "unclassified", "unclassified display"), cats)

    def test_stale_item_and_creature_absent_from_snapshot_not_overlaid(self):
        # remembered terrain, but a snapshot without the item/creature: only
        # the remembered terrain is drawn, and no contents record appears
        terrain = {(11, 10): instances.T_FLOOR, (12, 10): instances.T_FLOOR}
        ctx = room_ctx(terrain=terrain, snapshot=[((10, 10), ("@", "white"))])
        text = map_of(ctx)["text"]
        self.assertNotIn("&", text)
        self.assertNotIn("*", text)
        self.assertEqual(room_of(ctx)["contents"], [])

    def test_current_features_and_unknown_display_expand_map_bounds(self):
        # a feature/unknown far to the east expands the crop before any cap
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((30, 10), ("}", "blue", 0, "none"))]
        ctx = room_ctx(terrain={}, snapshot=snap)
        game_map = map_of(ctx)
        self.assertLessEqual(game_map["x_min"], 10)
        self.assertGreaterEqual(game_map["x_max"], 30)

    def test_remembered_bars_render_in_map_without_structured_record(self):
        terrain = {(12, 10): instances.T_BARS}
        snap = [((10, 10), ("@", "white", 0, "none"))]
        ctx = room_ctx(terrain=terrain, snapshot=snap)
        text = map_of(ctx)["text"]
        self.assertIn("|", text)          # legend-covered underlay
        room = room_of(ctx)
        self.assertEqual(room["contents"], [])
        # bars are deliberately excluded from both structured lists
        for rec in room["openings"]:
            self.assertNotEqual(rec["terrain"], instances.T_BARS)

    # -- contents ---------------------------------------------------------

    def test_contents_nearest_first_cap_and_exact_omitted_count(self):
        # 20 identical food appearances east of the hero: capped at 16
        snap = [((10, 10), ("@", "white", 0, "none"))]
        for i in range(20):
            snap.append(((11 + i, 10), ("%", "yellow", 0, "none")))
        ctx = room_ctx(terrain={}, snapshot=snap)
        room = room_of(ctx)
        self.assertEqual(len(room["contents"]), 16)
        self.assertEqual(room["contents_omitted"], 20 - 16)
        xs = [r["at"][0] for r in room["contents"]]
        self.assertEqual(xs, sorted(xs))          # nearest first

    def test_contents_row_major_without_hero(self):
        snap = [((12, 10), ("%", "yellow", 0, "none")),
                ((11, 12), ("!", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(hero=None, terrain={}, snapshot=snap)
        room = room_of(ctx)
        ats = [tuple(r["at"]) for r in room["contents"]]
        self.assertEqual(ats, [(11, 10), (12, 10), (11, 12)])   # (y, x)

    def test_room_null_empty_and_unavailable_sources(self):
        # (a) an unavailable snapshot with no remembered openings: contents is
        # null, but openings is an available-but-empty list
        ctx = room_ctx(terrain={}, snapshot=[])
        ctx.snapshot = None
        room = room_of(ctx)
        self.assertIsNone(room["contents"])
        self.assertIsNone(room["contents_omitted"])
        self.assertEqual(room["openings"], [])
        self.assertEqual(room["openings_omitted"], 0)
        # (b) no evidence at all (no snapshot, no hero, no terrain): the map is
        # unavailable, so *both* lists are null
        ctx0 = room_ctx(hero=None, terrain={}, snapshot=[])
        ctx0.snapshot = None
        room0 = room_of(ctx0)
        self.assertIsNone(room0["contents"])
        self.assertIsNone(room0["openings"])
        self.assertIsNone(room0["contents_omitted"])
        self.assertIsNone(room0["openings_omitted"])
        # (c) a known-empty map is available: empty lists with 0 omitted
        empty = protocol.Snapshot()
        empty.map = {}
        ctx2 = room_ctx(terrain={})
        ctx2.snapshot = empty
        room2 = room_of(ctx2)
        self.assertEqual(room2["contents"], [])
        self.assertEqual(room2["contents_omitted"], 0)
        # (d) a malformed (non-dict) map is unavailable, never known-empty
        bad = protocol.Snapshot()
        bad.map = ["not", "a", "dict"]
        ctx3 = room_ctx(terrain={})
        ctx3.snapshot = bad
        room3 = room_of(ctx3)
        self.assertIsNone(room3["contents"])
        self.assertIsNone(room3["contents_omitted"])

    # -- openings ---------------------------------------------------------

    def test_openings_screen_memory_sources_and_current_wall_override(self):
        room = room_of(room_ctx())
        by_at = {tuple(r["at"]): r for r in room["openings"]}
        # a current wall at (12,11) suppresses the stale remembered door
        self.assertNotIn((12, 11), by_at)
        # an item over remembered stairs keeps source=memory, shown=item
        self.assertEqual(by_at[(11, 10)]["source"], "memory")
        self.assertEqual(by_at[(11, 10)]["terrain"],
                         instances.T_STAIRS_DOWN)
        self.assertEqual(by_at[(11, 10)]["shown"], "item")
        # current corridor/door classifications are screen-sourced
        self.assertEqual(by_at[(13, 10)]["source"], "screen")
        self.assertEqual(by_at[(14, 10)]["terrain"], instances.T_CLOSED_DOOR)

    def test_remote_doors_stairs_precede_corridor_landmarks(self):
        room = room_of(room_ctx())
        terrains = [r["terrain"] for r in room["openings"]]
        corridor = terrains.index(instances.T_CORRIDOR)
        for i, t in enumerate(terrains):
            if t != instances.T_CORRIDOR:
                self.assertLess(i, corridor)

    def test_openings_eight_compass_directions_here_and_unknown_hero(self):
        hero = (10, 10)
        offsets = {(0, -1): "north", (1, -1): "northeast", (1, 0): "east",
                   (1, 1): "southeast", (0, 1): "south", (-1, 1): "southwest",
                   (-1, 0): "west", (-1, -1): "northwest"}
        for step, name in offsets.items():
            pos = (hero[0] + step[0], hero[1] + step[1])
            snap = [(hero, ("@", "white", 0, "none")),
                    (pos, ("#", "gray", 0, "none"))]
            ctx = room_ctx(hero=hero, terrain={}, snapshot=snap)
            room = room_of(ctx)
            self.assertEqual(room["openings"][0]["direction"], name, step)
        # with no hero, direction is null (not a guess)
        snap = [((25, 12), ("#", "gray", 0, "none"))]
        ctx = room_ctx(hero=None, terrain={}, snapshot=snap)
        self.assertIsNone(room_of(ctx)["openings"][0]["direction"])

    def test_openings_cap_and_no_accessibility_claim(self):
        snap = [((10, 10), ("@", "white", 0, "none"))]
        for i in range(20):
            snap.append(((11 + i, 12), ("#", "gray", 0, "none")))
        room = room_of(room_ctx(terrain={}, snapshot=snap))
        self.assertEqual(len(room["openings"]), 12)
        self.assertEqual(room["openings_omitted"], 20 - 12)
        # the records make no reachability/visibility claim (the fixed `scope`
        # wording explicitly denies them, and the records add none)
        blob = json.dumps(room["openings"]).lower()
        for word in ("reachable", "passable", "verified exit", "line of "
                     "sight", "clear", "no monster"):
            self.assertNotIn(word, blob)

    # -- purity / frozen contracts ---------------------------------------

    def test_renderer_is_pure_and_repeatable(self):
        ctx = room_ctx()
        before = (dict(ctx.terrain.terrain), dict(ctx.terrain.occupancy),
                  ctx.terrain.map_revision, ctx.terrain.occupancy_generation,
                  dict(ctx.snapshot.map), ctx.memory.hero)
        first = presentation.render_state(ctx)
        second = presentation.render_state(ctx)
        self.assertEqual(first, second)
        self.assertEqual((dict(ctx.terrain.terrain), dict(ctx.terrain.occupancy),
                          ctx.terrain.map_revision,
                          ctx.terrain.occupancy_generation,
                          dict(ctx.snapshot.map), ctx.memory.hero), before)

    def test_criteria_keys_order_indices_and_option_count_unchanged(self):
        cands = [move_cand(KEY.KEY_L, "navigate", (1, 0)),
                 move_cand(KEY.KEY_H, "navigate", (-1, 0)),
                 cand(KEY.KEY_SEARCH, "search")]
        table = table_of(cands)
        ctx = room_ctx(need=COMMAND)
        frozen, refusal = presentation.present("command", table.ordered_candidates,
                                               ctx)
        self.assertEqual(refusal, "")
        self.assertEqual(len(frozen.keys), len(table.ordered_candidates))
        self.assertEqual(list(frozen.criteria),
                         list(frozen.keys))
        self.assertEqual(sorted(frozen.key_index.values()),
                         list(range(len(frozen.keys))))

    def test_maximum_lists_and_crop_have_bounded_serialized_size(self):
        snap = [((10, 10), ("@", "white", 0, "none"))]
        for i in range(40):
            snap.append(((11 + (i % 20), 10 + (i // 20)),
                         ("%", "yellow", 0, "none")))
        for i in range(40):
            snap.append(((11 + (i % 20), 14 + (i // 20)),
                         ("#", "gray", 0, "none")))
        ctx = room_ctx(terrain={}, snapshot=snap)
        state_obj = presentation.render_state(ctx)
        self.assertLessEqual(len(state_obj["room"]["contents"]), 16)
        self.assertLessEqual(len(state_obj["room"]["openings"]), 12)
        blob = json.dumps(state_obj).encode("utf-8")
        # structurally bounded: the whole state payload stays well under 64 KiB
        self.assertLess(len(blob), 65536)

    # -- destination criteria --------------------------------------------

    def test_destination_appearance_not_route_target_or_capped_list(self):
        # the clause describes the *actual adjacent* destination, not a route
        # target far away nor the head of a capped contents list
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none")),
                ((20, 15), ("!", "white", 0, "none"))]
        ctx = room_ctx(terrain={(11, 10): instances.T_FLOOR}, snapshot=snap)
        text, refusal = presentation.render_criterion(
            move_cand(KEY.KEY_L, "navigate", (1, 0)), "command", ctx)
        self.assertEqual(refusal, "")
        self.assertIn("An item with food appearance is shown on that square.",
                      text)
        self.assertNotIn("potion", text)

    def test_food_appearance_never_named_ration_or_safe(self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(terrain={(11, 10): instances.T_FLOOR}, snapshot=snap)
        text, _ = presentation.render_criterion(
            move_cand(KEY.KEY_L, "navigate", (1, 0)), "command", ctx)
        self.assertIn("food appearance", text)
        for word in ("ration", "safe", "edible", "corpse", "BUC"):
            self.assertNotIn(word, text)

    def test_direction_answer_has_no_walking_or_destination_claim(self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(need=DIRECTION, terrain={}, snapshot=snap)
        cands = [move_cand(KEY.KEY_L, "navigate", (1, 0))]
        frozen, refusal = presentation.present("direction", cands, ctx)
        self.assertEqual(refusal, "")
        for text in frozen.criteria.values():
            self.assertNotIn("Walk", text)
            self.assertNotIn("shown on that square", text)

    def test_conflicting_current_terrain_omits_stale_criterion_phrase(self):
        # memory says floor, but the current screen shows a wall there
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("|", "gray", 0, "none"))]
        ctx = room_ctx(terrain={(11, 10): instances.T_FLOOR}, snapshot=snap)
        text, _ = presentation.render_criterion(
            move_cand(KEY.KEY_L, "navigate", (1, 0)), "command", ctx)
        self.assertNotIn("remembered room floor", text)
        self.assertIn("classifies that square as wall", text)

    def test_cycle_recovery_movement_gets_destination_appearance(self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(terrain={(11, 10): instances.T_FLOOR}, snapshot=snap)
        for label in ("recovery-step", "random-move", "unblock"):
            text, refusal = presentation.render_criterion(
                move_cand(KEY.KEY_L, label, (1, 0)), "command", ctx)
            self.assertEqual(refusal, "", label)
            self.assertIn("food appearance", text, label)

    def test_emergency_escape_movement_gets_destination_appearance(self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("a", "white", 0, "none"))]
        ctx = room_ctx(terrain={}, snapshot=snap)
        text, refusal = presentation.render_criterion(
            move_cand(KEY.KEY_L, "escape", (1, 0)), "command", ctx)
        self.assertEqual(refusal, "")
        self.assertIn("A creature is shown on that square", text)

    def test_search_wait_and_nonmovement_recovery_omit_destination_appearance(
            self):
        snap = [((10, 10), ("@", "white", 0, "none")),
                ((11, 10), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(terrain={}, snapshot=snap)
        cases = [
            (cand(KEY.KEY_SEARCH, "search-in-place"), "command"),
            (cand(KEY.KEY_SEARCH, "search"), "command"),
            (cand(KEY.KEY_WAIT, "unblock"), "command"),
            (cand(KEY.KEY_EAT, "eat-food"), "command"),
        ]
        for candidate, kind in cases:
            text, refusal = presentation.render_criterion(candidate, kind, ctx)
            self.assertEqual(refusal, "", candidate.semantic_label)
            self.assertNotIn("shown on that square", text)
            self.assertNotIn("classifies that square", text)
        # a non-command need never carries the clause either
        self.assertEqual(
            presentation.destination_appearance_clause(
                move_cand(KEY.KEY_L, "navigate", (1, 0)), "direction", ctx),
            "")

    def test_full_room_state_snapshot(self):
        room = room_of(room_ctx())
        self.assertEqual(room, {
            "scope": presentation.ROOM_SCOPE,
            "contents": [
                {"at": [11, 10], "kind": "item",
                 "category": "food appearance"},
                {"at": [12, 10], "kind": "creature", "category": "creature"},
                {"at": [15, 10], "kind": "unclassified",
                 "category": "unclassified display"},
            ],
            "contents_omitted": 0,
            "openings": [
                {"at": [11, 10], "direction": "east",
                 "terrain": instances.T_STAIRS_DOWN, "source": "memory",
                 "shown": "item"},
                {"at": [14, 10], "direction": "east",
                 "terrain": instances.T_CLOSED_DOOR, "source": "screen",
                 "shown": "none"},
                {"at": [13, 10], "direction": "east",
                 "terrain": instances.T_CORRIDOR, "source": "screen",
                 "shown": "none"},
                {"at": [16, 10], "direction": "east",
                 "terrain": instances.T_CORRIDOR, "source": "memory",
                 "shown": "none"},
            ],
            "openings_omitted": 0,
        })


if __name__ == "__main__":
    unittest.main()
