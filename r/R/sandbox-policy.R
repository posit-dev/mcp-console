# One internal parent gives the public configuration nodes a shared serializer.
# It has no state, defaults, or policy of its own.
ConfigNode <- S7::new_class(
  "ConfigNode",
  abstract = TRUE
)

config_strings <- function(value) {
  if (!is.null(value) && (anyNA(value) || any(!nzchar(value)))) {
    "must contain non-empty strings, without missing values"
  }
}

config_string_list <- function() {
  S7::new_property(NULL | S7::class_character, validator = config_strings)
}

config_choice <- function(choices) {
  force(choices)
  S7::new_property(
    NULL | S7::class_character,
    validator = function(value) {
      if (
        !is.null(value) &&
          (length(value) != 1L || is.na(value) || !value %in% choices)
      ) {
        paste0("must be NULL or one of: ", paste(choices, collapse = ", "))
      }
    }
  )
}

config_flag <- function() {
  S7::new_property(
    NULL | S7::class_logical,
    validator = function(value) {
      if (!is.null(value) && (length(value) != 1L || is.na(value))) {
        "must be NULL, TRUE, or FALSE"
      }
    }
  )
}

#' Configure sandbox filesystem and network access
#'
#' A `SandboxPolicy()` describes which files sandboxed code can read or change
#' and which network connections it can make. Build a policy from the
#' `SandboxFilesystem()` and `SandboxNetwork()` constructors, then pass it to
#' `console_tool(config = ConsoleConfig(sandbox = policy))`. The same object
#' can also be supplied as
#' `ConsoleConfig(resolver = ResolverConfig(sandbox = policy))` to configure
#' dependency preparation separately.
#'
#' With `SandboxPolicy()`, workers can read host files and write their private
#' temporary storage, but cannot write to the project or use the network.
#' Add permissions for the operations your code needs. This default permits
#' reading sensitive files unless you explicitly deny their paths.
#' Passing `ConsoleConfig(sandbox = policy)` skips configuration-file discovery.
#' An explicit policy replaces permissions from any files you opt into reading.
#' To disable sandboxing for both workers and preparation, use
#' `ConsoleConfig(sandbox = FALSE)`.
#'
#' @section Reading and writing files:
#' `SandboxFilesystem()` groups paths by the access you want to permit:
#'
#' * `read_write` permits reads and changes beneath each path. Use `"."` to
#'   permit project writes, or `"./output"` to permit only an output directory.
#' * `read_only` permits reads and prevents writes where it overrides a
#'   broader writable path. For example, permit project writes but protect
#'   `"./data"` with `read_only = "./data"`.
#' * `deny` blocks reading and writing the named paths and their contents.
#'   Use it for files the worker should not see, such as `"./secrets"`.
#'
#' More specific paths take precedence over broader paths. At the same path,
#' `deny` wins over `read_write`, then `read_only`; list order has no effect.
#' Adding a `read_only` path does not make all other host files unreadable.
#' Workers retain their host-read baseline.
#'
#' Paths are literal, without globs or automatic `~` or environment-variable
#' expansion. Relative paths use `console_tool(project = ...)`. Changing the
#' worker's directory later does not move those grants. To deny a home-directory
#' path, construct an
#' absolute path explicitly, for example `file.path(path.expand("~"), ".ssh")`.
#' The wrapper does not resolve symlinks or create configured directories.
#' Create writable directories before launching; directory roots are the
#' portable choice. Linux skips absent write roots and can reject file roots.
#' Linux mount masking can also prevent a narrower read exception beneath a
#' denied directory from being visible.
#'
#' The runner normally protects `.git`, `.agents`, `.codex`, and `.aws` from
#' writes beneath writable roots. Explicit grants can override these protections.
#' If you grant an ancestor of the project, list protected project paths in
#' `read_only` explicitly. Add `"./.claude"` there if it should also be protected.
#'
#' @section Choosing network access:
#' The `network` argument selects one of three arrangements:
#'
#' * `"restricted"` denies ordinary direct network access and starts no proxy.
#'   This is the worker default when `network = NULL`.
#' * `"enabled"` permits direct networking with the host's network access,
#'   without a managed proxy or domain filtering. Filesystem restrictions
#'   still apply; this does not disable the sandbox.
#' * `SandboxNetwork()` enables a managed proxy while restricting direct
#'   remote connections. Use its `proxy` argument to choose destinations and
#'   protocols. Clients use the proxy environment supplied to the worker;
#'   clients that ignore it cannot make ordinary direct remote connections.
#'
#' A bare `SandboxNetwork()` permits no worker proxy destinations. To call one
#' HTTPS API, supply `SandboxProxy(domains = SandboxDomains(allow =
#' "api.example.com"))`. The default proxy mode is `"full"`, which supports
#' HTTPS. An explicit `SandboxDomains()` replaces the destination defaults;
#' an empty `allow` list permits no destinations.
#'
#' @section What the proxy modes mean:
#' `SandboxProxy(mode = "full")` supports HTTP requests of any method and
#' HTTPS through CONNECT tunnels. CONNECT lets the client establish an
#' encrypted connection to an allowed destination; the proxy does not inspect
#' the HTTP methods inside that TLS connection. Destination rules still apply.
#' CONNECT and SOCKS tunnels can carry arbitrary application traffic to allowed
#' destinations.
#' `"full"` means these protocols are available, not that every host is allowed.
#'
#' `mode = "limited"` permits only plain HTTP GET, HEAD, and OPTIONS requests
#' to allowed destinations. The currently pinned runner rejects other methods,
#' including POST and HTTPS CONNECT, and disables SOCKS. Use `"full"` for
#' ordinary HTTPS APIs or package downloads. Limited mode does not provide
#' read-only HTTPS access or a guarantee against server-side changes: even a
#' GET request can have side effects. Direct local or Unix-socket access granted
#' separately is not subject to this HTTP method filter.
#'
#' The `socks5` argument controls an additional SOCKS5 proxy in full mode.
#' SOCKS5 lets a client tunnel protocols other than HTTP to allowed hosts:
#'
#' * `"tcp"` (the full-mode default) enables TCP tunnels.
#' * `"disabled"` disables SOCKS5 while leaving HTTP and HTTPS proxying enabled.
#' * `"tcp_udp"` requests TCP and UDP tunnels, but is unsupported by the
#'   currently pinned runner and fails before the command starts.
#'
#' With `mode = "limited"`, leave `socks5 = NULL`. Any explicitly supplied
#' value, including `"disabled"`, is an error. The limited mode itself disables
#' both SOCKS transports.
#'
#' `allow_upstream_proxy = TRUE` lets the managed proxy route onward through
#' HTTP(S) proxy settings in Console's trusted launch environment. The default
#' is `FALSE`. This can be useful behind a corporate proxy; destination and
#' method restrictions still apply. Environment changes made inside the worker
#' cannot select this upstream hop.
#'
#' @section Allowing and denying destinations:
#' `SandboxDomains(allow = ..., deny = ...)` accepts hostname patterns or IP
#' literals. A destination must match an allow rule and no deny rule. Deny
#' rules take precedence even over a more specific allow rule.
#'
#' * `"api.example.com"` matches only that host.
#' * `"*.example.com"` matches subdomains at any depth, but not `"example.com"`.
#' * `"**.example.com"` matches both the domain and its subdomains.
#' * `"127.0.0.1"` or `"::1"` matches that IP literal.
#'
#' Supply hostnames, not URLs or host-with-port strings: these rules cannot
#' restrict a URL path or destination port. Hosts serving redirects or downloaded
#' artifacts also need allow rules. Hostname matching ignores case and trailing
#' dots. When local binding is disabled, hostnames resolving to private addresses
#' are blocked even if allowlisted; explicitly allowed local IP literals can
#' still be reached through the proxy.
#'
#' @section Local applications and Unix sockets:
#' `SandboxNetwork(allow_local_binding = TRUE)` allows local networking needed
#' by development servers. The default `"auto"` (also selected by `NULL`)
#' preserves `FALSE` for Unix workers and `TRUE` for Unix dependency preparation.
#' Windows MXC proxies require `TRUE`; `"auto"` enables it and explicit `FALSE`
#' is rejected. On macOS, enabling it allows loopback
#' listeners and direct connections to host loopback services, including their
#' HTTP or WebSocket traffic. Those direct connections bypass domain and proxy
#' method filtering. It also relaxes the proxy's private-address checks.
#'
#' For a Shiny app on macOS, allow project writes if needed, prepare `shiny`
#' with `RConfig(packages = "shiny")`, and enable local binding. Run the app with
#' `host = "127.0.0.1"` and `launch.browser = FALSE`, then open its URL in your
#' host browser. The app occupies its Console cell until interrupted; a short
#' call timeout returns while it keeps running. Wait for the `Listening on ...`
#' message before opening the URL; use `send(timeout_ms = 1000L)` to inspect
#' later output while startup is still running. See the complete example below.
#'
#' Linux's managed proxy uses a private network namespace. Local binding does
#' not publish its ports to a host browser. For host-browser Shiny development
#' on Linux, use `SandboxPolicy(network = "enabled")` with the filesystem grants
#' you need; this also permits remote networking.
#'
#' Windows MXC supports managed TCP proxies. Its host-loopback access is
#' bidirectional, so a managed proxy requires local binding; `"auto"` enables
#' it and explicit `FALSE` is rejected. UDP and Unix-socket settings remain
#' unsupported.
#'
#' `SandboxSockets(unix_sockets = ...)` permits direct Unix-domain socket
#' connections, for example to a local service. Use literal absolute paths;
#' `character()` permits none, and `"dangerously_allow_all"` permits all such
#' sockets subject to filesystem permissions. These channels can carry arbitrary
#' traffic and bypass the proxy's HTTP filter. On macOS, native direct grants
#' use subpath matching rather than an exact-path firewall. Linux rejects
#' nonempty path lists; its allow-all setting is supported without path filtering.
#'
#' @section Defaults, omissions, and dependency preparation:
#' Worker permissions and resolver permissions are independent. Package
#' preparation keeps its own host-read/cache-write defaults and, on macOS and
#' Linux, a full proxy with an allowlist for package repositories and downloads.
#' Denying a worker path or hostname does not deny it to preparation. Limit the
#' resolver separately with `ResolverConfig(sandbox = ...)` when needed.
#' Replacing resolver domains removes its repository allowlist. Include every
#' repository, artifact, and redirect host needed by your R, Python, and DuckDB
#' packages; limiting it to PyPI will prevent downloads from CRAN, for example.
#' An omitted resolver filesystem retains cache grants; an explicit filesystem
#' replaces its default entries, so include necessary reads and cache writes.
#' Explicit resolver sandbox policies are unsupported on Windows, where
#' preparation runs with host permissions.
#'
#' `NULL` means omit the property and use that context's default. A constructed
#' empty object is an explicit empty mapping; `character()` is an explicit empty
#' list. For example, `SandboxProxy(domains = NULL)` retains the resolver's
#' generated download allowlist, whereas `SandboxProxy(domains =
#' SandboxDomains(allow = character()))` permits no proxy destinations. In a
#' worker, both have an empty destination list. An explicit network scalar
#' removes the managed proxy, including the resolver's generated download proxy.
#'
#' `as.list(policy)` returns a recursive, JSON-ready application configuration.
#' It preserves omitted fields, empty mappings, and arrays, including singletons.
#' No native defaults are baked into these R objects. Console and its pinned
#' native runner validate and enforce platform capabilities. Unsupported settings
#' fail; the wrapper does not retry with broader permissions.
#'
#' @param read_only,read_write,deny Character vectors of literal filesystem
#'   paths granting reads, reads and writes, or denying both, respectively.
#'   For `SandboxDomains()`, `deny` instead contains hostname patterns or IP
#'   literals whose proxy connections should be blocked.
#' @return An S7 configuration object. The constructor is also its S7 class.
#' @examples
#' # Read host files; write only private temporary storage; no worker network
#' SandboxPolicy()
#'
#' # Add writable roots; keep data read-only and hide secrets
#' files <- SandboxPolicy(
#'   filesystem = SandboxFilesystem(
#'     read_write = c(".", "../shared-output"),
#'     read_only = "./data",
#'     deny = c("./secrets", file.path(path.expand("~"), ".ssh"))
#'   )
#' )
#'
#' # Allow an HTTPS API
#' api <- SandboxPolicy(
#'   network = SandboxNetwork(
#'     proxy = SandboxProxy(
#'       mode = "full",
#'       domains = SandboxDomains(allow = "api.example.com")
#'     )
#'   )
#' )
#'
#' # Allow a domain and its subdomains, except one blocked host
#' sites <- SandboxPolicy(
#'   network = SandboxNetwork(
#'     proxy = SandboxProxy(
#'       domains = SandboxDomains(
#'         allow = "**.example.org",
#'         deny = "blocked.example.org"
#'       )
#'     )
#'   )
#' )
#'
#' # Restrict the proxy to plain HTTP GET, HEAD, and OPTIONS
#' http <- SandboxPolicy(
#'   network = SandboxNetwork(
#'     proxy = SandboxProxy(
#'       mode = "limited",
#'       domains = SandboxDomains(allow = "example.org")
#'     )
#'   )
#' )
#' as.list(http)
#'
#' # Shiny development on macOS, using an existing app directory
#' shiny_policy <- SandboxPolicy(
#'   filesystem = SandboxFilesystem(read_write = "."),
#'   network = SandboxNetwork(allow_local_binding = TRUE)
#' )
#' \dontrun{
#' send <- console_tool(
#'   project = "/path/to/app",
#'   config = ConsoleConfig(
#'     r = RConfig(packages = "shiny"),
#'     sandbox = shiny_policy
#'   )
#' )
#' send(
#'   r = "shiny::runApp('.', host = '127.0.0.1', port = 3838, launch.browser = FALSE)",
#'   timeout_ms = 1000L
#' )
#' # Wait for "Listening on ..."; poll with send(timeout_ms = 1000L) if needed.
#' # Open http://127.0.0.1:3838 in your browser; stop with:
#' send(control = "interrupt")
#' }
#'
#' # Direct networking for host-browser development on Linux
#' shiny_direct <- SandboxPolicy(
#'   filesystem = SandboxFilesystem(read_write = "."),
#'   network = "enabled"
#' )
#'
#' # Restrict resolver downloads to PyPI while retaining cache grants
#' resolver_policy <- SandboxPolicy(
#'   network = SandboxNetwork(
#'     proxy = SandboxProxy(
#'       domains = SandboxDomains(allow = c("pypi.org", "files.pythonhosted.org"))
#'     )
#'   )
#' )
#' ConsoleConfig(resolver = ResolverConfig(sandbox = resolver_policy))
#' @name SandboxPolicy
#' @export
SandboxFilesystem <- S7::new_class(
  "SandboxFilesystem",
  parent = ConfigNode,
  properties = list(
    read_only = config_string_list(),
    read_write = config_string_list(),
    deny = config_string_list()
  )
)

#' @rdname SandboxPolicy
#' @param allow Character vector of hostname patterns or IP literals permitted
#'   by the proxy. Unmatched destinations are denied; matching `deny` rules win.
#' @export
SandboxDomains <- S7::new_class(
  "SandboxDomains",
  parent = ConfigNode,
  properties = list(allow = config_string_list(), deny = config_string_list())
)

#' @rdname SandboxPolicy
#' @param mode `NULL` (defaults to `"full"`), `"full"` for HTTP of any method
#'   and HTTPS CONNECT, or `"limited"` for plain HTTP GET, HEAD, and OPTIONS
#'   only. Limited mode blocks HTTPS and SOCKS in the pinned runner.
#' @param domains `NULL` or a `SandboxDomains()`. An explicit empty object
#'   clears generated domain defaults; `NULL` preserves them.
#' @param socks5 `NULL` (defaults to `"tcp"` in full mode), `"disabled"` for
#'   HTTP/HTTPS proxying alone, `"tcp"` for additional SOCKS5 TCP tunnels, or
#'   `"tcp_udp"` for TCP and UDP (currently unsupported; launch fails).
#'   With `mode = "limited"`, only `NULL` is valid.
#' @param allow_upstream_proxy `NULL`, `TRUE`, or `FALSE`; controls use of an
#'   upstream HTTP(S) proxy from the trusted launch environment. Defaults to
#'   `FALSE`; enabling it retains destination and method restrictions.
#' @export
SandboxProxy <- S7::new_class(
  "SandboxProxy",
  parent = ConfigNode,
  properties = list(
    mode = config_choice(c("full", "limited")),
    domains = NULL | SandboxDomains,
    socks5 = config_choice(c("disabled", "tcp", "tcp_udp")),
    allow_upstream_proxy = config_flag()
  ),
  validator = function(self) {
    if (identical(self@mode, "limited") && !is.null(self@socks5)) {
      "`socks5` must be NULL in limited mode"
    }
  }
)

#' @rdname SandboxPolicy
#' @param unix_sockets `NULL`, a character vector of literal absolute Unix
#'   socket paths (possibly empty), or `"dangerously_allow_all"` for any Unix
#'   socket subject to filesystem permissions. Defaults to no allowed sockets.
#'   Nonempty path lists are unsupported on Linux; see the local-network section.
#' @export
SandboxSockets <- S7::new_class(
  "SandboxSockets",
  parent = ConfigNode,
  properties = list(unix_sockets = config_string_list())
)

#' @rdname SandboxPolicy
#' @param proxy A `SandboxProxy()`; defaults to an explicit empty proxy
#'   mapping. A network mapping always enables a managed proxy. Use a scalar
#'   network choice in `SandboxPolicy()` for proxy-free networking.
#' @param sockets `NULL` or a `SandboxSockets()`.
#' @param allow_local_binding `"auto"` (the default) or `NULL` to select the
#'   platform/context default, `TRUE` to permit local networking and relax private-address
#'   checks, or `FALSE` to restrict it. On macOS, `TRUE` permits loopback listeners
#'   and direct host-loopback connections. Linux managed-proxy ports remain
#'   inside a private network namespace.
#' @export
SandboxNetwork <- S7::new_class(
  "SandboxNetwork",
  parent = ConfigNode,
  properties = list(
    proxy = S7::new_property(SandboxProxy, default = quote(SandboxProxy())),
    sockets = NULL | SandboxSockets,
    allow_local_binding = S7::new_property(
      NULL | S7::class_character | S7::class_logical,
      default = "auto",
      validator = function(value) {
        if (
          !is.null(value) &&
            (length(value) != 1L ||
              is.na(value) ||
              (is.character(value) && value != "auto"))
        ) {
          "must be NULL, auto, TRUE, or FALSE"
        }
      }
    )
  )
)

#' @rdname SandboxPolicy
#' @param filesystem `NULL` or a `SandboxFilesystem()`.
#' @param network `NULL`, `"restricted"`, `"enabled"`, or a
#'   `SandboxNetwork()`: preserve context defaults, deny ordinary direct network
#'   access without a proxy, permit direct host networking, or enable a managed
#'   proxy, respectively. Filesystem enforcement remains in place.
#' @param profile `NULL` to preserve context defaults or `"workspace"` for
#'   the native workspace profile. Filesystem entries compose with the profile.
#' @param tempdir `NULL` to preserve context defaults, `"managed"` (the default) for private
#'   writable storage owned and cleaned up by the active sandbox, `"read_write"` for
#'   automatic write grants to normal host temp locations without Console-owned
#'   cleanup, or `"read_only"` to omit automatic temp grants. Explicit filesystem
#'   permissions still apply. Without an active sandbox and managed storage,
#'   Console adds no temporary-directory cleanup guarantees; R uses its normal
#'   best-effort session-tempdir cleanup.
#' @export
SandboxPolicy <- S7::new_class(
  "SandboxPolicy",
  parent = ConfigNode,
  properties = list(
    filesystem = NULL | SandboxFilesystem,
    network = S7::new_property(
      NULL | S7::class_character | SandboxNetwork,
      validator = function(value) {
        if (
          is.character(value) &&
            (length(value) != 1L ||
              is.na(value) ||
              !value %in% c("restricted", "enabled"))
        ) {
          "must be NULL, restricted, enabled, or a SandboxNetwork()"
        }
      }
    ),
    profile = config_choice("workspace"),
    tempdir = config_choice(c("managed", "read_write", "read_only"))
  )
)

# Wire conversion preserves omissions, mappings, and arrays; native validation
# and defaults remain the CLI's responsibility.
config_mapping <- function(fields) {
  # NULL is an explicit reset here, not an omitted property. Keep the auto
  # selector in layered overrides; native capture owns platform resolution.
  if (
    "allow_local_binding" %in%
      names(fields) &&
      is.null(fields$allow_local_binding)
  ) {
    fields$allow_local_binding <- "auto"
  }
  fields <- fields[!vapply(fields, is.null, logical(1))]
  arrays <- c(
    "read_only",
    "read_write",
    "deny",
    "allow",
    "unix_sockets",
    "packages",
    "languages"
  )
  for (name in names(fields)) {
    value <- fields[[name]]
    if (S7::S7_inherits(value, ConfigNode)) {
      fields[[name]] <- as.list(value)
    } else if (name == "environment") {
      fields[[name]] <- json_object(as.list(value))
    } else if (name == "parameters") {
      fields[[name]] <- json_object(lapply(value, as.list))
    } else if (name == "schema") {
      fields[[name]] <- json_object(value)
    } else if (name == "first_available") {
      fields[[name]] <- unname(lapply(value, function(candidate) {
        if (S7::S7_inherits(candidate, ConfigNode)) {
          as.list(candidate)
        } else {
          candidate
        }
      }))
    } else if (
      name %in%
        arrays &&
        !(name == "unix_sockets" &&
          identical(unname(value), "dangerously_allow_all"))
    ) {
      fields[[name]] <- unname(as.list(value))
    } else {
      fields[[name]] <- unname(value)
    }
  }
  json_object(fields)
}

S7::method(as.list, ConfigNode) <- function(x, ...) {
  S7::validate(x)
  config_mapping(S7::props(x))
}

config_override <- function(key, value) {
  paste0(key, "=", jsonlite::toJSON(value, auto_unbox = TRUE))
}
