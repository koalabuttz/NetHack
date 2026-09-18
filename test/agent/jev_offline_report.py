#!/usr/bin/env python3
"""Offline Jev presentation comparison harness (no network).

For every fixture in the Phase-0 paired corpus, this harness:

  * re-renders the **new semantic-wire request** from the fixture's frozen
    ``retained_table`` + ``frozen_context`` (the only renderer inputs);
  * reads the committed **legacy raw request body** beside it;
  * materializes both the old positional-key (``opt-N``) and the new semantic
    key response bodies from the fixture's ``canned_response`` (which is
    expressed wholly in retained-index terms, so nothing in the fixture
    references an old ID directly), parses each with its own parser, and maps
    the result back to a retained index; and
  * reports the byte counts, the refusal tallies and the parser-level
    selection agreement.

Usage (exactly, from the repository root):

    python3 test/agent/jev_offline_report.py \\
        --fixtures test/agent/fixtures/jev_legacy_requests \\
        --out test/agent/fixtures/jev_offline_report.json

The report is deterministic and contains **no wall-clock field**: latency,
real token counts, cost, distribution shapes and gameplay survival are
live-only, operator-approved metrics and are deliberately not automatable
here.  Serialized byte counts are reported as *bytes* and are never converted
to tokens (there is no Jev tokenizer in this repository).

Besides the report it writes the committed **paired** artifact for every
fixture under ``<fixtures>/paired/``: the exact new semantic-wire body, or a
zero-byte file for a fixture the renderer refuses (a refusal carries no wire
body; its code lives in the report).  Both artifacts are byte-for-byte
deterministic across runs.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import jev_fixtures as jf  # noqa: E402
from tools.agent import providers  # noqa: E402

SCHEMA_VERSION = 1

#: The committed report artifact this harness generates.
DEFAULT_REPORT = os.path.join(_HERE, "fixtures", "jev_offline_report.json")

#: The committed paired-artifact directory, relative to the fixtures root: one
#: file per manifest fixture, holding that fixture's deterministic new
#: semantic-wire body.  A fixture the renderer refuses has no body, so its
#: artifact is a zero-byte file (the refusal code lives in the report, never in
#: the paired body).
PAIRED_DIR = "paired"

#: Field names that would make the report non-deterministic.  A wall-clock
#: value anywhere in the artifact would break byte-for-byte reproducibility,
#: so their presence is a defect rather than a formatting choice.
FORBIDDEN_FIELDS = ("latency", "wall", "wallclock", "timestamp", "elapsed",
                    "duration", "generated_at", "started", "finished", "t")


def default_fixtures():
    """The paired Phase-0 corpus root (the harness's default input)."""
    return jf.default_root()


def legacy_keys(count):
    """The pre-migration positional key namespace for *count* members."""
    return ["opt-%d" % i for i in range(count)]


def retained_probabilities(canned, count):
    """The retained-index probability vector, or ``None`` when absent."""
    probs = canned.get("probabilities") or []
    if len(probs) != count:
        return None
    return [float(p) for p in probs]


def materialize_legacy_response(canned, count):
    """An old positional-key response body, built from retained indices."""
    keys = legacy_keys(count)
    probs = {key: value
             for key, value in zip(keys, canned["probabilities"])}
    index = canned["chosen_retained_index"]
    action = {"type": "choice", "choice": keys[index], "probabilities": probs,
              "confidence": canned["confidence"]}
    return {"model": "jev-latest", "answers": {"action": action},
            "usage": dict(canned["usage"])}


def materialize_semantic_response(canned, option_keys):
    """A new semantic-key response body, built from retained indices."""
    probs = {key: value
             for key, value in zip(option_keys, canned["probabilities"])}
    index = canned["chosen_retained_index"]
    action = {"type": "choice", "choice": option_keys[index],
              "probabilities": probs, "confidence": canned["confidence"]}
    return {"model": "jev-latest", "answers": {"action": action},
            "usage": dict(canned["usage"])}


def parse_legacy(body, keys):
    """The pre-migration parser: a positional ``opt-N`` key maps to N.

    Replicated here because the migrated adapter no longer offers positional
    keys at all; the rules (explicit ``type == "choice"``, an offered key, a
    complete normalized distribution, a maximal selection) are unchanged.
    """
    answer = providers._jev_answer_of(body)
    if answer is None:
        return None
    action = answer.get("action")
    if not isinstance(action, dict) or action.get("type") != "choice":
        return None
    choice = action.get("choice", answer.get("choice"))
    if not isinstance(choice, str) or choice not in keys:
        return None
    probs = action.get("probabilities", answer.get("probabilities"))
    ok, _why = providers._check_probabilities(probs, keys)
    if not ok:
        return None
    if float(probs[choice]) < max(float(v) for v in probs.values()):
        return None
    return keys.index(choice)


def parse_semantic(body, key_index):
    """The current parser, exercised through the real adapter code path."""
    adapter = providers.JevReflex(providers.ProviderConfig(reflex="jev"),
                                  jev_dispatch_enabled=True)
    built = providers.PreparedJevRequest(
        table_id="offline", need_key=(), table_version=1, prompt="",
        state={}, criteria={}, key_index=dict(key_index), payload={})
    result = providers.WorkerResult(ok=True, json=body)
    choice = adapter._choice_from(result, built)
    if choice is None or choice.index is None:
        return None
    return int(choice.index)


def _request_bytes(payload):
    """The exact bytes the worker would POST for *payload*."""
    return json.dumps(payload).encode("utf-8")


def paired_path(root, name):
    """The committed paired new-wire artifact for one fixture name."""
    return os.path.join(root, PAIRED_DIR, name + ".json")


def render_new_wire(entry):
    """The deterministic paired representation of one fixture's new request.

    Returns ``(body, option_keys, key_index, refusal)``.  The body is the
    exact bytes the worker would POST; for a fixture the renderer refuses, the
    body is the **empty** byte string, because a refusal carries no wire body
    (its code is recorded in the report).  This is the single source of truth
    for both the report's ``bytes_new`` and the committed paired artifact.
    """
    context = jf.unfreeze_context(entry["frozen_context"])
    context.prepared = jf.prepared_from_frozen(entry["retained_table"])
    adapter = providers.JevReflex(providers.ProviderConfig(reflex="jev"),
                                  jev_dispatch_enabled=True)
    build = adapter.build_request(context)
    if build.request is None:
        return (b"", [], {}, build.refusal or "")
    return (_request_bytes(build.request.payload),
            list(build.request.key_index),
            dict(build.request.key_index), "")


def write_paired(root, manifest=None):
    """Write the paired new-wire artifact for every manifest fixture.

    One file per fixture under ``<root>/paired/``; byte-for-byte deterministic
    so a re-run reproduces the committed artifacts exactly.
    """
    manifest = manifest if manifest is not None else jf.load_manifest(root)
    out_dir = os.path.join(root, PAIRED_DIR)
    os.makedirs(out_dir, exist_ok=True)
    for name in jf.fixture_names(manifest):
        body = render_new_wire(manifest["fixtures"][name])[0]
        with open(paired_path(root, name), "wb") as handle:
            handle.write(body)


def _fallback_category(outcome):
    if outcome.startswith(jf.OUTCOME_FALLBACK):
        return outcome[len(jf.OUTCOME_FALLBACK):]
    return None


def analyse(name, entry, manifest_root):
    table = jf.unfreeze_table(entry["retained_table"])
    canned = entry["canned_response"]
    count = len(table)

    # -- the new semantic-wire request, re-rendered from the frozen inputs
    new_bytes, option_keys, key_index, refusal = render_new_wire(entry)

    # -- the legacy raw body, exactly as committed
    legacy_path = entry["legacy_body"]
    if legacy_path:
        with open(os.path.join(manifest_root, legacy_path), "rb") as handle:
            legacy_bytes = handle.read()
    else:
        legacy_bytes = b""

    # -- parser-level selection on both key namespaces
    legacy_index = None
    if legacy_bytes:
        legacy_index = parse_legacy(materialize_legacy_response(canned, count),
                                    legacy_keys(count))
    frozen_legacy = entry["expected_legacy_parser_selected_index"]
    if legacy_index != frozen_legacy:
        raise SystemExit("%s: legacy parser selected %r, fixture froze %r"
                         % (name, legacy_index, frozen_legacy))

    new_index = None
    if option_keys and retained_probabilities(canned, count) is not None:
        body = materialize_semantic_response(canned, option_keys)
        new_index = parse_semantic(body, key_index)
    if new_index != frozen_legacy:
        raise SystemExit("%s: semantic parser selected %r, fixture froze %r"
                         % (name, new_index, frozen_legacy))

    usage = dict(canned.get("usage") or {})
    return {
        "fixture": name,
        "bytes_new": len(new_bytes),
        "bytes_legacy": len(legacy_bytes),
        "refusal_codes": ({refusal: 1} if refusal else {}),
        "selected_retained_indices": {"legacy": legacy_index,
                                      "new": new_index},
        "fallback_category": _fallback_category(entry["expected_outcome"]),
        "canned_usage": {"input_tokens": usage.get("input_tokens"),
                         "output_tokens": usage.get("output_tokens"),
                         "synthetic": True},
        "synthetic": True,
    }


def build_report(root):
    manifest = jf.load_manifest(root)
    problems = jf.validate_manifest(manifest)
    if problems:
        raise SystemExit("manifest schema invalid:\n  " + "\n  ".join(problems))
    per_request = []
    for name in jf.fixture_names(manifest):
        per_request.append(analyse(name, manifest["fixtures"][name], root))

    tallies = {}
    total_new = 0
    total_legacy = 0
    agreed = 0
    paired = 0
    for record in per_request:
        total_new += record["bytes_new"]
        total_legacy += record["bytes_legacy"]
        for code, n in record["refusal_codes"].items():
            tallies[code] = tallies.get(code, 0) + n
        legacy = record["selected_retained_indices"]["legacy"]
        new = record["selected_retained_indices"]["new"]
        if legacy is not None and new is not None:
            paired += 1
            if legacy == new:
                agreed += 1
    agreement_rate = (float(agreed) / paired) if paired else None
    return {
        "schema_version": SCHEMA_VERSION,
        "per_request": per_request,
        "summary": {
            "total_bytes_new": total_new,
            "total_bytes_legacy": total_legacy,
            "refusal_tallies": tallies,
            "agreement_rate": agreement_rate,
        },
    }


def render(report):
    return json.dumps(report, indent=1, sort_keys=True) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="offline Jev presentation comparison (no network)")
    parser.add_argument("--fixtures", default=jf.default_root())
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report = build_report(args.fixtures)
    text = render(report)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    # The paired new-wire artifact for every fixture is part of the Phase D
    # contract and is committed beside the corpus; write it deterministically.
    write_paired(args.fixtures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
