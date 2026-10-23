#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#ifndef _WIN32
#include <sys/select.h>
#endif

typedef struct _InputHandler InputHandler;
typedef void (*repl_init_fn)(void);
typedef int (*repl_do_one_fn)(void);
typedef void (*before_do_one_fn)(void);
typedef int (*top_level_exec_fn)(void (*)(void *), void *);
typedef void *(*check_activity_fn)(int, int);
typedef void (*run_handlers_fn)(void *, void *);
typedef int (*read_console_fn)(const char *, unsigned char *, int, int);
typedef void (*check_interrupt_fn)(void);
typedef void *(*exec_with_cleanup_fn)(
    void *(*)(void *), void *, void (*)(void *), void *
);
typedef void (*object_fn)(void *);
typedef InputHandler *(*add_input_handler_fn)(
    InputHandler *, int, void (*)(void *), int
);
typedef int (*remove_input_handler_fn)(InputHandler **, InputHandler *);
typedef void (*process_events_fn)(void);

struct event_handlers {
    check_activity_fn check_activity;
    run_handlers_fn run_handlers;
    void *input_handlers;
};

struct event_wait {
    add_input_handler_fn add_input_handler;
    check_activity_fn check_activity;
    InputHandler **input_handlers;
    InputHandler *sideband_handler;
    int sideband_fd;
    int wait_usec;
    int sideband_ready;
};

static read_console_fn read_console;
static check_interrupt_fn check_interrupt;
static volatile int *interrupts_pending;

void mcp_r_record_interrupt(void) {
    *interrupts_pending = 1;
}

void mcp_r_console_configure(
    read_console_fn read,
    check_interrupt_fn check,
    volatile int *pending
) {
    read_console = read;
    check_interrupt = check;
    interrupts_pending = pending;
}

/*
 * Rust returns -1 after reporting a cancelled read. Check the pending
 * interrupt here so R's jump cannot cross a live Rust frame, and retry if R
 * defers the interrupt instead of translating the cancellation into EOF.
 */
int mcp_r_read_console(
    const char *prompt,
    unsigned char *buffer,
    int length,
    int add_history
) {
    int status;
    while ((status = read_console(prompt, buffer, length, add_history)) < 0) {
        if (*interrupts_pending != 0) check_interrupt();
    }
    return status;
}

struct repl_api {
    repl_init_fn init;
    repl_do_one_fn do_one;
    top_level_exec_fn top_level_exec;
    exec_with_cleanup_fn exec_with_cleanup;
    object_fn preserve;
    object_fn release;
    int *stack_top;
    void ***stack;
    void *nil;
};

static struct repl_api repl;

void mcp_r_repl_configure(const struct repl_api *api) {
    repl = *api;
}

struct repl_cell {
    before_do_one_fn before_do_one;
    void **roots;
    int root_count;
    int preserved;
    int last_status;
};

static void restore_roots(void *data) {
    struct repl_cell *cell = data;
    memcpy(*repl.stack, cell->roots, cell->root_count * sizeof(void *));
}

static void *run_cell(void *data) {
    struct repl_cell *cell = data;
    for (;;) {
        cell->before_do_one();
        if (*interrupts_pending != 0) check_interrupt();
        int status = repl.do_one();
        restore_roots(cell);
        *repl.stack_top = cell->root_count;
        if (*interrupts_pending != 0) check_interrupt();
        if (status < 0) return repl.nil;
        cell->last_status = status;
    }
}

static void run_protected_cell(void *data) {
    struct repl_cell *cell = data;
    /*
     * R_ReplDLLdo1 resets the protection stack to zero. Preserve the outer
     * context's roots separately, and restore its stack before normal return
     * or error unwinding. R_ExecWithCleanup runs the restoration in both cases.
     */
    cell->root_count = *repl.stack_top;
    cell->roots = malloc(cell->root_count * sizeof(void *));
    if (cell->roots == NULL) abort();
    memcpy(cell->roots, *repl.stack, cell->root_count * sizeof(void *));
    for (int i = 0; i < cell->root_count; ++i) {
        repl.preserve(cell->roots[i]);
        cell->preserved++;
    }
    (void) repl.exec_with_cleanup(run_cell, cell, restore_roots, cell);
}

static void run_ready_handlers(void *data) {
    struct event_handlers *handlers = data;
    void *ready = handlers->check_activity(0, 1);
    /* NULL intentionally runs R's polled-event hooks. */
    handlers->run_handlers(handlers->input_handlers, ready);
}

#ifndef _WIN32
static void wait_for_activity(void *data) {
    struct event_wait *wait = data;
    wait->sideband_handler = wait->add_input_handler(
        *wait->input_handlers, wait->sideband_fd, NULL, 0
    );
    fd_set *ready = wait->check_activity(wait->wait_usec, 1);
    wait->sideband_ready = ready != NULL && FD_ISSET(wait->sideband_fd, ready);
}

#endif

void mcp_r_run_ready_handlers(
    top_level_exec_fn top_level_exec,
    check_activity_fn check_activity,
    run_handlers_fn run_handlers,
    void *input_handlers
) {
    struct event_handlers handlers = {
        check_activity,
        run_handlers,
        input_handlers,
    };
    /* Contain handler long jumps without promoting them to worker failures. */
    (void) top_level_exec(run_ready_handlers, &handlers);
}

#ifdef _WIN32
static void run_windows_events(void *data) {
    process_events_fn *process_events = data;
    (*process_events)();
}

void mcp_r_run_windows_events(
    top_level_exec_fn top_level_exec, process_events_fn process_events
) {
    /* R dispatches window timers (including later) at top level. Keep all
     * handler/error/interrupt jumps inside C, never across a live Rust frame. */
    (void) top_level_exec(run_windows_events, &process_events);
}
#endif

#ifndef _WIN32
int mcp_r_wait_for_activity(
    top_level_exec_fn top_level_exec,
    add_input_handler_fn add_input_handler,
    remove_input_handler_fn remove_input_handler,
    check_activity_fn check_activity,
    InputHandler **input_handlers,
    int sideband_fd,
    int wait_usec
) {
    if (sideband_fd < 0 || sideband_fd >= FD_SETSIZE) {
        return -1;
    }
    struct event_wait wait = {
        add_input_handler,
        check_activity,
        input_handlers,
        NULL,
        sideband_fd,
        wait_usec > 0 ? wait_usec : -1,
        0,
    };
    int completed = top_level_exec(wait_for_activity, &wait);
    int removed = wait.sideband_handler == NULL ||
        remove_input_handler(input_handlers, wait.sideband_handler);
    if (!removed) {
        return -1;
    }
    /* R_ToplevelExec contains interrupt and handler-error unwinds. */
    return completed ? wait.sideband_ready : 0;
}

#endif

int mcp_r_repl_run_cell(before_do_one_fn before_do_one) {
    struct repl_cell cell = { .before_do_one = before_do_one, .last_status = 1 };
    repl.init();
    /* Never jump into the already-returned R_ReplDLLinit frame. */
    int completed = repl.top_level_exec(run_protected_cell, &cell);
    for (int i = 0; i < cell.preserved; ++i) repl.release(cell.roots[i]);
    free(cell.roots);
    return completed ? cell.last_status : 0;
}
