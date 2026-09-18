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

    def fold(self, snap: protocol.Snapshot) -> None:
        self.observations += 1
        for pos, cell in snap.map.items():
            if cell and cell[0] != " ":
                self.cells.add(pos)
            if cell and cell[0] == ">":
                self.stairs_cells.add(pos)
        hero = hero_of(snap)
        if hero is not None:
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
            "longest_loop_span": self.loop_span,
            "loop_spans_ge_2": sum(1 for s in self.loop_spans if s >= 2),
        }


def episode_metrics(wire_path: str, meta: Optional[dict] = None) -> dict:
    """Stream one ``.wire.jsonl`` recording into a metrics dict."""
    em = EpisodeMetrics()
    snap = protocol.Snapshot()
    with open(wire_path, "r", encoding="utf-8") as fh:
        for line in fh:
            if '"obs"' not in line:
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
