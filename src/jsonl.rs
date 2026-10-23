use serde::de::DeserializeOwned;

const RETAINED_CAPACITY: usize = 8 * 1024;

/// Incremental framing only; callers retain their read, EOF, and cancellation policy.
#[derive(Default)]
pub(crate) struct JsonlBuffer {
    bytes: Vec<u8>,
    consumed: usize,
    scanned: usize,
}

impl JsonlBuffer {
    pub(crate) fn append(&mut self, bytes: &[u8]) {
        self.bytes.extend_from_slice(bytes);
    }

    pub(crate) fn has_buffered_data(&self) -> bool {
        self.consumed < self.bytes.len()
    }

    #[cfg(any(unix, test))]
    pub(crate) fn next<T: DeserializeOwned>(&mut self) -> Result<Option<T>, serde_json::Error> {
        self.decode_next(false)
    }

    // Windows readers historically decode the delimiter too. Retaining it
    // preserves serde's line/column diagnostics for malformed or empty frames.
    #[cfg(windows)]
    pub(crate) fn next_line<T: DeserializeOwned>(
        &mut self,
    ) -> Result<Option<T>, serde_json::Error> {
        self.decode_next(true)
    }

    fn decode_next<T: DeserializeOwned>(
        &mut self,
        keep_delimiter: bool,
    ) -> Result<Option<T>, serde_json::Error> {
        let Some(newline) = self.bytes[self.scanned..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|offset| self.scanned + offset)
        else {
            self.scanned = self.bytes.len();
            // The batch is drained. Shift its partial tail once, and release
            // large completed-frame storage without shrinking a growing frame.
            if self.consumed != 0 {
                self.bytes.drain(..self.consumed);
                self.scanned -= self.consumed;
                self.consumed = 0;
                self.bytes.shrink_to(RETAINED_CAPACITY);
            }
            return Ok(None);
        };
        let frame = if keep_delimiter {
            &self.bytes[self.consumed..=newline]
        } else {
            let frame = &self.bytes[self.consumed..newline];
            frame.strip_suffix(b"\r").unwrap_or(frame)
        };
        // A complete malformed frame is consumed too; preserve its JSON error.
        self.consumed = newline + 1;
        self.scanned = self.consumed;
        serde_json::from_slice(frame).map(Some)
    }
}

#[cfg(test)]
mod tests {
    use super::JsonlBuffer;
    use serde_json::{Value, json};

    #[test]
    fn fragmented_frame_with_split_multibyte_text() {
        let mut buffer = JsonlBuffer::default();
        for byte in "\"a🦀é\"".as_bytes() {
            buffer.append(&[*byte]);
            assert!(buffer.next::<String>().unwrap().is_none());
        }
        buffer.append(b"\n");
        assert_eq!(buffer.next::<String>().unwrap(), Some("a🦀é".into()));
        assert!(!buffer.has_buffered_data());
    }

    #[test]
    fn crlf_and_multiple_frames_with_a_partial_tail() {
        let mut buffer = JsonlBuffer::default();
        buffer.append(b"1\r\n2\n{\"a\":");
        assert_eq!(buffer.next::<u64>().unwrap(), Some(1));
        assert_eq!(buffer.next::<u64>().unwrap(), Some(2));
        assert!(buffer.next::<Value>().unwrap().is_none());
        assert!(buffer.has_buffered_data());
        buffer.append(b"3}\n");
        assert_eq!(buffer.next::<Value>().unwrap(), Some(json!({"a": 3})));
        assert!(!buffer.has_buffered_data());
        assert!(buffer.next::<Value>().unwrap().is_none());
    }

    #[test]
    fn empty_and_malformed_frames_are_consumed_after_error() {
        for frame in [b"\n".as_slice(), b"\r\n", b"nope\n", b"[]x\n"] {
            let mut buffer = JsonlBuffer::default();
            buffer.append(frame);
            buffer.append(b"42\n");
            let error = buffer.next::<Value>().unwrap_err();
            let expected = serde_json::from_slice::<Value>(
                frame
                    .strip_suffix(b"\n")
                    .unwrap()
                    .strip_suffix(b"\r")
                    .unwrap_or(&frame[..frame.len() - 1]),
            )
            .unwrap_err();
            assert_eq!(error.to_string(), expected.to_string());
            assert!(buffer.has_buffered_data());
            assert_eq!(buffer.next::<u64>().unwrap(), Some(42));
            assert!(!buffer.has_buffered_data());
        }
    }

    #[test]
    fn large_frame_accumulated_in_many_chunks() {
        let text = "x".repeat(128 * 1024);
        let frame = serde_json::to_vec(&text).unwrap();
        let mut buffer = JsonlBuffer::default();
        for chunk in frame.chunks(127) {
            buffer.append(chunk);
            assert!(buffer.next::<String>().unwrap().is_none());
        }
        buffer.append(b"\ntrue\n");
        assert_eq!(buffer.next::<String>().unwrap(), Some(text));
        assert_eq!(buffer.next::<bool>().unwrap(), Some(true));
        assert!(buffer.next::<Value>().unwrap().is_none());
        buffer.append(b"42\n");
        assert_eq!(buffer.next::<u64>().unwrap(), Some(42));
        assert!(!buffer.has_buffered_data());
    }
}
