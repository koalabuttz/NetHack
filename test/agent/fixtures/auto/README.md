# Bounded replay fixtures for the offline evaluator

These fixtures are the small, committed corpus for
`python3 -m tools.agent.evaluate` (see `doc/agent-autoplay.md`, "Offline
evaluation").  They are **input recordings**, not expected-output golden
files: the evaluator replays them through the same assembly/state/boundary
machinery the live controller uses, and the unit tests assert on the
deterministic *structure* of that replay (coverage, agreement, labels), not on
incidental numbers that a policy tweak may legitimately move.

## Budget

The whole directory is well under the 256 KiB uncompressed budget the handoff
sets for committed transcript fixtures (it exists so multi-megabyte ephemeral
episodes are never checked in):

| file | bytes |
|---|---|
| `startup.wire.jsonl` | 18830 |
| `short.wire.jsonl` | 84786 |
| `short.actions.jsonl` | 12661 |
| `short.decisions.jsonl` | 10085 |
| `legacy-ep3.wire.jsonl` | 39341 |

Total: **165703 bytes** (161.8 KiB).

sha256 (integrity of the committed snapshots; the upstream ephemeral
recordings are not in the repository):

- `startup.wire.jsonl`
  `60a7572334ca58ea06c09c39d2c7c998b1b7ff18993123e7ff141174de93e6ca`
- `short.wire.jsonl`
  `02d779f44dbda793d9c7945da00d6b56b7fe66ea5cf422def4583fa3e78114b2`
- `short.actions.jsonl`
  `9d5011acb959664803138571f65adb7a331206038eb84bbc91cc6fde89021384`
- `short.decisions.jsonl`
  `4e0b33f38cc05f2fc13552bd0cb25bd7a560c191228e8d417c58001133730a7e`
- `legacy-ep3.wire.jsonl`
  `f59639a98d043c95c315cf4ab2bd93aa1fb656e7bb150e30a5dbf3294f4d00e7`

## Provenance

Build under test: NetHack 5.0.0 agent build at commit
`23bb9801cad7c7e68802ce6caab44ec6bae592df` (short `23bb9801c`), captured
2026-09-17.  The agent binaries (`src/nethack`, `src/nethack-agent`) were the
already-built agent build in the tree; game data was staged in
`/tmp/nethack-agent-data`.

### `startup.wire.jsonl` — legacy inbound-only prefix

A verbatim **prefix of the first 14 physical records** of
`/tmp/nethack-spectate/ep-3.jsonl` (Samurai, an older inbound-only spectate
recording; source sha256
`21b93972a644174756eb869c09365327d95b6c33a7d6705d1182e76d00775dbd`).  It
covers exactly the startup path the handoff names: `hello`, character
selection (`yn` "shall I pick ..." and the `menu` confirmation), two menu
pages, an `ack` page, the tutorial `menu` decline, and the first six gameplay
`command` needs.

**Truncation:** cut at the 14th record boundary (a whole `obs` record), so the
final need is fully decidable.  There is no `closed` record, so a replay ends
at EOF — which the evaluator reports as a note, not a completion.  This
transcript has **no action sidecar**, by construction: it predates the
controller recorder.  Replays must therefore label every `actual_action` as
`unknown` (agreement coverage 0) rather than guessing a movement key from a
state change.

### `legacy-ep3.wire.jsonl` — legacy inbound-only slice

The recorded `hello` (record 0 of `ep-3.jsonl`) followed by the verbatim
records 100–111 of the same source: a short mid-play slice that contains a
distinctive `yn` prompt (the teleportation-trap question) plus surrounding
`command` needs.  Same legacy properties as above (no sidecar, EOF at the
end).  It exists so the "unknown action" path is asserted on a slice that is
not merely the startup prefix.

### `short.*` — fresh recording with ground truth

A **new, complete** recording made with the current build:

```sh
env -u DEEPSEEK_API_KEY -u JEV_API_KEY \
  ./agent.sh auto --episodes 1 --reflex scripted --strategy off \
  --max-ticks 30 --episode-timeout 120 --output-dir /tmp/auto-short
```

`short.wire.jsonl` is `ep-1.wire.jsonl` copied verbatim (its hash matches the
source).  It is the inbound stream only.  `short.actions.jsonl` is the
controller's outbound-action sidecar (ordinal, preceding input offset,
`NeedKey`, kind, the exact action envelope and send status) and
`short.decisions.jsonl` is its decision sidecar (proposal, selected, provider,
reason, boundaries, usage).  The episode ran until the tick-cap graceful quit,
so it ends in a clean `closed`.

It carries 48 needs — character selection (`yn`, two `menu`s, an `ack`),
31 `command` moves, one `extcmd` (the tick-cap quit) and mid-run prompts — and
is the fixture with real ground-truth actions for agreement assertions.

**Reproduce the ground-truth agreement:**

```sh
python3 -m tools.agent.evaluate test/agent/fixtures/auto/short.wire.jsonl \
  --reflex scripted --strategy off --max-ticks 30 \
  --actions test/agent/fixtures/auto/short.actions.jsonl \
  --decisions test/agent/fixtures/auto/short.decisions.jsonl \
  --output /tmp/short-eval.jsonl
```

Because the recording was produced by the scripted reflex itself, replaying it
with the *same* configuration reproduces every original action
(agreement 48/48).  **The `--max-ticks` value must match the recording's** —
the replay's reflex quits at the same tick cap, and a different cap changes
the `extcmd` answer and the tick at which the quit is requested.  A mismatch
is a configuration error in the evaluation, not policy drift.

## Regenerating

`startup.wire.jsonl` and `legacy-ep3.wire.jsonl` are cut from an ephemeral
recording that is **not** in the repository (`/tmp/nethack-spectate/`); the
hashes above are the integrity check.  `short.*` can be regenerated with the
`./agent.sh auto` command shown above (any fresh run will differ in map
content — these fixtures are a snapshot of one run, not a canonical one).
