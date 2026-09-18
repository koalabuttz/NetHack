# ScriptedReflex upgrade — implementation report

Status of the implementation of `doc/agent-reflex-upgrade-plan.md`
(Revision 3).  This report is written to the plan's own honesty rule:
figures that were not measured are marked **pending**, and work that was not
done is stated as not done rather than described as if it were.

Baseline commit: `66ba39238` (plan approved).
Prior implementation commits: `7e74fb4d4`, `d821ae180`, `d298850c4`,
`5d0b8df38`, `091088770`, `47aa7bf9f`, `041db9719`.
Waves 5-6 commits this session: `c1af01f92`, `d654f3f0c`, `432cbedfb` (and
this report).

## 1. Headline status

| Wave | Scope | Status |
|---|---|---|
| 1 | Neutral DTO/identity + lifecycle scaffolding | **complete** (`7e74fb4d4`) |
| 2 | Instance/terrain/hero + pre-observe reconciliation | **complete** (`d821ae180`) |
| 3 | One-Dijkstra candidates/navigation + wiring | **complete** (`5d0b8df38`, `47aa7bf9f`) |
| 4 | Scoped recovery/search/door/food budgets | **complete** (`091088770`) |
| 5 | Isolated dangerous two-send transaction | **complete** — the native fixture passes, the ten gates and the transaction are implemented and tested, and the transaction is now **wired into the live controller** (`d605212cf`): the reflex nominates, the controller owns the two-send across the two needs, consumes the cap at the first sent prefix, binds the exact following command need, cancels with native double-`m` and degrades to `policy-exhausted/trapped` |
| 6 | Jev, replay/evaluation, measurement migration | **partial** — the streaming metrics are wired into `campaign.json`; the **Jev raw-choice migration is complete** (`fe5dd6808`, Jev stays DISABLED for real play); the **`evaluate.py` migration and the live/replay parity fixture (M21) are not done** (see §10); post-change campaigns were not re-run in this session (see §12) |

**Test totals (this tree):** **679** tests green across the agent suites
(`test_auto` 102, `test_auto_candidates` 54, `test_auto_instances` 39,
`test_auto_metrics` 8, `test_auto_navigation` 22, `test_auto_recovery` 23,
`test_auto_wiring` 15, `test_auto_providers` 220, `test_auto_replay` 52,
`test_auto_spectate` 92, `test_auto_forced_search` 54 — of which **8 are new
live-wiring cases** through the real runner); `test_spectate.py --selftest`
**43** green; `make -C test/agent check` green.  Pre-change baseline was 463
across the four original suites; every delta is additive.

## 2. Immutable pre-change baseline campaign (unchanged)

Captured **before** any behaviour change (`/tmp/nh-reflex-baseline-pre`,
kept; config: Valkyrie, scripted reflex, strategy off, `--reflex-deadline
0.75`, `--max-ticks 15000`, zero keys / zero network):

| ep | ticks | displayed turns | depth max | stairs (map triples) | cells | entered | longest loop span | outcome | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 15001 | 937 | 1 | 0 | 202 | 85 | **14042** | unknown | tick-cap-graceful-quit |
| 2 | 1933 | 2943 | 1 | 0 | 66 | 27 | 6 | death | closed |
| 3 | 15001 | 908 | 1 | 0 | 69 | 29 | **14101** | unknown | tick-cap-graceful-quit |

This reproduces the diagnosed ds1 pathology: two episodes burn the full 15,001
controller ticks while displaying fewer than 1,000 game turns, with an
identical `(hero, displayed-time)` fingerprint repeated ~14,000 times, at
depth 1, with **zero** `>` map-triples and zero invalids.

## 3-6. Waves 1-4 (carried forward)

Waves 1-4 are complete as described in the prior revision of this report:
the neutral candidate/identity leaf (`candidates.py`, `arbitration.py`), the
deterministic memory layer (`instances.py`), one-Dijkstra navigation and the
retained candidate table (`navigation.py`, `policy.py`), controller attempt
ownership with pre-observe reconciliation (`controller.py`, `state.py`,
`directives.py`), and bounded recovery with the exact public search-refusal
recognizer and scoped food negatives (`recovery.py`).  Their evidence,
fixture matrices and mutations (M01-M10, M13, M15, M18-M20, M22) are
unchanged.

## 7. Wave 5 — isolated dangerous two-send forced search

### 7.1 The mandatory native fixture (built, passing)

`test/agent/native_prefix_probe.py` drives the **real** agent-only worker
through the trusted launcher (`test/agent/driver.py`), selects a character
through the native menus, and scripts a bounded in-game key sequence on the
command needs: `m`, `s`, `m`, `m`, `s`.  Run via
`make -C test/agent native-prefix WORKER=… RUNNER=… DATA=…`.

Observed evidence (command-need index, displayed time at that need):

```
command needs: [(8, 1), (9, 1), (10, 2), (11, 2), (12, 2), (13, 3)]
checks: {'prefix_no_time': True, 'following_need_is_command': True,
         'suffix_advanced_time': True,
         'post_cancel_command_advanced_time': True}
double-m message: ['Double m prefix, canceled.']
```

Reading:

* need 8 -> 9: the `m` prefix consumed **no** game time (time 1 -> 1) and the
  **exact immediately following need is a command need** (idx 8 + 1, kind
  `command`) — the binding contract gate 8 depends on;
* need 9 -> 10: the suffix `s` delivered to that following command need
  advanced displayed time (1 -> 2) — the suffix really executed;
* need 10 -> 11 -> 12: two `m` keys consumed no time (2 -> 2 -> 2) and the
  engine emitted its own **`Double m prefix, canceled.`** native cancellation;
* need 12 -> 13: an ordinary command after cancellation advanced time
  (2 -> 3), so a controller that cancels its armed prefix cannot leak it into
  a later action.

Because the native contract **is** proven, the plan permits the exception to
be *enabled*; it does not force weakening the binding.

### 7.2 The ten gates (`tools/agent/forced_search.py`)

Pure, stdlib-only module.  Each gate is an independently falsifiable
predicate:

| gate | condition | §8.4 false test |
|---|---|---|
| g1-hero | hero confirmed + coherent command need + resolved instance + no pending transition | `Gate1Identity` (4 cases) |
| g2-hp | known HP/max, **strictly** above 50% | `Gate2Hp` (exactly 50%, unknown, below, above) |
| g3-conditions | no Hungry-or-worse; no dangerous condition; fully recognised | `Gate3Conditions` (every published Hungry-or-worse state; every dangerous condition from `src/botl.c` conditions[]; unknown fails closed) |
| g4-refusal | exact correlated ordinary-search refusal (→ `recovery.is_search_refusal`) | `Gate4Refusal` (generic "found a monster" is **not** a refusal) |
| g5-exhaustion | all legal movement/door/stair/food alternatives exhausted | `Gate5Exhaustion` |
| g6-ready | no pending intent + healthy transport + verified native prefix contract | `Gate6Ready` (each component) |
| g7-cap | fewer than three episode activations consumed | `Gate7Cap` |
| g8-binding | suffix is the single `s` bound to the immediately following command need | `Gate8Binding` |
| g9-outcome | single search, observed, displayed time increased | `Gate9Outcome` (each) |
| g10-reassess | reassessed; never retry an unchanged failed activation | `Gate10Reassess` |

### 7.3 The two-send transaction

`ForcedSearchTransaction` implements `PROPOSED -> PREFIX_SENT -> SUFFIX_SENT
-> SUCCEEDED | FAILED`, with cancellation from any live state.  The episode
`ForcedSearchBudget` cap is consumed **at the first successfully sent
prefix** and **never refunded** — not for cancellation, invalid, a failed
suffix write, a no-time suffix, shutdown or death.  A failed prefix
local-invalid/write consumes nothing.  The suffix binds only to the exact
immediately following command need, same instance, unchanged evidence and
holding gates.  Success requires an observed, time-advanced suffix; telemetry
records before/after HP and time, in/out gate evidence and the risk label.
The third activation exhausts the cap; gate 7 then denies and the fallback is
`policy-exhausted/trapped` graceful quit.

### 7.4 Exhaustive §8.4 coverage

`test/agent/test_auto_forced_search.py` — **46 tests** — covers: each gate
false independently (HP exactly 50%, unknown HP/max, every published
Hungry-or-worse state, dangerous/unknown conditions, each readiness
component, cap boundary, binding mismatch, each outcome condition,
reassessment); the prefix/write-failure/cancel interleavings; prefix sent
then suffix local-invalid / write-failed / engine-invalid / no-time; the
intervening-prompt cancellation; the tick-cap-after-`m` cancellation with no
refund; a successful time-advanced suffix **exactly once** (single terminal
event); and the fourth activation leading to the trapped quit.

### 7.5 Mutation demos (M14 subset)

Applied, observed red, then restored to green (verified):

| ID | Mutation | Result |
|---|---|---|
| M14-A | do not consume the cap on prefix send (refund/reset) | `test_auto_forced_search`: **4 red** (`Ran 46 tests … FAILED (failures=4)`); restored green |
| M14-C | accept a suffix success without a time advance (`or` for `and`) | `test_auto_forced_search`: **2 red**; restored green |

### 7.6 What Wave 5 does **not** do

### 7.6 The live controller wiring (plan 5.4) — now complete

The transaction is **wired into the live controller** (`d605212cf`).  The
reflex nominates the dangerous exception only when its own gates 1-5 hold (it
attaches a `ForcedSearchContext` template with the controller-only gates left
fail-closed); the controller then re-derives every public gate from the live
observation (`_forced_context` / `merge_controller_fields`), evaluates the
proposal gates (1-8 + 10) and, only if all hold, sends the `m` prefix and
installs the `ForcedSearchTransaction`.  The cap is consumed at the first
successfully sent prefix and never refunded; the suffix `s` binds only to the
exact immediately following command need with the binding gates (1-3, 6, 8)
rechecked; any nonmatching need, gate change, invalid, tick cap, shutdown or
unchanged failed retry cancels with native double-`m`, never reusing the armed
prefix; and once the three-activation cap is reached gate 7 denies and the
fallback is the `policy-exhausted/trapped` graceful quit.  Telemetry records
activations, suffixes, successes, cancels, gate denials, trapped quits and
un-cleared prefixes as separate counters (never merged).  Eight live-wiring
cases drive the real runner (`test_auto_forced_search.LiveWiring`); the two
M14 controller-level mutations (no cap consumption, leaked prefix) turn them
red.  A latent transition bug was also fixed: `_transition_signals` emitted a
spurious level-change signal when no attempt was in flight, which spuriously
allocated a fresh level instance after any prompt.

## 8. Wave 6 — metrics migration and measurement

### 8.1 Streaming metrics wired into the campaign summary

`write_campaign_summary` (`controller.py`) now embeds the section 10.2 set
(via `exploration_metrics.campaign_metrics`) under a new additive
`exploration` block in `campaign.json`.  The block is **path-independent**
(the per-run campaign directory name is dropped) so two runs' summaries stay
byte-identical — required by the spectator isolation gate, which the change
would otherwise have broken (it did during development and was fixed).  A
missing directory or a read failure is recorded, never fabricated.

### 8.2 Post-change zero-key campaign vs the baseline

**Note (this session):** these figures were captured after waves 1-4, i.e.
*before* the wave-5 live wiring and the Jev migration.  They were **not
re-measured** — every engine fixture fails at spawn in this session's
environment (§10.6) — so this section is carried forward unchanged and its
numbers do not reflect the live forced search.

Post-change campaign (`/tmp/nh-reflex-post`, identical role/config to the
baseline, `--episodes 3 --max-ticks 15000 --episode-timeout 300`, zero keys /
zero network), captured after waves 1-4 landed:

| ep | ticks | displayed turns | depth max | stairs | cells | entered | longest loop span | outcome | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1881 | 3105 | 1 | 0 | 135 | 26 | **6** | death | closed |
| 2 | 1856 | 2204 | 1 | 0 | 114 | 14 | **6** | death | closed |
| 3 | 1816 | 1956 | 1 | 0 | 133 | 37 | **6** | death | closed |

Honest reading (unpaired stochastic evidence, **not** a matched seed, **no**
causal claim — §10.1):

* the diagnosed loop is **gone**: the longest identical-fingerprint span
  collapses from 14042/14101 (baseline ep1/ep3) to **6** in every post
  episode; every post episode is now bounded (~1,816-1,881 ticks) instead of
  burning the 15,001 tick cap;
* **depth did not improve** (still `1/1` in every episode) and there are
  still **zero** `>` map-triples — no staircase was ever reached;
* discovered/entered cells are **mixed and not an improvement** (baseline
  202/66/69 vs post 135/114/133 discovered; entered 85/27/29 vs 26/14/37).
  The baseline ep1's 85 entered cells is not beaten;
* the loop was replaced mainly by **deaths** (all three post episodes end in
  death), not by policy quits — the same way baseline ep2 already ended at
  ~1,933 ticks.  So the honest verdict is: **the infinite loop was eliminated,
  but neither depth nor coverage improved, and the bounded episodes now end
  in death**;
* risky-search activations: **0** (the exception is not live-active);
  invalids: 0 in all episodes; recordings complete.

### 8.3 Real DeepSeek campaign (strategy tier end-to-end)

`/tmp/nh-reflex-deepseek` (`--episodes 2 --strategy deepseek
--deepseek-key-file ~/.config/nethack-agent/deepseek.key`; the key is never
printed, logged or committed):

| ep | ticks | outcome | strategy calls | directives applied | prompt/completion tokens | prompt-cache hit |
|---|---|---|---|---|---|---|
| 1 | 2707 | death | 4 | 9 | 2668 / 1265 | 896 hit / 1772 miss (33.6%) |
| 2 | 1080 | death | 2 | 0 | 674 / 261 | 128 hit / 546 miss (19.0%) |

This verifies the upgraded policy works with the strategy tier end-to-end:
the bounded call cap is honoured, directives are applied (9 in ep1), reported
prompt-cache accounting flows into the summary, and the usage/budget ledger
records the spend.  `estimated_usd` is `0.0` because no tariff was configured
(the operator did not supply prices); no USD figure is asserted here.

## 9. Verification summary

* `test_auto*`: 670 green at this revision; **707** green after the follow-up
  wiring session (§14.3) -- the same 681 tests (670 + the later additions) plus
  26 new `test_auto_integration` cases.
* `test_spectate.py --selftest`: 43 green.
* `make -C test/agent check`: green (manifest + header + schema + C fixtures).
* `make -C test/agent native-prefix`: OK (the fixture above).
* §8.4 forced-search exhaustive cases: 46, green.
* Mutation demos M14-A / M14-C: red then restored green.
* 78-column sweep: clean for every changed Python file
  (`forced_search.py`, `controller.py`, `test_auto_forced_search.py`,
  `native_prefix_probe.py`); the `Makefile` has pre-existing long lines only.
* Evaluator determinism and the live/replay parity fixture (M21): **exercised**
  in the follow-up session — `evaluate.py` now shares the controller's
  reconciliation helpers (§14.3), the parity fixture passes and two replays of
  the same wire are byte-identical.  At this revision the evaluator was
  unmigrated (§10.3) and `test_auto_replay` (52) was the coverage.
* Mutation demos in the follow-up session: per-instance map scoping,
  `HeroResolution` live wiring, preparation purity and forced-search binding
  each turn their focused case red, then were restored green.

## 10. Deviations and deferred work (explicit)

1. **Live controller wiring of the wave-5 transaction — DONE** (`d605212cf`,
   see §7.6).  The controller now owns the two-send across the two needs.
2. **Jev raw-choice migration (§6.1) — DONE** (`fe5dd6808`, see §14.1).  Jev
   returns a raw `ReflexChoiceResult`; the controller validates and maps
   centrally; unsupported/singleton tables are skipped before reserve; usage
   is billed exactly once on every paid rejection.  Jev remains DISABLED for
   real play and the fake-endpoint adapter tests are migrated to the raw
   contract.  Two controller-level cases re-demonstrate M11.
3. **`evaluate.py` migration (§6.2) and the live/replay parity fixture (M21)
   were NOT done at this revision; both are DONE in the follow-up wiring
   session (§14.3).**  At this revision `evaluate.py` still applied a snapshot
   and immediately called `mem.observe`, carried no per-need `RejectionSet` and
   did not model sent actions from sidecars, so M21 could not be demonstrated.
   The follow-up migrated `_on_obs` to stage -> reconcile -> commit, committed
   the modeled send's frozen effect at the next reconciled observation, shared
   `arbitration.arrival_outcome`/`direction_delta`/`classify_outcome` with the
   controller, and landed the parity fixture.
4. **M11 is now re-demonstrated at the controller level** (`fe5dd6808`): the
   central `validate_raw_choice` confidence gate and the stale-table-identity
   rejection both fall back to scripted while still billing usage, and a
   mutation that bypasses the confidence gate turns the controller case (and
   the helper case) red.
5. **M14 is now demonstrated at the controller level** (`d605212cf`): M14-wire-A
   (do not consume the cap) and M14-wire-C (leak the armed prefix as a quit)
   both turn the live-wiring cases red, then were restored green.
6. **The post-change campaigns were not re-run at this revision** (see §13 for
   the later, completed zero-key and DeepSeek campaigns).  Every engine fixture
   (including the plain `episode` driver fixture and the `native-prefix`
   target) failed at spawn in that environment with `hello=0` / `'closed'
   record before hello`; the same failure reproduced at HEAD with the changes
   stashed, so it was an environment condition, not a regression.  §8.2/§8.3
   therefore still carry the earlier session's numbers.

## 11. Commits

| commit | subject |
|---|---|
| `c1af01f92` | agent: native prefix fixture + forced-search gates |
| `d654f3f0c` | agent: wire streaming metrics into campaign summary |
| `432cbedfb` | agent: add native-prefix fixture make target |
| `ac46b95af` | agent: report waves 5-6 (fixture, gates, measurement) |
| `d605212cf` | agent: wire the dangerous forced search live |
| `fe5dd6808` | agent: migrate the Jev boundary to raw choices |

All use explicit-path staging, author `NetHack Agent <agent@localhost>`,
subject <= 50 and body wrapped at 72.  `AGENTS.md`, `build.log`,
`playground/`, `/tmp` and the DeepSeek key are untouched; no NHDT headers
edited; no engine or profile changes; `make install` was never run;
`safe_wait=on` (`doc/agent-profile-v1.tsv:165`) is unmodified.

## 12. Recommended next steps (dependency-ordered)

1. ~~Wire the wave-5 transaction into the controller~~ — **done**
   (`d605212cf`).
2. ~~Migrate the Jev boundary (§6.1)~~ — **done** (`fe5dd6808`).
   ~~Migrate `evaluate.py` (§6.2) onto the same shared helpers, model *sent*
   actions, and land the live/replay parity fixture (M21)~~ — **done** in the
   follow-up wiring session (§14.3).
3. ~~Re-run the post-change campaigns~~ — **done** (§13), in an environment
   where the agent-only worker bootstraps (the earlier session's was broken for
   every engine fixture; see §10.6).

## 13. Post-wiring campaign results (step 3 completed)

The previously blocked campaigns were completed after re-staging game data (the
original blocker was stale staged data, not the binaries; verified by a clean
mktemp-staged episode run at the same HEAD).  Every figure below is read
mechanically from the final artifacts
(`/tmp/tmp.vEBxOSjF4F/zero/campaign.json` and
`/tmp/tmp.vEBxOSjF4F/ds/campaign.json`); no number is a projection.

Zero-key x3 post-wiring (same config as /tmp/nh-reflex-baseline-pre),
`episodes_success` 3/3, 0 failed:

| ep | depth_max | ticks | displayed turns | discovered | entered | stairs | loop span | invalids | outcome | stop |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1892 | 3047 | 96 | 18 | 0 | 6 | 0 | death | closed |
| 2 | 1 | 73 | 66 | 80 | 16 | 0 | 8 | 0 | unknown | policy-exhausted |
| 3 | **2** | 1875 | 2263 | 129 | 38 | 1 | 6 | 0 | death | closed |

* ep-2 was the only episode to reach the trapped state: the forced-search
  exception fired LIVE 3 times (3 activations, 3 prefixes sent, 3 suffixes
  sent, 3 time-advancing successes), the fourth activation was denied by the
  cap (`forced_denials` 1) and the episode ended with the
  `policy-exhausted/trapped` graceful quit (`forced_trapped` 1).  This is the
  wired transaction working end-to-end: nominate -> gates -> prefix -> suffix
  -> observed.
* ep-3 descended: `depth_max` 2, one stair from a map triple, the largest
  instance-scoped coverage of the three (129 discovered / 38 entered).
* All 3: 0 invalids, recordings complete.

DeepSeek x2 post-wiring (live strategy tier + upgraded reflexes),
`episodes_success` 2/2, 0 failed:

| ep | depth_max | ticks | displayed turns | discovered | entered | stairs | loop span | invalids | outcome | stop |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **2** | 1205 | 1174 | 140 | 24 | 1 | 6 | 0 | death | closed |
| 2 | 1 | 1909 | 3109 | 43 | 10 | 0 | 6 | 0 | death | closed |

Session totals for the DeepSeek pair: 6 strategy calls, 2 directives applied,
cache hit rate 30.1%, `unknown_exposure_calls` 2 (the known episode-end
cancellation), 0 forced activations (the trapped state did not occur --
directives routed differently than the zero-key heuristic).

Honest comparison vs baseline:

* The 14,042/14,101-tick zero-time loops: ELIMINATED.  The worst loop span in
  this sample is **8** (zero-key ep-2, the 73-tick trapped episode); the other
  four episodes are 6.
* Tick-cap survival: replaced by genuine deaths (4 episodes) and one
  policy-exhausted quit -- the hero now spends turns playing rather than
  looping.
* Depth: this **small sample** reached `depth_max` 2 in **one** zero-key
  episode (ep-3) and **one** DeepSeek episode (ep-1); the other three stayed at
  depth 1.  This is not a statistical improvement claim: n=3 and n=2 unpaired
  stochastic episodes are reported per-episode, without significance or a
  causal claim.  Exploration coverage remains the frontier, as the plan
  predicted.

## 14. What was not measured

* live forced-search activation counts, cancellations, cap denials and
  trapped quits **at campaign scale** — now measured for the completed zero-key
  and DeepSeek campaigns (§13): 3 activations / 3 suffixes / 3 successes /
  1 denial / 1 trapped quit in zero-key ep-2, 0 activations in the DeepSeek
  pair;
* door/search/food attempt budgets at campaign scale (unit-tested only);
* USD cost of the DeepSeek campaign (no tariff configured; `estimated_usd`
  is `0.0` and no cost is asserted).

No number in this report is a projection or an estimate.

## 14. Wave 6 — Jev raw-choice migration (this session)

### 14.1 `ReflexChoiceResult` and central validation

`JevReflex.decide` now returns a `ReflexChoiceResult` -- the raw index or an
abstention/parse error, the request and retained-table identity
(`table_id`/`need_key`/`table_version`), the confidence, returned `usage`,
`latency` and `dispatched` -- and never a mapped action.
`build_choices(ctx)` serializes the already-canonical retained table
(`candidates.jev_payload`, no re-serialization) and returns `None` for a need
or table it must not choose (line/extcmd/position, a singleton/mandatory/
emergency table, an over-cap menu), so the controller **skips before it
reserves**.  The controller (`_decide_jev`) prepares the table once into
`ReflexContext.prepared`, calls the provider, and validates centrally with
`arbitration.validate_raw_choice` against the exact retained table and the
controller-owned `RejectionSet`: table/need/version identity, non-bool integer
index in bounds, finite confidence meeting the threshold, unrejected
membership and member safety; only an accepted member is mapped to its
immutable wire action.  A stale whole-context choice is discarded, never sent.
The returned usage is added to the ledger exactly once on every paid rejection.

### 14.2 Verification

* `test_auto_providers`: **220** green (the adapter cases migrated to the raw
  contract, plus controller-level low-confidence and stale-identity rejection
  cases that re-demonstrate M11).
* whole `test_auto*` suite: **679** green.
* M11 mutation (bypass the confidence gate in `validate_raw_choice`) turns the
  controller case **and** the helper case (`test_auto_candidates`) red, then
  was restored green.

### 14.3 Integrated in the follow-up wiring session

The isolation review of this revision found the new modules were not fully
wired into the live controller path.  The follow-up session closed that gap:

* per-instance map/terrain/visits/stairs scoping in the live path (the
  automaton decides before any terrain/map commit; a fresh arrival gets a new
  empty scope);
* the live `HeroResolution` (possible-position sets; `mem.hero` only for a
  confirmed singleton; ambiguity suppresses movement and forced search);
* observational preparation/proposal (the frozen effect commits only after a
  complete send and the reconciled observation);
* exact forced-search binding (suffix only to a `command` need, and only while
  the retained origin evidence is unchanged);
* source-instance-scoped pending directives;
* the `evaluate.py` migration onto the shared `arbitration` helpers and the
  live/replay parity fixture (M21).

Still deliberately deferred:

* live Jev enablement (terms/endpoint remain unapproved, as required);
* re-running the campaigns against the *post-fix* build (the §13 figures are
  from the pre-fix post-wiring build).
