"""ScriptedReflex: the always-available scripted policy.

The play agent's inline policy was not recoverable from the tree, so the
campaign's working heuristics are reconstructed here explicitly (see
``doc/agent-autoplay-plan.md`` section "ScriptedReflex"):

  * startup: pick the configured role / race / alignment and explicitly
    decline the tutorial from a *recognised* menu; an unrecognised menu is
    cancelled rather than blindly confirmed (a bare "first selectable row"
    could accept a tutorial or a destructive choice);
  * safety: explicit low-HP disengagement, and a monster is never walked into
    -- public appearance is ambiguous, so getting unblocked means searching,
    routing around or changing plan, never attacking a possible pet;
  * navigation: Dijkstra over remembered, publicly observed terrain with a
    visit-count penalty, preferring known down-stairs, then reachable
    frontiers, with bounded searching to break loops; **unknown cells are not
    walkable in the fallback** -- exploration happens only through deliberate
    observed edges (frontier cells border known floor);
  * hunger: a known-safe food allowlist (corpses and unrecognised items are
    not assumed safe), with a mandatory loop breaker after two equivalent
    rejected food intents;
  * rest: only when provably safe -- no adjacent monster, adequate HP and not
    hungry;
  * inventory: refreshed from recognised inventory content and by a bounded
    periodic ``i`` when the cache goes stale;
  * every need has a total fallback path so no request is ever left
    unanswered.

Confidence is a documented heuristic uncertainty score, not calibrated
probability.  Structural validity is separate from safety confidence.
"""

import heapq
import random
import time
from typing import Dict, List, Optional, Tuple

from . import protocol, state
from .providers import ReflexContext, ReflexResult, ReflexTimeout

KEY = protocol

# Safety thresholds (heuristic policy constants, not calibrated values).
LOW_HP_FRACTION = 0.30          # disengage at or below this HP fraction
ADEQUATE_HP_FRACTION = 0.50     # rest is only considered above this
INV_STALE_TICKS = 240           # refresh the inventory cache after this many
INV_REFRESH_COOLDOWN = 40       # ... but never more often than this
EAT_RETRY_INTERVAL = 25

# Recognised startup menu kinds.  Only these are answered with a selection;
# anything else is cancelled.
_MENU_KEYWORDS = (("tutorial", "tutorial"),
                  ("role", "role"),
                  ("profession", "role"),
                  ("race", "race"),
                  ("alignment", "alignment"),
                  ("creed", "alignment"),
                  ("start game", "ok"),
                  ("ok", "ok"))


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


def menu_kind(title: str) -> str:
    """Classify a menu by its displayed title; "" means unrecognised."""
    low = (title or "").lower()
    for needle, kind in _MENU_KEYWORDS:
        if needle in low:
            return kind
    return ""


class ScriptedReflex(object):
    """Deterministic, always-available scripted decision policy."""

    def __init__(self, config) -> None:
        self.config = config
        self.intent = ""            # "", "eat", "quit"
        self.selection_done = False
        self.eat_reject_base = 0
        self.eat_forced_menu = False
        self.last_eat_tick = -1000
        self.last_inv_tick = -1000
        self.quitting = False
        self.quit_reason = ""
        self.last_hero: Optional[Tuple[int, int]] = None
        self.stuck = 0
        self.rng = random.Random(0)
        self.max_ticks = 2000
        # Absolute monotonic deadline for the decision in flight (0 = none).
        # The controller sets it via ReflexContext.deadline; it is checked at
        # every loop boundary so a single scripted decision cannot overrun its
        # allowance.
        self.deadline = 0.0

    # -- provider surface ------------------------------------------------
    def _check_deadline(self) -> None:
        if self.deadline and time.monotonic() >= self.deadline:
            raise ReflexTimeout("scripted reflex exceeded its deadline")

    def decide(self, context: ReflexContext) -> ReflexResult:
        self.deadline = float(getattr(context, "deadline", 0.0) or 0.0)
        self._check_deadline()
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
        title = context.snapshot.window_title(need.get("content")) or ""
        self._maybe_refresh_inventory(context, context.pages, title)

        if self.intent == "eat":
            self.intent = ""
            self.eat_forced_menu = False
            food = [r for r in rows
                    if state.is_known_safe_food(r.get("text"))]
            if food:
                return {"menu": need.get("menu"),
                        "commit": [[food[0]["r"], -1]]}, "eat a safe food row"
            return {"cancel": True}, "no known-safe food row: cancel"

        kind = menu_kind(title)
        if not kind:
            # an unmatched menu is never blindly confirmed
            return {"cancel": True}, "unrecognised menu title: cancel"
        if kind in ("tutorial", "ok"):
            self.selection_done = True
        pick = self._preferred_row(rows, kind)
        if pick is None:
            return {"cancel": True}, "no matching row in a %s menu: cancel" \
                % kind
        return {"menu": need.get("menu"),
                "commit": [[pick["r"], -1]]}, "select the %s row" % kind

    def _preferred_row(self, rows: List[dict], kind: str) -> Optional[dict]:
        wants: List[str] = []
        if kind == "tutorial":
            wants = ["no, just start play", "no"]
        elif kind == "ok":
            wants = ["yes; start game", "yes"]
        elif kind == "role":
            wants = [self.config.role.lower()]
        elif kind == "race":
            wants = ["human"]
        elif kind == "alignment":
            wants = ["lawful"]
        for want in wants:
            for r in rows:
                if want and want in (r.get("text") or "").lower():
                    return r
        return None

    # -- inventory cache maintenance ------------------------------------
    def _maybe_refresh_inventory(self, context: ReflexContext, rows, title):
        if not title or "inventory" not in title.lower():
            return
        context.memory.inventory.refresh(
            list(rows), context.tick, context.snapshot.time_value())

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
        # only answer a letter the cached inventory already confirmed is safe
        known = {l.lower() for l in context.memory.inventory.food_letters()}
        safe = [c for c in letters if c.lower() in known]
        if safe:
            return {"yn": ord(safe[0])}, "eat a known-safe cached food letter"
        if has_star:
            return {"yn": ord("*")}, "open the food menu"
        return {"yn": KEY.KEY_ESC}, "no known-safe food answer: cancel eat"

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

        # 1. low-HP disengagement: escape before taking any other action
        if hero is not None and self._low_hp(st):
            return self._escape(mem, hero)

        # 2. hunger: schedule a known-safe food intent
        if self._hungry(st) and \
                (context.tick - self.last_eat_tick) > EAT_RETRY_INTERVAL:
            self.last_eat_tick = context.tick
            self.intent = "eat"
            self.eat_reject_base = sum(1 for m in mem.messages
                                       if "don't have that object" in m)
            self.eat_forced_menu = False
            return {"key": KEY.KEY_EAT}, "hungry: attempt to eat"

        if hero is None:
            # The hero's square is unknown, so every direction leads into
            # unknown space and adjacency cannot be evaluated: never move
            # blind, hold the turn with a search instead.
            return {"key": KEY.KEY_SEARCH}, "no hero fix: search in place"

        # 3. loop breakers: progress without ever walking into a monster
        np = mem.no_progress
        if np >= 10:
            return self._unblock(mem, hero, st), "loop breaker: unblock"
        if np >= 6:
            key, why = self._random_move(mem, hero)
            return {"key": key}, "loop breaker: %s" % why
        if np >= 3:
            return {"key": KEY.KEY_SEARCH}, "loop breaker: search"

        # 4. inventory cache maintenance (never preempts safety or progress)
        if mem.inventory.stale(context.tick, INV_STALE_TICKS):
            if (context.tick - self.last_inv_tick) > INV_REFRESH_COOLDOWN:
                self.last_inv_tick = context.tick
                return {"key": KEY.KEY_INV}, "refresh the inventory cache"

        step = self._move_key(context, hero)
        return {"key": step[0]}, step[1]

    def _hungry(self, st: state.Status) -> bool:
        return st.hunger.startswith(("Hungry", "Weak", "Fainting"))

    def _low_hp(self, st: state.Status) -> bool:
        if st.hp is None or not st.hp_max:
            return False
        return st.hp / float(st.hp_max) <= LOW_HP_FRACTION

    def _adequate_hp(self, st: state.Status) -> bool:
        if st.hp is None or not st.hp_max:
            return True
        return st.hp / float(st.hp_max) > ADEQUATE_HP_FRACTION

    def _adjacent_monsters(self, mem, hero):
        out = []
        for d in KEY.DIR_KEYS:
            dest = (hero[0] + d[0], hero[1] + d[1])
            if state.monster_cell(mem.tile(dest), hero, dest):
                out.append(d)
        return out

    # -- safety: escape ------------------------------------------------
    def _escape(self, mem, hero):
        """Disengage at low HP: never toward a monster."""
        threats = self._adjacent_monsters(mem, hero)
        for dx, dy in threats:
            away = (-dx, -dy)
            if mem.known_passable((hero[0] + away[0], hero[1] + away[1])):
                return {"key": KEY.DIR_KEYS[away]}, "low HP: retreat"
        for d, k in KEY.DIR_KEYS.items():
            dest = (hero[0] + d[0], hero[1] + d[1])
            if d not in threats and mem.known_passable(dest):
                return {"key": k}, "low HP: sidestep"
        if hero in mem.stairs_up:
            return {"key": ord("<")}, "low HP: withdraw upstairs"
        target = self._nearest(mem.stairs_up, hero)
        if target is not None:
            step = self._first_step(mem, hero, target)
            if step is not None:
                return {"key": KEY.DIR_KEYS[step]}, "low HP: flee upstairs"
        if self._safe_to_rest(mem, mem.status, hero):
            return {"key": KEY.KEY_WAIT}, "low HP: hold position"
        return {"key": KEY.KEY_SEARCH}, "low HP: search for an exit"

    def _safe_to_rest(self, mem, st, hero) -> bool:
        if self._hungry(st):
            return False
        if not self._adequate_hp(st):
            return False
        return not self._adjacent_monsters(mem, hero)

    def _unblock(self, mem, hero, st):
        """Break a stuck state without walking into an ambiguous monster."""
        if self._safe_to_rest(mem, st, hero):
            return {"key": KEY.KEY_WAIT}
        if self._adjacent_monsters(mem, hero):
            target = self._frontier_target(mem, hero)
            if target is not None:
                step = self._first_step(mem, hero, target)
                if step is not None:
                    dest = (hero[0] + step[0], hero[1] + step[1])
                    if not state.monster_cell(mem.tile(dest), hero, dest):
                        return {"key": KEY.DIR_KEYS[step]}
            return {"key": KEY.KEY_SEARCH}
        return {"key": KEY.KEY_SEARCH}

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

    def _frontier_target(self, mem, hero):
        best = None
        for pos in mem.grid:
            if pos == hero or not state.passable(mem.tile(pos)):
                continue
            if self._is_frontier(mem, pos):
                if best is None or _manhattan(pos, hero) < \
                        _manhattan(best, hero):
                    best = pos
        return best

    def _nearest(self, cells, hero):
        cells = list(cells)
        if not cells:
            return None
        return min(cells, key=lambda p: _manhattan(p, hero))

    def _random_move(self, mem, hero):
        """A fallback move over *known* floor only.

        Unknown blanks are not freely traversable: a step into an unpainted
        cell is only ever taken by a deliberate navigation path that ends on a
        remembered frontier cell, never by this fallback.  When no known floor
        is available this never simply waits -- a wait is only safe when the
        hero is not hungry, not at low HP and has no adjacent monster -- so an
        unsafe hold becomes a search instead.
        """
        dirs = list(KEY.DIR_KEYS)
        self.rng.shuffle(dirs)
        for d in dirs:
            dest = (hero[0] + d[0], hero[1] + d[1])
            if mem.known_passable(dest):
                return KEY.DIR_KEYS[d], "random walk (known floor)"
        if hero is not None and self._safe_to_rest(mem, mem.status, hero):
            return KEY.KEY_WAIT, "no known floor: wait"
        return KEY.KEY_SEARCH, "no known floor and unsafe to rest: search"

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
            self._check_deadline()
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
