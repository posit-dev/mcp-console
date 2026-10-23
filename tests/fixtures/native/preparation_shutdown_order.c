#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static pthread_t main_thread;
static atomic_bool close_task_started = false;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static bool close_written = false;
static int close_descriptor = -1;

typedef ssize_t (*write_function)(int, const void *, size_t);
typedef int (*create_function)(pthread_t *, const pthread_attr_t *,
                               void *(*)(void *), void *);

static write_function next_write(void) {
#ifdef __APPLE__
    return write;
#else
    return (write_function)dlsym(RTLD_NEXT, "write");
#endif
}

static bool armed(void) {
    const char *path = getenv("MCP_CONSOLE_TEST_SHUTDOWN_ORDER_ARMED");
    return path != NULL && access(path, F_OK) == 0;
}

__attribute__((constructor)) static void initialize(void) {
    main_thread = pthread_self();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
}

static int observe_create(pthread_t *thread, const pthread_attr_t *attributes,
                          void *(*start)(void *), void *argument) {
    // After arming, shutdown's blocking task creates the preparation closer.
    // Main-thread creation of a Tokio blocking-pool worker is unrelated.
    if (armed() && !pthread_equal(pthread_self(), main_thread)) {
        atomic_store(&close_task_started, true);
    }
#ifdef __APPLE__
    return pthread_create(thread, attributes, start, argument);
#else
    return ((create_function)dlsym(RTLD_NEXT, "pthread_create"))(
        thread, attributes, start, argument);
#endif
}

static bool contains(const void *buffer, size_t count, const char *match) {
    size_t length = strlen(match);
    const unsigned char *bytes = buffer;
    for (size_t index = 0; index + length <= count; ++index) {
        if (memcmp(bytes + index, match, length) == 0) return true;
    }
    return false;
}

static void record(write_function write_next, const char *event) {
    int descriptor = open(getenv("MCP_CONSOLE_TEST_SHUTDOWN_ORDER_RECORD"),
                          O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    size_t count = strlen(event);
    if (descriptor < 0 || write_next(descriptor, event, count) != (ssize_t)count) {
        _exit(125);
    }
    close(descriptor);
}

static ssize_t observe_write(int descriptor, const void *buffer, size_t count) {
    write_function write_next = next_write();
    if (!armed()) return write_next(descriptor, buffer, count);
    // Relay JSON is serialized in fragments; the kind's string body is one write.
    bool shutdown = count == 8 && memcmp(buffer, "shutdown", 8) == 0;
    bool control = contains(buffer, count, "\"Control\"");
    bool closing = count == 7 && memcmp(buffer, "\"Close\"", 7) == 0;
    pthread_mutex_lock(&lock);
    // Force a closer that was started before relay shutdown to finish its
    // request first. This exposes the race without a scheduling delay.
    while (shutdown && atomic_load(&close_task_started) && !close_written) {
        pthread_cond_wait(&changed, &lock);
    }
    ssize_t result = write_next(descriptor, buffer, count);
    int saved_errno = errno;
    if (result == (ssize_t)count) {
        if (shutdown) record(write_next, "shutdown\n");
        if (control) record(write_next, "control\n");
        if (closing) close_descriptor = descriptor;
        if (descriptor == close_descriptor && count == 1 &&
            memcmp(buffer, "\n", 1) == 0) {
            record(write_next, "close\n");
            close_descriptor = -1;
            close_written = true;
            pthread_cond_broadcast(&changed);
        }
    }
    pthread_mutex_unlock(&lock);
    errno = saved_errno;
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, replacee)                                      \
    __attribute__((used)) static struct {                                    \
        const void *replacement;                                            \
        const void *replacee;                                                \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                               \
        (const void *)(uintptr_t)&replacee,                                  \
    };

INTERPOSE(observe_create, pthread_create)
INTERPOSE(observe_write, write)
#else
int pthread_create(pthread_t *thread, const pthread_attr_t *attributes,
                   void *(*start)(void *), void *argument) {
    return observe_create(thread, attributes, start, argument);
}
ssize_t write(int descriptor, const void *buffer, size_t count) {
    return observe_write(descriptor, buffer, count);
}
#endif
