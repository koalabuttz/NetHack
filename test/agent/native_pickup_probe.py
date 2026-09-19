#!/usr/bin/env python3
"""Native pickup-shape probe (Phase 0 of the destination-commitment plan).

The engine-side probe required by ``doc/agent-destination-commitment-plan.md``
section 6 Phase 0 (and AC17/AC18).  It drives the **real** agent worker through
the trusted launcher, selects a character through the native menus, and issues
a real pickup command (``,``) on a bare floor square, so the engine's own
``dopickup`` path runs.  It then asserts the protocol shape the plan names:
the no-object case produces *no menu*, *no inventory change* and a
``command`` need carrying the "nothing here to pick up" message.

Disposition (Phase 0 investigation): the worker takes no fixed RNG seed and
offers no deterministic object-construction hook, so the level layout and its
floor objects differ on every run.  The only shape that is *deterministically*
constructible through the real adapter in this harness is ``no_object``; every
other shape needs a specific floor object or pile and is therefore
**manual-required** (AC18) -- the probe marks each of them explicitly
``unsupported`` rather than silently skipping it, and never claims a shape it
did not verify.  ``pickup_shapes.validate_partition`` guarantees the eight
shapes are dispositioned exactly once.

Usage:
    python3 test/agent/native_pickup_probe.py \
        --runner  src/nethack-agent --worker src/nethack \
        --data /tmp/nethack-agent-data --sysconf /tmp/nethack-agent-data/sysconf \
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
import pickup_shapes  # noqa: E402

KEY_PICKUP = ord(",")
NO_OBJECT_TEXT = "nothing here to pick up"

#: Orthogonal directions with their command keys, in a fixed order.
ORTH_STEPS = (((0, -1), ord("k")), ((0, 1), ord("j")),
              ((-1, 0), ord("h")), ((1, 0), ord("l")))


class PickupShapePolicy(driver.PlayPolicy):
    """Select a character, step onto bare floor, then issue a real pickup.

    The hero starts on the up-stairs, so a pickup there is not the no-object
    case.  The probe first steps onto an adjacent bare floor square (a '.' map
    cell -- a cell holding an item or a monster shows that glyph instead, so a
    '.' neighbour is provably empty) and then issues the pickup.
    """

    def __init__(self, runner, schema, timeout=30):
        driver.PlayPolicy.__init__(self, runner, schema, timeout,
                                   moves=(), role_text="Barbarian",
                                   quit=True)
        self.command_needs = []   # per gameplay command need: idx/time/msg
        self.phase = "seek"
        self.pickup_sent = False

    def _floor_target(self):
        """An adjacent '.' (bare floor) cell and the key that enters it."""
        cur = self.client.cur
        if cur is None:
            return None
        for (dx, dy), key in ORTH_STEPS:
            cell = self.client.map.get((cur[0] + dx, cur[1] + dy))
            if cell and cell[0] == ".":
                return (cur[0] + dx, cur[1] + dy), key
        return None

    def answer_key(self, need):
        if not self.gameplay_started:
            self.gameplay_started = True
        idx = len(self.need_kinds) - 1
        self.command_needs.append({
            "idx": idx,
            "time": self.client.time_value(),
            "msg": [m.get("text", "") for m in self.client.msg],
            "hero": self.client.cur,
        })
        if self.phase == "seek":
            found = self._floor_target()
            if found is not None:
                self.phase = "pickup"
                self.send_act(need, {"key": found[1]})
                self.keys_sent.append(found[1])
                return
            # no bare floor neighbour this tick: fall through to the ordinary
            # script (the assertion below then fails with a clear message)
        elif self.phase == "pickup":
            self.phase = "done"
            self.pickup_sent = True
            self.send_act(need, {"key": KEY_PICKUP})
            self.keys_sent.append(KEY_PICKUP)
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
        temp_root = tempfile.mkdtemp(prefix="agent-native-pickup.")
        args.private_root = temp_root
    os.makedirs(args.private_root, exist_ok=True)
    pickup_shapes.validate_partition()
    evidence = {"runner": args.runner, "worker": args.worker,
                "native_shapes": list(pickup_shapes.NATIVE_SHAPES),
                "manual_shapes": list(pickup_shapes.MANUAL_SHAPES),
                "observed": {}, "checks": {}}
    pol = None
    err = b""
    code = 1
    try:
        runner = driver.Runner(args)
        pol = PickupShapePolicy(runner, schema, timeout=args.timeout)
        try:
            pol.run()
        finally:
            code, _out, err = runner.finish()
    except (AssertionError, TimeoutError) as exc:
        evidence["exception"] = str(exc)
        print("native-pickup-probe: FAIL (harness): %s" % exc)
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
    if not _check(problems, len(pol.command_needs) >= 3,
                  "only %d gameplay command needs observed (need >= 3)"
                  % len(pol.command_needs)):
        return 1, evidence, problems, err

    c = pol.command_needs
    # need 0: gameplay start on the up-stairs; need 1: after the step onto bare
    # floor; need 2: after the pickup, carrying its outcome.
    _check(problems, pol.pickup_sent, "the probe never issued a pickup command")
    _check(problems, c[1]["hero"] != c[0]["hero"],
           "the hero did not step onto bare floor (%r -> %r)"
           % (c[0]["hero"], c[1]["hero"]))
    _check(problems, pol.need_kinds[c[2]["idx"]] == "command",
           "the need after a floor pickup is not a command need (kind %r)"
           % pol.need_kinds[c[2]["idx"]])
    blob = " ".join(m.lower() for m in c[2]["msg"])
    _check(problems, NO_OBJECT_TEXT in blob,
           "the no-object pickup did not report 'nothing here to pick up': %r"
           % (c[2]["msg"],))
    _check(problems, c[1]["time"] is not None and c[1]["time"] == c[2]["time"],
           "a no-object pickup advanced displayed time (%r -> %r)"
           % (c[1]["time"], c[2]["time"]))
    evidence["checks"]["no_object_message"] = c[2]["msg"]
    evidence["checks"]["no_object_no_time"] = (c[1]["time"] == c[2]["time"])
    evidence["observed"]["no_object"] = NO_OBJECT_TEXT in blob
    # Every other shape is explicitly unsupported (manual-required, AC18).
    for key in pickup_shapes.MANUAL_SHAPES:
        evidence["observed"][key] = "unsupported"
    evidence["command_needs"] = c
    evidence["need_kinds"] = pol.need_kinds
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
        print("native-pickup-probe: FAIL")
        for p in problems:
            print("  - %s" % p)
        if err:
            print("  stderr tail: %s"
                  % err.decode("utf-8", "replace")[-300:]
                  .replace("\n", " | "))
        return 1
    print("native-pickup-probe: OK")
    print("  native shape passed: %s" % ",".join(evidence["native_shapes"]))
    print("  manual-required (operator-gated): %s"
          % ",".join(evidence["manual_shapes"]))
    print("  no-object message: %r"
          % (evidence["checks"]["no_object_message"],))
    return 0


if __name__ == "__main__":
    sys.exit(main())
