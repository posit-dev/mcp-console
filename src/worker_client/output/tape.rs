//! Streaming projection and response cuts under the existing output owner.

use super::preview::Source;
use super::*;
use std::collections::VecDeque;

#[derive(Default)]
pub(super) struct OutputTapeState {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    stream: Option<Stream>,
    next_diagnostic: u64,
    terminal: terminal::Stream,
    current: ResponseBuilder,
    sealed: VecDeque<(u64, Response)>,
    next_cut: u64,
    cell_active: bool,
    cell_output: Option<crate::transcript::CellOutput>,
    session_output: Option<crate::transcript::CellOutput>,
    session_recording: Option<crate::transcript::Transcript>,
    raw_bytes: u64,
    /// Worker publications before decoding, projection, or image admission.
    /// Native diagnostics and server notices do not advance this revision.
    worker_revision: u64,
    recovered: Option<Response>,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Stream {
    Output,
    Diagnostic,
    Stdout,
    Stderr,
    NativeDiagnostic(u64),
}

impl OutputTape {
    pub(in crate::worker_client) fn new() -> Self {
        Self(Arc::new(Mutex::new(OutputTapeState::default())))
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, OutputTapeState> {
        self.0.lock().unwrap_or_else(|error| error.into_inner())
    }

    pub(in crate::worker_client) fn record_session_output(
        &self,
        transcript: crate::transcript::Transcript,
    ) {
        let mut state = self.lock();
        if state.session_output.is_none() && state.session_recording.is_none() {
            state.session_recording = Some(transcript);
        }
    }

    pub(in crate::worker_client) fn finish_session_output(&self) {
        let mut state = self.lock();
        state.seal(true);
        if let Some(output) = state.session_output.take() {
            let record = output.record();
            state.recording_notice(output.finish());
            record.publish();
        }
        // EOF need not poll completed output. Retained response receipts must
        // reach the journal before its owner emits recording_ended.
        for (_, response) in &state.sealed {
            response.preview.publish_output_records();
        }
        if let Some(response) = &state.recovered {
            response.preview.publish_output_records();
        }
    }

    pub(in crate::worker_client) fn direct_stdout(&self) -> DirectOutput {
        DirectOutput {
            output: self.clone(),
            stream: DirectOutputStream::Stdout,
        }
    }
    pub(in crate::worker_client) fn direct_stderr(&self) -> DirectOutput {
        DirectOutput {
            output: self.clone(),
            stream: DirectOutputStream::Stderr,
        }
    }

    pub(in crate::worker_client) fn diagnostics(&self) -> crate::process_output::Diagnostics {
        let output = self.clone();
        Arc::new(move || {
            let output = output.clone();
            // Native readers and worker stderr are different producers. Each
            // reader keeps its identity across writes, including split controls.
            let stream = {
                let mut state = output.lock();
                state.next_diagnostic += 1;
                Stream::NativeDiagnostic(state.next_diagnostic)
            };
            let mut pending = Vec::new();
            Box::new(move |bytes| {
                let mut state = output.lock();
                if bytes.is_empty() {
                    state.text(stream, &String::from_utf8_lossy(&pending));
                    pending.clear();
                    // Response cuts and shutdown finish the shared terminal.
                    // This EOF cannot finish another producer's progress line.
                    return;
                }
                let notice = state.spool(bytes);
                let bytes = if pending.is_empty() {
                    bytes
                } else {
                    pending.extend_from_slice(bytes);
                    &pending
                };
                let complete = complete_utf8_prefix(bytes);
                let remainder = bytes[complete..].to_vec();
                state.text(stream, &String::from_utf8_lossy(&bytes[..complete]));
                pending = remainder;
                state.recording_notice(notice);
            })
        })
    }

    pub(in crate::worker_client) fn push_console_text(
        &self,
        channel: crate::worker_protocol::ConsoleChannel,
        text: impl Into<String>,
    ) {
        let text = text.into();
        if text.is_empty() {
            return;
        }
        let mut state = self.lock();
        state.worker_revision += 1;
        let notice = state.spool(text.as_bytes());
        state.flush_decoders();
        state.text(
            match channel {
                crate::worker_protocol::ConsoleChannel::Output => Stream::Output,
                crate::worker_protocol::ConsoleChannel::Diagnostic => Stream::Diagnostic,
            },
            &text,
        );
        state.recording_notice(notice);
    }

    pub(in crate::worker_client) fn push_image_with_artifact<F>(
        &self,
        data: String,
        mime_type: String,
        make_artifact: F,
    ) -> Result<(), String>
    where
        F: FnOnce(&str, &str) -> Result<Option<crate::transcript::Artifact>, String>,
    {
        let mut state = self.lock();
        state.worker_revision += 1;
        state.flush_decoders();
        state.flush_terminal();
        // Before the first send, startup images share this preview. Apply its
        // budget before retaining artifacts, so omitted startup images never
        // acquire receipts that could advertise unpublished paths.
        if state
            .current
            .response
            .preview
            .admits_image(&data, &mime_type)
        {
            let artifact = make_artifact(&data, &mime_type)?;
            state.current.image(data, mime_type, artifact);
        } else {
            state.current.response.preview.omit_image(data.len());
        }
        Ok(())
    }

    pub(in crate::worker_client) fn push_notice_line(&self, message: impl Into<String>) {
        let mut state = self.lock();
        state.flush_decoders();
        state.flush_terminal();
        state.current.notice_line(message);
    }

    pub(in crate::worker_client) fn push_bounded_notice_line(&self, message: impl Into<String>) {
        let mut state = self.lock();
        state.flush_decoders();
        state.flush_terminal();
        // Informational resolver messages share the ordinary text allowance.
        let prefix = if state.current.needs_line_break() && !state.current.response.is_empty() {
            "\n"
        } else {
            ""
        };
        state
            .current
            .response
            .preview
            .information(format!("{prefix}{}\n", render_notice(message)));
    }

    pub(in crate::worker_client) fn push_failure(&self, failure: SendFailure) {
        let mut state = self.lock();
        state.flush_decoders();
        state.flush_terminal();
        state.current.send_failure(failure);
    }

    pub(in crate::worker_client) fn cut(&self) -> OutputCut {
        self.lock().seal(false)
    }

    pub(in crate::worker_client) fn take(&self) -> Response {
        let mut state = self.lock();
        let cut = state.seal(false);
        state.drain(cut)
    }

    pub(in crate::worker_client) fn take_prelude(&self) -> Response {
        self.take_prelude_before(|| {})
    }

    pub(in crate::worker_client) fn take_admission_prelude(&self) -> (Response, u64) {
        let mut state = self.lock();
        let cut = state.seal(true);
        (state.drain(cut), cut.worker_revision)
    }

    pub(in crate::worker_client) fn take_prelude_before(
        &self,
        boundary: impl FnOnce(),
    ) -> Response {
        let mut state = self.lock();
        let cut = state.seal(true);
        let response = state.drain(cut);
        boundary();
        response
    }

    pub(in crate::worker_client) fn begin_cell_output_before(
        &self,
        cell_output: Option<crate::transcript::CellOutput>,
        capture_prelude: bool,
        boundary: impl FnOnce(),
    ) -> Response {
        let mut state = self.lock();
        assert!(
            !state.cell_active,
            "only one cell output file can be active"
        );
        // Always seal the preceding source, even if this caller does not own it.
        let cut = state.seal(true);
        let response = if capture_prelude {
            state.drain(cut)
        } else {
            Response::default()
        };
        state.cell_active = true;
        state.cell_output = cell_output;
        boundary();
        response
    }

    pub(in crate::worker_client) fn finish_cell_output(&self) -> OutputCut {
        let mut state = self.lock();
        let cut = state.seal(true);
        let notice = state.cell_output.take().and_then(|output| output.finish());
        state.cell_active = false;
        if notice.is_some() {
            state.recording_notice(notice);
            return state.seal(false);
        }
        cut
    }

    pub(in crate::worker_client) fn drain_through(&self, cut: OutputCut) -> Response {
        self.lock().drain(cut)
    }

    pub(in crate::worker_client) fn recover(&self, response: Response) {
        let mut state = self.lock();
        assert!(
            state.recovered.is_none(),
            "only one response can be recovered at a time"
        );
        state.recovered = Some(response);
    }
}

impl DirectOutput {
    pub(in crate::worker_client) fn push(&self, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let mut state = self.output.lock();
        state.worker_revision += 1;
        let notice = state.spool(bytes);
        let stream = match self.stream {
            DirectOutputStream::Stdout => Stream::Stdout,
            DirectOutputStream::Stderr => Stream::Stderr,
        };
        let other = if stream == Stream::Stdout {
            Stream::Stderr
        } else {
            Stream::Stdout
        };
        state.flush_decoder(other);
        // Only an incomplete UTF-8 scalar survives ingestion. Decode the incoming
        // publication before retaining any text, including after preview overflow.
        let mut pending = std::mem::take(state.decoder(stream));
        let bytes = if pending.is_empty() {
            bytes
        } else {
            pending.extend_from_slice(bytes);
            &pending
        };
        let complete = complete_utf8_prefix(bytes);
        state.decoder(stream).extend_from_slice(&bytes[complete..]);
        state.text(stream, &String::from_utf8_lossy(&bytes[..complete]));
        state.recording_notice(notice);
    }

    pub(in crate::worker_client) fn close(&self) {
        let mut state = self.output.lock();
        let stream = match self.stream {
            DirectOutputStream::Stdout => Stream::Stdout,
            DirectOutputStream::Stderr => Stream::Stderr,
        };
        state.flush_decoder(stream);
        if state.stream == Some(stream) {
            state.flush_terminal();
        }
    }
}

impl OutputTapeState {
    fn spool(&mut self, bytes: &[u8]) -> Option<String> {
        self.raw_bytes = self.raw_bytes.saturating_add(bytes.len() as u64);
        if !self.cell_active
            && let Some(transcript) = self.session_recording.take()
        {
            match transcript.create_session_output() {
                Ok(output) => self.session_output = output,
                Err(error) => return Some(error),
            }
        }
        self.current_output()
            .and_then(|output| output.append(bytes).1)
    }

    fn current_output(&mut self) -> Option<&mut crate::transcript::CellOutput> {
        // A failed cell log remains owned by that cell; it cannot borrow the
        // session file or trigger a second recording failure while evaluating.
        if self.cell_active {
            self.cell_output.as_mut()
        } else {
            self.session_output.as_mut()
        }
    }

    fn text(&mut self, stream: Stream, text: &str) {
        if text.is_empty() {
            return;
        }
        if self.stream != Some(stream) {
            self.flush_terminal();
            self.stream = Some(stream);
        }
        self.terminal
            .ingest(text, &mut self.current.response.preview);
    }

    fn flush_terminal(&mut self) {
        self.terminal.finish(&mut self.current.response.preview);
        self.stream = None;
    }

    fn decoder(&mut self, stream: Stream) -> &mut Vec<u8> {
        match stream {
            Stream::Stdout => &mut self.stdout,
            Stream::Stderr => &mut self.stderr,
            _ => unreachable!(),
        }
    }

    fn flush_decoder(&mut self, stream: Stream) {
        let bytes = std::mem::take(self.decoder(stream));
        self.text(stream, &String::from_utf8_lossy(&bytes));
    }

    fn flush_decoders(&mut self) {
        // Each direct push flushes the other decoder first, so at most one
        // stream has pending bytes, including across polls and stream closes.
        for stream in [Stream::Stdout, Stream::Stderr] {
            self.flush_decoder(stream);
        }
    }

    fn recording_notice(&mut self, notice: Option<String>) {
        if let Some(notice) = notice {
            // A recording failure does not retire a direct output stream.
            self.flush_terminal();
            self.current.notice_line(notice);
        }
    }

    fn seal(&mut self, flush_decoders: bool) -> OutputCut {
        if flush_decoders {
            self.flush_decoders();
        }
        self.flush_terminal();
        let notice = self.current_output().and_then(|output| output.flush());
        self.recording_notice(notice);
        let raw_bytes = self.raw_bytes;
        let source = match self.current_output() {
            Some(output) => Source {
                file: Some(output.record()),
                raw_bytes,
                retained_bytes: output.retained_bytes(),
                discarded_bytes: output.discarded_bytes(),
            },
            None => Source {
                raw_bytes,
                ..Source::default()
            },
        };
        self.current.response.preview.source(source);
        let response = std::mem::take(&mut self.current).finish();
        let cut = self.next_cut;
        self.next_cut += 1;
        // Only intervals with rendered text need a receipt for later accounting.
        self.sealed.push_back((cut, response));
        self.raw_bytes = 0;
        OutputCut {
            sequence: cut,
            worker_revision: self.worker_revision,
        }
    }

    fn drain(&mut self, cut: OutputCut) -> Response {
        let mut output = self.recovered.take().unwrap_or_default();
        while self
            .sealed
            .front()
            .is_some_and(|(sequence, _)| *sequence <= cut.sequence)
        {
            output.extend(self.sealed.pop_front().expect("checked sealed interval").1);
        }
        output
    }
}

fn complete_utf8_prefix(bytes: &[u8]) -> usize {
    let mut offset = 0;
    loop {
        match std::str::from_utf8(&bytes[offset..]) {
            Ok(_) => return bytes.len(),
            Err(error) => match error.error_len() {
                Some(length) => offset += error.valid_up_to() + length,
                None => return offset + error.valid_up_to(),
            },
        }
    }
}
