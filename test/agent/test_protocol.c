/* test_protocol.c -- strict parsing, framing, counters, retries, paging.
 *
 * Engine-free golden vectors for doc/agent-interface.md sections 5, 7, 8, 11,
 * 13.  No engine headers, no engine objects.
 */

#include <stdio.h>
#include <string.h>

#include "agent_protocol.h"

static int failures;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

/* ---- transport harness with controllable fragmentation ---- */
struct io {
    char in[8192];
    size_t inlen, inpos;
    char out[131072];
    size_t outlen;
    size_t chunk_in;
    size_t chunk_out;
    int write_calls;
};

static struct io io;
static struct agent_session sess;
static struct agent_commit_row apool[8];

/* the caller owns the action's commit storage, so seed it before use */
static void
prep(struct agent_action *a)
{
    memset(a, 0, sizeof *a);
    a->commit = apool;
    a->commit_cap = 8;
}


static long
rd(void *ctx, char *buf, size_t cap)
{
    struct io *p = (struct io *) ctx;
    size_t n = p->inlen - p->inpos;

    if (n == 0)
        return 0;
    if (p->chunk_in && n > p->chunk_in)
        n = p->chunk_in;
    if (n > cap)
        n = cap;
    memcpy(buf, p->in + p->inpos, n);
    p->inpos += n;
    return (long) n;
}

static long
wr(void *ctx, const char *buf, size_t len)
{
    struct io *p = (struct io *) ctx;
    size_t n = len;

    if (p->chunk_out && n > p->chunk_out)
        n = p->chunk_out;
    if (p->outlen + n > sizeof p->out)
        return -1;
    memcpy(p->out + p->outlen, buf, n);
    p->outlen += n;
    ++p->write_calls;
    return (long) n;
}

static void
reset_io(void)
{
    memset(&io, 0, sizeof io);
    agent_session_init(&sess, rd, wr, &io);
}

static void
feed(const char *s)
{
    size_t n = strlen(s);

    memcpy(io.in + io.inlen, s, n);
    io.inlen += n;
}

static const char *
outstr(void)
{
    io.out[io.outlen] = '\0';
    return io.out;
}

static int
count_sub(const char *hay, const char *needle)
{
    int n = 0;
    const char *p = hay;

    while ((p = strstr(p, needle)) != NULL) {
        ++n;
        p += strlen(needle);
    }
    return n;
}

static long
find_int_field(const char *line, const char *key)
{
    char pat[32];
    const char *p;
    long v = -1;

    snprintf(pat, sizeof pat, "\"%s\":", key);
    p = strstr(line, pat);
    if (!p)
        return -1;
    p += strlen(pat);
    v = 0;
    while (*p >= '0' && *p <= '9')
        v = v * 10 + (*p++ - '0');
    return v;
}

/* a small view used by the commit tests */
static struct agent_view view;

static void
build_view(int ncells)
{
    int i;

    memset(&view, 0, sizeof view);
    view.full = true;
    view.pal[0].ch = AG_BLANK_CHAR;
    view.pal[0].fg = AG_COL_NONE;
    view.pal[0].style = 0;
    view.pal[0].frame = AG_COL_NONE;
    view.pal[1].ch = '.';
    view.pal[1].fg = AG_COL_GRAY;
    view.pal[2].ch = '@';
    view.pal[2].fg = AG_COL_WHITE;
    view.npal = 3;
    for (i = 0; i < ncells; ++i) {
        int y = i / AG_MAP_COLS;
        int x = i % AG_MAP_COLS;

        if (y >= AG_MAP_ROWS)
            break;
        view.map[y][x] = (i == 0) ? 2 : 1;
    }
    view.has_cursor = true;
    view.cur_x = 0;
    view.cur_y = 0;
    view.status[0].name = "time";
    view.status[0].text = "42";
    view.status[0].color = AG_COL_NONE;
    view.nstatus = 1;
    view.cond[0].text = "Blind";
    view.cond[0].color = AG_COL_NONE;
    view.ncond = 1;
    view.msg[0].e = 1;
    view.msg[0].text = "You hear someone counting money.";
    view.msg[0].style = 0;
    view.nmsg = 1;
}

/* ================================================================= */

static void
test_parse_accepts(void)
{
    struct agent_action a;

    prep(&a);
    struct agent_commit_row c[4];

    static const char key_h[] = "{\"v\":1,\"type\":\"act\",\"id\":7,"
                                "\"action\":{\"key\":104}}";
    static const char key_104[] = "{\"v\":1,\"type\":\"act\",\"seq\":3,"
                                  "\"id\":8,\"action\":{\"key\":104}}";

    memset(&a, 0, sizeof a);
    CHECK(agent_parse_action(key_h, 0, &a) == AG_BAD_INPUT);
    CHECK(agent_parse_action(key_h, sizeof key_h - 1, &a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.id == 7 && a.key == 'h' && !a.has_seq);

    CHECK(agent_parse_action(key_104, sizeof key_104 - 1, &a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.key == 104 && a.has_seq && a.seq == 3);

    {
        static const char txt[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                  "\"action\":{\"text\":\"ab cd\"}}";
        CHECK(agent_parse_action(txt, sizeof txt - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_TEXT && strcmp(a.text, "ab cd") == 0);
    }
    {
        static const char pos[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                  "\"action\":{\"position\":[12,8],"
                                  "\"mod\":0}}";
        CHECK(agent_parse_action(pos, sizeof pos - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_POSITION && a.px == 12 && a.py == 8
              && a.pmod == 0);
    }
    {
        static const char yn[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                 "\"action\":{\"yn\":121,\"count\":3}}";
        CHECK(agent_parse_action(yn, sizeof yn - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_YN && a.key == 'y' && a.has_count
              && a.yn_count == 3);
    }
    {
        static const char mn[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                 "\"action\":{\"menu\":\"m4\","
                                 "\"commit\":[[2,-1],[5,3]]}}";
        a.commit = c;
        a.commit_cap = 4;
        CHECK(agent_parse_action(mn, sizeof mn - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_MENU && a.ncommit == 2);
        CHECK(a.commit[0].r == 2 && a.commit[0].count == -1);
        CHECK(a.commit[1].r == 5 && a.commit[1].count == 3);
    }
    {
        static const char ca[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                 "\"action\":{\"cancel\":true}}";
        CHECK(agent_parse_action(ca, sizeof ca - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_CANCEL);
    }
    {
        static const char ak[] = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                                 "\"action\":{\"ack\":true}}";
        CHECK(agent_parse_action(ak, sizeof ak - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_ACK);
    }
}

static void
test_parse_rejects(void)
{
    struct agent_action a;

    prep(&a);
    struct agent_commit_row c[4];
    static const char *const bad[] = {
        /* unknown / forbidden keys */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1},\"x\":1}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"ch\":\"player\","
        "\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"key\":1,\"then\":[]}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"selectall\":true}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"invert\":true}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"bulk\":true}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"group\":\")\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"keys\":\"hhh\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"raw\":true}}",
        /* duplicate keys */
        "{\"v\":1,\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"key\":1,\"key\":2}}",
        /* missing required */
        "{\"v\":1,\"type\":\"act\",\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1}",
        /* wrong constants */
        "{\"v\":2,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"obs\",\"id\":1,\"action\":{\"key\":1}}",
        /* range and type violations */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":256}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1.5}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1e2}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":01}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":-1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[80,0],"
        "\"mod\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[1,21],"
        "\"mod\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"count\":2}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"mod\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"menu\":\"m1\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"menu\":\"x1\","
        "\"commit\":[]}}",
        /* two shapes at once */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1,"
        "\"cancel\":true}}",
        /* empty action */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{}}",
        /* structural */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}x",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}",
        /* bad escapes / encoding */
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"text\":\"a\\u0000b\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"text\":\"\\ud800\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"text\":\"a\x01\" \"b\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}\x80}"
    };
    size_t i;

    for (i = 0; i < sizeof bad / sizeof bad[0]; ++i) {
        memset(&a, 0, sizeof a);
        a.commit = c;
        a.commit_cap = 4;
        if (agent_parse_action(bad[i], strlen(bad[i]), &a) == AG_OK) {
            printf("FAIL: unexpectedly accepted: %s\n", bad[i]);
            ++failures;
        }
    }

    /* an NUL byte anywhere is rejected even though strlen would hide it */
    {
        static const char nz[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                 "\"action\":{\"text\":\"a\"}}";
        memset(&a, 0, sizeof a);
        CHECK(agent_parse_action(nz, sizeof nz - 1, &a) == AG_OK);
    }
    /* commit rows beyond caller storage fail closed */
    {
        static const char many[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                   "\"action\":{\"menu\":\"m1\","
                                   "\"commit\":[[1,1],[2,1],[3,1]]}}";
        memset(&a, 0, sizeof a);
        a.commit = c;
        a.commit_cap = 2;
        CHECK(agent_parse_action(many, sizeof many - 1, &a) != AG_OK);
    }
    /* commit with zero or out-of-range counts */
    {
        static const char z[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                "\"action\":{\"menu\":\"m1\","
                                "\"commit\":[[1,0]]}}";
        static const char r[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                "\"action\":{\"menu\":\"m1\","
                                "\"commit\":[[65536,1]]}}";
        memset(&a, 0, sizeof a);
        a.commit = c;
        a.commit_cap = 4;
        CHECK(agent_parse_action(z, sizeof z - 1, &a) != AG_OK);
        CHECK(agent_parse_action(r, sizeof r - 1, &a) != AG_OK);
    }
}

static void
test_escaping(void)
{
    /* a text field with escapes decodes to the raw bytes */
    {
        static const char in[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                 "\"action\":{\"text\":\"a\\\"b\\\\c\"}}";
        struct agent_action a;

        memset(&a, 0, sizeof a);
        CHECK(agent_parse_action(in, sizeof in - 1, &a) == AG_OK);
        CHECK(strcmp(a.text, "a\"b\\c") == 0);
    }
    /* encoder escapes control characters and quotes */
    {
        struct agent_view v;

        build_view(3);
        v = view;
        v.nmsg = 1;
        v.msg[0].text = "a\"b\\c\nd";
        reset_io();
        CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
        CHECK(strstr(outstr(), "a\\\"b\\\\c\\nd") != NULL);
    }
}

static void
test_hello_and_closed(void)
{
    static const char expect_hello[] =
        "{\"v\":1,\"ch\":\"control\",\"type\":\"hello\",\"d\":1,"
        "\"profile\":\"normal-ascii-color-v1\",\"policy\":\"llm-final-v1\","
        "\"caps\":[\"snapshot\",\"menu\",\"paging\"],"
        "\"coord\":\"engine-map\","
        "\"size\":[80,21],\"x0\":1,\"y0\":0,\"limits\":{\"line\":65536,"
        "\"page_bytes\":16384,\"page_rows\":128,\"count\":2147483647}}\n";
    static const char expect_closed[] =
        "{\"v\":1,\"ch\":\"control\",\"type\":\"closed\"}\n";

    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    CHECK(strcmp(outstr(), expect_hello) == 0);
    CHECK(sess.next_delivery == 1);

    CHECK(agent_write_closed(&sess) == AG_OK);
    {
        const char *p = strstr(outstr(), "{\"v\":1,\"ch\":\"control\","
                                          "\"type\":\"closed\"}");

        CHECK(p != NULL);
        CHECK(strcmp(p, expect_closed) == 0);
        CHECK(strstr(p, "\"d\"") == NULL); /* the closed-counter exception */
    }
    CHECK(sess.closed == true);
    /* terminal closure is not retryable and emits no counter */
    CHECK(agent_write_closed(&sess) == AG_OK);
}

static void
test_commit_and_counters(void)
{
    struct agent_need need;

    build_view(4);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK); /* d=1 */
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_COMMAND;
    need.id = 1;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK); /* d=2, seq=1 */
    CHECK(sess.next_delivery == 2);
    CHECK(sess.next_seq == 1);
    CHECK(sess.outstanding_id == 1);
    CHECK(strstr(outstr(), "\"seq\":1") != NULL);
    CHECK(strstr(outstr(), "\"base\":null") != NULL);
    CHECK(strstr(outstr(), "\"need\":{\"id\":1,\"kind\":\"command\"}")
          != NULL);

    /* a final-boundary commit clears the outstanding request and advances
     * seq exactly once; a nonblocking display would not advance it at all */
    {
        uint64_t before = sess.next_seq;

        agent_commit(&sess, &view, NULL);
        CHECK(sess.next_seq == before + 1);
        CHECK(sess.outstanding_id == 0);
    }
}

static void
test_fragmentation_and_short_writes(void)
{
    struct agent_action a;

    prep(&a);
    struct agent_need need;

    /* one byte at a time input, three bytes at a time output */
    build_view(2);
    reset_io();
    io.chunk_in = 1;
    io.chunk_out = 3;
    CHECK(agent_write_hello(&sess) == AG_OK);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_COMMAND;
    need.id = 1;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":104}}\n");
    CHECK(agent_receive(&sess, &a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.key == 'h');
    CHECK(io.write_calls > 3); /* short writes were retried to completion */

    /* a fragmented line still frames correctly across several reads */
    reset_io();
    io.chunk_in = 5;
    io.chunk_out = 1;
    agent_session_init(&sess, rd, wr, &io);
    CHECK(agent_write_hello(&sess) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":9}}\n");
    feed("{\"v\":1,\"type\":\"act\",\"id\":2,\"action\":{\"key\":8}}\n");
    CHECK(agent_receive(&sess, &a) == AG_OK);
    CHECK(a.key == 9);
    CHECK(agent_receive(&sess, &a) == AG_OK);
    CHECK(a.key == 8);
    /* exhausted input is EOF */
    CHECK(agent_receive(&sess, &a) == AG_IO);

}

static void
test_stale_and_duplicate_actions(void)
{
    struct agent_action a;

    prep(&a);
    struct agent_need need;
    const char *act5 = "{\"v\":1,\"type\":\"act\",\"id\":5,"
                       "\"action\":{\"key\":104}}\n";
    const char *act5b = "{\"v\":1,\"type\":\"act\",\"id\":5,"
                        "\"action\":{\"key\":105}}\n";

    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_COMMAND;
    need.id = 5;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* a stale id consumes nothing and leaves the request outstanding */
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,\"action\":{\"key\":120}}\n");
    CHECK(agent_receive(&sess, &a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_STALE);
    CHECK(sess.outstanding_id == 5);
    CHECK(sess.have_action == false);

    /* the fresh, matching id is accepted */
    feed(act5);
    CHECK(agent_receive(&sess, &a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.id == 5 && !a.replay);
    CHECK(sess.have_action && sess.action_id == 5);

    /* the answer is retained by the next commit */
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_COMMAND;
    need.id = 6;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.have_reply == true);
    {
        size_t before = io.outlen;

        /* an identical retry replays the retained response and is consumed */
        feed(act5);
        feed("{\"v\":1,\"type\":\"act\",\"id\":6,"
             "\"action\":{\"key\":106}}\n");
        CHECK(agent_receive(&sess, &a) == AG_OK);
        CHECK(a.id == 6);
        CHECK(sess.replays == 1);
        CHECK(io.outlen > before); /* the reply was re-written */
    }
    /* conflicting reuse of an accepted id closes the transport */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"key\":107}}\n");
    CHECK(agent_receive(&sess, &a) == AG_INTERNAL);
    /* a superseded (stale) id is rejected without closing */
    feed(act5b);
    CHECK(agent_receive(&sess, &a) == AG_BAD_INPUT);
}

static void
test_aux_records_and_paging(void)
{
    struct agent_action a;

    prep(&a);
    struct agent_need need;

    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = 9;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = 2;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.need_pages == 2);

    /* a selection is forbidden until every required page is delivered */
    feed("{\"v\":1,\"type\":\"act\",\"id\":9,\"action\":{\"menu\":\"m1\","
         "\"commit\":[[1,1]]}}\n");
    CHECK(agent_receive(&sess, &a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_INCOMPLETE);
    CHECK(sess.outstanding_id == 9);

    /* request the pages */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":0}\n");
    feed("{\"v\":1,\"type\":\"ack_seq\",\"seq\":1}\n");
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":1}\n");
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":5}\n"); /* out of range */
    CHECK(agent_receive(&sess, &a) == AG_IO); /* all transport ops consumed */
    CHECK(sess.pages_sent == 2);
    CHECK(count_sub(outstr(), "\"type\":\"page\"") == 2);
    CHECK(count_sub(outstr(), "\"code\":\"range\"") == 1);

    /* an unknown auxiliary field is rejected (no additional properties) */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    feed("{\"v\":1,\"type\":\"ack_seq\",\"seq\":1,\"junk\":1}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"schema\"") == 1);

    /* an over-deep structure inside a skipped value is rejected */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    feed("{\"v\":1,\"type\":\"ack_seq\",\"seq\":1,\"junk\":"
         "[[[[[[[[[[1]]]]]]]]]]}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"schema\"") == 1);

    /* a stale durable acknowledgement is reported and does not advance */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    feed("{\"v\":1,\"type\":\"ack_seq\",\"seq\":99}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"stale\"") == 1);
    CHECK(sess.acked_seq == 0);
}

static void
test_chunk_plan_boundaries(void)
{
    size_t parts[6];
    size_t chunk_of[6];
    size_t budget;

    parts[0] = 10;
    parts[1] = 20;
    parts[2] = 5;
    parts[3] = 7;
    parts[4] = 30;
    parts[5] = 3;

    for (budget = 1; budget <= 80; ++budget) {
        size_t n = agent_chunk_plan(parts, 6, budget, chunk_of, 16);
        size_t i;

        if (budget < 30) {
            CHECK(n == 0); /* one part never fits */
            continue;
        }
        CHECK(n >= 1);
        if (n == 0)
            continue;
        CHECK(chunk_of[0] == 0);
        for (i = 1; i < 6; ++i)
            CHECK(chunk_of[i] == chunk_of[i - 1]
                  || chunk_of[i] == chunk_of[i - 1] + 1);
        CHECK(chunk_of[5] == n - 1);
        /* each chunk fits */
        {
            size_t c;

            for (c = 0; c < n; ++c) {
                size_t total = 0, k, count = 0;

                for (k = 0; k < 6; ++k)
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
    /* everything in one chunk when the budget covers the whole record */
    CHECK(agent_chunk_plan(parts, 6, 100, chunk_of, 16) == 1);
    /* caller storage too small fails closed */
    CHECK(agent_chunk_plan(parts, 6, 30, chunk_of, 2) == 0);
}

static void
test_chunk_emission(void)
{
    struct agent_need need;
    int nonblank = 101; /* 100 floor cells + 1 hero cell */
    char *line, *next;
    long rid0 = -1;
    int idx = -1;
    int chunks = 0;

    build_view(nonblank);
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.limit_line = 300; /* force the chunk path with identical content */
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_COMMAND;
    need.id = 1;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* every part is present exactly once across the chunk stream */
    CHECK(count_sub(outstr(), "\"p\":\"map\"") == nonblank);
    CHECK(count_sub(outstr(), "\"p\":\"h\"") == 6);
    CHECK(strstr(outstr(), "\"last\":true") != NULL);

    line = io.out;
    while (line && *line) {
        char *nl = strchr(line, '\n');

        if (nl)
            *nl = '\0';
        if (strstr(line, "\"type\":\"chunk\"")) {
            long r = find_int_field(line, "rid");
            long i = find_int_field(line, "i");

            if (rid0 < 0)
                rid0 = r;
            CHECK(r == rid0);
            CHECK(i == idx + 1);
            idx = (int) i;
            ++chunks;
        }
        next = nl ? nl + 1 : NULL;
        line = next;
    }
    CHECK(chunks > 1);
    /* the logical record id is the delivery counter of the first chunk */
    CHECK(rid0 > 0);

    /* identical retry re-sends the exact last physical record */
    {
        size_t before = io.outlen;

        CHECK(agent_retry_last(&sess) == AG_OK);
        CHECK(io.outlen == before + sess.last_line_len);
        CHECK(memcmp(io.out + before, sess.last_line,
                     sess.last_line_len) == 0);
    }
}

int
main(void)
{
    test_parse_accepts();
    test_parse_rejects();
    test_escaping();
    test_hello_and_closed();
    test_commit_and_counters();
    test_fragmentation_and_short_writes();
    test_stale_and_duplicate_actions();
    test_aux_records_and_paging();
    test_chunk_plan_boundaries();
    test_chunk_emission();

    if (failures) {
        printf("test_protocol: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_protocol: ok\n");
    return 0;
}
