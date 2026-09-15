#!/usr/bin/env python3
"""Scripted driver for agent episodes (test-side, standard library only).

Not a production dependency.  It launches the trusted runner, reads the public
JSON-line stream, validates it against the frozen schema, and checks the
lifecycle guarantees that Wave A must provide.

Usage:
    python3 test/agent/driver.py episode \
        --runner  <path to src/nethack-agent> \
        --worker  <path to src/nethack> \
        --data    <staged immutable data root> \
        --sysconf <path to the trusted sysconf> \
        --private-root <a fresh directory for per-episode roots>
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import schema_check  # noqa: E402  (test-side validator)


def _fail(msg):
    print("driver: FAIL: %s" % msg)
    return 1


def validate_record(schema, rec, what):
    errs = []
    schema_check.validate(rec, schema, schema, "$", errs)
    return errs


def cmd_episode(args):
    schema = json.load(open(schema_check.SCHEMA))
    os.makedirs(args.private_root, exist_ok=True)
    before = set(os.listdir(args.private_root))

    argv = [args.runner, "--worker", args.worker, "--private-root",
            args.private_root, "--data", args.data]
    if args.sysconf:
        argv += ["--sysconf", args.sysconf]
    if args.config:
        argv += ["--config", args.config]
    if args.deadline:
        argv += ["--deadline", str(args.deadline)]

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input=b"", timeout=args.timeout)
    text = out.decode("utf-8", "replace")

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return _fail("no public output; runner stderr was: %s"
                     % err.decode("utf-8", "replace")[:400])

    # Every public line must be a JSON object: nothing diagnostic may leak.
    records = []
    for i, ln in enumerate(lines):
        try:
            records.append(json.loads(ln))
        except ValueError:
            return _fail("public line %d is not JSON: %r" % (i, ln[:120]))

    hello = [r for r in records if r.get("type") == "hello"]
    closed = [r for r in records if r.get("type") == "closed"]
    if len(hello) != 1:
        return _fail("expected exactly one hello, saw %d" % len(hello))
    if len(closed) != 1:
        return _fail("expected exactly one closed, saw %d" % len(closed))

    errs = validate_record(schema, hello[0], "hello")
    if errs:
        return _fail("hello does not match the frozen schema: %s" % errs[:3])
    if closed[0] != {"v": 1, "ch": "control", "type": "closed"}:
        return _fail("closed is not the exact bare object: %r" % (closed[0],))

    # Whatever else appears must also be schema-valid player/control records.
    for r in records:
        errs = validate_record(schema, r, r.get("type", "?"))
        if errs:
            return _fail("record %r does not match the schema: %s"
                         % (r.get("type"), errs[:3]))

    # The worker must have been reaped and the private tree cleaned.
    after = set(os.listdir(args.private_root))
    if after != before:
        return _fail("private root was not cleaned: leftovers %s"
                     % sorted(after - before))

    if proc.returncode != 0:
        return _fail("runner exited %d" % proc.returncode)

    kinds = [r.get("type") for r in records]
    print("driver: episode ok: %d public lines %s" % (len(records), kinds))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="driver.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ep = sub.add_parser("episode")
    ep.add_argument("--runner", required=True)
    ep.add_argument("--worker", required=True)
    ep.add_argument("--data", required=True)
    ep.add_argument("--sysconf", default=None)
    ep.add_argument("--config", default=None)
    ep.add_argument("--private-root", required=True)
    ep.add_argument("--deadline", type=int, default=20)
    ep.add_argument("--timeout", type=int, default=60)
    ep.set_defaults(func=cmd_episode)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
