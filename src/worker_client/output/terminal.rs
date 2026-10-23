//! Bounded plain-text terminal projection for one producer/response interval.

use super::preview::{Preview, TEXT_BYTES, suffix_start};
use super::utf8_prefix_length;

// Only short CSI parameters can affect the frame. Longer syntax is consumed
// through its final character; control-string payloads are never buffered.
const CONTROL_BYTES: usize = 128;

#[derive(Default)]
enum Control {
    #[default]
    Text,
    Escape {
        intermediate: bool,
    },
    Csi {
        parameters: String,
        intermediate: bool,
        length: usize,
    },
    String {
        osc: bool,
        escape: bool,
    },
}

impl Control {
    fn csi() -> Self {
        Self::Csi {
            parameters: String::new(),
            intermediate: false,
            length: 2,
        }
    }

    fn string(osc: bool) -> Self {
        Self::String { osc, escape: false }
    }
}

#[derive(Default)]
pub(super) struct Stream {
    head: String,
    tail: String,
    tail_start: usize,
    omitted: u64,
    head_closed: bool,
    pending_carriage_return: bool,
    replace_on_write: bool,
    control: Control,
}

impl Stream {
    pub(super) fn ingest(&mut self, text: &str, output: &mut Preview) {
        let mut plain = 0;
        for (index, character) in text.char_indices() {
            if matches!(self.control, Control::Text)
                && !matches!(
                    character,
                    '\r' | '\n' | '\x08' | '\x1b' | '\u{80}'..='\u{9f}'
                )
            {
                continue;
            }
            self.write(&text[plain..index]);
            plain = if self.consume(character, output) {
                index + character.len_utf8()
            } else {
                // Malformed syntax ends the control; keep the offending scalar
                // as text, including Unicode and UTF-8 replacement characters.
                index
            };
        }
        self.write(&text[plain..]);
    }

    fn write(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        if self.pending_carriage_return || self.replace_on_write {
            self.clear();
        }
        self.append(text);
    }

    fn consume(&mut self, character: char, output: &mut Preview) -> bool {
        if let Control::String { osc, escape } = &mut self.control {
            if character == '\u{9c}'
                || (*escape && character == '\\')
                || (*osc && character == '\x07')
            {
                self.control = Control::Text;
            } else {
                *escape = character == '\x1b';
            }
            return true;
        }
        // Introducers restart malformed non-string controls. Decoded C1 aliases
        // are a projection convention; invalid raw bytes still use UTF-8 loss.
        match character {
            '\x1b' => {
                self.control = Control::Escape {
                    intermediate: false,
                };
                return true;
            }
            '\u{9b}' => {
                self.control = Control::csi();
                return true;
            }
            '\u{9d}' => {
                self.control = Control::string(true);
                return true;
            }
            '\u{90}' | '\u{98}' | '\u{9e}' | '\u{9f}' => {
                self.control = Control::string(false);
                return true;
            }
            '\u{80}'..='\u{9f}' => {
                self.control = Control::Text;
                return true;
            }
            '\r' | '\n' | '\x08' => {
                self.control = Control::Text;
                match character {
                    '\r' => self.pending_carriage_return = true,
                    '\n' => {
                        let delimiter = if self.pending_carriage_return {
                            "\r\n"
                        } else {
                            "\n"
                        };
                        self.finish_frame(output);
                        output.text(delimiter);
                    }
                    _ => {
                        if self.pending_carriage_return {
                            self.pending_carriage_return = false;
                            self.replace_on_write = true;
                        }
                        // Backspace edits the retained suffix. It cannot reconstruct
                        // an already omitted middle after erasing that entire suffix;
                        // the gap remains until a carriage return replaces the frame.
                        if self.tail.len() > self.tail_start {
                            self.tail.pop();
                        } else if self.omitted == 0 {
                            self.head.pop();
                        }
                    }
                }
                return true;
            }
            _ => {}
        }
        match std::mem::take(&mut self.control) {
            Control::Text => false,
            Control::Escape { intermediate } => {
                self.control = match character {
                    '[' if !intermediate => Control::csi(),
                    ']' if !intermediate => Control::string(true),
                    'P' | 'X' | '^' | '_' if !intermediate => Control::string(false),
                    '\x20'..='\x2f' => Control::Escape { intermediate: true },
                    '\x30'..='\x7e' => Control::Text,
                    _ => return false,
                };
                true
            }
            Control::Csi {
                mut parameters,
                mut intermediate,
                mut length,
            } => {
                length = (length + 1).min(CONTROL_BYTES + 1);
                match character {
                    '\x30'..='\x3f' if !intermediate => {
                        if length <= CONTROL_BYTES {
                            parameters.push(character);
                        }
                    }
                    '\x20'..='\x2f' => intermediate = true,
                    '\x40'..='\x7e' => {
                        // SGR and unsupported screen operations are suppressed.
                        // EL only edits this frame, never earlier finished lines.
                        if character == 'K'
                            && !intermediate
                            && length <= CONTROL_BYTES
                            && (parameters == "2"
                                || (matches!(parameters.as_str(), "" | "0")
                                    && (self.pending_carriage_return || self.replace_on_write)))
                        {
                            self.erase();
                        }
                        return true;
                    }
                    _ => return false,
                }
                self.control = Control::Csi {
                    parameters,
                    intermediate,
                    length,
                };
                true
            }
            Control::String { .. } => unreachable!("strings consumed above"),
        }
    }

    fn append(&mut self, text: &str) {
        let head = if self.head_closed {
            0
        } else {
            utf8_prefix_length(text, TEXT_BYTES.saturating_sub(self.head.len()))
        };
        self.head.push_str(&text[..head]);
        let text = &text[head..];
        self.head_closed |= !text.is_empty();
        if text.is_empty() {
            return;
        }
        if text.len() >= TEXT_BYTES {
            let start = suffix_start(text, TEXT_BYTES);
            self.omitted += (self.tail.len() - self.tail_start + start) as u64;
            self.tail.clear();
            self.tail_start = 0;
            self.tail.push_str(&text[start..]);
        } else {
            // Advance through the retained suffix between bounded compactions.
            // Reserve only this fixed ceiling, including the discarded prefix.
            if self.tail.len() + text.len() > 2 * TEXT_BYTES {
                self.tail.drain(..self.tail_start);
                self.tail_start = 0;
            }
            if self.tail.capacity() < self.tail.len() + text.len() {
                self.tail.reserve_exact(2 * TEXT_BYTES - self.tail.len());
            }
            self.tail.push_str(text);
            let start = suffix_start(&self.tail[self.tail_start..], TEXT_BYTES);
            self.omitted += start as u64;
            self.tail_start += start;
        }
    }

    fn erase(&mut self) {
        self.head.clear();
        self.tail.clear();
        self.tail_start = 0;
        self.omitted = 0;
        self.head_closed = false;
    }

    fn clear(&mut self) {
        self.erase();
        self.pending_carriage_return = false;
        self.replace_on_write = false;
    }

    fn finish_frame(&mut self, output: &mut Preview) {
        output.text(&self.head);
        output.gap(self.omitted);
        output.text(&self.tail[self.tail_start..]);
        self.clear();
    }

    pub(super) fn finish(&mut self, output: &mut Preview) {
        self.finish_frame(output);
        // Controls never join across producers, cuts, images, or lifecycle
        // boundaries. An unterminated string suppresses only this interval.
        self.control = Control::Text;
    }
}
