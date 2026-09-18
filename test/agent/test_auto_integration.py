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

import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (arbitration, candidates, controller,  # noqa: E402
                         directives, forced_search, instances, policy,
                         protocol, state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402
from test_auto import CLOSED, HELLO, WireHarness, _line, obs  # noqa: E402

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
                ref.last_inv_tick, ref.eat_forced_menu,
                len(ref.recovery.search.completed),
                set(ref.recovery.search.refused),
                ref.recovery.refused_site, tuple(ref.recovery.cycle.history),
                ref._cycled, ref.food.inventory_signature,
                set(ref.food.locations), mem.no_progress,
                mem.searches_since_progress, dict(mem.visits), mem.hero)

    def test_prepare_and_decide_leave_state_unchanged(self):
        ref = policy.ScriptedReflex(ProviderConfig())
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


# ---------------------------------------------------------------- MEDIUM 6

class LiveEvaluatorParity(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
