#!/usr/bin/env python3
"""Hostile-startup matrix for the agent-only worker (the M1 exit gate).

This is a test-side harness, not a production component.  It stands in for the
trusted launcher so that a case can inject an argument vector, an environment,
a sysconf, or a handshake that the real launcher would never produce.  The
trust model it reproduces is the one the launcher implements:

  * the worker's stdin is /dev/null;
  * the worker's stdout and stderr go to a private diagnostic sink (so the
    "public channel" here is the private bootstrap socket, exactly as it is
    for the launcher);
  * the handshake is written on that socket before any player byte;
  * every public line must be schema-valid JSON from doc/agent-v1.schema.json.

Each case asserts one of two things:

  * expect-normal: the hostile input had no effect -- the public record
    SEQUENCE, the exact public bytes, and the private episode tree (entry
    names AND contents AND modes) are identical to the run with no hostile
    input;
  * expect-reject: the worker refused before publishing, produced ZERO public
    bytes, exited privately (70), wrote the expected detail to the private
    diagnostic sink, and created nothing outside its episode root.

Every case also asserts that no public line carries diagnostic text.

Two run modes:

  * smoke (`make matrix`): the impossible-worker cases are reported as SKIPPED
    and do not fail the run;
  * full gate (`make matrix-full`, `--require-impossible`): the impossible
    worker is REQUIRED and its absence is a failure, so the runtime-policy and
    decision-callback contracts cannot be silently skipped.

Usage:
    python3 test/agent/hostile_matrix.py \
        --worker  <src/nethack (agent-only build)> \
        --runner  <src/nethack-agent> \
        --data    <staged immutable data root> \
        --sysconf <path to the trusted sysconf> \
        [--private-root <fresh dir>] [--impossible-worker <variant build>] \
        [--require-impossible] [--keep]
"""

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import schema_check  # noqa: E402  (test-side validator)

# struct agent_handshake (win/agent/agent_handshake.h), engine-free wire form
_HS = struct.Struct("<IIII64s512s512s512s512s")
HS_MAGIC = 0x4741484E
HS_VERSION = 1
HS_MODE_NEW = 0
HS_MODE_RESTORE = 1
HS_PROFILE = "normal-ascii-color-v1"

# Test-only launch modes, accepted ONLY by a worker built with
# AGENT_TEST_IMPOSSIBLE (win/agent/agent_handshake.h).  A production worker
# must refuse them, which one case below asserts.
HS_MODE_TEST_DISPLAY = 60
HS_MODE_TEST_SELECT = 61
HS_MODE_TEST_MSGMENU = 62
HS_MODE_TEST_EXEC = 63
HS_MODE_TEST_RIP = 64
HS_MODE_TEST_WIZSAVE = 65
HS_MODE_TEST_BADSAVE = 66
HS_MODE_TEST_WIZSAVEFILE = 67
HS_MODE_TEST_GATEPROBE = 68
TEST_MODE_KINDS = {
    "test-display": HS_MODE_TEST_DISPLAY,
    "test-select": HS_MODE_TEST_SELECT,
    "test-msgmenu": HS_MODE_TEST_MSGMENU,
    "test-exec": HS_MODE_TEST_EXEC,
    "test-rip": HS_MODE_TEST_RIP,
    "test-wizsave": HS_MODE_TEST_WIZSAVE,
    "test-badsave": HS_MODE_TEST_BADSAVE,
    "test-wizsavefile": HS_MODE_TEST_WIZSAVEFILE,
    "test-gateprobe": HS_MODE_TEST_GATEPROBE,
    "restore": HS_MODE_RESTORE,
}

RC_CONTENT = (
    "OPTIONS=windowtype:tty,name:Hostile,role:Wizard,playmode:debug\n"
    "BINDINGS=a:help\n"
    "SYMBOLS=S_foo:x\n"
)

# The reviewed minimal configuration, mirrored from test/agent/sysconf.  Every
# forbidden-directive case below is this body plus exactly one extra line.
CANONICAL_SYSCONF = (
    "WIZARDS=\n"
    "EXPLORERS=\n"
    "GENERICUSERS=agent\n"
    "MAXPLAYERS=1\n"
    "MAX_REROLL_RATE=0\n"
)

FORBIDDEN_DIAG = "forbidden sysconf directive in agent mode"

# The launcher's terminal-closure object (sys/unix/agent_runner.c).  It is the
# only public line a rejected launch may carry: the worker itself publishes
# nothing.
CLOSED_LINE = '{"v":1,"ch":"control","type":"closed"}'

# The game's lock file in the episode root: <letter>lock.<n>.  Its content is
# the live process id, so it is compared by type and mode only.
LOCKFILE_RE = re.compile(r"^[a-z]lock\.[0-9]+$")


def _field(text, size):
    return text.encode("utf-8")[: size - 1].ljust(size, b"\0")


def build_handshake(data_root, sysconf, writable_root, kind="full"):
    magic, version, profile = HS_MAGIC, HS_VERSION, HS_PROFILE
    mode = TEST_MODE_KINDS.get(kind, HS_MODE_NEW)
    if kind == "badmagic":
        magic = 0xDEADBEEF
    elif kind == "badprofile":
        profile = "unicode-tiles-v9"
    blob = _HS.pack(
        magic, version, mode, 0,
        _field(profile, 64), _field(data_root or "", 512),
        _field("", 512), _field(writable_root, 512),
        _field(sysconf or "", 512),
    )
    if kind == "short":
        return blob[:8]
    return blob


def clean_env(home):
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "USER": "agent",
        "LOGNAME": "agent",
        "HOME": home,
    }


def read_all(fd, timeout):
    out = bytearray()
    deadline = time.time() + timeout
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remain)
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return bytes(out)


def tree_signature(root):
    """Map every entry under root to (type, mode[, content hash]).

    Entry NAMES alone would let two trees differ in permissions or in file
    contents and still compare equal, so the full descriptor travels with each
    entry: symlinks by target, directories by mode, regular files by mode and
    a content digest.

    One entry is deliberately not compared by content: the lock file the game
    creates in the episode root.  It records the live process id, so its bytes
    differ between any two runs by construction; its type and mode are still
    compared.
    """
    sig = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                sig[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                sig[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif "/" not in rel and LOCKFILE_RE.match(rel):
                sig[rel] = ("lockfile", stat.S_IMODE(st.st_mode))
            else:
                mode = stat.S_IMODE(st.st_mode)
                try:
                    with open(p, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()[:16]
                except OSError:
                    # A file the engine deliberately made unreadable (mode 0,
                    # the "disallow parallel restores" marker a restore
                    # puts on the save it consumed) is compared by mode
                    # alone.
                    digest = None
                sig[rel] = ("file", mode, digest)
    return sig


def sig_diff(a, b):
    """Human-readable difference between two tree signatures."""
    keys = sorted(set(a) ^ set(b))
    changed = [k for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return "only-in-one=%s changed=%s" % (keys[:6], changed[:6])


class Result(object):
    def __init__(self, public, exit_code, diag, tree):
        self.public = public
        self.exit_code = exit_code
        self.diag = diag
        self.tree = tree

    def lines(self):
        return [ln for ln in self.public.decode("utf-8",
                                                "replace").split("\n")
                if ln.strip()]


def parse_records(res, schema, label, failures):
    records = []

    for i, ln in enumerate(res.lines()):
        try:
            rec = json.loads(ln)
        except ValueError:
            failures.append("%s: public line %d is not JSON: %r"
                            % (label, i, ln[:100]))
            continue
        errs = []
        schema_check.validate(rec, schema, schema, "$", errs)
        if errs:
            failures.append("%s: public line %d is not schema-valid: %s"
                            % (label, i, errs[:2]))
        records.append(rec)
        low = ln.lower()
        for marker in ("impossible", "panic", "agent:", "assert"):
            if marker in low:
                failures.append("%s: public line leaks %r" % (label, marker))
    return records


def setup_workdir(private_root, label, data_root):
    workdir = tempfile.mkdtemp(prefix=label + ".", dir=private_root)
    for sub in ("save", "diag", "home"):
        os.mkdir(os.path.join(workdir, sub), 0o700)
    open(os.path.join(workdir, "perm"), "w").close()
    if data_root:
        for name in ("nhdat", "license", "symbols"):
            src = os.path.join(data_root, name)
            if os.path.exists(src):
                os.symlink(src, os.path.join(workdir, name))
    return workdir


def run_worker(args, label, *, worker=None, extra_argv=(), env=None,
               handshake_kind="full", hostile_home=None, sysconf=None,
               timeout=30, seed_save=None, capture_save=None,
               shutdown_write=True):
    worker = worker or args.worker
    if sysconf is None:
        sysconf = args.sysconf
    workdir = setup_workdir(args.private_root, label, args.data)
    diagpath = os.path.join(workdir, "diag", "worker.log")

    # A restore episode finds the save where the engine looks for it: the
    # episode's own save directory.  The trusted controller (here) seeds it.
    if seed_save:
        for name in os.listdir(seed_save):
            src = os.path.join(seed_save, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(workdir, "save", name))

    if env is None:
        env = clean_env(os.path.join(workdir, "home"))

    parent_sock, child_sock = socket.socketpair()
    child_fd = child_sock.fileno()
    argv = [worker]
    pass_fds = ()
    if handshake_kind != "none":
        os.set_inheritable(child_fd, True)
        argv.append("--agent-fd=%d" % child_fd)
        pass_fds = (child_fd,)
    argv += list(extra_argv)

    with open(diagpath, "wb") as diag:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=diag,
                                stderr=diag, cwd=workdir, env=env,
                                pass_fds=pass_fds, close_fds=True)

    # The parent must not keep the worker's end of the socket open: if it
    # does, its own reads never see EOF when the worker exits (the peer
    # is still open).  The child's copy is closed at exec when it is not
    # passed.
    child_sock.close()

    if handshake_kind != "none":
        blob = build_handshake(args.data, sysconf, workdir, handshake_kind)
        try:
            parent_sock.sendall(blob)
            if shutdown_write:
                parent_sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    public = read_all(parent_sock.fileno(), timeout)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    parent_sock.close()

    with open(diagpath, "rb") as fh:
        diag_text = fh.read().decode("utf-8", "replace")
    # Capture an artifact the worker wrote into its own save directory before
    # the episode tree is torn down (test-only fixture production).
    if capture_save:
        os.makedirs(capture_save, exist_ok=True)
        sd = os.path.join(workdir, "save")
        if os.path.isdir(sd):
            for name in os.listdir(sd):
                src = os.path.join(sd, name)
                if os.path.isfile(src):
                    shutil.copyfile(src, os.path.join(capture_save, name))
    tree = tree_signature(workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    return Result(public, code, diag_text, tree)


def run_runner(args, label, env, extra=()):
    priv = tempfile.mkdtemp(prefix=label + ".", dir=args.private_root)
    argv = [args.runner, "--worker", args.worker, "--private-root", priv]
    if args.data:
        argv += ["--data", args.data]
    if args.sysconf:
        argv += ["--sysconf", args.sysconf]
    argv += list(extra)
    proc = subprocess.run(argv, input=b"", stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=90)
    tree = tree_signature(priv)
    shutil.rmtree(priv, ignore_errors=True)
    return Result(proc.stdout, proc.returncode,
                  proc.stderr.decode("utf-8", "replace"), tree)


def write_provenance(directory, worker, data, sysconf, artifact,
                     profile=HS_PROFILE, mode="new", uid=None):
    """Synthesise the controller-owned provenance record for an artifact the
    harness produced directly (the matrix stands in for the launcher).  The
    contract is the launcher's, in sys/unix/agent_runner.c: canonical
    key=value lines binding the artifact to the build, staged data, profile,
    producing mode and owner scope."""
    if uid is None:
        uid = os.getuid()

    def dig(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    lines = ["version=1", "mode=%s" % mode, "profile=%s" % profile,
             "owner-uid=%d" % uid, "worker-sha256=%s" % dig(worker)]
    for name in ("nhdat", "license", "symbols"):
        p = os.path.join(data, name) if data else None
        if p and os.path.exists(p):
            lines.append("data-%s-sha256=%s" % (name, dig(p)))
    if sysconf:
        lines.append("sysconf-sha256=%s" % dig(sysconf))
    lines.append("save-name=%s" % artifact)
    lines.append("save-sha256=%s" % dig(os.path.join(directory, artifact)))
    with open(os.path.join(directory, "provenance.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def worker_survivors(worker):
    """PIDs whose executable is the worker path (Linux /proc)."""
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


def run_runner_races(args, label, env, rounds=6):
    """Hostile transport races against the runner.

    Round A closes the runner's stdout the moment it is launched: the runner's
    writes to the consumer then fail with EPIPE, which must be an ordinary
    transport failure rather than a SIGPIPE that kills the runner before it
    reaps the worker and removes the episode tree.

    Round B pushes input at the runner while the worker starts and dies, so
    the write to a dead transport happens for real.

    Both rounds assert a controlled exit, no surviving worker process, and a
    removed episode tree.
    """
    problems = []
    for r in range(rounds):
        for kind in ("early-stdout-close", "input-while-dying"):
            priv = tempfile.mkdtemp(prefix="%s.%s.%d." % (label, kind, r),
                                    dir=args.private_root)
            argv = [args.runner, "--worker", args.worker,
                    "--private-root", priv]
            if args.data:
                argv += ["--data", args.data]
            if args.sysconf:
                argv += ["--sysconf", args.sysconf]
            why = "%s/%d" % (kind, r)
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env)
            if kind == "early-stdout-close":
                proc.stdout.close()
            else:
                os.set_blocking(proc.stdin.fileno(), False)
                proc.stdout.close()
                deadline = time.time() + 3.0
                while time.time() < deadline and proc.poll() is None:
                    try:
                        proc.stdin.write(b"x" * 65536)
                        proc.stdin.flush()
                    except OSError:
                        break
                    time.sleep(0.02)
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                problems.append("%s: runner did not terminate" % why)
                code = None
            if code is not None and code < 0:
                problems.append("%s: runner died of signal %d" % (why, -code))
            elif code not in (0, None):
                problems.append("%s: runner exited %d" % (why, code))
            tree = tree_signature(priv)
            if tree:
                problems.append("%s: episode tree not removed: %s"
                                % (why, sorted(tree)))
            shutil.rmtree(priv, ignore_errors=True)
    survivors = worker_survivors(args.worker)
    if survivors:
        problems.append("worker processes survived the races: %s" % survivors)
    return problems


def build_cases():
    cases = [
        dict(name="baseline", handshake="full", expect="normal"),
        dict(name="hostile-home-rc", handshake="full", expect="normal",
             hostile_home=True),
        dict(name="hostile-env", handshake="full", expect="normal",
             env_over=dict(NETHACKOPTIONS="name:Hostile,windowtype:tty",
                           HACKOPTIONS="!news,role:wizard",
                           TERM="xterm", NETHACKDIR="/nonexistent")),
    ]
    argv_cases = [
        ("argv-D", ["-D"], "debug-or-DECgraphics"),
        ("argv-debug", ["-debug"], "debug-or-DECgraphics"),
        ("argv-X", ["-X"], "explore"),
        ("argv-w-tty", ["-w", "tty"], "window-type"),
        ("argv-wagent", ["-wagent"], "window-type"),
        ("argv-windowtype", ["-windowtype=tty"], "window-type"),
        ("argv-DECgraphics", ["-DECgraphics"], "debug-or-DECgraphics"),
        ("argv-IBMgraphics", ["-IBMgraphics"], "IBMgraphics"),
        ("argv-dir", ["-d", "/tmp"], "playground-directory"),
        ("argv-nethackrc", ["-nethackrc", "/etc/passwd"], "rc-file"),
        ("argv-config", ["-config", "/etc/passwd"], "configuration"),
        ("argv-symset", ["-symset", "DEC"], "symbol-set"),
        ("argv-hook", ["-hook", "/bin/sh"], "hook"),
        ("argv-showpaths", ["--showpaths"], "startup-probe"),
        ("argv-version", ["--version"], "startup-probe"),
    ]
    for name, av, family in argv_cases:
        cases.append(dict(name=name, handshake="full", expect="reject",
                          extra_argv=av, diag_min=family))
    cases += [
        dict(name="no-locator", handshake="none", expect="reject",
             diag_min="without a bootstrap"),
        dict(name="bad-locator", handshake="none",
             extra_argv=["--agent-fd=99999"], expect="reject",
             diag_min="without a bootstrap"),
        dict(name="short-handshake", handshake="short", expect="reject",
             diag_min="short or failed bootstrap handshake"),
        dict(name="bad-magic", handshake="badmagic", expect="reject",
             diag_min="bad handshake magic"),
        dict(name="bad-profile", handshake="badprofile", expect="reject",
             diag_min="unsupported rendering profile"),
        # A test-only launch mode must be refused by a PRODUCTION worker: the
        # selector exists only in a matrix build.
        dict(name="prod-test-mode", handshake="test-display", expect="reject",
             diag_min="unsupported launch mode"),
    ]
    return cases


def forbidden_sysconf_bodies(forbidden_dir):
    """One sysconf per forbidden facility (plus two accepted names carrying
    a wrong value), each being the canonical body plus exactly ONE extra
    line."""
    p = os.path.join(forbidden_dir, "should-not-exist")
    entries = [
        ("hackdir", "HACKDIR=%s" % p),
        # The empty value is the exact trigger for the engine's
        # "cwd if HACKDIR is empty" fallback; it must be refused too.
        ("hackdir-empty", "HACKDIR="),
        ("bonesdir", "BONESDIR=%s" % p),
        ("datadir", "DATADIR=%s" % p),
        ("configdir", "CONFIGDIR=%s" % p),
        ("livelog", "LIVELOG=1"),
        ("dumplogfile", "DUMPLOGFILE=%s" % p),
        ("panictrace-gdb", "PANICTRACE_GDB=1"),
        ("gdbpath", "GDBPATH=/bin/sh"),
        ("greppath", "GREPPATH=/bin/sh"),
        ("crashreporturl", "CRASHREPORTURL=file://%s" % p),
        ("wizkit", "WIZKIT=%s" % p),
        ("shelldir", "SHELLDIR=%s" % p),
        ("msghandler", "MSGHANDLER=/bin/sh"),
        ("shellers", "SHELLERS=root"),
        ("recover", "RECOVER=/bin/sh"),
        ("portable-device-paths", "PORTABLE_DEVICE_PATHS=1"),
        ("pager", "PAGER=/bin/sh"),
        ("sounddir", "SOUNDDIR=%s" % p),
        ("mail", "MAIL=/var/mail/agent"),
        ("value-wizards", "WIZARDS=agent"),
        ("value-maxplayers", "MAXPLAYERS=2"),
    ]
    return {name: CANONICAL_SYSCONF + line + "\n" for name, line in entries}


def main(argv):
    ap = argparse.ArgumentParser(prog="hostile_matrix.py")
    ap.add_argument("--worker", required=True)
    ap.add_argument("--runner", required=True)
    ap.add_argument("--data", default=None)
    ap.add_argument("--sysconf", default=None)
    ap.add_argument("--private-root", default=None)
    ap.add_argument("--impossible-worker", default=None)
    ap.add_argument("--require-impossible", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    schema = json.load(open(schema_check.SCHEMA))
    # The worker validates that every launch path in the handshake is
    # absolute, so normalise the test-side paths before they become
    # handshake fields.
    for attr in ("worker", "runner", "data", "sysconf", "private_root",
                 "impossible_worker"):
        v = getattr(args, attr)
        if v:
            setattr(args, attr, os.path.abspath(v))
    owned_root = args.private_root is None
    if owned_root:
        args.private_root = tempfile.mkdtemp(prefix="agent-matrix.")
    os.makedirs(args.private_root, exist_ok=True)

    failures = []
    rows = []
    baseline = None
    hostile_home_root = tempfile.mkdtemp(prefix="hostile-home.",
                                         dir=args.private_root)

    # Every forbidden-directive case writes its sysconf here and the values
    # point inside it, so "nothing written outside the episode root" is a real
    # assertion rather than an assumption.
    forbidden_root = tempfile.mkdtemp(prefix="forbidden.",
                                      dir=args.private_root)
    forbidden_sysconfs = {}
    for name, body in forbidden_sysconf_bodies(forbidden_root).items():
        path = os.path.join(forbidden_root, "sysconf.%s" % name)
        with open(path, "w") as fh:
            fh.write(body)
        forbidden_sysconfs[name] = path

    for case in build_cases():
        name = case["name"]
        env = None
        if case.get("hostile_home"):
            home = os.path.join(hostile_home_root, name)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, ".nethackrc"), "w") as fh:
                fh.write(RC_CONTENT)
            env = clean_env(home)
        elif case.get("env_over"):
            home = os.path.join(hostile_home_root, name)
            os.makedirs(home, exist_ok=True)
            with open(os.path.join(home, ".nethackrc"), "w") as fh:
                fh.write(RC_CONTENT)
            env = clean_env(home)
            env.update(case["env_over"])

        res = run_worker(args, name, extra_argv=case.get("extra_argv", ()),
                         env=env, handshake_kind=case["handshake"],
                         timeout=args.timeout)
        records = parse_records(res, schema, name, failures)
        kinds = [r.get("type") for r in records]

        if case["expect"] == "normal":
            # EXACT record sequence, not just "hello present, some obs".
            if kinds != ["hello", "obs"]:
                failures.append("%s: expected exactly [hello, obs], saw %s"
                                % (name, kinds))
            if "native character selection" not in res.diag:
                failures.append("%s: did not reach native character selection"
                                % name)
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (name, res.exit_code))
            if baseline is None:
                baseline = res
            else:
                if res.public != baseline.public:
                    failures.append(
                        "%s: public transcript differs from baseline" % name)
                if res.tree != baseline.tree:
                    failures.append(
                        "%s: episode tree differs from baseline (%s)"
                        % (name, sig_diff(res.tree, baseline.tree)))
        else:
            if res.public:
                failures.append("%s: expected zero public bytes, saw %r"
                                % (name, res.public[:80]))
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (name, res.exit_code))
            if case["diag_min"] not in res.diag:
                failures.append("%s: private diag missing %r; got %r"
                                % (name, case["diag_min"], res.diag[:160]))

        rows.append(
            (name, "reject" if case["expect"] == "reject" else "normal",
             "empty" if not res.public else "%d lines" % len(records),
             str(res.exit_code),
             "ok" if not any(name in f for f in failures) else "FAIL"))

    # ---- forbidden sysconf directives, one at a time --------------------
    for name, path in sorted(forbidden_sysconfs.items()):
        label = "sysconf-" + name
        res = run_worker(args, label, handshake_kind="full", sysconf=path,
                         timeout=args.timeout)
        parse_records(res, schema, label, failures)
        if res.public:
            failures.append("%s: expected zero public bytes, saw %r"
                            % (label, res.public[:80]))
        if res.exit_code != 70:
            failures.append("%s: exit %d, expected private 70"
                            % (label, res.exit_code))
        if FORBIDDEN_DIAG not in res.diag:
            failures.append("%s: private diag missing %r; got %r"
                            % (label, FORBIDDEN_DIAG, res.diag[:160]))
        rows.append((label, "reject", "empty" if not res.public
                     else "%d lines" % len(res.lines()), str(res.exit_code),
                     "ok" if not any(label in f for f in failures)
                     else "FAIL"))

    # Nothing the forbidden directives named may have been created: a rejected
    # sysconf must not write (or exec) outside the episode root.
    leftovers = sorted(os.listdir(forbidden_root))
    leftovers = [x for x in leftovers if not x.startswith("sysconf.")]
    if leftovers:
        failures.append("forbidden sysconf wrote outside the episode root: %s"
                        % leftovers)
    if os.path.exists(os.path.join(forbidden_root, "should-not-exist")):
        failures.append("forbidden directive created its named path")
    rows.append(("sysconf-side-effects", "reject", "-", "-",
                 "ok" if not leftovers else "FAIL"))

    # ---- lifecycle: restore rejection before publication -----------------
    # A trusted controller owns the save files.  The harness produces one real
    # native save via the launcher, then seeds episodes with it and with
    # deliberately broken variants; every broken variant must be refused
    # before a single public byte.
    import driver as _driver
    save_art = os.path.join(args.private_root, "save-artifact")
    os.makedirs(save_art, exist_ok=True)
    real_save = []
    try:
        ns = argparse.Namespace(
            runner=args.runner, worker=args.worker, data=args.data,
            sysconf=args.sysconf, config=None,
            private_root=os.path.join(args.private_root, "save-episode"),
            deadline=30, timeout=args.timeout,
            save_out=save_art, mode="new", restore_in=None)
        os.makedirs(ns.private_root, exist_ok=True)
        _pol, _m, _t, artifacts = _driver._do_save(ns)
        real_save = list(artifacts)
    except Exception as exc:
        failures.append("restore-fixture: could not produce a native save: %s"
                        % exc)

    def _seed_variant(mutate):
        d = tempfile.mkdtemp(prefix="saveseed.", dir=args.private_root)
        for f in real_save:
            shutil.copyfile(os.path.join(save_art, f), os.path.join(d, f))
        if mutate:
            mutate(d)
        return d

    def _mutate_truncated(d):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                body = fh.read()
            with open(p, "wb") as fh:
                fh.write(body[: max(1, len(body) // 2)])

    def _mutate_foreign(d):
        for f in os.listdir(d):
            with open(os.path.join(d, f), "wb") as fh:
                fh.write(b"this file is not a nethack save\n" * 8)

    if real_save:
        restore_cases = [
            ("restore-ok", _seed_variant(None)),
            ("restore-truncated", _seed_variant(_mutate_truncated)),
            ("restore-foreign", _seed_variant(_mutate_foreign)),
        ]
    else:
        restore_cases = [("restore-missing", None)]

    for label, seeddir in restore_cases:
        if seeddir is None:
            # a restore launch with nothing to restore must fail closed
            seeddir = tempfile.mkdtemp(prefix="saveseed-empty.",
                                       dir=args.private_root)
        res = run_worker(args, label, handshake_kind="restore",
                         seed_save=seeddir, timeout=args.timeout)
        parse_records(res, schema, label, failures)
        if label == "restore-ok":
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (label, res.exit_code))
            recs = [json.loads(x) for x in res.lines()]
            kinds = [r.get("type") for r in recs]
            if kinds[:2] != ["hello", "obs"]:
                failures.append("%s: expected [hello, obs, ...], saw %s"
                                % (label, kinds[:3]))
            obs = [r for r in recs if r.get("type") == "obs"]
            if obs and obs[0].get("seq") != 1:
                failures.append("%s: first obs seq %r, expected 1"
                                % (label, obs[0].get("seq")))
            if obs and not obs[0].get("map"):
                failures.append("%s: restored map is empty" % label)
            if obs and not any(o.get("hist") for o in obs):
                failures.append("%s: no restored history tagged hist" % label)
            # restgamestate() carries the saved message history as hist and
            # exclusive of the live message window AT THE BOUNDARY THAT
            # PUBLISHES IT: the first boundary already carries the history, no
            # line of it may be tagged both hist and msg there, and the known
            # saved-game line must appear as history and never as a live msg.
            if obs:
                hist = [m.get("text") for o in obs for m in o.get("hist", [])]
                msg = [m.get("text") for o in obs for m in o.get("msg", [])]
                if not obs[0].get("hist"):
                    failures.append("%s: the first boundary carries no"
                                    " restored history" % label)
                both = sorted({m.get("text") for m in obs[0].get("hist", [])}
                              & {m.get("text")
                                 for m in obs[0].get("msg", [])})
                if both:
                    failures.append("%s: the first boundary tags restored"
                                    " lines as live messages: %r"
                                    % (label, both[:3]))
                if "Saving..." not in hist:
                    failures.append("%s: the known saved-game history line"
                                    " is not tagged hist (hist=%r)"
                                    % (label, hist[:5]))
                if "Saving..." in msg:
                    failures.append("%s: the known restored line was"
                                    " published as a live message" % label)
        else:
            if res.public:
                failures.append("%s: expected zero public bytes, saw %r"
                                % (label, res.public[:80]))
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (label, res.exit_code))
        rows.append((label, "restore" if label == "restore-ok" else "reject",
                     "empty" if not res.public
                     else "%d lines" % len(res.lines()),
                     str(res.exit_code),
                     "ok" if not any(label in f for f in failures)
                     else "FAIL"))

    # ---- M4 restore gate: a same-build save that fails INSIDE
    # restgamestate() and a same-build save carrying wizard mode.  Both
    # fixtures are produced by the impossible worker's BADSAVE / WIZSAVEFILE
    # launch modes, which restore the valid save above and re-write it through
    # the engine's OWN save path with one hostile field -- so the matrix
    # replays a REAL restore over a genuine same-build artifact rather than
    # mutating bytes at a build-specific offset or driving a seam.
    bad_save = tempfile.mkdtemp(prefix="badsave.", dir=args.private_root)
    wiz_save = tempfile.mkdtemp(prefix="wizsavefile.", dir=args.private_root)
    fixture_produced = {"badsave": [], "wizsavefile": []}
    if real_save and args.impossible_worker:
        for label, kind, dest in (
                ("badsave", "test-badsave", bad_save),
                ("wizsavefile", "test-wizsavefile", wiz_save)):
            res = run_worker(args, "mk-" + label,
                             worker=args.impossible_worker,
                             handshake_kind=kind, seed_save=save_art,
                             capture_save=dest, timeout=args.timeout)
            fixture_produced[label] = [
                f for f in os.listdir(dest)
                if os.path.isfile(os.path.join(dest, f))]
            if not fixture_produced[label]:
                failures.append("%s fixture: the helper wrote no save;"
                                " diag %r" % (label, res.diag[:160]))
            rows.append(("mk-" + label, "fixture",
                         "empty" if not res.public
                         else "%d lines" % len(res.lines()),
                         str(res.exit_code),
                         "ok" if fixture_produced[label] else "FAIL"))

        if fixture_produced["badsave"]:
            # High 1: restgamestate() returns false, so dorecover()'s failure
            # path is reached.  The restore must terminate privately with ZERO
            # public bytes and must NEVER wait for an agent decision (the
            # transport write side stays open here, so a blocking publish
            # would hang instead of silently succeeding).
            label = "restore-dead-hero"
            res = run_worker(args, label, handshake_kind="restore",
                             seed_save=bad_save, shutdown_write=False,
                             timeout=args.timeout)
            parse_records(res, schema, label, failures)
            if res.public:
                failures.append("%s: expected zero public bytes, saw %r"
                                % (label, res.public[:80]))
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (label, res.exit_code))
            rows.append((label, "reject", "empty" if not res.public
                         else "%d lines" % len(res.lines()),
                         str(res.exit_code),
                         "ok" if not any(label in f for f in failures)
                         else "FAIL"))

            # Launcher-level: give the same hostile artifact the controller's
            # provenance so it passes the per-save binding and reaches the
            # native restore, then assert the launcher publishes no player
            # byte and cleans the private root it created.
            label = "runner-restore-dead-hero"
            lbad = tempfile.mkdtemp(prefix="badsave-prov.",
                                    dir=args.private_root)
            for f in fixture_produced["badsave"]:
                shutil.copyfile(os.path.join(bad_save, f),
                                os.path.join(lbad, f))
            art = [f for f in os.listdir(lbad)
                   if os.path.isfile(os.path.join(lbad, f))][0]
            write_provenance(lbad, args.worker, args.data, args.sysconf, art)
            lhome = os.path.join(hostile_home_root, label)
            os.makedirs(lhome, exist_ok=True)
            res = run_runner(args, label, clean_env(lhome),
                             extra=["--mode", "restore",
                                    "--restore-in", lbad])
            parse_records(res, schema, label, failures)
            if res.lines() != [CLOSED_LINE]:
                failures.append("%s: expected only the launcher's terminal"
                                " closure, saw %r" % (label, res.lines()[:3]))
            if res.exit_code != 0:
                failures.append("%s: launcher exit %d, expected 0"
                                % (label, res.exit_code))
            if res.tree:
                failures.append("%s: private root not cleaned: %s"
                                % (label, sorted(res.tree)[:5]))
            rows.append((label, "reject", "empty" if not res.public
                         else "%d lines" % len(res.lines()),
                         str(res.exit_code),
                         "ok" if not any(label in f for f in failures)
                         else "FAIL"))

        if fixture_produced["wizsavefile"]:
            # Medium 4b: a valid save whose serialized flags carry debug mode
            # must be rejected by agent_validate_restored_flags() DURING A
            # REAL restore(), not only by the in-memory seam probe above.
            label = "restore-wizard-savefile"
            res = run_worker(args, label, handshake_kind="restore",
                             seed_save=wiz_save, timeout=args.timeout)
            parse_records(res, schema, label, failures)
            if res.public:
                failures.append("%s: expected zero public bytes, saw %r"
                                % (label, res.public[:80]))
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (label, res.exit_code))
            if "restored save enables wizard mode" not in res.diag:
                failures.append("%s: the restored-flags validator did not"
                                " reject the real save; diag %r"
                                % (label, res.diag[:160]))
            rows.append((label, "reject", "empty" if not res.public
                         else "%d lines" % len(res.lines()),
                         str(res.exit_code),
                         "ok" if not any(label in f for f in failures)
                         else "FAIL"))

        # Central gate enforcement, independent of the restore path: a commit
        # attempted while the publication gate is closed must fail closed with
        # a private fatal and zero public bytes.
        label = "gate-closed-commit"
        res = run_worker(args, label, worker=args.impossible_worker,
                         handshake_kind="test-gateprobe",
                         timeout=args.timeout)
        parse_records(res, schema, label, failures)
        if res.public:
            failures.append("%s: expected zero public bytes, saw %r"
                            % (label, res.public[:80]))
        if res.exit_code != 70:
            failures.append("%s: exit %d, expected private 70"
                            % (label, res.exit_code))
        if "commit while the publication gate is closed" not in res.diag:
            failures.append("%s: the central guard did not fire; diag %r"
                            % (label, res.diag[:160]))
        rows.append((label, "reject", "empty" if not res.public
                     else "%d lines" % len(res.lines()),
                     str(res.exit_code),
                     "ok" if not any(label in f for f in failures)
                     else "FAIL"))
    else:
        for label in ("mk-badsave", "mk-wizsavefile", "restore-dead-hero",
                      "runner-restore-dead-hero", "restore-wizard-savefile",
                      "gate-closed-commit"):
            rows.append((label, "skipped", "-", "-", "skipped"))
        if args.require_impossible:
            failures.append("--require-impossible: the M4 restore-gate cases "
                            "could not run (no artifact or impossible"
                            " worker)")

    # ---- runner-level case: hostile parent environment must not reach the
    # worker, and the worker's own HOME stays private.
    hostile = os.path.join(hostile_home_root, "runner")
    os.makedirs(hostile, exist_ok=True)
    rcpath = os.path.join(hostile, ".nethackrc")
    with open(rcpath, "w") as fh:
        fh.write(RC_CONTENT)
    renv = clean_env(hostile)
    renv.update(TERM="xterm", NETHACKOPTIONS="name:Hostile,windowtype:tty",
                HACKOPTIONS="!news,role:wizard", NETHACKDIR="/nonexistent")
    res = run_runner(args, "runner-hostile-env", renv)
    records = parse_records(res, schema, "runner-hostile-env", failures)
    kinds = [r.get("type") for r in records]
    if kinds != ["hello", "obs", "closed"]:
        failures.append("runner-hostile-env: expected exactly "
                        "[hello, obs, closed], saw %s" % kinds)
    if res.exit_code != 0:
        failures.append("runner-hostile-env: runner exit %d" % res.exit_code)
    if res.tree:
        failures.append("runner-hostile-env: private root not cleaned: %s"
                        % sorted(res.tree))
    if open(rcpath).read() != RC_CONTENT:
        failures.append("runner-hostile-env: planted rc file was consumed")
    if baseline is not None:
        # The runner transcript must be the worker's own transcript plus the
        # launcher's terminal-closure line.
        wlines = baseline.lines()
        rlines = res.lines()
        if rlines[:len(wlines)] != wlines:
            failures.append("runner-hostile-env: transcript differs from the "
                            "worker baseline")
    rows.append(("runner-hostile-env", "normal", "%d lines" % len(records),
                 str(res.exit_code),
                 "ok" if not any("runner-hostile-env" in f for f in failures)
                 else "FAIL"))

    # ---- transport races: EPIPE must be an ordinary failure, not a signal
    # and not a reason to skip the reap/cleanup.
    race_problems = run_runner_races(args, "runner-race", renv)
    for p in race_problems:
        failures.append("runner-race: %s" % p)
    rows.append(("runner-races", "normal", "-", "-",
                 "ok" if not race_problems else "FAIL"))

    # ---- test-only diagnostic injection: impossible() producer isolation,
    # the runtime policy predicates, and one unimplemented decision callback
    # per process.
    if args.impossible_worker:
        res = run_worker(args, "impossible", worker=args.impossible_worker,
                         handshake_kind="full", timeout=args.timeout)
        parse_records(res, schema, "impossible", failures)
        if res.public:
            failures.append("impossible: expected zero public bytes, saw %r"
                            % res.public[:80])
        if res.exit_code != 70:
            failures.append("impossible: exit %d, expected private 70"
                            % res.exit_code)
        for want in ("AGENT_TEST_IMPOSSIBLE", "probe denyset=1",
                     "probe bindkeys=1", "probe symset=1",
                     "probe symsetload=1", "probe parsemutate=1",
                     "probe wizardcmd=1", "probe ordinary=0",
                     "probe handlers=1",
                     "probe glypheq-map=1", "probe glypheq-frame=1",
                     "probe glypheq-menu=1", "probe glypheq-female=1",
                     "probe mapclear=1", "probe msgstore=1",
                     "probe msghistory=1", "probe dispfile=1",
                     "probe yn-one-request=1", "probe yn-case=1",
                     "probe yn-reset=1", "probe yn-escape=1",
                     "probe yn-hidden=1", "probe yn-count=1",
                     "probe yn-zero=1", "probe yndir-kind=1",
                     "probe ynkind-other=1",
                     "probe menu-multipage=1",
                     "probe menu-incomplete=1",
                     "probe menu-selector0-multipage=1",
                     "probe menu-public-rows=1",
                     "probe menu-duplicate=1",
                     "probe menu-heading-reject=1",
                     "probe menu-skipinvert=1",
                     "probe menu-forbidden=1",
                     "probe menu-count=1",
                     "probe menu-empty=1",
                     "probe menu-state-empty=1",
                     "probe menu-preselect=1",
                     "probe menu-cancel=1",
                     "probe menu-state-cancel=1",
                     "probe menu-repeat=1",
                     "probe menu-state-roundtrip=1",
                     "probe menu-stale=1"):
            if want not in res.diag:
                failures.append("impossible: private diag missing %r" % want)
        rows.append(("impossible", "reject", "empty", str(res.exit_code),
                     "ok" if not any("impossible:" in f for f in failures)
                     else "FAIL"))

        # One M2 decision boundary per worker process: each publishes exactly
        # one durable snapshot carrying its outstanding request and then
        # terminates privately on the silent transport rather than fabricating
        # a decision the agent never made.  The published request shape (need
        # kind and durable seq) is asserted, not just the record kinds.
        for kind, label, need_kind in (
                ("test-display", "display", "ack"),
                ("test-select", "select", "menu"),
                ("test-msgmenu", "msgmenu", "key"),
                ("test-rip", "rip", "ack")):
            label = "decision-" + label
            res = run_worker(args, label, worker=args.impossible_worker,
                             handshake_kind=kind, timeout=args.timeout)
            records = parse_records(res, schema, label, failures)
            kinds = [r.get("type") for r in records]
            if kinds != ["hello", "obs"]:
                failures.append("%s: expected exactly [hello, obs], saw %s"
                                % (label, kinds))
            obs = [r for r in records if r.get("type") == "obs"]
            if len(obs) == 1:
                need = obs[0].get("need")
                nk = need.get("kind") if isinstance(need, dict) else None
                if nk != need_kind:
                    failures.append(
                        "%s: obs need %r, expected kind %r"
                        % (label, need, need_kind))
                elif need.get("id") != 1:
                    failures.append("%s: obs need id %r, expected 1"
                                    % (label, need.get("id")))
                if kind == "test-rip" and need.get("pages", 0) < 1:
                    failures.append(
                        "%s: the endgame tombstone window published no pages"
                        " (the rip renders through the text window)"
                        % label)
                if obs[0].get("seq") != 1:
                    failures.append("%s: obs seq %r, expected 1"
                                    % (label, obs[0].get("seq")))
                if obs[0].get("base") is not None:
                    failures.append("%s: obs base %r, not a full snapshot"
                                    % (label, obs[0].get("base")))
            if res.exit_code != 70:
                failures.append("%s: exit %d, expected private 70"
                                % (label, res.exit_code))
            if "native character selection" not in res.diag:
                failures.append("%s: private diag missing the selection "
                                "context; got %r" % (label, res.diag[:160]))
            if "wrecon ok" not in res.diag:
                failures.append(
                    "%s: the presentation reconstruction self-check "
                    "did not run; got %r" % (label, res.diag[:160]))
            rows.append((label, "reject",
                         "empty" if not res.public
                         else "%d lines" % len(res.lines()),
                         str(res.exit_code),
                         "ok" if not any(label in f for f in failures)
                         else "FAIL"))

        # Descriptor inventory: the transport must be close-on-exec, so the
        # exec'd helper does not inherit the player-JSON channel.  The
        # handshake the worker already consumed proves it was usable pre-exec.
        res = run_worker(args, "transport-cloexec",
                         worker=args.impossible_worker,
                         handshake_kind="test-exec", timeout=args.timeout)
        parse_records(res, schema, "transport-cloexec", failures)
        if res.public:
            failures.append("transport-cloexec: expected zero public bytes, "
                            "saw %r" % res.public[:80])
        for want in ("probe cloexec=1", "helper:transport-absent"):
            if want not in res.diag:
                failures.append("transport-cloexec: private diag missing %r; "
                                "got %r" % (want, res.diag[:200]))
        if "helper:transport-present" in res.diag:
            failures.append("transport-cloexec: the exec'd helper inherited "
                            "the transport descriptor")
        rows.append(("transport-cloexec", "normal", "empty",
                     str(res.exit_code),
                     "ok" if not any("transport-cloexec" in f
                                     for f in failures) else "FAIL"))

        # Restored-flags validator (the M1-noted missing seam): a save whose
        # flags carry wizard mode must be rejected by agent_validate_restored_
        # flags() before any publication.  The probe drives the real seam with
        # the same in-memory input restore() would present it.
        res = run_worker(args, "restored-wizard-flags",
                         worker=args.impossible_worker,
                         handshake_kind="test-wizsave", timeout=args.timeout)
        parse_records(res, schema, "restored-wizard-flags", failures)
        if res.public:
            failures.append("restored-wizard-flags: expected zero public "
                            "bytes, saw %r" % res.public[:80])
        if res.exit_code != 70:
            failures.append("restored-wizard-flags: exit %d, expected "
                            "private 70" % res.exit_code)
        if "restored save enables wizard mode" not in res.diag:
            failures.append("restored-wizard-flags: the validator did not "
                            "reject the wizard flag; diag %r"
                            % res.diag[:160])
        rows.append(("restored-wizard-flags", "reject", "empty",
                     str(res.exit_code),
                     "ok" if not any("restored-wizard-flags" in f
                                     for f in failures) else "FAIL"))
    else:
        msg = ("impossible-worker cases SKIPPED: the impossible() producer "
               "isolation, the runtime-policy probes and the "
               "decision-callback fatals did NOT run")
        print("NOTE: " + msg)
        rows.append(("impossible", "skipped", "-", "-", "skipped"))
        rows.append(("decision-callbacks", "skipped", "-", "-", "skipped"))
        rows.append(("transport-cloexec", "skipped", "-", "-", "skipped"))
        if args.require_impossible:
            failures.append("--require-impossible: no --impossible-worker "
                            "was given, so the full gate cannot run")

    print("%-22s %-8s %-10s %-5s %s"
          % ("case", "expect", "public", "exit", "result"))
    for row in rows:
        print("%-22s %-8s %-10s %-5s %s" % row)

    if owned_root and not args.keep:
        shutil.rmtree(args.private_root, ignore_errors=True)
    if failures:
        print("\nhostile matrix FAILED (%d):" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("\nhostile matrix: all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
