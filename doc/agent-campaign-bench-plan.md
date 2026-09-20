# Campaign bench plan (Revision 5 — DRAFT, pending plan review)

> **Round-1 plan-review provenance.** Review verdict: **REVISE — 3 High + 5 Medium + 1 Low + 2 scope notes, all addressed.** Changes: (1) a predeclared **termination-safety admission contract** — noninferiority on death / policy-exhausted-before-horizon / early-terminal rates, a minimum common exposure rule, and explicit unknown-outcome handling — deterministic and required before any apply, with AC11 and `test_termination_safety_admission_blocks_reckless_candidates` (§2, §3, §6); (2) a specified **forced-abort containment mechanism** — child SIGINT/SIGTERM → controller `_reap` (`controller.py:347–407`) plus an enforceable cgroup/systemd scope or approved cancellation API for the forced path — with the nested-group reaping test (§7, Phase 3); (3) test files renamed to `test/agent/test_auto_bench.py` / `test_auto_bench_judge.py` for the existing discovery gate, with an AC1–AC12 → test mapping table, postmortem-package tests, and the executable AC10 preflight test (§Implementation Summary, §Test Strategy); (4) **statistical policy hardening** — precommitted sample count, no early stop, no post-result changes, 4/arm as diagnostic-only, and a counterbalanced seeded pair order (§3); (5) exact **invalid categories** replacing "no invalid outgoing actions" (§2); (6) **scorecard exactness** — 15 enumerated exploration keys and precise usage-key provenance with legacy availability (§2); (7) a judge **question-bundling go/no-go** (§4); (8) **refreshed citations** at the pinned revision with a revalidate note (§Verified Facts); (9) a defined **provenance manifest** with hard-not-comparable vs reported-only rules (§1); (10) the **attempts_source admission policy** (§3); (11) **judge supervisor reuse** without the private `_WorkerSupervisor` (§4). Sections not named here are retained verbatim from the draft.

> **Round-2 plan-review provenance.** Review verdict: **REVISE — 3 High + 4 Medium + 2 Low, all addressed** in this revision. Changes: (1) an executable **terminal-classification table** (trusted `stop_reason` category first, then visible-text `outcome` — explicitly not proof, `recording.py:329–341` — then unknown) with a **numeric minimum common exposure** rule that counts all early adverse terminals in the rate denominators and marks insufficient exposure **inconclusive** (not not-comparable), plus four tests (§2, §3, AC11); (2) the forced-abort mechanism **decided** as a **bench-owned `/proc` PPID-recursion process-tree walk** (SIGKILL each discovered descendant and its process group, with documented race/PID-reuse/permission limits), keeping the graceful path primary and requiring no controller change or systemd dependency, with the nested-reap test including a TERM-ignoring descendant (§7, Phase 3); (3) the **provenance domains split** — deterministic run/scoring/comparison provenance may be hard not-comparable, but advisory judge provenance never invalidates the deterministic comparison or gates apply — with `test_rubric_only_change_leaves_deterministic_comparison_unchanged_and_forces_rejudgment` (§1, §3); (4) the **exact repository gate command** `python3 -m unittest discover -s test/agent -p 'test_auto*.py'` and four AC6 judge accounting/branch tests (§Test Strategy, §4, AC6); (5) **sample-count exactness** — 4/arm unconditionally diagnostic-only and exact `screening_episodes_per_arm`/`confirmation_episodes_per_arm` required in the spec (§1, §3, §6); (6) the **invalid-code allowlist** default EMPTY with exceptions in a versioned operator-approved policy (approval hash in the spec) and the "reaches the engine" clarification (§1, §2); (7) revised **judge evaluation wording** (§4); (8) **review scope AC1–AC12** and the refreshed Jev endpoint citation `providers.py:1468–1470` (§Review Strategy, §Verified Facts). Sections not named here are retained verbatim from Revision 2.

> **Round-3 plan-review provenance.** Review verdict: **REVISE — 3 High + 1 Low, all addressed** in this revision. Changes: (1) the termination classifier is redefined over **exact artifact values** — authoritative `stop_reason` values (`policy-exhausted` → adverse-early; `tick-cap-graceful-quit` → horizon-completion; bench interruption → excluded-from-comparison; deadline/protocol/transport/spawn/recorder failures and `closed-unanswered` → operational/integrity failure) plus, only for a generic `stop_reason="closed"`, the visible-text `outcome` (`death`/`starvation` → adverse observed; `ascension` → explicit category; else adverse/unknown) — with visible text stated as observational-but-usable-for-the-conservative-safety-rate, exact `min_completed_episodes_per_arm`/`min_aggregate_at_risk_ticks_per_arm` validation semantics, and production-shaped `(stop_reason, outcome)` fixtures (§2, AC11); (2) comparability is decided by **domain-specific content hashes, not commit-id inequality** (commit ids and dirty hashes recorded for audit only), with `bench_judge.py`, the rubric/model, and the judge schema excluded from the deterministic "any imported module" phrase and the rubric-change test strengthened (§1, §3); (3) the forced-abort walk is made **ownership-isolated** — launch the episode root in a dedicated session/process group, capture PID/start-time/session/PGID, never `killpg` the supervisor's/current group, re-walk while keeping the root alive, reap the root last and verify no captured identity survives, and treat any permission/identity-validation failure as **teardown-failure that fails AC7** (§7, Phase 3, AC7); (4) the implementation-plan campaign wording now uses the spec's exact `screening_episodes_per_arm` diagnostic campaign followed by the larger exact `confirmation_episodes_per_arm` campaign (§Implementation Plan). Sections not named here are retained verbatim from Revision 3.

> **Round-4 plan-review provenance.** Review verdict: **REVISE — 2 Medium closure gaps, both addressed** in this revision. Changes: (1) the terminal-classification table now enumerates the **complete exact administrative `stop_reason` vocabulary** — `content-deadline`, `episode-timeout`, `protocol-failure`, `transport-failure-write`, `transport-failure-eof`, `spawn-failure`, `recorder-failure`, `closed-unanswered`, with `content-deadline`/`episode-timeout` classified horizon/completion-or-adverse-early by a predeclared `deadline_classification` policy — defines canonical **bench-owned** persisted reasons `bench-stopped-graceful` (graceful operator stop) and `bench-aborted` (forced abort), maps both to excluded-from-comparison, states the never-benign fallback for any unrecognized future `stop_reason`, and extends the classifier fixture to one case per enumerated row (§1, §2, §7, AC11); (2) added `test_forced_abort_permission_or_identity_failure_sets_teardown_failure` — injecting `/proc` read / `getpgid`-identity / start-time / signal-permission failures and asserting partial status, `teardown_failure` set, non-success, and no baseline promotion — mapped to AC7 alongside the nested/inherited-PGID/spawn-during-rewalk scenarios, with the log-and-ignore mutation killed by it (§7, Test Strategy, AC7, Mutation checks). Sections not named here are retained verbatim from Revision 4.

Design produced by `architect:architect-campaign-bench`. Operator decisions baked in: deterministic metrics own the objective; Jev is the per-episode fuzzy judge (advisory); postmortem analysis is agent/subagent work, never DeepSeek; tuning stays inside operator-approved rails (suggest-first).

## Implementation preflight record (Phase 1 — FROZEN)

Pinned revision: **`41a9c7ae01fbdc58e3b3f70d5a660b6adb2c528a`** ("agent: qualify
route reopening under exit b"). Every citation below was re-validated against
this revision during implementation; line numbers that drifted from the design
round are corrected here and the implementation binds to the **symbols**, not
the numbers.

**Interface validation (all present, semantics unchanged):**

| Cited interface | Pinned location | Note |
|---|---|---|
| Campaign entry / loop | `controller.py:245` `Controller.run_campaign(episodes)` | calls `run_episode` per index, then `write_campaign_summary` |
| Episode result / success | `controller.py:3515` `episode_ok` | operational success, **not** game victory (AC1) |
| Reduced usage set | `controller.py:3587–3605` (not 3546–3564) | the `usage` block the scorecard copies verbatim |
| Campaign summary | `controller.py:3609` `campaign_summary`, `:3673` `write_campaign_summary` | writes 0600 `campaign.json` |
| Teardown | `controller.py:372` `_reap` | controller-owned launcher-session teardown |
| Exploration | `exploration_metrics.py:238` `episode_metrics`, `:296` `campaign_metrics`, `:138–154` 15 fields | matches §2 exact field list |
| Lifecycle | `lifecycle_metrics.py:150` `summarize_artifact`, `:244–287` keys | `None` = unavailable, never zero |
| Mutation artifact | `mutation_checks.py:733` `_lifecycle_summary`, `:781` `--artifact` | delegate confirmed |
| Jev transport | `providers.py:1468` `jev_endpoint`, `:1697–1707` request, `:534` `_WorkerSupervisor`, `worker.py:147` `run_job` | generic worker job protocol reusable |
| Secret / env | `providers.py:791` `load_secret`, `:494` `worker_env` | credential references only |
| Invalid codes | `protocol.py:33` `INVALID_CODES = ("schema","stale","kind","range","incomplete")` | enumerated for §2 taxonomy |
| Outcome inference | `recording.py:329` `infer_outcome` → `death|starvation|ascension|unknown` | observational only |

**Jev noul/score wire schema and billing — verified against primary TypeSafe
documentation** (`docs.typesafe.ai/api.md`, `docs.typesafe.ai/models.md`,
`docs.typesafe.ai/patterns/fan-out.md`, and the Cloudflare-hosted model page
`developers.cloudflare.com/ai/models/typesafe/jev`):

- **Endpoint** `POST https://api.typesafe.ai/v1/systemone`, `Authorization:
  Bearer`, body `{state, model, questions}` — matches `JEV_OFFICIAL_BASE_URL`.
- **Question types.** `noul` → answer `{type, noul}` where `noul` ∈ [0,1] (no
  probabilities/confidence). `score` → answer `{type, score, legend,
  probabilities, confidence}` where `score` is a **probability-weighted value
  across the ordered `criteria` levels, and may land between levels**; its
  scale is the **level-index scale `0 .. len(criteria)-1`** (2–10 levels), so
  local 0–1 normalization is `score / (n_levels - 1)`. `choice` → `{type,
  choice, probabilities, confidence}`. `noul`/`score` both accept optional
  `criteria` (`noul`: `{true,false}`; `score`: ordered **array**).
- **Response** `{model, answers:{<id>: Answer}, usage:{input_tokens,
  output_tokens}}`; one answer per question keyed by the same id.
- **Bundling.** Multi-question **bundled requests are supported and are the
  documented fan-out pattern** ("send many questions in a single call"): the
  Cloudflare Usage example carries `noul`+`choice`+`score` in one `questions`
  map and returns all three answers. **Billing is per input token** — "Jev
  ingests the `state` once and evaluates every question against it in
  parallel"; the 64k budget covers `state` + all questions. One bundled request
  is therefore billed as one call's `input_tokens` (cheaper than three, which
  would each re-ingest the state).
- **Tariff.** `$42/Btok = $0.042/Mtok`, **charged per input token; output
  tokens are free** — this **confirms** the repository snapshot
  (`budget.py:158–159`). The snapshot is now verified billing contract, not
  mere repository evidence.

**Go/no-go:** **GO — bundled branch (one request per judge evaluation).** The
three judge questions (`degenerate_loop` noul, `exploration_productivity`
score, `termination_sanity` noul) are sent in **one** `/systemone` request;
`judge_calls_total` and per-episode eligibility count **one** dispatch; a
response missing any of the three answers ids is **invalid** (a partial answer
is never partially credited). The three-single-question accounting primitive is
**also implemented and unit-tested** (`request_shape="single"`) so the branch
is available if the contract regresses, but the **bundled branch's tests are
the production-gating ones**.

**Corrected cap/cost semantics (documented for the bench):**

- `reflex_call_cap` bounds **applied** Jev decisions (complete sends of
  unoverridden, locally valid proposals) — `budget.py:653–681`. It is **not** a
  paid-call ceiling: rejected/skipped/abstained consultations still cost money.
- **A token cap or a USD cap disables Jev dispatch outright**
  (`budget.py:698–699`): the service-side token accounting is unknown, so no
  strict spend bound exists under those caps. The bench never multiplies the
  applied cap by a presumed per-call cost to claim a spend bound (AC5).
- `estimated_usd` can be nonzero when DeepSeek prices are absent because it may
  include **priced Jev** usage; unknown price and unknown exposure are tracked
  separately and never merged into one asserted figure.
- `postmortem_reserve` defaults to 1 and runs DeepSeek postmortems; **bench
  forces it to 0** (AC6).

**AC10 vapor-cloud prerequisite:** the caller's attestation is **not yet
supplied**; `live_claims.measured` is `false` and the attestation is recorded
as **pending-operator**. No live campaign is run in this implementation; the
gate is enforced by `test_live_testing_gated_on_vapor_cloud_attestation`.

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
7. **Jev transport reusable, reflex semantics not reusable.** `tools/agent/providers.py:1468–1470` constructs `/systemone`; `:1690–1704` sends state/model/questions with a choice question. `:1608–1615` requires accepted terms and key; `:1744–1789` loads the secret and supervises a bounded worker. `tools/agent/worker.py:91–134,147–164` accepts a generic payload and returns JSON, without choice-specific parsing. It enforces HTTPS/loopback-test restrictions and same-origin redirects, and avoids leaking credentials. The current adapter proves choice support only; exact noul/score wire schemas and multi-question response semantics were not verified locally. They must be checked against current primary API documentation before judge implementation; do not guess the schema.
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
- `comparison`: `metric_policy_version`, `target_metric`, `min_improvement`, `noninferiority_margins`, `min_samples`, `resampling_seed`, `resamples`, `confidence_level`, `screening_episodes_per_arm`, `confirmation_episodes_per_arm` (exact integers; confirmation strictly greater than screening), `min_completed_episodes_per_arm`, `min_aggregate_at_risk_ticks_per_arm`, `deadline_classification` (`horizon-completion|adverse-early`), `invalid_policy_version`, `invalid_policy_approval_hash`.
- `tuning`: `mode` (`suggest|apply-approved`), `parameters`, `approval_id`, `approval_expiry`, `expected_base_config_hash`.

Before any run, the operator-approved spec must contain **exact** `screening_episodes_per_arm` and `confirmation_episodes_per_arm` values — no "at least"/"proposed minimum" phrasing — with `confirmation_episodes_per_arm` strictly greater than the screening tier; the metric/exclusion and invalid-allowlist policies are pinned by `*_policy_version` plus their recorded approval hash.

Resolve the operator config plus allowlisted overrides to a validated `ProviderConfig`. Config contains credential *references*, never raw keys. Do not copy referenced secret files into output. Reject unknown knobs, nonfinite values, impossible caps, postmortem reserve !=0, and scope-incompatible live-Jev budgeting before network work.

Every run records a redacted resolved config, config/code/worker/data/sysconf hashes, metric and provider presentation versions, fixed evaluation horizon, role, actual tier coverage, seed capability (`uncontrolled` initially), source checksums and comparison policy. Store outputs under unique run/variant/episode directories. Private permissions; atomic manifest/report updates; immutable completed episode artifacts. No overwriting an old run on resume.

**Canonical provenance manifest.** Define one manifest that is the single comparability authority; the same manifest shape is written for baseline and candidate and is hashed into the comparison record. It contains:

- **VCS:** commit id, dirty status, and a dirty-diff hash when the tree is dirty.
- **Imported code:** content hashes of every imported bench/agent Python module **except the advisory-judge modules** (`bench_judge.py`, the judge rubric/model, and the judge response schema, which are hashed in the advisory domain instead) — the modules actually loaded, resolved from `sys.modules`, not a directory walk.
- **Binaries:** hashes of the worker and launcher binaries.
- **Data/sysconf:** hashes of the data and sysconf trees, or their versioned manifests where a tree hash is impractical.
- **Versions:** schema, rubric, and policy versions (scorecard schema, judge rubric, comparison/metric policy versions).

**Provenance domains (split).** Comparability is decided by **domain-specific content hashes and versions — not by commit-id inequality**. Every changed file/module is partitioned into one of three domains:

- **Deterministic domain** — controller/provider gameplay code, the bench runner/metrics/comparison code, the worker/launcher binaries, the data/sysconf trees, and the **metric/exclusion policy**. A difference in **this domain's content hash or version** is **hard not-comparable**. Only deterministic-domain differences have that effect.
- **Advisory judge domain** — `bench_judge.py`, the judge rubric, the judge model, and the judge response schema, all used only **after scorecard sealing**. A difference here must **not** invalidate the deterministic comparison or gate apply: it only invalidates the **judge cache/results** (forcing rejudgment) or makes **judge outputs mutually incomparable**. A rubric-only change therefore leaves the deterministic `pass|fail|inconclusive|not-comparable` verdict unchanged while requiring a fresh judge call — the cached judge result, keyed on the rubric hash, is no longer reusable.
- **Reported-only** — non-behavioral metadata (wall-clock timestamps, output directory paths, run labels) never forces not-comparable.

**Commit ids and dirty hashes are recorded for audit only**, never used as the comparability decision. Consequently two runs with **different commit ids** but **identical deterministic-domain content hashes** remain deterministically comparable, while a change confined to the advisory judge domain forces rejudgment without changing the deterministic verdict. The `sys.modules` content hashes **exclude the advisory-judge modules** (`bench_judge.py`, rubric/model, judge schema); those are hashed in the advisory domain. The manifest records all three domains so a comparison can state precisely which hash drove its verdict, and so judge provenance can never masquerade as a deterministic blocker.

A bench manifest lists scheduled/completed/interrupted episodes, episode artifact paths/checksums, budget reservations/settlements and run status. Preserve each existing campaign JSON. Proposed generated files: `manifest.json`, `scorecards.json`, `comparison.json`, `judge.jsonl`, `postmortem/manifest.json`, `tuning-report.json`, `report.md`.

### 2. Scorecard contract

`episode-scorecard/2` has exactly these sections (the implementation bumped the
schema from `/1` to `/2`, adding the bench-owned `terminal_class`, `invalids`
and `gates` sections — see `doc/agent-campaign-bench.md` for the documented
extras, enforced exactly by `validate_scorecard_shape()`):
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
  - **Hard failures and the allowlist.** The default gameplay-invalid hard-failure **allowlist is EMPTY** — every native gameplay invalid code is a hard failure unless excepted by a **versioned operator-approved metric/safety policy** whose **approval hash is recorded in the spec**. `incomplete` is governed separately by the delivery-integrity policy above, not by this allowlist. "Reaches the engine" means the **rejected original proposal** was actually sent to the engine and rejected; it does **not** include a safe fallback action the agent sent instead (that fallback is a different, separately-accounted action), and a local validation fallback intercepted before send never "reaches the engine." Prompt, inventory, and other non-gameplay frames are neither. No hard failure is offsettable by coverage or a judge score.
- **Termination-safety admission contract (required before any apply).** Because entered cells can be gamed by reckless play, predeclare a **deterministic-only** termination-safety admission evaluated on every comparison and required before any `apply-approved`. Every episode is first classified by the **terminal-classification table**, which is the only authority for adverse-termination rates:

  | Precedence | Field | Exact value | Class |
  |---|---|---|---|
  | 1 | `stop_reason` | `policy-exhausted` | adverse-early |
  | 2 | `stop_reason` | `tick-cap-graceful-quit` | horizon-completion (non-adverse) |
  | 3 | `stop_reason` | `content-deadline` | horizon/completion **or** adverse-early — per predeclared policy |
  | 4 | `stop_reason` | `episode-timeout` | horizon/completion **or** adverse-early — per predeclared policy |
  | 5 | `stop_reason` | `protocol-failure` | operational/integrity failure |
  | 6 | `stop_reason` | `transport-failure-write` | operational/integrity failure |
  | 7 | `stop_reason` | `transport-failure-eof` | operational/integrity failure |
  | 8 | `stop_reason` | `spawn-failure` | operational/integrity failure |
  | 9 | `stop_reason` | `recorder-failure` | operational/integrity failure |
  | 10 | `stop_reason` | `closed-unanswered` | operational/integrity failure |
  | 11 | `stop_reason` | `bench-stopped-graceful` (bench-owned) | excluded-from-comparison |
  | 12 | `stop_reason` | `bench-aborted` (bench-owned) | excluded-from-comparison |
  | 13 | `stop_reason` | `closed` (generic — the value a real death almost always carries) | fall through to `outcome` (rows 14–17) |
  | 14 | `outcome` (generic `closed` only) | `death` | adverse-early (observed, conservative) |
  | 15 | `outcome` (generic `closed` only) | `starvation` | adverse-early (observed, conservative) |
  | 16 | `outcome` (generic `closed` only) | `ascension` | explicit non-adverse category `ascension` |
  | 17 | `outcome` (generic `closed` only) | any other / unknown | adverse/unknown (counts against admission) |
  | 18 | `stop_reason` | any other unrecognized value (future-proof fallback) | operational/integrity failure **or** adverse/unknown — **never benign** |

  The classifier is defined over the **complete exact administrative vocabulary**, not over labels the controller never emits. `controller.py:1058–1070,1089–1108,290–293` shows the controller's administrative `stop_reason` values are exactly `closed`, `tick-cap-graceful-quit`, `policy-exhausted`, `content-deadline`, `episode-timeout`, `protocol-failure`, `transport-failure-write`, `transport-failure-eof`, `spawn-failure`, `recorder-failure`, and `closed-unanswered` — it **never** emits `death` or `starvation` as a `stop_reason`. Death and starvation exist only as `recording.infer_outcome()` values (`recording.py:329–341`), and a real death is typically `stop_reason="closed", outcome="death"`. So rows 1–12 key on the trusted `stop_reason`; only a generic `closed` (row 13) consults the visible `outcome` (rows 14–17); and any value not enumerated falls to the never-benign fallback (row 18). **Deadline policy:** `content-deadline` and `episode-timeout` are classified by a predeclared policy (the spec's `deadline_classification`) — **horizon/completion** when the deadline coincides with the fixed evaluation horizon (a legitimately bounded run) and **adverse-early** otherwise; the policy is chosen before results and never changed after them. **Bench-owned reasons:** the bench wraps each episode and adds two canonical persisted reasons — `bench-stopped-graceful` (graceful operator stop: first SIGINT/SIGTERM or stop-file) and `bench-aborted` (forced abort/interruption: second interrupt / total deadline, after forced teardown) — both **excluded-from-comparison** (not safety-rate events and not horizon completion), recorded verbatim so a stop is never mistaken for a game outcome. Visible-text `outcome` is **observational, not proof** — a heuristic inference (`recording.py:329–341`) — but it **is** usable for the **conservative safety rate**: `death`/`starvation` outcomes count as adverse observed outcomes, and any unrecognized `outcome` is adverse/unknown. `ascension` gets its own explicit non-adverse category rather than being folded into unknown or horizon-completion.
  - **Noninferiority limits** on the adverse/unknown rates — `policy-exhausted`, any `content-deadline`/`episode-timeout` the predeclared policy classes adverse-early, generic-`closed` `death`/`starvation` outcomes, and the unknown class — at operator-approved absolute noninferiority margins. The bench-owned stops `bench-stopped-graceful`/`bench-aborted` (excluded-from-comparison), operational/integrity failures (rows 5–10), and explicit `ascension` are **not** safety-rate events; the row-18 unrecognized fallback counts as adverse/unknown (never benign).
  - **Minimum common exposure (numeric, exact validation).** The spec carries both `min_completed_episodes_per_arm` (integer ≥ 0) and `min_aggregate_at_risk_ticks_per_arm` (integer ≥ 0); `0` disables that particular minimum, and a spec whose **both** minima are `0` is **rejected** (there must be a predeclared floor). An arm is **under-exposed** if it falls below **any active (non-zero) minimum** — the conjunction of the active minima — and the active value of its field is the binding minimum. **All** early adverse terminals are counted in the rate denominators (never discard the adverse event). Under-exposure yields **inconclusive** (insufficient aggregate exposure) — **not** not-comparable.
  - **Unknown outcomes:** an unresolvable `stop_reason` or an unrecognized generic-`closed` `outcome` counts as **adverse/unknown evidence** (inconclusive at best), never a pass or a benign terminal; a visible-text `outcome` alone never rescues a class the trusted `stop_reason` already resolves.
  - Confirmation runs must pass the contract before apply; a candidate that fails it is rejected regardless of coverage gain.
- Primary target: mean entered instance-scoped cells per episode at the same fixed horizon. Require improvement and protect the metrics below. No wall-clock objective (provider latency varies).
- Behavioral guardrails: entered-cell productivity, depth_max, time_advances (noninferiority); longest_loop_span and stationary_span_max (no material increase). Operator-configured absolute thresholds can detect gross failures in smoke runs. Display full distributions.
- Initially observations only: commitment lengths, switch rate, directive rates, reach/pickup outcomes, discovered cells, provider acceptance/fallback/cache rates, termination/game outcome. Terminal completeness is not blindly required to be 1 for horizon-truncated episodes with an active commitment.

### 3. Comparison and variance

One comparison engine for A/B and candidate-versus-baseline. Report `pass|fail|inconclusive|not-comparable`, per-metric direction, sample counts, missing counts, baseline/candidate summaries, effect, confidence interval, margin, gate decision and reason, plus config diff and provenance differences. Provenance differences reported here are the **deterministic-domain** differences of §1; advisory judge provenance never changes the deterministic verdict.

No matched-seed comparisons initially. Interleave baseline and candidate episodes in a **fixed seeded balanced schedule that randomizes/counterbalances pair order** (both AB and BA occur; never always baseline-first), so ordering effects cannot align with the candidate; retain every episode including failures. Same engine/data/role/budgets/horizon/provider versions required except the declared change. Engine or horizon changes require a new baseline. Attempts-source differences make comparisons not-comparable, not merely noisier (see the attempts_source admission policy below).

**Precommitted sample and no post-result change (universal comparison-engine invariant, applied to A/B, candidate-vs-baseline, and tuning — not a tuning-only rule).** Each run must **precommit an exact sample count** and complete **all** scheduled episodes regardless of interim results; there is no early stop on a favorable (or unfavorable) interim look. Post-result changes to the metric set, margins, candidate set, sample count, or resampling policy are forbidden; if one must change, abandon the run and re-precut it with a new sample — never edit it in place. **4 episodes/arm is unconditionally diagnostic-only** — never acceptance and never apply, at that power, with no exception. Acceptance/apply uses the spec's exact `confirmation_episodes_per_arm` (strictly greater than `screening_episodes_per_arm`); a low-power acceptance profile is only possible as a **separate, explicitly named policy version**, never by promoting a 4/arm screening run.

**attempts_source admission policy.** Admission and automatic apply require `attempts_source == "actions"` (real attempt accounting from the actions sidecar). A matching displacement-fallback source is allowed **only for diagnostic comparisons**; a missing or corrupt actions sidecar makes attempts-based productivity **unavailable required evidence**, so the productivity gate cannot pass (it is not a measured zero).

**Termination-safety admission at confirmation.** Every comparison — and confirmation before apply — evaluates the §2 termination-safety admission contract over its exact values. A candidate that improves coverage but raises the adverse/unknown rate (`policy-exhausted`, generic-`closed` `death`/`starvation` outcomes, unknown) beyond the approved noninferiority margin, or whose arm is under-exposed relative to the predeclared active minimum, fails or is inconclusive regardless of coverage gain.

Smoke (2 episodes per variant): hard invariants and gross absolute bounds only; behavior deltas diagnostic; never authorizes auto-apply.

Full screening (the spec's exact `screening_episodes_per_arm`; 4 is the diagnostic screen and never accepts): reproducible unpaired bootstrap intervals over episode-level metrics, fixed seed/resample count, simultaneous guardrail coverage (e.g. Bonferroni). Pass only if each higher-is-better guardrail's lower difference bound is >= negative approved margin, each lower-is-better guardrail's upper bound is <= approved margin, and the target's lower improvement bound exceeds `min_improvement`. Missing evidence or broad intervals => inconclusive. Zero tolerated regression on hard safety gates; behavioral margins are explicit practical tolerances, not changed after results.

For tuning: screen a small fixed candidate set, then test the selected winner on fresh confirmation episodes sized to the spec's exact `confirmation_episodes_per_arm` (strictly greater than `screening_episodes_per_arm`; no "proposed minimum"). Selection and confirmation budgets reserved before searching. No repeated peeking until success; inconclusive at maximum budget means suggest-only. Provider nondeterminism remains an explicit limitation.

### 4. Jev per-episode judge

A thin bench-side client reuses `load_secret`, `jev_endpoint`, and the bounded worker transport/supervisor. Do not instantiate `JevReflex` or route judge answers through controller arbitration. Judge calls run after scorecard sealing, outside episode gameplay, charged to a separate bench ledger.

**One judge evaluation per eligible episode**, implemented as **either one bundled request or three single-question requests according to preflight** (below); eligibility is reserved against the **selected request count** (1 or 3) before dispatch:
- `degenerate_loop` (noul): repetitive nonproductive behavior rather than reasonable local recovery?
- `exploration_productivity` (score): rubric-anchored assessment from coverage, attempts, depth/time progress and lifecycle consistency; normalize 0–1 locally only after validating the actual API score scale.
- `termination_sanity` (noul): recorded ending consistent with stated limits/outcome/evidence? Insufficient terminal evidence is unavailable, not "sane."

**Question-bundling go/no-go (preflight).** The current adapter constructs exactly **one** choice question (`providers.py:1690–1707`) and `_choice_from` parses exactly one answer; a bundled noul/score response in one request is **unproven**. Make preflight resolution an explicit go/no-go:
- if **primary API documentation plus a contract fixture prove** that a single request carries all three bundled questions, retain **one request** and update `judge_calls_total`, per-episode eligibility, atomicity/partial-answer semantics, caching, and cost accounting accordingly;
- otherwise issue **three independently budgeted single-question calls** (each counted in `judge_calls_total` and per-episode eligibility, each charged separately), or **disable** unsupported question types.

Regardless of branch, define atomicity/partial-answer semantics (a partial response is invalid, never partially credited), caching, and cost accounting for the chosen shape, and update the judge tests. **Do not import the choice parser for judge responses**; use a judge-specific typed parser. The branches contribute different gating tests — **only the selected branch's tests are production-gating**, while both accounting primitives stay unit-tested: bundled branch → `test_bundled_judge_counts_one_dispatch_and_rejects_partial_answers`; single branch → `test_single_question_judge_counts_three_dispatches_and_handles_partial_failure`. Both branches additionally require `test_judge_cache_key_and_hit_do_not_dispatch` and `test_rejudge_is_new_recorded_budgeted_call`.

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

Coordinate search over a finite operator-approved grid, deterministic order, one parameter at a time, at most two sweeps, small explicit candidate budget (suggested maximum six screening candidates). Hold horizon, metric policy, role, presentation, safety settings and candidate budget fixed. Reserve confirmation capacity sized to the spec's exact `confirmation_episodes_per_arm`; screening capacity is the spec's exact `screening_episodes_per_arm`.

Initial eligible knobs: `reflex_call_cap`, `strategy_call_cap`, `boundary_cooldown_ticks`, `boundary_cooldown_wall`. Example rails, NOT automatic approval: reflex applied cap {4,8,12}, strategy cap {2,4,6}, tick cooldown {25,50,100}, wall cooldown {2.5,5,10}s. Zero cap is a deliberate tier-off experiment. Strategy cap remains steering-call control with reserve fixed at zero.

`jev_relative_factor` is safety-policy-adjacent: freeze at the operator's existing value by default. If separately approved, a suggested grid {1.25,1.5,1.75} satisfies the validator, but proposals remain report-only unless the approval specifically authorizes this confidence-policy change. Confidence mode/threshold, eligibility/safety checks, emergency cooldown, stall/door constants and forced-search budgets are not automatic tuning knobs.

Default `suggest` emits ranked candidates, all gate results, rejected/inconclusive evidence and exact JSON config diff. `apply-approved` writes a versioned nonsecret config overlay only after fresh confirmation passes, the **§2 termination-safety admission contract** passes, approval is valid, every changed key/value is in its exact authorized grid/range, baseline config hash matches, and no safety-semantic key is implicitly included. Atomically advance an operator-designated active-config reference; retain the previous reference for rollback. Never rewrite secret config files or patch a running agent. Trace approval id, hashes, before/after diff, evidence/run ids and rollback target.

### 7. Cost, safety and stop controls

Per-episode caps reset today; bench must reserve the whole next episode's approved resource allocation before launching it. No parallel live jobs initially. Sum caps across episodes/candidates/judges. Strategy exposure bound includes conservative prompt bound plus `deepseek_max_tokens`, cache misses at the conservative price, and timed-out dispatched calls. Do not equate output max_tokens with total tokens.

Supported first profiles:
1. Offline dry-run: ReplayPass, no network, zero judge calls, scripted fallback explicitly labeled.
2. Strict-budget scripted reflex + optional DeepSeek steering: supplied complete DeepSeek tariff/USD cap or strict strategy-call and bounded payload/output configuration, explicit total episode budget. Jev judging separately bounded as above.
3. Live Jev reflex: **blocked for strict-spend automatic tuning until a hard paid-call/spend bound is verified.** Current token/USD caps disable Jev, and applied cap cannot supply that bound. Prefer an existing verified provider-account hard allowance if available. Without it, only an explicitly operator-approved, wall/tick-bounded unknown-exposure diagnostic run is honest; do not call it strict-budget or allow unattended auto-apply. Do not quietly add an intercepting proxy; a paid-dispatch limiter inside controller/provider admission would solve this but violates the no-live-behavior-change scope and requires separate approval.

Implement bench stop-after-current-episode via first SIGINT/SIGTERM or stop-file: persist the requested stop as the bench-owned `stop_reason=bench-stopped-graceful` (graceful first stop) or `bench-aborted` (forced abort) — §2 rows 11–12, both excluded-from-comparison — admit no new episodes/judges, and let the bounded current episode finish. Second interrupt or total deadline triggers bounded teardown of the episode supervisor and its worker/launcher groups, then records partial status and retains artifacts. Explicitly test nested process groups (the controller starts a new session; killing only its parent PID is insufficient). On startup/resume, interrupted reservations remain unknown exposure, not refunded automatically. Resume is opt-in and validates hashes/locks; never replay a possibly billed judge request silently.

**Forced-abort containment mechanism (decided).**

- **First (graceful) path (primary):** the bench episode child installs explicit **SIGINT/SIGTERM handlers** that request cancellation through the Python controller so `run_episode()` reaches its `finally`, where the controller-owned `_reap` tears down the launcher session (`controller.py:347–407`); the bench waits a **bounded** interval for that cleanup to complete.
- **Launch ownership capture.** The bench launches the episode **root in a dedicated session and process group** (`setsid` / `start_new_session`) and captures its **PID, start time, session id, and PGID** at launch, plus the same identities for the controller child and launcher session leader as they appear. These captured identities define the **owned episode tree**.
- **Second/forced path — ownership-isolated process-tree walk (no controller change, no systemd dependency).** If the bounded wait elapses or a descendant ignores TERM, reap **only the owned tree**:
  - **Never `killpg` the supervisor's / current process group.** Signal a process group only when its **validated membership belongs to the captured episode tree** (re-check each member's start time / session / PGID against the captured identities before signalling).
  - **Ordering:** keep the **root alive** while repeatedly discovering and killing descendants and their groups — walk `/proc` by **PPID recursion** from the root's PID and **re-walk** until the tree is stable or the bound elapses, so children spawned during the walk are caught. Kill/reap the **root LAST**, then **verify no captured identity survives**.
  - **Failure is fatal, not cosmetic.** A **permission or identity-validation failure** — a `/proc` read failure, a `getpgid`/identity mismatch, a start-time mismatch, or a signal permission failure — sets **teardown-failure / partial** status, marks the run non-success, blocks baseline promotion, and **FAILS AC7**, even when cleanup continues best-effort. (This replaces the earlier "logged, never fatal" wording; `test_forced_abort_permission_or_identity_failure_sets_teardown_failure` injects each such failure and asserts it.)
  - Documented residual limits remain: an inherent **race window** and **PID reuse** (validate identity/start time before signalling a reused PID). The controller's `start_new_session` launcher is a *different session* but still a `pid`-tree descendant, so PPID recursion from the owned root reaches it.
- **Test:** `test_forced_abort_reaps_nested_launcher_and_provider_groups` drives bench child → controller launcher session → a **TERM-ignoring descendant** and asserts **every PID is gone**; it additionally includes an **inherited-PGID sentinel** proving the supervisor survives the walk (a child that inherits the supervisor's PGID must not be signalled), and a **child-spawn-during-rewalk** case proving the re-walk catches a descendant spawned after the walk begins.

## Implementation Plan

1. **Pin and preflight:** freeze a revision, wait for the caller's vapor-cloud fix before any live tests, validate current interfaces, document corrected cap semantics, verify Jev noul/score API and billing contract. No engine edits.
2. **Artifact-only core:** spec parsing, manifest, scoring/integrity, comparison engine, fixture reports. Import existing artifacts and generate postmortem packages without credentials/network.
3. **Runner and operational controls:** dry-run integration, one-episode campaign wrapper, atomic checkpoints, caps/reservations and stop behavior (including the §7 forced-abort containment mechanism — child SIGINT/SIGTERM → controller `_reap`, plus the **ownership-isolated** bench-owned `/proc` PPID-recursion walk that signals only the captured episode tree and never the supervisor's group). Prove fake-child process cleanup and actual-tier detection. Gate strict live-Jev support as described rather than weaken preflight.
4. **Judge:** implement verified typed schema over existing worker transport, fake endpoint tests, separate usage accounting; optional approved single live contract probe only after the vapor-cloud prerequisite and budget permission.
5. **Tuner:** report-only finite search first; confirmation and approval-bound config overlays afterward. Never start with autonomous live applies.
6. **Regression workflow/docs:** wire correctness suite + mutation harness + offline bench into per-change work; a diagnostic campaign of the spec's exact `screening_episodes_per_arm` episodes per arm, followed by the larger campaign of the spec's exact `confirmation_episodes_per_arm` episodes per arm, follow separately. Store a versioned baseline and calibrate practical margins before permitting automatic apply.

## Acceptance Criteria

AC1. Existing artifact scoring is byte-stable, versioned and faithful; missing/legacy/corrupt/zero-denominator data never becomes measured zero.
AC2. Dry-run makes no provider calls and reports that Jev behavior was not evaluated.
AC3. Every live run records requested versus actually observed tiers; a fallback-only Jev campaign cannot pass live-Jev validation.
AC4. Hard gate failures cannot be offset by coverage or judge scores; incomplete/mismatched required evidence yields inconclusive/not-comparable.
AC5. All paid activity is reserved/accounted or explicitly marked unknown exposure; unsupported strict-spend configurations fail before dispatch. Applied reflex caps are never presented as paid-call ceilings.
AC6. Bench campaigns make zero DeepSeek postmortem calls; Jev judge is per-episode, typed, bounded, advisory and separately metered. Cached results key on scorecard+rubric+model hashes, and any rejudge is a new recorded call (never a silent recompute); `judge_calls_total` counts dispatched calls with cache hits reported separately, so cache/rejudge accounting is distinguishable.
AC7. Stops admit no further work, reap all children within the documented escalation bound, preserve partial artifacts and never label interruption successful; the forced path signals **only the owned episode tree** (never the supervisor's/current process group), and any **permission or identity-validation failure** sets teardown-failure/partial status and **fails** this criterion.
AC8. Search is finite, repeatable over frozen input reports, obeys episode/wall/candidate budgets and requires fresh confirmation before apply.
AC9. Apply changes only authorized keys/ranges against the approved config hash; every apply has exact diff, evidence and rollback pointer. Default remains report-only.
AC10. Live testing does not begin before the vapor-cloud prerequisite lands; its status is supplied by the caller, and the gate is enforced by an executable preflight test (`test_live_testing_gated_on_vapor_cloud_attestation`), not by convention.

AC11. The termination-safety admission contract (§2) is predeclared, deterministic and blocks any apply: episodes are classified by the **terminal-classification table over the complete exact administrative `stop_reason` vocabulary** — `policy-exhausted` → adverse-early; `tick-cap-graceful-quit` → horizon-completion; `content-deadline`/`episode-timeout` → horizon/completion or adverse-early per the predeclared `deadline_classification`; `protocol-failure`/`transport-failure-write`/`transport-failure-eof`/`spawn-failure`/`recorder-failure`/`closed-unanswered` → operational/integrity failure; the bench-owned `bench-stopped-graceful`/`bench-aborted` → excluded-from-comparison; any unrecognized future `stop_reason` → operational/integrity failure or adverse/unknown, **never benign** — and, only for a generic `stop_reason="closed"`, the visible-text `outcome` (`death`/`starvation` → adverse-early observed; `ascension` → explicit category; else adverse/unknown). A candidate that raises the adverse/unknown rate beyond the approved noninferiority margin fails; an arm below any predeclared active exposure minimum yields **inconclusive** (not not-comparable); neither passes on coverage gain.

AC12. A postmortem package is bounded, checksummed and untrusted-transcript-safe: it contains a manifest, bounded excerpts with line/event/tick ranges and omitted counts, checksums, and task text that is explicitly not instructions.

## Test Strategy

**Discovery.** The bench test files are `test/agent/test_auto_bench.py` and `test/agent/test_auto_bench_judge.py` (renamed to the existing `test_auto*.py` pattern so the discovery gate collects them). Exact repository gate command (no pytest — none is configured):

```
python3 -m unittest discover -s test/agent -p 'test_auto*.py'
```

This command is the bench validation gate.

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
- `test_forced_abort_permission_or_identity_failure_sets_teardown_failure`: injects a `/proc` read failure, a `getpgid`/identity mismatch, a start-time mismatch, and a signal permission failure into the `/proc` read/identity/signal abstraction; each asserts partial status, `teardown_failure` set, non-success, and **no baseline promotion**.
- `test_partial_run_never_promoted_to_baseline`
- `test_tuner_finite_grid_and_reserved_confirmation_budget`
- `test_apply_requires_approval_range_hash_and_fresh_confirmation`
- `test_confidence_factor_frozen_without_specific_policy_approval`
- `test_overlay_apply_rollback_preserves_secret_config`
- `test_termination_safety_admission_blocks_reckless_candidates`
- `test_no_early_stop_and_no_post_result_extension`
- `test_counterbalanced_pair_order_is_deterministic`
- `test_invalid_gate_categories_are_exact`: fixtures covering native invalid **schema / stale / kind / range** codes, a **resolved `incomplete`** (delivery repaired within the retry budget), an **unresolved `incomplete`** (never resolved within the retry budget), and a **local-validation fallback** intercepted before send.
- `test_scorecard_keys_are_exact_and_versioned`
- `test_dirty_python_change_makes_comparison_not_comparable`
- `test_attempts_source_fallback_both_arms_diagnostic_only`
- `test_attempts_source_fallback_one_arm_not_comparable`
- `test_postmortem_package_is_bounded_and_checksummed`
- `test_postmortem_task_text_is_not_instructions`
- `test_live_testing_gated_on_vapor_cloud_attestation`
- `test_death_rate_regression_blocks_admission`: production-shaped pairs — a candidate whose episodes terminate as `stop_reason="closed", outcome="death"` is rejected on the adverse-rate noninferiority limit even when coverage rises.
- `test_policy_exhausted_regression_blocks_admission`: production-shaped — a candidate carrying more early-adverse terminations via the real `stop_reason="policy-exhausted"` (not a `stop_reason="death"`, which the controller never emits) is rejected.
- `test_unknown_outcome_handled_by_precedence_table`: **one production-shaped `(stop_reason, outcome)` case per enumerated row** — `("policy-exhausted", *)` → adverse-early; `("tick-cap-graceful-quit", *)` → horizon-completion; `("content-deadline", *)`/`("episode-timeout", *)` → horizon/completion or adverse-early per `deadline_classification`; `("protocol-failure", *)`, `("transport-failure-write", *)`, `("transport-failure-eof", *)`, `("spawn-failure", *)`, `("recorder-failure", *)`, `("closed-unanswered", *)` → operational/integrity failure; `("bench-stopped-graceful", *)`/`("bench-aborted", *)` → excluded-from-comparison; `("closed", "death")`/`("closed", "starvation")` → adverse-early; `("closed", "ascension")` → explicit `ascension`; `("closed", "<unrecognized>")` → adverse/unknown; and an unenumerated `stop_reason` → operational/integrity failure or adverse/unknown (never benign).
- `test_insufficient_common_exposure_is_inconclusive_not_not_comparable`: an arm below an active predeclared minimum (`min_completed_episodes_per_arm` or `min_aggregate_at_risk_ticks_per_arm`) yields **inconclusive**, never not-comparable.
- `test_rubric_only_change_leaves_deterministic_comparison_unchanged_and_forces_rejudgment`: two manifests with **different commit ids** but identical **deterministic-domain** content hashes keep the same deterministic `pass|fail|inconclusive|not-comparable` verdict, while a differing **advisory** (rubric) hash forces rejudgment and leaves the deterministic verdict unchanged.
- `test_judge_cache_key_and_hit_do_not_dispatch`
- `test_rejudge_is_new_recorded_budgeted_call`
- `test_bundled_judge_counts_one_dispatch_and_rejects_partial_answers`
- `test_single_question_judge_counts_three_dispatches_and_handles_partial_failure`

### AC → named-test map

Each acceptance criterion's named tests; a mutation that regresses an AC must fail at least one of them.

| AC | Named tests |
|---|---|
| AC1 | `test_scorecard_roundtrip_byte_stable`, `test_legacy_lifecycle_unavailable_not_zero`, `test_torn_sidecar_marks_partial_despite_summarizer_output`, `test_operational_ok_is_not_game_victory`, `test_low_denominator_lifecycle_rates_remain_observations`, `test_scorecard_keys_are_exact_and_versioned`, `test_attempts_source_fallback_both_arms_diagnostic_only`, `test_rubric_only_change_leaves_deterministic_comparison_unchanged_and_forces_rejudgment` |
| AC2 | `test_dry_run_network_is_impossible` |
| AC3 | `test_live_jev_capped_fallback_not_reported_as_live_coverage` |
| AC4 | `test_hard_failure_cannot_be_offset_by_target_improvement`, `test_termination_safety_admission_blocks_reckless_candidates`, `test_invalid_gate_categories_are_exact`, `test_attempts_source_mismatch_not_comparable`, `test_attempts_source_fallback_one_arm_not_comparable`, `test_dirty_python_change_makes_comparison_not_comparable` |
| AC5 | `test_reflex_applied_cap_not_used_as_paid_call_bound`, `test_usd_without_tariff_rejected_before_spawn`, `test_jev_token_or_usd_cap_profile_rejected_before_spawn`, `test_episode_allocations_sum_across_candidates_and_confirmation`, `test_unknown_exposure_survives_timeout_and_resume` |
| AC6 | `test_bench_forces_zero_postmortem_reserve`, `test_judge_payload_contains_only_scorecard_allowlist`, `test_judge_typed_answers_missing_nan_extra_questions`, `test_judge_timeout_no_retry_and_no_secret_in_artifacts`, `test_judge_disagreement_does_not_change_objective_or_gate`, `test_judge_cache_key_and_hit_do_not_dispatch`, `test_rejudge_is_new_recorded_budgeted_call`, `test_bundled_judge_counts_one_dispatch_and_rejects_partial_answers`, `test_single_question_judge_counts_three_dispatches_and_handles_partial_failure`, `test_rubric_only_change_leaves_deterministic_comparison_unchanged_and_forces_rejudgment` |
| AC7 | `test_stop_after_episode_prevents_next_episode_and_judge`, `test_forced_abort_reaps_nested_launcher_and_provider_groups`, `test_forced_abort_permission_or_identity_failure_sets_teardown_failure`, `test_partial_run_never_promoted_to_baseline`, `test_unknown_exposure_survives_timeout_and_resume` |
| AC8 | `test_comparison_fixed_resampling_and_inconclusive_small_sample`, `test_tuner_finite_grid_and_reserved_confirmation_budget`, `test_no_early_stop_and_no_post_result_extension`, `test_counterbalanced_pair_order_is_deterministic` |
| AC9 | `test_apply_requires_approval_range_hash_and_fresh_confirmation`, `test_confidence_factor_frozen_without_specific_policy_approval`, `test_overlay_apply_rollback_preserves_secret_config` |
| AC10 | `test_live_testing_gated_on_vapor_cloud_attestation` |
| AC11 | `test_termination_safety_admission_blocks_reckless_candidates` (the named regression in which a candidate enters more cells but dies / exhausts policy more often and must fail or be inconclusive), `test_death_rate_regression_blocks_admission`, `test_policy_exhausted_regression_blocks_admission`, `test_unknown_outcome_handled_by_precedence_table`, `test_insufficient_common_exposure_is_inconclusive_not_not_comparable` |
| AC12 | `test_postmortem_package_is_bounded_and_checksummed`, `test_postmortem_task_text_is_not_instructions` |

Reuse existing mutation_checks artifact reporting; add fixture assertions tying the bench lifecycle section to the same summarizer output. Fake endpoints and fake children cover operational behavior without spend. Any later live evidence must state exact config, revision, episode counts, actual tiers and usage completeness.

### Mutation checks

Each mutation killed by its named test: scorecard silently coerces null→zero (AC1); dry-run performs a provider call (AC2); fallback-only live run passes tier validation (AC3); hard-gate failure offset by target improvement (AC4); postmortem reserve not forced to zero (AC6); stop admits the next episode (AC7); tuner peeks until success / exceeds candidate budget (AC8); apply without approval/hash/range (AC9); confidence factor auto-tuned without specific approval (AC9/D3); interrupted run promoted to baseline (AC7); termination-safety admission ignored so a coverage-gaining reckless candidate passes (AC11); early stop on a favorable interim look or a post-result margin/sample change (AC8); a coverage-gaining candidate accepted despite a higher death / policy-exhausted / unknown rate (AC11); a judge rubric-only change invalidating the deterministic comparison or blocking apply (AC6); a forced-abort path that logs and ignores a permission/identity failure (the prior defect) → `test_forced_abort_permission_or_identity_failure_sets_teardown_failure` (AC7).

## Review Strategy

Review artifact contracts and statistical policy first, then budget/credentials/interrupt handling, then tuner apply logic. After implementation: the execute agent dispatches an independent reviewer against the actual diff and AC1–AC12 plus inherited contracts; the execute agent fixes or explicitly rebuts every finding, reruns affected tests, and redispatches after Critical/Important fixes until none remain or an operator decision is required. Require independent review of the two highest-risk claims: enforcement of paid exposure and interpretation of nonseeded results.

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
