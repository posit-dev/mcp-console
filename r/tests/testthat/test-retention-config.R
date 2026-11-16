test_that("retention construction and edits preserve omissions and disabled cleanup", {
  config <- ConsoleConfig(retention = list(cache_days = 7))
  expect_identical(as.list(config)$retention, list(cache_days = 7))
  expect_null(config@retention@recordings_days)
  config@retention@recordings_days <- NULL
  value <- jsonlite::fromJSON(
    jsonlite::toJSON(as.list(config), auto_unbox = TRUE),
    simplifyVector = FALSE
  )
  expect_identical(
    value$retention,
    list(cache_days = 7L, recordings_days = NULL)
  )
  expect_identical(as.list(ConsoleConfig(as.list(config))), as.list(config))

  config@retention <- list(cache_days = NULL, recordings_days = 30)
  expect_null(config@retention@cache_days)
  expect_identical(config@retention@recordings_days, 30)
  expect_identical(
    jsonlite::fromJSON(
      jsonlite::toJSON(as.list(config), auto_unbox = TRUE),
      simplifyVector = FALSE
    )$retention,
    list(cache_days = NULL, recordings_days = 30L)
  )
  config@retention@cache_days <- 21L
  expect_identical(
    as.list(config)$retention,
    list(cache_days = 21L, recordings_days = 30)
  )
  expect_identical(
    as.list(ConsoleConfig(retention = list()))$retention,
    structure(list(), names = character())
  )
  expect_false("retention" %in% names(as.list(ConsoleConfig())))
})

test_that("retention accepts positive day counts and rejects invalid settings", {
  for (name in c("cache_days", "recordings_days")) {
    for (value in list(
      0,
      -1,
      1.5,
      Inf,
      NA_real_,
      numeric(),
      c(1, 2),
      TRUE,
      "14",
      2^32
    )) {
      expect_error(
        ConsoleConfig(retention = stats::setNames(list(value), name)),
        name
      )
    }
  }
  expect_error(ConsoleConfig(retention = list(days = 14)), "retention.days")
  config <- ConsoleConfig(retention = list(cache_days = 14))
  expect_error(config@retention@cache_days <- 0, "cache_days")
})

test_that("loaded retention policies preserve null overrides and round trip", {
  with_sandbox_directory({
    binary <- sandbox_test_binary()
    writeLines("retention: {cache_days: 7, recordings_days: 30}", "global.yaml")
    config <- load_console_config(
      global_file = "global.yaml",
      project_file = NULL,
      overrides = list(retention = list(recordings_days = NULL)),
      path = binary
    )
    expect_identical(config@retention@cache_days, 7L)
    expect_null(config@retention@recordings_days)
    jsonlite::write_json(as.list(config), "saved.json", auto_unbox = TRUE)
    reloaded <- load_console_config(
      global_file = NULL,
      project_file = "saved.json",
      path = binary
    )
    expect_identical(as.list(reloaded), as.list(config))
    defaults <- load_console_config(
      global_file = NULL,
      project_file = NULL,
      path = binary
    )
    expect_identical(defaults@retention@cache_days, 14L)
    expect_identical(defaults@retention@recordings_days, 14L)
  })
})
