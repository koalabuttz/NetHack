#!/usr/bin/env python3
"""Canonical client formatter for the NetHack agent v1 wire.

This is a test-side debugging tool, not a production dependency.  It reads
newline-delimited JSON records (from a file or stdin), assembles chunk streams
back into logical records, and prints a canonical human-readable projection of
observations.  Standard library only.

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
time (the shape a live proxy needs).  The batch ``assemble`` generator is a
thin wrapper over an unlimited instance, so the two cannot drift.  A live
consumer may set retained-state budgets; when one is reached the assembler
releases every byte it holds and raises :class:`AssemblerLimit`, which means
"stop rendering", never "the wire was wrong".

Usage:
    python3 format_obs.py transcript.jsonl        # human projection
    python3 format_obs.py --json transcript.jsonl # assembled records
    python3 format_obs.py --raw transcript.jsonl  # pass lines through
    python3 format_obs.py --selftest              # chunk-assembly vectors
"""

import json
import sys

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

    The optional ``max_*`` budgets exist for a live visualizer.  Exact
    validation of arbitrarily old retries cannot run forever with finite
    memory, so the selected policy is to disable the visualization visibly at
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
    cur is the [x, y] cursor pair or None.  The human projection below and the
    live spectate renderer both decode the map through this, so they cannot
    drift.
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


def render(rec, out):
    t = rec.get("type")
    if t == "hello":
        out.write("hello profile=%s policy=%s caps=%s size=%s limits=%s\n"
                  % (rec["profile"], rec["policy"], ",".join(rec["caps"]),
                     rec["size"], rec["limits"]))
        return
    if t == "closed":
        out.write("closed\n")
        return
    if t == "invalid":
        out.write("invalid d=%s code=%s\n" % (rec.get("d"), rec.get("code")))
        return
    if t == "page":
        out.write("page content=%s %d/%d rows=%d\n"
                  % (rec.get("content"), rec.get("page"), rec.get("pages"),
                     len(rec.get("rows", []))))
        return
    if t != "obs":
        out.write("? %s\n" % json.dumps(rec, sort_keys=True))
        return

    seq = rec.get("seq")
    base = rec.get("base")
    need = rec.get("need")
    out.write("obs d=%s seq=%s base=%s need=%s\n"
              % (rec.get("d"), seq, base,
                 json.dumps(need, sort_keys=True) if need else "-"))

    s = rec.get("s") or {}
    if s:
        parts = []
        for k in sorted(s):
            v = s[k]
            parts.append("%s=%s" % (k, v["text"] if v else "(deleted)"))
        out.write("  status: " + " ".join(parts) + "\n")

    cond = rec.get("cond") or []
    if cond:
        out.write("  cond: " + " ".join(c["text"] for c in cond) + "\n")

    grid, cur = map_grid(rec)
    for y in range(MAP_H):
        row = []
        for x in range(MAP_W):
            if x == 0:
                row.append(" ")
                continue
            if cur and [x, y] == cur:
                row.append("*")
            else:
                row.append(grid[y][x][0])
        out.write("  |" + "".join(row).rstrip() + "|\n")

    for m in rec.get("msg") or []:
        out.write("  msg[%s] %s\n" % (m["e"], m["text"]))
    for h in rec.get("hist") or []:
        out.write("  hist[%s] %s\n" % (h["e"], h["text"]))
    for w in rec.get("windows") or []:
        out.write("  window %s kind=%s title=%r content=%s pages=%s%s\n"
                  % (w["w"], w["kind"], w["title"], w["content"], w["pages"],
                     " mode=%s" % w["mode"] if "mode" in w else ""))


# ---------------------------------------------------------------- selftest

def _chunk(rid, i, last, parts, d=1):
    return {"v": 1, "ch": "control", "type": "chunk", "d": d, "rid": rid,
            "i": i, "last": last, "parts": parts}


HEAD = [{"p": "h", "k": "v", "val": 1},
        {"p": "h", "k": "ch", "val": "player"},
        {"p": "h", "k": "type", "val": "obs"},
        {"p": "h", "k": "seq", "val": 1},
        {"p": "h", "k": "base", "val": None}]

STREAM = [HEAD + [{"p": "msg", "val": {"e": 7, "text": "", "style": 0}}],
          [{"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 0,
            "text": "abc", "last": False},
           {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 3,
            "text": "def", "last": True},
           {"p": "need", "val": None}]]


def _run(records):
    return list(assemble(json.dumps(r) for r in records))


def _feed_all(records, asm=None):
    """Drive an incremental assembler over raw record dicts."""
    asm = asm or IncrementalAssembler()
    out = []
    for rec in records:
        out.extend(asm.feed(json.dumps(rec)))
    return out, asm


def _expect_reject(name, records):
    try:
        _run(records)
    except ChunkError:
        return 0
    print("SELFTEST FAIL: %s was accepted" % name)
    return 1


def selftest():
    bad = 0

    lines = [_chunk(1, i, i == len(STREAM) - 1, p)
             for i, p in enumerate(STREAM)]
    clean = _run(lines)
    if len(clean) != 1 or clean[0]["msg"][0]["text"] != "abcdef":
        print("SELFTEST FAIL: clean stream did not rebuild")
        bad += 1

    # every chunk retried, in order, still yields one exact record
    retried = []
    for ln in lines:
        retried.append(ln)
        retried.append(json.loads(json.dumps(ln)))
    got = _run(retried)
    if len(got) != 1 or got[0] != clean[0]:
        print("SELFTEST FAIL: retried stream did not deduplicate")
        bad += 1

    # the incremental API reproduces the batch output exactly
    inc, _asm = _feed_all(lines)
    if inc != clean:
        print("SELFTEST FAIL: incremental output differs from batch")
        bad += 1
    inc, _asm = _feed_all(retried)
    if inc != clean:
        print("SELFTEST FAIL: incremental retries did not deduplicate")
        bad += 1

    # a gap: chunk 1 never arrives
    bad += _expect_reject("a chunk gap",
                          [_chunk(1, 0, False, STREAM[0]),
                           _chunk(1, 2, True, STREAM[1])])

    # changed content under an existing (rid, i)
    changed = json.loads(json.dumps(lines[0]))
    changed["parts"] = HEAD + [{"p": "msg", "val": {"e": 7, "text": "x",
                                                    "style": 0}}]
    bad += _expect_reject("a changed repeat", [lines[0], changed, lines[1]])

    # a header part outside chunk 0
    bad += _expect_reject(
        "a header part in chunk 1",
        [_chunk(1, 0, False, HEAD),
         _chunk(1, 1, True, [{"p": "h", "k": "seq", "val": 1}])])

    # overlapping text offsets
    bad += _expect_reject(
        "overlapping text offsets",
        [_chunk(1, 0, True, HEAD + [
            {"p": "msg", "val": {"e": 7, "text": "", "style": 0}},
            {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 0,
             "text": "abc", "last": False},
            {"p": "t", "k": "msg", "e": 7, "f": "text", "offset": 2,
             "text": "def", "last": True}])])

    # a text slice naming a missing element
    bad += _expect_reject(
        "a slice with no target",
        [_chunk(1, 0, True, HEAD + [
            {"p": "t", "k": "msg", "e": 9, "f": "text", "offset": 0,
             "text": "abc", "last": True}])])

    # an incomplete stream is dropped, and finish() reports it
    inc = IncrementalAssembler()
    if inc.feed(json.dumps(lines[0])) != []:
        print("SELFTEST FAIL: an incomplete chunk produced a record")
        bad += 1
    notes = inc.finish()
    if not notes or "incomplete" not in notes[0]:
        print("SELFTEST FAIL: an incomplete stream was not reported")
        bad += 1

    # a budget hit raises, clears state, and leaves nothing behind
    limited = IncrementalAssembler(max_chunks=1)
    limited.feed(json.dumps(lines[0]))
    try:
        limited.feed(json.dumps(lines[1]))
    except AssemblerLimit:
        pass
    else:
        print("SELFTEST FAIL: the chunk budget was not enforced")
        bad += 1
    if limited.finish() or limited._streams or limited._chunks:
        print("SELFTEST FAIL: a budget hit did not clear assembler state")
        bad += 1

    # an overlong physical line is a budget hit, not a protocol error
    limited = IncrementalAssembler(max_line_bytes=4)
    try:
        limited.feed(json.dumps(lines[0]))
    except AssemblerLimit:
        pass
    else:
        print("SELFTEST FAIL: the line budget was not enforced")
        bad += 1

    if bad:
        print("format_obs selftest: %d failure(s)" % bad)
        return 1
    print("format_obs selftest: ok")
    return 0


def main(argv):
    mode = "text"
    args = []
    for a in argv[1:]:
        if a == "--json":
            mode = "json"
        elif a == "--raw":
            mode = "raw"
        elif a == "--selftest":
            return selftest()
        else:
            args.append(a)

    if args:
        fh = open(args[0], "r", encoding="utf-8")
    else:
        fh = sys.stdin

    if mode == "raw":
        for line in fh:
            if line.strip():
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
        return 0

    try:
        for rec in assemble(fh):
            if mode == "json":
                print(json.dumps(rec, sort_keys=True))
            else:
                render(rec, sys.stdout)
    except ChunkError as exc:
        sys.stderr.write("chunk stream rejected: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
