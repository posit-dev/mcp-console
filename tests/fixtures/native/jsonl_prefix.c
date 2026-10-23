#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_read)(int, void *, size_t);
#define read native_read
#endif

static const char *prefix;
static int consumed;
static int release;

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
#ifdef __linux__
    native_read = dlsym(RTLD_NEXT, "read");
    if (!native_read) _exit(90);
#endif
    prefix = getenv("MCP_CONSOLE_TEST_JSONL_PREFIX");
    consumed = open(getenv("MCP_CONSOLE_TEST_JSONL_CONSUMED"), O_WRONLY | O_CLOEXEC);
    release = open(getenv("MCP_CONSOLE_TEST_JSONL_RELEASE"), O_RDONLY | O_CLOEXEC);
    if (!prefix || consumed < 0 || release < 0) _exit(91);
}

static ssize_t observed_read(int fd, void *buffer, size_t length) {
    ssize_t result = read(fd, buffer, length);
    if (result >= (ssize_t)strlen(prefix) &&
        memcmp(buffer, prefix, strlen(prefix)) == 0) {
        // Hold the successful native read before another read can coalesce
        // the suffix. No delimiter has been delivered to the consumer yet.
        if (write(consumed, "1", 1) != 1) _exit(92);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(93);
    }
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct { const void *replacement; const void *original; }
    interpose_read __attribute__((section("__DATA,__interpose"))) = {
        (const void *)(uintptr_t)&observed_read, (const void *)(uintptr_t)&read};
#else
#undef read
ssize_t read(int fd, void *buffer, size_t length) { return observed_read(fd, buffer, length); }
#endif
