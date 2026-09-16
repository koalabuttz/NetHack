#!/usr/bin/env python3
"""Live spectate / replay / transparent-proxy tool for the agent wire.

This is test-side tooling, not a production component: it lets a human
watch an episode with their eyes while an LLM harness or one of the scripted
driver policies plays.  Standard library only.

Three shapes, all sharing one renderer:

  * ``wrap``    -- a TRANSPARENT PROXY.  It spawns the real launcher, relays
                   the player channel byte-for-byte in both directions
                   (consumer stdin -> launcher stdin, launcher stdout ->
                   consumer stdout), and renders a copy of the flowing
                   records to a side channel.  The wire is never reserialized
                   and no renderer failure can change a wire byte.
  * ``replay``  -- render a saved ``--transcript`` file with no runner at
                   all, at full speed (default) or paced (``--replay-speed``).
  * runner mode -- with no subcommand, every remaining argument is forwarded
                   to the launcher verbatim, so ``spectate.py`` can be used
                   directly as the driver's ``--runner``.  Its own options
                   then come from ``SPECTATE_*`` environment variables,
                   because the driver -- not a human -- builds that argv.

Rendering runs on its own schedule behind a bounded handoff: the relay thread
only enqueues bytes, a worker thread decodes and coalesces frames with a real
deadline, and a small helper process performs the blocking destination writes
so a stalled side channel can never stall the wire or prevent a bounded
shutdown.  Frames are a best-effort view: they coalesce under load and
rendering disables itself rather than growing without bound.  That auxiliary
view is unrelated to the wire and the transcript, which stay byte-exact.

Transcript semantics: on success the transcript is exactly the launcher-output
bytes the consumer-stdout writes accepted, in order -- a confirmed prefix, not
a claim that the consumer read them.  On a downstream write failure the
transcript ends at the last confirmed write (possibly inside a line), the
launcher is terminated and reaped, and the wrapper exits nonzero.  A
transcript-storage failure is reported as an incomplete recording.

Usage:
    python3 test/agent/spectate.py wrap [options] -- <launcher args...>
    python3 test/agent/spectate.py replay FILE|- [options]
    python3 test/agent/spectate.py selftest
    python3 test/agent/spectate.py <launcher args...>     # implicit wrap

Options (wrap/replay; also settable by SPECTATE_* env in runner mode):
    --launcher PATH          real launcher to spawn
                             (default: <repo>/src/nethack-agent)
    --transcript FILE        write the runner-side JSONL verbatim (replay)
    --render-fd N|tty        side channel (default: 2; replay: 1)
    --messages N             trailing messages per frame (default: 3)
    --min-frame-interval S   coalesce faster obs frames (default: 0.15)
    --replay-speed S         replay frames/second, or "instant" (default)
    --no-color / --color     force ANSI colors off / on
    --quiet                  suppress the launch banner

Environment (runner mode): SPECTATE_LAUNCHER, SPECTATE_TRANSCRIPT,
SPECTATE_RENDER_FD, SPECTATE_MESSAGES, SPECTATE_MIN_FRAME_INTERVAL,
SPECTATE_REPLAY_SPEED, SPECTATE_NO_COLOR and NO_COLOR.  Live rendering
rejects fd 1: that is the consumer's stdout, and rendering there would corrupt
the wire.  A launcher whose first argument is literally a reserved word here
(``wrap``, ``replay``, ``selftest``, ``help`` / ``-h`` / ``--help``) needs an
explicit ``wrap --``.
"""

import errno
import fcntl
import io
import json
import math
import os
import select
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import format_obs  # noqa: E402  (test-side independent chunk/page decoder)

REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_LAUNCHER = os.path.join(REPO_ROOT, "src", "nethack-agent")
DEFAULT_MIN_FRAME_INTERVAL = 0.15
DEFAULT_MESSAGES = 3

# Live-render safety limits.  These are named constants rather than flags on
# purpose: they are operational bounds, not a user-facing policy surface.
INGRESS_LIMIT = 1 << 20           # bounded relay -> renderer handoff
INPUT_BATCH_BYTES = 1 << 18       # bounded decode work per scheduling pass
MAX_LINE_BYTES = 1 << 20          # longest physical line we will look at
MAX_RETAINED_BYTES = 16 << 20     # assembler canonical-chunk budget
MAX_CHUNKS = 65536                # assembler chunk-count budget
MAX_STREAMS = 4096                # assembler stream-count budget
CONTROL_QUEUE_MAX = 32            # bounded ordered control/notice queue
CONTROL_QUEUE_BYTES = 64 << 10
OBS_CHARGE = 8192                 # nominal bytes charged per pending obs
TRANSPORT_STALL_LIMIT = 2.0       # no-progress limit before disabling
RENDER_SHUTDOWN_GRACE = 2.0       # total live-render shutdown grace
LAUNCHER_WAIT_LIMIT = 60.0        # normal launcher wait (as the driver uses)
FAILURE_KILL_GRACE = 1.0          # terminate -> kill escalation
STDIN_LIMIT = 1 << 20             # bounded consumer-stdin pending bytes
SEL_TIMEOUT = 0.2                 # relay select tick (signal responsiveness)

# The writer helper is the same script under a private invocation.  The
# environment marker keeps a stray ``--writer-helper`` from ever reaching the
# public command line.
_WRITER_MODE_ARG = "--writer-helper"
_WRITER_MARKER_ENV = "SPECTATE_WRITER_MARKER"
_WRITER_MARKER_VALUE = "spectate-writer-v1"
_ACK_ERROR_ID = 0xFFFFFFFF

# The 16 frozen public color names -> SGR foreground / background codes.
# Slot 8 is the native NO_COLOR ("none") and maps to the terminal default.
_FOREGROUND = {
    "black": 30, "red": 31, "green": 32, "brown": 33, "blue": 34,
    "magenta": 35, "cyan": 36, "gray": 37,
    "orange": 91, "brightgreen": 92, "yellow": 93, "brightblue": 94,
    "brightmagenta": 95, "brightcyan": 96, "white": 97, "none": 39,
}
_BACKGROUND = {
    "black": 40, "red": 41, "green": 42, "brown": 43, "blue": 44,
    "magenta": 45, "cyan": 46, "gray": 47,
    "orange": 101, "brightgreen": 102, "yellow": 103, "brightblue": 104,
    "brightmagenta": 105, "brightcyan": 106, "white": 107, "none": 49,
}
# style bitmask: bold=1 dim=2 italic=4 underline=8 blink=16 inverse=32
_STYLE_BITS = ((1, 1), (2, 2), (4, 3), (8, 4), (16, 5), (32, 7))
_RESET = "\x1b[0m"


class UsageError(Exception):
    """A command line this tool cannot act on (exit 2)."""


class _RelayError(Exception):
    """A relay-side failure that must stop the wire (exit 1)."""


class _ConsumerError(_RelayError):
    """The consumer's stdout refused or broke mid-delivery."""


class _TranscriptError(_RelayError):
    """The transcript could not record a confirmed write."""


class _Terminated(Exception):
    """A termination signal arrived; unwind and report 128+n."""

    def __init__(self, signum):
        Exception.__init__(self, "signal %d" % signum)
        self.signum = signum


# ------------------------------------------------------------------
# formatting: one logical record -> the lines of one frame
# ------------------------------------------------------------------


def _sgr(cell):
    """The SGR sequence for one palette cell, or "" for a plain cell."""
    _char, color, style, frame = cell
    codes = [code for bit, code in _STYLE_BITS if style & bit]
    if color != "none":
        codes.append(_FOREGROUND.get(color, 39))
    if frame != "none":
        codes.append(_BACKGROUND.get(frame, 49))
    if not codes:
        return ""
    return "\x1b[" + ";".join(str(c) for c in codes) + "m"


def _map_row(grid, cur, y, color):
    """One map row as a string of exactly MAP_W columns (ANSI-colored)."""
    out = []
    current = None
    for x in range(format_obs.MAP_W):
        if x == 0:
            out.append(" ")
            continue
        if cur and [x, y] == cur:
            cell = ("*", "none", 0, "none")
        else:
            cell = grid[y][x]
        if color:
            want = _sgr(cell)
            if want != current:
                out.append(_RESET if current else "")
                out.append(want)
                current = want
        out.append(cell[0])
    if color and current:
        out.append(_RESET)
    return "".join(out)


def _status_line(rec):
    s = rec.get("s") or {}
    parts = []
    for key in sorted(s):
        value = s[key]
        parts.append("%s=%s" % (key, value["text"] if value else "(deleted)"))
    return " ".join(parts)


def _need_text(need, windows):
    """The outstanding request as one compact line body (kind + identity)."""
    if need is None:
        return None
    kind = need.get("kind")
    if kind in ("command", "key", "direction"):
        text = kind
        if need.get("prompt"):
            text += " prompt=%s" % json.dumps(need["prompt"])
    elif kind == "position":
        text = "position prompt=%s rect=[%d,%d..%d,%d]" % (
            json.dumps(need.get("prompt", "")), need.get("x0"),
            need.get("y0"), need.get("x1"), need.get("y1"))
    elif kind == "yn":
        text = "yn prompt=%s choices=%s default=%s numeric=%s" % (
            json.dumps(need.get("prompt", "")),
            json.dumps(need.get("choices")), need.get("default"),
            need.get("numeric"))
    elif kind in ("line", "extcmd"):
        text = "%s prompt=%s max=%s" % (
            kind, json.dumps(need.get("prompt", "")), need.get("max"))
    elif kind == "menu":
        text = "menu %s mode=%s content=%s pages=%s" % (
            need.get("menu"), need.get("mode"), need.get("content"),
            need.get("pages"))
        titles = [w["title"] for w in windows if w.get("kind") == "menu"]
        if titles:
            text += " title=%s" % json.dumps(
                titles[0] if len(titles) == 1 else titles)
    elif kind == "ack":
        text = "ack content=%s pages=%s" % (
            need.get("content"), need.get("pages"))
    else:
        text = json.dumps(need, sort_keys=True)
    return "  need: " + text


def obs_frame(rec, messages=DEFAULT_MESSAGES, color=True):
    """The frame for one observation: map, status, messages, need."""
    lines = ["obs d=%s seq=%s base=%s"
             % (rec.get("d"), rec.get("seq"), rec.get("base"))]
    status = _status_line(rec)
    lines.append("  status: " + (status if status else "-"))
    cond = rec.get("cond") or []
    if cond:
        lines.append("  cond: " + " ".join(c["text"] for c in cond))

    grid, cur = format_obs.map_grid(rec)
    for y in range(format_obs.MAP_H):
        lines.append("  |" + _map_row(grid, cur, y, color) + "|")

    msgs = rec.get("msg") or []
    if messages > 0:
        for entry in msgs[-messages:]:
            lines.append("  msg[%s] %s" % (entry.get("e"), entry.get("text")))
    need_line = _need_text(rec.get("need"), rec.get("windows") or [])
    if need_line:
        lines.append(need_line)
    return lines


def one_liner(rec):
    """A one-line form of a non-obs record (None if we do not show it)."""
    t = rec.get("type")
    if t == "hello":
        return ("hello profile=%s policy=%s caps=%s size=%s limits=%s"
                % (rec.get("profile"), rec.get("policy"),
                   ",".join(rec.get("caps") or []), rec.get("size"),
                   json.dumps(rec.get("limits"), sort_keys=True)))
    if t == "closed":
        return "closed"
    if t == "invalid":
        return "invalid d=%s code=%s" % (rec.get("d"), rec.get("code"))
    if t == "page":
        return "page content=%s %s/%s rows=%d" % (
            rec.get("content"), rec.get("page"), rec.get("pages"),
            len(rec.get("rows") or []))
    if t in ("chunk",):
        return None
    return "? " + json.dumps(rec, sort_keys=True)


class FramePainter(object):
    """Turn frames into bytes; owns only the TTY redraw height.

    Pure and synchronous: replay and the selftests drive it directly, while
    the live worker owns the scheduling (deadline, coalescing) around it.
    """

    def __init__(self, messages=DEFAULT_MESSAGES, color=True, tty=False):
        self.messages = messages
        self.color = color
        self.tty = tty
        self.height = 0

    def frame(self, lines):
        """The bytes for drawing ``lines``, redrawing in place on a TTY."""
        if not self.tty:
            return "".join(line + "\n" for line in lines).encode(
                "utf-8", "replace")
        out = []
        if self.height:
            out.append("\x1b[%dA" % self.height)
        for line in lines:
            out.append(line + "\n")
        if self.height > len(lines):
            out.append("\x1b[0J")
        self.height = len(lines)
        return "".join(out).encode("utf-8", "replace")

    def obs(self, rec):
        return self.frame(obs_frame(rec, self.messages, self.color))

    def note(self, text):
        return self.frame(str(text).split("\n"))


# ------------------------------------------------------------------
# destination policy
# ------------------------------------------------------------------


class RenderDestination(object):
    """A single-owner descriptor for rendered frames.

    ``fd`` is always an fd this process opened (or a dup it owns), never the
    caller's descriptor itself and never one whose flags we mutate.  The live
    path hands ``fd`` to the helper process; the replay path writes to it
    directly.
    """

    def __init__(self, fd, owned=True, tty=False, label="fd", note=None):
        self.fd = fd
        self.owned = owned
        self.tty = tty
        self.label = label
        self.note = note
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.owned:
            _close_fd(self.fd)


def _aliases_stdout(fd):
    """True when ``fd`` is an obvious alias of the consumer's stdout.

    Only shared pipes and regular files count: two independent opens of the
    same character device (a terminal) are not a shared output channel, and
    framing that as an alias would reject the documented default.  Aliases
    opened separately from the same regular file cannot be detected here; a
    side channel must genuinely be a separate sink.
    """
    try:
        a = os.fstat(fd)
        b = os.fstat(1)
    except OSError:
        return False
    if stat.S_ISFIFO(a.st_mode) and stat.S_ISFIFO(b.st_mode):
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    if stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode):
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    return False


def _open_tty():
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
        return RenderDestination(fd, True, True, "/dev/tty")
    except OSError:
        # No controlling terminal (a harness, a batch run): the documented
        # default is stderr, so fall back to isolated helper writes on fd 2.
        try:
            fd = os.dup(2)
        except OSError as exc:
            raise UsageError("no controlling terminal and stderr is not "
                             "usable as a fallback: %s" % exc)
        return RenderDestination(
            fd, True, os.isatty(fd), "fd 2",
            note="no controlling terminal: frames fall back to fd 2")


def open_render(spec, live):
    """Open the frame side channel from a spec of ``N`` or ``tty``.

    Never mutates a caller-supplied descriptor: an integer spec is validated
    and then duplicated, so this process owns exactly one descriptor and the
    caller keeps its flags and file position untouched.
    """
    text = str(spec).strip()
    if text in ("tty", "/dev/tty"):
        return _open_tty()
    try:
        n = int(text, 10)
    except (TypeError, ValueError):
        raise UsageError("--render-fd needs an integer or 'tty', not %r"
                         % (spec,))
    if n < 0:
        raise UsageError("--render-fd must not be negative")
    if live and n == 1:
        raise UsageError(
            "live render fd 1 is the consumer stdout: rendering there would "
            "corrupt the wire; use a dedicated fd, 'tty', or 2")
    try:
        flags = fcntl.fcntl(n, fcntl.F_GETFL)
    except OSError as exc:
        raise OSError("render fd %d is not usable: %s" % (n, exc))
    if (flags & os.O_ACCMODE) == os.O_RDONLY:
        raise UsageError("render fd %d is not writable" % n)
    if live and _aliases_stdout(n):
        raise UsageError(
            "render fd %d is an alias of the consumer stdout (fd 1): "
            "rendering there would corrupt the wire" % n)
    if live and n == 2 and _isatty(2):
        # prefer a separately opened terminal descriptor over fd 2's dup
        try:
            base = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            pass
        else:
            return RenderDestination(base, True, True, "/dev/tty")
    fd = os.dup(n)
    return RenderDestination(fd, True, _isatty(fd), "fd %d" % n)


def _isatty(fd):
    try:
        return os.isatty(fd)
    except OSError:
        return False


# ------------------------------------------------------------------
# small fd helpers
# ------------------------------------------------------------------


def _close_fd(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _close_quietly(obj):
    if obj is None:
        return
    try:
        obj.close()
    except OSError:
        pass


def _fd_of(stream, fallback):
    try:
        return stream.fileno()
    except (AttributeError, ValueError, OSError):
        return fallback


def _read_exact(fd, count):
    """Read exactly ``count`` bytes; None on EOF before that."""
    buf = bytearray()
    while len(buf) < count:
        try:
            chunk = os.read(fd, count - len(buf))
        except InterruptedError:
            continue
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _write_all(fd, data):
    """Write every byte, waiting for writability on EAGAIN."""
    view = memoryview(bytes(data))
    while len(view):
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [])
            continue
        if written <= 0:
            raise OSError(errno.EIO, "zero-length write")
        view = view[written:]


def _wait_writable(fd):
    select.select([], [fd], [])


def _read_quiet(fd, size):
    """Read up to ``size`` bytes: bytes, b"" at EOF, None if no data yet."""
    try:
        return os.read(fd, size)
    except BlockingIOError:
        return None
    except OSError:
        return b""


# ------------------------------------------------------------------
# the writer helper: the only component that blocks on the destination
# ------------------------------------------------------------------


def _writer_helper_main(argv):
    """Private helper: proxy pipe -> destination fd, with acknowledgements.

    ``argv`` is ``[--writer-helper, DATA_FD, ACK_FD, DEST_FD]``.  Payloads
    arrive as length-delimited envelopes; the helper writes each one whole to
    the destination and publishes the highest completed frame id, because a
    write to the pipe proves only that the proxy buffered it.  Exit status 0
    means every envelope it read was written and it saw a clean EOF; 2 means
    the destination failed.
    """
    if len(argv) != 4:
        return 2
    try:
        data_fd, ack_fd, dest_fd = (int(x) for x in argv[1:])
    except (TypeError, ValueError):
        return 2
    state = {"pending": None}

    def flush_ack():
        pending = state["pending"]
        if pending is None:
            return True
        try:
            written = os.write(ack_fd, pending)
        except InterruptedError:
            return False
        except BlockingIOError:
            return False
        except OSError:
            return False
        if written < len(pending):
            state["pending"] = pending[written:]
            return False
        state["pending"] = None
        return True

    def note(frame_id, status):
        state["pending"] = struct.pack("!Ii", frame_id, status)
        flush_ack()

    while True:
        header = _read_exact(data_fd, 8)
        if header is None:
            break
        frame_id, size = struct.unpack("!II", header)
        remaining = size
        try:
            while remaining:
                want = min(remaining, 65536)
                chunk = _read_exact(data_fd, want)
                if chunk is None:
                    # the proxy closed mid-frame: the frame is incomplete and
                    # must never be reported as written
                    return 3
                _write_all(dest_fd, chunk)
                remaining -= len(chunk)
        except OSError as exc:
            note(_ACK_ERROR_ID, -(exc.errno or errno.EIO))
            deadline = time.monotonic() + FAILURE_KILL_GRACE
            while state["pending"] is not None \
                    and time.monotonic() < deadline:
                select.select([], [ack_fd], [], 0.05)
                flush_ack()
            return 2
        note(frame_id, 0)
    flush_ack()
    return 0


# ------------------------------------------------------------------
# the live render session: relay handoff + renderer worker + helper
# ------------------------------------------------------------------


class LiveRenderSession(object):
    """Bounded, independently scheduled rendering for the live proxy.

    ``submit_bytes`` is the only relay-facing entry point and never waits for
    queue space.  One non-daemon worker thread owns the line splitter, the
    incremental assembler, the coalescing/deadline state and the frame bytes;
    a helper process performs the destination writes.  Nothing here ever
    mutates the caller's descriptors or the wire.
    """

    def __init__(self, dest, messages=DEFAULT_MESSAGES, color=True,
                 min_frame_interval=DEFAULT_MIN_FRAME_INTERVAL,
                 shutdown_grace=RENDER_SHUTDOWN_GRACE,
                 ingress_limit=INGRESS_LIMIT):
        self.dest = dest
        self.painter = FramePainter(messages, color, dest.tty)
        self.min_interval = max(0.0, float(min_frame_interval))
        self.shutdown_grace = shutdown_grace
        self.ingress_limit = ingress_limit
        self.max_line_bytes = MAX_LINE_BYTES
        self.assembler = format_obs.IncrementalAssembler(
            max_retained_bytes=MAX_RETAINED_BYTES, max_chunks=MAX_CHUNKS,
            max_streams=MAX_STREAMS, max_line_bytes=MAX_LINE_BYTES)

        self._cond = threading.Condition()
        self._inbox = deque()
        self._inbox_bytes = 0
        self._notes = deque()
        self._eof = False
        self._disabled = False
        self._disabled_reason = None
        self._forced = False
        self._stop_at = None
        self._input_dropped = 0
        self._coalesced = 0
        self._enqueued = 0
        self._queue_high = 0

        # worker-owned state (never touched from the relay thread)
        self._rem = bytearray()
        self._outq = deque()
        self._outq_bytes = 0
        self._last_emit = 0.0
        self._emitted_once = False
        self._frame_id = 0

        # transport / process resources
        self._fd_lock = threading.Lock()
        self._data_w = None
        self._ack_r = None
        self._helper = None
        self._worker = None
        self._ack_buf = bytearray()
        self._acked = 0
        self._dest_error = None
        self._note_written = False
        self._finished = False
        self._close_lock = threading.Lock()
        self.abort_reason = None

    # -- lifecycle ---------------------------------------------------

    def start(self):
        """Spawn the helper and the worker.  Raises OSError on failure."""
        data_r, self._data_w = os.pipe()
        ack_r, ack_w = os.pipe()
        os.set_blocking(self._data_w, False)
        os.set_blocking(ack_r, False)
        self._ack_r = ack_r
        env = dict(os.environ)
        env[_WRITER_MARKER_ENV] = _WRITER_MARKER_VALUE
        script = os.path.abspath(__file__)
        argv = [sys.executable or "python3", script, _WRITER_MODE_ARG,
                str(data_r), str(ack_w), str(self.dest.fd)]
        try:
            self._helper = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                pass_fds=(data_r, ack_w, self.dest.fd), env=env)
        finally:
            _close_fd(data_r)
            _close_fd(ack_w)
        self._worker = threading.Thread(target=self._worker_main,
                                        name="spectate-render")
        self._worker.start()

    @property
    def ack_fd(self):
        return self._ack_r

    @property
    def disabled(self):
        return self._disabled

    def pending_bytes(self):
        """Bytes accepted for rendering but not yet decoded.

        Bounded by the ingress limit, so a caller that wants the renderer to
        keep up (a benchmark, a test) can throttle on it.
        """
        with self._cond:
            return self._inbox_bytes + len(self._rem)

    def submit_bytes(self, data):
        """Enqueue launcher bytes for rendering; never blocks."""
        if not data:
            return
        with self._cond:
            if self._disabled or self._forced:
                return
            if self._inbox_bytes + len(data) > self.ingress_limit:
                self._input_dropped += self._inbox_bytes + len(data)
                self._disable_locked(
                    "render input dropped: %d bytes" % self._input_dropped)
                return
            self._inbox.append(bytes(data))
            self._inbox_bytes += len(data)
            if self._inbox_bytes > self._queue_high:
                self._queue_high = self._inbox_bytes
            self._cond.notify()

    def submit_note(self, text):
        """Queue an out-of-band notice (banner, decoder complaint)."""
        if not text:
            return
        with self._cond:
            if self._disabled or self._forced:
                return
            self._notes.append(text)
            self._cond.notify()

    def finish(self, timeout=None):
        """Normal EOF: drain, flush the newest frame, join, reap.

        Returns the completion statistics, which stay readable afterwards.
        """
        self._shutdown(True, timeout)
        return self.stats()

    def abort(self, reason):
        """Stop now, releasing queues and killing the helper if needed."""
        if reason:
            self.abort_reason = reason
        self._shutdown(False, None)

    def close(self):
        """Release every resource; idempotent and safe after any failure."""
        with self._close_lock:
            if self._finished and self.dest._closed and self._data_w is None:
                return
        if not self._finished:
            self._shutdown(False, None)
        self._close_data_w()
        self._close_ack()
        self.dest.close()

    def _shutdown(self, graceful, timeout):
        if self._finished:
            return
        self._finished = True
        if timeout is None:
            timeout = self.shutdown_grace
        now = time.monotonic()
        with self._cond:
            if graceful:
                self._eof = True
            else:
                self._forced = True
            stop = now + timeout
            if self._stop_at is None or self._stop_at > stop:
                self._stop_at = stop
            self._cond.notify_all()
        self._join(timeout)

    def _join(self, timeout):
        worker = self._worker
        if worker is not None:
            worker.join(timeout + 1.0)
            if worker.is_alive():
                with self._cond:
                    self._forced = True
                    self._cond.notify_all()
                worker.join(FAILURE_KILL_GRACE)
        self._close_data_w()          # the helper sees EOF and winds down
        self._reap_helper(timeout)    # drains acknowledgements while waiting
        self._drain_acks()
        self._close_ack()

    def _reap_helper(self, timeout):
        proc = self._helper
        if proc is None:
            return
        try:
            proc.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            _terminate(proc)
            try:
                proc.wait(timeout=FAILURE_KILL_GRACE)
            except subprocess.TimeoutExpired:
                _kill(proc)
                proc.wait()
        self._drain_acks()
        if proc.returncode == 0:
            # a clean helper exit proves every enqueued envelope was written
            self._acked = max(self._acked, self._enqueued)
        elif proc.returncode is not None and self._dest_error is None:
            if proc.returncode == 2:
                self._dest_error = "render destination write failed"
            elif proc.returncode == 3:
                self._dest_error = "render helper stopped mid-frame"
            elif proc.returncode < 0:
                self._dest_error = ("render helper killed by signal %d"
                                    % -proc.returncode)
            else:
                self._dest_error = "render helper exited %d" % proc.returncode

    def _close_data_w(self):
        with self._fd_lock:
            fd, self._data_w = self._data_w, None
        if fd is not None:
            _close_fd(fd)

    def _close_ack(self):
        with self._fd_lock:
            fd, self._ack_r = self._ack_r, None
        if fd is not None:
            _close_fd(fd)

    # -- relay-facing polling ---------------------------------------

    def poll(self):
        """Drain acknowledgements and note a dead or failed helper."""
        self._drain_acks()
        proc = self._helper
        if proc is not None and self._dest_error is None:
            code = proc.poll()
            if code is not None and code != 0:
                self._dest_error = "render helper exited %d" % code
        return self.stats()

    def _drain_acks(self):
        fd = self._ack_r
        if fd is None:
            return
        while True:
            try:
                data = os.read(fd, 8192)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            self._ack_buf.extend(data)
            while len(self._ack_buf) >= 8:
                frame_id, status = struct.unpack("!Ii",
                                                 bytes(self._ack_buf[:8]))
                del self._ack_buf[:8]
                if frame_id == _ACK_ERROR_ID or status < 0:
                    self._dest_error = ("render destination failed "
                                        "(errno %s)" % status)
                elif frame_id + 1 > self._acked:
                    self._acked = frame_id + 1

    def stats(self):
        written = min(self._acked, self._enqueued)
        return {
            "render_frames_enqueued": self._enqueued,
            "render_frames_written": written,
            "render_frames_dropped": self._enqueued - written,
            "render_frames_coalesced": self._coalesced,
            "render_input_dropped_bytes": self._input_dropped,
            "render_queue_high_water": self._queue_high,
            "render_disabled": self._disabled,
            "render_disabled_reason": self._disabled_reason,
            "render_destination_error": self._dest_error,
            "render_output_delivered": written > 0,
        }

    # -- worker ------------------------------------------------------

    def _disable_locked(self, reason):
        if self._disabled:
            return
        self._disabled = True
        self._disabled_reason = reason
        self._inbox.clear()
        self._inbox_bytes = 0
        self._cond.notify_all()

    def _disable(self, reason):
        with self._cond:
            self._disable_locked(reason)

    def _stop_remaining(self):
        if self._stop_at is None:
            return None
        return self._stop_at - time.monotonic()

    def _worker_main(self):
        try:
            self._loop()
        except BaseException as exc:   # the worker must never crash the tool
            with self._cond:
                self._disable_locked("renderer error: %s" % exc)
        finally:
            self._close_data_w()

    def _loop(self):
        while True:
            self._process_input()
            now = time.monotonic()
            with self._cond:
                disabled = self._disabled
                forced = self._forced
            if disabled or forced:
                break
            remaining = self._stop_remaining()
            if remaining is not None and remaining <= 0:
                with self._cond:
                    self._forced = True
                break
            with self._cond:
                eof = self._eof
                busy = bool(self._inbox) or bool(self._rem)
            self._emit_due(now, force=eof and not busy)
            with self._cond:
                if self._disabled or self._forced:
                    break
                if self._eof and not self._inbox and not self._rem \
                        and not self._outq:
                    break
                timeout = self._next_wait_locked(time.monotonic())
                if timeout is None:
                    self._cond.wait()
                elif timeout > 0:
                    self._cond.wait(timeout)
        self._finalize()

    def _next_wait_locked(self, now):
        if self._inbox or self._notes:
            return 0.0
        wait = None
        if self._outq:
            kind = self._outq[0][0]
            if kind == "obs" and self._emitted_once:
                wait = max(0.0, self._last_emit + self.min_interval - now)
            else:
                wait = 0.0
        stop = self._stop_remaining()
        if stop is not None:
            return stop if wait is None else min(wait, max(0.0, stop))
        return wait

    def _process_input(self):
        budget = INPUT_BATCH_BYTES
        self._drain_notes()
        while budget > 0:
            with self._cond:
                if self._disabled or self._forced:
                    return
                chunk = self._inbox.popleft() if self._inbox else None
                if chunk is not None:
                    self._inbox_bytes -= len(chunk)
            if chunk is None:
                self._drain_notes()
                return
            budget -= len(chunk)
            self._feed_chunk(chunk)

    def _drain_notes(self):
        with self._cond:
            notes = list(self._notes)
            self._notes.clear()
        for text in notes:
            self._queue_item("control", text)

    def _feed_chunk(self, data):
        buf = self._rem
        buf.extend(data)
        start = 0
        while True:
            nl = buf.find(b"\n", start)
            if nl < 0:
                break
            line = bytes(buf[start:nl])
            start = nl + 1
            if not self._handle_line(line):
                del buf[:]
                return
        if start:
            del buf[:start]
        if not self._disabled and len(buf) > self.max_line_bytes:
            self._disable("physical line exceeds %d bytes"
                          % self.max_line_bytes)
            del buf[:]

    def _handle_line(self, line):
        if not line.strip():
            return True
        if len(line) > self.max_line_bytes:
            self._disable("physical line exceeds %d bytes"
                          % self.max_line_bytes)
            return False
        try:
            records = self.assembler.feed(line)
        except format_obs.AssemblerLimit as exc:
            self._disable("render budget reached: %s" % exc)
            return False
        except (format_obs.ChunkError, ValueError, KeyError, TypeError,
                IndexError) as exc:
            self._disable("stream not renderable: %s" % exc)
            return False
        for rec in records:
            self._accept(rec)
        return not self._disabled

    def _accept(self, rec):
        if rec.get("type") == "obs":
            self._queue_item("obs", rec)
            return
        line = one_liner(rec)
        if line is not None:
            self._queue_item("control", line)

    def _queue_item(self, kind, item):
        if kind == "obs":
            if self._outq and self._outq[-1][0] == "obs":
                # a later obs replaces only the still-unsent newest one
                self._outq[-1] = ("obs", item)
                self._coalesced += 1
                return
            cost = OBS_CHARGE
        else:
            cost = len(item) + 64
        self._outq.append((kind, item))
        self._outq_bytes += cost
        if len(self._outq) > CONTROL_QUEUE_MAX \
                or self._outq_bytes > CONTROL_QUEUE_BYTES:
            self._disable("render queue overflow")

    # -- emission ----------------------------------------------------

    def _emit_due(self, now, force=False):
        while True:
            if not self._outq or self._disabled:
                return
            kind, item = self._outq[0]
            if kind == "obs" and self._emitted_once and not force \
                    and (now - self._last_emit) < self.min_interval:
                return
            if kind == "obs":
                payload = self.painter.obs(item)
            else:
                payload = self.painter.note(item)
            if not self._transport_frame(payload):
                return
            self._outq.popleft()
            if kind == "obs":
                self._outq_bytes = max(0, self._outq_bytes - OBS_CHARGE)
            else:
                self._outq_bytes = max(0, self._outq_bytes - len(item) - 64)
            self._last_emit = time.monotonic()
            self._emitted_once = True
            now = time.monotonic()

    def _transport_frame(self, payload):
        """Hand one whole frame to the helper; False if rendering stopped."""
        if self._disabled:
            return False
        header = struct.pack("!II", self._frame_id, len(payload))
        self._frame_id += 1
        blob = header + payload
        view = memoryview(blob)
        sent = 0
        stalled = None
        while sent < len(blob):
            try:
                written = os.write(self._data_w, view[sent:])
            except InterruptedError:
                continue
            except BlockingIOError:
                written = 0
            except OSError as exc:
                self._disable("render pipe: %s" % exc)
                return False
            if written > 0:
                sent += written
                stalled = None
                continue
            now = time.monotonic()
            if stalled is None:
                stalled = now
            if now - stalled >= TRANSPORT_STALL_LIMIT:
                self._disable("render sink stalled (no progress)")
                return False
            wait = TRANSPORT_STALL_LIMIT - (now - stalled)
            stop = self._stop_remaining()
            if stop is not None:
                if stop <= 0:
                    return False
                wait = min(wait, stop)
            select.select([], [self._data_w], [], wait)
        self._enqueued += 1
        return True

    def _finalize(self):
        with self._cond:
            disabled = self._disabled
            reason = self._disabled_reason
        if disabled:
            self.assembler.clear()
            self._outq.clear()
            self._outq_bytes = 0
            del self._rem[:]
            note = "spectate: rendering disabled: %s" % (reason,)
            if self._transport_frame(self.painter.note(note)):
                self._note_written = True
        else:
            self._report_incomplete()
        self._close_data_w()

    def _report_incomplete(self):
        try:
            notes = self.assembler.finish()
        except Exception:
            return
        for note in notes:
            if not self._transport_frame(
                    self.painter.note("spectate: " + note)):
                return


def _render_failed_unreported(stats):
    """True when rendering failed with no way to report it anywhere."""
    failed = (stats["render_disabled"]
              or stats["render_destination_error"]
              or stats["render_frames_dropped"] > 0)
    return bool(failed) and not stats["render_output_delivered"]


# ------------------------------------------------------------------
# the transparent proxy
# ------------------------------------------------------------------


def _deliver(fd, data, tf):
    """Write ``data`` to the consumer, confirming each slice
    to the transcript.

    The transcript receives exactly the bytes the consumer-stdout write
    accepted, so it is a confirmed prefix of the launcher output and never a
    claim that the consumer application read them.  Raises _RelayError.
    """
    view = memoryview(data)
    off = 0
    total = len(data)
    while off < total:
        try:
            written = os.write(fd, view[off:])
        except InterruptedError:
            continue
        except BlockingIOError:
            _wait_writable(fd)
            continue
        except OSError as exc:
            raise _ConsumerError("consumer stdout: %s" % exc)
        if written <= 0:
            raise _ConsumerError("consumer stdout: zero-length write")
        if tf is not None:
            try:
                tf.write(data[off:off + written])
            except OSError as exc:
                raise _TranscriptError(
                    "incomplete recording: transcript write failed after "
                    "%d confirmed bytes: %s" % (off, exc))
        off += written


def _flush_stdin(fd, buf):
    """Push pending consumer bytes at the launcher stdin (non-blocking)."""
    while buf:
        try:
            written = os.write(fd, buf)
        except InterruptedError:
            continue
        except BlockingIOError:
            return
        except OSError:
            del buf[:]
            return
        if written <= 0:
            del buf[:]
            return
        del buf[:written]


def _relay(proc, session, tf):
    """Relay the player channel until the launcher's stdout reaches EOF.

    A single full-duplex select loop owns every pipe endpoint: launcher stdout
    and stdin, the consumer's stdin, and the render helper's acknowledgements.
    Launcher reads pause while a chunk is still being delivered downstream, so
    an output chunk in flight never competes with a new one.  Returns
    (status, failure): (0, None) is a clean relay, otherwise a nonzero wrapper
    status and a human-readable cause.
    """
    out_fd = proc.stdout.fileno()
    in_fd = proc.stdin.fileno()
    os.set_blocking(out_fd, False)
    os.set_blocking(in_fd, False)
    consumer_fd = _fd_of(sys.stdout, 1)
    stdin_fd = _fd_of(sys.stdin, None)
    ack_fd = session.ack_fd

    pending = b""
    stdin_buf = bytearray()
    stdin_open = True
    launcher_eof = False
    while True:
        if pending:
            try:
                _deliver(consumer_fd, pending, tf)
            except _RelayError as exc:
                return 1, str(exc)
            pending = b""
        if launcher_eof:
            break
        if stdin_open and stdin_fd is None and not stdin_buf:
            _close_quietly(proc.stdin)
            stdin_open = False
        reads = [out_fd]
        if stdin_open and stdin_fd is not None \
                and len(stdin_buf) < STDIN_LIMIT:
            reads.append(stdin_fd)
        if ack_fd is not None:
            reads.append(ack_fd)
        writes = [in_fd] if (stdin_open and stdin_buf) else []
        try:
            readable, writable, _ = select.select(reads, writes, [],
                                                  SEL_TIMEOUT)
        except InterruptedError:
            continue
        except (OSError, ValueError) as exc:
            return 1, "relay select failed: %s" % exc
        if writable:
            _flush_stdin(in_fd, stdin_buf)
        for fd in readable:
            if fd == out_fd:
                chunk = _read_quiet(out_fd, 65536)
                if chunk is None:
                    continue
                if chunk:
                    pending = chunk
                    session.submit_bytes(chunk)
                else:
                    launcher_eof = True
            elif ack_fd is not None and fd == ack_fd:
                session.poll()
            elif stdin_fd is not None and fd == stdin_fd:
                data = _read_quiet(stdin_fd, 65536)
                if data is None:
                    continue
                if data:
                    stdin_buf.extend(data)
                else:
                    stdin_fd = None     # stop polling a closed consumer stdin
    return 0, None


def _wait_launcher(proc, limit):
    """Wait for the launcher with the normal limit; report a timeout."""
    _close_quietly(proc.stdout)
    try:
        return proc.wait(timeout=limit), False
    except subprocess.TimeoutExpired:
        return None, True


def _kill_launcher(proc):
    """Close launcher stdin, terminate, escalate to SIGKILL."""
    _close_quietly(proc.stdin)
    _terminate(proc)
    try:
        proc.wait(timeout=FAILURE_KILL_GRACE)
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            proc.wait(timeout=FAILURE_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
    _close_quietly(proc.stdout)
    return proc.returncode, False


def _terminate(proc):
    try:
        proc.terminate()
    except OSError:
        pass


def _kill(proc):
    try:
        proc.kill()
    except OSError:
        pass


def _exit_status(code):
    """A negative launcher status is a signal; report it as 128+n."""
    if code is None:
        return 1
    return 128 + (-code) if code < 0 else code


def run_proxy(launcher_argv, session, transcript_path=None, banner=None,
              wait_limit=LAUNCHER_WAIT_LIMIT):
    """Spawn the launcher and relay the player channel byte-for-byte."""
    tf = None
    proc = None
    status = 0
    failure = None
    interrupted = None
    try:
        if transcript_path:
            try:
                tf = open(transcript_path, "wb", buffering=0)
            except OSError as exc:
                sys.stderr.write("spectate: cannot open transcript %s: %s\n"
                                 % (transcript_path, exc))
                return 1
        try:
            session.start()
        except OSError as exc:
            sys.stderr.write("spectate: cannot start renderer: %s\n" % exc)
            return 1
        if banner:
            session.submit_note(banner)
        try:
            proc = subprocess.Popen(launcher_argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
        except OSError as exc:
            _report_launch_failure(launcher_argv[0], exc)
            return 1
        try:
            status, failure = _relay(proc, session, tf)
        except _Terminated as exc:
            interrupted = exc.signum
        except KeyboardInterrupt:
            interrupted = signal.SIGINT
        except OSError as exc:
            status = 1
            failure = "relay failed: %s" % exc
        except Exception as exc:      # never traceback out of the wrapper
            status = 1
            failure = "relay failed: %s: %s" % (type(exc).__name__, exc)
    finally:
        graceful = failure is None and interrupted is None
        launcher_code = None
        if proc is not None:
            if graceful:
                launcher_code, timed_out = _wait_launcher(proc, wait_limit)
                if timed_out:
                    failure = ("launcher did not exit within %.0f s"
                               % wait_limit)
                    graceful = False
                    _kill_launcher(proc)
                    launcher_code = None
            else:
                launcher_code, _ = _kill_launcher(proc)
        if interrupted is not None:
            status = 128 + interrupted
            failure = failure or "terminated by signal %d" % interrupted
        elif failure is not None:
            status = status if status else 1
        else:
            status = _exit_status(launcher_code)
        try:
            if graceful:
                session.finish()
            else:
                session.abort(failure or "relay stopped")
        finally:
            try:
                stats = session.stats()
            finally:
                session.close()
        if status == 0 and _render_failed_unreported(stats):
            status = 1
            failure = failure or "rendering failed with no reportable sink"
        if failure:
            sys.stderr.write("spectate: %s\n" % failure)
        _close_quietly(tf)
    return status


def _report_launch_failure(path, exc):
    sys.stderr.write("spectate: cannot launch %s: %s\n"
                     "  set --launcher or SPECTATE_LAUNCHER\n" % (path, exc))


# ------------------------------------------------------------------
# replay
# ------------------------------------------------------------------


def parse_speed(value):
    """--replay-speed -> seconds between frames (0.0 == instant)."""
    if value is None or str(value).strip() == "":
        return 0.0
    text = str(value).strip().lower()
    if text in ("instant", "full", "0", "0.0"):
        return 0.0
    fps = _parse_float(value, "--replay-speed", positive=True)
    return 1.0 / fps


def _replay_emit(fd, painter, rec):
    if rec.get("type") == "obs":
        payload = painter.obs(rec)
    else:
        line = one_liner(rec)
        if line is None:
            return True
        payload = painter.note(line)
    try:
        _write_all(fd, payload)
    except OSError as exc:
        sys.stderr.write("spectate: cannot render replay: %s\n" % exc)
        return False
    return True


def run_replay(path, painter, dest_fd, delay=0.0):
    """Render a saved transcript line by line, with no live coalescing."""
    close = False
    if path == "-":
        fh = sys.stdin
    else:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write("spectate: cannot read transcript %s: %s\n"
                             % (path, exc))
            return 1
        close = True
    assembler = format_obs.IncrementalAssembler()
    try:
        for line in fh:
            try:
                records = assembler.feed(line)
            except (format_obs.ChunkError, format_obs.AssemblerLimit,
                    ValueError, KeyError, TypeError, IndexError) as exc:
                sys.stderr.write("spectate: stream not renderable: "
                                 "%s\n" % exc)
                return 1
            for rec in records:
                if not _replay_emit(dest_fd, painter, rec):
                    return 1
                if delay:
                    time.sleep(delay)
        for note in assembler.finish():
            sys.stderr.write("spectate: %s\n" % note)
    except KeyboardInterrupt:
        sys.stderr.write("spectate: replay interrupted\n")
        return 130
    finally:
        if close:
            _close_quietly(fh)
    return 0


# ------------------------------------------------------------------
# command line
# ------------------------------------------------------------------

_VALUE_OPTS = {
    "--launcher": "launcher",
    "--transcript": "transcript",
    "--render-fd": "render_fd",
    "--messages": "messages",
    "--min-frame-interval": "min_frame_interval",
    "--replay-speed": "replay_speed",
}
_FLAG_OPTS = {
    "--no-color": ("color", False),
    "--color": ("color", True),
    "--quiet": ("quiet", True),
    "-q": ("quiet", True),
}
_SUBCOMMANDS = ("wrap", "replay", "selftest", "help", "-h", "--help")


def split_opts(argv):
    """Split argv into (spectate options, everything else, in order).

    Handles ``--name value`` and ``--name=value``.  Unrecognised arguments --
    the launcher's own options and their values -- are preserved verbatim so a
    ``wrap`` call can forward them untouched.
    """
    opts = {}
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i + 1:])
            break
        name, eq, value = arg.partition("=")
        if name in _VALUE_OPTS:
            if eq:
                opts[_VALUE_OPTS[name]] = value
                i += 1
            else:
                if i + 1 >= len(argv):
                    raise UsageError("%s needs a value" % name)
                opts[_VALUE_OPTS[name]] = argv[i + 1]
                i += 2
            continue
        if arg in _FLAG_OPTS:
            key, val = _FLAG_OPTS[arg]
            opts[key] = val
            i += 1
            continue
        rest.append(arg)
        i += 1
    return opts, rest


def _env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _parse_int(raw, name, minimum=None):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        raise UsageError("%s must be an integer, not %r" % (name, raw))
    if minimum is not None and value < minimum:
        raise UsageError("%s must be >= %d" % (name, minimum))
    return value


def _parse_float(raw, name, minimum=None, positive=False):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        raise UsageError("%s must be a number, not %r" % (name, raw))
    if not math.isfinite(value):
        raise UsageError("%s must be finite, not %r" % (name, raw))
    if positive and value <= 0:
        raise UsageError("%s must be positive" % name)
    if minimum is not None and value < minimum:
        raise UsageError("%s must be >= %g" % (name, minimum))
    return value


def resolve(opts, default_render_fd):
    """Merge explicit options, SPECTATE_* environment and defaults."""
    cfg = {}
    cfg["launcher"] = (opts.get("launcher") or _env("SPECTATE_LAUNCHER")
                       or DEFAULT_LAUNCHER)
    cfg["transcript"] = (opts.get("transcript")
                         or _env("SPECTATE_TRANSCRIPT"))
    cfg["render_fd"] = (opts.get("render_fd")
                        or _env("SPECTATE_RENDER_FD") or default_render_fd)
    raw = opts.get("messages", _env("SPECTATE_MESSAGES"))
    cfg["messages"] = DEFAULT_MESSAGES if raw is None \
        else _parse_int(raw, "--messages", 0)
    raw = opts.get("min_frame_interval",
                   _env("SPECTATE_MIN_FRAME_INTERVAL"))
    cfg["min_frame_interval"] = DEFAULT_MIN_FRAME_INTERVAL \
        if raw is None else _parse_float(raw, "--min-frame-interval", 0.0)
    cfg["replay_speed"] = opts.get("replay_speed",
                                   _env("SPECTATE_REPLAY_SPEED"))
    if "color" in opts:
        cfg["color"] = opts["color"]
    elif _env("NO_COLOR") or _env("SPECTATE_NO_COLOR"):
        cfg["color"] = False
    else:
        cfg["color"] = None
    cfg["quiet"] = bool(opts.get("quiet"))
    return cfg


def _install_signal_handlers():
    def handler(signum, _frame):
        raise _Terminated(signum)
    for name in ("SIGTERM", "SIGHUP"):
        num = getattr(signal, name, None)
        if num is None:
            continue
        try:
            signal.signal(num, handler)
        except (ValueError, OSError):
            pass


def cmd_wrap(opts, launcher_args):
    cfg = resolve(opts, default_render_fd="2")
    try:
        dest = open_render(cfg["render_fd"], live=True)
    except UsageError:
        raise
    except OSError as exc:
        sys.stderr.write("spectate: cannot open the render side channel: "
                         "%s\n" % exc)
        return 1
    _install_signal_handlers()
    color = cfg["color"]
    if color is None:
        color = dest.tty
    try:
        session = LiveRenderSession(
            dest, messages=cfg["messages"], color=color,
            min_frame_interval=cfg["min_frame_interval"])
        banner = None
        if not cfg["quiet"]:
            lines = ["spectate: %s %s" % (cfg["launcher"],
                                          " ".join(launcher_args))]
            if cfg["transcript"]:
                lines.append("spectate: transcript -> %s" % cfg["transcript"])
            if dest.note:
                lines.append("spectate: %s" % dest.note)
            banner = "\n".join(lines)
        return run_proxy([cfg["launcher"]] + list(launcher_args), session,
                         cfg["transcript"], banner=banner)
    finally:
        dest.close()


def cmd_replay(opts, path):
    cfg = resolve(opts, default_render_fd="1")
    delay = parse_speed(cfg["replay_speed"])
    try:
        dest = open_render(cfg["render_fd"], live=False)
    except UsageError:
        raise
    except OSError as exc:
        sys.stderr.write("spectate: cannot open the render side channel: "
                         "%s\n" % exc)
        return 1
    _install_signal_handlers()
    color = cfg["color"]
    if color is None:
        color = dest.tty
    painter = FramePainter(cfg["messages"], color, dest.tty)
    try:
        return run_replay(path, painter, dest.fd, delay)
    finally:
        dest.close()


def usage(out):
    out.write(__doc__)


def main(argv):
    if argv and argv[0] == _WRITER_MODE_ARG \
            and os.environ.get(_WRITER_MARKER_ENV) == _WRITER_MARKER_VALUE:
        return _writer_helper_main(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage(sys.stdout)
        return 0
    cmd = argv[0]
    try:
        if cmd == "selftest":
            return selftest()
        if cmd == "replay":
            opts, rest = split_opts(argv[1:])
            if len(rest) != 1:
                raise UsageError("replay needs exactly one FILE")
            return cmd_replay(opts, rest[0])
        if cmd == "wrap":
            opts, rest = split_opts(argv[1:])
        else:
            # runner mode: the driver built this argv, so every element is a
            # launcher argument.  Our own options come from SPECTATE_* only.
            opts, rest = {}, list(argv)
        return cmd_wrap(opts, rest)
    except UsageError as exc:
        sys.stderr.write("spectate: %s\n" % exc)
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("spectate: interrupted\n")
        return 130
    except _Terminated as exc:
        sys.stderr.write("spectate: terminated by signal %d\n" % exc.signum)
        return 128 + exc.signum


# ------------------------------------------------------------------
# selftest (synchronous; the integration suite lives in test_spectate.py)
# ------------------------------------------------------------------


def _tiny_obs():
    return {
        "v": 1, "ch": "player", "type": "obs", "d": 2, "seq": 1,
        "base": None, "s": {"time": {"text": "42", "color": "none",
                                     "style": 0}},
        "cond": [], "pal": [[0, " ", "none", 0, "none"],
                            [1, "@", "white", 0, "none"]],
        "map": [[8, 10, 1]], "cur": [8, 11],
        "msg": [{"e": 1, "text": "hi", "style": 0}], "hist": [],
        "windows": [], "need": {"id": 1, "kind": "command"},
    }


def _render_obs(rec):
    return FramePainter(messages=3, color=False,
                        tty=False).obs(rec).decode("utf-8")


def _chunk_lines(obs):
    """Chunk `obs` the way the wire would, as raw JSON lines."""
    head = [{"p": "h", "k": "v", "val": obs["v"]},
            {"p": "h", "k": "ch", "val": obs["ch"]},
            {"p": "h", "k": "seq", "val": obs["seq"]},
            {"p": "h", "k": "base", "val": obs["base"]}]
    parts = [head,
             [{"p": "s", "k": "time", "val": obs["s"]["time"]},
              {"p": "pal", "val": obs["pal"][0]},
              {"p": "pal", "val": obs["pal"][1]},
              {"p": "map", "val": obs["map"][0]},
              {"p": "cur", "val": obs["cur"]},
              {"p": "msg", "val": obs["msg"][0]},
              {"p": "need", "val": obs["need"]}]]
    lines = []
    for i, part in enumerate(parts):
        rec = {"v": 1, "ch": "control", "type": "chunk", "d": 1, "rid": 2,
               "i": i, "last": i == len(parts) - 1, "parts": part}
        lines.append(json.dumps(rec))
    return lines


def _read_available(fd):
    """Drain whatever a non-blocking fd already holds, without closing it."""
    os.set_blocking(fd, False)
    chunks = []
    while True:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def _capture_session(records, interval, raw=None, gap=0.0, probe_delay=0.0,
                     timeout=3.0):
    """Drive a real LiveRenderSession over a capture pipe.

    Returns (rendered text, stats).  ``gap`` sleeps before the second record
    so coalescing is not racing the worker, and ``probe_delay`` reads the
    capture pipe *before* finishing, which is what proves an idle deadline
    fired on its own rather than the EOF flush forcing it.
    """
    read_fd, write_fd = os.pipe()
    dest = RenderDestination(write_fd, True, False, "capture")
    session = LiveRenderSession(dest, messages=3, color=False,
                                min_frame_interval=interval)
    session.start()
    if raw is not None:
        session.submit_bytes(raw)
    for index, rec in enumerate(records):
        if gap and index == 1:
            time.sleep(gap)
        session.submit_bytes((json.dumps(rec) + "\n").encode("utf-8"))
    early = b""
    if probe_delay:
        time.sleep(probe_delay)
        early = _read_available(read_fd)
    session.finish(timeout)
    data = early + _read_available(read_fd)
    stats = session.stats()
    session.close()
    os.close(read_fd)
    return data.decode("utf-8", "replace"), stats


def selftest():
    bad = 0

    obs = _tiny_obs()
    text = _render_obs(obs)

    if "\x1b" in text:
        print("SELFTEST FAIL: --no-color output still carries ANSI")
        bad += 1
    if "  |        *" not in text:
        print("SELFTEST FAIL: the map row does not place the cursor glyph")
        bad += 1
    if "status: time=42" not in text:
        print("SELFTEST FAIL: the status line is missing")
        bad += 1
    if "msg[1] hi" not in text:
        print("SELFTEST FAIL: the trailing message is missing")
        bad += 1
    if "need: command" not in text:
        print("SELFTEST FAIL: the outstanding need is missing")
        bad += 1
    if len([ln for ln in text.splitlines() if ln.startswith("  |")]) \
            != format_obs.MAP_H:
        print("SELFTEST FAIL: the frame is not MAP_H rows tall")
        bad += 1

    # A chunk stream must assemble to the identical frame.
    asm = format_obs.IncrementalAssembler()
    records = []
    for line in _chunk_lines(obs):
        records.extend(asm.feed(line))
    painter = FramePainter(messages=3, color=False, tty=False)
    chunked = b"".join(painter.obs(rec) for rec in records).decode("utf-8")
    if chunked != text:
        print("SELFTEST FAIL: the chunked obs did not render identically")
        bad += 1

    # Coalescing: with a long interval the first obs draws at once and every
    # fast follower collapses into the newest one (the middle never renders).
    first, middle, last = _tiny_obs(), _tiny_obs(), _tiny_obs()
    first["seq"], middle["seq"], last["seq"] = 1, 2, 3
    drawn, _stats = _capture_session([first, middle, last], 10.0, gap=0.3)
    if "seq=1" not in drawn or "seq=3" not in drawn or "seq=2" in drawn:
        print("SELFTEST FAIL: fast obs did not coalesce to the newest frame")
        bad += 1

    # Deadline: a second observation inside the interval still renders at the
    # idle deadline, with no further input byte and before any EOF flush.
    first, second = _tiny_obs(), _tiny_obs()
    first["seq"], second["seq"] = 1, 2
    drawn, _stats = _capture_session([first, second], 0.2, probe_delay=0.8)
    if "seq=2" not in drawn:
        print("SELFTEST FAIL: the idle deadline did not draw the newest obs")
        bad += 1

    # Non-obs records are one-liners.
    if one_liner({"type": "closed"}) != "closed":
        print("SELFTEST FAIL: closed is not a one-liner")
        bad += 1
    if not one_liner({"type": "invalid", "d": 3,
                      "code": "stale"}).startswith("invalid d=3"):
        print("SELFTEST FAIL: invalid is not a one-liner")
        bad += 1

    # A non-JSON line must never crash anything: rendering disables and the
    # wire keeps flowing.
    _drawn, stats = _capture_session([], 0.0, raw=b"not json at all\n")
    if not stats["render_disabled"]:
        print("SELFTEST FAIL: a non-JSON line did not disable rendering")
        bad += 1

    # Live fd 1 is rejected before anything is spawned.
    try:
        open_render("1", live=True)
    except UsageError:
        pass
    else:
        print("SELFTEST FAIL: live render fd 1 was accepted")
        bad += 1

    # An unreadable transcript is a clean failure, not a traceback.
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        rc = run_replay("/nonexistent/spectate-transcript",
                        FramePainter(color=False), 1)
    finally:
        sys.stderr = old
    if rc == 0:
        print("SELFTEST FAIL: a missing transcript did not fail")
        bad += 1

    if bad:
        print("spectate selftest: %d failure(s)" % bad)
        return 1
    print("spectate selftest: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
