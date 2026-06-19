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

## Implementation seam (for later — not scaffolded yet)

This is a window port; wiring mirrors how `win/agent` plugs in — a new
`win/android/` directory plus ~5 shared touchpoints:

- `include/winprocs.h` — interface (only if a new proc is needed).
- `src/windows.c` — register the port's `struct window_procs`.
- the platform makefiles — add the port's sources.
- the platform sys glue (the `win/agent` analogue touched `sys/windows/windsys.c`).

The core (`src/`, `include/`) stays untouched. Build selection is the standard
`WANT_WIN_*` mechanism, so this port coexists with the others.

---

## Open questions (to iron out before scaffolding)

- **Rendering substrate:** ASCII/tileset glyph grid vs. a richer tiled renderer?
  Reuse an existing port's tile assets, or new ones?
- **Wheel contents:** exact 8 spokes on each wheel; what's promoted vs. buried in
  "More…". Are wheels fixed or user-editable?
- **Default Assist state** on first launch, and where the toggle lives.
- **Autoexplore:** does 5.0 core expose `#autoexplore`, or do we implement
  travel-based exploration in the port?
- **Map panning / camera** on a small screen: follow-@ vs. free-scroll; how the
  look cursor interacts with a viewport smaller than the level.
- **Save/quit lifecycle** under Android (backgrounding, process death) — when to
  auto-save.
- **Layout for fewer buttons** (no L3/R3, or no second stick) — graceful
  degradation path.
- **Hold vs. toggle** for the R1/L1 modifier layers (accessibility option).
