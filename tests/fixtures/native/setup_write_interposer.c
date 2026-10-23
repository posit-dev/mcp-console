#include <crt_externs.h>
#include <errno.h>
#include <stdatomic.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

#include "manager_start_interposer.c"

static _Atomic int observed_setup_write = 0;
static _Atomic(const void *) setup_remainder = NULL;

static ssize_t observe_setup_write(int descriptor, const void *buffer, size_t count, int flags) {
    if (buffer == setup_remainder) {
        setup_remainder = NULL;
        ssize_t written = send(descriptor, buffer, count, flags);
        if (written > 0) {
            signal_checkpoint("MCP_CONSOLE_TEST_SETUP_WRITE_CONTINUED");
        }
        return written;
    }
    const unsigned char *bytes = buffer;
    if (runner_is_supervisor() && count > 4 && bytes[4] == '{'
        && (((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16)
            | ((uint32_t)bytes[2] << 8) | bytes[3]) == count - 4
        && atomic_exchange(&observed_setup_write, 1) == 0) {
        // Put part of the actual frame into the native setup socket before cancellation.
        ssize_t prefix = send(descriptor, buffer, 1, flags);
        if (prefix != 1) {
            _exit(125);
        }
        signal_checkpoint("MCP_CONSOLE_TEST_SETUP_WRITE_STARTED");
        wait_for_release("MCP_CONSOLE_TEST_SETUP_WRITE_RELEASE");
        if (getenv("MCP_CONSOLE_TEST_SETUP_WRITE_CONTINUED") != NULL) {
            // Return a successful short write with room left in the socket.
            // Observe whether the runner sends more after cancellation.
            setup_remainder = bytes + 1;
            return prefix;
        }
        // The stopped runner cannot drain the large frame in this scenario.
        ssize_t written = send(descriptor, bytes + 1, count - 1, flags);
        return written < 0 ? prefix : prefix + written;
    }
    return send(descriptor, buffer, count, flags);
}

DYLD_INTERPOSE(observe_setup_write, send)
