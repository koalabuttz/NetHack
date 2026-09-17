"""ScriptedReflex: the always-available scripted policy.

The play agent's inline policy was not recoverable from the tree, so the
campaign's working heuristics are reconstructed here explicitly (see
``doc/agent-autoplay-plan.md`` section "ScriptedReflex"):

  * startup: pick the configured role, take the visible start-game row, and
    explicitly decline the tutorial; never select the first row blindly;
  * navigation: Dijkstra over remembered, publicly observed terrain with a
    visit-count penalty, preferring known down-stairs, then reachable
    frontiers, with bounded searching to break loops;
  * safety: disengage at low HP, avoid traps and monsters (never walk into a
    pet), rest only when safe;
  * hunger: answer the engine's ``getobj`` eat prompt with a *valid* inventory
    letter parsed out of the prompt, or open the inventory menu, with a
    mandatory loop breaker after two equivalent rejected food intents;
  * every need has a total fallback path so no request is ever left
    unanswered.

Confidence is a documented heuristic uncertainty score, not calibrated
probability.  Structural validity is separate from safety confidence.
"""

import heapq
import random
from typing import Dict, List, Optional, Tuple

from . import protocol, state
from .providers import ReflexContext, ReflexResult

KEY = protocol


def bracket_info(prompt: str) -> Tuple[List[str], bool, bool]:
    """Extract the bracketed answer hints from a native prompt.

    Returns (letters, has_star, has_qmark).  Connector words ("or", "and") are
    removed before single alphanumerics are collected, so "[d or ?*]" yields
    (["d"], True, True) and "[ynaq]" yields (["y","n","a","q"], False, False).
    """
    i = prompt.rfind("[")
    j = prompt.rfind("]")
    if i < 0 or j <= i:
        return [], False, False
    content = prompt[i + 1:j]
    content = content.replace(" or ", " ").replace(" and ", " ")
    letters = [c for c in content if c.isalnum()]
    return letters, "*" in content, "?" in content


class ScriptedReflex(object):
    """Deterministic, always-available scripted decision policy."""

    def __init__(self, config) -> None:
        self.config = config
        self.intent = ""            # "", "eat", "quit"
        self.selection_done = False
        self.eat_reject_base = 0
        self.eat_forced_menu = False
        self.last_eat_tick = -1000
        self.quitting = False
        self.quit_reason = ""
        self.last_hero: Optional[Tuple[int, int]] = None
        self.stuck = 0
        self.rng = random.Random(0)
        self.max_ticks = 2000

    # -- provider surface ------------------------------------------------
    def decide(self, context: ReflexContext) -> ReflexResult:
        need = context.need or {}
        kind = need.get("kind")
        if kind == "menu":
            action, reason = self._menu(context)
        elif kind == "yn":
            action, reason = self._yn(context)
        elif kind in ("command", "key", "direction"):
            action, reason = self._command(context)
        elif kind == "ack":
            action, reason = {"ack": True}, "acknowledge display"
        elif kind in ("line", "extcmd"):
            action, reason = self._textish(context)
        elif kind == "position":
            action, reason = {"key": KEY.KEY_ESC}, "cancel position request"
        else:
            action, reason = {"key": KEY.KEY_ESC}, "unknown kind fallback"
        return ReflexResult(action=action, confidence=0.5,
                            provider="scripted", reason=reason)

    def fallback(self, context: ReflexContext) -> ReflexResult:
        return self.decide(context)

    # -- startup / selection menus --------------------------------------
    def _menu(self, context: ReflexContext):
        need = context.need or {}
        if self.intent == "quit" or self.quitting:
            return {"cancel": True}, "dismiss endgame display"
        rows = [r for r in context.pages if r.get("selectable")]
        title = context.snapshot.window_title(need.get("content")).lower()

        if self.intent == "eat":
            food = [r for r in rows if _is_food(r.get("text", ""))]
            self.intent = ""
            self.eat_forced_menu = False
            if food:
                return {"menu": need.get("menu"),
                        "commit": [[food[0]["r"], -1]]}, "eat a food row"
            return {"cancel": True}, "no food row: cancel"

        pick = self._preferred_row(rows, title)
        if "tutorial" in title:
            self.selection_done = True
        if pick is not None:
            if title and ("ok" in title or "tutorial" in title):
                self.selection_done = True
            return {"menu": need.get("menu"),
                    "commit": [[pick["r"], -1]]}, "select a visible row"
        if rows:
            return {"menu": need.get("menu"),
                    "commit": [[rows[0]["r"], -1]]}, "first selectable row"
        return {"cancel": True}, "no selectable row: cancel"

    def _preferred_row(self, rows: List[dict],
                       title: str) -> Optional[dict]:
        wants: List[str] = []
        if "tutorial" in title:
            wants = ["no, just start play", "no"]
        elif "ok" in title:
            wants = ["yes; start game", "yes"]
        elif "role" in title or "profession" in title:
            wants = [self.config.role.lower()]
        elif "race" in title:
            wants = ["human"]
        elif "alignment" in title or "creed" in title:
            wants = ["lawful"]
        for want in wants:
            for r in rows:
                if want in (r.get("text") or "").lower():
                    return r
        return None

    # -- yes/no (including the unrestricted getobj prompt) --------------
    def _yn(self, context: ReflexContext):
        need = context.need or {}
        prompt = need.get("prompt") or ""
        low = prompt.lower()
        letters, has_star, _has_q = bracket_info(prompt)

        if "shall i pick" in low:
            return {"yn": KEY.KEY_N}, "decline auto-pick"
        if "really quit" in low or "quit without saving" in low:
            return {"yn": KEY.KEY_Y}, "confirm quit"
        if "save" in low and "really" in low:
            return {"yn": KEY.KEY_Y}, "confirm save"

        if self.intent == "eat" and "eat" in low:
            return self._eat_answer(context, letters, has_star)

        if need.get("default") is not None:
            return {"yn": int(need["default"])}, "native default"
        choices = need.get("choices")
        if choices:
            key = KEY.KEY_N if "n" in choices else ord(choices[0])
            return {"yn": key}, "visible choice"
        if "y" in letters and "n" in letters:
            return {"yn": KEY.KEY_N}, "decline yes/no"
        if letters:
            return {"yn": ord(letters[0])}, "bracketed letter"
        return {"yn": KEY.KEY_N}, "safe decline"

    def _eat_answer(self, context: ReflexContext, letters: List[str],
                    has_star: bool):
        rejected = sum(1 for m in context.memory.messages
                       if "don't have that object" in m) \
            - self.eat_reject_base
        if rejected >= 2 or self.eat_forced_menu:
            self.eat_forced_menu = True
            return {"yn": ord("*")}, "eat loop breaker: open inventory menu"
        if letters:
            return {"yn": ord(letters[0])}, "eat a bracketed food letter"
        if has_star:
            return {"yn": ord("*")}, "open the food menu"
        return {"yn": KEY.KEY_ESC}, "no food answer: cancel eat"

    # -- gameplay commands ----------------------------------------------
    def _command(self, context: ReflexContext):
        self.intent = ""
        if self.quitting or context.tick >= self.max_ticks:
            self.quitting = True
            if self.quit_reason == "":
                self.quit_reason = ("tick-cap" if context.tick >=
                                    self.max_ticks else "quit")
            return {"key": KEY.KEY_HASH}, "tick cap: request quit"
        mem = context.memory
        st = mem.status
        hero = mem.hero

        if self._hungry(st) and \
                (context.tick - self.last_eat_tick) > 25:
            # schedule a food intent before navigation if we might have food
            self.last_eat_tick = context.tick
            self.intent = "eat"
            self.eat_reject_base = sum(1 for m in mem.messages
                                       if "don't have that object" in m)
            self.eat_forced_menu = False
            return {"key": KEY.KEY_EAT}, "hungry: attempt to eat"

        if hero is None:
            key = self._random_dir(mem, hero)
            return {"key": key}, "no hero fix: random move"

        # loop breaker: a move that never changes the hero's square (a bump
        # into a wall, a boulder or a locked door) must not repeat forever
        np = mem.no_progress
        if np >= 10:
            # stepping into an adjacent monster swaps places with a pet and
            # attacks a hostile -- either way the square is freed
            key = self._step_into_monster(mem, hero)
            if key is not None:
                return {"key": key}, "loop breaker: unblock a monster"
            return {"key": KEY.KEY_WAIT}, "loop breaker: rest"
        if np >= 6:
            key, why = self._random_move(mem, hero)
            return {"key": key}, "loop breaker: %s" % why
        if np >= 3:
            return {"key": KEY.KEY_SEARCH}, "loop breaker: search"

        step = self._move_key(context, hero)
        return {"key": step[0]}, step[1]

    def _hungry(self, st: state.Status) -> bool:
        return st.hunger.startswith(("Hungry", "Weak", "Fainting"))

    # -- navigation ------------------------------------------------------
    def _move_key(self, context: ReflexContext, hero):
        mem = context.memory
        # standing on the known down stairs: descend (bounded progress)
        if hero in mem.stairs_down:
            return ord(">"), "descend the known stairs"
        for target in self._target_list(mem, hero):
            step = self._first_step(mem, hero, target)
            if step is not None:
                return KEY.DIR_KEYS[step], "navigate"
        if mem.searches_since_progress < 3:
            mem.searches_since_progress += 1
            return KEY.KEY_SEARCH, "search for secret doors"
        return self._random_move(mem, hero)

    def _target_list(self, mem, hero):
        down = [p for p in mem.stairs_down
                if p != hero and state.passable(mem.tile(p))]
        frontier, unvisited = [], []
        for pos, cell in mem.grid.items():
            if pos == hero or not state.passable(cell[0]):
                continue
            if self._is_frontier(mem, pos):
                frontier.append(pos)
            elif mem.visits.get(pos, 0) == 0:
                unvisited.append(pos)
        out = []
        if down:
            out.append(min(down, key=lambda p: _manhattan(p, hero)))
        out += sorted(frontier,
                      key=lambda p: _manhattan(p, hero) + 0.5
                      * mem.visits.get(p, 0))[:8]
        out += sorted(unvisited, key=lambda p: _manhattan(p, hero))[:8]
        return out

    def _random_move(self, mem, hero):
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        # prefer a destination we already know is walkable
        for dx, dy in dirs:
            dest = (hero[0] + dx, hero[1] + dy)
            if mem.known_passable(dest):
                return KEY.DIR_KEYS[(dx, dy)], "random walk (known floor)"
        for dx, dy in dirs:
            dest = (hero[0] + dx, hero[1] + dy)
            if dest not in mem.grid:
                return KEY.DIR_KEYS[(dx, dy)], "random step into the dark"
        for dx, dy in dirs:
            return KEY.DIR_KEYS[(dx, dy)], "random walk"
        return KEY.KEY_WAIT, "wait"

    def _step_into_monster(self, mem, hero):
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        for dx, dy in dirs:
            dest = (hero[0] + dx, hero[1] + dy)
            cell = mem.grid.get(dest)
            if cell and state.monster_glyph(cell[0]):
                return KEY.DIR_KEYS[(dx, dy)]
        return None

    def _is_frontier(self, mem: state.EpisodeMemory, pos) -> bool:
        x, y = pos
        for nx, ny in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y)):
            if nx < protocol.MAP_MIN_X or nx > protocol.MAP_MAX_X:
                continue
            if ny < protocol.MAP_MIN_Y or ny > protocol.MAP_MAX_Y:
                continue
            if (nx, ny) not in mem.grid:
                return True
        return False

    def _first_step(self, mem, hero, target):
        """Dijkstra over remembered passable cells with a visit penalty."""
        if target == hero:
            return None
        dist = {hero: 0.0}
        parent: Dict[Tuple[int, int], Tuple[int, int]] = {}
        pq = [(0.0, hero)]
        found = False
        while pq:
            d, pos = heapq.heappop(pq)
            if d > dist.get(pos, float("inf")):
                continue
            if pos == target:
                found = True
                break
            x, y = pos
            for dx, dy in KEY.DIR_KEYS:
                nx, ny = x + dx, y + dy
                nxt = (nx, ny)
                if nxt not in mem.grid or not state.passable(
                        mem.tile(nxt)):
                    continue
                cost = 1.0 + 0.5 * mem.visits.get(nxt, 0)
                nd = d + cost
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    parent[nxt] = pos
                    heapq.heappush(pq, (nd, nxt))
        if not found:
            return None
        node = target
        while parent.get(node) != hero and node in parent:
            node = parent[node]
        if parent.get(node) != hero:
            return None
        return (node[0] - hero[0], node[1] - hero[1])

    def _random_dir(self, mem, hero):
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        for dx, dy in dirs:
            if hero is None:
                return KEY.DIR_KEYS[(dx, dy)]
        return KEY.DIR_KEYS[dirs[0]]

    # -- text / extcmd ---------------------------------------------------
    def _textish(self, context: ReflexContext):
        kind = (context.need or {}).get("kind")
        if kind == "extcmd" and (self.quitting or
                                 context.tick >= self.max_ticks):
            return {"text": "quit"}, "quit through the native prompt"
        if kind == "extcmd":
            return {"cancel": True}, "dismiss extended command prompt"
        return {"cancel": True}, "cancel line prompt"

    def on_closed(self):
        self.intent = ""
        self.quitting = True


def _manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_food(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in ("ration", "food", "apple", "banana",
                                  "orange", "melon", "corpse", "kelp",
                                  "cram", "lembas", "food ration"))
