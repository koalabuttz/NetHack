"""Streaming exploration metrics over campaign artifacts (section 10.2).

Reads the per-episode recordings a campaign already writes and derives the
plan's measurement set **without** loading a whole episode into memory: the
wire is streamed line by line and only counters and bounded fingerprints are
retained.

Two things this module is careful about, because the plan calls them out:

* a stair encounter is counted only when a **map triple** references a ``>``
  palette entry -- a palette entry alone is not an encounter;
* controller ticks are kept strictly separate from game turns measured by
  displayed status ``time`` deltas.

Coverage is reported *instance-scoped*: a fresh level-instance allocation may
rediscover the same cells, so the cell totals are not true distinct dungeon
area.
"""

import json
import os
import re
from typing import Dict, Iterable, List, Optional

from . import protocol

_DLVL = re.compile(r"(\d+)\s*$")


def parse_dlvl(text: str) -> Optional[int]:
    """The numeric depth from a displayed ``Dlvl:`` status value."""
    if not text:
        return None
    m = _DLVL.search(text.strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def hero_of(snap: protocol.Snapshot) -> Optional[tuple]:
    """The unique hero square, or ``None`` when there is not exactly one."""
    found = None
    for pos, cell in snap.map.items():
        if cell and cell[0] == "@":
            if found is not None:
                return None
            found = pos
    return found


class EpisodeMetrics(object):
    """Streaming accumulators for one episode's wire recording."""

    def __init__(self) -> None:
        self.observations = 0
        self.cells = set()
        self.entered = set()
        self.stairs_cells = set()
        self.depth_max: Optional[int] = None
        self.depth_final: Optional[int] = None
        self.time_first: Optional[int] = None
        self.time_last: Optional[int] = None
        self.time_advances = 0
        self.loop_span = 0
        self.loop_spans: List[int] = []
        self._last_fp = None
        self._run = 0
        # AC9: attempts vs displayed-time advances, and the maximum stationary
        # span (a run of identical hero squares).  ``attempts`` is the sent
        # *gameplay* attempt count: it is filled from the actions sidecar when
        # one is supplied (so a zero-time command still counts, review item 6a)
        # and otherwise falls back to the hero-displacement count, which is
        # reported separately and flagged via ``attempts_source``.
        self.attempts = 0
        self.attempts_source = "hero-displacement"
        self.hero_displacements = 0
        self.stationary_span = 0
        self.teardown_frames = 0
        self._prev_hero = None
        self._stat_run = 0

    def fold(self, snap: protocol.Snapshot) -> None:
        self.observations += 1
        for pos, cell in snap.map.items():
            if cell and cell[0] != " ":
                self.cells.add(pos)
            if cell and cell[0] == ">":
                self.stairs_cells.add(pos)
        hero = hero_of(snap)
        if hero is not None:
            if self._prev_hero is not None and hero != self._prev_hero:
                # a *displacement* is not the attempt count (a zero-time command
                # is an attempt with no displacement); tracked separately and
                # resolved to ``attempts`` by the caller
                self.hero_displacements += 1
            if hero == self._prev_hero:
                self._stat_run += 1
                if self._stat_run > self.stationary_span:
                    self.stationary_span = self._stat_run
            else:
                self._stat_run = 0
            self._prev_hero = hero
            self.entered.add(hero)
        text = snap.status_text()
        depth = parse_dlvl(text.get("dungeon-level") or "")
        if depth is not None:
            self.depth_final = depth
            if self.depth_max is None or depth > self.depth_max:
                self.depth_max = depth
        t = snap.time_value()
        if t is not None:
            if self.time_first is None:
                self.time_first = t
            if self.time_last is not None and t > self.time_last:
                self.time_advances += 1
            self.time_last = t
        # a loop span is a run of identical (hero, displayed time) frames
        fp = (hero, t)
        if fp == self._last_fp and hero is not None:
            self._run += 1
            if self._run > self.loop_span:
                self.loop_span = self._run
        else:
            if self._run:
                self.loop_spans.append(self._run)
            self._run = 0
            self._last_fp = fp

    def finish(self) -> dict:
        if self._run:
            self.loop_spans.append(self._run)
        turns = None
        if self.time_first is not None and self.time_last is not None:
            turns = self.time_last - self.time_first
        return {
            "observations": self.observations,
            "discovered_cells_instance_scoped": len(self.cells),
            "entered_cells_instance_scoped": len(self.entered),
            "stairs_from_map_triples": len(self.stairs_cells),
            "depth_max": self.depth_max,
            "depth_final": self.depth_final,
            "displayed_turns": turns,
            "time_advances": self.time_advances,
            "attempts": self.attempts,
            "attempts_source": self.attempts_source,
            "hero_displacements": self.hero_displacements,
            "stationary_span_max": self.stationary_span,
            "longest_loop_span": self.loop_span,
            "loop_spans_ge_2": sum(1 for s in self.loop_spans if s >= 2),
            "teardown_frames_excluded": self.teardown_frames,
        }


#: Need kinds whose answer is a gameplay *attempt* (command/key/direction).
_GAMEPLAY_KINDS = ("command", "key", "direction")


def attempts_from_actions(actions_path: str) -> Optional[int]:
    """Count the sent *gameplay* acts in an ``ep-N.actions.jsonl`` sidecar.

    A zero-time command is still a sent attempt, so the recorded acts -- not
    hero displacement -- are the correct attempt denominator (review item 6a).
    Returns ``None`` when the path is absent/unreadable, so the caller can fall
    back and flag the source rather than report a fabricated zero.
    """
    if not actions_path or not os.path.exists(actions_path):
        return None
    n = 0
    with open(actions_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("kind") in _GAMEPLAY_KINDS:
                n += 1
    return n


def episode_metrics(wire_path: str, meta: Optional[dict] = None,
                    actions_path: Optional[str] = None) -> dict:
    """Stream one ``.wire.jsonl`` recording into a metrics dict.

    Coverage excludes *teardown*: once the episode's ``closed`` record is seen,
    any later observation is a post-close teardown frame (a goodbye/quit
    confirmation) and is counted but never folded into cells/coverage, so a
    teardown frame can neither inflate discovered area nor the stationary span.
    """
    em = EpisodeMetrics()
    snap = protocol.Snapshot()
    closed = False
    with open(wire_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if '"obs"' not in line:
                if '"closed"' in line:
                    closed = True
                continue
            if closed:
                em.teardown_frames += 1
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "obs":
                continue
            try:
                snap.apply(rec)
            except protocol.ProtocolError:
                continue
            em.fold(snap)
    # Resolve the attempt count (review item 6a): the sent-gameplay acts are the
    # true denominator (a zero-time command still counts); the hero-displacement
    # count is the flagged fallback, never a silent substitute.
    from_actions = attempts_from_actions(actions_path) if actions_path else None
    if from_actions is not None:
        em.attempts = from_actions
        em.attempts_source = "actions"
    else:
        em.attempts = em.hero_displacements
        em.attempts_source = "hero-displacement"
    out = em.finish()
    if meta:
        out.update({
            "ticks": meta.get("ticks"),
            "needs": meta.get("needs"),
            "actions": meta.get("actions"),
            "invalids": meta.get("invalids"),
            "boundaries": meta.get("boundaries"),
            "game_outcome": meta.get("game_outcome"),
            "stop_reason": meta.get("stop_reason"),
            "recording_complete": meta.get("recording_complete"),
        })
    return out


def campaign_metrics(campaign_dir: str) -> dict:
    """Metrics for every ``ep-N.wire.jsonl`` in a campaign directory."""
    episodes = []
    names = sorted(n for n in os.listdir(campaign_dir)
                   if n.endswith(".wire.jsonl"))
    for name in names:
        idx = name.split(".")[0].split("-")[-1]
        meta_path = os.path.join(campaign_dir, "ep-%s.meta.json" % idx)
        meta = None
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        episodes.append({"episode": idx,
                         **episode_metrics(os.path.join(campaign_dir, name),
                                           meta)})
    return {"campaign_dir": campaign_dir, "episodes": episodes,
            "episode_count": len(episodes)}


# -- AC9 confidence/consultation report ------------------------------------
#
# The closed consultation-outcome vocabulary, keyed by the decision reason the
# provider tier wrote.  A *skipped singleton* and a *cap-unavailable* fallback
# are deliberately distinct from a genuine *rejected* answer, so a report can
# never conflate a deliberate singleton bypass or a spent cap with a real
# confidence rejection (plan AC9).
CONSULTATION_OUTCOMES = ("accepted", "rejected", "skipped-singleton",
                         "skipped-unsupported", "cap-unavailable",
                         "unavailable", "timeout", "scripted", "other")

_OUTCOME_RULES = (
    ("skipped-singleton", "jev skipped: singleton"),
    ("skipped-unsupported", "jev skipped: "),
    ("cap-unavailable", "jev paid-reflex cap reached"),
    ("cap-unavailable", "jev paid-reflex unavailable"),
    ("unavailable", "jev unavailable:"),
    ("timeout", "deadline exceeded"),
    ("timeout", "jev fallback:"),
    ("rejected", "jev rejected:"),
    ("accepted", "jev choice"),
)


def classify_consultation(provider: str, reason: str) -> str:
    """Map one decision's provider/reason to a consultation outcome (AC9)."""
    text = reason or ""
    for code, needle in _OUTCOME_RULES:
        if needle in text:
            return code
    if (provider or "") == "scripted":
        return "scripted"
    return "other"


def confidence_report(records, n_by_key=None) -> dict:
    """Stratify consultations by phase, option count (N) and outcome (AC9).

    *records* is an iterable of decision records (``{"record": "need", ...}``);
    *n_by_key* optionally maps a need key to the offered option count, so the
    report can separate a binary/ternary table from a larger one.  The result is
    a flat ``{"<phase>|n=<N>|<outcome>": count}`` histogram.
    """
    n_by_key = n_by_key or {}
    strata: Dict[str, int] = {}
    for r in records or ():
        if not isinstance(r, dict) or r.get("record") != "need":
            continue
        need = r.get("need") or {}
        phase = need.get("kind", "unknown")
        key = (need.get("seq"), need.get("id"))
        n = n_by_key.get(key)
        outcome = classify_consultation(r.get("provider", ""),
                                        r.get("reason", ""))
        label = "%s|n=%s|%s" % (phase, "?" if n is None else n, outcome)
        strata[label] = strata.get(label, 0) + 1
    return strata


def _main(argv: Iterable[str] = ()) -> int:
    import sys
    argv = list(argv) or sys.argv[1:]
    if not argv:
        print("usage: python3 -m tools.agent.exploration_metrics DIR")
        return 2
    print(json.dumps(campaign_metrics(argv[0]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
