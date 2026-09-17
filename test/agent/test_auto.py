#!/usr/bin/env python3
"""Unit and integration tests for the tools/agent autoplay harness.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

The integration tests drive the real controller
(:class:`tools.agent.controller.Controller`) against an in-memory fake wire
(a pipe the controller reads exactly as it would a launcher), so the whole
request state machine -- pages, chunks, invalid recovery, retry caps,
EOF/closed, session validation -- is exercised without spawning a game.

The transport tests additionally drive a *bidirectional* fake peer (a real
pipe pair) so the deadline-governed write path, the single-in-flight page
obligation and the closure-honesty rules are tested end to end.
"""

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (controller, policy, protocol,  # noqa: E402
                         recording, state)
from tools.agent import __main__ as agent_main  # noqa: E402
from tools.agent.providers import (ProviderConfig,  # noqa: E402
                                   ReflexContext, ReflexResult)


# ------------------------------------------------------------------ fake wire

class _FakeStdin(object):
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, b):
        self.data += b
        return len(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FdStdin(object):
    """A stdin-shaped object backed by a real descriptor."""

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


class _DeadStdin(object):
    """A stdin whose peer end is already closed: writes raise EPIPE."""

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def flush(self):
        pass

    def close(self):
        pass


class _FakeStdout(object):
    def __init__(self, rfd):
        self._fd = rfd

    def fileno(self):
        return self._fd


class _FakeStderr(object):
    def readline(self, *a):
        return b""


class FakeProc(object):
    """A process-shaped object whose stdout is a real pipe the controller
    reads with select()+os.read(), exactly as it reads a launcher."""

    def __init__(self, scenario: bytes, eof: bool = True):
        self.r, self.w = os.pipe()
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(self.r)
        self.stderr = _FakeStderr()
        self.returncode = 0
        self.killed = False
        self._t = threading.Thread(target=self._feed, args=(scenario, eof),
                                   daemon=True)
        self._t.start()

    def _feed(self, scenario, eof):
        try:
            self.wr = os.fdopen(self.w, "wb", buffering=0)
            self.wr.write(scenario)
            if eof:
                self.wr.close()
        except OSError:
            pass

    def wait(self, timeout=None):
        self._t.join(timeout=timeout or 5)
        return self.returncode

    def kill(self):
        self.killed = True
        self.close()

    def terminate(self):
        self.killed = True

    def close(self):
        try:
            self.wr.close()
        except Exception:
            pass
        try:
            os.close(self.r)
        except OSError:
            pass


class _PeerProc(object):
    """A process-shaped peer with real pipes in both directions."""

    def __init__(self, out_r, in_w):
        self.stdout = _FakeStdout(out_r)
        self.stdin = _FdStdin(in_w)
        self.stderr = _FakeStderr()
        self.returncode = 0
        self.pid = None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass

    def close(self):
        pass


class WirePeer(object):
    """A bidirectional fake peer that answers get_page requests on demand.

    It records every get_page/act it receives, so a test can prove the
    controller keeps at most one page request outstanding at a time.
    """

    def __init__(self, pages, content="c9", menu="m9", rid=None):
        self.pages = pages
        self.content = content
        self.menu = menu
        self.rid = rid
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        os.set_blocking(self.in_w, False)
        self.get_pages = []
        self.acts = []
        self.violations = 0
        self._outstanding = 0
        self.closed_sent = False
        self.proc = _PeerProc(self.out_r, self.in_w)
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def obs_line(self):
        need = {"id": 9, "kind": "menu", "menu": self.menu, "mode": "one",
                "content": self.content, "pages": self.pages}
        return obs(1, need, windows=[
            {"w": "w9", "kind": "menu", "title": "Pick a role or profession",
             "content": self.content, "pages": self.pages}])

    def _rows(self, k):
        if k == 0:
            return [row(14, "a Valkyrie"), row(3, "an Archeologist")]
        return [row(100 + k, "filler row %d" % k)]

    def _serve(self):
        try:
            with os.fdopen(self.out_w, "wb", buffering=0) as w:
                w.write(_line(HELLO))
                w.write(_line(self.obs_line()))
                buf = b""
                while True:
                    try:
                        chunk = os.read(self.in_r, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        if raw.strip():
                            self._handle(json.loads(raw), w)
        except OSError:
            pass

    def _handle(self, msg, w):
        t = msg.get("type")
        if t == "get_page":
            if self._outstanding:
                self.violations += 1
            self._outstanding = 1
            self.get_pages.append(msg["page"])
            w.write(_line(page(self.content, msg["page"], self.pages,
                               self._rows(msg["page"]))))
            self._outstanding = 0
        elif t == "act":
            self.acts.append(msg)
            if not self.closed_sent:
                w.write(_line(CLOSED))
                self.closed_sent = True

    def join(self):
        self._t.join(timeout=5)

    def close(self):
        # signal EOF to the peer first so its reader thread can exit
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


class SilentPeer(object):
    """Writes a preamble and then neither reads nor writes again."""

    def __init__(self, preamble: bytes):
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        self.proc = _PeerProc(self.out_r, self.in_w)
        os.write(self.out_w, preamble)

    def read_requests(self):
        os.set_blocking(self.in_r, False)
        out = b""
        while True:
            try:
                chunk = os.read(self.in_r, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        return [json.loads(x) for x in out.split(b"\n") if x.strip()]

    def close(self):
        for fd in (self.out_r, self.out_w, self.in_r, self.in_w):
            try:
                os.close(fd)
            except OSError:
                pass


class WireHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="auto-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _controller(self, timeout=10.0, max_ticks=200, config=None):
        config = config or ProviderConfig(max_ticks=max_ticks)
        return controller.Controller(
            config, controller.ControllerPaths(
                worker="w", runner="r", data="d", sysconf="s"),
            self.dir, episode_timeout=timeout)

    def run_scenario(self, scenario: bytes, eof: bool = True,
                     timeout: float = 10.0, max_ticks: int = 200):
        ctl = self._controller(timeout=timeout, max_ticks=max_ticks)
        proc = FakeProc(scenario, eof=eof)
        ctl._spawn = lambda priv: proc  # deterministic: no real subprocess
        try:
            result = ctl.run_episode(1)
            actions = _parse_actions(proc.stdin.data)
        finally:
            proc.close()
        return result, actions

    def run_proc(self, proc, timeout=10.0, max_ticks=200, config=None,
                 episodes=1):
        ctl = self._controller(timeout=timeout, max_ticks=max_ticks,
                               config=config)
        ctl._spawn = lambda priv: proc
        return ctl.run_campaign(episodes)


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _parse_actions(data: bytes):
    out = []
    for raw in data.split(b"\n"):
        if raw.strip():
            out.append(json.loads(raw))
    return out


def _read_jsonl(path):
    out = []
    with open(path) as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


BLANK = [[0, " ", "none", 0, "none"]]


def obs(seq, need=None, msg=(), map_=None, pal=None, cur=None, windows=()):
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None, "s": {}, "cond": [], "pal": pal or BLANK,
            "map": map_ or [], "cur": cur, "msg": list(msg), "hist": [],
            "windows": list(windows), "need": need}


def yn_need(i, prompt="Continue?", choices=None, default=None):
    return {"id": i, "kind": "yn", "prompt": prompt, "choices": choices,
            "default": default, "numeric": False}


def menu_need(i, mid, content, pages=1):
    return {"id": i, "kind": "menu", "menu": mid, "mode": "one",
            "content": content, "pages": pages}


def obs_menu(seq, i, mid, content, title, pages=1):
    return obs(seq, menu_need(i, mid, content, pages),
               windows=[{"w": "w%d" % i, "kind": "menu", "title": title,
                         "content": content, "pages": pages}])


def ack_need(i, content, pages=1):
    return {"id": i, "kind": "ack", "content": content, "pages": pages}


def page(content, k, pages, rows):
    return {"v": 1, "ch": "control", "type": "page", "d": 1,
            "content": content, "page": k, "pages": pages, "rows": rows}


def row(r, text, selectable=True):
    return {"r": r, "text": text, "selectable": selectable, "key": None,
            "group": None, "initial": None, "style": 0, "color": "none",
            "icon": None}


HELLO = {"v": 1, "ch": "control", "type": "hello", "d": 1,
         "profile": "normal-ascii-color-v1", "policy": "llm-final-v1",
         "caps": ["snapshot", "menu", "paging"], "coord": "engine-map",
         "size": [80, 21], "x0": 1, "y0": 0,
         "limits": {"line": 65536, "page_bytes": 16384, "page_rows": 128,
                    "count": 2147483647}}


def hello(**over):
    h = dict(HELLO)
    h.update(over)
    return h


CLOSED = {"v": 1, "ch": "control", "type": "closed"}


# ============================================================ unit tests

class TestValidation(unittest.TestCase):
    def test_every_need_kind_accepts_its_shape(self):
        cases = [
            ({"kind": "command"}, {"key": 46}),
            ({"kind": "key"}, {"key": 27}),
            ({"kind": "direction"}, {"key": 104}),
            ({"kind": "position"}, {"position": [10, 5], "mod": 0}),
            ({"kind": "position"}, {"key": 27}),
            ({"kind": "yn"}, {"yn": 110}),
            ({"kind": "yn"}, {"yn": 100, "count": 3}),
            ({"kind": "line", "max": 10}, {"text": "hi"}),
            ({"kind": "line", "max": 10}, {"cancel": True}),
            ({"kind": "extcmd", "max": 10}, {"text": "quit"}),
            ({"kind": "menu", "menu": "m1"}, {"menu": "m1",
                                              "commit": [[2, -1]]}),
            ({"kind": "menu", "menu": "m1"}, {"cancel": True}),
            ({"kind": "menu", "menu": "m1"}, {"ack": True}),
            ({"kind": "ack"}, {"ack": True}),
            ({"kind": "ack"}, {"cancel": True}),
        ]
        for need, action in cases:
            self.assertIsNone(protocol.validate_action(need, action),
                              "%s %s" % (need, action))

    def test_kind_mismatches_rejected(self):
        self.assertIsNotNone(protocol.validate_action({"kind": "yn"},
                                                      {"key": 100}))
        self.assertIsNotNone(protocol.validate_action({"kind": "command"},
                                                      {"yn": 100}))
        self.assertIsNotNone(protocol.validate_action({"kind": "menu",
                                                       "menu": "m1"},
                                                      {"key": 100}))
        # a stale menu generation is a shape error
        self.assertIsNotNone(protocol.validate_action({"kind": "menu",
                                                       "menu": "m2"},
                                                      {"menu": "m1",
                                                       "commit": [[1, -1]]}))

    def test_range_and_shape_rejections(self):
        self.assertIsNotNone(protocol.validate_action({"kind": "yn"},
                                                      {"yn": 0}))
        self.assertIsNotNone(protocol.validate_action({"kind": "yn"},
                                                      {"yn": 256}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "position"}, {"position": [0, 5], "mod": 0}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "position"}, {"position": [10, 5], "mod": 1}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": 3}, {"text": "toolong"}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "menu", "menu": "m1"}, {"menu": "m1",
                                             "commit": [[1, 0]]}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "yn"}, {"yn": 100, "key": 100}))


class TestHelloValidation(unittest.TestCase):
    def test_compatible_hello_accepted(self):
        self.assertIsNone(protocol.validate_hello(HELLO))

    def test_incompatible_hello_rejected(self):
        self.assertIsNotNone(protocol.validate_hello(hello(profile="other")))
        self.assertIsNotNone(protocol.validate_hello(hello(policy="other")))
        self.assertIsNotNone(protocol.validate_hello(hello(coord="screen")))
        self.assertIsNotNone(protocol.validate_hello(hello(caps=["menu"])))
        self.assertIsNotNone(protocol.validate_hello(hello(v=2)))


class TestSnapshot(unittest.TestCase):
    def test_full_snapshot_clears_previous_cells(self):
        snap = protocol.Snapshot()
        pal = [[0, " ", "none", 0, "none"], [1, "#", "gray", 0, "none"],
               [2, "@", "white", 0, "none"]]
        snap.apply(obs(1, map_=[[1, 0, 1], [2, 0, 1], [3, 0, 2]], pal=pal,
                       cur=[3, 0]))
        self.assertEqual(len(snap.map), 3)
        # the second snapshot omits (1,0) and (2,0): a full snapshot replaces,
        # it does not accumulate
        snap.apply(obs(2, map_=[[3, 0, 2]], pal=pal, cur=[3, 0]))
        self.assertEqual(set(snap.map.keys()), {(3, 0)})

    def test_blank_and_cursor_validation(self):
        snap = protocol.Snapshot()
        with self.assertRaises(protocol.ProtocolError):
            snap.apply(obs(1, map_=[[1, 0, 0]]))  # blank must be omitted
        with self.assertRaises(protocol.ProtocolError):
            snap.apply(obs(1, map_=[[1, 0, 9]]))  # undefined palette id


class TestRequestObligations(unittest.TestCase):
    def test_two_page_menu_requires_both_pages(self):
        req = protocol.Request()
        req.begin(menu_need(5, "m5", "c5", pages=2), 1)
        self.assertFalse(req.pages_complete())
        # exactly one page request is available at a time
        preq = req.next_page_request()
        self.assertEqual(preq["page"], 0)
        req.mark_page_requested(0)
        self.assertIsNone(req.next_page_request())  # 0 is in flight
        req.note_page(page("c5", 0, 2, [row(1, "a")]))
        self.assertFalse(req.pages_complete())
        preq = req.next_page_request()
        self.assertEqual(preq["page"], 1)
        req.mark_page_requested(1)
        req.note_page(page("c5", 1, 2, [row(2, "b")]))
        self.assertTrue(req.pages_complete())
        self.assertEqual([r["r"] for r in req.page_rows()], [1, 2])

    def test_page_request_not_marked_before_send(self):
        req = protocol.Request()
        req.begin(menu_need(5, "m5", "c5", pages=1), 1)
        # never mark_page_requested: the request stays owed
        self.assertEqual(req.next_page_request()["page"], 0)
        self.assertEqual(req.requested, set())

    def test_page_out_of_content_ignored(self):
        req = protocol.Request()
        req.begin(menu_need(5, "m5", "c5", pages=1), 1)
        req.note_page(page("cX", 0, 1, [row(1, "a")]))
        self.assertFalse(req.pages_complete())


class TestScriptedReflex(unittest.TestCase):
    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        self.mem = state.EpisodeMemory()

    def ctx(self, need, pages=(), msg=(), tick=0, title=None):
        for m in msg:
            self.mem.messages.append(m)
        snap = protocol.Snapshot()
        if title is not None and need.get("content"):
            snap.windows = {need["content"]: {
                "w": "w1", "kind": "menu", "title": title,
                "content": need["content"], "pages": need.get("pages", 1)}}
        return ReflexContext(
            episode=1, tick=tick, need=need,
            need_key=protocol.NeedKey(1, tick, need.get("id")),
            snapshot=snap, pages=list(pages), memory=self.mem, deadline=0.0)

    def test_every_kind_yields_a_valid_action(self):
        cases = [
            ({"kind": "command", "id": 1}, ()),
            ({"kind": "key", "id": 1}, ()),
            ({"kind": "direction", "id": 1}, ()),
            ({"kind": "position", "id": 1, "x0": 1, "y0": 0, "x1": 79,
              "y1": 20}, ()),
            (yn_need(1, "Really quit without saving?", choices="yn"), ()),
            (yn_need(1, "Do you want your possessions identified?"
                        " [ynq]"), ()),
            ({"kind": "line", "id": 1, "max": 100}, ()),
            ({"kind": "extcmd", "id": 1, "max": 100}, ()),
            (menu_need(1, "m1", "c1"), (row(1, "Yes; start game"),)),
            (ack_need(1, "c1"), ()),
        ]
        for need, pages in cases:
            res = self.ref.decide(self.ctx(need, pages,
                                           title="Is this ok? [ynq]"))
            self.assertIsNone(protocol.validate_action(need, res.action),
                              "kind %s gave %s" % (need["kind"], res.action))

    def test_tutorial_declined_and_role_chosen(self):
        rows = [row(1, "Yes, do a tutorial"), row(2, "No, just start play")]
        need = menu_need(1, "m1", "c1")
        res = self.ref.decide(self.ctx(need, rows,
                                       title="Do you want a tutorial?"))
        self.assertEqual(res.action["commit"], [[2, -1]])
        self.assertTrue(self.ref.selection_done)
        rows2 = [row(3, "an Archeologist"), row(14, "a Valkyrie")]
        res2 = self.ref.decide(self.ctx(menu_need(2, "m2", "c2"), rows2,
                                        title="Pick a role or profession"))
        self.assertEqual(res2.action["commit"], [[14, -1]])

    def test_shall_i_pick_declined(self):
        res = self.ref.decide(self.ctx(yn_need(1, "Shall I pick character's"
                                                   " race...? [ynaq]")))
        self.assertEqual(res.action, {"yn": protocol.KEY_N})

    def test_eat_prompt_uses_a_cached_safe_letter(self):
        self.ref.intent = "eat"
        self.mem.inventory.refresh([row(0, "d - a food ration")], 0, 0)
        need = yn_need(1, "What do you want to eat? [d or ?*]")
        res = self.ref.decide(self.ctx(need))
        self.assertEqual(res.action, {"yn": ord("d")})

    def test_eat_prompt_without_a_cache_opens_the_menu(self):
        self.ref.intent = "eat"
        need = yn_need(1, "What do you want to eat? [d or ?*]")
        res = self.ref.decide(self.ctx(need))
        self.assertEqual(res.action, {"yn": ord("*")})

    def test_eat_loop_breaker_after_two_rejections(self):
        self.ref.intent = "eat"
        need = yn_need(1, "What do you want to eat? [d or ?*]")
        ctx = self.ctx(need, msg=["You don't have that object.",
                                  "You don't have that object."])
        res = self.ref.decide(ctx)
        self.assertEqual(res.action, {"yn": ord("*")})  # open the menu

    def test_search_loop_breaker_never_steps_into_a_monster(self):
        hero = (10, 10)
        self.mem.hero = hero
        self.mem.grid[(10, 10)] = ("@", "white", 0, "none")
        self.mem.grid[(9, 10)] = ('|', "gray", 0, "none")
        self.mem.no_progress = 3
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})
        # a monster blocks the only route: never walk into it
        self.mem.grid[(9, 10)] = ("d", "white", 32, "none")
        self.mem.no_progress = 12
        res = self.ref.decide(self.ctx({"kind": "command", "id": 3}))
        self.assertNotEqual(res.action["key"], protocol.DIR_KEYS[(-1, 0)])
        self.assertIn(res.action["key"], (protocol.KEY_SEARCH,
                                          protocol.KEY_WAIT))

    def test_fresh_instance_resets_per_episode(self):
        self.ref.selection_done = True
        self.ref.intent = "eat"
        fresh = policy.ScriptedReflex(ProviderConfig())
        self.assertFalse(fresh.selection_done)
        self.assertEqual(fresh.intent, "")


class TestScriptedReflexSafety(unittest.TestCase):
    """Medium 6: each documented safety rule has a focused assertion."""

    def setUp(self):
        self.ref = policy.ScriptedReflex(ProviderConfig(role="Valkyrie"))
        self.mem = state.EpisodeMemory()

    def ctx(self, need, pages=(), title=None, tick=0):
        snap = protocol.Snapshot()
        if title is not None and need.get("content"):
            snap.windows = {need["content"]: {
                "w": "w1", "kind": "menu", "title": title,
                "content": need["content"], "pages": need.get("pages", 1)}}
        return ReflexContext(
            episode=1, tick=tick, need=need,
            need_key=protocol.NeedKey(1, tick, need.get("id")),
            snapshot=snap, pages=list(pages), memory=self.mem, deadline=0.0)

    def _hero(self, x=10, y=10):
        self.mem.hero = (x, y)
        self.mem.grid[(x, y)] = ("@", "white", 0, "none")

    def test_unrecognised_menu_is_cancelled(self):
        rows = [row(1, "Yes, do a tutorial"), row(2, "No")]
        res = self.ref.decide(self.ctx(menu_need(1, "m1", "c1"), rows,
                                       title="Something unfamiliar"))
        self.assertEqual(res.action, {"cancel": True})

    def test_menu_without_a_matching_row_is_cancelled(self):
        rows = [row(3, "an Archeologist"), row(4, "a Tourist")]
        res = self.ref.decide(self.ctx(menu_need(1, "m1", "c1"), rows,
                                       title="Pick a role or profession"))
        self.assertEqual(res.action, {"cancel": True})

    def test_low_hp_escapes_away_from_an_adjacent_monster(self):
        self._hero(10, 10)
        self.mem.grid[(11, 10)] = ("d", "white", 0, "none")
        self.mem.grid[(9, 10)] = (".", "gray", 0, "none")
        self.mem.status.hp = 3
        self.mem.status.hp_max = 20
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        # retreat is away from the monster at (11,10) -> west
        self.assertEqual(res.action, {"key": protocol.KEY_H})

    def test_stuck_beside_a_hostile_or_a_pet_never_attacks(self):
        for glyph in ("d", "F", "@"):  # pet-ish, hostile, and non-monster
            self.mem = state.EpisodeMemory()
            self._hero(10, 10)
            if glyph != "@":
                self.mem.grid[(9, 10)] = (glyph, "white", 0, "none")
            self.mem.no_progress = 20
            self.mem.status.hp = 20
            self.mem.status.hp_max = 20
            res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
            self.assertNotEqual(
                res.action.get("key"), protocol.DIR_KEYS[(-1, 0)],
                "glyph %r must not be walked into" % glyph)

    def test_unknown_blanks_are_not_walked_into(self):
        self._hero(10, 10)
        # no known floor around the hero: the fallback must wait, not wander
        key, why = self.ref._random_move(self.mem, (10, 10))
        self.assertEqual(key, protocol.KEY_WAIT)
        self.assertIn("no known floor", why)

    def test_rest_is_not_chosen_beside_a_monster(self):
        self._hero(10, 10)
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        self.mem.grid[(11, 10)] = ("d", "white", 0, "none")
        self.mem.no_progress = 20
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertNotEqual(res.action, {"key": protocol.KEY_WAIT})

    def test_corpse_is_not_chosen_when_a_ration_is_available(self):
        self.ref.intent = "eat"
        rows = [row(7, "a kobold corpse"), row(2, "a food ration")]
        res = self.ref.decide(
            self.ctx(menu_need(1, "m1", "c1"), rows,
                     title="What do you want to eat?"))
        self.assertEqual(res.action["commit"], [[2, -1]])

    def test_inventory_cache_is_refreshed_when_stale(self):
        self._hero(10, 10)
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        self.mem.inventory.refresh([row(0, "d - a food ration")], 0, 0)
        self.assertFalse(self.mem.inventory.stale(0, 240))
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1},
                                       tick=500))
        self.assertEqual(res.action, {"key": protocol.KEY_INV})

    def test_open_eat_then_commit_a_non_first_safe_food_row(self):
        # Low 9: row-model coverage.  The eat prompt is answered with '*'
        # (open the menu), then a *current*, non-first known-safe food row is
        # committed -- parsed through the real row adapter and validated.
        self.ref.intent = "eat"
        first = self.ref.decide(
            self.ctx(yn_need(1, "What do you want to eat? [d or ?*]")))
        self.assertEqual(first.action, {"yn": ord("*")})
        rows = [row(7, "a kobold corpse"), row(2, "a food ration"),
                row(9, "a banana")]
        need = menu_need(2, "m2", "c2")
        second = self.ref.decide(self.ctx(need, rows,
                                          title="What do you want to eat?"))
        self.assertEqual(second.action["commit"], [[2, -1]])
        self.assertIsNone(protocol.validate_action(need, second.action))


# ====================================================== integration tests

class TestController(WireHarness):
    def test_startup_selection_and_quit(self):
        rows_role = [row(3, "an Archeologist"), row(14, "a Valkyrie")]
        rows_tut = [row(1, "Yes, do a tutorial"),
                    row(2, "No, just start play")]
        scen = b"".join([
            _line(HELLO),
            _line(obs(1, yn_need(1, "Shall I pick character's race...?"
                                    " [ynaq]"))),
            _line(obs_menu(2, 2, "m2", "c2", "Pick a role or profession")),
            _line(page("c2", 0, 1, rows_role)),
            _line(obs_menu(3, 3, "m3", "c3", "Do you want a tutorial?")),
            _line(page("c3", 0, 1, rows_tut)),
            _line(obs(4, {"kind": "command", "id": 4})),
            _line(CLOSED),
        ])
        result, actions = self.run_scenario(scen)
        self.assertTrue(result.closed)
        self.assertEqual(result.invalids, 0)
        self.assertEqual(result.stop_reason, "closed")
        # the ref declined auto-pick, chose Valkyrie, declined the tutorial
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertIn({"v": 1, "type": "act", "id": 1, "seq": 1,
                       "action": {"yn": protocol.KEY_N}}, acts)
        self.assertTrue(any(a["action"].get("commit") == [[14, -1]]
                            for a in acts))
        self.assertTrue(any(a["action"].get("commit") == [[2, -1]]
                            for a in acts))

    def test_two_page_menu_and_ack(self):
        rows0 = [row(1, "heading", selectable=False),
                 row(14, "a Valkyrie")]
        rows1 = [row(3, "a Tourist")]
        scen = b"".join([
            _line(HELLO),
            _line(obs_menu(1, 1, "m1", "c1", "Pick a role or profession",
                           pages=2)),
            _line(page("c1", 0, 2, rows0)),
            _line(page("c1", 1, 2, rows1)),
            _line(obs(2, ack_need(2, "c2", pages=1))),
            _line(page("c2", 0, 1, [row(1, "text")])),
            _line(CLOSED),
        ])
        result, actions = self.run_scenario(scen)
        gets = [a for a in actions if a.get("type") == "get_page"
                and a["content"] == "c1"]
        self.assertEqual([g["page"] for g in gets], [0, 1])
        commits = [a for a in actions
                   if a.get("action", {}).get("commit")]
        self.assertEqual(commits[0]["action"]["commit"], [[14, -1]])
        self.assertTrue(any(a.get("action", {}).get("ack") for a in actions))

    def test_chunked_snapshot_is_assembled_and_acked(self):
        parts = [
            [{"p": "h", "k": "v", "val": 1},
             {"p": "h", "k": "ch", "val": "player"},
             {"p": "h", "k": "type", "val": "obs"},
             {"p": "h", "k": "seq", "val": 1},
             {"p": "h", "k": "base", "val": None},
             {"p": "pal", "val": [0, " ", "none", 0, "none"]}],
            [{"p": "need", "val": {"id": 1, "kind": "command"}}],
        ]
        chunk0 = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": 7,
                  "i": 0, "last": False, "parts": parts[0]}
        chunk1 = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": 7,
                  "i": 1, "last": True, "parts": parts[1]}
        scen = b"".join([_line(HELLO), _line(chunk0), _line(chunk1),
                         _line(CLOSED)])
        result, actions = self.run_scenario(scen)
        self.assertTrue(result.closed)
        self.assertGreaterEqual(result.needs, 1)
        acks = [a for a in actions if a.get("type") == "ack_chunk"]
        self.assertEqual([(a["rid"], a["i"]) for a in acks], [(7, 0), (7, 1)])

    def test_same_id_invalid_recovery(self):
        scen = b"".join([
            _line(HELLO),
            _line(obs(1, yn_need(5, "Do you want to continue? [yn]"))),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 1,
                   "code": "range"}),
            _line(obs(2, {"kind": "command", "id": 6})),
            _line(CLOSED),
        ])
        result, actions = self.run_scenario(scen)
        acts = [a for a in actions if a.get("type") == "act"]
        # the same id 5 is answered twice (the retry preserves the id)
        ids = [a["id"] for a in acts]
        self.assertEqual(ids.count(5), 2)
        self.assertEqual(result.invalids, 1)

    def test_fresh_menu_mapping_on_reissue(self):
        rows = [row(14, "a Valkyrie")]
        scen = b"".join([
            _line(HELLO),
            _line(obs_menu(1, 10, "m10", "c10", "Pick a role or profession")),
            _line(page("c10", 0, 1, rows)),
            _line(obs_menu(2, 11, "m11", "c11", "Pick a role or profession")),
            _line(page("c11", 0, 1, rows)),
            _line(CLOSED),
        ])
        result, actions = self.run_scenario(scen)
        commits = [a["action"]["menu"] for a in actions
                   if a.get("action", {}).get("commit")]
        self.assertEqual(commits, ["m10", "m11"])

    def test_eof_without_closed_is_transport_failure(self):
        scen = b"".join([_line(HELLO),
                         _line(obs(1, {"kind": "command", "id": 1}))])
        result, _ = self.run_scenario(scen, eof=True)
        self.assertFalse(result.closed)
        self.assertTrue(result.eof)
        self.assertEqual(result.stop_reason, "transport-failure-eof")
        self.assertFalse(agent_main.episode_ok(result))

    def test_retry_cap_stops_after_repeated_invalids(self):
        # an odd kind keeps getting invalid(kind): the retry cap must stop
        scen = b"".join([
            _line(HELLO),
            _line(obs(1, {"kind": "command", "id": 1})),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 1,
                   "code": "kind"}),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 2,
                   "code": "kind"}),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 3,
                   "code": "kind"}),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 4,
                   "code": "kind"}),
            _line({"v": 1, "ch": "control", "type": "invalid", "d": 5,
                   "code": "kind"}),
        ], )
        result, _ = self.run_scenario(scen, eof=False, timeout=5.0)
        self.assertIsNotNone(result.protocol_failure)
        self.assertEqual(result.stop_reason, "protocol-failure")


class TestSessionValidation(WireHarness):
    """Medium 3: bounded session validation terminates one episode only."""

    def _scen(self, *records):
        return b"".join(_line(r) for r in records)

    def test_missing_hello_is_a_protocol_failure(self):
        result, _ = self.run_scenario(
            self._scen(obs(1, {"kind": "command", "id": 1}), CLOSED))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("before hello", result.protocol_failure)
        self.assertEqual(result.stop_reason, "protocol-failure")

    def test_duplicate_hello_is_a_protocol_failure(self):
        result, _ = self.run_scenario(
            self._scen(HELLO, HELLO, obs(1, {"kind": "command", "id": 1})))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("duplicate hello", result.protocol_failure)

    def test_wrong_profile_hello_is_a_protocol_failure(self):
        result, _ = self.run_scenario(
            self._scen(hello(profile="other"),
                       obs(1, {"kind": "command", "id": 1})))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("incompatible", result.protocol_failure)

    def test_repeated_seq_is_a_protocol_failure(self):
        result, _ = self.run_scenario(
            self._scen(HELLO, obs(1, {"kind": "command", "id": 1}),
                       obs(1, {"kind": "command", "id": 2}), CLOSED))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("non-monotonic", result.protocol_failure)

    def test_decreasing_seq_is_a_protocol_failure(self):
        result, _ = self.run_scenario(
            self._scen(HELLO, obs(5, {"kind": "command", "id": 1}),
                       obs(4, {"kind": "command", "id": 2}), CLOSED))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("non-monotonic", result.protocol_failure)

    def test_malformed_snapshot_is_a_protocol_failure(self):
        bad = obs(1, {"kind": "command", "id": 1}, map_=[[1, 0, 9]])
        result, _ = self.run_scenario(self._scen(HELLO, bad, CLOSED))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("invalid snapshot", result.protocol_failure)

    def test_overlong_unterminated_line_terminates_promptly(self):
        big = b"a" * (protocol.MAX_PHYSICAL_LINE + 32)
        scen = _line(HELLO) + big
        t0 = time.monotonic()
        result, _ = self.run_scenario(scen, eof=False, timeout=10.0)
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("exceeds", result.protocol_failure)
        self.assertLess(time.monotonic() - t0, 8.0)

    def test_campaign_continues_after_a_protocol_failure(self):
        bad = _line(obs(1, {"kind": "command", "id": 1})) + _line(CLOSED)
        good = _line(HELLO) + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        ctl = self._controller(timeout=5.0)
        stream = [FakeProc(bad), FakeProc(good)]
        ctl._spawn = lambda priv: stream.pop(0)
        results = ctl.run_campaign(2)
        self.assertIsNotNone(results[0].protocol_failure)
        self.assertTrue(results[1].closed)
        self.assertIsNone(results[1].protocol_failure)


class TestTransport(WireHarness):
    """High 1: one page in flight and deadline-bounded writes."""

    def test_one_get_page_is_in_flight_at_a_time(self):
        peer = WirePeer(pages=25)
        self.addCleanup(peer.close)
        results = self.run_proc(peer.proc, timeout=10.0)
        peer.join()
        result = results[0]
        self.assertTrue(result.closed)
        self.assertEqual(peer.violations, 0)
        self.assertEqual(peer.get_pages, list(range(25)))
        # ordering proof from the recorded wire offsets: get_page k (k>0) was
        # emitted only after page k-1 had been received
        acts = _read_jsonl(os.path.join(self.dir, "ep-1.actions.jsonl"))
        got = {a["action"]["page"]: a["input_offset"] for a in acts
               if a["kind"] == "get_page"}
        offs = _page_offsets(os.path.join(self.dir, "ep-1.wire.jsonl"))
        self.assertEqual(set(got), set(range(25)))
        for k in range(1, 25):
            self.assertGreaterEqual(got[k], offs[k - 1],
                                    "page %d requested before page %d "
                                    "arrived" % (k, k - 1))

    def test_actions_sidecar_matches_the_wire(self):
        # Medium 4: every outbound line is recorded, in order.
        parts0 = [{"p": "h", "k": "v", "val": 1},
                  {"p": "h", "k": "ch", "val": "player"},
                  {"p": "h", "k": "type", "val": "obs"},
                  {"p": "h", "k": "seq", "val": 1},
                  {"p": "h", "k": "base", "val": None},
                  {"p": "pal", "val": [0, " ", "none", 0, "none"]},
                  {"p": "win", "val": {"w": "w9", "kind": "menu",
                                       "title": "Pick a role or profession",
                                       "content": "c9", "pages": 2}}]
        parts1 = [{"p": "need", "val": {"id": 9, "kind": "menu",
                                        "menu": "m9", "mode": "one",
                                        "content": "c9", "pages": 2}}]
        chunk0 = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": 7,
                  "i": 0, "last": False, "parts": parts0}
        chunk1 = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": 7,
                  "i": 1, "last": True, "parts": parts1}
        scen = b"".join([
            _line(HELLO), _line(chunk0), _line(chunk1),
            _line(page("c9", 0, 2, [row(14, "a Valkyrie")])),
            _line(page("c9", 1, 2, [row(3, "a Tourist")])),
            _line(CLOSED),
        ])
        result, stdin_actions = self.run_scenario(scen)
        self.assertTrue(result.closed)
        sidecar = _read_jsonl(os.path.join(self.dir, "ep-1.actions.jsonl"))
        self.assertEqual([a["action"] for a in sidecar], stdin_actions)
        # every outbound line carries an ordinal, offset, kind and status
        self.assertEqual([a["ordinal"] for a in sidecar],
                         list(range(1, len(sidecar) + 1)))
        for a in sidecar:
            self.assertTrue(a["status"])
            self.assertIn(a["kind"], ("act", "get_page", "ack_chunk"))
        kinds = [a["kind"] for a in sidecar]
        self.assertEqual(kinds,
                         ["ack_chunk", "ack_chunk", "get_page", "get_page",
                          "act"])

    def test_invalid_proposal_is_recorded_as_proposal_vs_selected(self):
        class _BadReflex(object):
            def __init__(self, config):
                self.config = config
                self.max_ticks = 10
                self.quitting = False
                self.quit_reason = ""

            def decide(self, ctx):
                return ReflexResult(action={"key": 999}, provider="bad",
                                    reason="out of range proposal")

            def fallback(self, ctx):
                return self.decide(ctx)

            def on_closed(self):
                pass

        scen = b"".join([_line(HELLO),
                         _line(obs(1, {"kind": "command", "id": 1})),
                         _line(CLOSED)])
        with mock.patch.object(controller, "ScriptedReflex", _BadReflex):
            result, actions = self.run_scenario(scen)
        self.assertTrue(result.closed)
        # the wire only ever saw a valid action
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertEqual(acts[-1]["action"], {"key": protocol.KEY_WAIT})
        decs = _read_jsonl(os.path.join(self.dir, "ep-1.decisions.jsonl"))
        chosen = [d for d in decs if d["selected"] is not None]
        self.assertTrue(chosen)
        last = chosen[-1]
        self.assertEqual(last["proposal"], {"key": 999})
        self.assertEqual(last["selected"], {"key": protocol.KEY_WAIT})
        self.assertIn("validation fallback", last["reason"])

    def test_write_all_is_bounded_when_stdin_is_full(self):
        r, w = os.pipe()
        os.set_blocking(w, False)
        blob = b"x" * 65536
        while True:
            try:
                os.write(w, blob)
            except BlockingIOError:
                break
        ctl = self._controller(timeout=5.0)
        proc = types.SimpleNamespace(stdin=_FdStdin(w),
                                     stdout=_FakeStdout(r))
        rec = recording.EpisodeRecorder(self.dir, 90)
        result = controller.EpisodeResult(index=90)
        runner = controller._EpisodeRunner(ctl, proc, rec, result)
        t0 = time.monotonic()
        with self.assertRaises(controller._TransportFailure):
            runner._write_all(b"a get_page line\n",
                              time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - t0, 1.5)
        rec.finalize({})

    def test_peer_that_never_answers_aborts_within_content_deadline(self):
        cfg = ProviderConfig(max_ticks=200, content_deadline=0.4)
        need = {"id": 9, "kind": "menu", "menu": "m9", "mode": "one",
                "content": "c9", "pages": 1000}
        preamble = _line(HELLO) + _line(obs(
            1, need, windows=[{"w": "w9", "kind": "menu",
                               "title": "Pick a role or profession",
                               "content": "c9", "pages": 1000}]))
        peer = SilentPeer(preamble)
        self.addCleanup(peer.close)
        t0 = time.monotonic()
        results = self.run_proc(peer.proc, timeout=8.0, config=cfg)
        elapsed = time.monotonic() - t0
        result = results[0]
        self.assertFalse(result.closed)
        self.assertEqual(result.stop_reason, "content-deadline")
        self.assertLess(elapsed, 4.0)          # aborted within the deadline
        reqs = peer.read_requests()
        gets = [m for m in reqs if m.get("type") == "get_page"]
        self.assertEqual(len(gets), 1)          # never grows without bound


def _page_offsets(wire_path):
    off = 0
    out = {}
    with open(wire_path, "rb") as fh:
        for raw in fh:
            off += len(raw)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("type") == "page":
                out[rec["page"]] = off
    return out


def _process_gone(pid):
    """True if *pid* is neither running nor an unreaped zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open("/proc/%d/stat" % pid) as fh:
            fields = fh.read().split()
    except OSError:
        return True
    return len(fields) > 2 and fields[2] == "Z"


def _wait_gone(pid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.05)
    return _process_gone(pid)


class TestClosure(WireHarness):
    """High 2: closure honesty under pending obligations and failures."""

    def test_broken_stdin_is_a_terminal_transport_failure(self):
        r, w = os.pipe()
        os.close(r)  # no reader: every write raises EPIPE
        proc = FakeProc(_line(HELLO)
                        + _line(obs(1, {"kind": "command", "id": 1})),
                        eof=False)
        proc.stdin = _DeadStdin(w)
        results = self.run_proc(proc, timeout=5.0)
        proc.close()
        result = results[0]
        self.assertFalse(result.closed)
        self.assertEqual(result.stop_reason, "transport-failure-write")
        self.assertIsNotNone(result.failure_reason)
        self.assertFalse(agent_main.episode_ok(result))

    def test_closed_during_a_paged_need_is_unanswered(self):
        scen = b"".join([
            _line(HELLO),
            _line(obs_menu(1, 9, "m9", "c9", "Pick a role or profession",
                           pages=2)),
            _line(page("c9", 0, 2, [row(14, "a Valkyrie")])),
            _line(CLOSED),          # page 1 never arrives
        ])
        result, _ = self.run_scenario(scen)
        self.assertTrue(result.closed)
        self.assertTrue(result.unanswered)
        self.assertEqual(result.stop_reason, "closed-unanswered")
        self.assertIn("unanswered", result.failure_reason)
        self.assertFalse(agent_main.episode_ok(result))

    def test_bare_closed_with_nonzero_exit_is_not_success(self):
        scen = b"".join([_line(HELLO),
                         _line(obs(1, {"kind": "command", "id": 1})),
                         _line(CLOSED)])
        proc = FakeProc(scen)
        proc.returncode = 1
        results = self.run_proc(proc)
        result = results[0]
        self.assertTrue(result.closed)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(agent_main.episode_ok(result))

    def test_answered_terminal_action_then_closed_is_success(self):
        result, _ = self.run_scenario(
            _line(HELLO) + _line(obs(1, {"kind": "command", "id": 1}))
            + _line(CLOSED))
        self.assertTrue(result.closed)
        self.assertFalse(result.unanswered)
        self.assertTrue(agent_main.episode_ok(result))

    def test_episode_ok_requires_every_condition(self):
        good = controller.EpisodeResult(index=1, spawn_ok=True, closed=True,
                                        returncode=0, recording_complete=True)
        self.assertTrue(agent_main.episode_ok(good))
        for field, value in (("spawn_ok", False), ("closed", False),
                             ("forced_kill", True), ("eof", True),
                             ("unanswered", True), ("teardown_failure", True),
                             ("recording_complete", False)):
            bad = controller.EpisodeResult(
                index=1, spawn_ok=True, closed=True, returncode=0,
                recording_complete=True)
            setattr(bad, field, value)
            self.assertFalse(agent_main.episode_ok(bad))


class TestRecording(WireHarness):
    def test_incomplete_when_a_writer_errors(self):
        rec = recording.EpisodeRecorder(self.dir, 1)
        rec.record_wire(b'{"type":"hello"}\n')
        rec._wire.error = "disk full"     # simulate a disk failure
        meta = rec.finalize({})
        self.assertFalse(meta["recording_complete"])

    def test_complete_recording_round_trips(self):
        rec = recording.EpisodeRecorder(self.dir, 2)
        rec.record_wire(b'{"type":"hello"}\n')
        rec.record_action(1, 0, protocol.NeedKey(2, 1, 1), "act", {"key": 46},
                          "sent")
        rec.record_decision({"key": 46}, {"key": 46}, "scripted", "navigate")
        meta = rec.finalize({"stop_reason": "closed"})
        self.assertTrue(meta["recording_complete"])
        self.assertEqual(meta["wire_lines"], 1)
        self.assertEqual(meta["writer_status"], ["drained"] * 3)
        with open(os.path.join(self.dir, "ep-2.actions.jsonl")) as fh:
            line = json.loads(fh.readline())
        self.assertEqual(line["action"], {"key": 46})
        self.assertEqual(line["kind"], "act")

    def test_finalize_marks_incomplete_when_a_writer_is_alive(self):
        rec = recording.EpisodeRecorder(self.dir, 3)
        rec.record_wire(b'{"type":"hello"}\n')

        class _StuckWriter(object):
            error = None
            dropped = 0
            alive = True

            def shutdown(self, timeout=5.0):
                return "terminated"

        rec._wire = _StuckWriter()
        meta = rec.finalize({})
        self.assertFalse(meta["recording_complete"])
        self.assertIn("terminated", meta["writer_status"])

    def test_blocked_writer_shutdown_reports_not_drained(self):
        path = os.path.join(self.dir, "blocked")
        stopped = threading.Event()

        class _Blocked(recording._Writer):
            def run(self):
                stopped.wait(5)

        w = _Blocked(path, maxsize=1)
        w.start()
        self.assertTrue(w.submit(b"x"))     # fills the one-slot queue
        status = w.shutdown(timeout=0.2)
        self.assertIn(status, ("terminated", "not-drained"))
        self.assertTrue(w.alive)
        stopped.set()
        w.join(timeout=2)

    def test_private_dir_and_file_modes_are_tightened(self):
        loose = tempfile.mkdtemp(prefix="auto-loose.")
        self.addCleanup(shutil.rmtree, loose, ignore_errors=True)
        os.chmod(loose, 0o755)
        sidecar = os.path.join(loose, "ep-1.actions.jsonl")
        with open(sidecar, "w") as fh:
            fh.write("")
        os.chmod(sidecar, 0o644)
        rec = recording.EpisodeRecorder(loose, 1)
        self.assertEqual(os.stat(loose).st_mode & 0o777, 0o700)
        mode = os.stat(rec.actions_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        rec.finalize({})


class TestWriter(unittest.TestCase):
    def test_full_queue_is_reported(self):
        d = tempfile.mkdtemp(prefix="auto-rec.")
        w = recording._Writer(os.path.join(d, "x"), maxsize=1)
        self.assertTrue(w.submit(b"a"))
        self.assertFalse(w.submit(b"b"))   # queue full, dropped
        self.assertEqual(w.dropped, 1)
        w.shutdown()


class TestEnvAndProcess(WireHarness):
    """Medium 7 and 8: child environment and process-group teardown."""

    def test_child_env_strips_provider_secrets(self):
        with mock.patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "sentinel-deepseek",
                "JEV_API_KEY": "sentinel-jev",
                "SOME_OTHER_TOKEN": "sentinel-token",
                "HOME": "/home/tester"}, clear=False):
            env = controller.child_env()
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("JEV_API_KEY", env)
        self.assertNotIn("SOME_OTHER_TOKEN", env)
        self.assertEqual(env["HOME"], "/home/tester")
        self.assertIn("PATH", env)

    def _env_dump_script(self, dump_path):
        script = os.path.join(self.dir, "envdump.py")
        with open(script, "w") as fh:
            fh.write("#!/usr/bin/env python3\n"
                     "import json, os, sys\n"
                     "json.dump(dict(os.environ), "
                     "open(%r, 'w'))\n" % dump_path)
        os.chmod(script, 0o755)
        return script

    def test_spawn_does_not_leak_secrets_to_the_child(self):
        dump = os.path.join(self.dir, "child-env.json")
        script = self._env_dump_script(dump)
        ctl = controller.Controller(
            ProviderConfig(), controller.ControllerPaths(
                worker="w", runner=script, data="d"), self.dir,
            episode_timeout=5.0)
        with mock.patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "sentinel-deepseek",
                "JEV_API_KEY": "sentinel-jev"}, clear=False):
            proc = ctl._spawn(self.dir)
            proc.wait(timeout=5)
            ctl._reap(proc, controller.EpisodeResult(index=1))
        with open(dump) as fh:
            child_env = json.load(fh)
        self.assertNotIn("DEEPSEEK_API_KEY", child_env)
        self.assertNotIn("JEV_API_KEY", child_env)
        self.assertIn("PATH", child_env)
        self.assertIn("HOME", child_env)

    def _launcher_script(self, pidfile):
        script = os.path.join(self.dir, "stubborn.py")
        with open(script, "w") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "import os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "pid = os.fork()\n"
                "if pid == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    while True:\n"
                "        time.sleep(0.5)\n"
                "open(%r, 'w').write(str(pid))\n"
                "while True:\n"
                "    time.sleep(0.5)\n" % pidfile)
        os.chmod(script, 0o755)
        return script

    def test_watchdog_reaps_the_whole_process_group(self):
        pidfile = os.path.join(self.dir, "child.pid")
        script = self._launcher_script(pidfile)
        ctl = controller.Controller(
            ProviderConfig(), controller.ControllerPaths(
                worker="w", runner=script, data="d"), self.dir,
            episode_timeout=5.0, reap_grace=0.5)
        proc = ctl._spawn(self.dir)
        deadline = time.monotonic() + 3.0
        child_pid = None
        while time.monotonic() < deadline:
            if os.path.exists(pidfile):
                with open(pidfile) as fh:
                    child_pid = int(fh.read().strip())
                break
            time.sleep(0.05)
        self.assertIsNotNone(child_pid, "the launcher never forked a child")
        result = controller.EpisodeResult(index=1)
        t0 = time.monotonic()
        ctl._reap(proc, result)
        elapsed = time.monotonic() - t0
        # SIGTERM was ignored, so the group was escalated to SIGKILL
        self.assertTrue(result.forced_kill)
        self.assertFalse(result.teardown_failure)
        self.assertLess(elapsed, 12.0)
        self.assertTrue(_wait_gone(child_pid, 3.0),
                        "the grandchild survived teardown")
        self.assertTrue(_wait_gone(proc.pid, 3.0),
                        "the launcher survived teardown")


class TestPerEpisodeReset(unittest.TestCase):
    def test_two_episodes_have_independent_state(self):
        scen = _line(HELLO) + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        config = ProviderConfig(max_ticks=50)
        d = tempfile.mkdtemp(prefix="auto-reset.")
        ctl = controller.Controller(
            config, controller.ControllerPaths("w", "r", "d", "s"), d,
            episode_timeout=5.0)
        results = []
        for i in (1, 2):
            proc = FakeProc(scen)
            ctl._spawn = lambda priv, _p=proc: _p
            results.append(ctl.run_episode(i))
        self.assertTrue(all(r.closed for r in results))
        self.assertEqual([r.index for r in results], [1, 2])


if __name__ == "__main__":
    unittest.main()
