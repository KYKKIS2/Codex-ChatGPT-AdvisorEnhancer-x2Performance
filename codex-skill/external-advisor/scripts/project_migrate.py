"""Migrate old advisor state into ChatGPT Project-bound state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
import advisor  # noqa: E402


ROOT_STATE_FILES = ("conversation.json", "transcript.json", "transcript.md")


def advisor_dir(project_dir: Path) -> Path:
    return project_dir / ".codex-advisor"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def project_binding_path(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "project.json"


def root_conversation_path(project_dir: Path) -> Path:
    return advisor_dir(project_dir) / "conversation.json"


def project_state_path(project_dir: Path, project_id: str) -> Path:
    return advisor_dir(project_dir) / "projects" / project_id / "conversation.json"


def existing_project_id(project_dir: Path) -> str | None:
    binding = load_json(project_binding_path(project_dir))
    for key in ("chatgpt_project_id", "gizmo_id", "project_id", "chatgpt_project_url"):
        value = binding.get(key)
        if isinstance(value, str):
            project_id = advisor.normalize_chatgpt_project_id(value)
            if project_id:
                return project_id
    return None


def conversation_id_from_state(path: Path) -> str | None:
    data = load_json(path)
    conversation = data.get("conversation")
    if not isinstance(conversation, dict):
        return None
    conversation_id = conversation.get("conversation_id")
    return conversation_id if isinstance(conversation_id, str) and conversation_id else None


def find_project_id_in_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("gizmo_id", "conversation_template_id", "id", "short_url"):
            value = payload.get(key)
            if isinstance(value, str):
                project_id = advisor.normalize_chatgpt_project_id(value)
                if project_id:
                    return project_id
        for value in payload.values():
            project_id = find_project_id_in_payload(value)
            if project_id:
                return project_id
    elif isinstance(payload, list):
        for value in payload:
            project_id = find_project_id_in_payload(value)
            if project_id:
                return project_id
    return None


def infer_project_id_from_root_state(project_dir: Path, timeout: int) -> str | None:
    conversation_id = conversation_id_from_state(root_conversation_path(project_dir))
    if not conversation_id:
        return None
    auth = advisor.load_chatgpt_auth()
    if not auth:
        return None
    try:
        data = advisor.get_json(f"https://chatgpt.com/backend-api/conversation/{conversation_id}", auth["headers"], timeout)
    except RuntimeError as exc:
        print(f"Could not inspect old advisor conversation: {advisor.redact_sensitive(str(exc))}", file=sys.stderr)
        return None
    return find_project_id_in_payload(data)


def write_binding(project_dir: Path, project_id: str, name: str | None, source: str) -> Path:
    path = project_binding_path(project_dir)
    payload = load_json(path)
    payload["chatgpt_project_id"] = project_id
    payload.setdefault("chatgpt_project_source", source)
    if name:
        payload.setdefault("name", name)
    write_json(path, payload)
    return path


def copy_root_state(project_dir: Path, project_id: str, overwrite: bool) -> list[Path]:
    root = advisor_dir(project_dir)
    destination_root = project_state_path(project_dir, project_id).parent
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for name in ROOT_STATE_FILES:
        source = root / name
        destination = destination_root / name
        if not source.exists():
            continue
        if destination.exists() and not overwrite:
            continue
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def archive_root_state(project_dir: Path) -> list[Path]:
    root = advisor_dir(project_dir)
    existing = [root / name for name in ROOT_STATE_FILES if (root / name).exists()]
    if not existing:
        return []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = root / "legacy-root" / stamp
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for source in existing:
        destination = archive_dir / source.name
        shutil.move(str(source), str(destination))
        moved.append(destination)
    return moved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--url", "--id", dest="project", help="Existing ChatGPT Project URL or g-p-... ID to bind.")
    parser.add_argument("--name", help="Readable Project name for local metadata or auto-create.")
    parser.add_argument("--create-missing", action="store_true", help="Create a private ChatGPT Project if no binding can be inferred.")
    parser.add_argument("--archive-root", action="store_true", help="Move old root conversation/transcript files into .codex-advisor/legacy-root.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite project-specific conversation/transcript files when copying root state.")
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = args.project_dir.resolve()
    os.environ["ADVISOR_PROJECT_DIR"] = str(project_dir)
    name = args.name or project_dir.name or "Codex Advisor"

    project_id = advisor.normalize_chatgpt_project_id(args.project) if args.project else existing_project_id(project_dir)
    source = "existing-binding" if project_id else ""

    if not project_id:
        project_id = infer_project_id_from_root_state(project_dir, args.timeout)
        source = "inferred-from-root-conversation" if project_id else ""

    if not project_id and args.create_missing:
        project_id = advisor.create_chatgpt_project(name, args.timeout)
        source = "auto-created-by-migration" if project_id else ""

    if not project_id:
        print("No ChatGPT Project binding found or inferred. Use --url or --create-missing.")
        return 1

    binding = write_binding(project_dir, project_id, name, source)
    copied = copy_root_state(project_dir, project_id, args.overwrite)
    archived = archive_root_state(project_dir) if args.archive_root else []

    print(f"Project binding: {binding}")
    print(f"Project id: {project_id}")
    print(f"Copied root state files: {len(copied)}")
    if archived:
        print(f"Archived root state files: {len(archived)}")
    elif args.archive_root:
        print("Archived root state files: 0")
    print(f"Project state dir: {project_state_path(project_dir, project_id).parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
