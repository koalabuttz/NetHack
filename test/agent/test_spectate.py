#!/usr/bin/env python3
"""Integration tests and benchmarks for spectate.py and format_obs.py.

Standard-library unittest.  Everything here is engine-free and runs without a
NetHack build: the launcher side is a synthetic Python stub and the render
side is either a capture pipe or a leftover fd.  The native wrapper runs in
``doc/agent-spectate-revision.md`` section 5 belong to the opt-in driver
targets, not to this file.

Usage:
    python3 -m unittest discover -s test/agent -p test_spectate.py
    python3 test/agent/test_spectate.py --benchmark --sizes 10000,50000 \\
        --workload plain --output /tmp/spec-perf.json
    python3 test/agent/test_spectate.py --byte-exact /tmp/spec-bytecheck
"""

import fcntl
import io
import json
import os
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import format_obs  # noqa: E402
import spectate  # noqa: E402

SPECTATE = os.path.join(HERE, "spectate.py")

STUB = r'''#!/usr/bin/env python3
"""Synthetic launcher for the spectate tests."""
import fcntl
import json
import os
import signal
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def obs(seq):
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None,
            "s": {"time": {"text": str(seq), "color": "none", "style": 0}},
            "cond": [], "pal": [[0, " ", "none", 0, "none"],
                                [1, "@", "white", 0, "none"]],
            "map": [[8, 10, 1]], "cur": [8, 11],
            "msg": [{"e": 1, "text": "m%d" % seq, "style": 0}],
            "hist": [], "windows": [], "need": {"id": 1, "kind": "command"}}


def nonblock(fd):
    return bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)


mode = os.environ.get("STUB_MODE", "records")
if mode == "argv":
    emit({"argv": sys.argv[1:]})
elif mode == "flags":
    emit({"fd0_nonblock": nonblock(0), "fd1_nonblock": nonblock(1),
          "fd2_nonblock": nonblock(2), "fd1_isatty": os.isatty(1)})
elif mode == "cat":
    with open(os.environ["STUB_FILE"], "rb") as fh:
        data = fh.read()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    try:
        sys.stdin.buffer.read()
    except OSError:
        pass
elif mode == "records":
    count = int(os.environ.get("STUB_COUNT", "5"))
    emit({"v": 1, "ch": "control", "type": "hello", "profile": "p",
          "policy": "x", "caps": ["a"], "size": 1, "limits": {}})
    for i in range(1, count + 1):
        emit(obs(i))
    emit({"v": 1, "ch": "control", "type": "closed"})
elif mode == "bulk":
    total = int(os.environ["STUB_BYTES"])
    pad = int(os.environ.get("STUB_LINE", "200"))
    written = 0
    index = 0
    while written < total:
        index += 1
        raw = (json.dumps({"i": index, "pad": "\u00e9" * pad})
               + "\n").encode("utf-8")
        sys.stdout.buffer.write(raw)
        written += len(raw)
    sys.stdout.buffer.write(b'{"i": %d, "unterminated": tr' % (index + 1))
    sys.stdout.buffer.flush()
elif mode == "echo":
    emit({"v": 1, "ch": "control", "type": "closed"})
    line = sys.stdin.readline()
    emit({"argv": [line.rstrip("\n")]})
elif mode == "hang":
    emit({"v": 1, "ch": "control", "type": "closed"})
    sys.stdout.flush()
    os.close(1)          # real stdout EOF while the process keeps running
    time.sleep(120)
elif mode == "signal":
    emit({"v": 1, "ch": "control", "type": "closed"})
    name = os.environ.get("STUB_SIGNAL", "SIGTERM")
    os.kill(os.getpid(), getattr(signal, name))
    time.sleep(5)
else:
    sys.stderr.write("unknown STUB_MODE %r\n" % mode)
    sys.exit(3)
'''


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def make_stub(directory):
    path = os.path.join(directory, "stub-launcher.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(STUB)
    os.chmod(path, 0o755)
    return path


def obs_record(seq, rid=None):
    return {"v": 1, "ch": "player", "type": "obs", "d": rid or seq,
            "seq": seq, "base": None,
            "s": {"time": {"text": str(seq), "color": "none", "style": 0}},
            "cond": [], "pal": [[0, " ", "none", 0, "none"],
                                [1, "@", "white", 0, "none"]],
            "map": [[8, 10, 1]], "cur": [8, 11],
            "msg": [{"e": 1, "text": "m%d" % seq, "style": 0}],
            "hist": [], "windows": [],
            "need": {"id": 1, "kind": "command"}}


def obs_line(seq, rid=None):
    return (json.dumps(obs_record(seq, rid)) + "\n").encode("utf-8")


def chunk_pair(rid, seq):
    """The two chunk lines one chunked observation of ``seq`` becomes.

    The rebuilt record is byte-identical to ``obs_record(seq, rid)``, so the
    chunked and plain renderings must match exactly.
    """
    head = [{"p": "h", "k": "v", "val": 1},
            {"p": "h", "k": "ch", "val": "player"},
            {"p": "h", "k": "seq", "val": seq},
            {"p": "h", "k": "base", "val": None}]
    first_parts = head + [
        {"p": "s", "k": "time",
         "val": {"text": str(seq), "color": "none", "style": 0}},
        {"p": "pal", "val": [0, " ", "none", 0, "none"]},
        {"p": "pal", "val": [1, "@", "white", 0, "none"]},
        {"p": "map", "val": [8, 10, 1]},
        {"p": "cur", "val": [8, 11]}]
    second_parts = [
        {"p": "msg", "val": {"e": 1, "text": "m%d" % seq, "style": 0}},
        {"p": "need", "val": {"id": 1, "kind": "command"}}]
    first = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": rid,
             "i": 0, "last": False, "parts": first_parts}
    second = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": rid,
              "i": 1, "last": True, "parts": second_parts}
    return ((json.dumps(first) + "\n" + json.dumps(second) + "\n")
            .encode("utf-8"))


def spectate_argv(*args):
    return [sys.executable, SPECTATE] + list(args)


# Implicit mode still needs at least one launcher argument: with no arguments
# spectate prints usage (the documented first-token dispatch is preserved).
IMPLICIT_PROBE = "--probe"


def clean_env(**extra):
    env = dict(os.environ)
    for name in ("SPECTATE_LAUNCHER", "SPECTATE_TRANSCRIPT",
                 "SPECTATE_RENDER_FD", "SPECTATE_MESSAGES",
                 "SPECTATE_MIN_FRAME_INTERVAL", "SPECTATE_REPLAY_SPEED",
                 "SPECTATE_NO_COLOR", "NO_COLOR"):
        env.pop(name, None)
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def drain_thread(fd, sink):
    """Read ``fd`` until EOF or the sink is closed, appending to ``sink``."""
    while True:
        try:
            ready = select.select([fd], [], [], 0.2)[0]
        except OSError:
            return
        if not ready:
            continue
        try:
            data = os.read(fd, 1 << 16)
        except OSError:
            return
        if not data:
            return
        sink.append(data)


def group_gone(pid):
    """True when no process remains in the process group ``pid``."""
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class FdStream(object):
    """A stand-in for sys.stdout/sys.stdin that exposes a chosen fd."""

    def __init__(self, fd):
        self._fd = fd

    def fileno(self):
        return self._fd

    def write(self, _text):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


class PatchedStdio(object):
    """Temporarily point spectate's relay I/O at test-owned descriptors."""

    def __init__(self, stdout_fd=None, stdin_fd=None):
        self.stdout_fd = stdout_fd
        self.stdin_fd = stdin_fd

    def __enter__(self):
        self._out = sys.stdout
        self._in = sys.stdin
        if self.stdout_fd is not None:
            sys.stdout = FdStream(self.stdout_fd)
        if self.stdin_fd is not None:
            sys.stdin = FdStream(self.stdin_fd)
        return self

    def __exit__(self, *exc):
        sys.stdout = self._out
        sys.stdin = self._in
        return False


def make_session(dest_fd=None, interval=0.0, color=False, drain=True):
    """A live session over a fresh capture pipe (helper + worker real)."""
    read_fd, write_fd = os.pipe()
    dest = spectate.RenderDestination(write_fd, True, False, "capture")
    session = spectate.LiveRenderSession(
        dest, messages=3, color=color, min_frame_interval=interval)
    if dest_fd is not None:
        raise AssertionError("dest_fd is not used")
    session.start()
    captured = []
    reader = None
    if drain:
        reader = threading.Thread(target=drain_thread,
                                  args=(read_fd, captured))
        reader.daemon = True
        reader.start()
    return session, read_fd, captured, reader


# ------------------------------------------------------------------
# Step A -- incremental assembler
# ------------------------------------------------------------------


class AssemblerTests(unittest.TestCase):
    def setUp(self):
        self.head = [{"p": "h", "k": "v", "val": 1},
                     {"p": "h", "k": "ch", "val": "player"},
                     {"p": "h", "k": "seq", "val": 1},
                     {"p": "h", "k": "base", "val": None}]
        self.stream = [
            self.head + [{"p": "msg", "val": {"e": 7, "text": "",
                                              "style": 0}}],
            [{"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 0,
              "text": "abc", "last": False},
             {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 3,
              "text": "def", "last": True}]]

    def chunk(self, i, parts, last=None, rid=1):
        return {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": rid,
                "i": i, "last": len(self.stream) - 1 == i if last is None
                else last, "parts": parts}

    def batch(self, records):
        return list(format_obs.assemble(json.dumps(r) for r in records))

    def incremental(self, records, asm=None):
        asm = asm or format_obs.IncrementalAssembler()
        out = []
        for rec in records:
            out.extend(asm.feed(json.dumps(rec)))
        return out, asm

    def lines(self):
        return [self.chunk(i, parts) for i, parts in enumerate(self.stream)]

    def test_unchunked_and_chunked_agree(self):
        plain = [{"type": "obs", "d": 1, "seq": 1},
                 {"type": "closed"}]
        inc, _ = self.incremental(plain)
        self.assertEqual(inc, self.batch(plain))
        lines = self.lines()
        inc, _ = self.incremental(lines)
        self.assertEqual(len(inc), 1)
        self.assertEqual(inc[0]["msg"][0]["text"], "abcdef")
        self.assertEqual(inc, self.batch(lines))

    def test_adjacent_duplicates_deduplicate(self):
        records = []
        for line in self.lines():
            records.append(line)
            records.append(json.loads(json.dumps(line)))
        inc, _ = self.incremental(records)
        self.assertEqual(inc, self.batch(records))
        self.assertEqual(len(inc), 1)

    def test_retry_after_completion_and_intervening(self):
        records = self.lines() + [{"type": "closed"}] + \
            [json.loads(json.dumps(line)) for line in self.lines()]
        inc, _ = self.incremental(records)
        batch = self.batch(records)
        self.assertEqual(inc, batch)
        # the completed stream is retried silently: one record, no extra obs
        self.assertEqual([r["type"] for r in inc], ["obs", "closed"])

    def test_changed_duplicate_rejected(self):
        lines = self.lines()
        altered = json.loads(json.dumps(lines[0]))
        altered["parts"] = self.head + [
            {"p": "msg", "val": {"e": 7, "text": "x", "style": 0}}]
        with self.assertRaises(format_obs.ChunkError):
            self.incremental([lines[0], altered])
        # the same vector is rejected after completion too
        with self.assertRaises(format_obs.ChunkError):
            self.incremental(lines + [altered])

    def test_gap_rejected(self):
        with self.assertRaises(format_obs.ChunkError):
            self.incremental([self.chunk(0, self.stream[0]),
                              self.chunk(2, self.stream[1], last=True)])

    def test_header_only_in_chunk_zero(self):
        with self.assertRaises(format_obs.ChunkError):
            self.incremental([self.chunk(0, self.head),
                              self.chunk(1, [{"p": "h", "k": "seq",
                                              "val": 1}], last=True)])

    def test_text_slice_utf8_offsets(self):
        # offsets are byte offsets: a multi-byte slice advances by its bytes
        head = self.head
        good = [head + [{"p": "msg", "val": {"e": 7, "text": "",
                                             "style": 0}}],
                [{"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 0,
                  "text": "\u00e9", "last": False},
                 {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 2,
                  "text": "z", "last": True}]]
        recs = [self.chunk(i, parts, last=i == 1)
                for i, parts in enumerate(good)]
        inc, _ = self.incremental(recs)
        self.assertEqual(inc[0]["msg"][0]["text"], "\u00e9z")
        bad = [head + [{"p": "msg", "val": {"e": 7, "text": "",
                                            "style": 0}}],
               [{"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 0,
                 "text": "\u00e9", "last": False},
                {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 1,
                 "text": "z", "last": True}]]
        recs = [self.chunk(i, parts, last=i == 1)
                for i, parts in enumerate(bad)]
        with self.assertRaises(format_obs.ChunkError):
            self.incremental(recs)

    def test_incomplete_eof_reported_and_dropped(self):
        asm = format_obs.IncrementalAssembler()
        out = asm.feed(json.dumps(self.chunk(0, self.stream[0])))
        self.assertEqual(out, [])
        notes = asm.finish()
        self.assertTrue(notes and "incomplete" in notes[0])

    def test_malformed_shapes(self):
        asm = format_obs.IncrementalAssembler()
        with self.assertRaises(ValueError):
            asm.feed("not json")
        with self.assertRaises(KeyError):
            asm.feed(json.dumps({"type": "chunk"}))
        self.assertEqual(asm.feed(""), [])
        self.assertEqual(asm.feed("   "), [])

    def test_oversized_line_is_a_budget_not_a_protocol_error(self):
        asm = format_obs.IncrementalAssembler(max_line_bytes=8)
        with self.assertRaises(format_obs.AssemblerLimit):
            asm.feed(json.dumps(self.stream[0]) + "padding")
        self.assertEqual(asm._streams, {})

    def test_cap_exhaustion_clears_state(self):
        for kwargs in ({"max_chunks": 2}, {"max_streams": 1},
                       {"max_retained_bytes": 64}):
            asm = format_obs.IncrementalAssembler(**kwargs)
            hit = False
            try:
                for rid in range(1, 8):
                    asm.feed(json.dumps(self.chunk(0, self.stream[0],
                                                   last=False, rid=rid)))
                    asm.feed(json.dumps(self.chunk(1, self.stream[1],
                                                   last=True, rid=rid)))
            except format_obs.AssemblerLimit:
                hit = True
            self.assertTrue(hit, kwargs)
            self.assertEqual(asm._streams, {}, kwargs)
            self.assertEqual(asm._chunks, 0, kwargs)
            self.assertEqual(asm.finish(), [], kwargs)

    def test_cap_does_not_affect_batch_output(self):
        # batch assembly (no budgets) keeps producing the same records
        lines = self.lines()
        self.assertEqual(len(self.batch(lines)), 1)


# ------------------------------------------------------------------
# Step B -- timed render pipeline, transport and shutdown
# ------------------------------------------------------------------


class RenderTests(unittest.TestCase):
    def test_idle_deadline_renders_without_more_input(self):
        session, read_fd, captured, reader = make_session(interval=0.2)
        try:
            session.submit_bytes(obs_line(1))
            time.sleep(0.1)
            session.submit_bytes(obs_line(2))
            time.sleep(0.8)          # no further input: deadline must draw
            early = b"".join(captured)
            self.assertIn(b"seq=1", early)
            self.assertIn(b"seq=2", early)
        finally:
            session.finish()
            session.close()
            reader.join(1.0)
            os.close(read_fd)

    def test_eof_flushes_newest_and_joins(self):
        baseline = threading.active_count()
        session, read_fd, captured, reader = make_session(interval=30.0)
        session.submit_bytes(obs_line(1))
        time.sleep(0.2)
        session.submit_bytes(obs_line(2))
        stats = session.finish(timeout=3.0)
        session.close()
        reader.join(1.0)
        os.close(read_fd)
        text = b"".join(captured).decode("utf-8", "replace")
        # the throttled newest obs is forced out at EOF and acknowledged
        self.assertIn("seq=2", text)
        self.assertGreaterEqual(stats["render_frames_written"], 2)
        self.assertEqual(stats["render_frames_dropped"], 0)
        time.sleep(0.2)
        self.assertLessEqual(threading.active_count(), baseline)

    def test_permanently_unread_pipe_disables_and_shuts_down(self):
        read_fd, write_fd = os.pipe()
        dest = spectate.RenderDestination(write_fd, True, False, "unread")
        session = spectate.LiveRenderSession(dest, min_frame_interval=0.0)
        session.start()
        started = time.monotonic()
        # the first frame fits and is acknowledged; the second cannot fit and
        # the destination is never read, so the pipeline must give up
        session.submit_bytes(obs_line(1))
        time.sleep(0.3)
        huge = obs_record(2)
        huge["msg"] = [{"e": 1, "text": "x" * 200000, "style": 0}]
        session.submit_bytes((json.dumps(huge) + "\n").encode())
        session.finish(timeout=5.0)
        elapsed = time.monotonic() - started
        stats = session.stats()
        session.close()
        os.close(read_fd)
        self.assertTrue(stats["render_disabled"], stats)
        self.assertTrue(stats["render_output_delivered"], stats)
        self.assertIn("stalled", stats["render_disabled_reason"] or "")
        self.assertLess(elapsed, 20.0)
        self.assertIsNotNone(session._helper.poll())

    def test_fd_flags_unchanged_in_relay_and_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            render_r, render_w = os.pipe()
            before = fcntl.fcntl(render_w, fcntl.F_GETFL)
            env = clean_env(STUB_MODE="flags", SPECTATE_LAUNCHER=stub)
            proc = subprocess.Popen(
                spectate_argv(IMPLICIT_PROBE), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=render_w, close_fds=True)
            out, _ = proc.communicate(b"", timeout=60)
            after = fcntl.fcntl(render_w, fcntl.F_GETFL)
            os.close(render_w)
            os.close(render_r)
            record = json.loads(out.decode().splitlines()[0])
            # the launcher's own descriptors were never made non-blocking
            self.assertFalse(record["fd0_nonblock"])
            self.assertFalse(record["fd1_nonblock"])
            self.assertFalse(record["fd2_nonblock"])
            self.assertEqual(proc.returncode, 0)
            # and the caller's render descriptor keeps its flags too: the
            # helper duplicates it but never calls F_SETFL on it
            self.assertEqual(before & os.O_NONBLOCK, after & os.O_NONBLOCK)

    def test_render_fd_flags_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            render_r, render_w = os.pipe()
            before = fcntl.fcntl(render_w, fcntl.F_GETFL)
            env = clean_env(STUB_MODE="records", STUB_COUNT="2",
                            SPECTATE_LAUNCHER=stub,
                            SPECTATE_RENDER_FD=str(render_w))
            proc = subprocess.Popen(
                spectate_argv(IMPLICIT_PROBE), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(render_w,))
            proc.communicate(b"", timeout=60)
            after = fcntl.fcntl(render_w, fcntl.F_GETFL)
            os.close(render_r)
            os.close(render_w)
            self.assertEqual(before & os.O_NONBLOCK,
                             after & os.O_NONBLOCK)
            self.assertEqual(proc.returncode, 0)

    def test_live_fd1_and_alias_rejected(self):
        with self.assertRaises(spectate.UsageError):
            spectate.open_render("1", live=True)
        read_fd, write_fd = os.pipe()
        saved = os.dup(1)
        try:
            os.dup2(write_fd, 1)
            # fd 3 is an alias of the consumer stdout: refusing it is the
            # whole point of the destination policy
            with self.assertRaises(spectate.UsageError):
                spectate.open_render(str(write_fd), live=True)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(read_fd)
            os.close(write_fd)
        # replay legitimately keeps fd 1
        dest = spectate.open_render("1", live=False)
        dest.close()

    def test_replay_stdout_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "wb") as fh:
                fh.write(obs_line(1))
                fh.write(b'{"v": 1, "ch": "control", "type": "closed"}\n')
            proc = subprocess.run(
                spectate_argv("replay", path, "--no-color"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b"seq=1", proc.stdout)
            self.assertIn(b"closed", proc.stdout)

    def test_helper_death_midframe_disables(self):
        read_fd, write_fd = os.pipe()
        dest = spectate.RenderDestination(write_fd, True, False, "victim")
        session = spectate.LiveRenderSession(dest, min_frame_interval=0.0)
        session.start()
        session.submit_bytes(obs_line(1))
        time.sleep(0.2)
        session._helper.kill()
        session._helper.wait()
        for i in range(2, 40):
            session.submit_bytes(obs_line(i))
        session.finish(timeout=5.0)
        session.close()
        os.close(read_fd)
        stats = session.stats()
        # the worker noticed the dead helper rather than hanging or lying
        self.assertTrue(stats["render_destination_error"] or
                        stats["render_disabled"], stats)

    def test_ingress_overflow_disables_with_byte_count(self):
        # a producer far faster than the renderer must not grow without bound
        read_fd, write_fd = os.pipe()
        dest = spectate.RenderDestination(write_fd, True, False, "slow")
        session = spectate.LiveRenderSession(
            dest, min_frame_interval=30.0, ingress_limit=4096)
        session.start()
        try:
            for i in range(1, 4000):
                session.submit_bytes(obs_line(i))
                if session.disabled:
                    break
            stats = session.stats()
        finally:
            session.finish(timeout=5.0)
            session.close()
            os.close(read_fd)
        self.assertTrue(stats["render_disabled"], stats)
        self.assertIn("render input dropped",
                      stats["render_disabled_reason"] or "")
        self.assertGreater(stats["render_input_dropped_bytes"], 0)

    def test_coalescing_statistics(self):
        session, read_fd, captured, reader = make_session(interval=30.0)
        try:
            session.submit_bytes(obs_line(1))
            time.sleep(0.2)
            for i in range(2, 6):
                session.submit_bytes(obs_line(i))
            stats = session.finish(timeout=3.0)
        finally:
            session.close()
            reader.join(1.0)
            os.close(read_fd)
        self.assertGreaterEqual(stats["render_frames_coalesced"], 3)
        self.assertGreaterEqual(stats["render_queue_high_water"], 0)
        self.assertFalse(stats["render_disabled"])

    def test_transport_envelope_integrity_under_partial_writes(self):
        # a very large frame forces the internal pipe to take several writes
        read_fd, write_fd = os.pipe()
        dest = spectate.RenderDestination(write_fd, True, False, "big")
        session = spectate.LiveRenderSession(dest, min_frame_interval=0.0)
        session.start()
        captured = []
        reader = threading.Thread(target=drain_thread,
                                  args=(read_fd, captured))
        reader.daemon = True
        reader.start()
        big = obs_record(1)
        big["msg"] = [{"e": i, "text": "x" * 80000, "style": 0}
                      for i in (1, 2, 3)]
        session.submit_bytes((json.dumps(big) + "\n").encode())
        session.finish(timeout=10.0)
        reader.join(2.0)
        session.close()
        os.close(read_fd)
        text = b"".join(captured).decode("utf-8", "replace")
        self.assertIn("msg[1]", text)
        self.assertIn("msg[3]", text)
        self.assertIn("need: command", text)
        stats = session.stats()
        self.assertEqual(stats["render_frames_dropped"], 0)
        self.assertEqual(stats["render_frames_written"], 1)


# ------------------------------------------------------------------
# Step C -- wire delivery, transcript, argv, failures
# ------------------------------------------------------------------


class ProxyTests(unittest.TestCase):
    def test_backpressure_bytes_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            source = os.path.join(tmp, "source.bin")
            payload = bytearray()
            index = 0
            while len(payload) < (8 << 20):
                index += 1
                payload.extend((json.dumps(
                    {"i": index, "pad": "\u00e9" * 400}) + "\n")
                    .encode("utf-8"))
            payload.extend(b'{"i": 0, "partial": tr')   # unterminated tail
            with open(source, "wb") as fh:
                fh.write(payload)
            transcript = os.path.join(tmp, "t.jsonl")
            render_r, render_w = os.pipe()
            env = clean_env(STUB_MODE="cat", STUB_FILE=source,
                            SPECTATE_LAUNCHER=stub,
                            SPECTATE_RENDER_FD=str(render_w),
                            SPECTATE_TRANSCRIPT=transcript)
            proc = subprocess.Popen(
                spectate_argv(IMPLICIT_PROBE), env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(render_w,))
            proc.stdin.close()      # consumer EOF propagates to the launcher
            captured = []
            reader = threading.Thread(target=drain_thread,
                                      args=(render_r, captured))
            reader.daemon = True
            reader.start()
            # read the consumer side slowly, in odd-sized pieces
            consumed = bytearray()
            while True:
                chunk = proc.stdout.read(12345)
                if not chunk:
                    break
                consumed.extend(chunk)
                time.sleep(0.001)
            proc.wait(timeout=120)
            reader.join(2.0)
            os.close(render_r)
            os.close(render_w)
            self.assertEqual(proc.returncode, 0, proc.stderr.read())
            self.assertEqual(bytes(consumed), bytes(payload))
            with open(transcript, "rb") as fh:
                self.assertEqual(fh.read(), bytes(payload))
            self.assertTrue(group_gone(proc.pid))

    def test_consumer_close_transcript_is_confirmed_prefix(self):
        with self.assertRaises(spectate._ConsumerError):
            self._unit_short_writes()
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            source = os.path.join(tmp, "source.bin")
            with open(source, "wb") as fh:
                fh.write(b"a" * 300000)
            transcript = os.path.join(tmp, "t.jsonl")
            render_r, render_w = os.pipe()
            env = clean_env(STUB_MODE="cat", STUB_FILE=source,
                            SPECTATE_LAUNCHER=stub,
                            SPECTATE_RENDER_FD=str(render_w),
                            SPECTATE_TRANSCRIPT=transcript)
            proc = subprocess.Popen(
                spectate_argv(IMPLICIT_PROBE), env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(render_w,), start_new_session=True)
            os.close(render_w)
            seen = proc.stdout.read(1234)     # deliberately not line aligned
            proc.stdout.close()
            proc.wait(timeout=60)
            os.close(render_r)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(group_gone(proc.pid))
            with open(transcript, "rb") as fh:
                recorded = fh.read()
            # exactly a confirmed prefix of the launcher output
            self.assertEqual(recorded, b"a" * len(recorded))
            self.assertGreaterEqual(len(recorded), len(seen))
            self.assertLessEqual(len(recorded), 300000)
            err = proc.stderr.read()
            self.assertIn(b"consumer stdout", err)

    def _unit_short_writes(self):
        accepted = []
        state = {"calls": 0}
        real_write = os.write

        def fake_write(fd, view):
            if state["calls"] >= 2:
                raise BrokenPipeError(32, "broken pipe")
            state["calls"] += 1
            take = min(3, len(view))
            accepted.append(bytes(view[:take]))
            return take

        sink = io.BytesIO()
        os.write = fake_write
        try:
            spectate._deliver(1, b"0123456789", sink)
        finally:
            os.write = real_write
        self.assertEqual(sink.getvalue(), b"".join(accepted))
        self.assertEqual(sink.getvalue(), b"012345")
        raise spectate._ConsumerError("expected")

    def test_argv_identity_implicit_and_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            tail = ["--worker", "a b", "", "--", "--quiet", "--messages",
                    "1", "--render-fd=1", "--", "x y", "-q", "3"]
            proc = subprocess.run(
                spectate_argv(*tail),
                env=clean_env(STUB_MODE="argv", SPECTATE_LAUNCHER=stub,
                              SPECTATE_RENDER_FD="2",
                              SPECTATE_TRANSCRIPT=os.devnull),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=60, input=b"")
            self.assertEqual(proc.returncode, 0)
            record = json.loads(proc.stdout.decode().splitlines()[0])
            self.assertEqual(record["argv"], tail)

    def test_argv_identity_explicit_wrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            tail = ["a b", "--", "--quiet", ""]
            proc = subprocess.run(
                spectate_argv("wrap", "--launcher", stub) + ["--"] + tail,
                env=clean_env(STUB_MODE="argv",
                              SPECTATE_TRANSCRIPT=os.devnull),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=60, input=b"")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            record = json.loads(proc.stdout.decode().splitlines()[0])
            self.assertEqual(record["argv"], tail)

    def test_argv_identity_runner_shaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            tail = ["--worker", "/repo/src/nethack", "--private-root",
                    "/tmp/spec/private", "--data", "/tmp/agent-data",
                    "--sysconf", "/tmp/agent-data/sysconf",
                    "--deadline", "30"]
            proc = subprocess.run(
                spectate_argv(*tail),
                env=clean_env(STUB_MODE="argv", SPECTATE_LAUNCHER=stub,
                              SPECTATE_TRANSCRIPT=os.devnull),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=60, input=b"")
            record = json.loads(proc.stdout.decode().splitlines()[0])
            self.assertEqual(record["argv"], tail)


class FailureTests(unittest.TestCase):
    def run_wrap(self, stub, extra_env=None, args=None, timeout=60,
                 render=True):
        env = clean_env(STUB_MODE="records", STUB_COUNT="2",
                        SPECTATE_LAUNCHER=stub)
        if extra_env:
            env.update(extra_env)
        kwargs = {}
        if render:
            render_r, render_w = os.pipe()
            env["SPECTATE_RENDER_FD"] = str(render_w)
            kwargs["pass_fds"] = (render_w,)
        proc = subprocess.run(
            spectate_argv(*(args or (IMPLICIT_PROBE,))), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, input=b"", **kwargs)
        if render:
            os.close(render_r)
            os.close(render_w)
        return proc

    def test_numeric_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            for bad in (["--messages", "-1"], ["--messages", "x"],
                        ["--min-frame-interval", "nan"],
                        ["--min-frame-interval", "-1"],
                        ["--render-fd", "notanumber"]):
                proc = self.run_wrap(stub, args=["wrap", "--launcher", stub]
                                     + bad + ["--"], render=False)
                self.assertEqual(proc.returncode, 2, (bad, proc.stderr))
                self.assertNotIn(b"Traceback", proc.stderr)
            proc = self.run_wrap(
                stub, args=["replay", "/dev/null", "--replay-speed", "inf"],
                render=False)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn(b"Traceback", proc.stderr)

    def test_bad_render_fd_is_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            proc = self.run_wrap(
                stub, extra_env={"SPECTATE_RENDER_FD": "999"},
                render=False)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)
            proc = self.run_wrap(
                stub, extra_env={"SPECTATE_RENDER_FD": "-4"}, render=False)
            self.assertEqual(proc.returncode, 2)

    def test_missing_and_nonexecutable_launcher(self):
        proc = subprocess.run(
            spectate_argv(IMPLICIT_PROBE),
            env=clean_env(SPECTATE_LAUNCHER="/nonexistent/launcher"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, input=b"")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(b"Traceback", proc.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "not-executable")
            with open(plain, "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            proc = subprocess.run(
                spectate_argv(IMPLICIT_PROBE),
                env=clean_env(SPECTATE_LAUNCHER=plain),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30, input=b"")
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)

    def test_transcript_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            proc = self.run_wrap(
                stub, extra_env={"SPECTATE_TRANSCRIPT": tmp}, render=False)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)
            blocked = os.path.join(tmp, "dir", "t.jsonl")
            proc = self.run_wrap(
                stub, extra_env={"SPECTATE_TRANSCRIPT": blocked},
                render=False)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)

    def test_launcher_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            for name, code in (("SIGTERM", 143), ("SIGKILL", 137)):
                proc = self.run_wrap(
                    stub, extra_env={"STUB_MODE": "signal",
                                     "STUB_SIGNAL": name}, render=False)
                self.assertEqual(proc.returncode, code, name)
                self.assertNotIn(b"Traceback", proc.stderr)

    def test_downstream_epipe_stops_promptly(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            source = os.path.join(tmp, "source.bin")
            with open(source, "wb") as fh:
                fh.write(b"z" * 200000)
            proc = subprocess.Popen(
                spectate_argv("wrap", "--launcher", stub, "--quiet", "--"),
                env=clean_env(STUB_MODE="cat", STUB_FILE=source),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
            proc.stdout.close()
            proc.wait(timeout=60)
            err = proc.stderr.read()
            self.assertEqual(proc.returncode, 1)
            self.assertIn(b"consumer stdout", err)
            self.assertNotIn(b"Traceback", err)
            self.assertTrue(group_gone(proc.pid))

    def test_launcher_stdout_eof_then_hang(self):
        # stdout closes, the process does not: the wrapper must not wait
        # forever, and must reap what it started
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            session, read_fd, _captured, reader = make_session()
            consumer_path = os.path.join(tmp, "consumer.bin")
            previous = os.environ.get("STUB_MODE")
            os.environ["STUB_MODE"] = "hang"
            try:
                with open(consumer_path, "wb") as consumer:
                    stdin_fd = os.open(os.devnull, os.O_RDONLY)
                    with PatchedStdio(stdout_fd=consumer.fileno(),
                                      stdin_fd=stdin_fd):
                        status = spectate.run_proxy(
                            [sys.executable, stub], session, None,
                            wait_limit=1.0)
                    os.close(stdin_fd)
            finally:
                if previous is None:
                    os.environ.pop("STUB_MODE", None)
                else:
                    os.environ["STUB_MODE"] = previous
            reader.join(1.0)
            os.close(read_fd)
            self.assertEqual(status, 1)
            self.assertIn("did not exit", session.abort_reason or "")
            self.assertIsNotNone(session._helper.poll())

    def test_render_failure_never_becomes_clean_success(self):
        # an already-broken render pipe cannot report the disable, so the
        # wrapper must not exit 0
        with tempfile.TemporaryDirectory() as tmp:
            stub = make_stub(tmp)
            render_r, render_w = os.pipe()
            os.close(render_r)              # nobody will ever read
            env = clean_env(STUB_MODE="records", STUB_COUNT="400",
                            SPECTATE_LAUNCHER=stub,
                            SPECTATE_RENDER_FD=str(render_w))
            proc = subprocess.run(
                spectate_argv(IMPLICIT_PROBE), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(render_w,), timeout=60, input=b"")
            os.close(render_w)
            self.assertNotEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(b"Traceback", proc.stderr)

    def test_replay_invalid_stream_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.jsonl")
            with open(path, "w") as fh:
                fh.write("this is not json\n")
            proc = subprocess.run(
                spectate_argv("replay", path, "--no-color"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(proc.returncode, 1)
            self.assertNotIn(b"Traceback", proc.stderr)
            self.assertIn(b"not renderable", proc.stderr)


# ------------------------------------------------------------------
# Step E -- replay
# ------------------------------------------------------------------


class ReplayTests(unittest.TestCase):
    def replay(self, path, *args, **kwargs):
        return subprocess.run(
            spectate_argv("replay", path, "--no-color", *args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=int(
                kwargs.get("timeout", 60)))

    def test_plain_and_chunked_render_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain.jsonl")
            chunked = os.path.join(tmp, "chunked.jsonl")
            with open(plain, "wb") as fh:
                fh.write(obs_line(1, rid=7))
                fh.write(b'{"v": 1, "ch": "control", "type": "closed"}\n')
            with open(chunked, "wb") as fh:
                fh.write(chunk_pair(7, 1))
                fh.write(b'{"v": 1, "ch": "control", "type": "closed"}\n')
            one = self.replay(plain)
            two = self.replay(chunked)
            self.assertEqual(one.returncode, 0, one.stderr)
            self.assertEqual(two.returncode, 0, two.stderr)
            self.assertEqual(one.stdout, two.stdout)

    def test_retries_pages_and_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "wb") as fh:
                for line in chunk_pair(3, 1).decode().splitlines():
                    fh.write(line.encode())
                    fh.write(b"\n")
                    fh.write(line.encode())       # exact retry
                    fh.write(b"\n")
                fh.write(b'{"type":"page","content":"menu","page":1,'
                         b'"pages":1,"rows":[]}\n')
                fh.write(b'{"type":"invalid","d":3,"code":"stale"}\n')
                fh.write(b'{"v": 1, "ch": "control", "type": "closed"}\n')
            proc = self.replay(path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout.decode()
            self.assertEqual(out.count("seq=1"), 1)     # retries dedupe
            self.assertIn("page content=menu", out)
            self.assertIn("invalid d=3", out)
            self.assertIn("closed", out)

    def test_invalid_chunk_vector_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.jsonl")
            head = [{"p": "h", "k": "seq", "val": 1}]
            bad = {"v": 1, "ch": "control", "type": "chunk", "d": 1,
                   "rid": 1, "i": 1, "last": True, "parts": head}
            with open(path, "w") as fh:
                fh.write(json.dumps(bad) + "\n")
            proc = self.replay(path)
            self.assertEqual(proc.returncode, 1)
            self.assertIn(b"not renderable", proc.stderr)

    def test_final_unterminated_record_and_no_color(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "wb") as fh:
                fh.write(obs_line(1))
                fh.write(b'{"v":1,"ch":"control","type":"closed"}')  # no \n
            proc = self.replay(path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b"closed", proc.stdout)
            self.assertNotIn(b"\x1b[", proc.stdout)

    def test_instant_and_paced_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "wb") as fh:
                for i in range(1, 6):
                    fh.write(obs_line(i))
            instant = self.replay(path)
            paced = self.replay(path, "--replay-speed", "50")
            self.assertEqual(instant.stdout, paced.stdout)
            self.assertEqual(paced.returncode, 0)

    def test_bounded_file_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "many.jsonl")
            with open(path, "wb") as fh:
                for i in range(1, 3000):
                    fh.write(obs_line(i))
            started = time.monotonic()
            proc = self.replay(path)
            self.assertEqual(proc.returncode, 0)
            self.assertLess(time.monotonic() - started, 60)
            # no live coalescing: every frame is present
            self.assertEqual(proc.stdout.count(b"obs d="), 2999)


# ------------------------------------------------------------------
# Step D -- performance and byte-exact artifacts
# ------------------------------------------------------------------


def _bench_workload(size, workload):
    chunks = []
    for i in range(1, size + 1):
        if workload == "chunked":
            chunks.append(chunk_pair(i, i))
        else:
            chunks.append(obs_line(i))
    return b"".join(chunks)


# Let the renderer keep up so the measurement isolates decode/assembly
# scaling rather than the bounded-ingress backstop (which has its own test).
PACE_BYTES = 128 << 10


def benchmark_worker(size, workload):
    """One measurement in a fresh process; prints a single JSON line."""
    read_fd, write_fd = os.pipe()
    dest = spectate.RenderDestination(write_fd, True, False, "bench")
    session = spectate.LiveRenderSession(dest, messages=3, color=False,
                                         min_frame_interval=0.0)
    held = []
    reader = threading.Thread(target=drain_thread, args=(read_fd, held))
    reader.daemon = True
    reader.start()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    session.start()
    started = time.monotonic()
    cpu_before = time.process_time()
    disabled_at = None
    paced = 0
    for i in range(1, size + 1):
        if workload == "chunked":
            session.submit_bytes(chunk_pair(i, i))
        else:
            session.submit_bytes(obs_line(i))
        if session.pending_bytes() > PACE_BYTES:
            # let the renderer keep up: the point of the measurement is
            # decode/assembly scaling, not the bounded-ingress backstop
            paced += 1
            while session.pending_bytes() > PACE_BYTES:
                time.sleep(0.001)
        if disabled_at is None and session.disabled:
            disabled_at = i
    session.finish(timeout=30.0)
    elapsed = time.monotonic() - started
    cpu = time.process_time() - cpu_before
    stats = session.stats()
    session.close()
    reader.join(2.0)
    os.close(read_fd)
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "size": size,
        "workload": workload,
        "elapsed_s": elapsed,
        "cpu_s": cpu,
        "seconds_per_record": elapsed / size,
        "paced_waits": paced,
        "rss_kib_before": rss_before,
        "rss_kib_self": self_usage.ru_maxrss,
        "rss_kib_children": child_usage.ru_maxrss,
        "render_disabled_at": disabled_at,
        "frames_enqueued": stats["render_frames_enqueued"],
        "frames_written": stats["render_frames_written"],
        "frames_coalesced": stats["render_frames_coalesced"],
        "frames_dropped": stats["render_frames_dropped"],
        "queue_high_water": stats["render_queue_high_water"],
        "input_dropped_bytes": stats["render_input_dropped_bytes"],
        "disabled_reason": stats["render_disabled_reason"],
    }


def run_benchmarks(sizes, workload, output):
    results = []
    for size in sizes:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--benchmark-worker",
             str(size), workload],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode())
            raise SystemExit("benchmark worker failed for %d" % size)
        results.append(json.loads(proc.stdout.decode().strip()))
    document = {"workload": workload, "results": results}
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, sort_keys=True)
    for row in results:
        print("%-8s size=%-7d elapsed=%.3fs cpu=%.3fs s/rec=%.6f "
              "rss_self=%.1fMiB rss_child=%.1fMiB frames=%d coalesced=%d "
              "dropped=%d disabled_at=%s"
              % (row["workload"], row["size"], row["elapsed_s"],
                 row["cpu_s"], row["seconds_per_record"],
                 row["rss_kib_self"] / 1024.0,
                 row["rss_kib_children"] / 1024.0,
                 row["frames_written"], row["frames_coalesced"],
                 row["frames_dropped"], row["render_disabled_at"]))
    return document


def byte_exact(directory):
    """Write source.bin, consumer.bin and transcript.bin for a real relay."""
    os.makedirs(directory, exist_ok=True)
    source = os.path.join(directory, "source.bin")
    payload = bytearray()
    index = 0
    while len(payload) < (256 << 10):
        index += 1
        payload.extend((json.dumps({"i": index, "pad": "\u00e9" * 300})
                        + "\n").encode("utf-8"))
    payload.extend(b'{"i": 0, "partial": tr')
    with open(source, "wb") as fh:
        fh.write(payload)
    with tempfile.TemporaryDirectory() as tmp:
        stub = make_stub(tmp)
        transcript = os.path.join(directory, "transcript.bin")
        render_r, render_w = os.pipe()
        env = clean_env(STUB_MODE="cat", STUB_FILE=source,
                        SPECTATE_RENDER_FD=str(render_w),
                        SPECTATE_TRANSCRIPT=transcript)
        proc = subprocess.Popen(
            spectate_argv(stub), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(render_w,))
        os.close(render_w)
        out, err = proc.communicate(b"", timeout=300)
        os.close(render_r)
        with open(os.path.join(directory, "consumer.bin"), "wb") as fh:
            fh.write(out)
        if proc.returncode != 0:
            sys.stderr.write(err.decode())
            raise SystemExit("byte-exact relay exited %d" % proc.returncode)
    print("byte-exact: %d bytes" % len(payload))
    return 0


def main(argv):
    if argv and argv[0] == "--benchmark-worker":
        size = int(argv[1])
        workload = argv[2]
        print(json.dumps(benchmark_worker(size, workload)))
        return 0
    if argv and argv[0] == "--benchmark":
        sizes = [10000, 50000, 100000]
        workload = "plain"
        output = None
        i = 1
        while i < len(argv):
            if argv[i] == "--sizes":
                sizes = [int(x) for x in argv[i + 1].split(",")]
                i += 2
            elif argv[i] == "--workload":
                workload = argv[i + 1]
                i += 2
            elif argv[i] == "--output":
                output = argv[i + 1]
                i += 2
            else:
                raise SystemExit("unknown option %r" % argv[i])
        run_benchmarks(sizes, workload, output)
        return 0
    if argv and argv[0] == "--byte-exact":
        return byte_exact(argv[1])
    if argv and argv[0] == "--selftest":
        return _run_unittest()
    raise SystemExit("unknown arguments %r" % (argv,))


def _run_unittest():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
