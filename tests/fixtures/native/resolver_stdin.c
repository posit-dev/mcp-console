#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_int input = -1;
static atomic_bool blocked;

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t native_write(int fd, const void *buffer, size_t length) {
#ifdef __APPLE__
    return write(fd, buffer, length);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(fd, buffer, length);
#endif
}

static ssize_t observe_write(int fd, const void *buffer, size_t length) {
    if (length >= 14 && memcmp(buffer, "{\"extensions\":", 14) == 0) input = fd;
    if (fd != input) return native_write(fd, buffer, length);
    // Observe real backpressure even on the old blocking writer, then preserve
    // that writer's original behavior. The child never consumes stdin here.
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) _exit(120);
    ssize_t result = native_write(fd, buffer, length);
    int error = errno;
    if (fcntl(fd, F_SETFL, flags) < 0) _exit(121);
    if (result < 0 && error == EAGAIN && !atomic_exchange(&blocked, true)) {
        int checkpoint = open(getenv("MCP_CONSOLE_TEST_STDIN_BLOCKED"), O_WRONLY);
        if (checkpoint < 0 || native_write(checkpoint, "1", 1) != 1) _exit(122);
        close(checkpoint);
    }
    if (result < 0 && error == EAGAIN && !(flags & O_NONBLOCK)) {
        return native_write(fd, buffer, length);
    }
    errno = error;
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *replacee; }
interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)&observe_write, (const void *)&write,
};
#else
ssize_t write(int fd, const void *buffer, size_t length) { return observe_write(fd, buffer, length); }
#endif
