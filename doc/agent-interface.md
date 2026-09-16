# Agent Interface v1 — Wire, Profile, and Callback Disclosure Specification

This document is the frozen Phase 1 (P1) contract for the NetHack agent window
port. It is normative for the encoder, the reference state model, the menu
model, and the standalone fixtures under `test/agent/`. It derives from
`doc/agent-architecture.md` (the design spec) and `doc/agent-impl-plan.md` (the
handoff plan). Where this document and the plan disagree, the design spec wins;
the plan's stricter reading is used when the spec is silent.

P1 deliberately builds **no engine dependency**. Everything specified here is
implemented and tested with plain C99 that includes only `<stdint.h>`,
`<stddef.h>`, `<stdbool.h>`, `<stdlib.h>`, and `<string.h>`. No translation unit
under `win/agent/` or `test/agent/` includes `hack.h` or any `include/` header.

Sections:

1. Versions, channels, and trust boundary
2. Public appearance and normalization
3. Profile and delivery policy
4. Callback disclosure table
5. Wire records
6. Bounds and constants
7. Chunk part grammar
8. Transport auxiliaries
9. Durable state machine
10. Transient (audit) state machine
11. Action grammar
12. Menu contract
13. Counter discipline and the closed-counter exception
14. Repeated-menu mapping clarification
15. Capability negotiation and Phase 2 exclusions
16. Forbidden keys and shapes
17. Conformance vectors

---

## 1. Versions, channels, and trust boundary

Two independent, versioned choices exist.

| Choice | Identifier | Meaning |
|---|---|---|
| Rendering profile | `normal-ascii-color-v1` | ordinary fixed ASCII/color rendering and input semantics |
| Delivery policy | `llm-final-v1` (default) | final durable deltas at decision boundaries, complete messages/prompts/menus/text, no nonblocking animation |
| Delivery policy | `audit-frames-v1` (opt-in) | additionally negotiates the `ev` transient capability |

The wire is UTF-8, newline-delimited, compact JSON. One JSON object per physical
line. No BOM. No comments. No trailing content after the object on a line. The
line terminator is a single `\n` (0x0A); a `\r` before it is not accepted.

There are exactly two logical channels:

* `ch:"player"` — observations, menus, messages, prompts, actions. Only
  presentation-derived values appear here (section 4).
* `ch:"control"` — `hello`, `closed`, and the transport auxiliaries `page`,
  `chunk`, `invalid`.

Privileged evaluator control (seeds, outcomes, saves, metrics, reset) is **not**
a third `ch` value and is not reachable on this interface. It lives on a
separate descriptor owned only by the trusted supervisor.

The agent is untrusted. It receives only the projection defined here. It never
receives engine objects, raw glyph ids, tile or species indices, background-map
layers, native menu identifiers, RNG state, seeds, PIDs, or diagnostic text.

## 2. Public appearance and normalization

### 2.1 Cell tuple

A public cell is a 4-tuple `[char, color, style, frame]`:

* `char` — a single printable ASCII character (`0x20`–`0x7E`) as a JSON string.
  The built-in ASCII/primary-Rogue symbol set is enforced; non-ASCII symbol
  fallbacks are not published.
* `color` — a public basic color name, or `"none"`.
* `style` — a single JSON integer, the OR of the public style bits below.
* `frame` — a public basic color name rendered as a background/frame color, or
  `"none"`.

Public style bits: `bold=1`, `dim=2`, `italic=4`, `underline=8`, `blink=16`,
`inverse=32`.

Native `ATR_*` values are **not** this mask and must be normalized. Native
`HL_*`/`MG_*` masks never cross the boundary.

### 2.2 Basic color names (frozen from `include/color.h`)

The public color table is exactly the 16 native basic-color slots, in native
numbering. The slot-8 name is taken from the native header, not invented.

| Native macro | Slot | Public wire name |
|---|---|---|
| `CLR_BLACK` | 0 | `black` |
| `CLR_RED` | 1 | `red` |
| `CLR_GREEN` | 2 | `green` |
| `CLR_BROWN` | 3 | `brown` |
| `CLR_BLUE` | 4 | `blue` |
| `CLR_MAGENTA` | 5 | `magenta` |
| `CLR_CYAN` | 6 | `cyan` |
| `CLR_GRAY` | 7 | `gray` |
| `NO_COLOR` | 8 | `none` |
| `CLR_ORANGE` | 9 | `orange` |
| `CLR_BRIGHT_GREEN` | 10 | `brightgreen` |
| `CLR_YELLOW` | 11 | `yellow` |
| `CLR_BRIGHT_BLUE` | 12 | `brightblue` |
| `CLR_BRIGHT_MAGENTA` | 13 | `brightmagenta` |
| `CLR_BRIGHT_CYAN` | 14 | `brightcyan` |
| `CLR_WHITE` | 15 | `white` |

There is no separate "no color" public value: slot 8 already *is* `NO_COLOR`,
so `"none"` is the native "no color" and is used for both a missing foreground
and a missing frame. Blank palette entry 0 is always
`[" ","none",0,"none"]`.

### 2.3 Normalization and precedence (`agent_normalize_appearance`)

The sanitizer receives a synthetic `struct agent_render_input` containing only
the candidate character, candidate basic foreground color, candidate basic
frame color, and *already reduced* display booleans. It never receives a glyph
number, tile index, symbol index, pointer, or an entire `MG_*` mask; the
engine-facing bridge is responsible for computing the reduced booleans.

Precedence follows the tty renderer's *displayed* result, not its reasons:

1. If the character is outside `0x20`–`0x7E`, normalization fails
   (`AG_BAD_INPUT`). Non-ASCII is never published.
2. If the foreground slot is outside `0`–`15`, normalization fails.
3. Frame color, when present (not `none`), has precedence over pet highlighting:
   the pet attribute is dropped and only the frame color survives.
4. Otherwise, in the **map** context, a pet-attribute candidate, a pile or
   detection candidate, or a black-and-white inverse candidate each set the
   `inverse` style bit. Wizard-only reasons (for example female highlighting)
   are never applied and never visible, so they cannot influence the result.
5. In the **menu** context, map-only pet/detection/inverse candidates are
   ignored: menu icons use menu-displayed icon rules. A menu row with no
   displayed icon carries a null icon rather than a synthesized one.
6. Background-terrain identity and symbol are discarded unconditionally. Only a
   frame color that the fixed profile actually displays may survive.

Two different raw inputs that display identically must produce byte-identical
public tuples. This is a P1 gate vector.

## 3. Profile and delivery policy

The rendering profile is exactly `normal-ascii-color-v1`. The default delivery
policy is exactly `llm-final-v1`. Both are frozen in
`doc/agent-profile-v1.tsv`, which enumerates every active option for this build,
the 16 color slots, every status condition, the capability bits, and the
non-option symbol/binding state.

Policy summary (details and per-row rationale in the manifest):

* Force the agent window; normal play mode only (debug, explore, and fuzzer
  false).
* Primary/Rogue built-in ASCII symbols; color on; inverse on; pet and pile
  highlighting on with pet attribute inverse.
* No custom symbols or colors, no Unicode glyph handlers, no tiles, no hidden
  background terrain layer, no permanent inventory, no mouse, no sound, no
  shell/suspend/pager/editor/mail/Lua user hooks, no alternate-meta
  interpretation, no cross-episode bones.
* Standard native bindings; `number_pad` off.
* Displayed game time on; optional score/experience-point total/version/
  weapon/armor/terrain status fields off; hit-point bar and configurable status
  highlighting off. Ordinary enabled base fields and XP/HD applicability are
  preserved and not forced merely because a callback exists.
* Conditions: the default enabled subset is frozen (section 4.4). No status
  color or style override is applied to condition text.
* Capabilities: `WC_COLOR | WC_HILITE_PET | WC_INVERSE | WC_EIGHT_BIT_IN`, plus
  `WC2_FLUSH_STATUS | WC2_RESET_STATUS`. All 16 basic colors are supported.
* Runtime option mutation is disallowed in this round. The allowlist is empty.
  Choosing native gameplay actions is not option mutation.

## 4. Callback disclosure table

`win/agent/winagent.c` (M1/M2, out of P1 scope) implements the port's callbacks.
Every callback is classified by what may be published. The classification is
binding on the implementer; P1 only freezes it.

Legend: **P** = publishable player presentation; **T** = transport/control only;
**D** = dropped or private; **N** = no-op.

| Callback | Class | Published projection |
|---|---|---|
| `init_nhwindows` | T | agent/terminal-free startup; no player text |
| `player_selection` | P | character-selection menus/line/yn only |
| `askname` | P | line prompt via native name rules |
| `get_nh_event` | N | event pump; never sleeps |
| `exit_nhwindows` | T | not proof of save/death/victory |
| `suspend_nhwindows`/`resume_nhwindows` | D | policy-denied; never SIGSTOP |
| `create_nhwindow` | T | public window id assignment |
| `clear_nhwindow` | P | explicit clear in window content |
| `display_nhwindow` | P | text content; blocking form is a boundary |
| `destroy_nhwindow` | P | explicit close event |
| `curs` | P | displayed cursor position (may be target, not hero) |
| `putstr` | P | rendered text lines and style |
| `putmixed` | P | decoded-to-displayed symbols only; raw escape payloads never |
| `display_file` | P | trusted DLB/data text into a text window |
| `start_menu` | P | new menu generation; prior row mapping discarded |
| `add_menu` | P | copied identifier/preselection/flags + displayed content |
| `end_menu` | P | menu title/prompt |
| `select_menu` | P | final selection set (public row ids/counts only) |
| `message_menu` | P | displayed text |
| `mark_synch`/`wait_synch` | N | nonblocking; never sleeps |
| `cliparound` | N | no clipping advertised |
| `update_positionbar` | N | no position bar advertised |
| `print_glyph` | P | two cells normalized by §2.3; raw identity dropped |
| `raw_print`/`raw_print_bold` | D | private and fail-closed in agent mode |
| `nhgetch` | P | key byte 1..255 |
| `nh_poskey` | P | native position primitive; no mouse claim |
| `nhbell` | D | no bell |
| `doprev_message` | P | restored history, tagged as history |
| `yn_function` | P | visible choices only (section 4.2) |
| `getlin` | P | displayed prompt, bounded UTF-8 line |
| `get_ext_cmd` | P | text resolved by the native exact matcher |
| `number_pad` | N | ignored; locked profile value used |
| `delay_output` | N | never sleeps; no observation |
| `change_color` | D | denied; no color mutation |
| `get_color_string` | D | stable supported default only |
| `outrip` | P | only if rendered through ordinary callbacks |
| `preference_update` | D | runtime mutation denied |
| `getmsghistory`/`putmsghistory` | D | private history plumbing |
| `status_init`/`status_finish` | T | batching, not observations |
| `status_enablefield` | P | enabled/disabled displayed field |
| `status_update` | P | changed displayed fields only (section 4.3) |
| `can_suspend` | D | false |
| `update_inventory` | N | no permanent inventory advertised |
| `ctrl_nhwindow` | T | ABI-approved unchanged/unsupported response only |

### 4.1 Batching is not observation

`BL_FLUSH` and `BL_RESET` are presentation batching operations. They do not
advance a durable sequence and do not create searchable content by themselves.
`display_nhwindow(WIN_MAP, FALSE)`, `BL_FLUSH`, and delay callbacks do not alone
advance `seq` (section 9).

### 4.2 Hidden prompt suffixes

`yn_function` receives a `choices` string in which an embedded native Escape
(`0x1b`) separates the *displayed* choices from an *undisplayed* accepted
suffix. The public choices field stops before the first Escape and is `null` for
an unrestricted prompt. The displayed default byte is **public** (tty shows it
in the prompt and returns it on Escape, so `default` is player presentation);
the native acceptable set beyond the visible prefix, Escape handling, and
`yn_number` remain private.

`agent_visible_choices` performs this projection: it copies the prefix before
the first `0x1b` or `0x00`, rejects bytes outside `0x20`–`0x7E`, and rejects
overflow. It never exposes the hidden suffix.

### 4.3 Status

Only **enabled, displayed** fields are published, as normalized strings plus
visible style. Native enablement logic is preserved:

```
BL_SCORE   -> showscore          BL_TIME -> time
BL_EXP     -> showexp && !Upolyd BL_XP   -> !Upolyd
BL_HD      -> Upolyd             BL_VERS -> showvers
BL_WEAPON  -> weaponstatus       BL_ARMOR -> armorstatus
BL_TERRAIN -> terrainstatus      (all other base fields -> enabled)
```

Generic pointers, hidden percentages, and the raw condition bitmask are never
serialized. Conditions are published as an ordered array of displayed
`{text,color,style}` entries (section 4.4), never as bits.

### 4.4 Condition display

Native condition text (`txt1`, before its abbreviation fallback) and ranking
come from `src/botl.c`. The comparator orders by ascending ranking, then by the
visible condition option name. The frozen default-enabled subset is:

`blind, conf, deaf, iron, fly, foodPois, grab, hallucinat, lava, levitate,
ride, slime, stone, strngl, stun, termIll`.

All other listed conditions are off. Disabled or false conditions are not
published. Condition text is published with `color:"none"` and `style:0`; the
profile applies no status color or style override.

Frozen ranking/text (text is the first native string):

| option | rank | text |
|---|---|---|
| `barehanded` | 20 | Bare |
| `blind` | 10 | Blind |
| `busy` | 20 | Busy |
| `conf` | 10 | Conf |
| `deaf` | 10 | Deaf |
| `iron` | 15 | Iron |
| `fly` | 10 | Fly |
| `foodPois` | 6 | FoodPois |
| `glowhands` | 20 | Glow |
| `grab` | 2 | Grab |
| `hallucinat` | 10 | Hallu |
| `held` | 20 | Held |
| `ice` | 20 | Icy |
| `lava` | 8 | InLava |
| `levitate` | 10 | Lev |
| `paralyzed` | 20 | Parlyz |
| `ride` | 10 | Ride |
| `sleep` | 20 | Zzz |
| `slime` | 6 | Slime |
| `slip` | 20 | Slip |
| `stone` | 6 | Stone |
| `strngl` | 4 | Strngl |
| `stun` | 10 | Stun |
| `submerged` | 15 | Submrg |
| `termIll` | 6 | TermIll |
| `tethered` | 20 | Teth |
| `trap` | 20 | Trap |
| `unconscious` | 20 | Out |
| `woundedlegs` | 20 | WLegs |
| `holding` | 20 | UHold |

### 4.5 Coordinates

Legal public map coordinates are `x = 1..79`, `y = 0..20`. Column zero is
unused. The map is described by `"coord":"engine-map"`, `"size":[80,21]`,
`"x0":1`, `"y0":0` in `hello`. The full logical map corresponds to an
adequately sized human display; there is no artificial viewport.

## 5. Wire records

There are four principal logical records plus transport auxiliaries.

### 5.1 hello (control, emitted once)

```json
{"v":1,"ch":"control","type":"hello","d":1,"profile":"normal-ascii-color-v1","policy":"llm-final-v1","caps":["snapshot","menu","paging"],"coord":"engine-map","size":[80,21],"x0":1,"y0":0,"limits":{"line":65536,"page_bytes":16384,"page_rows":128,"count":2147483647}}
```

`d` is the delivery counter (section 13). `limits` are fixed contract constants,
not current resource use. hello carries no build hash, PID, seed, worker id,
version-string fingerprint, or diagnostic field. It is emitted once, after
compatible startup or restore readiness and before any player payload.

### 5.2 obs (player)

Fields: `v, ch, type, d, seq, base, s, cond, pal, map, cur, msg, hist, windows, need`.

```json
{"v":1,"ch":"player","type":"obs","d":2,"seq":1,"base":null,"s":{"time":{"text":"42","color":"none","style":0},"hitpoints":{"text":"11","color":"none","style":0}},"cond":[{"text":"Blind","color":"none","style":0}],"pal":[[0," ","none",0,"none"],[1,"@","white",32,"none"],[2,".","gray",0,"none"]],"map":[[8,10,1],[8,11,2]],"cur":[8,11],"msg":[{"e":1,"text":"You hear someone counting money.","style":0}],"hist":[],"windows":[],"need":{"id":1,"kind":"command"}}
```

* `seq` versions durable presentation; the first `seq` is 1 and it increments
  only at a decision/blocking/final boundary.
* `base` names the required predecessor durable version; Phase 2 emits
  `base:null` for every full snapshot. `base:null` means the record is a full
  snapshot containing all durable state, the palette, the current request, and
  content references.
* `s` maps a canonical status field name to `{text,color,style}`, or to `null`
  to delete the field. In reserved delta mode only changed fields appear; in
  Phase 2 full snapshots every enabled field appears. The canonical names are
  the native status field names: `title, strength, dexterity, constitution,
  intelligence, wisdom, charisma, alignment, score, carrying-capacity, gold,
  power, power-max, experience-level, armor-class, HD, time, hunger, hitpoints,
  hitpoints-max, dungeon-level, experience, condition, version, weapon, armor,
  terrain`.
* `cond` is the ordered array of displayed condition entries `{text,color,style}`,
  ordered by ascending native ranking then visible condition option name. It is
  the plan's "ordered array of displayed entries, not bits" and is separate from
  `s` because a condition entry is a list element, not a `{text,color,style}`
  scalar field value.
* `pal` is an array of `[id,char,color,style,frame]` palette definitions.
  Palette id 0 is always `[0," ","none",0,"none"]`.
* `map` is an array of `[x,y,palette_id]` triples, row-major. **Blank cells are
  omitted (sparse).** An unpainted cell is the declared blank appearance, so a
  full snapshot enumerates only cells whose palette id is not 0; a cell absent
  from `map` is palette id 0, the blank tuple. Sparse omission, not a complete
  grid, is the normative blank-cell representation.

  The adapter stores the native 80-column map array indexed by native
  coordinate and **emits the stored coordinate unchanged**: it never applies an
  offset. Native column zero is unused and is never emitted, so a published
  `x` is always in `1..79` and a published `y` always in `0..20`. The cursor
  follows the same convention; a cursor outside those ranges is refused.
* `cur` is `[x,y]` or `null`. Cursor removal is explicit.
* `msg` is an ordered array of message/presentation events `{e,text,style}`.
  `e` is a presentation-derived event id, never an engine identifier.
* `hist` is an ordered array of restored-history text entries, same `{e,text,style}`
  discipline, tagged separately from new events.
* `windows` is an ordered array of content descriptors (section 12.4).
* `need` is `null` at a final boundary or a tagged request (section 11.1).

Every full observation carries the complete fixed field set
`s, cond, pal, map, cur, msg, hist, windows, need`, in that order. An empty
collection is present and empty (`"s":{}`, `"map":[]`), never omitted; a
chunked full snapshot that enumerates no element of an array means that array
is empty. `need:null` is legal in both the plain and the chunked form.

Message/request/window ids are presentation-derived counters, never native
`winid`s or file paths.

### 5.3 act (player input)

```json
{"v":1,"type":"act","seq":19,"id":20,"action":{"key":104}}
```

Key and yn answers are byte integers in `1..255` (`104` is `h`), never strings;
the schema and the fixtures both enforce that.

`seq` is the durable version the action acknowledges (optional; a following
valid action may acknowledge implicitly). `id` is the public request id the
action answers. `action` is exactly one tagged shape (section 11.2).

### 5.4 closed (control, terminal)

```json
{"v":1,"ch":"control","type":"closed"}
```

Exactly these three keys and no others. See section 13 for why this record has
no `d`.

### 5.5 Additional control records

`chunk` (section 7), `page` (section 8), and `invalid` (section 8).

## 6. Bounds and constants

| Constant | Value | Meaning |
|---|---|---|
| protocol version | 1 | `v` |
| max physical line | 65536 bytes | including the terminating LF |
| input nesting | 8 | max JSON container depth |
| max object keys | 32 | per object |
| max tokens | 32768 | per physical line |
| max action bytes | 65535 | serialized `action` |
| key byte | 1..255 | 0 rejected except a native position sentinel |
| line input | `min(255, destcap-1)` UTF-8 bytes | advertised per request |
| concurrent windows | 32 | |
| menu rows / text lines | 65535 | per menu or per text window |
| individual text value | 1048576 bytes | split at UTF-8 boundaries |
| retained public state | 33554432 bytes | content + unacked spool per worker |
| content page | 128 rows and 16384 bytes | whichever is smaller |
| outstanding gameplay request | 1 | |
| counters | 1..2^53-1 | uint64_t internally; close before wrap |
| menu counts | -1 or 1..2147483647 | additionally `<= LONG_MAX` |
| content pages | at most 65535 | zero-based indices `0..65534` |
| line/extcmd answer | the advertised `max` | tested over decoded UTF-8 bytes |

These are local engineering bounds, not measured production budgets. Input
errors do not consume input; internal or resource failures close generically.

## 7. Chunk part grammar

A large logical record is an ordered stream of `chunk` records, never partial
independently applicable observations. Outer fields:

```json
{"v":1,"ch":"control","type":"chunk","d":7,"rid":7,"i":0,"last":false,"parts":[{"p":"h","k":"seq","val":4}, ...]}
```

* `rid` is the logical output record id — the delivery id of the first chunk.
* each physical chunk has its own monotonically assigned `d`.
* retries preserve `d` and the exact bytes.
* `parts` is an array of tagged path/value pieces.

Enumerated part paths for v1 (no arbitrary JSON-pointer patch language):

| `p` | Shape | Path | Notes |
|---|---|---|---|
| `h` | `{"p":"h","k":K,"val":V}` | header scalar `K` | only in chunk 0; `K` ∈ {v,ch,type,seq,base} |
| `s` | `{"p":"s","k":N,"val":{...}\|null}` | one status member | `N` is a canonical field name |
| `cond` | `{"p":"cond","val":{...}}` | one condition entry | array element, never split |
| `pal` | `{"p":"pal","val":[...]}` | one palette definition | array element |
| `map` | `{"p":"map","val":[x,y,id]}` | one map triple | array element |
| `msg` | `{"p":"msg","val":{...}}` | one message event | array element |
| `hist` | `{"p":"hist","val":{...}}` | one history event | array element |
| `win` | `{"p":"win","val":{...}}` | one window descriptor | array element |
| `cur` | `{"p":"cur","val":[x,y]\|null}` | cursor | scalar |
| `need` | `{"p":"need","val":{...}\|null}` | request | scalar |
| `t` | `{"p":"t","k":K,"e":E\|"w":W,"f":F,"offset":O,"text":S,"last":B}` | long text | `K` ∈ {msg,hist,win}; see below |

Header parts carry **only** `v`, `ch`, `type`, `seq`, and `base`. The logical
record's own delivery counter is not a header part: it is `rid`, the delivery
counter of the first chunk. Each physical chunk has its own `d`.

Header scalars appear in part 0. `map`, `pal`, `msg`, `hist`, `cond`, `s`, and
`win` arrays are split only *between* elements. Splits never cut a UTF-8
sequence or a scalar value ambiguously.

### 7.1 Long-text parts

A field value longer than the physical budget is addressed unambiguously by
element and field identity, so several long strings can be in flight at once:

* `k` names the element kind: `msg`, `hist`, or `win`.
* `e` is the presentation event id for `msg`/`hist`; `w` is the window id
  (`"wN"`) for `win`. Exactly one of the two is present.
* `f` is the field being spliced: `text` for `msg`/`hist`, `title` for `win`.
* `offset` is the UTF-8 byte offset of this slice within the whole field value.
* `text` is the slice itself; `last` is true only on the final slice.

The element part that introduces a spliced field carries that field as an empty
string (`"text":""` / `"title":""`). A client concatenates the `t` slices for a
given `(k, e|w, f)` in ascending `offset` order and installs the result in that
element's field. Slices are cut only at UTF-8 scalar boundaries, so every slice
is independently valid UTF-8; `offset` values are contiguous and the final
slice is marked `last:true`.

The client assembles and validates the entire logical record before atomic
application. Identical retry chunks are deduplicated by `(rid,i)`; changing
content under an existing id is a protocol failure.

Stable menu/text page content may be sent as separate `page` records; selections
are forbidden until every required page has been delivered and acknowledged.

## 8. Transport auxiliaries

Inputs (strict control allowlist):

* `{"v":1,"type":"get_page","id":N,"content":"cN","page":K}`
* `{"v":1,"type":"ack_chunk","rid":R,"i":K}`
* `{"v":1,"type":"ack_seq","seq":N}`

Outputs:

* `page` — a stable content slice `{v,ch:"control",type:"page",d,content,page,pages,rows:[...]}`.
* `chunk` — section 7.
* `invalid` — `{"v":1,"ch":"control","type":"invalid","d":D,"code":C}` where `C`
  is one of `schema`, `stale`, `kind`, `range`, `incomplete`. `invalid` carries
  no diagnostic text and leaves the outstanding request unchanged.

`resync` is reserved for later advertised support and is not emitted or accepted
in Phase 2. Unknown top-level schemas produce `invalid(schema)` or a generic
close for framing exhaustion. Conflicting accepted-action id reuse closes; an
accepted action is never executed twice.

Every auxiliary record is parsed by one strict object parser: the key set must
be exactly the one listed above for that record type, duplicate keys are
rejected, `v` must be exactly 1, every integer is bounded, and no trailing
content is permitted.

### 8.1 Page delivery and acknowledgement

`get_page` is **its own acknowledgement**. A page counts as delivered when its
`page` response has been sent in answer to a `get_page` request; there is no
separate page-ack record. Consequences:

* Delivery is tracked per page index in a bounded per-request bitmap. Only the
  *first* delivery of a given index counts; a repeated `get_page` for an
  already-delivered page is an idempotent retry that re-sends the page without
  changing the delivered set.
* Two requests for page 0 of a two-page menu therefore deliver one page, not
  two. A selection remains `invalid(incomplete)` until every required page index
  has been delivered.
* `get_page` must name the outstanding request id and that request's content
  id, and its page index must lie within the declared page count; otherwise it
  is rejected without changing any delivery state.
* A page index is 0-based and at most `65534`; a request may declare at most
  `65535` pages.
* The delivered bit is set only **after** the `page` response has been written,
  so a failed emission cannot mark a page as delivered.

### 8.2 Chunk acknowledgement

`ack_chunk` is cumulative and contiguous, with an explicit **none** state. The
first acknowledgement of a stream must name index 0; a repeat of an
acknowledged index is idempotent, the next index advances the acknowledgement,
and an index beyond `acknowledged + 1` is a gap and is rejected as
`invalid(incomplete)`. The acknowledgement high-water belongs to one stream:
it is reset whenever the logical record id changes, so a fresh stream cannot
inherit a previous stream's high-water and its first acknowledgement is index 0
again. The rid must be the most recent chunk stream and the index must lie
inside it. Chunk acknowledgement is transport bookkeeping: it is not input, not
a durable commit acknowledgement, and not permission to advance a prompt.

## 9. Durable state machine

Three distinct concepts are maintained:

1. **Working renderer `W`** — latest sanitized callback presentation, updated
   during execution.
2. **Durable snapshot `D_n`** — complete presentation at a publication/decision
   boundary, with durable version `n` and durable palette `P_n`.
3. **Transport delivery state** — which immutable records/chunks the client
   acknowledged. Delivery never mutates `D_n`.

A durable boundary occurs at an unsatisfied input request, a blocking display
requiring acknowledgement, or final ordinary player-facing completion.
Nonblocking `display_nhwindow`, `BL_FLUSH`, and delay callbacks do not alone
advance `seq`.

At a boundary, freeze `W` as `D_(n+1)` and encode differences **against `D_n`**,
not against a transient frame or the last socket write. New complete
prompt/message events are always recorded, even when the durable map is
unchanged. Every patch is an absolute replacement. Client application is atomic
after validating the whole logical commit; a duplicate version is not applied
again.

A full snapshot uses `base:null`. Initially unpainted cells equal the declared
blank appearance; hidden stone and unexplored are not distinguished when both
are blank. Blank cells are omitted from `map` (section 5.2): a cell absent from
the snapshot is palette id 0.

New durable palette ids are assigned at commit, from newly used durable tuples
in deterministic public presentation order: row-major over the durable map,
then displayed content order. A tuple seen only in an omitted animation must
not consume a durable palette id. Definitions and their references become
durable atomically in the same commit. Ids are never reused for a different
tuple within an episode.

Resynchronization snapshots come only from `D_n`, never by scanning game state.
New or restored workers begin with a full snapshot and empty protocol history.

## 10. Transient (audit) state machine

`ev` is a negotiated capability, not an optional field whose absence
ambiguously means lost data. `llm-final-v1` does not negotiate it. Audit mode
uses ordered successor patches on a separate scratch view, never mutations of
durable state.

For an execution interval starting at durable version `n`:

1. Emit `begin(interval,base:n)`. Initialize scratch view `T_0` from `D_n`.
2. Each rendering checkpoint produces `frame(interval,k,prev:k-1,patch)`.
   Applying it replaces cells/styles in `T_(k-1)` to form `T_k`. Map
   checkpoints are captured at all map displays, including direct calls;
   delay checkpoints only when the normalized visible state changed.
3. Identical normalized frames are suppressed. Callback counts, raw repaint
   order, wall-clock durations, and empty delays are never exported.
4. Transient cell patches carry inline normalized appearance tuples in v1. They
   do not allocate or redefine durable palette ids.
5. At the next durable boundary, emit `end(interval,last:k,target:n+1)` and the
   durable commit. The client applies the durable patch against `D_n`, then
   explicitly discards its transient scratch view and displays `D_(n+1)`. If
   the last transient view differs, this reset is an explicit visible final
   restoration, not an implied no-op.

A projectile moving across cells includes the source restoration and the next
projectile cell in successive patches. If the final map equals the pre-action
map, the durable map delta can be empty while the audit stream still contains
all movement and restoration frames.

Transport rules for chunks, acknowledgement, retries, and resync are in
sections 7, 8, and 13.

## 11. Action grammar

### 11.1 Requests

Request common fields: `{id, kind}`. Types:

* `command`, `key`, `direction` — optional displayed prompt; byte answers only
  in Phase 2. Direction is a context label, not direct engine mutation.
* `position` — displayed prompt and legal map bounds; answer byte or explicit
  position primitive only where the native `getpos` accepts it.
* `yn` — prompt, visible choices (prefix before Escape; `null` for
  unrestricted), default (byte or `null`); numeric true only if displayed
  choices include a numeric affordance.
* `line`, `extcmd` — displayed prompt and max byte count; extcmd answers are
  text resolved privately with the native exact matcher. The advertised `max`
  is persisted with the request and enforced at the protocol layer over the
  **decoded UTF-8 byte length** of the answer, so an answer longer than `max`
  bytes is rejected (leaving the request outstanding) even when it contains
  fewer code points.
* `menu` — `{id,kind:"menu",menu:"mN",mode:"none|one|any",content:"cN",pages:N}`.
* `ack` — `{id,kind:"ack",content:"cN",pages:N}`; all required content must be
  published before acknowledgement is accepted.

### 11.2 Action shapes

`action` is exactly one of:

| Shape | Meaning |
|---|---|
| `{"key":B}` | one key byte `B` in 1..255 |
| `{"text":S}` | bounded UTF-8 text |
| `{"position":[x,y],"mod":M}` | explicit position; `M` is frozen to 0 |
| `{"yn":B,"count":C}` | yes/no byte, optional count |
| `{"menu":"mN","commit":[[r,count],...]}` | final menu selection set |
| `{"cancel":true}` | cancellation |
| `{"ack":true}` | acknowledgement of display-only content |

An explicit position must satisfy `x` in `1..79` and `y` in `0..20` on the wire.
Column zero is the internal native sentinel and is never accepted; the
accumulated native modifier must be `0`. In addition, a position answer must
lie inside the rectangle the outstanding request advertised (`x0,y0,x1,y1`),
and a menu answer must name the generation id (`"mN"`) that request pinned;
both bounds sets are persisted with the request and checked at the protocol
layer before the action is offered to the caller.

No `then`, `group`, `selectall`, `invert`, `bulk`, or raw-menu-key fields
exist in v1. Escape/cancel mappings vary by native callback; `{"cancel":true}`
is accepted for `line` and `extcmd` requests and returns the native Escape/`-1`
result, and for a menu it returns native cancellation; menu cancel is `-1`/no
result. Key bytes include control/meta codes without terminal escape-sequence
interpretation; 0 is rejected except where a native position sentinel is
returned after a valid position answer.

### 11.3 Accepted-action identity

Accepting an action is explicitly two-phase:

1. `agent_receive` frames a line, parses it, and applies every protocol-level
   check that needs no engine knowledge — schema, ranges, the outstanding
   request id, kind compatibility, the pinned menu generation, the advertised
   position rectangle, page completeness, and the implied durable
   acknowledgement. It does **not** record the action as accepted.
2. The caller then runs its own semantic validation (menu contents via the menu
   model, native yes/no semantics, position legality). If that succeeds the
   caller calls `agent_accept`, which is what records acceptance.

`agent_accept` takes **no action argument**. Acceptance operates solely on the
session-owned pending identity captured by `agent_receive`, so an unrelated
object cannot be bound to the pending bytes, and a zero request id — which the
parser never produces — can never be accepted. The pending identity is
consumed exactly once; a second call without an intervening receive fails and
changes nothing.

A semantically rejected action therefore leaves the request outstanding and may
be resubmitted with the same request id; only a *recorded* accepted id can
conflict.

Acceptance records the exact request bytes, with a 32-bit hash used only as a
fast pre-filter. An identical retry replays the retained response, which is the
complete immutable logical response stream (every physical chunk), byte for
byte. Two distinct actions that happen to share a hash are still distinguished,
because the bytes decide. A conflicting reuse of an accepted id closes the
transport. An accepted request is never executed twice. If the retained
response is unavailable, the session closes generically rather than
re-executing.

## 12. Menu contract

### 12.1 Construction and ownership

* `start_menu` starts a fresh menu generation and discards previous row mappings
  for that window.
* `add_menu` copies identifier, preselection, counts/flags needed by the ABI,
  and displayed content into adapter-owned storage before the callback returns.
* Selectability is determined by the native identifier convention followed by
  tty, not by the accelerator. Selector `0` means no supplied selector, not a
  heading or an unselectable row. An identifier is never dereferenced to
  discover item properties or quantity.
* Row ids are assigned sequentially in insertion order, including headings, and
  are scoped to `(menu generation, selection request)`. They remain stable
  across paging, retry, and resync for that request. An old request's row ids
  are never reused as live selections.
* Ordered headings and duplicate visible text are preserved. Title, prompt,
  text attributes, and icon appearance are public; raw flags and identifiers
  are not.
* On success, the native result array is allocated and saved
  identifiers/counts/item flags copied per the existing ABI. Cancellation
  produces no result array.

### 12.2 Rows and selector policy

Row ids are authoritative. Supplied individual/group accelerators are advisory
presentation labels. Version 1 does not emulate tty page-local automatic
accelerators: selector `0` is encoded as `null`, and every selectable row remains
reachable by id. Protocol pages are deterministic slices of insertion order
under the negotiated public byte/row limits, independent of identifiers or
hidden quantities.

A row is exactly:

```json
{"r":2,"text":"a dagger","selectable":true,"key":null,"group":")","initial":-1,"style":0,"color":"gray","icon":[")","gray",0,"none"]}
```

`initial:null` means initially unselected; `initial:-1` means selected at the
native all/default count. Any native positive initial count is preserved when
present. Menu icons use the same rendered-appearance sanitizer as map cells,
adapted to menu rendering rules. A row with no displayed icon uses
`"icon":null`.

### 12.3 Final-set actions

```json
{"v":1,"type":"act","seq":20,"id":21,"action":{"menu":"m4","commit":[[2,-1],[5,3]]}}
```

The selected rows are an **explicit final set**, not toggles. Every omitted row
is unselected, including preselected rows. A client accepting the initial
choices must explicitly submit them. Each row appears once; result order is
normalized to menu insertion order.

* Counts are integer `-1` (native all/default) or positive integers within both
  the declared protocol bound and the native `long` range. Zero, other
  negatives, fractions, overflow, and duplicate rows are rejected without
  completing the prompt.
* Hidden stack quantities are never inspected to validate or clamp counts.
* `PICK_NONE`: display-only; accepts acknowledgement or an empty commit;
  nonempty selections are invalid.
* `PICK_ONE`: the final set has zero or one selectable row; a nonempty set
  selects that row with the supplied legal count.
* `PICK_ANY`: the final set has any number of selectable rows within the bounded
  menu size.
* An accepted empty commit returns native count `0` and no results, even with
  preselected rows. An explicit `cancel` returns native cancellation (`-1`) and
  no results; it never means "accept preselection". Validation errors leave the
  current request outstanding.

### 12.4 Menu/text window descriptors

Menu and text content is published through `windows`:

```json
{"w":"w1","kind":"menu","title":"What do you want to eat?","mode":"one","content":"c1","pages":2}
{"w":"w2","kind":"text","title":"Inventory","content":"c2","pages":1}
```

`w` and `content` are monotonically presentation-derived ids, never native
`winid`s or file paths. `mode` appears only for menus. Content is retrievable
page by page through `get_page`/`page`.

### 12.5 Groups, bulk operations, and `SKIPINVERT`

Version 1 deliberately omits group-select, invert-all, select-all, and
bulk-toggle conveniences. Advisory group accelerators do not authorize a hidden
group operation. Direct final row selection remains legal and does not
approximate tty inversion rules. Group/bulk action fields and raw menu keys that
purport to perform those operations are rejected. A future convenience must
reproduce `menuitem_invert_test()`/`SKIPINVERT` semantics exactly; an
approximate "select every row with this label" is never acceptable. A caller can
explicitly select desired visible row ids in the final set.

## 13. Counter discipline and the closed-counter exception

* Every output record has a monotonically increasing public delivery counter
  `d`, starting at 1.
* `seq` (durable version) increments only at a decision/blocking/final boundary
  and starts at 1.
* Message, request, window, and content ids are assigned from public
  presentation only. Protocol counters are public bookkeeping, never PIDs,
  seeds, or hidden-state hashes.
* Chunk acknowledgement is cumulative and contiguous (section 8.2). Chunk
  acknowledgement releases sender buffer pressure; it is not input, not a
  durable commit acknowledgement, and not permission to advance a prompt.
* A durable acknowledgement names the applied `seq`. A following valid action
  may acknowledge that version implicitly. No gameplay action is accepted while
  a required commit is only partly delivered.
* Retries are deduplicated by public id. Changing content under an existing id
  is a protocol failure.

**Counter bounds.** Every output counter is bounded by `1..2^53-1`. Before any
counter advances, and before any encoder emits a caller-supplied event, request,
window, or content id, the value is checked; a counter that would wrap, or a
supplied id outside the bound, causes a generic close (`AG_LIMIT`) rather than
an unchecked increment. The same applies to every parsed integer counter.

**Single-emission records.** `hello` and `closed` are each emitted at most once
per connection. A second call is rejected and emits nothing, so a transcript
contains exactly one `hello` and exactly one `closed`.

**Parsing bounds.** Beyond the physical line length, the reader enforces the
declared nesting depth, per-object key count, and a per-line token budget of
32768 tokens; exceeding the token budget rejects the line rather than parsing
it. Byte length is validated as a separate layer from character length: the
declared text budgets are byte budgets, and invalid UTF-8 is rejected outright.

**Encoder validation and fail-closed commits.** Every public text value is
validated as well-formed UTF-8 (rejecting lone continuation bytes, truncated
leads, overlong encodings, surrogates, and code points above U+10FFFF) and
against its byte budget **before either representation is built**. A malformed
value makes the commit fail closed: no record, page, or chunk is written and no
counter moves. Long-text slices are cut only at UTF-8 scalar boundaries, so
every slice and every physical line is independently valid UTF-8.

**Closed-counter exception.** Architecture §6.5 mandates that the terminal
closure be exactly `{"v":1,"ch":"control","type":"closed"}` with no other field.
This is an explicit, documented exception to the §4.5 rule that *every* output
record has a delivery counter. Terminal closure is not retryable, not
acknowledged, and is emitted at most once per connection; a delivery counter on
it would carry no useful ordering information and would violate the mandated
bare object. `d` is therefore omitted from `closed` and only from `closed`.
Every other control record (`hello`, `chunk`, `page`, `invalid`) carries `d`.

## 14. Repeated-menu mapping clarification

`select_menu` may legally be invoked more than once for the same constructed
menu in some native code paths. Two obligations must be kept distinct:

1. **Active request mapping** — the `(menu generation, request)` row-id to native
   identifier mapping is released at *completed selection*, *restart*,
   *window destruction*, *reset*, or *worker exit*.
2. **Private template** — the adapter must retain the constructed menu's private
   row template (copied `ANY_P` identifiers, item flags, displayed content)
   until the window is destroyed or the menu is restarted, because a later
   native `select_menu` for the same constructed menu needs the identifiers that
   a completed request's *active* mapping has already retired.

Releasing the active mapping must therefore **not** destroy the identifiers a
later native `select_menu` still requires. A later `select_menu` invocation
creates a fresh selection *request* (new request id and new row-id scope) with
initial selection taken from the adapter's then-current menu selection state.
Old request ids are dead and are never reused as live selections. Retained
public transcripts store no pointers.

## 15. Capability negotiation and Phase 2 exclusions

Advertised in Phase 2: `snapshot`, `menu`, `paging`.

Phase 2 does **not** advertise and does not accept: `delta`, `ev`,
`continuation`, `save-token`, `resync`, `perm_invent`, `enhanced colors`,
`unicode`, `mouse`, `hilite_status`, `hitpointbar`, `extra status`, `sounds`,
`selectsaved`. Any negotiated capability that is advertised must have a named
behavior and a fixture before it is advertised.

## 16. Forbidden keys and shapes

Rejected without consuming engine input:

* Unknown or duplicate object keys at any level.
* Numbers that are not exact integers (fractions, exponents, `NaN`,
  `Infinity`), and integers outside the declared bound.
* More than 8 levels of nesting, more than 32 keys per object, more than 32768
  tokens per line, more than 65536 physical line bytes.
* NUL bytes; invalid UTF-8; UTF-16 surrogates encoded as UTF-8.
* `action` with more than one tagged shape, or with none.
* `action` keys `then`, `group`, `selectall`, `invert`, `bulk`, `keys`, `raw`.
* `menu` commit with a row id absent from the current menu generation, a
  non-selectable row, a duplicate row, or a count of `0`, `<-1`, a fraction, or
  overflow.
* Anything in a `player` object that is not in its schema (no smuggling extra
  fields).
* Any field in `closed` beyond `v`, `ch`, `type`.

## 17. Conformance vectors

P1 must pass, at minimum:

* Equal-looking glyph inputs produce byte-identical public cells and icons.
* A hidden `yn` suffix is never encoded; visible choices stop before Escape.
* Selector-0 selectable rows, headings, duplicate visible text, preselected
  `PICK_ANY`, positive/default counts, empty versus cancel, `PICK_NONE/ONE/ANY`,
  and explicit rejection of group/bulk fields.
* Exact parsing, escaping, and bounds; every forbidden key/shape is rejected.
* Palette isolation: an animation-only tuple consumes no durable palette id.
* A projectile that returns to its original durable map yields an empty durable
  map patch and a nonempty audit frame ledger.
* Every legal chunk split boundary, every retry, and every resync replay yields
  the same reference state and the same exactly-once ordered distinct frame
  ledger.
* Stale actions consume no input; identical retries do not execute twice;
  conflicting reuse closes.
