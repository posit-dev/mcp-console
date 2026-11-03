test_that("console_tools discovers, registers, and calls all tools across pages", {
  with_console_config_probe(
    function(path, record) {
      tools <- console_tools(path = path, config = ConsoleConfig())
      expect_named(tools, c("evaluate", "inspect"))
      expect_identical(tools$evaluate@description, "Evaluate values")
      expect_identical(tools$inspect@description, "")
      expect_true(tools$evaluate@arguments@properties$values@required)
      expect_identical(
        tools$evaluate@arguments@properties$values@json$type,
        list("array", "null")
      )
      expect_false(tools$evaluate@arguments@properties$options@required)
      expect_error(tools$evaluate(), "values")
      expect_identical(
        tools$evaluate(
          values = "one",
          options = list(labels = character())
        )@text,
        '{"name":"evaluate","arguments":{"values":["one"],"options":{"labels":[]}}}'
      )
      expect_identical(
        tools$evaluate(
          values = "one",
          client = NULL,
          options = list(label = NULL)
        )@text,
        '{"name":"evaluate","arguments":{"values":["one"],"client":null,"options":{"label":null}}}'
      )
      expect_identical(
        tools$evaluate(
          values = "one",
          fields = "f",
          metadata = "m",
          arguments = "a"
        )@text,
        '{"name":"evaluate","arguments":{"values":["one"],"fields":"f","metadata":"m","arguments":"a"}}'
      )
      expect_identical(
        tools$evaluate(values = "one", fields = NULL)@text,
        '{"name":"evaluate","arguments":{"values":["one"]}}'
      )
      expect_identical(
        tools$evaluate(values = NULL)@text,
        '{"name":"evaluate","arguments":{"values":null}}'
      )
      expect_identical(
        tools$evaluate(
          values = "one",
          extras = list(answer = 42L, absent = NULL)
        )@text,
        '{"name":"evaluate","arguments":{"values":["one"],"extras":{"answer":42,"absent":null}}}'
      )

      chat <- ellmer::chat_openai_compatible(
        base_url = "https://example.test/v1",
        model = "mock",
        credentials = function() "unused",
        echo = "none"
      )
      chat$register_tools(tools)
      expect_named(chat$get_tools(), c("evaluate", "inspect"))
      expect_identical(
        chat$get_tools()$inspect()@text,
        '{"name":"inspect","arguments":{}}'
      )
      evaluate <- tools$evaluate
      rm(tools, chat)
      invisible(gc())
      expect_false(file.exists(paste0(record, ".closed")))
      expect_identical(
        evaluate(values = character())@text,
        '{"name":"evaluate","arguments":{"values":[]}}'
      )
      rm(evaluate)
      invisible(gc())
      expect_identical(readLines(paste0(record, ".closed")), "closed")
    },
    fixture = "console-tools-probe.R"
  )
})

test_that("tool discovery reserves all unused arguments", {
  expect_error(
    console_tools(tool = "inspect", config = ConsoleConfig(), path = "missing"),
    "must be empty"
  )
})

test_that("ellmer chat preserves required and optional nullable arguments", {
  with_console_config_probe(
    function(path, record) {
      tools <- console_tools(path = path, config = ConsoleConfig())
      fields <- names(tools$evaluate@arguments@properties)
      nullable <- stats::setNames(rep(list(NULL), length(fields)), fields)
      non_null <- nullable
      non_null["values"] <- list(list("one"))
      results <- mock_console_chat(
        tools,
        list(
          list(name = "evaluate", arguments = non_null),
          list(name = "evaluate", arguments = nullable)
        )
      )
      expect_null(results[[1L]]@error)
      expect_identical(
        results[[1L]]@value@text,
        '{"name":"evaluate","arguments":{"values":["one"],"client":null}}'
      )
      expect_null(results[[2L]]@error)
      expect_identical(
        results[[2L]]@value@text,
        '{"name":"evaluate","arguments":{"values":null,"client":null}}'
      )
      rm(tools, results)
      invisible(gc())
    },
    fixture = "console-tools-probe.R"
  )
})

test_that("provider requests preserve nullable schemas and provider optionality", {
  with_console_config_probe(
    function(path, record) {
      tools <- console_tools(path = path, config = ConsoleConfig())
      providers <- c(
        anthropic = FALSE,
        openai = TRUE,
        compatible = TRUE,
        compatible_nonstrict = FALSE
      )
      for (provider in names(providers)) {
        strict <- providers[[provider]]
        protocol <- if (startsWith(provider, "compatible")) {
          "compatible"
        } else {
          provider
        }
        chat <- switch(
          protocol,
          anthropic = ellmer::chat_anthropic(
            base_url = "https://example.test/v1",
            model = "mock",
            credentials = function() "unused",
            cache = "none",
            echo = "none"
          ),
          openai = ellmer::chat_openai(
            base_url = "https://example.test/v1",
            model = "mock",
            credentials = function() "unused",
            echo = "none"
          ),
          compatible = mock_compatible_chat(strict = strict)
        )
        nullable <- list(
          values = NULL,
          client = NULL,
          options = list(label = NULL)
        )
        if (strict) {
          fields <- names(tools$evaluate@arguments@properties)
          nullable <- stats::setNames(rep(list(NULL), length(fields)), fields)
          nullable$options <- list(labels = NULL, label = NULL)
        }
        non_null <- nullable
        non_null["values"] <- list(list("one"))
        non_null$client <- "client"
        non_null$options <- list(labels = list("label"), label = "option")
        calls <- list(nullable, non_null)
        responses <- lapply(seq_along(calls), function(index) {
          id <- paste0("call_", index)
          arguments <- jsonlite::toJSON(
            calls[[index]],
            auto_unbox = TRUE,
            null = "null"
          )
          switch(
            protocol,
            anthropic = mock_anthropic_message(
              list(list(
                type = "tool_use",
                id = id,
                name = "evaluate",
                input = calls[[index]]
              )),
              "tool_use"
            ),
            openai = mock_openai_response(list(list(
              type = "function_call",
              id = id,
              call_id = id,
              name = "evaluate",
              arguments = arguments
            ))),
            compatible = mock_completion(
              list(
                role = "assistant",
                content = NULL,
                tool_calls = list(list(
                  id = id,
                  type = "function",
                  `function` = list(name = "evaluate", arguments = arguments)
                ))
              ),
              "tool_calls"
            )
          )
        })
        responses[[3L]] <- switch(
          protocol,
          anthropic = mock_anthropic_message(
            list(list(type = "text", text = "done")),
            "end_turn"
          ),
          openai = mock_openai_response(list(list(
            type = "message",
            role = "assistant",
            content = list(list(type = "output_text", text = "done"))
          ))),
          compatible = mock_completion(
            list(role = "assistant", content = "done"),
            "stop"
          )
        )
        requests <- list()
        httr2::local_mocked_responses(function(req) {
          requests[[length(requests) + 1L]] <<- httr2::req_get_body(req)
          httr2::response(
            headers = list(`Content-Type` = "application/json"),
            body = charToRaw(jsonlite::toJSON(
              responses[[length(requests)]],
              auto_unbox = TRUE,
              null = "null"
            ))
          )
        })
        chat$register_tools(tools)
        tool_results <- list()
        chat$on_tool_result(function(result) {
          tool_results[[length(tool_results) + 1L]] <<- result
        })
        expect_identical(
          as.character(chat$chat("Use the console tools.")),
          "done"
        )
        expect_identical(
          lapply(tool_results, function(result) result@error),
          list(NULL, NULL)
        )
        expect_identical(
          lapply(tool_results, function(result) result@value@text),
          list(
            '{"name":"evaluate","arguments":{"values":null,"client":null,"options":{"label":null}}}',
            '{"name":"evaluate","arguments":{"values":["one"],"client":"client","options":{"labels":["label"],"label":"option"}}}'
          )
        )
        expect_length(requests, 3L)
        definitions <- requests[[1L]]$tools
        if (protocol == "compatible") {
          definitions <- lapply(definitions, function(tool) tool[["function"]])
        }
        names(definitions) <- vapply(
          definitions,
          function(tool) tool$name,
          character(1)
        )
        expect_named(definitions, c("evaluate", "inspect"))
        schema <- if (protocol == "anthropic") {
          definitions$evaluate$input_schema
        } else {
          definitions$evaluate$parameters
        }
        expect_identical(
          unlist(schema$properties$client$type, use.names = FALSE),
          c("string", "null"),
          info = provider
        )
        expect_identical(
          unlist(schema$properties$values$type, use.names = FALSE),
          c("array", "null"),
          info = provider
        )
        options <- schema$properties$options
        expect_identical(
          list(
            options = unlist(options$type, use.names = FALSE),
            labels = unlist(options$properties$labels$type, use.names = FALSE),
            label = unlist(options$properties$label$type, use.names = FALSE)
          ),
          list(
            options = c("object", if (strict) "null"),
            labels = c("array", if (strict) "null"),
            label = c("string", "null")
          ),
          info = provider
        )
        expect_setequal(
          unlist(schema$required, use.names = FALSE),
          if (strict) names(schema$properties) else "values"
        )
        if (strict) {
          expect_setequal(
            unlist(options$required, use.names = FALSE),
            names(options$properties)
          )
        } else {
          expect_null(options$required)
        }
      }
      rm(tools, chat, tool_results)
      invisible(gc())
    },
    fixture = "console-tools-probe.R"
  )
})
