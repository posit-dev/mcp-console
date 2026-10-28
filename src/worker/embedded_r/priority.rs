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
        let foreground = super::core::cell_language().is_some();
        // Match R_tryCatch: protect its R-level handler setup and teardown,
        // enabling delivery in the body only if the caller allowed interrupts.
        let suspended = unsafe { libr::get(libr::R_interrupts_suspended) };
        unsafe { libr::set(libr::R_interrupts_suspended, libr::Rboolean_TRUE) };
        let result = harp::top_level_exec(|| unsafe {
            let text = libr::Rf_protect(libr::Rf_ScalarString(libr::Rf_mkCharLenCE(
                source.as_ptr().cast(),
                length,
                libr::cetype_t_CE_UTF8,
            )));
            let interruptible = libr::Rf_protect(libr::Rf_ScalarLogical(c_int::from(
                suspended == libr::Rboolean_FALSE,
            )));
            let call = libr::Rf_protect(libr::Rf_lang3(self.helper.sexp, text, interruptible));
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
            libr::Rf_unprotect(4);
            failure
        });
        unsafe { libr::set(libr::R_interrupts_suspended, suspended) };
        let failure = match result {
            Ok(failure) => failure,
            Err(_) => Some(crate::worker_protocol::ExecutionError::new(
                "RError",
                harp::exec::r_peek_error_buffer(),
            )),
        };
        let interrupted = failure
            .as_ref()
            .is_some_and(|error| error.ename == "Interrupt");
        if let Some(failure) = failure {
            super::core::record_execution_error(failure);
        }
        if interrupted && foreground {
            // R clears this flag before signaling the interrupt condition. The
            // private handler contained its unwind; restore foreground delivery
            // only after all private R calls and output reporting have finished.
            // Never check it here: an R jump must not cross these Rust frames.
            unsafe { libr::set(libr::R_interrupts_pending, 1) };
        }
        Ok(())
    }
}
