//! Native process fixtures for framing; no production Reader is embedded here.
use super::*;
use std::process::Command;

fn flush(handle: *mut c_void) -> io::Result<()> {
    // This is a consumption barrier, not merely a completed write.
    if unsafe { FlushFileBuffers(handle) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn command(read: *mut c_void) -> io::Result<Vec<u8>> {
    let mut frame = Vec::new();
    loop {
        let mut byte = [0];
        if transfer(read, &mut byte, false)? == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        frame.push(byte[0]);
        if byte[0] == b'\n' {
            return Ok(frame);
        }
    }
}

fn holder(control: &mut Gate, refill: bool) -> io::Result<()> {
    let mut ready = Gate::listen()?;
    let mut child = Command::new(std::env::current_exe()?)
        .env("TEST_WORKER_SCENARIO", "framing_holder")
        .env("TEST_HOLDER_READY", &ready.name)
        .env("TEST_HOLDER_REFILL", if refill { "1" } else { "0" })
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()?;
    let handshake = (|| {
        // The test acquires this child's native handle before releasing our exit.
        ready.accept(&child)?;
        let mut byte = [0];
        ready.read_exact(&mut byte)?;
        writeln!(control, "{}", child.id())?;
        control.read_exact(&mut byte)?;
        assert_eq!(byte, [b'1']);
        Ok(())
    })();
    if handshake.is_err() {
        // Ownership has not reached the test's pinned handle. Retire the
        // fixture child if either rendezvous fails; preserve the gate error.
        let _ = child.kill();
        let _ = child.wait();
    }
    handshake
}

pub(super) fn hold(write: *mut c_void) -> io::Result<()> {
    let mut ready = Gate::connect(std::env::var("TEST_HOLDER_READY").unwrap())?;
    if std::env::var("TEST_HOLDER_REFILL").as_deref() == Ok("1") {
        send(
            write,
            "{\"kind\":\"console_output\",\"data\":\"inherited writer\"}\n",
        )?;
        flush(write)?;
        ready.write_all(b"1")?;
        loop {
            match send(
                write,
                "{\"kind\":\"console_output\",\"data\":\"inherited writer\"}\n",
            ) {
                Ok(()) => {}
                // Retirement closes the reader; the test still owns our lifetime.
                Err(error) if error.kind() == io::ErrorKind::BrokenPipe => break,
                Err(error) => return Err(error),
            }
        }
    } else {
        ready.write_all(b"1")?;
    }
    // The test owns termination. Retain handles without manufacturing EOF.
    let event = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    assert!(!event.is_null());
    let event = unsafe { OwnedHandle::from_raw_handle(event) };
    assert_eq!(
        unsafe { WaitForSingleObject(event.as_raw_handle(), u32::MAX) },
        0
    );
    unreachable!()
}

pub(super) fn run(
    scenario: &str,
    read: *mut c_void,
    write: *mut c_void,
    mut control: Gate,
) -> io::Result<()> {
    if matches!(scenario, "framing_interrupt" | "framing_later") {
        return builtin(read, write, control, scenario == "framing_interrupt");
    }
    send(write, "{\"kind\":\"ready\"}\n")?;
    let frame = command(read)?;
    match scenario {
        "framing_echo" => {
            let marker = std::env::var("TEST_DISPATCHED").unwrap();
            std::fs::write(marker, frame)?;
            send(write, "{\"kind\":\"completed\"}\r\n")?;
            assert!(
                String::from_utf8(command(read)?)
                    .unwrap()
                    .contains("shutdown")
            );
        }
        "framing_malformed" => send(write, "{\"kind\":\n")?,
        "framing_empty" => send(write, "\r\n")?,
        "framing_partial" => {
            send(write, "{\"kind\":")?;
            // Closing the writer while we remain alive exercises ordinary EOF.
            drop(unsafe { OwnedHandle::from_raw_handle(write) });
            let _ = command(read)?;
        }
        "framing_fragmented" => {
            let frame = "{\"kind\":\"console_output\",\"data\":\"fragmented 🦀\"}\r\n";
            let split = frame.find('🦀').unwrap() + 1;
            send_bytes(write, &frame.as_bytes()[..split])?;
            flush(write)?;
            writeln!(control, "prefix consumed")?;
            let mut byte = [0];
            control.read_exact(&mut byte)?;
            send_bytes(write, &frame.as_bytes()[split..])?;
            send(write, "{\"kind\":\"completed\"}\n{\"kind\":")?;
            holder(&mut control, false)?;
        }
        "framing_refill" => holder(&mut control, true)?,
        _ => panic!("unknown framing scenario: {scenario}"),
    }
    Ok(())
}

fn builtin(
    read: *mut c_void,
    write: *mut c_void,
    mut control: Gate,
    interrupt_wait: bool,
) -> io::Result<()> {
    let name: Vec<u16> = format!(r"\\.\pipe\console-framing-{}", std::process::id())
        .encode_utf16()
        .chain(Some(0))
        .collect();
    // Use the server write end so FlushFileBuffers is a documented worker
    // consumption barrier.
    let output = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            2 | 0x40000000,
            0,
            1,
            65536,
            65536,
            0,
            std::ptr::null(),
        )
    };
    assert_ne!(output as isize, -1);
    let output = unsafe { OwnedHandle::from_raw_handle(output) };
    let input = unsafe {
        CreateFileW(
            name.as_ptr(),
            0x80000000,
            0,
            std::ptr::null(),
            3,
            0x40000000,
            std::ptr::null_mut(),
        )
    };
    assert_ne!(input as isize, -1);
    let input = unsafe { OwnedHandle::from_raw_handle(input) };
    let interrupt = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    assert!(!interrupt.is_null());
    let interrupt = unsafe { OwnedHandle::from_raw_handle(interrupt) };
    for handle in [input.as_raw_handle(), interrupt.as_raw_handle()] {
        assert_ne!(unsafe { SetHandleInformation(handle, 1, 1) }, 0);
    }
    let mut child = Command::new(std::env::var("TEST_CONSOLE_BINARY").unwrap())
        .arg("worker")
        .env(
            "MCP_CONSOLE_SIDEBAND_READ_HANDLE",
            (input.as_raw_handle() as usize).to_string(),
        )
        .env(
            "MCP_CONSOLE_SIDEBAND_WRITE_HANDLE",
            (write as usize).to_string(),
        )
        .env(
            "MCP_CONSOLE_INTERRUPT_HANDLE",
            (interrupt.as_raw_handle() as usize).to_string(),
        )
        .spawn()?;
    writeln!(control, "{}", child.id())?;
    drop(input);
    // Initialize R first, then hold a second command inside a UTF-8 scalar.
    send_bytes(output.as_raw_handle(), &command(read)?)?;
    let frame = command(read)?;
    let split = frame
        .windows(4)
        .position(|bytes| bytes == "🦀".as_bytes())
        .unwrap()
        + 1;
    send_bytes(output.as_raw_handle(), &frame[..split])?;
    flush(output.as_raw_handle())?;
    if interrupt_wait {
        assert_ne!(unsafe { SetEvent(interrupt.as_raw_handle()) }, 0);
    }
    writeln!(control, "prefix consumed")?;
    let mut byte = [0];
    control.read_exact(&mut byte)?;
    // The test releases only after real R idle interrupt/callback output.
    send_bytes(output.as_raw_handle(), &frame[split..])?;
    // The outer relay owns this wrapper. Retire the real interpreter at a
    // separate checkpoint so its finalizers cannot consume the wrapper's
    // shutdown budget.
    control.read_exact(&mut byte)?;
    assert_eq!(byte, [b'2']);
    send(output.as_raw_handle(), "{\"kind\":\"shutdown\"}\n")?;
    assert!(child.wait()?.success());
    writeln!(control, "worker exited")?;
    assert_eq!(
        String::from_utf8(command(read)?).unwrap().trim(),
        r#"{"kind":"shutdown"}"#
    );
    Ok(())
}
