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
import fcntl
import json
import os
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import format_obs  # noqa: E402
from tools.agent import render, spectating  # noqa: E402
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
    """One full frame, pinned verbatim (map, cursor, status, counters)."""

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


# ==================================================================
# Commit 3 -- bounded transport (spectating.py)
# ==================================================================

class _Clock(object):
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeDest(object):
    """A destination stub: scripted success/failure, records payloads."""

    def __init__(self, tty=False, results=None, partial=3):
        self.tty = tty
        self._results = list(results) if results is not None else None
        self._partial = partial
        self.bytes_written = 0
        self.writes = []
        self.closed = False

    def write(self, data, *, deadline):
        data = bytes(data)
        self.writes.append(data)
        if self._results is None:
            self.bytes_written += len(data)
            return True
        ok = self._results.pop(0) if self._results else True
        if ok:
            self.bytes_written += len(data)
        else:
            self.bytes_written += min(self._partial, len(data))
        return ok

    def close(self):
        self.closed = True


def _stream(interval=0.15, dest=None, clock=None, diagnostic=None):
    clock = clock or _Clock()
    dest = dest or _FakeDest()
    return (spectating.RenderStream(dest, interval, clock=clock,
                                    diagnostic=diagnostic), dest, clock)


class SpectateValidation(unittest.TestCase):
    def test_destinations(self):
        for dest in ("tty", "stderr", "none"):
            self.assertIsNone(spectating.validate_spectate(dest, 0.15))
        self.assertIsNotNone(spectating.validate_spectate("stdout", 0.15))

    def test_interval_bounds(self):
        self.assertIsNone(spectating.validate_spectate("stderr", 0))
        self.assertIsNotNone(spectating.validate_spectate("stderr", -1))
        self.assertIsNotNone(
            spectating.validate_spectate("stderr", float("nan")))
        self.assertIsNotNone(
            spectating.validate_spectate("stderr", float("inf")))
        self.assertIsNotNone(
            spectating.validate_spectate("stderr", float("-inf")))


class RenderStreamThrottle(unittest.TestCase):
    def test_first_attempt_is_immediate(self):
        st, dest, clk = _stream(interval=0.15)
        st.offer(["a"])
        self.assertEqual(st.frames_rendered, 1)
        self.assertEqual(dest.writes, [b"a\n"])
        self.assertEqual(st._last_attempt, 0.0)   # completion, not None

    def test_interval_is_measured_from_completion(self):
        st, dest, clk = _stream(interval=0.15)
        st.offer(["a"])
        clk.advance(0.14)
        st.offer(["b"])
        self.assertEqual(st.frames_rendered, 1)   # not due yet
        self.assertAlmostEqual(st.next_due(), 0.15)
        clk.advance(0.01)
        st.flush()
        self.assertEqual(st.frames_rendered, 2)   # due at equality
        self.assertEqual(dest.writes[-1], b"b\n")

    def test_zero_interval_attempts_each_candidate(self):
        st, dest, clk = _stream(interval=0)
        st.offer(["a"])
        st.offer(["b"])
        self.assertEqual(st.frames_rendered, 2)

    def test_newest_candidate_coalesces(self):
        st, dest, clk = _stream(interval=0.15)
        st.offer(["a"])
        st.offer(["b"])
        st.offer(["c"])
        self.assertEqual(st._coalesced, 1)
        self.assertAlmostEqual(st.next_due(), 0.15)
        clk.advance(0.15)
        st.flush()
        self.assertEqual(dest.writes[-1], b"c\n")

    def test_next_due_none_without_pending(self):
        st, dest, clk = _stream(interval=0.15)
        st.offer(["a"])
        self.assertIsNone(st.next_due())

    def test_finish_forces_once_and_is_then_idle(self):
        st, dest, clk = _stream(interval=5.0)
        st.offer(["a"])
        st.finish(["final"])                     # forced, interval ignored
        self.assertEqual(st.frames_rendered, 2)
        self.assertEqual(dest.writes[-1], b"final\n")
        st.finish()                              # nothing pending: no-op
        self.assertEqual(st.frames_rendered, 2)

    def test_per_episode_reset(self):
        st, dest, clk = _stream(interval=0)
        st.offer(["a"])
        fresh, _, _ = _stream(interval=0)
        self.assertEqual(fresh.frames_rendered, 0)
        self.assertEqual(fresh.frames_dropped, 0)
        self.assertIsNone(fresh.disabled_reason)


class RenderStreamFailure(unittest.TestCase):
    def test_three_consecutive_stalls_disable(self):
        dest = _FakeDest(results=[False, False, False])
        st, _, clk = _stream(interval=0, dest=dest)
        for i in range(3):
            st.offer(["x%d" % i])
        self.assertEqual(st.frames_rendered, 0)
        self.assertEqual(st.frames_dropped, 3)
        self.assertEqual(st.disabled_reason, "write-deadline")
        self.assertIsNone(st.next_due())

    def test_success_resets_the_stall_streak(self):
        dest = _FakeDest(results=[False, True, False, False])
        st, _, clk = _stream(interval=0, dest=dest)
        for i in range(4):
            st.offer(["x%d" % i])
        self.assertEqual(st.frames_rendered, 1)
        self.assertEqual(st.frames_dropped, 3)
        self.assertIsNone(st.disabled_reason)    # never 3 *consecutive*

    def test_coalescing_does_not_reset_the_streak(self):
        dest = _FakeDest(results=[False, False, False])
        st, _, clk = _stream(interval=0.1, dest=dest)
        st.offer(["a"])                          # dropped, streak 1
        clk.advance(0.03)
        st.offer(["b"])                          # pending
        st.offer(["c"])                          # coalesced, not delivered
        clk.advance(0.07)                        # t == 0.10: due
        st.flush()                               # dropped, streak 2
        clk.advance(0.1)                         # t == 0.20: due
        st.offer(["d"])                          # dropped, streak 3
        self.assertEqual(st.disabled_reason, "write-deadline")
        self.assertEqual(st.frames_dropped, 3)

    def test_dropped_frame_is_not_counted_rendered_or_resumed(self):
        dest = _FakeDest(results=[False])
        st, _, clk = _stream(interval=0, dest=dest)
        st.offer(["a"])
        self.assertEqual(st.frames_rendered, 0)
        self.assertEqual(st.frames_dropped, 1)
        self.assertIsNone(st.next_due())         # not resumed
        self.assertEqual(len(dest.writes), 1)

    def test_deadline_cap_drops_without_writing(self):
        st, dest, clk = _stream(interval=10.0)
        st.offer(["a"])                          # first attempt
        clk.advance(0.01)
        st.offer(["b"])                          # pending (not due)
        before = len(dest.writes)
        st.flush(force=True, deadline_cap=clk() - 1.0)
        self.assertEqual(len(dest.writes), before)   # no fresh allowance
        self.assertEqual(st.frames_dropped, 1)

    def test_disable_is_idempotent_and_diagnoses_once(self):
        notes = []
        dest = _FakeDest(results=[False, False, False])
        st, _, clk = _stream(interval=0, dest=dest,
                             diagnostic=notes.append)
        for i in range(3):
            st.offer(["x%d" % i])
        self.assertEqual(st.disabled_reason, "write-deadline")
        st.disable("write-error", "later")
        self.assertEqual(st.disabled_reason, "write-deadline")
        self.assertEqual(len(notes), 1)
        self.assertIn("write-deadline", notes[0])


class RenderStreamTtyTransaction(unittest.TestCase):
    def test_height_commits_only_on_complete_delivery(self):
        dest = _FakeDest(tty=True)
        st, _, clk = _stream(interval=0, dest=dest)
        st.offer(["a", "b"])
        self.assertEqual(st._height, 2)
        st.offer(["c"])
        self.assertTrue(dest.writes[-1].startswith(b"\x1b[2A"))  # up 2
        self.assertNotIn(b"\x1b[2J", dest.writes[-1])

    def test_partial_tty_write_forces_absolute_resync(self):
        dest = _FakeDest(tty=True, results=[False], partial=3)
        st, _, clk = _stream(interval=0, dest=dest)
        st.offer(["a"])
        self.assertTrue(st._pos_unknown)
        st.offer(["b"])
        self.assertTrue(dest.writes[-1].startswith(b"\x1b[2J\x1b[H"))

    def test_zero_bytes_leaves_position_known(self):
        dest = _FakeDest(tty=True, results=[True, False], partial=0)
        st, _, clk = _stream(interval=0, dest=dest)
        st.offer(["a", "b"])                     # complete: height 2
        st.offer(["c"])                          # dropped, zero bytes
        self.assertFalse(st._pos_unknown)
        self.assertEqual(st._height, 2)
        st.offer(["d"])                          # ordinary frame
        self.assertTrue(dest.writes[-1].startswith(b"\x1b[2A"))


class WriteSeams(object):
    """Deterministic select/write seams for RenderDestination.write."""

    def __init__(self, clock, fdval=7, chunk=4, eintr=0, eagain=0,
                 zero=False, advance=0.0):
        self.clock = clock
        self.fdval = fdval
        self.chunk = chunk
        self._eintr = eintr
        self._eagain = eagain
        self.zero = zero
        self.advance = advance
        self.select_calls = 0
        self.write_calls = 0
        self.written = []
        self.view_lengths = []

    def select(self, r, w, x, timeout):
        self.select_calls += 1
        if self._eintr > 0:
            self._eintr -= 1
            raise InterruptedError
        self.clock.t += self.advance
        return ([], [self.fdval], [])

    def write(self, fd, view):
        if self._eagain > 0:
            self._eagain -= 1
            raise BlockingIOError
        if self.zero:
            return 0
        self.view_lengths.append(len(view))
        self.write_calls += 1
        n = min(self.chunk, len(view))
        self.written.append(bytes(view[:n]))
        return n


class BoundedWrites(unittest.TestCase):
    def _dest(self, seams, chunk_limit=4):
        clk = seams.clock
        return spectating.RenderDestination(
            9, owned=True, tty=False, clock=clk, select_fn=seams.select,
            write_fn=seams.write, chunk_limit=chunk_limit)

    def test_chunks_are_at_most_pipe_buf(self):
        clk = _Clock()
        seams = WriteSeams(clk, chunk=4)
        dest = self._dest(seams)
        self.assertTrue(dest.write(b"0123456789", deadline=clk() + 100))
        self.assertEqual(seams.written, [b"0123", b"4567", b"89"])
        self.assertEqual(dest.bytes_written, 10)
        # The destination itself never hands the sink more than the chunk
        # bound (PIPE_BUF by default); the seam here caps at 4.
        self.assertTrue(seams.view_lengths)
        self.assertLessEqual(max(seams.view_lengths), 4)

    def test_select_before_every_write(self):
        clk = _Clock()
        seams = WriteSeams(clk, chunk=4)
        dest = self._dest(seams)
        dest.write(b"0123456789", deadline=clk() + 100)
        self.assertEqual(seams.select_calls, seams.write_calls)

    def test_deadline_exhaustion_returns_false(self):
        clk = _Clock()
        seams = WriteSeams(clk, chunk=4, advance=10.0)
        dest = self._dest(seams)
        self.assertFalse(dest.write(b"0123456789", deadline=clk() + 0.25))
        self.assertEqual(dest.bytes_written, 0)

    def test_eintr_does_not_extend_or_break_the_deadline(self):
        clk = _Clock()
        seams = WriteSeams(clk, chunk=4, eintr=1)
        dest = self._dest(seams)
        self.assertTrue(dest.write(b"0123456789", deadline=clk() + 100))
        self.assertEqual(seams.select_calls, seams.write_calls + 1)
        self.assertEqual(dest.bytes_written, 10)

    def test_eagain_returns_to_select(self):
        clk = _Clock()
        seams = WriteSeams(clk, chunk=4, eagain=1)
        dest = self._dest(seams)
        self.assertTrue(dest.write(b"0123456789", deadline=clk() + 100))
        self.assertEqual(dest.bytes_written, 10)

    def test_zero_write_raises(self):
        clk = _Clock()
        seams = WriteSeams(clk, zero=True)
        dest = self._dest(seams)
        with self.assertRaises(OSError):
            dest.write(b"abc", deadline=clk() + 100)


# ---------------------------------------------- fd lifecycle (subprocesses)

_CHILD_PREAMBLE = r'''
import os, sys, json, socket, fcntl, stat, time
RESULT = os.environ["AUTOSPEC_RESULT"]
sys.path.insert(0, os.environ["AUTOSPEC_ROOT"])
from tools.agent import spectating as S

def emit(obj):
    with open(RESULT, "w") as fh:
        fh.write(json.dumps(obj))

def open_fds():
    return sorted(int(n) for n in os.listdir("/proc/self/fd"))
'''


class FdLifecycle(unittest.TestCase):
    def _run(self, body, pass_fds=(), new_session=False):
        with tempfile.TemporaryDirectory() as td:
            result = os.path.join(td, "r.json")
            env = dict(os.environ)
            env["AUTOSPEC_RESULT"] = result
            env["AUTOSPEC_ROOT"] = ROOT
            code = _CHILD_PREAMBLE + body
            proc = subprocess.run(
                [sys.executable, "-c", code], env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, pass_fds=tuple(pass_fds),
                start_new_session=new_session, timeout=30)
            try:
                with open(result) as fh:
                    return json.load(fh), proc
            except (OSError, ValueError):
                return None, proc

    def test_closed_stdout_relocates_and_restores_fd1(self):
        body = r'''
os.close(1)
before = len(open_fds())
dest = S.open_destination("stderr")
fd = dest.fd
fd1_closed = False
try:
    os.fstat(1)
except OSError:
    fd1_closed = True
dest.write(b"FRAME", deadline=time.monotonic() + 2)
dest.close()
after = len(open_fds())
emit({"fd": fd, "fd1_closed": fd1_closed, "leak": after - before})
'''
        data, proc = self._run(body)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertGreaterEqual(data["fd"], 3)
        self.assertTrue(data["fd1_closed"])
        self.assertEqual(data["leak"], 0)

    def test_socket_alias_is_rejected(self):
        body = r'''
a, b = socket.socketpair()
os.dup2(a.fileno(), 1)
os.dup2(a.fileno(), 2)
try:
    S.open_destination("stderr")
    emit({"rejected": False})
except S.SpectateError:
    emit({"rejected": True})
'''
        data, proc = self._run(body)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertTrue(data["rejected"])

    def test_2_and_1_pipe_alias_is_rejected(self):
        body = r'''
r, w = os.pipe()
os.dup2(w, 1)
os.dup2(w, 2)
try:
    S.open_destination("stderr")
    emit({"rejected": False})
except S.SpectateError:
    emit({"rejected": True})
'''
        data, proc = self._run(body)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertTrue(data["rejected"])

    def test_2_and_1_file_alias_is_rejected(self):
        body = r'''
import tempfile
d = tempfile.mkdtemp()
f = os.open(os.path.join(d, "alias.bin"),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.dup2(f, 1)
os.dup2(f, 2)
try:
    S.open_destination("stderr")
    emit({"rejected": False})
except S.SpectateError:
    emit({"rejected": True})
'''
        data, proc = self._run(body)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertTrue(data["rejected"])

    def test_tty_fallback_notes_and_rejects_alias(self):
        # No controlling terminal (new session): a plain tty open falls back
        # to fd 2 with one note.
        body = r'''
try:
    dest = S.open_destination("tty")
    emit({"note": bool(dest.note), "label": dest.label})
except S.SpectateError as exc:
    emit({"error": str(exc)})
'''
        data, proc = self._run(body, new_session=True)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertTrue(data.get("note"))
        self.assertEqual(data.get("label"), "fd 2")

    def test_tty_fallback_rejects_2_1_alias_before_note(self):
        body = r'''
r, w = os.pipe()
os.dup2(w, 1)
os.dup2(w, 2)
try:
    S.open_destination("tty")
    emit({"rejected": False})
except S.SpectateError:
    emit({"rejected": True})
'''
        data, proc = self._run(body, new_session=True)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertTrue(data["rejected"])

    def test_stderr_uses_fd2_despite_sys_stderr(self):
        read_fd, write_fd = os.pipe()
        body = r'''
os.dup2(%d, 2)
import io
sys.stderr = io.StringIO()
dest = S.open_destination("stderr")
dest.write(b"HELLO", deadline=time.monotonic() + 2)
dest.close()
emit({"stringio": sys.stderr.getvalue()})
''' % (write_fd,)
        data, proc = self._run(body, pass_fds=(write_fd,))
        os.close(write_fd)
        self.assertIsNotNone(data, proc.stderr.decode())
        r, _, _ = select.select([read_fd], [], [], 5)
        got = os.read(read_fd, 4096) if r else b""
        os.close(read_fd)
        self.assertEqual(got, b"HELLO")          # went to fd 2
        self.assertEqual(data["stringio"], "")   # not to sys.stderr

    def test_none_opens_nothing_and_close_leaves_fd2(self):
        body = r'''
before = open_fds()
dest = S.open_destination("none")
fd_none = dest.fd
probe = dest.write(b"ignored", deadline=time.monotonic() + 1)
dest.close()
fd2_ok = True
try:
    os.fstat(2)
except OSError:
    fd2_ok = False
emit({"fd": fd_none, "probe": probe, "fd2_ok": fd2_ok,
      "leak": len(open_fds()) - len(before)})
'''
        data, proc = self._run(body)
        self.assertIsNotNone(data, proc.stderr.decode())
        self.assertIsNone(data["fd"])
        self.assertTrue(data["probe"])
        self.assertTrue(data["fd2_ok"])
        self.assertEqual(data["leak"], 0)


# ==================================================================
# Commit 4 -- controller / CLI integration (isolation)
# ==================================================================

class _PStdout(object):
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd


class _PStdin(object):
    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def write(self, b):
        return os.write(self._fd, b)

    def flush(self):
        pass

    def close(self):
        pass


class _PStderr(object):
    def readline(self, *a):
        return b""


class _PProc(object):
    """A process-shaped peer with real pipes in both directions."""

    def __init__(self, out_r, in_w):
        self.stdout = _PStdout(out_r)
        self.stdin = _PStdin(in_w)
        self.stderr = _PStderr()
        self.returncode = 0
        self.pid = None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def terminate(self):
        pass

    def close(self):
        pass


def _wline(obj):
    return (json.dumps(obj) + "\n").encode()


def _wrow(r, text, selectable=True):
    return {"r": r, "text": text, "selectable": selectable, "key": None,
            "group": None, "initial": None, "style": 0, "color": "none",
            "icon": None}


def _wpage(content, k, pages, rows):
    return {"v": 1, "ch": "control", "type": "page", "d": 1,
            "content": content, "page": k, "pages": pages, "rows": rows}


WHELLO = {"v": 1, "ch": "control", "type": "hello", "d": 1,
          "profile": "normal-ascii-color-v1", "policy": "llm-final-v1",
          "caps": ["snapshot", "menu", "paging"], "coord": "engine-map",
          "size": [80, 21], "x0": 1, "y0": 0,
          "limits": {"line": 65536, "page_bytes": 16384, "page_rows": 128,
                     "count": 2147483647}}
WCLOSED = {"v": 1, "ch": "control", "type": "closed"}


def _wobs(seq, need=None, msg=(), map_=None, cur=None, windows=(), pal=None):
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None, "s": {"time": {"text": str(seq), "color": "none",
                                         "style": 0}},
            "cond": [], "pal": pal or [[0, " ", "none", 0, "none"]],
            "map": map_ or [], "cur": cur, "msg": list(msg), "hist": [],
            "windows": list(windows), "need": need}


WPAL = [[0, " ", "none", 0, "none"], [1, "@", "white", 0, "none"]]


def _chunked_obs_lines(rid, seq, need):
    head = [{"p": "h", "k": "v", "val": 1},
            {"p": "h", "k": "ch", "val": "player"},
            {"p": "h", "k": "type", "val": "obs"},
            {"p": "h", "k": "seq", "val": seq},
            {"p": "h", "k": "base", "val": None}]
    part0 = head + [
        {"p": "s", "k": "time", "val": {"text": str(seq), "color": "none",
                                        "style": 0}},
        {"p": "pal", "val": [0, " ", "none", 0, "none"]},
        {"p": "pal", "val": [1, "@", "white", 0, "none"]},
        {"p": "map", "val": [10, 5, 1]},
        {"p": "cur", "val": [10, 5]}]
    part1 = [{"p": "msg", "val": {"e": 1, "text": "chunked hello",
                                 "style": 0}},
             {"p": "need", "val": need}]
    return [_wline({"v": 1, "ch": "control", "type": "chunk", "d": 1,
                    "rid": rid, "i": 0, "last": False, "parts": part0}),
            _wline({"v": 1, "ch": "control", "type": "chunk", "d": 1,
                    "rid": rid, "i": 1, "last": True, "parts": part1})]


class _ScriptPeer(object):
    """A deterministic bidirectional peer: HELLO, full/chunked obs, a menu
    need with a page, one `invalid` retry, then `closed`."""

    def __init__(self):
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        self.proc = _PProc(self.out_r, self.in_w)
        self.inbound = bytearray()
        self.acts = []
        self.get_pages = []
        self.home = [WHELLO,
                     _wobs(1, {"id": 1, "kind": "command"},
                           msg=[{"e": 1, "text": "Welcome.", "style": 0}],
                           pal=WPAL, map_=[[10, 5, 1]], cur=[10, 5])]
        self.after = [
            _chunked_obs_lines(2, 2, {"id": 2, "kind": "command"}),
            [_wline(_wobs(3, {"id": 3, "kind": "menu", "menu": "m1",
                              "mode": "one", "content": "c1", "pages": 1},
                          windows=[{"w": "w1", "kind": "menu",
                                    "title": "What do you want to eat?",
                                    "content": "c1", "pages": 1}]))],
            [_wline({"v": 1, "ch": "player", "type": "invalid", "d": 3,
                     "code": "kind"})],
        ]
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        w = None
        try:
            w = os.fdopen(self.out_w, "wb", buffering=0)
            for rec in self.home:
                w.write(_wline(rec))
            idx = 0
            buf = b""
            while True:
                chunk = os.read(self.in_r, 4096)
                if not chunk:
                    break
                self.inbound += chunk
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if not raw.strip():
                        continue
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "act":
                        self.acts.append(msg)
                        if idx < len(self.after):
                            for rec in self.after[idx]:
                                w.write(rec)
                            idx += 1
                        else:
                            w.write(_wline(WCLOSED))
                            return
                    elif t == "get_page":
                        self.get_pages.append(msg)
                        w.write(_wline(_wpage(
                            msg["content"], msg["page"], 1,
                            [_wrow(14, "a food ration")])))
        except OSError:
            pass
        finally:
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass

    def join(self):
        self._t.join(timeout=5)

    def close(self):
        for fd in (self.in_w, self.out_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self.join()
        for fd in (self.out_r, self.in_r):
            try:
                os.close(fd)
            except OSError:
                pass


class _Counters(object):
    def __init__(self):
        self.record_event = 0
        self.health = 0
        self.cancel = 0
        self.event_sink = 0


def _read_jsonl(path):
    out = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def _normalize_actions(rows):
    return [{k: v for k, v in r.items() if k != "t"} for r in rows]


def _normalize_decisions(rows):
    return [{k: v for k, v in r.items() if k not in ("t", "latency")}
            for r in rows]


def _normalize_events(rows):
    return [{k: v for k, v in r.items() if k != "wall"} for r in rows]


def _normalize_meta(meta):
    meta = dict(meta)
    meta.pop("spectate_frames_rendered", None)
    meta.pop("spectate_disabled_reason", None)
    return meta


def _run_episode(spectate="none", frame_path=None, inject=None,
                 interval=0.0):
    """Run one deterministic episode; return (result, counters, out_dir)."""
    from tools.agent import controller as C
    from tools.agent.providers import ProviderConfig
    out_dir = tempfile.mkdtemp(prefix="spectate-iso.")
    config = ProviderConfig(reflex="scripted", strategy="off", max_ticks=200)
    ctl = C.Controller(
        config, C.ControllerPaths(worker="w", runner="r", data="d",
                                  sysconf="s"),
        out_dir, episode_timeout=10.0, spectate=spectate,
        spectate_interval=interval)
    counters = _Counters()

    orig_health = C._EpisodeRunner._note_recorder_health
    orig_sink = C._EpisodeRunner._event_sink
    orig_new = C.Controller._new_strategy_provider
    from tools.agent import recording as R

    orig_record_event = R.EpisodeRecorder.record_event

    def health(self):
        counters.health += 1
        return orig_health(self)

    def event_sink(self, rec):
        counters.event_sink += 1
        return orig_sink(self, rec)

    def record_event(self, obj):
        counters.record_event += 1
        return orig_record_event(self, obj)

    def new_provider(self):
        prov = orig_new(self)
        real_cancel = prov.cancel

        def cancel():
            counters.cancel += 1
            return real_cancel()

        try:
            prov.cancel = cancel
        except (AttributeError, TypeError):
            pass
        return prov

    C._EpisodeRunner._note_recorder_health = health
    C._EpisodeRunner._event_sink = event_sink
    R.EpisodeRecorder.record_event = record_event
    C.Controller._new_strategy_provider = new_provider

    restore = []
    if frame_path is not None:
        fd = os.open(frame_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        orig_open = spectating.open_destination

        def fake_open(destination, clock=None, **kw):
            if destination == "none":
                if clock is None:
                    return orig_open(destination, **kw)
                return orig_open(destination, clock=clock, **kw)
            return spectating.RenderDestination(fd, owned=True, tty=False,
                                                label="file")

        spectating.open_destination = fake_open
        restore.append(lambda: setattr(spectating, "open_destination",
                                       orig_open))

    if inject is not None:
        restore.append(inject())

    peer = _ScriptPeer()
    ctl._spawn = lambda priv: peer.proc
    try:
        results = ctl.run_campaign(1)
    finally:
        peer.close()
        for fn in reversed(restore):
            fn()
        C._EpisodeRunner._note_recorder_health = orig_health
        C._EpisodeRunner._event_sink = orig_sink
        R.EpisodeRecorder.record_event = orig_record_event
        C.Controller._new_strategy_provider = orig_new
    with open(os.path.join(out_dir, "stdin.bin"), "wb") as fh:
        fh.write(bytes(peer.inbound))
    return results[0], counters, out_dir


class IntegrationIsolation(unittest.TestCase):
    """none vs spectate must be byte-identical on the wire artifacts."""

    def test_none_and_spectate_artifacts_are_identical(self):
        none_res, none_c, none_dir = _run_episode("none")
        frame_path = os.path.join(none_dir, "frames.txt")
        spec_res, spec_c, spec_dir = _run_episode("stderr",
                                                  frame_path=frame_path)

        # Same success, exit status and outcome.
        self.assertEqual(none_res.stop_reason, spec_res.stop_reason)
        self.assertEqual(none_res.outcome, spec_res.outcome)
        self.assertEqual(none_res.returncode, spec_res.returncode)
        self.assertEqual(none_res.ticks, spec_res.ticks)
        self.assertEqual(none_res.needs, spec_res.needs)
        self.assertEqual(none_res.invalids, spec_res.invalids)
        self.assertEqual(none_res.actions, spec_res.actions)
        self.assertEqual(none_res.budget, spec_res.budget)

        # The wire and the captured outbound stdin bytes are byte-for-byte.
        with open(os.path.join(none_dir, "ep-1.wire.jsonl"), "rb") as fh:
            none_wire = fh.read()
        with open(os.path.join(spec_dir, "ep-1.wire.jsonl"), "rb") as fh:
            spec_wire = fh.read()
        self.assertEqual(none_wire, spec_wire)
        self.assertNotEqual(len(none_wire), 0)

        # The controller's outbound stdin bytes are byte-for-byte too.
        with open(os.path.join(none_dir, "stdin.bin"), "rb") as fh:
            none_out = fh.read()
        with open(os.path.join(spec_dir, "stdin.bin"), "rb") as fh:
            spec_out = fh.read()
        self.assertEqual(none_out, spec_out)
        self.assertNotEqual(len(none_out), 0)

        # Same sidecars after normalizing only the approved timing fields.
        for name, norm, nonempty in (("actions", _normalize_actions, True),
                                     ("decisions", _normalize_decisions,
                                      True),
                                     ("events", _normalize_events, False)):
            a = _read_jsonl(os.path.join(none_dir, "ep-1.%s.jsonl" % name))
            b = _read_jsonl(os.path.join(spec_dir, "ep-1.%s.jsonl" % name))
            self.assertEqual(norm(a), norm(b), name)
            if nonempty:
                self.assertTrue(a, name)

        for tag, d in (("none", none_dir), ("spec", spec_dir)):
            with open(os.path.join(d, "ep-1.meta.json")) as fh:
                meta = json.load(fh)
            if tag == "none":
                self.assertEqual(meta["spectate_frames_rendered"], 0)
                self.assertIsNone(meta["spectate_disabled_reason"])
        with open(os.path.join(none_dir, "ep-1.meta.json")) as fh:
            none_meta = _normalize_meta(json.load(fh))
        with open(os.path.join(spec_dir, "ep-1.meta.json")) as fh:
            spec_meta = _normalize_meta(json.load(fh))
        self.assertEqual(none_meta, spec_meta)

        # Campaign rollup is identical too.
        with open(os.path.join(none_dir, "campaign.json")) as fh:
            none_camp = json.load(fh)
        with open(os.path.join(spec_dir, "campaign.json")) as fh:
            spec_camp = json.load(fh)
        self.assertEqual(none_camp, spec_camp)

        # The spectate run rendered frames to the attached file.
        with open(frame_path, "rb") as fh:
            frames = fh.read()
        self.assertIn(b"auto episode=1", frames)
        self.assertGreaterEqual(spec_res.spectate_frames_rendered, 1)
        self.assertIsNone(spec_res.spectate_disabled_reason)
        # The final frame is a fresh composition (settled counters, resolved
        # stop reason and outcome), never a replay of an observation.
        self.assertIn(b" final stop=", frames)
        self.assertIn(b"outcome=", frames)
        # One offer per accepted marker (plus the final frame): a candidate is
        # never recomposed and re-offered when no new snapshot arrived.
        self.assertLessEqual(spec_res.spectate_frames_rendered, 5)


class HookIsolation(unittest.TestCase):
    """A fault in any render hook never changes the wire outcome."""

    def _framed(self):
        d = tempfile.mkdtemp(prefix="spectate-hook.")
        return os.path.join(d, "f.txt")

    def _inject_and_run(self, inject):
        return _run_episode("stderr", frame_path=self._framed(),
                            inject=inject)

    def _fail(self, target, attr):
        def inject():
            original = getattr(target, attr)

            def boom(*a, **k):
                raise RuntimeError("injected %s" % attr)

            setattr(target, attr, boom)
            return lambda: setattr(target, attr, original)
        return inject

    def test_each_hook_fault_is_isolated(self):
        from tools.agent import controller as C

        clean_res, clean_c, _ = _run_episode("stderr",
                                             frame_path=self._framed())
        injections = [
            self._fail(spectating, "open_destination"),
            self._fail(C, "auto_frame"),
            self._fail(spectating.RenderStream, "offer"),
            self._fail(spectating.RenderStream, "flush"),
            self._fail(spectating.RenderStream, "finish"),
            self._fail(spectating.RenderStream, "close"),
            self._fail(spectating.RenderDestination, "write"),
        ]
        for inject in injections:
            res, counters, _ = self._inject_and_run(inject)
            # The episode is unaffected: same success, outcome, wire counts.
            self.assertEqual(res.stop_reason, clean_res.stop_reason)
            self.assertEqual(res.outcome, clean_res.outcome)
            self.assertEqual(res.actions, clean_res.actions)
            self.assertEqual(res.ticks, clean_res.ticks)
            self.assertTrue(res.recording_complete)
            # The fault never touches recorder health, the event sink or paid
            # work cancellation: those counts match a clean run exactly.
            self.assertEqual(counters.health, clean_c.health)
            self.assertEqual(counters.event_sink, clean_c.event_sink)
            self.assertEqual(counters.record_event, clean_c.record_event)
            self.assertEqual(counters.cancel, clean_c.cancel)
            # The fault disables rendering (recorded honestly in the meta).
            self.assertIsNotNone(res.spectate_disabled_reason)

    def test_open_failure_never_becomes_a_spawn_failure(self):
        def inject():
            original = spectating.open_destination

            def boom(*a, **k):
                raise OSError("injected open failure")

            spectating.open_destination = boom
            return lambda: setattr(spectating, "open_destination", original)

        res, counters, _ = self._inject_and_run(inject)
        self.assertTrue(res.spawn_ok)
        self.assertEqual(res.stop_reason, "closed")
        self.assertEqual(res.spectate_disabled_reason, "open-failed")
        self.assertEqual(res.spectate_frames_rendered, 0)

    def test_close_fault_appears_in_meta(self):
        # A close fault must be recorded in the meta, which is only possible
        # because the guarded close and its stats copy run BEFORE the
        # recording is finalized.
        from tools.agent import controller as C
        res, _, out_dir = self._inject_and_run(
            self._fail(spectating.RenderStream, "close"))
        with open(os.path.join(out_dir, "ep-1.meta.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["spectate_disabled_reason"], "close-error")
        self.assertIn("spectate_frames_rendered", meta)


class ReadlineRenderWake(unittest.TestCase):
    """The _readline select timeout is capped by a due frame."""

    def test_select_timeout_is_capped_by_due_frame(self):
        from tools.agent import controller as C

        class _Stream(object):
            def __init__(self, due):
                self._due = due
                self.flushes = []

            def next_due(self):
                return self._due

            def flush(self, force=False, deadline_cap=None):
                self.flushes.append(deadline_cap)

        runner = object.__new__(C._EpisodeRunner)
        runner.buf = b""
        runner.deadline = time.monotonic() + 5.0
        runner.spectate = _Stream(time.monotonic() + 0.3)
        runner.proc = types.SimpleNamespace(
            stdout=types.SimpleNamespace(fileno=lambda: -1))

        calls = []

        class _Stop(Exception):
            pass

        original = C.select.select

        def fake_select(r, w, x, timeout):
            calls.append(timeout)
            raise _Stop()

        C.select.select = fake_select
        try:
            with self.assertRaises(_Stop):
                runner._readline(None)
        finally:
            C.select.select = original
        self.assertTrue(calls)
        # A due render wake caps the wait well below the 1.0s wire tick.
        self.assertLess(calls[0], 0.9)


class ComparatorNormalizer(unittest.TestCase):
    """The comparator normalizes ONLY the approved timing fields."""

    def test_approved_timing_fields_are_dropped(self):
        row = {"schema": 1, "ordinal": 2, "kind": "act", "t": 0.5}
        self.assertNotIn("t", _normalize_actions([row])[0])
        dec = {"schema": 1, "reason": "x", "latency": 0.1, "t": 0.2}
        self.assertNotIn("latency", _normalize_decisions([dec])[0])
        self.assertNotIn("t", _normalize_decisions([dec])[0])
        ev = {"schema": 1, "state": "queued", "wall": 1.0}
        self.assertNotIn("wall", _normalize_events([ev])[0])

    def test_semantic_fields_survive_normalization(self):
        # A difference in a *semantic* field must remain visible to the
        # comparator; broadening the normalizer to hide it is a mutation the
        # suite rejects.
        a = {"schema": 1, "ordinal": 1, "kind": "act", "reason": "r1",
             "status": "sent"}
        b = dict(a, kind="get_page", status="write-failed")
        self.assertNotEqual(_normalize_actions([a]), _normalize_actions([b]))
        d1 = {"schema": 1, "reason": "validated", "directives": [{"x": 1}]}
        d2 = {"schema": 1, "reason": "fallback", "directives": []}
        self.assertNotEqual(_normalize_decisions([d1]),
                            _normalize_decisions([d2]))


class MultiEpisodeIsolation(unittest.TestCase):
    def test_renderer_restored_per_episode_and_none_is_zero_null(self):
        from tools.agent import controller as C
        from tools.agent.providers import ProviderConfig
        out_dir = tempfile.mkdtemp(prefix="spectate-multi.")
        config = ProviderConfig(reflex="scripted", strategy="off",
                                max_ticks=200)
        ctl = C.Controller(
            config, C.ControllerPaths(worker="w", runner="r", data="d",
                                      sysconf="s"),
            out_dir, episode_timeout=10.0, spectate="none")
        peers = []

        def spawn(priv):
            peer = _ScriptPeer()
            peers.append(peer)
            return peer.proc

        ctl._spawn = spawn
        try:
            results = ctl.run_campaign(2)
        finally:
            for peer in peers:
                peer.close()
        self.assertEqual(len(results), 2)
        for res in results:
            self.assertEqual(res.spectate_frames_rendered, 0)
            self.assertIsNone(res.spectate_disabled_reason)
        for i in (1, 2):
            with open(os.path.join(out_dir, "ep-%d.meta.json" % i)) as fh:
                meta = json.load(fh)
            self.assertEqual(meta["spectate_frames_rendered"], 0)
            self.assertIsNone(meta["spectate_disabled_reason"])


if __name__ == "__main__":
    unittest.main()

