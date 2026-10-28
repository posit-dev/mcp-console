//! One opt-in priority request may run beside the ordinary cell. Its managed
//! output has a separate owner; process-wide native streams remain untagged.

use std::cell::Cell;
use std::collections::VecDeque;
use std::io;
use std::sync::Mutex;

use crate::worker_protocol::{PriorityEvent, PriorityExecution, ServerMessage, WorkerMessage};

static R_REQUESTS: Mutex<VecDeque<(u64, String)>> = Mutex::new(VecDeque::new());
static INGRESS_FAILURE: Mutex<Option<String>> = Mutex::new(None);
thread_local! {
    static OWNER: Cell<Option<(u64, bool)>> = const { Cell::new(None) };
}

pub(crate) fn active() -> bool {
    OWNER.with(|owner| owner.get().is_some())
}

pub(crate) fn background() -> bool {
    OWNER.with(|owner| owner.get().is_some_and(|(_, background)| background))
}

pub(super) fn ingress_failure() -> Option<String> {
    INGRESS_FAILURE
        .lock()
        .expect("worker ingress failure")
        .take()
}

pub(super) fn publish(event: PriorityEvent) -> Option<Result<(), String>> {
    OWNER.with(|owner| owner.get().map(|(request_id, _)| send(request_id, event)))
}

fn send(request_id: u64, event: PriorityEvent) -> Result<(), String> {
    super::core::send_worker_message(&WorkerMessage::Priority { request_id, event })
}

pub(super) fn ingress(mut reader: crate::sideband::Reader) -> io::Result<crate::sideband::Reader> {
    let (coordinator, commands) = crate::sideband::local()?;
    std::thread::Builder::new()
        .name("worker-command-ingress".into())
        .spawn(move || {
            loop {
                let message = match reader.receive::<ServerMessage>() {
                    Ok(message) => message,
                    Err(error) => {
                        // Preserve wire order: an EOF/error after queued cells must
                        // not mark those earlier operations failed before they run.
                        *INGRESS_FAILURE.lock().expect("worker ingress failure") =
                            Some(format!("worker sideband read failed: {error}"));
                        break;
                    }
                };
                match message {
                    ServerMessage::EvaluatePriority {
                        request_id,
                        execution: PriorityExecution::PythonThread,
                        source,
                    } => {
                        if let Err(error) = std::thread::Builder::new()
                            .name("integrator-python".into())
                            .spawn(move || {
                                run(request_id, true, || {
                                    crate::python::evaluate_priority(&source)
                                });
                            })
                        {
                            let _ = send(
                                request_id,
                                PriorityEvent::Completed {
                                    error: Some(error.to_string()),
                                },
                            );
                        }
                    }
                    ServerMessage::EvaluatePriority {
                        request_id,
                        execution: PriorityExecution::RReentrant,
                        source,
                    } => {
                        R_REQUESTS
                            .lock()
                            .expect("priority R queue")
                            .push_back((request_id, source));
                        if commands.send(&ServerMessage::PriorityReady).is_err() {
                            break;
                        }
                    }
                    message => {
                        let shutdown = matches!(message, ServerMessage::Shutdown);
                        if commands.send(&message).is_err() || shutdown {
                            break;
                        }
                    }
                }
            }
        })?;
    Ok(coordinator)
}

pub(super) extern "C" fn run_r() {
    // R event dispatch can itself reenter this hook. Never nest priority work.
    if active() || super::core::bootstrapping() {
        return;
    }
    let request = R_REQUESTS.lock().expect("priority R queue").pop_front();
    if let Some((request_id, source)) = request {
        run(request_id, false, || {
            super::r_integration::evaluate_priority(&source)
        });
    }
}

fn run(request_id: u64, background: bool, evaluate: impl FnOnce() -> Result<(), String>) {
    OWNER.with(|owner| {
        assert!(owner.replace(Some((request_id, background))).is_none());
    });
    let result = evaluate();
    OWNER.with(|owner| owner.set(None));
    if let Err(error) = send(
        request_id,
        PriorityEvent::Completed {
            error: result.err(),
        },
    ) {
        super::core::record_worker_failure(error);
    }
}
