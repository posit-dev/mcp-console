#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Static guards for the sandbox process-boundary dependency direction.

Inspect explicit paths, use trees, and literal control arguments without Rust
name resolution or macro expansion.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
RUST_TEXT = re.compile(
    r"(?P<line>//[^\n]*)|(?P<block>/\*)"
    r'|(?P<string>(?:[bc]?r(?P<hashes>#*)".*?"(?P=hashes))'
    r'|(?:[bc]?"(?:\\.|[^"\\])*"))'
    r"|(?:b?'(?:\\(?:x[\da-fA-F]{2}|u\{[\da-fA-F_]+\}|.)|[^'\\\n])')",
    re.DOTALL,
)
RUST_PATH_ROOT = r"\b(?:crate|super(?:::super)*)::"


def rust_sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.rs"))


def rust_code_and_strings(source: str) -> tuple[str, list[tuple[int, str]]]:
    """Mask comments and literals, preserving positions for code diagnostics."""
    code: list[str] = []
    strings: list[tuple[int, str]] = []
    offset = 0
    while token := RUST_TEXT.search(source, offset):
        start, end = token.span()
        if token.group("block"):
            depth = 1
            for delimiter in re.finditer(r"/\*|\*/", source[end:]):
                depth += 1 if delimiter.group() == "/*" else -1
                if depth == 0:
                    end += delimiter.end()
                    break
            assert depth == 0, "unterminated Rust block comment"
        elif token.group("string"):
            strings.append((source.count("\n", 0, start) + 1, source[start:end]))
        code.extend((source[offset:start], re.sub(r"[^\n]", " ", source[start:end])))
        offset = end
    code.append(source[offset:])
    return "".join(code), strings


def flatten_use(statement: str) -> str:
    """Expand explicit use trees; this lexical guard does not resolve names."""
    statement = re.sub(r"\s*::\s*", "::", statement)
    # Expand the innermost group first so each parent prefix reaches its leaves.
    while group := re.search(r"(\w+(?:::\w+)*::)\{([^{}]*)\}", statement):
        prefix, members = group.groups()
        paths = ", ".join(
            prefix + member.strip() for member in members.split(",") if member.strip()
        )
        statement = statement[: group.start()] + paths + statement[group.end() :]
    return " ".join(statement.split())


def matching_lines(
    paths: list[Path], pattern: str, *, strings: bool = False
) -> list[str]:
    matches: list[str] = []
    forbidden = re.compile(pattern)
    for path in paths:
        source, literals = rust_code_and_strings(path.read_text(encoding="utf-8"))
        if strings:
            candidates = literals
        else:
            candidates = [
                (
                    source.count("\n", 0, statement.start()) + 1,
                    flatten_use(statement.group()),
                )
                for statement in re.finditer(
                    r"\buse\s+[^;]+;|\b(?:crate|super|sandbox)(?:\s*::\s*\w+)+",
                    source,
                )
            ]
        for line_number, line in candidates:
            if forbidden.search(line):
                matches.append(
                    f"{path.relative_to(ROOT).as_posix()}:{line_number}: {line.strip()}"
                )
    return list(dict.fromkeys(matches))


class SandboxProcessBoundaryTests(unittest.TestCase):
    def test_server_relay_and_worker_do_not_import_sandbox_internals(self) -> None:
        host_sources = [
            path
            for path in rust_sources(SOURCE_ROOT)
            if path.relative_to(SOURCE_ROOT).parts[0]
            not in {"main.rs", "cli.rs", "sandbox.rs", "sandbox"}
        ]
        self.assertTrue(host_sources, "no Rust source files found")
        violations = matching_lines(
            host_sources,
            RUST_PATH_ROOT + r"sandbox\b|\bsandbox::platform\b",
        )
        self.assertEqual(
            violations,
            [],
            "server, relay, or worker code depends on private sandbox implementation:\n"
            + "\n".join(violations),
        )

    def test_sandbox_does_not_depend_on_relay_or_server_protocols(self) -> None:
        sandbox_sources = [
            SOURCE_ROOT / "sandbox.rs",
            *rust_sources(SOURCE_ROOT / "sandbox"),
            # Shared process primitives must stay independent of runtime owners.
            SOURCE_ROOT / "process_exit.rs",
            SOURCE_ROOT / "process_descriptors.rs",
        ]
        missing = [path for path in sandbox_sources if not path.is_file()]
        self.assertEqual(missing, [], f"missing source files: {missing}")
        violations = matching_lines(
            sandbox_sources,
            RUST_PATH_ROOT
            + r"(?:worker(?:_client|_protocol|_relay)?|relay_protocol|server(?:_transport)?)\b",
        )
        self.assertEqual(
            violations,
            [],
            "sandbox code depends on relay or server internals:\n"
            + "\n".join(violations),
        )

    def test_relay_does_not_know_sandbox_control_arguments(self) -> None:
        relay_sources = [
            SOURCE_ROOT / "worker_relay.rs",
            SOURCE_ROOT / "relay_protocol.rs",
            *rust_sources(SOURCE_ROOT / "worker_relay"),
        ]
        missing = [path for path in relay_sources if not path.is_file()]
        self.assertEqual(missing, [], f"missing source files: {missing}")
        violations = matching_lines(
            relay_sources,
            r"--exit-with-parent|sandbox-manager|sandbox-target",
            strings=True,
        )
        self.assertEqual(
            violations,
            [],
            "relay code knows sandbox control-plane arguments:\n"
            + "\n".join(violations),
        )


class ArchitectureCheckTests(unittest.TestCase):
    def test_checker_preserves_rust_boundaries(self) -> None:
        cases = (
            ("sandbox/runner.rs", "use super::super::server;", "depends on"),
            (
                "sandbox/runner.rs",
                "use super::{super::{super::{worker_relay as relay}}};",
                "depends on",
            ),
            ("process_exit.rs", "use crate::server;", "depends on"),
            ("process_exit.rs", "use super::worker_client;", "depends on"),
            ("process_descriptors.rs", "use crate::worker;", "depends on"),
            (
                "sandbox/runner.rs",
                "use super::{process, process_tree};",
                None,
            ),
            (
                "sandbox/runner.rs",
                "use crate::{process_descriptors, process_exit};",
                None,
            ),
            ("sideband.rs", "use crate::sandbox::platform;", "depends on"),
            ("python.rs", "use crate::sandbox::platform;", "depends on"),
            ("server.rs", "// Explain why crate::sandbox is private.\n", None),
            ("sandbox.rs", 'const NOTE: &str = "crate::server is separate";', None),
            (
                "server.rs",
                """
                /* Document crate::sandbox.
                   /* Nested documentation with a " quote. */
                   use crate::{sandbox};
                */
                const NOTE: &str = r##"a " quote, crate::sandbox, and "# text"##;
                const BYTE_NOTE: &[u8] = br#"crate::sandbox"#;
                const QUOTE: char = '"';
                """,
                None,
            ),
            (
                "server.rs",
                """
                const QUOTE: char = '"';
                use crate::{/* a comment */ sandbox};
                """,
                "depends on",
            ),
            (
                "server.rs",
                "fn forbidden() { crate /* a comment */ :: sandbox::run(); }",
                "depends on",
            ),
            ("worker_relay.rs", "// The launcher owns --exit-with-parent.\n", None),
            (
                "worker_relay.rs",
                'const CONTROL: &str = "--exit-with-parent";',
                "knows sandbox control-plane arguments",
            ),
            (
                "worker_relay.rs",
                'const CONTROL: &str = r#"sandbox-manager"#;',
                "knows sandbox control-plane arguments",
            ),
            ("server.rs", "use crate::{sandbox, server};", "depends on"),
            (
                "worker_client/process.rs",
                "use super::{sandbox as private};",
                "depends on",
            ),
            (
                "server.rs",
                """
                use crate :: {
                    process_exit,
                    sandbox :: {self, child::Cleanup as Cleanup},
                };
                """,
                "depends on",
            ),
            (
                "sandbox/runner.rs",
                "use crate::{worker, relay_protocol};",
                "depends on",
            ),
            (
                "sandbox/runner.rs",
                "use crate::{server::{self, Server}};",
                "depends on",
            ),
            ("server.rs", "use crate::{process_exit, cli};", None),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            checker = root / "tests" / "architecture.py"
            shutil.copy2(__file__, checker)
            source = root / "src"
            # Exercise the public guards on small source trees, rather than
            # copying production sources and launching Python for each snippet.
            for relative in {
                "sandbox.rs",
                "process_exit.rs",
                "process_descriptors.rs",
                "worker_relay.rs",
                "relay_protocol.rs",
                *(relative for relative, _, _ in cases),
            }:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            with patch.multiple(sys.modules[__name__], ROOT=root, SOURCE_ROOT=source):
                for relative, statement, diagnostic in cases:
                    with self.subTest(path=relative, statement=statement):
                        path = source / relative
                        path.write_text(statement, encoding="utf-8")
                        try:
                            result = unittest.TestResult()
                            unittest.defaultTestLoader.loadTestsFromTestCase(
                                SandboxProcessBoundaryTests
                            ).run(result)
                        finally:
                            path.write_text("", encoding="utf-8")
                        self.assertEqual(result.errors, [])
                        if diagnostic is None:
                            self.assertTrue(result.wasSuccessful(), result.failures)
                        else:
                            self.assertEqual(len(result.failures), 1, result.failures)
                            message = result.failures[0][1]
                            self.assertIn(diagnostic, message)
                            self.assertIn(f"src/{relative}:", message)
            # One process invocation verifies the checker entry point and its
            # exit status/diagnostic for a real dependency-direction violation.
            (source / "sandbox/runner.rs").write_text("use crate::server;")
            result = subprocess.run(
                [sys.executable, checker, "SandboxProcessBoundaryTests"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("depends on", result.stderr)
            self.assertIn("src/sandbox/runner.rs:1:", result.stderr)


if __name__ == "__main__":
    unittest.main()
