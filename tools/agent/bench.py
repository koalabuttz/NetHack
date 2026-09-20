"""Campaign bench CLI, spec validation, runner and suggest-first tuner.

The bench is **agent-side tooling that observes and configures**: it validates
a declarative ``bench-spec/1``, runs bounded campaigns by wrapping the existing
``Controller.run_campaign(1)`` in isolated episode directories, scores the
artifacts with :mod:`tools.agent.bench_metrics`, compares runs, requests an
advisory per-episode Jev judgment (see :mod:`tools.agent.bench_judge`), and
suggests (or, only with explicit approval, narrowly applies) parameter changes.
It never patches the engine, the controller or a running agent.

Commands:

    python3 -m tools.agent.bench {validate,run,score,compare,tune,package} ...

Hard rules enforced here:
* the bench observes and configures -- no live controller/reflex edits;
* credentials stay config *references* (never copied into output);
* artifacts are versioned, additive, and record *unavailable* rather than zero;
* ``postmortem_reserve`` is forced to 0 (no DeepSeek postmortems in bench);
* stops admit no further work and reap only the owned episode tree.
"""

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import bench_metrics as M

SPEC_SCHEMA = "bench-spec/1"
MANIFEST_SCHEMA = "bench-manifest/1"
DRY_RUN = "dry-run"
LIVE = "live"
PROFILES = ("smoke", "full", "confirmation")
TIERS = (DRY_RUN, LIVE)
COST_MODES = ("priced-bound", "call-bounded", "operator-approved-unknown")

SPEC_TOP_KEYS = (
    "schema_version", "name", "tier", "profile", "provider_config_ref",
    "overrides", "episodes", "episode_timeout_s", "campaign_timeout_s",
    "replay_inputs", "baseline_ref", "budget", "judge", "comparison",
    "tuning",
)
BUDGET_KEYS = ("strategy_calls_total", "judge_calls_total",
               "max_total_episodes", "max_candidates", "max_total_wall_s",
               "usd_limit", "cost_mode", "external_limit_ref")
JUDGE_KEYS = ("enabled", "model", "rubric_version", "deadline_s",
              "max_state_bytes", "max_response_bytes", "retries")
COMPARISON_KEYS = (
    "metric_policy_version", "target_metric", "min_improvement",
    "noninferiority_margins", "min_samples", "resampling_seed", "resamples",
    "confidence_level", "screening_episodes_per_arm",
    "confirmation_episodes_per_arm", "min_completed_episodes_per_arm",
    "min_aggregate_at_risk_ticks_per_arm", "deadline_classification",
    "invalid_policy_version", "invalid_policy_approval_hash",
)
TUNING_KEYS = ("mode", "parameters", "approval_id", "approval_expiry",
               "expected_base_config_hash")
#: Optional tuning keys that are still part of the *allowed* schema.
TUNING_OPTIONAL_KEYS = ("overlay_version",)
DEADLINE_CLASSES = ("horizon-completion", "adverse-early")

#: Provider knobs an operator may override through a spec, and the frozen ones.
OVERRIDABLE_KNOBS = (
    "reflex", "strategy", "role", "max_ticks", "strategy_call_cap",
    "reflex_call_cap", "boundary_cooldown_ticks",
    "boundary_cooldown_wall", "boundary_emergency_wall", "token_cap",
    "usd_cap", "deepseek_price_in", "deepseek_price_out",
    "deepseek_price_cache_hit", "deepseek_model", "deepseek_base_url",
    "deepseek_key_file", "jev_key_file", "jev_base_url", "jev_accept_terms",
    "jev_relative_factor", "jev_confidence_mode", "postmortem_reserve",
)

#: The tuner's eligible coordinate-search knobs (plan §6 rails).  Everything
#: else -- especially ``jev_relative_factor`` -- is frozen by default.
TUNER_ELIGIBLE_KNOBS = ("reflex_call_cap", "strategy_call_cap",
                        "boundary_cooldown_ticks", "boundary_cooldown_wall")
TUNER_FROZEN_KNOBS = ("jev_relative_factor", "jev_confidence_mode",
                      "confidence_threshold", "low_confidence_needs",
                      "boundary_emergency_wall", "max_ticks")


# --------------------------------------------------------------------------
# spec validation
# --------------------------------------------------------------------------

def _finite(v) -> bool:
    return (not isinstance(v, bool) and isinstance(v, (int, float))
            and math.isfinite(float(v)))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _unknown_keys(obj: dict, allowed: Sequence[str], where: str) -> List[str]:
    if not isinstance(obj, dict):
        return ["%s must be an object" % where]
    return ["%s: unknown key %r" % (where, k) for k in sorted(obj)
            if k not in allowed]


def validate_spec(spec: dict) -> Optional[str]:
    """Return an error string for an invalid ``bench-spec/1``, else ``None``."""
    if not isinstance(spec, dict):
        return "spec must be an object"
    problem = _unknown_keys(spec, SPEC_TOP_KEYS, "spec")
    if problem:
        return problem[0]
    for key in SPEC_TOP_KEYS:
        if key not in spec:
            return "spec missing required key %r" % key
    if spec.get("schema_version") != SPEC_SCHEMA:
        return "schema_version must be %r" % SPEC_SCHEMA
    if spec.get("tier") not in TIERS:
        return "tier must be one of %s" % (TIERS,)
    if spec.get("profile") not in PROFILES:
        return "profile must be one of %s" % (PROFILES,)
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        return "name must be a non-empty string"
    if not _is_int(spec.get("episodes")) or spec["episodes"] < 1:
        return "episodes must be a positive integer"
    for key in ("episode_timeout_s", "campaign_timeout_s"):
        if not _finite(spec.get(key)) or spec[key] <= 0:
            return "%s must be a positive finite number" % key
    if not isinstance(spec.get("provider_config_ref"), (str, dict)):
        return "provider_config_ref must be a string or object reference"
    if not isinstance(spec.get("overrides"), dict):
        return "overrides must be an object"
    bad = [k for k in spec["overrides"] if k not in OVERRIDABLE_KNOBS]
    if bad:
        return "overrides contains non-allowlisted knob %r" % bad[0]
    if not isinstance(spec.get("replay_inputs"), list):
        return "replay_inputs must be a list"

    problem = _validate_budget(spec["budget"])
    if problem:
        return problem
    problem = _validate_judge(spec["judge"])
    if problem:
        return problem
    problem = _validate_comparison(spec["comparison"])
    if problem:
        return problem
    problem = _validate_tuning(spec["tuning"])
    if problem:
        return problem
    return None


def _validate_budget(b: dict) -> Optional[str]:
    problem = _unknown_keys(b, BUDGET_KEYS, "budget")
    if problem:
        return problem[0]
    for key in BUDGET_KEYS:
        if key not in b:
            return "budget missing required key %r" % key
    for key in ("strategy_calls_total", "judge_calls_total",
                "max_total_episodes", "max_candidates", "max_total_wall_s"):
        if not _is_int(b[key]) or b[key] < 0:
            return "budget.%s must be a nonnegative integer" % key
    if b["cost_mode"] not in COST_MODES:
        return "budget.cost_mode must be one of %s" % (COST_MODES,)
    if b["usd_limit"] is not None:
        if not _finite(b["usd_limit"]) or b["usd_limit"] < 0:
            return "budget.usd_limit must be null or nonnegative finite"
        if b["cost_mode"] != "priced-bound":
            return ("budget.usd_limit requires cost_mode=priced-bound")
    if b["external_limit_ref"] is not None \
            and not isinstance(b["external_limit_ref"], str):
        return "budget.external_limit_ref must be null or a string"
    return None


def _validate_judge(j: dict) -> Optional[str]:
    problem = _unknown_keys(j, JUDGE_KEYS, "judge")
    if problem:
        return problem[0]
    for key in JUDGE_KEYS:
        if key not in j:
            return "judge missing required key %r" % key
    if not isinstance(j["enabled"], bool):
        return "judge.enabled must be a boolean"
    if not isinstance(j["model"], str) or not j["model"]:
        return "judge.model must be a non-empty string"
    if not isinstance(j["rubric_version"], str) or not j["rubric_version"]:
        return "judge.rubric_version must be a non-empty string"
    if j["retries"] != 0:
        return "judge.retries must be exactly 0 (no automatic retries)"
    for key in ("deadline_s",):
        if not _finite(j[key]) or j[key] <= 0:
            return "judge.%s must be positive and finite" % key
    for key in ("max_state_bytes", "max_response_bytes"):
        if not _is_int(j[key]) or j[key] < 1:
            return "judge.%s must be a positive integer" % key
    if j["max_state_bytes"] > 8 * 1024:
        return "judge.max_state_bytes must be <= 8192 (initial 8 KiB cap)"
    return None


def _validate_comparison(c: dict) -> Optional[str]:
    problem = _unknown_keys(c, COMPARISON_KEYS, "comparison")
    if problem:
        return problem[0]
    for key in COMPARISON_KEYS:
        if key not in c:
            return "comparison missing required key %r" % key
    for key in ("screening_episodes_per_arm", "confirmation_episodes_per_arm",
                "min_samples", "resamples", "resampling_seed",
                "min_completed_episodes_per_arm",
                "min_aggregate_at_risk_ticks_per_arm"):
        if not _is_int(c[key]) or c[key] < 0:
            return "comparison.%s must be a nonnegative integer" % key
    if c["screening_episodes_per_arm"] < 1:
        return "comparison.screening_episodes_per_arm must be >= 1"
    if c["confirmation_episodes_per_arm"] <= \
            c["screening_episodes_per_arm"]:
        return ("comparison.confirmation_episodes_per_arm must be strictly "
                "greater than screening_episodes_per_arm")
    if c["min_samples"] < 1:
        return "comparison.min_samples must be >= 1"
    if c["min_completed_episodes_per_arm"] == 0 \
            and c["min_aggregate_at_risk_ticks_per_arm"] == 0:
        return ("comparison requires a predeclared exposure floor: at least one "
                "of min_completed_episodes_per_arm / "
                "min_aggregate_at_risk_ticks_per_arm must be non-zero")
    if c["deadline_classification"] not in DEADLINE_CLASSES:
        return "comparison.deadline_classification must be one of %s" \
            % (DEADLINE_CLASSES,)
    for key in ("min_improvement", "confidence_level"):
        if not _finite(c[key]):
            return "comparison.%s must be a finite number" % key
    if not (0.0 < c["confidence_level"] < 1.0):
        return "comparison.confidence_level must be in (0, 1)"
    if not isinstance(c["noninferiority_margins"], dict):
        return "comparison.noninferiority_margins must be an object"
    for k, v in c["noninferiority_margins"].items():
        if not _finite(v):
            return "noninferiority_margins[%r] must be finite" % k
    for key in ("metric_policy_version", "invalid_policy_version"):
        if not isinstance(c[key], str) or not c[key]:
            return "comparison.%s must be a non-empty string" % key
    if not isinstance(c["invalid_policy_approval_hash"], str) \
            or not c["invalid_policy_approval_hash"]:
        return ("comparison.invalid_policy_approval_hash must be a non-empty "
                "string")
    return None


def _validate_tuning(t: dict) -> Optional[str]:
    problem = _unknown_keys(t, tuple(TUNING_KEYS) + TUNING_OPTIONAL_KEYS,
                            "tuning")
    if problem:
        return problem[0]
    for key in TUNING_KEYS:
        if key not in t:
            return "tuning missing required key %r" % key
    if "overlay_version" in t and (not _is_int(t["overlay_version"])
                                   or t["overlay_version"] < 0):
        return "tuning.overlay_version must be a nonnegative integer"
    if t["mode"] not in ("suggest", "apply-approved"):
        return "tuning.mode must be suggest or apply-approved"
    if not isinstance(t["parameters"], dict):
        return "tuning.parameters must be an object"
    for knob, rail in t["parameters"].items():
        if knob not in TUNER_ELIGIBLE_KNOBS and knob not in TUNER_FROZEN_KNOBS:
            return "tuning.parameters has unknown knob %r" % knob
        if not isinstance(rail, dict) or "grid" not in rail:
            return "tuning.parameters[%r] must have a grid" % knob
        grid = rail["grid"]
        if not isinstance(grid, list) or not grid:
            return "tuning.parameters[%r].grid must be a non-empty list" % knob
        if any(not _finite(v) for v in grid):
            return "tuning.parameters[%r].grid must be finite numbers" % knob
        lo, hi = rail.get("min"), rail.get("max")
        if (lo is not None and any(v < lo for v in grid)) or \
                (hi is not None and any(v > hi for v in grid)):
            return ("tuning.parameters[%r].grid exceeds operator rails" % knob)
    return None


def load_spec(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)
    problem = validate_spec(spec)
    if problem:
        raise ValueError(problem)
    return spec


# --------------------------------------------------------------------------
# provider-config resolution (references only, postmortem reserve forced 0)
# --------------------------------------------------------------------------

def resolve_provider_config(spec: dict, *, base: Optional[dict] = None
                            ) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve ``provider_config_ref`` + allowlisted ``overrides`` to a config.

    Returns ``(config, error)``.  ``postmortem_reserve`` is forced to 0 and the
    resolved values are validated through ``ProviderConfig.validate`` -- the one
    authority -- so an impossible cap is rejected before any spawn.
    """
    from . import providers
    ref = spec["provider_config_ref"]
    values: Dict[str, Any] = {}
    if isinstance(ref, dict):
        values.update(ref)
    elif isinstance(ref, str) and os.path.exists(ref):
        with open(ref, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            return None, "provider_config_ref file must contain an object"
        values.update(loaded)
    if base:
        values.update(base)
    values.update(spec.get("overrides") or {})
    # Bench policy: no DeepSeek postmortems, ever.  Reject an explicit attempt
    # to enable them rather than silently clamping.
    if values.get("postmortem_reserve", 0):
        return None, ("postmortem_reserve must be 0 in a bench campaign "
                      "(DeepSeek postmortems are disabled)")
    values["postmortem_reserve"] = 0
    unknown = [k for k in values if k not in OVERRIDABLE_KNOBS]
    if unknown:
        return None, "provider config has unknown knob %r" % unknown[0]
    try:
        config = providers.ProviderConfig(**values)
    except TypeError as exc:
        return None, "provider config: %s" % exc
    problem = config.validate(episodes=spec["episodes"],
                              episode_timeout=spec["episode_timeout_s"])
    if problem:
        return None, problem
    return config, None


def jev_profile_conflict(config) -> Optional[str]:
    """Reject a live Jev profile whose caps would silently disable Jev.

    A token cap or a USD cap disables paid Jev dispatch in the ledger, so a
    "strict-budget live-Jev" spec cannot honestly ship by wrapping those caps.
    """
    if getattr(config, "reflex", None) != "jev":
        return None
    if getattr(config, "token_cap", 0):
        return ("live Jev reflex with a token cap: Jev dispatch is disabled "
                "under a token cap, so this profile cannot provide the live "
                "coverage it claims")
    if getattr(config, "usd_cap", None) is not None:
        return ("live Jev reflex with a USD cap: Jev dispatch is disabled "
                "under a USD cap")
    return None


def paid_call_bound(config) -> Dict[str, Any]:
    """The *paid* Jev call bound, if a verified one exists -- else unavailable.

    The applied reflex cap bounds *applied decisions*, not paid calls, so the
    bench never multiplies it by a presumed cost to manufacture a spend bound.
    """
    if getattr(config, "reflex", None) != "jev":
        return {"basis": "not-a-paid-tier", "bound": None}
    return {
        "basis": "no-verified-bound",
        "bound": None,
        "applied_cap": getattr(config, "reflex_call_cap", 0),
        "note": ("applied cap bounds applied decisions, not paid calls; a "
                 "token/USD cap disables Jev, so no strict spend bound exists "
                 "without an external provider-account limit"),
    }


def preflight(spec: dict) -> Dict[str, Any]:
    """Validate a spec end-to-end before any spawn.  Returns a report dict."""
    problem = validate_spec(spec)
    if problem:
        return {"ok": False, "stage": "spec", "error": problem}
    if spec["tier"] == LIVE:
        attest = vapor_cloud_attestation()
        if not attest["attested"]:
            return {"ok": False, "stage": "live-gate",
                    "error": ("live bench testing requires the vapor-cloud "
                              "attestation: %s" % attest["reason"])}
    config, err = resolve_provider_config(spec)
    if err:
        return {"ok": False, "stage": "provider_config", "error": err}
    conflict = jev_profile_conflict(config)
    if conflict:
        return {"ok": False, "stage": "scope", "error": conflict}
    problem = budget_preflight(spec, config)
    if problem:
        return {"ok": False, "stage": "budget", "error": problem}
    b = spec["budget"]
    if b["cost_mode"] == "priced-bound":
        from . import providers
        if not providers.tariff_complete(config):
            return {"ok": False, "stage": "cost",
                    "error": ("cost_mode=priced-bound requires a complete "
                              "DeepSeek tariff (price_in and price_out)")}
    effective, unattended, forced_from = effective_cost_mode(spec, config)
    return {"ok": True, "stage": "done", "config": config,
            "paid_call_bound": paid_call_bound(config),
            "effective_cost_mode": effective,
            "cost_mode_forced_from": forced_from,
            "unattended_apply_allowed": unattended,
            "allocations": plan_allocations(spec, max(1, spec["budget"][
                "max_candidates"])),
            "postmortem_reserve": config.postmortem_reserve}


def effective_cost_mode(spec: dict, config) -> Tuple[str, bool, Optional[str]]:
    """Resolve the honest cost mode, forcing unknown-exposure when unstrict.

    A live Jev profile that claims ``call-bounded`` without an external verified
    limit is **not** strict: it is forced to ``operator-approved-unknown`` and
    loses unattended apply.
    """
    budget = spec["budget"]
    mode = budget["cost_mode"]
    if (getattr(config, "reflex", None) == "jev"
            and mode == "call-bounded"
            and not budget.get("external_limit_ref")):
        return "operator-approved-unknown", False, mode
    unattended = mode != "operator-approved-unknown"
    return mode, unattended, None


def budget_preflight(spec: dict, config) -> Optional[str]:
    """Reject an allocation plan that exceeds the declared limits."""
    budget = spec["budget"]
    comp = spec["comparison"]
    episodes = spec["episodes"]
    if budget["max_total_episodes"] and \
            episodes > budget["max_total_episodes"]:
        return ("episodes (%d) exceed budget.max_total_episodes (%d)"
                % (episodes, budget["max_total_episodes"]))
    # the whole campaign must fit the campaign timeout
    campaign = spec.get("campaign_timeout_s") or 0
    if campaign and episodes * spec["episode_timeout_s"] > campaign:
        return ("episodes x episode_timeout_s (%g) exceed campaign_timeout_s "
                "(%g)" % (episodes * spec["episode_timeout_s"], campaign))
    # judge budget: zero is literally zero, and enabled needs a real budget
    if spec["judge"]["enabled"]:
        if budget["judge_calls_total"] <= 0:
            return ("judge.enabled requires budget.judge_calls_total > 0 "
                    "(zero means zero judge calls)")
        if episodes > budget["judge_calls_total"]:
            return ("episodes (%d) exceed budget.judge_calls_total (%d): one "
                    "bundled dispatch per eligible episode"
                    % (episodes, budget["judge_calls_total"]))
    # strategy demand against the strategy-call budget
    strategy_cap = int(getattr(config, "strategy_call_cap", 0) or 0)
    if getattr(config, "strategy", "off") != "off" and strategy_cap:
        demand = episodes * max(1, strategy_cap)
        if budget["strategy_calls_total"] and \
                demand > budget["strategy_calls_total"]:
            return ("strategy demand (%d = episodes x strategy_call_cap) "
                    "exceeds budget.strategy_calls_total (%d)"
                    % (demand, budget["strategy_calls_total"]))
    del comp
    return None


#: The caller-supplied attestation that the vapor-cloud fix is landed.  The
#: bench never invents it; without it no live tier passes preflight (AC10).
VAPOR_CLOUD_ENV = "BENCH_VAPOR_CLOUD_ATTESTED"


def vapor_cloud_attestation(source: Optional[dict] = None) -> Dict[str, Any]:
    """Whether the operator has attested the vapor-cloud prerequisite."""
    source = os.environ if source is None else source
    value = (source.get(VAPOR_CLOUD_ENV) or "").strip()
    if not value:
        return {"attested": False,
                "reason": "pending-operator (set %s)" % VAPOR_CLOUD_ENV}
    return {"attested": True, "token": value,
            "reason": "operator-attested"}


def precommit(comparison: dict) -> Dict[str, Any]:
    """Freeze the comparison policy at run start (no post-result change)."""
    keys = ("target_metric", "min_improvement", "noninferiority_margins",
            "min_samples", "resampling_seed", "resamples", "confidence_level",
            "screening_episodes_per_arm", "confirmation_episodes_per_arm",
            "min_completed_episodes_per_arm",
            "min_aggregate_at_risk_ticks_per_arm", "deadline_classification",
            "metric_policy_version", "invalid_policy_version")
    return {k: comparison.get(k) for k in keys}


def assert_precommitted(pre: dict, comparison: dict) -> None:
    """Raise when a policy field changed after results (forbidden in place)."""
    current = precommit(comparison)
    drift = [k for k in pre if pre[k] != current.get(k)]
    if drift:
        raise ValueError("comparison policy changed after precommit: %s "
                         "(abandon the run and re-precut it)" % drift)


def precommit_record(comparison: dict, *, episodes: Optional[int] = None,
                     arm_episodes: Optional[int] = None) -> Dict[str, Any]:
    """A hashable precommit record, persisted before any result exists.

    It commits the comparison **policy** *and* the exact balanced counter-
    balanced schedule (built from ``arm_episodes``, defaulting to the spec's
    ``confirmation_episodes_per_arm``).  The recorded ``hash`` covers the whole
    design, so editing either the policy or the schedule is detectable.
    """
    policy = precommit(comparison)
    if arm_episodes is None:
        arm_episodes = int(comparison.get("confirmation_episodes_per_arm", 0)
                           or 0)
    design = M.precommit_design(comparison, arm_episodes=int(arm_episodes))
    if episodes is not None:
        design["total_episodes"] = int(episodes)
        sched = M.episode_schedule(int(episodes),
                                   int(comparison.get("resampling_seed", 0)))
        design["episode_schedule"] = sched
        design["expected_arm_counts"] = {
            "baseline": sum(1 for e in sched if e["arm"] == "baseline"),
            "candidate": sum(1 for e in sched if e["arm"] == "candidate"),
        }
    return {"schema_version": "bench-precommit/1",
            "policy": policy, "design": design,
            "hash": M.design_hash(design)}


# --------------------------------------------------------------------------
# budget planning
# --------------------------------------------------------------------------

def plan_allocations(spec: dict, n_candidates: int) -> Dict[str, Any]:
    """Reserve the whole campaign's episodes and judge calls up front.

    Every candidate is screened on ``screening_episodes_per_arm`` per arm, and
    the selected winner is confirmed on ``confirmation_episodes_per_arm`` per
    arm.  Judge calls are bounded at eligible episodes x request count.
    """
    comp = spec["comparison"]
    budget = spec["budget"]
    judge = spec["judge"]
    screen = int(comp["screening_episodes_per_arm"])
    confirm = int(comp["confirmation_episodes_per_arm"])
    judge_per_episode = 1 if spec.get("judge", {}).get("enabled") else 0
    screening_episodes = 2 * screen * max(0, int(n_candidates))
    confirmation_episodes = 2 * confirm
    total_episodes = screening_episodes + confirmation_episodes
    total_judges = judge_per_episode * total_episodes
    reasons: List[str] = []
    if budget["max_candidates"] and n_candidates > budget["max_candidates"]:
        reasons.append("candidate-budget: %d > %d"
                       % (n_candidates, budget["max_candidates"]))
    if budget["max_total_episodes"] and total_episodes > \
            budget["max_total_episodes"]:
        reasons.append("episode-budget: %d > %d"
                       % (total_episodes, budget["max_total_episodes"]))
    if budget["judge_calls_total"] and total_judges > \
            budget["judge_calls_total"]:
        reasons.append("judge-budget: %d > %d"
                       % (total_judges, budget["judge_calls_total"]))
    if judge["enabled"] and not budget["judge_calls_total"]:
        reasons.append("judge enabled but judge_calls_total is 0")
    return {
        "schema_version": "bench-allocations/1",
        "n_candidates": int(n_candidates),
        "screening_episodes_per_arm": screen,
        "confirmation_episodes_per_arm": confirm,
        "screening_episodes": screening_episodes,
        "confirmation_episodes": confirmation_episodes,
        "total_episodes": total_episodes,
        "judge_per_episode": judge_per_episode,
        "total_judge_calls": total_judges,
        "within_budget": not reasons,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------
# §7 forced-abort containment: ownership-isolated /proc PPID-recursion walk
# --------------------------------------------------------------------------

class ProcReader(object):
    """The pluggable ``/proc`` + signal abstraction (failure injectable)."""

    #: A reaped-but-unwaited process is still in /proc; it is not alive.
    DEAD_STATES = ("Z", "X", "x")

    def children(self, pid: int) -> List[int]:
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                identity = self.identity(int(entry))
            except ProcError:
                continue
            if identity and identity.get("ppid") == pid \
                    and identity.get("state") not in self.DEAD_STATES:
                out.append(int(entry))
        return sorted(out)

    def identity(self, pid: int) -> Optional[Dict[str, Any]]:
        try:
            with open("/proc/%d/stat" % pid, "r") as fh:
                stat = fh.read()
        except OSError:
            return None
        try:
            # comm may contain spaces/parens; parse after the last ')'.
            rest = stat[stat.rindex(")") + 2:].split()
            state = rest[0]
            ppid = int(rest[1])
            pgid = int(rest[2])
            session = int(rest[3])
            starttime = rest[19]
        except (ValueError, IndexError):
            return None
        return {"pid": pid, "ppid": ppid, "pgid": pgid, "session": session,
                "starttime": starttime, "state": state}

    def pgid(self, pid: int) -> int:
        """The process group of *pid*; ``-1`` only when it is gone.

        A permission failure is raised, never swallowed as ``-1``: treating an
        unreadable ``getpgid`` as "gone" is exactly the fail-open defect the
        review calls out.
        """
        try:
            return os.getpgid(pid)
        except ProcessLookupError:
            return -1
        except PermissionError:
            raise
        except OSError:
            return -1

    def members(self, pgid: int) -> List[int]:
        """Every live PID whose process group is *pgid* (for ownership checks)."""
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                if self.pgid(pid) == pgid:
                    out.append(pid)
            except PermissionError:
                raise
            except OSError:
                continue
        return sorted(out)

    def signal(self, pid: int, sig: int) -> None:
        os.kill(pid, sig)

    def signal_group(self, pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)


class ProcError(Exception):
    """A permission / identity-validation failure in the abort walk."""


class OwnedProcessTree(object):
    """Reap **only** a captured episode tree; never the supervisor's group.

    The walk re-discovers descendants by PPID recursion from the root, keeps
    the root alive while it does so, re-walks until the tree is stable, then
    reaps the root last and verifies no captured identity survives.  A
    permission or identity-validation failure is **fatal**: it sets
    ``teardown_failure`` and marks the run non-success.
    """

    def __init__(self, reader: Optional[ProcReader] = None, *,
                 grace: float = 2.0, bound: float = 10.0):
        self.reader = reader or ProcReader()
        self.grace = grace
        self.bound = bound

    def capture(self, pid: int) -> List[Dict[str, Any]]:
        """Capture the owned identities: the root plus its descendants."""
        return self._walk(pid)

    def _walk(self, root: int) -> List[Dict[str, Any]]:
        """PPID recursion from *root*, with fail-closed identity validation."""
        seen: Dict[int, Dict[str, Any]] = {}
        frontier = [root]
        while frontier:
            pid = frontier.pop()
            if pid in seen:
                continue
            try:
                ident = self.reader.identity(pid)
            except Exception as exc:  # noqa: BLE001 - any failure is fatal
                raise ProcError("identity read failed for %d: %s" % (pid, exc))
            if ident is None:
                continue
            try:
                pg = self.reader.pgid(pid)
            except Exception as exc:  # noqa: BLE001 - fatal, never -1
                raise ProcError("getpgid failed for %d: %s" % (pid, exc))
            if pg < 0:
                # a negative pgid for a still-live process is a resolution
                # failure, not a disappearance.
                try:
                    still = self.reader.identity(pid)
                except Exception as exc:  # noqa: BLE001
                    raise ProcError("identity re-check failed for %d: %s"
                                    % (pid, exc))
                if still is not None and still.get("state") \
                        not in getattr(self.reader, "DEAD_STATES", ("Z",)):
                    raise ProcError("pgid resolution failed for live pid %d"
                                    % pid)
                continue
            ident["own_pgid"] = pg
            seen[pid] = ident
            try:
                kids = self.reader.children(pid)
            except Exception as exc:  # noqa: BLE001
                raise ProcError("children read failed for %d: %s" % (pid, exc))
            frontier.extend(kids)
        return list(seen.values())

    def _group_owned(self, pgid: int, captured_pids: Iterable[int]) -> bool:
        """True only when every member of *pgid* is inside the captured tree."""
        try:
            members = self.reader.members(pgid)
        except Exception as exc:  # noqa: BLE001 - cannot prove ownership
            raise ProcError("group membership read failed for pgid %d: %s"
                            % (pgid, exc))
        return set(members) <= set(captured_pids)

    def reap(self, root: Any) -> Dict[str, Any]:
        """The forced path: SIGKILL the owned tree, root last.

        *root* may be a captured identity dict or a bare pid.  Every failure is
        recorded and returns a non-success teardown result -- cleanup continues
        best effort, but the run is never reported as clean.
        """
        root_pid = root["pid"] if isinstance(root, dict) else int(root)
        result: Dict[str, Any] = {"teardown_failure": False, "killed": [],
                                  "survivors": [], "permission_failure": False,
                                  "errors": []}

        def _fatal(exc: ProcError) -> None:
            result["teardown_failure"] = True
            result["permission_failure"] = True
            result["errors"].append(str(exc))
            result["error"] = str(exc)

        try:
            captured = self._walk(root_pid)
        except ProcError as exc:
            _fatal(exc)
            return result
        root_identity = next((i for i in captured if i["pid"] == root_pid), None)
        deadline = time.monotonic() + self.bound
        stable = 0
        while time.monotonic() < deadline and stable < 2:
            try:
                tree = self._walk(root_pid)
            except ProcError as exc:
                _fatal(exc)
                return result
            ids = {i["pid"] for i in tree}
            if ids == {i["pid"] for i in captured}:
                stable += 1
            else:
                stable = 0
            captured = tree
            pids = {i["pid"] for i in captured}
            # kill descendants (never the root yet) and their owned groups
            for ident in captured:
                if ident["pid"] == root_pid:
                    continue
                try:
                    self._kill(ident, result, pids)
                except ProcError as exc:
                    _fatal(exc)
            time.sleep(0.05)
        # reap the root last
        if root_identity is not None:
            try:
                self._kill(root_identity, result,
                           {i["pid"] for i in captured})
            except ProcError as exc:
                _fatal(exc)
        # verify no captured identity survives (bounded: SIGKILL delivery and
        # zombie reaping are asynchronous)
        poll_deadline = time.monotonic() + self.grace + 2.0
        surviving: List[int] = []
        while True:
            surviving = [i["pid"] for i in captured if self._alive(i)]
            if not surviving or time.monotonic() >= poll_deadline:
                break
            time.sleep(0.05)
        result["survivors"] = surviving
        if result["survivors"] or result["permission_failure"] \
                or result["errors"]:
            result["teardown_failure"] = True
        return result

    def _kill(self, ident: Dict[str, Any], result: Dict[str, Any],
              captured_pids: Iterable[int]) -> None:
        pid = ident["pid"]
        try:
            current = self.reader.identity(pid)
        except Exception as exc:  # noqa: BLE001 - fatal
            raise ProcError("identity re-check failed for %d: %s" % (pid, exc))
        if current is None:
            return
        if current.get("state") in getattr(self.reader, "DEAD_STATES", ("Z",)):
            return
        if current.get("starttime") != ident.get("starttime"):
            # PID reuse / a stale identity: we cannot confirm this is the tree
            # we captured, so we must not signal it -- but that is a
            # teardown failure, never a silent skip.
            result.setdefault("errors", []).append(
                "start-time mismatch for pid %d (stale/PID-reused): not "
                "signalled" % pid)
            return
        pg = ident.get("own_pgid", -1)
        try:
            if pg and pg > 0 and pg != os.getpgrp():
                # Signal a group only when every member belongs to the captured
                # tree; otherwise a mixed group could hold an unrelated process.
                if self._group_owned(pg, captured_pids):
                    self.reader.signal_group(pg, signal.SIGKILL)
                else:
                    self.reader.signal(pid, signal.SIGKILL)
            self.reader.signal(pid, signal.SIGKILL)
            result["killed"].append(pid)
        except PermissionError as exc:
            result["teardown_failure"] = True
            result["permission_failure"] = True
            result.setdefault("errors", []).append(
                "signal permission failure for %d: %s" % (pid, exc))
        except (ProcessLookupError, OSError):
            return

    def _alive(self, ident: Dict[str, Any]) -> bool:
        try:
            current = self.reader.identity(ident["pid"])
        except Exception:  # noqa: BLE001
            return False
        if current is None:
            return False
        if current.get("state") in getattr(self.reader, "DEAD_STATES", ("Z",)):
            return False
        return current.get("starttime") == ident.get("starttime")


# --------------------------------------------------------------------------
# stop controller
# --------------------------------------------------------------------------

class StopController(object):
    """Stop-after-episode: first SIGINT/SIGTERM or stop-file is graceful.

    The bench-owned reasons are persisted verbatim (``bench-stopped-graceful``
    / ``bench-aborted``) so a stop is never mistaken for a game outcome.
    """

    def __init__(self, stop_file: Optional[str] = None):
        self.stop_file = stop_file
        self.level = 0

    def request(self) -> int:
        self.level += 1
        return self.level

    def stop_reason(self) -> Optional[str]:
        if self.level <= 0:
            return None
        if self.level >= 2:
            return M.BENCH_ABORTED
        return M.BENCH_STOPPED_GRACEFUL

    def should_stop(self) -> bool:
        if self.level > 0:
            return True
        if self.stop_file and os.path.exists(self.stop_file):
            self.level = max(1, self.level + 1)
            return True
        return False

    def forced(self) -> bool:
        return self.level >= 2


class signal_handlers(object):
    """Install bench SIGINT/SIGTERM handlers for the duration of a block."""

    def __init__(self, controller: StopController, *,
                 on_forced: Optional[Callable[[], None]] = None):
        self.controller = controller
        self.on_forced = on_forced
        self._saved: Dict[int, Any] = {}

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._saved[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass
        return self

    def __exit__(self, *exc):
        for sig, handler in self._saved.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        return False

    def _handle(self, signum, frame):  # noqa: ARG002
        level = self.controller.request()
        if level >= 2 and self.on_forced is not None:
            self.on_forced()


# --------------------------------------------------------------------------
# manifest (atomic updates)
# --------------------------------------------------------------------------

def write_json_atomic(path: str, obj: Any, mode: int = 0o600) -> None:
    """Write JSON to *path* atomically (temp + rename), at 0600."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def child_env(source: Optional[dict] = None) -> Dict[str, str]:
    """The allowlisted environment for an episode child process.

    Credential-looking names are dropped; the child loads its own referenced
    secret files.  Reuses the provider worker's allowlist so the child and the
    worker share one env policy.
    """
    from . import providers
    return providers.worker_env(source)


def _read_json(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _read_jsonl(path: Optional[str]) -> Optional[List[dict]]:
    """Records from a JSONL sidecar, or ``None`` when the file is absent.

    ``None`` (unavailable) is deliberately distinct from ``[]`` (a present but
    empty source), so fail-closed evidence handling can tell them apart.
    """
    if not path or not os.path.exists(path):
        return None
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return None
    return out


def episode_artifact_paths(episode_dir: str, index: int) -> Dict[str, str]:
    """The per-episode artifact paths the recorder writes."""
    base = os.path.join(episode_dir, "ep-%d" % index)
    return {
        "wire": base + ".wire.jsonl",
        "actions": base + ".actions.jsonl",
        "decisions": base + ".decisions.jsonl",
        "events": base + ".events.jsonl",
        "meta": base + ".meta.json",
        "campaign": os.path.join(episode_dir, "campaign.json"),
    }


def source_hashes_of(paths: Dict[str, str]) -> Dict[str, str]:
    """sha256 of each *present* artifact, keyed by logical name."""
    out: Dict[str, str] = {}
    for name, path in sorted(paths.items()):
        digest = M.sha256_file(path)
        if digest is not None:
            out[name] = digest
    return out


def result_to_meta(result: Any) -> Dict[str, Any]:
    """Normalize an EpisodeResult (or mapping) into the meta shape."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return dict(result)
    fields = ("stop_reason", "outcome", "closed", "eof", "forced_kill",
              "teardown_failure", "unanswered", "recorder_failed",
              "protocol_failure", "failure_reason", "ticks", "needs",
              "invalids", "actions", "returncode", "recording_complete",
              "boundaries", "strategy_calls", "directives_applied", "budget")
    meta = {}
    for name in fields:
        if hasattr(result, name):
            meta[name] = getattr(result, name)
    meta.setdefault("game_outcome", meta.get("outcome"))
    return meta


def forced_search_of(result: Any) -> Optional[Dict[str, Any]]:
    """The forced-search counters from an EpisodeResult, or ``None``.

    ``None`` means the source carries no forced-search evidence, which the
    gate treats as *unavailable* rather than as a measured zero.
    """
    if result is None or not all(
            hasattr(result, "forced_" + f) for f in
            ("activations", "uncleared")):
        return None
    return {
        "activations": getattr(result, "forced_activations", None),
        "suffixes": getattr(result, "forced_suffixes", None),
        "successes": getattr(result, "forced_successes", None),
        "cancels": getattr(result, "forced_cancels", None),
        "denials": getattr(result, "forced_denials", None),
        "trapped": getattr(result, "forced_trapped", None),
        "uncleared": getattr(result, "forced_uncleared", None),
    }


class Manifest(object):
    """The bench campaign manifest with atomic, additive updates."""

    def __init__(self, path: str, *, spec_ref: Optional[str] = None):
        self.path = path
        self.data: Dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA,
            "spec_ref": spec_ref,
            "status": "planned",
            "episodes": [],
            "judge_calls": [],
            "reservations": [],
            "stop_reason": None,
            "actual_tiers": {"reflex": None, "strategy": None, "judge": None},
            "unknown_exposure": [],
            "rollback": None,
        }

    def add_episode(self, entry: dict) -> None:
        self.data["episodes"].append(entry)
        self.flush()

    def reserve(self, key: str, detail: dict) -> None:
        self.data["reservations"].append(dict(detail, key=key,
                                              settled=False))
        self.flush()

    def settle(self, key: str, outcome: str) -> None:
        for entry in self.data["reservations"]:
            if entry["key"] == key and not entry["settled"]:
                entry["settled"] = True
                entry["outcome"] = outcome
        self.flush()

    def record_root(self, key: str, identity: dict) -> None:
        """Persist the captured root identity (PID/start/session/PGID) atomically.

        Written *before* the episode's work runs, so a forced abort always has
        a validated root to reap.
        """
        for entry in self.data["reservations"]:
            if entry["key"] == key and not entry.get("settled"):
                entry["root"] = dict(identity)
        self.flush()

    def unsettled_roots(self) -> List[dict]:
        return [e["root"] for e in self.data["reservations"]
                if not e.get("settled") and e.get("root")]

    def note_unknown_exposure(self, detail: dict) -> None:
        self.data["unknown_exposure"].append(dict(detail))
        self.flush()

    def reconcile(self) -> List[dict]:
        """Move interrupted reservations to unknown exposure (not refunded).

        Resume is opt-in: a reservation that was never settled may still have
        been billed upstream, so it becomes *unknown exposure* rather than a
        refund.  Returns the newly-recorded entries.
        """
        recorded = []
        for entry in self.data["reservations"]:
            if not entry["settled"]:
                entry["settled"] = True
                entry["outcome"] = "unknown-exposure"
                record = {"key": entry["key"], "reason":
                          "interrupted-reservation-not-refunded",
                          "episode": entry.get("episode")}
                self.data["unknown_exposure"].append(record)
                recorded.append(record)
        self.flush()
        return recorded

    def set_status(self, status: str, **extra) -> None:
        self.data["status"] = status
        self.data.update(extra)
        self.flush()

    def flush(self) -> None:
        write_json_atomic(self.path, self.data)


# --------------------------------------------------------------------------
# the runner
# --------------------------------------------------------------------------

#: The repository root, used as the child process working directory so
#: ``python3 -m tools.agent.bench`` resolves.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def default_episode_command(runner, spec_path: str, episode_dir: str,
                            index: int, timeout: float) -> List[str]:
    """The argv that runs exactly one episode in a dedicated child process."""
    paths = runner._paths() or {}
    argv = [sys.executable, "-m", "tools.agent.bench", "_child",
            "--spec", spec_path, "--episode-dir", episode_dir,
            "--timeout", str(timeout),
            "--worker", paths.get("worker", ""),
            "--runner", paths.get("runner", ""),
            "--data", paths.get("data", "")]
    if paths.get("sysconf"):
        argv += ["--sysconf", paths["sysconf"]]
    return argv


def default_episode_runner(config, paths, episode_dir: str, index: int,
                           timeout: float):
    """In-process fallback runner (used only when explicitly injected).

    The production path launches a dedicated child through
    :func:`default_episode_command`; this in-process form is retained for the
    offline tests that must not spawn a real launcher.
    """
    from . import controller as C
    if not paths:
        raise RuntimeError("bench live tier needs BENCH_WORKER/BENCH_RUNNER/"
                           "BENCH_DATA")
    if isinstance(paths, dict):
        paths = C.ControllerPaths(**paths)
    os.makedirs(episode_dir, mode=0o700, exist_ok=True)
    ctl = C.Controller(config, paths, episode_dir, episode_timeout=timeout)
    results = ctl.run_campaign(1)
    result = results[0] if results else None
    return result, episode_dir


class BenchRunner(object):
    """Run a bounded bench campaign, one episode at a time, in isolation.

    A default in-process fake is **not** provided: the dry-run tier performs
    offline scoring only (no provider, no spawn); the live tier needs a real
    worker/launcher.  Tests inject ``episode_runner``.
    """

    def __init__(self, spec: dict, out_dir: str, *,
                 episode_runner: Optional[Callable] = None,
                 episode_command_factory: Optional[Callable] = None,
                 judge_factory: Optional[Callable] = None,
                 stop_file: Optional[str] = None,
                 grace: float = 5.0,
                 spec_path: Optional[str] = None,
                 now: Callable[[], float] = time.monotonic):
        self.spec = spec
        self.out_dir = out_dir
        # ``episode_runner`` is an injected IN-PROCESS runner (offline tests).
        # The production default is a dedicated child process spawned from
        # ``episode_command_factory``.
        self.episode_runner = episode_runner
        self.episode_command_factory = episode_command_factory or \
            default_episode_command
        self.judge_factory = judge_factory
        self.stop = StopController(stop_file)
        self.grace = grace
        self.now = now
        self.manifest = Manifest(os.path.join(out_dir, "manifest.json"),
                                 spec_ref=spec.get("name"))
        self.abort_tree = OwnedProcessTree()
        self.started = now()
        self.plan = plan_allocations(spec, spec["budget"]["max_candidates"]
                                     or 1)
        self.judge = None
        self._prov = None
        self.spec_path = spec_path

    def _spec_path(self) -> str:
        if self.spec_path is None:
            self.spec_path = os.path.join(self.out_dir, "spec.json")
            write_json_atomic(self.spec_path, self.spec)
        return self.spec_path

    # -- provenance --------------------------------------------------------
    def provenance_id(self) -> Optional[str]:
        if self._prov is None:
            try:
                self._prov = M.provenance_manifest(
                    imported_code=M.imported_module_hashes(),
                    versions={"scorecard_schema": M.SCORECARD_SCHEMA,
                              "comparison_policy":
                                  self.spec["comparison"].get(
                                      "metric_policy_version")})
            except Exception:  # noqa: BLE001 - provenance is best-effort
                self._prov = {}
        return self._prov.get("provenance_id")

    def _write_run_artifact(self, name: str, obj: Any) -> str:
        path = os.path.join(self.out_dir, name)
        write_json_atomic(path, obj)
        return path

    def run(self) -> Dict[str, Any]:
        if self.spec["tier"] == DRY_RUN:
            return self.run_dry()
        return self.run_live()

    # -- dry-run tier: offline only, impossible to reach the network -------
    def run_dry(self) -> Dict[str, Any]:
        self.manifest.set_status(
            "running", tier=DRY_RUN,
            actual_tiers={"reflex": "replay", "strategy": "off",
                          "judge": "none"},
            network_calls=0, judge_behavior="not-evaluated")
        cards = []
        for i, wire in enumerate(self.spec.get("replay_inputs") or [], start=1):
            card = M.build_scorecard(
                episode_id="replay-%d" % i, provenance_id=None,
                wire_path=wire if os.path.exists(wire) else None,
                deadline_classification=self.spec["comparison"][
                    "deadline_classification"])
            cards.append(card)
        self.manifest.set_status("complete", tier=DRY_RUN, scorecards=cards,
                                 network_calls=0,
                                 judge_behavior="not-evaluated "
                                 "(dry-run makes no Jev calls)")
        return {"tier": DRY_RUN, "scorecards": cards, "network_calls": 0,
                "judge_behavior": "not-evaluated"}

    # -- live tier ---------------------------------------------------------
    def run_live(self) -> Dict[str, Any]:
        pre = preflight(self.spec)
        if not pre["ok"]:
            self.manifest.set_status("rejected", stage=pre["stage"],
                                     error=pre["error"])
            return {"ok": False, "error": pre["error"], "stage": pre["stage"]}
        config = pre["config"]
        requested = {"reflex": config.reflex, "strategy": config.strategy,
                     "judge": ("jev" if self.spec["judge"]["enabled"]
                               else "none")}
        self.manifest.set_status("running", tier=LIVE,
                                 requested_tiers=requested,
                                 cost_mode=pre.get("effective_cost_mode"),
                                 unattended_apply_allowed=pre.get(
                                     "unattended_apply_allowed"))
        self._unattended = bool(pre.get("unattended_apply_allowed", True))
        # Precommit the comparison design BEFORE any result exists: the policy
        # AND the exact balanced AB/BA schedule the runner will execute.
        planned = self.spec["episodes"]
        self._pre_record = precommit_record(
            self.spec["comparison"], episodes=planned,
            arm_episodes=max(1, planned // 2))
        self._pre_record_hash = self._pre_record["hash"]
        self._schedule = list(self._pre_record["design"]["episode_schedule"])
        self._write_run_artifact("precommit.json", self._pre_record)
        self.judge = self._make_judge(pre)

        results: List[Tuple[int, Any, str]] = []
        cards: List[dict] = []
        judge_results: List[dict] = []
        executed_schedule: List[str] = []
        index = 0
        with signal_handlers(
                self.stop,
                on_forced=lambda: self._force_abort(results)) as _handlers:
            while index < len(self._schedule):
                if self.stop.should_stop():
                    break
                if self._deadline_exceeded():
                    self.manifest.set_status(
                        "partial", stop_reason=M.BENCH_ABORTED,
                        reason="campaign-deadline-exceeded")
                    return self._after_loop(cards, "partial", M.BENCH_ABORTED,
                                            judge_results)
                index += 1
                entry = self._schedule[index - 1]
                arm = entry["arm"]
                executed_schedule.append(
                    entry["order"] if len(entry["order"]) == 2
                    else ("AB" if arm == "baseline" else "BA"))
                episode_dir = os.path.join(self.out_dir, "ep-%d" % index)
                # reserve the WHOLE next episode's approved allocation BEFORE
                # launching it (the per-episode caps reset, so a later episode
                # must not be able to exceed the campaign budget).
                self.manifest.reserve("ep-%d" % index, {
                    "episode": index, "dir": episode_dir, "arm": arm,
                    "pair": entry["pair"], "order": entry["order"],
                    "allocation": {
                        "episode_timeout_s": self.spec["episode_timeout_s"],
                        "strategy_calls": int(getattr(
                            config, "strategy_call_cap", 0) or 0),
                        "judge_dispatches": (1 if self.judge is not None
                                             else 0),
                    }})
                result, episode_dir = self._run_one(config, episode_dir, index)
                card, hashes, paths = self._seal_episode(
                    episode_dir, index, result, requested)
                card["arm"] = arm
                cards.append(card)
                results.append((index, result, episode_dir))
                self.manifest.settle("ep-%d" % index, "completed")
                self.manifest.add_episode({
                    "index": index, "dir": episode_dir, "arm": arm,
                    "pair": entry["pair"], "order": entry["order"],
                    "scorecard_hash": M.sha256_bytes(
                        M.pretty_scorecard(card).encode("utf-8")),
                    "source_hashes": hashes,
                    "integrity": card["integrity"]["status"],
                    "terminal_class": card["terminal_class"],
                    "hard_failure": card["gates"]["hard_failure"],
                })
                judged = self._judge_episode(card, index)
                if judged is not None:
                    judge_results.append(judged)
        self._executed_schedule = executed_schedule
        self.manifest.data["executed_schedule"] = executed_schedule
        self.manifest.flush()
        stop_reason = self.stop.stop_reason()
        status = "complete" if stop_reason is None else "partial"
        return self._after_loop(cards, status, stop_reason, judge_results)

    def _make_judge(self, pre: dict):
        """Construct the advisory judge when enabled and budgeted."""
        if not self.spec["judge"]["enabled"]:
            return None
        calls_total = self.spec["budget"]["judge_calls_total"]
        if calls_total <= 0:
            return None
        if self.judge_factory is not None:
            return self.judge_factory(calls_total, self.spec["judge"])
        from . import bench_judge as J
        return J.BenchJudge(model=self.spec["judge"]["model"],
                            rubric_version=self.spec["judge"]["rubric_version"],
                            deadline_s=self.spec["judge"]["deadline_s"],
                            max_state_bytes=self.spec["judge"]["max_state_bytes"],
                            calls_total=calls_total)

    def _run_one(self, config, episode_dir, index):
        os.makedirs(episode_dir, mode=0o700, exist_ok=True)
        if self.episode_runner is not None:
            # injected in-process runner (offline tests): no child process
            result, episode_dir = self.episode_runner(
                config, self._paths(), episode_dir, index,
                self.spec["episode_timeout_s"])
            return result, episode_dir
        return self._run_child(episode_dir, index)

    def _run_child(self, episode_dir, index):
        """Launch the episode as a dedicated child/session, then supervise it.

        The captured PID/start-time/session/PGID are persisted *before* the
        work runs, so a forced abort always has a validated root to reap.
        """
        key = "ep-%d" % index
        argv = self.episode_command_factory(
            self, self._spec_path(), episode_dir, index,
            self.spec["episode_timeout_s"])
        proc = subprocess.Popen(argv, start_new_session=True, cwd=ROOT,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=child_env())
        try:
            identity = self.abort_tree.reader.identity(proc.pid) \
                or {"pid": proc.pid}
        except Exception:  # noqa: BLE001 - fail closed on capture
            identity = {"pid": proc.pid, "capture_failed": True}
            self.manifest.set_status("teardown-failure",
                                     teardown_failure=True)
        self.manifest.record_root(key, identity)
        self._supervise(key, proc, identity)
        try:
            proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        result = None
        child = _read_json(os.path.join(episode_dir, "bench-child.json"))
        if child:
            result = child.get("result")
        return result, episode_dir

    def _supervise(self, key, proc, identity) -> None:
        """Wait for the child, relaying a graceful stop and enforcing deadlines.

        The wall deadline is enforced **asynchronously during** the episode: a
        second stop request, a campaign/`campaign_timeout_s` deadline or the
        episode timeout aborts the captured tree immediately rather than being
        noticed only between episodes.
        """
        deadline = time.monotonic() + self.spec["episode_timeout_s"]
        grace_deadline = None
        while True:
            if proc.poll() is not None:
                return
            if self.stop.forced():
                self._abort_handle(key, proc, identity, "forced-abort")
                return
            if self._deadline_exceeded():
                self._abort_handle(key, proc, identity,
                                   "campaign-deadline-exceeded")
                return
            if self.stop.should_stop() and grace_deadline is None:
                # relay the FIRST (graceful) stop to the child so it can reach
                # its own ``finally`` teardown.
                try:
                    os.kill(proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                grace_deadline = time.monotonic() + self.grace
            if grace_deadline is not None and time.monotonic() >= grace_deadline:
                self._abort_handle(key, proc, identity, "graceful-stop-timeout")
                return
            if time.monotonic() >= deadline:
                self._abort_handle(key, proc, identity, "episode-timeout")
                return
            time.sleep(0.05)

    def _abort_handle(self, key, proc, identity, reason) -> Dict[str, Any]:
        outcome = self.abort_tree.reap(identity)
        teardown = bool(outcome.get("teardown_failure"))
        self.manifest.note_unknown_exposure(
            {"reason": reason, "root_pid": identity.get("pid"),
             "teardown_failure": teardown})
        if teardown:
            self.manifest.data["teardown_failure"] = True
            self.manifest.set_status("teardown-failure", teardown_failure=True)
        return outcome

    def _seal_episode(self, episode_dir: str, index: int, result: Any,
                      requested: dict) -> Tuple[dict, dict, dict]:
        """Hash the artifacts and build/store the immutable scorecard."""
        paths = episode_artifact_paths(episode_dir, index)
        meta = _read_json(paths["meta"])
        if not meta:
            meta = result_to_meta(result)
        decisions = _read_jsonl(paths["decisions"])
        hashes = source_hashes_of(paths)
        budget = meta.get("budget") if isinstance(meta.get("budget"), dict) \
            else (getattr(result, "budget", {}) if result is not None else {})
        card = M.build_scorecard(
            episode_id="ep-%d" % index, provenance_id=self.provenance_id(),
            source_hashes=hashes, meta=meta, budget=budget or {},
            wire_path=paths["wire"], actions_path=paths["actions"],
            events_path=paths["events"], decisions=decisions,
            forced_search=forced_search_of(result), requested_tiers=requested,
            deadline_classification=self.spec["comparison"][
                "deadline_classification"])
        return card, hashes, paths

    def _judge_episode(self, card: dict, index: int) -> Optional[dict]:
        """One advisory dispatch per *eligible* episode, after sealing."""
        if self.judge is None:
            return None
        if card["integrity"]["status"] == "missing":
            return None
        if card.get("terminal_class") == M.EXCLUDED:
            return None
        try:
            result = self.judge.evaluate(card)
        except Exception as exc:  # noqa: BLE001 - advisory never fatal
            result = {"status": "error", "error": str(exc), "advisory": True}
        self.manifest.data["judge_calls"].append(
            {"episode": index, "status": result.get("status"),
             "dispatches": result.get("dispatches", 0),
             "cache_hit": result.get("cache_hit", False),
             "flags": result.get("flags", [])})
        self.manifest.flush()
        return dict(result, episode=index)

    def _baseline_cards(self) -> List[dict]:
        ref = self.spec.get("baseline_ref")
        if not ref:
            return []
        path = ref if ref.endswith(".json") else os.path.join(
            ref, "scorecards.json")
        loaded = _read_json(path)
        if not loaded:
            return []
        cards = loaded.get("candidate") or loaded.get("cards") or []
        return list(cards)

    def _live_validation(self, cards: Sequence[dict]) -> Dict[str, Any]:
        required = False
        failed = []
        for card in cards:
            req = card["integrity"]["requested_tiers"]
            obs = card["integrity"]["observed_tiers"]
            check = M.live_jev_validation(req, obs)
            if check["required"]:
                required = True
                if not check["ok"]:
                    failed.append({"episode": card["episode_id"],
                                   "reason": check["reason"]})
        return {"required": required, "ok": not failed, "failures": failed}

    def _after_loop(self, cards: List[dict], status: str,
                    stop_reason: Optional[str],
                    judge_results: Sequence[dict]) -> Dict[str, Any]:
        """Comparison, postmortem packaging and tuner stages after the loop."""
        live = self._live_validation(cards)
        scorecards_path = self._write_run_artifact("scorecards.json", {
            "schema_version": "bench-scorecards/1",
            "candidate": cards,
            "precommit": precommit_record(self.spec["comparison"]),
        })
        comparison = self._compare(cards, live)
        comparison_path = self._write_run_artifact("comparison.json",
                                                   comparison)
        postmortem_path = self._package(cards, comparison, live,
                                        judge_results)
        tuning_path = self._tuning_stage(comparison)
        self.manifest.set_status(
            status, stop_reason=stop_reason, episodes_done=len(cards),
            partial=(status != "complete"),
            live_validation=live,
            scorecards=scorecards_path, comparison=comparison_path,
            postmortem=postmortem_path, tuning=tuning_path)
        return {"ok": status == "complete" and live["ok"],
                "status": status, "stop_reason": stop_reason,
                "episodes": len(cards), "live_validation": live,
                "comparison": comparison, "scorecards": cards,
                "postmortem": postmortem_path, "tuning": tuning_path,
                "manifest": self.manifest.data}

    def _compare(self, cards: List[dict], live: dict) -> Dict[str, Any]:
        if not live["ok"]:
            return {"schema_version": M.COMPARISON_SCHEMA, "refused": True,
                    "refusal_reason": "live-tier-validation-failed",
                    "live_validation": live,
                    "admission": {"apply_allowed": False},
                    "verdict": "not-comparable"}
        baseline = self._baseline_cards()
        by_arm_base = [c for c in cards if c.get("arm") == "baseline"]
        by_arm_cand = [c for c in cards if c.get("arm") == "candidate"]
        if by_arm_base and by_arm_cand:
            # an interleaved A/B run carries both arms in this run
            base_arm, cand_arm = by_arm_base, by_arm_cand
        elif by_arm_cand:
            base_arm, cand_arm = baseline, by_arm_cand
        else:
            base_arm, cand_arm = baseline, list(cards)
        if not base_arm:
            return {"schema_version": M.COMPARISON_SCHEMA, "refused": True,
                    "refusal_reason": "no-baseline",
                    "admission": {"apply_allowed": False},
                    "verdict": "inconclusive"}
        policy = dict(self.spec["comparison"])
        policy["admission_margin"] = 0.0
        pre = getattr(self, "_pre_record", None) or precommit_record(
            self.spec["comparison"], episodes=self.spec["episodes"],
            arm_episodes=max(1, self.spec["episodes"] // 2))
        result = M.compare_arms(
            base_arm, cand_arm, policy,
            base_provenance=None, cand_provenance=self._prov,
            precommit_design=pre.get("design"),
            precommit_hash=pre.get("hash"),
            observed_schedule=getattr(self, "_executed_schedule", None))
        result["design"] = pre
        if not getattr(self, "_unattended", True):
            # an unknown-exposure (unstrict) run never permits unattended apply
            result.setdefault("admission", {})["apply_allowed"] = False
            result["unattended_apply_allowed"] = False
            result.setdefault("reasons", []).append(
                "cost-mode:operator-approved-unknown (no unattended apply)")
        return result

    def _package(self, cards: List[dict], comparison: dict, live: dict,
                 judge_results: Sequence[dict]) -> Optional[str]:
        hard = [c["episode_id"] for c in cards
                if c["gates"]["hard_failure"]]
        flags = [j for j in judge_results if j.get("flags")]
        verdict = comparison.get("verdict")
        needs = bool(hard) or not live["ok"] or bool(flags) or \
            verdict in ("fail", "inconclusive") or comparison.get("refused")
        if not needs:
            return None
        excerpt_paths = {}
        anchors = {}
        for card in cards:
            if not card["gates"]["hard_failure"]:
                continue
            name = card["episode_id"]
            wire = os.path.join(self.out_dir, name, name + ".wire.jsonl")
            decisions = os.path.join(self.out_dir, name,
                                     name + ".decisions.jsonl")
            if os.path.exists(wire):
                excerpt_paths["%s.wire" % name] = wire
            if os.path.exists(decisions):
                excerpt_paths["%s.decisions" % name] = decisions
        pkg = M.build_postmortem_package(
            out_dir=self.out_dir, failure_kind="bench-gate-or-regression",
            failed_gates=sorted({g for c in cards
                                 for g, v in c["gates"].items()
                                 if v is True and g.endswith("hard_failure")}),
            comparison=comparison, scorecards=cards,
            judge_answers=list(judge_results),
            artifact_paths={c["episode_id"]: os.path.join(
                self.out_dir, c["episode_id"]) for c in cards},
            excerpt_paths=excerpt_paths, excerpt_anchors=anchors,
            spec_ref=self.spec.get("name"))
        return pkg.get("written_to")

    def _tuning_stage(self, comparison: dict) -> str:
        report = tuner_plan(self.spec)
        report["comparison_verdict"] = comparison.get("verdict")
        report["apply_allowed"] = bool(
            (comparison.get("admission") or {}).get("apply_allowed"))
        return self._write_run_artifact("tuning-report.json", report)

    def _paths(self):
        """Resolve the worker/runner/data paths from the environment.

        The bench never stores a path in the spec; the operator supplies the
        built launcher and worker through the standard ``BENCH_*`` variables.
        """
        worker = os.environ.get("BENCH_WORKER")
        runner = os.environ.get("BENCH_RUNNER")
        data = os.environ.get("BENCH_DATA")
        if not (worker and runner and data):
            return None
        return {"worker": worker, "runner": runner, "data": data,
                "sysconf": os.environ.get("BENCH_SYSCONF") or None}

    def _deadline(self) -> float:
        """The campaign wall budget: the stricter of the two declared limits."""
        limits = []
        for key in ("max_total_wall_s", "campaign_timeout_s"):
            value = self.spec["budget"].get(key) \
                if key == "max_total_wall_s" else self.spec.get(key)
            if value:
                limits.append(float(value))
        return min(limits) if limits else 0.0

    def _deadline_exceeded(self) -> bool:
        limit = self._deadline()
        if not limit:
            return False
        return (self.now() - self.started) > limit

    def _force_abort(self, results) -> None:
        # forced: reap the owned tree of every captured episode root
        teardown_failed = False
        for root in list(self.manifest.unsettled_roots()) + [
                {"pid": e["root_pid"]}
                for e in self.manifest.data["episodes"]
                if e.get("root_pid")]:
            outcome = self.abort_tree.reap(root)
            if outcome.get("teardown_failure"):
                teardown_failed = True
        self.manifest.note_unknown_exposure(
            {"reason": "forced-abort", "episodes": len(results)})
        if teardown_failed:
            self.manifest.data["teardown_failure"] = True
            self.manifest.set_status("teardown-failure", teardown_failure=True)


# --------------------------------------------------------------------------
# baseline promotion (a partial run is never promoted)
# --------------------------------------------------------------------------

def promote_baseline(*, run_status: str, comparison: Optional[dict],
                     teardown_failure: bool = False) -> Dict[str, Any]:
    """Promote a run to the versioned baseline only when it qualifies.

    Requires a complete run, no teardown failure, and a comparison whose
    admission allows apply.  A partial or interrupted run is refused.
    """
    reasons: List[str] = []
    if run_status != "complete":
        reasons.append("run-not-complete:%s" % run_status)
    if teardown_failure:
        reasons.append("teardown-failure")
    if comparison is None:
        reasons.append("no-comparison")
    elif comparison.get("verdict") != "pass":
        reasons.append("comparison-not-pass:%s" % comparison.get("verdict"))
    elif not (comparison.get("admission") or {}).get("apply_allowed"):
        reasons.append("admission-not-applied")
    return {"promoted": not reasons, "reasons": reasons}


# --------------------------------------------------------------------------
# §6 tuner (report-only coordinate search; apply only with approval)
# --------------------------------------------------------------------------

def tuner_candidates(spec: dict) -> List[Dict[str, Any]]:
    """The finite, deterministic coordinate-search candidate list.

    One parameter at a time, deterministic order, at most the spec's knob count;
    frozen knobs are never proposed.
    """
    params = spec["tuning"]["parameters"]
    candidates: List[Dict[str, Any]] = []
    for knob in TUNER_ELIGIBLE_KNOBS:
        rail = params.get(knob)
        if not rail:
            continue
        for value in rail["grid"]:
            candidates.append({knob: value})
    return candidates[: max(1, spec["budget"]["max_candidates"])]


def tuner_plan(spec: dict) -> Dict[str, Any]:
    """Reserve the screening and confirmation budgets before searching."""
    candidates = tuner_candidates(spec)
    alloc = plan_allocations(spec, len(candidates))
    return {"candidates": candidates, "n_candidates": len(candidates),
            "screening_episodes_per_arm":
                spec["comparison"]["screening_episodes_per_arm"],
            "confirmation_episodes_per_arm":
                spec["comparison"]["confirmation_episodes_per_arm"],
            "allocations": alloc,
            "sweeps": min(2, max(1, len(candidates))),
            "finite": True}


def tuner_report(spec: dict, results: Sequence[dict]) -> Dict[str, Any]:
    """Rank candidates by a deterministic objective; report-only."""
    ranked = []
    for item in results:
        comp = item.get("comparison") or {}
        ranked.append({
            "candidate": item.get("candidate"),
            "verdict": comp.get("verdict"),
            "target": (comp.get("per_metric") or {}).get(
                comp.get("target_metric", ""), {}).get("effect"),
            "admission": (comp.get("admission") or {}).get("apply_allowed"),
        })
    ranked.sort(key=lambda r: (
        0 if r["verdict"] == "pass" else 1,
        -(r["target"] if isinstance(r["target"], (int, float)) else -1e9)))
    return {"schema_version": M.TUNING_SCHEMA, "mode": spec["tuning"]["mode"],
            "ranked": ranked, "report_only": True}


def _config_overlay(values: Dict[str, Any]) -> Dict[str, Any]:
    """A nonsecret overlay: only tuner knobs, never a credential reference."""
    secret_keys = {"deepseek_key_file", "jev_key_file", "jev_accept_terms"}
    return {k: v for k, v in values.items()
            if k not in secret_keys and k in TUNER_ELIGIBLE_KNOBS}


def _overlay_files(overlay_dir: str) -> List[int]:
    """The versions of existing immutable overlay files, ascending."""
    if not os.path.isdir(overlay_dir):
        return []
    out = []
    for name in os.listdir(overlay_dir):
        if name.startswith("config-overlay-v") and name.endswith(".json"):
            try:
                out.append(int(name[len("config-overlay-v"):-len(".json")]))
            except ValueError:
                continue
    return sorted(out)


def _check_approval(approval: Optional[dict], now: float) -> List[str]:
    """Validate a *verified* approval object; ``None``/omitted always fails."""
    reasons: List[str] = []
    if not isinstance(approval, dict):
        return ["no-verified-approval"]
    if not approval.get("id"):
        reasons.append("approval-missing-id")
    if not approval.get("authorization_hash"):
        reasons.append("approval-missing-authorization-hash")
    if not isinstance(approval.get("authorized"), dict) or \
            not approval["authorized"]:
        reasons.append("approval-missing-authorized-ranges")
    expiry = approval.get("expiry")
    if not _finite(expiry):
        reasons.append("approval-missing-expiry")
    elif float(expiry) <= now:
        reasons.append("approval-expired")
    return reasons


def apply_approved(spec: dict, *, candidate: dict, confirmation: dict,
                   config_hash: str, current_config: dict,
                   overlay_dir: str, approval: Optional[dict] = None,
                   now: Optional[Callable[[], float]] = None) -> Dict[str, Any]:
    """Write a versioned nonsecret overlay -- only after every precondition.

    Preconditions (ALL required):
      * a **verified approval object** (id, authorization hash + ranges, and an
        expiry in the future relative to trusted time) -- ``None`` fails;
      * a fresh confirmation that passed and whose admission allows apply;
      * the baseline config hash matches;
      * every changed key/value is inside its exact *authorized* grid/range.

    Overlays are immutable and versioned: two applies create two distinct
    files (overwriting is refused).  The apply record persists the exact
    before/after diff, confirmation evidence/run ids, the approval expiry and
    authorization hash, and the previous active reference.  Never rewrites a
    secret config file.
    """
    tuning = spec["tuning"]
    now = now or time.time
    reasons: List[str] = []
    if tuning["mode"] != "apply-approved":
        reasons.append("mode-not-apply-approved")
    reasons.extend(_check_approval(approval, float(now())))
    if (confirmation or {}).get("verdict") != "pass":
        reasons.append("confirmation-not-pass")
    if not (confirmation or {}).get("admission", {}).get("apply_allowed"):
        reasons.append("admission-not-applied")
    if tuning.get("expected_base_config_hash") != config_hash:
        reasons.append("config-hash-mismatch")
    # exact key/range compliance against the APPROVAL's authorized ranges
    authorized = (approval or {}).get("authorized") or {}
    for knob, value in (candidate or {}).items():
        rail = tuning["parameters"].get(knob)
        auth = authorized.get(knob)
        if knob not in TUNER_ELIGIBLE_KNOBS or not rail or not auth:
            reasons.append("unauthorized-key:%s" % knob)
            continue
        if value not in rail["grid"]:
            reasons.append("value-out-of-grid:%s=%r" % (knob, value))
        grid = auth.get("grid")
        if grid is not None and value not in grid:
            reasons.append("value-outside-approval:%s=%r" % (knob, value))
        if auth.get("min") is not None and value < auth["min"]:
            reasons.append("value-below-approval:%s" % knob)
        if auth.get("max") is not None and value > auth["max"]:
            reasons.append("value-above-approval:%s" % knob)
    if reasons:
        return {"applied": False, "reasons": reasons}

    version = max(_overlay_files(overlay_dir) + [0]) + 1
    os.makedirs(overlay_dir, mode=0o700, exist_ok=True)
    path = os.path.join(overlay_dir, "config-overlay-v%d.json" % version)
    if os.path.exists(path):
        return {"applied": False, "reasons": ["overlay-overwrite-refused"]}
    merged = dict(current_config)
    merged.update(candidate)
    overlay = _config_overlay(merged)
    before = {k: current_config.get(k) for k in candidate}
    after = {k: candidate[k] for k in candidate}
    write_json_atomic(path, {"schema_version": "config-overlay/1",
                             "version": version,
                             "overlay": overlay})
    pointer = os.path.join(overlay_dir, "active.json")
    previous = None
    if os.path.exists(pointer):
        previous = _read_json(pointer) or {}
        previous = previous.get("active")
    record = {
        "schema_version": "bench-apply-record/1",
        "overlay_path": path,
        "version": version,
        "before": before,
        "after": after,
        "diff": {"%s" % k: {"before": before[k], "after": after[k]}
                 for k in sorted(after)},
        "approval_id": (approval or {}).get("id"),
        "approval_expiry": (approval or {}).get("expiry"),
        "authorization_hash": (approval or {}).get("authorization_hash"),
        "confirmation_run_ids": list(
            (confirmation or {}).get("run_ids")
            or (confirmation or {}).get("evidence") or []),
        "previous_active": previous,
        "base_config_hash": config_hash,
    }
    record_path = os.path.join(overlay_dir,
                               "apply-record-v%d.json" % version)
    write_json_atomic(record_path, record)
    write_json_atomic(pointer, {"active": path, "previous": previous,
                                "version": version,
                                "record": record_path})
    return {"applied": True, "overlay_path": path, "version": version,
            "rollback": previous, "overlay": overlay,
            "record": record_path, "diff": record["diff"]}


def rollback_apply(overlay_dir: str) -> Dict[str, Any]:
    """Restore the previous active overlay (content, not just a path).

    Returns the restored path and its exact bytes so a caller can verify the
    rollback restored the earlier overlay byte-for-byte.
    """
    pointer = os.path.join(overlay_dir, "active.json")
    state = _read_json(pointer)
    if not state or not state.get("previous"):
        return {"rolled_back": False, "reason": "no-previous-overlay"}
    restored = state["previous"]
    with open(restored, "rb") as fh:
        content = fh.read()
    write_json_atomic(pointer, {"active": restored,
                                "previous": None,
                                "rolled_back_from": state.get("active"),
                                "version": state.get("version")})
    return {"rolled_back": True, "active": restored,
            "content_sha256": M.sha256_bytes(content),
            "content": content.decode("utf-8")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _cmd_validate(args) -> int:
    spec = load_spec(args.spec)
    report = preflight(spec)
    print(json.dumps({k: v for k, v in report.items() if k != "config"},
                     indent=2, sort_keys=True, default=str))
    return 0 if report["ok"] else 2


def _cmd_score(args) -> int:
    spec = load_spec(args.spec)
    runner = BenchRunner(spec, args.out_dir or ".")
    result = runner.run_dry()
    print(json.dumps({"scorecards": len(result["scorecards"])}, indent=2))
    return 0


def _cmd_run(args) -> int:
    spec = load_spec(args.spec)
    out_dir = args.out_dir or ("bench-run-" + str(int(time.time())))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    runner = BenchRunner(spec, out_dir)
    result = runner.run()
    print(json.dumps({k: v for k, v in result.items() if k != "scorecards"},
                     indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok", True) else 1


def _cmd_compare(args) -> int:
    """Compare a run's candidate scorecards against a baseline, write JSON."""
    spec = load_spec(args.spec) if args.spec else None
    run_dir = args.run_dir or args.out_dir or ("." if not args.spec else None)
    if not run_dir:
        print("error: compare needs --run-dir", file=sys.stderr)
        return 2
    loaded = _read_json(os.path.join(run_dir, "scorecards.json"))
    if not loaded:
        print("error: no scorecards.json under %s" % run_dir, file=sys.stderr)
        return 2
    candidate = list(loaded.get("candidate") or loaded.get("cards") or [])
    baseline_dir = args.baseline_dir
    baseline = []
    if baseline_dir:
        base_loaded = _read_json(os.path.join(baseline_dir, "scorecards.json"))
        baseline = list((base_loaded or {}).get("candidate")
                        or (base_loaded or {}).get("cards") or [])
    if spec is None:
        policy = dict(loaded.get("precommit", {}).get("policy") or {})
    else:
        policy = dict(spec["comparison"])
    policy.setdefault("admission_margin", 0.0)
    if not baseline:
        result = {"schema_version": M.COMPARISON_SCHEMA, "refused": True,
                  "refusal_reason": "no-baseline",
                  "admission": {"apply_allowed": False},
                  "verdict": "inconclusive"}
    else:
        result = M.compare_arms(baseline, candidate, policy)
        if spec is not None:
            result["design"] = precommit_record(spec["comparison"])
    out = os.path.join(run_dir, "comparison.json")
    write_json_atomic(out, result)
    print(json.dumps({"verdict": result.get("verdict"), "written": out},
                     indent=2, sort_keys=True))
    return 0 if result.get("verdict") else 1


def _cmd_tune(args) -> int:
    """Emit the finite tuner plan/report and write the artifact."""
    spec = load_spec(args.spec)
    report = tuner_plan(spec)
    if args.run_dir:
        results = []
        loaded = _read_json(os.path.join(args.run_dir, "scorecards.json"))
        if loaded:
            results = [{"candidate": None,
                        "comparison": _read_json(os.path.join(
                            args.run_dir, "comparison.json")) or {}}]
        report["ranked"] = tuner_report(spec, results)["ranked"]
    else:
        report["ranked"] = []
    report["mode"] = spec["tuning"]["mode"]
    out_dir = args.out_dir or args.run_dir or "."
    out = os.path.join(out_dir, "tuning-report.json")
    write_json_atomic(out, report)
    print(json.dumps({"n_candidates": report["n_candidates"], "written": out},
                     indent=2, sort_keys=True))
    return 0


def _cmd_package(args) -> int:
    """Assemble the bounded postmortem package from a run directory."""
    run_dir = args.run_dir or args.out_dir
    if not run_dir:
        print("error: package needs --run-dir", file=sys.stderr)
        return 2
    loaded = _read_json(os.path.join(run_dir, "scorecards.json"))
    cards = list((loaded or {}).get("candidate")
                 or (loaded or {}).get("cards") or [])
    comparison = _read_json(os.path.join(run_dir, "comparison.json"))
    excerpt_paths: Dict[str, str] = {}
    artifact_paths: Dict[str, str] = {}
    for card in cards:
        name = card.get("episode_id")
        if not name:
            continue
        ep_dir = os.path.join(run_dir, name)
        artifact_paths[name] = ep_dir
        for label in ("wire", "decisions", "events", "actions"):
            path = os.path.join(ep_dir, "%s.%s.jsonl" % (name, label))
            if os.path.exists(path):
                excerpt_paths["%s.%s" % (name, label)] = path
    pkg = M.build_postmortem_package(
        out_dir=run_dir,
        failure_kind=args.failure_kind or "operator-requested",
        failed_gates=sorted({g for c in cards
                             for g, v in (c.get("gates") or {}).items()
                             if v is True and g.endswith("hard_failure")}),
        comparison=comparison, scorecards=cards,
        artifact_paths=artifact_paths, excerpt_paths=excerpt_paths)
    print(json.dumps({"written": pkg.get("written_to"),
                      "excerpts": sorted(pkg["excerpts"])},
                     indent=2, sort_keys=True))
    return 0


def _cmd_child(args) -> int:
    """Run exactly one controller episode in its own process/session."""
    from . import controller as C
    spec = load_spec(args.spec)
    config, err = resolve_provider_config(spec)
    if err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    paths = {"worker": args.worker, "runner": args.runner, "data": args.data,
             "sysconf": args.sysconf or None}
    ctl = C.Controller(config, C.ControllerPaths(**paths), args.episode_dir,
                       episode_timeout=args.timeout)
    results = ctl.run_campaign(1)
    result = result_to_meta(results[0] if results else None)
    write_json_atomic(os.path.join(args.episode_dir, "bench-child.json"),
                      {"ok": True, "result": result})
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.agent.bench", description=__doc__)
    sub = ap.add_subparsers(dest="command")
    for name in ("validate", "run", "score", "compare", "tune", "package"):
        p = sub.add_parser(name)
        p.add_argument("spec", nargs="?")
        p.add_argument("--out-dir", default=None)
        p.add_argument("--run-dir", default=None)
        p.add_argument("--baseline-dir", default=None)
        p.add_argument("--failure-kind", default=None)
    child = sub.add_parser("_child")
    for flag in ("--spec", "--worker", "--runner", "--data", "--sysconf",
                 "--episode-dir", "--timeout"):
        child.add_argument(flag, required=flag in ("spec", "worker", "runner",
                                                   "data", "episode-dir"))
    args = ap.parse_args(argv)
    handlers = {"validate": _cmd_validate, "run": _cmd_run,
                "score": _cmd_score, "compare": _cmd_compare,
                "tune": _cmd_tune, "package": _cmd_package,
                "_child": _cmd_child}
    handler = handlers.get(args.command)
    if handler is None:
        ap.print_help()
        return 2
    if args.command in ("validate", "run", "score", "tune") and not args.spec:
        print("error: a spec path is required", file=sys.stderr)
        return 2
    if args.command == "tune":
        args.run_dir = args.run_dir or args.out_dir
    if args.command == "_child":
        args.timeout = float(args.timeout or 300.0)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
