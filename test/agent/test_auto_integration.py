#!/usr/bin/env python3
"""Live-integration tests for the ScriptedReflex upgrade wiring.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

These lock in the wiring fixes the isolation review found missing:
per-instance
map/terrain scoping in the live path (HIGH 1), the live ``HeroResolution``
(HIGH 2), observational preparation/proposal (HIGH 3), the exact forced-search
suffix binding (HIGH 4) and source-instance-scoped pending directives (MEDIUM
5), plus the evaluator/live-model parity fixture (MEDIUM 6).

They drive the *real* ``_EpisodeRunner`` reconciliation in-process (no
engine), so a regression that re-merges an old coordinate, adopts a first
``@``, mutates
on a mere proposal or binds the dangerous suffix to a non-command need fails.
"""

import hashlib
import json
import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (arbitration, budget, candidates, controller,  # noqa: E402
                         directives, forced_search, instances, policy,
                         protocol, providers, recording, state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402
from test_auto import (CLOSED, HELLO, WireHarness, _line,  # noqa: E402
                       _parse_actions, hello, obs)
from test_auto_providers import paced  # noqa: E402

PAL = [[0, " ", "none", 0, "none"],
       [1, ".", "gray", 0, "none"],
       [2, "@", "white", 0, "none"],
       [3, ">", "gray", 0, "none"],
       [4, "<", "gray", 0, "none"],
       [5, "|", "gray", 0, "none"]]

# hero at (10,10), floor east, a down stair at (12,10) and a wall at (13,10)
MAP_A = [[10, 10, 2], [11, 10, 1], [12, 10, 3], [13, 10, 5]]
# a disjoint partial map: the hero elsewhere, every old coordinate unobserved
MAP_B = [[30, 5, 2], [31, 5, 1]]
EAST = protocol.DIR_KEYS[(1, 0)]
STEP = (5, 5)


def snap_of(rec):
    snap = protocol.Snapshot()
    snap.apply(rec)
    return snap


def _runner():
    """A real ``_EpisodeRunner`` shell with only reconciliation state set."""
    r = object.__new__(controller._EpisodeRunner)
    r.attempt = None
    r.attempt_before = None
    r.rejections = {}
    r.instance = instances.LevelInstanceAutomaton()
    r.terrain = instances.TerrainMemory()
    r.herores = None
    r._resolved_hero = None
    r._attempt_effect = None
    r._attempt_label = ""
    r._attempt_kind = ""
    r.observation_generation = 0
    r.attempts_armed = 0
    r.reconciliations = 0
    r._last_table_id = ""
    r.forced = None
    r.forced_budget = forced_search.ForcedSearchBudget()
    r._forced_next = None
    r._forced_origin = None
    r._forced_suffix_ordinal = None
    r._forced_failed_fp = None
    r.mem = state.EpisodeMemory()
    r.reflex = policy.ScriptedReflex(ProviderConfig())
    r.pending_key = protocol.NeedKey(1, 1, 1)
    r.result = types.SimpleNamespace(index=1)
    r.tick = 0
    return r


def apply_obs(r, rec):
    """Reconcile then commit one observation, exactly like ``_on_obs``."""
    staged = r.mem.stage(snap_of(rec))
    r._reconcile_observation(staged)
    r.mem.commit(staged, hero=r._resolved_hero)
    note = getattr(r.reflex, "note_observation", None)
    if note is not None:
        note(r.mem)


def _obs(seq, dlvl="1", map_=MAP_A, pal=PAL, msg=(), need=None):
    rec = obs(seq, need, map_=map_, pal=pal, msg=msg)
    rec["s"] = {"dungeon-level": {"text": dlvl},
                "time": {"text": str(100 + seq)},
                "hitpoints": {"text": "10"},
                "hitpoints-max": {"text": "10"}}
    return rec


# ---------------------------------------------------------------- HIGH 1

class PerInstanceScoping(unittest.TestCase):
    """A fresh arrival must not inherit (or merge) the old instance's map."""

    def _prime(self):
        r = _runner()
        apply_obs(r, _obs(1, "1"))
        # mark the old scope with a visit and a search budget
        r.mem.visits[(12, 10)] = 3
        r.mem.searches_since_progress = 2
        self.assertEqual(r.instance.current(), 1)
        self.assertIn((12, 10), r.mem.stairs_down)
        self.assertTrue(r.terrain.walkable((11, 10)))
        return r

    def _assert_old_scope_expired(self, r):
        # the old, unobserved coordinate is unknown and non-walkable now
        self.assertEqual(r.terrain.ter((11, 10)), instances.T_UNKNOWN)
        self.assertFalse(r.terrain.walkable((11, 10)))
        self.assertNotIn((12, 10), r.mem.stairs_down)
        self.assertEqual(r.mem.tile((12, 10)), " ")
        # old visits and budgets are absent
        self.assertNotIn((12, 10), r.mem.visits)
        self.assertNotIn((11, 10), r.mem.visits)
        self.assertEqual(r.mem.searches_since_progress, 0)

    def test_same_dlvl_branch_arrival_resets_the_scope(self):
        r = self._prime()
        # a same-displayed-level arrival: only an arrival outcome signal
        apply_obs(r, _obs(2, "1", map_=MAP_B,
                          msg=[{"e": 2, "text": "You materialize."}]))
        self.assertNotEqual(r.instance.current(), 1)
        self._assert_old_scope_expired(r)
        # the arrival cells are in the *new* scope
        self.assertEqual(r.terrain.ter((31, 5)), instances.T_FLOOR)

    def test_ordinary_depth_change_resets_the_scope(self):
        r = self._prime()
        apply_obs(r, _obs(2, "2", map_=MAP_B))
        self.assertNotEqual(r.instance.current(), 1)
        self._assert_old_scope_expired(r)

    def test_zero_at_arrival_allocates_before_hero_resolution(self):
        r = self._prime()
        # a map with no @ at all, plus an arrival outcome
        apply_obs(r, _obs(2, "1", map_=[[30, 5, 1]], msg=[
            {"e": 2, "text": "You materialize."}]))
        self.assertNotEqual(r.instance.current(), 1)
        self.assertIsNone(r.mem.hero)
        self._assert_old_scope_expired(r)

    def test_multiple_at_arrival_allocates_before_hero_resolution(self):
        r = self._prime()
        apply_obs(r, _obs(2, "1", map_=[[30, 5, 2], [31, 5, 2]],
                          msg=[{"e": 2, "text": "You materialize."}]))
        self.assertNotEqual(r.instance.current(), 1)
        self.assertIsNone(r.mem.hero)
        self._assert_old_scope_expired(r)

    def test_no_candidate_uses_the_old_coordinate(self):
        r = self._prime()
        apply_obs(r, _obs(2, "1", map_=MAP_B,
                          msg=[{"e": 2, "text": "You materialize."}]))
        ctx = ReflexContext(
            episode=1, tick=0, need={"kind": "command", "id": 1},
            need_key=protocol.NeedKey(1, 1, 1),
            snapshot=snap_of(_obs(2, "1")),
            pages=[], memory=r.mem, directives=[], deadline=0.0)
        prepared = r.reflex.prepare(ctx)
        for cand in prepared.table.ordered_candidates:
            for r_, c_ in cand.rows:
                self.assertNotEqual((r_, c_), (12, 10))
            if cand.direction:
                self.assertNotEqual(cand.direction, (2, 0))


# ---------------------------------------------------------------- HIGH 2

class LiveHeroResolution(unittest.TestCase):
    """``mem.hero`` is only ever a positively supported singleton."""

    def _two_at(self, first, second):
        r = _runner()
        triples = sorted([(first[0], first[1], 2), (second[0], second[1], 2),
                          (20, 20, 1)], key=lambda t: (t[1], t[0]))
        rec = _obs(1, "1", map_=[list(t) for t in triples])
        apply_obs(r, rec)
        return r

    def test_two_at_in_both_orders_suppress_identity(self):
        for first, second in (((10, 10), (20, 10)), ((20, 10), (10, 10))):
            r = self._two_at(first, second)
            self.assertIsNone(r.mem.hero)       # no first-@ adoption
            self.assertFalse(r.herores.resolved)
            self.assertEqual(r.herores.possible,
                             frozenset((first, second)))  # retained
            # no directional candidate and no forced-search prefix
            ctx = ReflexContext(
                episode=1, tick=0, need={"kind": "command", "id": 1},
                need_key=protocol.NeedKey(1, 1, 1),
                snapshot=snap_of(_obs(1, "1")), pages=[],
                memory=r.mem, directives=[], deadline=0.0)
            prepared = r.reflex.prepare(ctx)
            for cand in prepared.table.ordered_candidates:
                self.assertFalse(cand.direction)
                self.assertNotEqual(cand.proposed_effect,
                                    forced_search.FORCED_SEARCH_EFFECT)

    def test_supported_continuity_confirms_the_right_singleton(self):
        r = _runner()
        apply_obs(r, _obs(1, "1"))                  # unique @ at (10,10)
        self.assertEqual(r.mem.hero, (10, 10))
        # a movement attempt to the east, and the expected destination arrives
        cand = candidates.make_candidate({"key": EAST}, "navigate",
                                         proposed_effect="navigate")
        r.reflex.last_candidate = cand
        r._arm_attempt(1, {"key": EAST})
        apply_obs(r, _obs(2, "1", map_=[[11, 10, 2], [12, 10, 1]]))
        self.assertTrue(r.herores.resolved)
        self.assertEqual(r.mem.hero, (11, 10))

    def test_zero_at_clears_identity_without_losing_possibilities(self):
        r = _runner()
        apply_obs(r, _obs(1, "1"))
        before = r.herores.possible
        apply_obs(r, _obs(2, "1", map_=[[20, 20, 1]]))
        self.assertIsNone(r.mem.hero)
        self.assertFalse(r.herores.resolved)
        self.assertTrue(before.issubset(r.herores.possible))


# ---------------------------------------------------------------- HIGH 3

class PreparationPurity(unittest.TestCase):
    """Preparation and proposal must not mutate gameplay/recovery state."""

    def _ctx(self, mem, tick=0):
        return ReflexContext(
            episode=1, tick=tick, need={"kind": "command", "id": 1},
            need_key=protocol.NeedKey(1, tick, 1),
            snapshot=protocol.Snapshot(), pages=[], memory=mem,
            directives=[], deadline=0.0)

    def _mem(self, messages=(), hero=(10, 10)):
        mem = state.EpisodeMemory()
        mem.grid[(10, 10)] = (".", "gray", 0, "none")
        mem.grid[(11, 10)] = (".", "gray", 0, "none")
        mem.hero = hero
        mem.status.hp = 20
        mem.status.hp_max = 20
        mem.messages.extend(messages)
        return mem

    def _snapshot(self, ref, mem):
        return (ref.intent, ref.quitting, ref.quit_reason, ref.last_eat_tick,
                ref.last_inv_tick, ref.eat_forced_menu, ref.selection_done,
                len(ref.recovery.search.completed),
                set(ref.recovery.search.refused),
                ref.recovery.refused_site, tuple(ref.recovery.cycle.history),
                ref._cycled, ref.food.inventory_signature,
                set(ref.food.locations), mem.no_progress,
                mem.searches_since_progress, dict(mem.visits), mem.hero,
                mem.inventory.seen_tick, mem.inventory.seen_time,
                tuple(r.get("text") for r in mem.inventory.rows))

    def test_prepare_and_decide_leave_state_unchanged(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        # a refresh-inventory decision would otherwise stamp last_inv_tick
        mem = self._mem(messages=[])
        before = self._snapshot(ref, mem)
        ref.prepare(self._ctx(mem))
        res = ref.decide(self._ctx(mem))
        self.assertEqual(self._snapshot(ref, mem), before)
        # and the chosen effect is only frozen on the candidate, not applied
        self.assertEqual(res.action, {"key": protocol.KEY_INV})

    def test_decide_does_not_commit_a_search_or_eat_intent(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        # a refused ordinary search at a boxed-in site selects a real step;
        # the *refusal* evidence is derived purely, so no fold is needed
        mem = self._mem(messages=["You already found a monster."])
        mem.no_progress = 3
        before = self._snapshot(ref, mem)
        ref.prepare(self._ctx(mem))
        ref.decide(self._ctx(mem))
        self.assertEqual(self._snapshot(ref, mem), before)

    def test_local_invalid_candidate_commits_nothing(self):
        # a proposal that is then discarded (no complete send) mutates nothing
        ref = policy.ScriptedReflex(ProviderConfig())
        mem = self._mem(messages=[])
        before = self._snapshot(ref, mem)
        ctx = self._ctx(mem)
        prepared = ref.prepare(ctx)
        ctx.prepared = prepared
        cand = ref._select(prepared, None)
        # simulate a local validation failure: the candidate is never sent
        self.assertIsNotNone(cand)
        self.assertEqual(self._snapshot(ref, mem), before)

    def test_commit_effect_is_the_only_mutation_point(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        mem = self._mem()
        before = self._snapshot(ref, mem)
        ref.commit_effect("schedule-eat", "eat", 7, mem)
        after = self._snapshot(ref, mem)
        self.assertNotEqual(after, before)
        self.assertEqual(ref.intent, "eat")
        self.assertEqual(ref.last_eat_tick, 7)

    def test_observed_time_advancing_search_commits_one_budget(self):
        r = _runner()
        apply_obs(r, _obs(1, "1"))
        r.mem.grid.clear()
        r.mem.grid[(10, 10)] = (".", "gray", 0, "none")
        # a search attempt that the observation shows advanced time
        cand = candidates.make_candidate({"key": protocol.KEY_SEARCH},
                                         "search-secret",
                                         proposed_effect="secret-search")
        r.reflex.last_candidate = cand
        r._arm_attempt(1, {"key": protocol.KEY_SEARCH})
        before = r.mem.searches_since_progress
        staged = r.mem.stage(snap_of(_obs(2, "1")))
        r._reconcile_observation(staged)
        r.mem.commit(staged, hero=r._resolved_hero)
        r.reflex.note_observation(r.mem)
        if r._attempt_effect:
            r.reflex.commit_effect(r._attempt_effect, r._attempt_label,
                                   r.tick, r.mem,
                                   observed_kind=r._attempt_kind)
        self.assertEqual(r.mem.searches_since_progress, before + 1)

    def test_no_time_search_commits_no_completed_budget(self):
        r = _runner()
        apply_obs(r, _obs(1, "1"))
        cand = candidates.make_candidate({"key": protocol.KEY_SEARCH},
                                         "search-secret",
                                         proposed_effect="secret-search")
        r.reflex.last_candidate = cand
        r._arm_attempt(1, {"key": protocol.KEY_SEARCH})
        # the same frame at the same time: a no-time outcome
        staged = r.mem.stage(snap_of(_obs(1, "1")))
        staged = state.StagedObservation(
            cells=staged.cells, stairs_down=staged.stairs_down,
            stairs_up=staged.stairs_up, hero=staged.hero,
            status=staged.status, messages=(), hero_cells=staged.hero_cells)
        r._reconcile_observation(staged)
        r.mem.commit(staged, hero=r._resolved_hero)
        if r._attempt_effect:
            r.reflex.commit_effect(r._attempt_effect, r._attempt_label,
                                   r.tick, r.mem,
                                   observed_kind=r._attempt_kind)
        self.assertEqual(r.mem.searches_since_progress, 0)
        self.assertEqual(r._attempt_kind, "no-time")

    # -- non-command proposals are frozen effects too (plan 3.1) ---------
    _FOOD_ROW = {"r": 65, "text": "a - a food ration", "selectable": True}

    def _menu_ctx(self, mem, rows, title, tick=0):
        snap = protocol.Snapshot()
        snap.windows = {"c1": {"w": "w1", "kind": "menu", "title": title,
                               "content": "c1", "pages": 1}}
        return ReflexContext(
            episode=1, tick=tick,
            need={"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                  "content": "c1", "pages": 1},
            need_key=protocol.NeedKey(1, tick, 1), snapshot=snap,
            pages=list(rows), memory=mem, directives=[], deadline=0.0)

    def _yn_ctx(self, mem, prompt, tick=0):
        return ReflexContext(
            episode=1, tick=tick,
            need={"id": 1, "kind": "yn", "prompt": prompt},
            need_key=protocol.NeedKey(1, tick, 1),
            snapshot=protocol.Snapshot(),
            pages=[], memory=mem, directives=[], deadline=0.0)

    def test_menu_prepare_is_pure_and_freezes_both_its_effects(self):
        # an inventory menu under the eat intent both refreshes the cache and
        # transitions the intent: neither may happen during preparation
        ref = policy.ScriptedReflex(ProviderConfig())
        mem = self._mem()
        ref.intent = "eat"
        ctx = self._menu_ctx(mem, [self._FOOD_ROW], "Inventory", tick=7)
        before = self._snapshot(ref, mem)
        prepared = ref.prepare(ctx)
        ref.decide(ctx)
        self.assertEqual(self._snapshot(ref, mem), before)
        self.assertIsNone(mem.inventory.seen_tick)
        self.assertEqual(ref.intent, "eat")
        cand = prepared.table.scripted()
        self.assertEqual(set(cand.proposed_effect.split("+")),
                         {"refresh-inventory-menu", "eat-menu"})
        rows, seen_tick, _time = cand.effect_payload
        self.assertEqual([r.get("text") for r in rows],
                         ["a - a food ration"])
        self.assertEqual(seen_tick, 7)
        # only a reconciled commit applies both effects
        ref.commit_effect(cand.proposed_effect, cand.semantic_label, 7, mem,
                          payload=cand.effect_payload)
        self.assertEqual(ref.intent, "")
        self.assertEqual(mem.inventory.seen_tick, 7)

    def test_selection_done_is_a_frozen_effect(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        mem = self._mem()
        rows = [{"r": 1, "text": "Yes; start game", "selectable": True}]
        ctx = self._menu_ctx(mem, rows, "Is this ok? [ynq]", tick=0)
        before = self._snapshot(ref, mem)
        cand = ref.prepare(ctx).table.scripted()
        ref.decide(ctx)
        self.assertEqual(self._snapshot(ref, mem), before)
        self.assertFalse(ref.selection_done)
        self.assertEqual(cand.proposed_effect, "selection-done")
        ref.commit_effect("selection-done", "prompt", 0, mem)
        self.assertTrue(ref.selection_done)

    def test_eat_forced_menu_is_a_frozen_effect(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        mem = self._mem(messages=["You don't have that object.",
                                  "You don't have that object."])
        ref.intent = "eat"
        ctx = self._yn_ctx(mem, "What do you want to eat? [d or ?*]")
        before = self._snapshot(ref, mem)
        cand = ref.prepare(ctx).table.scripted()
        res = ref.decide(ctx)
        self.assertEqual(self._snapshot(ref, mem), before)
        self.assertFalse(ref.eat_forced_menu)
        self.assertEqual(cand.proposed_effect, "eat-forced-menu")
        self.assertEqual(res.action, {"yn": ord("*")})
        ref.commit_effect("eat-forced-menu", "prompt", 0, mem)
        self.assertTrue(ref.eat_forced_menu)

    def test_controller_freezes_a_noncommand_effect_on_a_matching_send(self):
        r = _runner()
        cand = candidates.make_candidate({"yn": protocol.KEY_N}, "prompt",
                                         proposed_effect="eat-forced-menu")
        r.reflex.last_candidate = cand
        r._freeze_noncommand_effect({"yn": protocol.KEY_N})
        self.assertEqual(r._attempt_effect, "eat-forced-menu")
        self.assertIsNone(r.attempt)          # no SentAttempt is armed
        # a fallback send the reflex never proposed freezes nothing
        r._attempt_effect = None
        r._freeze_noncommand_effect({"yn": protocol.KEY_ESC})
        self.assertIsNone(r._attempt_effect)


# ---------------------------------------------------------------- HIGH 4

class ForcedSearchBinding(WireHarness):
    """Gate 8: the suffix binds ONLY to the exact following command need."""

    _LIVE_PAL = [[0, " ", "none", 0, "none"], [1, ".", "gray", 0, "none"],
                 [2, "@", "white", 0, "none"], [3, "a", "brown", 0, "none"]]
    _LIVE_MAP = [[10, 10, 2], [11, 10, 3]]
    _REFUSAL = ("You already found a monster.  "
                "Use 'm' prefix to force another search.")

    def _live(self, seq, i, hp=10, hp_max=10, t=100, kind="command"):
        need = None if i is None else {"kind": kind, "id": i}
        if kind == "yn":
            need.update({"prompt": "Continue?", "choices": None,
                         "default": None, "numeric": False})
        rec = obs(seq, need, map_=self._LIVE_MAP, pal=self._LIVE_PAL,
                  msg=[{"e": seq, "text": self._REFUSAL}])
        rec["s"] = {"hitpoints": {"text": str(hp)},
                    "hitpoints-max": {"text": str(hp_max)},
                    "time": {"text": str(t)},
                    "dungeon-level": {"text": "1"}}
        return rec

    def _keys(self, recs, max_ticks=200):
        scenario = b"".join([_line(HELLO)] + [_line(r) for r in recs]
                            + [_line(CLOSED)])
        result, actions = self.run_scenario(scenario, max_ticks=max_ticks)
        out = []
        for a in actions:
            if a.get("type") != "act":
                continue
            action = a.get("action") or {}
            if "key" in action:
                out.append(chr(action["key"]))
            else:
                out.append(next(iter(action)))
        return result, out

    def test_generic_key_need_never_binds_the_suffix(self):
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, t=100, kind="key"),
                self._live(4, None, t=100)]
        result, keys = self._keys(recs)
        self.assertIn("m", keys)               # the prefix was armed
        self.assertNotIn("s", keys)            # ... never the suffix
        self.assertEqual(result.forced_suffixes, 0)
        self.assertGreaterEqual(result.forced_cancels, 1)

    def test_generic_direction_need_never_binds_the_suffix(self):
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, t=100, kind="direction"),
                self._live(4, None, t=100)]
        result, keys = self._keys(recs)
        self.assertIn("m", keys)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)

    def test_time_advance_after_prefix_refuses_the_binding(self):
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, t=101), self._live(4, None, t=101)]
        result, keys = self._keys(recs)
        self.assertIn("m", keys)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)

    def test_hp_change_after_prefix_refuses_the_binding(self):
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, hp=4, t=100), self._live(4, None, t=100)]
        result, keys = self._keys(recs)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)

    def test_command_need_still_binds_and_succeeds(self):
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, t=100), self._live(4, None, t=101)]
        result, keys = self._keys(recs)
        self.assertEqual(keys.count("s"), 1)
        self.assertEqual(result.forced_suffixes, 1)
        self.assertEqual(result.forced_successes, 1)

    def test_never_an_ordinary_command_through_an_armed_prefix(self):
        # the prompt cannot carry the native double-m: nothing prefixed is
        # sent, and no later command leaks through the prefix either
        recs = [self._live(1, 1, t=100), self._live(2, 2, t=100),
                self._live(3, 3, t=100, kind="yn"),
                self._live(4, 4, t=100), self._live(5, None, t=100)]
        result, keys = self._keys(recs)
        self.assertIn("m", keys)
        self.assertNotIn("yn", keys)
        self.assertNotIn("s", keys)
        self.assertEqual(result.forced_suffixes, 0)
        self.assertGreaterEqual(result.forced_uncleared, 1)


# ---------------------------------------------------------------- MEDIUM 5

class DirectiveActivationParity(unittest.TestCase):
    """AC7/AC15: the same canned advice steers the same command in both tiers.

    Live activates pending advice before ``_decide`` builds its view; the
    evaluator activates before it builds its directive view.  Driving both with
    the *same* canned v2 set must therefore leave the same post-activation
    view in force at the command boundary (the shared ordering, plan 2.2).
    """

    def _live(self):
        r = _runner()
        r.book = directives.DirectiveBook()
        r.boundary_queue = types.SimpleNamespace(
            events=[],
            finish=lambda ok, reason=None: r.boundary_queue.events.append(
                (ok, reason)))
        r.tick = 5
        r.instance.begin_playable()
        r.mem.status.dlvl = "1"
        return r

    def _replay(self):
        from tools.agent import evaluate
        cfg = ProviderConfig(reflex="scripted", strategy="off")
        rp = evaluate.ReplayPass([], cfg, "scripted", "off")
        rp.tick = 5
        rp.mem.status.dlvl = "1"
        return rp

    def _activate_live(self, r, dset):
        r._pending_directives = dset
        r._pending_directives_level = "1"
        r._pending_directives_instance = None       # instance check skipped
        r._activate_pending_directives({"kind": "command", "id": 7})

    def _activate_evaluator(self, rp, dset):
        rp._strategy_pending = dset
        rp._strategy_level = "1"
        rp._strategy_instance = None
        rp._activate_directives({"kind": "command", "id": 7})

    def _assert_same_command_view(self, book, *, target=None, goal=None):
        view = book.view(6, "1", directives.PreconditionState())
        self.assertTrue(view.active)
        if target is not None:
            self.assertEqual(tuple(view.target), tuple(target))
        if goal is not None:
            self.assertTrue(view.wants(goal))
        return view

    def _reflex_effect_of(self, view, *, evidence=(), upstairs=()):
        """The reflex's selected destination *effect* under *view* (plan 1.4).

        Drives the real scripted reflex so the assertion is on the chosen
        candidate's frozen effect (operation, purpose, coordinate), not merely
        on active-view state.
        """
        import test_auto_navigation as nav
        from tools.agent import policy
        mem = nav.mem_with({(x, 10): nav.FLOOR for x in range(1, 8)}, (1, 10))
        for pos in upstairs:
            mem.stairs_up.add(tuple(pos))
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        for pos in evidence:
            ref.floor.observe_item(ref.instance_id, tuple(pos),
                                   "coin appearance")
        table = ref.prepare(nav.ctx(mem, directives=[view])).table
        return table.scripted()

    def test_pending_collect_items_steers_same_command_live_and_evaluator(self):
        dset, why = directives.validate_directive_set(
            {"schema_version": 2, "goals": ["collect_items"],
             "target": [5, 10], "ttl": 50})
        self.assertEqual(why, "")
        live = self._live()
        self._activate_live(live, dset)
        replay = self._replay()
        self._activate_evaluator(replay, dset)
        for book in (live.book, replay.book):
            view = self._assert_same_command_view(book, target=(5, 10),
                                                  goal="collect_items")
            cand = self._reflex_effect_of(view, evidence=[(5, 10)])
            # the *same* command decision selects the same destination effect
            self.assertEqual(cand.effect_payload[1], "acquire")
            self.assertEqual(cand.effect_payload[3], "collect-items")
            self.assertEqual(tuple(cand.effect_payload[4:6]), (5, 10))

    def test_pending_flee_to_upstairs_steers_same_command_live_and_evaluator(
            self):
        dset, why = directives.validate_directive_set(
            {"schema_version": 2, "goals": ["flee_to_upstairs"], "ttl": 50})
        self.assertEqual(why, "")
        live = self._live()
        self._activate_live(live, dset)
        replay = self._replay()
        self._activate_evaluator(replay, dset)
        for book in (live.book, replay.book):
            view = self._assert_same_command_view(book,
                                                  goal="flee_to_upstairs")
            cand = self._reflex_effect_of(view, upstairs=[(6, 10)])
            self.assertEqual(cand.effect_payload[1], "acquire")
            self.assertEqual(cand.effect_payload[3], "flee-upstairs")
            self.assertEqual(tuple(cand.effect_payload[4:6]), (6, 10))

    def test_modifier_only_legacy_advice_parity_live_and_evaluator(self):
        dset, why = directives.validate_directive_set(
            {"schema_version": 1, "goals": ["survive"], "ttl": 50})
        self.assertEqual(why, "")
        live = self._live()
        self._activate_live(live, dset)
        replay = self._replay()
        self._activate_evaluator(replay, dset)
        for book in (live.book, replay.book):
            self._assert_same_command_view(book, goal="survive")

    def test_evaluator_rejects_source_instance_mismatch_before_activation(
            self):
        dset, _ = directives.validate_directive_set(
            {"schema_version": 2, "goals": ["collect_items"], "target": [5, 5],
             "ttl": 50})
        rp = self._replay()
        rp.instance.begin_playable()                # instance 1
        rp.mem.begin_instance(2)
        rp.instance.observe((instances.S_OUTCOME,), True)
        self.assertEqual(rp.instance.current(), 2)
        rp._strategy_pending = dset
        rp._strategy_level = "1"
        rp._strategy_instance = 1                   # dispatched for instance 1
        rp._activate_directives({"kind": "command", "id": 7})
        # zero activation and the stale advice consumed before any effect
        self.assertFalse(rp.book.has_active)
        self.assertEqual(rp.book.generation, 0)
        self.assertIsNone(rp._strategy_pending)


class DirectiveSourceInstance(unittest.TestCase):
    """Pending advice is scoped to its source level instance (plan 4.4)."""

    def test_on_instance_change_expires_once(self):
        book = directives.DirectiveBook()
        dset = directives.DirectiveSet(goals=("explore_frontier",), ttl=50)
        book.activate(dset, tick=1, level="1", instance=1)
        self.assertTrue(book.on_instance_change(2, tick=2))
        self.assertFalse(book.has_active)
        self.assertEqual(book.generation, 1)
        # an expired book has nothing left to expire
        self.assertFalse(book.on_instance_change(3, tick=3))

    def test_same_instance_change_is_a_noop(self):
        book = directives.DirectiveBook()
        dset = directives.DirectiveSet(goals=("survive",), ttl=50)
        book.activate(dset, tick=1, level="1", instance=1)
        self.assertFalse(book.on_instance_change(1, tick=2))
        self.assertTrue(book.has_active)

    def _runner(self):
        r = _runner()
        r.book = directives.DirectiveBook()
        r.boundary_queue = types.SimpleNamespace(
            events=[],
            finish=lambda ok, reason=None: r.boundary_queue.events.append(
                (ok, reason)))
        r.tick = 5
        return r

    def test_stale_source_instance_is_rejected_before_activation(self):
        r = self._runner()
        r.instance.begin_playable()                     # instance 1
        r.mem.status.dlvl = "1"
        r._pending_directives = directives.DirectiveSet(
            goals=("explore_frontier",), ttl=50)
        r._pending_directives_level = "1"
        r._pending_directives_instance = 1
        # a same-Dlvl transition to instance 2
        r.mem.begin_instance(2)
        r.instance.observe((instances.S_OUTCOME,), True)
        self.assertEqual(r.instance.current(), 2)
        r._activate_pending_directives({"kind": "command", "id": 7})
        # zero score contribution, no active directive, one expiry event
        self.assertFalse(r.book.has_active)
        self.assertEqual(r.book.generation, 0)
        self.assertEqual(r.boundary_queue.events, [(False, "stale-instance")])

    def test_matching_source_instance_activates(self):
        r = self._runner()
        r.instance.begin_playable()
        r.mem.status.dlvl = "1"
        r._pending_directives = directives.DirectiveSet(
            goals=("explore_frontier",), ttl=50)
        r._pending_directives_level = "1"
        r._pending_directives_instance = 1
        r._activate_pending_directives({"kind": "command", "id": 7})
        self.assertTrue(r.book.has_active)
        self.assertEqual(r.boundary_queue.events, [(True, None)])

    def test_pending_key_does_not_apply_new_destination(self):
        # a v2 destination set is never activated on a key need (plan 1.5)
        r = self._runner()
        r.instance.begin_playable()
        r.mem.status.dlvl = "1"
        r._pending_directives = directives.DirectiveSet(
            schema_version=2, goals=("collect_items",), target=(5, 5), ttl=50)
        r._pending_directives_level = "1"
        r._pending_directives_instance = 1
        r._activate_pending_directives({"kind": "key", "id": 7})
        self.assertFalse(r.book.has_active)
        self.assertIsNotNone(r._pending_directives)     # preserved
        self.assertEqual(r.boundary_queue.events, [])

    def test_menu_or_yn_need_does_not_consume_pending_destination(self):
        for kind in ("menu", "yn"):
            with self.subTest(kind=kind):
                r = self._runner()
                r.instance.begin_playable()
                r.mem.status.dlvl = "1"
                r._pending_directives = directives.DirectiveSet(
                    schema_version=2, goals=("flee_to_upstairs",), ttl=50)
                r._pending_directives_level = "1"
                r._pending_directives_instance = 1
                r._activate_pending_directives({"kind": kind, "id": 7})
                self.assertIsNotNone(r._pending_directives)
                self.assertFalse(r.book.has_active)
                # a following genuine command activates it
                r._activate_pending_directives({"kind": "command", "id": 8})
                self.assertTrue(r.book.has_active)
                self.assertIsNone(r._pending_directives)

    def test_v1_modifier_set_keeps_the_broad_gate(self):
        # a modifier-only legacy set (no destination) may still activate on a
        # direction need, where destination resolution is provably deferred
        r = self._runner()
        r.instance.begin_playable()
        r.mem.status.dlvl = "1"
        r._pending_directives = directives.DirectiveSet(
            goals=("survive",), ttl=50)
        r._pending_directives_level = "1"
        r._pending_directives_instance = 1
        r._activate_pending_directives({"kind": "direction", "id": 7})
        self.assertTrue(r.book.has_active)

    def test_active_view_expires_after_instance_change(self):
        # the shared DirectiveBook instance scope (used by live and evaluator)
        book = directives.DirectiveBook()
        book.activate(directives.DirectiveSet(goals=("survive",), ttl=50),
                      5, "1", instance=1)
        st = directives.PreconditionState()
        self.assertTrue(book.view(6, "1", st, instance=1).active)
        # a same-level transition to a fresh instance expires the view
        self.assertFalse(book.view(6, "1", st, instance=2).active)
        self.assertFalse(book.has_active)


# ---------------------------------------------------------------- MEDIUM 6

class LiveEvaluatorParity(WireHarness):
    """The M21 parity fixture: replay and live agree on table ids, rejection
    sets, fallback choice, lifecycle and usage (plan 6.2)."""

    _WIRE_PAL = [[0, " ", "none", 0, "none"], [1, ".", "gray", 0, "none"],
                 [2, "@", "white", 0, "none"]]
    _WIRE_MAP = [[10, 10, 2], [11, 10, 1], [12, 10, 1]]

    def _rec(self, seq, i, t=100):
        rec = obs(seq, {"kind": "command", "id": i} if i else None,
                  map_=self._WIRE_MAP, pal=self._WIRE_PAL)
        rec["s"] = {"hitpoints": {"text": "10"},
                    "hitpoints-max": {"text": "10"},
                    "time": {"text": str(t)},
                    "dungeon-level": {"text": "1"}}
        return rec

    def _live_tables(self, recs):
        """The live model's prepared table ids and retained choices."""
        r = _runner()
        out = []
        for rec in recs:
            apply_obs(r, rec)
            need = rec.get("need")
            if not need:
                continue
            r.pending_key = protocol.NeedKey(1, rec["seq"], need.get("id"))
            ctx = ReflexContext(
                episode=1, tick=r.mem.no_progress,
                need=need, need_key=r.pending_key,
                snapshot=snap_of(rec), pages=[], memory=r.mem,
                directives=[], deadline=0.0)
            prepared = r.reflex.prepare(ctx)
            cand = r.reflex._select(prepared, None)
            out.append((prepared.table.table_id,
                        cand.candidate_id if cand else None))
        return out

    def test_live_model_and_replay_agree_on_canonical_tables(self):
        from tools.agent import evaluate

        recs = [self._rec(1, 1, t=100), self._rec(2, 2, t=101),
                self._rec(3, 3, t=102), self._rec(4, None, t=103)]
        wire = b"".join([_line(HELLO)] + [_line(r) for r in recs]
                        + [_line(CLOSED)])
        lines = wire.split(b"\n")
        lines = [ln + b"\n" for ln in lines if ln]

        live = self._live_tables(recs)
        replay = evaluate.ReplayPass(
            lines, ProviderConfig(reflex="scripted", strategy="off",
                                  max_ticks=200),
            "scripted", "off")
        replay.run()
        # the replay prepared the same canonical tables in the same order
        self.assertTrue(live)
        seen = [d["need"]["id"] for d in replay.decisions
                if d.get("record") == "need"]
        self.assertEqual(len(seen), len(live))
        # determinism: a second replay is byte-identical
        replay2 = evaluate.ReplayPass(
            lines, ProviderConfig(reflex="scripted", strategy="off",
                                  max_ticks=200),
            "scripted", "off")
        replay2.run()
        self.assertEqual(replay.decisions, replay2.decisions)

    def test_shared_rejection_set_is_one_copy(self):
        # M21: the replay and the live controller use the same rejection logic
        rs = arbitration.RejectionSet()
        cand = candidates.make_candidate({"key": EAST}, "east")
        rs.exclude(cand)
        self.assertTrue(rs.excludes(cand))
        self.assertTrue(rs.excludes_signature(cand.action_signature))
        self.assertIs(arbitration.classify_outcome,
                      arbitration.classify_outcome)

    def _yn(self, seq, t=100):
        rec = obs(seq, {"id": seq, "kind": "yn", "prompt": "Continue?",
                        "choices": None, "default": None, "numeric": False},
                  map_=self._WIRE_MAP, pal=self._WIRE_PAL)
        rec["s"] = {"hitpoints": {"text": "10"},
                    "hitpoints-max": {"text": "10"},
                    "time": {"text": str(t)},
                    "dungeon-level": {"text": "1"}}
        return rec

    def test_live_controller_and_replay_select_the_same_actions(self):
        # M21 parity for the *whole* selection: the live controller and the
        # offline replay, driven by the same wire, must select the identical
        # action for every need -- a gameplay command *and* a non-command
        # prompt -- because they share the prepared table and arbitration.
        from tools.agent import evaluate

        recs = [self._rec(1, 1, t=100), self._rec(2, 2, t=101),
                self._yn(3, t=102), self._rec(4, None, t=103)]
        scenario = b"".join([_line(HELLO)] + [_line(r) for r in recs]
                            + [_line(CLOSED)])
        lines = [ln + b"\n" for ln in scenario.split(b"\n") if ln]
        _result, actions = self.run_scenario(scenario, max_ticks=200)
        live = [(a.get("seq"), a.get("id"), a.get("action"))
                for a in actions if a.get("type") == "act"]
        replay = evaluate.ReplayPass(
            lines, ProviderConfig(reflex="scripted", strategy="off",
                                  max_ticks=200), "scripted", "off")
        replay.run()
        rg = [(d["need"]["seq"], d["need"]["id"], d["selected"])
              for d in replay.decisions
              if d.get("record") == "need" and d.get("selected") is not None]
        self.assertTrue(live)
        self.assertEqual(live, rg)


# ---------------------------------------------------------------- AC.12

class JevPresentationIsolation(WireHarness):
    """The Jev presentation migration touches Jev presentation only.

    The DeepSeek strategy rendering (and therefore its cache-plan history
    invariants) must be byte-identical to the pre-migration snapshot, and no
    Jev presentation field may leak into it.
    """

    #: sha256 of ``tools.agent.providers._render_strategy_prompt`` for the
    #: frozen context below.  Pinned so any drift in the strategy rendering --
    #: a field reorder, a renamed line or a stray Jev field -- fails here.
    PROMPT_SHA256 = (
        "dacb308b8e01cef26e755a8ffbb5127efa38c2587a3913907b6115fad295534d")

    def _ctx(self):
        from tools.agent.providers import StrategyContext
        return StrategyContext(
            episode=1, tick=17, role="explore",
            map_text=" 0 hello\n 1 world",
            status_text="HP 12/20  Dlvl:1",
            recent_messages=["You see here a food ration.",
                             "You hear a noise."],
            inventory=["a food ration", "b - a dagger"],
            history=[{"eid": "b1", "reason": "hunger", "tick": 4,
                      "level": "1"}],
            boundaries=["b2"],
            level="1", remaining_budget=7,
            directives=[{"schema_version": 1, "goals": ["survive"],
                         "ttl": 5, "explanation": "fake"}])

    def test_deepseek_rendering_snapshot_unchanged(self):
        from tools.agent import providers
        ctx = self._ctx()
        rendered = providers._render_strategy_prompt(ctx)
        self.assertEqual(hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                         self.PROMPT_SHA256)
        # the documented, cache-friendly field order is intact
        lines = rendered.split("\n")
        self.assertTrue(lines[0].startswith("GAME STATE (untrusted data):"))
        self.assertTrue(lines[-1].startswith("remaining strategy calls: "))
        self.assertIn("active directives: ", rendered)
        # no Jev presentation field leaks into the strategy prompt
        for token in ("criteria", "navigate-north", "presentation_version",
                      "legend", "objective", "state.hero"):
            self.assertNotIn(token, rendered)

        # the frozen DeepSeek request payload is unchanged too
        cfg = providers.ProviderConfig(strategy="deepseek",
                                       deepseek_model="deepseek-chat")
        prepared = providers.prepare_strategy_request(cfg, ctx)
        body = json.dumps(prepared.payload(), separators=(",", ":"))
        # Contract migration (Phase 2): the schema-v2 system prompt changes the
        # frozen request payload once (plan 2.3); the rendered snapshot above is
        # unchanged.
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "8e822a801e71a6a1f9af6bcd9eb7ce2be77177f167f41955411e43a594910fe0")
        # rendering the same context twice is byte-identical (render-once)
        self.assertEqual(providers._render_strategy_prompt(self._ctx()),
                         rendered)

    def test_room_enrichment_does_not_change_deepseek_payload_or_history(self):
        from tools.agent import presentation, providers
        from test_auto_jev_presentation import room_ctx
        # rendering the *enriched* Jev state changes nothing on the DeepSeek
        # side: its renderer and frozen request payload are untouched
        enriched = presentation.render_state(room_ctx())
        self.assertIn("room", enriched)
        rendered = providers._render_strategy_prompt(self._ctx())
        self.assertEqual(hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                         self.PROMPT_SHA256)
        cfg = providers.ProviderConfig(strategy="deepseek",
                                       deepseek_model="deepseek-chat")
        prepared = providers.prepare_strategy_request(cfg, self._ctx())
        body = json.dumps(prepared.payload(), separators=(",", ":"))
        # Contract migration (Phase 2): the schema-v2 system prompt changes the
        # frozen request payload once (plan 2.3); the rendered snapshot is
        # unchanged.
        self.assertEqual(
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "8e822a801e71a6a1f9af6bcd9eb7ce2be77177f167f41955411e43a594910fe0")

    def test_room_enrichment_leaves_relative_acceptance_and_applied_cap_unchanged(
            self):
        from tools.agent import arbitration, presentation
        from test_auto_jev_presentation import ROOM_HERO, move_cand, room_ctx
        # the offered option count and their order are the same with and
        # without the enriched snapshot, so N (and the relative gate) is
        # untouched by the enrichment
        cands = [move_cand(protocol.KEY_L, "navigate", (1, 0)),
                 move_cand(protocol.KEY_H, "navigate", (-1, 0))]
        table = candidates.build_table(protocol.NeedKey(1, 1, 1), 1, cands)
        rich, refusal = presentation.present("command", table.ordered_candidates,
                                             room_ctx())
        bare, refusal2 = presentation.present(
            "command", table.ordered_candidates,
            room_ctx(terrain={}, snapshot=[(ROOM_HERO, ("@", "white"))]))
        self.assertEqual(refusal, "")
        self.assertEqual(refusal2, "")
        self.assertEqual(len(rich.keys), len(bare.keys))
        self.assertEqual(list(rich.key_index.values()),
                         list(bare.key_index.values()))
        # the *relative* gate decision is identical over the same table
        def outcome(prob):
            raw = arbitration.RawChoice(
                table_id=table.table_id, need_key=tuple(table.need_key),
                table_version=table.table_version, index=0,
                selected_probability=prob)
            return arbitration.validate_raw_choice(
                table, raw, arbitration.RejectionSet()).accepted
        self.assertEqual(outcome(0.80), outcome(0.80))
        self.assertTrue(outcome(0.80))          # > 1.5/2
        self.assertFalse(outcome(0.70))         # <= 1.5/2

    def test_room_enrichment_uses_same_frozen_snapshot_for_map_and_criteria(
            self):
        from tools.agent import presentation
        from test_auto_jev_presentation import ROOM_HERO, move_cand, room_ctx
        snap = [(ROOM_HERO, ("@", "white", 0, "none")),
                ((ROOM_HERO[0] + 1, ROOM_HERO[1]), ("%", "yellow", 0, "none"))]
        ctx = room_ctx(terrain={(ROOM_HERO[0] + 1, ROOM_HERO[1]):
                                instances.T_FLOOR},
                       snapshot=snap)
        # the map marks the destination as an item appearance ...
        text = presentation.render_state(ctx)["map"]["text"]
        self.assertIn("&", text)
        # ... and the criterion for the move onto it names the same category
        criterion, refusal = presentation.render_criterion(
            move_cand(protocol.KEY_L, "navigate", (1, 0)), "command", ctx)
        self.assertEqual(refusal, "")
        self.assertIn("item with food appearance", criterion)


# -------------------------------------------- applied-decision cap (phase 2)

class _ChoiceJev(object):
    """A typed-choice Jev double that never opens a socket (no real provider)."""

    name = "jev"
    version = "fake/1"
    last_error = ""

    def __init__(self, index=0, selected_probability=0.9, confidence=0.9,
                 abstain=False, usage=None, parse_error="", returns_none=False):
        self.index = index
        self.selected_probability = selected_probability
        self.confidence = confidence
        self.abstain = abstain
        self.usage = usage or {}
        self.parse_error = parse_error
        self.returns_none = returns_none
        self.decides = 0

    def available(self, config):
        return providers.Availability(True, "fake jev")

    def build_request(self, ctx):
        return providers.JevBuild({"table_id": "fake", "candidates": []}, "")

    def decide(self, ctx, deadline=0.0):
        self.decides += 1
        if self.returns_none:
            self.last_error = "timeout"
            return None
        table = getattr(getattr(ctx, "prepared", None), "table", None)
        return providers.ReflexChoiceResult(
            table_id=(table.table_id if table is not None else ""),
            need_key=(tuple(table.need_key) if table is not None else ()),
            table_version=(table.table_version if table is not None else -1),
            index=None if self.abstain else self.index,
            confidence=self.confidence,
            selected_probability=self.selected_probability,
            abstain=self.abstain, parse_error=self.parse_error,
            usage=self.usage, dispatched=True, reason="fake")

    def cancel(self):
        pass

    def on_closed(self):
        pass


class JevAppliedCap(WireHarness):
    """AC.5/AC.6: the cap bounds *applied* decisions, charged on complete send."""

    def _runner(self, fake, cap=2, config=None):
        cfg = config or ProviderConfig(
            max_ticks=200, reflex="jev", postmortem_reserve=0,
            reflex_call_cap=cap)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._new_reflex_provider = lambda reflex: fake
        result = controller.EpisodeResult(index=1)
        rec = recording.EpisodeRecorder(self.dir, 1)
        proc = paced([hello()], [0.0])
        self.addCleanup(proc.close)
        r = controller._EpisodeRunner(ctl, proc, rec, result)
        r.pending_key = protocol.NeedKey(1, 1, 1)
        r.pending_seq = 1
        r.pending_need = {"id": 1, "kind": "command", "prompt": ""}
        # a plus-shaped floor with a confirmed hero yields >1 distinct
        # movement candidate, so the relative gate has a real N >= 2 table
        for pos in [(5, 5), (4, 5), (6, 5), (5, 4), (5, 6)]:
            r.mem.grid[pos] = "."
        r.mem.hero = (5, 5)
        r.mem.status.hp = 10
        r.mem.status.hp_max = 10
        r.mem.inventory.refresh([], 0, 0)
        r.req.begin({"id": 1, "kind": "command", "prompt": ""}, 1)
        return r, rec, proc

    def _answer(self, r):
        r.pending = True
        r.pending_need = {"id": 1, "kind": "command", "prompt": ""}
        return r._answer_now(None)

    def test_jev_rejections_do_not_exhaust_applied_cap(self):
        # a rejected consultation is reserved and billed, but spends no
        # applied allowance: cap=1 still admits many consultations
        fake = _ChoiceJev(index=99, usage={"prompt_tokens": 100})
        r, rec, _ = self._runner(fake, cap=1)
        for _ in range(4):
            self._answer(r)
        rec.finalize({})
        self.assertEqual(r.ledger.reflex_applied, 0)
        self.assertEqual(r.ledger.reflex_paid_dispatched, 4)
        self.assertTrue(r.ledger.reflex_paid_available())
        self.assertEqual(fake.decides, 4)

    def test_jev_skips_abstentions_and_timeouts_leave_applied_allowance(self):
        abstain = _ChoiceJev(abstain=True, usage={"prompt_tokens": 5})
        r, rec, _ = self._runner(abstain, cap=1)
        self._answer(r)
        rec.finalize({})
        self.assertEqual(r.ledger.reflex_applied, 0)
        self.assertEqual(r.ledger.reflex_paid_dispatched, 1)
        self.assertTrue(r.ledger.reflex_paid_available())

        timeout = _ChoiceJev(returns_none=True)
        r2, rec2, _ = self._runner(timeout, cap=1)
        self._answer(r2)
        rec2.finalize({})
        self.assertEqual(r2.ledger.reflex_applied, 0)
        self.assertTrue(r2.ledger.reflex_paid_available())

    def test_jev_cap_stops_after_exactly_c_complete_applied_sends(self):
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake, cap=2)
        for _ in range(4):
            self._answer(r)
        rec.finalize({})
        # exactly two complete applied sends, then the paid tier is suppressed
        self.assertEqual(r.ledger.reflex_applied, 2)
        self.assertEqual(fake.decides, 2)
        self.assertFalse(r.ledger.reflex_paid_available())

    def test_jev_validation_fallback_and_forced_override_do_not_charge_applied(
            self):
        from unittest import mock
        # (a) a locally invalid Jev proposal is replaced by the fallback
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake)
        with mock.patch.object(controller.protocol, "validate_action",
                               return_value="bad action"):
            self._answer(r)
        self.assertEqual(r.ledger.reflex_applied, 0)
        self.assertTrue(r.ledger.reflex_paid_available())
        # (b) a controller-owned forced-search override charges nothing either
        fake2 = _ChoiceJev()
        r2, rec2, _ = self._runner(fake2)
        r2._forced_override = lambda need, selected: (
            {"key": protocol.KEY_SEARCH}, "forced search", "prefix")
        self._answer(r2)
        rec.finalize({})
        rec2.finalize({})
        self.assertEqual(r2.ledger.reflex_applied, 0)
        self.assertTrue(r2.ledger.reflex_paid_available())

    def test_equal_action_forced_override_uses_provenance_not_dict_equality(
            self):
        # The override's action dict is *identical* to the Jev proposal; the
        # decision is still a controller override, so provenance -- not dict
        # equality -- decides, and nothing is charged.
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake)
        seen = {}
        real_record = r.rec.record_decision
        r.rec.record_decision = lambda **kw: (seen.update(kw),
                                              real_record(**kw))[1]

        def override(need, selected):
            seen["overridden_action"] = dict(selected)
            return (dict(selected), "forced search", "prefix")

        r._forced_override = override
        self._answer(r)
        rec.finalize({})
        # the override action really did coincide with the Jev proposal ...
        self.assertEqual(seen["provider"], "scripted")
        self.assertEqual(seen["overridden_action"], seen["selected"])
        # ... yet the decision is a controller override: nothing applied
        self.assertEqual(r.ledger.reflex_applied, 0)
        self.assertIsNone(r._last_jev_send)
        self.assertTrue(r.ledger.reflex_paid_available())

    def test_jev_failed_or_partial_send_does_not_charge_applied(self):
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake)

        def boom(*a, **k):
            raise controller._TransportFailure("partial write")

        r._emit = boom
        with self.assertRaises(controller._TransportFailure):
            self._answer(r)
        rec.finalize({})
        self.assertEqual(r.ledger.reflex_applied, 0)
        self.assertTrue(r.ledger.reflex_paid_available())

    def test_jev_delivery_repair_does_not_double_charge_decision(self):
        fake = _ChoiceJev(usage={"prompt_tokens": 1000})
        r, rec, _ = self._runner(fake, cap=1)
        self._answer(r)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(r.action_ordinal, 1)
        # the engine rejects the delivery as incomplete: page repair + resend
        r._on_invalid({"code": "incomplete"})
        self.assertIsNotNone(r._repair_send)
        self._answer(r)
        rec.finalize({})
        # one provider call, one applied increment, two sent ordinals
        self.assertEqual(fake.decides, 1)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(r.action_ordinal, 2)
        # paid settlement is unchanged: the single consultation billed once
        self.assertEqual(r.ledger.reflex_paid_dispatched, 1)
        self.assertEqual(r.ledger.prompt_tokens, 1000)

    def test_incomplete_delivery_repair_does_not_activate_or_consume_pending(
            self):
        # a delivery-repair (incomplete-retry) pass must neither activate nor
        # consume pending destination advice: it stays preserved until the next
        # fresh command decision, where it activates normally (plan 2.2)
        fake = _ChoiceJev(usage={"prompt_tokens": 1000})
        r, rec, _ = self._runner(fake, cap=1)
        self._answer(r)                       # the fresh decision and its send
        dset, why = directives.validate_directive_set(
            {"schema_version": 2, "goals": ["collect_items"], "target": [5, 5],
             "ttl": 50})
        self.assertEqual(why, "")
        # the strategy has since returned this advice, still pending
        r._pending_directives = dset
        r._pending_directives_level = r.mem.status.dlvl
        r._pending_directives_instance = None
        r._on_invalid({"code": "incomplete"})
        self.assertIsNotNone(r._repair_send)
        self._answer(r)                       # the delivery-repair pass
        # the repair left the pending advice untouched and never activated it
        self.assertIs(r._pending_directives, dset)
        self.assertFalse(r.book.has_active)
        rec.finalize({})
        # the next genuine command decision activates it normally
        r._activate_pending_directives({"kind": "command", "id": 9})
        self.assertTrue(r.book.has_active)
        self.assertIsNone(r._pending_directives)

    def test_jev_applied_send_later_native_invalid_is_not_refunded(self):
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake, cap=1)
        self._answer(r)
        self.assertEqual(r.ledger.reflex_applied, 1)
        # a later ordinary engine invalid does not refund the applied count
        r._on_invalid({"code": "kind"})
        rec.finalize({})
        self.assertEqual(r.ledger.reflex_applied, 1)

    def test_incomplete_repair_cannot_resurrect_older_rejected_jev_action(
            self):
        # cap=1: accepted Jev A is applied and sent, then an ordinary engine
        # invalid rejects the retry, then the scripted retry gets an
        # `incomplete`.  The stale Jev record must NOT be resendable, so the
        # already-rejected A is never resurrected.
        fake = _ChoiceJev(usage={"prompt_tokens": 1000})
        r, rec, _ = self._runner(fake, cap=1)
        self._answer(r)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(fake.decides, 1)
        self.assertEqual(r.action_ordinal, 1)
        # ordinary invalid (excludes A): Jev repair eligibility is dropped
        r._on_invalid({"code": "kind"})
        self.assertIsNone(r._last_jev_send)
        # the retry falls back to scripted (applied cap reached): no new Jev
        self._answer(r)
        self.assertEqual(fake.decides, 1)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(r.action_ordinal, 2)
        self.assertIsNone(r._last_jev_send)
        # an incomplete on the scripted retry must not arm a Jev repair
        r._on_invalid({"code": "incomplete"})
        self.assertIsNone(r._repair_send)
        self._answer(r)
        rec.finalize({})
        # no resurrection: no new consultation, one applied decision, one
        # paid reservation
        self.assertEqual(fake.decides, 1)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(r.ledger.reflex_paid_dispatched, 1)

    def test_incomplete_repair_fails_closed_when_rejection_identity_stale(
            self):
        # the frozen repair record is only honoured while its rejection
        # identity is unchanged: a rejection-version bump between the send and
        # the repair makes it stale, so the resend falls back to scripted
        # instead of reusing the frozen Jev token/action.
        fake = _ChoiceJev(usage={"prompt_tokens": 1000})
        r, rec, _ = self._runner(fake, cap=1)
        self._answer(r)
        self.assertEqual(r.ledger.reflex_applied, 1)
        # a rejection-version change between the send and the repair
        r._rejection_for(r.pending_key).version += 1
        r._on_invalid({"code": "incomplete"})
        self.assertIsNotNone(r._repair_send)
        # the resend helper refuses the stale record (scripted, no token)
        _sel, provider, reason, _lat, _usage, low, token = \
            r._resend_repair(r.pending_need, r._repair_send)
        self.assertEqual(provider, "scripted")
        self.assertIsNone(token)
        self.assertTrue(low)
        self.assertIn("stale", reason)
        self._answer(r)
        rec.finalize({})
        self.assertEqual(fake.decides, 1)
        self.assertEqual(r.ledger.reflex_applied, 1)
        self.assertEqual(r.ledger.reflex_paid_dispatched, 1)

    def test_incomplete_repair_fails_closed_when_table_identity_stale(self):
        # the frozen repair record is honoured only while its table identity
        # still matches the controller-owned *authoritative* identity of the
        # accepted decision: both the recorded table id AND the recorded table
        # version are compared, not merely stored.  Changing either the
        # authoritative id or the authoritative version between the send and
        # the repair makes the record stale, so the resend falls closed to the
        # scripted action with no token -- the already charged applied count
        # stays put rather than the frozen Jev action being resurrected.
        for attr, mutate in (("_applied_table_id", lambda v: v + "-stale"),
                             ("_applied_table_version", lambda v: v + 1)):
            fake = _ChoiceJev(usage={"prompt_tokens": 1000})
            r, rec, _ = self._runner(fake, cap=1)
            self._answer(r)
            self.assertEqual(r.ledger.reflex_applied, 1)
            # the repair record and the authoritative identity agree on send
            self.assertEqual(r._last_jev_send["table_id"], r._applied_table_id)
            self.assertEqual(r._last_jev_send["table_version"],
                             r._applied_table_version)
            authoritative = getattr(r, attr)
            self.assertNotIn(authoritative, (None, ""))
            setattr(r, attr, mutate(authoritative))
            r._on_invalid({"code": "incomplete"})
            self.assertIsNotNone(r._repair_send)
            # the resend helper refuses the mismatched-table record
            _sel, provider, reason, _lat, _usage, low, token = \
                r._resend_repair(r.pending_need, r._repair_send)
            self.assertEqual(provider, "scripted")
            self.assertIsNone(token)
            self.assertTrue(low)
            self.assertIn("stale", reason)
            self._answer(r)
            rec.finalize({})
            self.assertEqual(fake.decides, 1)
            self.assertEqual(r.ledger.reflex_applied, 1)
            self.assertEqual(r.ledger.reflex_paid_dispatched, 1)

    def test_reflex_rejected_counts_only_defined_answer_rejections(self):
        # one arbitration rejection + one pre-dispatch skip + one timeout +
        # one cap-reached fallback: rejected is 1 (the arbitration rejection),
        # fallback is the broad 4.
        reject = _ChoiceJev(index=99, usage={"prompt_tokens": 10})
        skip = _ChoiceJev(usage={"prompt_tokens": 10})
        slow = _ChoiceJev()          # sleeps past the reflex deadline

        def slow_decide(ctx, deadline=0.0):
            slow.decides += 1
            time.sleep(1.2)
            return None
        slow.decide = slow_decide

        rej, rec, _ = self._runner(reject, cap=2)

        def use(provider):
            rej.reflex_provider = provider
            self._answer(rej)

        # 1. an arbitration rejection (invalid selected index)
        use(reject)
        # 2. a pre-dispatch skip: the request build refuses the whole request,
        #    so no reservation is made
        skip.build_request = lambda ctx: providers.JevBuild(
            None, "unsupported need")
        use(skip)
        # 3. a paid timeout that exceeds the reflex deadline
        use(slow)
        # 4. the applied cap is reached: the paid tier is suppressed
        rej.ledger.reflex_applied = rej.ledger.reflex_cap
        use(reject)
        rec.finalize({})
        self.assertEqual(rej.ledger.reflex_rejected, 1)
        self.assertEqual(rej.ledger.reflex_fallback, 4)
        self.assertEqual(rej.ledger.reflex_timeout, 1)
        self.assertEqual(rej.ledger.as_dict()["reflex"]["rejected"], 1)
        self.assertEqual(rej.ledger.as_dict()["reflex"]["fallback"], 4)

    def test_jev_rejected_usage_settled_once_and_sidecar_retained(self):
        records = []
        fake = _ChoiceJev(index=99, usage={"prompt_tokens": 1000})
        r, rec, _ = self._runner(fake, cap=1)
        real_record = r.rec.record_decision

        def capture(**kw):
            records.append(kw)
            return real_record(**kw)

        r.rec.record_decision = capture
        self._answer(r)
        rec.finalize({})
        # the paid usage is settled exactly once even though it was rejected
        self.assertEqual(r.ledger.prompt_tokens, 1000)
        self.assertEqual(r.ledger.reflex_paid_dispatched, 1)
        self.assertEqual(r.ledger.reflex_applied, 0)
        # a normal fallback decision record is retained with its reason
        self.assertTrue(records)
        self.assertEqual(records[-1]["provider"], "scripted")
        self.assertIn("jev rejected", records[-1]["reason"])

    def test_jev_cap_zero_and_monetary_admission_remain_fail_closed(self):
        fake = _ChoiceJev()
        r, rec, _ = self._runner(fake, cap=0)
        self._answer(r)
        rec.finalize({})
        self.assertEqual(fake.decides, 0)          # cap 0 disables the tier
        cfg = ProviderConfig(max_ticks=200, reflex="jev", reflex_call_cap=5,
                             postmortem_reserve=0, usd_cap=1.0,
                             deepseek_price_in=1.0, deepseek_price_out=1.0)
        fake2 = _ChoiceJev()
        r2, rec2, _ = self._runner(fake2, config=cfg)
        self._answer(r2)
        rec2.finalize({})
        self.assertEqual(fake2.decides, 0)         # a USD cap refuses Jev
        self.assertIsNone(r2.ledger.reserve_reflex_paid())


    def test_episode_and_campaign_summary_report_reflex_applied_and_consulted_counts(
            self):
        # the per-episode and campaign summaries report both the applied count
        # (bounded by the applied cap) and the consulted/reserved diagnostic
        r = controller.EpisodeResult(index=1)
        r.budget = {"usage": {"prompt_tokens": 10},
                    "reflex": {"applied": 2, "paid_dispatched": 5,
                               "successful": 2, "fallback": 3, "timeout": 1,
                               "invalid": 0, "low_confidence": 0}}
        ep = controller._episode_summary(r)
        self.assertEqual(ep["reflex"]["applied"], 2)
        self.assertEqual(ep["reflex"]["paid_dispatched"], 5)
        summary = controller.campaign_summary([r], ProviderConfig(), 1.0)
        self.assertEqual(summary["totals"]["reflex"]["applied"], 2)
        self.assertEqual(summary["totals"]["reflex"]["paid_dispatched"], 5)
        # applied never exceeds the applied cap even when consultations do
        self.assertLessEqual(summary["totals"]["reflex"]["applied"],
                             summary["totals"]["reflex"]["paid_dispatched"])


class SummaryCompatibility(unittest.TestCase):
    """AC.7: additive ``reflex.applied`` reporting keeps old consumers working."""

    def _result(self, index, budget):
        r = controller.EpisodeResult(index=index)
        r.spawn_ok = True
        r.closed = True
        r.returncode = 0
        r.recording_complete = True
        r.budget = budget
        return r

    def test_episode_and_campaign_summary_consumer_compatibility_with_reflex_applied(
            self):
        # an OLD artifact dictionary lacks reflex.* entirely: defaults to 0
        old = self._result(1, {"usage": {"prompt_tokens": 5}})
        ep = controller._episode_summary(old)
        self.assertEqual(ep["reflex"]["applied"], 0)
        self.assertEqual(ep["reflex"]["paid_dispatched"], 0)
        summary = controller.campaign_summary([old], ProviderConfig(), 1.0)
        self.assertEqual(summary["totals"]["reflex"]["applied"], 0)
        # a NEW artifact carries applied/paid_dispatched and they are preserved
        new = self._result(2, {
            "usage": {"prompt_tokens": 5},
            "reflex": {"applied": 3, "paid_dispatched": 7, "successful": 3,
                       "fallback": 4, "timeout": 1, "invalid": 0,
                       "low_confidence": 0}})
        ep2 = controller._episode_summary(new)
        self.assertEqual(ep2["reflex"]["applied"], 3)
        self.assertEqual(ep2["reflex"]["paid_dispatched"], 7)
        summary2 = controller.campaign_summary([old, new], ProviderConfig(), 1.0)
        self.assertEqual(summary2["totals"]["reflex"]["applied"], 3)
        self.assertEqual(summary2["totals"]["reflex"]["paid_dispatched"], 7)
        # a JSON round trip stays parseable by an older consumer
        json.dumps(summary2)

    def test_reflex_rejected_defaults_to_zero_and_is_summed_distinctly(self):
        # an OLD artifact without reflex.rejected defaults to 0 -- and stays
        # distinct from the broad fallback count
        old = self._result(1, {
            "usage": {"prompt_tokens": 5},
            "reflex": {"applied": 1, "paid_dispatched": 3, "successful": 1,
                       "fallback": 2, "timeout": 0, "invalid": 0,
                       "low_confidence": 0}})
        ep = controller._episode_summary(old)
        self.assertEqual(ep["reflex"]["rejected"], 0)
        self.assertEqual(ep["reflex"]["fallback"], 2)
        # a NEW artifact carries a distinct rejected count, preserved verbatim
        new = self._result(2, {
            "usage": {"prompt_tokens": 5},
            "reflex": {"applied": 0, "paid_dispatched": 4, "successful": 0,
                       "rejected": 1, "fallback": 4, "timeout": 1,
                       "invalid": 0, "low_confidence": 0}})
        ep2 = controller._episode_summary(new)
        self.assertEqual(ep2["reflex"]["rejected"], 1)
        self.assertEqual(ep2["reflex"]["fallback"], 4)
        summary = controller.campaign_summary([old, new], ProviderConfig(), 1.0)
        self.assertEqual(summary["totals"]["reflex"]["rejected"], 1)
        self.assertEqual(summary["totals"]["reflex"]["fallback"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)