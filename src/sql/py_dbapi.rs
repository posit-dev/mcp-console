use libr::SEXP;

const PYTHON_RUNTIME_SOURCE: &str = include_str!("dbapi.py");

pub(super) enum Provider {
    R,
    Managed,
    Handled,
}

pub(super) fn install_runtime() -> Result<bool, String> {
    crate::python::install_sql_runtime(PYTHON_RUNTIME_SOURCE)
}

pub(super) fn dispatch(source: &str) -> Result<Provider, String> {
    match crate::python::dispatch_sql(source)? {
        crate::python::SqlProvider::R => Ok(Provider::R),
        crate::python::SqlProvider::Managed => Ok(Provider::Managed),
        crate::python::SqlProvider::Handled => Ok(Provider::Handled),
    }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_use_r() -> harp::Result<SEXP> {
    crate::python::use_r_sql().map_err(|error| harp::anyhow!("{error}"))?;
    unsafe { Ok(libr::R_NilValue) }
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_has_python_connection() -> harp::Result<SEXP> {
    let selected =
        crate::python::has_selected_sql_connection().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(selected).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_r_connection_selected() -> harp::Result<SEXP> {
    let selected =
        crate::python::r_sql_connection_selected().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(selected).sexp)
}

#[allow(clippy::result_large_err)]
#[harp::register]
pub extern "C-unwind" fn mcp_console_sql_take_restore_request() -> harp::Result<SEXP> {
    let requested =
        crate::python::take_sql_restore_request().map_err(|error| harp::anyhow!("{error}"))?;
    Ok(harp::object::RObject::from(requested).sexp)
}
