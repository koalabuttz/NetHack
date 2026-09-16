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
                   records to a side channel (stderr by default) plus an
                   optional verbatim transcript.  Because rendering happens
                   on a copy of the bytes, the wire is never touched: no
                   re-serialisation, no extra buffering, no byte lost.
  * ``replay``  -- render a saved ``--transcript`` file with no runner at
                   all, at full speed (default) or paced (``--replay-speed``).
  * runner mode -- with no subcommand, every remaining argument is forwarded
                   to the launcher, so ``spectate.py`` can be used directly as
                   the driver's ``--runner`` (a byte-exact proxy in the
                   driver's own process tree).  Its own options then come from
                   ``SPECTATE_*`` environment variables, because the driver --
                   not a human -- builds that argv.

Rendering reuses :mod:`format_obs`: the chunk assembler turns a chunk
stream back into logical records, and ``map_grid`` decodes the map.  The
renderer only formats; it never invents records.

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
"""

import contextlib
import json
import io
import os
import select
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import format_obs  # noqa: E402  (test-side independent chunk/page decoder)

REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_LAUNCHER = os.path.join(REPO_ROOT, "src", "nethack-agent")
DEFAULT_MIN_FRAME_INTERVAL = 0.15
DEFAULT_MESSAGES = 3

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
    """A command line this tool cannot act on."""


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


# ------------------------------------------------------------------
# the renderer: throttle/coalesce obs frames, redraw in place on a TTY
# ------------------------------------------------------------------


class Renderer(object):
    """Turns a stream of logical records into frames on a side channel.

    Consecutive observations that arrive faster than ``min_frame_interval``
    are coalesced into the newest one: a human can only read one map at a
    time, so intermediate maps are dropped rather than queued.  On a TTY the
    newest block overwrites the previous one in place (cursor-up); off a TTY
    each block is appended.
    """

    def __init__(self, out, messages=DEFAULT_MESSAGES, color=True,
                 min_frame_interval=DEFAULT_MIN_FRAME_INTERVAL, tty=False):
        self.out = out
        self.messages = messages
        self.color = color
        self.min = max(0.0, min_frame_interval)
        self.tty = tty
        self._pending = None
        self._last = 0.0
        self._height = 0

    def feed(self, rec):
        if rec.get("type") == "obs":
            self._pending = rec
            if time.monotonic() - self._last >= self.min:
                self._draw()
            return
        # a non-obs record: finalise any pending map, then show the one-liner
        self._draw()
        line = one_liner(rec)
        if line is not None:
            self._emit([line])
            self._last = time.monotonic()

    def note(self, text):
        """Emit an out-of-band line (a banner, a decoder complaint)."""
        self._draw()
        self._emit([text])
        self._last = time.monotonic()

    def flush(self):
        self._draw()
        self.out.flush()

    def _draw(self):
        if self._pending is None:
            return
        rec, self._pending = self._pending, None
        self._emit(obs_frame(rec, self.messages, self.color))
        self._last = time.monotonic()

    def _emit(self, lines):
        if self.tty:
            if self._height:
                self.out.write("\x1b[%dA" % self._height)
            for line in lines:
                self.out.write(line + "\n")
            if self._height > len(lines):
                self.out.write("\x1b[0J")
            self._height = len(lines)
        else:
            for line in lines:
                self.out.write(line + "\n")
        self.out.flush()


# ------------------------------------------------------------------
# chunk assembly, reusing format_obs, fed one line at a time
# ------------------------------------------------------------------


class Assembler(object):
    """Incremental front end to ``format_obs.assemble``.

    ``format_obs.assemble`` is a batch generator; a live proxy has one line
    at a time, so this re-runs it over the lines seen so far and yields only
    the records beyond the count already produced.  The prefix of a
    deterministic assembler is stable, so that is exact -- and it keeps the
    chunk rules in one place instead of duplicating them here.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.lines = []
        self.count = 0
        self.broken = False

    def feed(self, line):
        if self.broken or not line.strip():
            return
        self.lines.append(line)
        try:
            records = list(format_obs.assemble(self.lines))
        except (format_obs.ChunkError, ValueError) as exc:
            self.broken = True
            self.renderer.note("stream not renderable: %s" % exc)
            return
        new = records[self.count:]
        self.count = len(records)
        for rec in new:
            self.renderer.feed(rec)


# ------------------------------------------------------------------
# the transparent proxy
# ------------------------------------------------------------------


def _pump_stdin(proc):
    """Relay consumer stdin to the launcher's stdin, then close it (EOF)."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        fd = 0
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            proc.stdin.write(data)
            proc.stdin.flush()
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def run_proxy(launcher_argv, renderer, transcript_path=None):
    """Spawn the launcher and relay the player channel byte-for-byte.

    Returns the launcher's exit status so a caller that owns the process
    tree (the driver) still sees the real one.  The launcher inherits our
    stderr, so its diagnostics are preserved; the frames go to the renderer's
    own channel.
    """
    tf = open(transcript_path, "wb") if transcript_path else None
    proc = subprocess.Popen(launcher_argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    pump = threading.Thread(target=_pump_stdin, args=(proc,), daemon=True)
    pump.start()

    out = sys.stdout.buffer
    buf = bytearray()
    asm = Assembler(renderer)

    def deliver(chunk):
        try:
            out.write(chunk)
            out.flush()
        except (BrokenPipeError, OSError):
            pass
        if tf is not None:
            tf.write(chunk)
            tf.flush()
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            buf[:] = rest
            asm.feed(line.decode("utf-8", "replace"))

    try:
        while True:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                deliver(chunk)
            elif proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    deliver(rest)
                break
    finally:
        renderer.flush()
        if tf is not None:
            tf.close()
    code = proc.wait(timeout=60)
    try:
        proc.stdout.close()
    except OSError:
        pass
    return code


# ------------------------------------------------------------------
# replay
# ------------------------------------------------------------------


def parse_speed(value):
    """--replay-speed -> seconds between frames (0.0 == instant)."""
    if value is None or value == "":
        return 0.0
    if str(value).strip().lower() in ("instant", "full", "0", "0.0"):
        return 0.0
    fps = float(value)
    if fps <= 0:
        raise UsageError("--replay-speed must be positive or 'instant'")
    return 1.0 / fps


def run_replay(path, renderer, delay=0.0):
    try:
        if path == "-":
            lines = sys.stdin.read().split("\n")
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
    except OSError as exc:
        sys.stderr.write("spectate: cannot read transcript %s: %s\n"
                         % (path, exc))
        return 1
    try:
        records = list(format_obs.assemble(lines))
    except (format_obs.ChunkError, ValueError) as exc:
        sys.stderr.write("spectate: stream not renderable: %s\n" % exc)
        return 1
    for rec in records:
        renderer.feed(rec)
        if delay:
            time.sleep(delay)
    renderer.flush()
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


def resolve(opts, default_render_fd):
    """Merge explicit options, SPECTATE_* environment and defaults."""
    cfg = {}
    cfg["launcher"] = (opts.get("launcher") or _env("SPECTATE_LAUNCHER")
                       or DEFAULT_LAUNCHER)
    cfg["transcript"] = (opts.get("transcript")
                         or _env("SPECTATE_TRANSCRIPT"))
    cfg["render_fd"] = (opts.get("render_fd")
                        or _env("SPECTATE_RENDER_FD") or default_render_fd)
    raw_messages = opts.get("messages", _env("SPECTATE_MESSAGES"))
    cfg["messages"] = DEFAULT_MESSAGES if raw_messages is None \
        else int(raw_messages)
    raw_interval = opts.get("min_frame_interval",
                            _env("SPECTATE_MIN_FRAME_INTERVAL"))
    cfg["min_frame_interval"] = DEFAULT_MIN_FRAME_INTERVAL \
        if raw_interval is None else float(raw_interval)
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


class RenderStream(object):
    """A best-effort, non-blocking side channel for frames.

    Rendering must never disturb the player, so the channel is
    non-blocking: if the reader is not keeping up (or there is no reader at
    all), frames are dropped rather than allowed to stall the relay.  A
    dropped frame can leave a partial line; that is the visible price of
    never blocking the wire.
    """

    def __init__(self, fh):
        self.fh = fh
        self.fd = fh.fileno()
        self._tty = fh.isatty()
        try:
            os.set_blocking(self.fd, False)
        except (AttributeError, OSError):
            pass

    def isatty(self):
        return self._tty

    def write(self, text):
        data = memoryview(text.encode("utf-8", "replace"))
        while len(data):
            try:
                written = os.write(self.fd, data)
            except (BlockingIOError, OSError):
                return
            if written <= 0:
                return
            data = data[written:]

    def flush(self):
        pass

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def open_render(spec):
    """Open the frame side channel: an integer fd (dup'd), or 'tty'."""
    text = str(spec)
    if text in ("tty", "/dev/tty"):
        try:
            return RenderStream(open("/dev/tty", "w", encoding="utf-8",
                                     errors="replace"))
        except OSError:
            # No controlling terminal (a harness, a batch run): stderr is the
            # documented default, so fall back to it rather than fail.
            return RenderStream(os.fdopen(os.dup(2), "w", encoding="utf-8",
                                          errors="replace"))
    return RenderStream(os.fdopen(os.dup(int(text)), "w", encoding="utf-8",
                                  errors="replace"))


def _build_renderer(cfg, render, min_frame_interval):
    tty = render.isatty()
    color = cfg["color"]
    if color is None:
        color = tty
    return Renderer(render, messages=cfg["messages"], color=color,
                    min_frame_interval=min_frame_interval, tty=tty)


def cmd_wrap(opts, launcher_args):
    cfg = resolve(opts, default_render_fd="2")
    launcher = cfg["launcher"]
    if not os.path.exists(launcher):
        sys.stderr.write("spectate: launcher not found: %s\n"
                         "  set --launcher or SPECTATE_LAUNCHER\n" % launcher)
        return 2
    render = open_render(cfg["render_fd"])
    renderer = _build_renderer(cfg, render, cfg["min_frame_interval"])
    if not cfg["quiet"]:
        renderer.note("spectate: %s %s" % (launcher, " ".join(launcher_args)))
        if cfg["transcript"]:
            renderer.note("spectate: transcript -> %s" % cfg["transcript"])
    return run_proxy([launcher] + list(launcher_args), renderer,
                     cfg["transcript"])


def cmd_replay(opts, path):
    cfg = resolve(opts, default_render_fd="1")
    render = open_render(cfg["render_fd"])
    renderer = _build_renderer(cfg, render, 0.0)
    return run_replay(path, renderer, parse_speed(cfg["replay_speed"]))


def usage(out):
    out.write(__doc__)


def main(argv):
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
                sys.stderr.write("spectate: replay needs exactly one FILE\n")
                return 2
            return cmd_replay(opts, rest[0])
        if cmd == "wrap":
            opts, rest = split_opts(argv[1:])
        else:
            # runner mode: the driver built this argv, so it is all launcher
            # arguments; our options come from the SPECTATE_* environment.
            opts, rest = split_opts(argv)
        return cmd_wrap(opts, rest)
    except UsageError as exc:
        sys.stderr.write("spectate: %s\n" % exc)
        return 2


# ------------------------------------------------------------------
# selftest
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
    buf = io.StringIO()
    Renderer(buf, messages=3, color=False, min_frame_interval=0.0,
             tty=False).feed(rec)
    return buf.getvalue()


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
    buf = io.StringIO()
    renderer = Renderer(buf, messages=3, color=False, min_frame_interval=0.0,
                        tty=False)
    asm = Assembler(renderer)
    for line in _chunk_lines(obs):
        asm.feed(line)
    renderer.flush()
    if buf.getvalue() != text:
        print("SELFTEST FAIL: the chunked obs did not render identically")
        bad += 1

    # Coalescing: with a long interval, the first obs draws at once and every
    # fast follower is collapsed into the newest one (the middle is never
    # rendered).
    buf = io.StringIO()
    renderer = Renderer(buf, messages=3, color=False, min_frame_interval=10.0,
                        tty=False)
    first, middle, last = _tiny_obs(), _tiny_obs(), _tiny_obs()
    first["seq"], middle["seq"], last["seq"] = 1, 2, 3
    renderer.feed(first)
    renderer.feed(middle)
    renderer.feed(last)
    renderer.flush()
    drawn = buf.getvalue()
    if "seq=1" not in drawn or "seq=3" not in drawn or "seq=2" in drawn:
        print("SELFTEST FAIL: fast obs did not coalesce to the newest frame")
        bad += 1

    # Non-obs records are one-liners.
    if one_liner({"type": "closed"}) != "closed":
        print("SELFTEST FAIL: closed is not a one-liner")
        bad += 1
    if not one_liner({"type": "invalid", "d": 3,
                      "code": "stale"}).startswith("invalid d=3"):
        print("SELFTEST FAIL: invalid is not a one-liner")
        bad += 1

    # A non-JSON line must never crash the assembler: the proxy must keep
    # relaying bytes even when the copy it renders cannot be parsed.
    sink = Renderer(io.StringIO(), color=False, tty=False)
    asm = Assembler(sink)
    asm.feed("not json at all")
    if not asm.broken:
        print("SELFTEST FAIL: a non-JSON line did not mark the copy broken")
        bad += 1

    # An unreadable transcript is a clean failure, not a traceback.
    quiet = io.StringIO()
    with contextlib.redirect_stderr(quiet):
        rc = run_replay("/nonexistent/spectate-transcript",
                        Renderer(io.StringIO(), color=False, tty=False))
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
