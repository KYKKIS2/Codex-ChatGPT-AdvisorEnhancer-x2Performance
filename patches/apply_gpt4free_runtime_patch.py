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

    if '"gpt-5-6-thinking"' not in text:
        text = text.replace(
            'text_models = [default_model, ',
            'text_models = [default_model, "gpt-5-6-thinking", "gpt-5-6-pro", "gpt-5.6-sol-wm", "gpt-5.6-terra-wm", "gpt-5.6-luna-wm", ',
            1,
        )
        changed = True

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

    if '"gpt-5.6-sol": "gpt-5-6-thinking"' not in text:
        needle = 'model_aliases = {\n'
        block = (
            '    "gpt-5.6": "gpt-5-6-thinking",\n'
            '    "gpt-5_6": "gpt-5-6-thinking",\n'
            '    "gpt-5-6": "gpt-5-6-thinking",\n'
            '    "gpt-5.6-sol": "gpt-5-6-thinking",\n'
            '    "gpt-5-6-sol": "gpt-5-6-thinking",\n'
            '    "gpt-5_6_sol": "gpt-5-6-thinking",\n'
            '    "gpt-5.6-pro": "gpt-5-6-pro",\n'
            '    "gpt-5_6_pro": "gpt-5-6-pro",\n'
            '    "gpt-5.6-terra": "gpt-5.6-terra-wm",\n'
            '    "gpt-5-6-terra": "gpt-5.6-terra-wm",\n'
            '    "gpt-5_6_terra": "gpt-5.6-terra-wm",\n'
            '    "gpt-5.6-luna": "gpt-5.6-luna-wm",\n'
            '    "gpt-5-6-luna": "gpt-5.6-luna-wm",\n'
            '    "gpt-5_6_luna": "gpt-5.6-luna-wm",\n'
        )
        text, _ = replace_once(text, needle, needle + block, "OpenAI GPT-5.6 model_aliases declaration")
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

    gpt56_vision_prefix = (
        "vision_models = ['gpt-5-6-thinking', 'gpt-5-6-pro', "
        "'gpt-5.6-sol-wm', 'gpt-5.6-terra-wm', 'gpt-5.6-luna-wm', "
    )
    stale_gpt56_vision_prefix = "vision_models = ['gpt-5-6-sol', 'gpt-5-6-terra', 'gpt-5-6-luna', "
    if stale_gpt56_vision_prefix in text:
        text, _ = replace_once(
            text,
            stale_gpt56_vision_prefix,
            gpt56_vision_prefix,
            "stale any_model_map GPT-5.6 vision_models declaration",
        )
        changed = True
    elif gpt56_vision_prefix not in text:
        needle = "vision_models = ["
        text, _ = replace_once(text, needle, gpt56_vision_prefix, "any_model_map GPT-5.6 vision_models declaration")
        changed = True

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

    if '"gpt-5-6-thinking": {' not in text:
        needle = '  "gpt-5-5": {\n' if '  "gpt-5-5": {\n' in text else '  "gpt-5.2": {\n'
        block = (
            '  "gpt-5-6-thinking": {\n'
            '    "OpenaiChat": "gpt-5-6-thinking"\n'
            '  },\n'
            '  "gpt-5-6-pro": {\n'
            '    "OpenaiChat": "gpt-5-6-pro"\n'
            '  },\n'
            '  "gpt-5.6-sol-wm": {\n'
            '    "OpenaiChat": "gpt-5.6-sol-wm"\n'
            '  },\n'
            '  "gpt-5.6-terra-wm": {\n'
            '    "OpenaiChat": "gpt-5.6-terra-wm"\n'
            '  },\n'
            '  "gpt-5.6-luna-wm": {\n'
            '    "OpenaiChat": "gpt-5.6-luna-wm"\n'
            '  },\n'
            '  "gpt-5-6-sol": {\n'
            '    "OpenaiChat": "gpt-5-6-thinking"\n'
            '  },\n'
            '  "gpt-5-6-terra": {\n'
            '    "OpenaiChat": "gpt-5.6-terra-wm"\n'
            '  },\n'
            '  "gpt-5-6-luna": {\n'
            '    "OpenaiChat": "gpt-5.6-luna-wm"\n'
            '  },\n'
            '  "gpt-5-6": {\n'
            '    "OpenaiChat": "gpt-5-6-thinking"\n'
            '  },\n'
        )
        text, _ = replace_once(text, needle, block + needle, "any_model_map GPT-5.6 model map")
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

    if '"gpt-5.6-sol": "gpt-5-6-thinking"' not in text:
        needle = (
            '  "gpt-5.5": "gpt-5-5",\n'
            if '  "gpt-5.5": "gpt-5-5",\n' in text
            else '  "gpt-5-2": "gpt-5.2",\n'
        )
        block = (
            '  "gpt-5.6": "gpt-5-6-thinking",\n'
            '  "gpt-5_6": "gpt-5-6-thinking",\n'
            '  "gpt-5.6-sol": "gpt-5-6-thinking",\n'
            '  "gpt-5_6_sol": "gpt-5-6-thinking",\n'
            '  "gpt-5.6-pro": "gpt-5-6-pro",\n'
            '  "gpt-5_6_pro": "gpt-5-6-pro",\n'
            '  "gpt-5.6-terra": "gpt-5.6-terra-wm",\n'
            '  "gpt-5_6_terra": "gpt-5.6-terra-wm",\n'
            '  "gpt-5.6-luna": "gpt-5.6-luna-wm",\n'
            '  "gpt-5_6_luna": "gpt-5.6-luna-wm",\n'
        )
        text, _ = replace_once(text, needle, block + needle, "any_model_map GPT-5.6 aliases")
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


def patch_har_file(g4f: Path) -> None:
    path = g4f / "g4f" / "Provider" / "openai" / "har_file.py"
    text = path.read_text(encoding="utf-8")
    changed = False

    if "CLIENT_HEADER_NAMES = {" not in text:
        needle = 'conversation_url = "https://chatgpt.com/c/"\n'
        block = '''

CLIENT_HEADER_NAMES = {
    "oai-client-build-number",
    "oai-client-version",
    "oai-device-id",
    "oai-language",
    "oai-session-id",
    "origin",
    "x-openai-target-path",
    "x-openai-target-route",
}
_client_request_context = None
'''
        text, _ = replace_once(text, needle, needle + block, "ChatGPT conversation_url declaration")
        changed = True

    if "def get_client_request_context()" not in text:
        needle = "def parseHAREntry(entry) -> arkReq:\n"
        helpers = '''def get_client_request_context() -> tuple[dict, str | None]:
    """Return non-secret web-client headers and the frontend conduit seed."""
    global _client_request_context
    if _client_request_context is not None:
        return _client_request_context
    try:
        paths = reversed(get_har_files())
    except NoValidHarFileError:
        _client_request_context = ({}, None)
        return _client_request_context
    for path in paths:
        try:
            with open(path, "rb") as file:
                har_file = json.loads(file.read())
        except (OSError, json.JSONDecodeError):
            continue
        entries = har_file.get("log", {}).get("entries", [])
        selected = {}
        conduit_seed = None
        for entry in reversed(entries):
            request = entry.get("request", {})
            request_url = request.get("url", "").split("?", 1)[0]
            if request_url not in (backend_url, prepare_url):
                continue
            headers = get_headers(entry)
            if not selected:
                selected = {name: headers[name] for name in CLIENT_HEADER_NAMES if headers.get(name)}
            candidate = headers.get("x-conduit-token")
            if (
                conduit_seed is None
                and candidate
                and candidate.count(".") != 2
                and len(candidate) <= 64
            ):
                conduit_seed = candidate
            if selected and conduit_seed:
                _client_request_context = (selected, conduit_seed)
                return _client_request_context
        if selected:
            _client_request_context = (selected, conduit_seed)
            return _client_request_context
    _client_request_context = ({}, None)
    return _client_request_context

def get_client_headers() -> dict:
    return dict(get_client_request_context()[0])

def get_conduit_seed() -> str | None:
    return get_client_request_context()[1]

'''
        text, _ = replace_once(text, needle, helpers + needle, "har_file parseHAREntry declaration")
        changed = True

    if changed:
        path.write_text(text, encoding="utf-8")


def patch_openai_chat(g4f: Path) -> None:
    path = g4f / "g4f" / "Provider" / "needs_auth" / "OpenaiChat.py"
    text = path.read_text(encoding="utf-8")
    changed = False

    if "from ..openai.har_file import get_client_headers, get_conduit_seed, get_request_config" not in text:
        needle = "from ..openai.har_file import get_request_config\n"
        replacement = "from ..openai.har_file import get_client_headers, get_conduit_seed, get_request_config\n"
        text, _ = replace_once(text, needle, replacement, "OpenaiChat HAR helper import")
        changed = True

    if '"metadata": {"selected_sources": [],' not in text:
        needle = '"metadata": {"serialization_metadata": {"custom_symbol_offsets": []},\n'
        replacement = '"metadata": {"selected_sources": [],\n                         "serialization_metadata": {"custom_symbol_offsets": []},\n'
        text, _ = replace_once(text, needle, replacement, "OpenaiChat message metadata")
        changed = True

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

    if "use_prepare = not (model.endswith(\"-pro\") and thinking_effort is not None)" not in text:
        text, _ = replace_once(
            text,
            "                conduit_token = None\n                if cls._api_key is not None:\n",
            "                conduit_token = None\n"
            "                use_prepare = not (model.endswith(\"-pro\") and thinking_effort is not None)\n"
            "                if cls._api_key is not None and use_prepare:\n",
            "OpenaiChat Pro conduit prewarm guard",
        )
        changed = True

    if "conduit_token = get_conduit_seed()" not in text:
        needle = (
            "                conduit_token = None\n"
            "                use_prepare = not (model.endswith(\"-pro\") and thinking_effort is not None)\n"
        )
        replacement = (
            "                conduit_token = get_conduit_seed()\n"
            "                use_prepare = not (model.endswith(\"-pro\") and thinking_effort is not None)\n"
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat conduit seed initialization")
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

    if '"client_prepare_dispatch": "immediate"' not in text:
        needle = (
            '                        "client_prepare_state": "none",\n'
            '                        "timezone_offset_min": -120,\n'
        )
        replacement = (
            '                        "client_prepare_state": "none",\n'
            '                        "client_prepare_dispatch": "immediate",\n'
            '                        "client_prepare_source": "context_change",\n'
            '                        "timezone_offset_min": -120,\n'
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat prepare client metadata")
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

    if '"has_web_push_capabilities": True' not in text:
        needle = (
            '                        "supported_encodings": ["v1"],\n'
            '                        "force_parallel_switch": "auto"\n'
        )
        replacement = (
            '                        "supported_encodings": ["v1"],\n'
            '                        "client_contextual_info": {\n'
            '                            "app_name": "chatgpt.com",\n'
            '                            "has_web_push_capabilities": True,\n'
            '                            "web_push_notification_permission": "default",\n'
            '                        },\n'
            '                        "force_parallel_switch": "auto"\n'
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat prepare contextual info")
        changed = True

        needle = (
            '                                               "screen_height": 1080, "screen_width": 1920},\n'
            '                    "paragen_cot_summary_display_override": "allow",\n'
        )
        replacement = (
            '                                               "screen_height": 1080, "screen_width": 1920,\n'
            '                                               "app_name": "chatgpt.com",\n'
            '                                               "has_web_push_capabilities": True,\n'
            '                                               "web_push_notification_permission": "default"},\n'
            '                    "paragen_cot_summary_display_override": "allow",\n'
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat conversation contextual info")
        changed = True

    if '"x-conduit-token": conduit_token' not in text.split("async with session.post(\n                        prepare_url", 1)[-1].split(") as response:", 1)[0]:
        needle = (
            "                        prepare_url,\n"
            "                        json=data,\n"
            "                        headers=cls._headers\n"
        )
        replacement = (
            "                        prepare_url,\n"
            "                        json=data,\n"
            "                        headers={\n"
            "                            **cls._headers,\n"
            "                            **({} if conduit_token is None else {\"x-conduit-token\": conduit_token}),\n"
            "                        }\n"
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat prepare request headers")
        changed = True

    if '(await response.json()).get("conduit_token") or conduit_token' not in text:
        needle = '                        conduit_token = (await response.json())["conduit_token"]\n'
        replacement = '                        conduit_token = (await response.json()).get("conduit_token") or conduit_token\n'
        text, _ = replace_once(text, needle, replacement, "OpenaiChat prepare conduit response")
        changed = True

    if "auth_result.headers = cls._headers" not in text:
        needle = (
            '                if not cls._set_api_key(getattr(auth_result, "api_key", None)):\n'
            '                    raise MissingAuthError("Access token is not valid")\n'
            '                async with session.get(cls.url, headers=cls._headers) as response:\n'
        )
        replacement = (
            '                if not cls._set_api_key(getattr(auth_result, "api_key", None)):\n'
            '                    raise MissingAuthError("Access token is not valid")\n'
            '                auth_result.headers = cls._headers\n'
            '                async with session.get(cls.url, headers=cls._headers) as response:\n'
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat authenticated header propagation")
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

    if "**get_client_headers()," not in text:
        needle = "        cls._headers = cls.get_default_headers() if headers is None else headers\n"
        replacement = (
            "        cls._headers = {\n"
            "            **cls.get_default_headers(),\n"
            "            **get_client_headers(),\n"
            "            **({} if headers is None else headers),\n"
            "        }\n"
        )
        text, _ = replace_once(text, needle, replacement, "OpenaiChat request header merge")
        changed = True

    if changed:
        path.write_text(text, encoding="utf-8")


def verify_markers(g4f: Path) -> None:
    stubs = (g4f / "g4f" / "api" / "stubs.py").read_text(encoding="utf-8")
    openai_chat = (g4f / "g4f" / "Provider" / "needs_auth" / "OpenaiChat.py").read_text(encoding="utf-8")
    har_file = (g4f / "g4f" / "Provider" / "openai" / "har_file.py").read_text(encoding="utf-8")
    openai_models = (g4f / "g4f" / "Provider" / "openai" / "models.py").read_text(encoding="utf-8")
    any_model_map = (g4f / "g4f" / "providers" / "any_model_map.py").read_text(encoding="utf-8")
    required = {
        "gizmo_id: Optional[str]": stubs,
        "conversation_mode: Optional[dict]": stubs,
        "thinking_effort: Optional[str]": stubs,
        '"metadata": {"selected_sources": [],': openai_chat,
        "data[\"thinking_effort\"] = thinking_effort": openai_chat,
        '"client_prepare_state": "none"': openai_chat,
        '"client_prepare_dispatch": "immediate"': openai_chat,
        '"has_web_push_capabilities": True': openai_chat,
        '"force_parallel_switch": "auto"': openai_chat,
        "conduit_token = get_conduit_seed()": openai_chat,
        "**get_client_headers(),": openai_chat,
        "auth_result.headers = cls._headers": openai_chat,
        "def get_resume_turn_topic_id": openai_chat,
        "iter_conversation_turn_ws": openai_chat,
        "CLIENT_HEADER_NAMES = {": har_file,
        "def get_client_request_context()": har_file,
        "def get_conduit_seed()": har_file,
        '"gpt-5-6-thinking"': openai_models,
        '"gpt-5-6-pro"': openai_models,
        '"gpt-5-5-thinking"': openai_models,
        '"gpt-5-5-pro"': openai_models,
        '"gpt-5-pro"': openai_models,
        '"gpt-5-6-thinking": {': any_model_map,
        '"gpt-5-6-pro": {': any_model_map,
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
    patch_har_file(g4f)
    patch_openai_chat(g4f)
    verify_markers(g4f)
    print("gpt4free advisor runtime patch verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
