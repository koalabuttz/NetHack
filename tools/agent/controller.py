"""One bounded pipe-owning controller: scheduling, fallback, retry, lifecycle.

The controller owns exactly one game pipe per episode.  It never stalls the
wire: it acts the moment a request is outstanding (after its pages arrive),
fetches at most one page at a time, caps retries, and bounds every wait by the
episode deadline.  The state machine per the handoff:

  * ``invalid`` is a retry state, not completion -- preserve the request id,
    repair once where applicable, then fall back per kind, then stop after a
    small total retry cap instead of spinning forever;
  * ``closed`` ends the episode; **EOF without closed is a transport
    failure**, never a fabricated terminal record;
  * timeout / forced kills are reported separately from natural completions.
"""

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import protocol, recording
from .codec import ChunkError, IncrementalAssembler
from .policy import ScriptedReflex
from .protocol import NeedKey, Request, Snapshot
from .providers import ProviderConfig, ReflexContext
from .state import EpisodeMemory


class _ProtocolFailure(Exception):
    pass


@dataclass
class EpisodeResult(object):
    index: int
    spawn_ok: bool = False
    closed: bool = False
    eof: bool = False
    forced_kill: bool = False
    protocol_failure: Optional[str] = None
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
                 read_slack: float = 30.0, max_retries: int = 3):
        self.config = config
        self.paths = paths
        self.output_dir = output_dir
        self.episode_timeout = episode_timeout
        self.read_slack = read_slack
        self.max_retries = max_retries
        os.makedirs(output_dir, mode=0o700, exist_ok=True)

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
        except OSError as exc:
            result.protocol_failure = "spawn failed: %s" % exc
            result.stop_reason = "spawn-failure"
        finally:
            self._reap(proc, result)
            shutil.rmtree(priv, ignore_errors=True)
            meta = {
                "config": _safe_config(self.config),
                "episode_timeout": self.episode_timeout,
                "stop_reason": result.stop_reason,
                "game_outcome": result.outcome,
                "closed": result.closed,
                "eof": result.eof,
                "forced_kill": result.forced_kill,
                "protocol_failure": result.protocol_failure,
                "ticks": result.ticks,
                "needs": result.needs,
                "invalids": result.invalids,
                "returncode": result.returncode,
            }
            rec.finalize(meta)
            result.recording_complete = not rec.incomplete
        return result

    # -- process ---------------------------------------------------------
    def _spawn(self, priv):
        argv = [self.paths.runner, "--worker", self.paths.worker,
                "--private-root", priv, "--data", self.paths.data]
        if self.paths.sysconf:
            argv += ["--sysconf", self.paths.sysconf]
        argv += ["--deadline", str(int(self.episode_timeout + self.read_slack))]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        proc._auto_stderr = _StderrDrain(proc.stderr)
        proc._auto_stderr.start()
        return proc

    def _reap(self, proc, result: EpisodeResult) -> None:
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            result.forced_kill = True
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        drain = getattr(proc, "_auto_stderr", None)
        if drain is not None:
            drain.join(timeout=2)
            result.stderr_tail = drain.text()[-1000:]
        result.returncode = proc.returncode


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


class _EpisodeRunner(object):
    def __init__(self, controller, proc, rec, result):
        self.c = controller
        self.proc = proc
        self.rec = rec
        self.result = result
        self.asm = IncrementalAssembler()
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
        self.last_seq = 0
        self.closed = False
        self.deadline = 0.0
        self.action_ordinal = 0

    # -- top loop --------------------------------------------------------
    def run(self):
        self.deadline = time.monotonic() + self.c.episode_timeout
        try:
            while not self.closed:
                if self.pending:
                    if self.req.pages_declared and not self.req.pages_complete():
                        self._request_pages()
                        if not self._pump():
                            break
                        continue
                    self._decide_and_send()
                    continue
                if not self._pump():
                    break
        except _ProtocolFailure as exc:
            self.result.protocol_failure = str(exc)
            self.result.stop_reason = "protocol-failure"
        except TimeoutError:
            self.result.timed_out = True
            self.result.stop_reason = "episode-timeout"
        self._finish()

    def _finish(self):
        self.result.ticks = self.tick
        self.result.needs = self._needs
        self.result.invalids = len(self._invalids)
        self.result.actions = self.action_ordinal
        self.result.closed = self.closed
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

    # -- pipe ------------------------------------------------------------
    def _send(self, obj) -> str:
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return "sent"
        except (BrokenPipeError, OSError, ValueError) as exc:
            return "write-failed: %s" % exc

    def _readline(self):
        while b"\n" not in self.buf:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("episode deadline reached")
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
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def _pump(self) -> bool:
        """Read and handle one physical line.  Returns False on EOF."""
        line = self._readline()
        if line is None:
            self.result.eof = True
            return False
        self.rec.record_wire(line)
        self._handle_line(line)
        return not self.closed

    def _handle_line(self, line: bytes):
        try:
            logical = self.asm.feed(line)
        except ChunkError as exc:
            raise _ProtocolFailure("chunk stream: %s" % exc)
        except (ValueError, KeyError, TypeError) as exc:
            raise _ProtocolFailure("malformed record: %s" % exc)
        self._ack_chunk(line)
        for rec in logical:
            t = rec.get("type")
            if t == "hello":
                self.hello = rec
            elif t == "obs":
                self._on_obs(rec)
            elif t == "page":
                self.req.note_page(rec)
            elif t == "invalid":
                self._on_invalid(rec)
            elif t == "closed":
                self.closed = True
                self.reflex.on_closed()
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
        if raw.get("type") != "chunk":
            return
        key = (raw.get("rid"), raw.get("i"))
        if key in self.acked_chunks:
            return
        self.acked_chunks.add(key)
        self._send(protocol.make_ack_chunk(raw.get("rid"), raw.get("i")))

    # -- records ---------------------------------------------------------
    def _on_obs(self, rec):
        seq = rec.get("seq")
        if seq is not None and self.last_seq and seq <= self.last_seq:
            # a durable seq must strictly increase; a repeat is a protocol
            # defect in our own replay, not the engine's
            pass
        self.last_seq = seq if seq is not None else self.last_seq
        self.snap.apply(rec)
        self.mem.observe(self.snap)
        need = rec.get("need")
        self.req.begin(need, rec.get("seq"))
        self.pending = need is not None
        self.pending_need = need
        self.pending_seq = rec.get("seq")
        self.force_fallback = False
        self.retries = 0
        if need is not None:
            self._needs += 1
            self.pending_key = NeedKey(self.result.index, rec.get("seq"),
                                       need.get("id"))
        else:
            self.pending_key = None

    def _on_invalid(self, rec):
        code = rec.get("code")
        self._invalids.append(code)
        self.retries += 1
        if self.retries > self.c.max_retries:
            raise _ProtocolFailure(
                "request id %r rejected %d times (last code %r)"
                % (self.req.id, self.retries, code))
        if code == "incomplete":
            # pages were not all delivered: drop the request bookkeeping so
            # the page obligation is re-issued
            self.req.pages_delivered = {}
            self.req.requested = set()
        else:
            self.force_fallback = True
        # the engine left the SAME request outstanding: re-arm it
        self.pending = True

    # -- decisions -------------------------------------------------------
    def _request_pages(self):
        for preq in self.req.page_requests():
            self._send(preq)
            self.req.requested.add(preq["page"])

    def _decide_and_send(self):
        need = self.pending_need
        seq = self.pending_seq
        action = self._decide(need)
        err = protocol.validate_action(need, action)
        if err:
            action = self._safe_fallback(need)
            reason = "validation fallback: %s" % err
        else:
            reason = ""
        status = self._send(protocol.make_act(seq, need["id"], action))
        self.action_ordinal += 1
        self.rec.record_action(self.action_ordinal, self.rec.wire_bytes,
                               self.pending_key, action, status)
        if need.get("kind") in ("command", "key", "direction"):
            self.tick += 1
        # the request is answered but KEPT: an `invalid` for the same id
        # re-arms it (the engine leaves the request outstanding), and a new
        # obs replaces it wholesale
        self.pending = False
        self.force_fallback = False

    def _decide(self, need):
        if self.force_fallback:
            return self._safe_fallback(need)
        ctx = ReflexContext(
            episode=self.result.index, tick=self.tick, need=need,
            need_key=self.pending_key, snapshot=self.snap,
            pages=self.req.page_rows(), memory=self.mem)
        res = self.reflex.decide(ctx)
        boundaries = self.mem.boundary.check(self.mem.status)
        self.rec.record_decision(
            proposal=res.action, selected=res.action, provider=res.provider,
            reason=res.reason, boundaries=[b.eid for b in boundaries],
            latency=res.latency, usage=res.usage)
        if res.action is None:
            return self._safe_fallback(need)
        return res.action

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
    }
