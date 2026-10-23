#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdatomic.h>
#include <sys/wait.h>
#include <unistd.h>

static atomic_int suspensions;

static int after_stage_exit(pid_t pid, int signal) {
    if (signal == SIGSTOP && atomic_fetch_add(&suspensions, 1) == 1) {
        // The first stage consumes an interrupt and returns a valid uv path.
        // Its pending control also targets the next stage. Observe that child's
        // independent exit before suspension, preserving real status,
        // the owner's non-reaping observer and all process/I/O retirement.
        siginfo_t status;
        int result;
        do {
            result = waitid(P_PID, (id_t)pid, &status, WEXITED | WNOWAIT);
        } while (result < 0 && errno == EINTR);
        if (result != 0 || status.si_code != CLD_EXITED || status.si_status != 23)
            _exit(125);
    }
#ifdef __APPLE__
    return kill(pid, signal);
#else
    return ((int (*)(pid_t, int))dlsym(RTLD_NEXT, "kill"))(pid, signal);
#endif
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} kill_interposer __attribute__((section("__DATA,__interpose"))) = {
    (const void *)after_stage_exit, (const void *)kill,
};
#else
int kill(pid_t pid, int signal) { return after_stage_exit(pid, signal); }
#endif
