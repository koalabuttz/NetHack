#!/usr/bin/env python3
"""Canonical client formatter for the NetHack agent v1 wire.

This is a test-side debugging tool, not a production dependency.  It reads
newline-delimited JSON records (from a file or stdin), assembles chunk streams
back into logical records, and prints a canonical human-readable projection of
observations.  Standard library only.

The strict chunk assembler and the snapshot decoder now live in
``tools/agent/codec.py`` (promoted so the autonomous controller and this tool
share one implementation); this module re-exports every name it always had so
existing importers -- ``driver.py``, ``spectate.py``, ``test_spectate.py`` --
keep working, and keeps the CLI and its selftest behavior unchanged.

Usage:
    python3 format_obs.py transcript.jsonl        # human projection
    python3 format_obs.py --json transcript.jsonl # assembled records
    python3 format_obs.py --raw transcript.jsonl  # pass lines through
    python3 format_obs.py --selftest              # chunk-assembly vectors
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.agent import codec as _codec  # noqa: E402

# Re-export the promoted decoder API unchanged.
MAP_W = _codec.MAP_W
MAP_H = _codec.MAP_H
BLANK = _codec.BLANK
CHUNK_OVERHEAD = _codec.CHUNK_OVERHEAD
STREAM_OVERHEAD = _codec.STREAM_OVERHEAD
ChunkError = _codec.ChunkError
AssemblerLimit = _codec.AssemblerLimit
canonical = _codec.canonical
IncrementalAssembler = _codec.IncrementalAssembler
assemble = _codec.assemble
rebuild = _codec.rebuild
map_grid = _codec.map_grid


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
