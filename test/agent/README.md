# test/agent — engine-free fixtures for the NetHack agent port

These are the Phase 1 (P1) fixtures for the headless agent interface described in
`doc/agent-interface.md`. They are **standalone**: no engine header, no engine
object, no Lua, no tty, no supervisor, and no `setup.sh`. Nothing here builds or
touches the main game.

The public encoder, menu model, and appearance sanitizer live in `win/agent/`
and include only `<stdint.h>`, `<stddef.h>`, `<stdbool.h>`, `<stdlib.h>`, and
`<string.h>`.

## Commands

```sh
make -C test/agent check       # manifest + header + schema + every fixture
make -C test/agent sanitize    # same, under AddressSanitizer + UBSan
make -C test/agent depcheck    # prove no fixture reaches an engine header
make -C test/agent clean
```

`check` is the whole gate.  It first regenerates `doc/agent-profile-v1.tsv`
from `include/optlist.h` and diffs it, so a drifted or hand-edited manifest
fails; it regenerates `win/agent/agent_profile.h` with `--emit-c` and diffs
that too, so the committed enforcement header cannot drift from the generator;
it then schema-validates real encoder output plus the built-in grammar vectors
(`schema_check.py`); it then builds and runs every fixture.

The hostile-startup matrix (the M1 exit gate) needs a built agent-only worker
and the trusted launcher, so it is not part of the engine-free `check`:

```sh
make agent-test-data AGENT_TEST_DATA=/tmp/agent-data   # stages data only; never relinks the game
make -C test/agent matrix \
    WORKER="$PWD/src/nethack" RUNNER="$PWD/src/nethack-agent" \
    DATA=/tmp/agent-data SYSCONF=/tmp/agent-data/sysconf
```

The plan's standalone smoke compile also works from the repository root:

```sh
cc -std=c99 -Wall -Wextra -Werror -Iwin/agent \
  win/agent/agent_view.c win/agent/agent_menu.c \
  test/agent/test_menu.c -o /tmp/nethack-agent-menu-test
/tmp/nethack-agent-menu-test
```

`depcheck` uses the compiler's `-MD` dependency output rather than grepping
`#include` lines: it fails if any translation unit's prerequisites contain a
relative `include/…` or `src/…` path. The same proof by hand:

```sh
cc -std=c99 -Iwin/agent -H -c win/agent/agent_protocol.c -o /dev/null
```

prints only `win/agent/*.h` plus system headers.

## What each fixture covers

| File | Covers |
|---|---|
| `test_view.c` | equal-looking inputs → identical public cells; pet/pile/detection/BW inverse collapse to one style; a displayed frame color beats pet highlighting; wizard-only reasons never encode; menu context ignores map-only reasons; non-ASCII and out-of-range colors rejected; the 16 frozen color names; hidden `yn` choices stop before Escape |
| `test_menu.c` | selector-zero selectable rows, headings, duplicate visible text, preselected `PICK_ANY`, counts (-1 / positive / zero / overflow), empty versus cancel, `PICK_NONE`/`PICK_ONE`/`PICK_ANY`, duplicate rows, normalization to insertion order, and explicit rejection of group/bulk/invert/select-all/raw-key fields |
| `test_protocol.c` | strict `act` parsing (every tagged shape plus ~40 rejected shapes), escaping and UTF-8 validity, exact `hello` and the bare `closed` object, delivery/seq counter discipline, fragmented input and short writes, stale id consumes nothing, identical retry replays the retained response, conflicting id reuse closes, paging (`invalid(incomplete)` until every required page is delivered, `get_page` range errors), chunk planning at every boundary, and forced chunk emission with identical logical content |
| `test_state.c` | the durable/audit reference model: a projectile that returns to the original durable map, palette isolation from animation, identical-frame suppression, nonblocking displays never advancing `seq`, chunk acknowledgement and identical retries, and resync replay that dedupes `(interval,k)` |
| `test/agent/gen_profile.py` | regenerates `doc/agent-profile-v1.tsv` (default) and the enforcement header `win/agent/agent_profile.h` (`--emit-c`) from `include/optlist.h`; refuses to emit a row for an option it has no classification for |
| `test/agent/hostile_matrix.py` | the M1 exit gate: hostile HOME/rc/env, hostile argv, bad/missing bootstrap handshake, and the test-only diagnostic injection, asserting no public leakage and the expected private rejection |
| `test/agent/format_obs.py` | canonical client formatter: assembles chunk streams back into logical records (deriving `d` from `rid` and splicing `t` long-text slices) and prints a human-readable projection |
| `test/agent/schema_check.py` | dependency-free JSON Schema subset validator plus 25 positive and 29 negative vectors; also validates real encoder output piped from `test_protocol --dump` |
| `test/agent/spectate.py` | live spectate: a transparent byte-exact proxy that renders the flowing records to a side channel while relaying the wire, a transcript replay mode, and a runner-mode shim usable as the driver's `--runner` |
| `test/agent/test_spectate.py` | integration tests for the spectate revision: incremental/batch assembler agreement and every rejection vector, the deadline-scheduled render pipeline and its bounded shutdown, the writer helper and its acknowledgements, byte-exact relay/transcript back-pressure, argv identity, failure exit-status hygiene, replay equivalence, plus the opt-in performance (`--benchmark`) and byte-exact (`--byte-exact`) harnesses |

## Formatter

```sh
python3 test/agent/format_obs.py transcript.jsonl        # human projection
python3 test/agent/format_obs.py --json transcript.jsonl # assembled records
python3 test/agent/format_obs.py --raw transcript.jsonl  # pass lines through
python3 test/agent/format_obs.py --selftest              # chunk-assembly vectors
```

It is a client-side debugging aid, not a production component. Its chunk
assembler is strict and doubles as the reference for what a client must reject:
chunks stored by `(rid, i)` with contiguous indices from zero, exact repeats
deduplicated, changed repeats and gaps rejected, header parts allowed only in
chunk 0, and long-text slices required to name an existing element with
contiguous offsets before the record is rebuilt atomically.

## Watching an episode

`test/agent/spectate.py` renders the wire as it flows — a compact frame per
observation (an 80×21 coloured map, the status line, the last few messages, and
the outstanding `need`) — so a human can watch while a scripted policy or an LLM
harness plays. It is a **transparent proxy**: it spawns the real launcher and
relays the player channel byte-for-byte in both directions, rendering a *copy*.
Rendering is bounded and best-effort: it runs on its own schedule behind a
small handoff queue, coalesces frames under load, and disables itself (never
silently, and never by changing a wire byte) rather than growing without
bound. A stalled side channel therefore cannot stall the player, and shutdown
stays bounded.

The transcript is exact in a precise, narrow sense: on success it is exactly
the launcher-output bytes the consumer-stdout writes accepted, in order. It
contains no rendered frames or diagnostics and is never reserialised. That
confirms delivery to the operating-system channel, **not** that the consumer
application read or processed the bytes. On a downstream write failure,
forwarding stops immediately, the transcript ends at the last confirmed write
(possibly inside a line), the launcher is terminated and reaped, and the
wrapper exits nonzero; a transcript-storage failure is reported as an
incomplete recording.

Watch a scripted play run (frames to the terminal, verbatim transcript saved):

```sh
SPECTATE_RENDER_FD=tty SPECTATE_TRANSCRIPT=/tmp/play.jsonl \
python3 test/agent/driver.py play \
    --runner test/agent/spectate.py \
    --worker "$PWD/src/nethack" --data /tmp/agent-data \
    --sysconf /tmp/agent-data/sysconf --private-root /tmp/spectate-play
```

Here `spectate.py` *is* the driver's `--runner`, so the driver builds the
launcher argv and the tool's own options come from `SPECTATE_*` environment
variables (`SPECTATE_LAUNCHER` overrides the launcher path, which defaults to
`<repo>/src/nethack-agent`). `SPECTATE_RENDER_FD=tty` opens `/dev/tty`
independently, so frames reach the terminal even though the driver pipes the
wrapper's stderr; with no controlling terminal the frames fall back to
isolated writes on fd 2, with a one-time note. The same shape runs `breadth` or
any other subcommand the driver drives.

The default live channel is the logical fd 2. Launcher diagnostics share it,
so frames and diagnostics can interleave, and a launcher that writes to an
undrained stderr can block on it independently of this wrapper; `--render-fd
N` (or `SPECTATE_RENDER_FD=N`, plus `N>file` on the command line) gives frames
a dedicated sink and avoids both. **Live rendering rejects fd 1** — that is the
consumer's stdout, and rendering there would corrupt the wire — as does any
obvious alias of that stdout (a shared pipe or regular file). Replay keeps
fd 1 as its valid default. An integer fd is duplicated and never has its own
flags changed, so a caller's non-blocking descriptor stays non-blocking and is
never repurposed.

Insert it in front of an external player (an LLM harness that speaks the wire),
giving the options explicitly on the command line:

```sh
mkdir -p /tmp/spec
python3 test/agent/spectate.py wrap --launcher "$PWD/src/nethack-agent" \
    --transcript /tmp/harness.jsonl --render-fd tty -- \
    --worker "$PWD/src/nethack" --private-root /tmp/spec \
    --data /tmp/agent-data --sysconf /tmp/agent-data/sysconf
```

A harness configured with the wrapper as its runner uses the same implicit
form, `spectate.py <launcher args...>`. In implicit (runner) mode every
remaining argument is forwarded to the launcher verbatim — spectate parses no
options there, so the tool's own settings come from `SPECTATE_*` alone. A
launcher whose first argument is literally `wrap`, `replay`, `selftest`,
`help`, `-h` or `--help` needs the explicit `wrap --` form.

Replay a saved transcript with no runner at all — full speed by default, paced
with `--replay-speed FRAMES-PER-SECOND`:

```sh
python3 test/agent/spectate.py replay /tmp/play.jsonl --no-color
python3 test/agent/spectate.py replay /tmp/play.jsonl --replay-speed 8
python3 test/agent/format_obs.py --raw /tmp/play.jsonl \
    | python3 test/agent/spectate.py replay -
```

Options (wrap/replay, and the same names as `SPECTATE_*` in runner mode):
`--transcript FILE` (verbatim runner-side JSONL), `--render-fd N|tty`,
`--no-color` / `--color`, `--messages N` (default 3), `--min-frame-interval S`
(default 0.15 — observations arriving faster than this coalesce to the newest
frame, redrawn in place on a TTY and appended otherwise), `--replay-speed S`
(frames per second, or `instant`, the default) and `--quiet`. Replay assembles
chunked records through the same strict incremental decoder as `format_obs.py`
and applies no live coalescing, so every saved record is rendered in order.

The environment variables actually read are `SPECTATE_LAUNCHER`,
`SPECTATE_TRANSCRIPT`, `SPECTATE_RENDER_FD`, `SPECTATE_MESSAGES`,
`SPECTATE_MIN_FRAME_INTERVAL`, `SPECTATE_REPLAY_SPEED`, `SPECTATE_NO_COLOR` and
`NO_COLOR`. `--quiet` (and `-q`) is a wrap/replay command-line flag only; there
is no `SPECTATE_QUIET` or `SPECTATE_COLOR`. Numeric settings are validated
before the launcher is spawned: a bad value is a usage error (exit 2), not a
traceback. Rendering that fails where no diagnostic can be delivered returns a
nonzero status rather than a clean success, while a reportable degrade leaves
the launcher's own exit status intact.

Sessions own heap allocations. Release every session with `agent_session_free`
on shutdown and before reinitializing it, or the retained response stream and
the accepted-action bytes leak.

## Action acceptance is two-phase

`agent_receive` frames and parses a line, applies every protocol-level check
(schema, ranges, outstanding request id, kind, the pinned menu generation, the
advertised position rectangle, the advertised line/extcmd byte budget, page
completeness), and returns the action *without* recording it. The caller
validates semantically (menu contents, yes/no semantics) and then calls
`agent_accept`, which takes **no action argument** and records the
session-owned pending identity only. A semantically rejected action therefore
leaves the request outstanding and may be resubmitted with the same request id.

`agent_session.force_chunk` makes the encoder take the chunk path even when a
record would fit one line, so the same logical record can be produced and
compared both ways.

## Regenerating the profile manifest and the enforcement header

```sh
python3 test/agent/gen_profile.py > doc/agent-profile-v1.tsv
python3 test/agent/gen_profile.py --emit-c > win/agent/agent_profile.h
```

`test/agent/gen_profile.py` is the single source of truth for both artifacts.
`doc/agent-profile-v1.tsv` is the human-readable frozen manifest; the agent-only
build also consumes a machine form of the same data, `win/agent/agent_profile.h`,
which is **checked in** and generated by `--emit-c`.  The header lists exactly
the options whose frozen value differs from this revision's compiled default, so
the engine's own initialization pins everything else.  Both files are verified
by `make -C test/agent check` (targets `manifest-check` and `header-check`), so a
hand-edit or drift from the generator fails the gate rather than shipping.

The manifest has one row per active `optlist.h` entry plus sections for the 16
native color slots, the status conditions, the complete `WC_`/`WC2_` capability
table, the standard binding inventory, and non-optlist symbol state.  Boolean
options carry their compiled default; compound options carry their resolved
startup value (never a placeholder). Its columns are: option name, availability
guard, resolved startup value, classification, setter/handler entry point,
saved-field binding, rationale. Rows behind a guard that is inactive in this
build — and the two blocks that are compiled out entirely — are marked
`unavailable-external`.

## Save-artifact provenance (controller metadata)

A native save crosses three trust boundaries — the worker writes it, the
launcher moves it, and a later restore feeds it back — so the trusted launcher
binds each exported artifact to the inputs that produced it. This record lives
in the **controller-owned export directory** the test controller passes to
`--save-out`, never on the player channel, and the worker never reads it.

On **export** (`--save-out DIR`), the launcher writes `provenance.txt`
alongside the artifact after copying it into a fresh staging path, fsync'ing,
and renaming the staging directory into place. It is a canonical, sorted
`key=value` file:

| key | value |
|---|---|
| `version` | `1` |
| `mode` | the launch mode that produced the artifact (`new`) |
| `profile` | the frozen rendering profile name |
| `owner-uid` | the uid that produced the artifact |
| `worker-sha256` | digest of the worker executable |
| `data-nhdat-sha256`, `data-license-sha256`, `data-symbols-sha256` | digest of each staged immutable data file |
| `sysconf-sha256` | digest of the trusted sysconf |
| `save-name` | the artifact's file name |
| `save-sha256` | digest of the artifact bytes |

On **restore** (`--restore-in DIR`), the launcher requires
`provenance.txt` in that directory, recomputes every digest against what it is
about to stage, and re-checks the profile, mode, owner scope and artifact name.
An absence, an extra or missing artifact, or any mismatch is a **private
launch failure** (exit 7, no worker started, no player byte). This is a
per-save binding, not a substitute for the native restored-flags validation,
which still runs inside `restore()` on the deserialized save.

The harness stands in for the launcher when it drives a worker directly
(`hostile_matrix.run_worker`), so `hostile_matrix.write_provenance` synthesises
the same record for artifacts the matrix produces itself.

```sh
make -C test/agent lifecycle \
    WORKER="$PWD/src/nethack" RUNNER="$PWD/src/nethack-agent" \
    DATA=/tmp/agent-data SYSCONF=/tmp/agent-data/sysconf
make -C test/agent sentinel \
    WORKER="$PWD/src/nethack" RUNNER="$PWD/src/nethack-agent" \
    DATA=/tmp/agent-data SYSCONF=/tmp/agent-data/sysconf
```

## Explicit limits

* `struct agent_view`, `struct agent_action`, and `struct agent_session` use
  fixture-sized bounds (`AG_VIEW_MAX_*`, `AG_MAX_LINE_BYTES`). Production will
  size the view/palette storage from the real contract; the protocol bounds
  themselves are the ones in `doc/agent-interface.md` section 6.
* `get_page` returns an empty `rows` array in these fixtures. The paging rule
  under test is the ordering rule (per-page delivery tracking, and no
  selection until every required page is delivered), not row content.
* The engine-facing bridge (`win/agent/winagent.c`), the native menu sidecars,
  the launcher, and the build wiring are later milestones (M1/M2/M3). This
  directory proves the frozen contract and the reference state machines, not
  integration.
* A same-user test runner is a convenience harness, not a secure evaluation
  deployment.
