"""One bounded pipe-owning controller: scheduling, fallback, retry, lifecycle.

The controller owns exactly one game pipe per episode.  It never stalls the
wire:

  * page transfer keeps exactly **one** ``get_page`` in flight (the next page
    is requested only after its response has been consumed and validated), so
    a peer advertising 65,535 pages cannot make the outbound obligation grow
    without bound;
  * every outbound line is written with a select-based, monotonic-deadline
    write-all, so a full stdin pipe is bounded by the answer deadline instead
    of blocking a thread forever;
  * ``invalid`` is a retry state, not completion -- preserve the request id,
    repair once where applicable, then fall back per kind, then stop after a
    small total retry cap instead of spinning forever;
  * ``closed`` ends the episode; **EOF without closed is a transport
    failure**, never a fabricated terminal record.  Closure is best-effort
    (see ``sys/unix/agent_runner.c``), so a ``closed`` that arrives while a
    request is still unanswered is a *failure*, not a success;
  * timeout / forced kills / unanswered requests are reported separately from
    natural completions.

Session validation is bounded and per-episode: a wrong or missing ``hello``,
a duplicate ``hello``, an out-of-order or non-monotonic record, an oversized
physical line and an exhausted assembler budget each terminate *that episode*
promptly and the campaign continues.

The launcher child is spawned into its own process group with an allowlisted
environment (provider secrets are stripped), and teardown escalates
TERM -> KILL across the whole group while always reaping the direct child.
"""

import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import protocol, recording
from .codec import AssemblerLimit, ChunkError, IncrementalAssembler
from .policy import ScriptedReflex
from .protocol import NeedKey, Request, Snapshot
from .providers import ProviderConfig, ReflexContext
from .state import EpisodeMemory

# Environment names the launcher is allowed to inherit.  Everything else --
# in particular DEEPSEEK_API_KEY and JEV_API_KEY -- is dropped by
# construction, not merely unset: this is an allowlist, so a new provider
# secret cannot leak just because someone forgot to add it to a deny list.
_CHILD_ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                    "TERM", "USER", "LOGNAME",
                    "NETHACK_AGENT_TEST_COPY_LIMIT")
_CHILD_ENV_DEFAULTS = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8"}
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


class _ProtocolFailure(Exception):
    """A bounded per-episode wire violation (the campaign continues)."""


class _TransportFailure(Exception):
    """An outbound write failed: terminal for this episode."""


class _DeadlineExceeded(Exception):
    """A per-need (content/transport) deadline expired."""


def child_env(source=None) -> dict:
    """The minimal, allowlisted environment for the launcher child.

    Only the names in :data:`_CHILD_ENV_ALLOW` survive, and anything whose
    name still looks like a credential is dropped as well.  The result is what
    is handed to :func:`subprocess.Popen` as ``env``.
    """
    source = os.environ if source is None else source
    env = {}
    for name in _CHILD_ENV_ALLOW:
        if name in source:
            env[name] = source[name]
    for name, value in _CHILD_ENV_DEFAULTS.items():
        env.setdefault(name, value)
    for name in list(env):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            del env[name]
    return env


def ensure_private_dir(path: str) -> None:
    """Create *path* if needed and tighten it to owner-only.

    ``makedirs(mode=0700, exist_ok=True)`` does **not** tighten a directory
    that already exists with looser bits; this does, or raises if it cannot.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


@dataclass
class EpisodeResult(object):
    index: int
    spawn_ok: bool = False
    closed: bool = False
    eof: bool = False
    forced_kill: bool = False
    teardown_failure: bool = False
    unanswered: bool = False
    recorder_failed: bool = False
    protocol_failure: Optional[str] = None
    failure_reason: Optional[str] = None
    timed_out: bool = False
    stop_reason: str = "unknown"
    outcome: str = "unknown"
    ticks: int = 0
    needs: int = 0
    invalids: int = 0
    actions: int = 0
    returncode: Optional[int] = None
    recording_complete: bool = False
    stderr_tail: str = ""


@dataclass
class ControllerPaths(object):
    worker: str
    runner: str
    data: str
    sysconf: Optional[str] = None


class Controller(object):
    def __init__(self, config: ProviderConfig, paths: ControllerPaths,
                 output_dir: str, episode_timeout: float = 300.0,
                 read_slack: float = 30.0, max_retries: int = 3,
                 reap_grace: float = 5.0):
        self.config = config
        self.paths = paths
        self.output_dir = output_dir
        self.episode_timeout = episode_timeout
        self.read_slack = read_slack
        self.max_retries = max_retries
        self.reap_grace = reap_grace
        # The declared provider deadlines are the wire's deadlines too; they
        # are configurable (CLI -> ProviderConfig) and actually enforced here.
        self.answer_deadline = float(getattr(config, "answer_deadline", 1.0))
        self.content_deadline = float(
            getattr(config, "content_deadline", 5.0))
        ensure_private_dir(output_dir)

    # -- campaign --------------------------------------------------------
    def run_campaign(self, episodes: int) -> List[EpisodeResult]:
        results = []
        for i in range(1, episodes + 1):
            results.append(self.run_episode(i))
        return results

    def run_episode(self, index: int) -> EpisodeResult:
        result = EpisodeResult(index=index)
        priv = tempfile.mkdtemp(prefix="nh-auto-ep.")
        rec = recording.EpisodeRecorder(self.output_dir, index)
        proc = None
        try:
            proc = self._spawn(priv)
            result.spawn_ok = True
            runner = _EpisodeRunner(self, proc, rec, result)
            runner.run()
            result.recorder_failed = rec.failed
        except OSError as exc:
            result.failure_reason = "spawn failed: %s" % exc
            result.stop_reason = "spawn-failure"
        finally:
            self._reap(proc, result)
            shutil.rmtree(priv, ignore_errors=True)
            result.recorder_failed = result.recorder_failed or rec.failed
            meta = {
                "config": _safe_config(self.config),
                "episode_timeout": self.episode_timeout,
                "answer_deadline": self.answer_deadline,
                "content_deadline": self.content_deadline,
                "stop_reason": result.stop_reason,
                "game_outcome": result.outcome,
                "closed": result.closed,
                "eof": result.eof,
                "forced_kill": result.forced_kill,
                "teardown_failure": result.teardown_failure,
                "unanswered": result.unanswered,
                "recorder_failed": result.recorder_failed,
                "protocol_failure": result.protocol_failure,
                "failure_reason": result.failure_reason,
                "ticks": result.ticks,
                "needs": result.needs,
                "invalids": result.invalids,
                "returncode": result.returncode,
            }
            rec.finalize(meta)
            result.recording_complete = not rec.incomplete
        return result

    # -- process ---------------------------------------------------------
    def _child_env(self) -> dict:
        return child_env()

    def _spawn(self, priv):
        argv = [self.paths.runner, "--worker", self.paths.worker,
                "--private-root", priv, "--data", self.paths.data]
        if self.paths.sysconf:
            argv += ["--sysconf", self.paths.sysconf]
        deadline = int(self.episode_timeout + self.read_slack)
        argv += ["--deadline", str(deadline)]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0,
                                env=self._child_env(),
                                start_new_session=True)
        try:
            os.set_blocking(proc.stdin.fileno(), False)
        except (OSError, ValueError):
            pass
        proc._auto_stderr = _StderrDrain(proc.stderr)
        proc._auto_stderr.start()
        return proc

    def _reap(self, proc, result: EpisodeResult) -> None:
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        # Give the launcher a moment to exit and clean up its own tree; a
        # clean exit must keep its real status.  Only a launcher that does not
        # exit is escalated, TERM -> KILL, across the whole process group it
        # owns (start_new_session made it a group leader), so a launcher that
        # ignores SIGTERM cannot leave a long-lived worker behind.
        if not _wait_child(proc, self.reap_grace):
            _signal_group(proc, signal.SIGTERM)
            if not _wait_child(proc, 3.0):
                result.forced_kill = True
                _signal_group(proc, signal.SIGKILL)
                if not _wait_child(proc, 5.0):
                    # never silently continue: the subtree may still be alive
                    result.teardown_failure = True
        drain = getattr(proc, "_auto_stderr", None)
        if drain is not None:
            drain.join(timeout=2)
            result.stderr_tail = drain.text()[-1000:]
        result.returncode = proc.returncode


def _signal_group(proc, sig) -> None:
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    send = getattr(proc, "kill" if sig == signal.SIGKILL else "terminate",
                   None)
    if send is not None:
        try:
            send()
        except (OSError, ProcessLookupError):
            pass


def _wait_child(proc, timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


class _StderrDrain(object):
    """Drain the launcher's stderr on a daemon thread so it cannot block."""

    def __init__(self, fh, cap=256):
        import threading
        self.fh = fh
        self.cap = cap
        self.lines: List[str] = []
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        try:
            for raw in iter(self.fh.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                self.lines.append(line)
                if len(self.lines) > self.cap:
                    del self.lines[:len(self.lines) - self.cap]
        except (OSError, ValueError):
            pass

    def join(self, timeout=2):
        self._t.join(timeout=timeout)

    def text(self):
        return "\n".join(self.lines)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class _EpisodeRunner(object):
    def __init__(self, controller, proc, rec, result):
        self.c = controller
        self.proc = proc
        self.rec = rec
        self.result = result
        self.asm = IncrementalAssembler(
            max_retained_bytes=protocol.MAX_RETAINED_BYTES,
            max_chunks=protocol.MAX_CHUNKS, max_streams=protocol.MAX_STREAMS,
            max_line_bytes=protocol.MAX_PHYSICAL_LINE)
        self.snap = Snapshot()
        self.req = Request()
        self.mem = EpisodeMemory()
        self.reflex = ScriptedReflex(controller.config)
        self.reflex.max_ticks = controller.config.max_ticks
        self.tick = 0
        self.retries = 0
        self._needs = 0
        self._invalids: List[str] = []
        self.force_fallback = False
        self.pending = False
        self.pending_need = None
        self.pending_seq = None
        self.pending_key = None
        self.acked_chunks = set()
        self.buf = b""
        self.hello = None
        self.hello_seen = False
        self.last_seq = 0
        self.closed = False
        self.deadline = 0.0
        self.need_started = 0.0
        self.action_ordinal = 0
        self.rec_healthy = True

    # -- top loop --------------------------------------------------------
    def run(self):
        self.deadline = time.monotonic() + self.c.episode_timeout
        try:
            while not self.closed:
                if self.pending:
                    need_dl = self._need_deadline()
                    if need_dl is not None and time.monotonic() >= need_dl:
                        raise _DeadlineExceeded(
                            "content deadline expired for need %r"
                            % (self.req.id,))
                    if self.req.pages_declared \
                            and not self.req.pages_complete():
                        if not self._request_page(need_dl):
                            break     # EOF
                        continue
                    if not self._answer_now(need_dl):
                        break
                    continue
                if not self._pump():
                    break
        except _DeadlineExceeded as exc:
            self.result.failure_reason = str(exc)
            self.result.stop_reason = "content-deadline"
        except _TransportFailure as exc:
            self.result.failure_reason = "stdin write failed: %s" % exc
            self.result.stop_reason = "transport-failure-write"
        except _ProtocolFailure as exc:
            self.result.protocol_failure = str(exc)
            self.result.stop_reason = "protocol-failure"
        except TimeoutError as exc:
            self.result.timed_out = True
            self.result.failure_reason = str(exc)
            self.result.stop_reason = "episode-timeout"
        self._finish()

    def _finish(self):
        self.result.ticks = self.tick
        self.result.needs = self._needs
        self.result.invalids = len(self._invalids)
        self.result.actions = self.action_ordinal
        self.result.closed = self.closed
        # Closure is best-effort: `closed` while a request is still awaiting
        # pages or an action is an unanswered obligation, not a completion.
        if self.closed and self.pending and self.pending_need is not None:
            self.result.unanswered = True
            if self.result.failure_reason is None:
                self.result.failure_reason = (
                    "closed with an unanswered request (id %r)"
                    % (self.req.id,))
            self.result.stop_reason = "closed-unanswered"
        if self.result.stop_reason == "unknown":
            if self.closed:
                if self.reflex.quitting and self.reflex.quit_reason == \
                        "tick-cap":
                    self.result.stop_reason = "tick-cap-graceful-quit"
                else:
                    self.result.stop_reason = "closed"
            elif self.result.eof:
                self.result.stop_reason = "transport-failure-eof"
        self.result.outcome = recording.infer_outcome(self.mem.messages)

    # -- outbound (one send-and-record path) -----------------------------
    def _emit(self, kind, obj, need_key=None, write_deadline=None):
        """Write one outbound line and record it; return its ordinal.

        Every outbound line -- act, get_page, ack_chunk -- goes through here,
        so the actions sidecar is a faithful, ordered record of the wire.  A
        failed write is a terminal transport failure and is recorded with its
        status before propagating.
        """
        payload = (json.dumps(obj) + "\n").encode("utf-8")
        offset = self.rec.wire_bytes
        ordinal = self.action_ordinal + 1
        try:
            self._write_all(payload, write_deadline)
        except _TransportFailure:
            self.action_ordinal = ordinal
            self.rec.record_action(ordinal, offset, need_key, kind, obj,
                                   "write-failed")
            self._note_recorder_health()
            raise
        self.action_ordinal = ordinal
        self.rec.record_action(ordinal, offset, need_key, kind, obj, "sent")
        self._note_recorder_health()
        return ordinal

    def _write_all(self, data: bytes, deadline) -> None:
        stream = self.proc.stdin
        fileno = getattr(stream, "fileno", None)
        if fileno is None:
            # a test double without a real descriptor: a single atomic write
            try:
                stream.write(data)
                flush = getattr(stream, "flush", None)
                if flush is not None:
                    flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise _TransportFailure(str(exc))
            return
        fd = stream.fileno()
        if deadline is None:
            deadline = time.monotonic() + self.c.answer_deadline
        view = memoryview(data)
        while len(view):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportFailure("write deadline exceeded")
            try:
                _, w, _ = select.select([], [fd], [], min(remaining, 0.25))
            except (OSError, ValueError) as exc:
                raise _TransportFailure("select on stdin failed: %s" % exc)
            if not w:
                continue
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise _TransportFailure(str(exc))
            if n <= 0:
                raise _TransportFailure("stdin accepted no data")
            view = view[n:]

    def _note_recorder_health(self) -> None:
        if not self.rec_healthy:
            return
        if self.rec.failed:
            # Recorder failure surfaces here immediately.  Wave 1 has no paid
            # dispatch; Wave 2 hooks its graceful-stop / disable-paid-work
            # policy on this flag.
            self.rec_healthy = False

    # -- pipe ------------------------------------------------------------
    def _need_deadline(self):
        if not self.pending or self.pending_need is None:
            return None
        return self.need_started + self.c.content_deadline

    def _readline(self, deadline=None):
        if deadline is None:
            deadline = self.deadline
        bound = min(deadline, self.deadline)
        while b"\n" not in self.buf:
            remaining = bound - time.monotonic()
            if remaining <= 0:
                if self.deadline <= time.monotonic():
                    raise TimeoutError("episode deadline reached")
                raise _DeadlineExceeded("content deadline reached")
            if len(self.buf) > protocol.MAX_PHYSICAL_LINE:
                raise _ProtocolFailure(
                    "unterminated line exceeds %d bytes"
                    % protocol.MAX_PHYSICAL_LINE)
            r, _, _ = select.select([self.proc.stdout], [], [],
                                    min(remaining, 1.0))
            if not r:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line
                return None
            self.buf += chunk
        idx = self.buf.index(b"\n")
        if idx > protocol.MAX_PHYSICAL_LINE:
            raise _ProtocolFailure("physical line exceeds %d bytes"
                                   % protocol.MAX_PHYSICAL_LINE)
        line, self.buf = self.buf[:idx], self.buf[idx + 1:]
        return line

    def _pump(self, deadline=None) -> bool:
        """Read and handle one physical line.  Returns False on EOF."""
        line = self._readline(deadline)
        if line is None:
            self.result.eof = True
            return False
        self.rec.record_wire(line)
        self._handle_line(line)
        self._note_recorder_health()
        return not self.closed

    def _handle_line(self, line: bytes):
        if len(line) > protocol.MAX_PHYSICAL_LINE:
            raise _ProtocolFailure("physical line exceeds %d bytes"
                                   % protocol.MAX_PHYSICAL_LINE)
        try:
            logical = self.asm.feed(line)
        except AssemblerLimit as exc:
            raise _ProtocolFailure("assembler retention limit: %s" % exc)
        except ChunkError as exc:
            raise _ProtocolFailure("chunk stream: %s" % exc)
        except (ValueError, KeyError, TypeError) as exc:
            raise _ProtocolFailure("malformed record: %s" % exc)
        for rec in logical:
            self._handle_record(rec)
        self._ack_chunk(line)

    def _handle_record(self, rec):
        if not isinstance(rec, dict):
            raise _ProtocolFailure("record is not an object")
        t = rec.get("type")
        # A validated hello must precede *every* non-hello record -- closed
        # included: a closed-only stream (or a page/invalid before the
        # handshake) is a protocol failure, not a completed episode.
        if t != "hello" and not self.hello_seen:
            raise _ProtocolFailure("%r record before hello" % (t,))
        if t == "hello":
            self._on_hello(rec)
        elif t == "obs":
            self._on_obs(rec)
        elif t == "page":
            self._on_page(rec)
        elif t == "invalid":
            self._on_invalid(rec)
        elif t == "closed":
            self._on_closed(rec)
        else:
            raise _ProtocolFailure("unknown record type %r" % (t,))

    def _ack_chunk(self, line: bytes):
        # chunk acknowledgement is cumulative and contiguous; ack each newly
        # seen (rid, i) once, in arrival order
        if b'"chunk"' not in line:
            return
        try:
            raw = json.loads(line)
        except ValueError:
            return
        if not isinstance(raw, dict) or raw.get("type") != "chunk":
            return
        key = (raw.get("rid"), raw.get("i"))
        if key in self.acked_chunks:
            return
        self.acked_chunks.add(key)
        self._emit("ack_chunk", protocol.make_ack_chunk(raw.get("rid"),
                                                        raw.get("i")))

    # -- records ---------------------------------------------------------
    def _on_hello(self, rec):
        if self.hello_seen:
            raise _ProtocolFailure("duplicate hello")
        reason = protocol.validate_hello(rec)
        if reason:
            raise _ProtocolFailure("incompatible hello: %s" % reason)
        self.hello_seen = True
        self.hello = rec

    def _on_obs(self, rec):
        seq = rec.get("seq")
        if not _is_int(seq):
            raise _ProtocolFailure("obs without an integer seq")
        if seq <= self.last_seq:
            raise _ProtocolFailure("non-monotonic seq %r after %r"
                                   % (seq, self.last_seq))
        self.last_seq = seq
        # A malformed full snapshot must end this episode, not the whole
        # campaign: expected decode/shape faults (a bad palette entry, map
        # triple, window, cursor or message) are converted here, at the
        # episode boundary, into a per-episode protocol failure.  Only
        # ProtocolError and the shape errors of well-typed-but-broken data
        # are caught -- never control-flow or system exceptions.
        try:
            self.snap.apply(rec)
            self.mem.observe(self.snap)
        except protocol.ProtocolError as exc:
            raise _ProtocolFailure("invalid snapshot: %s" % exc)
        except (IndexError, KeyError, TypeError, ValueError,
                AttributeError) as exc:
            raise _ProtocolFailure("malformed snapshot: %s" % exc)
        need = rec.get("need")
        if need is not None and not isinstance(need, dict):
            raise _ProtocolFailure("need is not an object")
        self.req.begin(need, seq)
        self.pending = need is not None
        self.pending_need = need
        self.pending_seq = seq
        self.need_started = time.monotonic()
        self.force_fallback = False
        self.retries = 0
        if need is not None:
            self._needs += 1
            self.pending_key = NeedKey(self.result.index, seq, need.get("id"))
        else:
            self.pending_key = None

    def _on_page(self, rec):
        if not self.pending or self.req.need is None:
            raise _ProtocolFailure("page delivered with no outstanding need")
        if rec.get("content") != self.req.content:
            raise _ProtocolFailure("page for unexpected content %r"
                                   % (rec.get("content"),))
        idx = rec.get("page")
        if not _is_int(idx) or not (0 <= idx < self.req.pages_declared):
            raise _ProtocolFailure("page index %r out of the declared range"
                                   % (idx,))
        self.req.note_page(rec)

    def _on_invalid(self, rec):
        code = rec.get("code")
        if code not in protocol.INVALID_CODES:
            raise _ProtocolFailure("unknown invalid code %r" % (code,))
        # `invalid` refers to the request the engine still holds outstanding;
        # that is true whether or not we have already sent an answer for it.
        if self.req.need is None or self.pending_need is None:
            raise _ProtocolFailure("invalid with no outstanding request")
        self._invalids.append(code)
        self.retries += 1
        # record the rejected attempt: a decision with no selected action,
        # carrying the rejection reason
        self.rec.record_decision(
            proposal=None, selected=None, provider="controller",
            reason="invalid:%s (attempt %d)" % (code, self.retries))
        if self.retries > self.c.max_retries:
            raise _ProtocolFailure(
                "request id %r rejected %d times (last code %r)"
                % (self.req.id, self.retries, code))
        if code == "incomplete":
            # pages were not all delivered: drop the request bookkeeping so
            # the page obligation is re-issued
            self.req.reset_delivery()
        else:
            self.force_fallback = True
        # the engine left the SAME request outstanding: re-arm it
        self.pending = True
        self.need_started = time.monotonic()

    def _on_closed(self, rec):
        self.closed = True
        self.reflex.on_closed()

    # -- decisions -------------------------------------------------------
    def _request_page(self, deadline) -> bool:
        """Send the single owed get_page, then read one line."""
        preq = self.req.next_page_request()
        if preq is None:
            # a page response is outstanding: wait for it
            return self._pump(deadline)
        write_dl = time.monotonic() + self.c.answer_deadline
        if deadline is not None:
            write_dl = min(write_dl, deadline)
        self._emit("get_page", preq, need_key=self.pending_key,
                   write_deadline=write_dl)
        # mark requested only after the complete request line was written
        self.req.mark_page_requested(preq["page"])
        return self._pump(deadline)

    def _answer_now(self, deadline) -> bool:
        need = self.pending_need
        write_dl = time.monotonic() + self.c.answer_deadline
        if deadline is not None:
            write_dl = min(write_dl, deadline)
        proposal, provider, reason, latency, usage = self._decide(need)
        if proposal is None:
            selected = self._safe_fallback(need)
            sel_reason = ("no proposal (%s): safe fallback"
                          % (reason or "none"))
        else:
            err = protocol.validate_action(need, proposal)
            if err:
                selected = self._safe_fallback(need)
                sel_reason = "validation fallback: %s" % err
            else:
                selected = proposal
                sel_reason = reason
        boundaries = [b.eid for b in self.mem.boundary.check(self.mem.status)]
        self.rec.record_decision(
            proposal=proposal, selected=selected, provider=provider,
            reason=sel_reason, boundaries=boundaries, latency=latency,
            usage=usage)
        obj = protocol.make_act(self.pending_seq, need["id"], selected)
        self._emit("act", obj, need_key=self.pending_key,
                   write_deadline=write_dl)
        # requested/pending state mutates only after the complete send
        if need.get("kind") in ("command", "key", "direction"):
            self.tick += 1
        self.pending = False
        self.force_fallback = False
        return True

    def _decide(self, need):
        if self.force_fallback:
            return (self._safe_fallback(need), "controller",
                    "forced fallback", 0.0, {})
        ctx = ReflexContext(
            episode=self.result.index, tick=self.tick, need=need,
            need_key=self.pending_key, snapshot=self.snap,
            pages=self.req.page_rows(), memory=self.mem)
        t0 = time.monotonic()
        res = self.reflex.decide(ctx)
        latency = time.monotonic() - t0
        return res.action, res.provider or "scripted", res.reason, latency, \
            res.usage

    def _safe_fallback(self, need) -> dict:
        kind = need.get("kind")
        if kind in ("command", "key", "direction"):
            return {"key": protocol.KEY_WAIT}
        if kind == "yn":
            return {"yn": protocol.KEY_ESC}
        if kind == "position":
            return {"key": protocol.KEY_ESC}
        if kind in ("line", "extcmd"):
            return {"cancel": True}
        if kind == "menu":
            return {"cancel": True}
        if kind == "ack":
            return {"ack": True}
        return {"key": protocol.KEY_ESC}


def _safe_config(config: ProviderConfig) -> dict:
    return {
        "reflex": config.reflex,
        "strategy": config.strategy,
        "role": config.role,
        "max_ticks": config.max_ticks,
        "confidence_threshold": config.confidence_threshold,
        "strategy_call_cap": config.strategy_call_cap,
        "deepseek_model": config.deepseek_model,
        "deepseek_base_url": config.deepseek_base_url,
        "reflex_deadline": getattr(config, "reflex_deadline", None),
        "answer_deadline": getattr(config, "answer_deadline", None),
        "content_deadline": getattr(config, "content_deadline", None),
    }
