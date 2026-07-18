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
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def endpoint_ready(url: str, timeout: float = 1.0) -> bool:
    request = urllib.request.Request(f"{url.rstrip('/')}/models", method="GET")
    try:
        with concurrency.open_loopback_url(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, RuntimeError, urllib.error.URLError):
        return False


def worker_ports(base_port: int, workers: int, step: int) -> list[int]:
    return [base_port + index * step for index in range(workers)]


def worker_command(python: Path, port: int, debug: bool) -> list[str]:
    command = [
        str(python),
        "-m",
        "g4f",
        "api",
        "--bind",
        f"127.0.0.1:{port}",
        "--port",
        str(port),
    ]
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
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
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
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        try:
            process.kill()
        except OSError:
            return
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        pass


def transient_log_dir(run_id: str) -> Path:
    path = concurrency.runtime_root() / "transient-logs" / concurrency.key_digest(run_id, 32)
    safety.ensure_private_dir(path)
    return path


def update_request(path: Path, **updates: Any) -> dict[str, Any]:
    with concurrency.transient_request_lock(path):
        if concurrency.transient_release_path(path).exists() or not path.exists():
            return concurrency.load_json(path)
        payload = concurrency.load_json(path)
        if not payload:
            return {}
        payload.update(updates)
        safety.atomic_write_json(path, payload, sort_keys=True)
        return payload


def validated_transient_log_path(raw_path: str | Path | None) -> Path | None:
    return concurrency.validated_transient_log_path(raw_path)


def recorded_transient_log_path(path: Path) -> Path | None:
    return validated_transient_log_path(concurrency.load_json(path).get("log_path"))


def remove_request_artifacts(path: Path, log_path: Path | None = None) -> None:
    with concurrency.transient_request_lock(path):
        if log_path is None:
            log_path = recorded_transient_log_path(path)
        else:
            log_path = validated_transient_log_path(log_path)
        path.unlink(missing_ok=True)
        if log_path is not None:
            log_path.unlink(missing_ok=True)
        concurrency.transient_release_path(path).unlink(missing_ok=True)


def reap_stale_transient_requests() -> None:
    root = concurrency.runtime_root() / "transient-requests"
    if not root.exists():
        return
    for path in root.glob("*/*.json"):
        payload = concurrency.load_json(path)
        concurrency.terminate_external_process(
            int(payload.get("worker_pid") or 0),
            str(payload.get("worker_identity") or ""),
        )
        remove_request_artifacts(path)


def start_transient_worker(
    *,
    request_path: Path,
    payload: dict[str, Any],
    python: Path,
    g4f_dir: Path,
    env: dict[str, str],
    port: int,
    debug: bool,
    run_id: str,
    startup_timeout: float,
) -> dict[str, Any]:
    log_path = transient_log_dir(run_id) / f"{request_path.stem}.log"
    log_handle = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            worker_command(python, port, debug),
            cwd=g4f_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
    except BaseException:
        log_handle.close()
        log_path.unlink(missing_ok=True)
        raise
    url = f"http://127.0.0.1:{port}/v1"
    try:
        registered = update_request(
            request_path,
            status="starting",
            url=url,
            worker_pid=process.pid,
            worker_identity=concurrency.process_identity(process.pid),
            log_path=str(log_path),
            started_at=time.time(),
        )
        if (
            registered.get("status") != "starting"
            or int(registered.get("worker_pid") or 0) != process.pid
        ):
            raise RuntimeError("transient request was released before worker registration")
    except BaseException:
        terminate_process(process)
        log_handle.close()
        log_path.unlink(missing_ok=True)
        raise
    return {
        "request_path": request_path,
        "process": process,
        "log_handle": log_handle,
        "log_path": log_path,
        "url": url,
        "slot": int(payload.get("slot") or 0),
        "startup_deadline": time.monotonic() + startup_timeout,
        "ready": False,
    }


def stop_transient_worker(worker: dict[str, Any]) -> None:
    process = worker.get("process")
    if isinstance(process, subprocess.Popen):
        terminate_process(process)
    log_handle = worker.get("log_handle")
    if log_handle is not None:
        try:
            log_handle.close()
        except OSError:
            pass


def service_transient_requests(
    *,
    request_dir: Path,
    active: dict[str, dict[str, Any]],
    python: Path,
    g4f_dir: Path,
    env: dict[str, str],
    run_id: str,
    max_workers: int,
    port_base: int,
    port_step: int,
    debug: bool,
    startup_timeout: float,
) -> None:
    active_slots = {int(worker["slot"]) for worker in active.values()}
    for path in sorted(request_dir.glob("*.json"), key=lambda item: item.name):
        request_id = path.stem
        payload = concurrency.load_json(path)
        release_path = concurrency.transient_release_path(path)
        owner_alive = concurrency.process_alive(
            int(payload.get("owner_pid") or 0),
            str(payload.get("owner_identity") or ""),
        )
        worker = active.get(request_id)
        if release_path.exists() or not owner_alive or payload.get("run_id") != run_id:
            if worker is not None:
                stop_transient_worker(worker)
                active.pop(request_id, None)
                active_slots.discard(int(worker["slot"]))
                remove_request_artifacts(path, worker.get("log_path"))
            else:
                remove_request_artifacts(path)
            continue
        if worker is not None:
            continue
        if payload.get("status") != "pending":
            continue
        slot = int(payload.get("slot") if payload.get("slot") is not None else -1)
        if slot < 0 or slot >= max_workers:
            update_request(path, status="failed", error="assigned transient slot is invalid")
            continue
        if slot in active_slots:
            continue
        port = port_base + slot * port_step
        if not port_bindable(port):
            update_request(path, status="failed", error="assigned transient worker port is unavailable")
            continue
        try:
            worker = start_transient_worker(
                request_path=path,
                payload=payload,
                python=python,
                g4f_dir=g4f_dir,
                env=env,
                port=port,
                debug=debug,
                run_id=run_id,
                startup_timeout=startup_timeout,
            )
        except (OSError, RuntimeError) as exc:
            update_request(path, status="failed", error=f"worker process could not start: {exc}")
            continue
        active[request_id] = worker
        active_slots.add(slot)

    for request_id, worker in list(active.items()):
        path = worker["request_path"]
        process = worker["process"]
        if concurrency.transient_release_path(path).exists():
            stop_transient_worker(worker)
            active.pop(request_id, None)
            remove_request_artifacts(path, worker.get("log_path"))
            continue
        if process.poll() is not None:
            worker["log_handle"].close()
            active.pop(request_id, None)
            updated = update_request(
                path,
                status="failed",
                error=f"worker exited with status {process.returncode}",
            )
            if not updated:
                # The caller can release/remove the request between the
                # release check above and process exit. The supervisor still
                # owns the closed private log and must remove it explicitly.
                log_path = validated_transient_log_path(worker.get("log_path"))
                if log_path is not None:
                    log_path.unlink(missing_ok=True)
            continue
        if not worker["ready"] and endpoint_ready(worker["url"]):
            worker["ready"] = True
            update_request(path, status="ready", ready_at=time.time())
            continue
        if not worker["ready"] and time.monotonic() >= worker["startup_deadline"]:
            stop_transient_worker(worker)
            active.pop(request_id, None)
            update_request(path, status="failed", error="worker did not become ready before startup timeout")


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
    transient_port_base = args.transient_port_base or args.port + 100
    transient_ports = worker_ports(
        transient_port_base,
        args.max_transient_workers,
        args.transient_port_step,
    )
    if args.mode == "transient":
        if args.max_transient_workers < 1:
            raise RuntimeError("The transient-worker ceiling must be at least one.")
        if (
            len(set(transient_ports)) != len(transient_ports)
            or any(port <= 0 or port > 65535 for port in transient_ports)
            or set(ports).intersection(transient_ports)
        ):
            raise RuntimeError("Transient g4f worker ports are invalid or overlap control-worker ports.")

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
    transient_processes: dict[str, dict[str, Any]] = {}
    request_dir = concurrency.transient_request_dir(run_id)
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    try:
        reap_stale_transient_requests()
        print(
            f"Starting g4f advisor supervisor: mode={args.mode} control_workers={args.workers} "
            f"ports={','.join(str(port) for port in ports)}",
            flush=True,
        )
        if args.mode == "transient":
            print(
                f"Transient call workers: on-demand, ceiling={args.max_transient_workers}, "
                f"ports={transient_port_base}-{transient_ports[-1]}",
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
                start_new_session=os.name == "posix",
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
            "schema_version": 2,
            "run_id": run_id,
            "manager_pid": os.getpid(),
            "manager_identity": concurrency.process_identity(os.getpid()),
            "started_at": time.time(),
            "mode": args.mode,
            "model": args.model,
            "provider": args.provider,
            "remote_chatgpt_capacity": concurrency.int_env(
                "ADVISOR_REMOTE_MAX_CONCURRENCY",
                concurrency.DEFAULT_REMOTE_CONCURRENCY,
            ),
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
        if args.mode == "transient":
            manifest["transient"] = {
                "max_workers": args.max_transient_workers,
                "port_base": transient_port_base,
                "port_step": args.transient_port_step,
                "startup_timeout": args.transient_startup_timeout,
            }
        safety.atomic_write_json(concurrency.pool_manifest_path(), manifest, sort_keys=True)
        print(f"g4f advisor supervisor ready. Manifest: {concurrency.pool_manifest_path()}", flush=True)

        while not stopping:
            for index, process in enumerate(processes):
                code = process.poll()
                if code is not None:
                    raise RuntimeError(f"g4f control worker {index + 1} exited with code {code}; stopping the pool.")
            if args.mode == "transient":
                service_transient_requests(
                    request_dir=request_dir,
                    active=transient_processes,
                    python=python,
                    g4f_dir=g4f_dir,
                    env=env,
                    run_id=run_id,
                    max_workers=args.max_transient_workers,
                    port_base=transient_port_base,
                    port_step=args.transient_port_step,
                    debug=args.debug,
                    startup_timeout=args.transient_startup_timeout,
                )
            time.sleep(0.1)
        return 0
    finally:
        remove_owned_manifest(run_id)
        for worker in list(transient_processes.values()):
            stop_transient_worker(worker)
            remove_request_artifacts(worker["request_path"], worker.get("log_path"))
        transient_processes.clear()
        for path in request_dir.glob("*.json"):
            payload = concurrency.load_json(path)
            concurrency.terminate_external_process(
                int(payload.get("worker_pid") or 0),
                str(payload.get("worker_identity") or ""),
            )
            remove_request_artifacts(path)
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
    print(f"mode: {payload.get('mode') or 'fixed'}")
    live_workers = 0
    for item in workers:
        if not isinstance(item, dict):
            continue
        alive = concurrency.process_alive(int(item.get("pid") or 0), str(item.get("process_identity") or ""))
        if alive:
            live_workers += 1
        print(f"worker_{int(item.get('index') or 0) + 1}: {'running' if alive else 'stopped'} {item.get('url') or ''}")
    print(f"live_workers: {live_workers}")
    active_transient = 0
    if payload.get("mode") == "transient" and payload.get("run_id"):
        for request_path in concurrency.transient_request_dir(str(payload["run_id"])).glob("*.json"):
            request = concurrency.load_json(request_path)
            if concurrency.process_alive(
                int(request.get("worker_pid") or 0),
                str(request.get("worker_identity") or ""),
            ):
                active_transient += 1
        transient = payload.get("transient") if isinstance(payload.get("transient"), dict) else {}
        print(f"active_transient_workers: {active_transient}")
        print(f"transient_worker_ceiling: {int(transient.get('max_workers') or 0)}")
    configured_remote = concurrency.configured_remote_capacity()
    effective_remote, remote_degraded = concurrency.remote_concurrency_limit()
    occupied_remote_slots = 0
    occupied_retired_slots = 0
    for slot in concurrency.known_remote_slot_indexes(configured_remote):
        lock = concurrency.InterProcessLock(
            concurrency.runtime_root() / "remote-slots" / f"slot-{slot}.lock",
            timeout=0.0,
        )
        if lock.try_acquire():
            lock.release()
        else:
            occupied_remote_slots += 1
            if slot >= configured_remote:
                occupied_retired_slots += 1
    remote_queue = concurrency.runtime_root() / "queues" / "remote-chatgpt"
    concurrency.cleanup_stale_tickets(remote_queue)
    queued_remote = len(list(remote_queue.glob("*.json"))) if remote_queue.exists() else 0
    print(f"remote_chatgpt_capacity: {effective_remote}")
    print(f"remote_chatgpt_degraded: {'yes' if remote_degraded else 'no'}")
    print(f"occupied_remote_slots: {occupied_remote_slots}")
    print(f"occupied_retired_remote_slots: {occupied_retired_slots}")
    print(f"queued_remote_calls: {queued_remote}")
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

    serve = subparsers.add_parser("serve", help="Start the g4f advisor worker supervisor.")
    serve.add_argument("--python", type=Path, required=True)
    serve.add_argument("--g4f-dir", type=Path, required=True)
    serve.add_argument("--port", type=int, default=int(os.environ.get("G4F_PORT", "8080")))
    serve.add_argument(
        "--mode",
        choices=["transient", "fixed"],
        default=os.environ.get("G4F_WORKER_MODE", "transient"),
        help="Use one disposable worker per call (default) or a fixed compatibility pool.",
    )
    serve.add_argument("--workers", type=int, default=int(os.environ.get("G4F_WORKERS", "1")))
    serve.add_argument("--max-workers", type=int, default=int(os.environ.get("G4F_MAX_WORKERS", "4")))
    serve.add_argument("--port-step", type=int, default=int(os.environ.get("G4F_WORKER_PORT_STEP", "1")))
    transient_base = os.environ.get("G4F_TRANSIENT_PORT_BASE")
    serve.add_argument(
        "--transient-port-base",
        type=int,
        default=int(transient_base) if transient_base else None,
    )
    serve.add_argument(
        "--transient-port-step",
        type=int,
        default=int(os.environ.get("G4F_TRANSIENT_PORT_STEP", "1")),
    )
    serve.add_argument(
        "--max-transient-workers",
        type=int,
        default=int(os.environ.get("G4F_MAX_TRANSIENT_WORKERS", "32")),
        help="Emergency ceiling for simultaneous disposable call workers.",
    )
    serve.add_argument(
        "--transient-startup-timeout",
        type=float,
        default=float(os.environ.get("G4F_TRANSIENT_STARTUP_TIMEOUT", "45")),
    )
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
