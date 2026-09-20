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
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stall_recovery_fixtures as fx  # noqa: E402
from tools.agent import (arbitration, candidates, instances,  # noqa: E402
                         navigation, policy, protocol, state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402

FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")
MONSTER = (":", "gray", 0, "none")
STAIR_UP = ("<", "gray", 0, "none")
STAIR_DOWN = (">", "gray", 0, "none")

KEY_EAST = protocol.DIR_KEYS[(1, 0)]
KEY_NORTH = protocol.DIR_KEYS[(0, -1)]
KEY_WEST = protocol.DIR_KEYS[(-1, 0)]


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
                           (classless, "classless"), (synth, "synthesized")):
            self.assertIsNone(self._origin(cand), name)

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
        self.assertTrue(self.ref.arm_movement_prompt(origin, yn, 7, (10, 10)))
        pending = self.ref.pending_prompt
        self.assertIsNotNone(pending)
        self.assertEqual((pending.src, pending.dst), ((10, 10), (11, 10)))
        # a re-presentation creates no second context and earns no second count
        self.assertFalse(self.ref.arm_movement_prompt(origin, yn, 7, (10, 10)))
        # the answer result must not count again: no second arm is possible
        self.ref.note_prompt_answer_sent(3)
        self.assertFalse(self.ref.arm_movement_prompt(origin, yn, 7, (10, 10)))

    def test_unrelated_yn_and_interaction_direction_do_not_advance_stationary(
            self):
        origin = self._origin()
        for need in ({"kind": "yn", "id": 2, "prompt": "Really quit?"},
                     {"kind": "yn", "id": 2, "prompt": "Do you want to eat?"},
                     {"kind": "yn", "id": 2, "prompt": "Shall I pick up?"},
                     {"kind": "menu", "id": 2, "prompt": "inventory"},
                     {"kind": "yn", "id": 2},
                     {"kind": "yn", "id": 2, "prompt": "Step into water?"}):
            self.assertFalse(
                self.ref.arm_movement_prompt(origin, need, 7, (10, 10)), need)
        # no frozen origin (an unmatched or non-movement frame) counts nothing
        self.assertFalse(self.ref.arm_movement_prompt(
            None, {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}, 7,
            (10, 10)))

    def test_prompt_replay_unknown_hero_and_instance_change_do_not_double_count(
            self):
        origin = self._origin()
        yn = {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT}
        # unknown hero
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 7, None))
        # instance change
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 8, (10, 10)))
        # a moved hero is not a stationary confirmation
        self.assertIsNone(arbitration.matched_movement_prompt(
            origin, yn, 7, (11, 10)))
        self.assertTrue(self.ref.arm_movement_prompt(origin, yn, 7, (10, 10)))
        self.assertFalse(self.ref.arm_movement_prompt(origin, yn, 7, (10, 10)))

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
            origin, {"kind": "menu", "id": 2}, 7, (10, 10)))


# ---------------------------------------------- AC2: decline evidence rules

class DeclineEvidence(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))

    def _arm(self, src=(10, 10), dst=(11, 10), action_class="normal"):
        cand = _nav_candidate(
            payload=("dest", "acquire", 7, "x", dst[0], dst[1], "frontier",
                     "default", 0, -1, ""))
        origin = arbitration.movement_origin_from_selected(
            cand, "command", self.ref.instance_id, src, ((1, 1, 1), "t", "c", 1))
        self.ref.arm_movement_prompt(
            origin, {"kind": "yn", "id": 2, "prompt": fx.VAPOR_PROMPT},
            self.ref.instance_id, src)
        return origin

    def test_declined_movement_records_origin_edge_not_destination_or_answer(
            self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self._arm()
        self.ref.note_prompt_answer_sent(3)
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
        # a genuine send then resolves exactly once
        self.ref.note_prompt_answer_sent(3)
        self.assertTrue(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), True))
        self.assertIsNone(self.ref.pending_prompt)
        # a stale `n` (confirmation still up / replaced) writes nothing
        self._arm()
        self.ref.note_prompt_answer_sent(4)
        before = dict(self.ref.prompt_declined_edges)
        self.assertFalse(self.ref.resolve_prompt_decline(
            mem, self.ref.instance_id, (10, 10), False))
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
        self.ref.note_prompt_answer_sent(3)
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

    def test_prompt_edge_reopens_on_positive_local_cloud_change(self):
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))
        # a positive relevant local change at the attempted destination (a
        # monster appears) reopens the edge by its signature
        mem.grid[(11, 10)] = MONSTER
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

    def test_prompt_edge_signature_preserves_diagonal_side_evidence(self):
        cells = {(10, 10): FLOOR, (11, 11): FLOOR, (11, 10): FLOOR,
                 (10, 11): FLOOR}
        mem = mem_with(cells, (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 11), "normal", mem,
            fx.VAPOR_PROMPT)
        self.assertTrue(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 11)))
        mem.grid[(11, 10)] = WALL           # a diagonal side cell changes
        self.assertFalse(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 11)))

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
        mem = mem_with(fx.vapor_corridor_cells(), (10, 10))
        self.ref.record_prompt_decline(
            self.ref.instance_id, (10, 10), (11, 10), "normal", mem,
            fx.VAPOR_PROMPT)
        # while suppressed the frontier beyond is unreachable and no longer
        # elected (a route-only failure, evidence-owned by the edge)
        plan = navigation.plan(
            self.ref._terrain(mem), (10, 10), mem.visits, None,
            edge_admissible=self.ref._edge_admissible(
                self.ref._terrain(mem), navigation.ACTION_NORMAL))
        self.assertNotIn((12, 10), plan.dist)
        # once the edge reopens (positive local change) the target is electable
        mem.grid[(11, 10)] = MONSTER
        plan2 = navigation.plan(
            self.ref._terrain(mem), (10, 10), mem.visits, None,
            edge_admissible=self.ref._edge_admissible(
                self.ref._terrain(mem), navigation.ACTION_NORMAL))
        # the reopened edge is legal again for planning purposes
        self.assertFalse(self.ref.edge_suppressed(
            self.ref._terrain(mem), (10, 10), (11, 10)))


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
    def test_live_evaluator_movement_prompt_accounting_and_ledger_parity(self):
        # both paths derive the movement origin and the pending context through
        # the SAME pure arbitration seam and the SAME ordering
        ctrl = _read("tools/agent/controller.py")
        ev = _read("tools/agent/evaluate.py")
        for src in (ctrl, ev):
            self.assertIn("arbitration.movement_origin_from_selected", src)
            self.assertIn("_capture_movement_prompt", src)
            self.assertIn("_resolve_pending_prompt", src)
            self.assertIn("advance_stationary=bool(", src)
        self.assertIn("_bind_prompt_answer", ctrl)
        self.assertIn("arm_movement_prompt", ctrl)
        self.assertIn("arm_movement_prompt", ev)
        # the accounting term is the identity-bound matched-prompt boolean
        for src in (ctrl, ev):
            self.assertIn("or matched_prompt", src)
        # both bind a sent yn answer identically
        self.assertIn("note_prompt_answer_sent", ev)


# ------------------------------------------------------------------ helpers

VAPOR_CELL = fx.VAPOR


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
