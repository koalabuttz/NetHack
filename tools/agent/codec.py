"""Canonical v1 wire decoder: strict chunk assembly and snapshot decoding.

This module is the reusable implementation promoted out of
``test/agent/format_obs.py`` (see ``doc/agent-autoplay-plan.md`` section
"Layout and reuse").  ``format_obs.py`` remains the compatibility CLI/export
wrapper and re-exports every name here, so the two cannot drift.

The chunk assembler is strict, because it is also the reference for what a
client must reject:

  * chunks are stored by (rid, i) and the indices of a stream must be
    contiguous from zero;
  * an exact repeat of an already stored chunk is deduplicated;
  * a repeat that changes content under the same (rid, i) is rejected;
  * header parts may appear only in chunk 0;
  * long-text slices must name an existing element and have contiguous
    offsets, and are validated before the record is rebuilt atomically.

:class:`IncrementalAssembler` exposes the same rules one physical line at a
time (the shape a live controller needs).  The batch ``assemble`` generator is
a thin wrapper over an unlimited instance, so the two cannot drift.  A live
consumer may set retained-state budgets; when one is reached the assembler
releases every byte it holds and raises :class:`AssemblerLimit`, which means
"stop rendering", never "the wire was wrong".
"""

import json

MAP_W, MAP_H = 80, 21
BLANK = [" ", "none", 0, "none"]  # char, color, style, frame

# Conservative per-container charges, applied only when a live consumer sets a
# retained-state budget.  Batch assembly sets no budget, so these constants
# never change reference behavior.
CHUNK_OVERHEAD = 64
STREAM_OVERHEAD = 256


class ChunkError(Exception):
    """A chunk stream a client must reject."""


class AssemblerLimit(Exception):
    """A retained-state budget was exhausted.

    The assembler has already released every retained stream and canonical
    chunk.  This is not a wire rejection -- the bytes a client receives are
    untouched -- only the auxiliary copy a visualizer was assembling, so the
    caller must stop rendering rather than treat it as bad input.
    """


def canonical(parts):
    """A stable serialisation of one chunk's parts, for repeat checks."""
    return json.dumps(parts, sort_keys=True, separators=(",", ":"))


class IncrementalAssembler(object):
    """Strict chunk assembly fed one physical line at a time.

    ``feed`` returns the logical records the line completed (normally zero or
    one).  ``finish`` makes the incomplete-at-EOF policy explicit and
    ``clear`` releases every byte of retained state.

    The optional ``max_*`` budgets exist for a live consumer.  Exact
    validation of arbitrarily old retries cannot run forever with finite
    memory, so the selected policy is to disable the auxiliary view visibly at
    the cap instead of silently evicting history: a completed stream keeps its
    canonical chunks for exact post-completion retry comparison, and that
    retention is what the budgets bound.
    """

    def __init__(self, max_retained_bytes=None, max_chunks=None,
                 max_streams=None, max_line_bytes=None):
        self.max_retained_bytes = max_retained_bytes
        self.max_chunks = max_chunks
        self.max_streams = max_streams
        self.max_line_bytes = max_line_bytes
        self.clear()

    def clear(self):
        """Release all retained assembly state."""
        self._streams = {}
        self._chunks = 0
        self._bytes = 0

    def feed(self, line):
        """Feed one physical line; return the records it completes.

        Raises :class:`ChunkError` for a protocol violation and
        :class:`AssemblerLimit` when a budget is reached; ``ValueError`` and
        friends surface malformed JSON or malformed record shapes unchanged.
        """
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.strip()
        if not line:
            return []
        if self.max_line_bytes is not None \
                and len(line) > self.max_line_bytes:
            self._limit("physical line exceeds %d bytes"
                        % self.max_line_bytes)
        rec = json.loads(line)
        if rec.get("type") != "chunk":
            return [rec]

        rid = rec["rid"]
        i = rec["i"]
        canon = canonical(rec["parts"])
        slot = self._streams.get(rid)
        if slot is None:
            if self.max_streams is not None \
                    and len(self._streams) >= self.max_streams:
                self._limit("more than %d chunk streams" % self.max_streams)
            self._charge(STREAM_OVERHEAD)
            slot = {"chunks": {}, "last": None, "done": False}
            self._streams[rid] = slot

        if slot["done"]:
            # a repeat of a chunk from a completed stream is a retry
            if slot["chunks"].get(i) != canon:
                raise ChunkError(
                    "rid %s: chunk %s changed content after completion"
                    % (rid, i))
            return []

        if i in slot["chunks"]:
            if slot["chunks"][i] != canon:
                raise ChunkError(
                    "rid %s chunk %s changed content" % (rid, i))
            # an exact repeat is a retry and is ignored
        else:
            if i != len(slot["chunks"]):
                raise ChunkError(
                    "rid %s: chunk %s arrived out of order (expected %d)"
                    % (rid, i, len(slot["chunks"])))
            for part in rec["parts"]:
                if part["p"] == "h" and i != 0:
                    raise ChunkError(
                        "rid %s: header part in chunk %s" % (rid, i))
            if self.max_chunks is not None \
                    and self._chunks >= self.max_chunks:
                self._limit("more than %d chunks" % self.max_chunks)
            self._charge(len(canon) + CHUNK_OVERHEAD)
            slot["chunks"][i] = canon
            self._chunks += 1

        if rec.get("last"):
            slot["last"] = i

        if slot["last"] is not None:
            if slot["last"] != len(slot["chunks"]) - 1:
                raise ChunkError(
                    "rid %s: last flag on chunk %d but %d chunks arrived"
                    % (rid, slot["last"], len(slot["chunks"])))
            slot["done"] = True
            return [rebuild(rid, dict(slot["chunks"]))]
        return []

    def finish(self):
        """Apply the incomplete-at-EOF policy: drop tails, report them.

        Returns one diagnostic string per stream that never completed.  The
        batch path never calls this, so its output is unchanged; a live
        consumer may surface the diagnostics as a render note.
        """
        notes = []
        for rid, slot in self._streams.items():
            if not slot["done"]:
                notes.append(
                    "rid %s: stream incomplete at EOF (%d chunk(s))"
                    % (rid, len(slot["chunks"])))
        return notes

    def _charge(self, amount):
        if self.max_retained_bytes is not None \
                and self._bytes + amount > self.max_retained_bytes:
            self._limit("more than %d retained bytes"
                        % self.max_retained_bytes)
        self._bytes += amount

    def _limit(self, reason):
        self.clear()
        raise AssemblerLimit(reason)


def assemble(lines):
    """Yield logical records, validating each chunk stream strictly."""
    asm = IncrementalAssembler()
    for line in lines:
        for rec in asm.feed(line):
            yield rec


def rebuild(rid, chunks):
    """Turn a complete, validated chunk stream into one logical obs record.

    The stream carries no header d: the logical record's delivery counter
    is rid, the delivery counter of its first chunk.  Long text arrives as
    ordered t slices addressed by (kind, event-or-window, field); the target
    element must exist and the offsets must be contiguous, checked here
    before the record is produced.
    """
    parts = {}
    elements = {}   # (kind, e-or-w, field) -> [slices]
    for i in range(len(chunks)):
        for part in json.loads(chunks[i]):
            p = part["p"]
            if p == "h":
                parts[("h", part["k"])] = part["val"]
            elif p == "s":
                parts.setdefault("s", {})[part["k"]] = part["val"]
            elif p in ("cond", "pal", "map", "msg", "hist", "win"):
                parts.setdefault(p, []).append(part["val"])
            elif p in ("cur", "need"):
                parts[p] = part["val"]
            elif p == "t":
                key = (part["k"], part.get("e"), part.get("w"), part["f"])
                elements.setdefault(key, []).append(
                    (part["offset"], part["text"], part["last"]))
            else:
                raise ChunkError("rid %s: unknown part %r" % (rid, p))

    rec = {
        "v": parts.get(("h", "v")),
        "ch": parts.get(("h", "ch")),
        "type": "obs",
        "d": rid,
        "seq": parts.get(("h", "seq")),
        "base": parts.get(("h", "base")),
        "s": parts.get("s", {}),
        "cond": parts.get("cond", []),
        "pal": parts.get("pal", []),
        "map": parts.get("map", []),
        "cur": parts.get("cur"),
        "msg": parts.get("msg", []),
        "hist": parts.get("hist", []),
        "windows": parts.get("win", []),
        "need": parts.get("need"),
    }

    for (kind, ev, wid, field), slices in elements.items():
        slices.sort(key=lambda s: s[0])
        want = 0
        for offset, text, last in slices:
            if offset != want:
                raise ChunkError(
                    "rid %s: text slice offset %d, expected %d"
                    % (rid, offset, want))
            want += len(text.encode("utf-8"))
        if not slices[-1][2]:
            raise ChunkError("rid %s: text slices do not end with last:true"
                             % rid)

        target = None
        if kind in ("msg", "hist"):
            for entry in rec[kind]:
                if entry.get("e") == ev:
                    target = entry
                    break
            key = "text"
        else:
            for entry in rec["windows"]:
                if entry.get("w") == wid:
                    target = entry
                    break
            key = "title"
        if target is None:
            raise ChunkError(
                "rid %s: text slices name missing %s element %r"
                % (rid, kind, ev if kind != "win" else wid))
        target[key] = "".join(s[1] for s in slices)
    return rec


def map_grid(rec):
    """Build the 80x21 cell grid and cursor from an obs record.

    Returns (grid, cur) where grid[y][x] is the palette cell tuple
    (char, color, style, frame) -- BLANK for a cell no triple covers -- and
    cur is the [x, y] cursor pair or None.  The human projection in
    ``format_obs`` and the live spectate renderer both decode the map through
    this, so they cannot drift.
    """
    grid = [[BLANK for _ in range(MAP_W)] for _ in range(MAP_H)]
    for triple in rec.get("map") or []:
        x, y, pid = triple
        cell = BLANK
        for entry in rec.get("pal") or []:
            if entry[0] == pid:
                cell = entry[1:5]
                break
        if 0 <= y < MAP_H and 0 <= x < MAP_W:
            grid[y][x] = cell
    return grid, rec.get("cur")
