#!/usr/bin/env python3
"""Generate doc/agent-profile-v1.tsv from include/optlist.h.

The mechanical columns (name, guard, setter, resolved value, saved binding) are
extracted from the option list.  The judgment columns (classification, override,
rationale) come from the tables in this file.  The script refuses to emit a row
for a name it does not have a classification for, so an unclassified active
option fails generation instead of shipping as an unclassified 'default'.

Usage:  python3 test/agent/gen_profile.py > doc/agent-profile-v1.tsv
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Preprocessor state for the standard Linux configuration in this tree
# (include/config.h as shipped by sys/unix/hints/linux.500).
BUILD = {
    "TTY_GRAPHICS": 1, "INSURANCE": 1, "CRASHREPORT": 1, "STATUS_HILITES": 1,
    "SCORE_ON_BOTL": 0, "TIMED_DELAY": 1, "ALTMETA": 1, "CHANGE_COLOR": 1,
    "BACKWARD_COMPAT": 1, "NEWS": 1, "PREV_MSGS": 0, "TILES_IN_GLYPHMAP": 0,
    "WIN32": 0, "MICRO": 0, "AMIGA": 0, "AMIGA_INTUITION": 0, "MAC68K": 0,
    "MSDOS": 0, "NO_TERMS": 0, "VIDEOSHADES": 0, "WIN32CON": 0, "WINCHAIN": 0,
    "CURSES_GRAPHICS": 0, "SND_SPEECH": 0, "SND_LIB_INTEGRATED": 0,
    "TTY_TILES_ESCCODES": 0, "TTY_SOUND_ESCCODES": 0, "DEBUG": 0,
}

# name -> (classification, rationale, override or None)
# classification: locked-presentation | locked-gameplay |
#                 unavailable-external | char-field
SCHED = {
    "windowtype": ("locked-presentation", "port locked to agent at bootstrap", "agent"),
    "playmode": ("locked-gameplay", "normal play only; debug/explore rejected", "normal"),
    "name": ("char-field", "character name via allowlisted startup data", None),
    "role": ("char-field", "starting role via ordinary callbacks/startup data", None),
    "race": ("char-field", "starting race via ordinary callbacks/startup data", None),
    "gender": ("char-field", "starting gender via ordinary callbacks/startup data", None),
    "alignment": ("char-field", "starting alignment via callbacks/startup data", None),
    "accessiblemsg": ("locked-presentation", "compiled default pinned", None),
    "acoustics": ("locked-gameplay", "compiled default pinned", None),
    "align_message": ("locked-presentation", "window alignment not advertised", None),
    "align_status": ("locked-presentation", "window alignment not advertised", None),
    "altkeyhandling": ("unavailable-external", "not applicable on this platform", None),
    "altmeta": ("locked-gameplay", "alternate-meta ambiguity disabled", "off"),
    "armorstatus": ("locked-presentation", "optional status field off", "off"),
    "ascii_map": ("locked-presentation", "ASCII map is the only map", "on"),
    "autocompletions": ("unavailable-external", "editor hook; no user editing", None),
    "autodescribe": ("locked-presentation", "compiled default pinned", None),
    "autodig": ("locked-gameplay", "compiled default pinned", None),
    "autoopen": ("locked-gameplay", "compiled default pinned", None),
    "autopickup": ("locked-gameplay", "compiled default pinned", None),
    "autopickup exceptions": ("unavailable-external", "editor hook; no user editing", None),
    "autoquiver": ("locked-gameplay", "compiled default pinned", None),
    "autounlock": ("locked-gameplay", "compiled default pinned", None),
    "bgcolors": ("locked-presentation", "background terrain layer disabled", "off"),
    "bind keys": ("unavailable-external", "runtime key rebinding denied", None),
    "BIOS": ("unavailable-external", "platform facility absent", None),
    "blind": ("locked-gameplay", "role-play extra pinned at default", None),
    "bones": ("locked-gameplay", "no cross-episode bones", "off"),
    "boulder": ("locked-gameplay", "deprecated alias; no effect", None),
    "catname": ("locked-gameplay", "pet name is not an allowlisted startup "
                "field; pinned at its default", None),
    "checkpoint": ("locked-gameplay", "private crash recovery; not agent-visible", None),
    "cmdassist": ("locked-gameplay", "compiled default pinned", None),
    "color": ("locked-presentation", "color on", "on"),
    "confirm": ("locked-gameplay", "compiled default pinned", None),
    "crash_email": ("unavailable-external", "external reporting facility", None),
    "crash_name": ("unavailable-external", "external reporting facility", None),
    "crash_urlmax": ("unavailable-external", "external reporting facility", None),
    "cursesgraphics": ("unavailable-external", "curses symset loading not compiled", None),
    "customcolors": ("locked-presentation", "no custom colors", "off"),
    "customsymbols": ("locked-presentation", "no unicode symbol replacement", "off"),
    "dark_room": ("locked-presentation", "map display rule pinned", None),
    "deaf": ("locked-gameplay", "role-play extra pinned at default", None),
    "DECgraphics": ("unavailable-external", "symset loading disabled", None),
    "debug_hunger": ("unavailable-external", "wizard/fuzzer only", None),
    "debug_mongen": ("unavailable-external", "wizard/fuzzer only", None),
    "debug_overwrite_stairs": ("unavailable-external", "wizard/fuzzer only", None),
    "disclose": ("locked-gameplay", "compiled default pinned", None),
    "dogname": ("locked-gameplay", "pet name is not an allowlisted startup "
                "field; pinned at its default", None),
    "dropped_nopick": ("locked-gameplay", "compiled default pinned", None),
    "dungeon": ("locked-presentation", "dungeon symbol set pinned", None),
    "effects": ("locked-presentation", "effect symbol set pinned", None),
    "eight_bit_tty": ("locked-presentation", "WC_EIGHT_BIT_IN capability advertised", None),
    "extmenu": ("locked-presentation", "compiled default pinned", None),
    "female": ("char-field", "deprecated alias for gender", None),
    "fireassist": ("locked-gameplay", "compiled default pinned", None),
    "fixinv": ("locked-gameplay", "compiled default pinned", None),
    "font_map": ("unavailable-external", "no font facility", None),
    "font_menu": ("unavailable-external", "no font facility", None),
    "font_message": ("unavailable-external", "no font facility", None),
    "font_size_map": ("unavailable-external", "no font facility", None),
    "font_size_menu": ("unavailable-external", "no font facility", None),
    "font_size_message": ("unavailable-external", "no font facility", None),
    "font_size_status": ("unavailable-external", "no font facility", None),
    "font_size_text": ("unavailable-external", "no font facility", None),
    "font_status": ("unavailable-external", "no font facility", None),
    "font_text": ("unavailable-external", "no font facility", None),
    "force_invmenu": ("locked-presentation", "compiled default pinned", None),
    "fruit": ("locked-gameplay", "compiled default pinned", None),
    "fullscreen": ("locked-presentation", "WC2_FULLSCREEN not advertised", None),
    "glyph": ("unavailable-external", "unicode glyph handler disabled", None),
    "goldX": ("locked-gameplay", "compiled default pinned", None),
    "guicolor": ("locked-presentation", "UI color capability not advertised", None),
    "help": ("locked-gameplay", "compiled default pinned", None),
    "herecmd_menu": ("locked-presentation", "compiled default pinned", None),
    "hicolor": ("unavailable-external", "compiled out (obsolete platform)", None),
    "hilite_pet": ("locked-presentation", "pet highlighting on", "on"),
    "hilite_pile": ("locked-presentation", "pile highlighting on", "on"),
    "hilite_status": ("locked-presentation", "configurable status highlighting off", "off"),
    "hitpointbar": ("locked-presentation", "hit-point bar off", "off"),
    "horsename": ("locked-gameplay", "pet name is not an allowlisted "
                  "startup field; pinned at its default", None),
    "IBMgraphics": ("unavailable-external", "symset loading disabled", None),
    "idlecheckpoint": ("locked-gameplay", "compiled default pinned", None),
    "ignintr": ("locked-gameplay", "compiled default pinned", None),
    "implicit_uncursed": ("locked-gameplay", "compiled default pinned", None),
    "large_font": ("unavailable-external", "compiled out (obsolete platform)", None),
    "legacy": ("locked-presentation", "introductory message pinned", None),
    "lit_corridor": ("locked-gameplay", "compiled default pinned", None),
    "lootabc": ("locked-gameplay", "compiled default pinned", None),
    "mail": ("unavailable-external", "mail daemon disabled", "off"),
    "map_mode": ("unavailable-external", "platform facility absent", None),
    "mention_decor": ("locked-gameplay", "compiled default pinned", None),
    "mention_map": ("locked-presentation", "accessibility message pinned", None),
    "mention_walls": ("locked-gameplay", "compiled default pinned", None),
    "menu_deselect_all": ("locked-presentation", "menu key binding pinned", None),
    "menu_deselect_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_first_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_headings": ("locked-presentation", "menu heading style pinned", None),
    "menu_invert_all": ("locked-presentation", "menu key binding pinned", None),
    "menu_invert_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_last_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_next_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_objsyms": ("locked-presentation", "menu icon rule pinned", None),
    "menu_overlay": ("locked-presentation", "tty-only menu geometry; absent in agent-only", None),
    "menu_previous_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_search": ("locked-presentation", "menu key binding pinned", None),
    "menu_select_all": ("locked-presentation", "menu key binding pinned", None),
    "menu_select_page": ("locked-presentation", "menu key binding pinned", None),
    "menu_shift_left": ("locked-presentation", "menu key binding pinned", None),
    "menu_shift_right": ("locked-presentation", "menu key binding pinned", None),
    "menu_tab_sep": ("unavailable-external", "wizard-only menu formatting", None),
    "menucolors": ("locked-presentation", "menu colors off", "off"),
    "menu colors": ("unavailable-external", "menu color editing denied", None),
    "menuinvertmode": ("locked-presentation", "compiled default pinned", None),
    "menustyle": ("locked-presentation", "compiled default pinned", None),
    "message types": ("unavailable-external", "message handler configuration denied", None),
    "mon_movement": ("locked-presentation", "accessibility message pinned", None),
    "monpolycontrol": ("unavailable-external", "wizard-only", None),
    "montelecontrol": ("unavailable-external", "wizard-only", None),
    "monsters": ("locked-presentation", "monster symbol set pinned", None),
    "mouse_support": ("unavailable-external", "mouse input not advertised", None),
    "msg_window": ("unavailable-external", "not applicable without tty history", None),
    "msghistory": ("locked-presentation", "history depth pinned", None),
    "news": ("locked-presentation", "compiled default pinned", None),
    "nudist": ("locked-gameplay", "role-play extra pinned at default", None),
    "null": ("locked-presentation", "compiled default pinned", None),
    "number_pad": ("locked-presentation", "standard native bindings; number_pad off", "off"),
    "objects": ("locked-presentation", "object symbol set pinned", None),
    "packorder": ("locked-gameplay", "compiled default pinned", None),
    "palette": ("unavailable-external", "color mutation denied", None),
    "paranoid_confirmation": ("locked-gameplay", "compiled default pinned", None),
    "pauper": ("locked-gameplay", "role-play extra pinned at default", None),
    "perm_invent": ("locked-presentation", "no permanent inventory", "off"),
    "perminv_mode": ("locked-presentation", "no permanent inventory", None),
    "petattr": ("locked-presentation", "pet attribute inverse", None),
    "pettype": ("locked-gameplay", "pet type is not an allowlisted startup "
                "field; pinned at its native default (random)", None),
    "pickup_burden": ("locked-gameplay", "compiled default pinned", None),
    "pickup_stolen": ("locked-gameplay", "compiled default pinned", None),
    "pickup_thrown": ("locked-gameplay", "compiled default pinned", None),
    "pickup_types": ("locked-gameplay", "compiled default pinned", None),
    "pile_limit": ("locked-gameplay", "compiled default pinned", None),
    "player_selection": ("locked-presentation", "native presentation allowed", None),
    "popup_dialog": ("locked-presentation", "popup dialog capability not advertised", None),
    "preload_tiles": ("unavailable-external", "tiles disabled", "off"),
    "price_quotes": ("locked-gameplay", "compiled default pinned", None),
    "pushweapon": ("locked-gameplay", "compiled default pinned", None),
    "query_menu": ("locked-presentation", "compiled default pinned", None),
    "quick_farsight": ("locked-gameplay", "compiled default pinned", None),
    "rawio": ("unavailable-external", "platform facility absent", None),
    "reroll": ("locked-gameplay", "role-play extra pinned at default", None),
    "rest_on_space": ("locked-gameplay", "compiled default pinned", None),
    "roguesymset": ("unavailable-external", "symset loading disabled", None),
    "runmode": ("locked-presentation", "display frequency pinned", None),
    "safe_pet": ("locked-gameplay", "compiled default pinned", None),
    "safe_wait": ("locked-gameplay", "compiled default pinned", None),
    "sanity_check": ("unavailable-external", "wizard-only", None),
    "scores": ("locked-presentation", "end-of-game disclosure pinned", None),
    "scroll_amount": ("locked-presentation", "window geometry not advertised", None),
    "scroll_margin": ("locked-presentation", "window geometry not advertised", None),
    "selectsaved": ("locked-presentation", "no save-selection UI", "off"),
    "showdamage": ("locked-presentation", "message content pinned", None),
    "showexp": ("locked-presentation", "optional status field off", "off"),
    "showrace": ("locked-presentation", "hero symbol rule pinned", None),
    "showscore": ("unavailable-external", "SCORE_ON_BOTL not compiled", None),
    "showvers": ("locked-presentation", "optional status field off", "off"),
    "silent": ("locked-presentation", "no terminal bell", None),
    "softkeyboard": ("unavailable-external", "platform facility absent", None),
    "sortdiscoveries": ("locked-presentation", "compiled default pinned", None),
    "sortloot": ("locked-presentation", "compiled default pinned", None),
    "sortpack": ("locked-presentation", "compiled default pinned", None),
    "sortvanquished": ("locked-presentation", "compiled default pinned", None),
    "soundlib": ("unavailable-external", "sound disabled", None),
    "sounds": ("unavailable-external", "sound disabled", None),
    "sparkle": ("locked-presentation", "map effect pinned", None),
    "spot_monsters": ("locked-presentation", "accessibility message pinned", None),
    "splash_screen": ("locked-presentation", "compiled default pinned", None),
    "standout": ("locked-presentation", "compiled default pinned", None),
    "status_updates": ("locked-presentation", "status updates pinned on", None),
    "status condition fields": ("unavailable-external", "condition configuration denied", None),
    "statushilites": ("locked-presentation", "status highlighting off", "off"),
    "status highlight rules": ("unavailable-external", "status highlight editing denied", None),
    "statuslines": ("locked-presentation", "compiled default pinned", None),
    "subkeyvalue": ("unavailable-external", "platform facility absent", None),
    "suppress_alert": ("locked-presentation", "compiled default pinned", None),
    "symset": ("unavailable-external", "symset loading disabled", None),
    "term_cols": ("locked-presentation", "map size is a build constant", None),
    "term_rows": ("locked-presentation", "map size is a build constant", None),
    "terrainstatus": ("locked-presentation", "optional status field off", "off"),
    "tile_file": ("unavailable-external", "tiles disabled", None),
    "tile_height": ("unavailable-external", "tiles disabled", None),
    "tile_width": ("unavailable-external", "tiles disabled", None),
    "tiled_map": ("unavailable-external", "tiles disabled", "off"),
    "time": ("locked-presentation", "displayed game time on", "on"),
    "timed_delay": ("locked-presentation", "port never sleeps; value inert", None),
    "tips": ("locked-presentation", "compiled default pinned", None),
    "tombstone": ("locked-presentation", "compiled default pinned", None),
    "toptenwin": ("locked-presentation", "compiled default pinned", None),
    "traps": ("locked-presentation", "trap symbol set pinned", None),
    "travel": ("locked-gameplay", "compiled default pinned", None),
    "travel_debug": ("unavailable-external", "wizard/DEBUG only", None),
    "tutorial": ("locked-gameplay", "compiled default pinned", None),
    "use_darkgray": ("locked-presentation", "WC2_DARKGRAY not advertised; inert", None),
    "use_inverse": ("locked-presentation", "inverse on", "on"),
    "use_truecolor": ("locked-presentation", "enhanced colors off", "off"),
    "vary_msgcount": ("locked-presentation", "WC_VARY_MSGCOUNT not advertised", None),
    "verbose": ("locked-presentation", "message verbosity pinned", None),
    "versinfo": ("locked-presentation", "compiled default pinned", None),
    "video": ("unavailable-external", "platform facility absent", None),
    "videocolors": ("unavailable-external", "platform facility absent", None),
    "videoshades": ("unavailable-external", "platform facility absent", None),
    "video_width": ("unavailable-external", "platform facility absent", None),
    "video_height": ("unavailable-external", "platform facility absent", None),
    "voices": ("unavailable-external", "sound/speech disabled", None),
    "vt_tiledata": ("unavailable-external", "tty tile escape codes not compiled", None),
    "vt_sounddata": ("unavailable-external", "tty sound escape codes not compiled", None),
    "warnings": ("locked-presentation", "warning symbol set pinned", None),
    "weaponstatus": ("locked-presentation", "optional status field off", "off"),
    "whatis_coord": ("locked-presentation", "compiled default pinned", None),
    "whatis_filter": ("locked-presentation", "compiled default pinned", None),
    "whatis_menu": ("locked-presentation", "compiled default pinned", None),
    "whatis_moveskip": ("locked-presentation", "compiled default pinned", None),
    "windowborders": ("locked-presentation", "compiled default pinned", None),
    "windowchain": ("unavailable-external", "window chaining not compiled", None),
    "windowcolors": ("locked-presentation", "window colors pinned", None),
    "wizmgender": ("unavailable-external", "wizard-only", None),
    "wizweight": ("unavailable-external", "wizard-only", None),
    "wraptext": ("locked-presentation", "WC2_WRAPTEXT not advertised", None),
    "cond_": ("locked-presentation", "condition namespace; see conditions section", None),
    "font": ("unavailable-external", "font prefix; no font facility", None),
    "IBM_": ("unavailable-external", "micro platform prefix; not compiled", None),
}

COLORS = [
    ("CLR_BLACK", 0, "black"), ("CLR_RED", 1, "red"), ("CLR_GREEN", 2, "green"),
    ("CLR_BROWN", 3, "brown"), ("CLR_BLUE", 4, "blue"), ("CLR_MAGENTA", 5, "magenta"),
    ("CLR_CYAN", 6, "cyan"), ("CLR_GRAY", 7, "gray"), ("NO_COLOR", 8, "none"),
    ("CLR_ORANGE", 9, "orange"), ("CLR_BRIGHT_GREEN", 10, "brightgreen"),
    ("CLR_YELLOW", 11, "yellow"), ("CLR_BRIGHT_BLUE", 12, "brightblue"),
    ("CLR_BRIGHT_MAGENTA", 13, "brightmagenta"), ("CLR_BRIGHT_CYAN", 14, "brightcyan"),
    ("CLR_WHITE", 15, "white"),
]

CONDITIONS = [
    ("barehanded", 20, "Bare", "off"), ("blind", 10, "Blind", "on"),
    ("busy", 20, "Busy", "off"), ("conf", 10, "Conf", "on"),
    ("deaf", 10, "Deaf", "on"), ("iron", 15, "Iron", "on"),
    ("fly", 10, "Fly", "on"), ("foodPois", 6, "FoodPois", "on"),
    ("glowhands", 20, "Glow", "off"), ("grab", 2, "Grab", "on"),
    ("hallucinat", 10, "Hallu", "on"), ("held", 20, "Held", "off"),
    ("ice", 20, "Icy", "off"), ("lava", 8, "InLava", "on"),
    ("levitate", 10, "Lev", "on"), ("paralyzed", 20, "Parlyz", "off"),
    ("ride", 10, "Ride", "on"), ("sleep", 20, "Zzz", "off"),
    ("slime", 6, "Slime", "on"), ("slip", 20, "Slip", "off"),
    ("stone", 6, "Stone", "on"), ("strngl", 4, "Strngl", "on"),
    ("stun", 10, "Stun", "on"), ("submerged", 15, "Submrg", "off"),
    ("termIll", 6, "TermIll", "on"), ("tethered", 20, "Teth", "off"),
    ("trap", 20, "Trap", "off"), ("unconscious", 20, "Out", "off"),
    ("woundedlegs", 20, "WLegs", "off"), ("holding", 20, "UHold", "off"),
]

CAPS = [
    ("WC_COLOR", "0x00000001L", "on", "basic color rendering"),
    ("WC_HILITE_PET", "0x00000002L", "on", "pet highlighting"),
    ("WC_ASCII_MAP", "0x00000004L", "off", "declared only with a fixture"),
    ("WC_TILED_MAP", "0x00000008L", "off", "tiles disabled"),
    ("WC_PRELOAD_TILES", "0x00000010L", "off", "tiles disabled"),
    ("WC_TILE_WIDTH", "0x00000020L", "off", "tiles disabled"),
    ("WC_TILE_HEIGHT", "0x00000040L", "off", "tiles disabled"),
    ("WC_TILE_FILE", "0x00000080L", "off", "tiles disabled"),
    ("WC_INVERSE", "0x00000100L", "on", "inverse video"),
    ("WC_ALIGN_MESSAGE", "0x00000200L", "off", "geometry not advertised"),
    ("WC_ALIGN_STATUS", "0x00000400L", "off", "geometry not advertised"),
    ("WC_VARY_MSGCOUNT", "0x00000800L", "off", "not advertised"),
    ("WC_FONT_MAP", "0x00001000L", "off", "no font facility"),
    ("WC_FONT_MESSAGE", "0x00002000L", "off", "no font facility"),
    ("WC_FONT_STATUS", "0x00004000L", "off", "no font facility"),
    ("WC_FONT_MENU", "0x00008000L", "off", "no font facility"),
    ("WC_FONT_TEXT", "0x00010000L", "off", "no font facility"),
    ("WC_FONTSIZ_MAP", "0x00020000L", "off", "no font facility"),
    ("WC_FONTSIZ_MESSAGE", "0x040000L", "off", "no font facility"),
    ("WC_FONTSIZ_STATUS", "0x0080000L", "off", "no font facility"),
    ("WC_FONTSIZ_MENU", "0x00100000L", "off", "no font facility"),
    ("WC_FONTSIZ_TEXT", "0x00200000L", "off", "no font facility"),
    ("WC_SCROLL_MARGIN", "0x00400000L", "off", "no viewport scrolling"),
    ("WC_SPLASH_SCREEN", "0x00800000L", "off", "not advertised"),
    ("WC_POPUP_DIALOG", "0x01000000L", "off", "not advertised"),
    ("WC_SCROLL_AMOUNT", "0x02000000L", "off", "no viewport scrolling"),
    ("WC_EIGHT_BIT_IN", "0x04000000L", "on", "8-bit character input"),
    ("WC_PERM_INVENT", "0x08000000L", "off", "no permanent inventory"),
    ("WC_MAP_MODE", "0x10000000L", "off", "platform facility absent"),
    ("WC_WINDOWCOLORS", "0x20000000L", "off", "window colors pinned"),
    ("WC_PLAYER_SELECTION", "0x40000000L", "off", "menu/line/yn cover selection"),
    ("WC_MOUSE_SUPPORT", "0x80000000L", "off", "mouse input not advertised"),
    ("WC2_FULLSCREEN", "0x0001L", "off", "not advertised"),
    ("WC2_SOFTKEYBOARD", "0x0002L", "off", "platform facility absent"),
    ("WC2_WRAPTEXT", "0x0004L", "off", "not advertised"),
    ("WC2_HILITE_STATUS", "0x0008L", "off", "status highlighting off"),
    ("WC2_SELECTSAVED", "0x0010L", "off", "no save-selection UI"),
    ("WC2_DARKGRAY", "0x0020L", "off", "dark gray not advertised"),
    ("WC2_HITPOINTBAR", "0x0040L", "off", "hit-point bar off"),
    ("WC2_FLUSH_STATUS", "0x0080L", "on", "status batching (initial)"),
    ("WC2_RESET_STATUS", "0x0100L", "on", "status reset (initial)"),
    ("WC2_TERM_SIZE", "0x0200L", "off", "terminal size is a build constant"),
    ("WC2_STATUSLINES", "0x0400L", "off", "status line count frozen at 2"),
    ("WC2_WINDOWBORDERS", "0x0800L", "off", "no window borders advertised"),
    ("WC2_PETATTR", "0x1000L", "off", "pet attr fixed to inverse"),
    ("WC2_GUICOLOR", "0x2000L", "off", "no non-map colour facility"),
    ("WC2_URGENT_MESG", "0x4000L", "off", "no message urgency channel"),
    ("WC2_SUPPRESS_HIST", "0x8000L", "off", "no history suppression channel"),
    ("WC2_MENU_SHIFT", "0x010000L", "off", "no horizontal menu scrolling"),
    ("WC2_U_UTF8STR", "0x020000L", "off", "unicode disabled"),
    ("WC2_EXTRACOLORS", "0x040000L", "off", "enhanced colors disabled"),
    ("WC2_EXTRASTATUS", "0x080000L", "off", "optional status fields off"),
]

BINDINGS = [
    ("command keys", "standard native bindings", "locked-presentation",
     "no runtime rebinding; bind keys denied"),
    ("number_pad", "off", "locked-presentation",
     "classic single-keystroke movement"),
    ("numpad meta prefixes", "not applicable (number_pad off)",
     "locked-presentation", "no alternate numpad interpretation"),
    ("altmeta", "off", "locked-presentation",
     "no ESC-prefix meta interpretation"),
    ("menu command keys", "built-in MENU_* defaults (wintype.h)",
     "locked-presentation",
     "menu_select_all '.', menu_deselect_all '-', menu_invert_all '@', "
     "menu_select_page ',', menu_deselect_page '\\', menu_invert_page '~', "
     "menu_first_page '^', menu_last_page '|', menu_next_page '>', "
     "menu_previous_page '<', menu_shift_left '{', menu_shift_right '}', "
     "menu_search ':'"),
    ("user key binding", "denied", "unavailable-external",
     "bind_key(user=true) gated; only core reset_commands/user=false allowed"),
    ("symbol set loading", "denied", "unavailable-external",
     "load_symset/parsesymbols gated"),
    ("mouse input", "denied", "unavailable-external",
     "WC_MOUSE_SUPPORT not advertised"),
]

SYMBOLS = [
    ("symbol source", "primary/Rogue built-in ASCII", "locked-presentation",
     "no custom/unicode/tile symbols"),
    ("custom symbol loading", "disabled", "unavailable-external",
     "load_symset/parsesymbols gated"),
    ("custom color loading", "disabled", "unavailable-external",
     "change_color denied"),
    ("background glyph layer", "disabled", "locked-presentation",
     "frame color only; no terrain identity"),
    ("initial blank appearance", "palette id 0 = blank", "locked-presentation",
     "unpainted cells share one blank tuple"),
]


# Resolved startup value of every ACTIVE compound option, taken from the
# option's own do_init/default assignment or its native default table.  The
# generator refuses to emit a row for a compound option that is missing here.
COMPOUND = {
    "align_message": "top (native default)",
    "align_status": "bottom (native default)",
    "autounlock": "apply-key (AUTOUNLOCK_APPLY_KEY)",
    "boulder": "(none; deprecated alias)",
    "catname": "(empty)",
    "disclose": "prompt, default no: inventory/attributes/vanquished/"
                "genocided/conduct/overview",
    "dogname": "(empty)",
    "dungeon": "(built-in default symbol set)",
    "effects": "(built-in default symbol set)",
    "fruit": "slime mold",
    "horsename": "(empty)",
    "hilite_status": "(empty rule set; status highlighting off)",
    "menu_deselect_all": "key '-' (MENU_UNSELECT_ALL)",
    "menu_deselect_page": "key '\\' (MENU_UNSELECT_PAGE)",
    "menu_first_page": "key '^' (MENU_FIRST_PAGE)",
    "menu_headings": "inverse (ATR_INVERSE, NO_COLOR)",
    "menu_invert_all": "key '@' (MENU_INVERT_ALL)",
    "menu_invert_page": "key '~' (MENU_INVERT_PAGE)",
    "menu_last_page": "key '|' (MENU_LAST_PAGE)",
    "menu_next_page": "key '>' (MENU_NEXT_PAGE)",
    "menu_objsyms": "off",
    "menu_previous_page": "key '<' (MENU_PREVIOUS_PAGE)",
    "menu_search": "key ':' (MENU_SEARCH)",
    "menu_select_all": "key '.' (MENU_SELECT_ALL)",
    "menu_select_page": "key ',' (MENU_SELECT_PAGE)",
    "menu_shift_left": "key '{' (MENU_SHIFT_LEFT)",
    "menu_shift_right": "key '}' (MENU_SHIFT_RIGHT)",
    "menuinvertmode": "1 (native default)",
    "menustyle": "full (MENU_FULL)",
    "monsters": "(built-in default symbol set)",
    "msg_window": "single (iflags.prevmsg_window = 's')",
    "msghistory": "20 (iflags.msg_history)",
    "objects": "(built-in default symbol set)",
    "packorder": "$)\"[%?+!=/(*`0_ (flags.inv_order = def_inv_order)",
    "paranoid_confirmation": "pray+swim+trap (PARANOID_PRAY|SWIM|TRAP)",
    "perminv_mode": "none (permanent inventory off)",
    "petattr": "inverse (ATR_INVERSE)",
    "pettype": "random (no preference)",
    "pickup_burden": "stressed (MOD_ENCUMBER)",
    "pickup_types": "all (empty list)",
    "pile_limit": "5 (PILE_LIMIT_DFLT)",
    "player_selection": "dialog (VIA_DIALOG)",
    "runmode": "run (RUN_LEAP)",
    "scores": "3 top / 2 around / own off",
    "scroll_amount": "1 (native default)",
    "scroll_margin": "5 (native default)",
    "sortdiscoveries": "o (flags.discosort)",
    "sortloot": "loot (flags.sortloot = 'l')",
    "sortvanquished": "o (flags.vansort)",
    "statushilites": "0 (status highlighting off)",
    "statuslines": "2 (iflags.wc2_statuslines)",
    "suppress_alert": "0 (flags.suppress_alert)",
    "term_cols": "80 (build constant COLNO)",
    "term_rows": "21 (build constant ROWNO)",
    "traps": "(built-in default symbol set)",
    "vary_msgcount": "0",
    "versinfo": "4 (have_branch)",
    "warnings": "(built-in default symbol set)",
    "whatis_coord": "none (GPCOORDS_NONE)",
    "whatis_filter": "none (GFILTER_NONE)",
    "windowborders": "auto (iflags.wc2_windowborders = 2)",
    "windowcolors": "(none set)",
    "name": "(adapter / allowlisted startup data)",
    "role": "(adapter / allowlisted startup data)",
    "race": "(adapter / allowlisted startup data)",
    "gender": "(adapter / allowlisted startup data)",
    "alignment": "(adapter / allowlisted startup data)",
}

# Options that are inactive in this build, compiled out, or external
# facilities: they have no resolved startup value to state.
COMPOUND_NA = (
    "DECgraphics", "IBMgraphics", "altkeyhandling", "crash_email", "crash_name",
    "crash_urlmax", "cursesgraphics", "font_map", "font_menu", "font_message",
    "font_size_map", "font_size_menu", "font_size_message", "font_size_status",
    "font_size_text", "font_status", "font_text", "glyph", "hicolor",
    "map_mode", "mouse_support", "palette", "roguesymset", "soundlib",
    "subkeyvalue", "symset", "tile_file", "tile_height", "tile_width",
    "video", "video_height", "video_width", "videocolors", "videoshades",
    "windowchain", "windowtype", "playmode", "number_pad",
)
for _n in COMPOUND_NA:
    COMPOUND.setdefault(_n, "-")


def split_args(s):
    parts, depth, cur, instr, esc = [], 0, "", False, False
    for ch in s:
        if instr:
            cur += ch
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def balanced(text):
    """True when every '(' has a matching ')' outside string literals."""
    depth, instr, esc = 0, False, False
    for ch in text:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth <= 0 and not instr


def strip_comments(s):
    return re.sub(r"/\*.*?\*/", " ", s).strip()


def guard_active(g):
    g = strip_comments(g).strip()
    if g.startswith("else_"):
        return not guard_active(g[5:])
    if g.startswith("elif_"):
        return False
    m = re.match(r"^ifdef\s+(\w+)$", g)
    if m:
        return bool(BUILD.get(m.group(1), 0))
    m = re.match(r"^ifndef\s+(\w+)$", g)
    if m:
        return not BUILD.get(m.group(1), 0)
    m = re.match(r"^if\s+(.*)$", g)
    if m:
        expr = m.group(1).strip()
        if expr == "0":
            return False
        neg = re.findall(r"!defined\((\w+)\)", expr)
        pos = re.findall(r"(?<!!)defined\((\w+)\)", expr)
        named = re.findall(r"(?<![(\w])([A-Z][A-Z0-9_]{2,})(?![)\w])", expr)
        ok = True
        for n in pos:
            ok = ok and bool(BUILD.get(n, 0))
        for n in neg:
            ok = ok and not BUILD.get(n, 0)
        for n in named:
            if n not in pos and n not in neg:
                ok = ok and bool(BUILD.get(n, 0))
        return ok
    return False


def parse_optlist():
    lines = open(os.path.join(ROOT, "include/optlist.h")).read().split("\n")
    stack, out, i = [], [], 0
    while i < len(lines):
        s = lines[i].strip()
        m = re.match(r"^#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)", s)
        if m:
            k, r = m.group(1), m.group(2).strip()
            if k == "endif":
                if stack:
                    stack.pop()
            elif k == "else":
                if stack:
                    stack[-1] = "else_" + stack[-1]
            elif k == "elif":
                if stack:
                    stack[-1] = "elif_" + r
            else:
                stack.append(k + " " + r)
            i += 1
            continue
        m = re.match(r"(NHOPTB|NHOPTC|NHOPTO|NHOPTP)\(", s)
        if m:
            typ = m.group(1)[5:]
            raw = s
            while not balanced(raw) and i + 1 < len(lines):
                i += 1
                raw += " " + lines[i].strip()
            args = raw[raw.index("(") + 1:]
            a = split_args(args)
            guards = [g for g in stack if not g.startswith("if defined(NHOPT")]
            out.append({"typ": typ, "args": a, "guard": " & ".join(guards)})
        i += 1
    return out


def main():
    entries = parse_optlist()
    # Collapse #if/#elif/#else variants: one row per option name, using the
    # variant whose guard is active in this build (falling back to the first).
    by_name = {}
    for e in entries:
        name = e["args"][0].strip('"')
        guards = e["guard"].split(" & ") if e["guard"] else []
        e["active"] = all(guard_active(g) for g in guards) if guards else True
        by_name.setdefault(name, []).append(e)

    records = []
    for name, variants in by_name.items():
        chosen = next((v for v in variants if v["active"]), variants[0])
        records.append((name, chosen))

    rows = []
    for name, e in records:
        typ, a = e["typ"], e["args"]
        guard = e["guard"] or "always"
        if typ == "B":
            setter, init, addr = a[4], a[5], a[10]
            if addr.startswith("(boolean"):
                addr = "-"
            saved = addr if addr.startswith("&") else "-"
            value = "on" if init == "On" else "off"
        elif typ == "C":
            setter, saved = a[4], "-"
            if name not in COMPOUND:
                sys.exit("ERROR: no resolved value for compound option %r"
                         % name)
            value = COMPOUND[name]
        elif typ == "O":
            setter, saved, value = a[5], "-", "n/a (action)"
        else:
            setter, saved, value = a[4], "-", "n/a (prefix)"
        if name not in SCHED:
            sys.exit("ERROR: no classification for option %r" % name)
        cls, why, override = SCHED[name]
        if override is not None:
            value = override + " (override)"
        if not e["active"]:
            cls = "unavailable-external"
            why = why + "; guard inactive in this build"
        guard = strip_comments(e["guard"]) or "always"
        rows.append((name, guard, value, cls, setter, saved, why))

    out = []
    w = out.append
    w("# doc/agent-profile-v1.tsv -- frozen effective profile for "
      "normal-ascii-color-v1 / llm-final-v1")
    w("# Generated by test/agent/gen_profile.py from include/optlist.h."
      "  Do not hand-edit; edit the generator.")
    w("# \"this build\" = the standard Linux configuration present in"
      " include/config.h (sys/unix/hints/linux.500).")
    w("# The strict agent-only build additionally defines NOTTYGRAPHICS,"
      " which removes the TTY_GRAPHICS-only rows below.")
    w("#")
    w("# Columns: name <TAB> availability/build guard <TAB> resolved startup"
      " value <TAB> classification <TAB> setter/handler entry point <TAB>"
      " saved-field binding <TAB> rationale")
    w("# classification: locked-presentation | locked-gameplay |"
      " unavailable-external | char-field")
    w("# value: the resolved startup value: 'on'/'off' for booleans, the native"
      " default for compound options, or 'x (override)' when the profile"
      " overrides it")
    w("# runtime mutation allowlist is EMPTY; every row is read-only at runtime")
    w("#")
    w("# == options (one row per active optlist.h entry) ==")
    for r in rows:
        w("\t".join(r))
    w("")
    w("# == colors (include/color.h; exact native names, native slot numbering) ==")
    w("# name <TAB> guard <TAB> resolved startup value <TAB> classification"
      " <TAB> setter <TAB> saved <TAB> rationale")
    for macro, slot, wire in COLORS:
        w("\t".join(("color.%s" % macro, "always", "%d/%s" % (slot, wire),
                     "locked-presentation", "colortable[]", "-",
                     "public wire name for native basic-color slot %d" % slot)))
    w("")
    w("# == status conditions (src/botl.c; default enabled subset frozen) ==")
    w("# name <TAB> guard <TAB> resolved startup value <TAB> classification"
      " <TAB> setter <TAB> saved <TAB> rationale")
    for name, rank, txt, dv in CONDITIONS:
        w("\t".join(("cond.%s" % name, "always", "%s (rank %d, text %r)" % (dv, rank, txt),
                     "locked-presentation", "cond_%s" % name, "-",
                     "condition display: %s when enabled and true" % ("shown" if dv == "on" else "hidden"))))
    w("")
    w("# == window capabilities (include/winprocs.h WC_/WC2_ bits) ==")
    w("# name <TAB> guard <TAB> resolved startup value <TAB> classification"
      " <TAB> setter <TAB> saved <TAB> rationale")
    for name, val, onoff, why in CAPS:
        w("\t".join((("cap.%s" % name), "always", "%s %s" % (val, onoff),
                     "locked-presentation", "windowprocs.wincap", "-", why)))
    w("")
    w("# == standard binding inventory (non-optlist) ==")
    w("# name <TAB> guard <TAB> resolved startup value <TAB> classification"
      " <TAB> setter <TAB> saved <TAB> rationale")
    for name, val, cls, why in BINDINGS:
        w("\t".join(("bind.%s" % name.replace(" ", "_"), "always", val, cls,
                     "cmd.c / options.c", "-", why)))
    w("")
    w("# == symbol / appearance state (non-optlist) ==")
    w("# name <TAB> guard <TAB> resolved startup value <TAB> classification"
      " <TAB> setter <TAB> saved <TAB> rationale")
    for name, val, cls, why in SYMBOLS:
        w("\t".join(("sym.%s" % name.replace(" ", "_"), "always", val, cls,
                     "symbols.c", "-", why)))
    w("")
    w("# == delivery policy (non-optlist) ==")
    for name, val, why in [
        ("policy.render", "normal-ascii-color-v1", "fixed rendering profile"),
        ("policy.delivery", "llm-final-v1", "final durable deltas; no animation"),
        ("policy.audit", "not negotiated in Phase 2", "audit-frames-v1 is opt-in"),
        ("policy.runtime_mutation", "empty allowlist", "no runtime option changes"),
        ("policy.bones", "disabled", "no cross-episode contamination"),
        ("policy.wallclock_metadata", "absent", "no wall-clock/time metadata fields"),
    ]:
        w("\t".join(("pol.%s" % name.split(".")[1], "always", val,
                     "locked-gameplay", "-", "-", why)))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
