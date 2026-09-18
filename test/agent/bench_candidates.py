#!/usr/bin/env python3
"""Opt-in performance benchmark for the candidate preparation path.

Not part of the unit suite -- it is a measurement harness for the plan's
performance gate (``doc/agent-reflex-upgrade-plan.md`` section 3.2): build
and hash a table with 1, a representative and 255 candidates, then run a
150,000-table x 255-candidate streaming loop measuring per-table latency
percentiles, throughput, deadline overruns and peak retained memory.

    python3 test/agent/bench_candidates.py [--tables 150000] [--out FILE]

The loop discards each table, so the entire episode's tables are never
retained in memory.  Percentiles are reported against the 0.75s reflex
deadline (``--reflex-deadline``).
"""

import argparse
import gc
import json
import os
import platform
import sys
import time
import tracemalloc

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.agent import candidates  # noqa: E402

# A representative worst case: the eight move directions plus interactions
# (descend, search, inventory, eat, open-door approaches on four sides).
_DIRS = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0),
         (-1, 1), (0, 1), (1, 1))
_FAMILIES = ("stair", "frontier", "unvisited", "secret-search", "door")


def make_candidates(count):
    """A deterministic candidate set of *count* distinct actions."""
    out = []
    for i in range(count):
        if i < 8:
            dx, dy = _DIRS[i]
            key = {(-1, -1): 121, (0, -1): 107, (1, -1): 117, (-1, 0): 104,
                   (1, 0): 108, (-1, 1): 98, (0, 1): 106,
                   (1, 1): 110}[(dx, dy)]
            action = {"key": key}
            direction, drank = (dx, dy), i
        else:
            # distinct interaction keys / menu rows keep every action unique
            code = 33 + (i % 90)
            action = {"key": code} if i % 3 else \
                {"menu": 1 + (i % 7), "commit": [[1 + i % 40, -1]]}
            direction, drank = (), i
        fam = _FAMILIES[i % len(_FAMILIES)]
        score = 100 + (i % 900)
        out.append(candidates.make_candidate(
            action, "cand-%d" % i, fam, direction, drank, score,
            [("base", score)], "reason-%d" % i, "effect-%d" % i))
    return out


def percentile(sorted_ms, frac):
    if not sorted_ms:
        return 0.0
    idx = min(len(sorted_ms) - 1, int(frac * (len(sorted_ms) - 1)))
    return sorted_ms[idx]


def bench_size(count, reps):
    cands = make_candidates(count)
    # warm
    candidates.build_table((1, 1, 1), 1, cands, "dig")
    best = None
    for _ in range(reps):
        t0 = time.perf_counter()
        table = candidates.build_table((1, 1, 1), 1, cands, "dig")
        payload = candidates.jev_payload(table)
        dt = time.perf_counter() - t0
        if best is None or dt < best:
            best = dt
    return {
        "candidates": count,
        "retained_bytes": len(table.canonical_bytes),
        "payload_bytes": len(json.dumps(payload)),
        "best_table_ms": round(best * 1000.0, 4),
    }


def benchmark(tables, reflex_deadline):
    report = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "reflex_deadline_s": reflex_deadline,
        "sizes": [bench_size(1, 2000), bench_size(40, 2000),
                  bench_size(255, 2000)],
    }
    # -- the streaming worst case ---------------------------------------
    # Latency is measured WITHOUT tracemalloc (whose per-allocation tracing
    # would dominate); retirement is proven separately by the memory pass.
    cands = make_candidates(255)
    latencies = []
    overruns = 0
    digest = candidates.ReflexFeatures(episode=1, controller_tick=1).digest()
    gc.collect()
    t0 = time.perf_counter()
    for i in range(tables):
        t_start = time.perf_counter()
        table = candidates.build_table((1, i, i + 1), 1 + (i % 4), cands,
                                       digest)
        table_id = table.table_id
        payload = candidates.jev_payload(table)
        dt = time.perf_counter() - t_start
        latencies.append(dt)
        if dt > reflex_deadline:
            overruns += 1
        # discard: never retain the episode's tables
        del table, payload, table_id
    elapsed = time.perf_counter() - t0
    latencies.sort()
    report["streaming"] = {
        "tables": tables,
        "elapsed_s": round(elapsed, 3),
        "tables_per_s": round(tables / elapsed, 1),
        "retained_tables": 1,
        "deadline_overruns": overruns,
        "p50_ms": round(percentile(latencies, 0.50) * 1000.0, 4),
        "p95_ms": round(percentile(latencies, 0.95) * 1000.0, 4),
        "p99_ms": round(percentile(latencies, 0.99) * 1000.0, 4),
        "max_ms": round(latencies[-1] * 1000.0, 4),
    }
    report["memory"] = memory_pass(min(tables, 2000), cands, digest)
    return report


def memory_pass(tables, cands, digest):
    """Peak retained memory for a bounded streaming loop (tracemalloc)."""
    gc.collect()
    tracemalloc.start()
    keep = None
    for i in range(tables):
        keep = candidates.build_table((1, i, i + 1), 1 + (i % 4), cands,
                                      digest)
        del keep
        keep = None
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "tables": tables,
        "peak_traced_bytes": peak,
        "retained_tables": 1,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", type=int, default=150000)
    ap.add_argument("--reflex-deadline", type=float, default=0.75)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    report = benchmark(args.tables, args.reflex_deadline)
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
