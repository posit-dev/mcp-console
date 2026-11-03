mock_completion <- function(message, finish_reason) {
  list(
    id = "chatcmpl-test",
    object = "chat.completion",
    created = 0L,
    model = "mock",
    choices = list(list(
      index = 0L,
      message = message,
      finish_reason = finish_reason
    )),
    usage = list(
      prompt_tokens = 0L,
      completion_tokens = 0L,
      total_tokens = 0L
    )
  )
}

mock_console_chat <- function(tools, calls) {
  responses <- lapply(seq_along(calls), function(index) {
    call <- calls[[index]]
    mock_completion(
      list(
        role = "assistant",
        content = NULL,
        tool_calls = list(list(
          id = paste0("call_", index),
          type = "function",
          `function` = list(
            name = call$name,
            arguments = jsonlite::toJSON(
              call$arguments,
              auto_unbox = TRUE,
              null = "null"
            )
          )
        ))
      ),
      "tool_calls"
    )
  })
  responses[[length(responses) + 1L]] <- mock_completion(
    list(role = "assistant", content = "done"),
    "stop"
  )
  index <- 0L
  httr2::local_mocked_responses(function(req) {
    index <<- index + 1L
    httr2::response_json(body = responses[[index]])
  })
  chat <- ellmer::chat_openai_compatible(
    base_url = "https://example.test/v1",
    model = "mock",
    credentials = function() "unused",
    echo = "none"
  )
  chat$register_tools(tools)
  results <- list()
  chat$on_tool_result(function(result) {
    results[[length(results) + 1L]] <<- result
  })
  expect_identical(as.character(chat$chat("Use the console tools.")), "done")
  expect_length(results, length(calls))
  results
}
