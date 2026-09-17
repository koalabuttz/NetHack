# Headless AI Agent Interface for NetHack 5.0.0

## Recommendation

Add a native `agent` window port, backed by a narrowly audited **player-visible rendering adapter**, and run one game per subprocess under a lightweight trusted supervisor. Use versioned, compact newline-delimited JSON for observations and actions. Observations describe presentation, not engine state.

The decisive security rule is:

> The player-observation encoder receives only values that the selected ordinary-human presentation would display. It never receives engine objects, raw glyph IDs, background-map layers, native menu identifiers, RNG state, or diagnostic text.

Lock this boundary at **trusted bootstrap, before configuration processing**, not by resetting options after initialization. Separate player observations from administrative control. Use explicit durable snapshot semantics, with a separately negotiated transient replay state machine. Default LLM presentation is final durable deltas plus complete messages, prompts, menus, and text—not animation-heavy audit output.

The window abstraction is the right seam, but its callback arguments are not themselves an information-safe wire format. This tree supplies raw glyph metadata, opaque menu identifiers, hidden prompt choices, and diagnostics through window-related paths. The mod must sanitize them deliberately.

### Revision history

**Revision 2 incorporates a design review:**

- Lock the safe profile at trusted bootstrap, before user configuration or window selection can run, and validate restores before public output.
- Separate presentation-noninterference obligations from a minimal, explicitly declassified administrative control channel.
- Define independent durable snapshots and transient replay, including palette, chunk, acknowledgement, retry, and resynchronization semantics.
- Complete the adapter-owned menu contract: authoritative row IDs, final selection sets, preselection, counts, cancellation, and safe glyph rendering.

This is a design, not an implementation report. No implementation changes, builds, or tests are claimed. The review found no Critical flaws and confirmed the core architecture and sampled factual citations. The acceptance tests below are work for the implementer. Source findings from the original investigation and additional reviewer-supplied citations are distinguished from proposals by context; proposed paths and interfaces are labeled explicitly.

## 1. Scope and success criteria

### Required

- Operate without a controlling terminal, terminal emulator, curses, or tty rendering.
- Support normal legal play: character selection, movement, inventory, targeting, menus, text entry, death, saving, and restoration.
- Send compact deltas rather than a complete map each action.
- Support thousands of independent episodes with bounded memory and no cross-episode contamination.
- Make explicit observation leaks structurally difficult and testable.
- Remain C99 and fit the existing window-port and Unix build organization.

### Assumptions

- Unix/Linux is the first supported platform. The adapter and protocol are portable C99; launching and sandboxing are initially Unix-specific.
- The agent can be untrusted. Secure evaluation does not give it the worker's credentials, memory, writable filesystem, or privileged supervisor API.
- A trusted evaluator may retain seeds, saves, diagnostics, and metrics separately from agent-facing interfaces.
- Human equivalence is relative to a fixed ordinary ASCII/color presentation, not every tiled, customized, accessibility, or wizard configuration.
- The default LLM policy deliberately omits nonblocking transient map animation; it does not omit messages, prompts, decision-time maps, menus, or text. Lossless transient replay is an opt-in audit capability.

### Non-goals for version 1

- In-process reinitialization, concurrent games in one engine address space, or fork-from-live-game reset.
- Omniscient RL features, privileged reward shaping, arbitrary agent Lua, or automatic identification of map entities.
- Rewritten command semantics, combat rules, or game timing.
- A network-facing server; a gateway can later wrap the local supervisor.
- Guaranteed one RPC per engine turn. NetHack has zero-turn commands, nested prompts, and multi-turn occupations.
- Strong wall-clock/cache/resource side-channel resistance without additional deployment controls.

### Observable success

1. A simple move normally uses one action request and one response with changed presentation values only.
2. Menu- and prompt-heavy play works without guessed answers or a terminal.
3. Applying durable deltas reconstructs the adapter's durable presentation exactly.
4. Negotiated audit replay reconstructs ordered distinct visible frames independently of chunking, retry, or resynchronization.
5. Equal permitted presentation histories and actions yield equal player payloads; administrative envelopes independently satisfy their allowlist.
6. Reset starts a fresh worker with isolated writable state.
7. Existing tty builds remain unchanged when agent support is disabled.

## 2. Verified architecture and its consequences

### 2.1 Window callbacks are the primary seam

`struct window_procs` covers window lifecycle, text, mixed text, menus, glyphs, raw output, keyboard/position input, yes/no input, line input, extended commands, status, history, inventory notification, and control callbacks (`include/winprocs.h:26-99`). Registration is centralized in `winchoices[]` (`src/windows.c:92-109`). The tty callback table is an implementation model (`win/tty/wintty.c:98-161`).

**Decision:** add a sibling port, not a tty scraper or Lua observation API. Implement all required callback semantics; headless operation cannot discard text windows or menus.

There is no dedicated direction callback. `getdir` sets `getdirInp` (`src/cmd.c:3990-3993`), and position input sets `getposInp` (`src/cmd.c:5325-5327`).

### 2.2 Map output is structured, but not pre-sanitized

`putstr` receives text, not structured map cells. Map output uses `print_glyph(window,x,y,foreground,background)` (`include/winprocs.h:45-65`). `glyph_info` includes raw glyph number, tty character, frame color, flags, symbol index, color, tile index, and optional Unicode representation (`include/wintype.h:78-109`).

Conversion lives in **`src/display.c`**, not `src/mapglyph.c`. `map_glyphinfo()` copies the glyph-map entry, handles hero/accessibility overrides, resolves `ttychar`, and retains the raw glyph (`src/display.c:2634-2696`).

The tty renderer conditionally uses character, color, and display attributes. Some metadata is not displayed; female highlighting requires wizard mode (`win/tty/wintty.c:3876-3937`).

**Decision:** palette identity derives exclusively from normalized rendered appearance. Never publish raw glyphs, tile indices, species indices, or a wholesale `MG_*` mask.

### 2.3 Visibility is processed by the core

`newsym()` handles swallowing, underwater restrictions, physical sight, sensing, detection, warning, and remembered terrain (`src/display.c:925-953`, `1001-1086`). Sensed monsters can appear outside physical sight (`1040-1056`). Hallucination changes object, pet, and warning presentation (`338-359`, `599-640`).

**Decision:** consume displayed results. Do not traverse `levl`, monster/object/trap lists, or vision arrays. A displayed cell does not establish current physical visibility, species, BUC status, hostility, or true identity. Preserve ambiguity; obtain precise descriptions through normal look/farlook/inventory commands.

### 2.4 Background glyphs are not the ASCII foreground

Background glyph calculation consults seen cells and underlying terrain (`src/display.c:2548-2607`). The tty path displays background frame color, not an additional underlying-terrain glyph layer (`win/tty/wintty.c:3924-3937`).

**Decision:** disable background-glyph use and discard its identity and symbol. Only a frame color actually displayed by the fixed profile may survive sanitization.

### 2.5 Rendering and input have different boundaries

`flush_screen()` updates dirty status, emits glyphs, optionally moves the cursor to the hero, and calls `display_nhwindow(WIN_MAP,FALSE)` (`src/display.c:2249-2307`). Command parsing flushes and marks `commandInp` (`src/cmd.c:5135-5153`). Queued/replayed input can bypass the port, and count input can reuse `commandInp` (`5246-5260`, `5301-5305`, `5144-5153`). Occupations and repetition run separately from ordinary command acquisition (`src/allmain.c:486-537`).

Reviewer-supplied evidence reinforces why rendering is not a durable observation boundary: `tmp_at()` flushes per-update (`src/display.c:1195-1197`, `1311-1334`), restores cells (`1220-1267`), and effects call `nh_delay_output()` (`1110-1123`, `1345-1361`). Direct map-display paths also exist outside `flush_screen()` (`src/allmain.c:549-556`; `src/detect.c:781-850`).

**Decision:** distinguish rendering checkpoints, input requests, durable commits, and engine turns. Do not treat any one callback as all four.

### 2.6 Status is field-based

Status callbacks carry field enablement and updates (`include/winprocs.h:90-95`). Ordinary fields use formatted strings; conditions use a bitmask (`src/botl.c:1607-1612`). `BL_FLUSH` and `BL_RESET` are presentation batching operations (`1660-1676`). Enablement depends on options and applicability such as polymorph (`1700-1716`). Tty ignores inactive fields and decodes mixed gold text (`win/tty/wintty.c:4470-4515`).

**Decision:** publish enabled displayed values, not backing structures, generic pointers, hidden percentages, or undisplayed condition metadata.

### 2.7 Input and menus have port-owned semantics

TTY input uses terminal functions; Unix position input delegates to keyboard input (`win/tty/wintty.c:4046-4099`, `4120-4148`). Replace this path rather than reusing it.

Yes/no responses may contain an undisplayed suffix after Escape (`win/tty/topl.c:371-381`, `397-417`). Defaults, Escape, unrestricted input, and numeric responses have distinct behavior, including setting `yn_number` (`423-428`, `463-530`). Extended commands resolve through the existing matcher (`win/tty/getline.c:292-325`).

Menu results contain opaque `anything` identifiers, counts, and flags (`include/wintype.h:15-38`, `66-71`). The reviewer identifies tty's identifier/preselection copying (`win/tty/wintty.c:2561-2624`), per-page accelerator assignment (`2644-2728`), count/toggle/group handling (`1118-1311`, `1331-1761`), and `menuitem_invert_test()` handling of `MENU_ITEMFLAGS_SKIPINVERT` (`src/windows.c:1557-1588`).

**Decision:** implement the complete adapter contract in §5.6. Native identifiers remain private; selection cannot be approximated by returning letters or guessing from visible text.

### 2.8 Window callbacks alone do not close every leak

`impossible()` logs diagnostics and then calls ordinary `pline()` with them (`src/pline.c:584-603`). Early/recursive messages can use `raw_print` (`235-241`). Wizard Lua commands have `WIZMODECMD` flags (`src/cmd.c:1977-1980`) checked by dispatch (`479-481`). Shell commands exist conditionally on `SHELL` (`1865-1870`).

**Decision:** isolate diagnostics at their producers and enforce a locked normal-play execution policy, not merely a safe serializer.

### 2.9 Trusted bootstrap must precede configuration

The reviewer verified that `initoptions()` runs at `sys/unix/unixmain.c:150`, before argument processing/window initialization at `170-174`. Personal rc/environment configuration enters through `rcfile()` and option finishing (`src/options.c:7322-7344`, `7091-7128`). Runtime option setters remain represented in `include/optlist.h`. Restore replaces startup flags (`src/restore.c:576-603`).

**Decision:** recognize trusted agent bootstrap before any configuration, frontend selection, or public raw-output path. Skipping untrusted configuration is mandatory; parsing it and resetting values afterward is not equivalent. Restore validation is a separate publication gate.

## 3. Components and ownership

The following are **proposed new paths**:

| Component | Location | Responsibility |
|---|---|---|
| Window adapter | `win/agent/winagent.c`, `include/winagent.h` | `agent_procs`, callback ABI, private menu identifier ownership, native input results. |
| Presentation sanitizer | `win/agent/agent_view.c` | Fixed-profile rendered values only; map and menu glyph sanitization. |
| Public types | `win/agent/agent_types.h` | Pointer-free cells, styles, fields, text, menu rows, requests. No engine types. |
| Protocol and state | `win/agent/agent_protocol.c` | Bounded JSON framing, durable state, optional audit replay, sequence/ack/retry rules. No `hack.h`. |
| Input transactions | `win/agent/agent_input.c` | Typed actions, continuation matching, cancellation, thin native-command conveniences. |
| Trusted bootstrap/policy | `win/agent/agent_bootstrap.c` | Early mode latch, fixed profile, runtime policy checks, publication gate. Narrow engine-facing code. |
| Trusted supervisor | `sys/unix/agent_runner.c` | Launch/reap/reset, descriptor/filesystem isolation, private diagnostics, limits, save tokens. |
| Protocol/policy docs | `doc/agent-interface.md` | Wire schema, profile and LLM policy versions, callback disclosure table. |
| Harness | `test/agent/…` | Standalone fixtures, scripted driver, transcript/replay tests, benchmarks. |

These are responsibility boundaries, not separate services. The engine remains single-threaded.

```text
trusted launcher → early bootstrap latch → trusted configuration/profile
                                              ↓
NetHack callbacks → sanitizer → pointer-free presentation
                                 ├→ durable state/deltas
                                 └→ negotiated transient audit frames
                                              ↓
                                    private worker transport
                                              ↓
                          supervisor channel allowlist → agent

agent action → validation → input transaction → native callback result
```

Only engine-facing translation units include engine headers. Public encoding must compile without them. Native menu identifiers never enter public state. No observation code traverses the world. No agent C/Lua executes inside the worker. The supervisor receives only sanctioned lifecycle notifications and sanitized payloads; it is not a state-inspection service.

These restrictions prevent accidental exposure; they do not sandbox arbitrary C memory corruption. OS separation protects against direct agent access.

## 4. Observation model

### 4.1 Fixed reference profile and LLM policy

Publish two independent versioned choices:

- **Rendering profile `normal-ascii-color-v1`:** ordinary fixed ASCII/color rendering and input semantics.
- **Delivery policy `llm-final-v1` (default):** final durable map/status deltas at decision boundaries, complete ordered messages/prompts, all menu/text content through bounded paging, and no nonblocking transient map replay.

The rendering profile fixes ordinary symbols, basic color behavior, key bindings, pet/pile highlighting, and inverse-video precedence. Use normal primary/Rogue symbols without user overrides; enable ordinary pet/pile highlighting and inverse display, with their normalized visible attributes, not semantic flags. Disable custom symbols/colors, enhanced glyph/Unicode handlers, tiles, background terrain, tracing, fuzzer, discovery/wizard mode, user Lua callbacks, and external facilities. Time display is on; optional status fields and condition display are frozen in the versioned profile manifest. Applicability changes caused by normal play, such as polymorph fields, remain permitted.

The Phase 1 manifest must enumerate every effective option, symbol source, condition-display rule, and capability bit for this build. Unspecified options are not a runtime customization mechanism. Unsupported/new options fail closed until classified. Advertise only capabilities actually implemented; initially avoid permanent inventory, enhanced color, Unicode, and mouse input. Inventory remains available through ordinary menus.

The full logical map corresponds to an adequately sized human display. No artificial viewport reveals hidden cells. Normal inspection/help commands remain available inside the sandbox.

`audit-frames-v1` additionally negotiates the `ev` capability described below. It is an opt-in, lossless record of distinct normalized rendering checkpoints, not part of the default LLM prompt. Once negotiated, it cannot silently disappear because output is large. Both policies preserve decision-time temporary maps and blocking displays; the default only coalesces intervening nonblocking animation.

### 4.2 Canonical public state

- **Map:** `(character, foreground color, rendered attribute mask, rendered frame color)`. Equal visible tuples remain equal despite different raw identity or reasons for highlighting.
- **Cursor:** displayed position. A command-boundary cursor may indicate the hero; a targeting cursor is not an authoritative hero coordinate. Never substitute `MG_HERO`.
- **Status:** normalized displayed strings, displayed conditions/styles, explicit removal when disabled. No hunger points, hidden timers, exact unseen weights, or backing combat state.
- **Messages/prompts:** complete ordered displayed text and attributes. Distinguish restored history from new events. Lossless consecutive repetition encoding is allowed; paraphrasing in the worker is not.
- **Menus:** title, mode, ordered rows, public selectability, advisory selectors, initial selection, rendered text/style/icon. Native identifiers and raw flags stay private.
- **Text windows:** ordered lines, style, instance ID, explicit clear/close. Help, inventory, discoveries, and endgame output are not reduced to one message.
- **Input request:** exactly one outstanding native interaction, with public request ID and visible prompt/choices/default/context.

Mixed text must be decoded to rendered symbols before encoding; `decode_mixed()` demonstrates the conversion (`src/windows.c:1465-1486`). Never export embedded authenticated/raw glyph payloads.

### 4.3 Wire shape and channel partition

Use UTF-8 newline-delimited JSON with escaped embedded control characters. Examples are proposed schema sketches. The complete field/type/size specification is a Phase 1 deliverable.

```json
{"v":1,"ch":"control","type":"hello","profile":"normal-ascii-color-v1","policy":"llm-final-v1","caps":["delta","menu","snapshot"],"coord":"engine-map","size":[80,21],"x0":1,"y0":0}
```

Dimensions are illustrative; derive actual values from build constants and explicitly document legal coordinates and unused column zero.

```json
{"v":1,"ch":"player","type":"obs","seq":19,"base":18,"s":{"time":"42","hp":"11"},"pal":[[3,"@","white",0,"none"],[4,".","gray",0,"none"]],"map":[[10,8,4],[11,8,3]],"cur":[11,8],"msg":[[27,"You hear someone counting money."]],"need":{"id":20,"kind":"command"}}
```

`seq` versions durable presentation, `base` names its required predecessor, `pal` defines normalized display tuples, and `map` contains absolute cell replacements. `s` contains changed fields; `null` deletes one. Cursor removal is explicit. Message/request IDs are presentation-derived counters, not engine identifiers. Unchanged fields are absent.

A separate connection represents each episode; strict player payloads need no random episode ID. The supervisor may privately assign lifecycle identifiers. Version/request counters are public protocol bookkeeping, not PIDs, seeds, or hidden-state hashes.

Administrative events use `ch:"control"`; schemas cannot smuggle extra fields into player objects. Both logical channels can share the supervisor's public stream. Privileged evaluator control is a separate, inaccessible capability, not another agent-selectable `ch` value. §7 defines the declassification policy.

### 4.4 Durable state machine

Maintain three distinct concepts:

1. **Working renderer `W`:** latest sanitized callback presentation, updated during execution.
2. **Durable snapshot `D_n`:** complete presentation at a publication/decision boundary, with durable version `n` and durable palette `P_n`.
3. **Transport delivery state:** which immutable records/chunks the client acknowledged. Delivery does not mutate `D_n`.

A durable boundary occurs at an unsatisfied input request, a blocking display requiring acknowledgement, or final ordinary player-facing completion. Port-managed auto-ack content must first be published completely; acknowledgement/paging never silently authorizes a gameplay decision. Nonblocking `display_nhwindow`, `BL_FLUSH`, and delay callbacks do not alone advance `seq`.

At a boundary, freeze `W` as `D_(n+1)` and encode its differences **against `D_n`**, not against a transient frame or last socket write. Always record new complete prompt/message events, even if the durable map is unchanged. Every patch is an absolute replacement. Client application is atomic after validating the whole logical commit; a duplicate version is not applied again.

A full snapshot uses `base:null` and contains all durable state, palette, current request, and content/page references. Initially unpainted cells equal the declared blank appearance; do not distinguish hidden stone from unexplored when both are blank.

Assign new durable palette IDs at commit from newly used durable tuples in deterministic public presentation order. A tuple seen only in an omitted animation must not consume a durable palette ID. Definitions and their references become durable atomically in the same commit. Never reuse IDs for a different tuple within an episode.

Map clears/redraws change working presentation; they do not reveal a dungeon/branch/file ID. Compare the final normalized state and use sparse triples initially; optional row runs are a later measured optimization. Client memory across visits comes from the transcript, not engine level identity.

Resynchronization snapshots come only from `D_n`, never by scanning game state. New/restored workers begin with a full snapshot and empty protocol history.

### 4.5 Transient replay state machine, chunks, and recovery

`ev` is a negotiated capability, not an optional field whose absence ambiguously means lost data. `llm-final-v1` does not negotiate it. Audit mode uses **ordered successor patches on a separate scratch view**, never mutations of durable state.

For an execution interval starting at durable version `n`:

1. Emit `begin(interval,base:n)`. Initialize scratch view `T_0` from `D_n`.
2. Each rendering checkpoint produces `frame(interval,k,prev:k-1,patch)`. Applying it replaces cells/styles in `T_(k-1)` to form `T_k`. Capture map checkpoints at all map displays, including direct calls, and delay checkpoints only when normalized visible state changed. Status/text/cursor display events use the same ordered event stream where relevant.
3. Suppress identical normalized frames. Do not export callback counts, raw repaint order, wall-clock durations, or empty delays.
4. Transient cell patches carry inline normalized appearance tuples in version 1. They do not allocate or redefine durable palette IDs. This intentionally trades some audit bytes for unambiguous independent state.
5. At the next durable boundary, emit `end(interval,last:k,target:n+1)` and the durable commit. The client applies the durable patch against **`D_n`**, then explicitly discards/reset its transient scratch view and displays `D_(n+1)`. If the last transient view differs, this reset is an explicit visible final restoration, not an implied no-op.

A projectile moving across cells includes the source restoration and next projectile cell in successive patches. If the final map equals the pre-action map, the durable map delta can be empty while the audit stream still contains all movement/restoration frames. Blocking displays are decision boundaries even when their map is temporary; later restoration is an ordinary later durable change.

**Transport rules:**

- Every output record has a monotonically increasing public delivery counter. Large logical records use ordered chunks with record ID, zero-based chunk index, and explicit final marker. Splits occur only at defined element/text boundaries; UTF-8 and scalar values are never cut ambiguously.
- Chunks can be acknowledged cumulatively through the last contiguous chunk. Chunk acknowledgement releases sender buffer pressure; it is not input, not a durable commit acknowledgement, and not permission to advance a prompt.
- Assemble a full logical frame/commit before applying it. Reject gaps or inconsistent repeated chunks. Identical retry chunks/frames are deduplicated by their public IDs. Changing content under an existing ID is a protocol failure.
- A durable acknowledgement names the applied `seq`. A following valid action may acknowledge that version implicitly. No gameplay action is accepted while a required commit is only partly delivered.
- Retain the latest unacknowledged durable commit and the complete current audit interval until its final durable commit is acknowledged. Use bounded private spooling/backpressure rather than dropping frames. Enforce total resource limits through §7's control policy.
- During audit resync, pause transport advancement at a record boundary, resend `D_n` and replay the retained interval from `begin` through the produced prefix, then continue. Resync is not a game action. The client rebuilds scratch state from that named base. Its audit event ledger deduplicates `(interval,k)`, so already displayed frames are not counted/displayed twice.
- Resync during a partially assembled frame discards that assembly and restarts replay. Resync after the interval's durable commit is acknowledged returns the latest durable snapshot; retrieval of older audit history requires a separately retained client/trusted transcript, not re-execution.
- If retained state needed for an unacknowledged interval is unavailable, close generically in strict mode. Never re-execute the action or pretend replay was lossless.

Messages/prompts have their own monotonically assigned presentation event IDs within this record order. They are delivered once semantically in both policies, including intermediate prompts satisfied by explicit continuations. Text/menu pages are stable public content slices, not reruns of engine commands. Final-only clients omit transient scratch handling but use the same durable/chunk rules.

### 4.6 Nearby entities

Do not publish a second authoritative monster/object list. It duplicates map output and encourages engine traversal.

An optional client-side `near` formatter can derive `red D at (12,8)` or `inverse d at (9,7)` from the reconstructed map. It cannot add species certainty, hostility, HP, FOV, or stable entity IDs. Player-visible text/history can inform explicitly labeled client inferences.

Use ordinary look/farlook conveniences for precise descriptions. Do not proactively inspect every square inside the port; inspection can change knowledge or display RNG state.

### 4.7 Encoding and token policy

Compact JSON lines are the initial recommendation: mature escaping, readable diagnostics, easy script clients, sparse tuples, and incremental palette. Custom compact text is suitable as a client formatter after tokenizer measurements. Binary transport can follow if RL throughput justifies it. Full-screen text is a debugging/client rendering option, not the normal wire representation.

`llm-final-v1` is a first-class deliverable, with canonical short formatting, no repeated map dump, no duplicate `near` list by default, and no animation frames. The default does not summarize away gameplay text. Long text/menu material is presented in lossless numbered pages with explicit retrieval/acknowledgement; the game cannot accept a selection while its required menu is incomplete.

Travel/display-delay behavior (`src/hack.c:2994-3014`, reviewer citation) and repeated `tmp_at()` updates motivate measuring tails, not only ordinary moves. Audit frames go to a separate opt-in consumer; an LLM consumer that negotiated them must handle backpressure or explicitly start a new session/policy, not silently discard negotiated wire data.

## 5. Action and interaction model

### 5.1 Requests, not fictitious turns

The basic operation resumes an outstanding request until the next unsatisfied request or terminal transport outcome. Types include `command`, `key`, `direction`, `position`, `yn`, `line`, `extcmd`, `menu`, and display-only `ack`, plus normal character selection.

A move usually uses one exchange. Inventory may cost no game time; travel or eating may consume many turns. Only ordinary displayed time is exposed, not a synthetic turn counter derived from callbacks.

### 5.2 Primitives and conveniences

```json
{"v":1,"type":"act","seq":19,"id":20,"action":{"key":"h"}}
```

Thin `move`, `wait`, `command(name)`, `inventory`, `look_at`, and `travel_to` conveniences compile to native input, never direct movement/object mutation calls. Resolve extended names through existing match/dispatch rules (`win/tty/getline.c:316-325`); do not expose internal indices.

Allow explicit byte codes for control/meta input under fixed bindings. Disable alternate-meta ambiguity. Reject NUL except in a deliberately implemented native position-input capability. Native yes/no validation remains private, including hidden suffixes, default/Escape rules, and `yn_number`; the visible choices field contains only what the human prompt displays.

### 5.3 Typed continuations

```json
{"v":1,"type":"act","seq":31,"id":32,"action":{"command":"open"},"then":[{"expect":{"kind":"direction"},"answer":{"dir":"e"}}]}
```

Continuations are finite and transaction-local. Match request kind and exact public prompt/menu guards. For a not-yet-seen menu, match only an unambiguous visible row/selector across the entire menu, not just the current transport page. Duplicate text or repeated selectors requires pausing and publishing the actual menu. No unrestricted regex engine or agent code executes inside the worker.

On mismatch, ambiguity, unexpected confirmation, or exhausted continuation, pause. At the next fresh top-level command boundary discard leftover continuation entries. Never let them become an unrelated move. Raw key sequences are a bounded compatibility feature, not unguarded input spilling across menus and commands. Display-only auto-ack is permitted only under an explicit policy after complete content delivery; it is not blanket yes/no consent.

One round trip is feasible for predictable interactions. Unforeseen questions necessarily require another exchange unless the agent delegates an explicit answer policy.

### 5.4 Command boundaries

Reuse input context for direction/position, but not `commandInp` alone for fresh transactions: count/replay paths make it insufficient (`src/cmd.c:5144-5153`, `5255-5260`).

Add a small conditional agent notification at `parse()` after its normal initial flush and before the first command read. It carries no hidden-state payload. It invalidates leftover input and marks a fresh command request. Prefer an equivalent stable existing notification if one is verified during implementation; do not guess from individual key reads.

Keep this a private integration hook rather than adding a universal window ABI member. Do not force `bot()`, vision recalculation, or map scans from nested input callbacks; line input explicitly suppresses status processing (`win/tty/getline.c:63-64`). Publish existing callback presentation.

### 5.5 Travel, occupations, and interrupts

Use native travel/targeting, repetition, and interruption behavior. Do not pathfind over hidden terrain. Supervisor wall-clock/CPU limits may terminate a worker; they do not create an invented safe resumable engine point. Fine-grained interruptible occupations require a separately reviewed main-loop hook and are deferred.

### 5.6 Complete adapter-level menu contract

**Construction and ownership**

- `start_menu` starts a fresh menu generation and discards previous row mappings for that window. `add_menu` copies identifier, preselection, counts/flags needed by the ABI, and displayed content into adapter-owned storage before the callback returns.
- Determine selectability using the native identifier convention followed by tty, not the accelerator. Selector `0` means no supplied selector, not a heading or unselectable row. Never dereference an identifier to discover item properties or quantity.
- Assign sequential row IDs in insertion order, including headings. They are scoped to `(menu generation, selection request)` and remain stable across paging, retry, and resync for that request. Never reuse an old request's row IDs as live selections.
- Keep ordered headings and duplicate visible text. Title, prompt, text attributes, and icon appearance are public; raw menu flags and identifiers are not.
- On success, allocate the native result array and copy saved identifiers/counts/item flags according to the existing ABI; caller ownership follows native `select_menu` conventions. Cancellation produces no result array. Release mappings at completed selection, restart, window destruction, reset, or worker exit; retained public transcripts do not retain pointers. A later `select_menu` invocation creates a fresh request and initial selection from the adapter's then-current menu selection state.

**Rows and selector policy**

Use row IDs as authoritative. Supplied individual/group accelerators are advisory presentation labels. Version 1 does not emulate tty page-local automatic accelerators: selector `0` is encoded as `null`, and every selectable row remains reachable by ID. Protocol pages are deterministic slices of insertion order under negotiated public byte/row limits, independent of identifiers or hidden quantities.

Example row:

```json
{"r":2,"text":"a dagger","selectable":true,"key":null,"group":")","initial":-1,"style":0,"icon":[")","gray",0,"none"]}
```

`initial:null` means initially unselected; `initial:-1` means selected at native all/default count. Any native positive initial count is preserved when present. Menu icons use the same rendered-appearance sanitizer as map cells, adapted to menu rendering rules. Do not serialize raw glyph identity or attributes the menu renderer would not display.

**Final-set actions**

```json
{"v":1,"type":"act","seq":20,"id":21,"action":{"menu":"m4","commit":[[2,-1],[5,3]]}}
```

The selected rows are an **explicit final set**, not toggles. Every omitted row is unselected, including preselected rows. A client accepting the initial choices must explicitly submit them. Each row appears once; result order is normalized to menu insertion order.

- Counts are integer `-1` (native all/default) or positive integers within both the declared protocol bound and native `long` range. Zero, other negatives, fractions, overflow, and duplicate rows are rejected without completing the prompt.
- Do not inspect hidden stack quantities to validate or clamp counts. Return a valid count through the native API; core callers handle actual quantity semantics. Visible quantities may guide the client but are not an authorization oracle.
- `PICK_NONE`: display-only, accepts acknowledgement/empty commit; nonempty selections are invalid.
- `PICK_ONE`: final set has zero or one selectable row. A nonempty set selects that row with the supplied legal count.
- `PICK_ANY`: final set has any number of selectable rows within bounded menu size.
- Accepted empty commit returns native count `0` and no results, even with preselected rows. Explicit `cancel` returns native cancellation (`-1`) and no results. It never means "accept preselection." Validation errors leave the current request outstanding.

**Groups, bulk operations, and `SKIPINVERT`**

Version 1 deliberately omits group-select, invert-all, select-all, and bulk-toggle action conveniences. Advisory group accelerators do not authorize a hidden group operation. Direct final row selection remains legal and does not approximate tty inversion rules. Reject group/bulk action fields and raw menu keys that purport to perform those operations. A future convenience must reproduce `menuitem_invert_test()`/`SKIPINVERT` semantics (`src/windows.c:1557-1588`) exactly; do not implement an approximate "select every row with this label." A caller can explicitly select desired visible row IDs in the final set.

## 6. Episode and process control

### 6.1 Lifecycle

```text
idle → trusted launch/bootstrap → compatible startup/restore → playing
playing ↔ outstanding request
playing → normal completion/save | private crash/limit/reset classification
reset → terminate/reap/clean → fresh launch
```

Unix startup selects a window system (`sys/unix/unixmain.c:104`), initializes options (`150`), processes arguments/windows (`170-174`), sets playmode (`192-193`), initializes DLB (`209`), restores (`243`), or starts a game (`315`), then enters non-returning `moveloop` (`319-320`).

A fresh worker fits this lifecycle and avoids proving reset of globals, Lua, static locals, files, and callbacks. Do not re-enter `newgame()` in an old address space for an unmeasured optimization.

### 6.2 Trusted launcher and early bootstrap

Public agent transport is supervisor stdin/stdout JSON lines; no listener is required. Worker transport is a private inherited socketpair or pipe pair, separate from stdout/stderr. Proposed executable: `nethack-agent`.

A **trusted inherited descriptor** and bounded launcher handshake identify agent bootstrap mode before `choose_windows`, `initoptions`, user-config discovery, or any public raw output. A private descriptor-number argument may locate the descriptor, but is not itself authority. The launcher controls executable, descriptors, arguments, environment, configuration roots, and writable directories; the agent cannot launch a worker with arbitrary inherited state through this API. Missing/invalid bootstrap for an agent-only worker fails privately, never falls back to tty or personal configuration.

Bootstrap sets a process-lifetime, non-restorable agent-mode latch and closes the public observation gate. It:

1. Forces and locks `windowtype=agent`, including direct frontend selection paths.
2. Bypasses personal rc discovery and `NETHACKOPTIONS` entirely, including alternate/early passes. Loads only the trusted system/profile configuration from fixed non-agent-writable locations.
3. Rejects conflicting `-D`, `-X`, windowtype, config-path, symbol, hook, or arbitrary gameplay-policy arguments before normal processing. Only allowlisted character/start fields are translated by the supervisor.
4. Routes all pre-window raw output, stdout/stderr, and startup errors privately from the first possible output point.
5. Installs the central runtime policy gate before setters can execute.
6. Validates startup/restore compatibility before any player publication. A configuration/version incompatibility fails closed.

A fixed environment and private directories are necessary in Phase 2, not postponed hardening. Run without terminal/session dependencies, sound, shell, suspend, editor/pager/mail hooks, or setuid privileges. Delay callbacks never sleep.

**Runtime enforcement:** enforce an allowlist at common option mutation entry points, including `O`, direct setters, key binding changes, symbol/message handlers, window selection, and any bypass paths. Reject before mutation or handler invocation, not in a subsequent preference callback. Version 1 allows no runtime changes to profile-locked settings; harmless options require explicit classification before being allowlisted. Hide unavailable options where convenient, but UI hiding is not the security boundary. Compile-time exclusions and native wizard flags remain defense in depth.

### 6.3 Fast startup and reset

Link only the agent backend in throughput builds. Share immutable executable/DLB data through page caching; keep per-episode writable state separate, optionally memory-backed with limits. Reset must reap before cleanup and allocate a new connection/state namespace. Do not rebuild, install, or copy static data per episode.

Phase 5 adds bounded parallel workers, sandbox hardening, and optional independently initialized prewarming after measurement. Do not begin with a fork server. A later fork point would need independent entropy, descriptor ownership, and proof that no episode-specific files/Lua/game state have initialized.

### 6.4 Save and restore

Use native save commands and normal restoration, not arbitrary mid-prompt snapshots. Wait for confirmed native save success before issuing an opaque token. Tokens, file paths, seed metadata, and save bytes belong to trusted control; a permitted agent save/restore operation uses an opaque capability without filesystem access.

Bind trusted save metadata to build/data/profile/mode and ownership. Treat saves as private executable-input material, not arbitrary agent uploads. Restore starts a fresh worker with the public gate closed. Because restoration replaces flags (`src/restore.c:576-603`), check saved profile/mode compatibility before restored flags can enable public rendering or external facilities. Prevalidate trusted metadata and validate the actual restored flags while quarantined; metadata alone is insufficient. Reject wizard, discovery, incompatible-rendering, or unknown-profile saves. The bootstrap latch and runtime locks are not restored from save bytes.

After compatible restoration and normal redraw, initialize empty public caches and send a full snapshot. Mark restored history as history. Never restore menu pointers, action continuations, or old protocol sequence space. Core output suppression during save/restore (`src/display.c:2260-2262`) is useful but not a substitute for this publication gate.

### 6.5 Seeds and terminal outcomes

Seed setting and actual seed values are privileged evaluator capabilities. Exact RNG initialization sites remain to be verified before implementing reproducibility. Never expose seeds or seed-derived IDs to the agent.

The supervisor may privately distinguish `ended`, `saved`, `truncated`, `crashed`, and reset. **Strict agent mode does not publish crash/limit/reset classes or reasons.** It emits the same bare control object `{"v":1,"ch":"control","type":"closed"}` and closes the episode transport for every terminal transport outcome. Ordinary endgame/save messages already produced through the permitted player presentation may precede closure. There are no terminal diagnostic correlation tokens, timestamps, PIDs, exit codes, limit names, or failure-specific identifiers on that interface.

Do not infer victory/death or save success from exit status alone. Inspect native completion paths when implementing minimal trusted lifecycle notifications. Endgame disclosures are player information only when normally shown, and never become earlier live observations.

## 7. Information hygiene and trust model

### 7.1 Two obligations, not an absolute all-channel invariant

**Player-observation noninterference:** for equal normalized rendering histories under the negotiated policy and equal public action histories, player payloads must be equal, including palette allocation, menu/request IDs, messages, snapshots, and audit frames. Protocol counters derive only from public presentation/actions. Transport delivery timing and administrative closure are not player facts.

For final-only mode, nonblocking frames discarded by that policy must not influence durable palette IDs, output sizes, or counters. Audit mode instead compares equal distinct normalized frame histories. Compare equal produced prefixes when administrative termination interrupts execution; termination occurrence is separately governed below.

**Administrative declassification:** the agent-facing control allowlist contains fixed build-independent schema/profile/capability negotiation, public sequence/chunk acknowledgements, input-validation results based only on public input/schema, and generic terminal closure. Resource use can influence whether/when closure occurs; this **liveness/termination fact is explicitly declassified**, not silently claimed noninterfering. No reason, diagnostic, duration, worker identifier, seed, hidden-state hash, or privileged outcome is declassified.

Strict mode uses §6.5's generic closure for crash, limit, reset, save, and normal exit. A trusted operator API can retain detailed classes and metrics, but agents cannot negotiate access to it. These obligations are independently testable and do not claim upstream presentation is bug-free.

### 7.2 Leak vectors and closures

| Vector | Closure |
|---|---|
| Raw glyph/species/object/tile identity | Rendered tuples only; public palette identity never uses raw IDs. |
| Hidden glyph flags | Apply fixed visible style rules; no differentiation by undisplayed cause. |
| Background terrain | Disable/discard layer; retain only actually rendered frame color. |
| World lists, traps, vision, hidden status | No serializer access or traversal; honor displayed field/condition rules. |
| Invented entity/FOV information | No authoritative nearby list, stable monster IDs, hidden hostility, or current-visibility mask. |
| Menu identifiers | Private copied native mapping; public insertion-order row IDs only. No dereferencing for extra facts. |
| Menu icons | Same rendered-appearance boundary as map, with menu-specific visible attributes. |
| Hidden prompt choices/mixed escapes | Public choices stop before Escape; decode mixed glyphs privately to displayed representation. |
| Lua | Trusted immutable game-data Lua only; no agent execution, writable hooks, or wizard loading. |
| Modes/configuration | Early non-restorable bootstrap latch, bypass personal config/environment, runtime allowlist, restore quarantine. |
| Diagnostics/raw output | Private descriptors and producer-level routing; no English-string filtering as a safety boundary. |
| External facilities | Compile out/disable shell, editor, pager, suspend, message handlers; sandbox defense in depth. |
| Saves/checkpoints/level files/bones | Private isolated storage; opaque trusted capabilities; disable cross-episode bones for independent evaluation. |
| Logs/replays | Agent-readable logs contain only sanctioned player/control records. Seeds, diagnostics, private outcomes remain separate. |
| OS access | Separate credentials/sandbox, no ptrace or process-memory access, agent-visible dumps, shared writable roots, or inherited secrets. |
| Timing/resource metadata | No explicit metrics; only documented terminal liveness declassification. Stronger timing protection requires deployment controls. |

### 7.3 Diagnostic isolation is a required core change

`impossible()` is a verified leak producer (`src/pline.c:598-603`). In agent mode, route details privately and terminate generically rather than invoking public gameplay text. Audit panic/debug/raw/external-handler paths similarly. A port cannot recover missing text provenance reliably by matching message strings.

### 7.4 Limits stated honestly

An agent with direct worker memory/filesystem access can cheat. A same-user convenience runner is not a secure evaluation deployment. Low-overhead C adapters do not prevent cache, scheduling, wall-clock, or memory-corruption side channels. Hardware isolation, scheduling/padding, and restricted clocks may be needed for a stronger threat model, with throughput costs.

The strict contract forbids explicit hidden metadata and permits only the documented control envelope; it does not falsely promise termination-sensitive or physical timing noninterference.

## 8. Build and repository integration

### Existing modification points

| Path | Intended integration |
|---|---|
| `include/winprocs.h` | **Append `wp_agent` after current last entry `wp_trace` (`13-23`), never insert.** Do not add a universal callback member for private command notification. |
| `src/windows.c` | Declare/register `agent_procs` under `AGENT_GRAPHICS` beside existing choices (`92-109`); enforce locked selection through bootstrap policy. |
| `sys/unix/Makefile.src` | Add agent source/object groups, rules, dependency/source inventories and supervisor target. Tty groups: `242-245`; inventory: `549`; linked `WINOBJ`: `637`. Avoid tty/ncurses fallback (`417-418`). |
| `sys/unix/hints/include/multiw-1.500` | Document/select proposed `WANT_WIN_AGENT`; existing window choice documentation starts at `7-9`. |
| `sys/unix/hints/include/multiw-2.500` | Add agent enabled/default/source/object/flag branches; agent-only must satisfy the at-least-one-port test (`38-47`, `57-87`). |
| `sys/unix/hints/linux.500` | Supported agent profile/runner wiring; default already derives from `WANT_DEFAULT` (`285-287`). Prefer a small profile include to duplicated hints. |
| `sys/unix/README.hints` | Document `WANT_WIN_AGENT=1` and `WANT_DEFAULT=agent`, beside existing switches (`28-46`, reviewer citation). |
| `sys/unix/NewInstall.unx` | Document agent-only and combined developer builds and no-terminal dependencies beside existing build instructions (`58-76`, reviewer citation). |
| `sys/unix/unixmain.c` | Bootstrap before window/config paths (`104`, `150`), argument policy (`170-174`), restore/publication gate (`243-320`), minimal lifecycle integration. |
| `src/options.c`, `include/optlist.h` | Bypass personal rc/environment (`options.c:7091-7128`, `7322-7344`) and centrally gate all runtime/direct profile-changing setters. Classify options; UI restrictions alone are insufficient. |
| `src/restore.c` | Compatibility/publication barrier around restoration of flags (`576-603`); bootstrap policy cannot be overwritten by save data. |
| `src/cmd.c` | Fresh-command notification (`5135-5144`), command policy where compile-time exclusions are insufficient. |
| `src/pline.c` | Diagnostic isolation at producer (`584-603`) and audit related routes. |
| `Files` | Add every shipped source/header/document/hints/test file; authoritative manifest per `AGENTS.md:61-63`. |

Proposed new files are listed in §3. Follow the existing declaration/include pattern used by `src/windows.c`; `include/windconf.h` is Windows platform configuration, not a generic window registry. Any additional configuration-header edits must follow verified build paths, not guesses.

### Build policy

- Agent-only: proposed `WANT_WIN_AGENT=1 WANT_DEFAULT=agent`.
- Combined tty+agent builds are useful for developer parity testing; trusted bootstrap still locks agent episodes.
- **`WANT_WIN_ALL` does not include agent.** Existing human frontend defaults remain unchanged. Combining `WANT_WIN_ALL=1 WANT_WIN_AGENT=1` explicitly enables both; agent production should specify its default explicitly.
- Keep trusted Lua/DLB loading and static data packaging. Protocol data does not belong in DLB except deliberately player-readable help.
- Modify hints/source Makefile inputs, not generated configuration as the primary integration mechanism.

Follow C99/style guidance, preserve existing NHDT stamps, and update `Files` (`AGENTS.md:53-63`). This new design document needs no stamps. Existing validation is build/run plus optional wizard Lua tests, not an existing standalone suite (`AGENTS.md:65-76`). Avoid destructive install workflows in tests: `make install` replaces the playground (`43-45`).

## 9. Failure handling and operational behavior

- Bound frame bytes, nesting, string length, key count, menu rows/selections, and continuation length before allocation. Reject unknown required fields and inconsistent types.
- One outstanding gameplay request; chunk acknowledgements/paging/resync are transport operations only.
- Reject stale request/version, illegal count/coordinate, wrong menu generation, duplicate row, or wrong action kind without consuming engine input.
- Track accepted action IDs with their content. Identical retries replay retained responses; conflicting reuse fails. Never execute a request twice. If response state is unavailable, require valid resync or close, not re-execution.
- Preserve unexpected prompts; do not guess or repeatedly feed Escape.
- EOF/disconnect defaults to truncation/termination. Optional native hangup-save requires separate tests (`src/cmd.c:5193-5240`). Public strict closure stays generic.
- Backpressure or page large content; do not silently omit text or negotiated audit frames. Hard resource exhaustion terminates under the control policy, not by inventing successful completion.
- Crash discards worker state; reset terminates/reaps before deleting owned files. Never reuse a crashed process or its menu pointers.
- Detailed parser/engine failures remain private. Agent-visible validation errors are limited to public schema/input facts; runtime failure does not publish a hidden reason.

## 10. Alternatives and tradeoffs

**PTY/tty scraping:** useful as a test oracle, not production. It adds terminal initialization/parsing, fragile prompt recognition, and screen-dump overhead.

**Lua extension:** useful for trusted content/fixtures, rejected as agent surface because it grants privileged state access and can bypass gameplay.

**Raw glyphs/RL vectors:** rejected for the strict product because their distinctions exceed ordinary rendered knowledge. A privileged research API would be a separate product, not a flag on this interface.

**In-process reset/library embedding:** deferred pending measurement and a reset proof. Fresh processes fit the verified lifecycle.

**Large structured command language:** rejected as primary API; it duplicates command semantics. Native input plus narrow conveniences is simpler and safer.

**Always-lossless animation in LLM prompts:** rejected as default because it defeats tail token budgets. Retain explicitly negotiated audit replay with exact state semantics, while default final-only delivery documents its temporal information loss.

**TTY-identical menu pages/toggles:** unnecessary for legal structured play. Authoritative row IDs and explicit final sets remove page-local selector ambiguity. Group/bulk convenience is omitted rather than approximating `SKIPINVERT`.

**Compressed full screens:** byte compression is not token reduction once expanded. Sparse presentation deltas are the appropriate level.

## 11. Ordered implementation handoff and acceptance gates

### Phase 1 — Freeze safe presentation, state machines, and menu contract

Define the complete fixed profile/options manifest, callback disclosure table, condition styles, coordinates, wire schemas, control allowlist, final LLM formatter, durable/audit state machines, and §5.6 menu contract. Implement no dependency on a future supervisor for this gate.

**Harness available:** new standalone sanitizer/protocol/menu-model fixtures with synthetic callback inputs; specification-level replay vectors. Engine headers are absent from public encoder tests.

**Gate:** equal-looking glyph inputs produce identical public cells/icons; hidden prompt suffixes/identifiers never encode. Tests model selector-zero selectable rows, preselected `PICK_ANY`, positive/default counts, empty versus cancel, headings, duplicate text, advisory accelerator ambiguity, and explicit rejection of omitted group/bulk operations. State vectors cover a projectile returning to the original durable map.

### Phase 2 — End-to-end headless vertical slice

Implement a **minimal trusted launcher now**: controlled environment/arguments, private transport and stdout/stderr, fixed configuration roots, per-episode private writable directory, bounded one-request/one-response framing, and fresh-process reset. Add early bootstrap latch, configuration bypass, runtime policy gates, diagnostic isolation, and restore quarantine before public output.

Implement the native port's lifecycle, windows, map, status, text/history, required input callbacks, and correct basic menus including all final-set/count/preselection/cancel rules. Start with full snapshots. Add blocking content paging if required to honor framing limits. No typed continuation optimizer, delta encoder, audit stream, opaque save service, or process pool is needed for this gate.

**Harness available:** a temporary scripted JSON driver using the minimal launcher; manual primitive responses to every nested prompt. Native save/restore can be tested by a trusted fixture controller placing the produced save into a fresh private worker directory—no Phase 5 token API required.

**Gate:** no controlling terminal, `TERM`, or display server; agent-only link has no tty/curses dependencies. Complete selection, movement, inventory, text entry, targeting, menu cancellation, native save/restore, quit, and reset. Hostile `HOME`/`.nethackrc`/`NETHACKOPTIONS`, `-D`/`-X`/windowtype flags, runtime `O`/binding/symbol/handler changes, incompatible rendering saves, and wizard saves must either be rejected before player publication or produce the exact permitted fixed-profile transcript. Rejection may only reveal the strict generic control closure, never raw diagnostics. Test zero-selector multi-page menus and preselected counted stacks in actual callbacks.

### Phase 3 — Transaction conveniences and robust retry

Add command-boundary notification, guarded typed continuations, normal command-name conveniences, request/content deduplication, full-snapshot resync, and robust action validation. Basic menu correctness and framing already exist.

**Harness available:** extend the Phase 2 scripted driver with stale/repeated/fragmented requests and nested-prompt scenarios; standalone parser fuzz/property tests are proposed new infrastructure.

**Gate:** direction commands, wield/throw/zap, containers, payment, naming, and unexpected confirmation pause correctly. Counts are not fresh commands; leftover keys cannot become later actions. Duplicate menu text/accelerators pause continuation matching. Invalid/stale actions consume no input, and identical retries do not execute twice.

### Phase 4 — Durable deltas, negotiated audit replay, LLM presentation

Implement normalized comparisons, durable palette allocation, snapshots, chunk/ack rules, and the independent transient scratch-state machine. Implement `llm-final-v1` and `audit-frames-v1` negotiation; preserve all messages/prompts/text in both.

**Harness available:** Phase 3 driver plus deterministic replay client, chunk-splitting proxy, and callback-level rendering fixtures. Compatible tty rendering is an optional differential oracle, not a production dependency.

**Gate:** redundant internal redraws do not create final-only changes or consume durable palette IDs. Movement avoids full-map retransmission. Clear/redraw, polymorph fields, detection, swallowing, underwater views, history, and blocking temporary maps reconstruct correctly.

For a projectile crossing/restoring several cells, apply every frame incrementally, retry each record, split at every legal chunk boundary, and resync before/after every frame including partial assembly. Every path ends with the same durable map **and the same exactly-once ordered distinct visible audit frames**. A final unchanged map has an empty durable map patch but complete audit replay. Final-only mode intentionally lacks those nonblocking frames and is tested against its own documented projection.

### Phase 5 — Production isolation, control services, and throughput

Extend—not introduce—the launcher with hardened credential/filesystem/process isolation, opaque save tokens, private seed/outcome storage, CPU/wall-clock/resource limits, bounded parallelism, and optional independent worker prewarming after measurement.

**Harness available:** existing scripted/replay driver plus sandbox-negative tests, concurrency/reset stress driver, and a trusted benchmark collector inaccessible to agents.

**Gate:** thousands of episodes show no accumulating workers/descriptors/files, collisions, stale menus/messages/maps, or unbounded supervisor memory. Reset at prompts and during long activity works. Agent attempts to read saves, diagnostics, process memory, configuration, or other workers fail. Fault/limit/reset all produce the exact strict control envelope, without specific reasons or identifiers.

### Phase 6 — Information hygiene and performance release gate

Use trusted synthetic fixtures and, where helpful, wizard Lua in a **separate fixture-construction build**. Evaluation runs through locked normal-play workers; production never enables wizard Lua to execute tests. Supplied reviewer spot checks are not substitutes for these new tests.

Test unseen monsters, secret doors/corridors, traps, concealed objects, indistinguishable species/gender, unknown BUC/enchantment, hallucination, blindness, telepathy, warning, invisibility, mimics, remembered versus seen cells, detection, hidden underlying terrain, highlight precedence, and native menu identifiers. Also cover mixed text, hidden response suffixes/defaults/numbers/Escape, diagnostics, external facilities, hostile startup/runtime options, restore incompatibility, and endgame disclosure ordering.

Menu regressions must include selector-zero multi-page menus, preselected `PICK_ANY`, counted stacks, accepted empty, cancel, headings, duplicate visible text, glyphs differing only in raw identity, advisory groups, and `SKIPINVERT` rows. Verify group/bulk operations are explicitly unsupported in v1 while direct final-set selection remains correct. If such conveniences are added later, their exact native inversion semantics become a release gate.

**Player gate:** compare canonical player payloads for equal public presentation/action histories under each negotiated policy. Hidden differences must not change durable palette IDs, row/request IDs, deltas, text, or audit output. For administrative termination compare the equal available prefix; do not pretend a killed worker produced further observations.

**Control gate:** separately validate every control record against the strict allowlist. Inject crashes, resets, resource limits, bootstrap failures, and restore failures. Require generic closure without classification, timing fields, diagnostics, seeds, PIDs, hashes, or correlation tokens. Delivery timing and termination existence remain explicitly declassified liveness, not an untestable claim of equality.

**Performance gate:** record cold/warm spawn-to-first-input, reset latency, native simulation throughput, encode/decode time, allocations, bytes, actual tokenizer counts, and memory/descriptors/files over long runs. Include travel, animation, large menus, and long-message tails, not only simple moves.

Initial proposed LLM delivery budgets, to be ratified with measured tokenizer results before release:

- At most 2,048 model tokens per ordinary delivered LLM page; no more than 512 tokens of protocol/formatter overhead per page, excluding required player content.
- Report p50/p95/p99 and maximum ordinary-action tokens separately from initial snapshots and paged text/menu episodes. Target p95 ordinary movement/search/wait responses below 512 tokens on the agreed corpus.
- Required content exceeding the page budget uses lossless continuation pages, not truncation. Report total tokens/pages for the complete interaction and bound total buffered/spooled bytes through a documented resource policy. No claim that every legal menu can fit in one page.
- Nonblocking animation count must not increase final-only map-output tokens for an otherwise equal final presentation/messages/prompts. Audit throughput and tails are measured separately.
- Set and publish explicit wall-clock, total episode output/spool, and pending-page limits from benchmark capacity before production. Limit failure uses the generic closure policy; it is not a successful shortened observation.

These are proposed acceptance targets, not measured performance promises. If changed after baseline measurement, version/document the policy and rationale; do not silently weaken information completeness to meet them.

## 12. Remaining decisions and risks

1. **Exact profile manifest:** Phase 1 must classify all effective options and displayed status conditions for this build. No production publication before that inventory and enforcement audit are complete.
2. **Runtime setter coverage:** central gates must also cover direct/bypass setter paths, frontend switching, bindings, and handlers. A grep or disabled `O` menu alone is not proof.
3. **Restore ordering:** verify actual flag-loading and side-effect paths around the cited restore code; enforce quarantine before restored settings can affect public/external behavior. Unknown/untrusted save provenance is rejected.
4. **Diagnostic audit:** `impossible()` is a verified leak, not proof of exhaustiveness. Trace supported-build panic/debug/raw/handler output before claiming closure.
5. **Input completeness:** counts, replay, prefixes, and nested requests need integration tests despite the verified direction/position contexts and proposed command hook.
6. **Temporal information tradeoff:** final-only mode intentionally omits nonblocking map animation. Decision-time maps and complete text remain mandatory; exact transient fidelity requires negotiated audit mode and its storage/backpressure cost.
7. **Fine-grained turn stepping:** deferred; a per-engine-turn API or interruptible occupation hook requires separate design.
8. **Reproducible seeding:** RNG initialization/control sites remain to be verified. Seed values stay private regardless of implementation choice.
9. **Terminal signaling:** inspect native completion/save paths rather than equating `exit_nhwindows()` or process exit with successful save, death, or victory. Only privileged control needs detailed classes.
10. **Physical side channels:** timing/cache/resource resistance beyond the explicit control policy is deployment-dependent and costs throughput. It is not promised by this port.
11. **Portability and budgets:** Unix/Linux is the initial target. Other platform builds and proposed tokenizer/performance budgets require actual validation; none are claimed tested here.

The architecture is ready for the ordered handoff above. Its release claim rests on early immutable policy enforcement, a narrow rendering boundary, complete input/menu semantics, explicit state and transport rules, and separate verification of player information and administrative declassification—not on the assumption that all window callback data is safe.
