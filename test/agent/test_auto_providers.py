"""Wave-2 tests: boundaries, directives, budgets, workers, providers.

Run from the repository root:

    python3 -m unittest discover -s test/agent -p 'test_auto*.py'

Every network test talks to a **fake HTTP endpoint on loopback** driven by
the real worker process, so the whole path -- spawn, bounded POST, size and
redirect guards, deadline, kill, reap -- is exercised without touching a
real provider.  The default configuration is asserted to be network-free.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_auto import (BLANK, CLOSED, HELLO, WireHarness,  # noqa: E402
                       _FakeStderr, _FakeStdin, _FakeStdout, _line,
                       _parse_actions, _read_jsonl, ack_need, hello)
from tools.agent import (budget, controller, directives, events,  # noqa
                         policy, protocol, providers, state, worker)
from tools.agent.providers import (Availability, ProviderConfig,  # noqa
                                   ReflexContext, StrategyContext,
                                   StrategyResult)

DSEV = directives


# ------------------------------------------------------------- fake endpoint

def _chat_body(content, prompt_tokens=10, completion_tokens=5):
    if not isinstance(content, str):
        content = json.dumps(content)
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }).encode("utf-8")


def _ok_directives(goals=("survive",), ttl=50):
    return {"schema_version": 1, "goals": list(goals), "ttl": ttl,
            "explanation": "fake"}


class FakeEndpoint(object):
    """A loopback HTTP endpoint with controllable timing and responses."""

    def __init__(self, responder=None):
        self.responder = responder or (
            lambda path, body: (200, _chat_body(_ok_directives())))
        self.requests = []
        self.delay = 0.0
        self.drip = 0.0
        self.hang = 0.0
        handler = _make_handler(self)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base_url = "http://127.0.0.1:%d" % self.port
        self._t = threading.Thread(target=self.server.serve_forever,
                                   daemon=True)
        self._t.start()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                    # noqa: BLE001
            pass


def _make_handler(owner):
    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            owner.requests.append({
                "path": self.path, "body": body,
                "auth": self.headers.get("Authorization")})
            if owner.hang:
                time.sleep(owner.hang)
                return
            if owner.delay:
                time.sleep(owner.delay)
            status, payload = owner.responder(self.path, body)
            if not isinstance(payload, bytes):
                payload = payload.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if owner.drip:
                    for i in range(0, len(payload), 16):
                        self.wfile.write(payload[i:i + 16])
                        self.wfile.flush()
                        time.sleep(owner.drip)
                else:
                    self.wfile.write(payload)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *a):
            pass

    return _H


# ------------------------------------------------------------- fake strategy

class FakeStrategy(providers.StrategyProvider):
    """A deterministic, in-process strategy provider (no network)."""

    name = "fake"

    def __init__(self, payload=None, available=True, delay=0.0, ok=True):
        self.payload = payload
        self._available = available
        self._delay = delay
        self._ok = ok
        self.calls = []
        self.cancelled = 0

    def available(self, config):
        return Availability(self._available,
                            "fake" if self._available else "fake disabled")

    def deliberate(self, context, deadline=0.0):
        self.calls.append(context)
        if self._delay:
            time.sleep(self._delay)
        if not self._ok or self.payload is None:
            return StrategyResult(provider="fake", reason="no plan", ok=False)
        dset = self.payload
        if not isinstance(dset, DSEV.DirectiveSet):
            dset, why = DSEV.validate_directive_set(dset)
            if dset is None:
                return StrategyResult(provider="fake",
                                      reason="invalid: %s" % why, ok=False)
        return StrategyResult(directives=[dset], provider="fake",
                              reason="directives", ok=True,
                              usage={"prompt_tokens": 5,
                                     "completion_tokens": 3,
                                     "reported": True})

    def cancel(self):
        self.cancelled += 1


class PacedProc(object):
    """A process-shaped peer that feeds records with gaps between them.

    The controller's loop only services the strategy tier at the top of each
    iteration, so pacing the wire is what makes the dispatch/finalize/activate
    sequence deterministic: a gap after a boundary gives the (instant, fake)
    strategy call time to complete before the next record is read.
    """

    def wait(self, timeout=None):
        self._t.join(timeout=timeout or 5)
        return self.returncode

    def kill(self):
        self.killed = True

    def terminate(self):
        self.killed = True

    def close(self):
        try:
            os.close(self.r)
        except OSError:
            pass


def paced(records, gaps, eof=True):
    proc = PacedProc.__new__(PacedProc)
    proc.r, proc.w = os.pipe()
    proc.stdin = _FakeStdin()
    proc.stdout = _FakeStdout(proc.r)
    proc.stderr = _FakeStderr()
    proc.returncode = 0
    proc.killed = False
    proc._records = [_line(r) for r in records]
    proc._gaps = list(gaps)
    proc._eof = eof
    proc._t = threading.Thread(target=_feed_paced, args=(proc,), daemon=True)
    proc._t.start()
    return proc


def _feed_paced(proc):
    try:
        wr = os.fdopen(proc.w, "wb", buffering=0)
        for rec in proc._records:
            wr.write(rec)
            time.sleep(proc._gaps.pop(0) if proc._gaps else 0.0)
        if proc._eof:
            wr.close()
    except (OSError, IndexError):
        pass


def st_obs(seq, need=None, dlvl=None, hp=None, hp_max=None, hunger=None,
           msg=(), map_=None, xl=None):
    s = {}
    if hp is not None:
        s["hitpoints"] = {"text": str(hp)}
    if hp_max is not None:
        s["hitpoints-max"] = {"text": str(hp_max)}
    if hunger is not None:
        s["hunger"] = {"text": hunger}
    if dlvl is not None:
        s["dungeon-level"] = {"text": dlvl}
    if xl is not None:
        s["experience-level"] = {"text": str(xl)}
    return {"v": 1, "ch": "player", "type": "obs", "d": seq, "seq": seq,
            "base": None, "s": s, "cond": [], "pal": BLANK,
            "map": map_ or [], "cur": None, "msg": list(msg), "hist": [],
            "windows": [], "need": need}


def command_need(i):
    return {"id": i, "kind": "command", "prompt": ""}


# ============================================================ directives

class TestDirectiveValidation(unittest.TestCase):
    def test_a_valid_set_is_accepted(self):
        dset, why = DSEV.validate_directive_set({
            "schema_version": 1, "goals": ["survive", "acquire_food"],
            "target": [10, 5], "risk": 0.25, "ttl": 30,
            "preconditions": ["hero_known"], "explanation": "eat then run"})
        self.assertEqual(why, "")
        self.assertEqual(dset.goals, ("survive", "acquire_food"))
        self.assertEqual(dset.target, (10, 5))
        self.assertEqual(dset.ttl, 30)

    def test_minimal_set_defaults(self):
        dset, why = DSEV.validate_directive_set({"goals": ["recover"]})
        self.assertEqual(why, "")
        self.assertEqual(dset.schema_version, 1)
        self.assertEqual(dset.risk, 0.0)
        self.assertEqual(dset.ttl, 1)

    def test_rejections(self):
        bad = [
            ({"goals": ["fly"]}, "unknown goal"),
            ({"goals": []}, "non-empty"),
            ({"goals": ["survive"], "key": 255}, "looks like wire content"),
            ({"goals": ["survive"], "menu": "m1"}, "looks like wire content"),
            ({"goals": ["survive"], "action": "x"},
             "looks like wire content"),
            ({"goals": ["survive"], "extra": 1}, "unexpected field"),
            ({"goals": ["survive"], "risk": 2}, "[0,1]"),
            ({"goals": ["survive"], "risk": float("nan")}, "[0,1]"),
            ({"goals": ["survive"], "ttl": 0}, "ttl"),
            ({"goals": ["survive"], "ttl": 99999}, "ttl"),
            ({"goals": ["survive"], "target": [0, 5]}, "outside"),
            ({"goals": ["survive"], "target": [1]}, "[x,y]"),
            ({"goals": ["survive"], "preconditions": ["immortal"]},
             "unknown precondition"),
            ({"goals": ["survive"], "explanation": "x" * 500}, "too long"),
            ({"goals": ["survive"], "schema_version": 2}, "schema_version"),
            ({"survive": True}, "unexpected field"),
            ({"goals": ["survive", "survive"]}, "duplicate"),
            ("nope", "not an object"),
        ]
        for obj, needle in bad:
            with self.subTest(obj=obj):
                dset, why = DSEV.validate_directive_set(obj)
                self.assertIsNone(dset)
                self.assertIn(needle, why)

    def test_preconditions_are_evaluated(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["survive"], "preconditions": ["hp_below_half"]})
        st = DSEV.PreconditionState(hero_known=True, hp_known=True,
                                    hp_frac=0.4)
        self.assertTrue(DSEV.preconditions_met(dset, st))
        st.hp_frac = 0.9
        self.assertFalse(DSEV.preconditions_met(dset, st))


class TestDirectiveBook(unittest.TestCase):
    def _dset(self, goals=("survive",), ttl=5):
        dset, why = DSEV.validate_directive_set(
            {"goals": list(goals), "ttl": ttl})
        self.assertEqual(why, "")
        return dset

    def _fresh(self):
        return DSEV.PreconditionState(hero_known=True, inventory_fresh=True)

    def test_activate_and_read(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(), tick=3, level="Dlvl:1")
        active = book.active(tick=4, level="Dlvl:1", st=self._fresh())
        self.assertIsNotNone(active)

    def test_level_change_expires(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(), tick=3, level="Dlvl:1")
        self.assertIsNone(book.active(tick=4, level="Dlvl:2",
                                      st=self._fresh()))
        self.assertIn("expired", [e["state"] for e in book.events])

    def test_ttl_expires(self):
        book = DSEV.DirectiveBook()
        book.activate(self._dset(ttl=3), tick=0, level="Dlvl:1")
        self.assertIsNotNone(book.active(tick=3, level="Dlvl:1",
                                         st=self._fresh()))
        self.assertIsNone(book.active(tick=4, level="Dlvl:1",
                                      st=self._fresh()))

    def test_precondition_failure_expires(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["survive"], "ttl": 10,
             "preconditions": ["hungry"]})
        book = DSEV.DirectiveBook()
        book.activate(dset, tick=0, level="Dlvl:1")
        st = self._fresh()
        st.hungry = False
        self.assertIsNone(book.active(tick=1, level="Dlvl:1", st=st))

    def test_view_predicates(self):
        dset, _ = DSEV.validate_directive_set(
            {"goals": ["explore_frontier", "acquire_food"]})
        view = DSEV.DirectiveView(dset, 1)
        self.assertTrue(view.prefers_frontier())
        self.assertFalse(view.prefers_stairs())
        self.assertTrue(view.wants_food())
        self.assertEqual(view.top_goal(), "explore_frontier")
        empty = DSEV.DirectiveView(None)
        self.assertFalse(empty.active)
        self.assertEqual(empty.goals, ())


# ============================================================ boundaries

class TestBoundaryDetector(unittest.TestCase):
    def setUp(self):
        self.d = events.BoundaryDetector()

    def check(self, st=None, **kw):
        return self.d.check(st if st is not None else St(), **kw)

    def test_initial_level_then_change_then_return(self):
        b = self.check(st=St(dlvl="Dlvl:1"))
        self.assertEqual([x.reason for x in b], ["initial-level"])
        b = self.check(st=St(dlvl="Dlvl:2"))
        self.assertEqual([x.reason for x in b], ["level-change"])
        b = self.check(st=St(dlvl="Dlvl:1"))
        self.assertEqual([x.reason for x in b], ["level-change"])
        # distinct ids per occurrence
        self.assertEqual(len({x.eid for x in b}), 1)

    def test_repeated_snapshot_yields_no_events(self):
        first = self.check(st=St(dlvl="Dlvl:1", hp=10, hp_max=20,
                                 hunger="Hungry"),
                           classes={"d"}, messages=["You see here a scroll."])
        self.assertGreater(len(first), 0)
        for _ in range(5):
            self.assertEqual(self.check(
                st=St(dlvl="Dlvl:1", hp=10, hp_max=20, hunger="Hungry"),
                classes={"d"}, messages=["You see here a scroll."]), [])
        # a replay of the same history is likewise not new
        self.assertEqual(self.check(st=St(dlvl="Dlvl:1")), [])

    def test_hp_crisis_hysteresis(self):
        self.check(st=St(hp=80, hp_max=100))
        crisis = self.check(st=St(hp=29, hp_max=100))
        self.assertEqual([x.reason for x in crisis], ["hp-crisis"])
        self.assertTrue(crisis[0].severe)
        # staying low does not re-fire
        self.assertEqual(self.check(st=St(hp=25, hp_max=100)), [])
        # rearm above 50% is silent
        self.assertEqual(self.check(st=St(hp=60, hp_max=100)), [])
        second = self.check(st=St(hp=10, hp_max=100))
        self.assertEqual([x.reason for x in second], ["hp-crisis"])
        self.assertNotEqual(crisis[0].eid, second[0].eid)

    def test_hunger_worsens_only(self):
        self.assertEqual([x.reason for x in
                          self.check(st=St(hunger="Hungry"))],
                         ["hunger-hungry"])
        with self.subTest(state="repeat"):
            self.assertEqual(self.check(st=St(hunger="Hungry")), [])
        weak = self.check(st=St(hunger="Weak"))
        self.assertEqual([x.reason for x in weak], ["hunger-weak"])
        faint = self.check(st=St(hunger="Fainting"))
        self.assertEqual([x.reason for x in faint], ["hunger-fainting"])
        self.assertTrue(faint[0].severe)
        # recovery then re-worsening to an already-seen stage is not worse
        self.assertEqual(self.check(st=St(hunger="")), [])
        self.assertEqual(self.check(st=St(hunger="Weak")), [])

    def test_novelty_class_once(self):
        b = self.check(classes={"d", "f"})
        self.assertEqual(sorted(x.reason for x in b),
                         ["novelty-class", "novelty-class"])
        self.assertEqual(len({x.eid for x in b}), 2)
        self.assertEqual(self.check(classes={"d"}), [])
        more = self.check(classes={"d", "f", "&"})
        self.assertEqual([x.eid for x in more], ["class:&"])

    def test_novelty_item_once(self):
        b = self.check(messages=["You see here a scroll.",
                                 "You see here a potion."])
        self.assertEqual(len(b), 2)   # two distinct descriptions
        kinds = [x.reason for x in b]
        self.assertEqual(kinds, ["novelty-item", "novelty-item"])
        self.assertEqual(self.check(messages=["You see here a scroll.",
                                              "You see here a potion."]), [])

    def test_inventory_baseline_then_change(self):
        self.assertEqual(self.check(inventory_sig=("a food ration",)), [])
        self.assertEqual(self.check(inventory_sig=("a food ration",)), [])
        b = self.check(inventory_sig=("a food ration", "a dagger"))
        self.assertEqual([x.reason for x in b], ["inventory-change"])

    def test_failed_food_buckets(self):
        self.assertEqual(self.check(failed_food=1), [])
        first = self.check(failed_food=2)
        self.assertEqual([x.eid for x in first], ["food-failed:1"])
        self.assertEqual(self.check(failed_food=3), [])
        second = self.check(failed_food=4)
        self.assertEqual([x.eid for x in second], ["food-failed:2"])

    def test_low_confidence_buckets(self):
        self.assertEqual(self.check(low_conf_streak=2,
                                    low_conf_threshold=3), [])
        b = self.check(low_conf_streak=3, low_conf_threshold=3)
        self.assertEqual([x.eid for x in b], ["low-confidence:1"])
        self.assertEqual(self.check(low_conf_streak=5,
                                    low_conf_threshold=3), [])
        b = self.check(low_conf_streak=6, low_conf_threshold=3)
        self.assertEqual([x.eid for x in b], ["low-confidence:2"])

    def test_closed_once(self):
        self.assertEqual([x.reason for x in self.check(closed=True)],
                         ["closed"])
        self.assertEqual(self.check(closed=True), [])


class St(object):
    def __init__(self, hp=None, hp_max=None, hunger="", dlvl="", time=None,
                 gold=None, level=None):
        self.hp = hp
        self.hp_max = hp_max
        self.hunger = hunger
        self.dlvl = dlvl
        self.time = time
        self.gold = gold
        self.level = level


# ============================================================ queue

class TestBoundaryQueue(unittest.TestCase):
    def setUp(self):
        self.led = budget.BudgetLedger()
        self.q = events.BoundaryQueue(cooldown_ticks=50, cooldown_wall=5.0,
                                      emergency_wall=2.0, ledger=self.led)

    def b(self, reason, eid, severe=False):
        return events.Boundary(reason, eid, severe)

    def test_coalescing_one_pending_set(self):
        self.q.submit([self.b("hp-crisis", "hp-crisis:1", True),
                       self.b("hunger-weak", "hunger:Weak", True)], 1)
        pending = self.q.ready(1, 100.0)
        self.assertIsNotNone(pending)
        self.assertEqual(sorted(pending.eids),
                         ["hp-crisis:1", "hunger:Weak"])
        self.assertTrue(pending.severe)
        self.assertEqual(self.led.boundaries_queued, 2)

    def test_normal_cooldown_needs_ticks_and_wall(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.assertIsNotNone(self.q.ready(0, 100.0))
        self.q.mark_dispatched(0, 100.0)
        self.q.finish(True)
        self.q.submit([self.b("level-change", "level:2:1")], 10)
        # only 10 ticks elapsed: still on cooldown
        self.assertIsNone(self.q.ready(10, 106.0))
        # enough ticks but not enough wall time
        self.assertIsNone(self.q.ready(60, 101.0))
        self.assertIsNotNone(self.q.ready(60, 106.0))

    def test_severe_bypasses_normal_cooldown(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 100.0)
        self.q.finish(True)
        self.q.submit([self.b("hp-crisis", "hp-crisis:1", True)], 1)
        # emergency cooldown still applies
        self.assertIsNone(self.q.ready(1, 100.5))
        self.assertIsNotNone(self.q.ready(1, 102.5))

    def test_suppress_and_expire_states(self):
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 0)
        self.assertEqual(self.q.suppress("cap"), ["hunger:Hungry"])
        self.assertEqual(self.led.boundaries_suppressed, 1)
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.expire("level-changed")
        self.assertEqual(self.led.boundaries_expired, 1)
        states = self.q.states()
        self.assertIn("suppressed", states)
        self.assertIn("expired", states)

    def test_only_one_call_in_flight(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 0.0)
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 1)
        self.assertIsNone(self.q.ready(100, 100.0))   # in flight

    def test_applied_and_expired_accounting(self):
        self.q.submit([self.b("initial-level", "level:1:1")], 0)
        self.q.mark_dispatched(0, 0.0)
        self.q.finish(True)
        self.assertEqual(self.led.boundaries_applied, 1)
        self.q.submit([self.b("hunger-hungry", "hunger:Hungry")], 1)
        self.q.mark_dispatched(1, 1.0)
        self.q.finish(False)
        self.assertEqual(self.led.boundaries_expired, 1)


# ============================================================ budget

class TestBudgetLedger(unittest.TestCase):
    def test_postmortem_reserve_limits_play_to_seven(self):
        led = budget.BudgetLedger(strategy_cap=8, postmortem_reserve=1)
        for _ in range(7):
            self.assertTrue(led.reserve_strategy())
            led.commit_strategy({})
        self.assertFalse(led.strategy_available())
        self.assertFalse(led.reserve_strategy())
        # the reserved slot is still spendable for the postmortem
        self.assertTrue(led.strategy_available(postmortem=True))
        self.assertTrue(led.reserve_strategy(postmortem=True))
        led.commit_strategy({}, postmortem=True)
        self.assertEqual(led.strategy_dispatched, 8)
        self.assertEqual(led.postmortem_dispatched, 1)
        self.assertFalse(led.strategy_available(postmortem=True))

    def test_timeout_is_billed(self):
        led = budget.BudgetLedger(strategy_cap=8, postmortem_reserve=1)
        led.reserve_strategy()
        led.commit_strategy(None)         # no usage returned
        self.assertEqual(led.strategy_dispatched, 1)
        self.assertEqual(led.strategy_reserved, 0)

    def test_release_drops_an_undelivered_reservation(self):
        led = budget.BudgetLedger()
        led.reserve_strategy()
        led.release_strategy()
        self.assertEqual(led.strategy_reserved, 0)
        self.assertEqual(led.strategy_dispatched, 0)

    def test_tariff_prices_reported_usage(self):
        led = budget.BudgetLedger(
            tariff=budget.Tariff(prompt_per_mtok=1.0,
                                 completion_per_mtok=2.0))
        led.add_usage({"prompt_tokens": 1000000, "completion_tokens": 500000,
                       "reported": True})
        self.assertAlmostEqual(led.estimated_usd, 1.0 + 1.0)

    def test_no_tariff_is_counted_as_unknown_price(self):
        led = budget.BudgetLedger()
        led.add_usage({"prompt_tokens": 10, "reported": True})
        self.assertEqual(led.estimated_usd, 0.0)
        self.assertEqual(led.unknown_price_calls, 1)

    def test_usd_cap_disables_dispatch(self):
        led = budget.BudgetLedger(
            strategy_cap=100, postmortem_reserve=0, usd_cap=0.5,
            tariff=budget.Tariff(1.0, 1.0))
        led.add_usage({"prompt_tokens": 1000000, "completion_tokens": 0,
                       "reported": True})
        self.assertFalse(led.strategy_available())

    def test_reflex_paid_bound_is_separate(self):
        led = budget.BudgetLedger(reflex_cap=2)
        self.assertTrue(led.reserve_reflex_paid())
        self.assertTrue(led.reflex_paid_available())
        led.reserve_reflex_paid()
        self.assertFalse(led.reserve_reflex_paid())

    def test_report_shape(self):
        led = budget.BudgetLedger()
        led.note_boundary("detected", 3)
        led.note_boundary("queued", 2)
        out = led.as_dict()
        self.assertEqual(out["boundaries"]["detected"], 3)
        self.assertEqual(out["boundaries"]["queued"], 2)
        self.assertIn("usage", out)


# ============================================================ worker module

class TestWorkerModule(unittest.TestCase):
    def test_url_allowlist(self):
        self.assertEqual(worker.validate_url("https://api.example.com/x"),
                         "https://api.example.com/x")
        self.assertEqual(worker.validate_url("http://127.0.0.1:9/x"),
                         "http://127.0.0.1:9/x")
        for bad in ("http://api.example.com/x",
                    "ftp://api.example.com/x",
                    "https://user:pw@api.example.com/x",
                    "https://api.example.com/x?k=v",
                    ""):
            with self.subTest(url=bad):
                with self.assertRaises(worker.UrlError):
                    worker.validate_url(bad)

    def test_run_line_on_garbage(self):
        out = json.loads(worker.run_line("}{ not json"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad-job")

    def test_main_requires_the_private_marker(self):
        with mock.patch("sys.stdin"), mock.patch("sys.stdout") as out, \
                mock.patch("sys.stderr"):
            rc = worker.main([])
            self.assertEqual(rc, 2)
            self.assertFalse(out.write.called)

    def test_bad_url_reported_without_raising(self):
        out = json.loads(worker.run_line(json.dumps(
            {"url": "http://evil.example.com/", "payload": {}})))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "bad-url")


# ============================================================ supervisor

class TestWorkerSupervisor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sup-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _script(self, body):
        path = os.path.join(self.dir, "fake_worker.py")
        with open(path, "w") as fh:
            fh.write(body)
        return [sys.executable, path]

    def test_hanging_worker_is_killed_and_reaped(self):
        argv = self._script("import time\ntime.sleep(30)\n")
        sup = providers._WorkerSupervisor(argv)
        t0 = time.monotonic()
        sup.start({"v": 1}, time.monotonic() + 0.3)
        self.assertTrue(sup.wait(2.0))
        res = sup.poll()
        self.assertIsNotNone(res)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.error, "timeout")
        self.assertLess(time.monotonic() - t0, 3.0)
        # the process is reaped, not left running
        self.assertIsNotNone(sup.proc.poll())
        sup.reap()

    def test_drip_worker_times_out(self):
        argv = self._script(
            "import sys, time\n"
            "sys.stdout.write('{\"v\":1,')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n")
        sup = providers._WorkerSupervisor(argv)
        sup.start({"v": 1}, time.monotonic() + 0.3)
        self.assertTrue(sup.wait(2.0))
        res = sup.poll()
        self.assertTrue(res.timed_out)
        sup.reap()

    def test_worker_result_is_parsed(self):
        argv = self._script(
            "import sys\n"
            "sys.stdout.write('{\"v\":1,\"ok\":true,\"status\":200,"
            "\"json\":{\"a\":1}}\\n')\n")
        sup = providers._WorkerSupervisor(argv)
        sup.start({"v": 1}, time.monotonic() + 2.0)
        self.assertTrue(sup.wait(3.0))
        res = sup.poll()
        self.assertTrue(res.ok)
        self.assertEqual(res.json, {"a": 1})
        sup.reap()


# ============================================================ DeepSeek

class TestDeepSeekWorkerSupervised(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ds-test.")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)
        self.env = mock.patch.dict(os.environ,
                                   {"DEEPSEEK_API_KEY": "sk-test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cfg(self, **over):
        base = dict(strategy="deepseek", deepseek_base_url=self.ep.base_url,
                    strategy_deadline=3.0, deepseek_max_bytes=1024)
        base.update(over)
        return ProviderConfig(**base)

    def ctx(self):
        return StrategyContext(episode=1, tick=5, status_text="HP 10/20",
                               level="Dlvl:1")

    def test_valid_directives_round_trip(self):
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.directives[0].goals, ("survive",))
        self.assertEqual(res.usage.get("prompt_tokens"), 10)
        self.assertEqual(len(self.ep.requests), 1)
        self.assertTrue(self.ep.requests[0]["auth"].startswith("Bearer "))
        prov.reap()

    def test_http_401_discards_the_plan(self):
        self.ep.responder = lambda p, b: (401, b'{"error":"nope"}')
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("http-401", res.reason)
        prov.reap()

    def test_http_429_and_5xx_apply_cooldown(self):
        for status, tag in ((429, "http-429"), (503, "http-5xx")):
            with self.subTest(status=status):
                self.ep.responder = lambda p, b, s=status: (s, b"{}")
                prov = providers.DeepSeekStrategy(self.cfg())
                res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
                self.assertIn(tag, res.reason)
                self.assertGreater(prov.cooldown_until, prov.now())
                prov.reap()

    def test_malformed_json(self):
        self.ep.responder = lambda p, b: (200, b"this is not json")
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("malformed-json", res.reason)
        prov.reap()

    def test_oversized_response(self):
        big = json.dumps({"choices": [{"message": {"content": "x" * 5000}}]})
        self.ep.responder = lambda p, b: (200, big.encode())
        prov = providers.DeepSeekStrategy(self.cfg(deepseek_max_bytes=256))
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("oversized", res.reason)
        prov.reap()

    def test_invalid_directive_json_is_discarded(self):
        self.ep.responder = lambda p, b: (
            200, _chat_body({"goals": ["teleport"], "key": 7}))
        prov = providers.DeepSeekStrategy(self.cfg())
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertFalse(res.ok)
        self.assertIn("invalid-directives", res.reason)
        prov.reap()

    def test_hanging_endpoint_hits_the_deadline_and_is_killed(self):
        self.ep.hang = 30.0
        prov = providers.DeepSeekStrategy(self.cfg(strategy_deadline=0.6))
        t0 = time.monotonic()
        res = prov.deliberate(self.ctx(), time.monotonic() + 0.6)
        elapsed = time.monotonic() - t0
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "timeout")
        self.assertLess(elapsed, 3.0)
        self.assertIsNone(prov._sup)      # reaped
        prov.reap()

    def test_slow_but_within_deadline_succeeds(self):
        self.ep.delay = 0.3
        prov = providers.DeepSeekStrategy(self.cfg(strategy_deadline=3.0))
        res = prov.deliberate(self.ctx(), time.monotonic() + 3.0)
        self.assertTrue(res.ok)
        prov.reap()

    def test_no_key_is_unavailable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            prov = providers.DeepSeekStrategy(
                ProviderConfig(strategy="deepseek"))
            self.assertFalse(prov.available(prov.config).enabled)

    def test_key_file_must_be_0600(self):
        path = os.path.join(self.dir, "key")
        with open(path, "w") as fh:
            fh.write("sk-file-secret\n")
        os.chmod(path, 0o644)
        with self.assertRaises(providers.SecretError):
            providers.load_secret(path, "DEEPSEEK_API_KEY")
        os.chmod(path, 0o600)
        secret = providers.load_secret(path, "DEEPSEEK_API_KEY")
        self.assertEqual(str(secret), "<redacted>")
        self.assertEqual(repr(secret), "<redacted>")

    def test_secret_repr_is_redacted(self):
        secret = providers.Secret("sk-abc123")
        self.assertNotIn("sk-abc123", repr(secret))
        self.assertNotIn("sk-abc123", str(secret))

    def test_chat_url_is_joined_once(self):
        self.assertEqual(
            providers._chat_url("https://api.deepseek.com"),
            "https://api.deepseek.com/chat/completions")
        self.assertEqual(
            providers._chat_url("https://api.deepseek.com/chat/completions"),
            "https://api.deepseek.com/chat/completions")

    def test_payload_carries_the_model_and_untrusted_state(self):
        payload = providers.deepseek_payload("m1", self.ctx(), 123)
        self.assertEqual(payload["model"], "m1")
        self.assertEqual(payload["max_tokens"], 123)
        self.assertFalse(payload["stream"])
        joined = json.dumps(payload)
        self.assertIn("untrusted", joined.lower())


# ============================================================ Jev

class TestJevAdapter(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint()
        self.addCleanup(self.ep.close)
        self.env = mock.patch.dict(os.environ,
                                   {"JEV_API_KEY": "jev-test-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cfg(self, **over):
        base = dict(reflex="jev", jev_base_url=self.ep.base_url,
                    jev_accept_terms=True, reflex_deadline=2.0,
                    confidence_threshold=0.8)
        base.update(over)
        return ProviderConfig(**base)

    def ctx(self, need):
        return ReflexContext(episode=1, tick=1, need=need,
                             need_key=protocol.NeedKey(1, 1, need.get("id")),
                             snapshot=protocol.Snapshot(), pages=[],
                             memory=state.EpisodeMemory())

    def test_ships_disabled_without_terms(self):
        prov = providers.JevReflex(ProviderConfig(reflex="jev"))
        self.assertFalse(prov.available(prov.config).enabled)
        self.assertIn("terms", prov.available(prov.config).reason)

    def test_disabled_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            prov = providers.JevReflex(self.cfg())
            self.assertFalse(prov.available(prov.config).enabled)

    def test_disabled_without_endpoint(self):
        prov = providers.JevReflex(self.cfg(jev_base_url=None))
        self.assertFalse(prov.available(prov.config).enabled)

    def test_enabled_only_with_terms_key_and_endpoint(self):
        prov = providers.JevReflex(self.cfg(reflex_call_cap=1))
        self.assertTrue(prov.available(prov.config).enabled)

    def test_valid_choice(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 1, "confidence": 0.9}).encode())
        prov = providers.JevReflex(self.cfg())
        res = prov.decide(self.ctx(command_need(1)), time.monotonic() + 2.0)
        self.assertIsNotNone(res)
        self.assertEqual(res.confidence, 0.9)
        self.assertEqual(res.action,
                         {"key": providers.KEY_CHOICES[1][1]})
        prov.cancel()

    def test_confidence_nan_and_out_of_range_fall_back(self):
        for conf in (float("nan"), 1.5, -0.1, "high", None):
            with self.subTest(conf=conf):
                self.ep.responder = lambda p, b, c=conf: (
                    200, json.dumps({"option": 0, "confidence": c}).encode())
                prov = providers.JevReflex(self.cfg())
                res = prov.decide(self.ctx(command_need(1)),
                                  time.monotonic() + 2.0)
                self.assertIsNone(res)
                prov.cancel()

    def test_low_confidence_falls_back(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 0, "confidence": 0.2}).encode())
        prov = providers.JevReflex(self.cfg())
        self.assertIsNone(prov.decide(self.ctx(command_need(1)),
                                      time.monotonic() + 2.0))
        prov.cancel()

    def test_unknown_option_falls_back(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 999, "confidence": 0.99}).encode())
        prov = providers.JevReflex(self.cfg())
        self.assertIsNone(prov.decide(self.ctx(command_need(1)),
                                      time.monotonic() + 2.0))
        prov.cancel()

    def test_unsupported_needs_never_call_jev(self):
        prov = providers.JevReflex(self.cfg())
        for kind in ("line", "extcmd", "position"):
            need = {"id": 1, "kind": kind, "prompt": ""}
            if kind == "position":
                need.update({"x0": 1, "y0": 0, "x1": 2, "y1": 1})
            with self.subTest(kind=kind):
                self.assertIsNone(prov.decide(self.ctx(need),
                                              time.monotonic() + 2.0))
        self.assertEqual(self.ep.requests, [])
        prov.cancel()

    def test_oversized_menu_is_scripted(self):
        prov = providers.JevReflex(self.cfg())
        need = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        ctx = self.ctx(need)
        ctx.pages = [{"r": i, "text": "row", "selectable": True}
                     for i in range(200)]
        self.assertIsNone(prov.decide(ctx, time.monotonic() + 2.0))
        self.assertEqual(self.ep.requests, [])
        prov.cancel()

    def test_small_menu_choice_maps_to_a_row(self):
        self.ep.responder = lambda p, b: (
            200, json.dumps({"option": 2, "confidence": 0.95}).encode())
        prov = providers.JevReflex(self.cfg())
        need = {"id": 1, "kind": "menu", "menu": "m1", "mode": "one",
                "content": "c1", "pages": 1}
        ctx = self.ctx(need)
        ctx.pages = [{"r": 10 + i, "text": "row %d" % i, "selectable": True}
                     for i in range(4)]
        res = prov.decide(ctx, time.monotonic() + 2.0)
        self.assertEqual(res.action, {"menu": "m1", "commit": [[12, -1]]})
        prov.cancel()


# ============================================================ integration

class TestStrategyIntegration(WireHarness):
    """End-to-end controller behaviour with a local fake strategy tier."""

    def _controller_with(self, fake, config=None, **kw):
        config = config or ProviderConfig(max_ticks=200)
        ctl = controller.Controller(
            config, controller.ControllerPaths(
                worker="w", runner="r", data="d", sysconf="s"),
            self.dir, episode_timeout=kw.pop("timeout", 10.0))
        ctl._new_strategy_provider = lambda: fake
        return ctl

    def test_boundary_counts_per_kind_and_a_single_call(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(3, command_need(3), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertTrue(result.closed)
        # the strategy is not polled per command: 3 commands, 1 boundary call
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result.strategy_calls, 1)
        # boundaries: initial-level + level-change detected (plus closed)
        self.assertGreaterEqual(result.boundaries, 2)

    def test_postmortem_uses_the_reserved_slot(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=2,
                             postmortem_reserve=1, low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        # one play call plus exactly one postmortem
        budget = result.budget["strategy"]
        self.assertEqual(budget["postmortem_dispatched"], 1)
        self.assertEqual(result.strategy_calls, 2)
        self.assertTrue(fake.calls[-1].postmortem)

    def test_repeated_snapshots_produce_no_new_boundaries(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        same = dict(dlvl="Dlvl:1", hp=10, hp_max=20)
        records = [hello()]
        for seq in range(1, 6):
            records.append(st_obs(seq, command_need(seq), **same))
        records.append(CLOSED)
        proc = paced(records, [0.05] * len(records))
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        # five identical snapshots -> one initial-level event, plus the one
        # closed event; nothing else
        self.assertEqual(result.boundaries, 2)

    def test_strategy_never_emits_a_wire_action(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        ctl = self._controller_with(fake)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.5, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        acts = [a for a in _parse_actions(proc.stdin.data)
                if a.get("type") == "act"]
        proc.close()
        self.assertEqual(len(acts), 2)         # one per need, no extras
        for a in acts:
            self.assertIsNone(protocol.validate_action(
                {"kind": "command", "id": a["id"]}, a["action"]))
        # the directive set carries goals only -- no keys, no text
        for call in fake.calls:
            self.assertNotIn("key", json.dumps(call.summary))

    def test_stale_level_advice_is_discarded(self):
        fake = FakeStrategy(_ok_directives(goals=("explore_frontier",)))
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [
            hello(),
            st_obs(1, None, dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:2", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.6, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(len(fake.calls), 1)
        # the level changed between dispatch and the command boundary
        self.assertEqual(result.directives_applied, 0)
        self.assertGreaterEqual(
            result.budget["boundaries"]["expired"], 1)

    def test_cap_suppresses_further_dispatch(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, strategy_call_cap=1,
                             postmortem_reserve=0, boundary_cooldown_ticks=0,
                             boundary_cooldown_wall=0.0)
        ctl = self._controller_with(fake, config=cfg)
        records = [hello()]
        for seq in range(1, 6):
            records.append(st_obs(seq, command_need(seq),
                                  dlvl="Dlvl:%d" % seq, hp=10, hp_max=20))
        records.append(CLOSED)
        proc = paced(records, [0.05] + [0.35] * len(records))
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 1)
        self.assertGreaterEqual(
            result.budget["boundaries"]["suppressed"], 1)

    def test_repeated_identical_snapshot_has_no_boundary(self):
        fake = FakeStrategy(_ok_directives())
        cfg = ProviderConfig(max_ticks=200, postmortem_reserve=0,
                             low_confidence_needs=1000)
        ctl = self._controller_with(fake, config=cfg)
        records = [hello(), st_obs(1, command_need(1), dlvl="Dlvl:1"),
                   st_obs(2, command_need(2), dlvl="Dlvl:1"),
                   CLOSED]
        proc = paced(records, [0.05, 0.2, 0.2, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(len(fake.calls), 1)

    def test_recorder_failure_disables_paid_dispatch(self):
        class _FailRec(object):
            failed = True
            wire_bytes = 0

            def record_wire(self, line):
                pass

            def record_action(self, *a, **k):
                pass

            def record_decision(self, *a, **k):
                pass

        fake = FakeStrategy(_ok_directives())
        ctl = self._controller_with(fake)
        result = controller.EpisodeResult(index=1)
        proc = paced([hello()], [0.0])
        runner = controller._EpisodeRunner(ctl, proc, _FailRec(), result)
        proc.close()
        self.assertTrue(runner.rec_healthy)
        runner._note_recorder_health()
        self.assertFalse(runner.rec_healthy)
        self.assertTrue(runner.paid_disabled)
        self.assertFalse(runner._strategy_live())
        self.assertEqual(fake.cancelled, 1)

    def test_default_config_makes_no_calls(self):
        # NullStrategy: the shipped default strategy tier is a no-op
        ctl = controller.Controller(
            ProviderConfig(strategy="off"),
            controller.ControllerPaths(worker="w", runner="r", data="d",
                                       sysconf="s"),
            self.dir, episode_timeout=5.0)
        records = [hello(), st_obs(1, command_need(1), dlvl="Dlvl:1"),
                   CLOSED]
        proc = paced(records, [0.05, 0.1, 0.05])
        ctl._spawn = lambda priv: proc
        result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 0)


class TestReflexConsultsDirectives(unittest.TestCase):
    def ctx(self, goals=None, tick=5):
        ctx_dirs = []
        if goals:
            dset, why = DSEV.validate_directive_set({"goals": list(goals)})
            self.assertEqual(why, "")
            ctx_dirs = [DSEV.DirectiveView(dset, 1)]
        mem = state.EpisodeMemory()
        return ReflexContext(
            episode=1, tick=tick, need={"id": 1, "kind": "command"},
            need_key=protocol.NeedKey(1, tick, 1),
            snapshot=protocol.Snapshot(), pages=[], memory=mem,
            directives=ctx_dirs)

    def test_disengage_raises_the_flee_threshold(self):
        reflex = policy.ScriptedReflex(
            providers.ProviderConfig(max_ticks=200))
        mem = state.EpisodeMemory()
        mem.status.hp, mem.status.hp_max = 40, 100   # 40%: not normally low
        self.assertFalse(reflex._low_hp(mem.status))
        ctx = self.ctx(["disengage"])
        ctx.memory = mem
        reflex.decide(ctx)
        self.assertTrue(reflex._low_hp(mem.status))

    def test_acquire_food_inspects_the_inventory(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        mem = state.EpisodeMemory()
        mem.hero = (3, 10)
        mem.grid[(3, 10)] = ("@", "", 0, "")
        ctx = self.ctx(["acquire_food"], tick=1000)
        ctx.memory = mem
        res = reflex.decide(ctx)
        self.assertEqual(res.action, {"key": protocol.KEY_INV})
        self.assertIn("acquire_food", res.reason)

    def test_no_directive_keeps_the_default(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        ctx = self.ctx(None)
        res = reflex.decide(ctx)
        self.assertIsNotNone(res.action)
        self.assertFalse(reflex.directives.active)

    def test_directive_never_becomes_a_key(self):
        reflex = policy.ScriptedReflex(providers.ProviderConfig())
        mem = state.EpisodeMemory()
        mem.hero = (3, 10)
        mem.grid[(3, 10)] = ("@", "", 0, "")
        ctx = self.ctx(["explore_frontier"])
        ctx.memory = mem
        res = reflex.decide(ctx)
        # whatever it returns must be a legal action for the need
        self.assertIsNone(protocol.validate_action(ctx.need, res.action))


class TestSecretsInArtifacts(WireHarness):
    def test_no_key_material_in_recordings(self):
        ep = FakeEndpoint(lambda p, b: (200, _chat_body(_ok_directives())))
        self.addCleanup(ep.close)
        secret = "sk-live-DO-NOT-LEAK-0123456789"
        config = ProviderConfig(strategy="deepseek",
                                deepseek_base_url=ep.base_url,
                                strategy_deadline=3.0,
                                postmortem_reserve=0,
                                low_confidence_needs=1000)
        ctl = controller.Controller(
            config, controller.ControllerPaths(worker="w", runner="r",
                                               data="d", sysconf="s"),
            self.dir, episode_timeout=8.0)
        records = [
            hello(),
            st_obs(1, command_need(1), dlvl="Dlvl:1", hp=10, hp_max=20),
            st_obs(2, command_need(2), dlvl="Dlvl:1", hp=10, hp_max=20),
            CLOSED,
        ]
        proc = paced(records, [0.05, 0.8, 0.05, 0.05])
        ctl._spawn = lambda priv: proc
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            result = ctl.run_episode(1)
        proc.close()
        self.assertEqual(result.strategy_calls, 1)
        leaks = []
        for name in os.listdir(self.dir):
            if name.startswith("ep-"):
                with open(os.path.join(self.dir, name), "rb") as fh:
                    data = fh.read()
                if b"sk-" in data:
                    leaks.append(name)
        self.assertEqual(leaks, [])


if __name__ == "__main__":
    unittest.main()
