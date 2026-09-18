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
| 5 | Isolated dangerous two-send transaction | **partial** — the native prefix/cancellation fixture is built and **passing** through the real adapter; the ten gates and the `PROPOSED…SUCCEEDED\|FAILED` transaction are implemented and exhaustively tested; **live controller wiring is not done**, so the exception is not yet live-active (see §7.6) |
| 6 | Jev, replay/evaluation, measurement migration | **partial** — the streaming metrics are now wired into `campaign.json`; post-change zero-key and real DeepSeek campaigns were run and measured (see §8); the **Jev raw-choice migration and the `evaluate.py` migration + parity fixture are not done** (see §10) |

**Test totals (this tree):** **670** tests green across the agent suites
(`test_auto` 102, `test_auto_candidates` 54, `test_auto_instances` 39,
`test_auto_metrics` 8, `test_auto_navigation` 22, `test_auto_recovery` 23,
`test_auto_wiring` 15, `test_auto_providers` 217, `test_auto_replay` 52,
`test_auto_spectate` 92, **`test_auto_forced_search` 46 new**);
`test_spectate.py --selftest` **43** green; `make -C test/agent check` green
(manifest + header + schema + `test_view/menu/protocol/state`).  Pre-change
baseline was 463 across the four original suites; every delta is additive.

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

The transaction is **not wired into the live controller**.  The reflex still
degrades member exhaustion to a single structural `s` proposal (`policy.py`
`decide`), and no live code path proposes or sends the `m`/`s` pair.  The
exception is therefore implemented, fixture-proven and unit-tested but **not
live-active** (risky-search activations are correctly 0 in §8).  This is a
real deviation from the plan's wave-5 change list and is **not** a claim of
completion (see §10).

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

* `test_auto*`: 670 green; `test_spectate.py --selftest`: 43 green.
* `make -C test/agent check`: green (manifest + header + schema + C fixtures).
* `make -C test/agent native-prefix`: OK (the fixture above).
* §8.4 forced-search exhaustive cases: 46, green.
* Mutation demos M14-A / M14-C: red then restored green.
* 78-column sweep: clean for every changed Python file
  (`forced_search.py`, `controller.py`, `test_auto_forced_search.py`,
  `native_prefix_probe.py`); the `Makefile` has pre-existing long lines only.
* Evaluator determinism and the live/replay parity fixture (M21): **not
  exercised** — the evaluator was not migrated (§10).  The existing
  `test_auto_replay` suite (52) remains green.

## 10. Deviations and deferred work (explicit)

1. **Live controller wiring of the wave-5 transaction is not done.**  The
   module, gates, transaction and the native fixture are complete and tested,
   but no live code path proposes or sends the `m`/`s` pair, so the exception
   is not active in play.  The plan's wave-5 change list requires the
   controller two-send ownership; that remains the next step.
2. **Jev raw-choice migration (§6.1) is not done.**  `JevReflex.decide` still
   returns a mapped `ReflexResult` and maps the index inside the adapter; the
   `ReflexChoiceResult` raw path, `ReflexContext.prepared`-based
   `build_choices`, central `validate_raw_choice` mapping and the
   skip-before-reserve/billing changes are not implemented.  Jev remains
   DISABLED for real play; the existing fake-endpoint provider tests (217,
   including the Jev block) remain green and unmodified.
3. **`evaluate.py` migration (§6.2) and the live/replay parity fixture are
   not done.**  `evaluate.py` still applies a snapshot and immediately calls
   `mem.observe`.  M21 cannot be demonstrated because its subject is unmigrated.
4. **M11 is not re-demonstrated** beyond the wave-1 evidence already in the
   prior report: central live validation/mapping (its wave-6 home) is unmigrated.
5. **M14 is demonstrated only at the module level** (M14-A, M14-C above); the
   controller-level M14 variants depend on deviation 1.

## 11. Commits

| commit | subject |
|---|---|
| `c1af01f92` | agent: native prefix fixture + forced-search gates |
| `d654f3f0c` | agent: wire streaming metrics into campaign summary |
| `432cbedfb` | agent: add native-prefix fixture make target |

All use explicit-path staging, author `NetHack Agent <agent@localhost>`,
subject <= 50 and body wrapped at 72.  `AGENTS.md`, `build.log`,
`playground/`, `/tmp` and the DeepSeek key are untouched; no NHDT headers
edited; no engine or profile changes; `make install` was never run;
`safe_wait=on` (`doc/agent-profile-v1.tsv:165`) is unmodified.

## 12. Recommended next steps (dependency-ordered)

1. **Wire the wave-5 transaction into the controller**: have the reflex
   propose the forced search only when the recovery ladder is genuinely
   exhausted, and give the `_EpisodeRunner` ownership of the
   `ForcedSearchTransaction` and `ForcedSearchBudget` across the two needs
   (`_answer_now` prefix send, the following command need's suffix send),
   with the native-verified double-`m` cancellation used on every abort path.
2. **Migrate the Jev boundary (§6.1)** and **`evaluate.py` (§6.2)** onto the
   shared `arbitration` helpers, then land the parity fixture (M21) and
   re-demonstrate M11.
3. Re-run the post-change campaigns after (1)/(2) and re-populate §8.

## 13. What was not measured

* live forced-search activation counts, cancellations, cap denials and
  trapped quits (the exception is not live-active);
* door/search/food attempt budgets at campaign scale (unit-tested only);
* live/replay parity and evaluator determinism under the new semantics;
* USD cost of the DeepSeek campaign (no tariff configured; `estimated_usd`
  is `0.0` and no cost is asserted).

No number in this report is a projection or an estimate.
