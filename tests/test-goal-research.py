#!/usr/bin/env python3
"""Deterministic offline regressions for the explicit goal-research lane."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "codex-skill" / "external-advisor" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "goal-research"
sys.path.insert(0, str(SCRIPTS))

import goal_research  # noqa: E402
import goal_research_roles as roles  # noqa: E402
import goal_research_state as state  # noqa: E402


def run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
        )


@contextmanager
def fixture_repo(name: str = "positive-lossy-pipeline") -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "repo"
        shutil.copytree(FIXTURES / name, project)
        (project / ".gitignore").write_text(
            ".codex-advisor/\n__pycache__/\n*.pyc\n", encoding="utf-8"
        )
        run(["git", "init", "-q"], project)
        run(["git", "config", "user.email", "tests@example.invalid"], project)
        run(["git", "config", "user.name", "Goal Research Tests"], project)
        run(["git", "add", "."], project)
        run(["git", "commit", "-qm", "fixture"], project)
        yield project


def goal_contract(version: int = 1) -> dict[str, Any]:
    goal = json.loads((FIXTURES / "goal.json").read_text(encoding="utf-8"))
    goal["version"] = version
    return goal


def assert_raises(expected: str, callback: Any) -> None:
    try:
        callback()
    except state.GoalResearchError as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected {expected!r} in {exc!r}") from exc
    else:
        raise AssertionError(f"expected GoalResearchError containing {expected!r}")


def sample_claim(local_id: str = "c1", statement: str = "Identity is dropped.") -> dict[str, Any]:
    return {
        "id": local_id,
        "statement": statement,
        "evidence_class": "repository_observation",
        "status": "supported",
        "severity": "critical",
        "confidence": 0.95,
        "basis": "encode_event returns side, amount, and price but not wallet_id or timestamp_seconds.",
        "repository_locations": [
            {"path": "pipeline.py", "line": 18, "symbol": "encode_event"}
        ],
        "goal_clause_ids": ["preserve-information"],
        "acceptance_ids": ["information-path"],
    }


def information_assessment(
    *,
    claim_id: str = "c1",
    justified: bool = False,
) -> dict[str, Any]:
    if justified:
        fields = [
            {
                "id": "display_label",
                "description": "Presentation-only label",
                "classification": "excluded_with_justification",
                "justification": "Not available at decision time and excluded by the goal.",
                "evidence_claim_ids": [claim_id],
                "goal_relevant": False,
            },
            {
                "id": "device_state",
                "description": "Device identity, time, and value",
                "classification": "retained",
                "justification": "All required fields cross the representation boundary.",
                "evidence_claim_ids": [claim_id],
                "goal_relevant": True,
            },
        ]
        boundaries: list[dict[str, Any]] = []
    else:
        fields = [
            {
                "id": "wallet_identity",
                "description": "Wallet identity and recurrence",
                "classification": "unexplained",
                "justification": "The source field exists but is omitted by encode_event.",
                "evidence_claim_ids": [claim_id],
                "goal_relevant": True,
            },
            {
                "id": "event_time",
                "description": "Event timestamp and relative timing",
                "classification": "unexplained",
                "justification": "The timestamp is omitted before the score boundary.",
                "evidence_claim_ids": [claim_id],
                "goal_relevant": True,
            },
            {
                "id": "event_order",
                "description": "Ordered sequence motifs",
                "classification": "aggregated",
                "justification": "Mean pooling is permutation invariant.",
                "evidence_claim_ids": [claim_id],
                "goal_relevant": True,
            },
        ]
        boundaries = [
            {
                "id": "identity-time-drop",
                "stage_id": "representation",
                "field_family_ids": ["wallet_identity", "event_time"],
                "failure_layer": "pipeline_preservation",
                "description": "encode_event drops wallet_id and timestamp_seconds.",
                "critical": True,
                "evidence_claim_ids": [claim_id],
                "discriminating_checks": ["identity ablation"],
            },
            {
                "id": "order-collapse",
                "stage_id": "aggregation",
                "field_family_ids": ["event_order"],
                "failure_layer": "representation_distinguishability",
                "description": "mean_pool maps permutations to the same representation.",
                "critical": True,
                "evidence_claim_ids": [claim_id],
                "discriminating_checks": ["order-destruction control"],
            },
        ]
    field_ids = [item["id"] for item in fields]
    stages = [
        {
            "id": "source",
            "kind": "raw_source",
            "description": "Input dataclass",
            "input_field_family_ids": field_ids,
            "output_field_family_ids": field_ids,
            "evidence_claim_ids": [claim_id],
            "risks": [],
        },
        {
            "id": "representation",
            "kind": "representation_boundary",
            "description": "Tuple encoder",
            "input_field_family_ids": field_ids,
            "output_field_family_ids": field_ids,
            "evidence_claim_ids": [claim_id],
            "risks": [] if justified else ["identity and time loss"],
        },
        {
            "id": "aggregation",
            "kind": "aggregation_compression",
            "description": "Sequence aggregation",
            "input_field_family_ids": field_ids,
            "output_field_family_ids": field_ids,
            "evidence_claim_ids": [claim_id],
            "risks": [] if justified else ["order collapse"],
        },
        {
            "id": "evaluation",
            "kind": "evaluation",
            "description": "Ordinary behavior tests",
            "input_field_family_ids": field_ids,
            "output_field_family_ids": field_ids,
            "evidence_claim_ids": [claim_id],
            "risks": [] if justified else ["tests do not detect information loss"],
        },
    ]
    layers = {
        layer: {
            "status": "supported" if justified else "unknown",
            "evidence_claim_ids": [claim_id],
            "unknowns": [] if justified else [f"Need a discriminating {layer} control."],
        }
        for layer in state.INFORMATION_LAYERS
    }
    if not justified:
        layers["pipeline_preservation"]["status"] = "unsupported"
        layers["representation_distinguishability"]["status"] = "unsupported"
    return {
        "field_families": fields,
        "pipeline_stages": stages,
        "layers": layers,
        "loss_boundaries": boundaries,
        "recommended_probes": []
        if justified
        else ["identity ablation", "order-destruction control"],
    }


def raw_role_report(
    role: str,
    *,
    run_id: str = "test-run",
    iteration_id: str = "iteration-0001",
    snapshot_id: str = "snapshot-000000000000000000000000",
    hypothesis_kind: str = "leading",
    with_information: bool | None = None,
    specialist: bool = False,
) -> dict[str, Any]:
    if with_information is None:
        with_information = role in {"cartographer", "clean-room-remapper"}
    unknowns = [
        {
            "id": "u1",
            "question": "Does the output change under an identity-preserving counterfactual?",
            "impact": "Presence alone does not prove utilization.",
            "next_check": "Run a decision-sensitivity control.",
            "critical": False,
        }
    ]
    requests = (
        [
            {
                "profile": "data-ml-causality",
                "unresolved_question": unknowns[0]["question"],
                "expected_evidence": "A counterfactual sensitivity design.",
                "rationale": "Core mapping cannot establish practical utilization.",
                "stopping_condition": "One leakage-safe discriminating control is specified.",
                "priority": "high",
            }
        ]
        if specialist
        else []
    )
    return {
        "schema_version": state.SCHEMA_VERSION,
        "role": role,
        "run_id": run_id,
        "goal_version": 1,
        "iteration_id": iteration_id,
        "source_snapshot_id": snapshot_id,
        "summary": "The ordinary tests pass while the required information path is incomplete.",
        "claims": [sample_claim(statement=f"{role} observed the encode_event loss boundary.")],
        "contradictions": [],
        "unknowns": unknowns,
        "specialist_requests": requests,
        "hypothesis_candidates": [
            {
                "kind": hypothesis_kind,
                "mechanism": f"{hypothesis_kind} mechanism proposed by {role}",
                "predictions": [f"{hypothesis_kind} prediction"],
                "falsifiers": [f"{hypothesis_kind} falsifier"],
                "evidence_for_claim_ids": ["c1"],
                "evidence_against_claim_ids": [],
                "retry_conditions": ["representation changes"],
                "changed_condition": "",
            }
        ],
        "information_assessment": information_assessment() if with_information else None,
    }


def normalized_role(
    role: str,
    *,
    run_id: str = "test-run",
    iteration_id: str = "iteration-0001",
    snapshot_id: str = "snapshot-000000000000000000000000",
    hypothesis_kind: str = "leading",
    specialist: bool = False,
) -> dict[str, Any]:
    return roles.normalize_grounding_report(
        raw_role_report(
            role,
            run_id=run_id,
            iteration_id=iteration_id,
            snapshot_id=snapshot_id,
            hypothesis_kind=hypothesis_kind,
            specialist=specialist,
        ),
        role=role,
        run_id=run_id,
        goal=goal_contract(),
        iteration_id=iteration_id,
        snapshot_id=snapshot_id,
    )


def test_state() -> None:
    with fixture_repo() as project:
        marker = project.parent / "unsafe-goal-git-helper-ran"
        helper = project.parent / "unsafe-goal-git-helper.sh"
        helper.write_text(
            "#!/bin/sh\n"
            f": > {shlex.quote(str(marker))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        helper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        run(
            ["git", "config", "core.fsmonitor", str(helper)],
            project,
        )
        assert_raises(
            "Command-bearing or external-path Git config",
            lambda: state.create_run(
                project,
                goal_contract(),
                requested_run_id="unsafe-git-state-test",
            ),
        )
        if marker.exists():
            raise AssertionError(
                "goal-research executed repository core.fsmonitor before sandboxing"
            )
        run(["git", "config", "--unset", "core.fsmonitor"], project)
        run_dir, projection = state.create_run(
            project, goal_contract(), requested_run_id="state-test"
        )
        if projection["phase"] != state.PHASE_GOAL_FROZEN:
            raise AssertionError("initial phase is not GOAL_FROZEN")
        report = run_dir / "report.md"
        if not report.is_file() or "run_initialized" not in report.read_text(encoding="utf-8"):
            raise AssertionError("initial run did not create its durable report projection")
        if os.name == "posix":
            if stat.S_IMODE(run_dir.stat().st_mode) & 0o077:
                raise AssertionError("run directory is not private")
        _, events, replay = state.load_run(run_dir)
        if replay != projection or len(events) != 1:
            raise AssertionError("event replay changed initial projection")
        artifact = state.write_artifact(
            run_dir, "retry.json", "retry-test", {"value": 1}
        )
        repeated = state.write_artifact(
            run_dir, "retry.json", "retry-test", {"value": 1}
        )
        if artifact != repeated:
            raise AssertionError("idempotent artifact retry changed its descriptor")
        _, baseline = goal_research.baseline_snapshot(run_dir, events, projection)
        pipeline = project / "pipeline.py"
        pipeline.chmod(pipeline.stat().st_mode | stat.S_IXUSR)
        mode_changed = state.capture_repository_snapshot(project)
        if state.snapshot_delta(baseline, mode_changed) != ["pipeline.py"]:
            raise AssertionError("snapshot delta ignored an executable-mode change")
        assert_raises(
            "immutable artifact",
            lambda: state.write_artifact(
                run_dir, "retry.json", "retry-test", {"value": 2}
            ),
        )
        event_path = run_dir / "events.jsonl"
        original = event_path.read_text(encoding="utf-8")
        event_path.write_text(original[:-2], encoding="utf-8")
        assert_raises("event log JSON", lambda: state.load_run(run_dir))

    with fixture_repo() as project:
        run_dir, _ = state.create_run(
            project, goal_contract(), requested_run_id="artifact-corruption-test"
        )
        baseline_path = run_dir / "iterations" / "0001" / "baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline["dirty"] = not baseline["dirty"]
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        assert_raises("artifact digest mismatch", lambda: state.load_run(run_dir))

    with fixture_repo() as project:
        run_dir, _ = state.create_run(
            project, goal_contract(), requested_run_id="event-envelope-test"
        )
        event_path = run_dir / "events.jsonl"
        event = json.loads(event_path.read_text(encoding="utf-8"))
        event["unexpected"] = "must fail"
        event["event_sha256"] = state._event_digest(event)
        event_path.write_text(state.canonical_json(event) + "\n", encoding="utf-8")
        assert_raises("unknown keys", lambda: state.load_run(run_dir))

    with fixture_repo() as project:
        run_dir, _ = state.create_run(
            project, goal_contract(), requested_run_id="amend-test"
        )
        amended = goal_contract(version=2)
        amended["objective"] += " Updated explicitly."
        projection = state.amend_goal(run_dir, amended)
        if projection["goal_version"] != 2 or projection["iteration_number"] != 2:
            raise AssertionError("goal amendment did not version and invalidate the iteration")
        invalid = goal_contract(version=3)
        invalid["budgets"]["max_advisor_turns"] += 1
        assert_raises("frozen budgets", lambda: state.amend_goal(run_dir, invalid))

    waived = goal_contract()
    waived["waivers"] = [
        {
            "id": "user-waiver-utilization",
            "acceptance_dimension_id": "utilization-control",
            "user_decision": "The user explicitly accepts this bounded omission for version 1.",
            "reason": "The qualitative control is deferred outside this bounded run.",
        }
    ]
    normalized_waived = state.validate_goal_contract(waived)
    if "utilization-control" not in state.waiver_map(normalized_waived):
        raise AssertionError("explicit user waiver was not retained in the frozen contract")
    invalid_hard_waiver = goal_contract()
    invalid_hard_waiver["waivers"] = [
        {
            "id": "invalid-hard-waiver",
            "acceptance_dimension_id": "information-path",
            "user_decision": "User asked to waive this invariant.",
            "reason": "Invalid test waiver.",
        }
    ]
    assert_raises(
        "hard-invariant",
        lambda: state.validate_goal_contract(invalid_hard_waiver),
    )
    assert_raises(
        "explicit user waiver",
        lambda: state.validate_audit_updates(
            {
                "acceptance_updates": [
                    {
                        "id": "utilization-control",
                        "status": "waived",
                        "evidence_classes": [],
                        "evidence": "Unapproved waiver attempt.",
                    }
                ],
                "goal_clause_updates": [],
                "critical_contradiction_ids": [],
            },
            state.validate_goal_contract(goal_contract()),
        ),
    )
    missing_evidence = goal_contract()
    missing_evidence["acceptance_dimensions"][0]["evidence_requirements"] = []
    assert_raises(
        "evidence_requirements",
        lambda: state.validate_goal_contract(missing_evidence),
    )
    unknown_budget = goal_contract()
    unknown_budget["budgets"]["surprise"] = 1
    assert_raises(
        "unknown keys",
        lambda: state.validate_goal_contract(unknown_budget),
    )
    missing_escalation = goal_contract()
    missing_escalation["escalation_conditions"] = []
    assert_raises(
        "escalation_conditions",
        lambda: state.validate_goal_contract(missing_escalation),
    )

    with fixture_repo() as project:
        baseline = state.capture_repository_snapshot(project)
        (project / "test_pipeline.py").write_text("outside packet\n", encoding="utf-8")
        resulting = state.capture_repository_snapshot(project)
        packet = {
            "packet_id": "packet-test",
            "permitted_scope": ["pipeline.py"],
            "forbidden_scope": [],
        }
        raw = {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": "run-test",
            "goal_version": 1,
            "iteration_id": "iteration-0001",
            "packet_id": "packet-test",
            "baseline_snapshot_id": baseline["snapshot_id"],
            "resulting_snapshot_id": resulting["snapshot_id"],
            "summary": "outside scope",
            "changed_paths": ["test_pipeline.py"],
            "commands": [],
            "retained_evidence_paths": [],
        }
        assert_raises(
            "outside the packet scope",
            lambda: state.validate_codex_receipt(
                raw,
                run_id="run-test",
                goal=goal_contract(),
                iteration_id="iteration-0001",
                packet=packet,
                baseline=baseline,
                resulting=resulting,
            ),
        )

    with fixture_repo() as project:
        baseline = state.capture_repository_snapshot(project)
        pipeline = project / "pipeline.py"
        pipeline.write_text(pipeline.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        run(["git", "add", "pipeline.py"], project)
        resulting = state.capture_repository_snapshot(project)
        raw = {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": "run-test",
            "goal_version": 1,
            "iteration_id": "iteration-0001",
            "packet_id": "packet-test",
            "baseline_snapshot_id": baseline["snapshot_id"],
            "resulting_snapshot_id": resulting["snapshot_id"],
            "summary": "staged change",
            "changed_paths": ["pipeline.py"],
            "commands": [],
            "retained_evidence_paths": [],
        }
        assert_raises(
            "staged-index change",
            lambda: state.validate_codex_receipt(
                raw,
                run_id="run-test",
                goal=goal_contract(),
                iteration_id="iteration-0001",
                packet={
                    "packet_id": "packet-test",
                    "permitted_scope": ["pipeline.py"],
                    "forbidden_scope": [],
                },
                baseline=baseline,
                resulting=resulting,
            ),
        )


def test_roles() -> None:
    cartographer_key = roles.repo_role_conversation_key("test-run", "cartographer")
    if cartographer_key != roles.repo_role_conversation_key("test-run", "cartographer"):
        raise AssertionError("repo-aware role conversation key is not stable")
    if cartographer_key == roles.repo_role_conversation_key("other-run", "cartographer"):
        raise AssertionError("repo-aware role conversations leaked across goal runs")
    if cartographer_key == roles.repo_role_conversation_key("test-run", "falsifier"):
        raise AssertionError("independent repo-aware roles share one conversation")
    for blind_role in roles.FRESH_CONVERSATION_ROLES:
        if roles.repo_role_conversation_key("test-run", blind_role) is not None:
            raise AssertionError(f"{blind_role} did not retain clean-room chat isolation")
    contract = goal_contract()
    prompt = roles.role_prompt(
        role="cartographer",
        run_id="test-run",
        goal=contract,
        iteration_id="iteration-0001",
        snapshot={
            "snapshot_id": "snapshot-" + "0" * 24,
            "head": "0" * 40,
            "index_sha256": "1" * 64,
            "status_sha256": "2" * 64,
            "dirty": False,
            "changed_paths": [],
        },
    )
    vocabulary = roles.role_output_vocabulary(contract)
    if set(vocabulary["claims[].status"]) != state.CLAIM_STATUSES:
        raise AssertionError("role prompt claim status vocabulary drifted from validation")
    if set(vocabulary["hypothesis_candidates[].kind"]) != state.HYPOTHESIS_KINDS:
        raise AssertionError("role prompt hypothesis vocabulary drifted from validation")
    if set(vocabulary["information_assessment.field_families[].classification"]) != state.FIELD_CLASSIFICATIONS:
        raise AssertionError("role prompt field vocabulary drifted from validation")
    if vocabulary["specialist_requests[].profile"] != contract["allowed_specialists"]:
        raise AssertionError("role prompt did not constrain specialists to the frozen goal")
    for required_text in (
        "Every listed field is an enum",
        "unsupported is valid only for information layer status",
        "supporting is not a hypothesis kind",
        "lost_at_encoding are descriptions, not classification values",
        "required evidence does not yet exist",
    ):
        if required_text not in prompt:
            raise AssertionError(f"role prompt omitted enum guidance: {required_text}")
    specialist_prompt = roles.role_prompt(
        role="data-ml-causality",
        run_id="test-run",
        goal=contract,
        iteration_id="iteration-0001",
        snapshot={
            "snapshot_id": "snapshot-" + "0" * 24,
            "head": "0" * 40,
            "index_sha256": "1" * 64,
            "status_sha256": "2" * 64,
            "dirty": False,
            "changed_paths": [],
        },
    )
    if '"information_assessment": null' not in specialist_prompt:
        raise AssertionError("specialist prompt did not freeze information_assessment to null")
    if '"specialist_requests": []' not in specialist_prompt:
        raise AssertionError("specialist prompt invited nested specialist requests")
    if "must be exactly null" not in specialist_prompt:
        raise AssertionError("specialist prompt omitted its information-assessment ownership rule")
    command = roles._agent_command(
        project=ROOT,
        role="cartographer",
        role_dir=ROOT / ".codex-advisor" / "test-role",
        marker="GOAL-RESEARCH-TEST-COMPLETE",
        base_url="http://127.0.0.1:8080/v1",
        timeout=0,
        queue_timeout=0,
        max_output_tokens=4000,
        conversation_key=cartographer_key,
        resume=False,
        live_activity=False,
    )
    key_index = command.index("--conversation-key")
    if command[key_index + 1] != cartographer_key:
        raise AssertionError("repo-aware role command lost its stable conversation key")
    prompt_command = roles._prompt_command(
        timeout=0,
        response_path=ROOT / ".codex-advisor" / "prompt-response.md",
        live_activity=False,
    )
    if "--base-url" in prompt_command:
        raise AssertionError("prompt-only goal phase passed an unsupported advisor.py CLI flag")
    journal = ROOT / ".codex-advisor" / "missing-turn-journal.json"
    if roles._turn_submission_started(journal):
        raise AssertionError("missing turn journal was treated as a submitted advisor turn")

    report = normalized_role("cartographer", specialist=True)
    if not report["claims"] or report["information_assessment"] is None:
        raise AssertionError("cartographer normalization lost grounded evidence")
    invalid_claim = raw_role_report("verifier")
    invalid_claim["claims"][0]["status"] = "unsupported"
    assert_raises(
        "claim status is invalid",
        lambda: roles.normalize_grounding_report(
            invalid_claim,
            role="verifier",
            run_id="test-run",
            goal=contract,
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    invalid_kind = raw_role_report("falsifier")
    invalid_kind["hypothesis_candidates"][0]["kind"] = "supporting"
    assert_raises(
        "hypothesis candidate kind is invalid",
        lambda: roles.normalize_grounding_report(
            invalid_kind,
            role="falsifier",
            run_id="test-run",
            goal=contract,
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    invalid_classification = raw_role_report("cartographer")
    invalid_classification["information_assessment"]["field_families"][0][
        "classification"
    ] = "lost_at_encoding"
    assert_raises(
        "field family classification is invalid",
        lambda: roles.normalize_grounding_report(
            invalid_classification,
            role="cartographer",
            run_id="test-run",
            goal=contract,
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    specialist_assessment = raw_role_report("data-ml-causality")
    specialist_assessment["information_assessment"] = information_assessment()
    assert_raises(
        "information_assessment=null",
        lambda: roles.normalize_grounding_report(
            specialist_assessment,
            role="data-ml-causality",
            run_id="test-run",
            goal=contract,
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    merged = roles.merge_grounding_reports([report])
    selection = roles.select_specialists(merged, goal_contract())
    if [item["profile"] for item in selection["selected"]] != ["data-ml-causality"]:
        raise AssertionError("justified specialist was not selected")
    no_specialist = normalized_role("cartographer", specialist=False)
    omitted = roles.select_specialists(
        roles.merge_grounding_reports([no_specialist]), goal_contract()
    )
    if omitted["selected"] or not omitted["omitted_reason"]:
        raise AssertionError("unjustified specialist selection was not omitted")

    with fixture_repo() as project:
        checkpoint = project / ".codex-advisor" / "schema-conversion"
        checkpoint.mkdir(parents=True)
        prompt = "validate malformed advisor output"
        digest = __import__("hashlib").sha256(prompt.encode()).hexdigest()
        paths = roles._prompt_phase_paths(checkpoint)
        paths["response"].write_text("{}\n", encoding="utf-8")
        paths["meta"].write_text(
            json.dumps(
                {
                    "schema_version": state.SCHEMA_VERSION,
                    "status": "ok",
                    "input_sha256": digest,
                    "response_path": str(paths["response"]),
                    "response_source": "test",
                }
            ),
            encoding="utf-8",
        )
        try:
            roles.run_prompt_phase(
                project=project,
                checkpoint_dir=checkpoint,
                prompt=prompt,
                normalize=lambda raw: state.validate_goal_contract(raw),
            )
        except roles.GoalResearchRoleError as exc:
            if "failed schema validation" not in str(exc):
                raise AssertionError("schema error lost its role-failure classification") from exc
        else:
            raise AssertionError("malformed advisor output bypassed role-failure classification")
    specialist_raw = raw_role_report("data-ml-causality", specialist=True)
    assert_raises(
        "cannot spawn nested specialists",
        lambda: roles.normalize_grounding_report(
            specialist_raw,
            role="data-ml-causality",
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    raw = raw_role_report("cartographer")
    raw["claims"][0]["repository_locations"] = []
    assert_raises(
        "concrete repository location",
        lambda: roles.normalize_grounding_report(
            raw,
            role="cartographer",
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            snapshot_id="snapshot-000000000000000000000000",
        ),
    )
    contradiction_raw = raw_role_report("falsifier")
    contradiction_raw["contradictions"] = [
        {
            "claim_ids": ["c1"],
            "description": "The implementation shape conflicts with the stated information goal.",
            "severity": "critical",
            "critical": True,
            "resolution_check": "Run the exact identity-preservation control.",
        }
    ]
    contradiction_report = roles.normalize_grounding_report(
        contradiction_raw,
        role="falsifier",
        run_id="test-run",
        goal=goal_contract(),
        iteration_id="iteration-0001",
        snapshot_id="snapshot-000000000000000000000000",
    )
    claim = contradiction_report["claims"][0]
    contradiction = contradiction_report["contradictions"][0]
    hypothesis = state.validate_hypotheses(
        [
            {
                "id": "hypothesis-test",
                "kind": "leading",
                "mechanism": "Representation loss",
                "predictions": ["Identity control fails"],
                "falsifiers": ["Identity control passes"],
                "evidence_for_claim_ids": [claim["id"]],
                "evidence_against_claim_ids": [],
                "retry_conditions": [],
                "status": "active",
                "changed_condition": "",
            }
        ],
        goal_contract(),
        known_claim_ids={claim["id"]},
    )
    raw_challenge = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": "test-run",
        "goal_version": 1,
        "iteration_id": "iteration-0001",
        "round": 1,
        "claim_reviews": [
            {
                "claim_id": claim["id"],
                "disposition": "supported",
                "reasoning": "The repository observation is precise.",
                "proposed_check": "Run the identity control.",
            }
        ],
        "hypothesis_reviews": [
            {
                "hypothesis_id": "hypothesis-test",
                "disposition": "retain",
                "reasoning": "The control is still needed.",
            }
        ],
        "new_contradictions": [],
        "preserved_contradiction_ids": [contradiction["id"]],
        "recommended_investigation": {
            "hypothesis_id": "hypothesis-test",
            "kind": "experiment",
            "description": "Run the identity control.",
            "relative_cost": "low",
            "rationale": "It discriminates the leading hypothesis.",
        },
    }
    normalized_challenge = roles.normalize_challenge(
        raw_challenge,
        run_id="test-run",
        goal=goal_contract(),
        iteration_id="iteration-0001",
        claims=[claim],
        contradictions=[contradiction],
        hypotheses=hypothesis,
    )
    if normalized_challenge["preserved_contradiction_ids"] != [contradiction["id"]]:
        raise AssertionError("challenge erased an open contradiction")
    synthesis_text = roles.synthesis_prompt(
        packet_id="packet-test",
        run_id="test-run",
        goal=goal_contract(),
        iteration_id="iteration-0001",
        baseline_snapshot_id="snapshot-000000000000000000000000",
        claims=[claim],
        contradictions=[contradiction],
        hypotheses=hypothesis,
        challenge=normalized_challenge,
    )
    for required_text in (
        "blocking_reasons must be exactly []",
        "not in blocking_reasons",
        '"issue_packet"',
        '"block"',
    ):
        if required_text not in synthesis_text:
            raise AssertionError(f"synthesis prompt omitted branch contract: {required_text}")
    invalid_synthesis = synthesis_raw(
        packet_id="packet-test",
        run={"run_id": "test-run"},
        projection={"goal_version": 1, "iteration_id": "iteration-0001"},
        baseline={"snapshot_id": "snapshot-000000000000000000000000"},
        claim_id=claim["id"],
        hypothesis_id=hypothesis[0]["id"],
    )
    invalid_synthesis["preserved_contradiction_ids"] = [contradiction["id"]]
    invalid_synthesis["packet"]["open_contradiction_ids"] = [contradiction["id"]]
    invalid_synthesis["blocking_reasons"] = [
        "A current evidence gap that the packet is designed to resolve."
    ]
    assert_raises(
        "no blocking_reasons",
        lambda: roles.normalize_synthesis(
            invalid_synthesis,
            packet_id="packet-test",
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            baseline_snapshot_id="snapshot-000000000000000000000000",
            claims=[claim],
            contradictions=[contradiction],
            hypotheses=hypothesis,
        ),
    )
    raw_post = post_audit_raw(
        run={"run_id": "test-run"},
        projection={"goal_version": 1, "iteration_id": "iteration-0001"},
        snapshot={"snapshot_id": "snapshot-000000000000000000000000"},
        hypotheses=hypothesis,
    )
    raw_post["existing_contradiction_updates"] = [
        {
            "id": contradiction["id"],
            "status": "resolved",
            "evidence": "The fresh post-change claim directly resolves this contradiction.",
            "evidence_claim_ids": ["c1"],
        }
    ]
    normalized_post = roles.normalize_post_audit(
        raw_post,
        run_id="test-run",
        goal=goal_contract(),
        iteration_id="iteration-0001",
        resulting_snapshot_id="snapshot-000000000000000000000000",
        existing_contradictions=[contradiction],
        hypotheses=hypothesis,
    )
    if normalized_post["critical_contradiction_ids"]:
        raise AssertionError("freshly resolved critical contradiction remained a gate blocker")
    omitted_existing = dict(raw_post, existing_contradiction_updates=[])
    assert_raises(
        "every existing contradiction exactly once",
        lambda: roles.normalize_post_audit(
            omitted_existing,
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            resulting_snapshot_id="snapshot-000000000000000000000000",
            existing_contradictions=[contradiction],
            hypotheses=hypothesis,
        ),
    )
    unsupported_resolution = json.loads(json.dumps(raw_post))
    unsupported_resolution["existing_contradiction_updates"][0][
        "evidence_claim_ids"
    ] = []
    assert_raises(
        "requires fresh evidence_claim_ids",
        lambda: roles.normalize_post_audit(
            unsupported_resolution,
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            resulting_snapshot_id="snapshot-000000000000000000000000",
            existing_contradictions=[contradiction],
            hypotheses=hypothesis,
        ),
    )
    refresh_base = {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": "test-run",
        "goal_version": 1,
        "iteration_id": "iteration-0001",
        "recommendation": "next_iteration",
        "rationale": "One bounded remap is still required.",
        "hypothesis_updates": normalized_post["hypothesis_updates"],
        "remaining_contradiction_ids": [],
        "remap_required": True,
        "remap_reasons": ["An open contradiction remains."],
        "next_discriminating_question": "Which boundary remains open?",
    }
    assert_raises(
        "preserve every open contradiction exactly",
        lambda: roles.normalize_epistemic_refresh(
            refresh_base,
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            hypotheses=hypothesis,
            contradictions=[contradiction],
            post_audit=normalized_post,
        ),
    )
    invalid_final_refresh = {
        **refresh_base,
        "recommendation": "final_audit",
        "rationale": "Candidate completion.",
        "next_discriminating_question": "",
    }
    assert_raises(
        "cannot retain remap work",
        lambda: roles.normalize_epistemic_refresh(
            invalid_final_refresh,
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            hypotheses=hypothesis,
            contradictions=[],
            post_audit=normalized_post,
        ),
    )
    invalid_round = dict(raw_challenge, round=2)
    assert_raises(
        "round",
        lambda: roles.normalize_challenge(
            invalid_round,
            run_id="test-run",
            goal=goal_contract(),
            iteration_id="iteration-0001",
            claims=[claim],
            contradictions=[contradiction],
            hypotheses=hypothesis,
        ),
    )


def test_hypotheses() -> None:
    reports = [
        normalized_role("cartographer", hypothesis_kind="leading"),
        normalized_role("falsifier", hypothesis_kind="alternative"),
        normalized_role("verifier", hypothesis_kind="null_measurement"),
    ]
    candidates = roles.merge_grounding_reports(reports)["hypothesis_candidates"]
    portfolio = roles.build_hypothesis_portfolio(candidates, goal_contract())
    if {item["kind"] for item in portfolio} != {
        "leading",
        "alternative",
        "null_measurement",
    }:
        raise AssertionError("portfolio did not preserve three competing explanations")
    rejected = [dict(portfolio[0], status="rejected")]
    same = [dict(candidates[0], changed_condition="")]
    assert_raises(
        "changed_condition",
        lambda: roles.build_hypothesis_portfolio(same, goal_contract(), prior=rejected),
    )
    same[0]["changed_condition"] = "The representation now includes wallet identity."
    roles.build_hypothesis_portfolio(same, goal_contract(), prior=rejected)


def test_information_path() -> None:
    positive = state.validate_information_assessment(information_assessment())
    boundaries = {item["id"]: item for item in positive["loss_boundaries"]}
    if boundaries["identity-time-drop"]["failure_layer"] != "pipeline_preservation":
        raise AssertionError("positive fixture classified the wrong loss layer")
    if "order-destruction control" not in positive["recommended_probes"]:
        raise AssertionError("positive fixture lost its discriminating order control")
    generic = information_assessment()
    generic["loss_boundaries"] = []
    assert_raises(
        "critical loss boundary",
        lambda: state.validate_information_assessment(generic),
    )
    negative = state.validate_information_assessment(
        information_assessment(justified=True)
    )
    if negative["loss_boundaries"]:
        raise AssertionError("justified negative exclusion became a critical finding")
    catalog = json.loads((FIXTURES / "benchmark-catalog.json").read_text(encoding="utf-8"))
    layers = {item["layer"] for item in catalog["cases"]}
    if not set(state.INFORMATION_LAYERS).issubset(layers):
        raise AssertionError("benchmark catalog omits a required epistemic failure layer")
    for case in catalog["cases"]:
        assessment = information_assessment()
        assessment["loss_boundaries"][0]["failure_layer"] = case["layer"]
        assessment["loss_boundaries"][0]["id"] = case["id"]
        for layer in state.INFORMATION_LAYERS:
            assessment["layers"][layer]["status"] = "unknown"
        assessment["layers"][case["layer"]]["status"] = "unsupported"
        assessment["layers"]["representation_distinguishability"]["status"] = "unsupported"
        normalized_case = state.validate_information_assessment(assessment)
        if normalized_case["loss_boundaries"][0]["failure_layer"] != case["layer"]:
            raise AssertionError(f"benchmark case collapsed layer {case['id']}")


def test_clean_room() -> None:
    prompt = roles.role_prompt(
        role="clean-room-remapper",
        run_id="test-run",
        goal=goal_contract(),
        iteration_id="iteration-0001",
        snapshot={
            "snapshot_id": "snapshot-000000000000000000000000",
            "head": "0" * 40,
            "index_sha256": "1" * 64,
            "status_sha256": "2" * 64,
            "dirty": False,
            "changed_paths": [],
        },
        task_context={},
        clean_room=True,
    )
    forbidden = ["accepted hypothesis", "implementation-packet-123", "inherited diagnosis"]
    if any(item in prompt for item in forbidden):
        raise AssertionError("clean-room prompt leaked inherited conclusions")
    if "You receive no prior conclusions" not in prompt:
        raise AssertionError("clean-room prompt lacks the isolation contract")
    inherited = normalized_role("falsifier", hypothesis_kind="alternative")
    inherited["summary"] = "Inherited framing incorrectly focuses only on label tuning."
    clean = normalized_role("clean-room-remapper", hypothesis_kind="leading")
    merged = roles.merge_grounding_reports([inherited, clean])
    clean_assessments = [
        item["assessment"]
        for item in merged["information_assessments"]
        if item["role"] == "clean-room-remapper"
    ]
    if not clean_assessments or {
        item["id"] for item in clean_assessments[0]["loss_boundaries"]
    } != {"identity-time-drop", "order-collapse"}:
        raise AssertionError("clean-room remap did not recover the omitted loss boundary")


def fidelity_trace(
    covered: bool = True,
    *,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    goal = goal_contract()
    supplied_evidence_ids = list(evidence_ids or [])
    return {
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
                "evidence_ids": supplied_evidence_ids if covered else [],
                "covered": covered,
                "drift_risks": [],
            }
            for clause in goal["clauses"]
        ],
        "proxy_drift": [],
        "blocking_issues": [],
    }


def test_goal_fidelity() -> None:
    normalized = state.validate_fidelity_trace(fidelity_trace(), goal_contract())
    if not all(item["covered"] for item in normalized["clause_trace"]):
        raise AssertionError("goal-fidelity trace lost clause coverage")
    missing = fidelity_trace()
    missing["clause_trace"] = missing["clause_trace"][:-1]
    assert_raises(
        "cover every goal clause",
        lambda: state.validate_fidelity_trace(missing, goal_contract()),
    )
    remapped = fidelity_trace()
    remapped["clause_trace"][0]["acceptance_ids"] = ["utilization-control"]
    assert_raises(
        "exact clause-to-acceptance mapping",
        lambda: state.validate_fidelity_trace(remapped, goal_contract()),
    )


@contextmanager
def patched(target: Any, name: str, value: Any) -> Iterator[None]:
    previous = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, previous)


def controller_options() -> goal_research.ControllerOptions:
    return goal_research.ControllerOptions(
        base_url="http://127.0.0.1:8080/v1",
        timeout=0,
        queue_timeout=0,
        max_output_tokens=6000,
        live_activity=False,
    )


def reload_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    return state.load_run(run_dir)


def fake_independent_runner(
    *,
    project: Path,
    role_root: Path,
    role_prompts: dict[str, str],
    normalizers: dict[str, Any],
    conversation_namespace: str | None = None,
    **_: Any,
) -> list[roles.RoleResult]:
    run_dir = next(project.joinpath(".codex-advisor", "goal-research-runs").iterdir())
    run, _, projection = state.load_run(run_dir)
    if conversation_namespace != f"{run['run_id']}-v{projection['goal_version']}":
        raise AssertionError("grounding did not preserve the versioned goal conversation namespace")
    _, baseline = goal_research.baseline_snapshot(run_dir, state.read_events(run_dir), projection)
    kind_by_role = {
        "cartographer": "leading",
        "clean-room-remapper": "leading",
        "falsifier": "alternative",
        "verifier": "null_measurement",
    }
    results: list[roles.RoleResult] = []
    for role in role_prompts:
        raw = raw_role_report(
            role,
            run_id=run["run_id"],
            iteration_id=projection["iteration_id"],
            snapshot_id=baseline["snapshot_id"],
            hypothesis_kind=kind_by_role.get(role, "alternative"),
            specialist=False,
        )
        report = normalizers[role](raw)
        role_dir = role_root / role
        role_dir.mkdir(parents=True, exist_ok=True)
        results.append(
            roles.RoleResult(
                role=role,
                report=report,
                metadata={},
                run_dir=role_dir,
                workspace_generation="a" * 24,
                workspace_fingerprint="b" * 64,
                resumed=False,
            )
        )
    return results


def challenge_raw(
    run: dict[str, Any], projection: dict[str, Any], claims: list[dict[str, Any]], hypotheses: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": state.SCHEMA_VERSION,
        "run_id": run["run_id"],
        "goal_version": projection["goal_version"],
        "iteration_id": projection["iteration_id"],
        "round": 1,
        "claim_reviews": [
            {
                "claim_id": claims[0]["id"],
                "disposition": "supported",
                "reasoning": "The exact return tuple omits two source fields.",
                "proposed_check": "Run identity and order controls.",
            }
        ],
        "hypothesis_reviews": [
            {
                "hypothesis_id": item["id"],
                "disposition": "retain",
                "reasoning": "Keep this explanation until the discriminating control runs.",
            }
            for item in hypotheses
        ],
        "new_contradictions": [],
        "preserved_contradiction_ids": [],
        "recommended_investigation": {
            "hypothesis_id": hypotheses[0]["id"],
            "kind": "experiment",
            "description": "Add a bounded information-preservation control.",
            "relative_cost": "low",
            "rationale": "It distinguishes missing representation from a null measurement cause.",
        },
    }


def synthesis_raw(
    *,
    packet_id: str,
    run: dict[str, Any],
    projection: dict[str, Any],
    baseline: dict[str, Any],
    claim_id: str,
    hypothesis_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": state.SCHEMA_VERSION,
        "decision": "issue_packet",
        "decision_reason": "A bounded control is the cheapest discriminating next step.",
        "preserved_contradiction_ids": [],
        "blocking_reasons": [],
        "packet": {
            "schema_version": state.SCHEMA_VERSION,
            "packet_id": packet_id,
            "run_id": run["run_id"],
            "goal_version": projection["goal_version"],
            "iteration_id": projection["iteration_id"],
            "baseline_snapshot_id": baseline["snapshot_id"],
            "hypothesis_id": hypothesis_id,
            "objective": "Record a bounded identity/order control without expanding scope.",
            "rationale": "The control discriminates between representation loss and null measurement.",
            "permitted_scope": ["pipeline.py", "test_pipeline.py"],
            "forbidden_scope": [],
            "evidence_claim_ids": [claim_id],
            "required_checks": ["python3 test_pipeline.py"],
            "expected_signals": ["The control distinguishes identity or order changes."],
            "rejection_criteria": ["The output remains invariant for a goal-relevant counterfactual."],
            "rollback_guidance": "Do not auto-revert; retain evidence and let Codex or the user decide.",
            "open_contradiction_ids": [],
        },
    }


def post_audit_raw(
    *,
    run: dict[str, Any],
    projection: dict[str, Any],
    snapshot: dict[str, Any],
    hypotheses: list[dict[str, Any]],
) -> dict[str, Any]:
    goal = goal_contract()
    return {
        "schema_version": state.SCHEMA_VERSION,
        "role": "post-change-auditor",
        "run_id": run["run_id"],
        "goal_version": projection["goal_version"],
        "iteration_id": projection["iteration_id"],
        "source_snapshot_id": snapshot["snapshot_id"],
        "summary": "Fresh audit accepts the bounded fixture iteration.",
        "outcome": "accepted",
        "claims": [sample_claim(statement="Post-change audit verified the bounded fixture path.")],
        "contradictions": [],
        "unknowns": [],
        "acceptance_updates": [
            {
                "id": item["id"],
                "status": "passed",
                "evidence_classes": ["codex_local_result", "independent_audit_result"],
                "evidence": "The deterministic control and fresh repository audit passed.",
            }
            for item in goal["acceptance_dimensions"]
        ],
        "goal_clause_updates": [
            {
                "id": item["id"],
                "status": "supported",
                "evidence": "Fresh repository and local evidence support this clause.",
            }
            for item in goal["clauses"]
        ],
        "existing_contradiction_updates": [],
        "hypothesis_updates": [
            {
                "id": item["id"],
                "status": "supported" if index == 0 else "inconclusive",
                "evidence": "The bounded iteration updated this explanation.",
            }
            for index, item in enumerate(hypotheses)
        ],
    }


def final_audit_raw(
    *,
    run: dict[str, Any],
    projection: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    goal = goal_contract()
    return {
        "schema_version": state.SCHEMA_VERSION,
        "role": "final-blind-auditor",
        "run_id": run["run_id"],
        "goal_version": projection["goal_version"],
        "iteration_id": projection["iteration_id"],
        "source_snapshot_id": snapshot["snapshot_id"],
        "summary": "Blind audit independently supports fixture completion.",
        "claims": [sample_claim(statement="Final blind audit inspected the complete fixture path.")],
        "contradictions": [],
        "goal_clause_status": [
            {"id": item["id"], "status": "supported", "evidence": "Blind audit evidence."}
            for item in goal["clauses"]
        ],
        "acceptance_status": [
            {"id": item["id"], "status": "passed", "evidence": "Blind audit evidence."}
            for item in goal["acceptance_dimensions"]
        ],
        "blocking_findings": [],
        "recommend_completion": True,
    }


def test_iteration() -> None:
    with fixture_repo() as project:
        run_dir, projection = state.create_run(
            project, goal_contract(), requested_run_id="iteration-test"
        )
        options = controller_options()

        def prompt_fidelity(**kwargs: Any) -> dict[str, Any]:
            return kwargs["normalize"](fidelity_trace(covered=True))

        run_data, events, projection = reload_run(run_dir)
        with patched(roles, "run_prompt_phase", prompt_fidelity):
            goal_research.phase_goal_fidelity(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        with patched(roles, "run_independent_repo_roles", fake_independent_runner):
            goal_research.phase_grounding(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        goal_research.phase_hypotheses(
            project, run_dir, run_data, events, projection, options
        )

        run_data, events, projection = reload_run(run_dir)
        _, claims, _, hypotheses = goal_research._iteration_evidence(
            run_dir, events, projection
        )

        def prompt_challenge(**kwargs: Any) -> dict[str, Any]:
            return kwargs["normalize"](
                challenge_raw(run_data, projection, claims, hypotheses)
            )

        with patched(roles, "run_prompt_phase", prompt_challenge):
            goal_research.phase_challenge(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        _, baseline = goal_research.baseline_snapshot(run_dir, events, projection)

        def prompt_synthesis(**kwargs: Any) -> dict[str, Any]:
            match = __import__("re").search(r'"packet_id": "([^"]+)"', kwargs["prompt"])
            if match is None:
                raise AssertionError("packet id missing from synthesis prompt")
            return kwargs["normalize"](
                synthesis_raw(
                    packet_id=match.group(1),
                    run=run_data,
                    projection=projection,
                    baseline=baseline,
                    claim_id=claims[0]["id"],
                    hypothesis_id=hypotheses[0]["id"],
                )
            )

        with patched(roles, "run_prompt_phase", prompt_synthesis):
            goal_research.phase_synthesis(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        goal_research.phase_packet_ready(project, run_dir, events, projection)
        run_data, events, projection = reload_run(run_dir)
        packet = goal_research.active_packet(run_dir, events, projection)
        _, baseline = goal_research.baseline_snapshot(run_dir, events, projection)
        current = state.capture_repository_snapshot(project)
        codex_input = run_dir / "codex-input.json"
        codex_input.write_text(
            json.dumps(
                {
                    "schema_version": state.SCHEMA_VERSION,
                    "run_id": run_data["run_id"],
                    "goal_version": projection["goal_version"],
                    "iteration_id": projection["iteration_id"],
                    "packet_id": packet["packet_id"],
                    "baseline_snapshot_id": baseline["snapshot_id"],
                    "resulting_snapshot_id": current["snapshot_id"],
                    "summary": "No source edit was required for the mocked control receipt.",
                    "changed_paths": [],
                    "commands": [],
                    "retained_evidence_paths": [],
                }
            ),
            encoding="utf-8",
        )
        goal_research.command_record_codex(
            argparse.Namespace(
                project_dir=project,
                run_dir=str(run_dir),
                receipt_file=codex_input,
                json=True,
            )
        )

        run_data, events, projection = reload_run(run_dir)
        verification_input = run_dir / "verification-input.json"
        verification_input.write_text(
            json.dumps(
                {
                    "schema_version": state.SCHEMA_VERSION,
                    "run_id": run_data["run_id"],
                    "goal_version": projection["goal_version"],
                    "iteration_id": projection["iteration_id"],
                    "packet_id": packet["packet_id"],
                    "resulting_snapshot_id": current["snapshot_id"],
                    "commands": [
                        {
                            "command": "python3 test_pipeline.py",
                            "exit_code": 0,
                            "duration_seconds": 0.1,
                            "evidence_path": "",
                        }
                    ],
                    "required_check_results": [
                        {
                            "check": check,
                            "status": "passed",
                            "evidence": "Deterministic fixture result.",
                        }
                        for check in packet["required_checks"]
                    ],
                    "acceptance_results": [
                        {
                            "id": item["id"],
                            "status": "passed",
                            "evidence": "Deterministic fixture result.",
                            "evidence_class": "codex_local_result",
                        }
                        for item in goal_contract()["acceptance_dimensions"]
                    ],
                    "notes": "",
                }
            ),
            encoding="utf-8",
        )
        goal_research.command_record_verification(
            argparse.Namespace(
                project_dir=project,
                run_dir=str(run_dir),
                receipt_file=verification_input,
                json=True,
            )
        )

        run_data, events, projection = reload_run(run_dir)
        hypothesis_records = goal_research.read_iteration_artifact(
            run_dir, events, projection, "hypotheses"
        )["hypotheses"]

        def fake_post_role(**kwargs: Any) -> roles.RoleResult:
            expected_key = roles.repo_role_conversation_key(
                f"{run_data['run_id']}-v{projection['goal_version']}",
                "post-change-auditor",
            )
            if kwargs.get("conversation_key") != expected_key:
                raise AssertionError("post-change auditor lost its stable role conversation")
            raw = post_audit_raw(
                run=run_data,
                projection=projection,
                snapshot=current,
                hypotheses=hypothesis_records,
            )
            report = kwargs["normalize"](raw)
            return roles.RoleResult(
                role="post-change-auditor",
                report=report,
                metadata={},
                run_dir=kwargs["role_dir"],
                workspace_generation="a" * 24,
                workspace_fingerprint="b" * 64,
                resumed=False,
            )

        with patched(roles, "run_repo_role", fake_post_role):
            goal_research.phase_post_audit(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        post = goal_research.read_iteration_artifact(
            run_dir, events, projection, "post-audit"
        )

        def fake_refresh_prompts(**kwargs: Any) -> dict[str, Any]:
            checkpoint = Path(kwargs["checkpoint_dir"]).name
            if checkpoint == "post-goal-fidelity":
                return kwargs["normalize"](
                    fidelity_trace(
                        covered=True,
                        evidence_ids=[item["id"] for item in post["claims"]],
                    )
                )
            if checkpoint != "epistemic-refresh":
                raise AssertionError(f"unexpected prompt checkpoint: {checkpoint}")
            raw = {
                "schema_version": state.SCHEMA_VERSION,
                "run_id": run_data["run_id"],
                "goal_version": projection["goal_version"],
                "iteration_id": projection["iteration_id"],
                "recommendation": "final_audit",
                "rationale": "All controller gates now have fresh evidence.",
                "hypothesis_updates": post["hypothesis_updates"],
                "remaining_contradiction_ids": [],
                "remap_required": False,
                "remap_reasons": [],
                "next_discriminating_question": "",
            }
            return kwargs["normalize"](raw)

        with patched(roles, "run_prompt_phase", fake_refresh_prompts):
            goal_research.phase_epistemic_refresh(
                project, run_dir, run_data, events, projection, options
            )

        run_data, events, projection = reload_run(run_dir)
        goal_research.phase_refresh_decision(
            project, run_dir, run_data, events, projection
        )

        run_data, events, projection = reload_run(run_dir)
        _, frozen_final = goal_research.event_artifact(
            run_dir,
            events,
            kind="final-audit-snapshot",
            iteration_id=projection["iteration_id"],
        )
        assert frozen_final is not None
        blind_evidence = goal_research.final_blind_local_evidence(run_dir, events)
        if set(blind_evidence) != {
            "codex_receipts",
            "local_verification_receipts",
        }:
            raise AssertionError("final blind audit received inherited advisor conclusions")
        serialized_blind = json.dumps(blind_evidence, sort_keys=True)
        if any(
            forbidden in serialized_blind
            for forbidden in ("goal_fidelity", "post_audit", "synthesis")
        ):
            raise AssertionError("final blind evidence leaked prior framing")

        def fake_final_role(**kwargs: Any) -> roles.RoleResult:
            if kwargs.get("conversation_key") is not None:
                raise AssertionError("final blind auditor reused a prior conversation")
            raw = final_audit_raw(
                run=run_data,
                projection=projection,
                snapshot=frozen_final,
            )
            report = kwargs["normalize"](raw)
            return roles.RoleResult(
                role="final-blind-auditor",
                report=report,
                metadata={},
                run_dir=kwargs["role_dir"],
                workspace_generation="a" * 24,
                workspace_fingerprint="b" * 64,
                resumed=False,
            )

        with patched(roles, "run_repo_role", fake_final_role):
            goal_research.phase_final_audit(
                project, run_dir, run_data, events, projection, options
            )
        _, events, projection = reload_run(run_dir)
        if projection["phase"] != state.PHASE_COMPLETED:
            raise AssertionError("mocked complete vertical slice did not reach GOAL_COMPLETED")
        if projection["advisor_turns_used"] != 11 or len(events) != 13:
            raise AssertionError(
                f"unexpected bounded run accounting: turns={projection['advisor_turns_used']} "
                f"events={len(events)}"
            )


def test_resume() -> None:
    checkpoint_base = Path("/private/run/checkpoints")
    initial_checkpoint = goal_research.phase_attempt_path(
        checkpoint_base,
        "synthesis",
        events=[],
        phase=state.PHASE_CHALLENGE,
        iteration_id="iteration-0001",
    )
    if initial_checkpoint != checkpoint_base / "synthesis":
        raise AssertionError("initial phase attempt received an unexpected suffix")
    retried_checkpoint = goal_research.phase_attempt_path(
        checkpoint_base,
        "synthesis",
        events=[
            {
                "event_type": "run_resumed",
                "to_state": state.PHASE_CHALLENGE,
                "iteration_id": "iteration-0001",
                "payload": {"new_attempt": True},
            }
        ],
        phase=state.PHASE_CHALLENGE,
        iteration_id="iteration-0001",
    )
    if retried_checkpoint != checkpoint_base / "synthesis-attempt-0002":
        raise AssertionError("explicit resume reused an immutable failed checkpoint")
    pre_submit = roles.GoalResearchRoleError("local wrapper rejected the request")
    goal_research.account_terminal_phase_failure(pre_submit, 3)
    if pre_submit.advisor_turns_attempted != 3:
        raise AssertionError("pre-submission failure lost completed prior advisor turns")
    submitted = roles.GoalResearchRoleError(
        "remote output was malformed", advisor_turns_attempted=1
    )
    goal_research.account_terminal_phase_failure(submitted, 2)
    if submitted.advisor_turns_attempted != 3:
        raise AssertionError("terminal phase failure lost completed advisor turns")

    with fixture_repo() as project:
        run_dir, projection = state.create_run(
            project, goal_contract(), requested_run_id="resume-test"
        )
        blocked = goal_research.block_run(run_dir, projection, "connector unavailable")
        if blocked["phase"] != state.PHASE_BLOCKED:
            raise AssertionError("run did not enter BLOCKED")
        resumed = goal_research.resume_blocked(run_dir, blocked)
        if resumed["phase"] != state.PHASE_GOAL_FROZEN:
            raise AssertionError("resume did not restore the exact blocked phase")

        checkpoint = run_dir / "prompt-recovery"
        checkpoint.mkdir(parents=True)
        paths = roles._prompt_phase_paths(checkpoint)
        prompt = "bounded prompt"
        input_sha = __import__("hashlib").sha256(prompt.encode()).hexdigest()
        request = {
            "schema_version": state.SCHEMA_VERSION,
            "project_dir": str(project),
            "checkpoint_dir": str(checkpoint),
            "input_sha256": input_sha,
            "prompt": prompt + "\nMARKER-RECOVERY-123456789",
            "marker": "MARKER-RECOVERY-123456789",
            "state": str(paths["state"]),
            "journal": str(paths["journal"]),
            "response": str(paths["response"]),
            "chatgpt_project_id": "",
        }
        paths["request"].write_text(json.dumps(request), encoding="utf-8")
        paths["journal"].write_text("{}", encoding="utf-8")
        status_value, _ = roles._recover_prompt_phase(
            project=project,
            checkpoint_dir=checkpoint,
            input_sha256=input_sha,
            timeout=0,
        )
        if status_value != "safe-to-submit":
            raise AssertionError("unsubmitted prompt checkpoint was not safe to submit")

    with fixture_repo() as project:
        run_dir, _ = state.create_run(
            project, goal_contract(), requested_run_id="failed-turn-test"
        )

        def fail_after_submission(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise roles.GoalResearchRoleError(
                "advisor returned malformed JSON",
                advisor_turns_attempted=1,
            )

        handlers = dict(goal_research.PHASE_HANDLERS)
        handlers[state.PHASE_GOAL_FROZEN] = fail_after_submission
        args = argparse.Namespace(
            project_dir=project,
            run_dir=run_dir,
            dry_run=False,
            timeout=0,
            queue_timeout=0,
            max_output_tokens=2_000,
            base_url="http://127.0.0.1:8080/v1",
            no_live_activity=True,
            json=True,
        )
        with patched(goal_research, "PHASE_HANDLERS", handlers):
            exit_code = goal_research.command_advance(args)
        if exit_code != goal_research.EXIT_BLOCKED:
            raise AssertionError("failed submitted advisor turn did not block the run")
        _, events, projection = reload_run(run_dir)
        if projection["advisor_turns_used"] != 1:
            raise AssertionError("failed submitted advisor turn was not charged to the budget")
        if events[-1]["budget_effect"]["advisor_turns"] != 1:
            raise AssertionError("blocked event omitted failed advisor-turn accounting")


def test_completion_audit() -> None:
    goal = goal_contract()
    projection = {
        "acceptance_status": {
            item["id"]: {
                "id": item["id"],
                "status": "passed",
                "evidence_classes": ["codex_local_result"],
            }
            for item in goal["acceptance_dimensions"]
        },
        "goal_clause_status": {
            goal["clauses"][0]["id"]: {"status": "supported"},
            goal["clauses"][1]["id"]: {"status": "regressed"},
        },
        "critical_contradiction_ids": [],
    }
    final = {
        "goal_clause_status": [
            {"id": item["id"], "status": "supported"} for item in goal["clauses"]
        ],
        "acceptance_status": [
            {"id": item["id"], "status": "passed"}
            for item in goal["acceptance_dimensions"]
        ],
        "blocking_findings": [],
        "recommend_completion": True,
    }
    ready, reasons = state.completion_ready(goal, projection, final)
    if ready or not any("critical goal clause" in item for item in reasons):
        raise AssertionError("proxy metric success hid a regressed qualitative goal clause")

    waived_goal = goal_contract()
    waived_goal["waivers"] = [
        {
            "id": "user-waiver-utilization",
            "acceptance_dimension_id": "utilization-control",
            "user_decision": "The user explicitly approved this waiver.",
            "reason": "The qualitative control is deferred from this goal version.",
        }
    ]
    waived_goal = state.validate_goal_contract(waived_goal)
    waived_projection = {
        "acceptance_status": {
            "information-path": {
                "id": "information-path",
                "status": "passed",
                "evidence_classes": ["codex_local_result"],
            },
            "utilization-control": {
                "id": "utilization-control",
                "status": "waived",
                "evidence_classes": [],
            },
        },
        "goal_clause_status": {
            item["id"]: {"status": "supported"} for item in waived_goal["clauses"]
        },
        "critical_contradiction_ids": [],
    }
    waived_final = {
        "goal_clause_status": [
            {"id": item["id"], "status": "supported"}
            for item in waived_goal["clauses"]
        ],
        "acceptance_status": [
            {"id": "information-path", "status": "passed"},
            {"id": "utilization-control", "status": "waived"},
        ],
        "blocking_findings": [],
        "recommend_completion": True,
    }
    ready, reasons = state.completion_ready(
        waived_goal, waived_projection, waived_final
    )
    if not ready or reasons:
        raise AssertionError(f"approved non-invariant waiver did not satisfy completion: {reasons}")


def test_cli() -> None:
    with fixture_repo() as project:
        private = project / ".codex-advisor"
        private.mkdir()
        goal_path = private / "goal-input.json"
        goal_path.write_text(json.dumps(goal_contract()), encoding="utf-8")
        init = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goal_research.py"),
                "init",
                "--project-dir",
                str(project),
                "--goal-file",
                str(goal_path),
                "--run-id",
                "cli-test",
                "--json",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if init.returncode != 0 or json.loads(init.stdout)["phase"] != state.PHASE_GOAL_FROZEN:
            raise AssertionError(f"CLI init failed offline: {init.stdout!r} {init.stderr!r}")
        status_call = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goal_research.py"),
                "status",
                "--project-dir",
                str(project),
                "--run-dir",
                "cli-test",
                "--json",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if status_call.returncode != 0:
            raise AssertionError(
                f"CLI status failed offline: {status_call.stdout!r} {status_call.stderr!r}"
            )
        status_payload = json.loads(status_call.stdout)
        if status_payload["phase"] != state.PHASE_GOAL_FROZEN:
            raise AssertionError("CLI status replay changed phase")
        dry = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goal_research.py"),
                "advance",
                "--project-dir",
                str(project),
                "--run-dir",
                "cli-test",
                "--dry-run",
                "--json",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        dry_payload = json.loads(dry.stdout)
        if dry.returncode != 0 or dry_payload["would_contact_advisor"] is not True:
            raise AssertionError(f"CLI dry-run failed without an advisor: {dry.stderr!r}")
        run_dir = project / ".codex-advisor" / "goal-research-runs" / "cli-test"
        if len(state.read_events(run_dir)) != 1:
            raise AssertionError("CLI dry-run wrote an event")


GROUPS = {
    "state": test_state,
    "roles": test_roles,
    "hypotheses": test_hypotheses,
    "information-path": test_information_path,
    "clean-room": test_clean_room,
    "goal-fidelity": test_goal_fidelity,
    "iteration": test_iteration,
    "resume": test_resume,
    "completion-audit": test_completion_audit,
    "cli": test_cli,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", choices=[*GROUPS, "all"], default="all")
    args = parser.parse_args()
    selected = GROUPS.items() if args.group == "all" else [(args.group, GROUPS[args.group])]
    for name, callback in selected:
        callback()
        print(f"goal-research {name} tests passed")


if __name__ == "__main__":
    main()
