#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_key_t diagnostic_reader;
static _Thread_local size_t matched;

static void reader_exited(void *value) {
    (void)value;
    int checkpoint = open(getenv("MCP_CONSOLE_TEST_DIAGNOSTIC_EXIT"), O_WRONLY | O_CLOEXEC);
    if (checkpoint < 0 || write(checkpoint, "1", 1) != 1) _exit(125);
    close(checkpoint);
}

__attribute__((constructor)) static void initialize(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
    if (pthread_key_create(&diagnostic_reader, reader_exited) != 0) _exit(125);
}

static ssize_t observe_read(int descriptor, void *buffer, size_t length) {
#ifdef __APPLE__
    ssize_t result = read(descriptor, buffer, length);
#else
    ssize_t result = ((ssize_t (*)(int, void *, size_t))dlsym(RTLD_NEXT, "read"))(
        descriptor, buffer, length);
#endif
    int saved_errno = errno;
    const char marker[] = "diagnostic reader ready\n";
    const unsigned char *bytes = buffer;
    for (ssize_t index = 0; index < result; ++index) {
        matched = bytes[index] == (unsigned char)marker[matched]
                      ? matched + 1
                      : (bytes[index] == (unsigned char)marker[0] ? 1 : 0);
        if (matched == strlen(marker)) {
            if (pthread_setspecific(diagnostic_reader, (void *)1) != 0) _exit(125);
            matched = 0;
        }
    }
    errno = saved_errno;
    return result;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_read __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observe_read, (const void *)(uintptr_t)&read,
};
#else
ssize_t read(int descriptor, void *buffer, size_t length) {
    return observe_read(descriptor, buffer, length);
}
#endif
