#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT="$(mktemp -d)"
trap 'rm -rf "$PROJECT"' EXIT

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" <<'PY'
import contextlib
import http.client
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
    "ADVISOR_MODEL": "gpt-5-6-thinking",
    "ADVISOR_THINKING_EFFORT": "pro-extended",
    "ADVISOR_AUTO_CREATE_PROJECT": "false",
    "ADVISOR_VALIDATE_MODEL": "false",
    "ADVISOR_CONVERSATION_KEY": None,
    "ADVISOR_TURN_JOURNAL_PATH": str(project / ".codex-advisor" / "turn-journal.json"),
}


with patched_env(ADVISOR_PRO_EXTENDED_MODEL=None, ADVISOR_ALLOW_PRO_MODEL_OVERRIDE=None):
    if advisor.DEFAULT_MODEL != "gpt-5-6-thinking":
        raise SystemExit(f"Unexpected default model: {advisor.DEFAULT_MODEL!r}")
    if advisor.configured_thinking_effort(None) != "max":
        raise SystemExit("Default advisor thinking effort should be ChatGPT max.")
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
    if selected_effort != "max":
        raise SystemExit(f"Unset advisor thinking effort should clamp to max: {selected_effort!r}")
    selected_effort = advisor.select_request_thinking_effort("none")
    if selected_effort != "max":
        raise SystemExit(f"Explicit no-thinking route should clamp to max: {selected_effort!r}")
    selected_effort = advisor.select_request_thinking_effort("medium")
    if selected_effort != "max":
        raise SystemExit(f"Weak non-Pro effort should clamp to max: {selected_effort!r}")
    selected = advisor.select_request_model(None, "gpt-5-5-thinking")
    if selected != "gpt-5-6-thinking":
        raise SystemExit(f"Legacy Thinking model should clamp to GPT-5.6 Thinking by default: {selected!r}")
    selected = advisor.select_request_model(None, "default")
    if selected != "gpt-5-6-thinking":
        raise SystemExit(f"ADVISOR_MODEL=default should use the safe default model: {selected!r}")
    selected = advisor.select_request_model("extended", "gpt-5-5")
    if selected != "gpt-5-6-thinking":
        raise SystemExit(f"Explicit non-thinking model should clamp to default GPT-5.6 Thinking model: {selected!r}")
    selected = advisor.select_request_model("extended", "gpt-4o")
    if selected != "gpt-5-6-thinking":
        raise SystemExit(f"Arbitrary non-Pro model should clamp to default GPT-5.6 Thinking model: {selected!r}")
    with patched_env(ADVISOR_ALLOW_NON_DEFAULT_ROUTE="true"):
        selected_effort = advisor.select_request_thinking_effort("none")
        if selected_effort != "none":
            raise SystemExit(f"Diagnostic route override did not preserve explicit effort: {selected_effort!r}")
        selected = advisor.select_request_model("none", "gpt-4o")
        if selected != "gpt-4o":
            raise SystemExit(f"Diagnostic route override did not preserve explicit model: {selected!r}")
    selected = advisor.select_request_model("high", "gpt-5-5-thinking")
    if selected != "gpt-5-6-thinking":
        raise SystemExit(f"Legacy Thinking model should clamp to GPT-5.6 Thinking with normal policy: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "gpt-5-5-thinking")
    if selected != "gpt-5-6-pro":
        raise SystemExit(f"Pro Extended did not override normal thinking model: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "default")
    if selected != "gpt-5-6-pro":
        raise SystemExit(f"ADVISOR_MODEL=default with Pro Extended should use Pro model: {selected!r}")
    selected = advisor.select_request_model("pro-extended", "gpt-5-6-pro")
    if selected != "gpt-5-6-pro":
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
            advisor.call_compatible("current prompt", "gpt-5-6-pro", 1)
        except RuntimeError as exc:
            if "empty response" not in str(exc):
                raise SystemExit(f"Unexpected empty Pro error: {exc}") from exc
        else:
            raise SystemExit("Empty Pro Extended response did not fail closed.")


with patched_env(**base_env):
    advisor.remove_state_files(advisor.default_state_path())
    prompt = "embedded prompt"
    embedded = conversation_data(prompt, "embedded final")
    with patched(
        advisor,
        post_json=lambda *a, **k: fake_chat_response("", extra={"debug_conversation": embedded}),
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        result = advisor.call_compatible(prompt, "gpt-5-6-pro", 1)
        if result != "embedded final":
            raise SystemExit(f"Embedded conversation recovery failed: {result!r}")


agent_progress = conversation_data("agent prompt", "Still inspecting files.", conv_id="conv-agent")
agent_progress["mapping"]["a1"]["message"]["end_turn"] = False
if advisor.latest_finished_assistant_text_for_prompt_data(agent_progress, "agent prompt"):
    raise SystemExit("Agent progress text with end_turn=false was accepted as a final response.")
agent_final = json.loads(json.dumps(agent_progress))
agent_final["mapping"]["a2"] = {
    "id": "a2",
    "parent": "a1",
    "message": {
        "id": "a2",
        "author": {"role": "assistant"},
        "content": {"parts": ["REVIEWER REPORT\nFinal findings."]},
        "status": "finished_successfully",
        "end_turn": True,
    },
}
agent_final["current_node"] = "a2"
if advisor.latest_finished_assistant_text_for_prompt_data(agent_final, "agent prompt") != "REVIEWER REPORT\nFinal findings.":
    raise SystemExit("Final agent response with end_turn=true was not recovered.")


repeated_messages = [
    {"id": "old-user", "role": "user", "content": "same prompt"},
    {
        "id": "old-answer",
        "role": "assistant",
        "content": "stale answer",
        "status": "finished_successfully",
        "end_turn": True,
    },
]
if advisor.latest_assistant_text_after_prompt_messages(
    repeated_messages,
    "same prompt",
    "old-answer",
):
    raise SystemExit("Repeated-prompt recovery reused an answer from before the turn parent.")
repeated_messages.extend(
    [
        {"id": "new-user", "role": "user", "content": "same prompt"},
        {
            "id": "new-answer",
            "role": "assistant",
            "content": "current answer",
            "status": "finished_successfully",
            "end_turn": True,
        },
    ]
)
if advisor.latest_assistant_text_after_prompt_messages(
    repeated_messages,
    "same prompt",
    "old-answer",
) != "current answer":
    raise SystemExit("Repeated-prompt recovery did not select the answer after the turn parent.")


agent_state = project / ".codex-advisor" / "agent.conversation.json"
agent_conv = {"conversation_id": "conv-agent", "message_id": "adapter-msg"}
conversation_fetches = {"count": 0}


def agent_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        return {"status": "IS_STREAMING"}
    conversation_fetches["count"] += 1
    return agent_progress if conversation_fetches["count"] == 1 else agent_final


with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT="2",
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
    ADVISOR_FINAL_FETCH_MAX_POLLS="2",
):
    with patched(
        advisor,
        get_json=agent_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=type("FakeTime", (), {"monotonic": staticmethod(__import__("time").monotonic), "sleep": staticmethod(lambda _seconds: None)}),
    ):
        result = advisor.fetch_remote_final_text(agent_state, agent_conv.copy(), "agent prompt", 2)
if result != "REVIEWER REPORT\nFinal findings.":
    raise SystemExit(f"Streaming agent final fetch failed: {result!r}")
saved_agent = advisor.load_conversation(agent_state)
if saved_agent.get("parent_message_id") != "a2":
    raise SystemExit("Streaming agent recovery did not advance state to the final assistant message.")

unbounded_state = project / ".codex-advisor" / "agent-unbounded.conversation.json"
unbounded_fetches = {"count": 0, "status": 0}


def unbounded_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        unbounded_fetches["status"] += 1
        return {"status": "IS_STREAMING"}
    unbounded_fetches["count"] += 1
    return agent_progress if unbounded_fetches["count"] == 1 else agent_final


with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT=None,
    ADVISOR_FINAL_FETCH_MAX_POLLS=None,
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
):
    with patched(
        advisor,
        get_json=unbounded_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=type("FakeTime", (), {"monotonic": staticmethod(__import__("time").monotonic), "sleep": staticmethod(lambda _seconds: None)}),
    ):
        result = advisor.fetch_remote_final_text(unbounded_state, agent_conv.copy(), "agent prompt", 0)
if result != "REVIEWER REPORT\nFinal findings.":
    raise SystemExit(f"Unlimited agent final fetch failed: {result!r}")
if unbounded_fetches["status"] != 0:
    raise SystemExit("Unfinished conversation evidence should avoid a redundant stream-status request.")


legacy_state = project / ".codex-advisor" / "agent-legacy-end-turn.conversation.json"
legacy_progress = conversation_data("legacy prompt", "Final-looking progress", conv_id="conv-agent")
legacy_final = json.loads(json.dumps(legacy_progress))
legacy_final["mapping"]["a2"] = {
    "id": "a2",
    "parent": "a1",
    "message": {
        "id": "a2",
        "author": {"role": "assistant"},
        "content": {"parts": ["Actual final response"]},
        "status": "finished_successfully",
        "end_turn": True,
    },
}
legacy_final["current_node"] = "a2"
legacy_fetches = {"count": 0, "status": 0}


def legacy_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        legacy_fetches["status"] += 1
        return {"status": "IS_STREAMING"}
    legacy_fetches["count"] += 1
    return legacy_progress if legacy_fetches["count"] == 1 else legacy_final


with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT=None,
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
):
    with patched(
        advisor,
        get_json=legacy_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=type("FakeTime", (), {"monotonic": staticmethod(__import__("time").monotonic), "sleep": staticmethod(lambda _seconds: None)}),
    ):
        result = advisor.fetch_remote_final_text(legacy_state, agent_conv.copy(), "legacy prompt", 0)
if result != "Actual final response" or legacy_fetches["status"] != 1:
    raise SystemExit(
        "A missing end_turn response was accepted while the remote stream was active: "
        f"result={result!r} fetches={legacy_fetches!r}"
    )


class UnknownStatusClock:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now

    @classmethod
    def sleep(cls, seconds):
        cls.now += seconds


def unknown_status_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        return {"status": "NEW_UNDOCUMENTED_STATUS"}
    return legacy_progress


unknown_state = project / ".codex-advisor" / "agent-unknown-status.conversation.json"
with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT=None,
    ADVISOR_FINAL_FETCH_UNKNOWN_STATUS_TIMEOUT="2",
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
    ADVISOR_FINAL_FETCH_MAX_POLLS=None,
):
    with patched(
        advisor,
        get_json=unknown_status_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=UnknownStatusClock,
    ):
        result = advisor.fetch_remote_final_text(
            unknown_state,
            agent_conv.copy(),
            "legacy prompt",
            0,
        )
if result or UnknownStatusClock.now < 2:
    raise SystemExit(
        "A missing-end_turn response was accepted when stream completion was unknown: "
        f"result={result!r} time={UnknownStatusClock.now}"
    )

with patched_env(**base_env):
    legacy_call_state = advisor.default_state_path()
    legacy_call_state.parent.mkdir(parents=True, exist_ok=True)
    legacy_call_state.write_text(
        json.dumps({"conversation": {"conversation_id": "conv-agent"}}),
        encoding="utf-8",
    )
    legacy_call_fetches = {"count": 0}

    def sync_legacy_call(path, conversation, *_args, **kwargs):
        if kwargs.get("expected_prompt"):
            advisor.write_transcript(path, legacy_progress, advisor.transcript_from_conversation(legacy_progress))
        return conversation

    with patched(
        advisor,
        post_json=lambda *_args, **_kwargs: fake_chat_response(
            "Final-looking progress",
            conv_id="conv-agent",
        ),
        sync_remote_conversation=sync_legacy_call,
        remote_conversation_stream_status=lambda *_args, **_kwargs: "IS_STREAMING",
        fetch_remote_final_text=lambda *_args, **_kwargs: legacy_call_fetches.__setitem__(
            "count", legacy_call_fetches["count"] + 1
        ) or "Actual final response",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        response_needs_remote_recovery=lambda *_args, **_kwargs: False,
        assert_resolved_model_route=lambda *_args, **_kwargs: None,
        assert_pro_model_route=lambda *_args, **_kwargs: None,
    ):
        result = advisor.call_compatible("legacy prompt", "gpt-5-6-pro", 1)
if result != "Actual final response" or legacy_call_fetches["count"] != 1:
    raise SystemExit(
        "call_compatible accepted a missing-end_turn transcript while streaming: "
        f"result={result!r} fetches={legacy_call_fetches!r}"
    )
advisor.remove_state_files(legacy_call_state)


invisible_state = project / ".codex-advisor" / "agent-invisible.conversation.json"
invisible_fetches = {"count": 0}
invisible_turn = conversation_data("different prompt", "different answer", conv_id="conv-agent")


def invisible_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        return {"status": "NOT_STREAMING"}
    invisible_fetches["count"] += 1
    return invisible_turn if invisible_fetches["count"] <= 3 else agent_final


with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT=None,
    ADVISOR_FINAL_FETCH_MAX_POLLS="2",
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
):
    with patched(
        advisor,
        get_json=invisible_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=type("FakeTime", (), {"monotonic": staticmethod(__import__("time").monotonic), "sleep": staticmethod(lambda _seconds: None)}),
    ):
        result = advisor.fetch_remote_final_text(invisible_state, agent_conv.copy(), "agent prompt", 0)
if result != "REVIEWER REPORT\nFinal findings." or invisible_fetches["count"] != 4:
    raise SystemExit(
        "Unlimited final fetch still stopped at the inactive-poll ceiling: "
        f"result={result!r} fetches={invisible_fetches!r}"
    )


class AcceptanceClock:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now

    @classmethod
    def sleep(cls, seconds):
        cls.now += seconds


never_visible_state = project / ".codex-advisor" / "agent-never-visible.conversation.json"
never_visible_fetches = {"count": 0}


def never_visible_get_json(url, *_args, **_kwargs):
    if url.endswith("/stream_status"):
        return {"status": "NOT_STREAMING"}
    never_visible_fetches["count"] += 1
    return invisible_turn


with patched_env(
    ADVISOR_FINAL_FETCH_TIMEOUT=None,
    ADVISOR_FINAL_FETCH_ACCEPTANCE_TIMEOUT="2",
    ADVISOR_FINAL_FETCH_MAX_POLLS=None,
    ADVISOR_FINAL_FETCH_POLL_SECONDS="0.5",
):
    with patched(
        advisor,
        get_json=never_visible_get_json,
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        time=AcceptanceClock,
    ):
        result = advisor.fetch_remote_final_text(
            never_visible_state,
            agent_conv.copy(),
            "agent prompt",
            0,
        )
if result or never_visible_fetches["count"] > 6 or AcceptanceClock.now < 2:
    raise SystemExit(
        "Unlimited final fetch did not bound discovery of an unaccepted prompt: "
        f"result={result!r} fetches={never_visible_fetches!r} time={AcceptanceClock.now}"
    )


rate_limit_calls = {"count": 0, "recorded": 0}
rate_limit_sleeps = []


def rate_limited_get_json(*_args, **_kwargs):
    rate_limit_calls["count"] += 1
    if rate_limit_calls["count"] <= 2:
        raise advisor.RateLimitError("HTTP 429 test throttle", retry_after=0.25)
    return {"ok": True}


with patched_env(ADVISOR_RATE_LIMIT_MAX_RETRIES="4"):
    with patched(
        advisor,
        get_json=rate_limited_get_json,
        rate_limit_backoff_seconds=lambda _attempt, _retry_after: 0.25,
        time=type(
            "FakeTime",
            (),
            {
                "monotonic": staticmethod(__import__("time").monotonic),
                "sleep": staticmethod(rate_limit_sleeps.append),
            },
        ),
    ):
        original_record = advisor.concurrency.record_remote_rate_limit
        advisor.concurrency.record_remote_rate_limit = (
            lambda _retry_after=None: rate_limit_calls.__setitem__(
                "recorded", rate_limit_calls["recorded"] + 1
            )
        )
        try:
            recovered_json = advisor.get_remote_json_with_backoff(
                "https://chatgpt.com/backend-api/conversation/test",
                {"Authorization": "Bearer fake"},
                0,
                operation="test fetch",
            )
        finally:
            advisor.concurrency.record_remote_rate_limit = original_record
if recovered_json != {"ok": True}:
    raise SystemExit(f"Rate-limit backoff did not recover: {recovered_json!r}")
if rate_limit_calls["recorded"] != 2 or rate_limit_sleeps != [0.25, 0.25]:
    raise SystemExit(
        f"Rate-limit recovery did not back off and record throttling: "
        f"calls={rate_limit_calls!r} sleeps={rate_limit_sleeps!r}"
    )


transient_calls = {"count": 0}
transient_sleeps = []


def transient_get_json(*_args, **_kwargs):
    transient_calls["count"] += 1
    if transient_calls["count"] <= 2:
        raise RuntimeError("connection reset during test fetch")
    return {"ok": "recovered"}


with patched_env(ADVISOR_REMOTE_GET_MAX_TRANSIENT_RETRIES="3"):
    with patched(
        advisor,
        get_json=transient_get_json,
        transient_get_backoff_seconds=lambda _attempt: 0.1,
        time=type(
            "FakeTime",
            (),
            {
                "monotonic": staticmethod(__import__("time").monotonic),
                "sleep": staticmethod(transient_sleeps.append),
            },
        ),
    ):
        transient_result = advisor.get_remote_json_with_backoff(
            "https://chatgpt.com/backend-api/conversation/test",
            {"Authorization": "Bearer fake"},
            0,
            operation="transient test fetch",
        )
if transient_result != {"ok": "recovered"} or transient_sleeps != [0.1, 0.1]:
    raise SystemExit(
        "Transient remote GET recovery did not retry cleanly: "
        f"result={transient_result!r} calls={transient_calls!r} sleeps={transient_sleeps!r}"
    )


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


class FakePostResponse:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if self.error is not None:
            raise self.error
        return self.value


post_faults = (
    ("incomplete-read", FakePostResponse(error=http.client.IncompleteRead(b"{"))),
    ("timeout", FakePostResponse(error=TimeoutError("timed out"))),
    ("connection-reset", FakePostResponse(error=ConnectionResetError("reset"))),
    ("invalid-utf8", FakePostResponse(value=b"\xff")),
    ("invalid-json", FakePostResponse(value=b"not-json")),
)
for label, fake_response in post_faults:
    post_calls = {"count": 0}

    def open_fault(*_args, **_kwargs):
        post_calls["count"] += 1
        return fake_response

    with patched(advisor, open_url=open_fault):
        try:
            advisor.post_json(
                "http://127.0.0.1:8080/v1/chat/completions",
                {"model": "test"},
                {"Content-Type": "application/json"},
                1,
            )
        except advisor.AmbiguousSubmissionError as exc:
            if not getattr(exc, "submission_outcome_unknown", False):
                raise SystemExit(f"{label} lost its ambiguous-submission marker")
        else:
            raise SystemExit(f"{label} POST response failure was accepted")
    if post_calls["count"] != 1:
        raise SystemExit(f"{label} performed more than one POST attempt")


pro_ambiguous = conversation_data(
    "pro prompt",
    "pro answer",
    conv_id="conv-pro",
    assistant_metadata={
        "model_slug": "gpt-5-6-pro",
        "resolved_model_slug": "gpt-5-3-mini",
        "default_model_slug": "gpt-5-6-pro",
        "thinking_effort": "standard",
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

    def post_ambiguous(*_args, **_kwargs):
        calls["count"] += 1
        raise RuntimeError("HTTP 500 from local adapter: ResponseStatusError: Response 422: {'detail': 'Invalid conversation body'}")

    with patched(
        advisor,
        post_json=post_ambiguous,
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        try:
            advisor.call_compatible("ambiguous prompt", "gpt-5-5-thinking", 1)
        except RuntimeError as exc:
            if "Invalid conversation body" not in str(exc):
                raise SystemExit(f"Ambiguous failure changed unexpectedly: {exc}") from exc
        else:
            raise SystemExit("Ambiguous 422 failure was retried instead of failing closed.")
        if calls["count"] != 1:
            raise SystemExit(f"Ambiguous failure performed an unsafe retry: {calls['count']}")
        if not state_path.exists():
            raise SystemExit("Ambiguous failure removed conversation state.")
        journal = json.loads((project / ".codex-advisor" / "turn-journal.json").read_text(encoding="utf-8"))
        if journal.get("phase") != "submission-outcome-unknown" or journal.get("prompt_sha256") is None:
            raise SystemExit(f"Ambiguous submission journal was not durable: {journal!r}")
        if "ambiguous prompt" in json.dumps(journal):
            raise SystemExit("Turn journal stored raw prompt content")

    calls.update({"count": 0, "fetch": 0, "recorded": 0})

    def post_rate_limited(*_args, **_kwargs):
        calls["count"] += 1
        raise advisor.RateLimitError("HTTP 429 test submission", retry_after=1.0)

    original_record = advisor.concurrency.record_remote_rate_limit
    advisor.concurrency.record_remote_rate_limit = (
        lambda _retry_after=None: calls.__setitem__("recorded", calls["recorded"] + 1)
    )
    try:
        with patched(
            advisor,
            post_json=post_rate_limited,
            sync_remote_conversation=lambda *a, **k: a[1],
            fetch_remote_final_text=lambda *a, **k: calls.__setitem__("fetch", calls["fetch"] + 1) or "",
            load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
        ):
            try:
                advisor.call_compatible("rate-limited prompt", "gpt-5-5-thinking", 1)
            except advisor.RateLimitError:
                pass
            else:
                raise SystemExit("Rate-limited turn submission did not fail closed.")
    finally:
        advisor.concurrency.record_remote_rate_limit = original_record
    if calls["count"] != 1 or calls["fetch"] != 0 or calls["recorded"] != 1:
        raise SystemExit(f"Rate-limited POST used an unsafe retry/recovery path: {calls!r}")

    calls["count"] = 0

    def post_missing(*_args, **_kwargs):
        calls["count"] += 1
        raise RuntimeError("HTTP 404 from local adapter: conversation_not_found")

    with patched(
        advisor,
        post_json=post_missing,
        sync_remote_conversation=lambda *a, **k: a[1],
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        try:
            advisor.call_compatible("missing prompt", "gpt-5-5-thinking", 1)
        except RuntimeError as exc:
            if "conversation_not_found" not in str(exc):
                raise
        else:
            raise SystemExit("A POST-side 404 was retried instead of failing closed.")
    if calls["count"] != 1 or not state_path.exists():
        raise SystemExit("A POST-side 404 repeated the turn or cleared ambiguous state.")

    sync_calls = {"count": 0}

    def clear_stale_before_submission(path, conversation, *_args, **_kwargs):
        sync_calls["count"] += 1
        if sync_calls["count"] == 1:
            advisor.remove_state_files(path)
            return None
        return conversation

    calls["count"] = 0

    def post_after_preflight_clear(*_args, **_kwargs):
        calls["count"] += 1
        return fake_chat_response(
            "",
            conv_id="new-conv",
            extra={
                "debug_conversation": conversation_data(
                    "missing prompt",
                    "fresh recovered",
                    conv_id="new-conv",
                )
            },
        )

    with patched(
        advisor,
        post_json=post_after_preflight_clear,
        sync_remote_conversation=clear_stale_before_submission,
        fetch_remote_final_text=lambda *a, **k: "",
        load_chatgpt_auth=lambda: {"headers": {"Authorization": "Bearer fake"}, "user_id": "fake"},
    ):
        result = advisor.call_compatible("missing prompt", "gpt-5-5-thinking", 1)
    if result != "fresh recovered" or calls["count"] != 1:
        raise SystemExit("Pre-submission stale-state reconciliation did not use exactly one POST.")

print("Advisor transport recovery tests passed.")
PY
