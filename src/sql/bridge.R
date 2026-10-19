base::local(
  {
    managed_connection <- NULL
    rendering_connection <- NULL
    selected_connection <- NULL
    last_result <- NULL
    result_epoch <- 0
    source <- NULL
    printer_ready <- FALSE
    preview_rows <- 20L
    preview_columns <- 12L
    cell_width <- 160L
    preview_width <- 200L
    response_bytes <- 12L * 1024L

    bridge <- environment()
    send_query_arrow <- eval(
      bquote(
        function() {
          DBI::dbSendQueryArrow(
            .(bridge)$selected_connection,
            .(bridge)$source
          )
        }
      ),
      envir = globalenv()
    )

    open_connection <- function(config = list()) {
      config$extension_directory <- Sys.getenv(
        "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"
      )
      parent <- if (identical(Sys.getenv("MCP_CONSOLE_MANAGED_TEMPDIR"), "1")) {
        Sys.getenv("TMPDIR")
      } else {
        tempdir()
      }
      storage <- file.path(parent, "mcp-console-duckdb")
      config$secret_directory <- file.path(storage, "stored-secrets")
      config$temp_directory <- file.path(storage, "spill")
      connection <- DBI::dbConnect(
        duckdb::duckdb(
          dbdir = ":memory:",
          config = config,
          environment_scan = TRUE
        )
      )
      complete <- FALSE
      on.exit({
        if (!complete) {
          tryCatch(
            DBI::dbDisconnect(connection),
            error = function(condition) {
              cat(
                "Error closing SQL connection: ",
                conditionMessage(condition),
                "\n",
                sep = ""
              )
            }
          )
        }
      })
      DBI::dbExecute(connection, "SET enable_progress_bar = false")
      complete <- TRUE
      connection
    }

    ensure_managed_connection <- function() {
      if (is.null(managed_connection)) {
        managed_connection <<- open_connection()
      }
      invisible(managed_connection)
    }

    ensure_rendering_connection <- function() {
      # Preview formatting and conversion must not execute another statement on
      # a retained cursor's connection. Keep a separate scratch catalog.
      if (is.null(rendering_connection)) {
        rendering_connection <<- open_connection(
          config = list(preserve_insertion_order = "true")
        )
      }
      rendering_connection
    }

    initialize_managed_connection <- function() {
      tryCatch(
        {
          # Bare environments may omit the optional managed provider.
          if (
            !nzchar(system.file(package = "DBI")) ||
              !nzchar(system.file(package = "duckdb"))
          ) {
            return(0L)
          }
          ensure_managed_connection()
          1L
        },
        interrupt = function(condition) {
          cat("\n")
          -1L
        },
        error = function(condition) {
          cat("Error: ", conditionMessage(condition), "\n", sep = "")
          0L
        }
      )
    }

    ensure_connection <- function() {
      if (is.null(selected_connection)) {
        selected_connection <<- ensure_managed_connection()
      }
      selected_connection
    }

    sql_connection <- function(connection) {
      if (missing(connection)) {
        if (.Call("mcp_console_sql_has_python_connection")) {
          stop(paste(
            "The active SQL connection belongs to Python;",
            "use _console.sql_connection() in Python"
          ))
        }
        if (.Call("mcp_console_sql_take_restore_request")) {
          selected_connection <<- NULL
        }
        return(ensure_connection())
      }
      if (is.null(connection)) {
        connection <- ensure_managed_connection()
      } else {
        if (
          !inherits(connection, "DBIConnection") ||
            !isTRUE(tryCatch(
              DBI::dbIsValid(connection),
              error = function(...) FALSE
            ))
        ) {
          stop("`connection` must be a valid DBIConnection or NULL")
        }
      }
      close_last_result()
      invisible(.Call("mcp_console_sql_use_r"))
      selected_connection <<- connection
      invisible(NULL)
    }

    initialize_connection <- function(source) {
      program <- tryCatch(
        parse(text = source, keep.source = FALSE),
        error = function(condition) {
          # R's parse diagnostic appends the source excerpt on later lines.
          cat(
            "Error: R startup syntax error: ",
            strsplit(conditionMessage(condition), "\n", fixed = TRUE)[[1L]][1L],
            "\n",
            sep = ""
          )
          NULL
        }
      )
      if (is.null(program)) {
        return("failed")
      }
      tryCatch(
        {
          eval(program, envir = globalenv())
          if (
            !.Call("mcp_console_sql_r_connection_selected") ||
              is.null(selected_connection) ||
              identical(selected_connection, managed_connection) ||
              !isTRUE(DBI::dbIsValid(selected_connection))
          ) {
            stop(
              "startup must select a valid native connection with .console$sql_connection(connection)"
            )
          }
          "ready"
        },
        interrupt = function(condition) {
          cat("R startup interrupted\n")
          "interrupted"
        },
        error = function(condition) {
          cat("Error: ", conditionMessage(condition), "\n", sep = "")
          "failed"
        }
      )
    }

    restore_managed_connection <- function() {
      close_last_result()
      selected_connection <<- NULL
      1L
    }
    tools <- base::as.environment("tools:mcp-console")
    # Match reticulate's getter-only `py` binding. Attribute assignment such as
    # `py$name <- value` already writes through the returned Python module proxy.
    base::makeActiveBinding("py", function() reticulate::py, tools)
    console <- new.env(parent = emptyenv())
    console$sql_connection <- sql_connection
    console$sql_last_result <- function(n = NULL) fetch_last_result(n)
    base::assign(".console", console, envir = tools)

    ensure_printer <- function() {
      if (printer_ready) {
        return(invisible(NULL))
      }

      type_sum <- function(x) {
        attr(x, "arrow_type", exact = TRUE)
      }
      pillar_shaft <- function(x, ...) {
        type <- attr(x, "arrow_type", exact = TRUE)
        # MCP displays UTF-8 independently of the R process locale.
        formatted <- utf8::utf8_encode(
          unclass(x),
          quote = grepl("^(large_)?string|^string_view", type),
          utf8 = TRUE
        )
        align <- if (grepl("^(u?int|float|double|decimal)", type)) {
          "right"
        } else {
          "left"
        }
        pillar::new_pillar_shaft_simple(
          formatted,
          align = align,
          min_width = 1L,
          na = "NULL",
          shorten = "back"
        )
      }

      registerS3method(
        "type_sum",
        "mcp_console_sql_column",
        type_sum,
        envir = asNamespace("pillar")
      )
      registerS3method(
        "pillar_shaft",
        "mcp_console_sql_column",
        pillar_shaft,
        envir = asNamespace("pillar")
      )
      printer_ready <<- TRUE
      invisible(NULL)
    }

    arrow_type <- function(schema) {
      sub("^<nanoarrow_schema (.*)>$", "\\1", format(schema))
    }

    close_last_result <- function() {
      result_epoch <<- result_epoch + 1
      previous <- last_result
      last_result <<- NULL
      # Discard first; failed driver cleanup must not cancel the next action.
      tryCatch(
        {
          if (!is.null(previous$result) && DBI::dbIsValid(previous$result)) {
            DBI::dbClearResult(previous$result)
          }
        },
        error = function(condition) {
          cat("Error: ", conditionMessage(condition), "\n", sep = "")
        }
      )
      1L
    }

    retain_result <- function(result, buffer, schema = NULL) {
      # Driver completion and cleanup callbacks can change the selection.
      # Build this candidate before checking the epoch for publication.
      if (DBI::dbHasCompleted(result)) {
        DBI::dbClearResult(result)
        result <- NULL
      }
      list(
        result = result,
        buffer = buffer,
        offset = 0L,
        schema = schema,
        empty = if (is.null(schema)) buffer[integer(), , drop = FALSE]
      )
    }

    fetch_last_result <- function(n) {
      stopifnot(
        is.null(n) ||
          (is.numeric(n) &&
            length(n) == 1L &&
            !is.na(n) &&
            is.finite(n) &&
            n >= 0 &&
            n == floor(n))
      )
      if (is.null(last_result)) {
        stop(
          "No retained R SQL result; retrieve in the connection's owning language"
        )
      }
      pending <- last_result
      epoch <- result_epoch
      complete <- FALSE
      on.exit({
        if (epoch == result_epoch) {
          if (complete) last_result <<- pending else close_last_result()
        }
      })
      if (!is.null(pending$result) && !DBI::dbIsValid(pending$result)) {
        stop("SQL result is no longer valid")
      }
      # Only managed DuckDB needs the temporary Arrow retrieval path below.
      # Its conversion boundary is duckdb_result_data_frame().
      arrow <- !is.null(pending$schema)
      remaining <- if (is.null(n)) Inf else n
      # Arrow conversion skips the consumed prefix of the first saved batch.
      offset <- if (is.null(pending$buffer)) 0L else pending$offset
      parts <- list()
      fetched <- 0
      while (
        remaining > 0 && (!is.null(pending$buffer) || !is.null(pending$result))
      ) {
        if (is.null(pending$buffer)) {
          size <- min(remaining, 65536L)
          pending$buffer <- if (arrow) {
            DBI::dbFetchArrowChunk(pending$result, chunk_size = size)
          } else {
            DBI::dbFetch(pending$result, n = size)
          }
          pending$offset <- 0L
          if (DBI::dbHasCompleted(pending$result)) {
            DBI::dbClearResult(pending$result)
            pending$result <- NULL
          }
        }
        count <- if (arrow) pending$buffer$length else nrow(pending$buffer)
        if (count == 0L) {
          pending$buffer <- NULL
          break
        }
        take <- min(remaining, count - pending$offset)
        parts[[length(parts) + 1L]] <- if (arrow) {
          pending$buffer
        } else {
          pending$buffer[
            seq.int(pending$offset + 1L, pending$offset + take),
            ,
            drop = FALSE
          ]
        }
        remaining <- remaining - take
        fetched <- fetched + take
        pending$offset <- pending$offset + take
        if (pending$offset == count) pending$buffer <- NULL
      }
      output <- if (arrow) {
        # Bound SQL LIMIT by fetched rows, not the caller's numeric allowance.
        duckdb_result_data_frame(parts, pending$schema, fetched, offset)
      } else if (length(parts)) {
        do.call(rbind, parts)
      } else {
        pending$empty
      }
      complete <- TRUE
      output
    }

    # DuckDB R 1.5.6 materializes ordinary DBI results before dbFetch(n).
    # Use its streaming Arrow result until the supported driver includes:
    # https://github.com/duckdb/duckdb-r/pull/2587
    # Then use the ordinary DBI retrieval path and remove its Arrow branches.
    fetch_duckdb <- function() {
      epoch <- result_epoch
      result <- send_query_arrow()
      retained <- FALSE
      on.exit(if (!retained && !is.null(result)) DBI::dbClearResult(result))
      if (result@stmt_lst$return_type != "QUERY_RESULT") {
        return(NULL)
      }

      batch <- DBI::dbFetchArrowChunk(result, chunk_size = preview_rows + 1L)
      schema <- nanoarrow::infer_nanoarrow_schema(batch)
      candidate <- retain_result(result, batch, schema = schema)
      result <- candidate$result
      if (epoch == result_epoch) {
        last_result <<- candidate
        retained <- TRUE
      }
      if (!is.null(batch) && batch$length == 0L) {
        batch <- NULL
      }
      list(schema = schema, batch = batch)
    }

    fetch_dbi <- function() {
      epoch <- result_epoch
      # DBI has no dialect-neutral way to classify a cell before dispatch.
      # Commands that need the statement interface run through DBI from R.
      result <- DBI::dbSendQuery(selected_connection, source)
      retained <- FALSE
      on.exit(if (!retained && !is.null(result)) DBI::dbClearResult(result))
      # DuckDB exposes a Count column even for non-query statements.
      # Fetching those results warns instead of returning a query preview.
      if (
        inherits(result, "duckdb_result") &&
          result@stmt_lst$return_type != "QUERY_RESULT"
      ) {
        return(NULL)
      }
      if (nrow(DBI::dbColumnInfo(result)) == 0L) {
        return(NULL)
      }
      data <- DBI::dbFetch(result, n = preview_rows + 1L)
      candidate <- retain_result(result, data)
      result <- candidate$result
      if (epoch == result_epoch) {
        last_result <<- candidate
        retained <- TRUE
      }
      schema <- nanoarrow::infer_nanoarrow_schema(data)
      batch <- nanoarrow::as_nanoarrow_array(data, schema = schema)
      if (batch$length == 0L) {
        batch <- NULL
      }
      list(schema = schema, batch = batch)
    }

    # Temporary conversion boundary for managed DuckDB's streaming workaround.
    # Ask DuckDB to convert only the fetched rows; never replay the source SQL
    # or use Nanoarrow's R type mappings. Delete when streaming dbFetch(n)
    # replaces retained Arrow retrieval; see the upstream link above.
    duckdb_result_data_frame <- function(batches, schema, n, offset) {
      # Positional scratch names prevent DuckDB from repairing duplicates.
      # Restore the original labels after the driver's data-frame conversion.
      children <- schema$children
      names(children) <- sprintf("column_%02d", seq_along(children))
      scratch_schema <- nanoarrow::nanoarrow_schema_modify(
        schema,
        list(children = children)
      )
      data <- query_arrow(
        batches,
        scratch_schema,
        function(connection, relation) {
          # OFFSET only skips consumed rows in the saved batches, whose order
          # the scratch connection preserves; it never paginates the source SQL.
          paste(
            "SELECT * FROM",
            DBI::dbQuoteIdentifier(connection, relation),
            "LIMIT",
            n,
            "OFFSET",
            offset
          )
        }
      )
      names(data) <- names(schema$children)
      data
    }

    query_arrow <- function(batches, schema, query) {
      render_connection <- ensure_rendering_connection()
      table <- list(schema = schema, batches = batches)
      export_batches <- function(
        table,
        pointer,
        projection = NULL,
        filter = TRUE
      ) {
        stopifnot(isTRUE(filter))
        columns <- if (is.null(projection)) {
          seq_along(table$schema$children)
        } else {
          match(projection, names(table$schema$children))
        }
        stopifnot(!anyNA(columns))
        schema <- nanoarrow::nanoarrow_schema_modify(
          table$schema,
          list(children = table$schema$children[columns])
        )
        arrays <- lapply(table$batches, function(batch) {
          # Keep full column lengths; the SQL query controls row selection.
          array <- nanoarrow::nanoarrow_array_modify(
            batch,
            list(children = batch$children[columns])
          )
          nanoarrow::nanoarrow_array_set_schema(array, schema)
          array
        })
        stream <- nanoarrow::basic_array_stream(arrays, schema = schema)
        nanoarrow::nanoarrow_pointer_export(stream, pointer)
      }
      export_schema <- function(table, pointer) {
        nanoarrow::nanoarrow_pointer_export(table$schema, pointer)
      }
      # This private catalog runs one conversion at a time and unregisters it
      # on exit, so no user-table collision checks or per-cell names are needed.
      relation <- "__mcp_console_arrow"

      # DuckDB's public R wrapper builds Arrow Scanner/Expression callbacks.
      # Export only the fetched C Data batches. Both callers use unfiltered
      # scans, so no filter-expression callbacks or R Arrow package are needed.
      # DuckDB owns conversion to R column types; the user's SQL is never replayed.
      duckdb:::rapi_register_arrow(
        render_connection@conn_ref,
        relation,
        list(export_batches, NULL, NULL, NULL, export_schema),
        table
      )
      on.exit(duckdb::duckdb_unregister_arrow(render_connection, relation))
      DBI::dbGetQuery(render_connection, query(render_connection, relation))
    }

    stringify <- function(batch, schema, rows, columns) {
      names <- sprintf("column_%02d", seq_len(columns))
      truncated_names <- sprintf("truncated_%02d", seq_len(columns))
      children <- schema$children[seq_len(columns)]
      names(children) <- names
      schema <- nanoarrow::nanoarrow_schema_modify(
        schema,
        list(children = children)
      )
      output <- query_arrow(
        list(batch),
        schema,
        function(render_connection, relation) {
          identifiers <- DBI::dbQuoteIdentifier(render_connection, names)
          cast_projection <- paste0(
            "CAST(preview.",
            identifiers,
            " AS VARCHAR) AS ",
            identifiers,
            collapse = ", "
          )
          value_projection <- paste0(
            "CASE WHEN length(strings.",
            identifiers,
            ") > ",
            cell_width,
            " THEN left(strings.",
            identifiers,
            ", ",
            cell_width - 1L,
            ") || '…' ELSE strings.",
            identifiers,
            " END AS ",
            identifiers
          )
          truncated_identifiers <- DBI::dbQuoteIdentifier(
            render_connection,
            truncated_names
          )
          truncated_projection <- paste0(
            "coalesce(length(strings.",
            identifiers,
            ") > ",
            cell_width,
            ", false) AS ",
            truncated_identifiers
          )
          paste(
            "WITH strings AS (SELECT",
            cast_projection,
            "FROM",
            DBI::dbQuoteIdentifier(render_connection, relation),
            "AS preview) SELECT",
            paste(c(value_projection, truncated_projection), collapse = ", "),
            "FROM strings LIMIT",
            rows
          )
        }
      )
      list(
        values = as.list(output[names]),
        truncated = as.list(output[truncated_names])
      )
    }

    format_table <- function(
      values,
      names,
      types,
      truncated,
      rows,
      columns,
      fetched_rows,
      total_columns,
      more_rows
    ) {
      ensure_printer()
      data <- lapply(seq_len(columns), function(column) {
        structure(
          values[[column]][seq_len(rows)],
          class = c("mcp_console_sql_column", "character"),
          arrow_type = types[[column]]
        )
      })
      names(data) <- names[seq_len(columns)]
      table <- tibble::as_tibble(data, .name_repair = "minimal")

      previous_options <- options(
        OutDec = ".",
        cli.num_colors = 1L,
        cli.unicode = TRUE,
        digits = 7L,
        pillar.advice = FALSE,
        pillar.bidi = FALSE,
        pillar.bold = FALSE,
        pillar.max_extra_cols = columns,
        pillar.max_footer_lines = columns + 1L,
        pillar.min_chars = 3L,
        pillar.min_title_chars = 3L,
        pillar.print_max = preview_rows,
        pillar.subtle = FALSE,
        pillar.superdigit_sep = "\u200b",
        pillar.width = preview_width,
        scipen = 0L,
        width = preview_width
      )
      on.exit(options(previous_options))
      output <- paste(
        format(
          table,
          n = rows,
          width = preview_width,
          max_extra_cols = columns,
          max_footer_lines = columns + 1L
        ),
        collapse = "\n"
      )

      markers <- character()
      if (rows == 0L && fetched_rows == 0L) {
        markers <- c(markers, "[0 rows]")
      }
      if (more_rows || rows < fetched_rows) {
        markers <- c(markers, "[additional rows omitted]")
      }
      omitted_columns <- total_columns - columns
      if (omitted_columns > 0L) {
        suffix <- if (omitted_columns == 1L) "column" else "columns"
        markers <- c(
          markers,
          sprintf("[%d additional %s omitted]", omitted_columns, suffix)
        )
      }
      if (
        any(vapply(
          seq_len(columns),
          function(column) any(truncated[[column]][seq_len(rows)]),
          logical(1)
        ))
      ) {
        markers <- c(markers, "[cell values truncated to 160 characters]")
      }
      paste(c(output, markers), collapse = "\n")
    }

    render_preview <- function(preview) {
      schema <- preview$schema
      batch <- preview$batch
      total_columns <- length(schema$children)
      if (total_columns == 0L) {
        return(invisible(NULL))
      }

      columns <- min(total_columns, preview_columns)
      fetched_rows <- if (is.null(batch)) 0L else batch$length
      rows <- min(fetched_rows, preview_rows)
      more_rows <- fetched_rows > preview_rows
      names <- names(schema$children)[seq_len(columns)]
      types <- vapply(
        schema$children[seq_len(columns)],
        arrow_type,
        character(1)
      )

      if (rows == 0L) {
        values <- rep(list(character()), columns)
        truncated <- rep(list(logical()), columns)
      } else {
        # Timestamp schemas carry the source timezone. Preserve it when casting
        # in the scratch catalog rather than querying the live result connection.
        schema_timezones <- function(schema) {
          c(
            if (grepl("^ts[^:]*:.+", schema$format)) {
              sub("^ts[^:]*:", "", schema$format)
            },
            unlist(lapply(schema$children, schema_timezones), use.names = FALSE)
          )
        }
        zones <- unique(schema_timezones(schema))
        if (length(zones) == 1L) {
          rendering <- ensure_rendering_connection()
          DBI::dbExecute(
            rendering,
            paste("SET TimeZone =", DBI::dbQuoteString(rendering, zones))
          )
        }
        cells <- stringify(batch, schema, rows, columns)
        values <- cells$values
        truncated <- cells$truncated
      }

      visible_rows <- rows
      visible_columns <- columns
      repeat {
        output <- format_table(
          values,
          names,
          types,
          truncated,
          visible_rows,
          visible_columns,
          rows,
          total_columns,
          more_rows
        )
        if (nchar(output, type = "bytes") + 1L <= response_bytes) {
          # MCP carries UTF-8 even when R runs in the C locale.
          writeLines(enc2utf8(output), useBytes = TRUE)
          return(invisible(NULL))
        }
        if (visible_rows > 0L) {
          visible_rows <- visible_rows - 1L
        } else if (visible_columns > 1L) {
          visible_columns <- visible_columns - 1L
        } else {
          stop("SQL preview cannot fit within the response budget")
        }
      }
    }

    evaluate_impl <- function() {
      complete <- FALSE
      on.exit(if (!complete) close_last_result())
      tryCatch(
        {
          ensure_connection()
          if (
            !isTRUE(tryCatch(
              DBI::dbIsValid(selected_connection),
              error = function(...) FALSE
            ))
          ) {
            stop(
              paste(
                "The selected SQL connection is no longer valid;",
                "call .console$sql_connection(NULL) to restore DuckDB"
              )
            )
          }

          preview <- if (identical(selected_connection, managed_connection)) {
            fetch_duckdb()
          } else {
            fetch_dbi()
          }
          if (!is.null(preview)) {
            render_preview(preview)
          }
          complete <- TRUE
        },
        error = function(error) {
          cat("Error: ", conditionMessage(error), "\n", sep = "")
        }
      )
      invisible(NULL)
    }

    interrupted <- FALSE

    evaluate <- function(id) {
      interrupted <<- FALSE
      # Observe the condition without handling it; R_tryEval remains the boundary.
      withCallingHandlers(
        evaluate_impl(),
        interrupt = function(condition) interrupted <<- TRUE,
        error = function(condition) interrupted <<- FALSE
      )
    }

    environment()
  },
  envir = base::new.env(parent = base::baseenv())
)
