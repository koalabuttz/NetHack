"""Deterministic presentation: one logical record -> the lines of one frame.

This is the pure, reusable rendering layer promoted out of the test-side
``test/agent/spectate.py`` so the live autoplay controller and the spectator
tool share one implementation instead of drifting.  Nothing here performs
I/O, reads a clock, consults policy, or reaches into the controller: every
function reads its arguments synchronously and returns detached strings.

Two generations of formatting live here side by side:

  * the **legacy spectator** formatting (``obs_frame``, ``one_liner``,
    ``FramePainter`` and the module color tables) is byte-for-byte the
    behavior ``spectate.py`` always had -- its bordered, ANSI-colored map and
    in-place TTY redraw are frozen, and both the replay path and the selftest
    drive it directly;
  * the **autoplay frame** (``auto_frame``) renders the same presentation
    authority -- an applied :class:`~tools.agent.protocol.Snapshot` -- as an
    explicitly 80-column, plain-ASCII block for a live side channel.

The chunk assembler and snapshot decoder live in ``tools.agent.codec``; this
module imports them directly (render -> codec is cycle-free, and ``codec``
imports only ``json``).
"""

import json
import os
import sys

# Make ``tools.agent.codec`` importable however this module is reached: as
# ``tools.agent.render`` from the package, or as a top-level ``render`` after
# ``test/agent`` put itself on ``sys.path`` (the ``format_obs.py`` pattern).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.agent import codec as _codec  # noqa: E402

# The map presentation authority: the canonical decoder's dimensions and its
# strict grid builder.  ``format_obs`` re-exports the same names.
MAP_W = _codec.MAP_W
MAP_H = _codec.MAP_H
map_grid = _codec.map_grid

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


# ------------------------------------------------------------------
# legacy spectator formatting: one logical record -> the lines of one frame
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
    for x in range(MAP_W):
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


def _need_body(need, windows):
    """The outstanding request as a compact line body, or None if no need."""
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
    return text


def _need_text(need, windows):
    """The outstanding request as one compact line body (kind + identity)."""
    body = _need_body(need, windows)
    return None if body is None else "  need: " + body


def obs_frame(rec, messages=DEFAULT_MESSAGES, color=True):
    """The frame for one observation: map, status, messages, need."""
    lines = ["obs d=%s seq=%s base=%s"
             % (rec.get("d"), rec.get("seq"), rec.get("base"))]
    status = _status_line(rec)
    lines.append("  status: " + (status if status else "-"))
    cond = rec.get("cond") or []
    if cond:
        lines.append("  cond: " + " ".join(c["text"] for c in cond))

    grid, cur = map_grid(rec)
    for y in range(MAP_H):
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
# autoplay frame: the same presentation authority, plain and bounded
# ------------------------------------------------------------------


def _clip80(text):
    """Printable-ASCII, deterministically clipped to 80 columns.

    Every non-map autoplay line goes through this: any character outside
    printable ASCII (a tab, a control byte, a non-ASCII glyph the profile
    should not have sent) becomes ``?``, and the result is truncated to 80
    columns.  The mapping is one character to one column, so the clip is
    stable and can never reflow a frame.
    """
    out = []
    for ch in str(text):
        o = ord(ch)
        out.append(ch if 32 <= o <= 126 else "?")
    return "".join(out)[:80]


def _glyph(ch):
    """A single printable-ASCII map cell; non-ASCII degrades to ``?``."""
    if ch and len(ch) == 1 and 32 <= ord(ch) <= 126:
        return ch
    return "?"


def _auto_status_line(memory):
    """Status from durable memory; an unknown field is ``?``, never zero."""
    st = memory.status
    hp = "?" if st.hp is None else str(st.hp)
    hp_max = "?" if st.hp_max is None else str(st.hp_max)
    return ("  status: dlvl=%s hp=%s/%s time=%s xp=%s hunger=%s gold=%s"
            % (_or_q(st.dlvl), hp, hp_max, _or_q(st.time),
               _or_q(st.level), _or_q(st.hunger), _or_q(st.gold)))


def _or_q(value):
    return "?" if value is None or value == "" else str(value)


def _auto_directives_line(view):
    """The peeked directive state; ``none`` when no set is in force."""
    if view is None or not getattr(view, "active", False):
        return "  directives: none"
    dset = view.dset
    target = ("%d,%d" % dset.target) if dset.target else "-"
    return ("  directives: gen=%d goals=%s target=%s risk=%.2f ttl=%d"
            % (view.generation, ",".join(dset.goals), target, dset.risk,
               dset.ttl))


def _auto_counters_line(strategy_calls, usage):
    """Settled counters; hit% is over classified tokens only, else ``n/a``."""
    u = usage or {}
    hit = u.get("cache_hit_tokens", 0) or 0
    miss = u.get("cache_miss_tokens", 0) or 0
    unclassified = u.get("cache_unclassified_tokens", 0) or 0
    denom = hit + miss
    rate = "n/a" if denom <= 0 else "%.1f%%" % (100.0 * hit / denom)
    return ("  counters: strategy_calls=%d cache_hit=%d cache_miss=%d "
            "unclass=%d hit=%s"
            % (strategy_calls, hit, miss, unclassified, rate))


def auto_frame(snapshot, memory, *, episode, seq, tick, need, windows,
               directives, strategy_calls, usage, messages=DEFAULT_MESSAGES,
               final_reason=None, outcome=None):
    """The autoplay frame for one accepted observation (or the final state).

    Reads its arguments synchronously and retains none: all returned strings
    are detached.  The map is the *currently applied* :class:`Snapshot` only
    (never remembered terrain), so a cell that has disappeared from the live
    presentation is blank here.  The cursor is the snapshot's own cursor
    (never the remembered hero), overlaid as ``*`` at its valid coordinate
    without mutating any cell.  Status comes from durable memory; recent
    messages are episode history (a message-less new observation therefore
    retains the earlier lines); directives are the *peeked* view (never
    expired by rendering); need titles come from ``snapshot.windows`` with
    ``need`` overriding ``snapshot.need``.

    ``final_reason``/``outcome`` turn this into the final frame by adding the
    ``final`` clause; they are always freshly supplied, never a replay of an
    older candidate.  Output is plain text (no ANSI): 21 bare 80-column map
    rows plus compact annotation lines clipped to 80 columns.
    """
    lines = []
    header = "auto episode=%s seq=%s tick=%s" % (episode, seq, tick)
    if final_reason is not None or outcome is not None:
        header += " final stop=%s outcome=%s" % (
            final_reason if final_reason is not None else "?",
            outcome if outcome is not None else "?")
    lines.append(_clip80(header))
    lines.append(_clip80(_auto_status_line(memory)))

    # Map: exactly MAP_H rows of MAP_W ASCII cells.  x=0 is a synthesized
    # blank (the engine map is x=1..79); a cell absent from the applied
    # snapshot is blank, not remembered.
    cur = snapshot.cur
    for y in range(MAP_H):
        row = []
        for x in range(MAP_W):
            if x == 0:
                row.append(" ")
                continue
            if cur is not None and (x, y) == cur:
                row.append("*")
                continue
            cell = snapshot.map.get((x, y))
            row.append(_glyph(cell[0]) if cell else " ")
        lines.append("".join(row))

    if messages > 0:
        for text in memory.recent_messages(messages):
            lines.append(_clip80("  msg: " + (text or "")))

    body = _need_body(need, windows)
    if body is not None:
        lines.append(_clip80("  need: " + body))

    lines.append(_clip80(_auto_directives_line(directives)))
    lines.append(_clip80(_auto_counters_line(strategy_calls, usage)))
    return lines
