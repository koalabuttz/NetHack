"""Bounded v1 wire protocol: request state machine, obligations, validation.

The controller owns the pipe (``controller.py``); this module owns everything
about *what the wire means* that does not depend on scheduling:

  * :class:`Snapshot` -- the public presentation model, rebuilt atomically
    from each ``base:null`` full snapshot (adapted from the test driver's
    ``Client``; it stays an independent implementation on purpose).
  * :class:`Request` -- the one outstanding need plus its page/chunk
    obligations.
  * :func:`validate_action` -- a structural mirror of the engine's per-kind
    action gate (``win/agent/agent_protocol.c``), so a decision can be checked
    before it is sent.  It validates *shape*, never game semantics.
  * :func:`make_act`, :func:`make_get_page`, :func:`make_ack_chunk` -- the
    outbound envelope builders.

Nothing here blocks, spawns, or touches the network.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Bounds mirrored from win/agent/agent_types.h.
MAP_MIN_X, MAP_MAX_X = 1, 79
MAP_MIN_Y, MAP_MAX_Y = 0, 20
KEY_MIN, KEY_MAX = 1, 255
LINE_INPUT_MAX = 255
COUNT_MAX = 2147483647
MAX_MENU_ROWS = 65535

INVALID_CODES = ("schema", "stale", "kind", "range", "incomplete")

# Native bindings (number_pad off); the same set the quickstart documents.
KEY_H, KEY_J, KEY_K, KEY_L = ord("h"), ord("j"), ord("k"), ord("l")
KEY_Y, KEY_U, KEY_B, KEY_N = ord("y"), ord("u"), ord("b"), ord("n")
KEY_WAIT = ord(".")
KEY_SEARCH = ord("s")
KEY_EAT = ord("e")
KEY_INV = ord("i")
KEY_PICKUP = ord(",")
KEY_ESC = 27
KEY_HASH = ord("#")

DIR_KEYS = {
    (-1, -1): KEY_Y, (0, -1): KEY_K, (1, -1): KEY_U,
    (-1, 0): KEY_H, (1, 0): KEY_L,
    (-1, 1): KEY_B, (0, 1): KEY_J, (1, 1): KEY_N,
}


class ProtocolError(Exception):
    """A transport or framing failure (never a legal `invalid`)."""


@dataclass(frozen=True)
class NeedKey:
    """The identity of one outstanding request: (episode, seq, id)."""

    episode: int
    seq: int
    id: int


class Snapshot(object):
    """A client-side presentation model rebuilt from every full snapshot.

    Every current observation is a complete ``base:null`` snapshot: the map
    array is the whole painted region for this moment, palette id 0 is the
    declared blank, and a cell absent from ``map`` is blank.  Durable
    exploration memory is kept separately in :mod:`tools.agent.state`.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pal: Dict[int, tuple] = {0: (" ", "none", 0, "none")}
        self.map: Dict[tuple, tuple] = {}
        self.cur: Optional[tuple] = None
        self.s: Dict[str, Any] = {}
        self.cond: List[Any] = []
        self.msg: List[Any] = []
        self.hist: List[Any] = []
        self.windows: Dict[str, Any] = {}
        self.need: Optional[dict] = None
        self.seq = 0

    def apply(self, rec: dict) -> None:
        """Atomically apply one full ``base:null`` snapshot."""
        if rec.get("base") is not None:
            raise ProtocolError("expected a full snapshot (base:null)")
        if "pal" not in rec or "map" not in rec:
            raise ProtocolError("full snapshot missing pal/map")
        pal: Dict[int, tuple] = {}
        for entry in rec["pal"]:
            pal[entry[0]] = (entry[1], entry[2], entry[3], entry[4])
        if pal.get(0) != (" ", "none", 0, "none"):
            raise ProtocolError("palette entry 0 is not the blank tuple")
        cells: Dict[tuple, tuple] = {}
        last = None
        for triple in rec["map"]:
            x, y, pid = triple
            if pid not in pal:
                raise ProtocolError("map references undefined palette id %r"
                                    % (pid,))
            if pid == 0:
                raise ProtocolError("blank cells must be omitted, not id 0")
            if not (MAP_MIN_X <= x <= MAP_MAX_X
                    and MAP_MIN_Y <= y <= MAP_MAX_Y):
                raise ProtocolError("map coordinate out of range: %r"
                                    % (triple,))
            order = (y, x)
            if last is not None and order < last:
                raise ProtocolError("map is not row-major ordered")
            last = order
            cells[(x, y)] = pal[pid]
        self.pal, self.map = pal, cells
        self.cur = tuple(rec["cur"]) if rec["cur"] else None
        if self.cur is not None and not (
                MAP_MIN_X <= self.cur[0] <= MAP_MAX_X
                and MAP_MIN_Y <= self.cur[1] <= MAP_MAX_Y):
            raise ProtocolError("cursor out of range: %r" % (self.cur,))
        self.s = rec.get("s") or {}
        self.cond = rec.get("cond") or []
        self.msg = rec.get("msg") or []
        self.hist = rec.get("hist") or []
        self.windows = {w["w"]: w for w in rec.get("windows") or []}
        self.need = rec.get("need")
        self.seq = rec.get("seq")

    def status_text(self) -> Dict[str, str]:
        out = {}
        for name, val in (self.s or {}).items():
            if isinstance(val, dict):
                out[name] = val.get("text") or ""
        return out

    def time_value(self) -> Optional[int]:
        t = self.s.get("time") if self.s else None
        if not t:
            return None
        try:
            return int(t["text"])
        except (TypeError, ValueError, KeyError):
            return None

    def window_title(self, content: Optional[str]) -> str:
        for w in self.windows.values():
            if w.get("content") == content:
                return w.get("title") or ""
        return ""

    def window_kind(self, content: Optional[str]) -> Optional[str]:
        for w in self.windows.values():
            if w.get("content") == content:
                return w.get("kind")
        return None


class Request(object):
    """The one outstanding need and its delivery obligations."""

    def __init__(self) -> None:
        self.need: Optional[dict] = None
        self.seq = 0
        self.pages_declared = 0
        self.pages_delivered: Dict[int, List[Any]] = {}
        self.requested: set = set()
        self.content: Optional[str] = None
        self.menu: Optional[str] = None

    def begin(self, need: Optional[dict], seq: int) -> None:
        self.need = need
        self.seq = seq
        self.pages_delivered = {}
        self.requested = set()
        if not need:
            self.pages_declared = 0
            self.content = None
            self.menu = None
            return
        self.pages_declared = need.get("pages", 0) or 0
        self.content = need.get("content")
        self.menu = need.get("menu")

    @property
    def kind(self) -> Optional[str]:
        return (self.need or {}).get("kind")

    @property
    def id(self):
        return (self.need or {}).get("id")

    def page_requests(self) -> List[dict]:
        """The ``get_page`` requests still owed, in index order."""
        out = []
        if not self.need or self.pages_declared <= 0:
            return out
        for k in range(self.pages_declared):
            if k not in self.pages_delivered and k not in self.requested:
                out.append(make_get_page(self.need["id"], self.content, k))
        return out

    def note_page(self, rec: dict) -> None:
        idx = rec.get("page")
        if idx is None:
            return
        # a page for content we are not waiting on is ignored
        if rec.get("content") != self.content and self.content is not None:
            return
        if rec.get("pages") not in (None, self.pages_declared):
            return
        self.pages_delivered.setdefault(idx, rec.get("rows") or [])

    def pages_complete(self) -> bool:
        if self.pages_declared <= 0:
            return True
        return all(k in self.pages_delivered
                   for k in range(self.pages_declared))

    def page_rows(self) -> List[Any]:
        rows = []
        for k in range(self.pages_declared):
            rows.extend(self.pages_delivered.get(k, []))
        return rows


# ------------------------------------------------------------------ builders

def make_act(seq: Optional[int], need_id, action: dict) -> dict:
    obj = {"v": 1, "type": "act", "id": need_id, "action": action}
    if seq is not None:
        obj["seq"] = seq
    return obj


def make_get_page(need_id, content: Optional[str], page: int) -> dict:
    return {"v": 1, "type": "get_page", "id": need_id, "content": content,
            "page": page}


def make_ack_chunk(rid, index: int) -> dict:
    return {"v": 1, "type": "ack_chunk", "rid": rid, "i": index}


def make_ack_seq(seq: int) -> dict:
    return {"v": 1, "type": "ack_seq", "seq": seq}


# --------------------------------------------------------------- validation

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _shape(action: dict) -> Optional[str]:
    """Return the single tagged shape of an action object, or None."""
    shapes = [k for k in ("key", "text", "position", "yn", "menu", "ack",
                          "cancel") if k in action]
    if len(shapes) != 1:
        return None
    return shapes[0]


def validate_action(need: Optional[dict], action: Any) -> Optional[str]:
    """Return None if *action* is a legal shape for *need*, else a reason.

    This mirrors ``agent_receive``'s structural gate.  It deliberately does
    not check page completeness (the :class:`Request` owns that obligation)
    nor any game semantics.
    """
    if not isinstance(need, dict):
        return "no outstanding request"
    if not isinstance(action, dict):
        return "action is not an object"
    kind = need.get("kind")
    shape = _shape(action)
    if shape is None:
        return "action must carry exactly one tagged shape"

    # counts are a modifier accepted only alongside yn
    for extra in action:
        if extra not in ("key", "text", "position", "yn", "menu", "commit",
                         "cancel", "ack", "count", "mod"):
            return "unknown action field %r" % (extra,)
    if "count" in action:
        if shape != "yn":
            return "count is only valid with yn"
        c = action["count"]
        if not _is_int(c) or not (1 <= c <= COUNT_MAX):
            return "count out of range"

    if kind in ("command", "key", "direction"):
        if shape != "key":
            return "%s needs a key action" % kind
        return _check_key(action["key"])
    if kind == "position":
        if shape == "key":
            return _check_key(action["key"])
        if shape != "position":
            return "position needs a position or key action"
        pos = action["position"]
        if (not isinstance(pos, (list, tuple)) or len(pos) != 2
                or not _is_int(pos[0]) or not _is_int(pos[1])):
            return "position must be [x,y]"
        if not (MAP_MIN_X <= pos[0] <= MAP_MAX_X
                and MAP_MIN_Y <= pos[1] <= MAP_MAX_Y):
            return "position out of the map rectangle"
        if action.get("mod", 0) != 0:
            return "mod is frozen to 0"
        return None
    if kind == "yn":
        if shape != "yn":
            return "yn needs a yn action"
        return _check_key(action["yn"])
    if kind in ("line", "extcmd"):
        if shape == "cancel":
            return None
        if shape != "text":
            return "%s needs a text or cancel action" % kind
        text = action["text"]
        if not isinstance(text, str):
            return "text must be a string"
        limit = need.get("max", LINE_INPUT_MAX) or LINE_INPUT_MAX
        if len(text.encode("utf-8")) > limit:
            return "text exceeds the advertised byte budget"
        return None
    if kind == "menu":
        if shape == "cancel":
            return None
        if shape == "ack":
            return None
        if shape != "menu":
            return "menu needs a menu, cancel or ack action"
        if action["menu"] != need.get("menu"):
            return "menu answer names a stale generation"
        commit = action.get("commit")
        if not isinstance(commit, list):
            return "commit must be a list"
        seen = set()
        for row in commit:
            if (not isinstance(row, (list, tuple)) or len(row) != 2
                    or not _is_int(row[0]) or not _is_int(row[1])):
                return "commit rows must be [row,count]"
            r, c = row
            if not (1 <= r <= MAX_MENU_ROWS):
                return "commit row id out of range"
            if c != -1 and not (1 <= c <= COUNT_MAX):
                return "commit count must be -1 or positive"
            if r in seen:
                return "duplicate commit row"
            seen.add(r)
        return None
    if kind == "ack":
        if shape == "cancel":
            return None
        if shape != "ack":
            return "ack needs an ack or cancel action"
        return None
    return "unknown need kind %r" % (kind,)


def _check_key(v) -> Optional[str]:
    if not _is_int(v) or not (KEY_MIN <= v <= KEY_MAX):
        return "key out of range"
    return None
