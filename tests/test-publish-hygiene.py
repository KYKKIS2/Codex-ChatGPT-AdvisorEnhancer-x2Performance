#!/usr/bin/env python3
"""Fail when Git's publishable file set contains private runtime material."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CONFIG = Path.home() / ".config" / "advisor-domain-mcp"
MAX_SCAN_BYTES = 4 * 1024 * 1024

SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
PRIVATE_DIR_NAMES = {
    ".aws",
    ".azure",
    ".cloudflared",
    ".codex-advisor",
    ".devspace",
    ".gnupg",
    ".kube",
    ".ssh",
    "har_and_cookies",
    "wallets",
}
PRIVATE_FILE_NAMES = {
    ".dockerconfigjson",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "advisor-config.json",
    "advisor-tunnel-id",
    "allowed-email",
    "auth_openaichat.json",
    "cf-access-client-id",
    "cf-access-client-secret",
    "cloudflare-api-token",
    "cloudflare-audit-token",
    "cloudflared-token",
    "config.pre-runtime-migration.json",
    "conversation.json",
    "cookie.json",
    "cookies.json",
    "credentials",
    "credentials.json",
    "gateway-runtime.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "install-cloudflared-token.sh",
    "oauth-token.json",
    "oauth_tokens.json",
    "origin-secret",
    "pinned-root",
    "terraform.tfstate",
    "token.json",
    "tokens.json",
    "transcript.json",
    "transcript.md",
    "tunnel-config.pre-runtime-migration.json",
    "tunnel-token",
}
PRIVATE_SUFFIXES = (
    ".agekey",
    ".cookie.json",
    ".cookies.json",
    ".har",
    ".har.gz",
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".ovpn",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".tfstate",
    ".token",
)
OPAQUE_SUFFIXES = (
    ".7z",
    ".a",
    ".bin",
    ".db",
    ".dll",
    ".doc",
    ".docx",
    ".duckdb",
    ".dylib",
    ".egg",
    ".exe",
    ".gz",
    ".h5",
    ".ipynb",
    ".jar",
    ".joblib",
    ".map",
    ".npy",
    ".npz",
    ".onnx",
    ".parquet",
    ".pcap",
    ".pcapng",
    ".pdf",
    ".pickle",
    ".pkl",
    ".ppt",
    ".pptx",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".saz",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".war",
    ".wasm",
    ".whl",
    ".xls",
    ".xlsx",
    ".zip",
)
IGNORE_PROBES = (
    ".codex-advisor/conversation.json",
    "nested/.cloudflared/config.yml",
    "nested/.env.production",
    "nested/har_and_cookies/session.har",
    "nested/session.har.gz",
    "nested/cookie.json",
    "nested/cookies.json",
    "nested/cookies-export.txt",
    "nested/cloudflare-api-token",
    "nested/cloudflare-audit-token",
    "nested/origin-secret",
    "nested/gateway-runtime.json",
    "nested/pinned-root",
    "nested/config.pre-runtime-migration.json",
    "nested/tunnel-config.pre-runtime-migration.json",
    "nested/install-cloudflared-token.sh",
    "nested/advisor-tunnel-id",
    "nested/tunnel-token",
    "nested/cloudflared-token",
    "nested/cf-access-client-secret",
    "nested/oauth-token.json",
    "nested/private.token",
    "nested/private.secret",
    "nested/wallet.json",
    "nested/keypair.json",
    "nested/mnemonic.txt",
    "nested/seed-phrase.txt",
    "nested/terraform.tfstate.backup",
    "nested/capture.pcap",
    "nested/notebook.ipynb",
    "nested/report.pdf",
    "nested/source.map",
    "nested/support-bundle.tar.gz",
    "nested/archive.zip",
    "nested/model.safetensors",
    "nested/training-data.parquet",
    "nested/compiled.wasm",
)
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("OpenAI-style key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "GitHub token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Stripe secret", re.compile(rb"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("Anthropic key", re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
)


def git_bytes(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def publishable_paths() -> list[Path]:
    output = git_bytes("ls-files", "-co", "--exclude-standard", "-z")
    return [ROOT / os.fsdecode(item) for item in output.split(b"\0") if item]


def private_path_reason(path: Path) -> str:
    relative = path.relative_to(ROOT)
    parts = [part.lower() for part in relative.parts]
    name = parts[-1]
    if any(part in PRIVATE_DIR_NAMES for part in parts):
        return "private runtime directory"
    if name in PRIVATE_FILE_NAMES:
        return "private runtime filename"
    if name == ".env" or (name.startswith(".env.") and name not in SAFE_ENV_TEMPLATES):
        return "environment file"
    if name.startswith("auth_") and name.endswith(".json"):
        return "authentication export"
    if name.startswith("wallet") and name.endswith(".json"):
        return "wallet file"
    if name.startswith("keypair") and name.endswith(".json"):
        return "keypair file"
    if name.startswith("mnemonic") or name.startswith("seed-phrase") or name.startswith("seed_phrase"):
        return "wallet recovery material"
    if name.startswith("terraform.tfstate"):
        return "Terraform state"
    if any(name.endswith(suffix) for suffix in PRIVATE_SUFFIXES):
        return "private file suffix"
    if any(name.endswith(suffix) for suffix in OPAQUE_SUFFIXES):
        return "opaque artifact requiring explicit review"
    return ""


def nested_value(data: object, dotted_key: str) -> str:
    current = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return ""
        current = current[part]
    return current.strip() if isinstance(current, str) else ""


def local_private_values() -> dict[bytes, str]:
    values: dict[bytes, str] = {}
    for name in ("cloudflare-api-token", "origin-secret", "allowed-email"):
        path = PRIVATE_CONFIG / name
        if path.is_file():
            value = path.read_bytes().strip()
            if len(value) >= 8:
                values[value] = name
    marker = Path("/etc/cloudflared/advisor-tunnel-id")
    if marker.is_file():
        value = marker.read_bytes().strip()
        if len(value) >= 8:
            values[value] = "system-tunnel-id"
    hostname = socket.gethostname().strip()
    if len(hostname) >= 8:
        values[hostname.encode()] = "local-hostname"
    values[str(Path.home()).encode()] = "local-home-path"
    values[str(ROOT).encode()] = "local-repository-path"
    selected = {
        "config.json": (
            "accessAudience",
            "accessIssuer",
            "publicHostname",
            "projectDir",
            "cloudflareHardening.tunnelId",
            "cloudflareHardening.zoneId",
        ),
        "gateway-runtime.json": (
            "accessAudience",
            "accessIssuer",
            "publicHostname",
        ),
    }
    for filename, keys in selected.items():
        path = PRIVATE_CONFIG / filename
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in keys:
            value = nested_value(data, key)
            if len(value) >= 8:
                values[value.encode()] = f"{filename}.{key}"
    advisor_state = ROOT / ".codex-advisor"
    identifier_keys = {
        "chatgpt_project_id",
        "conversation_id",
        "conversationid",
        "project_id",
        "projectid",
    }

    def collect_identifiers(value: object, label: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = key.lower().replace("-", "_")
                child = f"{label}.{key}"
                if (
                    normalized in identifier_keys
                    and isinstance(item, str)
                    and len(item.strip()) >= 12
                ):
                    values[item.strip().encode()] = child
                elif isinstance(item, (dict, list)):
                    collect_identifiers(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                collect_identifiers(item, f"{label}[{index}]")

    if advisor_state.is_dir():
        for path in advisor_state.rglob("*.json"):
            if path.name not in {"conversation.json", "project.json"}:
                continue
            try:
                if path.stat().st_size > MAX_SCAN_BYTES:
                    continue
                collect_identifiers(
                    json.loads(path.read_text(encoding="utf-8")),
                    f"advisor-state.{path.name}",
                )
            except (OSError, ValueError):
                continue
        projects = advisor_state / "projects"
        if projects.is_dir():
            for path in projects.iterdir():
                if path.is_dir() and re.fullmatch(r"g-p-[A-Za-z0-9_-]{12,}", path.name):
                    values[path.name.encode()] = "advisor-state.project-directory"
    return values


def content_for_index_path(relative: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f":{relative}"],
        stderr=subprocess.DEVNULL,
    )


def scan_content(
    relative: str,
    content: bytes,
    private_values: dict[bytes, str],
    failures: list[str],
    *,
    source: str,
) -> None:
    for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS:
        if pattern.search(content):
            failures.append(f"{label} pattern in {source}: {relative}")
    for value, label in private_values.items():
        if value in content:
            failures.append(f"exact local private value ({label}) in {source}: {relative}")


def scan_staged_index(
    private_values: dict[bytes, str],
    failures: list[str],
) -> None:
    output = git_bytes(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    for item in output.split(b"\0"):
        if not item:
            continue
        relative = os.fsdecode(item)
        path = ROOT / relative
        reason = private_path_reason(path)
        if reason:
            failures.append(f"{reason} in staged index: {relative}")
        try:
            content = content_for_index_path(relative)
        except subprocess.CalledProcessError:
            failures.append(f"could not inspect staged index content: {relative}")
            continue
        if len(content) <= MAX_SCAN_BYTES:
            scan_content(relative, content, private_values, failures, source="staged index")


def scan_history_for_exact_values(
    private_values: dict[bytes, str],
    failures: list[str],
) -> None:
    if not private_values:
        return
    objects = git_bytes("rev-list", "--objects", "--all").decode(
        "utf-8", errors="surrogateescape"
    )
    seen: set[str] = set()
    for entry in objects.splitlines():
        object_id, separator, relative = entry.partition(" ")
        if not separator or object_id in seen:
            continue
        seen.add(object_id)
        metadata = subprocess.check_output(
            [
                "git",
                "-C",
                str(ROOT),
                "cat-file",
                "--batch-check=%(objecttype) %(objectsize)",
            ],
            input=(object_id + "\n").encode(),
        ).decode().strip().split()
        if len(metadata) != 2 or metadata[0] != "blob":
            continue
        if not metadata[1].isdigit() or int(metadata[1]) > MAX_SCAN_BYTES:
            continue
        content = subprocess.check_output(
            ["git", "-C", str(ROOT), "cat-file", "blob", object_id]
        )
        for value, label in private_values.items():
            if value in content:
                failures.append(
                    f"exact local private value ({label}) in Git history: {relative}"
                )


def check_git_adjacent_state(failures: list[str]) -> None:
    if (ROOT / ".gitmodules").exists():
        failures.append(".gitmodules requires an explicit submodule URL/content audit")
    attributes = ROOT / ".gitattributes"
    if attributes.is_file():
        text = attributes.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?im)\bfilter\s*=\s*lfs\b", text):
            failures.append("Git LFS rules require a separate LFS object audit")
    skip_dirs = {
        ".git",
        ".codex-advisor",
        ".venv",
        "__pycache__",
        "node_modules",
        "vendor",
    }
    for current, directories, _files in os.walk(ROOT, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in directories:
            path = current_path / name
            if name == ".git":
                if path != ROOT / ".git":
                    failures.append(
                        f"nested non-ignored Git repository: {path.parent.relative_to(ROOT)}"
                    )
                continue
            if name in skip_dirs:
                continue
            kept.append(name)
        directories[:] = kept
    remotes = git_bytes("remote").decode().split()
    for remote in remotes:
        urls = git_bytes("remote", "get-url", "--all", remote).decode().splitlines()
        for url in urls:
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and (
                parsed.username or parsed.password or parsed.query or parsed.fragment
            ):
                failures.append(f"credential-bearing or parameterized Git remote: {remote}")


def main() -> int:
    failures: list[str] = []
    for probe in IGNORE_PROBES:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "--quiet", "--no-index", probe],
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"ignore rule missing for synthetic path: {probe}")
    safe_probe = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--quiet", "--no-index", ".env.example"],
        check=False,
    )
    if safe_probe.returncode == 0:
        failures.append(".env.example must remain publishable")

    paths = publishable_paths()
    private_values = local_private_values()
    for path in paths:
        reason = private_path_reason(path)
        if reason:
            failures.append(f"{reason}: {path.relative_to(ROOT)}")
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            content = path.read_bytes()
        except OSError as exc:
            failures.append(f"could not inspect {path.relative_to(ROOT)}: {exc}")
            continue
        scan_content(
            str(path.relative_to(ROOT)),
            content,
            private_values,
            failures,
            source="working tree",
        )

    scan_staged_index(private_values, failures)
    scan_history_for_exact_values(private_values, failures)
    check_git_adjacent_state(failures)

    if failures:
        print("Publish hygiene failed:")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1
    print(
        f"Publish hygiene passed: {len(paths)} tracked/unignored files; "
        f"{len(private_values)} exact local private values checked."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
