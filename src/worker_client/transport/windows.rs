use super::{OutputNotifier, RelayTransport, WriterAbort};
use crate::process_output::Diagnostics;
use crate::windows::{Event, Notify, Pipe};
use std::io;
use std::process::{Child, Command, Stdio};

pub(super) type Notifier = Notify;
pub(in crate::worker_client) type RelayInput = Pipe;

pub(in crate::worker_client) fn configure_stdio(command: &mut Command) {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
}

pub(in crate::worker_client) struct PreparedTransport {
    output_exit: Event,
    input: RelayInput,
    output: Pipe,
}

impl PreparedTransport {
    pub(in crate::worker_client) fn new(
        command: &mut Command,
    ) -> Result<(Self, OutputNotifier, WriterAbort), String> {
        let (output_exit, notify_output_exit) =
            crate::windows::notification().map_err(|error| error.to_string())?;
        let (writer_aborted, abort_writer) =
            crate::windows::notification().map_err(|error| error.to_string())?;
        let (input, output) = crate::windows::command_pipes(command, writer_aborted)
            .map_err(|error| error.to_string())?;
        Ok((
            Self {
                output_exit,
                input,
                output,
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
        _child: &mut Child,
        _diagnostics: Diagnostics,
        _on_diagnostic_error: impl FnOnce(io::Error) + Send + 'static,
    ) -> Result<RelayTransport, String> {
        Ok(RelayTransport {
            input: self.input,
            output: crate::process_output::RelayOutput::new(self.output, self.output_exit),
            diagnostic_reader: None,
        })
    }
}
