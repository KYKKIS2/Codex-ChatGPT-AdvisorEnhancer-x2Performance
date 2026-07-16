#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PYTHONPATH="$SCRIPTS" python3 - "$WORK" <<'PY'
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import activity_monitor
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


work = Path(sys.argv[1]).resolve()
project = work / "project"
runtime = work / "runtime"
state_dir = runtime / "project-state"
project.mkdir()
state_dir.mkdir(parents=True)
runtime.chmod(0o700)
state_dir.chmod(0o700)
log_path = state_dir / "devspace.log"
historical_secret = "HISTORICAL_SECRET_SHOULD_NOT_APPEAR"
log_path.write_text(
    json.dumps({"event": "tool_call", "tool": "read", "success": True, "durationMs": 1, "path": historical_secret}) + "\n",
    encoding="utf-8",
)
state_path = state_dir / "state.json"
state_path.write_text(
    json.dumps({"project_dir": str(project), "log_path": str(log_path), "devspace_pid": os.getpid()}),
    encoding="utf-8",
)

output = io.StringIO()
monitor = activity_monitor.ActivityMonitor.for_project(
    project,
    root=runtime,
    output=output,
    heartbeat_seconds=5,
    poll_seconds=0.05,
    process_validator=lambda _pid: True,
)
if not monitor.active:
    raise SystemExit("Expected a matching live activity monitor.")
monitor.start()
monitor._last_activity -= 10
with log_path.open("a", encoding="utf-8") as handle:
    handle.write("not-json\n")
    handle.write(json.dumps({"event": "tool_call", "tool": "unknown_secret_tool", "success": True, "secret": "LEAK_ME"}) + "\n")
    handle.write(json.dumps({"event": "tool_call", "tool": "read", "success": True, "durationMs": 12, "path": "/private/file"}) + "\n")
    handle.write(json.dumps({"event": "tool_call", "tool": "bash", "success": False, "durationMs": 25, "error": "PRIVATE_ERROR"}) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
time.sleep(0.35)
monitor.stop(response_received=True)
rendered = output.getvalue()
for expected in (
    "Watching project DevSpace activity",
    "ChatGPT agent is still working",
    "DevSpace read completed in 12 ms",
    "DevSpace bash failed in 25 ms",
    "ChatGPT response received",
):
    if expected not in rendered:
        raise SystemExit(f"Missing activity output: {expected!r}\n{rendered}")
for forbidden in (historical_secret, "LEAK_ME", "PRIVATE_ERROR", "/private/file", "unknown_secret_tool"):
    if forbidden in rendered:
        raise SystemExit(f"Unsafe or historical data leaked into activity output: {forbidden!r}")

disabled = activity_monitor.ActivityMonitor.for_project(
    project,
    enabled=False,
    root=runtime,
    process_validator=lambda _pid: True,
)
if disabled.active:
    raise SystemExit("Explicitly disabled monitor was active.")

state_path.write_text(
    json.dumps({"project_dir": str(project), "log_path": str(log_path), "pid": os.getpid()}),
    encoding="utf-8",
)
if activity_monitor.discover_log_path(project, root=runtime, process_validator=lambda _pid: True) != log_path:
    raise SystemExit("Legacy connector pid state was not accepted.")

outside = work / "outside.log"
outside.write_text("", encoding="utf-8")
link = state_dir / "linked.log"
link.symlink_to(outside)
state_path.write_text(
    json.dumps({"project_dir": str(project), "log_path": str(link), "pid": os.getpid()}),
    encoding="utf-8",
)
if activity_monitor.discover_log_path(project, root=runtime, process_validator=lambda _pid: True) is not None:
    raise SystemExit("Symlinked out-of-root activity log was accepted.")

state_path.write_text(
    json.dumps({"project_dir": str(work / "different-project"), "log_path": str(log_path), "pid": os.getpid()}),
    encoding="utf-8",
)
if activity_monitor.discover_log_path(project, root=runtime, process_validator=lambda _pid: True) is not None:
    raise SystemExit("Mismatched project activity state was accepted.")


class FakeMonitor:
    def __init__(self):
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False


fake_monitor = FakeMonitor()
expected_text = "The final advisor response remains unchanged while optional local activity reporting runs. " * 4
fake_response = {
    "choices": [{"message": {"content": expected_text}}],
    "conversation": {"conversation_id": "conv-live", "message_id": "msg-live"},
}


def assert_monitor_still_active(*_args, **_kwargs):
    if not fake_monitor.entered or fake_monitor.exited:
        raise SystemExit("Activity monitor stopped before final route validation.")


with patched_env(
    ADVISOR_PROJECT_DIR=str(project),
    ADVISOR_PERSIST_CONVERSATION="false",
    ADVISOR_TEMPORARY="true",
    ADVISOR_SYNC_REMOTE="false",
    ADVISOR_AUTO_RETRY_TAIL_FRAGMENT="false",
    ADVISOR_VALIDATE_MODEL="false",
):
    with patched(
        advisor,
        post_json=lambda *_args, **_kwargs: fake_response,
        chatgpt_project_id=lambda *_args, **_kwargs: None,
        response_needs_remote_recovery=lambda *_args, **_kwargs: False,
        assert_resolved_model_route=assert_monitor_still_active,
        assert_pro_model_route=lambda *_args, **_kwargs: None,
    ):
        with patched(advisor.activity_monitor.ActivityMonitor, for_project=lambda *_args, **_kwargs: fake_monitor):
            actual = advisor.call_compatible("Live activity transport invariance test.", advisor.DEFAULT_MODEL, 1)
if actual != expected_text:
    raise SystemExit("Activity monitoring changed the final advisor response.")
if not fake_monitor.entered or not fake_monitor.exited:
    raise SystemExit("Advisor did not scope the monitor around the complete logical advisor call.")

print("Advisor live activity tests passed.")
PY
