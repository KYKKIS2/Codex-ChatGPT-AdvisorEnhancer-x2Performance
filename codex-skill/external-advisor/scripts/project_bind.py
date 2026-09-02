"""Bind the current working directory to a ChatGPT Project URL or ID."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import advisor_safety as safety


PROJECT_ID_RE = re.compile(r"(g-p-[A-Za-z0-9]+)")


def normalize_project_id(value: str) -> str:
    match = PROJECT_ID_RE.search(value)
    if not match:
        raise ValueError("Expected a ChatGPT Project URL or ID containing g-p-...")
    return match.group(1)


def project_path(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor" / "project.json"


def resolve_project_dir(project_dir: Path | None) -> Path:
    if project_dir is not None:
        return project_dir.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    return advisor.advisor_project_dir()


def bind_project(project_dir: Path, value: str, name: str | None) -> Path:
    try:
        project_id = normalize_project_id(value)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    path = project_path(project_dir)
    payload = {
        "chatgpt_project_id": project_id,
        "chatgpt_project_source": value,
    }
    if name:
        payload["name"] = name
    safety.atomic_write_json(path, payload)
    try:
        import advisor_cloud_catalog  # noqa: PLC0415

        advisor_cloud_catalog.register_project_binding(project_dir, payload)
    except Exception:
        print(
            "Advisor GUI catalog registration was skipped; the repository Project binding is still valid.",
            file=sys.stderr,
        )
    return path


def clear_project(project_dir: Path) -> Path:
    path = project_path(project_dir)
    path.unlink(missing_ok=True)
    try:
        import advisor_cloud_catalog  # noqa: PLC0415

        advisor_cloud_catalog.unregister_project_path(project_dir)
    except Exception:
        print(
            "Advisor GUI catalog cleanup was skipped; the repository Project binding was removed.",
            file=sys.stderr,
        )
    return path


def create_project(project_dir: Path, name: str, timeout: int) -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415

    previous = os.environ.get("ADVISOR_PROJECT_DIR")
    os.environ["ADVISOR_PROJECT_DIR"] = str(project_dir)
    try:
        project_id = advisor.create_chatgpt_project(name, timeout)
    finally:
        if previous is None:
            os.environ.pop("ADVISOR_PROJECT_DIR", None)
        else:
            os.environ["ADVISOR_PROJECT_DIR"] = previous
    if not project_id:
        raise RuntimeError("Could not create ChatGPT Project. Check HAR/auth and ChatGPT endpoint availability.")
    return project_path(project_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, help="Project directory. Defaults to the nearest Git repo root or current directory.")
    parser.add_argument("--url", "--id", dest="project", help="ChatGPT Project URL or g-p-... ID.")
    parser.add_argument("--name", help="Optional readable project name.")
    parser.add_argument("--create", action="store_true", help="Create a private ChatGPT Project and bind it.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--clear", action="store_true", help="Remove the project binding.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = resolve_project_dir(args.project_dir)
    if args.clear:
        path = clear_project(project_dir)
        print(f"Removed ChatGPT Project binding: {path}")
        return 0
    if args.create:
        name = args.name or project_dir.name or "Codex Advisor"
        path = create_project(project_dir, name, args.timeout)
        print(f"ChatGPT Project created and bound: {path}")
        return 0
    if not args.project:
        print("Provide --url/--id, --create, or --clear.")
        return 2
    path = bind_project(project_dir, args.project, args.name)
    print(f"ChatGPT Project binding written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
