use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use crate::resolver::ResolverStopHandle;
use crate::resolver::process::{ResolverProcess, resolver_command};

use super::startup::SelectedPython;

const INSPECTION_SOURCE: &str = include_str!("inspection.py");

pub(crate) fn explicit_executable(value: &std::ffi::OsStr) -> Result<PathBuf, String> {
    let executable = PathBuf::from(value);
    let executable = if executable.components().count() == 1 {
        crate::resolver::find_path_entry(
            executable
                .to_str()
                .ok_or("explicit Python executable is not UTF-8")?,
        )
        .ok_or("explicit Python executable is not on PATH")?
    } else {
        executable
    };
    // Preserve executable and virtualenv spelling, including symlinks.
    std::path::absolute(executable)
        .map_err(|error| format!("cannot locate selected Python: {error}"))
}

/// Executable and environment identity observed together on the host.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct NativePython {
    pub(crate) embedding: SelectedPython,
    pub(crate) prefix: String,
    pub(crate) exec_prefix: String,
    pub(crate) base_prefix: String,
    pub(crate) base_exec_prefix: String,
    pub(crate) metadata: ConversionMetadata,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ConversionMetadata {
    base_executable: String,
    pythonpath: String,
    version: String,
    version_number: String,
    architecture: String,
    conda: bool,
    numpy: Option<NumpyMetadata>,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct NumpyMetadata {
    path: String,
    version: String,
}

/// Describe a selected executable without changing the calling process or
/// selecting a replacement. The selected installation is trusted and must
/// remain stable through initialization; concurrent replacement is unsupported.
pub(crate) fn inspect_native(
    executable: &Path,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<NativePython, String> {
    if !executable.is_absolute() || !executable.is_file() {
        return Err(format!(
            "selected Python executable is not an absolute file: {}",
            executable.display()
        ));
    }
    let selected = executable
        .to_str()
        .ok_or_else(|| "selected Python executable is not UTF-8".to_string())?;
    let result = crate::resolver::result_file::ResultFile::create(&std::env::temp_dir())?;
    let resolver = ResolverProcess::new();
    let mut command = resolver_command(executable);
    command
        // Exclude workspace, PYTHONPATH, and user-site imports. Environment
        // startup hooks still execute under the caller's inspection policy.
        .arg("-I")
        .arg("-c")
        .arg(INSPECTION_SOURCE)
        .arg(result.path())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let invocation = resolver.spawn(&mut command, None).map_err(|error| {
        format!("failed to inspect selected Python executable `{selected}`: {error}")
    })?;
    let output = resolver.collect(invocation, executable, "Python inspection", on_started)?;
    output
        .write_result
        .as_ref()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr);
        let ordinary = String::from_utf8_lossy(&output.stdout);
        return Err(output.failure(format!(
            "selected Python inspection failed ({}): {}{}",
            output.status, ordinary, diagnostic
        )));
    }
    let description: Description = serde_json::from_slice(&result.read(64 * 1024)?)
        .map_err(|error| format!("invalid selected Python configuration: {error}"))?;
    description.validate(executable)?;
    let python_home = if description.base_prefix == description.base_exec_prefix {
        description.base_prefix.clone()
    } else {
        format!(
            "{}:{}",
            description.base_prefix, description.base_exec_prefix
        )
    };
    Ok(NativePython {
        embedding: SelectedPython {
            // sys.executable verifies the child's identity, while the caller's
            // spelling retains a selected virtualenv or other executable symlink.
            python: selected.to_string(),
            libpython: description.libpython,
            python_home,
        },
        prefix: description.prefix,
        exec_prefix: description.exec_prefix,
        base_prefix: description.base_prefix,
        base_exec_prefix: description.base_exec_prefix,
        metadata: description.metadata,
    })
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Description {
    executable: PathBuf,
    libpython: String,
    prefix: String,
    exec_prefix: String,
    base_prefix: String,
    base_exec_prefix: String,
    metadata: ConversionMetadata,
}

impl Description {
    fn validate(&self, selected: &Path) -> Result<(), String> {
        let reported = fs::canonicalize(&self.executable).map_err(|error| {
            format!(
                "selected Python reported unusable executable `{}`: {error}",
                self.executable.display()
            )
        })?;
        let selected_identity = fs::canonicalize(selected).map_err(|error| {
            format!("selected Python executable disappeared during inspection: {error}")
        })?;
        if !self.executable.is_absolute() || reported != selected_identity {
            return Err(format!(
                "selected Python reported a different executable: {}",
                self.executable.display()
            ));
        }
        for (kind, path) in [
            ("prefix", &self.prefix),
            ("exec prefix", &self.exec_prefix),
            ("base prefix", &self.base_prefix),
            ("base exec prefix", &self.base_exec_prefix),
        ] {
            if !Path::new(path).is_absolute() || !Path::new(path).is_dir() {
                return Err(format!("selected Python returned invalid {kind}: {path}"));
            }
        }
        let library = Path::new(&self.libpython);
        if !library.is_absolute() || !library.is_file() {
            return Err(format!(
                "selected Python embedding library is missing: {}",
                library.display()
            ));
        }
        Ok(())
    }
}
