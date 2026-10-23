use std::ffi::{CStr, c_int};

type TryEval = unsafe extern "C-unwind" fn(libr::SEXP, libr::SEXP, *mut c_int) -> libr::SEXP;

/// Calls a language adapter held in a process-lifetime private R environment.
pub(crate) struct Bridge {
    state: libr::SEXP,
    try_eval: TryEval,
    next_evaluation_id: std::cell::Cell<u64>,
    language: &'static str,
}

impl Bridge {
    pub(crate) fn initialize(initializer: &str, language: &'static str) -> Result<Self, String> {
        #[cfg(unix)]
        let library = libloading::os::unix::Library::this();
        #[cfg(windows)]
        let library = libloading::os::windows::Library::open_already_loaded("R.dll")
            .map_err(|e| e.to_string())?;
        let try_eval = unsafe {
            *library
                .get::<TryEval>(b"R_tryEval\0")
                .map_err(|error| format!("failed to load R_tryEval: {error}"))?
        };
        let initializer_length = c_int::try_from(initializer.len())
            .map_err(|_| format!("{language} bridge exceeds R's maximum string size"))?;
        let (evaluation_error, state, is_environment) = harp::top_level_exec(|| {
            // SAFETY: This runs on R's main thread. The top-level boundary
            // contains allocation failures, and R_tryEval contains errors raised
            // while R parses and evaluates the fixed bridge source.
            unsafe {
                let source = libr::Rf_protect(r_string(initializer, initializer_length));
                let str2expression = libr::Rf_install(c"str2expression".as_ptr());
                let call = libr::Rf_protect(libr::Rf_lang2(str2expression, source));
                let eval = libr::Rf_install(c"eval".as_ptr());
                let call = libr::Rf_protect(libr::Rf_lang2(eval, call));
                let mut evaluation_error = 0;
                let state = try_eval(call, libr::R_BaseEnv, &mut evaluation_error);
                if evaluation_error != 0 || state.is_null() {
                    libr::Rf_unprotect(3);
                    return (evaluation_error, state, false);
                }
                let state = libr::Rf_protect(state);
                let is_environment = libr::TYPEOF(state) == libr::ENVSXP as c_int;
                if is_environment {
                    libr::R_PreserveObject(state);
                }
                libr::Rf_unprotect(4);
                (evaluation_error, state, is_environment)
            }
        })
        .map_err(|error| format!("failed to initialize the {language} bridge: {error}"))?;
        if evaluation_error != 0 {
            return Err(format!(
                "{language} bridge initialization failed during R evaluation"
            ));
        }
        if state.is_null() {
            return Err(format!(
                "{language} state initialization returned a null R object"
            ));
        }
        if !is_environment {
            return Err(format!(
                "{language} state initialization did not produce an environment"
            ));
        }
        Ok(Self {
            state,
            try_eval,
            next_evaluation_id: std::cell::Cell::new(1),
            language,
        })
    }

    pub(crate) fn evaluate(&self, source: &str) -> Result<(), String> {
        self.evaluate_completed(source).map(|_| ())
    }

    pub(crate) fn evaluate_completed(&self, source: &str) -> Result<bool, String> {
        self.evaluate_completed_with(source, |_| Ok(()))
            .map(|value| value.is_some())
    }

    pub(crate) fn evaluate_completed_string(&self, source: &str) -> Result<Option<String>, String> {
        self.evaluate_completed_with(source, |value| {
            String::try_from(harp::object::RObject::view(value)).map_err(|error| error.to_string())
        })
    }

    fn evaluate_completed_with<T>(
        &self,
        source: &str,
        convert: impl FnOnce(libr::SEXP) -> Result<T, String>,
    ) -> Result<Option<T>, String> {
        let source_length = c_int::try_from(source.len())
            .map_err(|_| format!("{} source exceeds R's maximum string size", self.language))?;
        let evaluation_id = format!("e{}", self.next_evaluation_id.get());
        self.next_evaluation_id
            .set(self.next_evaluation_id.get() + 1);
        let evaluation_id_length = c_int::try_from(evaluation_id.len())
            .expect("generated evaluation IDs should fit in an R string");
        let result = harp::top_level_exec(|| {
            // SAFETY: This runs on R's main thread. The outer top-level
            // boundary contains allocation errors; R_tryEval contains errors
            // raised while the preserved private environment calls its adapter.
            unsafe {
                let source = libr::Rf_protect(r_string(source, source_length));
                let evaluation_id =
                    libr::Rf_protect(r_string(&evaluation_id, evaluation_id_length));
                let source_symbol = libr::Rf_install(c"source".as_ptr());
                let evaluate_symbol = libr::Rf_install(c"evaluate".as_ptr());
                let interrupted_symbol = libr::Rf_install(c"interrupted".as_ptr());
                libr::Rf_defineVar(source_symbol, source, self.state);
                let call = libr::Rf_protect(libr::Rf_lang2(evaluate_symbol, evaluation_id));
                let mut evaluation_error = 0;
                let value = (self.try_eval)(call, self.state, &mut evaluation_error);
                let interrupted = evaluation_error != 0
                    && libr::Rf_asInteger(libr::Rf_findVarInFrame(self.state, interrupted_symbol))
                        == 1;
                let value = if evaluation_error == 0 {
                    let value = libr::Rf_protect(value);
                    let converted = convert(value);
                    libr::Rf_unprotect(1);
                    Some(converted)
                } else {
                    None
                };
                libr::Rf_defineVar(source_symbol, libr::R_NilValue, self.state);
                libr::Rf_unprotect(3);
                (evaluation_error, interrupted, value)
            }
        });
        #[cfg(windows)]
        crate::windows::restore_worker_stdio().map_err(|error| error.to_string())?;
        let (evaluation_error, interrupted, value) = result
            .map_err(|error| format!("failed to call the {} bridge: {error}", self.language))?;
        if evaluation_error != 0 {
            if interrupted {
                crate::worker::record_bootstrap_interrupt();
                return Ok(None);
            }
            return Err(format!(
                "{} bridge failed during R evaluation",
                self.language
            ));
        }
        value
            .expect("successful R evaluation should return a value")
            .map(Some)
            .map_err(|error| format!("{} bridge returned {error}", self.language))
    }

    pub(crate) fn call0_integer(&self, function: &CStr) -> Result<c_int, String> {
        self.call0(function, |value| Ok(unsafe { libr::Rf_asInteger(value) }))
    }

    pub(crate) fn call1_string(
        &self,
        function: &CStr,
        argument: &str,
    ) -> Result<Option<String>, String> {
        let argument_length = c_int::try_from(argument.len()).map_err(|_| {
            format!(
                "{} bridge argument exceeds R's maximum string size",
                self.language
            )
        })?;
        let result = harp::top_level_exec(|| {
            // SAFETY: This runs on R's main thread. The outer top-level
            // boundary contains allocation errors; R_tryEval contains errors
            // raised while calling the private environment's fixed function.
            unsafe {
                let function = libr::Rf_install(function.as_ptr());
                let argument = libr::Rf_protect(r_string(argument, argument_length));
                let call = libr::Rf_protect(libr::Rf_lang2(function, argument));
                let mut evaluation_error = 0;
                let value = (self.try_eval)(call, self.state, &mut evaluation_error);
                let value = if evaluation_error == 0 {
                    let value = libr::Rf_protect(value);
                    let value = Option::<String>::try_from(harp::object::RObject::view(value))
                        .map_err(|error| error.to_string());
                    libr::Rf_unprotect(1);
                    Some(value)
                } else {
                    None
                };
                libr::Rf_unprotect(2);
                (evaluation_error, value)
            }
        });
        #[cfg(windows)]
        crate::windows::restore_worker_stdio().map_err(|error| error.to_string())?;
        let (evaluation_error, value) = result
            .map_err(|error| format!("failed to call the {} bridge: {error}", self.language))?;
        if evaluation_error != 0 {
            return Err(format!(
                "{} bridge failed during R evaluation",
                self.language
            ));
        }
        value
            .expect("successful R evaluation should return a value")
            .map_err(|error| format!("{} bridge returned {error}", self.language))
    }

    fn call0<T>(
        &self,
        function: &CStr,
        convert: impl FnOnce(libr::SEXP) -> Result<T, String>,
    ) -> Result<T, String> {
        let result = harp::top_level_exec(|| {
            // SAFETY: This runs on R's main thread. The outer top-level
            // boundary contains allocation errors; R_tryEval contains errors
            // raised while calling the private environment's fixed function.
            unsafe {
                let function = libr::Rf_install(function.as_ptr());
                let call = libr::Rf_protect(libr::Rf_lang1(function));
                let mut evaluation_error = 0;
                let value = (self.try_eval)(call, self.state, &mut evaluation_error);
                let value = if evaluation_error == 0 {
                    let value = libr::Rf_protect(value);
                    let value = convert(value);
                    libr::Rf_unprotect(1);
                    Some(value)
                } else {
                    None
                };
                libr::Rf_unprotect(1);
                (evaluation_error, value)
            }
        });
        #[cfg(windows)]
        crate::windows::restore_worker_stdio().map_err(|error| error.to_string())?;
        let (evaluation_error, value) = result
            .map_err(|error| format!("failed to call the {} bridge: {error}", self.language))?;
        if evaluation_error != 0 {
            return Err(format!(
                "{} bridge failed during R evaluation",
                self.language
            ));
        }
        value
            .expect("successful R evaluation should return a value")
            .map_err(|error| format!("{} bridge returned {error}", self.language))
    }
}

fn r_string(value: &str, length: c_int) -> libr::SEXP {
    // SAFETY: The caller runs under R's top-level allocation boundary and
    // immediately protects the returned scalar string.
    unsafe {
        let value = libr::Rf_mkCharLenCE(value.as_ptr().cast(), length, libr::cetype_t_CE_UTF8);
        libr::Rf_ScalarString(value)
    }
}
