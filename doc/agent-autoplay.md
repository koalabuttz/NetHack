# Autonomous agent play (`./agent.sh auto`)

The two-tier autonomous-play harness.  It drives the headless agent wire
(`doc/agent-interface.md`) itself: one bounded controller owns the game pipe,
reconstructs public state, fulfils every transport obligation, and sends one
validated action per request.  Nothing here reads engine state, invents a
protocol, or (by default) touches the network.

Two tiers, from the handoff (`doc/agent-autoplay-plan.md`):

  * **reflex** -- answers every request, in the action path.  ScriptedReflex
    always ships and is always available; an optional Jev tier can replace it
    with a typed-choice decision.  Paid reflex work ships **disabled**.
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
    --strategy-call-cap 8 --output-dir /tmp/auto-ds
```

## Command line

`--episodes N` (default 1)
    how many independent episodes to run.

`--reflex scripted|jev` (default scripted)
    the reflex tier.  `scripted` is always available; `jev` requires
    `JEV_API_KEY`, `--i-accept-jev-terms` and `--jev-base-url`, and even then
    only routes to the fake-endpoint-testable adapter (see "Providers").

`--strategy off|deepseek` (default off)
    the strategy tier.  `off` is completely network-free.

`--role ROLE` (default Valkyrie)
    the role the reflex selects at startup.

`--max-ticks N` (default 2000)
    graceful-quit budget, counted in gameplay commands.

`--episode-timeout S` (default 300)
    wall-clock budget per episode.

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
    the minimum confidence for a *paid* reflex answer to be accepted; a
    paid answer below it falls back to scripted play.  Valid range `0..1`.
    It applies to the calibrated paid reflex confidence only -- the scripted
    heuristic score is never compared to it, so an ordinary scripted decision
    is not treated as uncertain.

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
    No price is invented: with no tariff the estimate stays zero and the
    unknown-price exposure is counted instead.  Every cap is a *conservative
    ceiling*: a call is admitted only when the estimated prompt plus the
    configured maximum output fits in what is left, and a call that returns
    no usage keeps that exposure recorded (never silently dropped).

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

`--deepseek-max-tokens N` (default 400)
    a conservative output bound; responses are non-streaming and size-capped.

`--jev-key-file`, `--jev-base-url`, `--i-accept-jev-terms`
    the Jev trio; all three are needed before `--reflex jev` will start.

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

Every started strategy operation settles exactly once.  When an episode ends
with a call still outstanding (a failure, an episode timeout, or a clean
closure), the call is committed as **dispatched** -- with its reported usage
if it returned one, and as counted unknown exposure if it did not; it is
never released as undelivered -- and its boundary set is terminated so every
dispatched event has exactly one terminal state.  A result that completed in
the cancellation race is preserved rather than dropped.

A postmortem is requested only after a **clean** closure: a validated
`closed` with no request left unanswered, a healthy recording and a
postmortem slot still reserved.  An EOF, a protocol failure, an episode or
content deadline, and a `closed` that arrived with a request still
unanswered are all ineligible -- none of them is a completion to reflect on.

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
    of unknown-price calls otherwise.

The cap is authoritative: a call is reserved before dispatch, failed calls
are not refunded, and once the remaining cap cannot conservatively cover one
more call, paid dispatch is disabled for the rest of the episode.

A **recorder failure** (full queue or disk error) triggers the graceful-stop
policy: paid dispatch stops immediately, any in-flight worker is cancelled,
and scripted play continues so the request obligation is still met -- the
recording is marked incomplete rather than silently truncated.

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
malformed or unknown value -- but **ships DISABLED**: `--reflex jev` fails
with a clear message unless `JEV_API_KEY`, `--i-accept-jev-terms` and
`--jev-base-url` are all present, and even then only the fake-endpoint
adapter is reachable (no official contract has been supplied).  Keys 1..255
fit one <= 255-way choice; menus are limited to 128 selectable rows;
position, `line` and `extcmd` are never sent to Jev.

Secrets come only from `DEEPSEEK_API_KEY` / `JEV_API_KEY` or a **0600 key
file**.  The key never appears in a log line, a recording, a structured
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
    directive activation/expiry;
  * `ep-N.meta.json` — schema versions, allowlisted configuration, the stop
    reason, the visible outcome and the **budget ledger**.

Writes run on a bounded background writer so the wire is never blocked; a
full queue or disk failure marks the recording **incomplete**
(`recording_complete: false`) rather than silently dropping bytes.
Directories are `0700` and files `0600`.  No secrets are recorded.  Note that
game names and game text may themselves be private.

## Verification

```sh
python3 test/agent/format_obs.py --selftest          # decoder (compat)
python3 test/agent/spectate.py selftest              # renderer (compat)
make -C test/agent check                             # fixtures + manifest
python3 -m unittest discover -s test/agent -p 'test_auto*.py'   # harness
```

The provider tests run against a **fake loopback HTTP endpoint** driven by
the real worker process, covering slow, hung and drip responses, 401/429/5xx,
malformed JSON, an oversized body, an outage mid-play, and a secret-free
artifact audit.  Live API smoke tests are opt-in only.
