base::local(
  {
    pager <- function(files, header, title, delete.file, encoding = "") {
      base::on.exit(if (delete.file) base::unlink(files))
      for (i in base::seq_along(files)) {
        if (base::nzchar(header[[i]])) {
          base::writeLines(base::enc2utf8(header[[i]]), useBytes = TRUE)
        }
        base::writeLines(
          base::enc2utf8(base::readLines(
            files[[i]],
            warn = FALSE,
            encoding = encoding
          )),
          useBytes = TRUE
        )
      }
      base::invisible(NULL)
    }

    base::options(help_type = "text", pager = pager)
    tools::Rd2txt_options(underline_titles = FALSE, showURLs = TRUE)

    utf8_pager <- function(...) pager(..., encoding = "UTF-8")
    print_vignette <- function(x, ...) {
      source <- base::file.path(x$Dir, "doc", x$File)
      if (!base::file.exists(source)) {
        base::stop(
          "Installed vignette source is unavailable: ",
          source,
          call. = FALSE
        )
      }
      selected_pager <- base::getOption("pager")
      # Read supported UTF-8/ASCII source independently of R's current locale.
      if (base::identical(selected_pager, pager)) {
        selected_pager <- utf8_pager
      }
      base::file.show(source, title = x$Title, pager = selected_pager)
      base::invisible(x)
    }

    # capture.output() can dispatch in utils' namespace. Follow S3 overrides there.
    utils_namespace <- base::asNamespace("utils")
    base::unlockBinding("print.vignette", utils_namespace)
    base::assign(
      "print.vignette",
      function(x, ...) {
        method <- utils::getS3method(
          "print",
          "vignette",
          envir = base::baseenv()
        )
        method(x, ...)
      },
      envir = utils_namespace
    )
    base::lockBinding("print.vignette", utils_namespace)
    base::registerS3method(
      "print",
      "vignette",
      print_vignette,
      envir = base::baseenv()
    )
    base::invisible(NULL)
  },
  envir = base::new.env(parent = base::baseenv())
)
