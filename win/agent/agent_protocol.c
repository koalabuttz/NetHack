/* agent_protocol.c -- bounded JSON framing, durable commits, session state.
 *
 * Engine-free.  Strict, allocation-bounded, and deterministic: no hash-table
 * iteration order, fixed field order in every encoder.
 */

#include "agent_protocol.h"

#include <stdlib.h>
#include <string.h>

/* internal ceiling for one assembled logical record (production uses the
 * retained-state policy of doc/agent-interface.md section 6) */
#define AG_MAX_LOGICAL_BYTES (4u * 1024u * 1024u)
#define AG_CHUNK_OVERHEAD 160u

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
        return ag_putc(b, '-') && ag_put_u64(b, (uint64_t) (-v));
    return ag_put_u64(b, (uint64_t) v);
}

static bool
ag_put_jstr(struct ag_buf *b, const char *s)
{
    static const char hex[] = "0123456789abcdef";

    if (!ag_putc(b, '"'))
        return false;
    for (; *s; ++s) {
        unsigned char c = (unsigned char) *s;

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
ag_put_color(struct ag_buf *b, uint8_t slot)
{
    const char *nm = agent_color_name(slot);

    if (!nm)
        nm = "none";
    return ag_put_jstr(b, nm);
}

static bool
ag_put_style(struct ag_buf *b, uint8_t style)
{
    return ag_put_u64(b, style);
}

/* ------------------------------------------------------------------ */
/* strict JSON reader                                                  */
/* ------------------------------------------------------------------ */

struct ag_cur {
    const char *p;
    const char *end;
    int depth;
};

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
    if (c->p >= c->end || *c->p != '"')
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

/* Parse one integer.  Rejects fractions, exponents, '+', leading zeros. */
static bool
ag_int(struct ag_cur *c, long long *out, bool *over)
{
    bool neg = false;
    unsigned long long v = 0;
    const char *start;

    *over = false;
    if (c->p >= c->end)
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
        *out = (long long) (-(long long) v);
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

    if (c->depth >= AG_MAX_NESTING)
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
    if (c->depth >= AG_MAX_NESTING)
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
/* action parsing                                                      */
/* ------------------------------------------------------------------ */

static enum agent_result
ag_act_fail(struct agent_action *out, enum agent_invalid_code code)
{
    out->kind = AG_ACT_NONE;
    out->code = code;
    return AG_BAD_INPUT;
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
        ag_init(&key);
        if (!ag_string(c, &key)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        if (key.len >= AG_MAX_LINE_BYTES || !ag_reserve(&key, 1)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        key.p[key.len] = '\0';
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
            if (c->p >= c->end || *c->p != '[')
                goto bad;
            if (c->depth >= AG_MAX_NESTING)
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
            if (x < 0 || x > 79 || y < 0 || y > 20)
                goto range;
            out->px = (int) x;
            out->py = (int) y;
        } else if (strcmp(key.p, "mod") == 0) {
            long long v;
            bool over;

            if (seen_mod || !ag_int(c, &v, &over))
                goto bad;
            seen_mod = true;
            if (over || v < 0 || v > 255)
                goto range;
            out->pmod = (int) v;
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
            if (t.len > 32) {
                ag_free(&t);
                goto toobig;
            }
            memcpy(out->text, t.p ? t.p : "", t.len);
            out->text[t.len] = '\0';
            if (!ag_menu_id_ok(out->text)) {
                ag_free(&t);
                goto badfree;
            }
            out->menu = out->text;
            ag_free(&t);
        } else if (strcmp(key.p, "commit") == 0) {
            size_t n = 0;

            if (seen_commit)
                goto bad;
            seen_commit = true;
            ag_ws(c);
            if (c->p >= c->end || *c->p != '[')
                goto bad;
            if (c->depth >= AG_MAX_NESTING)
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
                    if (c->p >= c->end || *c->p != '[')
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
                    if (cnt < -1 || cnt == 0)
                        goto range;
                    if (cnt > AG_COUNT_MAX)
                        goto range;
                    if (n >= AG_MAX_MENU_ROWS || n >= out->commit_cap)
                        goto toobig;
                    if (!out->commit)
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

    /* exactly one tagged shape, with its optional companions only */
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

    /* companion fields are only legal with their shape */
    if (seen_mod && !seen_pos)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_count && !seen_yn)
        return ag_act_fail(out, AG_INV_SCHEMA);
    if (seen_pos && !seen_mod) {
        out->pmod = 0; /* mod is explicit; its absence is invalid */
        return ag_act_fail(out, AG_INV_SCHEMA);
    }
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

    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{')
        return ag_act_fail(out, AG_INV_SCHEMA);
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        return ag_act_fail(out, AG_INV_SCHEMA);

    ag_init(&key);
    for (;;) {
        ag_ws(&c);
        ag_init(&key);
        if (!ag_string(&c, &key)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        if (key.len >= AG_MAX_LINE_BYTES || !ag_reserve(&key, 1)) {
            ag_free(&key);
            return ag_act_fail(out, AG_INV_SCHEMA);
        }
        key.p[key.len] = '\0';
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
            if (over || v < 1)
                goto range;
            if ((unsigned long long) v > AG_COUNTER_MAX)
                goto range;
            out->seq = (uint64_t) v;
            out->has_seq = true;
        } else if (strcmp(key.p, "id") == 0) {
            long long v;
            bool over;

            if (have_id || !ag_int(&c, &v, &over))
                goto schema;
            have_id = true;
            if (over || v < 1)
                goto range;
            if ((unsigned long long) v > AG_COUNTER_MAX)
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

struct ag_part {
    int kind;
    const char *key; /* header key or status member name, else NULL */
    size_t off;
    size_t len;
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

static bool
ag_parts_add(struct ag_parts *ps, int kind, const char *key, const char *val,
             size_t vlen)
{
    struct ag_part *p;

    if (ps->n == ps->cap) {
        size_t ncap = ps->cap ? ps->cap * 2 : 64;
        struct ag_part *nv = (struct ag_part *) realloc(ps->v,
                                                        ncap * sizeof *nv);

        if (!nv)
            return false;
        ps->v = nv;
        ps->cap = ncap;
    }
    p = &ps->v[ps->n++];
    p->kind = kind;
    p->key = key;
    p->off = ps->arena.len;
    p->len = vlen;
    if (vlen && val && !ag_put(&ps->arena, val, vlen)) {
        --ps->n;
        return false;
    }
    return true;
}

static const char *
ag_part_val(const struct ag_parts *ps, const struct ag_part *p)
{
    return ps->arena.p + p->off;
}

static const char *
ag_array_key(int kind)
{
    switch (kind) {
    case AG_P_COND:
        return "cond";
    case AG_P_PAL:
        return "pal";
    case AG_P_MAP:
        return "map";
    case AG_P_MSG:
        return "msg";
    case AG_P_HIST:
        return "hist";
    case AG_P_WIN:
        return "windows";
    default:
        break;
    }
    return (const char *) 0;
}

/* Render the logical obs record from the parts, in fixed field order. */
static bool
ag_render_obs(const struct ag_parts *ps, struct ag_buf *o)
{
    size_t i;
    int open = 0; /* 0 none, 1 "s" object, 2 "cond", 3 "pal", 4 "map",
                     5 "msg", 6 "hist", 7 "windows" */
    bool first_top = true;
    bool first_in = true;

    if (!ag_putc(o, '{'))
        return false;

    for (i = 0; i < ps->n; ++i) {
        const struct ag_part *p = &ps->v[i];
        const char *v = ag_part_val(ps, p);

        if (p->kind == AG_P_HEADER || p->kind == AG_P_CUR
            || p->kind == AG_P_NEED) {
            if (open) {
                if (!ag_putc(o, open == 1 ? '}' : ']'))
                    return false;
                open = 0;
            }
            if (!first_top && !ag_putc(o, ','))
                return false;
            first_top = false;
            if (p->kind == AG_P_HEADER) {
                if (!ag_put_jstr(o, p->key) || !ag_putc(o, ':')
                    || !ag_put(o, v, p->len))
                    return false;
            } else {
                if (!ag_put_jstr(o, p->kind == AG_P_CUR ? "cur" : "need")
                    || !ag_putc(o, ':') || !ag_put(o, v, p->len))
                    return false;
            }
            continue;
        }
        if (p->kind == AG_P_STATUS) {
            if (open != 1) {
                if (open && !ag_putc(o, open == 1 ? '}' : ']'))
                    return false;
                if (!first_top && !ag_putc(o, ','))
                    return false;
                first_top = false;
                if (!ag_putc(o, '"') || !ag_puts(o, "s")
                    || !ag_puts(o, "\":{"))
                    return false;
                open = 1;
                first_in = true;
            }
            if (!first_in && !ag_putc(o, ','))
                return false;
            first_in = false;
            if (!ag_put_jstr(o, p->key) || !ag_putc(o, ':')
                || !ag_put(o, v, p->len))
                return false;
            continue;
        }
        /* array element */
        {
            int want = 2;
            const char *akey = ag_array_key(p->kind);

            if (p->kind == AG_P_PAL)
                want = 3;
            else if (p->kind == AG_P_MAP)
                want = 4;
            else if (p->kind == AG_P_MSG)
                want = 5;
            else if (p->kind == AG_P_HIST)
                want = 6;
            else if (p->kind == AG_P_WIN)
                want = 7;
            if (open != want) {
                if (open && !ag_putc(o, open == 1 ? '}' : ']'))
                    return false;
                if (!first_top && !ag_putc(o, ','))
                    return false;
                first_top = false;
                if (!ag_putc(o, '"') || !ag_puts(o, akey)
                    || !ag_puts(o, "\":["))
                    return false;
                open = want;
                first_in = true;
            }
            if (!first_in && !ag_putc(o, ','))
                return false;
            first_in = false;
            if (!ag_put(o, v, p->len))
                return false;
        }
    }
    if (open && !ag_putc(o, open == 1 ? '}' : ']'))
        return false;
    return ag_putc(o, '}');
}

/* Build the ordered part list for one durable commit. */
static bool
ag_build_parts(struct ag_parts *ps, const struct agent_view *v,
               const struct agent_need *need, uint64_t d, uint64_t seq)
{
    struct ag_buf b;
    size_t i, j;
    bool ok = true;
    const char *mname;

    ag_init(&b);
#define EMIT(kind, key)                                                     \
    do {                                                                    \
        if (ok)                                                             \
            ok = ag_parts_add(ps, (kind), (key), b.p ? b.p : "", b.len);     \
        b.len = 0;                                                          \
    } while (0)

    /* header scalars */
    ag_put_u64(&b, AG_VERSION);
    EMIT(AG_P_HEADER, "v");
    ag_put_jstr(&b, "player");
    EMIT(AG_P_HEADER, "ch");
    ag_put_jstr(&b, "obs");
    EMIT(AG_P_HEADER, "type");
    ag_put_u64(&b, d);
    EMIT(AG_P_HEADER, "d");
    ag_put_u64(&b, seq);
    EMIT(AG_P_HEADER, "seq");
    ag_puts(&b, "null");
    EMIT(AG_P_HEADER, "base");

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
        ag_put_style(&b, s->style);
        ag_putc(&b, '}');
        EMIT(AG_P_STATUS, s->name);
    }
    for (i = 0; i < v->ncond && ok; ++i) {
        const struct agent_cond *cd = &v->cond[i];

        ag_puts(&b, "{\"text\":");
        ag_put_jstr(&b, cd->text ? cd->text : "");
        ag_puts(&b, ",\"color\":");
        ag_put_color(&b, cd->color);
        ag_puts(&b, ",\"style\":");
        ag_put_style(&b, cd->style);
        ag_putc(&b, '}');
        EMIT(AG_P_COND, (const char *) 0);
    }
    for (i = 0; i < v->npal && ok; ++i) {
        ag_putc(&b, '[');
        ag_put_u64(&b, i);
        ag_puts(&b, ",\"");
        ag_putc(&b, (char) v->pal[i].ch);
        ag_puts(&b, "\",");
        ag_put_color(&b, v->pal[i].fg);
        ag_putc(&b, ',');
        ag_put_style(&b, v->pal[i].style);
        ag_putc(&b, ',');
        ag_put_color(&b, v->pal[i].frame);
        ag_putc(&b, ']');
        EMIT(AG_P_PAL, (const char *) 0);
    }
    for (j = 0; j < AG_MAP_ROWS && ok; ++j) {
        for (i = 0; i < AG_MAP_COLS && ok; ++i) {
            uint16_t id = v->map[j][i];

            if (id == 0)
                continue; /* blank cells are the declared default */
            ag_putc(&b, '[');
            ag_put_u64(&b, i + AG_MAP_MIN_X);
            ag_putc(&b, ',');
            ag_put_u64(&b, j + AG_MAP_MIN_Y);
            ag_putc(&b, ',');
            ag_put_u64(&b, id);
            ag_putc(&b, ']');
            EMIT(AG_P_MAP, (const char *) 0);
        }
    }
    if (v->has_cursor) {
        ag_putc(&b, '[');
        ag_put_u64(&b, (uint64_t) v->cur_x + AG_MAP_MIN_X);
        ag_putc(&b, ',');
        ag_put_u64(&b, (uint64_t) v->cur_y + AG_MAP_MIN_Y);
        ag_putc(&b, ']');
    } else {
        ag_puts(&b, "null");
    }
    EMIT(AG_P_CUR, (const char *) 0);

    for (i = 0; i < v->nmsg && ok; ++i) {
        ag_puts(&b, "{\"e\":");
        ag_put_u64(&b, v->msg[i].e);
        ag_puts(&b, ",\"text\":");
        ag_put_jstr(&b, v->msg[i].text ? v->msg[i].text : "");
        ag_puts(&b, ",\"style\":");
        ag_put_style(&b, v->msg[i].style);
        ag_putc(&b, '}');
        EMIT(AG_P_MSG, (const char *) 0);
    }
    for (i = 0; i < v->nhist && ok; ++i) {
        ag_puts(&b, "{\"e\":");
        ag_put_u64(&b, v->hist[i].e);
        ag_puts(&b, ",\"text\":");
        ag_put_jstr(&b, v->hist[i].text ? v->hist[i].text : "");
        ag_puts(&b, ",\"style\":");
        ag_put_style(&b, v->hist[i].style);
        ag_putc(&b, '}');
        EMIT(AG_P_HIST, (const char *) 0);
    }
    for (i = 0; i < v->nwindows && ok; ++i) {
        const struct agent_window *w = &v->windows[i];

        ag_puts(&b, "{\"w\":");
        ag_put_jstr(&b, w->w ? w->w : "w1");
        ag_puts(&b, ",\"kind\":");
        ag_put_jstr(&b, w->kind ? "menu" : "text");
        ag_puts(&b, ",\"title\":");
        ag_put_jstr(&b, w->title ? w->title : "");
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
        EMIT(AG_P_WIN, (const char *) 0);
    }

    /* need */
    if (!need || need->kind == AG_NEED_NONE) {
        ag_puts(&b, "null");
    } else {
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
        case AG_NEED_ACK:
            ag_put_jstr(&b, "ack");
            break;
        default:
            ag_put_jstr(&b, "command");
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
    EMIT(AG_P_NEED, (const char *) 0);

#undef EMIT

    ag_free(&b);
    return ok && !ps->arena.ovf;
}

/* Render one part as its chunk-grammar fragment. */
static bool
ag_part_fragment(const struct ag_parts *ps, const struct ag_part *p,
                 struct ag_buf *o)
{
    const char *tag = "h";
    size_t vlen = p->len;

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
    case AG_P_NEED:
        tag = "need";
        break;
    default:
        break;
    }
    if (!ag_puts(o, "{\"p\":") || !ag_put_jstr(o, tag))
        return false;
    if (p->key) {
        if (!ag_puts(o, ",\"k\":") || !ag_put_jstr(o, p->key))
            return false;
    }
    if (!ag_puts(o, ",\"val\":")
        || !ag_put(o, ag_part_val(ps, p), vlen))
        return false;
    return ag_putc(o, '}');
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

/* Write one physical record: bytes plus LF, retaining it for retry. */
static enum agent_result
ag_emit(struct agent_session *s, const char *buf, size_t len, uint64_t d)
{
    enum agent_result r;

    if (len + 1 > sizeof s->last_line)
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

enum agent_result
agent_write_hello(struct agent_session *s)
{
    struct ag_buf o;
    enum agent_result r;
    uint64_t d = ++s->next_delivery;

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
    r = ag_emit(s, o.p, o.len, d);
    ag_free(&o);
    return r;
}

enum agent_result
agent_write_closed(struct agent_session *s)
{
    static const char closed[] = "{\"v\":1,\"ch\":\"control\","
                                 "\"type\":\"closed\"}";
    enum agent_result r;

    /* exactly the bare closure: no delivery counter, no reason, no id */
    r = ag_write_all(s, closed, sizeof closed - 1);
    if (r == AG_OK)
        r = ag_write_all(s, "\n", 1);
    if (r == AG_OK)
        s->closed = true;
    return r;
}

enum agent_result
agent_write_invalid(struct agent_session *s, enum agent_invalid_code code)
{
    static const char *const names[] = { "schema", "stale", "kind", "range",
                                         "incomplete" };
    struct ag_buf o;
    enum agent_result r;
    uint64_t d = ++s->next_delivery;
    const char *nm = "schema";

    if (code >= AG_INV_SCHEMA && code <= AG_INV_INCOMPLETE)
        nm = names[code - 1];
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
    r = ag_emit(s, o.p, o.len, d);
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
    if (s->last_line_len == 0)
        return AG_INTERNAL;
    /* identical bytes and the original delivery counter */
    return ag_write_all(s, s->last_line, s->last_line_len);
}

/* Emit an oversized logical record as an ordered chunk stream. */
static enum agent_result
ag_emit_chunked(struct agent_session *s, const struct ag_parts *ps)
{
    enum agent_result r;
    size_t i, nchunks, budget;
    size_t *lens;
    size_t *chunk_of;
    struct ag_buf o;
    uint64_t rid = 0;

    r = AG_OK;

    if (ps->n > (size_t) 65535)
        return AG_LIMIT;
    lens = (size_t *) calloc(ps->n ? ps->n : 1, sizeof *lens);
    chunk_of = (size_t *) calloc(ps->n ? ps->n : 1, sizeof *chunk_of);
    if (!lens || !chunk_of) {
        free(lens);
        free(chunk_of);
        return AG_INTERNAL;
    }
    /* measure each fragment */
    for (i = 0; i < ps->n; ++i) {
        struct ag_buf f;

        ag_init(&f);
        if (!ag_part_fragment(ps, &ps->v[i], &f)) {
            ag_free(&f);
            free(lens);
            free(chunk_of);
            return AG_LIMIT;
        }
        lens[i] = f.len;
        ag_free(&f);
    }
    budget = s->limit_line > AG_CHUNK_OVERHEAD + 1
                 ? s->limit_line - AG_CHUNK_OVERHEAD - 1
                 : 0;
    nchunks = agent_chunk_plan(lens, ps->n, budget, chunk_of, 65535);
    if (nchunks == 0) {
        free(lens);
        free(chunk_of);
        return AG_LIMIT;
    }

    for (i = 0; i < nchunks; ++i) {
        size_t j;
        uint64_t d;
        bool last = (i + 1 == nchunks);

        ag_init(&o);
        d = ++s->next_delivery;
        if (rid == 0)
            rid = d;
        ag_puts(&o, "{\"v\":1,\"ch\":\"control\",\"type\":\"chunk\",\"d\":");
        ag_put_u64(&o, d);
        ag_puts(&o, ",\"rid\":");
        ag_put_u64(&o, rid);
        ag_puts(&o, ",\"i\":");
        ag_put_u64(&o, (uint64_t) i);
        ag_puts(&o, ",\"last\":");
        ag_puts(&o, last ? "true" : "false");
        ag_puts(&o, ",\"parts\":[");
        {
            bool first = true;

            for (j = 0; j < ps->n; ++j) {
                if (chunk_of[j] != i)
                    continue;
                if (!first && !ag_putc(&o, ',')) {
                    r = AG_LIMIT;
                    goto done;
                }
                first = false;
                if (!ag_part_fragment(ps, &ps->v[j], &o)) {
                    r = AG_LIMIT;
                    goto done;
                }
            }
        }
        ag_puts(&o, "]}");
        if (o.ovf) {
            r = AG_LIMIT;
            goto done;
        }
        r = ag_emit(s, o.p, o.len, d);
        ag_free(&o);
        if (r != AG_OK)
            goto out;
    }
    s->last_rid = rid;
    r = AG_OK;
    goto out;

done:
    ag_free(&o);
out:
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

    d = ++s->next_delivery;
    seq = ++s->next_seq;

    ag_parts_init(&ps);
    if (!ag_build_parts(&ps, v, need, d, seq)) {
        ag_parts_free(&ps);
        return AG_LIMIT;
    }
    ag_init(&o);
    if (!ag_render_obs(&ps, &o) || o.ovf) {
        ag_free(&o);
        ag_parts_free(&ps);
        return AG_LIMIT;
    }

    if (o.len + 1 <= s->limit_line) {
        r = ag_emit(s, o.p, o.len, d);
    } else {
        /* the delivery counter for chunk 0 is the one just allocated */
        --s->next_delivery;
        r = ag_emit_chunked(s, &ps);
    }
    ag_free(&o);
    ag_parts_free(&ps);
    if (r != AG_OK)
        return r;

    /* publish the outstanding request */
    if (need && need->kind != AG_NEED_NONE) {
        s->outstanding_id = need->id;
        s->outstanding_kind = need->kind;
        s->need_pages = need->pages > 0 ? need->pages : 0;
        s->pages_sent = 0;
        s->need_content = need->content;
    } else {
        s->outstanding_id = 0;
        s->outstanding_kind = AG_NEED_NONE;
        s->need_pages = 0;
        s->pages_sent = 0;
        s->need_content = (const char *) 0;
    }

    /* retain this response for the last accepted action */
    if (s->have_action) {
        if (s->last_line_len <= sizeof s->last_reply) {
            memcpy(s->last_reply, s->last_line, s->last_line_len);
            s->last_reply_len = s->last_line_len;
            s->have_reply = true;
        }
    }
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

/* Read a small string field of the form "key":<string>. */
static bool
ag_get_string_field(const char *line, size_t len, const char *key, char *out,
                    size_t cap)
{
    struct ag_cur c;
    bool found = false;

    c.p = line;
    c.end = line + len;
    c.depth = 0;
    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{')
        return false;
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        return false;
    for (;;) {
        struct ag_buf k, v;

        ag_ws(&c);
        ag_init(&k);
        ag_init(&v);
        if (!ag_string(&c, &k) || !ag_reserve(&k, 1)) {
            ag_free(&k);
            ag_free(&v);
            return false;
        }
        k.p[k.len] = '\0';
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&k);
            ag_free(&v);
            return false;
        }
        ++c.p;
        ag_ws(&c);
        if (k.p && strcmp(k.p, key) == 0) {
            if (!ag_string(&c, &v)) {
                ag_free(&k);
                ag_free(&v);
                return false;
            }
            if (v.len + 1 > cap) {
                ag_free(&k);
                ag_free(&v);
                return false;
            }
            memcpy(out, v.p ? v.p : "", v.len);
            out[v.len] = '\0';
            found = true;
        } else if (!ag_skip_value(&c)) {
            ag_free(&k);
            ag_free(&v);
            return false;
        }
        ag_free(&k);
        ag_free(&v);
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
    if (c.p != c.end)
        return false;
    return found;
}

/* Strict top-level key set check for a transport auxiliary record: every key
 * must be in the allowed list, each allowed key must appear exactly once, and
 * no other key may appear.  This enforces "no additional properties". */
static bool
ag_keys_exact(const char *line, size_t len, const char *const *keys,
              unsigned n)
{
    struct ag_cur c;
    unsigned seen = 0;
    unsigned i;

    c.p = line;
    c.end = line + len;
    c.depth = 0;
    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{')
        return false;
    ++c.depth;
    ++c.p;
    ag_ws(&c);
    if (c.p < c.end && *c.p == '}')
        return n == 0;
    for (;;) {
        struct ag_buf k;
        bool matched = false;

        ag_ws(&c);
        ag_init(&k);
        if (!ag_string(&c, &k) || !ag_reserve(&k, 1)) {
            ag_free(&k);
            return false;
        }
        k.p[k.len] = '\0';
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&k);
            return false;
        }
        ++c.p;
        ag_ws(&c);
        if (!ag_skip_value(&c)) {
            ag_free(&k);
            return false;
        }
        for (i = 0; i < n; ++i) {
            if (strcmp(k.p, keys[i]) == 0) {
                seen |= (1u << i);
                matched = true;
                break;
            }
        }
        ag_free(&k);
        if (!matched)
            return false;
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
    if (c.p != c.end)
        return false;
    return seen == ((n >= 32) ? 0xffffffffu : ((1u << n) - 1u));
}

static bool
ag_field_present(const char *line, size_t len, const char *key,
                 long long *ival, bool *is_int)
{
    struct ag_cur c;
    bool found = false;

    if (is_int)
        *is_int = false;
    c.p = line;
    c.end = line + len;
    c.depth = 0;
    ag_ws(&c);
    if (c.p >= c.end || *c.p != '{')
        return false;
    ++c.depth;
    ++c.p;
    for (;;) {
        struct ag_buf k;

        ag_ws(&c);
        ag_init(&k);
        if (!ag_string(&c, &k) || !ag_reserve(&k, 1)) {
            ag_free(&k);
            return false;
        }
        k.p[k.len] = '\0';
        ag_ws(&c);
        if (c.p >= c.end || *c.p != ':') {
            ag_free(&k);
            return false;
        }
        ++c.p;
        ag_ws(&c);
        if (k.p && strcmp(k.p, key) == 0) {
            long long v;
            bool over;

            if (!ag_int(&c, &v, &over) || over) {
                ag_free(&k);
                return false;
            }
            if (ival)
                *ival = v;
            if (is_int)
                *is_int = true;
            found = true;
        } else if (!ag_skip_value(&c)) {
            ag_free(&k);
            return false;
        }
        ag_free(&k);
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
    return found;
}

/* Emit a single page of the outstanding content. */
static enum agent_result
ag_emit_page(struct agent_session *s, const char *content, int page,
             int pages)
{
    struct ag_buf o;
    enum agent_result r;
    uint64_t d = ++s->next_delivery;

    ag_init(&o);
    ag_puts(&o, "{\"v\":1,\"ch\":\"control\",\"type\":\"page\",\"d\":");
    ag_put_u64(&o, d);
    ag_puts(&o, ",\"content\":");
    ag_put_jstr(&o, content);
    ag_puts(&o, ",\"page\":");
    ag_put_i64(&o, page);
    ag_puts(&o, ",\"pages\":");
    ag_put_i64(&o, pages);
    ag_puts(&o, ",\"rows\":[]}");
    if (o.ovf) {
        ag_free(&o);
        return AG_LIMIT;
    }
    r = ag_emit(s, o.p, o.len, d);
    ag_free(&o);
    return r;
}

enum agent_result
agent_receive(struct agent_session *s, struct agent_action *out)
{
    char line[AG_MAX_LINE_BYTES];
    size_t len;

    if (!s || !out)
        return AG_INTERNAL;
    out->replay = false;

    for (;;) {
        enum agent_result lr;

        if (s->closed)
            return AG_IO;
        lr = ag_read_exact_line(s, line, &len);
        if (lr != AG_OK)
            return lr;
        if (len == 0)
            continue; /* tolerate a bare newline */
        if (memchr(line, '\r', len) != (const void *) 0
            || memchr(line, 0, len) != (const void *) 0) {
            if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                return AG_IO;
            continue;
        }
        {
            char type[16];

            if (!ag_get_string_field(line, len, "type", type, sizeof type)) {
                if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                    return AG_IO;
                continue;
            }
            if (strcmp(type, "ack_seq") == 0) {
                static const char *const keys[] = { "v", "type", "seq" };
                long long v;
                bool is_int;

                if (!ag_keys_exact(line, len, keys, 3)) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if (!ag_field_present(line, len, "seq", &v, &is_int)
                    || !is_int || v < 1
                    || (unsigned long long) v > AG_COUNTER_MAX) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if ((uint64_t) v > s->next_seq) {
                    if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                        return AG_IO;
                    continue;
                }
                s->acked_seq = (uint64_t) v;
                continue;
            }
            if (strcmp(type, "ack_chunk") == 0) {
                static const char *const keys[] = { "v", "type", "rid", "i" };
                long long rid, idx;
                bool io1, io2;

                if (!ag_keys_exact(line, len, keys, 4)) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if (!ag_field_present(line, len, "rid", &rid, &io1)
                    || !ag_field_present(line, len, "i", &idx, &io2)
                    || !io1 || !io2 || rid < 1 || idx < 0) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if ((uint64_t) rid != s->last_rid) {
                    if (agent_write_invalid(s, AG_INV_STALE) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if ((uint64_t) idx > s->acked_chunk)
                    s->acked_chunk = (uint64_t) idx;
                continue;
            }
            if (strcmp(type, "get_page") == 0) {
                static const char *const keys[] = { "v", "type", "id",
                                                    "content", "page" };
                char content[32];
                long long page;
                bool is_int;

                if (!ag_keys_exact(line, len, keys, 5)) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if (!ag_get_string_field(line, len, "content", content,
                                         sizeof content)
                    || !ag_field_present(line, len, "page", &page, &is_int)
                    || !is_int) {
                    if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if (!s->need_content
                    || strcmp(content, s->need_content) != 0
                    || page < 0 || page >= s->need_pages) {
                    if (agent_write_invalid(s, AG_INV_RANGE) != AG_OK)
                        return AG_IO;
                    continue;
                }
                if (ag_emit_page(s, content, (int) page, s->need_pages)
                    != AG_OK)
                    return AG_IO;
                if (s->pages_sent < s->need_pages)
                    ++s->pages_sent;
                continue;
            }
            if (strcmp(type, "act") != 0) {
                if (agent_write_invalid(s, AG_INV_SCHEMA) != AG_OK)
                    return AG_IO;
                continue;
            }
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
            /* accepted-action identity: identical retry replays; conflicting
             * reuse of an accepted id closes; a stale id consumes nothing */
            if (s->have_action && out->id == s->action_id) {
                if (h != s->action_hash)
                    return AG_INTERNAL;
                if (!s->have_reply)
                    return AG_INTERNAL;
                if (ag_write_all(s, s->last_reply, s->last_reply_len)
                    != AG_OK)
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
            /* kind must match the outstanding request */
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
                    ok = (out->kind == AG_ACT_TEXT);
                    break;
                case AG_NEED_MENU:
                    ok = (out->kind == AG_ACT_MENU
                          || out->kind == AG_ACT_CANCEL
                          || out->kind == AG_ACT_ACK);
                    break;
                case AG_NEED_ACK:
                    ok = (out->kind == AG_ACT_ACK);
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
            /* required content must be fully delivered before a selection */
            if ((s->outstanding_kind == AG_NEED_MENU
                 || s->outstanding_kind == AG_NEED_ACK)
                && s->need_pages > 0 && s->pages_sent < s->need_pages) {
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
            /* accept: remember identity; the reply is retained on commit */
            s->have_action = true;
            s->action_id = out->id;
            s->action_hash = h;
            s->have_reply = false;
            out->replay = false;
            return AG_OK;
        }
    }
}
