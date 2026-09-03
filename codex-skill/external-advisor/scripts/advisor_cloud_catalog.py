#!/usr/bin/env python3
"""Private catalog for optional ChatGPT Project import into the local g4f GUI."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

import advisor_concurrency as concurrency
import advisor_safety as safety


CATALOG_VERSION = 1
PROJECT_ID_RE = re.compile(r"^g-p-[A-Za-z0-9]+$")
MAX_TITLE_CHARS = 300
MAX_RECOVERY_HISTORY = 20


class CatalogError(RuntimeError):
    """Raised when private GUI catalog state cannot be used safely."""


class AccountMismatchError(CatalogError):
    """Raised when the active HAR belongs to another ChatGPT account."""


class RecoveryStateChangedError(CatalogError):
    """Raised when a recovery action no longer targets the current journal."""


def state_root() -> Path:
    explicit = os.environ.get("ADVISOR_GUI_STATE_DIR")
    if explicit:
        root = Path(explicit).expanduser()
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        root = codex_home / "advisor-gui"
    safety.ensure_private_dir(root)
    return root.resolve()


def catalog_path() -> Path:
    return state_root() / "catalog.json"


def catalog_lock(timeout: float | None = 30.0) -> concurrency.InterProcessLock:
    return concurrency.InterProcessLock(
        state_root() / "catalog.lock",
        timeout=timeout,
        wait_message="Advisor GUI is waiting for another catalog update.",
    )


def _empty_catalog() -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "account_fingerprint": "",
        "projects": {},
    }


def _load_unlocked() -> dict[str, Any]:
    path = catalog_path()
    if not path.exists():
        return _empty_catalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("The private advisor GUI catalog is malformed.") from exc
    if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
        raise CatalogError("The private advisor GUI catalog has an unsupported version.")
    if not isinstance(data.get("projects"), dict):
        raise CatalogError("The private advisor GUI catalog has an invalid project map.")
    return data


def _save_unlocked(data: dict[str, Any]) -> None:
    safety.atomic_write_json(catalog_path(), data, mode=0o600, sort_keys=True)


def _durably_save_submission_unlocked(data: dict[str, Any]) -> None:
    """Persist the pre-POST journal before provider stream iteration can begin."""
    _save_unlocked(data)
    path = catalog_path()
    try:
        # Windows CPython maps fsync() to _commit(), which rejects a read-only
        # descriptor with EBADF. Open the already-written journal without
        # truncation but with write access for cross-platform behavior.
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise CatalogError("Could not durably record the cloud submission journal.") from exc


def _key_bytes_unlocked() -> bytes:
    path = state_root() / "catalog.key"
    if not path.exists():
        value = secrets.token_bytes(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise CatalogError("Could not read the private advisor GUI catalog key.") from exc
    if len(value) != 32:
        raise CatalogError("The private advisor GUI catalog key is invalid.")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def _opaque_unlocked(kind: str, value: str) -> str:
    digest = hmac.new(
        _key_bytes_unlocked(),
        f"{kind}\0{value}".encode("utf-8", errors="strict"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def recovery_journal_token(record: dict[str, Any]) -> str | None:
    """Return an opaque identity for the exact unresolved journal in a record."""
    submission = record.get("submission")
    reconcile_message_id = record.get("reconcile_message_id")
    material: dict[str, Any] = {}
    if isinstance(submission, dict):
        nonce = submission.get("nonce")
        started_at = submission.get("started_at")
        if not isinstance(nonce, str) or not nonce:
            return None
        material["submission_nonce"] = nonce
        if isinstance(started_at, (int, float)):
            material["submission_started_at"] = started_at
    if isinstance(reconcile_message_id, str) and reconcile_message_id:
        material["reconcile_message_id"] = reconcile_message_id
        completed_at = record.get("last_completed_at")
        if isinstance(completed_at, (int, float)):
            material["last_completed_at"] = completed_at
    if not material:
        return None
    serialized = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return _opaque_unlocked("recovery-journal", serialized)


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def account_identity(auth: dict[str, Any]) -> str:
    headers = auth.get("headers") if isinstance(auth, dict) else None
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    token = ""
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    claims = _decode_jwt_claims(token)
    nested = claims.get("https://api.openai.com/auth")
    nested = nested if isinstance(nested, dict) else {}
    stable_parts = [
        nested.get("chatgpt_account_id"),
        nested.get("chatgpt_account_user_id"),
        nested.get("chatgpt_user_id"),
        nested.get("user_id"),
        claims.get("sub"),
    ]
    values = [str(value).strip() for value in stable_parts if isinstance(value, str) and value.strip()]
    if not values:
        fallback = auth.get("user_id") if isinstance(auth, dict) else None
        if isinstance(fallback, str) and fallback.strip():
            values.append(fallback.strip())
    if not values:
        raise CatalogError("The active ChatGPT authentication has no stable account identity.")
    return "\0".join(values)


def _account_fingerprint_unlocked(auth: dict[str, Any]) -> str:
    return _opaque_unlocked("account", account_identity(auth))


def _bind_account_unlocked(data: dict[str, Any], auth: dict[str, Any] | None) -> None:
    if auth is None:
        return
    current = _account_fingerprint_unlocked(auth)
    saved = data.get("account_fingerprint")
    if isinstance(saved, str) and saved and not hmac.compare_digest(saved, current):
        raise AccountMismatchError(
            "The advisor GUI catalog belongs to a different ChatGPT account. "
            "Use the original account or reset the optional GUI catalog explicitly."
        )
    data["account_fingerprint"] = current


def _project_id(binding: dict[str, Any]) -> str:
    value = binding.get("chatgpt_project_id") if isinstance(binding, dict) else None
    if not isinstance(value, str) or not PROJECT_ID_RE.fullmatch(value.strip()):
        raise CatalogError("The selected directory has no valid ChatGPT Project binding.")
    return value.strip()


def _clean_title(value: Any, fallback: str) -> str:
    title = " ".join(value.split()) if isinstance(value, str) else ""
    return (title or fallback)[:MAX_TITLE_CHARS]


def register_project_binding(
    project_dir: Path,
    binding: dict[str, Any],
    *,
    auth: dict[str, Any] | None = None,
) -> str:
    project_id = _project_id(binding)
    resolved = project_dir.expanduser().resolve()
    if not resolved.is_dir():
        raise CatalogError("The project directory is not available.")
    now = time.time()
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        key = _opaque_unlocked("project", project_id)
        projects = data["projects"]
        record = projects.get(key)
        if not isinstance(record, dict):
            record = {
                "project_id": project_id,
                "registered_at": now,
                "paths": [],
                "conversations": {},
            }
        paths = record.get("paths") if isinstance(record.get("paths"), list) else []
        path_text = str(resolved)
        record["paths"] = sorted({str(item) for item in paths if isinstance(item, str)} | {path_text})
        record["name"] = _clean_title(binding.get("name"), resolved.name or "ChatGPT Project")
        record["updated_at"] = now
        if not isinstance(record.get("conversations"), dict):
            record["conversations"] = {}
        projects[key] = record
        _save_unlocked(data)
        return key


def register_bound_project(project_dir: Path, *, auth: dict[str, Any] | None = None) -> str:
    resolved = project_dir.expanduser().resolve()
    path = resolved / ".codex-advisor" / "project.json"
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError("The selected directory has no .codex-advisor/project.json binding.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("The selected directory has a malformed ChatGPT Project binding.") from exc
    if not isinstance(binding, dict):
        raise CatalogError("The selected directory has a malformed ChatGPT Project binding.")
    return register_project_binding(resolved, binding, auth=auth)


def unregister_project_path(project_dir: Path) -> None:
    target = str(project_dir.expanduser().resolve())
    with catalog_lock():
        data = _load_unlocked()
        changed = False
        for key, record in list(data["projects"].items()):
            if not isinstance(record, dict):
                continue
            paths = record.get("paths") if isinstance(record.get("paths"), list) else []
            filtered = [item for item in paths if isinstance(item, str) and item != target]
            if len(filtered) == len(paths):
                continue
            changed = True
            if filtered:
                record["paths"] = filtered
            else:
                data["projects"].pop(key, None)
        if changed:
            _save_unlocked(data)


def reset_catalog() -> None:
    """Remove catalog mappings but retain the private opaque-key seed."""
    with catalog_lock():
        _save_unlocked(_empty_catalog())


def list_projects(auth: dict[str, Any]) -> list[dict[str, Any]]:
    with catalog_lock():
        data = _load_unlocked()
        before = data.get("account_fingerprint")
        _bind_account_unlocked(data, auth)
        if before != data.get("account_fingerprint"):
            _save_unlocked(data)
        output: list[dict[str, Any]] = []
        for key, record in data["projects"].items():
            if not isinstance(key, str) or not isinstance(record, dict):
                continue
            output.append({
                "key": key,
                "name": _clean_title(record.get("name"), "ChatGPT Project"),
                "registeredPaths": len(record.get("paths") or []),
            })
        return sorted(output, key=lambda item: item["name"].casefold())


def project_record(project_key: str, auth: dict[str, Any]) -> dict[str, Any]:
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        record = data["projects"].get(project_key)
        if not isinstance(record, dict):
            raise CatalogError("The requested ChatGPT Project is not registered.")
        return copy.deepcopy(record)


def conversation_id_from_item(item: dict[str, Any]) -> str:
    for name in ("id", "conversation_id"):
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _timestamp(item: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = item.get(name)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def sync_conversations(
    project_key: str,
    items: Iterable[dict[str, Any]],
    auth: dict[str, Any],
) -> list[dict[str, Any]]:
    now = time.time()
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        if not isinstance(project, dict):
            raise CatalogError("The requested ChatGPT Project is not registered.")
        conversations = project.get("conversations")
        if not isinstance(conversations, dict):
            conversations = {}
        public: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            conversation_id = conversation_id_from_item(item)
            if not conversation_id:
                continue
            key = _opaque_unlocked("conversation", f"{project['project_id']}\0{conversation_id}")
            existing = conversations.get(key)
            record = existing if isinstance(existing, dict) else {}
            record.update({
                "conversation_id": conversation_id,
                "title": _clean_title(item.get("title"), "Untitled conversation"),
                "create_time": _timestamp(item, "create_time", "created_at"),
                "update_time": _timestamp(item, "update_time", "updated_at"),
                "last_seen_at": now,
            })
            conversations[key] = record
            public.append({
                "key": key,
                "title": record["title"],
                "createdAt": record.get("create_time"),
                "updatedAt": record.get("update_time"),
                "needsRefresh": bool(record.get("submission") or record.get("reconcile_message_id")),
            })
        project["conversations"] = conversations
        project["last_refreshed_at"] = now
        project["updated_at"] = now
        _save_unlocked(data)
    public.sort(key=lambda item: item.get("updatedAt") or item.get("createdAt") or 0, reverse=True)
    return public


def conversation_record(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
) -> dict[str, Any]:
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        if not isinstance(project, dict):
            raise CatalogError("The requested ChatGPT Project is not registered.")
        conversations = project.get("conversations")
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not in the registered Project cache.")
        output = copy.deepcopy(record)
        output["project_id"] = project.get("project_id")
        output["project_name"] = project.get("name")
        return output


def update_remote_state(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    state: dict[str, Any],
) -> None:
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        returned_id = state.get("conversation_id")
        if returned_id != record.get("conversation_id"):
            raise CatalogError("ChatGPT returned a different conversation while updating the cloud handle.")
        for name in ("message_id", "parent_message_id", "user_id"):
            value = state.get(name)
            if isinstance(value, str) and value:
                record[name] = value
        record["state_updated_at"] = time.time()
        _save_unlocked(data)


def begin_submission(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    nonce: str,
    *,
    prompt_sha256: str,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256):
        raise CatalogError("The cloud submission journal has an invalid prompt fingerprint.")
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        if record.get("submission"):
            raise CatalogError("This cloud conversation needs an explicit refresh before another send.")
        record["submission"] = {
            "nonce": nonce,
            "started_at": time.time(),
            "prior_message_id": record.get("message_id"),
            "prompt_sha256": prompt_sha256,
        }
        _durably_save_submission_unlocked(data)


def bind_submission_user_message(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    nonce: str,
    user_message_id: str,
) -> None:
    if (
        len(user_message_id) < 8
        or len(user_message_id) > 200
        or any(not (character.isalnum() or character in "-_") for character in user_message_id)
    ):
        raise CatalogError("The provider supplied an invalid cloud user-message identity.")
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        submission = record.get("submission") if isinstance(record, dict) else None
        if not isinstance(submission, dict) or submission.get("nonce") != nonce:
            raise CatalogError("The cloud submission journal changed before provider admission.")
        existing = submission.get("user_message_id")
        if isinstance(existing, str) and existing != user_message_id:
            raise CatalogError("The provider changed the cloud user-message identity before submission.")
        submission["user_message_id"] = user_message_id
        _durably_save_submission_unlocked(data)


def finish_submission(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    nonce: str,
) -> None:
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        submission = record.get("submission")
        if isinstance(submission, dict) and submission.get("nonce") == nonce:
            message_id = record.get("message_id")
            if isinstance(message_id, str) and message_id:
                record["reconcile_message_id"] = message_id
            record.pop("submission", None)
            record["last_completed_at"] = time.time()
            _save_unlocked(data)


def complete_submission_from_remote_state(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    nonce: str,
    state: dict[str, Any],
) -> None:
    """Atomically install proven remote state and clear the matching journal."""
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        submission = record.get("submission")
        if not isinstance(submission, dict) or submission.get("nonce") != nonce:
            raise CatalogError("The cloud submission journal changed before reconciliation completed.")
        if state.get("conversation_id") != record.get("conversation_id"):
            raise CatalogError("ChatGPT returned a different conversation during reconciliation.")
        message_id = state.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise CatalogError("ChatGPT reconciliation returned no continuation message.")

        for name in ("message_id", "parent_message_id", "user_id"):
            value = state.get(name)
            if isinstance(value, str) and value:
                record[name] = value
        now = time.time()
        record.pop("submission", None)
        record.pop("reconcile_message_id", None)
        record["last_completed_at"] = now
        record["state_updated_at"] = now
        record["submission_refreshed_at"] = now
        _durably_save_submission_unlocked(data)


def clear_submission_after_refresh(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
) -> None:
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        if "submission" in record or "reconcile_message_id" in record:
            record.pop("submission", None)
            record.pop("reconcile_message_id", None)
            record["submission_refreshed_at"] = time.time()
            _save_unlocked(data)


def adopt_current_branch(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    state: dict[str, Any],
    expected_recovery_token: str,
) -> None:
    """Archive an unresolved journal and atomically adopt the active cloud branch."""
    if not re.fullmatch(r"[0-9a-f]{32}", expected_recovery_token):
        raise CatalogError("The cloud recovery identity is invalid.")
    with catalog_lock():
        data = _load_unlocked()
        _bind_account_unlocked(data, auth)
        project = data["projects"].get(project_key)
        conversations = project.get("conversations") if isinstance(project, dict) else None
        record = conversations.get(conversation_key) if isinstance(conversations, dict) else None
        if not isinstance(record, dict):
            raise CatalogError("The requested cloud conversation is not registered.")
        if state.get("conversation_id") != record.get("conversation_id"):
            raise CatalogError("ChatGPT returned a different conversation during branch recovery.")

        submission = record.get("submission")
        reconcile_message_id = record.get("reconcile_message_id")
        if not isinstance(submission, dict) and not isinstance(reconcile_message_id, str):
            raise RecoveryStateChangedError(
                "This cloud conversation was already reconciled by another session."
            )
        current_recovery_token = recovery_journal_token(record)
        if (
            not isinstance(current_recovery_token, str)
            or not hmac.compare_digest(current_recovery_token, expected_recovery_token)
        ):
            raise RecoveryStateChangedError(
                "The cloud recovery state changed before it could be adopted."
            )

        now = time.time()
        recovery: dict[str, Any] = {
            "resolution": "adopt_current_branch",
            "resolved_at": now,
            "previous_message_id": record.get("message_id"),
        }
        if isinstance(submission, dict):
            recovery["submission"] = copy.deepcopy(submission)
        if isinstance(reconcile_message_id, str):
            recovery["reconcile_message_id"] = reconcile_message_id
        history = record.get("recovery_history")
        if not isinstance(history, list):
            history = []
        record["recovery_history"] = (history + [recovery])[-MAX_RECOVERY_HISTORY:]

        for name in ("message_id", "parent_message_id", "user_id"):
            value = state.get(name)
            if isinstance(value, str) and value:
                record[name] = value
        record.pop("submission", None)
        record.pop("reconcile_message_id", None)
        record["state_updated_at"] = now
        record["current_branch_adopted_at"] = now
        _durably_save_submission_unlocked(data)


def submission_pending(project_key: str, conversation_key: str, auth: dict[str, Any]) -> bool:
    return bool(conversation_record(project_key, conversation_key, auth).get("submission"))
