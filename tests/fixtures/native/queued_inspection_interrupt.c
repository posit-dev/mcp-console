#include <signal.h>
#include <unistd.h>

void queue_inspection_interrupt(void) {
    /* raise returns after this thread's handler has recorded SIGINT. */
    if (raise(SIGINT) != 0) _exit(111);
}
