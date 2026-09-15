#!/usr/bin/env python3
"""Canonical client formatter for the NetHack agent v1 wire.

This is a test-side debugging tool, not a production dependency.  It reads
newline-delimited JSON records (from a file or stdin), assembles chunk streams
back into logical records, and prints a canonical human-readable projection of
observations.  Standard library only.

Usage:
    python3 format_obs.py transcript.jsonl
    python3 format_obs.py --json transcript.jsonl   # assembled records
    python3 format_obs.py --raw transcript.jsonl    # one line per record
"""

import json
import sys

MAP_W, MAP_H = 80, 21
BLANK = [" ", "none", 0, "none"]  # char, color, style, frame


def assemble(lines):
    """Yield logical records, joining chunk streams by rid."""
    pending = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("type") != "chunk":
            yield rec
            continue
        rid = rec["rid"]
        slot = pending.setdefault(rid, {"n": 0, "parts": {}, "last": False})
        for part in rec["parts"]:
            p = part["p"]
            if p == "h":
                slot["parts"][("h", part["k"])] = part["val"]
            elif p == "s":
                slot["parts"].setdefault("s", {})[part["k"]] = part["val"]
            elif p in ("cond", "pal", "map", "msg", "hist", "win"):
                slot["parts"].setdefault(p, []).append(part["val"])
            else:
                slot["parts"][p] = part["val"]
        slot["n"] += 1
        if rec.get("last"):
            slot["last"] = True
            yield rebuild(slot)


def rebuild(slot):
    """Turn assembled parts back into a logical obs record."""
    parts = slot["parts"]
    rec = {
        "v": parts.get(("h", "v")),
        "ch": parts.get(("h", "ch")),
        "type": "obs",
        "d": parts.get(("h", "d")),
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
    return rec


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

    grid = [[BLANK for _ in range(MAP_W)] for _ in range(MAP_H)]
    for triple in rec.get("map") or []:
        x, y, pid = triple
        cell = BLANK
        for entry in rec.get("pal") or []:
            if entry[0] == pid:
                cell = entry[1:5]
                break
        if 0 <= y < MAP_H:
            grid[y][x] = cell

    cur = rec.get("cur")
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


def main(argv):
    mode = "text"
    args = []
    for a in argv[1:]:
        if a == "--json":
            mode = "json"
        elif a == "--raw":
            mode = "raw"
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

    for rec in assemble(fh):
        if mode == "json":
            print(json.dumps(rec, sort_keys=True))
        else:
            render(rec, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
