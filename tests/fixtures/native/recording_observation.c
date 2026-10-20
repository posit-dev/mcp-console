#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static _Atomic int observed;
static pid_t owner;

__attribute__((constructor)) static void initialize(void) {
    owner = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
#ifdef __APPLE__
    ssize_t result = write(descriptor, buffer, length);
#else
    ssize_t (*next_write)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    ssize_t result = next_write(descriptor, buffer, length);
#endif
    const char *event = getenv("MCP_CONSOLE_TEST_OBSERVED_EVENT");
    const char *text = getenv("MCP_CONSOLE_TEST_OBSERVED_TEXT");
    const char *path = getenv("MCP_CONSOLE_TEST_JOURNAL_OBSERVED");
    if (getpid() == owner && (event != NULL || text != NULL) && path != NULL && result == (ssize_t)length) {
        char marker[128];
        int size = text != NULL ? snprintf(marker, sizeof(marker), "%s", text)
                               : snprintf(marker, sizeof(marker), "{\"event\":\"%s\",", event);
        int matches = 0;
        if (size > 0 && (size_t)size < sizeof(marker) && length >= (size_t)size) {
            size_t last = text != NULL ? length - (size_t)size : 0;
            for (size_t offset = 0; offset <= last; offset++) {
                if (memcmp((const char *)buffer + offset, marker, size) == 0) {
                    matches = 1;
                    break;
                }
            }
        }
        if (matches && !atomic_exchange(&observed, 1)) {
            int reached = open(path, O_WRONLY | O_NONBLOCK);
            if (reached < 0 || write(reached, "1", 1) != 1) _exit(124);
            close(reached);
            const char *release_path = getenv("MCP_CONSOLE_TEST_JOURNAL_RELEASE");
            if (release_path != NULL) {
                int release = open(release_path, O_RDONLY);
                if (release < 0) _exit(125);
                char token;
                ssize_t count;
                do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
                if (count != 1 || token != '1') _exit(126);
                close(release);
            }
        }
    }
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observed_write, (const void *)(uintptr_t)&write,
};
#else
ssize_t write(int descriptor, const void *buffer, size_t length) {
    return observed_write(descriptor, buffer, length);
}
#endif
