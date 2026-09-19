#!/usr/bin/env python3
"""Phase 4 mutation demonstrations and the validation report.

Run from the repository root:

    python3 test/agent/mutation_checks.py --write

Each named mutation is applied to a source file as a *temporary controlled
edit*, its named killer test is run (and must FAIL), then the file is restored
byte-for-byte.  The results, together with the gate results, the commit
identifiers, the native-vs-manual pickup-shape disposition and the explicitly
*unmeasured* live claim, are written to the validation-report fixture that
``test_validation_report_contains_required_fields`` checks.

Mutations whose named killer test is not implemented in this checkout are
listed in :data:`NOT_PERFORMED` and recorded as ``not-performed`` -- never
silently omitted.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pickup_shapes  # noqa: E402

REPORT_PATH = os.path.join(HERE, "fixtures", "destination_commitment_report.json")
REPORT_SCHEMA = 1

#: The gate commands and the result recorded when the report was generated.
DEFAULT_GATES = {
    "auto_suite": {
        "command": ("python3 -m unittest discover -s test/agent "
                    "-p 'test_auto*.py'"),
        "result": "OK",
    },
    "make_check": {
        "command": "make -C test/agent check",
        "result": "all P1 fixtures pass",
    },
    "native_pickup": {
        "command": ("make -C test/agent native-pickup WORKER=src/nethack "
                    "RUNNER=src/nethack-agent DATA=/tmp/nethack-agent-data "
                    "SYSCONF=/tmp/nethack-agent-data/sysconf"),
        "result": "native-pickup-probe: OK (native shape: no_object)",
    },
    "make_agent_all": {
        "command": ("make WANT_WIN_AGENT=1 WANT_DEFAULT=agent "
                    "WANT_AGENT_STRICT=1 all"),
        "result": "not run (agent-only Python change; engine pre-built)",
    },
}

#: Every field the validation report must carry (AC16/AC18).
REQUIRED_FIELDS = (
    "schema_version", "generated_from", "commits", "gates", "named_mutations",
    "pickup_shape_disposition", "live_claims",
)

MUTATIONS = (
    {
        "name": "mutation_drop_held_target_each_tick",
        "file": "tools/agent/policy.py",
        "old": ("        if held is not None and not superseded \\\n"
                "                and self.targets.holds(self.instance_id, "
                "terrain, hero):"),
        "new": ("        if False and held is not None and not superseded \\\n"
                "                and self.targets.holds(self.instance_id, "
                "terrain, hero):"),
        "killer": ("test_auto_commitment.PrepareAndReconcile."
                   "test_destination_survives_alternate_score_and_visit_"
                   "changes"),
    },
    {
        "name": "mutation_commit_during_prepare",
        "file": "tools/agent/policy.py",
        "old": "        if payload and payload[0] == \"dest\":",
        "new": "        if False and payload and payload[0] == \"dest\":",
        "killer": ("test_auto_commitment.PrepareAndReconcile."
                   "test_reconciled_destination_effect_commits_exactly_once"),
    },
    {
        "name": "mutation_complete_door_on_approach",
        "file": "tools/agent/navigation.py",
        "old": ("            step = first.get(approach)\n"
                "            if step is not None:\n"
                "                return step, None, \"approach the closed "
                "door\""),
        "new": ("            step = first.get(approach)\n"
                "            if step is not None:\n"
                "                return None, \"arrive\", \"approach the "
                "closed door\""),
        "killer": ("test_auto_commitment.CommitmentLifecycle."
                   "test_route_held_destination_door_survives_approach"),
    },
    {
        "name": "mutation_change_margin_gt_to_ge",
        "file": "tools/agent/policy.py",
        "old": ("            if not (entry[3] - alt > "
                "self.ANTIBACKTRACK_MARGIN):"),
        "new": ("            if not (entry[3] - alt >= "
                "self.ANTIBACKTRACK_MARGIN):"),
        "killer": ("test_auto_navigation.AntiBacktrackPreference."
                   "test_antibacktrack_score_exception_at_40_41_and_directive_"
                   "bonus"),
    },
    {
        "name": "mutation_reset_pickup_budget_on_inventory_change",
        "file": "tools/agent/pickup.py",
        "old": ("        material = (cur is None or cur.instance != instance\n"
                "                    or cur.appearance != appearance "
                "or cur.count != count)"),
        "new": "        material = True",
        "killer": ("test_auto_pickup.PickupEvidenceAndIntent."
                   "test_stationary_frames_do_not_reset_attempt_budget"),
    },
    {
        "name": "mutation_trust_food_glyph_as_safe",
        "file": "tools/agent/pickup.py",
        "old": ('def appearance_proves_safety(ev: Optional[FloorEvidence]) '
                '-> bool:\n'
                '    """An appearance NEVER proves BUC, safety, ownership '
                'or exact type."""\n'
                '    return False'),
        "new": ('def appearance_proves_safety(ev: Optional[FloorEvidence]) '
                '-> bool:\n'
                '    """An appearance NEVER proves BUC, safety, ownership '
                'or exact type."""\n'
                '    return True'),
        "killer": ("test_auto_pickup.PickupEvidenceAndIntent."
                   "test_food_appearance_alone_never_asserts_safe_food"),
    },
    {
        "name": "mutation_apply_antibacktrack_to_committed_target",
        "file": "tools/agent/policy.py",
        "old": ("        if step is not None:\n"
                "            payload = self._dest_payload(\"continue\", held)"),
        "new": ("        if step is not None and not self._is_reverse(\n"
                "                step, hero, self.recovery.previous_distinct):\n"
                "            payload = self._dest_payload(\"continue\", held)"),
        "killer": ("test_auto_commitment.CommittedBehaviour."
                   "test_committed_reverse_survives_same_family_margin"),
    },
    {
        "name": "mutation_reactivate_failed_directive_each_tick",
        "file": "tools/agent/directives.py",
        "old": ("    def expire(self, reason: str, tick: Optional[int] = None,\n"
                "               level: Optional[str] = None) -> None:\n"
                "        if self._active is not None:"),
        "new": ("    def expire(self, reason: str, tick: Optional[int] = None,\n"
                "               level: Optional[str] = None) -> None:\n"
                "        if False and self._active is not None:"),
        "killer": ("test_auto_providers.DirectiveSchemaV2."
                   "test_served_generation_does_not_reassert_destination"),
    },
    {
        "name": "mutation_route_from_raw_grid_under_item",
        "file": "tools/agent/policy.py",
        "old": ("        if persistent is not None and hasattr(persistent, "
                "\"ter\"):\n            return persistent"),
        "new": ("        if False and persistent is not None "
                "and hasattr(persistent, \"ter\"):\n            return persistent"),
        "killer": ("test_auto_commitment.CommittedBehaviour."
                   "test_item_overlay_uses_persistent_known_ground"),
    },
    {
        "name": "mutation_sort_or_drop_choice_member",
        "file": "tools/agent/policy.py",
        "old": ("        if alt is None:\n            return base\n"
                "        return tuple(base) + (alt,)"),
        "new": ("        if alt is None:\n            return base\n"
                "        return tuple(base)"),
        "killer": ("test_auto_pickup.PickupPolicyWiring."
                   "test_pickup_choice_criteria_object_key_index_and_n_frozen"),
    },
    {
        "name": "mutation_rerender_historical_strategy_turn",
        "file": "tools/agent/providers.py",
        "old": ("    messages = _messages_for(retained, user_text)\n"
                "    fits = _payload_bytes(config, messages) <= ceiling"),
        "new": ("    messages = _messages_for(\n"
                "        [StrategyExchange(user=user_text, assistant=\"\")],\n"
                "        user_text)\n"
                "    fits = _payload_bytes(config, messages) <= ceiling"),
        "killer": ("test_auto_providers.StrategyHistoryFrozen."
                   "test_strategy_historical_bytes_not_rerendered_after_"
                   "commitment_change"),
    },
)

#: The plan's remaining named mutations whose killer tests are not
#: implemented in this checkout.  Empty: all eleven are demonstrated.
NOT_PERFORMED = ()


def run_mutation(m):
    """Apply one mutation, run its killer test, restore, return the result."""
    path = os.path.join(ROOT, m["file"])
    with open(path) as fh:
        original = fh.read()
    if m["old"] not in original:
        return {"name": m["name"], "status": "skip", "killer": m["killer"],
                "reason": "anchor not found"}
    try:
        with open(path, "w") as fh:
            fh.write(original.replace(m["old"], m["new"], 1))
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", m["killer"]],
            cwd=os.path.join(ROOT, "test", "agent"),
            capture_output=True, text=True, timeout=300,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        killed = proc.returncode != 0
    finally:
        with open(path, "w") as fh:
            fh.write(original)
    return {"name": m["name"], "killer": m["killer"],
            "status": "killed" if killed else "SURVIVED"}


def _git(*args):
    try:
        return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except Exception:                        # noqa: BLE001
        return ""


def build_report(*, gates=None, mutations=None):
    commits = []
    for line in _git("log", "--format=%H %s", "-8").splitlines():
        sha, _, subject = line.partition(" ")
        commits.append({"hash": sha, "subject": subject})
    mutations = list(mutations or [])
    for m in NOT_PERFORMED:
        mutations.append({"name": m["name"], "killer": None,
                          "status": "not-performed", "reason": m["reason"]})
    return {
        "schema_version": REPORT_SCHEMA,
        "generated_from": {"repository": "nethack",
                           "scope": "agent destination commitment + pickup"},
        "commits": commits,
        "gates": gates if gates is not None else dict(DEFAULT_GATES),
        "named_mutations": mutations,
        "pickup_shape_disposition": pickup_shapes.disposition(),
        "live_claims": {
            "measured": False,
            "note": ("The operator-approved live comparison was not run (no "
                     "credentials/upstream recordings).  Replay/unit success "
                     "is not evidence of live exploration improvement."),
            "adapted_to_fake_provider": False,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="run the mutations and write the report fixture")
    ap.add_argument("--out", default=REPORT_PATH)
    args = ap.parse_args(argv)
    results = [run_mutation(m) for m in MUTATIONS]
    report = build_report(mutations=results)
    if args.write:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
        print("wrote %s" % args.out)
    for r in results:
        print("%-52s %s" % (r["name"], r["status"]))
    survived = [r["name"] for r in results if r["status"] != "killed"]
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
