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

import io
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
                                   ReflexContext, ReflexResult,
                                   ReflexTimeout)


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


class DripPeer(object):
    """Writes a preamble, then drips one record at a fixed interval.

    Used to prove that a retry state (`invalid`) consumes the *same* content
    budget instead of restarting it: a peer that drip-feeds rejections must
    not be able to push the need deadline forward forever.
    """

    def __init__(self, preamble: bytes, record, interval=0.08, limit=200):
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        self.proc = _PeerProc(self.out_r, self.in_w)
        self.record = record
        self.interval = interval
        self.limit = limit
        self.sent = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, args=(preamble,),
                                   daemon=True)
        self._t.start()

    def _serve(self, preamble):
        w = os.fdopen(self.out_w, "wb", buffering=0)
        try:
            w.write(preamble)
            for _ in range(self.limit):
                if self._stop.is_set():
                    break
                w.write(_line(self.record))
                self.sent += 1
                time.sleep(self.interval)
        except OSError:
            pass
        finally:
            try:
                w.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self._t.join(timeout=1.0)
        for fd in (self.in_r, self.in_w, self.out_r):
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
                     timeout: float = 10.0, max_ticks: int = 200,
                     config=None):
        ctl = self._controller(timeout=timeout, max_ticks=max_ticks,
                               config=config)
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


class TestNeedValidation(unittest.TestCase):
    """Medium 6: the complete need shape is checked before it is stored."""

    NEEDS = (
        {"id": 1, "kind": "command"},
        {"id": 2, "kind": "key", "prompt": ""},
        {"id": 3, "kind": "direction"},
        {"id": 4, "kind": "position", "prompt": "Where?",
         "x0": 1, "y0": 0, "x1": 79, "y1": 20},
        {"id": 5, "kind": "yn", "prompt": "Continue? [yn]",
         "choices": "yn", "default": 121, "numeric": False},
        {"id": 6, "kind": "line", "prompt": "Name?", "max": 32},
        {"id": 7, "kind": "extcmd", "prompt": "#", "max": 255},
        {"id": 8, "kind": "menu", "menu": "m1", "mode": "one",
         "content": "c1", "pages": 2},
        {"id": 9, "kind": "ack", "content": "c9", "pages": 1},
    )

    def test_complete_needs_of_every_kind_are_accepted(self):
        for need in self.NEEDS:
            self.assertIsNone(protocol.validate_need(need),
                              "%s must be accepted" % (need,))

    def test_missing_mistyped_and_misranged_fields_are_rejected(self):
        menu = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        cases = [
            ("not an object", "nope"),
            ("unknown kind", {"id": 1, "kind": "wibble"}),
            ("id missing", {"kind": "command"}),
            ("id not an int", {"kind": "command", "id": "1"}),
            ("id zero", {"kind": "command", "id": 0}),
            ("id past the counter", {"kind": "command", "id": 2 ** 53}),
            ("id is a bool", {"kind": "command", "id": True}),
            ("pages string", dict(menu, pages="1")),
            ("pages negative", dict(menu, pages=-1)),
            ("pages oversized", dict(menu, pages=65536)),
            ("pages is a bool", dict(menu, pages=True)),
            ("menu ref missing",
             {k: v for k, v in menu.items() if k != "menu"}),
            ("menu ref malformed", dict(menu, menu="x1")),
            ("mode unknown", dict(menu, mode="some")),
            ("content missing",
             {k: v for k, v in menu.items() if k != "content"}),
            ("content not a string", dict(menu, content=3)),
            ("content ref malformed", dict(menu, content="m1")),
            ("ack content missing", {"id": 2, "kind": "ack", "pages": 1}),
            ("unexpected field", dict(menu, extra=1)),
            ("max not an int",
             {"id": 3, "kind": "line", "prompt": "", "max": "9"}),
            ("max oversized",
             {"id": 3, "kind": "line", "prompt": "", "max": 256}),
            ("yn default out of range",
             {"id": 4, "kind": "yn", "prompt": "", "choices": None,
              "default": 0, "numeric": False}),
            ("yn numeric not a bool",
             {"id": 4, "kind": "yn", "prompt": "", "choices": None,
              "default": None, "numeric": "no"}),
            ("position missing a corner",
             {"id": 5, "kind": "position", "prompt": "",
              "x0": 1, "y0": 0, "y1": 20}),
            ("position out of range",
             {"id": 5, "kind": "position", "prompt": "",
              "x0": 0, "y0": 0, "x1": 79, "y1": 20}),
        ]
        for label, need in cases:
            self.assertIsNotNone(protocol.validate_need(need), label)

    def test_a_validated_need_keeps_the_page_obligation_safe(self):
        # the page/chunk obligations read the stored need directly: every
        # accepted shape must be safe to drive through them
        for need in self.NEEDS:
            self.assertIsNone(protocol.validate_need(need))
            req = protocol.Request()
            req.begin(need, 1)
            self.assertIsInstance(req.pages_declared, int)
            req.pages_complete()
            preq = req.next_page_request()
            if req.pages_declared:
                self.assertIsInstance(preq, dict)
                self.assertIsInstance(preq["id"], int)
            else:
                self.assertIsNone(preq)

    def test_the_line_max_is_never_compared_to_a_string(self):
        # a malformed advertised max is a shape error, never a TypeError
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": "9"}, {"text": "x"}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": -1}, {"text": "x"}))
        self.assertIsNone(protocol.validate_action(
            {"kind": "line", "max": 3}, {"text": "ok"}))

    def test_prompt_must_be_a_string_and_bounded(self):
        # re-review residual 2a (low): the schema types prompt as a string,
        # never null, and the engine always publishes one -- empty when there
        # is none -- so a null must fail on every kind that can carry a
        # prompt, including the optional-prompt command family.
        for kind, extra in (("command", {}), ("key", {}), ("direction", {}),
                            ("position", {"x0": 1, "y0": 0, "x1": 79,
                                          "y1": 20}),
                            ("yn", {"choices": None, "default": None,
                                    "numeric": False}),
                            ("line", {"max": 32}),
                            ("extcmd", {"max": 255})):
            need = {"id": 1, "kind": kind, "prompt": None}
            need.update(extra)
            self.assertIsNotNone(protocol.validate_need(need),
                                 "null prompt accepted for %s" % kind)
        # the engine's own empty-string prompt is fine, and the schema's
        # maxLength bound is enforced on both sides of the limit
        self.assertIsNone(protocol.validate_need(
            {"id": 1, "kind": "command", "prompt": ""}))
        self.assertIsNone(protocol.validate_need(
            {"id": 1, "kind": "command", "prompt": "x" * 1048576}))
        self.assertIsNotNone(protocol.validate_need(
            {"id": 1, "kind": "command", "prompt": "x" * 1048577}))
        # menu/ack have no prompt property at all: one is an unknown field,
        # so even "prompt": null is rejected there
        self.assertIsNotNone(protocol.validate_need(
            {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
             "content": "c1", "pages": 1, "prompt": None}))

    def test_zero_line_max_is_preserved(self):
        # re-review residual 2b (low): an advertised max of 0 must stay 0 (it
        # forbids any non-empty text), never widen to the 255 default.  Empty
        # text is valid at max 0; a single byte is not.
        self.assertIsNone(protocol.validate_action(
            {"kind": "line", "max": 0}, {"text": ""}))
        self.assertIsNone(protocol.validate_action(
            {"kind": "extcmd", "max": 0}, {"cancel": True}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": 0}, {"text": "x"}))
        # a multibyte character counts as its decoded byte length, so it is
        # still over a zero budget
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": 0}, {"text": "\u00e9"}))
        # a legal non-zero budget is the boundary it advertises
        self.assertIsNone(protocol.validate_action(
            {"kind": "line", "max": 1}, {"text": "x"}))
        self.assertIsNotNone(protocol.validate_action(
            {"kind": "line", "max": 1}, {"text": "xy"}))


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

    def test_punctuation_monsters_are_hazards_not_floor(self):
        # Medium 6: a monster is classified by the whole public glyph table,
        # not only its letters.
        for glyph in ("&", ";", ":", "'", "~", "]"):
            self.assertTrue(state.monster_glyph(glyph),
                            "%r must be a monster glyph" % glyph)
            self.assertFalse(state.passable(glyph),
                            "%r must not be walkable" % glyph)
        # a boxed-in hero beside a demon never steps into it
        self._hero(10, 10)
        self.mem.grid[(9, 10)] = ("&", "red", 0, "none")
        self.mem.no_progress = 20
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertNotEqual(res.action.get("key"), protocol.DIR_KEYS[(-1, 0)])

    def test_adjacent_at_is_a_monster_hazard(self):
        # Medium 8: '@' is the hero *and* every other human.  Hero identity
        # is the known hero square; an '@' on any other cell is a
        # monster-class hazard.
        self._hero(10, 10)
        self.assertFalse(state.monster_glyph("@"))     # identity is per cell
        self.assertFalse(state.monster_cell("@", (10, 10), (10, 10)))
        self.mem.grid[(11, 10)] = ("@", "white", 32, "none")
        self.assertTrue(state.monster_cell("@", (10, 10), (11, 10)))
        self.assertFalse(self.mem.known_passable((11, 10)))
        self.assertIn((1, 0),
                      self.ref._adjacent_monsters(self.mem, (10, 10)))
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        self.assertFalse(self.ref._safe_to_rest(self.mem, self.mem.status,
                                                (10, 10)))
        # boxed in beside it: never step into it, and never rest
        self.mem.no_progress = 20
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})
        self.assertNotEqual(res.action.get("key"), protocol.DIR_KEYS[(1, 0)])

    def test_boxed_in_and_hungry_searches_instead_of_waiting(self):
        # Medium 6: every wait is gated on _safe_to_rest, so a boxed-in,
        # hungry hero searches rather than resting.
        self._hero(10, 10)
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        self.mem.status.hunger = "Hungry"
        self.ref.last_eat_tick = 0     # the eat intent is already on cooldown
        self.mem.no_progress = 6
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}, tick=0))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})

    def test_random_move_never_waits_when_resting_is_unsafe(self):
        self._hero(10, 10)
        self.mem.status.hp = 20
        self.mem.status.hp_max = 20
        # hungry and no known floor: a wait is not safe, so search instead
        self.mem.status.hunger = "Weak"
        key, why = self.ref._random_move(self.mem, (10, 10))
        self.assertEqual(key, protocol.KEY_SEARCH)
        self.assertIn("unsafe to rest", why)
        # an adjacent monster is likewise not a moment to rest
        self.mem.status.hunger = ""
        self.mem.grid[(11, 10)] = ("&", "red", 0, "none")
        key2, _ = self.ref._random_move(self.mem, (10, 10))
        self.assertEqual(key2, protocol.KEY_SEARCH)

    def test_missing_hero_never_moves_blind(self):
        # Medium 6: without a known hero square every direction is unknown
        # space, so hold the turn with a search rather than a blind step.
        self.mem.hero = None
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})
        self.assertNotIn(res.action["key"], protocol.DIR_KEYS.values())

    def test_cockatrice_egg_is_never_a_safe_food(self):
        # Medium 6: food matching is exact enough to reject a dangerous
        # qualified egg, while still accepting the bare egg and a ration.
        self.assertFalse(state.is_known_safe_food("a cockatrice egg"))
        self.assertFalse(state.is_known_safe_food("kobold egg"))
        self.assertTrue(state.is_known_safe_food("an egg"))
        self.assertTrue(state.is_known_safe_food("egg"))
        self.assertTrue(state.is_known_safe_food("d - a food ration"))
        self.ref.intent = "eat"
        rows = [row(7, "a cockatrice egg"), row(2, "a food ration")]
        res = self.ref.decide(self.ctx(menu_need(1, "m1", "c1"), rows,
                                       title="What do you want to eat?"))
        self.assertEqual(res.action["commit"], [[2, -1]])

    def test_counted_and_plural_food_stacks_are_recognised(self):
        # Medium 9: a displayed leading count and the exact plural of an
        # allowlisted name are recognised, so a real stack stays edible.
        for text in ("2 food rations", "d - 2 food rations", "2 apples",
                     "3 bananas", "f: 12 oranges", "a - 2 food rations",
                     "an egg", "egg"):
            self.assertTrue(state.is_known_safe_food(text), text)
        # ... while a qualified egg, a corpse or tin, and a lookalike stay
        # unsafe even when counted
        for text in ("2 cockatrice eggs", "cockatrice egg", "kobold egg",
                     "2 eggs", "a kobold corpse", "a tin of spinach",
                     "banana peel", "applesauce"):
            self.assertFalse(state.is_known_safe_food(text), text)

    def test_inventory_food_rows_include_counted_stacks(self):
        # Medium 9: the inventory row path (and its letters) sees the stack.
        self.mem.inventory.refresh([row(0, "a - 2 food rations"),
                                    row(1, "b - 2 apples"),
                                    row(2, "c - 2 cockatrice eggs")], 0, 0)
        self.assertEqual([r["r"] for r in self.mem.inventory.food_rows()],
                         [0, 1])
        self.assertEqual(self.mem.inventory.food_letters(), ["a", "b"])

    def test_eat_menu_commits_a_counted_stack_row(self):
        # Medium 9: the eat-intent menu path commits the real stack row.
        self.ref.intent = "eat"
        rows = [row(7, "a kobold corpse"), row(2, "2 food rations")]
        res = self.ref.decide(self.ctx(menu_need(1, "m1", "c1"), rows,
                                       title="What do you want to eat?"))
        self.assertEqual(res.action["commit"], [[2, -1]])

    def test_canonical_engine_food_names_are_recognised(self):
        # re-review residual 1 (medium): the allowlist names are the engine's
        # own object names, so a carried lembas wafer or cram ration -- which
        # used to be rejected because they are not the bare "lembas"/"cram"
        # -- is not starved past.
        for text in ("a lembas wafer", "2 lembas wafers", "a cram ration",
                     "3 cram rations", "a tripe ration", "a kelp frond",
                     "2 kelp fronds", "a food ration", "an orange",
                     "a cream pie", "a candy bar", "a fortune cookie",
                     "a meatball", "some food rations"):
            self.assertTrue(state.is_known_safe_food(text), text)

    def test_doname_metadata_is_stripped_before_matching(self):
        # re-review residual 1 (medium): a real inventory row wraps the name
        # in a count/article, a BUC or "partly eaten" qualifier and a shop
        # annotation, all of which must be peeled off before the base name is
        # matched.  "partly eaten" stays edible (src/eat.c continues a
        # partly eaten meal).
        for text in ("a food ration named lunch",
                     "an uncursed food ration (unpaid, 45 zorkmids)",
                     "a blessed partly eaten food ration",
                     "a cursed partly eaten lembas wafer",
                     "2 uncursed food rations (unpaid, 90 zorkmids)",
                     "d - a blessed partly eaten food ration",
                     "a - 2 lembas wafers"):
            self.assertTrue(state.is_known_safe_food(text), text)

    def test_named_suffix_is_removed_whole_before_matching(self):
        # re-review residual 1 (medium): the base name, never the user
        # " named ..." text, decides -- so a safe base named with a lethal
        # word stays edible while a lethal base named with a safe word does
        # not, and a lookalike that merely contains a safe word is rejected.
        self.assertTrue(state.is_known_safe_food(
            "a food ration named cockatrice egg"))
        for text in ("a cockatrice egg named lunch",
                     "a cockatrice egg named lembas wafer",
                     "apple pie", "banana slug", "applesauce",
                     "banana peel", "a food ration of doom",
                     "an enormous meatball"):
            self.assertFalse(state.is_known_safe_food(text), text)

    def test_inventory_rows_with_doname_metadata_are_edible(self):
        # re-review residual 1 (medium): the inventory row path (and its
        # letters) sees through doname()'s metadata.
        self.mem.inventory.refresh(
            [row(0, "a - a lembas wafer"),
             row(1, "b - a food ration named lunch"),
             row(2, "c - an uncursed food ration (unpaid, 45 zorkmids)"),
             row(3, "d - a cockatrice egg"),
             row(4, "e - a kobold corpse")], 0, 0)
        self.assertEqual([r["r"] for r in self.mem.inventory.food_rows()],
                         [0, 1, 2])
        self.assertEqual(self.mem.inventory.food_letters(), ["a", "b", "c"])

    def test_low_hp_on_upstairs_returns_a_valid_ascend_action(self):
        # Medium 6: the upstairs withdrawal is a structurally valid action,
        # so arbitration cannot silently replace ascend with a wait.
        self._hero(10, 10)
        self.mem.stairs_up.add((10, 10))
        self.mem.status.hp = 2
        self.mem.status.hp_max = 20
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertEqual(res.action, {"key": ord("<")})
        self.assertIsNone(
            protocol.validate_action({"kind": "command"}, res.action))


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

    def test_page_response_must_match_the_outstanding_request(self):
        # Fix 5: only the exact outstanding page, with a matching page count,
        # is accepted; a wrong index or total is an immediate protocol fault.
        cases = {
            "wrong-index": _line(page("c9", 1, 2, [row(1, "x")])),
            "wrong-total": _line(page("c9", 0, 5, [row(1, "x")])),
        }
        for label, raw in cases.items():
            with self.subTest(case=label):
                scen = b"".join([
                    _line(HELLO),
                    _line(obs_menu(1, 9, "m9", "c9",
                                   "Pick a role or profession", pages=2)),
                    raw,
                    _line(CLOSED),
                ])
                result, _ = self.run_scenario(scen)
                self.assertIsNotNone(result.protocol_failure,
                                     "%s slipped through" % label)
                self.assertEqual(result.stop_reason, "protocol-failure")

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

    def test_closed_before_hello_is_a_protocol_failure(self):
        # Medium 3: closure is only meaningful after a validated handshake,
        # so a closed-only stream fails rather than succeeding.
        result, _ = self.run_scenario(self._scen(CLOSED))
        self.assertIsNotNone(result.protocol_failure)
        self.assertIn("before hello", result.protocol_failure)
        self.assertFalse(result.closed)
        self.assertFalse(agent_main.episode_ok(result))

    def test_malformed_shapes_end_only_that_episode(self):
        # Medium 3: a JSON array, a broken palette/map/window/cursor or a
        # non-object need must terminate episode 1 and leave episode 2
        # running, instead of aborting the whole campaign.
        hello_line = _line(HELLO)
        good = hello_line + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        cmd = {"kind": "command", "id": 1}
        shapes = {
            "array": b"[]\n",
            "pal": _line(obs(1, cmd, pal=[[]])),
            "triple": _line(obs(1, cmd, map_=[[1, 0]])),
            "windows": _line(obs(1, cmd, windows=[[]])),
            "cur": _line(obs(1, cmd, cur=[1])),
            "need": _line(obs(1, "not an object")),
        }
        for label, raw in shapes.items():
            with self.subTest(shape=label):
                ctl = self._controller(timeout=5.0)
                stream = [FakeProc(hello_line + raw), FakeProc(good)]
                ctl._spawn = lambda priv: stream.pop(0)
                results = ctl.run_campaign(2)
                self.assertIsNotNone(
                    results[0].protocol_failure,
                    "%s slipped through: %r" % (label,
                                                results[0].protocol_failure))
                self.assertFalse(results[0].closed)
                self.assertTrue(results[1].closed)
                self.assertIsNone(results[1].protocol_failure)

    def test_dict_shaped_malformed_needs_end_only_that_episode(self):
        # Medium 6: a well-typed but incomplete or out-of-range need used to
        # be stored on the Request and only read later, by pages_complete /
        # next_page_request -- outside run()'s boundary, so the KeyError or
        # TypeError it raised aborted the whole campaign.  Every such need
        # must now fail episode 1 alone and leave episode 2 running to closed.
        hello_line = _line(HELLO)
        good = hello_line + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        menu = {"kind": "menu", "id": 1, "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        cases = {
            "pages-string": dict(menu, pages="1"),
            "pages-negative": dict(menu, pages=-1),
            "pages-oversized": dict(menu, pages=65536),
            "id-missing": {"kind": "command"},
            "id-missing-paged": {k: v for k, v in menu.items()
                                 if k != "id"},
            "id-not-int": {"kind": "command", "id": "1"},
            "id-zero": {"kind": "command", "id": 0},
            "menu-missing": {k: v for k, v in menu.items() if k != "menu"},
            "content-missing": {k: v for k, v in menu.items()
                                if k != "content"},
            "content-mistyped": dict(menu, content=3),
            "ack-content-missing": {"kind": "ack", "id": 2, "pages": 1},
            "prompt-null": {"kind": "command", "id": 1, "prompt": None},
        }
        for label, need in cases.items():
            with self.subTest(case=label):
                ctl = self._controller(timeout=5.0)
                stream = [FakeProc(hello_line + _line(obs(1, need))),
                          FakeProc(good)]
                ctl._spawn = lambda priv: stream.pop(0)
                results = ctl.run_campaign(2)
                self.assertIsNotNone(
                    results[0].protocol_failure,
                    "%s slipped through" % label)
                self.assertEqual(results[0].stop_reason, "protocol-failure")
                self.assertFalse(results[0].closed)
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
        # the wire only ever saw a valid action, and the universal fallback is
        # deliberately non-resting (it cannot prove a rest safe)
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertEqual(acts[-1]["action"], {"key": protocol.KEY_SEARCH})
        self.assertNotEqual(acts[-1]["action"],
                            {"key": protocol.KEY_WAIT})
        decs = _read_jsonl(os.path.join(self.dir, "ep-1.decisions.jsonl"))
        chosen = [d for d in decs if d["selected"] is not None]
        self.assertTrue(chosen)
        last = chosen[-1]
        self.assertEqual(last["proposal"], {"key": 999})
        self.assertEqual(last["selected"], {"key": protocol.KEY_SEARCH})
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


class TestDeadlines(WireHarness):
    """High 1: the reflex deadline and the aggregate content deadline."""

    def test_blocking_reflex_falls_back_within_the_reflex_deadline(self):
        class _SlowReflex(object):
            def __init__(self, config):
                self.config = config
                self.max_ticks = 10
                self.quitting = False
                self.quit_reason = ""

            def decide(self, ctx):
                time.sleep(1.5)
                return ReflexResult(action={"key": protocol.KEY_SEARCH},
                                    provider="slow")

            def fallback(self, ctx):
                return self.decide(ctx)

            def on_closed(self):
                pass

        cfg = ProviderConfig(max_ticks=50, reflex_deadline=0.15)
        scen = b"".join([_line(HELLO),
                         _line(obs(1, {"kind": "command", "id": 1})),
                         _line(CLOSED)])
        with mock.patch.object(controller, "ScriptedReflex", _SlowReflex):
            t0 = time.monotonic()
            result, actions = self.run_scenario(scen, config=cfg)
            elapsed = time.monotonic() - t0
        self.assertTrue(result.closed)
        self.assertLess(elapsed, 1.2)          # never waited for the sleep
        self.assertGreaterEqual(result.reflex_timeouts, 1)
        acts = [a for a in actions if a.get("type") == "act"]
        # a timeout does not buy a rest: the fallback is non-resting
        self.assertEqual(acts[-1]["action"], {"key": protocol.KEY_SEARCH})
        self.assertNotEqual(acts[-1]["action"], {"key": protocol.KEY_WAIT})
        decs = _read_jsonl(os.path.join(self.dir, "ep-1.decisions.jsonl"))
        self.assertTrue(any("reflex deadline exceeded" in (d["reason"] or "")
                            for d in decs))

    def test_scripted_reflex_raises_when_past_its_deadline(self):
        ref = policy.ScriptedReflex(ProviderConfig())
        ctx = ReflexContext(
            episode=1, tick=0, need={"kind": "command", "id": 1},
            need_key=protocol.NeedKey(1, 0, 1), snapshot=protocol.Snapshot(),
            pages=[], memory=state.EpisodeMemory(),
            deadline=time.monotonic() - 1.0)
        with self.assertRaises(ReflexTimeout):
            ref.decide(ctx)

    def test_drip_invalids_do_not_extend_the_content_deadline(self):
        # retries consume the ORIGINAL content budget: a peer cannot buy a
        # fresh content_deadline with each rejection
        cfg = ProviderConfig(max_ticks=200, content_deadline=0.4,
                             reflex_deadline=0.05)
        ctl = controller.Controller(
            cfg, controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=1.5, max_retries=1000)
        need = {"id": 1, "kind": "command"}
        preamble = _line(HELLO) + _line(obs(1, need))
        invalid = {"v": 1, "ch": "control", "type": "invalid", "d": 2,
                   "code": "kind"}
        peer = DripPeer(preamble, invalid)
        self.addCleanup(peer.close)
        ctl._spawn = lambda priv: peer.proc
        t0 = time.monotonic()
        results = ctl.run_campaign(1)
        elapsed = time.monotonic() - t0
        result = results[0]
        self.assertEqual(result.stop_reason, "content-deadline")
        self.assertLess(elapsed, 1.4)
        self.assertGreaterEqual(result.invalids, 1)
        self.assertLessEqual(result.invalids, 8)


class TestFallbackSafety(WireHarness):
    """Medium 7: the controller fallback is never an unproven rest."""

    PAL = [[0, " ", "none", 0, "none"], [1, "@", "white", 32, "none"],
           [2, ".", "gray", 0, "none"], [3, "&", "red", 0, "none"]]

    class _TimeoutReflex(object):
        """A reflex that never answers inside its allowance."""

        def __init__(self, config):
            self.config = config
            self.max_ticks = config.max_ticks
            self.quitting = False
            self.quit_reason = ""

        def decide(self, ctx):
            time.sleep(1.0)
            return ReflexResult(action={"key": protocol.KEY_SEARCH},
                                provider="slow")

        def fallback(self, ctx):
            return self.decide(ctx)

        def on_closed(self):
            pass

    class _InvalidReflex(object):
        """A reflex that proposes a structurally invalid key."""

        def __init__(self, config):
            self.config = config
            self.max_ticks = config.max_ticks
            self.quitting = False
            self.quit_reason = ""

        def decide(self, ctx):
            return ReflexResult(action={"key": 999}, provider="bad",
                                reason="out of range proposal")

        def fallback(self, ctx):
            return self.decide(ctx)

        def on_closed(self):
            pass

    @staticmethod
    def _status(hp="20", hp_max="20", hunger=None):
        s = {"hitpoints": {"text": hp, "color": "none", "style": 0},
             "hitpoints-max": {"text": hp_max, "color": "none",
                               "style": 0}}
        if hunger is not None:
            s["hunger"] = {"text": hunger, "color": "none", "style": 0}
        return s

    def _contexts(self):
        """(label, map triples, snapshot status) for every context the
        fallback must survive: four where a rest is unsafe, plus one where a
        rest would be provable (the universal fallback is non-resting
        regardless, so it never has to prove anything)."""
        return (
            ("adjacent demon", [[10, 10, 1], [11, 10, 3]], self._status()),
            ("hungry", [[10, 10, 1]], self._status(hunger="Hungry")),
            ("low HP", [[10, 10, 1]], self._status(hp="2")),
            ("no hero", [[2, 0, 2]], self._status()),
            ("restable", [[10, 10, 1]], self._status()),
        )

    def _scenario(self, map_, status):
        rec = obs(1, {"kind": "command", "id": 1}, map_=map_, pal=self.PAL)
        rec["s"] = status
        return b"".join([_line(HELLO), _line(rec), _line(CLOSED)])

    def _assert_never_rests(self, actions):
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertTrue(acts)
        self.assertEqual(acts[-1]["action"], {"key": protocol.KEY_SEARCH})
        self.assertNotEqual(acts[-1]["action"], {"key": protocol.KEY_WAIT})

    def test_universal_command_fallback_is_never_a_rest(self):
        runner = object.__new__(controller._EpisodeRunner)
        for kind in ("command", "key", "direction"):
            self.assertEqual(
                controller._EpisodeRunner._safe_fallback(
                    runner, {"kind": kind, "id": 1}),
                {"key": protocol.KEY_SEARCH})
        # the other kinds keep their own structurally valid fallbacks
        self.assertEqual(controller._EpisodeRunner._safe_fallback(
            runner, {"kind": "yn", "id": 1}), {"yn": protocol.KEY_ESC})
        self.assertEqual(controller._EpisodeRunner._safe_fallback(
            runner, {"kind": "menu", "id": 1}), {"cancel": True})

    def test_timeout_fallback_never_rests(self):
        cfg = ProviderConfig(max_ticks=50, reflex_deadline=0.15)
        with mock.patch.object(controller, "ScriptedReflex",
                               self._TimeoutReflex):
            for label, map_, status in self._contexts():
                with self.subTest(context=label):
                    result, actions = self.run_scenario(
                        self._scenario(map_, status), config=cfg)
                    self.assertTrue(result.closed)
                    self.assertGreaterEqual(result.reflex_timeouts, 1)
                    self._assert_never_rests(actions)

    def test_invalid_proposal_fallback_never_rests(self):
        with mock.patch.object(controller, "ScriptedReflex",
                               self._InvalidReflex):
            for label, map_, status in self._contexts():
                with self.subTest(context=label):
                    result, actions = self.run_scenario(
                        self._scenario(map_, status))
                    self.assertTrue(result.closed)
                    self._assert_never_rests(actions)


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
        self.assertEqual(meta["writer_status"], ["drained"] * 4)
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

    def test_fchmod_failure_is_a_recording_error(self):
        # the small residual: a filesystem that rejects chmod must not
        # silently proceed with a permissive transcript
        path = os.path.join(self.dir, "perm.txt")

        def boom(fd, mode):
            raise OSError("chmod rejected by the filesystem")

        with mock.patch.object(os, "fchmod", boom):
            with self.assertRaises(OSError):
                recording._open_private(
                    path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        # failed closed: nothing was left group- or world-accessible
        self.assertEqual(os.stat(path).st_mode & 0o077, 0)

    def test_recorder_fchmod_failure_fails_the_episode(self):
        # a recorder that cannot enforce 0600 is a recording failure for that
        # episode, not a crash of the whole campaign
        def boom(fd, mode):
            raise OSError("chmod rejected by the filesystem")

        scen = _line(HELLO) + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        ctl = self._controller(timeout=5.0)
        proc = FakeProc(scen)
        ctl._spawn = lambda priv: proc
        with mock.patch.object(os, "fchmod", boom):
            results = ctl.run_campaign(1)
        proc.close()
        result = results[0]
        self.assertTrue(result.recorder_failed)
        self.assertEqual(result.stop_reason, "recorder-failure")
        self.assertIsNone(result.protocol_failure)
        self.assertFalse(result.recording_complete)


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

    def _forker_script(self, pidfile):
        """A launcher that forks a TERM-ignoring child and then, unlike the
        child, exits on TERM (the default disposition)."""
        script = os.path.join(self.dir, "forker.py")
        with open(script, "w") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "import os, signal, time\n"
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

    def _wait_pidfile(self, pidfile, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(pidfile):
                with open(pidfile) as fh:
                    return int(fh.read().strip())
            time.sleep(0.05)
        return None

    def test_watchdog_kills_the_group_when_the_leader_exits_on_term(self):
        # Medium 8: escalation must not depend on the direct launcher still
        # running.  Here the leader exits on TERM but its child ignores it, so
        # only a group-level assessment can find and kill the descendant.
        pidfile = os.path.join(self.dir, "fork.pid")
        script = self._forker_script(pidfile)
        ctl = controller.Controller(
            ProviderConfig(), controller.ControllerPaths(
                worker="w", runner=script, data="d"), self.dir,
            episode_timeout=5.0, reap_grace=0.5)
        proc = ctl._spawn(self.dir)
        child_pid = self._wait_pidfile(pidfile)
        self.assertIsNotNone(child_pid, "the launcher never forked a child")
        result = controller.EpisodeResult(index=1)
        ctl._reap(proc, result)
        # the group was killed after the leader had already exited
        self.assertTrue(result.forced_kill)
        self.assertFalse(result.teardown_failure)
        self.assertTrue(_wait_gone(child_pid, 3.0),
                        "the TERM-ignoring descendant survived teardown")
        self.assertTrue(_wait_gone(proc.pid, 3.0),
                        "the launcher was not reaped")


class TestCliValidation(unittest.TestCase):
    """Medium 2: invalid numeric configuration is rejected before start."""

    def _args(self, argv):
        return agent_main.build_parser().parse_args(argv)

    def _bad(self, argv, needle):
        problem = agent_main.validate_args(self._args(argv))
        self.assertIsNotNone(problem, argv)
        self.assertIn(needle, problem)

    def _ok(self, argv):
        self.assertIsNone(agent_main.validate_args(self._args(argv)), argv)

    def test_usd_cap_requires_a_complete_tariff(self):
        self._bad(["auto", "--output-dir", "/tmp/x", "--usd-cap", "1"],
                  "complete tariff")
        self._bad(["auto", "--output-dir", "/tmp/x", "--usd-cap", "1",
                   "--deepseek-price-in", "1"], "complete tariff")
        self._ok(["auto", "--output-dir", "/tmp/x", "--usd-cap", "1",
                  "--deepseek-price-in", "1", "--deepseek-price-out", "2"])

    def test_numeric_ranges_are_validated(self):
        self._bad(["auto", "--output-dir", "/tmp/x",
                   "--confidence-threshold", "2"], "confidence-threshold")
        self._bad(["auto", "--output-dir", "/tmp/x",
                   "--confidence-threshold", "nan"], "finite")
        self._bad(["auto", "--output-dir", "/tmp/x", "--usd-cap", "nan"],
                  "finite")
        self._bad(["auto", "--output-dir", "/tmp/x", "--usd-cap", "-1"],
                  "nonnegative")
        self._bad(["auto", "--output-dir", "/tmp/x",
                   "--deepseek-price-out", "-2"], "nonnegative")
        self._bad(["auto", "--output-dir", "/tmp/x", "--token-cap", "-1"],
                  "token-cap")
        self._bad(["auto", "--output-dir", "/tmp/x",
                   "--episode-timeout", "inf"], "finite")
        self._bad(["auto", "--output-dir", "/tmp/x", "--strategy-deadline",
                   "-2"], "strategy-deadline")
        self._bad(["auto", "--output-dir", "/tmp/x", "--low-confidence-needs",
                   "0"], "low-confidence-needs")

    def test_postmortem_reserve_must_fit_the_cap(self):
        # a negative reserve would silently enlarge the play budget
        self._bad(["auto", "--output-dir", "/tmp/x", "--postmortem-reserve",
                   "-1"], "postmortem-reserve")
        self._bad(["auto", "--output-dir", "/tmp/x", "--strategy-call-cap",
                   "2", "--postmortem-reserve", "3"], "cannot exceed")
        self._ok(["auto", "--output-dir", "/tmp/x", "--strategy-call-cap",
                  "2", "--postmortem-reserve", "1"])

    def test_default_config_is_valid(self):
        self._ok(["auto", "--output-dir", "/tmp/x"])

    def test_cmd_auto_rejects_before_starting(self):
        out = io.StringIO()
        with mock.patch("sys.stderr", out):
            rc = agent_main.cmd_auto(self._args(
                ["auto", "--output-dir", "/tmp/x", "--usd-cap", "1"]))
        self.assertEqual(rc, 2)
        self.assertIn("complete tariff", out.getvalue())


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


class TestRecorderConstructionFailure(unittest.TestCase):
    """Medium 10: a partially constructed recorder leaks nothing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="auto-rec.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _patched(self, fail_on):
        """Patch recording with a _Writer stand-in that keeps a strong
        reference to every writer it builds -- so the test, not the garbage
        collector, decides when a descriptor is released -- and an
        _open_private that fails where *fail_on(n)* is true for the 1-based
        call number *n*."""
        created = []
        real_writer = recording._Writer

        class _TrackedWriter(real_writer):
            def __init__(self, *args, **kwargs):
                # tracked only once it is fully built: a writer whose own
                # construction fails holds no descriptor to release
                super().__init__(*args, **kwargs)
                created.append(self)

        real_open = recording._open_private
        calls = []

        def flaky(path, flags):
            calls.append(path)
            if fail_on(len(calls)):
                raise OSError(24, "too many open files")
            return real_open(path, flags)

        return created, (mock.patch.object(recording, "_Writer",
                                           _TrackedWriter),
                         mock.patch.object(recording, "_open_private",
                                           flaky))

    def _assert_released(self, writers):
        for w in writers:
            # the descriptor is closed, not merely dereferenced: no fd leaks
            self.assertTrue(w.fh.closed, "%s leaked" % w.path)
            with self.assertRaises(ValueError):
                w.fh.fileno()

    def test_partial_construction_closes_every_writer(self):
        # the third _open_private call (decisions) fails: both earlier
        # writers must be shut down, not just the first
        created, patches = self._patched(lambda n: n == 3)
        with patches[0], patches[1]:
            with self.assertRaises(OSError):
                recording.EpisodeRecorder(self.dir, 1)
        self.assertEqual(sorted(os.path.basename(w.path)
                                for w in created),
                         ["ep-1.actions.jsonl", "ep-1.wire.jsonl"])
        self._assert_released(created)

    def test_a_campaign_continues_and_leaks_no_descriptor(self):
        scen = _line(HELLO) + _line(obs(1, {"kind": "command", "id": 1})) \
            + _line(CLOSED)
        ctl = controller.Controller(
            ProviderConfig(max_ticks=50),
            controller.ControllerPaths("w", "r", "d", "s"), self.dir,
            episode_timeout=5.0)
        ctl._spawn = lambda priv: FakeProc(scen)
        # fail the fourth open of every episode (four writers per recorder)
        created, patches = self._patched(lambda n: n % 4 == 0)
        with patches[0], patches[1]:
            results = ctl.run_campaign(3)
        # the campaign kept going, each episode reported a recorder failure
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.recorder_failed for r in results))
        self.assertTrue(all(r.stop_reason == "recorder-failure"
                            for r in results))
        # three writers per doomed recorder: all nine, and no fd, leaked
        self.assertEqual(len(created), 9)
        self._assert_released(created)


if __name__ == "__main__":
    unittest.main()
