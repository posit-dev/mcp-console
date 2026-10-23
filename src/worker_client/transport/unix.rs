use super::{OutputNotifier, RelayTransport, WriterAbort};
use crate::process_output::Diagnostics;
use std::io::{self, PipeReader, PipeWriter};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::thread;

pub(super) type Notifier = PipeWriter;
pub(in crate::worker_client) type RelayInput = crate::process_io::Io<ChildStdin>;

pub(in crate::worker_client) fn configure_stdio(command: &mut Command) {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
}

pub(in crate::worker_client) struct PreparedTransport {
    output_exit: PipeReader,
    writer_aborted: PipeReader,
}

impl PreparedTransport {
    pub(in crate::worker_client) fn new(
        _command: &mut Command,
    ) -> Result<(Self, OutputNotifier, WriterAbort), String> {
        let (output_exit, notify_output_exit) = io::pipe()
            .map_err(|error| format!("failed to create launcher exit notification: {error}"))?;
        let (writer_aborted, abort_writer) = io::pipe()
            .map_err(|error| format!("failed to create relay writer abort pipe: {error}"))?;
        Ok((
            Self {
                output_exit,
                writer_aborted,
            },
            OutputNotifier {
                _native: notify_output_exit,
            },
            WriterAbort {
                _native: abort_writer,
            },
        ))
    }

    pub(in crate::worker_client) fn connect(
        self,
        child: &mut Child,
        diagnostics: Diagnostics,
        on_diagnostic_error: impl FnOnce(io::Error) + Send + 'static,
    ) -> Result<RelayTransport, String> {
        let input = child
            .stdin
            .take()
            .expect("piped worker relay stdin should be available");
        let output = child
            .stdout
            .take()
            .expect("piped worker relay stdout should be available");
        let input = RelayInput::new(input, Some(self.writer_aborted))?;
        let stderr = child.stderr.take().expect("piped launcher stderr");
        let exited = self
            .output_exit
            .try_clone()
            .map_err(|error| error.to_string())?;
        let diagnostic_reader = thread::spawn(move || {
            if let Err(error) = crate::process_output::forward(stderr, exited, diagnostics) {
                on_diagnostic_error(error);
            }
        });
        Ok(RelayTransport {
            input,
            output: crate::process_output::RelayOutput::new(output, self.output_exit),
            diagnostic_reader: Some(diagnostic_reader),
        })
    }
}
