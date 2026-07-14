#!/usr/bin/env python3
"""Cross-process coordination for local g4f-backed advisor calls."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import advisor_safety as safety


DEFAULT_QUEUE_TIMEOUT_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 0.2
DEFAULT_FAILURE_WINDOW_SECONDS = 120.0
DEFAULT_DEGRADE_SECONDS = 300.0
DEFAULT_DEGRADE_FAILURES = 3


def bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def float_env(name: str, default: float, minimum: float = 0.0) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {value!r}.") from exc
    return max(minimum, parsed)


def int_env(name: str, default: int, minimum: int = 1) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {value!r}.") from exc
    return max(minimum, parsed)


def runtime_root() -> Path:
    explicit = os.environ.get("ADVISOR_RUNTIME_DIR")
    if explicit:
        root = Path(explicit).expanduser()
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        root = codex_home / "advisor-runtime"
    safety.ensure_private_dir(root)
    return root.resolve()


def pool_manifest_path() -> Path:
    explicit = os.environ.get("ADVISOR_POOL_MANIFEST")
    return Path(explicit).expanduser() if explicit else runtime_root() / "g4f-pool.json"


def normalized_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def local_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def process_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "posix":
        try:
            stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            return f"{boot_id}:{stat_fields[21]}"
        except (OSError, IndexError):
            pass
    return ""


def process_state(pid: int) -> str:
    if pid <= 0 or os.name != "posix":
        return ""
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return stat_fields[2]
    except (OSError, IndexError):
        return ""


def process_alive(pid: int, expected_identity: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    if process_state(pid) == "Z":
        return False
    if expected_identity:
        current = process_identity(pid)
        if current and current != expected_identity:
            return False
    return True


class InterProcessLock:
    """One-byte OS lock that is released automatically when its process exits."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        wait_message: str = "",
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.wait_message = wait_message
        self.handle: Any = None
        self.waited_seconds = 0.0

    def _open(self) -> None:
        safety.ensure_private_dir(self.path.parent)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()

    def _try_lock(self) -> bool:
        if self.handle is None:
            self._open()
        assert self.handle is not None
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, 13, 36}:
                self.release()
                raise
            self.handle.close()
            self.handle = None
            return False

    def try_acquire(self) -> bool:
        return self._try_lock()

    def acquire(self) -> "InterProcessLock":
        started = time.monotonic()
        announced = False
        while not self._try_lock():
            elapsed = time.monotonic() - started
            if elapsed >= self.timeout:
                raise RuntimeError(
                    f"Timed out after {self.timeout:.0f}s waiting for advisor coordination lock."
                )
            if self.wait_message and not announced and elapsed >= 1.0:
                print(self.wait_message, file=sys.stderr)
                announced = True
            time.sleep(self.poll_seconds)
        self.waited_seconds = time.monotonic() - started
        return self

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "InterProcessLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.release()
        return False


def key_digest(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def project_binding_lock(project_dir: Path, timeout: float = 60.0) -> InterProcessLock:
    key = key_digest(str(project_dir.resolve()))
    return InterProcessLock(
        runtime_root() / "project-bindings" / f"{key}.lock",
        timeout=timeout,
        wait_message="Advisor is waiting for another session to finish project binding setup.",
    )


def conversation_lock_keys(state_path: Path | None) -> list[str]:
    if state_path is None:
        return []
    conversation_id = ""
    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            conversation = payload.get("conversation") if isinstance(payload, dict) else None
            if isinstance(conversation, dict) and isinstance(conversation.get("conversation_id"), str):
                conversation_id = conversation["conversation_id"].strip()
        except (OSError, json.JSONDecodeError):
            pass
    # Always retain the state-path lock. The first turn starts without a
    # conversation id and writes one during the request; using only the id
    # afterward would let a second process cross that transition concurrently.
    keys = ["state:" + str(state_path.resolve())]
    if conversation_id:
        keys.append("conversation:" + conversation_id)
    return keys


@contextlib.contextmanager
def conversation_lock(state_path: Path | None, timeout: float) -> Iterator[None]:
    keys = conversation_lock_keys(state_path)
    if not keys:
        yield
        return
    locks = [
        InterProcessLock(
            runtime_root() / "conversations" / f"{key_digest(key)}.lock",
            timeout=timeout,
            wait_message="Advisor queued behind an earlier turn in the same ChatGPT conversation.",
        )
        for key in sorted(keys)
    ]
    with contextlib.ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        yield


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def manifest_worker_urls(configured_base_url: str) -> list[str]:
    if not bool_env("ADVISOR_POOL_ENABLED", True):
        return [configured_base_url]
    explicit = os.environ.get("ADVISOR_POOL_WORKER_URLS")
    if explicit:
        values = [normalized_base_url(item) for item in explicit.split(",") if item.strip()]
        if not values:
            raise RuntimeError("ADVISOR_POOL_WORKER_URLS did not contain any URLs.")
        if not all(local_http_url(item) for item in values):
            raise RuntimeError("Advisor worker-pool URLs must use loopback HTTP(S) addresses.")
        return list(dict.fromkeys(values))

    manifest = load_json(pool_manifest_path())
    workers = manifest.get("workers")
    manager_pid = int(manifest.get("manager_pid") or 0)
    manager_identity = str(manifest.get("manager_identity") or "")
    if not isinstance(workers, list) or not process_alive(manager_pid, manager_identity):
        return [configured_base_url]
    values: list[str] = []
    for item in workers:
        if not isinstance(item, dict):
            continue
        url = normalized_base_url(str(item.get("url") or ""))
        pid = int(item.get("pid") or 0)
        identity = str(item.get("process_identity") or "")
        if url and local_http_url(url) and process_alive(pid, identity):
            values.append(url)
    values = list(dict.fromkeys(values))
    if not values or normalized_base_url(configured_base_url) not in values:
        return [configured_base_url]
    return values


def pool_id(urls: list[str]) -> str:
    return key_digest("|".join(urls), 16)


def health_state_path() -> Path:
    return runtime_root() / "pool-health.json"


def health_lock() -> InterProcessLock:
    return InterProcessLock(runtime_root() / "pool-health.lock", timeout=10.0)


def read_health_state() -> dict[str, Any]:
    return load_json(health_state_path())


def write_health_state(state: dict[str, Any]) -> None:
    safety.atomic_write_json(health_state_path(), state, sort_keys=True)


def degraded_pool(urls: list[str], now: float | None = None) -> bool:
    if len(urls) <= 1:
        return False
    current = time.time() if now is None else now
    with health_lock():
        state = read_health_state()
        if state.get("pool_id") != pool_id(urls):
            return False
        degraded_until = float(state.get("degraded_until") or 0.0)
        if degraded_until <= current:
            if degraded_until:
                state["degraded_until"] = 0.0
                write_health_state(state)
            return False
        return True


def preferred_degraded_worker(urls: list[str]) -> str:
    if not urls:
        raise RuntimeError("Advisor worker pool has no configured workers.")
    with health_lock():
        state = read_health_state()
        if state.get("pool_id") != pool_id(urls):
            return urls[0]
        events = state.get("failures")
        if not isinstance(events, list):
            events = []
        counts = {key_digest(url, 12): 0 for url in urls}
        for item in events:
            if isinstance(item, dict):
                worker = str(item.get("worker") or "")
                if worker in counts:
                    counts[worker] += 1
        last_success = state.get("last_success")
        last_success_worker = (
            str(last_success.get("worker") or "") if isinstance(last_success, dict) else ""
        )
        return min(
            urls,
            key=lambda url: (
                counts[key_digest(url, 12)],
                0 if key_digest(url, 12) == last_success_worker else 1,
                urls.index(url),
            ),
        )


def record_transport_failure(url: str, urls: list[str], now: float | None = None) -> None:
    if len(urls) <= 1:
        return
    current = time.time() if now is None else now
    window = float_env("ADVISOR_POOL_FAILURE_WINDOW_SECONDS", DEFAULT_FAILURE_WINDOW_SECONDS, 1.0)
    threshold = int_env("ADVISOR_POOL_DEGRADE_FAILURES", DEFAULT_DEGRADE_FAILURES)
    cooldown = float_env("ADVISOR_POOL_DEGRADE_SECONDS", DEFAULT_DEGRADE_SECONDS, 1.0)
    current_pool_id = pool_id(urls)
    with health_lock():
        previous_state = read_health_state()
        same_pool = previous_state.get("pool_id") == current_pool_id
        events = previous_state.get("failures") if same_pool else []
        if not isinstance(events, list):
            events = []
        events = [
            item for item in events
            if isinstance(item, dict) and float(item.get("time") or 0.0) >= current - window
        ]
        events.append({"time": current, "worker": key_digest(url, 12)})
        state = {"pool_id": current_pool_id, "failures": events}
        if same_pool and isinstance(previous_state.get("last_success"), dict):
            state["last_success"] = previous_state["last_success"]
        previous_degraded_until = float(previous_state.get("degraded_until") or 0.0) if same_pool else 0.0
        if previous_degraded_until > current:
            state["degraded_until"] = previous_degraded_until
        distinct_workers = {str(item.get("worker") or "") for item in events}
        if len(events) >= threshold and len(distinct_workers) >= min(2, len(urls)):
            state["degraded_until"] = max(float(state.get("degraded_until") or 0.0), current + cooldown)
        write_health_state(state)


def record_transport_success(url: str, urls: list[str], now: float | None = None) -> None:
    if len(urls) <= 1:
        return
    current = time.time() if now is None else now
    current_pool_id = pool_id(urls)
    with health_lock():
        state = read_health_state()
        if state.get("pool_id") != current_pool_id:
            state = {"pool_id": current_pool_id, "failures": [], "degraded_until": 0.0}
        state["last_success"] = {"time": current, "worker": key_digest(url, 12)}
        write_health_state(state)


def transport_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "http 500",
        "error in message stream",
        "connection refused",
        "connection reset",
        "remote end closed",
        "incompleteread",
        "websocket",
        "timed out",
    )
    return any(marker in text for marker in markers)


def ticket_owner_alive(path: Path) -> bool:
    payload = load_json(path)
    return process_alive(int(payload.get("pid") or 0), str(payload.get("process_identity") or ""))


def cleanup_stale_tickets(queue_dir: Path) -> None:
    if not queue_dir.exists():
        return
    for path in queue_dir.glob("*.json"):
        if not ticket_owner_alive(path):
            path.unlink(missing_ok=True)


@dataclass
class WorkerLease:
    url: str
    index: int
    all_urls: list[str]
    active_urls: list[str]
    waited_seconds: float
    degraded: bool
    lock: InterProcessLock

    def report_failure(self) -> None:
        record_transport_failure(self.url, self.all_urls)

    def report_success(self) -> None:
        record_transport_success(self.url, self.all_urls)


@contextlib.contextmanager
def worker_lease(configured_base_url: str, timeout: float) -> Iterator[WorkerLease]:
    all_urls = manifest_worker_urls(normalized_base_url(configured_base_url))
    is_degraded = degraded_pool(all_urls)
    active_urls = [preferred_degraded_worker(all_urls)] if is_degraded else all_urls
    if is_degraded:
        print(
            "Advisor worker pool is temporarily degraded to one worker after repeated transport failures.",
            file=sys.stderr,
        )
    current_pool_id = pool_id(all_urls)
    queue_dir = runtime_root() / "queues" / current_pool_id
    safety.ensure_private_dir(queue_dir)
    ticket_id = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex}"
    ticket_path = queue_dir / f"{ticket_id}.json"
    safety.atomic_write_json(
        ticket_path,
        {
            "pid": os.getpid(),
            "process_identity": process_identity(os.getpid()),
            "created": time.time(),
        },
        sort_keys=True,
    )
    started = time.monotonic()
    poll_seconds = float_env("ADVISOR_QUEUE_POLL_SECONDS", DEFAULT_POLL_SECONDS, 0.05)
    announced = False
    selected_lock: InterProcessLock | None = None
    selected_index = -1
    try:
        while selected_lock is None:
            cleanup_stale_tickets(queue_dir)
            tickets = sorted(queue_dir.glob("*.json"), key=lambda item: item.name)
            try:
                position = tickets.index(ticket_path)
            except ValueError:
                raise RuntimeError("Advisor queue ticket disappeared before a worker was assigned.")
            if position < len(active_urls):
                rotation = int(key_digest(ticket_id, 8), 16) % len(active_urls)
                indexes = list(range(len(active_urls)))
                indexes = indexes[rotation:] + indexes[:rotation]
                for index in indexes:
                    candidate = InterProcessLock(
                        runtime_root()
                        / "workers"
                        / f"{current_pool_id}-{key_digest(active_urls[index], 12)}.lock",
                        timeout=0.0,
                    )
                    if candidate.try_acquire():
                        selected_lock = candidate
                        selected_index = index
                        break
            elapsed = time.monotonic() - started
            if selected_lock is not None:
                break
            if elapsed >= timeout:
                raise RuntimeError(
                    f"Advisor queue timed out after {timeout:.0f}s before a local g4f worker became available."
                )
            if not announced and elapsed >= 1.0:
                print(
                    f"Advisor queued: waiting for a local g4f worker (queue position {position + 1}).",
                    file=sys.stderr,
                )
                announced = True
            time.sleep(poll_seconds)
        ticket_path.unlink(missing_ok=True)
        waited = time.monotonic() - started
        if announced:
            print(
                f"Advisor queue: worker acquired after {waited:.1f}s; "
                f"parallel capacity={len(active_urls)}.",
                file=sys.stderr,
            )
        lease = WorkerLease(
            url=active_urls[selected_index],
            index=selected_index,
            all_urls=all_urls,
            active_urls=active_urls,
            waited_seconds=waited,
            degraded=is_degraded,
            lock=selected_lock,
        )
        if bool_env("ADVISOR_DEBUG_ROUTE", False):
            print(
                f"Advisor worker lease: worker={selected_index + 1}/{len(active_urls)} "
                f"waited={waited:.1f}s degraded={'yes' if is_degraded else 'no'}.",
                file=sys.stderr,
            )
        try:
            yield lease
        finally:
            selected_lock.release()
    finally:
        ticket_path.unlink(missing_ok=True)


@contextlib.contextmanager
def coordinated_call(
    configured_base_url: str,
    state_path: Path | None,
    *,
    request_timeout: float,
) -> Iterator[WorkerLease]:
    if not bool_env("ADVISOR_COORDINATION", True):
        lock = InterProcessLock(runtime_root() / "disabled-placeholder.lock", timeout=0.0)
        yield WorkerLease(
            url=normalized_base_url(configured_base_url),
            index=0,
            all_urls=[normalized_base_url(configured_base_url)],
            active_urls=[normalized_base_url(configured_base_url)],
            waited_seconds=0.0,
            degraded=False,
            lock=lock,
        )
        return
    queue_timeout = float_env(
        "ADVISOR_QUEUE_TIMEOUT",
        max(DEFAULT_QUEUE_TIMEOUT_SECONDS, request_timeout),
        1.0,
    )
    with conversation_lock(state_path, queue_timeout):
        with worker_lease(configured_base_url, queue_timeout) as lease:
            yield lease
