#include <errno.h>
#include <signal.h>
#include <unistd.h>

int wait_for_probe_release(int started, int release) {
    ssize_t count;
    do {
        count = write(started, "1", 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1) {
        return -1;
    }

    // Stay inside the Python caller's native call until the test has sent its
    // interrupt. A signal can run on any worker thread, so wait for the explicit
    // release even when this thread's read is interrupted.
    char token;
    do {
        count = read(release, &token, 1);
    } while (count < 0 && errno == EINTR);
    return count == 1 && token == '1' ? 0 : -1;
}

int wait_for_probe_interrupt(int started, int release, int wakeup,
                             int (*check_signals)(void)) {
    if (wait_for_probe_release(started, release) != 0) {
        return -1;
    }

    // Python writes its wakeup descriptor after marking the signal pending,
    // including when another worker thread receives the process signal.
    unsigned char signal;
    ssize_t count;
    do {
        count = read(wakeup, &signal, 1);
    } while (count < 0 && errno == EINTR);
    if (count != 1 || signal != SIGINT) {
        return -1;
    }
    return check_signals();
}
