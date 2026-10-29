//! A bounded observation history, independent of MCP projection and delivery.
//!
//! Sequences describe ingestion here, not chronology across independent pipes.
//! Immediate requests use a private tape; projection-omitted ordinary requests
//! suppress their entire observed interval in the shared history.

use std::collections::VecDeque;

use serde_json::{Value, json};

use super::utf8_prefix_length;

const EVENTS: usize = 512;
const EVALUATIONS: usize = 32;
const TEXT_BYTES: usize = 64 * 1024;
const IMAGE_BYTES: usize = 8 * 1024 * 1024;
const SOURCE_BYTES: usize = 8 * 1024;

#[derive(Default)]
pub(super) struct History {
    enabled: bool,
    events: VecDeque<(Value, usize, usize)>,
    evaluations: VecDeque<Value>,
    active: Option<u64>,
    hidden: bool,
    worker_id: u64,
    next_sequence: u64,
    text_bytes: usize,
    image_bytes: usize,
    omitted_events: u64,
    omitted_bytes: u64,
}

impl History {
    pub(super) fn enable(&mut self) {
        self.enabled = true;
    }
    pub(super) fn enabled(&self) -> bool {
        self.enabled
    }

    pub(super) fn worker_started(&mut self) {
        // Admission can precede the first worker's startup. Keep that accepted
        // evaluation associated with the new worker, but discard retired history.
        let admitted = self.active.and_then(|id| {
            self.evaluations
                .iter()
                .find(|item| item["id"] == id)
                .cloned()
        });
        self.worker_id += 1;
        self.events.clear();
        self.evaluations.clear();
        self.text_bytes = 0;
        self.image_bytes = 0;
        self.omitted_events = 0;
        self.omitted_bytes = 0;
        if let Some(admitted) = admitted {
            self.evaluations.push_back(admitted);
            self.push(json!({"type": "evaluation_admitted"}), 0, 0);
        }
    }

    pub(super) fn worker_id(&self) -> u64 {
        self.worker_id
    }
    pub(super) fn set_worker_id(&mut self, id: u64) {
        self.worker_id = id;
    }

    pub(super) fn begin(&mut self, mut evaluation: Value, hidden: bool) {
        if !self.enabled {
            return;
        }
        self.finish();
        self.active = evaluation["id"].as_u64();
        self.hidden = hidden;
        if hidden {
            return;
        }
        let source = evaluation["source"].as_str().expect("evaluation source");
        let length = utf8_prefix_length(source, SOURCE_BYTES);
        let truncated = length < source.len();
        evaluation["source"] = json!(&source[..length]);
        evaluation["source_truncated"] = json!(truncated);
        evaluation["state"] = json!("running");
        evaluation["started"] = json!(evaluation["execution"] == "immediate");
        if let Some(request_id) = evaluation["request_id"].as_str() {
            let length = utf8_prefix_length(request_id, 512);
            let truncated = length < request_id.len();
            evaluation["request_id"] = json!(&request_id[..length]);
            evaluation["request_id_truncated"] = json!(truncated);
        }
        evaluation["execution_error"] = Value::Null;
        if self.evaluations.len() == EVALUATIONS {
            let removed = self.evaluations.pop_front().expect("oldest evaluation")["id"].clone();
            // Events never outlive the metadata needed to attribute them.
            while self
                .events
                .iter()
                .any(|(event, _, _)| event["evaluation_id"] == removed)
            {
                self.evict();
            }
        }
        self.evaluations.push_back(evaluation);
        self.push(json!({"type": "evaluation_admitted"}), 0, 0);
    }

    pub(super) fn entered(&mut self) {
        if !self.hidden
            && let Some(id) = self.active
            && let Some(evaluation) = self.evaluations.iter_mut().find(|item| item["id"] == id)
        {
            evaluation["started"] = json!(true);
            self.push(json!({"type": "evaluation_started"}), 0, 0);
        }
    }

    pub(super) fn finish(&mut self) {
        if let Some(id) = self.active {
            if !self.hidden {
                if let Some(evaluation) = self.evaluations.iter_mut().find(|item| item["id"] == id)
                {
                    evaluation["state"] = json!("complete");
                }
                self.push(json!({"type": "evaluation_completed"}), 0, 0);
            }
            self.active = None;
        }
        self.hidden = false;
    }

    pub(super) fn text(&mut self, kind: &str, text: &str) {
        if text.is_empty() || self.hidden || !self.enabled {
            return;
        }
        let length = utf8_prefix_length(text, TEXT_BYTES);
        let omitted = text.len() - length;
        self.omitted_bytes += omitted as u64;
        self.push(
            json!({"type": kind, "text": &text[..length], "omitted_bytes": omitted}),
            length,
            0,
        );
    }

    pub(super) fn image(&mut self, data: &str, mime_type: &str) {
        if self.hidden || !self.enabled {
            return;
        }
        if data.len() + mime_type.len() > IMAGE_BYTES {
            self.omitted_bytes += data.len() as u64;
            self.omitted_events += 1;
            self.push(
                json!({"type": "omission", "kind": "image", "bytes": data.len()}),
                0,
                0,
            );
        } else {
            self.push(
                json!({"type": "image", "data": data, "mimeType": mime_type}),
                0,
                data.len() + mime_type.len(),
            );
        }
    }

    pub(super) fn execution_error(&mut self, error: &crate::worker_protocol::ExecutionError) {
        if self.hidden || !self.enabled {
            return;
        }
        if let Some(id) = self.active
            && let Some(evaluation) = self.evaluations.iter_mut().find(|item| item["id"] == id)
            && evaluation["execution_error"].is_null()
        {
            evaluation["execution_error"] =
                json!({"ename": error.ename, "evalue": error.evalue, "truncated": error.truncated});
        }
        let event = json!({"type": "execution_error", "error": error});
        let length = serde_json::to_vec(&event).expect("execution error").len();
        self.push(event, length, 0);
    }

    fn push(&mut self, mut event: Value, text_bytes: usize, image_bytes: usize) {
        if self.hidden || !self.enabled {
            return;
        }
        event["sequence"] = json!(self.next_sequence);
        event["evaluation_id"] = json!(self.active);
        self.next_sequence += 1;
        while self.events.len() >= EVENTS
            || self.text_bytes + text_bytes > TEXT_BYTES
            || self.image_bytes + image_bytes > IMAGE_BYTES
        {
            self.evict();
        }
        self.text_bytes += text_bytes;
        self.image_bytes += image_bytes;
        self.events.push_back((event, text_bytes, image_bytes));
    }

    fn evict(&mut self) {
        let (_, text_bytes, image_bytes) = self.events.pop_front().expect("bounded event history");
        self.text_bytes -= text_bytes;
        self.image_bytes -= image_bytes;
        self.omitted_events += 1;
        self.omitted_bytes += (text_bytes + image_bytes) as u64;
    }

    pub(super) fn snapshot(&self, evaluation_id: Option<u64>) -> Value {
        json!({
            "worker_id": self.worker_id,
            "first_sequence": self.events.front().map_or(self.next_sequence, |(event, _, _)| event["sequence"].as_u64().expect("event sequence")),
            "next_sequence": self.next_sequence,
            "omitted_events": self.omitted_events,
            "omitted_bytes": self.omitted_bytes,
            "evaluations": self.evaluations.iter().filter(|item| evaluation_id.is_none_or(|id| item["id"] == id)).collect::<Vec<_>>(),
            "events": self.events.iter().filter(|(event, _, _)| evaluation_id.is_none_or(|id| event["evaluation_id"] == id)).map(|(event, _, _)| event).collect::<Vec<_>>(),
        })
    }
}
