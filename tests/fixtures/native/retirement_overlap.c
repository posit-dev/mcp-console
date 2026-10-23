#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static int closing = -1;

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t observe_write(int fd, const void *buffer, size_t count) {
#ifdef __APPLE__
    ssize_t result = write(fd, buffer, count);
#else
    ssize_t result = ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(fd, buffer, count);
#endif
    // The real preparation Close frame proves EOF has entered shutdown while
    // the test still holds the earlier restart's relay retirement gate.
    pthread_mutex_lock(&lock);
    if (result == (ssize_t)count && count == 7 && memcmp(buffer, "\"Close\"", 7) == 0)
        closing = fd;
    else if (fd == closing && result == 1 && count == 1 && memcmp(buffer, "\n", 1) == 0) {
        closing = -1;
        int checkpoint = open(getenv("MCP_CONSOLE_TEST_RETIREMENT_CLOSE"), O_WRONLY | O_NONBLOCK);
        if (checkpoint < 0) _exit(125);
#ifdef __APPLE__
        ssize_t notified = write(checkpoint, "1", 1);
#else
        ssize_t notified = ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(checkpoint, "1", 1);
#endif
        if (notified != 1) _exit(125);
        close(checkpoint);
    }
    pthread_mutex_unlock(&lock);
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *original; }
interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observe_write, (const void *)(uintptr_t)&write,
};
#else
ssize_t write(int fd, const void *buffer, size_t count) { return observe_write(fd, buffer, count); }
#endif
