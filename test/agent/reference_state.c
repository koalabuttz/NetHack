/* reference_state.c -- durable/audit reference model implementation. */

#include "reference_state.h"

#include <string.h>

static struct agent_cell
blank_cell(void)
{
    struct agent_cell c;

    c.ch = AG_BLANK_CHAR;
    c.fg = AG_COL_NONE;
    c.style = 0;
    c.frame = AG_COL_NONE;
    return c;
}

static void
fill(struct agent_cell (*map)[AG_MAP_COLS], struct agent_cell c)
{
    int y, x;

    for (y = 0; y < AG_MAP_ROWS; ++y)
        for (x = 0; x < AG_MAP_COLS; ++x)
            map[y][x] = c;
}

static bool
cell_eq(const struct agent_cell *a, const struct agent_cell *b)
{
    return a->ch == b->ch && a->fg == b->fg && a->style == b->style
           && a->frame == b->frame;
}

void
agent_model_reset(struct agent_model *m)
{
    struct agent_cell b = blank_cell();

    memset(m, 0, sizeof *m);
    fill(m->wmap, b);
    fill(m->dmap, b);
    fill(m->tmap, b);
    fill(m->lastview, b);
    fill(m->prevmap, b);
    m->dpal[0] = b;
    m->ndpal = 1;
    m->lc = AG_BOOT;
    m->tx = AG_TX_IDLE;
    m->audit = AG_AUDIT_IDLE;
    m->last_k = -1;
}

/* Allocate durable palette ids from the frozen map in row-major order.
 * Only the durable map is scanned: a tuple seen only in an animation is never
 * allocated here. */
static void
allocate_palette(struct agent_model *m)
{
    int y, x;
    size_t i;

    m->dpal[0] = blank_cell();
    m->ndpal = 1;
    for (y = 0; y < AG_MAP_ROWS; ++y) {
        for (x = 0; x < AG_MAP_COLS; ++x) {
            struct agent_cell *c = &m->dmap[y][x];

            if (cell_eq(c, &m->dpal[0]))
                continue;
            for (i = 1; i < m->ndpal; ++i)
                if (cell_eq(c, &m->dpal[i]))
                    break;
            if (i == m->ndpal) {
                if (m->ndpal >= AG_MODEL_MAX_PAL)
                    continue;
                m->dpal[m->ndpal++] = *c;
                ++m->palette_allocations;
            }
        }
    }
}

/* Freeze W as the next durable snapshot, then re-allocate the palette. */
static void
commit_durable(struct agent_model *m)
{
    int y, x;
    bool changed = false;

    for (y = 0; y < AG_MAP_ROWS && !changed; ++y)
        for (x = 0; x < AG_MAP_COLS && !changed; ++x)
            if (!cell_eq(&m->wmap[y][x], &m->dmap[y][x]))
                changed = true;

    m->durable_map_changed = changed;
    memcpy(m->prevmap, m->dmap, sizeof m->dmap);
    memcpy(m->dmap, m->wmap, sizeof m->dmap);
    ++m->durable_seq;
    ++m->commits;
    allocate_palette(m);
}

/* Capture one transient frame from the scratch view; identical frames are
 * suppressed and never reach the ledger. */
static enum agent_result
emit_frame(struct agent_model *m)
{
    struct agent_model_frame f;
    int y, x;

    f.interval = m->interval;
    f.k = m->last_k + 1;
    f.ncells = 0;
    for (y = 0; y < AG_MAP_ROWS; ++y) {
        for (x = 0; x < AG_MAP_COLS; ++x) {
            if (cell_eq(&m->tmap[y][x], &m->lastview[y][x]))
                continue;
            if (f.ncells < AG_MODEL_MAX_FRAME_CELLS) {
                f.cells[f.ncells].x = x;
                f.cells[f.ncells].y = y;
                f.cells[f.ncells].cell = m->tmap[y][x];
            }
            ++f.ncells;
        }
    }
    if (f.ncells == 0) {
        ++m->suppressed;
        return AG_OK;
    }
    if (m->nledger >= AG_MODEL_MAX_FRAMES)
        return AG_LIMIT;
    m->last_k = f.k;
    m->ledger[m->nledger++] = f;
    ++m->frames_displayed;
    memcpy(m->lastview, m->tmap, sizeof m->tmap);
    return AG_OK;
}

/* Build the deterministic byte string for one chunk: its part indices. */
static size_t
chunk_bytes(const struct agent_model *m, int chunk, char *out, size_t cap)
{
    size_t i, n = 0;
    bool first = true;

    for (i = 0; i < AG_MODEL_MAX_PARTS; ++i) {
        if ((int) m->last_chunk_of[i] != chunk)
            continue;
        if (!first && n + 1 < cap)
            out[n++] = ',';
        first = false;
        if (n + 3 < cap) {
            out[n++] = (char) ('0' + (int) (i % 10));
        }
    }
    if (n < cap)
        out[n] = '\0';
    return n;
}

enum agent_result
agent_model_step(struct agent_model *m, const struct agent_model_event *ev)
{
    if (!m || !ev)
        return AG_INTERNAL;

    switch (ev->kind) {
    case AG_EV_SET_DURABLE:
        if (ev->x < 0 || ev->x >= AG_MAP_COLS || ev->y < 0
            || ev->y >= AG_MAP_ROWS)
            return AG_BAD_INPUT;
        m->wmap[ev->y][ev->x] = ev->cell;
        return AG_OK;

    case AG_EV_AUDIT_BEGIN:
        if (m->audit != AG_AUDIT_IDLE)
            return AG_BAD_INPUT;
        memcpy(m->tmap, m->dmap, sizeof m->tmap); /* T_0 = D_n */
        memcpy(m->lastview, m->tmap, sizeof m->lastview);
        m->scratch_active = true;
        m->audit = AG_AUDIT_OPEN;
        ++m->interval;
        m->last_k = -1;
        return AG_OK;

    case AG_EV_ANIM:
        if (m->audit != AG_AUDIT_OPEN)
            return AG_BAD_INPUT;
        if (ev->x < 0 || ev->x >= AG_MAP_COLS || ev->y < 0
            || ev->y >= AG_MAP_ROWS)
            return AG_BAD_INPUT;
        m->wmap[ev->y][ev->x] = ev->cell; /* working renderer */
        m->tmap[ev->y][ev->x] = ev->cell; /* inline transient tuple */
        return emit_frame(m);

    case AG_EV_ANIM_FRAME:
        if (m->audit != AG_AUDIT_OPEN)
            return AG_BAD_INPUT;
        return emit_frame(m);

    case AG_EV_NONBLOCK:
        /* a nonblocking display, BL_FLUSH, or delay never advances seq */
        return AG_OK;

    case AG_EV_BLOCKING:
    case AG_EV_INPUT:
    case AG_EV_FINAL: {
        if (m->audit == AG_AUDIT_OPEN)
            m->audit = AG_AUDIT_ENDED; /* end(interval,last,target) */
        commit_durable(m);
        m->scratch_active = false;
        m->audit = AG_AUDIT_IDLE;
        m->lc = (ev->kind == AG_EV_INPUT)   ? AG_WAIT_INPUT
                : (ev->kind == AG_EV_FINAL) ? AG_DRAINING
                                            : AG_EXECUTING;
        m->tx = AG_TX_WAIT_SEQ_ACK;
        return AG_OK;
    }

    case AG_EV_CHUNK_SPLIT: {
        size_t n;

        if (ev->nparts == 0 || ev->nparts > AG_MODEL_MAX_PARTS)
            return AG_BAD_INPUT;
        memset(m->last_chunk_of, 0, sizeof m->last_chunk_of);
        n = agent_chunk_plan(ev->part_len, ev->nparts, ev->budget,
                             m->last_chunk_of, AG_MODEL_MAX_PARTS);
        if (n == 0)
            return AG_LIMIT;
        m->last_nchunks = (int) n;
        m->last_rid = ++m->next_delivery;
        m->tx = AG_TX_WAIT_CHUNK_ACK;
        {
            int i;

            for (i = 0; i < m->last_nchunks; ++i)
                m->last_chunk_len[i] =
                    chunk_bytes(m, i, m->last_chunk_bytes[i],
                                sizeof m->last_chunk_bytes[i]);
        }
        return AG_OK;
    }

    case AG_EV_RETRY: {
        char buf[64];
        size_t n;

        if (ev->index < 0 || ev->index >= m->last_nchunks)
            return AG_BAD_INPUT;
        n = chunk_bytes(m, ev->index, buf, sizeof buf);
        /* an identical retry reproduces the exact bytes */
        if (n != m->last_chunk_len[ev->index]
            || memcmp(buf, m->last_chunk_bytes[ev->index], n) != 0)
            return AG_INTERNAL;
        ++m->retries;
        return AG_OK;
    }

    case AG_EV_ACK_CHUNK:
        if (ev->index < 0 || ev->index >= m->last_nchunks)
            return AG_BAD_INPUT;
        if (ev->index == m->last_nchunks - 1)
            m->tx = AG_TX_WAIT_SEQ_ACK;
        else
            m->tx = AG_TX_WAIT_CHUNK_ACK;
        return AG_OK;

    case AG_EV_ACK_SEQ:
        if (ev->index < 0 || (uint64_t) ev->index > m->durable_seq)
            return AG_BAD_INPUT;
        m->tx = AG_TX_IDLE;
        return AG_OK;

    case AG_EV_RESYNC: {
        /* Replay the retained interval prefix; its ledger dedupes
         * so already delivered frames are never counted twice. */
        size_t i, j;
        unsigned distinct = 0;

        ++m->resync_replays;
        for (i = 0; i < m->nledger; ++i) {
            bool dup = false;

            for (j = 0; j < i; ++j)
                if (m->ledger[j].interval == m->ledger[i].interval
                    && m->ledger[j].k == m->ledger[i].k)
                    dup = true;
            if (!dup)
                ++distinct;
        }
        if (distinct != m->frames_displayed)
            return AG_INTERNAL;
        return AG_OK;
    }

    default:
        break;
    }
    return AG_BAD_INPUT;
}
