/* agent_view.h -- engine-free appearance and text normalization. */

#ifndef AGENT_VIEW_H
#define AGENT_VIEW_H

#include "agent_types.h"

/* Normalize a synthetic render candidate to a public cell.
 *
 * Returns false when the candidate cannot be published: a non-ASCII
 * character or an out-of-range color slot.  Otherwise writes the public
 * 4-tuple, applying the fixed profile's displayed precedence:
 *   frame color wins over pet highlighting; pet/pile/detection/black-and-white
 *   inverse set the public inverse style bit in the map context; map-only
 *   reasons are ignored in the menu context; wizard-only reasons are dropped.
 */
bool agent_normalize_appearance(const struct agent_render_input *in,
                                enum agent_render_context ctx,
                                struct agent_cell *out);

/* Project a native yes/no choices string to its displayed prefix.
 *
 * The native string may carry an undisplayed accepted suffix after an embedded
 * Escape.  Only bytes before the first Escape (or NUL) are copied.  Bytes
 * outside printable ASCII are rejected, and overflow of out->cap fails.
 * len may be 0; out->len is set to the number of copied bytes.
 */
bool agent_visible_choices(const char *choices, size_t len,
                           struct agent_text *out);

#endif /* AGENT_VIEW_H */
