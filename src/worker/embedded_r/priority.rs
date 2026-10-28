//! Reentrant evaluation keeps handled conditions out of R's foreground
//! warning queue. R_ToplevelExec remains the outer native-unwind boundary.

use std::error::Error;
use std::ffi::{CStr, c_int};

use harp::object::RObject;

pub(super) struct Evaluator {
    helper: RObject,
}

impl Evaluator {
    pub(super) fn initialize() -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            helper: harp::parse_eval_base(include_str!("priority.R"))?,
        })
    }

    pub(super) fn evaluate(&self, source: &str) -> Result<(), String> {
        let length =
            c_int::try_from(source.len()).map_err(|_| "R source exceeds maximum string size")?;
        let result = harp::top_level_exec(|| unsafe {
            let text = libr::Rf_protect(libr::Rf_ScalarString(libr::Rf_mkCharLenCE(
                source.as_ptr().cast(),
                length,
                libr::cetype_t_CE_UTF8,
            )));
            let call = libr::Rf_protect(libr::Rf_lang2(self.helper.sexp, text));
            let value = libr::Rf_protect(libr::Rf_eval(call, libr::R_BaseEnv));
            let failure = if value == libr::R_NilValue {
                None
            } else {
                let name = libr::Rf_translateCharUTF8(libr::STRING_ELT(value, 0));
                let message = libr::Rf_translateCharUTF8(libr::STRING_ELT(value, 1));
                Some(crate::worker_protocol::ExecutionError::new(
                    &CStr::from_ptr(name).to_string_lossy(),
                    CStr::from_ptr(message).to_string_lossy().into_owned(),
                ))
            };
            // No allocating R call follows construction of owned Rust values.
            libr::Rf_unprotect(3);
            failure
        });
        let failure = match result {
            Ok(failure) => failure,
            Err(_) => Some(crate::worker_protocol::ExecutionError::new(
                "RError",
                harp::exec::r_peek_error_buffer(),
            )),
        };
        if let Some(failure) = failure {
            super::core::record_execution_error(failure);
        }
        Ok(())
    }
}
