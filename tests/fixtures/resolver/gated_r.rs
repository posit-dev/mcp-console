//! Hold R discovery with a descendant whose pipe/socket records its lifetime.
use std::io::{self, Read, Write};
#[cfg(windows)]
use std::net::TcpStream;
use std::process::{Command, exit};

fn main() -> io::Result<()> {
    let executable = std::env::current_exe()?;
    let root = executable.parent().unwrap();
    if std::env::args().nth(1).as_deref() == Some("--discovery-child") {
        #[cfg(windows)]
        let mut lifetime = TcpStream::connect(std::fs::read_to_string(root.join("address"))?)?;
        #[cfg(unix)]
        let mut lifetime = std::fs::OpenOptions::new()
            .write(true)
            .open(root.join("lifetime"))?;
        lifetime.write_all(&std::process::id().to_be_bytes())?;
        #[cfg(windows)]
        let mut gate = &lifetime;
        #[cfg(unix)]
        let mut gate = std::fs::File::open(root.join("release"))?;
        let mut release = [0];
        gate.read_exact(&mut release)?;
        assert_eq!(release, [b'1']);
        return Ok(());
    }
    assert_eq!(std::env::args().nth(1).as_deref(), Some("RHOME"));
    // Only the descendant owns the connection. Killing the direct probe alone
    // must not satisfy the host's cleanup assertion.
    // Gate the first attempt; explicit restart can repair the R home afterward.
    let started = root.join("started");
    if !started.exists() {
        std::fs::write(started, b"1")?;
        assert!(
            Command::new(&executable)
                .arg("--discovery-child")
                .status()?
                .success()
        );
    }
    let home = root.join("r-home");
    if home.exists() {
        println!("{}", std::fs::read_to_string(home)?);
    } else {
        eprintln!("{}", std::fs::read_to_string(root.join("failure"))?);
        exit(17);
    }
    Ok(())
}
