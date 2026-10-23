# Configure the disposable CI installation, including profile-free resolvers.
system <- Sys.info()[["sysname"]]
stopifnot(system %in% c("Linux", "Darwin", "Windows"))

cran <- "https://cloud.r-project.org"
if (system == "Linux") {
  stopifnot(R.version$arch %in% c("x86_64", "aarch64"))
  version <- paste(
    R.version$major,
    sub("\\..*$", "", R.version$minor),
    sep = "."
  )
  # Jammy ARM64 has no distro-specific binaries. Portable binaries retain the
  # same glibc floor on both architectures and need no HTTP user-agent rewrite.
  cran <- sprintf(
    "https://packagemanager.posit.co/cran/latest/bin/linux/manylinux_2_28-%s/%s",
    R.version$arch,
    version
  )
}

path <- file.path(R.home("etc"), "repositories")
repositories <- read.delim(path, row.names = 1, comment.char = "#")
stopifnot("CRAN" %in% rownames(repositories))
repositories["CRAN", "URL"] <- cran
write.table(repositories, path, sep = "\t", quote = FALSE)

# Stock Ubuntu's site profile overrides the repository table. Keep ordinary
# R sessions consistent while isolated preparation reads only the data file.
cat(
  "\noptions(repos = c(CRAN = ",
  encodeString(cran, quote = '"'),
  "))\n",
  file = file.path(R.home("etc"), "Rprofile.site"),
  append = TRUE,
  sep = ""
)
message("CI CRAN repository: ", cran)
