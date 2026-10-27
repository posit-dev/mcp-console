use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use super::*;

static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);
const PYTHON_PATHS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/native_python_paths.py"
);
const PYTHON_SITECUSTOMIZE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/native_python_sitecustomize.py"
);

struct PythonFixture(PathBuf);

impl PythonFixture {
    fn new() -> Self {
        let sequence = FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!(
            "mcp-console-embed-test-{}-{sequence}",
            std::process::id()
        ));
        std::fs::create_dir(&path).expect("create Python fixture directory");
        Self(path)
    }

    fn executable(&self) -> PathBuf {
        self.0.join("venv/bin/python")
    }

    fn create_venv(&self) -> PathBuf {
        let output = Command::new("python3")
            .args(["-m", "venv", "--without-pip", "--copies"])
            .arg(self.0.join("venv"))
            .output()
            .expect("create test virtual environment");
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let output = Command::new(self.executable())
            .args([PYTHON_PATHS, "site-packages"])
            .output()
            .expect("find virtual environment site packages");
        assert!(output.status.success());
        PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
    }

    fn sitecustomize(&self, site_packages: &Path, mode: &str) {
        std::fs::copy(PYTHON_SITECUSTOMIZE, site_packages.join("sitecustomize.py"))
            .expect("install test startup hook");
        std::fs::write(site_packages.join("inspection-mode"), mode)
            .expect("select startup fixture mode");
    }
}

impl Drop for PythonFixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).expect("remove Python fixture directory");
    }
}

fn receive(reader: &mut crate::sideband::Reader) -> WorkerMessage {
    receive_bounded(reader)
        .expect("native worker response")
        .expect("native worker did not respond")
}

fn receive_bounded(reader: &mut crate::sideband::Reader) -> io::Result<Option<WorkerMessage>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(message) = reader.receive_buffered()? {
            return Ok(Some(message));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(None);
        }
        let timeout = remaining.as_millis().try_into().unwrap_or(i32::MAX);
        let mut descriptor = libc::pollfd {
            fd: reader.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        match unsafe { libc::poll(&mut descriptor, 1, timeout) } {
            0 => return Ok(None),
            -1 if io::Error::last_os_error().kind() == io::ErrorKind::Interrupted => continue,
            -1 => return Err(io::Error::last_os_error()),
            _ => match reader.read_chunk() {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
                Err(error) => return Err(error),
            },
        }
    }
}

fn receive_python_probe(reader: &mut crate::sideband::Reader, child: &mut Child) -> WorkerMessage {
    let reason = match receive_bounded(reader) {
        Ok(Some(message)) => return message,
        Ok(None) => "timed out waiting for a sideband message".to_owned(),
        Err(error) => format!("sideband receive failed: {error}"),
    };
    let _ = child.kill();
    let status = child.wait().expect("native Python probe exit");
    let mut stderr = Vec::new();
    child
        .stderr
        .take()
        .expect("native Python probe stderr")
        .read_to_end(&mut stderr)
        .expect("read native Python probe stderr");
    panic!(
        "native Python probe {reason} (exit {status}): {}",
        String::from_utf8_lossy(&stderr)
    );
}

fn spawn_probe(
    scenario: &str,
) -> (
    std::process::Child,
    crate::sideband::Reader,
    crate::sideband::Writer,
) {
    spawn_probe_with_configuration(scenario, None, None, None)
}

fn spawn_probe_with_configuration(
    scenario: &str,
    configuration: Option<&str>,
    storage: Option<&Path>,
    manifest: Option<&crate::worker_protocol::PythonRequirementManifest>,
) -> (
    std::process::Child,
    crate::sideband::Reader,
    crate::sideband::Writer,
) {
    let (reader, writer, endpoints) = crate::sideband::bind().expect("bind sideband");
    let mut command = Command::new(std::env::current_exe().expect("test executable"));
    command
        .args([
            "--exact",
            "worker::coordinator::tests::native_probe",
            "--nocapture",
        ])
        .env("MCP_CONSOLE_NATIVE_PROBE", scenario)
        .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    if let Some(configuration) = configuration {
        command.env("MCP_CONSOLE_NATIVE_PYTHON_CONFIG", configuration);
    }
    if let Some(storage) = storage {
        command
            .env("TMPDIR", storage)
            .env(crate::settings::MANAGED_TEMPDIR_ENVIRONMENT, "1");
    }
    if let Some(manifest) = manifest {
        command.env(
            "MCP_CONSOLE_MANAGED_PYTHON",
            serde_json::to_string(manifest).expect("serialize managed Python declaration"),
        );
    }
    endpoints.configure_process(&mut command);
    let child = command.spawn().expect("start native worker probe");
    (child, reader, writer)
}

#[test]
fn native_python_setup_runs_cells_without_r() {
    let storage = PythonFixture::new();
    let selected = Command::new("python3")
        .args([PYTHON_PATHS, "executable"])
        .output()
        .expect("find test Python executable");
    assert!(selected.status.success());
    let selected = String::from_utf8(selected.stdout).expect("selected executable");
    let (mut child, mut reader, _writer) = spawn_probe_with_configuration(
        "python_setup",
        Some(selected.trim()),
        Some(&storage.0),
        None,
    );
    assert!(matches!(
        receive_python_probe(&mut reader, &mut child),
        WorkerMessage::Ready
    ));

    let mut cells = Vec::new();
    for _ in 0..6 {
        let mut output = String::new();
        let mut diagnostic = String::new();
        loop {
            let message = receive_python_probe(&mut reader, &mut child);
            match message {
                WorkerMessage::ConsoleOutput { data }
                | WorkerMessage::ExecutionResult { text: data } => output.push_str(&data),
                WorkerMessage::ConsoleDiagnostic { data } => diagnostic.push_str(&data),
                WorkerMessage::Completed => break,
                _ => panic!("unexpected native Python response"),
            }
        }
        cells.push((output, diagnostic));
    }
    assert_eq!(cells[0].0, "42\n");
    assert_eq!(cells[1].0, "43\n");
    assert!(cells[2].1.contains("ValueError: native setup failure"));
    assert_eq!(cells[3].0, "44\n");
    assert_eq!(cells[4], (String::new(), String::new()));
    assert!(cells[5].1.contains(
        "ModuleNotFoundError: No module named 'mcp_console_missing_native_fixture_package'"
    ));
    assert!(
        child
            .wait_with_output()
            .expect("native Python probe exit")
            .status
            .success()
    );
}

#[test]
fn native_python_activation_preserves_runtime_without_r() {
    let storage = PythonFixture::new();
    let resolver =
        crate::resolver::ManagedPythonResolverConfiguration::capture().without_r_bootstrap();
    let manifest = |packages| crate::worker_protocol::PythonRequirementManifest {
        packages,
        python_version: vec![],
        exclude_newer: None,
    };
    let initial = crate::resolver::resolve_python_manifest_for_host(
        manifest(vec!["duckdb".into()]),
        &resolver,
        None,
        |_| Ok(()),
    )
    .expect("prepare initial managed Python environment");
    let candidate = crate::resolver::resolve_python_manifest_for_host(
        manifest(vec!["duckdb".into(), "py-yaml12".into()]),
        &resolver,
        None,
        |_| Ok(()),
    )
    .expect("prepare candidate managed Python environment");
    let configuration = serde_json::to_string(&[
        initial.python().to_str().unwrap(),
        candidate.python().to_str().unwrap(),
    ])
    .unwrap();

    for (scenario, expected_output, expected_error) in [
        (
            "python_activation_success",
            "activation-success 41 True True True True\n",
            None,
        ),
        (
            "python_activation_incompatible",
            "activation-rejected 41 True True\n",
            None,
        ),
        (
            "python_activation_exception",
            "activation-continued 41 True True\n",
            Some("ValueError: native activation hook failed"),
        ),
    ] {
        let (mut child, mut reader, _writer) = spawn_probe_with_configuration(
            scenario,
            Some(&configuration),
            Some(&storage.0),
            Some(initial.requirements()),
        );
        assert!(matches!(
            receive_python_probe(&mut reader, &mut child),
            WorkerMessage::Ready
        ));
        assert!(matches!(
            receive_python_probe(&mut reader, &mut child),
            WorkerMessage::PythonActivated { requirements } if requirements == *initial.requirements()
        ));
        loop {
            match receive_python_probe(&mut reader, &mut child) {
                WorkerMessage::ConsoleOutput { .. } | WorkerMessage::ExecutionResult { .. } => {}
                WorkerMessage::Completed => break,
                _ => panic!("native activation setup failed"),
            }
        }
        let mut output = String::new();
        let mut diagnostic = String::new();
        loop {
            match receive_python_probe(&mut reader, &mut child) {
                WorkerMessage::ConsoleOutput { data }
                | WorkerMessage::ExecutionResult { text: data } => output.push_str(&data),
                WorkerMessage::ConsoleDiagnostic { data } => diagnostic.push_str(&data),
                WorkerMessage::Completed => break,
                _ => panic!("unexpected native activation response"),
            }
        }
        assert_eq!(output, expected_output, "{scenario}");
        if let Some(error) = expected_error {
            assert_eq!(diagnostic.matches(error).count(), 1, "{diagnostic}");
            assert!(diagnostic.contains("fail_activation_hook"), "{diagnostic}");
        } else {
            assert_eq!(diagnostic, "", "{scenario}");
        }
        let result = child
            .wait_with_output()
            .expect("native activation probe exit");
        assert!(
            result.status.success(),
            "{scenario}: {}",
            String::from_utf8_lossy(&result.stderr)
        );
    }
}

#[test]
fn native_python_inspection_preserves_virtualenv_executable_and_startup_output() {
    let fixture = PythonFixture::new();
    let site_packages = fixture.create_venv();
    fixture.sitecustomize(&site_packages, "output");
    let selected = crate::python::inspect_native(&fixture.executable(), |_| Ok(()))
        .expect("inspect virtual environment");
    assert_eq!(
        selected.embedding.python,
        fixture.executable().to_str().unwrap()
    );
    assert_ne!(
        selected.embedding.python_home,
        fixture.0.join("venv").to_str().unwrap()
    );
    assert!(
        selected
            .embedding
            .python_home
            .split(':')
            .all(|root| Path::new(root).is_dir())
    );
    assert!(Path::new(&selected.embedding.libpython).is_file());
}

#[test]
fn native_python_inspection_rejects_invalid_executable_and_missing_library() {
    let fixture = PythonFixture::new();
    let missing = fixture.0.join("missing-python");
    let error = crate::python::inspect_native(&missing, |_| Ok(())).unwrap_err();
    assert!(error.contains("selected Python executable"), "{error}");

    let site_packages = fixture.create_venv();
    fixture.sitecustomize(&site_packages, "missing");
    let error = crate::python::inspect_native(&fixture.executable(), |_| Ok(())).unwrap_err();
    assert!(error.contains("embedding library is missing"), "{error}");

    std::fs::write(
        site_packages.join("fake-library.so"),
        "not a shared library",
    )
    .unwrap();
    fixture.sitecustomize(&site_packages, "unusable");
    let error = crate::python::inspect_native(&fixture.executable(), |_| Ok(())).unwrap_err();
    assert!(error.contains("embedding library is unusable"), "{error}");
}

#[test]
fn native_python_inspection_rejects_old_version_and_other_runtime() {
    let fixture = PythonFixture::new();
    let site_packages = fixture.create_venv();
    fixture.sitecustomize(&site_packages, "old-version");
    let error = crate::python::inspect_native(&fixture.executable(), |_| Ok(())).unwrap_err();
    assert!(error.contains("requires Python 3.10 or later"), "{error}");

    let output = Command::new("cc")
        .args(["-shared", "-fPIC"])
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/native/other_python.c"
        ))
        .arg("-o")
        .arg(site_packages.join("fake-library.so"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    fixture.sitecustomize(&site_packages, "other-library");
    let error = crate::python::inspect_native(&fixture.executable(), |_| Ok(())).unwrap_err();
    assert!(
        error.contains("embedding library does not match the running interpreter"),
        "{error}"
    );
}

#[test]
fn native_python_inspection_cancellation_cleans_up_and_allows_retry() {
    use std::os::unix::net::UnixListener;

    let fixture = PythonFixture::new();
    let site_packages = fixture.create_venv();
    let socket = fixture.0.join("ready.sock");
    let listener = UnixListener::bind(&socket).expect("bind startup checkpoint");
    fixture.sitecustomize(&site_packages, "wait");
    std::fs::write(
        site_packages.join("inspection-socket"),
        socket.to_str().unwrap(),
    )
    .unwrap();
    let mut stop_handle = None;
    let error = crate::python::inspect_native(&fixture.executable(), |handle| {
        let (_connection, _) = listener.accept().expect("observe Python startup");
        handle.stop()?;
        stop_handle = Some(handle);
        Ok(())
    })
    .unwrap_err();
    assert!(error.contains("cancelled"), "{error}");
    assert!(stop_handle.unwrap().cleanup_confirmed());

    std::fs::remove_file(site_packages.join("sitecustomize.py")).unwrap();
    let selected = crate::python::inspect_native(&fixture.executable(), |_| Ok(()))
        .expect("retry selected Python inspection");
    assert_eq!(
        selected.embedding.python,
        fixture.executable().to_str().unwrap()
    );
}

#[test]
fn native_python_inspection_callback_rejection_confirms_cleanup() {
    let fixture = PythonFixture::new();
    fixture.create_venv();
    let mut stop_handle = None;
    let error = crate::python::inspect_native(&fixture.executable(), |handle| {
        stop_handle = Some(handle);
        Err("inspection admission rejected".to_string())
    })
    .unwrap_err();
    assert_eq!(error, "inspection admission rejected");
    assert!(stop_handle.unwrap().cleanup_confirmed());
    crate::python::inspect_native(&fixture.executable(), |_| Ok(()))
        .expect("retry after rejected inspection");
}

#[test]
fn native_command_wait_and_shutdown() {
    let (child, mut reader, writer) = spawn_probe("command");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    writer
        .send(&ServerMessage::Shutdown)
        .expect("send shutdown");
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_interrupt_wakes_and_acknowledges_once() {
    let (child, mut reader, writer) = spawn_probe("interrupt");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    let result = unsafe { libc::kill(child.id() as i32, libc::SIGINT) };
    assert_eq!(result, 0, "send interrupt");
    assert!(
        matches!(receive(&mut reader), WorkerMessage::ConsoleOutput { data } if data == "acknowledged")
    );
    writer
        .send(&ServerMessage::Shutdown)
        .expect("send shutdown");
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_wait_observes_stdin_shutdown() {
    let (mut child, mut reader, _writer) = spawn_probe("stdin");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    drop(child.stdin.take());
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_probe() {
    let Ok(scenario) = std::env::var("MCP_CONSOLE_NATIVE_PROBE") else {
        return;
    };
    let (reader, writer) = crate::sideband::connect_from_env().expect("connect sideband");
    interrupt::normalize_signal().expect("normalize worker signal");
    core::initialize(reader, writer.clone()).expect("initialize sideband");
    let integration =
        Integration::new(None, Default::default()).expect("initialize native integration");
    writer
        .send(&WorkerMessage::Ready)
        .expect("report readiness");

    if scenario.starts_with("python_activation_") {
        let name = if cfg!(target_os = "macos") {
            c"libR.dylib"
        } else {
            c"libR.so"
        };
        let r_library = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) };
        assert!(
            r_library.is_null(),
            "libR was loaded before native activation"
        );
        let executables: [String; 2] = serde_json::from_str(
            &std::env::var("MCP_CONSOLE_NATIVE_PYTHON_CONFIG").expect("selected environments"),
        )
        .expect("decode selected environments");
        let initial = crate::python::inspect_native(Path::new(&executables[0]), |_| Ok(()))
            .expect("inspect initial environment");
        let candidate = crate::python::inspect_native(Path::new(&executables[1]), |_| Ok(()))
            .expect("inspect candidate environment");
        crate::python::configure_native_worker_environment(Some(Path::new(
            &std::env::var("TMPDIR").expect("native storage"),
        )))
        .expect("configure native worker environment");
        let mut runtime = crate::python::Runtime::new(crate::local_runtime::WorkerSelection {
            r_settings: Default::default(),
            automatic_python_resolution: false,
            r: false,
            installation: None,
            python: Some(crate::local_runtime::Python {
                selected: Box::new(initial.clone()),
                explicit: None,
                managed: true,
                duckdb_extension_directory: None,
            }),
        })
        .expect("initialize native Python runtime");
        let mut sql = crate::sql::Bridge::new();
        runtime
            .evaluate("import sys, runpy, subprocess, multiprocessing\nidentity = object()\nidentity_id = id(identity)\noriginal_executable = sys.executable")
            .expect("create persistent Python objects");
        sql.evaluate("CREATE TABLE native_activation AS SELECT 41 AS answer")
            .expect("create persistent SQL catalog");
        writer
            .send(&WorkerMessage::Completed)
            .expect("report native activation setup");

        let selected_libpython = if scenario == "python_activation_incompatible" {
            "incompatible-libpython"
        } else {
            &candidate.embedding.libpython
        };
        let input = crate::python::ActivationInput {
            candidate_python: &candidate.embedding.python,
            candidate_libpython: selected_libpython,
            candidate_executable: &candidate.embedding.python,
            running_libpython: &initial.embedding.libpython,
        };
        if scenario == "python_activation_incompatible" {
            runtime
                .evaluate("def reject_activation_hook(_path):\n    raise AssertionError('incompatible candidate ran activation script')\nrunpy.run_path = reject_activation_hook")
                .expect("guard incompatible candidate against mutation");
        } else if scenario == "python_activation_exception" {
            runtime
                .evaluate("def fail_activation_hook(_path):\n    raise ValueError('native activation hook failed')\nrunpy.run_path = fail_activation_hook")
                .expect("install failing activation hook");
        }
        let result = crate::python::activate_managed_environment(input);
        match scenario.as_str() {
            "python_activation_success" => result.expect("activate candidate"),
            "python_activation_incompatible" => {
                let error = result.expect_err("reject incompatible candidate");
                assert!(matches!(
                    &error,
                    crate::python::ActivationFailure::Incompatible { .. }
                ));
                assert_eq!(
                    error.to_string(),
                    format!(
                        "New environment does not use the same Python binary\nnew libpython: incompatible-libpython\nold libpython: {}",
                        initial.embedding.libpython
                    )
                );
            }
            "python_activation_exception" => {
                assert!(matches!(
                    result,
                    Err(crate::python::ActivationFailure::PythonException)
                ));
                runtime
                    .evaluate("__import__('_mcp_console').raise_setup_error()")
                    .expect("report retained Python exception through evaluator");
            }
            _ => unreachable!(),
        }
        let candidate_path = serde_json::to_string(&candidate.embedding.python).unwrap();
        let candidate_prefix = serde_json::to_string(&candidate.prefix).unwrap();
        let source = match scenario.as_str() {
            "python_activation_success" => format!(
                "import yaml12\nprint('activation-success', _console.sql_connection().execute('SELECT answer FROM native_activation').fetchone()[0], id(identity) == identity_id, sys.prefix == {candidate_prefix}, sys.executable == {candidate_path}, subprocess.check_output([sys.executable, '-c', 'import sys; print(sys.executable)'], text=True).strip() == {candidate_path})"
            ),
            "python_activation_incompatible" => "import importlib.util\nassert importlib.util.find_spec('yaml12') is None\nprint('activation-rejected', _console.sql_connection().execute('SELECT answer FROM native_activation').fetchone()[0], id(identity) == identity_id, sys.executable == original_executable)".into(),
            "python_activation_exception" => "print('activation-continued', _console.sql_connection().execute('SELECT answer FROM native_activation').fetchone()[0], id(identity) == identity_id, sys.executable == original_executable)".into(),
            _ => unreachable!(),
        };
        runtime
            .evaluate(&source)
            .expect("evaluate after activation");
        assert!(
            unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) }.is_null(),
            "libR was loaded during native activation"
        );
        writer
            .send(&WorkerMessage::Completed)
            .expect("report native activation completion");
        // The probe owns its process. Skip extension-library exit destructors
        // after exercising the retained interpreter and SQL connection.
        unsafe { libc::_exit(0) }
    }

    if scenario == "python_setup" {
        let name = if cfg!(target_os = "macos") {
            c"libR.dylib"
        } else {
            c"libR.so"
        };
        let r_library = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) };
        assert!(r_library.is_null(), "libR was loaded before Python setup");
        let executable =
            std::env::var("MCP_CONSOLE_NATIVE_PYTHON_CONFIG").expect("selected Python executable");
        let configuration =
            crate::python::inspect_native(std::path::Path::new(&executable), |_| Ok(()))
                .expect("inspect selected Python executable");
        crate::python::initialize_selected(&configuration).expect("initialize known Python");
        assert!(
            crate::python::setup_runtime(
                std::path::Path::new(&configuration.embedding.libpython),
                false,
            )
            .expect("install native Python setup")
        );
        crate::python::finish_initialization().expect("release initial Python GIL");
        for (index, source) in [
            r#"native_value = 41
native_value + 1"#,
            r#"native_value += 2
native_value"#,
            r#"raise ValueError('native setup failure')"#,
            r#"native_value += 1
native_value"#,
            r#"import _mcp_console

def fail_reconfiguration(*arguments):
    raise AssertionError('configured runtime was installed twice')

_mcp_console.configure_import_resolution = fail_reconfiguration"#,
            r#"import mcp_console_missing_native_fixture_package"#,
        ]
        .into_iter()
        .enumerate()
        {
            crate::python::evaluate_embedded(source, &format!("<native-python:{index}>"))
                .expect("evaluate native Python cell");
            if index == 4 {
                assert!(
                    crate::python::setup_runtime(
                        std::path::Path::new(&configuration.embedding.libpython),
                        false,
                    )
                    .expect("reuse completed Python setup")
                );
            }
            writer
                .send(&WorkerMessage::Completed)
                .expect("report completed Python cell");
        }
        let r_library = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) };
        assert!(r_library.is_null(), "libR was loaded during Python setup");
        return;
    }

    if scenario == "interrupt" {
        let descriptor = match core::next_command().expect("next command") {
            CommandReadiness::Waiting(descriptor) => descriptor,
            CommandReadiness::Ready(_) => panic!("unexpected command"),
        };
        assert!(!interrupt::wait_for_activity(descriptor).expect("wait for interrupt"));
        let acknowledgments = std::thread::scope(|scope| {
            let first = scope.spawn(interrupt::acknowledge_python_interrupt);
            let second = scope.spawn(interrupt::acknowledge_python_interrupt);
            usize::from(first.join().expect("first acknowledgment"))
                + usize::from(second.join().expect("second acknowledgment"))
        });
        assert_eq!(acknowledgments, 1);
        assert!(!interrupt::acknowledge_python_interrupt());
        writer
            .send(&WorkerMessage::ConsoleOutput {
                data: "acknowledged".to_string(),
            })
            .expect("report acknowledgment");
    }

    assert!(matches!(
        Coordinator::wait_for_message(&integration).expect("wait for shutdown"),
        ServerMessage::Shutdown
    ));
    writer
        .send(&WorkerMessage::Completed)
        .expect("report shutdown");
}
