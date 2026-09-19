#!/usr/bin/env python3
"""Regenerate ``test/agent/fixtures/jev_golden_request.json``.

The golden fixture pins the exact request bytes a Jev presentation emits, plus
an explicit *clean-tree* provenance claim: the recorded commit must be a commit
whose ``tools/agent/presentation.py`` emits the declared
``PRESENTATION_VERSION`` (plan §6 Phase 3, ``dirty: false``).

Run from a clean tree (the capture script must already be committed, so the
working tree is not dirty when the provenance is recorded), then commit the
regenerated fixture:

    python3 test/agent/jev_golden_capture.py
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_auto_providers as T  # noqa: E402
from tools.agent import providers  # noqa: E402

GOLDEN = os.path.join(HERE, "fixtures", "jev_golden_request.json")


def capture():
    """One real (local fake-endpoint) request; returns ``(raw_bytes, path)``."""
    ep = T.FakeEndpoint()
    try:
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "jev-wire-secret"}):
            cfg = providers.ProviderConfig(
                reflex="jev", jev_accept_terms=True, reflex_deadline=2.0,
                jev_base_url=ep.base_url)
            prov = providers.JevReflex(cfg, jev_dispatch_enabled=True)
            ctx = T.wire_ctx()
            probs = {T.WIRE_KEYS[0]: 0.1, T.WIRE_KEYS[1]: 0.2,
                     T.WIRE_KEYS[2]: 0.7}
            body = T.jev_answer(T.WIRE_KEYS[2], probs,
                                usage={"input_tokens": 10, "output_tokens": 2})
            ep.responder = lambda path, b: (200, json.dumps(body).encode())
            res = prov.decide(ctx, time.monotonic() + 2.0)
            assert res is not None and res.index == 2, res
            return ep.requests[-1]["body"], ep.requests[-1]["path"]
    finally:
        ep.close()


def _git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                          text=True).stdout.strip()


def main():
    raw, path = capture()
    sent = json.loads(raw)
    with open(GOLDEN) as fh:
        golden = json.load(fh)
    commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain"))
    golden["request_body"] = sent
    golden["request_body_sha256"] = hashlib.sha256(raw).hexdigest()
    golden["option_keys"] = list(
        sent["questions"]["action"]["criteria"].keys())
    golden["instructions"] = sent["questions"]["action"]["instructions"]
    golden["legend"] = sent["state"]["legend"]
    capture = golden.setdefault("capture", {})
    capture["presentation_version"] = providers.JEV_PRESENTATION_VERSION
    capture["adapter_version"] = getattr(providers, "JEV_ADAPTER_VERSION",
                                         capture.get("adapter_version"))
    capture["commit"] = commit
    capture["dirty"] = dirty
    capture["endpoint_path"] = path
    with open(GOLDEN, "w") as fh:
        json.dump(golden, fh, indent=1, sort_keys=True)
    print("wrote %s (commit %s, dirty=%s)" % (GOLDEN, commit[:12], dirty))
    return 0 if not dirty else 1


if __name__ == "__main__":
    sys.exit(main())
