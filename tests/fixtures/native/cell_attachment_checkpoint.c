#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "capture_admission_checkpoint.h"

__attribute__((constructor)) static void initialize(void) {
    // This gate belongs to the server, not its resolver or worker children.
    if (unsetenv("DYLD_INSERT_LIBRARIES") != 0) _exit(120);
}

static int observed_unlock(pthread_mutex_t *mutex) {
    int result = pthread_mutex_unlock(mutex);
    if (result != 0) return result;
    unlocked(mutex);
    if (mutex == attachment) {
        attachment = NULL;
        int reached = open(getenv("MCP_CONSOLE_TEST_CELL_ATTACHED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_CELL_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(123);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(124);
        close(reached);
        close(release);
    }
    return result;
}

INTERPOSE(observed_unlock, pthread_mutex_unlock)
