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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Bounds mirrored from win/agent/agent_types.h.
MAP_MIN_X, MAP_MAX_X = 1, 79
MAP_MIN_Y, MAP_MAX_Y = 0, 20
KEY_MIN, KEY_MAX = 1, 255
LINE_INPUT_MAX = 255
PROMPT_MAX_LEN = 1048576           # schema maxLength for a need's prompt text
COUNT_MAX = 2147483647
MAX_MENU_ROWS = 65535

INVALID_CODES = ("schema", "stale", "kind", "range", "incomplete")

# Frozen contract identity (doc/agent-interface.md sections 3 and 5.1).  A
# session begins with exactly one hello that names these; anything else is a
# protocol failure, not something to play through.
EXPECTED_PROFILE = "normal-ascii-color-v1"
EXPECTED_POLICY = "llm-final-v1"
REQUIRED_CAPS = ("snapshot", "menu", "paging")
EXPECTED_COORD = "engine-map"

# Physical bounds.  A malicious or broken peer must not be able to make the
# controller allocate without limit: the physical line, the assembler's
# retained chunk state and the number of concurrent chunk streams are all
# capped, and exceeding one is a per-episode protocol failure.
MAX_PHYSICAL_LINE = 1 << 20        # bytes before json.loads ever sees a line
MAX_RETAINED_BYTES = 1 << 20       # assembler retained chunk bytes
MAX_CHUNKS = 4096                  # assembler retained logical chunks
MAX_STREAMS = 64                   # concurrent chunk streams
MAX_PAGES = 65535                  # protocol-legal page count per request
MAX_COUNTER = 9007199254740991     # public counter ceiling (2**53-1)

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
    """The one outstanding need and its delivery obligations.

    Page transfer keeps **exactly one** ``get_page`` in flight: a page request
    is built only while no response is outstanding, and a page is marked
    requested only after its complete request line reached the wire (the
    controller calls :meth:`mark_page_requested` then).  This bounds the
    outbound obligation to one small line per delivered page, so a peer that
    advertises many pages and reads stdin slowly can never wedge the pipe.
    """

    def __init__(self) -> None:
        self.need: Optional[dict] = None
        self.seq = 0
        self.pages_declared = 0
        self.pages_delivered: Dict[int, List[Any]] = {}
        self.in_flight: Optional[int] = None
        self.requested: set = set()
        self.content: Optional[str] = None
        self.menu: Optional[str] = None

    def begin(self, need: Optional[dict], seq: int) -> None:
        self.need = need
        self.seq = seq
        self.pages_delivered = {}
        self.in_flight = None
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

    def next_page_request(self) -> Optional[dict]:
        """The single ``get_page`` still owed, or None.

        Returns None while a response is outstanding so the caller can never
        have two page requests in flight at once.
        """
        if not self.need or self.pages_declared <= 0:
            return None
        if self.in_flight is not None:
            return None
        for k in range(self.pages_declared):
            if k not in self.pages_delivered and k not in self.requested:
                return make_get_page(self.need["id"], self.content, k)
        return None

    def mark_page_requested(self, page: int) -> None:
        """Record that page *page*'s complete request line was written."""
        self.requested.add(page)
        self.in_flight = page

    def reset_delivery(self) -> None:
        """Re-arm the page obligation (used after ``invalid(incomplete)``)."""
        self.pages_delivered = {}
        self.requested = set()
        self.in_flight = None

    def note_page(self, rec: dict) -> None:
        """Deliver the response for the one outstanding page request.

        Only the exact page that is in flight is accepted; a response for any
        other page is ignored here (a strict caller rejects it first), so the
        request can never be marked delivered by a page it never asked for.
        """
        idx = rec.get("page")
        if idx != self.in_flight:
            return
        self.in_flight = None
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


# ------------------------------------------------------------- hello check

def validate_hello(rec: dict, profile: str = EXPECTED_PROFILE,
                   policy: str = EXPECTED_POLICY,
                   caps=REQUIRED_CAPS,
                   coord: str = EXPECTED_COORD) -> Optional[str]:
    """Return None if *rec* is a compatible ``hello``, else a reason.

    Compatibility is a value equality on the frozen contract identity, never
    a substring or version-range guess: an unknown profile or delivery policy
    is a different contract the controller does not speak.
    """
    if not isinstance(rec, dict):
        return "hello is not an object"
    if rec.get("v") != 1:
        return "hello v is not 1"
    if rec.get("ch") != "control":
        return "hello channel is not control"
    if rec.get("profile") != profile:
        return "incompatible profile %r" % (rec.get("profile"),)
    if rec.get("policy") != policy:
        return "incompatible delivery policy %r" % (rec.get("policy"),)
    if rec.get("coord") != coord:
        return "unexpected coordinate system %r" % (rec.get("coord"),)
    have = rec.get("caps")
    if not isinstance(have, list):
        return "hello caps is not a list"
    missing = [c for c in caps if c not in have]
    if missing:
        return "hello is missing capabilities %r" % (missing,)
    size = rec.get("size")
    if (not isinstance(size, (list, tuple)) or len(size) != 2
            or not all(_is_int(v) for v in size)):
        return "hello size is not a [w,h] pair"
    return None


# --------------------------------------------------------------- validation

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ------------------------------------------------------------ need validation

# The complete shape of one outstanding request, mirroring the frozen client
# schema (``doc/agent-v1.schema.json``, the ``need`` ``oneOf``) and the
# engine-side range gate in ``agent_protocol.c``.  The kind selects the
# required field set, every field is type- and bound-checked, and an
# unexpected field is rejected.  Validating the *whole* need here -- before
# anything is stored on the :class:`Request` -- is what keeps a malformed
# request from raising later inside ``pages_complete``/``next_page_request``,
# which runs outside the controller's per-episode failure boundary and would
# abort the entire campaign instead of one episode.
_NEED_FIELDS = {
    "command": (("id", "kind"), ("prompt",)),
    "key": (("id", "kind"), ("prompt",)),
    "direction": (("id", "kind"), ("prompt",)),
    "position": (("id", "kind", "prompt", "x0", "y0", "x1", "y1"), ()),
    "yn": (("id", "kind", "prompt", "choices", "default", "numeric"), ()),
    "line": (("id", "kind", "prompt", "max"), ()),
    "extcmd": (("id", "kind", "prompt", "max"), ()),
    "menu": (("id", "kind", "menu", "mode", "content", "pages"), ()),
    "ack": (("id", "kind", "content", "pages"), ()),
}
_MENU_MODES = ("none", "one", "any")


def validate_need(need: Any) -> Optional[str]:
    """Return None if *need* is a complete, well-shaped request, else why.

    This is a shape and range check only, never game semantics: it pins the
    required fields, types and bounds of the frozen contract so a peer cannot
    hand the controller a need that only fails *later*, when the page/chunk
    obligations read it.  The controller converts any reason into an
    episode-local protocol failure.
    """
    if not isinstance(need, dict):
        return "need is not an object"
    kind = need.get("kind")
    if kind not in _NEED_FIELDS:
        return "unknown need kind %r" % (kind,)
    required, optional = _NEED_FIELDS[kind]
    for name in required:
        if name not in need:
            return "%s need is missing %r" % (kind, name)
    for name in need:
        if name not in required and name not in optional:
            return "%s need has an unexpected field %r" % (kind, name)
    return _check_need_fields(kind, need)


def _check_need_fields(kind, need) -> Optional[str]:
    if not (_is_int(need["id"]) and 1 <= need["id"] <= MAX_COUNTER):
        return "id is not a public counter in 1..%d" % MAX_COUNTER
    # prompt is a string wherever it appears: the frozen schema types it
    # "string" (never null) and marks it required for every kind but
    # command/key/direction, where it is still a string when present.  The
    # engine always publishes one, empty when there is none, so a null must
    # fail here rather than be stored for a later reader.
    if "prompt" in need:
        prompt = need["prompt"]
        if not isinstance(prompt, str):
            return "prompt must be a string"
        if len(prompt) > PROMPT_MAX_LEN:
            return "prompt is longer than %d characters" % PROMPT_MAX_LEN
    # choices is the one need field the schema types as string-or-null
    if "choices" in need and need["choices"] is not None \
            and not isinstance(need["choices"], str):
        return "choices must be a string or null"
    if "default" in need:
        d = need["default"]
        if d is not None and not (_is_int(d) and KEY_MIN <= d <= KEY_MAX):
            return "default must be null or a key byte"
    if "numeric" in need and not isinstance(need["numeric"], bool):
        return "numeric must be a boolean"
    if "max" in need:
        m = need["max"]
        if not (_is_int(m) and 0 <= m <= LINE_INPUT_MAX):
            return "max must be in 0..%d" % LINE_INPUT_MAX
    if kind == "menu":
        if need["mode"] not in _MENU_MODES:
            return "mode must be none, one or any"
        if not _ref_id(need["menu"], "m"):
            return "menu must be a generation id like mN"
    if "content" in need and not _ref_id(need["content"], "c"):
        return "content must be a content id like cN"
    if "pages" in need:
        p = need["pages"]
        if not (_is_int(p) and 0 <= p <= MAX_PAGES):
            return "pages must be in 0..%d" % MAX_PAGES
    for name, lo, hi in (("x0", MAP_MIN_X, MAP_MAX_X),
                         ("x1", MAP_MIN_X, MAP_MAX_X),
                         ("y0", MAP_MIN_Y, MAP_MAX_Y),
                         ("y1", MAP_MIN_Y, MAP_MAX_Y)):
        if name in need and not (_is_int(need[name])
                                 and lo <= need[name] <= hi):
            return "%s is outside the map rectangle" % name
    return None


def _ref_id(v, letter) -> bool:
    """True for a generation/content reference like ``m1`` or ``c12``."""
    return isinstance(v, str) and \
        re.match(r"^%s[1-9][0-9]*\Z" % letter, v) is not None


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
        # an explicit zero max is preserved (it forbids any non-empty text);
        # only a *missing* max falls back to the full line budget, so a
        # need that advertises zero is never silently widened to 255
        limit = need["max"] if "max" in need else LINE_INPUT_MAX
        # a malformed need that advertises a non-integer or out-of-range max
        # is a shape error here, never a TypeError or a silent pass
        if not _is_int(limit) or not (0 <= limit <= LINE_INPUT_MAX):
            return "the need advertises an invalid max"
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
