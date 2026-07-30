#!/usr/bin/env python3
"""Bounded advisor roles and recoverable prompt phases for goal-research."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import advisor_safety as safety
import goal_research_state as state


CORE_ROLES = ("cartographer", "falsifier", "verifier")
FRESH_CONVERSATION_ROLES = frozenset({"clean-room-remapper", "final-blind-auditor"})
CHALLENGE_CLAIM_DISPOSITIONS = {"supported", "challenged", "uncertain"}
CHALLENGE_HYPOTHESIS_DISPOSITIONS = {"retain", "weaken", "reject"}
INVESTIGATION_KINDS = {"inspection", "experiment", "implementation"}
RELATIVE_COSTS = {"low", "medium", "high"}
SYNTHESIS_DECISIONS = {"issue_packet", "block"}
POST_HYPOTHESIS_STATUSES = {"active", "supported", "rejected", "inconclusive"}
EPISTEMIC_HYPOTHESIS_STATUSES = POST_HYPOTHESIS_STATUSES | {"retired"}
EPISTEMIC_RECOMMENDATIONS = {"next_iteration", "final_audit", "block"}
CLOSED_VOCABULARY_RULE = (
    "Every listed field is an enum. Use one exact value from its list; do not invent "
    "synonyms, composite labels, or new categories. An example value in the output shape "
    "does not narrow or extend these lists."
)
REPO_ROLE_DESCRIPTIONS = {
    "cartographer": (
        "Map the implemented system and every goal-relevant data, information, and control "
        "path. Distinguish raw availability, preservation, representation, learnability, "
        "utilization, evaluation validity, and causal or operational validity."
    ),
    "falsifier": (
        "Try to disprove the current framing. Find omitted inputs, destructive transformations, "
        "unused information, proxy objectives, leakage, unsupported assumptions, and ways tests "
        "can pass while the actual goal fails."
    ),
    "verifier": (
        "Map each acceptance dimension and original goal clause to concrete repository evidence "
        "and the cheapest discriminating local checks Codex can run. Identify unsupported gates."
    ),
    "clean-room-remapper": (
        "Independently rebuild the problem and system map from the original goal and repository. "
        "Do not inherit or assume any prior diagnosis, hypothesis, packet, or synthesis."
    ),
    "post-change-auditor": (
        "Inspect the resulting repository snapshot and judge the active packet against the goal, "
        "local verification evidence, open contradictions, and actual changed implementation."
    ),
    "final-blind-auditor": (
        "Blindly audit the final repository against the original goal and acceptance matrix. "
        "Look for omitted clauses, proxy success, inherited framing errors, and unsupported closure."
    ),
    "architecture-integration": "Resolve the stated architecture or integration unknown only.",
    "data-ml-causality": "Resolve the stated data, ML, information-use, or causality unknown only.",
    "domain-workflow": "Resolve the stated domain or operational workflow unknown only.",
    "performance-reliability": "Resolve the stated performance or reliability unknown only.",
    "security-privacy": "Resolve the stated security or privacy unknown only.",
}


def repo_role_conversation_key(run_id: str, role: str) -> str | None:
    """Return one stable per-goal chat key, except for deliberately blind roles."""
    run_id = state.validate_identifier(run_id, "run_id")
    if role not in REPO_ROLE_DESCRIPTIONS:
        raise GoalResearchRoleError(f"unknown repo-aware goal-research role: {role}")
    if role in FRESH_CONVERSATION_ROLES:
        return None
    return f"goal-research-{run_id}-{role}"


class GoalResearchRoleError(state.GoalResearchError):
    """Raised when a role or prompt-only checkpoint cannot safely advance."""

    def __init__(self, message: str, *, advisor_turns_attempted: int = 0) -> None:
        super().__init__(message)
        self.advisor_turns_attempted = max(0, int(advisor_turns_attempted))


class GoalResearchPending(GoalResearchRoleError):
    """Raised when an accepted remote turn is still running and must be resumed."""


@dataclass(frozen=True)
class RoleResult:
    role: str
    report: dict[str, Any]
    metadata: dict[str, Any]
    run_dir: Path
    workspace_generation: str
    workspace_fingerprint: str
    resumed: bool


def _balanced_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    start = -1
    depth = 0
    quote = False
    escaped = False
    for index, character in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
            continue
        if character == '"':
            quote = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start : index + 1])
                start = -1
    return candidates


def extract_json_object(text: str, label: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, count=1, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value, count=1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    matches: list[dict[str, Any]] = []
    for candidate in _balanced_json_candidates(text):
        try:
            item = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            matches.append(item)
    if len(matches) != 1:
        raise GoalResearchRoleError(
            f"{label} must return exactly one valid JSON object; found {len(matches)}."
        )
    return matches[0]


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "head": snapshot["head"],
        "index_sha256": snapshot["index_sha256"],
        "status_sha256": snapshot["status_sha256"],
        "dirty": snapshot["dirty"],
        "changed_paths": snapshot["changed_paths"],
    }


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True)


def role_output_vocabulary(goal: dict[str, Any]) -> dict[str, Any]:
    """Return the closed vocabularies shared by role prompts and validators."""
    return {
        "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
        "claims[].evidence_class": sorted(state.EVIDENCE_CLASSES),
        "claims[].status": sorted(state.CLAIM_STATUSES),
        "claims[].severity": sorted(state.SEVERITIES),
        "contradictions[].severity": sorted(state.SEVERITIES),
        "specialist_requests[].profile": list(goal["allowed_specialists"]),
        "specialist_requests[].priority": ["high", "medium", "low"],
        "hypothesis_candidates[].kind": sorted(state.HYPOTHESIS_KINDS),
        "information_assessment.field_families[].classification": sorted(
            state.FIELD_CLASSIFICATIONS
        ),
        "information_assessment.pipeline_stages[].kind": sorted(
            state.PIPELINE_STAGE_KINDS
        ),
        "information_assessment.layers.*.status": sorted(state.INFORMATION_STATUSES),
        "information_assessment.loss_boundaries[].failure_layer": list(
            state.INFORMATION_LAYERS
        ),
        "classification_semantics": {
            "retained": "preserved in a materially equivalent usable form",
            "transformed": "preserved through a documented non-aggregating transformation",
            "aggregated": "compressed or pooled so some instance-level detail is no longer distinct",
            "excluded_with_justification": (
                "intentionally omitted with a goal-consistent explicit justification"
            ),
            "unavailable": "not present at the source boundary",
            "unexplained": (
                "goal-relevant but omitted, lost, or not accounted for without an accepted justification"
            ),
        },
        "claim_status_semantics": {
            "supported": "the cited evidence supports the claim statement",
            "contradicted": "the cited evidence directly conflicts with the claim statement",
            "uncertain": "the available evidence is insufficient or mixed",
        },
        "hypothesis_kind_semantics": {
            "leading": "the strongest current candidate mechanism",
            "alternative": "a materially different competing mechanism",
            "null_measurement": "a null, instrumentation, evaluation, or measurement explanation",
        },
        "important_disambiguation": [
            "unsupported is valid only for information layer status, never for claim status",
            "lost, omitted, dropped, and lost_at_encoding are descriptions, not classification values; use unavailable or unexplained according to the source evidence",
            "supporting is not a hypothesis kind; use leading, alternative, or null_measurement",
        ],
    }


def role_prompt(
    *,
    role: str,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    snapshot: dict[str, Any],
    task_context: dict[str, Any] | None = None,
    clean_room: bool = False,
) -> str:
    if role not in REPO_ROLE_DESCRIPTIONS:
        raise GoalResearchRoleError(f"unknown goal-research role: {role}")
    context = task_context or {}
    clean_room_rule = (
        "This is a clean-room pass. You receive no prior conclusions. Do not infer or request "
        "prior reports, accepted hypotheses, implementation packets, or synthesis output."
        if clean_room
        else "Work independently. Do not ask for or assume another advisor's conclusions."
    )
    information_required = role in {"cartographer", "clean-room-remapper"}
    temporary_specialist = role in state.SPECIALIST_CATALOG
    schema = {
        "schema_version": state.SCHEMA_VERSION,
        "role": role,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": snapshot["snapshot_id"],
        "summary": "bounded conclusion",
        "claims": [
            {
                "id": "local-claim-id",
                "statement": "one falsifiable statement",
                "evidence_class": "repository_observation",
                "status": "supported",
                "severity": "high",
                "confidence": 0.8,
                "basis": "what the evidence establishes and what it does not",
                "repository_locations": [
                    {"path": "relative/path.py", "line": 1, "symbol": "optional_symbol"}
                ],
                "goal_clause_ids": ["goal-clause-id"],
                "acceptance_ids": ["acceptance-id"],
            }
        ],
        "contradictions": [
            {
                "claim_ids": ["local-claim-id"],
                "description": "conflict or tension",
                "severity": "high",
                "critical": False,
                "resolution_check": "specific discriminating check",
            }
        ],
        "unknowns": [
            {
                "id": "local-unknown-id",
                "question": "specific unresolved question",
                "impact": "why it matters",
                "next_check": "cheapest check",
                "critical": False,
            }
        ],
        "specialist_requests": []
        if temporary_specialist
        else [
            {
                "profile": "one allowed profile",
                "unresolved_question": "exactly match one unknown question",
                "expected_evidence": "specific evidence",
                "rationale": "why core roles are insufficient",
                "stopping_condition": "when this specialist is done",
                "priority": "high",
            }
        ],
        "hypothesis_candidates": [
            {
                "kind": "leading",
                "mechanism": "candidate causal mechanism",
                "predictions": ["prediction"],
                "falsifiers": ["falsifier"],
                "evidence_for_claim_ids": ["local-claim-id"],
                "evidence_against_claim_ids": [],
                "retry_conditions": ["changed condition needed after rejection"],
                "changed_condition": "empty unless a prior rejected idea is now retryable",
            }
        ],
        "information_assessment": {
            "field_families": [
                {
                    "id": "field-family-id",
                    "description": "goal-relevant information family",
                    "classification": "retained",
                    "justification": "exact repository-grounded reason",
                    "evidence_claim_ids": ["local-claim-id"],
                    "goal_relevant": True,
                }
            ],
            "pipeline_stages": [
                {
                    "id": "stage-id",
                    "kind": "raw_source",
                    "description": "one concrete stage",
                    "input_field_family_ids": ["field-family-id"],
                    "output_field_family_ids": ["field-family-id"],
                    "evidence_claim_ids": ["local-claim-id"],
                    "risks": [],
                }
            ],
            "layers": {layer: {"status": "unknown", "evidence_claim_ids": [], "unknowns": []} for layer in state.INFORMATION_LAYERS},
            "loss_boundaries": [
                {
                    "id": "loss-boundary-id",
                    "stage_id": "stage-id",
                    "field_family_ids": ["field-family-id"],
                    "failure_layer": "pipeline_preservation",
                    "description": "exact boundary, or omit this entry when none exists",
                    "critical": False,
                    "evidence_claim_ids": ["local-claim-id"],
                    "discriminating_checks": ["specific check"],
                }
            ],
            "recommended_probes": ["specific probe"],
        }
        if information_required
        else None,
    }
    return "\n\n".join(
        [
            "You are one read-only repo-aware function in a bounded goal-research controller.",
            REPO_ROLE_DESCRIPTIONS[role],
            clean_room_rule,
            "Use DevSpace read/search tools to inspect only goal-relevant repository evidence. "
            "Do not edit files, run shell commands, claim local test execution, or rely on chat memory.",
            "Repository observations require precise relative paths and line or symbol locations. "
            "Keep inference, proposed experiments, local Codex results, and audit results in their "
            "distinct evidence classes. Do not promote inference to fact.",
            "Request a temporary specialist only when one additional bounded read-only repository "
            "inspection can materially resolve the exact unknown now. Do not request a specialist "
            "to run future Codex checks, predict a post-change audit, replace local verification, or "
            "answer a question whose required evidence does not yet exist; keep those as unknowns.",
            "Return exactly one JSON object and no Markdown. For cartographer and clean-room-remapper, "
            "information_assessment must be the complete object shown. For every other role it must "
            "be exactly null; express findings through claims, contradictions, unknowns, and hypothesis "
            "candidates instead. Temporary specialist roles must return specialist_requests as an empty "
            "list. Do not invent IDs outside the supplied goal contract. Validate every enum against "
            "the closed vocabulary before returning.",
            "Closed output vocabulary and semantics:\n"
            + _json_block(role_output_vocabulary(goal)),
            "Frozen goal contract:\n" + _json_block(goal),
            "Frozen source snapshot identity:\n" + _json_block(_public_snapshot(snapshot)),
            "Bounded task context:\n" + _json_block(context),
            "Required output shape:\n" + _json_block(schema),
        ]
    )


def _normalize_hypothesis_candidates(
    raw: Any,
    *,
    local_claim_ids: dict[str, str],
    role: str,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise GoalResearchRoleError("hypothesis_candidates must be a list.")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("hypothesis candidate must be an object.")
        state.reject_unknown_keys(
            item,
            {
                "kind",
                "mechanism",
                "predictions",
                "falsifiers",
                "evidence_for_claim_ids",
                "evidence_against_claim_ids",
                "retry_conditions",
                "changed_condition",
            },
            "hypothesis candidate",
        )
        kind = str(item.get("kind") or "")
        if kind not in state.HYPOTHESIS_KINDS:
            raise GoalResearchRoleError("hypothesis candidate kind is invalid.")
        positive = state.string_list(
            item.get("evidence_for_claim_ids", []), "candidate evidence_for_claim_ids"
        )
        negative = state.string_list(
            item.get("evidence_against_claim_ids", []), "candidate evidence_against_claim_ids"
        )
        if (set(positive) | set(negative)) - set(local_claim_ids):
            raise GoalResearchRoleError("hypothesis candidate references an unknown local claim.")
        result.append(
            {
                "source_role": role,
                "kind": kind,
                "mechanism": state.require_string(item.get("mechanism"), "candidate mechanism"),
                "predictions": state.string_list(
                    item.get("predictions"), "candidate predictions", allow_empty=False
                ),
                "falsifiers": state.string_list(
                    item.get("falsifiers"), "candidate falsifiers", allow_empty=False
                ),
                "evidence_for_claim_ids": [local_claim_ids[item_id] for item_id in positive],
                "evidence_against_claim_ids": [local_claim_ids[item_id] for item_id in negative],
                "retry_conditions": state.string_list(
                    item.get("retry_conditions", []), "candidate retry_conditions"
                ),
                "changed_condition": state.optional_string(
                    item.get("changed_condition"), "candidate changed_condition"
                ),
            }
        )
    return result


def _remap_information_claims(
    assessment: dict[str, Any], local_claim_ids: dict[str, str]
) -> dict[str, Any]:
    result = json.loads(json.dumps(assessment))
    claim_lists: list[list[str]] = []
    for field in result["field_families"]:
        claim_lists.append(field["evidence_claim_ids"])
    for stage in result["pipeline_stages"]:
        claim_lists.append(stage["evidence_claim_ids"])
    for layer in result["layers"].values():
        claim_lists.append(layer["evidence_claim_ids"])
    for boundary in result["loss_boundaries"]:
        claim_lists.append(boundary["evidence_claim_ids"])
    for values in claim_lists:
        if set(values) - set(local_claim_ids):
            raise GoalResearchRoleError("information assessment references an unknown local claim.")
        values[:] = [local_claim_ids[item] for item in values]
    return result


def normalize_grounding_report(
    raw: dict[str, Any],
    *,
    role: str,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    information_required = role in {"cartographer", "clean-room-remapper"}
    allowed = {
        "schema_version",
        "role",
        "run_id",
        "goal_version",
        "iteration_id",
        "source_snapshot_id",
        "summary",
        "claims",
        "contradictions",
        "unknowns",
        "specialist_requests",
        "hypothesis_candidates",
        "information_assessment",
    }
    state.reject_unknown_keys(raw, allowed, f"{role} report")
    expected = {
        "schema_version": state.SCHEMA_VERSION,
        "role": role,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": snapshot_id,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise GoalResearchRoleError(f"{role} report {key} does not match the frozen input.")
    claims, local_ids = state.validate_claims(raw.get("claims"), goal, role=role)
    if not claims:
        raise GoalResearchRoleError(f"{role} must return at least one grounded claim.")
    unknowns = state.validate_unknowns(raw.get("unknowns"), role=role)
    contradictions = state.validate_contradictions(
        raw.get("contradictions"), role=role, local_claim_ids=local_ids
    )
    specialist_requests = state.validate_specialist_requests(
        raw.get("specialist_requests"), goal, role=role
    )
    if role in state.SPECIALIST_CATALOG and specialist_requests:
        raise GoalResearchRoleError("temporary specialists cannot spawn nested specialists.")
    candidates = _normalize_hypothesis_candidates(
        raw.get("hypothesis_candidates"), local_claim_ids=local_ids, role=role
    )
    raw_information = raw.get("information_assessment")
    information: dict[str, Any] | None = None
    if not information_required and raw_information is not None:
        raise GoalResearchRoleError(
            f"{role} must return information_assessment=null; information mapping belongs to "
            "cartographer and clean-room-remapper."
        )
    if raw_information is not None:
        if not isinstance(raw_information, dict):
            raise GoalResearchRoleError("information_assessment must be an object or null.")
        information = _remap_information_claims(
            state.validate_information_assessment(raw_information), local_ids
        )
    if information_required and information is None:
        raise GoalResearchRoleError(f"{role} must return an information assessment.")
    return {
        **expected,
        "summary": state.require_string(raw.get("summary"), f"{role} summary"),
        "claims": claims,
        "contradictions": contradictions,
        "unknowns": unknowns,
        "specialist_requests": specialist_requests,
        "hypothesis_candidates": candidates,
        "information_assessment": information,
    }


def merge_grounding_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise GoalResearchRoleError("grounding requires at least one validated report.")
    claims = [item for report in reports for item in report["claims"]]
    contradictions = [item for report in reports for item in report["contradictions"]]
    unknowns = [item for report in reports for item in report["unknowns"]]
    candidates = [item for report in reports for item in report["hypothesis_candidates"]]
    requests = [item for report in reports for item in report["specialist_requests"]]
    claim_ids = {item["id"] for item in claims}
    if len(claim_ids) != len(claims):
        raise GoalResearchRoleError("grounding reports produced duplicate normalized claim ids.")
    contradiction_ids = {item["id"] for item in contradictions}
    if len(contradiction_ids) != len(contradictions):
        raise GoalResearchRoleError("grounding reports produced duplicate contradiction ids.")
    unknown_ids = {item["id"] for item in unknowns}
    if len(unknown_ids) != len(unknowns):
        raise GoalResearchRoleError("grounding reports produced duplicate unknown ids.")
    return {
        "schema_version": state.SCHEMA_VERSION,
        "claims": claims,
        "contradictions": contradictions,
        "unknowns": unknowns,
        "hypothesis_candidates": candidates,
        "specialist_requests": requests,
        "information_assessments": [
            {"role": report["role"], "assessment": report["information_assessment"]}
            for report in reports
            if report["information_assessment"] is not None
        ],
    }


def select_specialists(
    merged: dict[str, Any], goal: dict[str, Any]
) -> dict[str, Any]:
    unknowns = merged["unknowns"]
    by_question: dict[str, list[dict[str, Any]]] = {}
    for item in unknowns:
        key = re.sub(r"\s+", " ", item["question"].strip().lower())
        by_question.setdefault(key, []).append(item)
    priority_order = {"high": 0, "medium": 1, "low": 2}
    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for request in merged["specialist_requests"]:
        key = re.sub(r"\s+", " ", request["unresolved_question"].strip().lower())
        matches = by_question.get(key, [])
        if len(matches) != 1:
            continue
        unknown = matches[0]
        candidates.append(
            (
                priority_order[request["priority"]],
                request["id"],
                request,
                unknown,
            )
        )
    candidates.sort(key=lambda item: (item[0], item[1]))
    selected: list[dict[str, Any]] = []
    used_profiles: set[str] = set()
    maximum = goal["budgets"]["max_specialists_per_iteration"]
    for _, _, request, unknown in candidates:
        if request["profile"] in used_profiles or len(selected) >= maximum:
            continue
        used_profiles.add(request["profile"])
        selected.append(
            {
                "profile": request["profile"],
                "unknown_id": unknown["id"],
                "unresolved_question": request["unresolved_question"],
                "expected_evidence": request["expected_evidence"],
                "rationale": request["rationale"],
                "stopping_condition": request["stopping_condition"],
            }
        )
    raw = {
        "schema_version": state.SCHEMA_VERSION,
        "selected": selected,
        "omitted_reason": "Core reports contained no uniquely grounded, justified specialist request."
        if not selected
        else "",
    }
    return state.validate_specialist_selection(
        raw, goal, known_unknown_ids={item["id"] for item in unknowns}
    )


def build_hypothesis_portfolio(
    candidates: list[dict[str, Any]],
    goal: dict[str, Any],
    *,
    prior: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        raise GoalResearchRoleError("grounding produced no hypothesis candidates.")
    source_rank = {
        "clean-room-remapper": 0,
        "falsifier": 1,
        "cartographer": 2,
        "verifier": 3,
    }
    by_signature: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        signature = state.hypothesis_signature(candidate)
        current = by_signature.get(signature)
        score = (
            len(candidate["evidence_for_claim_ids"]),
            -source_rank.get(candidate["source_role"], 99),
        )
        if current is None:
            by_signature[signature] = candidate
        else:
            current_score = (
                len(current["evidence_for_claim_ids"]),
                -source_rank.get(current["source_role"], 99),
            )
            if score > current_score:
                by_signature[signature] = candidate
    chosen: list[dict[str, Any]] = []
    for kind in ("leading", "alternative", "null_measurement"):
        options = [item for item in by_signature.values() if item["kind"] == kind]
        options.sort(
            key=lambda item: (
                -len(item["evidence_for_claim_ids"]),
                source_rank.get(item["source_role"], 99),
                state.hypothesis_signature(item),
            )
        )
        if options and len(chosen) < goal["budgets"]["max_active_hypotheses"]:
            selected = options[0]
            signature = state.hypothesis_signature(selected)
            chosen.append(
                {
                    "id": "hypothesis-" + signature[:24],
                    "kind": kind,
                    "mechanism": selected["mechanism"],
                    "predictions": selected["predictions"],
                    "falsifiers": selected["falsifiers"],
                    "evidence_for_claim_ids": selected["evidence_for_claim_ids"],
                    "evidence_against_claim_ids": selected["evidence_against_claim_ids"],
                    "retry_conditions": selected["retry_conditions"],
                    "changed_condition": selected["changed_condition"],
                    "status": "active",
                }
            )
    if not any(item["kind"] == "leading" for item in chosen):
        raise GoalResearchRoleError("grounding did not produce a leading hypothesis.")
    known_claims = {
        claim_id
        for item in candidates
        for claim_id in (
            item["evidence_for_claim_ids"] + item["evidence_against_claim_ids"]
        )
    }
    return state.validate_hypotheses(
        chosen,
        goal,
        prior=prior,
        known_claim_ids=known_claims,
    )


def goal_fidelity_prompt(
    *,
    goal: dict[str, Any],
    durable_context: dict[str, Any],
) -> str:
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "role": "goal-fidelity-steward",
        "clause_trace": [
            {
                "clause_id": clause["id"],
                "acceptance_ids": [
                    item["id"]
                    for item in goal["acceptance_dimensions"]
                    if clause["id"] in item["goal_clause_ids"]
                ],
                "hypothesis_ids": [],
                "packet_ids": [],
                "evidence_ids": [],
                "covered": False,
                "drift_risks": [],
            }
            for clause in goal["clauses"]
        ],
        "proxy_drift": [],
        "blocking_issues": [],
    }
    return "\n\n".join(
        [
            "You are the independent goal-fidelity steward in a bounded controller.",
            "Map every original goal clause to acceptance, active hypotheses, packets, and fresh "
            "evidence supplied below. Detect omission, proxy substitution, regressed qualitative "
            "requirements, or incompatible acceptance rules. Do not rewrite the goal, grant a "
            "waiver, inspect a repository, or invent evidence.",
            "Frozen goal contract:\n" + _json_block(goal),
            "Durable controller context only:\n" + _json_block(durable_context),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_goal_fidelity(
    raw: dict[str, Any],
    goal: dict[str, Any],
    durable_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace = state.validate_fidelity_trace(raw, goal)
    context = durable_context or {}
    allowed_hypotheses = set(context.get("active_hypothesis_ids", []))
    active_packet = str(context.get("active_packet_id") or "")
    allowed_packets = {active_packet} if active_packet else set()
    allowed_evidence = set(context.get("fresh_evidence_ids", []))
    require_fresh_evidence = "fresh_evidence_ids" in context
    for item in trace["clause_trace"]:
        if set(item["hypothesis_ids"]) - allowed_hypotheses:
            raise GoalResearchRoleError("goal-fidelity trace references unknown active hypotheses.")
        if set(item["packet_ids"]) - allowed_packets:
            raise GoalResearchRoleError("goal-fidelity trace references an unknown active packet.")
        if set(item["evidence_ids"]) - allowed_evidence:
            raise GoalResearchRoleError("goal-fidelity trace references unknown fresh evidence.")
        if require_fresh_evidence and item["covered"] and not item["evidence_ids"]:
            raise GoalResearchRoleError(
                "a covered post-change goal clause requires fresh evidence ids."
            )
    return trace


def challenge_prompt(
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    claims: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> str:
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "round": 1,
        "claim_reviews": [
            {
                "claim_id": "claim-id",
                "disposition": "supported",
                "reasoning": "bounded challenge",
                "proposed_check": "specific discriminating check",
            }
        ],
        "hypothesis_reviews": [
            {
                "hypothesis_id": "hypothesis-id",
                "disposition": "retain",
                "reasoning": "bounded challenge",
            }
        ],
        "new_contradictions": [
            {
                "claim_ids": ["claim-id"],
                "description": "conflict",
                "severity": "high",
                "critical": False,
                "resolution_check": "specific check",
            }
        ],
        "preserved_contradiction_ids": ["existing-contradiction-id"],
        "recommended_investigation": {
            "hypothesis_id": "hypothesis-id",
            "kind": "implementation",
            "description": "cheapest discriminating next step",
            "relative_cost": "low",
            "rationale": "why this separates the explanations",
        },
    }
    bounded_claims = [
        {
            "id": item["id"],
            "statement": item["statement"],
            "evidence_class": item["evidence_class"],
            "status": item["status"],
            "basis": item["basis"],
            "repository_locations": item["repository_locations"],
        }
        for item in claims
    ]
    return "\n\n".join(
        [
            "Run exactly one coordinator-mediated challenge round. Use only supplied artifacts; "
            "do not inspect the repository, create new roles, or continue a debate.",
            "Challenge explicit claim IDs, preserve unresolved contradictions, compare the active "
            "explanations, and choose the cheapest investigation that discriminates between them.",
            "Goal:\n" + _json_block(goal),
            "Claims:\n" + _json_block(bounded_claims),
            "Existing contradictions:\n" + _json_block(contradictions),
            "Active hypotheses:\n" + _json_block(hypotheses),
            "Closed output vocabulary:\n"
            + _json_block(
                {
                    "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
                    "claim_reviews[].disposition": sorted(CHALLENGE_CLAIM_DISPOSITIONS),
                    "hypothesis_reviews[].disposition": sorted(
                        CHALLENGE_HYPOTHESIS_DISPOSITIONS
                    ),
                    "new_contradictions[].severity": sorted(state.SEVERITIES),
                    "recommended_investigation.kind": sorted(INVESTIGATION_KINDS),
                    "recommended_investigation.relative_cost": sorted(RELATIVE_COSTS),
                }
            ),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_challenge(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    claims: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> dict[str, Any]:
    state.reject_unknown_keys(
        raw,
        {
            "schema_version",
            "run_id",
            "goal_version",
            "iteration_id",
            "round",
            "claim_reviews",
            "hypothesis_reviews",
            "new_contradictions",
            "preserved_contradiction_ids",
            "recommended_investigation",
        },
        "challenge",
    )
    bindings = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "round": 1,
    }
    for key, value in bindings.items():
        if raw.get(key) != value:
            raise GoalResearchRoleError(f"challenge {key} does not match its bounded input.")
    claim_ids = {item["id"] for item in claims}
    hypothesis_ids = {item["id"] for item in hypotheses}
    contradiction_ids = {item["id"] for item in contradictions}
    reviews_raw = raw.get("claim_reviews")
    if not isinstance(reviews_raw, list) or not reviews_raw:
        raise GoalResearchRoleError("challenge requires claim_reviews.")
    reviews: list[dict[str, Any]] = []
    for item in reviews_raw:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("challenge claim review must be an object.")
        state.reject_unknown_keys(
            item, {"claim_id", "disposition", "reasoning", "proposed_check"}, "claim review"
        )
        claim_id = state.validate_identifier(item.get("claim_id"), "challenge claim_id")
        disposition = str(item.get("disposition") or "")
        if claim_id not in claim_ids or disposition not in CHALLENGE_CLAIM_DISPOSITIONS:
            raise GoalResearchRoleError("challenge claim review is invalid.")
        reviews.append(
            {
                "claim_id": claim_id,
                "disposition": disposition,
                "reasoning": state.require_string(item.get("reasoning"), "claim review reasoning"),
                "proposed_check": state.require_string(
                    item.get("proposed_check"), "claim review proposed_check"
                ),
            }
        )
    if len({item["claim_id"] for item in reviews}) != len(reviews):
        raise GoalResearchRoleError("challenge reviewed a claim more than once.")
    hypothesis_reviews_raw = raw.get("hypothesis_reviews")
    if not isinstance(hypothesis_reviews_raw, list) or not hypothesis_reviews_raw:
        raise GoalResearchRoleError("challenge requires hypothesis_reviews.")
    hypothesis_reviews: list[dict[str, Any]] = []
    for item in hypothesis_reviews_raw:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("hypothesis review must be an object.")
        state.reject_unknown_keys(item, {"hypothesis_id", "disposition", "reasoning"}, "hypothesis review")
        identifier = state.validate_identifier(item.get("hypothesis_id"), "challenge hypothesis_id")
        disposition = str(item.get("disposition") or "")
        if identifier not in hypothesis_ids or disposition not in CHALLENGE_HYPOTHESIS_DISPOSITIONS:
            raise GoalResearchRoleError("challenge hypothesis review is invalid.")
        hypothesis_reviews.append(
            {
                "hypothesis_id": identifier,
                "disposition": disposition,
                "reasoning": state.require_string(item.get("reasoning"), "hypothesis review reasoning"),
            }
        )
    if {item["hypothesis_id"] for item in hypothesis_reviews} != hypothesis_ids:
        raise GoalResearchRoleError("challenge must review every active hypothesis exactly once.")
    new_contradictions = state.validate_contradictions(
        raw.get("new_contradictions"),
        role="challenge",
        local_claim_ids={},
        known_claim_ids=claim_ids,
    )
    preserved = state.string_list(
        raw.get("preserved_contradiction_ids", []), "preserved_contradiction_ids"
    )
    if set(preserved) - contradiction_ids:
        raise GoalResearchRoleError("challenge references an unknown existing contradiction.")
    investigation = raw.get("recommended_investigation")
    if not isinstance(investigation, dict):
        raise GoalResearchRoleError("challenge requires recommended_investigation.")
    state.reject_unknown_keys(
        investigation,
        {"hypothesis_id", "kind", "description", "relative_cost", "rationale"},
        "recommended investigation",
    )
    hypothesis_id = state.validate_identifier(
        investigation.get("hypothesis_id"), "investigation hypothesis_id"
    )
    kind = str(investigation.get("kind") or "")
    cost = str(investigation.get("relative_cost") or "")
    if hypothesis_id not in hypothesis_ids or kind not in INVESTIGATION_KINDS:
        raise GoalResearchRoleError("recommended investigation is invalid.")
    if cost not in RELATIVE_COSTS:
        raise GoalResearchRoleError("recommended investigation relative_cost is invalid.")
    return {
        **bindings,
        "claim_reviews": reviews,
        "hypothesis_reviews": hypothesis_reviews,
        "new_contradictions": new_contradictions,
        "preserved_contradiction_ids": sorted(set(preserved)),
        "recommended_investigation": {
            "hypothesis_id": hypothesis_id,
            "kind": kind,
            "description": state.require_string(
                investigation.get("description"), "investigation description"
            ),
            "relative_cost": cost,
            "rationale": state.require_string(investigation.get("rationale"), "investigation rationale"),
        },
    }


def synthesis_prompt(
    *,
    packet_id: str,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    baseline_snapshot_id: str,
    claims: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    challenge: dict[str, Any],
) -> str:
    investigation = challenge["recommended_investigation"]
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "decision": "issue_packet",
        "decision_reason": "why one bounded packet is justified",
        "preserved_contradiction_ids": [item["id"] for item in contradictions],
        "blocking_reasons": [],
        "packet": {
            "schema_version": state.SCHEMA_VERSION,
            "packet_id": packet_id,
            "run_id": run_id,
            "goal_version": goal["version"],
            "iteration_id": iteration_id,
            "baseline_snapshot_id": baseline_snapshot_id,
            "hypothesis_id": investigation["hypothesis_id"],
            "objective": investigation["description"],
            "rationale": investigation["rationale"],
            "permitted_scope": goal["allowed_scope"],
            "forbidden_scope": [],
            "evidence_claim_ids": [item["id"] for item in claims[:1]],
            "required_checks": ["one concrete local check"],
            "expected_signals": ["signal that supports the hypothesis"],
            "rejection_criteria": ["signal that rejects or leaves it inconclusive"],
            "rollback_guidance": "Codex and the user decide how to handle rejected changes; never auto-revert.",
            "open_contradiction_ids": [item["id"] for item in contradictions],
        },
    }
    return "\n\n".join(
        [
            "You are the bounded goal-research synthesizer. Use only validated supplied artifacts. "
            "Do not inspect the repository, add facts, hide disagreement, expand scope, or emit more "
            "than one implementation packet.",
            "Prefer the challenge's cheapest discriminating investigation. A packet may be a bounded "
            "instrumentation or experiment change, not only a feature. Return decision='block' with "
            "packet=null only when no safe evidence-based packet exists. Branch contract: when "
            "decision='issue_packet', packet must be one complete object and blocking_reasons must be "
            "exactly []; current evidence gaps that the packet is designed to resolve belong in its "
            "objective, required_checks, rejection_criteria, and open contradictions, not in "
            "blocking_reasons. When decision='block', packet must be null and blocking_reasons must "
            "contain at least one reason no safe packet can be issued.",
            "Frozen goal:\n" + _json_block(goal),
            "Validated claims:\n" + _json_block(claims),
            "Open contradictions:\n" + _json_block(contradictions),
            "Active hypotheses:\n" + _json_block(hypotheses),
            "Single challenge round:\n" + _json_block(challenge),
            "Closed output vocabulary:\n"
            + _json_block(
                {
                    "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
                    "decision": sorted(SYNTHESIS_DECISIONS),
                    "branch_contract": {
                        "issue_packet": {"packet": "one object", "blocking_reasons": []},
                        "block": {
                            "packet": None,
                            "blocking_reasons": ["at least one terminal reason"],
                        },
                    },
                }
            ),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_synthesis(
    raw: dict[str, Any],
    *,
    packet_id: str,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    baseline_snapshot_id: str,
    claims: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> dict[str, Any]:
    state.reject_unknown_keys(
        raw,
        {
            "schema_version",
            "decision",
            "decision_reason",
            "preserved_contradiction_ids",
            "blocking_reasons",
            "packet",
        },
        "synthesis",
    )
    if raw.get("schema_version") != state.SCHEMA_VERSION:
        raise GoalResearchRoleError("synthesis schema_version is invalid.")
    decision = str(raw.get("decision") or "")
    if decision not in SYNTHESIS_DECISIONS:
        raise GoalResearchRoleError("synthesis decision is invalid.")
    open_ids = {item["id"] for item in contradictions if item.get("status") == "open"}
    preserved = set(
        state.string_list(
            raw.get("preserved_contradiction_ids", []), "synthesis preserved_contradiction_ids"
        )
    )
    if preserved != open_ids:
        raise GoalResearchRoleError("synthesis must preserve every open contradiction exactly.")
    blocking = state.string_list(raw.get("blocking_reasons", []), "synthesis blocking_reasons")
    packet_raw = raw.get("packet")
    if decision == "block":
        if packet_raw is not None or not blocking:
            raise GoalResearchRoleError("blocking synthesis requires packet=null and blocking_reasons.")
        packet = None
    else:
        if not isinstance(packet_raw, dict) or blocking:
            raise GoalResearchRoleError("packet synthesis requires one packet and no blocking_reasons.")
        if packet_raw.get("packet_id") != packet_id:
            raise GoalResearchRoleError("synthesis changed the controller-assigned packet id.")
        packet = state.validate_implementation_packet(
            packet_raw,
            run_id=run_id,
            goal=goal,
            iteration_id=iteration_id,
            baseline_snapshot_id=baseline_snapshot_id,
            hypotheses=hypotheses,
            known_claim_ids={item["id"] for item in claims},
            open_contradiction_ids=open_ids,
        )
    return {
        "schema_version": state.SCHEMA_VERSION,
        "decision": decision,
        "decision_reason": state.require_string(raw.get("decision_reason"), "synthesis decision_reason"),
        "preserved_contradiction_ids": sorted(preserved),
        "blocking_reasons": blocking,
        "packet": packet,
    }


def post_audit_prompt(
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    resulting_snapshot: dict[str, Any],
    packet: dict[str, Any],
    codex_receipt: dict[str, Any],
    verification_receipt: dict[str, Any],
    open_contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> str:
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "role": "post-change-auditor",
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": resulting_snapshot["snapshot_id"],
        "summary": "independent result",
        "outcome": "inconclusive",
        "claims": [
            {
                "id": "local-post-claim",
                "statement": "fresh repository observation",
                "evidence_class": "independent_audit_result",
                "status": "supported",
                "severity": "medium",
                "confidence": 0.8,
                "basis": "what the fresh audit establishes",
                "repository_locations": [
                    {"path": "relative/path.py", "line": 1, "symbol": "optional"}
                ],
                "goal_clause_ids": [goal["clauses"][0]["id"]],
                "acceptance_ids": [goal["acceptance_dimensions"][0]["id"]],
            }
        ],
        "contradictions": [],
        "unknowns": [],
        "acceptance_updates": [
            {
                "id": item["id"],
                "status": "unknown",
                "evidence_classes": ["independent_audit_result"],
                "evidence": "what the fresh audit establishes",
            }
            for item in goal["acceptance_dimensions"]
        ],
        "goal_clause_updates": [
            {"id": item["id"], "status": "unknown", "evidence": "fresh audit evidence"}
            for item in goal["clauses"]
        ],
        "existing_contradiction_updates": [
            {
                "id": item["id"],
                "status": "open",
                "evidence": "fresh reason this remains open, or exact resolution evidence",
                "evidence_claim_ids": ["local-post-claim"],
            }
            for item in open_contradictions
        ],
        "hypothesis_updates": [
            {"id": item["id"], "status": "inconclusive", "evidence": "iteration evidence"}
            for item in hypotheses
        ],
    }
    return "\n\n".join(
        [
            "You are a fresh read-only post-change auditor. Inspect the current sanitized "
            "repository snapshot through DevSpace. Judge actual implementation and local evidence, "
            "not the prose plan. Do not run commands, edit, or silently resolve contradictions.",
            REPO_ROLE_DESCRIPTIONS["post-change-auditor"],
            "Outcome must be exactly one of accepted, rejected, inconclusive, or escalated. "
            "Passing existing tests alone is not enough. Cover every supplied existing contradiction "
            "exactly once in existing_contradiction_updates. Use status='resolved' only when fresh "
            "claims in this response directly establish resolution; otherwise keep status='open'. "
            "Outcome='accepted' is valid only when every required acceptance gate passes, every "
            "critical goal clause is supported, and no critical contradiction remains open. Return "
            "exactly one JSON object.",
            "Frozen goal:\n" + _json_block(goal),
            "Resulting snapshot identity:\n" + _json_block(_public_snapshot(resulting_snapshot)),
            "Active packet:\n" + _json_block(packet),
            "Codex implementation receipt:\n" + _json_block(codex_receipt),
            "Local verification receipt:\n" + _json_block(verification_receipt),
            "Open contradictions:\n" + _json_block(open_contradictions),
            "Active hypotheses:\n" + _json_block(hypotheses),
            "Closed output vocabulary:\n"
            + _json_block(
                {
                    "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
                    "outcome": sorted(state.ITERATION_OUTCOMES),
                    "claims[].evidence_class": sorted(state.EVIDENCE_CLASSES),
                    "claims[].status": sorted(state.CLAIM_STATUSES),
                    "claims[].severity": sorted(state.SEVERITIES),
                    "contradictions[].severity": sorted(state.SEVERITIES),
                    "acceptance_updates[].status": sorted(state.ACCEPTANCE_STATUSES),
                    "acceptance_updates[].evidence_classes[]": sorted(
                        state.EVIDENCE_CLASSES
                    ),
                    "goal_clause_updates[].status": sorted(state.CLAUSE_STATUSES),
                    "existing_contradiction_updates[].status": ["open", "resolved"],
                    "hypothesis_updates[].status": sorted(POST_HYPOTHESIS_STATUSES),
                }
            ),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_post_audit(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    resulting_snapshot_id: str,
    existing_contradictions: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
) -> dict[str, Any]:
    state.reject_unknown_keys(
        raw,
        {
            "schema_version",
            "role",
            "run_id",
            "goal_version",
            "iteration_id",
            "source_snapshot_id",
            "summary",
            "outcome",
            "claims",
            "contradictions",
            "unknowns",
            "acceptance_updates",
            "goal_clause_updates",
            "existing_contradiction_updates",
            "hypothesis_updates",
        },
        "post-change audit",
    )
    bindings = {
        "schema_version": state.SCHEMA_VERSION,
        "role": "post-change-auditor",
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": resulting_snapshot_id,
    }
    for key, value in bindings.items():
        if raw.get(key) != value:
            raise GoalResearchRoleError(f"post-change audit {key} does not match the current snapshot.")
    claims, local_ids = state.validate_claims(raw.get("claims"), goal, role="post-change-auditor")
    if not claims:
        raise GoalResearchRoleError("post-change audit requires fresh repository claims.")
    contradictions = state.validate_contradictions(
        raw.get("contradictions"),
        role="post-change-auditor",
        local_claim_ids=local_ids,
    )
    unknowns = state.validate_unknowns(raw.get("unknowns"), role="post-change-auditor")
    updates = state.validate_audit_updates(raw, goal)
    expected_acceptance = {item["id"] for item in goal["acceptance_dimensions"]}
    expected_clauses = {item["id"] for item in goal["clauses"]}
    if {item["id"] for item in updates["acceptance_updates"]} != expected_acceptance:
        raise GoalResearchRoleError("post-change audit must update every acceptance dimension.")
    if {item["id"] for item in updates["goal_clause_updates"]} != expected_clauses:
        raise GoalResearchRoleError("post-change audit must update every goal clause.")
    for item in updates["acceptance_updates"]:
        if "independent_audit_result" not in item["evidence_classes"]:
            raise GoalResearchRoleError("post-change acceptance updates require independent audit evidence.")
    existing_by_id = {item["id"]: item for item in existing_contradictions}
    raw_existing_updates = raw.get("existing_contradiction_updates")
    if not isinstance(raw_existing_updates, list):
        raise GoalResearchRoleError(
            "post-change audit requires existing_contradiction_updates."
        )
    existing_updates: list[dict[str, Any]] = []
    for item in raw_existing_updates:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("existing contradiction update must be an object.")
        state.reject_unknown_keys(
            item,
            {"id", "status", "evidence", "evidence_claim_ids"},
            "existing contradiction update",
        )
        identifier = state.validate_identifier(
            item.get("id"), "existing contradiction update id"
        )
        status_value = str(item.get("status") or "")
        if identifier not in existing_by_id or status_value not in {"open", "resolved"}:
            raise GoalResearchRoleError("existing contradiction update is invalid.")
        evidence_local_ids = state.string_list(
            item.get("evidence_claim_ids", []),
            "existing contradiction evidence_claim_ids",
        )
        if set(evidence_local_ids) - set(local_ids):
            raise GoalResearchRoleError(
                "existing contradiction update references an unknown fresh local claim."
            )
        if status_value == "resolved" and not evidence_local_ids:
            raise GoalResearchRoleError(
                "resolved existing contradiction requires fresh evidence_claim_ids."
            )
        existing_updates.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence": state.require_string(
                    item.get("evidence"), "existing contradiction update evidence"
                ),
                "evidence_claim_ids": [local_ids[value] for value in evidence_local_ids],
            }
        )
    if len({item["id"] for item in existing_updates}) != len(existing_updates):
        raise GoalResearchRoleError("post-change audit updated an existing contradiction twice.")
    if {item["id"] for item in existing_updates} != set(existing_by_id):
        raise GoalResearchRoleError(
            "post-change audit must update every existing contradiction exactly once."
        )
    critical_existing = {
        item["id"]
        for item in existing_updates
        if item["status"] == "open" and existing_by_id[item["id"]]["critical"]
    }
    critical_ids = critical_existing | {
        item["id"] for item in contradictions if item["critical"] and item["status"] == "open"
    }
    outcome = str(raw.get("outcome") or "")
    if outcome not in state.ITERATION_OUTCOMES:
        raise GoalResearchRoleError("post-change audit outcome is invalid.")
    required_acceptance = {
        item["id"] for item in goal["acceptance_dimensions"] if item["required"]
    }
    critical_clauses = {item["id"] for item in goal["clauses"] if item["critical"]}
    acceptance_map = {item["id"]: item for item in updates["acceptance_updates"]}
    clause_map = {item["id"]: item for item in updates["goal_clause_updates"]}
    if outcome == "accepted" and (
        critical_ids
        or any(
            not state.acceptance_status_satisfies_gate(
                goal, item, acceptance_map[item]["status"]
            )
            for item in required_acceptance
        )
        or any(clause_map[item]["status"] != "supported" for item in critical_clauses)
    ):
        raise GoalResearchRoleError("an accepted iteration cannot retain failed gates or critical contradictions.")
    raw_hypotheses = raw.get("hypothesis_updates")
    if not isinstance(raw_hypotheses, list):
        raise GoalResearchRoleError("post-change audit requires hypothesis_updates.")
    active_ids = {item["id"] for item in hypotheses}
    hypothesis_updates: list[dict[str, Any]] = []
    for item in raw_hypotheses:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("hypothesis update must be an object.")
        state.reject_unknown_keys(item, {"id", "status", "evidence"}, "hypothesis update")
        identifier = state.validate_identifier(item.get("id"), "hypothesis update id")
        status_value = str(item.get("status") or "")
        if identifier not in active_ids or status_value not in POST_HYPOTHESIS_STATUSES:
            raise GoalResearchRoleError("hypothesis update is invalid.")
        hypothesis_updates.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence": state.require_string(item.get("evidence"), "hypothesis update evidence"),
            }
        )
    if {item["id"] for item in hypothesis_updates} != active_ids:
        raise GoalResearchRoleError("post-change audit must update every active hypothesis.")
    return {
        **bindings,
        "summary": state.require_string(raw.get("summary"), "post-change audit summary"),
        "outcome": outcome,
        "claims": claims,
        "contradictions": contradictions,
        "unknowns": unknowns,
        **updates,
        "existing_contradiction_updates": existing_updates,
        "critical_contradiction_ids": sorted(critical_ids),
        "hypothesis_updates": hypothesis_updates,
    }


def final_audit_prompt(
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    final_snapshot: dict[str, Any],
    durable_evidence: dict[str, Any],
) -> str:
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "role": "final-blind-auditor",
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": final_snapshot["snapshot_id"],
        "summary": "blind completion judgment",
        "claims": [
            {
                "id": "local-final-claim",
                "statement": "fresh blind repository observation",
                "evidence_class": "independent_audit_result",
                "status": "supported",
                "severity": "medium",
                "confidence": 0.8,
                "basis": "what the blind audit establishes",
                "repository_locations": [
                    {"path": "relative/path.py", "line": 1, "symbol": "optional"}
                ],
                "goal_clause_ids": [goal["clauses"][0]["id"]],
                "acceptance_ids": [goal["acceptance_dimensions"][0]["id"]],
            }
        ],
        "contradictions": [],
        "goal_clause_status": [
            {"id": item["id"], "status": "unknown", "evidence": "fresh evidence"}
            for item in goal["clauses"]
        ],
        "acceptance_status": [
            {"id": item["id"], "status": "unknown", "evidence": "fresh evidence"}
            for item in goal["acceptance_dimensions"]
        ],
        "blocking_findings": [],
        "recommend_completion": False,
    }
    return "\n\n".join(
        [
            "You are the final blind repo-aware auditor. Start from the original goal and current "
            "repository. You are deliberately not given the current synthesis recommendation or "
            "accepted hypothesis label. Inspect with read-only DevSpace tools.",
            "Detect omitted clauses, proxy success, inherited framing errors, unsupported evidence, "
            "and regressions. Do not edit or run commands. Recommend completion only if every "
            "required gate is independently supported. Return exactly one JSON object.",
            "Original frozen goal:\n" + _json_block(goal),
            "Final snapshot identity:\n" + _json_block(_public_snapshot(final_snapshot)),
            "Raw Codex and local-verification receipts, without inherited advisor conclusions:\n"
            + _json_block(durable_evidence),
            "Closed output vocabulary:\n"
            + _json_block(
                {
                    "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
                    "claims[].evidence_class": sorted(state.EVIDENCE_CLASSES),
                    "claims[].status": sorted(state.CLAIM_STATUSES),
                    "claims[].severity": sorted(state.SEVERITIES),
                    "contradictions[].severity": sorted(state.SEVERITIES),
                    "goal_clause_status[].status": sorted(state.CLAUSE_STATUSES),
                    "acceptance_status[].status": sorted(state.ACCEPTANCE_STATUSES),
                }
            ),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_final_audit(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    final_snapshot_id: str,
) -> dict[str, Any]:
    state.reject_unknown_keys(
        raw,
        {
            "schema_version",
            "role",
            "run_id",
            "goal_version",
            "iteration_id",
            "source_snapshot_id",
            "summary",
            "claims",
            "contradictions",
            "goal_clause_status",
            "acceptance_status",
            "blocking_findings",
            "recommend_completion",
        },
        "final audit",
    )
    bindings = {
        "schema_version": state.SCHEMA_VERSION,
        "role": "final-blind-auditor",
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "source_snapshot_id": final_snapshot_id,
    }
    for key, value in bindings.items():
        if raw.get(key) != value:
            raise GoalResearchRoleError(f"final audit {key} does not match the blind input.")
    claims, local_ids = state.validate_claims(raw.get("claims"), goal, role="final-blind-auditor")
    if not claims:
        raise GoalResearchRoleError("final blind audit requires fresh repository claims.")
    contradictions = state.validate_contradictions(
        raw.get("contradictions"), role="final-blind-auditor", local_claim_ids=local_ids
    )
    clause_raw = raw.get("goal_clause_status")
    acceptance_raw = raw.get("acceptance_status")
    update_shape = {
        "goal_clause_updates": clause_raw,
        "acceptance_updates": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "evidence": item.get("evidence"),
                "evidence_classes": ["independent_audit_result"],
            }
            for item in acceptance_raw
        ]
        if isinstance(acceptance_raw, list)
        else acceptance_raw,
        "critical_contradiction_ids": [],
    }
    updates = state.validate_audit_updates(update_shape, goal)
    if {item["id"] for item in updates["goal_clause_updates"]} != {
        item["id"] for item in goal["clauses"]
    }:
        raise GoalResearchRoleError("final audit must cover every goal clause.")
    if {item["id"] for item in updates["acceptance_updates"]} != {
        item["id"] for item in goal["acceptance_dimensions"]
    }:
        raise GoalResearchRoleError("final audit must cover every acceptance dimension.")
    blockers = state.string_list(raw.get("blocking_findings"), "final audit blocking_findings")
    recommend = state.require_bool(raw.get("recommend_completion"), "recommend_completion")
    required_acceptance = {
        item["id"] for item in goal["acceptance_dimensions"] if item["required"]
    }
    critical_clauses = {item["id"] for item in goal["clauses"] if item["critical"]}
    acceptance_map = {item["id"]: item for item in updates["acceptance_updates"]}
    clause_map = {item["id"]: item for item in updates["goal_clause_updates"]}
    critical_contradictions = [item for item in contradictions if item["critical"]]
    gates_pass = (
        not blockers
        and not critical_contradictions
        and all(
            state.acceptance_status_satisfies_gate(
                goal, item, acceptance_map[item]["status"]
            )
            for item in required_acceptance
        )
        and all(clause_map[item]["status"] == "supported" for item in critical_clauses)
    )
    if recommend != gates_pass:
        raise GoalResearchRoleError("final audit completion recommendation contradicts its gates.")
    return {
        **bindings,
        "summary": state.require_string(raw.get("summary"), "final audit summary"),
        "claims": claims,
        "contradictions": contradictions,
        "goal_clause_status": updates["goal_clause_updates"],
        "acceptance_status": updates["acceptance_updates"],
        "blocking_findings": blockers,
        "recommend_completion": recommend,
    }


def epistemic_refresh_prompt(
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    fidelity: dict[str, Any],
    hypotheses: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    post_audit: dict[str, Any],
    iteration_outcomes: list[dict[str, Any]],
) -> str:
    shape = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
        "recommendation": "next_iteration",
        "rationale": "how the evidence changes the problem framing",
        "hypothesis_updates": post_audit["hypothesis_updates"],
        "remaining_contradiction_ids": [item["id"] for item in contradictions],
        "remap_required": True,
        "remap_reasons": ["first pass, repeated failure, drift, or critical contradiction"],
        "next_discriminating_question": "one bounded question, or empty only for final audit",
    }
    return "\n\n".join(
        [
            "You are the epistemic refresh function. Revisit the problem framing after the delivery "
            "result. Use only supplied durable artifacts; do not inspect the repository, create a "
            "packet, or erase contradictions.",
            "Recommend exactly next_iteration, final_audit, or block. An accepted iteration is not "
            "goal completion. Final audit is only a candidate and remains controller-gated. Preserve "
            "every contradiction supplied in Open contradictions; this phase has no new repository "
            "evidence and cannot resolve one. Branch contract: next_iteration requires "
            "remap_required=true, at least one remap reason, and one next_discriminating_question; "
            "final_audit requires remap_required=false, empty remap_reasons, and an empty next question.",
            "Frozen goal:\n" + _json_block(goal),
            "Latest goal-fidelity trace:\n" + _json_block(fidelity),
            "Active hypotheses before delivery:\n" + _json_block(hypotheses),
            "Open contradictions:\n" + _json_block(contradictions),
            "Fresh post-change audit:\n" + _json_block(post_audit),
            "Prior iteration outcomes:\n" + _json_block(iteration_outcomes),
            "Closed output vocabulary:\n"
            + _json_block(
                {
                    "closed_vocabulary_rule": CLOSED_VOCABULARY_RULE,
                    "recommendation": sorted(EPISTEMIC_RECOMMENDATIONS),
                    "hypothesis_updates[].status": sorted(
                        EPISTEMIC_HYPOTHESIS_STATUSES
                    ),
                    "branch_contract": {
                        "next_iteration": {
                            "remap_required": True,
                            "remap_reasons": ["at least one reason"],
                            "next_discriminating_question": "one bounded question",
                        },
                        "final_audit": {
                            "remap_required": False,
                            "remap_reasons": [],
                            "next_discriminating_question": "",
                        },
                    },
                }
            ),
            "Required output shape:\n" + _json_block(shape),
        ]
    )


def normalize_epistemic_refresh(
    raw: dict[str, Any],
    *,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    hypotheses: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    post_audit: dict[str, Any],
) -> dict[str, Any]:
    state.reject_unknown_keys(
        raw,
        {
            "schema_version",
            "run_id",
            "goal_version",
            "iteration_id",
            "recommendation",
            "rationale",
            "hypothesis_updates",
            "remaining_contradiction_ids",
            "remap_required",
            "remap_reasons",
            "next_discriminating_question",
        },
        "epistemic refresh",
    )
    bindings = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": run_id,
        "goal_version": goal["version"],
        "iteration_id": iteration_id,
    }
    for key, value in bindings.items():
        if raw.get(key) != value:
            raise GoalResearchRoleError(f"epistemic refresh {key} is stale.")
    recommendation = str(raw.get("recommendation") or "")
    if recommendation not in EPISTEMIC_RECOMMENDATIONS:
        raise GoalResearchRoleError("epistemic refresh recommendation is invalid.")
    raw_updates = raw.get("hypothesis_updates")
    if not isinstance(raw_updates, list):
        raise GoalResearchRoleError("epistemic refresh requires hypothesis_updates.")
    hypothesis_ids = {item["id"] for item in hypotheses}
    audit_updates = {item["id"]: item for item in post_audit["hypothesis_updates"]}
    updates: list[dict[str, Any]] = []
    for item in raw_updates:
        if not isinstance(item, dict):
            raise GoalResearchRoleError("epistemic hypothesis update must be an object.")
        state.reject_unknown_keys(item, {"id", "status", "evidence"}, "epistemic hypothesis update")
        identifier = state.validate_identifier(item.get("id"), "epistemic hypothesis id")
        status_value = str(item.get("status") or "")
        if identifier not in hypothesis_ids or status_value not in EPISTEMIC_HYPOTHESIS_STATUSES:
            raise GoalResearchRoleError("epistemic hypothesis update is invalid.")
        if audit_updates.get(identifier, {}).get("status") in {"rejected", "supported"} and (
            status_value != audit_updates[identifier]["status"]
        ):
            raise GoalResearchRoleError("epistemic refresh contradicted a decisive fresh audit status.")
        updates.append(
            {
                "id": identifier,
                "status": status_value,
                "evidence": state.require_string(item.get("evidence"), "epistemic update evidence"),
            }
        )
    if {item["id"] for item in updates} != hypothesis_ids:
        raise GoalResearchRoleError("epistemic refresh must preserve every hypothesis record.")
    known_contradictions = {item["id"] for item in contradictions}
    remaining = set(
        state.string_list(raw.get("remaining_contradiction_ids"), "remaining_contradiction_ids")
    )
    if remaining != known_contradictions:
        raise GoalResearchRoleError(
            "epistemic refresh must preserve every open contradiction exactly."
        )
    audit_critical = set(post_audit["critical_contradiction_ids"])
    if not audit_critical.issubset(remaining):
        raise GoalResearchRoleError("epistemic refresh erased a critical post-audit contradiction.")
    remap_required = state.require_bool(raw.get("remap_required"), "remap_required")
    remap_reasons = state.string_list(raw.get("remap_reasons", []), "remap_reasons")
    if remap_required and not remap_reasons:
        raise GoalResearchRoleError("required remap needs at least one reason.")
    next_question = state.optional_string(
        raw.get("next_discriminating_question"), "next_discriminating_question"
    )
    if recommendation == "next_iteration" and not next_question:
        raise GoalResearchRoleError("next iteration requires one discriminating question.")
    if recommendation == "next_iteration" and not remap_required:
        raise GoalResearchRoleError("next iteration requires a clean-room remap.")
    if recommendation == "final_audit" and (
        remap_required or remap_reasons or next_question
    ):
        raise GoalResearchRoleError(
            "final audit recommendation cannot retain remap work or a next question."
        )
    return {
        **bindings,
        "recommendation": recommendation,
        "rationale": state.require_string(raw.get("rationale"), "epistemic refresh rationale"),
        "hypothesis_updates": updates,
        "remaining_contradiction_ids": sorted(remaining),
        "remap_required": remap_required,
        "remap_reasons": remap_reasons,
        "next_discriminating_question": next_question,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalResearchRoleError(f"checkpoint JSON is corrupt or unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise GoalResearchRoleError(f"checkpoint JSON must contain an object: {path.name}")
    return value


def _turn_submission_started(journal_path: Path) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor_agent  # noqa: PLC0415

    return advisor_agent.journal_proves_submission(_read_json(journal_path))


def _safe_response_path(project: Path, role_dir: Path, raw: Any) -> Path:
    path = Path(str(raw or "")).expanduser().resolve()
    private_root = (project / ".codex-advisor").resolve()
    try:
        path.relative_to(private_root)
        path.relative_to(role_dir.resolve())
    except ValueError as exc:
        raise GoalResearchRoleError("advisor role response path escaped its private checkpoint.") from exc
    if not path.is_file():
        raise GoalResearchRoleError("advisor role did not produce a response file.")
    return path


def _metadata_workspace_identity(metadata: dict[str, Any]) -> tuple[str, str]:
    evidence = metadata.get("tool_evidence")
    if not isinstance(evidence, dict):
        raise GoalResearchRoleError("repo-aware role lacks exact-conversation tool evidence.")
    required_zero = (
        "failed_open_workspace_count",
        "wrong_workspace_open_count",
        "inspection_before_open_count",
        "workspace_id_mismatch_count",
        "sensitive_path_attempt_count",
    )
    if evidence.get("open_workspace_count") != 1 or int(evidence.get("inspection_count") or 0) < 1:
        raise GoalResearchRoleError("repo-aware role did not complete one verified workspace inspection.")
    if any(int(evidence.get(key) or 0) != 0 for key in required_zero):
        raise GoalResearchRoleError("repo-aware role evidence contains an unsafe workspace event.")
    if evidence.get("disallowed"):
        raise GoalResearchRoleError("repo-aware role used a disallowed tool.")
    if metadata.get("chatgpt_attachment_marked") is not True:
        raise GoalResearchRoleError("repo-aware role did not preserve verified ChatGPT attachment state.")
    agent_mode = metadata.get("agent_mode")
    sanitized = agent_mode.get("sanitized_workspace") if isinstance(agent_mode, dict) else None
    if not isinstance(sanitized, dict) or sanitized.get("used") is not True:
        raise GoalResearchRoleError("goal-research requires a generated sanitized advisor workspace.")
    generation = str(sanitized.get("generation_id") or "")
    fingerprint = str(sanitized.get("source_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", generation) or not re.fullmatch(
        r"[0-9a-f]{64}", fingerprint
    ):
        raise GoalResearchRoleError("repo-aware role returned an invalid workspace identity.")
    return generation, fingerprint


def _role_checkpoint(role_dir: Path, role: str, prompt: str) -> tuple[str, bool]:
    safety.ensure_private_dir(role_dir)
    path = role_dir / "goal-role-checkpoint.json"
    input_sha = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    existing = _read_json(path)
    if existing:
        if existing.get("role") != role or existing.get("input_sha256") != input_sha:
            raise GoalResearchRoleError("role checkpoint input changed after it was frozen.")
        marker = str(existing.get("recovery_token") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,160}", marker):
            raise GoalResearchRoleError("role checkpoint has an invalid recovery token.")
        return marker, True
    marker = f"GOAL-RESEARCH-{role.upper()}-{uuid.uuid4().hex.upper()}-COMPLETE"
    safety.atomic_write_json(
        path,
        {
            "schema_version": state.SCHEMA_VERSION,
            "role": role,
            "input_sha256": input_sha,
            "recovery_token": marker,
        },
        sort_keys=True,
    )
    return marker, False


def _agent_command(
    *,
    project: Path,
    role: str,
    role_dir: Path,
    marker: str,
    base_url: str,
    timeout: int,
    queue_timeout: float,
    max_output_tokens: int,
    conversation_key: str | None,
    resume: bool,
    live_activity: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor_agent.py")),
        "--project-dir",
        str(project),
        "--provider",
        "openai-compatible",
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
        "--queue-timeout",
        str(queue_timeout),
        "--json",
    ]
    if conversation_key:
        command.extend(["--conversation-key", conversation_key])
    if resume:
        command.extend(["--resume-run-dir", str(role_dir)])
    else:
        command.extend(
            [
                "--role",
                role,
                "--run-dir",
                str(role_dir),
                "--recovery-token",
                marker,
                "--max-output-tokens",
                str(max_output_tokens),
            ]
        )
    command.append("--live-activity" if live_activity else "--no-live-activity")
    return command


def _invoke_agent_process(
    *,
    command: list[str],
    project: Path,
    prompt: str | None,
    timeout: int,
    queue_timeout: float,
) -> tuple[int, dict[str, Any]]:
    env = os.environ.copy()
    env["ADVISOR_PERSIST_CONVERSATION"] = "true"
    env["ADVISOR_SYNC_REMOTE"] = "true"
    env["ADVISOR_TEMPORARY"] = "false"
    try:
        completed = subprocess.run(
            command,
            cwd=project,
            env=env,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=None,
            timeout=None
            if timeout <= 0 or queue_timeout <= 0
            else timeout + queue_timeout + 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GoalResearchRoleError("repo-aware role exceeded the explicit operator deadline.") from exc
    except OSError as exc:
        raise GoalResearchRoleError(f"could not launch repo-aware role wrapper: {exc}") from exc
    try:
        metadata = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GoalResearchRoleError("repo-aware role wrapper returned invalid metadata JSON.") from exc
    if not isinstance(metadata, dict):
        raise GoalResearchRoleError("repo-aware role wrapper metadata must be an object.")
    return completed.returncode, metadata


def _invoke_repo_agent_process(
    *,
    command: list[str],
    project: Path,
    role_dir: Path,
    prompt: str | None,
    timeout: int,
    queue_timeout: float,
) -> tuple[int, dict[str, Any]]:
    try:
        return _invoke_agent_process(
            command=command,
            project=project,
            prompt=prompt,
            timeout=timeout,
            queue_timeout=queue_timeout,
        )
    except GoalResearchRoleError as exc:
        if _turn_submission_started(role_dir / "turn-journal.json"):
            exc.advisor_turns_attempted = max(exc.advisor_turns_attempted, 1)
        raise


def run_repo_role(
    *,
    project: Path,
    role_dir: Path,
    role: str,
    prompt: str,
    normalize: Callable[[dict[str, Any]], dict[str, Any]],
    base_url: str = "http://127.0.0.1:8080/v1",
    timeout: int = 0,
    queue_timeout: float = 0,
    max_output_tokens: int = 4_000,
    conversation_key: str | None = None,
    live_activity: bool = True,
) -> RoleResult:
    project = project.resolve()
    marker, _ = _role_checkpoint(role_dir, role, prompt)
    existing_meta = _read_json(role_dir / "meta.json")
    resumed = False
    metadata: dict[str, Any]
    returncode: int
    if existing_meta.get("status") == "ok":
        metadata = existing_meta
        returncode = 0
    elif (role_dir / "request.json").is_file():
        returncode, metadata = _invoke_repo_agent_process(
            command=_agent_command(
                project=project,
                role=role,
                role_dir=role_dir,
                marker=marker,
                base_url=base_url,
                timeout=timeout,
                queue_timeout=queue_timeout,
                max_output_tokens=max_output_tokens,
                conversation_key=conversation_key,
                resume=True,
                live_activity=live_activity,
            ),
            project=project,
            role_dir=role_dir,
            prompt=None,
            timeout=timeout,
            queue_timeout=queue_timeout,
        )
        resumed = True
        status_value = str(metadata.get("status") or "")
        if status_value == "not-submitted" and metadata.get("safe_to_submit") is True:
            returncode, metadata = _invoke_repo_agent_process(
                command=_agent_command(
                    project=project,
                    role=role,
                    role_dir=role_dir,
                    marker=marker,
                    base_url=base_url,
                    timeout=timeout,
                    queue_timeout=queue_timeout,
                    max_output_tokens=max_output_tokens,
                    conversation_key=conversation_key,
                    resume=False,
                    live_activity=live_activity,
                ),
                project=project,
                role_dir=role_dir,
                prompt=prompt,
                timeout=timeout,
                queue_timeout=queue_timeout,
            )
        elif status_value == "remote-pending":
            raise GoalResearchPending("repo-aware role is still running remotely; resume this run later.")
    else:
        returncode, metadata = _invoke_repo_agent_process(
            command=_agent_command(
                project=project,
                role=role,
                role_dir=role_dir,
                marker=marker,
                base_url=base_url,
                timeout=timeout,
                queue_timeout=queue_timeout,
                max_output_tokens=max_output_tokens,
                conversation_key=conversation_key,
                resume=False,
                live_activity=live_activity,
            ),
            project=project,
            role_dir=role_dir,
            prompt=prompt,
            timeout=timeout,
            queue_timeout=queue_timeout,
        )
    turn_attempted = 1 if _turn_submission_started(role_dir / "turn-journal.json") else 0
    if returncode != 0 or metadata.get("status") != "ok":
        detail = metadata.get("resume_detail")
        errors = metadata.get("errors")
        if not detail and isinstance(errors, list):
            detail = "; ".join(str(item) for item in errors[:4])
        raise GoalResearchRoleError(
            "repo-aware role failed closed" + (f": {safety.redact_sensitive_text(str(detail))}" if detail else "."),
            advisor_turns_attempted=turn_attempted,
        )
    try:
        response_path = _safe_response_path(project, role_dir, metadata.get("response_path"))
        raw = extract_json_object(
            response_path.read_text(encoding="utf-8", errors="replace"), f"{role} response"
        )
        report = normalize(raw)
        generation, fingerprint = _metadata_workspace_identity(metadata)
    except GoalResearchRoleError as exc:
        exc.advisor_turns_attempted = max(exc.advisor_turns_attempted, turn_attempted)
        raise
    except state.GoalResearchError as exc:
        raise GoalResearchRoleError(
            f"{role} response failed schema validation: {exc}",
            advisor_turns_attempted=turn_attempted,
        ) from exc
    return RoleResult(
        role=role,
        report=report,
        metadata=metadata,
        run_dir=role_dir,
        workspace_generation=generation,
        workspace_fingerprint=fingerprint,
        resumed=resumed or bool(metadata.get("resumed")),
    )


def run_independent_repo_roles(
    *,
    project: Path,
    role_root: Path,
    role_prompts: dict[str, str],
    normalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    base_url: str,
    timeout: int,
    queue_timeout: float,
    max_output_tokens: int,
    conversation_namespace: str | None = None,
    max_workers: int = 3,
    live_activity: bool = True,
) -> list[RoleResult]:
    roles = list(role_prompts)
    if not roles:
        raise GoalResearchRoleError("at least one repo-aware role is required.")
    for role in roles:
        if role not in normalizers:
            raise GoalResearchRoleError(f"missing report normalizer for role: {role}")
        _role_checkpoint(role_root / role, role, role_prompts[role])

    def invoke(role: str) -> RoleResult:
        return run_repo_role(
            project=project,
            role_dir=role_root / role,
            role=role,
            prompt=role_prompts[role],
            normalize=normalizers[role],
            base_url=base_url,
            timeout=timeout,
            queue_timeout=queue_timeout,
            max_output_tokens=max_output_tokens,
            conversation_key=(
                repo_role_conversation_key(conversation_namespace, role)
                if conversation_namespace
                else None
            ),
            live_activity=live_activity,
        )

    results: dict[str, RoleResult] = {}
    failures: list[tuple[str, GoalResearchRoleError]] = []
    pending: list[tuple[str, GoalResearchPending]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(roles))) as executor:
        futures = {executor.submit(invoke, role): role for role in roles}
        for future in concurrent.futures.as_completed(futures):
            role = futures[future]
            try:
                results[role] = future.result()
            except GoalResearchPending as exc:
                pending.append((role, exc))
            except GoalResearchRoleError as exc:
                failures.append((role, exc))
    if pending:
        attempted = len(results) + sum(
            item.advisor_turns_attempted for _, item in [*pending, *failures]
        )
        details = "; ".join(f"{role}: {error}" for role, error in pending)
        raise GoalResearchPending(
            "independent repo-aware roles remain pending: "
            + safety.truncate(safety.redact_sensitive_text(details), 1_000),
            advisor_turns_attempted=attempted,
        )
    if failures:
        submitted_failures = sum(item.advisor_turns_attempted for _, item in failures)
        attempted = len(results) + submitted_failures
        details = "; ".join(f"{role}: {error}" for role, error in failures)
        raise GoalResearchRoleError(
            "independent repo-aware roles failed closed: "
            + safety.truncate(safety.redact_sensitive_text(details), 1_000),
            advisor_turns_attempted=attempted,
        )
    ordered = [results[role] for role in roles]
    generations = {item.workspace_generation for item in ordered}
    fingerprints = {item.workspace_fingerprint for item in ordered}
    if len(generations) != 1 or len(fingerprints) != 1:
        raise GoalResearchRoleError(
            "independent roles inspected different sanitized workspace generations.",
            advisor_turns_attempted=len(ordered),
        )
    return ordered


def _prompt_phase_paths(checkpoint_dir: Path) -> dict[str, Path]:
    return {
        "request": checkpoint_dir / "request.json",
        "response": checkpoint_dir / "response.md",
        "state": checkpoint_dir / "conversation.json",
        "journal": checkpoint_dir / "turn-journal.json",
        "meta": checkpoint_dir / "meta.json",
    }


def _publish_prompt_phase(
    checkpoint_dir: Path,
    *,
    input_sha256: str,
    output: str,
    source: str,
) -> dict[str, Any]:
    paths = _prompt_phase_paths(checkpoint_dir)
    safety.atomic_write_text(paths["response"], output.rstrip() + "\n")
    metadata = {
        "schema_version": state.SCHEMA_VERSION,
        "status": "ok",
        "input_sha256": input_sha256,
        "response_path": str(paths["response"]),
        "response_source": source,
    }
    safety.atomic_write_json(paths["meta"], metadata, sort_keys=True)
    return metadata


def _recover_prompt_phase(
    *,
    project: Path,
    checkpoint_dir: Path,
    input_sha256: str,
    timeout: int,
) -> tuple[str, str]:
    paths = _prompt_phase_paths(checkpoint_dir)
    request = _read_json(paths["request"])
    if not request:
        return "safe-to-submit", "prompt phase was never prepared"
    if request.get("input_sha256") != input_sha256:
        raise GoalResearchRoleError("prompt checkpoint input changed after it was frozen.")
    recorded_project = Path(str(request.get("project_dir") or "")).expanduser().resolve()
    recorded_checkpoint = Path(str(request.get("checkpoint_dir") or "")).expanduser().resolve()
    if recorded_project != project or recorded_checkpoint != checkpoint_dir.resolve():
        raise GoalResearchRoleError("prompt checkpoint path identity is invalid.")
    if any(
        Path(str(request.get(key) or "")).expanduser().resolve() != paths[key]
        for key in ("state", "journal", "response")
    ):
        raise GoalResearchRoleError("prompt checkpoint private paths do not match this run.")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import advisor  # noqa: PLC0415
    import advisor_agent  # noqa: PLC0415

    journal = _read_json(paths["journal"])
    saved_state = _read_json(paths["state"])
    conversation = (
        saved_state.get("conversation")
        if isinstance(saved_state.get("conversation"), dict)
        else {}
    )
    conversation_id = str(conversation.get("conversation_id") or "")
    if not conversation_id and not advisor_agent.journal_proves_submission(journal):
        return "safe-to-submit", "prompt journal proves no submission began"
    prompt = str(request.get("prompt") or "")
    marker = str(request.get("marker") or "")
    if not prompt or marker not in prompt:
        raise GoalResearchRoleError("prompt checkpoint lacks its recovery marker.")
    project_id = str(
        request.get("chatgpt_project_id")
        or saved_state.get("chatgpt_project_id")
        or advisor.chatgpt_project_id(allow_create=False)
        or ""
    )
    if not project_id:
        return "pending", "submitted prompt has no bound ChatGPT Project id"
    if conversation_id:
        remote, error = advisor_agent.fetch_conversation_by_id(conversation_id, timeout)
    else:
        remote, conversation_id, error = advisor_agent.discover_exact_remote_conversation(
            project_id, prompt, timeout
        )
    if error or not remote or not conversation_id:
        return "pending", error or "submitted prompt is not yet discoverable"
    output = advisor_agent.final_text_from_conversation_data(remote, prompt)
    if not output:
        return "pending", "submitted prompt has not produced a final response"
    if marker not in output:
        raise GoalResearchRoleError("recovered prompt final omitted its completion marker.")
    output = advisor_agent.strip_completion_marker(output, marker)
    advisor_agent.persist_recovered_conversation(
        state_path=paths["state"],
        project_id=project_id,
        conversation_id=conversation_id,
        data=remote,
    )
    _publish_prompt_phase(
        checkpoint_dir,
        input_sha256=input_sha256,
        output=output,
        source="interrupted-run-remote-recovery",
    )
    return "ok", output


def _prompt_command(
    *,
    timeout: int,
    response_path: Path,
    live_activity: bool,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve().with_name("advisor.py")),
        "--provider",
        "openai-compatible",
        "--timeout",
        str(timeout),
        "--save",
        str(response_path),
        "--live-activity" if live_activity else "--no-live-activity",
    ]


def run_prompt_phase(
    *,
    project: Path,
    checkpoint_dir: Path,
    prompt: str,
    normalize: Callable[[dict[str, Any]], dict[str, Any]],
    base_url: str = "http://127.0.0.1:8080/v1",
    timeout: int = 0,
    queue_timeout: float = 0,
    max_output_tokens: int = 4_000,
    live_activity: bool = True,
) -> dict[str, Any]:
    project = project.resolve()
    checkpoint_dir = checkpoint_dir.resolve()
    private_root = (project / ".codex-advisor").resolve()
    try:
        checkpoint_dir.relative_to(private_root)
    except ValueError as exc:
        raise GoalResearchRoleError("prompt checkpoint must stay under project .codex-advisor.") from exc
    safety.ensure_private_dir(checkpoint_dir)
    input_sha = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    paths = _prompt_phase_paths(checkpoint_dir)
    existing = _read_json(paths["meta"])
    output = ""
    turn_attempted = 0
    if existing.get("status") == "ok":
        if existing.get("input_sha256") != input_sha:
            raise GoalResearchRoleError("completed prompt checkpoint input changed.")
        try:
            output = paths["response"].read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            raise GoalResearchRoleError(
                "completed prompt checkpoint lacks its immutable response.",
                advisor_turns_attempted=1,
            ) from exc
    elif paths["request"].is_file():
        recovery_status, recovery_value = _recover_prompt_phase(
            project=project,
            checkpoint_dir=checkpoint_dir,
            input_sha256=input_sha,
            timeout=timeout,
        )
        if recovery_status == "ok":
            output = recovery_value
            turn_attempted = 1
        elif recovery_status == "pending":
            raise GoalResearchPending("prompt-only advisor phase is still running; resume later.")
        elif recovery_status != "safe-to-submit":
            raise GoalResearchRoleError(recovery_value)
    if not output:
        marker = f"GOAL-RESEARCH-PROMPT-{uuid.uuid4().hex.upper()}-COMPLETE"
        submitted_prompt = (
            prompt.rstrip()
            + "\n\nReturn exactly one JSON object, then finish with this exact marker on its own line:\n"
            + marker
        )
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import advisor  # noqa: PLC0415

        request = {
            "schema_version": state.SCHEMA_VERSION,
            "status": "ready-to-submit",
            "project_dir": str(project),
            "checkpoint_dir": str(checkpoint_dir),
            "input_sha256": input_sha,
            "prompt": submitted_prompt,
            "marker": marker,
            "state": str(paths["state"]),
            "journal": str(paths["journal"]),
            "response": str(paths["response"]),
            "chatgpt_project_id": advisor.chatgpt_project_id(allow_create=False) or "",
        }
        safety.atomic_write_json(paths["request"], request, sort_keys=True)
        env = os.environ.copy()
        env["ADVISOR_PROJECT_DIR"] = str(project)
        env["ADVISOR_PROVIDER"] = "openai-compatible"
        env["ADVISOR_BASE_URL"] = base_url
        env["ADVISOR_TIMEOUT"] = str(timeout)
        env["ADVISOR_QUEUE_TIMEOUT"] = str(queue_timeout)
        env["ADVISOR_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
        env["ADVISOR_STATE_PATH"] = str(paths["state"])
        env["ADVISOR_RESPONSE_PATH"] = str(paths["response"])
        env["ADVISOR_TURN_JOURNAL_PATH"] = str(paths["journal"])
        env["ADVISOR_AUTO_CREATE_PROJECT"] = "false"
        env["ADVISOR_PERSIST_CONVERSATION"] = "true"
        env["ADVISOR_SYNC_REMOTE"] = "true"
        env["ADVISOR_TEMPORARY"] = "false"
        try:
            completed = subprocess.run(
                _prompt_command(
                    timeout=timeout,
                    response_path=paths["response"],
                    live_activity=live_activity,
                ),
                cwd=project,
                env=env,
                input=submitted_prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=None,
                timeout=None
                if timeout <= 0 or queue_timeout <= 0
                else timeout + queue_timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GoalResearchRoleError(
                "prompt phase exceeded the explicit operator deadline.",
                advisor_turns_attempted=(1 if _turn_submission_started(paths["journal"]) else 0),
            ) from exc
        except OSError as exc:
            raise GoalResearchRoleError(f"could not launch prompt-only advisor wrapper: {exc}") from exc
        output = completed.stdout.strip()
        turn_attempted = 1 if _turn_submission_started(paths["journal"]) else 0
        if not output and paths["response"].is_file():
            output = paths["response"].read_text(encoding="utf-8", errors="replace").strip()
        if completed.returncode != 0:
            raise GoalResearchRoleError(
                f"prompt-only advisor wrapper exited with status {completed.returncode}; resume before retrying.",
                advisor_turns_attempted=turn_attempted,
            )
        if marker not in output:
            raise GoalResearchRoleError(
                "prompt-only final response omitted its completion marker.",
                advisor_turns_attempted=turn_attempted,
            )
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import advisor_agent  # noqa: PLC0415

        output = advisor_agent.strip_completion_marker(output, marker)
        _publish_prompt_phase(
            checkpoint_dir,
            input_sha256=input_sha,
            output=output,
            source="advisor-transport",
        )
    if not output:
        raise GoalResearchRoleError(
            "prompt-only advisor phase returned no output.",
            advisor_turns_attempted=turn_attempted,
        )
    try:
        raw = extract_json_object(output, "prompt-only advisor response")
        return normalize(raw)
    except GoalResearchRoleError as exc:
        exc.advisor_turns_attempted = max(exc.advisor_turns_attempted, turn_attempted)
        raise
    except state.GoalResearchError as exc:
        raise GoalResearchRoleError(
            f"prompt-only advisor response failed schema validation: {exc}",
            advisor_turns_attempted=turn_attempted,
        ) from exc
