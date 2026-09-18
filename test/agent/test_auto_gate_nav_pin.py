#!/usr/bin/env python3
"""Phase-0 regression pins for the Jev gate/applied-cap/navigation follow-up.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

These tests capture the *pre-change* behaviour that the approved follow-up
plan (``doc/agent-jev-gate-nav-plan.md``, revision 4) exists to change.  They
are recorded evidence, **not** a new golden standard: each pin is updated in
the phase that deliberately changes the behaviour it locks down --

  * pins 1-2  -> Phase 1 (peakedness-relative confidence acceptance),
  * pin 3     -> Phase 2 (applied-decision cap accounting),
  * pins 4-6  -> Phase 3 (scripted anti-oscillation).

The canned responses and deterministic maps here are the reproduction inputs
the plan asks for: a valid spread winner below the old absolute threshold,
reservations exhausted by consultations that were never applied, two frontier
pockets with no third exit, and the observation-owned recovery surface.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (arbitration, budget,  # noqa: E402
                         policy, protocol, recovery, state)
from tools.agent.providers import (ProviderConfig,  # noqa: E402
                                   ReflexContext)

FLOOR = (".", "gray", 0, "none")
WALL = ("|", "gray", 0, "none")
DOOR = ("+", "brown", 0, "none")
DOWN = (">", "white", 0, "none")


def _cand(code, label, **kw):
    from tools.agent import candidates
    return candidates.make_candidate({"key": code}, label, **kw)


def _table(cands, need_key=(1, 1, 1)):
    from tools.agent import candidates
    return candidates.build_table(need_key, 1, cands)


def ctx(mem, tick=0):
    return ReflexContext(
        episode=1, tick=tick, need={"kind": "command", "id": 1},
        need_key=protocol.NeedKey(1, tick, 1), snapshot=protocol.Snapshot(),
        pages=[], memory=mem, directives=[], deadline=0.0)


def mem_with(cells, hero):
    mem = state.EpisodeMemory()
    mem.grid.update(cells)
    mem.hero = hero
    mem.status.hp = 20
    mem.status.hp_max = 20
    mem.inventory.refresh([], 0, 0)
    return mem


def _walk(cells, hero, steps):
    """A deterministic scripted walk over *cells*; returns visited cells."""
    ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
    mem = mem_with(cells, hero)
    history = [hero]
    for i in range(steps):
        chosen = ref.prepare(ctx(mem, tick=i)).table.scripted()
        key = chosen.action.to_wire().get("key")
        step = None
        for delta, code in protocol.DIR_KEYS.items():
            if code == key:
                step = delta
        if step is None:
            break
        hero = (hero[0] + step[0], hero[1] + step[1])
        history.append(hero)
        mem.hero = hero
        mem.visits[hero] = mem.visits.get(hero, 0) + 1
    return history


class PinConfidenceGate(unittest.TestCase):
    """The old flat absolute gate, and the relative rule that replaces it.

    Phase 1 flipped these pins: the flat threshold survives only in the
    explicit ``absolute`` rollback, while the default relative rule accepts a
    genuine spread winner on concentration.
    """

    def _table(self):
        return _table([_cand(protocol.KEY_H, "navigate", family="frontier"),
                       _cand(protocol.KEY_L, "navigate", family="frontier"),
                       _cand(protocol.KEY_J, "navigate", family="frontier")])

    def test_pin_absolute_gate_rejects_spread_winner(self):
        # A valid three-way spread distribution whose winning probability is
        # 1/3 is rejected by the shipped 0.8 threshold -- the campaign defect,
        # now reachable only through the explicit absolute rollback.
        table = self._table()
        raw = arbitration.RawChoice(
            table_id=table.table_id, need_key=tuple(table.need_key),
            table_version=table.table_version, index=0,
            confidence=1.0 / 3.0, dispatched=True)
        outcome = arbitration.validate_raw_choice(
            table, raw, arbitration.RejectionSet(), threshold=0.8,
            eligible=lambda c: True, mode=arbitration.CONFIDENCE_ABSOLUTE)
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.code, "confidence")

    def test_relative_gate_accepts_a_soft_spread_winner(self):
        # The default relative rule reads the selected option's own validated
        # probability: a genuine but soft three-way winner (0.55 > 1.5/3)
        # passes on concentration even though the legacy 0.8 gate rejects it.
        table = self._table()
        raw = arbitration.RawChoice(
            table_id=table.table_id, need_key=tuple(table.need_key),
            table_version=table.table_version, index=0,
            confidence=0.0, selected_probability=0.55, dispatched=True)
        outcome = arbitration.validate_raw_choice(
            table, raw, arbitration.RejectionSet(), eligible=lambda c: True)
        self.assertTrue(outcome.accepted)
        self.assertIn("N=3", outcome.reason)

    def test_pin_selected_probability_field_is_present(self):
        # The neutral records now carry the selected probability the relative
        # gate reads; it defaults to None so old constructors stay valid.
        self.assertIsNone(arbitration.RawChoice().selected_probability)
        self.assertIn("selected_probability",
                      {f for f in arbitration.RawChoice.__dataclass_fields__})


class PinAppliedCap(unittest.TestCase):
    """Reservation-based admission, replaced by applied-decision admission.

    Phase 2 flipped these pins: the cap now bounds *applied* Jev decisions, so
    unapplied consultations no longer exhaust the allowance.
    """

    def test_pin_reservation_cap_no_longer_bounds_unapplied_consultations(
            self):
        ledger = budget.BudgetLedger(reflex_cap=2)
        # two paid consultations are reserved and then both rejected at
        # arbitration: nothing was ever applied to the wire ...
        self.assertIsNotNone(ledger.reserve_reflex_paid())
        self.assertIsNotNone(ledger.reserve_reflex_paid())
        # ... so the applied cap is untouched and further consultation stays
        # admissible (the reservation diagnostic still records both).
        self.assertEqual(ledger.reflex_paid_dispatched, 2)
        self.assertEqual(ledger.reflex_applied, 0)
        self.assertTrue(ledger.reflex_paid_available())
        self.assertIsNotNone(ledger.reserve_reflex_paid())
        # and the ledger now reports an applied counter
        self.assertIn("applied", ledger.as_dict()["reflex"])


class PinNavigation(unittest.TestCase):
    """Navigation-generated reversals (Phase 3 changes this)."""

    def test_pin_two_frontier_pockets_alternate(self):
        # A one-wide corridor with a frontier pocket at each end: the shipped
        # frontier scoring drives an avoidable two-cell oscillation.
        cells = {}
        for x in range(2, 7):
            cells[(x, 10)] = FLOOR
            cells[(x, 9)] = WALL
            cells[(x, 11)] = WALL
        history = _walk(cells, (3, 10), 6)
        self.assertEqual(history[:4], [(3, 10), (2, 10), (3, 10), (2, 10)])

    def test_pin_cycled_flag_alone_does_not_enter_recovery(self):
        # The detected cycle flag currently only *suppresses* search; with no
        # stationary no_progress it does not force a recovery decision, so the
        # scripted reflex still offers ordinary navigation.
        cells = {}
        for x in range(2, 7):
            cells[(x, 10)] = FLOOR
            cells[(x, 9)] = WALL
            cells[(x, 11)] = WALL
        mem = mem_with(cells, (3, 10))
        ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        ref._cycled = True
        table = ref.prepare(ctx(mem)).table
        self.assertGreater(len(table.ordered_candidates), 1)
        self.assertEqual(table.scripted().family, "frontier")

    def test_pin_recovery_state_has_no_movement_history_surface(self):
        rs = recovery.RecoveryState()
        self.assertFalse(hasattr(rs, "previous_distinct"))
        self.assertFalse(hasattr(rs, "cycle_active"))


if __name__ == "__main__":
    unittest.main()
