#!/usr/bin/env python3
"""CLI entrypoint for the two-tier autoplay harness.

    python3 -m tools.agent auto --episodes N --reflex scripted \\
        --strategy off --max-ticks 2000 --episode-timeout 300 \\
        --output-dir DIR

The default configuration (``--reflex scripted --strategy off``) is
completely network-free: no code path opens a socket.  A provider is only
contacted when it is both requested on the command line *and* locally
available (a key plus, for Jev, an explicit terms flag); the presence of a
key alone never opts a user into paid calls.
"""

import argparse
import os
import sys

from .controller import Controller, ControllerPaths
from .providers import ProviderConfig, reflex_provider, strategy_provider

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _default_data():
    return os.environ.get("AGENT_DATA", "/tmp/nethack-agent-data")


def build_parser():
    p = argparse.ArgumentParser(prog="tools.agent",
                                description="Autonomous NetHack play harness")
    sub = p.add_subparsers(dest="command", required=True)

    auto = sub.add_parser("auto", help="run autonomous scripted episodes")
    auto.add_argument("--episodes", type=int, default=1)
    auto.add_argument("--reflex", choices=["scripted", "jev"],
                      default="scripted",
                      help="reflex tier (scripted is always available)")
    auto.add_argument("--strategy", choices=["off", "deepseek"],
                      default="off",
                      help="strategy tier (off is completely network-free)")
    auto.add_argument("--role", default="Valkyrie")
    auto.add_argument("--max-ticks", type=int, default=2000)
    auto.add_argument("--episode-timeout", type=float, default=300.0)
    auto.add_argument("--output-dir", required=True)
    auto.add_argument("--worker", default=os.path.join(_REPO, "src/nethack"))
    auto.add_argument("--runner",
                      default=os.path.join(_REPO, "src/nethack-agent"))
    auto.add_argument("--data", default=_default_data())
    auto.add_argument("--sysconf", default=None)
    auto.add_argument("--confidence-threshold", type=float, default=0.8)
    # -- deadlines --------------------------------------------------------
    auto.add_argument("--reflex-deadline", type=float, default=0.75,
                      help="reflex decision allowance in seconds")
    auto.add_argument("--answer-deadline", type=float, default=1.0,
                      help="seconds to answer a need once its content is "
                           "complete")
    auto.add_argument("--content-deadline", type=float, default=5.0,
                      help="aggregate content/transport deadline per need")
    auto.add_argument("--strategy-deadline", type=float, default=20.0,
                      help="wall deadline for one strategy call")
    auto.add_argument("--strategy-cooldown", type=float, default=2.0,
                      help="cooldown after a strategy timeout")
    # -- strategy budget --------------------------------------------------
    auto.add_argument("--strategy-call-cap", type=int, default=8,
                      help="strategy calls per episode (default 8)")
    auto.add_argument("--postmortem-reserve", type=int, default=1,
                      help="calls held back for the postmortem")
    auto.add_argument("--token-cap", type=int, default=0,
                      help="0 disables the token cap")
    auto.add_argument("--usd-cap", type=float, default=None,
                      help="requires a configured tariff to be enforceable")
    auto.add_argument("--deepseek-price-in", type=float, default=None,
                      help="operator-configured USD per Mtok prompt tokens")
    auto.add_argument("--deepseek-price-out", type=float, default=None,
                      help="operator-configured USD per Mtok completion "
                           "tokens")
    auto.add_argument("--reflex-call-cap", type=int, default=0,
                      help="bound on paid reflex (Jev) calls; 0 disables Jev")
    # -- boundaries -------------------------------------------------------
    auto.add_argument("--boundary-cooldown-ticks", type=int, default=50)
    auto.add_argument("--boundary-cooldown-wall", type=float, default=5.0)
    auto.add_argument("--boundary-emergency-wall", type=float, default=2.0)
    auto.add_argument("--low-confidence-needs", type=int, default=3)
    # -- DeepSeek ---------------------------------------------------------
    auto.add_argument("--deepseek-model", default="deepseek-v4-flash")
    auto.add_argument("--deepseek-base-url",
                      default="https://api.deepseek.com")
    auto.add_argument("--deepseek-key-file", default=None,
                      help="0600 file holding DEEPSEEK_API_KEY")
    auto.add_argument("--deepseek-max-tokens", type=int, default=400)
    # -- Jev (ships disabled) --------------------------------------------
    auto.add_argument("--jev-key-file", default=None)
    auto.add_argument("--jev-base-url", default=None)
    auto.add_argument("--i-accept-jev-terms", dest="jev_accept_terms",
                      action="store_true",
                      help="acknowledge the Jev terms (required for --reflex "
                           "jev; the real service still is not contacted)")
    return p


def _config_from_args(a) -> ProviderConfig:
    return ProviderConfig(
        reflex=a.reflex, strategy=a.strategy, role=a.role,
        max_ticks=a.max_ticks,
        confidence_threshold=a.confidence_threshold,
        strategy_call_cap=a.strategy_call_cap,
        postmortem_reserve=a.postmortem_reserve,
        reflex_deadline=a.reflex_deadline,
        answer_deadline=a.answer_deadline,
        content_deadline=a.content_deadline,
        strategy_deadline=a.strategy_deadline,
        strategy_cooldown=a.strategy_cooldown,
        deepseek_model=a.deepseek_model,
        deepseek_base_url=a.deepseek_base_url,
        deepseek_key_file=a.deepseek_key_file,
        deepseek_max_tokens=a.deepseek_max_tokens,
        jev_key_file=a.jev_key_file,
        jev_base_url=a.jev_base_url,
        jev_accept_terms=a.jev_accept_terms,
        token_cap=a.token_cap,
        usd_cap=a.usd_cap,
        deepseek_price_in=a.deepseek_price_in,
        deepseek_price_out=a.deepseek_price_out,
        reflex_call_cap=a.reflex_call_cap,
        boundary_cooldown_ticks=a.boundary_cooldown_ticks,
        boundary_cooldown_wall=a.boundary_cooldown_wall,
        boundary_emergency_wall=a.boundary_emergency_wall,
        low_confidence_needs=a.low_confidence_needs)


def episode_ok(r) -> bool:
    """The campaign success predicate for one episode.

    ``closed`` alone is best-effort evidence, not proof: success also requires
    a clean spawn, no forced kill or teardown failure, no unanswered request,
    no protocol/transport/deadline failure, a zero launcher exit status and a
    complete recording.
    """
    return (r.spawn_ok and r.closed and not r.forced_kill and not r.eof
            and not r.unanswered and not r.teardown_failure
            and r.protocol_failure is None and r.failure_reason is None
            and r.returncode == 0 and r.recording_complete)


def cmd_auto(a) -> int:
    config = _config_from_args(a)
    sysconf = a.sysconf or os.path.join(a.data, "sysconf")
    paths = ControllerPaths(worker=a.worker, runner=a.runner, data=a.data,
                            sysconf=sysconf)

    reflex = reflex_provider(config)
    strategy = strategy_provider(config)
    for name, prov in (("reflex", reflex), ("strategy", strategy)):
        av = prov.available(config)
        print("provider %s: %s" % (name, "enabled" if av.enabled
                                   else "disabled (%s)" % av.reason))
    if config.reflex != "scripted" and not reflex.available(config).enabled:
        print("error: --reflex %s is unavailable: %s"
              % (config.reflex, reflex.available(config).reason),
              file=sys.stderr)
        return 2
    if config.strategy != "off" and not strategy.available(config).enabled:
        print("error: --strategy %s is unavailable: %s"
              % (config.strategy, strategy.available(config).reason),
              file=sys.stderr)
        return 2

    controller = Controller(config, paths, a.output_dir,
                            episode_timeout=a.episode_timeout)
    results = controller.run_campaign(a.episodes)

    failures = 0
    for r in results:
        # A closed episode is a success only if nothing was left unanswered,
        # the transport was clean, the launcher exited zero and the recording
        # is complete.  `closed` alone is best-effort evidence, not proof.
        ok = episode_ok(r)
        if not ok:
            failures += 1
        print("episode %d: stop=%s outcome=%s ticks=%d needs=%d actions=%d "
              "invalids=%d closed=%s unanswered=%s recording_complete=%s "
              "rc=%s boundaries=%d strategy_calls=%d"
              % (r.index, r.stop_reason, r.outcome, r.ticks, r.needs,
                 r.actions, r.invalids, r.closed, r.unanswered,
                 r.recording_complete, r.returncode, r.boundaries,
                 r.strategy_calls))
        if r.protocol_failure:
            print("  protocol failure: %s" % r.protocol_failure)
        if r.failure_reason:
            print("  failure: %s" % r.failure_reason)
        if r.teardown_failure:
            print("  teardown failure: the launcher subtree did not exit")
        if not r.recording_complete:
            print("  recording incomplete")
        if r.stderr_tail.strip():
            print("  stderr tail: %s" % r.stderr_tail.strip()
                  .replace("\n", " | ")[-300:])
    print("campaign: %d episode(s), %d failure(s)" % (len(results), failures))
    return 1 if failures else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.command == "auto":
        return cmd_auto(a)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
