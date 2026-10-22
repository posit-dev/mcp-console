//! Hold initial worker preparation until the client records its first request.
use std::fs::File;
use std::io::ErrorKind;
use std::path::Path;
use std::process::{Command, exit};
use std::time::Duration;

fn main() {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if arguments.first().is_some_and(|argument| argument == "run") {
        let claim = std::env::var_os("MCP_CONSOLE_TEST_IR_CLAIM").unwrap();
        match File::create_new(claim) {
            Ok(_) => {
                std::fs::write(
                    std::env::var_os("MCP_CONSOLE_TEST_IR_STARTED").unwrap(),
                    b"1",
                )
                .unwrap();
                let release = std::env::var_os("MCP_CONSOLE_TEST_IR_RELEASE").unwrap();
                // A file gate also works inside the native Windows resolver.
                // The parent watches readiness and releases this in cleanup.
                while !Path::new(&release).exists() {
                    std::thread::sleep(Duration::from_millis(10));
                }
            }
            Err(error) if error.kind() == ErrorKind::AlreadyExists => {}
            Err(error) => panic!("cannot claim preparation checkpoint: {}", error),
        }
    }
    let status = Command::new(std::env::var_os("MCP_CONSOLE_TEST_REAL_IR").unwrap())
        .args(arguments)
        .status()
        .expect("run real ir");
    exit(status.code().expect("real ir terminated by signal"));
}
