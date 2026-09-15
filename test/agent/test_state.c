/* test_state.c -- durable/audit reference vectors.
 *
 * P1 gate vectors: a projectile that returns to its original map,
 * palette isolation from animation, chunk splits at every boundary, retries,
 * and resync replay.  Engine-free.
 */

#include <stdio.h>
#include <string.h>

#include "reference_state.h"

static int failures;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

static struct agent_model model;

static struct agent_cell
mkcell(unsigned char ch, unsigned char fg)
{
    struct agent_cell c;

    c.ch = ch;
    c.fg = fg;
    c.style = 0;
    c.frame = AG_COL_NONE;
    return c;
}

int
main(void)
{
    size_t i;

    /* ================================================================
     * Vector A: a projectile returns to the original durable map.
     * ================================================================ */
    agent_model_reset(&model);
    {
        struct agent_model_event ev;

        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_SET_DURABLE;
        ev.x = 12;
        ev.y = 8;
        ev.cell = mkcell('.', AG_COL_GRAY);
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        ev.x = 13;
        CHECK(agent_model_step(&model, &ev) == AG_OK);

        ev.kind = AG_EV_INPUT; /* durable boundary -> D1 */
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.commits == 1);
        CHECK(model.durable_seq == 1);
        CHECK(model.durable_map_changed == true);
    }
    {
        size_t npal_before = model.ndpal;

        /* begin an audit interval based on D1 */
        {
            struct agent_model_event ev;

            memset(&ev, 0, sizeof ev);
            ev.kind = AG_EV_AUDIT_BEGIN;
            CHECK(agent_model_step(&model, &ev) == AG_OK);
            CHECK(model.audit == AG_AUDIT_OPEN);
            CHECK(model.interval == 1);
        }
        /* the projectile crosses from (12,8) to (13,8) and both cells are
         * restored; the animation tuple uses a color absent from D1 */
        {
            struct agent_model_event ev;

            memset(&ev, 0, sizeof ev);
            ev.kind = AG_EV_ANIM;
            ev.x = 12;
            ev.y = 8;
            ev.cell = mkcell('*', AG_COL_ORANGE);
            CHECK(agent_model_step(&model, &ev) == AG_OK); /* frame 0 */

            ev.x = 12;
            ev.cell = mkcell('.', AG_COL_GRAY); /* source restoration */
            CHECK(agent_model_step(&model, &ev) == AG_OK); /* frame 1 */

            ev.x = 13;
            ev.cell = mkcell('*', AG_COL_ORANGE); /* next projectile cell */
            CHECK(agent_model_step(&model, &ev) == AG_OK); /* frame 2 */

            ev.x = 13;
            ev.cell = mkcell('.', AG_COL_GRAY); /* final restoration */
            CHECK(agent_model_step(&model, &ev) == AG_OK); /* frame 3 */
        }
        CHECK(model.nledger == 4);
        CHECK(model.frames_displayed == 4);

        /* the blocking display ends the interval and commits */
        {
            struct agent_model_event ev;

            memset(&ev, 0, sizeof ev);
            ev.kind = AG_EV_BLOCKING;
            CHECK(agent_model_step(&model, &ev) == AG_OK);
        }
        /* the durable map is unchanged: an empty durable patch */
        CHECK(model.commits == 2);
        CHECK(model.durable_seq == 2);
        CHECK(model.durable_map_changed == false);
        /* ... but the audit stream is not empty */
        CHECK(model.nledger == 4);

        /* Vector B: the animation-only tuple never became a durable
         * palette id, and the durable palette did not grow */
        CHECK(model.ndpal == npal_before);
        for (i = 0; i < model.ndpal; ++i)
            CHECK(!(model.dpal[i].ch == '*'
                    && model.dpal[i].fg == AG_COL_ORANGE));
    }

    /* ================================================================
     * Identical normalized frames are suppressed; nonblocking displays
     * never advance the durable version.
     * ================================================================ */
    {
        struct agent_model_event ev;
        uint64_t seq_before = model.durable_seq;

        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_NONBLOCK;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.durable_seq == seq_before);

        ev.kind = AG_EV_AUDIT_BEGIN;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_ANIM_FRAME; /* nothing changed since the clone */
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.suppressed == 1);
        CHECK(model.nledger == 4); /* no new ledger entry */
        CHECK(model.frames_displayed == 4);

        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_INPUT;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.audit == AG_AUDIT_IDLE);
        CHECK(model.scratch_active == false);
    }

    /* ================================================================
     * Vector C: chunk splits at every legal boundary.
     * ================================================================ */
    {
        size_t parts[5];
        size_t chunk_of[5];
        size_t budget;

        parts[0] = 12;
        parts[1] = 3;
        parts[2] = 40;
        parts[3] = 8;
        parts[4] = 1;

        for (budget = 1; budget <= 64; ++budget) {
            size_t n = agent_chunk_plan(parts, 5, budget, chunk_of, 16);

            if (budget < 40) {
                CHECK(n == 0);
                continue;
            }
            CHECK(n >= 1);
            if (!n)
                continue;
            CHECK(chunk_of[0] == 0);
            for (i = 1; i < 5; ++i)
                CHECK(chunk_of[i] == chunk_of[i - 1]
                      || chunk_of[i] == chunk_of[i - 1] + 1);
            CHECK(chunk_of[4] == n - 1);
            {
                size_t c;

                for (c = 0; c < n; ++c) {
                    size_t total = 0, count = 0, k;

                    for (k = 0; k < 5; ++k)
                        if (chunk_of[k] == c) {
                            total += parts[k];
                            ++count;
                        }
                    if (count > 1)
                        total += count - 1;
                    CHECK(total <= budget);
                }
            }
        }
    }

    /* ================================================================
     * Vector D: chunk acknowledgement and identical retries.
     * ================================================================ */
    {
        struct agent_model_event ev;
        int c;

        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_CHUNK_SPLIT;
        ev.part_len[0] = 12;
        ev.part_len[1] = 3;
        ev.part_len[2] = 40;
        ev.part_len[3] = 8;
        ev.part_len[4] = 1;
        ev.nparts = 5;
        ev.budget = 44;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.tx == AG_TX_WAIT_CHUNK_ACK);
        CHECK(model.last_nchunks > 1);

        for (c = 0; c < model.last_nchunks; ++c) {
            memset(&ev, 0, sizeof ev);
            ev.kind = AG_EV_RETRY;
            ev.index = c;
            CHECK(agent_model_step(&model, &ev) == AG_OK);
        }
        CHECK(model.retries == (unsigned) model.last_nchunks);

        /* a chunk index that was never sent is rejected */
        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_RETRY;
        ev.index = model.last_nchunks;
        CHECK(agent_model_step(&model, &ev) == AG_BAD_INPUT);

        for (c = 0; c < model.last_nchunks; ++c) {
            memset(&ev, 0, sizeof ev);
            ev.kind = AG_EV_ACK_CHUNK;
            ev.index = c;
            CHECK(agent_model_step(&model, &ev) == AG_OK);
            CHECK(model.tx == (c + 1 == model.last_nchunks
                                   ? AG_TX_WAIT_SEQ_ACK
                                   : AG_TX_WAIT_CHUNK_ACK));
        }
        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_ACK_SEQ;
        ev.index = (int) model.durable_seq;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.tx == AG_TX_IDLE);
        /* a durable acknowledgement beyond the current version is rejected */
        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_ACK_SEQ;
        ev.index = (int) model.durable_seq + 5;
        CHECK(agent_model_step(&model, &ev) == AG_BAD_INPUT);
    }

    /* ================================================================
     * Vector E: resync replays the retained prefix without double counting.
     * ================================================================ */
    {
        struct agent_model_event ev;
        unsigned displayed = model.frames_displayed;
        size_t ledger = model.nledger;

        memset(&ev, 0, sizeof ev);
        ev.kind = AG_EV_RESYNC;
        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.resync_replays == 1);
        CHECK(model.frames_displayed == displayed); /* dedup (interval,k) */
        CHECK(model.nledger == ledger);

        CHECK(agent_model_step(&model, &ev) == AG_OK);
        CHECK(model.resync_replays == 2);
        CHECK(model.frames_displayed == displayed);
    }

    if (failures) {
        printf("test_state: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_state: ok\n");
    return 0;
}
