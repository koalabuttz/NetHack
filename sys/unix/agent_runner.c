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
 * player-facing channel never carries a path or file bytes. */
static int r_write_all(int, const char *, size_t);

static int
r_copy_tree(const char *src, const char *dst)
{
    DIR *d;
    struct dirent *e;
    struct stat st;

    if (stat(src, &st) != 0 || !S_ISDIR(st.st_mode))
        return -1;
    if (mkdir(dst, 0700) != 0 && errno != EEXIST)
        return -1;
    d = opendir(src);
    if (!d)
        return -1;
    while ((e = readdir(d)) != NULL) {
        char from[RUNNER_PATH_MAX], to[RUNNER_PATH_MAX];
        struct stat es;

        if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
            continue;
        if ((size_t) snprintf(from, sizeof from, "%s/%s", src, e->d_name)
                >= sizeof from
            || (size_t) snprintf(to, sizeof to, "%s/%s", dst, e->d_name)
                   >= sizeof to)
            continue;
        if (lstat(from, &es) != 0)
            continue;
        if (S_ISDIR(es.st_mode))
            (void) r_copy_tree(from, to);
        else if (S_ISREG(es.st_mode)) {
            int in = open(from, O_RDONLY), out;
            char buf[65536];
            ssize_t n;

            if (in < 0)
                continue;
            out = open(to, O_WRONLY | O_CREAT | O_TRUNC, 0600);
            if (out < 0) {
                (void) close(in);
                continue;
            }
            while ((n = read(in, buf, sizeof buf)) > 0) {
                if (r_write_all(out, buf, (size_t) n) != 0)
                    break;
            }
            (void) close(in);
            (void) close(out);
        }
    }
    closedir(d);
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

    /* Suppress SIGPIPE for the whole run.  A worker that exits mid-write, or
     * a consumer that closes our stdout, must surface as an ordinary write
     * error (EPIPE) rather than a signal that kills this process before the
     * cleanup path can reap the worker and remove the episode tree. */
    (void) signal(SIGPIPE, SIG_IGN);

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

    /* Restore: seed the episode's save directory from the controller-owned
     * directory before the worker starts.  The launcher owns the transfer and
     * the handshake names no file, only that a restore is in progress. */
    if (cfg.restore_in) {
        struct stat rs;

        if (stat(cfg.restore_in, &rs) != 0 || !S_ISDIR(rs.st_mode)) {
            r_diag("restore source is missing or not a directory");
            r_remove_tree(workdir);
            return 7;
        }
        if (r_copy_tree(cfg.restore_in, savedir) != 0) {
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

    /* Terminal closure is best effort: if the consumer already closed our
     * stdout this write fails with EPIPE and the episode simply ends without
     * it, but the cleanup above still ran.  The launcher alone reports
     * terminal closure to the public channel. */
    if (r_write_all(STDOUT_FILENO, closed_record,
                    sizeof closed_record - 1) == 0)
        (void) r_write_all(STDOUT_FILENO, "\n", 1);

    /* Save: the worker produced its native save artifact under the episode's
     * save directory.  The launcher copies it into the caller-owned directory
     * the trusted test controller provides, so the artifact is retrievable
     * without exposing a path or bytes on the player channel; the episode
     * root is then removed exactly as for every other mode. */
    if (cfg.save_out)
        (void) r_copy_tree(savedir, cfg.save_out);

    /* remove only the tree we created */
    r_remove_tree(workdir);
    return status;
}
