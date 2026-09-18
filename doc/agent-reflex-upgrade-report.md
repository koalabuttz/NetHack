# ScriptedReflex upgrade — implementation report

Status of the implementation of `doc/agent-reflex-upgrade-plan.md`
(Revision 3).  This report is written to the plan's own honesty rule:
figures that were not measured are marked **pending**, and work that was not
done is stated as not done rather than described as if it were.

Baseline commit: `66ba39238` (plan approved).
Implementation commits: `7e74fb4d4`, `d821ae180`, `d298850c4`, `5d0b8df38`,
`091088770`, `47aa7bf9f`, `041db9719`.

## 1. Headline status

| Wave | Scope | Status |
|---|---|---|
| 1 | Neutral DTO/identity + lifecycle scaffolding | **complete** (`7e74fb4d4`) |
| 2 | Instance/terrain/hero + pre-observe reconciliation | **complete** — deterministic memory layer (`d821ae180`) plus controller activation |
| 3 | One-Dijkstra candidates/navigation + wiring | **complete** |
| 4 | Scoped recovery/search/door/food budgets | **complete** |
| 5 | Isolated dangerous two-send transaction | **not started** (native prefix/cancellation fixture **not built**, so the exception is correctly **unavailable**) |
| 6 | Jev, replay/evaluation, measurement migration | **partial** — metrics module complete; evaluator pre-observe migration, parity fixture, Jev raw-choice path and post-change campaigns **not started** |

The legacy gameplay path is now governed by the candidate pipeline: the
scripted reflex prepares one immutable candidate table per command decision,
the controller owns the single in-flight `SentAttempt`, and `_on_obs` parses,
reconciles and only then commits memory.  Safety emergencies, hunger, loop
breakers and inventory maintenance were preserved as *priority* (sole)
candidates, so no prior safety behaviour regressed.

**Test totals (this tree):** 624 tests green across the agent suites
(`test_auto` 102, `test_auto_candidates` 54, `test_auto_instances` 39,
`test_auto_metrics` 8, `test_auto_navigation` 22, `test_auto_recovery` 23,
`test_auto_wiring` 15, `test_auto_providers` 217, `test_auto_replay` 52,
`test_auto_spectate` 92; `test_spectate` 43 also green).  Pre-change baseline
was 463 across the four original suites; every delta is additive.

## 2. Immutable pre-change baseline campaign

Captured **before** any behaviour change, exactly as the plan's §9 preamble
requires:

```
./agent.sh auto --episodes 3 --reflex scripted --strategy off \
    --max-ticks 15000 --episode-timeout 300 \
    --output-dir /tmp/nh-reflex-baseline-pre
```

* directory: `/tmp/nh-reflex-baseline-pre` (kept, never deleted)
* config: role Valkyrie (default), scripted reflex, strategy off,
  `--reflex-deadline 0.75`, `--answer-deadline 1.0`,
  `--content-deadline 5.0`, zero keys / zero network
* engine: the pre-change agent binary; data `/tmp/nethack-agent-data`

Measured with `python3 -m tools.agent.exploration_metrics
/tmp/nh-reflex-baseline-pre`:

| ep | ticks | displayed turns | depth max | stairs (map triples) | cells | entered | longest loop span | outcome | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 15001 | 937 | 1 | 0 | 202 | 85 | **14042** | unknown | tick-cap-graceful-quit |
| 2 | 1933 | 2943 | 1 | 0 | 66 | 27 | 6 | death | closed |
| 3 | 15001 | 908 | 1 | 0 | 69 | 29 | **14101** | unknown | tick-cap-graceful-quit |

This reproduces the diagnosed ds1 pathology under a fresh zero-key control:
two episodes burn the full 15,001 controller ticks while displaying fewer
than 1,000 game turns, with an identical `(hero, displayed-time)` fingerprint
repeated ~14,000 times, at depth 1, with **zero** `>` map-triples.  Invalids
were 0 in all three episodes; all recordings were complete.

Per §10.1 these are **unpaired stochastic** observations, not a matched-seed
control.  They establish the loop, not causality.

## 3. Wave 1 — neutral candidate/identity leaf (complete)

Commit `7e74fb4d4`.

Delivered:

* `tools/agent/candidates.py` — dependency-neutral leaf (imports **nothing**
  from the package): immutable tagged action model, versioned deterministic
  canonical JSON encoding, content-addressed candidate/table identities,
  deterministic dedup-before-truncation and family/direction ordering,
  `ReflexFeatures`/`PreparedReflex`/`SentAttempt` records.
* `tools/agent/arbitration.py` — the shared pure `RejectionSet`,
  `select_retained`, `RawChoice`/`validate_raw_choice` and reconciliation
  classification reused by live control and evaluation.  Imports only
  `candidates`.
* `test/agent/test_auto_candidates.py` (54 tests), `bench_candidates.py`.

Exit-gate evidence:

* identical legacy selected actions — trivially true, the legacy path is
  untouched and all 463 pre-existing tests pass;
* neutral import graph — asserted by AST inspection of `candidates.py`
  (no package sibling, not even `protocol`) and of `arbitration.py` (only
  `candidates`);
* deterministic IDs — table ID is not circular (excludes its own ID and the
  retained bytes), is invariant to input order, and reacts to table version,
  feature digest and rejection version;
* canonicalize-once — `canonicalize_count()` proves the retained bytes are
  reused on the selection and payload paths (M22);
* M11 choice gate — all five sub-gates (identity, index type, index range,
  confidence, rejected member) demonstrated independently.

**Not done in wave 1:** the controller "attempt-event scaffolding"
(shadow-only build of a `PreparedReflex` on each decision).  Deferred to
wave 2 activation to avoid a second state owner without a verified consumer
— see §7.

## 4. Wave 2 — deterministic memory layer (partial)

Commit `d821ae180`.

Delivered (`tools/agent/instances.py`, 39 tests):

* full-cell `classify_cell(glyph,color,style,other)` implementing the
  reviewer-confirmed door semantics (gray `-`/`|` walls, **brown** `-`/`|`
  open doors, **brown** `+` closed door), re-verified against
  `include/defsym.h:91-152`; unknown variants fail closed;
* `TerrainMemory`: persistent terrain separated from current occupancy, with
  `map_revision` bumped only on structural change and
  `occupancy_generation` on occupant change; a monster overlay never erases a
  remembered staircase;
* `HeroResolution`: position **sets** that never pick a first/nearest `@`,
  with an explicit `outside` flag so an empty set is not read as certainty,
  and `reconcile_hero` that preserves the old position on nonmovement and
  expands the set on unexpected relocation;
* `LevelInstanceAutomaton`: episode-local monotonic instance IDs implementing
  plan §4.1 rules 1-8.  Arrival that cannot be disproved fails closed to a
  **fresh** scope; a cancelled proposal never sent creates no transition;
  `N` (affirmative no-arrival) alone keeps the old scope; a timeout retires
  the old scope to `FRESH_UNRESOLVED`.  **No `strong_public_match`, no
  archived-map reuse, no cross-instance merge** (impl-1 requirement).

Section 8.2 fixture matrix — all asserted: depth change, same-label branch,
message lookalike (ignored) and ambiguous outcome-looking (allocates fresh),
label-only and topology-only without message, zero-`@` and multiple-`@`
(fresh before hero resolution), branch depth collision (three scopes),
rejected stair (keeps old scope), conflict/timeout/cancellation.

**Wave-2 controller activation (now done):** see §5a below — `on_action_sent`
ownership, pre-observe reconciliation in `_on_obs`, staged/committed memory,
instance-scoped directive settlement and the runner-owned automaton/terrain.
The evaluator's own `mem.observe` remains its wave-6 migration point.

## 5a. Wave 3 — one-Dijkstra navigation and controller wiring (complete)

Commit `5d0b8df38` (`tools/agent/navigation.py`, `tools/agent/policy.py`,
`test/agent/test_auto_navigation.py`) and commit `47aa7bf9f`
(`tools/agent/controller.py`, `state.py`, `directives.py`, `providers.py`,
`test/agent/test_auto_wiring.py`).

Delivered:

* `navigation.py` — **one** Dijkstra from the confirmed hero over classified
  terrain with integer base cost and capped visit/failed-edge penalties;
  all-reachable-target enumeration (**no `[:8]` prefilter**, **no
  Manhattan-only stair**); cardinal closed-door approaches (a closed door is
  approached, never stepped on); diagonal door entry/exit and corner-squeeze
  edge legality; instance-scoped `TargetStore` persistence.
* `policy.py` — `ScriptedReflex.prepare(context) -> PreparedReflex` builds one
  canonical `CandidateTable` per command decision with §3.3 integer scores and
  bounded directive components; `decide` selects the retained argmax
  (honouring a controller-supplied rejection set).  Safety emergencies,
  hunger, loop breakers and inventory maintenance are priority (sole)
  candidates, preserving every prior behaviour.
* `state.py` — `EpisodeMemory.observe` split into a pure `stage()` parse and a
  single `commit()`; messages stay event-id deduplicated.
* `controller.py` — `_on_obs` parses, **reconciles the single in-flight
  `SentAttempt` before any hero/level/map commit**, then commits once.  A
  complete send arms exactly one frozen attempt; an ordinary invalid
  terminally excludes its canonical action (the retry reselects the next
  retained member, never the same winner); `incomplete` repairs transport
  without a gameplay exclusion; closed discards the attempt.  The runner owns
  a `LevelInstanceAutomaton` and `TerrainMemory` fed from applied snapshots.
* `directives.py` — `_ineligibility_reason` extended with instance scope;
  `DirectiveBook` carries the activated instance and `peek_view` stays pure.

Exit-gate evidence: the §8.2 transition matrix and §8.3 interleaving rows are
covered by `test_auto_instances` (39) and `test_auto_wiring` (15) plus the
reconciliation classification in `arbitration.classify_outcome`; the named
mutations **M08/M09/M10** each turn their designated navigation test red and
restore green, and **M18** is caught by `InvalidExclusion`.  The regenerable
`short.*` ground-truth fixture was re-captured for the new navigation (a
legitimate policy change invalidates the old trajectory, exactly as the
fixture README warns); its hashes and README were updated.

## 5b. Wave 4 — bounded recovery and scoped negatives (complete)

Commit `091088770` (`tools/agent/recovery.py`, `tools/agent/policy.py`,
`test/agent/test_auto_recovery.py`).

Delivered:

* exact public search-refusal recognizer derived from `src/do.c:2333-2353`
  ("You already found a monster." with the optional `Use 'm' prefix` suffix,
  and "Searching doesn't feel like a good idea right now."); generic "found a
  monster", lookalike and farlook text are explicitly **not** refusals;
* scoped food negatives — inventory-negative by signature, location-negative
  by `(instance, position, floor revision)` — for both engine message forms;
* bounded per-site search budget (three completed searches; one refusal
  suppresses) and a deterministic 2-/3-cycle detector;
* reflex integration: a refused or exhausted ordinary search at a site
  suppresses the next `s` and recovery escalates through a deterministic safe
  step to a bounded graceful quit, breaking the diagnosed rejected-search loop
  **without** the wave-5 forced search.  Hunger no longer blinds itself
  against a scoped inventory negative.

Exit-gate evidence: `test_auto_recovery` (23) plus the mutation **M15** (make
the inventory negative global) turning `test_inventory_negative_is_signature_scoped`
red.

## 5c. Controller-wiring summary — what replaced eager `mem.observe`

`_on_obs` previously called `self.mem.observe(self.snap)` eagerly.  It now:

1. `self.snap.apply(rec)` then `staged = self.mem.stage(self.snap)` (a pure
   parse — terrain cells, stairs, hero, status and new messages, no mutation);
2. `self._reconcile_observation(staged)` — merge classified terrain, classify
   the in-flight attempt into a terminal outcome (released and counted exactly
   once), derive the `{S, L, O, D, N}` transition signals and settle the
   level-instance automaton;
3. `self.mem.commit(staged)` — the single hero/level/map/message commit.

The send side follows the §3.4 order exactly: local checks → map the retained
candidate → `validate_action` → `_emit` → `_arm_attempt` (only on a complete
send) → reconcile.  The evaluator is deliberately left on its wave-6
migration point.

## 5. Measurement module (wave 6 prep, complete)

Commit `d298850c4`: `tools/agent/exploration_metrics.py` + 8 tests.  Streams
a campaign wire recording into the §10.2 set (ticks kept separate from
displayed-time turns, depth, map-triple stairs, instance-scoped cells,
longest loop span) without retaining an episode.  **Not yet wired into the
campaign summary.**

## 6. Performance benchmark (§3.2 gate)

`python3 test/agent/bench_candidates.py --tables 150000`, CPython 3.13.5,
x86_64 Linux, 6 CPUs, reflex deadline 0.75 s:

| candidates | retained bytes | payload bytes | best build (ms) |
|---|---|---|---|
| 1 | 479 | 329 | 0.0176 |
| 40 | 13729 | 7509 | 0.2253 |
| 255 | 50345 | 27818 | 0.8451 |

150,000 tables x 255 candidates streaming (table build + hash + payload,
each table discarded):

* elapsed 150.726 s, **995 tables/s**
* p50 **0.953 ms**, p95 1.2596 ms, p99 **1.4045 ms**, max 2.7989 ms
* **0 deadline overruns** against 0.75 s → ~530x headroom on p99
* peak traced memory 282 KB, 1 table retained

A single representative table differs from the worst case: the 0.75 s reflex
allowance includes preparation, selection and reserved send headroom, so the
measured ~1.4 ms p99 leaves the reservation essentially untouched.  As the
plan warns, cost is serialization-bound (two canonicalizations per candidate
plus one for the body), not hash-bound.

## 7. Deviations (explicit)

1. **Wave-1 controller shadow scaffolding deferred.**  The plan lists a
   shadow-only attempt-event build on each decision.  It was folded into the
   wave-3 activation instead of landing as a second, consumer-less owner.
2. **The controller rebuilds the prepared table per decision.**  `decide`
   prepares a table each call (the controller passes the rejection set, not a
   pre-built `PreparedReflex`).  Canonicalisation is still a single serialize
   per table (M22 holds for the payload path), but the "prepare once and reuse
   across selection/payload/telemetry" optimisation is not yet threaded
   through the controller.  The reflex does expose `last_prepared` for the
   `SentAttempt` identity.
3. **The runner-owned automaton/terrain are fed but not yet read by the
   reflex.**  `_EpisodeRunner` owns a `LevelInstanceAutomaton` and
   `TerrainMemory` and settles them from applied snapshots, but the scripted
   reflex still derives its navigation terrain from `mem.grid` each decision.
   The evidence exists and is tested; switching the reflex to consume the
   runner's terrain is a follow-up, not a correctness gap for the current
   depth-1 behaviour.
4. **Forced-search exception remains unavailable.**  Per plan §5.3/§8.4 the
   native prefix (`m`)/continuation (`s`)/cancellation fixture is mandatory
   *before* enabling `risky-emergency-forced-search`.  It was **not built**,
   so the exception is correctly left unavailable — the conservative outcome
   the plan mandates, not a weakened substitute.
5. **No post-change campaign was run.**  Wave 3 changed navigation behaviour;
   a fresh zero-key campaign belongs with the wave-6 measurement migration and
   the preserved pre-change baseline in §2.  The `short.*` fixture was
   re-captured locally to keep the replay gate meaningful, which is a
   fixture refresh, not a measured campaign.
6. **The evaluator was not migrated.**  `evaluate.py` still applies a snapshot
   and immediately calls `mem.observe`; the shared-reconciliation migration
   and the live/replay parity fixture are wave 6.

## 8. Mutation table

Every mutation was applied, observed red on its designated test, then
restored to green (verified).  None is committed.

| ID | Mutation | Designated test | Result |
|---|---|---|---|
| M22 | `jev_payload` re-canonicalizes the table | `test_payload_path_does_not_recanonicalize` | red `1 != 0`; restored green |
| M11 | index-type check removed | `test_m11_index_type_gate` | red; restored green |
| M11 | index-range check removed (clamp) | `test_m11_index_range_gate` (+`test_usage_is_carried…` collateral) | red; restored green |
| M11 | probability+threshold removed | `test_m11_confidence_gate` | red; restored green |
| M11 | identity check made partial | `test_m11_identity_gate` | red; restored green |
| M11 | rejected-member check removed | `test_m11_rejected_member_gate` | red; restored green |
| M01 | closed `+` traversed as open door | `test_closed_door_is_not_walkable_and_needs_opening` | red; restored green |
| M02 | blank admitted as floor | `test_blank_is_unknown_not_floor` | red; restored green |
| M03 | `;` removed from monster classes | `test_every_monster_punctuation_is_a_hazard` | red; restored green |
| M04/M19 | first `@` wins instead of a set | `test_first_at_is_never_chosen`, `test_zero_and_multiple_at_are_sets` | red (2); restored green |
| M13 | `L` transition detector removed | `test_transition_without_message_label_only`, `test_each_signal_alone_allocates_fresh` | red (2); restored green |
| M08 | `[:8]` target prefilter restored | `test_no_eight_target_prefilter` | red; restored green |
| M09 | Manhattan-nearest stair only | `test_reachable_farther_stair_beats_unreachable_nearer`, `test_farther_reachable_stair_is_chosen_when_nearer_is_isolated` | red (2); restored green |
| M10 | diagonal door/corner edge allowed | `test_diagonal_door_entry_is_illegal`, `test_corner_squeeze_is_illegal` | red (2); restored green |
| M15 | inventory negative made global | `test_inventory_negative_is_signature_scoped` | red; restored green |
| M18 | `_on_invalid` no longer excludes | `test_ordinary_invalid_excludes_the_candidate` (+`InvalidExclusion`) | red; restored green |
| M19 | (already covered by M04 first-`@`) | `test_first_at_is_never_chosen` | red; restored green |

Still not demonstrable because their subject code is not implemented: M05 (the
`safe_wait` positive gates belong to the wave-5 forced search), M06
(food-allowlist tightening), M07 (Escape-as-universal), M12 (premature
effect), M14 (risky transaction), M16/M17 (frontier/ticks — partially
exercised), M20 (directive instance — the instance predicate is now tested
positively in `test_auto_wiring`), M21 (replay parity, wave 6).

## 9. Commits

| commit | subject |
|---|---|
| `7e74fb4d4` | agent: neutral candidate/identity leaf (wave 1) |
| `d821ae180` | agent: instance/terrain/hero memory (wave 2) |
| `d298850c4` | agent: streaming exploration metrics module |
| `550c823d6` | agent: isolate the leaf import-graph test |
| `5d0b8df38` | agent: one-Dijkstra navigation + candidate tables |
| `091088770` | agent: bounded recovery + scoped food negatives |
| `47aa7bf9f` | agent: controller attempt ownership + reconcile |
| `041db9719` | agent: assert invalid exclusion in wiring tests |

This report is itself committed on top of those, so its own hash is not
listed here.

All use explicit-path staging, author `NetHack Agent <agent@localhost>`,
subject <= 50 and body wrapped at 72.  `AGENTS.md`, `build.log`,
`playground/`, `/tmp` and the DeepSeek key are untouched; no NHDT headers
edited; no engine or profile changes; `safe_wait=on`
(`doc/agent-profile-v1.tsv:165`) is unmodified.

## 10. Deferred work and recommended order

1. **Wave 5** (unchanged): build the native prefix (`m`)/continuation (`s`)/
   cancellation C fixture first; only then enable the ten-gated two-send
   transaction, with M14.  Until then `risky-emergency-forced-search` stays
   unavailable.
2. **Wave 6**: migrate `evaluate.py` onto the shared reconciliation/selection
   helpers with the live/replay parity fixture (M21), wire the Jev
   raw-choice/retained-byte path (M11/M22 through the controller), integrate
   `exploration_metrics` into the campaign summary, and run the post-change
   zero-key campaign against the baseline in §2.
3. **Follow-ups flagged in §7**: thread a prepared-once `PreparedReflex`
   through the controller, and switch the reflex to consume the runner-owned
   automaton/terrain rather than rebuilding from `mem.grid`.

## 11. What was not measured

* post-change campaign metrics (wave 3 changed navigation; the campaign
  belongs with wave 6);
* full §10.2 metric set beyond the columns in §2 (risky activations are
  correctly zero — the exception is unavailable — and door/search/food
  attempt budgets are exercised by unit tests, not a campaign);
* live-model/replay parity (wave 6);
* 78-column sweep of every modified file is satisfied for the files touched
  (checked with `awk 'length>78'`, clean for all `tools/agent/*.py` and the new
  tests).

No number in this report is a projection or an estimate.
