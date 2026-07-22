#!/usr/bin/env python3
"""Cross-process regression tests for advisor queueing and worker leases."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "codex-skill" / "external-advisor" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import advisor_concurrency as concurrency
import advisor
import g4f_pool


CHILD = r"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import advisor_concurrency as concurrency
import advisor

mode, label, hold, output, state_path = sys.argv[2:]
started = time.time()
if mode == "worker":
    with concurrency.worker_lease(os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080/v1"), 10.0) as lease:
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = lease.index
        worker_url = lease.url
        transient = lease.transient
elif mode == "conversation":
    with concurrency.conversation_lock(Path(state_path), 10.0):
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = -1
        worker_url = ""
        transient = False
elif mode == "conversation-upgrade":
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with concurrency.coordinated_call(
        os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080/v1"),
        target,
        request_timeout=10.0,
    ):
        time.sleep(0.2)
        advisor.save_conversation(
            target,
            {"conversation": {"conversation_id": "conversation-transition-cross-state"}},
        )
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = -1
        worker_url = ""
        transient = False
elif mode == "conversation-delayed-upgrade":
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conversation_id = "conversation-capacity-one-alias"
    with concurrency.coordinated_call(
        os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080/v1"),
        target,
        request_timeout=10.0,
    ):
        target.write_text(
            json.dumps({"conversation": {"conversation_id": conversation_id}}),
            encoding="utf-8",
        )
        time.sleep(0.2)
        concurrency.upgrade_active_conversation_lock(conversation_id)
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = -1
        worker_url = ""
        transient = False
elif mode == "coordinated":
    with concurrency.coordinated_call(
        os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080/v1"),
        Path(state_path),
        request_timeout=10.0,
    ):
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = -1
        worker_url = ""
        transient = False
elif mode == "remote":
    with concurrency.remote_call_slot(10.0):
        acquired = time.time()
        time.sleep(float(hold))
        finished = time.time()
        worker = -1
        worker_url = ""
        transient = False
else:
    raise SystemExit("unknown mode")
Path(output).write_text(json.dumps({
    "label": label,
    "started": started,
    "acquired": acquired,
    "finished": finished,
    "worker": worker,
    "worker_url": worker_url,
    "transient": transient,
}), encoding="utf-8")
"""


def child_env(runtime: Path, worker_urls: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_POOL_WORKER_URLS": worker_urls,
        "ADVISOR_QUEUE_POLL_SECONDS": "0.05",
        "ADVISOR_REMOTE_MAX_CONCURRENCY": "2",
        "ADVISOR_REMOTE_START_INTERVAL_SECONDS": "0",
        "PYTHONPATH": str(SCRIPTS),
    })
    return env


def transient_child_env(runtime: Path, base_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_QUEUE_POLL_SECONDS": "0.02",
        "TEST_BASE_URL": base_url,
        "PYTHONPATH": str(SCRIPTS),
    })
    env.pop("ADVISOR_POOL_WORKER_URLS", None)
    return env


def remote_child_env(runtime: Path, capacity: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_REMOTE_MAX_CONCURRENCY": str(capacity),
        "ADVISOR_REMOTE_START_INTERVAL_SECONDS": "0",
        "ADVISOR_QUEUE_POLL_SECONDS": "0.02",
        "PYTHONPATH": str(SCRIPTS),
    })
    return env


def spawn_child(
    mode: str,
    label: str,
    hold: float,
    output: Path,
    state_path: Path,
    env: dict[str, str],
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            CHILD,
            str(SCRIPTS),
            mode,
            label,
            str(hold),
            str(output),
            str(state_path),
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_children(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        if process.returncode != 0:
            raise AssertionError(
                f"child failed with {process.returncode}: stdout={stdout!r} stderr={stderr!r}"
            )


def read_results(paths: list[Path]) -> list[dict[str, float | int | str]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def max_overlap(results: list[dict[str, float | int | str]]) -> int:
    events: list[tuple[float, int]] = []
    for item in results:
        events.append((float(item["acquired"]), 1))
        events.append((float(item["finished"]), -1))
    active = maximum = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return maximum


def test_two_worker_capacity(root: Path) -> None:
    runtime = root / "two-worker-runtime"
    outputs = [root / f"worker-{index}.json" for index in range(3)]
    env = child_env(
        runtime,
        "http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1",
    )
    processes = [
        spawn_child("worker", str(index), 0.6, outputs[index], root / "unused.json", env)
        for index in range(3)
    ]
    wait_children(processes)
    results = read_results(outputs)
    if max_overlap(results) != 2:
        raise AssertionError(f"two-worker pool did not cap concurrency at two: {results!r}")
    if {int(item["worker"]) for item in results} != {0, 1}:
        raise AssertionError(f"worker leases did not use both workers: {results!r}")


def test_fifo_single_worker(root: Path) -> None:
    runtime = root / "fifo-runtime"
    outputs = [root / f"fifo-{index}.json" for index in range(3)]
    env = child_env(runtime, "http://127.0.0.1:8080/v1")
    processes: list[subprocess.Popen[str]] = []
    for index in range(3):
        processes.append(spawn_child("worker", str(index), 0.25, outputs[index], root / "unused.json", env))
        time.sleep(0.08)
    wait_children(processes)
    results = sorted(read_results(outputs), key=lambda item: float(item["acquired"]))
    order = [str(item["label"]) for item in results]
    if order != ["0", "1", "2"]:
        raise AssertionError(f"single-worker queue was not FIFO: {order!r}")
    if max_overlap(results) != 1:
        raise AssertionError("single-worker queue allowed overlapping leases")


def test_same_conversation_serializes(root: Path) -> None:
    runtime = root / "conversation-runtime"
    state_path = root / "shared.conversation.json"
    state_path.write_text(
        json.dumps({"conversation": {"conversation_id": "conversation-shared-test"}}),
        encoding="utf-8",
    )
    outputs = [root / f"conversation-{index}.json" for index in range(2)]
    env = child_env(
        runtime,
        "http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1",
    )
    processes = [
        spawn_child("conversation", str(index), 0.4, outputs[index], state_path, env)
        for index in range(2)
    ]
    wait_children(processes)
    results = read_results(outputs)
    if max_overlap(results) != 1:
        raise AssertionError("same-conversation calls overlapped")


def test_same_conversation_across_state_files_serializes(root: Path) -> None:
    runtime = root / "cross-state-conversation-runtime"
    state_paths = [
        root / "cross-state-first.conversation.json",
        root / "cross-state-second.conversation.json",
    ]
    for state_path in state_paths:
        state_path.write_text(
            json.dumps({"conversation": {"conversation_id": "conversation-cross-state-test"}}),
            encoding="utf-8",
        )
    outputs = [root / f"cross-state-conversation-{index}.json" for index in range(2)]
    env = child_env(
        runtime,
        "http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1",
    )
    processes = [
        spawn_child("conversation", str(index), 0.4, outputs[index], state_paths[index], env)
        for index in range(2)
    ]
    wait_children(processes)
    if max_overlap(read_results(outputs)) != 1:
        raise AssertionError("same conversation id in separate state files overlapped")


def test_remote_admission_capacity(root: Path) -> None:
    runtime = root / "remote-capacity-runtime"
    outputs = [root / f"remote-capacity-{index}.json" for index in range(4)]
    env = remote_child_env(runtime, 2)
    processes: list[subprocess.Popen[str]] = []
    for index in range(4):
        processes.append(
            spawn_child("remote", str(index), 0.4, outputs[index], root / "unused.json", env)
        )
        time.sleep(0.08)
    wait_children(processes)
    results = read_results(outputs)
    if max_overlap(results) != 2:
        raise AssertionError(f"remote admission did not cap concurrency at two: {results!r}")
    ordered = sorted(results, key=lambda item: float(item["acquired"]))
    if [str(item["label"]) for item in ordered] != ["0", "1", "2", "3"]:
        raise AssertionError(f"two-slot remote admission was not FIFO: {ordered!r}")


def test_remote_admission_fifo(root: Path) -> None:
    runtime = root / "remote-fifo-runtime"
    outputs = [root / f"remote-fifo-{index}.json" for index in range(3)]
    env = remote_child_env(runtime, 1)
    processes: list[subprocess.Popen[str]] = []
    for index in range(3):
        processes.append(
            spawn_child("remote", str(index), 0.2, outputs[index], root / "unused.json", env)
        )
        time.sleep(0.08)
    wait_children(processes)
    results = sorted(read_results(outputs), key=lambda item: float(item["acquired"]))
    if [str(item["label"]) for item in results] != ["0", "1", "2"]:
        raise AssertionError(f"remote safety queue was not FIFO: {results!r}")
    if max_overlap(results) != 1:
        raise AssertionError("single-slot remote safety queue allowed overlap")


def test_remote_rate_limit_temporarily_serializes(root: Path) -> None:
    runtime = root / "remote-rate-limit-runtime"
    previous = {
        name: os.environ.get(name)
        for name in (
            "ADVISOR_RUNTIME_DIR",
            "ADVISOR_REMOTE_MAX_CONCURRENCY",
            "ADVISOR_REMOTE_RATE_LIMIT_COOLDOWN_SECONDS",
        )
    }
    os.environ.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_REMOTE_MAX_CONCURRENCY": "4",
        "ADVISOR_REMOTE_RATE_LIMIT_COOLDOWN_SECONDS": "20",
    })
    try:
        if concurrency.remote_concurrency_limit(now=100.0) != (4, False):
            raise AssertionError("clean remote admission unexpectedly started degraded")
        concurrency.record_remote_rate_limit(retry_after=30.0, now=100.0)
        if concurrency.remote_concurrency_limit(now=120.0) != (1, True):
            raise AssertionError("429 did not temporarily serialize remote admission")
        if concurrency.remote_concurrency_limit(now=131.0) != (4, False):
            raise AssertionError("remote rate-limit cooldown did not expire")
        concurrency.record_remote_rate_limit(retry_after=30.0)
        held_slot = concurrency.InterProcessLock(
            runtime / "remote-slots" / "slot-1.lock",
            timeout=0.0,
        )
        if not held_slot.try_acquire():
            raise AssertionError("could not acquire synthetic pre-degrade remote slot")
        try:
            try:
                with concurrency.remote_call_slot(0.15):
                    raise AssertionError(
                        "degraded remote admission ignored an older call in another slot"
                    )
            except RuntimeError as exc:
                if "queue timed out" not in str(exc).lower():
                    raise
        finally:
            held_slot.release()
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_supervisor_remote_capacity_is_authoritative(root: Path) -> None:
    runtime = root / "authoritative-remote-capacity-runtime"
    previous = {
        name: os.environ.get(name)
        for name in ("ADVISOR_RUNTIME_DIR", "ADVISOR_REMOTE_MAX_CONCURRENCY")
    }
    os.environ.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_REMOTE_MAX_CONCURRENCY": "4",
    })
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "g4f-pool.json").write_text(
            json.dumps({
                "manager_pid": os.getpid(),
                "manager_identity": concurrency.process_identity(os.getpid()),
                "remote_chatgpt_capacity": 2,
            }),
            encoding="utf-8",
        )
        if concurrency.configured_remote_capacity() != 2:
            raise AssertionError("caller environment overrode the live supervisor's remote capacity")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_capacity_shrink_waits_for_retired_slots(root: Path) -> None:
    runtime = root / "capacity-shrink-runtime"
    previous = {
        name: os.environ.get(name)
        for name in ("ADVISOR_RUNTIME_DIR", "ADVISOR_REMOTE_MAX_CONCURRENCY")
    }
    os.environ.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_REMOTE_MAX_CONCURRENCY": "3",
        "ADVISOR_REMOTE_START_INTERVAL_SECONDS": "0",
    })
    held = concurrency.InterProcessLock(runtime / "remote-slots" / "slot-2.lock", timeout=0.0)
    try:
        if not held.try_acquire():
            raise AssertionError("could not acquire the retired-slot test lease")
        os.environ["ADVISOR_REMOTE_MAX_CONCURRENCY"] = "1"
        try:
            with concurrency.remote_call_slot(0.15):
                raise AssertionError("capacity shrink admitted a new call while a retired slot was occupied")
        except RuntimeError as exc:
            if "queue timed out" not in str(exc).lower():
                raise
    finally:
        held.release()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_capacity_one_first_turn_alias_does_not_deadlock(root: Path) -> None:
    runtime = root / "capacity-one-first-turn-runtime"
    advisor_root = root / "capacity-one-first-turn" / ".codex-advisor"
    first_state = advisor_root / "first" / "conversation.json"
    alias_state = advisor_root / "alias" / "conversation.json"
    outputs = [root / "capacity-one-first.json", root / "capacity-one-alias.json"]
    env = child_env(runtime, "http://127.0.0.1:8080/v1")
    env["ADVISOR_REMOTE_MAX_CONCURRENCY"] = "1"
    first = spawn_child("conversation-delayed-upgrade", "first", 0.35, outputs[0], first_state, env)
    deadline = time.monotonic() + 5.0
    while not first_state.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not first_state.exists():
        first.terminate()
        first.communicate(timeout=3)
        raise AssertionError("first-turn alias test never persisted its conversation id")
    alias_state.parent.mkdir(parents=True, exist_ok=True)
    alias_state.write_text(first_state.read_text(encoding="utf-8"), encoding="utf-8")
    alias = spawn_child("coordinated", "alias", 0.1, outputs[1], alias_state, env)
    wait_children([first, alias])
    results = read_results(outputs)
    if max_overlap(results) != 1:
        raise AssertionError("capacity-one first-turn alias calls overlapped")


def test_first_turn_transition_serializes(root: Path) -> None:
    runtime = root / "first-turn-runtime"
    state_path = root / "first-turn.conversation.json"
    outputs = [root / f"first-turn-{index}.json" for index in range(2)]
    env = child_env(
        runtime,
        "http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1",
    )
    first = spawn_child("conversation", "first", 0.5, outputs[0], state_path, env)
    time.sleep(0.15)
    state_path.write_text(
        json.dumps({"conversation": {"conversation_id": "conversation-created-mid-turn"}}),
        encoding="utf-8",
    )
    second = spawn_child("conversation", "second", 0.2, outputs[1], state_path, env)
    wait_children([first, second])
    if max_overlap(read_results(outputs)) != 1:
        raise AssertionError("first-turn state-to-conversation transition overlapped")


def test_first_turn_cross_state_upgrade_serializes(root: Path) -> None:
    runtime = root / "first-turn-cross-state-runtime"
    advisor_root = root / "first-turn-cross-state" / ".codex-advisor"
    first_state = advisor_root / "roles" / "first" / "conversation.json"
    third_state = advisor_root / "roles" / "third" / "conversation.json"
    outputs = [root / f"first-turn-cross-state-{index}.json" for index in range(3)]
    env = child_env(
        runtime,
        "http://127.0.0.1:8080/v1,http://127.0.0.1:8081/v1",
    )
    first = spawn_child("conversation-upgrade", "first", 0.5, outputs[0], first_state, env)
    state_lock_path = (
        runtime
        / "conversations"
        / f"{concurrency.key_digest('state:' + str(first_state.resolve()))}.lock"
    )
    lock_deadline = time.monotonic() + 5.0
    while time.monotonic() < lock_deadline:
        probe = concurrency.InterProcessLock(state_lock_path, timeout=0.0)
        if not probe.try_acquire():
            break
        probe.release()
        time.sleep(0.02)
    else:
        first.terminate()
        raise AssertionError("first-turn owner never acquired its state lock")
    same_state_waiter = spawn_child(
        "conversation",
        "same-state-waiter",
        0.3,
        outputs[1],
        first_state,
        env,
    )
    deadline = time.monotonic() + 5.0
    while not first_state.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not first_state.exists():
        first.terminate()
        raise AssertionError("first-turn test never published its conversation id")

    third_state.parent.mkdir(parents=True, exist_ok=True)
    third_state.write_text(
        json.dumps({
            "conversation": {"conversation_id": "conversation-transition-cross-state"}
        }),
        encoding="utf-8",
    )
    cross_state_waiter = spawn_child(
        "conversation",
        "cross-state-waiter",
        0.2,
        outputs[2],
        third_state,
        env,
    )
    wait_children([first, same_state_waiter, cross_state_waiter])
    if max_overlap(read_results(outputs)) != 1:
        raise AssertionError(
            "a pre-persistence same-state waiter overlapped a cross-state conversation alias"
        )


def test_stale_and_timed_out_queue_tickets_are_removed(root: Path) -> None:
    runtime = root / "queue-cleanup-runtime"
    url = "http://127.0.0.1:8080/v1"
    previous = {
        name: os.environ.get(name)
        for name in (
            "ADVISOR_RUNTIME_DIR",
            "ADVISOR_POOL_WORKER_URLS",
            "ADVISOR_QUEUE_POLL_SECONDS",
        )
    }
    os.environ.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_POOL_WORKER_URLS": url,
        "ADVISOR_QUEUE_POLL_SECONDS": "0.02",
    })
    try:
        queue_dir = runtime / "queues" / concurrency.pool_id([url])
        queue_dir.mkdir(parents=True)
        stale_ticket = queue_dir / "00000000000000000000-dead.json"
        stale_ticket.write_text(
            json.dumps({"pid": 999999999, "process_identity": "dead"}),
            encoding="utf-8",
        )
        concurrency.cleanup_stale_tickets(queue_dir)
        if stale_ticket.exists():
            raise AssertionError("dead advisor queue ticket was not removed")

        worker_lock = concurrency.InterProcessLock(
            runtime
            / "workers"
            / f"{concurrency.pool_id([url])}-{concurrency.key_digest(url, 12)}.lock",
            timeout=0.0,
        )
        if not worker_lock.try_acquire():
            raise AssertionError("could not acquire the synthetic worker lock")
        try:
            try:
                with concurrency.worker_lease(url, 0.15):
                    raise AssertionError("worker lease unexpectedly bypassed the held lock")
            except RuntimeError as exc:
                if "queue timed out" not in str(exc).lower():
                    raise
        finally:
            worker_lock.release()
        if list(queue_dir.glob("*.json")):
            raise AssertionError("timed-out advisor queue left a ticket behind")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_pool_degrades_after_cross_worker_failures(root: Path) -> None:
    runtime = root / "health-runtime"
    urls = ["http://127.0.0.1:8080/v1", "http://127.0.0.1:8081/v1"]
    previous = {
        name: os.environ.get(name)
        for name in (
            "ADVISOR_RUNTIME_DIR",
            "ADVISOR_POOL_DEGRADE_FAILURES",
            "ADVISOR_POOL_FAILURE_WINDOW_SECONDS",
            "ADVISOR_POOL_DEGRADE_SECONDS",
        )
    }
    os.environ.update({
        "ADVISOR_RUNTIME_DIR": str(runtime),
        "ADVISOR_POOL_DEGRADE_FAILURES": "3",
        "ADVISOR_POOL_FAILURE_WINDOW_SECONDS": "120",
        "ADVISOR_POOL_DEGRADE_SECONDS": "300",
    })
    try:
        concurrency.record_transport_failure(urls[0], urls, now=100.0)
        concurrency.record_transport_failure(urls[1], urls, now=101.0)
        concurrency.record_transport_failure(urls[0], urls, now=102.0)
        if not concurrency.degraded_pool(urls, now=103.0):
            raise AssertionError("pool did not degrade after repeated cross-worker failures")
        if concurrency.preferred_degraded_worker(urls) != urls[1]:
            raise AssertionError("degraded pool did not choose the worker with fewer recent failures")
        if concurrency.degraded_pool(urls, now=500.0):
            raise AssertionError("pool degradation did not expire")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_pool_helpers() -> None:
    if g4f_pool.worker_ports(8080, 3, 2) != [8080, 8082, 8084]:
        raise AssertionError("worker port calculation changed")
    command = g4f_pool.worker_command(Path(sys.executable), 8080, True)
    bind_index = command.index("--bind")
    if command[bind_index:bind_index + 2] != ["--bind", "127.0.0.1:8080"]:
        raise AssertionError(f"g4f worker was not bound to loopback: {command!r}")
    if command[-3:] != ["--port", "8080", "--debug"]:
        raise AssertionError(f"unexpected worker command: {command!r}")
    venv_launcher = Path("relative-venv") / "bin" / "python"
    if g4f_pool.executable_path(venv_launcher) != (Path.cwd() / venv_launcher):
        raise AssertionError("worker executable normalization no longer preserves the venv launcher path")
    if not concurrency.transport_failure(RuntimeError("HTTP 500: Error in message stream")):
        raise AssertionError("known stream transport failure was not classified")
    if concurrency.transport_failure(RuntimeError("HTTP 422: invalid request")):
        raise AssertionError("validation failure was incorrectly classified as transport instability")
    previous = os.environ.pop("ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT", None)
    try:
        if concurrency.effective_agent_timeout(900) != 0:
            raise AssertionError("legacy 900-second agent cutoff was not neutralized")
        if concurrency.effective_agent_timeout(120) != 120:
            raise AssertionError("explicit non-legacy agent deadline was changed")
        os.environ["ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT"] = "true"
        if concurrency.effective_agent_timeout(900) != 900:
            raise AssertionError("legacy timeout compatibility override was ignored")
    finally:
        if previous is None:
            os.environ.pop("ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT", None)
        else:
            os.environ["ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT"] = previous


def test_loopback_transport_is_proxy_free_and_rejects_redirects() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_port}/models",
                )
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = {name: os.environ.get(name) for name in ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")}
    os.environ.update({
        "HTTP_PROXY": "http://127.0.0.1:9",
        "http_proxy": "http://127.0.0.1:9",
        "NO_PROXY": "",
        "no_proxy": "",
    })
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/models",
            method="GET",
        )
        with concurrency.open_loopback_url(request, timeout=2.0) as response:
            if response.status != 200:
                raise AssertionError("proxy-free loopback request did not reach the local server")
        redirect = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect",
            method="GET",
        )
        try:
            concurrency.open_loopback_url(redirect, timeout=2.0)
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise
        else:
            raise AssertionError("loopback transport followed an HTTP redirect")

        original_getaddrinfo = concurrency.socket.getaddrinfo
        try:
            concurrency.socket.getaddrinfo = lambda *_args, **_kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 8080))
            ]
            if concurrency.local_http_url("http://localhost:8080/v1"):
                raise AssertionError("localhost resolving outside loopback passed validation")
            request = urllib.request.Request("http://localhost:8080/v1/models")
            try:
                advisor.open_url(request, timeout=1.0)
            except RuntimeError:
                pass
            else:
                raise AssertionError("an invalid localhost resolution fell back to proxy-aware transport")
        finally:
            concurrency.socket.getaddrinfo = original_getaddrinfo
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_process_identity_uncertainty_fails_closed() -> None:
    original = concurrency.process_identity
    try:
        concurrency.process_identity = lambda _pid: ""
        if concurrency.process_alive(os.getpid(), "expected-process-identity"):
            raise AssertionError("an unverifiable process identity was treated as a safe PID match")
    finally:
        concurrency.process_identity = original


def test_recorded_transient_log_cleanup(root: Path) -> None:
    runtime = root / "transient-log-cleanup-runtime"
    previous_runtime = os.environ.get("ADVISOR_RUNTIME_DIR")
    os.environ["ADVISOR_RUNTIME_DIR"] = str(runtime)
    try:
        request = runtime / "transient-requests" / "run" / "request.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        log_path = runtime / "transient-logs" / "run" / "request.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("private diagnostic output", encoding="utf-8")
        request.write_text(json.dumps({"log_path": str(log_path)}), encoding="utf-8")
        g4f_pool.remove_request_artifacts(request)
        if request.exists() or log_path.exists():
            raise AssertionError("recorded transient failure artifacts were not removed")
        if g4f_pool.update_request(request, status="ready"):
            raise AssertionError("a late worker update returned data for a removed request")
        if request.exists():
            raise AssertionError("a late worker update recreated a removed request")

        request.write_text(json.dumps({"status": "starting"}), encoding="utf-8")
        release_path = concurrency.transient_release_path(request)
        release_path.write_text("release\n", encoding="utf-8")
        if g4f_pool.update_request(request, status="ready").get("status") != "starting":
            raise AssertionError("a released request accepted a late worker update")
        if json.loads(request.read_text(encoding="utf-8"))["status"] != "starting":
            raise AssertionError("a late worker update changed a released request on disk")

        release_path.unlink()
        outside_log = root / "must-not-be-deleted.log"
        outside_log.write_text("unrelated", encoding="utf-8")
        g4f_pool.remove_request_artifacts(request, outside_log)
        if not outside_log.exists():
            raise AssertionError("transient cleanup deleted a log outside its private runtime root")
    finally:
        if previous_runtime is None:
            os.environ.pop("ADVISOR_RUNTIME_DIR", None)
        else:
            os.environ["ADVISOR_RUNTIME_DIR"] = previous_runtime


def test_cancelled_transient_start_does_not_leak_worker(root: Path) -> None:
    runtime = root / "cancelled-transient-start-runtime"
    previous_runtime = os.environ.get("ADVISOR_RUNTIME_DIR")
    os.environ["ADVISOR_RUNTIME_DIR"] = str(runtime)
    request = runtime / "transient-requests" / "run" / "request.json"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(json.dumps({"slot": 0, "status": "pending"}), encoding="utf-8")

    class FakeProcess:
        pid = 424242

    process = FakeProcess()
    terminated: list[object] = []
    original_popen = g4f_pool.subprocess.Popen
    original_update = g4f_pool.update_request
    original_terminate = g4f_pool.terminate_process
    try:
        g4f_pool.subprocess.Popen = lambda *_args, **_kwargs: process
        g4f_pool.update_request = lambda *_args, **_kwargs: {}
        g4f_pool.terminate_process = lambda candidate: terminated.append(candidate)
        try:
            g4f_pool.start_transient_worker(
                request_path=request,
                payload={"slot": 0},
                python=Path(sys.executable),
                g4f_dir=root,
                env=os.environ.copy(),
                port=18080,
                debug=False,
                run_id="cancelled-start",
                startup_timeout=1.0,
            )
        except RuntimeError as exc:
            if "released before worker registration" not in str(exc):
                raise
        else:
            raise AssertionError("a cancelled transient request registered a live worker")
        if terminated != [process]:
            raise AssertionError("a worker spawned for a cancelled request was not terminated")
        if list((runtime / "transient-logs").rglob("*.log")):
            raise AssertionError("a cancelled transient start left a private worker log behind")
    finally:
        g4f_pool.subprocess.Popen = original_popen
        g4f_pool.update_request = original_update
        g4f_pool.terminate_process = original_terminate
        if previous_runtime is None:
            os.environ.pop("ADVISOR_RUNTIME_DIR", None)
        else:
            os.environ["ADVISOR_RUNTIME_DIR"] = previous_runtime


def test_exited_transient_worker_removes_log_after_request_race(root: Path) -> None:
    runtime = root / "transient-exit-race-runtime"
    previous_runtime = os.environ.get("ADVISOR_RUNTIME_DIR")
    os.environ["ADVISOR_RUNTIME_DIR"] = str(runtime)
    request_dir = runtime / "transient-requests" / "run"
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / "gone.json"
    log_path = runtime / "transient-logs" / "run" / "gone.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)

    class ExitedProcess:
        returncode = 17

        @staticmethod
        def poll() -> int:
            return 17

    active = {
        "gone": {
            "request_path": request_path,
            "process": ExitedProcess(),
            "log_handle": log_handle,
            "log_path": log_path,
            "slot": 0,
            "url": "http://127.0.0.1:18080/v1",
            "ready": False,
            "startup_deadline": time.monotonic() + 10,
        }
    }
    try:
        g4f_pool.service_transient_requests(
            request_dir=request_dir,
            active=active,
            python=Path(sys.executable),
            g4f_dir=root,
            env=os.environ.copy(),
            run_id="run",
            max_workers=1,
            port_base=18080,
            port_step=1,
            debug=False,
            startup_timeout=1.0,
        )
        if active:
            raise AssertionError("an exited transient worker remained active")
        if log_path.exists():
            raise AssertionError("an exited transient worker leaked its log after request removal")
    finally:
        if not log_handle.closed:
            log_handle.close()
        if previous_runtime is None:
            os.environ.pop("ADVISOR_RUNTIME_DIR", None)
        else:
            os.environ["ADVISOR_RUNTIME_DIR"] = previous_runtime


def reserve_contiguous_ports(count: int) -> int:
    for _attempt in range(100):
        sockets = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
        try:
            sockets[0].bind(("127.0.0.1", 0))
            port = int(sockets[0].getsockname()[1])
            if port + count - 1 > 65535:
                continue
            for index, sock in enumerate(sockets[1:], start=1):
                sock.bind(("127.0.0.1", port + index))
            return port
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise AssertionError(f"could not reserve {count} contiguous test ports")


def reserve_port_pair() -> int:
    return reserve_contiguous_ports(2)


def create_fake_g4f(root: Path) -> Path:
    fake_root = root / "fake-g4f"
    package = fake_root / "g4f"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        """import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[sys.argv.index('--port') + 1])

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/v1/models':
            body = json.dumps({'object': 'list', 'data': []}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
server.serve_forever()
""",
        encoding="utf-8",
    )
    return fake_root


def test_pool_supervisor_lifecycle(root: Path) -> None:
    fake_root = create_fake_g4f(root)
    runtime = root / "supervisor-runtime"
    env = os.environ.copy()
    env.update({"ADVISOR_RUNTIME_DIR": str(runtime), "PYTHONPATH": str(SCRIPTS)})
    base_port = reserve_port_pair()
    manager = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "g4f_pool.py"),
            "serve",
            "--python",
            sys.executable,
            "--g4f-dir",
            str(fake_root),
            "--port",
            str(base_port),
            "--workers",
            "2",
            "--startup-timeout",
            "10",
            "--port-wait-seconds",
            "0",
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    manifest = runtime / "g4f-pool.json"
    try:
        deadline = time.monotonic() + 12
        while not manifest.exists() and manager.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not manifest.exists():
            stdout, stderr = manager.communicate(timeout=3)
            raise AssertionError(f"pool supervisor did not become ready: {stdout!r} {stderr!r}")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        workers = payload.get("workers")
        if not isinstance(workers, list) or len(workers) != 2:
            raise AssertionError(f"pool manifest omitted workers: {payload!r}")
        for item in workers:
            with urllib.request.urlopen(str(item["url"]) + "/models", timeout=2) as response:
                if response.status != 200:
                    raise AssertionError("fake worker health endpoint failed")
        duplicate = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "g4f_pool.py"),
                "serve",
                "--python",
                sys.executable,
                "--g4f-dir",
                str(fake_root),
                "--port",
                str(base_port),
                "--workers",
                "2",
                "--startup-timeout",
                "5",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        if duplicate.returncode != 0 or "already running" not in duplicate.stdout:
            raise AssertionError(
                f"duplicate supervisor did not reuse the active pool: {duplicate.stdout!r} {duplicate.stderr!r}"
            )
        status = subprocess.run(
            [sys.executable, str(SCRIPTS / "g4f_pool.py"), "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if status.returncode != 0 or "live_workers: 2" not in status.stdout:
            raise AssertionError(f"pool status failed: {status.stdout!r} {status.stderr!r}")
        stopped = subprocess.run(
            [sys.executable, str(SCRIPTS / "g4f_pool.py"), "stop", "--timeout", "10"],
            env=env,
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )
        if stopped.returncode != 0 or "stopped" not in stopped.stdout:
            raise AssertionError(f"pool stop failed: {stopped.stdout!r} {stopped.stderr!r}")
    finally:
        if manager.poll() is None:
            manager.terminate()
        stdout, stderr = manager.communicate(timeout=15)
        if manager.returncode != 0:
            raise AssertionError(f"pool supervisor shutdown failed: {stdout!r} {stderr!r}")
    if manifest.exists():
        raise AssertionError("pool supervisor left its manifest behind")
    release_deadline = time.monotonic() + 5
    while time.monotonic() < release_deadline:
        if all(g4f_pool.port_bindable(port) for port in (base_port, base_port + 1)):
            break
        time.sleep(0.1)
    if not all(g4f_pool.port_bindable(port) for port in (base_port, base_port + 1)):
        raise AssertionError("pool supervisor left a worker process running")


def test_transient_supervisor_lifecycle(root: Path) -> None:
    fake_root = create_fake_g4f(root)
    runtime = root / "transient-supervisor-runtime"
    env = os.environ.copy()
    env.update({"ADVISOR_RUNTIME_DIR": str(runtime), "PYTHONPATH": str(SCRIPTS)})
    base_port = reserve_contiguous_ports(4)
    transient_base = base_port + 1
    manager = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "g4f_pool.py"),
            "serve",
            "--python",
            sys.executable,
            "--g4f-dir",
            str(fake_root),
            "--mode",
            "transient",
            "--port",
            str(base_port),
            "--workers",
            "1",
            "--max-transient-workers",
            "3",
            "--transient-port-base",
            str(transient_base),
            "--startup-timeout",
            "10",
            "--transient-startup-timeout",
            "10",
            "--port-wait-seconds",
            "0",
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    manifest = runtime / "g4f-pool.json"
    try:
        deadline = time.monotonic() + 12
        while not manifest.exists() and manager.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not manifest.exists():
            stdout, stderr = manager.communicate(timeout=3)
            raise AssertionError(f"transient supervisor did not become ready: {stdout!r} {stderr!r}")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("mode") != "transient":
            raise AssertionError(f"transient supervisor wrote the wrong manifest mode: {payload!r}")
        previous_runtime = os.environ.get("ADVISOR_RUNTIME_DIR")
        os.environ["ADVISOR_RUNTIME_DIR"] = str(runtime)
        try:
            detected = concurrency.active_pool_manifest(f"http://127.0.0.1:{base_port}/v1")
        finally:
            if previous_runtime is None:
                os.environ.pop("ADVISOR_RUNTIME_DIR", None)
            else:
                os.environ["ADVISOR_RUNTIME_DIR"] = previous_runtime
        if detected.get("run_id") != payload.get("run_id"):
            raise AssertionError(f"transient manifest was not discoverable by callers: {payload!r}")
        request_dir = runtime / "transient-requests" / concurrency.key_digest(str(payload["run_id"]), 32)
        child_environment = transient_child_env(runtime, f"http://127.0.0.1:{base_port}/v1")
        outputs = [root / f"transient-{index}.json" for index in range(4)]
        processes = [
            spawn_child("worker", str(index), 0.7, outputs[index], root / "unused.json", child_environment)
            for index in range(4)
        ]

        ready_payloads: list[dict[str, object]] = []
        ready_deadline = time.monotonic() + 10
        while time.monotonic() < ready_deadline:
            ready_payloads = [
                concurrency.load_json(path)
                for path in request_dir.glob("*.json")
                if concurrency.load_json(path).get("status") == "ready"
            ]
            if len(ready_payloads) == 3:
                break
            time.sleep(0.05)
        if len(ready_payloads) != 3:
            all_payloads = [concurrency.load_json(path) for path in request_dir.glob("*.json")]
            completed_results = [
                json.loads(path.read_text(encoding="utf-8")) for path in outputs if path.exists()
            ]
            child_failures = []
            for process in processes:
                if process.poll() is not None:
                    child_failures.append((process.returncode, process.stderr.read() if process.stderr else ""))
            raise AssertionError(
                "transient supervisor did not start three isolated workers: "
                f"ready={ready_payloads!r} requests={all_payloads!r} "
                f"results={completed_results!r} children={child_failures!r} manager_status={manager.poll()!r}"
            )
        if len({int(item.get("worker_pid") or 0) for item in ready_payloads}) != 3:
            raise AssertionError("transient calls did not receive distinct worker processes")

        wait_children(processes)
        results = read_results(outputs)
        if max_overlap(results) != 3:
            raise AssertionError(f"transient emergency ceiling did not cap four calls at three: {results!r}")
        if not all(bool(item.get("transient")) for item in results):
            raise AssertionError("managed calls did not use transient leases")
        if len({str(item.get("worker_url")) for item in results}) < 3:
            raise AssertionError("transient leases did not use the configured isolated ports")

        cleanup_deadline = time.monotonic() + 8
        while list(request_dir.glob("*.json")) and time.monotonic() < cleanup_deadline:
            time.sleep(0.05)
        if list(request_dir.glob("*.json")):
            raise AssertionError("completed transient calls left worker request records behind")
        if not all(g4f_pool.port_bindable(port) for port in range(transient_base, transient_base + 3)):
            raise AssertionError("completed transient calls left worker ports open")

        orphan_output = root / "transient-orphan.json"
        orphan = spawn_child(
            "worker",
            "orphan",
            30.0,
            orphan_output,
            root / "unused.json",
            child_environment,
        )
        orphan_deadline = time.monotonic() + 10
        while time.monotonic() < orphan_deadline:
            if any(concurrency.load_json(path).get("status") == "ready" for path in request_dir.glob("*.json")):
                break
            time.sleep(0.05)
        else:
            orphan.kill()
            orphan.communicate(timeout=3)
            raise AssertionError("orphan test worker never became ready")
        orphan.kill()
        orphan.communicate(timeout=3)
        reap_deadline = time.monotonic() + 8
        while list(request_dir.glob("*.json")) and time.monotonic() < reap_deadline:
            time.sleep(0.05)
        if list(request_dir.glob("*.json")):
            raise AssertionError("supervisor did not reap a transient worker after its owner died")
        if not all(g4f_pool.port_bindable(port) for port in range(transient_base, transient_base + 3)):
            raise AssertionError("orphaned transient worker retained its port")

        status = subprocess.run(
            [sys.executable, str(SCRIPTS / "g4f_pool.py"), "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if status.returncode != 0 or "active_transient_workers: 0" not in status.stdout:
            raise AssertionError(f"transient pool status was incorrect: {status.stdout!r} {status.stderr!r}")
        stopped = subprocess.run(
            [sys.executable, str(SCRIPTS / "g4f_pool.py"), "stop", "--timeout", "10"],
            env=env,
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )
        if stopped.returncode != 0:
            raise AssertionError(f"transient supervisor stop failed: {stopped.stdout!r} {stopped.stderr!r}")
    finally:
        if manager.poll() is None:
            manager.terminate()
        stdout, stderr = manager.communicate(timeout=15)
        if manager.returncode != 0:
            raise AssertionError(f"transient supervisor shutdown failed: {stdout!r} {stderr!r}")
    if not all(g4f_pool.port_bindable(port) for port in range(base_port, base_port + 4)):
        raise AssertionError("transient supervisor shutdown left a process running")


class FakeLease:
    def __init__(self, url: str) -> None:
        self.url = url
        self.success = False
        self.failure = False

    def report_success(self) -> None:
        self.success = True

    def report_failure(self) -> None:
        self.failure = True


def test_advisor_main_coordination(root: Path) -> None:
    original_base_url = "http://127.0.0.1:8080/v1"
    worker_url = "http://127.0.0.1:8081/v1"
    state_path = root / "main-state.json"
    events: list[str] = []
    lease = FakeLease(worker_url)

    @contextlib.contextmanager
    def fake_coordinated_call(configured: str, state: Path, *, request_timeout: float):
        if configured != original_base_url or state != state_path or request_timeout != 0:
            raise AssertionError("advisor main passed incorrect coordination inputs")
        events.append("enter")
        try:
            yield lease
        finally:
            events.append("exit")

    def fake_call(_prompt: str, _model: str | None, _timeout: int) -> str:
        if os.environ.get("ADVISOR_BASE_URL") != worker_url:
            raise AssertionError("advisor call did not use its leased worker URL")
        if _timeout != 0:
            raise AssertionError("advisor main imposed a deadline on an implicit prompt-only call")
        events.append("call")
        return "coordinated guidance"

    def fake_write(_args: argparse.Namespace, _guidance: str) -> list[Path]:
        events.append("write")
        return []

    args = argparse.Namespace(
        prompt="test prompt",
        context_file=[],
        model=None,
        thinking_effort=None,
        provider="openai-compatible",
        timeout=7,
        save=None,
        allow_outside_project=False,
        live_activity=None,
    )
    replacements = {
        "configure_stdio": lambda: None,
        "parse_args": lambda: args,
        "coordinated_state_path": lambda _timeout: state_path,
        "call_compatible": fake_call,
        "write_guidance_outputs": fake_write,
    }
    old = {name: getattr(advisor, name) for name in replacements}
    old_coordinated_call = concurrency.coordinated_call
    old_base = os.environ.get("ADVISOR_BASE_URL")
    old_timeout = os.environ.get("ADVISOR_TIMEOUT")
    try:
        for name, value in replacements.items():
            setattr(advisor, name, value)
        concurrency.coordinated_call = fake_coordinated_call
        os.environ["ADVISOR_BASE_URL"] = original_base_url
        os.environ.pop("ADVISOR_TIMEOUT", None)
        if advisor.main() != 0:
            raise AssertionError("advisor main returned failure")
    finally:
        for name, value in old.items():
            setattr(advisor, name, value)
        concurrency.coordinated_call = old_coordinated_call
        if old_base is None:
            os.environ.pop("ADVISOR_BASE_URL", None)
        else:
            os.environ["ADVISOR_BASE_URL"] = old_base
        if old_timeout is None:
            os.environ.pop("ADVISOR_TIMEOUT", None)
        else:
            os.environ["ADVISOR_TIMEOUT"] = old_timeout
    if events != ["enter", "call", "write", "exit"]:
        raise AssertionError(f"advisor main coordination order changed: {events!r}")
    if not lease.success or lease.failure:
        raise AssertionError("advisor main did not report worker success")
    if os.environ.get("ADVISOR_BASE_URL") != old_base:
        raise AssertionError("advisor main did not restore ADVISOR_BASE_URL")


def test_remote_start_is_marked_at_request_boundary() -> None:
    events: list[str] = []

    class RemoteLease:
        def mark_start(self) -> None:
            events.append("remote-start")

    @contextlib.contextmanager
    def fake_remote(
        _timeout: float | None,
        *,
        defer_start: bool = False,
        exclusive: bool = False,
    ):
        del exclusive
        if not defer_start:
            raise AssertionError("coordinated_call did not defer pacing until worker readiness")
        events.append("remote-enter")
        try:
            yield RemoteLease()
        finally:
            events.append("remote-exit")

    @contextlib.contextmanager
    def fake_worker(_url: str, _timeout: float | None):
        events.append("worker-ready")
        try:
            yield object()
        finally:
            events.append("worker-exit")

    original_remote = concurrency.remote_call_slot
    original_worker = concurrency.worker_lease
    try:
        concurrency.remote_call_slot = fake_remote
        concurrency.worker_lease = fake_worker
        with concurrency.coordinated_call(
            "http://127.0.0.1:8080/v1",
            None,
            request_timeout=10,
        ):
            events.append("preflight")
            concurrency.mark_active_remote_start()
            events.append("request")
    finally:
        concurrency.remote_call_slot = original_remote
        concurrency.worker_lease = original_worker
    if events != [
        "remote-enter",
        "worker-ready",
        "preflight",
        "remote-start",
        "request",
        "worker-exit",
        "remote-exit",
    ]:
        raise AssertionError(f"remote start pacing did not immediately precede the request: {events!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_two_worker_capacity(root)
        test_fifo_single_worker(root)
        test_same_conversation_serializes(root)
        test_same_conversation_across_state_files_serializes(root)
        test_remote_admission_capacity(root)
        test_remote_admission_fifo(root)
        test_remote_rate_limit_temporarily_serializes(root)
        test_supervisor_remote_capacity_is_authoritative(root)
        test_capacity_shrink_waits_for_retired_slots(root)
        test_capacity_one_first_turn_alias_does_not_deadlock(root)
        test_first_turn_transition_serializes(root)
        test_first_turn_cross_state_upgrade_serializes(root)
        test_stale_and_timed_out_queue_tickets_are_removed(root)
        test_pool_degrades_after_cross_worker_failures(root)
        test_pool_helpers()
        test_loopback_transport_is_proxy_free_and_rejects_redirects()
        test_process_identity_uncertainty_fails_closed()
        test_recorded_transient_log_cleanup(root)
        test_cancelled_transient_start_does_not_leak_worker(root)
        test_exited_transient_worker_removes_log_after_request_race(root)
        test_pool_supervisor_lifecycle(root)
        test_transient_supervisor_lifecycle(root)
        test_advisor_main_coordination(root)
        test_remote_start_is_marked_at_request_boundary()
    print("Advisor concurrency tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
