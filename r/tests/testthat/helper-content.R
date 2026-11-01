# Text-only calls can also return a separate session-bundle discovery block.
# Validate that block before checking execution output, including preview limits.
console_output_text <- function(result) {
  if (S7::S7_inherits(result, ellmer::ContentText)) {
    return(result@text)
  }
  expect_length(result, 2L)
  notice <- result[[1L]]@text
  lines <- strsplit(notice, "\n", fixed = TRUE)[[1L]]
  expect_length(lines, 4L)
  prefixes <- c("Session bundle on the Console host: ", "Source: ", "Report: ")
  paths <- vapply(
    seq_along(prefixes),
    function(i) {
      expect_true(startsWith(lines[[i]], prefixes[[i]]))
      jsonlite::fromJSON(substring(lines[[i]], nchar(prefixes[[i]]) + 1L))
    },
    character(1)
  )
  expect_true(all(file.exists(paths)))
  paths <- normalizePath(paths, winslash = "/", mustWork = TRUE)
  expect_identical(basename(paths[[2L]]), "session.qmd")
  expect_identical(basename(paths[[3L]]), "session.md")
  expect_identical(dirname(paths[[2L]]), paths[[1L]])
  expect_identical(dirname(paths[[3L]]), paths[[1L]])
  expect_identical(
    lines[[4L]],
    "This live bundle updates as results are assembled; the path grants no filesystem access."
  )
  result[[2L]]@text
}
