# ScriptedReflex upgrade — Revision 3 (implementation handoff)

APPROVED after three plan-review rounds (round 3: no actionable findings).
Implement via the six gated sub-waves in §9; every wave preserves all prior
safety/protocol/replay/spectator tests plus its cumulative new mutations.
The decisive architecture rule: **a proposal does not mutate gameplay memory,
a send is not a successful gameplay outcome, and an observation must be
reconciled before it is committed to memory.**

Keep `risky-emergency-forced-search` as the caller-approved dangerous
exception (operator decision: keep, tightly gated), not a safe fallback.
Consume its episode cap when the first dangerous prefix is successfully
sent, not when a search succeeds.

Full design sections follow, exactly as approved. Where this document
summarizes, the summary is normative; implementers must not weaken gates.

## 1. Requirements, constraints and safety contract

### 1.1 Required outcomes

* Bounded search, food, door and recovery attempts instead of equivalent-action loops.
* One Dijkstra per prepared navigation state; all reachable stairs/frontiers/door approaches considered before bounding action candidates.
* One deterministic, immutable table of at most 255 candidates for scripted selection, optional Jev selection and telemetry.
* Pure scripted preparation/fallback; retained argmax; unchanged `ReflexResult` fields.
* Positive public evidence for hero identity, terrain traversal and action outcomes.
* No map, target, directive or attempt leakage between level instances.
* Live-model/evaluation parity for identity, selection, invalid retries, lifecycle and usage accounting.
* Better measured coverage/depth as an objective, never a guarantee.

### 1.2 Constraints and non-goals

Stdlib-only, 78-column source, iterative commits, no engine/profile changes, public state only. Maintain deterministic evaluation, but **migrate `evaluate.py` rather than leaving its old semantics untouched**. No combat expansion, speculative item recognition, arbitrary menu decomposition or invented live Jev API. Terms and official Jev contract remain pending.

### 1.3 Safety

All candidates, not merely scripted argmax, must exclude unknown-blank steps, monster/hazard steps, unsafe rests and non-allowlisted food. Missing hero/HP/condition evidence cannot establish safety. Directives, confidence and heuristic score cannot override eligibility.

Ordinary search is bounded and purposeful, not a universal safe fallback. Forced search intentionally overrides an engine protection and may permit attacks; label it dangerous in code-facing semantics, docs, recordings and reports.

Escape is not universally "no." Native mandatory prompts retain their own reviewed default/refusal/cancellation semantics. When no safe gameplay action exists, a structurally legal action is not automatically semantically safe; prefer deliberate graceful termination to infinite context-free search.

## 2. Confirmed diagnosis and architectural evidence

### 2.1 Gameplay diagnosis carried forward

* The ds1 loop is rejected ordinary `s`, not farlook or an existing retained `m` intent. `policy.py:297-305,375-388` settles into `_unblock`; `state.py:308-325` counts stationary observations. `src/detect.c:2095-2103` calls `cmd_safety_prevention`; `src/do.c:2333-2353` may return without advancing displayed time.
* `Norep` suppresses repeated refusal messages. Count equivalent post-command state/time outcomes, not repeated messages. A prior exact refusal can remain relevant while identical fingerprints recur.
* Reviewer-confirmed `doc/agent-profile-v1.tsv:165` pins `safe_wait=on`; do not alter the profile.
* Full-cell door semantics: gray `-`/`|` walls; brown `-`/`|` open doors; brown `+` closed door. `include/defsym.h:104-109` corroborates this. Glyph-only passability reverses important behavior.
* Reviewer confirms zero drawn `>` tiles in ds1. A palette entry alone is not a stair encounter. A descend directive cannot route to unobserved stairs.
* `policy.py:415-525` prefilters targets before reachability, prefers a Manhattan-nearest stair, repeats Dijkstra and lacks durable frontier exhaustion.
* Food rejection checks floor food as well as inventory, per reviewer-confirmed `src/eat.c:3580-3605,3714-3723`. Global floor-food-negative inference is invalid.
* Reviewer-confirmed `src/botl.c:464-474` and `src/dungeon.c:1439-1453`: displayed depth is not a unique dungeon-level identity.
* First-`@` hero selection (`state.py:220-225` in the inspected checkout) is unsafe in the presence of human-class monsters.

Historical ds1 comprises three episodes of 15,001 controller ticks, depth 1, zero invalids and zero drawn down stairs, as supplied/confirmed by caller/reviewer. The separate `/tmp/auto-deepseek` corroboration covers **two**, not three, episodes. Do not combine their denominators or invent aggregate measurements.

### 2.2 Integration evidence

* `controller.py:1950-1951` computes a mutating scripted fallback before a possible Jev movement: orphaned intent risk.
* `providers.py:1375-1501` independently constructs choices and maps indices inside the adapter; central retained-table validation requires changing that boundary.
* `controller.py:1853-1858` already marks controller request state only after complete `_emit`; extend this boundary to the new lifecycle.
* `evaluate.py:580-582` currently applies a snapshot and immediately calls `mem.observe`; this must move behind reconciliation. `_on_invalid` at 658-683 records rejection information but is not yet the required shared retry lifecycle. `_decide_pending` and `_propose` are migration points.
* `directives.py:188-215,262-302` already uses `_ineligibility_reason` for both mutating view and nonmutating peek. The missing design is **instance scope and controller settle-once ownership/generation**, not predicate drift.

## 3. Immutable candidates and controller-owned attempts

### 3.1 Dependency shape and types

Proposed `tools/agent/candidates.py` is a dependency-neutral leaf: import neither `policy` nor `providers`, directly or indirectly. Prefer stdlib-only DTOs with neutral scalar/tuple identity fields. Policy, providers, controller and evaluation depend on it. No provider behavior, memory mutation or navigation belongs in this leaf.

Conceptual immutable types:

```
ReflexFeatures:
  episode, controller_tick, need_key, observation_generation
  level_instance_id, displayed_level, map_revision
  HeroResolution, status/time/conditions, public map subset
  inventory/floor evidence, directives + generation
  targets/budgets/recovery summaries, rejection_version

ActionCandidate:
  candidate_id, immutable_action, semantic_label
  score, score_components, reason, proposed_effect

CandidateTable:
  schema_version, need_key, table_version, table_id
  ordered_candidates, scripted_index, jev_eligibility
  canonical_bytes

PreparedReflex:
  immutable_features, table

SentAttempt:
  NeedKey, table_id, candidate_id, sent_ordinal
  immutable_action, before_fingerprint, before_hero_set
  source_instance, expected_effect, terminal_state
```

The actual primary key for a sent attempt is the four-tuple **NeedKey + table ID + candidate ID + sent ordinal**. One gameplay attempt may be in flight at a time. Transport page requests are not additional gameplay attempts. A multi-send intent contains successive attempts, not simultaneous gameplay sends.

Use immutable tagged action fields/tuple menu commits; a frozen dataclass containing a writable dict is not immutable. Only materialize a fresh protocol dict at the boundary.

API sketches:

```
ScriptedReflex.prepare(context) -> PreparedReflex
select_retained(prepared, rejected_members) -> candidate_id | exhausted
validate_raw_choice(prepared, raw_result, rejected_members) -> selection
reconcile_attempt(attempt, parsed_observation, prior_state) -> Reconciliation
commit_reconciliation(reconciliation) -> None  # controller-owned
```

Shared arbitration/reconciliation functions should be pure and reused by live control and evaluation. Proposed `tools/agent/arbitration.py` may hold them if existing modules cannot share them cleanly; it must not create a policy/providers cycle. `on_action_sent` is a controller lifecycle operation, not a mutating provider callback.

`ScriptedReflex.decide/fallback(context)` still return unchanged `ReflexResult`. In production they select from `context.prepared`; fallback never rebuilds the table. Direct-call compatibility may use a pure prepare path when no prepared object exists, but cannot mutate gameplay memory.

### 3.2 Canonical identity and performance

Canonicalize once per prepared table using a versioned deterministic encoding: explicit ordered records, sorted object keys where used, stable UTF-8 JSON separators, no NaN/Infinity, integer scores, no wall clocks/Python hash/mutable RNG. Identity inputs include NeedKey, table version, relevant immutable features, instance/revisions, directive generation, rejection-set version and all ordered candidate/action/effect data.

Candidate IDs bind canonical action plus semantic/effect identity. Table ID is SHA-256 of the canonical body **excluding the ID and retained-byte fields themselves**, avoiding circular identity. Retain those exact bytes; use them for hashing, fake Jev payload table content and telemetry/replay comparison. An outer request envelope may include the resulting ID but must not recanonicalize the table. Do not conflate this identity encoding with evaluator semantic-action comparison rules.

Use deterministic deduplication and tie-breaking. Equivalent wire actions cannot evade rejection by changing labels/IDs. Deduplicate actions before truncation; retain one deterministic winning target/effect. Explicit family/direction/row order breaks equal scores.

Performance gate: benchmark preparation/canonicalization/hash/payload separately for 1, representative and 255 candidates, plus **150,000 tables x 255 candidates** worst-case streaming loop. Record hardware/Python, payload sizes, elapsed throughput, p50/p95/p99/max per-table latency, deadline overruns and peak retained memory. Do not retain the entire episode's tables in memory. Default 0.75s reflex allowance includes preparation, selection and reserved send headroom, not just hashing. Every preparation must obey the configured absolute deadline and fail closed on expiry. Set and record a target-machine headroom budget before accepting the wave; do not claim this review measured it. SHA-256 cost is likely serialization-bound overall, not intrinsically hash-bound; measure rather than optimizing the wrong component.

### 3.3 Scores and safety separation

Evaluate all reachable targets before bounding candidates to <=255. Normal command tables aggregate at most eight movement directions plus interactions. Eligibility eliminates unsafe actions before scoring.

Initial ordinary base scores: descend here 900; reachable stair progress 800; frontier 500; unvisited known cell 400; justified budgeted secret search 300; recovery step 200; eligible inventory inspection 100; proven-safe rest 0. Bound directive/visit/path adjustments so ordinary frontier bias cannot suppress a newly reachable staircase indefinitely. Safety emergencies and mandatory prompt continuations take precedence through explicit eligibility/priority, not uncontrolled score bonuses.

Directives add bounded logged components to existing candidates only. Coordinate bias requires observed reachable support. No directive renews attempts, changes hunger/HP gates, invents a staircase or grants unknown traversal.

### 3.4 Exact send/reconciliation order

For every selected gameplay action:

1. Check local current NeedKey, prepared table/version, raw index type/bounds/confidence where applicable, rejected-member exclusion and member eligibility.
2. Map the retained candidate to its immutable action/effect.
3. Materialize a fresh dict and call `protocol.validate_action` against the actual pending need.
4. Complete `_emit` within the existing bounded write/deadline rules.
5. Only on successful complete send, call controller `on_action_sent` and create the single `SentAttempt` with actual sent ordinal.
6. Then either `_on_invalid` rejects **that exact** attempt, or the next valid observation is parsed into temporary presentation and reconciled against it **before** hero, level or map memory commits.
7. Commit the reconciliation once; emit one terminal lifecycle event and release the attempt.

No local validation failure or failed write arms a sent attempt. Record these as terminal proposal/send failures with no sent ordinal and no gameplay effects. If a partial write makes transport state uncertain, do not retry the action blindly; terminate transport according to current policy. A definitely failed prefix write does not consume the emergency cap; an ambiguous partial transport cannot continue play to exploit that rule.

Exactly-once terminal categories include `observed`, `rejected`, `discarded-no-observation`; observed carries `moved`, `stationary-time-advanced`, `no-time`, `prompt-opened`, `unknown`, etc. Unknown is an outcome classification, not an excuse to leave the attempt live indefinitely. Duplicate reconciliation must not alter memory or produce a second terminal event. Uncorrelatable invalid/duplicate protocol records follow strict protocol-failure handling, never reject a new attempt by accident.

Close/EOF/protocol failure before a usable observation discards the in-flight attempt without gameplay outcome commits. On invalid, record exclusion/telemetry but do not credit a move/search/food/door success. Paid usage is separate from gameplay outcomes and remains chargeable. The forced-prefix activation cap is the explicit send-time risk-accounting exception; success budgets otherwise wait for observations.

Reconciliation produces transition evidence, hero possibilities and effect deltas together. Parse is not commit. The transition automaton below is evaluated before any arrival cells are merged into active terrain. Then settle directives and commit the new observation/memory atomically from the controller's point of view.

### 3.5 Invalid retry exclusion and deadlines

Keep a controller-owned rejection set per NeedKey, retaining candidate IDs and canonical action signatures. A candidate rejected locally or by ordinary engine `invalid` is terminally excluded for that NeedKey. Retry selects the next safe member from the **retained** table, deterministically; the argmax among remaining members is the retained fallback for that retry. Never recompute the same winner and resend it.

If actual new page/evidence input requires rebuilding, create a new table version with the rejection set as an identity input. Exclusion follows equivalent actions across IDs/labels/table versions. No gameplay-side effect is committed by rebuilding or excluding.

`incomplete` is special transport repair: do not mark its candidate as gameplay-rejected. Terminate that wire send's lifecycle as a delivery-repair rejection with no gameplay effects, repair missing page obligations, then allow a new sent ordinal after completion if the action remains valid. This is the sole specified delivery exception, not permission to resend ordinary rejected members.

Retain the **original content deadline** through invalid recovery, same-ID retries and drip-page repair. Do not reset it on each retry/table version/page. Preserve current bounded write handling; a new preparation does not buy a new deadline.

On member exhaustion use reviewed per-kind structural fallback only if not already excluded and semantically appropriate; otherwise graceful termination. Track fallback actions in the same lifecycle/exclusion rules. Command `s` is not an infinite exhaustion fallback. A repeated same-ID invalid must converge to another legal member or stop.

## 4. Public memory: transitions, hero identity, directives and navigation

### 4.1 Deterministic level-instance automaton

Use episode-local monotonic `LevelInstanceId`; displayed `Dlvl` is metadata. **No `strong_public_match`, archived map reuse or cross-instance terrain merge in implementation 1.** Optional reuse is deferred to a separate reviewed design with explicit landmark collision analysis.

Automaton states:

* `UNBOUND`: before initial playable observation.
* `ACTIVE(id)`: consistent public continuity.
* `PENDING(old_id, transition_token, evidence)`: possible arrival not yet settled; freeze old map/targets/attempt effects.
* `FRESH_UNRESOLVED(new_id, token)`: allocated fresh scope but hero/arrival presentation incomplete.
* `ACTIVE(new_id)`: fresh arrival has a usable command-context presentation.
* `STOPPED`: no further gameplay commits.

Inputs extracted deterministically from **matched sent attempt plus temporary observation**, not historical substring scans:

* `S`: successfully sent stair/ladder ascend or descend action; include a failed-looking stair use until rejection is established.
* `O`: current public trapdoor, hole, level-teleport or other arrival outcome, using an allowlisted source-derived recognizer tied to current observation/outcome. Quoted/history/look descriptions do not become authoritative outcomes.
* `L`: displayed-level label changed, including change without a canonical message.
* `D`: structural/position discontinuity inconsistent with the matched ordinary action. Define structural comparison on stable observed terrain classes (walls, doors, stairs and other reviewed fixed terrain), excluding occupants, lighting and expected local open-door changes. Any unexplained conflicting stable cell, or inability to establish old-map continuity after unexpected relocation, is sufficient to suspect a fresh instance; no arbitrary percentage threshold.
* `N`: affirmative no-arrival proof: exact correlated native rejection/cancellation of transition action, coherent unchanged source presentation/confirmed hero/label and no contradictory `O/L/D`. Equality of topology or time **alone** is not no-arrival proof.

Rules in priority order:

1. Invalid snapshot/close/protocol failure: discard attempt and stop; do not commit partial arrival data. Preserve quarantine evidence in telemetry. No need to allocate gameplay memory after terminal shutdown.
2. From UNBOUND, first valid playable observation allocates fresh instance.
3. `S` creates PENDING after successful send. No old-map outcome effects are committed yet.
4. Any `O`, `L` or `D`, with or without `S`, creates or enriches PENDING. A label change alone suffices. Same-label arrivals do not escape this rule.
5. At the first post-action observation capable of settling the pending transition: if `N` holds and no positive signal conflicts, keep the old instance and terminate the transition token as rejected/no-arrival. Otherwise allocate **one fresh instance**, even if evidence is contradictory, message text is absent, the label is unchanged, or there are zero/multiple `@` cells. Arrival that cannot be disproved fails closed to fresh identity.
6. Fresh allocation expires old-instance targets/directives/continuations and creates empty map-local budgets/negatives. Arrival data goes only into the fresh scope. If hero cannot be resolved, remain FRESH_UNRESOLVED and suppress movement/forced search; do not defer allocation until an `@` appears.
7. Follow-up observations for the same pending token complete the same fresh arrival; do not allocate repeatedly merely because hero ambiguity persists. New independent transition signals create a new token.
8. A cancelled transition proposal that was never sent creates no transition. Cancellation after send preserves pending uncertainty unless `N` is established. On bounded timeout with no affirmative no-arrival proof, retire the old active scope; if play resumes, it does so in a newly allocated unresolved instance before any observation commit. If shutdown is required, remain stopped with no map commit. A later cancelled/stale callback cannot reactivate the old scope.

A message lookalike in a non-outcome context with unchanged consistent continuity is ignored as an arrival signal; no new scope is needed and there is **no merge operation**. An indistinguishable exact current outcome-looking message cannot be reliably disproved merely by equal topology: conservative allocation may lose recall. Message text can trigger quarantine, never establish identity with an old instance.

Residual: a completely unobservable same-label transition producing identical public state and no action/outcome/discontinuity signal is information-theoretically indistinguishable from remaining in place. The design cannot promise to detect that. It guarantees no merge once arrival is evidenced or uncertain; it does not invent inaccessible engine identity. Conservatism may also allocate fresh instances after same-level teleport or terrain alteration. This recall loss is accepted.

Required fixtures in section 8 verify ordinary depth change, same-label branch arrival, lookalike messages, message-free real transition, zero/multiple-`@` arrivals, branch depth collision and independent signal removal.

### 4.2 HeroResolution and unknown outcomes

`HeroResolution` holds status, optional confirmed position, **set of possible positions**, evidence and observation generation. Never use first/nearest `@` or a targeting cursor.

Under the normal pinned hero-rendering contract, a unique `@` at an initial coherent command boundary can bootstrap only when no contradictory transformation/condition evidence exists. Multiple candidates remain a set.

For a matched attempt in the same instance:

* explicit rejected/no-time nonmovement with coherent presentation preserves old confirmed position;
* same-position/time-advanced confirms stationarity, not failure to spend a turn;
* expected movement destination with consistent evidence supports that destination;
* unresolved movement preserves old and expected positions plus any other evidence-supported possibilities;
* unexpected square, teleport, zero/multiple `@` or incompatible presentation invalidates the expected-move inference and expands/retains possibilities rather than forcing a single winner.

Intersect with reliable current rendering evidence only when that evidence is complete and applicable. Do not turn an empty intersection into a fabricated singleton. Unknown may include an explicit outside-known-candidates possibility (e.g. unsupported transformed/missing hero), represented by a status flag rather than assuming an empty set means certainty.

The next observation's resolution must use the pre-commit attempt and prior set. Do not overwrite `mem.hero` with first `@` and then attempt reconciliation. Only positively supported singleton continuity becomes CONFIRMED. Other `@` cells remain hazards; unresolved sets suppress movement, stair/door actions requiring position, and forced search. Bounded legitimate prompt handling or safe clarification may continue; unresolved gameplay identity ultimately terminates, not blind search.

### 4.3 Terrain, occupancy and revisions

Persist supported terrain separately from current occupants and floor-object evidence. Hero/monster overlays do not erase remembered stairs; current or uncertain occupancy blocks traversal. Omission alone does not prove a monster square safely empty or an unknown square floor.

Classify the full `(glyph, color, style, other public cell fields)` tuple under the pinned profile; apply brown/gray door distinctions above. Unknown/unrecognized variants fail closed. Keep wall/bar/water/hazard treatment conservative.

Per-instance `map_revision` increments for changed structural/unknown evidence; occupancy/reachability generation tracks dynamic blockers. Observation-frontier exhaustion stores local adjacent-unknown and reachable-approach signatures. Reopen only on relevant signature changes; unrelated discoveries do not replenish every frontier. Monster clearance may open an approach but does not renew secret-search budget.

Target persistence requires same instance, valid reachable approach, no blocking occupant and unexhausted relevant evidence. Cycle recovery invalidates the target. One arrival with no new information exhausts that observation-frontier signature; secret-search opportunity remains separately budgeted.

### 4.4 Directive scope and settle-once ownership

Keep public directive schema unchanged. Add internal source instance to pending strategy request/result envelopes, and activated instance to `DirectiveBook` alongside displayed level and generation.

Controller is the sole lifecycle settling owner. At observation reconciliation/command boundary it:

1. settles fresh-instance transitions and expires active old-instance generation once;
2. rejects pending advice whose source instance no longer equals active instance;
3. settles TTL/precondition expiry through the existing shared eligibility authority;
4. activates eligible pending advice before preparing candidates;
5. freezes one view/generation into PreparedReflex.

Extend `_ineligibility_reason` with instance mismatch. Preserve the existing common predicate for `view`/`peek_view`. `peek_view` remains pure, emits no event, changes no generation and cannot cause expiry. A `view` read after controller settlement cannot expire the same generation twice. If compatibility retains mutating `view`, it must use idempotent generation-scoped settlement; production ownership is still controller-only.

Fixture: target directive active on instance A, transition to same-Dlvl B. Both view and peek report inactive; exactly one expiry event for A's generation; zero score contribution; pending A advice is rejected before activation. Repeated spectator renders/reads leave lifecycle unchanged.

### 4.5 One-Dijkstra navigation

Use one Dijkstra from confirmed hero over known-safe legal edges, with positive base cost and capped visit/failed-edge penalties. Consider all reachable targets, not nearest eight before reachability. Exclude uncertain/hazard occupants, unknown cells and closed-door traversal. Forbid diagonal door entry/exit and unsupported corner squeezing; verify native fixtures.

Candidate target kinds: all observed down stairs, known-safe observation frontiers, unvisited known cells, cardinal closed-door approaches and plausible independently budgeted corridor-end searches. A closed door requires opening; its future floor is not included in the graph until observed. A monster-covered stair is remembered but not presently reachable. Hero-on-target must yield arrival/descend/interaction handling, not disappear because first step is absent.

Persist target only under section 4.3 conditions; newly reachable stairs/emergency actions can replace it. Aggregate targets sharing a first step into one candidate/effect deterministically. Directive score bonuses do not manufacture unknown destinations.

## 5. Recovery, scoped negatives and dangerous forced search

### 5.1 Bounded ordinary recovery and refusal evidence

Track discovered cells, entered cells, actual movement, displayed-time delta, no-time outcomes and prompt-only activity separately. Use bounded stationary/cycle history. Initial recovery: justified search -> deterministic safe alternative step -> invalidate/reselect target. No mutable RNG and no reset on every observation.

Initial budgets: three completed ordinary searches per plausible site/topology signature; one refused/equivalent no-time search before suppression; two failed door-open attempts per relevant door state. Bound overall no-progress recovery as well. Opening/food intents bind exact expected prompts; unexpected needs cancel rather than falling into generic `_command` logic. No kicking, unlocking or attacks.

**Refusal recognizer:** match only current public `cmd_safety_prevention` evidence for a sent ordinary `s`: normalized exact `You already found a monster.` with the engine-emitted optional `Use 'm' prefix to force another search.` suffix, or the actual `Searching doesn't feel like a good idea right now.` variant. Derive and pin accepted variants from source/native fixtures. Do not match generic `found a monster`, farlook descriptions, quoted/history text or unrelated messages.

A no-time identical fingerprint alone means no progress/unknown outcome, not proof of this specific safety refusal. It can suppress repeated search, but cannot independently authorize forced search. After one exact correlated refusal, identical post-command state/time fingerprints preserve that refusal evidence through `Norep` suppression until relevant evidence changes. Dangerous-property refusal will normally fail the forced-search condition gate anyway.

Search and door budgets count observed outcomes, with separate rejection/exhaustion evidence; proposal generation/local validation/write failure do not consume gameplay success budgets.

### 5.2 Food negatives

Maintain inventory-negative evidence tied to inventory signature/version, and location-negative/floor evidence tied to `(level_instance, confirmed_position, floor_revision)`. Recognize both "don't have anything to eat" and "don't have anything else to eat" only as matched eat outcomes.

Negative inventory plus current floor evidence does not imply no food elsewhere. Reassess on inventory signature change, fresh instance, or arrival on publicly identified allowlisted floor food. Known floor ration may authorize location-specific eating while inventory remains negative; an unknown `%` may not. Retain actual inventory observations across levels; clearing location suppression is not proof food appeared. Unchanged inventory refresh does not reopen blind probes indefinitely.

Current safe-row/letter allowlist remains; corpses, tinned food and cockatrice egg are rejected. One bounded inventory-menu expansion after equivalent letter failures; no repeated `*`. Commit cache changes only from accepted content/outcomes, not from discarded candidates.

### 5.3 Ten mandatory gates for `risky-emergency-forced-search`

1. Confirmed hero, coherent command need and active resolved instance; no transition or identity ambiguity.
2. Known positive HP/max with HP **strictly above 50%**; equality and unknown values fail.
3. No dangerous public condition and complete recognized condition interpretation. Deny Hungry, Weak, Fainting, Fainted, Starved/other Hungry-or-worse published states and dangerous incapacitation/property indicators. Unknown conditions fail closed. This is not a claim to reproduce hidden engine state.
4. Exact correlated ordinary-search refusal evidence from 5.1, still applicable to the unchanged trap state.
5. All known legal movement, door and usable stair alternatives, plus immediate justified safe food/progress actions, exhausted. "Low score" is not exhaustion; an untried eligible closed door blocks activation. Do not step into a monster to satisfy exploration.
6. No other pending gameplay intent, healthy transport/recording policy, and verified native prefix/cancellation contract. No outstanding provider can override continuation.
7. Fewer than three episode activations consumed; count persists across instances and recovery/target changes.
8. Exact controller-owned two-send binding: prefix `m`, then **the exact immediately following command NeedKey and `s`** only. Not generic key/direction need, not any future command.
9. One single search without repeat count; suffix outcome must be observed and displayed time must increase before calling it successful.
10. Reassess all gates/outcomes before another activation; never retry unchanged failed activation automatically, never reuse an armed prefix; bounded exhaustion leads to `policy-exhausted/trapped` graceful quit.

Any gate false forbids activation. Ordinary safe alternatives remain available where applicable; when genuinely trapped and the exception cannot run, graceful quit is the fallback, not endless normal search.

### 5.4 Controller-owned two-send transaction

Transaction states: `PROPOSED -> PREFIX_SENT -> SUFFIX_SENT -> SUCCEEDED | FAILED`, with cancellation possible from any live state. Each successfully sent action is also an ordinary SentAttempt under section 3.4, with its own exactly-once terminal event.

* PROPOSED retains origin NeedKey/instance/hero/fingerprint, ten gate results, planned suffix and risk label. No cap consumption.
* Complete `_emit(m)` then `on_action_sent`: increment episode activation count exactly once and enter PREFIX_SENT with prefix send ID. Failed write/local invalid never consumes. Successful `m` is **never refunded**, including cancellation, invalid, failed write of `s`, no-time `s`, shutdown or death.
* Reconcile prefix attempt against its next observation before memory commits. Bind the suffix to that observation's exact command NeedKey only when it is the immediately following need, same instance, unchanged required evidence and gates still hold. Expect the prefix itself to consume no game time; unexpected time/HP/state change is reason to cancel and reassess, not silently bind.
* The reserved continuation table permits only `s` for that exact need, or separately controlled genuine native cancellation. Do not ask Jev, run normal recovery or produce another `m`. Protocol auxiliaries are allowed only to fulfill that need's transport obligations, not to bridge a different gameplay need.
* Recheck gates immediately before suffix send. Complete `_emit(s)` then create suffix attempt and enter SUFFIX_SENT. No-time/rejected/unknown suffix ends FAILED; displayed-time increase with coherent matched outcome is required for success. Success may include HP loss and remains dangerous. Record before/after HP/time and outcome once.
* Cancel on any nonmatching need/action, prompt, instance transition, hero ambiguity, gate change, invalid (including `incomplete` for this dangerous transaction), content/deadline expiry, tick cap, shutdown or protocol/recording stop. Once cancelled, no suffix may use the prefix again. A failed local validation/write of `s` cancels; it cannot fall through to another retained ordinary member while prefix might still be active.

**No prefix leakage:** cancelling controller state does not cancel engine state. Use a source/native-fixture-verified cancellation action in the actual next need; never blindly treat Escape as universally correct. Do not send another ordinary command or `#quit` until cancellation is observed or the engine contract proves it consumed the prefix. If prefix state cannot be safely cleared/confirmed, terminate the transport/episode rather than let it modify a later action. This may prevent graceful in-game quit on failure paths; record the distinction honestly. In particular tick-cap after `m` cancels first, never sends a prefixed quit.

Residual risk: public gates cannot prove absence of every hidden danger, and a valid forced search deliberately advances time near hazards. A pet may not move, the hero may take damage or die, and a successful search may reveal nothing. Three activations bound exposure, not guarantee escape. Exact-prefix native verification is a release gate; if the engine does not expose the specified next-command/cancellation contract reliably, leave this path unavailable rather than weaken binding.

## 6. Jev and evaluation/replay parity

### 6.1 Provider boundary

Before: `JevReflex.build_choices` creates generic choices; `decide` returns mapped `ReflexResult` action.

After:

```
ReflexContext.prepared: PreparedReflex
JevReflex.build_choices(ctx) -> retained-table payload | unsupported
JevReflex.decide(ctx, deadline) -> ReflexChoiceResult
validate_raw_choice(prepared, result, rejected_set) -> candidate | rejection
controller maps candidate -> immutable action -> validated wire dict
```

`ReflexChoiceResult` carries raw index/abstention, confidence or parse error, request/NeedKey/table identity, usage, latency and dispatch status — never a mapped action. Keep existing DeepSeek `StrategyResult` for directives; do not overload that name. `ReflexResult` remains unchanged and is constructed after controller selection.

Central validation checks exact request/NeedKey/table version, non-bool integer index in bounds, finite confidence in `[0,1]` meeting threshold, unrejected membership and current safety. Default threshold 0.8 remains experimental. Paid rejection uses retained argmax among eligible unrejected members; stale whole context means discard and prepare for the actual new need, never send an old fallback.

Skip before reserve for unsupported needs, singleton/mandatory continuation/emergency tables, expired deadlines, unavailable provider or unhealthy recorder. Bill returned usage once even on stale/malformed/low-confidence/abstaining rejection; preserve dispatched unknown exposure when no usable usage arrives. Choice acceptance does not determine billing.

Jev stays scripted for line/extcmd, full 1,659-position domain, arbitrary multi-select and menus over 128 selectable rows. All pages must be delivered; no silent shortlist truncation pretending to represent all menu rows. Abstain is out-of-band, not entry 256. Real endpoint/terms remain unapproved; fake tests do not establish vendor schema/calibration.

### 6.2 `evaluate.py` migration

Migrate `_on_obs`, `_on_invalid`, `_on_closed`, `_decide_pending` and `_propose` to the same PreparedReflex/canonical bytes, raw-choice validator, retained selection/exclusion and reconciliation functions. Replace its current immediate `mem.observe` with temporary parse -> reconcile -> instance/hero resolution -> commit. Reuse per-kind structural fallback rather than a second copy. Preserve deterministic clocks and existing semantic action/agreement reporting.

Replay must model **sent** actions from sidecar/correlated recording, including invalid retries and sent ordinal; do not treat every proposal as sent. Unknown send/outcome remains unknown. Wire-only input cannot reconstruct an index or prove a hypothetical action caused the next recorded state. In counterfactual proposal evaluation, only recorded sent actions drive recorded-state reconciliation; candidate comparisons remain comparisons. A controlled parity fixture supplies matching live-model sends/provider results so full equality is meaningful.

Provide one ordered fixture containing accepted action, ordinary engine-invalid with alternate retained retry, unknown/no-time outcome, paid Jev rejection carrying usage and stale table identity. Feed identical events/recorded provider results to an in-memory live controller model and offline evaluator. Assert identical canonical bytes/table IDs, rejection sets, retained fallback choice, lifecycle terminal categories, hero possibilities, ticks/displayed-time deltas and usage totals. Ignore wall-clock latency fields only; do not omit semantic differences to force equality.

Preserve existing offline/no-network default and explicit network opt-in behavior. Historical sidecars without new fields remain readable with unknown identity where necessary; never fabricate new-table ground truth. Evaluation determinism means repeatable new semantics, not freezing the flawed old path.

## 7. Per-file handoff and compatibility

* `tools/agent/state.py`: temporary public observation extraction, full-cell classification, LevelInstanceId automaton, HeroResolution sets, separated terrain/occupancy, revisions, scoped food evidence and pure reconciliation inputs. No first-`@` or depth-keyed map reuse.
* `tools/agent/policy.py`: pure prepare/candidate generation, integer directive scoring, one Dijkstra, target persistence, bounded recovery/food/door effects. No send ownership or provider-side mutable intents.
* `tools/agent/providers.py`: PreparedReflex context and raw ReflexChoiceResult; project retained canonical table, preserve usage/worker cancellation; keep DeepSeek result contract and ReflexResult unchanged.
* `tools/agent/controller.py`: owns one SentAttempt, rejection set/deadline, pre-observe reconciliation/commit, directive settlement and risky two-send transaction. Modify `_answer_now`, `_on_invalid`, observation/closed handling, `_reflex_context`, `_decide_scripted`, `_decide_jev` and shutdown paths. Retain wire/page ownership.
* `tools/agent/directives.py`: internal source/active instance and generation-scoped settle-once lifecycle; extend existing `_ineligibility_reason`; pure peek.
* `tools/agent/evaluate.py`: shared arbitration/reconciliation, retry exclusion, actual sent-action correlation and table/usage parity; preserve deterministic replay and old recording readers.
* `tools/agent/recording.py`: versioned instance/table/candidate/attempt IDs, proposal/send/outcome events, rejection/repair distinction, risky transaction/gates, before-after HP/time, bounded writer/backpressure. Canonical table references may avoid repeated table-body logs but must remain replay-resolvable.
* `doc/agent-autoplay-plan.md`: updated safety/transition/lifecycle/Jev/evaluation/measurement contract. Cite safe_wait pin without altering it.
* Existing `test/agent/test_auto.py`, `test_auto_providers.py`, `test_auto_replay.py`, `test_auto_spectate.py`: preserve and extend coverage.

Proposed new paths: `tools/agent/candidates.py` neutral leaf; `tools/agent/arbitration.py` shared pure helpers if needed; `tools/agent/exploration_metrics.py`; focused candidate/exploration tests and bounded fixtures under `test/agent/`; `doc/agent-reflex-upgrade-report.md`. Follow existing manifest/fixture conventions; no generated engine changes.

Preserve safety anchors `test_auto.py:811-825,835-1015`. Some literal `s`/missing-HP-wait expectations represent behavior being intentionally tightened; migrate those with explicit reason while retaining no-attack, no-blind-move, no-unsafe-wait and prompt/food safety assertions. Do not delete them to pass the risky path. Add the new lifecycle/prefix assertions alongside them.

## 8. Verification and mutation acceptance

### 8.1 Full regression gate, every wave

Run **all pre-existing agent safety/protocol/provider/replay/spectator tests**, not only newly added M01-M17 cases. Specifically preserve same-ID invalid recovery, `incomplete`/drip content deadlines, page obligations, native defaults, spectator nonmutation, worker cancellation, billing/unknown exposure, recorder backpressure and offline replay. Record actual counts/results; 285 is historical supplied information, not a count verified here.

Run cumulative new tests/mutations introduced by prior waves. Mutations are temporary, isolated, must demonstrate the designated failure, then be restored to green; never commit them.

### 8.2 Transition fixture matrix

For each fixture assert allocated instance IDs, no cross-instance terrain/stair merge, no carried target or site attempts, and one transition settlement per token:

* `transition_depth_change`: sent stair, changed Dlvl.
* `transition_same_label_branch`: successful/ambiguous same-label arrival after stair/ladder or levelport evidence.
* `transition_message_lookalike`: quoted/look-context arrival phrase, unchanged topology/continuity; no merge/reuse decision arises. Add ambiguous exact outcome-looking variant that conservatively allocates fresh.
* `transition_without_message`: displayed-level or incompatible topology/relocation signal only; fresh instance.
* `transition_zero_at` and `transition_multiple_at`: allocate fresh before hero resolution; no old hero/target/attempt restoration.
* `transition_branch_depth_collision`: main-3 -> 4 -> branch-3 with conflicting terrain; three scopes.
* `transition_rejected_stair`: exact no-arrival evidence, no conflicting signals; retain old scope without new-map merge.
* `transition_conflict_timeout_cancel`: contradictory positive/rejection evidence, cancellation after send and timeout: quarantine/fresh-on-resume, never silently resume old map.

Signal-ablation tests remove `S`, `O`, `L`, and `D` one at a time from multi-signal fixtures; remaining ambiguity/evidence must still allocate fresh. Separate sole-signal fixtures prove each detector is necessary: disabling its implementation must make that test red. Include trapdoor, hole, level teleport and ladder cases. Do not claim an entirely invisible transition is detectable.

### 8.3 Sent-attempt interleaving matrix

Each case asserts exactly one terminal event for each proposed/sent lifecycle, no premature memory/success-budget mutation and no second effect on duplicate delivery:

1. Local invalid: never armed; excluded member; alternative/fallback.
2. Write failed: no sent attempt/effect; transport handling.
3. Sent -> invalid: exact attempt rejected; no gameplay commit; next safe member, same deadline.
4. Sent -> same position/no time: no movement/turn success; rejection/unknown classification as evidence allows.
5. Sent -> same position/time advanced: stationary time-consuming outcome, not a movement failure guess.
6. Sent move -> destination: observed movement once.
7. Sent -> unexpected square/teleport: transition/unknown reconciliation before memory; no expected-destination visit credit.
8. Sent -> multiple `@`: preserve possible-position set; movement/risky search suppressed.
9. Sent -> closed/protocol failure: discarded without gameplay commits.
10. Duplicate reconciliation: idempotent terminal state/effects, or strict protocol failure without another commit.

Add same-ID retry across versioned tables, equivalent action renamed, retained-table exhaustion, `incomplete` page repair without gameplay exclusion, and drip delivery retaining original deadline.

### 8.4 Forced-search exhaustive cases

One independent false test per gate; include HP exactly 50%, unknown HP/max, every published Hungry-or-worse state, dangerous/unknown condition, each legal alternative type and missing exact refusal evidence. Generic "found a monster" text must not activate it.

Test episode cap across fresh instances; `m` local invalid/write failure; successful `m` followed by `s` local-invalid/write-failed/engine-invalid/no-time; intervening prompt; transition/hero ambiguity/gate change after `m`; tick-cap/shutdown after `m`; successful time-advanced suffix exactly once; fourth activation -> `policy-exhausted/trapped`. Assert telemetry and no prefix leakage.

Native prefix/command/cancellation fixtures are mandatory before enabling the exception. Pure mocked requests cannot prove engine prefix cleanup.

### 8.5 Named mutation matrix

* M01 door_full_cell: invert brown/gray or traverse closed `+`.
* M02 unknown_floor: admit omitted blank destination.
* M03 monster_classes: independently remove each punctuation monster (`'`, `&`, `;`, `:`, `~`, `]`) and off-hero `@` hazard.
* M04 first_at: resolve first/nearest human glyph instead of evidence/set.
* M05 unsafe_wait: bypass each positive HP/hunger/hero/threat requirement.
* M06 food_allowlist: allow corpse/cockatrice substring.
* M07 escape_default: make Escape universal refusal/cancellation.
* M08 frontier_prefilter: restore `[:8]` prefilter.
* M09 Manhattan_stair: ignore reachable farther stair.
* M10 diagonal_door: allow illegal doorway/corner edge.
* M11 choice_gate: independently bypass identity, index type/range, confidence or rejected-member gate; preserve billed usage on rejection.
* M12 premature_effect: mutate fallback or commit before complete send/reconciled observation.
* M13 instance_collision: key by Dlvl/reuse old scope or remove one transition detector.
* M14 risky_transaction: refund/reset cap, bind a later need, reuse prefix or allow ordinary fallback after suffix failure.
* M15 food_scope: make floor negative global, ignore "anything else" or identified floor arrival.
* M16 frontier_revision: never reopen/reopen on unrelated change/reset secret-search budget/retain cycle target.
* M17 ticks_turns: treat ticks/messages/observations as displayed turns.
* M18 invalid_resend: resend rejected member/equivalent renamed action or restart content deadline.
* M19 observation_order: commit hero/map before reconciliation or drop possible-position alternatives.
* M20 directive_instance: retain same-Dlvl directive, accept stale pending advice or let peek settle expiry.
* M21 replay_copy: make replay arbitration/usage/outcome diverge from shared live helpers; parity fixture must fail.
* M22 repeated_canonicalization: recanonicalize retained table in selection/payload path; instrument call counts and performance gate.

Also verify dependency import graph for neutral leaf, table determinism under reordered inputs, cardinality, deduplication, singleton/unsupported skip-before-reserve, canonical-byte parity and bounded memory.

## 9. Six explicit gated sub-waves

Capture immutable pre-change zero-key baseline artifacts and test manifest **before wave 1**. Each wave is a sequence of small reversible commits; every exit runs the full regression gate plus cumulative new tests/mutations. No wave can claim release completion with a broken offline/spectator/provider regression.

### Wave 1 — Neutral DTO/identity and lifecycle scaffolding; behavior unchanged

Entry: baseline source/config/recordings and current tests captured; no gameplay changes.

Changes: neutral immutable candidate/action DTOs, deterministic canonical encoding/retained bytes, versioned identities, controller attempt-event scaffolding and shared pure helper interfaces. Wrap current single decisions without changing selection order. Legacy gameplay path remains authoritative until wave 2; scaffolding is shadow/event-only, not a second committer. Add cheap compatibility adapters for evaluator readers. Benchmark canonicalization across required sizes/episode loop.

Exit: identical legacy selected actions on baseline fixtures; neutral import graph; deterministic IDs; no extra mutation from shadow scaffolding; all existing suites, serialization/performance tests and applicable M11/M12/M22 cases. No live Jev behavior change.

Rollback: revert scaffolding/DTO commits; retain baseline and backward-compatible evidence. Do not leave a second state owner active.

### Wave 2 — Instance/terrain/hero and pre-observe reconciliation

Entry: wave 1 green, one lifecycle design available.

Changes: activate controller SentAttempt ownership and pre-observe reconciliation; remove duplicate legacy mutations; implement rejection exclusion/deadline preservation, instance automaton/no reuse, full-cell terrain/occupancy, HeroResolution sets and instance-scoped directive settlement. Add evaluator compatibility plumbing needed to preserve its current regressions; full new parity is wave 6, not permission to leave evaluator broken.

Exit: transition and interleaving matrices, same-ID invalid/drip repair, no map merge, no first-`@`, same-Dlvl directive expiry once, spectator reads pure; cumulative M01-M07/M12-M13/M17-M20 as applicable and all existing tests.

Rollback: revert this wave as a unit if memory/lifecycle invariants fail; do not keep new memory with old eager observe/send effects.

### Wave 3 — One-Dijkstra candidates/navigation and persisted targets

Entry: reliable instance/hero/outcome scope, no ambiguous movement.

Changes: pure multi-candidate preparation, bounded deterministic integer scoring/directive components, all-target reachability, one Dijkstra, door/corner edge legality, reachable stairs/frontiers/approaches and target persistence predicates. Retained-table selection replaces priority-return navigation. No dangerous prefix path.

Exit: farther reachable targets/stairs, hero-on-target, target invalidation by blockers/instance/cycle event hooks, no unknown/hazard candidates, deterministic table cap/dedup/performance; M08-M10/M16 applicable portions plus all prior tests.

Rollback: revert generation/navigation commits while keeping wave 2 safety and reconciliation. Avoid reintroducing depth-keyed memory or speculative mutation.

### Wave 4 — Scoped recovery/search/door/food budgets and negatives

Entry: stable safe target generation and lifecycle.

Changes: local frontier exhaustion/reopening, separate secret-search budgets, observed door attempts/prompts, deterministic cycle recovery, exact refusal fingerprints, scoped inventory/floor negatives and bounded eat-menu handling. Exhaustion uses explicit safe fallback/quit. Forced search remains unavailable.

Exit: recorded loops bounded, Norep handled without generic refusal matching, budgets not reset by movement/unrelated revision, floor-safe-food reopening works, both negative messages, no repeated rejected candidate; M14 guard prerequisites/M15-M17/M18 plus all prior tests. Report temporary extra trapped quits honestly.

Rollback: revert recovery changes without weakening wave 2/3 safety; do not substitute infinite structural `s` fallback.

### Wave 5 — Isolated dangerous two-send transaction

Entry: all ordinary alternatives/budgets auditable; native command-prefix and cancellation fixtures established; controller can stop without prefix leakage.

Changes: ten gates, controller two-send transaction, successful-prefix cap consumption, exact following command binding, cancellation/quarantine/shutdown handling, dangerous telemetry and docs. Do not share it with generic intent continuation shortcuts.

Exit: every false gate and every interleaving, cap across instances, no refund/no prefix leakage, success once, fourth activation trapped quit; native fixture and M14 variants; all earlier suites/mutations. If contract verification fails, exception remains unavailable — no weakened substitute.

Rollback: disable/revert isolated risky transaction and use bounded trapped termination; keep ordinary recovery improvements.

### Wave 6 — Jev, replay/evaluation and measurement migration

Entry: scripted pipeline and risky transaction fully gated; real Jev still disabled.

Changes: raw ReflexChoiceResult, exact retained-byte payload, central validation/mapping, skip-before-reserve/billing retention; complete evaluator/shared-helper migration and parity fixture; schema readers, streaming metrics and report. Rebenchmark complete preparation/payload path with 1/typical/255 candidates and episode-scale workload. Execute post-change zero-key campaigns against preserved pre-change baseline.

Exit: identical table IDs/fallback/lifecycle/usage in live-model and replay fixture; all paid rejection/unknown exposure/backpressure tests; unsupported domains scripted; M11/M21/M22 and entire cumulative matrix; every pre-existing suite; measured deadline headroom and bounded memory; full honest baseline/post distributions. No terms acceptance or live service enablement.

Rollback: return Jev to unavailable/scripted and revert adapter/evaluator migration together where needed; never keep live/evaluation semantic copies knowingly divergent. Preserve compatible recordings and reported failures.

## 10. Measurement, residual risks and reviewer focus

### 10.1 Campaign protocol

Use existing auto entry point with scripted reflex, strategy off, same engine binary/profile/role, deadlines, episode timeout, episode count and `--max-ticks 15000` for ds1 comparison. Capture before any behavior change and use separate immutable baseline/post output directories with commit/config/binary identity and completeness. No keys/network needed.

Historical ds1 used DeepSeek and is contextual evidence, not an isolated zero-key control. `/tmp/auto-deepseek` contributes two-episode corroboration only. Fresh zero-key baseline/post campaigns are **unpaired stochastic evidence**: report per-episode values and distributions with sample counts, not significance or causal claims. Same settings do not create matched seeds. Replays are not counterfactual gameplay trajectories.

### 10.2 Metrics

Read campaign, wire and action/decision sidecars; preserve evaluation's offline default.

* Controller ticks according to existing definition, separately from game turns measured by displayed status `time` deltas.
* Ticks and displayed turns at depth 1, max displayed depth, instance/label transition sequence.
* Stair encounters only when a map triple references a `>` palette entry; unique `(instance,x,y)` counts separate from repeated observations.
* Observed cells and confirmed entered cells separately, with instance scope and unknown hero outcomes excluded from claimed visits.
* Longest no-time rejected-search streak/equivalent trap span using post-command fingerprints, not message counts; actual farlook/prompt loops separately labeled.
* Frontier/door/search/food attempts, outcomes, exhaustion and reopening; candidate rejection/retry/repair counts.
* Risky activations versus successfully sent suffixes versus time-advanced successful searches; damage, cancellations, cap denials and trapped quits.
* Actual deaths, graceful tick-cap quits, policy-exhausted quits, transport stops, unknown end states and protocol failures separately.
* Invalids, unanswered needs, recording completeness, paid calls/usage and zero-key network-call count.

Fingerprints omit NeedKey/seq/message-history noise but include relevant public map/occupants, hero possibility set, status/time/conditions and action-specific evidence. Missing displayed time stays unknown, not zero. Fresh-instance allocation may count rediscovered cells again; label metrics instance-scoped coverage rather than true distinct dungeon area.

### 10.3 Evidence for the implementation report

Known supplied baseline: ds1 3 x 15,001 controller ticks, depth 1, zero invalids and drawn down stairs. Operator reports survival; campaign protocol success is not independently equivalent to survival. Earlier inspected refusal snapshots at times 1693/438/268 are snapshots, not episode turn totals.

Exact coverage, full maximum loop spans, benchmark numbers and baseline/post campaign figures remain unmeasured by this design review. Implementer must populate them from artifacts; mark pending rather than invent. Report whether depth improved, whether coverage improved without stairs, and whether loops were replaced mainly by policy quits. Do not declare gameplay success solely from green tests or protocol-perfect campaigns.

### 10.4 Reviewer scrutiny and remaining gates

1. Transition/no-arrival precedence and no-reuse rule, including same-label and ambiguous zero/multiple-hero arrivals.
2. Pre-observe reconciliation and exactly-once terminal lifecycle; no write/local-invalid arming, unknown hero sets preserved.
3. Terminal candidate exclusion across same-ID retries/new table versions and unchanged content deadlines, while preserving `incomplete` repair.
4. Instance-bound directive expiry with one controller settlement event and pure spectator peek.
5. Successful-prefix irreversible cap use and true engine-prefix cancellation; no ordinary fallback can leak through an armed prefix.
6. Shared live/evaluator raw-choice, outcome and billing semantics; recorded actions, not counterfactual proposals, drive recorded-state outcomes.
7. Neutral DTO dependency graph, canonicalize-once retained bytes and measured deadline headroom.
8. Full existing tests at every dependency-ordered wave, not only the new mutation list.

Residuals are explicit: invisible public-state-equivalent transitions cannot be recognized; conservative allocation loses recall; unsupported hero/condition evidence may cause early quits; secret search is heuristic; forced search can injure/kill without freeing the hero; cancellation failure may require transport termination instead of graceful quit; Jev schema/terms/calibration are unverified. These are not reasons to weaken safety or fabricate outcomes.

Implementation proceeds only through the specified review/verification gates. Native prefix/cancellation verification is required before enabling the dangerous exception; official Jev approval remains separate. No unresolved question authorizes a developer to merge levels by text, resend a rejected member, refund a sent dangerous prefix, or bypass evaluator parity.
