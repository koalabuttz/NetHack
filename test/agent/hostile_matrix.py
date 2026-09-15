#!/usr/bin/env python3
"""Hostile-startup matrix for the agent-only worker (the M1 exit gate).

This is a test-side harness, not a production component.  It stands in for the
trusted launcher so that a case can inject an argument vector, an environment,
or a handshake that the real launcher would never produce.  The trust model it
reproduces is the one the launcher implements:

  * the worker's stdin is /dev/null;
  * the worker's stdout and stderr go to a private diagnostic sink (so the
    "public channel" here is the private bootstrap socket, exactly as it is
    for the launcher);
  * the handshake is written on that socket before any player byte;
  * every public line must be schema-valid JSON from doc/agent-v1.schema.json.

Each case asserts one of two things:

  * expect-normal: the hostile input had no effect -- the public transcript and
    the private episode tree are identical to the run with no hostile input;
  * expect-reject: the worker refused before publishing, produced ZERO public
    bytes, and wrote the expected detail to the private diagnostic sink.

Every case also asserts that no public line carries diagnostic text.

Usage:
    python3 test/agent/hostile_matrix.py \
        --worker  <src/nethack (agent-only build)> \
        --runner  <src/nethack-agent> \
        --data    <staged immutable data root> \
        --sysconf <path to the trusted sysconf> \
        [--private-root <fresh dir>] [--impossible-worker <variant build>] \
        [--keep]
"""

import argparse
import json
import os
import select
import shutil
import socket
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
HS_PROFILE = "normal-ascii-color-v1"

RC_CONTENT = (
    "OPTIONS=windowtype:tty,name:Hostile,role:Wizard,playmode:debug\n"
    "BINDINGS=a:help\n"
    "SYMBOLS=S_foo:x\n"
)


def _field(text, size):
    return text.encode("utf-8")[: size - 1].ljust(size, b"\0")


def build_handshake(data_root, sysconf, writable_root, kind="full"):
    magic, version, profile = HS_MAGIC, HS_VERSION, HS_PROFILE
    if kind == "badmagic":
        magic = 0xDEADBEEF
    elif kind == "badprofile":
        profile = "unicode-tiles-v9"
    blob = _HS.pack(
        magic, version, HS_MODE_NEW, 0,
        _field(profile, 64), _field(data_root or "", 512),
        _field("", 512), _field(writable_root, 512), _field(sysconf or "", 512),
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


class Result(object):
    def __init__(self, public, exit_code, diag, listing):
        self.public = public
        self.exit_code = exit_code
        self.diag = diag
        self.listing = listing

    def lines(self):
        return [ln for ln in self.public.decode("utf-8", "replace").split("\n")
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
               handshake_kind="full", hostile_home=None, timeout=30):
    worker = worker or args.worker
    workdir = setup_workdir(args.private_root, label, args.data)
    diagpath = os.path.join(workdir, "diag", "worker.log")

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

    # The parent must not keep the worker's end of the socket open: if it does,
    # its own reads never see EOF when the worker exits (the peer is still
    # open).  The child's copy is closed at exec when it is not passed.
    child_sock.close()

    if handshake_kind != "none":
        blob = build_handshake(args.data, args.sysconf, workdir, handshake_kind)
        try:
            parent_sock.sendall(blob)
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
    listing = sorted(os.listdir(workdir))
    shutil.rmtree(workdir, ignore_errors=True)
    return Result(public, code, diag_text, listing)


def run_runner(args, label, env):
    priv = tempfile.mkdtemp(prefix=label + ".", dir=args.private_root)
    argv = [args.runner, "--worker", args.worker, "--private-root", priv]
    if args.data:
        argv += ["--data", args.data]
    if args.sysconf:
        argv += ["--sysconf", args.sysconf]
    proc = subprocess.run(argv, input=b"", stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=90)
    left = sorted(os.listdir(priv))
    shutil.rmtree(priv, ignore_errors=True)
    return Result(proc.stdout, proc.returncode,
                  proc.stderr.decode("utf-8", "replace"), left)


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
    ]
    return cases


def main(argv):
    ap = argparse.ArgumentParser(prog="hostile_matrix.py")
    ap.add_argument("--worker", required=True)
    ap.add_argument("--runner", required=True)
    ap.add_argument("--data", default=None)
    ap.add_argument("--sysconf", default=None)
    ap.add_argument("--private-root", default=None)
    ap.add_argument("--impossible-worker", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    schema = json.load(open(schema_check.SCHEMA))
    owned_root = args.private_root is None
    if owned_root:
        args.private_root = tempfile.mkdtemp(prefix="agent-matrix.")
    os.makedirs(args.private_root, exist_ok=True)

    failures = []
    rows = []
    baseline = None
    hostile_home_root = tempfile.mkdtemp(prefix="hostile-home.",
                                         dir=args.private_root)

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
            if kinds[:1] != ["hello"] or "obs" not in kinds:
                failures.append("%s: expected hello+obs, saw %s" % (name, kinds))
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
                    failures.append("%s: public transcript differs from baseline"
                                    % name)
                if res.listing != baseline.listing:
                    failures.append("%s: episode tree differs from baseline %s"
                                    % (name, sorted(set(res.listing)
                                                    ^ set(baseline.listing))))
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

        rows.append((name, "reject" if case["expect"] == "reject" else "normal",
                     "empty" if not res.public else "%d lines" % len(records),
                     str(res.exit_code),
                     "ok" if not any(name in f for f in failures) else "FAIL"))

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
    if kinds[:1] != ["hello"] or kinds[-1:] != ["closed"] or "obs" not in kinds:
        failures.append("runner-hostile-env: expected hello+obs+closed, saw %s"
                        % kinds)
    if res.exit_code != 0:
        failures.append("runner-hostile-env: runner exit %d" % res.exit_code)
    if res.listing:
        failures.append("runner-hostile-env: private root not cleaned: %s"
                        % res.listing)
    if open(rcpath).read() != RC_CONTENT:
        failures.append("runner-hostile-env: planted rc file was consumed")
    rows.append(("runner-hostile-env", "normal", "%d lines" % len(records),
                 str(res.exit_code),
                 "ok" if not any("runner-hostile-env" in f for f in failures)
                 else "FAIL"))

    # ---- test-only diagnostic injection: impossible() producer isolation and
    # the runtime policy predicates.
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
                     "probe symsetload=1", "probe wizardcmd=1",
                     "probe ordinary=0", "probe handlers=1"):
            if want not in res.diag:
                failures.append("impossible: private diag missing %r" % want)
        rows.append(("impossible", "reject", "empty", str(res.exit_code),
                     "ok" if not any("impossible:" in f for f in failures)
                     else "FAIL"))
    else:
        rows.append(("impossible", "skipped", "-", "-", "skipped"))

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
