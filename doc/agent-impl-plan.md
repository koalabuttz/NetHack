# Implementation Plan — Agent Interface Phases 1 & 2

Handoff plan produced by architect review of `doc/agent-architecture.md`. Authoritative spec: `doc/agent-architecture.md`. Where implementation shortcuts conflict with the spec, the spec's stricter interpretation wins: early trust boundary, no raw diagnostics, no engine identity in observations, lossless required content, final menu sets, fresh-process reset.

## 1. Scope and milestone gates

**P1 — frozen contract and engine-free fixtures.** Deliver the option/profile inventory, capability and callback disclosure table, wire schema, menu model, normalized appearance sanitizer, durable/audit reference model, replay vectors, and a simple canonical client formatter. Encoder/model fixtures must compile without engine headers or engine objects. Production audit streaming remains out.

**M1 — build/launch/bootstrap/quarantine.** Port registers, launcher starts a worker over a private socketpair, descriptor latch occurs before configuration, controlled roots and environment are enforced, all diagnostics stay private, and a validated startup can emit hello plus a synthetic/character-selection request. No claim of safe playable integration until the policy inventory and runtime gate are connected. Proves agent-only linking and the trusted startup boundary.

**M2 — native rendering and basic input.** New game/selection, map, cursor, enabled status, messages/history, text windows, key/direction/position/line/yn/extcmd callbacks, full durable snapshots, exactly one outstanding native input request, bounded paging. A move normally causes one act and one logical obs; it is not necessarily one engine turn. Proves terminal-free native control and complete presentation at decision boundaries.

**M3 — complete basic menus and interaction coverage.** All §5.6 final-set/count/preselection/cancel rules, heading/selector-zero/duplicate-text rows, bounded lossless pages, native inventory/targeting/text entry/help, repeated selections. No raw key/bulk/group shortcuts inside menu requests. Proves legal structured play without tty semantics emulation.

**M4 — lifecycle and negative gates.** Native save to private storage, trusted fixture restore into a fresh process, actual restored-flags validation, normal quit/endgame, disconnect/reset/reaping/cleanup, hostile startup/runtime configuration tests. Proves the full Phase 2 gate. No opaque save token API; a trusted test controller owns save files and provenance metadata.

Out of scope: delta production, audit production negotiation, continuations/key queues, command convenience DSL, fine-grained occupation interrupts, user seed control, public save uploads/tokens, process pool, fork server, hardened multi-user sandbox/performance release claims. Same-user test runner is explicitly not secure evaluation deployment (§7.4).

## 2. Load-bearing source findings

* `include/winprocs.h:13-23`: append `wp_agent` after `wp_trace`; existing IDs must not shift. Callback ABI is lines 26-99.
* `src/windows.c:7-8,92-109,267-284`: existing header/choice pattern; choose_windows loops winchoices, copies the entire procs struct, and calls an optional initialization routine. Guard selection before that loop.
* **Build correction:** actual names are `WINTTYSRC` and `WINTTYOBJ`, not `WINTTY_SRC` (`sys/unix/Makefile.src:242-245`). `multiw-2.500:83-88` adds tty sources/objects when enabled and `-DNOTTYGRAPHICS` otherwise. It does **not** add `-DTTY_GRAPHICS`; `include/config.h:55-57` defines TTY_GRAPHICS unless NOTTYGRAPHICS. Inspection of config1.h found no tty selection gate there. Do not edit config1.h or windconf.h for registration.
* `sys/unix/Makefile.src:417-418` falls back to tty objects and ncurses if variables are undefined. Agent-only must define WINOBJ and WINLIB even when the latter is empty. WINCSRC is line 549; linked WINOBJ is line 637; final WINLIB link is line 669.
* `multiw-2.500:32-48,57-80`: WANT_WIN_ALL only human ports, no-port fallback and default selection need agent-aware branches. `linux.500:469` finally assigns WINOBJ from WINOBJ0. `linux.500:354-356` adds CURSESLIB if defined; ensure agent-only does not accidentally configure it.
* `unixmain.c:58-66,104,128-150,170-174`: main currently calls early_init, choose_windows, reads NETHACKDIR/HACKDIR, runs early_options/chdirx, then initoptions and process_options. A hook after initoptions is too late. `src/allmain.c:27-38` also initializes crash reporting in early_init: perform the OS-only descriptor latch before early_init, then engine-dependent profile setup after global initialization.
* `options.c:7093-7128,7133-7165,7300-7343`: system configuration appears in both initoptions and initoptions_init; finish invokes rcfile. `src/cfgfiles.c:1912-1963` actually implements rcfile, reading both NETHACKOPTIONS and HACKOPTIONS. Its alternate passes at 2027-2070 call the same function. This additional file must be changed, not just options.c.
* Option handlers are called via allopt at options.c:638,7460,8528,8648,8698,8967,9074. do_handler can bypass parseoptions. `cmd.c:2669` bind_key, `options.c:7628` parsebindings, and `symbols.c:681,781` load_symset/parsesymbols are extra mutation seams.
* `restore.c:579-603`: Sfi_flag overwrites flags at 580, before saved wizard/discovery modes are processed. Validate immediately after deserialization and before line 583 onward. `unixmain.c:243-279` also emits restore output before/around dorecover, so a port publication gate is independently necessary.
* `pline.c:584-616`: impossible formats diagnostics then invokes public pline at 603 and further diagnostic text at 615-616. Branch before recursive panic/public paths; do not filter strings in putstr.
* `wintype.h:10-38,66-71,104-109`: anything is an opaque union; result includes identifier, long count, unsigned itemflags; glyph_info has raw glyph and rich metadata. `wintty.c:2596,2612-2621`: selectability is identifier->a_void != NULL, identifier is copied by value, initial count is -1, preselection comes from itemflags. There is no positive-count add_menu parameter.
* `wintty.c:3924-3937`: visible frame color has precedence over pet attribute, then pile/detection/BW inverse; female highlighting requires wizard. Raw reasons must collapse to identical style.
* `botl.c:1683-1716`: status enables XP versus HD depending on polymorph and optional fields from flags. `botl.c:781-849` supplies condition display strings/default enablement. Never serialize generic callback pointers, percentages, or the raw condition bitmask.
* `win/tty/getline.c:314-325`: normalize extended command with mungspaces, then extcmds_match(..., ECM_IGNOREAC | ECM_EXACTMATCH, &matches); return the sole native index privately, not over the wire.
* `include/global.h:382-383`: COLNO=80, ROWNO=21. Public legal coordinates are x=1..79, y=0..20; column zero is unused.
* `unixmain.c:125-140,476-535`: -d overrides environment playground, chdirx honors supplied dir even under SECURE (drops elevated IDs), otherwise HACKDIR is used. VAR_PLAYGROUND can assign writable prefixes. `linux.500:284-287` enables DLB, embeds HACKDIR and absolute SYSCF_FILE. Changing cwd/HACKDIR environment does not relocate that absolute system config.
* `Makefile.top:104-106,280-281,326-327`: DLB static staging is nhdat plus DATNODLB (license, symbols and selected port extras). `test/README.md:3` and AGENTS.md:73-76 require no-DLB only for existing wizard Lua tests. New standalone and normal agent integration tests do not require disabling DLB.

## 3. Contract decisions for Phase 1

These are recommended v1 decisions, not claims that schemas currently exist.

### 3.1 Profile inventory and enforcement

Rendering profile is exactly `normal-ascii-color-v1`; delivery policy is `llm-final-v1`. Freeze **all active options** for this Linux build in a checked-in TSV manifest with columns: canonical option name; availability/build guard; resolved startup value; classification; setter/handler entry points; saved-field binding if applicable; rationale. Do not accept a catch-all 'default' at runtime. Generate/check the inventory against the active optlist entries and separately list non-optlist symbol/condition/binding/capability state. New/missing/unclassified entries fail the build/test and prevent profile readiness.

Chosen classification policy:

* Ordinary gameplay option values: pin this revision's compiled defaults, with explicit overrides below. The manifest records resolved values, including compound values, rather than depending on future defaults. Classify every option as locked presentation, locked gameplay, unavailable/external, or trusted startup character field. Runtime mutation allowlist is empty in this round; read-only inspection is allowed. Choosing native gameplay actions is not 'option mutation'.
* Allow role/race/gender/alignment/name selection through ordinary callbacks or launcher allowlisted startup data; no arbitrary argv/OPTIONS surface.
* Force agent window, normal mode (debug/explore/fuzzer false), primary/Rogue built-in ASCII symbols, color on, inverse on, pet and pile highlighting on, pet attribute inverse. No custom symbols/colors, Unicode glyph handlers, tiles, hidden background terrain layer, perm inventory, mouse, sound, shell/suspend/pager/editor/mail/Lua user hooks, alternate-meta interpretation, cross-episode bones. Standard native bindings, number_pad off. No wall-clock/time metadata fields in protocol.
* Displayed game time on; optional score/experience-point total/version/weapon/armor/terrain status off; HP bar and configurable status highlighting off. Preserve ordinary enabled base fields, XP/HD applicability and title changes; do not force fields active merely because callbacks exist. Read displayed time as formatted status, not moves.
* Conditions: freeze default enabled subset from botl.c:820-849: blind, conf, deaf, iron, fly, foodPois, grab, hallucinat, lava, levitate, ride, slime, stone, strngl, stun, termIll. Other listed conditions off. Use the full ordinary text strings at botl.c:781-812, ordered by ranking then visible condition option name as the native comparator does; no status color/style override (color none, style 0). Do not expose false/disabled conditions.
* Capabilities: `WC_COLOR | WC_HILITE_PET | WC_INVERSE | WC_EIGHT_BIT_IN`; `WC2_FLUSH_STATUS | WC2_RESET_STATUS` only initially. All 16 basic colors supported. Do not claim WC2_HILITE_STATUS, hitpoint bar, extra status, permanent inventory, Unicode, enhanced colors, mouse, or suspend. Add further bits only with a named behavior and fixture. WC2_FLUSH_STATUS is important: botl.h:214 uses it in deciding status handling.

A baseline of pinned compiled defaults plus explicit overrides is deliberate: it avoids inventing hundreds of gameplay changes. Producing the fully expanded inventory is a required coding task, not something this inspection has already exhaustively audited. Profile-readiness must fail until that expansion and setter coverage review are done.

### 3.2 Public types and appearance

Public C headers use only stdint.h/stddef.h/stdbool.h; no engine includes. Example interface shapes:

```c
struct agent_cell { uint8_t ch, fg, style, frame; };
enum agent_result { AG_OK, AG_BAD_INPUT, AG_LIMIT, AG_IO, AG_INTERNAL };
bool agent_normalize_appearance(const struct agent_render_input *,
                                enum agent_render_context, struct agent_cell *);
```

The synthetic render_input contains only the candidate character/basic color/frame and already reduced display booleans (pet attribute/inverse candidate), never a glyph number, tile, symbol index, pointer, or entire MG mask. Engine bridge computes those candidate effects; the standalone sanitizer tests normalization/precedence. Add engine-facing equality fixtures later to prove raw glyph changes are discarded by the bridge as well. This avoids pretending an engine-free synthetic test proves the bridge automatically.

Public appearance tuple `[char,color,style,frame]`: ASCII scalar character string; colors named black/red/green/brown/blue/magenta/cyan/gray/orange/brightgreen/yellow/brightblue/brightmagenta/brightcyan/white plus the remaining native basic-color slot mapped explicitly in the manifest (do not guess CLR_* numbering), and `none`; style public bits bold=1, dim=2, italic=4, underline=8, blink=16, inverse=32. Normalize native ATR_* values, which are not this wire mask. Frame is a basic color or none, never background terrain identity. Set blank palette entry 0 to `[" ","none",0,"none"]`. Enforce ASCII built-in glyph profile rather than publishing raw/non-ASCII symbol fallbacks.

Map sanitizer follows tty's displayed precedence, discards raw identity and wizard-only reasons. Menu context uses menu-displayed icon rules, not map-only pet/detection flags; headings/no displayed icon use null. Mixed text must be decoded privately to displayed symbols (src/windows.c:1465-1486 is the spec's cited model). Byte/control handling must be consistent with visible text semantics; never export authenticated mixed-glyph escape payloads.

### 3.3 Wire grammar, bounds, and framing

UTF-8 compact JSON lines, no BOM, no comments, exact integer parsing, reject duplicate/unknown keys, invalid UTF-8/surrogates, NaN/fractions for integers, NUL, excessive nesting. Escape all JSON control characters. Fixed field order in canonical encoder; no hash-table iteration order.

Constants: protocol v=1; max physical line 65,536 bytes including LF; input nesting 8; max object keys 32; max tokens 32,768; max action bytes 65,535; key byte 1..255; line input maximum `min(255, native destination capacity-1)` UTF-8 bytes (advertise request max); 32 concurrent windows; 65,535 rows/menu and text lines/window; 1 MiB logical individual text value (split at UTF-8 boundaries); 32 MiB retained public state/content+unacked spool per worker; content page max 128 rows/lines and 16 KiB encoded content; one outstanding gameplay request; counters 1..2^53-1 (uint64_t internally; close generically before wrap); menu counts -1 or 1..2,147,483,647, additionally <= LONG_MAX. Input errors do not consume input; internal/resource failures close generically. These are local engineering bounds, not measured production budgets (§11 Phase 6 budgets remain deferred).

Four principal logical records:

1. hello (control), emitted once after compatible startup/restore readiness and before any player payload:

```json
{"v":1,"ch":"control","type":"hello","d":1,"profile":"normal-ascii-color-v1","policy":"llm-final-v1","caps":["snapshot","menu","paging"],"coord":"engine-map","size":[80,21],"x0":1,"y0":0,"limits":{"line":65536,"page_bytes":16384,"page_rows":128,"count":2147483647}}
```

No build hash, pid, seed, worker ID, version-string fingerprint, or diagnostic field. Limits are fixed contract constants, not current resource use. Phase 2 does not advertise delta, ev, continuation, save-token, or resync support.

2. obs (player): fields `v,ch,type,d,seq,base,s,pal,map,cur,msg,hist,windows,need`. Phase 2 uses base:null for every full snapshot. First seq=1; seq increments only at a decision/blocking/final boundary. `s` maps canonical field name to `{text,color,style}` or null deletion in reserved delta mode; conditions are an ordered array of displayed `{text,color,style}` entries, not bits. `pal` is `[id,char,color,style,frame]` definitions; `map` is `[x,y,palette_id]` triples, row-major, all legal cells in a full snapshot. `cur` is `[x,y]` or null. `msg` events are `{e,text,style}`; restored history uses the separate `hist` event list, same presentation ID discipline. `windows` is ordered content descriptors `{w,kind,title,mode,content,pages}` (mode only menus); content IDs/window IDs are monotonically presentation-derived, never native winid/file paths. Full snapshots include current complete content or immutable content references with retrievable pages. `need` null at final boundary or a tagged request.

Request common `{id,kind}`; types:
* command/key/direction: optional displayed prompt; byte answers only in Phase 2. Direction is a context label, not direct engine mutation.
* position: displayed prompt and legal map bounds; answer byte or explicit position primitive only where nh_poskey/getpos accepts it. This primitive returns native coordinate/modifier values, no mouse capability claim or hidden object lookup.
* yn: prompt, visible choices (prefix before Escape; null for unrestricted), default (byte or null), numeric true only if displayed choices include numeric affordance. Native full choices remain private.
* line/extcmd: displayed prompt, max byte count; extcmd answer is text, resolved privately with the native exact matcher.
* menu: `{id,kind:"menu",menu:"mN",mode:"none|one|any",content:"cN",pages:N}`.
* ack: `{id,kind:"ack",content:"cN",pages:N}`; must publish all required content before accepting acknowledgement.

Normal character selection uses menus/line/yn, not a second privileged input path.

Menu page rows exactly `{r,text,selectable,key,group,initial,style,color,icon}`; r starts at 1 and includes headings; key/group byte or null; initial null/-1/positive. Menu generation `mN` plus request ID scope row mappings. Final selection is explicit, insertion-order normalized.

3. act input: `{"v":1,"type":"act","seq":N,"id":R,"action":A}`. A is exactly one tagged shape: `{key:B}`, `{text:S}`, `{position:[x,y],mod:0}`, `{yn:B,count:C?}`, `{menu:"mN",commit:[[r,count],...]}`, `{cancel:true}`, `{ack:true}`. Escape/cancel mappings vary by native callback; line/extcmd cancel returns the native Escape/-1 result; menu cancel is -1/no result. No then/group/selectall/invert/bulk/raw-menu-key fields. Native key bytes include control/meta codes without terminal escape-sequence interpretation; 0 rejected except internally returning native position sentinel after a valid position answer. Numeric yn sets yn_number only through valid native numeric semantics; do not conflate native numeric counts with menu counts.

4. terminal closure is **exactly** `{"v":1,"ch":"control","type":"closed"}`. No d, reason, request ID, timestamp, outcome, etc. This is an explicit exception to §4.5's 'every record has a delivery counter', because §6.5 mandates the exact bare closure; terminal closure is not retryable. Document this reconciliation in the schema.

Transport auxiliaries (strict control allowlist): `get_page`, `ack_chunk`, `ack_seq` inputs; `page`, `chunk`, `invalid` outputs; reserve resync only for later advertised support. `invalid` contains only d and public code from schema/stale/kind/range/incomplete; never diagnostic text. It leaves the outstanding request unchanged. Unknown top-level schemas produce invalid(schema) or generic close for framing exhaustion. Conflicting accepted-action ID reuse closes; never execute twice.

Large logical obs is an ordered chunk record stream, not partial independently applicable observations. Chunk outer fields: `v,ch:"control",type:"chunk",d,rid,i,last,parts`. `rid` is the logical output record ID (delivery ID of first chunk); each physical chunk has its own monotonically assigned d; retries preserve d and exact bytes. `parts` is an array of tagged path/value pieces. Header scalars appear in part 0; map/pal/msg/status/window arrays are split only between elements, long text uses `{path,offset,text,last}` with offsets in UTF-8 byte counts at scalar boundaries. Phase 1 must enumerate paths in schema, no arbitrary JSON pointer patch language. The client assembles and validates the entire logical record before atomic application. Stable menu/text page content can be sent as separate page records; selections are forbidden until every required page has been delivered and acknowledged. A page request/response does not resume gameplay. This maintains one action/one logical response, not a false promise that a giant menu fits one physical line.

Phase 2 implements bounded framing/paging and at least last accepted action identity+content: stale/duplicate accepted requests must never execute twice. Robust historical retry/resync remains Phase 3. Retain/replay the last response if available; otherwise generic close rather than re-execution. Fix behavior in fixtures so later retry support does not change gameplay semantics.

### 3.4 State machine reference model

Enums in engine-free model:
* lifecycle `AG_BOOT, AG_QUARANTINE, AG_READY, AG_WAIT_INPUT, AG_EXECUTING, AG_DRAINING, AG_CLOSED`;
* delivery `AG_TX_IDLE, AG_TX_RECORD, AG_TX_WAIT_CHUNK_ACK, AG_TX_WAIT_SEQ_ACK`;
* audit reference `AG_AUDIT_IDLE, AG_AUDIT_OPEN, AG_AUDIT_ENDED`.

Working W updates on sanitized callbacks. At an unsatisfied input, blocking display, or final ordinary completion: freeze immutable D(n+1), allocate new palette tuples in row-major map then displayed content order, make new request/event counters only from public presentation, emit against Dn. Phase 2 emits full snapshots but retains the same D semantics. Nonblocking display/BL_FLUSH/delay never increment seq. Delays never sleep.

Audit model (P1 fixtures only): begin `{interval,base}` clones Dn to scratch T; changed normalized frame `{interval,k,prev,patch}` uses inline appearance tuples; identical frames suppressed; end `{interval,last,target}` precedes final durable commit against Dn, then scratch reset/discard. No durable palette IDs allocated by animation. Model chunk acknowledgement, atomic assembly, duplicate retry, full durable acknowledgement, and resync replay from retained Dn+interval prefix exactly as §4.5. Keep unacknowledged durable/audit records immutable and bounded; no action reexecution. Production implementation of this model is Phase 4, not a Phase 2 advertised feature.

## 4. Bootstrap and policy sequence

### Launcher trust model

`sys/unix/agent_runner.c` builds standalone `src/nethack-agent`, not linked to game globals. Public stdin/stdout are JSON lines only. Supervisor binary path/data-root/sysconf-root/start fields are trusted invocation parameters, not player frame fields. No public 'worker argv', filename, environment, or arbitrary channel selector.

For each episode: ensure real/effective uid/gid equal (refuse setuid execution); mkdtemp mode 0700; create save and private diagnostic/spool directories; create socketpair(AF_UNIX,SOCK_STREAM); fork an uninitialized launcher child, setsid to eliminate controlling terminal, route worker stdin to /dev/null and stdout/stderr to private bounded diagnostic sink, close all descriptors except explicitly owned bootstrap/game socket and diagnostics; exec the fixed worker with a private `--agent-fd=N` locator. Socket FD ownership and supervisor-controlled exec are authority; the locator or magic bytes alone are not authentication against a same-user adversary. Validate socket type/peer and bounded handshake; avoid claims SO_PEERCRED/magic alone establishes cryptographic trust. OS credential hardening remains Phase 5.

Use a binary/internal fixed bootstrap handshake <=4096 bytes over that descriptor before exposing player frames: magic/version, required profile, trusted static/data/config root identifiers/paths, private writable root, mode new/restore and trusted restore metadata. No untrusted JSON from the player is reused as handshake. Launcher closes its unused socket end, controls signals/reaping, and enforces memory/output/deadline caps privately.

Environment is constructed, not filtered: fixed locale (C.UTF-8 for JSON text; ASCII glyph profile), HOME private empty dir, fixed USER/LOGNAME or explicit native name path, no TERM/DISPLAY/WAYLAND_DISPLAY/NETHACKOPTIONS/HACKOPTIONS/NETHACKDIR/pagers/Lua loader settings/LD_* inherited. Fixed locale requirement should be documented/tested; if unavailable use a verified UTF-8 validation implementation independent of locale. No shell invocation for exec or cleanup.

### Worker hook order

1. At top of unixmain main before current early_init (line 66), OS-only `agent_bootstrap_probe(&argc,&argv)`. Store latch in private static non-restorable storage, close publication gate, verify/consume only the internal FD argument/handshake. Agent-only build without valid bootstrap exits privately before normal initialization; combined build without any agent locator follows human startup unchanged. Combined explicit `-wagent` without a trusted latch must fail, not synthesize trust.
2. Run early_init; after it, `agent_bootstrap_after_globals()`. Initialize private prefixes/diagnostic routing and runtime policy phase. Avoid referencing engine globals before early_init resets them. If crashreport or other early side effects can run in the supported build, exclude them in the trusted profile or add a narrow agent branch before activation; private stderr alone does not disable external crash submission.
3. At choose_windows line 104 force agent when latched; `choose_windows` itself refuses any non-agent choice under latch and agent without latch. Avoid trusting restorable iflags.windowtype_locked as the security lock.
4. Bypass normal environment/early_options for agent workers. Use only supervisor-validated `-d` equivalent/private directory and character start data; reject -D/-X/-w/config/symbol/hooks before ordinary argument parsing. Do not let rejected early --showpaths/--version produce public output. Guard console probes/init and shell/pager/mail/sound startup branches (`unixmain.c:142-147,175-190`) for agent mode in combined builds.
5. In initoptions_init, retain necessary built-in engine initialization (allopt, RNG, commands, symbols, fruit); ignore cmdline window overrides and terminal-dependent symbol detection for agent mode. Trusted internal initialization scope is private and narrow, not inferred from the mutable go.opt_initial flag. Apply explicit profile overrides during trusted option initialization before any untrusted source could run.
6. Both SYSCF reads use a fixed launcher-approved immutable config root in agent mode; reject unknown/external directives. In initoptions_finish replace the agent rcfile call with profile finishing while retaining fruit/symbol bookkeeping afterward. Independently return before getenv in cfgfiles.c:rcfile for agent mode; guards on read_config_file/parse_config_line accept only explicitly trusted source/root. This closes alternate rc passes and legacy HACKOPTIONS.
7. Route all optfn dispatch through a helper `int agent_checked_option_call(int opt, int req, boolean neg, char *opts, char *buf)` (or a generic wrapper named without agent with conditional policy). Reads/init under scoped trusted setup allowed; setters/do_handler denied before invocation. Guard boolean addr mutation too, not only optfn calls. Include explicit guards at parsebindings, bind_key(user=true), load_symset/parsesymbols, condition configuration handlers, and message/external hook configuration paths. Core reset_commands/user=false bindings and legitimate level-driven primary/Rogue switching remain allowed. A read-only O view or fixed denial is acceptable; hiding O alone is not the boundary.
8. Command dispatch rejects external facilities, enter-explore, wizard/Lua/fuzzer and policy-changing handlers even in combined builds; normal help/look/inventory/save/quit remain native. No Phase 2 key queue, so fresh-command notification can remain deferred as §11 Phase 3 specifies. If added now at parse after initial flush, keep it payload-free and do not infer fresh commands from commandInp (counts reuse it).
9. Startup readiness opens the gate before ordinary new-game character selection, after profile validation and the decision that no restore is pending. On every attempt_restore path close the gate; no early character prompt should block a quarantined restore unexpectedly. Restore fixture supplies a fixed name, no rename/save-selection UI. On restore, validate trusted metadata before opening file, then validate actual flags immediately after Sfi_flag, before set_playmode/role_init. Reject, do not downgrade, saved debug/explore/profile changes. On failure terminate generically, do not fall through to a new character game.
10. Quarantined output is held/discarded with provenance: diagnostics private, restore history tagged history, valid ordinary restored presentation released only after successful compatible restoration and normal redraw. Reset all public caches, menu mappings, continuations, counters. First restored observation full snapshot; do not force world scans from a nested callback.

### Diagnostics

Add agent branch at the start of impossible before recursive panic handling, consuming va_args into a bounded private diagnostic routine and terminating with a low-level path that cannot call exit_nhwindows/pline. The launcher alone emits closed. Preserve human behavior byte-for-byte outside latch. Audit supported-build panic/raw/external-handler paths; raw_print is private/fail-closed in agent mode, not a catch-all public message route. If a normally visible early message must be preserved, give its producer a reviewed presentation path; do not promote arbitrary raw diagnostics. `exit_nhwindows` is not proof of save/death/victory. For Phase 2 generic closure needs no private outcome taxonomy; the save fixture verifies native save artifact and compatible restored progress, never exit status alone.

### Data/reset

Stage immutable nhdat/license/symbols once into trusted test data root. Set DATAPREFIX/HACKPREFIX to that root and every writable prefix (save/level/bones/lock/score/trouble) to the private episode root after initialization, ensuring options initialization cannot overwrite them. Configure SYSCONFPREFIX separately and do not assume it overrides absolute SYSCF_FILE automatically. Use a single trusted config-path accessor for agent branches. Create record/logfile/xlogfile/perm/save directory as native startup requires; avoid global score/bones files. Retain static data sharing across resets, never symlink mutable files into the source or shared playground.

Reset is trusted supervisor control on a separate inaccessible FD/API, not a player act. Kill/reap worker (terminate then kill after private deadline), close socket, clean only the mkdtemp-owned tree without following symlinks, allocate a fresh worker and public connection/counter namespace. Old connection receives generic closed. A single invocation may own a trusted reset/control FD for fixture testing; no arbitrary public privileged ch.

## 5. File-by-file implementation work

All paths below labelled NEW are proposed. Rough sizes are planning estimates, not line-count targets.

### New files

1. **NEW doc/agent-interface.md** (350-600 lines): complete protocol/profile/callback disclosure/state machine/menu specification, exceptions and staged capabilities.
2. **NEW doc/agent-profile-v1.tsv** (one row per active option plus symbols/conditions/caps): exhaustive frozen effective values and mutation-source classification. Do not copy hidden game-state values here.
3. **NEW doc/agent-v1.schema.json** (400-700 lines): exact tagged unions/bounds, no additionalProperties, principal/transport records, page/part grammar. Include later-capability records but explicitly disallow them in Phase 2 negotiation.
4. **NEW win/agent/agent_types.h** (150-250 lines): pointer-free owned public values, counters, errors, request/action types and hard limits. Arrays/strings may use owned buffers internally, but no opaque engine pointers/native IDs in public semantic values.
5. **NEW win/agent/agent_view.h/.c** (~60/200-350): engine-free appearance/text normalization; `bool agent_normalize_appearance(const struct agent_render_input *, enum agent_render_context, struct agent_cell *);` and `bool agent_visible_choices(const char *, size_t, struct agent_text *);`. Hidden suffixes stripped before encoder sees them.
6. **NEW win/agent/agent_protocol.h/.c** (~150/700-1000): bounded JSON reader/writer, full snapshots, physical/chunk framing, stable paging, public validation results, last request bookkeeping. `enum agent_result agent_parse_action(const char *, size_t, struct agent_action *);`, `enum agent_result agent_commit(struct agent_session *, const struct agent_view *, const struct agent_need *);`, `enum agent_result agent_receive(struct agent_session *, struct agent_action *);`. No hack.h; I/O through explicit read/write callbacks, not ambient stdout.
7. **NEW win/agent/agent_menu.h/.c** (~80/250-450): standalone public menu model/validation. `enum agent_result agent_menu_validate(const struct agent_menu *, const struct agent_menu_answer *, struct agent_selection *);`. Native anything is absent; selection result contains public row/count pairs only.
8. **NEW win/agent/agent_input.c** (450-700): engine-facing primitive native conversion, yn/default/Escape/numeric, getlin/extcmd, input context and menu callback response handoff. `int agent_input_key(enum agent_need_kind);`, `char agent_input_yn(const char *, const char *, char);`, `void agent_input_line(const char *, char *);`, `int agent_input_extcmd(void);`. No continuation engine this round.
9. **NEW win/agent/winagent.c** (900-1400): agent_procs and callback implementations, W ownership, windows/history/status, private native menu sidecars, player selection and names using native role legality helpers. `void agent_render_glyph(const glyph_info *, const glyph_info *, enum agent_render_context, struct agent_cell *);` stays engine-facing here. This is the only translation path from glyph metadata to public appearance.
10. **NEW include/winagent.h** (100-160): engine-facing declaration of agent_procs, exact callbacks and bootstrap integration functions. Public headers should not include it.
11. **NEW win/agent/agent_bootstrap.c** (450-750): non-restorable latch, root/profile setup, runtime policy, quarantine, low-level diagnostic close. `void agent_bootstrap_probe(int *, char ***);`, `boolean agent_mode(void);`, `void agent_bootstrap_after_globals(void);`, `boolean agent_policy_option(int, int);`, `boolean agent_policy_command(int (*)(void));`, `void agent_validate_restored_flags(void);`, `void agent_publication_ready(void);`, `void agent_private_fatal(const char *);`. Signature details for variadic diagnostic helper can use va_list, not public wire errors.
12. **NEW win/agent/agent_profile.h** (generated/checked-in data inventory, ~100-400): frozen classification/value table and explicit overrides usable by bootstrap. Keep exhaustive manifest-to-active-optlist check in tests; no unconstrained runtime fallback.
13. **NEW sys/unix/agent_runner.c** (600-900): standalone executable, safe launch/transport allowlist/private dirs/controlled env/trusted reset/reap; `int main(int,char **);`, internal `int spawn_worker(const struct runner_config *, struct worker *);`, `void reap_worker(struct worker *);`. It may link engine-free protocol code, never game library.
14. **NEW sys/unix/hints/include/agent.500** (~60-100): conditional agent runner/profile compilation support, feature exclusions for an explicit strict agent build variable (recommend WANT_AGENT_STRICT=1), no tty deps, trusted static/config path defines. Human-only/combined normal defaults unchanged. Compile-time NOSHELL/NOSUSPEND supported by unixconf.h:289-292,321-322; other exclusions must follow their actual guards rather than guessing names.
15. **NEW test/agent/Makefile** (~80-130): plain-C tests, standalone include isolation, sanitizer builds, script integration entry points.
16. **NEW test/agent/test_view.c** (~200): identical appearances, visible style precedence, hidden yn suffix, invalid text/mixed bridge expectations.
17. **NEW test/agent/test_menu.c** (~300): all §5.6 vectors, count overflow, 0 selector, duplicate text, empty/cancel, no bulk conveniences.
18. **NEW test/agent/test_protocol.c** (~300): bounds, escaping, fragmented lines/short writes, tagged unions, stale action no-consume, chunk/page incompleteness and counters.
19. **NEW test/agent/reference_state.h/.c** (~60/350): pure durable/audit state reference; `enum agent_result agent_model_step(struct agent_model *, const struct agent_model_event *);` used only fixtures now.
20. **NEW test/agent/test_state.c** (~250): projectile returning to original map, palette isolation, retries/chunk boundaries/resync replay vectors. Store vectors as C tables to avoid another required dependency/file format.
21. **NEW test/agent/driver.py** (~400-600): standard-library scripted JSON driver/strict schema assertions/simple canonical human-readable formatter, page/chunk assembly, trusted episode controller, reset/save-copy fixture tests. Not a production dependency.
22. **NEW test/agent/profile_check.py** (~150-250): compare explicit manifest against mechanically dumped active optlist/profile entries, detect unknown fields/values. Avoid pretending regex alone resolves C preprocessor variants.
23. **NEW test/agent/native_fixture.c** (~200-350): conditional test-build callback fixture for actual ANY_P copy/zero selectors/counts, diagnostic injection, incompatible restored-flags checks. Not linked into production binary, not accessible via public commands.
24. **NEW test/agent/sysconf** (~30-80): reviewed minimal immutable normal-play test config; no developer/user hooks or writable paths. Runner supplies private writable roots separately.
25. **NEW test/agent/README.md** (~150): proposed commands, coverage, no-install staging, trusted versus public control, limitations.

### Existing files to modify

* include/winprocs.h:13-23 append enum; no universal command callback addition.
* src/windows.c:7-8 add guarded winagent include; 98-109 register `{ &agent_procs, 0 CHAINR(0) }` following actual initializer shape; 267 before selection loop enforce latch/window policy. Agent initializes itself through init_nhwindows; no registry init routine required.
* sys/unix/Makefile.src:242-245 add WINAGENTSRC/WINAGENTOBJ groups with TARGETPFX object names; 340-418 add explicitly empty WINAGENTLIB and suppress fallback through hints; 549 add WINAGENTSRC inventory; add compilation/dependency rules alongside tty rules; 646 all builds runner when selected; separate runner link excludes HOBJ/Lua/tty. Add clean rules for runner/agent objects and include runner source appropriately in source inventories without linking its main into game.
* multiw-1.500:7-20 document WANT_WIN_AGENT and WANT_DEFAULT=agent. multiw-2.500:38-48 include agent in no-port fallback; 57-76 default agent only when no prior human choice; after 83-99 add AGENT_GRAPHICS/WINAGENTSRC/WINAGENTOBJ/WINAGENTLIB. Never add it to WANT_WIN_ALL lines 32-36.
* linux.500:284-288 integrate conditional strict profile include/agent trusted root defines; ensure no sound/external features for strict agent build; 354-356 avoid CURSESLIB creation/use when neither tty nor curses chosen; 469 keep WINOBJ0 aggregation. Do not overwrite existing human flags globally for combined builds.
* sys/unix/Makefile.top:104-106,138,280-281 add **non-destructive** `agent-test-data` target that depends on nhdat/data and copies immutable data to a fresh caller-owned AGENT_TEST_DATA directory. No rm -rf existing playground, no install dependency. Refuse nonempty/unowned destination except explicit idempotent verified data update. Stage test sysconf separately. Forward agent runner build target as needed.
* unixmain.c:66,104,128-150,157,170-198,243-320,476-535: startup order/controlled dirs/names/arguments/console/pager/mail bypass/readiness/restore and private writable prefixes described above. Avoid getlogin-derived real host name in agent episode.
* options.c:489-638 central mutation gate; 7093-7343 trusted system paths and no-rc initialization; 7460 scoped do_init; 7628 binding guard; 8698/8967 direct handler gate and any direct boolean writes. All optfn invocations route through checked wrapper so additions have one obvious policy seam.
* include/optlist.h:44-48,77-86 add/refer to option policy classification in existing macro-generated tables, or use separate indexed table with completeness assertion. Prefer separate agent table to keep non-agent table ABI unchanged; do not bulk hand-edit every optfn function.
* src/cfgfiles.c:1407,1642 source/root gate and directive classification; 1912 immediate agent rc bypass closes 2027-2070 alternate passes.
* src/cmd.c: command policy before dispatch (spec anchor 479-481), bind_key:2669 and rebind/autocomplete handler entries:2296,2414,2455. Fresh parse notification at spec anchor 5135-5144 deferred unless added as inert private hook; no continuation work.
* src/symbols.c:681,781 gate custom symbol loading/parsing, allow only scoped trusted init and verified native built-in primary/Rogue changes.
* src/botl.c: condopt mutation around 1308-1327 and interactive condition configuration around 1417-1449 need policy guard if reachable outside checked optfn. Preserve normal condition evaluation. This avoids relying solely on O denial.
* src/restore.c:580 immediately validate loaded flags under !SFCTOOL/AGENT_GRAPHICS guard; quarantine before mode logic. Preserve non-agent/SFCTOOL behavior.
* src/pline.c:584 agent impossible producer isolation, and supported-build pline handler activation region near 638 audit/gate. Other diagnostic producer file edits are contingent on supported-build audit; do not claim this sampled inspection proved exhaustive panic/output closure.
* sys/unix/README.hints and sys/unix/NewInstall.unx: add agent-only/combined no-install instructions beside existing window/build instructions (architecture cites README.hints:28-46/NewInstall.unx:58-76; these specific line ranges were supplied by spec, not separately reread).
* Files: add every shipped new source/header/doc/hints/test path. Preserve existing NHDT stamps; new files need no stamps per design §8/AGENTS.

## 6. Exact callback coverage / safe optional stubs

Use C99 designated initializers for agent_procs to avoid positional shifts under conditional fields; copy signatures from winprocs.h, not tty-specific prototypes. Required functions with `agent_` prefix:

`void init_nhwindows(int *,char **); void player_selection(void); void askname(void); void get_nh_event(void); void exit_nhwindows(const char *); void suspend_nhwindows(const char *); void resume_nhwindows(void);`
`winid create_nhwindow(int); void clear_nhwindow(winid); void display_nhwindow(winid,boolean); void destroy_nhwindow(winid); void curs(winid,int,int); void putstr(winid,int,const char *); void putmixed(winid,int,const char *); void display_file(const char *,boolean);`
`void start_menu(winid,unsigned long); void add_menu(winid,const glyph_info *,const ANY_P *,char,char,int,int,const char *,unsigned int); void end_menu(winid,const char *); int select_menu(winid,int,MENU_ITEM_P **); char message_menu(char,int,const char *);`
`void mark_synch(void); void wait_synch(void);` conditional `void cliparound(int,int); void update_positionbar(char *);`
`void print_glyph(winid,coordxy,coordxy,const glyph_info *,const glyph_info *); void raw_print(const char *); void raw_print_bold(const char *); int nhgetch(void); int nh_poskey(coordxy *,coordxy *,int *); void nhbell(void); int doprev_message(void); char yn_function(const char *,const char *,char); void getlin(const char *,char *); int get_ext_cmd(void); void number_pad(int); void delay_output(void);`
Conditional CHANGE_COLOR `void change_color(int,long,int); char *get_color_string(void);`; MAC68K conditional `void change_background(int); short set_font_name(winid,char *);` only if compiling that ABI (Linux gate does not promise Mac support).
`void outrip(winid,int,time_t); void preference_update(const char *); char *getmsghistory(boolean); void putmsghistory(const char *,boolean); void status_init(void); void status_finish(void); void status_enablefield(int,const char *,const char *,boolean); void status_update(int,genericptr_t,int,int,int,unsigned long *); boolean can_suspend(void); void update_inventory(int); win_request_info *ctrl_nhwindow(winid,int,win_request_info *);`

Event pump/bell/delay/number_pad (after checking locked value), nonblocking sync, cliparound/positionbar (no clipping advertised), inventory notification without permanent inventory can be no-ops. can_suspend false; suspend is policy-denied, never SIGSTOP; resume no-op. Optional color mutation is denied, color-string query safely returns stable supported default. ctrl_nhwindow returns only ABI-approved unchanged/unsupported response, no private pointer on wire. genl_outrip can be reused only after confirming it renders via ordinary callbacks; avoid tty-dependent generic status or file helpers. display_file must use trusted native DLB/data resolution and text windows, not cat/pager subprocess. Blocking display/yn/getlin/menu/status/history cannot be stubs. Every compiled callback slot should be non-null unless native ABI explicitly documents null handling.

Native menu sidecar: rows own a copied ANY_P and itemflags, never dereference. add_menu initial selected=>-1; positive counts only from an accepted prior selection. Validate public row/count list completely before allocating native `menu_item[]`; initialize *out=NULL even on error/cancel; sort result by insertion order; return 0 empty or -1 cancel. Do not clamp by hidden stack quantity. Free per-request active mappings at completion; if repeated select_menu is legal for same constructed menu, retain/rebuild its private template until restart/destroy, while old request IDs are dead. Document this ownership clarification: releasing active mapping cannot destroy the only identifiers required by a later native select_menu. Retained public transcript stores no pointers.

## 7. Verification commands and acceptance

Run these only during implementation, after wiring proposed targets. Do not invoke make install (including WANT_SOURCE_INSTALL which linux.500:514-516 makes trigger install).

### Build

```sh
cd /home/david/projects/nethack
(cd sys/unix && sh setup.sh hints/linux.500)
make fetch-Lua             # only if Lua is absent; network required
make WANT_WIN_AGENT=1 WANT_DEFAULT=agent WANT_AGENT_STRICT=1 all
make -C test/agent check
```

WANT_AGENT_STRICT is a **new proposed hints variable**, not an existing command. Agent-only is selected by absence of WANT_WIN_TTY; do not pass WANT_WIN_TTY=0 because these hints use ifdef and zero still enables it. Strict adds only verified feature exclusions. Do not pass WANT_WIN_ALL for production.

Changing window/CFLAGS sets may leave stale objects. In a dedicated clean worktree/build checkout (or after preserving anything generated the developer needs):

```sh
make spotless
(cd sys/unix && sh setup.sh hints/linux.500)
make WANT_WIN_AGENT=1 WANT_WIN_TTY=1 WANT_DEFAULT=tty all
```

This is the developer combined build; test both normal tty unaffected and trusted agent latch forcing agent despite tty default. Repeat clean setup for tty-only `make WANT_WIN_TTY=1 all`. Use compiler warnings and symbol/link inspection (`ldd src/nethack`, `nm`/link map) to prove agent-only has no tty/curses/ncurses link dependencies, rather than trusting port selection alone. Strict tests need not remove DLB. Existing wizard Lua tests require a separate non-DLB fixture-construction build; never enable wizard Lua in evaluation worker.

Standalone direct compile smoke (once files exist):

```sh
cc -std=c99 -Wall -Wextra -Werror -Iwin/agent \
  win/agent/agent_view.c win/agent/agent_menu.c \
  test/agent/test_menu.c -o /tmp/nethack-agent-menu-test
/tmp/nethack-agent-menu-test
make -C test/agent check
make -C test/agent sanitize    # proposed ASan/UBSan target, compiler permitting
```

No -Iinclude, engine objects, Lua, tty, or supervisor for P1 check. Use compiler dependency output to enforce no engine headers, not just grep #include in one file.

### Private staging / running in place

The worker binary is src/nethack, runner src/nethack-agent. New staging target:

```sh
root=$(mktemp -d)
make WANT_WIN_AGENT=1 WANT_DEFAULT=agent WANT_AGENT_STRICT=1 \
  AGENT_TEST_DATA="$root/data" agent-test-data
python3 test/agent/driver.py episode \
  --runner "$PWD/src/nethack-agent" --worker "$PWD/src/nethack" \
  --data "$root/data" --sysconf "$root/data/sysconf" \
  --private-root "$root/episodes"
python3 test/agent/driver.py suite --root "$root" \
  --runner "$PWD/src/nethack-agent" --worker "$PWD/src/nethack"
```

These driver/target flags are proposed contract to implement. Stage nhdat/license/symbols and reviewed sysconf once; launcher makes private writable roots. No need to copy binary/static data per episode. Test with setsid and absent TERM/DISPLAY, not a PTY.

Verified existing alternative for a human developer build is `src/nethack -d <private-playground> ...` (or NETHACKDIR), with required data/record/save files staged; an absolute SYSCF_FILE still must exist at the compiled path. Therefore **do not claim `cd dat && ../src/nethack` or HACKDIR environment alone works with stock linux.500**. Our explicit agent config-root accessor removes that reliance for the new runner.

### Gates by task

P1: raw-identity variation in native bridge fixture later; engine-free equal reduced appearances now; hidden suffix never encodes; all menu cases; exact parsing/escaping/bounds; palette animation independence; projectile out/back with unchanged durable map and nonempty audit frames; every chunk split/retry/resync produces same reference state/frame ledger.

M1: no tty/TERM/display dependency; missing/invalid FD agent-only fails privately; arbitrary -wagent cannot establish mode; fixed hello only after validation; hostile HOME/rc/environment never read; stdout/stderr/panic diagnostic markers never on public channel; reset reaps before cleanup. Native fixture injection compiled only in test worker.

M2: select character through ordinary menus, create game, move/wait/search with one act per native request, display status/time and map, inventory/text help, naming/line entry, yes/no default/Escape/unrestricted/numeric, position targeting and direction, full snapshot apply/reconstruct at every decision. No seq advance for redundant nonblocking display or sleep delay; no forced bot/vision in nested prompt. UTF-8 content complete and losslessly paged.

M3: actual callback fixture for selector=0 selectable rows over multiple pages, headings/duplicate text, preselected PICK_ANY, positive count roundtrip/default count, selected SKIPINVERT row legal by explicit ID, empty vs cancel, stale generation and duplicate row rejected without consuming request; no selection until required pages delivered; repeated select_menu owns identifiers safely. Caller owns allocated result and ASan reports no UAF/leaks.

M4: save using native command, prove native ordinary save presentation plus artifact, transfer save with trusted build/data/profile/owner metadata into fresh private root, restore same ordinary progress with first full snapshot/new seq/history tagging. Wizard/discovery/incompatible option saves rejected before any player payload; runtime O/bindings/symbols/condition/message handlers cannot alter profile; normal quit/endgame and injected fault/EOF/reset all finish with exactly bare closed. Do not infer success from exit status. Loop at least 100 resets at menus/key/line prompts and ensure processes/FDs/files plateau (thousands/performance production gate later).

## 8. Risks and required mitigations

1. **Exhaustive profile/setter audit is real work.** The active optlist manifest cannot be replaced by 'defaults safe'. Common gate plus direct mutation source coverage; classify system-config directives and private init scopes. Snapshot/assert profile at input boundaries is defense in depth, not a substitute for rejecting before side effects. This is the largest judgment-heavy task.
2. **Early globals reset latch.** Private static OS latch must survive early_init and restore. Never store security authority in flags/iflags. Guard pre-early_init crash/external paths or compile them out in strict build.
3. **Agent-only fallback dependencies.** ifdef variables, empty WINLIB, fallback tty, CURSESLIB and generated flag stale objects can defeat headlessness. Clean separate configurations and inspect link outputs.
4. **ABI conditionals/types.** coordxy is not necessarily int; add_menu has both attr and color; status ptr types change by field; compile CLIPPING/POSITIONBAR/CHANGE_COLOR variations where supported. Designated initializer and exact prototypes prevent shifted slots.
5. **Status information leak.** Only enabled displayed fields, formatted string and visible styles; BL_FLUSH/RESET are batching, not observations; conditions translate known enabled masks to display labels, ignore percentages/hidden metadata. Golden polymorph enable/disable tests.
6. **Glyph equality.** Do not compare/hash raw glyph_info or MG bits for public palette IDs; normalize first. Menu icons have separate renderer context. Whole glyph struct includes raw index/tile/custom color pointers; never pass to encoder.
7. **Native menu lifetime.** ANY_P is copied, not serialized/dereferenced; immutable row IDs scoped to generation+request; result malloc compatible with caller free; long range checks before cast; no quantity oracle. Repeated select_menu needs a retained private template despite completed request mapping retirement.
8. **Yn semantics.** Hidden suffix stays private; blank response/default/Escape/numeric require actual tty semantic port, not generic 'character in public choices'. Rejection must be explained only by public schema, not enumerate hidden accepted choices. Native validation can accept hidden response byte if ordinarily legal without publishing suffix.
9. **Extended command semantics.** Return exact native matcher result, dispatcher still enforces policy; never export index/list containing wizard commands. No separate name-to-engine function call shortcut.
10. **Blocking/quarantine deadlock.** Character selection needs publication after startup readiness; restore must stay closed through loaded flag validation and redraw. Quarantined invalid save that prompts must terminate privately instead of soliciting a public answer or hanging forever.
11. **Raw/panic output provenance.** impossible is verified, not exhaustive proof. Audit actual supported preprocessor build, external pline handler and crash reporter routes. Generic close only; no reason-correlated control IDs.
12. **Text completeness vs bounded memory.** Pages/chunks cannot truncate messages or authorize choices. Exceed hard retained limits => generic termination, never shortened 'success'. UTF-8 safe boundaries and escaped byte budget tests mandatory.
13. **One RPC misunderstanding.** Logical response can have pages/chunks; gameplay may have nested prompts or no turn progression. Transport acknowledgements never count as engine input.
14. **Reset/filesystem trust.** mkdtemp ownership, no recursive symlink traversal, descriptor CLOEXEC discipline, no shared bones/logs/saves, reaping before delete. Same-user convenience runner is not a security sandbox; do not market it as one.
15. **Build/profile compatibility metadata.** Trusted save metadata can hold hashes privately; hello cannot expose them. Validate actual flags too. Restored gameplay mutable state is legitimate; compare only profile-locked persisted settings, not every flags bit (some are engine progression).

## 9. Ordered executable handoff (dependencies; task size)

1. **[Judgment, 1 unit]** Expand active options/non-option manifest and callback disclosure table; decide every row using baseline+overrides above. Dependency none. Exit criterion no unclassified active option, condition, binding/symbol source, capability or external facility.
2. **[Judgment, 1 unit]** Freeze schema/page/chunk grammar/limits and state vectors; record exact closed-counter exception and repeated-menu mapping clarification. Depends 1. Exit criterion validators accept every golden vector and reject forbidden keys.
3. **[Mechanical+tests, 1-2 units]** Public types/view/menu model/encoder parser/reference model + standalone Makefile/tests. Depends 2. Exit P1 gate with no engine headers.
4. **[Mechanical, 1 unit]** Build variables/groups/rules/enum/registry/Files/runner skeleton; agent callback table initially private fail-closed placeholders for unimplemented required behavior. Depends 3. Exit compile/link both agent-only and tty-only unchanged; do not publish gameplay with stubs.
5. **[Judgment, 1-2 units]** Launcher controlled exec, descriptors, data staging, environment/private roots, low-level latch and safe diagnostic termination. Depends 4. Exit no bootstrap fallback or stdout leakage.
6. **[Judgment, 2 units]** Profile application/rc bypass/common and direct setter gates/command/external policy; bootstrap manifest readiness. Depends 1,5. Exit hostile startup/runtime mutation tests pass before ordinary publication. M1 gate.
7. **[Mechanical+semantic, 1-2 units]** Native window W/map/status/text/history adapter/full snapshots and blocking paging; bridge sanitizer fixtures. Depends 3,6. Exit synthetic native callback transcripts reconstruct perfectly.
8. **[Judgment, 1-2 units]** Native input callbacks and character selection with no queues/conveniences; yn/line/extcmd and position semantics. Depends 7. Exit playable movement/text/targeting M2.
9. **[Mechanical+semantic, 1 unit]** Native ANY_P menu sidecars/result ownership/full final-set contract + actual multipage/count/preselect fixtures. Depends 7,8 (can share menu scaffolding needed for character selection earlier). Exit M3.
10. **[Judgment, 1-2 units]** Restore trusted metadata/quarantine/actual flags gate/history reset, native save fixture, generic final drain/close, trusted fresh reset/reaping. Depends 6-9. Exit complete M4.
11. **[Verification, 1-2 units]** Clean agent-only/combined/tty regression, hostile config matrix, sanitizer tests, diagnostic injections, 100-reset resource plateau. Depends 10. Fix any uncovered mutation/output path rather than waiving it.
12. **[Mechanical review, 1 unit]** Final Files/docs/build instructions/profile audit evidence; report exactly commands/results and remaining production-only work. Depends 11.

'Unit' means a coding subagent progress-sized work item, not a time promise. This is too much for one unreviewed patch: land P1, build/bootstrap, rendering/input/menu, lifecycle verification as reviewable commits while keeping default human build unchanged.

## 10. Remaining verification obligations / decisions

No operator input is needed to choose the main architecture. The proposals above resolve the protocol/staging/scope decisions. Three mandatory implementation-stage audits remain and must not be represented as already verified: exhaustive resolved active-option/direct-setter inventory; full supported-build diagnostic/external-facility producer coverage; complete restored state side effects beyond the verified Sfi_flag seam. Additional files may require narrow guards after those audits; naming fictitious exhaustive paths would be less useful than explicitly gating publication on their completion.

One minor schema editorial action from task 1 — copying the exact 16 CLR_* color names from the native color header into the manifest — is DONE: `doc/agent-profile-v1.tsv` (generated by `test/agent/gen_profile.py`) now contains all 16 native color slots including `NO_COLOR` (slot 8, wire name `none`), verified against `include/color.h:14-30`. The remaining three audits above are still open and gate M1/M4 publication respectively.
