//! Shared command interpretation, without I/O or scheduling. Native relays keep
//! their thread, channel, acknowledgment, failure, and retirement policies.

use crate::relay_protocol::RelayCommand;
use crate::worker_protocol::ServerMessage;

pub(super) enum Operation {
    Worker(ServerMessage),
    Stdin { data: String },
    Interrupt { request_id: u64 },
    Shutdown { grace_millis: u64 },
}

impl From<RelayCommand> for Operation {
    fn from(command: RelayCommand) -> Self {
        let message = match command {
            RelayCommand::Evaluate { language, source } => {
                ServerMessage::Evaluate { language, source }
            }
            RelayCommand::PrepareR { library } => ServerMessage::PrepareR { library },
            RelayCommand::RResolved { library } => ServerMessage::RResolved { library },
            RelayCommand::RResolutionFailed { failure, message } => {
                ServerMessage::RResolutionFailed { failure, message }
            }
            RelayCommand::PreparePython { packages } => ServerMessage::PreparePython { packages },
            RelayCommand::PythonResolved { python, native } => {
                ServerMessage::PythonResolved { python, native }
            }
            RelayCommand::PythonResolutionFailed { message } => {
                ServerMessage::PythonResolutionFailed { message }
            }
            RelayCommand::PythonVersionResolved { version } => {
                ServerMessage::PythonVersionResolved { version }
            }
            RelayCommand::PythonVersionResolutionFailed { message } => {
                ServerMessage::PythonVersionResolutionFailed { message }
            }
            RelayCommand::Stdin { data } => return Self::Stdin { data },
            RelayCommand::Interrupt { request_id } => return Self::Interrupt { request_id },
            RelayCommand::Shutdown { grace_millis } => return Self::Shutdown { grace_millis },
        };
        Self::Worker(message)
    }
}
