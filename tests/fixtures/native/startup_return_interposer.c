#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
#include <linux/futex.h>
#include <stdarg.h>
#include <sys/syscall.h>
static int (*native_socketpair)(int, int, int, int[2]);
static int (*native_close)(int);
static long (*native_syscall)(long, ...);
#endif

static pid_t server_pid;
static atomic_int completion_descriptor = -1;
static pthread_t startup_thread;
static atomic_bool completion_claimed = false;
static _Thread_local bool released = false;

__attribute__((constructor)) static void initialize(void) {
    const char *owner = getenv("MCP_CONSOLE_TEST_COMPLETION_SERVER");
    if (owner == NULL) {
        server_pid = getpid();
        char pid[32];
        snprintf(pid, sizeof(pid), "%ld", (long)server_pid);
        setenv("MCP_CONSOLE_TEST_COMPLETION_SERVER", pid, 1);
    } else {
        server_pid = (pid_t)strtol(owner, NULL, 10);
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
#ifdef __linux__
    native_socketpair = dlsym(RTLD_NEXT, "socketpair");
    native_close = dlsym(RTLD_NEXT, "close");
    native_syscall = dlsym(RTLD_NEXT, "syscall");
    if (native_socketpair == NULL || native_close == NULL || native_syscall == NULL)
        _exit(120);
#endif
}

static void notify(const char *name) {
    int descriptor = open(getenv(name), O_WRONLY | O_NONBLOCK);
    if (descriptor < 0 || write(descriptor, "1", 1) != 1) _exit(121);
    close(descriptor);
}

static void await_release(void) {
    int descriptor = open(getenv("MCP_CONSOLE_TEST_COMPLETION_RELEASE"), O_RDONLY);
    if (descriptor < 0) _exit(122);
    char token;
    ssize_t count;
    do {
        count = read(descriptor, &token, 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(123);
    close(descriptor);
}

static int observe_socketpair(int domain, int type, int protocol, int descriptors[2]) {
#ifdef __APPLE__
    int result = socketpair(domain, type, protocol, descriptors);
#else
    int result = native_socketpair(domain, type, protocol, descriptors);
#endif
    // Local-host startup creates the server's first socket pair for its input
    // watcher. Python probes and compute owners create pairs only later, or in
    // child processes. Its creator closes the first endpoint after initialize
    // returns, before joining the watcher and returning the blocking task.
    if (result == 0 && getpid() == server_pid &&
        atomic_load(&completion_descriptor) == -1) {
        if (domain != AF_UNIX || (type & SOCK_STREAM) == 0) _exit(124);
        startup_thread = pthread_self();
        atomic_store(&completion_descriptor, descriptors[0]);
    }
    return result;
}

static int observe_close(int descriptor) {
#ifdef __APPLE__
    int result = close(descriptor);
#else
    int result = native_close(descriptor);
#endif
    if (result == 0 && getpid() == server_pid &&
        descriptor == atomic_load(&completion_descriptor) &&
        pthread_equal(pthread_self(), startup_thread) &&
        !atomic_exchange(&completion_claimed, true)) {
        if (access(getenv("MCP_CONSOLE_TEST_COMPLETION_ARMED"), F_OK) != 0) _exit(125);
        notify("MCP_CONSOLE_TEST_COMPLETION_STARTED");
        await_release();
        released = true;
    }
    return result;
}

static void before_park(void) {
    // Only the thread that closed the startup completion socket can acknowledge
    // return to the blocking pool, after the test releases its delayed outcome.
    if (getpid() == server_pid && released) {
        released = false;
        notify("MCP_CONSOLE_TEST_COMPLETION_PARKED");
    }
}

#ifdef __APPLE__
static int observe_cond_timedwait_relative(pthread_cond_t *condition, pthread_mutex_t *mutex,
                                           const struct timespec *timeout) {
    before_park();
    return pthread_cond_timedwait_relative_np(condition, mutex, timeout);
}

#define DYLD_INTERPOSE(replacement, replacee)                                  \
    __attribute__((used)) static struct {                                      \
        const void *replacement;                                               \
        const void *replacee;                                                  \
    } interpose_##replacee __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)(uintptr_t)&replacement,                                 \
        (const void *)(uintptr_t)&replacee,                                    \
    };

DYLD_INTERPOSE(observe_socketpair, socketpair)
DYLD_INTERPOSE(observe_close, close)
DYLD_INTERPOSE(observe_cond_timedwait_relative, pthread_cond_timedwait_relative_np)
#else
int socketpair(int domain, int type, int protocol, int descriptors[2]) {
    return observe_socketpair(domain, type, protocol, descriptors);
}

int close(int descriptor) {
    return observe_close(descriptor);
}

long syscall(long number, ...) {
    va_list arguments;
    va_start(arguments, number);
    long slots[6];
    for (int index = 0; index < 6; ++index) slots[index] = va_arg(arguments, long);
    va_end(arguments);
    long command = number == SYS_futex ? slots[1] & FUTEX_CMD_MASK : -1;
    if ((command == FUTEX_WAIT || command == FUTEX_WAIT_BITSET) && slots[3] != 0)
        before_park();
    return native_syscall(number, slots[0], slots[1], slots[2], slots[3], slots[4], slots[5]);
}
#endif
