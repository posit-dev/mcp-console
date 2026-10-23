#ifdef __linux__
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __APPLE__
#include <crt_externs.h>
#include <pthread.h>
#else
#include <dlfcn.h>
static int (*native_lstat)(const char *, struct stat *);
static int (*native_statx)(int, const char *, int, unsigned int, struct statx *);
static ssize_t (*native_read)(int, void *, size_t);
static ssize_t (*native_write)(int, const void *, size_t);
#define lstat native_lstat
#define read native_read
#define write native_write
#endif

static const char *selected;
static atomic_bool used;
static atomic_bool eof;
static bool lose_close_receipt;
#ifdef __APPLE__
static bool hold_queued_input;
static pthread_t main_thread;
static _Atomic(pthread_cond_t *) main_condition;
static _Atomic(pthread_mutex_t *) main_mutex;
static atomic_bool startup_returned;
static _Thread_local bool startup_thread;
static _Thread_local bool awaiting_watcher;
static _Thread_local pthread_t startup_watcher;
static _Thread_local bool watcher_joined;
static bool completion_dispatched;
static pthread_mutex_t input_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t input_condition = PTHREAD_COND_INITIALIZER;
static bool input_released;
#endif

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_lstat = dlsym(RTLD_NEXT, "lstat");
    native_statx = dlsym(RTLD_NEXT, "statx");
    native_read = dlsym(RTLD_NEXT, "read");
    native_write = dlsym(RTLD_NEXT, "write");
    if (!native_lstat || !native_statx || !native_read || !native_write) _exit(120);
#endif
    const char *path = getenv("MCP_CONSOLE_TEST_STARTUP_SELECTION");
    if (!path) return;
#ifdef __APPLE__
    if (*_NSGetArgc() < 2) return;
    const char *command = (*_NSGetArgv())[1];
#else
    char args[16384];
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) _exit(121);
    ssize_t count = read(fd, args, sizeof(args) - 1);
    close(fd);
    if (count <= 0) _exit(121);
    args[count] = '\0';
    size_t next = strlen(args) + 1;
    if (next >= (size_t)count) return;
    const char *command = args + next;
#endif
    bool drop_receipt = getenv("MCP_CONSOLE_TEST_STARTUP_LOSE_CLOSE") != NULL;
    if (strcmp(command, "resolve") == 0)
        // Lose only the worker-policy inspection peer's receipt. The retained
        // dependency peer still closes normally after inspection cancellation.
        lose_close_receipt = drop_receipt && getenv("RETICULATE_PYTHON") == NULL;
    else if (strcmp(command, "serve") == 0) selected = path;
    else return;
#ifdef __APPLE__
    main_thread = pthread_self();
    hold_queued_input = selected && getenv("MCP_CONSOLE_TEST_QUEUED_INPUT_REACHED");
#endif
    // Discovery and inspection children retain real process/I/O behavior.
    if (!selected || !drop_receipt) {
        unsetenv("DYLD_INSERT_LIBRARIES");
        unsetenv("LD_PRELOAD");
    }
}

static ssize_t observed_write(int fd, const void *buffer, size_t count) {
    // Only the preparation child loses its final close receipt, after its
    // operation result confirmed native cleanup. CLI exit cannot replace it.
    if (lose_close_receipt && count == 8 && memcmp(buffer, "\"Closed\"", 8) == 0) {
        int marker = open(getenv("MCP_CONSOLE_TEST_STARTUP_CLOSE_LOST"),
                          O_WRONLY | O_CREAT | O_CLOEXEC, 0600);
        if (marker < 0) _exit(125);
        close(marker);
        _exit(47);
    }
    return write(fd, buffer, count);
}

static ssize_t observed_read(int fd, void *buffer, size_t count) {
    ssize_t result = read(fd, buffer, count);
#ifdef __APPLE__
    if (hold_queued_input && fd == STDIN_FILENO && result > 0 &&
        memmem(buffer, (size_t)result, "queued-EOF", 10)) {
        int reached = open(getenv("MCP_CONSOLE_TEST_QUEUED_INPUT_REACHED"), O_WRONLY);
        if (reached < 0 || write(reached, "1", 1) != 1) _exit(126);
        close(reached);
        // Physical EOF is visible to the startup watcher, while the complete
        // request remains owned by this read and has no response reservation.
        pthread_mutex_lock(&input_mutex);
        while (!input_released) pthread_cond_wait(&input_condition, &input_mutex);
        pthread_mutex_unlock(&input_mutex);
    }
#endif
    if (selected && fd == STDIN_FILENO && result == 0 && !atomic_exchange(&eof, true)) {
        int observed = open(getenv("MCP_CONSOLE_TEST_STARTUP_EOF"), O_WRONLY);
        if (observed < 0 || write(observed, "1", 1) != 1) _exit(124);
        close(observed);
    }
    return result;
}

static void checkpoint_selection(const char *path) {
    if (selected && path && strcmp(path, selected) == 0 && !atomic_exchange(&used, true)) {
#ifdef __APPLE__
        startup_thread = hold_queued_input;
#endif
        int reached = open(getenv("MCP_CONSOLE_TEST_STARTUP_STAGE_REACHED"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_STARTUP_STAGE_RELEASE"), O_RDONLY);
        if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(122);
        char token;
        ssize_t count;
        do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1') _exit(123);
        close(reached);
        close(release);
    }
}

static int observed_lstat(const char *path, struct stat *status) {
    checkpoint_selection(path);
    return lstat(path, status);
}

#ifdef __APPLE__
static int observed_fstat(int descriptor, struct stat *status) {
    if (hold_queued_input && descriptor == STDIN_FILENO) awaiting_watcher = true;
    return fstat(descriptor, status);
}

static int observed_thread_create(pthread_t *thread, const pthread_attr_t *attributes,
                                  void *(*start)(void *), void *argument) {
    int result = pthread_create(thread, attributes, start, argument);
    if (awaiting_watcher) {
        if (result != 0) _exit(128);
        startup_watcher = *thread;
        awaiting_watcher = false;
    }
    return result;
}

static int observed_thread_join(pthread_t thread, void **result) {
    int status = pthread_join(thread, result);
    if (startup_thread && pthread_equal(thread, startup_watcher)) {
        if (status != 0) _exit(129);
        watcher_joined = true;
    }
    return status;
}

static bool before_park(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    if (!hold_queued_input) return false;
    if (startup_thread && watcher_joined) {
        // Returning to the blocking pool proves initialization and its scoped
        // EOF watcher have finished. Wake the runtime to dispatch that result.
        startup_thread = false;
        atomic_store(&startup_returned, true);
        pthread_mutex_t *wake_mutex = atomic_load(&main_mutex);
        pthread_cond_t *wake_condition = atomic_load(&main_condition);
        if (!wake_mutex || !wake_condition) _exit(127);
        pthread_mutex_lock(wake_mutex);
        pthread_cond_signal(wake_condition);
        pthread_mutex_unlock(wake_mutex);
    }
    if (!pthread_equal(pthread_self(), main_thread)) return false;
    atomic_store(&main_mutex, mutex);
    atomic_store(&main_condition, condition);
    if (!atomic_load(&startup_returned)) return false;
    if (!completion_dispatched) {
        completion_dispatched = true;
        return true;
    }
    // Ready startup/shutdown tasks have run before the runtime fully parks.
    // Release the queued request only now, without sleeps or scheduler guesses.
    pthread_mutex_lock(&input_mutex);
    input_released = true;
    pthread_cond_signal(&input_condition);
    pthread_mutex_unlock(&input_mutex);
    return false;
}

static int observed_cond_wait(pthread_cond_t *condition, pthread_mutex_t *mutex) {
    if (before_park(condition, mutex)) return 0;
    return pthread_cond_wait(condition, mutex);
}

static int observed_timed_wait(pthread_cond_t *condition, pthread_mutex_t *mutex,
                               const struct timespec *timeout) {
    if (before_park(condition, mutex)) return 0;
    return pthread_cond_timedwait_relative_np(condition, mutex, timeout);
}

#define INTERPOSE(replacement, original)                                      \
    __attribute__((used)) static struct {                                     \
        const void *replacement;                                              \
        const void *original;                                                 \
    } interpose_##original __attribute__((section("__DATA,__interpose"))) = { \
        (const void *)(uintptr_t)&replacement,                                \
        (const void *)(uintptr_t)&original,                                   \
    };
INTERPOSE(observed_cond_wait, pthread_cond_wait)
INTERPOSE(observed_timed_wait, pthread_cond_timedwait_relative_np)
INTERPOSE(observed_fstat, fstat)
INTERPOSE(observed_thread_create, pthread_create)
INTERPOSE(observed_thread_join, pthread_join)
INTERPOSE(observed_lstat, lstat)
INTERPOSE(observed_read, read)
INTERPOSE(observed_write, write)
#else
#undef lstat
#undef read
#undef write
int lstat(const char *path, struct stat *status) { return observed_lstat(path, status); }
// Rust uses statx for Linux symlink_metadata, and lstat on macOS.
int statx(int directory, const char *path, int flags, unsigned int mask, struct statx *status) {
    checkpoint_selection(path);
    return native_statx(directory, path, flags, mask, status);
}
ssize_t read(int fd, void *buffer, size_t count) { return observed_read(fd, buffer, count); }
ssize_t write(int fd, const void *buffer, size_t count) { return observed_write(fd, buffer, count); }
#endif
