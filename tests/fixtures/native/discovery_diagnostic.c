#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#endif

__attribute__((constructor)) static void discovery_diagnostic(int argc, char **argv) {
#ifdef __APPLE__
    argc = *_NSGetArgc();
    argv = *_NSGetArgv();
#endif
    // Worker-policy inspection clears this selection and is a separate peer.
    if (argc > 1 && strcmp(argv[1], "resolve") == 0 &&
        getenv("RETICULATE_PYTHON") != NULL) {
        const char message[] = "preparation detail\n";
        if (write(STDERR_FILENO, message, sizeof(message) - 1) != sizeof(message) - 1)
            _exit(125);
    }
}
static ssize_t observed_write(int descriptor, const void *buffer, size_t length) {
#ifdef __APPLE__
    ssize_t result = write(descriptor, buffer, length);
#else
    ssize_t (*next_write)(int, const void *, size_t) = dlsym(RTLD_NEXT, "write");
    ssize_t result = next_write(descriptor, buffer, length);
#endif
    const char marker[] = "{\"event\":\"startup_failed\",";
    const char *path = getenv("MCP_CONSOLE_TEST_STARTUP_FAILURE_RECORDED");
    if (path != NULL && result == (ssize_t)length && length >= sizeof(marker) - 1 &&
        memcmp(buffer, marker, sizeof(marker) - 1) == 0) {
        int reached = open(path, O_WRONLY | O_NONBLOCK);
        if (reached < 0 || write(reached, "1", 1) != 1) _exit(125);
        close(reached);
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
