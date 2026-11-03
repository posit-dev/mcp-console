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
