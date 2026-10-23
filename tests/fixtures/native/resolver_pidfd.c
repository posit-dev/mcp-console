#define _GNU_SOURCE
#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

__attribute__((constructor)) static void deny_pidfd_open(void) {
    const char *name = getenv("MCP_CONSOLE_TEST_PIDFD_ERRNO");
    int error = name != NULL && strcmp(name, "EPERM") == 0 ? EPERM : ENOSYS;
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_pidfd_open, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | error),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {
        .len = sizeof(filter) / sizeof(filter[0]), .filter = filter,
    };
    // The filter and blocked signal mask are inherited by every observer and
    // materializer. Test-process event witnesses remain outside this policy.
    sigset_t blocked;
    if (sigemptyset(&blocked) != 0 || sigaddset(&blocked, SIGCHLD) != 0 ||
        sigprocmask(SIG_BLOCK, &blocked, NULL) != 0 ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) != 0) _exit(126);
    if (syscall(SYS_pidfd_open, getpid(), 0) != -1 || errno != error) _exit(127);
    unsetenv("LD_PRELOAD");
}
