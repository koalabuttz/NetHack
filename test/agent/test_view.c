/* test_view.c -- appearance normalization and hidden-choice projection.
 *
 * Golden vectors for doc/agent-interface.md sections 2.3 and 4.2.  Engine-free.
 */

#include <stdio.h>
#include <string.h>

#include "agent_view.h"

static int failures;

#define CHECK(cond)                                                       \
    do {                                                                  \
        if (!(cond)) {                                                    \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);        \
            ++failures;                                                   \
        }                                                                 \
    } while (0)

static void
cell(struct agent_render_input *in, unsigned char ch, unsigned char fg,
     unsigned char frame)
{
    memset(in, 0, sizeof *in);
    in->ch = ch;
    in->fg = fg;
    in->frame = frame;
}

int
main(void)
{
    struct agent_render_input in;
    struct agent_cell a, b;
    struct agent_text t;
    char buf[32];

    /* ---- equal displayed appearance, different raw reasons ---- */
    cell(&in, '@', AG_COL_WHITE, AG_COL_NONE);
    in.pet_attr = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, '@', AG_COL_WHITE, AG_COL_NONE);
    in.detected = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &b));
    CHECK(a.style == AG_STYLE_INVERSE);
    CHECK(memcmp(&a, &b, sizeof a) == 0);

    cell(&in, '@', AG_COL_WHITE, AG_COL_NONE);
    in.pile_attr = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &b));
    CHECK(memcmp(&a, &b, sizeof a) == 0);
    cell(&in, '@', AG_COL_WHITE, AG_COL_NONE);
    in.bw_inverse = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &b));
    CHECK(memcmp(&a, &b, sizeof a) == 0);

    /* no reason at all is a plain cell */
    cell(&in, '@', AG_COL_WHITE, AG_COL_NONE);
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &b));
    CHECK(b.style == 0);

    /* ---- a displayed frame color beats pet highlighting ---- */
    cell(&in, 'd', AG_COL_GREEN, AG_COL_BLUE);
    in.pet_attr = true;
    in.detected = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &a));
    CHECK(a.frame == AG_COL_BLUE);
    CHECK(a.fg == AG_COL_GREEN);
    CHECK(a.style == 0); /* pet attribute dropped, not combined */

    /* ---- wizard-only reasons never survive ---- */
    cell(&in, 'w', AG_COL_WHITE, AG_COL_NONE);
    in.female_wizard = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, 'w', AG_COL_WHITE, AG_COL_NONE);
    CHECK(agent_normalize_appearance(&in, AG_RC_MAP, &b));
    CHECK(memcmp(&a, &b, sizeof a) == 0);
    CHECK(a.style == 0);

    /* ---- menu context ignores map-only reasons ---- */
    cell(&in, ')', AG_COL_GRAY, AG_COL_NONE);
    in.pet_attr = true;
    in.detected = true;
    in.pile_attr = true;
    in.bw_inverse = true;
    CHECK(agent_normalize_appearance(&in, AG_RC_MENU, &a));
    CHECK(a.style == 0);

    /* ---- rejection of unpublishable candidates ---- */
    cell(&in, 0x80, AG_COL_WHITE, AG_COL_NONE);
    CHECK(!agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, 0x1f, AG_COL_WHITE, AG_COL_NONE);
    CHECK(!agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, 0x7f, AG_COL_WHITE, AG_COL_NONE);
    CHECK(!agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, 'x', 99, AG_COL_NONE);
    CHECK(!agent_normalize_appearance(&in, AG_RC_MAP, &a));
    cell(&in, 'x', AG_COL_WHITE, 99);
    CHECK(!agent_normalize_appearance(&in, AG_RC_MAP, &a));

    /* ---- blank tuple and the frozen color names ---- */
    CHECK(strcmp(agent_color_name(0), "black") == 0);
    CHECK(strcmp(agent_color_name(8), "none") == 0);
    CHECK(strcmp(agent_color_name(15), "white") == 0);
    CHECK(agent_color_name(16) == NULL);

    /* ---- visible choices stop before the hidden suffix ---- */
    t.buf = buf;
    t.cap = sizeof buf;
    /* the native string carries an undisplayed accepted suffix after Escape */
    CHECK(agent_visible_choices("ynq\x1b" "ABC", 7, &t));
    CHECK(t.len == 3);
    CHECK(memcmp(buf, "ynq", 3) == 0);

    /* unrestricted prompt: an empty displayed choice string */
    t.len = 0;
    CHECK(agent_visible_choices("", 0, &t));
    CHECK(t.len == 0);

    /* a NUL also terminates the displayed portion */
    t.len = 0;
    CHECK(agent_visible_choices("yn\0q", 4, &t));
    CHECK(t.len == 2);
    CHECK(memcmp(buf, "yn", 2) == 0);

    /* a non-printable byte is rejected rather than published */
    t.len = 0;
    CHECK(!agent_visible_choices("y\x01n", 3, &t));

    /* overflow of the caller buffer fails closed */
    t.cap = 2;
    t.len = 0;
    CHECK(!agent_visible_choices("ynq", 3, &t));

    if (failures) {
        printf("test_view: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_view: ok\n");
    return 0;
}
