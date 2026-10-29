"""Launch one Shiny process; keep its generated files inside this app directory."""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--binary", type=Path, default=shutil.which("mcp-console"))
    parser.add_argument(
        "--renviron", type=Path, help="Read only OPENAI_API_KEY from this file"
    )
    parser.add_argument("--private-path", type=Path, action="append", default=[])
    parser.add_argument(
        "--warmup", action="store_true", help="Prepare and check R/Python, then exit"
    )
    args = parser.parse_args()
    if args.binary is None:
        parser.error(
            "Install MCP Console or pass --binary /absolute/path/to/mcp-console"
        )
    if args.renviron:
        for line in args.renviron.expanduser().read_text().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "OPENAI_API_KEY":
                parts = shlex.split(value, comments=True)
                if len(parts) != 1:
                    parser.error("OPENAI_API_KEY must contain one value")
                os.environ["OPENAI_API_KEY"] = parts[0]
                break
        args.private_path.append(args.renviron.expanduser())
    if not os.environ.get("OPENAI_API_KEY") and not args.warmup:
        parser.error("Set OPENAI_API_KEY or provide --renviron ~/.Renviron")
    if args.warmup:
        os.environ.setdefault("OPENAI_API_KEY", "unused-for-warmup")
    # Shiny's own temporary files must be covered by every worker's deny rule.
    temporary = ROOT / ".runtime" / "server" / "private" / "controller-tmp"
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update(
        {
            "TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CONSOLE_CHAT_RUNTIME": str(ROOT / ".runtime" / "server"),
            "CONSOLE_CHAT_BINARY": str(Path(args.binary).absolute()),
            "CONSOLE_CHAT_CAPACITY": str(args.sessions),
            "CONSOLE_CHAT_PRIVATE_PATHS": json.dumps(
                [str(p.resolve()) for p in args.private_path]
            ),
        }
    )
    if args.warmup:
        from runtime import Pool

        async def warmup() -> None:
            pool = Pool(
                ROOT / ".runtime" / "server",
                Path(args.binary),
                Path(sys.executable),
                args.sessions,
                tuple(args.private_path),
            )
            try:
                session = await pool.open()
                for code in (
                    {"r": "cat('R ready\\n')"},
                    {"python": "import pandas, matplotlib; print('Python ready')"},
                ):
                    result = await session.send(**code)
                    print(result.value)
                    while "[running; poll with an empty send]" in str(result.value):
                        result = await session.send()
                        print(result.value)
            finally:
                await pool.close()

        asyncio.run(warmup())
    else:
        import uvicorn
        from app import app

        uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
