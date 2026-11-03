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
      proxy = list(
        mode = "full",
        domains = list(allow = "*.example.org", deny = "bad.example.org"),
        socks5 = "tcp",
        allow_upstream_proxy = FALSE
      ),
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
      proxy = list(
        mode = "full",
        domains = list(
          allow = list("*.example.org"),
          deny = list("bad.example.org")
        ),
        socks5 = "tcp",
        allow_upstream_proxy = FALSE
      ),
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
      sandbox = list(network = list(allow = "api.example.org"))
    ),
    "sandbox.network.allow"
  )
})

test_that("network shorthands retain canonical modes and proxy domain rules", {
  for (network in list(TRUE, "enabled", FALSE, "restricted")) {
    expected <- if (isTRUE(network) || identical(network, "enabled")) {
      "enabled"
    } else {
      "restricted"
    }
    config <- ConsoleConfig(sandbox = list(network = network))
    expect_identical(config@sandbox@network, expected)
    expect_identical(as.list(config)$sandbox$network, expected)
    config@sandbox@network <- "api.example.org"
    config@sandbox@network <- network
    expect_identical(as.list(config)$sandbox$network, expected)
  }
  for (network in list(
    "api.example.org",
    c("api.example.org", "*.example.com"),
    list("api.example.org", "*.example.com"),
    character(),
    list()
  )) {
    expected <- list(proxy = list(domains = list(allow = as.list(network))))
    config <- ConsoleConfig(sandbox = list(network = network))
    expect_identical(as.list(config)$sandbox$network, expected)
    config@sandbox@network <- FALSE
    config@sandbox@network <- network
    expect_identical(as.list(config)$sandbox$network, expected)
  }
  for (network in list(NA, c(TRUE, FALSE))) {
    expect_error(
      ConsoleConfig(sandbox = list(network = network)),
      "sandbox.network.*named mapping"
    )
  }
  for (network in list(NA_character_, "")) {
    expect_error(
      ConsoleConfig(sandbox = list(network = network)),
      "@allow must contain non-empty strings"
    )
  }
  expect_error(
    ConsoleConfig(sandbox = list(network = list(1))),
    "sandbox.network.proxy.domains.allow.*contain strings"
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
    '{"sandbox":{"network":{"proxy":{"domains":{"allow":[]}}}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(network = structure(list(), names = character()))
    )),
    '{"sandbox":{"network":{}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(
        network = list(proxy = list(domains = list(deny = character())))
      )
    )),
    '{"sandbox":{"network":{"proxy":{"domains":{"deny":[]}}}}}'
  )
  expect_identical(
    policy_json(ConsoleConfig(
      sandbox = list(network = list(proxy = list(domains = list())))
    )),
    '{"sandbox":{"network":{"proxy":{"domains":{}}}}}'
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
  expect_error(
    ConsoleConfig(
      sandbox = list(
        network = list(proxy = list(mode = "limited", socks5 = "disabled"))
      )
    ),
    "socks5"
  )
  config <- ConsoleConfig(
    sandbox = list(network = "api.example.org")
  )
  config@sandbox@filesystem <- list(read_write = "./reports")
  config@sandbox@network@proxy@mode <- "limited"
  expect_identical(config@sandbox@filesystem@read_write, "./reports")
  expect_identical(config@sandbox@network@proxy@mode, "limited")
  expect_error(
    config@sandbox@filesystem@read_write <- NA_character_,
    "read_write"
  )
  expect_error(config@sandbox@network@proxy@socks5 <- "tcp", "socks5")
  `append<-` <- function(x, value) c(x, value)
  append(config@sandbox@network@proxy@domains@allow) <- "data.example.org"
  expect_identical(
    config@sandbox@network@proxy@domains@allow,
    c("api.example.org", "data.example.org")
  )
  config@sandbox@network@proxy <- list(
    mode = "full",
    domains = list(allow = "replacement.example.org")
  )
  expect_identical(
    config@sandbox@network@proxy@domains@allow,
    "replacement.example.org"
  )
  config@sandbox@network@proxy@domains <- list(deny = "blocked.example.org")
  expect_identical(
    as.list(config)$sandbox$network$proxy$domains,
    list(deny = list("blocked.example.org"))
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
  config@resolver@sandbox@network <- list("packages.example.org")
  expect_identical(config@sandbox@network, "restricted")
  expect_identical(
    as.list(config)$resolver$sandbox$network,
    list(proxy = list(domains = list(allow = list("packages.example.org"))))
  )
})

test_that("invalid public launch options fail before executable resolution", {
  expect_error(
    console_tools(config = list(), path = "missing"),
    "ConsoleConfig"
  )
})
