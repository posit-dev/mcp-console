#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifdef __linux__
#include <dlfcn.h>
static ssize_t (*native_write)(int, const void *, size_t);
#define write native_write
#endif

static int worker_writer = -1;
static int signal_receipt;
static struct sigaction worker_interrupt;

__attribute__((constructor)) static void initialize(void) {
#ifdef __linux__
    native_write = dlsym(RTLD_NEXT, "write");
    if (native_write == NULL) _exit(120);
#endif
    const char *descriptor = getenv("MCP_CONSOLE_SIDEBAND_WRITE_FD");
    if (descriptor != NULL) worker_writer = atoi(descriptor);
}

static void observe_interrupt(int number) {
    int saved_errno = errno;
    worker_interrupt.sa_handler(number);
    if (write(signal_receipt, "1", 1) != 1) _exit(121);
    errno = saved_errno;
}

static ssize_t observe_write(int descriptor, const void *buffer, size_t length) {
    const char prefix[] = "{\"kind\":\"runtime_initialized\"";
    if (descriptor == worker_writer && length >= sizeof(prefix) - 1 &&
        memcmp(buffer, prefix, sizeof(prefix) - 1) == 0) {
        worker_writer = -1;
        int reached = open(getenv("MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETING"), O_WRONLY);
        int release = open(getenv("MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETE"), O_RDONLY);
        signal_receipt = open(getenv("MCP_CONSOLE_TEST_BOOTSTRAP_SIGNAL"), O_WRONLY | O_NONBLOCK);
        if (reached < 0 || release < 0 || signal_receipt < 0 ||
            sigaction(SIGINT, NULL, &worker_interrupt) != 0 ||
            worker_interrupt.sa_handler == SIG_DFL ||
            worker_interrupt.sa_handler == SIG_IGN ||
            (worker_interrupt.sa_flags & SA_SIGINFO) != 0) _exit(122);
        struct sigaction action = worker_interrupt;
        action.sa_handler = observe_interrupt;
        if (sigaction(SIGINT, &action, NULL) != 0 || write(reached, "1", 1) != 1) _exit(123);
        // The receipt is already serialized. Deliver SIGINT here, after the
        // worker's final acknowledgment, before the controller can admit code.
        char token;
        ssize_t count;
        do {
            count = read(release, &token, 1);
        } while (count < 0 && errno == EINTR);
        if (count != 1 || token != '1' ||
            sigaction(SIGINT, &worker_interrupt, NULL) != 0) _exit(124);
        close(reached);
        close(release);
        close(signal_receipt);
    }
    return write(descriptor, buffer, length);
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *replacee;
} interpose_write __attribute__((section("__DATA,__interpose"))) = {
    (const void *)(uintptr_t)&observe_write, (const void *)(uintptr_t)&write,
};
#else
#undef write
ssize_t write(int descriptor, const void *buffer, size_t length) {
    return observe_write(descriptor, buffer, length);
}
#endif
