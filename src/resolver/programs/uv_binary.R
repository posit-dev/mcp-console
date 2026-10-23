base::local({
  reticulate_path <- base::find.package("reticulate", quiet = TRUE)
  if (!base::length(reticulate_path)) {
    base::quit(save = "no", status = 42L, runLast = FALSE)
  }

  namespace <- base::loadNamespace("reticulate")
  if (!base::exists("uv_binary", envir = namespace, inherits = FALSE)) {
    base::quit(save = "no", status = 43L, runLast = FALSE)
  }

  if (base::identical(base::commandArgs(trailingOnly = TRUE), "--probe")) {
    base::quit(save = "no", status = 0L, runLast = FALSE)
  }

  uv <- base::get("uv_binary", envir = namespace, inherits = FALSE)()
  if (
    !base::is.character(uv) ||
      base::length(uv) != 1L ||
      base::is.na(uv) ||
      !base::nzchar(uv)
  ) {
    base::quit(save = "no", status = 44L, runLast = FALSE)
  }
  base::writeLines(base::enc2utf8(uv))
})
