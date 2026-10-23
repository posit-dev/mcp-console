#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

static struct sigaction query_action;
static int interrupt_pipe[2];

static void observe_interrupt(int number) {
    int saved_errno = errno;
    query_action.sa_handler(number);
    if (write(interrupt_pipe[1], "1", 1) != 1) _exit(121);
    errno = saved_errno;
}

// Called by DuckDB's progress callback while the query and its SIGINT handler
// are active. Forward SIGINT to that handler before releasing the query.
void wait_for_sql_interrupt(char **started_path) {
    if (pipe(interrupt_pipe) != 0 || sigaction(SIGINT, NULL, &query_action) != 0 ||
        query_action.sa_handler == SIG_DFL || query_action.sa_handler == SIG_IGN ||
        (query_action.sa_flags & SA_SIGINFO) != 0) _exit(122);
    struct sigaction action = query_action;
    action.sa_handler = observe_interrupt;
    if (sigaction(SIGINT, &action, NULL) != 0) _exit(123);

    int started = open(*started_path, O_WRONLY);
    if (started < 0 || write(started, "1", 1) != 1) _exit(124);
    close(started);

    char token;
    ssize_t count;
    do {
        count = read(interrupt_pipe[0], &token, 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(125);
    if (sigaction(SIGINT, &query_action, NULL) != 0) _exit(126);
    close(interrupt_pipe[0]);
    close(interrupt_pipe[1]);
}
