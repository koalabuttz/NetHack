/* agent_runner.c -- the trusted launch supervisor (Wave A / M1).
 *
 * This is a STANDALONE executable.  It is never linked with the game: it
 * includes only the engine-free handshake definition, so a bug here cannot
 * reach engine state and the game library cannot be probed through it.
 *
 * Trust model (plan section 4, "Launcher trust model"):
 *   - the worker executable and the data, config, sysconf and writable roots
 *     are trusted invocation parameters, never player frame fields;
 *   - public stdin/stdout carry JSON lines only;
 *   - the agent cannot cause this program to launch a worker with arbitrary
 *     inherited state.
 *
 * One invocation runs one episode: spawn, proxy, reap, clean, report.
 */

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>

#include "agent_handshake.h"

#define RUNNER_DIAG_CAP (1024u * 1024u) /* private diagnostic sink bound */
#define RUNNER_DEFAULT_DEADLINE 20 /* seconds before terminate then kill */
#define RUNNER_IO_BUF 65536
#define RUNNER_PATH_MAX 1024

/* Test-only fault injection for the all-or-error copy path.  When
 * NETHACK_AGENT_TEST_COPY_LIMIT is set in the LAUNCHER's environment (which
 * belongs to the trusted controller, never the worker), the copy fails after
 * that many bytes, simulating a short write / ENOSPC against a real
 * filesystem we cannot shrink.  It can only ever cause a failure, never a
 * bypass, so it is fail-closed by construction. */
static long r_copy_fault = -1;
static long r_copy_bytes = 0;
static int r_copy_failed = 0;

static const char closed_record[] = "{\"v\":1,\"ch\":\"control\","
                                    "\"type\":\"closed\"}";

struct runner_config {
    const char *worker;
    const char *data_root;
    const char *config_root;
    const char *sysconf;
    const char *private_root;
    const char *profile;
    const char *save_out;    /* copy the episode's save artifacts here */
    const char *restore_in;  /* seed the episode's save dir from here */
    int restore;
    int deadline;
};

static void
r_diag(const char *fmt, ...)
{
    va_list ap;

    va_start(ap, fmt);
    fputs("nethack-agent: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static void
usage(void)
{
    fputs("usage: nethack-agent --worker PATH [--data DIR] [--config DIR]\n"
          "       [--sysconf FILE] [--private-root DIR] [--profile NAME]\n"
          "       [--mode new|restore] [--restore-in DIR] [--save-out DIR]\n"
          "       [--deadline SECONDS]\n", stderr);
}

/* Recursively remove a tree we created, never following symlinks. */
static void
r_remove_tree(const char *path)
{
    DIR *d;
    struct dirent *e;

    d = opendir(path);
    if (d) {
        while ((e = readdir(d)) != NULL) {
            char child[RUNNER_PATH_MAX];
            struct stat st;

            if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
                continue;
            if ((size_t) snprintf(child, sizeof child, "%s/%s", path,
                                  e->d_name) >= sizeof child)
                continue;
            if (lstat(child, &st) != 0)
                continue;
            if (S_ISDIR(st.st_mode))
                r_remove_tree(child);
            else
                (void) unlink(child);
        }
        closedir(d);
    }
    (void) rmdir(path);
}

/* Copy the regular files of a directory tree from src into dst, creating dst
 * (mode 0700) as needed and never following symlinks.  Used to move a save
 * artifact between a finished episode root and the caller-owned directory the
 * trusted test controller provides: the launcher owns the transfer, so the
 * player-facing channel never carries a path or file bytes.
 *
 * ALL-OR-ERROR: every lstat/open/read/write/close and every recursive call is
 * checked, and a name that does not fit its buffer or a non-regular entry is
 * a refusal, not a silent skip.  A copy that returns 0 has reproduced the
 * whole tree; anything else is a failure the caller turns into a private
 * error.  `skip` names one entry to omit (the controller-owned metadata
 * record), or NULL. */
static int r_write_all(int, const char *, size_t);
static char *r_dir_join(char *buf, size_t cap, const char *a, const char *b);

/* The controller-owned provenance record and the helpers that build, write
 * and re-validate it (defined below, after the digest helpers). */
#define R_META_NAME "provenance.txt"
#define R_META_MAX 32768
static int r_sha256_file(const char *path, char hex[65]);
static char *r_meta_build(const struct runner_config *cfg,
                          const char *savename, const char *savedir,
                          size_t *outlen);
static int r_meta_write(const char *dir, const char *body, size_t len);
static const char *r_meta_get(const char *body, size_t len, const char *key,
                              char *out, size_t cap);
static int r_single_artifact(const char *dir, char *name, size_t cap);

static int
r_copy_tree(const char *src, const char *dst, const char *skip)
{
    DIR *d;
    struct dirent *e;
    struct stat st;
    int rc = 0;

    if (stat(src, &st) != 0 || !S_ISDIR(st.st_mode))
        return -1;
    if (mkdir(dst, 0700) != 0) {
        if (errno != EEXIST || stat(dst, &st) != 0 || !S_ISDIR(st.st_mode))
            return -1;
    }
    d = opendir(src);
    if (!d)
        return -1;
    while ((e = readdir(d)) != NULL) {
        char from[RUNNER_PATH_MAX], to[RUNNER_PATH_MAX];
        struct stat es;
        int n;

        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        if (skip && strcmp(e->d_name, skip) == 0)
            continue;
        n = snprintf(from, sizeof from, "%s/%s", src, e->d_name);
        if (n < 0 || (size_t) n >= sizeof from) {
            rc = -1;
            break;
        }
        n = snprintf(to, sizeof to, "%s/%s", dst, e->d_name);
        if (n < 0 || (size_t) n >= sizeof to) {
            rc = -1;
            break;
        }
        if (lstat(from, &es) != 0) {
            rc = -1;
            break;
        }
        if (S_ISDIR(es.st_mode)) {
            if (r_copy_tree(from, to, (const char *) 0) != 0) {
                rc = -1;
                break;
            }
        } else if (S_ISREG(es.st_mode)) {
            int in, out;
            char buf[65536];
            ssize_t got;

            in = open(from, O_RDONLY);
            if (in < 0) {
                rc = -1;
                break;
            }
            out = open(to, O_WRONLY | O_CREAT | O_TRUNC, 0600);
            if (out < 0) {
                (void) close(in);
                rc = -1;
                break;
            }
            got = 0;
            while ((got = read(in, buf, sizeof buf)) > 0) {
                if (r_copy_fault >= 0
                    && r_copy_bytes + (long) got > r_copy_fault) {
                    r_copy_failed = 1;
                    break;
                }
                r_copy_bytes += (long) got;
                if (r_write_all(out, buf, (size_t) got) != 0)
                    break;
            }
            if (got < 0 || r_copy_failed)
                rc = -1;
            /* A deferred write error (ENOSPC, EIO) surfaces only at close, so
             * an unchecked close is an unchecked copy. */
            if (close(in) != 0)
                rc = -1;
            if (close(out) != 0)
                rc = -1;
            if (rc != 0)
                break;
        } else {
            /* symlinks, fifos, devices: a save artifact is only ever regular
             * files, so anything else is a refusal */
            rc = -1;
            break;
        }
    }
    if (closedir(d) != 0)
        rc = -1;
    return rc;
}

/* fsync one path (file or directory). */
static int
r_fsync_path(const char *path)
{
    int fd = open(path, O_RDONLY);
    int rc;

    if (fd < 0)
        return -1;
    rc = fsync(fd);
    if (close(fd) != 0)
        rc = -1;
    return rc;
}

/* fsync every regular file and every directory under root. */
static int
r_fsync_tree(const char *root)
{
    DIR *d;
    struct dirent *e;
    int rc = 0;

    d = opendir(root);
    if (!d)
        return -1;
    while ((e = readdir(d)) != NULL) {
        char child[RUNNER_PATH_MAX];
        struct stat st;

        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        if (!r_dir_join(child, sizeof child, root, e->d_name)) {
            rc = -1;
            break;
        }
        if (lstat(child, &st) != 0) {
            rc = -1;
            break;
        }
        if (S_ISDIR(st.st_mode)) {
            if (r_fsync_tree(child) != 0) {
                rc = -1;
                break;
            }
        } else if (S_ISREG(st.st_mode)) {
            if (r_fsync_path(child) != 0) {
                rc = -1;
                break;
            }
        }
    }
    if (closedir(d) != 0)
        rc = -1;
    if (r_fsync_path(root) != 0)
        rc = -1;
    return rc;
}

/* A destination an export may replace: absent, or an existing, WRITABLE,
 * EMPTY directory.  Anything else -- a file, an unwritable directory, or a
 * directory that already holds an artifact -- is a refusal, so a save export
 * can never be mistaken for a merge and an unwritable controller directory is
 * a loud private failure rather than a silent replacement. */
static int
r_dest_ok(const char *dst)
{
    struct stat ds;
    DIR *d;
    struct dirent *e;
    int empty = 1;

    if (stat(dst, &ds) != 0)
        return (errno == ENOENT) ? 0 : -1;
    if (!S_ISDIR(ds.st_mode)) {
        r_diag("save export: destination exists and is not a directory");
        return -1;
    }
    if (access(dst, W_OK | X_OK) != 0) {
        r_diag("save export: destination is not writable");
        return -1;
    }
    d = opendir(dst);
    if (!d) {
        r_diag("save export: destination is not readable");
        return -1;
    }
    while ((e = readdir(d)) != NULL)
        if (strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0) {
            empty = 0;
            break;
        }
    if (closedir(d) != 0)
        return -1;
    if (!empty) {
        r_diag("save export: destination is not empty");
        return -1;
    }
    return 0;
}

/* Export the episode's save artifact into the controller-owned directory:
 * copy into a fresh sibling staging path, fsync it, then rename it into place
 * (an empty destination is replaced atomically).  Returns 0 only when the
 * artifact and its provenance record are durably at the destination. */
static int
r_export_tree(const struct runner_config *cfg, const char *src,
              const char *dst)
{
    char stage[RUNNER_PATH_MAX];
    char name[RUNNER_PATH_MAX];
    char *meta;
    size_t mtlen = 0;

    if (r_dest_ok(dst) != 0)
        return -1;
    r_copy_bytes = 0;
    r_copy_failed = 0;
    if ((size_t) snprintf(stage, sizeof stage, "%s.new.%ld", dst,
                          (long) getpid()) >= sizeof stage)
        return -1;
    /* a staging path left by an earlier aborted export */
    r_remove_tree(stage);
    if (r_copy_tree(src, stage, (const char *) 0) != 0) {
        r_remove_tree(stage);
        return -1;
    }
    if (r_single_artifact(stage, name, sizeof name) != 0) {
        r_diag("save export: the episode produced no single artifact");
        r_remove_tree(stage);
        return -1;
    }
    meta = r_meta_build(cfg, name, stage, &mtlen);
    if (!meta || r_meta_write(stage, meta, mtlen) != 0) {
        r_diag("save export: could not bind the artifact to its provenance");
        free(meta);
        r_remove_tree(stage);
        return -1;
    }
    free(meta);
    if (r_fsync_tree(stage) != 0) {
        r_diag("save export: could not flush the artifact to stable storage");
        r_remove_tree(stage);
        return -1;
    }
    if (rename(stage, dst) != 0) {
        r_diag("save export: could not move the artifact into place: %s",
               strerror(errno));
        r_remove_tree(stage);
        return -1;
    }
    /* make the rename itself durable */
    {
        char parent[RUNNER_PATH_MAX];
        char *slash = strrchr(dst, '/');
        size_t plen = slash ? (size_t) (slash - dst) : 0;

        if (plen > 0 && plen < sizeof parent) {
            memcpy(parent, dst, plen);
            parent[plen] = '\0';
            (void) r_fsync_path(parent);
        }
    }
    return 0;
}

/* Re-validate the controller-owned provenance metadata that must accompany a
 * restore artifact.  The metadata binds the artifact to the build, staged
 * immutable data, trusted sysconf, profile, producing mode, and owner scope
 * recorded when it was exported; every binding is recomputed here and a
 * mismatch, an absence, or an extra/missing artifact is a private failure
 * BEFORE any worker is launched.  This is not a substitute for the native
 * restored-flags validation; it is the per-save provenance half. */
static int
r_meta_verify(const struct runner_config *cfg, const char *dir,
              char *artifact, size_t cap)
{
    char path[RUNNER_PATH_MAX], full[RUNNER_PATH_MAX], want[65], got[65];
    char val[512];
    static const char *const shared[] = { "nhdat", "license", "symbols" };
    char body[R_META_MAX];
    size_t k;
    int fd, n;

    if (!r_dir_join(path, sizeof path, dir, R_META_NAME)) {
        r_diag("restore: metadata path too long");
        return -1;
    }
    fd = open(path, O_RDONLY);
    if (fd < 0) {
        r_diag("restore: the artifact carries no provenance metadata");
        return -1;
    }
    n = (int) read(fd, body, sizeof body - 1);
    if (n < 0 || close(fd) != 0 || n <= 0) {
        r_diag("restore: could not read the provenance metadata");
        return -1;
    }
    body[n] = '\0';

    if (!r_meta_get(body, (size_t) n, "version", val, sizeof val)
        || strcmp(val, "1") != 0) {
        r_diag("restore: unsupported provenance metadata version");
        return -1;
    }
    if (!r_meta_get(body, (size_t) n, "mode", val, sizeof val)
        || strcmp(val, "new") != 0) {
        r_diag("restore: the artifact was not produced by a new-game"
               " episode");
        return -1;
    }
    if (r_meta_get(body, (size_t) n, "owner-uid", val, sizeof val)) {
        if (strtoul(val, (char **) 0, 10) != (unsigned long) getuid()) {
            r_diag("restore: the artifact belongs to a different owner");
            return -1;
        }
    } else {
        r_diag("restore: metadata has no owner scope");
        return -1;
    }
    if (!r_meta_get(body, (size_t) n, "profile", val, sizeof val)
        || strcmp(val, cfg->profile) != 0) {
        r_diag("restore: the artifact was produced under a different"
               " profile");
        return -1;
    }
    if (r_sha256_file(cfg->worker, got) != 0
        || !r_meta_get(body, (size_t) n, "worker-sha256", want, sizeof want)
        || strcmp(want, got) != 0) {
        r_diag("restore: the worker does not match the artifact provenance");
        return -1;
    }
    for (k = 0; k < sizeof shared / sizeof shared[0]; ++k) {
        char key[64];

        if (!cfg->data_root)
            break;
        if (!r_dir_join(full, sizeof full, cfg->data_root, shared[k]))
            continue;
        (void) snprintf(key, sizeof key, "data-%s-sha256", shared[k]);
        if (!r_meta_get(body, (size_t) n, key, want, sizeof want))
            continue; /* data set was not bound at export time */
        if (r_sha256_file(full, got) != 0 || strcmp(want, got) != 0) {
            r_diag("restore: staged game data does not match the artifact"
                   " provenance");
            return -1;
        }
    }
    if (cfg->sysconf) {
        if (r_sha256_file(cfg->sysconf, got) != 0
            || !r_meta_get(body, (size_t) n, "sysconf-sha256", want,
                           sizeof want)
            || strcmp(want, got) != 0) {
            r_diag("restore: the trusted sysconf does not match the artifact"
                   " provenance");
            return -1;
        }
    }
    if (r_single_artifact(dir, artifact, cap) != 0) {
        r_diag("restore: the artifact directory does not hold exactly one"
               " save");
        return -1;
    }
    if (!r_meta_get(body, (size_t) n, "save-name", val, sizeof val)
        || strcmp(val, artifact) != 0) {
        r_diag("restore: the artifact name does not match its provenance");
        return -1;
    }
    if (!r_dir_join(full, sizeof full, dir, artifact)
        || r_sha256_file(full, got) != 0
        || !r_meta_get(body, (size_t) n, "save-sha256", want, sizeof want)
        || strcmp(want, got) != 0) {
        r_diag("restore: the save bytes do not match the artifact"
               " provenance");
        return -1;
    }
    return 0;
}

/* Write exactly len bytes to fd, retrying short writes. */
static int
r_write_all(int fd, const char *buf, size_t len)
{
    size_t off = 0;

    while (off < len) {
        ssize_t n = write(fd, buf + off, len - off);

        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        if (n == 0)
            return -1;
        off += (size_t) n;
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* content digests                                                      */
/* ------------------------------------------------------------------ */

/* SHA-256 (FIPS 180-4), self-contained: the launcher is a STANDALONE
 * executable and must not depend on a crypto library to bind a save artifact
 * to the trusted inputs it was produced with.  Used only for provenance. */
struct r_sha256_ctx {
    uint32_t h[8];
    uint64_t bits;
    unsigned char block[64];
    size_t used;
};

static const uint32_t r_sha256_k[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu,
    0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u,
    0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u,
    0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u,
    0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
    0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u,
    0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u, 0x1e376c08u,
    0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu,
    0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

static uint32_t
r_rotr(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32 - n));
}

static void
r_sha256_block(struct r_sha256_ctx *c, const unsigned char *p)
{
    uint32_t w[64], a, b, cc, d, e, f, g, h;
    int i;

    for (i = 0; i < 16; ++i)
        w[i] = ((uint32_t) p[i * 4] << 24) | ((uint32_t) p[i * 4 + 1] << 16)
               | ((uint32_t) p[i * 4 + 2] << 8) | (uint32_t) p[i * 4 + 3];
    for (i = 16; i < 64; ++i) {
        uint32_t s0 = r_rotr(w[i - 15], 7) ^ r_rotr(w[i - 15], 18)
                      ^ (w[i - 15] >> 3);
        uint32_t s1 = r_rotr(w[i - 2], 17) ^ r_rotr(w[i - 2], 19)
                      ^ (w[i - 2] >> 10);

        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = c->h[0]; b = c->h[1]; cc = c->h[2]; d = c->h[3];
    e = c->h[4]; f = c->h[5]; g = c->h[6]; h = c->h[7];
    for (i = 0; i < 64; ++i) {
        uint32_t s1 = r_rotr(e, 6) ^ r_rotr(e, 11) ^ r_rotr(e, 25);
        uint32_t ch = (e & f) ^ ((~e) & g);
        uint32_t t1 = h + s1 + ch + r_sha256_k[i] + w[i];
        uint32_t s0 = r_rotr(a, 2) ^ r_rotr(a, 13) ^ r_rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & cc) ^ (b & cc);
        uint32_t t2 = s0 + maj;

        h = g; g = f; f = e; e = d + t1;
        d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void
r_sha256_init(struct r_sha256_ctx *c)
{
    static const uint32_t iv[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u
    };

    memcpy(c->h, iv, sizeof iv);
    c->bits = 0;
    c->used = 0;
}

static void
r_sha256_update(struct r_sha256_ctx *c, const unsigned char *p, size_t len)
{
    c->bits += (uint64_t) len * 8u;
    while (len > 0) {
        size_t take = 64 - c->used;

        if (take > len)
            take = len;
        memcpy(c->block + c->used, p, take);
        c->used += take;
        p += take;
        len -= take;
        if (c->used == 64) {
            r_sha256_block(c, c->block);
            c->used = 0;
        }
    }
}

static void
r_sha256_final(struct r_sha256_ctx *c, unsigned char out[32])
{
    unsigned char pad[72];
    size_t padlen = (c->used < 56) ? 56 - c->used : 120 - c->used;
    uint64_t bits = c->bits;
    int i;

    memset(pad, 0, sizeof pad);
    pad[0] = 0x80;
    for (i = 0; i < 8; ++i)
        pad[padlen + i] = (unsigned char) (bits >> (56 - 8 * i));
    r_sha256_update(c, pad, padlen + 8);
    for (i = 0; i < 8; ++i) {
        out[i * 4] = (unsigned char) (c->h[i] >> 24);
        out[i * 4 + 1] = (unsigned char) (c->h[i] >> 16);
        out[i * 4 + 2] = (unsigned char) (c->h[i] >> 8);
        out[i * 4 + 3] = (unsigned char) c->h[i];
    }
}

/* SHA-256 of a regular file, written as a lowercase hex string into `hex`
 * (65 bytes).  Returns 0 on success, -1 on any error: an unreadable input is
 * a failed binding, never a skipped check. */
static int
r_sha256_file(const char *path, char hex[65])
{
    struct r_sha256_ctx c;
    unsigned char buf[65536], dig[32];
    static const char digits[] = "0123456789abcdef";
    ssize_t n;
    int fd, i;

    fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    r_sha256_init(&c);
    while ((n = read(fd, buf, sizeof buf)) > 0)
        r_sha256_update(&c, buf, (size_t) n);
    if (n < 0 || close(fd) != 0)
        return -1;
    r_sha256_final(&c, dig);
    for (i = 0; i < 32; ++i) {
        hex[i * 2] = digits[dig[i] >> 4];
        hex[i * 2 + 1] = digits[dig[i] & 0xf];
    }
    hex[64] = '\0';
    return 0;
}

/* ------------------------------------------------------------------ */
/* save provenance metadata (controller-owned, off the player channel)  */
/* ------------------------------------------------------------------ */

/* A canonical, sorted key=value record.  It lives in the controller-owned
 * artifact directory the trusted test controller passes in; it never touches
 * the player-facing channel and the worker never sees it. */
struct r_meta {
    char *buf;
    size_t len;
};

static int
r_meta_add(struct r_meta *m, const char *key, const char *val)
{
    int n = snprintf(m->buf + m->len, R_META_MAX - m->len, "%s=%s\n",
                     key, val);

    if (n < 0 || (size_t) n >= R_META_MAX - m->len)
        return -1;
    m->len += (size_t) n;
    return 0;
}

static int
r_meta_digest(struct r_meta *m, const char *key, const char *path)
{
    char hex[65];

    if (r_sha256_file(path, hex) != 0)
        return -1;
    return r_meta_add(m, key, hex);
}

/* Look up key in the parsed metadata; returns the value or NULL. */
static const char *
r_meta_get(const char *body, size_t len, const char *key, char *out,
           size_t cap)
{
    size_t klen = strlen(key);
    const char *p = body, *end = body + len;

    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t) (end - p));
        size_t linelen = nl ? (size_t) (nl - p) : (size_t) (end - p);

        if (linelen > klen + 1 && p[klen] == '='
            && memcmp(p, key, klen) == 0) {
            size_t vlen = linelen - klen - 1;

            if (vlen >= cap)
                return (const char *) 0;
            memcpy(out, p + klen + 1, vlen);
            out[vlen] = '\0';
            return out;
        }
        p += linelen + (nl ? 1 : 0);
    }
    return (const char *) 0;
}

/* Build the metadata for an exported artifact: digests of the save bytes, the
 * worker binary, each staged immutable data file, the trusted sysconf, plus
 * the profile, the producing mode, and the owner scope.  Returns the buffer
 * (caller frees) or NULL. */
static char *
r_meta_build(const struct runner_config *cfg, const char *savename,
             const char *savedir, size_t *outlen)
{
    char *buf = (char *) calloc(1, R_META_MAX);
    struct r_meta m;
    char full[RUNNER_PATH_MAX], hex[65];
    static const char *const shared[] = { "nhdat", "license", "symbols" };
    size_t k;

    if (!buf)
        return (char *) 0;
    m.buf = buf;
    m.len = 0;

    if (r_meta_add(&m, "version", "1") != 0
        || r_meta_add(&m, "mode", "new") != 0
        || r_meta_add(&m, "profile", cfg->profile) != 0)
        goto bad;
    (void) snprintf(hex, sizeof hex, "%lu", (unsigned long) getuid());
    if (r_meta_add(&m, "owner-uid", hex) != 0)
        goto bad;

    if (r_meta_digest(&m, "worker-sha256", cfg->worker) != 0)
        goto bad;
    for (k = 0; k < sizeof shared / sizeof shared[0]; ++k) {
        char key[64];

        if (!cfg->data_root)
            break;
        if (!r_dir_join(full, sizeof full, cfg->data_root, shared[k]))
            goto bad;
        (void) snprintf(key, sizeof key, "data-%s-sha256", shared[k]);
        if (access(full, R_OK) == 0
            && r_meta_digest(&m, key, full) != 0)
            goto bad;
    }
    if (cfg->sysconf && r_meta_digest(&m, "sysconf-sha256",
                                      cfg->sysconf) != 0)
        goto bad;
    if (r_meta_add(&m, "save-name", savename) != 0)
        goto bad;
    if (!r_dir_join(full, sizeof full, savedir, savename))
        goto bad;
    if (r_meta_digest(&m, "save-sha256", full) != 0)
        goto bad;

    *outlen = m.len;
    return buf;
bad:
    free(buf);
    return (char *) 0;
}

/* Write the metadata and fsync it, so a reader never sees a partial
 * record. */
static int
r_meta_write(const char *dir, const char *body, size_t len)
{
    char path[RUNNER_PATH_MAX];
    int fd, rc = 0;

    if (!r_dir_join(path, sizeof path, dir, R_META_NAME))
        return -1;
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0)
        return -1;
    if (r_write_all(fd, body, len) != 0)
        rc = -1;
    if (fsync(fd) != 0)
        rc = -1;
    if (close(fd) != 0)
        rc = -1;
    return rc;
}

/* Find the single regular file in a directory other than the metadata record,
 * i.e. the save artifact.  Returns -1 unless exactly one is present. */
static int
r_single_artifact(const char *dir, char *name, size_t cap)
{
    DIR *d = opendir(dir);
    struct dirent *e;
    int found = 0;

    if (!d)
        return -1;
    while ((e = readdir(d)) != NULL) {
        struct stat st;
        char full[RUNNER_PATH_MAX];

        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        if (strcmp(e->d_name, R_META_NAME) == 0)
            continue;
        if (!r_dir_join(full, sizeof full, dir, e->d_name))
            continue;
        if (lstat(full, &st) != 0 || !S_ISREG(st.st_mode))
            continue;
        if (found || strlen(e->d_name) >= cap) {
            (void) closedir(d);
            return -1;
        }
        strcpy(name, e->d_name);
        found = 1;
    }
    if (closedir(d) != 0)
        return -1;
    return found ? 0 : -1;
}

/* Terminate a worker and every process it started.
 *
 * The worker calls setsid(), so its process group is its own pid.  Killing
 * the NEGATIVE pid reaches the whole group, so a descendant (save
 * compression, panic tracer) is cleaned up with the leader instead of being
 * orphaned.  If the group does not exist yet (the setsid() race) or setsid()
 * failed, the negative-pid kill reports ESRCH and the leader alone is
 * signalled. */
static void
r_kill_tree(pid_t pid, int sig)
{
    if (pid <= 0)
        return;
    if (kill(-pid, sig) != 0)
        (void) kill(pid, sig);
}

/* Close every descriptor above 2 except the ones we own. */
static void
r_close_all(int keep1, int keep2)
{
    long maxfd = sysconf(_SC_OPEN_MAX);
    int fd;

    if (maxfd < 0 || maxfd > 65536)
        maxfd = 65536;
    for (fd = 3; fd < maxfd; ++fd) {
        if (fd == keep1 || fd == keep2)
            continue;
        (void) close(fd);
    }
}

/* Bounded copy that always terminates and never trips -Wformat-truncation. */
static void
r_copy_bounded(char *dst, size_t cap, const char *src)
{
    size_t n;

    if (!dst || cap == 0)
        return;
    if (!src)
        src = "";
    n = strlen(src);
    if (n > cap - 1)
        n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static char *
r_dir_join(char *buf, size_t cap, const char *a, const char *b)
{
    if ((size_t) snprintf(buf, cap, "%s/%s", a, b) >= cap)
        return (char *) 0;
    return buf;
}

int
main(int argc, char **argv)
{
    struct runner_config cfg;
    char workdir[RUNNER_PATH_MAX];
    char savedir[RUNNER_PATH_MAX], diagdir[RUNNER_PATH_MAX];
    char diagpath[RUNNER_PATH_MAX], homedir[RUNNER_PATH_MAX];
    char sockpath_arg[64];
    int sv[2];
    pid_t child;
    struct agent_handshake hs;
    int status = 0, i;
    time_t started;
    int child_status = 0;
    int stdin_open = 1;
    int handshake_sent = 0;

    /* Suppress SIGPIPE for the whole run.  A worker that exits mid-write, or
     * a consumer that closes our stdout, must surface as an ordinary write
     * error (EPIPE) rather than a signal that kills this process before the
     * cleanup path can reap the worker and remove the episode tree. */
    (void) signal(SIGPIPE, SIG_IGN);

    /* Test-only: a copy fault injection limit for the export path (see
     * r_copy_fault).  Read once, from the launcher's own environment. */
    {
        const char *fl = getenv("NETHACK_AGENT_TEST_COPY_LIMIT");

        if (fl && *fl)
            r_copy_fault = (long) strtol(fl, (char **) 0, 10);
    }

    memset(&cfg, 0, sizeof cfg);
    cfg.profile = "normal-ascii-color-v1";
    cfg.deadline = RUNNER_DEFAULT_DEADLINE;

    for (i = 1; i < argc; ++i) {
        const char *a = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;

#define TAKE(opt, field)                                                     \
    if (strcmp(a, opt) == 0) {                                               \
        if (!v) {                                                            \
            r_diag("missing value for %s", opt);                       \
            return 2;                                                        \
        }                                                                    \
        cfg.field = v;                                                       \
        ++i;                                                                 \
        continue;                                                            \
    }
        TAKE("--worker", worker)
        TAKE("--data", data_root)
        TAKE("--config", config_root)
        TAKE("--sysconf", sysconf)
        TAKE("--private-root", private_root)
        TAKE("--profile", profile)
        TAKE("--save-out", save_out)
        TAKE("--restore-in", restore_in)
#undef TAKE
        if (strcmp(a, "--mode") == 0 && v) {
            if (strcmp(v, "new") == 0)
                cfg.restore = 0;
            else if (strcmp(v, "restore") == 0)
                cfg.restore = 1;
            else {
                r_diag("unknown mode %s", v);
                return 2;
            }
            ++i;
            continue;
        }
        if (strcmp(a, "--deadline") == 0 && v) {
            cfg.deadline = atoi(v);
            ++i;
            continue;
        }
        r_diag("unknown option %s", a);
        usage();
        return 2;
    }
    if (!cfg.worker) {
        usage();
        return 2;
    }
    if (!cfg.private_root) {
        r_diag("--private-root is required");
        return 2;
    }

    /* Refuse setuid execution: this program must never run with credentials
     * its caller did not have. */
    if (getuid() != geteuid() || getgid() != getegid()) {
        r_diag("refusing to run with elevated credentials");
        return 3;
    }

    if ((size_t) snprintf(workdir, sizeof workdir, "%s/episode.XXXXXX",
                          cfg.private_root) >= sizeof workdir) {
        r_diag("private root path too long");
        return 2;
    }
    if (!mkdtemp(workdir)) {
        r_diag("mkdtemp failed: %s", strerror(errno));
        return 4;
    }
    (void) chmod(workdir, 0700);
    if (!r_dir_join(savedir, sizeof savedir, workdir, "save")
        || !r_dir_join(diagdir, sizeof diagdir, workdir, "diag")
        || !r_dir_join(homedir, sizeof homedir, workdir, "home")) {
        r_diag("private path too long");
        r_remove_tree(workdir);
        return 4;
    }
    if (mkdir(savedir, 0700) != 0 || mkdir(diagdir, 0700) != 0
        || mkdir(homedir, 0700) != 0) {
        r_diag("cannot create private directories: %s", strerror(errno));
        r_remove_tree(workdir);
        return 4;
    }
    if (!r_dir_join(diagpath, sizeof diagpath, diagdir, "worker.log")) {
        r_diag("private path too long");
        r_remove_tree(workdir);
        return 4;
    }

    /* Restore: the artifact must carry controller-owned provenance metadata,
     * re-validated here against the build, staged data, trusted sysconf,
     * profile, mode and owner scope before the episode root is seeded or any
     * worker starts.  A missing or mismatched binding is a private failure.
     * The launcher owns the transfer and the handshake names no file, only
     * that a restore is in progress. */
    if (cfg.restore_in) {
        struct stat rs;
        char artifact[RUNNER_PATH_MAX];

        if (stat(cfg.restore_in, &rs) != 0 || !S_ISDIR(rs.st_mode)) {
            r_diag("restore source is missing or not a directory");
            r_remove_tree(workdir);
            return 7;
        }
        if (r_meta_verify(&cfg, cfg.restore_in, artifact,
                          sizeof artifact) != 0) {
            r_diag("restore artifact failed provenance validation");
            r_remove_tree(workdir);
            return 7;
        }
        if (r_copy_tree(cfg.restore_in, savedir, R_META_NAME) != 0) {
            r_diag("could not seed the episode save directory");
            r_remove_tree(workdir);
            return 7;
        }
    }

    /* The private episode root is the worker's playground.  Immutable game
     * data is shared into it by symlink (never copied per episode, never a
     * writable file shared out), and the writable files the game expects are
     * created here, so nothing is written into the staged data root. */
    if (cfg.data_root) {
        static const char *const shared[] = { "nhdat", "license", "symbols" };
        size_t k;

        for (k = 0; k < sizeof shared / sizeof shared[0]; ++k) {
            char src[RUNNER_PATH_MAX], dst[RUNNER_PATH_MAX];

            if (!r_dir_join(src, sizeof src, cfg.data_root, shared[k])
                || !r_dir_join(dst, sizeof dst, workdir, shared[k])) {
                r_diag("private path too long");
                r_remove_tree(workdir);
                return 4;
            }
            /* Sharing immutable game data is required, not best effort: a
             * missing or unshareable file would silently start an incomplete
             * episode tree, so it fails closed instead. */
            if (access(src, R_OK) != 0) {
                r_diag("required immutable data file is unreadable: %s", src);
                r_remove_tree(workdir);
                return 7;
            }
            if (symlink(src, dst) != 0 && errno != EEXIST) {
                r_diag("cannot share immutable data file: %s", dst);
                r_remove_tree(workdir);
                return 7;
            }
        }
    }
    {
        char permfile[RUNNER_PATH_MAX];
        int pfd;

        if (r_dir_join(permfile, sizeof permfile, workdir, "perm")) {
            pfd = open(permfile, O_WRONLY | O_CREAT | O_EXCL, 0600);
            if (pfd >= 0)
                (void) close(pfd);
        }
    }

    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
        r_diag("socketpair failed: %s", strerror(errno));
        r_remove_tree(workdir);
        return 5;
    }

    child = fork();
    if (child < 0) {
        r_diag("fork failed: %s", strerror(errno));
        (void) close(sv[0]);
        (void) close(sv[1]);
        r_remove_tree(workdir);
        return 5;
    }

    if (child == 0) {
        /* ---- worker side ---- */
        int devnull, diagfd, flags;
        char *wargv[4];
        char *wenv[8];
        int nenv = 0;

        (void) chdir(workdir);
        /* eliminate any controlling terminal */
        if (setsid() < 0)
            _exit(60);

        devnull = open("/dev/null", O_RDONLY);
        if (devnull < 0)
            _exit(61);
        if (dup2(devnull, STDIN_FILENO) < 0)
            _exit(61);
        if (devnull > 2)
            (void) close(devnull);

        diagfd = open(diagpath, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (diagfd < 0)
            _exit(61);
        if (dup2(diagfd, STDOUT_FILENO) < 0
            || dup2(diagfd, STDERR_FILENO) < 0)
            _exit(61);
        if (diagfd > 2)
            (void) close(diagfd);

        (void) close(sv[0]);
        /* everything except the transport descriptor is closed */
        r_close_all(sv[1], -1);
        /* The handshake must cross execve, so FD_CLOEXEC is cleared here.
         * The worker sets it again (agent_bootstrap.c) once the handshake has
         * been consumed, so no later descendant inherits the channel.  Both
         * fcntl calls are checked: an unverifiable descriptor state is not a
         * launch. */
        flags = fcntl(sv[1], F_GETFD);
        if (flags < 0)
            _exit(63);
        if (fcntl(sv[1], F_SETFD, flags & ~FD_CLOEXEC) < 0)
            _exit(63);

        /* Hard bound the private diagnostic sink.  RLIMIT_FSIZE is enforced
         * by the kernel on the very next write, so the worker cannot
         * overshoot the cap between the supervisor's polls; the supervisor's
         * polling check below remains only as a backstop. */
        {
            struct rlimit rl;

            rl.rlim_cur = (rlim_t) RUNNER_DIAG_CAP;
            rl.rlim_max = (rlim_t) RUNNER_DIAG_CAP;
            (void) setrlimit(RLIMIT_FSIZE, &rl);
        }

        (void) snprintf(sockpath_arg, sizeof sockpath_arg, "--agent-fd=%d",
                        sv[1]);
        wargv[0] = (char *) cfg.worker;
        wargv[1] = sockpath_arg;
        wargv[2] = (char *) 0;
        wargv[3] = (char *) 0;

        /* A constructed environment, not a filtered one. */
        wenv[nenv++] = (char *) "PATH=/usr/bin:/bin";
        wenv[nenv++] = (char *) "LANG=C.UTF-8";
        wenv[nenv++] = (char *) "LC_ALL=C.UTF-8";
        wenv[nenv++] = (char *) "USER=agent";
        wenv[nenv++] = (char *) "LOGNAME=agent";
        {
            static char home_env[RUNNER_PATH_MAX + 8];

            (void) snprintf(home_env, sizeof home_env, "HOME=%s", homedir);
            wenv[nenv++] = home_env;
        }
        wenv[nenv] = (char *) 0;

        (void) execve(cfg.worker, wargv, wenv);
        _exit(62); /* exec failed */
    }

    /* ---- supervisor side ---- */
    (void) close(sv[1]);

    memset(&hs, 0, sizeof hs);
    hs.magic = AG_HS_MAGIC;
    hs.version = AG_HS_VERSION;
    hs.mode = cfg.restore ? AG_HS_MODE_RESTORE : AG_HS_MODE_NEW;
    hs.reserved = 0u;
    r_copy_bounded(hs.profile, sizeof hs.profile, cfg.profile);
    r_copy_bounded(hs.data_root, sizeof hs.data_root, cfg.data_root);
    r_copy_bounded(hs.config_root, sizeof hs.config_root, cfg.config_root);
    r_copy_bounded(hs.writable_root, sizeof hs.writable_root, workdir);
    r_copy_bounded(hs.sysconf_path, sizeof hs.sysconf_path, cfg.sysconf);
    if (r_write_all(sv[0], (const char *) &hs, sizeof hs) != 0) {
        r_diag("could not send the bootstrap handshake");
        status = 6;
        goto cleanup;
    }
    handshake_sent = 1;

    started = time((time_t *) 0);
    for (;;) {
        struct pollfd pfd[2];
        int nfd = 0;
        int i_stdin = -1, i_sock = -1;
        int rc;

        if (stdin_open) {
            i_stdin = nfd;
            pfd[nfd].fd = STDIN_FILENO;
            pfd[nfd].events = POLLIN;
            ++nfd;
        }
        i_sock = nfd;
        pfd[nfd].fd = sv[0];
        pfd[nfd].events = POLLIN;
        ++nfd;

        rc = poll(pfd, nfd, 250);
        if (rc < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        if (stdin_open && pfd[i_stdin].revents & (POLLIN | POLLHUP)) {
            char buf[RUNNER_IO_BUF];
            ssize_t n = read(STDIN_FILENO, buf, sizeof buf);

            if (n > 0) {
                if (r_write_all(sv[0], buf, (size_t) n) != 0)
                    break;
            } else {
                (void) shutdown(sv[0], SHUT_WR);
                stdin_open = 0;
            }
        }
        if (pfd[i_sock].revents & (POLLIN | POLLHUP)) {
            char buf[RUNNER_IO_BUF];
            ssize_t n = read(sv[0], buf, sizeof buf);

            if (n <= 0)
                break;
            if (r_write_all(STDOUT_FILENO, buf, (size_t) n) != 0)
                break;
        }
        /* bound the private diagnostic sink */
        {
            struct stat ds;

            if (stat(diagpath, &ds) == 0
                && (unsigned long) ds.st_size > RUNNER_DIAG_CAP) {
                r_diag("worker diagnostic sink exceeded its bound");
                break;
            }
        }
        if (time((time_t *) 0) - started > cfg.deadline) {
            r_diag("worker deadline expired");
            break;
        }
    }
    /* ---- ONE cleanup path for every post-fork outcome ----
     * Reached by falling out of the loop (transport end, write failure
     * including EPIPE, diagnostic bound, or deadline) and by a handshake
     * failure.  It closes the transport, terminates the worker's whole
     * process group, reaps the worker, and removes only the tree this runner
     * created. */
cleanup:
    (void) close(sv[0]);
    r_kill_tree(child, SIGTERM);
    for (i = 0; i < 40; ++i) {
        pid_t w = waitpid(child, &child_status, WNOHANG);

        if (w == child)
            break;
        if (w < 0 && errno != EINTR)
            break;
        if (i == 25)
            r_kill_tree(child, SIGKILL);
        {
            struct timespec ts;

            ts.tv_sec = 0;
            ts.tv_nsec = 50 * 1000 * 1000;
            (void) nanosleep(&ts, (struct timespec *) 0);
        }
    }
    {
        pid_t w;

        do {
            w = waitpid(child, &child_status, 0);
        } while (w < 0 && errno == EINTR);
    }

    /* Save: the worker produced its native save artifact under the episode's
     * save directory.  The launcher copies it into the caller-owned directory
     * the trusted test controller provides, together with the provenance
     * record that binds it to the build, staged data, profile and owner
     * scope;
     * the artifact is retrievable without exposing a path or bytes on the
     * player channel.
     *
     * Export is confirmed BEFORE the private root is removed and before the
     * episode is reported complete.  On failure the root is kept and the
     * launcher exits nonzero privately rather than presenting a bare
     * closure that would claim an artifact it never produced. */
    if (handshake_sent && cfg.save_out && status == 0
        && r_export_tree(&cfg, savedir, cfg.save_out) != 0) {
        r_diag("save export failed; the private episode root is retained");
        return 8;
    }

    /* Terminal closure is best effort: if the consumer already closed our
     * stdout this write fails with EPIPE and the episode simply ends without
     * it, but the cleanup above still ran.  The launcher alone reports
     * terminal closure to the public channel. */
    if (r_write_all(STDOUT_FILENO, closed_record,
                    sizeof closed_record - 1) == 0)
        (void) r_write_all(STDOUT_FILENO, "\n", 1);

    /* remove only the tree we created */
    r_remove_tree(workdir);
    return status;
}
