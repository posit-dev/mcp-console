base::local({
  input <- base::paste(
    base::readLines(
      base::file("stdin", encoding = "UTF-8"),
      warn = FALSE
    ),
    collapse = "\n"
  )
  input <- jsonlite::fromJSON(input)
  extensions <- base::unlist(input$extensions, use.names = FALSE)
  base::stopifnot(
    base::is.character(extensions),
    base::length(extensions) > 0L,
    base::all(base::grepl("^[a-z][a-z0-9_]*$", extensions))
  )

  extension_directory <- Sys.getenv("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY")
  file_identity <- function(paths) {
    paths <- sort(paths)
    information <- file.info(paths)
    stopifnot(!anyNA(information$size))
    list(
      paths = normalizePath(paths, winslash = "/", mustWork = TRUE),
      size = information$size,
      modified = as.numeric(information$mtime),
      changed = as.numeric(information$ctime)
    )
  }
  preparation_identity <- function() {
    packages <- lapply(c("DBI", "duckdb", "jsonlite"), function(package) {
      file_identity(list.files(
        find.package(package),
        full.names = TRUE,
        recursive = TRUE,
        all.files = TRUE,
        no.. = TRUE
      ))
    })
    as.character(jsonlite::toJSON(
      list(
        recipe = input$recipe,
        runtime = list(
          version = R.version.string,
          executable = file_identity(commandArgs()[[1L]])
        ),
        packages = packages,
        extensions = sort(extensions),
        cache = normalizePath(
          extension_directory,
          winslash = "/",
          mustWork = FALSE
        ),
        artifacts = file_identity(list.files(
          extension_directory,
          pattern = "[.]duckdb_extension([.]info)?$",
          full.names = TRUE,
          recursive = TRUE
        ))
      ),
      auto_unbox = TRUE,
      digits = NA
    ))
  }
  receipt <- file.path(extension_directory, ".mcp-console-r-duckdb.json")
  # Cache hints never supply package paths or permissions. Match current
  # identities directly, including installed artifacts and package contents.
  # Native host-default storage remains owned by DuckDB when no root is given.
  if (nzchar(extension_directory)) {
    reused <- tryCatch(
      file.exists(receipt) &&
        identical(
          readLines(receipt, warn = FALSE, encoding = "UTF-8"),
          preparation_identity()
        ),
      error = function(condition) FALSE
    )
    if (reused) {
      return(invisible())
    }
  }

  storage <- base::tempfile("mcp-console-duckdb-resolver-")
  connection <- DBI::dbConnect(
    duckdb::duckdb(
      dbdir = ":memory:",
      config = list(
        # Discover built-ins without reading another installer's metadata.
        extension_directory = base::file.path(storage, "builtin-extensions"),
        secret_directory = base::file.path(storage, "stored-secrets"),
        temp_directory = base::file.path(storage, "spill")
      )
    )
  )
  disconnect_attempted <- FALSE
  base::on.exit(
    if (!disconnect_attempted) DBI::dbDisconnect(connection),
    add = TRUE
  )
  DBI::dbExecute(connection, "SET enable_progress_bar = false")
  if (Sys.getenv("CODEX_NETWORK_PROXY_ACTIVE") == "1") {
    # Extension INSTALL does not consume the proxy environment itself.
    DBI::dbExecute(
      connection,
      paste(
        "SET http_proxy =",
        DBI::dbQuoteString(connection, Sys.getenv("HTTP_PROXY"))
      )
    )
  }

  builtin_extensions <- DBI::dbGetQuery(
    connection,
    paste(
      "SELECT unnest(aliases || [extension_name]) AS name FROM duckdb_extensions()",
      "WHERE install_path = '(BUILT-IN)'"
    )
  )$name
  # Installation uses the captured shared cache, including DuckDB's native
  # default when no explicit extension directory was selected.
  DBI::dbExecute(
    connection,
    base::paste(
      "SET extension_directory =",
      DBI::dbQuoteString(connection, extension_directory)
    )
  )
  for (extension in base::setdiff(extensions, builtin_extensions)) {
    identifier <- DBI::dbQuoteIdentifier(connection, extension)
    DBI::dbExecute(
      connection,
      base::paste("INSTALL", identifier)
    )
  }
  disconnect_attempted <- TRUE
  DBI::dbDisconnect(connection)
  if (nzchar(extension_directory)) {
    # Partial or concurrent receipt writes are harmless misses: reuse requires
    # exact equality with the full current identity. Failed installs never get here.
    tryCatch(
      {
        dir.create(extension_directory, recursive = TRUE, showWarnings = FALSE)
        writeLines(preparation_identity(), receipt, useBytes = TRUE)
      },
      error = function(condition) invisible()
    )
  }
})
