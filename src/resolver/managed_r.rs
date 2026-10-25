use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::ManagedR;
use super::process::{
    ResolverInvocation, ResolverOutput, ResolverProcess, ResolverStopHandle, resolver_command,
};

const MANAGED_R_LIBRARY_RESOLVER_SOURCE: &str = include_str!("programs/r_library.R");
const UV_BINARY_RESOLVER_SOURCE: &str = include_str!("programs/uv_binary.R");
const MINIMUM_IR_VERSION: semver::Version = semver::Version::new(0, 4, 0);
const PAK_SUBPROCESS_STARTUP_TIMEOUT_MILLISECONDS: &str = "60000";
const UV_UNAVAILABLE_STATUSES: &[i32] = &[42, 43, 44];

#[derive(Clone)]
struct IrCommand {
    program: OsString,
    display_program: OsString,
    arguments: Vec<OsString>,
}

impl IrCommand {
    fn direct(program: impl Into<OsString>) -> Self {
        Self {
            program: program.into(),
            display_program: OsString::from("ir"),
            arguments: Vec::new(),
        }
    }

    fn through_uv(uv: impl Into<OsString>) -> Self {
        let program = uv.into();
        Self::through_uv_with_display(program.clone(), program)
    }

    fn through_path_uv(uv: impl Into<OsString>) -> Self {
        Self::through_uv_with_display(uv.into(), OsString::from("uv"))
    }

    fn through_uv_with_display(program: OsString, display_program: OsString) -> Self {
        Self {
            program,
            display_program,
            arguments: ["tool", "run", "--from", "r-lib-ir", "ir"]
                .into_iter()
                .map(OsString::from)
                .collect(),
        }
    }

    fn command(&self) -> Command {
        let mut command = resolver_command(Path::new(&self.program));
        command.args(&self.arguments);
        // --vanilla controls ir's final R invocation; its dependency drivers
        // also start Rscript. No preparation child may execute ambient startup.
        let null = if cfg!(windows) { "NUL" } else { "/dev/null" };
        for name in ["R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"] {
            command.env(name, null);
        }
        command
    }

    fn display_program(&self) -> &Path {
        Path::new(&self.display_program)
    }

    fn label(&self) -> String {
        std::iter::once(self.display_program.as_os_str())
            .chain(self.arguments.iter().map(OsString::as_os_str))
            .map(|argument| argument.to_string_lossy())
            .collect::<Vec<_>>()
            .join(" ")
    }
}

/// Captures the chosen bootstrap without invoking installation or version checks.
#[derive(Clone)]
pub(crate) struct ManagedRBootstrap {
    ir: Option<IrCommand>,
    rscript: PathBuf,
}

impl ManagedRBootstrap {
    pub(crate) fn prepare(
        &self,
        python: &mut super::ManagedPythonResolverConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<ManagedRResolverConfiguration, String> {
        let resolver = ResolverProcess::new();
        let mut on_started = Some(on_started);
        let ir = match &self.ir {
            Some(ir) => ir.clone(),
            None => {
                let uv = resolve_uv_with_rscript(
                    &resolver,
                    &mut on_started,
                    &self.rscript,
                    python,
                    false,
                )?
                .expect("selected reticulate bootstrap must resolve uv or fail");
                python.set_resolved_uv(uv.clone());
                IrCommand::through_uv(uv)
            }
        };
        validate_ir_version(&resolver, &mut on_started, &ir)?;
        Ok(ManagedRResolverConfiguration {
            ir,
            rscript: self.rscript.clone(),
        })
    }
}

#[derive(Clone)]
pub(crate) struct ManagedRResolverConfiguration {
    ir: IrCommand,
    rscript: PathBuf,
}

impl ManagedRResolverConfiguration {
    pub(crate) fn resolve_uv(
        &self,
        managed_r: &ManagedR,
        configuration: &super::ManagedPythonResolverConfiguration,
        on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<OsString, String> {
        let resolver = ResolverProcess::new();
        let mut on_started = Some(on_started);
        let mut command = resolver_command(&self.rscript);
        command.arg("--vanilla");
        let _program = super::r_program::RProgram::append(&mut command, UV_BINARY_RESOLVER_SOURCE)?;
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        managed_r.configure_worker(&mut command)?;
        configuration.configure_uv_bootstrap(&mut command);
        let invocation = resolver.spawn(&mut command, None).map_err(|error| {
            format!(
                "failed to resolve `uv` with `{}`: {error}",
                self.rscript.display()
            )
        })?;
        let output = collect_resolver_output(
            &resolver,
            invocation,
            &mut on_started,
            &self.rscript,
            "`uv`",
        )?;
        finish_uv_resolution(output, false)?.ok_or_else(|| {
            "managed R environment does not provide reticulate `uv` resolution".to_string()
        })
    }
}

pub(crate) fn discover(
    python: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<(Option<ManagedRBootstrap>, PathBuf), String> {
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let rscript = discover_rscript(&resolver, &mut on_started)?;
    let ir = select_ir_command(python);
    if ir.is_none() && !probe_ambient_uv(&resolver, &mut on_started, &rscript, python)? {
        return Ok((None, rscript));
    }
    Ok((
        Some(ManagedRBootstrap {
            ir,
            rscript: rscript.clone(),
        }),
        rscript,
    ))
}

pub(crate) fn resolve_r(
    requirements: Vec<String>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    on_configured: impl FnOnce(ManagedRResolverConfiguration),
) -> Result<ManagedR, String> {
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let mut python = super::ManagedPythonResolverConfiguration::capture();
    let configuration = discover_r_resolver_with(&resolver, &mut on_started, &mut python)?
        .ok_or_else(|| "dynamic environment resolution requires `ir` or `uv`".to_string())?;
    // Retain the selected executables even when this first manifest fails.
    on_configured(configuration.clone());
    resolve_r_with_process(&configuration, requirements, &resolver, &mut on_started)
}

pub(crate) fn resolve_r_with(
    configuration: &ManagedRResolverConfiguration,
    requirements: Vec<String>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedR, String> {
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    resolve_r_with_process(configuration, requirements, &resolver, &mut on_started)
}

fn discover_r_resolver_with(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    python: &mut super::ManagedPythonResolverConfiguration,
) -> Result<Option<ManagedRResolverConfiguration>, String> {
    let rscript = discover_rscript(resolver, on_started)?;
    let ir = match select_ir_command(python) {
        Some(ir) => ir,
        None => {
            let Some(uv) = resolve_uv_with_rscript(resolver, on_started, &rscript, python, true)?
            else {
                return Ok(None);
            };
            python.set_resolved_uv(uv.clone());
            IrCommand::through_uv(uv)
        }
    };
    validate_ir_version(resolver, on_started, &ir)?;
    Ok(Some(ManagedRResolverConfiguration { ir, rscript }))
}

fn select_ir_command(python: &super::ManagedPythonResolverConfiguration) -> Option<IrCommand> {
    let path_ir = super::find_path_entry("ir");
    let path_uv = super::find_path_entry("uv");
    if let Some(ir) = path_ir {
        Some(IrCommand::direct(ir))
    } else if let Some(uv) = path_uv {
        Some(IrCommand::through_path_uv(uv))
    } else {
        python
            .explicit_uv()
            .filter(|uv| *uv != OsStr::new("managed"))
            .map(|uv| IrCommand::through_uv(uv.to_os_string()))
    }
}

fn discover_rscript(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
) -> Result<PathBuf, String> {
    if let Some(r_home) = std::env::var_os("R_HOME") {
        let rscript = rscript_in(Path::new(&r_home));
        if !rscript.is_file() {
            return Err(format!(
                "R_HOME must select an existing R installation: {} is missing",
                rscript.display()
            ));
        }
        return Ok(rscript);
    }
    #[cfg(windows)]
    let selected = super::find_r_path_entry().unwrap_or_else(|| PathBuf::from("R"));
    #[cfg(windows)]
    let program = selected.as_path();
    #[cfg(not(windows))]
    let program = Path::new("R");
    let mut command = resolver_command(program);
    command
        .arg("RHOME")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let invocation = resolver.spawn(&mut command, None).map_err(|error| {
        format!(
            "failed to discover the worker R home with `{}`: {error}",
            program.display()
        )
    })?;
    let output =
        collect_resolver_output(resolver, invocation, on_started, program, "worker R home")?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(output.failure(format!(
            "worker R home discovery failed with {}: {}",
            output.status,
            stderr.trim()
        )));
    }
    let r_home = String::from_utf8(output.stdout)
        .map_err(|error| format!("worker R returned a non-UTF-8 home path: {error}"))?;
    let r_home = r_home.trim();
    if r_home.is_empty() {
        return Err("worker R returned an empty home path".to_string());
    }
    Ok(rscript_in(Path::new(r_home)))
}

fn rscript_in(home: &Path) -> PathBuf {
    #[cfg(unix)]
    return home.join("bin/Rscript");
    #[cfg(windows)]
    {
        let executable = home.join("bin/Rscript.exe");
        if executable.is_file() {
            executable
        } else {
            home.join("bin/x64/Rscript.exe")
        }
    }
}

fn resolve_uv_with_rscript(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    rscript: &Path,
    configuration: &super::ManagedPythonResolverConfiguration,
    unavailable_is_bare: bool,
) -> Result<Option<OsString>, String> {
    let output = run_uv_program(resolver, on_started, rscript, configuration, false)?;
    finish_uv_resolution(output, unavailable_is_bare)
}

fn probe_ambient_uv(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    rscript: &Path,
    configuration: &super::ManagedPythonResolverConfiguration,
) -> Result<bool, String> {
    let output = run_uv_program(resolver, on_started, rscript, configuration, true)?;
    if output.status.success() {
        Ok(true)
    } else if matches!(output.status.code(), Some(42 | 43)) {
        Ok(false)
    } else {
        Err(output.failure(uv_resolution_error(&output)))
    }
}

fn run_uv_program(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    rscript: &Path,
    configuration: &super::ManagedPythonResolverConfiguration,
    probe_only: bool,
) -> Result<ResolverOutput, String> {
    let mut command = resolver_command(rscript);
    command.arg("--vanilla");
    let _program = super::r_program::RProgram::append(&mut command, UV_BINARY_RESOLVER_SOURCE)?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if probe_only {
        command.arg("--probe");
    }
    configuration.configure_uv_bootstrap(&mut command);
    let invocation = resolver.spawn(&mut command, None).map_err(|error| {
        format!(
            "failed to inspect ambient reticulate with `{}`: {error}",
            rscript.display()
        )
    })?;
    collect_resolver_output(
        resolver,
        invocation,
        on_started,
        rscript,
        "ambient reticulate `uv`",
    )
}

fn uv_resolution_error(output: &ResolverOutput) -> String {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let detail = if stderr.trim().is_empty() {
        stdout.trim()
    } else {
        stderr.trim()
    };
    format!(
        "reticulate `uv` resolution failed with {}: {detail}",
        output.status
    )
}

fn finish_uv_resolution(
    output: ResolverOutput,
    unavailable_is_bare: bool,
) -> Result<Option<OsString>, String> {
    if !output.status.success() {
        if unavailable_is_bare
            && output
                .status
                .code()
                .is_some_and(|status| UV_UNAVAILABLE_STATUSES.contains(&status))
        {
            return Ok(None);
        }
        return Err(output.failure(uv_resolution_error(&output)));
    }
    let output = String::from_utf8(output.stdout)
        .map_err(|_| "reticulate `uv` resolver returned a non-UTF-8 path".to_string())?;
    #[cfg(windows)]
    let output = output.replace("\r\n", "\n");
    let path = output
        .strip_suffix('\n')
        .filter(|path| !path.is_empty() && !path.contains(['\n', '\r']))
        .map(PathBuf::from)
        .ok_or_else(|| "reticulate `uv` resolver returned an invalid path line".to_string())?;
    if !path.is_absolute() || !path.is_file() {
        return Err(format!(
            "reticulate `uv` resolver returned invalid executable `{}`",
            path.display()
        ));
    }
    Ok(Some(path.into_os_string()))
}

fn resolve_r_with_process(
    configuration: &ManagedRResolverConfiguration,
    requirements: Vec<String>,
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
) -> Result<ManagedR, String> {
    // `ir` uses the preparation process's cache and network permissions.
    // Requirement strings are process arguments, never R source.
    let mut command = configuration.ir.command();
    // Pak's callr subprocess explicitly sources cwd/.Rprofile even when the
    // outer Rscript has --vanilla and R_PROFILE_USER is disabled. Local source
    // references are already forbidden; preparation has no project cwd input.
    let directory = tempfile::Builder::new()
        .prefix("mcp-console-r-preparation-")
        .tempdir()
        .map_err(|error| error.to_string())?;
    command.current_dir(directory.path());
    command.arg("run").arg("--rscript");
    let _rscript = super::r_program::Rscript::append(&mut command, &configuration.rscript)?;
    for requirement in &requirements {
        command.arg("--with").arg(requirement);
    }
    command
        .env("IR_NO_LOCAL_SOURCES", "1")
        // Pak starts its resolver subprocess asynchronously. Its five-second
        // default can expire while the host is oversubscribed, before the
        // resolver subprocess has had a chance to report readiness.
        .env(
            "PKG_SUBPROCESS_TIMEOUT",
            PAK_SUBPROCESS_STARTUP_TIMEOUT_MILLISECONDS,
        )
        // Only the final library-path expression needs this option. ir forwards
        // architecture arguments, not default-package selection, to its resolver.
        .args(["--isolated", "--vanilla", "--default-packages=NULL"]);
    let _program =
        super::r_program::RProgram::append(&mut command, MANAGED_R_LIBRARY_RESOLVER_SOURCE)?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let invocation = resolver.spawn(&mut command, None).map_err(|error| {
        format!(
            "failed to run R package resolver with `{}`: {error}",
            configuration.ir.label()
        )
    })?;
    let output = collect_resolver_output(
        resolver,
        invocation,
        on_started,
        configuration.ir.display_program(),
        "R package",
    )?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(output.failure(format!(
            "R package resolution failed with {}: {detail}",
            output.status
        )));
    }

    let output = String::from_utf8(output.stdout)
        .map_err(|_| "R package resolver returned a non-UTF-8 path".to_string())?;
    let library = PathBuf::from(output);
    if !library.is_absolute() || !library.is_dir() {
        return Err(format!(
            "R package resolver returned invalid library `{}`",
            library.display()
        ));
    }
    let mut libraries = vec![library.clone()];
    if let Some(inherited) = std::env::var_os("R_LIBS") {
        libraries
            .extend(std::env::split_paths(&inherited).filter(|path| !path.as_os_str().is_empty()));
    }
    let r_libs = std::env::join_paths(libraries)
        .map_err(|error| format!("failed to construct R library path: {error}"))?;
    Ok(ManagedR {
        library,
        r_libs,
        rscript: configuration.rscript.clone(),
        requirements,
    })
}

fn validate_ir_version(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    ir: &IrCommand,
) -> Result<(), String> {
    let mut command = ir.command();
    command
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let invocation = resolver.spawn(&mut command, None).map_err(|error| {
        format!(
            "failed to check R package resolver version with `{}`: {error}",
            ir.label()
        )
    })?;
    let output = collect_resolver_output(
        resolver,
        invocation,
        on_started,
        ir.display_program(),
        "R package",
    )?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(output.failure(format!(
            "failed to check R package resolver version with {}: {detail}",
            output.status
        )));
    }

    let output = String::from_utf8(output.stdout)
        .map_err(|_| "R package resolver returned a non-UTF-8 version".to_string())?;
    let reported = output.trim();
    let version = reported
        .strip_prefix("ir ")
        .and_then(|version| semver::Version::parse(version).ok())
        .ok_or_else(|| {
            format!(
                "R package resolution requires `ir` {MINIMUM_IR_VERSION} or later; could not parse `{reported}`"
            )
        })?;
    if version < MINIMUM_IR_VERSION {
        return Err(format!(
            "R package resolution requires `ir` {MINIMUM_IR_VERSION} or later; found `ir` {version}"
        ));
    }
    Ok(())
}

fn collect_resolver_output(
    resolver: &ResolverProcess,
    invocation: ResolverInvocation,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String> {
    resolver.collect(invocation, program, kind, |handle| {
        match on_started.take() {
            Some(started) => started(handle),
            None => Ok(()),
        }
    })
}
