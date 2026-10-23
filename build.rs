use sha2::{Digest as _, Sha256};
use std::path::PathBuf;

fn main() {
    if std::env::var_os("CARGO_CFG_WINDOWS").is_some() {
        println!("cargo:rerun-if-changed=src/windows/console.manifest");
        println!("cargo:rerun-if-changed=src/windows/console.rc");
        embed_resource::compile_for_everything("src/windows/console.rc", embed_resource::NONE)
            .manifest_required()
            .expect("failed to embed Windows UTF-8 manifest");
    }
    println!("cargo:rerun-if-changed=src/r_graphics.c");
    println!("cargo:rerun-if-changed=src/r_repl.c");
    println!("cargo:rerun-if-changed=src/worker/interrupt.c");

    if matches!(
        std::env::var("CARGO_CFG_TARGET_OS").as_deref(),
        Ok("macos" | "linux" | "windows")
    ) {
        bind_private_runner();
    }
    if std::env::var_os("CARGO_CFG_UNIX").is_some()
        || std::env::var_os("CARGO_CFG_WINDOWS").is_some()
    {
        let mut native = cc::Build::new();
        if std::env::var_os("CARGO_CFG_UNIX").is_some() {
            native.file("src/worker/interrupt.c");
        }
        native
            .file("src/r_graphics.c")
            .file("src/r_repl.c")
            .compile("mcp_console_r_repl");
    }
}

fn bind_private_runner() {
    let root = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let output = PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
    // OUT_DIR is <target prefix>/<profile>/build/<package>/out.
    // Native bundles require Cargo's default shared build/target layout: Cargo
    // does not expose the caller's --target-dir to build scripts. A separate
    // build.build-dir is unsupported for running the Cargo output; wheels use
    // wheel-data independently. See RELEASE.md.
    let prefix = output.ancestors().nth(4).unwrap();
    println!("cargo:rerun-if-changed=sandbox-runner.json");
    println!("cargo:rerun-if-changed=target/sandbox-runner-build.json");
    let target = std::env::var("TARGET").expect("Cargo did not provide its build target");

    let pin: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.join("sandbox-runner.json")).unwrap()).unwrap();
    let build: serde_json::Value =
        serde_json::from_slice(&std::fs::read(root.join("target/sandbox-runner-build.json"))
            .expect("private sandbox runner is not staged; use uv tool install --reinstall . or run scripts/stage-sandbox-runner"))
            .expect("invalid private sandbox runner build manifest");
    assert_eq!(build["source_revision"], pin["commit"]);
    assert_eq!(build["target"].as_str(), Some(target.as_str()));
    // Windows locks running executables. Keep each complete native bundle at
    // an immutable path so rebuilding cannot replace an active companion.
    // Wheels retain their flat layout, owned by the packaging backend.
    let native_bundle = if target.contains("windows") {
        let digest = Sha256::digest(serde_json::to_vec(&build).unwrap());
        let digest_hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        format!("libexec/mcp-console-{digest_hex}")
    } else {
        String::new()
    };
    let prefix = prefix.join(&native_bundle);
    let mut artifacts = String::new();
    let mut bundle = vec![
        ("LICENSE", "share/licenses/mcp-console/LICENSE"),
        ("NOTICE", "share/licenses/mcp-console/NOTICE"),
    ];
    if target.contains("windows") {
        bundle.push(("mcp-console-sandbox.exe", "libexec/mcp-console-sandbox.exe"));
    } else {
        bundle.push(("mcp-console-sandbox", "libexec/mcp-console-sandbox"));
    }
    if target.contains("linux") {
        bundle.extend([
            ("bwrap", "libexec/bwrap"),
            (
                "bubblewrap-COPYING",
                "share/licenses/mcp-console/bubblewrap-COPYING",
            ),
            (
                "bubblewrap-NOTICE",
                "share/licenses/mcp-console/bubblewrap-NOTICE",
            ),
            (
                "bubblewrap-SOURCE.json",
                "share/licenses/mcp-console/bubblewrap-SOURCE.json",
            ),
        ]);
        if build["artifacts"].get("libcap-NOTICE").is_some() {
            bundle.push(("libcap-NOTICE", "share/licenses/mcp-console/libcap-NOTICE"));
        }
    }
    for (name, relative) in bundle {
        let source = root.join("wheel-data/data").join(relative);
        println!("cargo:rerun-if-changed={}", source.display());
        let bytes = std::fs::read(&source).unwrap();
        let digest = Sha256::digest(&bytes);
        let digest_hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        assert_eq!(
            build["artifacts"][name].as_str(),
            Some(digest_hex.as_str()),
            "private sandbox runner artifact {name} changed during staging"
        );
        if name == "bubblewrap-SOURCE.json" {
            let provenance: serde_json::Value =
                serde_json::from_slice(&bytes).expect("invalid Bubblewrap provenance");
            assert_eq!(
                provenance["source_revision"], pin["commit"],
                "stale Bubblewrap provenance"
            );
            assert_eq!(provenance["source_repository"], pin["repository"]);
            assert_eq!(provenance["source_directory"], "codex-rs/vendor/bubblewrap");
            assert_eq!(provenance["build_script"], "codex-rs/bwrap/build.rs");
            assert_eq!(provenance["wrapper_directory"], "codex-rs/bwrap");
            assert_eq!(provenance["sha256"], build["artifacts"]["bwrap"]);
            if provenance["libcap_linkage"] == "static" {
                assert!(
                    build["artifacts"].get("libcap-NOTICE").is_some(),
                    "missing static libcap notice"
                );
            } else {
                assert_eq!(provenance["libcap_linkage"], "dynamic");
                assert!(build["artifacts"].get("libcap-NOTICE").is_none());
            }
        }
        artifacts.push_str(&format!("({relative:?}, {:?}),\n", digest.as_slice()));
        // Wheel staging belongs to the packaging backend. Cargo only installs
        // the verified companion beside its own native build output.
        {
            let destination = prefix.join(relative);
            std::fs::create_dir_all(destination.parent().unwrap()).unwrap();
            let permissions = source.metadata().unwrap().permissions();
            // These files are also tracked inputs. Rewriting identical bytes
            // would make every following Cargo invocation rebuild the crate.
            if !destination.exists()
                || std::fs::read(&destination).unwrap() != bytes
                || destination.metadata().unwrap().permissions() != permissions
            {
                // An active sandbox may still use the previous executable.
                let temporary =
                    destination.with_file_name(format!(".{name}-{}", std::process::id()));
                std::fs::write(&temporary, &bytes).unwrap();
                std::fs::set_permissions(&temporary, permissions).unwrap();
                std::fs::rename(temporary, &destination).unwrap();
            }
            // Restore removed data even when Cargo can reuse the compiled binary.
            println!("cargo:rerun-if-changed={}", destination.display());
        }
    }
    let generated = output.join("sandbox_runner_installation.rs");
    let contents = format!(
        "#[cfg(windows)] const NATIVE_BUNDLE: &str = {native_bundle:?};\n\
             const ARTIFACTS: &[(&str, [u8; 32])] = &[{artifacts}];\n",
    );
    if !generated.exists() || std::fs::read(&generated).unwrap() != contents.as_bytes() {
        std::fs::write(generated, contents)
            .expect("failed to bind private sandbox runner installation");
    }
}
