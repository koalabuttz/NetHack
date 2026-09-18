# ScriptedReflex upgrade — implementation report

Status of the implementation of `doc/agent-reflex-upgrade-plan.md`
(Revision 3).  This report is written to the plan's own honesty rule:
figures that were not measured are marked **pending**, and work that was not
done is stated as not done rather than described as if it were.

Baseline commit: `66ba39238` (plan approved).
Implementation commits: `7e74fb4d4`, `d821ae180`, `d298850c4`.

## 1. Headline status

| Wave | Scope | Status |
|---|---|---|
| 1 | Neutral DTO/identity + lifecycle scaffolding | **complete** (`7e74fb4d4`) |
| 2 | Instance/terrain/hero + pre-observe reconciliation | **partial** — deterministic memory layer complete (`d821ae180`); controller activation **not done** |
| 3 | One-Dijkstra candidates/navigation | **not started** |
| 4 | Scoped recovery/search/door/food budgets | **not started** |
| 5 | Isolated dangerous two-send transaction | **not started** (native prefix/cancellation fixture **not built**, so the exception is correctly **unavailable**) |
| 6 | Jev, replay/evaluation, measurement migration | **partial** — metrics module complete; evaluator migration, parity fixture, Jev raw-choice path and post-change campaigns **not started** |

The legacy gameplay path is unchanged.  No behaviour was activated that could
not be fully verified, which is why wave 2's controller wiring and waves 3-6
were deliberately not attempted rather than half-wired.

**Test totals (this tree):** 607 tests green across the agent suites
(`test_auto` 102, `test_auto_candidates` 54, `test_auto_instances` 39,
`test_auto_metrics` 8, `test_auto_providers` 217, `test_auto_replay` 52,
`test_auto_spectate` 92; `test_spectate` 43 also green).  Pre-change baseline
was 463 across the same four original suites; the deltas are purely additive.

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

**Not done in wave 2:** controller activation — no `on_action_sent`
ownership, no pre-observe reconciliation wired into `_on_obs`, no eager
`mem.observe` removal, no directive instance-scope settlement, no evaluator
plumbing.  See §7 for why.

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
   shadow-only attempt-event build on each decision.  Wiring it without a
   verified consumer risks exactly the "second state owner / premature
   mutation" failure the plan forbids, and any new event could perturb the
   recording-sensitive controller tests.  Deferred into wave 2 activation.
2. **Wave-2 controller activation not attempted.**  `_on_obs`
   (`controller.py:1225-1282`) still calls `self.mem.observe(self.snap)`
   eagerly.  Moving that behind reconciliation + single `SentAttempt`
   ownership touches a 2,192-line controller with intricate page/deadline
   bookkeeping and is guarded by 102 controller tests.  Doing it blindly
   risked leaving the tree red — i.e. violating the wave gate — so the
   verified memory layer was landed alone and the wiring was left to a
   focused, test-driven pass.  This is the main outstanding work item.
3. **Waves 3-6 not started** (`policy.py` navigation rewrite, bounded
   recovery/food/door budgets, the risky two-send transaction, evaluator
   migration, Jev raw-choice path, post-change campaigns).
4. **Forced-search exception remains unavailable.**  Per plan §5.3/§8.4 the
   native prefix (`m`)/continuation (`s`)/cancellation fixture is mandatory
   *before* enabling `risky-emergency-forced-search`.  It was **not built**,
   so the exception is correctly left unavailable — the conservative
   outcome the plan mandates, not a weakened substitute.
5. **No post-change campaign was run.**  Waves 1-2 changed no gameplay
   behaviour, so a post-change campaign would be measuring the unchanged
   legacy path and would be misleading.  It belongs with the wave 3
   navigation activation.

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

Not yet demonstrable because their subject code is not implemented: M05, M06,
M07, M08, M09, M10, M12, M14, M15, M16, M17, M18, M20, M21.

## 9. Commits

| commit | subject |
|---|---|
| `7e74fb4d4` | agent: neutral candidate/identity leaf (wave 1) |
| `d821ae180` | agent: instance/terrain/hero memory (wave 2) |
| `d298850c4` | agent: streaming exploration metrics module |
| `550c823d6` | agent: isolate the leaf import-graph test |

This report is itself committed on top of those, so its own hash is not
listed here.

All use explicit-path staging, author `NetHack Agent <agent@localhost>`,
subject <= 50 and body wrapped at 72.  `AGENTS.md`, `build.log`,
`playground/`, `/tmp` and the DeepSeek key are untouched; no NHDT headers
edited; no engine or profile changes; `safe_wait=on`
(`doc/agent-profile-v1.tsv:165`) is unmodified.

## 10. Deferred work and recommended order

1. **Wave 2 activation** (highest priority): single `SentAttempt` ownership,
   pre-observe reconciliation, rejection-set/deadline preservation,
   `incomplete` delivery-repair exception, instance-scoped directive
   settlement (`directives.py`), and the evaluator compatibility plumbing.
   Must land with the §8.3 interleaving matrix and M12/M18/M20.
2. **Wave 3**: `policy.py` one-Dijkstra multi-candidate preparation replacing
   the nearest-stair loop and the `[:8]` prefilter (`policy.py:415-440`), with
   M08/M09/M10/M16.
3. **Wave 4**: bounded scoped recovery/search/door/food budgets, exact
   refusal fingerprints, M15/M17.
4. **Wave 5**: build the native prefix/cancellation C fixture first; only
   then enable the ten-gated two-send transaction, with M14.
5. **Wave 6**: evaluator migration + parity fixture, Jev raw-choice path,
   campaign-summary integration for `exploration_metrics`, and the
   post-change zero-key campaign **after wave 3 changes behaviour**, compared
   per §10.2 against the baseline in §2 of this report.

## 11. What was not measured

* post-change campaign metrics (waves 1-2 changed no gameplay behaviour);
* full §10.2 metric set beyond the columns in §2 (risky activations, door/
  search/food attempt budgets, candidate rejection/retry/repair counts, paid
  usage — the features do not exist yet, so zero is uninformative);
* 78-column sweep of every modified file is satisfied for the files touched
  (checked with `awk 'length>78'`, clean), but the plan's repository-wide
  sweep over pre-existing files was not re-run.

No number in this report is a projection or an estimate.
