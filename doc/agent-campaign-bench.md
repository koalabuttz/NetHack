# Campaign bench — schema, operation and acceptance policy

The campaign bench is **agent-side Python tooling that observes and
configures**. It runs bounded campaigns around the existing controller, derives
reproducible scorecards, compares runs, requests a cheap per-episode Jev second
opinion, packages regressions for coding-agent analysis, and *suggests* (or,
only with explicit approval, narrowly applies) parameter changes. It never
patches the engine, the controller or a running agent.

Design authority: `doc/agent-campaign-bench-plan.md` (Revision 5, with the
Phase 1 preflight record). This document is the operational guide.

Modules:

| Path | Responsibility |
|---|---|
| `tools/agent/bench.py` | `bench-spec/1` validation, CLI, one-episode runner, stop policy, budget planning, ownership-isolated forced abort, suggest-first tuner |
| `tools/agent/bench_metrics.py` | `episode-scorecard/2`, provenance manifest, comparison engine, termination-safety admission, postmortem packages |
| `tools/agent/bench_judge.py` | typed advisory Jev judge over the public worker transport |
| `test/agent/test_auto_bench.py` | offline bench tests (metrics, comparison, runner, tuner) |
| `test/agent/test_auto_bench_judge.py` | offline judge tests |

## Commands

```sh
python3 -m tools.agent.bench validate SPEC.json      # spec + preflight report
python3 -m tools.agent.bench run      SPEC.json --out-dir RUN_DIR
python3 -m tools.agent.bench score    SPEC.json --out-dir RUN_DIR
python3 -m tools.agent.bench compare  --run-dir RUN_DIR [--baseline-dir BASE_DIR]
python3 -m tools.agent.bench tune     SPEC.json --run-dir RUN_DIR
python3 -m tools.agent.bench package  --run-dir RUN_DIR [--max-excerpts N] \
                                      [--package-max-bytes N]
```

`run` writes the whole workflow's artifacts in one pass: `precommit.json`
before any result, a sealed scorecard per episode (with source checksums), a
`manifest.json` entry per episode, the advisory judge calls, then
`comparison.json`, `postmortem/manifest.json` and `tuning-report.json`.
`compare`, `tune` and `package` re-run their stages over an existing run
directory and **write their artifacts** (they never return a stub failure):
`comparison.json`, `tuning-report.json`, and `postmortem/manifest.json`.

`run --tier dry-run` performs **offline evaluation only**: it makes no provider
call and reports `judge_behavior: "not-evaluated"`. Enabling the paid judge is
allowed only for an explicit live tier, and a live tier additionally requires
the caller's vapor-cloud attestation (see **AC10** below) — the value
`BENCH_VAPOR_CLOUD_ATTESTED` must equal the exact token
`vapor-cloud-fix-landed-and-tested` (any other non-empty value is refused).

### Episodes and artifacts

Each episode runs in its own isolated directory via a dedicated
`run_campaign(1)` child, so the controller always writes `ep-1.*` **inside the
episode directory**; the parent then **remaps** those local artifacts to
`ep-<global index>.*`, so episodes 2+ are not seen as missing. The child
serializes the **complete** `EpisodeResult` evidence — every field *including
all forced-search counters* — into `bench-child.json` and additively into the
episode meta (the controller's own meta omits them), so a scorecard carries
*measured* forced-search values (a genuine zero included) rather than an
unavailable gap.

Scheduling metadata (`arm`, `pair`, `order`, `config_hash`) lives in the
`manifest.json` entries and the `scorecards.json` **envelope** — never inside
the immutable `/2` scorecard object, whose hash therefore recomputes from the
exact persisted bytes.

## Spec schema (`bench-spec/1`)

Exact top-level fields: `schema_version`, `name`, `tier`
(`dry-run|live`), `profile` (`smoke|full|confirmation`), `provider_config_ref`,
`overrides`, `episodes`, `episode_timeout_s`, `campaign_timeout_s`,
`replay_inputs`, `baseline_ref`.

* `budget`: `strategy_calls_total`, `judge_calls_total`, `max_total_episodes`,
  `max_candidates`, `max_total_wall_s`, `usd_limit` (nullable), `cost_mode`
  (`priced-bound|call-bounded|operator-approved-unknown`), `external_limit_ref`.
* `judge`: `enabled`, `model`, `rubric_version`, `deadline_s`,
  `max_state_bytes`, `max_response_bytes`, `retries` (exactly 0 initially).
* `comparison`: `metric_policy_version`, `target_metric`, `min_improvement`,
  `noninferiority_margins`, `min_samples`, `resampling_seed`, `resamples`,
  `confidence_level`, `screening_episodes_per_arm`,
  `confirmation_episodes_per_arm` (exact integers; confirmation strictly
  greater than screening), `min_completed_episodes_per_arm`,
  `min_aggregate_at_risk_ticks_per_arm`, `deadline_classification`
  (`horizon-completion|adverse-early`), `invalid_policy_version`,
  `invalid_policy_approval_hash`.
* `tuning`: `mode` (`suggest|apply-approved`), `parameters`, `approval_id`,
  `approval_expiry`, `expected_base_config_hash`.

Validation rejects unknown knobs, non-finite values, impossible caps, a
`retries` other than 0, a confirmation tier not strictly greater than the
screening tier, both exposure minima zero (there must be a predeclared floor),
and any attempt to enable DeepSeek postmortems. `postmortem_reserve` is
**forced to 0** in every bench campaign.

**Every campaign cap is literal, including zero.** `max_total_episodes = 0`
means zero episodes, `judge_calls_total = 0` means zero judge calls, and
`strategy_calls_total = 0` with the strategy tier on means zero strategy calls —
none of them means "unlimited". The **complete** tuning allocation (screening ×
candidates + confirmation, its judge calls, its strategy demand and its
duration) is checked against every cap by `tuning_preflight()` and by the `tune`
command, which refuses the plan **before any child or worker is spawned** and
writes no tuning report. `max_total_wall_s = 0` is the one exception: it is the
documented "unset" sentinel for a duration cap.

The spec stores credential *references* only (`***_key_file` paths); the bench
never copies a referenced secret into output.

## Scorecard contract (`episode-scorecard/2`)

Sections: `schema_version`, `episode_id`, `provenance_id`, `source_hashes`,
`integrity`, `terminal_class`, `termination`, `activity`, `exploration`,
`lifecycle`, `reflex`, `forced_search`, `usage`, `invalids`, `gates`,
`availability`.

**Documented `/2` extras (bench-owned, absent from `/1`).** The schema was
bumped deliberately from `/1` to add three top-level sections that carry the
deterministic gate evidence the comparison and postmortem consume:

* `terminal_class` — the episode's class from the exact precedence table.
* `invalids` — the split taxonomy (`available`/`reason`, native codes by code,
  resolved/unresolved `incomplete`, local validation fallbacks, `hard_failure`).
* `gates` — the per-episode gate evidence (`integrity_ok`, `operational_ok`,
  `operational_integrity_failure`, `invalid_evidence_available`,
  `forced_search_evidence_available`, `evidence_available`,
  `uncleared_forced_search`, `prohibited_postmortem`, `postmortem_reserve`,
  `postmortem_dispatched`, `hard_failure`).

`validate_scorecard_shape()` enforces the **exact** top-level and per-section
key sets: an extra field is a deviation, and a missing field is a deviation
unless the `availability` map explicitly accounts for it (a metric absent
*without* an availability reason is a gap, never silently accepted).

* `integrity.status` is `complete|partial|missing|invalid`. A **torn sidecar
  marks `partial` even when the lifecycle summarizer still produced output**:
  the summarizer skips malformed lines, so its output alone is not proof.
  `integrity.operational_ok` restates `controller.episode_ok` — it is
  *operational* success, **not** a game victory, and no field claims a win.
* `exploration` holds exactly the 15 fields from `exploration_metrics.py` plus
  `entered_per_100_attempts` (the sole initially derived productivity metric).
  There is **no revisit / moving-cycle metric**; the existing loop spans measure
  repeated `(hero, displayed time)` observations and miss moving oscillation.
* `lifecycle` holds the exact `lifecycle_metrics.summarize_artifact` keys. An
  unevidenced metric is `None` with an `availability` reason — **never a
  fabricated zero** (a genuine reported zero stays a measured zero, and a
  zero-denominator rate is `zero-denominator`, not `0`).
* `usage` copies the campaign-summary reduced set verbatim and adds
  bench-derived extras (`reserved_bounds`, `tariff`, `usd_cap`, `token_cap`,
  `providers`, `cost_status`, `reserved_upper_usd`, `reserved_upper_tokens`).
  Aggregate zero USD is never presented as "free" when any paid usage is
  unknown.
* `invalids` splits the invalid-action taxonomy: native codes **by code** (per
  `protocol.INVALID_CODES`), `incomplete` split into **resolved** (delivery
  repair) and **unresolved** (hard failure), and **local validation fallbacks**
  (a candidate rejected locally before send), kept distinct. The default
  gameplay-invalid hard-failure allowlist is **EMPTY**.

Scoring is byte-stable: canonical JSON, sorted keys, no timestamps and no
absolute paths in metric content.

## Provenance and comparability

One **provenance manifest** is the single comparability authority. It splits
every changed file/module into three domains:

* **deterministic** — controller/provider gameplay code, the bench
  runner/metrics/comparison code, the worker/launcher binaries, the
  data/sysconf trees, and the metric/exclusion policy. A difference in **this
  domain's content hash or version** is **hard not-comparable**.
* **advisory** — `bench_judge.py`, the judge rubric, the judge model and the
  judge response schema. A difference here must **not** invalidate the
  deterministic comparison or gate apply; it only invalidates the **judge
  cache/results**, forcing rejudgment.
* **reported-only** — timestamps, output paths, run labels. Never blocks a
  comparison.

**Commit ids and dirty hashes are recorded for audit only**, never used as the
comparability decision: two runs with different commit ids but identical
deterministic-domain hashes remain comparable, while a rubric-only change
forces rejudgment without changing the deterministic verdict.

## Comparison policy

One engine serves A/B and candidate-vs-baseline. It reports
`pass|fail|inconclusive|not-comparable` with per-metric direction, sample
counts, missing counts, summaries, effect, confidence interval, margin, gate
decision/reason, config diff and provenance differences.

* **Precommitted samples, no early stop.** Each run precommits an exact sample
  count and completes every scheduled episode; there is no early stop on a
  favorable interim look. Post-result changes to the metric set, margins,
  sample count or resampling policy are forbidden — abandon and re-precut
  instead (`bench.assert_precommitted`). **4 episodes/arm is unconditionally
  diagnostic-only** and never authorizes apply.
* **Interleaving, distinct arm configs and the precommitted design.** The run
  commits a `precommit.json` **before any result exists** containing the
  comparison policy, the exact **balanced** counterbalanced schedule
  (`episode_schedule`), the **per-arm config hashes** and the arm mode. An A/B
  run requires an explicit `baseline_config_ref` (a genuinely distinct,
  separately referenced config); each scheduled episode runs *its own arm's*
  config, so the arm labels are true. Without a baseline config the run is
  **candidate-only** — no baseline label is fabricated — and the comparison
  falls back to the external `baseline_ref` scorecards. The observed order is
  recorded **once per pair** (the committed representation), both arms of a
  same-run experiment share this run's deterministic provenance, and the
  comparison requires the observed arm counts, order and per-arm config hashes
  to match the committed design exactly — a committed 10/arm rejects 10/11
  *and* 11/11, an edited schedule is caught by its hash
  (`precommit-hash-mismatch`), a changed config is caught by
  (`config-hash-mismatch:<arm>`), and only an exact 10/10 may apply.
* **Unpaired bootstrap** intervals over episode-level metrics at a fixed seed
  and resample count, with simultaneous guardrail coverage (Bonferroni).
* **attempts_source admission.** Admission and apply require
  `attempts_source == "actions"`. The displacement fallback is allowed only for
  diagnostic comparisons; a **mixed** source is `not-comparable`, not merely
  noisier.
* **No matched-seed comparisons**: the controller supplies no seed argument, so
  seeds are `uncontrolled`. Do not claim matched-seed evidence.

### Termination-safety admission

Every episode is classified by the exact precedence table over the complete
administrative `stop_reason` vocabulary:

| stop_reason | class |
|---|---|
| `policy-exhausted` | adverse-early |
| `tick-cap-graceful-quit` | horizon-completion |
| `content-deadline` / `episode-timeout` | horizon/completion **or** adverse-early per `deadline_classification` |
| `protocol-failure`, `transport-failure-write`, `transport-failure-eof`, `spawn-failure`, `recorder-failure`, `closed-unanswered` | operational/integrity failure |
| `bench-stopped-graceful`, `bench-aborted` (bench-owned) | excluded-from-comparison |
| `closed` (generic) + `outcome=death`/`starvation` | adverse-early (observed, conservative) |
| `closed` + `outcome=ascension` | explicit `ascension` |
| `closed` + any other/unknown `outcome` | adverse/unknown |
| any unrecognized `stop_reason` | operational/integrity failure **or** adverse/unknown — **never benign** |

Visible-text `outcome` is observational (a heuristic inference), but it is used
for the conservative safety rate. All early adverse terminals are counted in
the rate denominators. A candidate whose adverse/unknown rate rises beyond the
approved noninferiority margin **fails** regardless of coverage gain; an arm
below any active predeclared exposure minimum is **inconclusive** (never
not-comparable).

## Cost facts (corrected)

* `reflex_call_cap` bounds **applied** Jev decisions (complete sends of
  unoverridden, locally valid proposals) — it is **not** a paid-call ceiling.
  Rejected/skipped/abstained consultations still cost money; never present the
  applied cap as a spend bound.
* **A token cap or a USD cap disables Jev dispatch outright** (the service-side
  token accounting is unknown), so a "strict-budget live-Jev" profile cannot
  honestly ship by wrapping those caps. A live Jev profile with a token/USD cap
  is rejected before spawn. Without a verified external provider-account limit,
  only an explicitly operator-approved, wall/tick-bounded **unknown-exposure
  diagnostic** run is honest.
* A USD cap requires a **complete DeepSeek tariff** (`price_in` and `price_out`);
  an unpriced USD cap is rejected before any spawn.
* `estimated_usd` can be nonzero when DeepSeek prices are absent because it may
  include priced Jev usage. Unknown price and unknown exposure are tracked
  separately and never merged.

### Jev judge cost and wire contract

Verified against the primary TypeSafe documentation
(`docs.typesafe.ai/api.md`, `models.md`, `patterns/fan-out.md`, and the
Cloudflare-hosted model page):

* `POST /v1/systemone` with `{state, model, questions}`.
* `noul` answer `{type, noul}` (0–1); `score` answer
  `{type, score, legend, probabilities, confidence}` where `score` is on the
  **level-index scale `0 .. len(criteria)-1`** and may land between levels
  (normalized locally to 0–1 only after validation).
* **Bundled multi-question requests are supported** (the documented fan-out
  pattern) and billed **per input token** of the single request: the three
  judge questions are sent in one request (`request_shape="bundled"`), so
  `judge_calls_total` counts **one** dispatch per episode. The three
  single-question shape is also implemented and unit-tested.
* Tariff: `$0.042/Mtok` input, **output free**. Illustrative: 2,000 input
  tokens ≈ $0.000084 — a reported estimate, not a guaranteed per-request cost.

## The advisory Jev judge (AC6)

One **advisory** judgment per eligible episode, run **after scorecard sealing**
and outside gameplay, charged to a separate bench ledger. The judge state is an
**allowlisted scorecard subset only** (metrics, units, availability flags,
terminal class) capped at 8 KiB, and never includes raw transcripts, credentials
or file paths.

Each answer is **strictly typed and validated**: the `noul` answer carries a
probability in [0,1]; the `score` answer carries `score`, `confidence`, a
`legend` keyed exactly by the level indices, and `probabilities` keyed exactly
by the level indices, each in [0,1] and summing to 1 (the validated
legend/probabilities/confidence are retained as typed metadata, with the score
normalized to 0–1 on the `0..len(criteria)-1` scale). A partial response, a
missing or extra field, a malformed legend or probability vector, or a body
whose `model` is not the requested model (or its documented alias
`jev-latest` → `jev-1.13.0`) is **invalid**, never partially credited. There are
**no automatic retries**, and rejudging is an explicit new recorded call. The
dispatch embeds the **exact rubric instance** whose hash/version the result
records (`self.rubric`, not a module constant). The cache key is
`scorecard hash + rubric hash + model hash`, so a rubric-only change forces
rejudgment. Cache hits are reported separately from dispatched calls.

**Policy: advisory-with-flags only.** Flags select postmortem packages; they
never contribute to the objective, veto acceptance, or change the rails.

**Dispatch.** The default judge is built over the **public worker transport**
(`make_worker_transport`): the resolved `BENCH_WORKER` executable, the credential
reference and base URL, the configured deadline and response cap. A
judge-enabled preflight that cannot resolve the worker or a key reference is
**refused** (`stage: "judge"`) before any episode launches, and a required
dispatch that cannot be constructed is a preflight error — never a silent
zero-dispatch advisory downgrade.

## Postmortem packages (AC12)

On a hard failure, behavioral regression/inconclusive result, or advisory judge
flag, the bench writes a bounded, checksummed local package containing: a
manifest identifying failed gates and the config diff; provenance and source
checksums; the relevant scorecards, comparison slice and judge answers (labelled
advisory); artifact paths; deterministic excerpts around protocol faults, the
largest loop/stationary spans, unresolved directives, unexplained replacements
and repeated pickup sites; the mutation validation report; and a concise task
for the coding agent whose text is explicitly framed as **not instructions**
(transcript strings are untrusted data).

Boundedness and evidence integrity are enforced, not aspirational:

* every source is **streamed** line by line under an input/read cap
  (`DEFAULT_MAX_INPUT_BYTES`, 1 MiB) — a multi-megabyte artifact is never
  `read().splitlines()`-ed whole, so peak read memory is bounded by the window
  plus one line;
* a byte cap on the kept lines records `truncated_bytes`, and when the input cap
  is hit `omissions_exact` is `false` with `omitted_at_least: true` (the omitted
  count is a lower bound, stated explicitly);
* excerpts carry the **line range**, `omitted_before`/`omitted_after`, and the
  **tick range** / **event range** of the kept records when present;
* the whole package is held inside an explicit **byte/count budget**
  (`DEFAULT_PACKAGE_MAX_BYTES`, `DEFAULT_PACKAGE_MAX_EXCERPTS`). **Every
  variable-size section** — excerpts, comparison slice, mutation report,
  provenance, config diff, evidence lists, judge answers, scorecards, the task
  text and the artifact-path map — is eligible for deterministic omission or
  summarization, and each omission is recorded in `omitted` with its reason. If
  even a minimal manifest cannot fit, the bench **refuses to write an oversized
  package** and writes a bounded `package_error: "budget-too-small"` record
  instead;
* `source_checksums` are **recomputed from every referenced source** — a file
  by its sha256 and a **directory** by a deterministic
  `directory_manifest_hash` (relative-path → sha256, `source_kinds` records
  which). A caller-supplied checksum is retained only as
  `source_checksums_supplied` and any disagreement is listed in
  `source_checksum_mismatches`;
* which episodes are excerpted is chosen by the deterministic
  `select_evidence()` ranking (fault reasons, then largest loop span, then
  episode id) — never by iteration order.

No automatic DeepSeek analysis, transcript upload or self-rewriting tuner.

## Tuning and apply policy (AC9)

Report-only coordinate search over the eligible knobs
(`reflex_call_cap`, `strategy_call_cap`, `boundary_cooldown_ticks`,
`boundary_cooldown_wall`) within operator rails, deterministic order, one
parameter at a time, at most two sweeps, a finite candidate budget. The
confirmation budget is reserved before searching. **`jev_relative_factor` is
frozen** unless a specific confidence-policy approval authorizes it;
confidence mode/threshold, eligibility/safety checks, emergency cooldown,
stall/door constants and forced-search budgets are not automatic tuning knobs.

`apply-approved` writes a versioned **nonsecret** config overlay only after
**all** of: a fresh confirmation passes, the termination-safety contract passes,
a **verified approval object** is present, every changed key/value is inside its
exact **authorized** grid/range, and the baseline config hash matches.

The approval object is `{"id", "authorization_hash", "authorized", "expiry"}`;
`authorization_hash` must equal the **recomputed** sha256 of the canonical
authorized payload — the recipe is documented so an operator can recompute it
independently:

```
payload  = {"id": <id>, "expiry": <expiry>,
            "authorized": {<knob>: {"grid": <sorted grid or null>,
                                    "min": <min or null>,
                                    "max": <max or null>}, ...}}   # sorted knobs
sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))
```

`bench.approval_hash(approval)` / `bench.approval_payload(approval)` implement
this exactly. Mutating **any** range or the expiry without re-hashing is
therefore rejected (`approval-authorization-hash-mismatch`); the approval `id`
must equal `spec.tuning.approval_id` (`approval-id-mismatch`) and its `expiry`
must equal `spec.tuning.approval_expiry` (`approval-expiry-mismatch`); and an
unavailable trusted clock fails closed (`trusted-time-unavailable`).

Overlays are **immutable and versioned**: two valid applies create two distinct
files (overwriting is refused). Each apply persists an `bench-apply-record/1`
with the exact before/after diff, the confirmation evidence/run ids, the
approval expiry and authorization hash, and the previous active reference.
`rollback_apply()` restores the previous overlay **byte-for-byte** (it returns
the restored content and its checksum). The bench never rewrites a secret config
file or patches a running agent.

## Stop escalation and containment (AC7)

A bench stop after the current episode is requested with the first
`SIGINT`/`SIGTERM` or a stop-file; the requested stop is persisted as the
bench-owned `stop_reason=bench-stopped-graceful` (excluded-from-comparison).
The bench admits **no new episodes or judges**. A second interrupt or the total
deadline triggers the forced path: a bench-owned, **ownership-isolated**
`/proc` PPID-recursion walk that launches the episode root in a dedicated
session, captures PID/start-time/session/PGID, signals a process group only when
its validated membership belongs to the captured episode tree **and does not
contain the root**, keeps the root alive while re-walking to catch children
spawned during the walk, reaps the root last, and verifies no captured identity
survives. A descendant that inherits the dedicated root's PGID is therefore
signalled **individually** until root-last, so the root can never be killed by a
group signal early. It **never** `killpg`s the supervisor's own group.

Fail-closed guarantees (AC7):

* `ProcReader.identity()` distinguishes genuine disappearance (ENOENT → `None`)
  from a **permission, I/O or parse failure**, which raises `ProcError`; a
  `getpgid` permission failure is likewise fatal, never a silent `-1`;
* `_alive()` **fails closed**: a verification exception records a teardown
  failure and treats the process as still alive;
* a permission or identity-validation failure sets `teardown_failure` / partial
  status, marks the run non-success, blocks baseline promotion, and fails AC7
  even when cleanup continues best-effort.

Graceful cancellation is the primary path: the parent writes the **root-ready
handshake** only after the captured root identity is durably recorded, and the
child **blocks on that handshake** before starting the (expensive) episode work;
the child installs bench-owned SIGINT/SIGTERM handlers that record a durable
cancellation acknowledgment and let the bounded episode reach the controller's
own `finally` instead of dying mid-flight. The parent relays the first
(graceful) stop as SIGTERM and awaits that acknowledgment within the grace
window.

Residual limits are documented: an inherent race window and PID reuse (identity
and start time are validated before signalling).

Interrupted reservations remain **unknown exposure** and are not refunded
automatically; resume is opt-in and validates hashes/locks, and never replays a
possibly-billed judge request silently. A partial run is never promoted to
baseline.

## Acceptance criteria

| AC | Summary |
|---|---|
| AC1 | Artifact scoring byte-stable, versioned, faithful; missing/legacy/corrupt/zero-denominator never a measured zero |
| AC2 | Dry-run makes no provider calls and reports Jev not evaluated |
| AC3 | Requested vs observed tiers recorded; a fallback-only Jev run cannot pass live validation |
| AC4 | Hard-gate failures cannot be offset by coverage or judge score |
| AC5 | Paid activity reserved/accounted or marked unknown; unsupported strict-spend configs fail before dispatch; applied caps are never spend ceilings |
| AC6 | Zero DeepSeek postmortem calls; the Jev judge is per-episode, typed, bounded, advisory, separately metered; cache/rejudge accounting is distinguishable |
| AC7 | Stops admit no further work, reap all children, preserve partial artifacts, never label interruption successful; the forced path signals only the owned tree and a permission/identity failure fails the criterion |
| AC8 | Search is finite, repeatable, obeys episode/wall/candidate budgets, and requires fresh confirmation before apply |
| AC9 | Apply changes only authorized keys/ranges against the approved config hash, with exact diff, evidence and rollback pointer; default is report-only |
| AC10 | Live testing does not begin before the vapor-cloud prerequisite lands, enforced by an executable preflight test |
| AC11 | The termination-safety admission contract is predeclared, deterministic, and blocks any apply |
| AC12 | A postmortem package is bounded, checksummed and untrusted-transcript-safe |

**AC10 status: pending-operator.** The caller must attest that the vapor-cloud
fix is landed before any live bench testing: set `BENCH_VAPOR_CLOUD_ATTESTED` to
the **exact documented token** `vapor-cloud-fix-landed-and-tested` (any other
non-empty value — `yes`, `true`, a label — is refused by preflight).
`mutation_checks.py` records `live_claims.measured: false`; no live campaign was
run in this implementation.

## Per-change workflow

The bench smoke profile joins the correctness work for any change to the agent:

```sh
python3 -m unittest discover -s test/agent -p 'test_auto*.py'   # correctness gate
python3 test/agent/mutation_checks.py                           # mutation gate
make -C test/agent check                                        # fixture gate
python3 -m tools.agent.bench validate bench-spec.json           # bench preflight
python3 -m tools.agent.bench run bench-spec.json --out-dir /tmp/bench-smoke
```

A **diagnostic** campaign of the spec's exact `screening_episodes_per_arm`
episodes per arm, followed by the larger campaign of the spec's exact
`confirmation_episodes_per_arm` episodes per arm, run separately. Store a
versioned baseline and calibrate practical margins before permitting automatic
apply.

## Known limitations

* No seed control: the controller supplies no seed argument, so seeds are
  `uncontrolled`; no matched-seed claim is justified. Save restoration is a
  different scenario distribution, not a matched seed.
* Coverage favours exploration but does not prove survival/endgame skill;
  entered cells can be gamed by reckless play — hence the termination-safety
  admission contract.
* Current loop metrics miss moving cycles; lifecycle rates can improve by
  avoiding difficult tasks.
* Provider nondeterminism remains an explicit limitation; unpaired comparisons
  are the honest default.
* Strict-budget live-Jev autotuning remains **blocked** without a verified
  external hard provider quota. Do not conceal this with a watchdog or an
  intercepting proxy.
