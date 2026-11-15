use crate::r_bridge::EvaluationOutcome;
use libr::SEXP;

const SQL_BRIDGE_SOURCE: &str = include_str!("bridge.R");

/// Sets whether R's thread blocks SIGINT and returns the previous setting, so
/// the bridge can start DuckDB's threads where they never accept an interrupt.
#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_set_interrupt_blocked(
    blocked: SEXP,
) -> harp::Result<SEXP> {
    let blocked = bool::try_from(harp::object::RObject::view(blocked))?;
    #[cfg(unix)]
    let previous = crate::worker::set_interrupt_blocked(blocked)
        .map_err(|error| harp::anyhow!("failed to change the interrupt mask: {error}"))?;
    // Windows interrupts arrive as relay events, never on a DuckDB thread.
    #[cfg(not(unix))]
    let previous = {
        let _ = blocked;
        false
    };
    Ok(harp::object::RObject::from(previous).sexp)
}

pub(super) struct Backend(crate::r_bridge::Bridge);

impl Backend {
    pub(super) fn initialize_source(&self, source: &str) -> Result<bool, String> {
        let outcome = self.0.call1_string(c"initialize_connection", source)?;
        if outcome.as_deref() == Some("interrupted") {
            crate::worker::record_bootstrap_interrupt();
        }
        Ok(outcome.as_deref() == Some("ready"))
    }

    pub(super) fn initialize_managed(&self) -> Result<(), String> {
        let outcome = crate::r_environment::without_automatic_resolution(|| {
            self.0.call0_integer(c"initialize_managed_connection")
        })?;
        if outcome == -1 {
            crate::worker::record_bootstrap_interrupt();
        }
        Ok(())
    }

    pub(super) fn initialize() -> Result<Self, String> {
        crate::r_bridge::Bridge::initialize(SQL_BRIDGE_SOURCE, "SQL").map(Self)
    }

    pub(super) fn evaluate(&self, source: &str) -> Result<(), String> {
        match self.0.evaluate_outcome(source)? {
            EvaluationOutcome::Complete => {}
            EvaluationOutcome::Error(message) => crate::worker::record_execution_error(
                crate::worker_protocol::ExecutionError::new("SQLError", message),
            ),
            EvaluationOutcome::Interrupted => crate::worker::record_execution_error(
                crate::worker_protocol::ExecutionError::new("KeyboardInterrupt", ""),
            ),
        }
        Ok(())
    }

    pub(super) fn close_result(&self) -> Result<(), String> {
        self.0.call0_integer(c"close_last_result").map(|_| ())
    }

    pub(super) fn restore_managed(&self) -> Result<(), String> {
        match self.0.call0_integer(c"restore_managed_connection")? {
            1 => Ok(()),
            _ => Err("SQL DBI bridge did not restore managed DuckDB".to_string()),
        }
    }
}
