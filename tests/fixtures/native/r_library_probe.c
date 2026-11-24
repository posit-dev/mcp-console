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
    if (chdir(getenv("TMPDIR")) != 0) _exit(79);
    FILE *pid = fopen("probe-pid.tmp", "w");
    if (pid == NULL || fprintf(pid, "%ld\n", (long)getpid()) < 0 || fclose(pid)) _exit(80);
    if (rename("probe-pid.tmp", "probe.pid") != 0) _exit(81);
    for (;;) pause();
}
