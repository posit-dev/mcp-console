policy_json <- function(x) {
  as.character(jsonlite::toJSON(as.list(x), auto_unbox = TRUE))
}

test_that("sandbox mappings use the same shape as config.yaml", {
  fields <- list(
    filesystem = list(
      read_only = c("./data", "./.claude"),
      read_write = ".",
      deny = "./secrets"
    ),
    network = list(
      allow = "*.example.org",
      deny = "bad.example.org",
      proxy = list(mode = "full", socks5 = "tcp", allow_upstream_proxy = FALSE),
      sockets = list(unix_sockets = "/tmp/a.sock"),
      allow_local_binding = FALSE
    )
  )
  config <- ConsoleConfig(sandbox = fields)
  expected <- list(
    filesystem = list(
      read_only = list("./data", "./.claude"),
      read_write = list("."),
      deny = list("./secrets")
    ),
    network = list(
      allow = list("*.example.org"),
      deny = list("bad.example.org"),
      proxy = list(mode = "full", socks5 = "tcp", allow_upstream_proxy = FALSE),
      sockets = list(unix_sockets = list("/tmp/a.sock")),
      allow_local_binding = FALSE
    )
  )
  expect_identical(as.list(config)$sandbox, expected)
  expect_identical(
    jsonlite::fromJSON(policy_json(config), simplifyVector = FALSE)$sandbox,
    expected
  )
  expect_error(
    ConsoleConfig(
      sandbox = list(network = list(proxy = list(domains = list())))
    ),
    "sandbox.network.proxy.domains"
  )
})

test_that("omitted mappings and explicit empty arrays stay distinct", {
  expect_identical(policy_json(ConsoleConfig()), "{}")
  expect_identical(
    policy_json(ConsoleConfig(sandbox = list(filesystem = list()))),
    '{"sandbox":{"filesystem":{}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(filesystem = list(read_write = character()))
    )),
    '{"sandbox":{"filesystem":{"read_write":[]}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(sandbox = list(network = list()))),
    '{"sandbox":{"network":{}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(network = list(allow = character()))
    )),
    '{"sandbox":{"network":{"allow":[]}}}'
  )
})

test_that("client profiles and temporary storage retain their YAML shape", {
  for (profile in c("read_only", "workspace", "inherit_codex")) {
    expect_identical(
      as.list(ConsoleConfig(sandbox = list(profile = profile)))$sandbox,
      list(profile = profile)
    )
  }
  fields <- list(profile = list(inherit_codex = list(fallback = "workspace")))
  expect_identical(as.list(ConsoleConfig(sandbox = fields))$sandbox, fields)
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(profile = list(inherit_codex = list()))
    )),
    '{"sandbox":{"profile":{"inherit_codex":{}}}}'
  )
  expect_error(ConsoleConfig(sandbox = list(profile = "codex")), "profile")
  expect_error(
    ConsoleConfig(
      sandbox = list(profile = list(inherit_codex = list(fallback = "future")))
    ),
    "fallback"
  )
  for (tempdir in c("managed", "read_write", "read_only")) {
    expect_identical(
      ConsoleConfig(sandbox = list(tempdir = tempdir))@sandbox@tempdir,
      tempdir
    )
  }
  expect_error(ConsoleConfig(sandbox = list(tempdir = TRUE)), "tempdir")
})

test_that("local binding preserves omission and explicit resets", {
  expect_null(
    ConsoleConfig(
      sandbox = list(network = list())
    )@sandbox@network@allow_local_binding
  )
  for (value in list("auto", NULL, TRUE, FALSE)) {
    config <- ConsoleConfig(
      sandbox = list(network = list(allow_local_binding = value))
    )
    expected <- if (is.null(value)) "auto" else value
    expect_identical(
      as.list(config)$sandbox$network$allow_local_binding,
      expected
    )
  }
  for (value in list("true", "false", "AUTO", "", 0, 1, NA, logical())) {
    expect_error(
      ConsoleConfig(
        sandbox = list(network = list(allow_local_binding = value))
      ),
      "allow_local_binding"
    )
  }
})

test_that("network scalars, socket selectors, and literal paths stay distinct", {
  for (network in c("restricted", "enabled")) {
    expect_identical(
      as.list(ConsoleConfig(sandbox = list(network = network)))$sandbox,
      list(network = network)
    )
  }
  for (sockets in list(character(), "dangerously_allow_all")) {
    config <- ConsoleConfig(
      sandbox = list(network = list(sockets = list(unix_sockets = sockets)))
    )
    expected <- if (length(sockets)) sockets else list()
    expect_identical(
      as.list(config)$sandbox$network$sockets$unix_sockets,
      expected
    )
  }
  paths <- c(
    named = "./café 雪",
    "../not-created",
    "~/literal",
    "$HOME/literal"
  )
  config <- ConsoleConfig(sandbox = list(filesystem = list(read_write = paths)))
  expect_identical(
    as.list(config)$sandbox$filesystem$read_write,
    unname(as.list(paths))
  )
})

test_that("typed nodes validate construction and concise replacement edits", {
  for (value in list(NA_character_, "", TRUE)) {
    expect_error(
      ConsoleConfig(sandbox = list(filesystem = list(read_write = value))),
      "read_write"
    )
  }
  for (value in list(FALSE, character(), "enable")) {
    expect_error(ConsoleConfig(sandbox = list(network = value)), "network")
  }
  expect_error(
    ConsoleConfig(
      sandbox = list(
        network = list(proxy = list(mode = "limited", socks5 = "disabled"))
      )
    ),
    "socks5"
  )
  config <- ConsoleConfig(
    sandbox = list(network = list(allow = "api.example.org"))
  )
  config@sandbox@filesystem <- list(read_write = "./reports")
  config@sandbox@network@proxy <- list(mode = "limited")
  expect_identical(config@sandbox@filesystem@read_write, "./reports")
  expect_identical(config@sandbox@network@proxy@mode, "limited")
  expect_error(
    config@sandbox@filesystem@read_write <- NA_character_,
    "read_write"
  )
  expect_error(config@sandbox@network@proxy@socks5 <- "tcp", "socks5")
  `append<-` <- function(x, value) c(x, value)
  append(config@sandbox@network@allow) <- "data.example.org"
  expect_identical(
    config@sandbox@network@allow,
    c("api.example.org", "data.example.org")
  )
})

test_that("configurations retain value semantics and separate resolver policy", {
  config <- ConsoleConfig(
    sandbox = list(network = "restricted"),
    resolver = list(sandbox = list())
  )
  copy <- config
  copy@sandbox@network <- "enabled"
  expect_identical(config@sandbox@network, "restricted")
  expect_identical(copy@sandbox@network, "enabled")
  expect_identical(
    as.list(config)$resolver,
    list(sandbox = structure(list(), names = character()))
  )
})

test_that("invalid public launch options fail before executable resolution", {
  expect_error(
    console_tools(config = list(), path = "missing"),
    "ConsoleConfig"
  )
})
