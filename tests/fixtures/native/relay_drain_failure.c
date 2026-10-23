#define _GNU_SOURCE
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_read)(int, void *, size_t);
static pid_t (*native_waitpid)(pid_t, int *, int);
#define read native_read
#define waitpid native_waitpid
#endif

static pid_t relay;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static int reaped;

__attribute__((constructor)) static void initialize(void) {
    relay = getpid();
    unsetenv("DYLD_INSERT_LIBRARIES");
    unsetenv("LD_PRELOAD");
#ifdef __linux__
    native_read = dlsym(RTLD_NEXT, "read");
    native_waitpid = dlsym(RTLD_NEXT, "waitpid");
    if (!native_read || !native_waitpid) _exit(90);
#endif
}

static ssize_t drain_after_reap(int fd, void *buffer, size_t length) {
    ssize_t result = read(fd, buffer, length);
    if (getpid() != relay) return result;
    static _Thread_local size_t matched;
    const char token[] = "\"broken\"";
    const char *bytes = buffer;
    for (ssize_t index = 0; index < result; ++index) {
        matched = bytes[index] == token[matched] ? matched + 1 : 0;
        if (matched == sizeof(token) - 1) {
            // Deliver the actual malformed bytes only after native reaping.
            // The failure must be retained after the control loop has settled.
            pthread_mutex_lock(&mutex);
            while (!reaped) pthread_cond_wait(&changed, &mutex);
            pthread_mutex_unlock(&mutex);
            matched = 0;
        }
    }
    return result;
}

static pid_t publish_reap(pid_t pid, int *status, int options) {
    pid_t result = waitpid(pid, status, options);
    if (getpid() == relay && result > 0) {
        pthread_mutex_lock(&mutex);
        reaped = 1;
        pthread_cond_broadcast(&changed);
        pthread_mutex_unlock(&mutex);
    }
    return result;
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement, (const void *)(uintptr_t)&original \
    };
INTERPOSE(drain_after_reap, read)
INTERPOSE(publish_reap, waitpid)
#else
#undef read
#undef waitpid
ssize_t read(int fd, void *buffer, size_t length) { return drain_after_reap(fd, buffer, length); }
pid_t waitpid(pid_t pid, int *status, int options) { return publish_reap(pid, status, options); }
#endif
