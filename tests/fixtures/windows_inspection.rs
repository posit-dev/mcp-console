//! Selected-Python inspection held until the acceptance owner closes MCP input.
use std::io::{self, Read, Write};

mod windows_gate;

fn main() -> io::Result<()> {
    let mut ready = windows_gate::Gate::connect(std::env::var("TEST_INSPECTION_GATE").unwrap())?;
    writeln!(ready, "{}", std::process::id())?;
    // Reached inspection, then blocked on an explicit unreleased rendezvous.
    // The host pins our process handle before closing server input.
    let mut release = [0];
    ready.read_exact(&mut release)?;
    panic!("inspection gate was unexpectedly released")
}
