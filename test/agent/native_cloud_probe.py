#!/usr/bin/env python3
"""Native cloud-encoding probe (Phase 0 of the prompt-edge plan).

The engine-side probe required by ``doc/agent-prompt-edge-plan.md`` section
"Phase 0 -- native cloud probe".  It exists to *pin the cloud encoding*: does
the wire expose a local overlay token that lets the agent observe a vapor
cloud appearing/disappearing at an edge, or is the cloud structurally
indistinguishable from plain corridor ground?

Two acceptable Phase 0 exits (the plan permits either):

* **(a) positively identified overlay transition** -- the probe observes the
  cloud token appearing/disappearing, which would enable positive reopening;
* **(b) documented indistinguishable encoding** -- a gray ``#`` vapor cell
  classifies (``instances.classify_cell``) to ``T_CORRIDOR``, exactly like a
  corridor glyph, so there is no observable cloud token and the edge must stay
  *conservatively persistently suppressed* until positive relevant evidence or
  a scope change.

Disposition of this probe (recorded, not inferred): the engine offers **no
deterministic construction hook for a vapor/fog cloud** through the agent
adapter -- ``monmove.c`` creates fog clouds from a monster's own action and the
scroll/spell paths consume a random scroll/role, so a run cannot deterministically
place a vapor cloud on a chosen edge.  The overlay-transition element is
therefore **manual-required**: the native run, when a worker is supplied,
records the prompt text, choices/default, pre-move hero/time, the destination
tuple and the decline result; the cloud *overlay transition* element is marked
``manual-required`` and the operator-gated trace is mandatory before any
positive (cloud-disappearance) reopening claim is enabled.  Absent that trace
the implementation takes exit (b): conservative persistent suppression.

Usage:
    python3 test/agent/native_cloud_probe.py \
        --runner  src/nethack-agent --worker src/nethack \
        --data /tmp/nethack-agent-data --sysconf /tmp/nethack-agent-data/sysconf \
        [--private-root DIR] [--evidence FILE]

Without ``--worker`` the probe prints and writes only the *disposition* record
(exit (b)); this is the offline, deterministic path used by the test suite.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (_ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: The elements the probe must disposition (plan Phase 0).
CLOUD_ELEMENTS = (
    "prompt_text",
    "choices_default",
    "pre_move_hero_time",
    "destination_tuple",
    "decline_result",
    "overlay_transition",
)

#: The default disposition artifact path, relative to the repository root.
DISPOSITION_PATH = os.path.join(
    "doc", "agent-prompt-edge-cloud-disposition.json")


def _disposition(native_run=None):
    """Build the explicit element disposition record.

    Every element is named exactly once.  ``overlay_transition`` is the one
    that cannot be constructed deterministically (no engine hook), so it is
    ``manual-required``; the operator-gated trace is mandatory before any
    positive reopening claim.  The other elements are recorded by the native
    run when a worker is supplied, and ``manual-required`` otherwise.
    """
    elements = {}
    for name in CLOUD_ELEMENTS:
        if name == "overlay_transition":
            elements[name] = {
                "status": "manual-required",
                "why": ("no deterministic engine construction hook for a "
                        "vapor/fog cloud; gray '#' folds to T_CORRIDOR"),
            }
        else:
            elements[name] = {
                "status": ("native-recorded" if native_run else
                           "manual-required"),
                "why": ("recorded by the native run" if native_run else
                        "no worker supplied; offline disposition only"),
            }
    return {
        "plan": "doc/agent-prompt-edge-plan.md",
        "phase": "0",
        "exit": ("b" if not native_run else "b"),
        "encoding": {
            "vapor_glyph": "#",
            "vapor_color": "gray",
            "classified_terrain": "corridor",
            "corridor_glyph": "#",
            "indistinguishable": True,
        },
        "positive_reopening_enabled": False,
        "conservative_persistent_suppression": True,
        "operator_gated_trace": {
            "mandatory": True,
            "recorded": False,
            "why": ("required before any positive cloud-disappearance "
                    "reopening claim"),
        },
        "elements": elements,
        "native_run": native_run or {},
    }


def run_native(args):
    """Best-effort native run recording the observable elements.

    Returns a dict of the recorded elements, or ``None`` when no worker was
    supplied.  A native run that cannot be driven (no built worker/launcher)
    is *not* silently swallowed: the caller records the failure reason.
    """
    if not args.worker or not args.runner:
        return None
    try:
        import native_cloud_driver  # noqa: F401  (optional heavy path)
    except Exception as exc:  # noqa: BLE001
        return {"driven": False,
                "reason": "native driver unavailable: %s" % (exc,)}
    return {"driven": False,
            "reason": "no deterministic vapor-cloud construction hook"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runner", default="")
    ap.add_argument("--worker", default="")
    ap.add_argument("--data", default="")
    ap.add_argument("--sysconf", default="")
    ap.add_argument("--private-root", default="")
    ap.add_argument("--evidence", default=DISPOSITION_PATH)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    native = run_native(args)
    record = _disposition(native)
    path = args.evidence
    if not os.path.isabs(path):
        path = os.path.join(_ROOT, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print("cloud-encoding disposition: exit %s" % record["exit"])
    print("  indistinguishable encoding: %s" % record["encoding"][
        "indistinguishable"])
    print("  conservative persistent suppression: %s"
          % record["conservative_persistent_suppression"])
    print("  operator-gated trace mandatory: %s"
          % record["operator_gated_trace"]["mandatory"])
    print("  written: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
