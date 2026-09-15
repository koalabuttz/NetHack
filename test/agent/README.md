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
make -C test/agent check       # manifest + schema + every fixture
make -C test/agent sanitize    # same, under AddressSanitizer + UBSan
make -C test/agent depcheck    # prove no fixture reaches an engine header
make -C test/agent clean
```

`check` is the whole gate.  It first regenerates `doc/agent-profile-v1.tsv`
from `include/optlist.h` and diffs it, so a drifted or hand-edited manifest
fails; it then schema-validates real encoder output plus the built-in grammar
vectors (`schema_check.py`); it then builds and runs every fixture.

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
| `test/agent/gen_profile.py` | regenerates `doc/agent-profile-v1.tsv` from `include/optlist.h`; refuses to emit a row for an option it has no classification for |
| `test/agent/format_obs.py` | canonical client formatter: assembles chunk streams back into logical records (deriving `d` from `rid` and splicing `t` long-text slices) and prints a human-readable projection |
| `test/agent/schema_check.py` | dependency-free JSON Schema subset validator plus 25 positive and 29 negative vectors; also validates real encoder output piped from `test_protocol --dump` |

## Formatter

```sh
python3 test/agent/format_obs.py transcript.jsonl        # human projection
python3 test/agent/format_obs.py --json transcript.jsonl # assembled records
python3 test/agent/format_obs.py --raw transcript.jsonl  # pass lines through
```

It is a client-side debugging aid, not a production component.

## Action acceptance is two-phase

`agent_receive` frames and parses a line, applies every protocol-level check
(schema, ranges, outstanding request id, kind, the pinned menu generation,
the advertised position rectangle, page completeness), and returns the action
*without* recording it.  The caller validates semantically (menu contents,
yes/no semantics) and then calls `agent_accept`.  A semantically rejected
action therefore leaves the request outstanding and may be resubmitted with
the same request id.

`agent_session.force_chunk` makes the encoder take the chunk path even when a
record would fit one line, so the same logical record can be produced and
compared both ways.

## Regenerating the profile manifest

```sh
python3 test/agent/gen_profile.py > doc/agent-profile-v1.tsv
```

The manifest has one row per active `optlist.h` entry plus sections for the 16
native color slots, the status conditions, the complete `WC_`/`WC2_` capability
table, the standard binding inventory, and non-optlist symbol state.  Boolean
options carry their compiled default; compound options carry their resolved
startup value (never a placeholder). Its columns are: option name, availability
guard, resolved startup value, classification, setter/handler entry point,
saved-field binding, rationale. Rows behind a guard that is inactive in this
build — and the two blocks that are compiled out entirely — are marked
`unavailable-external`.

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
