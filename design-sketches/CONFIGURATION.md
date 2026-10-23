# MCP Console configuration

**Status:** Exploratory design sketch.
The small built-in-selection workflow in section 2 is implemented; the broader profile system below remains proposed. \
**Configuration file:** `.agents/console/config.yaml` \
**Design date:** 2026-09-09

**Revised:** 2026-09-11

This document proposes extensions to the implemented configuration interface.
Current fields, native composition, and platform limitations are defined in [Sandbox configuration](../docs/SANDBOX_CONFIGURATION.md).
It covers both near-term configuration and capabilities that will arrive through later pull requests.
Examples describe the proposed interface, not commands or options that can all be used in the current release.
This is an exploratory sketch: it need not settle every edge case or agree with other sketches.
Production code and documentation of implemented behavior retain their normal correctness and consistency requirements.

The design has one central unit: **a profile is a complete session definition**.
It describes permissions, where the relay and worker run, how they are sandboxed, their language environments, and resource limits.
Recording and cleanup settings apply to the project's one Console directory, independently of the selected profile.
Most configurations define that profile directly, without a `profiles` wrapper.
Reusable definitions are available when duplication becomes inconvenient.

## 1. Recommendation

Use YAML for ordinary configuration loading and profile selection.
Keep all MCP Console-managed configuration, state, records, and preparation artifacts under `.agents/console/`.
The native `":workspace"` profile protects `.agents`, including that whole directory, by default.
Explicit native write entries can override these defaults; they are not mandatory denial ceilings.

MCP Console is a thin intermediary between the user and the extracted sandbox runner.
It passes the native baseline and explicit adjustments to the sandbox, which owns policy parsing, normalization, compatibility checks, and enforcement.
Console forwards the sandbox's result and diagnostics without independently deciding whether a policy combination is safe.
Ordinary YAML loading, selecting a session, and arranging its inputs do not require another policy engine.
The permission examples below illustrate the proposed user interface; their supported forms and semantics come from the sandbox.

Use these terms consistently:

| Term                     | Meaning                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ |
| **Profile**              | A complete, selectable session definition.                                                                   |
| **Permissions**          | The filesystem, network, listener, and local-IPC access requested for the session.                           |
| **Target**               | Where the relay and worker run: transport, compute environment, and workspace.                               |
| **Transport**            | How the controller reaches the compute host: local or SSH initially.                                         |
| **Compute**              | The execution environment on that host: host process, Docker container, Docker Sandbox, or another provider. |
| **Sandbox provider**     | The implementation that enforces the requested permissions in that environment.                              |
| **Language environment** | The R installation and libraries, Python installation and environment, and SQL connections.                  |
| **Controller**           | The MCP server, which remains outside the worker sandbox.                                                    |
| **Launcher**             | The public `mcp-console sandbox` boundary and its provider adapters.                                         |
| **Runner / manager**     | The selected sandbox implementation and the component responsible for supervision and cleanup.               |

Do not use “environment” to mean all of a remote host, a Python virtual environment, and a permissions policy.
Do not require users to understand Codex internal structs or platform-specific sandbox flags.

The most consequential choices are:

- No configuration preserves the existing host-read policy with private writable temporary storage and restricted networking.
- Console reads the selected configuration and passes its requested policy to the sandbox.
- `":workspace"` supplies native metadata defaults for `.git`, `.agents`, and `.codex`; Console adds `.claude` as a native read entry.
  Deliberate write exceptions follow native precedence.
- Workers can read the whole managed directory, including current and previous session records, unless the user explicitly restricts reads.
- The configuration contains no secrets; SSH uses an already configured passwordless connection.
- The sandbox decides which policy combinations it accepts; Console forwards any rejection to the caller.
  There is no automatic unsandboxed or unrestricted-network fallback.
- A profile or target change replaces the worker generation.
  It does not mutate the security boundary around an existing R/Python session.

## 2. The common case

The implemented project-editing configuration is one line:

```yaml
extends: :workspace
```

This grants writes beneath the fixed launch workspace, with `.git`, `.agents`, `.codex`, and `.claude` readable and protected from writes by default.
The native constructors supply the first three defaults; Console supplies `.claude` through the native adjustment interface.
The `.agents` default covers Console's configuration and managed state without a redundant `.agents/console` rule.
Explicit writes to a protected path or descendant can override these defaults using native precedence.
An ordinary native `read` entry also grants reads; it is not an independent write-only denial.
See the implemented documentation for Linux read-mask limitations.

The colon is part of each native built-in identifier.
Examples quote the identifiers; valid unquoted YAML is handled by the ordinary parser.
`extends: ":read-only"` selects the native read-only baseline, and omission preserves the existing no-profile defaults.
No version field is required.
The captured workspace stays fixed across worker directory changes and replacements.
Networking remains restricted by default, and both upstream shared-temp switches are explicitly excluded while runner-owned private temporary storage stays writable.

The initializer, custom profiles, additional permission syntax, targets, and other examples below are proposals, not implemented configuration.

For a project that needs only one API, replace the `network` block with:

```yaml
permissions:
  network:
    mode: allowlist
    allow: [https://api.example.com]
```

For a remote workspace, add:

```yaml
target:
  transport: {kind: ssh, host: research-box}
  workspace: /srv/projects/analysis
```

`research-box` is an OpenSSH host alias.
The permission paths now refer to the remote workspace and the remote user's home.
This does **not** upload the local project or select an unsandboxed remote execution path.

The following proposal examples are configuration fragments unless shown with `version: 1`; the implemented one-line starter above is complete.
They are meant to be merged into the surrounding file, not appended as duplicate YAML keys.

## 3. Relationship to existing systems

### Codex

Codex currently exposes built-in permission profiles named `":read-only"`, `":workspace"`, and `":danger-full-access"`, as well as custom permission profiles with `extends`, filesystem rules, and network rules.
Its newer permission profiles are distinct from its older `sandbox_mode` settings.
The extracted sandbox runner owns the supported policy model; Console exposes those settings without implementing a second permission model.[1]

MCP Console uses the upstream identifiers directly: `":read-only"` and `":workspace"` are implemented.
`":danger-full-access"` below is a proposal; current unrestricted and external filesystem requests use the native `sandbox.filesystem.kind` interface.
Rust constructor names are implementation details, not configuration aliases.

### Anthropic Sandbox Runtime

The supplied repository now redirects to `anthropics/sandbox-runtime`.
Its `allowWrite`, `denyWrite`, `denyRead`, domain rules, local binding, and Unix socket controls are useful vocabulary.
MCP Console uses snake_case equivalents and makes listener publishing a separate concern.
SRT also distinguishes filesystem and proxy-mediated network enforcement.[2]

The native runner owns native policy interpretation and enforcement.
The implemented [Docker Sandbox compute provider](../docs/DOCKER_SANDBOX.md) uses Docker's existing policy and rejects unsupported native restrictions.
The broader provider selection below remains exploratory; each provider must own its policy interpretation and enforcement.
Adapters may map configuration fields to a provider's API, but Console does not supply a shared security validator or evaluator for them.

### Current MCP Console

The implemented interface includes project YAML, native built-in selection, and managed recording storage.
It reads `.agents/console/config.yaml` in the launch working directory and records sessions under `.agents/console/sessions/`.
The current configuration interface and its supported fields are documented in [`docs/SANDBOX_CONFIGURATION.md`](../docs/SANDBOX_CONFIGURATION.md).

Package preparation intentionally happens outside the worker sandbox, and `--no-sandbox` omits sandbox descendant cleanup.
Custom profiles, inheritance, target adapters, expanded controls, and the nested log layout below remain proposals.
Earlier inspected source references are retained for context.[3][4]

## 4. Proposed file shape, discovery, and custom profiles

### Top-level keys

A file has document-level keys and session-level keys:

| Document-level key | Meaning                                                                |
| ------------------ | ---------------------------------------------------------------------- |
| `version`          | Configuration schema major version. Initially `1`.                     |
| `default_profile`  | Profile selected when the caller supplies none; defaults to `default`. |
| `profiles`         | Optional map of named session profiles.                                |
| `definitions`      | Optional reusable `targets`, `permissions`, and `environments`.        |
| `server`           | Controller-wide settings such as session admission limits.             |
| `storage`          | Project-wide log retention and managed-cache cleanup.                  |

All other recognized top-level keys form the implicit `default` profile:

| Session-level key | Meaning                                                                                                                          |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `extends`         | Proposed parent profile. Currently only explicit native built-in selection is implemented; omission preserves existing defaults. |
| `description`     | Human-readable purpose, not executable instructions.                                                                             |
| `permissions`     | Filesystem, outbound network, listeners, and local IPC.                                                                          |
| `target`          | Transport, compute environment, and workspace.                                                                                   |
| `sandbox`         | Provider selection and provider-specific options.                                                                                |
| `services`        | Named listeners and explicitly requested publication routes.                                                                     |
| `environments`    | R, Python, and SQL configuration.                                                                                                |
| `packages`        | Preparation, resolution, repositories, and source policy.                                                                        |
| `resources`       | Per-session hard limits and scheduling preferences.                                                                              |
| `env`             | Deliberately forwarded or assigned worker environment variables.                                                                 |

Do not require a `session:` or `profiles.default:` wrapper for a one-profile configuration.
Do not allow the same implicit profile to be defined again as `profiles.default`.
`storage` is document-wide and is not inherited or overridden by session profiles.

### Discovery

The canonical and only discovered project path is `.agents/console/config.yaml`.
`config init` creates that file in the same managed directory used for transcripts, logs, projections, and other Console artifacts.
There is no alternate project filename or separate user-level Console configuration directory.
Whether generated files are tracked or ignored by version control is the user's choice.
Initialization does not create or edit Git ignore rules.

Choose the project root once at startup: an explicit workspace argument, then the discovered project configuration's parent project, then the invocation working directory.
Search upward only within the initial repository/workspace boundary; do not keep discovering new configuration after a worker changes directory.
Do not load a chain of ancestor project files implicitly.

Recommended precedence for ordinary session settings is built-in defaults, the selected project file, and explicit controller CLI settings.
Trusted sandbox requirements supplied by the caller remain the sandbox's responsibility to apply.

Select another project through an explicit workspace argument, which selects that project's `.agents/console/` directory.
Do not add a config-path or storage-root override that scatters Console-managed files across unrelated locations.

### Named profiles

```yaml
version: 1
extends: :read-only
permissions:
  filesystem:
    allow_write: [.]
    deny_write: [.git, .agents, .codex, .claude]

profiles:
  review:
    extends: :read-only

  api_work:
    extends: default
    permissions:
      network:
        mode: allowlist
        allow: [https://api.example.com]

  remote:
    extends: default
    target:
      transport: {kind: ssh, host: research-box}
      workspace: /srv/projects/analysis
```

`default`, the three built-in names, and any future names beginning with `:` are reserved.
User profiles cannot shadow them.
A named profile with omitted `extends` inherits `default`; examples should normally spell that out.

An explicit `extends` chooses the sole parent.
In particular, `review` above does **not** inherit the top-level project's write grant.
Otherwise a supposedly read-only review profile could accidentally remain writable.
Controller-wide settings and administrator requirements still apply to every selection.

Materialize only the selected inheritance chain, then apply missing engine defaults.
Reject unknown parents and cycles.
Do not support multiple profile inheritance or implicit cross-products of profiles.

Selecting a built-in directly produces its default local target, not the target from the implicit project profile.
`config explain` shows the selected target.
To remove sandboxing while retaining an already selected remote target, use the explicit policy-only override described under `":danger-full-access"`, not an unrelated profile selection.

### Optional reusable definitions

```yaml
version: 1
extends: :read-only

definitions:
  targets:
    lab:
      transport: {kind: ssh, host: research-box}
      workspace: /srv/projects/analysis
      compute: {kind: host}

  permissions:
    project_edits:
      filesystem:
        allow_write: [.]
        deny_write: [.git, .agents, .codex, .claude]

  environments:
    analysis:
      r:
        library:
          kind: managed
          requirements: [dplyr, ggplot2]
      python:
        environment:
          kind: managed
          requirements: [numpy, pandas]

profiles:
  remote_analysis:
    extends: default
    target: {use: lab}
    permissions: {use: project_edits}
    environments: {use: analysis}
```

`use` expands a definition from the matching namespace.
Sibling fields overlay the expanded value using the same merge rules as profile inheritance.
A component accepts one `use`, not a list.
Definitions may themselves use one parent definition; cycles are errors.

Definitions reuse ordinary settings.
The resulting target and requested permissions still pass through the selected sandbox/provider interface.

The three namespaces are enough initially.
Do not introduce separate registries for every small object, or force ordinary files to spread state across them.

## 5. Loading and managed files

Console reads the project configuration as data, selects the profile, and passes the requested policy to the sandbox/provider.
The sandbox owns policy parsing, validation, and enforcement; Console returns its result or error.
There is no separate Console authorization step, approval dialog, trust record, or per-operation permission workflow in this sketch.
Any policy requirements supplied by the caller use the sandbox's existing interface.

Ordinary configuration loading and profile listing do not execute configured programs.
Target setup, builds, and package preparation happen when the selected operation needs them, through the existing adapters.
Pass the loaded configuration values through the launcher's existing setup interface, keeping relay stdin/stdout/stderr available for their normal streams.

### Protected managed directory

The `":workspace"` profile's native `.agents` read entry protects `.agents/console/` from worker writes by default, including its configuration, state, and records.
These are deliberate defaults: native write entries for `.agents` or a descendant can override them, while equal-path denials take precedence over grants.
The controller writes from outside the worker sandbox.
The no-profile default and `":read-only"` baseline grant no ordinary workspace writes; explicit writable paths still use native precedence.
Unrestricted or externally enforced filesystem kinds retain their native meaning, and `serve --no-sandbox` bypasses sandbox project configuration.

Workers can read the managed directory by default, including current and previous session records.
Session and generation identities do not imply filesystem read isolation.
Explicit read restrictions use native semantics and their platform limitations.
For proposed remote or container adapters, permissions must be interpreted in the target namespace by its sandbox provider.

### Referenced files

For sandboxed sessions, referenced preparation inputs must have the same protection from worker writes as the config file.
This includes Dockerfiles, complete build contexts, requirements manifests, lockfiles, and files they refer to that a trusted builder or installer will consume.
Inputs under `.agents/console/` meet this proposed rule only while the effective policy protects them from worker writes; deliberate overrides must be considered.
An external file, including one outside the project or in the user's home directory, is also eligible if the sandbox/provider confirms it is outside the worker's effective write access.
An ordinary file or build context in a writable project directory is rejected; selecting its path does not make it a protected input.
When replacing a running worker, protection must also cover that worker's current access.
The sandbox/provider owns this access check; Console uses its result without implementing a path matcher or filesystem safety checks.

Preparation adapters read the referenced inputs through their ordinary file and build interfaces.
Console does not add an approval-bound snapshot store, a separate authorization step for reading files, or private staging rules for unapproved captures.
File-loading and preparation errors propagate from the adapter.

## 6. Built-in profiles and merge semantics

### Built-ins

| Profile                            | Filesystem                                                                                                                     | Worker outbound network     | Sandbox             |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------- | ------------------- |
| `":read-only"`                     | Read the visible target filesystem; write only private session temporary storage                                               | None                        | Required            |
| `":workspace"`                     | `":read-only"` plus workspace writes; native read defaults for `.git`, `.agents`, `.codex`, and Console's `.claude` adjustment | None                        | Required            |
| `":danger-full-access"` (proposed) | No MCP Console filesystem restrictions                                                                                         | Unrestricted by MCP Console | Disabled explicitly |

Read-only is a mutation restriction, **not** a confidentiality guarantee.
Use read denials, `read: minimal`, a container, or a combination when host files must not be visible.
OS permissions and an outer host/container boundary always remain in force.

Private temporary storage is an explicit built-in exception.
Do not make all of shared `/tmp` writable.
Engine-owned logs and package preparation are separate from worker write permissions and must be reported separately.
Metadata protection is a profile default and may be deliberately overridden; no separate mandatory managed-directory denial is supplied.

The implemented starter selects `":workspace"` directly.
Repeating its native metadata defaults is unnecessary.

### Proposed custom-profile composition and sandbox policy

The implemented [project-file and CLI layering](../docs/CONFIGURATION.md) preserves omitted keys, merges mappings recursively, and replaces scalar values and sequences.
Explicit null remains a value.
These generic rules run before the built-in adapter: current restricted filesystem entries augment the native baseline, network values replace its network setting, and unrestricted/external filesystem kinds replace its filesystem.
For future custom-profile composition, changing a tagged object's `kind` could replace that whole object rather than retaining incompatible fields from its old kind.
That field-specific behavior is not part of the implemented generic layering module.
Under that proposal, changing a Python environment from `managed` to `existing` would not retain the managed requirements list.

Permission blocks describe the policy requested from the sandbox.
Console does not evaluate access rules, compare permissions, or add a separate set-delta language.
Use a profile with the desired baseline and supply its requested rules:

```yaml
profiles:
  write_results_only:
    extends: :read-only
    permissions:
      filesystem:
        allow_write: [results]
```

The sandbox's schema owns the meaning of grants, denials, precedence, and supported combinations.
Console supplies the native baseline and explicit adjustments to that schema and returns the sandbox's result.
Metadata defaults remain subject to native precedence.
Do not implement a second rule matcher or promise access semantics beyond those provided by the selected sandbox.

### Proposed full-access built-in and no sandbox

This built-in selector is a proposal:

```yaml
version: 1
extends: :danger-full-access
```

It may add a target, environments, and resources; document-wide storage settings still apply.
It must not also claim enforced filesystem or network restrictions.
Such contradictory configurations are errors.
A hard resource limit can still be enforced by a separate supervisor or compute provider.

Also support `mcp-console serve --no-sandbox` as a deliberate policy-only override: retain the selected target and environment, disable the worker sandbox, and clearly report that configured worker permission rules are not enforced.
Managed requirements still use the configured resolver path.
This is an explicit user action, never an error-recovery path.

Internally `":danger-full-access"` selects `sandbox.provider: none`.
Setting `provider:
none` alongside a restricted policy is otherwise rejected; do not silently reinterpret an apparently restricted YAML file as unrestricted execution.

**Proposed behavior change:** lifecycle supervision should be independent of security isolation.
Unsandboxed workers should still receive normal session ownership, process-tree cleanup where supported, and resource accounting.
The current no-sandbox behavior differs.[3] Expose any cleanup limitation in the launch report rather than implying isolation is needed to own a child process.

## 7. Filesystem rules and path meaning

```yaml
permissions:
  filesystem:
    read: all
    allow_read: []
    allow_write: [results, /scratch/analysis]
    deny_read: [~/.ssh, secrets, "**/.env", "**/.env.*"]
    deny_write: [.git, .agents, .codex, .claude, data/raw]
```

`read` is `all` or `minimal`.
`minimal` requests the sandbox's supported runtime baseline; add project/data reads with `allow_read`.
`config explain` presents the sandbox's reported grants and diagnostics.
If the sandbox rejects a requested combination, Console forwards the error.

Paths and quoted patterns are passed to the sandbox using its supported rule syntax.
The sandbox owns path interpretation and filesystem safety, including whether a requested combination is supported.
Console does not expand policy patterns by scanning the filesystem or inspect filesystem objects to decide whether access is safe.

Path namespaces are determined by the field:

| Field                                                            | Namespace and relative base                                                      |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `permissions.filesystem.*`                                       | Final worker filesystem; relative to `target.workspace`                          |
| Language executable, library, project, and database paths        | Final worker filesystem; relative to the workspace                               |
| Unix socket paths                                                | Final worker filesystem; normally absolute                                       |
| `target.workspace` with host compute                             | Destination host filesystem; must exist unless creation was explicitly requested |
| Docker mount `source`                                            | Compute host reached by the transport                                            |
| Docker mount `target`                                            | Container filesystem                                                             |
| SSH host alias                                                   | Controller's existing OpenSSH configuration                                      |
| Referenced Dockerfile, build context, or requirements manifest   | Controller filesystem; relative to the controller project root                   |
| Project-environment lockfile                                     | Selected project environment on the target; subject to protected-input rules     |
| Console configuration, state, logs, transcripts, and projections | Controller project's `.agents/console/`                                          |
| Console preparation caches                                       | `.agents/console/cache/` on the host where preparation runs                      |

Support a small set of explicit path substitutions: `${project}` for the controller project root, `${workspace}` for the final worker workspace, and `${session_tmp}` for private target temporary storage.
`~` resolves in the field's own namespace.
Do not expand arbitrary environment variables in policy paths; an agent must not change a policy by changing `HOME` or `TMPDIR` later.

A controller `${project}` path is not automatically a valid remote mount source.
Reject cross-machine substitutions unless a declared staging operation provides the mapping.
Console supplies the declared paths and namespace mappings to the sandbox.
The sandbox owns normalization, policy validation, and enforcement, and its filesystem restrictions and limitations apply as reported.
Console forwards an error if the sandbox rejects the request; it does not independently certify an accepted policy.

## 8. Outbound networking

The sandbox and its proxy own network-rule parsing, validation, and enforcement.
Console passes the requested settings through the provider interface and returns its diagnostics.
The examples describe requested capabilities, not a separate Console network policy language.

### Three explicit modes

```yaml
permissions:
  network:
    mode: none
```

`none` means no worker-initiated outbound connections.
`allowlist` means only the requested destinations, through a managed and enforced mediation path.
`unrestricted` means no MCP Console outbound destination filtering, while filesystem sandboxing may remain enabled.

The mode defaults to `none`.
Adding an allow rule does not silently change it.
An inactive rule produces a diagnostic.
The sandbox reports incompatible combinations such as unrestricted networking with filter rules.

In `allowlist` mode the launcher requests the sandbox's managed proxy.
There is no separate feature flag whose omission leaves listed restrictions unenforced.
This intentionally differs from Codex's separately enabled proxy feature.[1]

### Destination allowances

```yaml
permissions:
  network:
    mode: allowlist
    allow:
      - https://api.example.com
      - https://*.data.example.org
      - protocol: tcp
        host: analytics-db.corp.example
        port: 5432
        allow_private: true
        addresses: [10.44.8.12/32]
    deny:
      - https://tracking.data.example.org
```

A string requests an origin-shaped destination rule; a structured entry requests a specific TCP endpoint.
Supported schemes, patterns, addresses, and ports follow the sandbox/proxy schema.
Console does not introduce its own hostname matcher or address classifier.

An HTTPS destination grant authorizes an opaque connection to that host and port.
It is **not** a promise to restrict HTTP methods, URL paths, request bodies, or the application protocol sent over that connection.
The scheme shorthand must not disguise the limits of a non-intercepting CONNECT proxy.

The structured example requests one database endpoint, including its private-address allowance.
The provider determines whether it supports those fields and what network protocols it can mediate.
Forward unsupported-field and capability errors to the user.

### URL paths and methods

Full URL filtering is a stronger, separate capability:

```yaml
permissions:
  network:
    mode: allowlist
    proxy:
      tls_inspection: required
    allow:
      - url: https://data.example.org/public/**
        methods: [GET, HEAD]
```

A `url` rule with a path or `methods` requires HTTP-aware enforcement.
HTTPS requires a configured TLS-terminating proxy or equivalent trusted application gateway.
The example requests TLS inspection; it does not assume a CONNECT tunnel can see encrypted paths.
SRT's optional TLS termination is one possible upstream building block, not an assurance that every provider has this capability.[2]

URL parsing, rule interactions, and TLS compatibility are provider responsibilities.
Console passes these settings to a supporting sandbox/proxy and forwards its acceptance or rejection without adding HTTP inspection of its own.

### Enforcement and failure behavior

Network isolation and mediation belong to the sandbox and its proxy.
Console does not add connection-time checks or independently verify the sandbox's enforcement.

A non-proxy-aware database driver must work through an enforced endpoint route, a transparent compatible provider, or a managed TCP forward used by its connection adapter.
Merely setting `ALL_PROXY` is not enough.
When a provider reports that it cannot supply the requested route, forward that error.
Preserve the intended server name and certificate verification if the logical endpoint is routed through a local forward.

The sandbox/provider owns proxy failure behavior and tunnel lifetime.
Console reports its status and does not retry with a less restricted policy.

Transport and preparation traffic are separate.
The controller can make the configured SSH connection even when the worker's network mode is `none`.
Image pulls and package downloads use the preparation path's settings, independently of the worker's network restrictions.

## 9. Local development: listeners, publishing, and IPC

These are three different capabilities: initiating an outbound connection, binding a listener, and connecting to a local IPC socket.
None should imply the others.

### A Shiny app reachable from another tool

```yaml
version: 1
extends: :workspace
permissions:
  network:
    mode: none

services:
  shiny:
    protocol: http
    listen: {host: 127.0.0.1, port: 3838}
    publish: {scope: controller_loopback, port: auto}
```

A service declaration explicitly requests its exact listener and a managed publication route.
It is therefore a capability request, not just documentation.
The sandbox/provider interprets the requested listener/forward grants and checks them against its policy requirements.
Users do not have to repeat those grants in a second list.
It does not grant arbitrary outbound loopback access.

The worker can run:

```r
shiny::runApp(".", host = "127.0.0.1", port = 3838, launch.browser = FALSE)
```

`runApp` accepts a bind address and TCP port.[5] The app may keep its cell active while the browser interacts with it; polling/interrupts must remain available.
The configured environment must contain Shiny or permit its explicit preparation.
The service does not implicitly install or run application code.

The manager establishes a forward from a controller loopback port to this specific worker service.
On SSH, that includes an authenticated channel to a target-side connector; in a container or isolated network namespace, it includes the necessary namespace-aware connector.
Publishing a container port that cannot reach a loopback-only app is not an acceptable implementation.

Return the reachable URL, service name, session/generation, and access scope in session metadata.
A browser tool running on the controller host can use that URL.
A cloud browser cannot reach the user's loopback address merely because a port is open: it needs a separately configured authenticated tunnel/exporter.
Reserve `scope: authenticated_tunnel` for such a provider, with explicit recipient/authentication and lifetime.
Never default to a public URL or a non-loopback bind to make an otherwise unreachable browser work.

HTTP publishing must support WebSocket upgrades and long-lived connections.
Forward only the named service, not an unrestricted tunnel into the sandbox's network namespace.
Attribute listener ownership to this worker generation; a port conflict must not publish another process's service.
Close the forward and retire the service's descendants when the session ends or is replaced.

`mode: none` still blocks new outbound connections.
Responses on the approved inbound service are allowed and can carry data; service publication is itself a disclosure capability, not “zero possible data transfer.”

### A listener without publication

```yaml
permissions:
  network:
    listen:
      - {protocol: tcp, host: 127.0.0.1, port: 8000}
```

This permits the bind only.
It neither publishes the port nor authorizes connections to unrelated host services.
Non-loopback binding must be explicitly configured.

Automatic allocation is safe when the manager owns the listening socket, as for `publish.port: auto`.
For an application-owned listener, the application must bind port zero and report its bound endpoint, or accept a supported socket handoff.
Never probe a free port, release it, and promise it remains reserved.

### Unix sockets and named pipes

```yaml
permissions:
  network:
    local_sockets:
      allow:
        - {kind: unix, path: /run/user/1000/example.sock}
      deny: []
```

A future Windows adapter can use `kind: named_pipe` with an exact pipe name.
Connecting and binding/creating an IPC endpoint should be separate operations; the `allow` entries above permit connecting only.
Filesystem visibility of a socket does not itself authorize connection.

Do not include the Docker daemon socket or SSH agent socket in defaults.
Authorizing an IPC endpoint authorizes the service behind it; it is not merely file access.
A Docker API socket can defeat a container's intended host boundary.
Such grants must be explicit and must not be described as narrow just because they name one filesystem path.

## 10. Targets: local, SSH, Docker, and other compute

### Local host

```yaml
target:
  transport: {kind: local}
  compute: {kind: host}
  workspace: ${project}
sandbox:
  provider: auto
```

This is the ordinary default.
`auto` selects the default provider for the target, ordinarily the bundled native runner, which validates the requested policy.
It does not mean “try something, then run without a sandbox.” Report the selected provider and effective capabilities before launch.

### SSH host

```yaml
target:
  transport:
    kind: ssh
    host: research-box
    executable: ssh
    setup:
      install: never
      executable: /opt/mcp-console/bin/mcp-console
    disconnect:
      action: terminate
      lease: 30s
  compute: {kind: host}
  workspace: /srv/projects/analysis
sandbox:
  provider: auto
```

The minimal form in section 2 uses the controller host's existing OpenSSH configuration and requires an existing compatible target executable.
The user sets up passwordless, noninteractive SSH before selecting this target.
OpenSSH and the host's existing key or credential facilities own authentication, identities, and host-key verification.
Invoke SSH in batch mode and forward authentication or connection errors; do not add password prompts, private keys, or SSH credential fields to Console configuration.
Do not forward the SSH agent as an implementation convenience.

A future `setup.install: if_missing` may stage a version-matched executable and runtime assets when selected.
It is not an unpinned `curl | sh`.
Pin/check the protocol and runtime compatibility before launching the relay.
Binary download, staging, and package preparation are setup operations separate from worker networking.

The controller launches the remote manager through the public launcher boundary; the manager applies the remote sandbox and then starts relay and worker.
The server is not taught the details of a remote Seatbelt, bubblewrap, Windows, or third-party sandbox implementation.

No PTY by default.
Relay stdout remains protocol data, not shell banners or setup logs.
The manager uses ownership/lease semantics to retire the remote process tree after disconnect.
A local PID ownership flag alone cannot enforce remote cleanup after a network partition.

The remote host is trusted with submitted code, data, and any explicitly forwarded credentials.
A sandbox on that host does not protect the session from its administrator.
The remote launcher uses the controller-selected policy rather than discovering another Console configuration on the remote host.
The sandbox applies any target-local requirements through its own interface.

There is no implicit project synchronization.
Optional later synchronization needs an explicit source, destination, direction, and conflict policy.
An existing remote workspace is sufficient for the first SSH implementation.

### Docker container

```yaml
target:
  transport: {kind: local}
  workspace: /workspace
  compute:
    kind: docker
    image: company/mcp-console:analysis
    pull: if_missing
    mounts:
      - source: ${project}
        target: /workspace
        access: read_write
sandbox:
  provider: auto
```

The mount exposes a path to the compute environment; `access: read_write` is an upper bound, not a replacement for the session filesystem policy.
A read-only profile still requires read-only effective access.
A workspace-write profile still protects its denied subpaths.
The selected sandbox/provider validates the requested mount and policy combination; Console forwards its result.

Record the resolved image digest with the launch settings.
Pull/build policy should distinguish `never`, `if_missing`, and an explicit refresh; do not silently move a running profile to a different mutable tag.
Images must contain or receive compatible runtime components through the configured setup path.

Use typed fields for image, mounts, user, working directory, devices/GPU access, resource limits, and lifecycle.
Do not default to privileged mode, a host PID or network namespace, a daemon socket mount, or unnecessary capabilities.
A container is compute placement and isolation, not automatically an exact implementation of all requested permissions.

Any provider-specific arguments belong to that provider's supported interface and validation.
Console does not parse arbitrary Docker flags to decide whether they preserve a permission boundary.
An arbitrary container wrapper belongs under the explicit command-provider contract.

### Docker on an SSH host

The two dimensions compose without another top-level target kind:

```yaml
target:
  transport: {kind: ssh, host: research-box}
  workspace: /workspace
  compute:
    kind: docker
    image: company/mcp-console:analysis
    mounts:
      - source: /srv/projects/analysis
        target: /workspace
        access: read_write
sandbox:
  provider: auto
```

The mount source is on `research-box`, not on the controller.
Filesystem permissions refer to `/workspace` inside the container.
Cache backing and service forwarding are likewise resolved across both boundaries.

### Docker Sandbox

The [implemented first slice](../docs/DOCKER_SANDBOX.md) requires a prepared digest-qualified template, explicit same-path shares, and one owned VM per worker generation.
The session lifetime and policy translation described below remain proposals.

```yaml
target:
  transport: {kind: local}
  workspace: /workspace
  compute:
    kind: docker_sandbox
    template: company/mcp-console-sandbox:analysis
    lifetime: session
sandbox:
  provider: compute
```

Use a distinct compute kind.
Docker's current Sandboxes documentation describes a microVM-backed execution environment, not merely a synonym for an ordinary Docker container.[6] MCP Console should hide the particular vendor CLI spelling behind its adapter instead of incorporating changing command names into the common schema.

`provider: compute` asks that provider to interpret and enforce the requested permissions.
If it cannot enforce an exact read denial, network rule, or other requested restriction, it fails or requires an explicit compatible inner provider.
It does not report a broad outer boundary as if the narrower policy were enforced.
A workspace mapping must be specified or supplied by an explicit adapter default that maps the project only and appears in the launch plan.

### Embedded or separate Dockerfile

```yaml
target:
  compute:
    kind: docker
    build:
      context: .agents/console/build
      dockerfile: .agents/console/Dockerfile
```

For very small definitions, permit an alternative:

```yaml
target:
  compute:
    kind: docker
    build:
      context: .agents/console/build
      dockerfile_inline: |
        FROM company/mcp-console-base:approved
        ENV LANG=C.UTF-8
```

`dockerfile` and `dockerfile_inline` are mutually exclusive.
Prefer the separate file once the definition grows beyond a few lines; it gets normal Dockerfile editing, review, and linting.
The examples keep the Dockerfile and build context under `.agents/console/`.
A separate Dockerfile or context outside that directory is allowed only when it meets the protected-input rules in section 5.
The whole writable project is not an eligible build context for a sandboxed session.

The selected build adapter reads the Dockerfile and context through its ordinary build interface.
Remote builds require explicit context staging through that adapter.
Build-time network and secret access are separate from runtime permissions.
Image builds are code execution, not inert configuration parsing.

## 11. Sandbox and command-provider contract

`sandbox.provider` is one of `auto`, `native`, `srt`, `compute`, `command`, or `none`.
`native` means the bundled MCP Console native runner; it does not expose its upstream Codex patch details.
`srt` identifies an adapter, not automatic loading of the user's unrelated SRT settings.

A third-party sandbox can be configured without putting shell text in the core schema:

```yaml
sandbox:
  provider: command
  command:
    executable: /usr/local/bin/company-sandbox-adapter
    args: [run, --region, us-east, --setup-fd, "${setup_fd}"]
    env:
      COMPANY_SANDBOX_TOKEN: {from_env: COMPANY_SANDBOX_TOKEN}
    protocol: mcp-console-launch-v1
```

This adapter is trusted executable configuration.
It receives the requested policy, target argv, configured environment, resource requirements, and lifecycle parameters through a versioned setup contract.
The adapter owns parsing and validation of its policy input and returns its diagnostics to Console.
`${setup_fd}` is an injected placeholder, not general template evaluation.
The adapter launches the exact provided argv without reconstructing it through a shell.

An adapter that places the worker in another compute environment can use the same `command` object under `target.compute`, with `kind: command`.
A policy-enforcing compute adapter can be selected with `sandbox.provider:
compute`.
Keep its placement role distinct from its permission enforcement role even if one executable performs both.

The contract requires separate setup/control and target data paths; relay stdout is not mixed with capability reports.
It must support cancellation, exit status, signal/interrupt forwarding, environment/path mapping, descendant retirement, and the applicable resource/network/service controls.
Capability negotiation takes place before execution, not after a partially unconfined worker has started.

For simple existing wrappers, a command passthrough form may accept an argv placeholder and report **externally managed** enforcement.
It reports the wrapper's stated enforcement without treating command success as a Console verification of the policy.
Supporting restricted profiles requires a trusted policy-aware adapter or a separately enforced inner sandbox.
Provider self-reporting is a compatibility check, not proof that an untrusted executable is safe.

The public server-to-launcher architecture remains:

```text
controller <-> mcp-console sandbox <-> [transport/compute/manager] <-> relay <-> worker
```

The launcher subsystem owns provider selection and proxy setup through its adapters.
Sandbox/provider process-tree supervision and cleanup belong to the selected runner or manager.
The server retains logical relay lifetime orchestration and retirement; the relay retains direct-worker signal delivery, bounded termination, and reaping.
The relay remains unaware of sandbox implementation.
Use the extracted runner's configuration interface and keep its implementation details behind that boundary.
Changes to sandbox policy validation or enforcement belong in the sandbox, not in a Console adapter.

## 12. R and Python environments

Package availability, selecting an interpreter, preparing an environment, automatic missing-package resolution, and importing a package are separate operations.
Configuration should not collapse them into one `requirements` list with implicit side effects.

### Managed defaults with always-available packages

```yaml
environments:
  r:
    executable: auto
    library:
      kind: managed
      lifetime: session
      requirements: [dplyr, ggplot2, shiny]
  python:
    executable: auto
    environment:
      kind: managed
      lifetime: session
      requirements: [numpy>=2, pandas, matplotlib]
```

`managed` means MCP Console prepares and selects an environment from the declared requirements.
The R implementation may use an `ir` library; Python may use a managed virtual environment.
These packages are available, not attached, imported, or executed automatically by configuration.

A session-lifetime environment can reuse immutable cached artifacts.
It need not redownload everything at each start.
Worker mutation of shared installed packages is not permitted; dynamic additions use a managed environment update or overlay, leaving unrelated user libraries unchanged.

Engine-required bridge/runtime packages are distinct from user packages and are reported explicitly.
A user list does not remove runtime components necessary for the chosen language configuration.
Conversely, a Python-only future runtime must not require R just because a historical default did.

A release may supply a documented built-in default manifest.
Its exact contents belong in implementation documentation and resolved-environment metadata, not in the permanent meaning of `version: 1`.
Preserve a lock or explicit manifest when reproducibility across MCP Console releases matters.

Inline requirements are enough for small files.
A typed `requirements_file` keeps a larger manifest beside the configuration:

```yaml
environments:
  r:
    library:
      kind: managed
      requirements_file:
        format: mcp_console
        path: .agents/console/requirements.yaml
  python:
    environment:
      kind: managed
      requirements_file:
        format: mcp_console
        path: .agents/console/requirements.yaml
```

The referenced `requirements.yaml` uses a separate, data-only manifest schema:

```yaml
version: 1
r: [dplyr, ggplot2]
python: [numpy>=2, pandas]
```

Each language consumes its own list through the preparation adapter's existing manifest parser.
Inline `requirements` and `requirements_file` are mutually exclusive within a language environment.
Manifest paths are controller paths, relative to the controller project root, just like referenced Dockerfiles and build contexts.
The example therefore reads the `requirements.yaml` beside the discovered config even for an SSH or container target.
Absolute controller paths are also allowed if they meet the protected-input rules in section 5.
The preparation adapter reads the manifest and supplies its requirements to target-side preparation through its normal setup channel.
It does not look for a similarly named target file or implicitly synchronize the project.

Additional explicit formats can include `requirements_txt` for Python and a supported data-only `ir` manifest format for R.
An adapter must reject formats it does not understand.
Do not evaluate an arbitrary R or Python script merely to discover requirements.
These manifests declare requirements; use a supported lockfile/project environment when an exact resolved dependency graph is needed.

### Existing installations and environments

```yaml
packages:
  resolution: disabled

environments:
  r:
    executable: /opt/R/4.5/bin/R
    library:
      kind: existing
      paths: [/opt/company/R-library]
  python:
    executable: /opt/company/python/bin/python3
    environment:
      kind: existing
      path: /opt/company/python
```

`existing` never installs into or modifies the selected environment.
Missing packages produce ordinary actionable errors.
An explicit interpreter must be compatible with its selected library/environment; conflicting selectors are an error, not competing priorities.

A future managed installation selector can use `version` instead of `executable`.
`version` does not download an interpreter unless the preparation settings enable it.
Interpreter downloads use the configured runtime source, independently of package indexes.

Typed configuration takes precedence over ambient `R_HOME`, `RETICULATE_PYTHON`, and virtual-environment hints.
Conflicts must be reported; do not depend on accidental environment-variable ordering.
The selected executables and libraries are target paths, not controller paths.

### Explicit project environments

```yaml
packages:
  resolution: explicit
  frozen: true

environments:
  r:
    library:
      kind: project
      manager: renv
      path: .
      lockfile: renv.lock
      mutation: never
  python:
    environment:
      kind: project
      manager: uv
      path: .
      lockfile: uv.lock
      mutation: never
```

`mutation: never` means use an existing project environment without changing its contents or lock.
A requested preparation that would modify it fails with instructions.
`mutation: overlay` may prepare a separate managed environment from the locked project plus requested additions, leaving the project unchanged.
`mutation: project` explicitly selects preparation/update of the project environment.

Project-environment paths and lockfiles remain in the target namespace.
Using an existing environment does not authorize a trusted preparation process to consume worker-writable project files.
Preparation from a project lock or its referenced files follows section 5's protected-input rule.

Explicit project selection wins over discovery.
An optional `kind: auto` can perform documented discovery on the target; it must report the selected manager and reject ambiguous competing environments.
Do not activate arbitrary project startup code on the trusted controller while detecting an environment.
R startup files and Python startup hooks execute in the selected runtime, inside the worker sandbox when enabled.

Do not implement “always available” Python packages by stacking unrelated virtual environments on `PYTHONPATH`.
Resolve the project and required additions as one environment, or reject incompatible requirements.
Likewise, R library composition must respect the chosen R installation, ABI, precedence, and loaded-package constraints.

Changes to interpreter, target, or environment kind require a new worker.
Additive package updates may preserve live state only where the runtime's existing activation contract supports that safely.
A failed preparation must not half-commit a new environment or replay user code automatically.
Current MCP Console already distinguishes preparation from import/attachment and has explicit retained-environment transitions.[7]

## 13. Package resolution, mirrors, and preparation

### Resolution modes

```yaml
packages:
  resolution: explicit
  offline: false
  frozen: false
```

| Mode        | Behavior                                                                                                         |
| ----------- | ---------------------------------------------------------------------------------------------------------------- |
| `automatic` | Prepare declared requirements, accept explicit additions, and permit supported missing-package inference.        |
| `explicit`  | Prepare declared requirements and explicitly requested additions; missing imports alone do not install anything. |
| `disabled`  | Do not invoke a package resolver, installer, or downloader. Selected environments must already exist.            |

`offline: true` blocks preparation network access while allowing use of existing artifacts and cache-only resolution.
`frozen: true` requires an applicable locked solution and prevents changing it.
`disabled` is stronger than offline: a local build or cache-only install is still an installation.

These controls cover MCP Console's resolver path, including R, Python, and DuckDB extension preparation.
They cannot stop arbitrary evaluated code from implementing an installer if its ordinary filesystem and network permissions allow that.
Runtime enforcement remains the sandbox's responsibility.

`automatic` preserves the current default and runtime missing-package behavior.[7] The host resolver handles supported inferred requirements outside the worker sandbox, using its configured sources and existing resolver rules.
It may install packages and run accepted installation or build code with server permissions.
There is no additional Console approval step for an inferred package, its transitive dependencies, or its artifact downloads.
A read-only worker or a worker with networking disabled can still use this resolver path.
Choose `explicit` to disable missing-package inference, or `disabled` to disable resolution entirely.

The compatibility preparation default remains `network: sources` and `builds: trusted_host`.
Worker permissions do not constrain this host-side preparation process.
Use `builds: isolated` for untrusted source builds once supported, or `builds: disabled` to require suitable prebuilt artifacts.
An eventual change to isolated preparation by default is a separate migration, not something the YAML loader alone delivers.

### Corporate sources

```yaml
packages:
  resolution: explicit
  sources:
    r:
      repositories:
        CRAN: https://packages.corp.example/cran/approved
    python:
      indexes:
        - name: approved
          url: https://packages.corp.example/pypi/simple
      index_strategy: first_index
    duckdb:
      repositories:
        - https://packages.corp.example/duckdb
  source_policy:
    allowed_kinds: [registry]
  preparation:
    network: sources
    builds: isolated
```

An explicitly configured repository/index collection **replaces** that language's implicit public defaults.
Do not append public PyPI or CRAN as a fallback.
Python index ordering is significant; `first_index` selects the first index containing a package, rather than choosing competing versions across all indexes.
This follows the safety motivation documented by uv.[8]

The selected preparation adapter owns package-source handling for direct and transitive requirements and build dependencies.
If isolated preparation is selected, its sandbox/provider owns the requested network and filesystem restrictions.
Project package-manager configuration and ambient index/repository variables cannot silently add a source outside this policy.
A corporate configuration may require different approved copies of built-in runtime dependencies; missing approved artifacts are errors, not permission to bypass the mirror.

With `builds: trusted_host`, retain the current accepted inputs: documented trusted `ir` references with `IR_NO_LOCAL_SOURCES=1`, named Python PEP 508 registry requirements, and validated DuckDB extension names.
`source_policy` may further restrict those inputs; it cannot authorize local sources, Python URL/VCS direct references, or arbitrary DuckDB source selectors on this path.
Supporting additional source kinds requires a separately implemented isolated preparation path; reject them on the trusted-host path.

`network: sources` uses the configured repositories and the resolver's ordinary handling of transitive requirements, redirects, and artifact downloads.
Proxy credentials and repository authentication are held by the preparation path, not automatically exposed to the worker.

Source selection and build isolation are separate settings.
The existing resolver input contracts still apply to declared and runtime-inferred requirements; this sketch does not add a package-approval system.

`builds: isolated` requests isolated preparation with a bounded source snapshot, controlled network, resource limits, and owned output directories.
If unavailable, fail when a build is needed.
`builds: trusted_host` names the compatibility behavior with a stronger trust requirement; `builds: disabled` permits only suitable prebuilt artifacts.
Do not present current host-side preparation as isolated until that boundary exists.

Preparation occurs for the target's platform and runtime.
On SSH this normally means a target-side trusted preparation process; for containers it means a compatible builder or image layer.
Never copy the controller's arbitrary site-packages or compiled R library into an incompatible target.

Preparation remains controller-owned even when it runs remotely.
Use a separate preparation launch rather than assigning installer lifecycle to the worker relay.
A command adapter supporting only relay launch must use pre-existing environments until it also supports the preparation lifecycle.
Requests that need unsupported preparation fail.

Cache keys include the effective manifest/lock, selected runtime and ABI, target platform/architecture, source policy, and artifact integrity information.
Do not reuse artifacts from a different configured source simply because the name and version match a corporate package.

## 14. SQL installations and connections

SQL is a map of named connections with an explicit default:

```yaml
environments:
  sql:
    default: scratch
    connections:
      scratch:
        driver: duckdb
        database: ":memory:"
        access: read_write
      archive:
        driver: duckdb
        database: data/archive.duckdb
        access: read_only
        options:
          threads: 2
```

The default with no SQL configuration is one DuckDB in-memory connection.
Its read-write database semantics do not grant filesystem writes.
The in-memory connection belongs to a worker generation and is lost when that generation is replaced.

`driver` is a tagged adapter name, initially `duckdb`, later `sqlite`, `postgres`, or an explicitly installed adapter.
`options` is adapter-specific, validated by that adapter, and cannot override reserved identity or access fields.
Unknown drivers and options are not silently ignored.

For a persistent writable database:

```yaml
permissions:
  filesystem:
    allow_write: [state/database]
environments:
  sql:
    default: main
    connections:
      main:
        driver: duckdb
        database: state/database/analysis.duckdb
        access: read_write
```

A dedicated directory makes the necessary database and sidecar/temp-file scope clear.
A connection declaration never grants broad parent-directory write access implicitly.
Read-only archives should also have filesystem write denials when another grant would otherwise let the worker reopen or replace them.

For a future PostgreSQL adapter:

```yaml
permissions:
  network:
    mode: allowlist
    allow:
      - protocol: tcp
        host: analytics-db.corp.example
        port: 5432
        allow_private: true

environments:
  sql:
    default: analytics
    connections:
      analytics:
        driver: postgres
        host: analytics-db.corp.example
        port: 5432
        database: analytics
        user: agent_reader
        password: {from_env: ANALYTICS_DB_PASSWORD}
        access: read_only
        tls: verify_full
```

Connection selection and network authorization remain separate.
The sandbox/provider checks the requested route and returns any policy error through Console.
Configuring a default connection does not prohibit user code from making other connections that its permissions allow.

`access: read_only` asks the adapter to open a read-only connection; it is not an independent security boundary against arbitrary R/Python/SQL.
Use database-side roles for a remote service and filesystem restrictions for an embedded database.
DuckDB exposes external-access/security controls separately from connection access mode.[9] Extension loading, remote reads, exports, and other external I/O still need their corresponding preparation and sandbox permissions.

For installations, allow a SQL connection's `runtime` selector to name an embedded adapter/version, an approved driver/library, or a supported CLI executable.
Do not require a fake “SQL interpreter” common to all databases.
The usual DuckDB adapter uses the selected language environment; a PostgreSQL connection does not need a local PostgreSQL server installation.

For example, a future SQLite adapter could explicitly select a CLI installation:

```yaml
environments:
  sql:
    default: archive
    connections:
      archive:
        driver: sqlite
        runtime:
          kind: cli
          executable: /opt/sqlite/bin/sqlite3
        database: data/archive.sqlite
        access: read_only
```

Only an adapter that actually implements this CLI interaction can accept that selector.
A library-backed adapter can instead define `runtime.kind: embedded` with a supported `version` selector.
Runtime selection does not imply installing anything, spawning a database server, or obtaining network/filesystem grants.

Concurrency must honor each driver's locking model.
Several profiles selecting the same writable database path must not be treated as independent safe writers.
Admission may reject conflicting use or require an explicit supported access mode.
A driver-specific limitation is an actionable error, not a reason to silently switch to another database.

## 15. Resources and scheduling

```yaml
server:
  limits:
    max_sessions: 2

resources:
  memory: 8GiB
  cpus: 2
  processes: 128
  wall_time: 2h
  priority: background
```

Hard resource limits apply to the aggregate worker/relay process tree, not just the initial PID.
The sandbox/provider validates and enforces these limits; Console forwards the settings and its diagnostics.
`memory` is a memory limit, `cpus` is CPU-time bandwidth in core equivalents, and `processes` limits process creation.
`cpus: 2` means at most two cores' worth of CPU time over the provider's quota interval, not “use CPU numbers 0 and 1” and not “2% of the machine.” Avoid an ambiguous `max_cpu_percent` setting.

`wall_time` is a session lifetime budget, not an MCP polling timeout.
Optional future per-evaluation limits must have a separate name.
Preparation processes need their own budget and admission accounting; do not imply a worker memory limit contains an unsupervised host package build.

`priority: background` is a portable scheduling preference, not a hard CPU limit.
Also permit a target-specific `nice: 10` override where supported.
Reject specifying both when their mapping conflicts.
An unsupported scheduling hint may produce a visible warning; an unsupported hard resource limit is a launch error.
Do not relabel an unenforced limit as a hint.

Recommended scheduling default is `background`, to reduce competition with the user's other work.
Leave memory/CPU hard caps unset until requested or imposed by managed policy; arbitrary built-in caps can make ordinary analysis fail.

`server.limits.max_sessions` is the number of active sessions admitted by this controller, not a fleet-wide host limit.
Reserve capacity atomically before preparation/start, count starting/running/retiring generations, and release it after cleanup.
Idle-but-live sessions count.
Reject excess requests with an explicit capacity error rather than creating an unbounded hidden queue.

This remains useful before concurrency exists: the supported value is initially `1`; selecting `2` must fail as unsupported until implemented.
Multiple separate MCP Console servers require a shared supervisor or external resource manager for a real aggregate host ceiling.
Do not claim this per-controller setting provides one.

## 16. Environment variables and secrets

`config.yaml` contains no secret values.
Passwords, tokens, private keys, and other credentials remain in the host's existing environment, authentication configuration, or OS credential facilities.
Credential fields accept references such as `from_env`, never inline literals.
Literal environment settings below are for non-secret values.
The config remains readable according to the sandbox's ordinary read policy; its write protection is not a secret-storage mechanism.

```yaml
env:
  inherit: [LANG, LC_ALL, TZ]
  set:
    OMP_NUM_THREADS: "2"
    PROJECT_TOKEN: {from_env: PROJECT_TOKEN}
  unset: [SSH_AUTH_SOCK]
```

Use a controlled baseline needed to launch the selected runtimes, not automatic inheritance of every controller environment variable.
`inherit` names variables from the controller's environment; values are deliberately forwarded to the target.
`from_env` is an explicit credential/value reference evaluated on the controller unless a later secret-provider type says otherwise.

Resolved secret values go through protected setup channels, not command-line arguments or routine configuration dumps.
`config explain` shows the reference and recipient, not the secret.
A secret intentionally given to the worker is readable by arbitrary code in that worker; do not imply stronger isolation.
Database credentials are delivered only when the connection is prepared, and provider/package-proxy credentials are delivered only to their intended recipient.
SSH authentication remains entirely with the host's already configured passwordless OpenSSH connection.

Environment values are not a route to change managed cache destinations, interpreter choice, proxy enforcement, or library paths behind the typed schema.
Reject conflicting assignments to fields controlled by typed configuration.
Provider-specific environment variables belong to the provider's `command.env`, not the worker `env` map.

Worker `env` values belong only to the worker and its descendants, not the controller, SSH/Docker launcher, provider, relay, or resolver.
In sandboxed sessions, the sandbox injects them after enforcement is established.
Helpers use an implementation-owned baseline plus their configured helper settings.
A worker `LD_PRELOAD` assignment must not affect an unsandboxed launcher.
Provider `command.env`, executable, argv, and working directory are launcher settings, separate from the worker environment.
A restricted profile fails if its provider cannot keep helper and worker environments separate.

Do not add inline secret fields, a Console credential store, password prompting, or executable secret lookup to this configuration.
The configuration names existing environment variables or external tool identities; authentication setup belongs to the host and the selected tools.

## 17. Logs, caches, and retention

### One managed directory

`.agents/console/` is the only persistent root managed by MCP Console for a project:

```text
.agents/console/
  config.yaml
  state/
  sessions/
    <timestamp>-<pid>/
      journal.jsonl
      01/
        transcript.md
        transcript.qmd
        outputs/
        artifacts/
      02/
        transcript.md
        transcript.qmd
        outputs/
        artifacts/
  cache/
    r/
    python/
    duckdb/
```

Configuration inputs such as a Dockerfile or requirements manifest also live under this directory when Console manages them.
There is no separate default state directory or configurable log/cache root elsewhere.
The `":workspace"` baseline protects this root through its `.agents` read entry for the workload and subprocesses.
Deliberate native overrides can grant writes; Console does not impose a separate denial ceiling.

The controller records transcripts, logs, projections, output files, and image artifacts from worker responses.
Recording does not grant the worker direct write access to `sessions/` or any other part of the managed directory.
Records for a remote worker are collected in the controller project's managed directory.
Proposed target-side preparation artifacts use the target workspace's `.agents/console/`; their effective protection must account for native write exceptions.

### Logs and session records

Keep the canonical `.agents/console/sessions/` location.
Each server process launch creates its own `<timestamp>-<pid>/` directory there, using the startup timestamp and server PID.
Its `journal.jsonl` is the append-only record for that entire server lifetime, including calls, control events, worker restarts, and crashes.
The server records a crash or retirement before recording the replacement generation.
A new server process creates a new directory and journal rather than appending to a previous server's record.

Every worker generation gets a subdirectory numbered in creation order within that server lifetime: `01`, `02`, `03`, and so on.
The journal associates each generation with its directory and session/profile identity.
Profile names are journal metadata, not separate storage locations or retention namespaces.
Tools can parse the JSONL and filter by profile to create a view or projection.
Each generation directory holds its own `transcript.md`, source-only `transcript.qmd`, output files, images, and other artifacts.
These projections cover that generation's cells and output, so a later export can select a generation without mixing separate runtime lifetimes.
Restarting or replacing a worker preserves the server journal and all earlier generation directories; it does not truncate or overwrite them.
An agent recovering context can read the prior generation projections as well as the current one, and the journal retains the complete history across them.

```yaml
storage:
  logs:
    retention:
      max_age: 30d
      max_size: 2GiB
      sweep_interval: 24h
```

`storage` is a document-level setting for the shared project store, independent of session profiles.
Every profile uses the same log retention and cache cleanup settings; selecting a profile cannot replace them.
This block configures retention for records under `.agents/console/sessions/`; it does not select another directory.
Target-manager diagnostics are returned to the controller for recording there.

Retention operates on complete retired server-session directories, including the journal and every generation's projections and referenced artifacts, not arbitrary files selected by a glob.
`max_age` is time since the server lifetime ended; `max_size` evicts the oldest retired directories.
Never delete a live server's journal or earlier generation directories to meet a retention target.
Report when pinned/live records prevent reaching the budget.

Sweep no more often than `sweep_interval` while the controller runs, including an overdue sweep at startup.
This is not a promise of a background daemon while MCP Console is stopped.
Coordinate cleaners with locks.
A user-invoked prune command may force an eligible sweep and show a dry run.

Logs and transcripts are visible records of the session's code, input, output, and artifacts.
The default `.agents` protection covers controller records, subject to deliberate native overrides.
All current and previous records remain readable by default, as described in section 5; retention and generation boundaries do not add read denials.
Do not execute an exported transcript as part of recording or cleanup.

### Preparation caches and runtime scratch

```yaml
storage:
  cache:
    cleanup:
      interval: 7d
      unused_for: 30d
      max_size: 10GiB
```

Console-owned installer downloads, package artifacts, and managed environments use protected storage under `.agents/console/cache/`.
Preparation adapters route R, Python, and DuckDB artifacts into their respective subdirectories using the tools' supported cache controls.[11][12][13] The `ir` adapter uses its supported controls rather than an invented environment variable.
If preparation itself runs in a sandbox, it writes runner-owned temporary output and the trusted preparation adapter publishes the completed artifacts into the protected cache.
Existing user-selected environments remain user-owned inputs; Console does not take over their storage or cleanup.

These preparation mappings are never reused as writable runtime-cache mappings.
Worker application caches and database spill files use the sandbox's private temporary storage when managed by the session.
That temporary storage is runner-owned scratch, not another persistent Console state directory.
Runtime settings such as cooperating R packages' user cache location are distinct from installer settings.[10] DuckDB's writable spill directory is likewise separate from its prepared extension artifacts.[9]

The cache cleanup settings above are also document-wide.
The proposal supplies no automatic worker-writable cache grant under `.agents/console/`; explicit native policy exceptions remain possible.
The worker may read prepared artifacts as allowed by the sandbox policy, but trusted preparation never consumes the worker's scratch cache as its own package or executable cache.
Publishing selected runtime output under `sessions/<timestamp>-<pid>/` is a controller recording operation, not promotion into a preparation cache.
Tool-specific routing remains with the corresponding adapter, and filesystem validation remains with the sandbox.

### Ownership and cleanup safety

Only prune Console-owned entries under `.agents/console/cache/`.
Do not sweep user-owned environments, native tool caches outside that root, or persistent database files.
`cleanup: disabled` preserves the managed entries when automatic cleanup is unwanted.

The generic cleanup block applies to owned namespaces only.
Cache age means last use, not file modification time.
Pin active environments and their artifacts with leases/reference counts so cleanup cannot break a live worker.
Active-use protection spans every process sharing the cache, not just this controller.
Coordinate activation, publication, and cleanup across processes sharing the managed cache.
Respect tool locks and use supported pruning APIs.
uv specifically documents its cache-management commands and concurrency expectations.[14]

Unused owned entries may be removed when due or to satisfy `max_size`; never indiscriminately clear every cache on a timer.
Cleanup never removes `config.yaml` or active session records as a cache entry.

## 18. Resolution, validation, and switching

The conceptual pipeline is:

```text
load selected YAML as data
  -> select the profile, session settings, and document-wide storage settings
  -> materialize the native baseline and compose explicit adjustments using native precedence
  -> pass the requested policy and path requirements to the sandbox/provider
  -> use its validation result or return its error
  -> preparation adapters prepare configured environments/images/storage as needed
  -> sandbox/provider launches with its enforcement
  -> controller appends to the server journal and records projections in the generation directory
```

The sandbox/provider owns target checks and any required probes through its normal launch interface.
Console does not wrap them in another authorization protocol.
Do not contact every SSH host or build every image just to list profiles.
Console can attach the selected config path and profile to a diagnostic while preserving the sandbox/provider's error details.
It does not rerun those checks with a separate validator or turn an upstream acceptance into a stronger security claim.

Proposed commands:

```sh
mcp-console config init
mcp-console config check
mcp-console config explain --profile remote
mcp-console profiles list
mcp-console serve --profile remote
```

`config explain` should show the selected target, workspace, provider, the sandbox's reported permissions, package preparation settings, environment references, canonical storage paths, and limits.
Pending or failed policy checks are reported using the sandbox/provider's result.
Its default operation reports settings without starting a worker or preparing environments.
A machine-readable JSON form is useful for clients and tests, but the on-disk format remains YAML.

Select a profile at server startup with `serve --profile`; the first worker uses that profile directly.
This sketch does not add named-session creation operations or change the MCP `send`/`prepare` schema.
Any future MCP profile-selection interface belongs in that interface's implementation work.
Switching an existing worker's profile would require replacement, since it may already hold open files, sockets, or loaded code from its current environment.
File edits do not hot-reload into a running session.

## 19. Schema evolution and implementation slices

### Review boundary

This sketch explores the user-facing configuration, the managed-directory layout, and the boundary between Console and its sandbox.
It is not a complete specification or a requirement to synchronize every other design sketch.
Review Console's file discovery, session settings, policy forwarding, storage ownership, and reporting of upstream errors here.
Policy parsing, filesystem safety, network mediation, and enforcement are sandbox responsibilities and are developed and tested there.
An accepted configuration carries the sandbox's guarantees and limitations; Console does not independently verify or extend them.

### Parsing contract

Use an existing YAML 1.2 library's data-only loader and its normal protections and error behavior.
Load the Console fields needed to select a session and fail on errors from that loader or the selected adapter.
Propagate those errors without requiring a custom diagnostic or recovery layer.
Console does not add a YAML pre-scanner, expansion or nesting budgets, or a separate validator for aliases, merge keys, or tags.
Parser resource handling stays with the chosen library.
Keep validation to the ordinary field and profile checks needed for the supported happy path.
Do not build a custom parser, executable YAML features, an include language, or a policy normalization engine.

`version: 1` identifies the Console configuration envelope.
Sandbox policy types, supported fields, compatibility, and access semantics belong to the selected sandbox's schema and version.
Pass policy data through its supported interface and surface its parse or validation errors.
Do not duplicate the sandbox's validators in Console or silently discard fields it rejects.

Only the selected profile needs a sandbox/provider operation; listing profiles does not probe every target.
Provider `options` belong to that provider's parser.
`config check` loads the file and delegates policy checking to the sandbox/provider.
Ordinary language, package, and connection adapters retain their existing input contracts, without acquiring sandbox enforcement responsibilities.

### Suggested sequence of pull requests

| Slice | Deliverable                                                                                                             | Deliberate limit                                                                   |
| ----- | ----------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1     | Ordinary YAML loading at `.agents/console/config.yaml`, profile selection, sandbox policy forwarding, `check`/`explain` | Reuse the sandbox parser and diagnostics                                           |
| 2     | Centralized config, state, transcripts, logs, projections, and preparation storage under `.agents/console/`             | Use native metadata defaults and retain deliberate overrides                       |
| 3     | Expose the sandbox's supported proxy and destination settings                                                           | No Console network validator or enforcement layer                                  |
| 4     | Language selection, resolution controls, explicit manifests and repository policy                                       | Preserve current activation contracts; disclose existing host-build trust          |
| 5     | Retention, protected preparation caches, runtime scratch routing, scheduling hints                                      | No automatic runtime-cache write grant under the managed directory                 |
| 6     | SSH target, configured setup, remote manager lifecycle, path mapping                                                    | Passwordless host setup and an existing remote workspace; no credential management |
| 7     | Services/forwarding and exact local-IPC grants                                                                          | No blanket localhost/LAN allowance                                                 |
| 8     | Docker, Docker Sandbox, and versioned command adapters                                                                  | No silent policy dilution by provider flags                                        |
| 9     | Forward provider resource limits, add session admission and SQL adapters, expose additional proxy features              | Enforcement remains in the sandbox/provider                                        |

These are coherent implementation areas, not a requirement to make exactly nine PRs.
Split further where needed.
In particular, parser support does not constitute security-feature implementation.

The implemented project-editing config is `extends: ":workspace"`; `extends: ":read-only"` is also supported.
No version field is required, and metadata defaults remain deliberately overridable.
The starter uses settings accepted by the bundled sandbox; any unsupported policy is reported through its diagnostics.

### Acceptance tests that preserve the design

Test the public configuration and launcher workflow: discover the single config path, select native built-ins, and forward the baseline with explicit adjustments.
An initializer and custom-profile tests belong to their proposed implementation work.
Cover native defaults, quoted and unquoted built-in selectors, explicit write exceptions and read denials, and unrestricted/external policies; do not assert a mandatory managed-directory write-denial ceiling.
Cover default reads of current and previous records and forwarding of explicit user read restrictions.
Verify that loader and adapter failures reach the caller without an additional Console approval workflow.
Check controller-relative manifests for local and remote targets and forward the sandbox/provider's result for protected preparation inputs.
Preserve automatic runtime resolution outside the worker sandbox through the existing resolver contracts.
Verify that controller recording stays under `.agents/console/`, preparation caches remain separate from writable runtime scratch, and configuration uses credential references with passwordless SSH.
Across restart and crash recovery, verify one server journal, successive generation directories with separate projections, preservation of earlier generations, and a fresh journal for a new server process.
Retention removes only complete retired server record sets.
Different profiles use the same canonical storage and document-wide cleanup policy, with profile identity available in the journal for filtering.

Use the public launcher boundary to check that sandbox acceptance, rejection, and error details reach the caller unchanged, without a different policy being substituted.
Keep session switching, package-input contracts, cache retention, and service lifecycle tests at their existing public boundaries.
Native filesystem and network enforcement regressions belong to the sandbox's suite; they do not add path-safety algorithms or exploit-specific fields to Console's configuration.
Keep repetitive initialization payloads out of per-case transcript snapshots.

## 20. Alternatives and decisions worth revisiting

### YAML versus TOML

**Recommended: YAML first.** It keeps nested targets, connection maps, rule objects, comments, and inline Dockerfiles readable in one file.
A data-only loader avoids treating YAML as a programming language.

**Reasonable alternative: TOML only.** It aligns with Codex's file format and has fewer YAML-specific parsing surprises.
The cost is more table syntax for profiles and arrays of network/compute objects.
The semantic model above does not depend on YAML, so switching the serialization now would not invalidate the design.

**Another alternative: both with exact semantic parity.** This helps users who strongly prefer one, but doubles parser/editor/docs testing.
Do not auto-merge `config.yaml` and `config.toml`; their coexistence must be an error or require an explicit selection.
Never let one format express an authority feature the other cannot represent.
Defer dual-format support until it earns its cost.

### Permission spelling

Expose the sandbox's supported permission model.
The allow/deny names in these examples are proposed presentation choices; any mapping to the runner's schema must stay mechanical.
A path-to-access representation may be a closer fit to that schema.[1] Choose the spelling during integration without adding Console-specific precedence, path matching, or safety rules.

### Complete profiles versus mandatory orthogonal registries

**Recommended:** a complete inline default profile, optional named profiles, and optional reusable component definitions.
This keeps most files short while supporting an SSH/Docker/environment matrix when a user actually needs one.

**Reasonable alternative:** require separate `targets`, `policies`, `environments`, and `profiles` catalogs.
It eliminates duplication in larger team configurations, but makes a simple “write here, deny this, allow that API” file unnecessarily indirect.

**Reasonable smaller alternative:** profiles only, with no `definitions` in the first release.
Copy a few target blocks until real duplication justifies the extra machinery.
Keep the top-level schema slots reserved, rather than implementing an elaborate reference system preemptively.

### Separate target and sandbox versus one provider

**Recommended:** separate them.
SSH answers where; Docker answers what compute container; a sandbox answers which permissions are enforced.
The selected sandbox/provider decides whether it supports their composition.

**Reasonable alternative:** a single `execution.provider` owns everything, including remote placement and security.
This is simpler for a single vendor sandbox, but either duplicates SSH+Docker combinations or requires inventing a nested provider language.
Even with one user-facing provider, retain the separation in the normalized internal model.

**Another reasonable alternative:** one tagged `run_on` node for local, SSH, Docker, Docker Sandbox, or command execution, with one optional `inside` container.
The earlier configuration exploration used this approach.[15] It hides the transport/compute distinction in ordinary files, at the cost of less uniform composition.
Prefer it if the simpler entry point outweighs independently reusable transport and compute settings.
URI shorthands are optional: a short mapping with an explicit remote workspace avoids inventing an implicit workspace or synchronization policy.
Keep any shorthand equivalent under inheritance, not merely when read in isolation.

### Default package behavior

**Recommended:** retain `resolution: automatic` and the existing host resolver behavior.

**Reasonable alternative:** select `explicit` when only declared or explicitly requested packages should be prepared.
This is a product/workflow decision, not a reason to conflate worker network permissions with installer access.

### Exact URL filtering now versus destination filtering first

**Recommended:** expose supported origins and exact TCP endpoints first; reserve the stronger typed URL rule for a capable sandbox/proxy.
They cover the common API and database cases without silently breaking certificate assumptions.

**Reasonable alternative:** expose a provider's TLS-terminating mediation and path/method rules earlier.
Certificate handling, compatibility, and URL validation remain with that provider.
Console does not implement the proxy to support its configuration fields.

### What should stay out of version 1

Do not build a general orchestration DSL, unrestricted shell interpolation, multiple inheritance, arbitrary provider composition graphs, automatic project synchronization, a package-manager replacement, or live permission mutation.
The configuration should describe a session and its requested capabilities, not become another programming language.

## References

External details were checked on 2026-09-09.
Upstream pages are evolving; the ideas in this document are MCP Console proposals rather than claims of source compatibility.

| Source | Reference                                                                    |
| ------ | ---------------------------------------------------------------------------- |
| 1      | [Codex permission profiles and rules][1]                                     |
| 2      | [Anthropic Sandbox Runtime README][2]                                        |
| 3      | [MCP Console README at the inspected commit][3]                              |
| 4      | [MCP Console CLI at the inspected commit][4]                                 |
| 5      | [Shiny `runApp` reference][5]                                                |
| 6      | [Docker Sandbox security model][6]                                           |
| 7      | [MCP Console requirements and environments][7]                               |
| 8      | [uv settings and index strategy][8]                                          |
| 9      | [DuckDB configuration reference][9]                                          |
| 10     | [R user directories][10]                                                     |
| 11     | [renv cache and library paths][11]                                           |
| 12     | [pkgcache configuration][12]                                                 |
| 13     | [uv environment variables][13]                                               |
| 14     | [uv cache behavior and maintenance][14]                                      |
| 15     | [Earlier configuration exploration, PR #156 (historical, not normative)][15] |

[1]: https://developers.openai.com/codex/permissions
[2]: https://github.com/anthropics/sandbox-runtime/blob/main/README.md
[3]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/README.md
[4]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/src/cli.rs
[5]: https://shiny.posit.co/r/reference/shiny/latest/runapp.html
[6]: https://docs.docker.com/ai/sandboxes/security/
[7]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/docs/REQUIREMENTS.md
[8]: https://docs.astral.sh/uv/reference/settings/
[9]: https://duckdb.org/docs/current/configuration/overview.html
[10]: https://stat.ethz.ch/R-manual/R-devel/library/tools/help/R_user_dir.html
[11]: https://rstudio.github.io/renv/reference/paths.html
[12]: https://github.com/r-lib/pkgcache
[13]: https://docs.astral.sh/uv/reference/environment/
[14]: https://docs.astral.sh/uv/concepts/cache/
[15]: https://github.com/t-kalinowski/mcp-console/pull/156
