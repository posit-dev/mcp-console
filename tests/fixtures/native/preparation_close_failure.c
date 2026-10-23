#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static ssize_t fail_close(int descriptor, const void *buffer, size_t count) {
    // Let the real local resolver finish preparation and receive Close, then
    // exit before acknowledging retirement. No timing or child PID race.
    if (count == 8 && memcmp(buffer, "\"Closed\"", 8) == 0) {
        int marker = open(getenv("MCP_CONSOLE_TEST_CLOSE_MARKER"),
                          O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
        if (marker < 0) _exit(125);
        close(marker);
        _exit(47);
    }
#ifdef __APPLE__
    return write(descriptor, buffer, count);
#else
    return ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(
        descriptor, buffer, count);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} write_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)fail_close, (const void *)write,
};
#else
ssize_t write(int descriptor, const void *buffer, size_t count) {
    return fail_close(descriptor, buffer, count);
}
#endif
