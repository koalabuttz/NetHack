/* agent_protocol.c -- bounded JSON framing, durable commits, session state.
 *
 * Engine-free.  Strict, allocation-bounded, and deterministic: no hash-table
 * iteration order, fixed field order in every encoder.
 */

#include "agent_protocol.h"

#include <stdlib.h>
#include <string.h>

/* The retained public state/content+unacked spool budget of
 * doc/agent-interface.md section 6. */
#define AG_MAX_LOGICAL_BYTES AG_MAX_RETAINED_BYTES
/* conservative fixed overhead of one chunk record around its parts */
#define AG_CHUNK_OVERHEAD 96u
/* headroom inside one chunk for a long-text fragment's own envelope */
#define AG_T_MARGIN 110u
/* largest number of physical fragments one logical record may expand to */
#define AG_MAX_FRAGS 8192

/* ------------------------------------------------------------------ */
/* growable byte buffer                                                */
/* ------------------------------------------------------------------ */

struct ag_buf {
    char *p;
    size_t len;
    size_t cap;
    bool ovf;
};

static void
ag_init(struct ag_buf *b)
{
    b->p = (char *) 0;
    b->len = 0;
    b->cap = 0;
    b->ovf = false;
}

static void
ag_free(struct ag_buf *b)
{
    if (b->p)
        free(b->p);
    ag_init(b);
}

static bool
ag_reserve(struct ag_buf *b, size_t extra)
{
    size_t need = b->len + extra;
    char *np;
    size_t ncap;

    if (need > AG_MAX_LOGICAL_BYTES) {
        b->ovf = true;
        return false;
    }
    if (need <= b->cap)
        return true;
    ncap = b->cap ? b->cap : 256;
    while (ncap < need)
        ncap *= 2;
    if (ncap > AG_MAX_LOGICAL_BYTES)
        ncap = AG_MAX_LOGICAL_BYTES;
    np = (char *) realloc(b->p, ncap);
    if (!np) {
        b->ovf = true;
        return false;
    }
    b->p = np;
    b->cap = ncap;
    return true;
}

static bool
ag_put(struct ag_buf *b, const char *s, size_t n)
{
    if (!ag_reserve(b, n))
        return false;
    if (n)
        memcpy(b->p + b->len, s, n);
    b->len += n;
    return true;
}

static bool
ag_puts(struct ag_buf *b, const char *s)
{
    return ag_put(b, s, strlen(s));
}

static bool
ag_putc(struct ag_buf *b, char c)
{
    return ag_put(b, &c, 1);
}

static bool
ag_put_u64(struct ag_buf *b, uint64_t v)
{
    char tmp[24];
    int i = 0;

    if (v == 0)
        return ag_putc(b, '0');
    while (v && i < (int) sizeof tmp)
        tmp[i++] = (char) ('0' + (v % 10)), v /= 10;
    while (i)
        if (!ag_putc(b, tmp[--i]))
            return false;
    return true;
}

static bool
ag_put_i64(struct ag_buf *b, long long v)
{
    if (v < 0)
        return ag_putc(b, '-') && ag_put_u64(b, (uint64_t) (-(v + 1)) + 1u);
    return ag_put_u64(b, (uint64_t) v);
}

/* Write a JSON string literal for the n bytes at s. */
static bool
ag_put_jstr_n(struct ag_buf *b, const char *s, size_t n)
{
    static const char hex[] = "0123456789abcdef";
    size_t i;

    if (!ag_putc(b, '"'))
        return false;
    for (i = 0; i < n; ++i) {
        unsigned char c = (unsigned char) s[i];

        switch (c) {
        case '"':
            if (!ag_puts(b, "\\\""))
                return false;
            break;
        case '\\':
            if (!ag_puts(b, "\\\\"))
                return false;
            break;
        case '\b':
            if (!ag_puts(b, "\\b"))
                return false;
            break;
        case '\f':
            if (!ag_puts(b, "\\f"))
                return false;
            break;
        case '\n':
            if (!ag_puts(b, "\\n"))
                return false;
            break;
        case '\r':
            if (!ag_puts(b, "\\r"))
                return false;
            break;
        case '\t':
            if (!ag_puts(b, "\\t"))
                return false;
            break;
        default:
            if (c < 0x20) {
                char esc[7];

                esc[0] = '\\';
                esc[1] = 'u';
                esc[2] = '0';
                esc[3] = '0';
                esc[4] = hex[(c >> 4) & 0xf];
                esc[5] = hex[c & 0xf];
                esc[6] = '\0';
                if (!ag_puts(b, esc))
                    return false;
            } else if (!ag_putc(b, (char) c)) {
                return false;
            }
            break;
        }
    }
    return ag_putc(b, '"');
}

static bool
ag_put_jstr(struct ag_buf *b, const char *s)
{
    return ag_put_jstr_n(b, s ? s : "", s ? strlen(s) : 0);
}

static bool
ag_put_color(struct ag_buf *b, uint8_t slot)
{
    const char *nm = agent_color_name(slot);

    if (!nm)
        nm = "none";
    return ag_put_jstr(b, nm);
}

/* ------------------------------------------------------------------ */
/* strict JSON reader                                                  */
/* ------------------------------------------------------------------ */

struct ag_cur {
    const char *p;
    const char *end;
    int depth;
    unsigned tokens;
};

static bool
ag_tick(struct ag_cur *c)
{
    return ++c->tokens <= AG_MAX_TOKENS;
}

static void
ag_ws(struct ag_cur *c)
{
    while (c->p < c->end && (*c->p == ' ' || *c->p == '\t'))
        ++c->p;
}

static bool
ag_utf8_len(const unsigned char *p, size_t n, size_t *len)
{
    unsigned char c = p[0];

    if (c < 0x80) {
        *len = 1;
        return true;
    }
    if (c >= 0xc2 && c <= 0xdf) {
        if (n < 2 || (p[1] & 0xc0) != 0x80)
            return false;
        *len = 2;
        return true;
    }
    if (c >= 0xe0 && c <= 0xef) {
        if (n < 3 || (p[1] & 0xc0) != 0x80 || (p[2] & 0xc0) != 0x80)
            return false;
        if (c == 0xe0 && p[1] < 0xa0)
            return false;
        if (c == 0xed && p[1] >= 0xa0) /* surrogates */
            return false;
        *len = 3;
        return true;
    }
    if (c >= 0xf0 && c <= 0xf4) {
        if (n < 4 || (p[1] & 0xc0) != 0x80 || (p[2] & 0xc0) != 0x80
            || (p[3] & 0xc0) != 0x80)
            return false;
        if (c == 0xf0 && p[1] < 0x90)
            return false;
        if (c == 0xf4 && p[1] >= 0x90)
            return false;
        *len = 4;
        return true;
    }
    return false;
}

static bool
ag_hex4(struct ag_cur *c, unsigned *out)
{
    unsigned v = 0;
    int i;

    for (i = 0; i < 4; ++i) {
        unsigned char ch;

        if (c->p >= c->end)
            return false;
        ch = (unsigned char) *c->p++;
        v <<= 4;
        if (ch >= '0' && ch <= '9')
            v |= (unsigned) (ch - '0');
        else if (ch >= 'a' && ch <= 'f')
            v |= (unsigned) (ch - 'a' + 10);
        else if (ch >= 'A' && ch <= 'F')
            v |= (unsigned) (ch - 'A' + 10);
        else
            return false;
    }
    *out = v;
    return true;
}

static bool
ag_put_utf8(struct ag_buf *b, unsigned cp)
{
    if (cp < 0x80)
        return ag_putc(b, (char) cp);
    if (cp < 0x800)
        return ag_putc(b, (char) (0xc0 | (cp >> 6)))
               && ag_putc(b, (char) (0x80 | (cp & 0x3f)));
    if (cp < 0x10000)
        return ag_putc(b, (char) (0xe0 | (cp >> 12)))
               && ag_putc(b, (char) (0x80 | ((cp >> 6) & 0x3f)))
               && ag_putc(b, (char) (0x80 | (cp & 0x3f)));
    return ag_putc(b, (char) (0xf0 | (cp >> 18)))
           && ag_putc(b, (char) (0x80 | ((cp >> 12) & 0x3f)))
           && ag_putc(b, (char) (0x80 | ((cp >> 6) & 0x3f)))
           && ag_putc(b, (char) (0x80 | (cp & 0x3f)));
}

/* Parse one JSON string.  When out is non-NULL the decoded bytes are appended
 * to it (NUL is rejected).  Returns false on any malformed input. */
static bool
ag_string(struct ag_cur *c, struct ag_buf *out)
{
    if (c->p >= c->end || *c->p != '"' || !ag_tick(c))
        return false;
    ++c->p;
    for (;;) {
        unsigned char ch;

        if (c->p >= c->end)
            return false;
        ch = (unsigned char) *c->p;
        if (ch == '"') {
            ++c->p;
            return true;
        }
        if (ch < 0x20)
            return false; /* raw control character */
        if (ch == '\\') {
            ++c->p;
            if (c->p >= c->end)
                return false;
            ch = (unsigned char) *c->p++;
            switch (ch) {
            case '"':
            case '\\':
            case '/':
                if (out && !ag_putc(out, (char) ch))
                    return false;
                break;
            case 'b':
                if (out && !ag_putc(out, '\b'))
                    return false;
                break;
            case 'f':
                if (out && !ag_putc(out, '\f'))
                    return false;
                break;
            case 'n':
                if (out && !ag_putc(out, '\n'))
                    return false;
                break;
            case 'r':
                if (out && !ag_putc(out, '\r'))
                    return false;
                break;
            case 't':
                if (out && !ag_putc(out, '\t'))
                    return false;
                break;
            case 'u': {
                unsigned cp;

                if (!ag_hex4(c, &cp))
                    return false;
                if (cp == 0)
                    return false; /* NUL is rejected on the wire */
                if (cp >= 0xd800 && cp <= 0xdbff) {
                    unsigned lo;

                    if (c->p + 1 >= c->end || c->p[0] != '\\'
                        || c->p[1] != 'u')
                        return false;
                    c->p += 2;
                    if (!ag_hex4(c, &lo) || lo < 0xdc00 || lo > 0xdfff)
                        return false;
                    cp = 0x10000u + ((cp - 0xd800u) << 10) + (lo - 0xdc00u);
                } else if (cp >= 0xdc00 && cp <= 0xdfff) {
                    return false; /* lone low surrogate */
                }
                if (out && !ag_put_utf8(out, cp))
                    return false;
                break;
            }
            default:
                return false;
            }
            continue;
        }
        if (ch < 0x80) {
            if (out && !ag_putc(out, (char) ch))
                return false;
            ++c->p;
        } else {
            size_t n;

            if (!ag_utf8_len((const unsigned char *) c->p,
                             (size_t) (c->end - c->p), &n))
                return false;
            if (out && !ag_put(out, c->p, n))
                return false;
            c->p += n;
        }
    }
}

/* Parse one integer.  Rejects fractions, exponents, '+', leading zeros.
 * LLONG_MIN is handled explicitly so no negation of the minimum signed value
 * is ever evaluated. */
#define AG_I64_MIN (-9223372036854775807LL - 1LL)

static bool
ag_int(struct ag_cur *c, long long *out, bool *over)
{
    bool neg = false;
    unsigned long long v = 0;
    const char *start;

    *over = false;
    if (c->p >= c->end || !ag_tick(c))
        return false;
    if (*c->p == '-') {
        neg = true;
        ++c->p;
    }
    start = c->p;
    if (c->p >= c->end || *c->p < '0' || *c->p > '9')
        return false;
    if (*c->p == '0') {
        ++c->p;
        if (c->p < c->end && *c->p >= '0' && *c->p <= '9')
            return false; /* leading zero */
    } else {
        while (c->p < c->end && *c->p >= '0' && *c->p <= '9') {
            unsigned d = (unsigned) (*c->p - '0');

            if (v > (0xffffffffffffffffULL - d) / 10ULL) {
                *over = true;
                return false;
            }
            v = v * 10ULL + d;
            ++c->p;
        }
    }
    if (c->p < c->end
        && (*c->p == '.' || *c->p == 'e' || *c->p == 'E' || *c->p == '+'))
        return false;
    if (c->p == start)
        return false;
    if (neg) {
        if (v > 9223372036854775808ULL) {
            *over = true;
            return false;
        }
        if (v == 9223372036854775808ULL) {
            *out = AG_I64_MIN; /* exact minimum; no negation overflow */
        } else {
            *out = -(long long) v;
        }
    } else {
        if (v > 9223372036854775807ULL) {
            *over = true;
            return false;
        }
        *out = (long long) v;
    }
    return true;
}

static bool
ag_lit(struct ag_cur *c, const char *word)
{
    size_t n = strlen(word);

    if (c->p >= c->end || !ag_tick(c))
        return false;
    if ((size_t) (c->end - c->p) < n || memcmp(c->p, word, n) != 0)
        return false;
    c->p += n;
    return true;
}

/* Skip (and validate) any well-formed JSON value. */
static bool
ag_skip_value(struct ag_cur *c);

static bool
ag_skip_object(struct ag_cur *c)
{
    unsigned keys = 0;

    if (c->depth >= AG_MAX_NESTING || !ag_tick(c))
        return false;
    ++c->depth;
    ++c->p; /* '{' */
    ag_ws(c);
    if (c->p < c->end && *c->p == '}') {
        ++c->p;
        --c->depth;
        return true;
    }
    for (;;) {
        ag_ws(c);
        if (++keys > AG_MAX_KEYS_PER_OBJECT)
            return false;
        if (!ag_string(c, (struct ag_buf *) 0))
            return false;
        ag_ws(c);
        if (c->p >= c->end || *c->p != ':')
            return false;
        ++c->p;
        ag_ws(c);
        if (!ag_skip_value(c))
            return false;
        ag_ws(c);
        if (c->p < c->end && *c->p == ',') {
            ++c->p;
            continue;
        }
        if (c->p < c->end && *c->p == '}') {
            ++c->p;
            --c->depth;
            return true;
        }
        return false;
    }
}

static bool
ag_skip_array(struct ag_cur *c)
{
    if (c->depth >= AG_MAX_NESTING || !ag_tick(c))
        return false;
    ++c->depth;
    ++c->p; /* '[' */
    ag_ws(c);
    if (c->p < c->end && *c->p == ']') {
        ++c->p;
        --c->depth;
        return true;
    }
    for (;;) {
        ag_ws(c);
        if (!ag_skip_value(c))
            return false;
        ag_ws(c);
        if (c->p < c->end && *c->p == ',') {
            ++c->p;
            continue;
        }
        if (c->p < c->end && *c->p == ']') {
            ++c->p;
            --c->depth;
            return true;
        }
        return false;
    }
}

static bool
ag_skip_value(struct ag_cur *c)
{
    ag_ws(c);
    if (c->p >= c->end)
        return false;
    switch (*c->p) {
    case '{':
        return ag_skip_object(c);
    case '[':
        return ag_skip_array(c);
    case '"':
        return ag_string(c, (struct ag_buf *) 0);
    case 't':
        return ag_lit(c, "true");
    case 'f':
        return ag_lit(c, "false");
    case 'n':
        return ag_lit(c, "null");
    default: {
        long long v;
        bool over;

        return ag_int(c, &v, &over);
    }
    }
}

/* ------------------------------------------------------------------ */
/* small helpers                                                       */
/* ------------------------------------------------------------------ */

/* Read a NUL-terminated copy of a decoded key into k, reusing k's storage so
 * that repeated calls inside a loop do not orphan earlier allocations. */
static bool
ag_key_text(struct ag_cur *c, struct ag_buf *k)
{
    if (!k->p)
        ag_init(k);
    k->len = 0;
    if (!ag_string(c, k) || !ag_reserve(k, 1))
        return false;
    k->p[k->len] = '\0';
    return true;
}

static bool
ag_menu_id_ok(const char *s)
{
    if (s[0] != 'm' || s[1] < '1' || s[1] > '9')
        return false;
    s += 2;
    while (*s) {
        if (*s < '0' || *s > '9')
            return false;
        ++s;
    }
    return true;
}

static bool
ag_is_counter(long long v, bool over)
{
    return !over && v >= 1 && (unsigned long long) v <= AG_COUNTER_MAX;
}

/* Validate one byte range as well-formed UTF-8 (rejecting lone continuation
 * bytes, truncated leads, overlong encodings, surrogates, and code points
 * above U+10FFFF). */
static bool
ag_utf8_valid(const char *s, size_t n)
{
    size_t i = 0;

    while (i < n) {
        size_t len;

        if (!ag_utf8_len((const unsigned char *) s + i, n - i, &len))
            return false;
        i += len;
    }
    return true;
}

/* Validate one public text value: well-formed UTF-8 within its byte budget.
 * An absent (NULL) value is allowed; the caller decides where absence is
 * meaningful. */
static bool
ag_text_ok(const char *s)
{
    size_t n;

    if (!s)
        return true;
    n = strlen(s);
    if (n > AG_MAX_TEXT_BYTES)
        return false;
    return ag_utf8_valid(s, n);
}

#define AG_STYLE_MASK                                                  \
    (AG_STYLE_BOLD | AG_STYLE_DIM | AG_STYLE_ITALIC | AG_STYLE_UNDERLINE \
     | AG_STYLE_BLINK | AG_STYLE_INVERSE)

/* Validate every public value of a commit BEFORE any representation is built,
 * so a malformed view fails closed with no output at all. */
static bool
ag_validate_view(const struct agent_view *v, const struct agent_need *need)
{
    size_t i;
    int j, k;

    if (!v || v->npal > AG_VIEW_MAX_PALETTE || v->nstatus > AG_VIEW_MAX_STATUS
        || v->ncond > AG_VIEW_MAX_COND || v->nwindows > AG_VIEW_MAX_WINDOWS)
        return false;
    if ((v->nmsg && !v->msg) || (v->nhist && !v->hist))
        return false;

    for (i = 0; i < v->npal; ++i) {
        unsigned char ch = v->pal[i].ch;

        /* a published cell character is one printable ASCII character */
        if (ch < 0x20 || ch > 0x7e)
            return false;
        if (v->pal[i].fg >= AG_COL_MAX || v->pal[i].frame >= AG_COL_MAX)
            return false;
        if (v->pal[i].style & ~AG_STYLE_MASK)
            return false;
    }
    for (j = 0; j < AG_MAP_ROWS; ++j)
        for (k = 0; k < AG_MAP_COLS; ++k)
            if (v->map[j][k] >= v->npal)
                return false;
    if (v->has_cursor
        && (v->cur_x < AG_MAP_MIN_X || v->cur_x > AG_MAP_MAX_X
            || v->cur_y < AG_MAP_MIN_Y || v->cur_y > AG_MAP_MAX_Y))
        return false;
    for (i = 0; i < v->nstatus; ++i) {
        if ((v->status[i].name && !ag_text_ok(v->status[i].name))
            || !ag_text_ok(v->status[i].text))
            return false;
        if (v->status[i].color >= AG_COL_MAX
            || (v->status[i].style & ~AG_STYLE_MASK))
            return false;
    }
    for (i = 0; i < v->ncond; ++i) {
        if ((v->cond[i].text && !ag_text_ok(v->cond[i].text))
            || v->cond[i].color >= AG_COL_MAX)
            return false;
    }
    for (i = 0; i < v->nmsg; ++i)
        if (!ag_text_ok(v->msg[i].text))
            return false;
    for (i = 0; i < v->nhist; ++i)
        if (!ag_text_ok(v->hist[i].text))
            return false;
    for (i = 0; i < v->nwindows; ++i) {
        if (!v->windows[i].title || !ag_text_ok(v->windows[i].title)
            || !ag_text_ok(v->windows[i].w)
            || !ag_text_ok(v->windows[i].content))
            return false;
    }
    if (need && need->kind != AG_NEED_NONE) {
        if (!ag_text_ok(need->prompt) || !ag_text_ok(need->choices)
            || !ag_text_ok(need->menu) || !ag_text_ok(need->content))
            return false;
        if (need->pages < 0 || need->pages > AG_MAX_PAGES)
            return false;
        if ((need->kind == AG_NEED_LINE || need->kind == AG_NEED_EXTCMD)
            && (need->max < 0 || need->max > AG_LINE_INPUT_MAX))
            return false;
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* action parsing                                                      */
/* ------------------------------------------------------------------ */

static enum agent_result
ag_act_fail(struct agent_action *out, enum agent_invalid_code code)
{
    out->kind = AG_ACT_NONE;
    out->code = code;
    return AG_BAD_INPUT;
}

/* Parse the "action" value.  Exactly one tagged shape is accepted. */
static enum agent_result
ag_parse_action_value(struct ag_cur *c, struct agent_action *out)
{
    bool seen_key = false, seen_text = false, seen_pos = false;
    bool seen_mod = false, seen_yn = false, seen_count = false;
    bool seen_menu = false, seen_commit = false;
    bool seen_cancel = false, seen_ack = false;
    int shapes = 0;
    struct ag_buf key;

    ag_ws(c);
    if (c->p >= c->end || *c->p != '{')
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (c->depth >= AG_MAX_NESTING)
        return ag_act_fail(out, AG_INV_SCHEMA);
    ++c->depth;
    ++c->p;
    ag_ws(c);
    if (c->p < c->end && *c->p == '}')
        return ag_act_fail(out, AG_INV_SCHEMA); /* empty action, no shape */

    ag_init(&key);
    for (;;) {
        ag_ws(c);
        if (!ag_key_text(c, &key)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        if (key.len >= AG_MAX_LINE_BYTES) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        ag_ws(c);
        if (c->p >= c->end || *c->p != ':') {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        ++c->p;
        ag_ws(c);

        if (strcmp(key.p, "key") == 0) {
            long long v;
            bool over;

            if (seen_key || !ag_int(c, &v, &over))
                goto bad;
            seen_key = true;
            ++shapes;
            if (over || v < AG_KEY_MIN || v > AG_KEY_MAX)
                goto range;
            out->key = (uint8_t) v;
        } else if (strcmp(key.p, "text") == 0) {
            struct ag_buf t;

            if (seen_text)
                goto bad;
            seen_text = true;
            ++shapes;
            ag_init(&t);
            if (!ag_string(c, &t)) {
                ag_free(&t);
                goto badfree;
            }
            if (t.len > AG_LINE_INPUT_MAX) {
                ag_free(&t);
                goto toobig;
            }
            memcpy(out->text, t.p ? t.p : "", t.len);
            out->text[t.len] = '\0';
            ag_free(&t);
        } else if (strcmp(key.p, "position") == 0) {
            long long x, y;
            bool over;

            if (seen_pos)
                goto bad;
            seen_pos = true;
            ++shapes;
            ag_ws(c);
            if (c->p >= c->end || *c->p != '['
                || c->depth >= AG_MAX_NESTING)
                goto bad;
            ++c->depth;
            ++c->p;
            ag_ws(c);
            if (!ag_int(c, &x, &over))
                goto bad;
            ag_ws(c);
            if (c->p >= c->end || *c->p != ',')
                goto bad;
            ++c->p;
            ag_ws(c);
            if (!ag_int(c, &y, &over))
                goto bad;
            ag_ws(c);
            if (c->p >= c->end || *c->p != ']')
                goto bad;
            ++c->p;
            --c->depth;
            /* column zero is the internal native sentinel only and is never
             * accepted over the wire */
            if (x < AG_MAP_MIN_X || x > AG_MAP_MAX_X || y < AG_MAP_MIN_Y
                || y > AG_MAP_MAX_Y)
                goto range;
            out->px = (int) x;
            out->py = (int) y;
        } else if (strcmp(key.p, "mod") == 0) {
            long long v;
            bool over;

            if (seen_mod || !ag_int(c, &v, &over))
                goto bad;
            seen_mod = true;
            /* mod is frozen to 0 in v1 */
            if (over || v != 0)
                goto range;
            out->pmod = 0;
        } else if (strcmp(key.p, "yn") == 0) {
            long long v;
            bool over;

            if (seen_yn)
                goto bad;
            seen_yn = true;
            ++shapes;
            if (!ag_int(c, &v, &over))
                goto bad;
            if (over || v < AG_KEY_MIN || v > AG_KEY_MAX)
                goto range;
            out->key = (uint8_t) v;
        } else if (strcmp(key.p, "count") == 0) {
            long long v;
            bool over;

            if (seen_count)
                goto bad;
            seen_count = true;
            if (!ag_int(c, &v, &over))
                goto bad;
            if (over || v < 1 || v > AG_COUNT_MAX)
                goto range;
            out->yn_count = (long) v;
            out->has_count = true;
        } else if (strcmp(key.p, "menu") == 0) {
            struct ag_buf t;

            if (seen_menu)
                goto bad;
            seen_menu = true;
            ++shapes;
            ag_init(&t);
            if (!ag_string(c, &t)) {
                ag_free(&t);
                goto badfree;
            }
            if (t.len + 1 > AG_ID_STR_MAX) {
                ag_free(&t);
                goto toobig;
            }
            memcpy(out->menu, t.p ? t.p : "", t.len);
            out->menu[t.len] = '\0';
            if (!ag_menu_id_ok(out->menu)) {
                ag_free(&t);
                goto badfree;
            }
            ag_free(&t);
        } else if (strcmp(key.p, "commit") == 0) {
            size_t n = 0;

            if (seen_commit)
                goto bad;
            seen_commit = true;
            ag_ws(c);
            if (c->p >= c->end || *c->p != '['
                || c->depth >= AG_MAX_NESTING)
                goto bad;
            ++c->depth;
            ++c->p;
            ag_ws(c);
            if (c->p < c->end && *c->p == ']') {
                ++c->p;
                --c->depth;
            } else {
                for (;;) {
                    long long r, cnt;
                    bool over;

                    ag_ws(c);
                    if (c->p >= c->end || *c->p != '['
                        || c->depth >= AG_MAX_NESTING)
                        goto bad;
                    ++c->depth;
                    ++c->p;
                    ag_ws(c);
                    if (!ag_int(c, &r, &over))
                        goto bad;
                    ag_ws(c);
                    if (c->p >= c->end || *c->p != ',')
                        goto bad;
                    ++c->p;
                    ag_ws(c);
                    if (!ag_int(c, &cnt, &over))
                        goto bad;
                    ag_ws(c);
                    if (c->p >= c->end || *c->p != ']')
                        goto bad;
                    ++c->p;
                    --c->depth;
                    if (r < 1 || r > AG_MAX_MENU_ROWS)
                        goto range;
                    if (cnt < -1 || cnt == 0 || cnt > AG_COUNT_MAX)
                        goto range;
                    if (n >= AG_MAX_MENU_ROWS || n >= out->commit_cap
                        || !out->commit)
                        goto toobig;
                    out->commit[n].r = (long) r;
                    out->commit[n].count = (long) cnt;
                    ++n;
                    ag_ws(c);
                    if (c->p < c->end && *c->p == ',') {
                        ++c->p;
                        continue;
                    }
                    if (c->p < c->end && *c->p == ']') {
                        ++c->p;
                        --c->depth;
                        break;
                    }
                    goto bad;
                }
            }
            out->ncommit = n;
        } else if (strcmp(key.p, "cancel") == 0) {
            if (seen_cancel || !ag_lit(c, "true"))
                goto bad;
            seen_cancel = true;
            ++shapes;
        } else if (strcmp(key.p, "ack") == 0) {
            if (seen_ack || !ag_lit(c, "true"))
                goto bad;
            seen_ack = true;
            ++shapes;
        } else {
            /* unknown / forbidden action field (then, group, selectall,
             * invert, bulk, keys, raw, ...) */
            goto bad;
        }
        ag_free(&key);
        ag_ws(c);
        if (c->p < c->end && *c->p == ',') {
            ++c->p;
            continue;
        }
        if (c->p < c->end && *c->p == '}') {
            ++c->p;
            --c->depth;
            break;
        }
        return ag_act_fail(out, AG_INV_SCHEMA);
    }

    if (shapes != 1)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_key)
        out->kind = AG_ACT_KEY;
    else if (seen_text)
        out->kind = AG_ACT_TEXT;
    else if (seen_pos)
        out->kind = AG_ACT_POSITION;
    else if (seen_yn)
        out->kind = AG_ACT_YN;
    else if (seen_menu)
        out->kind = AG_ACT_MENU;
    else if (seen_cancel)
        out->kind = AG_ACT_CANCEL;
    else if (seen_ack)
        out->kind = AG_ACT_ACK;

    if (seen_mod && !seen_pos)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_count && !seen_yn)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_pos && !seen_mod)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_menu && !seen_commit)
        return ag_act_fail(out, AG_INV_SCHEMA);
    return AG_OK;

bad:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_SCHEMA);
range:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_RANGE);
toobig:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_RANGE);
badfree:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_SCHEMA);
}

enum agent_result
agent_parse_action(const char *buf, size_t len, struct agent_action *out)
{
    struct ag_cur c;
    bool have_v = false, have_type = false, have_id = false;
    bool have_action = false, have_seq = false;
    struct ag_buf key;

    if (!buf || !out)
        return AG_INTERNAL;
    if (len == 0 || len > AG_MAX_ACTION_BYTES + 4096)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (memchr(buf, 0, len) != (const void *) 0)
        return ag_act_fail(out, AG_INV_SCHEMA);

    {
        /* the caller presets commit storage; preserve it across the reset */
        struct agent_commit_row *keep_commit = out->commit;
        size_t keep_cap = out->commit_cap;

        memset(out, 0, sizeof *out);
        out->commit = keep_commit;
        out->commit_cap = keep_cap;
    }
    out->kind = AG_ACT_NONE;
    out->code = AG_INV_NONE;

    c.p = buf;
    c.end = buf + len;
    c.depth = 0;
    c.tokens = 0;

    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{' || !ag_tick(&c))
        return ag_act_fail(out, AG_INV_SCHEMA);
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        return ag_act_fail(out, AG_INV_SCHEMA);

    ag_init(&key);
    for (;;) {
        ag_ws(&c);
        if (!ag_key_text(&c, &key)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        if (key.len >= AG_MAX_LINE_BYTES) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        ++c.p;
        ag_ws(&c);

        if (strcmp(key.p, "v") == 0) {
            long long v;
            bool over;

            if (have_v || !ag_int(&c, &v, &over))
                goto schema;
            have_v = true;
            if (over || v != AG_VERSION)
                goto range;
        } else if (strcmp(key.p, "type") == 0) {
            struct ag_buf t;

            if (have_type)
                goto schema;
            have_type = true;
            ag_init(&t);
            if (!ag_string(&c, &t)) {
                ag_free(&t);
                goto schemafree;
            }
            if (!(t.len == 3 && t.p && memcmp(t.p, "act", 3) == 0)) {
                ag_free(&t);
                goto schemafree;
            }
            ag_free(&t);
        } else if (strcmp(key.p, "seq") == 0) {
            long long v;
            bool over;

            if (have_seq || !ag_int(&c, &v, &over))
                goto schema;
            have_seq = true;
            if (!ag_is_counter(v, over))
                goto range;
            out->seq = (uint64_t) v;
            out->has_seq = true;
        } else if (strcmp(key.p, "id") == 0) {
            long long v;
            bool over;

            if (have_id || !ag_int(&c, &v, &over))
                goto schema;
            have_id = true;
            if (!ag_is_counter(v, over))
                goto range;
            out->id = (uint64_t) v;
        } else if (strcmp(key.p, "action") == 0) {
            enum agent_result r;

            if (have_action)
                goto schema;
            have_action = true;
            r = ag_parse_action_value(&c, out);
            if (r != AG_OK) {
                ag_free(&key);
                return r;
            }
        } else {
            /* unknown or forbidden top-level key */
            goto schema;
        }
        ag_free(&key);
        ag_ws(&c);
        if (c.p < c.end && *c.p == ',') {
            ++c.p;
            continue;
        }
        if (c.p < c.end && *c.p == '}') {
            ++c.p;
            --c.depth;
            break;
        }
        return ag_act_fail(out, AG_INV_SCHEMA);
    }

    ag_ws(&c);
    if (c.p != c.end)
        return ag_act_fail(out, AG_INV_SCHEMA); /* trailing content */
    if (!have_v || !have_type || !have_id || !have_action)
        return ag_act_fail(out, AG_INV_SCHEMA);
    return AG_OK;

schema:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_SCHEMA);
range:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_RANGE);
schemafree:
    ag_free(&key);
    return ag_act_fail(out, AG_INV_SCHEMA);
}

/* ------------------------------------------------------------------ */
/* strict transport auxiliary parsing                                  */
/* ------------------------------------------------------------------ */

/* One strict object parser for ack_seq / ack_chunk / get_page.  It enforces
 * the exact key set, rejects duplicate keys, requires v==1, bounds every
 * integer, and rejects trailing content. */
enum agent_result
agent_parse_aux(const char *buf, size_t len, struct agent_aux *out)
{
    static const char *const ack_seq_keys[] = { "v", "type", "seq" };
    static const char *const ack_chunk_keys[] = { "v", "type", "rid", "i" };
    static const char *const get_page_keys[] = { "v", "type", "id", "content",
                                                 "page" };
    const char *const *allowed = ack_seq_keys;
    unsigned nallowed = 3;
    struct ag_cur c;
    struct ag_buf key, val;
    char type[16];
    bool have_v = false, have_type = false;
    bool have_seq = false, have_rid = false, have_i = false;
    bool have_id = false, have_content = false, have_page = false;
    unsigned seen_any = 0;

    if (!buf || !out)
        return AG_INTERNAL;
    memset(out, 0, sizeof *out);
    out->kind = AG_AUX_NONE;
    out->code = AG_INV_NONE;
    if (len == 0 || len > AG_MAX_LINE_BYTES)
        goto schema;
    if (memchr(buf, 0, len) != (const void *) 0)
        goto schema;

    /* first pass: the exact key set requires knowing the type */
    {
        struct ag_cur t;
        struct ag_buf tk;
        char prev[AG_MAX_KEYS_PER_OBJECT][32];
        unsigned nprev = 0, i;
        bool found = false;

        t.p = buf;
        t.end = buf + len;
        t.depth = 0;
        t.tokens = 0;
        ag_ws(&t);
        if (t.p >= t.end || *t.p != '{' || !ag_tick(&t))
            goto schema;
        ++t.depth;
        ++t.p;
        ag_init(&tk);
        for (;;) {
            ag_ws(&t);
            if (!ag_key_text(&t, &tk)) {
                ag_free(&tk);
                goto schema;
            }
            if (tk.len >= sizeof prev[0]) {
                ag_free(&tk);
                goto schema;
            }
            for (i = 0; i < nprev; ++i)
                if (strcmp(prev[i], tk.p) == 0) {
                    ag_free(&tk);
                    goto schema; /* duplicate key */
                }
            if (nprev >= AG_MAX_KEYS_PER_OBJECT) {
                ag_free(&tk);
                goto schema;
            }
            strcpy(prev[nprev++], tk.p);
            ag_ws(&t);
            if (t.p >= t.end || *t.p != ':') {
                ag_free(&tk);
                goto schema;
            }
            ++t.p;
            ag_ws(&t);
            if (strcmp(tk.p, "type") == 0) {
                struct ag_buf tv;

                ag_init(&tv);
                if (!ag_string(&t, &tv) || tv.len + 1 > sizeof type) {
                    ag_free(&tv);
                    ag_free(&tk);
                    goto schema;
                }
                memcpy(type, tv.p ? tv.p : "", tv.len);
                type[tv.len] = '\0';
                ag_free(&tv);
                found = true;
            } else if (!ag_skip_value(&t)) {
                ag_free(&tk);
                goto schema;
            }
            ag_ws(&t);
            if (t.p < t.end && *t.p == ',') {
                ++t.p;
                continue;
            }
            if (t.p < t.end && *t.p == '}') {
                ++t.p;
                break;
            }
            ag_free(&tk);
            goto schema;
        }
        ag_free(&tk);
        ag_ws(&t);
        if (t.p != t.end || !found)
            goto schema;
    }

    if (strcmp(type, "ack_seq") == 0) {
        out->kind = AG_AUX_ACK_SEQ;
        allowed = ack_seq_keys;
        nallowed = 3;
    } else if (strcmp(type, "ack_chunk") == 0) {
        out->kind = AG_AUX_ACK_CHUNK;
        allowed = ack_chunk_keys;
        nallowed = 4;
    } else if (strcmp(type, "get_page") == 0) {
        out->kind = AG_AUX_GET_PAGE;
        allowed = get_page_keys;
        nallowed = 5;
    } else {
        goto schema;
    }

    c.p = buf;
    c.end = buf + len;
    c.depth = 0;
    c.tokens = 0;
    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{' || !ag_tick(&c))
        goto schema;
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        goto schema;

    ag_init(&key);
    ag_init(&val);
    for (;;) {
        unsigned i;
        bool matched = false;

        ag_ws(&c);
        if (!ag_key_text(&c, &key)) {
            ag_free(&key);
            ag_free(&val);
            goto schema;
        }
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&key);
            ag_free(&val);
            goto schema;
        }
        for (i = 0; i < nallowed; ++i) {
            if (strcmp(key.p, allowed[i]) == 0) {
                if (seen_any & (1u << i)) {
                    ag_free(&key);
                    ag_free(&val);
                    goto schema; /* duplicate key */
                }
                seen_any |= (1u << i);
                matched = true;
                break;
            }
        }
        if (!matched) {
            ag_free(&key);
            ag_free(&val);
            goto schema; /* additional property */
        }
        ++c.p;
        ag_ws(&c);

        if (strcmp(key.p, "v") == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_v = true;
            if (over || v != AG_VERSION) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
        } else if (strcmp(key.p, "type") == 0) {
            struct ag_buf t;

            ag_init(&t);
            if (!ag_string(&c, &t)) {
                ag_free(&t);
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_type = true;
            ag_free(&t);
        } else if (strcmp(key.p, "seq") == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_seq = true;
            if (!ag_is_counter(v, over)) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            out->seq = (uint64_t) v;
        } else if (strcmp(key.p, "rid") == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_rid = true;
            if (!ag_is_counter(v, over)) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            out->rid = (uint64_t) v;
        } else if (strcmp(key.p, "i") == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_i = true;
            if (over || v < 0) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            out->i = (long) v;
        } else if (strcmp(key.p, "id") == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_id = true;
            if (!ag_is_counter(v, over)) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            out->id = (uint64_t) v;
        } else if (strcmp(key.p, "content") == 0) {
            struct ag_buf t;

            ag_init(&t);
            if (!ag_string(&c, &t)) {
                ag_free(&t);
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            if (t.len + 1 > sizeof out->content) {
                ag_free(&t);
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            memcpy(out->content, t.p ? t.p : "", t.len);
            out->content[t.len] = '\0';
            if (t.len < 2 || out->content[0] != 'c'
                || out->content[1] < '1' || out->content[1] > '9') {
                ag_free(&t);
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_content = true;
            ag_free(&t);
        } else { /* page */
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over)) {
                ag_free(&key);
                ag_free(&val);
                goto schema;
            }
            have_page = true;
            if (over || v < 0 || v > AG_MAX_PAGE_INDEX) {
                ag_free(&key);
                ag_free(&val);
                goto range;
            }
            out->page = (long) v;
        }

        ag_free(&key);
        ag_free(&val);
        ag_ws(&c);
        if (c.p < c.end && *c.p == ',') {
            ++c.p;
            continue;
        }
        if (c.p < c.end && *c.p == '}') {
            ++c.p;
            --c.depth;
            break;
        }
        goto schema;
    }

    ag_ws(&c);
    if (c.p != c.end)
        goto schema;
    if (!have_v || !have_type)
        goto schema;
    if (out->kind == AG_AUX_ACK_SEQ && !have_seq)
        goto schema;
    if (out->kind == AG_AUX_ACK_CHUNK && (!have_rid || !have_i))
        goto schema;
    if (out->kind == AG_AUX_GET_PAGE
        && (!have_id || !have_content || !have_page))
        goto schema;
    return AG_OK;

schema:
    out->kind = AG_AUX_NONE;
    out->code = AG_INV_SCHEMA;
    return AG_BAD_INPUT;
range:
    out->kind = AG_AUX_NONE;
    out->code = AG_INV_RANGE;
    return AG_BAD_INPUT;
}

/* ------------------------------------------------------------------ */
/* parts: one enumeration drives both the plain object and the chunks  */
/* ------------------------------------------------------------------ */

enum ag_part_kind {
    AG_P_HEADER = 0,
    AG_P_STATUS,
    AG_P_COND,
    AG_P_PAL,
    AG_P_MAP,
    AG_P_MSG,
    AG_P_HIST,
    AG_P_WIN,
    AG_P_CUR,
    AG_P_NEED
};

/* long-text field identity */
enum { AG_TF_NONE = 0, AG_TF_TEXT = 1, AG_TF_TITLE = 2 };

struct ag_part {
    int kind;
    const char *key;   /* header key or status member name */
    size_t off, len;   /* value JSON (full text) */
    size_t head_off, head_len; /* element JSON with an empty text field */
    long ref;          /* event id for msg/hist */
    int field;         /* AG_TF_* */
    const char *raw;   /* raw text bytes (not JSON-escaped) */
    size_t raw_len;
};

struct ag_parts {
    struct ag_part *v;
    size_t n;
    size_t cap;
    struct ag_buf arena;
};

static void
ag_parts_init(struct ag_parts *ps)
{
    ps->v = (struct ag_part *) 0;
    ps->n = 0;
    ps->cap = 0;
    ag_init(&ps->arena);
}

static void
ag_parts_free(struct ag_parts *ps)
{
    if (ps->v)
        free(ps->v);
    ag_free(&ps->arena);
}

/* Append the len bytes at val (which may be NULL when len is 0) as the value
 * of one part, returning -1 on failure. */
static long
ag_parts_add(struct ag_parts *ps, int kind, const char *key, const char *val,
             size_t vlen)
{
    struct ag_part *p;
    size_t off;

    if (ps->n == ps->cap) {
        size_t ncap = ps->cap ? ps->cap * 2 : 64;
        struct ag_part *nv = (struct ag_part *) realloc(ps->v,
                                                        ncap * sizeof *nv);

        if (!nv)
            return -1;
        ps->v = nv;
        ps->cap = ncap;
    }
    p = &ps->v[ps->n];
    memset(p, 0, sizeof *p);
    p->kind = kind;
    p->key = key;
    p->off = ps->arena.len;
    p->len = vlen;
    off = p->off;
    if (vlen && val && !ag_put(&ps->arena, val, vlen))
        return -1;
    ++ps->n;
    return (long) off;
}

static void
ag_part_set_head(struct ag_part *p, size_t off, size_t len)
{
    p->head_off = off;
    p->head_len = len;
}

static const char *
ag_arena_at(const struct ag_parts *ps, size_t off)
{
    return ps->arena.p + off;
}

/* ------------------------------------------------------------------ */
/* plain (unlined) observation rendering                                */
/* ------------------------------------------------------------------ */

static bool
ag_emit_obj_members(struct ag_buf *o, const struct ag_parts *ps, int kind,
                    const char *objkey, bool members)
{
    size_t i;
    bool first = true;

    if (!ag_putc(o, '"') || !ag_puts(o, objkey)
        || !ag_puts(o, members ? "\":{" : "\":["))
        return false;
    for (i = 0; i < ps->n; ++i) {
        const struct ag_part *p = &ps->v[i];

        if (p->kind != kind)
            continue;
        if (!first && !ag_putc(o, ','))
            return false;
        first = false;
        if (members) {
            if (!ag_put_jstr(o, p->key) || !ag_putc(o, ':'))
                return false;
        }
        if (!ag_put(o, ag_arena_at(ps, p->off), p->len))
            return false;
    }
    return ag_putc(o, members ? '}' : ']');
}

static bool
ag_emit_fields(struct ag_buf *o, const struct ag_parts *ps, uint64_t d,
               uint64_t seq)
{
    size_t i;

    if (!ag_puts(o, "{\"v\":") || !ag_put_u64(o, AG_VERSION)
        || !ag_puts(o, ",\"ch\":\"player\",\"type\":\"obs\",\"d\":")
        || !ag_put_u64(o, d) || !ag_puts(o, ",\"seq\":")
        || !ag_put_u64(o, seq) || !ag_puts(o, ",\"base\":null,"))
        return false;

    /* every full observation carries the complete fixed field set, including
     * empty collections for empty ones */
    if (!ag_emit_obj_members(o, ps, AG_P_STATUS, "s", true)
        || !ag_putc(o, ',')
        || !ag_emit_obj_members(o, ps, AG_P_COND, "cond", false)
        || !ag_putc(o, ',')
        || !ag_emit_obj_members(o, ps, AG_P_PAL, "pal", false)
        || !ag_putc(o, ',')
        || !ag_emit_obj_members(o, ps, AG_P_MAP, "map", false)
        || !ag_putc(o, ','))
        return false;

    for (i = 0; i < ps->n; ++i) {
        const struct ag_part *p = &ps->v[i];

        if (p->kind == AG_P_CUR) {
            if (!ag_puts(o, "\"cur\":")
                || !ag_put(o, ag_arena_at(ps, p->off), p->len))
                return false;
            break;
        }
    }
    if (i == ps->n)
        return false;
    if (!ag_putc(o, ','))
        return false;

    if (!ag_emit_obj_members(o, ps, AG_P_MSG, "msg", false)
        || !ag_putc(o, ',')
        || !ag_emit_obj_members(o, ps, AG_P_HIST, "hist", false)
        || !ag_putc(o, ',')
        || !ag_emit_obj_members(o, ps, AG_P_WIN, "windows", false)
        || !ag_putc(o, ','))
        return false;

    for (i = 0; i < ps->n; ++i) {
        const struct ag_part *p = &ps->v[i];

        if (p->kind == AG_P_NEED) {
            if (!ag_puts(o, "\"need\":")
                || !ag_put(o, ag_arena_at(ps, p->off), p->len))
                return false;
            break;
        }
    }
    if (i == ps->n)
        return false;
    return ag_putc(o, '}');
}

/* ------------------------------------------------------------------ */
/* part construction                                                   */
/* ------------------------------------------------------------------ */

/* Build the msg/hist/win element JSON twice: once with the real text and once
 * with an empty text field, so the chunk path can splice long text out. */
static bool
ag_add_text_element(struct ag_parts *ps, int kind, long ref, int field,
                    const char *raw, size_t raw_len,
                    const struct ag_buf *full, const struct ag_buf *head)
{
    long o = ag_parts_add(ps, kind, (const char *) 0,
                          full->p ? full->p : "", full->len);
    struct ag_part *p;

    if (o < 0)
        return false;
    p = &ps->v[ps->n - 1];
    p->ref = ref;
    p->field = field;
    p->raw = raw;
    p->raw_len = raw_len;
    if (raw_len) {
        size_t off = ps->arena.len;

        if (!ag_put(&ps->arena, head->p ? head->p : "", head->len))
            return false;
        ag_part_set_head(p, off, head->len);
    }
    return true;
}

#define AG_EMIT_SIMPLE(kind, key)                                            \
    do {                                                                     \
        if (ok)                                                              \
            ok = (ag_parts_add(ps, (kind), (key), b.p ? b.p : "", b.len)     \
                  >= 0);                                                     \
        b.len = 0;                                                           \
    } while (0)

static bool
ag_build_parts(struct ag_parts *ps, const struct agent_view *v,
               const struct agent_need *need, uint64_t seq)
{
    struct ag_buf b, head;
    size_t i, j;
    bool ok = true;
    const char *mname;

    ag_init(&b);
    ag_init(&head);

    /* header scalars: exactly v, ch, type, seq, base.  The logical record's
     * own delivery counter is carried by the record (or by rid when chunked),
     * never as a header part. */
    ag_put_u64(&b, AG_VERSION);
    AG_EMIT_SIMPLE(AG_P_HEADER, "v");
    ag_put_jstr(&b, "player");
    AG_EMIT_SIMPLE(AG_P_HEADER, "ch");
    ag_put_jstr(&b, "obs");
    AG_EMIT_SIMPLE(AG_P_HEADER, "type");
    ag_put_u64(&b, seq);
    AG_EMIT_SIMPLE(AG_P_HEADER, "seq");
    ag_puts(&b, "null");
    AG_EMIT_SIMPLE(AG_P_HEADER, "base");

    /* status members */
    for (i = 0; i < v->nstatus && ok; ++i) {
        const struct agent_status *s = &v->status[i];

        if (!s->name)
            continue;
        ag_puts(&b, "{\"text\":");
        ag_put_jstr(&b, s->text ? s->text : "");
        ag_puts(&b, ",\"color\":");
        ag_put_color(&b, s->color);
        ag_puts(&b, ",\"style\":");
        ag_put_u64(&b, s->style);
        ag_putc(&b, '}');
        AG_EMIT_SIMPLE(AG_P_STATUS, s->name);
    }
    for (i = 0; i < v->ncond && ok; ++i) {
        const struct agent_cond *cd = &v->cond[i];

        ag_puts(&b, "{\"text\":");
        ag_put_jstr(&b, cd->text ? cd->text : "");
        ag_puts(&b, ",\"color\":");
        ag_put_color(&b, cd->color);
        ag_puts(&b, ",\"style\":");
        ag_put_u64(&b, cd->style);
        ag_putc(&b, '}');
        AG_EMIT_SIMPLE(AG_P_COND, (const char *) 0);
    }
    for (i = 0; i < v->npal && ok; ++i) {
        ag_putc(&b, '[');
        ag_put_u64(&b, i);
        ag_puts(&b, ",\"");
        ag_putc(&b, (char) v->pal[i].ch);
        ag_puts(&b, "\",");
        ag_put_color(&b, v->pal[i].fg);
        ag_putc(&b, ',');
        ag_put_u64(&b, v->pal[i].style);
        ag_putc(&b, ',');
        ag_put_color(&b, v->pal[i].frame);
        ag_putc(&b, ']');
        AG_EMIT_SIMPLE(AG_P_PAL, (const char *) 0);
    }
    /* Sparse blank omission: an unpainted cell is the declared blank
     * appearance, so palette id 0 cells are not enumerated.  The loop walks
     * the native array by native x and skips native column zero, which is
     * unused; the stored coordinate is emitted unchanged. */
    for (j = AG_MAP_MIN_Y; j <= AG_MAP_MAX_Y && ok; ++j) {
        for (i = AG_MAP_MIN_X; i <= AG_MAP_MAX_X && ok; ++i) {
            uint16_t id = v->map[j][i];

            if (id == 0)
                continue;
            ag_putc(&b, '[');
            ag_put_u64(&b, i);
            ag_putc(&b, ',');
            ag_put_u64(&b, j);
            ag_putc(&b, ',');
            ag_put_u64(&b, id);
            ag_putc(&b, ']');
            AG_EMIT_SIMPLE(AG_P_MAP, (const char *) 0);
        }
    }
    if (v->has_cursor) {
        /* the cursor follows the same convention: no offset is applied */
        ag_putc(&b, '[');
        ag_put_u64(&b, (uint64_t) v->cur_x);
        ag_putc(&b, ',');
        ag_put_u64(&b, (uint64_t) v->cur_y);
        ag_putc(&b, ']');
    } else {
        ag_puts(&b, "null");
    }
    AG_EMIT_SIMPLE(AG_P_CUR, (const char *) 0);

    for (i = 0; i < v->nmsg && ok; ++i) {
        if (!ag_is_counter((long long) v->msg[i].e, false)) {
            ok = false;
            break;
        }
        ag_puts(&b, "{\"e\":");
        ag_put_u64(&b, v->msg[i].e);
        ag_puts(&b, ",\"text\":");
        ag_put_jstr(&b, v->msg[i].text ? v->msg[i].text : "");
        ag_puts(&b, ",\"style\":");
        ag_put_u64(&b, v->msg[i].style);
        ag_putc(&b, '}');
        ag_puts(&head, "{\"e\":");
        ag_put_u64(&head, v->msg[i].e);
        ag_puts(&head, ",\"text\":\"\",\"style\":");
        ag_put_u64(&head, v->msg[i].style);
        ag_putc(&head, '}');
        ok = ag_add_text_element(ps, AG_P_MSG, (long) v->msg[i].e, AG_TF_TEXT,
                                 v->msg[i].text,
                                 v->msg[i].text ? strlen(v->msg[i].text) : 0,
                                 &b, &head);
        b.len = 0;
        head.len = 0;
    }
    for (i = 0; i < v->nhist && ok; ++i) {
        if (!ag_is_counter((long long) v->hist[i].e, false)) {
            ok = false;
            break;
        }
        ag_puts(&b, "{\"e\":");
        ag_put_u64(&b, v->hist[i].e);
        ag_puts(&b, ",\"text\":");
        ag_put_jstr(&b, v->hist[i].text ? v->hist[i].text : "");
        ag_puts(&b, ",\"style\":");
        ag_put_u64(&b, v->hist[i].style);
        ag_putc(&b, '}');
        ag_puts(&head, "{\"e\":");
        ag_put_u64(&head, v->hist[i].e);
        ag_puts(&head, ",\"text\":\"\",\"style\":");
        ag_put_u64(&head, v->hist[i].style);
        ag_putc(&head, '}');
        ok = ag_add_text_element(ps, AG_P_HIST, (long) v->hist[i].e,
                                 AG_TF_TEXT, v->hist[i].text,
                                 v->hist[i].text
                                     ? strlen(v->hist[i].text) : 0,
                                 &b, &head);
        b.len = 0;
        head.len = 0;
    }
    for (i = 0; i < v->nwindows && ok; ++i) {
        const struct agent_window *w = &v->windows[i];
        const char *wid = w->w ? w->w : "w1";

        if (!w->title)
            ok = false;
        if (!ok)
            break;
        ag_puts(&b, "{\"w\":");
        ag_put_jstr(&b, wid);
        ag_puts(&b, ",\"kind\":");
        ag_put_jstr(&b, w->kind ? "menu" : "text");
        ag_puts(&b, ",\"title\":");
        ag_put_jstr(&b, w->title);
        if (w->kind) {
            ag_puts(&b, ",\"mode\":");
            mname = agent_menu_mode_name((enum agent_menu_mode) w->mode);
            ag_put_jstr(&b, mname ? mname : "none");
        }
        ag_puts(&b, ",\"content\":");
        ag_put_jstr(&b, w->content ? w->content : "c1");
        ag_puts(&b, ",\"pages\":");
        ag_put_u64(&b, (uint64_t) (w->pages < 0 ? 0 : w->pages));
        ag_putc(&b, '}');
        ag_puts(&head, "{\"w\":");
        ag_put_jstr(&head, wid);
        ag_puts(&head, ",\"kind\":");
        ag_put_jstr(&head, w->kind ? "menu" : "text");
        ag_puts(&head, ",\"title\":\"\"");
        if (w->kind) {
            ag_puts(&head, ",\"mode\":");
            mname = agent_menu_mode_name((enum agent_menu_mode) w->mode);
            ag_put_jstr(&head, mname ? mname : "none");
        }
        ag_puts(&head, ",\"content\":");
        ag_put_jstr(&head, w->content ? w->content : "c1");
        ag_puts(&head, ",\"pages\":");
        ag_put_u64(&head, (uint64_t) (w->pages < 0 ? 0 : w->pages));
        ag_putc(&head, '}');
        ok = ag_add_text_element(ps, AG_P_WIN, 0, AG_TF_TITLE, w->title,
                                 strlen(w->title), &b, &head);
        b.len = 0;
        head.len = 0;
        /* the window element needs its id string for the t-part path */
        if (ok) {
            struct ag_part *p = &ps->v[ps->n - 1];

            p->key = wid;
        }
    }

    /* need */
    if (!need || need->kind == AG_NEED_NONE) {
        ag_puts(&b, "null");
    } else {
        if (!ag_is_counter((long long) need->id, false)) {
            ok = false;
            goto out;
        }
        ag_puts(&b, "{\"id\":");
        ag_put_u64(&b, need->id);
        ag_puts(&b, ",\"kind\":");
        switch (need->kind) {
        case AG_NEED_COMMAND:
            ag_put_jstr(&b, "command");
            break;
        case AG_NEED_KEY:
            ag_put_jstr(&b, "key");
            break;
        case AG_NEED_DIRECTION:
            ag_put_jstr(&b, "direction");
            break;
        case AG_NEED_POSITION:
            ag_put_jstr(&b, "position");
            break;
        case AG_NEED_YN:
            ag_put_jstr(&b, "yn");
            break;
        case AG_NEED_LINE:
            ag_put_jstr(&b, "line");
            break;
        case AG_NEED_EXTCMD:
            ag_put_jstr(&b, "extcmd");
            break;
        case AG_NEED_MENU:
            ag_put_jstr(&b, "menu");
            break;
        default:
            ag_put_jstr(&b, "ack");
            break;
        }
        if (need->prompt) {
            ag_puts(&b, ",\"prompt\":");
            ag_put_jstr(&b, need->prompt);
        }
        if (need->kind == AG_NEED_POSITION) {
            ag_puts(&b, ",\"x0\":");
            ag_put_i64(&b, need->x0);
            ag_puts(&b, ",\"y0\":");
            ag_put_i64(&b, need->y0);
            ag_puts(&b, ",\"x1\":");
            ag_put_i64(&b, need->x1);
            ag_puts(&b, ",\"y1\":");
            ag_put_i64(&b, need->y1);
        }
        if (need->kind == AG_NEED_YN) {
            ag_puts(&b, ",\"choices\":");
            if (need->choices)
                ag_put_jstr(&b, need->choices);
            else
                ag_puts(&b, "null");
            ag_puts(&b, ",\"default\":");
            if (need->def)
                ag_put_i64(&b, need->def);
            else
                ag_puts(&b, "null");
            ag_puts(&b, ",\"numeric\":");
            ag_puts(&b, need->numeric ? "true" : "false");
        }
        if (need->kind == AG_NEED_LINE || need->kind == AG_NEED_EXTCMD) {
            ag_puts(&b, ",\"max\":");
            ag_put_i64(&b, need->max);
        }
        if (need->kind == AG_NEED_MENU) {
            ag_puts(&b, ",\"menu\":");
            ag_put_jstr(&b, need->menu ? need->menu : "m1");
            ag_puts(&b, ",\"mode\":");
            mname = agent_menu_mode_name((enum agent_menu_mode) need->mode);
            ag_put_jstr(&b, mname ? mname : "none");
            ag_puts(&b, ",\"content\":");
            ag_put_jstr(&b, need->content ? need->content : "c1");
            ag_puts(&b, ",\"pages\":");
            ag_put_u64(&b, (uint64_t) (need->pages < 0 ? 0 : need->pages));
        }
        if (need->kind == AG_NEED_ACK) {
            ag_puts(&b, ",\"content\":");
            ag_put_jstr(&b, need->content ? need->content : "c1");
            ag_puts(&b, ",\"pages\":");
            ag_put_u64(&b, (uint64_t) (need->pages < 0 ? 0 : need->pages));
        }
        ag_putc(&b, '}');
    }
    AG_EMIT_SIMPLE(AG_P_NEED, (const char *) 0);

out:
    ag_free(&b);
    ag_free(&head);
    return ok && !ps->arena.ovf;
}

#undef AG_EMIT_SIMPLE

/* ------------------------------------------------------------------ */
/* chunk fragments                                                     */
/* ------------------------------------------------------------------ */

struct ag_frag {
    size_t off;   /* offset into the caller's scratch buffer */
    size_t len;
};

/* Trim n so that the slice never ends inside a UTF-8 sequence. */
static size_t
ag_utf8_floor(const char *s, size_t n)
{
    while (n > 0 && ((unsigned char) s[n] & 0xc0) == 0x80)
        --n;
    return n;
}

/* Render one part's chunk fragment(s) into out, appending to the fragment
 * list.  Returns false on failure. */
static bool
ag_render_part_frags(const struct ag_parts *ps, const struct ag_part *p,
                     size_t budget, struct ag_buf *scratch,
                     struct ag_frag *frags, size_t *nfrags, size_t maxfrags)
{
    const char *tag = "h";
    struct ag_buf one;
    struct ag_buf t;

    switch (p->kind) {
    case AG_P_HEADER:
        tag = "h";
        break;
    case AG_P_STATUS:
        tag = "s";
        break;
    case AG_P_COND:
        tag = "cond";
        break;
    case AG_P_PAL:
        tag = "pal";
        break;
    case AG_P_MAP:
        tag = "map";
        break;
    case AG_P_MSG:
        tag = "msg";
        break;
    case AG_P_HIST:
        tag = "hist";
        break;
    case AG_P_WIN:
        tag = "win";
        break;
    case AG_P_CUR:
        tag = "cur";
        break;
    default:
        tag = "need";
        break;
    }

    ag_init(&one);
    if (!ag_puts(&one, "{\"p\":") || !ag_put_jstr(&one, tag))
        goto fail;
    if (p->key && (p->kind == AG_P_HEADER || p->kind == AG_P_STATUS)) {
        if (!ag_puts(&one, ",\"k\":") || !ag_put_jstr(&one, p->key))
            goto fail;
    }
    if (!ag_puts(&one, ",\"val\":")
        || !ag_put(&one, ag_arena_at(ps, p->off), p->len)
        || !ag_putc(&one, '}'))
        goto fail;

    if (one.len <= budget) {
        size_t off = scratch->len;

        if (*nfrags >= maxfrags || !ag_put(scratch, one.p, one.len))
            goto fail;
        frags[*nfrags].off = off;
        frags[*nfrags].len = one.len;
        ++*nfrags;
        ag_free(&one);
        return true;
    }
    ag_free(&one);

    /* a long text value is spliced into t parts */
    if (!p->raw || p->field == AG_TF_NONE)
        return false;
    if (p->raw_len > AG_MAX_TEXT_BYTES)
        return false;

    {
        size_t off = scratch->len;

        if (*nfrags >= maxfrags
            || !ag_put(scratch, ag_arena_at(ps, p->head_off), p->head_len))
            return false;
        frags[*nfrags].off = off;
        frags[*nfrags].len = p->head_len;
        ++*nfrags;
    }

    {
        size_t pos = 0;
        size_t room = budget > AG_T_MARGIN ? budget - AG_T_MARGIN : 0;

        if (room == 0)
            return false;
        while (pos < p->raw_len) {
            size_t take = p->raw_len - pos;
            size_t off = scratch->len;
            bool fit = false;

            if (take > room)
                take = room;
            take = ag_utf8_floor(p->raw + pos, take);
            if (take == 0)
                return false;
            ag_init(&t);
            for (;;) {
                t.len = 0;
                if (!ag_puts(&t, "{\"p\":\"t\",\"k\":")
                    || !ag_put_jstr(&t, tag))
                    goto tfail;
                if (p->kind == AG_P_WIN) {
                    if (!ag_puts(&t, ",\"w\":")
                        || !ag_put_jstr(&t, p->key ? p->key : "w1"))
                        goto tfail;
                } else {
                    if (!ag_puts(&t, ",\"e\":") || !ag_put_i64(&t, p->ref))
                        goto tfail;
                }
                if (!ag_puts(&t, ",\"f\":")
                    || !ag_put_jstr(&t, p->field == AG_TF_TITLE ? "title"
                                                                : "text"))
                    goto tfail;
                if (!ag_puts(&t, ",\"offset\":") || !ag_put_u64(&t, pos))
                    goto tfail;
                if (!ag_puts(&t, ",\"text\":")
                    || !ag_put_jstr_n(&t, p->raw + pos, take))
                    goto tfail;
                if (!ag_puts(&t, ",\"last\":")
                    || !ag_puts(&t, (pos + take == p->raw_len) ? "true"
                                                               : "false")
                    || !ag_putc(&t, '}'))
                    goto tfail;
                if (t.len <= budget) {
                    fit = true;
                    break;
                }
                if (take <= 1)
                    goto tfail;
                take = ag_utf8_floor(p->raw + pos, take / 2);
                if (take == 0)
                    goto tfail;
            }
            if (!fit)
                goto tfail;
            if (*nfrags >= maxfrags
                || !ag_put(scratch, t.p, t.len)) {
                ag_free(&t);
                return false;
            }
            frags[*nfrags].off = off;
            frags[*nfrags].len = t.len;
            ++*nfrags;
            ag_free(&t);
            pos += take;
        }
    }
    return true;

tfail:
    ag_free(&t);
    return false;
fail:
    ag_free(&one);
    return false;
}

size_t
agent_chunk_plan(const size_t part_len[], size_t nparts, size_t budget,
                 size_t chunk_of[], size_t max_chunks)
{
    size_t i, chunks = 0;
    size_t cur = 0;

    if (!part_len || !chunk_of || budget == 0)
        return 0;
    for (i = 0; i < nparts; ++i) {
        size_t need;

        if (part_len[i] > budget)
            return 0; /* a single part never fits: caller must split text */
        need = part_len[i] + (cur ? 1 : 0);
        if (cur + need > budget) {
            ++chunks;
            if (chunks > max_chunks)
                return 0;
            cur = 0;
            need = part_len[i];
        }
        cur += need;
        chunk_of[i] = chunks; /* zero-based chunk index */
    }
    if (nparts == 0) {
        if (max_chunks < 1)
            return 0;
        return 1;
    }
    if (chunks + 1 > max_chunks)
        return 0;
    return chunks + 1;
}

/* ------------------------------------------------------------------ */
/* session plumbing                                                    */
/* ------------------------------------------------------------------ */

static enum agent_result
ag_write_all(struct agent_session *s, const char *buf, size_t len)
{
    size_t off = 0;

    while (off < len) {
        long n = s->write(s->io, buf + off, len - off);

        if (n <= 0)
            return AG_IO;
        off += (size_t) n;
    }
    return AG_OK;
}

static bool
ag_reply_append(struct agent_session *s, const char *buf, size_t len)
{
    if (s->reply_len + len > AG_MAX_RETAINED_BYTES)
        return false;
    if (s->reply_len + len > s->reply_cap) {
        size_t ncap = s->reply_cap ? s->reply_cap : 4096;
        char *np;

        while (ncap < s->reply_len + len)
            ncap *= 2;
        if (ncap > AG_MAX_RETAINED_BYTES)
            ncap = AG_MAX_RETAINED_BYTES;
        np = (char *) realloc(s->reply, ncap);
        if (!np)
            return false;
        s->reply = np;
        s->reply_cap = ncap;
    }
    memcpy(s->reply + s->reply_len, buf, len);
    s->reply_len += len;
    return true;
}

/* Write one physical record: bytes plus LF, retaining it for retry and, when
 * collecting, appending it to the retained logical response stream. */
static enum agent_result
ag_emit(struct agent_session *s, const char *buf, size_t len, uint64_t d,
        bool collect)
{
    enum agent_result r;

    if (len + 1 > sizeof s->last_line)
        return AG_LIMIT;
    if (collect && !ag_reply_append(s, buf, len))
        return AG_LIMIT;
    if (collect && !ag_reply_append(s, "\n", 1))
        return AG_LIMIT;
    r = ag_write_all(s, buf, len);
    if (r != AG_OK)
        return r;
    r = ag_write_all(s, "\n", 1);
    if (r != AG_OK)
        return r;
    memcpy(s->last_line, buf, len);
    s->last_line[len] = '\n';
    s->last_line_len = len + 1;
    s->last_line_d = d;
    return AG_OK;
}

static uint32_t
ag_hash(const char *buf, size_t len)
{
    uint32_t h = 2166136261u;
    size_t i;

    for (i = 0; i < len; ++i) {
        h ^= (unsigned char) buf[i];
        h *= 16777619u;
    }
    return h;
}

/* Advance one output counter, refusing to wrap. */
static bool
ag_bump(struct agent_session *s, uint64_t *v)
{
    (void) s;
    if (*v >= AG_COUNTER_MAX)
        return false;
    ++*v;
    return true;
}

void
agent_session_init(struct agent_session *s, agent_read_fn rd,
                   agent_write_fn wr, void *io)
{
    memset(s, 0, sizeof *s);
    s->read = rd;
    s->write = wr;
    s->io = io;
    s->limit_line = AG_MAX_LINE_BYTES;
}

void
agent_session_free(struct agent_session *s)
{
    if (!s)
        return;
    if (s->reply)
        free(s->reply);
    if (s->action_text)
        free(s->action_text);
    s->reply = (char *) 0;
    s->reply_len = s->reply_cap = 0;
    s->action_text = (char *) 0;
    s->action_len = s->action_cap = 0;
}

static bool
ag_action_store(struct agent_session *s, const char *buf, size_t len)
{
    if (len > AG_MAX_ACTION_BYTES)
        return false;
    if (len > s->action_cap) {
        char *np = (char *) realloc(s->action_text, len ? len : 1);

        if (!np)
            return false;
        s->action_text = np;
        s->action_cap = len;
    }
    memcpy(s->action_text, buf, len);
    s->action_len = len;
    return true;
}

enum agent_result
agent_write_hello(struct agent_session *s)
{
    struct ag_buf o;
    enum agent_result r;
    uint64_t d;

    if (!s)
        return AG_INTERNAL;
    if (s->hello_sent || s->closed)
        return AG_BAD_INPUT; /* hello is emitted at most once */
    if (!ag_bump(s, &s->next_delivery))
        return AG_LIMIT;
    d = s->next_delivery;

    ag_init(&o);
    ag_puts(&o, "{\"v\":1,\"ch\":\"control\",\"type\":\"hello\",\"d\":");
    ag_put_u64(&o, d);
    ag_puts(&o, ",\"profile\":\"normal-ascii-color-v1\"");
    ag_puts(&o, ",\"policy\":\"llm-final-v1\"");
    ag_puts(&o, ",\"caps\":[\"snapshot\",\"menu\",\"paging\"]");
    ag_puts(&o, ",\"coord\":\"engine-map\",\"size\":[80,21]");
    ag_puts(&o, ",\"x0\":1,\"y0\":0");
    ag_puts(&o, ",\"limits\":{\"line\":65536,\"page_bytes\":16384");
    ag_puts(&o, ",\"page_rows\":128,\"count\":2147483647}}");
    if (o.ovf) {
        ag_free(&o);
        return AG_LIMIT;
    }
    r = ag_emit(s, o.p, o.len, d, false);
    ag_free(&o);
    if (r == AG_OK)
        s->hello_sent = true;
    return r;
}

enum agent_result
agent_write_closed(struct agent_session *s)
{
    static const char closed[] = "{\"v\":1,\"ch\":\"control\","
                                 "\"type\":\"closed\"}";
    enum agent_result r;

    if (!s)
        return AG_INTERNAL;
    if (s->closed_sent)
        return AG_BAD_INPUT; /* terminal closure is emitted at most once */
    /* exactly the bare closure: no delivery counter, no reason, no id */
    r = ag_write_all(s, closed, sizeof closed - 1);
    if (r == AG_OK)
        r = ag_write_all(s, "\n", 1);
    if (r == AG_OK) {
        s->closed_sent = true;
        s->closed = true;
    }
    return r;
}

enum agent_result
agent_write_invalid(struct agent_session *s, enum agent_invalid_code code)
{
    static const char *const names[] = { "schema", "stale", "kind", "range",
                                         "incomplete" };
    struct ag_buf o;
    enum agent_result r;
    uint64_t d;
    const char *nm = "schema";

    if (!s)
        return AG_INTERNAL;
    if (code >= AG_INV_SCHEMA && code <= AG_INV_INCOMPLETE)
        nm = names[code - 1];
    if (!ag_bump(s, &s->next_delivery))
        return AG_LIMIT;
    d = s->next_delivery;
    ag_init(&o);
    ag_puts(&o, "{\"v\":1,\"ch\":\"control\",\"type\":\"invalid\",\"d\":");
    ag_put_u64(&o, d);
    ag_puts(&o, ",\"code\":");
    ag_put_jstr(&o, nm);
    ag_putc(&o, '}');
    if (o.ovf) {
        ag_free(&o);
        return AG_LIMIT;
    }
    r = ag_emit(s, o.p, o.len, d, false);
    ag_free(&o);
    if (r == AG_OK) {
        ++s->invalids;
        s->last_code = code;
    }
    return r;
}

enum agent_result
agent_retry_last(struct agent_session *s)
{
    if (!s || s->last_line_len == 0)
        return AG_INTERNAL;
    /* identical bytes and the original delivery counter */
    return ag_write_all(s, s->last_line, s->last_line_len);
}

/* Start (or clear) a chunk delivery stream: the acknowledgement high-water
 * belongs to one stream, so it is reset whenever last_rid changes. */
static void
ag_new_stream(struct agent_session *s, uint64_t rid, long count)
{
    s->last_rid = rid;
    s->last_chunk_count = count;
    s->acked_chunk = 0;
    s->have_chunk_ack = false; /* "none": no index acknowledged yet */
}

static enum agent_result
ag_emit_chunked(struct agent_session *s, const struct ag_parts *ps)
{
    enum agent_result r = AG_OK;
    size_t budget, i, nfrags = 0, nchunks;
    struct ag_frag *frags;
    size_t *lens, *chunk_of;
    struct ag_buf scratch, o;
    uint64_t rid = 0;

    if (ps->n > AG_MAX_FRAGS)
        return AG_LIMIT;
    frags = (struct ag_frag *) calloc(AG_MAX_FRAGS, sizeof *frags);
    lens = (size_t *) calloc(AG_MAX_FRAGS, sizeof *lens);
    chunk_of = (size_t *) calloc(AG_MAX_FRAGS, sizeof *chunk_of);
    if (!frags || !lens || !chunk_of) {
        free(frags);
        free(lens);
        free(chunk_of);
        return AG_INTERNAL;
    }
    ag_init(&scratch);
    ag_init(&o);

    budget = s->limit_line > AG_CHUNK_OVERHEAD + 1
                 ? s->limit_line - AG_CHUNK_OVERHEAD - 1
                 : 0;
    if (budget == 0) {
        r = AG_LIMIT;
        goto out;
    }
    for (i = 0; i < ps->n; ++i) {
        if (!ag_render_part_frags(ps, &ps->v[i], budget, &scratch, frags,
                                  &nfrags, AG_MAX_FRAGS)) {
            r = AG_LIMIT;
            goto out;
        }
    }
    for (i = 0; i < nfrags; ++i)
        lens[i] = frags[i].len;
    nchunks = agent_chunk_plan(lens, nfrags, budget, chunk_of, 65535);
    if (nchunks == 0) {
        r = AG_LIMIT;
        goto out;
    }

    for (i = 0; i < nchunks; ++i) {
        size_t j;
        uint64_t d;
        bool last = (i + 1 == nchunks);

        if (!ag_bump(s, &s->next_delivery)) {
            r = AG_LIMIT;
            goto out;
        }
        d = s->next_delivery;
        if (rid == 0)
            rid = d;
        o.len = 0;
        if (!ag_puts(&o, "{\"v\":1,\"ch\":\"control\","
                         "\"type\":\"chunk\",\"d\":")
            || !ag_put_u64(&o, d) || !ag_puts(&o, ",\"rid\":")
            || !ag_put_u64(&o, rid) || !ag_puts(&o, ",\"i\":")
            || !ag_put_u64(&o, (uint64_t) i) || !ag_puts(&o, ",\"last\":")
            || !ag_puts(&o, last ? "true" : "false")
            || !ag_puts(&o, ",\"parts\":[")) {
            r = AG_LIMIT;
            goto out;
        }
        {
            bool first = true;

            for (j = 0; j < nfrags; ++j) {
                if (chunk_of[j] != i)
                    continue;
                if (!first && !ag_putc(&o, ',')) {
                    r = AG_LIMIT;
                    goto out;
                }
                first = false;
                if (!ag_put(&o, scratch.p + frags[j].off,
                            frags[j].len)) {
                    r = AG_LIMIT;
                    goto out;
                }
            }
        }
        if (!ag_puts(&o, "]}")) {
            r = AG_LIMIT;
            goto out;
        }
        if (o.ovf) {
            r = AG_LIMIT;
            goto out;
        }
        r = ag_emit(s, o.p, o.len, d, true);
        if (r != AG_OK)
            goto out;
    }
    ag_new_stream(s, rid, (long) nchunks);
    r = AG_OK;

out:
    ag_free(&scratch);
    ag_free(&o);
    free(frags);
    free(lens);
    free(chunk_of);
    return r;
}

enum agent_result
agent_commit(struct agent_session *s, const struct agent_view *v,
             const struct agent_need *need)
{
    struct ag_parts ps;
    struct ag_buf o;
    enum agent_result r;
    uint64_t d, seq;

    if (!s || !v)
        return AG_INTERNAL;
    /* a commit supersedes content registered for the previous request */
    s->content_rows = (const struct agent_content_row *) 0;
    s->content_nrows = 0;
    /* Fail closed if this commit would silently replace an outstanding
     * request whose action the native handler has NOT accepted.  A gameplay
     * request may only succeed one that was already accepted: the frozen
     * contract (section 11.3) leaves a semantically rejected action's request
     * outstanding, so republishing a fresh request id (and advancing the
     * durable sequence) here would be a protocol violation.  Checked before
     * anything is built, counted, or written. */
    if (need && need->kind != AG_NEED_NONE && s->outstanding_id != 0
        && !(s->have_action && s->action_id == s->outstanding_id))
        return AG_INTERNAL;
    /* validate every public value before anything is built or counted, so a
     * malformed view fails closed with no output and no counter movement */
    if (!ag_validate_view(v, need))
        return AG_LIMIT;

    s->reply_len = 0;
    s->have_reply = false;

    if (!ag_bump(s, &s->next_delivery))
        return AG_LIMIT;
    d = s->next_delivery;
    if (!ag_bump(s, &s->next_seq))
        return AG_LIMIT;
    seq = s->next_seq;

    ag_parts_init(&ps);
    ag_init(&o);
    if (!ag_build_parts(&ps, v, need, seq)) {
        ag_free(&o);
        ag_parts_free(&ps);
        return AG_LIMIT;
    }
    if (!ag_emit_fields(&o, &ps, d, seq) || o.ovf) {
        ag_free(&o);
        ag_parts_free(&ps);
        return AG_LIMIT;
    }

    if (!s->force_chunk && o.len + 1 <= s->limit_line) {
        r = ag_emit(s, o.p, o.len, d, true);
    } else {
        /* the delivery counter for chunk 0 is the one just allocated */
        --s->next_delivery;
        ag_new_stream(s, 0, 0);
        r = ag_emit_chunked(s, &ps);
    }
    ag_free(&o);
    ag_parts_free(&ps);
    if (r != AG_OK)
        return r;

    /* publish every outstanding-request constraint */
    if (need && need->kind != AG_NEED_NONE) {
        s->outstanding_id = need->id;
        s->outstanding_kind = need->kind;
        if (need->content) {
            strncpy(s->need_content, need->content,
                    sizeof s->need_content - 1);
            s->need_content[sizeof s->need_content - 1] = '\0';
        } else {
            s->need_content[0] = '\0';
        }
        if (need->menu) {
            strncpy(s->need_menu, need->menu, sizeof s->need_menu - 1);
            s->need_menu[sizeof s->need_menu - 1] = '\0';
        } else {
            s->need_menu[0] = '\0';
        }
        s->need_x0 = need->x0;
        s->need_y0 = need->y0;
        s->need_x1 = need->x1;
        s->need_y1 = need->y1;
        s->need_pages = need->pages > 0 ? need->pages : 0;
        s->need_max = (need->kind == AG_NEED_LINE
                       || need->kind == AG_NEED_EXTCMD)
                          ? need->max
                          : 0;
        memset(s->pages_done, 0, sizeof s->pages_done);
        s->pages_delivered = 0;
    } else {
        s->outstanding_id = 0;
        s->outstanding_kind = AG_NEED_NONE;
        s->need_content[0] = '\0';
        s->need_menu[0] = '\0';
        s->need_x0 = s->need_y0 = s->need_x1 = s->need_y1 = 0;
        s->need_pages = 0;
        s->need_max = 0;
        memset(s->pages_done, 0, sizeof s->pages_done);
        s->pages_delivered = 0;
    }

    /* retain the complete logical response for the last accepted action */
    if (s->have_action)
        s->have_reply = true;
    return AG_OK;
}

/* ------------------------------------------------------------------ */
/* receive path                                                        */
/* ------------------------------------------------------------------ */

static enum agent_result
ag_read_exact_line(struct agent_session *s, char *line, size_t *linelen)
{
    for (;;) {
        char *nl = (char *) memchr(s->inbuf, '\n', s->inlen);

        if (nl) {
            size_t n = (size_t) (nl - s->inbuf);

            memcpy(line, s->inbuf, n);
            line[n] = '\0';
            *linelen = n;
            memmove(s->inbuf, nl + 1, s->inlen - n - 1);
            s->inlen -= n + 1;
            return AG_OK;
        }
        if (s->inlen >= sizeof s->inbuf)
            return AG_LIMIT;
        {
            long got = s->read(s->io, s->inbuf + s->inlen,
                               sizeof s->inbuf - s->inlen);

            if (got <= 0) {
                s->eof = true;
                return AG_IO;
            }
            s->inlen += (size_t) got;
        }
    }
}

static bool
ag_page_delivered(const struct agent_session *s, long page)
{
    if (page < 0 || page >= AG_MAX_PAGES)
        return false;
    return (s->pages_done[page / 8] & (1u << (page % 8))) != 0;
}

static void
ag_mark_page(struct agent_session *s, long page)
{
    if (page < 0 || page >= AG_MAX_PAGES)
        return;
    if (!ag_page_delivered(s, page)) {
        s->pages_done[page / 8] |= (unsigned char) (1u << (page % 8));
        ++s->pages_delivered;
    }
}

/* Render one content row exactly as a page record carries it. */
static void
ag_put_content_row(struct ag_buf *o, const struct agent_content_row *row)
{
    if (row->r > 0) {
        ag_puts(o, "{\"r\":");
        ag_put_i64(o, row->r);
        ag_puts(o, ",\"text\":");
        ag_put_jstr(o, row->text ? row->text : "");
        ag_puts(o, ",\"selectable\":");
        ag_puts(o, row->selectable ? "true" : "false");
        ag_puts(o, ",\"key\":");
        if (row->key)
            ag_put_i64(o, row->key);
        else
            ag_puts(o, "null");
        ag_puts(o, ",\"group\":");
        if (row->group)
            ag_put_i64(o, row->group);
        else
            ag_puts(o, "null");
        ag_puts(o, ",\"initial\":");
        if (row->has_initial)
            ag_put_i64(o, row->initial);
        else
            ag_puts(o, "null");
        ag_puts(o, ",\"style\":");
        ag_put_u64(o, row->style);
        ag_puts(o, ",\"color\":");
        ag_put_jstr(o, agent_color_name(row->color)
                           ? agent_color_name(row->color)
                           : "none");
        ag_puts(o, ",\"icon\":");
        if (row->has_icon) {
            ag_puts(o, "[\"");
            ag_putc(o, (char) row->icon.ch);
            ag_puts(o, "\",");
            ag_put_jstr(o, agent_color_name(row->icon.fg)
                               ? agent_color_name(row->icon.fg)
                               : "none");
            ag_puts(o, ",");
            ag_put_u64(o, row->icon.style);
            ag_puts(o, ",");
            ag_put_jstr(o, agent_color_name(row->icon.frame)
                               ? agent_color_name(row->icon.frame)
                               : "none");
            ag_putc(o, ']');
        } else {
            ag_puts(o, "null");
        }
        ag_putc(o, '}');
    } else {
        ag_puts(o, "{\"text\":");
        ag_put_jstr(o, row->text ? row->text : "");
        ag_puts(o, ",\"style\":");
        ag_put_u64(o, row->style);
        ag_putc(o, '}');
    }
}

/* The largest wrapper around a page's row array: the fixed page-record
 * preamble/postamble with every integer at its widest.  A page's encoded line
 * is bounded by this plus the sum of its rows, so bounding the whole line by
 * AG_PAGE_MAX_BYTES keeps every page inside the advertised byte limit. */
#define AG_PAGE_WRAPPER_RESERVE 160

/* Deterministically pack the content rows into pages bounded by BOTH
 * AG_PAGE_MAX_ROWS rows and AG_PAGE_MAX_BYTES encoded bytes.  The plan is a
 * pure function of the rows, so the request's declared page count, the window
 * descriptor, and get_page emission all agree exactly and retries reproduce
 * identical pages.  A row that alone exceeds the byte budget still occupies a
 * page by itself (content is never truncated, split, or reordered).  Returns
 * the number of pages and, when target < that count, the row range of the
 * target page. */
static size_t
ag_page_walk(const struct agent_content_row *rows, size_t nrows,
             size_t target, size_t *first, size_t *last)
{
    struct ag_buf t;
    size_t i = 0, pages = 0;

    if (!rows || nrows == 0)
        return 0;
    ag_init(&t);
    while (i < nrows) {
        size_t pf = i, used = 0;

        while (i < nrows) {
            size_t rsz;

            t.len = 0;
            ag_put_content_row(&t, &rows[i]);
            rsz = t.len;
            if (i > pf) {
                if ((i - pf) >= AG_PAGE_MAX_ROWS)
                    break;
                if (AG_PAGE_WRAPPER_RESERVE + used + 1 + rsz
                    > AG_PAGE_MAX_BYTES)
                    break;
                used += 1 + rsz;
            } else {
                used = rsz;
            }
            ++i;
        }
        if (target == pages) {
            *first = pf;
            *last = i;
        }
        ++pages;
    }
    ag_free(&t);
    return pages;
}

/* Emit a single page of the outstanding content. */
static enum agent_result
ag_emit_page(struct agent_session *s, const char *content, long page,
             int pages)
{
    struct ag_buf o;
    enum agent_result r;
    uint64_t d;
    size_t first = 0, last = 0, i;

    (void) ag_page_walk(s->content_rows, s->content_nrows, (size_t) page,
                        &first, &last);
    if (!ag_bump(s, &s->next_delivery))
        return AG_LIMIT;
    d = s->next_delivery;
    ag_init(&o);
    ag_puts(&o, "{\"v\":1,\"ch\":\"control\",\"type\":\"page\",\"d\":");
    ag_put_u64(&o, d);
    ag_puts(&o, ",\"content\":");
    ag_put_jstr(&o, content);
    ag_puts(&o, ",\"page\":");
    ag_put_i64(&o, page);
    ag_puts(&o, ",\"pages\":");
    ag_put_i64(&o, pages);
    ag_puts(&o, ",\"rows\":[");
    for (i = first; i < last; ++i) {
        if (i > first)
            ag_putc(&o, ',');
        ag_put_content_row(&o, &s->content_rows[i]);
    }
    ag_puts(&o, "]}");
    if (o.ovf) {
        ag_free(&o);
        return AG_LIMIT;
    }
    r = ag_emit(s, o.p, o.len, d, false);
    ag_free(&o);
    return r;
}

size_t
agent_content_pages(const struct agent_content_row *rows, size_t nrows)
{
    size_t first = 0, last = 0;

    return ag_page_walk(rows, nrows, (size_t) -1, &first, &last);
}

void
agent_session_set_content(struct agent_session *s,
                          const struct agent_content_row *rows,
                          size_t nrows)
{
    if (!s)
        return;
    s->content_rows = (rows && nrows)
                          ? rows : (const struct agent_content_row *) 0;
    s->content_nrows = (rows && nrows) ? nrows : 0;
}

/* Handle one parsed transport auxiliary.  Returns AG_OK to continue reading,
 * AG_IO on a transport failure, or AG_BAD_INPUT after emitting invalid. */
static enum agent_result
ag_handle_aux(struct agent_session *s, struct agent_aux *aux)
{
    switch (aux->kind) {
    case AG_AUX_ACK_SEQ:
        if (aux->seq > s->next_seq) {
            if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        s->acked_seq = aux->seq;
        return AG_OK;

    case AG_AUX_ACK_CHUNK:
        if (s->last_rid == 0 || aux->rid != s->last_rid
            || s->last_chunk_count <= 0) {
            if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        if (aux->i >= s->last_chunk_count) {
            if (agent_write_invalid(s, AG_INV_RANGE) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        /* Cumulative and contiguous, with an explicit "none" state: the first
         * acknowledgement of a stream must be index 0, a repeat of an
         * acknowledged index is idempotent, the next index advances, and
         * anything beyond is a gap. */
        if (!s->have_chunk_ack) {
            if (aux->i != 0) {
                if (agent_write_invalid(s, AG_INV_INCOMPLETE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            s->have_chunk_ack = true;
            s->acked_chunk = 0;
            return AG_OK;
        }
        if ((uint64_t) aux->i > s->acked_chunk + 1) {
            if (agent_write_invalid(s, AG_INV_INCOMPLETE) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        if ((uint64_t) aux->i > s->acked_chunk)
            s->acked_chunk = (uint64_t) aux->i;
        return AG_OK;

    case AG_AUX_GET_PAGE:
        if (s->outstanding_id == 0 || aux->id != s->outstanding_id) {
            if (agent_write_invalid(s, AG_INV_KIND) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        if (s->need_content[0] == '\0'
            || strcmp(aux->content, s->need_content) != 0
            || aux->page >= s->need_pages) {
            if (agent_write_invalid(s, AG_INV_RANGE) != AG_OK)
                return AG_IO;
            return AG_BAD_INPUT;
        }
        /* the request-driven response is the acknowledgement; a repeat is a
         * retry and is re-sent without changing the delivered set.  The
         * delivered bit is set only once the page response has been sent. */
        if (ag_emit_page(s, aux->content, aux->page, s->need_pages) != AG_OK)
            return AG_IO;
        ag_mark_page(s, aux->page);
        return AG_OK;

    default:
        break;
    }
    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
        return AG_IO;
    return AG_BAD_INPUT;
}

/* Peek the "type" of a record, strictly validating the object as we go. */
static bool
ag_peek_type(const char *line, size_t len, char *type, size_t cap)
{
    struct ag_cur c;
    struct ag_buf key;
    char prev[AG_MAX_KEYS_PER_OBJECT][32];
    unsigned nprev = 0;
    bool found = false;

    if (len == 0 || memchr(line, 0, len) != (const void *) 0)
        return false;
    c.p = line;
    c.end = line + len;
    c.depth = 0;
    c.tokens = 0;
    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{' || !ag_tick(&c))
        return false;
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        return false;
    ag_init(&key);
    for (;;) {
        unsigned i;

        ag_ws(&c);
        if (!ag_key_text(&c, &key)) {
            ag_free(&key);
            return false;
        }
        if (key.len >= sizeof prev[0] || nprev >= AG_MAX_KEYS_PER_OBJECT) {
            ag_free(&key);
            return false;
        }
        for (i = 0; i < nprev; ++i)
            if (strcmp(prev[i], key.p) == 0) {
                ag_free(&key);
                return false; /* duplicate key */
            }
        strcpy(prev[nprev++], key.p);
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&key);
            return false;
        }
        ++c.p;
        ag_ws(&c);
        if (strcmp(key.p, "type") == 0) {
            struct ag_buf t;

            ag_init(&t);
            if (!ag_string(&c, &t) || t.len + 1 > cap) {
                ag_free(&t);
                ag_free(&key);
                return false;
            }
            memcpy(type, t.p ? t.p : "", t.len);
            type[t.len] = '\0';
            ag_free(&t);
            found = true;
        } else if (!ag_skip_value(&c)) {
            ag_free(&key);
            return false;
        }
        ag_free(&key);
        ag_ws(&c);
        if (c.p < c.end && *c.p == ',') {
            ++c.p;
            continue;
        }
        if (c.p < c.end && *c.p == '}') {
            ++c.p;
            break;
        }
        return false;
    }
    ag_ws(&c);
    return found && c.p == c.end;
}

enum agent_result
agent_receive(struct agent_session *s, struct agent_action *out)
{
    char line[AG_MAX_LINE_BYTES];
    size_t len;

    if (!s || !out)
        return AG_INTERNAL;
    out->replay = false;
    s->pending_len = 0;

    for (;;) {
        enum agent_result lr;
        char type[24];

        if (s->closed)
            return AG_IO;
        lr = ag_read_exact_line(s, line, &len);
        if (lr != AG_OK)
            return lr;
        if (len == 0)
            continue; /* tolerate a bare newline */
        if (memchr(line, '\r', len) != (const void *) 0) {
            if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                return AG_IO;
            continue;
        }
        if (!ag_peek_type(line, len, type, sizeof type)) {
            if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                return AG_IO;
            continue;
        }

        if (strcmp(type, "ack_seq") == 0 || strcmp(type, "ack_chunk") == 0
            || strcmp(type, "get_page") == 0) {
            struct agent_aux aux;
            enum agent_result ar = agent_parse_aux(line, len, &aux);

            if (ar != AG_OK) {
                if (agent_write_invalid(s, aux.code ? aux.code
                                                    : AG_INV_SCHEMA)
                    != AG_OK)
                    return AG_IO;
                continue;
            }
            lr = ag_handle_aux(s, &aux);
            if (lr == AG_IO)
                return AG_IO;
            continue;
        }
        if (strcmp(type, "act") != 0) {
            if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                return AG_IO;
            continue;
        }

        /* an act record */
        {
            enum agent_result pr;
            uint32_t h = ag_hash(line, len);

            pr = agent_parse_action(line, len, out);
            if (pr != AG_OK) {
                if (agent_write_invalid(s, out->code ? out->code
                                                     : AG_INV_SCHEMA)
                    != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }

            /* accepted-action identity: exact bytes decide; the hash is
             * only a fast pre-filter.  An identical retry replays the
             * retained response; conflicting reuse of an accepted id
             * closes. */
            if (s->have_action && out->id == s->action_id) {
                if (s->action_hash != h)
                    return AG_INTERNAL;
                if (s->action_len != len || !s->action_text
                    || memcmp(s->action_text, line, len) != 0)
                    return AG_INTERNAL;
                if (!s->have_reply)
                    return AG_INTERNAL;
                if (ag_write_all(s, s->reply, s->reply_len) != AG_OK)
                    return AG_IO;
                ++s->replays;
                continue;
            }
            if (s->have_action && out->id < s->action_id) {
                out->code = AG_INV_STALE;
                if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* outstanding request identity */
            if (s->outstanding_id != 0 && out->id != s->outstanding_id) {
                enum agent_invalid_code code
                    = (out->id < s->outstanding_id) ? AG_INV_STALE
                                                    : AG_INV_KIND;

                out->code = code;
                if (agent_write_invalid(s, code) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* kind must be compatible with the outstanding request */
            {
                enum agent_need_kind k = s->outstanding_kind;
                bool ok = true;

                switch (k) {
                case AG_NEED_COMMAND:
                case AG_NEED_KEY:
                case AG_NEED_DIRECTION:
                    ok = (out->kind == AG_ACT_KEY);
                    break;
                case AG_NEED_POSITION:
                    ok = (out->kind == AG_ACT_POSITION
                          || out->kind == AG_ACT_KEY);
                    break;
                case AG_NEED_YN:
                    ok = (out->kind == AG_ACT_YN);
                    break;
                case AG_NEED_LINE:
                case AG_NEED_EXTCMD:
                    /* native Escape / -1 maps to an explicit cancel */
                    ok = (out->kind == AG_ACT_TEXT
                          || out->kind == AG_ACT_CANCEL);
                    break;
                case AG_NEED_MENU:
                    ok = (out->kind == AG_ACT_MENU
                          || out->kind == AG_ACT_CANCEL
                          || out->kind == AG_ACT_ACK);
                    break;
                case AG_NEED_ACK:
                    ok = (out->kind == AG_ACT_ACK
                          || out->kind == AG_ACT_CANCEL);
                    break;
                default:
                    break;
                }
                if (!ok) {
                    out->code = AG_INV_KIND;
                    if (agent_write_invalid(s, AG_INV_KIND) != AG_OK)
                        return AG_IO;
                    return AG_BAD_INPUT;
                }
            }
            /* a menu answer must name the generation the request pinned */
            if (s->outstanding_kind == AG_NEED_MENU
                && out->kind == AG_ACT_MENU
                && strcmp(out->menu, s->need_menu) != 0) {
                out->code = AG_INV_STALE;
                if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* a position must lie inside the advertised rectangle */
            if (out->kind == AG_ACT_POSITION
                && (out->px < s->need_x0 || out->px > s->need_x1
                    || out->py < s->need_y0 || out->py > s->need_y1)) {
                out->code = AG_INV_RANGE;
                if (agent_write_invalid(s, AG_INV_RANGE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* a line/extcmd answer must fit the byte budget the request
             * advertised, counted over the decoded UTF-8 bytes */
            if (out->kind == AG_ACT_TEXT
                && (s->outstanding_kind == AG_NEED_LINE
                    || s->outstanding_kind == AG_NEED_EXTCMD)
                && strlen(out->text) > (size_t) s->need_max) {
                out->code = AG_INV_RANGE;
                if (agent_write_invalid(s, AG_INV_RANGE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* every required page must be delivered before a selection */
            if ((s->outstanding_kind == AG_NEED_MENU
                 || s->outstanding_kind == AG_NEED_ACK)
                && (out->kind == AG_ACT_MENU || out->kind == AG_ACT_ACK)
                && s->need_pages > 0
                && s->pages_delivered < s->need_pages) {
                out->code = AG_INV_INCOMPLETE;
                if (agent_write_invalid(s, AG_INV_INCOMPLETE) != AG_OK)
                    return AG_IO;
                return AG_BAD_INPUT;
            }
            /* an action may acknowledge a durable version implicitly */
            if (out->has_seq) {
                if (out->seq > s->next_seq) {
                    out->code = AG_INV_STALE;
                    if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                        return AG_IO;
                    return AG_BAD_INPUT;
                }
                s->acked_seq = out->seq;
            }
            /* not accepted yet: record the raw line and the session-owned
             * identity for agent_accept() */
            memcpy(s->pending_line, line, len);
            s->pending_len = len;
            s->pending_hash = h;
            s->pending_id = out->id;
            s->pending_kind = out->kind;
            out->replay = false;
            return AG_OK;
        }
    }
}

enum agent_result
agent_accept(struct agent_session *s)
{
    if (!s)
        return AG_INTERNAL;
    if (s->pending_len == 0)
        return AG_INTERNAL; /* nothing was received for this action */
    /* acceptance is bound to the session-owned pending identity only; a zero
     * id is never a valid wire action and can never be accepted */
    if (s->pending_id == 0 || s->pending_kind == AG_ACT_NONE)
        return AG_INTERNAL;
    if (!ag_action_store(s, s->pending_line, s->pending_len))
        return AG_LIMIT;
    s->action_hash = s->pending_hash;
    s->action_id = s->pending_id;
    s->have_action = true;
    s->have_reply = false;
    /* the pending identity is consumed exactly once */
    s->pending_len = 0;
    s->pending_id = 0;
    s->pending_kind = AG_ACT_NONE;
    return AG_OK;
}
