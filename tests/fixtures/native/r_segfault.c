#include <stddef.h>

void mcp_test_segfault(void) {
    // An unmapped address delivers SIGSEGV on both systems. macOS delivers
    // SIGBUS for write-protection faults. Keep the pointer load volatile so
    // the compiler emits the faulting access instead of substituting a trap.
    volatile char *volatile address = NULL;
    *address = 1;
}
