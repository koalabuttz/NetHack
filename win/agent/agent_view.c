/* agent_view.c -- engine-free appearance and text normalization.
 *
 * No engine headers.  Everything here is a pure function of public values, so
 * the fixtures can prove that equal-looking inputs produce equal cells.
 */

#include "agent_view.h"

static const char *const ag_color_names[AG_COL_MAX] = {
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "gray",
    "none", "orange", "brightgreen", "yellow", "brightblue", "brightmagenta",
    "brightcyan", "white"
};

const char *
agent_color_name(uint8_t slot)
{
    if (slot >= AG_COL_MAX)
        return (const char *) 0;
    return ag_color_names[slot];
}

bool
agent_normalize_appearance(const struct agent_render_input *in,
                           enum agent_render_context ctx,
                           struct agent_cell *out)
{
    if (!in || !out)
        return false;
    /* ASCII built-in glyph profile only; no non-ASCII symbol fallback. */
    if (in->ch < 0x20 || in->ch > 0x7e)
        return false;
    if (in->fg >= AG_COL_MAX || in->frame >= AG_COL_MAX)
        return false;

    out->ch = in->ch;
    out->fg = in->fg;
    out->frame = in->frame;
    out->style = 0;

    /* A displayed frame color takes precedence over pet highlighting, so the
     * pet attribute is dropped rather than combined. */
    if (in->frame != AG_COL_NONE)
        return true;

    if (ctx == AG_RC_MAP) {
        if (in->pet_attr || in->pile_attr || in->detected || in->bw_inverse)
            out->style |= AG_STYLE_INVERSE;
    }
    /* Menu context uses menu-displayed icon rules; map-only pet/pile/
     * detection/inverse candidates are ignored there.
     * in->female_wizard is a wizard-only reason and is never applied, so raw
     * inputs that differ only by that reason collapse to the same tuple. */
    return true;
}

bool
agent_visible_choices(const char *choices, size_t len, struct agent_text *out)
{
    size_t i, n = 0;

    if (!out || (!choices && len))
        return false;

    for (i = 0; i < len; ++i) {
        unsigned char c = (unsigned char) choices[i];

        /* an embedded Escape (or NUL) starts the hidden accepted suffix */
        if (c == 0x1b || c == 0x00)
            break;
        /* only displayed, printable ASCII may be published */
        if (c < 0x20 || c > 0x7e)
            return false;
        if (n + 1 > out->cap)
            return false;
        out->buf[n++] = (char) c;
    }
    out->len = n;
    return true;
}
