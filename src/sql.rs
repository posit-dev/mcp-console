mod py_dbapi;
mod r_dbi;

thread_local! {
    static R_BACKEND: std::cell::OnceCell<std::rc::Rc<r_dbi::Backend>> = const { std::cell::OnceCell::new() };
}

pub(crate) fn close_r_result() -> Result<(), String> {
    let backend = R_BACKEND.with(|slot| slot.get().cloned());
    if let Some(backend) = backend {
        backend.close_result()?;
    }
    Ok(())
}

pub(crate) fn attach_r() -> Result<(), String> {
    let backend = std::rc::Rc::new(r_dbi::Backend::initialize()?);
    R_BACKEND
        .with(|slot| slot.set(backend))
        .map_err(|_| "R SQL backend already attached".to_string())
}

/// Worker-facing SQL runtime router.
///
/// R DBI connections stay in embedded R, while Python DB-API connections stay
/// in CPython. Rust chooses the active provider for each SQL cell without
/// converting connection objects or result rows between the runtimes.
pub(crate) struct Bridge {
    startup_failure: Option<String>,
}

impl Bridge {
    pub(crate) fn initialize_r_source(&self, source: &str) -> Result<bool, String> {
        self.r_backend()?.initialize_source(source)
    }

    pub(crate) fn new() -> Self {
        Self {
            startup_failure: None,
        }
    }

    pub(crate) fn withhold_startup(&mut self, message: Option<String>) {
        self.startup_failure = message;
    }

    fn r_backend(&self) -> Result<std::rc::Rc<r_dbi::Backend>, String> {
        crate::worker::ensure_r()?;
        R_BACKEND
            .with(|slot| slot.get().cloned())
            .ok_or("R SQL backend is unavailable".into())
    }

    pub(crate) fn evaluate(&mut self, source: &str) -> Result<(), String> {
        close_r_result()?;
        if let Some(message) = &self.startup_failure {
            crate::worker::emit_output(
                crate::worker_protocol::ConsoleChannel::Diagnostic,
                format!("Error: SQL unavailable: {message}; explicit restart required\n")
                    .as_bytes(),
            );
            return Ok(());
        }
        match py_dbapi::dispatch(source)? {
            py_dbapi::Provider::Handled => Ok(()),
            provider @ (py_dbapi::Provider::Managed | py_dbapi::Provider::R) => {
                let r_dbi = self.r_backend()?;
                if matches!(provider, py_dbapi::Provider::Managed) {
                    r_dbi.restore_managed()?;
                }
                r_dbi.evaluate(source)
            }
        }
    }
}

pub(crate) fn install_python_runtime() -> Result<bool, String> {
    py_dbapi::install_runtime()
}
