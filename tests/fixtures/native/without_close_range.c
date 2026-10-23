#define _GNU_SOURCE
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

// Exercise the public server with the responses produced by pre-5.9 kernels
// (ENOSYS), kernels without CLOSE_RANGE_CLOEXEC (EINVAL), and seccomp policies
// that deny close_range (EPERM).
int main(int argc, char **argv) {
    if (argc < 3) return 90;
    unsigned int error = (unsigned int)atoi(argv[1]);
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_close_range, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | error),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog filter = {
        .len = sizeof(instructions) / sizeof(instructions[0]),
        .filter = instructions,
    };
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter) != 0) return 91;
    execv(argv[2], &argv[2]);
    return 92;
}
