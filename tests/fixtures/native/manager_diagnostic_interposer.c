#include "manager_start_interposer.c"
#include <stdarg.h>
#include <stdio.h>
#include <sys/ioctl.h>

static const char diagnostic[] = "mcp-console-sandbox: failed to fill whole buffer\n";
static size_t diagnostic_written;
static atomic_int drain_cuts;
static char *helper_environment[6];

static int native_helper(void) {
    return *_NSGetArgc() > 1 &&
           strcmp((*_NSGetArgv())[1], "--native-macos") == 0;
}

/* Capture before Rust clears the fork child's environment, and allocate before
 * the manager starts threads. The production helper environment stays empty. */
__attribute__((constructor)) static void capture_helper_environment(void) {
    if (!runner_is_supervisor()) return;
    const char *names[] = {
        "MCP_CONSOLE_TEST_DIAGNOSTIC_STARTED",
        "MCP_CONSOLE_TEST_DIAGNOSTIC_RELEASE",
        "MCP_CONSOLE_TEST_DIAGNOSTIC_WRITTEN",
        "MCP_CONSOLE_TEST_HELPER_EXIT",
    };
    if (runner_test_library == NULL ||
        asprintf(&helper_environment[0], "DYLD_INSERT_LIBRARIES=%s", runner_test_library) < 0) {
        _exit(125);
    }
    for (size_t i = 0; i < 4; ++i) {
        const char *value = getenv(names[i]);
        if (value == NULL ||
            asprintf(&helper_environment[i + 1], "%s=%s", names[i], value) < 0) {
            _exit(125);
        }
    }
}

/* Inject only the fixture and its rendezvous paths at the real helper exec,
 * before Seatbelt or the target exec. No replacement executable is involved. */
static int diagnostic_execve(const char *path, char *const arguments[],
                             char *const environment[]) {
    if (arguments[0] == NULL || arguments[1] == NULL ||
        strcmp(arguments[1], "--native-macos") != 0) {
        return execve(path, arguments, environment);
    }
    if (environment[0] != NULL || helper_environment[0] == NULL) _exit(125);
    return execve(path, arguments, helper_environment);
}

/* Observe actual stderr bytes, including the final newline, rather than the
 * setup EOF that merely permits this independent producer to write later. */
static ssize_t diagnostic_write(int descriptor, const void *buffer, size_t count) {
    if (!native_helper() || descriptor != STDERR_FILENO) {
        return write(descriptor, buffer, count);
    }
    if (diagnostic_written == 0) {
        signal_checkpoint("MCP_CONSOLE_TEST_DIAGNOSTIC_STARTED");
        wait_for_release("MCP_CONSOLE_TEST_DIAGNOSTIC_RELEASE");
    }
    if (diagnostic_written + count > sizeof(diagnostic) - 1 ||
        memcmp(buffer, diagnostic + diagnostic_written, count) != 0) {
        _exit(125);
    }
    ssize_t result = write(descriptor, buffer, count);
    if (result > 0) diagnostic_written += (size_t)result;
    if (diagnostic_written == sizeof(diagnostic) - 1) {
        signal_checkpoint("MCP_CONSOLE_TEST_DIAGNOSTIC_WRITTEN");
        wait_for_release("MCP_CONSOLE_TEST_HELPER_EXIT");
    }
    return result;
}

/* Both real output readers reach FIONREAD only after launcher exit wakes
 * them. Hold their finite cuts so the test chooses the producer ordering. */
static int diagnostic_ioctl(int descriptor, unsigned long request, ...) {
    va_list arguments;
    va_start(arguments, request);
    void *argument = va_arg(arguments, void *);
    va_end(arguments);
    int gate = *_NSGetArgc() > 1 && strcmp((*_NSGetArgv())[1], "serve") == 0 &&
               request == FIONREAD && atomic_fetch_add(&drain_cuts, 1) < 2;
    if (gate) {
        signal_checkpoint("MCP_CONSOLE_TEST_DRAIN_STARTED");
        wait_for_release("MCP_CONSOLE_TEST_DRAIN_RELEASE");
    }
    int result = ioctl(descriptor, request, argument);
    if (gate) {
        if (result != 0) _exit(125);
        int queued = *(int *)argument;
        if (queued == 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_DRAIN_EMPTY");
        } else if (queued == sizeof(diagnostic) - 1) {
            signal_checkpoint("MCP_CONSOLE_TEST_DRAIN_DIAGNOSTIC");
        } else {
            _exit(125);
        }
    }
    return result;
}

DYLD_INTERPOSE(diagnostic_execve, execve)
DYLD_INTERPOSE(diagnostic_write, write)
DYLD_INTERPOSE(diagnostic_ioctl, ioctl)
