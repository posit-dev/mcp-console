use std::io::{Read, Write};
use std::process::Command;

mod windows_gate;
use windows_gate::Gate;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().is_some_and(|arg| arg == "--descendant") {
        std::thread::park();
        return;
    }
    let program = std::env::current_exe().unwrap();
    let mut record = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(std::env::var_os("TEST_RESOLVER_RECORD").unwrap())
        .unwrap();
    writeln!(record, "{:?}", args).unwrap();
    if args.iter().any(|arg| arg == "--version") {
        println!("ir 0.4.0");
    } else if args.first().is_some_and(|arg| arg == "python") {
        println!(
            r#"[{{"version":"3.12.7","version_parts":{{"major":3,"minor":12,"patch":7}},"symlink":null,"variant":"default","implementation":"cpython"}}]"#
        );
    } else {
        if std::env::var_os("TEST_RESOLVER_CACHE_PROBE").is_some() {
            let root = std::path::PathBuf::from(std::env::var_os("CACHE_TEST_ROOT").unwrap());
            let cache = std::path::PathBuf::from(std::env::var_os("UV_CACHE_DIR").unwrap());
            assert!(cache.starts_with(root));
            std::fs::create_dir_all(&cache).unwrap();
            std::fs::write(cache.join("resolver-probe"), "prepared").unwrap();
        }
        if let Ok(gate) = std::env::var("TEST_RESOLVER_GATE") {
            let child = Command::new(&program).arg("--descendant").spawn().unwrap();
            let mut gate = Gate::connect(gate).unwrap();
            writeln!(gate, "{} {}", std::process::id(), child.id()).unwrap();
            // The acceptance owner pins both process handles before allowing
            // this resolver to exit or sending it a control.
            let mut ready = [0];
            gate.read_exact(&mut ready).unwrap();
            assert_eq!(ready, [1]);
            if std::env::var("TEST_RESOLVER_MODE").unwrap() == "blocked" {
                std::thread::park();
            }
        }
        if std::env::var("TEST_RESOLVER_MODE").as_deref() == Ok("failed") {
            eprintln!("fixture resolver failure");
            let code = std::env::var("TEST_RESOLVER_EXIT_CODE")
                .map(|code| code.parse().unwrap())
                .unwrap_or(23);
            std::process::exit(code);
        }
        if program.file_stem().unwrap() == "ir" || args.iter().any(|arg| arg == "ir") {
            print!("{}", std::env::var("TEST_RESOLVER_LIBRARY").unwrap());
        } else {
            std::fs::write(
                args.last().unwrap(),
                std::env::var("TEST_RESOLVER_PYTHON").unwrap(),
            )
            .unwrap();
        }
    }
}
