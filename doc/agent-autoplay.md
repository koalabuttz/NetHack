# Autonomous agent play (`./agent.sh auto`)

The two-tier autonomous-play harness.  It drives the headless agent wire
(`doc/agent-interface.md`) itself: one bounded controller owns the game pipe,
reconstructs public state, fulfils every transport obligation, and sends one
validated action per request.  Nothing here reads engine state, invents a
protocol, or (by default) touches the network.

Two tiers, from the handoff (`doc/agent-autoplay-plan.md`):

  * **reflex** -- answers every request, in the action path.  ScriptedReflex
    always ships and is always available; an optional Jev tier can replace it
    with a typed-choice decision.  Paid reflex work is opt-in.
  * **strategy** -- deliberates at *strategy boundaries* and returns
    constrained **goals**, never wire actions.  Optional DeepSeek, run in a
    killable worker process so a hung provider cannot hold the wire.

## Quick start

```sh
# three independent scripted episodes; recordings under the output dir
./agent.sh auto --episodes 3 --reflex scripted --strategy off \
    --max-ticks 2000 --episode-timeout 300 --output-dir /tmp/auto-smoke
```

The launcher, worker and staged data come from the same `agent.sh` front end
as `serve`/`play`/`watch`; `$AGENT_DATA` (default `/tmp/nethack-agent-data`)
is honoured.  Running the module directly is equivalent:

```sh
python3 -m tools.agent auto --episodes 1 --output-dir /tmp/auto
```

The optional strategy tier is opt-in **and** needs a key; a key in the
environment alone never starts paid calls:

```sh
./agent.sh auto --episodes 1 --reflex scripted --strategy deepseek \
    --deepseek-key-file ~/.config/nethack-agent/deepseek.key \
    --strategy-call-cap 8 --output-dir /tmp/auto-ds --spectate tty
```

Add `--spectate tty` (or `stderr`) to watch a run live; see "Live
spectating" below.

A recording can be replayed offline -- no game, no network -- and compared to
its own actions with `python3 -m tools.agent.evaluate`; see "Offline
evaluation" below.

## Command line

`--episodes N` (default 1)
    how many independent episodes to run.  Must be a positive integer; a
    zero, negative or non-integer count is rejected rather than silently
    running nothing.

`--reflex scripted|jev` (default scripted)
    the reflex tier.  `scripted` is always available; `jev` requires
    `JEV_API_KEY` and `--i-accept-jev-terms`, and posts to the official
    `api.typesafe.ai` endpoint unless `--jev-base-url` overrides it (see
    "Providers").  Any other value is rejected.

`--strategy off|deepseek` (default off)
    the strategy tier.  `off` is completely network-free.  Any other value is
    rejected.

`--role ROLE` (default Valkyrie)
    the role the reflex selects at startup.

`--max-ticks N` (default 2000)
    graceful-quit budget, counted in gameplay commands.

`--episode-timeout S` (default 300)
    wall-clock budget per episode.

`--spectate [tty|stderr|none]` (default none; bare flag means stderr)
    render the live presentation to a side channel while the episode runs.
    `none` opens nothing at all.  Frames never go to fd 1 (the wire) or into
    a recording; see "Live spectating".

`--spectate-interval S` (default 0.15)
    the minimum time between rendered frames; `0` is unthrottled.  A
    negative, NaN or infinite value is rejected before any episode starts.

### Deadlines

`--answer-deadline S` (default 1)
    how long the controller may take to write one outbound line once a
    request's content is complete; a full stdin pipe is bounded by this
    rather than blocking.

`--content-deadline S` (default 5)
    the aggregate content/transport deadline for one outstanding request;
    exceeding it aborts the episode (`stop=content-deadline`).

`--reflex-deadline S` (default 0.75)
    the absolute reflex decision allowance.  The deadline is passed to the
    provider and the call runs on a bounded thread, so a provider that
    overruns is abandoned and the scripted fallback answers.

`--strategy-deadline S` (default 20)
    the wall deadline for one strategy call.  It is a **process** deadline:
    the worker is killed (TERM -> KILL) when it expires.

`--strategy-cooldown S` (default 2)
    a pause after a strategy timeout before another call may start.

### Reflex confidence

`--confidence-threshold C` (default 0.8)
    the minimum confidence for a *paid* reflex answer to be accepted **in
    absolute mode only**; a paid answer below it falls back to scripted play.
    Valid range `0..1`.  It applies to the calibrated paid reflex confidence
    only -- the scripted heuristic score is never compared to it, so an
    ordinary scripted decision is not treated as uncertain.  Changing it alone
    no longer tunes the default Jev acceptance rule (see below).

`--jev-confidence-mode {relative,absolute}` (default `relative`)
    the Jev acceptance policy.  `relative` accepts on **concentration**: the
    selected option's *own validated probability* must strictly exceed
    `--jev-relative-factor / N`, where `N` is the number of retained offered
    options.  The service's separate `confidence` scalar is never consulted in
    this mode, and a missing or malformed selected probability fails closed.
    `absolute` is the explicit rollback to the legacy flat
    `--confidence-threshold` comparison.  The two modes are never combined.

`--jev-relative-factor K` (default 1.5)
    the relative multiplier, strictly between 1 and 2.  Above 1 so acceptance
    requires a genuinely non-uniform distribution; below 2 so a two-option
    choice stays attainable (`p > 1.5/2 = 0.75`).  Thresholds: `N=2: >0.75`,
    `N=3: >0.5`, `N=4: >0.375`, `N=5: >0.3`, `N=6: >0.25`.  An exactly
    uniform distribution still abstains, and the comparison is strict
    (`p > k/N`, never `>=`).  Concentration is **not permission**: every
    identity, membership, rejection and eligibility gate still applies, and a
    passing concentration test never bypasses safety.

`--reflex-call-cap N` (default 0 = disabled)
    the **applied-decision** limit for the paid Jev reflex: at most `N`
    accepted Jev proposals may be **completely sent** (locally valid, not
    replaced by a fallback or a forced-search override, and written to the
    wire).  Rejected, skipped, abstained, timed-out or failed-send
    consultations do **not** spend the applied allowance, but they still cost
    money -- there is deliberately no rejection-attempt cap.  The reservation
    diagnostic `reflex.paid_dispatched` (consultations *reserved*) is retained
    for compatibility and may exceed the applied count; the applied count is
    reported as `reflex.applied` in the ledger and the episode/campaign
    summaries.  A delivery repair (`invalid` with `incomplete`) resends the
    frozen validated action without re-consulting Jev and is charged once.

**Anti-oscillation navigation.**  The scripted reflex owns a movement-history
state machine over *confirmed* observations only (a proposal, a rejected
candidate or a failed write never advances it): the current cell, the last
distinct previous cell, a deduplicated trailing window and an active-cycle
flag.  Ordinary navigation applies a bounded **same-family** preference against
an immediate reversal -- a reversing target is suppressed only when a
comparable non-reversing target exists in the same family within a strict
`40`-point margin (the `+30` directive contribution participates), rejected
alternatives are ignored, and a dead end whose only exit is backtracking keeps
it.  A detected period-2/period-3 cycle independently enters a safe recovery
step over edge-legal neighbours (door/corner legality respected), preferring a
non-reversing exit; when no legal movement exists it uses the existing bounded
search-fallback/forced-search nomination machinery.  Emergency disengagement
is untouched and may reverse.

**Room awareness.**  The Jev `state` payload carries a `room` object
(`scope`, `contents`, `contents_omitted`, `openings`, `openings_omitted`) and
an enriched map whose foreground markers are canonical: `*` creature
appearance, `&` item appearance, `?` unclassified display, `@` the confirmed
hero (final precedence).  Raw glyphs never leak; the fixed `legend` covers
every emittable glyph and the map carries no colour.  `room.contents` (cap 16)
lists current-screen creature/item/unclassified appearances and classified
features nearest-first; `room.openings` (cap 12) lists corridor/door/stair
landmarks with an exact `source` of `screen` or `memory` and makes **no**
reachability or visibility claim.  Movement criteria add at most one
destination-appearance clause from the *actual adjacent* square.  All helpers
are pure: rendering mutates no terrain, occupancy, memory or snapshot, and
missing evidence degrades rather than refusing the request.  The presentation
version is bumped to `jev-presentation/2`.

### Strategy budget

`--strategy-call-cap N` (default 8)
    strategy calls per episode.  A call is charged **before** it is
    dispatched, so a timeout that returns no usage is still billed.

`--postmortem-reserve N` (default 1)
    calls held back for one postmortem; with the default cap this leaves 7
    spendable while playing.  `0` disables the postmortem entirely.

`--token-cap N`, `--usd-cap USD`
    optional ceilings.  A USD cap is only enforceable when a tariff is
    configured (`--deepseek-price-in` / `--deepseek-price-out`, USD per
    million tokens), and the CLI requires the **complete** tariff (both
    prices) when `--usd-cap` is given rather than silently ignoring the cap.
    The optional `--deepseek-price-cache-hit` (USD per million prompt
    cache-hit tokens) only lowers the *reported* cost of a cache hit; it never
    completes a tariff and is rejected when it exceeds `--deepseek-price-in`,
    because the full input price is the conservative fallback every
    reservation relies on.  No price is invented: with no tariff the estimate
    stays zero and the unknown-price exposure is counted instead.  A
    **partial** tariff (only one of the in/out prices) is usable but
    explicitly **incomplete**: it never enforces a USD cap, its serialized
    form carries `complete: false`, and a reported usage component the tariff
    does not price is counted as unknown-price rather than billed at a
    fabricated zero.  Every cap is a *conservative ceiling*: a call is
    admitted only when the estimated
    prompt plus the configured maximum output fits in what is left, and a call
    that returns no usage keeps that exposure recorded (never silently
    dropped).

`--reflex-call-cap N` (default 0)
    a separate bound on **paid reflex** (Jev) calls; `0` disables Jev work.

### Boundaries

`--boundary-cooldown-ticks N` (default 50) and
`--boundary-cooldown-wall S` (default 5)
    the normal dispatch cooldown: both must elapse before another normal
    boundary is sent to the strategy.

`--boundary-emergency-wall S` (default 2)
    a severe crossing (HP crisis, hunger at Fainting or worse) may bypass the
    normal cooldown but not this short wall cooldown, and never the cap.

`--low-confidence-needs N` (default 3)
    how many consecutive uncertain reflex decisions escalate to a
    low-confidence boundary.

### Providers and paths

`--deepseek-model` (default `deepseek-v4-flash`)
    the chat model id.  The documented ids are `deepseek-v4-flash` and
    `deepseek-v4-pro`; the handoff's `deepseek-v4.1-flash` is not published,
    so the default follows `api-docs.deepseek.com/api/list-models`.

`--deepseek-base-url` (default `https://api.deepseek.com`)
`--deepseek-key-file PATH`
    a 0600 file holding the key.  Otherwise `DEEPSEEK_API_KEY` is used.
    There is deliberately no `--deepseek-key` flag.

`--deepseek-max-tokens N` (default 4096)
    a bounded output budget; responses are non-streaming and size-capped.
    `deepseek-v4-flash` is a **reasoning** model, so a budget smaller than its
    reasoning tokens returns empty content (an unusable response); the
    reasoning length is variable, so the default leaves headroom for the JSON
    plan after it.

`--deepseek-history-pairs N` (default 8; range 0..64)
    how many committed user/assistant exchanges the harness retains as
    episode-local conversation continuity for the cache-aware prefix.  `0` is
    the explicit rollback to stateless requests.  See
    "Prompt-cache utilization" below.

`--deepseek-context-max-bytes N` (default 262144)
    a harness **payload-safety ceiling** for one prepared request, measured
    against the UTF-8 JSON serialization of the whole API payload.  It is not
    a claim about a model's context window; an irreducible oversize request
    is refused locally (`strategy-context-too-large`) without spawning a
    worker.

`--jev-key-file`, `--jev-base-url`, `--i-accept-jev-terms`
    the Jev controls; a key and the terms acknowledgement are needed before
    `--reflex jev` will start, and `--jev-base-url` overrides the official
    endpoint.

`--output-dir DIR`
    where the `ep-N.*` recordings are written.

`--worker`, `--runner`, `--data`, `--sysconf`
    paths to the game, the launcher and the staged data (derived by
    `agent.sh`; `--sysconf` defaults to `<data>/sysconf`).

## What "completes an episode" means

An episode is a success only when the wire reports `closed` after the
controller has answered every outstanding request: startup selection, the
tutorial prompt, ordinary play, and every nested end-game prompt, the
launcher exits **zero**, its process group is reaped, and the recording is
complete.  Reported separately, never as success:

  * **tick-cap graceful quit** — the reflex asked to quit through the native
    `#quit` path once `--max-ticks` was reached
    (`stop=tick-cap-graceful-quit`);
  * **EOF without `closed`** — a transport failure
    (`stop=transport-failure-eof`), never a fabricated terminal record;
  * **closure without completion** — `closed` arrived while a request was
    still unanswered (`stop=closed-unanswered`), a failed outbound write
    (`stop=transport-failure-write`), a nonzero launcher exit, or a process
    group that could not be reaped (`teardown_failure`).  Closure is
    best-effort (`sys/unix/agent_runner.c`), so it is not by itself proof
    that a request was answered;
  * **episode timeout / forced kill**, and **protocol failure** (a request
    rejected beyond the retry cap, or a bounded session-validation failure).

The harness also reports the *game outcome* inferred from visible text
(`death`, `starvation`, `ascension`, ...) **separately** from its own stop
reason: seeing `closed` alone does not prove death or victory.

## Wire guarantees

  * One outstanding request at a time; every request is answered once.
  * All required pages are fetched (bounded, one in flight) before any menu
    or acknowledgement is sent; a repeated `get_page` is a retry.  Only the
    exact outstanding page, with a matching declared page count and content,
    is accepted.
  * Chunk streams are assembled strictly and acknowledged cumulatively as
    they arrive.
  * `invalid` is a **retry state**: the same request id is re-answered,
    repaired once where applicable, then answered with a per-kind safe
    fallback, and the episode stops after a small total retry cap.  Retries
    consume the *same* aggregate content budget as the original need.
  * A malformed record ends **that** episode as a protocol failure; the
    campaign continues with the next episode.  A need is validated in full
    before any of it is stored.
  * The controller's fallback for a gameplay command is never a rest: it
    carries no snapshot context, so it cannot prove a rest safe, and it
    searches instead.
  * A game message such as `You don't have that object.` is distinct from
    wire `invalid`; the reflex carries a separate repeated-food breaker.
  * **The strategy tier is never in the action path.**  It runs on a bounded
    background call; the reflex keeps answering throughout.  A strategy
    result is only ever a set of goals, and it is applied at the next command
    boundary -- never awaited to answer a crisis.

## ScriptedReflex policy

The always-available reflex.  It is deliberately conservative and its
limitations are real (better exploration is an objective, not an established
capability):

  * **startup** — declines auto-pick, selects the configured role, takes the
    visible start-game row, and explicitly declines the tutorial;
  * **navigation** — Dijkstra over *remembered, publicly observed* terrain
    with a visit-count penalty, preferring known down-stairs, then reachable
    frontiers, with bounded searching; unknown blanks are not floor;
  * **safety** — never walks into a monster: public appearance is ambiguous,
    so a monster is never assumed tame and is never stepped into.  Visible
    traps, boulders and the punctuation monster classes (`'`, `&`, `;`, `:`,
    `~`, `]`) are avoided too, as is an `@` that is not on the hero's own
    known square;
  * **hunger** — answers the engine's `getobj` eat prompt with a valid
    inventory letter parsed out of the prompt text, or opens the inventory
    menu, and after two equivalent rejected intents refreshes and changes
    plan.  Only an allowlist of known-safe items is edible, keyed to the
    engine's exact canonical object names and their exact plurals;
  * **loop breakers** — a move that never changes the hero's square
    escalates through search, a random move, then an unblock/rest step.

Scripted confidence is a documented heuristic uncertainty score, not a
calibrated probability.

### Known engine-side eat behaviour (Phase-0 finding)

The engine's `getobj` prompt for `eat` (`src/invent.c`) is delivered as an
ordinary `yn` request whose `choices` field is `null`: the valid inventory
letters appear only inside the prompt text (`What do you want to eat?
[d or ?*]`).  Answering it with any byte that is not a carried object is
*accepted by the wire* (no `invalid`) but the engine replies `You don't have
that object.` and re-issues the prompt with a fresh request id.  A client
that answers from `choices`/`default` alone therefore loops forever; the
reflex parses the bracketed letters instead and, after two equivalent
rejected food intents, opens the inventory menu and refreshes.

## Strategy tier: boundaries and directives

A **boundary** is a deterministic, episode-local event that a strategy might
need to know about.  Detection runs once per applied snapshot and is
separate from any model call.  Each event has a stable id and is emitted
**once** -- re-presenting the same snapshot, or replaying history, produces
no new events.

Detected kinds:

  * `initial-level` / `level-change` -- the displayed dungeon level;
  * `novelty-class` -- a first-seen monster-class appearance signature
    (identity stays ambiguous, and the boundary says so);
  * `novelty-item` -- an explicitly described new item or trap, e.g.
    "You see here a ...";
  * `hp-crisis` -- an HP crossing with hysteresis (enter at <= 30%, rearm
    above 50%);
  * `hunger-<stage>` -- a *worsening* hunger stage, so a repeated snapshot is
    not an event;
  * `inventory-change` -- the material inventory list changed;
  * `food-intent-failed` -- two rejected food intents need a new plan;
  * `low-confidence` -- sustained reflex uncertainty across N decisions;
  * `closed` -- one postmortem event.

Simultaneous reasons **coalesce** into a single request.  An event moves
through observable states -- `detected`, `queued`, `dispatched`,
`suppressed`, `expired`, `applied` -- all counted in the budget ledger, all
visible in the decisions sidecar and the episode meta, and each event's full
lifecycle persisted in the `ep-N.events.jsonl` ledger.

At most **one** strategy call is in flight, with at most one coalesced
pending set.  Late responses are not wire actions: a returned directive set
is activated only at the next **command** boundary, and is discarded as
stale if the displayed level changed since the call was dispatched, if its
tick TTL ran out, or if a precondition no longer holds.

Every started strategy operation settles exactly once.  A call that may have
reached the wire is committed as **dispatched** -- with its reported usage if
it returned one, and as counted unknown exposure if it did not; an ambiguous
missing result after the thread started is *never* treated as proof that no
dispatch happened. A result that proves a **known local refusal** -- a missing
key, a cooldown, a spawn failure or an oversize payload -- never crossed the
dispatch boundary and is *released* rather than booked as phantom exposure,
matching the postmortem's treatment.  Either way the boundary set is
terminated so every dispatched event has exactly one terminal state, and a
result that completed in the cancellation race is preserved rather than
dropped.  A successful validated result is also the only thing that extends
the episode's conversation (see "Prompt-cache utilization"): a failed,
cancelled or refused call appends nothing.

A postmortem is requested only after a **clean** closure: a validated
`closed` with no request left unanswered, a healthy recording and a
postmortem slot still reserved.  An EOF, a protocol failure, an episode or
content deadline, and a `closed` that arrived with a request still
unanswered are all ineligible -- none of them is a completion to reflect on.

The postmortem runs through a **fresh provider lifecycle**.  The gameplay
provider is cancelled with the episode (and that cancellation is sticky), so
the reserved call would never reach the provider if it reused it; a new
provider instance, with its own bounded worker and deadline, is built for the
postmortem instead.  It is booked as *dispatched* only once work actually
crossed the dispatch boundary -- a call refused before any worker existed
(no key, a cooldown, a spawn failure) is released without booking a
dispatched call or any billing exposure, and a call that reached the wire but
returned no usage keeps its conservative bound as *unknown exposure* rather
than being dropped.

A directive set is a small closed-world object (`schema_version`, ordered
`goals`, an optional observed `target`, a bounded `risk`, a `ttl`,
`preconditions`, a short `explanation`).  Unknown fields, unknown goals,
key-like or text-like fields, out-of-range coordinates or risk, and a `NaN`
risk are all rejected whole -- there is no partial application.  The reflex
consumes goals as *biases only*: `disengage`/`survive` raise the flee
threshold, `acquire_food`/`eat_known_safe_food` inspect the inventory
sooner, and `explore_frontier`/`search_dead_ends` reorder navigation targets.
A directive can never contribute an action of its own.

## Budgets and cost

Every episode carries a ledger in `ep-N.meta.json`:

  * reflex: attempted / successful / timeout / invalid / low-confidence /
    fallback / paid-dispatched;
  * strategy: boundaries detected / queued / dispatched / suppressed /
    expired / applied, plus calls dispatched and the postmortem count;
  * usage: prompt and completion tokens as actually reported, an estimated
    USD figure **only** where an operator tariff is configured, and a count
    of unknown-price calls otherwise.  When the provider reports a
    prompt-cache partition, `cache_hit_tokens`, `cache_miss_tokens`,
    `cache_unclassified_tokens` and `cache_hit_rate` are also carried, plus
    `reasoning_tokens` as a diagnostic subset of the completion tokens
    (never billed twice).

The cap is authoritative: a call is reserved before dispatch, failed calls
are not refunded, and once the remaining cap cannot conservatively cover one
more call, paid dispatch is disabled for the rest of the episode.

A **recorder failure** (full queue or disk error) triggers the graceful-stop
policy: paid dispatch stops immediately, any in-flight worker is cancelled,
and scripted play continues so the request obligation is still met -- the
recording is marked incomplete rather than silently truncated.  Recorder
health is re-checked after every write, including the event-ledger sink, so a
failure first seen while persisting a lifecycle record disables paid dispatch
before the next decision is made.

## Providers

`--reflex scripted --strategy off` is completely network-free by
construction: no code path opens a socket.

**ScriptedReflexProvider** wraps the policy and is always available.
**DeepSeekStrategy** speaks urllib HTTPS to an OpenAI-compatible chat
completions endpoint, but never in-process: each call runs in a separate
**worker process** (`tools/agent/worker.py`, gated by a private invocation
marker) so a DNS hang or a slow-drip read cannot wedge the wire.  The parent
owns the wall deadline, escalates TERM -> KILL, reaps the process, and
applies a cooldown after a timeout.  The worker enforces a response-size
limit, a socket timeout, an HTTPS-only URL allowlist (loopback HTTP only for
the fake-endpoint tests), rejects userinfo/query credentials and refuses a
cross-origin redirect.  A malformed, oversized or schema-invalid response
discards the plan; the controller's scripted directives continue.

**JevReflex** implements the typed-choice adapter contract -- a bounded
choice table, a finite `[0,1]` confidence check, and fallback on any
malformed or unknown value.  It posts the documented typesafe.ai body to
`(--jev-base-url or the official https://api.typesafe.ai/v1) + /systemone`:
a top-level `state` (the compact remembered-state payload), `model`
(`"jev-latest"`), and a `questions.action` object carrying the fixed
`instructions`, `type` (`"choice"`) and a `criteria` JSON object mapping each
**semantic kebab-case option key** to a grounded criterion sentence.  The
answer is read from `answers.action`: `choice` names one offered key,
`probabilities` is the full distribution over exactly those keys, and
`confidence` is the score -- the selected key's probability stands in when
the body carries none.  Nothing is sent to Jev for `position`, `line` or
`extcmd`, and menus, yes/no prompts and acknowledgements stay wholly on the
scripted tier.

### Jev presentation contract

The presentation layer (`tools/agent/presentation.py`) names and describes
candidates the policy has already built; it never changes *what* candidates
exist.  The retained-table `key_index` -> original candidate mapping, the
candidate semantic labels/actions/ordering, `candidate_id`/`table_id`
identity and every safety/dispatch gate are unchanged, and `policy.py` is
untouched.

* **Option keys** are derived from `(need, frozen candidate)` and are
  need-aware: a `command`-need movement action is `navigate-<compass>`, a
  `direction` need is `direction-<compass>` (never `navigate-*`), and a `key`
  need is `key-<semantic>`.  The closed normalization is ASCII-only
  lowercase with runs of non-alphanumerics collapsed to `-`; an empty,
  non-ASCII or already `--`-bearing label is rejected rather than lossily
  aliased.  A closed alias table maps the policy's own labels
  (`eat` -> `eat-food`, `search`/`search-secret` -> `search-in-place`,
  `descend` -> `descend-stairs`, `inspect-inventory`/`refresh-inventory`
  -> `inventory`, `rest` -> `wait`).  Collisions take `--1`, `--2`, ... in
  retained-table order; the double hyphen is reserved for that suffix, and
  the emitted key count is asserted to equal the retained-table length and
  to stay within the 255-way bound before dispatch.  `key_index` is
  authoritative -- a returned key is never parsed back into an action.
* **Criteria** are per-family grounded templates.  Remembered terrain comes
  only from the runner-owned persistent classified `TerrainMemory`, so
  ground under a current occupant survives; current occupancy comes only from
  the current snapshot and never infers hostility from a glyph; a
  direction/key need is never described as walking or opening; an `eat`
  command is never bound to an item without an exact frozen binding; quit is
  conspicuous; and an unrecognized policy reason omits its purpose clause
  rather than dumping raw metadata.  The canonical action JSON and heuristic
  scores are no longer model-facing.
* **Refusals** are fixed, nonsecret codes recorded in the decision sidecar
  *before* any reservation, with zero dispatch and zero endpoint requests:
  `unsupported-need`, `singleton`, `invalid-label`, `unsupported-semantic`,
  `missing-required-binding`.  A refusal always refuses the **whole**
  request -- candidates are never selectively dropped.  Conservative
  degradation is not a refusal: absent optional evidence (terrain
  classification, route purpose, occupant data) simply uses the shorter
  template.  A frozen table above the 255-key wire bound is the same kind of
  unrepresentable table shape and refuses as `unsupported-semantic` rather
  than adding a code outside the closed vocabulary.
* **State payload** is one JSON object with `game`, `objective`, a fully
  inlined `legend`, `status` (`hp`, `hp_max`, `hunger`, `dungeon_level`,
  `experience_level`, `conditions`), `hero` (the controller-resolved square
  only), `inventory` (`items`/`cached`/`age_turns`/`truncated`),
  deterministic `directives` summaries, `intent`, `messages`, `need`, a
  bounded remembered `map` and any remembered `stairs`.  Required fields are
  present with `null` when unavailable; a list is `null` when unavailable and
  `[]` only when genuinely empty.  The map's terrain glyphs come from the
  persistent classified terrain, occupancy only from the current snapshot
  (every observed non-hero creature collapses to one canonical `*` marker),
  and the confirmed hero renders `@` with final precedence; the crop is
  clamped to the protocol rectangle and is `null` with no evidence.
* **No synthetic option** is ever added to a Choice table: the retained set
  is the complete candidate set and a synthetic member would have no retained
  index.  Coverage is handled by the refusal codes plus the existing scripted
  fallback paths.

The wire contract is the official one: `criteria` is a JSON object whose
member order is the retained-table order (the worker serializes with
insertion-order-preserving JSON), and the response parser keeps requiring an
explicit `answers.action.type == "choice"`.  The local snapshot test
`TestJevWireContract` pins the exact serialized request bytes against
`test/agent/fixtures/jev_golden_request.json`; that catches **local
serializer drift only** -- an upstream official-contract change is caught
only by a manual documentation review at each TypeSafe release.

### Jev confidence

`confidence` is a distribution-derived score: the documented
`answers.action.confidence` when present, else the selected key's own
probability.  It measures **concentration, not correctness**, and it is
**not permission to act**: several legitimately acceptable navigation
alternatives can spread probability, so an honest multi-alternative table
looks low-confidence.  The safety and eligibility gates remain authoritative
independently of it.  Any threshold change or bypass is an operator decision
backed by measured data.  Distribution-shape measurement across
multi-alternative navigation tables is a **live-only, operator-gated** metric
and is never fabricated offline.

Since the approved follow-up (`doc/agent-jev-gate-nav-plan.md`, revision 4)
the *default* acceptance rule is **peakedness-relative**: the selected
option's own validated probability must strictly exceed
`--jev-relative-factor / N` (default `1.5/N`), and the service `confidence`
scalar is no longer consulted in that mode.  The legacy flat
`--confidence-threshold` gate is preserved as the explicit `--jev-confidence-mode
absolute` rollback.  This is a policy heuristic, not a calibrated risk
probability, and it changes only *which* legitimate choice is taken -- never
whether a choice is safe.  See "Reflex confidence" above for the exact
boundaries and the applied-decision cap.

### Alternating-motion measurement

`longest_loop_span` and `loop_spans_ge_2` measure runs of identical
`(hero, displayed time)` frames -- **stationary** duplicates only.  They do
not measure movement alternation, so they cannot prove the anti-oscillation
fix succeeded or failed.  The validation report therefore derives a separate,
report-local alternating-move figure from the confirmed hero positions
(consecutive duplicates removed, then consecutive `A-B-A`/`B-A-B`
confirmations), scoped to the level instance; `test/agent/test_auto_metrics.py`
computes it without any production metrics-schema migration.  Decisions that
merely *offer* a reversal are not counted: only a committed, confirmed move
advances the movement history.

### Offline measurement

`test/agent/jev_offline_report.py` compares the pre-migration baseline corpus
against the migrated presentation with **no network**:

```sh
python3 test/agent/jev_offline_report.py \
    --fixtures test/agent/fixtures/jev_legacy_requests \
    --out test/agent/fixtures/jev_offline_report.json
```

The committed `test/agent/fixtures/jev_offline_report.json` is deterministic
and byte-for-byte reproducible (there is deliberately **no wall-clock field**
and no latency, token, cost, distribution or survival figure).  The report's
top-level field set is exactly `schema_version`, `per_request` and `summary`.
The same command also writes one **paired** artifact per fixture under
`test/agent/fixtures/jev_legacy_requests/paired/`: the exact new
semantic-wire body the fixture re-renders to, or a **zero-byte** file when the
renderer refuses it (a refusal carries no wire body; its code lives in the
report).  Serialized
byte counts are reported as *bytes* and are never converted to tokens: there
is no Jev tokenizer in this repository, and the documented 32,000-token Jev
context window is quoted, not claimed as a measured fit.  Real token counts,
latency, billed cost, distribution shapes and gameplay progress/survival are
**live-only, operator-gated** metrics; an optional live A/B smoke is manual
and reports its own limitations.

Secrets come only from `DEEPSEEK_API_KEY` / `JEV_API_KEY` or a **0600 key
file** (`--jev-key-file PATH`); the repository has no default key path (an
operator may keep one at `~/.config/nethack-agent/jev.key` as a local
convention).  The key never appears in a log line, a recording, a structured
error or an exception message.  It is stripped from the game child's
environment by construction, and the provider **worker** likewise inherits
only a minimal runtime environment (PATH and locale): the selected key
travels to the worker *only* in the stdin job, never in an environment
variable and never in argv.  Logging records allowlisted configuration and
error *categories*, never request headers or provider bodies.

## Recordings

Each episode writes five sidecars under `--output-dir`:

  * `ep-N.wire.jsonl` — the inbound physical bytes, verbatim, in order;
  * `ep-N.actions.jsonl` — outbound actions/auxiliaries with their ordinal,
    the preceding input offset, the `NeedKey` and the send status;
  * `ep-N.decisions.jsonl` — proposals, the selected provider, fallback
    reasons, the *dispatched* boundary ids, the complete validated directive
    set, latency and usage;
  * `ep-N.events.jsonl` — the schema-versioned event-lifecycle ledger: one
    record per boundary EID (`detected` -> `queued` -> `dispatched` -> one
    terminal state, with the tick and displayed level of each step, the
    coalesced members, and wall timing kept in a separate field) and one per
    directive activation/expiry.  Records are written **incrementally** as
    they finalise, so a long episode never bursts at the end: a boundary that
    is detected but never queued (the shipped `--strategy off` default) is
    finalised in the round that produced it with `terminal: null`, and the
    in-memory detail window is capped while emission continues.
  * `ep-N.meta.json` — schema versions, allowlisted configuration, the stop
    reason, the visible outcome and the **budget ledger**.  It also carries
    two live-spectating keys (`spectate_frames_rendered`,
    `spectate_disabled_reason`) on every episode, `0`/`null` when spectating
    is off; see "Live spectating".

A campaign also writes `campaign.json` into `--output-dir`: a compact,
secret-free rollup of every episode (stop reason, visible outcome, protocol
health, tick/need/boundary/strategy counts and token/USD usage), campaign
totals, and the allowlisted configuration -- including the non-secret
`deepseek_max_tokens` and `deepseek_max_bytes` bounds.  Reported usage,
`unknown_price_calls` (a real answer whose price is unset) and
`unknown_exposure_calls`/`_tokens`/`_usd` (a call that reached the wire but
returned no usage) are preserved and totalled **separately**: an unknown
exposure is never converted into an asserted cost.  It is written after the
episodes so a failure to write it cannot lose them; the CLI prints its path on
success and reports the failure explicitly on stderr -- it never claims a path
that does not exist.

Writes run on a bounded background writer so the wire is never blocked; a
full queue or disk failure marks the recording **incomplete**
(`recording_complete: false`) rather than silently dropping bytes.
Directories are `0700` and files `0600`.  No secrets are recorded.  Note that
game names and game text may themselves be private.

## Live spectating

`--spectate [tty|stderr|none]` renders the live presentation to a side
channel while an episode plays, without ever touching the wire or a
recording.  `none` (the default) opens, duplicates and probes nothing.  A
bare `--spectate` means `stderr`; `tty` opens `/dev/tty` separately and falls
back to fd 2 (with one best-effort note per campaign) when there is no
controlling terminal.  fd 1 and any detectable stdout alias — a shared FIFO,
regular file or socket, including a `2>&1` redirection — are refused, because
frames there would corrupt the wire; a genuinely separate terminal device is
fine.  Redirect the side channel to a file only by keeping fd 1 clear:

```sh
./agent.sh auto --episodes 3 --strategy off \
    --output-dir /tmp/auto --spectate tty

./agent.sh auto --output-dir /tmp/auto \
    --spectate --spectate-interval 0.25 2>/tmp/auto-frames.txt
```

fd 2 also carries ordinary diagnostics, so neither file is an authoritative
recording — the sidecars are.  Note that the rendered game names and game
text may themselves be private.

**Frames.** One frame is 21 bare 80-column ASCII map rows plus compact
status, message, need, directive and counter lines clipped to 80 columns.
The map is the **currently applied `Snapshot`** — the presentation authority —
never the remembered terrain, so a cell that has disappeared from the live
presentation is drawn blank.  The cursor is the snapshot's own, overlaid as
`*` at its valid coordinate (never inferred from the remembered hero).
Status comes from durable memory with `?` for any unknown field (never an
invented zero).  The trailing messages are **episode history**
(`recent_messages(3)`), so a new observation that carries no message keeps the
previous lines.  Active directives are shown through a *peeked* read that
never expires or logs them.

**Scheduling.** `--spectate-interval` is the minimum time between frames;
`0` is unthrottled.  One observation candidate is taken per successfully
applied and need-validated snapshot, and throttling **coalesces**: only the
newest candidate is retained.  The controller's own select loop is woken by
the next due frame, so a throttled trailing frame is still delivered without
a new wire record, and its write deadline is capped by any active wire
deadline so a render wake can never extend a wire timeout.

**Timing.** Advice settled during a strategy service first appears in the
**next** snapshot's frame, never retroactively.  The final frame is a fresh
composition — recentred on the settled counters, current directives and the
*current* outstanding need — after the outcome is resolved; it is never a
replay of an older candidate.  With no accepted snapshot there is no
fabricated final frame.

**Failure handling.** Writes are select-gated in chunks no larger than
`PIPE_BUF` against one absolute per-frame deadline (0.25s) that progress,
`EINTR` and `EAGAIN` never extend.  An exhausted frame is dropped and never
resumed; after three consecutive exhausted frames spectating disables itself
for the episode with `spectate_disabled_reason: write-deadline`.  A partial
frame may remain visible on the side channel — that is not wire corruption.
On a TTY the redraw height commits only after complete delivery, and a
partially written frame forces an absolute resynchronization before the next
ordinary frame.  A presentation failure never changes the wire, recorder
health, provider policy, budget settlement, event emission, outcome or exit
accounting; it only sets the episode's `spectate_disabled_reason`.  All
render-only state resets per episode.

Honest limitation: a synchronous deadline cannot preempt a blocking syscall
in an exceptional stderr race or a pathological device/filesystem stall (an
owned nonblocking fd avoids the common case, but a duplicated blocking fd 2
can still, very rarely, block in one short write).  The bounded exhaustion
and disable path — not the absence of every adversarial race — is what is
tested.

## Prompt-cache utilization

DeepSeek caches a request's *prefix* automatically and reports how much of the
prompt was served from cache (`prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`).  The harness exploits this by keeping a bounded,
**episode-local** conversation and rendering each turn so the previous prefix
stays byte-identical.

  * **Ownership.**  One conversation per episode (live) or per replay pass --
    never one spanning several campaign episodes.  The harness owns it, not
    the provider: a worker or provider respawn does not own or clear it, and
    a direct `DeepSeekStrategy.deliberate` call outside the harness is
    stateless (it prepares a fresh two-message request).
  * **Render order.**  Each user turn is a complete bounded snapshot: the
    `GAME STATE (untrusted data):` label first, then `mode`/`role`, active
    directives, inventory, boundary history, pending boundaries, recent
    messages, map, displayed level, status, tick, and the
    `remaining strategy calls:` line last.  An earlier turn is never
    re-rendered with a newer budget or state.  No episode id, campaign path,
    UUID or wall time enters model-facing text.
  * **Boundary history** is a bounded 16-record harness-owned window with
    stable eid/reason/tick/level fields; the current request's pending
    boundaries are rendered separately and always survive the truncation.
  * **Transaction.**  A user/assistant pair is committed only when the result
    is ok, carries validated directives and was not cancelled -- the retained
    slice the frozen request carried plus the new pair, capped to
    `--deepseek-history-pairs`.  The assistant text is the model's **verbatim
    validated** response (canonicalizing it would change the generated
    prefix); a dict-shaped or injected response without verbatim text falls
    back to a stable serialization.  A failed, timed-out, cancelled or
    locally-refused call appends nothing and leaves the prior history intact.
  * **Eviction.**  Deterministic: oldest complete pairs are dropped until both
    the pair-count cap and the byte ceiling fit, always keeping the system
    message and the current user message.  Eviction is *not* used to make room
    for another paid call -- an oversize reservation follows the normal
    cap-refusal path.  A reset happens on a new episode (or, for a change of
    model/base URL, a new runner); it does **not** happen at a level change.

Reservation stays conservative: `strategy_token_bound` covers the *exact*
frozen request -- every message role and content in UTF-8 bytes, a fixed
request allowance and a per-message allowance, plus the configured max
completion tokens -- and never discounts cached tokens.  Later strategy calls
can therefore be refused *sooner* as history accumulates; a reported cache
partition lowers only the settled USD, and only once the usage is known.

Prompt-cache accounting is deliberately conservative.  A discount is granted
only for a **complete, consistent** partition (`hit + miss == prompt`); an
absent, partial, contradictory, negative, floating or boolean cache report
prices the prompt at the full input price and counts it as unclassified.  The
reported `cache_hit_rate` is measured over classified tokens only, so a high
rate over thin reporting coverage is not mistaken for a wide one; the campaign
rollup computes the rate from the summed hit and miss totals, never as an
average of episode percentages.  `reasoning_tokens` is a diagnostic subset of
the completion tokens and is never added to them a second time.

The **postmortem** always gets a separate, empty conversation and a fresh
provider; it never transfers gameplay history and never touches the gameplay
provider's sticky cancellation flag.  Its prompt is `mode: postmortem` plus an
allowlisted deterministic episode summary (outcome/stop reason, final tick,
visible level and HP, action/invalid/boundary counts, strategy dispatch count,
applicable advice), bounded terminal messages and final visible state.

## Offline evaluation (`tools/agent.evaluate`)

Replay a recording **without a game and, by default, without a network**,
through the same assembly, public-state, boundary and decision machinery the
live controller uses, and compare the candidate providers' proposals to the
recorded action:

```sh
# replay the startup fixture with scripted/off; writes a JSONL report
python3 -m tools.agent.evaluate test/agent/fixtures/auto/startup.wire.jsonl \
    --reflex scripted --strategy off --output /tmp/auto-eval.jsonl

# compare a recording's actions to its own sidecar; assert menu semantics
python3 -m tools.agent.evaluate test/agent/fixtures/auto/short.wire.jsonl \
    --reflex scripted --strategy off --max-ticks 30 \
    --actions test/agent/fixtures/auto/short.actions.jsonl \
    --decisions test/agent/fixtures/auto/short.decisions.jsonl \
    --output /tmp/short-eval.jsonl
```

The positional argument is a `ep-N.wire.jsonl` recording (or any inbound
physical line stream).  `--reflex scripted|jev` selects the primary candidate;
repeatable `--provider scripted|jev` adds more candidates to compare; each
candidate is replayed in **its own isolated pass**, with its own episode
memory and budget ledger, so one provider's decisions can never influence
another's.

An explicitly `--allow-network --strategy deepseek` pass keeps the **same**
continuity policy as live play -- the same K/byte eviction, render and
preparation helper, whole-request bound, and successful-settlement commit rule
-- and starts each pass from an **empty** conversation. Comparison passes with
the strategy off stay off and unchanged.  The evaluator's own
`--deepseek-history-pairs`, `--deepseek-context-max-bytes` and
`--deepseek-price-cache-hit` flags mirror the live CLI.

Determinism is scoped deliberately: the same wire and configuration produce
byte-identical **requests** and byte-identical cleaned artifacts, because
preparation is deterministic and uses no random ids, real clocks or
wall-timed eviction; network calls were never reproducible merely because
preparation is.  The new usage fields are deterministic zeros/`null` in
offline runs.

### Output

The output JSONL has three record kinds:

  * `record: "need"` — one per need: the need key, the candidate(s)'
    `proposal`, the `selected` action, the provider label, `legal` (the
    selected action passes the wire shape gate), `fallback`, `low_confidence`,
    the `actual_action` and its `actual_action_source` (`sidecar` or
    `unknown`), the `agreement` against the original, the `sent_ordinal` of
    the modeled send (every answer is one act, exactly as live), the
    boundary `eids` detected at that need, and any active directive set;
    a rejected attempt's row additionally carries `rejected_action`, the
    `retry_action` that replaces it and that retry's `sent_ordinal`;
  * `record: "boundary"` / `record: "directive"` — the deterministic
    event-lifecycle ledger, with the wall-timing map **dropped**;
  * `record: "summary"` — the rollup: per-need coverage, per-provider
    agreement rate, legality rate and fallback count, boundary detections by
    reason, directive applications, the visible outcome, and the budget
    ledger.

### Interpreting it

  * **coverage** is *decided needs / needs* — an unanswered need would lower
    it below 1.0.
  * **agreement** compares action **semantics**, never JSON bytes: a menu
    commit is its final set of rows **with their counts** (a menu generation
    id is ignored), and `key`/`yn`/`position`/`text`/`cancel`/`ack` compare by
    shape and value.  `-1` ("the whole stack") and `1` ("one item") normalize
    equal **only** when the delivered page rows prove the row is a single item
    (no count prefix in its displayed text, no positive stack count); on a
    stack, or with no delivered metadata, the counts stay distinct.
  * **ground truth follows the accepted attempt.**  When the wire rejects an
    action with `invalid`, the rejected attempt is labelled as such (its own
    `record: "need"` row carries the `rejected_action`, and the retried need
    lists it under `rejected_attempts`) and the *accepted* candidate — the
    attempt the wire did not reject — is what `actual_action` and the
    agreement/coverage figures use.  A first, rejected action is never
    reported as the original.  The rejected attempt is also **discarded**,
    exactly as the live controller discards its in-flight attempt: its
    frozen effect is never committed at the next observation and its action
    drives no recorded-state reconciliation.  The accepted retry — the
    attempt recorded after the rejected ones, or unknown when the sidecar is
    exhausted — is the modeled send instead, so a same-ID retry does not
    resend (or commit) the rejected winner.
  * **legality** is structural validity (the `validate_action` gate); it is
    not safety.  Confidence agreement with a recorded action is **not**
    evidence of calibration, and is never reported as such.
  * A recording with **no actions sidecar** (the older inbound-only corpus)
    labels every `actual_action` `unknown` and reports agreement coverage 0
    (a movement key is never *guessed* from a state change).

### Caveats

  * **The replay configuration must match the recording's.**  `--max-ticks`
    in particular changes the scripted tick-cap quit; replaying a run
    recorded with `--max-ticks 30` under the default 2000 legitimately
    disagrees on the quit, which is an evaluation error, not policy drift.
  * **Deterministic**: a replay is a pure function of the wire bytes and the
    candidate configuration.  Wall timing is confined to a separate `wall`
    map (dropped here), so two runs of the same command are byte-identical.
  * **Page collection is as strict as the live client.**  A page is only ever
    the response to the owed `get_page`: an unsolicited, out-of-order or
    duplicate page, a page for the wrong content, or one declaring a different
    total than the need is an evaluation **protocol failure** (`closed`, with
    `protocol_failure` set), exactly as in the live controller.
  * **Provider decisions are never injected** into the trajectory, and a
    hypothetical effect is never claimed.  The replay advances only along the
    recorded wire; you cannot read a counterfactual outcome from it.
  * **Real provider evaluation is opt-in.**  `--strategy deepseek` refuses to
    run without `--allow-network`; comparison passes stay network-free so an
    N-provider comparison does not multiply paid calls.  The presence of a
    key alone never opts a user in, mirroring the live harness.

### Fixtures

`test/agent/fixtures/auto/` holds the bounded corpus (see its README for
provenance and hashes): `startup.wire.jsonl` and `legacy-ep3.wire.jsonl` are
inbound-only excerpts that exercise the `unknown`-action path, and
`short.wire/actions/decisions` is a fresh recording with real ground-truth
actions (replaying it with the matching `--max-ticks 30` reproduces every
original action).

## Verification

```sh
python3 test/agent/format_obs.py --selftest          # decoder (compat)
python3 test/agent/spectate.py selftest              # renderer (compat)
make -C test/agent check                             # fixtures + manifest
python3 -m unittest discover -s test/agent -p 'test_auto*.py'   # harness

# offline evaluator: deterministic (run twice, diff clean), no network
python3 -m tools.agent.evaluate test/agent/fixtures/auto/startup.wire.jsonl \
    --reflex scripted --strategy off --output /tmp/auto-eval.jsonl
# ground-truth agreement against the fresh fixture (must match --max-ticks)
python3 -m tools.agent.evaluate test/agent/fixtures/auto/short.wire.jsonl \
    --reflex scripted --strategy off --max-ticks 30 \
    --actions test/agent/fixtures/auto/short.actions.jsonl \
    --output /tmp/short-eval.jsonl
```

The provider tests run against a **fake loopback HTTP endpoint** driven by
the real worker process, covering slow, hung and drip responses, 401/429/5xx,
malformed JSON, an oversized body, an outage mid-play, and a secret-free
artifact audit.  Live API smoke tests are opt-in only.
