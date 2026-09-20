"""Prompt-declined edge learning (prompt-edge plan Rev 3, AC1-AC5).

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Covers the vapor-cloud freeze fix: matched-movement-prompt accounting, the one
bounded pending prompt context, the typed ``prompt-declined`` edge ledger, the
keyword-only Dijkstra-wide edge-admissibility predicate, and the classified
emergency-escape fallback order.
"""

import inspect
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

import stall_recovery_fixtures as fx  # noqa: E402
from tools.agent import (arbitration, candidates, controller, evaluate,  # noqa: E402
                         instances, navigation, policy, protocol, recording,
                         state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402
from test_auto import _line, _parse_actions, hello, obs  # noqa: E402
from test_auto_providers import paced  # noqa: E402

FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")
MONSTER = (":", "gray", 0, "none")
STAIR_UP = ("<", "gray", 0, "none")
STAIR_DOWN = (">", "gray", 0, "none")

KEY_EAST = protocol.DIR_KEYS[(1, 0)]
KEY_NORTH = protocol.DIR_KEYS[(0, -1)]
KEY_WEST = protocol.DIR_KEYS[(-1, 0)]

#: A representative validated ``yn`` response NeedKey (index, seq, id).
RESP_KEY = protocol.NeedKey(1, 2, 2)


def _terrain(cells):
    tm = instances.TerrainMemory()
    tm.merge(cells)
    return tm


def mem_with(cells, hero, messages=()):
    mem = state.EpisodeMemory()
    mem.grid.update(cells)
    mem.hero = hero
    mem.status.hp = 20
    mem.status.hp_max = 20
    mem.inventory.refresh([], 0, 0)
    mem.messages.extend(messages)
    return mem


def ctx(mem, tick=0, need=None):
    need = need or {"kind": "command", "id": 1}
    return ReflexContext(
        episode=1, tick=tick, need=need,
        need_key=protocol.NeedKey(1, tick, need.get("id")),
        snapshot=protocol.Snapshot(), pages=[], memory=mem,
        directives=[], deadline=0.0)


def _walk_first(hero, first):
    """Follow ``first`` steps from *hero* until a step repeats/leaves."""
    hops = 0
    pos = tuple(hero)
    seen = {pos}
    while pos in first:
        step = first[pos]
        pos = (pos[0] + step[0], pos[1] + step[1])
        hops += 1
        if pos in seen:
            break
        seen.add(pos)
    return pos, hops


def _nav_candidate(hop=1, label="navigate", family="frontier",
                   effect="navigate", key=KEY_EAST, payload=None):
    return candidates.make_candidate(
        {"key": key}, label, family=family, direction=(1, 0),
        direction_rank=navigation.DIR_RANK.get((1, 0), 0),
        reason="test", proposed_effect=effect,
        effect_payload=payload if payload is not None else ())


# ------------------------------------------------------------------ AC5: nav

class EdgeAdmissiblePredicate(unittest.TestCase):
    """AC5: the keyword-only, Dijkstra-wide edge-admissibility predicate."""

    def _corridor(self, xs=(1, 2, 3, 4, 5)):
        return {(x, 10): FLOOR for x in xs}

    def test_forbidden_edge_default_preserves_dist_first_steps(self):
        t = _terrain(self._corridor())
        hero = (1, 10)
        d0, f0, s0 = navigation.one_dijkstra_steps(t, hero)
        d1, f1, s1 = navigation.one_dijkstra_steps(
            t, hero, edge_admissible=lambda s, d, c: True)
        self.assertEqual(d0, d1)
        self.assertEqual(f0, f1)
        self.assertEqual(s0, s1)
        # the parameter is keyword-only: a positional predicate is a TypeError
        with self.assertRaises(TypeError):
            navigation.one_dijkstra_steps(t, hero, None, None, False,
                                          lambda s, d, c: True)

    def test_forbidden_edge_filters_seed_and_interior(self):
        t = _terrain(self._corridor())
        hero = (1, 10)
        seed = navigation.one_dijkstra_steps(
            t, hero,
            edge_admissible=lambda s, d, c: not (s == hero and d == (2, 10)))
        self.assertNotIn((2, 10), seed[0])
        interior = navigation.one_dijkstra_steps(
            t, hero,
            edge_admissible=lambda s, d, c: not (s == (3, 10)
                                                 and d == (4, 10)))
        self.assertIn((3, 10), interior[0])
        self.assertNotIn((4, 10), interior[0])
        self.assertNotIn((5, 10), interior[0])

    def test_forbidden_edge_alternate_route_keeps_true_hops(self):
        # a corridor with a bypass: forbidding the interior direct edge keeps a
        # longer alternate route whose hop count is the TRUE edge count
        cells = {(1, 10): FLOOR, (2, 10): FLOOR, (3, 10): FLOOR,
                 (4, 10): FLOOR, (5, 10): FLOOR,
                 (2, 9): FLOOR, (3, 9): FLOOR, (4, 9): FLOOR}
        t = _terrain(cells)
        hero = (1, 10)
        pred = lambda s, d, c: not (s == (2, 10) and d == (3, 10))  # noqa: E731
        dist, first, steps = navigation.one_dijkstra_steps(
            t, hero, edge_admissible=pred)
        self.assertIn((5, 10), dist)
        end, hops = _walk_first(hero, first)
        # the first-step walk reaches some cell; its hop count equals steps[]
        self.assertEqual(steps[end], hops)
        # the forbidden edge is never traversed on any chosen path
        pos = hero
        while pos in first:
            step = first[pos]
            nb = (pos[0] + step[0], pos[1] + step[1])
            self.assertNotEqual((pos, nb), ((2, 10), (3, 10)))
            pos = nb

    def test_forbidden_edge_preserves_equal_cost_tie_order(self):
        cells = {(3, 10): FLOOR, (4, 10): FLOOR, (3, 9): FLOOR,
                 (3, 11): FLOOR, (2, 10): FLOOR}
        t = _terrain(cells)
        hero = (3, 10)
        d0, f0, _ = navigation.one_dijkstra_steps(t, hero)
        d1, f1, _ = navigation.one_dijkstra_steps(
            t, hero, edge_admissible=lambda s, d, c: True)
        self.assertEqual(d0, d1)
        self.assertEqual(f0, f1)

    def test_forbidden_edge_still_checks_deadline(self):
        t = _terrain(self._corridor())

        def boom():
            raise RuntimeError("deadline")

        with self.assertRaises(RuntimeError):
            navigation.one_dijkstra_steps(
                t, (1, 10), None, None, boom,
                edge_admissible=lambda s, d, c: True)

    def test_edge_admissible_is_keyword_only(self):
        t = _terrain(self._corridor())
        hero = (1, 10)
        pred = lambda s, d, c: True  # noqa: E731
        # keyword form succeeds for all three entry points
        navigation.one_dijkstra_steps(t, hero, edge_admissible=pred)
        navigation.one_dijkstra(t, hero, edge_admissible=pred)
        navigation.plan(t, hero, edge_admissible=pred)
        # a positional predicate (6th arg, after deadline_check) is rejected
        with self.assertRaises(TypeError):
            navigation.one_dijkstra_steps(t, hero, None, None, False, pred)
        with self.assertRaises(TypeError):
            navigation.one_dijkstra(t, hero, None, None, False, pred)
        with self.assertRaises(TypeError):
            navigation.plan(t, hero, None, None, False, pred)
        # the predicate sits after the bare '*' in every signature
        for fn in (navigation.one_dijkstra, navigation.one_dijkstra_steps,
                   navigation.plan):
            params = inspect.signature(fn).parameters
            self.assertEqual(params["edge_admissible"].kind,
                             inspect.Parameter.KEYWORD_ONLY)


# ------------------------------------------------------ AC2: operation class

class MovementOriginTaxonomy(unittest.TestCase):
    """AC2: the closed accepted/rejected operation-class taxonomy matrix."""

    SRC = (10, 10)
    KEY = ((1, 1, 1), "t", "c", 1)

    def _origin(self, cand, need_kind="command"):
        return arbitration.movement_origin_from_selected(
            cand, need_kind, 7, self.SRC, self.KEY)

    def test_movement_origin_operation_taxonomy_matrix(self):
        acquire = _nav_candidate(
            payload=("dest", "acquire", 7, "explore-frontier", 11, 10,
                     "frontier", "default", 0, -1, ""))
        cont = _nav_candidate(
            payload=("dest", "continue", 7, "explore-frontier", 12, 12,
                     "frontier", "default", 0, 3, ""))
        recovery = candidates.make_candidate(
            {"key": KEY_EAST}, "recovery-step", family="recovery",
            proposed_effect="recovery", effect_payload=("recovery", (1, 0)))
        escape = candidates.make_candidate(
            {"key": KEY_EAST}, "escape", family="emergency",
            proposed_effect="emergency")
        door = _nav_candidate(
            payload=("dest", "continue", 7, "open-door", 11, 10, "door",
                     "default", 0, 3, ""))
        prefix = candidates.make_candidate(
            {"key": ord("m")}, "navigate", family="frontier",
            proposed_effect="navigate",
            effect_payload=("dest", "acquire", 7, "x", 11, 10, "frontier",
                            "default", 0, -1, ""))
        stair = candidates.make_candidate(
            {"key": ord(">")}, "descend", family="descend",
            proposed_effect="descend")
        wait = candidates.make_candidate(
            {"key": ord("s")}, "search", family="recovery",
            proposed_effect="site-search")
        classless = candidates.make_candidate(
            {"key": KEY_EAST}, "prompt", family="prompt")
        synth = _nav_candidate(payload=())
        # review F7: an unknown destination operation must fail closed, and a
        # non-gameplay need kind never establishes an edge.
        unknown_op = _nav_candidate(
            payload=("dest", "teleport", 7, "x", 11, 10, "frontier",
                     "default", 0, -1, ""))
        arrive_op = _nav_candidate(
            payload=("dest", "arrive", 7, "x", 11, 10, "frontier",
                     "default", 0, -1, ""))

        accepted = {"destination": (acquire, cont), "recovery": (recovery,),
                    "emergency": (escape,)}
        for op, cands in accepted.items():
            for cand in cands:
                origin = self._origin(cand)
                self.assertIsNotNone(origin, op)
                self.assertEqual(origin.operation, op)
                self.assertEqual(origin.src, self.SRC)
                self.assertEqual(origin.dst, (11, 10))
        for cand, name in ((door, "door"), (prefix, "prefix"),
                           (stair, "stair"), (wait, "wait/search"),
                           (classless, "classless"), (synth, "synthesized"),
                           (unknown_op, "unknown-op"),
                           (arrive_op, "arrive-op")):
            self.assertIsNone(self._origin(cand), name)
        # a non-gameplay need kind is rejected outright, even for an otherwise
        # accepted movement candidate
        for kind in ("yn", "menu", "ack", "line", "extcmd", "position", ""):
            self.assertIsNone(self._origin(acquire, kind), kind)

    def test_movement_origin_dst_is_origin_edge_not_destination(self):
        # a continuation whose semantic destination is several steps away must
        # still bind dst to the frozen src + selected delta
        cont = _nav_candidate(
            payload=("dest", "continue", 7, "explore-frontier", 12, 12,
                     "frontier", "default", 0, 3, ""))
        origin = self._origin(cont)
        self.assertEqual(origin.src, (10, 10))
        self.assertEqual(origin.dst, (11, 10))
        self.assertNotEqual(origin.dst, (12, 12))

    def test_movement_origin_fails_closed_without_pre_hero(self):
        acquire = _nav_candidate(
            payload=("dest", "acquire", 7, "x", 11, 10, "frontier",
                     "default", 0, -1, ""))
        self.assertIsNone(arbitration.movement_origin_from_selected(
            acquire, "command", 7, None, self.KEY))


class PromptContinuity(unittest.TestCase):
    """Round-2 F1: continuity uses the stable request identity, not full seq."""

    def _pending(self):
        origin = arbitration.movement_origin_from_selected(
            _nav_candidate(payload=("dest", "acquire", 7, "x", 11, 10,
                                    "frontier", "default", 0, -1, "")),
            "command", 7, (10, 10), ((1, 1, 1), "t", "c", 1))
        return arbitration.matched_movement_prompt(
            origin, {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT},
            7, (10, 10), protocol.NeedKey(1, 2, 2))

    def test_same_id_newer_seq_is_the_same_request(self):
        pending = self._pending()
        self.assertEqual(arbitration.prompt_request_identity(
            protocol.NeedKey(1, 2, 2)), (1, 2))
        self.assertEqual(arbitration.prompt_request_identity(
            protocol.NeedKey(1, 9, 2)), (1, 2))
        # same episode + id + prompt text, newer seq: still a re-presentation
        self.assertTrue(arbitration.is_representation_of(
            pending, protocol.NeedKey(1, 9, 2), fx.VAPOR_PROMPT))
        # a different prompt id is a replacement
        self.assertFalse(arbitration.is_representation_of(
            pending, protocol.NeedKey(1, 3, 3), fx.VAPOR_PROMPT))
        # a different episode is not the same request
        self.assertFalse(arbitration.is_representation_of(
            pending, protocol.NeedKey(2, 3, 2), fx.VAPOR_PROMPT))
        # whitespace/case normalization only; a materially different prompt is
        # not the same confirmation
        self.assertTrue(arbitration.is_representation_of(
            pending, protocol.NeedKey(1, 4, 2),
            "  STEP INTO   THAT VAPOR CLOUD? "))
        self.assertFalse(arbitration.is_representation_of(
            pending, protocol.NeedKey(1, 4, 2),
            "Step into that poison gas cloud?"))
        # an incomplete key is never a representation
        self.assertFalse(arbitration.is_representation_of(pending, (), ""))
        self.assertIsNone(arbitration.prompt_request_identity(()))


class MovementEntryRecognition(unittest.TestCase):
    def test_only_cloud_confirmations_are_recognized(self):
        self.assertTrue(arbitration.is_movement_entry_confirmation(
            "Step into that vapor cloud?"))
        self.assertTrue(arbitration.is_movement_entry_confirmation(
            "  step INTO that   poison gas cloud? "))
        for other in ("Really quit?", "Do you want to eat it?",
                      "Shall I pick up the object?", "Step into that water?",
                      "", None):
            self.assertFalse(arbitration.is_movement_entry_confirmation(other))


# --------------------------------------------------------- AC1: accounting

class MatchedMovementAccounting(unittest.TestCase):
    """AC1: once-only, identity-bound stationary counting at prompt arrival."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _origin(self, src=(10, 10)):
        return arbitration.movement_origin_from_selected(
            _nav_candidate(
                payload=("dest", "acquire", 7, "x", 11, 10, "frontier",
                         "default", 0, -1, "")),
            "command", 7, src, ((1, 1, 1), "t", "c", 1))

    def test_matched_movement_vapor_prompt_advances_stationary_once(self):
        origin = self._origin()
        yn = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}
        self.assertTrue(self.ref.arm_movement_prompt(
            origin, yn, 7, (10, 10), response_need_key=RESP_KEY))
        pending = self.ref.pending_prompt
        self.assertIsNotNone(pending)
        self.assertEqual((pending.src, pending.dst), ((10, 10), (11, 10)))
        # the context is bound to the *validated response* NeedKey, not the
        # originating command need key (review F1)
        self.assertEqual(candidates.normalize_need_key(pending.response_need_key),
                         candidates.normalize_need_key(RESP_KEY))
        # a re-presentation creates no second context and earns no second count
        self.assertFalse(self.ref.arm_movement_prompt(
            origin, yn, 7, (10, 10), response_need_key=RESP_KEY))
        # the answer result must not count again: no second arm is possible
        self.ref.note_prompt_answer_sent(3, RESP_KEY, protocol.KEY_N)
        self.assertFalse(self.ref.arm_movement_prompt(
            origin, yn, 7, (10, 10), response_need_key=RESP_KEY))

    def test_answer_binding_requires_exact_response_key_and_n_byte(self):
        # review F1: only the exact response NeedKey AND the KEY_N byte bind;
        # a `y`, an ESC, a different NeedKey or a failed write clears the
        # context and writes nothing
        for need_key, byte, binds in (
                (RESP_KEY, protocol.KEY_N, True),
                (RESP_KEY, ord("y"), False),
                (RESP_KEY, protocol.KEY_ESC, False),
                (protocol.NeedKey(1, 2, 3), protocol.KEY_N, False),
                (RESP_KEY, None, False),
                (None, protocol.KEY_N, False)):
            ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
            ref.arm_movement_prompt(
                self._origin(), {"kind": "yn", "id": 2,
                                 "prompt": fx.VAPOR_PROMPT}, 7, (10, 10),
                response_need_key=RESP_KEY)
            ref.note_prompt_answer_sent(3, need_key, byte)
            pending = ref.pending_prompt
            if binds:
                self.assertIsNotNone(pending)
                self.assertTrue(pending.answer_sent)
                self.assertEqual(pending.answer_byte, protocol.KEY_N)
            else:
                self.assertIsNone(pending, (need_key, byte))

    def test_unrelated_yn_and_interaction_direction_do_not_advance_stationary(
            self):
        origin = self._origin()
        for need in ({"kind": "yn", "id": 2, "prompt": "Really quit?"},
                     {"kind": "yn", "id": 2, "prompt": "Do you want to eat?"},
                     {"kind": "yn", "id": 2, "prompt": "Shall I pick up?"},
                     {"kind": "menu", "id": 2, "prompt": "inventory"},
                     {"kind": "yn", "id": 2},
                     {"kind": "yn", "id": 2, "prompt": "Step into water?"}):
            self.assertFalse(self.ref.arm_movement_prompt(
                origin, need, 7, (10, 10), response_need_key=RESP_KEY), need)
        # no frozen origin (an unmatched or non-movement frame) counts nothing
        self.assertFalse(self.ref.arm_movement_prompt(
            None, {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}, 7,
            (10, 10), response_need_key=RESP_KEY))

    def test_prompt_replay_unknown_hero_and_instance_change_do_not_double_count(
            self):
        origin = self._origin()
        yn = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}
        # unknown hero
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 7, None, RESP_KEY))
        # instance change
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 8, (10, 10), RESP_KEY))
        # a moved hero is not a stationary confirmation
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 7, (11, 10), RESP_KEY))
        self.assertTrue(self.ref.arm_movement_prompt(
            origin, yn, 7, (10, 10), response_need_key=RESP_KEY))
        self.assertFalse(self.ref.arm_movement_prompt(
            origin, yn, 7, (10, 10), response_need_key=RESP_KEY))
        # an ambiguous frame (two displayed `@`) reconciles to no confirmed
        # hero, so it never arms a context (review F2)
        self.ref.clear_pending_prompt()
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 7, None, RESP_KEY))
        self.assertFalse(self.ref.arm_movement_prompt(
            origin, yn, 7, None, response_need_key=RESP_KEY))

    def test_prompt_bound_attempts_compose_with_stationary_thresholds_3_6_10(
            self):
        cells = stall_fx_ep4_corridor()
        for start in (2, 5, 9):
            ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
            mem = mem_with(cells, (10, 10),
                           messages=["You already found a monster."])
            mem.no_progress = start
            mem.last_hero = (10, 10)
            # the bound prompt arrival advances the counter exactly once
            mem.commit(_staged(mem), hero=(10, 10),
                       advance_stationary=True)
            self.assertEqual(mem.no_progress, start + 1)
            # the answer result does not advance it again
            mem.commit(_staged(mem), hero=(10, 10),
                       advance_stationary=False)
            self.assertEqual(mem.no_progress, start + 1)
            # the composed count reaches the existing 3/6/10 threshold
            self.assertEqual(mem.no_progress, start + 1)
            cand = ref.prepare(ctx(mem)).table.scripted()
            self.assertEqual(cand.family, "recovery", start)

    def test_malformed_cloud_prompt_creates_no_accounting_or_context(self):
        good = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT,
                "choices": "yn", "default": None, "numeric": False}
        self.assertEqual(protocol.validate_need(good), None)
        # a malformed cloud-shaped yn must fail complete protocol validation, so
        # the ordered live/evaluator paths reach no accounting or context
        for bad in ({"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT,
                     "choices": "nope"},
                    {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT,
                     "default": "yes"}):
            self.assertNotEqual(protocol.validate_need(bad), "", bad)
        # the validation precedes movement-prompt recognition in BOTH paths
        for path in ("tools/agent/controller.py", "tools/agent/evaluate.py"):
            src = _read(path)
            self.assertLess(src.index("validate_need(frame_need)"),
                            src.index("_capture_movement_prompt("), path)
        # an unvalidated (malformed) need is never armed
        origin = self._origin()
        self.assertFalse(self.ref.arm_movement_prompt(
            origin, {"kind": "menu", "id": 2}, 7, (10, 10),
            response_need_key=RESP_KEY))


# ---------------------------------------------- AC2: decline evidence rules

class DeclineEvidence(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _arm(self, src=(10, 10), dst=(11, 10), response_need_key=RESP_KEY):
        cand = _nav_candidate(
            payload=("dest", "acquire", 7, "x", dst[0], dst[1], "frontier",
                     "default", 0, -1, ""))
        origin = arbitration.movement_origin_from_selected(
            cand, "command", self.ref.instance_id, src, ((1, 1, 1), "t", "c", 1))
        self.ref.arm_movement_prompt(
            origin, {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT},
            self.ref.instance_id, src, response_need_key=response_need_key)
        return origin

    def test_declined_movement_records_origin_edge_not_destination_or_answer(
            self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self._arm()
        self.ref.note_prompt_answer_sent(3, RESP_KEY, protocol.KEY_N)
        self.assertTrue(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        key = self.ref.prompt_decline_record_key(
            self.ref.instance_id, (10, 10), (11, 10), "normal")
        self.assertIn(key, self.ref.prompt_declined_edges)
        # the edge is the *origin* edge, never the semantic destination or the
        # answer action's edge
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))
        self.assertFalse(self.ref.edge_suppressed(
            self.ref._terrain(mem), (11, 10), (12, 10)))

    def test_prompt_decline_requires_matching_answer_send_and_resolution(self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        # prepare-only (no answer sent): nothing is written
        self._arm()
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertFalse(self.ref.prompt_declined_edges)
        # a failed write leaves no answer binding: re-arm and resolve with no
        # answer sent -- still nothing written
        self.ref.clear_pending_prompt()
        self._arm()
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertFalse(self.ref.prompt_declined_edges)
        # a genuine decline send then resolves exactly once
        self.ref.note_prompt_answer_sent(3, RESP_KEY, protocol.KEY_N)
        self.assertTrue(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertIsNone(self.ref.pending_prompt)
        before = dict(self.ref.prompt_declined_edges)
        # a stale `n` (the confirmation is still up) writes nothing
        self._arm()
        self.ref.note_prompt_answer_sent(4, RESP_KEY, protocol.KEY_N)
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), False))
        self.assertEqual(self.ref.prompt_declined_edges, before)
        # an ESC fallback answer writes nothing: it never binds
        self.ref.clear_pending_prompt()
        self._arm()
        self.ref.note_prompt_answer_sent(5, RESP_KEY, protocol.KEY_ESC)
        self.assertIsNone(self.ref.pending_prompt)
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertEqual(self.ref.prompt_declined_edges, before)
        # a `y` answer writes nothing either
        self.ref.clear_pending_prompt()
        self._arm()
        self.ref.note_prompt_answer_sent(6, RESP_KEY, ord("y"))
        self.assertIsNone(self.ref.pending_prompt)
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertEqual(self.ref.prompt_declined_edges, before)
        # a same-text prompt with a *different* id writes nothing: the answer
        # is bound to RESP_KEY, not to the replacement confirmation's key
        self.ref.clear_pending_prompt()
        self._arm()
        self.ref.note_prompt_answer_sent(
            7, protocol.NeedKey(1, 2, 9), protocol.KEY_N)
        self.assertIsNone(self.ref.pending_prompt)
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertEqual(self.ref.prompt_declined_edges, before)

    def test_prompt_answer_does_not_reapply_destination_or_applied_token(self):
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        need = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}
        action, reason, effect, payload = ref._yn(ctx(mem, need=need))
        self.assertEqual(action, {"yn": protocol.KEY_N})
        # a prompt continuation: no destination effect and no frozen payload,
        # so no destination attempt or applied token is spent by the answer
        self.assertEqual(effect, "prompt")
        self.assertEqual(payload, ())

    def test_cloud_confirmation_declines_even_with_yes_native_default(self):
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        need = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT,
                "default": 1, "choices": ["y"]}
        action, _reason, effect, _payload = ref._yn(ctx(mem, need=need))
        self.assertEqual(action, {"yn": protocol.KEY_N})
        self.assertEqual(effect, "prompt")

    def test_prompt_decline_resolution_with_hero_progress_records_no_edge(
            self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self._arm()
        self.ref.note_prompt_answer_sent(3, RESP_KEY, protocol.KEY_N)
        # a hero-progress variant (moved), an unknown hero and a nonadjacent
        # relocation all fail the unchanged-confirmed-hero condition
        for hero in ((11, 10), None, (12, 10)):
            self.assertFalse(self.ref.resolve_prompt_decline(
                mem, self.ref.instance_id, hero, True), hero)
            self.assertEqual(self.ref.prompt_declined_edges, {})
        # the same instance/hero still resolves once the hero is unchanged
        self.assertTrue(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))


# ---------------------------------------------- AC3: suppression & reopening

class PromptEdgeSuppression(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def test_prompt_declined_edge_survives_prompt_absence_and_destination_reacquisition(
            self):
        mem = mem_with(fx.vapor_corridor_route_around_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        # the suppression holds with no prompt text present at all
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))
        # a default acquisition routes around instead of re-crossing the edge
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertNotEqual(cand.direction, (1, 0))

    def test_exit_b_suppression_survives_local_signature_changes(self):
        # review F3: under exit (b) the disposition records
        # ``positive_reopening_enabled: false``, so a prompt-declined record
        # suppresses regardless of *any* local signature change
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.assertFalse(self.ref.positive_reopening_enabled)
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        terrain = self.ref._terrain(mem)
        self.assertTrue(self.ref.edge_suppressed(terrain, (10, 10), (11, 10)))
        for change in ("occupant", "corridor-floor", "side-cell"):
            if change == "occupant":
                mem.grid[(11, 10)] = MONSTER
            elif change == "corridor-floor":
                mem.grid[(11, 10)] = FLOOR
            else:
                mem.grid[(11, 10)] = ("#", "brown", 0, "none")
            self.assertTrue(
                self.ref.edge_suppressed(self.ref._terrain(mem), (10, 10),
                                         (11, 10)),
                change)
        # the stored signature/prompt are retained for diagnostics only
        rec = self.ref.prompt_declined_edges[
            self.ref.prompt_decline_record_key(self.ref.instance_id,
                                               (10, 10), (11, 10), "normal")]
        self.assertEqual(rec[1], " ".join(fx.VAPOR_PROMPT.split()))
        # an instance/scope reset is the only reopening
        self.ref.begin_instance(2)
        self.assertFalse(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))

    def test_positive_reopening_capability_gates_signature_reopening(self):
        # the signature-based reopening path exists but is *disabled* until the
        # mandatory operator-gated trace explicitly enables it
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        mem.grid[(11, 10)] = MONSTER
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))
        self.ref.positive_reopening_enabled = True
        self.assertFalse(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))

    def test_prompt_edge_ignores_time_visits_remote_occupancy_and_occlusion(
            self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        # visit counts and a remote occupancy change are not edge evidence
        mem.visits[(11, 10)] = 9
        mem.grid[(12, 10)] = ("d", "white", 0, "none")
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))

    def test_prompt_ledger_overwrites_per_edge_and_clears_on_instance_reset(
            self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        for _ in range(3):
            self.ref.record_prompt_decline(
                self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
                fx.VAPOR_PROMPT)
        self.assertEqual(len(self.ref.prompt_declined_edges), 1)
        self.ref.begin_instance(99)
        self.assertEqual(self.ref.prompt_declined_edges, {})
        self.assertIsNone(self.ref.pending_prompt)

    def test_route_only_failure_does_not_permanently_suppress_target_after_reopen(
            self):
        # a route-only failure keeps the *edge* as the suppression owner, so a
        # scope change (the only reopening under exit (b)) restores the target
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        terrain = self.ref._terrain(mem)
        plan = navigation.plan(
            terrain, (10, 10), mem.visits, None,
            edge_admissible=self.ref._edge_admissible(terrain,
                                                      navigation.ACTION_NORMAL))
        # while suppressed the frontier beyond is unreachable (route-only)
        self.assertNotIn((12, 10), plan.dist)
        # no permanent *target* suppression was written for the failed route
        self.assertIsNone(
            self.ref.targets.serviced_signature((12, 10)))
        # a scope reset reopens the edge and the target is electable again
        self.ref.begin_instance(2)
        terrain2 = self.ref._terrain(mem)
        self.assertFalse(self.ref.edge_suppressed(terrain2, (10, 10),
                                                  (11, 10)))
        plan2 = navigation.plan(
            terrain2, (10, 10), mem.visits, None,
            edge_admissible=self.ref._edge_admissible(terrain2,
                                                      navigation.ACTION_NORMAL))
        self.assertIn((12, 10), plan2.dist)


# ------------------------------------------------ AC4: full pipeline loop

class VaporCloudLoop(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _decline(self, mem):
        self.ref.record_prompt_decline(
            self.ref.instance_id, fx.VAPOR_SRC, fx.VAPOR_DST, "normal", mem,
            fx.VAPOR_PROMPT)

    def test_vapor_cloud_loop_routes_around_with_held_destination(self):
        mem = mem_with(fx.vapor_corridor_route_around_cells(), fx.VAPOR_HERO)
        # the north detour is a longer route to the same frontier, so the
        # cheaper east edge into the cloud is elected first
        mem.visits.update({(10, 9): 1, (11, 9): 1, (12, 9): 1})
        first = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertEqual(first.direction, (1, 0))     # east into the cloud
        self._decline(mem)
        again = self.ref.prepare(ctx(mem)).table.scripted()
        # the same destination is reachable along the alternate (north) route
        self.assertNotEqual(again.direction, (1, 0))
        self.assertEqual(again.direction, (0, -1))

    def test_vapor_cloud_loop_filters_interior_dijkstra_edges(self):
        cells = {(x, 10): FLOOR for x in (1, 2, 3, 4, 5)}
        t = _terrain(cells)
        pred = lambda s, d, c: not (s == (3, 10) and d == (4, 10))  # noqa: E731
        dist, _f, _s = navigation.one_dijkstra_steps(t, (1, 10),
                                                     edge_admissible=pred)
        # an interior forbidden edge (not merely the first hop) is filtered
        self.assertIn((3, 10), dist)
        self.assertNotIn((4, 10), dist)
        self.assertNotIn((5, 10), dist)

    def test_blocked_interior_edge_on_route_to_upstairs_is_filtered(self):
        cells = {(x, 10): FLOOR for x in (1, 2, 3, 4, 5, 6)}
        cells[(6, 10)] = STAIR_UP
        t = _terrain(cells)
        mem = mem_with(cells, (1, 10))
        pred = lambda s, d, c: not (s == (3, 10) and d == (4, 10))  # noqa: E731
        dist, _f, steps = navigation.one_dijkstra_steps(
            t, (1, 10), edge_admissible=pred)
        self.assertNotIn((6, 10), dist)

    def test_vapor_cloud_loop_all_routes_blocked_enters_bounded_recovery(self):
        mem = mem_with(fx.vapor_corridor_cells(), fx.VAPOR_HERO)
        self._decline(mem)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        self.assertNotEqual(cand.direction, (1, 0))
        # bounded recovery/search: never an unbounded wait or raw-grid step
        self.assertIn(cand.semantic_label,
                      ("search", "search-secret", "recovery-step",
                       "forced-search", "trapped"))

    def test_vapor_prompt_destination_attempts_respect_three_attempt_bound(
            self):
        # a destination attempt is charged exactly once per acquisition, so a
        # bound prompt arrival can never double-charge the 3-attempt bound
        self.assertEqual(navigation.STALL_MAX, 3)
        cand = self.ref.prepare(
            ctx(mem_with(fx.vapor_corridor_cells(), fx.VAPOR_HERO)))
        self.assertIsNotNone(cand.table.scripted())
        # recording the decline is not a destination charge
        self.assertIsNone(self.ref.targets.held())
        self.ref.record_prompt_decline(
            self.ref.instance_id, fx.VAPOR_SRC, fx.VAPOR_DST, "normal",
            mem_with(fx.vapor_corridor_cells(), fx.VAPOR_HERO),
            fx.VAPOR_PROMPT)
        self.assertIsNone(self.ref.targets.held())


# ------------------------------- AC5: emergency / pickup / parity / cloud gate

class EmergencyFallback(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _low_hp_mem(self, cells, hero, messages=()):
        mem = mem_with(cells, hero, messages)
        mem.status.hp = 1
        mem.status.hp_max = 20
        return mem

    def test_declined_emergency_edge_uses_alternate_legal_escape(self):
        cells = {(x, y): FLOOR for x in range(4, 8) for y in range(4, 8)}
        cells[(4, 5)] = MONSTER
        mem = self._low_hp_mem(cells, (5, 5))
        # the preferred retreat (east, away from the monster) is declined for
        # the emergency class; another legal emergency edge is used instead
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 5), (6, 5), "emergency", mem,
            fx.VAPOR_PROMPT)
        action, _why = self.ref._escape(mem, (5, 5))
        self.assertNotEqual(action, {"key": KEY_EAST})

    def test_sole_legal_reverse_remains_available_to_emergency(self):
        cells = {(5, 5): FLOOR, (4, 5): FLOOR}
        mem = self._low_hp_mem(cells, (5, 5))
        # a normal-navigation prompt decline must never suppress the sole legal
        # reverse for the emergency class
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 5), (4, 5), "normal", mem,
            fx.VAPOR_PROMPT)
        action, _why = self.ref._escape(mem, (5, 5))
        self.assertEqual(action, {"key": KEY_WEST})

    def test_emergency_singleton_precedence_unchanged_by_edge_filter(self):
        cells = {(x, 10): FLOOR for x in (3, 4, 5, 6)}
        cells[(6, 10)] = VAPOR_CELL
        mem = self._low_hp_mem(cells, (5, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 10), (5, 9), "normal", mem,
            fx.VAPOR_PROMPT)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # the emergency branch still preempts ordinary navigation
        self.assertEqual(cand.family, "emergency")

    def test_prompt_edge_route_filter_preserves_emergency_and_pickup_precedence(
            self):
        cells = {(x, 10): FLOOR for x in (3, 4, 5, 6)}
        cells[(6, 10)] = VAPOR_CELL
        mem = self._low_hp_mem(cells, (5, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 10), (6, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        cand = self.ref.prepare(ctx(mem)).table.scripted()
        # low-HP escape is still the sole candidate (precedence preserved)
        self.assertEqual(cand.family, "emergency")

    def test_emergency_step_three_normal_only_record_keeps_sole_edge(self):
        # review F4 case 1: a *normal-only* record never suppresses the sole
        # legal edge for the emergency class, so the escape keeps it
        cells = {(5, 5): FLOOR, (4, 5): FLOOR}
        mem = self._low_hp_mem(cells, (5, 5))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 5), (4, 5), "normal", mem,
            fx.VAPOR_PROMPT)
        action, _why = self.ref._escape(mem, (5, 5))
        self.assertEqual(action, {"key": KEY_WEST})

    def test_emergency_step_three_emergency_record_is_not_retried(self):
        # review F4 case 2: step 3 must never override an *emergency-class*
        # record, so a previously emergency-declined sole edge is not retried
        # and the policy proceeds to rest/search instead
        cells = {(5, 5): FLOOR, (4, 5): FLOOR}
        mem = self._low_hp_mem(cells, (5, 5))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 5), (4, 5), "emergency", mem,
            fx.VAPOR_PROMPT)
        action, _why = self.ref._escape(mem, (5, 5))
        self.assertNotEqual(action, {"key": KEY_WEST})
        self.assertEqual(action, {"key": protocol.KEY_SEARCH})

    def test_emergency_step_three_both_records_are_not_retried(self):
        # review F4 case 3: with both a normal and an emergency record the sole
        # edge stays unavailable (step 3 requires normal-only suppression)
        cells = {(5, 5): FLOOR, (4, 5): FLOOR}
        mem = self._low_hp_mem(cells, (5, 5))
        for cls in ("normal", "emergency"):
            self.ref.record_prompt_decline(
                self.ref.instance_id, (5, 5), (4, 5), cls, mem,
                fx.VAPOR_PROMPT)
        action, _why = self.ref._escape(mem, (5, 5))
        self.assertNotEqual(action, {"key": KEY_WEST})
        self.assertEqual(action, {"key": protocol.KEY_SEARCH})

    def test_emergency_step_three_bounded_across_low_hp_frames(self):
        # review F4 case 4: repeated low-HP frames on an emergency-declined sole
        # edge are bounded -- a stable non-movement action, never a retry loop
        cells = {(5, 5): FLOOR, (4, 5): FLOOR}
        mem = self._low_hp_mem(cells, (5, 5))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (5, 5), (4, 5), "emergency", mem,
            fx.VAPOR_PROMPT)
        seen = set()
        for _ in range(6):
            action, _why = self.ref._escape(mem, (5, 5))
            seen.add(tuple(sorted(action.items())))
            self.assertNotEqual(action, {"key": KEY_WEST})
        self.assertEqual(len(seen), 1)

    def test_capped_scripted_vapor_decline_retains_effect_ownership(self):
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        need = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}
        action, _r, effect, payload = ref._yn(ctx(mem_with({}, (1, 1)),
                                                  need=need))
        # the decline carries no frozen destination/applied-token payload, so
        # the cap/ownership contracts are untouched
        self.assertEqual(action, {"yn": protocol.KEY_N})
        self.assertEqual(payload, ())
        self.assertEqual(effect, "prompt")


class ReflexContextSeam(unittest.TestCase):
    """AC5/F6: the internal-only context seam is populated, not always-None.

    The seam carries immutable values only (a frozen ``MovementOrigin`` and the
    frozen pending context) in *both* paths; it is never rendered or consumed by
    providers/presentation.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prompt-edge-seam.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _live(self):
        ctl = controller.Controller(
            ProviderConfig(max_ticks=200),
            controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        r = controller._EpisodeRunner(ctl, proc, rec, result)
        r.mem.inventory.refresh([], 0, 0)
        return r, proc

    def test_live_context_seam_populates_origin_and_pending(self):
        r, _proc = self._live()
        need = _cmd(1)
        self.assertIsNone(r._reflex_context(need, 0.0).prompt_origin)
        r._on_obs(_rec(1, need, _MAP_TRAP, 100))
        r._answer_now(None)
        r._on_obs(_rec(2, _yn_need(2), _MAP_TRAP, 100))
        ctx = r._reflex_context(_yn_need(2), 0.0)
        self.assertIs(ctx.matched_movement_prompt, r.reflex.pending_prompt)
        self.assertIsNotNone(ctx.matched_movement_prompt)
        # the seam also carries the frozen origin *edge* of the confirmation
        self.assertIsNotNone(ctx.prompt_origin)
        self.assertEqual(tuple(ctx.prompt_origin.src), (10, 10))
        self.assertEqual(tuple(ctx.prompt_origin.dst), (11, 10))
        # the DTOs are frozen: the seam cannot be used to mutate them
        with self.assertRaises(Exception):
            ctx.matched_movement_prompt.src = (0, 0)
        # outside the prompt window the seam is None on both sides
        r.reflex.clear_pending_prompt()
        ctx = r._reflex_context(need, 0.0)
        self.assertIsNone(ctx.prompt_origin)
        self.assertIsNone(ctx.matched_movement_prompt)

    def test_evaluator_context_seam_populates_identically(self):
        p = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        p.invalids_by_key = {}
        p.mem.inventory.refresh([], 0, 0)
        p._feed_line(_line(hello()))
        captured = []
        original = p._propose

        def spy(ctx):
            captured.append(ctx)
            return original(ctx)

        p._propose = spy
        p._feed_line(_line(_rec(1, _cmd(1), _MAP_TRAP, 100)))
        p._feed_line(_line(_rec(2, _yn_need(2), _MAP_TRAP, 100)))
        self.assertTrue(captured)
        # the prompt-need context carried the frozen origin edge and the frozen
        # pending confirmation, exactly as the live path did
        prompt_ctxs = [c for c in captured
                       if c.matched_movement_prompt is not None]
        self.assertTrue(prompt_ctxs)
        for c in prompt_ctxs:
            self.assertIsNotNone(c.prompt_origin)
            self.assertEqual(tuple(c.prompt_origin.dst), (11, 10))
        # resolving the confirmation clears both seam fields
        p.reflex.clear_pending_prompt()
        self.assertIsNone(p.reflex.prompt_origin)
        self.assertIsNone(p.reflex.pending_prompt)


class CloudEncodingDisposition(unittest.TestCase):
    def test_cloud_encoding_disposition_is_native_or_manual(self):
        path = os.path.join(_ROOT, "doc",
                            "agent-prompt-edge-cloud-disposition.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertIn(rec["exit"], ("a", "b"))
        names = set(rec["elements"])
        self.assertEqual(names, set(
            __import__("native_cloud_probe").CLOUD_ELEMENTS))
        for name, el in rec["elements"].items():
            self.assertIn(el["status"],
                          ("native-recorded", "manual-required"), name)
        self.assertEqual(rec["encoding"]["classified_terrain"],
                         instances.T_CORRIDOR)
        self.assertTrue(rec["encoding"]["indistinguishable"])
        if rec["exit"] == "b":
            self.assertFalse(rec["positive_reopening_enabled"])
            self.assertTrue(rec["conservative_persistent_suppression"])
            self.assertTrue(rec["operator_gated_trace"]["mandatory"])


class LiveEvaluatorParity(unittest.TestCase):
    """AC5/F5: a REAL ``_EpisodeRunner`` vs ``ReplayPass`` frame-by-frame.

    Both paths are driven with the same wire: an actual gameplay command send,
    a validated cloud ``yn`` observation, an actual ``n`` send and the
    successor observation.  After every frame the complete per-frame state --
    stationary counter, pending identity, both ledgers, hero, lifecycle and
    applied counters -- must be identical.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prompt-edge.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _live(self, config=None):
        cfg = config or ProviderConfig(max_ticks=200)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        r = controller._EpisodeRunner(ctl, proc, rec, result)
        # a fresh, current inventory cache so the periodic inventory-refresh
        # maintenance branch never preempts the movement decision under test
        r.mem.inventory.refresh([], 0, 0)
        return r, rec, proc

    def _snap(self, x):
        ref = x.reflex
        pend = getattr(ref, "pending_prompt", None)
        return {
            "no_progress": x.mem.no_progress,
            "hero": x.mem.hero,
            "declined": dict(getattr(ref, "prompt_declined_edges", {})),
            "edges": dict(getattr(ref, "blocked_edges", {})),
            "pending": None if pend is None else (
                tuple(pend.src), tuple(pend.dst), pend.action_class,
                tuple(pend.response_need_key), bool(pend.answer_sent),
                pend.answer_byte),
            "lifecycle": len(ref.lifecycle.events),
            "applied": x.ledger.reflex_applied,
        }

    def test_live_evaluator_movement_prompt_accounting_and_ledger_parity(self):
        recs = [_rec(1, _cmd(1), _MAP_AROUND, 100),
                _rec(2, _yn_need(2), _MAP_AROUND, 100),
                _rec(3, _cmd(3), _MAP_AROUND, 102),
                _rec(4, _cmd(4), _MAP_AROUND, 103),
                _rec(5, _yn_need(1), _MAP_AROUND, 104),
                _rec(6, _cmd(6), _MAP_AROUND, 105)]
        r, _recorder, _proc = self._live()
        p = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        p.invalids_by_key = {}
        p.mem.inventory.refresh([], 0, 0)
        p._feed_line(_line(hello()))
        for frame in recs:
            r._on_obs(frame)
            r._answer_now(None)
            p._feed_line(_line(frame))
            self.assertEqual(self._snap(r), self._snap(p),
                             "frame seq %s" % frame["seq"])

    def test_real_live_vapor_loop_reroutes_and_learns_once(self):
        # the production path: send east into the cloud, observe the cloud yn,
        # send `n`, observe the successor -- the decline is learned exactly once
        # and the next command reroutes north along the alternate route
        r, _recorder, proc = self._live()
        r._on_obs(_rec(1, _cmd(1), _MAP_AROUND, 100))
        r._answer_now(None)
        acts = _parse_actions(proc.stdin.data)
        self.assertEqual(acts[-1]["action"], {"key": KEY_EAST})
        r._on_obs(_rec(2, _yn_need(2), _MAP_AROUND, 100))
        r._answer_now(None)
        acts = _parse_actions(proc.stdin.data)
        self.assertEqual(acts[-1]["action"], {"yn": protocol.KEY_N})
        # the prompt arrival counted the stationary stage exactly once
        self.assertEqual(r.mem.no_progress, 1)
        self.assertIsNotNone(r.reflex.pending_prompt)
        r._on_obs(_rec(3, _cmd(3), _MAP_AROUND, 102))
        r._answer_now(None)
        key = (1, (10, 10), (11, 10), "normal")
        self.assertIn(key, r.reflex.prompt_declined_edges)
        self.assertIsNone(r.reflex.pending_prompt)
        acts = _parse_actions(proc.stdin.data)
        self.assertEqual(acts[-1]["action"],
                         {"key": protocol.DIR_KEYS[(0, 1)]})

    def test_real_live_all_routes_blocked_is_bounded_and_learned_once(self):
        # the fully trapped fixture through the configured search/quit bound:
        # zero subsequent attempts on the learned edge after the first decline
        r, _recorder, proc = self._live()
        frames = [_rec(1, _cmd(1), _MAP_TRAP, 100),
                  _rec(2, _yn_need(2), _MAP_TRAP, 100),
                  _rec(3, _cmd(3), _MAP_TRAP, 102)]
        for i in range(4, 4 + 6):
            frames.append(_rec(i, _cmd(i), _MAP_TRAP, 100 + i))
        for frame in frames:
            r._on_obs(frame)
            r._answer_now(None)
        acts = _parse_actions(proc.stdin.data)
        east = [a for a in acts if a.get("action") == {"key": KEY_EAST}]
        # exactly one east attempt: the original acquisition, never again
        self.assertEqual(len(east), 1)
        # exactly one learned record, and the decline answer was sent once
        self.assertEqual(len(r.reflex.prompt_declined_edges), 1)
        n_answers = [a for a in acts
                     if a.get("action") == {"yn": protocol.KEY_N}]
        self.assertEqual(len(n_answers), 1)
        # the remaining actions are bounded recovery/search, never a
        # re-acquisition through the blocked edge
        for a in acts[2:]:
            self.assertNotEqual(a["action"], {"key": KEY_EAST}, a)

    def test_re_presented_same_id_prompt_stays_bound_live_and_replay(self):
        # Round-2 review F1: a legitimately re-presented identical cloud prompt
        # (same id, newer seq) must keep the bound decline transaction -- the
        # stable request identity, not full-seq equality, decides continuity
        recs = [_rec(1, _cmd(1), _MAP_TRAP, 100),
                _rec(2, _yn_need(2), _MAP_TRAP, 100),
                _rec(3, _yn_need(2), _MAP_TRAP, 100),   # same id, newer seq
                _rec(4, _cmd(4), _MAP_TRAP, 102)]       # the successor
        live, _r, _p = self._live()
        replay = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        replay.invalids_by_key = {}
        replay.mem.inventory.refresh([], 0, 0)
        replay._feed_line(_line(hello()))
        first_ordinal = None
        for frame in recs:
            live._on_obs(frame)
            live._answer_now(None)
            replay._feed_line(_line(frame))
            self.assertEqual(self._snap(live), self._snap(replay),
                             "frame seq %s" % frame["seq"])
            if frame["seq"] == 2:
                # armed, bound, counted exactly once
                self.assertIsNotNone(live.reflex.pending_prompt)
                self.assertTrue(live.reflex.pending_prompt.answer_sent)
                first_ordinal = live.reflex.pending_prompt.answer_ordinal
                self.assertEqual(live.mem.no_progress, 1)
            if frame["seq"] == 3:
                # the same-id re-presentation stays bound: no second stationary
                # count, no ledger write, and the original accounting unchanged
                self.assertIsNotNone(live.reflex.pending_prompt)
                self.assertTrue(live.reflex.pending_prompt.answer_sent)
                self.assertEqual(live.reflex.pending_prompt.answer_ordinal,
                                 first_ordinal)
                self.assertEqual(live.mem.no_progress, 1)
                self.assertEqual(live.reflex.prompt_declined_edges, {})
        # the eventual non-cloud successor writes exactly once
        self.assertEqual(len(live.reflex.prompt_declined_edges), 1)
        self.assertIsNone(live.reflex.pending_prompt)
        self.assertIsNone(live.reflex.prompt_origin)

    def test_re_presented_different_prompt_id_discards_live_and_replay(self):
        # Round-2 review F1: a cloud confirmation with a *different* prompt id is
        # a replacement, discarded without writing
        recs = [_rec(1, _cmd(1), _MAP_TRAP, 100),
                _rec(2, _yn_need(2), _MAP_TRAP, 100),
                _rec(3, _yn_need(3), _MAP_TRAP, 101),   # different id
                _rec(4, _cmd(4), _MAP_TRAP, 102)]
        live, _r, _p = self._live()
        replay = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        replay.invalids_by_key = {}
        replay.mem.inventory.refresh([], 0, 0)
        replay._feed_line(_line(hello()))
        for frame in recs:
            live._on_obs(frame)
            live._answer_now(None)
            replay._feed_line(_line(frame))
            self.assertEqual(self._snap(live), self._snap(replay),
                             "frame seq %s" % frame["seq"])
            if frame["seq"] == 3:
                self.assertIsNone(live.reflex.pending_prompt)
        self.assertEqual(live.reflex.prompt_declined_edges, {})
        self.assertEqual(live.mem.no_progress, 1)

    def test_evaluator_invalid_clears_prompt_transaction_like_live(self):
        # Round-2 review F2: an `invalid` in the replay must clear the pending
        # prompt context exactly as the live controller does, so a stale bound
        # answer can never resolve against a later non-cloud observation
        recs = [_rec(1, _cmd(1), _MAP_TRAP, 100),
                _rec(2, _yn_need(2), _MAP_TRAP, 100)]
        live, _r, _p = self._live()
        replay = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        replay.invalids_by_key = {}
        replay.mem.inventory.refresh([], 0, 0)
        replay._feed_line(_line(hello()))
        for frame in recs:
            live._on_obs(frame)
            live._answer_now(None)                 # sends the `n` decline
            replay._feed_line(_line(frame))
        self.assertTrue(live.reflex.pending_prompt.answer_sent)
        self.assertTrue(replay.reflex.pending_prompt.answer_sent)
        # both paths clear on the same invalid
        invalid = _line({"v": 1, "ch": "control", "type": "invalid",
                         "code": "kind"})
        live._on_invalid({"type": "invalid", "code": "kind"})
        replay._feed_line(invalid)
        self.assertIsNone(live.reflex.pending_prompt)
        self.assertIsNone(replay.reflex.pending_prompt)
        self.assertIsNone(live.reflex.prompt_origin)
        self.assertIsNone(replay.reflex.prompt_origin)
        self.assertEqual(live.reflex.prompt_declined_edges, {})
        self.assertEqual(replay.reflex.prompt_declined_edges, {})
        # a later non-cloud observation writes no false evidence in either path
        live._on_obs(_rec(4, _cmd(4), _MAP_TRAP, 102))
        live._answer_now(None)
        replay._feed_line(_line(_rec(4, _cmd(4), _MAP_TRAP, 102)))
        self.assertEqual(live.reflex.prompt_declined_edges, {})
        self.assertEqual(replay.reflex.prompt_declined_edges, {})

    def test_evaluator_close_clears_prompt_transaction_and_seam(self):
        # Round-2 review F2: closing the episode clears the pending context and
        # both context-seam fields in the replay as well as live
        recs = [_rec(1, _cmd(1), _MAP_TRAP, 100),
                _rec(2, _yn_need(2), _MAP_TRAP, 100)]
        live, _r, _p = self._live()
        replay = evaluate.ReplayPass(
            [], ProviderConfig(reflex="scripted", strategy="off",
                               max_ticks=200), "scripted", "off")
        replay.invalids_by_key = {}
        replay.mem.inventory.refresh([], 0, 0)
        replay._feed_line(_line(hello()))
        for frame in recs:
            live._on_obs(frame)
            live._answer_now(None)
            replay._feed_line(_line(frame))
        self.assertIsNotNone(replay.reflex.pending_prompt)
        self.assertIsNotNone(replay.reflex.prompt_origin)
        replay._feed_line(_line({"v": 1, "ch": "control", "type": "closed"}))
        live._on_closed({"type": "closed"})
        for x in (live, replay):
            self.assertIsNone(x.reflex.pending_prompt, x)
            self.assertIsNone(x.reflex.prompt_origin, x)
        # the live context seam is also None after close
        ctx = live._reflex_context(_cmd(9), 0.0)
        self.assertIsNone(ctx.prompt_origin)
        self.assertIsNone(ctx.matched_movement_prompt)

    def test_real_live_invalid_clears_then_retry_rebinds(self):
        # review F1: an `invalid` after a sent answer discards the pending
        # context, so a stale answer can never write evidence; a fresh
        # confirmation then rebinds correctly
        r, _recorder, _proc = self._live()
        r._on_obs(_rec(1, _cmd(1), _MAP_TRAP, 100))
        r._answer_now(None)
        r._on_obs(_rec(2, _yn_need(2), _MAP_TRAP, 100))
        self.assertIsNotNone(r.reflex.pending_prompt)
        r._answer_now(None)                       # the `n` decline is bound
        self.assertTrue(r.reflex.pending_prompt.answer_sent)
        r._on_invalid({"type": "invalid", "code": "kind"})
        self.assertIsNone(r.reflex.pending_prompt)
        self.assertEqual(r.reflex.prompt_declined_edges, {})
        # a fresh confirmation now rebinds from the cleared state: a new
        # movement send, then its confirmation, then the successor observation
        r._on_obs(_rec(3, _cmd(3), _MAP_TRAP, 101))
        r._answer_now(None)
        r._on_obs(_rec(4, _yn_need(4), _MAP_TRAP, 101))
        self.assertIsNotNone(r.reflex.pending_prompt)
        r._answer_now(None)
        r._on_obs(_rec(5, _cmd(5), _MAP_TRAP, 102))
        self.assertEqual(len(r.reflex.prompt_declined_edges), 1)
        self.assertIsNone(r.reflex.pending_prompt)

    def test_ambiguous_hero_frame_arms_no_context_real_path(self):
        # review F2 through the production path: when the reconciled confirmed
        # hero is *not* the frozen source square, no context is armed and no
        # stationary count happens -- even though the frame's raw first `@` is
        # the source square (which the old `staged.hero` path would have matched)
        from unittest import mock
        r, _recorder, _proc = self._live()
        r._on_obs(_rec(1, _cmd(1), _MAP_TRAP, 100))
        r._answer_now(None)
        frame = _rec(2, _yn_need(2), _MAP_TRAP, 100)
        snap = protocol.Snapshot()
        snap.apply(frame)
        staged = state.EpisodeMemory().stage(snap)
        self.assertEqual(staged.hero, (10, 10))   # the raw first @ IS the source
        before = r.mem.no_progress
        original = r._reconcile_observation

        def ambiguous(staged_obs):
            original(staged_obs)
            r._resolved_hero = None               # reconciled: not confirmed

        with mock.patch.object(r, "_reconcile_observation", ambiguous):
            r._on_obs(frame)
        self.assertIsNone(r.reflex.pending_prompt)
        self.assertEqual(r.mem.no_progress, before)


# ------------------------------------------------------------------ helpers

VAPOR_CELL = fx.VAPOR

# A wire palette/map for the production-path fixtures: 0 blank, 1 floor, 2 the
# hero, 3 a gray '#' vapor cell (corridor), 4 a wall.
_PAL = [[0, " ", "none", 0, "none"], [1, ".", "gray", 0, "none"],
        [2, "@", "white", 0, "none"], [3, "#", "gray", 0, "none"],
        [4, "|", "gray", 0, "none"]]
# hero (10,10), the vapor cell east (11,10) and a floor frontier (12,10) whose
# unknown neighbour (13,10) elects it; the rest is walled so the vapor edge is
# the only route (the fully trapped variant).
_MAP_TRAP = ([[x, y, 4] for x in range(8, 15) for y in range(7, 15)]
             + [[10, 10, 2], [11, 10, 3], [12, 10, 1]])
_MAP_TRAP = [t for t in _MAP_TRAP if not (t[0] == 13 and t[1] == 10)]
# the same vapor edge plus a strictly longer legal south detour to (12,10), so
# the east step is the unique cheapest route and the detour is the only
# alternate once it is suppressed
_MAP_AROUND = ([[x, y, 4] for x in range(8, 15) for y in range(7, 15)]
               + [[10, 10, 2], [11, 10, 3], [12, 10, 1],
                  [10, 11, 1], [10, 12, 1], [11, 12, 1], [12, 12, 1],
                  [12, 11, 1]])
_MAP_AROUND = [t for t in _MAP_AROUND if not (t[0] == 13 and t[1] == 10)]


def _cmd(i):
    return {"id": i, "kind": "command", "prompt": ""}


def _yn_need(i, prompt=fx.VAPOR_PROMPT):
    return {"id": i, "kind": "yn", "prompt": prompt, "choices": None,
            "default": None, "numeric": False}


def _rec(seq, need, map_, t=100):
    rec = obs(seq, need,
              map_=sorted(map_, key=lambda tr: (tr[1], tr[0])), pal=_PAL)
    rec["s"] = {"hitpoints": {"text": "10"},
                "hitpoints-max": {"text": "10"},
                "time": {"text": str(t)},
                "dungeon-level": {"text": "1"}}
    return rec


def stall_fx_ep4_corridor():
    return {(10, 10): FLOOR, (11, 10): FLOOR, (9, 10): WALL,
            (10, 9): WALL, (10, 11): WALL}


def _origin_for(ref, src):
    return arbitration.movement_origin_from_selected(
        _nav_candidate(
            payload=("dest", "acquire", 7, "x", src[0] + 1, src[1],
                     "frontier", "default", 0, -1, "")),
        "command", ref.instance_id, src, ((1, 1, 1), "t", "c", 1))


def _staged(mem):
    return state.StagedObservation(
        cells={}, stairs_down=frozenset(), stairs_up=frozenset(),
        hero=mem.hero, status=mem.status, messages=(), hero_cells=())


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()
