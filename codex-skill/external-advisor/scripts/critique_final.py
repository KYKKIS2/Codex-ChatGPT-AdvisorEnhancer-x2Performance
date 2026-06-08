#!/usr/bin/env python3
"""Ask the critic advisor to review a Codex draft before the final answer."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def sanitize_text(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")


def read_text(path: str) -> str:
    return sanitize_text(Path(path).read_text(encoding="utf-8"))


def conclave_script_path() -> Path:
    return Path(__file__).resolve().with_name("conclave.py")


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def build_prompt(args: argparse.Namespace) -> str:
    blocks = [
        "Critique this Codex draft before the final answer is sent.",
        "Focus on weak assumptions, missing risks, unclear guidance, wrong direction, and concrete improvements.",
    ]
    if args.prompt:
        blocks.append(f"Original user request:\n{args.prompt.strip()}")
    for path in args.context_file:
        blocks.append(f"Context file: {path}\n{read_text(path)}")
    blocks.append(f"Codex draft:\n{args.draft.strip()}")
    blocks.append(
        "Return concise critique only. Do not write the final answer. "
        "Say what Codex should change, remove, verify, or preserve."
    )
    return "\n\n---\n\n".join(block for block in blocks if block.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Original user request or task.")
    parser.add_argument("--draft", help="Draft answer to critique. Reads stdin when omitted.")
    parser.add_argument("--draft-file", help="Read draft answer from a file.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional UTF-8 context file.")
    parser.add_argument("--provider", choices=["openai", "openai-compatible"], default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"))
    parser.add_argument("--base-url", default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"))
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL", "gpt-5-5-thinking"))
    parser.add_argument("--reasoning-effort", default=os.environ.get("ADVISOR_REASONING_EFFORT", "high"))
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1200")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    parser.add_argument("--machine-json", action="store_true", help="Request machine-readable JSON critique.")
    parser.add_argument("--no-sync", action="store_true", help="Skip remote transcript sync.")
    parser.add_argument("--dry-run", action="store_true", help="Print the generated prompt without calling the model.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    args.project_dir = resolve_project_dir(args.project_dir)
    if args.draft_file:
        args.draft = read_text(args.draft_file)
    elif args.draft is None:
        args.draft = sanitize_text(sys.stdin.read())
    else:
        args.draft = sanitize_text(args.draft)
    if not args.draft.strip():
        print("Provide --draft, --draft-file, or pipe a draft on stdin.", file=sys.stderr)
        return 2

    prompt = build_prompt(args)
    if args.dry_run:
        print(prompt)
        return 0

    env = os.environ.copy()
    env["ADVISOR_PROVIDER"] = args.provider
    env["ADVISOR_BASE_URL"] = args.base_url
    env["ADVISOR_MODEL"] = args.model
    env["ADVISOR_REASONING_EFFORT"] = args.reasoning_effort
    env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(args.max_output_tokens)

    command = [
        sys.executable,
        str(conclave_script_path()),
        "--provider", args.provider,
        "--model", args.model,
        "--timeout", str(args.timeout),
        "--mode", "general",
        "--roles", "critic",
        "--no-synthesis",
    ]
    if args.no_sync:
        command.append("--no-sync")
    if args.machine_json:
        command.append("--machine-json")

    completed = subprocess.run(
        command,
        cwd=args.project_dir,
        env=env,
        input=prompt,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=args.timeout + 30,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
