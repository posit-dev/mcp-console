if (Sys.getenv("QUARTO_PROJECT_RENDER_ALL") != "1") {
  quit(save = "no")
}

output_dir <- Sys.getenv("QUARTO_PROJECT_OUTPUT_DIR")
stopifnot(nzchar(output_dir))

pkgdown::build_site_github_pages(
  pkg = "../r",
  dest_dir = file.path(normalizePath(output_dir, mustWork = TRUE), "r"),
  install = TRUE,
  examples = FALSE
)

# Repository links in the package README target guides beside the r/ subsite.
home <- file.path(output_dir, "r", "index.html")
html <- readLines(home, encoding = "UTF-8", warn = FALSE)
html <- gsub('href="../docs/', 'href="../', html, fixed = TRUE)
writeLines(html, home, useBytes = TRUE)
