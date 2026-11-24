#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// The selected Rscript inherits the loader setting from the worker's .Renviron.
__attribute__((constructor)) static void hold_library_probe(void) {
    const char *user = getenv("R_LIBS_USER");
    if (user == NULL || strcmp(user, "NULL") != 0) return;
    signal(SIGINT, SIG_IGN);
    FILE *pid = fopen("probe.pid", "w");
    if (pid == NULL || fprintf(pid, "%ld\n", (long)getpid()) < 0 || fclose(pid)) _exit(80);
    FILE *ready = fopen("probe-ready", "w");
    if (ready == NULL || fputc('1', ready) == EOF || fclose(ready)) _exit(81);
    for (;;) pause();
}
