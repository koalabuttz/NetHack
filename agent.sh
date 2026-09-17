#!/bin/sh
# Convenience front end for the NetHack headless agent interface.
#
#   ./agent.sh build        build/refresh the agent binaries (automatic on demand)
#   ./agent.sh auto OPTS    run autonomous scripted episodes (tools/agent)
#   ./agent.sh watch [N]    watch N scripted episodes live (default 1)
#   ./agent.sh play [N]     run N scripted episodes headlessly
#   ./agent.sh serve        expose the JSON wire on stdin/stdout (LLM harness mode)
#   ./agent.sh replay FILE  replay a saved spectate transcript
#
# The wire protocol is specified in doc/agent-interface.md; driving it from
# an LLM is described in doc/agent-llm-quickstart.md, and the autonomous
# harness in doc/agent-autoplay.md.  Data is staged once into $AGENT_DATA
# (default /tmp/nethack-agent-data); episodes run in private temp directories
# that are cleaned up automatically.

set -e
cd "$(dirname "$0")"

BIN=src/nethack
RUNNER=src/nethack-agent
DATA=${AGENT_DATA:-/tmp/nethack-agent-data}

is_agent_build() {
    [ -x "$BIN" ] && [ -x "$RUNNER" ] \
        && nm "$BIN" 2>/dev/null | grep -q agent_procs \
        && ! ldd "$BIN" 2>/dev/null | grep -q ncurses
}

build() {
    if ! is_agent_build; then
        echo "agent.sh: building agent binaries (a few minutes)..." >&2
        make spotless >/dev/null
        (cd sys/unix && sh setup.sh hints/linux.500)
        make WANT_WIN_AGENT=1 WANT_DEFAULT=agent WANT_AGENT_STRICT=1 all
    fi
}

stage() {
    if [ ! -f "$DATA/nhdat" ] || [ ! -f "$DATA/sysconf" ]; then
        make agent-test-data AGENT_TEST_DATA="$DATA" >/dev/null
        echo "agent.sh: staged game data in $DATA" >&2
    fi
}

new_episode_args() {
    PRIV=$(mktemp -d "${TMPDIR:-/tmp}/agent-ep.XXXXXX")
}

watch() {
    build
    stage
    n=${1:-1}
    i=1
    while [ "$i" -le "$n" ]; do
        new_episode_args
        # Frames go to the terminal when there is one; otherwise the default
        # stderr rendering applies (it may coalesce or disable under
        # backpressure -- by design, never affecting the wire).
        if [ -t 2 ]; then
            SPECTATE_RENDER_FD=tty \
                python3 test/agent/driver.py play \
                --runner test/agent/spectate.py \
                --worker "$PWD/$BIN" --private-root "$PRIV" \
                --data "$DATA" --sysconf "$DATA/sysconf"
        else
            echo "agent.sh: no terminal on stderr; running headless" \
                "(live frames need ./agent.sh watch from a terminal)" >&2
            python3 test/agent/driver.py play \
                --runner "$RUNNER" \
                --worker "$PWD/$BIN" --private-root "$PRIV" \
                --data "$DATA" --sysconf "$DATA/sysconf"
        fi
        rm -rf "$PRIV"
        i=$((i + 1))
    done
}

play() {
    build
    stage
    n=${1:-1}
    i=1
    while [ "$i" -le "$n" ]; do
        new_episode_args
        python3 test/agent/driver.py play \
            --runner "$RUNNER" \
            --worker "$PWD/$BIN" --private-root "$PRIV" \
            --data "$DATA" --sysconf "$DATA/sysconf"
        rm -rf "$PRIV"
        i=$((i + 1))
    done
}

serve() {
    build
    stage
    new_episode_args
    # One episode per invocation: the harness reads/writes JSON lines on
    # stdin/stdout until the bare {"type":"closed"} record, then calls
    # ./agent.sh serve again for a fresh episode (see the quickstart doc).
    status=0
    "$RUNNER" --worker "$PWD/$BIN" --private-root "$PRIV" \
        --data "$DATA" --sysconf "$DATA/sysconf" || status=$?
    rm -rf "$PRIV"
    return "$status"
}

replay() {
    [ -f "$1" ] || { echo "agent.sh: replay needs a transcript file" >&2; exit 2; }
    exec python3 test/agent/spectate.py replay "$1"
}

auto() {
    build
    stage
    # The autonomous harness is a package, not a single script: it owns the
    # game pipe itself and writes ep-N recordings into --output-dir.  This
    # wave ships scripted-only play; the strategy tier is a later addition.
    python3 -m tools.agent auto "$@" \
        --worker "$PWD/$BIN" --runner "$RUNNER" \
        --data "$DATA" --sysconf "$DATA/sysconf"
}

case "${1:-help}" in
    build)  build ;;
    auto)   shift; auto "$@" ;;
    watch)  shift; watch "$@" ;;
    play)   shift; play "$@" ;;
    serve)  serve ;;
    replay) shift; replay "$@" ;;
    *)
        sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
        ;;
esac
