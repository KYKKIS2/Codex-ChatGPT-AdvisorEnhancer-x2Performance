#!/usr/bin/env python3
"""Durable state, schemas, and repository evidence for goal-research runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

import advisor_concurrency as concurrency
import advisor_safety as safety


SCHEMA_VERSION = "1.0"
RUNS_DIR = Path(".codex-advisor") / "goal-research-runs"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")

PHASE_NEW = "NEW"
PHASE_GOAL_FROZEN = "GOAL_FROZEN"
PHASE_GOAL_FIDELITY = "GOAL_FIDELITY_CHECK"
PHASE_CLEAN_ROOM = "CLEAN_ROOM_GROUNDING"
PHASE_HYPOTHESES = "HYPOTHESIS_SET_READY"
PHASE_CHALLENGE = "CHALLENGE"
PHASE_PACKET_READY = "PACKET_READY"
PHASE_WAITING_CODEX = "WAITING_FOR_CODEX"
PHASE_WAITING_VERIFICATION = "WAITING_FOR_LOCAL_VERIFICATION"
PHASE_POST_AUDIT = "POST_CHANGE_AUDIT"
PHASE_ITERATION_CLOSED = "ITERATION_CLOSED"
PHASE_EPISTEMIC_REFRESH = "EPISTEMIC_REFRESH"
PHASE_FINAL_AUDIT = "FINAL_CLEAN_ROOM_AUDIT"
PHASE_COMPLETED = "GOAL_COMPLETED"
PHASE_BLOCKED = "BLOCKED"

TERMINAL_PHASES = {PHASE_COMPLETED}
ALL_PHASES = {
    PHASE_NEW,
    PHASE_GOAL_FROZEN,
    PHASE_GOAL_FIDELITY,
    PHASE_CLEAN_ROOM,
    PHASE_HYPOTHESES,
    PHASE_CHALLENGE,
    PHASE_PACKET_READY,
    PHASE_WAITING_CODEX,
    PHASE_WAITING_VERIFICATION,
    PHASE_POST_AUDIT,
    PHASE_ITERATION_CLOSED,
    PHASE_EPISTEMIC_REFRESH,
    PHASE_FINAL_AUDIT,
    PHASE_COMPLETED,
    PHASE_BLOCKED,
}
ITERATION_OUTCOMES = {"accepted", "rejected", "inconclusive", "escalated"}
EVIDENCE_CLASSES = {
    "repository_observation",
    "advisor_inference",
    "proposed_experiment",
    "codex_local_result",
    "independent_audit_result",
}
INFORMATION_LAYERS = (
    "source_availability",
    "pipeline_preservation",
    "representation_distinguishability",
    "learnability",
    "utilization",
    "evaluation_validity",
    "causal_operational_validity",
)
INFORMATION_STATUSES = {
    "supported",
    "unsupported",
    "unknown",
    "not_applicable",
}
FIELD_CLASSIFICATIONS = {
    "retained",
    "transformed",
    "aggregated",
    "excluded_with_justification",
    "unavailable",
    "unexplained",
}
SPECIALIST_CATALOG = {
    "architecture-integration",
    "data-ml-causality",
    "domain-workflow",
    "performance-reliability",
    "security-privacy",
}
SEVERITIES = {"critical", "high", "medium", "low", "info"}
CLAIM_STATUSES = {"supported", "contradicted", "uncertain"}
HYPOTHESIS_KINDS = {"leading", "alternative", "null_measurement"}
ACCEPTANCE_STATUSES = {"passed", "failed", "unknown", "waived"}
CLAUSE_STATUSES = {"supported", "regressed", "unknown"}
PIPELINE_STAGE_KINDS = {
    "raw_source",
    "selection_filtering",
    "feature_construction",
    "representation_boundary",
    "masking_truncation_order_defaults",
    "processing_model",
    "aggregation_compression",
    "outputs_heads",
    "loss_decision",
    "evaluation",
}
FAILURE_LAYERS = set(INFORMATION_LAYERS)

EVENT_TRANSITIONS: dict[str, set[tuple[str, str]]] = {
    "run_initialized": {(PHASE_NEW, PHASE_GOAL_FROZEN)},
    "goal_fidelity_recorded": {(PHASE_GOAL_FROZEN, PHASE_GOAL_FIDELITY)},
    "grounding_recorded": {(PHASE_GOAL_FIDELITY, PHASE_CLEAN_ROOM)},
    "hypotheses_recorded": {(PHASE_CLEAN_ROOM, PHASE_HYPOTHESES)},
    "challenge_recorded": {(PHASE_HYPOTHESES, PHASE_CHALLENGE)},
    "packet_issued": {(PHASE_CHALLENGE, PHASE_PACKET_READY)},
    "codex_wait_started": {(PHASE_PACKET_READY, PHASE_WAITING_CODEX)},
    "codex_implementation_recorded": {
        (PHASE_WAITING_CODEX, PHASE_WAITING_VERIFICATION)
    },
    "implementation_receipt_invalidated": {
        (PHASE_WAITING_VERIFICATION, PHASE_WAITING_CODEX)
    },
    "local_verification_recorded": {
        (PHASE_WAITING_VERIFICATION, PHASE_POST_AUDIT)
    },
    "post_change_audit_recorded": {
        (PHASE_POST_AUDIT, PHASE_ITERATION_CLOSED)
    },
    "epistemic_refresh_recorded": {
        (PHASE_ITERATION_CLOSED, PHASE_EPISTEMIC_REFRESH)
    },
    "next_iteration_started": {
        (PHASE_EPISTEMIC_REFRESH, PHASE_GOAL_FROZEN)
    },
    "final_audit_started": {
        (PHASE_EPISTEMIC_REFRESH, PHASE_FINAL_AUDIT)
    },
    "goal_completed": {(PHASE_FINAL_AUDIT, PHASE_COMPLETED)},
}


class GoalResearchError(RuntimeError):
    """Raised when durable goal-research state fails closed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GoalResearchError(f"value is not canonical JSON: {exc}") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not ID_PATTERN.fullmatch(text):
        raise GoalResearchError(
            f"{label} must be 1-120 ASCII letters, digits, dots, underscores, or hyphens."
        )
    return text


def require_string(value: Any, label: str, *, maximum: int = 20_000) -> str:
    text = str(value or "").strip()
    if not text:
        raise GoalResearchError(f"{label} must be a non-empty string.")
    if len(text) > maximum:
        raise GoalResearchError(f"{label} exceeds the {maximum}-character limit.")
    return text


def string_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise GoalResearchError(f"{label} must be a list of strings.")
    result = [require_string(item, f"{label} item", maximum=4_000) for item in value]
    if not allow_empty and not result:
        raise GoalResearchError(f"{label} must not be empty.")
    return result


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise GoalResearchError(f"{label} must be a boolean.")
    return value


def reject_unknown_keys(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise GoalResearchError(f"{label} contains unknown keys: {', '.join(unknown)}")


def optional_string(value: Any, label: str, *, maximum: int = 20_000) -> str:
    if value is None or value == "":
        return ""
    return require_string(value, label, maximum=maximum)


def require_number(value: Any, label: str, *, minimum: float, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GoalResearchError(f"{label} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise GoalResearchError(f"{label} must be finite.")
    if result < minimum or result > maximum:
        raise GoalResearchError(f"{label} must be between {minimum} and {maximum}.")
    return result


def validate_relative_path(value: Any, label: str) -> str:
    raw = require_string(value, label, maximum=4_000).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise GoalResearchError(f"{label} must be repository-relative.")
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise GoalResearchError(f"{label} escapes or names the repository root.")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise GoalResearchError(f"{label} cannot reference Git internals.")
    if normalized == ".codex-advisor" or normalized.startswith(".codex-advisor/"):
        raise GoalResearchError(f"{label} cannot reference private advisor state.")
    return normalized


def validate_scope_selector(value: Any) -> str:
    selector = require_string(value, "allowed_scope item", maximum=1_000).replace("\\", "/")
    if selector.startswith("/") or re.match(r"^[A-Za-z]:/", selector):
        raise GoalResearchError("allowed_scope selectors must be repository-relative.")
    normalized = posixpath.normpath(selector)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise GoalResearchError("allowed_scope selectors cannot escape or select the whole repository.")
    literal_prefix = re.split(r"[*?[{]", normalized, maxsplit=1)[0].rstrip("/")
    if not literal_prefix:
        raise GoalResearchError("allowed_scope selectors need a literal repository prefix.")
    if literal_prefix in {".git", ".codex-advisor"} or literal_prefix.startswith(
        (".git/", ".codex-advisor/")
    ):
        raise GoalResearchError("allowed_scope cannot include Git or private advisor state.")
    protected_candidates = (
        ".git",
        ".git/config",
        ".codex-advisor",
        ".codex-advisor/state.json",
    )
    if any(PurePosixPath(candidate).match(normalized) for candidate in protected_candidates):
        raise GoalResearchError("allowed_scope cannot match Git or private advisor state.")
    return normalized


def selector_within_scope(selector: str, allowed_scope: Iterable[str]) -> bool:
    """Conservatively prove that one packet selector cannot exceed goal scope."""
    normalized = validate_scope_selector(selector)
    allowed = [validate_scope_selector(item) for item in allowed_scope]
    if normalized in allowed:
        return True
    if re.search(r"[*?[{]", normalized):
        return False
    return path_in_scope(normalized, allowed)


def read_json_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise GoalResearchError(f"Required JSON file does not exist: {path}") from None
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalResearchError(f"Could not read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GoalResearchError(f"JSON file must contain an object: {path}")
    return value


def immutable_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        existing = read_json_object(path)
        if canonical_json(existing) != canonical_json(value):
            raise GoalResearchError(f"Refusing to overwrite immutable artifact: {path}")
        return
    safety.atomic_write_json(path, value, sort_keys=True)


def private_mode_ok(path: Path) -> bool:
    if os.name != "posix" or not path.exists():
        return True
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode & 0o077 == 0


def _unique_ids(items: Iterable[dict[str, Any]], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        identifier = validate_identifier(item.get("id"), f"{label} id")
        if identifier in seen:
            raise GoalResearchError(f"Duplicate {label} id: {identifier}")
        seen.add(identifier)


def validate_goal_contract(raw: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(
        raw,
        {
            "schema_version",
            "goal_id",
            "version",
            "objective",
            "clauses",
            "non_goals",
            "constraints",
            "allowed_scope",
            "acceptance_dimensions",
            "budgets",
            "escalation_conditions",
            "requires_information_audit",
            "allowed_specialists",
            "waivers",
        },
        "goal contract",
    )
    if str(raw.get("schema_version") or "") != SCHEMA_VERSION:
        raise GoalResearchError(
            f"goal schema_version must be {SCHEMA_VERSION!r}."
        )
    goal_id = validate_identifier(raw.get("goal_id"), "goal_id")
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise GoalResearchError("goal version must be a positive integer.")
    objective = require_string(raw.get("objective"), "objective")

    raw_clauses = raw.get("clauses")
    if not isinstance(raw_clauses, list) or not raw_clauses:
        raise GoalResearchError("goal clauses must be a non-empty list.")
    clauses: list[dict[str, Any]] = []
    for item in raw_clauses:
        if not isinstance(item, dict):
            raise GoalResearchError("each goal clause must be an object.")
        reject_unknown_keys(item, {"id", "text", "critical"}, "goal clause")
        clauses.append(
            {
                "id": validate_identifier(item.get("id"), "goal clause id"),
                "text": require_string(item.get("text"), "goal clause text"),
                "critical": require_bool(item.get("critical", True), "goal clause critical"),
            }
        )
    _unique_ids(clauses, "goal clause")
    clause_ids = {item["id"] for item in clauses}

    raw_acceptance = raw.get("acceptance_dimensions")
    if not isinstance(raw_acceptance, list) or not raw_acceptance:
        raise GoalResearchError("acceptance_dimensions must be a non-empty list.")
    acceptance: list[dict[str, Any]] = []
    for item in raw_acceptance:
        if not isinstance(item, dict):
            raise GoalResearchError("each acceptance dimension must be an object.")
        reject_unknown_keys(
            item,
            {
                "id",
                "description",
                "kind",
                "required",
                "goal_clause_ids",
                "evidence_requirements",
            },
            "acceptance dimension",
        )
        kind = str(item.get("kind") or "").strip()
        if kind not in {"hard_invariant", "quantitative", "qualitative"}:
            raise GoalResearchError(
                "acceptance kind must be hard_invariant, quantitative, or qualitative."
            )
        linked = string_list(
            item.get("goal_clause_ids"),
            "acceptance goal_clause_ids",
            allow_empty=False,
        )
        unknown = sorted(set(linked) - clause_ids)
        if unknown:
            raise GoalResearchError(
                "acceptance dimension references unknown goal clauses: "
                + ", ".join(unknown)
            )
        required_value = require_bool(
            item.get("required", True), "acceptance required"
        )
        acceptance.append(
            {
                "id": validate_identifier(item.get("id"), "acceptance id"),
                "description": require_string(
                    item.get("description"), "acceptance description"
                ),
                "kind": kind,
                "required": required_value,
                "goal_clause_ids": linked,
                "evidence_requirements": string_list(
                    item.get("evidence_requirements", []),
                    "acceptance evidence_requirements",
                    allow_empty=not required_value,
                ),
            }
        )
    _unique_ids(acceptance, "acceptance")
    acceptance_by_id = {item["id"]: item for item in acceptance}
    mapped_clauses = {
        clause_id
        for dimension in acceptance
        if dimension["required"]
        for clause_id in dimension["goal_clause_ids"]
    }
    missing_clauses = sorted(clause_ids - mapped_clauses)
    if missing_clauses:
        raise GoalResearchError(
            "every goal clause must map to a required acceptance dimension: "
            + ", ".join(missing_clauses)
        )

    raw_waivers = raw.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise GoalResearchError("waivers must be a list.")
    waivers: list[dict[str, Any]] = []
    waived_dimensions: set[str] = set()
    for item in raw_waivers:
        if not isinstance(item, dict):
            raise GoalResearchError("each waiver must be an object.")
        reject_unknown_keys(
            item,
            {"id", "acceptance_dimension_id", "user_decision", "reason"},
            "waiver",
        )
        acceptance_id = validate_identifier(
            item.get("acceptance_dimension_id"), "waiver acceptance_dimension_id"
        )
        dimension = acceptance_by_id.get(acceptance_id)
        if dimension is None:
            raise GoalResearchError("waiver references an unknown acceptance dimension.")
        if dimension["kind"] == "hard_invariant":
            raise GoalResearchError("hard-invariant acceptance dimensions cannot be waived.")
        if acceptance_id in waived_dimensions:
            raise GoalResearchError("an acceptance dimension cannot have multiple active waivers.")
        waived_dimensions.add(acceptance_id)
        waivers.append(
            {
                "id": validate_identifier(item.get("id"), "waiver id"),
                "acceptance_dimension_id": acceptance_id,
                "user_decision": require_string(
                    item.get("user_decision"), "waiver user_decision"
                ),
                "reason": require_string(item.get("reason"), "waiver reason"),
            }
        )
    _unique_ids(waivers, "waiver")

    budgets = raw.get("budgets")
    if not isinstance(budgets, dict):
        raise GoalResearchError("budgets must be an object.")
    normalized_budgets: dict[str, int] = {}
    limits = {
        "max_iterations": (1, 100),
        "max_advisor_turns": (1, 10_000),
        "max_specialists_per_iteration": (0, 2),
        "max_active_hypotheses": (1, 3),
    }
    reject_unknown_keys(budgets, set(limits), "budgets")
    for key, (minimum, maximum) in limits.items():
        value = budgets.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise GoalResearchError(f"budgets.{key} must be an integer.")
        if value < minimum or value > maximum:
            raise GoalResearchError(
                f"budgets.{key} must be between {minimum} and {maximum}."
            )
        normalized_budgets[key] = value
    minimum_complete_slice = 11 + normalized_budgets["max_specialists_per_iteration"]
    if normalized_budgets["max_advisor_turns"] < minimum_complete_slice:
        raise GoalResearchError(
            "max_advisor_turns is too small for one complete bounded iteration and final audit; "
            f"minimum is {minimum_complete_slice}."
        )

    allowed_specialists = raw.get(
        "allowed_specialists", sorted(SPECIALIST_CATALOG)
    )
    specialists = string_list(allowed_specialists, "allowed_specialists")
    unknown_specialists = sorted(set(specialists) - SPECIALIST_CATALOG)
    if unknown_specialists:
        raise GoalResearchError(
            "unknown specialist profiles: " + ", ".join(unknown_specialists)
        )

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "goal_id": goal_id,
        "version": version,
        "objective": objective,
        "clauses": clauses,
        "non_goals": string_list(raw.get("non_goals", []), "non_goals"),
        "constraints": string_list(raw.get("constraints", []), "constraints"),
        "allowed_scope": [
            validate_scope_selector(item)
            for item in string_list(
                raw.get("allowed_scope"), "allowed_scope", allow_empty=False
            )
        ],
        "acceptance_dimensions": acceptance,
        "budgets": normalized_budgets,
        "escalation_conditions": string_list(
            raw.get("escalation_conditions"),
            "escalation_conditions",
            allow_empty=False,
        ),
        "requires_information_audit": require_bool(
            raw.get("requires_information_audit", False),
            "requires_information_audit",
        ),
        "allowed_specialists": sorted(set(specialists)),
        "waivers": waivers,
    }
    return normalized


def waiver_map(goal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["acceptance_dimension_id"]: item
        for item in goal.get("waivers", [])
        if isinstance(item, dict)
    }


def acceptance_status_satisfies_gate(
    goal: dict[str, Any], acceptance_id: str, status_value: str
) -> bool:
    return status_value == "passed" or (
        status_value == "waived" and acceptance_id in waiver_map(goal)
    )


def validate_acceptance_status_authority(
    goal: dict[str, Any], acceptance_id: str, status_value: str
) -> None:
    if status_value == "waived" and acceptance_id not in waiver_map(goal):
        raise GoalResearchError(
            "acceptance status 'waived' requires a frozen explicit user waiver."
        )


def validate_goal_amendment(previous: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    current = validate_goal_contract(raw)
    if current["goal_id"] != previous["goal_id"]:
        raise GoalResearchError("a goal amendment must preserve goal_id.")
    if current["version"] != int(previous["version"]) + 1:
        raise GoalResearchError("a goal amendment must increment version by exactly one.")
    if current["budgets"] != previous["budgets"]:
        raise GoalResearchError("goal amendments cannot change the run's frozen budgets.")
    return current


def path_in_scope(path: str, selectors: Iterable[str]) -> bool:
    normalized = validate_relative_path(path, "changed path")
    candidate = PurePosixPath(normalized)
    for raw in selectors:
        selector = validate_scope_selector(raw)
        literal = selector.rstrip("/")
        if not any(character in selector for character in "*?["):
            if normalized == literal or normalized.startswith(literal + "/"):
                return True
        elif candidate.match(selector):
            return True
    return False


def _git(project: Path, *args: str, text: bool = False) -> bytes | str:
    try:
        completed = safety.run_hardened_git(
            project,
            list(args),
            text=text,
            timeout=60,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise GoalResearchError(f"Could not run git: {exc}") from exc
    if completed.returncode != 0:
        stderr = safety.truncate(
            safety.sanitize_text(completed.stderr),
            500,
        ).strip()
        raise GoalResearchError(f"Git command failed: git {' '.join(args)}: {stderr}")
    return completed.stdout


def require_git_root(project: Path) -> Path:
    project = project.expanduser().resolve()
    root = Path(str(_git(project, "rev-parse", "--show-toplevel", text=True)).strip()).resolve()
    if project != root:
        raise GoalResearchError(
            f"goal-research v1 requires --project-dir to be the Git root: {root}"
        )
    return root


def require_private_state_ignored(project: Path) -> None:
    try:
        completed = safety.run_hardened_git(
            project,
            [
                "check-ignore",
                "--quiet",
                ".codex-advisor/goal-research-probe",
            ],
            text=True,
            timeout=10,
            maximum_output_bytes=1024 * 1024,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise GoalResearchError(f"Could not verify .codex-advisor ignore policy: {exc}") from exc
    if completed.returncode != 0:
        raise GoalResearchError(
            "the repository must ignore .codex-advisor/ before goal-research initialization."
        )


def _status_paths(status: bytes) -> list[str]:
    chunks = status.split(b"\0")
    result: list[str] = []
    index = 0
    while index < len(chunks):
        entry = chunks[index]
        if not entry:
            index += 1
            continue
        text = entry.decode("utf-8", errors="replace")
        if len(text) < 4:
            raise GoalResearchError("Git returned malformed porcelain status.")
        code = text[:2]
        result.append(text[3:])
        index += 1
        if ("R" in code or "C" in code) and index < len(chunks) and chunks[index]:
            result.append(chunks[index].decode("utf-8", errors="replace"))
            index += 1
    return sorted(set(result))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_repository_snapshot(project: Path) -> dict[str, Any]:
    root = require_git_root(project)
    head = str(_git(root, "rev-parse", "HEAD", text=True)).strip()
    staged = bytes(
        _git(
            root,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--ignore-submodules=all",
        )
    )
    status = bytes(
        _git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        )
    )
    listed = bytes(_git(root, "ls-files", "-co", "--exclude-standard", "-z"))
    path_hashes: dict[str, dict[str, str]] = {}
    for raw in listed.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        if relative == ".codex-advisor" or relative.startswith(".codex-advisor/"):
            continue
        path = root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            path_hashes[relative] = {
                "kind": "missing",
                "mode": "0000",
                "sha256": "",
            }
            continue
        except OSError as exc:
            raise GoalResearchError(f"Could not inspect repository path {relative!r}: {exc}") from exc
        if stat.S_ISREG(metadata.st_mode):
            kind = "file"
            digest = _hash_file(path)
        elif stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
            digest = sha256_text(os.readlink(path))
        else:
            kind = "non-regular"
            digest = sha256_text(str(stat.S_IFMT(metadata.st_mode)))
        path_hashes[relative] = {
            "kind": kind,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "sha256": digest,
        }

    body = {
        "schema_version": SCHEMA_VERSION,
        "git_root": str(root),
        "head": head,
        "index_sha256": sha256_bytes(staged),
        "dirty": bool(status),
        "status_sha256": sha256_bytes(status),
        "changed_paths": _status_paths(status),
        "path_hashes": path_hashes,
    }
    body["snapshot_id"] = "snapshot-" + sha256_text(canonical_json(body))[:24]
    body["created_utc"] = utc_now()
    return validate_snapshot(body, root)


def snapshot_delta(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    if before.get("git_root") != after.get("git_root"):
        raise GoalResearchError("snapshot roots do not match.")
    old = before.get("path_hashes")
    new = after.get("path_hashes")
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise GoalResearchError("snapshot path hashes are missing.")
    paths = set(old) | set(new)
    return sorted(path for path in paths if old.get(path) != new.get(path))


def run_root(project: Path) -> Path:
    root = require_git_root(project) / RUNS_DIR
    safety.ensure_private_dir(root)
    return root


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:10]}"


def resolve_run_dir(project: Path, raw: str | Path) -> Path:
    root = run_root(project).resolve()
    candidate = Path(raw).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise GoalResearchError(
            "goal-research run directories must stay under .codex-advisor/goal-research-runs."
        ) from exc
    if not relative.parts or not path.is_dir():
        raise GoalResearchError(f"goal-research run directory does not exist: {path}")
    return path


def artifact_path(run_dir: Path, relative: str | Path) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise GoalResearchError("artifact paths must be relative to the run directory.")
    root = run_dir.resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise GoalResearchError("artifact path escaped the goal-research run directory.") from exc
    return path


def write_artifact(
    run_dir: Path,
    relative: str | Path,
    kind: str,
    value: dict[str, Any],
) -> dict[str, Any]:
    kind = validate_identifier(kind, "artifact kind")
    path = artifact_path(run_dir, relative)
    stable_payload = dict(value)
    stable_payload.setdefault("schema_version", SCHEMA_VERSION)
    if path.exists():
        payload = read_json_object(path)
        existing_stable = {key: item for key, item in payload.items() if key != "created_utc"}
        requested_stable = {
            key: item for key, item in stable_payload.items() if key != "created_utc"
        }
        if canonical_json(existing_stable) != canonical_json(requested_stable):
            raise GoalResearchError(f"Refusing to overwrite immutable artifact: {path}")
    else:
        payload = stable_payload
        immutable_json(path, payload)
    digest = sha256_text(canonical_json(payload))
    return {
        "artifact_id": f"artifact-{digest[:24]}",
        "kind": kind,
        "path": str(path.relative_to(run_dir)),
        "sha256": digest,
    }


def verify_artifact(run_dir: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(
        descriptor,
        {"artifact_id", "kind", "path", "sha256"},
        "artifact descriptor",
    )
    validate_identifier(descriptor.get("kind"), "artifact kind")
    path = artifact_path(run_dir, require_string(descriptor.get("path"), "artifact path"))
    payload = read_json_object(path)
    digest = sha256_text(canonical_json(payload))
    if digest != descriptor.get("sha256"):
        raise GoalResearchError(f"artifact digest mismatch: {path}")
    expected_id = f"artifact-{digest[:24]}"
    if descriptor.get("artifact_id") != expected_id:
        raise GoalResearchError(f"artifact id mismatch: {path}")
    return payload


def validate_snapshot(snapshot: dict[str, Any], project: Path | None = None) -> dict[str, Any]:
    required = {
        "schema_version",
        "git_root",
        "head",
        "index_sha256",
        "dirty",
        "status_sha256",
        "changed_paths",
        "path_hashes",
        "snapshot_id",
        "created_utc",
    }
    reject_unknown_keys(snapshot, required, "repository snapshot")
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise GoalResearchError("repository snapshot schema_version is invalid.")
    git_root = Path(require_string(snapshot.get("git_root"), "snapshot git_root")).resolve()
    if project is not None and git_root != project.resolve():
        raise GoalResearchError("repository snapshot is bound to a different checkout.")
    head = require_string(snapshot.get("head"), "snapshot head", maximum=128)
    index_sha = require_string(snapshot.get("index_sha256"), "snapshot index_sha256", maximum=128)
    status_sha = require_string(snapshot.get("status_sha256"), "snapshot status_sha256", maximum=128)
    if not all(re.fullmatch(r"[0-9a-f]{40,64}", item) for item in (head, index_sha, status_sha)):
        raise GoalResearchError("repository snapshot contains an invalid Git or digest identity.")
    if not isinstance(snapshot.get("dirty"), bool):
        raise GoalResearchError("repository snapshot dirty must be a boolean.")
    changed_paths = [
        validate_relative_path(item, "snapshot changed path")
        for item in string_list(snapshot.get("changed_paths"), "snapshot changed_paths")
    ]
    hashes = snapshot.get("path_hashes")
    if not isinstance(hashes, dict):
        raise GoalResearchError("repository snapshot path_hashes must be an object.")
    normalized_hashes: dict[str, Any] = {}
    for raw_path, item in hashes.items():
        path = validate_relative_path(raw_path, "snapshot path")
        if not isinstance(item, dict) or set(item) != {"kind", "mode", "sha256"}:
            raise GoalResearchError("repository snapshot path metadata is invalid.")
        kind = str(item.get("kind") or "")
        mode = str(item.get("mode") or "")
        digest = str(item.get("sha256") or "")
        if kind not in {"file", "symlink", "non-regular"} or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise GoalResearchError("repository snapshot path hash is invalid.")
        if not re.fullmatch(r"[0-7]{4}", mode):
            raise GoalResearchError("repository snapshot path mode is invalid.")
        normalized_hashes[path] = {"kind": kind, "mode": mode, "sha256": digest}
    normalized = dict(snapshot)
    normalized["git_root"] = str(git_root)
    normalized["changed_paths"] = sorted(set(changed_paths))
    normalized["path_hashes"] = normalized_hashes
    expected_body = {key: value for key, value in normalized.items() if key not in {"snapshot_id", "created_utc"}}
    expected_id = "snapshot-" + sha256_text(canonical_json(expected_body))[:24]
    if snapshot.get("snapshot_id") != expected_id:
        raise GoalResearchError("repository snapshot_id does not match its content.")
    require_string(snapshot.get("created_utc"), "snapshot created_utc", maximum=100)
    return normalized


def _event_digest(event: dict[str, Any]) -> str:
    body = {key: value for key, value in event.items() if key != "event_sha256"}
    return sha256_text(canonical_json(body))


def read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GoalResearchError(f"Could not read event log: {exc}") from exc
    events: list[dict[str, Any]] = []
    previous = ""
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise GoalResearchError(f"event log contains a blank line at sequence {index}.")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoalResearchError(f"event log JSON is invalid at sequence {index}.") from exc
        if not isinstance(event, dict):
            raise GoalResearchError(f"event {index} must be an object.")
        reject_unknown_keys(
            event,
            {
                "schema_version",
                "event_id",
                "idempotency_key",
                "sequence",
                "previous_event_sha256",
                "run_id",
                "event_type",
                "from_state",
                "to_state",
                "actor",
                "goal_version",
                "iteration_id",
                "snapshot_ids",
                "artifacts",
                "budget_effect",
                "payload",
                "created_utc",
                "event_sha256",
            },
            f"event {index}",
        )
        if event.get("schema_version") != SCHEMA_VERSION:
            raise GoalResearchError(f"event {index} has an unsupported schema_version.")
        if event.get("sequence") != index:
            raise GoalResearchError(f"event sequence gap or reorder at {index}.")
        if event.get("previous_event_sha256") != previous:
            raise GoalResearchError(f"event digest chain is broken at sequence {index}.")
        digest = _event_digest(event)
        if event.get("event_sha256") != digest:
            raise GoalResearchError(f"event digest is invalid at sequence {index}.")
        event_id = validate_identifier(event.get("event_id"), "event_id")
        idempotency_key = validate_identifier(
            event.get("idempotency_key"), "idempotency_key"
        )
        if event_id in seen_ids or idempotency_key in seen_keys:
            raise GoalResearchError(f"duplicate event identity at sequence {index}.")
        seen_ids.add(event_id)
        seen_keys.add(idempotency_key)
        previous = digest
        events.append(event)
    return events


def _initial_projection(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run["run_id"],
        "project_dir": run["project_dir"],
        "phase": PHASE_NEW,
        "goal_version": 0,
        "iteration_number": 0,
        "iteration_id": "",
        "advisor_turns_used": 0,
        "iterations_started": 0,
        "current_packet_id": "",
        "current_hypothesis_ids": [],
        "iteration_outcomes": [],
        "acceptance_status": {},
        "goal_clause_status": {},
        "critical_contradiction_ids": [],
        "blocked_reason": "",
        "blocked_from_phase": "",
        "completed": False,
        "last_event_sha256": "",
        "event_count": 0,
    }


def reduce_events(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    projection = _initial_projection(run)
    for event in events:
        if event.get("run_id") != run.get("run_id"):
            raise GoalResearchError("event run_id does not match run.json.")
        event_type = str(event.get("event_type") or "")
        from_state = str(event.get("from_state") or "")
        to_state = str(event.get("to_state") or "")
        if from_state != projection["phase"]:
            raise GoalResearchError(
                f"event {event.get('sequence')} expected phase {projection['phase']}, got {from_state}."
            )
        if event_type == "run_blocked":
            if from_state in TERMINAL_PHASES or from_state == PHASE_BLOCKED:
                raise GoalResearchError("a completed or already blocked run cannot become blocked.")
            if to_state != PHASE_BLOCKED:
                raise GoalResearchError("run_blocked must transition to BLOCKED.")
        elif event_type == "goal_amended":
            if from_state in TERMINAL_PHASES:
                raise GoalResearchError("a completed run cannot be amended.")
            if to_state != PHASE_GOAL_FROZEN:
                raise GoalResearchError("goal_amended must return to GOAL_FROZEN.")
        elif event_type == "run_resumed":
            if from_state != PHASE_BLOCKED:
                raise GoalResearchError("run_resumed requires a blocked run.")
            if to_state != projection["blocked_from_phase"] or to_state in {
                "",
                PHASE_BLOCKED,
                PHASE_COMPLETED,
            }:
                raise GoalResearchError("run_resumed does not restore the blocked phase.")
        elif (from_state, to_state) not in EVENT_TRANSITIONS.get(event_type, set()):
            raise GoalResearchError(
                f"event {event_type!r} cannot transition {from_state} -> {to_state}."
            )

        goal_version = event.get("goal_version")
        if not isinstance(goal_version, int) or goal_version < projection["goal_version"]:
            raise GoalResearchError("event goal_version moved backwards or is invalid.")
        if event_type == "run_initialized":
            if projection["goal_version"] != 0 or goal_version != run["initial_goal_version"]:
                raise GoalResearchError("run initialization goal_version is invalid.")
        elif event_type == "goal_amended":
            if goal_version != projection["goal_version"] + 1:
                raise GoalResearchError("goal amendment did not increment goal_version exactly once.")
        elif goal_version != projection["goal_version"]:
            raise GoalResearchError("event does not match the active goal version.")
        event_iteration_id = require_string(event.get("iteration_id"), "event iteration_id")
        if event_type not in {"run_initialized", "next_iteration_started", "goal_amended"}:
            if event_iteration_id != projection["iteration_id"]:
                raise GoalResearchError("event does not match the active iteration.")
        projection["goal_version"] = goal_version
        projection["phase"] = to_state
        projection["event_count"] += 1
        projection["last_event_sha256"] = event["event_sha256"]
        effect = event.get("budget_effect")
        if not isinstance(effect, dict):
            raise GoalResearchError("event budget_effect must be an object.")
        advisor_turns = effect.get("advisor_turns", 0)
        if not isinstance(advisor_turns, int) or advisor_turns < 0:
            raise GoalResearchError("event advisor_turns budget effect is invalid.")
        projection["advisor_turns_used"] += advisor_turns
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise GoalResearchError("event payload must be an object.")

        if event_type == "run_initialized":
            projection["iteration_number"] = 1
            projection["iterations_started"] = 1
            projection["iteration_id"] = require_string(
                event.get("iteration_id"), "initial iteration_id"
            )
        elif event_type == "next_iteration_started":
            projection["iteration_number"] += 1
            projection["iterations_started"] += 1
            projection["iteration_id"] = require_string(
                event.get("iteration_id"), "iteration_id"
            )
            projection["current_packet_id"] = ""
            projection["current_hypothesis_ids"] = []
        elif event_type == "goal_amended":
            projection["current_packet_id"] = ""
            projection["current_hypothesis_ids"] = []
            projection["critical_contradiction_ids"] = []
            projection["acceptance_status"] = {}
            projection["goal_clause_status"] = {}
            projection["blocked_reason"] = ""
            projection["blocked_from_phase"] = ""
            projection["iteration_number"] += 1
            projection["iterations_started"] += 1
            projection["iteration_id"] = require_string(
                event.get("iteration_id"), "amended iteration_id"
            )
        elif event_type == "hypotheses_recorded":
            ids = payload.get("hypothesis_ids")
            if not isinstance(ids, list):
                raise GoalResearchError("hypotheses_recorded requires hypothesis_ids.")
            projection["current_hypothesis_ids"] = [
                validate_identifier(item, "hypothesis id") for item in ids
            ]
        elif event_type == "packet_issued":
            projection["current_packet_id"] = validate_identifier(
                payload.get("packet_id"), "packet_id"
            )
        elif event_type == "codex_wait_started":
            packet_id = validate_identifier(payload.get("packet_id"), "packet_id")
            if packet_id != projection["current_packet_id"]:
                raise GoalResearchError("Codex wait does not match the active packet.")
        elif event_type in {"codex_implementation_recorded", "local_verification_recorded"}:
            packet_id = validate_identifier(payload.get("packet_id"), "packet_id")
            if packet_id != projection["current_packet_id"]:
                raise GoalResearchError("receipt event does not match the active packet.")
        elif event_type == "post_change_audit_recorded":
            outcome = str(payload.get("outcome") or "")
            if outcome not in ITERATION_OUTCOMES:
                raise GoalResearchError("post-change audit has an invalid outcome.")
            projection["iteration_outcomes"].append(
                {
                    "iteration_id": projection["iteration_id"],
                    "outcome": outcome,
                }
            )
            updates = payload.get("acceptance_updates", [])
            if not isinstance(updates, list):
                raise GoalResearchError("acceptance_updates must be a list.")
            for update in updates:
                if not isinstance(update, dict):
                    raise GoalResearchError("acceptance update must be an object.")
                identifier = validate_identifier(update.get("id"), "acceptance id")
                projection["acceptance_status"][identifier] = update
            clause_updates = payload.get("goal_clause_updates", [])
            if not isinstance(clause_updates, list):
                raise GoalResearchError("goal_clause_updates must be a list.")
            for update in clause_updates:
                if not isinstance(update, dict):
                    raise GoalResearchError("goal clause update must be an object.")
                identifier = validate_identifier(update.get("id"), "goal clause id")
                projection["goal_clause_status"][identifier] = update
            critical = payload.get("critical_contradiction_ids", [])
            projection["critical_contradiction_ids"] = [
                validate_identifier(item, "contradiction id") for item in critical
            ]
        elif event_type == "epistemic_refresh_recorded":
            critical = payload.get("critical_contradiction_ids", [])
            if not isinstance(critical, list):
                raise GoalResearchError("epistemic refresh critical contradictions must be a list.")
            projection["critical_contradiction_ids"] = [
                validate_identifier(item, "contradiction id") for item in critical
            ]
        elif event_type == "run_blocked":
            projection["blocked_from_phase"] = from_state
            projection["blocked_reason"] = require_string(
                payload.get("reason"), "blocked reason"
            )
        elif event_type == "run_resumed":
            projection["blocked_reason"] = ""
            projection["blocked_from_phase"] = ""
        elif event_type == "goal_completed":
            projection["completed"] = True
            projection["blocked_reason"] = ""
            projection["blocked_from_phase"] = ""

    budgets = run.get("budgets")
    if not isinstance(budgets, dict):
        raise GoalResearchError("run.json budgets are invalid.")
    if projection["advisor_turns_used"] > budgets["max_advisor_turns"]:
        raise GoalResearchError("event history exceeds the advisor-turn budget.")
    if projection["iterations_started"] > budgets["max_iterations"]:
        raise GoalResearchError("event history exceeds the iteration budget.")
    return projection


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    run = read_json_object(run_dir / "run.json")
    reject_unknown_keys(
        run,
        {
            "schema_version",
            "run_id",
            "run_dir",
            "project_dir",
            "goal_id",
            "initial_goal_version",
            "budgets",
            "created_utc",
        },
        "run.json",
    )
    if str(run.get("schema_version") or "") != SCHEMA_VERSION:
        raise GoalResearchError("run.json has an unsupported schema_version.")
    validate_identifier(run.get("run_id"), "run_id")
    validate_identifier(run.get("goal_id"), "goal_id")
    if Path(str(run.get("run_dir") or "")).resolve() != run_dir.resolve():
        raise GoalResearchError("run.json path does not match the selected run directory.")
    project = Path(require_string(run.get("project_dir"), "run project_dir")).resolve()
    if require_git_root(project) != project:
        raise GoalResearchError("run project identity no longer resolves to its Git root.")
    expected_root = (project / RUNS_DIR).resolve()
    try:
        run_dir.resolve().relative_to(expected_root)
    except ValueError as exc:
        raise GoalResearchError("run directory escaped the project's private advisor state.") from exc
    if not private_mode_ok(run_dir) or not private_mode_ok(run_dir / "run.json"):
        raise GoalResearchError("goal-research run state is not private to the current user.")
    events = read_events(run_dir)
    for event in events:
        artifacts = event.get("artifacts")
        if not isinstance(artifacts, list):
            raise GoalResearchError("event artifacts must be a list.")
        for descriptor in artifacts:
            if not isinstance(descriptor, dict):
                raise GoalResearchError("event artifact descriptor must be an object.")
            verify_artifact(run_dir, descriptor)
    projection = reduce_events(run, events)
    return run, events, projection


def write_projection(run_dir: Path, projection: dict[str, Any]) -> None:
    value = dict(projection)
    value["updated_utc"] = utc_now()
    safety.atomic_write_json(run_dir / "status.json", value, sort_keys=True)


def _report_cell(value: Any, *, maximum: int = 240) -> str:
    text = safety.truncate(safety.redact_sensitive_text(str(value or "")), maximum)
    return " ".join(text.replace("|", "\\|").split())


def _historical_contradictions(
    run_dir: Path, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"contradictions", "new_contradictions"} and isinstance(item, list):
                    for candidate in item:
                        if not isinstance(candidate, dict):
                            continue
                        identifier = str(candidate.get("id") or "")
                        if ID_PATTERN.fullmatch(identifier):
                            by_id[identifier] = candidate
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for event in events:
        for descriptor in event["artifacts"]:
            visit(verify_artifact(run_dir, descriptor))
    return [by_id[key] for key in sorted(by_id)]


def write_report(
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> None:
    lines = [
        "# Goal Research Run",
        "",
        f"- Run: `{_report_cell(run['run_id'])}`",
        f"- Phase: `{_report_cell(projection['phase'])}`",
        f"- Goal version: `{projection['goal_version']}`",
        f"- Iteration: `{_report_cell(projection['iteration_id'])}`",
        (
            "- Advisor turns: "
            f"`{projection['advisor_turns_used']}/{run['budgets']['max_advisor_turns']}`"
        ),
        (
            "- Iterations: "
            f"`{projection['iterations_started']}/{run['budgets']['max_iterations']}`"
        ),
        f"- Next action: {_report_cell(next_action(projection))}",
    ]
    if projection["blocked_reason"]:
        lines.append(f"- Blocked reason: {_report_cell(projection['blocked_reason'], maximum=500)}")

    lines.extend(["", "## Acceptance", "", "| Dimension | Status | Evidence |", "|---|---|---|"])
    for identifier, item in sorted(projection["acceptance_status"].items()):
        lines.append(
            f"| `{_report_cell(identifier)}` | `{_report_cell(item.get('status'))}` | "
            f"{_report_cell(item.get('evidence'), maximum=320)} |"
        )
    if not projection["acceptance_status"]:
        lines.append("| _none recorded_ | `unknown` | |")

    lines.extend(["", "## Goal Clauses", "", "| Clause | Status | Evidence |", "|---|---|---|"])
    for identifier, item in sorted(projection["goal_clause_status"].items()):
        lines.append(
            f"| `{_report_cell(identifier)}` | `{_report_cell(item.get('status'))}` | "
            f"{_report_cell(item.get('evidence'), maximum=320)} |"
        )
    if not projection["goal_clause_status"]:
        lines.append("| _none recorded_ | `unknown` | |")

    critical = set(projection["critical_contradiction_ids"])
    contradictions = _historical_contradictions(run_dir, events)
    lines.extend(
        [
            "",
            "## Contradiction History",
            "",
            "| Contradiction | Reported status | Currently critical | Description |",
            "|---|---|---|---|",
        ]
    )
    for item in contradictions:
        identifier = str(item.get("id") or "")
        lines.append(
            f"| `{_report_cell(identifier)}` | `{_report_cell(item.get('status'))}` | "
            f"`{'yes' if identifier in critical else 'no'}` | "
            f"{_report_cell(item.get('description'), maximum=360)} |"
        )
    if not contradictions:
        lines.append("| _none recorded_ | | `no` | |")

    lines.extend(["", "## Iteration Outcomes", ""])
    if projection["iteration_outcomes"]:
        for item in projection["iteration_outcomes"]:
            lines.append(
                f"- `{_report_cell(item.get('iteration_id'))}`: "
                f"`{_report_cell(item.get('outcome'))}`"
            )
    else:
        lines.append("- None recorded.")

    lines.extend(
        [
            "",
            "## Event History",
            "",
            "| Seq | Event | Transition | Actor | Artifacts |",
            "|---:|---|---|---|---|",
        ]
    )
    for event in events:
        artifact_paths = ", ".join(
            f"`{_report_cell(item.get('path'))}`" for item in event["artifacts"]
        )
        lines.append(
            f"| {event['sequence']} | `{_report_cell(event['event_type'])}` | "
            f"`{_report_cell(event['from_state'])}` -> `{_report_cell(event['to_state'])}` | "
            f"`{_report_cell(event['actor'])}` | {artifact_paths} |"
        )
    lines.extend(
        [
            "",
            "This file is a replaceable projection. `events.jsonl` and its referenced immutable "
            "artifacts are authoritative.",
            "",
        ]
    )
    safety.atomic_write_text(run_dir / "report.md", "\n".join(lines))


def append_event(
    run_dir: Path,
    *,
    event_type: str,
    to_state: str,
    actor: str,
    goal_version: int,
    iteration_id: str,
    artifacts: list[dict[str, Any]] | None = None,
    snapshot_ids: list[str] | None = None,
    budget_effect: dict[str, int] | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    event_type = validate_identifier(event_type, "event_type")
    actor = validate_identifier(actor, "actor")
    idempotency_key = validate_identifier(idempotency_key, "idempotency_key")
    if to_state not in ALL_PHASES:
        raise GoalResearchError("event to_state is invalid.")
    iteration_id = validate_identifier(iteration_id, "iteration_id")
    normalized_artifacts = artifacts or []
    artifact_ids: set[str] = set()
    for descriptor in normalized_artifacts:
        if not isinstance(descriptor, dict):
            raise GoalResearchError("event artifacts must contain descriptors.")
        verify_artifact(run_dir, descriptor)
        artifact_id = validate_identifier(descriptor.get("artifact_id"), "artifact_id")
        if artifact_id in artifact_ids:
            raise GoalResearchError("event contains a duplicate artifact descriptor.")
        artifact_ids.add(artifact_id)
    normalized_snapshot_ids = [
        validate_identifier(item, "snapshot_id") for item in (snapshot_ids or [])
    ]
    if len(set(normalized_snapshot_ids)) != len(normalized_snapshot_ids):
        raise GoalResearchError("event contains duplicate snapshot ids.")
    normalized_effect = budget_effect or {"advisor_turns": 0}
    if set(normalized_effect) != {"advisor_turns"}:
        raise GoalResearchError("event budget_effect supports only advisor_turns in v1.")
    if not isinstance(normalized_effect["advisor_turns"], int) or isinstance(
        normalized_effect["advisor_turns"], bool
    ) or normalized_effect["advisor_turns"] < 0:
        raise GoalResearchError("event advisor_turns budget effect is invalid.")
    normalized_payload = payload or {}
    if not isinstance(normalized_payload, dict):
        raise GoalResearchError("event payload must be an object.")
    lock = run_dir / "events.lock"
    with concurrency.InterProcessLock(lock, timeout=30.0):
        run, events, projection = load_run(run_dir)
        for existing in events:
            if existing["idempotency_key"] == idempotency_key:
                comparable = {
                    "actor": actor,
                    "event_type": event_type,
                    "to_state": to_state,
                    "goal_version": goal_version,
                    "iteration_id": iteration_id,
                    "artifacts": normalized_artifacts,
                    "snapshot_ids": normalized_snapshot_ids,
                    "budget_effect": normalized_effect,
                    "payload": normalized_payload,
                }
                existing_comparable = {
                    key: existing[key] for key in comparable
                }
                if canonical_json(comparable) != canonical_json(existing_comparable):
                    raise GoalResearchError(
                        "idempotency key was reused for a different event payload."
                    )
                return existing, projection

        sequence = len(events) + 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event-{sequence:06d}-{uuid.uuid4().hex[:12]}",
            "idempotency_key": idempotency_key,
            "sequence": sequence,
            "previous_event_sha256": events[-1]["event_sha256"] if events else "",
            "run_id": run["run_id"],
            "event_type": event_type,
            "from_state": projection["phase"],
            "to_state": to_state,
            "actor": actor,
            "goal_version": goal_version,
            "iteration_id": iteration_id,
            "snapshot_ids": normalized_snapshot_ids,
            "artifacts": normalized_artifacts,
            "budget_effect": normalized_effect,
            "payload": normalized_payload,
            "created_utc": utc_now(),
        }
        event["event_sha256"] = _event_digest(event)
        # Validate the complete candidate history before appending one durable line.
        candidate_projection = reduce_events(run, [*events, event])
        path = run_dir / "events.jsonl"
        encoded = "".join(canonical_json(item) + "\n" for item in [*events, event])
        safety.atomic_write_text(path, encoded)
        write_projection(run_dir, candidate_projection)
        write_report(run_dir, run, [*events, event], candidate_projection)
        return event, candidate_projection


def create_run(
    project: Path,
    goal: dict[str, Any],
    *,
    requested_run_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    project = require_git_root(project)
    require_private_state_ignored(project)
    normalized = validate_goal_contract(goal)
    run_id = validate_identifier(requested_run_id, "run_id") if requested_run_id else new_run_id()
    path = run_root(project) / run_id
    if path.exists():
        raise GoalResearchError(f"goal-research run already exists: {path}")
    safety.ensure_private_dir(path)
    for relative in (
        "goals",
        "acceptance",
        "goal-fidelity",
        "clean-room",
        "iterations/0001/roles",
        "final-audit",
    ):
        safety.ensure_private_dir(path / relative)
    snapshot = capture_repository_snapshot(project)
    snapshot_artifact = write_artifact(
        path,
        "iterations/0001/baseline.json",
        "repository-snapshot",
        snapshot,
    )
    goal_artifact = write_artifact(
        path,
        f"goals/v{normalized['version']}.json",
        "goal-contract",
        normalized,
    )
    acceptance_artifact = write_artifact(
        path,
        f"acceptance/v{normalized['version']}.json",
        "acceptance-contract",
        {
            "schema_version": SCHEMA_VERSION,
            "goal_id": normalized["goal_id"],
            "goal_version": normalized["version"],
            "dimensions": normalized["acceptance_dimensions"],
        },
    )
    iteration_id = "iteration-0001"
    run = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(path.resolve()),
        "project_dir": str(project),
        "goal_id": normalized["goal_id"],
        "initial_goal_version": normalized["version"],
        "budgets": normalized["budgets"],
        "created_utc": utc_now(),
    }
    immutable_json(path / "run.json", run)
    # load_run requires run.json and accepts an empty event log.
    _, projection = append_event(
        path,
        event_type="run_initialized",
        to_state=PHASE_GOAL_FROZEN,
        actor="controller",
        goal_version=normalized["version"],
        iteration_id=iteration_id,
        artifacts=[goal_artifact, acceptance_artifact, snapshot_artifact],
        snapshot_ids=[snapshot["snapshot_id"]],
        payload={
            "goal_artifact_id": goal_artifact["artifact_id"],
            "acceptance_artifact_id": acceptance_artifact["artifact_id"],
            "baseline_artifact_id": snapshot_artifact["artifact_id"],
        },
        idempotency_key=f"init-{run_id}",
    )
    return path, projection


def current_goal(run_dir: Path, projection: dict[str, Any] | None = None) -> dict[str, Any]:
    if projection is None:
        _, _, projection = load_run(run_dir)
    path = run_dir / "goals" / f"v{projection['goal_version']}.json"
    return validate_goal_contract(read_json_object(path))


def amend_goal(run_dir: Path, raw: dict[str, Any]) -> dict[str, Any]:
    run, _, projection = load_run(run_dir)
    previous = current_goal(run_dir, projection)
    current = validate_goal_amendment(previous, raw)
    if projection["iterations_started"] >= run["budgets"]["max_iterations"]:
        raise GoalResearchError("goal amendment would exceed the frozen iteration budget.")
    iteration_number = projection["iteration_number"] + 1
    iteration_id = f"iteration-{iteration_number:04d}"
    iteration_dir = run_dir / "iterations" / f"{iteration_number:04d}"
    safety.ensure_private_dir(iteration_dir / "roles")
    snapshot = capture_repository_snapshot(Path(run["project_dir"]))
    snapshot_artifact = write_artifact(
        run_dir,
        f"iterations/{iteration_number:04d}/baseline.json",
        "repository-snapshot",
        snapshot,
    )
    descriptor = write_artifact(
        run_dir,
        f"goals/v{current['version']}.json",
        "goal-contract",
        current,
    )
    acceptance = write_artifact(
        run_dir,
        f"acceptance/v{current['version']}.json",
        "acceptance-contract",
        {
            "schema_version": SCHEMA_VERSION,
            "goal_id": current["goal_id"],
            "goal_version": current["version"],
            "dimensions": current["acceptance_dimensions"],
        },
    )
    _, updated = append_event(
        run_dir,
        event_type="goal_amended",
        to_state=PHASE_GOAL_FROZEN,
        actor="codex",
        goal_version=current["version"],
        iteration_id=iteration_id,
        artifacts=[descriptor, acceptance, snapshot_artifact],
        snapshot_ids=[snapshot["snapshot_id"]],
        payload={
            "previous_goal_version": previous["version"],
            "baseline_artifact_id": snapshot_artifact["artifact_id"],
        },
        idempotency_key=f"goal-v{current['version']}",
    )
    if updated["run_id"] != run["run_id"]:
        raise GoalResearchError("goal amendment changed run identity.")
    return updated


def validate_fidelity_trace(trace: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(
        trace,
        {
            "schema_version",
            "role",
            "clause_trace",
            "proxy_drift",
            "blocking_issues",
        },
        "goal-fidelity trace",
    )
    if str(trace.get("schema_version") or "") != SCHEMA_VERSION:
        raise GoalResearchError("goal-fidelity trace schema_version is invalid.")
    if trace.get("role") != "goal-fidelity-steward":
        raise GoalResearchError("goal-fidelity trace role is invalid.")
    raw_items = trace.get("clause_trace")
    if not isinstance(raw_items, list):
        raise GoalResearchError("goal-fidelity clause_trace must be a list.")
    expected_clauses = {item["id"] for item in goal["clauses"]}
    expected_acceptance = {item["id"] for item in goal["acceptance_dimensions"]}
    expected_acceptance_by_clause = {
        clause_id: {
            item["id"]
            for item in goal["acceptance_dimensions"]
            if clause_id in item["goal_clause_ids"]
        }
        for clause_id in expected_clauses
    }
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise GoalResearchError("goal-fidelity clause entry must be an object.")
        reject_unknown_keys(
            item,
            {
                "clause_id",
                "acceptance_ids",
                "hypothesis_ids",
                "packet_ids",
                "evidence_ids",
                "covered",
                "drift_risks",
            },
            "goal-fidelity clause entry",
        )
        clause_id = validate_identifier(item.get("clause_id"), "goal clause id")
        acceptance_ids = string_list(
            item.get("acceptance_ids", []), "goal-fidelity acceptance_ids"
        )
        if set(acceptance_ids) - expected_acceptance:
            raise GoalResearchError("goal-fidelity trace references unknown acceptance ids.")
        if set(acceptance_ids) != expected_acceptance_by_clause.get(clause_id, set()):
            raise GoalResearchError(
                "goal-fidelity trace must preserve the exact clause-to-acceptance mapping."
            )
        normalized.append(
            {
                "clause_id": clause_id,
                "acceptance_ids": acceptance_ids,
                "hypothesis_ids": string_list(
                    item.get("hypothesis_ids", []), "goal-fidelity hypothesis_ids"
                ),
                "packet_ids": string_list(
                    item.get("packet_ids", []), "goal-fidelity packet_ids"
                ),
                "evidence_ids": string_list(
                    item.get("evidence_ids", []), "goal-fidelity evidence_ids"
                ),
                "covered": require_bool(item.get("covered", False), "goal-fidelity covered"),
                "drift_risks": string_list(
                    item.get("drift_risks", []), "goal-fidelity drift_risks"
                ),
            }
        )
    seen = {item["clause_id"] for item in normalized}
    if seen != expected_clauses or len(normalized) != len(expected_clauses):
        raise GoalResearchError("goal-fidelity trace must cover every goal clause exactly once.")
    blockers = string_list(trace.get("blocking_issues", []), "blocking_issues")
    proxy_drift = string_list(trace.get("proxy_drift", []), "proxy_drift")
    return {
        "schema_version": SCHEMA_VERSION,
        "role": "goal-fidelity-steward",
        "clause_trace": normalized,
        "proxy_drift": proxy_drift,
        "blocking_issues": blockers,
    }


def _known_goal_ids(goal: dict[str, Any]) -> tuple[set[str], set[str]]:
    return (
        {item["id"] for item in goal["clauses"]},
        {item["id"] for item in goal["acceptance_dimensions"]},
    )


def validate_claims(
    raw: list[dict[str, Any]],
    goal: dict[str, Any],
    *,
    role: str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if not isinstance(raw, list):
        raise GoalResearchError("claims must be a list.")
    clause_ids, acceptance_ids = _known_goal_ids(goal)
    normalized: list[dict[str, Any]] = []
    local_map: dict[str, str] = {}
    seen_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise GoalResearchError("claim entry must be an object.")
        reject_unknown_keys(
            item,
            {
                "id",
                "statement",
                "evidence_class",
                "status",
                "severity",
                "confidence",
                "basis",
                "repository_locations",
                "goal_clause_ids",
                "acceptance_ids",
            },
            "claim",
        )
        local_id = validate_identifier(item.get("id") or f"local-{index:03d}", "claim local id")
        if local_id in local_map:
            raise GoalResearchError(f"duplicate claim local id: {local_id}")
        statement = require_string(item.get("statement"), "claim statement")
        evidence_class = str(item.get("evidence_class") or "")
        if evidence_class not in EVIDENCE_CLASSES:
            raise GoalResearchError("claim evidence_class is invalid.")
        status = str(item.get("status") or "uncertain")
        if status not in CLAIM_STATUSES:
            raise GoalResearchError("claim status is invalid.")
        severity = str(item.get("severity") or "")
        if severity not in SEVERITIES:
            raise GoalResearchError("claim severity is invalid.")
        locations_raw = item.get("repository_locations", [])
        if not isinstance(locations_raw, list):
            raise GoalResearchError("claim repository_locations must be a list.")
        locations: list[dict[str, Any]] = []
        for location in locations_raw:
            if not isinstance(location, dict):
                raise GoalResearchError("repository location must be an object.")
            reject_unknown_keys(location, {"path", "line", "symbol"}, "repository location")
            line = location.get("line")
            if line is not None and (
                not isinstance(line, int) or isinstance(line, bool) or line < 1
            ):
                raise GoalResearchError("repository location line must be a positive integer.")
            locations.append(
                {
                    "path": validate_relative_path(location.get("path"), "repository path"),
                    "line": line,
                    "symbol": optional_string(location.get("symbol"), "repository symbol", maximum=500),
                }
            )
        if evidence_class == "repository_observation" and not locations:
            raise GoalResearchError("repository observations require a concrete repository location.")
        linked_clauses = string_list(item.get("goal_clause_ids", []), "claim goal_clause_ids")
        linked_acceptance = string_list(item.get("acceptance_ids", []), "claim acceptance_ids")
        if set(linked_clauses) - clause_ids:
            raise GoalResearchError("claim references unknown goal clauses.")
        if set(linked_acceptance) - acceptance_ids:
            raise GoalResearchError("claim references unknown acceptance dimensions.")
        identity = {
            "role": role,
            "statement": re.sub(r"\s+", " ", statement.strip().lower()),
            "locations": locations,
        }
        claim_id = "claim-" + sha256_text(canonical_json(identity))[:24]
        if claim_id in seen_ids:
            raise GoalResearchError("duplicate normalized claim in one role report.")
        seen_ids.add(claim_id)
        local_map[local_id] = claim_id
        normalized.append(
            {
                "id": claim_id,
                "source_role": role,
                "statement": statement,
                "evidence_class": evidence_class,
                "status": status,
                "severity": severity,
                "confidence": require_number(
                    item.get("confidence"), "claim confidence", minimum=0.0, maximum=1.0
                ),
                "basis": require_string(item.get("basis"), "claim basis"),
                "repository_locations": locations,
                "goal_clause_ids": sorted(set(linked_clauses)),
                "acceptance_ids": sorted(set(linked_acceptance)),
            }
        )
    return normalized, local_map


def validate_unknowns(raw: Any, *, role: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GoalResearchError("unknowns must be a list.")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise GoalResearchError("unknown entry must be an object.")
        reject_unknown_keys(
            item,
            {"id", "question", "impact", "next_check", "critical"},
            "unknown",
        )
        local_id = validate_identifier(item.get("id") or f"unknown-{index:03d}", "unknown id")
        identity = {
            "role": role,
            "question": re.sub(r"\s+", " ", require_string(item.get("question"), "unknown question").lower()),
        }
        result.append(
            {
                "id": "unknown-" + sha256_text(canonical_json(identity))[:24],
                "source_local_id": local_id,
                "source_role": role,
                "question": require_string(item.get("question"), "unknown question"),
                "impact": require_string(item.get("impact"), "unknown impact"),
                "next_check": require_string(item.get("next_check"), "unknown next_check"),
                "critical": require_bool(item.get("critical", False), "unknown critical"),
            }
        )
    _unique_ids(result, "unknown")
    return result


def validate_contradictions(
    raw: Any,
    *,
    role: str,
    local_claim_ids: dict[str, str],
    known_claim_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GoalResearchError("contradictions must be a list.")
    known = set(known_claim_ids or set()) | set(local_claim_ids.values())
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GoalResearchError("contradiction entry must be an object.")
        reject_unknown_keys(
            item,
            {"claim_ids", "description", "severity", "resolution_check", "critical"},
            "contradiction",
        )
        referenced = string_list(item.get("claim_ids"), "contradiction claim_ids", allow_empty=False)
        resolved = [local_claim_ids.get(identifier, identifier) for identifier in referenced]
        if set(resolved) - known:
            raise GoalResearchError("contradiction references unknown claims.")
        description = require_string(item.get("description"), "contradiction description")
        severity = str(item.get("severity") or "")
        if severity not in SEVERITIES:
            raise GoalResearchError("contradiction severity is invalid.")
        identity = {
            "claims": sorted(set(resolved)),
            "description": re.sub(r"\s+", " ", description.lower()),
        }
        result.append(
            {
                "id": "contradiction-" + sha256_text(canonical_json(identity))[:24],
                "source_role": role,
                "claim_ids": sorted(set(resolved)),
                "description": description,
                "severity": severity,
                "critical": require_bool(item.get("critical", False), "contradiction critical"),
                "resolution_check": require_string(
                    item.get("resolution_check"), "contradiction resolution_check"
                ),
                "status": "open",
            }
        )
    _unique_ids(result, "contradiction")
    return result


def validate_specialist_requests(raw: Any, goal: dict[str, Any], *, role: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GoalResearchError("specialist_requests must be a list.")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GoalResearchError("specialist request must be an object.")
        reject_unknown_keys(
            item,
            {
                "profile",
                "unresolved_question",
                "expected_evidence",
                "rationale",
                "stopping_condition",
                "priority",
            },
            "specialist request",
        )
        profile = str(item.get("profile") or "")
        if profile not in goal["allowed_specialists"]:
            raise GoalResearchError(f"specialist profile is not allowed: {profile}")
        priority = str(item.get("priority") or "medium")
        if priority not in {"high", "medium", "low"}:
            raise GoalResearchError("specialist request priority is invalid.")
        question = require_string(item.get("unresolved_question"), "specialist unresolved_question")
        identity = {"profile": profile, "question": re.sub(r"\s+", " ", question.lower())}
        result.append(
            {
                "id": "specialist-request-" + sha256_text(canonical_json(identity))[:24],
                "source_role": role,
                "profile": profile,
                "unresolved_question": question,
                "expected_evidence": require_string(
                    item.get("expected_evidence"), "specialist expected_evidence"
                ),
                "rationale": require_string(item.get("rationale"), "specialist rationale"),
                "stopping_condition": require_string(
                    item.get("stopping_condition"), "specialist stopping_condition"
                ),
                "priority": priority,
            }
        )
    _unique_ids(result, "specialist request")
    return result


def hypothesis_signature(item: dict[str, Any]) -> str:
    mechanism = re.sub(r"\s+", " ", str(item.get("mechanism") or "").strip().lower())
    predictions = sorted(
        re.sub(r"\s+", " ", str(value).strip().lower())
        for value in item.get("predictions", [])
    )
    return sha256_text(canonical_json({"mechanism": mechanism, "predictions": predictions}))


def validate_hypotheses(
    raw: list[dict[str, Any]],
    goal: dict[str, Any],
    *,
    prior: list[dict[str, Any]] | None = None,
    known_claim_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    maximum = goal["budgets"]["max_active_hypotheses"]
    if not raw or len(raw) > maximum:
        raise GoalResearchError(f"active hypotheses must contain 1-{maximum} entries.")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_signatures: set[str] = set()
    prior_by_signature = {
        hypothesis_signature(item): item for item in (prior or [])
    }
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise GoalResearchError("hypothesis entry must be an object.")
        kind = str(item.get("kind") or "").strip()
        if kind not in HYPOTHESIS_KINDS:
            raise GoalResearchError("hypothesis kind is invalid.")
        identifier = validate_identifier(
            item.get("id") or f"hypothesis-{index:02d}", "hypothesis id"
        )
        if identifier in seen_ids:
            raise GoalResearchError(f"duplicate hypothesis id: {identifier}")
        seen_ids.add(identifier)
        record = {
            "id": identifier,
            "kind": kind,
            "mechanism": require_string(item.get("mechanism"), "hypothesis mechanism"),
            "predictions": string_list(
                item.get("predictions"), "hypothesis predictions", allow_empty=False
            ),
            "falsifiers": string_list(
                item.get("falsifiers"), "hypothesis falsifiers", allow_empty=False
            ),
            "evidence_for_claim_ids": string_list(
                item.get("evidence_for_claim_ids", []),
                "hypothesis evidence_for_claim_ids",
            ),
            "evidence_against_claim_ids": string_list(
                item.get("evidence_against_claim_ids", []),
                "hypothesis evidence_against_claim_ids",
            ),
            "retry_conditions": string_list(
                item.get("retry_conditions", []), "hypothesis retry_conditions"
            ),
            "status": str(item.get("status") or "active"),
        }
        if known_claim_ids is not None:
            referenced = set(record["evidence_for_claim_ids"]) | set(
                record["evidence_against_claim_ids"]
            )
            if referenced - known_claim_ids:
                raise GoalResearchError("hypothesis references unknown claim ids.")
        if record["status"] not in {
            "active",
            "supported",
            "rejected",
            "retired",
            "inconclusive",
        }:
            raise GoalResearchError("hypothesis status is invalid.")
        signature = hypothesis_signature(record)
        if signature in seen_signatures:
            raise GoalResearchError("semantically duplicate active hypotheses are not allowed.")
        seen_signatures.add(signature)
        old = prior_by_signature.get(signature)
        changed_condition = require_string(
            item.get("changed_condition"), "changed_condition"
        ) if item.get("changed_condition") else ""
        if old and old.get("status") in {"rejected", "retired"} and not changed_condition:
            raise GoalResearchError(
                "a repeated rejected hypothesis requires a recorded changed_condition."
            )
        record["signature"] = signature
        record["changed_condition"] = changed_condition
        normalized.append(record)
    if sum(1 for item in normalized if item["kind"] == "leading") != 1:
        raise GoalResearchError("exactly one active leading hypothesis is required.")
    return normalized


def validate_information_assessment(raw: dict[str, Any]) -> dict[str, Any]:
    reject_unknown_keys(
        raw,
        {
            "field_families",
            "pipeline_stages",
            "layers",
            "loss_boundaries",
            "recommended_probes",
        },
        "information assessment",
    )
    field_families = raw.get("field_families")
    if not isinstance(field_families, list) or not field_families:
        raise GoalResearchError("information assessment requires field_families.")
    normalized_fields: list[dict[str, Any]] = []
    for item in field_families:
        if not isinstance(item, dict):
            raise GoalResearchError("field family must be an object.")
        classification = str(item.get("classification") or "")
        if classification not in FIELD_CLASSIFICATIONS:
            raise GoalResearchError("field family classification is invalid.")
        reject_unknown_keys(
            item,
            {
                "id",
                "description",
                "classification",
                "justification",
                "evidence_claim_ids",
                "goal_relevant",
            },
            "field family",
        )
        normalized_fields.append(
            {
                "id": validate_identifier(item.get("id"), "field family id"),
                "description": require_string(
                    item.get("description"), "field family description"
                ),
                "classification": classification,
                "justification": require_string(
                    item.get("justification"), "field family justification"
                ),
                "evidence_claim_ids": string_list(
                    item.get("evidence_claim_ids", []), "field evidence_claim_ids"
                ),
                "goal_relevant": require_bool(
                    item.get("goal_relevant", True), "field family goal_relevant"
                ),
            }
        )
    _unique_ids(normalized_fields, "field family")
    field_ids = {item["id"] for item in normalized_fields}

    raw_stages = raw.get("pipeline_stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise GoalResearchError("information assessment requires pipeline_stages.")
    stages: list[dict[str, Any]] = []
    for item in raw_stages:
        if not isinstance(item, dict):
            raise GoalResearchError("pipeline stage must be an object.")
        reject_unknown_keys(
            item,
            {
                "id",
                "kind",
                "description",
                "input_field_family_ids",
                "output_field_family_ids",
                "evidence_claim_ids",
                "risks",
            },
            "pipeline stage",
        )
        kind = str(item.get("kind") or "")
        if kind not in PIPELINE_STAGE_KINDS:
            raise GoalResearchError("pipeline stage kind is invalid.")
        inputs = string_list(
            item.get("input_field_family_ids", []), "pipeline input_field_family_ids"
        )
        outputs = string_list(
            item.get("output_field_family_ids", []), "pipeline output_field_family_ids"
        )
        if (set(inputs) | set(outputs)) - field_ids:
            raise GoalResearchError("pipeline stage references unknown field families.")
        stages.append(
            {
                "id": validate_identifier(item.get("id"), "pipeline stage id"),
                "kind": kind,
                "description": require_string(item.get("description"), "pipeline stage description"),
                "input_field_family_ids": sorted(set(inputs)),
                "output_field_family_ids": sorted(set(outputs)),
                "evidence_claim_ids": string_list(
                    item.get("evidence_claim_ids", []), "pipeline evidence_claim_ids"
                ),
                "risks": string_list(item.get("risks", []), "pipeline risks"),
            }
        )
    _unique_ids(stages, "pipeline stage")
    stage_ids = {item["id"] for item in stages}
    raw_layers = raw.get("layers")
    if not isinstance(raw_layers, dict):
        raise GoalResearchError("information assessment layers must be an object.")
    layers: dict[str, Any] = {}
    for layer in INFORMATION_LAYERS:
        item = raw_layers.get(layer)
        if not isinstance(item, dict):
            raise GoalResearchError(f"information layer {layer} is missing.")
        reject_unknown_keys(item, {"status", "evidence_claim_ids", "unknowns"}, layer)
        status = str(item.get("status") or "")
        if status not in INFORMATION_STATUSES:
            raise GoalResearchError(f"information layer {layer} status is invalid.")
        layers[layer] = {
            "status": status,
            "evidence_claim_ids": string_list(
                item.get("evidence_claim_ids", []), f"{layer} evidence_claim_ids"
            ),
            "unknowns": string_list(item.get("unknowns", []), f"{layer} unknowns"),
        }
    raw_boundaries = raw.get("loss_boundaries", [])
    if not isinstance(raw_boundaries, list):
        raise GoalResearchError("loss_boundaries must be a list.")
    boundaries: list[dict[str, Any]] = []
    for item in raw_boundaries:
        if not isinstance(item, dict):
            raise GoalResearchError("loss boundary must be an object.")
        reject_unknown_keys(
            item,
            {
                "id",
                "stage_id",
                "field_family_ids",
                "failure_layer",
                "description",
                "critical",
                "evidence_claim_ids",
                "discriminating_checks",
            },
            "loss boundary",
        )
        stage_id = validate_identifier(item.get("stage_id"), "loss boundary stage_id")
        fields = string_list(
            item.get("field_family_ids"), "loss boundary field_family_ids", allow_empty=False
        )
        layer = str(item.get("failure_layer") or "")
        if stage_id not in stage_ids or set(fields) - field_ids:
            raise GoalResearchError("loss boundary references an unknown stage or field family.")
        if layer not in FAILURE_LAYERS:
            raise GoalResearchError("loss boundary failure_layer is invalid.")
        boundaries.append(
            {
                "id": validate_identifier(item.get("id"), "loss boundary id"),
                "stage_id": stage_id,
                "field_family_ids": sorted(set(fields)),
                "failure_layer": layer,
                "description": require_string(item.get("description"), "loss boundary description"),
                "critical": require_bool(item.get("critical", False), "loss boundary critical"),
                "evidence_claim_ids": string_list(
                    item.get("evidence_claim_ids", []), "loss boundary evidence_claim_ids"
                ),
                "discriminating_checks": string_list(
                    item.get("discriminating_checks"),
                    "loss boundary discriminating_checks",
                    allow_empty=False,
                ),
            }
        )
    _unique_ids(boundaries, "loss boundary")
    relevant_unexplained = {
        item["id"]
        for item in normalized_fields
        if item["goal_relevant"]
        and item["classification"] in {"unexplained", "unavailable"}
    }
    covered_unexplained = {
        field_id
        for boundary in boundaries
        if boundary["critical"]
        for field_id in boundary["field_family_ids"]
    }
    missing_boundaries = sorted(relevant_unexplained - covered_unexplained)
    if missing_boundaries:
        raise GoalResearchError(
            "goal-relevant unexplained fields require a critical loss boundary: "
            + ", ".join(missing_boundaries)
        )
    for boundary in boundaries:
        if boundary["critical"] and layers[boundary["failure_layer"]]["status"] == "supported":
            raise GoalResearchError(
                "a critical loss boundary cannot coexist with a supported failure layer."
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "field_families": normalized_fields,
        "pipeline_stages": stages,
        "layers": layers,
        "loss_boundaries": boundaries,
        "recommended_probes": string_list(
            raw.get("recommended_probes", []), "recommended_probes"
        ),
    }


def validate_specialist_selection(
    raw: dict[str, Any],
    goal: dict[str, Any],
    *,
    known_unknown_ids: set[str],
) -> dict[str, Any]:
    reject_unknown_keys(raw, {"schema_version", "selected", "omitted_reason"}, "specialist selection")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GoalResearchError("specialist selection schema_version is invalid.")
    selected = raw.get("selected")
    if not isinstance(selected, list):
        raise GoalResearchError("specialist selection selected must be a list.")
    maximum = goal["budgets"]["max_specialists_per_iteration"]
    if len(selected) > maximum:
        raise GoalResearchError(f"specialist selection exceeds the {maximum}-role budget.")
    normalized: list[dict[str, Any]] = []
    profiles: set[str] = set()
    for item in selected:
        if not isinstance(item, dict):
            raise GoalResearchError("selected specialist must be an object.")
        reject_unknown_keys(
            item,
            {
                "profile",
                "unknown_id",
                "unresolved_question",
                "expected_evidence",
                "rationale",
                "stopping_condition",
            },
            "selected specialist",
        )
        profile = str(item.get("profile") or "")
        if profile not in goal["allowed_specialists"] or profile in profiles:
            raise GoalResearchError("selected specialist is disallowed or duplicated.")
        unknown_id = validate_identifier(item.get("unknown_id"), "specialist unknown_id")
        if unknown_id not in known_unknown_ids:
            raise GoalResearchError("selected specialist is not tied to a known unresolved question.")
        profiles.add(profile)
        normalized.append(
            {
                "profile": profile,
                "unknown_id": unknown_id,
                "unresolved_question": require_string(
                    item.get("unresolved_question"), "specialist unresolved_question"
                ),
                "expected_evidence": require_string(
                    item.get("expected_evidence"), "specialist expected_evidence"
                ),
                "rationale": require_string(item.get("rationale"), "specialist rationale"),
                "stopping_condition": require_string(
                    item.get("stopping_condition"), "specialist stopping_condition"
                ),
            }
        )
    omitted_reason = optional_string(raw.get("omitted_reason"), "specialist omitted_reason")
    if not normalized and not omitted_reason:
        raise GoalResearchError("a no-specialist decision requires omitted_reason.")
    return {
        "schema_version": SCHEMA_VERSION,
        "selected": normalized,
        "omitted_reason": omitted_reason,
    }


def validate_implementation_packet(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    baseline_snapshot_id: str,
    hypotheses: list[dict[str, Any]],
    known_claim_ids: set[str],
    open_contradiction_ids: set[str],
) -> dict[str, Any]:
    reject_unknown_keys(
        raw,
        {
            "schema_version",
            "packet_id",
            "run_id",
            "goal_version",
            "iteration_id",
            "baseline_snapshot_id",
            "hypothesis_id",
            "objective",
            "rationale",
            "permitted_scope",
            "forbidden_scope",
            "evidence_claim_ids",
            "required_checks",
            "expected_signals",
            "rejection_criteria",
            "rollback_guidance",
            "open_contradiction_ids",
        },
        "implementation packet",
    )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GoalResearchError("implementation packet schema_version is invalid.")
    if raw.get("run_id") != run_id or raw.get("iteration_id") != iteration_id:
        raise GoalResearchError("implementation packet is bound to a different run or iteration.")
    if raw.get("goal_version") != goal["version"]:
        raise GoalResearchError("implementation packet is bound to a different goal version.")
    if raw.get("baseline_snapshot_id") != baseline_snapshot_id:
        raise GoalResearchError("implementation packet baseline snapshot is stale.")
    hypothesis_id = validate_identifier(raw.get("hypothesis_id"), "packet hypothesis_id")
    active_ids = {item["id"] for item in hypotheses if item.get("status") == "active"}
    if hypothesis_id not in active_ids:
        raise GoalResearchError("implementation packet must select one active hypothesis.")
    permitted = [
        validate_scope_selector(item)
        for item in string_list(raw.get("permitted_scope"), "packet permitted_scope", allow_empty=False)
    ]
    goal_scope = goal["allowed_scope"]
    for selector in permitted:
        if not selector_within_scope(selector, goal_scope):
            raise GoalResearchError("implementation packet expands beyond the goal's allowed scope.")
    evidence_ids = string_list(raw.get("evidence_claim_ids"), "packet evidence_claim_ids", allow_empty=False)
    if set(evidence_ids) - known_claim_ids:
        raise GoalResearchError("implementation packet references unknown evidence claims.")
    contradiction_ids = string_list(
        raw.get("open_contradiction_ids", []), "packet open_contradiction_ids"
    )
    if set(contradiction_ids) - open_contradiction_ids:
        raise GoalResearchError("implementation packet references unknown open contradictions.")
    if set(contradiction_ids) != open_contradiction_ids:
        raise GoalResearchError("implementation packet must preserve every open contradiction.")
    required_checks = string_list(
        raw.get("required_checks"), "packet required_checks", allow_empty=False
    )
    if len(required_checks) != len(set(required_checks)):
        raise GoalResearchError("implementation packet required_checks must be unique.")
    packet_id = validate_identifier(raw.get("packet_id"), "packet_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "packet_id": packet_id,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "baseline_snapshot_id": baseline_snapshot_id,
        "hypothesis_id": hypothesis_id,
        "objective": require_string(raw.get("objective"), "packet objective"),
        "rationale": require_string(raw.get("rationale"), "packet rationale"),
        "permitted_scope": sorted(set(permitted)),
        "forbidden_scope": [
            validate_scope_selector(item)
            for item in string_list(raw.get("forbidden_scope", []), "packet forbidden_scope")
        ],
        "evidence_claim_ids": sorted(set(evidence_ids)),
        "required_checks": required_checks,
        "expected_signals": string_list(
            raw.get("expected_signals"), "packet expected_signals", allow_empty=False
        ),
        "rejection_criteria": string_list(
            raw.get("rejection_criteria"), "packet rejection_criteria", allow_empty=False
        ),
        "rollback_guidance": require_string(raw.get("rollback_guidance"), "packet rollback_guidance"),
        "open_contradiction_ids": sorted(set(contradiction_ids)),
    }


def validate_command_results(raw: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GoalResearchError(f"{label} must be a list.")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GoalResearchError(f"{label} entry must be an object.")
        reject_unknown_keys(item, {"command", "exit_code", "duration_seconds", "evidence_path"}, label)
        exit_code = item.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise GoalResearchError(f"{label} exit_code must be an integer.")
        evidence_path = optional_string(
            item.get("evidence_path"), f"{label} evidence_path", maximum=4_000
        )
        if evidence_path:
            evidence_path = validate_relative_path(evidence_path, f"{label} evidence_path")
        result.append(
            {
                "command": require_string(item.get("command"), f"{label} command", maximum=8_000),
                "exit_code": exit_code,
                "duration_seconds": require_number(
                    item.get("duration_seconds", 0),
                    f"{label} duration_seconds",
                    minimum=0,
                    maximum=31_536_000,
                ),
                "evidence_path": evidence_path,
            }
        )
    return result


def validate_required_check_results(
    raw: Any, required_checks: list[str]
) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise GoalResearchError("verification required_check_results must be a list.")
    normalized: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GoalResearchError("verification required check result must be an object.")
        reject_unknown_keys(item, {"check", "status", "evidence"}, "required check result")
        status_value = str(item.get("status") or "")
        if status_value not in {"passed", "failed"}:
            raise GoalResearchError("required check result status must be passed or failed.")
        normalized.append(
            {
                "check": require_string(item.get("check"), "required check", maximum=4_000),
                "status": status_value,
                "evidence": require_string(
                    item.get("evidence"), "required check evidence", maximum=20_000
                ),
            }
        )
    checks = [item["check"] for item in normalized]
    if len(checks) != len(set(checks)):
        raise GoalResearchError("verification required checks must be unique.")
    if set(checks) != set(required_checks):
        raise GoalResearchError("verification must account for every packet required check exactly once.")
    by_check = {item["check"]: item for item in normalized}
    return [by_check[check] for check in required_checks]


def validate_codex_receipt(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    packet: dict[str, Any],
    baseline: dict[str, Any],
    resulting: dict[str, Any],
) -> dict[str, Any]:
    reject_unknown_keys(
        raw,
        {
            "schema_version",
            "run_id",
            "goal_version",
            "iteration_id",
            "packet_id",
            "baseline_snapshot_id",
            "resulting_snapshot_id",
            "summary",
            "changed_paths",
            "commands",
            "retained_evidence_paths",
        },
        "Codex receipt",
    )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GoalResearchError("Codex receipt schema_version is invalid.")
    expected_bindings = {
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "packet_id": packet["packet_id"],
        "baseline_snapshot_id": baseline["snapshot_id"],
        "resulting_snapshot_id": resulting["snapshot_id"],
    }
    for key, expected in expected_bindings.items():
        if raw.get(key) != expected:
            raise GoalResearchError(f"Codex receipt {key} does not match the active iteration.")
    if baseline.get("head") != resulting.get("head"):
        raise GoalResearchError("Codex receipt detected a commit or HEAD change during the packet.")
    if baseline.get("index_sha256") != resulting.get("index_sha256"):
        raise GoalResearchError("Codex receipt detected a staged-index change during the packet.")
    actual_delta = snapshot_delta(baseline, resulting)
    reported_delta = sorted(
        set(
            validate_relative_path(item, "Codex receipt changed path")
            for item in string_list(raw.get("changed_paths"), "Codex receipt changed_paths")
        )
    )
    if actual_delta != reported_delta:
        raise GoalResearchError("Codex receipt changed_paths do not match the repository snapshot delta.")
    for path in actual_delta:
        if not path_in_scope(path, goal["allowed_scope"]):
            raise GoalResearchError(f"Codex changed a path outside the frozen goal scope: {path}")
        if not path_in_scope(path, packet["permitted_scope"]):
            raise GoalResearchError(f"Codex changed a path outside the packet scope: {path}")
        if any(path_in_scope(path, [selector]) for selector in packet["forbidden_scope"]):
            raise GoalResearchError(f"Codex changed a path forbidden by the packet: {path}")
    return {
        **expected_bindings,
        "schema_version": SCHEMA_VERSION,
        "summary": require_string(raw.get("summary"), "Codex receipt summary"),
        "changed_paths": actual_delta,
        "commands": validate_command_results(raw.get("commands", []), "Codex receipt commands"),
        "retained_evidence_paths": [
            validate_relative_path(item, "retained evidence path")
            for item in string_list(
                raw.get("retained_evidence_paths", []), "retained_evidence_paths"
            )
        ],
    }


def validate_verification_receipt(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    packet: dict[str, Any],
    resulting_snapshot_id: str,
) -> dict[str, Any]:
    reject_unknown_keys(
        raw,
        {
            "schema_version",
            "run_id",
            "goal_version",
            "iteration_id",
            "packet_id",
            "resulting_snapshot_id",
            "commands",
            "required_check_results",
            "acceptance_results",
            "notes",
        },
        "verification receipt",
    )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise GoalResearchError("verification receipt schema_version is invalid.")
    expected = {
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "packet_id": packet["packet_id"],
        "resulting_snapshot_id": resulting_snapshot_id,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise GoalResearchError(f"verification receipt {key} does not match the active iteration.")
    commands = validate_command_results(raw.get("commands"), "verification commands")
    if not commands:
        raise GoalResearchError("verification receipt requires at least one local command result.")
    required_check_results = validate_required_check_results(
        raw.get("required_check_results"), packet["required_checks"]
    )
    raw_results = raw.get("acceptance_results")
    if not isinstance(raw_results, list) or not raw_results:
        raise GoalResearchError("verification receipt requires acceptance_results.")
    known_acceptance = {item["id"] for item in goal["acceptance_dimensions"]}
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            raise GoalResearchError("verification acceptance result must be an object.")
        reject_unknown_keys(item, {"id", "status", "evidence", "evidence_class"}, "acceptance result")
        identifier = validate_identifier(item.get("id"), "acceptance result id")
        status_value = str(item.get("status") or "")
        evidence_class = str(item.get("evidence_class") or "")
        if identifier not in known_acceptance or status_value not in ACCEPTANCE_STATUSES:
            raise GoalResearchError("verification acceptance result is invalid.")
        validate_acceptance_status_authority(goal, identifier, status_value)
        if evidence_class != "codex_local_result":
            raise GoalResearchError("local verification evidence must remain codex_local_result.")
        results.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence": require_string(item.get("evidence"), "acceptance result evidence"),
                "evidence_class": evidence_class,
            }
        )
    _unique_ids(results, "acceptance result")
    required_acceptance = {
        item["id"] for item in goal["acceptance_dimensions"] if item["required"]
    }
    if not required_acceptance.issubset({item["id"] for item in results}):
        raise GoalResearchError("verification must account for every required acceptance dimension.")
    if any(item["status"] == "passed" for item in results):
        if any(item["exit_code"] != 0 for item in commands):
            raise GoalResearchError("passed acceptance cannot rely on a failed verification command.")
        if any(item["status"] != "passed" for item in required_check_results):
            raise GoalResearchError("passed acceptance requires every packet check to pass.")
    return {
        **expected,
        "schema_version": SCHEMA_VERSION,
        "commands": commands,
        "required_check_results": required_check_results,
        "acceptance_results": results,
        "notes": optional_string(raw.get("notes"), "verification notes"),
    }


def validate_audit_updates(raw: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any]:
    acceptance_ids = {item["id"] for item in goal["acceptance_dimensions"]}
    clause_ids = {item["id"] for item in goal["clauses"]}
    acceptance_updates = raw.get("acceptance_updates")
    clause_updates = raw.get("goal_clause_updates")
    if not isinstance(acceptance_updates, list) or not isinstance(clause_updates, list):
        raise GoalResearchError("audit updates must contain acceptance and goal-clause lists.")
    normalized_acceptance: list[dict[str, Any]] = []
    for item in acceptance_updates:
        if not isinstance(item, dict):
            raise GoalResearchError("audit acceptance update must be an object.")
        reject_unknown_keys(item, {"id", "status", "evidence_classes", "evidence"}, "audit acceptance update")
        identifier = validate_identifier(item.get("id"), "audit acceptance id")
        status_value = str(item.get("status") or "")
        classes = string_list(item.get("evidence_classes", []), "audit evidence_classes")
        if identifier not in acceptance_ids or status_value not in ACCEPTANCE_STATUSES:
            raise GoalResearchError("audit acceptance update is invalid.")
        validate_acceptance_status_authority(goal, identifier, status_value)
        if set(classes) - EVIDENCE_CLASSES:
            raise GoalResearchError("audit acceptance update has an invalid evidence class.")
        normalized_acceptance.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence_classes": sorted(set(classes)),
                "evidence": require_string(item.get("evidence"), "audit acceptance evidence"),
            }
        )
    _unique_ids(normalized_acceptance, "audit acceptance")
    normalized_clauses: list[dict[str, Any]] = []
    for item in clause_updates:
        if not isinstance(item, dict):
            raise GoalResearchError("audit goal-clause update must be an object.")
        reject_unknown_keys(item, {"id", "status", "evidence"}, "audit goal-clause update")
        identifier = validate_identifier(item.get("id"), "audit goal clause id")
        status_value = str(item.get("status") or "")
        if identifier not in clause_ids or status_value not in CLAUSE_STATUSES:
            raise GoalResearchError("audit goal-clause update is invalid.")
        normalized_clauses.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence": require_string(item.get("evidence"), "audit goal-clause evidence"),
            }
        )
    _unique_ids(normalized_clauses, "audit goal clause")
    critical = [
        validate_identifier(item, "critical contradiction id")
        for item in string_list(
            raw.get("critical_contradiction_ids", []), "critical_contradiction_ids"
        )
    ]
    return {
        "acceptance_updates": normalized_acceptance,
        "goal_clause_updates": normalized_clauses,
        "critical_contradiction_ids": sorted(set(critical)),
    }


def completion_ready(
    goal: dict[str, Any],
    projection: dict[str, Any],
    final_audit: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    required_acceptance = {
        item["id"] for item in goal["acceptance_dimensions"] if item["required"]
    }
    required_clauses = {item["id"] for item in goal["clauses"] if item["critical"]}
    acceptance = projection.get("acceptance_status", {})
    clauses = projection.get("goal_clause_status", {})
    for identifier in sorted(required_acceptance):
        item = acceptance.get(identifier)
        if not isinstance(item, dict) or not acceptance_status_satisfies_gate(
            goal, identifier, str(item.get("status") or "")
        ):
            reasons.append(f"required acceptance dimension has not passed: {identifier}")
            continue
        if item.get("status") == "waived":
            continue
        classes = set(item.get("evidence_classes", []))
        if not classes & {"codex_local_result", "independent_audit_result"}:
            reasons.append(f"acceptance dimension lacks strong evidence: {identifier}")
    for identifier in sorted(required_clauses):
        item = clauses.get(identifier)
        if not isinstance(item, dict) or item.get("status") != "supported":
            reasons.append(f"critical goal clause is not supported: {identifier}")
    if projection.get("critical_contradiction_ids"):
        reasons.append("critical contradictions remain unresolved")
    audit_clauses = final_audit.get("goal_clause_status")
    audit_acceptance = final_audit.get("acceptance_status")
    if not isinstance(audit_clauses, list) or not isinstance(audit_acceptance, list):
        reasons.append("final audit is missing clause or acceptance status")
    else:
        clause_map = {
            str(item.get("id")): item for item in audit_clauses if isinstance(item, dict)
        }
        acceptance_map = {
            str(item.get("id")): item for item in audit_acceptance if isinstance(item, dict)
        }
        for identifier in sorted(required_clauses):
            if clause_map.get(identifier, {}).get("status") != "supported":
                reasons.append(f"final audit did not support goal clause: {identifier}")
        for identifier in sorted(required_acceptance):
            if not acceptance_status_satisfies_gate(
                goal,
                identifier,
                str(acceptance_map.get(identifier, {}).get("status") or ""),
            ):
                reasons.append(f"final audit did not pass acceptance dimension: {identifier}")
    blockers = final_audit.get("blocking_findings", [])
    if not isinstance(blockers, list) or blockers:
        reasons.append("final audit contains blocking findings")
    if final_audit.get("recommend_completion") is not True:
        reasons.append("final auditor did not recommend completion")
    return not reasons, reasons


def next_action(projection: dict[str, Any]) -> str:
    mapping = {
        PHASE_GOAL_FROZEN: "run goal-fidelity stewardship",
        PHASE_GOAL_FIDELITY: "run independent repo-aware grounding",
        PHASE_CLEAN_ROOM: "normalize competing hypotheses",
        PHASE_HYPOTHESES: "run one bounded claim-ID challenge round",
        PHASE_CHALLENGE: "synthesize one implementation packet",
        PHASE_PACKET_READY: "enter the explicit Codex implementation wait",
        PHASE_WAITING_CODEX: "Codex must implement the active packet and record a receipt",
        PHASE_WAITING_VERIFICATION: "Codex must record local verification evidence",
        PHASE_POST_AUDIT: "run a fresh repo-aware post-change audit",
        PHASE_ITERATION_CLOSED: "refresh epistemic state and evaluate stopping rules",
        PHASE_EPISTEMIC_REFRESH: "start the next iteration or final clean-room audit",
        PHASE_FINAL_AUDIT: "run the blind final completion audit",
        PHASE_COMPLETED: "goal is complete",
        PHASE_BLOCKED: "resolve the recorded blocker or amend the goal",
    }
    return mapping.get(str(projection.get("phase") or ""), "inspect run state")


def public_status(projection: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": projection["run_id"],
        "run_dir": str(run_dir),
        "phase": projection["phase"],
        "goal_version": projection["goal_version"],
        "iteration_number": projection["iteration_number"],
        "iteration_id": projection["iteration_id"],
        "advisor_turns_used": projection["advisor_turns_used"],
        "iterations_started": projection["iterations_started"],
        "current_packet_id": projection["current_packet_id"],
        "completed": projection["completed"],
        "blocked_reason": projection["blocked_reason"],
        "blocked_from_phase": projection["blocked_from_phase"],
        "next_action": next_action(projection),
    }
