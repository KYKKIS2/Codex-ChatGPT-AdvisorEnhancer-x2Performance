#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" <<'PY'
import contextlib
import json
import os
import sys
from pathlib import Path

import advisor


@contextlib.contextmanager
def patched(module, **replacements):
    old = {name: getattr(module, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in old.items():
            setattr(module, name, value)


@contextlib.contextmanager
def patched_env(**values):
    old = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def fake_chat_response(text="", conv_id="conv-1", extra=None):
    payload = {
        "choices": [{"message": {"content": text}}],
        "conversation": {"conversation_id": conv_id, "message_id": "msg-1"},
    }
    if extra:
        payload.update(extra)
    return payload


def conversation_data(user_text, assistant_text, *, conv_id="conv-1", assistant_metadata=None):
    return {
        "conversation_id": conv_id,
        "current_node": "a1",
        "mapping": {
            "u1": {
                "id": "u1",
                "parent": None,
                "message": {
                    "id": "u1",
                    "author": {"role": "user"},
                    "content": {"parts": [user_text]},
                    "status": "finished_successfully",
                },
            },
            "a1": {
                "id": "a1",
                "parent": "u1",
                "message": {
                    "id": "a1",
                    "author": {"role": "assistant"},
                    "content": {"parts": [assistant_text]},
                    "status": "finished_successfully",
                    **({"metadata": assistant_metadata} if assistant_metadata else {}),
                },
            },
        },
    }


project = Path(sys.argv[1]).resolve()
base_env = {
    "ADVISOR_PROJECT_DIR": str(project),
    "ADVISOR_BASE_URL": "http://127.0.0.1:8080/v1",
    "ADVISOR_MODEL": "gpt-5-5-thinking",
    "ADVISOR_THINKING_EFFORT": "pro-extended",
    "ADVISOR_AUTO_CREATE_PROJECT": "false",
    "ADVISOR_VALIDATE_MODEL": "false",
    "ADVISOR_CONVERSATION_KEY": None,
}


with patched_env(ADVISOR_PRO_EXTENDED_MODEL=None, ADVISOR_ALLOW_PRO_MODEL_OVERRIDE=None):
    if advisor.DEFAULT_MODEL != "gpt-5-5-thinking":
        raise SystemExit(f"Unexpected default model: {advisor.DEFAULT_MODEL!r}")
    if advisor.configured_thinking_effort(None) != "extended":
        raise SystemExit("Default advisor thinking effort should be ChatGPT extended.")
    if advisor.normalize_thinking_effort("high") != "extended":
        raise SystemExit("high did not normalize to ChatGPT's current extended effort")
    if advisor.normalize_thinking_effort("xhigh") != "max":
        raise SystemExit("xhigh did not normalize to ChatGPT's current max effort")
    if advisor.normalize_thinking_effort("reasoning-high") != "extended":
        raise SystemExit("reasoning-high did not normalize to ChatGPT's current extended effort")
    try:
        advisor.normalize_thinking_effort("definitely-not-a-real-effort")
    except RuntimeError as exc:
        if "Unknown ADVISOR_THINKING_EFFORT" not in str(exc):
            raise SystemExit(f"Unknown effort failed with unclear error: {exc}") from exc
    else:
        raise SystemExit("Unknown thinking effort was not rejected locally.")
    selected_effort = advisor.select_request_thinking_effort(None)
    if selected_effort != "extended":
        raise SystemExit(f"Unset advisor thinking effort should clamp to extended: {selected_effort!r}")
    selected_effort = advisor.select_request_thinking_effort("none")
    if selected_effort != "extended":
        raise SystemExit(f"Explicit no-thinking route should clamp to extended: {selected_effort!r}")
    selected_effort = advisor.select_request_thinking_effort("medium")
    if selected_effort != "extended":
        raise SystemExit(f"Weak non-Pro effort should clamp to extended: {selected_effort!r}")
    selected = advisor.select_request_model(None, "gpt-5-5-thinking")
    if selected != "gpt-5-5-thinking":
        raise SystemExit(f"Default Thinking model should be preserved: {selected!r}")
    selected = advisor.select_request_model(None, "default")
    if selected != "gpt-5-5-thinking":
        raise SystemExit(f"ADVISOR_MODEL=default should use the safe default model: {selected!r}")
    selected = advisor.select_request_model("extended", "gpt-5-5")
    if selected != "gpt-5-5-thinking":
        raise SystemExit(f"Explicit non-thinking model should clamp to default Thinking model: {selected!r}")
    selected = advisor.select_request_model("extended", "gpt-4o")
    if selected != "gpt-5-5-thinking":
        raise SystemExit(f"Arbitrary non-Pro model should clamp to default Thinking model: {selected!r}")
    with patched_env(ADVISOR_ALLOW_NON_DEFAULT_ROUTE="true"):
        selected_effort = advisor.select_request_thinking_effort("none")
        if selected_effort != "none":
            raise SystemExit(f"Diagnostic route override did not preserve explicit effort: {selected_effort!r}")
        selected = advisor.select_request_model("none", "gpt-4o")
        if selected != "gpt-4o":
            raise SystemExit(f"Diagnostic route override did not preserve explicit model: {selected!r}")
    selected = advisor.select_request_model("high", "gpt-5-5-thinking")
    if selected != "gpt-5-5-thinking":
        raise SystemExit(f"Legacy Thinking model with extended effort should be allowed: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "gpt-5-5-thinking")
    if selected != "gpt-5-5-pro":
        raise SystemExit(f"Pro Extended did not override normal thinking model: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "default")
    if selected != "gpt-5-5-pro":
        raise SystemExit(f"ADVISOR_MODEL=default with Pro Extended should use Pro model: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "gpt-5-5-pro")
    if selected != "gpt-5-5-pro":
        raise SystemExit(f"Pro Extended changed the Pro request model unexpectedly: {selected!r}")


with patched_env(**base_env):
    with patched(
        advisor,
        post_json=lambda *a, **k: fake_chat_response(""),
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        try:
            advisor.call_compatible("current prompt", "gpt-5-5-pro", 1)
        except RuntimeError as exc:
            if "empty response" not in str(exc):
                raise SystemExit(f"Unexpected empty Pro error: {exc}") from exc
        else:
            raise SystemExit("Empty Pro Extended response did not fail closed.")


with patched_env(**base_env):
    prompt = "embedded prompt"
    embedded = conversation_data(prompt, "embedded final")
    with patched(
        advisor,
        post_json=lambda *a, **k: fake_chat_response("", extra={"debug_conversation": embedded}),
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        result = advisor.call_compatible(prompt, "gpt-5-5-pro", 1)
        if result != "embedded final":
            raise SystemExit(f"Embedded conversation recovery failed: {result!r}")


state = project / ".codex-advisor" / "conversation.json"
state.parent.mkdir(parents=True, exist_ok=True)
conv = {"conversation_id": "conv-2", "message_id": "msg-2"}
stale = conversation_data("old prompt", "old answer", conv_id="conv-2")
with patched(
    advisor,
    get_json=lambda *a, **k: stale,
    load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
):
    advisor.sync_remote_conversation(state, conv.copy(), 1, expected_prompt="current prompt")
    if advisor.transcript_json_path(state).exists():
        raise SystemExit("Post-response sync wrote a stale transcript that lacked the current prompt.")


with patched(
    advisor,
    get_json=lambda *a, **k: stale,
    load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
):
    text = advisor.fetch_remote_final_text(state, conv.copy(), "current prompt", 1)
    if text:
        raise SystemExit("Final fetch returned text for the wrong prompt.")
    if advisor.transcript_json_path(state).exists():
        raise SystemExit("Final fetch exhaustion wrote a stale transcript.")


downgraded = conversation_data(
    "downgrade prompt",
    "downgraded answer",
    conv_id="conv-down",
    assistant_metadata={
        "model_slug": "gpt-5-5-thinking",
        "resolved_model_slug": "gpt-5-3-mini",
        "default_model_slug": "gpt-5-5-thinking",
    },
)
advisor.write_transcript(state, downgraded, advisor.transcript_from_conversation(downgraded))
try:
    advisor.assert_resolved_model_route(state, "downgrade prompt")
except RuntimeError as exc:
    if "gpt-5-3-mini" not in str(exc):
        raise SystemExit(f"Downgrade guard error omitted model metadata: {exc}") from exc
else:
    raise SystemExit("Downgraded resolved model was not rejected.")
advisor.remove_state_files(state)


pro_ambiguous = conversation_data(
    "pro prompt",
    "pro answer",
    conv_id="conv-pro",
    assistant_metadata={
        "model_slug": "gpt-5-5-pro",
        "resolved_model_slug": "gpt-5-3-mini",
        "default_model_slug": "gpt-5-5-pro",
        "thinking_effort": "extended",
    },
)
advisor.write_transcript(state, pro_ambiguous, advisor.transcript_from_conversation(pro_ambiguous))
with patched_env(ADVISOR_THINKING_EFFORT="pro-extended"):
    advisor.assert_resolved_model_route(state, "pro prompt")
    advisor.assert_pro_model_route(state, "pro prompt")
advisor.remove_state_files(state)


with patched_env(**base_env):
    state_path = advisor.default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"conversation": {"conversation_id": "bad-conv", "message_id": "bad-msg"}}), encoding="utf-8")
    calls = {"count": 0}

    def post_then_recover(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("HTTP 500 from local adapter: ResponseStatusError: Response 422: {'detail': 'Invalid conversation body'}")
        return fake_chat_response("fresh recovered", conv_id="new-conv")

    with patched(
        advisor,
        post_json=post_then_recover,
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        result = advisor.call_compatible("retry prompt", "gpt-5-5-thinking", 1)
        if result != "fresh recovered":
            raise SystemExit(f"Invalid conversation retry did not return fresh response: {result!r}")
        if calls["count"] != 2:
            raise SystemExit(f"Invalid conversation retry did not call post twice: {calls['count']}")

print("Advisor transport recovery tests passed.")
PY
