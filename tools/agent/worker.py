"""The one bounded, killable provider worker process.

Handoff ("Wire and deadline design"): a blocking ``urllib`` call cannot be
interrupted in-process -- ``Future.cancel`` does not stop it -- so every
network provider call runs in a **separate process**.  The parent owns the
wall deadline and escalates TERM -> KILL; this module is the child.

Invocation is gated by a private marker (``--invoke tools.agent.worker``) so
the entry point cannot be reached by accident or by a user typing the module
name.  The job arrives as one JSON line on stdin and exactly one JSON result
line is written to stdout:

    -> {"v":1,"provider":"deepseek","url":"...","payload":{...},
        "api_key":"...","timeout":20.0,"max_bytes":65536}
    <- {"v":1,"ok":true,"status":200,"json":{...},"bytes":1234}
    <- {"v":1,"ok":false,"error":"http-401"}

Security: the API key never appears in a result, a log line or an exception
message -- only a structured category leaves this process.  The URL is
checked to be HTTPS (loopback HTTP is allowed only for the fake-endpoint
tests), credential-free and query-free, and a cross-origin redirect is
refused rather than followed.
"""

import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

INVOCATION_MARKER = "tools.agent.worker"
_PROTOCOL_VERSION = 1
MAX_JOB_BYTES = 1 << 20
LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))


class UrlError(Exception):
    """A URL that the provider allowlist refuses."""


def validate_url(url: str, allow_insecure_loopback: bool = True) -> str:
    """Return *url* if it is allowed, else raise :class:`UrlError`.

    Only HTTPS is accepted, except for a loopback HTTP URL (the fake-endpoint
    tests).  Userinfo and a query string are rejected outright -- they are the
    classic places a credential gets smuggled into a URL that then lands in a
    log.
    """
    if not isinstance(url, str) or not url:
        raise UrlError("empty url")
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise UrlError("scheme %r is not allowed" % (parts.scheme,))
    if scheme == "http":
        if not (allow_insecure_loopback
                and (parts.hostname or "").lower() in LOOPBACK_HOSTS):
            raise UrlError("plain http is allowed only on loopback")
    if not parts.hostname:
        raise UrlError("url has no host")
    if parts.username is not None or parts.password is not None:
        raise UrlError("url must not carry credentials")
    if parts.query:
        raise UrlError("url must not carry a query string")
    return url


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when the origin (scheme/host/port) is
    unchanged."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != \
                (new.scheme, new.hostname, new.port):
            raise urllib.error.HTTPError(
                newurl, code, "cross-origin redirect rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        _SameOriginRedirect(),
        urllib.request.HTTPSHandler(context=context))


def _post(url: str, payload: dict, api_key: str, timeout: float,
          max_bytes: int) -> dict:
    """Perform one bounded POST and return a result dict (never raises)."""
    try:
        url = validate_url(url)
    except UrlError as exc:
        return {"ok": False, "error": "bad-url", "detail": str(exc)}
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json",
               "User-Agent": "tools.agent.worker/1"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    opener = _opener()
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": _http_error(exc.code),
                "status": int(exc.code)}
    except socket.timeout:
        return {"ok": False, "error": "timeout"}
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        # Never surface the exception string: it may echo the URL or headers.
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            return {"ok": False, "error": "timeout"}
        return {"ok": False, "error": "network"}
    try:
        with resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read(max_bytes + 1)
    except socket.timeout:
        return {"ok": False, "error": "timeout"}
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return {"ok": False, "error": "network"}
    if len(raw) > max_bytes:
        return {"ok": False, "error": "oversized", "status": status}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "error": "malformed-json", "status": status}
    return {"ok": True, "status": status, "json": parsed, "bytes": len(raw)}


def _http_error(code: int) -> str:
    if code in (401, 403, 429):
        return "http-%d" % code
    if 400 <= code < 500:
        return "http-4xx"
    if 500 <= code < 600:
        return "http-5xx"
    return "http-error"


def run_job(job: dict) -> dict:
    """Validate and run one job, returning a protocol result dict."""
    if not isinstance(job, dict):
        return {"ok": False, "error": "bad-job"}
    url = job.get("url")
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad-job"}
    timeout = job.get("timeout", 20.0)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 20.0
    max_bytes = job.get("max_bytes", 65536)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        max_bytes = 65536
    api_key = job.get("api_key") or ""
    if not isinstance(api_key, str):
        return {"ok": False, "error": "bad-job"}
    return _post(url, payload, api_key, float(timeout), int(max_bytes))


def run_line(line: str) -> str:
    """Map one job line (or any garbage) to one result line."""
    try:
        job = json.loads(line)
    except ValueError:
        return _dump({"ok": False, "error": "bad-job"})
    try:
        result = run_job(job)
    except Exception:                        # noqa: BLE001 - never leak
        result = {"ok": False, "error": "internal"}
    result.setdefault("v", _PROTOCOL_VERSION)
    return _dump(result)


def _dump(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True) + "\n"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Private invocation marker: refuse to act as a general-purpose tool.
    if INVOCATION_MARKER not in argv:
        sys.stderr.write("worker: private entry point; invocation marker "
                         "required\n")
        return 2
    line = sys.stdin.readline(MAX_JOB_BYTES)
    if not line:
        return 0
    sys.stdout.write(run_line(line))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
