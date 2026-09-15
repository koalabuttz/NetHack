/* winagent.c -- the agent window port (M2: native rendering and input).
 *
 * This is a trusted-bootstrap port.  It runs only when the process carries
 * the private agent latch established before early_init(); see
 * agent_bootstrap.c.  Every engine-facing byte the agent may see passes
 * through this file:
 *
 *   - the working presentation W is maintained from the real rendering
 *     callbacks (print_glyph, curs, putstr, status, message history, text
 *     windows) and frozen into a full durable snapshot (base:null) only at a
 *     decision boundary;
 *   - agent_render_glyph() is the ONLY translation from glyph metadata to a
 *     reduced public cell (char/color/style/frame), discarding raw glyph,
 *     tile, symbol and non-displayed reason bits;
 *   - menus are captured into adapter-owned sidecars and answered through the
 *     frozen 5.6 final-set model, never a selection-only shortcut;
 *   - the native input callbacks commit the snapshot with one outstanding
 *     request and delegate to the engine-facing primitives in agent_input.c.
 *
 * The port never writes the terminal-closure record: the trusted launcher
 * emits the generic `closed` object when the worker exits.
 */

#include "hack.h"

#include "winagent.h"
#include "agent_types.h"
#include "agent_protocol.h"
#include "agent_menu.h"
#include "agent_view.h"
#include "agent_input.h"

#include "dlb.h"

#include <string.h>

#ifdef AGENT_TEST_IMPOSSIBLE
#include "func_tab.h"        /* WIZMODECMD / CMD_NOT_AVAILABLE */
#include "agent_handshake.h" /* AG_HS_MODE_TEST_* (test-only launch modes) */
#include <fcntl.h>      /* F_GETFD / FD_CLOEXEC for the transport probe */
#endif

#include <errno.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* session transport                                                    */
/* ------------------------------------------------------------------ */

static struct agent_session agent_session;
static boolean agent_session_ready = FALSE;
static boolean agent_hello_done = FALSE;
static uint64_t agent_next_request = 1;
static uint64_t agent_next_event = 1;
static uint64_t agent_next_window = 1;
static uint64_t agent_next_content = 1;
static uint64_t agent_next_menu = 1;

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

/* ---- interface consumed by agent_input.c ---- */

struct agent_session *
agent_port_session(void)
{
    return &agent_session;
}

void
agent_port_fatal(const char *msg)
{
    agent_private_fatal(msg);
}

void
agent_port_diag(const char *msg)
{
    agent_private_diag(msg);
}

uint64_t
agent_port_alloc_request(void)
{
    if (agent_next_request >= AG_COUNTER_MAX)
        agent_private_fatal("public request counter exhausted");
    return agent_next_request++;
}

/* ------------------------------------------------------------------ */
/* presentation identifiers                                             */
/* ------------------------------------------------------------------ */

static void
ag_make_id(char *out, char prefix, uint64_t n)
{
    (void) snprintf(out, AG_ID_STR_MAX, "%c%llu", prefix,
                    (unsigned long long) n);
}

static uint64_t
ag_next_id(uint64_t *counter, const char *what)
{
    if (*counter >= AG_COUNTER_MAX)
        agent_private_fatal(what);
    return (*counter)++;
}

/* ------------------------------------------------------------------ */
/* working presentation W                                               */
/* ------------------------------------------------------------------ */

#define AGW_TITLE 256
#define AGW_MSG_MAX 64
#define AGW_HIST_MAX 64
#define AGW_STATUS_MAX MAXBLSTATS
#define AGW_WIN_MAX 64

struct ag_line {
    char *text;
    uint8_t style;
};

struct ag_event {
    uint64_t e;
    char *text;
    uint8_t style;
};

/* A copied menu row (native identifiers are copied, never dereferenced). */
struct ag_mrow {
    anything ident;
    unsigned int itemflags;
    char ch, gch;
    int attr, color;
    glyph_info gi;
    char text[AG_MENU_TEXT_MAX];
};

struct ag_winrec {
    boolean used;
    winid id;                 /* native handle */
    int type;                 /* NHW_* */
    int last_how;             /* PICK_* of the most recent select_menu */
    char w[AG_ID_STR_MAX];
    char content[AG_ID_STR_MAX];
    char title[AGW_TITLE];
    /* text lines accumulated through putstr */
    struct ag_line *lines;
    size_t nlines, caplines;
    /* menu rows accumulated through add_menu (NHW_MENU only) */
    struct ag_mrow *rows;
    size_t nrows, carows;
    char menu_id[AG_ID_STR_MAX]; /* generation id, "" until start_menu */
    unsigned long mbehavior;
};

/* the working map, indexed by native coordinate; column 0 is unused */
static struct agent_cell ag_w_map[AG_MAP_ROWS][AG_MAP_COLS];
static boolean ag_w_painted[AG_MAP_ROWS][AG_MAP_COLS];
static boolean ag_w_cursor;
static int ag_w_cx, ag_w_cy;

struct ag_status_slot {
    boolean enabled;
    boolean have;
    const char *name;
    char text[AGW_TITLE];
    uint8_t color;
    uint8_t style;
};
static struct ag_status_slot ag_w_status[AGW_STATUS_MAX];

static struct agent_cond ag_w_cond[CONDITION_COUNT];
static size_t ag_w_ncond;

static struct ag_event ag_w_msg[AGW_MSG_MAX];
static size_t ag_w_nmsg;
static struct ag_event ag_w_hist[AGW_HIST_MAX];
static size_t ag_w_nhist;

static struct ag_winrec ag_wins[AGW_WIN_MAX];

static int ag_window_serial = 0;

/* a transient menu descriptor published with the selection request */
static struct agent_window ag_menu_desc;
static boolean ag_menu_desc_active = FALSE;

static struct agent_cell
ag_blank_cell(void)
{
    struct agent_cell c;

    c.ch = (uint8_t) AG_BLANK_CHAR;
    c.fg = AG_COL_NONE;
    c.style = 0;
    c.frame = AG_COL_NONE;
    return c;
}

static boolean
ag_cell_is_blank(const struct agent_cell *c)
{
    return c->ch == (uint8_t) AG_BLANK_CHAR && c->fg == AG_COL_NONE
           && c->style == 0 && c->frame == AG_COL_NONE;
}

static char *
ag_strdup(const char *s)
{
    size_t n = s ? strlen(s) : 0;
    char *p = (char *) alloc((unsigned) (n + 1));

    if (n)
        memcpy(p, s, n);
    p[n] = '\0';
    return p;
}

/* ---- ATR_* to public style bits ---- */

static uint8_t
ag_style_from_attr(int attr)
{
    switch (attr & ~(ATR_URGENT | ATR_NOHISTORY)) {
    case ATR_BOLD:
        return AG_STYLE_BOLD;
    case ATR_DIM:
        return AG_STYLE_DIM;
    case ATR_ITALIC:
        return AG_STYLE_ITALIC;
    case ATR_ULINE:
        return AG_STYLE_UNDERLINE;
    case ATR_BLINK:
        return AG_STYLE_BLINK;
    case ATR_INVERSE:
        return AG_STYLE_INVERSE;
    default:
        break;
    }
    return 0;
}

static uint8_t
ag_color_slot(int color)
{
    if (color < 0 || color >= AG_COL_MAX)
        return AG_COL_NONE;
    return (uint8_t) color;
}

/* ---- the engine-facing glyph bridge ---- */

void agent_render_glyph(const glyph_info *, const glyph_info *,
                        enum agent_render_context, struct agent_cell *);

/* The ONLY translation from glyph metadata to a reduced public cell.  It
 * computes the already reduced display candidates the sanitizer expects and
 * never lets raw glyph id, tile, symbol index, custom color, or a broad MG
 * mask reach the wire. */
void
agent_render_glyph(const glyph_info *glyphinfo, const glyph_info *bkglyphinfo,
                   enum agent_render_context ctx, struct agent_cell *out)
{
    struct agent_render_input in;
    unsigned special = glyphinfo ? glyphinfo->gm.glyphflags : 0;
    int fg = glyphinfo ? glyphinfo->gm.sym.color : NO_COLOR;
    int frame = NO_COLOR;

    if (!out)
        return;
    *out = ag_blank_cell();

    /* a displayed frame color (from the background glyph) has precedence and
     * is the only reason that survives from the background layer */
    if (iflags.use_color && bkglyphinfo
        && bkglyphinfo->framecolor != (uint32) NO_COLOR)
        frame = (int) bkglyphinfo->framecolor;

    memset(&in, 0, sizeof in);
    in.ch = (uint8_t) (glyphinfo ? (glyphinfo->ttychar & 0xff) : 0);
    in.fg = ag_color_slot(fg);
    in.frame = (frame == NO_COLOR) ? AG_COL_NONE : ag_color_slot(frame);

    if (in.frame == AG_COL_NONE) {
        /* pet highlighting, then pile/detection/BW inverse, exactly the
         * precedence tty's displayed result uses; female highlighting is a
         * wizard-only reason and is dropped by the sanitizer */
        in.pet_attr = ((special & MG_PET) != 0) && iflags.hilite_pet;
        in.pile_attr = ((special & MG_OBJPILE) != 0) && iflags.hilite_pile;
        in.detected
            = (special
               & (MG_DETECT | MG_BW_LAVA | MG_BW_ICE | MG_BW_SINK
                  | MG_BW_ENGR)) != 0;
        in.female_wizard = ((special & MG_FEMALE) != 0) && wizard
                           && iflags.wizmgender;
        if (!iflags.use_inverse) {
            in.pet_attr = in.pile_attr = in.detected = in.female_wizard
                = FALSE;
        }
    }

    if (!agent_normalize_appearance(&in, ctx, out)) {
        /* an unrepresentable cell publishes nothing: the declared blank */
        *out = ag_blank_cell();
        agent_private_diag("glyph not representable in the ASCII profile");
    }
}

/* ---- commit ---- */

static int
ag_pal_add(struct agent_view *v, const struct agent_cell *c)
{
    size_t i;

    for (i = 0; i < v->npal; ++i) {
        const struct agent_cell *p = &v->pal[i];

        if (p->ch == c->ch && p->fg == c->fg && p->style == c->style
            && p->frame == c->frame)
            return (int) i;
    }
    if (v->npal >= AG_VIEW_MAX_PALETTE)
        return -1;
    v->pal[v->npal] = *c;
    return (int) v->npal++;
}

static boolean
ag_build_view(struct agent_view *v)
{
    int y, x;
    size_t i;

    memset(v, 0, sizeof *v);
    v->full = TRUE;
    v->pal[0] = ag_blank_cell();
    v->npal = 1;

    for (y = 0; y < AG_MAP_ROWS; ++y) {
        for (x = AG_MAP_MIN_X; x <= AG_MAP_MAX_X; ++x) {
            int id;

            if (!ag_w_painted[y][x] || ag_cell_is_blank(&ag_w_map[y][x]))
                continue; /* blank cells are omitted (sparse) */
            id = ag_pal_add(v, &ag_w_map[y][x]);
            if (id < 0)
                return FALSE;
            v->map[y][x] = (uint16_t) id;
        }
    }

    if (ag_w_cursor) {
        v->has_cursor = TRUE;
        v->cur_x = ag_w_cx;
        v->cur_y = ag_w_cy;
    }

    for (i = 0; i < AGW_STATUS_MAX; ++i) {
        if (!ag_w_status[i].enabled || !ag_w_status[i].have
            || !ag_w_status[i].name || !ag_w_status[i].text[0])
            continue;
        if (v->nstatus >= AG_VIEW_MAX_STATUS)
            return FALSE;
        v->status[v->nstatus].name = ag_w_status[i].name;
        v->status[v->nstatus].text = ag_w_status[i].text;
        v->status[v->nstatus].color = ag_w_status[i].color;
        v->status[v->nstatus].style = ag_w_status[i].style;
        ++v->nstatus;
    }

    for (i = 0; i < ag_w_ncond; ++i) {
        if (v->ncond >= AG_VIEW_MAX_COND)
            return FALSE;
        v->cond[v->ncond++] = ag_w_cond[i];
    }

    for (i = 0; i < ag_w_nmsg; ++i) {
        if (v->nmsg >= AG_VIEW_MAX_MSG)
            return FALSE;
        v->msg[v->nmsg].e = ag_w_msg[i].e;
        v->msg[v->nmsg].text = ag_w_msg[i].text;
        v->msg[v->nmsg].style = ag_w_msg[i].style;
        ++v->nmsg;
    }
    for (i = 0; i < ag_w_nhist; ++i) {
        if (v->nhist >= AG_VIEW_MAX_MSG)
            return FALSE;
        v->hist[v->nhist].e = ag_w_hist[i].e;
        v->hist[v->nhist].text = ag_w_hist[i].text;
        v->hist[v->nhist].style = ag_w_hist[i].style;
        ++v->nhist;
    }

    if (ag_menu_desc_active) {
        if (v->nwindows >= AG_VIEW_MAX_WINDOWS)
            return FALSE;
        v->windows[v->nwindows++] = ag_menu_desc;
    }
    for (i = 0; i < AGW_WIN_MAX; ++i) {
        struct ag_winrec *r = &ag_wins[i];

        if (!r->used || r->type != NHW_TEXT)
            continue;
        if (v->nwindows >= AG_VIEW_MAX_WINDOWS)
            return FALSE;
        {
            struct agent_window *w = &v->windows[v->nwindows++];

            w->w = r->w;
            w->kind = 0; /* text */
            w->title = r->title[0] ? r->title : " ";
            w->mode = 0;
            w->content = r->content;
            w->pages = (int) agent_content_pages(r->nlines);
        }
    }
    return TRUE;
}

/* ------------------------------------------------------------------ */
/* test-only: presentation reconstruction self-check                    */
/* ------------------------------------------------------------------ */
#ifdef AGENT_TEST_WRECON
/* Rebuild a client model purely from the emitted snapshot and compare it with
 * the working presentation W.  This proves the wire projection is lossless at
 * every durable boundary; a mismatch terminates privately. */
static void
ag_recon_check(const struct agent_view *v)
{
    int y, x;
    size_t i, j;
    size_t matched = 0;

    /* map + palette: exactly the painted, non-blank cells, at their stored
     * coordinates, resolving to the same public cell */
    for (y = 0; y < AG_MAP_ROWS; ++y) {
        for (x = AG_MAP_MIN_X; x <= AG_MAP_MAX_X; ++x) {
            boolean expect = ag_w_painted[y][x]
                             && !ag_cell_is_blank(&ag_w_map[y][x]);
            uint16_t id = v->map[y][x];

            if (expect && id == 0)
                agent_private_fatal("wrecon-mismatch: painted cell omitted");
            if (!expect && id != 0)
                agent_private_fatal("wrecon-mismatch: blank cell published");
            if (!expect)
                continue;
            {
                const struct agent_cell *c = &v->pal[id];

                if (c->ch != ag_w_map[y][x].ch || c->fg != ag_w_map[y][x].fg
                    || c->style != ag_w_map[y][x].style
                    || c->frame != ag_w_map[y][x].frame)
                    agent_private_fatal("wrecon-mismatch: cell tuple");
            }
            /* the same tuple must map to the same palette id */
            for (i = 1; i < v->npal; ++i)
                if (v->pal[i].ch == ag_w_map[y][x].ch
                    && v->pal[i].fg == ag_w_map[y][x].fg
                    && v->pal[i].style == ag_w_map[y][x].style
                    && v->pal[i].frame == ag_w_map[y][x].frame
                    && (int) i != (int) id)
                    agent_private_fatal("wrecon-mismatch: palette");
        }
    }
    if (v->has_cursor != ag_w_cursor
        || (ag_w_cursor && (v->cur_x != ag_w_cx || v->cur_y != ag_w_cy)))
        agent_private_fatal("wrecon-mismatch: cursor differs");

    for (i = 0; i < v->nstatus; ++i) {
        for (j = 0; j < AGW_STATUS_MAX; ++j) {
            if (ag_w_status[j].enabled && ag_w_status[j].have
                && ag_w_status[j].text[0] && ag_w_status[j].name
                && strcmp(v->status[i].name, ag_w_status[j].name) == 0
                && strcmp(v->status[i].text, ag_w_status[j].text) == 0
                && v->status[i].color == ag_w_status[j].color
                && v->status[i].style == ag_w_status[j].style) {
                ++matched;
                break;
            }
        }
    }
    if (matched != v->nstatus)
        agent_private_fatal("wrecon-mismatch: status differs");

    if (v->ncond != ag_w_ncond)
        agent_private_fatal("wrecon-mismatch: condition count differs");
    for (i = 0; i < v->ncond; ++i)
        if (v->cond[i].text != ag_w_cond[i].text
            || v->cond[i].color != ag_w_cond[i].color
            || v->cond[i].style != ag_w_cond[i].style)
            agent_private_fatal("wrecon-mismatch: condition differs");

    if (v->nmsg != ag_w_nmsg || v->nhist != ag_w_nhist)
        agent_private_fatal("wrecon-mismatch: message count differs");
    for (i = 0; i < v->nmsg; ++i)
        if (v->msg[i].e != ag_w_msg[i].e
            || strcmp(v->msg[i].text, ag_w_msg[i].text) != 0
            || v->msg[i].style != ag_w_msg[i].style)
            agent_private_fatal("wrecon-mismatch: message differs");
    for (i = 0; i < v->nhist; ++i)
        if (v->hist[i].e != ag_w_hist[i].e
            || strcmp(v->hist[i].text, ag_w_hist[i].text) != 0
            || v->hist[i].style != ag_w_hist[i].style)
            agent_private_fatal("wrecon-mismatch: history differs");

    agent_private_diag("wrecon ok");
}
#endif /* AGENT_TEST_WRECON */

enum agent_result
agent_port_commit_need(const struct agent_need *need)
{
    struct agent_view v;

    if (!agent_session_ready)
        agent_private_fatal("commit before the transport was opened");
    if (!agent_hello_done) {
        if (agent_write_hello(&agent_session) != AG_OK)
            agent_private_fatal("could not emit the hello record");
        agent_hello_done = TRUE;
    }
    if (!ag_build_view(&v))
        agent_private_fatal("presentation exceeds a public bound");
#ifdef AGENT_TEST_WRECON
    ag_recon_check(&v);
#endif
    if (agent_commit(&agent_session, &v, need) != AG_OK)
        agent_private_fatal("could not emit the durable snapshot");
    return AG_OK;
}

/* ------------------------------------------------------------------ */
/* window records                                                       */
/* ------------------------------------------------------------------ */

static struct ag_winrec *
ag_winrec_find(winid window)
{
    size_t i;

    for (i = 0; i < AGW_WIN_MAX; ++i)
        if (ag_wins[i].used && ag_wins[i].id == window)
            return &ag_wins[i];
    return (struct ag_winrec *) 0;
}

static void
ag_winrec_reset(struct ag_winrec *r)
{
    size_t i;

    for (i = 0; i < r->nlines; ++i)
        free(r->lines[i].text);
    if (r->lines)
        free(r->lines);
    if (r->rows)
        free(r->rows);
    memset(r, 0, sizeof *r);
}

static void
ag_msg_free(struct ag_event *ev)
{
    if (ev->text)
        free(ev->text);
    ev->text = (char *) 0;
}

static void
ag_msg_push(struct ag_event *list, size_t *n, size_t cap, const char *text,
            uint8_t style, uint64_t e)
{
    size_t i;

    if (*n == cap) {
        ag_msg_free(&list[0]);
        for (i = 1; i < cap; ++i)
            list[i - 1] = list[i];
        --*n;
    }
    list[*n].e = e;
    list[*n].text = ag_strdup(text);
    list[*n].style = style;
    ++*n;
}

/* ------------------------------------------------------------------ */
/* diagnostic/fail-closed helpers                                       */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* Wave A/M1 callbacks                                                  */
/* ------------------------------------------------------------------ */

static void
agent_init_nhwindows(int *argc, char **argv)
{
    (void) argc;
    (void) argv;
    agent_session_open();
}

static void
agent_emit_hello_once(void)
{
    if (agent_hello_done)
        return;
    agent_session_open();
    if (agent_write_hello(&agent_session) != AG_OK)
        agent_private_fatal("could not emit the hello record");
    agent_hello_done = TRUE;
}

#ifdef AGENT_TEST_IMPOSSIBLE
static void agent_test_probe(const char *name, int denied)
{
    char buf[80];

    (void) snprintf(buf, sizeof buf, "probe %s=%d", name, denied ? 1 : 0);
    agent_private_diag(buf);
}

/* Engine-facing glyph-bridge equality fixtures.  Two raw glyph_infos that
 * differ only in raw identity or in a non-displayed reason must produce
 * byte-identical public cells. */
static boolean
ag_cells_equal(const struct agent_cell *a, const struct agent_cell *b)
{
    return a->ch == b->ch && a->fg == b->fg && a->style == b->style
           && a->frame == b->frame;
}

static void
agent_test_glyph_equality(void)
{
    glyph_info a, b;
    glyph_info bk_a, bk_b;
    struct agent_cell ca, cb;
    boolean saved_wizard = wizard, saved_wmg = iflags.wizmgender;

    memset(&a, 0, sizeof a);
    memset(&b, 0, sizeof b);
    memset(&bk_a, 0, sizeof bk_a);
    memset(&bk_b, 0, sizeof bk_b);
    bk_a.framecolor = bk_b.framecolor = (uint32) NO_COLOR;

    /* (1) map: raw glyph id and non-displayed flags differ, display same */
    a.ttychar = 'd';
    b.ttychar = 'd';
    a.gm.sym.color = b.gm.sym.color = CLR_RED;
    a.gm.glyphflags = MG_HERO | MG_MALE;
    b.gm.glyphflags = 0;
    a.glyph = 1200;
    b.glyph = 5;
    agent_render_glyph(&a, (const glyph_info *) 0, AG_RC_MAP, &ca);
    agent_render_glyph(&b, (const glyph_info *) 0, AG_RC_MAP, &cb);
    agent_test_probe("glypheq-map", ag_cells_equal(&ca, &cb));

    /* (2) frame precedence: a displayed frame color drops the pet attr */
    bk_a.framecolor = bk_b.framecolor = (uint32) CLR_BLUE;
    a.gm.glyphflags = MG_PET;
    b.gm.glyphflags = 0;
    agent_render_glyph(&a, &bk_a, AG_RC_MAP, &ca);
    agent_render_glyph(&b, &bk_b, AG_RC_MAP, &cb);
    agent_test_probe("glypheq-frame", ag_cells_equal(&ca, &cb));

    /* (3) menu context: map-only pet/detection reasons are ignored */
    a.gm.glyphflags = MG_DETECT;
    b.gm.glyphflags = 0;
    agent_render_glyph(&a, (const glyph_info *) 0, AG_RC_MENU, &ca);
    agent_render_glyph(&b, (const glyph_info *) 0, AG_RC_MENU, &cb);
    agent_test_probe("glypheq-menu", ag_cells_equal(&ca, &cb));

    /* (4) wizard-only gender highlighting is never applied */
    wizard = TRUE;
    iflags.wizmgender = TRUE;
    a.gm.glyphflags = MG_FEMALE;
    b.gm.glyphflags = 0;
    agent_render_glyph(&a, (const glyph_info *) 0, AG_RC_MAP, &ca);
    agent_render_glyph(&b, (const glyph_info *) 0, AG_RC_MAP, &cb);
    wizard = saved_wizard;
    iflags.wizmgender = saved_wmg;
    agent_test_probe("glypheq-female", ag_cells_equal(&ca, &cb));
}

static void
agent_test_diagnostics(void)
{
    int r;

    agent_test_probe("denyset",
                     agent_test_runtime_set_denied("color")
                         && agent_test_runtime_set_denied("perm_invent"));
    agent_test_probe("bindkeys", parsebindings((char *) "a:help") == FALSE);
    agent_test_probe("symset",
                     parsesymbols((char *) "S_foo:x", PRIMARYSET) == FALSE);
    program_state.in_parseoptions += 1;
    r = load_symset("DECGraphics", PRIMARYSET);
    program_state.in_parseoptions -= 1;
    agent_test_probe("symsetload", r == 0);
    {
        char before[BUFSZ];

        Strcpy(before, svp.plname);
        r = parseoptions((char *) "name:Hostile", FALSE, FALSE);
        agent_test_probe("parsemutate",
                         r == FALSE && strcmp(before, svp.plname) == 0);
    }
    agent_test_probe("wizardcmd",
                     agent_policy_command_flags(WIZMODECMD) == FALSE);
    agent_test_probe("ordinary", agent_policy_command_flags(0) == FALSE);
    agent_test_probe("handlers",
                     agent_policy_command(doset) == FALSE
                         && agent_policy_command(enter_explore_mode)
                                == FALSE);

    agent_test_glyph_equality();

    impossible("AGENT_TEST_IMPOSSIBLE: injected diagnostic");
}

static void agent_test_mode_dispatch(void);
static void agent_test_exec_inventory(void);
#endif /* AGENT_TEST_IMPOSSIBLE */

static void
agent_player_selection(void)
{
    if (!agent_publication_open())
        agent_private_fatal("player selection before publication readiness");

#ifdef AGENT_TEST_IMPOSSIBLE
    agent_test_mode_dispatch();
    agent_test_diagnostics();
#endif

    agent_session_open();
    agent_emit_hello_once();

    /* The engine's ordinary selection path: role/race/gender/alignment menus
     * and the name rules, driven through the port's own callbacks.  There is
     * no privileged selection shortcut. */
    agent_port_set_input_context("native character selection");
    if (!genl_player_setup(0)) {
        /* the agent cancelled selection */
        nh_terminate(EXIT_SUCCESS);
    }
    agent_port_set_input_context("gameplay input");
}

static void
agent_askname(void)
{
    /* The character name is a trusted startup field, fixed before the
     * publication gate opens, so startup never blocks on an interactive
     * question. */
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
    (void) str;
    if (agent_session_ready) {
        agent_session_free(&agent_session);
        agent_session_ready = FALSE;
    }
}

static void
agent_suspend_nhwindows(const char *str)
{
    (void) str;
}

static void
agent_resume_nhwindows(void)
{
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
    agent_private_diag(str);
}

static void
agent_raw_print_bold(const char *str)
{
    agent_private_diag(str);
}

/* ------------------------------------------------------------------ */
/* window presentation                                                  */
/* ------------------------------------------------------------------ */

static winid
agent_create_nhwindow(int type)
{
    size_t i;

    for (i = 0; i < AGW_WIN_MAX; ++i) {
        if (!ag_wins[i].used) {
            struct ag_winrec *r = &ag_wins[i];

            ag_winrec_reset(r);
            r->used = TRUE;
            r->type = type;
            r->id = (winid) (++ag_window_serial);
            ag_make_id(r->w, 'w',
                       ag_next_id(&agent_next_window, "window id exhausted"));
            ag_make_id(r->content, 'c',
                       ag_next_id(&agent_next_content,
                                  "content id exhausted"));
            return r->id;
        }
    }
    agent_private_fatal("too many concurrent windows");
    return WIN_ERR;
}

static void
agent_clear_nhwindow(winid window)
{
    struct ag_winrec *r = ag_winrec_find(window);
    size_t i;

    if (!r)
        return;
    if (r->type == NHW_MESSAGE) {
        for (i = 0; i < ag_w_nmsg; ++i)
            ag_msg_free(&ag_w_msg[i]);
        ag_w_nmsg = 0;
        return;
    }
    for (i = 0; i < r->nlines; ++i)
        free(r->lines[i].text);
    r->nlines = 0;
}

static void
agent_destroy_nhwindow(winid window)
{
    struct ag_winrec *r = ag_winrec_find(window);
    size_t i;

    if (!r)
        return;
    if (r->type == NHW_MESSAGE) {
        for (i = 0; i < ag_w_nmsg; ++i)
            ag_msg_free(&ag_w_msg[i]);
        ag_w_nmsg = 0;
    }
    ag_winrec_reset(r);
}

/* Build content rows for a text window's lines (owned by the caller). */
static struct agent_content_row *
ag_lines_to_rows(struct ag_winrec *r, size_t *nrows)
{
    struct agent_content_row *rows;
    size_t i;

    *nrows = r->nlines;
    if (!r->nlines)
        return (struct agent_content_row *) 0;
    rows = (struct agent_content_row *) alloc(
        (unsigned) (r->nlines * sizeof *rows));
    for (i = 0; i < r->nlines; ++i) {
        memset(&rows[i], 0, sizeof rows[i]);
        rows[i].r = 0; /* plain text line */
        rows[i].text = r->lines[i].text;
        rows[i].style = r->lines[i].style;
    }
    return rows;
}

/* Publish the currently built menu rows as content rows. */
static struct agent_content_row *
ag_menu_to_rows(struct ag_winrec *r, size_t *nrows)
{
    struct agent_content_row *rows;
    size_t i;

    *nrows = r->nrows;
    if (!r->nrows)
        return (struct agent_content_row *) 0;
    rows = (struct agent_content_row *) alloc(
        (unsigned) (r->nrows * sizeof *rows));
    for (i = 0; i < r->nrows; ++i) {
        struct ag_mrow *mr = &r->rows[i];
        struct agent_content_row *cr = &rows[i];
        struct agent_cell icon;

        memset(cr, 0, sizeof *cr);
        cr->r = (long) i + 1;
        cr->text = mr->text;
        cr->selectable = (mr->ident.a_void != (void *) 0);
        cr->key = (unsigned char) mr->ch;
        cr->group = (unsigned char) mr->gch;
        cr->has_initial = (mr->itemflags & MENU_ITEMFLAGS_SELECTED) != 0;
        cr->initial = cr->has_initial ? -1 : 0;
        cr->style = ag_style_from_attr(mr->attr);
        cr->color = ag_color_slot(mr->color);
        agent_render_glyph(&mr->gi, (const glyph_info *) 0, AG_RC_MENU,
                           &icon);
        if (!ag_cell_is_blank(&icon)) {
            cr->has_icon = TRUE;
            cr->icon = icon;
        }
    }
    return rows;
}

/* Emit a blocking display boundary: freeze W with an acknowledgement request
 * and wait until the agent acknowledges or cancels.  rows/nrows describe the
 * content that must be paged before the acknowledgement is accepted. */
static void
ag_blocking_ack(const char *content, int pages,
                struct agent_content_row *rows, size_t nrows)
{
    struct agent_need need;
    struct agent_action act;
    struct agent_commit_row commit_rows[1];

    memset(&act, 0, sizeof act);
    act.commit = commit_rows;
    act.commit_cap = 1;

    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_ACK;
    need.id = agent_port_alloc_request();
    need.content = (pages > 0) ? content : (const char *) 0;
    need.pages = pages;

    agent_port_commit_need(&need);
    agent_session_set_content(&agent_session, rows, nrows);

    for (;;) {
        enum agent_result r = agent_receive(&agent_session, &act);

        if (r == AG_BAD_INPUT)
            continue;
        if (r != AG_OK)
            agent_port_fatal_ctx("transport failed during a blocking"
                                 " display");
        break;
    }
    if (agent_accept(&agent_session) != AG_OK)
        agent_port_fatal_ctx("could not record the acknowledgement");
    agent_session_set_content(&agent_session,
                              (const struct agent_content_row *) 0,
                              0);
}

static void
agent_display_nhwindow(winid window, boolean blocking)
{
    struct ag_winrec *r = ag_winrec_find(window);

    if (!blocking)
        return; /* nonblocking displays never advance a durable boundary */

    if (!r || r->type == NHW_MESSAGE) {
        /* the message content is already part of msg; the acknowledgement is
         * immediate */
        ag_blocking_ack((const char *) 0, 0,
                        (struct agent_content_row *) 0, 0);
        return;
    }

    if (r->type == NHW_MENU && r->nrows) {
        size_t nrows;
        struct agent_content_row *rows = ag_menu_to_rows(r, &nrows);
        char content[AG_ID_STR_MAX];

        Strcpy(content, r->content);
        ag_blocking_ack(content, (int) agent_content_pages(nrows), rows,
                        nrows);
        if (rows)
            free(rows);
        return;
    }

    {
        size_t nrows;
        struct agent_content_row *rows = ag_lines_to_rows(r, &nrows);
        char content[AG_ID_STR_MAX];

        Strcpy(content, r->content);
        ag_blocking_ack(content, (int) agent_content_pages(nrows), rows,
                        nrows);
        if (rows)
            free(rows);
    }
}

static void
agent_curs(winid window, int x, int y)
{
    struct ag_winrec *r = ag_winrec_find(window);

    if (!r || r->type != NHW_MAP)
        return;
    if (x < AG_MAP_MIN_X || x > AG_MAP_MAX_X || y < AG_MAP_MIN_Y
        || y > AG_MAP_MAX_Y)
        return;
    ag_w_cursor = TRUE;
    ag_w_cx = x;
    ag_w_cy = y;
}

static void
ag_push_line(struct ag_winrec *r, const char *str, uint8_t style)
{
    if (r->nlines == r->caplines) {
        size_t ncap = r->caplines ? r->caplines * 2 : 32;
        struct ag_line *nl = (struct ag_line *) alloc(
            (unsigned) (ncap * sizeof *nl));

        if (r->nlines)
            memcpy(nl, r->lines, r->nlines * sizeof *nl);
        if (r->lines)
            free(r->lines);
        r->lines = nl;
        r->caplines = ncap;
    }
    r->lines[r->nlines].text = ag_strdup(str);
    r->lines[r->nlines].style = style;
    ++r->nlines;
}

static void
agent_putstr(winid window, int attr, const char *str)
{
    struct ag_winrec *r = ag_winrec_find(window);
    uint8_t style = ag_style_from_attr(attr);

    if (!r || !str)
        return;
    if (r->type == NHW_MESSAGE) {
        if (!str[0])
            return;
        ag_msg_push(ag_w_msg, &ag_w_nmsg, AGW_MSG_MAX, str, style,
                    ag_next_id(&agent_next_event, "message id exhausted"));
        return;
    }
    ag_push_line(r, str, style);
}

static void
agent_putmixed(winid window, int attr, const char *str)
{
    struct ag_winrec *r = ag_winrec_find(window);
    uint8_t style = ag_style_from_attr(attr);

    if (!r || !str)
        return;
    if (r->type == NHW_MESSAGE) {
        char buf[BUFSZ];
        char *txt = decode_mixed(buf, str);

        if (!txt[0])
            return;
        ag_msg_push(ag_w_msg, &ag_w_nmsg, AGW_MSG_MAX, txt, style,
                    ag_next_id(&agent_next_event, "message id exhausted"));
        return;
    }
    {
        /* decode mixed text privately to displayed symbols; never forward a
         * raw authenticated glyph payload */
        char *copy = ag_strdup(str);
        size_t cap = strlen(copy) + 1;
        char *buf = (char *) alloc((unsigned) (cap + 4));
        char *txt = decode_mixed(buf, copy);

        ag_push_line(r, txt, style);
        free(buf);
        free(copy);
    }
}

static void
agent_display_file(const char *fname, boolean complain)
{
    dlb *f;
    char buf[BUFSZ];
    winid win;

    if (!fname)
        return;
    f = dlb_fopen(fname, "r");
    if (!f) {
        if (complain) {
            struct ag_winrec *mw = ag_winrec_find(WIN_MESSAGE);

            if (mw)
                ag_msg_push(ag_w_msg, &ag_w_nmsg, AGW_MSG_MAX,
                            "Cannot open the requested data file.", 0,
                            ag_next_id(&agent_next_event,
                                       "message id exhausted"));
        }
        return;
    }
    win = agent_create_nhwindow(NHW_TEXT);
    {
        struct ag_winrec *r = ag_winrec_find(win);

        if (r)
            Strcpy(r->title, fname);
        while (dlb_fgets(buf, BUFSZ, f)) {
            char *cr = strchr(buf, '\n');

            if (cr)
                *cr = '\0';
            agent_putstr(win, 0, buf);
        }
    }
    dlb_fclose(f);
    agent_display_nhwindow(win, TRUE);
    agent_destroy_nhwindow(win);
}

/* ------------------------------------------------------------------ */
/* menus: capture and the 5.6 final-set selection                       */
/* ------------------------------------------------------------------ */

static void
agent_start_menu(winid window, unsigned long mbehavior)
{
    struct ag_winrec *r = ag_winrec_find(window);

    if (!r)
        return;
    r->nrows = 0; /* discard previous row mappings for this window */
    r->mbehavior = mbehavior;
    ag_make_id(r->menu_id, 'm',
               ag_next_id(&agent_next_menu, "menu id exhausted"));
}

static void
agent_add_menu(winid window, const glyph_info *glyphinfo,
               const ANY_P *identifier, char ch, char gch, int attr,
               int color,
               const char *str, unsigned int itemflags)
{
    struct ag_winrec *r = ag_winrec_find(window);
    struct ag_mrow *row;

    if (!r)
        return;
    if (str && strlen(str) >= AG_MENU_TEXT_MAX)
        agent_private_fatal("menu row text exceeds the public bound");
    if (r->nrows == r->carows) {
        size_t ncap = r->carows ? r->carows * 2 : 32;
        struct ag_mrow *nr = (struct ag_mrow *) alloc(
            (unsigned) (ncap * sizeof *nr));

        if (r->nrows)
            memcpy(nr, r->rows, r->nrows * sizeof *nr);
        if (r->rows)
            free(r->rows);
        r->rows = nr;
        r->carows = ncap;
    }
    row = &r->rows[r->nrows++];
    memset(row, 0, sizeof *row);
    if (identifier)
        row->ident = *identifier;
    row->itemflags = itemflags;
    row->ch = ch;
    row->gch = gch;
    row->attr = attr;
    row->color = color;
    if (glyphinfo)
        row->gi = *glyphinfo;
    else
        row->gi = nul_glyphinfo;
    if (str)
        Strcpy(row->text, str);
    else
        row->text[0] = '\0';
}

static void
agent_end_menu(winid window, const char *prompt)
{
    struct ag_winrec *r = ag_winrec_find(window);

    if (!r)
        return;
    if (prompt && strlen(prompt) >= AGW_TITLE)
        agent_private_fatal("menu prompt exceeds the public bound");
    if (prompt)
        Strcpy(r->title, prompt);
    else
        r->title[0] = '\0';
}

/* Build the public menu model from a captured sidecar. */
static void
ag_build_menu_model(struct ag_winrec *r, struct agent_menu_row *rows,
                    struct agent_menu *m)
{
    size_t i;

    for (i = 0; i < r->nrows; ++i) {
        struct ag_mrow *mr = &r->rows[i];
        struct agent_menu_row *p = &rows[i];
        struct agent_cell icon;

        memset(p, 0, sizeof *p);
        p->r = (long) i + 1;
        Strcpy(p->text, mr->text);
        p->selectable = (mr->ident.a_void != (void *) 0);
        p->key = (unsigned char) mr->ch;
        p->group = (unsigned char) mr->gch;
        p->has_initial = (mr->itemflags & MENU_ITEMFLAGS_SELECTED) != 0;
        p->initial = p->has_initial ? -1 : 0;
        p->style = ag_style_from_attr(mr->attr);
        p->color = ag_color_slot(mr->color);
        agent_render_glyph(&mr->gi, (const glyph_info *) 0, AG_RC_MENU,
                           &icon);
        if (!ag_cell_is_blank(&icon)) {
            p->has_icon = TRUE;
            p->icon = icon;
        }
    }
    m->id = r->menu_id;
    m->mode = (r->last_how == PICK_ONE)
                  ? AG_MENU_ONE
                  : (r->last_how == PICK_ANY ? AG_MENU_ANY : AG_MENU_NONE);
    m->rows = rows;
    m->nrows = r->nrows;
    m->cap = r->nrows;
}

static int
agent_select_menu(winid window, int how, MENU_ITEM_P **menu_list)
{
    struct ag_winrec *r = ag_winrec_find(window);
    struct agent_menu_row *mrow;
    struct agent_menu m;
    struct agent_menu_answer ans;
    struct agent_selection sel;
    struct agent_selection_row *selrows;
    struct agent_commit_row crefs[AG_VIEW_MAX_COMMIT];
    struct agent_action act;
    struct agent_need need;
    size_t nrows, i;
    struct agent_content_row *crows;
    int result;

    if (!r || r->type != NHW_MENU) {
        if (menu_list)
            *menu_list = (MENU_ITEM_P *) 0;
        return 0;
    }
    if (menu_list)
        *menu_list = (MENU_ITEM_P *) 0;

    r->last_how = how;
    nrows = r->nrows;
    mrow = nrows ? (struct agent_menu_row *)
                       alloc((unsigned) (nrows * sizeof *mrow))
                 : (struct agent_menu_row *) 0;
    ag_build_menu_model(r, mrow, &m);
    if (agent_menu_check(&m) != AG_OK)
        agent_private_fatal("captured menu violates the public model");

    selrows = nrows ? (struct agent_selection_row *) alloc(
                          (unsigned) (nrows * sizeof *selrows))
                    : (struct agent_selection_row *) 0;

    crows = ag_menu_to_rows(r, &nrows);

    memset(&act, 0, sizeof act);
    act.commit = crefs;
    act.commit_cap = AG_VIEW_MAX_COMMIT;

    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = agent_port_alloc_request();
    need.menu = r->menu_id;
    need.mode = m.mode;
    need.content = r->content;
    need.pages = (int) agent_content_pages(nrows);

    /* publish the menu descriptor alongside the request */
    ag_menu_desc.w = r->w;
    ag_menu_desc.kind = 1;
    ag_menu_desc.title = r->title[0] ? r->title : " ";
    ag_menu_desc.mode = m.mode;
    ag_menu_desc.content = r->content;
    ag_menu_desc.pages = need.pages;
    ag_menu_desc_active = TRUE;

    agent_port_commit_need(&need);
    ag_menu_desc_active = FALSE;
    agent_session_set_content(&agent_session, crows, nrows);

    result = 0;
    for (;;) {
        enum agent_result rc = agent_receive(&agent_session, &act);

        if (rc == AG_BAD_INPUT)
            continue;
        if (rc != AG_OK)
            agent_port_fatal_ctx("transport failed during menu selection");

        memset(&ans, 0, sizeof ans);
        sel.cap = nrows;
        sel.rows = selrows;
        if (act.kind == AG_ACT_CANCEL) {
            ans.cancel = true;
        } else if (act.kind == AG_ACT_ACK) {
            ans.ack = true;
        } else { /* AG_ACT_MENU */
            ans.rows = act.commit;
            ans.nrows = act.ncommit;
        }
        if (agent_menu_validate(&m, &ans, &sel) != AG_OK) {
            if (agent_write_invalid(&agent_session, sel.code) != AG_OK)
                agent_port_fatal_ctx("could not write an invalid record");
            continue;
        }
        /* accepted */
        result = (int) sel.result;
        break;
    }
    if (agent_accept(&agent_session) != AG_OK)
        agent_port_fatal_ctx("could not record the accepted selection");
    agent_session_set_content(&agent_session,
                              (const struct agent_content_row *) 0,
                              0);

    if (result > 0) {
        MENU_ITEM_P *out = (MENU_ITEM_P *) alloc(
            (unsigned) (result * sizeof(MENU_ITEM_P)));

        for (i = 0; i < (size_t) result; ++i) {
            long rid = sel.rows[i].r;
            struct ag_mrow *mr = &r->rows[rid - 1];

            out[i].item = mr->ident;
            out[i].count = sel.rows[i].count;
            out[i].itemflags = mr->itemflags;
        }
        if (menu_list)
            *menu_list = out;
    }

    if (mrow)
        free(mrow);
    if (selrows)
        free(selrows);
    if (crows)
        free(crows);
    return result;
}

static char
agent_message_menu(char let, int how, const char *mesg)
{
    struct ag_winrec *r;

    if (how == PICK_NONE) {
        r = ag_winrec_find(WIN_MESSAGE);
        if (r && mesg && mesg[0])
            ag_msg_push(ag_w_msg, &ag_w_nmsg, AGW_MSG_MAX, mesg, 0,
                        ag_next_id(&agent_next_event,
                                   "message id exhausted"));
        return '\0';
    }
    {
        int ch = agent_input_key(AG_NEED_KEY, mesg);

        if (ch == (int) (unsigned char) let || ch == '\033')
            return (char) ch;
        return '\0';
    }
}

/* ------------------------------------------------------------------ */
/* non-observed plumbing                                                */
/* ------------------------------------------------------------------ */

static void
agent_mark_synch(void)
{
}

static void
agent_wait_synch(void)
{
}

#ifdef CLIPPING
static void
agent_cliparound(int x, int y)
{
    (void) x;
    (void) y;
}
#endif

#ifdef POSITIONBAR
static void
agent_update_positionbar(char *posbar)
{
    (void) posbar;
}
#endif

static void
agent_print_glyph(winid window, coordxy x, coordxy y,
                  const glyph_info *glyphinfo, const glyph_info *bkglyphinfo)
{
    struct ag_winrec *r = ag_winrec_find(window);
    struct agent_cell c;

    if (!r || r->type != NHW_MAP)
        return;
    if (x < 0 || x >= AG_MAP_COLS || y < 0 || y >= AG_MAP_ROWS)
        return;
    agent_render_glyph(glyphinfo, bkglyphinfo, AG_RC_MAP, &c);
    ag_w_map[y][x] = c;
    ag_w_painted[y][x] = TRUE;
}

/* ------------------------------------------------------------------ */
/* input callbacks                                                      */
/* ------------------------------------------------------------------ */

static int
agent_nhgetch(void)
{
    return agent_input_key(AG_NEED_COMMAND, (const char *) 0);
}

static int
agent_nh_poskey(coordxy *x, coordxy *y, int *mod)
{
    return agent_input_poskey(x, y, mod);
}

static int
agent_doprev_message(void)
{
    /* present the message history through an ordinary text window and the
     * blocking-display acknowledgement (no privileged input path) */
    winid win;
    size_t i;

    win = agent_create_nhwindow(NHW_TEXT);
    {
        struct ag_winrec *r = ag_winrec_find(win);

        if (r)
            Strcpy(r->title, "Message History");
    }
    agent_putstr(win, 0, "Message History");
    for (i = 0; i < ag_w_nmsg; ++i)
        agent_putstr(win, 0, ag_w_msg[i].text);
    agent_display_nhwindow(win, TRUE);
    agent_destroy_nhwindow(win);
    return 0;
}

static char
agent_yn_function(const char *query, const char *choices, char def)
{
    return (char) agent_input_yn(query, choices, def);
}

static void
agent_getlin(const char *prompt, char *outbuf)
{
    if (!outbuf)
        return;
    agent_input_line(prompt, outbuf, BUFSZ);
}

static int
agent_get_ext_cmd(void)
{
    return agent_input_extcmd();
}

#ifdef CHANGE_COLOR
static void
agent_change_color(int color, long rgb, int reverse)
{
    (void) color;
    (void) rgb;
    (void) reverse;
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
    /* the endgame rendering arrives in M4; until then it publishes nothing
     * and must not fabricate a player-visible epitaph */
    (void) window;
    (void) how;
    (void) when;
}

/* ------------------------------------------------------------------ */
/* message history                                                      */
/* ------------------------------------------------------------------ */

static size_t ag_hist_cursor = 0;

static char *
agent_getmsghistory(boolean init)
{
    if (init)
        ag_hist_cursor = 0;
    if (ag_hist_cursor >= ag_w_nhist)
        return (char *) 0;
    return ag_w_hist[ag_hist_cursor++].text;
}

static void
agent_putmsghistory(const char *msg, boolean restoring)
{
    (void) restoring;
    if (!msg)
        return;
    ag_msg_push(ag_w_hist, &ag_w_nhist, AGW_HIST_MAX, msg, 0,
                ag_next_id(&agent_next_event, "history id exhausted"));
}

/* ------------------------------------------------------------------ */
/* status                                                               */
/* ------------------------------------------------------------------ */

static void
agent_status_init(void)
{
    size_t i;

    for (i = 0; i < AGW_STATUS_MAX; ++i) {
        ag_w_status[i].enabled = FALSE;
        ag_w_status[i].have = FALSE;
        ag_w_status[i].name = (const char *) 0;
        ag_w_status[i].text[0] = '\0';
        ag_w_status[i].color = AG_COL_NONE;
        ag_w_status[i].style = 0;
    }
    ag_w_ncond = 0;
}

static void
agent_status_finish(void)
{
}

static void
agent_status_enablefield(int fieldidx, const char *name, const char *fmt,
                         boolean enable)
{
    (void) fmt;
    if (fieldidx < 0 || fieldidx >= AGW_STATUS_MAX)
        return;
    ag_w_status[fieldidx].enabled = enable;
    ag_w_status[fieldidx].name = name;
}

/* Rebuild the ordered displayed condition list from a native bitmask. */
static void
ag_conditions_from_mask(unsigned long mask)
{
    int k;

    ag_w_ncond = 0;
    for (k = 0; k < CONDITION_COUNT; ++k) {
        int i = cond_idx[k];

        if (i < 0 || i >= CONDITION_COUNT)
            continue;
        if (!condtests[i].enabled)
            continue;
        if ((mask & conditions[i].mask) == 0)
            continue;
        if (ag_w_ncond >= AG_VIEW_MAX_COND)
            break;
        ag_w_cond[ag_w_ncond].text = conditions[i].text[0];
        ag_w_cond[ag_w_ncond].color = AG_COL_NONE;
        ag_w_cond[ag_w_ncond].style = 0;
        ++ag_w_ncond;
    }
}

static void
agent_status_update(int idx, genericptr_t ptr, int chg, int percent,
                    int color,
                    unsigned long *colormasks)
{
    char buf[MAXCO + 8];
    char *text = (char *) ptr;

    (void) chg;
    (void) percent;
    (void) colormasks;

    if (idx < 0 || idx >= MAXBLSTATS) {
        if (idx == BL_FLUSH || idx == BL_RESET) {
            /* presentation batching: no observation, no seq bump */
            return;
        }
        return;
    }
    if (!ag_w_status[idx].enabled)
        return;

    if (idx == BL_CONDITION) {
        ag_conditions_from_mask((unsigned long) *((long *) ptr));
        return;
    }
    if (idx == BL_GOLD)
        text = decode_mixed(buf, text);

    if (!text)
        return;
    if (strlen(text) >= sizeof ag_w_status[idx].text)
        agent_private_fatal("status field exceeds the public bound");
    Strcpy(ag_w_status[idx].text, text);
    ag_w_status[idx].have = TRUE;
    ag_w_status[idx].color = ag_color_slot(color & 0x00ff);
    ag_w_status[idx].style = 0;
}

static void
agent_update_inventory(int arg)
{
    (void) arg;
}

static win_request_info *
agent_ctrl_nhwindow(winid window, int request, win_request_info *info)
{
    (void) window;
    (void) request;
    (void) info;
    return (win_request_info *) 0;
}

#ifdef AGENT_TEST_IMPOSSIBLE
/* Test-only: report the transport descriptor's close-on-exec state and then
 * exec a descriptor-inventory helper. */
static void
agent_test_exec_inventory(void)
{
    char script[256], fdstr[16], probe[80];
    char *av[4], *ev[2];
    int fd = agent_bootstrap_fd();
    int fdflags = (fd >= 0) ? fcntl(fd, F_GETFD) : -1;

    (void) snprintf(probe, sizeof probe, "probe cloexec=%d",
                    (fdflags >= 0 && (fdflags & FD_CLOEXEC)) ? 1 : 0);
    agent_private_diag(probe);

    (void) snprintf(fdstr, sizeof fdstr, "%d", fd);
    (void) snprintf(script, sizeof script,
                    "if [ -e /proc/self/fd/%s ]; then "
                    "echo helper:transport-present; else "
                    "echo helper:transport-absent; fi", fdstr);
    av[0] = (char *) "/bin/sh";
    av[1] = (char *) "-c";
    av[2] = script;
    av[3] = (char *) 0;
    ev[0] = (char *) "PATH=/usr/bin:/bin";
    ev[1] = (char *) 0;
    (void) execve("/bin/sh", av, ev);
    agent_private_fatal("descriptor-inventory helper could not be executed");
}

/* Test-only: drive ONE test path per worker process, selected by the
 * handshake launch mode the matrix sends (see agent_handshake.h).  The M2
 * decision-boundary cases each publish exactly one durable snapshot carrying
 * their request and then must terminate privately on the silent transport
 * rather than fabricate a decision. */
static void
agent_test_mode_dispatch(void)
{
    unsigned mode = agent_bootstrap_test_mode();

    if (mode == AG_HS_MODE_TEST_EXEC) {
        agent_test_exec_inventory();
        return;
    }
    if (mode == AG_HS_MODE_TEST_DISPLAY || mode == AG_HS_MODE_TEST_SELECT
        || mode == AG_HS_MODE_TEST_MSGMENU) {
        agent_session_open();
        agent_emit_hello_once();
        agent_port_set_input_context("native character selection");
    }
    switch (mode) {
    case AG_HS_MODE_TEST_DISPLAY: {
        winid win = agent_create_nhwindow(NHW_TEXT);
        struct ag_winrec *r = ag_winrec_find(win);

        if (r)
            Strcpy(r->title, "test display");
        agent_putstr(win, 0, "test content");
        agent_display_nhwindow(win, TRUE);
        agent_destroy_nhwindow(win);
        break;
    }
    case AG_HS_MODE_TEST_SELECT: {
        winid win = agent_create_nhwindow(NHW_MENU);
        anything any1, any2;

        any1 = cg.zeroany;
        any2 = cg.zeroany;
        any1.a_int = 1;
        any2.a_int = 2;
        agent_start_menu(win, MENU_BEHAVE_STANDARD);
        agent_add_menu(win, &nul_glyphinfo, &any1, 'a', 0, ATR_NONE,
                       NO_COLOR,
                       "first", MENU_ITEMFLAGS_NONE);
        agent_add_menu(win, &nul_glyphinfo, &any2, 'b', 0, ATR_NONE,
                       NO_COLOR,
                       "second", MENU_ITEMFLAGS_NONE);
        agent_end_menu(win, "test menu");
        (void) agent_select_menu(win, PICK_ONE, (MENU_ITEM_P **) 0);
        agent_destroy_nhwindow(win);
        break;
    }
    case AG_HS_MODE_TEST_MSGMENU:
        (void) agent_message_menu(' ', PICK_ONE, "test message menu");
        break;
    default:
        break;
    }
}
#endif /* AGENT_TEST_IMPOSSIBLE */

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
