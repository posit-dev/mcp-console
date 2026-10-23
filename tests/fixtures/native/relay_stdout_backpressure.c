#ifdef __linux__
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_write)(int descriptor, const void *buffer, size_t length);
#define write native_write
static ssize_t (*native_writev)(int descriptor, const struct iovec *buffers, int count);
#define writev native_writev
#endif

static int blocked;
static bool monitored;
static atomic_bool observed = false;
static struct stat output_identity;

__attribute__((constructor)) static void initialize_checkpoint(void) {
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    if (native_write == NULL) _exit(90);
    native_writev = dlsym(RTLD_NEXT, "writev");
    if (native_writev == NULL) _exit(90);
#endif
#ifdef MCP_CONSOLE_BACKPRESSURE_SELECT
    if (!MCP_CONSOLE_BACKPRESSURE_SELECT()) return;
#endif
    monitored = true;
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    const char *path = getenv("MCP_CONSOLE_TEST_STDOUT_BLOCKED");
    blocked = path == NULL ? -1 : open(path, O_WRONLY | O_CLOEXEC);
    if (blocked < 0 || fstat(STDOUT_FILENO, &output_identity) < 0) {
        _exit(91);
    }
}

static bool is_stdout(int descriptor) {
    struct stat identity;
    return monitored && fstat(descriptor, &identity) == 0 &&
           identity.st_dev == output_identity.st_dev &&
           identity.st_ino == output_identity.st_ino;
}

static void notify_backpressure(void) {
    if (!atomic_exchange(&observed, true) && write(blocked, "1", 1) != 1) {
        _exit(92);
    }
}

// Probe with the real write and retain every successful short write. Once the
// pipe actually refuses bytes, restore blocking semantics before retrying the
// same write. A nonblocking relay receives the original EAGAIN unchanged.
static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
    if (!is_stdout(descriptor)) {
        return write(descriptor, buffer, length);
    }
    int flags = fcntl(descriptor, F_GETFL);
    if (flags < 0 || fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) < 0) {
        _exit(93);
    }
    ssize_t result = write(descriptor, buffer, length);
    int error = errno;
    if (fcntl(descriptor, F_SETFL, flags) < 0) {
        _exit(94);
    }
    if (result < 0 && (error == EAGAIN || error == EWOULDBLOCK)) {
        notify_backpressure();
        if (!(flags & O_NONBLOCK)) {
            return write(descriptor, buffer, length);
        }
    }
    errno = error;
    return result;
}

static ssize_t observed_writev(int descriptor, const struct iovec *buffers, int count) {
    if (!is_stdout(descriptor)) {
        return writev(descriptor, buffers, count);
    }
    int flags = fcntl(descriptor, F_GETFL);
    if (flags < 0 || fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) < 0) {
        _exit(95);
    }
    ssize_t result = writev(descriptor, buffers, count);
    int error = errno;
    if (fcntl(descriptor, F_SETFL, flags) < 0) {
        _exit(96);
    }
    if (result < 0 && (error == EAGAIN || error == EWOULDBLOCK)) {
        notify_backpressure();
        if (!(flags & O_NONBLOCK)) {
            return writev(descriptor, buffers, count);
        }
    }
    errno = error;
    return result;
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                              \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observed_write, write)
DYLD_INTERPOSE(observed_writev, writev)

#else
#undef write
ssize_t write(int descriptor, const void *buffer, size_t length) {
    return observed_write(descriptor, buffer, length);
}
#undef writev
ssize_t writev(int descriptor, const struct iovec *buffers, int count) {
    return observed_writev(descriptor, buffers, count);
}
#endif
