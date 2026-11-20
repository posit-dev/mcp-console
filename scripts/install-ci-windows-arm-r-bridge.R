# R-universe publishes native Windows ARM64 binaries for the CRAN packages.
stopifnot(.Platform$OS.type == "windows", R.version$arch == "aarch64")
install.packages(
  c("reticulate", "RSQLite"),
  repos = "https://cran.r-universe.dev",
  type = "win.binary.aarch64"
)
stopifnot(
  requireNamespace("reticulate", quietly = TRUE),
  requireNamespace("RSQLite", quietly = TRUE)
)
