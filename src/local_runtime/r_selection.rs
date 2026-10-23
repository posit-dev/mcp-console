//! Explicit R launchers are probed only inside the worker's native policy.
use super::RInstallation;
use crate::resolver::ResolverStopHandle;
use crate::resolver::process::{ResolverProcess, resolver_command};
use sha2::{Digest as _, Sha256};
use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::process::Stdio;

#[derive(Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub(super) struct FileIdentity {
    #[serde(with = "super::native_path")]
    path: PathBuf,
    #[serde(with = "super::native_path")]
    target: PathBuf,
    digest: [u8; 32],
    length: u64,
    modified: std::time::SystemTime,
    #[cfg(unix)]
    device: u64,
    #[cfg(unix)]
    inode: u64,
    #[cfg(unix)]
    mode: u32,
}
impl FileIdentity {
    fn capture(path: &Path) -> Result<Self, String> {
        let mut options = std::fs::File::options();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.custom_flags(libc::O_NONBLOCK);
        }
        let mut file = options.open(path).map_err(|error| {
            format!(
                "R installation resource {} is unusable: {error}",
                path.display()
            )
        })?;
        let metadata = file.metadata().map_err(|error| error.to_string())?;
        if !metadata.is_file() {
            return Err(format!(
                "R installation resource {} is not a file",
                path.display()
            ));
        }
        let mut digest = Sha256::new();
        let mut buffer = [0; 64 * 1024];
        loop {
            let count = file.read(&mut buffer).map_err(|error| error.to_string())?;
            if count == 0 {
                break;
            }
            digest.update(&buffer[..count]);
        }
        #[cfg(unix)]
        use std::os::unix::fs::MetadataExt;
        Ok(Self {
            path: path.to_path_buf(),
            target: std::fs::canonicalize(path).map_err(|error| error.to_string())?,
            digest: digest.finalize().into(),
            length: metadata.len(),
            modified: metadata.modified().map_err(|error| error.to_string())?,
            #[cfg(unix)]
            device: metadata.dev(),
            #[cfg(unix)]
            inode: metadata.ino(),
            #[cfg(unix)]
            mode: metadata.mode(),
        })
    }
}
impl RInstallation {
    pub(crate) fn inspect(
        executable: &Path,
        on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let context = |error: String| format!("r.executable {}: {error}", executable.display());
        (|| {
            if !executable.is_absolute()
                || !std::fs::metadata(executable)
                    .map_err(|error| error.to_string())?
                    .is_file()
            {
                return Err(
                    "expected an existing R executable or launcher, not an R home directory".into(),
                );
            }
            let launcher = FileIdentity::capture(executable)?;
            #[cfg(unix)]
            if launcher.mode & 0o111 == 0 {
                return Err("selected R file is not executable".into());
            }
            // Unix's native binary cannot answer RHOME or CMD. Recognize its
            // installed bin/exec/R layout and use that installation's launcher
            // under the same worker policy, without guessing arbitrary parents.
            #[cfg(unix)]
            let native_home = (launcher.target.file_name() == Some(std::ffi::OsStr::new("R")))
                .then(|| launcher.target.parent())
                .flatten()
                .filter(|parent| parent.file_name() == Some(std::ffi::OsStr::new("exec")))
                .and_then(Path::parent)
                .filter(|parent| parent.file_name() == Some(std::ffi::OsStr::new("bin")))
                .and_then(Path::parent);
            #[cfg(unix)]
            let probe_launcher = native_home.map(|home| home.join("bin/R"));
            #[cfg(windows)]
            let probe_launcher: Option<PathBuf> = None;
            let probe_executable = probe_launcher.as_deref().unwrap_or(executable);
            let home = probe(probe_executable, &["RHOME"], on_started)?;
            let home = home.strip_suffix(b"\n").unwrap_or(&home);
            #[cfg(windows)]
            let home = home.strip_suffix(b"\r").unwrap_or(home);
            #[cfg(unix)]
            let home = {
                use std::os::unix::ffi::OsStringExt as _;
                PathBuf::from(std::ffi::OsString::from_vec(home.to_vec()))
            };
            #[cfg(windows)]
            let home =
                PathBuf::from(String::from_utf8(home.to_vec()).map_err(|error| error.to_string())?);
            if !home.is_absolute() || !home.is_dir() {
                return Err("R launcher returned an invalid R home directory".into());
            }
            #[cfg(unix)]
            if native_home
                .is_some_and(|native| std::fs::canonicalize(&home).ok().as_deref() != Some(native))
            {
                return Err("native R executable does not match its installed launcher".into());
            }
            #[cfg(unix)]
            let resources = {
                use std::os::unix::ffi::OsStringExt;
                let output = probe(
                    probe_executable,
                    &[
                        "CMD",
                        "/bin/sh",
                        "-c",
                        r#"printf '%s\000' "$R_SHARE_DIR" "$R_INCLUDE_DIR" "$R_DOC_DIR""#,
                    ],
                    on_started,
                )?;
                let values = output
                    .strip_suffix(b"\0")
                    .ok_or("R launcher did not terminate resource paths")?
                    .split(|byte| *byte == 0)
                    .collect::<Vec<_>>();
                if values.len() != 3 || values.iter().any(|value| value.is_empty()) {
                    return Err("R launcher must supply share, include, and doc paths".into());
                }
                std::array::from_fn(|index| std::ffi::OsString::from_vec(values[index].to_vec()))
            };
            #[cfg(windows)]
            let resources =
                ["share", "include", "doc"].map(|name| home.join(name).into_os_string());
            let rscript = home.join(if cfg!(windows) {
                "bin/Rscript.exe"
            } else {
                "bin/Rscript"
            });
            #[cfg(windows)]
            let rscript = if rscript.is_file() {
                rscript
            } else {
                home.join("bin/x64/Rscript.exe")
            };
            let library = home.join(if cfg!(target_os = "macos") {
                "lib/libR.dylib"
            } else if cfg!(windows) {
                "bin/x64/R.dll"
            } else {
                "lib/libR.so"
            });
            let mut identity = vec![
                launcher,
                FileIdentity::capture(&rscript)?,
                FileIdentity::capture(&library)?,
            ];
            if let Some(probe_launcher) = probe_launcher {
                identity.push(FileIdentity::capture(&probe_launcher)?);
            }
            for path in &resources {
                if !Path::new(path).is_dir() {
                    return Err("R launcher returned an unavailable resource directory".into());
                }
            }
            // Resource directory targets, rather than directory timestamps, are
            // retained; installing unrelated packages does not change identity.
            identity.push(FileIdentity::capture(&home.join(if cfg!(windows) {
                "etc/Rcmd_environ"
            } else {
                "etc/Renviron"
            }))?);
            Self {
                home,
                resources,
                identity,
                resource_targets: Vec::new(),
            }
            .capture_resource_targets()
        })()
        .map_err(context)
    }
    fn capture_resource_targets(mut self) -> Result<Self, String> {
        self.resource_targets = self
            .resources
            .iter()
            .map(|path| {
                std::fs::canonicalize(Path::new(path))
                    .map(PathBuf::into_os_string)
                    .map_err(|error| error.to_string())
            })
            .collect::<Result<_, _>>()?;
        Ok(self)
    }
    pub(crate) fn validate(&self) -> Result<(), String> {
        // Drift check for trusted installation inputs. This neither pins
        // execution to the opened files nor captures their dependency closure;
        // see docs/REQUIREMENTS.md#host-resolution-and-trust.
        for expected in &self.identity {
            if FileIdentity::capture(&expected.path)? != *expected {
                return Err(format!(
                    "r.executable: selected R installation changed at {}; restart the server to select it again",
                    expected.path.display()
                ));
            }
        }
        for (path, target) in self.resources.iter().zip(&self.resource_targets) {
            if !Path::new(path).is_dir()
                || std::fs::canonicalize(path)
                    .map_err(|error| error.to_string())?
                    .as_os_str()
                    != target
            {
                return Err(format!(
                    "r.executable: selected R resource directory changed: {}",
                    Path::new(path).display()
                ));
            }
        }
        Ok(())
    }
    pub(crate) fn configure(&self, command: &mut std::process::Command) -> Result<(), String> {
        self.validate()?;
        command.env("R_HOME", &self.home).env("RHOME", &self.home);
        for (name, value) in ["R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR"]
            .into_iter()
            .zip(&self.resources)
        {
            command.env(name, value);
        }
        Ok(())
    }
}
fn probe(
    executable: &Path,
    arguments: &[&str],
    started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
) -> Result<Vec<u8>, String> {
    let resolver = ResolverProcess::new();
    let mut command = resolver_command(executable);
    command
        .args(arguments)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for name in [
        "R_HOME",
        "RHOME",
        "R_SHARE_DIR",
        "R_INCLUDE_DIR",
        "R_DOC_DIR",
    ] {
        command.env_remove(name);
    }
    for name in ["R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"] {
        command.env(name, if cfg!(windows) { "NUL" } else { "/dev/null" });
    }
    let invocation = resolver
        .spawn(&mut command, None)
        .map_err(|error| error.to_string())?;
    let output = resolver.collect(invocation, executable, "R installation inspection", started)?;
    if !output.status.success() {
        return Err(format!(
            "R installation inspection failed ({}): {}{}",
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(output.stdout)
}
