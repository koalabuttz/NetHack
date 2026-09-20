# Prompt-declined edge learning plan (Revision 2 — DRAFT, pending plan review)

> **Round-1 plan-review provenance.** Review verdict: **REVISE — 3 High + 2 Medium + 1 Low, all addressed**; the root-cause audit is confirmed. Changes: (1) a pure `movement_origin_from_selected(candidate, need_kind, source_instance, pre_hero, attempt_key)` transport seam with an explicit accepted/rejected operation-class taxonomy, an optional internal-only `matched_movement_prompt`/`prompt_origin` field on `providers.ReflexContext` populated in live and evaluator, `providers.py` added to Phase 1's files, and a parameterized operation-taxonomy test (§A, §Phases Phase 1, AC2); (2) explicit replacement of `_escape`'s raw-grid movement selection/routing with classified `TerrainMemory` + `navigation.edge_legal` + the same immutable blocked-edge view, with a defined alternate-legal-edge fallback and four named tests (§E, §Phases Phase 3, AC4/AC5); (3) an executable Phase 0 cloud-encoding gate — a native `make -C test/agent native-cloud` probe or an explicit native-vs-operator-manual disposition — with a rewritten Phase 0 exit and a disposition test (§Phases Phase 0, §D, Risks, AC5); (4) an exact append-only/keyword-only edge-predicate API and predicate contract with five unit tests (§E, AC5); (5) an AC2 hero-progress suppression test plus its killing mutation (§Acceptance Criteria, §Mutation checks); (6) normalized plan headings, a compact AC1–AC5 → exact named-test table under Test Strategy, and the Phase 0 "only remaining gate" statement. Sections not named here are retained verbatim from the draft.

Design produced by `architect:architect-vapor-loop` from the live-campaign freeze diagnosis ("Step into that vapor cloud?" answered 'n' 1,484×). Follow-up to `doc/agent-stall-recovery-plan.md` (§2A matched-attempt accounting and blocked-edge ledger), `doc/agent-destination-commitment-plan.md`, `doc/agent-jev-gate-nav-plan.md`.

## Goal

Fix the vapor-cloud freeze with a narrow, conservative extension of matched-attempt accounting and the existing blocked-edge ledger. Count a matched movement whose immediate response is a blocking confirmation, carry its exact edge identity through the answer, learn from a confirmed decline, and exclude that edge throughout route planning under unchanged evidence. Continue answering `n`; enabling deliberate cloud entry or Jev at prompts is a separate feature.

This requires three linked changes, not just extending a need-kind list: (1) accounting, (2) decline evidence, and (3) graph-wide use of that evidence. Destination retirement already bounds individual commitments; the defect survives by reacquiring different destinations through an unlearned edge.

Read-only investigation only: no files modified, commands executed, tests run, dependencies installed, or work delegated. Campaign counts and behavior are evidence supplied by the caller, not independently replayed.

## Implementation Summary

1. Freeze movement intent and originating need/candidate identity at successful gameplay send; derive a shared, immutable matched-movement-prompt context before reconciliation discards the SentAttempt.
2. Extend stationary eligibility narrowly: existing matched gameplay responses remain eligible; a `yn` response additionally qualifies only when bound to a successfully sent movement operation and a recognized blocking movement confirmation. Count at prompt arrival, once per originating attempt; never count its answer again.
3. Preserve one bounded pending prompt context until its actual answer is sent and reconciled. On confirmed `n` plus unchanged confirmed hero/scope, record `prompt-declined` evidence for the originating directed edge, not for the answer action and not for the semantic destination.
4. Extend the existing ledger to typed failure evidence, including prompt text and edge-local observable cloud/overlay evidence. Feed an observational blocked-edge predicate into both Dijkstra expansion loops and all ordinary movement/recovery paths.
5. Reroute held destinations when possible. Otherwise use the existing single terminal owner and bounded search/recovery pipeline; do not poison the destination's long-lived service signature merely because one route is prompt-blocked.
6. Implement matching live/evaluator transitions, regression fixtures, mutation checks, and §2A documentation amendments together.

## Verified facts

### Caller-supplied operational evidence

Episode 4 was interrupted at 3,296 actions. The decision tail alternates east (`key:108`) and decline (`yn:110`); the prompt appeared 1,484 times. Destinations acquired, failed/stalled, and retired normally, while replacement routes crossed the same edge. No stationary 3/6/10 recovery activation was observed. Consistent with the inspected paths but not an independently validated campaign replay.

### Source-verified facts

- `tools/agent/controller.py:1399-1413`: reconcile occurs before memory commit; `advance_stationary` requires `_matched_gameplay_attempt` AND response kind in `("command", "key", "direction")`. `tools/agent/evaluate.py:639-642` has the same restriction using `_matched_gameplay`.
- `controller.py:1500-1520`: the pending SentAttempt is classified and cleared on its first observation, including a prompt. Its frozen effect is committed after memory commit at `1425-1430`. Consequently, movement destination accounting can run on the prompt while the stationary counter is skipped.
- `controller.py:2939-2960`: only command/key/direction sends arm gameplay attempts. A noncommand answer freezes only its effect. Therefore the subsequent command frame following `n` has no matching movement attempt to advance stationary accounting.
- `controller.py:1692-1735`: successful send freezes selected candidate/effect/payload, hero/time baseline, need/table/candidate identity and ordinal. `tools/agent/candidates.py:606-659` defines SentAttempt and its primary key `(need_key, table_id, candidate_id, sent_ordinal)`. It does not itself retain the entire candidate/direction; action and expected effect are available, and controller-held payloads carry destination direction.
- `controller.py:1584-1610`: movement evidence uses the matched action to obtain a plain directional-key delta. Once the attempt is released, this is not movement identity attached to the later `yn` answer.
- `tools/agent/policy.py:1078-1115`: `_yn` has no general identical-prompt counter. It handles pickup, quit/save, eating, then native default/visible choices. Generic answers propose only `prompt`. The default currently precedes conservative visible-choice selection, so an explicit recognized hazard-decline branch should precede that default.
- `policy.py:1117-1137` contains an eat-specific loop breaker, not a movement-confirmation breaker. No generic repeated-movement-prompt suppression was found in the inspected policy/controller paths.
- `policy.py:652-665,682-702`: blocked_edges is keyed by `(instance,src,dst)`, compares `blocked_edge_signature`, and is written by selected recovery outcomes. Despite comments emphasizing zero-time, the implementation also records `stationary-time-advanced` recovery failures. It is cleared on new instances at `2173-2174`.
- `policy.py:736-875`: destination acquisition/continuation accounts attempted navigation but does not write blocked-edge evidence. Acquisition without hero movement is attempt one. `policy.py:914-921` retires a stalled destination; `tools/agent/navigation.py:622-643` increments stall/total attempts, resets stall on position progress, and enforces the stall/route caps. The per-destination bound is therefore real, but is not cross-destination edge learning.
- `policy.py:1251-1273`: navigation computes a plan, routes a held destination, or elects reachable targets. No blocked-edge predicate is passed. `navigation.py:186-231` checks `edge_legal` at seed and interior expansions but not the policy ledger. Existing ledger checks appear in recovery (`policy.py:1832-1837`) and alternate movement (`2367-2369`), not Dijkstra.
- `navigation.py:114-132`: current edge signature includes destination terrain/occupancy and diagonal side terrain/occupancy. It lacks prompt or cloud-overlay evidence.
- `tools/agent/instances.py:229-272`: TerrainMemory persists classified terrain and replaces occupancy; unclassified overlays do not necessarily replace known terrain. Therefore classified terrain alone cannot be assumed to reveal cloud disappearance.
- `policy.py:1783-1812,1851-1879`: recovery chooses a legal edge, then budgeted ordinary search, then existing forced-search gates or graceful quit. A cloud refusal must not become authorization to use a dangerous movement prefix; preserve those gates.
- `policy.py:2017-2023`: unreachable held destinations use a frozen failure candidate carried on a search action. Audit its accounting for this new path rather than multiplying unbudgeted unreachable-search actions across reacquisitions.
- `src/hack.c:2527-2548`: paranoid movement checks a visible region and viable movement before asking `%s into that %s cloud?`; vapor vs poison gas is chosen from region damage. Decline clears movement and returns without applying it. `src/monmove.c:656-661` creates harmless vapor for fog clouds; `src/region.c:1189-1194` distinguishes damage-zero vapor from poison-cloud glyphs, and `1091-1125` implements gas harm. Thus this exact vapor label is not evidence that all clouds are damaging, but avoiding entry is safe and intentionally conservative. The engine explicitly also cites impaired vision as a reason to confirm (`hack.c:2539-2540`).

### Root-cause conclusion

Hypotheses 1–3 are confirmed for the inspected paths: movement is matched and consumed on prompt arrival; its response kind suppresses stationary accounting; decline lacks an originating-edge effect. In addition, even a new ledger writer would be insufficient without Dijkstra integration. Hypothesis 4: existing specialized eating/forced-search bounds do not cover this loop; no general movement-prompt suppression was found in inspected paths.

## Design

### A. Ownership and narrow movement identity

Keep send/reconcile ownership in controller/evaluator; do not mutate policy state during candidate preparation. Put shared pure eligibility/context construction helpers alongside existing reconciliation helpers in `tools/agent/arbitration.py`; put an immutable DTO in `candidates.py` if needed. Names below are interface sketches, not required API names.

At successful send, freeze enough information to distinguish a movement operation from a raw direction-shaped answer:

`MovementOrigin = {attempt_primary_key, origin_need_kind, instance, confirmed_src, delta, candidate_id, proposed_effect, operation/payload, expected_destination_serial}`.

The operation must actually mean movement. A direction response to opening a door, kicking, zapping, or another interaction is NOT movement merely because it contains east. Existing exact selected-candidate/effect identity remains authoritative. Support genuine movement sent under command/key/direction needs, but a direction continuation needs explicit movement intent; do not broaden `direction_delta` indiscriminately. Unknown/synthetic intent should fail closed for edge learning.

**MovementOrigin transport seam (pure contract).** Derive movement identity through exactly one pure function, shared by live and evaluator:

`movement_origin_from_selected(candidate, need_kind, source_instance, pre_hero, attempt_key) -> MovementOrigin | None`

It is total, side-effect-free, and depends only on its arguments plus immutable candidate/effect identity; it never reads mutable policy or engine state.

Accepted **operation classes** (return a `MovementOrigin`):
- destination `acquire` movement — a selected step that acquires a default/directive destination;
- destination `continue` movement — a selected step continuing a held destination;
- recovery-step movement — a selected recovery move that is a real step;
- directional emergency escape — a selected emergency escape expressed as a real step.

Rejected **operation classes** (return `None`; fail closed):
- door interaction (including a direction-shaped open/unlock/kick);
- movement prefix (e.g. an `m`-prefixed movement);
- stair traversal / level transition;
- wait, search, or forced-search actions;
- direction continuations **without** an explicit movement class;
- synthesized or effect-less candidates (no frozen movement payload).

Freeze the result in **both** live and evaluator at **successful gameplay send**, before reconciliation discards the SentAttempt. Add an optional, internal-only `matched_movement_prompt`/`prompt_origin` field to `providers.ReflexContext`, populated in both `controller._reflex_context` and evaluator context construction. It is **policy-input only**: presentation and `providers` never consume or render it, and it never affects the Choice/prompt/wire shape. A parameterized test covers every row above (accepted vs rejected): `test_movement_origin_operation_taxonomy_matrix`.

On the immediate validated response, before clearing SentAttempt, derive `MatchedMovementPrompt` only if:
- originating send completed and this observation matches its attempt;
- originating need was a gameplay need and selected operation was movement;
- response is `yn` with a recognized movement-entry confirmation (start with source-verified cloud confirmations; extend only with source/fixture evidence);
- instance and frozen/reconciled hero are coherent; confirmed same hero is required to call it stationary or learn a declined edge;
- prompt text comes from the current need, never a scan of recent messages.

Do not use the semantic destination to reconstruct the attempted edge: it may be several steps away. Compute dst from frozen src plus selected delta.

### B. §2A composition and exactly-once counting

Recommended counting boundary is prompt arrival. It is already the first reconciled response to the gameplay attempt and already commits its destination effect. Extend the predicate conceptually to:

`matched_gameplay AND (existing_gameplay_response_kind OR matched_movement_confirmation)`.

The second term is an identity-bound result, not simply `frame_kind == "yn"`. Pass the resulting boolean to EpisodeMemory.commit; preserve its existing unknown-hero and progress reset behavior. Record the source attempt as accounted in the one pending prompt context.

Transition composition:
- Matched movement → blocking yn, unchanged confirmed hero: stationary +1; existing acquisition/continuation route/stall charge exactly once; create pending prompt context.
- Same prompt re-presentation or unrelated observation: no extra stage/route charge.
- Bound `n` selected but not sent: no edge mutation.
- Bound `n` successfully sent → matching resolved response, same instance/hero and confirmation dismissed: learn declined edge; no second stationary or destination attempt.
- Bound answer → actual hero progress: existing position-progress reset applies; do not record decline evidence.
- Unrelated yn, eat/quit/character selection, interaction-direction prompts, unmatched or unknown-origin frames: no extension to stationary accounting.
- Unknown hero/transition: do not invent an edge; clear invalid scope/context.

This counts an attempt interrupted by confirmation, not an assertion that all future answers decline. A future deliberate `y` would not earn a second attempt; actual progress resets stationary state. Full accepted-prompt destination settlement is out of scope, because this change emits no such acceptance.

### C. One bounded pending prompt context

Use one controller/evaluator-owned slot because there is one action/need in flight. Carry origin identity, instance/src/dst/action, exact normalized prompt text, response NeedKey, local evidence baseline, and whether the source has already been counted. Bind the answer to that exact current need and record actual answer send identity/ordinal after a complete send.

Confirm decline only after the matching post-answer observation proves the engine left the confirmation and hero remains at src in the same instance. A stale `n`, invalid answer, answer to a replacement prompt, failed write, or re-presented confirmation must not write evidence. Preserve the existing bounded invalid/rejection path; on close/reset/instance transition clear context. On an unexpected observation or unrecognized nested need, discard rather than attach the edge to an unrelated answer. No new gameplay SentAttempt is fabricated for `yn`.

The original movement effect is not replayed when the answer resolves: this avoids double acquisition, double route/stall accounting, duplicate terminal events, and duplicate applied-cap spending. The answer resolution carries only the distinct prompt-edge evidence operation.

### D. Typed, scoped edge evidence and reopening

Extend blocked_edges values rather than inventing an independent destination blacklist. Retain recovery-failure semantics and add a typed `prompt-declined` reason. Key is the directed `(instance,src,dst[,movement_action_class])`; plain normal movement may retain the current three-part key because adjacent src/dst determines its action. Do not merge this with door-interaction or forced-prefix actions.

A prompt-declined record contains:
- current `blocked_edge_signature` components;
- normalized exact prompt text (case/whitespace normalization only, retain meaningful words);
- stable edge-local public hazard/overlay evidence at the attempted entry;
- original matched identity for diagnostics, NOT for signature equality.

Bound storage to one current failure record per directed adjacent edge/action in the current finite map instance, overwriting that edge's old evidence; no append-only list of prompts or attempts. Clear on instance reset. No timer or LRU eviction may silently enable repeat attempts under unchanged evidence.

Important comparison rule: absence of a prompt on ordinary command frames does not change the stored prompt text to empty and reopen the edge. Keep last bound prompt evidence until relevant edge evidence changes. Prompt text can be updated only by a newly matched prompt; an unrelated prompt must not reopen it.

Classified terrain alone is insufficient for clouds. Retain the minimum public edge-local overlay token needed to observe cloud-presence/change/disappearance, using the full observed cell data already available during observation folding. Prefer a small scoped token for stored failed endpoints over adding clouds as globally impassable terrain. Ignore time, visit counts, destination serials, global revisions, unrelated messages and remote occupancy. Do not interpret a hero/monster occlusion or missing/unknown cell as proof the cloud vanished. A positively observed relevant terrain/occupancy/side-cell or hazard change reopens the edge according to its signature; then at most one new decline is allowed for that new signature.

For recovery failures preserve the existing signature/reset semantics. For prompt failures compose them with prompt/overlay evidence; test explicitly which local changes reopen. If the wire cannot distinguish cloud disappearance, remain conservatively suppressed until positive relevant evidence or scope change; do not introduce a timer that recreates the loop.

### E. Route/destination/recovery integration

Add an optional pure edge-admissibility predicate (or equivalent immutable forbidden-edge view) to `navigation.plan`, `one_dijkstra`, and `one_dijkstra_steps`. The API is **append-only and keyword-only**: extend the signatures as `one_dijkstra_steps(..., deadline_check=None, edge_admissible=None)` (with matching additions to `one_dijkstra` and `plan`), and require every caller to pass the new arguments **by keyword**. Never insert a positional parameter before the existing `deadline_check`, which would silently reinterpret the live deadline callback. Predicate contract: **pure and directed** — `edge_admissible(src, dst, action_class)` — invoked **after** the geometric `edge_legal` check, at **both** root-neighbor seeding and every interior relaxation; being pure, it must not read or mutate policy state during a plan. `None` (the default) lets existing callers retain current behavior; policy supplies its scoped blocked-edge lookup. Preserve one-Dijkstra computation, weighted costs, true hop counts, deadline checks, and tie ordering.

Both held routing and target acquisition consume the same filtered dist/first maps. Consequently a longer valid route to the same held destination wins over retiring it; other destinations cannot repeatedly cross the blocked edge. Audit direct movement candidates and the already-filtered recovery/alternate paths for bypasses. Preserve emergency precedence; a known declined normal edge is not a useful emergency retry, so prefer another legal emergency move without promoting navigation above emergency handling. Do not alter unrelated emergency actions.

**Replace `_escape`'s movement selection/routing (Phase 3).** The emergency escape path currently selects and routes movement with raw `mem.known_passable` plus the legacy `_first_step` Dijkstra (`policy.py:2291-2312,2386-2422`). Replace that selection/routing with the classified `TerrainMemory`, `navigation.edge_legal`, and the **same immutable blocked-edge view** used by ordinary planning, while keeping the emergency branch **first** in policy precedence. Delete `_first_step` if no caller remains after the replacement. Define the fallback when the geometrically preferred emergency edge is declined: choose **another legal emergency edge**, then fall back to the existing stair/rest/search behavior. Never promote ordinary navigation above emergency handling, and never let a declined normal edge suppress a sole legal emergency move.

Named tests: emergency/route — `test_declined_emergency_edge_uses_alternate_legal_escape`, `test_blocked_interior_edge_on_route_to_upstairs_is_filtered`, `test_sole_legal_reverse_remains_available_to_emergency`, `test_emergency_singleton_precedence_unchanged_by_edge_filter`; edge-predicate API — `test_forbidden_edge_default_preserves_dist_first_steps`, `test_forbidden_edge_filters_seed_and_interior`, `test_forbidden_edge_alternate_route_keeps_true_hops`, `test_forbidden_edge_preserves_equal_cost_tie_order`, `test_forbidden_edge_still_checks_deadline`.

When no alternate held route exists, retire via the existing frozen unreachable/failure path and single terminal owner, earlier than or no later than the existing three-no-progress bound. Do not wait for three more cloud prompts. Crucially, distinguish route-only unreachability from a serviced/failed target: keep edge evidence as the suppression owner, or bind any route-failure target record to the relevant route evidence. A permanent target service-signature failure would stop legitimate reacquisition after the cloud clears. Do not use global map revision to reopen it.

When no filtered destination is reachable, bounded search/recovery owns the turn. The unreachable-settlement search must not become a per-destination unbudgeted search loop: avoid reacquiring unreachable targets, preserve site-search limits, and verify settlement accounting. Existing forced-search permissions and trapped/quit behavior remain unchanged. Cloud confirmation refusal must not authorize `m`-prefixed cloud entry.

### F. Compatibility and observability

No Choice wire shape, criteria order, confidence gate, Jev eligibility, cache/rendering/history, or provider-facing prompt/wire changes. (The only `providers.ReflexContext` change is the optional, internal-only `matched_movement_prompt`/`prompt_origin` field of §A, which `providers` and presentation never consume or render.) Preserve exact selected candidate and applied-token ownership across fallback, cap exhaustion, invalid delivery, and repair. Preserve pickup initiation/menu ownership and directive terminal semantics.

Expose minimal diagnostic reason data through existing recording paths: originating movement identity, prompt-bound stationary increment, decline evidence recorded, suppressed edge, evidence reopening, and route-unreachable disposition. These are observational diagnostics, not a new event ownership layer. Keep old recordings readable; avoid presentation/schema changes unless existing recording contracts actually require them.

## Phased Implementation Plan

### Phase 0 — Pin failing evidence and contracts

Extend existing `test/agent/stall_recovery_fixtures.py` and integration/replay tests with deterministic move-east → vapor yn → `n` → unchanged hero/time. Include multiple frontier destinations sharing that edge, a route-around variant, and a fully trapped variant. Prefer a sanitized real need/cell shape from the caller's recording. Record expected original failure: stationary stays zero and acquisition cycles.

**Native cloud probe (make the encoding gate executable).** Add a concrete, opt-in native target analogous to `native-pickup`: `make -C test/agent native-cloud` (or an equivalent `AGENT_TEST` cloud case) that deterministically constructs a vapor-cloud entry prompt and drives the real parity/movement check. It must record the prompt text and choices/default, the pre-move hero and displayed time, the destination tuple (`src`, `dst`, action class), the decline result, and — **if constructible** — the cloud's disappearance/overlay transition. If deterministic setup is infeasible for any element, add an explicit native-vs-operator-manual disposition for that element and make the operator-gated trace **mandatory** before any positive (cloud-disappearance) reopening claim is enabled; a manual result must be recorded, not inferred.

**Phase 0 exit (either/or).** The exit permits either (a) a **positively identified overlay transition** (the probe observes the cloud token appearing/disappearing), or (b) a **documented indistinguishable encoding** — e.g. gray `#` vapor vs corridor collision (`include/defsym.h:149,204`; `instances.py:199-204`) — which activates **conservative persistent suppression** (no positive reopening until positive relevant evidence or scope change). Both are acceptable exits; proceeding on an unpinned encoding is not.

Exit: implementer can demonstrate the original regression and either (a) positively identify the cloud overlay transition, or (b) document the indistinguishable encoding and enable conservative persistent suppression.

**The cloud native-vs-manual encoding decision is the only remaining Phase 0 gate.** No later phase may enable positive reopening claims until this gate is dispositioned (native probe passing, or an operator-gated manual trace recorded and marked as such).

### Phase 1 — Matched movement context and accounting

Affected verified files: `tools/agent/arbitration.py`, `candidates.py`, `controller.py`, `evaluate.py`, and `providers.py` (the optional, internal-only `matched_movement_prompt`/`prompt_origin` context field of §A); `state.py` only if a narrowly required public evidence field is missing. Preserve EpisodeMemory.commit API where possible. Add explicit safe cloud-prompt handling in `policy.py` before native default handling.

Exit: exact once-only count at prompt arrival; unrelated prompts excluded; no send/reconciliation/applied-token ownership regressions.

### Phase 2 — Confirmed decline ledger

Affected files: `controller.py`, `evaluate.py`, `policy.py`, `navigation.py`; use `instances.py` or scoped observation evidence storage only as needed for public cloud-overlay tokens. Implement scope reset, failed-send/invalid behavior, evidence comparison and bounded per-edge storage.

Exit: declined edge learned exactly once after real answer resolution, neither preparation nor stale `n` learns, positive evidence change reopens, ordinary prompt disappearance does not.

### Phase 3 — Planning and destination integration

Affected files: `navigation.py`, `policy.py`. Filter all Dijkstra edges; reuse maps for held/default/directive routing; avoid target-service poisoning and unbudgeted unreachable searches; preserve emergency and forced-search gates. **Explicitly replace `_escape`'s movement selection/routing** — currently raw `mem.known_passable` plus the legacy `_first_step` Dijkstra (`policy.py:2291-2312,2386-2422`) — with the classified `TerrainMemory`, `navigation.edge_legal`, and the same immutable blocked-edge view, keeping the emergency branch first; delete `_first_step` if no caller remains. When the geometrically preferred emergency edge is declined, choose another legal emergency edge, then the existing stair/rest/search behavior; never promote ordinary navigation above emergency handling and never let a declined normal edge suppress a sole legal emergency move.

Exit: route-around retains a valid commitment where possible; shared blocked edge cannot recur through new serials; no-route fixture reaches bounded recovery/exhaustion rather than cloud-looping.

### Phase 4 — Parity, mutations, documentation and review

Extend verified test files `test_auto_integration.py`, `test_auto_replay.py`, `test_auto_recovery.py`, `test_auto_navigation.py`, `test_auto_commitment.py`, `test_auto_candidates.py`, and `mutation_checks.py` as appropriate. Run existing pickup, forced-search, provider, presentation/cache and wiring regressions. Execute mutations and require a named test to fail for each. Run an operator-approved bounded live smoke campaign only after deterministic tests/review; report actual outcomes separately from expected ones.

## Acceptance Criteria

AC1 — Matched accounting is exact and narrow.
- `test_matched_movement_vapor_prompt_advances_stationary_once`: prompt arrival increments once; `n` result does not increment again.
- `test_unrelated_yn_and_interaction_direction_do_not_advance_stationary`: eat, quit, startup, inventory, open/zap direction and unmatched frames.
- `test_prompt_replay_unknown_hero_and_instance_change_do_not_double_count`.
- `test_prompt_bound_attempts_compose_with_stationary_thresholds_3_6_10`: parameterize starting stages 2/5/9 and verify next genuine decision follows existing recovery precedence.

AC2 — Edge evidence belongs to the actual declined movement.
- `test_declined_movement_records_origin_edge_not_destination_or_answer`.
- `test_prompt_decline_requires_matching_answer_send_and_resolution`: prepare-only, unselected, write-failed, invalid/repaired, replacement need, repeated prompt, and stale `n` variants.
- `test_prompt_answer_does_not_reapply_destination_or_applied_token`.
- `test_cloud_confirmation_declines_even_with_yes_native_default`: no blind acceptance or prefix bypass.
- `test_movement_origin_operation_taxonomy_matrix`: parameterized over each §A operation-class row — accepts destination acquire/continue movement, recovery-step movement, and directional emergency escape; rejects door interaction, movement prefix, stair, wait/search, classless direction continuations, and synthesized/effect-less candidates.
- `test_prompt_decline_resolution_with_hero_progress_records_no_edge`: with the decline-resolution path active, a hero-progress (moved), unknown-hero, or nonadjacent-relocation variant records **no** declined edge; live/evaluator parity.

AC3 — Suppression and reopening are precise and bounded.
- `test_prompt_declined_edge_survives_prompt_absence_and_destination_reacquisition`.
- `test_prompt_edge_reopens_on_positive_local_cloud_change`.
- `test_prompt_edge_ignores_time_visits_remote_occupancy_and_occlusion`.
- `test_prompt_edge_signature_preserves_diagonal_side_evidence`.
- `test_prompt_ledger_overwrites_per_edge_and_clears_on_instance_reset`.
- `test_route_only_failure_does_not_permanently_suppress_target_after_reopen`.

AC4 — Full pipeline cannot reproduce the alternating freeze.
- `test_vapor_cloud_loop_routes_around_with_held_destination`: after first completed decline under unchanged signature, zero further attempts on that edge; reach a valid alternate step within the next genuine command selection.
- `test_vapor_cloud_loop_filters_interior_dijkstra_edges`: ensure it is not merely a first-hop blacklist.
- `test_blocked_interior_edge_on_route_to_upstairs_is_filtered`: a blocked interior edge on a route to known upstairs is filtered by the same immutable blocked-edge view, not only at the first hop.
- `test_vapor_cloud_loop_all_routes_blocked_enters_bounded_recovery`: multiple frontier targets behind the same edge; no repeated acquisitions through it; one terminal per retired serial; search and forced-search use existing budgets and terminate in progress or policy exhaustion. Set the fixture's explicit action cap from configured existing search/quit budgets, not a vague timeout or arbitrary large cap.
- `test_vapor_prompt_destination_attempts_respect_three_attempt_bound`: acquisition/continuation compose without double-charge; earlier unreachable retirement is allowed.

AC5 — Live/evaluator and standing contracts remain aligned.
- `test_live_evaluator_movement_prompt_accounting_and_ledger_parity`: compare per-frame counter, pending identity, ledger, hero, lifecycle, selected wire and applied counters.
- `test_capped_scripted_vapor_decline_retains_effect_ownership`.
- `test_prompt_edge_route_filter_preserves_emergency_and_pickup_precedence`.
- `test_declined_emergency_edge_uses_alternate_legal_escape`: a declined geometrically preferred emergency edge falls back to another legal emergency edge before stair/rest/search behavior.
- `test_sole_legal_reverse_remains_available_to_emergency`: a sole legal reverse step is never suppressed for emergency escape by a declined normal edge.
- `test_emergency_singleton_precedence_unchanged_by_edge_filter`: edge filtering does not promote ordinary navigation above emergency handling.
- `test_cloud_encoding_disposition_is_native_or_manual`: the cloud-encoding gate is dispositioned explicitly — either the native `native-cloud` probe passes, or an operator-gated manual trace is recorded for the affected element(s); no shape is silently skipped.
- `test_forbidden_edge_default_preserves_dist_first_steps`: `edge_admissible=None` reproduces the existing dist/first maps exactly.
- `test_forbidden_edge_filters_seed_and_interior`: a predicate edge is excluded at both root seeding and interior relaxation.
- `test_forbidden_edge_alternate_route_keeps_true_hops`: an alternate route keeps true hop counts and weighted costs.
- `test_forbidden_edge_preserves_equal_cost_tie_order`: equal-cost tie ordering is unchanged under a predicate.
- `test_forbidden_edge_still_checks_deadline`: the deadline check still fires with a predicate supplied (callers pass new args by keyword, never positionally before `deadline_check`).
- Run existing Choice criteria/index/N, strategy frozen historical bytes/block order, selected-candidate identity, delivery-repair, applied-cap, pickup ownership and forced-search gate suites unchanged except explicitly migrated prompt-accounting expectations.

## Test Strategy

All names are proposed exact tests in verified suites; the compact map below ties each acceptance criterion to its named tests. Implementers execute them; no test execution is claimed here.

| AC | Named tests |
|---|---|
| AC1 | `test_matched_movement_vapor_prompt_advances_stationary_once`, `test_unrelated_yn_and_interaction_direction_do_not_advance_stationary`, `test_prompt_replay_unknown_hero_and_instance_change_do_not_double_count`, `test_prompt_bound_attempts_compose_with_stationary_thresholds_3_6_10` |
| AC2 | `test_declined_movement_records_origin_edge_not_destination_or_answer`, `test_prompt_decline_requires_matching_answer_send_and_resolution`, `test_prompt_answer_does_not_reapply_destination_or_applied_token`, `test_cloud_confirmation_declines_even_with_yes_native_default`, `test_movement_origin_operation_taxonomy_matrix`, `test_prompt_decline_resolution_with_hero_progress_records_no_edge` |
| AC3 | `test_prompt_declined_edge_survives_prompt_absence_and_destination_reacquisition`, `test_prompt_edge_reopens_on_positive_local_cloud_change`, `test_prompt_edge_ignores_time_visits_remote_occupancy_and_occlusion`, `test_prompt_edge_signature_preserves_diagonal_side_evidence`, `test_prompt_ledger_overwrites_per_edge_and_clears_on_instance_reset`, `test_route_only_failure_does_not_permanently_suppress_target_after_reopen` |
| AC4 | `test_vapor_cloud_loop_routes_around_with_held_destination`, `test_vapor_cloud_loop_filters_interior_dijkstra_edges`, `test_blocked_interior_edge_on_route_to_upstairs_is_filtered`, `test_vapor_cloud_loop_all_routes_blocked_enters_bounded_recovery`, `test_vapor_prompt_destination_attempts_respect_three_attempt_bound` |
| AC5 | `test_live_evaluator_movement_prompt_accounting_and_ledger_parity`, `test_capped_scripted_vapor_decline_retains_effect_ownership`, `test_prompt_edge_route_filter_preserves_emergency_and_pickup_precedence`, `test_declined_emergency_edge_uses_alternate_legal_escape`, `test_sole_legal_reverse_remains_available_to_emergency`, `test_emergency_singleton_precedence_unchanged_by_edge_filter`, `test_cloud_encoding_disposition_is_native_or_manual`, `test_forbidden_edge_default_preserves_dist_first_steps`, `test_forbidden_edge_filters_seed_and_interior`, `test_forbidden_edge_alternate_route_keeps_true_hops`, `test_forbidden_edge_preserves_equal_cost_tie_order`, `test_forbidden_edge_still_checks_deadline`, plus the existing Choice criteria/index/N, strategy frozen-bytes/block-order, selected-candidate identity, delivery-repair, applied-cap, pickup ownership, and forced-search gate suites |

### Mutation checks

Require each mutation to be killed, not merely executed:
1. Restore response-kind-only command/key/direction filter → AC1 matched-vapor test.
2. Permit every yn response to advance → AC1 unrelated/interacting-direction test.
3. Count both prompt arrival and answer result → AC1 exactly-once and AC2 no-reapply tests.
4. Infer movement from raw direction without operation identity → AC1 interaction-direction test.
5. Record evidence at proposal/send rather than resolved decline, or accept stale `n` → AC2 send/resolution test.
6. Derive dst from semantic destination or latest answer → AC2 origin-edge test.
7. Compare stored prompt with empty current command prompt → AC3 prompt-absence test.
8. Omit local cloud evidence or reopen on global revisions/time → AC3 positive-local-change and unrelated-evidence tests.
9. Filter only recovery or only Dijkstra seed edges → AC4 route-around/interior-edge fixtures.
10. Retain target service-signature suppression after route-only failure → AC3 target-reopen test.
11. Honor a yes native default for cloud confirmation or bypass via forced movement → AC2 safe-decline and AC4 no-route fixtures.
12. Patch live only, reset scoped ledger early, or duplicate repaired effect/applied token → AC5 parity/repair plus AC3 scope tests.
13. Remove the unchanged-confirmed-hero condition for decline resolution → `test_prompt_decline_resolution_with_hero_progress_records_no_edge`.

## Review Strategy

After implementation/testing, obtain the project's prescribed independent review, address findings and rerun relevant tests/mutations before an operator-approved live smoke run. No review or validation is claimed as performed here.

## Documentation Strategy

Amend `doc/agent-stall-recovery-plan.md` §2A introductory exclusion, table row for unmatched prompts, rule 1, and named prompt test at lines 102–126. Distinguish unbound prompts from a matched gameplay attempt whose first response happens to be a movement confirmation. Add answer-resolution and decline-evidence rules explicitly; preserve rules 2–6 and route/stall semantics. Amend §4 lines 160–164 for typed prompt-declined edge signatures, public overlay evidence, per-edge storage bounds, and route-wide suppression. Rename/split the existing broad `test_prompt_observations_do_not_advance_stationary_stage` expectation rather than silently weakening it.

Update `doc/agent-destination-commitment-plan.md` for rerouting, route-only failure reopening and exact lifecycle ownership; update `doc/agent-jev-gate-nav-plan.md` to state that command-only Jev/singleton behavior is unchanged and yn movement interruption is handled locally. Document exact fixture bounds, supported prompt recognizers and conservative uncertainty behavior in `test/agent/README.md`. Do not change presentation version merely for internal accounting.

## Risks and Decisions

- Main risk is wrong attribution: direction-shaped interactions and arbitrary yn prompts are not movement. Bind origin need, exact selected operation, attempt identity and answer need together.
- Public overlay observability is the remaining implementation-sensitive question. Source confirms engine semantics, but actual wire cloud token/visibility has not been inspected here. Phase 0 must pin it. If unavailable, conservative suppression is preferable to inventing cloud disappearance.
- Prompt arrival is not final engine movement completion. Counting there is a deliberate attempted-action policy consistent with current destination reducer timing; accepted cloud movement and richer nested prompts require a later explicit transaction design, not accidental support.
- Permanent target failure could hide a reopened route; route-only failure must be distinguished from actual serviced/failed target evidence.
- Rollback is a cohesive agent-side change; no engine/save migration is required. Do not roll back only graph filtering while leaving decline records apparently protective. Operator action caps remain containment, not the fix.

## Non-goals

- Reject blanket yn counting, unconditional `y`, cooldown-only retry, raising caps, changing Jev confidence, and merely retiring more destinations: each misses attribution, safety, or cross-destination recurrence.
- Reject making every displayed cloud globally impassable: use learned directed-edge evidence and preserve classified terrain; a future deliberate-entry policy can be layered separately.
- No engine behavior changes, new dependencies, general prompt interpreter, combat overhaul, pickup redesign, provider/cache changes, or new dangerous-action permissions.

## Reviewer scrutiny

1. Inspect exact live and evaluator ordering: capture matched origin before SentAttempt is cleared; commit memory once; commit original movement effect once; bind/resolve answer without arming a second gameplay attempt.
2. Demand a fixture with actual `need.prompt`, choices/default and cloud cell representation. Confirm the hero shown at the prompt is still pre-move; do not infer movement merely because an action was sent.
3. Verify gameplay-need provenance plus operation semantics; an open-door direction followed by confirmation must not acquire a movement edge.
4. Confirm no evidence mutation before successful answer delivery and matched resolution, including rejected-answer repair. Confirm stale context clears on transition, close, unexpected replacement, and unknown scope.
5. Read both Dijkstra loops and all direct movement constructors; a recovery-only or first-hop-only patch is incomplete.
6. Confirm prompt text participates without absence-of-prompt reopening, and cloud occlusion is not disappearance. No global revision/time/visits/serial in equality signatures.
7. Check route-only target failure does not permanently prevent reacquisition after relevant edge reopening, and no target churn creates unbudgeted search actions.
8. Check emergency/pickup priority, Choice wire/cache invariants, applied-cap and exact selected-candidate ownership remain intact.
9. Preserve existing forced-search gates: declining a cloud does not authorize dangerous prefixed movement.
10. Require measured bounded termination and killed mutations, not just unit-test counts or lifecycle retirement events. The original failure already had valid retirement events.
