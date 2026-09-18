#!/usr/bin/env python3
"""Native ``m`` -> ``s`` prefix/cancellation fixture (the wave-5 gate).

This is the mandatory native fixture of
``doc/agent-reflex-upgrade-plan.md`` sections 5.4 and 8.4: the dangerous
forced-search transaction may only be *enabled* once the real engine proves,
through the real adapter, that

  1. the ``m`` (reqmenu) prefix consumes **no** game time -- the displayed
     turn does not advance and the engine immediately asks for the next
     command, so the *exact immediately following command need* is where a
     suffix command is delivered;
  2. a suffix ``s`` delivered to that following command need really executes
     (a forced search advances displayed time);
  3. native double-``m`` is the engine's own cancellation action -- the
     prefix is cleared with the ``Double m prefix, canceled.`` message and no
     game time is spent;
  4. after that native cancellation an ordinary command still works, so a
     controller that cancels its own armed prefix cannot leak it into a later
     action.

It drives the real agent-only worker through the trusted launcher, selects a
character through the native menus, and scripts a bounded in-game key
sequence.  Nothing here contacts the network.

Usage:
    python3 test/agent/native_prefix_probe.py \
        --runner  src/nethack-agent \
        --worker  src/nethack \
        --data    /tmp/nethack-agent-data \
        --sysconf /tmp/nethack-agent-data/sysconf \
        [--private-root DIR] [--timeout 30] [--evidence FILE]
"""

import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import driver  # noqa: E402  (test-side harness)
import schema_check  # noqa: E402

KEY_M = ord("m")      # reqmenu prefix (do_reqmenu)
KEY_S = ord("s")      # ordinary search (dosearch)

# The bounded in-game key script, one key per command need:
#   c1 'm'  -> c2 's' : prefix then suffix (probe A)
#   c3 'm'  -> c4 'm' : native double-m cancellation (probe B)
#   c5 's'            : ordinary command after cancellation (no-leak check)
PROBE_KEYS = [KEY_M, KEY_S, KEY_M, KEY_M, KEY_S]

CANCEL_TEXT = "double"          # "Double m prefix, canceled."
CANCEL_MARK = "canceled"


class PrefixProbePolicy(driver.PlayPolicy):
    """PlayPolicy that scripts a fixed prefix probe then quits normally."""

    def __init__(self, runner, schema, timeout=30):
        driver.PlayPolicy.__init__(self, runner, schema, timeout,
                                   moves=(), role_text="Barbarian",
                                   quit=True)
        self.probe = list(PROBE_KEYS)
        self.probe_index = 0
        self.command_needs = []   # per command need: idx, time, msg texts

    def answer_key(self, need):
        if not self.gameplay_started:
            self.gameplay_started = True
        self.command_needs.append({
            "idx": len(self.need_kinds) - 1,
            "time": self.client.time_value(),
            "msg": [m.get("text", "") for m in self.client.msg],
        })
        if self.probe_index < len(self.probe):
            key = self.probe[self.probe_index]
            self.probe_index += 1
            self.keys_sent.append(key)
            self.send_act(need, {"key": key})
            return
        driver.PlayPolicy.answer_key(self, need)


def _check(problems, ok, msg):
    if not ok:
        problems.append(msg)
    return ok


def run_probe(args):
    schema = json.load(open(schema_check.SCHEMA))
    temp_root = None
    if not args.private_root:
        temp_root = tempfile.mkdtemp(prefix="agent-native-probe.")
        args.private_root = temp_root
    os.makedirs(args.private_root, exist_ok=True)
    evidence = {"runner": args.runner, "worker": args.worker,
                "probe_keys": list(PROBE_KEYS), "checks": {}}
    pol = None
    err = b""
    code = 1
    try:
        runner = driver.Runner(args)
        pol = PrefixProbePolicy(runner, schema, timeout=args.timeout)
        try:
            pol.run()
        finally:
            code, _out, err = runner.finish()
    except (AssertionError, TimeoutError) as exc:
        evidence["exception"] = str(exc)
        print("native-prefix-probe: FAIL (harness): %s" % exc)
        return 1, evidence, [str(exc)], err
    finally:
        if temp_root is not None:
            import shutil
            shutil.rmtree(temp_root, ignore_errors=True)

    problems = []
    if pol is None:
        return 1, evidence, ["no episode state was produced"], err
    _check(problems, code == 0,
           "runner exited %d (expected a clean 0)" % code)
    _check(problems, pol.seen_closed == 1,
           "expected exactly one closed, saw %d" % pol.seen_closed)
    _check(problems, not pol.invalids,
           "the episode emitted invalid records: %r" % pol.invalids)
    if not _check(problems, len(pol.command_needs) >= 6,
                  "only %d command needs observed (need >= 6)"
                  % len(pol.command_needs)):
        return 1, evidence, problems, err

    c = pol.command_needs

    # Probe A: the prefix consumes no game time and the very next need is
    # the command need that carries the suffix.
    _check(problems, c[1]["idx"] == c[0]["idx"] + 1
           and pol.need_kinds[c[1]["idx"]] == "command",
           "the need following the m prefix is not the immediate command "
           "need (idx %d -> %d, kinds %r/%r)"
           % (c[0]["idx"], c[1]["idx"], pol.need_kinds[c[0]["idx"]],
              pol.need_kinds[c[1]["idx"]]))
    _check(problems, c[0]["time"] is not None
           and c[0]["time"] == c[1]["time"],
           "the m prefix advanced displayed time (%r -> %r)"
           % (c[0]["time"], c[1]["time"]))
    _check(problems, c[2]["time"] is not None and c[1]["time"] is not None
           and c[2]["time"] > c[1]["time"],
           "the suffix s on the following command need did not advance "
           "displayed time (%r -> %r)" % (c[1]["time"], c[2]["time"]))
    evidence["checks"]["prefix_no_time"] = (c[0]["time"] == c[1]["time"])
    evidence["checks"]["following_need_is_command"] = (
        pol.need_kinds[c[1]["idx"]] == "command")
    evidence["checks"]["suffix_advanced_time"] = (c[2]["time"] > c[1]["time"])

    # Probe B: native double-m cancels with its own message and no game time.
    _check(problems, c[3]["idx"] == c[2]["idx"] + 1
           and pol.need_kinds[c[3]["idx"]] == "command",
           "the need following the first m of double-m is not the command "
           "need (idx %d -> %d)" % (c[2]["idx"], c[3]["idx"]))
    _check(problems, c[2]["time"] == c[3]["time"],
           "the first m of double-m advanced displayed time (%r -> %r)"
           % (c[2]["time"], c[3]["time"]))
    _check(problems, c[3]["time"] == c[4]["time"],
           "native double-m advanced displayed time (%r -> %r)"
           % (c[3]["time"], c[4]["time"]))
    blob = " ".join(m.lower() for m in c[4]["msg"])
    _check(problems, CANCEL_TEXT in blob and CANCEL_MARK in blob,
           "native double-m cancellation message was not observed: %r"
           % (c[4]["msg"],))
    evidence["checks"]["double_m_cancel_message"] = c[4]["msg"]

    # No prefix leakage: after native cancellation an ordinary command runs.
    _check(problems, c[5]["time"] is not None and c[4]["time"] is not None
           and c[5]["time"] > c[4]["time"],
           "an ordinary command after cancellation did not advance displayed "
           "time (%r -> %r)" % (c[4]["time"], c[5]["time"]))
    evidence["checks"]["post_cancel_command_advanced_time"] = (
        c[5]["time"] > c[4]["time"])

    evidence["command_needs"] = c
    evidence["need_kinds"] = pol.need_kinds
    evidence["keys_sent"] = pol.keys_sent
    evidence["stderr_tail"] = err.decode("utf-8", "replace")[-400:]
    return (1 if problems else 0), evidence, problems, err


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", required=True)
    ap.add_argument("--worker", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--sysconf", default=None)
    ap.add_argument("--private-root", default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--deadline", type=float, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--evidence", default=None,
                    help="write the JSON evidence record here")
    args = ap.parse_args(argv)
    code, evidence, problems, err = run_probe(args)
    if args.evidence:
        with open(args.evidence, "w") as fh:
            json.dump(evidence, fh, indent=1, sort_keys=True)
    if problems:
        print("native-prefix-probe: FAIL")
        for p in problems:
            print("  - %s" % p)
        if err:
            print("  stderr tail: %s"
                  % err.decode("utf-8", "replace")[-300:]
                  .replace("\n", " | "))
        return 1
    print("native-prefix-probe: OK")
    print("  command needs: %s"
          % [(c["idx"], c["time"]) for c in evidence["command_needs"][:6]])
    print("  checks: %s"
          % {k: v for k, v in evidence["checks"].items()
             if k != "double_m_cancel_message"})
    print("  double-m message: %r"
          % (evidence["checks"]["double_m_cancel_message"],))
    return 0


if __name__ == "__main__":
    sys.exit(main())
