"""Per-episode recording: wire bytes plus action/decision/meta sidecars.

Layout (``doc/agent-autoplay-plan.md`` section "Recording and offline
evaluation"):

  * ``ep-N.wire.jsonl``      -- the inbound physical bytes, verbatim, in order
                                (compatible with the existing replay tools);
  * ``ep-N.actions.jsonl``   -- outbound actions/auxiliaries with their
                                ordinal, the preceding input offset, the
                                ``NeedKey`` and the send status;
  * ``ep-N.decisions.jsonl`` -- what the policy proposed, the provider that
                                answered, fallback reasons, boundaries,
                                latency and usage;
  * ``ep-N.meta.json``       -- schema versions, allowlisted configuration,
                                completeness and outcome/stop-reason.

Writes run on a bounded background writer so the wire is never blocked: a
full queue or a disk failure marks the recording incomplete and follows the
documented graceful-stop policy rather than silently dropping bytes and
claiming a complete transcript.  Directories are 0700 and files 0600.

No secrets are recorded: only allowlisted configuration fields and structured
error categories, never environment dumps or raw provider bodies.
"""

import json
import os
import queue
import threading
import time

SCHEMA_WIRE = 1
SCHEMA_ACTIONS = 1
SCHEMA_DECISIONS = 1
SCHEMA_META = 1

_STOP = object()


class _Writer(threading.Thread):
    def __init__(self, path, maxsize=4096):
        super().__init__(daemon=True)
        self.path = path
        self.q = queue.Queue(maxsize=maxsize)
        self.error = None
        self.dropped = 0
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.fh = os.fdopen(fd, "wb", closefd=True)

    def submit(self, data: bytes) -> bool:
        try:
            self.q.put_nowait(data)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def run(self):
        while True:
            item = self.q.get()
            if item is _STOP:
                break
            if self.error is not None:
                continue
            try:
                self.fh.write(item)
            except OSError as exc:  # disk failure: stop, mark incomplete
                self.error = str(exc)
        try:
            self.fh.flush()
            self.fh.close()
        except OSError as exc:
            self.error = self.error or str(exc)

    def shutdown(self, timeout=5.0):
        if getattr(self, "_running", False):
            try:
                self.q.put(_STOP, timeout=timeout)
            except queue.Full:
                pass
            self.join(timeout=timeout)
        else:
            try:
                self.fh.close()
            except OSError:
                pass

    def start(self):
        self._running = True
        super().start()


class EpisodeRecorder(object):
    def __init__(self, output_dir, episode, maxsize=4096):
        self.output_dir = output_dir
        self.episode = episode
        os.makedirs(output_dir, mode=0o700, exist_ok=True)
        base = os.path.join(output_dir, "ep-%d" % episode)
        self.wire_path = base + ".wire.jsonl"
        self.actions_path = base + ".actions.jsonl"
        self.decisions_path = base + ".decisions.jsonl"
        self.meta_path = base + ".meta.json"
        self._wire = _Writer(self.wire_path, maxsize)
        self._acts = _Writer(self.actions_path, maxsize)
        self._decs = _Writer(self.decisions_path, maxsize)
        for w in (self._wire, self._acts, self._decs):
            w.start()
        self.incomplete = False
        self.wire_bytes = 0
        self.wire_lines = 0
        self.actions = 0
        self.decisions = 0
        self.started = time.time()

    # -- writers ---------------------------------------------------------
    def record_wire(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line = line + b"\n"
        self.wire_bytes += len(line)
        self.wire_lines += 1
        if not self._wire.submit(line):
            self.incomplete = True

    def record_action(self, ordinal, input_offset, need_key, action, status):
        obj = {"schema": SCHEMA_ACTIONS, "ordinal": ordinal,
               "input_offset": input_offset,
               "need": _need_key_obj(need_key), "action": action,
               "status": status, "t": round(time.time() - self.started, 6)}
        self.actions += 1
        if not self._acts.submit(_json_line(obj)):
            self.incomplete = True

    def record_decision(self, proposal, selected, provider, reason,
                        boundaries=(), directives=(), latency=0.0,
                        usage=None):
        obj = {"schema": SCHEMA_DECISIONS,
               "proposal": proposal, "selected": selected,
               "provider": provider, "reason": reason,
               "boundaries": list(boundaries),
               "directives": list(directives),
               "latency": round(latency, 6), "usage": usage or {},
               "t": round(time.time() - self.started, 6)}
        self.decisions += 1
        if not self._decs.submit(_json_line(obj)):
            self.incomplete = True

    # -- shutdown --------------------------------------------------------
    def finalize(self, meta):
        for w in (self._wire, self._acts, self._decs):
            w.shutdown()
            if w.error is not None or w.dropped:
                self.incomplete = True
        full = {
            "schema": SCHEMA_META,
            "episode": self.episode,
            "recording_complete": not self.incomplete,
            "wire_lines": self.wire_lines,
            "wire_bytes": self.wire_bytes,
            "actions": self.actions,
            "decisions": self.decisions,
            "files": {
                "wire": os.path.basename(self.wire_path),
                "actions": os.path.basename(self.actions_path),
                "decisions": os.path.basename(self.decisions_path),
            },
        }
        full.update(meta or {})
        fd = os.open(self.meta_path,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(full, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return full


def _json_line(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8")


def _need_key_obj(need_key):
    if need_key is None:
        return None
    return {"episode": need_key.episode, "seq": need_key.seq,
            "id": need_key.id}


def infer_outcome(messages):
    """Infer a *visible* game outcome from public text.  This is an
    observation, never proof: merely seeing ``closed`` does not prove death."""
    text = " ".join(messages).lower()
    for marker, label in (("you die", "death"),
                          ("you are dead", "death"),
                          ("goodbye", "death"),
                          ("you starve", "starvation"),
                          ("you faint from lack of food", "starvation"),
                          ("ascend", "ascension")):
        if marker in text:
            return label
    return "unknown"
