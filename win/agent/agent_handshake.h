/* agent_handshake.h -- the trusted bootstrap handshake wire format.
 *
 * Engine-free: this header is included by both the trusted launcher
 * (sys/unix/agent_runner.c, a standalone executable that is NOT linked with
 * the game) and the worker's bootstrap (win/agent/agent_bootstrap.c), so it
 * may only use <stdint.h> and <stddef.h>.
 *
 * The handshake is a single fixed-size binary record sent by the launcher
 * the inherited descriptor before any player-facing byte.  It is deliberately
 * not JSON: no player-supplied data is ever reused as handshake material, and
 * the record is bounded well below the 4096-byte contract limit.
 */

#ifndef AGENT_HANDSHAKE_H
#define AGENT_HANDSHAKE_H

#include <stdint.h>
#include <stddef.h>

#define AG_HS_MAGIC 0x4741484eu /* "NHAG" in little-endian byte order */
#define AG_HS_VERSION 1u
#define AG_HS_MAX_BYTES 4096u

/* launch mode */
#define AG_HS_MODE_NEW 0u
#define AG_HS_MODE_RESTORE 1u

#define AG_HS_PROFILE_MAX 64
#define AG_HS_ROOT_MAX 512

struct agent_handshake {
    uint32_t magic;      /* AG_HS_MAGIC */
    uint32_t version;    /* AG_HS_VERSION */
    uint32_t mode;       /* AG_HS_MODE_* */
    uint32_t reserved;   /* must be zero */
    char profile[AG_HS_PROFILE_MAX];      /* e.g. "normal-ascii-color-v1" */
    char data_root[AG_HS_ROOT_MAX];       /* immutable game data */
    char config_root[AG_HS_ROOT_MAX];     /* trusted system config */
    char writable_root[AG_HS_ROOT_MAX];   /* private per-episode root */
    char sysconf_path[AG_HS_ROOT_MAX];    /* trusted sysconf file */
};

/* The handshake must fit the documented bound with room to spare. */
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(struct agent_handshake) <= AG_HS_MAX_BYTES,
               "agent handshake exceeds its documented bound");
#endif

#endif /* AGENT_HANDSHAKE_H */
