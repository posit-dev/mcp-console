use std::error::Error;
use std::ffi::{CStr, c_int};

use harp::object::RObject;

use super::{ConsoleChannel, emit_output};

pub(super) struct Parser {
    helper: RObject,
}

impl Parser {
    pub(super) fn initialize() -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            helper: harp::parse_eval_base(include_str!("parse.R"))?,
        })
    }

    pub(super) fn complete(&self, source: &str) -> Result<bool, String> {
        let length = c_int::try_from(source.len())
            .map_err(|_| "R source exceeds R's maximum string size".to_string())?;
        // Direct evaluation bypasses REPL task callbacks, history, and .Last.value.
        // R_ToplevelExec contains unexpected errors and interrupts. The helper
        // returns NULL or a diagnostic without invoking top-level error handling.
        let result = harp::top_level_exec(|| unsafe {
            let text = libr::Rf_mkCharLenCE(source.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
            let text = libr::Rf_protect(libr::Rf_ScalarString(text));
            let call = libr::Rf_protect(libr::Rf_lang2(self.helper.sexp, text));
            let value = libr::Rf_protect(libr::Rf_eval(call, libr::R_BaseEnv));
            let message = if value == libr::R_NilValue {
                None
            } else {
                let text = libr::Rf_translateCharUTF8(libr::STRING_ELT(value, 0));
                Some(CStr::from_ptr(text).to_string_lossy().into_owned())
            };
            // Only non-allocating cleanup follows construction of the Rust value.
            libr::Rf_unprotect(3);
            message
        });
        let message = match result {
            Ok(message) => message,
            // R already reported an unexpected error or interrupt.
            Err(_) => return Ok(false),
        };
        if let Some(message) = message {
            emit_output(
                ConsoleChannel::Diagnostic,
                format!("Error: {message}\n").as_bytes(),
            );
            return Ok(false);
        }
        Ok(true)
    }
}
