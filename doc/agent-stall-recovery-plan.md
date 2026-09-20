# Stall recovery, effect ownership, and exploration progression plan (Revision 3 — DRAFT, pending plan review)

> **Round-1 plan-review provenance.** Review verdict: **REVISE — 7 findings, all addressed**; the reviewer verified root-cause claims (a)–(i). Changes: (1) an exact acquisition/stationary-attempt counting contract as a transition table keyed by frozen pre-send hero, selected operation, and reconciled outcome (§2A, new); (2) a lifecycle transition/event table with exactly one terminal disposition per acquired serial, an additive `schema_version` bump, and defined `destination_switch_rate` semantics for `replaced` events (§3); (3) an evidence-signature split into `service_signature`, `door_failure_signature`, and `blocked_edge_signature` with an instance + edge/action-keyed blockage ledger (§4); (4) door-refusal binding to newly observed message IDs since the frozen attempt baseline, with live/evaluator parity (§4); (5) recovery retirement timing fixed at the selected/reconciled recovery-effect boundary (§1); (6) an explicit AC1–AC9 → named-test table including existing regression pins and report-schema/value tests (§AC → named-test map); (7) two implementable replacements for the same-wire test, keeping the forced-override coincidence test separate (§Phase 1, AC3); (8) a mandated post-implementation reviewer loop against the actual diff and AC1–AC9 plus inherited contracts (§Review and documentation); (9) a contract-migration checklist entry for `test_stationary_recovery_ladder_3_6_10_unchanged` with gate/nav-plan clause and code-comment updates (§Review and documentation). Sections not named here are retained verbatim from the draft.

> **Round-2 plan-review provenance.** Review verdict: **APPROVE WITH FIXES — 3 Medium, 3 Low, all addressed** in this revision. Changes: (1) the Phase 3 file list is expanded to include `state.py`, `controller.py`, `evaluate.py`, and the selected-decision/attempt record, and an explicit door-refusal-seam substep binds refusal to the message-ID/text baseline captured when the exact door candidate is armed (§Phase 3, §4); (2) every mutation-check category reference is replaced by exact test names from the AC map, with a new dedicated `test_write_failure_commits_no_selected_destination_or_recovery_effect` for the commit-before-write/retained-override-payload mutation and the AC8 tests named for the gate/Choice-order/historical-bytes pins (§Mutation checks); (3) the AC4 map row now also carries `test_new_locked_message_fails_matching_door_once` and `test_stale_locked_message_does_not_fail_new_door` (kept under AC7); (4) the exact persisted lifecycle record shape is stated, with `outcome=replaced` terminal and the sole input to replacement switch pairing, and a legacy-stream rule for old `schema`-only records (§3); (5) reviewer-scrutiny items 4 and 7 are reworded and the migration entry's old expectation corrected to "all three thresholds return the recovery family" (§Reviewer scrutiny, §Contract migration checklist); (6) the review loop assigns the fix/rebut duty to the execute agent and requires redispatch after Critical/Important fixes (§Review and documentation). Sections not named here are retained verbatim from Revision 2.

Design produced by `architect:architect-stall-wander` from the post-commitment live-campaign diagnosis. Follow-up to `doc/agent-destination-commitment-plan.md` (live) and `doc/agent-jev-gate-nav-plan.md` (live).

## Recommendation

Fix the shared bounded-recovery path and selected-candidate effect ownership first; repair destination lifecycle reporting before tuning navigation or Jev confidence. The strongest supported diagnosis is **an unbounded legacy recovery loop into a locked door, with incomplete lifecycle telemetry**, not a demonstrated immortal held destination caused by the paid cap. Existing code already increments stationary evidence without time advance and already executes recovery before commitment routing. Its highest stationary branch bypasses the safe planner, search bounds, and destination accounting.

Read-only investigation completed. The architect read AGENTS.md, relevant source and plan sections, and targeted campaign artifacts. No files were modified, no commands executed, no tests run. Aggregate campaign statistics quoted by the caller remain caller-supplied; artifact samples independently inspected are identified below.

## Goal

1. Eliminate episode-4-style zero-time bump loops under capped, unavailable, rejected, timed-out, and normal scripted selection.
2. Ensure each actually selected/sent/reconciled navigation or interaction action updates the correct destination, with three-attempt no-progress and two-attempt ineffective-door bounds.
3. Keep recovery reachable at every stationary threshold, using legal edges and existing bounded search/forced-search/quit handling.
4. Reduce avoidable frontier reacquisition without weakening emergency precedence, deliberate committed reversals, or legitimate return travel.
5. Make lifecycle and rejection diagnostics reliable enough to validate these claims.

## Implementation Summary

- Replace the unsafe stationary recovery exits with one shared edge-legal, bounded recovery candidate path; retain the established emergency/hunger precedence and the 3/6/10 escalation concept, but explicitly amend the prior plan's instruction to leave stationary semantics unchanged.
- Carry the exact selected immutable candidate through final validation, override, send, and reconciliation. A raw wire action and a mutable `last_candidate` side channel are insufficient.
- Correct acquisition/continuation accounting using the actual pre-send hero/evidence baseline; count no-time attempts, not invented movement or repeated observations. Preserve pickup's distinct send-boundary ownership.
- Centralize destination retirement and lifecycle emission for default and directive-owned targets; distinguish recovery retirement from ordinary suspension.
- Separate successful exploration servicing evidence from transient blockage/failure evidence. Fix proven occupancy-driven reopening before adding new scoring/hysteresis.
- Add end-to-end regressions, live/evaluator parity coverage, additive decision diagnostics, and controlled validation. Do not lower the Jev threshold or expand tables to improve acceptance statistics.

## Verified facts and root-cause analysis

### A. Exact cap fallback / commit path

1. `tools/agent/controller.py:3021-3055`: `_decide_jev` freshly calls `reflex.prepare(ctx)`, installs it on `ctx`, calls `reflex.decide(ctx)`, then returns that scripted action when the applied cap is unavailable. Thus the cap does not reuse an old route step or old prepared table in this code.
2. `tools/agent/policy.py:224-254`: `decide` retains the selected scripted candidate in `last_candidate`, including its frozen payload. `fallback` at 796-797 delegates to `decide`.
3. `controller.py:2731-2794,2852-2900`: final selection/forced override precedes the complete write. Every successfully sent gameplay action arms an attempt, regardless of provider name.
4. `controller.py:1621-1655`: `_arm_attempt` keeps `last_candidate` if its wire action equals the sent action; otherwise it synthesizes an effect-less `sent` candidate. `controller.py:1379-1399` reconciles, commits memory, folds observation, then applies the frozen effect. There is **no Jev-only commit gate**.
5. Separate real defect: `controller.py:3096-3137` validates and returns the accepted Jev member's wire action but does not replace the scripted `last_candidate` with that member. If Jev chooses another action, `_arm_attempt` loses its destination/pickup effect. If distinct semantic candidates can share a wire action, wire equality alone is also insufficient proof of ownership. This can undermine commitments during accepted Jev decisions, but does not explain all-scripted post-cap spam.

### A. Why the stationary loop persists

- `tools/agent/state.py:626-634`: a confirmed unchanged hero increments `no_progress` on each committed observation without a displayed-time gate. `policy.note_observation` is not responsible for that counter and is not skipped by the cap.
- `policy.py:1000-1031`: the 3/6/10 ladder still executes **before** `_navigation_candidates`. It was not removed by the commitment pipeline. At >=10, `_unblock` returns immediately; at >=6, `_random_move` returns immediately. At >=3, refused search falls back to ordinary navigation. These exits do not consistently enter the bounded recovery/forced-search endpoint, and >=3 can bypass independent cycle handling at 1021.
- `policy.py:1933-1946`: `_unblock`, with an adjacent monster and unsafe rest, finds a legacy raw-grid frontier and follows `_first_step`; it otherwise returns unbudgeted search or wait. `policy.py:1962-1971,2010-2046`: these helpers ignore commitment failure suppression and use a separate raw-grid Dijkstra without `navigation.edge_legal`.
- `state.py:39-41,145-146`: legacy `passable` permits `+`. Thus `_unblock` can repeatedly choose a locked door as the first step toward a frontier. No-time keeps `no_progress >=10`; the same branch wins forever. Recovery moves carry effect `recovery`, not a `dest` payload (`policy.py:1002-1005`), so no destination action events or destination stall/door increments are expected for those decisions.
- `policy.py:1979-1997`: `_random_move` also uses raw known-passable destinations rather than the classified edge legality helper. It can choose a closed door/illegal diagonal, and its safe-rest fallback returns wait; `_search_fallback` treats any result other than search as a movement alternative (`1502-1517`). This is another unbounded recovery escape hatch.
- `recovery.py:291-328`: repeated identical positions preserve movement history/cycle state; they do not create a period-1 cycle. The detector intentionally addresses ABAB/ABCABC, not stationary bumps. That is not a missing increment; the stationary reducer must handle this case.

**Artifact corroboration:** `ep-4.decisions.jsonl:848-852` shows repeated scripted east actions labeled only `jev paid-reflex cap reached`. `ep-4.wire.jsonl:1691-1696` contains repeated time 727, hero `(64,4)`, a closed door `+` at `(65,4)`, a monster `:` at `(63,4)`, HP 13/16, and a newly numbered `This door is locked.` message each observation. This is strong evidence for the adjacent-monster `_unblock` locked-door loop. The current decision log omits the underlying scripted candidate reason, so a replay fixture should confirm the exact branch; the architect did not run that replay.

### A. Why the frozen lifecycle does not prove an immortal destination

- `policy.py:1666-1684`: the observation fold immediately retires a held open-door commitment upon `is locked`, independently of the selected action payload. The sampled wire explicitly contains that message.
- `navigation.py:574-599`: retirement/cycle invalidation clears the active store and appends to a private `events` list.
- `policy.py:1263-1279`: public destination reached/failed emission is coupled to `_settle_directive`. Default targets call `targets.retire(...)` without this public terminal emission; see 678-683, 708-734 and 1678-1684. No drain of `targets.events` was found in the targeted `tools/agent` search.
- Consequently, serial 73 may have been retired on locked-door evidence while the lifecycle sidecar remained frozen. Do not infer that it stayed held for 14k decisions. The measured duration/switch summaries are incomplete until all terminal events are emitted.
- `policy.py:684-725`: continuation counts navigation attempts and adjacent door attempts, then checks door/stall bounds. These checks are not reached by legacy recovery actions. Explicit door refusal can retire before `commit_effect`, making a continuation stale; this is legitimate, but it must produce a visible terminal event.
- Additional off-by-one risk: `navigation.py:512-516` initializes `progress_pos` to the target, not the hero; the first stationary continuation at a different hero square resets stalls in `note_progress` (542-549). Acquisition also returns before counting the initiating action (`policy.py:625-667`). A three-attempt bound cannot be specified rigorously without fixing the pre-send baseline and first-attempt accounting.

### A. Episode 2 trapped exit

The caller described episode 2 as dying. The artifact instead reports `stop_reason: policy-exhausted`, `game_outcome: unknown`, 24 ticks, clean return code and no protocol failure (`ep-2.meta.json:109-123`). `ep-2.decisions.jsonl:26-34` shows three `m`/`s` forced-search transactions, then `forced search denied: trapped`, followed by native quit and confirmation. `tools/agent/forced_search.py:56-63` defines the three-activation cap and trapped reason; controller's override/quit path is at 2516-2569 and 2684 onward. This is consistent with intended bounded exhaustion, not proven premature death. A fixture should verify that alternatives really were exhausted and identify the failed gate; do not raise the activation cap to mask the behavior. The strategy was still pending/cancelled at episode end (`ep-2.events.jsonl:7-17`), so cooldown/strategy latency did not rescue it, but safe reflex behavior must not depend on strategy arriving.

### B. Wandering: supported mechanisms and rejected hypotheses

1. **Transient servicing reset is real.** `navigation.py:69-85` includes neighboring occupancy bits in one shared local evidence signature. `policy.py:1158-1177,1652-1656` uses exact signature equality to exclude serviced frontiers. A monster moving near a completed waypoint can reopen it despite unchanged exploration information. Unrelated global-map changes are already excluded; do not propose a global-revision fix.
2. **Unvisited fallback is not indiscriminately revisiting visited cells.** `navigation.py:244-253` offers unvisited targets only when visits are zero and the square is not a frontier. Frontiers can be visited, so frontier reactivation is a more direct issue. Short one-hop commitments can also be legitimate; duration alone is not evidence of thrash.
3. **Anti-backtrack already spans reacquisition.** `policy.py:1187-1188` applies `_antibacktrack`; 1396-1457 uses `recovery.previous_distinct`, which survives stationary confirmations and is not cleared by target acquisition (`recovery.py:291-328`). It is same-family only and keeps reversal when no comparable alternative exists. Held routes correctly skip this comparison. Do not reinstate anti-backtrack on committed routes or claim the reference is universally missing.
4. **Accepted-Jev effect loss**, above, can create turns with no acquired destination, permitting another acquisition decision on the next turn; fix before tuning hysteresis.
5. **Legacy recovery wandering** bypasses commitments and uses random raw-grid motion at >=6 or `_search_fallback`; repair it as part of A.
6. **Cooldown does not switch the policy back to old all-family argmax.** `_navigation_candidates` routes a valid held target or acquires from restricted `explore or unvisited or stair` pools (`policy.py:1042-1103,1163-1177`). Configured cooldown is 50 ticks/5 seconds (`providers.py:126-127`; campaign metadata agrees). Every cap/singleton/unavailable result is returned as low confidence (`controller.py:3043-3074`), which creates noisy low-confidence boundaries even when the scripted choice is deliberate; the artifact samples show this. Separate fallback provenance from actual uncertainty for observability, but treat any scheduling-policy change as independently reviewed.
7. **Confidence regression has measured local explanations, not proven /3 causality.** `ep-1.decisions.jsonl:13,19,21` records p=.600,N=2 and p=.630,N=2 rejected against >.750; p=.480,N=3 rejected against >.500. `arbitration.py:227-231` uses strict p > k/N. Smaller genuine tables raise the required selected probability; singleton bypass is intentional. More context diffusing probability is plausible but unproven without matched-table comparisons. The supplied 32% vs 57% aggregate is not a controlled presentation experiment.

## Design

### 1. Shared bounded recovery

Keep quit/emergency/hunger/mandatory continuation precedence. Below it, use one recovery candidate builder shared by stationary thresholds and cycle recovery. Reuse classified terrain and `navigation.edge_legal`; remove live recovery dependence on `_frontier_target`/`_first_step` and raw `_random_move` (retain legacy helpers only if other verified callers need them).

- At threshold 3: bounded ordinary search only when allowed; refused/exhausted search must proceed to legal recovery, not back into an unchanged failed route.
- At 6: deterministic legal escape, preferring non-reversal when an alternative exists and considering scoped failed edges. Preserve the only legal reversal.
- At 10: escalate within the same bounded machinery, not a separate raw-grid planner or infinite wait/search. No legal/reasonable escape leads to existing forced-search nomination, controller gate evaluation, or bounded graceful trapped quit.
- A zero-time failure of a selected recovery move becomes scoped edge/action failure evidence, so unchanged evidence cannot select it indefinitely. Use existing bounded instance-local ledgers where practical; no global blacklists.
- **Recovery retirement timing.** Retirement ownership is at the **selected/reconciled recovery-effect boundary**: cycle detection may *nominate* recovery, but emergency/maintenance preemption **alone** must not retire a destination or spend destination counters. The selected recovery move's reconciled outcome decides:
  - **Succeeds (moved):** the recovery effect reconciles and retires/suppresses the held destination once when a cycle/stall nominated it; the next acquisition is a fresh decision.
  - **Fails no-time:** the zero-time recovery edge/action becomes scoped edge-failure evidence; the destination is not credited with progress, and the same unchanged edge is not selected forever.
  - **Overridden (pre-send override / local invalid):** nothing is reconciled against this move, so no retirement and no destination counter spend occurs — ownership stays with the action actually sent.
  - **Fails to send (write failure):** no reconciliation, no retirement, no counter spend; the destination remains held for the next decision.
  Only the **selected and reconciled** recovery effect spends destination counters or retires a destination; ordinary inventory, hunger, and isolated emergency interruptions suspend/revalidate rather than consume navigation stalls. Do not attach destination action counts indiscriminately to every recovery/emergency turn.
- Treat unknown hero, prompts and unmatched observations separately; never count an observation twice as an attempted command.

### 2. Exact selected effect and accounting

Controller retains an internal selected-decision record containing the exact candidate, prepared table/need identity, action, and applied token when applicable. Scripted and accepted Jev selection populate it equally. Final substitution/override must explicitly replace or clear it. A delivery repair retains the original logical selection and idempotence identity. Freeze effects only after a complete write; apply only to its matched observation. Mirror this ownership in `evaluate.py` (its existing matching path is at 1192-1196).

Use a frozen pre-send hero/target/evidence baseline for acquisition and continuation. A no-time acquisition attempt must not manufacture progress; a new one-hop success must produce a coherent acquired+terminal lifecycle (or a documented atomic completed-acquisition event counted consistently). Initialize route progress from the hero baseline, not destination position. Preserve compare-and-apply serial/instance/generation checks and never resurrect a serial retired earlier in the observation fold. Carry initial route hop count into the total cap rather than silently using the default cap for all routes.

### 2A. Attempt-counting transition table

Attempt and stationary-stage accounting is defined exactly, keyed by the **frozen pre-send hero** (the confirmed hero square captured before the selected action is sent), the **selected operation** (the payload operation actually sent — `acquire`, `continue`, `interact`, or a non-destination recovery/search/wait move), and the **reconciled outcome** (what the observation fold immediately following that send reports). Only **matched gameplay attempts** — reconciled against the observation that follows their own send — advance the stationary 3/6/10 stage or consume the route/stall budget; prompt, inventory, and unmatched observations do neither.

| Frozen pre-send hero | Selected operation | Reconciled outcome | Stationary stage | Route/stall accounting |
|---|---|---|---|---|
| one hop from target | acquire | moved (arrival satisfies the target) | none | coherent acquired → action → terminal lifecycle with exactly one real attempt; no parked hold installed |
| at the target | acquire | no-time (hero unchanged, display time not advanced) | **attempt 1 of 3** (target installed; no-progress) | route cap charged 1 |
| off the target (new acquisition) | acquire | moved (hero advanced) | not advanced (acquisition does not start the stationary stage) | route cap charged 1; stall progress starts from the **reconciled** hero, not the target position |
| held target | continue | moved from the frozen pre-send hero along the selected legal step | progress (stall reset) | route cap charged 1 |
| held target | continue | no-time / blocked (hero unchanged) | **stall attempt ++** toward the 3-attempt bound | route cap charged 1 |
| held target | continue | hero changed but did not move from the frozen pre-send hero along the selected legal step (only inequality with the target) | **no progress** | counted as no-progress; inequality with the target is not progress |
| at a door approach | interact | door interaction outcome (opened / refused / ineffective / no-time) | door-interaction attempt counted **only** when the selected pre-send action targets the door from an approach square | door bound charged; explicit refusal retires immediately (§3) |
| any | any move that ends adjacent to a door without targeting it | any | **not** a door interaction | no door bound charged |
| any | recovery / search / wait | any | governed by §1 (only a selected and reconciled recovery effect retires; §1) | destination stall spent only if a destination is the target |
| any | any destination payload | prompt / inventory / unmatched observation (no matching window) | not advanced | not charged |

Rules:

1. Only matched gameplay attempts advance the stationary 3/6/10 stage; prompt, inventory, and unmatched observations do not.
2. A no-time acquisition installs the target and counts as no-progress attempt 1 of 3.
3. A moved acquisition counts toward the total route cap but starts stall progress from the reconciled hero (not the target position).
4. A one-hop acquisition emits a coherent acquired → action → terminal lifecycle with one real attempt.
5. Continuation progress requires movement from the frozen pre-send hero along the selected legal step, not merely inequality with the target.
6. A door interaction is counted only when the selected pre-send action targets the door from an approach square.

Initialize route progress from the hero baseline and carry the initial route hop count into the total cap (§2). Named tests: `test_prompt_observations_do_not_advance_stationary_stage`, `test_first_no_time_acquisition_is_attempt_one_of_three`, `test_move_to_door_approach_is_not_an_interaction`.

### 3. One destination terminal owner

Within existing policy/store boundaries, provide one retirement/settlement path that captures `(instance, serial, source, purpose, generation, reason)` before clearing state and emits exactly one terminal destination outcome for **all** sources. Directive settlement is an additional operation, not the only way to emit destination termination. Cover arrival, door opening/refusal/ineffectiveness, stall, recovery/cycle invalidation, unreachable/invalid target, directive expiry/replacement, instance change and episode end. Define replacement and expiration semantics explicitly so metrics do not count them twice. Existing pickup terminal ownership remains authoritative.

#### Lifecycle transition/event table

For each **acquired serial**, exactly one terminal disposition is emitted by the single owner:

| Trigger | Terminal event for the old serial | New serial | Notes |
|---|---|---|---|
| Replacement (default or directive) | terminal with `reason=replaced` and `replacement_serial=<new>` | a distinct acquisition for the new serial | two distinct events in the same fold, old then new; never one combined event |
| Instance change | expired terminal, stable reason `instance_change` | none (state cleared) | emitted **before** store/ledger reset |
| Episode close | expired terminal, stable reason `episode_close` | none | emitted **before** episode state is cleared |
| Directive expiry / precondition failure | expired terminal, stable reason `directive_expired` / `precondition_failed` | none | directive-owned only; never laundered into a default |
| Arrival (frontier/unvisited/door/stair/flee) | terminal, reason `reached` | next acquisition separate | public emission, not only directive settlement |
| Door refusal / ineffective / stall / unreachable / invalid | terminal, reason `refused` / `ineffective` / `stall` / `unreachable` / `invalid` | next acquisition separate | same owner as every other path |
| Cycle / recovery invalidation | terminal, reason `cycle` | none until recovery is reconciled | §1 |

- **One owner.** Unreachable, default, door, cycle, and stall paths all use the same retirement/settlement owner; directive settlement is a **separate, once-only side effect** layered on top and is never the only path that emits termination.
- **Additive schema extension.** Add a `schema_version` field to the lifecycle record (bumped), with backward-compatible readers: records lacking the field are read as version 1, and new fields (`replacement_serial`, the stable `reason` enum) are additive. No field is renamed or removed.
- **`destination_switch_rate` consumes `replaced` events.** A `replaced` terminal followed by a distinct acquisition counts as exactly one switch (one pair), never two; an acquisition that overwrites a serial with no preceding terminal is an unexplained switch and must be flagged. Expired/instance/episode terminals are not switches. Telemetry must not count a replacement twice.

**Exact persisted record shape.** Each lifecycle record keeps the existing outer envelope `schema` field and adds `schema_version` inside the `kind`-tagged payload, e.g. `{"schema": "<envelope>", "schema_version": 2, "kind": "destination", "outcome": "replaced", "serial": OLD, "reason": "replaced", "replacement_serial": NEW}`. `outcome=replaced` is **terminal for completeness accounting** and is the **sole input to replacement switch pairing** (`serial` → `replacement_serial`); no other field substitutes. Readers encountering an old `schema`-only record (no `kind`/`schema_version`/`outcome`) treat it as a **legacy stream**: lifecycle metrics are reported **unavailable rather than zero**.

Named tests: `test_default_and_directive_replacement_emit_terminal_then_acquisition`, `test_directive_expiry_emits_expired_terminal`, `test_instance_transition_emits_expired_terminal_before_reset`, `test_episode_close_emits_expired_terminal`, `test_unreachable_default_retirement_is_visible`, `test_one_hop_atomic_completion_lifecycle`.

### 4. Evidence-scoped progression

Split successful exploration service signatures from failure signatures into **three separate value types/functions**, each with its own reset rule:

- `service_signature(target)`: target-relevant classified terrain / unknown / door / visibility evidence **only** — never occupancy, time, or visits. Reopens a serviced waypoint only on an actual exploration-relevant local change.
- `door_failure_signature(door)`: the target door's terrain plus its target-bound refusal/evidence generation — **never neighboring occupancy**. A wandering nearby monster must not reset an identical closed-door failure.
- `blocked_edge_signature(src, dst)`: the exact failed edge/action, the destination occupancy, and the diagonal side-cell terrain/occupancy needed by `edge_legal`. Reopens only when that specific failed edge/action or its legality-relevant cells change.

- Key the **blockage ledger by instance + edge/action**, not only by the semantic destination, so a different edge to the same destination is independently eligible.
- **Retry/suppression bound and reopening rule:** a failed edge is suppressed for its `blocked_edge_signature` and reopens when that signature changes (the destination or side occupancy clears, or the terrain/legality that `edge_legal` reads changes). `service_signature` and `door_failure_signature` reopen only on their own evidence change and never on unrelated occupancy movement.
- **Door refusal binding (review-raised to High).** Door refusal is consumed **only** from newly observed message IDs/text since the frozen attempt baseline (the message-id/text snapshot captured when the door attempt was armed) and **only** when a matching door serial/action was in flight; old refusal text never fails a new target. Capture the baseline in the selected-decision/attempt record when the exact door candidate is armed, carry it through send and reconciliation, and classify refusal at the matched effect/reconciliation reducer (or pass matched attempt evidence into the observation fold); `state.py` must retain the message IDs `EpisodeMemory.commit` currently discards (`state.py:636-641`). General recent-message scanning must not retire doors. Bind identically in live and evaluator (see §Phase 3).

Named tests: `test_blocked_edge_signature_reopens_when_blocker_leaves`, `test_target_occupant_does_not_reopen_locked_door`, `test_diagonal_side_blocker_is_part_of_edge_signature`, `test_unrelated_occupancy_movement_does_not_reopen_anything`, `test_stale_locked_message_does_not_fail_new_door`, `test_new_locked_message_fails_matching_door_once` (the last two with live/evaluator parity).

Do not immediately add a cooldown timer or larger scoring penalty. First make successful servicing monotonic under unchanged exploration evidence and preserve exact Jev destination selection. If controlled replay still demonstrates near-frontier ping-pong, add a narrowly scoped recent-serviced preference under unchanged evidence, never over explicit directives or sole legal exits. This is a contingent follow-up, not required groundwork.

### 5. Diagnostics

Add backward-compatible decision fields for selected semantic label/reason, recovery stage, reconciliation kind, held serial before/after, stall/door counters, and retirement reason. Keep provider fallback reason separately, so `cap reached` does not hide `unblock`. Add selected probability/N/threshold and consultation outcome category to reporting without altering Choice wire. Do not dump raw provider state or change DeepSeek prompt/history for these diagnostics.

## Ordered phases, acceptance criteria and named tests

All names below are proposed tests in verified existing suites; implementer must execute them. No test execution is claimed here.

### Phase 0 — Capture failure and audit measurements

Files: `test/agent/test_auto_integration.py`, `test_auto_recovery.py`, `test_auto_commitment.py`, `test_auto_metrics.py`; proposed small fixture under `test/agent/` extracted from the relevant campaign state, not the full large recording.

**AC1:** Reproduce capped fallback with hero `(64,4)`, locked east door, adjacent west monster and no-time responses; assert actual candidate/stage and held state rather than inferring them from lifecycle absence.
- `test_cap_exhausted_locked_door_adjacent_monster_enters_bounded_recovery`
- `test_default_locked_door_retirement_is_visible`

**AC2:** Establish metrics completeness failures and accepted-Jev non-scripted effect loss.
- `test_jev_non_scripted_choice_preserves_destination_effect`
- `test_default_destination_terminal_accounting_complete`

Exit: failures explain the intended fixes; record source/config identity and distinguish old incomplete telemetry from regenerated results.

### Phase 1 — Selection and attempt accounting

Files: `tools/agent/controller.py`, `evaluate.py`, `policy.py`, `navigation.py`; tests in commitment, wiring, replay, pickup and gate-nav-pin suites.

**AC3:** All provider/fallback modes preserve the selected candidate effect; local-invalid, write failure, override, late/unselected result and stale continuation mutate nothing. Repair counts once; applied cap charges only accepted Jev decisions successfully sent, never scripted/forced fallback.
- `test_selected_candidate_effect_survives_all_fallback_reasons`
- `test_jev_accepts_different_action_non_scripted_candidate_and_commits_its_exact_payload`
- `test_stale_candidate_same_wire_from_other_table_is_rejected_by_identity`
- `test_override_does_not_commit_discarded_destination`
- `test_write_failure_commits_no_selected_destination_or_recovery_effect`
- `test_delivery_repair_preserves_effect_and_applied_token_once`

The two identity tests replace the former single same-wire test: action deduplication is unchanged and no duplicate Choice members are added. The former same-wire test's intent — that a non-scripted Jev action commits its **exact** member payload — is split into (i) acceptance of a different-action non-scripted candidate committing that exact payload, and (ii) rejection of a stale same-wire candidate that belongs to another prepared table, matched by identity rather than wire equality. The forced-override coincidence test (`test_override_does_not_commit_discarded_destination`) remains separate.

**AC4:** A blocked held destination retires after no more than three reconciled no-progress navigation attempts, including correct acquisition baseline; explicit locked refusal retires immediately; two actual ineffective door interactions retire under unchanged evidence. Zero-time does not reset the budget, and prompts/maintenance do not consume it.
- `test_cap_exhausted_held_destination_retires_at_three_zero_time_attempts`
- `test_acquisition_no_time_is_not_progress`
- `test_first_no_time_acquisition_is_attempt_one_of_three`
- `test_prompt_observations_do_not_advance_stationary_stage`
- `test_move_to_door_approach_is_not_an_interaction`
- `test_door_interaction_bound_counts_sent_interactions_only`
- `test_long_route_cap_uses_initial_hops`
- `test_live_replay_selected_effect_parity`

### Phase 2 — Bounded recovery and terminal telemetry

Files: policy/navigation/recovery/controller/evaluate/lifecycle_metrics plus recovery, forced-search, metrics and integration suites.

**AC5:** Thresholds 3/6/10 all reach bounded recovery; no legacy edge bypass; independent cycles recover even when stationary thresholds also apply. No unchanged failed edge is selected forever. A blocked fixture either moves legally, makes an allowed bounded search attempt, or terminates gracefully within the fixture's explicitly enumerated attempt bound, well before tick cap.
- `test_stationary_thresholds_3_6_10_share_legal_bounded_recovery`
- `test_np10_recovery_never_routes_through_locked_door`
- `test_cycle_with_stationary_count_does_not_fall_back_into_navigation`
- `test_recovery_no_time_edge_failure_is_suppressed`
- `test_recovery_only_legal_reverse_is_not_trapped`
- `test_no_alternative_uses_forced_search_then_trapped_quit`
- `test_episode2_three_prefix_cap_and_trapped_reason_preserved`

**AC6:** Every acquired destination has one coherent replacement/terminal/episode-end disposition; default and directive reporting agree; failed earlier-fold serials never resurrect. Ordinary emergencies suspend, actual cycle/stall recovery retires.
- `test_default_arrival_stall_locked_cycle_and_instance_emit_terminal_once`
- `test_one_hop_acquisition_has_coherent_lifecycle`
- `test_one_hop_atomic_completion_lifecycle`
- `test_default_and_directive_replacement_emit_terminal_then_acquisition`
- `test_directive_expiry_emits_expired_terminal`
- `test_instance_transition_emits_expired_terminal_before_reset`
- `test_episode_close_emits_expired_terminal`
- `test_unreachable_default_retirement_is_visible`
- `test_earlier_fold_retirement_cannot_be_reinstalled`
- `test_emergency_suspension_does_not_spend_destination_stall`: with an already-active cycle/stationary stage, an emergency/maintenance preemption **alone** emits no terminal event and spends no destination counter; a terminal is emitted only once a recovery candidate is actually selected and reconciled.

### Phase 3 — Evidence-stable progression

Files: `navigation.py`, `policy.py`, `state.py`, `controller.py`, `evaluate.py`, and the selected-decision/attempt record; tests in navigation, commitment and recovery suites.

**AC7:** Serviced frontiers remain suppressed through neighboring creature movement, hero overlay, time, visits and unrelated map discovery; a genuine local exploration change can reopen. Blockage removal can reopen a blocked route; unrelated occupancy cannot reopen a locked door. With unchanged serviced frontiers exhausted, unvisited cells then stairs progress according to existing ordering.
- `test_serviced_frontier_ignores_transient_neighbor_occupancy`
- `test_frontier_reopens_only_for_local_exploration_change`
- `test_failed_route_reopens_after_blocker_leaves`
- `test_blocked_edge_signature_reopens_when_blocker_leaves`
- `test_diagonal_side_blocker_is_part_of_edge_signature`
- `test_unrelated_occupancy_movement_does_not_reopen_anything`
- `test_target_occupant_does_not_reopen_locked_door`
- `test_locked_door_not_reenabled_by_neighbor_monster_motion`
- `test_stale_locked_message_does_not_fail_new_door`
- `test_new_locked_message_fails_matching_door_once`
- `test_serviced_frontiers_progress_to_unvisited_then_stairs`
- `test_reacquisition_preserves_previous_distinct_and_strict_margin`
- `test_committed_reverse_survives_reacquisition_preferences`

**Door-refusal seam (explicit Phase 3 substep; §4).** Bind refusal to the exact attempt rather than to general recent-message scanning:

1. Capture the message-ID/text baseline when the exact door candidate is **armed**; the selected-decision/attempt record must carry it.
2. Carry that baseline with the selected attempt through send and reconciliation.
3. Classify refusal at the **matched effect/reconciliation reducer** (or pass the matched attempt evidence into the observation fold), not by rescanning recent messages.
4. `state.py` must retain the message IDs it currently discards: `EpisodeMemory.commit` drops IDs at `state.py:636-641`, so the baseline cannot be compared until that is fixed.
5. Mirror the exact path in the evaluator (its matching path is at `evaluate.py:1192-1196`).

Preserve the established reconciliation ordering (reconcile the observation → commit memory → `note_observation` → `commit_effect`); general recent-message scanning must **not** retire doors.

### Phase 4 — Verification, review and controlled campaign

**AC8:** Existing Choice, cache, emergency, directives, pickup, reservation/cancellation and applied-cap suites pass without weakening their assertions. Existing verified suites include `test_auto_candidates.py`, `test_auto_providers.py`, `test_auto_jev_presentation.py`, `test_auto_gate_nav_pin.py`, `test_auto_pickup.py`, `test_auto_wiring.py`, `test_auto_replay.py` and integration suites.

**AC9:** Report acceptance/rejection/timeout separately by N, phase and provider outcome, plus attempts/time advances, maximum stationary span, terminal completeness, serviced-site reopens, coverage and revisits excluding teardown. Re-run paired seeded campaigns only with caller approval. Safety acceptance is the deterministic bounded-loop tests; coverage/revisit improvement is a campaign comparison, not a fabricated universal target. Do not demand a return to 57% acceptance without matched-table evidence.
- `test_decision_diagnostics_keep_cap_reason_and_scripted_reason`
- `test_confidence_report_stratifies_binary_ternary_singleton_timeout`: strengthened to enumerate accepted / rejected / skipped-singleton / cap-unavailable / timeout outcomes by N and phase (not merely binary-vs-ternary), so the report cannot conflate a deliberate singleton bypass or cap fallback with genuine rejection.
- `test_report_attempts_vs_time_advances_and_stationary_span`
- `test_report_terminal_completeness_and_serviced_reopens`
- `test_report_coverage_excludes_teardown`
- `test_lifecycle_metrics_flag_incomplete_legacy_streams`

## AC → named-test map

Each acceptance criterion's named tests; a mutation that regresses an AC must fail at least one of them.

| AC | Named tests |
|---|---|
| AC1 | `test_cap_exhausted_locked_door_adjacent_monster_enters_bounded_recovery`, `test_default_locked_door_retirement_is_visible` |
| AC2 | `test_jev_non_scripted_choice_preserves_destination_effect`, `test_default_destination_terminal_accounting_complete` |
| AC3 | `test_selected_candidate_effect_survives_all_fallback_reasons`, `test_jev_accepts_different_action_non_scripted_candidate_and_commits_its_exact_payload`, `test_stale_candidate_same_wire_from_other_table_is_rejected_by_identity`, `test_override_does_not_commit_discarded_destination`, `test_write_failure_commits_no_selected_destination_or_recovery_effect`, `test_delivery_repair_preserves_effect_and_applied_token_once`, `test_delivery_repair_does_not_double_consume_pickup_attempt` |
| AC4 | `test_cap_exhausted_held_destination_retires_at_three_zero_time_attempts`, `test_acquisition_no_time_is_not_progress`, `test_first_no_time_acquisition_is_attempt_one_of_three`, `test_prompt_observations_do_not_advance_stationary_stage`, `test_move_to_door_approach_is_not_an_interaction`, `test_door_interaction_bound_counts_sent_interactions_only`, `test_new_locked_message_fails_matching_door_once`, `test_stale_locked_message_does_not_fail_new_door`, `test_long_route_cap_uses_initial_hops`, `test_live_replay_selected_effect_parity` |
| AC5 | `test_stationary_thresholds_3_6_10_share_legal_bounded_recovery`, `test_np10_recovery_never_routes_through_locked_door`, `test_cycle_with_stationary_count_does_not_fall_back_into_navigation`, `test_recovery_no_time_edge_failure_is_suppressed`, `test_recovery_only_legal_reverse_is_not_trapped`, `test_no_alternative_uses_forced_search_then_trapped_quit`, `test_episode2_three_prefix_cap_and_trapped_reason_preserved` |
| AC6 | `test_default_arrival_stall_locked_cycle_and_instance_emit_terminal_once`, `test_one_hop_acquisition_has_coherent_lifecycle`, `test_one_hop_atomic_completion_lifecycle`, `test_default_and_directive_replacement_emit_terminal_then_acquisition`, `test_directive_expiry_emits_expired_terminal`, `test_instance_transition_emits_expired_terminal_before_reset`, `test_episode_close_emits_expired_terminal`, `test_unreachable_default_retirement_is_visible`, `test_earlier_fold_retirement_cannot_be_reinstalled`, `test_emergency_suspension_does_not_spend_destination_stall` |
| AC7 | `test_serviced_frontier_ignores_transient_neighbor_occupancy`, `test_frontier_reopens_only_for_local_exploration_change`, `test_failed_route_reopens_after_blocker_leaves`, `test_blocked_edge_signature_reopens_when_blocker_leaves`, `test_diagonal_side_blocker_is_part_of_edge_signature`, `test_unrelated_occupancy_movement_does_not_reopen_anything`, `test_target_occupant_does_not_reopen_locked_door`, `test_locked_door_not_reenabled_by_neighbor_monster_motion`, `test_stale_locked_message_does_not_fail_new_door`, `test_new_locked_message_fails_matching_door_once`, `test_serviced_frontiers_progress_to_unvisited_then_stairs`, `test_reacquisition_preserves_previous_distinct_and_strict_margin`, `test_committed_reverse_survives_reacquisition_preferences` |
| AC8 | `TestJevRoomAwareness::test_criteria_keys_order_indices_and_option_count_unchanged`, `test_strategy_historical_bytes_not_rerendered_after_commitment_change`, `test_emergency_singleton_precedes_destination_application`, `test_delivery_repair_does_not_double_consume_pickup_attempt`, `test_jev_cap_stops_after_exactly_c_complete_applied_sends`, plus the existing reservation/cancellation tests `test_a_cache_price_never_lowers_a_reservation`, `test_history_reflects_only_successful_settlement`, `test_cancellation_adds_no_history` |
| AC9 | `test_decision_diagnostics_keep_cap_reason_and_scripted_reason`, `test_confidence_report_stratifies_binary_ternary_singleton_timeout` (strengthened to enumerate accepted/rejected/skipped-singleton/cap-unavailable/timeout by N and phase), `test_report_attempts_vs_time_advances_and_stationary_span`, `test_report_terminal_completeness_and_serviced_reopens`, `test_report_coverage_excludes_teardown`, `test_lifecycle_metrics_flag_incomplete_legacy_streams` |

## Mutation checks

Each mutation must be killed by a named regression, not merely reported as executed:
- Restore >=10 legacy `_unblock` routing → `test_np10_recovery_never_routes_through_locked_door`.
- Gate no-progress on time advancing → `test_cap_exhausted_held_destination_retires_at_three_zero_time_attempts`, `test_acquisition_no_time_is_not_progress`, `test_first_no_time_acquisition_is_attempt_one_of_three`, `test_stationary_thresholds_3_6_10_share_legal_bounded_recovery`.
- Change stall `>=` to `>` or seed progress at target → `test_first_no_time_acquisition_is_attempt_one_of_three`, `test_cap_exhausted_held_destination_retires_at_three_zero_time_attempts`, `test_long_route_cap_uses_initial_hops`.
- Omit acquisition attempt or clear no-time stalls → `test_acquisition_no_time_is_not_progress`.
- Drop accepted Jev candidate payload/use scripted winner → `test_selected_candidate_effect_survives_all_fallback_reasons`, `test_jev_accepts_different_action_non_scripted_candidate_and_commits_its_exact_payload`, `test_stale_candidate_same_wire_from_other_table_is_rejected_by_identity`.
- Commit before write / retain discarded override payload → `test_write_failure_commits_no_selected_destination_or_recovery_effect`, `test_override_does_not_commit_discarded_destination`.
- Ignore serial/instance guard or reinstall a retired serial → `test_earlier_fold_retirement_cannot_be_reinstalled`.
- Count delivery repair twice or charge scripted fallback → `test_delivery_repair_preserves_effect_and_applied_token_once`, `test_delivery_repair_does_not_double_consume_pickup_attempt`.
- Restore raw passable recovery or unbounded search/wait → `test_stationary_thresholds_3_6_10_share_legal_bounded_recovery`, `test_np10_recovery_never_routes_through_locked_door`, `test_recovery_no_time_edge_failure_is_suppressed`, `test_recovery_only_legal_reverse_is_not_trapped`, `test_no_alternative_uses_forced_search_then_trapped_quit`.
- Emit terminals only for directives / emit twice → `test_default_arrival_stall_locked_cycle_and_instance_emit_terminal_once`, `test_default_and_directive_replacement_emit_terminal_then_acquisition`, `test_directive_expiry_emits_expired_terminal`, `test_instance_transition_emits_expired_terminal_before_reset`, `test_episode_close_emits_expired_terminal`, `test_unreachable_default_retirement_is_visible`, `test_one_hop_atomic_completion_lifecycle`.
- Put occupancy back into successful frontier servicing or ignore it for blocked-route failure → `test_serviced_frontier_ignores_transient_neighbor_occupancy`, `test_frontier_reopens_only_for_local_exploration_change`, `test_blocked_edge_signature_reopens_when_blocker_leaves`, `test_diagonal_side_blocker_is_part_of_edge_signature`, `test_unrelated_occupancy_movement_does_not_reopen_anything`, `test_target_occupant_does_not_reopen_locked_door`, `test_locked_door_not_reenabled_by_neighbor_monster_motion`.
- Apply anti-backtrack to held routes or reject sole reverse → `test_committed_reverse_survives_reacquisition_preferences`, `test_reacquisition_preserves_previous_distinct_and_strict_margin`.
- Change strict relative gate boundary, reorder Choice criteria, or rerender historical strategy bytes → `TestJevRoomAwareness::test_criteria_keys_order_indices_and_option_count_unchanged`, `test_strategy_historical_bytes_not_rerendered_after_commitment_change`, `test_emergency_singleton_precedes_destination_application`.

## Review and documentation

Update `doc/agent-destination-commitment-plan.md` with actual terminal event ownership, pre-send baseline/attempt semantics, evidence-specific servicing, and recovery versus suspension rules. Update `doc/agent-jev-gate-nav-plan.md` to explicitly supersede the unsafe stationary-semantics preservation clause at line 185, documenting shared legal recovery without changing strict >40 uncommitted anti-backtrack or p>k/N. Preserve `doc/agent-jev-presentation-plan.md` contract; note additive diagnostics, not a presentation redesign.

Maintain Choice criteria as an insertion-ordered object, exact retained-index mapping and N, strict response parser, singleton/unsupported bypass, no distractor options. Preserve DeepSeek static system prompt, fixed block ordering/final budget line, byte-frozen historical turns, transactional reservations/history settlement/cancellation. No cache renderer changes are needed. If a later controlled presentation experiment is approved, version it separately and retain these invariants.

Have reviewers inspect selection/send/reconcile ownership first, then recovery safety, then lifecycle/evidence changes. Implementer should report exact commands and outcomes for repository suites and native pickup regression coverage where affected. Keep bug-fix phases separable for rollback; do not roll back bounded recovery merely because an optional progression preference harms coverage.

**Post-implementation review loop (required).** After all automated tests and mutation checks pass, dispatch the prescribed reviewer against the **actual diff** and AC1–AC9 plus the inherited contracts (Choice/cache, providers, pickup, reservation/cancellation, applied cap, and the gate/nav and commitment plans). The **execute agent** must fix or explicitly rebut every reviewer finding, rerun affected tests, and redispatch review after Critical/Important fixes, repeating until none remain or an operator decision is required. This mirrors the loop prescribed in `doc/agent-jev-gate-nav-plan.md` §10 and `doc/agent-destination-commitment-plan.md` §9.

### Contract migration checklist

Each existing test/fixture whose committed contract changes, with old vs new expectation; implementers add any further entry discovered during implementation:

- `test/agent/test_auto_recovery.py::test_stationary_recovery_ladder_3_6_10_unchanged` (`test_auto_recovery.py:302-317`): **old** only checks that all three thresholds (3/6/10) return the recovery family — insufficient mechanism-level coverage, since it does **not** pin the raw-grid exit behavior or any specific recovery mechanism; **new** rename/rewrite it to assert the **preserved threshold values (3/6/10)** and **emergency precedence** while explicitly expecting the **new shared legal bounded builder** and **attempt-owned semantics** — the thresholds stay correct, only the expected recovery mechanism changes.
- Same-wire identity fixtures in the wiring/replay suites: the single same-wire identity expectation is replaced by the two identity tests (§Phase 1, AC3), with action deduplication and Choice membership unchanged.
- Gate/nav plan clause and code comments: during the documentation phase, update the `doc/agent-jev-gate-nav-plan.md` stationary-semantics preservation clause (line 185) and the corresponding source code comments in `tools/agent/policy.py` (the 3/6/10 ladder and the `_unblock`/`_random_move` exits) to describe the shared legal bounded recovery and attempt-owned counting, without changing strict >40 anti-backtrack or p>k/N.

### Implemented contract-migration entries (discovered during execution)

- `test/agent/test_auto_replay.py::ShortFixtureTest::test_ground_truth_agreement_is_total` and `ProviderCompareTest::test_jev_offline_is_a_scripted_fallback`: **old** agreed-count bound `needs_answered - 6` (destination-commitment baseline) / rate `0.87`; **new** the shared legal bounded recovery builder adds exactly one further *navigation* divergence, so the bound is the **measured** `needs_answered - 7` (agree 40 of 47, rate 0.851).  The single added divergence is the short fixture's need index 12 (`fixtures/auto/short.decisions.jsonl`): the shared builder selects `'y'` (north-west, reason `loop breaker: bounded escape (>=6)`) where the recording holds `'k'` (north).  The corpus is an input recording, not a golden file; regenerating it is the operator-gated step.
- `test/agent/test_auto_commitment.py::DefaultDestinationPool::test_unvisited_fallback_after_serviced_frontiers` and `CommittedBehaviour::test_serviced_frontier_is_suppressed_then_reacquirable` / `test_locked_door_fails_once_and_next_target_progresses`: the serviced/failed suppression now uses the **split** evidence signatures (`service_signature` for successful servicing, `door_failure_signature` for a closed door), so the fixtures store the split signature rather than the legacy `local_evidence_signature`.
- `test/agent/mutation_checks.py::mutation_apply_antibacktrack_to_committed_target`: anchor updated to the new `_dest_payload("continue", held, step=step)` continuation line.

## Risks, alternatives and non-goals

- A blanket counter on all stationary observations would count prompts, inventory and harmless interruptions. Prefer matched gameplay-attempt accounting while retaining existing observation position evidence.
- Merely adding `>=` is insufficient: it already exists, and the repeated recovery actions never reach destination counters.
- Merely restoring destination payloads on capped fallback is insufficient: capped scripted candidates already retain them when appropriate; the loop is in recovery. Exact selection ownership is still required for accepted Jev and repairs.
- Globally suppressing sites forever prevents legitimate exploration after terrain changes. Use purpose-specific bounded local evidence.
- Lowering confidence or shortening strategy cooldown can spend more and conceal unsafe fallback. Defer until matched-table diagnostics justify an independent change.
- An external repeated-frame watchdog is a useful optional last-resort containment, not the primary fix; it must not turn valid prompt sequences or legal repeated search into false traps.
- Do not add combat, automatic kicking/unlocking, unknown-space traversal, arbitrary item pickup, ascent under flee advice, a planner service, or dependencies. Do not redesign scoring or force longer commitments solely to improve a metric.
- Historical event streams are incomplete and cannot prove past held-state lifetimes. Mark legacy metrics uncertain; do not silently rewrite them as corrected evidence.

## Reviewer scrutiny / remaining uncertainty

1. Demand a fixture showing the selected recovery label for the episode-4 geometry. Wire evidence and source strongly support `_unblock`; current logs alone do not prove the branch or serial-73 lifetime.
2. Verify the store actually clears on `This door is locked.` before any stale continuation, and public telemetry records that fact exactly once.
3. Ensure the selected immutable candidate—not a reconstructed action match—is authoritative through Jev acceptance, final override and delivery repair. Same action does not necessarily mean same destination effect.
4. Verify implementation matches §2A exactly. Three no-progress attempts means three real matched gameplay attempts, not four due to an incorrect progress baseline; two door interactions means actual door-targeting interactions, not merely ending a move adjacent to a door.
5. Do not let recovery retirement spend budgets on isolated emergency/maintenance suspension; do not let suspended targets survive an actual hard refusal or cycle indefinitely.
6. Verify every recovery edge with persistent classified terrain/current occupancy and retain the only legal reversal. Ensure dangerous forced search still has all controller gates and its three-activation cap.
7. Verify §4's mandatory matched-attempt binding. Door refusal scans recent messages today rather than a target-bound attempt; the implementation must consume refusal only from the message-ID/text baseline captured when the exact door candidate was armed and only when a matching door serial/action was in flight, so old refusal text never fails a newly acquired different door.
8. Inspect live/evaluator parity and pickup send/repair ownership; this patch must not move pickup initiation back to post-observation inventory state.
9. Reject conclusions that /3 context caused probability diffusion without controlled matched-N/table evidence; sampled rejections show valid strict-threshold behavior.
10. Campaign aggregate metrics, terminal counts and apparent low switch rates need regenerated, complete lifecycle reporting. Episode 2 is observed graceful exhaustion, not established death.
