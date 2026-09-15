/* agent_profile.h -- profile enforcement data for the agent port.
 *
 * GENERATED from doc/agent-profile-v1.tsv by test/agent/gen_profile.py
 * (--emit-c).  Do not edit by hand; edit the generator and regenerate.
 *
 * Every entry is an option whose frozen resolved value differs from this
 * revision's compiled default, so the agent profile has no unclassified
 * or implicit row: what is not listed here is pinned at the compiled
 * default by the engine's own initialization.
 */

#ifndef AGENT_PROFILE_H
#define AGENT_PROFILE_H

struct agent_pin {
    const char *name;   /* canonical option name */
    const char *value;  /* frozen resolved value */
    int locked_presentation; /* nonzero = presentation classification */
};

static const struct agent_pin agent_pins[] = {
    { "altmeta", "off", 0 },
    { "armorstatus", "off", 1 },
    { "ascii_map", "on", 1 },
    { "bgcolors", "off", 1 },
    { "bones", "off", 0 },
    { "color", "on", 1 },
    { "customcolors", "off", 1 },
    { "customsymbols", "off", 1 },
    { "hilite_pet", "on", 1 },
    { "hilite_pile", "on", 1 },
    { "hitpointbar", "off", 1 },
    { "mail", "off", 0 },
    { "menucolors", "off", 1 },
    { "number_pad", "0", 1 },
    { "perm_invent", "off", 1 },
    { "playmode", "normal", 0 },
    { "preload_tiles", "off", 0 },
    { "selectsaved", "off", 1 },
    { "showexp", "off", 1 },
    { "showvers", "off", 1 },
    { "statushilites", "off", 1 },
    { "terrainstatus", "off", 1 },
    { "tiled_map", "off", 0 },
    { "time", "on", 1 },
    { "use_inverse", "on", 1 },
    { "use_truecolor", "off", 1 },
    { "weaponstatus", "off", 1 },
    { "windowtype", "agent", 1 },
};

#define AGENT_PIN_COUNT (sizeof agent_pins / sizeof agent_pins[0])

#endif /* AGENT_PROFILE_H */
