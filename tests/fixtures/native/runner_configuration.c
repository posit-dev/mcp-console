#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#ifdef __linux__
#include <dlfcn.h>
static int (*next_execvp)(const char *, char *const[]);
__attribute__((constructor)) static void initialize(void) {
    next_execvp = dlsym(RTLD_NEXT, "execvp");
    if (next_execvp == NULL) _exit(125);
}
#define execvp next_execvp
#endif

/* Observe payloads without replacing the runner or loading this fixture into
 * the workload. Keep independent resolver launches separate from workers. */
static int capture_execvp(const char *path, char *const arguments[]) {
    if (arguments[0] != NULL && arguments[1] != NULL &&
        strcmp(arguments[1], "--config-env") == 0) {
        const char *destination = getenv("MCP_CONSOLE_TEST_RUNNER_CONFIGURATION");
        for (size_t index = 3; arguments[index] != NULL; index++) {
            if (strcmp(arguments[index], "--") == 0) {
                if (arguments[index + 1] != NULL && arguments[index + 2] != NULL &&
                    strcmp(arguments[index + 2], "resolve") == 0) {
                    destination = getenv("MCP_CONSOLE_TEST_RESOLVER_CONFIGURATION");
                    if (destination == NULL) return execvp(path, arguments);
                }
                break;
            }
        }
        const char *configuration = getenv(arguments[2]);
        if (destination == NULL || configuration == NULL) _exit(125);
        int output = open(destination, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (output < 0) _exit(125);
        size_t length = strlen(configuration);
        if (write(output, configuration, length) != (ssize_t)length ||
            write(output, "\n", 1) != 1 || close(output) != 0) _exit(125);
    }
    return execvp(path, arguments);
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&capture_execvp,
    (const void *)(uintptr_t)&execvp,
};
#else
#undef execvp
int execvp(const char *path, char *const arguments[]) {
    return capture_execvp(path, arguments);
}
#endif
