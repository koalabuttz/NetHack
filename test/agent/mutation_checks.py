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
import unittest

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
    "schema_version", "generated_from", "commits", "commit_range",
    "suite_count", "gates", "named_mutations", "pickup_shape_disposition",
    "lifecycle_metrics", "live_claims",
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
                "            payload = self._dest_payload(\"continue\", held, "
                "step=step)"),
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
    # -- stall-recovery plan (Rev 3) mutations ---------------------------
    {
        "name": "mutation_restore_np10_raw_grid_routing",
        "file": "tools/agent/policy.py",
        "old": ("        if np >= 10:\n"
                "            return self._bounded_recovery(\n"
                "                mem, hero, \"loop breaker: bounded escape "
                "(>=10)\")"),
        "new": ("        if np >= 10:\n"
                "            return (self._cand({\"key\": "
                "KEY.DIR_KEYS[(1, 0)]}, \"unblock\", \"recovery\", 0,\n"
                "                               \"loop breaker: unblock\",\n"
                "                               \"recovery\"),)"),
        "killer": ("test_auto_recovery.Phase2BoundedRecovery."
                   "test_np10_recovery_never_routes_through_locked_door"),
    },
    {
        "name": "mutation_gate_no_progress_on_time_advance",
        "file": "tools/agent/policy.py",
        "old": ("            if not self._hero_moved(pre_hero, mem, "
                "observed_kind):\n"
                "                self.targets.note_nav_attempt()"),
        "new": ("            if not self._hero_moved(pre_hero, mem, "
                "observed_kind) \\\n"
                "                    and observed_kind != \"no-time\":\n"
                "                self.targets.note_nav_attempt()"),
        "killer": ("test_auto_commitment.AttemptCounting."
                   "test_first_no_time_acquisition_is_attempt_one_of_three"),
    },
    {
        "name": "mutation_change_stall_ge_to_gt",
        "file": "tools/agent/navigation.py",
        "old": ("        return (self.stall_attempts >= STALL_MAX\n"
                "                or self.total_attempts >= self.stall_cap)"),
        "new": ("        return (self.stall_attempts > STALL_MAX\n"
                "                or self.total_attempts >= self.stall_cap)"),
        "killer": ("test_auto_commitment.AttemptCounting."
                   "test_cap_exhausted_held_destination_retires_at_three_zero_"
                   "time_attempts"),
    },
    {
        "name": "mutation_seed_progress_pos_at_target",
        "file": "tools/agent/navigation.py",
        "old": ("        self.progress_pos = (tuple(hero) if hero is not None "
                "else tuple(pos))"),
        "new": "        self.progress_pos = tuple(pos)",
        "killer": ("test_auto_commitment.AttemptCounting."
                   "test_first_no_time_acquisition_is_attempt_one_of_three"),
    },
    {
        "name": "mutation_drop_accepted_jev_candidate",
        "file": "tools/agent/controller.py",
        "old": ("        # The accepted member -- not the scripted winner -- owns "
                "the frozen\n"
                "        # effect from here (plan §2): its exact candidate is "
                "the selected one.\n"
                "        self._selected_candidate = outcome.candidate"),
        "new": "        self._selected_candidate = None",
        "killer": ("test_auto_integration.SelectedEffectOwnership."
                   "test_jev_accepts_different_action_non_scripted_candidate_"
                   "and_commits_its_exact_payload"),
    },
    {
        "name": "mutation_retain_selection_on_write_failure",
        "file": "tools/agent/controller.py",
        "old": ("            # A failed/partial write arms nothing (plan 3.4 step "
                "5): the\n"
                "            # selected-decision record is cleared too, so a "
                "write-failed\n"
                "            # candidate can never commit a destination or "
                "recovery effect.\n"
                "            self.selected_decision = None\n"
                "            self._selected_candidate = None\n"
                "            self._attempt_effect = None\n"
                "            self._attempt_payload = ()"),
        "new": "            self._attempt_payload = ()",
        "killer": ("test_auto_integration.SelectedEffectOwnership."
                   "test_write_failure_commits_no_selected_destination_or_"
                   "recovery_effect"),
    },
    {
        "name": "mutation_retain_discarded_override_payload",
        "file": "tools/agent/controller.py",
        "old": ("        chosen = self._selected_candidate\n"
                "        try:\n"
                "            chosen_matches = (chosen is not None\n"
                "                              and "
                "candidates.candidate_to_wire(chosen)\n"
                "                              == selected)\n"
                "        except Exception:                    # noqa: BLE001 - "
                "defensive\n"
                "            chosen_matches = False"),
        "new": ("        chosen = (self._selected_candidate\n"
                "                  or getattr(self.reflex, \"last_candidate\", "
                "None))\n"
                "        chosen_matches = chosen is not None"),
        "killer": ("test_auto_integration.SelectedEffectOwnership."
                   "test_override_does_not_commit_discarded_destination"),
    },
    {
        "name": "mutation_count_delivery_repair_twice",
        "file": "tools/agent/budget.py",
        "old": ("        if token in self._applied_tokens:\n"
                "            return False\n"
                "        self._applied_tokens.add(token)\n"
                "        self.reflex_applied += 1"),
        "new": ("        self._applied_tokens.add(token)\n"
                "        self.reflex_applied += 1"),
        "killer": ("test_auto_integration.SelectedEffectOwnership."
                   "test_delivery_repair_preserves_effect_and_applied_token_"
                   "once"),
    },
    {
        "name": "mutation_restore_raw_passable_recovery",
        "file": "tools/agent/policy.py",
        "old": ("            if not navigation.edge_legal(terrain, hero, "
                "dest):\n"
                "                continue\n"
                "            if state.monster_cell(mem.tile(dest), hero, "
                "dest):\n"
                "                continue"),
        "new": ("            if not mem.known_passable(dest):\n"
                "                continue\n"
                "            if state.monster_cell(mem.tile(dest), hero, "
                "dest):\n"
                "                continue"),
        "killer": ("test_auto_recovery.Ep4LockedDoorStall."
                   "test_cap_exhausted_locked_door_adjacent_monster_enters_"
                   "bounded_recovery"),
    },
    {
        "name": "mutation_emit_terminals_only_for_directives",
        "file": "tools/agent/policy.py",
        "old": ("        self.targets.retire(reason, pos=pos, "
                "signature=signature)\n"
                "        self._emit_destination_terminal(\n"
                "            term, reason, held.serial, purpose=held.purpose,\n"
                "            source=held.source, generation=held.generation)"),
        "new": ("        self.targets.retire(reason, pos=pos, "
                "signature=signature)\n"
                "        if held.source == navigation.SRC_DIRECTIVE:\n"
                "            self._emit_destination_terminal(\n"
                "                term, reason, held.serial, "
                "purpose=held.purpose,\n"
                "                source=held.source, "
                "generation=held.generation)"),
        "killer": ("test_auto_commitment.DestinationTerminalOwner."
                   "test_unreachable_default_retirement_is_visible"),
    },
    {
        "name": "mutation_occupancy_in_service_signature",
        "file": "tools/agent/navigation.py",
        "old": ("    pos = tuple(pos)\n"
                "    out = [terrain.ter(pos)]\n"
                "    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):\n"
                "        out.append(terrain.ter((pos[0] + dx, pos[1] + dy)))\n"
                "    return tuple(out)"),
        "new": ("    pos = tuple(pos)\n"
                "    out = [terrain.ter(pos)]\n"
                "    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):\n"
                "        nb = (pos[0] + dx, pos[1] + dy)\n"
                "        out.append(terrain.ter(nb))\n"
                "        out.append(\"occ\" if terrain.occupant(nb) != "
                "OCC_NONE else \"\")\n"
                "    return tuple(out)"),
        "killer": ("test_auto_commitment.Phase3EvidenceSplit."
                   "test_serviced_frontier_ignores_transient_neighbor_"
                   "occupancy"),
    },
    {
        "name": "mutation_reject_sole_legal_reverse",
        "file": "tools/agent/policy.py",
        "old": ("        if not options:\n"
                "            return None\n"
                "        options.sort()\n"
                "        return options[0][3]"),
        "new": ("        if not options:\n"
                "            return None\n"
                "        options = [o for o in options if o[0] == 0]\n"
                "        if not options:\n"
                "            return None\n"
                "        options.sort()\n"
                "        return options[0][3]"),
        "killer": ("test_auto_recovery.Phase2BoundedRecovery."
                   "test_recovery_only_legal_reverse_is_not_trapped"),
    },
    # -- prompt-edge plan (Rev 3) mutations ------------------------------
    {
        "name": "mutation_restore_response_kind_only_filter",
        "file": "tools/agent/controller.py",
        "old": ("                (self._matched_gameplay_attempt\n"
                "                 and frame_kind in (\"command\", \"key\", "
                "\"direction\"))\n"
                "                or matched_prompt))"),
        "new": ("                (self._matched_gameplay_attempt\n"
                "                 and frame_kind in (\"command\", \"key\", "
                "\"direction\"))))"),
        "killer": ("test_auto_prompt_edge.LiveEvaluatorParity."
                   "test_live_evaluator_movement_prompt_accounting_and_"
                   "ledger_parity"),
    },
    {
        "name": "mutation_permit_every_yn_to_advance",
        "file": "tools/agent/arbitration.py",
        "old": ("    low = \" \".join(str(prompt_text).lower().split())\n"
                "    return (\"into that \" in low and "
                "low.rstrip().endswith(\"cloud?\")\n"
                "            and (\"vapor cloud\" in low or \"poison gas "
                "cloud\" in low))"),
        "new": ("    low = \" \".join(str(prompt_text).lower().split())\n"
                "    return True"),
        "killer": ("test_auto_prompt_edge.MovementEntryRecognition."
                   "test_only_cloud_confirmations_are_recognized"),
    },
    {
        "name": "mutation_count_prompt_arrival_and_answer",
        "file": "tools/agent/policy.py",
        "old": ("        if self._pending_prompt is not None:\n"
                "            return False\n"
                "        pending = arbitration.matched_movement_prompt("),
        "new": "        pending = arbitration.matched_movement_prompt(",
        "killer": ("test_auto_prompt_edge.MatchedMovementAccounting."
                   "test_matched_movement_vapor_prompt_advances_stationary_"
                   "once"),
    },
    {
        "name": "mutation_infer_movement_from_raw_direction",
        "file": "tools/agent/arbitration.py",
        "old": ("    label = getattr(candidate, \"semantic_label\", \"\")\n"
                "    if not label or label in _REJECTED_MOVEMENT_LABELS:\n"
                "        return None\n"
                "    op = _ACCEPTED_MOVEMENT_OPS.get(label)\n"
                "    if op is None:\n"
                "        return None"),
        "new": ("    label = getattr(candidate, \"semantic_label\", \"\")\n"
                "    op = _ACCEPTED_MOVEMENT_OPS.get(label,\n"
                "                                     (\"destination\", \"normal\"))"),
        "killer": ("test_auto_prompt_edge.MovementOriginTaxonomy."
                   "test_movement_origin_operation_taxonomy_matrix"),
    },
    {
        "name": "mutation_record_evidence_at_proposal_or_stale_n",
        "file": "tools/agent/policy.py",
        "old": ("        p = self._pending_prompt\n"
                "        if not arbitration.prompt_decline_confirmed(\n"
                "                p, instance, confirmed_hero, dismissed):\n"
                "            return False"),
        "new": ("        p = self._pending_prompt\n"
                "        if p is None:\n"
                "            return False"),
        "killer": ("test_auto_prompt_edge.DeclineEvidence."
                   "test_prompt_decline_requires_matching_answer_send_and_"
                   "resolution"),
    },
    {
        "name": "mutation_derive_dst_from_semantic_destination",
        "file": "tools/agent/arbitration.py",
        "old": ("    src = (int(pre_hero[0]), int(pre_hero[1]))\n"
                "    dst = (src[0] + delta[0], src[1] + delta[1])"),
        "new": ("    src = (int(pre_hero[0]), int(pre_hero[1]))\n"
                "    _p = getattr(candidate, \"effect_payload\", ()) or ()\n"
                "    if len(_p) >= 6 and _p[0] == \"dest\":\n"
                "        dst = (int(_p[4]), int(_p[5]))\n"
                "    else:\n"
                "        dst = (src[0] + delta[0], src[1] + delta[1])"),
        "killer": ("test_auto_prompt_edge.MovementOriginTaxonomy."
                   "test_movement_origin_dst_is_origin_edge_not_destination"),
    },
    {
        "name": "mutation_compare_stored_prompt_with_absent_command",
        "file": "tools/agent/policy.py",
        "old": ("        if rec is None:\n"
                "            return False\n"
                "        if not self.positive_reopening_enabled:\n"
                "            return True\n"
                "        return rec[0] == navigation.blocked_edge_signature(\n"
                "            terrain, tuple(src), tuple(dst))"),
        "new": ("        if rec is None:\n"
                "            return False\n"
                "        return False"),
        "killer": ("test_auto_prompt_edge.PromptEdgeSuppression."
                   "test_prompt_declined_edge_survives_prompt_absence_and_"
                   "destination_reacquisition"),
    },
    {
        # plan mutation 8 ("omit local cloud evidence / reopen on revisions")
        # retargeted: under exit (b) the disposition disables signature-based
        # reopening, so the demonstrable mutation is forcing that path on.
        "name": "mutation_use_signature_reopening_under_exit_b",
        "file": "tools/agent/policy.py",
        "old": ("        if not self.positive_reopening_enabled:\n"
                "            return True\n"
                "        return rec[0] == navigation.blocked_edge_signature(\n"
                "            terrain, tuple(src), tuple(dst))"),
        "new": ("        return rec[0] == navigation.blocked_edge_signature(\n"
                "            terrain, tuple(src), tuple(dst))"),
        "killer": ("test_auto_prompt_edge.PromptEdgeSuppression."
                   "test_exit_b_suppression_survives_local_signature_changes"),
    },
    {
        # retargeted from the prompt-side remote-occupancy mutation (invisible
        # under exit (b)) to the recovery ledger that still compares signatures.
        "name": "mutation_recovery_edge_never_reopens",
        "file": "tools/agent/policy.py",
        "old": ("        stored = self.blocked_edges.get(key)\n"
                "        if stored is None:\n"
                "            return False\n"
                "        return stored == navigation.blocked_edge_signature("),
        "new": ("        stored = self.blocked_edges.get(key)\n"
                "        if stored is None:\n"
                "            return False\n"
                "        return True\n"
                "        return navigation.blocked_edge_signature("),
        "killer": ("test_auto_recovery.Phase2EdgeFailureAndExhaustion."
                   "test_failed_route_reopens_after_blocker_leaves"),
    },
    {
        "name": "mutation_bind_any_yn_answer",
        "file": "tools/agent/arbitration.py",
        "old": ("    if pending is None:\n"
                "        return False\n"
                "    if candidates.normalize_need_key(need_key) \\\n"
                "            != candidates.normalize_need_key("
                "pending.response_need_key):\n"
                "        return False\n"
                "    if answer_byte is None:\n"
                "        return False\n"
                "    return int(answer_byte) == DECLINE_BYTE"),
        "new": "    return pending is not None",
        "killer": ("test_auto_prompt_edge.MatchedMovementAccounting."
                   "test_answer_binding_requires_exact_response_key_and_n_"
                   "byte"),
    },
    {
        "name": "mutation_emergency_step_three_ignores_emergency_record",
        "file": "tools/agent/policy.py",
        "old": ("            normal_only = (\n"
                "                self._prompt_edge_suppressed(terrain, hero, "
                "dest,\n"
                "                                             "
                "navigation.ACTION_NORMAL)\n"
                "                and not self._prompt_edge_suppressed(\n"
                "                    terrain, hero, dest, "
                "navigation.ACTION_EMERGENCY)\n"
                "                and not self._edge_blocked(terrain, hero, "
                "dest))"),
        "new": ("            normal_only = (\n"
                "                self._prompt_edge_suppressed(terrain, hero, "
                "dest,\n"
                "                                             "
                "navigation.ACTION_NORMAL)\n"
                "                and not self._edge_blocked(terrain, hero, "
                "dest))"),
        "killer": ("test_auto_prompt_edge.EmergencyFallback."
                   "test_emergency_step_three_both_records_are_not_retried"),
    },
    {
        # round-2 F1
        "name": "mutation_full_seq_prompt_continuity",
        "file": "tools/agent/arbitration.py",
        "old": ("    identity = prompt_request_identity(frame_key)\n"
                "    if identity is None:\n"
                "        return False\n"
                "    if identity != prompt_request_identity("
                "pending.response_need_key):\n"
                "        return False\n"
                "    return normalize_prompt_text(frame_prompt) \\\n"
                "        == normalize_prompt_text(pending.prompt_text)"),
        "new": ("    if tuple(candidates.normalize_need_key(frame_key)) \\\n"
                "            != tuple(candidates.normalize_need_key("
                "pending.response_need_key)):\n"
                "        return False\n"
                "    return normalize_prompt_text(frame_prompt) \\\n"
                "        == normalize_prompt_text(pending.prompt_text)"),
        "killer": ("test_auto_prompt_edge.LiveEvaluatorParity."
                   "test_re_presented_same_id_prompt_stays_bound_live_and_"
                   "replay"),
    },
    {
        # round-2 F2
        "name": "mutation_evaluator_invalid_keeps_prompt_transaction",
        "file": "tools/agent/evaluate.py",
        "old": ("        self.reflex.clear_pending_prompt()\n"
                "        # It is recorded as its own decision row, and -- when "
                "the attempt it"),
        "new": ("        # It is recorded as its own decision row, and -- when "
                "the attempt it"),
        "killer": ("test_auto_prompt_edge.LiveEvaluatorParity."
                   "test_evaluator_invalid_clears_prompt_transaction_like_"
                   "live"),
    },
    {
        "name": "mutation_filter_only_seed_edges",
        "file": "tools/agent/navigation.py",
        "old": ("            if not admit(pos, nb):\n"
                "                continue\n"
                "            nd = d + _edge_cost(terrain, nb, visits, "
                "failed)"),
        "new": ("            if not edge_legal(terrain, pos, nb):\n"
                "                continue\n"
                "            nd = d + _edge_cost(terrain, nb, visits, "
                "failed)"),
        "killer": ("test_auto_prompt_edge.EdgeAdmissiblePredicate."
                   "test_forbidden_edge_filters_seed_and_interior"),
    },
    {
        "name": "mutation_honor_yes_native_default_for_cloud",
        "file": "tools/agent/policy.py",
        "old": ("        if arbitration.is_movement_entry_confirmation("
                "prompt):\n"
                "            return {\"yn\": KEY.KEY_N}, \"decline cloud "
                "entry\", \"prompt\", ()"),
        "new": ("        if False and "
                "arbitration.is_movement_entry_confirmation(prompt):\n"
                "            return {\"yn\": KEY.KEY_N}, \"decline cloud "
                "entry\", \"prompt\", ()"),
        "killer": ("test_auto_prompt_edge.DeclineEvidence."
                   "test_cloud_confirmation_declines_even_with_yes_native_"
                   "default"),
    },
    {
        "name": "mutation_keep_scoped_prompt_ledger_on_reset",
        "file": "tools/agent/policy.py",
        "old": ("        self.blocked_edges = {}\n"
                "        self.prompt_declined_edges = {}\n"
                "        self.clear_pending_prompt()"),
        "new": ("        self.blocked_edges = {}\n"
                "        self.clear_pending_prompt()"),
        "killer": ("test_auto_prompt_edge.PromptEdgeSuppression."
                   "test_prompt_ledger_overwrites_per_edge_and_clears_on_"
                   "instance_reset"),
    },
    {
        "name": "mutation_remove_unchanged_confirmed_hero_condition",
        "file": "tools/agent/arbitration.py",
        "old": ("    if hero is None or tuple(hero) != tuple(pending.src):\n"
                "        return False\n"
                "    return True"),
        "new": "    return True",
        "killer": ("test_auto_prompt_edge.DeclineEvidence."
                   "test_prompt_decline_resolution_with_hero_progress_records_"
                   "no_edge"),
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


def _suite_count():
    """The *actual* number of discovered auto tests, never a constant.

    Discovery imports the suites but runs nothing, so the count always tracks
    the real suite; a discovery failure reports ``None`` ("unavailable") rather
    than a stale constant.
    """
    try:
        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(ROOT, "test", "agent"),
                                pattern="test_auto*.py")
        return suite.countTestCases()
    except Exception:                        # noqa: BLE001
        return None


#: The explicitly named commit that opens this implementation series (review
#: item 9).  Its PARENT is the implementation base, and provenance covers every
#: commit from there through HEAD -- not a derived ``git log -8`` window.
_IMPLEMENTATION_BASE_MARKER = "agent: native pickup-shape probe"


def _implementation_base():
    """The parent of the named base commit (explicit, stable, not a window)."""
    for line in reversed(_git("log", "--format=%H %s").splitlines()):
        sha, _, subject = line.partition(" ")
        if subject.startswith(_IMPLEMENTATION_BASE_MARKER):
            return _git("rev-parse", "%s^" % sha) or sha
    return _git("rev-parse", "HEAD")


def _commit_range():
    """The implementation range: the explicit base .. final HEAD (item 9)."""
    return {"base": _implementation_base(),
            "head": _git("rev-parse", "HEAD")}


def _commits_since_base():
    """EVERY commit from the implementation base through HEAD, oldest first.

    Replaces the old ``git log -8`` window: the provenance must name the whole
    implementation range, so a reader can see all phase commits (review item 9).
    """
    rng = "%s..HEAD" % _implementation_base()
    out = []
    for line in _git("log", "--reverse", "--format=%H %s", rng).splitlines():
        sha, _, subject = line.partition(" ")
        if sha:
            out.append({"hash": sha, "subject": subject})
    return out


def _lifecycle_summary(artifact=None):
    """The lifecycle metric summary recorded in the validation report.

    Derived from a *produced artifact* when one is supplied (its persisted
    event sidecar carries the ``record: "lifecycle"`` stream); without an
    artifact no lifecycle events exist here, so every metric is reported as
    *unavailable* (``None``), never a manufactured zero (plan section 5).
    """
    if artifact:
        from tools.agent import lifecycle_metrics
        return lifecycle_metrics.summarize_artifact(artifact)
    from tools.agent import lifecycle_metrics
    return lifecycle_metrics.summarize([])


def build_report(*, gates=None, mutations=None, artifact=None):
    # provenance is the WHOLE implementation range (base..HEAD), never a
    # mis-derived fixed window (review item 9)
    commits = _commits_since_base()
    mutations = list(mutations or [])
    for m in NOT_PERFORMED:
        mutations.append({"name": m["name"], "killer": None,
                          "status": "not-performed", "reason": m["reason"]})
    return {
        "schema_version": REPORT_SCHEMA,
        "generated_from": {"repository": "nethack",
                           "scope": "agent destination commitment + pickup"},
        "commits": commits,
        "commit_range": _commit_range(),
        "suite_count": _suite_count(),
        "lifecycle_metrics": _lifecycle_summary(artifact),
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
    ap.add_argument("--artifact", default=None,
                    help="a produced ep-N.events.jsonl sidecar to derive the "
                         "lifecycle metrics from")
    ap.add_argument("--out", default=REPORT_PATH)
    args = ap.parse_args(argv)
    results = [run_mutation(m) for m in MUTATIONS]
    report = build_report(mutations=results, artifact=args.artifact)
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
