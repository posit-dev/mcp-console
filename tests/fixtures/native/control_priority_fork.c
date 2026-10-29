#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

/* An R-level gate could service events between release and fork. Stay in
 * native code until the child has checked its inherited R event hooks. */
void mcp_test_priority_fork(int *status) {
    void (*process_events)(void) = dlsym(RTLD_DEFAULT, "R_ProcessEvents");
    if (process_events == NULL) _exit(120);
    int reached = open(getenv("MCP_CONSOLE_TEST_FORK_REACHED"), O_WRONLY);
    int release = open(getenv("MCP_CONSOLE_TEST_FORK_RELEASE"), O_RDONLY);
    if (reached < 0 || release < 0 || write(reached, "1", 1) != 1) _exit(121);
    close(reached);
    char token;
    ssize_t count;
    do { count = read(release, &token, 1); } while (count < 0 && errno == EINTR);
    if (count != 1 || token != '1') _exit(122);
    close(release);

    pid_t child = fork();
    if (child < 0) _exit(123);
    if (child == 0) {
        process_events();
        _exit(0);
    }
    int child_status;
    pid_t waited;
    do { waited = waitpid(child, &child_status, 0); }
    while (waited < 0 && errno == EINTR);
    if (waited != child || !WIFEXITED(child_status)) _exit(124);
    *status = WEXITSTATUS(child_status);
    process_events();
}
