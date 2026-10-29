#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static atomic_bool held = false;
typedef ssize_t (*write_function)(int, const void *, size_t);

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static ssize_t checkpoint_write(int descriptor, const void *buffer, size_t count) {
#ifdef __APPLE__
    write_function next_write = write;
#else
    write_function next_write = (write_function)dlsym(RTLD_NEXT, "write");
#endif
    const unsigned char *bytes = buffer;
    const char match[] = "[output omitted:";
    bool matched = false;
    if (descriptor == STDOUT_FILENO && count && bytes[count - 1] == '\n') {
        for (size_t i = 0; i + sizeof(match) - 1 <= count; ++i) {
            if (memcmp(bytes + i, match, sizeof(match) - 1) == 0) {
                matched = true;
                break;
            }
        }
    }
    if (!matched || atomic_exchange(&held, true))
        return next_write(descriptor, buffer, count);

    // The peer receives the complete response before the transport writer can
    // acknowledge it. Integrator admission must still honor that delivery owner.
    size_t written = 0;
    while (written < count) {
        ssize_t result = next_write(descriptor, bytes + written, count - written);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0) _exit(125);
        written += (size_t)result;
    }
    int reached = open(getenv("MCP_CONSOLE_TEST_ACK_REACHED"), O_WRONLY);
    int release = open(getenv("MCP_CONSOLE_TEST_ACK_RELEASE"), O_RDONLY);
    if (reached < 0 || release < 0 || next_write(reached, "1", 1) != 1) _exit(125);
    close(reached);
    char token;
    ssize_t received;
    do { received = read(release, &token, 1); } while (received < 0 && errno == EINTR);
    if (received != 1 || token != '1') _exit(125);
    close(release);
    return (ssize_t)count;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&checkpoint_write, (const void *)(uintptr_t)&write,
};
#else
ssize_t write(int descriptor, const void *buffer, size_t count) {
    return checkpoint_write(descriptor, buffer, count);
}
#endif
