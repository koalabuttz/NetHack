/* agent_input.c -- engine-facing native input primitives (plan task 8).
 *
 * Each primitive publishes the current presentation as a full durable
 * snapshot carrying one outstanding request, then reads exactly one accepted
 * action through the frozen two-phase sequence: agent_receive() applies the
 * protocol-level checks (schema, ranges, request id, kind compatibility,
 * pinned menu generation, advertised position rectangle, page completeness);
 * the primitive then performs its own native semantic validation and calls
 * agent_accept().  A semantically rejected action emits `invalid` and leaves
 * the request outstanding, so the agent may resubmit.
 *
 * No privileged input path exists: every answer becomes ordinary native
 * input.  There are no queues, continuations, or bulk conveniences.
 */

#include "hack.h"

#include "winagent.h"
#include "agent_input.h"
#include "agent_view.h"
#include "agent_types.h"
#include "agent_protocol.h"

#include "func_tab.h"

#include <string.h>

/* The parser needs caller-provided storage for menu commit rows; it is
 * preserved across the internal resets of agent_receive(). */
static struct agent_commit_row ag_commit_rows[AG_VIEW_MAX_COMMIT];
static struct agent_action ag_action;

static const char *ag_ctx = "agent input";

void
agent_port_set_input_context(const char *ctx)
{
    ag_ctx = ctx ? ctx : "agent input";
}

/* A transport/protocol failure is fatal; the message names the native
 * interaction that was outstanding. */
static void ag_fatal(const char *msg) __attribute__((noreturn));
static void
ag_fatal(const char *msg)
{
    char buf[240];

    (void) snprintf(buf, sizeof buf, "%s: %s", ag_ctx, msg);
    agent_port_fatal(buf);
}

void
agent_port_fatal_ctx(const char *msg)
{
    ag_fatal(msg);
}

static struct agent_session *
ag_sess(void)
{
    return agent_port_session();
}

static void
ag_prep(void)
{
    memset(&ag_action, 0, sizeof ag_action);
    ag_action.commit = ag_commit_rows;
    ag_action.commit_cap = AG_VIEW_MAX_COMMIT;
}

/* Read until one protocol-valid action is available.  A rejected input has
 * already emitted `invalid` and left the request outstanding, so the loop
 * simply reads again. */
static void
ag_wait(void)
{
    struct agent_session *s = ag_sess();

    for (;;) {
        enum agent_result r = agent_receive(s, &ag_action);

        if (r == AG_OK)
            return;
        if (r == AG_BAD_INPUT)
            continue; /* invalid emitted; request stays outstanding */
        ag_fatal("transport failed while awaiting input");
    }
}

static void
ag_reject(enum agent_invalid_code code)
{
    if (agent_write_invalid(ag_sess(), code) != AG_OK)
        ag_fatal("could not write an invalid record");
}

/* Commit the snapshot with this request, then read one accepted action.
 * Returns the accepted action kind. */
static enum agent_action_kind
ag_request(struct agent_need *need)
{
    ag_prep();
    if (agent_port_commit_need(need) != AG_OK)
        ag_fatal("could not publish the outstanding request");
    ag_wait();
    return ag_action.kind;
}

static void
ag_finish(void)
{
    if (agent_accept(ag_sess()) != AG_OK)
        ag_fatal("could not record the accepted action");
}

/* ---- nhgetch ------------------------------------------------------- */

int
agent_input_key(enum agent_need_kind kind, const char *prompt)
{
    struct agent_need need;

    memset(&need, 0, sizeof need);
    need.kind = kind;
    need.id = agent_port_alloc_request();
    need.prompt = prompt;
    (void) ag_request(&need);
    ag_finish();
    return (int) ag_action.key;
}

/* ---- nh_poskey ----------------------------------------------------- */

int
agent_input_poskey(coordxy *x, coordxy *y, int *mod)
{
    struct agent_need need;
    boolean in_getpos = (program_state.input_state == getposInp);

    memset(&need, 0, sizeof need);
    need.kind = in_getpos ? AG_NEED_POSITION : AG_NEED_COMMAND;
    need.id = agent_port_alloc_request();
    if (in_getpos) {
        /* the legal public map rectangle (plan section 4.5) */
        need.x0 = AG_MAP_MIN_X;
        need.y0 = AG_MAP_MIN_Y;
        need.x1 = AG_MAP_MAX_X;
        need.y1 = AG_MAP_MAX_Y;
    }
    (void) ag_request(&need);
    if (ag_action.kind == AG_ACT_POSITION) {
        if (x)
            *x = (coordxy) ag_action.px;
        if (y)
            *y = (coordxy) ag_action.py;
        if (mod)
            *mod = ag_action.pmod;
        ag_finish();
        return 0; /* native position sentinel */
    }
    {
        int ch = (int) ag_action.key;

        ag_finish();
        return ch;
    }
}

/* ---- yn_function --------------------------------------------------- */

/* The request kind for the current native yn_function context.  getdir()
 * drives yn_function with no choices while the direction input context is set
 * (src/cmd.c:4051-4057), so that interaction is published as a direction
 * request rather than a yes/no question.  Native validation is untouched. */
enum agent_need_kind
agent_input_yn_kind(void)
{
    return (program_state.input_state == getdirInp) ? AG_NEED_DIRECTION
                                                    : AG_NEED_YN;
}

int
agent_input_yn(const char *query, const char *choices, char def)
{
    struct agent_need need;
    struct agent_text vis;
    char visbuf[BUFSZ];
    char *respbuf = 0;
    boolean allow_num = FALSE, preserve_case = FALSE, unrestricted;
    size_t rlen = 0;
    char result = def;

    /* tty_yn_function resets yn_number on entry (win/tty/topl.c:390), so a
     * stale count from an earlier prompt can never leak into this one. */
    yn_number = 0L;

    if (choices) {
        rlen = strlen(choices);
        respbuf = (char *) alloc((unsigned) (rlen + 1));
        memcpy(respbuf, choices, rlen + 1);
        allow_num = (strchr(respbuf, '#') != 0);
        preserve_case = (strpbrk(respbuf, "ABCDEFGHIJKLMNOPQRSTUVWXYZ") != 0);
    }

    vis.buf = visbuf;
    vis.cap = sizeof visbuf;
    vis.len = 0;
    if (respbuf) {
        if (!agent_visible_choices(respbuf, rlen, &vis)) {
            free(respbuf);
            ag_fatal("yn choices were not projectable");
        }
        vis.buf[vis.len] = '\0';
    } else {
        visbuf[0] = '\0';
        vis.len = 0;
    }
    /* the numeric affordance is advertised only when the *displayed* choices
     * carry it; a hidden numeric suffix stays private */

    unrestricted = (respbuf == 0);
    /* an unrestricted prompt returns the raw byte unchanged: tty's
     * choices==NULL path never folds case (win/tty/topl.c:422-428); the
     * "preserve case" test applies only to a restricted prompt */
    if (unrestricted)
        preserve_case = TRUE;

    /* getdir() drives yn_function with no choices while the direction input
     * context is set (src/cmd.c:4051-4057).  Publish that interaction as a
     * direction request rather than a yes/no question; the native validation
     * is untouched. */
    /* Allocate and commit the request exactly ONCE.  A semantic rejection
     * re-enters the receive loop below on the SAME request: the frozen
     * contract leaves the outstanding request unchanged, so a fresh request
     * id, an advanced durable sequence, or a second commit would be a
     * protocol violation (section 11.3). */
    memset(&need, 0, sizeof need);
    need.kind = agent_input_yn_kind();
    need.id = agent_port_alloc_request();
    need.prompt = query;
    need.choices = respbuf ? vis.buf : (const char *) 0;
    need.def = (int) (unsigned char) def;
    need.numeric = respbuf && strchr(vis.buf, '#') != 0;

    ag_prep();
    if (agent_port_commit_need(&need) != AG_OK)
        ag_fatal("could not publish the outstanding request");

    for (;;) {
        char q;
        boolean digit_ok;

        /* read one protocol-valid action on the request committed above; a
         * rejected line emitted `invalid` and left it outstanding */
        ag_wait();

        q = (char) ag_action.key;
        if (!preserve_case)
            q = lowc(q);

        if (unrestricted) {
            result = q;
            break;
        }
        digit_ok = (allow_num && digit(q));
        if (q == '\033') {
            result = strchr(respbuf, 'q')   ? 'q'
                     : strchr(respbuf, 'n') ? 'n'
                                            : def;
            break;
        }
        if (strchr(quitchars, q)) { /* space, return, newline */
            result = def;
            break;
        }
        if (!strchr(respbuf, q) && !digit_ok) {
            /* semantic rejection: the SAME request stays outstanding */
            ag_reject(AG_INV_RANGE);
            continue;
        }
        if (q == '#' || digit_ok) {
            long value;

            if (ag_action.has_count)
                value = ag_action.yn_count;
            else if (digit(q))
                value = (long) (q - '0');
            else
                value = 0;
            if (value > 0) {
                yn_number = value;
                result = '#';
                break;
            } else if (value == 0) {
                result = 'n';
                break;
            }
            ag_reject(AG_INV_RANGE);
            continue;
        }
        result = q;
        break;
    } /* for (;;) */
    ag_finish();
    if (respbuf)
        free(respbuf);
    return (int) (unsigned char) result;
}

/* ---- getlin -------------------------------------------------------- */

void
agent_input_line(const char *query, char *buf, size_t cap)
{
    struct agent_need need;
    int maxb = (int) (cap > 0 ? cap - 1 : 0);

    if (maxb > AG_LINE_INPUT_MAX)
        maxb = AG_LINE_INPUT_MAX;
    if (maxb < 0)
        maxb = 0;

    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_LINE;
    need.id = agent_port_alloc_request();
    need.prompt = query;
    need.max = maxb;
    (void) ag_request(&need);
    if (ag_action.kind == AG_ACT_CANCEL) {
        /* native Escape result, exactly as tty_getlin leaves it */
        if (cap >= 2) {
            buf[0] = '\033';
            buf[1] = '\0';
        } else if (cap == 1) {
            buf[0] = '\0';
        }
    } else {
        size_t n = strlen(ag_action.text);

        if (n >= cap)
            n = cap ? cap - 1 : 0;
        if (cap)
            memcpy(buf, ag_action.text, n);
        if (cap)
            buf[n] = '\0';
    }
    ag_finish();
}

/* ---- get_ext_cmd --------------------------------------------------- */

int
agent_input_extcmd(void)
{
    struct agent_need need;
    char buf[BUFSZ], init[2];
    int nmatches, *ecmatches = 0;

    init[0] = extcmd_initiator();
    init[1] = '\0';

    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_EXTCMD;
    need.id = agent_port_alloc_request();
    need.prompt = init;
    need.max = (int) (BUFSZ - 1) > AG_LINE_INPUT_MAX ? AG_LINE_INPUT_MAX
                                                     : (int) (BUFSZ - 1);
    (void) ag_request(&need);
    if (ag_action.kind == AG_ACT_CANCEL) {
        buf[0] = '\033';
        buf[1] = '\0';
        ag_finish();
        return -1;
    }
    Strcpy(buf, ag_action.text);
    ag_finish();

    (void) mungspaces(buf);
    /* the native exact matcher; the returned index never crosses the wire */
    nmatches = (buf[0] == '\0' || buf[0] == '\033')
                   ? -1
                   : extcmds_match(buf, ECM_IGNOREAC | ECM_EXACTMATCH,
                                   &ecmatches);
    if (nmatches != 1)
        return -1; /* native no-match */
    return ecmatches[0];
}
