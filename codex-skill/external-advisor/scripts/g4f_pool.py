#!/usr/bin/env python3
"""Supervise isolated local g4f worker processes for advisor concurrency."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import advisor_concurrency as concurrency
import advisor_safety as safety


def port_bindable(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def endpoint_ready(url: str, timeout: float = 1.0) -> bool:
    request = urllib.request.Request(f"{url.rstrip('/')}/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def worker_ports(base_port: int, workers: int, step: int) -> list[int]:
    return [base_port + index * step for index in range(workers)]


def worker_command(python: Path, port: int, debug: bool) -> list[str]:
    command = [str(python), "-m", "g4f", "api", "--port", str(port)]
    if debug:
        command.append("--debug")
    return command


def executable_path(path: Path) -> Path:
    # A venv's Python is commonly a symlink to the system interpreter. Keep
    # the venv path so Python activates the correct sys.prefix and site-packages.
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def terminate_process(process: subprocess.Popen[Any], timeout: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        pass


def remove_owned_manifest(run_id: str) -> None:
    path = concurrency.pool_manifest_path()
    payload = concurrency.load_json(path)
    if payload.get("run_id") == run_id and int(payload.get("manager_pid") or 0) == os.getpid():
        path.unlink(missing_ok=True)


def active_manifest_ready() -> bool:
    payload = concurrency.load_json(concurrency.pool_manifest_path())
    manager_pid = int(payload.get("manager_pid") or 0)
    if not concurrency.process_alive(manager_pid, str(payload.get("manager_identity") or "")):
        return False
    workers = payload.get("workers")
    if not isinstance(workers, list) or not workers:
        return False
    for item in workers:
        if not isinstance(item, dict):
            return False
        pid = int(item.get("pid") or 0)
        identity = str(item.get("process_identity") or "")
        url = str(item.get("url") or "")
        if not concurrency.process_alive(pid, identity) or not url or not endpoint_ready(url):
            return False
    return True


def command_serve(args: argparse.Namespace) -> int:
    manager_lock = concurrency.InterProcessLock(
        concurrency.runtime_root() / "g4f-pool-manager.lock",
        timeout=0.0,
    )
    deadline = time.monotonic() + args.startup_timeout
    while not manager_lock.try_acquire():
        if active_manifest_ready():
            print("A healthy g4f worker pool is already running; reusing it.", flush=True)
            return 0
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for another g4f pool startup to finish.")
        time.sleep(0.25)
    try:
        return command_serve_locked(args)
    finally:
        manager_lock.release()


def command_serve_locked(args: argparse.Namespace) -> int:
    python = executable_path(args.python)
    g4f_dir = args.g4f_dir.expanduser().resolve()
    if not python.exists():
        raise RuntimeError(f"g4f Python executable does not exist: {python}")
    if not (g4f_dir / "g4f").is_dir():
        raise RuntimeError(f"g4f package directory does not exist: {g4f_dir / 'g4f'}")
    if args.workers < 1 or args.workers > args.max_workers:
        raise RuntimeError(f"Worker count must be between 1 and {args.max_workers}.")
    ports = worker_ports(args.port, args.workers, args.port_step)
    if len(set(ports)) != len(ports) or any(port <= 0 or port > 65535 for port in ports):
        raise RuntimeError("g4f worker ports are invalid or overlap.")

    deadline = time.monotonic() + args.port_wait_seconds
    while True:
        busy = [port for port in ports if not port_bindable(port)]
        if not busy:
            break
        if time.monotonic() >= deadline:
            joined = ", ".join(str(port) for port in busy)
            raise RuntimeError(
                f"g4f worker port(s) already in use: {joined}. Stop the existing server or change G4F_PORT/G4F_WORKERS."
            )
        time.sleep(1.0)

    env = os.environ.copy()
    env["G4F_PROVIDER"] = args.provider
    env["G4F_MODEL"] = args.model
    run_id = uuid.uuid4().hex
    processes: list[subprocess.Popen[Any]] = []
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    try:
        print(
            f"Starting g4f worker pool: workers={args.workers} "
            f"ports={','.join(str(port) for port in ports)}",
            flush=True,
        )
        print(f"Provider: {args.provider}", flush=True)
        print(f"Model: {args.model}", flush=True)
        for index, port in enumerate(ports):
            process = subprocess.Popen(
                worker_command(python, port, args.debug),
                cwd=g4f_dir,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            processes.append(process)
            print(f"Worker {index + 1}: http://127.0.0.1:{port}/v1 (pid {process.pid})", flush=True)

        ready_deadline = time.monotonic() + args.startup_timeout
        ready: set[int] = set()
        while len(ready) < len(processes):
            for index, (process, port) in enumerate(zip(processes, ports)):
                if process.poll() is not None:
                    raise RuntimeError(
                        f"g4f worker {index + 1} exited during startup with code {process.returncode}."
                    )
                if index not in ready and endpoint_ready(f"http://127.0.0.1:{port}/v1"):
                    ready.add(index)
            if len(ready) == len(processes):
                break
            if time.monotonic() >= ready_deadline:
                missing = ", ".join(str(ports[index]) for index in range(len(ports)) if index not in ready)
                raise RuntimeError(f"g4f workers did not become ready before timeout: {missing}")
            time.sleep(0.25)

        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "manager_pid": os.getpid(),
            "manager_identity": concurrency.process_identity(os.getpid()),
            "started_at": time.time(),
            "model": args.model,
            "provider": args.provider,
            "workers": [
                {
                    "index": index,
                    "port": port,
                    "url": f"http://127.0.0.1:{port}/v1",
                    "pid": process.pid,
                    "process_identity": concurrency.process_identity(process.pid),
                }
                for index, (process, port) in enumerate(zip(processes, ports))
            ],
        }
        safety.atomic_write_json(concurrency.pool_manifest_path(), manifest, sort_keys=True)
        print(f"g4f worker pool ready. Manifest: {concurrency.pool_manifest_path()}", flush=True)

        while not stopping:
            for index, process in enumerate(processes):
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"g4f worker {index + 1} exited with code {code}; stopping the pool.")
            time.sleep(0.5)
        return 0
    finally:
        remove_owned_manifest(run_id)
        for process in reversed(processes):
            terminate_process(process)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def command_status(_args: argparse.Namespace) -> int:
    path = concurrency.pool_manifest_path()
    payload = concurrency.load_json(path)
    workers = payload.get("workers") if isinstance(payload.get("workers"), list) else []
    manager_pid = int(payload.get("manager_pid") or 0)
    manager_alive = concurrency.process_alive(manager_pid, str(payload.get("manager_identity") or ""))
    print(f"manifest: {path}")
    print(f"manager_running: {'yes' if manager_alive else 'no'}")
    live_workers = 0
    for item in workers:
        if not isinstance(item, dict):
            continue
        alive = concurrency.process_alive(int(item.get("pid") or 0), str(item.get("process_identity") or ""))
        if alive:
            live_workers += 1
        print(f"worker_{int(item.get('index') or 0) + 1}: {'running' if alive else 'stopped'} {item.get('url') or ''}")
    print(f"live_workers: {live_workers}")
    return 0 if manager_alive and live_workers == len(workers) and live_workers > 0 else 1


def command_stop(args: argparse.Namespace) -> int:
    path = concurrency.pool_manifest_path()
    payload = concurrency.load_json(path)
    manager_pid = int(payload.get("manager_pid") or 0)
    manager_identity = str(payload.get("manager_identity") or "")
    if not concurrency.process_alive(manager_pid, manager_identity):
        path.unlink(missing_ok=True)
        print("g4f worker pool is not running.")
        return 0
    try:
        os.kill(manager_pid, signal.SIGTERM)
    except OSError as exc:
        raise RuntimeError(f"Could not stop g4f pool manager pid {manager_pid}: {exc}") from exc
    deadline = time.monotonic() + args.timeout
    while concurrency.process_alive(manager_pid, manager_identity):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"g4f pool manager pid {manager_pid} did not stop within {args.timeout:.0f}s."
            )
        time.sleep(0.1)
    path.unlink(missing_ok=True)
    print("g4f worker pool stopped.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start and supervise isolated g4f workers.")
    serve.add_argument("--python", type=Path, required=True)
    serve.add_argument("--g4f-dir", type=Path, required=True)
    serve.add_argument("--port", type=int, default=int(os.environ.get("G4F_PORT", "8080")))
    serve.add_argument("--workers", type=int, default=int(os.environ.get("G4F_WORKERS", "2")))
    serve.add_argument("--max-workers", type=int, default=int(os.environ.get("G4F_MAX_WORKERS", "4")))
    serve.add_argument("--port-step", type=int, default=int(os.environ.get("G4F_WORKER_PORT_STEP", "1")))
    serve.add_argument("--model", default=os.environ.get("G4F_MODEL", "gpt-5-6-thinking"))
    serve.add_argument("--provider", default=os.environ.get("G4F_PROVIDER", "OpenaiAccount"))
    serve.add_argument("--startup-timeout", type=float, default=float(os.environ.get("G4F_STARTUP_TIMEOUT", "30")))
    serve.add_argument("--port-wait-seconds", type=float, default=float(os.environ.get("G4F_PORT_WAIT_SECONDS", "15")))
    serve.add_argument("--debug", action="store_true")
    serve.set_defaults(func=command_serve)

    status = subparsers.add_parser("status", help="Show the current worker-pool status.")
    status.set_defaults(func=command_status)

    stop = subparsers.add_parser("stop", help="Stop the current worker pool cleanly.")
    stop.add_argument("--timeout", type=float, default=15.0)
    stop.set_defaults(func=command_stop)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"g4f pool error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
