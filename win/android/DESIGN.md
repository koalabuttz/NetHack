# win/android — Controller + touch UI for NetHack 5.0

**Status:** design only. No code yet. This document is the spec we build the port
against; open questions are collected at the end and are expected to change.

**Target hardware:** Android handhelds with a physical gamepad — the reference
device is a Retroid Pocket 6 (D-pad, dual sticks with L3/R3, A/B/X/Y, L1/R1
bumpers, L2/R2 triggers, Start, Select, touchscreen).

**What this is:** a new NetHack *window port*, a peer of `win/tty`, `win/curses`,
`win/win32`, and `win/agent`. It implements the same `struct window_procs`
interface; the game core is untouched. See "Implementation seam" at the bottom.

---

## Decided parameters

These three were settled up front and the rest of the design follows from them:

1. **Audience: both, via a toggle.** One physical button map. An `Assist` flag
   changes *behavior and labels*, never *button positions*, so muscle memory
   transfers between newcomer and purist modes.
2. **Controller-complete; touch optional.** Everything must be doable on the
   gamepad alone. Touch is a convenience layer that may be absent (docked play,
   a controller without a touchscreen). Nothing may *require* it.
3. **Movement: D-pad primary.** The D-pad is the main movement input; the left
   stick is the guaranteed-diagonal fallback (see "Diagonals").

---

## The core problem & strategy

NetHack has ~100 commands; the gamepad has ~16 inputs. The genre-standard answer
(Shiren, Pixel Dungeon, Crawl-touch) is to **embrace modality** — a roguelike is
already modal (roaming / menu / aiming / count / text), so buttons remap per
context instead of cramming everything into one layer.

Four pillars:

1. **Item-first interaction.** Don't pick a verb then an item. Open inventory →
   pick item → the game offers only the verbs valid for *that* object (NetHack
   already knows: a potion offers quaff/throw/dip/drop/name). Collapses ~10 verbs
   into one flow.
2. **Context "A" button.** One button does the obvious adjacent/underfoot thing,
   with an on-screen label of exactly what it will do.
3. **Two wheels.** A fast radial for items (X) and one for non-item actions (Y);
   a full searchable list behind them covers the long tail.
4. **Touch hybrid (optional).** Where present, tap-to-travel and tap-an-item
   erase the awkward cases — but every one of them has a stick-only path too.

---

## Free-roam button map

| Input | Action |
|---|---|
| **D-pad** | Move 1 step, 8-way (primary) |
| **Left stick** | Alt 8-way move — the guaranteed-diagonal path |
| **Right stick** | Look/aim cursor (always-available farlook) |
| **A** | Interact (context) / confirm |
| **B** | Cancel / Back · *hold* = run |
| **X** | Items wheel (item-first) |
| **Y** | Actions wheel (verb-first) |
| **R2** | Fire quiver → aim |
| **L2** | Travel/run modifier (+dir = run that way) |
| **R1 (hold)** | Aim layer (D-pad → targeting cursor; for direction-only prompts) |
| **L1 (hold)** | Quick-slots wheel (favorites) |
| **R3 click** | Inspect tile under cursor / travel there |
| **L3 click** | Autoexplore (or toggle run) |
| **Start** | Game menu — save / options / help / discoveries |
| **Select** | Message log scroll / search any command |

---

## Diagonals (the D-pad-primary concern)

`y u b n` are life-or-death. On a cross D-pad, diagonals are the weak spot. Two
outs, no modifier required:

1. **Tunable simultaneous-press window** — e.g. up+left within ~40–60 ms reads as
   a diagonal. Expose the window as a slider (tight for rollers, loose for
   deliberate players).
2. **Left-stick fallback** — nudge the stick for a *guaranteed* diagonal at any
   time. No mode, no held button; the freed stick exists for exactly this.

---

## The two wheels

**X — Items wheel.** Flick to a category or open the inventory list → pick an
object → its valid verbs appear: wield / wear / put on / take off / remove / eat /
quaff / read / zap / apply / throw / drop / name / ready-as-quiver. Only valid
verbs show.

**Y — Actions wheel** (~8 spokes, the non-item verbs):
`Search` · `Wait/rest` · `Cast spell` · `Look/what-is` · `Up/Down stairs` ·
`Kick` · `Pray` · `More…` → full searchable extended-command (`#`) list.
Eight spokes cover the common case; "More…" keeps coverage at 100%.

---

## The "A" button — and its guardrail

Context buttons annoy purists by *guessing*. Rule: **A only ever does the single
unambiguous thing, and always shows what that is.** A soulslike prompt near the @
or in a status corner:

```
A: descend        A: pick up | dagger        A: open door (W)        A: attack jackal
```

If ambiguous (item *and* a door; two adjacent monsters), A opens a tiny 2–3-item
radial instead of choosing for you. Every verb A could do is *also* reachable
explicitly through the wheels — a purist can ignore A entirely and nothing is ever
hidden or auto-chosen.

---

## Targeting & direction prompts

Two flavors, both via **hold-R1** (or auto-entered when a command needs a target):

- **Free-target** (throw, zap, look): stick/cursor moves freely · **L2 cycles to
  nearest monster** · **A** fires · **B** cancels · **L3** = target self (`.`).
- **Direction-only** (kick, open, close, force, apply-in-direction): stick/D-pad
  snaps to one of 8 · A confirms · up/down for `<`/`>` where relevant.

`R2 = Fire` is the express path: pull trigger → aim → A/release. The most-used
ranged action gets its own dedicated trigger.

---

## Menu mode (inventory, multi-select, item selection)

| Input | Action |
|---|---|
| D-pad Up/Down | Move highlight |
| L1 / R1 | Page up/down |
| D-pad Left/Right | Jump to next object class (tabs across top) |
| A | Toggle/select item (or confirm in single-select) |
| Y | Confirm/accept selection (multi-select) |
| L2 / R2 | Select none / all (NetHack's `-` / `.`) |
| X | Set count for highlighted item |
| B | Cancel |

---

## Count entry

A `+`/`−` spinner: D-pad up/down adjusts, L/R ×10, A confirms. No keyboard.

---

## Text entry (mandatory for controller-complete)

A console-style **grid keyboard**: D-pad moves the caret, A types, B backspace,
X = shift/symbols, Y = done. Because every extended command lives in the Actions
wheel's searchable list, typing is reserved for **naming** and **wizard-wishing**
only. If touch is present, raise the Android soft keyboard instead — but it is
never required.

---

## Travel without touch

- Right stick summons the cursor → **A/R3 = travel there** (NetHack `_` / `G`).
- **L3 = autoexplore** handles most corridor walking with zero aiming.
- **L2 + direction = run** that way until something interesting.

Between those three, long single-stepping is rare — touch-to-travel was only ever
a convenience.

---

## Touch (optional convenience layer)

Where a touchscreen exists: **tap a tile** → travel · **tap an item** → its verb
menu · **drag** → scroll map · **pinch** → zoom tileset · soft keyboard for
naming. Controller handles combat/movement/reflex; touch is the fast path for
navigation/menus. Every touch action has a stick-only equivalent above.

---

## Quick-slots (the veteran feature)

**L1 = favorites wheel.** Bind specific items/actions to 8 spokes — *quaff:
healing*, *zap: digging*, *#pray*, *fire*, *cast: force bolt*. This is the Shiren
L/R-item trick; it's what makes the kit feel fast once learned. Pray-on-a-slot is
clutch in emergencies.

---

## The Assist toggle — exactly what flips

Identical physical layout in both modes; only behavior + labels change:

| | **Assist ON** (newcomer) | **Assist OFF** (purist) |
|---|---|---|
| **A button** | Resolves to the obvious action (attack/stairs/pickup) | A = confirm/pickup only; attack & stairs are explicit verbs → fully predictable, never guesses |
| **Hints** | On-screen "A: descend" labels; verb hints in wheels; labeled class tabs | Hidden; snappier |
| **Safety** | "Are you sure?" on attacking peaceful / known traps | Off — trusts you |
| **Wheels** | Common commands surfaced first | Shows the NetHack **key letter** beside each entry (wheel reads as a graphical keymap); full rebinding allowed |

Switching modes never moves a button. Purist mode just stops narrating and stops
second-guessing — consistent with NetHack's "never decide for the player" ethos.

---

## Controller-completeness coverage

Every NetHack input, stick-only path (no touch required):

| Need | Path |
|---|---|
| 8-way move | D-pad / left stick |
| Run · rush · travel | L2+dir · hold-B · autoexplore (L3) · cursor→A |
| Stairs `< >` | Context-A on stairs · Actions wheel |
| Melee / ranged | Move into foe · context-A · Fire (R2) |
| Pick up `,` | Context-A · Actions wheel |
| Inventory `i` | X |
| All item verbs `w W P T R q r z a t d E` | X → item → valid-verbs menu |
| Quiver `Q` / Fire `f` | X → "ready" / R2 |
| Cast `Z` | Y → Cast → spell list · L1 quick-slot |
| `#` commands (pray, loot, chat, force, dip, offer, sit, ride…) | Y → More… → searchable list |
| Look / whatis `; : /` | Right-stick cursor + R3 |
| Direction prompts (open/close/kick/force) | R1 aim layer + dir |
| Count entry | +/− spinner |
| Menu select / multi-select / counts | Menu-mode map |
| Naming / wishing / raw typing | On-screen grid keyboard |
| Help · options · save · discoveries | Start |
| Message recall | Select |

No row needs the touchscreen.

---

## Engine boundary & versioning

The app should outlive any single NetHack version. The architecture that buys
that: a **serialized protocol** between the app (all UI) and the engine (the
game), so engines slot in behind a stable contract. This section is the "why"
and the "how"; it supersedes a naive single-port build.

### What "slot in" can and can't mean

Two readings, only one achievable:

- **"Any version we've ported a thin seam into slots in."** Real. Each NetHack
  edition is built with a small adapter that speaks the protocol; the app never
  changes. This is the goal.
- **"Any *unmodified* upstream NetHack slots in untouched."** A myth — unless you
  abandon the structured UI (below). Core + window port compile into one binary;
  `struct window_procs` is not stable across versions (procs get added/changed —
  that's what the `WINDOWPORT` version macros are for), 5.0's grouped instance
  globals (`gc.context`, `svm.mons`) don't exist in 3.6.x, and data/save formats
  differ (5.0 Lua dat vs. 3.6 compiled levels). There is no edition where you
  drop in new source and the port just links.

The only way to wrap a truly unmodified engine is to run its TTY build as a
subprocess and make the app a terminal emulator that scrapes the 80x24 screen.
That is version-agnostic but **destroys the control scheme** — the item-first
wheels, context-A, and valid-verb menus all need *structured* state a scraped
character grid can't supply. It is also the screen-parsing the `win/agent` work
explicitly rejects. So: not this.

### The boundary is a serialized protocol (proven prior art — but not in this tree)

Put the boundary in the wire format, not a C ABI. A C-struct ABI is brittle
across versions (layouts, enums); a serialized protocol (JSON / flat text)
absorbs version drift. State goes out structured; commands come in structured.

There is **prior art for exactly this boundary**: the `win/agent` port's `_live/`
protocol (`view.json`, `frame.txt`, `cursor.txt`, `in.txt`) is a serialized
structured-state-out / command-in contract, pointed at an LLM. **Caveat — it is
NOT in this fork.** `win/agent` lives on the separate `agent-window-port` branch
(Windows-local, never pushed); `android-window-port` was forked from clean
`NetHack-5.0` precisely to keep the two experiments decoupled. So it is a
*reference to learn from*, not something present here to adopt — to use it, its
schema must be brought over or re-derived.

The **aspiration** (not yet realized) is convergence:

> the agent port and the android port as **two clients of one protocol** — the
> LLM consuming structured state to *decide*, the controller app consuming the
> same to *render wheels and hints*; then one seam per NetHack version yields both
> agent-play and controller-play.

Whether to pursue that (generalize win/agent's protocol into a shared spec) or to
**design the android protocol fresh for the UI and converge later** is open — see
Open Questions. Crucially, the version-blind machinery below ("declare, forward,
degrade") derives from NetHack's *own* window interface and does **not depend on
win/agent existing**, so protocol design can proceed now from first principles.

### How the app stays version-blind: "declare, forward, degrade"

NetHack is already data-driven internally — it builds its own menus,
character-creation screens, and status line from internal tables. The app's job
is to render *those same tables* forwarded over the wire, never to hardcode one
version's contents. The app branches on **what the engine declares**, never on a
version number. Five moves cover almost everything:

1. **Capability handshake** — on connect the engine declares what it supports
   ("techniques: yes", "autoexplore: no", protocol version, the command set).
2. **Forward the engine's own tables** — command list, glyph/tile set, object-
   class symbols, role/race tables, spell list. The engine already has these
   structs; the seam serializes them.
3. **Engine-computed affordances** — the app asks "what can I do with this
   object/tile?" and the *engine* answers (NetHack already knows). The app never
   reimplements applicability logic.
4. **Primitive-based prompts** — the windowprocs interface is *already* a tiny
   fixed vocabulary: `yn_function`, `getlin` (text), `getdir` (direction),
   `getpos` (target), the menu / `select_menu` API. Every prompt any version
   raises is one of these. The protocol models that handful; the seam tags each
   prompt with its primitive; the app renders primitives, not prompts.
5. **Graceful unknowns** — an unrecognized glyph, status field, or command
   renders generically (default tile, plain row, generic list entry) instead of
   crashing. This is what lets a *newer* engine run against an *older* app.

**The guardrail:** a `version ==` branch anywhere in the app is a bug, not a
shortcut. The moment you are tempted, that is the signal to extend the protocol
or the handshake instead. Violating it gives the worst case — a protocol layer
*and* version-specific app code.

### Where version differences actually land

Across the editions people play, the differences resolve to "a table grew" or
"the status field-list grew" — which the app renders blind:

| Version / variant | UI-surface difference | Stays version-blind via |
|---|---|---|
| 3.4.3 → 3.6 | structured status fields, Unicode glyphs, menucolors, new `#` cmds (`#tip`, `#terrain`, `#overview`) | status field list; glyph table; command list |
| 3.6 → 5.0 | instance-globals refactor, Lua dat, command tweaks | all internal → seam-only; app sees nothing |
| SLASH'EM | new roles/races, techniques (`^T`), masses of monsters/objects, marketplace/forge | role/race tables; command list; glyph + class tables; existing prompt primitives |
| EvilHack / SpliceHack / xNetHack / UnNetHack | new conditions, object materials/properties, new branches, rebalanced cmds | status fields; engine-computed verb list + detail text; just-more-map; command list |

The genuinely hard case — a new interaction *modality* that isn't a choice list,
a glyph grid, status fields, or one of the five prompt primitives (a crafting
grid, a dialogue tree, a real-time minigame) — **does not exist anywhere in the
mainstream NetHack family.** Supporting another favorite version means growing
tables, not branching on versions.

### Does this make updates easier, or just more complex?

Both, split by the *kind* of change — and net easier, because the common kinds
win big:

- **API / compile drift** (winprocs signature, a moved global): confined to the
  seam; app untouched. **Much easier** than the monolith, where the same drift
  sits next to UI code.
- **New content** (monsters / items / commands / glyphs): the handshake
  advertises it; the data-driven app auto-renders. **Much easier.**
- **New interaction primitive**: protocol v2 + app + seam — three layers.
  **Harder** than the monolith's one place — but rare-to-nonexistent in the
  NetHack family (previous section).

Maintenance scales far better: without the boundary, N versions = **O(N) full
ports** (the expensive UI duplicated N times); with it, N versions = **1 app + 1
protocol + N small seams** — the costly UI is **O(1)**. And the boundary's upkeep
is *split with the agent port*, so the Android port does not pay for it alone.

Costs to budget honestly: (a) **slower v1** — you build app + protocol + seam
instead of one port, with payback on version 2+; (b) **one expected protocol
revision** — you usually cannot design the right protocol until you have seen the
second concrete version (rule of three), so plan to revise once after 5.1 (or the
3.6.x thought experiment) reveals what you over-fit to 5.0; (c) the discipline of
the guardrail above.

### The per-version seam (implementation, for later — not scaffolded yet)

Each engine's seam is a NetHack window port that *emits the protocol* — wired the
way `win/agent` plugs in: a new `win/<port>/` directory plus ~5 shared
touchpoints, core untouched:

- `include/winprocs.h` — interface (only if a new proc is needed).
- `src/windows.c` — register the port's `struct window_procs`.
- the platform makefiles — add the port's sources.
- the platform sys glue (the `win/agent` analogue touched `sys/windows/windsys.c`).
- build selection via the standard `WANT_WIN_*` mechanism, so the port coexists
  with the others.

On Android the chosen engine ships as either a **native library** (`.so` via JNI;
`sys/libnh` is the starting point) or a **bundled native executable** the app
drives over a local socket (closer to the agent server, and the cleanest fit for
the serialized protocol). The "5.1 releases and just slots in with no app update"
ideal is *architecturally* real (ship the new engine as a separate artifact), but
mind the platform constraint: Google Play policy and Android 14+ restrict loading
executable / native code downloaded at runtime, so in practice engines ship
bundled in the app or via normal updates — the decoupling still means rebuilding
only the small engine module, not the UI.

---

## UI stack & aesthetic (decided)

### Stack: Godot 4.5+, 2D, GDScript

The client is a Godot 4.5+ 2D project written in GDScript. Godot wins on the
things a modern controller-first game needs for free — native **gamepad focus
navigation**, animation, shaders, 2D tile batching, audio, and a solid Android
export — and it is MIT-licensed, so it sits cleanly beside the NGPL engine as a
separate protocol client.

Godot was *not* the obvious pick for an agent-in-terminal workflow (its culture
is editor-centric), but the **MCP tooling closes that gap**: an agent drives the
editor and runtime through tool calls instead of a GUI. Chosen over the pure-code
alternatives (libGDX, Flutter) because that tooling now exists and the built-in
game-feel is worth more than their slightly tighter raw loop.

### Dev workflow: agent + Godot MCP, on Linux

Primary loop is Claude Code ↔ **`satelliteoflove/godot-mcp`** ↔ Godot. That
server is chosen for its **deterministic, controller-aware verify loop** — joypad
button/stick/analog **input injection**, frozen-clock frame-stepping, live game
state as JSON, and screenshots — ideal for proving controller UX reproducibly.
`GoPeak` (GDScript LSP + DAP debugger) is the alternative if the agent wants
code-intelligence / step-debugging. Fallback if the MCP misbehaves: plain
`godot --headless --script`, which works on its own.

Dev host is **Linux**, because the downstream Android pipeline (NDK cross-compile
of the engine, Godot Android export), headless agent loops, and `xvfb` screenshot
capture are all first-class there.

### Aesthetic: hybrid map + designed chrome, glyph↔tile toggle

The map renders as a modern pixel **tileset** by default, with a player toggle to
gorgeous **ASCII glyphs** — mirroring the Assist toggle, and cheap because the
protocol forwards glyph IDs (the renderer just maps glyph→sprite or glyph→char).
Menus / HUD / wheels are **typographic designed chrome**, never OS widgets —
building the UI out of system widgets is exactly the "dated Android app" failure
mode this port exists to avoid. Identity comes from NetHack's CGA-ish palette and
its object-class glyphs (`!`, `?`, `[`, `&`) reused as UI motifs.

References that set the bar: **Cogmind** (ASCII, stunning UI), **Caves of Qud** /
**Shattered Pixel Dungeon** (tiles + polish), **Jupiter Hell** (modern
controller-first roguelike presentation).

### Modern-feel ingredients (stack-agnostic)

Animated, glowing focus (never a system cursor) · radial menus that animate open ·
**haptics** (controller rumble on select) · smooth sub-tile camera pans and menu
transitions · cohesive audio + rumble · one type family + icon set + the NetHack
palette · low input latency.

### Repo layout

Two private repos, connected only by the protocol (not the filesystem):

- **`koalabuttz/NetHack`** (public fork) — NetHack 5.0 core + the `win/android`
  seam, on the `android-window-port` branch; the engine that emits the protocol.
  A fork rather than a private mirror, so the full history lives server-side with
  no bulk upload — only the port's commits are ours.
- **`yendor-client`** (private) — the Godot 4.5 project; the controller-first
  protocol client. Kept separate so its `.godot/`, import cache, and assets never
  clutter the C source tree.

---

## Open questions (to iron out before scaffolding)

**Protocol / engine boundary**

- **Protocol lineage:** `win/agent`'s `_live/` protocol is prior art but is **not
  in this fork** (it's on the Windows-local, unpushed `agent-window-port` branch).
  Decide: design the android protocol *fresh* for the UI (deriving from NetHack's
  window interface, converging with the agent later), or import/generalize
  win/agent's schema into a shared spec — in which case that reference must be
  made available here first. Recommended default: design fresh, converge later.
- **Handshake schema:** what exactly the engine declares (version, capability
  flags, forwarded tables) and how the app negotiates against it.
- **Transport on Android:** native library (`.so` via JNI) vs. bundled
  executable over a local socket — and how the engine is delivered (bundled vs.
  the policy-constrained downloadable artifact).
- **Capability-gated features:** for an engine lacking a feature (e.g. no
  autoexplore), does the seam *synthesize* it (travel-based exploration), or does
  the app simply not offer it? Default per feature.

**UI / interaction**

- **Tile assets:** reuse an existing NetHack tileset (classic 32×32, DawnLike,
  etc.) or commission new pixel art? (Substrate itself is decided — Godot 2D with
  a glyph↔tile toggle; glyph→tile mapping is forwarded data either way.)
- **Wheel contents:** exact 8 spokes on each wheel; what's promoted vs. buried in
  "More…". Wheels fixed or user-editable? (Populated from the forwarded command
  table, not hardcoded.)
- **Default Assist state** on first launch, and where the toggle lives.
- **Map panning / camera** on a small screen: follow-@ vs. free-scroll; how the
  look cursor interacts with a viewport smaller than the level.
- **Save/quit lifecycle** under Android (backgrounding, process death) — when to
  auto-save.
- **Layout for fewer buttons** (no L3/R3, or no second stick) — graceful
  degradation path.
- **Hold vs. toggle** for the R1/L1 modifier layers (accessibility option).
