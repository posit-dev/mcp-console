policy_json <- function(x) {
  as.character(jsonlite::toJSON(as.list(x), auto_unbox = TRUE))
}

test_that("workspace and tempdir selectors preserve their public wire shape", {
  policy <- SandboxPolicy(profile = "workspace", tempdir = "read_write")
  expect_identical(
    as.list(policy),
    list(profile = "workspace", tempdir = "read_write")
  )
  expect_error(SandboxPolicy(tempdir = TRUE), "tempdir")
  expect_error(SandboxPolicy(tempdir = "unmanaged"), "tempdir")
  expect_error(SandboxPolicy(profile = ":workspace"), "profile")
  expect_error(SandboxPolicy(profile = "workspace_write"), "profile")
})

test_that("omitted mappings and explicit empties stay distinct", {
  expect_identical(policy_json(SandboxPolicy()), "{}")
  expect_identical(
    policy_json(SandboxPolicy(filesystem = SandboxFilesystem())),
    '{"filesystem":{}}'
  )
  expect_identical(
    policy_json(SandboxPolicy(
      filesystem = SandboxFilesystem(read_write = character())
    )),
    '{"filesystem":{"read_write":[]}}'
  )
  expect_identical(
    policy_json(SandboxPolicy(network = SandboxNetwork())),
    '{"network":{"proxy":{},"allow_local_binding":"auto"}}'
  )
  expect_identical(
    policy_json(SandboxProxy(domains = SandboxDomains())),
    '{"domains":{}}'
  )
  expect_identical(
    policy_json(SandboxProxy(domains = SandboxDomains(allow = character()))),
    '{"domains":{"allow":[]}}'
  )
})

test_that("local binding accepts auto, null, and booleans without losing null overrides", {
  for (value in list("auto", NULL, TRUE, FALSE)) {
    network <- SandboxNetwork(allow_local_binding = value)
    expected <- if (is.null(value)) "auto" else value
    # R's omitted NULL properties normally disappear from overrides. Serialize
    # this reset as auto so it replaces an earlier boolean during native merge.
    expect_identical(as.list(network)$allow_local_binding, expected)
  }
  for (value in list("true", "false", "AUTO", "", 0, 1, NA, logical())) {
    expect_error(
      SandboxNetwork(allow_local_binding = value),
      "allow_local_binding"
    )
  }
})

test_that("all public fields have the config.yaml wire shape", {
  x <- SandboxPolicy(
    filesystem = SandboxFilesystem(
      read_only = c("./data", "./.claude"),
      read_write = ".",
      deny = "./secrets"
    ),
    network = SandboxNetwork(
      proxy = SandboxProxy(
        mode = "full",
        domains = SandboxDomains(
          allow = "*.example.org",
          deny = "bad.example.org"
        ),
        socks5 = "tcp",
        allow_upstream_proxy = FALSE
      ),
      sockets = SandboxSockets(unix_sockets = "/tmp/a.sock"),
      allow_local_binding = FALSE
    )
  )
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
  expect_identical(as.list(x), expected)
  expect_identical(
    jsonlite::fromJSON(policy_json(x), simplifyVector = FALSE),
    expected
  )
  expect_true(S7::S7_inherits(x, SandboxPolicy))
})

test_that("scalars, lists, and special socket selectors are not conflated", {
  for (network in c("restricted", "enabled")) {
    expect_identical(
      as.list(SandboxPolicy(network = network)),
      list(network = network)
    )
  }
  expect_identical(
    policy_json(SandboxSockets(unix_sockets = character())),
    '{"unix_sockets":[]}'
  )
  expect_identical(
    policy_json(SandboxSockets(unix_sockets = "dangerously_allow_all")),
    '{"unix_sockets":"dangerously_allow_all"}'
  )
  paths <- c(
    named = "./café 雪",
    "../not-created",
    "~/literal",
    "$HOME/literal"
  )
  expect_identical(
    as.list(SandboxFilesystem(read_write = paths))$read_write,
    unname(as.list(paths))
  )
})

test_that("S7 validates both construction and property updates", {
  expect_error(SandboxFilesystem(read_write = NA_character_), "read_write")
  expect_error(SandboxFilesystem(read_write = ""), "read_write")
  expect_error(SandboxFilesystem(read_write = TRUE), "read_write")
  expect_error(SandboxPolicy(filesystem = list()), "filesystem")
  expect_error(SandboxPolicy(network = FALSE), "network")
  expect_error(SandboxPolicy(network = character()), "network")
  expect_error(SandboxPolicy(network = "enable"), "network")
  expect_error(SandboxNetwork(proxy = NULL), "proxy")
  expect_error(
    SandboxNetwork(allow_local_binding = c(TRUE, FALSE)),
    "allow_local_binding"
  )
  expect_error(SandboxProxy(mode = "limited", socks5 = "disabled"), "socks5")
  expect_error(SandboxPolicy(unknown = TRUE), "unused argument")
  x <- SandboxProxy(mode = "full", socks5 = "tcp")
  expect_error(x@mode <- "limited", "socks5")
  expect_error(x@socks5 <- "udp", "socks5")
})

test_that("exported class objects have value semantics and retain context defaults", {
  x <- SandboxPolicy(network = "restricted")
  y <- x
  y@network <- "enabled"
  expect_identical(x@network, "restricted")
  expect_identical(y@network, "enabled")
  # Exactly the same node can be embedded under resolver.sandbox by a caller.
  json <- jsonlite::toJSON(
    list(resolver = list(sandbox = as.list(SandboxPolicy()))),
    auto_unbox = TRUE
  )
  expect_identical(as.character(json), '{"resolver":{"sandbox":{}}}')
})

test_that("invalid public launch options fail before executable resolution", {
  expect_error(
    console_tool(config = list(), path = "missing"),
    "ConsoleConfig"
  )
})
