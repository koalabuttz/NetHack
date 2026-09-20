# Campaign bench plan (Revision 2 — DRAFT, pending plan review)

> **Round-1 plan-review provenance.** Review verdict: **REVISE — 3 High + 5 Medium + 1 Low + 2 scope notes, all addressed.** Changes: (1) a predeclared **termination-safety admission contract** — noninferiority on death / policy-exhausted-before-horizon / early-terminal rates, a minimum common exposure rule, and explicit unknown-outcome handling — deterministic and required before any apply, with AC11 and `test_termination_safety_admission_blocks_reckless_candidates` (§2, §3, §6); (2) a specified **forced-abort containment mechanism** — child SIGINT/SIGTERM → controller `_reap` (`controller.py:347–407`) plus an enforceable cgroup/systemd scope or approved cancellation API for the forced path — with the nested-group reaping test (§7, Phase 3); (3) test files renamed to `test/agent/test_auto_bench.py` / `test_auto_bench_judge.py` for the existing discovery gate, with an AC1–AC12 → test mapping table, postmortem-package tests, and the executable AC10 preflight test (§Implementation Summary, §Test Strategy); (4) **statistical policy hardening** — precommitted sample count, no early stop, no post-result changes, 4/arm as diagnostic-only, and a counterbalanced seeded pair order (§3); (5) exact **invalid categories** replacing "no invalid outgoing actions" (§2); (6) **scorecard exactness** — 15 enumerated exploration keys and precise usage-key provenance with legacy availability (§2); (7) a judge **question-bundling go/no-go** (§4); (8) **refreshed citations** at the pinned revision with a revalidate note (§Verified Facts); (9) a defined **provenance manifest** with hard-not-comparable vs reported-only rules (§1); (10) the **attempts_source admission policy** (§3); (11) **judge supervisor reuse** without the private `_WorkerSupervisor` (§4). Sections not named here are retained verbatim from the draft.

Design produced by `architect:architect-campaign-bench`. Operator decisions baked in: deterministic metrics own the objective; Jev is the per-episode fuzzy judge (advisory); postmortem analysis is agent/subagent work, never DeepSeek; tuning stays inside operator-approved rails (suggest-first).

## Goal

Build an artifact-first campaign bench around the existing agent tooling: run bounded campaigns, derive reproducible scorecards, compare behavior, request a cheap per-episode Jev second opinion, package regressions for coding-agent analysis, and suggest or narrowly apply approved parameter changes.

**Recommendation:** implement a small Python wrapper, not a new controller or general optimization framework. Deterministic metrics exclusively determine quality gates and the tuning objective. Jev outputs are advisory flags. Failure analysis belongs to the coding agent/subagent, never DeepSeek. Preserve immutable run configurations and existing artifacts.

Two important corrections to the supplied premise affect implementation: current code already rejects unpriced USD caps, and the reflex cap limits *applied decisions*, not paid consultations. Moreover, current Jev dispatch is disabled under either token or USD caps. A strict-budget live-Jev profile therefore cannot honestly ship merely by wrapping existing caps.

## Implementation Summary

Proposed new paths (not existing files):
- `tools/agent/bench.py`: CLI, validated spec, episode scheduling, checkpointing, stop policy, budget reservation, tuner orchestration.
- `tools/agent/bench_metrics.py`: scorecards, provenance/comparability checks, deterministic comparisons.
- `tools/agent/bench_judge.py`: typed Jev judge requests, worker lifecycle, strict result validation and judge usage ledger.
- `test/agent/test_auto_bench.py`, `test/agent/test_auto_bench_judge.py`: offline and fake-provider tests (renamed to the `test_auto*.py` pattern so the existing discovery gate runs them; the exact command is in Test Strategy).
- `doc/agent-campaign-bench.md`: schema, operation, acceptance policy, known limitations.

Keep these as ordinary modules; no service, database, dependency-heavy statistics framework, or agent hot-patching. Reuse `Controller.run_campaign(1)` in isolated episode directories, existing metrics, the existing provider worker transport and credential loader. A bench supervisor launches one such episode job at a time and retains the existing `campaign.json` in each job directory. Its own manifest/report describes the whole campaign. This preserves existing campaign mode untouched while permitting interleaved A/B scheduling and campaign-level admission control between episodes.

Suggested interface: `python3 -m tools.agent.bench {validate,run,score,compare,tune,package} ...`. These are proposed commands, not implemented commands. `run --tier dry-run` performs offline evaluation only; enabling the paid judge is allowed only for an explicit live tier.

## Verified Facts and Corrections

Evidence is from read-only inspection. No files were modified and no commands, tests, or live calls were run. The checkout changed during inspection (controller line numbers moved); pin a revision and revalidate citations before implementation. One search failed because it referenced a guessed launcher path; subsequent inspection located the actual `sys/unix/agent_runner.c`. **All line-number citations below were refreshed at the pinned revision during this review round and must be revalidated at implementation time** — the controller's line numbers have already moved repeatedly.

1. **Existing campaign integration.** `tools/agent/__main__.py:141–187` builds and validates `ProviderConfig`; `:222–226` constructs `Controller` and invokes its campaign loop. `tools/agent/controller.py:245–266` validates episode count, loops over `run_episode`, and writes a summary. Caps and cooldown configuration are exposed in `tools/agent/providers.py:72–139`; episode timeout and episode count are separate runner arguments, not fields of `ProviderConfig`.
2. **Results distinguish operational success from game performance.** `controller.py:3474–3487` defines `episode_ok`: clean closure, successful process/transport, no unanswered request, complete recording. This is not "won NetHack." `:3427–3501` emits outcome, stop reason, ticks, actions, invalids, reflex counters, forced-search counters, and usage. `write_campaign_summary` adds `exploration` from campaign recordings (`:3632–3659`). It writes private files and preserves original results if summary writing fails.
3. **Lifecycle artifacts.** `tools/agent/events.py:387–403` adds `record:"lifecycle"`, preserving the outer event schema and separate lifecycle `schema_version`. `tools/agent/lifecycle_metrics.py:150–170` reads sidecars; `:173–287` derives commitment median/p90, switching, eligible/resolved directive execution rate, target reach, terminal completeness, replacement anomalies, and pickup outcomes. Legacy/unevidenced values are null, not zero. The reader skips malformed JSON lines; it is not an artifact-integrity validator. Bench must independently detect truncation/corruption rather than treating a partial summary as complete evidence.
4. **Mutation report integration.** `test/agent/mutation_checks.py:35` identifies `fixtures/destination_commitment_report.json`; `:476–506` delegates artifact metrics to `summarize_artifact`; `:524–530` exposes `--artifact`. Correctness mutation gates remain separate from behavioral gates.
5. **Exploration interface correction.** `tools/agent/exploration_metrics.py:238` defines `episode_metrics(wire_path, meta, actions_path)`; `:296–318` defines `campaign_metrics(campaign_dir)`. `:138–154` returns observations, discovered/entered instance-scoped cells, stairs, depth, displayed turns/time advances, attempts/source, hero displacement, stationary/loop spans and teardown exclusions. `:187–280` derives attempts from action sidecars or labels displacement fallback. There is no explicit revisit-rate field in this returned schema. Existing loop spans measure repeated hero/time observations, not all moving AB cycles.
6. **Replay does not test live Jev.** `tools/agent/evaluate.py:1351–1381` explicitly falls back to scripted for Jev replay; `:1585–1590` states live Jev evaluation is incompatible. Offline replay is a deterministic compatibility/correctness check on recorded observations, not a counterfactual new game.
7. **Jev transport reusable, reflex semantics not reusable.** `tools/agent/providers.py:1461–1463` constructs `/systemone`; `:1690–1704` sends state/model/questions with a choice question. `:1608–1615` requires accepted terms and key; `:1744–1789` loads the secret and supervises a bounded worker. `tools/agent/worker.py:91–134,147–164` accepts a generic payload and returns JSON, without choice-specific parsing. It enforces HTTPS/loopback-test restrictions and same-origin redirects, and avoids leaking credentials. The current adapter proves choice support only; exact noul/score wire schemas and multi-question response semantics were not verified locally. They must be checked against current primary API documentation before judge implementation; do not guess the schema.
8. **Cost premise is outdated.** DeepSeek prices default to null (`providers.py:130–138`), but `:247–250` and `tools/agent/budget.py:259–278` reject a USD cap without a complete tariff. Token/USD admission reserves prompt plus completion bounds (`budget.py:365–390`). Unknown price and unknown exposure are separately tracked. Jev has a built-in tariff snapshot of $0.042/million input tokens and zero output price (`budget.py:157–165`). Therefore `estimated_usd` is not necessarily always zero when DeepSeek prices are absent; it can include priced Jev usage. The snapshot is repository evidence, not independent verification of current billing.
9. **Critical Jev bound limitation.** `budget.py:656–664`: positive reflex caps bound applied decisions; zero disables the paid tier. `:683–703` refuses Jev whenever USD or token caps are enabled because service token bounds are unknown. `controller.py:3266–3339` reserves and settles paid Jev attempts even when later rejected. Never multiply the applied cap by a presumed call cost and claim a spend bound.
10. **No DeepSeek postmortems in bench.** They exist today: `controller.py:2416–2475` runs optional postmortems only when reserve >0. Bench must explicitly set and record `postmortem_reserve=0`; default is 1 (`providers.py:88–89`). Reject bench configurations that enable them.
11. **Parameter exposure.** `providers.py:173–178` validates `1 < jev_relative_factor < 2`; `:179–218` validates caps and cooldowns. Navigation stall/door budgets are constants: `navigation.py:490–492` has `DOOR_INTERACT_MAX=2`, `STALL_MAX=3`, not exposed `ProviderConfig` knobs. Do not patch/expose them in this project.
12. **Seeds and stopping.** `doc/agent-architecture.md:413,604` reserves seeds to the trusted evaluator and says RNG initialization/control remains unverified. `controller.py:347–358` supplies worker, private root, data, sysconf and deadline, but no seed argument. Targeted search found save-directory seeding, not an RNG-seed interface. No matched-seed claim is justified. `controller.py:294–305` reaps/finalizes in `finally`; campaign summary is written after the episode loop, not guaranteed on interruption. No operator graceful campaign-stop API was located. Recorder-failure "graceful stop" means disabling paid work, not a user kill switch.

## Design

### 1. Spec, ownership, artifacts

`bench-spec/1` exact top-level fields:
- `schema_version`, `name`, `tier` (`dry-run|live`), `profile` (`smoke|full|confirmation`), `provider_config_ref`, `overrides`, `episodes`, `episode_timeout_s`, `campaign_timeout_s`, `replay_inputs`, `baseline_ref`.
- `budget`: `strategy_calls_total`, `judge_calls_total`, `max_total_episodes`, `max_candidates`, `max_total_wall_s`, `usd_limit` (nullable), `cost_mode` (`priced-bound|call-bounded|operator-approved-unknown`), `external_limit_ref` (nullable).
- `judge`: `enabled`, `model`, `rubric_version`, `deadline_s`, `max_state_bytes`, `max_response_bytes`, `retries` (initially exactly 0).
- `comparison`: `metric_policy_version`, `target_metric`, `min_improvement`, `noninferiority_margins`, `min_samples`, `resampling_seed`, `resamples`, `confidence_level`.
- `tuning`: `mode` (`suggest|apply-approved`), `parameters`, `approval_id`, `approval_expiry`, `expected_base_config_hash`.

Resolve the operator config plus allowlisted overrides to a validated `ProviderConfig`. Config contains credential *references*, never raw keys. Do not copy referenced secret files into output. Reject unknown knobs, nonfinite values, impossible caps, postmortem reserve !=0, and scope-incompatible live-Jev budgeting before network work.

Every run records a redacted resolved config, config/code/worker/data/sysconf hashes, metric and provider presentation versions, fixed evaluation horizon, role, actual tier coverage, seed capability (`uncontrolled` initially), source checksums and comparison policy. Store outputs under unique run/variant/episode directories. Private permissions; atomic manifest/report updates; immutable completed episode artifacts. No overwriting an old run on resume.

**Canonical provenance manifest.** Define one manifest that is the single comparability authority; the same manifest shape is written for baseline and candidate and is hashed into the comparison record. It contains:

- **VCS:** commit id, dirty status, and a dirty-diff hash when the tree is dirty.
- **Imported code:** content hashes of every imported bench/agent Python module (the modules actually loaded, resolved from `sys.modules`, not a directory walk).
- **Binaries:** hashes of the worker and launcher binaries.
- **Data/sysconf:** hashes of the data and sysconf trees, or their versioned manifests where a tree hash is impractical.
- **Versions:** schema, rubric, and policy versions (scorecard schema, judge rubric, comparison/metric policy versions).

Comparability rules: a difference is **hard not-comparable** when it is behavior-affecting — a different commit id, a different hash of any imported bench/agent Python module, worker/launcher binary, data/sysconf manifest, or schema/rubric/policy version — even if the commit id is unchanged (a dirty working tree that alters a relevant module is not-comparable). Differences that are **reported-only** (do not by themselves force not-comparable) are non-behavioral metadata such as wall-clock timestamps, output directory paths, and run labels. The manifest records both categories so a comparison can state precisely which difference drove its verdict.

A bench manifest lists scheduled/completed/interrupted episodes, episode artifact paths/checksums, budget reservations/settlements and run status. Preserve each existing campaign JSON. Proposed generated files: `manifest.json`, `scorecards.json`, `comparison.json`, `judge.jsonl`, `postmortem/manifest.json`, `tuning-report.json`, `report.md`.

### 2. Scorecard contract

`episode-scorecard/1` has exactly these sections:
- `schema_version`, `episode_id`, `provenance_id`, `source_hashes`.
- `integrity`: `status` (`complete|partial|missing|invalid`), `reasons`, `recording_complete`, `operational_ok`, `requested_tiers`, `observed_tiers`.
- `termination`: `stop_reason`, `outcome`, `closed`, `returncode`, `protocol_failure`, `failure_reason`, `forced_kill`, `unanswered`.
- `activity`: `ticks`, `needs`, `actions`, `invalids`, `boundaries`, `strategy_calls`, `directives_applied`.
- `exploration`: exactly the **15** existing fields from `exploration_metrics.py:138–154`, enumerated so the schema is fixed rather than counted — `observations`, `discovered_cells_instance_scoped`, `entered_cells_instance_scoped`, `stairs_from_map_triples`, `depth_max`, `depth_final`, `displayed_turns`, `time_advances`, `attempts`, `attempts_source`, `hero_displacements`, `stationary_span_max`, `longest_loop_span`, `loop_spans_ge_2`, `teardown_frames_excluded` — plus `entered_per_100_attempts` as the sole initially derived productivity metric, null if attempts unavailable/zero; not a revisit measure.
- `lifecycle`: exact existing summarizer output keys from `lifecycle_metrics.py:244–287`.
- `reflex`: `applied`, `paid_dispatched`, `accepted`, `rejected`, `fallback`, `timeout`, `invalid`, `low_confidence`.
- `forced_search`: `activations`, `suffixes`, `successes`, `cancels`, `denials`, `trapped`, `uncleared` (detailed events stay in source artifacts).
- `usage`: the **campaign-summary reduced set is copied verbatim** from the campaign/meta artifacts (`controller.py:3546–3564`): `prompt_tokens`, `completion_tokens`, `estimated_usd`, `unknown_price_calls`, `unknown_exposure_calls`, `unknown_exposure_tokens`, `unknown_exposure_usd`, `cache_hit_tokens`, `cache_miss_tokens`, `cache_unclassified_tokens`, `cache_hit_rate`, `reasoning_tokens`. **Bench-derived extras** come from the per-episode ledger (`budget.py:734–761`): `reserved_bounds`, `tariff`, `usd_cap`, `token_cap`, and the per-provider `providers` block (`calls`, `prompt_tokens`, `completion_tokens`, `estimated_usd`); add bench `cost_status` (`known|partial|unknown`), `reserved_upper_usd`, `reserved_upper_tokens` (nullable). For legacy artifacts lacking a key, the key is **absent with an `availability` reason**, never a fabricated zero (a genuine reported zero stays zero). Never present aggregate zero USD as "free" when any paid usage is unknown.
- `availability`: mapping from unavailable metric path to reason (`legacy|missing-source|corrupt-source|zero-denominator|not-applicable|unsupported`); zero remains a valid measured value.

Judge results are separate and reference the immutable scorecard hash. Canonical sorting; no timestamps/absolute temp paths in metric content; byte-stable repeated scoring; timestamps in the manifest. Scorecard schema changes deliberately rather than silently importing arbitrary new fields.

**Gates versus observations:**
- Hard gates: artifact/protocol integrity; the **invalid-action taxonomy** below; no uncleared forced-search prefixes; no prohibited postmortem call; the **termination-safety admission contract** below (required before any apply); fixed budget/approval compliance; expected live-tier evidence; compatible provenance; required metric availability.
- **Invalid-action taxonomy (replaces "no invalid outgoing actions").** The gate must not be a single vague predicate:
  - **Native invalid counts by code**, parsed and exposed per `protocol.INVALID_CODES` code from the engine `invalid` records, never collapsed to one number.
  - **`incomplete` is not a gameplay invalid.** It is delivery repair (`controller.py:1886–1919`; the `incomplete` branch resets delivery rather than excluding the candidate), governed by a **separate delivery-integrity policy**: a repaired/incomplete delivery is a hard failure only if it never resolves to a successful send within the bounded retry budget; it does **not** count against the gameplay-invalid gate.
  - **Local validation-fallback counts** (a candidate rejected locally before send) are derived from the decision sidecars, kept distinct from native invalids.
  - **Hard failures:** any native invalid code outside the operator-approved allowlist, any unresolved `incomplete` delivery repair, and any local validation fallback that nonetheless reaches the engine. Prompt, inventory, and other non-gameplay frames are neither. No hard failure is offsettable by coverage or a judge score.
- **Termination-safety admission contract (required before any apply).** Because entered cells can be gamed by reckless play, predeclare a **deterministic-only** termination-safety admission evaluated on every comparison and required before any `apply-approved`:
  - **Noninferiority limits** on adverse termination rates — `death`, `policy-exhausted-before-horizon`, and the early-terminal categories of the termination section (`stop_reason`/`outcome` classification) — at operator-approved absolute noninferiority margins.
  - **Minimum common exposure:** both arms must complete the **same fixed evaluation horizon**; if either arm terminates early so exposure differs, the comparison is **not-comparable** (never silently averaged anyway).
  - **Unknown outcomes:** an unknown `game_outcome` or unclassified early terminal counts as **adverse/unknown evidence** (inconclusive at best), never a pass or a benign terminal.
  - Confirmation runs must pass the contract before apply; a candidate that fails it is rejected regardless of coverage gain.
- Primary target: mean entered instance-scoped cells per episode at the same fixed horizon. Require improvement and protect the metrics below. No wall-clock objective (provider latency varies).
- Behavioral guardrails: entered-cell productivity, depth_max, time_advances (noninferiority); longest_loop_span and stationary_span_max (no material increase). Operator-configured absolute thresholds can detect gross failures in smoke runs. Display full distributions.
- Initially observations only: commitment lengths, switch rate, directive rates, reach/pickup outcomes, discovered cells, provider acceptance/fallback/cache rates, termination/game outcome. Terminal completeness is not blindly required to be 1 for horizon-truncated episodes with an active commitment.

### 3. Comparison and variance

One comparison engine for A/B and candidate-versus-baseline. Report `pass|fail|inconclusive|not-comparable`, per-metric direction, sample counts, missing counts, baseline/candidate summaries, effect, confidence interval, margin, gate decision and reason, plus config diff and provenance differences.

No matched-seed comparisons initially. Interleave baseline and candidate episodes in a **fixed seeded balanced schedule that randomizes/counterbalances pair order** (both AB and BA occur; never always baseline-first), so ordering effects cannot align with the candidate; retain every episode including failures. Same engine/data/role/budgets/horizon/provider versions required except the declared change. Engine or horizon changes require a new baseline. Attempts-source differences make comparisons not-comparable, not merely noisier (see the attempts_source admission policy below).

**Precommitted sample and no post-result change (universal comparison-engine invariant, applied to A/B, candidate-vs-baseline, and tuning — not a tuning-only rule).** Each run must **precommit an exact sample count** and complete **all** scheduled episodes regardless of interim results; there is no early stop on a favorable (or unfavorable) interim look. Post-result changes to the metric set, margins, candidate set, sample count, or resampling policy are forbidden; if one must change, abandon the run and re-precut it with a new sample — never edit it in place. **4 episodes/arm is diagnostic screening only**, not acceptance; promoting a decision to acceptance/apply at that power requires explicit operator approval at that power.

**attempts_source admission policy.** Admission and automatic apply require `attempts_source == "actions"` (real attempt accounting from the actions sidecar). A matching displacement-fallback source is allowed **only for diagnostic comparisons**; a missing or corrupt actions sidecar makes attempts-based productivity **unavailable required evidence**, so the productivity gate cannot pass (it is not a measured zero).

**Termination-safety admission at confirmation.** Every comparison — and confirmation before apply — evaluates the §2 termination-safety admission contract. A candidate that improves coverage but raises `death`/`policy-exhausted-before-horizon`/early-terminal rates beyond the approved noninferiority margin, or that cannot establish minimum common exposure, fails or is inconclusive regardless of coverage gain.

Smoke (2 episodes per variant): hard invariants and gross absolute bounds only; behavior deltas diagnostic; never authorizes auto-apply.

Full screening (at least 4 per variant): reproducible unpaired bootstrap intervals over episode-level metrics, fixed seed/resample count, simultaneous guardrail coverage (e.g. Bonferroni). Pass only if each higher-is-better guardrail's lower difference bound is >= negative approved margin, each lower-is-better guardrail's upper bound is <= approved margin, and the target's lower improvement bound exceeds `min_improvement`. Missing evidence or broad intervals => inconclusive. Zero tolerated regression on hard safety gates; behavioral margins are explicit practical tolerances, not changed after results.

For tuning: screen a small fixed candidate set, then test the selected winner on fresh confirmation episodes (proposed minimum 8 per arm). Selection and confirmation budgets reserved before searching. No repeated peeking until success; inconclusive at maximum budget means suggest-only. Provider nondeterminism remains an explicit limitation.

### 4. Jev per-episode judge

A thin bench-side client reuses `load_secret`, `jev_endpoint`, and the bounded worker transport/supervisor. Do not instantiate `JevReflex` or route judge answers through controller arbitration. Judge calls run after scorecard sealing, outside episode gameplay, charged to a separate bench ledger.

One bounded `/systemone` request per completed eligible episode, three named questions, whose transport shape depends on a **preflight go/no-go** (below):
- `degenerate_loop` (noul): repetitive nonproductive behavior rather than reasonable local recovery?
- `exploration_productivity` (score): rubric-anchored assessment from coverage, attempts, depth/time progress and lifecycle consistency; normalize 0–1 locally only after validating the actual API score scale.
- `termination_sanity` (noul): recorded ending consistent with stated limits/outcome/evidence? Insufficient terminal evidence is unavailable, not "sane."

**Question-bundling go/no-go (preflight).** The current adapter constructs exactly **one** choice question (`providers.py:1690–1707`) and `_choice_from` parses exactly one answer; a bundled noul/score response in one request is **unproven**. Make preflight resolution an explicit go/no-go:
- if **primary API documentation plus a contract fixture prove** that a single request carries all three bundled questions, retain **one request** and update `judge_calls_total`, per-episode eligibility, atomicity/partial-answer semantics, caching, and cost accounting accordingly;
- otherwise issue **three independently budgeted single-question calls** (each counted in `judge_calls_total` and per-episode eligibility, each charged separately), or **disable** unsupported question types.

Regardless of branch, define atomicity/partial-answer semantics (a partial response is invalid, never partially credited), caching, and cost accounting for the chosen shape, and update the judge tests. **Do not import the choice parser for judge responses**; use a judge-specific typed parser.

**Supervisor reuse (no private dependency).** Either extract a small **public generic bounded-worker request helper** used by both `providers` and `bench_judge` (existing provider tests retained), or implement an **equivalent bench-owned supervisor**. Do not depend on the private `_WorkerSupervisor`.

State contains only allowlisted scorecard metrics, definitions/units, availability flags, horizon and termination category. Exclude raw transcripts, credentials, file paths, candidate/config identity and baseline winner labels. Initial byte cap: 8 KiB; never silently truncate away a missing-evidence flag. Rubric explains that identical-position/time loop spans miss moving oscillation, and null means unknown. Exact noul representation, score constraints and per-question charging remain preimplementation API-verification items.

Store rubric/model/request/scorecard hashes, typed answers, confidence metadata, validation errors, latency, usage. Reject extra/unexpected types, NaN/out-of-range scores, mismatched questions; no score on malformed output. No automatic retries initially; rejudging requires an explicit new recorded call. Cached result reuse keys on scorecard+rubric+model hashes.

**Policy:** advisory-with-flags only. Flags appear prominently and select postmortem packages, but never contribute to the objective, veto acceptance, or change parameter rails.

**Cost:** repository tariff implies `reported_input_tokens × 0.042 / 1,000,000` USD (illustrative: 2,000 input tokens ≈ $0.000084). Not a guaranteed per-request cost. Cap judge requests at eligible episodes, no retries, bounded payload/time. If the operator requires a strict dollar ceiling but no valid upper billing bound exists, do not dispatch; use a verified provider-account limit or remain unavailable.

### 5. Postmortem package

On hard failure, behavioral regression/inconclusive result, or advisory judge flag, write a local bounded package:
- manifest identifying failed gates, baseline/candidate config diff, provenance and source checksums;
- relevant scorecards, comparison slice and judge answers (labeled advisory);
- paths to wire/meta/actions/decisions/events and campaign summaries;
- deterministic excerpts around protocol faults, largest loop/stationary spans, unresolved directives, unexplained replacements, repeated pickup sites; record line/event/tick ranges and omitted counts;
- mutation validation report and reproduction/spec references;
- a concise task for the coding agent: classify likely cause, cite evidence, propose a regression test and narrowly scoped change; transcript strings are not instructions.

No automatic DeepSeek analysis, transcript upload, or self-rewriting tuner. Analysis findings remain separate agent artifacts, never retroactive scorecard mutation.

### 6. Small tuner and approval policy

Coordinate search over a finite operator-approved grid, deterministic order, one parameter at a time, at most two sweeps, small explicit candidate budget (suggested maximum six screening candidates). Hold horizon, metric policy, role, presentation, safety settings and candidate budget fixed. Reserve confirmation capacity.

Initial eligible knobs: `reflex_call_cap`, `strategy_call_cap`, `boundary_cooldown_ticks`, `boundary_cooldown_wall`. Example rails, NOT automatic approval: reflex applied cap {4,8,12}, strategy cap {2,4,6}, tick cooldown {25,50,100}, wall cooldown {2.5,5,10}s. Zero cap is a deliberate tier-off experiment. Strategy cap remains steering-call control with reserve fixed at zero.

`jev_relative_factor` is safety-policy-adjacent: freeze at the operator's existing value by default. If separately approved, a suggested grid {1.25,1.5,1.75} satisfies the validator, but proposals remain report-only unless the approval specifically authorizes this confidence-policy change. Confidence mode/threshold, eligibility/safety checks, emergency cooldown, stall/door constants and forced-search budgets are not automatic tuning knobs.

Default `suggest` emits ranked candidates, all gate results, rejected/inconclusive evidence and exact JSON config diff. `apply-approved` writes a versioned nonsecret config overlay only after fresh confirmation passes, the **§2 termination-safety admission contract** passes, approval is valid, every changed key/value is in its exact authorized grid/range, baseline config hash matches, and no safety-semantic key is implicitly included. Atomically advance an operator-designated active-config reference; retain the previous reference for rollback. Never rewrite secret config files or patch a running agent. Trace approval id, hashes, before/after diff, evidence/run ids and rollback target.

### 7. Cost, safety and stop controls

Per-episode caps reset today; bench must reserve the whole next episode's approved resource allocation before launching it. No parallel live jobs initially. Sum caps across episodes/candidates/judges. Strategy exposure bound includes conservative prompt bound plus `deepseek_max_tokens`, cache misses at the conservative price, and timed-out dispatched calls. Do not equate output max_tokens with total tokens.

Supported first profiles:
1. Offline dry-run: ReplayPass, no network, zero judge calls, scripted fallback explicitly labeled.
2. Strict-budget scripted reflex + optional DeepSeek steering: supplied complete DeepSeek tariff/USD cap or strict strategy-call and bounded payload/output configuration, explicit total episode budget. Jev judging separately bounded as above.
3. Live Jev reflex: **blocked for strict-spend automatic tuning until a hard paid-call/spend bound is verified.** Current token/USD caps disable Jev, and applied cap cannot supply that bound. Prefer an existing verified provider-account hard allowance if available. Without it, only an explicitly operator-approved, wall/tick-bounded unknown-exposure diagnostic run is honest; do not call it strict-budget or allow unattended auto-apply. Do not quietly add an intercepting proxy; a paid-dispatch limiter inside controller/provider admission would solve this but violates the no-live-behavior-change scope and requires separate approval.

Implement bench stop-after-current-episode via first SIGINT/SIGTERM or stop-file: persist requested stop, admit no new episodes/judges, let the bounded current episode finish. Second interrupt or total deadline triggers bounded teardown of the episode supervisor and its worker/launcher groups, then records partial status and retains artifacts. Explicitly test nested process groups (the controller starts a new session; killing only its parent PID is insufficient). On startup/resume, interrupted reservations remain unknown exposure, not refunded automatically. Resume is opt-in and validates hashes/locks; never replay a possibly billed judge request silently.

**Forced-abort containment mechanism (specified).** Make the behavior implementable rather than aspirational:

- **First (graceful) path:** the bench episode child installs explicit **SIGINT/SIGTERM handlers** that request cancellation through the Python controller so `run_episode()` reaches its `finally`, where the controller-owned `_reap` tears down the launcher session (`controller.py:347–407`); the bench waits a **bounded** interval for that cleanup to complete.
- **Second/forced path:** wrap the bench child in an **enforceable primitive** — either a **Linux cgroup or systemd scope** placed around the bench child (so the kernel reaps the whole process group even across descendants that ignore TERM), **or** an explicitly approved minimal controller-facing cancellation API that exposes the owned process-group ids. Choose one and document it.
- **Test:** `test_forced_abort_reaps_nested_launcher_and_provider_groups` drives bench child → controller launcher session → a **TERM-ignoring descendant** and asserts **every PID is gone** after the forced path (no survivors in the group), exercising the chosen primitive.

## Implementation Plan

1. **Pin and preflight:** freeze a revision, wait for the caller's vapor-cloud fix before any live tests, validate current interfaces, document corrected cap semantics, verify Jev noul/score API and billing contract. No engine edits.
2. **Artifact-only core:** spec parsing, manifest, scoring/integrity, comparison engine, fixture reports. Import existing artifacts and generate postmortem packages without credentials/network.
3. **Runner and operational controls:** dry-run integration, one-episode campaign wrapper, atomic checkpoints, caps/reservations and stop behavior (including the §7 forced-abort containment mechanism — child SIGINT/SIGTERM → controller `_reap`, plus the chosen cgroup/scope or approved cancellation API for the forced path). Prove fake-child process cleanup and actual-tier detection. Gate strict live-Jev support as described rather than weaken preflight.
4. **Judge:** implement verified typed schema over existing worker transport, fake endpoint tests, separate usage accounting; optional approved single live contract probe only after the vapor-cloud prerequisite and budget permission.
5. **Tuner:** report-only finite search first; confirmation and approval-bound config overlays afterward. Never start with autonomous live applies.
6. **Regression workflow/docs:** wire correctness suite + mutation harness + offline bench into per-change work; approved 2-episode smoke and 4+ full behavior campaigns follow separately. Store a versioned baseline and calibrate practical margins before permitting automatic apply.

## Acceptance Criteria

AC1. Existing artifact scoring is byte-stable, versioned and faithful; missing/legacy/corrupt/zero-denominator data never becomes measured zero.
AC2. Dry-run makes no provider calls and reports that Jev behavior was not evaluated.
AC3. Every live run records requested versus actually observed tiers; a fallback-only Jev campaign cannot pass live-Jev validation.
AC4. Hard gate failures cannot be offset by coverage or judge scores; incomplete/mismatched required evidence yields inconclusive/not-comparable.
AC5. All paid activity is reserved/accounted or explicitly marked unknown exposure; unsupported strict-spend configurations fail before dispatch. Applied reflex caps are never presented as paid-call ceilings.
AC6. Bench campaigns make zero DeepSeek postmortem calls; Jev judge is per-episode, typed, bounded, advisory and separately metered. Cached results key on scorecard+rubric+model hashes, and any rejudge is a new recorded call (never a silent recompute); `judge_calls_total` counts dispatched calls with cache hits reported separately, so cache/rejudge accounting is distinguishable.
AC7. Stops admit no further work, reap all children within the documented escalation bound, preserve partial artifacts and never label interruption successful.
AC8. Search is finite, repeatable over frozen input reports, obeys episode/wall/candidate budgets and requires fresh confirmation before apply.
AC9. Apply changes only authorized keys/ranges against the approved config hash; every apply has exact diff, evidence and rollback pointer. Default remains report-only.
AC10. Live testing does not begin before the vapor-cloud prerequisite lands; its status is supplied by the caller, and the gate is enforced by an executable preflight test (`test_live_testing_gated_on_vapor_cloud_attestation`), not by convention.

AC11. The termination-safety admission contract (§2) is predeclared, deterministic, and blocks any apply: a candidate that raises `death` / `policy-exhausted-before-horizon` / early-terminal rates beyond the approved noninferiority margin, or that cannot establish minimum common exposure, fails or is inconclusive regardless of coverage gain.

AC12. A postmortem package is bounded, checksummed and untrusted-transcript-safe: it contains a manifest, bounded excerpts with line/event/tick ranges and omitted counts, checksums, and task text that is explicitly not instructions.

## Test Strategy

**Discovery.** The bench test files are `test/agent/test_auto_bench.py` and `test/agent/test_auto_bench_judge.py` (renamed to the existing `test_auto*.py` pattern so the discovery gate collects them). Exact command for the validation gate:

```
python3 -m pytest test/agent/test_auto_bench.py test/agent/test_auto_bench_judge.py
```

(equivalently the repository `test/agent` discovery gate that collects `test_auto*.py`). This command is part of the bench validation gate.

Named tests (proposed, implementer executes):
- `test_scorecard_roundtrip_byte_stable`
- `test_legacy_lifecycle_unavailable_not_zero`
- `test_torn_sidecar_marks_partial_despite_summarizer_output`
- `test_attempts_source_mismatch_not_comparable`
- `test_operational_ok_is_not_game_victory`
- `test_low_denominator_lifecycle_rates_remain_observations`
- `test_dry_run_network_is_impossible`
- `test_live_jev_capped_fallback_not_reported_as_live_coverage`
- `test_reflex_applied_cap_not_used_as_paid_call_bound`
- `test_usd_without_tariff_rejected_before_spawn`
- `test_jev_token_or_usd_cap_profile_rejected_before_spawn`
- `test_episode_allocations_sum_across_candidates_and_confirmation`
- `test_unknown_exposure_survives_timeout_and_resume`
- `test_bench_forces_zero_postmortem_reserve`
- `test_judge_payload_contains_only_scorecard_allowlist`
- `test_judge_typed_answers_missing_nan_extra_questions`
- `test_judge_timeout_no_retry_and_no_secret_in_artifacts`
- `test_judge_disagreement_does_not_change_objective_or_gate`
- `test_comparison_fixed_resampling_and_inconclusive_small_sample`
- `test_hard_failure_cannot_be_offset_by_target_improvement`
- `test_stop_after_episode_prevents_next_episode_and_judge`
- `test_forced_abort_reaps_nested_launcher_and_provider_groups`
- `test_partial_run_never_promoted_to_baseline`
- `test_tuner_finite_grid_and_reserved_confirmation_budget`
- `test_apply_requires_approval_range_hash_and_fresh_confirmation`
- `test_confidence_factor_frozen_without_specific_policy_approval`
- `test_overlay_apply_rollback_preserves_secret_config`
- `test_termination_safety_admission_blocks_reckless_candidates`
- `test_no_early_stop_and_no_post_result_extension`
- `test_counterbalanced_pair_order_is_deterministic`
- `test_invalid_gate_categories_are_exact`
- `test_scorecard_keys_are_exact_and_versioned`
- `test_dirty_python_change_makes_comparison_not_comparable`
- `test_attempts_source_fallback_both_arms_diagnostic_only`
- `test_attempts_source_fallback_one_arm_not_comparable`
- `test_postmortem_package_is_bounded_and_checksummed`
- `test_postmortem_task_text_is_not_instructions`
- `test_live_testing_gated_on_vapor_cloud_attestation`

### AC → named-test map

Each acceptance criterion's named tests; a mutation that regresses an AC must fail at least one of them.

| AC | Named tests |
|---|---|
| AC1 | `test_scorecard_roundtrip_byte_stable`, `test_legacy_lifecycle_unavailable_not_zero`, `test_torn_sidecar_marks_partial_despite_summarizer_output`, `test_operational_ok_is_not_game_victory`, `test_low_denominator_lifecycle_rates_remain_observations`, `test_scorecard_keys_are_exact_and_versioned`, `test_attempts_source_fallback_both_arms_diagnostic_only` |
| AC2 | `test_dry_run_network_is_impossible` |
| AC3 | `test_live_jev_capped_fallback_not_reported_as_live_coverage` |
| AC4 | `test_hard_failure_cannot_be_offset_by_target_improvement`, `test_termination_safety_admission_blocks_reckless_candidates`, `test_invalid_gate_categories_are_exact`, `test_attempts_source_mismatch_not_comparable`, `test_attempts_source_fallback_one_arm_not_comparable`, `test_dirty_python_change_makes_comparison_not_comparable` |
| AC5 | `test_reflex_applied_cap_not_used_as_paid_call_bound`, `test_usd_without_tariff_rejected_before_spawn`, `test_jev_token_or_usd_cap_profile_rejected_before_spawn`, `test_episode_allocations_sum_across_candidates_and_confirmation`, `test_unknown_exposure_survives_timeout_and_resume` |
| AC6 | `test_bench_forces_zero_postmortem_reserve`, `test_judge_payload_contains_only_scorecard_allowlist`, `test_judge_typed_answers_missing_nan_extra_questions`, `test_judge_timeout_no_retry_and_no_secret_in_artifacts`, `test_judge_disagreement_does_not_change_objective_or_gate` |
| AC7 | `test_stop_after_episode_prevents_next_episode_and_judge`, `test_forced_abort_reaps_nested_launcher_and_provider_groups`, `test_partial_run_never_promoted_to_baseline`, `test_unknown_exposure_survives_timeout_and_resume` |
| AC8 | `test_comparison_fixed_resampling_and_inconclusive_small_sample`, `test_tuner_finite_grid_and_reserved_confirmation_budget`, `test_no_early_stop_and_no_post_result_extension`, `test_counterbalanced_pair_order_is_deterministic` |
| AC9 | `test_apply_requires_approval_range_hash_and_fresh_confirmation`, `test_confidence_factor_frozen_without_specific_policy_approval`, `test_overlay_apply_rollback_preserves_secret_config` |
| AC10 | `test_live_testing_gated_on_vapor_cloud_attestation` |
| AC11 | `test_termination_safety_admission_blocks_reckless_candidates` (the named regression in which a candidate enters more cells but dies / exhausts policy more often and must fail or be inconclusive) |
| AC12 | `test_postmortem_package_is_bounded_and_checksummed`, `test_postmortem_task_text_is_not_instructions` |

Reuse existing mutation_checks artifact reporting; add fixture assertions tying the bench lifecycle section to the same summarizer output. Fake endpoints and fake children cover operational behavior without spend. Any later live evidence must state exact config, revision, episode counts, actual tiers and usage completeness.

### Mutation checks

Each mutation killed by its named test: scorecard silently coerces null→zero (AC1); dry-run performs a provider call (AC2); fallback-only live run passes tier validation (AC3); hard-gate failure offset by target improvement (AC4); postmortem reserve not forced to zero (AC6); stop admits the next episode (AC7); tuner peeks until success / exceeds candidate budget (AC8); apply without approval/hash/range (AC9); confidence factor auto-tuned without specific approval (AC9/D3); interrupted run promoted to baseline (AC7); termination-safety admission ignored so a coverage-gaining reckless candidate passes (AC11); early stop on a favorable interim look or a post-result margin/sample change (AC8).

## Review Strategy

Review artifact contracts and statistical policy first, then budget/credentials/interrupt handling, then tuner apply logic. After implementation: the execute agent dispatches an independent reviewer against the actual diff and AC1–AC10 plus inherited contracts; the execute agent fixes or explicitly rebuts every finding, reruns affected tests, and redispatches after Critical/Important fixes until none remain or an operator decision is required. Require independent review of the two highest-risk claims: enforcement of paid exposure and interpretation of nonseeded results.

## Documentation Strategy

Document the revised cost facts, applied-versus-paid distinction, no-DeepSeek-postmortem policy, exact field units/availability, lack of moving-cycle/revisit metrics, judge rubric and advisory status, seed limitations, baseline compatibility rules, profile costs, stop escalation and rollback in `doc/agent-campaign-bench.md`. Link from `doc/agent-architecture.md` and the D3 follow-up documentation without rewriting safety policy. Check the repository `Files` manifest conventions before adding shipped files (`AGENTS.md:59–63`).

## Risks, Blockers, and Required Decisions

- **Resolved design:** wrapper rather than controller extension; deterministic multimetric gates and a simple target, not a Jev-weighted objective; unpaired comparisons (matched seed A/B not established; save restoration is a different scenario distribution).
- **Unresolved blocker:** strict live-Jev paid bounds. Existing cap behavior prevents pretending all desired constraints are simultaneously solved without external enforcement or separately approved behavior change.
- **Unresolved external contract:** noul/score schema, question bundling billing, current tariff and usage semantics. Generic transport is verified; judge request contract is not.
- **Statistical risk:** four episodes may be useful diagnostics but weak evidence. Margins and confirmation sample budget need operator approval; return inconclusive rather than manufacture certainty.
- **Measurement risk:** coverage favors exploration but does not prove survival/endgame skill; current loop metrics miss moving cycles, and lifecycle rates can improve by avoiding difficult tasks. Preserve raw evidence and diagnostic judge flags.
- **Operational risk:** sources are changing; do not calibrate or compare across unrecorded revisions. Cancellation may leave paid unknown exposure even after local process death.
- **Required operator decisions:** approve guardrail margins and confirmation sample budget; decide whether an external hard provider quota exists for strict live-Jev autotuning; approve the vapor-cloud prerequisite attestation before live bench testing.

## Non-goals

No engine changes, seed injection, live controller/reflex changes, mid-run tuning, general AutoML, new provider service/proxy, DeepSeek postmortems, credential migration, transcript-based model judging, automatic safety-policy changes, automatic fixing/committing code, or claims of NetHack win-rate validation from short exploration campaigns.

## Reviewer scrutiny

1. **Seed capability:** independently confirm launcher/config RNG interfaces before claiming absence or matched-seed support; the architecture explicitly leaves this unverified. The proposal safely assumes uncontrolled seeds.
2. **Cost accounting:** verify current budget code after in-flight edits; scrutinize tariff snapshots, missing usage, question billing, input bounds, repeated rejected consultations and the Jev cap refusal path. A reported estimate, deadline or applied cap is not a hard spend limit.
3. **Constraint conflict:** decide whether external hard provider quotas are available. Without them, strict-budget live-Jev autotuning remains blocked under the no-controller-change constraint. Do not conceal this with a watchdog.
4. **Typed Jev contract:** verify noul/score fields and response range with primary documentation and contract fixtures before production calls. This design deliberately does not invent the payload details.
5. **Stop ownership:** prove all launcher/provider groups are reaped; distinguish stop-after-episode from immediate abort, and preserve unknown billed exposure.
6. **Statistical acceptance:** approve margins and sample ceilings before results; avoid small-sample "no observed regression" claims and winner-selection bias.
7. **Missing evidence:** sidecar parsing can silently skip torn lines; bench integrity must independently prevent partial artifacts from passing. Distinguish absence of lifecycle opportunities from dropped telemetry.
8. **D3 ownership:** verify that a generic tuning approval cannot authorize confidence-gate changes. Relative factor stays frozen unless specifically approved.
9. **Prerequisite:** the caller must attest that the vapor-cloud fix is landed and tested before live bench testing; this design did not validate that change.
