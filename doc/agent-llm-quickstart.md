# Driving the NetHack agent interface from an LLM

How to wire an LLM (or any external policy) to the headless agent mod. The
wire protocol itself is specified in `doc/agent-interface.md`; this is the
practical recipe.

## The three pieces

```text
your LLM harness  <->  ./agent.sh serve  <->  nethack-agent  <->  NetHack worker
     (policy)            (JSON lines on stdin/stdout)
```

- `./agent.sh serve` exposes the wire: one episode per invocation, JSON lines
  on stdin/stdout. Call it again for a fresh episode.
- `./agent.sh watch` plays scripted episodes with live ASCII frames if you
  want to see the game while your harness learns the protocol.
- `python3 test/agent/spectate.py wrap -- ./agent.sh serve` shows live frames
  while YOUR harness drives (byte-transparent proxy).

## The loop

1. Read a line, parse JSON.
2. First record: `{"type":"hello",...}` — profile, policy, limits. Store it.
3. Every subsequent record is either an `obs` (what the player sees: `map`
   deltas, `s` status fields, `cond`, `msg`, `windows`, and a `need` object)
   or a transport record (`page`, `chunk`, `invalid`).
4. When `need` is present, the game is blocked on YOU. Compose the prompt for
   the model: render the map from the palette, the status line, the trailing
   messages, and the `need` (its `kind` tells you what kind of answer is
   legal). Send the model's choice as an `act`:

```json
{"v":1,"type":"act","seq":19,"id":20,"action":{"key":108}}
```

   `seq` must equal the observation's `seq`; `id` must equal `need.id`.
5. Go back to reading. One act normally produces one new `obs` with a fresh
   `need`. If your action was invalid you get an `invalid` record and the SAME
   request is still outstanding — fix and resubmit with the same `id`.
6. When you receive `{"v":1,"ch":"control","type":"closed"}` the episode is
   over. Start a new `./agent.sh serve` for the next one.

## Answer shapes by `need.kind`

| need.kind | action |
|---|---|
| `command` / `key` | `{"key":B}` — one byte, e.g. 104 = `h` (west), 106 = `j` (south), 46 = `.` (wait); native bindings, number pad off |
| `direction` | `{"key":B}` — a direction key (`h`/`j`/`k`/`l`/`y`/`u`/`b`/`n`) |
| `position` | `{"position":[x,y],"mod":0}` — x 1..79, y 0..20, or `{"key":B}` to aim with keys |
| `yn` | `{"yn":B}` — one of the *visible* choice bytes; `{"yn":B,"count":C}` for counted prompts; `{"cancel":true}` = Escape |
| `line` | `{"text":"..."}` or `{"cancel":true}` |
| `extcmd` | `{"text":"name"}` — the command name (e.g. `"annotate"`, `"quit"`), resolved by the native exact matcher; or `{"cancel":true}` |
| `menu` | `{"menu":"mN","commit":[[row,count],...]}` — explicit FINAL set of public row ids; counts are -1 (all/default) or positive; `commit:[]` accepts empty; `{"cancel":true}` cancels |
| `ack` | `{"ack":true}` — a blocking display you have read (fetch any pending `page`s first) |

Movement keys (native bindings, number_pad off): `h`/`j`/`k`/`l`/`y`/`u`/`b`/
`n` = directions, `.` = wait, `s` = search, `i` = inventory, `,` = pick up,
`S` = save, `#` = extended-command prompt. Sub-prompts open on their own —
the `need` always tells you what is being asked.

## Paging and chunks

Menus and long text arrive through `page` records; you must request missing
pages (`{"type":"get_page",...}` — see `doc/agent-interface.md` §8) and may
not commit a menu selection until every required page is delivered. Large
records may arrive split into `chunk` streams: assemble before use. The
reference client does all of this for you — reuse `test/agent/driver.py`'s
`Client`/`Runner` classes, or `test/agent/spectate.py` as a transparent
byte-exact proxy that renders frames for humans on a side channel.

## Minimal harness sketch

```python
import json, subprocess, tempfile, os

priv = tempfile.mkdtemp()
p = subprocess.Popen(
    ["./agent.sh", "serve"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE)

def send(obj):
    p.stdin.write((json.dumps(obj) + "\n").encode())
    p.stdin.flush()

need = None
while True:
    line = p.stdout.readline()
    if not line:
        break
    rec = json.loads(line)
    if rec.get("type") == "closed":
        break
    if rec.get("type") != "obs":
        continue
    need = rec.get("need")
    if not need:
        continue
    # ---- your LLM call goes here ----
    # prompt = render(rec)  (map + status + messages + need)
    # choice = llm(prompt)  -> one of the action shapes above
    send({"v": 1, "type": "act", "seq": rec["seq"],
          "id": need["id"], "action": {"key": ord("y")}})
os.rmdir(priv)  # only if empty; episodes also clean up after themselves
```

For serious use, keep per-episode state in a client model (the driver's
`Client` shows how to apply map deltas/palette), render the map as an ASCII
grid in the model prompt, and prefer `move`-style decisions to raw keys only
when you have verified the bindings (`llm-final-v1` fixes the bindings).

## Ground rules the protocol enforces

- You only ever see what a human player would see; there is nothing to cheat
  with and nothing hidden to ask for.
- One outstanding request at a time; answers are validated against the
  request you actually received (stale ids are rejected without consuming a
  turn).
- `invalid` records never cost a turn — the request stays open.
- The only "turn counter" is the displayed game time in the status fields.
- Episode outcomes arrive as a bare `{"type":"closed"}`; exit status and
  diagnostics are never exposed.
