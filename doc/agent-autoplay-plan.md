# Autonomous Two-Tier Play Harness — Architect Handoff

Implementation handoff for the two-tier autonomous-play harness (`tools/agent/`,
`./agent.sh auto`). Produced by architect review of the agent interface. A coding
agent executes this; the operator has approved defaults and supplied provider
decisions (see "Operator decisions" at the end).

## Recommendation

Build a stdlib-only, user-facing `tools/agent/` package (proposed new directory),
launched by `./agent.sh auto`. Keep tests and bounded corpus fixtures in
`test/agent/`. One controller owns the game pipe, reconstructs public state,
fulfills transport obligations, validates decisions, and sends actions.
ScriptedReflex is always available; optional Jev selects typed reflex decisions.
DeepSeek runs asynchronously at explicit strategic boundaries and returns
constrained goals, never wire actions. Isolate blocking HTTP calls in bounded,
killable worker processes so urllib/DNS/provider outages cannot hold the wire
hostage.

## Verified evidence and corrections

- `doc/agent-llm-quickstart.md:14-42,112-122`: one episode per invocation, one
  outstanding need, invalid leaves it pending, bare closed terminates without an
  outcome classification.
- `doc/agent-interface.md:337-389`; `test/agent/driver.py:179-247`: current
  observations are complete `base:null` snapshots. Sparse omitted map cells become
  blank; do not accumulate them as deltas. Maintain exploration memory separately
  from current presentation.
- `doc/agent-interface.md:204,226,315-320`: cursor can be a target rather than the
  hero; inventory is not permanently published; coordinates are x=1..79, y=0..20.
- `doc/agent-interface.md:430-436`: keys are 1..255, but 128 rows is a PAGE limit,
  not a menu limit. A menu may contain 65,535 rows. Jev cannot represent arbitrary
  menus or arbitrary subsets as one <=255-way choice.
- `doc/agent-interface.md:510-568,728-795`: page delivery is per request, every
  required page must arrive, chunk acknowledgements are contiguous, row ids are
  scoped to menu generation and selection request, and commits are final sets —
  not accelerator bytes or toggles.
- `test/agent/driver.py:255-315,395-440`: Runner provides useful process/pipe
  patterns; Episode fetches pages and explicitly sends the subsequent action.
  Runner itself is not a production deadline supervisor.
- `test/agent/spectate.py:24-37,1313,1559-1565`: rendering is isolated from wire
  forwarding; transcripts are launcher-output bytes only. Actual env var is
  `SPECTATE_TRANSCRIPT`, not `SPECTATE_TRANSPORT`.
- `/tmp/nethack-spectate/ep-3.jsonl` exists. Lines 1-8 show hello, Samurai
  confirmation, intro acknowledgement, tutorial choice. Search found no act/invalid
  records and found closed at line 1446. Thus this corpus cannot supply exact
  historical actions without another log. Do not infer movement keys as ground
  truth from state changes.
- `agent.sh:93-104` already stages and spawns each episode with a private
  directory; lines 112-117 contain mode dispatch. `Files:588-590` explicitly
  describes test/agent as fixtures. `AGENTS.md:61-63` requires the shipped-file
  manifest to track additions.

## Scope and success criteria

Required now: autonomous scripted-only play; optional provider substitution; legal
bounded responses; per-episode memory and budgets; robust recording; observational
replay evaluation; reproducible failure tests. No engine-state access, new game
protocol, training pipeline, vector database, cross-episode learning service, or
ascent-performance guarantee. Better exploration is an objective, not an
established capability. "Completes episodes" means handles selection, play, all
nested prompts, disclosure, and closed; timeout/forced-abort episodes must be
reported separately rather than called natural completions.

## Layout and reuse

- `tools/agent/__init__.py`, `__main__.py`: CLI, campaign entrypoint.
- `tools/agent/protocol.py`: bounded pipe I/O, request state machine, page/chunk
  obligations, action validation, public snapshot model adapted from Client.
- `tools/agent/codec.py`: promote the reusable incremental assembly implementation
  from existing `test/agent/format_obs.py`; retain that existing path as a
  compatibility CLI/export wrapper. Preserve selftest behavior. Keep driver.Client
  as an independent snapshot oracle rather than making all tests share the same
  state implementation.
- `tools/agent/state.py`: episode memory, map rendering, visible feature
  extraction, inventory cache and freshness, boundary detector.
- `tools/agent/policy.py`: ScriptedReflex, prompt intent state, action candidate
  construction, directive execution.
- `tools/agent/providers.py`: common contracts, NullStrategy, Jev and DeepSeek
  adapters, bounded provider worker supervision. Split by provider later if
  unwieldy.
- `tools/agent/controller.py`: scheduling, fallback, escalation, budget accounting
  and campaign lifecycle.
- `tools/agent/recording.py`: raw inbound stream plus action/decision/usage
  sidecars.
- `tools/agent/evaluate.py`: offline replay and comparison CLI.
- `test/agent/test_auto.py`, `test/agent/test_auto_replay.py`,
  `test/agent/test_auto_providers.py`: unittest suites with fake wire/API workers
  and fake clock.
- `test/agent/fixtures/auto/`: 1-2 small representative recordings, action sidecars
  where genuinely available, provenance README and expected boundary annotations.
- `doc/agent-autoplay.md`: operational guide, policy limitations, budget/privacy
  descriptions.

Existing updates: `agent.sh` gains auto dispatch without changing play/watch/serve
behavior; `Files` gains shipped additions; root `README` gains a brief agent
entrypoint/link; quickstart links autoplay; test/agent README documents
regression/evaluation commands. Avoid runtime imports of the entire test driver.
Promoting only the decoder is a small justified extraction, checked against
existing callers, not a rewrite of spectate.

## Contracts and ownership

Use dataclasses, enums/Literal-style tags, and explicit runtime validators; type
annotations do not validate provider JSON.

```
Provider.available(config) -> Availability(enabled, reason)
ReflexProvider.decide(context: ReflexContext, deadline: float) -> ReflexResult
StrategyProvider.deliberate(context: StrategyContext, deadline: float) -> StrategyResult
ScriptedReflex.fallback(context: ReflexContext) -> ReflexResult
```

Availability checks are local configuration/capability checks, not a mandatory
network call before play. Workers are the only components that invoke blocking
provider methods. NullStrategy implements the strategy interface with a
disabled/no-op result.

ReflexContext: immutable public-state snapshot, current NeedKey=(episode,seq,id),
fully assembled required content, prompt intent, current directives, legal/candidate
action table, time budget. ReflexResult: typed action proposal, confidence or
unknown, provider/version, reason code, usage, latency. Providers never fill seq/id
or write stdin; controller envelopes the selected action against the current need.

StrategyContext: immutable snapshot summary, boundary ids/reasons, level and
directive generation, rendered 79x21 map with coordinate legend, displayed
status/conditions, de-duplicated recent messages, observed inventory with last-seen
time/staleness, short action history, current goals and remaining budget. No private
launcher diagnostics, hidden engine ids, environment, or secrets.

StrategyResult: validated DirectiveSet plus usage/latency. Example directive
fields: schema_version, ordered goals from {survive, acquire_food,
eat_known_safe_food, recover, explore_frontier, search_dead_ends,
descend_known_stairs, inspect_inventory, disengage}, optional observed target
coordinate, bounded risk preference, tick TTL, preconditions, short explanation. No
key, arbitrary command text, menu id, shell action, or executable content.
Inventory references are semantic observations, never retained menu row ids.

EpisodeContext is the sole mutable authority: current public snapshot, per-level
exploration memory, pending prompt intent, inventory cache, seen-event sets,
boundary queue, active directives, confidence history, cooldowns, circuit breakers,
call/token/USD ledger and recording offsets. Reset everything per episode; logging
may persist but does not implicitly alter the next policy.

## Wire and deadline design

1. Read bounded physical lines; validate hello/profile; assemble chunks and
   acknowledge chunk indices as received, before waiting for full logical records.
2. Atomically apply each snapshot. Record public events once by presentation event
   id. Register the one need.
3. Fetch all required menu/text pages with a bounded in-flight window (initially
   one). After final page, immediately resume deciding/sending for the SAME need;
   do not wait for another obs. ACK is deterministic after delivery and requires no
   paid API.
4. Compute a cheap ScriptedReflex fallback first. If Jev is enabled and useful,
   submit one immutable job and wait only within the remaining reflex allowance.
   Result must match NeedKey, pass action validation/safety checks, and meet
   configured confidence threshold. Otherwise send fallback. Ignore late results.
5. Controller sends exactly one selected gameplay action at a time. invalid is a
   retry state, not completion: preserve id, repair once where applicable
   (especially incomplete pages), then safe per-kind fallback. After a small total
   retry cap, cancel legally or terminate/report protocol failure; do not spin
   forever.
6. A new obs means advance state; fresh menu ids require fresh row mapping even
   when text is identical. A game message "You don't have that object" is distinct
   from wire invalid and needs a separate repeated-intent breaker.
7. On closed, cancel reflex work, drain bounded recording work, optionally request
   postmortem, then start another launcher. EOF without closed is transport
   failure, never a fabricated terminal record.

Proposed defaults: Jev decision allowance 0.75s, gameplay answer deadline 1s after
complete content, aggregate transport/content deadline 5s, DeepSeek wall deadline
20s. Make these configurable and prove them with monotonic-clock tests. Page
transfer is part of the overall outstanding-request bound; pathological maximum
content or broken transport may require abort rather than a legal ack. "Always
legally answer" cannot be guaranteed when the peer withholds required pages;
document bounded fallback for provider failures and bounded explicit failure for
impossible transport states.

Use one bounded API worker per enabled tier, not a growing thread pool. Parent owns
wall deadlines; worker uses urllib socket timeouts, response-size limits, no
retries inside the current reflex need. On timeout terminate/reap worker and apply
cooldown. This handles DNS hangs, slow-drip reads, and the fact that Future.cancel
does not stop blocking urllib calls. No HTTP worker inherits game pipe ownership.
Nonblocking/bounded stdin writes and stderr draining prevent pipe deadlocks. Normal
shutdown asks quit through supported prompts; watchdog kill is distinctly reported.

## ScriptedReflex

Port the play agent's inline policy if recoverable; otherwise reconstruct the
listed heuristics explicitly — do not claim the original implementation exists
in-tree. Establish selection and nested-prompt coverage before tuning navigation.

- Startup: configured role/default, visible start-game row, explicitly decline
  tutorial, safe bounded player name. Do not select the first row blindly.
- Navigation: Dijkstra over remembered, publicly observed terrain; costs for
  visits, recent loops, hazards, uncertain tiles and pets. Recompute from current
  observations; no omniscient map. Find known down-stairs, otherwise reachable
  frontiers, door exploration and bounded searching at plausible dead ends. Unknown
  blanks are not freely traversable floor. Track failed movement/search saturation
  so loop breaking makes progress instead of repeating forever.
- Safety: low-HP disengagement, avoid traps and apparently tame monsters, rest only
  when sufficiently safe and food permits. Public appearance is ambiguous; track
  uncertainty rather than assert exact monster identity. Identify hero location from
  appropriate command-context evidence, not every cursor position.
- Hunger: schedule inventory refresh and known-safe food intents early; bound
  retries and mark failed item intentions unusable until refreshed. Corpse safety is
  uncertain; do not assume all food safe or unseen quantities known.
- Multi-step intentions: command opens eat/pickup/door/look prompt; next answer is
  conditioned on that intent. Reset/revalidate on unexpected prompt or game
  rejection. Menus use current public rows and explicit final sets. line/extcmd use
  scripted bounded allowlisted text or cancel; Jev never generates these.
- Each known need has a total fallback path: ack after pages, menu safe
  selection/cancel, yn context-aware choice/cancel, line/extcmd cancel unless
  deliberate scripted intent, position supported cancel/key or verified coordinate,
  command safe short action. Escape is not universally equivalent to "no"; respect
  prompt defaults/native behavior.

Scripted confidence is a documented heuristic uncertainty score, not calibrated
probability. Separate structural validity from safety confidence.

## Jev integration

The supplied cardinality, price, latency and calibration figures remain vendor
claims, not verified API guarantees. Endpoint, authentication, typed-choice
request/response schema and usage fields were not supplied or externally verified.
Implement and test the adapter contract against a fake HTTP service first; enable
real Jev only after obtaining official early-access documentation (operator has
access but has not yet accepted the terms — JevReflex ships DISABLED). Missing
configuration or unavailable API means scripted play without startup failure.

Provide bounded controller-owned choice tables mapped to typed actions. Keys 1..255
fit exactly only without an additional cancel/abstain option; filter relevant
candidates or represent abstention outside that choice. Single-choice menus may use
a bounded shortlist or staged group/row choice after all pages are read. Arbitrary
multi-select menus require multiple bounded decisions or scripted selection;
recommend scripted initially. Position initially goes scripted or through bounded
targeting keys; row-then-column is optional later, with both calls charged and
combined deadline/confidence. Never truncate rows silently or conflate page index
with menu membership. Validate returned option index and finite [0,1] confidence;
malformed/unknown values fall back. Start at configurable threshold 0.8 as an
experiment, not an asserted calibration result.

## Strategy boundaries and escalation

BoundaryDetector is deterministic and separate from model calls. Generate stable
episode-local event ids:

- initial playable level and changed displayed dungeon-level;
- first observed appearance/class signature or explicitly described novel item/trap,
  with ambiguity noted;
- HP crisis threshold crossing, with hysteresis (e.g. enter <=30%, rearm >50%);
- hunger worsening into configured risk stages, not every snapshot retaining the
  same word;
- material inventory change/unknown equipment decision or failed food intent
  requiring planning;
- sustained low-confidence escalation;
- one closed/postmortem event.

Do not treat repeated full snapshots or history replay as new events. Coalesce
simultaneous reasons into one strategy request. Suggested normal cooldown: 50
displayed ticks AND 5 wall seconds; severe HP/hunger crossings may bypass normal
cooldown but remain cap-limited and subject to a short emergency wall cooldown.
Exact values are tunable policy constants.

Confidence order: Jev low/invalid/unavailable -> use scripted now -> optionally
enqueue a low-confidence boundary if scripted is also uncertain or disagreement
persists (e.g. three decision needs). Never await strategy to answer a crisis;
scripted safety acts immediately. At most one strategy call in flight and one
bounded coalesced pending boundary set. Late responses are not wire actions: accept
directives only if episode, level, preconditions and TTL remain compatible, then
activate at the next command boundary. Discard obsolete tactical advice. Avoid
global seq-equality rejection of strategies, since play continues while they
deliberate.

For exact-boundary tests distinguish detected, queued, dispatched, suppressed,
expired and applied events. Without coalescing/cap suppression, fake strategy
receives exactly the expected boundaries; under caps/cooldowns, expected dispatched
calls are a deterministic subset with logged reasons. Strategy must never be polled
every command.

DeepSeek uses urllib HTTPS and OpenAI-compatible chat completions at
`https://api.deepseek.com`, model `deepseek-v4.1-flash` (operator-specified;
configurable), with the API key from `DEEPSEEK_API_KEY` or `--deepseek-key-file`
(0600 file; the operator has staged the key at
`~/.config/nethack-agent/deepseek.key`). Confirm model id and usage schema against
current official documentation before real activation; no invented pricing or SDK
dependency. Use bounded non-streaming responses, strict JSON directive parsing,
conservative model output limits. Invalid JSON or unexpected fields discard the
plan. Treat game text as untrusted quoted data, not instructions.

## Configuration, budgets and security

Proposed CLI: `auto --episodes N --reflex scripted|jev --strategy off|deepseek
--role Samurai --max-ticks 2000 --episode-timeout 300 --output-dir DIR`, plus
answer/provider/content timeouts, confidence threshold, strategy-call cap,
token/USD caps, cooldowns, API model/base URL and price configuration
(`--deepseek-price-in` / `--deepseek-price-out` and the optional
`--deepseek-price-cache-hit`), and the bounded-continuity controls
`--deepseek-history-pairs` (0..64, default 8) and
`--deepseek-context-max-bytes` (default 262144). Default
scripted/off is completely network-free; presence of a key alone does not opt users
into paid calls. CLI overrides nonsecret env/default settings. Preserve AGENT_DATA
behavior.

Secrets only `DEEPSEEK_API_KEY` and proposed `JEV_API_KEY`; no CLI key flags;
`--deepseek-key-file` reads a 0600 file. Use explicit provider URL
allowlists/validation, HTTPS certificate verification, reject userinfo/query
credentials and cross-origin redirects. Strip credentials from the game subprocess
environment. Logging records allowlisted config fields and structured error
categories, never request headers, environment dumps, raw HTTP error bodies or API
exception strings that may echo secrets. Private 0700 output directories and 0600
files; include a warning that game names and text may themselves be private.

Per-episode counters: reflex attempted/successful/timed-out/invalid/
low-confidence/fallback; strategy boundary and dispatch counts; actual reported
tokens versus estimates; model, configured tariff and timestamp; estimated/reported
USD and unknown billing exposure. Reserve budget BEFORE dispatch, including failed
calls; do not refund timeout exposure merely because no usage was returned. Disable
additional paid calls if the remaining cap cannot conservatively cover one request.
Unknown price means no asserted hard USD enforcement; require tariff configuration
when a USD cap is requested. DeepSeek input/output/reasoning/cache usage needs
model-specific accounting: prompt-cache hit/miss tokens are reported separately
and only a complete consistent partition earns a discount, while every
reservation stays at the full input price. Default example strategy cap 8 total,
reserving one for
postmortem (7 during play); postmortem counts against the same cap and can be
skipped. Also bound paid reflex calls/tokens: a strategy-only cap does not bound
Jev spending.

## Recording and offline evaluation

Write `ep-N.wire.jsonl` as inbound physical bytes compatible with existing replay;
`ep-N.actions.jsonl` as outbound actions/auxiliaries with ordinal, preceding input
offset, NeedKey and send status; `ep-N.decisions.jsonl` for proposals, actual
selected provider, fallback reasons, boundaries, directives, latency and usage;
`ep-N.meta.json` for schema versions, safe configuration, completeness, fixture
provenance and hashes. Keep game outcome inferred from visible text separate from
controller stop reason. Merely seeing closed does not prove death or victory.

Recording must not block the wire indefinitely: bounded writer queue/helper,
preflight directory, finite shutdown timeout. Queue overflow/disk failure marks
recording incomplete, disables further paid work, and follows a documented
graceful-stop policy rather than silently dropping bytes and claiming a complete
transcript. No design can guarantee both durable full recording and uninterrupted
play under arbitrary disk failure; default to preserving request responsiveness and
explicitly marking failure.

Replay runs WITHOUT a game and WITHOUT network by default. Feed raw records through
the same assembly/state/boundary machinery, collect pages before invoking each
decision, and advance only along the recorded trajectory. Each candidate provider
gets its own episode memory and isolated budgets. At each need compare its candidate
to a known original action, if available. Canonicalize menu final sets and counts;
compare action semantics, not JSON serialization. Keep invalid attempts distinct
from inferred accepted progression. Provider decisions are never injected into the
old trajectory and their hypothetical effects are never claimed.

For old ep-*.jsonl, report `actual_action=unknown` and action-agreement coverage=0
where no sidecar exists. Compare legality, goal adherence, boundary detections,
prompt coverage and candidate disagreements; visible outcomes remain descriptive
observations, not counterfactual policy reward. Strategy comparison means directive
differences and reflex-with-directive proposals, not strategy keystrokes. Real API
evaluation requires explicit opt-in and separate evaluation budgets; canned provider
responses yield reproducible offline tests. Confidence agreement with an original
action is not evidence of safety calibration; label outcomes/safety separately and
report calibration only when suitable labeled data exists.

Commit 1-2 real compact transcript fixtures totaling <=256 KiB uncompressed, with
all chunks/pages and the required prefix for valid reconstruction, hash/provenance
and truncation declaration. An ep-3 startup prefix covers selection, page delivery,
ack and tutorial; a newly captured short food/menu or crisis sequence with outbound
actions supports actual agreement. Do not fabricate missing historical actions. Add
synthetic hostile/timing vectors in tests rather than committing multi-megabyte
ephemeral episodes.

## Phase-0 eat-menu investigation (blocks reliance on that path)

First locate ep-6 and any original outgoing-action log while /tmp survives. Preserve
a bounded relevant excerpt. Reconstruct its final prompt/page sequence in an
engine-free minimal client and verify current need id, menu id, row id, count,
completed page delivery and final-set serialization. This replay tests client
behavior but cannot reproduce native object identity: an inbound transcript is not a
restorable engine state, and row ids cannot be sent to an unrelated live episode.

Then run a fresh minimal live client: choose the recorded role/config, decline
tutorial, inspect publicly displayed inventory, issue eat, fetch every page, commit
exactly one current selectable known-food row with count -1, and record both
directions. Repeat through inventory reorder/splitting and a fresh menu generation.
Prefer a same-build deterministic test setup or genuine save artifact if available;
do not assume matching RNG. Distinguish wire invalid from an accepted commit
followed by the game message and a new need. Inspect the menu native-identifier
round trip in verified `win/agent/agent_menu.c`, its integration in
`win/agent/winagent.c`, and `src/eat.c` only after narrowing reproduction; add a
regression to existing `test/agent/test_menu.c` and a live client case as
appropriate. No specific C defect is yet established.

If confirmed, fix the adapter separately before food-dependent autonomous release. A
loop breaker is mandatory regardless: after two equivalent rejected eat intents,
refresh inventory/change plan rather than thousands of retries. This mitigates a
failure but is not a substitute for fixing a broken commit path.

## Ordered implementation and verification handoff

0. Preserve corpus excerpts and perform the eat-menu investigation. Exit criterion:
   fresh-row known-food commit succeeds, or a precise separately tracked adapter
   failure gates rollout.
1. Extract decoder compatibly, add protocol/snapshot/request controller and
   bidirectional recorder. Verify `python3 test/agent/format_obs.py --selftest`,
   `python3 test/agent/spectate.py selftest`, and `make -C test/agent check`
   against existing documented targets. Add tests for full-snapshot clearing,
   chunks, two-page menu and ack, same-id invalid recovery, fresh menu mapping,
   EOF/closed and incomplete recording.
2. Port/harden ScriptedReflex and startup/prompt intent handling. Add
   `python3 -m unittest discover -s test/agent -p 'test_auto*.py'`. Cover every
   need kind, unavailable food, repeated rejection, tutorial decline, search/move
   loops and per-episode resets.
3. Wire `agent.sh auto`, docs and manifest. After build/staging checks, run:
   `env -u DEEPSEEK_API_KEY -u JEV_API_KEY ./agent.sh auto --episodes 3 --reflex
   scripted --strategy off --max-ticks 2000 --episode-timeout 300 --output-dir
   /tmp/auto-smoke`
   Acceptance: three independently spawned episodes autonomously reach closed after
   death or an explicit tick-cap graceful quit, nested endgame requests fully
   handled, zero API invocations, complete recordings, no unreaped children. Count
   forced kills, EOF failures and unanswered-needs as failures, not successful
   completions. Gameplay depth/ticks/kills are reported separately from protocol
   completion.
4. Implement directive validation, fake strategy and event detector.
   `python3 -m unittest discover -s test/agent -p 'test_auto_providers.py'`:
   fixture expects exact invocation reasons/counts for initial/new level, first
   novelty, HP crossing, hunger escalation, inventory question, sustained low
   confidence and postmortem; repeated snapshots create zero additional events; test
   coalescing/cooldown/caps separately. Verify strategy never emits a wire action
   and stale level advice is discarded.
5. Implement worker-supervised DeepSeek, then optional Jev when official API
   details are available. Fake urllib endpoint tests: slow/hung/drip response, 401,
   429, 5xx, malformed JSON, oversized response, confidence NaN/out of range,
   outage halfway through play. Assert current requests meet deadlines using
   scripted fallback, call reservations stay within caps, bounded workers are reaped
   and secrets are absent from artifacts. Live API smoke tests are opt-in only.
6. Add offline evaluator and two bounded fixtures. Proposed command:
   `python3 -m tools.agent.evaluate test/agent/fixtures/auto/startup.wire.jsonl
   --reflex scripted --strategy off --output /tmp/auto-eval.jsonl`
   Run twice and compare deterministic decision fields, excluding wall timings. For
   a fixture with sidecar, assert exact known-action coverage and canonical
   agreement; for legacy fixture, assert unknown labels rather than guessed actions.
   Verify all default evaluation paths make no network calls.
7. Run longer zero-key and optional paid campaigns, compare protocol health,
   exploration/depth, hunger deaths, repetition rate and provider costs. This is
   where improved play can be demonstrated; offline trajectory comparison alone
   cannot establish superiority.

## Alternatives, risks and remaining decisions

Rejected: putting the whole user-facing harness into test/agent (confuses tooling
with fixture ownership); importing the entire driver as runtime (test assumptions
and synchronous lifecycle); synchronous DeepSeek in the action path (latency
stalls); uncancellable executor threads (hung-call accumulation); LLM keystrokes
(violates settled tier separation); inferring original actions from output-only
episodes (invalid evaluation ground truth).

Principal risks: absent Jev API access/contract, unsupported multi-select
cardinality, unvalidated confidence and prices; DeepSeek latency/outdated advice;
cursor and symbol ambiguity; no reliable passive inventory; starvation and
secret-door exploration remain difficult; suspected native eat mapping; disk/
transport failures that cannot satisfy a literal always-answer guarantee. Rollback
is simple: `--reflex scripted --strategy off`; preserve existing serve/play/watch
and keep any adapter fix separately reviewable.

## Operator decisions (resolved)

1. DeepSeek: model `deepseek-v4.1-flash`; key staged at
   `~/.config/nethack-agent/deepseek.key` (0600, outside the repo, extracted from
   the operator's polytoken config; never logged or committed). DeepSeek approval
   to transmit public game state: granted.
2. Jev: operator has early access but has not accepted terms — JevReflex ships
   DISABLED, fake-endpoint-tested only.
3. Defaults approved: 8 strategy calls/episode (1 reserved postmortem), tick-cap
   graceful quit, boundary cooldowns 50 ticks + 5 s wall.
4. `AGENTS.md` committed.
