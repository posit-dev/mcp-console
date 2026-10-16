//! A captured existing path or a bounded Python-only candidate list.

use serde::{Deserialize, de::Error as _};
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub(crate) struct PythonChoice {
    pub(crate) executable: Option<PathBuf>,
    pub(crate) managed: Managed,
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Mapping {
    Existing(PathBuf),
    Managed(Managed),
    FirstAvailable(Vec<Value>),
}

#[derive(Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Managed {
    #[serde(deserialize_with = "super::sandbox::supplied")]
    pub(crate) packages: Option<Vec<String>>,
    #[serde(deserialize_with = "super::sandbox::supplied")]
    pub(crate) version: Option<String>,
    #[serde(deserialize_with = "super::resolution::managed")]
    pub(crate) resolution: super::Resolution,
}

impl Default for Managed {
    fn default() -> Self {
        Self {
            packages: None,
            version: None,
            resolution: super::Resolution::Explicit,
        }
    }
}

impl Managed {
    fn validate(&self) -> Result<(), String> {
        if let Some(packages) = &self.packages {
            crate::python_requirement::validate_all(packages)
                .map_err(|error| format!("packages: {error}"))?;
        }
        if let Some(version) = &self.version {
            crate::python_requirement::validate_version_constraint(version)
                .map_err(|error| format!("version: {error}"))?;
        }
        Ok(())
    }

    pub(crate) fn manifest(
        &self,
        native: bool,
    ) -> crate::worker_protocol::PythonRequirementManifest {
        let mut manifest = if native {
            crate::worker_protocol::default_native_python_requirement_manifest()
        } else {
            crate::worker_protocol::default_python_requirement_manifest()
        };
        if let Some(packages) = &self.packages {
            manifest.packages = packages.clone();
        }
        manifest.python_version = self.version.iter().cloned().collect();
        manifest.normalized()
    }
}

pub(super) struct Python(Value);

impl<'de> Deserialize<'de> for Python {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        if value.is_string() || value.is_object() {
            Ok(Self(value))
        } else {
            Err(D::Error::custom(
                "python: expected an existing path or a selection mapping",
            ))
        }
    }
}

enum Candidate {
    Existing(PathBuf),
    ActiveVenv,
    Managed(Managed),
}

impl Python {
    pub(super) fn capture(mut self) -> Result<(PythonChoice, Value), String> {
        let choice = self.select()?;
        Ok((choice, self.0))
    }

    fn select(&mut self) -> Result<PythonChoice, String> {
        if let Some(path) = self.0.as_str() {
            return existing(&capture_path(Path::new(path))?);
        }
        match mapping(self.0.clone())? {
            Mapping::Existing(path) => existing(&capture_path(&path)?),
            Mapping::Managed(managed) => Ok(PythonChoice {
                executable: None,
                managed,
            }),
            Mapping::FirstAvailable(values) => {
                if values.is_empty() {
                    return Err("python.first_available must not be empty".into());
                }
                let count = values.len();
                let mut active = false;
                let mut candidates = Vec::with_capacity(count);
                for (index, value) in values.into_iter().enumerate() {
                    let candidate = if value == "active_venv" && !active {
                        active = true;
                        Candidate::ActiveVenv
                    } else {
                        match mapping(value).map_err(|error| format!("python.first_available[{index}]: {error}"))? {
                            Mapping::Existing(path) => {
                                let path = crate::console_paths::expand_home(&path)?;
                                // Retain the candidate list as policy while exporting
                                // home paths captured from this launch environment.
                                self.0["first_available"][index]["existing"] =
                                    serde_json::to_value(&path).map_err(|error| {
                                        format!("cannot export python.first_available[{index}].existing: {error}")
                                    })?;
                                Candidate::Existing(capture_path(&path)?)
                            },
                            Mapping::Managed(managed) if index + 1 == count => {
                                self.0["first_available"][index]["managed"]["resolution"] =
                                    managed.resolution.name().into();
                                Candidate::Managed(managed)
                            },
                            _ => return Err("python.first_available permits existing paths, one active_venv, and one optional managed candidate last; nested chains are unsupported".into()),
                        }
                    };
                    candidates.push(candidate);
                }
                for candidate in candidates {
                    match candidate {
                        Candidate::Existing(path) => match std::fs::symlink_metadata(&path) {
                            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                            _ => return existing(&path),
                        },
                        Candidate::ActiveVenv => {
                            let Some(path) =
                                std::env::var_os("VIRTUAL_ENV").filter(|path| !path.is_empty())
                            else {
                                continue;
                            };
                            let path = capture_path(Path::new(&path))?;
                            if !std::fs::metadata(&path)
                                .map_err(|error| {
                                    format!("python.active_venv {}: {error}", path.display())
                                })?
                                .is_dir()
                            {
                                return Err(
                                    "python.active_venv must name a standard venv directory".into(),
                                );
                            }
                            return existing(&path);
                        }
                        Candidate::Managed(managed) => {
                            return Ok(PythonChoice {
                                executable: None,
                                managed,
                            });
                        }
                    }
                }
                Err("python.first_available: no candidate is available".into())
            }
        }
    }
}

fn mapping(value: Value) -> Result<Mapping, String> {
    if !value.is_object() || value.as_object().is_some_and(|value| value.len() != 1) {
        return Err("python: expected exactly one of existing, managed, first_available".into());
    }
    let mapping =
        serde_path_to_error::deserialize(value).map_err(|error| format!("python: {error}"))?;
    if let Mapping::Managed(managed) = &mapping {
        managed
            .validate()
            .map_err(|error| format!("python.managed.{error}"))?;
    }
    Ok(mapping)
}

fn capture_path(path: &Path) -> Result<PathBuf, String> {
    if path.as_os_str().is_empty() {
        return Err("python: expected a nonempty path".into());
    }
    let path = crate::console_paths::expand_home(path)
        .map_err(|error| format!("configured Python: {error}"))?;
    std::path::absolute(path).map_err(|error| format!("cannot locate configured Python: {error}"))
}

fn existing(path: &Path) -> Result<PythonChoice, String> {
    let metadata = std::fs::metadata(path)
        .map_err(|error| format!("cannot use existing Python {}: {error}", path.display()))?;
    let executable = if metadata.is_dir() {
        reject_conda(path)?;
        if !path.join("pyvenv.cfg").is_file() {
            return Err(format!(
                "existing Python directory {} must be a standard venv with pyvenv.cfg",
                path.display()
            ));
        }
        path.join(if cfg!(windows) {
            "Scripts/python.exe"
        } else {
            "bin/python"
        })
    } else if metadata.is_file() {
        path.to_path_buf()
    } else {
        return Err(format!(
            "existing Python {} must be an executable or venv directory",
            path.display()
        ));
    };
    if !std::fs::metadata(&executable)
        .map_err(|error| {
            format!(
                "existing Python executable {} is unusable: {error}",
                executable.display()
            )
        })?
        .is_file()
    {
        return Err(format!(
            "existing Python executable {} is not a file",
            executable.display()
        ));
    }
    let resolved = executable.canonicalize().map_err(|error| {
        format!(
            "cannot resolve existing Python executable {}: {error}",
            executable.display()
        )
    })?;
    // Inspect the target's installation without changing venv activation paths.
    for path in [&executable, &resolved] {
        for prefix in path
            .parent()
            .into_iter()
            .chain(path.parent().and_then(Path::parent))
        {
            reject_conda(prefix)?;
        }
    }
    Ok(PythonChoice {
        executable: Some(executable),
        managed: Default::default(),
    })
}

fn reject_conda(prefix: &Path) -> Result<(), String> {
    if prefix
        .join("conda-meta")
        .try_exists()
        .map_err(|error| error.to_string())?
    {
        return Err(format!(
            "existing Python {} is a Conda environment; Conda environments are unsupported; use a standard venv or managed Python",
            prefix.display()
        ));
    }
    Ok(())
}
