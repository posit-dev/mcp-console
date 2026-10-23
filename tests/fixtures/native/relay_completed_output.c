#ifdef __linux__
#define _GNU_SOURCE
#endif

#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_write)(int descriptor, const void *buffer, size_t length);
#define write native_write
static int (*native_clock_gettime)(clockid_t clock, struct timespec *time);
#define clock_gettime native_clock_gettime
#endif

static atomic_bool completed = false;
static struct stat output_identity;
static int completion_record;
static const char *completed_frame;
static int advance_seconds = 2;

__attribute__((constructor)) static void initialize_clock(void) {
    unsetenv("DYLD_INSERT_LIBRARIES");
#ifdef __linux__
    unsetenv("LD_PRELOAD");
    native_write = dlsym(RTLD_NEXT, "write");
    if (native_write == NULL) _exit(90);
    native_clock_gettime = dlsym(RTLD_NEXT, "clock_gettime");
    if (native_clock_gettime == NULL) _exit(90);
#endif
    completed_frame = getenv("MCP_CONSOLE_TEST_CLOCK_AFTER_FRAME");
    const char *seconds = getenv("MCP_CONSOLE_TEST_CLOCK_SECONDS");
    if (seconds != NULL) advance_seconds = atoi(seconds);
    completion_record = open(getenv("MCP_CONSOLE_TEST_OUTPUT_COMPLETE"),
                             O_WRONLY | O_CLOEXEC);
    if (completed_frame == NULL || *completed_frame == '\0' ||
        completion_record < 0 || fstat(STDOUT_FILENO, &output_identity) < 0) {
        _exit(91);
    }
}

static ssize_t observe_completed_write(int descriptor, const void *buffer, size_t length) {
    ssize_t result = write(descriptor, buffer, length);
    struct stat identity;
    if (result <= 0 || fstat(descriptor, &identity) < 0 ||
        identity.st_dev != output_identity.st_dev ||
        identity.st_ino != output_identity.st_ino) {
        return result;
    }
    size_t frame_length = strlen(completed_frame);
    static size_t matched = 0;
    const char *bytes = buffer;
    for (ssize_t index = 0; index < result; ++index) {
        matched = bytes[index] == completed_frame[matched]
                      ? matched + 1
                      : (bytes[index] == completed_frame[0] ? 1 : 0);
        if (matched == frame_length) {
            // Receiving stdout does not prove this interposer has resumed.
            // Publish the new clock before waking the checkpoint reader.
            atomic_store(&completed, true);
            if (write(completion_record, "1", 1) != 1) {
                _exit(92);
            }
            matched = 0;
        }
    }
    return result;
}

// Move past the retirement deadline after the selected complete frame is
// written. This models descheduling between writes or before a no-op flush.
static int advance_after_output(clockid_t clock, struct timespec *time) {
    int result = clock_gettime(clock, time);
    if (result == 0 && clock != CLOCK_REALTIME && atomic_load(&completed)) {
        time->tv_sec += advance_seconds;
    }
    return result;
}

#ifdef __APPLE__
#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                              \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observe_completed_write, write)
DYLD_INTERPOSE(advance_after_output, clock_gettime)

#else
#undef write
ssize_t write(int descriptor, const void *buffer, size_t length) {
    return observe_completed_write(descriptor, buffer, length);
}
#undef clock_gettime
int clock_gettime(clockid_t clock, struct timespec *time) {
    return advance_after_output(clock, time);
}
#endif
