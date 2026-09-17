#!/usr/bin/env python3
"""Unit and integration tests for the tools/agent autoplay harness.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

The integration tests drive the real controller
(:class:`tools.agent.controller.Controller`) against an in-memory fake wire
(a pipe the controller reads exactly as it would a launcher), so the whole
request state machine -- pages, chunks, invalid recovery, retry caps,
EOF/closed -- is exercised without spawning a game.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (controller, policy, protocol,  # noqa: E402
                         recording, state)
from tools.agent.providers import ProviderConfig, ReflexContext  # noqa: E402


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

    def close(self):
        try:
            self.wr.close()
        except Exception:
            pass
        try:
            os.close(self.r)
        except OSError:
            pass


class WireHarness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="auto-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def run_scenario(self, scenario: bytes, eof: bool = True,
                     timeout: float = 10.0, max_ticks: int = 200):
        config = ProviderConfig(max_ticks=max_ticks)
        ctl = controller.Controller(
            config, controller.ControllerPaths(
                worker="w", runner="r", data="d", sysconf="s"),
            self.dir, episode_timeout=timeout)
        proc = FakeProc(scenario, eof=eof)
        ctl._spawn = lambda priv: proc  # deterministic: no real subprocess
        try:
            result = ctl.run_episode(1)
            actions = _parse_actions(proc.stdin.data)
        finally:
            proc.close()
        return result, actions


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _parse_actions(data: bytes):
    out = []
    for raw in data.split(b"\n"):
        if raw.strip():
            out.append(json.loads(raw))
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

CLOSED = {"v": 1, "ch": "control", "type": "closed"}


# ============================================================ unit tests

class TestValidation(unittest.TestCase):
    def test_every_need_kind_accepts_its_shape(self):
        r = None
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
        self.assertEqual(len(req.page_requests()), 2)
        self.assertFalse(req.pages_complete())
        req.requested = {0, 1}
        req.note_page(page("c5", 0, 2, [row(1, "a")]))
        self.assertFalse(req.pages_complete())
        req.note_page(page("c5", 1, 2, [row(2, "b")]))
        self.assertTrue(req.pages_complete())
        rows = req.page_rows()
        self.assertEqual([r["r"] for r in rows], [1, 2])

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

    def test_eat_prompt_uses_a_bracketed_letter(self):
        self.ref.intent = "eat"
        need = yn_need(1, "What do you want to eat? [d or ?*]")
        res = self.ref.decide(self.ctx(need))
        self.assertEqual(res.action, {"yn": ord("d")})

    def test_eat_loop_breaker_after_two_rejections(self):
        self.ref.intent = "eat"
        need = yn_need(1, "What do you want to eat? [d or ?*]")
        ctx = self.ctx(need, msg=["You don't have that object.",
                                  "You don't have that object."])
        res = self.ref.decide(ctx)
        self.assertEqual(res.action, {"yn": ord("*")})  # open the menu

    def test_search_and_move_loop_breakers(self):
        hero = (10, 10)
        self.mem.hero = hero
        self.mem.grid[(10, 10)] = ("@", "white", 0, "none")
        self.mem.grid[(9, 10)] = ('|', "gray", 0, "none")
        self.mem.no_progress = 3
        res = self.ref.decide(self.ctx({"kind": "command", "id": 1}))
        self.assertEqual(res.action, {"key": protocol.KEY_SEARCH})
        self.mem.no_progress = 7
        res = self.ref.decide(self.ctx({"kind": "command", "id": 2}))
        self.assertIn(res.action["key"], list(protocol.DIR_KEYS.values()))
        # a monster blocking the only route is stepped into (a pet swaps)
        self.mem.grid[(9, 10)] = ("d", "white", 32, "none")
        self.mem.no_progress = 12
        res = self.ref.decide(self.ctx({"kind": "command", "id": 3}))
        self.assertEqual(res.action["key"], protocol.DIR_KEYS[(-1, 0)])

    def test_fresh_instance_resets_per_episode(self):
        self.ref.selection_done = True
        self.ref.intent = "eat"
        fresh = policy.ScriptedReflex(ProviderConfig())
        self.assertFalse(fresh.selection_done)
        self.assertEqual(fresh.intent, "")


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
        # the ref declined auto-pick, chose Valkyrie, declined the tutorial
        acts = [a for a in actions if a.get("type") == "act"]
        self.assertIn({"v": 1, "type": "act", "id": 1, "seq": 1,
                       "action": {"yn": protocol.KEY_N}}, acts)
        self.assertTrue(any(a["action"].get("commit") == [[14, -1]]
                            for a in acts))
        self.assertTrue(any(a["action"].get("commit") == [[2, -1]]
                            for a in acts))

    def test_two_page_menu_and_ack(self):
        rows0 = [row(1, "heading", selectable=False), row(2, "an apple")]
        rows1 = [row(3, "a banana")]
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
        self.assertEqual(commits[0]["action"]["commit"], [[2, -1]])
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
        rows = [row(1, "an apple")]
        scen = b"".join([
            _line(HELLO),
            _line(obs_menu(1, 10, "m10", "c10", "an apple menu")),
            _line(page("c10", 0, 1, rows)),
            _line(obs_menu(2, 11, "m11", "c11", "an apple menu")),
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


class TestRecording(unittest.TestCase):
    def test_incomplete_when_a_writer_errors(self):
        d = tempfile.mkdtemp(prefix="auto-rec.")
        rec = recording.EpisodeRecorder(d, 1)
        rec.record_wire(b'{"type":"hello"}\n')
        rec._wire.error = "disk full"     # simulate a disk failure
        meta = rec.finalize({})
        self.assertFalse(meta["recording_complete"])

    def test_complete_recording_round_trips(self):
        d = tempfile.mkdtemp(prefix="auto-rec.")
        rec = recording.EpisodeRecorder(d, 2)
        rec.record_wire(b'{"type":"hello"}\n')
        rec.record_action(1, 0, protocol.NeedKey(2, 1, 1), {"key": 46},
                          "sent")
        rec.record_decision({"key": 46}, {"key": 46}, "scripted", "navigate")
        meta = rec.finalize({"stop_reason": "closed"})
        self.assertTrue(meta["recording_complete"])
        self.assertEqual(meta["wire_lines"], 1)
        with open(os.path.join(d, "ep-2.actions.jsonl")) as fh:
            self.assertEqual(json.loads(fh.readline())["action"], {"key": 46})


class TestWriter(unittest.TestCase):
    def test_full_queue_is_reported(self):
        d = tempfile.mkdtemp(prefix="auto-rec.")
        w = recording._Writer(os.path.join(d, "x"), maxsize=1)
        self.assertTrue(w.submit(b"a"))
        self.assertFalse(w.submit(b"b"))   # queue full, dropped
        self.assertEqual(w.dropped, 1)
        w.shutdown()


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
