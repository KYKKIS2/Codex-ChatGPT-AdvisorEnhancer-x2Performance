#!/usr/bin/env python3
"""Cross-process coordination for local g4f-backed advisor calls."""

from __future__ import annotations

import contextlib
import contextvars
import errno
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.parse
import urllib.request
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
DEFAULT_REMOTE_CONCURRENCY = 2
DEFAULT_REMOTE_START_INTERVAL_SECONDS = 2.0
DEFAULT_REMOTE_RATE_LIMIT_COOLDOWN_SECONDS = 300.0
LEGACY_AGENT_TIMEOUT_SECONDS = 900


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


def effective_agent_timeout(value: int) -> int:
    """Neutralize the old 900-second agent cutoff unless explicitly preserved."""
    if (
        value == LEGACY_AGENT_TIMEOUT_SECONDS
        and not bool_env("ADVISOR_ALLOW_LEGACY_AGENT_TIMEOUT", False)
    ):
        return 0
    return value


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


def loopback_url_candidate(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def local_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if not loopback_url_candidate(value):
        return False
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    resolved = {
        item[4][0].split("%", 1)[0]
        for item in addresses
        if item[4]
    }
    if not resolved:
        return False
    try:
        return all(ipaddress.ip_address(address).is_loopback for address in resolved)
    except ValueError:
        return False


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_LOOPBACK_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _RejectRedirects(),
)


def open_loopback_url(
    request: urllib.request.Request,
    *,
    timeout: float | None,
) -> Any:
    if not local_http_url(request.full_url):
        raise RuntimeError("Refusing non-loopback URL in the local advisor transport")
    response = _LOOPBACK_OPENER.open(request, timeout=timeout)
    if not local_http_url(response.geturl()):
        response.close()
        raise RuntimeError("Local advisor transport escaped the loopback boundary")
    return response


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
    elif os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                try:
                    if kernel32.GetProcessTimes(
                        handle,
                        ctypes.byref(creation),
                        ctypes.byref(exit_time),
                        ctypes.byref(kernel),
                        ctypes.byref(user),
                    ):
                        return f"windows:{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
                finally:
                    kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
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
        if not current or current != expected_identity:
            return False
    return True


class InterProcessLock:
    """One-byte OS lock that is released automatically when its process exits."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float | None = DEFAULT_QUEUE_TIMEOUT_SECONDS,
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
            if self.timeout is not None and elapsed >= self.timeout:
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


def state_has_conversation_id(state_path: Path | None) -> bool:
    return any(
        key.startswith("conversation:")
        for key in conversation_lock_keys(state_path)
    )


@dataclass
class ConversationLockLease:
    timeout: float | None
    locks: list[InterProcessLock]
    keys: set[str]

    def acquire_key(self, key: str) -> None:
        if key in self.keys:
            return
        lock = InterProcessLock(
            runtime_root() / "conversations" / f"{key_digest(key)}.lock",
            timeout=self.timeout,
            wait_message="Advisor queued behind an earlier turn in the same ChatGPT conversation.",
        )
        lock.acquire()
        self.locks.append(lock)
        self.keys.add(key)

    def upgrade_conversation_id(self, conversation_id: str) -> None:
        normalized = conversation_id.strip()
        if normalized:
            self.acquire_key("conversation:" + normalized)

    def release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()
        self.locks.clear()
        self.keys.clear()


_ACTIVE_CONVERSATION_LEASE: contextvars.ContextVar[ConversationLockLease | None] = (
    contextvars.ContextVar("advisor_active_conversation_lease", default=None)
)


def upgrade_active_conversation_lock(conversation_id: str) -> None:
    lease = _ACTIVE_CONVERSATION_LEASE.get()
    if lease is not None:
        lease.upgrade_conversation_id(conversation_id)


@contextlib.contextmanager
def conversation_lock(
    state_path: Path | None,
    timeout: float | None,
) -> Iterator[ConversationLockLease | None]:
    if state_path is None:
        yield None
        return
    lease = ConversationLockLease(timeout=timeout, locks=[], keys=set())
    try:
        state_key = "state:" + str(state_path.resolve())
        # Acquire the state lock first, then re-read the state while holding it.
        # A waiter may have queued before the first turn persisted its new
        # conversation id; using keys calculated before that wait would miss
        # the cross-state conversation lock.
        lease.acquire_key(state_key)
        for key in conversation_lock_keys(state_path):
            if key != state_key:
                lease.acquire_key(key)
        yield lease
    finally:
        lease.release()


def remote_rate_state_path() -> Path:
    return runtime_root() / "remote-rate-state.json"


def remote_rate_lock() -> InterProcessLock:
    return InterProcessLock(runtime_root() / "remote-rate-state.lock", timeout=10.0)


def record_remote_rate_limit(retry_after: float | None = None, now: float | None = None) -> None:
    """Temporarily serialize new remote turns after ChatGPT throttles any caller."""
    current = time.time() if now is None else now
    cooldown = float_env(
        "ADVISOR_REMOTE_RATE_LIMIT_COOLDOWN_SECONDS",
        DEFAULT_REMOTE_RATE_LIMIT_COOLDOWN_SECONDS,
        1.0,
    )
    if retry_after is not None:
        cooldown = max(cooldown, retry_after)
    with remote_rate_lock():
        state = load_json(remote_rate_state_path())
        previous_until = float(state.get("degraded_until") or 0.0)
        safety.atomic_write_json(
            remote_rate_state_path(),
            {
                "degraded_until": max(previous_until, current + cooldown),
                "last_rate_limit_at": current,
            },
            sort_keys=True,
        )


def configured_remote_capacity() -> int:
    manifest = load_json(pool_manifest_path())
    try:
        manager_pid = int(manifest.get("manager_pid") or 0)
        manifest_capacity = int(manifest.get("remote_chatgpt_capacity") or 0)
    except (TypeError, ValueError):
        manager_pid = 0
        manifest_capacity = 0
    manager_identity = str(manifest.get("manager_identity") or "")
    if manifest_capacity > 0 and process_alive(manager_pid, manager_identity):
        return manifest_capacity
    return int_env("ADVISOR_REMOTE_MAX_CONCURRENCY", DEFAULT_REMOTE_CONCURRENCY)


def remote_concurrency_limit(now: float | None = None) -> tuple[int, bool]:
    configured = configured_remote_capacity()
    current = time.time() if now is None else now
    with remote_rate_lock():
        state = load_json(remote_rate_state_path())
        degraded_until = float(state.get("degraded_until") or 0.0)
        degraded = degraded_until > current
        if degraded_until and not degraded:
            remote_rate_state_path().unlink(missing_ok=True)
    return (1 if degraded else configured), degraded


def known_remote_slot_indexes(configured_capacity: int) -> list[int]:
    indexes = set(range(configured_capacity))
    slot_root = runtime_root() / "remote-slots"
    if slot_root.exists():
        for path in slot_root.glob("slot-*.lock"):
            match = re.fullmatch(r"slot-(\d+)\.lock", path.name)
            if match:
                indexes.add(int(match.group(1)))
    return sorted(indexes)


def pace_remote_start(started: float, timeout: float | None) -> None:
    interval = float_env(
        "ADVISOR_REMOTE_START_INTERVAL_SECONDS",
        DEFAULT_REMOTE_START_INTERVAL_SECONDS,
        0.0,
    )
    if interval <= 0:
        return
    with InterProcessLock(
        runtime_root() / "remote-start.lock",
        timeout=None if timeout is None else max(0.0, timeout - (time.monotonic() - started)),
        wait_message="Advisor is staggering remote ChatGPT turn starts to avoid burst throttling.",
    ):
        state_path = runtime_root() / "remote-start.json"
        state = load_json(state_path)
        wait_seconds = max(0.0, float(state.get("last_start_at") or 0.0) + interval - time.time())
        if timeout_expired(started, timeout) or (
            timeout is not None and time.monotonic() - started + wait_seconds >= timeout
        ):
            raise RuntimeError(
                f"Advisor queue timed out after {timeout:.0f}s while pacing remote ChatGPT starts."
            )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        safety.atomic_write_json(state_path, {"last_start_at": time.time()}, sort_keys=True)


@dataclass
class RemoteCallLease:
    ticket_path: Path
    queue_started: float
    timeout: float | None
    announced: bool
    selected_slot: int
    marked_started: bool = False

    def mark_start(self) -> None:
        if self.marked_started:
            return
        pace_remote_start(self.queue_started, self.timeout)
        self.ticket_path.unlink(missing_ok=True)
        self.marked_started = True
        if self.announced:
            print(
                "Advisor queue: remote ChatGPT turn admitted after "
                f"{time.monotonic() - self.queue_started:.1f}s in FIFO order.",
                file=sys.stderr,
            )
        if bool_env("ADVISOR_DEBUG_ROUTE", False):
            capacity, degraded = remote_concurrency_limit()
            print(
                f"Advisor remote lease: slot={self.selected_slot + 1}/{capacity} "
                f"degraded={'yes' if degraded else 'no'}.",
                file=sys.stderr,
            )


_ACTIVE_REMOTE_LEASE: contextvars.ContextVar[RemoteCallLease | None] = contextvars.ContextVar(
    "advisor_active_remote_lease",
    default=None,
)


def mark_active_remote_start() -> None:
    lease = _ACTIVE_REMOTE_LEASE.get()
    if lease is not None:
        lease.mark_start()


@contextlib.contextmanager
def remote_call_slot(
    timeout: float | None,
    *,
    defer_start: bool = False,
    exclusive: bool = False,
) -> Iterator[RemoteCallLease]:
    """FIFO admission control for remote ChatGPT turns, independent of local workers."""
    queue_dir = runtime_root() / "queues" / "remote-chatgpt"
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
    selected_locks: list[InterProcessLock] = []
    selected_slot = -1
    degraded_announced = False
    try:
        while not selected_locks:
            cleanup_stale_tickets(queue_dir)
            tickets = sorted(queue_dir.glob("*.json"), key=lambda item: item.name)
            try:
                position = tickets.index(ticket_path)
            except ValueError as exc:
                raise RuntimeError("Advisor remote queue ticket disappeared before admission.") from exc
            capacity, degraded = remote_concurrency_limit()
            if degraded and not degraded_announced:
                print(
                    "Advisor remote safety queue is temporarily serialized after ChatGPT rate limiting.",
                    file=sys.stderr,
                )
                degraded_announced = True
            # Only the oldest live ticket may claim the next free slot. Once it
            # finishes start pacing and removes its ticket, the next waiter may
            # claim another slot, preserving FIFO with capacity greater than one.
            if position == 0:
                configured_capacity = configured_remote_capacity()
                known_slots = known_remote_slot_indexes(configured_capacity)
                if degraded or exclusive:
                    acquired: list[InterProcessLock] = []
                    for slot in known_slots:
                        candidate = InterProcessLock(
                            runtime_root() / "remote-slots" / f"slot-{slot}.lock",
                            timeout=0.0,
                        )
                        if not candidate.try_acquire():
                            for held in reversed(acquired):
                                held.release()
                            acquired = []
                            break
                        acquired.append(candidate)
                    if acquired:
                        selected_locks = acquired
                        selected_slot = 0
                else:
                    # A restarted supervisor may advertise fewer slots while an
                    # older wrapper still owns a high-numbered lease. Wait for
                    # every retired slot to drain before admitting under the
                    # smaller authoritative capacity.
                    retired_clear = True
                    retired_probes: list[InterProcessLock] = []
                    for slot in (item for item in known_slots if item >= capacity):
                        probe = InterProcessLock(
                            runtime_root() / "remote-slots" / f"slot-{slot}.lock",
                            timeout=0.0,
                        )
                        if not probe.try_acquire():
                            retired_clear = False
                            break
                        retired_probes.append(probe)
                    for probe in reversed(retired_probes):
                        probe.release()
                    if retired_clear:
                        for slot in range(capacity):
                            candidate = InterProcessLock(
                                runtime_root() / "remote-slots" / f"slot-{slot}.lock",
                                timeout=0.0,
                            )
                            if not candidate.try_acquire():
                                continue
                            selected_locks = [candidate]
                            selected_slot = slot
                            break
            if selected_locks:
                break
            if timeout_expired(started, timeout):
                raise RuntimeError(
                    f"Advisor queue timed out after {timeout:.0f}s before remote ChatGPT admission."
                )
            if not announced and time.monotonic() - started >= 1.0:
                print(
                    "Advisor queued: waiting for remote ChatGPT safety admission "
                    f"(queue position {position + 1}, capacity {capacity}).",
                    file=sys.stderr,
                )
                announced = True
            time.sleep(poll_seconds)

        lease = RemoteCallLease(
            ticket_path=ticket_path,
            queue_started=started,
            timeout=timeout,
            announced=announced,
            selected_slot=selected_slot,
        )
        if not defer_start:
            lease.mark_start()
        yield lease
    finally:
        ticket_path.unlink(missing_ok=True)
        for selected_lock in reversed(selected_locks):
            selected_lock.release()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def active_pool_manifest(configured_base_url: str) -> dict[str, Any]:
    if not bool_env("ADVISOR_POOL_ENABLED", True):
        return {}
    if os.environ.get("ADVISOR_POOL_WORKER_URLS"):
        return {}
    manifest = load_json(pool_manifest_path())
    manager_pid = int(manifest.get("manager_pid") or 0)
    manager_identity = str(manifest.get("manager_identity") or "")
    if not process_alive(manager_pid, manager_identity):
        return {}
    workers = manifest.get("workers")
    if not isinstance(workers, list):
        return {}
    configured = normalized_base_url(configured_base_url)
    live_urls = {
        normalized_base_url(str(item.get("url") or ""))
        for item in workers
        if isinstance(item, dict)
        and process_alive(
            int(item.get("pid") or 0),
            str(item.get("process_identity") or ""),
        )
    }
    return manifest if configured in live_urls else {}


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
    if getattr(exc, "submission_outcome_unknown", False):
        return True
    text = str(exc).lower()
    markers = (
        "http 500",
        "http 429",
        "response 429",
        "too many requests",
        "rate limit",
        "error in message stream",
        "connection refused",
        "connection reset",
        "remote end closed",
        "incompleteread",
        "websocket",
        "timed out",
    )
    return any(marker in text for marker in markers)


def rate_limit_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("http 429", "response 429", "too many requests", "rate limit")
    )


def ticket_owner_alive(path: Path) -> bool:
    payload = load_json(path)
    return process_alive(int(payload.get("pid") or 0), str(payload.get("process_identity") or ""))


def cleanup_stale_tickets(queue_dir: Path) -> None:
    if not queue_dir.exists():
        return
    for path in queue_dir.glob("*.json"):
        if not ticket_owner_alive(path):
            path.unlink(missing_ok=True)


def transient_request_dir(run_id: str) -> Path:
    path = runtime_root() / "transient-requests" / key_digest(run_id, 32)
    safety.ensure_private_dir(path)
    return path


def transient_release_path(request_path: Path) -> Path:
    return request_path.with_suffix(".release")


def transient_request_lock(request_path: Path, timeout: float | None = 10.0) -> InterProcessLock:
    return InterProcessLock(
        runtime_root() / "transient-request-locks" / f"{key_digest(str(request_path), 32)}.lock",
        timeout=timeout,
    )


def validated_transient_log_path(raw_path: str | Path | None) -> Path | None:
    if not isinstance(raw_path, (str, Path)) or not str(raw_path):
        return None
    try:
        candidate = Path(raw_path).expanduser().resolve()
        expected_root = (runtime_root() / "transient-logs").resolve()
    except OSError:
        return None
    return candidate if candidate.is_relative_to(expected_root) else None


def remove_transient_log(raw_path: str | Path | None) -> None:
    log_path = validated_transient_log_path(raw_path)
    if log_path is not None:
        log_path.unlink(missing_ok=True)


def cleanup_warning(message: str, exc: BaseException) -> None:
    print(
        f"Advisor transient cleanup warning: {message} ({type(exc).__name__}).",
        file=sys.stderr,
    )


def timeout_expired(started: float, timeout: float | None) -> bool:
    return timeout is not None and time.monotonic() - started >= timeout


def terminate_external_process(pid: int, identity: str, timeout: float = 10.0) -> None:
    if not process_alive(pid, identity):
        return
    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + timeout
    while process_alive(pid, identity) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not process_alive(pid, identity):
        return
    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@dataclass
class WorkerLease:
    url: str
    index: int
    all_urls: list[str]
    active_urls: list[str]
    waited_seconds: float
    degraded: bool
    lock: InterProcessLock
    transient: bool = False

    def report_failure(self) -> None:
        record_transport_failure(self.url, self.all_urls)

    def report_success(self) -> None:
        record_transport_success(self.url, self.all_urls)


@contextlib.contextmanager
def transient_worker_lease(
    manifest: dict[str, Any],
    timeout: float | None,
) -> Iterator[WorkerLease]:
    run_id = str(manifest.get("run_id") or "")
    transient = manifest.get("transient")
    if not run_id or not isinstance(transient, dict):
        raise RuntimeError("The g4f supervisor manifest does not contain transient-worker settings.")
    max_workers = int(transient.get("max_workers") or 0)
    if max_workers < 1:
        raise RuntimeError("The g4f supervisor transient-worker ceiling is invalid.")

    current_pool_id = key_digest(f"transient:{run_id}", 16)
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
    slot_lock: InterProcessLock | None = None
    slot = -1
    request_path: Path | None = None
    release_path: Path | None = None
    try:
        while slot_lock is None:
            cleanup_stale_tickets(queue_dir)
            tickets = sorted(queue_dir.glob("*.json"), key=lambda item: item.name)
            try:
                position = tickets.index(ticket_path)
            except ValueError as exc:
                raise RuntimeError("Advisor queue ticket disappeared before a worker was assigned.") from exc
            if position < max_workers:
                rotation = int(key_digest(ticket_id, 8), 16) % max_workers
                for candidate_slot in list(range(max_workers))[rotation:] + list(range(max_workers))[:rotation]:
                    candidate = InterProcessLock(
                        runtime_root() / "transient-slots" / f"{current_pool_id}-{candidate_slot}.lock",
                        timeout=0.0,
                    )
                    if candidate.try_acquire():
                        slot_lock = candidate
                        slot = candidate_slot
                        break
            if slot_lock is not None:
                break
            if timeout_expired(started, timeout):
                raise RuntimeError(
                    f"Advisor queue timed out after {timeout:.0f}s before a transient g4f slot became available."
                )
            if not announced and time.monotonic() - started >= 1.0:
                print(
                    f"Advisor queued: waiting for a transient g4f slot (queue position {position + 1}).",
                    file=sys.stderr,
                )
                announced = True
            time.sleep(poll_seconds)

        ticket_path.unlink(missing_ok=True)
        request_id = uuid.uuid4().hex
        request_path = transient_request_dir(run_id) / f"{request_id}.json"
        release_path = transient_release_path(request_path)
        safety.atomic_write_json(
            request_path,
            {
                "schema_version": 1,
                "request_id": request_id,
                "run_id": run_id,
                "owner_pid": os.getpid(),
                "owner_identity": process_identity(os.getpid()),
                "slot": slot,
                "status": "pending",
                "created_at": time.time(),
            },
            sort_keys=True,
        )

        worker_payload: dict[str, Any] = {}
        while True:
            current_manifest = load_json(pool_manifest_path())
            manager_pid = int(current_manifest.get("manager_pid") or 0)
            manager_identity = str(current_manifest.get("manager_identity") or "")
            if current_manifest.get("run_id") != run_id or not process_alive(manager_pid, manager_identity):
                raise RuntimeError("The g4f supervisor stopped before the transient worker became ready.")
            worker_payload = load_json(request_path)
            status = str(worker_payload.get("status") or "")
            if status == "ready":
                worker_url = normalized_base_url(str(worker_payload.get("url") or ""))
                worker_pid = int(worker_payload.get("worker_pid") or 0)
                worker_identity = str(worker_payload.get("worker_identity") or "")
                if not local_http_url(worker_url) or not process_alive(worker_pid, worker_identity):
                    raise RuntimeError("The transient g4f worker became unavailable during startup.")
                break
            if status == "failed":
                detail = str(worker_payload.get("error") or "transient worker startup failed")
                raise RuntimeError(f"Could not start a transient g4f worker: {detail}")
            if timeout_expired(started, timeout):
                raise RuntimeError(
                    f"Advisor queue timed out after {timeout:.0f}s while starting a transient g4f worker."
                )
            time.sleep(poll_seconds)

        waited = time.monotonic() - started
        if announced:
            print(
                f"Advisor queue: transient worker acquired after {waited:.1f}s; "
                f"emergency ceiling={max_workers}.",
                file=sys.stderr,
            )
        lease = WorkerLease(
            url=worker_url,
            index=slot,
            all_urls=[worker_url],
            active_urls=[worker_url],
            waited_seconds=waited,
            degraded=False,
            lock=slot_lock,
            transient=True,
        )
        if bool_env("ADVISOR_DEBUG_ROUTE", False):
            print(
                f"Advisor worker lease: transient slot={slot + 1}/{max_workers} "
                f"waited={waited:.1f}s.",
                file=sys.stderr,
            )
        yield lease
    finally:
        try:
            try:
                ticket_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_warning("could not remove queue ticket", exc)
            if request_path is not None and release_path is not None:
                try:
                    safety.atomic_write_text(release_path, f"{time.time():.6f}\n")
                except OSError as exc:
                    cleanup_warning("could not signal worker release", exc)
                cleanup_timeout = float_env("ADVISOR_TRANSIENT_RELEASE_TIMEOUT", 20.0, 1.0)
                cleanup_deadline = time.monotonic() + cleanup_timeout
                while request_path.exists() and time.monotonic() < cleanup_deadline:
                    current_manifest = load_json(pool_manifest_path())
                    if current_manifest.get("run_id") != run_id:
                        break
                    time.sleep(0.1)
                if request_path.exists():
                    try:
                        with transient_request_lock(request_path):
                            payload = load_json(request_path)
                            try:
                                terminate_external_process(
                                    int(payload.get("worker_pid") or 0),
                                    str(payload.get("worker_identity") or ""),
                                )
                            finally:
                                try:
                                    remove_transient_log(payload.get("log_path"))
                                except OSError as exc:
                                    cleanup_warning("could not remove transient worker log", exc)
                                request_path.unlink(missing_ok=True)
                    except (OSError, RuntimeError) as exc:
                        cleanup_warning("caller fallback could not remove worker artifacts", exc)
                try:
                    release_path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_warning("could not remove worker release marker", exc)
        finally:
            if slot_lock is not None:
                slot_lock.release()


@contextlib.contextmanager
def worker_lease(configured_base_url: str, timeout: float | None) -> Iterator[WorkerLease]:
    normalized_url = normalized_base_url(configured_base_url)
    manifest = active_pool_manifest(normalized_url)
    if manifest.get("mode") == "transient" and bool_env("ADVISOR_TRANSIENT_WORKERS", True):
        with transient_worker_lease(manifest, timeout) as lease:
            yield lease
        return
    all_urls = manifest_worker_urls(normalized_url)
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
            if timeout_expired(started, timeout):
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
    default_queue_timeout = max(
        DEFAULT_QUEUE_TIMEOUT_SECONDS,
        request_timeout if request_timeout > 0 else DEFAULT_QUEUE_TIMEOUT_SECONDS,
    )
    configured_queue_timeout = float_env(
        "ADVISOR_QUEUE_TIMEOUT",
        default_queue_timeout,
        0.0,
    )
    queue_timeout = None if configured_queue_timeout <= 0 else configured_queue_timeout
    first_turn_exclusive = state_path is not None and not state_has_conversation_id(state_path)
    # Remote admission is the outer lock. Unknown first turns take every slot,
    # so no known-conversation caller can hold a conversation lock while waiting
    # on the first turn's remote slot and deadlock its ID upgrade.
    with remote_call_slot(
        queue_timeout,
        defer_start=True,
        exclusive=first_turn_exclusive,
    ) as remote_lease:
        remote_token = _ACTIVE_REMOTE_LEASE.set(remote_lease)
        try:
            with conversation_lock(state_path, queue_timeout) as conversation_lease:
                conversation_token = _ACTIVE_CONVERSATION_LEASE.set(conversation_lease)
                try:
                    with worker_lease(configured_base_url, queue_timeout) as lease:
                        yield lease
                finally:
                    _ACTIVE_CONVERSATION_LEASE.reset(conversation_token)
        finally:
            _ACTIVE_REMOTE_LEASE.reset(remote_token)
