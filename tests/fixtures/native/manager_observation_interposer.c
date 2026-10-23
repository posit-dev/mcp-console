#include "runner_interposer.h"
#include <crt_externs.h>
#include <errno.h>
#include <fcntl.h>
#include <libproc.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef int (*proc_listchildpids_function)(pid_t, void *, int);

static proc_listchildpids_function next_proc_listchildpids(void) {
    return proc_listchildpids;
}

static void report_observation(pid_t process_id) {
    const char *path = getenv("MCP_CONSOLE_TEST_MANAGER_OBSERVATIONS");
    if (path == NULL) {
        _exit(125);
    }
    int descriptor;
    do {
        descriptor = open(path, O_WRONLY | O_NONBLOCK);
    } while (descriptor < 0 && errno == EINTR);
    if (descriptor < 0) {
        _exit(125);
    }

    char record[32];
    int length = snprintf(record, sizeof(record), "%d\n", process_id);
    if (length <= 0 || (size_t)length >= sizeof(record)) {
        _exit(125);
    }
    ssize_t count;
    do {
        count = write(descriptor, record, (size_t)length);
    } while (count < 0 && errno == EINTR);
    close(descriptor);
    if (count != length) {
        _exit(125);
    }
}

static int observe_child_snapshot(
    pid_t process_id,
    void *buffer,
    int buffer_size
) {
    int result = next_proc_listchildpids()(process_id, buffer, buffer_size);
    int saved_errno = errno;
    if (runner_is_supervisor() && (result > 0 || (result == 0 && saved_errno == 0))) {
        report_observation(process_id);
    }
    errno = saved_errno;
    return result;
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observe_child_snapshot, proc_listchildpids)
