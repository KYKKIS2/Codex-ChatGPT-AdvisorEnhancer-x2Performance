#!/usr/bin/env python3
"""Run an optional loopback g4f GUI that imports bound ChatGPT Project chats."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote, urlencode, urlsplit

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, stream_with_context
from PIL import Image, ImageOps
from werkzeug.datastructures import ImmutableMultiDict

import advisor
import advisor_cloud_catalog as catalog
import advisor_concurrency as concurrency


DEFAULT_PORT = 8088
DEFAULT_IMPORT_MESSAGES = 80
DEFAULT_IMPORT_ACTIVITIES = 240
ALLOWED_PROVIDERS = {"OpenaiAccount", "OpenaiChat"}
ALLOWED_CLOUD_FIELDS = {
    "conversation",
    "messages",
    "model",
    "provider",
    "thinking_effort",
}
ALLOWED_MODEL_EFFORT = {
    ("gpt-5-6-thinking", "max"),
    ("gpt-5-6-pro", "standard"),
}
MAX_BROWSER_MESSAGE_CHARS = 200_000
MAX_BROWSER_REQUEST_BYTES = 240_000
MAX_IMAGE_UPLOAD_COUNT = 4
MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MULTIPART_REQUEST_BYTES = MAX_TOTAL_IMAGE_BYTES + MAX_BROWSER_REQUEST_BYTES + 256 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_TOTAL_IMAGE_PIXELS = 60_000_000
MAX_ACTIVITY_CHARS = 1_000
MAX_LIVE_ACTIVITIES = 100
MAX_IMPORTED_IMAGE_COUNT = 100
ALLOWED_IMAGE_FORMATS = {
    "GIF": ("image/gif", "gif"),
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
ALLOWED_DECLARED_IMAGE_TYPES = {
    "image/gif",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}
ALLOWED_REASONING_EVENT_FIELDS = {"type", "token", "status"}
LOCAL_GUI_PATHS = {
    "/",
    "/advisor-cloud.css",
    "/advisor-cloud.js",
    "/backend-api/v2/conversation",
    "/chat/",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "media-src 'none'; "
    "worker-src 'none'; "
    "manifest-src 'none'"
)
SENSITIVE_EVENT_KEYS = {
    "conversation_id",
    "gizmo_id",
    "message_id",
    "parent_message_id",
    "user_id",
}


class GuiBridgeError(RuntimeError):
    """Safe, user-facing bridge failure without private identifiers."""


class GuiRemoteTurnRunning(GuiBridgeError):
    """The cloud turn is still active and must be observed, not resubmitted."""


class GuiCloudHistoryPending(GuiBridgeError):
    """The cloud turn ended but its conversation graph is not settled yet."""


class GuiCloudHistoryUnresolved(GuiBridgeError):
    """A settled cloud graph cannot satisfy the durable reconciliation journal."""


class GuiRemoteStatusUnavailable(GuiBridgeError):
    """The cloud stream status is temporarily unavailable."""


class GuiRemoteReadUnavailable(GuiBridgeError):
    """A read-only cloud history request failed transiently."""


class GuiRequestTooLarge(GuiBridgeError):
    """A browser request exceeded the local GUI's bounded upload limits."""


class GuiActivityRateLimited(GuiBridgeError):
    """A nonessential live-activity read should cool down without retries."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Live activity is temporarily rate limited.")
        self.retry_after = max(60.0, float(retry_after or 0.0))


def require_auth() -> dict[str, Any]:
    auth = advisor.load_chatgpt_auth()
    if not auth:
        raise GuiBridgeError("ChatGPT HAR/auth is unavailable or expired.")
    catalog.account_identity(auth)
    return auth


def _bounded_timeout() -> int:
    raw = os.environ.get("ADVISOR_GUI_REMOTE_TIMEOUT", "60")
    try:
        return max(5, min(300, int(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_REMOTE_TIMEOUT must be an integer.") from exc


def _queue_timeout() -> float | None:
    raw = os.environ.get("ADVISOR_GUI_QUEUE_TIMEOUT", "0")
    try:
        value = float(raw)
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_QUEUE_TIMEOUT must be a number.") from exc
    return None if value <= 0 else value


def _reconcile_attempts() -> int:
    raw = os.environ.get("ADVISOR_GUI_RECONCILE_ATTEMPTS", "30")
    try:
        return max(1, min(120, int(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_RECONCILE_ATTEMPTS must be an integer.") from exc


def _reconcile_interval() -> float:
    raw = os.environ.get("ADVISOR_GUI_RECONCILE_INTERVAL", "1")
    try:
        return max(0.1, min(5.0, float(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_RECONCILE_INTERVAL must be a number.") from exc


def _recovery_attempts() -> int:
    raw = os.environ.get("ADVISOR_GUI_RECOVERY_ATTEMPTS", "240")
    try:
        return max(1, min(720, int(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_RECOVERY_ATTEMPTS must be an integer.") from exc


def _recovery_interval() -> float:
    raw = os.environ.get("ADVISOR_GUI_RECOVERY_INTERVAL", "5")
    try:
        return max(0.1, min(30.0, float(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_RECOVERY_INTERVAL must be a number.") from exc


def _unresolved_after_seconds() -> float:
    raw = os.environ.get("ADVISOR_GUI_UNRESOLVED_AFTER_SECONDS", "60")
    try:
        return max(15.0, min(3600.0, float(raw)))
    except ValueError as exc:
        raise GuiBridgeError("ADVISOR_GUI_UNRESOLVED_AFTER_SECONDS must be a number.") from exc


def list_remote_project_conversations(
    project_id: str,
    auth: dict[str, Any],
    *,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    timeout = _bounded_timeout()
    max_pages = max(1, min(20, max_pages))
    for _page in range(max_pages):
        query: dict[str, str | int] = {"limit": 50, "owned_only": "true"}
        if cursor:
            query["cursor"] = cursor
        url = f"https://chatgpt.com/backend-api/gizmos/{project_id}/conversations?{urlencode(query)}"
        try:
            payload = advisor.get_remote_json_with_backoff(
                url,
                auth["headers"],
                timeout,
                operation="advisor GUI Project conversation listing",
            )
        except RuntimeError as exc:
            raise GuiBridgeError("Could not refresh conversations from the registered ChatGPT Project.") from exc
        page_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(page_items, list):
            raise GuiBridgeError("ChatGPT returned an unexpected Project conversation list.")
        items.extend(item for item in page_items if isinstance(item, dict))
        next_cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return items


def _validated_remote_conversation(
    data: Any,
    conversation_id: str,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GuiBridgeError("ChatGPT returned an unexpected conversation payload.")
    returned_id = data.get("conversation_id") or data.get("id")
    if isinstance(returned_id, str) and returned_id != conversation_id:
        raise GuiBridgeError("ChatGPT returned a different conversation than requested.")
    return data


def fetch_remote_conversation(conversation_id: str, auth: dict[str, Any]) -> dict[str, Any]:
    try:
        data = advisor.get_remote_json_with_backoff(
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}",
            auth["headers"],
            _bounded_timeout(),
            operation="advisor GUI cloud conversation fetch",
        )
    except advisor.RateLimitError:
        raise
    except RuntimeError as exc:
        raise GuiRemoteReadUnavailable(
            "The selected ChatGPT conversation is temporarily unavailable."
        ) from exc
    return _validated_remote_conversation(data, conversation_id)


def fetch_remote_conversation_once(
    conversation_id: str,
    auth: dict[str, Any],
) -> dict[str, Any]:
    """Fetch optional live activity once; a 429 must not start a retry loop."""
    try:
        data = advisor.get_json(
            f"https://chatgpt.com/backend-api/conversation/{conversation_id}",
            auth["headers"],
            _bounded_timeout(),
        )
    except advisor.RateLimitError as exc:
        concurrency.record_remote_rate_limit(exc.retry_after)
        raise GuiActivityRateLimited(exc.retry_after) from exc
    except RuntimeError as exc:
        raise GuiBridgeError("Could not refresh live ChatGPT activity.") from exc
    return _validated_remote_conversation(data, conversation_id)


def _active_branch_message(data: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    """Find a message on the selected root-to-current-node lineage."""
    for node in advisor.ordered_nodes(data):
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        candidate_id = message.get("id") or node.get("id")
        if candidate_id == message_id:
            return message
    return None


def _active_branch_messages_after_id(
    data: dict[str, Any],
    message_id: str,
) -> list[dict[str, Any]]:
    """Return correlation fields after any active-branch node, including tools."""
    found_parent = False
    messages: list[dict[str, Any]] = []
    for node in advisor.ordered_nodes(data):
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        candidate_id = message.get("id") or node.get("id")
        if not found_parent:
            found_parent = candidate_id == message_id
            continue
        role = (message.get("author") or {}).get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        item: dict[str, Any] = {
            "id": candidate_id,
            "role": role,
            "status": message.get("status"),
            "content": advisor.message_text(message).strip(),
        }
        if "end_turn" in message:
            item["end_turn"] = message.get("end_turn")
        messages.append(item)
    return messages if found_parent else []


def _conversation_contains_reconciled_message(
    data: dict[str, Any],
    message_id: str,
) -> bool:
    message = _active_branch_message(data, message_id)
    if not isinstance(message, dict):
        return False
    role = (message.get("author") or {}).get("role")
    if role == "assistant":
        item: dict[str, Any] = {
            "role": role,
            "status": message.get("status"),
            "content": advisor.message_text(message).strip(),
        }
        if "end_turn" in message:
            item["end_turn"] = message.get("end_turn")
        return bool(item["content"] and advisor.assistant_item_has_final_content(item))
    if role != "tool":
        return False
    current_id = advisor.latest_message_id(
        data,
        advisor.transcript_from_conversation(data),
    )
    return current_id == message_id


def fetch_reconciled_conversation(
    conversation_id: str,
    auth: dict[str, Any],
    expected_message_id: str | None,
    *,
    attempts: int | None = None,
    interval: float | None = None,
) -> dict[str, Any]:
    if not expected_message_id:
        return fetch_remote_conversation(conversation_id, auth)
    max_attempts = attempts if attempts is not None else _reconcile_attempts()
    wait_seconds = interval if interval is not None else _reconcile_interval()
    for attempt in range(max(1, max_attempts)):
        data = fetch_remote_conversation(conversation_id, auth)
        if _conversation_contains_reconciled_message(data, expected_message_id):
            return data
        if attempt + 1 < max_attempts:
            time.sleep(max(0, wait_seconds))
    raise GuiCloudHistoryPending(
        "ChatGPT finished streaming, but its cloud history has not caught up yet. Refresh this chat again."
    )


def fetch_ambiguous_submission_result(
    conversation_id: str,
    auth: dict[str, Any],
    prior_message_id: str | None,
    prompt_sha256: str | None,
    user_message_id: str | None = None,
    *,
    attempts: int | None = None,
    interval: float | None = None,
) -> dict[str, Any]:
    """Reconcile an accepted-or-unknown POST using idempotent cloud reads only."""
    if (
        not isinstance(prior_message_id, str)
        or not prior_message_id
        or not isinstance(prompt_sha256, str)
        or len(prompt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in prompt_sha256)
    ):
        raise GuiBridgeError(
            "The interrupted cloud turn lacks correlation evidence. No prompt was resent."
        )
    max_attempts = attempts if attempts is not None else _recovery_attempts()
    wait_seconds = interval if interval is not None else _recovery_interval()
    last_status: str | None = None
    for attempt in range(max(1, max_attempts)):
        status = advisor.remote_conversation_stream_status(
            conversation_id,
            auth,
            _bounded_timeout(),
        )
        last_status = status
        if advisor.remote_conversation_is_complete(status):
            data = fetch_remote_conversation(conversation_id, auth)
            messages = _active_branch_messages_after_id(data, prior_message_id)
            first_user_index = next(
                (index for index, item in enumerate(messages) if item.get("role") == "user"),
                -1,
            )
            correlated_message_id = ""
            exact_user_was_accepted = False
            if first_user_index >= 0:
                first_user = messages[first_user_index]
                prompt = str(messages[first_user_index].get("content") or "").strip()
                actual_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                identity_matches = (
                    not user_message_id
                    or first_user.get("id") == user_message_id
                )
                if identity_matches and secrets.compare_digest(actual_sha256, prompt_sha256):
                    exact_user_was_accepted = bool(user_message_id)
                    for item in messages[first_user_index + 1:]:
                        if item.get("role") == "user":
                            break
                        if (
                            item.get("role") == "assistant"
                            and advisor.assistant_item_has_final_content(item)
                            and isinstance(item.get("id"), str)
                            and str(item.get("content") or "").strip()
                        ):
                            correlated_message_id = item["id"]
            if exact_user_was_accepted or correlated_message_id:
                return data
        if attempt + 1 < max_attempts:
            time.sleep(max(0, wait_seconds))
    if advisor.remote_conversation_is_streaming(last_status):
        raise GuiRemoteTurnRunning(
            "The selected ChatGPT conversation is still running."
        )
    if not advisor.remote_conversation_is_complete(last_status):
        raise GuiRemoteStatusUnavailable(
            "ChatGPT conversation status is temporarily unavailable."
        )
    raise GuiCloudHistoryPending(
        "The interrupted cloud turn could not yet be proven complete. No prompt was resent; retry Refresh."
    )


def require_complete_conversation(conversation_id: str, auth: dict[str, Any]) -> None:
    status = advisor.remote_conversation_stream_status(
        conversation_id,
        auth,
        _bounded_timeout(),
    )
    if advisor.remote_conversation_is_streaming(status):
        raise GuiRemoteTurnRunning("The selected ChatGPT conversation is still running.")
    if not advisor.remote_conversation_is_complete(status):
        raise GuiRemoteStatusUnavailable(
            "ChatGPT conversation status is temporarily unavailable; continuing is blocked to avoid a duplicate turn."
        )


def _finish_submission_if_remote_complete(
    project_key: str,
    conversation_key: str,
    conversation_id: str,
    auth: dict[str, Any],
    nonce: str,
) -> bool:
    """Resolve the exact journal from completed cloud history without replaying it."""
    try:
        record = catalog.conversation_record(project_key, conversation_key, auth)
        submission = record.get("submission")
        if not isinstance(submission, dict) or submission.get("nonce") != nonce:
            return False
        prior_message_id = submission.get("prior_message_id")
        prompt_sha256 = submission.get("prompt_sha256")
        user_message_id = submission.get("user_message_id")
        data = fetch_ambiguous_submission_result(
            conversation_id,
            auth,
            prior_message_id if isinstance(prior_message_id, str) else None,
            prompt_sha256 if isinstance(prompt_sha256, str) else None,
            user_message_id if isinstance(user_message_id, str) else None,
            attempts=1,
            interval=0,
        )
        state = remote_state_from_data(conversation_id, data, auth)
        catalog.complete_submission_from_remote_state(
            project_key,
            conversation_key,
            auth,
            nonce,
            state,
        )
    except Exception:
        return False
    return True


def remote_state_from_data(
    conversation_id: str,
    data: dict[str, Any],
    auth: dict[str, Any],
) -> dict[str, Any]:
    transcript = advisor.transcript_from_conversation(data)
    message_id = advisor.latest_message_id(data, transcript)
    if not isinstance(message_id, str) or not message_id:
        raise GuiBridgeError("The cloud conversation has no current branch node to continue from.")
    current_message = _active_branch_message(data, message_id)
    current_role = (
        (current_message.get("author") or {}).get("role")
        if isinstance(current_message, dict)
        else None
    )
    if current_role not in {"assistant", "tool"}:
        raise GuiBridgeError("The cloud conversation has no safe current continuation node.")
    state = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "parent_message_id": message_id,
        "finish_reason": None,
        "recipient": "all",
        "is_thinking": False,
        "p": None,
        "thoughts_summary": "",
        "prompt": None,
        "generated_images": None,
        "task": None,
    }
    user_id = auth.get("user_id")
    if isinstance(user_id, str) and user_id:
        state["user_id"] = user_id
    return state


def _message_is_visually_hidden(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and bool(
        metadata.get("is_visually_hidden_from_conversation")
        or metadata.get("is_visually_hidden_from_conversation_history")
    )


def _bounded_activity_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split()).strip()
    if not text:
        return ""
    if len(text) <= MAX_ACTIVITY_CHARS:
        return text
    return text[: MAX_ACTIVITY_CHARS - 1].rstrip() + "\u2026"


def _activity_summaries(message: dict[str, Any]) -> list[str]:
    author = message.get("author")
    content = message.get("content")
    metadata = message.get("metadata")
    if (
        not isinstance(author, dict)
        or author.get("role") != "assistant"
        or not isinstance(content, dict)
        or content.get("content_type") != "thoughts"
        or not isinstance(metadata, dict)
        or not any(
            isinstance(metadata.get(name), str) and metadata.get(name)
            for name in ("summary_type", "tool_summary_type")
        )
    ):
        return []
    thoughts = content.get("thoughts")
    if not isinstance(thoughts, list):
        return []
    summaries: list[str] = []
    for thought in thoughts:
        if not isinstance(thought, dict):
            continue
        summary = _bounded_activity_text(thought.get("summary"))
        if summary and (not summaries or summaries[-1] != summary):
            summaries.append(summary)
    return summaries


def _image_attachment_count(message: dict[str, Any]) -> int:
    content = message.get("content")
    content_parts = content.get("parts") if isinstance(content, dict) else None
    pointer_count = (
        sum(
            1
            for part in content_parts
            if isinstance(part, dict)
            and isinstance(part.get("asset_pointer"), str)
            and part["asset_pointer"].startswith(("file-service://", "sediment://"))
        )
        if isinstance(content_parts, list)
        else 0
    )
    metadata = message.get("metadata")
    attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
    metadata_count = (
        sum(
            1
            for attachment in attachments
            if isinstance(attachment, dict)
            and str(attachment.get("mimeType") or attachment.get("mime_type") or "").startswith("image/")
        )
        if isinstance(attachments, list)
        else 0
    )
    return min(MAX_IMPORTED_IMAGE_COUNT, max(pointer_count, metadata_count))


def _limit_visible_items(
    items: list[dict[str, Any]],
    *,
    message_limit: int,
    activity_limit: int = DEFAULT_IMPORT_ACTIVITIES,
) -> list[dict[str, Any]]:
    message_indexes = [index for index, item in enumerate(items) if item.get("kind") != "activity"]
    if len(message_indexes) > message_limit:
        items = items[message_indexes[-message_limit]:]
    excess = sum(item.get("kind") == "activity" for item in items) - max(0, activity_limit)
    if excess <= 0:
        return items
    limited: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") == "activity" and excess:
            excess -= 1
            continue
        limited.append(item)
    return limited


def visible_messages(
    data: dict[str, Any],
    limit: int = DEFAULT_IMPORT_MESSAGES,
    *,
    include_incomplete_assistant: bool = False,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for node in advisor.ordered_nodes(data):
        message = node.get("message") if isinstance(node, dict) else None
        if not isinstance(message, dict) or _message_is_visually_hidden(message):
            continue

        for summary in _activity_summaries(message):
            messages.append({
                "role": "assistant",
                "kind": "activity",
                "content": summary,
            })

        role = (message.get("author") or {}).get("role")
        if role not in {"user", "assistant"}:
            continue
        content = advisor.message_text(message).strip()
        if not content:
            continue
        item: dict[str, Any] = {
            "role": role,
            "status": message.get("status"),
            "content": content,
        }
        if "end_turn" in message:
            item["end_turn"] = message.get("end_turn")
        if role == "assistant" and not advisor.assistant_item_has_final_content(item):
            message_content = message.get("content")
            content_type = (
                message_content.get("content_type")
                if isinstance(message_content, dict)
                else None
            )
            if not include_incomplete_assistant or content_type not in {"text", "multimodal_text"}:
                continue
        browser_message: dict[str, Any] = {"role": role, "content": content}
        if role == "user":
            image_count = _image_attachment_count(message)
            if image_count:
                browser_message["imageCount"] = image_count
        if role == "assistant":
            browser_message["provider"] = {
                "name": "OpenaiAccount",
                "label": "ChatGPT Cloud",
            }
        messages.append(browser_message)
    return _limit_visible_items(messages, message_limit=max(2, limit))


def live_conversation_activities(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    submission_nonce: str,
) -> list[str]:
    """Return only user-visible summaries from the currently submitted turn."""
    record = catalog.conversation_record(project_key, conversation_key, auth)
    submission = record.get("submission")
    conversation_id = record.get("conversation_id")
    prior_message_id = submission.get("prior_message_id") if isinstance(submission, dict) else None
    if (
        not isinstance(submission, dict)
        or submission.get("nonce") != submission_nonce
        or not isinstance(conversation_id, str)
        or not isinstance(prior_message_id, str)
    ):
        return []

    data = fetch_remote_conversation_once(conversation_id, auth)
    found_parent = False
    summaries: list[str] = []
    for node in advisor.ordered_nodes(data):
        message = node.get("message") if isinstance(node, dict) else None
        message_id = (
            message.get("id")
            if isinstance(message, dict)
            else node.get("id") if isinstance(node, dict) else None
        )
        if not found_parent:
            if message_id == prior_message_id:
                found_parent = True
            continue
        if isinstance(message, dict) and not _message_is_visually_hidden(message):
            summaries.extend(_activity_summaries(message))
    return summaries[-MAX_LIVE_ACTIVITIES:] if found_parent else []


def _to_milliseconds(value: Any, default: int) -> int:
    if not isinstance(value, (int, float)):
        return default
    number = float(value)
    return int(number if number >= 1_000_000_000_000 else number * 1000)


def browser_conversation(
    project_key: str,
    conversation_key: str,
    record: dict[str, Any],
    data: dict[str, Any],
    *,
    limit: int = DEFAULT_IMPORT_MESSAGES,
    include_incomplete_assistant: bool = False,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    opaque_state = {
        "advisor_cloud_project": project_key,
        "advisor_cloud_handle": conversation_key,
    }
    items = visible_messages(
        data,
        limit,
        include_incomplete_assistant=include_incomplete_assistant,
    )
    if not items:
        raise GuiBridgeError("The selected cloud conversation has no visible messages to import.")
    current_message_id = advisor.latest_message_id(
        data,
        advisor.transcript_from_conversation(data),
    )
    current_message = (
        _active_branch_message(data, current_message_id)
        if isinstance(current_message_id, str)
        else None
    )
    current_role = (
        (current_message.get("author") or {}).get("role")
        if isinstance(current_message, dict)
        else None
    )
    advisor_cloud = {
        "project": project_key,
        "conversation": conversation_key,
        "syncedAt": now_ms,
        "continuationFromTool": current_role == "tool",
    }
    recovery_token = catalog.recovery_journal_token(record)
    if isinstance(recovery_token, str):
        advisor_cloud["recoveryToken"] = recovery_token
    return {
        "id": f"advisor-cloud-{project_key}-{conversation_key}",
        "title": str(record.get("title") or "ChatGPT Cloud conversation")[:300],
        "added": _to_milliseconds(record.get("create_time"), now_ms),
        "updated": _to_milliseconds(record.get("update_time"), now_ms),
        "system": "",
        "items": items,
        "data": {
            "OpenaiAccount": dict(opaque_state),
            "OpenaiChat": dict(opaque_state),
        },
        "advisorCloud": advisor_cloud,
    }


def refresh_project(project_key: str, auth: dict[str, Any]) -> list[dict[str, Any]]:
    project = catalog.project_record(project_key, auth)
    project_id = project.get("project_id")
    if not isinstance(project_id, str):
        raise GuiBridgeError("The registered Project mapping is invalid.")
    items = list_remote_project_conversations(project_id, auth)
    return catalog.sync_conversations(project_key, items, auth)


def _conversation_is_still_in_project(
    project_key: str,
    conversation_id: str,
    auth: dict[str, Any],
) -> None:
    project = catalog.project_record(project_key, auth)
    project_id = project.get("project_id")
    if not isinstance(project_id, str):
        raise GuiBridgeError("The registered Project mapping is invalid.")
    items = list_remote_project_conversations(project_id, auth)
    catalog.sync_conversations(project_key, items, auth)
    if conversation_id not in {catalog.conversation_id_from_item(item) for item in items}:
        raise GuiBridgeError("The selected conversation is no longer in the registered ChatGPT Project.")


def _pending_journal_started_at(record: dict[str, Any]) -> float | None:
    submission = record.get("submission")
    value = submission.get("started_at") if isinstance(submission, dict) else record.get("last_completed_at")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _history_pending_is_unresolved(record: dict[str, Any]) -> bool:
    started_at = _pending_journal_started_at(record)
    return bool(
        started_at is not None
        and time.time() - started_at >= _unresolved_after_seconds()
    )


def _require_current_recovery_token(
    record: dict[str, Any],
    expected_recovery_token: str,
) -> None:
    if (
        len(expected_recovery_token) != 32
        or any(character not in "0123456789abcdef" for character in expected_recovery_token)
    ):
        raise GuiBridgeError("The cloud recovery identity is invalid.")
    current_recovery_token = catalog.recovery_journal_token(record)
    if (
        not isinstance(current_recovery_token, str)
        or not secrets.compare_digest(current_recovery_token, expected_recovery_token)
    ):
        raise catalog.RecoveryStateChangedError(
            "The cloud recovery state changed; refresh before choosing a branch."
        )


def import_conversation(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    *,
    wait: bool = True,
) -> dict[str, Any]:
    record = catalog.conversation_record(project_key, conversation_key, auth)
    conversation_id = record.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise GuiBridgeError("The private cloud conversation mapping is invalid.")
    _conversation_is_still_in_project(project_key, conversation_id, auth)
    lease = concurrency.ConversationLockLease(timeout=_queue_timeout(), locks=[], keys=set())
    recovered_submission = False
    try:
        lease.acquire_key("conversation:" + conversation_id)
        record = catalog.conversation_record(project_key, conversation_key, auth)
        submission = record.get("submission")
        reconcile_message_id = record.get("reconcile_message_id")
        if not isinstance(reconcile_message_id, str):
            reconcile_message_id = None
        try:
            if isinstance(submission, dict):
                recovered_submission = True
                prior_message_id = submission.get("prior_message_id")
                if not isinstance(prior_message_id, str):
                    prior_message_id = None
                prompt_sha256 = submission.get("prompt_sha256")
                if not isinstance(prompt_sha256, str):
                    prompt_sha256 = None
                user_message_id = submission.get("user_message_id")
                if not isinstance(user_message_id, str):
                    user_message_id = None
                data = fetch_ambiguous_submission_result(
                    conversation_id,
                    auth,
                    prior_message_id,
                    prompt_sha256,
                    user_message_id,
                    attempts=None if wait else 1,
                    interval=None if wait else 0,
                )
            else:
                require_complete_conversation(conversation_id, auth)
                data = fetch_reconciled_conversation(
                    conversation_id,
                    auth,
                    reconcile_message_id,
                    attempts=None if wait else 1,
                    interval=None if wait else 0,
                )
        except GuiCloudHistoryPending as exc:
            if _history_pending_is_unresolved(record):
                raise GuiCloudHistoryUnresolved(
                    "ChatGPT finished, but the interrupted local send cannot be matched to the current cloud branch."
                ) from exc
            raise
        state = remote_state_from_data(conversation_id, data, auth)
        catalog.update_remote_state(project_key, conversation_key, auth, state)
        # Import/Refresh is the reconciliation step after an interrupted
        # browser stream. It never submits a remote turn.
        catalog.clear_submission_after_refresh(project_key, conversation_key, auth)
    finally:
        lease.release()
    refreshed = catalog.conversation_record(project_key, conversation_key, auth)
    result = browser_conversation(project_key, conversation_key, refreshed, data)
    result["recoveredSubmission"] = recovered_submission
    return result


def adopt_current_cloud_branch(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    expected_recovery_token: str,
) -> dict[str, Any]:
    """Explicitly archive unresolved state and continue from ChatGPT's active branch."""
    record = catalog.conversation_record(project_key, conversation_key, auth)
    if not (record.get("submission") or record.get("reconcile_message_id")):
        raise catalog.RecoveryStateChangedError(
            "This cloud conversation no longer needs branch recovery."
        )
    _require_current_recovery_token(record, expected_recovery_token)
    if not _history_pending_is_unresolved(record):
        raise GuiCloudHistoryPending("Cloud reconciliation is still within its automatic retry window.")
    conversation_id = record.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise GuiBridgeError("The private cloud conversation mapping is invalid.")

    lease = concurrency.ConversationLockLease(timeout=_queue_timeout(), locks=[], keys=set())
    try:
        lease.acquire_key("conversation:" + conversation_id)
        record = catalog.conversation_record(project_key, conversation_key, auth)
        if not (record.get("submission") or record.get("reconcile_message_id")):
            raise catalog.RecoveryStateChangedError(
                "This cloud conversation was reconciled by another session."
            )
        _require_current_recovery_token(record, expected_recovery_token)
        if not _history_pending_is_unresolved(record):
            raise catalog.RecoveryStateChangedError(
                "The cloud recovery state changed; refresh before choosing a branch."
            )
        _conversation_is_still_in_project(project_key, conversation_id, auth)
        require_complete_conversation(conversation_id, auth)
        data = fetch_remote_conversation(conversation_id, auth)
        state = remote_state_from_data(conversation_id, data, auth)
        catalog.adopt_current_branch(
            project_key,
            conversation_key,
            auth,
            state,
            expected_recovery_token,
        )
    finally:
        lease.release()

    refreshed = catalog.conversation_record(project_key, conversation_key, auth)
    result = browser_conversation(project_key, conversation_key, refreshed, data)
    result["adoptedCurrentBranch"] = True
    return result


def chatgpt_conversation_url(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
) -> str:
    record = catalog.conversation_record(project_key, conversation_key, auth)
    conversation_id = record.get("conversation_id")
    if (
        not isinstance(conversation_id, str)
        or len(conversation_id) < 8
        or len(conversation_id) > 200
        or any(not (character.isalnum() or character in "-_") for character in conversation_id)
    ):
        raise GuiBridgeError("The private cloud conversation mapping is invalid.")
    return "https://chatgpt.com/c/" + quote(conversation_id, safe="")


def observe_pending_conversation(
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
) -> dict[str, Any]:
    """Read an active cloud turn without clearing or advancing local state."""
    record = catalog.conversation_record(project_key, conversation_key, auth)
    pending_journal = bool(record.get("submission") or record.get("reconcile_message_id"))
    conversation_id = record.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise GuiBridgeError("The private cloud conversation mapping is invalid.")
    status = advisor.remote_conversation_stream_status(
        conversation_id,
        auth,
        _bounded_timeout(),
    )
    if advisor.remote_conversation_is_complete(status):
        public_status = "complete" if pending_journal else "ready"
    elif advisor.remote_conversation_is_streaming(status):
        public_status = "streaming"
    else:
        public_status = "unknown"
    data = fetch_remote_conversation(conversation_id, auth)
    return {
        "status": public_status,
        "conversation": browser_conversation(
            project_key,
            conversation_key,
            record,
            data,
            include_incomplete_assistant=True,
        ),
    }


def _loopback_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _host_is_loopback(value: str) -> bool:
    hostname = urlsplit("//" + value).hostname
    return _loopback_address(hostname)


def _same_origin_request() -> bool:
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == request.host and _host_is_loopback(parsed.netloc)
    fetch_site = request.headers.get("Sec-Fetch-Site")
    return fetch_site in {None, "none", "same-origin"}


def _error_response(message: str, status: int, code: str | None = None) -> tuple[Response, int]:
    error = {"message": message}
    if code:
        error["code"] = code
    return jsonify({"error": error}), status


def _safe_route(callable_: Any) -> Any:
    try:
        return callable_()
    except GuiActivityRateLimited as exc:
        response, status = _error_response(str(exc), 429, "activity_rate_limited")
        response.headers["Retry-After"] = str(int(exc.retry_after))
        return response, status
    except advisor.RateLimitError as exc:
        retry_after = max(60.0, float(exc.retry_after or 0.0))
        concurrency.record_remote_rate_limit(retry_after)
        response, status = _error_response(
            "ChatGPT cloud reads are temporarily rate limited.",
            429,
            "remote_read_rate_limited",
        )
        response.headers["Retry-After"] = str(int(retry_after))
        return response, status
    except GuiRemoteTurnRunning as exc:
        return _error_response(str(exc), 409, "remote_turn_running")
    except GuiCloudHistoryUnresolved as exc:
        return _error_response(str(exc), 409, "cloud_history_unresolved")
    except GuiCloudHistoryPending as exc:
        return _error_response(str(exc), 409, "cloud_history_pending")
    except GuiRemoteStatusUnavailable as exc:
        return _error_response(str(exc), 409, "remote_status_unavailable")
    except GuiRemoteReadUnavailable as exc:
        return _error_response(str(exc), 503, "remote_read_unavailable")
    except catalog.RecoveryStateChangedError as exc:
        return _error_response(str(exc), 409, "recovery_state_changed")
    except catalog.AccountMismatchError as exc:
        return _error_response(str(exc), 409)
    except (catalog.CatalogError, GuiBridgeError) as exc:
        return _error_response(str(exc), 409)
    except Exception:
        return _error_response("The advisor cloud bridge failed without exposing private state.", 500)


def _extract_cloud_handle(body: dict[str, Any]) -> tuple[str, str, str] | None:
    provider = body.get("provider")
    if provider not in ALLOWED_PROVIDERS:
        return None
    state = body.get("conversation")
    if not isinstance(state, dict):
        return None
    project_key = state.get("advisor_cloud_project")
    conversation_key = state.get("advisor_cloud_handle")
    if not isinstance(project_key, str) or not isinstance(conversation_key, str):
        return None
    if not _is_opaque_handle(project_key) or not _is_opaque_handle(conversation_key):
        raise GuiBridgeError("The browser supplied an invalid advisor cloud handle.")
    if set(state) != {"advisor_cloud_project", "advisor_cloud_handle"}:
        raise GuiBridgeError("The browser supplied unexpected cloud conversation state.")
    return provider, project_key, conversation_key


def _is_opaque_handle(value: str) -> bool:
    if len(value) != 32:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_cloud_turn_body(body: dict[str, Any]) -> tuple[str, str, str]:
    unexpected = set(body) - ALLOWED_CLOUD_FIELDS
    if unexpected:
        raise GuiBridgeError("The browser supplied unsupported cloud turn fields.")
    handle = _extract_cloud_handle(body)
    if handle is None:
        raise GuiBridgeError("The browser supplied no valid advisor cloud conversation.")
    model_effort = (body.get("model"), body.get("thinking_effort"))
    if model_effort not in ALLOWED_MODEL_EFFORT:
        raise GuiBridgeError("The browser supplied an unsupported ChatGPT mode.")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise GuiBridgeError("Each cloud turn must contain exactly one user message.")
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"role", "content"}:
        raise GuiBridgeError("The browser supplied an invalid cloud message.")
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, str) or not content.strip():
        raise GuiBridgeError("Each cloud turn must contain one non-empty user message.")
    if len(content) > MAX_BROWSER_MESSAGE_CHARS:
        raise GuiBridgeError("The cloud message is too large.")
    return handle


def _canonical_image_bytes(data: bytes, image_format: str) -> bytes:
    try:
        with Image.open(BytesIO(data)) as source:
            transposed = ImageOps.exif_transpose(source)
            has_alpha = "A" in transposed.getbands() or "transparency" in source.info
            canonical = transposed.convert("RGBA" if has_alpha and image_format != "JPEG" else "RGB")
            try:
                output = BytesIO()
                if image_format == "JPEG":
                    canonical.save(output, format="JPEG", quality=95)
                elif image_format == "PNG":
                    canonical.save(output, format="PNG", compress_level=6)
                elif image_format == "WEBP":
                    canonical.save(output, format="WEBP", lossless=True, method=2)
                elif image_format == "GIF":
                    canonical.save(output, format="GIF")
                else:
                    raise GuiBridgeError("Only JPEG, PNG, WebP, and GIF images are supported.")
                return output.getvalue()
            finally:
                canonical.close()
                if transposed is not source:
                    transposed.close()
    except GuiBridgeError:
        raise
    except Exception as exc:
        raise GuiBridgeError("An attached image could not be safely normalized.") from exc


def _validate_image_uploads() -> int:
    unexpected_fields = set(request.form) - {"json"}
    unexpected_files = set(request.files) - {"files"}
    if unexpected_fields or unexpected_files:
        raise GuiBridgeError("The browser supplied unsupported multipart fields.")

    files = request.files.getlist("files")
    if not files or len(files) > MAX_IMAGE_UPLOAD_COUNT:
        raise GuiBridgeError(f"Attach between one and {MAX_IMAGE_UPLOAD_COUNT} images.")

    total_bytes = 0
    total_pixels = 0
    for index, file in enumerate(files, start=1):
        declared_type = str(file.mimetype or "").lower()
        if declared_type not in ALLOWED_DECLARED_IMAGE_TYPES:
            raise GuiBridgeError("Only JPEG, PNG, WebP, and GIF images are supported.")

        data = file.stream.read(MAX_IMAGE_UPLOAD_BYTES + 1)
        if len(data) > MAX_IMAGE_UPLOAD_BYTES:
            raise GuiRequestTooLarge(
                f"Each image must be at most {MAX_IMAGE_UPLOAD_BYTES // (1024 * 1024)} MiB."
            )
        if not data:
            raise GuiBridgeError("Empty image uploads are not supported.")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise GuiRequestTooLarge(
                f"Attached images must total at most {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MiB."
            )

        try:
            with Image.open(BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise GuiBridgeError("An attached image has unsupported dimensions.")
                if int(getattr(image, "n_frames", 1) or 1) != 1:
                    raise GuiBridgeError("Animated image uploads are not supported.")
                total_pixels += width * height
                if total_pixels > MAX_TOTAL_IMAGE_PIXELS:
                    raise GuiBridgeError("The attached images have too many total decoded pixels.")
                image.verify()
        except GuiBridgeError:
            raise
        except Exception as exc:
            raise GuiBridgeError("An attached file is not a valid supported image.") from exc

        detected = ALLOWED_IMAGE_FORMATS.get(image_format)
        if detected is None:
            raise GuiBridgeError("Only JPEG, PNG, WebP, and GIF images are supported.")
        detected_type, extension = detected
        normalized_declared_type = "image/jpeg" if declared_type == "image/jpg" else declared_type
        if normalized_declared_type != detected_type:
            raise GuiBridgeError("An attached image's declared type does not match its contents.")

        canonical_data = _canonical_image_bytes(data, image_format)
        if len(canonical_data) > MAX_IMAGE_UPLOAD_BYTES:
            raise GuiRequestTooLarge(
                f"Each normalized image must be at most {MAX_IMAGE_UPLOAD_BYTES // (1024 * 1024)} MiB."
            )
        total_bytes += len(canonical_data) - len(data)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise GuiRequestTooLarge(
                f"Normalized images must total at most {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MiB."
            )

        # The cloud provider needs an extension, not the user's local filename.
        file.filename = f"image-{index}.{extension}"
        file.headers["Content-Type"] = detected_type
        original_stream = file.stream
        file.stream = BytesIO(canonical_data)
        original_stream.close()

    return len(files)


def _replace_request_body(body: dict[str, Any]) -> None:
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if request.mimetype == "multipart/form-data":
        request.form = ImmutableMultiDict({"json": encoded.decode("utf-8")})
        return
    request._cached_data = encoded  # type: ignore[attr-defined]
    request.environ["CONTENT_LENGTH"] = str(len(encoded))


def _scrub_value(value: Any, sensitive_values: Iterable[str]) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            output[key] = "[managed by advisor]" if key in SENSITIVE_EVENT_KEYS else _scrub_value(item, sensitive_values)
        return output
    if isinstance(value, list):
        return [_scrub_value(item, sensitive_values) for item in value]
    if isinstance(value, str):
        result = value
        for sensitive in sensitive_values:
            if sensitive:
                result = result.replace(sensitive, "[managed by advisor]")
        return result
    return value


def _provider_user_message_id(payload: dict[str, Any]) -> str:
    provider_request = payload.get("request")
    messages = provider_request.get("messages") if isinstance(provider_request, dict) else None
    if not isinstance(messages, list):
        return ""
    user_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and isinstance(message.get("author"), dict)
        and message["author"].get("role") == "user"
    ]
    if len(user_messages) != 1:
        return ""
    value = user_messages[0].get("id")
    return value if isinstance(value, str) else ""


def _rewrite_sse_payload(
    payload: Any,
    *,
    provider: str,
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    expected_conversation_id: str,
    sensitive_values: set[str],
    tracker: dict[str, bool],
    submission_nonce: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")

    if event_type == "request":
        if submission_nonce:
            user_message_id = _provider_user_message_id(payload)
            if not user_message_id:
                tracker["error"] = True
                return {"type": "error", "error": "ChatGPT returned invalid cloud submission identity."}
            catalog.bind_submission_user_message(
                project_key,
                conversation_key,
                auth,
                submission_nonce,
                user_message_id,
            )
            tracker["request"] = True
        return None

    if event_type == "conversation":
        conversation = payload.get("conversation")
        state = conversation.get(provider) if isinstance(conversation, dict) else None
        if not isinstance(state, dict):
            tracker["error"] = True
            return {"type": "error", "error": "ChatGPT returned invalid cloud conversation state."}
        returned_id = state.get("conversation_id")
        if returned_id != expected_conversation_id:
            tracker["error"] = True
            return {"type": "error", "error": "ChatGPT returned a different cloud conversation."}
        catalog.update_remote_state(project_key, conversation_key, auth, state)
        tracker["state"] = True
        return {
            "type": "conversation",
            "conversation": {
                provider: {
                    "advisor_cloud_project": project_key,
                    "advisor_cloud_handle": conversation_key,
                }
            },
        }

    if event_type == "reasoning":
        token = payload.get("token")
        if (
            not set(payload).issubset(ALLOWED_REASONING_EVENT_FIELDS)
            or (token is not None and token != "")
        ):
            return None
        activity = _bounded_activity_text(payload.get("status"))
        if activity:
            return {"type": "activity", "content": activity}
        return None

    if event_type == "content":
        content = payload.get("content")
        return {"type": "content", "content": content} if isinstance(content, str) else None

    if event_type == "provider":
        return {"type": "provider", "provider": "ChatGPT Cloud"}

    if event_type == "finish":
        tracker["finish"] = True
        return {"type": "finish"}

    if event_type in {"error", "auth"} or (event_type == "message" and payload.get("error")):
        tracker["error"] = True
        message = "ChatGPT authentication failed." if event_type == "auth" else "ChatGPT cloud turn failed."
        return {"type": "error", "error": message}

    # Request metadata, hidden reasoning tokens, tool calls, tool responses,
    # debug logs, and provider internals are unnecessary for this local UI.
    return None


def rewrite_sse_chunk(
    chunk: str | bytes | dict[str, Any],
    *,
    provider: str,
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
    expected_conversation_id: str,
    sensitive_values: set[str],
    tracker: dict[str, bool],
    submission_nonce: str | None = None,
) -> str | bytes:
    def rewrite(payload: Any) -> dict[str, Any] | None:
        rewritten = _rewrite_sse_payload(
            payload,
            provider=provider,
            project_key=project_key,
            conversation_key=conversation_key,
            auth=auth,
            expected_conversation_id=expected_conversation_id,
            sensitive_values=sensitive_values,
            tracker=tracker,
            submission_nonce=submission_nonce,
        )
        return _scrub_value(rewritten, sensitive_values) if rewritten is not None else None

    if isinstance(chunk, dict):
        rewritten = rewrite(dict(chunk))
        if rewritten is None:
            return ""
        return "data: " + json.dumps(rewritten, separators=(",", ":")) + "\n\n"

    was_bytes = isinstance(chunk, bytes)
    text = chunk.decode("utf-8", errors="replace") if was_bytes else chunk
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("data: "):
            output.append(line)
            continue
        suffix = "\n" if line.endswith("\n") else ""
        raw = line[6:].rstrip("\r\n")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            safe = {"type": "error", "error": "Advisor cloud bridge rejected malformed provider output."}
            output.append("data: " + json.dumps(safe) + suffix)
            tracker["error"] = True
            continue
        payload = rewrite(payload)
        if payload is not None:
            output.append("data: " + json.dumps(payload, separators=(",", ":")) + suffix)
    transformed = "".join(output)
    return transformed.encode("utf-8") if was_bytes else transformed


def _prepare_cloud_turn(
    body: dict[str, Any],
    provider: str,
    project_key: str,
    conversation_key: str,
    auth: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = catalog.conversation_record(project_key, conversation_key, auth)
    if record.get("submission") or record.get("reconcile_message_id"):
        raise GuiBridgeError(
            "The previous browser stream may have been interrupted. Import/Refresh this cloud chat before sending again."
        )
    conversation_id = record.get("conversation_id")
    project_id = record.get("project_id")
    if not isinstance(conversation_id, str) or not isinstance(project_id, str):
        raise GuiBridgeError("The private cloud conversation mapping is invalid.")
    require_complete_conversation(conversation_id, auth)
    data = fetch_remote_conversation(conversation_id, auth)
    state = remote_state_from_data(conversation_id, data, auth)
    catalog.update_remote_state(project_key, conversation_key, auth, state)
    updated = dict(body)
    updated["provider"] = provider
    updated["conversation"] = state
    updated["gizmo_id"] = project_id
    updated["temporary"] = False
    return updated, {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "message_id": state.get("message_id") or "",
    }


def install_cloud_turn_wrapper(app: Flask) -> None:
    original = app.view_functions.get("_handle_conversation")
    if original is None:
        raise GuiBridgeError("The installed g4f GUI has no compatible conversation endpoint.")

    def cloud_conversation() -> Any:
        if request.headers.get("X-Advisor-Cloud") != "1":
            return _error_response("The advisor cloud request marker is required.", 403)
        is_multipart = request.mimetype == "multipart/form-data"
        request_limit = MAX_MULTIPART_REQUEST_BYTES if is_multipart else MAX_BROWSER_REQUEST_BYTES
        if request.content_length is not None and request.content_length > request_limit:
            return _error_response("The cloud turn request is too large.", 413)
        if not _same_origin_request():
            return _error_response("Cross-origin cloud conversation requests are blocked.", 403)

        if is_multipart:
            raw: str | bytes = request.form.get("json", "")
            try:
                _validate_image_uploads()
            except GuiRequestTooLarge as exc:
                return _error_response(str(exc), 413)
            except GuiBridgeError as exc:
                return _error_response(str(exc), 400)
        elif request.mimetype == "application/json":
            raw = request.get_data(cache=True)
        else:
            return _error_response("The cloud turn content type is unsupported.", 415)
        try:
            body = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return _error_response("The browser supplied an invalid cloud turn.", 400)
        if not isinstance(body, dict):
            return _error_response("The browser supplied an invalid cloud turn.", 400)
        try:
            handle = _validate_cloud_turn_body(body)
        except GuiBridgeError as exc:
            return _error_response(str(exc), 400)

        provider, project_key, conversation_key = handle
        prompt = str(body["messages"][0]["content"]).strip()
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        remote_context: Any | None = None
        conversation_lease: concurrency.ConversationLockLease | None = None
        try:
            auth = require_auth()
            remote_context = concurrency.remote_call_slot(
                _queue_timeout(),
                defer_start=True,
            )
            remote_lease = remote_context.__enter__()
            conversation_lease = concurrency.ConversationLockLease(
                timeout=_queue_timeout(),
                locks=[],
                keys=set(),
            )
            record = catalog.conversation_record(project_key, conversation_key, auth)
            conversation_id = record.get("conversation_id")
            if not isinstance(conversation_id, str):
                raise GuiBridgeError("The private cloud conversation mapping is invalid.")
            conversation_lease.acquire_key("conversation:" + conversation_id)
            updated_body, private_state = _prepare_cloud_turn(
                body,
                provider,
                project_key,
                conversation_key,
                auth,
            )
            _replace_request_body(updated_body)
            response = original()
            if not isinstance(response, Response):
                response = app.make_response(response)
            if response.status_code >= 400:
                conversation_lease.release()
                remote_context.__exit__(None, None, None)
                return response
        except Exception as exc:
            if conversation_lease is not None:
                conversation_lease.release()
            if remote_context is not None:
                remote_context.__exit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, (catalog.CatalogError, GuiBridgeError)):
                return _error_response(str(exc), 409)
            return _error_response("Advisor cloud coordination failed.", 500)

        source = response.response
        nonce = secrets.token_hex(16)
        tracker = {"request": False, "state": False, "finish": False, "error": False}
        sensitive_values = {
            str(private_state.get("conversation_id") or ""),
            str(private_state.get("project_id") or ""),
            str(private_state.get("message_id") or ""),
        }
        released = False
        release_guard = threading.Lock()

        def release() -> None:
            nonlocal released
            with release_guard:
                if released:
                    return
                released = True
            conversation_lease.release()
            remote_context.__exit__(None, None, None)

        @stream_with_context
        def stream() -> Iterator[str | bytes]:
            completed_normally = False
            try:
                remote_lease.mark_start()
                catalog.begin_submission(
                    project_key,
                    conversation_key,
                    auth,
                    nonce,
                    prompt_sha256=prompt_sha256,
                )
                for chunk in source:
                    yield rewrite_sse_chunk(
                        chunk,
                        provider=provider,
                        project_key=project_key,
                        conversation_key=conversation_key,
                        auth=auth,
                        expected_conversation_id=str(private_state["conversation_id"]),
                        sensitive_values=sensitive_values,
                        tracker=tracker,
                        submission_nonce=nonce,
                    )
                completed_normally = True
            finally:
                try:
                    close = getattr(source, "close", None)
                    if callable(close):
                        close()
                finally:
                    if (
                        completed_normally
                        and tracker["request"]
                        and tracker["state"]
                        and tracker["finish"]
                        and not tracker["error"]
                    ):
                        _finish_submission_if_remote_complete(
                            project_key,
                            conversation_key,
                            str(private_state["conversation_id"]),
                            auth,
                            nonce,
                        )
                    release()

        response.response = stream()
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Advisor-Activity-Token"] = nonce
        response.call_on_close(release)
        return response

    cloud_conversation.__name__ = "advisor_cloud_conversation"
    app.view_functions["_handle_conversation"] = cloud_conversation


def install_local_gui_shell(app: Flask, assets: Path) -> None:
    def cloud_shell(filename: str | None = None) -> Response:
        if filename is not None:
            abort(404)
        return send_from_directory(assets, "advisor_cloud_gui.html")

    def cloud_root() -> Response:
        return redirect("/chat/", code=302)

    cloud_shell.__name__ = "advisor_cloud_shell"
    cloud_root.__name__ = "advisor_cloud_root"
    chat_replaced = False
    root_replaced = False
    for rule in list(app.url_map.iter_rules()):
        if rule.rule == "/chat/":
            app.view_functions[rule.endpoint] = cloud_shell
            chat_replaced = True
        elif rule.rule == "/":
            app.view_functions[rule.endpoint] = cloud_root
            root_replaced = True
    if not chat_replaced:
        app.add_url_rule("/chat/", "advisor_cloud_shell", cloud_shell, methods=["GET"])
    if not root_replaced:
        app.add_url_rule("/", "advisor_cloud_root", cloud_root, methods=["GET"])


def _local_gui_path_allowed(path: str) -> bool:
    return path in LOCAL_GUI_PATHS or path.startswith("/advisor-api/")


def install_advisor_routes(app: Flask, script_dir: Path | None = None) -> Flask:
    assets = (script_dir or Path(__file__).resolve().parent)
    app.config["MAX_CONTENT_LENGTH"] = MAX_MULTIPART_REQUEST_BYTES
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_BROWSER_REQUEST_BYTES
    app.config["MAX_FORM_PARTS"] = 8
    install_local_gui_shell(app, assets)

    @app.before_request
    def enforce_loopback() -> None:
        if not _loopback_address(request.remote_addr) or not _host_is_loopback(request.host):
            abort(403)
        if not _local_gui_path_allowed(request.path):
            abort(404)

    @app.after_request
    def secure_local_response(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        return response

    @app.errorhandler(413)
    def request_too_large(_error: Exception) -> tuple[Response, int]:
        return _error_response("The cloud turn request is too large.", 413)

    @app.get("/advisor-cloud.js")
    def advisor_cloud_js() -> Response:
        return send_from_directory(assets, "advisor_cloud_gui.js")

    @app.get("/advisor-cloud.css")
    def advisor_cloud_css() -> Response:
        return send_from_directory(assets, "advisor_cloud_gui.css")

    @app.get("/advisor-api/health")
    def health() -> Any:
        def payload() -> Any:
            auth = require_auth()
            return jsonify({"status": "ok", "projects": len(catalog.list_projects(auth))})
        return _safe_route(payload)

    @app.get("/advisor-api/projects")
    def projects() -> Any:
        return _safe_route(lambda: jsonify({"projects": catalog.list_projects(require_auth())}))

    @app.post("/advisor-api/projects/<project_key>/refresh")
    def project_refresh(project_key: str) -> Any:
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        return _safe_route(lambda: jsonify({"conversations": refresh_project(project_key, require_auth())}))

    @app.post("/advisor-api/projects/<project_key>/conversations/<conversation_key>/import")
    def conversation_import(project_key: str, conversation_key: str) -> Any:
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        def payload() -> Any:
            try:
                conversation = import_conversation(
                    project_key,
                    conversation_key,
                    require_auth(),
                    wait=False,
                )
            except GuiCloudHistoryUnresolved as exc:
                return jsonify({"message": str(exc), "status": "unresolved"})
            except GuiRemoteTurnRunning as exc:
                return jsonify({"message": str(exc), "status": "running"})
            except GuiCloudHistoryPending as exc:
                return jsonify({"message": str(exc), "status": "pending"})
            except GuiRemoteStatusUnavailable as exc:
                return jsonify({"message": str(exc), "status": "unknown"})
            return jsonify({"conversation": conversation})
        return _safe_route(payload)

    @app.post("/advisor-api/projects/<project_key>/conversations/<conversation_key>/adopt-current")
    def conversation_adopt_current(project_key: str, conversation_key: str) -> Any:
        if request.headers.get("X-Advisor-Cloud") != "1":
            return _error_response("The advisor cloud request marker is required.", 403)
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        body = request.get_json(silent=True)
        expected_keys = {"acknowledge", "recovery_token", "resolution"}
        if (
            not isinstance(body, dict)
            or set(body) != expected_keys
            or body.get("acknowledge") != "do_not_resend"
            or body.get("resolution") != "adopt_current_branch"
            or not isinstance(body.get("recovery_token"), str)
            or len(body["recovery_token"]) != 32
            or any(character not in "0123456789abcdef" for character in body["recovery_token"])
        ):
            return _error_response("Explicit current-branch recovery confirmation is required.", 400)
        return _safe_route(
            lambda: jsonify({
                "conversation": adopt_current_cloud_branch(
                    project_key,
                    conversation_key,
                    require_auth(),
                    body["recovery_token"],
                ),
            })
        )

    @app.get("/advisor-api/projects/<project_key>/conversations/<conversation_key>/open-chatgpt")
    def conversation_open_chatgpt(project_key: str, conversation_key: str) -> Any:
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        return _safe_route(
            lambda: redirect(
                chatgpt_conversation_url(
                    project_key,
                    conversation_key,
                    require_auth(),
                ),
                code=302,
            )
        )

    @app.get("/advisor-api/projects/<project_key>/conversations/<conversation_key>/observe")
    def conversation_observe(project_key: str, conversation_key: str) -> Any:
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        return _safe_route(
            lambda: jsonify(
                observe_pending_conversation(
                    project_key,
                    conversation_key,
                    require_auth(),
                )
            )
        )

    @app.get("/advisor-api/projects/<project_key>/conversations/<conversation_key>/activity")
    def conversation_activity(project_key: str, conversation_key: str) -> Any:
        if not _same_origin_request():
            return _error_response("Cross-origin advisor GUI requests are blocked.", 403)
        submission_nonce = request.headers.get("X-Advisor-Activity-Token", "")
        if (
            len(submission_nonce) != 32
            or any(character not in "0123456789abcdef" for character in submission_nonce)
        ):
            return _error_response("The activity request token is invalid.", 400)
        return _safe_route(
            lambda: jsonify({
                "activities": live_conversation_activities(
                    project_key,
                    conversation_key,
                    require_auth(),
                    submission_nonce,
                ),
            })
        )

    install_cloud_turn_wrapper(app)
    return app


def find_g4f_dir() -> Path:
    explicit = os.environ.get("ADVISOR_G4F_DIR")
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend(candidate / "vendor" / "gpt4free" for candidate in advisor.setup_dir_candidates())
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        if (resolved / "g4f" / "gui").is_dir():
            return resolved
    raise GuiBridgeError("Could not find the pinned gpt4free checkout. Run setup first.")


def create_gui_app() -> Flask:
    g4f_dir = find_g4f_dir()
    os.chdir(g4f_dir)
    sys.path.insert(0, str(g4f_dir))
    from g4f.cookies import read_cookie_files  # noqa: PLC0415
    from g4f.gui import get_gui_app  # noqa: PLC0415

    read_cookie_files()
    app = get_gui_app(timeout=0, stream_timeout=0)
    return install_advisor_routes(app)


def register_directories(paths: list[Path]) -> list[str]:
    auth = advisor.load_chatgpt_auth()
    names: list[str] = []
    for path in paths:
        catalog.register_bound_project(path, auth=auth)
        names.append(path.expanduser().resolve().name)
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="Start the loopback-only advisor g4f GUI.")
    serve.add_argument("--port", type=int, default=int(os.environ.get("ADVISOR_GUI_PORT", DEFAULT_PORT)))
    serve.add_argument("--project-dir", type=Path, action="append", default=[])
    serve.add_argument("--no-register-cwd", action="store_true")

    register = subparsers.add_parser("register", help="Register one bound local project explicitly.")
    register.add_argument("--project-dir", type=Path, required=True)

    subparsers.add_parser("list", help="List registered project display names.")
    subparsers.add_parser("reset", help="Explicitly clear optional GUI mappings, not .codex-advisor state.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = args.command or "serve"
    if command == "register":
        register_directories([args.project_dir])
        print("Registered the bound project for the optional advisor GUI.")
        return 0
    if command == "list":
        for item in catalog.list_projects(require_auth()):
            print(item["name"])
        return 0
    if command == "reset":
        catalog.reset_catalog()
        print("Cleared the optional advisor GUI catalog. Repository bindings were not changed.")
        return 0

    port = getattr(args, "port", DEFAULT_PORT)
    if port < 1 or port > 65535:
        raise SystemExit("--port must be between 1 and 65535")
    project_dirs = list(getattr(args, "project_dir", []))
    if not getattr(args, "no_register_cwd", False):
        cwd_binding = Path.cwd() / ".codex-advisor" / "project.json"
        if cwd_binding.is_file() and Path.cwd() not in project_dirs:
            project_dirs.append(Path.cwd())
    if project_dirs:
        register_directories(project_dirs)
    app = create_gui_app()
    print(f"Advisor cloud GUI: http://127.0.0.1:{port}/chat/", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
