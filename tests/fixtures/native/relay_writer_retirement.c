#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdint.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Observe the server's actual command descriptor and successful native joins.
// No write, wait, or join is replaced by a simulated completion.
struct writer {
    pthread_t thread;
    int descriptor;
    int joined;
    int large_write;
};
static struct writer writers[16];
static int count;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static const char *root;
static pid_t server_pid;

__attribute__((constructor)) static void initialize(void) {
    server_pid = getpid();
    root = getenv("MCP_CONSOLE_TEST_WRITER_ROOT");
    // Observe this server only, including sandboxed relay launches.
    // Unsetting the loader variable prevents injection after exec, but a forked
    // child still has this shim and may inherit a locked observer mutex. Each
    // hook must check the original PID before touching observation state.
#ifdef __APPLE__
    unsetenv("DYLD_INSERT_LIBRARIES");
#else
    unsetenv("LD_PRELOAD");
#endif
}

static void mark(const char *event, int index) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s-%d", root, event, index + 1) >= (int)sizeof(path)) _exit(120);
    int descriptor = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (descriptor < 0) _exit(121);
    close(descriptor);
}

static ssize_t observe_write(int descriptor, const void *bytes, size_t length) {
    int index = -1;
    int first_large = 0;
    const unsigned char *frame = bytes;
    int probe = (length == 12 && memcmp(bytes, "writer-probe", 12) == 0) ||
        (length == 17 && memcmp(bytes, "echo writer-probe", 17) == 0);
    int large = length > 512 * 1024;
    int bootstrap = large && frame[4] == '{' &&
        (((uint32_t)frame[0] << 24) | ((uint32_t)frame[1] << 16) |
         ((uint32_t)frame[2] << 8) | frame[3]) == length - 4;
    if (getpid() == server_pid && root != NULL && (probe || large)) {
        pthread_mutex_lock(&lock);
        for (int i = 0; i < count; i++) {
            if (!writers[i].joined && writers[i].descriptor == descriptor &&
                pthread_equal(writers[i].thread, pthread_self())) index = i;
        }
        if (index < 0 && (probe || bootstrap)) {
            if (count == 16) _exit(122);
            index = count++;
            writers[index] = (struct writer){ .thread = pthread_self(), .descriptor = descriptor };
        }
        first_large = index >= 0 && large && !writers[index].large_write;
        if (index >= 0) writers[index].large_write |= first_large;
        pthread_mutex_unlock(&lock);
        if (first_large) mark("large-write", index);
    }
#ifdef __APPLE__
    ssize_t result = write(descriptor, bytes, length);
#else
    ssize_t result = ((ssize_t (*)(int, const void *, size_t))dlsym(RTLD_NEXT, "write"))(descriptor, bytes, length);
#endif
    int saved = errno;
    if (first_large && result > 0 && (size_t)result < length) mark("partial-write", index);
    errno = saved;
    return result;
}

static int observe_close(int descriptor) {
    int index = -1;
    if (getpid() == server_pid) {
        pthread_mutex_lock(&lock);
        for (int i = 0; i < count; i++) {
            if (writers[i].descriptor == descriptor) {
                index = i;
                writers[i].descriptor = -1;
                break;
            }
        }
        pthread_mutex_unlock(&lock);
    }
#ifdef __APPLE__
    int result = close(descriptor);
#else
    int result = ((int (*)(int))dlsym(RTLD_NEXT, "close"))(descriptor);
#endif
    int saved = errno;
    if (result == 0 && index >= 0) mark("closed", index);
    errno = saved;
    return result;
}

static int observe_join(pthread_t thread, void **value) {
    int index = -1;
    if (getpid() == server_pid) {
        pthread_mutex_lock(&lock);
        for (int i = 0; i < count; i++) {
            if (!writers[i].joined && pthread_equal(writers[i].thread, thread)) index = i;
        }
        pthread_mutex_unlock(&lock);
    }
#ifdef __APPLE__
    int result = pthread_join(thread, value);
#else
    int result = ((int (*)(pthread_t, void **))dlsym(RTLD_NEXT, "pthread_join"))(thread, value);
#endif
    if (result == 0 && index >= 0) {
        pthread_mutex_lock(&lock);
        writers[index].joined = 1;
        pthread_mutex_unlock(&lock);
        mark("joined", index);
    }
    return result;
}

static int gate_retirement_signal(pid_t pid, int number) {
    const char *mode = getpid() == server_pid ? getenv("MCP_CONSOLE_TEST_WRITER_MODE") : NULL;
    if (root != NULL && mode != NULL &&
        (strcmp(mode, "interrupt_after_eof") == 0 || strcmp(mode, "aborted_frame") == 0) && number == SIGTERM) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/kill-reached-1", root);
        int reached = open(path, O_WRONLY);
        snprintf(path, sizeof(path), "%s/kill-release-1", root);
        int release = open(path, O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(123);
        // Let the real relay observe command EOF and send its terminal events
        // before the already selected forced launcher signal is delivered.
        char token;
        ssize_t received;
        do { received = read(release, &token, 1); } while (received < 0 && errno == EINTR);
        if (received != 1 || token != '1') _exit(124);
        close(reached);
        close(release);
    }
#ifdef __APPLE__
    return kill(pid, number);
#else
    return ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, number);
#endif
}

#ifdef __APPLE__
#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *replacement; const void *original; } \
    interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)replacement, (const void *)original \
    };
INTERPOSE(observe_write, write)
INTERPOSE(observe_close, close)
INTERPOSE(observe_join, pthread_join)
INTERPOSE(gate_retirement_signal, kill)
#else
ssize_t write(int descriptor, const void *bytes, size_t length) { return observe_write(descriptor, bytes, length); }
int close(int descriptor) { return observe_close(descriptor); }
int pthread_join(pthread_t thread, void **value) { return observe_join(thread, value); }
int kill(pid_t pid, int number) { return gate_retirement_signal(pid, number); }
#endif
