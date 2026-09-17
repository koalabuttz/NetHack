# Autonomous agent play (`./agent.sh auto`)

The two-tier autonomous-play harness.  It drives the headless agent wire
(`doc/agent-interface.md`) itself: one bounded controller owns the game pipe,
reconstructs public state, fulfils every transport obligation, and sends one
validated action per request.  Nothing here reads engine state, invents a
protocol, or (by default) touches the network.

This document describes the shipped **Wave 1** release: **scripted-only**
play (`--reflex scripted --strategy off`).  The provider contracts for a
later strategy tier exist and are stubbed; see "Providers" below.

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

## Command line

The launcher also accepts deeper provider flags this wave ignores
(`--confidence-threshold`, `--strategy-call-cap`, `--deepseek-model`,
`--deepseek-base-url`, `--deepseek-key-file`, `--jev-key-file`) so a later
wave can add providers without changing the argv.

`--episodes N` (default 1)
    how many independent episodes to run.

`--reflex scripted` (default scripted)
    the reflex tier; only `scripted` is shipped this wave.

`--strategy off` (default off)
    the strategy tier; only `off` is shipped this wave.

`--role ROLE` (default Valkyrie)
    the role the reflex selects at startup.

`--max-ticks N` (default 2000)
    graceful-quit budget, counted in gameplay commands.

`--episode-timeout S` (default 300)
    wall-clock budget per episode.

`--answer-deadline S` (default 1)
    how long the controller may take to write one outbound line once a
    request's content is complete; a full stdin pipe is bounded by this
    rather than blocking.

`--content-deadline S` (default 5)
    the aggregate content/transport deadline for one outstanding request;
    exceeding it aborts the episode (`stop=content-deadline`).

`--reflex-deadline S` (default 0.75)
    the absolute reflex decision allowance.  The deadline is passed to the
    provider and the call is run on a bounded thread, so a provider that
    overruns is abandoned and the scripted fallback answers instead of
    holding the wire.  A truly blocking network provider still needs the
    Wave-2 killable worker process; the contract and plumbing exist now.

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
    is accepted — any other page response ends that episode as a protocol
    failure rather than being counted as delivery.
  * Chunk streams are assembled strictly and acknowledged cumulatively as
    they arrive.
  * `invalid` is a **retry state**: the same request id is re-answered,
    repaired once where applicable, then answered with a per-kind safe
    fallback, and the episode stops after a small total retry cap instead of
    spinning forever.  Retries consume the *same* aggregate content budget as
    the original need — an `invalid` does not restart its deadline.
  * A malformed record (a JSON array, or a broken palette, map triple,
    window, cursor or need) ends **that** episode as a protocol failure; the
    campaign continues with the next episode.  A need is validated in full --
    per-kind required fields, types, bounds and reference patterns -- before
    any of it is stored, so a dict-shaped need that is missing a field or out
    of range fails one episode rather than the whole campaign.
  * The controller's fallback for a gameplay command is never a rest: it is
    reached only when the reflex fails, overruns its allowance or proposes
    something invalid, and it carries no snapshot context, so it cannot prove
    a rest safe.  It searches instead — the policy's own choice when rest is
    unproven — so a fallback never holds position beside a monster, while
    hungry, at low HP, or with the hero square unknown.
  * A game message such as `You don't have that object.` is distinct from
    wire `invalid`; the reflex carries a separate repeated-food breaker.

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
    so a monster is never assumed tame and is never stepped into.  Getting
    unblocked means searching, routing around or changing plan, never a
    last-resort attack or a pet swap.  Visible traps, boulders and the
    punctuation monster classes (`'`, `&`, `;`, `:`, `~`, `]`) are avoided
    too, as is an `@` that is not on the hero's own known square -- the human
    class is the hero *and* other humans, so identity is per cell;
  * **hunger** — answers the engine's `getobj` eat prompt with a valid
    inventory letter parsed out of the prompt text (the engine passes no
    machine-readable `choices` for that prompt), or opens the inventory
    menu, and after two equivalent rejected intents refreshes and changes
    plan.  Only an allowlist of known-safe items is edible, keyed to the
    engine's exact canonical object names ("lembas wafer", "food ration") and
    their exact plurals.  The inventory row's own metadata is stripped first
    -- a displayed stack count, an article, the uncursed/blessed/cursed and
    "partly eaten" qualifiers, a user " named <text>" suffix and a shop
    "(unpaid, ...)" annotation -- so a counted, qualified, named or priced row
    still matches ("2 lembas wafers", "a food ration named lunch", "an
    uncursed food ration (unpaid, 45 zorkmids)").  The stripped base name, not
    the suffix, is what must match, so "a food ration named cockatrice egg"
    stays edible while "a cockatrice egg named lunch" does not; a qualified
    egg, a corpse or a tin, and a lookalike that merely contains a safe word
    all stay unsafe;
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
rejected food intents, opens the inventory menu and refreshes.  This is a
client obligation, not an adapter defect — the row-commit path used when the
client presses `*` maps a non-first food row correctly.

## Providers

`--reflex scripted --strategy off` is completely network-free by
construction: no code path opens a socket.  The `providers.py` contracts
(`Provider.available`, `ReflexProvider.decide`,
`StrategyProvider.deliberate`) are fixed, and the `JevReflex` and
`DeepSeekStrategy` adapters are **disabled placeholders** — they advertise
themselves unavailable and perform no I/O.  A later wave implements them
under the same interfaces; presence of a key never opts a user into paid
calls.

## Recordings

Each episode writes four sidecars under `--output-dir`:

  * `ep-N.wire.jsonl` — the inbound physical bytes, verbatim, in order;
  * `ep-N.actions.jsonl` — outbound actions/auxiliaries with their ordinal,
    the preceding input offset, the `NeedKey` and the send status;
  * `ep-N.decisions.jsonl` — proposals, the selected provider, fallback
    reasons, boundaries and latency;
  * `ep-N.meta.json` — schema versions, allowlisted configuration,
    completeness, the stop reason and the visible outcome.

Writes run on a bounded background writer so the wire is never blocked; a
full queue or disk failure marks the recording **incomplete**
(`recording_complete: false`) rather than silently dropping bytes.
Directories are `0700` and files `0600`.  No secrets are recorded.

## Verification

```sh
python3 test/agent/format_obs.py --selftest          # decoder (compat)
python3 test/agent/spectate.py selftest              # renderer (compat)
make -C test/agent check                             # fixtures + manifest
python3 -m unittest discover -s test/agent -p 'test_auto*.py'   # harness
```
