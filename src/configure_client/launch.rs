//! Resolve a client launch command without retaining disposable cache paths.

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::cli::{ConfigureClient, ConfigureClientOptions};

pub(super) struct Launch {
    pub(super) command: String,
    args: Vec<String>,
    install: Option<String>,
}

impl Launch {
    pub(super) fn resolve(
        options: &ConfigureClientOptions,
        directory: &Path,
    ) -> Result<Self, String> {
        let mut launch = Self {
            command: "uvx".into(),
            args: vec![match &options.pin_version {
                Some(version) => format!("mcp-console@{version}"),
                None => "mcp-console".into(),
            }],
            install: None,
        };
        if options.no_uvx {
            let executable = std::env::current_exe()
                .and_then(|path| path.canonicalize())
                .map_err(|error| format!("Cannot locate this executable: {error}"))?;
            let target = if let Some(environment) = temporary_uv_environment(&executable)? {
                launch.install = Some(installed_requirement(environment)?);
                // uv marks both its normal cache and --no-cache temporary roots.
                // Query its persistent launcher directory before any installation
                // so dry-run can show the same command and configuration path.
                let output = Command::new("uv")
                    .args(["tool", "dir", "--bin"])
                    .stdin(Stdio::null())
                    .output()
                    .map_err(|error| format!("Cannot run uv tool dir --bin: {error}"))?;
                if !output.status.success() {
                    return Err(format!(
                        "uv tool dir --bin failed: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ));
                }
                let bin = String::from_utf8(output.stdout).map_err(|error| error.to_string())?;
                let bin = bin.trim();
                if bin.is_empty() {
                    return Err("uv tool dir --bin returned no directory".into());
                }
                directory
                    .join(bin)
                    .join(format!("mcp-console{}", std::env::consts::EXE_SUFFIX))
            } else {
                executable
            };
            launch.command = target
                .into_os_string()
                .into_string()
                .map_err(|_| "Executable path is not valid UTF-8")?;
            launch.args.clear();
        }
        launch.args.push("serve".into());
        Ok(launch)
    }

    pub(super) fn arguments(&self, client: ConfigureClient, serve_args: &[String]) -> Vec<String> {
        let mut args = self.args.clone();
        match client {
            ConfigureClient::Codex => {
                args.extend(["-c".into(), "sandbox.profile=inherit_codex".into()])
            }
            ConfigureClient::Claude => {}
        }
        // Explicit serve arguments take precedence over client defaults.
        args.extend_from_slice(serve_args);
        args
    }

    pub(super) fn prepare(&self, dry_run: bool) -> Result<(), String> {
        if let Some(package) = &self.install {
            // Preserve the original requirement for uv, but keep its query
            // out of our report and uv's source-bearing diagnostics.
            let query = package
                .find('?')
                .map(|start| package[start..].split('#').next().unwrap());
            let redact = |output: &[u8]| {
                let text = String::from_utf8_lossy(output);
                match query {
                    Some(query) => text.replace(query, "?<redacted>"),
                    None => text.into_owned(),
                }
            };
            let action = if dry_run {
                "Would install"
            } else {
                "Installing"
            };
            println!(
                "{action} a persistent executable: uv tool install {:?}",
                redact(package.as_bytes())
            );
            println!("Executable: {}", self.command);
            if !dry_run {
                std::io::stdout()
                    .flush()
                    .map_err(|error| error.to_string())?;
                let output = Command::new("uv")
                    .args(["tool", "install", package])
                    .stdin(Stdio::null())
                    .output()
                    .map_err(|error| format!("Cannot run uv tool install: {error}"))?;
                print!("{}", redact(&output.stdout));
                eprint!("{}", redact(&output.stderr));
                if !output.status.success() {
                    return Err(format!(
                        "uv tool install failed with {}; no client configuration files changed",
                        output.status
                    ));
                }
                if !PathBuf::from(&self.command).is_file() {
                    return Err(format!("uv tool install did not create {}", self.command));
                }
            }
        }
        Ok(())
    }
}

fn installed_requirement(environment: &Path) -> Result<String, String> {
    // Requires uv 0.11.29+ to retain recorded archive hashes. Older uv versions
    // are outside this path's supported contract; do not add compatibility logic.
    // Directory requirements intentionally rebuild the recorded checkout, which
    // must remain unchanged during setup; separate builds may differ in bytes.
    // Let uv reconstruct its installed requirement, including direct URLs,
    // archive hashes, and resolved Git commits. Index installs record only a
    // version, losing CLI-only resolver options. Never resolve that version
    // again through a potentially different index or --find-links source.
    let output = Command::new("uv")
        .args(["pip", "freeze", "--python"])
        .arg(environment)
        .args(["--color", "never"])
        .stdin(Stdio::null())
        .output()
        .map_err(|error| {
            format!("Cannot read this temporary tool's source with uv pip freeze: {error}")
        })?;
    if !output.status.success() {
        return Err(format!(
            "uv pip freeze failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let requirements = String::from_utf8(output.stdout).map_err(|error| error.to_string())?;
    for requirement in requirements.lines() {
        if requirement.starts_with("mcp-console==") {
            return Err("Cannot recover this temporary tool's index or --find-links source from its installed version; install Console persistently with uv tool install using the same resolver options, then run configure-client --no-uvx".into());
        }
        if requirement.starts_with("mcp-console @ ") {
            return Ok(requirement.to_owned());
        }
    }
    Err("Cannot identify this temporary tool's source; install Console persistently with uv tool install <SOURCE> before using --no-uvx".into())
}

fn temporary_uv_environment(executable: &Path) -> Result<Option<&Path>, String> {
    let Some(environment) = executable
        .ancestors()
        .find(|directory| directory.join("pyvenv.cfg").is_file())
    else {
        return Ok(None);
    };
    let configuration = environment.join("pyvenv.cfg");
    let contents = fs::read_to_string(&configuration)
        .map_err(|error| format!("Cannot read {}: {error}", configuration.display()))?;
    if !contents.lines().any(|line| line.starts_with("uv = ")) {
        return Ok(None);
    }
    // uv relocates disposable tool environments to CACHE/archive-v0/ID, even
    // with --no-cache. Only that cache root's tag implies disposal: persistent
    // uv environments have their own tag and may be below unrelated caches.
    let Some(cache) = environment
        .parent()
        .filter(|bucket| bucket.file_name().is_some_and(|name| name == "archive-v0"))
        .and_then(Path::parent)
    else {
        return Ok(None);
    };
    let marker = cache.join("CACHEDIR.TAG");
    match fs::read(&marker) {
        Ok(contents) => Ok(contents
            .starts_with(b"Signature: 8a477f597d28d172789f06886806bc55")
            .then_some(environment)),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("Cannot read {}: {error}", marker.display())),
    }
}
