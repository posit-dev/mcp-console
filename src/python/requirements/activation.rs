//! Live managed-environment activation for an already-running CPython.
//!
//! The caller supplies both configurations. Selection, inspection, resolution,
//! requirement commits, and publication belong outside this operation.

use std::fmt;
use std::path::Path;

/// Paths and library identities obtained before entering the live interpreter.
/// `candidate_python` locates the selected environment's activation script;
/// `candidate_executable` is the value to install in CPython and child launches.
pub(crate) struct ActivationInput<'a> {
    pub(crate) candidate_python: &'a str,
    pub(crate) candidate_libpython: &'a str,
    pub(crate) candidate_executable: &'a str,
    pub(crate) running_libpython: &'a str,
}

#[derive(Debug)]
pub(crate) enum ActivationFailure {
    Incompatible {
        candidate_libpython: String,
        running_libpython: String,
    },
    BeforeMutation(String),
    Infrastructure(String),
    /// The original exception and traceback remain in CPython's setup-error
    /// slot. The caller chooses how to report them through its own boundary.
    PythonException,
}

impl fmt::Display for ActivationFailure {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Incompatible {
                candidate_libpython,
                running_libpython,
            } => write!(
                formatter,
                "New environment does not use the same Python binary\nnew libpython: {candidate_libpython}\nold libpython: {running_libpython}"
            ),
            Self::BeforeMutation(message) | Self::Infrastructure(message) => {
                formatter.write_str(message)
            }
            Self::PythonException => formatter.write_str("Python activation raised an exception"),
        }
    }
}

/// Activate a resolved environment in the running interpreter.
///
/// Exact `libpython` text matching retains the reticulate activation policy.
/// A rejected candidate never reaches the activation script or process setup.
/// Once the script starts, its own side effects follow the existing CPython
/// failure contract; this operation does not attempt to undo them.
pub(crate) fn activate_managed_environment(
    input: ActivationInput<'_>,
) -> Result<(), ActivationFailure> {
    ensure_libpython_compatible(input.candidate_libpython, input.running_libpython)?;

    let script = Path::new(input.candidate_python)
        .parent()
        .ok_or_else(|| {
            ActivationFailure::BeforeMutation(
                "selected Python executable has no parent directory".into(),
            )
        })?
        .join("activate_this.py");
    let script = script.to_str().ok_or_else(|| {
        ActivationFailure::BeforeMutation("Python activation script path is not UTF-8".into())
    })?;
    match super::super::library::activate_environment(script, input.candidate_executable) {
        Ok(true) => Ok(()),
        Ok(false) => Err(ActivationFailure::PythonException),
        Err(error) => Err(ActivationFailure::Infrastructure(error)),
    }
}

pub(crate) fn ensure_libpython_compatible(
    candidate_libpython: &str,
    running_libpython: &str,
) -> Result<(), ActivationFailure> {
    if candidate_libpython == running_libpython {
        Ok(())
    } else {
        Err(ActivationFailure::Incompatible {
            candidate_libpython: candidate_libpython.to_owned(),
            running_libpython: running_libpython.to_owned(),
        })
    }
}
