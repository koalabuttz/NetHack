#!/usr/bin/env python3
"""Shared schema and (de)serialization for the paired Jev corpus.

The Phase-0 baseline corpus lives in
``test/agent/fixtures/jev_legacy_requests/``.  Each fixture is a
**self-contained paired record**: the raw legacy body alone cannot reconstruct
its context (replay deliberately falls back offline), so ``manifest.json``
carries, per fixture name, the complete frozen renderer inputs and the frozen
retained table, and the legacy raw request bytes beside it.

This module owns exactly two things:

  * the **schema** -- the load-bearing fields and their validation, so a
    fixture that omits one is rejected rather than silently skipped; and
  * the **frozen <-> live translation** -- rebuilding real
    :class:`protocol.Snapshot`, :class:`state.EpisodeMemory`,
    :class:`instances.TerrainMemory`, directive views and a
    :class:`candidates.CandidateTable` from the serialized record, so the new
    semantic wire request for a fixture is re-rendered deterministically from
    the same inputs the legacy one was.

This module makes no network call and no wall-clock read.
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.agent import (candidates, directives as directives_mod,  # noqa: E402
                         instances, protocol, state)
from tools.agent.providers import ReflexContext  # noqa: E402

#: Schema version of one manifest entry.  Bump when a frozen field changes
#: meaning, so an old corpus can never be read as a new one.
SCHEMA_VERSION = 1

#: Every load-bearing field of one manifest entry.  ``bodies/<name>.json`` is
#: resolved from ``legacy_body``; the rest are read directly.
REQUIRED_FIELDS = (
    "legacy_body",
    "retained_table",
    "frozen_context",
    "canned_response",
    "expected_legacy_parser_selected_index",
    "expected_outcome",
    "capture",
)

#: The load-bearing sub-fields of ``retained_table``.
TABLE_FIELDS = (
    "table_id", "need_key", "table_version", "rejection_version",
    "features_digest", "jev_eligibility", "scripted_index", "candidates",
)

#: The load-bearing sub-fields of ``frozen_context``.
CONTEXT_FIELDS = (
    "tick", "need", "need_key", "pages", "intent",
    "snapshot", "memory", "terrain", "directives",
)

#: The load-bearing sub-fields of one frozen candidate record.
CANDIDATE_FIELDS = (
    "candidate_id", "action", "action_signature", "semantic_label", "family",
    "family_rank", "direction", "direction_rank", "rows", "score",
    "score_components", "reason", "proposed_effect", "effect_payload",
)

#: The load-bearing sub-fields of ``canned_response`` (wholly in retained-index
#: terms -- nothing in a fixture references a legacy ``opt-N`` key directly).
CANNED_FIELDS = ("chosen_retained_index", "confidence", "probabilities",
                 "usage")

#: The load-bearing sub-fields of the capture metadata.
CAPTURE_FIELDS = ("commit", "adapter_version", "tick_context")

OUTCOME_SELECTED = "selected"
OUTCOME_REFUSED = "refused:"
OUTCOME_FALLBACK = "fallback:"


def default_root():
    return os.path.join(_HERE, "fixtures", "jev_legacy_requests")


def load_manifest(root=None):
    """The parsed ``manifest.json`` of the paired corpus."""
    root = root or default_root()
    with open(os.path.join(root, "manifest.json"), "r") as handle:
        return json.load(handle)


def fixture_names(manifest):
    return sorted(manifest.get("fixtures", {}))


def validate_entry(name, entry):
    """Every schema problem in one manifest entry (empty means valid)."""
    problems = []
    if not isinstance(entry, dict):
        return ["%s: entry is not an object" % name]
    for field in REQUIRED_FIELDS:
        if field not in entry:
            problems.append("%s: missing field %r" % (name, field))
    table = entry.get("retained_table")
    if isinstance(table, dict):
        for field in TABLE_FIELDS:
            if field not in table:
                problems.append("%s: retained_table missing %r"
                                % (name, field))
        cands = table.get("candidates")
        if isinstance(cands, list):
            for i, cand in enumerate(cands):
                if not isinstance(cand, dict):
                    problems.append("%s: candidate %d is not an object"
                                    % (name, i))
                    continue
                for field in CANDIDATE_FIELDS:
                    if field not in cand:
                        problems.append(
                            "%s: candidate %d missing %r" % (name, i, field))
        else:
            problems.append("%s: retained_table.candidates is not a list"
                            % name)
    else:
        problems.append("%s: retained_table is not an object" % name)
    frozen = entry.get("frozen_context")
    if isinstance(frozen, dict):
        for field in CONTEXT_FIELDS:
            if field not in frozen:
                problems.append("%s: frozen_context missing %r"
                                % (name, field))
    else:
        problems.append("%s: frozen_context is not an object" % name)
    canned = entry.get("canned_response")
    if isinstance(canned, dict):
        for field in CANNED_FIELDS:
            if field not in canned:
                problems.append("%s: canned_response missing %r"
                                % (name, field))
    else:
        problems.append("%s: canned_response is not an object" % name)
    capture = entry.get("capture")
    if isinstance(capture, dict):
        for field in CAPTURE_FIELDS:
            if field not in capture:
                problems.append("%s: capture missing %r" % (name, field))
    else:
        problems.append("%s: capture is not an object" % name)
    outcome = entry.get("expected_outcome")
    if not isinstance(outcome, str) or not (
            outcome == OUTCOME_SELECTED
            or outcome.startswith(OUTCOME_REFUSED)
            or outcome.startswith(OUTCOME_FALLBACK)):
        problems.append("%s: expected_outcome %r is not selected/"
                        "refused:<code>/fallback:<category>"
                        % (name, outcome))
    if "expected_legacy_parser_selected_index" not in entry:
        problems.append("%s: missing expected_legacy_parser_selected_index"
                        % name)
    return problems


def validate_manifest(manifest):
    """Every schema problem in the whole corpus (empty means valid)."""
    problems = []
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, dict):
        return ["manifest.fixtures is not an object"]
    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append("manifest.schema_version is not %d" % SCHEMA_VERSION)
    for name in sorted(fixtures):
        problems.extend(validate_entry(name, fixtures[name]))
    return problems


# -- serialization (live -> frozen) ---------------------------------------

def _pair(pos):
    return [int(pos[0]), int(pos[1])]


def _key(pos):
    return "%d,%d" % (pos[0], pos[1])


def freeze_snapshot(snap):
    return {
        "map": {_key(p): list(cell) for p, cell in sorted(snap.map.items())},
        "s": {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in sorted((snap.s or {}).items())},
        "cond": list(snap.cond or []),
        "msg": list(snap.msg or []),
    }


def freeze_memory(mem):
    st = mem.status
    inv = mem.inventory
    return {
        "status": {"hp": st.hp, "hp_max": st.hp_max, "hunger": st.hunger,
                   "dlvl": st.dlvl, "time": st.time, "gold": st.gold,
                   "level": st.level},
        "hero": _pair(mem.hero) if mem.hero is not None else None,
        "stairs_down": [_pair(p) for p in sorted(mem.stairs_down)],
        "stairs_up": [_pair(p) for p in sorted(mem.stairs_up)],
        "messages": list(mem.messages),
        "inventory": {"rows": [dict(r) for r in inv.rows],
                      "seen_tick": inv.seen_tick,
                      "seen_time": inv.seen_time},
    }


def freeze_terrain(terrain):
    if terrain is None:
        return {"terrain": {}, "occupancy": {}}
    return {
        "terrain": {_key(p): t for p, t in sorted(terrain.terrain.items())},
        "occupancy": {_key(p): o
                      for p, o in sorted(terrain.occupancy.items())},
    }


def freeze_directives(views):
    out = []
    for view in views or ():
        dset = getattr(view, "dset", None)
        if dset is None:
            dset = view if isinstance(view, directives_mod.DirectiveSet) \
                else None
        out.append(dset.to_dict() if dset is not None else None)
    return out


def freeze_context(ctx):
    """The frozen renderer inputs for one prepared request."""
    return {
        "tick": int(getattr(ctx, "tick", 0) or 0),
        "need": dict(ctx.need or {}),
        "need_key": list(candidates.normalize_need_key(ctx.need_key)),
        "pages": list(ctx.pages or []),
        "intent": getattr(ctx, "intent", "") or "",
        "snapshot": freeze_snapshot(ctx.snapshot),
        "memory": freeze_memory(ctx.memory),
        "terrain": freeze_terrain(getattr(ctx, "terrain", None)),
        "directives": freeze_directives(getattr(ctx, "directives", ())),
    }


def freeze_table(table, features_digest=""):
    """The complete frozen candidate records of one retained table.

    ``features_digest`` is a table-identity input but is not carried on the
    frozen :class:`candidates.CandidateTable`, so it is passed in and recorded
    here; without it the rebuilt table would have a different ``table_id``.
    """
    return {
        "table_id": table.table_id,
        "need_key": list(table.need_key),
        "table_version": table.table_version,
        "rejection_version": table.rejection_version,
        "features_digest": features_digest,
        "jev_eligibility": bool(table.jev_eligibility),
        "scripted_index": table.scripted_index,
        "candidates": [_freeze_candidate(c) for c in table.ordered_candidates],
    }


def _freeze_candidate(cand):
    return {
        "candidate_id": cand.candidate_id,
        "action": cand.action.to_wire(),
        "action_signature": cand.action_signature,
        "semantic_label": cand.semantic_label,
        "family": cand.family,
        "family_rank": cand.family_rank,
        "direction": [int(d) for d in cand.direction],
        "direction_rank": cand.direction_rank,
        "rows": [[int(r), int(c)] for r, c in cand.rows],
        "score": cand.score,
        "score_components": [[str(n), int(v)] for n, v in cand.score_components],
        "reason": cand.reason,
        "proposed_effect": cand.proposed_effect,
        "effect_payload": candidates._payload_json(cand.effect_payload),
    }


# -- deserialization (frozen -> live) -------------------------------------

def unfreeze_snapshot(frozen):
    snap = protocol.Snapshot()
    snap.map = {}
    for key, cell in frozen["map"].items():
        x, y = key.split(",")
        snap.map[(int(x), int(y))] = tuple(cell)
    snap.s = {k: (dict(v) if isinstance(v, dict) else v)
              for k, v in (frozen.get("s") or {}).items()}
    snap.cond = list(frozen.get("cond") or [])
    snap.msg = list(frozen.get("msg") or [])
    return snap


def unfreeze_memory(frozen):
    mem = state.EpisodeMemory()
    st = mem.status
    status = frozen["status"]
    st.hp = status.get("hp")
    st.hp_max = status.get("hp_max")
    st.hunger = status.get("hunger") or ""
    st.dlvl = status.get("dlvl") or ""
    st.time = status.get("time")
    st.gold = status.get("gold")
    st.level = status.get("level")
    hero = frozen.get("hero")
    mem.hero = tuple(hero) if hero else None
    for key, name in (("stairs_down", "stairs_down"),
                      ("stairs_up", "stairs_up")):
        target = getattr(mem, name)
        for pair in frozen.get(key) or []:
            target.add((int(pair[0]), int(pair[1])))
    mem.messages = [str(m) for m in frozen.get("messages") or []]
    inv = frozen.get("inventory") or {}
    mem.inventory.rows = [dict(r) for r in inv.get("rows") or []]
    mem.inventory.seen_tick = inv.get("seen_tick")
    mem.inventory.seen_time = inv.get("seen_time")
    return mem


def unfreeze_terrain(frozen):
    terrain = instances.TerrainMemory()
    for key, klass in (frozen.get("terrain") or {}).items():
        x, y = key.split(",")
        terrain.terrain[(int(x), int(y))] = klass
    for key, occ in (frozen.get("occupancy") or {}).items():
        x, y = key.split(",")
        terrain.occupancy[(int(x), int(y))] = occ
    return terrain


def unfreeze_directives(frozen):
    views = []
    for entry in frozen or []:
        if entry is None:
            continue
        dset, why = directives_mod.validate_directive_set(entry)
        if dset is None:
            raise ValueError("frozen directive is invalid: %s" % why)
        views.append(directives_mod.DirectiveView(dset, 1))
    return views


def unfreeze_context(frozen):
    """Rebuild a live :class:`ReflexContext` from its frozen rendering inputs."""
    ctx = ReflexContext(
        episode=1, tick=int(frozen.get("tick", 0)),
        need=dict(frozen["need"]), need_key=tuple(frozen["need_key"]),
        snapshot=unfreeze_snapshot(frozen["snapshot"]),
        pages=list(frozen.get("pages") or []),
        memory=unfreeze_memory(frozen["memory"]),
        intent=frozen.get("intent") or "",
        directives=unfreeze_directives(frozen.get("directives")),
        deadline=0.0)
    # The read-only classified-terrain reference is set by attribute so this
    # reader also works against a context type that has not declared it yet.
    ctx.terrain = unfreeze_terrain(frozen.get("terrain"))
    return ctx


def unfreeze_table(frozen):
    """Rebuild the exact frozen retained table, verifying its identity.

    The feature digest and rejection version are table-identity inputs, so
    they are frozen alongside the candidates; rebuilding must reproduce the
    identical ``table_id`` or this raises.
    """
    cands = []
    for rec in frozen["candidates"]:
        cands.append(candidates.make_candidate(
            rec["action"], rec["semantic_label"], family=rec["family"],
            direction=tuple(rec["direction"]),
            direction_rank=int(rec["direction_rank"]), score=int(rec["score"]),
            score_components=[(str(n), int(v))
                              for n, v in rec["score_components"]],
            reason=rec["reason"], proposed_effect=rec["proposed_effect"],
            effect_payload=tuple(rec["effect_payload"])))
    table = candidates.build_table(
        tuple(frozen["need_key"]), int(frozen["table_version"]), cands,
        features_digest=frozen.get("features_digest") or "",
        jev_eligibility=bool(frozen["jev_eligibility"]),
        rejection_version=int(frozen["rejection_version"]))
    if table.table_id != frozen["table_id"]:
        raise ValueError("rebuilt table id %s != frozen %s"
                         % (table.table_id, frozen["table_id"]))
    for i, (rec, cand) in enumerate(zip(frozen["candidates"],
                                        table.ordered_candidates)):
        if rec["candidate_id"] != cand.candidate_id:
            raise ValueError("candidate %d id drift: %s != %s"
                             % (i, rec["candidate_id"], cand.candidate_id))
    return table


def prepared_from_frozen(frozen):
    return candidates.PreparedReflex(
        immutable_features=candidates.ReflexFeatures(), table=unfreeze_table(
            frozen))
