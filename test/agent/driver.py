#!/usr/bin/env python3
"""Scripted driver for agent episodes (test-side, standard library only).

Not a production dependency.  It launches the trusted runner, reads the public
JSON-line stream, validates it against the frozen schema, and can play a real
game: applying full snapshots to a client model, answering character
selection through the native menus, issuing movement keys, answering
yn/line/direction/position requests, and requesting content pages.

Usage:
    python3 test/agent/driver.py episode --runner ... --worker ... --data ...
    python3 test/agent/driver.py play    --runner ... --worker ... --data ...
"""

import argparse
import json
import os
import select
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import schema_check  # noqa: E402  (test-side validator)

ESC = 27
KEY_H = 104
KEY_J = 106
KEY_K = 107
KEY_L = 108
KEY_HASH = 35
KEY_Y = 121
KEY_N = 110


def _fail(msg):
    print("driver: FAIL: %s" % msg)
    return 1


def validate_record(schema, rec, what):
    errs = []
    schema_check.validate(rec, schema, schema, "$", errs)
    return errs


# ------------------------------------------------------------------
# client model: applying an emitted snapshot reconstructs the presentation
# ------------------------------------------------------------------


class Client(object):
    """A client-side model rebuilt exactly from each full snapshot."""

    def reset(self):
        self.pal = {0: (" ", "none", 0, "none")}
        self.map = {}
        self.cur = None
        self.s = {}
        self.cond = []
        self.msg = []
        self.hist = []
        self.windows = {}
        self.need = None
        self.seq = 0
        self.descriptors = []

    def __init__(self):
        self.reset()

    def apply(self, rec):
        """Atomic application of a base:null full snapshot."""
        if rec.get("base") is not None:
            raise AssertionError("expected a full snapshot (base:null)")
        if "pal" not in rec or "map" not in rec:
            raise AssertionError("full snapshot missing pal/map")
        pal = {}
        for entry in rec["pal"]:
            pal[entry[0]] = (entry[1], entry[2], entry[3], entry[4])
        if pal.get(0) != (" ", "none", 0, "none"):
            raise AssertionError("palette entry 0 is not the blank tuple")
        cells = {}
        last = None
        for triple in rec["map"]:
            x, y, pid = triple
            if pid not in pal:
                raise AssertionError("map references undefined palette id %r"
                                     % pid)
            if pid == 0:
                raise AssertionError("blank cells must be omitted, not id 0")
            if not (1 <= x <= 79 and 0 <= y <= 20):
                raise AssertionError("map coordinate out of range: %r"
                                     % (triple,))
            order = (y, x)
            if last is not None and order < last:
                raise AssertionError("map is not row-major ordered")
            last = order
            cells[(x, y)] = pal[pid]
        # atomically replace
        self.pal, self.map = pal, cells
        self.cur = tuple(rec["cur"]) if rec["cur"] else None
        if self.cur is not None and not (1 <= self.cur[0] <= 79
                                         and 0 <= self.cur[1] <= 20):
            raise AssertionError("cursor out of range: %r" % (self.cur,))
        self.s = rec["s"]
        self.cond = rec["cond"]
        self.msg = rec["msg"]
        self.hist = rec["hist"]
        self.windows = {w["w"]: w for w in rec["windows"]}
        self.need = rec["need"]
        self.seq = rec["seq"]

    def time_value(self):
        t = self.s.get("time")
        if not t:
            return None
        try:
            return int(t["text"])
        except (TypeError, ValueError):
            return None


# ------------------------------------------------------------------
# the episode process
# ------------------------------------------------------------------


class Runner(object):
    def __init__(self, args):
        argv = [args.runner, "--worker", args.worker, "--private-root",
                args.private_root, "--data", args.data]
        if args.sysconf:
            argv += ["--sysconf", args.sysconf]
        if args.config:
            argv += ["--config", args.config]
        if args.deadline:
            argv += ["--deadline", str(args.deadline)]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.buf = b""

    def readline(self, timeout):
        deadline = time.time() + timeout
        while b"\n" not in self.buf:
            if time.time() > deadline:
                raise TimeoutError("no public line within %s s" % timeout)
            r, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if not r:
                continue
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line.decode("utf-8", "replace")
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def close_stdin(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def finish(self):
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        out = self.proc.stdout.read()
        err = self.proc.stderr.read()
        return self.proc.returncode, out, err


class Episode(object):
    """Protocol-level episode driver: applies snapshots and answers needs."""

    def __init__(self, runner, schema, timeout=30):
        self.runner = runner
        self.schema = schema
        self.timeout = timeout
        self.client = Client()
        self.records = []
        self.pages = {}
        self.seen_hello = 0
        self.seen_closed = 0
        self.invalids = []
        self.reconstructions = 0

    def validate(self, rec):
        errs = validate_record(self.schema, rec, rec.get("type", "?"))
        if errs:
            raise AssertionError("record %r is not schema-valid: %s"
                                 % (rec.get("type"), errs[:3]))
        self.records.append(rec)

    def read_record(self):
        line = self.runner.readline(self.timeout)
        if line is None:
            return None
        if not line.strip():
            return {}
        try:
            rec = json.loads(line)
        except ValueError:
            raise AssertionError("public line is not JSON: %r" % line[:120])
        self.validate(rec)
        return rec

    def fetch_pages(self, need):
        """Request every required page of the outstanding content."""
        pages = need.get("pages", 0)
        content = need.get("content")
        if not pages:
            return []
        for k in range(pages):
            self.runner.send({"v": 1, "type": "get_page", "id": need["id"],
                              "content": content, "page": k})
        want = pages
        rows = []
        while len(rows) < want:
            rec = self.read_record()
            if rec is None:
                raise AssertionError("transport closed while paging")
            if rec.get("type") == "page":
                if rec["content"] != content:
                    raise AssertionError("page for the wrong content")
                rows.append(rec["rows"])
            elif rec.get("type") in ("hello", "obs", "closed"):
                raise AssertionError("unexpected %r during paging"
                                     % rec.get("type"))
        return [r for page in rows for r in page]

    def menu_rows(self, need):
        rows = self.fetch_pages(need)
        return rows

    # -- policy hooks, overridden by the play policy -----------------

    def answer(self, need):
        # The plain episode leaves the request outstanding: its stdin is
        # already closed, so the worker observes EOF and closes generically.
        return

    def run(self):
        while True:
            rec = self.read_record()
            if rec is None:
                break
            if not rec:
                continue
            t = rec.get("type")
            if t == "hello":
                self.seen_hello += 1
            elif t == "obs":
                self.client.apply(rec)
                self.reconstructions += 1
                if rec["need"] is not None:
                    self.answer(rec["need"])
            elif t == "page":
                pass  # consumed by fetch_pages
            elif t == "chunk":
                raise AssertionError("chunked records are not expected here")
            elif t == "invalid":
                self.invalids.append(rec["code"])
            elif t == "closed":
                self.seen_closed += 1
                break
            else:
                raise AssertionError("unknown record type %r" % t)
        return 0


class PlayPolicy(Episode):
    """A policy that selects a character and plays a deterministic script."""

    def __init__(self, runner, schema, timeout=30,
                 moves=(KEY_L, KEY_L, KEY_H, 46, KEY_J, KEY_K, 46),
                 role_text="Barbarian", quit=True):
        Episode.__init__(self, runner, schema, timeout)
        self.moves = list(moves)
        self.role_text = role_text
        self.quit = quit
        self.answered_menus = 0
        self.role_chosen = None
        self.menu_titles = []
        self.move_index = 0
        self.gameplay_started = False
        self.time_first = None
        self.time_last = None
        self.keys_sent = []
        self.done = False
        self.quit_requested = False
        self.saw_extcmd = False
        self.saw_quit_yn = False
        self.saw_line = False
        self.did_annotate = False
        self.hash_count = 0

    def answer(self, need):
        kind = need["kind"]
        if kind == "yn":
            self.answer_yn(need)
        elif kind == "menu":
            self.answer_menu(need)
        elif kind in ("command", "key", "direction"):
            self.answer_key(need)
        elif kind == "ack":
            pages = need.get("pages", 0)
            if pages:
                self.fetch_pages(need)
            self.runner.send({"v": 1, "type": "act", "id": need["id"],
                              "action": {"ack": True}})
        elif kind in ("line", "extcmd"):
            if kind == "line":
                self.answer_line(need)
            else:
                self.answer_text(need)
        elif kind == "position":
            self.runner.send({"v": 1, "type": "act", "id": need["id"],
                              "action": {"key": ord(".")}})
        else:
            raise AssertionError("unexpected need kind %r" % kind)

    def answer_yn(self, need):
        prompt = need.get("prompt") or ""
        low = prompt.lower()
        if "shall i pick" in low:
            key = KEY_N
        elif "quit" in low or "save" in low:
            key = KEY_Y
            if "quit" in low:
                self.saw_quit_yn = True
        elif need.get("default") is not None:
            key = need["default"]
        elif need.get("choices"):
            key = ord(need["choices"][0])
        else:
            key = KEY_N
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": {"yn": key}})

    def answer_menu(self, need):
        rows = self.menu_rows(need)
        self.answered_menus += 1
        title = self.window_title(need.get("content"))
        self.menu_titles.append(title)
        selectable = [r for r in rows if r.get("selectable")]
        choice = None
        # prefer the deterministic non-tutorial path, then the confirmation
        for marker in ("No, just start play", "Yes; start game"):
            for r in selectable:
                if marker in r["text"]:
                    choice = r
                    break
            if choice is not None:
                break
        if choice is None and self.role_chosen is None:
            for r in selectable:
                if self.role_text.lower() in r["text"].lower():
                    choice = r
                    self.role_chosen = r["text"]
                    break
        if choice is None and selectable:
            choice = selectable[0]
        if choice is None:
            self.runner.send({"v": 1, "type": "act", "id": need["id"],
                              "action": {"cancel": True}})
            return
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": {"menu": need["menu"],
                                     "commit": [[choice["r"], -1]]}})

    def window_title(self, content):
        for w in self.client.windows.values():
            if w["content"] == content:
                return w["title"]
        return ""

    def answer_key(self, need):
        if not self.gameplay_started:
            self.gameplay_started = True
        if self.time_first is None:
            self.time_first = self.client.time_value()
        self.time_last = self.client.time_value()
        if self.move_index < len(self.moves):
            key = self.moves[self.move_index]
            self.move_index += 1
        elif self.quit and self.hash_count < 2:
            # first '#' reaches the native line prompt, second asks to quit
            key = KEY_HASH
            self.hash_count += 1
            self.quit_requested = True
        elif self.quit and self.quit_requested:
            key = KEY_N
        else:
            key = KEY_L
        self.keys_sent.append(key)
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": {"key": key}})

    def answer_text(self, need):
        self.saw_extcmd = True
        # first use the extended command to reach a native line prompt, then
        # ask to quit through the same ordinary path
        if not self.did_annotate:
            self.did_annotate = True
            text = "annotate"
        else:
            text = "quit" if self.quit else "wait"
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": {"text": text}})

    def answer_line(self, need):
        self.saw_line = True
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": {"text": "M2 line entry"}})


# ------------------------------------------------------------------
# subcommands
# ------------------------------------------------------------------


def cmd_episode(args):
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))

    runner = Runner(args)
    runner.close_stdin()
    ep = Episode(runner, schema, timeout=args.timeout)
    try:
        ep.run()
    except (AssertionError, TimeoutError) as exc:
        runner.finish()
        return _fail(str(exc))
    code, out, err = runner.finish()

    if ep.seen_hello != 1:
        return _fail("expected exactly one hello, saw %d" % ep.seen_hello)
    if ep.seen_closed != 1:
        return _fail("expected exactly one closed, saw %d" % ep.seen_closed)
    closed = [r for r in ep.records if r.get("type") == "closed"]
    if closed[0] != {"v": 1, "ch": "control", "type": "closed"}:
        return _fail("closed is not the exact bare object: %r" % (closed[0],))

    after = set(os.listdir(args.private_root))
    if after != before:
        return _fail("private root was not cleaned: leftovers %s"
                     % sorted(after - before))
    if code != 0:
        return _fail("runner exited %d" % code)

    kinds = [r.get("type") for r in ep.records]
    print("driver: episode ok: %d public lines %s" % (len(ep.records), kinds))
    return 0


def cmd_play(args):
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))

    runner = Runner(args)
    pol = PlayPolicy(runner, schema, timeout=args.timeout)
    try:
        pol.run()
    except (AssertionError, TimeoutError) as exc:
        runner.finish()
        return _fail(str(exc))
    code, out, err = runner.finish()

    if pol.seen_hello != 1:
        return _fail("expected exactly one hello, saw %d" % pol.seen_hello)
    if pol.invalids:
        return _fail("the episode emitted invalid records: %r" % pol.invalids)
    if not pol.gameplay_started:
        return _fail("character selection never completed")
    if pol.move_index < len(pol.moves):
        return _fail("only %d of %d moves were issued"
                     % (pol.move_index, len(pol.moves)))
    if pol.time_first is None or pol.time_last is None:
        return _fail("no displayed time was published")
    if pol.time_last <= pol.time_first:
        return _fail("displayed time did not advance (%s -> %s)"
                     % (pol.time_first, pol.time_last))
    if pol.quit and not pol.saw_extcmd:
        return _fail("the quit extended command was never requested")
    if pol.quit and not pol.saw_quit_yn:
        return _fail("the native quit confirmation was never requested")
    if not pol.saw_line:
        return _fail("no native line prompt was ever answered")

    if pol.seen_closed != 1:
        return _fail("expected exactly one closed, saw %d" % pol.seen_closed)

    after = set(os.listdir(args.private_root))
    if after != before:
        return _fail("private root was not cleaned: leftovers %s"
                     % sorted(after - before))

    kinds = [r.get("type") for r in pol.records]
    print("driver: play ok: %d records; role=%r; menus=%r; moves=%d; "
          "time %s->%s; keys=%r"
          % (len(pol.records), pol.role_chosen, pol.menu_titles,
             pol.move_index, pol.time_first, pol.time_last, pol.keys_sent))
    print("driver: record kinds: %s" % (kinds,))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="driver.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--runner", required=True)
        p.add_argument("--worker", required=True)
        p.add_argument("--data", required=True)
        p.add_argument("--sysconf", default=None)
        p.add_argument("--config", default=None)
        p.add_argument("--private-root", required=True)
        p.add_argument("--deadline", type=int, default=20)
        p.add_argument("--timeout", type=int, default=60)

    ep = sub.add_parser("episode")
    common(ep)
    ep.set_defaults(func=cmd_episode)

    pl = sub.add_parser("play")
    common(pl)
    pl.set_defaults(func=cmd_play)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
