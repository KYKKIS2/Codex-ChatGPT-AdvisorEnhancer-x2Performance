#!/usr/bin/env python3
"""Foreground event-sourced controller for bounded repo-aware goal research."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import advisor_concurrency as concurrency
import advisor_safety as safety
import goal_research_roles as roles
import goal_research_state as state


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PENDING = 3
EXIT_WAITING_CODEX = 4
EXIT_BLOCKED = 5


@dataclass(frozen=True)
class ControllerOptions:
    base_url: str
    timeout: int
    queue_timeout: float
    max_output_tokens: int
    live_activity: bool


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def iteration_rel(projection: dict[str, Any]) -> str:
    return f"iterations/{projection['iteration_number']:04d}"


def phase_attempt_path(
    base: Path,
    name: str,
    *,
    events: list[dict[str, Any]],
    phase: str,
    iteration_id: str,
) -> Path:
    """Preserve failed checkpoints and select a new path after explicit resume."""
    attempt = 1 + sum(
        1
        for event in events
        if event.get("event_type") == "run_resumed"
        and event.get("to_state") == phase
        and event.get("iteration_id") == iteration_id
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("new_attempt") is True
    )
    return base / name if attempt == 1 else base / f"{name}-attempt-{attempt:04d}"


def load_selected_run(
    project: Path, raw_run: str
) -> tuple[Path, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    run_dir = state.resolve_run_dir(project, raw_run)
    run, events, projection = state.load_run(run_dir)
    return run_dir, run, events, projection


def event_artifact(
    run_dir: Path,
    events: list[dict[str, Any]],
    *,
    kind: str | None = None,
    path: str | None = None,
    iteration_id: str | None = None,
    goal_version: int | None = None,
    required: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]] | tuple[None, None]:
    for event in reversed(events):
        if iteration_id is not None and event.get("iteration_id") != iteration_id:
            continue
        if goal_version is not None and event.get("goal_version") != goal_version:
            continue
        artifacts = event.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for descriptor in reversed(artifacts):
            if not isinstance(descriptor, dict):
                continue
            if kind is not None and descriptor.get("kind") != kind:
                continue
            if path is not None and descriptor.get("path") != path:
                continue
            return descriptor, state.verify_artifact(run_dir, descriptor)
    if required:
        selector = path or kind or "requested"
        raise state.GoalResearchError(f"run event history lacks required artifact: {selector}")
    return None, None


def baseline_snapshot(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor, payload = event_artifact(
        run_dir,
        events,
        path=f"{iteration_rel(projection)}/baseline.json",
        iteration_id=projection["iteration_id"],
    )
    if descriptor is None or payload is None:
        raise state.GoalResearchError("required iteration baseline artifact is missing")
    project = Path(state.read_json_object(run_dir / "run.json")["project_dir"])
    return descriptor, state.validate_snapshot(payload, project)


def require_fresh_baseline(
    project: Path,
    run_dir: Path,
    events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    descriptor, baseline = baseline_snapshot(run_dir, events, projection)
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != baseline["snapshot_id"]:
        raise state.GoalResearchError(
            "repository changed after the iteration baseline; amend/restart the iteration before "
            "submitting more advisor turns."
        )
    return descriptor, baseline


def latest_goal_fidelity(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> dict[str, Any]:
    _, payload = event_artifact(
        run_dir,
        events,
        kind="goal-fidelity",
        iteration_id=projection["iteration_id"],
    )
    if payload is None:
        raise state.GoalResearchError("required goal-fidelity artifact is missing")
    return payload


def read_iteration_artifact(
    run_dir: Path,
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    _, payload = event_artifact(
        run_dir, events, kind=kind, iteration_id=projection["iteration_id"]
    )
    if payload is None:
        raise state.GoalResearchError(f"required {kind} artifact is missing")
    return payload


def ensure_turn_budget(
    run: dict[str, Any], projection: dict[str, Any], additional: int
) -> None:
    if projection["advisor_turns_used"] + additional > run["budgets"]["max_advisor_turns"]:
        raise state.GoalResearchError(
            "advisor-turn budget is exhausted; amend the goal or stop explicitly."
        )


def account_terminal_phase_failure(
    exc: roles.GoalResearchRoleError, completed_turns: int
) -> None:
    exc.advisor_turns_attempted += max(0, completed_turns)


def fail_after_advisor_turns(message: str, attempted: int) -> None:
    exc = state.GoalResearchError(message)
    exc.advisor_turns_attempted = attempted
    raise exc


def block_run(
    run_dir: Path,
    projection: dict[str, Any],
    reason: str,
    *,
    artifacts: list[dict[str, Any]] | None = None,
    advisor_turns: int = 0,
) -> dict[str, Any]:
    clean = safety.truncate(safety.redact_sensitive_text(reason), 1_000).strip()
    if not clean:
        clean = "goal-research phase failed closed"
    _, updated = state.append_event(
        run_dir,
        event_type="run_blocked",
        to_state=state.PHASE_BLOCKED,
        actor="controller",
        goal_version=projection["goal_version"],
        iteration_id=projection["iteration_id"],
        artifacts=artifacts,
        budget_effect={"advisor_turns": advisor_turns},
        payload={"reason": clean},
        idempotency_key=(
            f"block-{projection['iteration_id']}-{projection['phase'].lower()}-"
            f"{projection['event_count'] + 1}"
        ),
    )
    return updated


def resume_blocked(run_dir: Path, projection: dict[str, Any]) -> dict[str, Any]:
    restore = str(projection.get("blocked_from_phase") or "")
    if not restore:
        raise state.GoalResearchError("blocked run does not record a resumable prior phase.")
    _, updated = state.append_event(
        run_dir,
        event_type="run_resumed",
        to_state=restore,
        actor="codex",
        goal_version=projection["goal_version"],
        iteration_id=projection["iteration_id"],
        payload={
            "previous_reason": projection["blocked_reason"],
            "new_attempt": True,
        },
        idempotency_key=f"resume-{projection['event_count'] + 1}-{projection['iteration_id']}",
    )
    return updated


def render_status(run_dir: Path, projection: dict[str, Any], *, as_json: bool) -> None:
    payload = state.public_status(projection, run_dir)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"Goal research run: {payload['run_id']}")
    print(f"Phase: {payload['phase']}")
    print(f"Iteration: {payload['iteration_number']} ({payload['iteration_id']})")
    print(
        f"Advisor turns: {payload['advisor_turns_used']} | "
        f"Goal version: {payload['goal_version']}"
    )
    if payload["blocked_reason"]:
        print(f"Blocked: {payload['blocked_reason']}")
    print(f"Next: {payload['next_action']}")
    print(f"Run directory: {payload['run_dir']}")


def read_goal_file(path: Path) -> dict[str, Any]:
    return state.read_json_object(path.expanduser().resolve())


def command_init(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    goal = state.validate_goal_contract(read_goal_file(args.goal_file))
    if args.dry_run:
        payload = {
            "schema_version": state.SCHEMA_VERSION,
            "status": "valid",
            "project_dir": str(project),
            "goal_id": goal["goal_id"],
            "goal_version": goal["version"],
            "advisor_turn_budget": goal["budgets"]["max_advisor_turns"],
        }
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else "Goal contract is valid.")
        return EXIT_OK
    run_dir, projection = state.create_run(project, goal, requested_run_id=args.run_id)
    render_status(run_dir, projection, as_json=args.json)
    return EXIT_OK


def command_status(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir, _, _, projection = load_selected_run(project, args.run_dir)
    render_status(run_dir, projection, as_json=args.json)
    return EXIT_OK


def command_amend(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir, _, _, _ = load_selected_run(project, args.run_dir)
    projection = state.amend_goal(run_dir, read_goal_file(args.goal_file))
    render_status(run_dir, projection, as_json=args.json)
    return EXIT_OK


def phase_goal_fidelity(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 1)
    _, baseline = require_fresh_baseline(project, run_dir, events, projection)
    goal = state.current_goal(run_dir, projection)
    durable_context = {
        "iteration_id": projection["iteration_id"],
        "source_snapshot_id": baseline["snapshot_id"],
        "acceptance_status": projection["acceptance_status"],
        "goal_clause_status": projection["goal_clause_status"],
        "active_hypothesis_ids": projection["current_hypothesis_ids"],
        "active_packet_id": projection["current_packet_id"],
        "critical_contradiction_ids": projection["critical_contradiction_ids"],
        "prior_iteration_outcomes": projection["iteration_outcomes"],
    }
    prompt = roles.goal_fidelity_prompt(goal=goal, durable_context=durable_context)
    try:
        trace = roles.run_prompt_phase(
            project=project,
            checkpoint_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "prompt-checkpoints",
                "goal-fidelity",
                events=events,
                phase=state.PHASE_GOAL_FROZEN,
                iteration_id=projection["iteration_id"],
            ),
            prompt=prompt,
            normalize=lambda raw: roles.normalize_goal_fidelity(
                raw, goal, durable_context
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/goal-fidelity.json",
        "goal-fidelity",
        trace,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="goal_fidelity_recorded",
        to_state=state.PHASE_GOAL_FIDELITY,
        actor="goal-fidelity-steward",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[descriptor],
        snapshot_ids=[baseline["snapshot_id"]],
        budget_effect={"advisor_turns": 1},
        payload={
            "blocking_issue_count": len(trace["blocking_issues"]),
            "proxy_drift_count": len(trace["proxy_drift"]),
        },
        idempotency_key=f"goal-fidelity-v{goal['version']}-{projection['iteration_id']}",
    )
    return updated


def _role_normalizer(
    *,
    role: str,
    run_id: str,
    goal: dict[str, Any],
    iteration_id: str,
    snapshot_id: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda raw: roles.normalize_grounding_report(
        raw,
        role=role,
        run_id=run_id,
        goal=goal,
        iteration_id=iteration_id,
        snapshot_id=snapshot_id,
    )


def _role_artifact(
    run_dir: Path,
    projection: dict[str, Any],
    result: roles.RoleResult,
) -> dict[str, Any]:
    return state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/roles/{result.role}/report.json",
        "role-report",
        {
            "schema_version": state.SCHEMA_VERSION,
            "role": result.role,
            "workspace_generation": result.workspace_generation,
            "workspace_fingerprint": result.workspace_fingerprint,
            "resumed": result.resumed,
            "report": result.report,
        },
    )


def _prior_refresh_question(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> str:
    _, payload = event_artifact(
        run_dir,
        events,
        kind="epistemic-refresh",
        goal_version=projection["goal_version"],
        required=False,
    )
    if not payload or payload.get("iteration_id") == projection["iteration_id"]:
        return ""
    return str(payload.get("next_discriminating_question") or "")


def phase_grounding(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    minimum_roles = [*roles.CORE_ROLES, "clean-room-remapper"]
    ensure_turn_budget(run, projection, len(minimum_roles))
    _, baseline = require_fresh_baseline(project, run_dir, events, projection)
    goal = state.current_goal(run_dir, projection)
    next_question = _prior_refresh_question(run_dir, events, projection)
    prompts: dict[str, str] = {}
    normalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
    for role in minimum_roles:
        prompts[role] = roles.role_prompt(
            role=role,
            run_id=run["run_id"],
            goal=goal,
            iteration_id=projection["iteration_id"],
            snapshot=baseline,
            task_context={"next_discriminating_question": next_question}
            if role != "clean-room-remapper" and next_question
            else {},
            clean_room=role == "clean-room-remapper",
        )
        normalizers[role] = _role_normalizer(
            role=role,
            run_id=run["run_id"],
            goal=goal,
            iteration_id=projection["iteration_id"],
            snapshot_id=baseline["snapshot_id"],
        )
    role_root = phase_attempt_path(
        run_dir / iteration_rel(projection),
        "roles",
        events=events,
        phase=state.PHASE_GOAL_FIDELITY,
        iteration_id=projection["iteration_id"],
    )
    try:
        initial_results = roles.run_independent_repo_roles(
            project=project,
            role_root=role_root,
            role_prompts=prompts,
            normalizers=normalizers,
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            conversation_namespace=f"{run['run_id']}-v{goal['version']}",
            max_workers=len(minimum_roles),
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    initial_merged = roles.merge_grounding_reports([item.report for item in initial_results])
    selection = roles.select_specialists(initial_merged, goal)
    specialist_results: list[roles.RoleResult] = []
    selected = selection["selected"]
    if selected:
        ensure_turn_budget(run, projection, len(minimum_roles) + len(selected))
        specialist_prompts: dict[str, str] = {}
        specialist_normalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        for item in selected:
            role = item["profile"]
            specialist_prompts[role] = roles.role_prompt(
                role=role,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                snapshot=baseline,
                task_context={
                    "unknown_id": item["unknown_id"],
                    "unresolved_question": item["unresolved_question"],
                    "expected_evidence": item["expected_evidence"],
                    "stopping_condition": item["stopping_condition"],
                },
            )
            specialist_normalizers[role] = _role_normalizer(
                role=role,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                snapshot_id=baseline["snapshot_id"],
            )
        try:
            specialist_results = roles.run_independent_repo_roles(
                project=project,
                role_root=role_root / "specialists",
                role_prompts=specialist_prompts,
                normalizers=specialist_normalizers,
                base_url=options.base_url,
                timeout=options.timeout,
                queue_timeout=options.queue_timeout,
                max_output_tokens=options.max_output_tokens,
                conversation_namespace=f"{run['run_id']}-v{goal['version']}",
                max_workers=len(selected),
                live_activity=options.live_activity,
            )
        except roles.GoalResearchRoleError as exc:
            account_terminal_phase_failure(exc, len(initial_results))
            raise
        expected_identity = {
            (item.workspace_generation, item.workspace_fingerprint) for item in initial_results
        }
        specialist_identity = {
            (item.workspace_generation, item.workspace_fingerprint) for item in specialist_results
        }
        if specialist_identity != expected_identity:
            fail_after_advisor_turns(
                "temporary specialists inspected a different sanitized workspace generation.",
                len(minimum_roles) + len(selected),
            )
    all_results = [*initial_results, *specialist_results]
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != baseline["snapshot_id"]:
        fail_after_advisor_turns(
            "repository changed while independent grounding roles were running.",
            len(minimum_roles) + len(selected),
        )
    merged = roles.merge_grounding_reports([item.report for item in all_results])
    role_artifacts = [_role_artifact(run_dir, projection, item) for item in all_results]
    selection_artifact = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/specialist-selection.json",
        "specialist-selection",
        selection,
    )
    grounding_artifact = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/grounding.json",
        "grounding",
        merged,
    )
    clean_room = next(
        item.report for item in initial_results if item.role == "clean-room-remapper"
    )
    clean_room_artifact = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/clean-room-remap.json",
        "clean-room-remap",
        clean_room,
    )
    all_artifacts = [*role_artifacts, selection_artifact, grounding_artifact, clean_room_artifact]
    turn_count = len(all_results)
    _, updated = state.append_event(
        run_dir,
        event_type="grounding_recorded",
        to_state=state.PHASE_CLEAN_ROOM,
        actor="controller",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=all_artifacts,
        snapshot_ids=[baseline["snapshot_id"]],
        budget_effect={"advisor_turns": turn_count},
        payload={
            "role_count": turn_count,
            "specialist_count": len(specialist_results),
            "claim_count": len(merged["claims"]),
            "contradiction_count": len(merged["contradictions"]),
            "workspace_generation": all_results[0].workspace_generation,
            "workspace_fingerprint": all_results[0].workspace_fingerprint,
        },
        idempotency_key=f"grounding-v{goal['version']}-{projection['iteration_id']}",
    )
    return updated


def prior_hypotheses(
    run_dir: Path,
    events: list[dict[str, Any]],
    current_iteration_id: str,
    goal_version: int,
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("goal_version") != goal_version:
            continue
        for descriptor in event.get("artifacts", []):
            if not isinstance(descriptor, dict):
                continue
            if descriptor.get("kind") == "hypotheses" and event.get("iteration_id") != current_iteration_id:
                payload = state.verify_artifact(run_dir, descriptor)
                for item in payload.get("hypotheses", []):
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        records[item["id"]] = dict(item)
            elif descriptor.get("kind") == "epistemic-refresh":
                payload = state.verify_artifact(run_dir, descriptor)
                for update in payload.get("hypothesis_updates", []):
                    if isinstance(update, dict) and update.get("id") in records:
                        records[update["id"]]["status"] = update.get("status")
    return list(records.values())


def phase_hypotheses(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    del options
    _, baseline = require_fresh_baseline(project, run_dir, events, projection)
    goal = state.current_goal(run_dir, projection)
    grounding = read_iteration_artifact(run_dir, events, projection, "grounding")
    hypotheses = roles.build_hypothesis_portfolio(
        grounding["hypothesis_candidates"],
        goal,
        prior=prior_hypotheses(
            run_dir,
            events,
            projection["iteration_id"],
            projection["goal_version"],
        ),
    )
    descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/hypotheses.json",
        "hypotheses",
        {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": run["run_id"],
            "goal_version": goal["version"],
            "iteration_id": projection["iteration_id"],
            "source_snapshot_id": baseline["snapshot_id"],
            "hypotheses": hypotheses,
        },
    )
    _, updated = state.append_event(
        run_dir,
        event_type="hypotheses_recorded",
        to_state=state.PHASE_HYPOTHESES,
        actor="controller",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[descriptor],
        snapshot_ids=[baseline["snapshot_id"]],
        payload={"hypothesis_ids": [item["id"] for item in hypotheses]},
        idempotency_key=f"hypotheses-v{goal['version']}-{projection['iteration_id']}",
    )
    return updated


def _iteration_evidence(
    run_dir: Path,
    events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grounding = read_iteration_artifact(run_dir, events, projection, "grounding")
    hypothesis_artifact = read_iteration_artifact(run_dir, events, projection, "hypotheses")
    return (
        grounding,
        grounding["claims"],
        grounding["contradictions"],
        hypothesis_artifact["hypotheses"],
    )


def phase_challenge(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 1)
    _, baseline = require_fresh_baseline(project, run_dir, events, projection)
    goal = state.current_goal(run_dir, projection)
    _, claims, contradictions, hypotheses = _iteration_evidence(
        run_dir, events, projection
    )
    prompt = roles.challenge_prompt(
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        claims=claims,
        contradictions=contradictions,
        hypotheses=hypotheses,
    )
    try:
        challenge = roles.run_prompt_phase(
            project=project,
            checkpoint_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "prompt-checkpoints",
                "challenge",
                events=events,
                phase=state.PHASE_HYPOTHESES,
                iteration_id=projection["iteration_id"],
            ),
            prompt=prompt,
            normalize=lambda raw: roles.normalize_challenge(
                raw,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                claims=claims,
                contradictions=contradictions,
                hypotheses=hypotheses,
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/challenge.json",
        "challenge",
        challenge,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="challenge_recorded",
        to_state=state.PHASE_CHALLENGE,
        actor="challenge",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[descriptor],
        snapshot_ids=[baseline["snapshot_id"]],
        budget_effect={"advisor_turns": 1},
        payload={
            "round": 1,
            "reviewed_claim_ids": [item["claim_id"] for item in challenge["claim_reviews"]],
        },
        idempotency_key=f"challenge-v{goal['version']}-{projection['iteration_id']}",
    )
    return updated


def _all_open_contradictions(
    grounding: dict[str, Any], challenge: dict[str, Any]
) -> list[dict[str, Any]]:
    combined = [*grounding["contradictions"], *challenge["new_contradictions"]]
    by_id: dict[str, dict[str, Any]] = {}
    for item in combined:
        if item["id"] in by_id and state.canonical_json(by_id[item["id"]]) != state.canonical_json(item):
            raise state.GoalResearchError("contradiction id collision changed content.")
        by_id[item["id"]] = item
    return [by_id[key] for key in sorted(by_id)]


def phase_synthesis(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 1)
    _, baseline = require_fresh_baseline(project, run_dir, events, projection)
    goal = state.current_goal(run_dir, projection)
    grounding, claims, _, hypotheses = _iteration_evidence(run_dir, events, projection)
    challenge = read_iteration_artifact(run_dir, events, projection, "challenge")
    contradictions = _all_open_contradictions(grounding, challenge)
    packet_seed = state.sha256_text(
        state.canonical_json(
            {
                "run_id": run["run_id"],
                "goal_version": goal["version"],
                "iteration_id": projection["iteration_id"],
                "challenge": challenge,
            }
        )
    )
    packet_id = f"packet-{projection['iteration_number']:04d}-{packet_seed[:16]}"
    prompt = roles.synthesis_prompt(
        packet_id=packet_id,
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        baseline_snapshot_id=baseline["snapshot_id"],
        claims=claims,
        contradictions=contradictions,
        hypotheses=hypotheses,
        challenge=challenge,
    )
    try:
        synthesis = roles.run_prompt_phase(
            project=project,
            checkpoint_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "prompt-checkpoints",
                "synthesis",
                events=events,
                phase=state.PHASE_CHALLENGE,
                iteration_id=projection["iteration_id"],
            ),
            prompt=prompt,
            normalize=lambda raw: roles.normalize_synthesis(
                raw,
                packet_id=packet_id,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                baseline_snapshot_id=baseline["snapshot_id"],
                claims=claims,
                contradictions=contradictions,
                hypotheses=hypotheses,
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != baseline["snapshot_id"]:
        fail_after_advisor_turns(
            "repository changed while prompt-only synthesis was running.",
            1,
        )
    synthesis_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/synthesis.json",
        "synthesis",
        synthesis,
    )
    if synthesis["decision"] == "block":
        return block_run(
            run_dir,
            projection,
            "; ".join(synthesis["blocking_reasons"]),
            artifacts=[synthesis_descriptor],
            advisor_turns=1,
        )
    packet = synthesis["packet"]
    packet_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/implementation-packet.json",
        "implementation-packet",
        packet,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="packet_issued",
        to_state=state.PHASE_PACKET_READY,
        actor="synthesizer",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[synthesis_descriptor, packet_descriptor],
        snapshot_ids=[baseline["snapshot_id"]],
        budget_effect={"advisor_turns": 1},
        payload={"packet_id": packet["packet_id"], "hypothesis_id": packet["hypothesis_id"]},
        idempotency_key=f"packet-v{goal['version']}-{projection['iteration_id']}",
    )
    return updated


def phase_packet_ready(
    project: Path,
    run_dir: Path,
    events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> dict[str, Any]:
    require_fresh_baseline(project, run_dir, events, projection)
    _, updated = state.append_event(
        run_dir,
        event_type="codex_wait_started",
        to_state=state.PHASE_WAITING_CODEX,
        actor="controller",
        goal_version=projection["goal_version"],
        iteration_id=projection["iteration_id"],
        payload={"packet_id": projection["current_packet_id"]},
        idempotency_key=f"codex-wait-{projection['iteration_id']}",
    )
    return updated


def active_packet(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> dict[str, Any]:
    packet = read_iteration_artifact(run_dir, events, projection, "implementation-packet")
    if packet.get("packet_id") != projection["current_packet_id"]:
        raise state.GoalResearchError("active packet artifact does not match projected state.")
    return packet


def resulting_snapshot(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> dict[str, Any]:
    payload = read_iteration_artifact(run_dir, events, projection, "resulting-snapshot")
    return state.validate_snapshot(payload, Path(state.read_json_object(run_dir / "run.json")["project_dir"]))


def command_receipt_template(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir, run, events, projection = load_selected_run(project, args.run_dir)
    goal = state.current_goal(run_dir, projection)
    packet = active_packet(run_dir, events, projection)
    if args.kind == "codex":
        if projection["phase"] != state.PHASE_WAITING_CODEX:
            raise state.GoalResearchError("Codex receipt template requires WAITING_FOR_CODEX.")
        _, baseline = baseline_snapshot(run_dir, events, projection)
        current = state.capture_repository_snapshot(project)
        payload = {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": run["run_id"],
            "goal_version": goal["version"],
            "iteration_id": projection["iteration_id"],
            "packet_id": packet["packet_id"],
            "baseline_snapshot_id": baseline["snapshot_id"],
            "resulting_snapshot_id": current["snapshot_id"],
            "summary": "Describe the bounded implementation or investigation.",
            "changed_paths": state.snapshot_delta(baseline, current),
            "commands": [],
            "retained_evidence_paths": [],
        }
    else:
        if projection["phase"] != state.PHASE_WAITING_VERIFICATION:
            raise state.GoalResearchError(
                "verification receipt template requires WAITING_FOR_LOCAL_VERIFICATION."
            )
        current = resulting_snapshot(run_dir, events, projection)
        payload = {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": run["run_id"],
            "goal_version": goal["version"],
            "iteration_id": projection["iteration_id"],
            "packet_id": packet["packet_id"],
            "resulting_snapshot_id": current["snapshot_id"],
            "commands": [
                {
                    "command": "Replace with the exact local verification command.",
                    "exit_code": 0,
                    "duration_seconds": 0,
                    "evidence_path": "",
                }
            ],
            "required_check_results": [
                {
                    "check": check,
                    "status": "failed",
                    "evidence": "Record the exact local evidence for this packet check.",
                }
                for check in packet["required_checks"]
            ],
            "acceptance_results": [
                {
                    "id": item["id"],
                    "status": "unknown",
                    "evidence": "Record what the local evidence establishes.",
                    "evidence_class": "codex_local_result",
                }
                for item in goal["acceptance_dimensions"]
            ],
            "notes": "",
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def command_record_codex(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir, run, events, projection = load_selected_run(project, args.run_dir)
    if projection["phase"] != state.PHASE_WAITING_CODEX:
        raise state.GoalResearchError("record-codex requires WAITING_FOR_CODEX.")
    goal = state.current_goal(run_dir, projection)
    packet = active_packet(run_dir, events, projection)
    _, baseline = baseline_snapshot(run_dir, events, projection)
    current = state.capture_repository_snapshot(project)
    raw = state.read_json_object(args.receipt_file.expanduser().resolve())
    receipt = state.validate_codex_receipt(
        raw,
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        packet=packet,
        baseline=baseline,
        resulting=current,
    )
    prior_receipts = sum(
        1
        for event in events
        if event.get("event_type") == "codex_implementation_recorded"
        and event.get("iteration_id") == projection["iteration_id"]
    )
    attempt = prior_receipts + 1
    snapshot_name = (
        "resulting-snapshot.json"
        if attempt == 1
        else f"resulting-snapshot-attempt-{attempt:04d}.json"
    )
    receipt_name = (
        "codex-receipt.json"
        if attempt == 1
        else f"codex-receipt-attempt-{attempt:04d}.json"
    )
    snapshot_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/{snapshot_name}",
        "resulting-snapshot",
        current,
    )
    receipt_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/{receipt_name}",
        "codex-receipt",
        receipt,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="codex_implementation_recorded",
        to_state=state.PHASE_WAITING_VERIFICATION,
        actor="codex",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[snapshot_descriptor, receipt_descriptor],
        snapshot_ids=list(
            dict.fromkeys([baseline["snapshot_id"], current["snapshot_id"]])
        ),
        payload={
            "packet_id": packet["packet_id"],
            "resulting_snapshot_id": current["snapshot_id"],
            "changed_paths": receipt["changed_paths"],
        },
        idempotency_key=f"codex-receipt-{packet['packet_id']}-attempt-{attempt:04d}",
    )
    render_status(run_dir, updated, as_json=args.json)
    return EXIT_OK


def command_record_verification(args: argparse.Namespace) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir, run, events, projection = load_selected_run(project, args.run_dir)
    if projection["phase"] != state.PHASE_WAITING_VERIFICATION:
        raise state.GoalResearchError(
            "record-verification requires WAITING_FOR_LOCAL_VERIFICATION."
        )
    goal = state.current_goal(run_dir, projection)
    packet = active_packet(run_dir, events, projection)
    resulting = resulting_snapshot(run_dir, events, projection)
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != resulting["snapshot_id"]:
        invalidation_descriptor = state.write_artifact(
            run_dir,
            (
                f"{iteration_rel(projection)}/invalidations/"
                f"implementation-{current['snapshot_id']}.json"
            ),
            "invalidated-snapshot",
            current,
        )
        _, updated = state.append_event(
            run_dir,
            event_type="implementation_receipt_invalidated",
            to_state=state.PHASE_WAITING_CODEX,
            actor="controller",
            goal_version=goal["version"],
            iteration_id=projection["iteration_id"],
            artifacts=[invalidation_descriptor],
            snapshot_ids=[resulting["snapshot_id"], current["snapshot_id"]],
            payload={
                "packet_id": packet["packet_id"],
                "invalidated_snapshot_id": resulting["snapshot_id"],
                "observed_snapshot_id": current["snapshot_id"],
            },
            idempotency_key=(
                f"invalidate-codex-receipt-{packet['packet_id']}-{current['snapshot_id']}"
            ),
        )
        render_status(run_dir, updated, as_json=args.json)
        return EXIT_WAITING_CODEX
    raw = state.read_json_object(args.receipt_file.expanduser().resolve())
    receipt = state.validate_verification_receipt(
        raw,
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        packet=packet,
        resulting_snapshot_id=resulting["snapshot_id"],
    )
    descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/verification-receipt.json",
        "verification-receipt",
        receipt,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="local_verification_recorded",
        to_state=state.PHASE_POST_AUDIT,
        actor="codex",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[descriptor],
        snapshot_ids=[resulting["snapshot_id"]],
        payload={"packet_id": packet["packet_id"], "command_count": len(receipt["commands"])},
        idempotency_key=f"verification-receipt-{packet['packet_id']}",
    )
    render_status(run_dir, updated, as_json=args.json)
    return EXIT_OK


def phase_post_audit(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 1)
    goal = state.current_goal(run_dir, projection)
    packet = active_packet(run_dir, events, projection)
    resulting = resulting_snapshot(run_dir, events, projection)
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != resulting["snapshot_id"]:
        raise state.GoalResearchError("repository changed before the fresh post-change audit.")
    codex_receipt = read_iteration_artifact(run_dir, events, projection, "codex-receipt")
    verification = read_iteration_artifact(run_dir, events, projection, "verification-receipt")
    grounding, _, _, hypotheses = _iteration_evidence(run_dir, events, projection)
    challenge = read_iteration_artifact(run_dir, events, projection, "challenge")
    contradictions = _all_open_contradictions(grounding, challenge)
    prompt = roles.post_audit_prompt(
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        resulting_snapshot=resulting,
        packet=packet,
        codex_receipt=codex_receipt,
        verification_receipt=verification,
        open_contradictions=contradictions,
        hypotheses=hypotheses,
    )
    try:
        role_result = roles.run_repo_role(
            project=project,
            role_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "roles",
                "post-change-auditor",
                events=events,
                phase=state.PHASE_POST_AUDIT,
                iteration_id=projection["iteration_id"],
            ),
            role="post-change-auditor",
            prompt=prompt,
            normalize=lambda raw: roles.normalize_post_audit(
                raw,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                resulting_snapshot_id=resulting["snapshot_id"],
                existing_contradictions=contradictions,
                hypotheses=hypotheses,
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            conversation_key=roles.repo_role_conversation_key(
                f"{run['run_id']}-v{goal['version']}", "post-change-auditor"
            ),
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    after = state.capture_repository_snapshot(project)
    if after["snapshot_id"] != resulting["snapshot_id"]:
        fail_after_advisor_turns(
            "repository changed while the post-change auditor was running.", 1
        )
    role_descriptor = _role_artifact(run_dir, projection, role_result)
    audit_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/post-audit.json",
        "post-audit",
        role_result.report,
    )
    audit = role_result.report
    _, updated = state.append_event(
        run_dir,
        event_type="post_change_audit_recorded",
        to_state=state.PHASE_ITERATION_CLOSED,
        actor="post-change-auditor",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[role_descriptor, audit_descriptor],
        snapshot_ids=[resulting["snapshot_id"]],
        budget_effect={"advisor_turns": 1},
        payload={
            "outcome": audit["outcome"],
            "acceptance_updates": audit["acceptance_updates"],
            "goal_clause_updates": audit["goal_clause_updates"],
            "existing_contradiction_updates": audit[
                "existing_contradiction_updates"
            ],
            "critical_contradiction_ids": audit["critical_contradiction_ids"],
        },
        idempotency_key=f"post-audit-{packet['packet_id']}",
    )
    return updated


def iteration_contradictions(
    run_dir: Path, events: list[dict[str, Any]], projection: dict[str, Any]
) -> list[dict[str, Any]]:
    grounding = read_iteration_artifact(run_dir, events, projection, "grounding")
    challenge = read_iteration_artifact(run_dir, events, projection, "challenge")
    combined = _all_open_contradictions(grounding, challenge)
    _, post = event_artifact(
        run_dir,
        events,
        kind="post-audit",
        iteration_id=projection["iteration_id"],
        required=False,
    )
    if post:
        existing_updates = {
            item["id"]: item
            for item in post.get("existing_contradiction_updates", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        by_id = {item["id"]: item for item in combined}
        for identifier, update in existing_updates.items():
            if identifier not in by_id:
                continue
            if update.get("status") == "resolved":
                del by_id[identifier]
            else:
                by_id[identifier] = {
                    **by_id[identifier],
                    "status": "open",
                    "post_audit_evidence": update.get("evidence", ""),
                    "post_audit_evidence_claim_ids": update.get(
                        "evidence_claim_ids", []
                    ),
                }
        for item in post.get("contradictions", []):
            if isinstance(item, dict):
                by_id[item["id"]] = item
        combined = [by_id[key] for key in sorted(by_id)]
    return combined


def _post_fidelity_context(
    projection: dict[str, Any], post: dict[str, Any], hypotheses: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "iteration_id": projection["iteration_id"],
        "acceptance_status": projection["acceptance_status"],
        "goal_clause_status": projection["goal_clause_status"],
        "active_hypothesis_ids": [item["id"] for item in hypotheses],
        "active_packet_id": projection["current_packet_id"],
        "fresh_evidence_ids": [item["id"] for item in post["claims"]],
        "critical_contradiction_ids": post["critical_contradiction_ids"],
        "iteration_outcome": post["outcome"],
    }


def phase_epistemic_refresh(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 2)
    goal = state.current_goal(run_dir, projection)
    result_snapshot = resulting_snapshot(run_dir, events, projection)
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != result_snapshot["snapshot_id"]:
        raise state.GoalResearchError("repository changed before epistemic refresh.")
    post = read_iteration_artifact(run_dir, events, projection, "post-audit")
    hypothesis_artifact = read_iteration_artifact(run_dir, events, projection, "hypotheses")
    hypotheses = hypothesis_artifact["hypotheses"]
    fidelity_prompt = roles.goal_fidelity_prompt(
        goal=goal,
        durable_context=_post_fidelity_context(projection, post, hypotheses),
    )
    try:
        fidelity = roles.run_prompt_phase(
            project=project,
            checkpoint_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "prompt-checkpoints",
                "post-goal-fidelity",
                events=events,
                phase=state.PHASE_ITERATION_CLOSED,
                iteration_id=projection["iteration_id"],
            ),
            prompt=fidelity_prompt,
            normalize=lambda raw: roles.normalize_goal_fidelity(
                raw,
                goal,
                _post_fidelity_context(projection, post, hypotheses),
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    contradictions = iteration_contradictions(run_dir, events, projection)
    refresh_prompt = roles.epistemic_refresh_prompt(
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        fidelity=fidelity,
        hypotheses=hypotheses,
        contradictions=contradictions,
        post_audit=post,
        iteration_outcomes=projection["iteration_outcomes"],
    )
    try:
        refresh = roles.run_prompt_phase(
            project=project,
            checkpoint_dir=phase_attempt_path(
                run_dir / iteration_rel(projection) / "prompt-checkpoints",
                "epistemic-refresh",
                events=events,
                phase=state.PHASE_ITERATION_CLOSED,
                iteration_id=projection["iteration_id"],
            ),
            prompt=refresh_prompt,
            normalize=lambda raw: roles.normalize_epistemic_refresh(
                raw,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                hypotheses=hypotheses,
                contradictions=contradictions,
                post_audit=post,
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError as exc:
        account_terminal_phase_failure(exc, 1)
        raise
    fidelity_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/goal-fidelity-after.json",
        "goal-fidelity",
        fidelity,
    )
    refresh_descriptor = state.write_artifact(
        run_dir,
        f"{iteration_rel(projection)}/epistemic-refresh.json",
        "epistemic-refresh",
        refresh,
    )
    critical_remaining = sorted(
        set(post["critical_contradiction_ids"])
        & set(refresh["remaining_contradiction_ids"])
    )
    _, updated = state.append_event(
        run_dir,
        event_type="epistemic_refresh_recorded",
        to_state=state.PHASE_EPISTEMIC_REFRESH,
        actor="controller",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[fidelity_descriptor, refresh_descriptor],
        snapshot_ids=[result_snapshot["snapshot_id"]],
        budget_effect={"advisor_turns": 2},
        payload={
            "recommendation": refresh["recommendation"],
            "critical_contradiction_ids": critical_remaining,
            "remap_required": refresh["remap_required"],
        },
        idempotency_key=f"epistemic-refresh-{projection['iteration_id']}",
    )
    return updated


def _candidate_gate_reasons(
    goal: dict[str, Any], projection: dict[str, Any], fidelity: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    acceptance = projection["acceptance_status"]
    clauses = projection["goal_clause_status"]
    for item in goal["acceptance_dimensions"]:
        if item["required"] and not state.acceptance_status_satisfies_gate(
            goal,
            item["id"],
            str(acceptance.get(item["id"], {}).get("status") or ""),
        ):
            reasons.append(f"required acceptance is not passed: {item['id']}")
    for item in goal["clauses"]:
        if item["critical"] and clauses.get(item["id"], {}).get("status") != "supported":
            reasons.append(f"critical goal clause is not supported: {item['id']}")
    if projection["critical_contradiction_ids"]:
        reasons.append("critical contradictions remain")
    if fidelity["blocking_issues"]:
        reasons.append("latest goal-fidelity trace has blocking issues")
    uncovered = [item["clause_id"] for item in fidelity["clause_trace"] if not item["covered"]]
    if uncovered:
        reasons.append("goal-fidelity clauses remain uncovered: " + ", ".join(uncovered))
    return reasons


def phase_refresh_decision(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> dict[str, Any]:
    goal = state.current_goal(run_dir, projection)
    refresh = read_iteration_artifact(run_dir, events, projection, "epistemic-refresh")
    fidelity = latest_goal_fidelity(run_dir, events, projection)
    recommendation = refresh["recommendation"]
    candidate_reasons = _candidate_gate_reasons(goal, projection, fidelity)
    if recommendation == "block":
        return block_run(run_dir, projection, refresh["rationale"])
    if recommendation == "final_audit" and not candidate_reasons:
        ensure_turn_budget(run, projection, 1)
        snapshot = state.capture_repository_snapshot(project)
        result = resulting_snapshot(run_dir, events, projection)
        if snapshot["snapshot_id"] != result["snapshot_id"]:
            raise state.GoalResearchError("repository changed before final clean-room audit.")
        descriptor = state.write_artifact(
            run_dir,
            f"final-audit/snapshot-v{goal['version']}-{projection['iteration_id']}.json",
            "final-audit-snapshot",
            snapshot,
        )
        _, updated = state.append_event(
            run_dir,
            event_type="final_audit_started",
            to_state=state.PHASE_FINAL_AUDIT,
            actor="controller",
            goal_version=goal["version"],
            iteration_id=projection["iteration_id"],
            artifacts=[descriptor],
            snapshot_ids=[snapshot["snapshot_id"]],
            payload={"candidate_gate_reasons": []},
            idempotency_key=f"final-audit-start-{projection['iteration_id']}",
        )
        return updated
    if projection["iterations_started"] >= run["budgets"]["max_iterations"]:
        reason = "iteration budget exhausted"
        if candidate_reasons:
            reason += ": " + "; ".join(candidate_reasons)
        return block_run(run_dir, projection, reason)
    next_number = projection["iteration_number"] + 1
    next_id = f"iteration-{next_number:04d}"
    safety.ensure_private_dir(run_dir / "iterations" / f"{next_number:04d}" / "roles")
    snapshot = state.capture_repository_snapshot(project)
    result = resulting_snapshot(run_dir, events, projection)
    if snapshot["snapshot_id"] != result["snapshot_id"]:
        raise state.GoalResearchError("repository changed before the next iteration baseline.")
    descriptor = state.write_artifact(
        run_dir,
        f"iterations/{next_number:04d}/baseline.json",
        "repository-snapshot",
        snapshot,
    )
    _, updated = state.append_event(
        run_dir,
        event_type="next_iteration_started",
        to_state=state.PHASE_GOAL_FROZEN,
        actor="controller",
        goal_version=goal["version"],
        iteration_id=next_id,
        artifacts=[descriptor],
        snapshot_ids=[snapshot["snapshot_id"]],
        payload={
            "previous_iteration_id": projection["iteration_id"],
            "next_discriminating_question": refresh["next_discriminating_question"],
            "candidate_gate_reasons": candidate_reasons,
        },
        idempotency_key=f"next-{next_id}-v{goal['version']}",
    )
    return updated


def phase_final_audit(
    project: Path,
    run_dir: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    projection: dict[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    ensure_turn_budget(run, projection, 1)
    goal = state.current_goal(run_dir, projection)
    final_snapshot = read_iteration_artifact(
        run_dir, events, projection, "final-audit-snapshot"
    )
    final_snapshot = state.validate_snapshot(final_snapshot, project)
    current = state.capture_repository_snapshot(project)
    if current["snapshot_id"] != final_snapshot["snapshot_id"]:
        raise state.GoalResearchError("repository changed after final-audit snapshot freeze.")
    fidelity = latest_goal_fidelity(run_dir, events, projection)
    durable_evidence = final_blind_local_evidence(
        run_dir,
        events,
        goal_version=goal["version"],
        snapshot_id=final_snapshot["snapshot_id"],
    )
    prompt = roles.final_audit_prompt(
        run_id=run["run_id"],
        goal=goal,
        iteration_id=projection["iteration_id"],
        final_snapshot=final_snapshot,
        durable_evidence=durable_evidence,
    )
    try:
        result = roles.run_repo_role(
            project=project,
            role_dir=phase_attempt_path(
                run_dir / "final-audit",
                f"v{goal['version']}-{projection['iteration_id']}",
                events=events,
                phase=state.PHASE_FINAL_AUDIT,
                iteration_id=projection["iteration_id"],
            ),
            role="final-blind-auditor",
            prompt=prompt,
            normalize=lambda raw: roles.normalize_final_audit(
                raw,
                run_id=run["run_id"],
                goal=goal,
                iteration_id=projection["iteration_id"],
                final_snapshot_id=final_snapshot["snapshot_id"],
            ),
            base_url=options.base_url,
            timeout=options.timeout,
            queue_timeout=options.queue_timeout,
            max_output_tokens=options.max_output_tokens,
            conversation_key=None,
            live_activity=options.live_activity,
        )
    except roles.GoalResearchRoleError:
        raise
    after = state.capture_repository_snapshot(project)
    if after["snapshot_id"] != final_snapshot["snapshot_id"]:
        fail_after_advisor_turns(
            "repository changed while final blind audit was running.", 1
        )
    audit_descriptor = state.write_artifact(
        run_dir,
        f"final-audit/report-v{goal['version']}-{projection['iteration_id']}.json",
        "final-audit",
        result.report,
    )
    ready, reasons = state.completion_ready(goal, projection, result.report)
    reasons.extend(_candidate_gate_reasons(goal, projection, fidelity))
    reasons = sorted(set(reasons))
    if not ready or reasons:
        return block_run(
            run_dir,
            projection,
            "final blind audit rejected completion: " + "; ".join(reasons),
            artifacts=[audit_descriptor],
            advisor_turns=1,
        )
    _, updated = state.append_event(
        run_dir,
        event_type="goal_completed",
        to_state=state.PHASE_COMPLETED,
        actor="controller",
        goal_version=goal["version"],
        iteration_id=projection["iteration_id"],
        artifacts=[audit_descriptor],
        snapshot_ids=[final_snapshot["snapshot_id"]],
        budget_effect={"advisor_turns": 1},
        payload={"final_audit_artifact_id": audit_descriptor["artifact_id"]},
        idempotency_key=f"goal-completed-v{goal['version']}",
    )
    return updated


def final_blind_local_evidence(
    run_dir: Path,
    events: list[dict[str, Any]],
    *,
    goal_version: int | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    codex_receipts: list[dict[str, Any]] = []
    verification_receipts: list[dict[str, Any]] = []
    for event in events:
        if goal_version is not None and event.get("goal_version") != goal_version:
            continue
        for descriptor in event.get("artifacts", []):
            if not isinstance(descriptor, dict):
                continue
            if descriptor.get("kind") == "codex-receipt":
                receipt = state.verify_artifact(run_dir, descriptor)
                if snapshot_id is None or receipt.get("resulting_snapshot_id") == snapshot_id:
                    codex_receipts.append(receipt)
            elif descriptor.get("kind") == "verification-receipt":
                receipt = state.verify_artifact(run_dir, descriptor)
                if snapshot_id is None or receipt.get("resulting_snapshot_id") == snapshot_id:
                    verification_receipts.append(receipt)
    if not codex_receipts or not verification_receipts:
        raise state.GoalResearchError(
            "final blind audit requires raw Codex and local-verification receipts."
        )
    return {
        "codex_receipts": codex_receipts,
        "local_verification_receipts": verification_receipts,
    }


PHASE_HANDLERS: dict[
    str,
    Callable[
        [Path, Path, dict[str, Any], list[dict[str, Any]], dict[str, Any], ControllerOptions],
        dict[str, Any],
    ],
] = {
    state.PHASE_GOAL_FROZEN: phase_goal_fidelity,
    state.PHASE_GOAL_FIDELITY: phase_grounding,
    state.PHASE_CLEAN_ROOM: phase_hypotheses,
    state.PHASE_HYPOTHESES: phase_challenge,
    state.PHASE_CHALLENGE: phase_synthesis,
    state.PHASE_POST_AUDIT: phase_post_audit,
    state.PHASE_ITERATION_CLOSED: phase_epistemic_refresh,
    state.PHASE_FINAL_AUDIT: phase_final_audit,
}


def dry_run_advance(
    run_dir: Path, run: dict[str, Any], projection: dict[str, Any]
) -> dict[str, Any]:
    estimates: dict[str, str | int] = {
        state.PHASE_GOAL_FROZEN: 1,
        state.PHASE_GOAL_FIDELITY: (
            f"4-{4 + run['budgets']['max_specialists_per_iteration']}"
        ),
        state.PHASE_CLEAN_ROOM: 0,
        state.PHASE_HYPOTHESES: 1,
        state.PHASE_CHALLENGE: 1,
        state.PHASE_PACKET_READY: 0,
        state.PHASE_WAITING_CODEX: 0,
        state.PHASE_WAITING_VERIFICATION: 0,
        state.PHASE_POST_AUDIT: 1,
        state.PHASE_ITERATION_CLOSED: 2,
        state.PHASE_EPISTEMIC_REFRESH: 0,
        state.PHASE_FINAL_AUDIT: 1,
        state.PHASE_COMPLETED: 0,
        state.PHASE_BLOCKED: 0,
    }
    return {
        **state.public_status(projection, run_dir),
        "dry_run": True,
        "estimated_advisor_turns_for_next_step": estimates.get(projection["phase"], 0),
        "would_contact_advisor": projection["phase"]
        in {
            state.PHASE_GOAL_FROZEN,
            state.PHASE_GOAL_FIDELITY,
            state.PHASE_HYPOTHESES,
            state.PHASE_CHALLENGE,
            state.PHASE_POST_AUDIT,
            state.PHASE_ITERATION_CLOSED,
            state.PHASE_FINAL_AUDIT,
        },
    }


def controller_options(args: argparse.Namespace) -> ControllerOptions:
    if args.timeout < 0 or args.queue_timeout < 0:
        raise state.GoalResearchError("timeouts cannot be negative; use 0 for no operator deadline.")
    if args.max_output_tokens < 512:
        raise state.GoalResearchError("max-output-tokens must be at least 512.")
    if not concurrency.local_http_url(args.base_url):
        raise state.GoalResearchError(
            "goal-research requires the loopback OpenAI-compatible advisor endpoint."
        )
    return ControllerOptions(
        base_url=args.base_url,
        timeout=args.timeout,
        queue_timeout=args.queue_timeout,
        max_output_tokens=args.max_output_tokens,
        live_activity=not args.no_live_activity,
    )


def command_advance(args: argparse.Namespace, *, resume: bool = False) -> int:
    project = state.require_git_root(args.project_dir)
    run_dir = state.resolve_run_dir(project, args.run_dir)
    if args.dry_run:
        run, _, projection = state.load_run(run_dir)
        payload = dry_run_advance(run_dir, run, projection)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    operation_lock = concurrency.InterProcessLock(
        run_dir / "operation.lock",
        timeout=0,
    )
    if not operation_lock.try_acquire():
        print(
            "Goal research run already has an active mutating command; resume or retry after it exits.",
            file=sys.stderr,
        )
        return EXIT_PENDING
    try:
        return _command_advance_locked(args, project, run_dir, resume=resume)
    finally:
        operation_lock.release()


def _command_advance_locked(
    args: argparse.Namespace,
    project: Path,
    run_dir: Path,
    *,
    resume: bool,
) -> int:
    run, events, projection = state.load_run(run_dir)
    if args.dry_run:
        payload = dry_run_advance(run_dir, run, projection)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    options = controller_options(args)
    if projection["phase"] == state.PHASE_BLOCKED:
        if not resume:
            render_status(run_dir, projection, as_json=args.json)
            return EXIT_BLOCKED
        projection = resume_blocked(run_dir, projection)
        render_status(run_dir, projection, as_json=args.json)
        return EXIT_OK
    if projection["phase"] == state.PHASE_COMPLETED:
        render_status(run_dir, projection, as_json=args.json)
        return EXIT_OK
    if projection["phase"] in {
        state.PHASE_WAITING_CODEX,
        state.PHASE_WAITING_VERIFICATION,
    }:
        render_status(run_dir, projection, as_json=args.json)
        return EXIT_WAITING_CODEX
    try:
        if projection["phase"] == state.PHASE_PACKET_READY:
            updated = phase_packet_ready(project, run_dir, events, projection)
        elif projection["phase"] == state.PHASE_EPISTEMIC_REFRESH:
            updated = phase_refresh_decision(project, run_dir, run, events, projection)
        else:
            handler = PHASE_HANDLERS.get(projection["phase"])
            if handler is None:
                raise state.GoalResearchError(
                    f"no controller handler exists for phase {projection['phase']}."
                )
            updated = handler(project, run_dir, run, events, projection, options)
    except roles.GoalResearchPending:
        raise
    except state.GoalResearchError as exc:
        attempted = getattr(exc, "advisor_turns_attempted", 0)
        updated = block_run(
            run_dir,
            projection,
            str(exc),
            advisor_turns=attempted,
        )
        render_status(run_dir, updated, as_json=args.json)
        return EXIT_BLOCKED
    render_status(run_dir, updated, as_json=args.json)
    if updated["phase"] == state.PHASE_BLOCKED:
        return EXIT_BLOCKED
    return EXIT_OK


def add_project_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Git repository root. Defaults to the current directory.",
    )


def add_run_options(parser: argparse.ArgumentParser) -> None:
    add_project_option(parser)
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Run id or private run directory under .codex-advisor/goal-research-runs.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable status JSON.")


def add_remote_options(parser: argparse.ArgumentParser) -> None:
    add_run_options(parser)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ADVISOR_BASE_URL", "http://127.0.0.1:8080/v1"),
        help="Loopback OpenAI-compatible control endpoint.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("ADVISOR_TIMEOUT", "0")),
        help="Explicit per-turn operator deadline; 0 waits for the final turn.",
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=float(os.environ.get("ADVISOR_QUEUE_TIMEOUT", "0")),
        help="Coordinator queue deadline; 0 waits without polling ChatGPT.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=int(os.environ.get("ADVISOR_MAX_OUTPUT_TOKENS", "6000")),
    )
    parser.add_argument("--no-live-activity", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Describe the next step without writing state or contacting ChatGPT.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a foreground, event-sourced goal-research loop with read-only repo-aware "
            "advisors and Codex-only implementation."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Validate and freeze a goal contract offline.")
    add_project_option(init)
    init.add_argument("--goal-file", type=Path, required=True)
    init.add_argument("--run-id")
    init.add_argument("--json", action="store_true")
    init.add_argument("--dry-run", action="store_true")
    init.set_defaults(handler=command_init)

    status_parser = subparsers.add_parser("status", help="Replay and display authoritative state.")
    add_run_options(status_parser)
    status_parser.set_defaults(handler=command_status)

    amend = subparsers.add_parser("amend-goal", help="Freeze a new goal version and invalidate downstream work.")
    add_run_options(amend)
    amend.add_argument("--goal-file", type=Path, required=True)
    amend.set_defaults(handler=command_amend)

    advance = subparsers.add_parser("advance", help="Perform at most one guarded controller phase.")
    add_remote_options(advance)
    advance.set_defaults(handler=lambda args: command_advance(args, resume=False))

    resume_parser = subparsers.add_parser(
        "resume", help="GET-reconcile checkpoints or restore one visibly blocked phase."
    )
    add_remote_options(resume_parser)
    resume_parser.set_defaults(handler=lambda args: command_advance(args, resume=True))

    template = subparsers.add_parser(
        "receipt-template", help="Print a snapshot-bound Codex or verification receipt template."
    )
    add_run_options(template)
    template.add_argument("--kind", choices=("codex", "verification"), required=True)
    template.set_defaults(handler=command_receipt_template)

    record_codex = subparsers.add_parser(
        "record-codex", help="Record a packet-bound implementation receipt; runs no commands."
    )
    add_run_options(record_codex)
    record_codex.add_argument("--receipt-file", type=Path, required=True)
    record_codex.set_defaults(handler=command_record_codex)

    record_verification = subparsers.add_parser(
        "record-verification", help="Record local verification evidence; runs no commands."
    )
    add_run_options(record_verification)
    record_verification.add_argument("--receipt-file", type=Path, required=True)
    record_verification.set_defaults(handler=command_record_verification)
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except roles.GoalResearchPending as exc:
        print(f"Goal research remote phase pending: {exc}", file=sys.stderr)
        return EXIT_PENDING
    except state.GoalResearchError as exc:
        print(f"Goal research failed closed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Goal research interrupted; use resume with the same run directory.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
