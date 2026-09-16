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
import format_obs  # noqa: E402  (test-side independent chunk/page decoder)

ESC = 27
KEY_H = 104
KEY_J = 106
KEY_K = 107
KEY_L = 108
KEY_HASH = 35
KEY_Y = 121
KEY_N = 110
KEY_S = 83  # native save command (number_pad off)

# The deterministic play script's expected character-selection menu titles and
# its request-kind transcript.  These pin the native interaction sequence the
# script exercises; they change only when the scripted play path changes.
EXPECTED_PLAY_MENUS = [
    "Pick a role or profession",
    "Pick a race or species",
    "Pick a gender or sex",
    "Pick an alignment or creed",
    "Is this ok? [ynq]",
    "Do you want a tutorial?",
]
EXPECTED_PLAY_KINDS = [
    "yn",                                   # "Shall I pick ...?"
    "menu", "menu", "menu", "menu", "menu",  # role, race, gender, align, ok
    "ack", "menu",                          # menu display, tutorial prompt
    "command", "command", "command", "command", "command", "command",
    "command", "command",                   # the seven movement keys + '.'
    "extcmd", "line", "command", "extcmd",  # quit path
    "yn", "yn", "yn", "yn", "yn",           # native confirmations
    "ack", "ack",                           # endgame: message window flush,
                                            # then the endgame text window
]

# The exact, RNG-independent part of the play transcript: everything through
# the quit extended command.  After that the end-of-game DISCLOSURE runs, and
# its yes/no confirmations depend on what the hero did: the vanquished and
# genocide queries are only offered when the scripted moves actually killed
# something, so the confirmation count is 5 or 6.  EXPECTED_PLAY_TAIL pins the
# invariant that remains regardless: a non-empty run of confirmation yn's
# followed by exactly the two endgame acknowledgements.
EXPECTED_PLAY_HEAD_LEN = 20  # through the quit "extcmd"
EXPECTED_PLAY_TAIL = ["ack", "ack"]

# The breadth scenario's pinned interaction core, at need-kind offsets 16..29:
# the shared selection prefix (8 kinds) and the movement keys + the first
# breadth key (8 commands) come first, then inventory (menu + item menu),
# farlook (menu + tips menu + position), help (menu + paged text ack),
# annotation (#extcmd + line), and the quit extcmd.  The remaining kinds are
# the native quit confirmations.
EXPECTED_BREADTH_KINDS = [
    "menu", "menu",                              # inventory, item menu
    "command",                                   # farlook
    "menu", "menu", "position",                  # look menu, tips, getpos
    "command",                                   # help
    "menu", "ack",                               # help topics, paged text
    "command", "extcmd", "line",                 # annotate
    "command", "extcmd",                         # quit
]

# The breadth scenario's full menu-title sequence: the shared character
# selection menus, then the inventory item menu, the farlook look/tip menus,
# and the help topic list.  The item-action menu names the hero's randomly
# chosen starting weapon, so that single title is matched by shape instead of
# being pinned verbatim; every other entry is exact.
def _item_action_title(title):
    return title.startswith("Do what with the ") and title.endswith("?")


EXPECTED_BREADTH_MENUS = EXPECTED_PLAY_MENUS + [
    " ",                              # inventory
    _item_action_title,               # item action menu (random weapon name)
    "What do you want to look at:",   # farlook
    " ",                              # farlook tip
    "Select one item:",               # help topic list
]

# Absolute need indices inside the breadth episode.  The shared selection
# prefix (8 kinds) and the movement keys plus the first breadth key (8
# commands) come first, so the breadth block starts at offset 8 + 8.
BREADTH_BASE = 16
# The help topic list and the paged text window it selects, scoped by position
# so the paging assertion cannot be satisfied by an earlier acknowledgement.
BREADTH_HELP_MENU = BREADTH_BASE + EXPECTED_BREADTH_KINDS.index("menu", 7)
BREADTH_HELP_ACK = BREADTH_BASE + EXPECTED_BREADTH_KINDS.index("ack")
# The help topic rows a breadth run must actually choose between; a run that
# silently falls back to the first selectable row has not exercised help.
HELP_TOPIC_MARKERS = ("Look up information", "List of game commands")


def titles_match(got, expected):
    """Compare a menu-title sequence against an expectation whose entries are
    either exact strings or predicates."""
    if len(got) != len(expected):
        return False
    for got_title, want in zip(got, expected):
        if callable(want):
            if not want(got_title):
                return False
        elif got_title != want:
            return False
    return True


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
        mode = getattr(args, "mode", None)
        if mode:
            argv += ["--mode", mode]
        save_out = getattr(args, "save_out", None)
        if save_out:
            argv += ["--save-out", save_out]
        restore_in = getattr(args, "restore_in", None)
        if restore_in:
            argv += ["--restore-in", restore_in]
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
        self.raw_lines = []
        self.pages = {}
        self.seen_hello = 0
        self.seen_closed = 0
        self.invalids = []
        self.reconstructions = 0
        self.obs_seen = 0
        self.acts_sent = 0
        self.need_kinds = []
        self.last_seq = 0

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
        self.raw_lines.append(line)
        try:
            rec = json.loads(line)
        except ValueError:
            raise AssertionError("public line is not JSON: %r" % line[:120])
        self.validate(rec)
        return rec

    def decode(self):
        """Independently assemble the ACTUAL emitted records (plain + chunked)
        through the shared strict decoder and return the logical stream.  This
        is the reconstruction authority; the live view is a cross-check."""
        return list(format_obs.assemble(self.raw_lines))

    def verify_reconstruction(self):
        """Rebuild the presentation from the assembled logical records and
        compare it with the live client view.  A disagreement means the wire
        projection is not lossless (or the decoder caught a stream defect)."""
        recon = Client()
        seen_obs = 0
        for rec in self.decode():
            if rec.get("type") == "obs":
                recon.apply(rec)
                seen_obs += 1
        if seen_obs != self.obs_seen:
            raise AssertionError(
                "decoder saw %d obs, live view saw %d" % (seen_obs,
                                                          self.obs_seen))
        if recon.seq != self.client.seq:
            raise AssertionError("decoded seq %r != live seq %r"
                                 % (recon.seq, self.client.seq))
        if recon.map != self.client.map:
            raise AssertionError("decoded map differs from the live view")
        if recon.cur != self.client.cur:
            raise AssertionError("decoded cursor differs from the live view")
        if recon.msg != self.client.msg:
            raise AssertionError("decoded messages differ from the live view")
        if recon.hist != self.client.hist:
            raise AssertionError("decoded history differs from the live view")
        if recon.s != self.client.s:
            raise AssertionError("decoded status differs from the live view")
        if recon.need != self.client.need:
            raise AssertionError("decoded request differs from the live view")

    def fetch_pages(self, need):
        """Request every page; verify index uniqueness and order."""
        pages = need.get("pages", 0)
        content = need.get("content")
        if not pages:
            return []
        for k in range(pages):
            self.runner.send({"v": 1, "type": "get_page", "id": need["id"],
                              "content": content, "page": k})
        got = {}
        order = []
        while len(order) < pages:
            rec = self.read_record()
            if rec is None:
                raise AssertionError("transport closed while paging")
            if rec.get("type") == "page":
                if rec["content"] != content:
                    raise AssertionError("page for the wrong content")
                if rec["pages"] != pages:
                    raise AssertionError(
                        "page declared %r pages, request declared %d"
                        % (rec["pages"], pages))
                idx = rec["page"]
                if idx in got:
                    raise AssertionError("duplicate page index %r" % idx)
                if idx != len(order):
                    raise AssertionError(
                        "page %r out of order (expected %d)"
                        % (idx, len(order)))
                got[idx] = rec["rows"]
                order.append(idx)
            elif rec.get("type") in ("hello", "obs", "closed"):
                raise AssertionError("unexpected %r during paging"
                                     % rec.get("type"))
        return [r for k in order for r in got[k]]

    def menu_rows(self, need):
        rows = self.fetch_pages(need)
        return rows

    def send_act(self, need, action):
        """Send one action answering need; every action is counted so the
        one-observation-per-action invariant can be checked."""
        self.acts_sent += 1
        self.runner.send({"v": 1, "type": "act", "id": need["id"],
                          "action": action})

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
                if rec["seq"] <= self.last_seq:
                    raise AssertionError(
                        "durable seq is not strictly increasing: %r after %r"
                        % (rec["seq"], self.last_seq))
                self.last_seq = rec["seq"]
                self.client.apply(rec)
                self.obs_seen += 1
                self.reconstructions += 1
                need = rec["need"]
                if need is not None:
                    self.need_kinds.append(need["kind"])
                    self.answer(need)
            elif t == "page":
                pass  # consumed by fetch_pages
            elif t == "chunk":
                # a chunked snapshot is legitimate; the decoder reassembles it
                pass
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
                 role_text="Barbarian", quit=True, breadth=()):
        Episode.__init__(self, runner, schema, timeout)
        self.moves = list(moves)
        self.role_text = role_text
        self.quit = quit
        # breadth commands issued between the movement script and the quit
        # path
        self.breadth = list(breadth)
        self.bi = 0
        self.saw_text_page = 0
        self.answered_menus = 0
        self.role_chosen = None
        self.menu_titles = []
        # per-need-index observations, so an assertion can be scoped to the
        # one request that is supposed to carry a property rather than to an
        # episode-global counter any acknowledgement could satisfy
        self.ack_info = {}
        self.menu_choice_marker = {}
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
            idx = len(self.need_kinds) - 1
            pages = need.get("pages", 0)
            rows = []
            if pages:
                rows = self.fetch_pages(need)
                self.saw_text_page += 1
            # record what THIS acknowledgement published, addressed by its own
            # need index and its own window descriptor
            self.ack_info[idx] = {
                "pages": pages,
                "rows": len(rows),
                "window_kind": self.window_kind(need.get("content")),
            }
            self.send_act(need, {"ack": True})
        elif kind in ("line", "extcmd"):
            if kind == "line":
                self.answer_line(need)
            else:
                self.answer_text(need)
        elif kind == "position":
            # a farlook/getpos request is cancelled with native Escape; a
            # breadth script never needs to select a map square
            self.send_act(need, {"key": 27})
        else:
            raise AssertionError("unexpected need kind %r" % kind)

    def answer_yn(self, need):
        prompt = need.get("prompt") or ""
        low = prompt.lower()
        if self.breadth and self.gameplay_started \
                and need.get("choices") is None \
                and need.get("default") is None:
            # an unrestricted native getobj prompt during play: Escape aborts
            # it cleanly and returns to the command prompt
            self.send_act(need, {"yn": 27})
            return
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
        self.send_act(need, {"yn": key})

    def answer_menu(self, need):
        rows = self.menu_rows(need)
        self.answered_menus += 1
        title = self.window_title(need.get("content"))
        self.menu_titles.append(title)
        selectable = [r for r in rows if r.get("selectable")]
        choice = None
        marker = None
        # breadth: from the inventory pick an item to inspect, and from the
        # help topic list choose a text topic (so a text window is paged)
        if self.breadth:
            for wanted in HELP_TOPIC_MARKERS:
                for r in selectable:
                    if wanted in r["text"]:
                        choice = r
                        marker = wanted
                        break
                if choice is not None:
                    break
        # prefer the deterministic non-tutorial path, then the confirmation
        if choice is None:
            for wanted in ("No, just start play", "Yes; start game"):
                for r in selectable:
                    if wanted in r["text"]:
                        choice = r
                        marker = wanted
                        break
                if choice is not None:
                    break
        if choice is None and self.role_chosen is None:
            for r in selectable:
                if self.role_text.lower() in r["text"].lower():
                    choice = r
                    marker = self.role_text
                    self.role_chosen = r["text"]
                    break
        # how this menu was answered, so a fallback to the first selectable
        # row cannot masquerade as a deliberate choice
        self.menu_choice_marker[len(self.need_kinds) - 1] = marker
        if choice is None and selectable:
            choice = selectable[0]
        if choice is None:
            self.send_act(need, {"cancel": True})
            return
        self.send_act(need, {"menu": need["menu"],
                             "commit": [[choice["r"], -1]]})

    def window_title(self, content):
        for w in self.client.windows.values():
            if w["content"] == content:
                return w["title"]
        return ""

    def window_kind(self, content):
        """The descriptor kind ("menu" or "text") of the window whose
        content id is content in the current view, or None when it is not
        published."""
        for w in self.client.windows.values():
            if w["content"] == content:
                return w["kind"]
        return None

    def answer_key(self, need):
        if not self.gameplay_started:
            self.gameplay_started = True
        if self.time_first is None:
            self.time_first = self.client.time_value()
        self.time_last = self.client.time_value()
        if self.move_index < len(self.moves):
            key = self.moves[self.move_index]
            self.move_index += 1
        elif self.bi < len(self.breadth):
            key = ord(self.breadth[self.bi])
            self.bi += 1
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
        self.send_act(need, {"key": key})

    def answer_text(self, need):
        self.saw_extcmd = True
        # first use the extended command to reach a native line prompt, then
        # ask to quit through the same ordinary path
        if not self.did_annotate:
            self.did_annotate = True
            text = "annotate"
        else:
            text = "quit" if self.quit else "wait"
        self.send_act(need, {"text": text})

    def answer_line(self, need):
        self.saw_line = True
        self.send_act(need, {"text": "M2 line entry"})


class SavePolicy(PlayPolicy):
    """Play the deterministic opening, then issue the NATIVE save command
    (the 'S' key) through the ordinary command path.  The engine asks
    "Really save?" through a yes/no boundary and then displays its "Saving..."
    message through the message window before exiting, so the save episode
    presents ordinary player output and closes generically; the launcher hands
    the produced save artifact to the caller-owned directory."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("quit", False)
        PlayPolicy.__init__(self, *args, **kwargs)
        self.save_requested = False

    def answer_key(self, need):
        if not self.gameplay_started:
            self.gameplay_started = True
        if self.time_first is None:
            self.time_first = self.client.time_value()
        self.time_last = self.client.time_value()
        if self.move_index < len(self.moves):
            key = self.moves[self.move_index]
            self.move_index += 1
        else:
            key = KEY_S
            self.save_requested = True
        self.keys_sent.append(key)
        self.send_act(need, {"key": key})


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
        ep.verify_reconstruction()
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
        # the independently assembled logical stream is the reconstruction
        # authority for the whole episode
        pol.verify_reconstruction()
    except (AssertionError, TimeoutError) as exc:
        runner.finish()
        return _fail(str(exc))
    code, out, err = runner.finish()

    if code != 0:
        return _fail("runner exited %d (expected a clean 0)" % code)
    if pol.seen_hello != 1:
        return _fail("expected exactly one hello, saw %d" % pol.seen_hello)
    if pol.invalids:
        return _fail("the episode emitted invalid records: %r" % pol.invalids)
    if not pol.gameplay_started:
        return _fail("character selection never completed")
    if pol.role_chosen != "a Barbarian":
        return _fail("unexpected role selection %r" % (pol.role_chosen,))
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

    # Exactly one durable response per accepted action: every action is
    # answered by one observation or by the terminal closure, and the episode
    # opens with exactly one un-prompted observation (character selection).
    if pol.obs_seen + pol.seen_closed != pol.acts_sent + 1:
        return _fail("expected one response per action: %d obs + %d closed "
                     "for %d acts" % (pol.obs_seen, pol.seen_closed,
                                      pol.acts_sent))

    # the request-kind transcript of the deterministic script: the RNG-free
    # head is exact, and the disclosure tail is a non-empty run of yn
    # confirmations followed by the two endgame acknowledgements.
    if pol.need_kinds[:EXPECTED_PLAY_HEAD_LEN] \
            != EXPECTED_PLAY_KINDS[:EXPECTED_PLAY_HEAD_LEN]:
        return _fail("request-kind transcript head differs:\n  got      %r\n"
                     "  expected %r"
                     % (pol.need_kinds[:EXPECTED_PLAY_HEAD_LEN],
                        EXPECTED_PLAY_KINDS[:EXPECTED_PLAY_HEAD_LEN]))
    tail = pol.need_kinds[EXPECTED_PLAY_HEAD_LEN:]
    if (len(tail) < len(EXPECTED_PLAY_TAIL) + 1
            or any(k != "yn" for k in tail[:-len(EXPECTED_PLAY_TAIL)])
            or tail[-len(EXPECTED_PLAY_TAIL):] != EXPECTED_PLAY_TAIL):
        return _fail("endgame disclosure tail differs (expected a run of yn"
                     " confirmations then %r):\n  got %r"
                     % (EXPECTED_PLAY_TAIL, tail))
    # the menu-title sequence of the character-selection menus
    if pol.menu_titles != EXPECTED_PLAY_MENUS:
        return _fail("menu-title sequence differs:\n  got      %r\n"
                     "  expected %r" % (pol.menu_titles, EXPECTED_PLAY_MENUS))

    if pol.seen_closed != 1:
        return _fail("expected exactly one closed, saw %d" % pol.seen_closed)

    # The endgame renders through the ordinary native text window: the message
    # window is flushed, then the closing epitaph block (and, on a death, the
    # tombstone art) is published as one text-window acknowledgement.  A run
    # that never reaches it has not exercised the endgame presentation.
    last_ack = pol.ack_info.get(len(pol.need_kinds) - 1)
    if not last_ack or last_ack.get("window_kind") != "text" \
            or last_ack.get("rows", 0) < 1:
        return _fail("the endgame text window was not published: %r"
                     % (last_ack,))

    after = set(os.listdir(args.private_root))
    if after != before:
        return _fail("private root was not cleaned: leftovers %s"
                     % sorted(after - before))

    kinds = [r.get("type") for r in pol.records]
    print("driver: play ok: %d records; role=%r; menus=%r; moves=%d; "
          "time %s->%s; keys=%r"
          % (len(pol.records), pol.role_chosen, pol.menu_titles,
             pol.move_index, pol.time_first, pol.time_last, pol.keys_sent))
    print("driver: request-kind transcript: %s" % (pol.need_kinds,))
    print("driver: record kinds: %s" % (kinds,))
    return 0


def cmd_breadth(args):
    """Interaction breadth on real engine menus: inventory, farlook, help
    (with text paging), annotation, and the native quit confirmations."""
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))

    runner = Runner(args)
    pol = PlayPolicy(runner, schema, timeout=args.timeout,
                     breadth=("i", "/", "?", "#"))
    try:
        pol.run()
        pol.verify_reconstruction()
    except (AssertionError, TimeoutError) as exc:
        runner.finish()
        return _fail(str(exc))
    code, out, err = runner.finish()

    if code != 0:
        return _fail("runner exited %d (expected a clean 0)" % code)
    if pol.seen_hello != 1:
        return _fail("expected exactly one hello, saw %d" % pol.seen_hello)
    if pol.seen_closed != 1:
        return _fail("expected exactly one closed, saw %d" % pol.seen_closed)
    if pol.invalids:
        return _fail("the episode emitted invalid records: %r" % pol.invalids)
    if not pol.gameplay_started:
        return _fail("character selection never completed")
    if pol.role_chosen != "a Barbarian":
        return _fail("unexpected role selection %r" % (pol.role_chosen,))
    if pol.bi < len(pol.breadth):
        return _fail("only %d of %d breadth commands were issued"
                     % (pol.bi, len(pol.breadth)))
    if not pol.saw_line:
        return _fail("no native line prompt was ever answered")
    if not pol.saw_extcmd:
        return _fail("the extended-command path was never exercised")
    if not pol.saw_quit_yn:
        return _fail("the native quit confirmation was never requested")

    # one durable response per accepted action
    if pol.obs_seen + pol.seen_closed != pol.acts_sent + 1:
        return _fail("expected one response per action: %d obs + %d closed "
                     "for %d acts" % (pol.obs_seen, pol.seen_closed,
                                      pol.acts_sent))
    # the deterministic selection prefix, then the movement+breadth commands,
    # then the pinned interaction core, then the native quit confirmations
    if pol.need_kinds[:8] != EXPECTED_PLAY_KINDS[:8]:
        return _fail("selection-kind prefix differs:\n  got      %r\n"
                     "  expected %r" % (pol.need_kinds[:8],
                                       EXPECTED_PLAY_KINDS[:8]))
    if pol.need_kinds[8:16] != ["command"] * 8:
        return _fail("movement/breadth command block differs: %r"
                     % (pol.need_kinds[8:16],))
    if pol.need_kinds[16:30] != EXPECTED_BREADTH_KINDS:
        return _fail("breadth-kind transcript differs:\n  got      %r\n"
                     "  expected %r" % (pol.need_kinds[16:30],
                                       EXPECTED_BREADTH_KINDS))
    tail = pol.need_kinds[30:]
    if (len(tail) < len(EXPECTED_PLAY_TAIL) + 1
            or any(k != "yn" for k in tail[:-len(EXPECTED_PLAY_TAIL)])
            or tail[-len(EXPECTED_PLAY_TAIL):] != EXPECTED_PLAY_TAIL):
        return _fail("quit-confirmation tail differs (expected a run of yn"
                     " confirmations then %r): %r"
                     % (EXPECTED_PLAY_TAIL, tail))

    # The help topic list must be answered out of a real help row, not by a
    # silent fallback to the first selectable row.
    help_marker = pol.menu_choice_marker.get(BREADTH_HELP_MENU)
    if help_marker not in HELP_TOPIC_MARKERS:
        return _fail("the help topic list was not answered from a help row"
                     " (marker %r)" % (help_marker,))
    # The acknowledgement at the help position must itself be the paged TEXT
    # window.  Scoping to that one request by index is the point: an earlier
    # paged acknowledgement elsewhere in the episode must not be able to
    # satisfy this, and the assertion must fail if the help topic's window is
    # empty.
    ack = pol.ack_info.get(BREADTH_HELP_ACK)
    if not ack:
        return _fail("no acknowledgement was published at the help position")
    if ack["pages"] < 1:
        return _fail("the help text window published no pages: %r" % (ack,))
    if ack["rows"] < 1:
        return _fail("the help text window returned no rows: %r" % (ack,))
    if ack["window_kind"] != "text":
        return _fail("the paged help window is not a text window: %r"
                     % (ack["window_kind"],))
    # The menu-title sequence of the whole breadth scenario.
    if not titles_match(pol.menu_titles, EXPECTED_BREADTH_MENUS):
        return _fail("breadth menu-title sequence differs:\n  got      %r\n"
                     "  expected %r" % (pol.menu_titles,
                                       EXPECTED_BREADTH_MENUS))

    after = set(os.listdir(args.private_root))
    if after != before:
        return _fail("private root was not cleaned: leftovers %s"
                     % sorted(after - before))

    print("driver: breadth ok: %d records; role=%r; menus=%r; paged-text=%d; "
          "keys=%r" % (len(pol.records), pol.role_chosen, pol.menu_titles,
                       pol.saw_text_page, pol.keys_sent))
    print("driver: request-kind transcript: %s" % (pol.need_kinds,))
    return 0


def _worker_survivors(worker):
    """PIDs currently running the worker executable (Linux /proc)."""
    hits = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return hits
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            exe = os.readlink("/proc/%s/exe" % pid)
        except OSError:
            continue
        if exe == worker:
            hits.append(pid)
    return hits


def _open_fds():
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _run_policy(args, policy, **kwargs):
    """Launch one episode with the given policy; return (policy, exit_code)."""
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    runner = Runner(args)
    pol = policy(runner, schema, timeout=args.timeout, **kwargs)
    try:
        pol.run()
        pol.verify_reconstruction()
    except (AssertionError, TimeoutError) as exc:
        runner.finish()
        raise AssertionError(str(exc))
    code, _out, _err = runner.finish()
    return pol, code


def _assert_closed(pol, label):
    if pol.seen_hello != 1:
        raise AssertionError("%s: expected one hello, saw %d"
                             % (label, pol.seen_hello))
    if pol.seen_closed != 1:
        raise AssertionError("%s: expected one closed, saw %d"
                             % (label, pol.seen_closed))
    if pol.invalids:
        raise AssertionError("%s: invalid records %r" % (label, pol.invalids))
    closed = [r for r in pol.records if r.get("type") == "closed"]
    if closed[0] != {"v": 1, "ch": "control", "type": "closed"}:
        raise AssertionError("%s: closed is not the bare object: %r"
                             % (label, closed[0]))


def _do_save(args):
    """Run the native-save episode; the launcher copies the save artifact into
    args.save_out.  Returns (policy, saved_map, saved_title)."""
    os.makedirs(args.save_out, exist_ok=True)
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))
    pol, code = _run_policy(args, SavePolicy)
    if code != 0:
        raise AssertionError("save episode: runner exited %d" % code)
    _assert_closed(pol, "save")
    if not pol.save_requested:
        raise AssertionError("save episode: the native save command was"
                             " never issued")
    if pol.saw_quit_yn is False and not any(
            "save" in (r.get("need") or {}).get("prompt", "").lower()
            for r in pol.records if r.get("type") == "obs"):
        raise AssertionError("save episode: no native save confirmation")
    after = set(os.listdir(args.private_root))
    if after != before:
        raise AssertionError("save episode: private root not cleaned: %s"
                             % sorted(after - before))
    artifacts = [f for f in os.listdir(args.save_out)
                 if os.path.isfile(os.path.join(args.save_out, f))]
    if not artifacts:
        raise AssertionError("save episode: no native save artifact produced")
    return pol, dict(pol.client.map), pol.client.s.get("title"), artifacts


def _do_restore(args, saved_map, saved_title):
    """Restore the save in a fresh worker and continue play."""
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))
    pol, code = _run_policy(args, PlayPolicy, moves=(KEY_L,), quit=True)
    if code != 0:
        raise AssertionError("restore episode: runner exited %d" % code)
    _assert_closed(pol, "restore")
    obs = [r for r in pol.records if r.get("type") == "obs"]
    if not obs:
        raise AssertionError("restore episode: no observation was published")
    first = obs[0]
    if first.get("base") is not None:
        raise AssertionError("restore episode: the first observation is not a"
                             " full snapshot")
    if first.get("seq") != 1:
        raise AssertionError("restore episode: first seq is %r, expected a"
                             " fresh namespace at 1" % (first.get("seq"),))
    if not any(o.get("hist") for o in obs):
        raise AssertionError("restore episode: no restored history was tagged"
                             " hist")
    if not first.get("map"):
        raise AssertionError("restore episode: the restored map is empty")
    # same game essence: the restored first snapshot reproduces the saved map
    # and status title.
    if saved_map and first_map(pol) != saved_map:
        raise AssertionError("restore episode: restored map differs from the"
                             " saved map")
    if saved_title and first.get("s", {}).get("title") != saved_title:
        raise AssertionError("restore episode: restored status title %r != %r"
                             % (first.get("s", {}).get("title"), saved_title))
    # continued play: one move after the restore advances the displayed time.
    if pol.time_first is None or pol.time_last is None \
            or pol.time_last <= pol.time_first:
        raise AssertionError("restore episode: displayed time did not advance"
                             " (%s -> %s)" % (pol.time_first, pol.time_last))
    after = set(os.listdir(args.private_root))
    if after != before:
        raise AssertionError("restore episode: private root not cleaned: %s"
                             % sorted(after - before))
    return pol


def first_map(pol):
    for r in pol.records:
        if r.get("type") == "obs":
            pal = {e[0]: (e[1], e[2], e[3], e[4]) for e in r["pal"]}
            return {(t[0], t[1]): pal[t[2]] for t in r["map"]}
    return {}


def cmd_save(args):
    try:
        pol, saved_map, title, artifacts = _do_save(args)
    except AssertionError as exc:
        return _fail(str(exc))
    print("driver: save ok: %d records; artifact=%r; title=%r"
          % (len(pol.records), artifacts, (title or {}).get("text")))
    return 0


def cmd_restore(args):
    try:
        pol = _do_restore(args, None, None)
    except AssertionError as exc:
        return _fail(str(exc))
    print("driver: restore ok: %d records; time %s->%s"
          % (len(pol.records), pol.time_first, pol.time_last))
    return 0


def cmd_lifecycle(args):
    """Native save -> trusted artifact transfer -> restore into a fresh worker
    -> assert same essence and continued play."""
    savepriv = os.path.join(args.root, "save-episode")
    restpriv = os.path.join(args.root, "restore-episode")
    savedir = os.path.join(args.root, "save-artifact")
    sa = argparse.Namespace(**vars(args))
    sa.private_root, sa.mode, sa.save_out, sa.restore_in = \
        savepriv, "new", savedir, None
    ra = argparse.Namespace(**vars(args))
    ra.private_root, ra.mode, ra.save_out, ra.restore_in = \
        restpriv, "restore", None, savedir
    try:
        spol, saved_map, title, artifacts = _do_save(sa)
        rpol = _do_restore(ra, saved_map, title)
    except AssertionError as exc:
        return _fail(str(exc))
    print("driver: lifecycle ok: save=%d records artifact=%r; restore=%d"
          " records time %s->%s"
          % (len(spol.records), artifacts, len(rpol.records),
             rpol.time_first, rpol.time_last))
    return 0


class AbortPolicy(PlayPolicy):
    """Answer the shared selection prefix, then stop answering at a chosen
    request (leaving it outstanding) so the worker observes transport EOF and
    closes generically.  Models a trusted reset at a varying point."""

    def __init__(self, *args, **kwargs):
        self.stop_at = kwargs.pop("stop_at", 0)
        PlayPolicy.__init__(self, *args, **kwargs)
        self.answered = 0

    def answer(self, need):
        if self.answered >= self.stop_at:
            self.runner.close_stdin()
            return
        self.answered += 1
        PlayPolicy.answer(self, need)


def cmd_plateau(args):
    """>=100 sequential episodes, each reset at a varying point."""
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    points = [0, 8, 14, 21]   # first prompt, mid-move, mid-menu, quit confirm
    n = args.count
    base_workers = set(_worker_survivors(args.worker))
    base_fds = _open_fds()
    leftovers = []
    first_seqs = []
    for i in range(n):
        stop_at = points[i % len(points)]
        priv = os.path.join(args.private_root, "ep%d" % i)
        os.makedirs(priv, exist_ok=True)
        a = argparse.Namespace(**vars(args))
        a.private_root = priv
        runner = Runner(a)
        pol = AbortPolicy(runner, schema, timeout=args.timeout,
                          stop_at=stop_at)
        try:
            pol.run()
            pol.verify_reconstruction()
        except (AssertionError, TimeoutError) as exc:
            runner.finish()
            return _fail("plateau episode %d (stop_at=%d): %s"
                         % (i, stop_at, exc))
        code, _o, _e = runner.finish()
        if code != 0:
            return _fail("plateau episode %d: runner exited %d"
                         % (i, code))
        if pol.seen_hello != 1 or pol.seen_closed != 1:
            return _fail("plateau episode %d: hello=%d closed=%d"
                         % (i, pol.seen_hello, pol.seen_closed))
        obs = [r for r in pol.records if r.get("type") == "obs"]
        if obs and obs[0].get("seq") != 1:
            return _fail("plateau episode %d: first seq %r, not a fresh"
                         " namespace" % (i, obs[0].get("seq")))
        first_seqs.append(obs[0].get("seq") if obs else None)
        if os.listdir(priv):
            leftovers.append(priv)
        os.rmdir(priv)
    if leftovers:
        return _fail("plateau: private roots not cleaned: %s" % leftovers[:5])
    survivors = set(_worker_survivors(args.worker)) - base_workers
    if survivors:
        return _fail("plateau: worker processes survived: %s"
                     % sorted(survivors))
    end_fds = _open_fds()
    if base_fds >= 0 and end_fds > base_fds + 4:
        return _fail("plateau: descriptor count grew %d -> %d"
                     % (base_fds, end_fds))
    os.rmdir(args.private_root)
    print("driver: plateau ok: %d episodes; every episode a fresh namespace"
          " (first seq=%r); fds %d->%d; no survivors"
          % (n, sorted(set(first_seqs)), base_fds, end_fds))
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

    br = sub.add_parser("breadth")
    common(br)
    br.set_defaults(func=cmd_breadth)

    sv = sub.add_parser("save")
    common(sv)
    sv.add_argument("--save-out", required=True)
    sv.set_defaults(func=cmd_save)

    rs = sub.add_parser("restore")
    common(rs)
    rs.add_argument("--restore-in", required=True)
    rs.set_defaults(func=cmd_restore)

    lc = sub.add_parser("lifecycle")
    common(lc)
    lc.add_argument("--root", required=True)
    lc.set_defaults(func=cmd_lifecycle)

    pl = sub.add_parser("plateau")
    common(pl)
    pl.add_argument("--count", type=int, default=100)
    pl.set_defaults(func=cmd_plateau)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
