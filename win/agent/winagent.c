/* winagent.c -- the agent window port (Wave A / M1 skeleton).
 *
 * This is a trusted-bootstrap port.  It runs only when the process
 * private agent latch established before early_init(); see agent_bootstrap.c.
 *
 * Wave A implements the startup path only: the session transport, a minimal
 * init, the player-selection handshake that emits hello plus a synthetic
 * character-selection request, and the private/no-op callbacks.  Every
 * callback that would publish gameplay (rendering, input, menus, status,
 * history, text windows) is a **fail-closed stub**: it writes nothing to the
 * public channel and terminates the worker privately.  The native
 * implementations arrive in M2/M3.
 *
 * The port never writes the terminal-closure record: the trusted launcher
 * alone emits the generic `closed` object when the worker exits.
 */

#include "hack.h"

#include "winagent.h"
#include "agent_types.h"
#include "agent_protocol.h"
#include "agent_menu.h"

#include <errno.h>
#include <string.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* session transport                                                    */
/* ------------------------------------------------------------------ */

static struct agent_session agent_session;
static boolean agent_session_ready = FALSE;
static boolean agent_hello_done = FALSE;
static uint64_t agent_next_request = 1;

static long
agent_read_cb(void *ctx, char *buf, size_t cap)
{
    int fd = *(const int *) ctx;
    ssize_t n;

    if (fd < 0)
        return 0;
    do {
        n = read(fd, buf, cap);
    } while (n < 0 && errno == EINTR);
    if (n < 0)
        return -1;
    return (long) n;
}

static long
agent_write_cb(void *ctx, const char *buf, size_t len)
{
    int fd = *(const int *) ctx;
    ssize_t n;

    if (fd < 0)
        return -1;
    do {
        n = write(fd, buf, len);
    } while (n < 0 && errno == EINTR);
    if (n < 0)
        return -1;
    return (long) n;
}

static int agent_transport_fd = -1;

static void
agent_session_open(void)
{
    int fd = agent_bootstrap_fd();

    if (agent_session_ready)
        return;
    if (fd < 0)
        agent_private_fatal("agent port has no transport descriptor");
    agent_transport_fd = fd;
    agent_session_init(&agent_session, agent_read_cb, agent_write_cb,
                       &agent_transport_fd);
    agent_session_ready = TRUE;
}

/* ------------------------------------------------------------------ */
/* fail-closed stubs                                                    */
/* ------------------------------------------------------------------ */

/* A callback that is not implemented yet must not publish anything and must
 * not let the engine continue into presentation the agent cannot observe. */
static void
agent_fail_closed(const char *what)
{
    agent_private_diag(what);
    agent_private_fatal("unimplemented agent callback in Wave A");
}

/* ------------------------------------------------------------------ */
/* Wave A callbacks                                                     */
/* ------------------------------------------------------------------ */

static void
agent_init_nhwindows(int *argc, char **argv)
{
    /* No extra command-line options and no terminal.  The trusted launcher
     * owns the argument vector; this port adds nothing to it. */
    (void) argc;
    (void) argv;
    agent_session_open();
}

/* Build the synthetic full observation used at startup.  Wave A publishes no
 * gameplay: the view is the declared blank presentation. */
static void
agent_synthetic_view(struct agent_view *v)
{
    memset(v, 0, sizeof *v);
    v->full = TRUE;
    v->pal[0].ch = (uint8_t) ' ';
    v->pal[0].fg = AG_COL_NONE;
    v->pal[0].style = 0;
    v->pal[0].frame = AG_COL_NONE;
    v->npal = 1;
    v->has_cursor = FALSE;
}

static void
agent_emit_hello(void)
{
    if (agent_hello_done)
        return;
    if (agent_write_hello(&agent_session) != AG_OK)
        agent_private_fatal("could not emit the hello record");
    agent_hello_done = TRUE;
}

static void
agent_player_selection(void)
{
    struct agent_view v;
    struct agent_need need;

    agent_session_open();
    agent_emit_hello();

    /* The M1 gate allows a synthetic character-selection request: the native
     * role menu (and its legality helpers) arrive in M2.  The request is
     * emitted through the frozen P1 protocol so the framing, counters and
     * schema are the production ones. */
    agent_synthetic_view(&v);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = agent_next_request++;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = 0;
    if (agent_commit(&agent_session, &v, &need) != AG_OK)
        agent_private_fatal("could not emit the synthetic selection request");

    /* Native character selection is M2 scope. */
    agent_fail_closed("native character selection is not implemented yet");
}

static void
agent_askname(void)
{
    /* The character name is a trusted startup field, not an agent choice
     * not a prompt: in agent mode it is fixed here so startup never blocks on
     * an interactive name question.  The native prompt path is M2 scope. */
    Strcpy(svp.plname, "Agent");
}

static void
agent_get_nh_event(void)
{
    /* Event pump: never sleeps, publishes nothing. */
}

static void
agent_exit_nhwindows(const char *str)
{
    /* `str` is a player-facing farewell in other ports; in agent mode it is
     * discarded.  The launcher owns the terminal closure record. */
    (void) str;
    if (agent_session_ready) {
        agent_session_free(&agent_session);
        agent_session_ready = FALSE;
    }
}

static void
agent_suspend_nhwindows(const char *str)
{
    /* Policy-denied: never SIGSTOP. */
    (void) str;
}

static void
agent_resume_nhwindows(void)
{
    /* nothing to resume */
}

static void
agent_preference_update(const char *pref)
{
    (void) pref;
}

static void
agent_number_pad(int state)
{
    (void) state;
}

static void
agent_nhbell(void)
{
    /* no bell on a headless transport */
}

static void
agent_delay_output(void)
{
    /* delay callbacks never sleep (and are not observations) */
}

static boolean
agent_can_suspend(void)
{
    return FALSE;
}

static void
agent_raw_print(const char *str)
{
    /* Private and fail-closed: raw output is never a public message route. */
    agent_private_diag(str);
}

static void
agent_raw_print_bold(const char *str)
{
    agent_private_diag(str);
}

/* ---- fail-closed stubs (M2/M3 scope) ---- */

static int agent_window_serial = 0;

static winid
agent_create_nhwindow(int type)
{
    (void) type;
    /* M1: no window content is published.  A synthetic handle keeps the
     * window plumbing usable without claiming a renderer. */
    return (winid) (++agent_window_serial);
}

static void
agent_clear_nhwindow(winid window)
{
    (void) window;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("clear_nhwindow");
}

static void
agent_display_nhwindow(winid window, boolean blocking)
{
    (void) window;
    (void) blocking;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("display_nhwindow");
}

static void
agent_destroy_nhwindow(winid window)
{
    (void) window;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("destroy_nhwindow");
}

static void
agent_curs(winid window, int x, int y)
{
    (void) window;
    (void) x;
    (void) y;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("curs");
}

static void
agent_putstr(winid window, int attr, const char *str)
{
    (void) window;
    (void) attr;
    (void) str;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("putstr");
}

static void
agent_putmixed(winid window, int attr, const char *str)
{
    (void) window;
    (void) attr;
    (void) str;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("putmixed");
}

static void
agent_display_file(const char *fname, boolean complain)
{
    (void) fname;
    (void) complain;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("display_file");
}

static void
agent_start_menu(winid window, unsigned long mbehavior)
{
    (void) window;
    (void) mbehavior;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("start_menu");
}

static void
agent_add_menu(winid window, const glyph_info *glyphinfo,
               const ANY_P *identifier, char ch, char gch, int attr,
               int color, const char *str, unsigned int itemflags)
{
    (void) window;
    (void) glyphinfo;
    (void) identifier;
    (void) ch;
    (void) gch;
    (void) attr;
    (void) color;
    (void) str;
    (void) itemflags;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("add_menu");
}

static void
agent_end_menu(winid window, const char *prompt)
{
    (void) window;
    (void) prompt;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("end_menu");
}

static int
agent_select_menu(winid window, int how, MENU_ITEM_P **menu_list)
{
    (void) window;
    (void) how;
    if (menu_list)
        *menu_list = (MENU_ITEM_P *) 0;
    /* M1 publishes nothing here; returning "no selection" is the native
     * cancellation result, not a fabricated decision. */
    return 0;
}

static char
agent_message_menu(char let, int how, const char *mesg)
{
    (void) let;
    (void) how;
    (void) mesg;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("message_menu");
    return '\033';
}

static void
agent_mark_synch(void)
{
    /* nonblocking; never sleeps */
}

static void
agent_wait_synch(void)
{
    /* nonblocking; never sleeps */
}

#ifdef CLIPPING
static void
agent_cliparound(int x, int y)
{
    (void) x;
    (void) y;
    /* no clipping is advertised */
}
#endif

#ifdef POSITIONBAR
static void
agent_update_positionbar(char *posbar)
{
    (void) posbar;
    /* no position bar is advertised */
}
#endif

static void
agent_print_glyph(winid window, coordxy x, coordxy y,
                  const glyph_info *glyphinfo,
                  const glyph_info *bkglyphinfo)
{
    (void) window;
    (void) x;
    (void) y;
    (void) glyphinfo;
    (void) bkglyphinfo;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("print_glyph");
}

static int
agent_nhgetch(void)
{
    agent_fail_closed("nhgetch");
    return '\033';
}

static int
agent_nh_poskey(coordxy *x, coordxy *y, int *mod)
{
    (void) x;
    (void) y;
    (void) mod;
    agent_fail_closed("nh_poskey");
    return 0;
}

static int
agent_doprev_message(void)
{
    agent_fail_closed("doprev_message");
    return 0;
}

static char
agent_yn_function(const char *query, const char *choices, char def)
{
    (void) query;
    (void) choices;
    (void) def;
    agent_fail_closed("yn_function");
    return '\033';
}

static void
agent_getlin(const char *prompt, char *outbuf)
{
    (void) prompt;
    if (outbuf)
        outbuf[0] = '\0';
    agent_fail_closed("getlin");
}

static int
agent_get_ext_cmd(void)
{
    agent_fail_closed("get_ext_cmd");
    return -1;
}

#ifdef CHANGE_COLOR
static void
agent_change_color(int color, long rgb, int reverse)
{
    (void) color;
    (void) rgb;
    (void) reverse;
    /* optional colour mutation is denied */
}

static char *
agent_get_color_string(void)
{
    return (char *) "black";
}
#endif

static void
agent_outrip(winid window, int how, time_t when)
{
    (void) window;
    (void) how;
    (void) when;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("outrip");
}

static char *
agent_getmsghistory(boolean init)
{
    (void) init;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("getmsghistory");
    return (char *) 0;
}

static void
agent_putmsghistory(const char *msg, boolean restoring)
{
    (void) msg;
    (void) restoring;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("putmsghistory");
}

static void
agent_status_init(void)
{
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("status_init");
}

static void
agent_status_finish(void)
{
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("status_finish");
}

static void
agent_status_enablefield(int fieldidx, const char *name, const char *fmt,
                         boolean enable)
{
    (void) fieldidx;
    (void) name;
    (void) fmt;
    (void) enable;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("status_enablefield");
}

static void
agent_status_update(int idx, genericptr_t ptr, int chg, int percent,
                    int color, unsigned long *colormasks)
{
    (void) idx;
    (void) ptr;
    (void) chg;
    (void) percent;
    (void) color;
    (void) colormasks;
    /* M1: discarded, publishes nothing (M2 renders it). */
    (void) ("status_update");
}

static void
agent_update_inventory(int arg)
{
    (void) arg;
    /* no permanent inventory is advertised */
}

static win_request_info *
agent_ctrl_nhwindow(winid window, int request, win_request_info *info)
{
    (void) window;
    (void) request;
    (void) info;
    /* only ABI-approved unchanged/unsupported responses; Wave A publishes
     * nothing */
    return (win_request_info *) 0;
}

/* ------------------------------------------------------------------ */
/* the port table                                                       */
/* ------------------------------------------------------------------ */

struct window_procs agent_procs = {
    WPID(agent),
    (WC_COLOR | WC_HILITE_PET | WC_INVERSE | WC_EIGHT_BIT_IN),
    (WC2_FLUSH_STATUS | WC2_RESET_STATUS),
    { TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE,
      TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE, TRUE },

    .win_init_nhwindows = agent_init_nhwindows,
    .win_player_selection = agent_player_selection,
    .win_askname = agent_askname,
    .win_get_nh_event = agent_get_nh_event,
    .win_exit_nhwindows = agent_exit_nhwindows,
    .win_suspend_nhwindows = agent_suspend_nhwindows,
    .win_resume_nhwindows = agent_resume_nhwindows,
    .win_create_nhwindow = agent_create_nhwindow,
    .win_clear_nhwindow = agent_clear_nhwindow,
    .win_display_nhwindow = agent_display_nhwindow,
    .win_destroy_nhwindow = agent_destroy_nhwindow,
    .win_curs = agent_curs,
    .win_putstr = agent_putstr,
    .win_putmixed = agent_putmixed,
    .win_display_file = agent_display_file,
    .win_start_menu = agent_start_menu,
    .win_add_menu = agent_add_menu,
    .win_end_menu = agent_end_menu,
    .win_select_menu = agent_select_menu,
    .win_message_menu = agent_message_menu,
    .win_mark_synch = agent_mark_synch,
    .win_wait_synch = agent_wait_synch,
#ifdef CLIPPING
    .win_cliparound = agent_cliparound,
#endif
#ifdef POSITIONBAR
    .win_update_positionbar = agent_update_positionbar,
#endif
    .win_print_glyph = agent_print_glyph,
    .win_raw_print = agent_raw_print,
    .win_raw_print_bold = agent_raw_print_bold,
    .win_nhgetch = agent_nhgetch,
    .win_nh_poskey = agent_nh_poskey,
    .win_nhbell = agent_nhbell,
    .win_doprev_message = agent_doprev_message,
    .win_yn_function = agent_yn_function,
    .win_getlin = agent_getlin,
    .win_get_ext_cmd = agent_get_ext_cmd,
    .win_number_pad = agent_number_pad,
    .win_delay_output = agent_delay_output,
#ifdef CHANGE_COLOR
    .win_change_color = agent_change_color,
    .win_get_color_string = agent_get_color_string,
#endif
    .win_outrip = agent_outrip,
    .win_preference_update = agent_preference_update,
    .win_getmsghistory = agent_getmsghistory,
    .win_putmsghistory = agent_putmsghistory,
    .win_status_init = agent_status_init,
    .win_status_finish = agent_status_finish,
    .win_status_enablefield = agent_status_enablefield,
    .win_status_update = agent_status_update,
    .win_can_suspend = agent_can_suspend,
    .win_update_inventory = agent_update_inventory,
    .win_ctrl_nhwindow = agent_ctrl_nhwindow,
};

/*winagent.c*/
