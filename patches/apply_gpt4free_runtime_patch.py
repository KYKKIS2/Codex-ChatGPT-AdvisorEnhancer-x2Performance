#!/usr/bin/env python3
"""Apply idempotent advisor runtime edits to a gpt4free checkout."""

from __future__ import annotations

import argparse
from pathlib import Path


def replace_once(text: str, needle: str, replacement: str, description: str) -> tuple[str, bool]:
    if needle not in text:
        raise SystemExit(f"Could not find {description}")
    return text.replace(needle, replacement, 1), True


def patch_stubs(g4f: Path) -> None:
    path = g4f / "g4f" / "api" / "stubs.py"
    text = path.read_text(encoding="utf-8")
    changed = False
    if "gizmo_id: Optional[str]" not in text:
        needle = "    extra_body: Optional[dict] = None\n"
        replacement = (
            "    extra_body: Optional[dict] = None\n"
            "    gizmo_id: Optional[str] = None\n"
            "    conversation_mode: Optional[dict] = None\n"
        )
        text, _ = replace_once(text, needle, replacement, "extra_body field in g4f/api/stubs.py")
        changed = True
    if "thinking_effort: Optional[str]" not in text:
        needle = "    reasoning_effort: Optional[Literal[\"low\", \"medium\", \"high\"]] = None\n"
        replacement = needle + "    thinking_effort: Optional[str] = None\n"
        text, _ = replace_once(text, needle, replacement, "reasoning_effort field in g4f/api/stubs.py")
        changed = True
    if changed:
        path.write_text(text, encoding="utf-8")


def patch_openai_model_registry(g4f: Path) -> None:
    path = g4f / "g4f" / "Provider" / "openai" / "models.py"
    text = path.read_text(encoding="utf-8")
    changed = False

    if '"gpt-5-5-thinking"' not in text:
        needle = 'text_models = [default_model, '
        replacement = (
            'text_models = [default_model, '
            '"gpt-5-5", "gpt-5-5-instant", "gpt-5-5-thinking", "gpt-5-5-pro", '
        )
        text, _ = replace_once(text, needle, replacement, "OpenAI text_models declaration")
        changed = True
    elif '"gpt-5-5-pro"' not in text:
        text = text.replace('"gpt-5-5-thinking", ', '"gpt-5-5-thinking", "gpt-5-5-pro", ', 1)
        changed = True
    if '"gpt-5-pro"' not in text:
        text = text.replace('"gpt-5-5-pro", ', '"gpt-5-5-pro", "gpt-5-pro", ', 1)
        changed = True

    if '"gpt-5.5-thinking": "gpt-5-5-thinking"' not in text:
        needle = 'model_aliases = {\n'
        replacement = (
            'model_aliases = {\n'
            '    "gpt-5.5": "gpt-5-5",\n'
            '    "gpt-5.5-instant": "gpt-5-5-instant",\n'
            '    "gpt-5.5-thinking": "gpt-5-5-thinking",\n'
            '    "gpt-5.5-pro": "gpt-5-5-pro",\n'
        )
        text, _ = replace_once(text, needle, replacement, "OpenAI model_aliases declaration")
        changed = True
    elif '"gpt-5.5-pro": "gpt-5-5-pro"' not in text:
        text = text.replace(
            '    "gpt-5.5-thinking": "gpt-5-5-thinking",\n',
            '    "gpt-5.5-thinking": "gpt-5-5-thinking",\n'
            '    "gpt-5.5-pro": "gpt-5-5-pro",\n',
            1,
        )
        changed = True

    if changed:
        path.write_text(text, encoding="utf-8")


def patch_any_model_map(g4f: Path) -> None:
    path = g4f / "g4f" / "providers" / "any_model_map.py"
    text = path.read_text(encoding="utf-8")
    changed = False

    if "'gpt-5-5-thinking'" not in text:
        needle = "vision_models = ["
        replacement = "vision_models = ['gpt-5-5', 'gpt-5-5-instant', 'gpt-5-5-thinking', 'gpt-5-5-pro', "
        text, _ = replace_once(text, needle, replacement, "any_model_map vision_models declaration")
        changed = True
    elif "'gpt-5-5-pro'" not in text:
        text = text.replace("'gpt-5-5-thinking', ", "'gpt-5-5-thinking', 'gpt-5-5-pro', ", 1)
        changed = True
    if "'gpt-5-pro'" not in text:
        text = text.replace("'gpt-5-5-pro', ", "'gpt-5-5-pro', 'gpt-5-pro', ", 1)
        changed = True

    if '"gpt-5-5-thinking": {' not in text:
        needle = '  "gpt-5.2": {\n'
        block = (
            '  "gpt-5-5": {\n'
            '    "OpenaiChat": "gpt-5-5"\n'
            '  },\n'
            '  "gpt-5-5-instant": {\n'
            '    "OpenaiChat": "gpt-5-5-instant"\n'
            '  },\n'
            '  "gpt-5-5-thinking": {\n'
            '    "OpenaiChat": "gpt-5-5-thinking"\n'
            '  },\n'
            '  "gpt-5-5-pro": {\n'
            '    "OpenaiChat": "gpt-5-5-pro"\n'
            '  },\n'
        )
        text, _ = replace_once(text, needle, block + needle, "any_model_map gpt-5.2 model map")
        changed = True
    elif '"gpt-5-5-pro": {' not in text:
        text = text.replace(
            '  "gpt-5-5-thinking": {\n'
            '    "OpenaiChat": "gpt-5-5-thinking"\n'
            '  },\n',
            '  "gpt-5-5-thinking": {\n'
            '    "OpenaiChat": "gpt-5-5-thinking"\n'
            '  },\n'
            '  "gpt-5-5-pro": {\n'
            '    "OpenaiChat": "gpt-5-5-pro"\n'
            '  },\n',
            1,
        )
        changed = True
    if '"gpt-5-pro": {' in text and '"OpenaiChat": "gpt-5-pro"' not in text:
        text = text.replace(
            '  "gpt-5-pro": {\n'
            '    "PuterJS": "openrouter:openai/gpt-5-pro",\n'
            '    "OpenRouter": "openai/gpt-5-pro"\n'
            '  },\n',
            '  "gpt-5-pro": {\n'
            '    "OpenaiChat": "gpt-5-pro",\n'
            '    "PuterJS": "openrouter:openai/gpt-5-pro",\n'
            '    "OpenRouter": "openai/gpt-5-pro"\n'
            '  },\n',
            1,
        )
        changed = True

    if '"gpt-5.5-thinking": "gpt-5-5-thinking"' not in text:
        needle = '  "gpt-5-2": "gpt-5.2",\n'
        block = (
            '  "gpt-5.5": "gpt-5-5",\n'
            '  "gpt-5.5-instant": "gpt-5-5-instant",\n'
            '  "gpt-5.5-thinking": "gpt-5-5-thinking",\n'
            '  "gpt-5.5-pro": "gpt-5-5-pro",\n'
        )
        text, _ = replace_once(text, needle, block + needle, "any_model_map gpt-5.2 aliases")
        changed = True
    elif '"gpt-5.5-pro": "gpt-5-5-pro"' not in text:
        text = text.replace(
            '  "gpt-5.5-thinking": "gpt-5-5-thinking",\n',
            '  "gpt-5.5-thinking": "gpt-5-5-thinking",\n'
            '  "gpt-5.5-pro": "gpt-5-5-pro",\n',
            1,
        )
        changed = True

    if changed:
        path.write_text(text, encoding="utf-8")


def patch_openai_chat(g4f: Path) -> None:
    path = g4f / "g4f" / "Provider" / "needs_auth" / "OpenaiChat.py"
    text = path.read_text(encoding="utf-8")
    changed = False

    needle = "        reasoning_effort: Optional[str] = None,\n        **kwargs\n"
    replacement = (
        "        reasoning_effort: Optional[str] = None,\n"
        "        thinking_effort: Optional[str] = None,\n"
        "        **kwargs\n"
    )
    if "thinking_effort: Optional[str] = None" not in text:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat.create_authed reasoning_effort parameter")
        changed = True

    old_derivation = (
        "            if thinking_effort is None and reasoning_effort in {\"low\", \"medium\", \"high\"}:\n"
        "                thinking_effort = reasoning_effort\n"
    )
    if old_derivation in text:
        text = text.replace(old_derivation, "", 1)
        changed = True

    needle = (
        "            system_hints = [\"picture_v2\"] if image_model else []\n"
        "            if reasoning_effort == \"high\":\n"
        "                system_hints.append(\"reason\")\n"
        "            if web_search:\n"
    )
    replacement = (
        "            system_hints = [\"picture_v2\"] if image_model else []\n"
        "            if reasoning_effort == \"high\" and thinking_effort is None:\n"
        "                system_hints.append(\"reason\")\n"
        "            if web_search:\n"
    )
    if "if reasoning_effort == \"high\" and thinking_effort is None" not in text:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat system_hints block")
        changed = True

    needle = (
        "                    if temporary:\n"
        "                        data[\"history_and_training_disabled\"] = True\n"
        "                    if conversation.conversation_id is not None and not temporary:\n"
    )
    replacement = (
        "                    if temporary:\n"
        "                        data[\"history_and_training_disabled\"] = True\n"
        "                    if thinking_effort:\n"
        "                        data[\"thinking_effort\"] = thinking_effort\n"
        "                    if conversation.conversation_id is not None and not temporary:\n"
    )
    if text.count("data[\"thinking_effort\"] = thinking_effort") < 1:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat prepare payload insertion point")
        changed = True

    needle = (
        "                if temporary:\n"
        "                    data[\"history_and_training_disabled\"] = True\n"
        "\n"
        "                if conversation.conversation_id is not None and not temporary:\n"
    )
    replacement = (
        "                if temporary:\n"
        "                    data[\"history_and_training_disabled\"] = True\n"
        "                if thinking_effort:\n"
        "                    data[\"thinking_effort\"] = thinking_effort\n"
        "\n"
        "                if conversation.conversation_id is not None and not temporary:\n"
    )
    if text.count("data[\"thinking_effort\"] = thinking_effort") < 2:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat conversation payload insertion point")
        changed = True

    pro_streaming_prepare_block = (
        "                    if thinking_effort == \"extended\" and model in {\"gpt-5-pro\", \"gpt-5-5-pro\"}:\n"
        "                        data[\"pro_mode_turn_topic_streaming\"] = True\n"
    )
    pro_streaming_conversation_block = (
        "                if thinking_effort == \"extended\" and model in {\"gpt-5-pro\", \"gpt-5-5-pro\"}:\n"
        "                    data[\"pro_mode_turn_topic_streaming\"] = True\n"
    )
    if pro_streaming_prepare_block in text or pro_streaming_conversation_block in text:
        text = text.replace(pro_streaming_prepare_block, "")
        text = text.replace(pro_streaming_conversation_block, "")
        changed = True

    if '"client_prepare_state": "none"' not in text:
        text = text.replace(
            '                        "parent_message_id": conversation.message_id,\n'
            '                        "model": model,\n',
            '                        "parent_message_id": conversation.message_id,\n'
            '                        "model": model,\n'
            '                        "client_prepare_state": "none",\n',
            1,
        )
        text = text.replace(
            '                    "parent_message_id": conversation.message_id,\n'
            '                    "model": model,\n',
            '                    "parent_message_id": conversation.message_id,\n'
            '                    "model": model,\n'
            '                    "client_prepare_state": "none",\n',
            1,
        )
        changed = True

    if '"force_parallel_switch": "auto"' not in text:
        text = text.replace(
            '                        "supported_encodings": ["v1"]\n'
            '                    }\n',
            '                        "supported_encodings": ["v1"],\n'
            '                        "force_parallel_switch": "auto"\n'
            '                    }\n',
            1,
        )
        text = text.replace(
            '                    "paragen_cot_summary_display_override": "allow"\n'
            '                }\n',
            '                    "paragen_cot_summary_display_override": "allow",\n'
            '                    "force_parallel_switch": "auto"\n'
            '                }\n',
            1,
        )
        changed = True

    needle = (
        "                async with session.post(\n"
        "                    backend_anon_url\n"
    )
    replacement = (
        "                turn_topic_id = None\n"
        "                async with session.post(\n"
        "                    backend_anon_url\n"
    )
    if "turn_topic_id = None" not in text:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat conversation POST block")
        changed = True

    needle = (
        "                    async for line in response.iter_lines():\n"
        "                        pattern = re.compile(r\"file-service://[\\w-]+\")\n"
    )
    replacement = (
        "                    async for line in response.iter_lines():\n"
        "                        turn_topic_id = turn_topic_id or cls.get_resume_turn_topic_id(line)\n"
        "                        pattern = re.compile(r\"file-service://[\\w-]+\")\n"
    )
    if "turn_topic_id = turn_topic_id or cls.get_resume_turn_topic_id(line)" not in text:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat SSE line loop")
        changed = True

    needle = (
        "                    if buffer:\n"
        "                        yield buffer\n"
        "                if sources.list:\n"
    )
    replacement = (
        "                    if buffer:\n"
        "                        yield buffer\n"
        "                if turn_topic_id and conversation.finish_reason is None:\n"
        "                    async for chunk in cls.iter_conversation_turn_ws(\n"
        "                        session,\n"
        "                        auth_result,\n"
        "                        turn_topic_id,\n"
        "                        conversation,\n"
        "                        sources,\n"
        "                        references,\n"
        "                        timeout,\n"
        "                    ):\n"
        "                        yield chunk\n"
        "                if sources.list:\n"
    )
    if "cls.iter_conversation_turn_ws(" not in text:
        text, _ = replace_once(text, needle, replacement, "OpenaiChat post-SSE insertion point")
        changed = True

    helpers = r'''
    @classmethod
    def get_turn_topic_id(cls, token: str) -> Optional[str]:
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        except Exception as e:
            debug.error(f"OpenaiChat: Could not decode turn topic token: {e}")
            return None
        return data.get("turn_topic_id")

    @classmethod
    def get_resume_turn_topic_id(cls, line: bytes) -> Optional[str]:
        if not line.startswith(b"data: "):
            return None
        try:
            data = json.loads(line[6:])
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("type") != "resume_conversation_token":
            return None
        if data.get("kind") != "topic":
            return None
        token = data.get("token")
        return cls.get_turn_topic_id(token) if isinstance(token, str) else None

    @classmethod
    async def iter_conversation_turn_ws(
        cls,
        session,
        auth_result: AuthResult,
        topic_id: str,
        conversation: Conversation,
        sources: OpenAISources,
        references: ContentReferences,
        timeout: Optional[int] = 120,
    ) -> AsyncIterator:
        async with AsyncSession(
            timeout=timeout,
            impersonate="chrome",
            headers=auth_result.headers,
            cookies=auth_result.cookies
        ) as ws_session:
            response = await ws_session.get(
                "https://chatgpt.com/backend-api/celsius/ws/user",
                headers=auth_result.headers,
            )
            response.raise_for_status()
            websocket_url = response.json().get("websocket_url")
            if not websocket_url:
                raise RuntimeError("OpenaiChat: No websocket_url returned for conversation-turn stream")
            if not isinstance(websocket_url, str) or not websocket_url.startswith("wss://ws.chatgpt.com/"):
                raise RuntimeError("OpenaiChat: Unexpected conversation-turn websocket URL")

            wss = await ws_session.ws_connect(websocket_url, timeout=3)
            command_id = 1
            subscribed = False
            try:
                await wss.send_json([
                    {"id": command_id, "command": {"type": "connect", "presence": {"type": "presence", "state": "foreground"}}},
                    {"id": command_id + 1, "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"}},
                ])
                command_id += 2
                subscribed = True
                started = False
                while not wss.closed:
                    try:
                        frames = await wss.recv_json(timeout=60 if not started else timeout)
                    except Exception:
                        break
                    if not isinstance(frames, list):
                        frames = [frames]
                    for frame in frames:
                        if not isinstance(frame, dict):
                            continue
                        messages = []
                        reply = frame.get("reply")
                        if isinstance(reply, dict):
                            messages.extend(reply.get("catchups") or [])
                        if frame.get("type") == "message":
                            messages.append(frame)
                        for message in messages:
                            if not isinstance(message, dict) or message.get("topic_id") != topic_id:
                                continue
                            payload = message.get("payload", {})
                            if payload.get("type") != "conversation-turn-stream":
                                continue
                            stream_item = payload.get("payload", {})
                            if conversation.conversation_id is not None and stream_item.get("conversation_id") != conversation.conversation_id:
                                continue
                            encoded_item = stream_item.get("encoded_item")
                            if not isinstance(encoded_item, str):
                                continue
                            started = True
                            for encoded_line in encoded_item.splitlines():
                                async for chunk in cls.iter_messages_line(
                                    session,
                                    auth_result,
                                    encoded_line.encode(),
                                    conversation,
                                    sources,
                                    references,
                                ):
                                    yield chunk
                            if "message_stream_complete" in encoded_item or conversation.finish_reason is not None:
                                return
            finally:
                if subscribed and not wss.closed:
                    try:
                        await wss.send_json([
                            {"id": command_id, "command": {"type": "unsubscribe", "topic_id": topic_id}}
                        ])
                    except Exception:
                        pass
                if not wss.closed:
                    await wss.close()

'''
    if "def get_resume_turn_topic_id" not in text:
        needle = "    @classmethod\n    async def wait_media(\n"
        text, _ = replace_once(text, needle, helpers + needle, "OpenaiChat wait_media insertion point")
        changed = True

    if changed:
        path.write_text(text, encoding="utf-8")


def verify_markers(g4f: Path) -> None:
    stubs = (g4f / "g4f" / "api" / "stubs.py").read_text(encoding="utf-8")
    openai_chat = (g4f / "g4f" / "Provider" / "needs_auth" / "OpenaiChat.py").read_text(encoding="utf-8")
    openai_models = (g4f / "g4f" / "Provider" / "openai" / "models.py").read_text(encoding="utf-8")
    any_model_map = (g4f / "g4f" / "providers" / "any_model_map.py").read_text(encoding="utf-8")
    required = {
        "gizmo_id: Optional[str]": stubs,
        "conversation_mode: Optional[dict]": stubs,
        "thinking_effort: Optional[str]": stubs,
        "data[\"thinking_effort\"] = thinking_effort": openai_chat,
        '"client_prepare_state": "none"': openai_chat,
        '"force_parallel_switch": "auto"': openai_chat,
        "def get_resume_turn_topic_id": openai_chat,
        "iter_conversation_turn_ws": openai_chat,
        '"gpt-5-5-thinking"': openai_models,
        '"gpt-5-5-pro"': openai_models,
        '"gpt-5-pro"': openai_models,
        '"gpt-5-5-thinking": {': any_model_map,
        '"gpt-5-5-pro": {': any_model_map,
        '"OpenaiChat": "gpt-5-pro"': any_model_map,
    }
    missing = [marker for marker, text in required.items() if marker not in text]
    if missing:
        raise SystemExit("gpt4free advisor runtime patch verification failed; missing: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gpt4free_dir", type=Path)
    args = parser.parse_args()
    g4f = args.gpt4free_dir.resolve()
    patch_stubs(g4f)
    patch_openai_model_registry(g4f)
    patch_any_model_map(g4f)
    patch_openai_chat(g4f)
    verify_markers(g4f)
    print("gpt4free advisor runtime patch verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
