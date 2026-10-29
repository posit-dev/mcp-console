//! One opt-in priority request may run beside the ordinary cell. Its managed
//! output has a separate owner; process-wide native streams remain untagged.

use std::cell::Cell;
use std::collections::VecDeque;
use std::io;
use std::sync::Mutex;

use crate::worker_protocol::{PriorityEvent, PriorityExecution, ServerMessage, WorkerMessage};

struct RQueue {
    requests: VecDeque<(u64, String)>,
    wake_pending: bool,
}

static R_REQUESTS: Mutex<RQueue> = Mutex::new(RQueue {
    requests: VecDeque::new(),
    wake_pending: false,
});
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
                        let wake = {
                            let mut queue = R_REQUESTS.lock().expect("priority R queue");
                            queue.requests.push_back((request_id, source));
                            !std::mem::replace(&mut queue.wake_pending, true)
                        };
                        // Serial admission consumes the ordinary command before
                        // foreground execution begins, leaving pipe capacity for
                        // one small wakeup. R events can finish many requests
                        // without reading that frame; never enqueue another until
                        // the coordinator consumes it.
                        if wake && commands.send(&ServerMessage::PriorityReady).is_err() {
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

pub(super) fn consume_r_wakeup() {
    // Rearm under the enqueue guard before checking for work. A racing request
    // is either handled here or carries a new wakeup for the next idle wait.
    R_REQUESTS.lock().expect("priority R queue").wake_pending = false;
    run_r();
}

pub(super) extern "C" fn run_r() {
    // A fork child keeps R's event hooks, but inherits work owned by its parent
    // and may inherit the queue mutex locked by an ingress thread. Check process
    // ownership before touching that queue or any other worker state.
    if !crate::sideband::available_in_process() {
        return;
    }
    // R event dispatch can itself reenter this hook. Never nest priority work.
    if active() || super::core::bootstrapping() {
        return;
    }
    let request = R_REQUESTS
        .lock()
        .expect("priority R queue")
        .requests
        .pop_front();
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
