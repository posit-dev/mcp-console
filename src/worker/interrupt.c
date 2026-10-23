#include <errno.h>
#include <signal.h>
#include <stdatomic.h>
#include <stddef.h>
#include <unistd.h>

typedef void (*interrupt_fn)(void);

static interrupt_fn record_interrupt;
static _Atomic(interrupt_fn) python_interrupt;
static int interrupt_wakeup;

static void handle_interrupt(int signum) {
    (void) signum;
    int saved_errno = errno;
    record_interrupt();
    interrupt_fn notify_python = atomic_load_explicit(&python_interrupt, memory_order_relaxed);
    if (notify_python != NULL) notify_python();
    /* Nonblocking self-pipe wakes managed input even if another thread got SIGINT. */
    ssize_t count = write(interrupt_wakeup, "1", 1);
    (void) count;
    errno = saved_errno;
}

static int install_interrupt_handler(void) {
    struct sigaction action = {0};
    action.sa_handler = handle_interrupt;
    /* Python must leave blocking syscalls to check its pending interrupt. */
    action.sa_flags = atomic_load_explicit(&python_interrupt, memory_order_relaxed)
        == NULL ? SA_RESTART : 0;
    sigemptyset(&action.sa_mask);
    return sigaction(SIGINT, &action, NULL);
}

int mcp_worker_install_python_interrupt(interrupt_fn set_interrupt) {
    atomic_store_explicit(&python_interrupt, set_interrupt, memory_order_relaxed);
    if (install_interrupt_handler() != 0) return -1;
    /*
     * R_SelectEx restores the previous SIGINT handler with signal(), which can
     * re-enable SA_RESTART after sigaction cleared it. siginterrupt also updates
     * libc's policy for later signal() calls, keeping blocking Python reads
     * interruptible. Suppress its deprecation warning only for this call: the
     * recommended replacement, sigaction, does not preserve that policy.
     */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
    return siginterrupt(SIGINT, 1);
#pragma GCC diagnostic pop
}

int mcp_worker_interrupt_configure(interrupt_fn record, int wakeup) {
    /* Reinstallation after R/reticulate hooks changes only the disposition.
     * The process-lifetime callback and pipe never change under a handler. */
    if (record_interrupt == NULL) {
        record_interrupt = record;
        interrupt_wakeup = wakeup;
    }
    return install_interrupt_handler();
}
