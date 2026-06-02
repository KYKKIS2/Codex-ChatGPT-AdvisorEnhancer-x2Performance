#!/usr/bin/env python3
"""Ask an external model for second-pass critique and answer-shaping guidance."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are an expert second-pass advisor helping improve an answer before it is sent.

Return concise guidance, not a polished final answer. Focus on the user's real goal,
missing assumptions, risks, better structure, and concrete details the final answer should include.
Do not expose private chain-of-thought. Do not invent facts.

Use this shape:

## Guidance

## Risks Or Gaps

## Better Final Answer Shape

## Key Details To Include
"""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def redact_sensitive(text: str) -> str:
    patterns = [
        (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", "[REDACTED_JWT]"),
        (r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{20,}", "Bearer [REDACTED]"),
        (r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(access[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(refresh[_-]?token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(session[_-]?id['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", r"\1[REDACTED]"),
        (r"(?i)(cookie['\"]?\s*[:=]\s*['\"]?)[^'\"}]+", r"\1[REDACTED]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        detail = redact_sensitive(detail)
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {body[:500]}") from exc


def extract_responses_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text

    parts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else json.dumps(response, indent=2)


def extract_chat_text(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return json.dumps(response, indent=2)
    return content if isinstance(content, str) else json.dumps(content, indent=2)


def default_state_path() -> Path:
    key = os.environ.get("ADVISOR_CONVERSATION_KEY")
    if key:
        root = Path(os.environ.get("ADVISOR_STATE_DIR", Path.home() / ".codex" / "external-advisor"))
        return root / f"{key}.conversation.json"
    return Path.cwd() / ".codex-advisor" / "conversation.json"


def load_conversation(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    conversation = data.get("conversation")
    return conversation if isinstance(conversation, dict) else None


def save_conversation(path: Path, response: dict[str, Any]) -> None:
    conversation = response.get("conversation")
    if not isinstance(conversation, dict):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conversation": conversation}, indent=2), encoding="utf-8")


def build_prompt(prompt: str, context_files: list[str]) -> str:
    blocks = [prompt.strip()]
    for path in context_files:
        blocks.append(f"\n\n--- Context file: {path} ---\n{read_text(path)}")
    return "\n".join(block for block in blocks if block.strip())


def call_openai(prompt: str, model: str, timeout: int) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required when ADVISOR_PROVIDER=openai")

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "reasoning": {"effort": os.environ.get("ADVISOR_REASONING_EFFORT", "high")},
        "max_output_tokens": int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1800")),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    return extract_responses_text(post_json(f"{base_url}/responses", payload, headers, timeout))


def call_compatible(prompt: str, model: str, timeout: int) -> str:
    base_url = os.environ.get("ADVISOR_BASE_URL", "http://localhost:8080/v1").rstrip("/")
    api_key = os.environ.get("ADVISOR_API_KEY", os.environ.get("OPENAI_API_KEY", "local"))
    reasoning_effort = os.environ.get("ADVISOR_REASONING_EFFORT")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "1800")),
    }
    persist = os.environ.get("ADVISOR_PERSIST_CONVERSATION", "true").lower() in ("1", "true", "yes")
    conversation = None
    if persist:
        state_path = default_state_path()
        conversation = load_conversation(state_path)
        if conversation:
            payload["conversation"] = conversation
    else:
        state_path = None
    if os.environ.get("ADVISOR_TEMPORARY", "false").lower() in ("1", "true", "yes"):
        payload["temporary"] = True
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
    except RuntimeError as exc:
        if persist and conversation and "conversation_deleted" in str(exc):
            if state_path is not None:
                state_path.unlink(missing_ok=True)
            payload.pop("conversation", None)
            response = post_json(f"{base_url}/chat/completions", payload, headers, timeout)
        else:
            raise
    if persist and state_path is not None:
        save_conversation(state_path, response)
    return extract_chat_text(response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Prompt, question, draft, or plan. Reads stdin when omitted.")
    parser.add_argument("--context-file", action="append", default=[], help="Additional UTF-8 context file.")
    parser.add_argument("--model", default=os.environ.get("ADVISOR_MODEL", "gpt-5-thinking"))
    parser.add_argument(
        "--provider",
        choices=["openai", "openai-compatible"],
        default=os.environ.get("ADVISOR_PROVIDER", "openai-compatible"),
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("ADVISOR_TIMEOUT", "300")))
    parser.add_argument("--save", help="Optional file path to write the guidance.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    prompt = build_prompt(prompt, args.context_file)
    if not prompt.strip():
        print("Provide --prompt or pipe text on stdin.", file=sys.stderr)
        return 2

    guidance = call_openai(prompt, args.model, args.timeout) if args.provider == "openai" else call_compatible(prompt, args.model, args.timeout)

    if args.save:
        Path(args.save).write_text(guidance, encoding="utf-8")
    print(guidance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
