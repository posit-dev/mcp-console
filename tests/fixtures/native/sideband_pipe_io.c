#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_write)(int, const void *, size_t);
static ssize_t (*native_read)(int, void *, size_t);
static int (*native_poll)(struct pollfd *, nfds_t, int);
#define write native_write
#define read native_read
#define poll native_poll
#endif

static atomic_int writer = -1;
static atomic_int reader = -1;
static atomic_int write_step = 0;
static atomic_int read_step = 0;
static atomic_bool poll_interrupted = false;
static atomic_bool blocked = false;
static bool faults;

static void mark(const char *name, bool checkpoint) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/%s", getenv("TMPDIR"), name);
    int fd = open(path, O_WRONLY | O_NONBLOCK | O_CLOEXEC |
                        (checkpoint ? 0 : O_CREAT), 0600);
    if (fd < 0) _exit(90);
    if (checkpoint && write(fd, "1", 1) != 1) _exit(91);
    close(fd);
}

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    native_read = dlsym(RTLD_NEXT, "read");
    native_poll = dlsym(RTLD_NEXT, "poll");
    if (!native_write || !native_read || !native_poll) _exit(92);
#endif
    const char *value = getenv("MCP_CONSOLE_TEST_PIPE_FAULTS");
    faults = value != NULL && strcmp(value, "1") == 0;
}

static bool pipe_descriptor(int fd) {
    struct stat status;
    return fd > 2 && fstat(fd, &status) == 0 && S_ISFIFO(status.st_mode);
}

static ssize_t observed_write(int fd, const void *buffer, size_t length) {
    const char prefix[] = "{\"kind\":\"evaluate\"";
    if (pipe_descriptor(fd) && length >= sizeof(prefix) - 1 &&
        memcmp(buffer, prefix, sizeof(prefix) - 1) == 0) {
        atomic_store(&writer, fd);
    }
    if (fd != atomic_load(&writer)) return write(fd, buffer, length);
    if (!(fcntl(fd, F_GETFL) & O_NONBLOCK)) _exit(93);
    if (faults) {
        int step = atomic_fetch_add(&write_step, 1);
        if (step < 2) {
            mark(step == 0 ? "write-eintr" : "write-eagain", false);
            errno = step == 0 ? EINTR : EAGAIN;
            return -1;
        }
        if (length > 1021) {
            length = 1021;
            if (step == 2) mark("short-write", false);
        }
    }
    ssize_t result = write(fd, buffer, length);
    int error = errno;
    if (result < 0 && error == EAGAIN && !atomic_exchange(&blocked, true)) {
        mark("writer-blocked", true);
    }
    errno = error;
    return result;
}

static ssize_t observed_read(int fd, void *buffer, size_t length) {
    if (faults && fd == atomic_load(&reader)) {
        int step = atomic_fetch_add(&read_step, 1);
        if (step < 2) {
            mark(step == 0 ? "read-eintr" : "read-eagain", false);
            errno = step == 0 ? EINTR : EAGAIN;
            return -1;
        }
        if (length > 1021) length = 1021;
    }
    ssize_t result = read(fd, buffer, length);
    const char ready[] = "{\"kind\":\"ready\"}\n";
    if (pipe_descriptor(fd) && result >= (ssize_t)(sizeof(ready) - 1) &&
        memcmp(buffer, ready, sizeof(ready) - 1) == 0) {
        atomic_store(&reader, fd);
    }
    return result;
}

static int observed_poll(struct pollfd *fds, nfds_t count, int timeout) {
    if (count > 0 && fds[0].fd == atomic_load(&writer)) {
        struct pollfd probe = fds[0];
        if (poll(&probe, 1, 0) == 0 && !atomic_exchange(&blocked, true)) {
            mark("writer-blocked", true);
        }
    }
    if (faults && count > 0 && fds[0].fd == atomic_load(&reader) &&
        !atomic_exchange(&poll_interrupted, true)) {
        mark("poll-eintr", false);
        errno = EINTR;
        return -1;
    }
    return poll(fds, count, timeout);
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                              \
    __attribute__((used)) static struct {                                  \
        const void *replacement;                                          \
        const void *replacee;                                              \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                             \
        (const void *)(uintptr_t)&replacee};
DYLD_INTERPOSE(observed_write, write)
DYLD_INTERPOSE(observed_read, read)
DYLD_INTERPOSE(observed_poll, poll)
#else
#undef write
#undef read
#undef poll
ssize_t write(int fd, const void *buffer, size_t length) {
    return observed_write(fd, buffer, length);
}
ssize_t read(int fd, void *buffer, size_t length) {
    return observed_read(fd, buffer, length);
}
int poll(struct pollfd *fds, nfds_t count, int timeout) {
    return observed_poll(fds, count, timeout);
}
#endif
