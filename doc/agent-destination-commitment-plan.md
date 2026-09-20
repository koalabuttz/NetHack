# Destination commitment and situational pickup (Revision 4 — DRAFT, pending plan review)

> **Round-1 plan-review provenance.** Review verdict: **REVISE — 5 High, 4 Medium, 2 Low, all addressed** in this revision. Changes: (1) pickup stays command-only with conservative local menu filtering (§1.3, §3.3, AC11); (2) explicit reconciliation state machine (§1.4); (3) honest shop/single-item-autoselect guarantee (§3.3, §3.5, AC11, Risks); (4) evaluator directive-scoping parity (§2.2, Phase 2); (5) full AC→named-test map plus native pickup-shape test infrastructure (§6 Phase 0/3, §7, §8); (6) corrected `directives_applied` semantics (Verified facts, §5); (7) evidence-epoch stability (§3.2); (8) command-only v2 application rule (§1.5, §2.2); (9) replace-not-branch core policy functions (§1.3, Phase 1, Implementation Summary); (10) v2 target-legality matrix (§2.1); (11) contract-migration checklist (§6). Sections not named here are retained verbatim from Revision 1.

> **Round-2 plan-review provenance.** Review verdict: **REVISE — 2 High, 3 Medium, 2 Low, all addressed** in this revision. Changes: (1) one common pending-directive activation ordering — **service/settle strategy → activate eligible pending advice → build the directive view → prepare/select the action**, with emergency-singleton precedence preserved inside policy — adopted for live and evaluator alike, so a newly activated v2 destination affects the same command decision in both (§2.2, Phase 2, AC7, AC15); (2) completed AC→named-test map for the previously untested core clauses of AC2, AC4, AC8, AC11, and AC14 (§7, §8); (3) `explore_frontier`/`search_dead_ends` renamed **destination-selecting goals** and the target-rule sentence corrected so only `collect_items`/`flee_to_upstairs`/`descend_known_stairs` carry or resolve coordinates (§2.1); (4) explicit post-implementation diff-review loop added (§9); (5) concrete native pickup probe infrastructure named and AC17's pass condition restated as the native target passing (§6 Phase 0/3, AC17, AC18, Risks); (6) `AUTOSELECT_SINGLE` residual reframed as one object *entry/stack* at its full quantity (`src/pickup.c:1012-1015,1072-1076`), not one unit (§3.3, §3.5, AC11, Phase 0, Risks); (7) stale `directives_applied` phrasing corrected to applied boundary EIDs, with no directive-set activation counts inferred from that field (§2.2, Risks, Verified facts); (8) contract-migration stair-test entry corrected for default-vs-explicit-stair semantics (§6). Sections not named here are retained verbatim from Revision 2.

> **Round-3 plan-review provenance.** Review verdict: **APPROVE WITH FIXES — 4 Medium, all addressed** in this revision. Changes: (1) explicit **emergency-preemption exception** to same-command destination activation — activation updates DirectiveBook authority *before* preparation, but an emergency/mandatory singleton that preempts the destination pipeline on that command **suspends** the old destination and **commits no replacement**; the newly active destination is resolved and compare-and-applied on the **next non-preempted genuine command decision** (§1.5, §2.2, AC7, and the strengthened `test_emergency_singleton_precedes_destination_application`); (2) **delivery repair must not activate pending advice** — pending advice activates only on the fresh-selection branch (`repair is None`) *before* `_decide()` builds context, and is never activated or consumed during `_resend_repair` (the current unconditional activation at `tools/agent/controller.py:2653`, which sits after repair handling, is gated); pending advice is preserved until the next fresh command decision (§2.2, Phase 2, §8, AC7, AC15, `test_incomplete_delivery_repair_does_not_activate_or_consume_pending_destination`); (3) **four residual AC→named-test map holes closed** — AC6 render purity (`test_destination_and_pickup_presentation_never_mutate_retained_table`, `test_destination_render_does_not_mutate_commitment_state`), AC7 (`test_pending_direction_does_not_apply_new_destination`), AC8 (`test_auto_spectate.py::test_ttl_equality_is_eligible`), AC9 (`test_replay_reads_v1_and_v2_directives_with_original_versions`), and a concrete AC16 validation-report check (`test_validation_report_contains_required_fields`) (§8); (4) **AC17/AC18 native-vs-manual partition made explicit** — AC17 must pass (or explicitly mark unsupported) every shape deemed deterministically constructible after Phase 0 investigation and never silently skip a non-constructible shape, AC18 covers each explicitly named unsupported shape with a mandatory operator-gated manual result, and the eight shapes' dispositions are required to be exhaustive (`test_pickup_shape_disposition_is_exhaustive`) (§6 Phase 0, §7 AC17/AC18, §8). Sections not named here are retained verbatim from Revision 3.

Design produced by `architect:architect-destination`. Follow-up to `doc/agent-jev-gate-nav-plan.md` (anti-oscillation/room awareness, live) and `doc/agent-jev-presentation-plan.md`. Operator direction: "Make it destination commitment. Default being frontier/door, but if DeepSeek thinks we 'need to pick up those items' or 'flee to upstairs' then we'll be directed that way. Deciding if something should be picked up should depend on the current situation and build."

## Goal

Make the reflex tier pursue a persistent destination rather than electing a new destination every tick. Default destinations are closed-door approaches and exploration frontiers, followed by unvisited known cells. DeepSeek can explicitly replace that destination to collect items or retreat to known upstairs. Item acquisition depends on displayed evidence, current need, role/build context, inventory freshness, and danger—not simply the presence of an item glyph.

**Recommendation:** extend the existing `navigation.TargetStore`, owned by `ScriptedReflex` and scoped to the active level instance. Retain one destination while running the existing single Dijkstra each command turn to find its current route. Enforce commitment through candidate generation, not a larger score bonus. Add directive schema v2 goals `collect_items` and `flee_to_upstairs`; retain a v1 reader. Let DeepSeek direct detours, Jev judge bounded on-route pickup opportunities, and the scripted fallback handle only narrow, evidence-supported urgent food acquisition. Preserve emergency and recovery priority.

This is a read-only design handoff. The architect inspected source and documents; it did not modify files, run commands, or run tests. The campaign measurements and the reported 907 passing auto tests are caller-supplied evidence, not independently reproduced here.

## Implementation Summary

1. Wire an extended `TargetStore` into policy; use persistent classified terrain from the frozen reflex context.
2. Separate destination selection, path recomputation, and terminal interaction by replacing `_navigation_candidates` with a pure `resolve_destination` → `route_held_destination` → `build_terminal_or_step_candidates` pipeline, with committed mutation confined to the reconcile/effect reducer. A closed door is not completed merely by reaching its approach square.
3. Produce only commitment-compatible navigation candidates while a target holds. Retain the existing >40 anti-backtrack rule for acquiring a default target, but do not apply comparisons with unrelated targets to a held target.
4. Add explicit, validated strategy destinations through two new schema-v2 goals. Resolve advice locally into observed, legal targets; never accept wire actions from the strategy tier.
5. Add a small floor-item evidence and attempt lifecycle, pickup initiation, and safe pickup-menu continuations. Opportunistic pickup does not abandon the exploration destination.
6. Extend live/replay wiring, presentation, recordings, and validation metrics together. Bump Jev presentation to `/3` for the added build/commitment context and directive summaries; preserve the Choice wire contract.

## Verified facts and corrections

Paths and line numbers below refer to the inspected checkout, not the older plan's line numbers.

- `tools/agent/policy.py:555-631`: quit, low-HP escape, hunger, recovery, and inventory maintenance precede navigation. Safety is implemented as singleton priority, not a score boost.
- `tools/agent/policy.py:642-688`: navigation calls one Dijkstra, enumerates/scorers all targets, applies anti-backtrack, and returns their first-step candidates each decision. There is no held destination in this main path.
- `tools/agent/policy.py:690-763`: immediate reversals are suppressed against same-family alternatives unless their score advantage is strictly greater than 40. Rejected signatures are excluded from suppression comparisons. `:765-802` implements edge-legal singleton cycle recovery.
- `tools/agent/policy.py:1183-1199`: ordinary base scores are stair 800, door 700, frontier 500, unvisited 400; path adjustment is capped at 40. `explore_first` boosts frontier/unvisited by 450. `:972-979` supplies only +30 for a coordinate-matching directive. **Default frontier/door commitment therefore requires an explicit target-pool policy change, not blindly retaining today's all-family argmax.**
- `tools/agent/navigation.py:159-168,185-230`: a visited known cell can remain a frontier. Door targets store the **door coordinate**, derive a cardinal approach, and issue a direction toward the door when already at that approach. The hero is excluded from ordinary frontier/unvisited enumeration.
- `tools/agent/navigation.py:255-316`: `PersistedTarget`/`TargetStore` exist. They check instance and local terrain/occupancy, but do not implement the proposed lifecycle, route reachability, frontier exhaustion, directive generation, or bounded terminal interactions. The primary policy path does not use them.
- `tools/agent/policy.py:317-334`: policy gameplay effects are committed after a selected action is sent and reconciled; preparation must not mutate gameplay state. `:884-925` folds observations once and resets reflex-local state on a fresh instance.
- `tools/agent/candidates.py:302-307,344-371`: frozen `effect_payload` already exists and nonempty payloads participate in identity. `tools/agent/controller.py:1623-1625,1654-1658` retains selected effect payloads for reconciliation. Extend this mechanism rather than adding a mutable candidate-to-target side map.
- `tools/agent/policy.py:966-970`: navigation currently reconstructs classified terrain from raw remembered cells. The presentation contract instead requires runner-owned persistent terrain (`doc/agent-jev-presentation-plan.md:38-39,80-84`). This matters when an item or creature overlays known ground.
- `tools/agent/directives.py:25-67,74-141`: schema v1 has exactly nine goals, one shared coordinate target, risk, TTL, preconditions, and explanation. It cannot explicitly distinguish item collection from general food acquisition or retreat to upstairs from generic disengagement.
- `tools/agent/directives.py:188-223,251-259,292-339`: DirectiveBook owns instance/level/TTL/precondition applicability, increments generation at activation, and supports a pure display peek. TTL equality is still eligible (`age > ttl` expires).
- `tools/agent/providers.py:805-821`: the static DeepSeek prompt explicitly lists v1 and all nine goals. `:861-897` fixes snapshot order and ends with remaining budget. `:902-951` freezes historical user/assistant pairs.
- `tools/agent/controller.py:1947-1950,2000-2032`: validated advice becomes pending, then activates if dispatch level/instance still match. Despite the method's command-boundary wording, the current activation gate accepts command, key, and direction needs. New destination intent must not hijack a pending prompt continuation.
- `tools/agent/controller.py:1056-1057,2030-2032`: `directives_applied` is the number of boundary EIDs settled as applied (`tools/agent/events.py:472-479,505-512`; one DirectiveBook activation can settle several EIDs); it is neither a count of directive-set activations nor of steered reflex decisions. The supplied 17 calls versus 16 applied boundary EIDs does not establish frequent validation rejection or expiry. A postmortem call, failure, cancellation, or stale response could account for the difference; no campaign artifacts were inspected to identify which.
- `tools/agent/controller.py:2040-2069`: strategy receives configured role, cached inventory, recent messages, map, HP/hunger/XL, active directives, and boundary history. This builder does not explicitly add current condition facts or inventory-age facts to status.
- **Pickup correction:** inspection of `tools/agent/policy.py` found no `pick-up` or `KEY_PICKUP` candidate generation. Presentation supports the key and generic/exact-bound pickup text (`tools/agent/presentation.py:118,262,556-560`), and the wire key exists (`tools/agent/protocol.py:62`). The earlier claim of a current scripted pickup singleton is not true of this checkout. Pickup is new policy behavior here.
- `tools/agent/policy.py:429-483`: current menu handling recognizes eating and a limited generic menu set; unrecognized menus cancel. Pickup needs an explicit intent/menu lifecycle.
- `tools/agent/instances.py:76-91,111-117`: closed display-appearance interpretation includes weapons, armor, food, coins, and other item categories. `:290` exposes remembered upstairs. `tools/agent/presentation.py:1076-1105` implements destination appearance clauses.
- `tools/agent/presentation.py:52-59`: current presentation is `jev-presentation/2`. `:721-729` is the nine-goal summary map. `:1157-1178` renders HP/hunger/conditions, inventory, room contents, and directives, but no role/build field.
- `doc/agent-cache-plan.md:31-48,52-101`: static system prompt; fixed snapshot block order; exact frozen historical messages; transactional history; bounded eviction and no rerendering old turns. `doc/agent-jev-presentation-plan.md:47-76` freezes Choice object/order/key-index/N relationships and prohibits presentation-layer candidate mutation. Room-awareness amendments are in `doc/agent-jev-gate-nav-plan.md` §5.
- `AGENTS.md:51-63`: preserve repository conventions/NHDT tags, avoid generated files and unrelated engine changes. Its general statement about no standalone tests predates the visible agent test suites; use the actual `test/agent/test_auto_*.py` suites for this Python change.

## 1. Destination lifecycle and ownership

### 1.1 State and responsibilities

Extend the existing navigation value types rather than introduce a planner service or dependency. `ScriptedReflex` owns one active store, reset through `begin_instance`; do not restore an old active commitment after leaving and revisiting a level. Level-instance identity, not displayed depth alone, is authoritative.

Conceptual destination record:

- instance id; deterministic local commitment serial;
- purpose: explore-frontier, explore-unvisited, open-door, collect-items, flee-upstairs, or existing directed stair navigation;
- semantic target coordinate, with a distinct approach coordinate for a door when needed;
- source: default or directive; originating directive generation and schema;
- phase: travelling or interacting;
- source evidence token; acquisition tick and last meaningful progress;
- bounded failed/no-progress attempt counters.

Do not persist a first-step direction or entire route as authoritative state. A route is recomputed from the current confirmed hero and persistent terrain. The semantic door coordinate remains stable even if the cheapest approach changes.

Keep a small instance-scoped resolved/failed-target ledger and floor-item ledger alongside the store. Bound them by observed map sites and small per-site records; do not accumulate unbounded history. These are derived policy memory, never engine object identities.

### 1.2 Selection and default ordering

When there is no eligible held target:

1. Resolve an eligible, unconsumed explicit strategy destination.
2. Otherwise choose from reachable doors and frontiers using the existing score and deterministic tie ordering within that restricted pool. With no explicit explore directive, existing scores prefer doors over frontiers; preserve that modest choice rather than tuning a new reward function here.
3. If that pool is exhausted/suppressed, choose an unvisited known cell.
4. If no exploration target is available, use current known-stair/search/recovery fallback behavior. Do not invent unknown-space moves.

Use existing `explore_frontier`/`search_dead_ends` score semantics where applicable. Explicit `descend_known_stairs` may still select stairs; targeting/downstairs priority is not globally removed. Existing descent on the hero's square remains a scripted singleton, but it must not interrupt an active explicit collection/retreat destination merely because its route crosses `>`.

For a new default target, apply existing anti-backtrack to scored representatives before choosing the winner. Preserve deterministic representative selection and the target associated with the winning first-step action. Do not select a target and then lose its identity through action deduplication.

### 1.3 Holding and routing

Each command boundary computes at most one navigation Dijkstra. If the held destination remains eligible, resolve its next step from that plan—even if a newly discovered target now scores higher, its frontier family classification has changed, or a visit penalty has increased. A frontier that ceases to border unknown space en route remains an exploration waypoint until reached unless local evidence makes it invalid; this prevents discovery itself from constantly canceling progress.

**Replace, do not branch, the core policy functions.** `_navigation_candidates` is replaced by a small pure pipeline — `resolve_destination` → `route_held_destination` → `build_terminal_or_step_candidates` — with committed mutation confined to the reconcile/effect reducer (§1.4). If menu handling is retained, the singleton `_noncommand_candidate` contract is replaced by a need-specific candidate builder rather than adding further conditionals to `_menu`. All callers and tests are updated together in one phase (Phase 1).

Return only the held destination's next navigation action (normally a singleton), plus any explicitly permitted on-square pickup alternative. Do not offer unrelated target movement to Jev during commitment. Destination selection is the policy/strategy tier's responsibility. Jev stays command-only: at a command decision on a supported item site the Choice offers exactly the `pick-up` action and the held-route continuation, and Jev may select between those two; Jev is never offered individual pickup-menu rows (§3.3). Fewer navigation consultations are an expected consequence, not a regression to repair by adding distractor candidates.

For a held target or newly resolved explicit strategy override, skip the cross-target anti-backtrack comparison. All edge legality, current occupancy, local rejection, emergency, and recovery checks still apply. A required reverse step must remain eligible even when an unrelated same-family target is within the 40-point margin. Keep the strict >40 rule unchanged for ordinary uncommitted acquisition.

### 1.4 Transaction boundary

Candidate preparation is pure. It proposes a destination acquisition/continuation/terminal effect as immutable payload, bound to instance, destination, and directive generation. Only the selected, sent, reconciled candidate commits acquisition and counters. Local-invalid, write-failed, unselected, canceled, stale-instance, and late Jev results must not change commitment.

#### Reconciliation state machine

Each destination payload carries `operation` (`acquire` | `continue` | `interact`), the expected current commitment serial (or none for a fresh acquisition), destination/evidence identity, and the originating directive generation. One operation is applied per reconciled fold.

The established fold order is fixed and shared by live and evaluator: reconcile the observation → commit memory → `note_observation` → `commit_effect` (live `tools/agent/controller.py:1355-1378`; evaluator `tools/agent/evaluate.py:611-619,775-783`). `commit_effect` applies compare-and-apply semantics against the serial/identity captured at proposal time:

- **never recreate a serial retired by that same fold** — a serial retired earlier in the fold must not be reinstalled by a later effect in the same fold;
- **newly acquired one-hop target** — inspect the post-observation state and directly record `reached`/`failed` instead of installing the destination, because the observation that justified acquisition may already satisfy it;
- **`continue`** — apply only if the expected serial is still the active commitment; otherwise drop the effect as stale;
- **`interact`** — increment the attempt counter only when the target and evidence identity still match the unchanged expected serial.

Fold observed completion/invalidation once at the established observation boundary. A no-time blocked action can establish failure evidence and invalidate a proposed destination, but must not count as movement progress. For directive changes and expiry, consult the active view during preparation; do not advance generation/counters while rendering.

Payload additions are intentional policy-identity changes: stable identical inputs still produce identical ids, but a different destination with the same immediate key must not silently share a commitment-bearing identity. Do not change labels merely to make presentation prettier. Audit candidate table fingerprints, pending Jev staleness checks, and replay with this distinction.

### 1.5 Completion, interruption, and invalidation

- Frontier/unvisited: complete on confirmed arrival, then mark that waypoint serviced for its **local evidence signature**. A visited frontier still bordering unchanged unknown terrain must not be immediately re-elected on every departure. Use local classified terrain/visibility/door changes as reset evidence—not visit counts, time, unrelated map discovery, or the hero overlay. Existing bounded search may investigate it later.
- Door: arrival at an approach does not complete the target. Use the existing locally generated door interaction behavior. Complete on observed opening/disappearance of the closed door, then allow the now-visible route to become the next exploration target. On an explicit locked/refused message, fail immediately; otherwise allow at most two reconciled ineffective door-interaction attempts per unchanged local evidence signature. Do not introduce automatic kicking/unlocking.
- Item collection: arrival begins pickup/inspection; success, no-items evidence, explicit refusal, deliberate cancellation, or exhausted attempt budget terminates that directive target. See §3.
- Flee upstairs: complete on confirmed arrival at the known upstairs square. This goal means **reach upstairs**, not automatically issue `<`. Do not initiate a new level transition under an ambiguous retreat directive; this is particularly important at the dungeon exit. Mark the generation served so it does not reassert every tick. If the operator later wants ascent, add an explicitly distinct interaction decision rather than silently changing this goal's meaning.
- Safety: emergency actions always preempt. Suspend rather than discard an otherwise valid destination for a single hunger, inventory, or escape interruption; revalidate after the interruption. If the target/path is dangerous or unreachable under current legal occupancy, fail/defer it rather than repeatedly trying it.
- Directive: a newly activated explicit destination replaces the old one at the next genuine command decision, not in a direction/menu continuation. Activation updates DirectiveBook authority **before** preparation, so the active view used during that command's preparation already reflects the new destination. **Emergency-preemption exception:** if an emergency or mandatory singleton preempts the destination pipeline on that command, the old destination is **suspended** and **no replacement is committed on that command**; the newly active destination is resolved and compare-and-applied (against the commitment serial/identity captured at proposal time) on the **next non-preempted genuine command decision**. Suspension keeps the prior destination for revalidation rather than discarding it; only the preempted command's destination pipeline is skipped. **One application rule:** pending v2 destination advice is neither consumed nor activated on `key`, `direction`, menu, or yes/no needs; it activates only when `need.kind == "command"`, in both live and evaluator. Because the existing activation gate is broad and accepts command, key, and direction needs, make activation itself command-gated for v2 destination sets rather than merely deferring resolution; modifier-only legacy sets may retain the broad gate only where destination resolution is provably deferred. An identical live destination in a new generation may refresh authority without resetting progress/failure counters. Repeated strategy advice must not erase observed failure or pickup negatives.
- Directive expiry/precondition failure: retire that directive-owned destination; do not launder it into an immortal default commitment. A fresh default target may independently happen to be the same coordinate if eligible.
- Level instance change: clear active commitment and scoped ledgers.
- Cycle: invalidate the active destination before existing cycle recovery, suppress its reselection under the same local evidence, and run the existing edge-legal recovery singleton. A failed strategy generation does not reassert itself on the next tick.
- Blockage/stall: immediate failure for an observed hard refusal/unreachable path; otherwise at most three reconciled navigation attempts without a new cell toward the route or useful local terrain progress. Add a generous total selected-navigation cap, initially `max(16, 4 * initial shortest-path hop count + 8)`, to bound longer cycles outside the existing period-2/3 detector. Count actual selected navigation actions, not inventory/prompt ticks. Constants must be named and telemetry-visible; tune only after baseline validation.

Failure suppression resets on relevant local terrain/occupancy evidence change or a genuinely new valid directive, but a new directive cannot override a hard safety constraint or reset identical item/door failure evidence. No global-map revision reset that revives every failed site whenever any room changes.

## 2. Strategy-directed destinations

### 2.1 Schema decision

Introduce directive schema v2, same object shape, with exactly two additional goals:

- `collect_items`: requires a non-null coordinate target.
- `flee_to_upstairs`: target optional; if absent, resolve the nearest reachable observed upstairs deterministically. If supplied, it must resolve to known upstairs locally.

No named target strings, executable content, item ids, inventory letters, raw menu rows, or keys. `risk` never relaxes navigation legality or pickup uncertainty controls. Explanation is untrusted rationale, not an instruction language.

Continue accepting schema v1 with exactly the original nine-goal vocabulary and existing semantics. Missing `schema_version` remains legacy v1, avoiding silent reinterpretation of old artifacts. Serialize the actual accepted version in `to_dict`; do not normalize old records to v2. New DeepSeek requests require explicit v2. Reject booleans/floats masquerading as schema versions as well as malformed coordinates, unknown fields/goals, duplicates, and wire-like fields.

For v2, define positional goals as `collect_items`, `flee_to_upstairs`, `explore_frontier`, `search_dead_ends`, and `descend_known_stairs`. Among these, `explore_frontier` and `search_dead_ends` are **destination-selecting goals** — they select their destination locally during resolution and are not coordinate-bearing — whereas `collect_items`, `flee_to_upstairs`, and `descend_known_stairs` carry or resolve explicit coordinates. Permit at most one positional goal in a set, so the single shared target is unambiguous; survival/food/recovery/inventory modifiers may coexist. Ordered goals still convey strategy priority, but hard emergency handling cannot be demoted. Keep v1 multi-goal validation unchanged for compatibility.

**V2 target-legality matrix** — one parameterized validator case per row, `test_v2_target_legality_matrix`:

| Goal | Target | Rule |
|---|---|---|
| v1 (all goals) | unchanged | v1 validation and semantics are untouched. |
| `collect_items` | required | A non-null target is mandatory; reject a null target. |
| `flee_to_upstairs` | null or target | Null resolves the nearest reachable observed upstairs deterministically; a supplied target must resolve to known upstairs locally. |
| `explore_frontier` | null only | A destination-selecting goal (selects its destination locally during resolution; not coordinate-bearing); reject a non-null target. |
| `search_dead_ends` | null only | A destination-selecting goal (selects its destination locally during resolution; not coordinate-bearing); reject a non-null target. |
| `descend_known_stairs` | optional | Null selects stairs by existing priority; a supplied target must resolve to a known stairs square. |
| no positional goal | must be null | Reject a non-null target when no positional goal is present. |

At command resolution, reject/defer semantically unsupported targets with a structured reason rather than treating coordinate validation as evidence validation. Collection requires observed item evidence; retreat requires known upstairs; destination-selecting goals resolve locally against legal known geometry or an observed closed door; only `collect_items`/`flee_to_upstairs`/`descend_known_stairs` carry or resolve explicit coordinates. If no explicit destination is present, retain current goal modifiers. A new modifier-only set does not churn a default commitment; replacing a directive-owned target with advice that no longer authorizes it releases it.

### 2.2 End-to-end path

Update together:

- `directives.py`: version-specific closed validators, exact new goals, immutable view accessors and unchanged DirectiveBook applicability authority.
- `providers.py`: static v2 schema prompt, destination semantics, role/inventory/need-aware pickup guidance, unknown BUC/safety warnings; ordinary provider validation continues to be the only gateway.
- `controller.py` and `evaluate.py`: matching frozen contexts; command-only destination resolution after non-command continuations; directive generation and completion/failure lifecycle parity; one shared command-boundary activation ordering (service/settle → activate → view → prepare/select).
- `presentation.py`: new closed goal-summary entries and route-purpose descriptions, no raw explanation-as-instructions.
- recording/evaluation readers: accept both versions and preserve historical version fields; missing new telemetry means unavailable, not zero.

**Evaluator directive-scoping parity (explicit Phase 2 substep with call sites).** Capture `_strategy_instance` at dispatch; reject a source-instance mismatch before activation; call `book.activate(..., instance=current_instance)`; and pass the same instance to every `book.view`/`peek_view`. Live already scopes this way (`tools/agent/controller.py:1947-1950,2000-2032,2056-2057,2653-2656`); the evaluator currently does not (`tools/agent/evaluate.py:909-925,967-980,988-1018`). This keeps destination lifecycle events and active-view expiry identical between live and replay.

**Common pending-directive activation ordering (explicit Phase 2 substep, both call sites).** Live and evaluator must apply pending destination advice in the *same* order at a command boundary: **service/settle strategy → activate eligible pending advice at the command boundary → build the directive view → prepare/select the action**, with emergency-singleton precedence preserved inside policy. Today the two disagree. Live calls `_decide()` and builds the reflex/Jev proposal *before* `_activate_pending_directives()` (`tools/agent/controller.py:2633-2654`), and `_decide` builds its context from the pre-activation `book.view` (`tools/agent/controller.py:2771-2818`); the evaluator does the reverse — services strategy, activates pending directives, and *then* builds the view/context and proposes (`tools/agent/evaluate.py:1013-1029`). The consequence is that a newly returned v2 destination can steer the activation-turn action in the evaluator but cannot in live, contradicting AC7 and AC15. Fix live so `_activate_pending_directives()` runs *before* `_decide()`'s context build, so `_decide` sees post-activation views; keep the evaluator's existing order but give it the instance guards and the v2 destination command gate (§1.5 "One application rule"). After this change, a newly activated v2 destination affects the same command decision in live and evaluator alike. Activation updates DirectiveBook authority **before** preparation, so `_decide` sees post-activation views. **Emergency-preemption exception (matching §1.5):** where an emergency or mandatory singleton preempts the destination pipeline on the activation command, the prior destination is **suspended** and **no replacement is committed on that command**; the newly active destination is resolved and compare-and-applied on the **next non-preempted genuine command decision**. The shared ordering therefore fixes *which* command decision a newly activated destination affects; it does not license overriding emergency precedence, which remains authoritative inside policy in both live and evaluator.

**Delivery repair must not activate pending advice (explicit Phase 2 substep, live and evaluator).** Activate pending advice **only on the fresh-selection branch** (`repair is None`) **before `_decide()` builds its context**, so a newly activated destination steers that fresh decision. Never activate or consume pending advice during `_resend_repair`: the current unconditional activation call at `tools/agent/controller.py:2653` sits *after* repair handling in the same path and must be **gated**, so a delivery-repair (incomplete-retry) pass leaves pending advice untouched and **preserved until the next fresh command decision**, at which point it activates normally. Mirror the distinction in the evaluator: wherever incomplete retries/delivery repair are represented in evaluator modeling, the evaluator must not activate or consume pending destination advice on that branch — it activates only on the fresh-selection branch before building its decision context.

Do not increase strategy call frequency or budgets as part of this feature. The supplied 17 calls / 16 applied boundary EIDs suggests measurement of **steering** is missing, not that more calls are necessarily needed. Do not infer directive-set activation counts from that field anywhere. Retain existing scheduling and report validated, activated, destination-resolved, first action sent, reached/failed/expired as separate events.

### 2.3 Context and cache invariants

DeepSeek needs coordinate-bearing item evidence, known upstairs, current commitment, configured role, visible conditions, and inventory freshness. Add bounded deterministic subcontent **within existing snapshot blocks**: item/target/commitment evidence under `map`, freshness within `inventory`, visible conditions in `status`. Preserve block order, the leading trust label, episode-static role header, and final remaining-budget line. Do not append volatile commitment facts to the static system message or before the stable role header.

Use the same display-appearance interpretation and player-visible evidence helper for policy and model context; do not import presentation as the policy's fact source. Keep first-40 inventory and last-six message bounds. Clearly mark a cached inventory's age/unknown freshness and observed versus remembered target evidence. Missing facts remain unknown.

Continue freezing the complete request once, computing reservation from those exact bytes, evicting complete historical pairs transactionally, retaining validated assistant text verbatim, and never rerendering old snapshots. Existing recordings remain readable; new system schema text intentionally changes the new-request prefix once.

## 3. Situational and build-aware pickup

### 3.1 Recommended split

**DeepSeek owns off-route collection decisions.** It can direct `collect_items` based on hunger, role, known equipment/inventory, visible conditions, resource shortages, and item evidence. No speculative full role/equipment table is needed.

**Jev owns optional on-route acquisition** when the hero is already on a supported item site and there is no urgent singleton or explicit retreat. Offer exactly a pickup action and the held-route continuation. Add configured role and inventory freshness to Jev state; existing HP/hunger/conditions/inventory/room evidence remain authoritative. A weapon appearance for a Valkyrie is a reason to inspect utility, not proof it is an upgrade; unknown quantity/weight/BUC stay unknown.

**Scripted fallback is conservative.** Preserve existing hunger/eat precedence, including recognized floor food. If hungry, no usable cached food is available, and a fresh location-bound exact recognized ration name supports acquisition, pickup can be a narrow scripted fallback when the existing eat path cannot serve the need. A `%` glyph alone does not establish edible/safe food and is not an urgent singleton. Ambiguous loot and gold are not mandatory reflex pickups; without an explicit collection directive or Jev selection, continue the committed route.

This deliberately avoids a blanket 'always pick up gold' patch. Gold pacing is first a navigation commitment/exhaustion problem; optional utility judgment must not recreate the loop.

### 3.2 Evidence and authorization

Use a small proposed `tools/agent/pickup.py` helper (new path) for pure floor evidence/row filtering plus bounded instance-scoped attempt records; no engine imports. Reuse `instances.display_appearance` and existing exact-name recognition where appropriate.

A floor evidence token contains instance, coordinate, appearance category, observed message/row binding where available, and a `source_epoch`. Define `source_epoch` as the observation that established or **materially refreshed** the item evidence: hero-overlay-retained evidence carries the **unchanged** source epoch; only a newly displayed item/row, or a location-bound current message carrying materially different evidence, creates a new epoch token. Unrelated observations, the hero overlay itself, time, inventory refresh, and global map revision must **not** reset the evidence epoch, and therefore must **not** reset the two-attempt bound (§3.3). The hero hides the floor glyph, so retain a just-observed destination item appearance when confirmed movement reaches that coordinate; label it as last-seen evidence until a fresh floor message/menu confirms it. Do not bind an arbitrary recent 'You see here' message to the current hero after subsequent movement. Bind such text only through the action's reconciled arrival/current-location observation.

An appearance authorizes at most inspection/acquisition consideration. It never establishes BUC, safety, monster disposition, exact type, ownership, quantity, weight, or equipment quality. Retain wording equivalent to 'Recognized food; no safety guarantee' in model-facing descriptions; existing known-safe food rules remain the separate eating authority.

Adjacent appearances are context, not automatic detours. They become destinations only through an explicit strategy directive or the narrow urgent-food case, validated against known-safe terrain. Item glyphs alone do not make unknown ground traversable.

### 3.3 Pickup protocol and bounded resolution

Implement a real `pickup` intent using existing frozen-effect machinery:

1. At an eligible location, locally construct `KEY_PICKUP` with a frozen location/evidence token and acquisition purpose. A collection directive makes this the terminal singleton after safety checks; opportunistic pickup competes only with committed-route continuation.
2. Follow only the actual need/prompt. Never send comma as a direction response. Consume/reject stale menu generations as existing candidates do.
3. Jev is command-only and never chooses menu rows. At a command decision on a supported item site the offered Choice is exactly the `pick-up` action and the committed-route continuation; that command-need choice is consistent with the existing presentation boundary and adds no `menu`-need provider path. The pickup intent then locally filters the resulting menu: it selects a **uniquely authorized exact row** — exact recognized food for urgent hunger, or a single bound row for a targeted `collect_items` — and **cancels** a broad or ambiguous pile rather than model-choosing among row candidates. A directive authorizes the inspection attempt, not indiscriminate acquisition of an entire stack. Repeated bounded single-row acquisitions may collect several useful items.
4. Preserve uncertainty and decline unsupported yes/no prompts, unknown pickup menus, rows explicitly marked unpaid, or capacity/burden prompts; do not add purchase/theft or burden overrides. Where a command automatically acquires a single object entry/stack at its full quantity without a menu, record that inherent uncertainty rather than claim preselection prevented it (see §3.5).
5. Treat an observed success message, relevant inventory change, or fresh no-items evidence as outcomes. Sending comma or losing a glyph beneath the hero is not proof of collection.

**Decision (deferred non-goal):** a Jev menu-choice path — extending presentation and providers to `menu` needs with multi-candidate preparation so Jev could pick among explicit rows — is deliberately **not** implemented here. Jev remains command-only. Any such path would require its own design and operator-approval phase; this plan neither assumes nor enables it.

Initial retry bound: at most two fully sent and reconciled pickup initiations per unchanged site evidence token, including a no-time failure or canceled inspection. Prompt continuations are not extra initiations. Local rejection/write failure do not count. A successful inventory-changing pickup may expose remaining items but does not reset the site's budget merely because an inventory row changed. Fresh displayed replacement/remaining-item evidence can establish a new bounded token. Inventory is invalidated/refreshed through existing cooldown mechanisms, not forced indefinitely.

If Jev or scripted fallback chooses continue over pickup, record that evidence token as declined for the current default visit/commitment; do not re-offer it on every subsequent tick. A later explicit collection directive may reopen a mere decline, but cannot erase a no-items/capacity/refusal negative without relevant new evidence.

A pickup detour at the hero's square suspends the exploration commitment and resumes it after resolution. Explicit retreat suppresses opportunistic acquisition entirely.

### 3.4 Gold pathology

Use three independent controls:

- held movement cannot oscillate toward whichever gold-overlaid frontier currently wins;
- serviced frontier evidence prevents unchanged gold sites from being repeatedly re-elected;
- pickup/decline/negative outcomes are bounded and evidence-scoped.

Do not permanently mark a looted square unwalkable. Routes may legitimately cross it. What is prohibited is treating stale loot or an already-serviced frontier as a recurring destination, not using necessary transit floor.

### 3.5 Shop and single-object-entry acquisition guarantee

The guarantee is deliberately narrow and honest rather than a general fail-closed claim:

- the pickup intent never selects a menu row explicitly marked **unpaid** and never initiates a purchase;
- later purchase or encumbrance (burden/capacity) prompts are declined;
- **direct single-object-entry acquisition under `AUTOSELECT_SINGLE` (`src/pickup.c:759-788`) is an acknowledged residual uncertainty.** With the pinned profile (`test/agent/gen_profile.py:427`), a lone object entry/stack on the hero's square can be auto-selected at its **full quantity** with no menu (`src/pickup.c:1012-1015,1072-1076` sets the selected count to `last->quan`), and no public shop/ownership evidence exists before the command is sent. The agent cannot inspect a menu that is never presented, so it cannot guarantee declining an unpaid object entry/stack on that path.

The residual risk is bounded to a single object entry/stack (at its full quantity) per command on a supported site, is recorded as `unknown`/unproven evidence rather than a success claim, and is carried in Risks (§10).

## 4. Failure containment and fallback

Failed or exhausted targets are suppressed under their local evidence signature; clear the active destination and acquire the next eligible one. A locked door must not monopolize the agent. A temporarily occupied route may become eligible after occupancy changes, but never route through the occupant merely because the strategy says to flee.

When no exploration target remains, reuse current bounded search, ordinary/forced-search accounting, cycle escape, and graceful exhaustion behavior. Do not reset search budgets on destination changes. Keep descending as an existing scripted singleton/known-stair fallback; do not broaden this into a new descent strategy or new transition machinery. The campaign's 'nobody descends' problem remains a separately measured follow-up.

There is no provider dependency for default commitment. DeepSeek unavailable/invalid/expired means default policy; Jev unavailable/low-confidence means conservative pickup fallback plus committed movement. Failure must never create an unbounded wait, pickup loop, forced-search bypass, or key from strategy text.

## 5. Observability and validation report

Extend existing recording/event and exploration reporting surfaces (`tools/agent/recording.py`, `events.py`, `exploration_metrics.py`, live controller and evaluator; paths verified, exact insertion points to inspect during implementation).

Record deterministic, nonsecret events keyed by commitment serial/instance:

- acquired/replaced/suspended/resumed/reached/failed/expired and closed reason enum;
- source and directive generation; purpose and coordinates;
- selected navigation steps, meaningful progress, held length, replans, reversal required;
- pickup offered/declined/attempted/succeeded/no-items/canceled/refused/unknown;
- eligible strategy destination activation, successful resolution, first action sent, and terminal outcome.

Define metrics explicitly:

- commitment length: selected movement/interaction attempts from acquisition to termination; report median, p90, and terminal reasons;
- destination-switch rate: unexplained switches per navigation decision, expected zero in deterministic holds;
- directive override execution rate: activated explicit destination generations with at least one reconciled destination action divided by eligible resolved generations; list unresolved/expired-before-action separately;
- target reach rate and steps-to-first-unvisited-cell;
- movement revisit rate and visited/discovered-cell coverage using the same existing definitions;
- pickup outcomes, repeated site attempts, and unresolved inspections.

Do not reinterpret the old `directives_applied` counter (it counts applied boundary EIDs, §Verified facts). Count separately: strategy calls, accepted responses, DirectiveBook activations/sets, applied boundary EIDs, eligible destination generations, resolved destinations, and first reconciled destination actions. Do not put ids, scores, wall times, or internal logs into Jev Choice keys. Old artifacts missing new fields report 'unavailable'. Offline replay tests lifecycle/decision consistency, not counterfactual campaign survival; changed actions invalidate claims that replay proves live exploration improvement.

## 6. Phased implementation handoff

### Phase 0 — Native pickup-shape test infrastructure

Build the concrete engine-side probe described here **before** any pickup behavior depends on it. `make -C test/agent check` is engine-free — it runs the Python suites and cannot compile `win/agent/winagent.c` or `src/pickup.c`. Native probes are separate opt-in targets that require built workers (`test/agent/Makefile:117-197`), and the `winagent.c` built-ins cannot drive `dopickup` directly. Add an engine-side pickup probe — a new `AGENT_TEST` pickup case in the worker test surface, or a dedicated native driver target `make -C test/agent native-pickup` — that constructs deterministic wizmode/test levels and drives the real `dopickup`. Reference the menu mechanics in `win/agent/winagent.c:2463-2525` and the `dopickup` flows in `src/pickup.c:759-788`. Cover at least:

- no object present;
- one object entry/stack with `AUTOSELECT_SINGLE` auto-selection (no menu), asserting the **full stack quantity** (`src/pickup.c:1012-1015,1072-1076` sets the selected count to `last->quan`), not one unit;
- multi-row `PICK_ANY` menu with title, mode, and row set;
- cancellation;
- success with the resulting inventory delta;
- unpaid/shop row annotation;
- capacity/burden prompt;
- stale menu generation.

**AC17/AC18 partition (explicit).** AC17's pass condition is **the native target passing**, not a Python row-model test. AC17 requires the native target to **pass for every shape deemed deterministically constructible** after this Phase 0 investigation, and where a shape is not constructible it requires the native target to **fail or mark that shape explicitly unsupported rather than silently skip it** — a silently skipped shape is not a pass. AC18 covers **each explicitly named unsupported shape** with a **mandatory operator-gated manual result**. No shape may be dropped: every one of the eight shapes must appear **exactly once** in either the native-passed set or the manual-required set (asserted by `test_pickup_shape_disposition_is_exhaustive`). Keep the pure Python row-model tests separate (they check the row model, not protocol shape); flag any infeasible shape in Risks; row-model tests alone must not claim protocol-shape verification. AC17/AC18 cover this infrastructure itself.

### Phase 1 — Contract fixtures and navigation foundation

Add fixtures reproducing adjacent unvisited corridor, repeated visited frontier, and door approach/opening. Extend TargetStore and target resolution. Pass persistent terrain through routing and safety paths with a documented conservative compatibility fallback only for direct unit callers lacking it. Implement pure proposals, frozen commitment payloads, and reconciled mutation. Replace `_navigation_candidates` with the pure `resolve_destination` → `route_held_destination` → `build_terminal_or_step_candidates` pipeline (§1.3) and replace the singleton `_noncommand_candidate` contract with a need-specific builder; update all callers and tests in this phase. Keep providers/schema untouched initially.

Exit: deterministic commitment, door completion, negative-target suppression, anti-backtrack composition, and cycle recovery tests pass.

### Phase 2 — Directive v2 and live/replay parity

Implement dual-version validator and prompt schema, semantic target resolver, command-only destination application, generation served/failed guards, TTL/instance invalidation, and upstairs arrival semantics. Update presentation goal summaries and evaluator/recording version handling. Add context evidence inside the existing cache block order. Add the evaluator directive-scoping-parity substep (§2.2): capture `_strategy_instance` at dispatch, reject a source-instance mismatch before activation, and thread the instance through `book.activate`/`book.view`/`peek_view`. Also implement the shared pending-directive activation ordering (§2.2): in live, move `_activate_pending_directives()` ahead of `_decide()`'s context build so `_decide` sees post-activation views (`tools/agent/controller.py:2633-2654,2771-2818`); the evaluator already activates before building its view/context (`tools/agent/evaluate.py:1013-1029`) but must gain the instance guards and the v2 destination command gate. Preserve emergency-singleton precedence inside policy in both. Also implement the delivery-repair gating (§2.2): gate the activation call at `tools/agent/controller.py:2653` (which sits after repair handling) so pending advice activates only on the fresh-selection branch (`repair is None`) before `_decide()` builds its context, and is never activated or consumed during `_resend_repair`; pending advice is preserved until the next fresh command decision. Mirror the fresh-selection-only activation in the evaluator wherever incomplete retries/delivery repair are represented. The emergency-preemption exception of §1.5/§2.2 governs this ordering: a preempting emergency/mandatory singleton suspends the old destination and commits no replacement on that command.

Exit: old v1 fixtures round-trip; targeted directives replace held destinations; stale/expired advice cannot create effects; a newly activated v2 destination steers the same command decision in live and evaluator; live/replay tests agree.

### Phase 3 — Pickup intent, contextual choice, and Jev command-only wiring

Implement floor evidence binding, narrow urgent-food fallback, strategy arrival inspection, the command-need pickup-versus-continuation Choice, local exact-row selection with conservative pile cancellation, bounded outcomes, and the narrow shop/single-object-entry guarantee (§3.5). Add Jev role/freshness/commitment context and bump presentation version to `/3` in existing allowlisted metadata only. Jev remains command-only; no `menu`-need provider path is added.

Run the Phase 0 native pickup-shape scenarios (`make -C test/agent native-pickup` or the equivalent `AGENT_TEST` pickup case) against this behavior; AC17 passes only when the native target itself passes. If a shape is not deterministically producible, the operator-gated manual probe for that shape becomes mandatory (AC18) and row-model tests must not claim protocol-shape verification.

Exit: no menu blind-confirm, no glyph-to-safety inference, no repeated stale pickup loops, no Jev menu-row choice path, and retained Choice identity/order invariants hold; the native pickup target passes (AC17) or the mandatory operator-gated manual probe is recorded for any explicitly named infeasible shape (AC18).

### Phase 4 — Metrics, regression, and controlled validation

Add lifecycle metrics and report fields. Run the complete agent auto suite; compare against the caller-reported 907 baseline and document every intentionally changed expectation (especially former multi-target navigation candidates, normal stair-first target selection, and presentation version). Do not simply weaken recovery/forced-search assertions.

Perform deterministic scripted and fake-provider integration episodes, then an operator-approved live comparison using matched configurations/seeds and identical provider/call budgets. Record coverage, revisit rates, terminal outcomes, target failures, pickup outcomes, Jev application changes, and directive execution. Report variance and regression cases; do not promise a numerical live improvement without data.

### Contract migration checklist

Name each existing test/fixture whose committed contract changes, with old vs new expectation. Implementers add any further entry discovered during implementation, each with old/new expectation:

- `test/agent/test_auto_navigation.py::test_farther_reachable_stair_is_chosen_when_nearer_is_isolated` (`test_auto_navigation.py:202-214`): **old** asserts that an unreachable nearer stair is excluded and the farther reachable stair wins under stair-first scoring; **new** preserves the unreachable-target exclusion while default destination selection prefers a reachable door/frontier before stair unless explicit stair advice or the descent fallback applies; keep a dedicated reachable-farther-stair test under explicit-stair/fallback semantics rather than as a default-selection expectation.
- Replay fixtures assuming one candidate per frontier direction (`test/agent/test_auto_replay.py:630-634`): **old** one navigation candidate per frontier direction; **new** a single committed-destination continuation candidate (plus the permitted pickup alternative), with updated expected candidate counts and identity.
- Presentation version metadata: **old** `jev-presentation/2`; **new** `/3`, recorded only in allowlisted metadata (guarded by `test_presentation_v3_recorded_only_in_allowlisted_metadata`).

## 7. Numbered acceptance criteria

AC1. In a static legal fixture, one destination remains unchanged until arrival or a specified invalidation, despite alternate score/visit changes; only its route is recomputed.

AC2. Default acquisition chooses a reachable frontier/door before down-stair routing, with unvisited cells as fallback; explicit known-stair advice and existing descent singleton remain functional subject to active explicit-destination precedence.

AC3. A required committed reversal survives unrelated same-family >40 suppression; uncommitted anti-backtrack retains its exact strict-margin and rejected-alternative behavior.

AC4. Observed cycle/recovery, instance changes, hard blockage, and bounded stall exhaustions retire the appropriate destination and cannot immediately recreate the same failed loop.

AC5. Door commitment persists through approach and completes only on observed terminal evidence; locked/refused/ineffective doors have bounded attempts and do not suppress other reachable rooms forever.

AC6. Preparing, rendering, rejecting, failing to send, or not selecting a candidate cannot acquire a target, spend a pickup attempt, or commit progress. Reconciliation commits exactly once.

AC7. Valid v2 `collect_items` and `flee_to_upstairs` directives structurally override default commitment at a genuine command boundary, and a newly activated v2 destination affects the same command decision in live and evaluator alike (one shared ordering: service/settle strategy → activate eligible pending advice → build the directive view → prepare/select the action). Mandatory continuations and emergency singletons remain higher priority: **where an emergency or mandatory singleton preempts the destination pipeline on the activation command, the old destination is suspended, no replacement is committed on that command, and the newly active destination is resolved and compare-and-applied on the next non-preempted genuine command decision** (the §1.5/§2.2 emergency-preemption exception). Pending advice is activated only on the fresh-selection branch (`repair is None`) *before* the decision context is built; it is never activated or consumed during delivery repair (`_resend_repair`) and is preserved until the next fresh command decision (the §2.2 delivery-repair rule).

AC8. Directive eligibility remains instance/level/TTL/precondition governed. Served or failed generations do not reassert each tick; repeats cannot reset identical failure evidence. Fleeing reaches known upstairs without automatically ascending or exiting.

AC9. v1 recordings/directives retain their accepted shape and semantics; v1 rejects v2-only goals. v2 rejects malformed/ambiguous/untrusted wire content. Rendering, provider validation, replay, and summaries recognize both versions consistently.

AC10. Pickup judgment has role, hunger/HP/conditions, cached inventory freshness, and displayed evidence available. Glyphs never imply BUC/safe food/upgrades; adjacent ambiguous items do not become reflex detours.

AC11. Pickup initiation, menu/yes-no continuation, no-item/refusal, success, and cancellation are correctly distinguished; attempts are bounded by evidence and instance. Jev is never offered individual pickup-menu rows: at a command decision the Choice is only `pick-up` versus route continuation, and the pickup intent selects a uniquely authorized exact row and cancels broad/ambiguous piles rather than model-choosing. The intent never selects a row explicitly marked unpaid and declines later purchase/encumbrance prompts; direct single-object-entry/stack `AUTOSELECT_SINGLE` acquisition is recorded as an acknowledged residual uncertainty, not a prevented case. Gold fixtures either collect once or continue without recurring destination/pickup loops.

AC12. Item-overlay paths use persistent known terrain; unknown terrain under a glyph remains unknown. Canonical appearance markers and immediate-destination clauses retain their evidence precedence.

AC13. Choice criteria remain an insertion-ordered object with exactly one key/index per retained member and unchanged N. Presentation never adds/drops/reorders candidates; changed policy identity is deterministic and deliberately tested.

AC14. DeepSeek block order, final budget line, frozen historical bytes, reservation, history settlement, and cancellation invariants remain intact.

AC15. Live/evaluator behavior and lifecycle event definitions agree, including that a newly activated v2 destination affects the same command decision in live and evaluator alike (§2.2 shared activation ordering). Report distinguishes directive activation from steering and pickup attempt from outcome; missing old fields are not manufactured zeros.

AC16. All existing auto suites pass or have individually justified expectation updates; named new regression and mutation checks pass. Live claims are accompanied by a validation report, not inferred from unit/replay success.

AC17. Native pickup-shape scenarios run deterministically through the engine-side pickup probe (§6 Phase 0 — the new `AGENT_TEST` pickup case or the `make -C test/agent native-pickup` target), and **AC17's pass condition is that native target passing**, not a Python row-model test. It covers all eight listed shapes: no object; single-object-entry `AUTOSELECT_SINGLE` (asserting full stack quantity); multi-row `PICK_ANY` (title/mode/rows); cancellation; success with inventory delta; unpaid/shop annotation; capacity/burden prompt; stale menu generation. For the partition with AC18: the native target must **pass for every shape deemed deterministically constructible** after the Phase 0 investigation, and for any non-constructible shape it must **fail or mark that shape explicitly unsupported rather than silently skip it** — skipping is not a pass, and every one of the eight shapes must be dispositioned exactly once across AC17's native-passed set and AC18's manual-required set. Pure Python row-model tests remain separate.

AC18. Covers **each explicitly named shape that AC17 deems non-constructible**: the plan names each such shape explicitly and requires a **mandatory operator-gated manual result** for it; the validation report records the native-vs-manual disposition of all eight shapes (each appearing exactly once, asserted by `test_pickup_shape_disposition_is_exhaustive`), and row-model-only tests must not claim protocol-shape verification.

## 8. Named-test strategy

The following are proposed exact test names, not claims that tests already exist. Place them in the existing suites noted, with new `test/agent/test_auto_commitment.py` and `test/agent/test_auto_pickup.py` as proposed new paths. Existing forced-search, gate/nav, provider, replay, integration, and presentation suites remain mandatory regression coverage.

### Commitment (`test_auto_commitment.py`, plus existing navigation/recovery suites)

- `test_default_commits_door_or_frontier_before_stair`
- `test_unvisited_fallback_after_serviced_frontiers`
- `test_destination_survives_alternate_score_and_visit_changes`
- `test_destination_survives_frontier_reclassification_en_route`
- `test_one_dijkstra_replans_route_not_destination`
- `test_serviced_frontier_requires_local_evidence_change`
- `test_door_commitment_survives_approach_and_open_prompt`
- `test_locked_door_fails_once_and_next_target_progresses`
- `test_ineffective_door_attempts_are_bounded`
- `test_committed_reverse_survives_same_family_margin`
- `test_uncommitted_reverse_preserves_strict_40_boundary`
- `test_rejected_alternative_cannot_suppress_required_reverse`
- `test_cycle_invalidates_and_suppresses_same_destination`
- `test_long_route_stall_budget_bounds_nonperiodic_loop`
- `test_emergency_suspends_then_revalidates_destination`
- `test_inventory_and_hunger_do_not_spend_route_budget`
- `test_instance_change_clears_commitment_and_negatives`
- `test_item_overlay_uses_persistent_known_ground`
- `test_item_on_unknown_ground_does_not_authorize_route`
- `test_no_targets_reuses_bounded_forced_search_accounting`
- `test_prepare_and_unselected_candidate_do_not_commit_destination`
- `test_destination_render_does_not_mutate_commitment_state`: rendering a destination-state snapshot (commitment purpose/phase/route/attempt summary) leaves the commitment record and its scoped ledgers unchanged, mirroring the presentation-level retained-table purity test.
- `test_write_failure_and_local_rejection_do_not_commit_destination`
- `test_reconciled_destination_effect_commits_exactly_once`
- `test_same_key_different_destination_has_distinct_effect_identity`
- `test_stationary_frames_do_not_reset_attempt_budget`
- `test_menu_prompt_frames_do_not_reset_attempt_budget`
- `test_inventory_refresh_does_not_reset_attempt_budget`
- `test_departure_return_without_new_evidence_keeps_negative`
- `test_explicit_stair_directive_selects_stair_destination`
- `test_on_stair_descent_singleton_unaffected_by_commitment`
- `test_hard_blockage_retires_and_suppresses_destination`

### Reconciliation (`test_auto_commitment.py`, live + evaluator)

- `test_one_hop_acquisition_records_reached_without_installation`
- `test_stale_continuation_after_cycle_invalidation_does_not_resurrect`
- `test_door_no_time_outcome_folds_once`
- `test_reconciliation_parity_live_and_evaluator`

### Directives/providers/integration/replay (existing `test_auto_providers.py`, `test_auto_wiring.py`, `test_auto_integration.py`, `test_auto_replay.py`)

- `test_v1_directive_roundtrip_preserves_version_and_defaults`
- `test_v1_rejects_v2_destination_goals`
- `test_v2_requires_collect_coordinate_and_unambiguous_positional_goal`
- `test_v2_rejects_wire_fields_bool_version_and_invalid_coordinates`
- `test_collect_directive_replaces_default_destination`
- `test_flee_resolves_nearest_reachable_known_upstairs`
- `test_flee_arrival_does_not_ascend_or_exit_dungeon`
- `test_pending_direction_does_not_apply_new_destination`
- `test_directive_expiry_releases_owned_destination`
- `test_served_generation_does_not_reassert_destination`
- `test_identical_new_generation_does_not_reset_failure_budget`
- `test_stale_instance_directive_never_creates_destination_effect`
- `test_strategy_prompt_v2_and_summary_goal_maps_match_validator`
- `test_strategy_context_item_coordinates_conditions_and_inventory_age`
- `test_strategy_render_order_and_final_budget_line_unchanged`
- `test_strategy_historical_bytes_not_rerendered_after_commitment_change`
- `test_live_replay_destination_and_pickup_effect_parity`
- `test_legacy_recording_without_commitment_fields_remains_readable`
- `test_replay_reads_v1_and_v2_directives_with_original_versions`: replaying a mixed v1/v2 artifact reads each directive with its originally accepted version (no normalization), and rendering/provider-summary/replay recognize both versions consistently.
- `test_v2_target_legality_matrix`
- `test_pending_key_does_not_apply_new_destination`
- `test_menu_or_yn_need_does_not_consume_pending_destination`
- `test_incomplete_delivery_repair_does_not_activate_or_consume_pending_destination`: a delivery-repair (incomplete-retry) pass neither activates nor consumes pending destination advice, which remains preserved; a subsequent fresh command decision (`repair is None`) then activates it normally.
- `test_emergency_singleton_precedes_destination_application`: asserts that when an emergency or mandatory singleton preempts the destination pipeline on the command that activates a new destination, **no replacement payload commits during that emergency command** (the old destination is suspended, not replaced), and the new directive **replaces the old target on the following eligible, non-preempted genuine command decision**, with compare-and-apply checked against the serial/identity captured at proposal time.
- `test_evaluator_rejects_same_level_instance_change_before_activation`
- `test_evaluator_active_view_expires_after_instance_change`
- `test_destination_lifecycle_events_parity_live_evaluator`
- `test_pending_collect_items_steers_same_command_live_and_evaluator`
- `test_pending_flee_to_upstairs_steers_same_command_live_and_evaluator`
- `test_modifier_only_legacy_advice_parity_live_and_evaluator`
- `test_level_change_expires_destination`
- `test_ttl_expiry_releases_destination`
- `test_precondition_failure_releases_destination`

### Pickup (`test_auto_pickup.py`)

- `test_hungry_exact_ration_allows_narrow_pickup_fallback`
- `test_food_appearance_alone_never_asserts_safe_food`
- `test_role_inventory_and_conditions_reach_pickup_judgment`
- `test_adjacent_ambiguous_item_does_not_redirect_default_route`
- `test_current_arrival_message_binds_item_to_location`
- `test_old_floor_message_cannot_bind_after_movement`
- `test_hero_overlay_retains_last_seen_evidence_without_claiming_presence`
- `test_opportunistic_pickup_preserves_exploration_destination`
- `test_retreat_suppresses_opportunistic_pickup`
- `test_pickup_menu_selects_only_bound_authorized_row`
- `test_pickup_menu_selects_unique_authorized_row`
- `test_broad_ambiguous_pile_is_cancelled_not_model_chosen`
- `test_unpaid_rows_and_capacity_prompts_are_declined`
- `test_single_object_entry_autoselect_is_acknowledged_uncertainty`
- `test_pickup_attempt_limit_counts_reconciled_initiations_only`
- `test_no_items_negative_survives_repeated_directive`
- `test_declined_pickup_not_reoffered_for_unchanged_evidence`
- `test_new_item_evidence_reopens_bounded_attempts`
- `test_gold_site_collect_or_decline_never_recreates_pacing_loop`
- `test_pickup_initiation_recorded_distinct_from_outcome`
- `test_yes_no_refusal_is_decline_not_failure`
- `test_no_items_evidence_terminates_target`
- `test_confirmed_pickup_success_records_inventory_delta`
- `test_deliberate_cancellation_terminates_site`

### Native pickup-shape infrastructure (engine-side probe, Phase 0)

- `make -C test/agent native-pickup` (or the equivalent `AGENT_TEST` pickup case) — the native target whose passing is AC17's pass condition: one case per shape, asserting real `dopickup` behavior and full stack quantity for `AUTOSELECT_SINGLE`, and **failing or marking a shape explicitly unsupported rather than silently skipping it** when a shape cannot be constructed deterministically.
- `test_pickup_row_model_shapes_are_self_consistent` (pure Python row-model test in `test_auto_pickup.py`; checks the row model only and never claims protocol-shape verification).
- `test_pickup_shape_disposition_is_exhaustive`: asserts that each of the eight pickup shapes appears **exactly once** across the native-passed set and the manual-required set — no shape silently skipped, none double-counted, and the union covering all eight (folds the AC17/AC18 partition and the validation report's native-vs-manual disposition).
- `test_pickup_protocol_shape_requires_native_or_manual_probe` (guards that shape verification cites the native target or a mandatory operator-gated manual probe, and that any non-constructible shape is dispositioned in the manual-required set).

### Presentation/metrics (existing `test_auto_jev_presentation.py`, `test_auto_metrics.py`, `test_auto_gate_nav_pin.py`)

- `test_destination_and_pickup_presentation_never_mutate_retained_table`
- `test_pickup_choice_criteria_object_key_index_and_n_frozen`
- `test_pickup_exact_binding_and_appearance_uncertainty_preserved`
- `test_room_markers_and_destination_clause_precedence_unchanged`
- `test_presentation_v3_recorded_only_in_allowlisted_metadata`
- `test_activation_and_override_execution_metrics_are_distinct`
- `test_validation_report_contains_required_fields`: the validation report must contain commit/config identifiers, suite and named-mutation results, the native-vs-manual pickup-shape disposition (which of the eight shapes are native-passed versus manual-required), and live claims explicitly labeled as *measured* rather than inferred; a report missing any required field fails.
- `test_pickup_attempt_and_confirmed_outcome_metrics_are_distinct`
- `test_legacy_missing_commitment_metrics_report_unavailable`

### Mutation checks

Use temporary controlled edits restored after each check. Name the checks in the validation report and require the listed regressions to fail:

- `mutation_drop_held_target_each_tick`: killed by `test_destination_survives_alternate_score_and_visit_changes`.
- `mutation_apply_antibacktrack_to_committed_target`: killed by `test_committed_reverse_survives_same_family_margin`.
- `mutation_change_margin_gt_to_ge`: killed by `test_uncommitted_reverse_preserves_strict_40_boundary`.
- `mutation_complete_door_on_approach`: killed by `test_door_commitment_survives_approach_and_open_prompt`.
- `mutation_reactivate_failed_directive_each_tick`: killed by `test_served_generation_does_not_reassert_destination` and `test_identical_new_generation_does_not_reset_failure_budget`.
- `mutation_commit_during_prepare`: killed by `test_prepare_and_unselected_candidate_do_not_commit_destination`.
- `mutation_reset_pickup_budget_on_inventory_change`: killed by `test_pickup_attempt_limit_counts_reconciled_initiations_only`.
- `mutation_trust_food_glyph_as_safe`: killed by `test_food_appearance_alone_never_asserts_safe_food`.
- `mutation_route_from_raw_grid_under_item`: killed by `test_item_overlay_uses_persistent_known_ground`.
- `mutation_sort_or_drop_choice_member`: killed by `test_pickup_choice_criteria_object_key_index_and_n_frozen`.
- `mutation_rerender_historical_strategy_turn`: killed by `test_strategy_historical_bytes_not_rerendered_after_commitment_change`.

### AC → named-test map

Each acceptance criterion's named tests; a mutation that regresses an AC must fail at least one of them.

| AC | Named tests |
|---|---|
| AC1 | `test_destination_survives_alternate_score_and_visit_changes`, `test_destination_survives_frontier_reclassification_en_route`, `test_one_dijkstra_replans_route_not_destination` |
| AC2 | `test_default_commits_door_or_frontier_before_stair`, `test_unvisited_fallback_after_serviced_frontiers`, `test_no_targets_reuses_bounded_forced_search_accounting`, `test_explicit_stair_directive_selects_stair_destination`, `test_on_stair_descent_singleton_unaffected_by_commitment` |
| AC3 | `test_committed_reverse_survives_same_family_margin`, `test_uncommitted_reverse_preserves_strict_40_boundary`, `test_rejected_alternative_cannot_suppress_required_reverse` |
| AC4 | `test_cycle_invalidates_and_suppresses_same_destination`, `test_long_route_stall_budget_bounds_nonperiodic_loop`, `test_instance_change_clears_commitment_and_negatives`, `test_hard_blockage_retires_and_suppresses_destination` |
| AC5 | `test_door_commitment_survives_approach_and_open_prompt`, `test_locked_door_fails_once_and_next_target_progresses`, `test_ineffective_door_attempts_are_bounded`, `test_door_no_time_outcome_folds_once`, `test_stale_continuation_after_cycle_invalidation_does_not_resurrect` |
| AC6 | `test_prepare_and_unselected_candidate_do_not_commit_destination`, `test_write_failure_and_local_rejection_do_not_commit_destination`, `test_reconciled_destination_effect_commits_exactly_once`, `test_same_key_different_destination_has_distinct_effect_identity`, `test_one_hop_acquisition_records_reached_without_installation`, `test_destination_render_does_not_mutate_commitment_state`, `test_destination_and_pickup_presentation_never_mutate_retained_table` |
| AC7 | `test_collect_directive_replaces_default_destination`, `test_flee_resolves_nearest_reachable_known_upstairs`, `test_pending_direction_does_not_apply_new_destination`, `test_pending_key_does_not_apply_new_destination`, `test_menu_or_yn_need_does_not_consume_pending_destination`, `test_incomplete_delivery_repair_does_not_activate_or_consume_pending_destination`, `test_emergency_singleton_precedes_destination_application`, `test_pending_collect_items_steers_same_command_live_and_evaluator`, `test_pending_flee_to_upstairs_steers_same_command_live_and_evaluator`, `test_modifier_only_legacy_advice_parity_live_and_evaluator` |
| AC8 | `test_flee_arrival_does_not_ascend_or_exit_dungeon`, `test_directive_expiry_releases_owned_destination`, `test_served_generation_does_not_reassert_destination`, `test_identical_new_generation_does_not_reset_failure_budget`, `test_stale_instance_directive_never_creates_destination_effect`, `test_evaluator_rejects_same_level_instance_change_before_activation`, `test_evaluator_active_view_expires_after_instance_change`, `test_level_change_expires_destination`, `test_ttl_expiry_releases_destination`, `test_precondition_failure_releases_destination`, `test_auto_spectate.py::test_ttl_equality_is_eligible` (TTL equality remains eligible per DirectiveBook semantics) |
| AC9 | `test_v1_directive_roundtrip_preserves_version_and_defaults`, `test_v1_rejects_v2_destination_goals`, `test_v2_requires_collect_coordinate_and_unambiguous_positional_goal`, `test_v2_rejects_wire_fields_bool_version_and_invalid_coordinates`, `test_v2_target_legality_matrix`, `test_strategy_prompt_v2_and_summary_goal_maps_match_validator`, `test_replay_reads_v1_and_v2_directives_with_original_versions` |
| AC10 | `test_hungry_exact_ration_allows_narrow_pickup_fallback`, `test_food_appearance_alone_never_asserts_safe_food`, `test_role_inventory_and_conditions_reach_pickup_judgment`, `test_adjacent_ambiguous_item_does_not_redirect_default_route`, `test_old_floor_message_cannot_bind_after_movement` |
| AC11 | `test_pickup_menu_selects_only_bound_authorized_row`, `test_pickup_menu_selects_unique_authorized_row`, `test_broad_ambiguous_pile_is_cancelled_not_model_chosen`, `test_unpaid_rows_and_capacity_prompts_are_declined`, `test_single_object_entry_autoselect_is_acknowledged_uncertainty`, `test_pickup_attempt_limit_counts_reconciled_initiations_only`, `test_no_items_negative_survives_repeated_directive`, `test_declined_pickup_not_reoffered_for_unchanged_evidence`, `test_new_item_evidence_reopens_bounded_attempts`, `test_gold_site_collect_or_decline_never_recreates_pacing_loop`, `test_stationary_frames_do_not_reset_attempt_budget`, `test_menu_prompt_frames_do_not_reset_attempt_budget`, `test_inventory_refresh_does_not_reset_attempt_budget`, `test_departure_return_without_new_evidence_keeps_negative`, `test_pickup_initiation_recorded_distinct_from_outcome`, `test_yes_no_refusal_is_decline_not_failure`, `test_no_items_evidence_terminates_target`, `test_confirmed_pickup_success_records_inventory_delta`, `test_deliberate_cancellation_terminates_site` |
| AC12 | `test_item_overlay_uses_persistent_known_ground`, `test_item_on_unknown_ground_does_not_authorize_route`, `test_room_markers_and_destination_clause_precedence_unchanged` |
| AC13 | `test_pickup_choice_criteria_object_key_index_and_n_frozen`, `test_destination_and_pickup_presentation_never_mutate_retained_table`, `test_presentation_v3_recorded_only_in_allowlisted_metadata` |
| AC14 | `test_strategy_render_order_and_final_budget_line_unchanged`, `test_strategy_historical_bytes_not_rerendered_after_commitment_change`, `test_strategy_context_item_coordinates_conditions_and_inventory_age`, `test_legacy_recording_without_commitment_fields_remains_readable`, plus the existing `test_auto_providers.py` invariants `test_a_cache_price_never_lowers_a_reservation`, `test_history_reflects_only_successful_settlement`, `test_cancellation_adds_no_history` (destination-context variants added if needed) |
| AC15 | `test_live_replay_destination_and_pickup_effect_parity`, `test_reconciliation_parity_live_and_evaluator`, `test_incomplete_delivery_repair_does_not_activate_or_consume_pending_destination`, `test_destination_lifecycle_events_parity_live_evaluator`, `test_pending_collect_items_steers_same_command_live_and_evaluator`, `test_pending_flee_to_upstairs_steers_same_command_live_and_evaluator`, `test_modifier_only_legacy_advice_parity_live_and_evaluator`, `test_activation_and_override_execution_metrics_are_distinct`, `test_pickup_attempt_and_confirmed_outcome_metrics_are_distinct`, `test_legacy_missing_commitment_metrics_report_unavailable` |
| AC16 | `test_legacy_recording_without_commitment_fields_remains_readable`, `test_presentation_v3_recorded_only_in_allowlisted_metadata`, `test_validation_report_contains_required_fields`, the full auto-suite gate, and the §8 mutation checks |
| AC17 | the native target `make -C test/agent native-pickup` (or the equivalent `AGENT_TEST` pickup case) — passing that target is the pass condition for every deterministically constructible shape, and a non-constructible shape must be explicitly marked unsupported rather than skipped; `test_pickup_shape_disposition_is_exhaustive` asserts the eight-shape partition; pure Python row-model coverage is `test_pickup_row_model_shapes_are_self_consistent` |
| AC18 | `test_pickup_shape_disposition_is_exhaustive` (each named unsupported shape has a mandatory operator-gated manual result, every shape dispositioned exactly once), `test_pickup_protocol_shape_requires_native_or_manual_probe` |

## 9. Review and documentation strategy

Persist this plan as `doc/agent-destination-commitment-plan.md`. Amend the existing gate/nav plan's anti-backtrack scope and target persistence discussion, presentation plan for `/3` state additions and closed goal mappings, cache plan for bounded subcontent inside unchanged block ordering, and agent operational docs for directive v2 and retreat-arrival semantics.

Review in three passes:

1. Navigation/lifecycle: deterministic acquisition, distinct door endpoint/approach, serviced-frontier reset rules, legal routes, atomic effect ownership, and recovery budgets.
2. Trust/contracts: v1/v2 compatibility, stale directives, prompt continuation safety, player-visible evidence, no explanation execution, pickup menu binding, frozen Choice identity, cache invariants.
3. Operational evidence: full suite results, mutation results, bounded integration fixtures, and operator-approved live report. Compare failures as well as averages; explain expected Jev consultation-rate changes.

**Post-implementation review loop (required).** After all automatable tests and mutation checks pass, dispatch the prescribed reviewer against the actual diff and AC1–AC18; fix or explicitly rebut every finding; rerun the affected tests; and repeat after any Critical/Important finding until none remain or an operator decision is required. This is the same loop prescribed in `doc/agent-jev-gate-nav-plan.md` §10.

Validation report should list commit/config identifiers, suite counts and exact changed tests, named mutation results, provider/network use, matched campaign conditions, commitment terminal reasons, pickup uncertainty, and outstanding descent behavior. Do not install/rebuild the engine for this agent-only change without a separate need; avoid the destructive install path warned about in AGENTS.md.

## 10. Risks and explicit decisions

- **Fewer Jev navigation choices:** intentional. A held destination cannot coexist with unrestricted model re-election of destinations every step. Optional pickup and later genuine local route alternatives remain possible.
- **Overcommitment to bad targets:** controlled by legal-path validation, local evidence suppression, cycle invalidation, and bounded attempts. Budget constants are initial tunables, not empirically optimal values.
- **Serviced-frontier suppression may hide useful secret-search sites:** scope suppression to unchanged local evidence and keep existing search machinery independent. Never equate 'serviced waypoint' with 'fully explored level'.
- **Raw glyph navigation loses terrain:** use existing persistent terrain; do not solve by declaring every item tile walkable.
- **Directive prompt compatibility:** v2 is a deliberate static prompt change; v1 remains readable. New positional-goal restrictions apply only to v2.
- **Pickup is larger than a key binding:** menu/yes-no outcomes, hero overlays, autopickup, shops, stale messages, and capacity must be tested. Conservative cancellation may forgo useful loot; prefer that to indiscriminate acquisition.
- **Build awareness is model judgment, not a complete optimizer:** role and observed equipment inform advice; no hidden item valuation, BUC inference, or class-specific equip engine.
- **Retreat means arrival, not ascent:** chosen explicitly to avoid unapproved transitions/dungeon exit. Reaching upstairs does not guarantee survival; emergency handling remains authoritative. Automatic ascent requires separate operator approval/design.
- **Metric interpretation:** 16 applied boundary EIDs / 17 calls is not 16 steered ticks. The applied-boundary-EID field is not a directive-set activation count; diagnose real advice usefulness with new lifecycle measures before changing strategy frequency.
- **Single-object-entry autoselect residual uncertainty:** with the pinned profile (`test/agent/gen_profile.py:427`), a lone object entry/stack on the hero's square can be acquired at its full quantity through `AUTOSELECT_SINGLE` (`src/pickup.c:759-788,1012-1015,1072-1076`) with no menu and no pre-command public shop/ownership evidence; the agent cannot inspect a menu that is never presented, so it cannot guarantee declining an unpaid object entry/stack on that path. The guarantee is narrowed to "never select a row explicitly marked unpaid; decline later purchase/encumbrance prompts" (§3.5), and the residual is recorded as unproven evidence, not a success claim.
- **Native pickup-shape determinism:** the Phase 0 engine-side probe (new `AGENT_TEST` pickup case or `make -C test/agent native-pickup`) is the intended protocol-shape evidence, and AC17's pass condition is that native target passing. If a shape cannot be produced deterministically, name it explicitly, make an operator-gated manual probe mandatory for it, and do not let row-model tests alone claim protocol-shape verification (§6, AC17, AC18).
- **Rollback:** retain backward readers and separate commits by phase. If live performance regresses, revert policy behavior while retaining schema/recording readers so generated artifacts remain readable. No new runtime feature-flag matrix is required merely for rollout; any temporary comparison flag should be narrowly scoped and documented rather than permanent duplicate policy.

## Non-goals

- Engine traversal, private object state, hidden maps, monster disposition/BUC inference.
- A general hierarchical planner, new dependency/service, learned reward model, or role equipment optimizer.
- Global changes to Jev gating, strategy call frequency, pricing, budgets, or history retention.
- Automatic equipping, unlocking/kicking, shop purchasing/theft, or unsafe eating.
- A new descent policy or proof that the 'nobody descends' campaign problem is solved.
- Automatic ascent under `flee_to_upstairs`.
- Presentation-layer candidate generation or Choice wire-schema changes.

## Reviewer scrutiny

1. The caller's pickup-singleton premise conflicts with inspected policy. Confirm no uninspected branch/injected policy supplies pickup before implementation; design here treats it as new behavior.
2. No campaign artifacts were read. The reported revisit/coverage/gold numbers and 907-test baseline are supplied evidence. The exact reason one of 17 calls did not count as an applied boundary remains unknown.
3. Verify actual game autopickup and pickup menu/yes-no shapes with fixtures before enabling acquisition. A broad `collect_items` directive is intentionally permission to inspect and choose, not to take every unknown row.
4. Confirm that arrival-only upstairs retreat matches operator intent. If ascent is intended, require explicit approval for the dungeon-exit case and add a separate terminal action contract.
5. Review the proposed two-door/three-stall/route-cap limits against live action reconciliation, especially no-time interactions and prompt continuations. They must not punish inventory maintenance or count unsent proposals.
6. Verify the exact current live/evaluator ordering of observation folding, `commit_effect`, active directive views, and pending Jev tokens while wiring payloads. Existing hooks support this design, but this read-only review did not exhaustively trace every replay/rejection branch.
7. Make sure local frontier/loot evidence signatures are not accidentally invalidated by hero movement, stale screen persistence, global map changes, or inventory refresh; otherwise the pacing bug returns under a new name.
8. Schema-v2 single-positional-goal validation is deliberately stricter than v1. Validate real DeepSeek responses against the new prompt rather than silently relaxing it when a model combines incompatible positional goals.
9. Do not carry command semantics into `key`/`direction` needs. The existing broad activation gate makes this an especially important integration review point.
10. Changing navigation to persistent terrain is correctness-critical but affects edge eligibility beyond commitment. Require existing navigation, recovery, room-awareness, and forced-search regressions, not just new happy-path tests.

## Addendum — actual semantics as implemented (stall-recovery plan Rev 3)

This section records how the contracts in this plan are **actually realised** in
`tools/agent/`, superseding any wording above that predates the
`doc/agent-stall-recovery-plan.md` (Revision 3) work.

- **One destination terminal owner.** Every termination source (arrival, door
  open/refusal/ineffective, stall, cycle/recovery invalidation, unreachable,
  instance change, replacement) funnels through a single owner in
  `policy.ScriptedReflex` (`_retire_owned` / `_retire_cycle_owned` /
  `_emit_destination_terminal`).  It captures `(instance, serial, source,
  purpose, generation)` *before* clearing state and emits exactly one terminal
  destination event.  Directive settlement remains a once-only side effect
  layered on top, never the only path that terminates a destination.  A
  replacement emits a `replaced` terminal for the **superseded** serial with
  `replacement_serial=<new>`, then a distinct acquisition for the new serial.
- **Pre-send baseline / attempt semantics.** Route progress is seeded from the
  hero baseline, not the target position; a no-time acquisition installs the
  target and counts as no-progress attempt 1 of 3, while a moved acquisition
  starts progress from the reconciled hero.  Only matched gameplay attempts
  advance the stationary stage; prompts, inventory and unmatched observations do
  not.
- **Evidence-specific servicing.** `navigation.service_signature` (exploration
  terrain only) suppresses a successfully serviced site; `door_failure_signature`
  suppresses a refused door; `blocked_edge_signature` keys a failed edge to its
  legality-relevant cells.  Neighbouring occupancy movement reopens none of them.
- **Recovery vs suspension.** Only a *selected and reconciled* recovery effect
  retires a destination or spends destination counters; ordinary emergency,
  hunger and inventory interruptions suspend/revalidate rather than consume
  navigation stalls.

## Prompt-edge amendment (Rev 3)

The prompt-edge plan (`doc/agent-prompt-edge-plan.md`) extends this design
without changing its lifecycle ownership:

- **Rerouting.** A held destination is re-routed through the same filtered
  Dijkstra maps, so a longer valid route around a declined edge wins over
  retiring the destination; a held target is not re-elected or dropped merely
  because one edge is prompt-blocked.
- **Route-only failure reopening.** When no alternate route exists the
  destination retires through the existing single terminal owner and frozen
  unreachable/failure path (earlier than or no later than the existing
  three-no-progress bound). The suppression **owner stays the edge evidence**,
  not the destination's long-lived service signature, so once the edge reopens
  (positive relevant local change or a scope change) the target is legitimately
  reacquirable — a permanent target-service failure would wrongly hide a
  reopened route. Global map revision/time never reopens it.
- **Bounded search.** When no filtered destination is reachable the existing
  bounded search/recovery owns the turn, with its own per-site limits and the
  forced-search gates unchanged; no per-destination unbudgeted search loop is
  introduced. Named tests: `test_route_only_failure_does_not_permanently_suppress_target_after_reopen`, `test_vapor_cloud_loop_all_routes_blocked_enters_bounded_recovery`, `test_vapor_cloud_loop_routes_around_with_held_destination`.
