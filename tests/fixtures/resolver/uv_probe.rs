//! Launch the shared preparation probe without requiring shebang support.

use std::io;
use std::process::{Command, exit};

fn main() -> io::Result<()> {
    let executable = std::env::current_exe()?;
    let python = std::fs::read_to_string(executable.with_extension("python"))?;
    let status = Command::new(python)
        .arg("-S")
        .arg(executable.with_extension("py"))
        .args(std::env::args_os().skip(1))
        .status()?;
    exit(status.code().unwrap_or(1));
}
