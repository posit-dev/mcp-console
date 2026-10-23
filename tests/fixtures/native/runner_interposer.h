#ifndef MCP_CONSOLE_RUNNER_INTERPOSER_H
#define MCP_CONSOLE_RUNNER_INTERPOSER_H

#include <crt_externs.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int runner_is_supervisor(void) {
    return *_NSGetArgc() > 1 &&
           strcmp((*_NSGetArgv())[1], "--config-env") == 0;
}

static char *runner_test_library;

__attribute__((constructor)) static void capture_runner_test_library(void) {
    const char *library = getenv("DYLD_INSERT_LIBRARIES");
    if (library != NULL) {
        runner_test_library = strdup(library);
    }
    /* The library is already loaded. Keep its injection out of the target
     * environment captured by the runner, including arm64e system targets. */
    if (runner_is_supervisor() && unsetenv("DYLD_INSERT_LIBRARIES") != 0) {
        _exit(125);
    }
}

/* Console strips host interposers at its production exec boundary. Reinsert
 * this test library into that exec's environment to observe native syscalls.
 * There is no replacement executable or additional process in the fixture. */
static int runner_test_execvp(const char *path, char *const arguments[]) {
    if (arguments[0] != NULL && arguments[1] != NULL &&
        strcmp(arguments[1], "--config-env") == 0 && runner_test_library != NULL) {
        if (setenv("DYLD_INSERT_LIBRARIES", runner_test_library, 1) != 0) {
            _exit(125);
        }
    }
    return execvp(path, arguments);
}

__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} runner_exec_interpose __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&runner_test_execvp,
    (const void *)(uintptr_t)&execvp,
};
#endif
