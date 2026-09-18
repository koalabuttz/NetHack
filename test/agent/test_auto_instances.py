#!/usr/bin/env python3
"""Wave-2 tests: the deterministic level-instance/terrain/hero layer.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Covers ``tools/agent/instances.py``: the full-cell terrain/occupancy split
(section 4.3), :class:`HeroResolution` sets (4.2) and the level-instance
automaton (4.1) including the plan's section 8.2 transition fixture matrix
and its signal-ablation cases.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import instances as I  # noqa: E402


class CellClassification(unittest.TestCase):
    """Full-cell classification under the pinned profile (M01-M03)."""

    def test_gray_walls_and_brown_doors(self):
        self.assertEqual(I.classify_cell("|", "gray").terrain, I.T_WALL)
        self.assertEqual(I.classify_cell("-", "gray").terrain, I.T_WALL)
        self.assertEqual(I.classify_cell("-", "brown").terrain,
                         I.T_OPEN_DOOR)
        self.assertEqual(I.classify_cell("|", "brown").terrain,
                         I.T_OPEN_DOOR)

    def test_closed_door_is_not_walkable_and_needs_opening(self):
        cell = I.classify_cell("+", "brown")
        self.assertEqual(cell.terrain, I.T_CLOSED_DOOR)
        self.assertFalse(cell.walkable)
        # a glyph-only classifier would call every '+' a closed door; the
        # colour matters, and a non-brown '+' is not proved a door
        self.assertEqual(I.classify_cell("+", "gray").terrain, I.T_WALL)

    def test_blank_is_unknown_not_floor(self):
        for glyph in (" ", ""):
            cell = I.classify_cell(glyph, "")
            self.assertEqual(cell.terrain, I.T_UNKNOWN)
            self.assertFalse(cell.walkable)

    def test_every_monster_punctuation_is_a_hazard(self):
        for glyph in ("'", "&", ";", ":", "~", "]"):
            cell = I.classify_cell(glyph, "gray")
            self.assertEqual(cell.occupant, I.OCC_MONSTER, glyph)

    def test_letters_and_off_hero_at_are_hazards(self):
        self.assertEqual(I.classify_cell("d", "").occupant, I.OCC_MONSTER)
        self.assertEqual(I.classify_cell("@", "").occupant,
                         I.OCC_HERO_OR_HUMAN)
        self.assertTrue(I.classify_cell("@", "").hazardous)

    def test_stairs_and_floor(self):
        self.assertEqual(I.classify_cell(">", "gray").terrain,
                         I.T_STAIRS_DOWN)
        self.assertEqual(I.classify_cell("<", "gray").terrain, I.T_STAIRS_UP)
        self.assertTrue(I.classify_cell(".", "gray").walkable)
        self.assertTrue(I.classify_cell("#", "gray").walkable)

    def test_water_and_lava_are_blocked(self):
        self.assertEqual(I.classify_cell("}", "blue").terrain, I.T_WATER)
        self.assertEqual(I.classify_cell("}", "red").terrain, I.T_LAVA)
        self.assertFalse(I.classify_cell("}", "blue").walkable)

    def test_unknown_variant_fails_closed(self):
        self.assertFalse(I.classify_cell("\x07", "gray").walkable)


class TerrainMemoryTests(unittest.TestCase):
    def test_occupant_never_erases_remembered_stairs(self):
        t = I.TerrainMemory()
        t.merge({(3, 3): (">", "gray", "", "")})
        self.assertIn((3, 3), t.stairs_down())
        t.merge({(3, 3): ("d", "", "", "")})
        self.assertIn((3, 3), t.stairs_down())      # remembered
        self.assertFalse(t.walkable((3, 3)))        # but blocked now

    def test_map_revision_only_on_structural_change(self):
        t = I.TerrainMemory()
        t.merge({(1, 1): (".", "gray", "", "")})
        rev = t.map_revision
        # an occupant change is not a structural change
        t.merge({(1, 1): ("d", "", "", "")})
        self.assertEqual(t.map_revision, rev)
        self.assertGreater(t.occupancy_generation, 0)
        t.merge({(1, 1): ("|", "gray", "", "")})
        self.assertGreater(t.map_revision, rev)

    def test_walkable_requires_proved_ground_and_no_occupant(self):
        t = I.TerrainMemory()
        self.assertFalse(t.walkable((9, 9)))        # never observed
        t.merge({(9, 9): (".", "gray", "", "")})
        self.assertTrue(t.walkable((9, 9)))
        t.merge({(9, 9): ("@", "", "", "")})
        self.assertFalse(t.walkable((9, 9)))

    def test_structural_delta_reports_conflicts_only(self):
        old = I.TerrainMemory()
        old.merge({(1, 1): ("|", "gray", "", ""),
                   (2, 2): (".", "gray", "", "")})
        same, conflicts = I.structural_delta(old, {(1, 1): I.T_WALL})
        self.assertFalse(same)
        same, conflicts = I.structural_delta(old, {(1, 1): I.T_STAIRS_DOWN})
        self.assertTrue(same)
        self.assertEqual(conflicts, (((1, 1), I.T_STAIRS_DOWN),))


class HeroResolutionTests(unittest.TestCase):
    def test_unique_at_bootstraps_confirmed(self):
        res = I.bootstrap_hero(((5, 5),))
        self.assertTrue(res.resolved)
        self.assertEqual(res.confirmed, (5, 5))
        self.assertFalse(res.suppress_movement)

    def test_zero_and_multiple_at_are_sets(self):
        zero = I.bootstrap_hero(())
        self.assertFalse(zero.resolved)
        self.assertTrue(zero.outside)
        multi = I.bootstrap_hero(((5, 5), (5, 6)))
        self.assertFalse(multi.resolved)
        self.assertEqual(multi.possible, frozenset(((5, 5), (5, 6))))
        self.assertTrue(multi.suppress_movement)

    def test_first_at_is_never_chosen(self):
        # M04: a resolver that picks the first @ would resolve here
        multi = I.bootstrap_hero(((5, 5), (7, 7)))
        self.assertIsNone(multi.confirmed)

    def test_incoherent_presentation_fails_closed(self):
        res = I.bootstrap_hero(((5, 5),), coherent=False)
        self.assertFalse(res.resolved)
        self.assertTrue(res.outside)

    def test_nonmovement_preserves_old_position(self):
        prior = I.bootstrap_hero(((5, 5),))
        ev = I.MovementEvidence(nonmovement=True, coherent=True)
        out = I.reconcile_hero(prior, ev, ((5, 5),), 1)
        self.assertTrue(out.resolved)
        self.assertEqual(out.confirmed, (5, 5))

    def test_expected_destination_confirms(self):
        prior = I.bootstrap_hero(((5, 5),))
        ev = I.MovementEvidence(expected=(5, 6))
        out = I.reconcile_hero(prior, ev, ((5, 6),), 1)
        self.assertTrue(out.resolved)
        self.assertEqual(out.confirmed, (5, 6))

    def test_unexpected_relocation_expands_the_set(self):
        prior = I.bootstrap_hero(((5, 5),))
        ev = I.MovementEvidence(expected=(5, 6), unexpected=True)
        out = I.reconcile_hero(prior, ev, ((9, 9),), 1)
        self.assertFalse(out.resolved)
        self.assertIn((5, 5), out.possible)
        self.assertIn((5, 6), out.possible)
        self.assertIn((9, 9), out.possible)
        self.assertTrue(out.outside)      # M19: alternatives preserved

    def test_unresolved_movement_keeps_possibilities_not_a_winner(self):
        prior = I.bootstrap_hero(((5, 5),))
        ev = I.MovementEvidence()
        out = I.reconcile_hero(prior, ev, ((5, 5), (5, 6)), 1)
        self.assertFalse(out.resolved)
        self.assertEqual(out.possible, frozenset(((5, 5), (5, 6))))


class TransitionFixtures(unittest.TestCase):
    """The plan's section 8.2 fixture matrix."""

    def _active(self):
        a = I.LevelInstanceAutomaton()
        iid = a.begin_playable()
        return a, iid

    def test_unbound_allocates_on_first_playable_observation(self):
        a = I.LevelInstanceAutomaton()
        self.assertEqual(a.state, I.UNBOUND)
        iid = a.begin_playable()
        self.assertEqual(a.state, I.ACTIVE)
        self.assertEqual(a.current(), iid)

    def test_transition_depth_change(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        self.assertEqual(a.state, I.PENDING)
        st = a.observe((I.S_STAIR, I.S_LABEL), hero_usable=True)
        self.assertEqual(st.state, I.ACTIVE)
        self.assertNotEqual(st.instance_id, iid)     # fresh scope
        self.assertTrue(a.events)                    # recorded once

    def test_transition_same_label_branch(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        st = a.observe((I.S_STAIR, I.S_OUTCOME), hero_usable=True)
        self.assertNotEqual(st.instance_id, iid)     # same label still fresh

    def test_transition_message_lookalike_ignored(self):
        a, iid = self._active()
        # no transition was sent; a lookalike message in a non-outcome
        # context is not a signal and creates no scope
        self.assertIsNone(a.note_transition_sent(False))
        st = a.observe((), hero_usable=True)
        self.assertEqual(st.instance_id, iid)
        self.assertEqual(st.state, I.ACTIVE)

    def test_transition_ambiguous_outcome_looking_allocates(self):
        a, iid = self._active()
        st = a.observe((I.S_OUTCOME,), hero_usable=True)
        self.assertNotEqual(st.instance_id, iid)     # conservative fresh

    def test_transition_without_message_label_only(self):
        a, iid = self._active()
        st = a.observe((I.S_LABEL,), hero_usable=True)
        self.assertNotEqual(st.instance_id, iid)

    def test_transition_without_message_topology_only(self):
        a, iid = self._active()
        st = a.observe((I.S_DISCONT,), hero_usable=True)
        self.assertNotEqual(st.instance_id, iid)

    def test_transition_zero_at_allocates_before_hero_resolution(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        st = a.observe((I.S_STAIR, I.S_LABEL), hero_usable=False)
        self.assertEqual(st.state, I.FRESH_UNRESOLVED)
        self.assertNotEqual(st.instance_id, iid)
        self.assertIsNotNone(a.current())            # allocated, unresolved
        hero = I.bootstrap_hero(())
        self.assertTrue(hero.suppress_movement)

    def test_transition_multiple_at_allocates_before_hero_resolution(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        st = a.observe((I.S_STAIR, I.S_DISCONT), hero_usable=False)
        self.assertEqual(st.state, I.FRESH_UNRESOLVED)
        self.assertNotEqual(st.instance_id, iid)
        hero = I.bootstrap_hero(((1, 1), (2, 2)))
        self.assertTrue(hero.suppress_movement)

    def test_fresh_unresolved_resolves_on_a_later_usable_observation(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        a.observe((I.S_STAIR, I.S_LABEL), hero_usable=False)
        st = a.observe((), hero_usable=True)
        self.assertEqual(st.state, I.ACTIVE)
        self.assertEqual(st.instance_id, iid + 1)    # same fresh arrival

    def test_transition_branch_depth_collision_three_scopes(self):
        a, first = self._active()
        a.note_transition_sent(True)
        s2 = a.observe((I.S_STAIR, I.S_LABEL), hero_usable=True)
        a.note_transition_sent(True)
        s3 = a.observe((I.S_STAIR, I.S_LABEL), hero_usable=True)
        ids = {first, s2.instance_id, s3.instance_id}
        self.assertEqual(len(ids), 3)                # three scopes

    def test_transition_rejected_stair_keeps_old_scope(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        st = a.observe((I.S_NOARRIVAL,), hero_usable=True)
        self.assertEqual(st.instance_id, iid)        # no new-map merge
        self.assertEqual(st.state, I.ACTIVE)

    def test_transition_conflict_timeout_retires_old_scope(self):
        a, iid = self._active()
        a.note_transition_sent(True)
        st = a.timeout()
        self.assertNotEqual(st.instance_id, iid)
        self.assertEqual(st.state, I.FRESH_UNRESOLVED)
        # a later stale callback must not reactivate the old scope
        st2 = a.observe((), hero_usable=False)
        self.assertEqual(st2.state, I.FRESH_UNRESOLVED)
        self.assertNotEqual(st2.instance_id, iid)

    def test_cancelled_proposal_never_sent_creates_no_transition(self):
        a, iid = self._active()
        a.note_transition_sent(False)
        st = a.observe((), hero_usable=True)
        self.assertEqual(st.instance_id, iid)
        self.assertEqual(st.state, I.ACTIVE)

    def test_stopped_commits_nothing(self):
        a, iid = self._active()
        a.stop()
        st = a.observe((I.S_STAIR, I.S_LABEL), hero_usable=True)
        self.assertEqual(st.state, I.STOPPED)
        self.assertEqual(st.instance_id, iid)


class SignalAblation(unittest.TestCase):
    """Removing any one signal still allocates fresh (8.2); each detector is
    separately necessary, so its sole-signal fixture must fail if disabled."""

    _SOLE = {
        I.S_STAIR: (I.S_STAIR,),
        I.S_OUTCOME: (I.S_OUTCOME,),
        I.S_LABEL: (I.S_LABEL,),
        I.S_DISCONT: (I.S_DISCONT,),
    }

    def test_multi_signal_allocates_fresh(self):
        a = I.LevelInstanceAutomaton()
        iid = a.begin_playable()
        st = a.observe((I.S_STAIR, I.S_OUTCOME, I.S_LABEL, I.S_DISCONT),
                       hero_usable=True)
        self.assertNotEqual(st.instance_id, iid)

    def test_each_signal_alone_allocates_fresh(self):
        for signal, sig in self._SOLE.items():
            a = I.LevelInstanceAutomaton()
            iid = a.begin_playable()
            if signal == I.S_STAIR:
                a.note_transition_sent(True)
            st = a.observe(sig, hero_usable=True)
            self.assertNotEqual(st.instance_id, iid, signal)

    def test_removing_one_signal_from_a_multi_set_still_allocation(self):
        for signal in self._SOLE:
            a = I.LevelInstanceAutomaton()
            iid = a.begin_playable()
            a.note_transition_sent(True)
            rest = tuple(s for s in (I.S_STAIR, I.S_OUTCOME, I.S_LABEL,
                                     I.S_DISCONT) if s != signal)
            if not rest:
                continue
            st = a.observe(rest, hero_usable=True)
            self.assertNotEqual(st.instance_id, iid, signal)

    def test_no_arrival_alone_keeps_scope(self):
        a = I.LevelInstanceAutomaton()
        iid = a.begin_playable()
        a.note_transition_sent(True)
        st = a.observe((I.S_NOARRIVAL,), hero_usable=True)
        self.assertEqual(st.instance_id, iid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
