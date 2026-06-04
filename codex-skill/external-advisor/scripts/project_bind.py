"""Bind the current working directory to a ChatGPT Project URL or ID."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PROJECT_ID_RE = re.compile(r"(g-p-[A-Za-z0-9]+)")


def normalize_project_id(value: str) -> str:
    match = PROJECT_ID_RE.search(value)
    if not match:
        raise ValueError("Expected a ChatGPT Project URL or ID containing g-p-...")
    return match.group(1)


def project_path(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor" / "project.json"


def bind_project(project_dir: Path, value: str, name: str | None) -> Path:
    project_id = normalize_project_id(value)
    path = project_path(project_dir)
    payload = {
        "chatgpt_project_id": project_id,
        "chatgpt_project_source": value,
    }
    if name:
        payload["name"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def clear_project(project_dir: Path) -> Path:
    path = project_path(project_dir)
    path.unlink(missing_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--url", "--id", dest="project", help="ChatGPT Project URL or g-p-... ID.")
    parser.add_argument("--name", help="Optional readable project name.")
    parser.add_argument("--clear", action="store_true", help="Remove the project binding.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.resolve()
    if args.clear:
        path = clear_project(project_dir)
        print(f"Removed ChatGPT Project binding: {path}")
        return 0
    if not args.project:
        print("Provide --url/--id or --clear.")
        return 2
    path = bind_project(project_dir, args.project, args.name)
    print(f"ChatGPT Project binding written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
