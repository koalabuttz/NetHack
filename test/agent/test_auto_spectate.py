#!/usr/bin/env python3
"""Focused tests for live autoplay spectating.

Standard-library unittest, engine-free.  Covers the read-only frame views
(``tools/agent/render.auto_frame``) and the shared directive-eligibility
authority (``directives._ineligibility_reason`` / ``DirectiveBook.peek_view``)
added by the autoplay work, and -- in later sections -- the bounded transport
(``tools/agent/spectating``) and the controller integration.

Usage:
    python3 -m unittest discover -s test/agent -p test_auto_spectate.py
"""

import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import format_obs  # noqa: E402
from tools.agent import render  # noqa: E402
from tools.agent.directives import (  # noqa: E402
    DirectiveBook, DirectiveSet, PreconditionState)
from tools.agent.protocol import Snapshot  # noqa: E402
from tools.agent.state import EpisodeMemory  # noqa: E402


# ------------------------------------------------------------------ helpers

def obs_record(seq, cells=(), cur=None, status=None, msg=(), windows=(),
               need=None):
    """A minimal but complete base:null obs record for direct application."""
    pal = [[0, " ", "none", 0, "none"]]
    triples = []
    for n, (x, y, ch) in enumerate(sorted(cells, key=lambda c: (c[1], c[0]))):
        pal.append([n + 1, ch, "none", 0, "none"])
        triples.append([x, y, n + 1])
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None, "s": status or {}, "cond": [], "pal": pal,
            "map": triples, "cur": list(cur) if cur else None,
            "msg": list(msg), "hist": [], "windows": list(windows),
            "need": need}


def win(w, kind="menu", title="", content="c1", pages=1):
    return {"w": w, "kind": kind, "title": title, "content": content,
            "pages": pages}


def apply_to(snap, mem, rec):
    snap.apply(rec)
    mem.observe(snap)


def make(seq=1, cells=(), cur=None, status=None, msg=(), windows=(),
         need=None):
    snap = Snapshot()
    mem = EpisodeMemory()
    rec = obs_record(seq, cells, cur, status, msg, windows, need)
    apply_to(snap, mem, rec)
    return snap, mem


def frame(snap, mem, **kw):
    base = dict(episode=1, seq=snap.seq, tick=0, need=None, windows=[],
                directives=None, strategy_calls=0, usage={})
    base.update(kw)
    return render.auto_frame(snap, mem, **base)


def map_rows(lines):
    return lines[2:2 + format_obs.MAP_H]


# ------------------------------------------------------- frame geometry

class AutoFrameGeometry(unittest.TestCase):
    def test_21_rows_of_80_ascii_cells(self):
        snap, mem = make(seq=1, cells=[(5, 4, "@")], cur=(5, 4))
        lines = frame(snap, mem)
        rows = map_rows(lines)
        self.assertEqual(len(rows), 21)
        for row in rows:
            self.assertEqual(len(row), 80)
            self.assertTrue(all(32 <= ord(c) <= 126 for c in row))

    def test_x_zero_is_a_synthesized_blank(self):
        snap, mem = make(seq=1, cells=[(1, 0, "-")])
        rows = map_rows(frame(snap, mem))
        for row in rows:
            self.assertEqual(row[0], " ")

    def test_map_reflects_applied_snapshot(self):
        snap, mem = make(seq=1, cells=[(5, 4, "@"), (6, 4, "-")])
        rows = map_rows(frame(snap, mem))
        self.assertEqual(rows[4][5], "@")
        self.assertEqual(rows[4][6], "-")

    def test_cursor_overlay_is_exact_and_no_hero_fallback(self):
        # hero is on the map but the engine cursor is elsewhere
        snap, mem = make(seq=1, cells=[(5, 4, "@")], cur=(9, 7))
        rows = map_rows(frame(snap, mem))
        self.assertEqual(rows[7][9], "*")
        self.assertEqual(rows[4][5], "@")     # hero glyph untouched
        # with no cursor at all, nothing is overlaid
        snap2, mem2 = make(seq=1, cells=[(5, 4, "@")], cur=None)
        rows2 = map_rows(frame(snap2, mem2))
        self.assertNotIn("*", "".join(rows2))
        self.assertEqual(rows2[4][5], "@")

    def test_non_ascii_glyph_degrades_to_one_cell(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        # palette entry with a non-ASCII char, applied directly
        rec = obs_record(1, cells=[(5, 4, "@")])
        rec["pal"].append([2, "\u00e9", "none", 0, "none"])
        rec["map"] = [[4, 4, 2]]
        apply_to(snap, mem, rec)
        rows = map_rows(frame(snap, mem))
        self.assertEqual(rows[4][4], "?")
        self.assertEqual(len(rows[4]), 80)


class AutoFrameDisappearingCell(unittest.TestCase):
    """The map authority is the applied Snapshot, never remembered terrain."""

    def test_cell_gone_from_presentation_is_blank(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        apply_to(snap, mem, obs_record(1, cells=[(5, 4, "=")]))
        self.assertEqual(mem.tile((5, 4)), "=")     # durable memory kept it
        rows = map_rows(frame(snap, mem))
        self.assertEqual(rows[4][5], "=")

        # the next presentation no longer paints that cell (e.g. out of
        # sight); memory still holds it, but the frame must not draw it
        apply_to(snap, mem, obs_record(2, cells=[]))
        self.assertEqual(mem.tile((5, 4)), "=")
        rows = map_rows(frame(snap, mem))
        self.assertEqual(rows[4][5], " ")


class AutoFrameMessageHistory(unittest.TestCase):
    def test_message_less_observation_retains_history(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        apply_to(snap, mem, obs_record(1, msg=[{"e": 1, "text": "hello",
                                                "style": 0}]))
        apply_to(snap, mem, obs_record(2, msg=[]))
        lines = frame(snap, mem)
        self.assertIn("  msg: hello", lines)

    def test_only_the_trailing_three_messages(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        apply_to(snap, mem, obs_record(1, msg=[
            {"e": 1, "text": "a", "style": 0},
            {"e": 2, "text": "b", "style": 0},
            {"e": 3, "text": "c", "style": 0},
            {"e": 4, "text": "d", "style": 0}]))
        lines = frame(snap, mem, messages=3)
        msgs = [ln for ln in lines if ln.startswith("  msg:")]
        self.assertEqual(msgs, ["  msg: b", "  msg: c", "  msg: d"])

    def test_sanitized_clipping(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        long_unicode = "\u00e9" * 200 + "\ttab"
        apply_to(snap, mem, obs_record(
            1, msg=[{"e": 1, "text": long_unicode, "style": 0}]))
        line = [ln for ln in frame(snap, mem) if ln.startswith("  msg:")][0]
        self.assertTrue(all(32 <= ord(c) <= 126 for c in line))
        self.assertLessEqual(len(line), 80)


class AutoFrameStatus(unittest.TestCase):
    def test_unknown_fields_are_question_marks(self):
        snap, mem = make(seq=1)
        line = frame(snap, mem)[1]
        self.assertEqual(
            line,
            "  status: dlvl=? hp=?/? time=? xp=? hunger=? gold=?")

    def test_known_fields_rendered(self):
        status = {"hitpoints": {"text": "18", "color": "none", "style": 0},
                  "hitpoints-max": {"text": "22", "color": "none",
                                    "style": 0},
                  "hunger": {"text": "Hungry", "color": "none", "style": 0},
                  "dungeon-level": {"text": "3", "color": "none",
                                    "style": 0},
                  "time": {"text": "123", "color": "none", "style": 0},
                  "experience-level": {"text": "4", "color": "none",
                                       "style": 0},
                  "gold": {"text": "$:45", "color": "none", "style": 0}}
        snap, mem = make(seq=1, status=status)
        line = frame(snap, mem)[1]
        self.assertEqual(
            line,
            "  status: dlvl=3 hp=18/22 time=123 xp=4 hunger=Hungry gold=45")


class AutoFrameNeed(unittest.TestCase):
    def test_need_kind_and_menu_title_from_snapshot_windows(self):
        windows = [win("m1", "menu", title="What do you want to eat?")]
        snap, mem = make(seq=1, windows=windows,
                         need={"id": 1, "kind": "menu", "menu": "m1",
                               "mode": "one", "content": "c1", "pages": 1})
        lines = frame(snap, mem, need=snap.need,
                      windows=list(snap.windows.values()))
        need_line = [ln for ln in lines if ln.startswith("  need:")][0]
        self.assertIn("menu m1 mode=one", need_line)
        self.assertIn("title=", need_line)

    def test_explicit_need_overrides_snapshot_need(self):
        snap, mem = make(seq=1, need={"id": 1, "kind": "command"})
        # caller passes a different (e.g. frozen) need
        lines = frame(snap, mem, need={"id": 2, "kind": "yn",
                                       "prompt": "Really?",
                                       "choices": "yn", "default": ord("y"),
                                       "numeric": False})
        need_line = [ln for ln in lines if ln.startswith("  need:")][0]
        self.assertIn("yn prompt=", need_line)

    def test_no_need_line_when_no_need(self):
        snap, mem = make(seq=1)
        lines = frame(snap, mem, need=None)
        self.assertFalse(any(ln.startswith("  need:") for ln in lines))


class AutoFrameDirectivesAndCounters(unittest.TestCase):
    def test_inactive_directives_line(self):
        snap, mem = make(seq=1)
        lines = frame(snap, mem, directives=None)
        self.assertIn("  directives: none", lines)

    def test_active_directives_line(self):
        book = DirectiveBook()
        dset = DirectiveSet(goals=("survive", "explore_frontier"),
                            target=(10, 5), risk=0.25, ttl=7)
        book.activate(dset, tick=1, level="1")
        st = PreconditionState(hero_known=True, hp_known=True, hp_frac=1.0)
        view = book.peek_view(tick=2, level="1", st=st)
        snap, mem = make(seq=1)
        line = [ln for ln in frame(snap, mem, directives=view)
                if ln.startswith("  directives:")][0]
        self.assertIn("goals=survive,explore_frontier", line)
        self.assertIn("target=10,5", line)
        self.assertIn("risk=0.25", line)
        self.assertIn("ttl=7", line)
        self.assertIn("gen=1", line)

    def test_counters_na_when_no_classified_tokens(self):
        snap, mem = make(seq=1)
        usage = {"cache_hit_tokens": 0, "cache_miss_tokens": 0,
                 "cache_unclassified_tokens": 40}
        line = [ln for ln in frame(snap, mem, strategy_calls=2, usage=usage)
                if ln.startswith("  counters:")][0]
        self.assertIn("strategy_calls=2", line)
        self.assertIn("unclass=40", line)
        self.assertIn("hit=n/a", line)

    def test_counters_rate_excludes_unclassified(self):
        snap, mem = make(seq=1)
        usage = {"cache_hit_tokens": 60, "cache_miss_tokens": 40,
                 "cache_unclassified_tokens": 1000}
        line = [ln for ln in frame(snap, mem, strategy_calls=3, usage=usage)
                if ln.startswith("  counters:")][0]
        self.assertIn("cache_hit=60", line)
        self.assertIn("cache_miss=40", line)
        self.assertIn("unclass=1000", line)
        self.assertIn("hit=60.0%", line)     # 60 / (60+40), unclassified out
        self.assertIn("strategy_calls=3", line)


class AutoFrameImmutability(unittest.TestCase):
    def test_render_is_deterministic_and_mutates_nothing(self):
        snap = Snapshot()
        mem = EpisodeMemory()
        apply_to(snap, mem, obs_record(
            1, cells=[(5, 4, "@")], cur=(5, 4),
            status={"gold": {"text": "$:12", "color": "none", "style": 0}},
            msg=[{"e": 1, "text": "x", "style": 0}]))
        before_map = copy.deepcopy(snap.map)
        before_cur = snap.cur
        before_msgs = copy.deepcopy(mem.messages)
        a = frame(snap, mem)
        b = frame(snap, mem)
        self.assertEqual(a, b)
        self.assertEqual(snap.map, before_map)
        self.assertEqual(snap.cur, before_cur)
        self.assertEqual(mem.messages, before_msgs)

    def test_final_frame_adds_final_clause(self):
        snap, mem = make(seq=4)
        obs_lines = frame(snap, mem, seq=4, final_reason=None, outcome=None)
        self.assertNotIn("final", obs_lines[0])
        fin = frame(snap, mem, seq=4, final_reason="closed", outcome="death")
        self.assertIn("final", fin[0])
        self.assertIn("stop=closed", fin[0])
        self.assertIn("outcome=death", fin[0])


class AutoFrameGolden(unittest.TestCase):
    """One full frame, pinned verbatim (map, cursor, status, need, counters)."""

    GOLDEN = "\n".join([
        "auto episode=2 seq=42 tick=9",
        "  status: dlvl=3 hp=18/22 time=812 xp=4 hunger=Hungry gold=45",
    ] + [
        " " * 80, " " * 80, " " * 80, " " * 80,
        " " * 9 + "|.-" + " " * 68,
        " " * 8 + "%|*d#" + " " * 67,
        " " * 11 + ">" + " " * 68,
    ] + [" " * 80] * 14 + [
        "  msg: You feel hungry.",
        "  need: command",
        "  directives: gen=1 goals=acquire_food,survive target=12,5 "
        "risk=0.20 ttl=50",
        "  counters: strategy_calls=3 cache_hit=60 cache_miss=40 unclass=0 "
        "hit=60.0%",
    ])

    def test_full_frame(self):
        status = {"hitpoints": {"text": "18", "color": "none", "style": 0},
                  "hitpoints-max": {"text": "22", "color": "none",
                                    "style": 0},
                  "hunger": {"text": "Hungry", "color": "none", "style": 0},
                  "dungeon-level": {"text": "3", "color": "none",
                                    "style": 0},
                  "time": {"text": "812", "color": "none", "style": 0},
                  "experience-level": {"text": "4", "color": "none",
                                       "style": 0},
                  "gold": {"text": "$:45", "color": "none", "style": 0}}
        cells = [(10, 5, "@"), (11, 4, "-"), (9, 4, "|"), (11, 5, "d"),
                 (10, 4, "."), (11, 6, ">"), (12, 5, "#"), (9, 5, "|"),
                 (8, 5, "%")]
        snap, mem = make(seq=42, cells=cells, cur=(10, 5), status=status,
                         msg=[{"e": 7, "text": "You feel hungry.",
                               "style": 0}])
        book = DirectiveBook()
        book.activate(DirectiveSet(goals=("acquire_food", "survive"),
                                   target=(12, 5), risk=0.2, ttl=50),
                      tick=1, level="3")
        st = PreconditionState(hero_known=True, hp_known=True,
                               hp_frac=18 / 22.0, hungry=True)
        view = book.peek_view(tick=5, level="3", st=st)
        usage = {"cache_hit_tokens": 60, "cache_miss_tokens": 40,
                 "cache_unclassified_tokens": 0}
        lines = render.auto_frame(
            snap, mem, episode=2, seq=42, tick=9,
            need={"id": 1, "kind": "command"}, windows=[], directives=view,
            strategy_calls=3, usage=usage)
        self.assertEqual("\n".join(lines), self.GOLDEN)


# -------------------------------------------- directive eligibility authority

class DirectiveEligibility(unittest.TestCase):
    def _st(self, **kw):
        base = dict(hero_known=True, hp_known=True, hp_frac=1.0,
                    hungry=False, inventory_fresh=True)
        base.update(kw)
        return PreconditionState(**base)

    def _book(self, sink=None):
        book = DirectiveBook(sink=sink)
        book.activate(DirectiveSet(goals=("survive",), ttl=3), tick=10,
                      level="1")
        return book

    def test_peek_view_equals_view_when_eligible(self):
        for tick, level in ((10, "1"), (13, "1")):
            b1, b2 = self._book(), self._book()
            pv = b1.peek_view(tick, level, self._st())
            vv = b2.view(tick, level, self._st())
            self.assertEqual(pv.dset, vv.dset)
            self.assertEqual(pv.generation, vv.generation)

    def test_peek_view_inactive_matches_view_inactive(self):
        cases = ((14, "1"),       # ttl expired (age 4 > 3)
                 (10, "2"))       # level changed
        for tick, level in cases:
            b1, b2 = self._book(), self._book()
            self.assertFalse(b1.peek_view(tick, level, self._st()).active)
            self.assertFalse(b2.view(tick, level, self._st()).active)

    def test_ttl_equality_is_eligible(self):
        book = self._book()
        # age == ttl (3) is eligible; age > ttl is not
        self.assertTrue(book.peek_view(13, "1", self._st()).active)
        self.assertFalse(book.peek_view(14, "1", self._st()).active)

    def test_precondition_failure_inactive(self):
        book2 = DirectiveBook()
        book2.activate(DirectiveSet(goals=("survive",), ttl=5,
                                    preconditions=("hungry",)), tick=1,
                       level="1")
        self.assertTrue(book2.peek_view(2, "1", self._st(hungry=True)).active)
        self.assertFalse(
            book2.peek_view(2, "1", self._st(hungry=False)).active)

    def test_unknown_level_on_either_side_is_eligible(self):
        # activated_level None, or level None, means "no level check"
        book = DirectiveBook()
        book.activate(DirectiveSet(goals=("survive",), ttl=5), tick=1,
                      level=None)
        self.assertTrue(book.peek_view(2, "2", self._st()).active)
        book2 = DirectiveBook()
        book2.activate(DirectiveSet(goals=("survive",), ttl=5), tick=1,
                       level="1")
        self.assertTrue(book2.peek_view(2, None, self._st()).active)

    def test_no_active_set_is_inactive(self):
        book = DirectiveBook()
        self.assertFalse(book.peek_view(1, "1", self._st()).active)
        self.assertEqual(book.peek_view(1, "1", self._st()).generation, 0)

    def test_peek_view_never_mutates_or_logs_or_sinks(self):
        sink_calls = []
        book = self._book(sink=sink_calls.append)
        sink_calls.clear()          # drop the activation record itself
        events_before = list(book.events)
        gen_before = book.generation
        active_before = book._active
        # an ineligible read (level changed, then ttl expired)
        book.peek_view(10, "2", self._st())
        book.peek_view(99, "1", self._st())
        self.assertEqual(book.events, events_before)
        self.assertEqual(book.generation, gen_before)
        self.assertIs(book._active, active_before)
        self.assertEqual(sink_calls, [])
        self.assertEqual(book.activated_tick, 10)
        self.assertEqual(book.level, "1")
        # the read still reports the set as eligible for its own rules
        self.assertTrue(book.peek_view(11, "1", self._st()).active)

    def test_precedence_is_level_then_ttl(self):
        # level mismatch wins over an also-expired ttl
        book = self._book()
        from tools.agent.directives import _ineligibility_reason
        reason = _ineligibility_reason(book._active, book.activated_tick,
                                       book.level, tick=99, level="2",
                                       st=self._st())
        self.assertEqual(reason, "level-changed")
        reason = _ineligibility_reason(book._active, book.activated_tick,
                                       book.level, tick=99, level="1",
                                       st=self._st())
        self.assertEqual(reason, "ttl-expired")

    def test_view_still_expires_and_logs(self):
        book = self._book()
        self.assertFalse(book.view(99, "1", self._st()).active)
        self.assertIsNone(book._active)
        self.assertTrue(any(e["reason"] == "ttl-expired"
                            for e in book.events))


if __name__ == "__main__":
    unittest.main()
