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
  * ``ep-N.events.jsonl``    -- the schema-versioned event-lifecycle ledger:
                                one record per boundary EID (detected ->
                                queued -> dispatched -> one terminal state,
                                with ticks and levels) and per directive
                                activation/expiry;
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
SCHEMA_EVENTS = 1
SCHEMA_META = 1

_STOP = object()

# Directory/file modes: recording is private by construction, and an existing
# looser directory or file is tightened rather than trusted.
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def ensure_private_dir(path: str) -> None:
    """Create *path* if needed and tighten it to owner-only (or raise)."""
    os.makedirs(path, mode=_DIR_MODE, exist_ok=True)
    if os.stat(path).st_mode & 0o077:
        os.chmod(path, _DIR_MODE)


def _open_private(path, flags) -> int:
    """Open *path* at 0600 and force the mode even for a pre-existing file.

    A filesystem that rejects the mode change is a *recording error*: a
    private transcript that silently became group- or world-readable is worse
    than a failed recording, so this fails closed rather than proceeding
    permissive.  A platform with no ``fchmod`` at all relies on the mode
    passed to :func:`os.open`.
    """
    fd = os.open(path, flags, _FILE_MODE)
    try:
        os.fchmod(fd, _FILE_MODE)
    except AttributeError:
        # no fchmod on this platform: os.open's mode is the only guarantee
        pass
    except OSError:
        os.close(fd)
        raise
    return fd


class _Writer(threading.Thread):
    def __init__(self, path, maxsize=4096):
        super().__init__(daemon=True)
        self.path = path
        self.q = queue.Queue(maxsize=maxsize)
        self.error = None
        self.dropped = 0
        self.alive = False
        self.drained = False
        fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        self.fh = os.fdopen(fd, "wb", closefd=True)

    def submit(self, data: bytes, block: bool = False,
               timeout: float = 0.2) -> bool:
        """Queue *data*; return False if it could not be accepted.

        ``block`` gives a *bounded* wait for room, for a producer that can
        momentarily outrun the writer (a lifecycle-event burst).  It never
        waits once the writer has failed, and a failed write is always
        reflected in ``error``, so a stall can never masquerade as success.
        """
        if self.error is not None:
            return False
        if block:
            try:
                self.q.put(data, timeout=timeout)
                return True
            except queue.Full:
                self.dropped += 1
                return False
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
        """Stop the writer and report how it ended.

        Returns one of ``drained`` (sentinel queued and the thread joined),
        ``not-drained`` (the sentinel could not be queued), ``terminated``
        (still alive after the join timed out) or ``closed`` (never started).
        """
        if not getattr(self, "_running", False):
            try:
                self.fh.close()
            except OSError as exc:
                self.error = self.error or str(exc)
            return "closed"
        status = "drained"
        try:
            self.q.put(_STOP, timeout=timeout)
        except queue.Full:
            status = "not-drained"
        self.join(timeout=timeout)
        if self.is_alive():
            self.alive = True
            status = "terminated"
        else:
            self.drained = status == "drained"
        return status

    def start(self):
        self._running = True
        super().start()


class EpisodeRecorder(object):
    def __init__(self, output_dir, episode, maxsize=4096):
        self.output_dir = output_dir
        self.episode = episode
        ensure_private_dir(output_dir)
        base = os.path.join(output_dir, "ep-%d" % episode)
        self.wire_path = base + ".wire.jsonl"
        self.actions_path = base + ".actions.jsonl"
        self.decisions_path = base + ".decisions.jsonl"
        self.events_path = base + ".events.jsonl"
        self.meta_path = base + ".meta.json"
        # Construct all four writers before starting any of them.  A writer
        # that cannot be opened at 0600 fails closed: every writer already
        # constructed is released, in reverse order, so a per-episode
        # construction failure cannot leak the descriptors it already holds.
        opened = []
        try:
            self._wire = _Writer(self.wire_path, maxsize)
            opened.append(self._wire)
            self._acts = _Writer(self.actions_path, maxsize)
            opened.append(self._acts)
            self._decs = _Writer(self.decisions_path, maxsize)
            opened.append(self._decs)
            self._evs = _Writer(self.events_path, maxsize)
            opened.append(self._evs)
        except OSError:
            for w in reversed(opened):
                w.shutdown()
            raise
        for w in opened:
            w.start()
        self.incomplete = False
        self.wire_bytes = 0
        self.wire_lines = 0
        self.actions = 0
        self.decisions = 0
        self.events = 0
        self.started = time.time()

    @property
    def failed(self) -> bool:
        """True the moment the recording can no longer be trusted as lossless.

        The controller reads this after every outbound line so a recorder
        failure surfaces immediately (Wave 2 hooks its disable-paid-work /
        graceful-stop policy here).
        """
        if self.incomplete:
            return True
        for w in (self._wire, self._acts, self._decs, self._evs):
            if w.error is not None or w.dropped:
                return True
        return False

    # -- writers ---------------------------------------------------------
    def record_wire(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line = line + b"\n"
        self.wire_bytes += len(line)
        self.wire_lines += 1
        if not self._wire.submit(line):
            self.incomplete = True

    def record_action(self, ordinal, input_offset, need_key, kind, action,
                      status):
        obj = {"schema": SCHEMA_ACTIONS, "ordinal": ordinal,
               "input_offset": input_offset,
               "need": _need_key_obj(need_key), "kind": kind,
               "action": action,
               "status": status, "t": round(time.time() - self.started, 6)}
        self.actions += 1
        if not self._acts.submit(_json_line(obj)):
            self.incomplete = True

    def record_decision(self, proposal, selected, provider, reason,
                        boundaries=(), directives=(), latency=0.0,
                        usage=None, diagnostics=None):
        obj = {"schema": SCHEMA_DECISIONS,
               "proposal": proposal, "selected": selected,
               "provider": provider, "reason": reason,
               "boundaries": list(boundaries),
               "directives": list(directives),
               "latency": round(latency, 6), "usage": usage or {},
               "t": round(time.time() - self.started, 6)}
        # Additive, backward-compatible decision diagnostics (stall-recovery
        # plan §5): the selected semantic label/reason, recovery stage, held
        # serial, stall/door counters and the provider fallback reason are
        # carried *separately*, so a `cap reached` fallback never hides the
        # scripted candidate's own reason.
        if diagnostics:
            obj["diagnostics"] = dict(diagnostics)
        self.decisions += 1
        if not self._decs.submit(_json_line(obj)):
            self.incomplete = True

    def record_event(self, obj) -> None:
        """Append one schema-versioned event-lifecycle record.

        The controller writes one record per boundary EID (its detected /
        queued / dispatched / terminal steps) and per directive activation or
        expiry.  Keeping this in its own sidecar leaves the deterministic
        lifecycle fields separate from the wall-clock ``t`` timestamps in the
        other streams, so a replay comparison can drop timing exactly.

        Unlike the wire stream -- which must never block -- the event stream
        takes a *bounded* wait for room: lifecycle records are emitted
        incrementally, so a burst can briefly outrun the writer, and dropping
        them would corrupt a complete-for-this-run ledger.
        """
        self.events += 1
        if not self._evs.submit(_json_line(obj), block=True):
            self.incomplete = True

    # -- shutdown --------------------------------------------------------
    def finalize(self, meta):
        statuses = []
        for w in (self._wire, self._acts, self._decs, self._evs):
            statuses.append(w.shutdown())
            if w.error is not None or w.dropped or w.alive \
                    or statuses[-1] != "drained":
                self.incomplete = True
        # "complete" is claimed only when all four streams actually drained
        full = {
            "schema": SCHEMA_META,
            "episode": self.episode,
            "recording_complete": not self.incomplete,
            "writer_status": statuses,
            "wire_lines": self.wire_lines,
            "wire_bytes": self.wire_bytes,
            "actions": self.actions,
            "decisions": self.decisions,
            "events": self.events,
            "files": {
                "wire": os.path.basename(self.wire_path),
                "actions": os.path.basename(self.actions_path),
                "decisions": os.path.basename(self.decisions_path),
                "events": os.path.basename(self.events_path),
            },
        }
        full.update(meta or {})
        fd = _open_private(self.meta_path,
                           os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
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
    observation, never proof: seeing ``closed`` alone does not prove death."""
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
