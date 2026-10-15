# Forced unlink can chmod symlink targets outside the session directory.
# Preserve caller permissions and report removal failures instead.
stopifnot(base::unlink(base::tempdir(), recursive = TRUE, force = FALSE) == 0L)
base::.Internal(printDeferredWarnings())
