/* test_protocol.c -- strict parsing, framing, counters, retries, paging.
 *
 * Engine-free golden vectors for doc/agent-interface.md.  No engine headers,
 * no engine objects.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "agent_protocol.h"
#include "agent_menu.h"

static int failures;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

/* ---- transport harness with controllable fragmentation ---- */
#define OUT_CAP (3u * 1024u * 1024u)

struct io {
    char in[16384];
    size_t inlen, inpos;
    char out[OUT_CAP];
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
    agent_session_free(&sess);
    memset(&io, 0, sizeof io);
    agent_session_init(&sess, rd, wr, &io);
}

static void
feed(const char *s)
{
    size_t n = strlen(s);

    if (io.inlen + n > sizeof io.in)
        return;
    memcpy(io.in + io.inlen, s, n);
    io.inlen += n;
}

static const char *
outstr(void)
{
    if (io.outlen < sizeof io.out)
        io.out[io.outlen] = '\0';
    else
        io.out[sizeof io.out - 1] = '\0';
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

/* ---- minimal JSON scanning helpers (test-side, not production) ---- */

/* length of the JSON value starting at p */
static size_t
span_value(const char *p)
{
    const char *start = p;
    int depth = 0;
    bool instr = false;

    while (*p) {
        char ch = *p;

        if (instr) {
            if (ch == '\\' && p[1]) {
                p += 2;
                continue;
            }
            if (ch == '"')
                instr = false;
            ++p;
            continue;
        }
        if (ch == '"') {
            instr = true;
            ++p;
            continue;
        }
        if (ch == '[' || ch == '{') {
            ++depth;
        } else if (ch == ']' || ch == '}') {
            if (depth == 0)
                break;
            --depth;
        } else if (ch == ',' && depth == 0) {
            break;
        }
        ++p;
        if (ch == '[' || ch == '{' || ch == ']' || ch == '}')
            continue;
        if (depth == 0 && (*p == ',' || *p == '}' || *p == ']'))
            break;
    }
    return (size_t) (p - start);
}

/* pointer just past the "key": of a top-level member, or NULL */
static const char *
find_key(const char *json, const char *key)
{
    char pat[32];
    const char *p;

    snprintf(pat, sizeof pat, "\"%s\":", key);
    p = strstr(json, pat);
    return p ? p + strlen(pat) : NULL;
}

/* Copy one top-level array's *content* (without the brackets) into out. */
static bool
array_content(const char *json, const char *key, char *out, size_t cap)
{
    const char *p = find_key(json, key);
    const char *start;
    int depth = 0;
    bool instr = false;

    if (!p || *p != '[')
        return false;
    start = ++p;
    while (*p) {
        char ch = *p;

        if (instr) {
            if (ch == '\\' && p[1]) {
                p += 2;
                continue;
            }
            if (ch == '"')
                instr = false;
            ++p;
            continue;
        }
        if (ch == '"') {
            instr = true;
        } else if (ch == '[' || ch == '{') {
            ++depth;
        } else if (ch == ']' || ch == '}') {
            if (depth == 0)
                break;
            --depth;
        }
        ++p;
    }
    if (*p != ']' || (size_t) (p - start) + 1 > cap)
        return false;
    memcpy(out, start, (size_t) (p - start));
    out[p - start] = '\0';
    return true;
}

/* Collect part values in stream order for one chunk-part tag. */
static bool
collect_parts(const char *stream, const char *tag, char *out, size_t cap)
{
    char pat[32];
    const char *p = stream;
    size_t n = 0;
    bool first = true;

    snprintf(pat, sizeof pat, "{\"p\":\"%s\"", tag);
    while ((p = strstr(p, pat)) != NULL) {
        const char *v = strstr(p, "\"val\":");

        if (!v)
            return false;
        v += 6;
        {
            size_t len = span_value(v);

            if (n + len + 2 > cap)
                return false;
            if (!first && n + 1 < cap)
                out[n++] = ',';
            first = false;
            memcpy(out + n, v, len);
            n += len;
        }
        p = v;
    }
    out[n] = '\0';
    return true;
}

/* Collect the concatenated text of every t part for one field. */
static bool
collect_text_parts(const char *stream, const char *tag, long ref,
                   const char *field, char *out, size_t cap)
{
    char pat[64];
    const char *p = stream;
    size_t n = 0;
    long last_off = 0;

    snprintf(pat, sizeof pat, "{\"p\":\"t\",\"k\":\"%s\",\"e\":%ld,"
                              "\"f\":\"%s\"", tag, ref, field);
    while ((p = strstr(p, pat)) != NULL) {
        const char *o = strstr(p, "\"offset\":");

        if (!o)
            return false;
        o += 9;
        if ((long) strtol(o, NULL, 10) != last_off)
            return false;
        {
            const char *v = strstr(p, "\"text\":\"");

            if (!v)
                return false;
            v += 8;
            while (*v && *v != '"') {
                if (*v == '\\')
                    return false; /* reconstruction test uses bare ASCII */
                if (n + 1 < cap)
                    out[n++] = *v;
                ++v;
                ++last_off;
            }
        }
        p = o;
    }
    out[n] = '\0';
    return true;
}

/* a small view used by the commit tests */
static struct agent_view view;
static char textbuf[AG_MAX_TEXT_BYTES + 4096];

/* The view borrows heap arrays in production; the fixtures borrow these small
 * static backing arrays so `v->msg`/`v->hist` are always non-NULL. */
static struct agent_msg fix_msg[1024];
static struct agent_msg fix_hist[1024];

static void
fix_attach(struct agent_view *v)
{
    v->msg = fix_msg;
    v->hist = fix_hist;
}

static void
build_view(int ncells)
{
    int i;

    memset(&view, 0, sizeof view);
    fix_attach(&view);
    view.full = true;
    view.pal[0].ch = AG_BLANK_CHAR;
    view.pal[0].fg = AG_COL_NONE;
    view.pal[1].ch = '.';
    view.pal[1].fg = AG_COL_GRAY;
    view.pal[2].ch = '@';
    view.pal[2].fg = AG_COL_WHITE;
    view.npal = 3;
    for (i = 0; i < ncells; ++i) {
        int y = AG_MAP_MIN_Y + i / AG_MAP_MAX_X;
        int x = AG_MAP_MIN_X + i % AG_MAP_MAX_X;

        if (y > AG_MAP_MAX_Y)
            break;
        view.map[y][x] = (i == 0) ? 2 : 1;
    }
    view.has_cursor = true;
    view.cur_x = AG_MAP_MIN_X;
    view.cur_y = AG_MAP_MIN_Y;
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

static void
need_cmd(struct agent_need *n, uint64_t id)
{
    memset(n, 0, sizeof *n);
    n->kind = AG_NEED_COMMAND;
    n->id = id;
}

/* receive a fresh action, returning the result and requiring AG_OK */
static enum agent_result
recv(struct agent_action *a)
{
    return agent_receive(&sess, a);
}

/* ================================================================= */

static void
test_parse_accepts(void)
{
    struct agent_action a;
    static const char key_h[] = "{\"v\":1,\"type\":\"act\",\"id\":7,"
                                "\"action\":{\"key\":104}}";
    static const char key_seq[] = "{\"v\":1,\"type\":\"act\",\"seq\":3,"
                                  "\"id\":8,\"action\":{\"key\":104}}";
    struct agent_commit_row c[4];

    prep(&a);
    CHECK(agent_parse_action(key_h, 0, &a) == AG_BAD_INPUT);
    CHECK(agent_parse_action(key_h, sizeof key_h - 1, &a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.id == 7 && a.key == 'h' && !a.has_seq);

    CHECK(agent_parse_action(key_seq, sizeof key_seq - 1, &a) == AG_OK);
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
                                 "\"commit\":[[5,3],[2,-1]]}}";
        prep(&a);
        CHECK(agent_parse_action(mn, sizeof mn - 1, &a) == AG_OK);
        CHECK(a.kind == AG_ACT_MENU && a.ncommit == 2);
        CHECK(strcmp(a.menu, "m4") == 0);
        CHECK(a.commit[0].r == 5 && a.commit[0].count == 3);
        CHECK(a.commit[1].r == 2 && a.commit[1].count == -1);
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
    (void) c;
}

static void
test_parse_rejects(void)
{
    struct agent_action a;
    struct agent_commit_row c[4];
    static const char *const bad[] = {
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
        "{\"v\":1,\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"key\":1,\"key\":2}}",
        "{\"v\":1,\"type\":\"act\",\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1}",
        "{\"v\":2,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"obs\",\"id\":1,\"action\":{\"key\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":256}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1.5}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1e2}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":01}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":-1}}",
        /* column zero is the internal sentinel, never a wire value */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[0,0],"
        "\"mod\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[80,0],"
        "\"mod\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[1,21],"
        "\"mod\":0}}",
        /* mod is frozen to zero */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[1,1],"
        "\"mod\":1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[1,1],"
        "\"mod\":-1}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"position\":[1,1]}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"count\":2}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"mod\":0}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"menu\":\"m1\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"menu\":\"x1\","
        "\"commit\":[]}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1,"
        "\"cancel\":true}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}}x",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"text\":\"a\\u0000b\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,"
        "\"action\":{\"text\":\"\\ud800\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"text\":\"a\x01\" "
        "\"b\"}}",
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}\x80}",
        /* a trailing space is not content but a trailing byte is */
        "{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":1}},"
    };
    size_t i;

    for (i = 0; i < sizeof bad / sizeof bad[0]; ++i) {
        prep(&a);
        if (agent_parse_action(bad[i], strlen(bad[i]), &a) == AG_OK) {
            printf("FAIL: unexpectedly accepted: %s\n", bad[i]);
            ++failures;
        }
    }

    /* commit rows beyond caller storage fail closed */
    {
        static const char many[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                   "\"action\":{\"menu\":\"m1\","
                                   "\"commit\":[[1,1],[2,1],[3,1]]}}";
        prep(&a);
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
        prep(&a);
        CHECK(agent_parse_action(z, sizeof z - 1, &a) != AG_OK);
        CHECK(agent_parse_action(r, sizeof r - 1, &a) != AG_OK);
    }
    (void) c;
}

static void
test_integer_and_token_bounds(void)
{
    struct agent_action a;

    /* LLONG_MIN and its neighbours must not invoke undefined negation */
    {
        static const char minv[] = "{\"v\":1,\"type\":\"act\",\"seq\":"
                                   "-9223372036854775808,\"id\":1,"
                                   "\"action\":{\"key\":1}}";
        static const char minp1[] = "{\"v\":1,\"type\":\"act\",\"seq\":"
                                    "-9223372036854775807,\"id\":1,"
                                    "\"action\":{\"key\":1}}";
        static const char minm1[] = "{\"v\":1,\"type\":\"act\",\"seq\":"
                                    "-9223372036854775809,\"id\":1,"
                                    "\"action\":{\"key\":1}}";

        prep(&a);
        CHECK(agent_parse_action(minv, sizeof minv - 1, &a) == AG_BAD_INPUT);
        CHECK(agent_parse_action(minp1, sizeof minp1 - 1, &a)
              == AG_BAD_INPUT);
        CHECK(agent_parse_action(minm1, sizeof minm1 - 1, &a)
              == AG_BAD_INPUT);
    }
    /* a near-boundary counter value is accepted, one past it is not */
    {
        static const char okmax[] = "{\"v\":1,\"type\":\"act\",\"id\":"
                                    "9007199254740991,\"action\":"
                                    "{\"key\":1}}";
        static const char over[] = "{\"v\":1,\"type\":\"act\",\"id\":"
                                   "9007199254740992,\"action\":"
                                   "{\"key\":1}}";

        prep(&a);
        CHECK(agent_parse_action(okmax, sizeof okmax - 1, &a) == AG_OK);
        CHECK(a.id == AG_COUNTER_MAX);
        CHECK(agent_parse_action(over, sizeof over - 1, &a) == AG_BAD_INPUT);
    }
    /* the token budget is enforced before a huge line can be parsed */
    {
        static char big[AG_MAX_ACTION_BYTES + 4096];
        size_t n = 0;
        bool first = true;
        int i;

        n += (size_t) snprintf(big + n, sizeof big - n,
                               "{\"v\":1,\"type\":\"act\",\"id\":1,"
                               "\"action\":{\"menu\":\"m1\","
                               "\"commit\":[");
        for (i = 0; i < 11500; ++i)
            n += (size_t) snprintf(big + n, sizeof big - n, "%s[1,1]",
                                   first ? "" : ","), first = false;
        n += (size_t) snprintf(big + n, sizeof big - n, "]}}");
        CHECK(n < sizeof big);
        CHECK(n < AG_MAX_ACTION_BYTES + 4096);
        prep(&a);
        CHECK(agent_parse_action(big, n, &a) != AG_OK);
    }
}

static void
test_escaping(void)
{
    {
        static const char in[] = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                                 "\"action\":{\"text\":\"a\\\"b\\\\c\"}}";
        struct agent_action a;

        prep(&a);
        CHECK(agent_parse_action(in, sizeof in - 1, &a) == AG_OK);
        CHECK(strcmp(a.text, "a\"b\\c") == 0);
    }
    {
        struct agent_view v;
        struct agent_need n;

        build_view(3);
        v = view;
        v.nmsg = 1;
        v.msg[0].text = "a\"b\\c\nd";
        reset_io();
        need_cmd(&n, 1);
        CHECK(agent_commit(&sess, &v, &n) == AG_OK);
        CHECK(strstr(outstr(), "a\\\"b\\\\c\\nd") != NULL);
    }
}

static void
test_hello_and_closed_once(void)
{
    static const char expect_hello[] =
        "{\"v\":1,\"ch\":\"control\",\"type\":\"hello\",\"d\":1,"
        "\"profile\":\"normal-ascii-color-v1\",\"policy\":\"llm-final-v1\","
        "\"caps\":[\"snapshot\",\"menu\",\"paging\"],"
        "\"coord\":\"engine-map\",\"size\":[80,21],\"x0\":1,\"y0\":0,"
        "\"limits\":{\"line\":65536,\"page_bytes\":16384,\"page_rows\":128,"
        "\"count\":2147483647}}\n";
    static const char expect_closed[] =
        "{\"v\":1,\"ch\":\"control\",\"type\":\"closed\"}\n";

    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    CHECK(strcmp(outstr(), expect_hello) == 0);
    /* hello is emitted at most once, and the duplicate emits nothing */
    CHECK(agent_write_hello(&sess) == AG_BAD_INPUT);
    CHECK(count_sub(outstr(), "\"type\":\"hello\"") == 1);
    CHECK(sess.next_delivery == 1);

    CHECK(agent_write_closed(&sess) == AG_OK);
    CHECK(agent_write_closed(&sess) == AG_BAD_INPUT);
    CHECK(count_sub(outstr(), "\"type\":\"closed\"") == 1);
    {
        const char *p = strstr(outstr(), expect_closed);

        CHECK(p != NULL);
        CHECK(strcmp(p, expect_closed) == 0);
        CHECK(strstr(p, "\"d\"") == NULL); /* the closed-counter exception */
    }
    CHECK(sess.closed == true);
}

static void
test_commit_shape(void)
{
    struct agent_need need;
    const char *line;

    build_view(4);
    reset_io();
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    line = outstr();
    /* every full observation carries the complete fixed field set */
    CHECK(strstr(line, "\"s\":{") != NULL);
    CHECK(strstr(line, "\"cond\":[") != NULL);
    CHECK(strstr(line, "\"pal\":[") != NULL);
    CHECK(strstr(line, "\"map\":[") != NULL);
    CHECK(strstr(line, "\"cur\":") != NULL);
    CHECK(strstr(line, "\"msg\":[") != NULL);
    CHECK(strstr(line, "\"hist\":[") != NULL);
    CHECK(strstr(line, "\"windows\":[") != NULL);
    CHECK(strstr(line, "\"need\":{\"id\":1") != NULL);
    CHECK(strstr(line, "\"seq\":1") != NULL);
    CHECK(strstr(line, "\"base\":null") != NULL);

    /* an empty view still emits every collection, as empty */
    {
        struct agent_view empty;

        memset(&empty, 0, sizeof empty);
        empty.pal[0].ch = AG_BLANK_CHAR;
        empty.pal[0].fg = AG_COL_NONE;
        empty.pal[0].frame = AG_COL_NONE;
        empty.npal = 1;
        empty.full = true;
        reset_io();
        CHECK(agent_commit(&sess, &empty, NULL) == AG_OK);
        line = outstr();
        CHECK(strstr(line, "\"s\":{}") != NULL);
        CHECK(strstr(line, "\"cond\":[]") != NULL);
        CHECK(strstr(line,
                     "\"pal\":[[0,\" \",\"none\",0,\"none\"]]")
              != NULL);
        CHECK(strstr(line, "\"map\":[]") != NULL);
        CHECK(strstr(line, "\"cur\":null") != NULL);
        CHECK(strstr(line, "\"msg\":[]") != NULL);
        CHECK(strstr(line, "\"hist\":[]") != NULL);
        CHECK(strstr(line, "\"windows\":[]") != NULL);
        CHECK(strstr(line, "\"need\":null") != NULL);
    }
}

/* ---- finding 1: two-phase acceptance ---- */
static void
test_two_phase_acceptance(void)
{
    struct agent_action a;
    struct agent_need need;
    struct agent_menu m;
    struct agent_menu_row rows[3];
    struct agent_menu_answer ans;
    struct agent_selection sel;
    struct agent_selection_row srows[3];
    struct agent_commit_row commit[3];
    const char *wrong = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                        "\"action\":{\"menu\":\"m2\",\"commit\":[[1,1]]}}\n";
    const char *right = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                        "\"action\":{\"menu\":\"m1\",\"commit\":[[1,1]]}}\n";

    prep(&a);
    build_view(2);
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = 9;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = 0;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* a valid-shape action naming a stale menu generation is rejected at the
     * protocol layer, and the request stays outstanding */
    feed(wrong);
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_STALE);
    CHECK(sess.outstanding_id == 9);
    CHECK(sess.have_action == false);

    /* the corrected action with the same request id is accepted */
    feed(right);
    CHECK(recv(&a) == AG_OK);
    CHECK(a.id == 9 && strcmp(a.menu, "m1") == 0);
    /* the session has NOT recorded acceptance yet */
    CHECK(sess.have_action == false);

    /* the caller now runs semantic validation and rejects a duplicate row */
    memset(&rows, 0, sizeof rows);
    rows[0].r = 1;
    snprintf(rows[0].text, sizeof rows[0].text, "a");
    rows[0].selectable = true;
    rows[1].r = 2;
    snprintf(rows[1].text, sizeof rows[1].text, "b");
    rows[1].selectable = true;
    memset(&m, 0, sizeof m);
    m.id = "m1";
    m.mode = AG_MENU_ANY;
    m.rows = rows;
    m.nrows = 2;
    commit[0].r = 1;
    commit[0].count = 1;
    commit[1].r = 1; /* duplicate row id */
    commit[1].count = 1;
    memset(&ans, 0, sizeof ans);
    ans.rows = commit;
    ans.nrows = 2;
    memset(&sel, 0, sizeof sel);
    sel.rows = srows;
    sel.cap = 3;
    CHECK(agent_menu_validate(&m, &ans, &sel) == AG_BAD_INPUT);
    /* the action was never accepted, so the request is still outstanding and
     * the same request id can be resubmitted */
    CHECK(sess.have_action == false);
    CHECK(sess.outstanding_id == 9);

    feed(right);
    CHECK(recv(&a) == AG_OK);
    CHECK(agent_accept(&sess) == AG_OK);
    CHECK(sess.have_action == true);
    CHECK(sess.action_id == 9);
}

/* ---- finding 7: action grammar ---- */
static void
test_action_grammar(void)
{
    struct agent_action a;
    struct agent_need need;

    prep(&a);
    build_view(2);

    /* cancel is permitted for a line request */
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_LINE;
    need.id = 4;
    need.max = 10;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":4,\"action\":{\"text\":\"x\"}}\n");
    CHECK(recv(&a) == AG_OK && a.kind == AG_ACT_TEXT);
    CHECK(agent_accept(&sess) == AG_OK);
    reset_io();
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":4,"
         "\"action\":{\"cancel\":true}}\n");
    CHECK(recv(&a) == AG_OK && a.kind == AG_ACT_CANCEL);

    /* cancel is permitted for an extended-command request */
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_EXTCMD;
    need.id = 5;
    need.max = 10;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":5,"
         "\"action\":{\"cancel\":true}}\n");
    CHECK(recv(&a) == AG_OK && a.kind == AG_ACT_CANCEL);

    /* a position answer must lie inside the advertised rectangle */
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_POSITION;
    need.id = 6;
    need.x0 = 5;
    need.y0 = 5;
    need.x1 = 10;
    need.y1 = 10;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* x=0 is rejected by the parser and nothing is consumed */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"position\":[0,6],"
         "\"mod\":0}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_RANGE);
    CHECK(sess.outstanding_id == 6);
    /* a nonzero mod is rejected as well */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"position\":[6,6],"
         "\"mod\":1}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(sess.outstanding_id == 6);
    /* a position outside the rectangle is rejected at the protocol layer */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"position\":[20,6],"
         "\"mod\":0}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_RANGE);
    CHECK(sess.outstanding_id == 6);
    CHECK(sess.have_action == false);
    /* the corrected position is accepted */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"position\":[6,6],"
         "\"mod\":0}}\n");
    CHECK(recv(&a) == AG_OK && a.kind == AG_ACT_POSITION && a.px == 6);
}

/* ---- finding 6: strict auxiliary parsing ---- */
static void
test_aux_strict(void)
{
    struct agent_action a;
    struct agent_need need;
    static const char *const bad[] = {
        /* duplicate keys */
        "{\"v\":1,\"type\":\"ack_seq\",\"seq\":1,\"seq\":2}\n",
        /* wrong protocol version */
        "{\"v\":2,\"type\":\"ack_seq\",\"seq\":1}\n",
        /* additional property */
        "{\"v\":1,\"type\":\"ack_seq\",\"seq\":1,\"junk\":1}\n",
        /* trailing content */
        "{\"v\":1,\"type\":\"ack_seq\",\"seq\":1}}x\n",
        /* malformed / missing fields */
        "{\"v\":1,\"type\":\"ack_seq\"}\n",
        "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":1}\n",
        "{\"v\":1,\"type\":\"get_page\",\"id\":1}\n",
        /* out-of-range integers */
        "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":1,\"i\":-1}\n",
        "{\"v\":1,\"type\":\"ack_seq\",\"seq\":0}\n",
        /* unknown record type */
        "{\"v\":1,\"type\":\"resync\"}\n"
    };
    size_t i;

    prep(&a);
    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    for (i = 0; i < sizeof bad / sizeof bad[0]; ++i) {
        reset_io();
        CHECK(agent_write_hello(&sess) == AG_OK);
        feed(bad[i]);
        CHECK(agent_receive(&sess, &a) == AG_IO);
        CHECK(count_sub(outstr(), "\"type\":\"invalid\"") >= 1);
    }

    /* a get_page naming a different request id is rejected */
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = 9;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = 2;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":8,\"content\":\"c1\","
         "\"page\":0}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"kind\"") == 1);

    /* a stale durable acknowledgement is rejected and does not advance */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    feed("{\"v\":1,\"type\":\"ack_seq\",\"seq\":99}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"stale\"") == 1);
    CHECK(sess.acked_seq == 0);
}

/* ---- finding 4: per-page delivery tracking ---- */
static void
test_per_page_delivery(void)
{
    struct agent_action a;
    struct agent_need need;
    const char *sel = "{\"v\":1,\"type\":\"act\",\"id\":9,"
                      "\"action\":{\"menu\":\"m1\",\"commit\":[[1,1]]}}\n";

    prep(&a);
    build_view(2);
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = 9;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = 2;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.need_pages == 2);

    /* page 0 requested twice: only one distinct page is delivered */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":0}\n");
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":0}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.pages_delivered == 1);
    CHECK(count_sub(outstr(), "\"type\":\"page\"") == 2);

    /* a selection is still forbidden: page 1 has not been delivered */
    feed(sel);
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_INCOMPLETE);
    CHECK(sess.outstanding_id == 9);

    /* an out-of-range page does not count */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":5}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.pages_delivered == 1);
    CHECK(count_sub(outstr(), "\"code\":\"range\"") == 1);

    /* delivering page 1 completes the requirement */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":9,\"content\":\"c1\","
         "\"page\":1}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.pages_delivered == 2);

    feed(sel);
    CHECK(recv(&a) == AG_OK);
    CHECK(a.kind == AG_ACT_MENU);
}

static void
test_fragmentation_and_short_writes(void)
{
    struct agent_action a;
    struct agent_need need;

    build_view(2);
    reset_io();
    io.chunk_in = 1;
    io.chunk_out = 3;
    CHECK(agent_write_hello(&sess) == AG_OK);
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":104}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(a.kind == AG_ACT_KEY && a.key == 'h');
    CHECK(io.write_calls > 3); /* short writes were retried to completion */

    reset_io();
    io.chunk_in = 5;
    io.chunk_out = 1;
    CHECK(agent_write_hello(&sess) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":9}}\n");
    feed("{\"v\":1,\"type\":\"act\",\"id\":2,\"action\":{\"key\":8}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(a.key == 9);
    CHECK(recv(&a) == AG_OK);
    CHECK(a.key == 8);
    CHECK(agent_receive(&sess, &a) == AG_IO); /* exhausted input */
}

static void
test_stale_and_duplicate_actions(void)
{
    struct agent_action a;
    struct agent_need need;
    const char *act5 = "{\"v\":1,\"type\":\"act\",\"id\":5,"
                       "\"action\":{\"key\":104}}\n";
    const char *act5b = "{\"v\":1,\"type\":\"act\",\"id\":5,"
                        "\"action\":{\"key\":105}}\n";

    prep(&a);
    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    need_cmd(&need, 5);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* a stale id consumes nothing and leaves the request outstanding */
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,\"action\":{\"key\":120}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_STALE);
    CHECK(sess.outstanding_id == 5);
    CHECK(sess.have_action == false);

    feed(act5);
    CHECK(recv(&a) == AG_OK);
    CHECK(agent_accept(&sess) == AG_OK);
    CHECK(sess.have_action && sess.action_id == 5);

    need_cmd(&need, 6);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.have_reply == true);
    {
        size_t before = io.outlen;

        feed(act5);
        feed("{\"v\":1,\"type\":\"act\",\"id\":6,"
             "\"action\":{\"key\":106}}\n");
        CHECK(recv(&a) == AG_OK);
        CHECK(a.id == 6);
        CHECK(sess.replays == 1);
        CHECK(io.outlen > before); /* the reply was re-written */
        CHECK(agent_accept(&sess) == AG_OK);
    }
    /* conflicting reuse of an accepted id closes the transport */
    feed("{\"v\":1,\"type\":\"act\",\"id\":6,\"action\":{\"key\":107}}\n");
    CHECK(recv(&a) == AG_INTERNAL);
    /* a superseded (stale) id is rejected without closing */
    feed(act5b);
    CHECK(recv(&a) == AG_BAD_INPUT);
}

/* ---- finding 5: multi-chunk retry replay and exact identity ---- */
static void
test_multichunk_retry_replay(void)
{
    struct agent_action a;
    struct agent_need need;
    const char *act = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                      "\"action\":{\"key\":104}}\n";

    prep(&a);
    build_view(120); /* large enough to force the chunk path */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 400;
    CHECK(agent_write_hello(&sess) == AG_OK);
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(count_sub(outstr(), "\"type\":\"chunk\"") > 1);
    /* no accepted action yet, so no retained response is usable */
    CHECK(sess.have_reply == false);

    feed(act);
    CHECK(recv(&a) == AG_OK);
    CHECK(agent_accept(&sess) == AG_OK);

    /* the next commit retains the complete multi-chunk response */
    need_cmd(&need, 2);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.reply_len > 1);
    CHECK(count_sub(sess.reply, "\"type\":\"chunk\"") > 1);

    /* an identical retry replays the whole logical stream byte for byte */
    {
        size_t before = io.outlen;

        feed(act);
        feed("{\"v\":1,\"type\":\"act\",\"id\":2,"
             "\"action\":{\"key\":106}}\n");
        CHECK(recv(&a) == AG_OK);
        CHECK(a.id == 2);
        CHECK(sess.replays == 1);
        CHECK(io.outlen == before + sess.reply_len);
        CHECK(memcmp(io.out + before, sess.reply, sess.reply_len) == 0);
    }
}

/* ---- finding 5: a hash collision must not be mistaken for a retry ---- */
static void
test_hash_collision_distinguished(void)
{
    struct agent_action a;
    struct agent_need need;
    /* These two distinct act records share the FNV-1a 32 hash 3834817770. */
    const char *A = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                    "\"action\":{\"text\":\"xjckybcc\"}}\n";
    const char *B = "{\"v\":1,\"type\":\"act\",\"id\":1,"
                    "\"action\":{\"text\":\"iemiwxkk\"}}\n";

    prep(&a);
    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_LINE;
    need.id = 1;
    need.max = 255;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    feed(A);
    CHECK(recv(&a) == AG_OK);
    CHECK(agent_accept(&sess) == AG_OK);
    need.id = 2;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.action_hash == 3834817770u);

    /* different bytes under the same hash is conflicting reuse */
    feed(B);
    CHECK(recv(&a) == AG_INTERNAL);
}

/* ---- finding 12: output counters refuse to wrap ---- */
static void
test_counter_bounds(void)
{
    struct agent_need need;
    struct agent_view v;

    reset_io();
    sess.next_delivery = AG_COUNTER_MAX;
    CHECK(agent_write_hello(&sess) == AG_LIMIT);

    reset_io();
    sess.next_delivery = AG_COUNTER_MAX;
    build_view(2);
    CHECK(agent_commit(&sess, &view, NULL) == AG_LIMIT);

    reset_io();
    sess.next_seq = AG_COUNTER_MAX;
    CHECK(agent_commit(&sess, &view, NULL) == AG_LIMIT);

    /* an event id one past the counter bound is refused */
    reset_io();
    v = view;
    v.nmsg = 1;
    v.msg[0].e = AG_COUNTER_MAX + 1;
    v.msg[0].text = "x";
    CHECK(agent_commit(&sess, &v, NULL) == AG_LIMIT);

    /* and one exactly at the bound is accepted */
    reset_io();
    v.msg[0].e = AG_COUNTER_MAX;
    CHECK(agent_commit(&sess, &v, NULL) == AG_OK);

    /* an outstanding need id past the bound is refused */
    reset_io();
    need_cmd(&need, AG_COUNTER_MAX + 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_LIMIT);
}

/* ---- findings 2b/2d: blank snapshots and sparse omission ---- */
static void
test_blank_snapshot_and_sparse(void)
{
    struct agent_view v;
    struct agent_need need;
    static char plain_map[16400], chunk_map[16400];
    static char plain_pal[16400], chunk_pal[16400];

    /* an all-blank final snapshot */
    memset(&v, 0, sizeof v);
    v.full = true;
    v.pal[0].ch = AG_BLANK_CHAR;
    v.pal[0].fg = AG_COL_NONE;
    v.npal = 1;
    reset_io();
    CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
    CHECK(strstr(outstr(), "\"map\":[]") != NULL);

    /* force the same record through the chunk path */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
    CHECK(count_sub(outstr(), "\"type\":\"chunk\"") >= 1);
    /* header parts carry only v, ch, type, seq, base - never d */
    CHECK(count_sub(outstr(), "\"p\":\"h\"") == 5);
    CHECK(strstr(outstr(), "\"k\":\"d\"") == NULL);
    CHECK(strstr(outstr(), "\"rid\":") != NULL);

    /* a sparse map reconstructs identically from both encodings */
    build_view(100);
    reset_io();
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    {
        static char arr[16384];

        CHECK(array_content(outstr(), "map", arr, sizeof arr));
        snprintf(plain_map, sizeof plain_map, "[%s]", arr);
        CHECK(array_content(outstr(), "pal", arr, sizeof arr));
        snprintf(plain_pal, sizeof plain_pal, "[%s]", arr);
    }

    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    {
        static char arr[16384];

        CHECK(collect_parts(outstr(), "map", arr, sizeof arr));
        snprintf(chunk_map, sizeof chunk_map, "[%s]", arr);
        CHECK(collect_parts(outstr(), "pal", arr, sizeof arr));
        snprintf(chunk_pal, sizeof chunk_pal, "[%s]", arr);
    }
    CHECK(strcmp(plain_map, chunk_map) == 0);
    CHECK(strcmp(plain_pal, chunk_pal) == 0);
    CHECK(count_sub(outstr(), "\"p\":\"map\"") == 100);
}

/* ---- finding 3: long-text chunking ---- */
static void
test_long_text(void)
{
    struct agent_view v;
    static char recon[AG_MAX_TEXT_BYTES + 64];
    size_t i;
    const size_t LEN = 200000;

    /* a text well past the physical line budget, forced multi-chunk */
    for (i = 0; i < LEN; ++i)
        textbuf[i] = (char) ('a' + (int) (i % 26));
    textbuf[LEN] = '\0';

    memset(&v, 0, sizeof v);
    fix_attach(&v);
    v.full = true;
    v.pal[0].ch = AG_BLANK_CHAR;
    v.pal[0].fg = AG_COL_NONE;
    v.npal = 1;
    v.nmsg = 1;
    v.msg[0].e = 7;
    v.msg[0].text = textbuf;
    v.msg[0].style = 0;

    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 4096;
    CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
    CHECK(count_sub(outstr(), "\"type\":\"chunk\"") > 1);
    CHECK(count_sub(outstr(), "\"p\":\"t\"") > 1);
    /* every chunk record stays within the physical budget */
    {
        const char *p = outstr();
        const char *nl;

        while ((nl = strchr(p, '\n')) != NULL) {
            size_t line = (size_t) (nl - p) + 1;

            CHECK(line <= sess.limit_line);
            p = nl + 1;
        }
    }
    /* the slices reconstruct the original text exactly */
    CHECK(collect_text_parts(outstr(), "msg", 7, "text", recon,
                             sizeof recon));
    CHECK(strlen(recon) == LEN);
    CHECK(memcmp(recon, textbuf, LEN) == 0);

    /* a 1 MiB value is supported; one byte over is refused */
    for (i = 0; i < AG_MAX_TEXT_BYTES; ++i)
        textbuf[i] = 'z';
    textbuf[AG_MAX_TEXT_BYTES] = '\0';
    v.msg[0].text = textbuf;
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 4096;
    CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
    memset(recon, 0, 16);
    CHECK(collect_text_parts(outstr(), "msg", 7, "text", recon,
                             sizeof recon));
    CHECK(strlen(recon) == AG_MAX_TEXT_BYTES);

    textbuf[AG_MAX_TEXT_BYTES] = 'z';
    textbuf[AG_MAX_TEXT_BYTES + 1] = '\0';
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    CHECK(agent_commit(&sess, &v, NULL) == AG_LIMIT);
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
    CHECK(agent_chunk_plan(parts, 6, 100, chunk_of, 16) == 1);
    CHECK(agent_chunk_plan(parts, 6, 30, chunk_of, 2) == 0);
}

static void
test_chunk_emission_details(void)
{
    struct agent_need need;
    int nonblank = 101;
    long rid0 = -1;
    int idx = -1;
    int chunks = 0;
    size_t saved_len;

    build_view(nonblank);
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 300;
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    CHECK(count_sub(outstr(), "\"p\":\"map\"") == nonblank);
    CHECK(count_sub(outstr(), "\"p\":\"h\"") == 5);
    CHECK(strstr(outstr(), "\"last\":true") != NULL);

    {
        char *line = io.out;
        char *next;

        while (line && *line) {
            char *nl = strchr(line, '\n');

            if (nl)
                *nl = '\0';
            if (strstr(line, "\"type\":\"chunk\"")) {
                const char *rp = strstr(line, "\"rid\":");
                const char *ip = strstr(line, "\"i\":");
                long r = rp ? strtol(rp + 6, NULL, 10) : -1;
                long i = ip ? strtol(ip + 4, NULL, 10) : -1;

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
    }
    CHECK(chunks > 1);
    CHECK(rid0 > 0);
    CHECK((uint64_t) rid0 == sess.last_rid);
    CHECK((long) chunks == sess.last_chunk_count);

    /* identical retry re-sends the exact last physical record */
    saved_len = io.outlen;
    CHECK(agent_retry_last(&sess) == AG_OK);
    CHECK(io.outlen == saved_len + sess.last_line_len);
    CHECK(memcmp(io.out + saved_len, sess.last_line,
                 sess.last_line_len) == 0);

    /* a chunk index beyond the stream is rejected */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.last_rid = 1;
    sess.last_chunk_count = 2;
    agent_commit(&sess, &view, &need);
    {
        struct agent_action a;

        prep(&a);
        feed("{\"v\":1,\"type\":\"ack_chunk\",\"rid\":1,\"i\":5}\n");
        CHECK(agent_receive(&sess, &a) == AG_IO);
        CHECK(count_sub(outstr(), "\"code\":\"range\"") == 1);
    }
    /* a non-contiguous acknowledgement is rejected */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 300;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    {
        struct agent_action a;
        char ack[96];
        long rid = (long) sess.last_rid;

        prep(&a);
        snprintf(ack, sizeof ack,
                 "{\"v\":1,\"type\":\"ack_chunk\","
                 "\"rid\":%ld,\"i\":2}\n", rid);
        feed(ack);
        CHECK(agent_receive(&sess, &a) == AG_IO);
        CHECK(count_sub(outstr(), "\"code\":\"incomplete\"") == 1);
        /* the contiguous acknowledgement is accepted */
        snprintf(ack, sizeof ack,
                 "{\"v\":1,\"type\":\"ack_chunk\","
                 "\"rid\":%ld,\"i\":0}\n", rid);
        feed(ack);
        CHECK(agent_receive(&sess, &a) == AG_IO);
        CHECK(sess.acked_chunk == 0);
    }
}

/* Emit real encoder output for schema validation by test-side tooling. */
static void
dump_vectors(void)
{
    struct agent_need need;

    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    fwrite(io.out, 1, io.outlen, stdout);

    build_view(3);
    reset_io();
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    fwrite(io.out, 1, io.outlen, stdout);

    /* a final boundary: need is null */
    reset_io();
    CHECK(agent_commit(&sess, &view, NULL) == AG_OK);
    fwrite(io.out, 1, io.outlen, stdout);

    /* the same record forced through the chunk path */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    CHECK(agent_commit(&sess, &view, NULL) == AG_OK);
    fwrite(io.out, 1, io.outlen, stdout);

    reset_io();
    CHECK(agent_write_closed(&sess) == AG_OK);
    fwrite(io.out, 1, io.outlen, stdout);
}


/* an independent UTF-8 validator for the test side */
static bool
utf8_ok(const char *s, size_t n)
{
    size_t i = 0;

    while (i < n) {
        unsigned char c = (unsigned char) s[i];
        size_t need;

        if (c < 0x80) {
            need = 1;
        } else if (c >= 0xc2 && c <= 0xdf) {
            need = 2;
        } else if (c >= 0xe0 && c <= 0xef) {
            need = 3;
        } else if (c >= 0xf0 && c <= 0xf4) {
            need = 4;
        } else {
            return false;
        }
        if (i + need > n)
            return false;
        if (need > 1) {
            size_t k;

            for (k = 1; k < need; ++k)
                if (((unsigned char) s[i + k] & 0xc0) != 0x80)
                    return false;
            if (need == 3 && c == 0xe0 && (unsigned char) s[i + 1] < 0xa0)
                return false;
            if (need == 3 && c == 0xed && (unsigned char) s[i + 1] >= 0xa0)
                return false;
            if (need == 4 && c == 0xf0 && (unsigned char) s[i + 1] < 0x90)
                return false;
            if (need == 4 && c == 0xf4 && (unsigned char) s[i + 1] >= 0x90)
                return false;
        }
        i += need;
    }
    return true;
}

/* every physical line of the current output must decode as UTF-8 */
static void
check_lines_utf8(const char *what)
{
    const char *p = outstr();
    const char *nl;

    while ((nl = strchr(p, '\n')) != NULL) {
        if (!utf8_ok(p, (size_t) (nl - p))) {
            printf("FAIL %s: a physical line is not valid UTF-8\n", what);
            ++failures;
        }
        p = nl + 1;
    }
}

/* ---- finding 1: map coordinates are native and never 0 or 80 ---- */
static void
test_map_coordinates(void)
{
    struct agent_view v;
    struct agent_need need;
    static char arr[512];
    const char *c;

    memset(&v, 0, sizeof v);
    v.full = true;
    v.pal[0].ch = AG_BLANK_CHAR;
    v.pal[0].fg = AG_COL_NONE;
    v.pal[1].ch = '.';
    v.pal[1].fg = AG_COL_GRAY;
    v.npal = 2;
    /* only native (1,0) and (79,20); native column zero is set but unused */
    v.map[0][0] = 1;
    v.map[0][1] = 1;
    v.map[20][79] = 1;
    v.has_cursor = true;
    v.cur_x = 1;
    v.cur_y = 0;

    reset_io();
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &v, &need) == AG_OK);
    CHECK(array_content(outstr(), "map", arr, sizeof arr));
    CHECK(strcmp(arr, "[1,0,1],[79,20,1]") == 0);
    c = find_key(outstr(), "cur");
    CHECK(c && strncmp(c, "[1,0]", 5) == 0);

    /* the same record forced through the chunk path */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    CHECK(agent_commit(&sess, &v, &need) == AG_OK);
    CHECK(collect_parts(outstr(), "map", arr, sizeof arr));
    CHECK(strcmp(arr, "[1,0,1],[79,20,1]") == 0);
    CHECK(collect_parts(outstr(), "cur", arr, sizeof arr));
    CHECK(strcmp(arr, "[1,0]") == 0);

    /* an out-of-range cursor fails closed */
    v.cur_x = 0;
    reset_io();
    CHECK(agent_commit(&sess, &v, &need) == AG_LIMIT);
    CHECK(io.outlen == 0);
    v.cur_x = 80;
    CHECK(agent_commit(&sess, &v, &need) == AG_LIMIT);
    CHECK(io.outlen == 0);
}

/* ---- finding 2: chunk acknowledgement has an explicit none state ---- */
static void
test_chunk_ack_state(void)
{
    struct agent_action a;
    struct agent_need need;
    char ack[128];
    long rid;

    prep(&a);
    build_view(60);
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 300;
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.last_chunk_count > 1);
    CHECK(sess.have_chunk_ack == false);
    rid = (long) sess.last_rid;

    /* a fresh stream cannot start above index 0 */
    snprintf(ack, sizeof ack,
             "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":%ld,\"i\":1}\n", rid);
    feed(ack);
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"incomplete\"") == 1);
    CHECK(sess.have_chunk_ack == false);

    /* index 0 starts the stream */
    snprintf(ack, sizeof ack,
             "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":%ld,\"i\":0}\n", rid);
    feed(ack);
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.have_chunk_ack == true);
    CHECK(sess.acked_chunk == 0);

    /* a repeat of 0 is idempotent */
    feed(ack);
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.acked_chunk == 0);

    /* then 1 advances */
    snprintf(ack, sizeof ack,
             "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":%ld,\"i\":1}\n", rid);
    feed(ack);
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.acked_chunk == 1);

    /* a second stream resets the high-water: i:1 is incomplete again */
    reset_io();
    agent_session_init(&sess, rd, wr, &io);
    sess.force_chunk = true;
    sess.limit_line = 300;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.have_chunk_ack == false);
    CHECK(sess.acked_chunk == 0);
    rid = (long) sess.last_rid;
    snprintf(ack, sizeof ack,
             "{\"v\":1,\"type\":\"ack_chunk\",\"rid\":%ld,\"i\":1}\n", rid);
    feed(ack);
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(count_sub(outstr(), "\"code\":\"incomplete\"") == 1);
    CHECK(sess.have_chunk_ack == false);
    CHECK(sess.acked_chunk == 0);
}

/* ---- finding 3: the advertised line/extcmd budget is enforced ---- */
static void
test_line_max(void)
{
    struct agent_action a;
    struct agent_need need;

    prep(&a);
    build_view(2);
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_LINE;
    need.id = 3;
    need.max = 5;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.need_max == 5);

    /* max-1 bytes is accepted */
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,"
         "\"action\":{\"text\":\"abcd\"}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(strlen(a.text) == 4);
    CHECK(agent_accept(&sess) == AG_OK);

    /* exactly max is accepted */
    reset_io();
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,"
         "\"action\":{\"text\":\"abcde\"}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(strlen(a.text) == 5);

    /* max+1 is rejected, and the request stays resubmittable */
    reset_io();
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,"
         "\"action\":{\"text\":\"abcdef\"}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(a.code == AG_INV_RANGE);
    CHECK(sess.outstanding_id == 3);
    CHECK(sess.pending_len == 0);
    feed("{\"v\":1,\"type\":\"act\",\"id\":3,"
         "\"action\":{\"text\":\"abcde\"}}\n");
    CHECK(recv(&a) == AG_OK);

    /* the budget counts bytes, not code points: two 2-byte scalars fit five
     * bytes, three of them do not */
    reset_io();
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    {
        static const char two[] = "\xc3\xa9" "\xc3\xa9";
        char line[128];
        size_t n;

        n = (size_t) snprintf(line, sizeof line,
                              "%s%s%s",
                              "{\"v\":1,\"type\":\"act\",\"id\":3,"
                              "\"action\":{\"text\":\"", two, "\"}}\n");
        memcpy(line + n - 5, two, 4);
        /* rebuild cleanly rather than fighting the format string */
        n = 0;
        n += (size_t) snprintf(line + n, sizeof line - n, "%s",
                               "{\"v\":1,\"type\":\"act\",\"id\":3,"
                               "\"action\":{\"text\":\"");
        memcpy(line + n, two, 4);
        n += 4;
        n += (size_t) snprintf(line + n, sizeof line - n, "%s", "\"}}\n");
        feed(line);
        CHECK(recv(&a) == AG_OK);
        CHECK(strlen(a.text) == 4);
    }
    reset_io();
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    {
        static const char three[] = "\xc3\xa9" "\xc3\xa9" "\xc3\xa9";
        char line[128];
        size_t n = 0;

        n += (size_t) snprintf(line + n, sizeof line - n, "%s",
                               "{\"v\":1,\"type\":\"act\",\"id\":3,"
                               "\"action\":{\"text\":\"");
        memcpy(line + n, three, 6);
        n += 6;
        n += (size_t) snprintf(line + n, sizeof line - n, "%s", "\"}}\n");
        feed(line);
        CHECK(recv(&a) == AG_BAD_INPUT);
        CHECK(a.code == AG_INV_RANGE);
        CHECK(sess.outstanding_id == 3);
    }

    /* an out-of-range advertised budget is refused at commit */
    reset_io();
    need.max = AG_LINE_INPUT_MAX + 1;
    CHECK(agent_commit(&sess, &view, &need) == AG_LIMIT);
    CHECK(io.outlen == 0);
}

/* ---- finding 4: the encoder validates UTF-8 before building ---- */
static void
test_encoder_utf8(void)
{
    struct agent_view v;
    static char text[4096];
    static char recon[4096 + 64];
    static const char mixed[] = "\xc3\xa9" "\xe2\x82\xac" "\xf0\x9f\x98\x80";
    size_t i, budget;

    memset(&v, 0, sizeof v);
    fix_attach(&v);
    v.full = true;
    v.pal[0].ch = AG_BLANK_CHAR;
    v.pal[0].fg = AG_COL_NONE;
    v.npal = 1;
    v.nmsg = 1;
    v.msg[0].e = 5;
    v.msg[0].style = 0;

    /* repeat the 2/3/4-byte scalars so a small budget forces many slices */
    for (i = 0; i + sizeof mixed - 1 <= sizeof text - 1;
         i += sizeof mixed - 1)
        memcpy(text + i, mixed, sizeof mixed - 1);
    text[i] = '\0';
    v.msg[0].text = text;

    /* split at every legal budget boundary: the reconstruction must be exact
     * and every physical line must still decode as UTF-8 */
    for (budget = 260; budget <= 1400; budget += 37) {
        reset_io();
        agent_session_init(&sess, rd, wr, &io);
        sess.force_chunk = true;
        sess.limit_line = budget;
        CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
        memset(recon, 0, sizeof recon);
        CHECK(collect_text_parts(outstr(), "msg", 5, "text", recon,
                                 sizeof recon));
        CHECK(strcmp(recon, text) == 0);
        check_lines_utf8("utf8 slice");
    }

    /* malformed, truncated, overlong and surrogate sequences fail closed */
    {
        static const char *const badtexts[] = {
            "\x80",             /* lone continuation byte */
            "\xc3\x28",         /* truncated 2-byte sequence */
            "\xe2\x82",         /* truncated 3-byte sequence */
            "\xf0\x9f\x98",     /* truncated 4-byte sequence */
            "\xc0\xaf",         /* overlong 2-byte encoding */
            "\xe0\x80\xaf",     /* overlong 3-byte encoding */
            "\xed\xa0\x80",     /* UTF-16 surrogate */
            "\xf5\x80\x80\x80", /* above U+10FFFF */
            "\xff",             /* invalid lead byte */
            "ok\xc3"            /* valid prefix, then truncated */
        };
        size_t k;

        for (k = 0; k < sizeof badtexts / sizeof badtexts[0]; ++k) {
            v.msg[0].text = badtexts[k];
            reset_io();
            CHECK(agent_commit(&sess, &v, NULL) == AG_LIMIT);
            CHECK(io.outlen == 0); /* fail closed: nothing is written */
        }
        /* a well-formed value is still accepted after the malformed ones */
        v.msg[0].text = "fine";
        reset_io();
        CHECK(agent_commit(&sess, &v, NULL) == AG_OK);
    }
}

/* ---- finding 5: acceptance binds only the pending identity ---- */
static void
test_accept_identity(void)
{
    struct agent_action a;
    struct agent_need need;

    prep(&a);
    build_view(2);
    reset_io();
    need_cmd(&need, 9);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);

    /* nothing pending: acceptance fails and changes nothing */
    CHECK(agent_accept(&sess) == AG_INTERNAL);
    CHECK(sess.have_action == false);
    CHECK(sess.action_id == 0);

    /* an unrelated id (8) is rejected by receive: never pending */
    feed("{\"v\":1,\"type\":\"act\",\"id\":8,\"action\":{\"key\":104}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(sess.pending_len == 0);
    CHECK(agent_accept(&sess) == AG_INTERNAL);
    CHECK(sess.action_id == 0);

    /* a zero request id is never a valid wire action */
    feed("{\"v\":1,\"type\":\"act\",\"id\":0,\"action\":{\"key\":104}}\n");
    CHECK(recv(&a) == AG_BAD_INPUT);
    CHECK(sess.pending_id == 0);
    CHECK(agent_accept(&sess) == AG_INTERNAL);
    CHECK(sess.action_id == 0);

    /* normal acceptance binds the session-owned id exactly once */
    feed("{\"v\":1,\"type\":\"act\",\"id\":9,\"action\":{\"key\":104}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(sess.pending_id == 9);
    CHECK(agent_accept(&sess) == AG_OK);
    CHECK(sess.action_id == 9);
    CHECK(sess.have_action == true);
    /* the pending identity is consumed exactly once */
    CHECK(agent_accept(&sess) == AG_INTERNAL);
    CHECK(sess.action_id == 9);
}

/* ---- finding 7: page count and index boundaries ---- */
static void
test_page_bounds(void)
{
    struct agent_action a;
    struct agent_need need;

    prep(&a);
    build_view(2);
    reset_io();
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_MENU;
    need.id = 1;
    need.menu = "m1";
    need.mode = AG_MENU_ANY;
    need.content = "c1";
    need.pages = AG_MAX_PAGES; /* exactly the maximum */
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    CHECK(sess.need_pages == 65535);

    /* exactly the last legal index is delivered */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":1,\"content\":\"c1\","
         "\"page\":65534}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.pages_delivered == 1);

    /* one index past the allowed maximum is rejected by the parser */
    feed("{\"v\":1,\"type\":\"get_page\",\"id\":1,\"content\":\"c1\","
         "\"page\":65535}\n");
    CHECK(agent_receive(&sess, &a) == AG_IO);
    CHECK(sess.pages_delivered == 1);
    CHECK(count_sub(outstr(), "\"code\":\"range\"") == 1);

    /* one page past the declared maximum is refused at commit */
    reset_io();
    need.pages = AG_MAX_PAGES + 1;
    CHECK(agent_commit(&sess, &view, &need) == AG_LIMIT);
    CHECK(io.outlen == 0);
}

/* ================================================================= */
/* M2 review findings                                                */
/* ================================================================= */

/* High 2: a commit may not silently replace an outstanding request whose
 * action the native handler has not accepted; once accepted, replacement is
 * allowed.  Nothing is emitted and no counter moves on the rejection. */
static void
test_commit_guard(void)
{
    struct agent_need n1, n2;
    struct agent_action a;
    size_t d, sq, olen;

    prep(&a);
    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    need_cmd(&n1, 1);
    CHECK(agent_commit(&sess, &view, &n1) == AG_OK);
    CHECK(sess.outstanding_id == 1);

    d = sess.next_delivery;
    sq = sess.next_seq;
    olen = io.outlen;
    need_cmd(&n2, 2);
    CHECK(agent_commit(&sess, &view, &n2) == AG_INTERNAL);
    CHECK(sess.next_delivery == d && sess.next_seq == sq);
    CHECK(io.outlen == olen);
    CHECK(sess.outstanding_id == 1);

    feed("{\"v\":1,\"type\":\"act\",\"id\":1,\"action\":{\"key\":104}}\n");
    CHECK(recv(&a) == AG_OK);
    CHECK(agent_accept(&sess) == AG_OK);
    CHECK(agent_commit(&sess, &view, &n2) == AG_OK);
    CHECK(sess.outstanding_id == 2);
}

/* Medium 3: a boundary carries every ordered message; never shift-dropped
 * past a fixed cap.  Too many for the byte limit is a fail-closed error,
 * not a partial snapshot. */
static void
test_message_store_complete(void)
{
    static char texts[200][24];
    struct agent_need need;
    size_t i;
    const char *p;

    build_view(2);
    for (i = 0; i < 200; ++i) {
        (void) snprintf(texts[i], sizeof texts[i], "message-%03u",
                        (unsigned) i);
        fix_msg[i].e = (uint64_t) (i + 1);
        fix_msg[i].text = texts[i];
        fix_msg[i].style = 0;
    }
    view.msg = fix_msg;
    view.nmsg = 200;
    view.hist = fix_hist;
    view.nhist = 0;

    reset_io();
    need_cmd(&need, 1);
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    p = outstr();
    CHECK(count_sub(p, "\"text\":\"message-") == 200);
    {
        const char *first = strstr(p, "\"text\":\"message-000\"");
        const char *last = strstr(p, "\"text\":\"message-199\"");

        CHECK(first != NULL && last != NULL && first < last);
    }

    /* a view that claims messages without storage fails closed */
    {
        struct agent_view bad = view;

        bad.msg = NULL;
        reset_io();
        CHECK(agent_commit(&sess, &bad, &need) == AG_LIMIT);
    }
}

/* Replace the full snapshot's own "d":N with a fixed token so two emissions
 * of the same content can be compared byte for byte. */
static void
strip_d(char *dst, size_t cap, const char *line)
{
    const char *d = find_key(line, "d");
    size_t head;
    const char *rest;

    if (!d) {
        snprintf(dst, cap, "%s", line);
        return;
    }
    head = (size_t) (d - line);
    if (head >= cap)
        head = cap - 1;
    memcpy(dst, line, head);
    dst[head] = '\0';
    rest = strchr(d, ',');
    if (rest)
        snprintf(dst + head, cap - head, "0%s", rest);
}

/* Medium 4: page planning is bounded by BOTH rows and encoded byte size, and
 * get_page emission uses the same plan, so pages are stable, ordered, in
 * insertion order, and idempotent on retry. */
static void
test_page_byte_plan(void)
{
    static struct agent_content_row rows[300];
    static char text[300][200];
    struct agent_need need;
    struct agent_action a;
    size_t pages, i, k;
    size_t npages = 0, idx = 0;
    bool fits = true, ordered = true;
    char first_page[AG_PAGE_MAX_BYTES + 64];
    size_t first_page_len = 0;

    for (i = 0; i < 300; ++i) {
        memset(text[i], (int) ('a' + (int) (i % 26)), 180);
        text[i][180] = '\0';
        memset(&rows[i], 0, sizeof rows[i]);
        rows[i].text = text[i];
    }
    pages = agent_content_pages(rows, 300);
    /* 128 rows of ~180 bytes would far exceed one page's byte budget, so the
     * plan must split well before the row-count limit */
    CHECK(pages >= 4);

    prep(&a);
    build_view(2);
    reset_io();
    CHECK(agent_write_hello(&sess) == AG_OK);
    memset(&need, 0, sizeof need);
    need.kind = AG_NEED_ACK;
    need.id = 41;
    need.content = "c7";
    need.pages = (int) pages;
    CHECK(agent_commit(&sess, &view, &need) == AG_OK);
    agent_session_set_content(&sess, rows, 300);

    for (k = 0; k < pages; ++k) {
        char g[96];

        (void) snprintf(g, sizeof g,
                        "{\"v\":1,\"type\":\"get_page\",\"id\":41,"
                        "\"content\":\"c7\",\"page\":%u}\n", (unsigned) k);
        feed(g);
    }
    CHECK(agent_receive(&sess, &a) == AG_IO);

    {
        const char *q = outstr();

        while (*q) {
            const char *nl = strchr(q, '\n');
            size_t len;
            char line[AG_PAGE_MAX_BYTES + 64];

            if (!nl)
                break;
            len = (size_t) (nl - q) + 1;
            if (len >= sizeof line) {
                fits = false;
                break;
            }
            memcpy(line, q, len);
            line[len] = '\0';
            if (strstr(line, "\"type\":\"page\"")) {
                const char *t;

                if (len > AG_PAGE_MAX_BYTES)
                    fits = false;
                if (npages == 0) {
                    size_t clen = len;

                    if (clen >= sizeof first_page)
                        clen = sizeof first_page - 1;
                    memcpy(first_page, line, clen);
                    first_page[clen] = '\0';
                    first_page_len = len;
                }
                ++npages;
                t = line;
                while ((t = strstr(t, "\"text\":\"")) != NULL) {
                    if (idx < 300
                        && t[8] != (char) ('a' + (int) (idx % 26)))
                        ordered = false;
                    ++idx;
                    t += 8;
                }
            }
            q = nl + 1;
        }
    }
    CHECK(npages == pages);
    CHECK(idx == 300);
    CHECK(ordered);
    CHECK(fits);
    CHECK(sess.pages_delivered == (int) pages);

    /* an idempotent retry of page 0: the delivered set is unchanged and the
     * re-emitted page equals the first one apart from its delivery counter */
    {
        char g[96];
        char second[AG_PAGE_MAX_BYTES + 64];
        char n0[AG_PAGE_MAX_BYTES + 64], n1[AG_PAGE_MAX_BYTES + 64];
        size_t base = io.outlen;

        (void) snprintf(g, sizeof g,
                        "{\"v\":1,\"type\":\"get_page\",\"id\":41,"
                        "\"content\":\"c7\",\"page\":0}\n");
        feed(g);
        CHECK(agent_receive(&sess, &a) == AG_IO);
        CHECK(sess.pages_delivered == (int) pages);
        {
            const char *q = io.out + base;
            const char *nl = strchr(q, '\n');
            size_t len = nl ? (size_t) (nl - q) + 1 : 0;

            CHECK(len > 0 && len < sizeof second);
            if (len > 0 && len < sizeof second) {
                memcpy(second, q, len);
                second[len] = '\0';
                strip_d(n0, sizeof n0, first_page);
                strip_d(n1, sizeof n1, second);
                CHECK(first_page_len > 0 && strcmp(n0, n1) == 0);
            }
        }
    }
}

int
main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--dump") == 0) {
        dump_vectors();
        return failures ? 1 : 0;
    }
    test_parse_accepts();
    test_parse_rejects();
    test_integer_and_token_bounds();
    test_escaping();
    test_hello_and_closed_once();
    test_commit_shape();
    test_two_phase_acceptance();
    test_action_grammar();
    test_aux_strict();
    test_per_page_delivery();
    test_fragmentation_and_short_writes();
    test_stale_and_duplicate_actions();
    test_multichunk_retry_replay();
    test_hash_collision_distinguished();
    test_counter_bounds();
    test_blank_snapshot_and_sparse();
    test_long_text();
    test_chunk_plan_boundaries();
    test_chunk_emission_details();
    test_map_coordinates();
    test_chunk_ack_state();
    test_line_max();
    test_encoder_utf8();
    test_accept_identity();
    test_page_bounds();
    test_commit_guard();
    test_message_store_complete();
    test_page_byte_plan();

    if (failures) {
        printf("test_protocol: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_protocol: ok\n");
    return 0;
}
