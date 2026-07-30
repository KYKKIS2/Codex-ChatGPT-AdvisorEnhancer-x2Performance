# ExecPlan: Goal Research Mode

This plan follows `AGENTS.md`, `docs/PLAYBOOK.md`, and
`docs/prds/goal-research-mode.md`.

## Purpose / Big Picture

Implement one narrow, complete `goal-research` lane for foreground Codex Goals.
The lane must turn a broad goal into a durable sequence of repository-grounded
claims, bounded advisor challenges, one hypothesis-scoped implementation packet,
Codex verification evidence, and a post-change audit of the fresh current
snapshot.

The target contains two bounded loops. The epistemic loop protects the original
goal, independently remaps the current system, separates information
availability from exploitability, compares competing explanations, and chooses
the next discriminating investigation. The delivery loop gives Codex one
bounded packet, records real local evidence, audits the resulting snapshot, and
returns the result to the epistemic loop.

The implementation is additive. Existing advisor lanes remain unchanged and
continue to own their current transport, routing, safety, connector, and
checkpoint contracts. Repo-aware advisors stay read-only; Codex remains the
only editor and local verifier.

The proof of value is a seeded repository fixture where ordinary tests pass but
useful information is silently discarded before the system can exploit it.
`goal-research` must identify the exact unsupported boundary and propose
discriminating checks without overfitting to one ML codebase.

## Progress

- [x] Inspect current repository guidance, advisor lanes, setup behavior,
  planning conventions, and dirty worktree before preparing this plan.
- [x] Characterize existing contracts and freeze the v1 CLI/artifact schemas.
- [x] Implement the private event log, deterministic state reducer, and guarded
  controller transitions.
- [x] Implement independent repo-aware role orchestration, typed report
  normalization, bounded claim-ID challenge, and synthesis.
- [x] Implement goal-fidelity stewardship, clean-room re-grounding, bounded
  task-specific specialists, and competing-hypothesis memory.
- [x] Implement implementation packets, Codex receipts, post-change audits,
  budgets, completion rules, and resume.
- [x] Add seeded information-adequacy, framing-drift, and negative-control
  fixtures with deterministic offline regressions.
- [ ] Run one live, sanitized, repo-aware vertical-slice benchmark.
- [x] Update only necessary README, skill, and setup/install documentation.
- [x] Run targeted and full regression validation, installed-skill parity, diff
  review, and secret-surface review.

## Surprises & Discoveries

- The existing repo-aware conclave checkpoints independent roles and synthesis,
  but its role contract is free-form review prose. `goal-research` needs its own
  typed artifacts and state reducer without changing that lane.
- `setup.sh` and `setup.ps1` maintain explicit required-file lists. New runtime
  scripts must be added narrowly so installed-skill parity is real rather than
  assumed.
- The current worktree already contains unrelated modifications to `AGENTS.md`,
  README, skill instructions, advisor/conclave/router code, and tests. The Goal
  must preserve those changes and inspect their diffs before touching any
  overlapping documentation.
- A healthy local tunnel does not prove ChatGPT has the DevSpace connector
  attached. Final live acceptance requires the existing exact-conversation MCP
  evidence checks; otherwise the Goal remains blocked.
- Prompt-only challenge and synthesis can reuse existing advisor machinery, but
  the first grounding and post-change audit cannot silently fall back to
  prompt-only mode.
- A rigorous delivery controller can still execute the wrong framing perfectly.
  The full target therefore needs a bounded epistemic layer, not merely stronger
  state integrity around one selected hypothesis.
- `advisor_agent.py` already provides a project-local `--conversation-key`, but
  the initial goal-research orchestration did not pass it. Without that key,
  every iteration directory also created a new ChatGPT conversation and lost
  useful role memory.
- The first live goal-fidelity step failed before submission because the new
  prompt-phase adapter passed `--base-url` to `advisor.py`, whose compatible
  endpoint is an environment-only contract. The narrow fix removed that CLI
  argument, retained `ADVISOR_BASE_URL`, and added a real command-shape
  regression before resuming the same checkpoint.
- The second live attempt proved all core repo-aware roles could inspect the
  exact sanitized snapshot, but natural-language aliases such as `lost`,
  `supporting`, and `unsupported` did not match the intentionally closed schema.
  Role prompts now enumerate every accepted value and explain adjacent terms;
  validators still fail closed instead of coercing aliases.
- A later live specialist invented a full information-path object even though
  temporary specialists do not own that mapping. Specialist prompts now require
  `information_assessment: null`, forbid nested specialist requests, and allow a
  specialist only when one current read-only inspection can close the unknown.
- A valid live synthesis treated evidence gaps that its packet was designed to
  resolve as terminal `blocking_reasons`. The synthesis contract now has two
  exclusive branches: one packet with no terminal blockers, or no packet with at
  least one terminal blocker.
- Turn accounting originally charged wrapper invocations that failed before any
  remote submission. Budgets now count only journal-proven submitted turns and
  preserve completed failures accurately.
- Resume needs two different guarantees. An interrupted or pending submission
  stays in its original checkpoint for GET-only reconciliation. A deliberate
  retry after a visible blocked result uses a numbered immutable attempt path so
  the original request, response, and journal remain auditable.
- A post-change audit cannot make an old contradiction disappear by omission.
  It must classify every prior contradiction as open or resolved, and resolution
  requires fresh current-snapshot claim evidence. The epistemic refresh must
  carry every still-open contradiction forward exactly.

## Decision Log

- Decision: Add a dedicated `goal_research.py` CLI rather than changing default
  router classification in v1.
  Rationale: Explicit selection minimizes compatibility risk and keeps existing
  lanes behaviorally unchanged.
  Date/Author: 2026-07-22 / Codex

- Decision: Keep the first implementation foreground and Codex-driven.
  Rationale: Durable state and evidence gates must be proven before introducing
  detached Codex workers or autonomous background editing.
  Date/Author: 2026-07-22 / Codex

- Decision: Use three fixed core functions, an independent goal-fidelity
  steward, and at most two justified temporary specialists.
  Rationale: The core covers grounding, falsification, and verification; the
  steward protects original intent; bounded specialists add domain depth
  without creating a generalized role framework.
  Date/Author: 2026-07-22 / Codex

- Decision: Use structured artifacts as the only inter-agent communication.
  Rationale: Independent first-round inspection plus one claim-ID challenge
  round preserves provenance and disagreement better than unrestricted chat.
  Date/Author: 2026-07-22 / Codex

- Decision: Treat the append-only event log as authoritative and all status
  files as rebuildable projections.
  Rationale: Resume and completion must not depend on chat history or mutable
  summary files.
  Date/Author: 2026-07-22 / Codex

- Decision: Invalidate repository-grounded approvals conservatively when the
  source snapshot or goal version changes.
  Rationale: Fine-grained semantic freshness is difficult to prove and is not
  needed for the first vertical slice.
  Date/Author: 2026-07-22 / Codex

- Decision: Do not automatically commit, revert, reset, checkout, or clean the
  working tree.
  Rationale: Codex must preserve pre-existing user work and remains responsible
  for implementation decisions.
  Date/Author: 2026-07-22 / Codex

- Decision: Require clean-room re-grounding before the first packet and final
  completion, plus bounded event-triggered remaps.
  Rationale: A fresh reviewer that does not inherit prior conclusions is the
  strongest practical defense against shared framing errors and synthesis
  anchoring.
  Date/Author: 2026-07-22 / Codex

- Decision: Maintain up to three competing explanations and implement only one
  at a time.
  Rationale: A leading, alternative, and null/measurement explanation reduce
  premature commitment while preserving a bounded delivery loop.
  Date/Author: 2026-07-22 / Codex

- Decision: Separate information availability, preservation/representation,
  learnability, utilization, evaluation validity, and causal/operational
  validity.
  Rationale: Merely passing information into a tensor or component does not
  prove that the architecture can exploit it for the real goal.
  Date/Author: 2026-07-22 / Codex

- Decision: Reuse one project-scoped ChatGPT conversation per goal run and
  persistent repo-aware role, while keeping clean-room and final-blind auditors
  on fresh conversations.
  Rationale: Core reviewers and specialists retain useful prior evidence without
  collapsing independent roles into one chat. Run-scoped keys prevent unrelated
  goals from contaminating each other, while fresh blind roles preserve the
  anti-anchoring checks. Iteration-specific recovery journals remain separate
  from conversation state. Role-chat memory remains orientation only; every
  iteration must reopen the exact current sanitized snapshot and produce fresh
  snapshot-bound evidence.
  Date/Author: 2026-07-22 / Codex

## Context And Orientation

Primary guidance:

- `AGENTS.md`
- `docs/PLAYBOOK.md`
- `docs/prds/goal-research-mode.md`

Existing behavior to reuse, not replace:

- `codex-skill/external-advisor/scripts/advisor.py`
- `codex-skill/external-advisor/scripts/conclave.py`
- `codex-skill/external-advisor/scripts/advisor_agent.py`
- `codex-skill/external-advisor/scripts/agent_conclave.py`
- `codex-skill/external-advisor/scripts/verifier_loop.py`
- `codex-skill/external-advisor/scripts/advisor_safety.py`
- `codex-skill/external-advisor/scripts/agent_mode.py`
- `codex-skill/external-advisor/scripts/advisor_concurrency.py`

Likely new implementation files:

- `codex-skill/external-advisor/scripts/goal_research.py`
- `codex-skill/external-advisor/scripts/goal_research_state.py`
- `codex-skill/external-advisor/scripts/goal_research_roles.py`
- `tests/test-goal-research.py`
- `tests/fixtures/goal-research/`

Narrow existing files likely to require edits:

- `README.md`
- `codex-skill/external-advisor/SKILL.md`
- `setup.sh`
- `setup.ps1`

Files that are out of scope unless a verified blocker is recorded first:

- `AGENTS.md`
- `codex-skill/external-advisor/scripts/router.py`
- `codex-skill/external-advisor/scripts/advisor.py`
- `codex-skill/external-advisor/scripts/conclave.py`
- `codex-skill/external-advisor/scripts/advisor_agent.py`
- `codex-skill/external-advisor/scripts/agent_conclave.py`
- `codex-skill/external-advisor/scripts/verifier_loop.py`
- `codex-skill/external-advisor/scripts/g4f_pool.py`
- `patches/`
- `vendor/`

Current friction:

- One-shot advisor reports do not carry a durable goal model across Codex edits.
- The current synthesizer receives prose reports and cannot enforce typed
  claims, freshness, contradiction survival, or completion gates.
- Existing tests can prove implementation consistency without proving that the
  system receives or exploits the information required by the user goal.
- Long chats are not a reliable source of state or memory.
- More agents without independent evidence and bounded communication can amplify
  the same framing error.

## V1 Contracts To Freeze Before Coding

### CLI

Provide one explicit entry point with these minimum operations. Exact argument
spelling may be refined during Milestone 1, but behavior and separation must
remain:

```text
goal_research.py init
goal_research.py advance
goal_research.py record-codex
goal_research.py status
goal_research.py resume
```

- `init` validates and freezes a goal/acceptance contract without calling an
  advisor.
- `advance` performs at most one safe state transition or one bounded advisor
  phase, then exits with machine-readable status.
- `record-codex` records implementation and local-verification receipts but does
  not run hidden project commands or edit source.
- `status` is read-only and reconstructs state from the event log.
- `resume` reconciles a possibly submitted turn in its original checkpoint
  before any new non-idempotent turn. After an explicit blocked result, a safe
  retry uses a numbered immutable checkpoint and preserves the failed attempt.
- All commands support `--project-dir`, explicit run selection, `--json`, and
  deterministic `--dry-run` where meaningful.

### Run Layout

Use a private layout under the existing ignored advisor state:

```text
.codex-advisor/goal-research-runs/<run-id>/
|-- run.json
|-- events.jsonl
|-- status.json
|-- goals/
|   `-- v1.json
|-- acceptance/
|   `-- v1.json
|-- goal-fidelity/
|-- clean-room/
|-- hypotheses.json
|-- iterations/
|   `-- 0001/
|       |-- baseline.json
|       |-- roles/
|       |-- specialist-selection.json
|       |-- claims.json
|       |-- contradictions.json
|       |-- challenge.json
|       |-- synthesis.json
|       |-- implementation-packet.json
|       |-- codex-receipt.json
|       |-- verification-receipt.json
|       |-- post-audit.json
|       `-- outcome.json
|-- final-audit/
`-- report.md
```

`events.jsonl` is authoritative. Other files are immutable artifacts or
rebuildable projections; none may silently replace event history.

### Typed Evidence

At minimum define and validate schemas for:

- goal and acceptance contracts
- goal-fidelity trace and blind completion audit
- run and iteration identifiers
- source and sanitized-workspace snapshots
- role report
- clean-room remap and specialist-selection decision
- claim and evidence reference
- contradiction
- hypothesis record, prediction, and retry condition
- challenge packet and response
- synthesis decision
- implementation packet
- Codex implementation receipt
- local verification receipt
- post-change audit
- iteration outcome
- information-path and exploitability assessment
- append-only event

Evidence must preserve its authority class:

```text
repository_observation
advisor_inference
proposed_experiment
codex_local_result
independent_audit_result
```

Do not add a generalized schema/plugin registry. Use explicit versioned
dataclasses, validators, and JSON serialization sufficient for this mode.

## Plan Of Work

### Milestone 1: Characterize And Freeze Contracts

Work:

- Capture the exact pre-existing `git status --short`, relevant dirty diffs, and
  current test commands before editing.
- Read the current role checkpoint, resume, sanitized workspace, MCP evidence,
  timeout, coordinator, and setup/install code paths.
- Add characterization tests where a shared behavior is not already covered.
- Freeze the CLI, two-loop state machine, schemas, state layout, exit-status
  semantics, core/specialist role budgets, remap triggers, hypothesis limits,
  iteration budgets, and terminal conditions in code-level test fixtures before
  implementing orchestration.
- Confirm the controller requires Git for v1 but supports a pre-existing dirty
  baseline without destructive cleanup.
- Record any required deviation from the PRD in this plan before coding it.

Validation:

- `git status --short`
- Inspect the existing diff for every currently modified file before overlapping
  it. Use `git diff -- <paths...>` with the paths reported by
  `git status --short`.
- `python3 codex-skill/external-advisor/scripts/advisor_agent.py --help`
- `python3 codex-skill/external-advisor/scripts/agent_conclave.py --help`
- `python3 codex-skill/external-advisor/scripts/conclave.py --help`

Exit gate:

- Tests encode the new contracts without modifying existing lane behavior.
- No unresolved product or safety decision is hidden in implementation code.

### Milestone 2: Event Log And Deterministic State Machine

Work:

- Add focused `goal_research_state.py` primitives for IDs, schema validation,
  private paths, atomic artifact writes, event append, digest chaining, replay,
  projection, transition guards, and conservative invalidation.
- Add `goal_research.py init`, `status`, and offline `advance` behavior.
- Make initialization freeze goal version 1, acceptance requirements, budgets,
  source baseline identity, and the first event without contacting ChatGPT.
- Make state replay reject gaps, duplicates, invalid digests, malformed JSON,
  impossible transitions, mutually exclusive outcomes, stale packet IDs, and
  incompatible schema versions.
- Ensure repeated accepted inputs are idempotent and cannot consume budgets
  twice.
- Ensure source changes and goal amendments invalidate downstream artifacts and
  never mutate old versions.
- Add deterministic transitions for goal-fidelity checks, clean-room remaps,
  hypothesis lifecycle, specialist selection, epistemic refresh, and blind
  completion audit without introducing a generic workflow engine.

Validation:

- Compile `goal_research.py` and `goal_research_state.py` with
  `python3 -m py_compile`.
- `python3 tests/test-goal-research.py --group state`
- `git diff --check`

Exit gate:

- A run can be initialized, replayed, projected, status-queried, amended, and
  failed closed entirely offline.
- Event corruption and stale-artifact cases are covered by deterministic tests.

### Milestone 3: Epistemic Grounding, Hypotheses, And Bounded Challenge

Work:

- Add `goal_research_roles.py` with three fixed core role contracts:
  cartographer, falsifier, and verifier.
- Add an independent goal-fidelity steward that maps original goal clauses to
  acceptance, hypotheses, packets, and evidence without rewriting the goal.
- Add a bounded temporary-specialist catalog. Permit at most two specialists
  only when each has an explicit unresolved question, expected evidence,
  rationale, and stopping condition.
- Invoke existing repo-aware wrappers rather than constructing compatible API
  requests directly.
- Give first-round roles the same frozen goal and snapshot but no other role's
  report.
- Require exact-conversation MCP evidence and capture returned workspace
  generation/fingerprint in each role artifact.
- Parse and validate strict JSON role outputs. Keep raw verified response paths
  private for diagnosis, but normalize only typed evidence into controller
  state.
- Build stable claim IDs, contradiction IDs, evidence links, and unknowns.
- Build at most three active hypothesis records for each unresolved decision:
  leading, credible alternative, and null/measurement explanation. Record
  predictions, falsifiers, evidence for/against, and retry conditions.
- Reject semantically repeated failed hypotheses unless a recorded condition
  changed. Use explicit normalized fields and bounded advisor review rather than
  adding embeddings or a generalized semantic database.
- Select the cheapest investigation that discriminates between active
  explanations before synthesis recommends implementation when practical.
- Run at most one coordinator-generated, claim-ID prompt-only challenge round.
  Do not expose repo paths, secrets, or raw private state unnecessarily.
- Run prompt-only synthesis only when required roles and challenge artifacts are
  valid. Preserve unresolved contradictions and refuse prose-only advancement.
- Reuse existing queue, model, timeout, transcript-recovery, and checkpoint
  behavior. Never post directly to `/v1/chat/completions`.

Validation:

- `python3 -m py_compile codex-skill/external-advisor/scripts/goal_research_roles.py`
- `python3 tests/test-goal-research.py --group roles`
- `python3 tests/test-goal-research.py --group hypotheses`
- `./tests/test-agent-conclave.sh`
- `python3 ./tests/test-prompt-conclave-orchestration.py`

Exit gate:

- Offline mocked runs prove role independence, schema rejection, exact one-round
  challenge limits, contradiction preservation, all-failed handling, checkpoint
  reuse, and no silent prompt-only fallback for grounding.
- Tests prove clause traceability, bounded specialist selection, no-specialist
  behavior, three-hypothesis limits, novelty checks, and retry conditions.

### Milestone 4: Clean-Room Mapping, Information Adequacy, And Benchmarks

Work:

- Add compact positive and negative fixtures under
  `tests/fixtures/goal-research/`.
- Keep fixtures small and domain-neutral enough to test the defect class rather
  than implement a production Transformer.
- Implement mandatory clean-room remaps before the first packet and final
  completion. Give the remapper the original goal and current snapshot, but no
  inherited reports, hypothesis labels, or synthesis recommendation.
- Implement bounded remap triggers for repeated related failures, material
  architecture/scope changes, critical contradictions, and goal amendments.
- Represent raw fields, filters, feature construction, representation boundary,
  order/masking/truncation/defaults, processing/model stages,
  pooling/compression, outputs, loss/decision, and evaluation.
- Require every relevant field family and stage to be classified as retained,
  transformed, aggregated, justified exclusion, unavailable, or unexplained.
- Assess source availability, pipeline preservation,
  representation/distinguishability, learnability, utilization, evaluation
  validity, and causal/operational validity as separate evidence dimensions.
- Seed positive variants for dropped source information, identity/order/relation
  loss, destructive compression, weak supervision/unlearnability,
  present-but-unused inputs, objective mismatch, long-context failure,
  shortcut/leakage, and goal-proxy drift while keeping normal fixture tests
  green.
- Seed a justified negative exclusion that must not become a critical finding.
- Seed inherited false framing that a clean-room remap must recover, a metric
  improvement that violates an original qualitative clause, competing causal
  explanations, and a semantically repeated failed hypothesis.
- Ensure the detector cannot pass through a generic warning: it must reference
  exact fixture evidence, identify the boundary, classify the missing
  information, and propose discriminating tests.

Validation:

- `python3 tests/test-goal-research.py --group information-path`
- `python3 tests/test-goal-research.py --group clean-room`
- `python3 tests/test-goal-research.py --group goal-fidelity`
- Run the fixtures' ordinary tests and confirm both positive and negative
  fixtures remain green before `goal-research` analysis.
- `git diff --check`

Exit gate:

- Deterministic tests prove typed path/exploitability assessments, fidelity
  traces, clean-room triggers, and acceptance gates.
- Positive fixtures classify the correct failure layer; negative controls avoid
  false critical findings.
- Clean-room and goal-proxy fixtures prove that inherited framing and improving
  metrics cannot bypass the original goal.

### Milestone 5: Codex Boundary, Post-Change Audit, And Completion

Work:

- Generate exactly one hypothesis-scoped implementation packet from validated
  synthesis.
- Enter explicit waiting-for-Codex and waiting-for-local-verification states.
- Add `record-codex` receipt validation bound to goal version, iteration,
  packet, baseline, current checkout, changed paths, commands, exit statuses,
  and retained evidence.
- Preserve pre-existing dirty changes and distinguish them from the bounded
  iteration delta. Do not auto-clean, commit, or revert.
- Refresh the sanitized generation after Codex changes and require the stable
  post-change role to inspect that fresh current snapshot with new MCP evidence.
- Close the iteration with one mutually exclusive outcome and update acceptance
  evidence.
- Return every outcome to the epistemic loop. Update hypothesis support,
  contradictions, novelty memory, goal-fidelity coverage, and the next
  discriminating question before another packet can be emitted.
- Permit another iteration only after the current one is closed and budget
  remains.
- Before overall completion, run a blind repo-aware auditor with the original
  goal, constraints, final snapshot, and raw local evidence but without the
  current synthesis recommendation. Normalize its findings independently.
- Enforce overall completion separately from iteration acceptance. Require
  complete goal-clause traceability, fresh acceptance evidence, no critical
  contradiction, and a passing blind completion audit.
- Implement resume/reconciliation from every durable boundary without replaying
  ambiguous advisor submissions, and preserve each explicit retry as a numbered
  immutable attempt.

Validation:

- `python3 tests/test-goal-research.py --group iteration`
- `python3 tests/test-goal-research.py --group resume`
- `python3 tests/test-goal-research.py --group completion-audit`
- `./tests/test-agent-mode.sh`
- `./tests/test-agent-conclave.sh`
- `./tests/test-security-regressions.sh`

Exit gate:

- Offline tests cover waiting states, receipt mismatch, dirty-baseline
  preservation, snapshot invalidation, budget exhaustion, terminal exclusivity,
  epistemic refresh, post-audit rejection, omitted-goal-clause rejection,
  acceptance-matrix closure, blind completion audit, and resume.

### Milestone 6: Live Vertical Slice

Work:

- Verify the current repository connector with
  `advisor_agent_connect.py status`.
- If the current URL is not attached and fail-closed agent mode is not ready,
  start/refresh the connector, present the MCP URL to the user, and stop until
  the user confirms attachment. Do not edit ChatGPT account settings.
- Initialize one goal-research run against the seeded positive fixture.
- Advance through goal-fidelity tracing, independent repo-aware grounding,
  clean-room remapping, competing explanations, justified specialist selection
  when required, bounded challenge, synthesis, one no-risk fixture-scoped Codex
  change or receipt, local fixture tests, post-change audit, epistemic refresh,
  and blind completion audit.
- Confirm live role artifacts carry exact workspace/MCP evidence and the
  expected snapshot fingerprint.
- Confirm the unsupported information boundary is grounded and the controller
  classifies whether the defect is availability, preservation/representation,
  learnability, utilization, evaluation, or causal/operational validity. It
  must not claim production-model improvement.
- Confirm a goal-proxy or omitted-clause fixture cannot reach completion despite
  passing local tests or improving its numeric metric.
- Exercise one interruption/resume boundary using GET-only recovery; do not
  intentionally duplicate an accepted ChatGPT turn.

Validation:

- Record exact commands and private run artifact paths in this plan's
  `Outcomes & Retrospective` without copying secrets, conversation IDs, raw
  prompts, or private repo contents into tracked files.
- `python3 codex-skill/external-advisor/scripts/goal_research.py status --project-dir . --run-dir <run-dir> --json`
- Re-run the fixture's local tests after the vertical slice.

Exit gate:

- One real sanitized repo-aware run reaches a valid iteration outcome and all
  exact-conversation evidence checks pass.
- Connector unavailability remains a blocker; it is not waived through mocked
  or prompt-only evidence.

### Milestone 7: Documentation And Installed-Skill Parity

Work:

- Add a concise fifth-lane section to README covering purpose, selection,
  epistemic/delivery loops, core and temporary roles, goal-fidelity and
  clean-room gates, hypothesis limits, lifecycle, state, budgets, read-only
  advisor boundary, Codex edit boundary, resume, and limitations.
- Update `codex-skill/external-advisor/SKILL.md` so future Codex sessions know
  when to use `goal-research` and when to use existing one-shot lanes.
- Add only the new required runtime scripts to `setup.sh` and `setup.ps1`
  completeness checks.
- Do not modify router defaults, global `~/.codex/AGENTS.md`, repo `AGENTS.md`,
  runtime patches, or vendor code for documentation convenience.
- Run `./setup.sh` only after repository validation is green, preserving
  `advisor-config.json` and other local runtime state.
- Verify repository and installed copies of every new or changed skill file.

Validation:

- `./setup.sh`
- Use `cmp` to compare each repository `goal_research*.py` file with its copy
  under `~/.codex/skills/external-advisor/scripts/`.
- `cmp codex-skill/external-advisor/SKILL.md ~/.codex/skills/external-advisor/SKILL.md`
- Inspect changed README and skill sections directly.
- `git diff --check`

Exit gate:

- New Codex sessions receive the implemented scripts and correct usage guidance.
- Setup changes contain no unrelated refactor.

### Milestone 8: Final Regression And Scope Audit

Work:

- Run the new test suite and the documented fast Linux advisor regressions.
- Run changed-file Python compilation.
- Review exact diff and staged/intended surface for secrets and private runtime
  artifacts.
- Confirm no existing dirty user change was reverted or accidentally absorbed.
- Confirm no `.codex-advisor`, HAR, cookie, auth, connector, DevSpace password,
  transcript, `.env`, key, vendor, cache, or generated fixture runtime file is
  tracked.
- Remove generated `__pycache__`, `*.pyc`, temporary run outputs, logs, and test
  workspaces created inside the repository.
- Update this plan's progress, discoveries, decision log, and outcomes with
  exact validation results.
- Do not commit, push, deploy, or publish; those require a separate explicit
  user request.

Validation:

```bash
python3 tests/test-goal-research.py
./tests/test-router.sh
./tests/test-context-pack.sh
./tests/test-verifier-loop.sh
./tests/test-advisor-transport-recovery.sh
python3 ./tests/test-prompt-transport.py
python3 ./tests/test-prompt-conclave-orchestration.py
./tests/test-advisor-live-activity.sh
./tests/test-advisor-concurrency.sh
./tests/test-security-regressions.sh
./tests/test-agent-mode.sh
./tests/test-agent-conclave.sh
./tests/test-memory.sh
./tests/test-ranking.sh
./tests/test-eval-harness.sh
python3 -m py_compile \
  codex-skill/external-advisor/scripts/goal_research.py \
  codex-skill/external-advisor/scripts/goal_research_state.py \
  codex-skill/external-advisor/scripts/goal_research_roles.py
git diff --check
git status --short
```

Exit gate:

- Every PRD acceptance criterion is mapped to passing evidence or an explicit
  user-approved waiver.
- No unresolved critical contradiction remains.
- Every original goal clause has fresh traceable evidence, and the blind final
  audit independently agrees that completion is supported.
- Existing lanes retain their documented behavior.
- The new mode is installed and one live seeded vertical slice passed.

## Risks And Rollback

- Risk: The feature grows into a generic orchestration framework.
  Mitigation: Fixed core contracts, at most two catalog specialists, explicit
  schemas, one challenge round, two fixed foreground loops, and no dynamic
  plugins or background workers.

- Risk: Multiple agents produce correlated prose instead of independent
  evidence.
  Mitigation: Isolated first-round repo-aware conversations, typed reports,
  stable claim IDs, clean-room remaps, explicit contradictions, and one bounded
  challenge round.

- Risk: The goal-fidelity steward becomes a second synthesizer or rewrites the
  user's intent.
  Mitigation: Restrict it to clause traceability and drift findings; only the
  user/Codex contract flow can version the goal or grant a waiver.

- Risk: Temporary specialists duplicate core roles or expand scope.
  Mitigation: Require one unresolved question, expected evidence, rationale,
  stopping condition, and a hard maximum of two specialists.

- Risk: Hypotheses accumulate or failed ideas are repeated indefinitely.
  Mitigation: Maximum three active explanations, explicit lifecycle/retry
  conditions, normalized novelty checks, and budgeted escalation.

- Risk: Clean-room reviews are anchored by inherited labels or run too often.
  Mitigation: Withhold prior conclusions and synthesis, require first/final
  remaps, and permit only specified event-triggered remaps.

- Risk: The seeded benchmark is passed by generic warnings.
  Mitigation: Require exact evidence, stage/field classification, an unsupported
  loss boundary, discriminating tests, and a justified negative fixture.

- Risk: Stale evidence is reused after a source or goal change.
  Mitigation: Bind every artifact to goal and snapshot IDs and invalidate
  conservatively in v1.

- Risk: Event state becomes contradictory or unrecoverable.
  Mitigation: Append-only sequence/digest validation, deterministic reducer,
  immutable artifacts, projection rebuilds, idempotency, and corruption tests.

- Risk: Codex or an advisor exceeds its authority.
  Mitigation: Existing read-only sanitized MCP tools, no advisor shell/mutation,
  explicit waiting states, receipts, and no controller Git mutation.

- Risk: Existing advisor behavior regresses through shared-module changes.
  Mitigation: Prefer new modules, prohibit router/default changes, document any
  unavoidable shared change before editing, and run the full regression suite.

- Risk: Existing dirty work is overwritten.
  Mitigation: Capture initial status/diffs, patch overlapping docs narrowly,
  never reset/revert/checkout/clean, and compare final changes against the
  recorded baseline.

- Risk: A live connector is unavailable during final validation.
  Mitigation: Stop in a documented blocked state and request connector
  attachment; do not substitute prompt-only or mocked success.

Rollback:

- Remove the new `goal_research*` scripts, tests/fixtures, README/skill section,
  and narrow setup required-file entries.
- Preserve all existing advisor scripts, lanes, state, project bindings,
  connector configuration, and unrelated worktree changes.
- Do not rewrite Git history or delete local `.codex-advisor` runs during
  rollback; archive or leave private diagnostic state for the user.

## Outcomes & Retrospective

The implementation adds `goal_research.py`, `goal_research_state.py`, and
`goal_research_roles.py`, with deterministic fixtures in
`tests/fixtures/goal-research/`, an end-to-end offline regression in
`tests/test-goal-research.py`, and narrow installation/documentation updates.
The v1 CLI remained explicit rather than entering default router selection.
Artifacts are immutable and event-sourced; state is rebuilt from the event log,
and every remote phase has a checkpoint and a bounded resume contract.

The positive fixture preserves passing ordinary tests while discarding useful
pipeline information; the controller's information-path audit identifies that
loss. The negative fixture justifies its exclusion and remains a true negative.
Goal-fidelity, closed-vocabulary normalization, hypothesis bounds, challenge,
Codex receipts, stale-snapshot rejection, post-change contradiction accounting,
completion gates, and interrupted-turn recovery all pass deterministic tests.

Validation completed across the goal-research suite and the existing advisor
concurrency, transport, router, agent, conclave, verifier, and security
regressions. Python, Node, and Bash syntax checks pass. The source and installed
skill were compared directly after synchronization. The public candidate was
also scanned as a working tree and staged index, including exact local private
values and high-confidence secret patterns.

One full live sanitized ChatGPT vertical slice remains intentionally unchecked
above. Individual live phases exposed and fixed real prompt-shape, schema,
specialist-boundary, accounting, and recovery defects, but this plan does not
claim an uninterrupted final live completion without preserving that evidence.

Final decision: **GO for explicit, controlled `goal-research` use** with Codex as
the editor and verifier. Keep it opt-in, retain the existing fail-closed
repo-aware evidence checks, and do not make it a default router lane until a
complete live vertical slice is recorded. Windows supports prompt-only lanes;
sanitized repo-aware generation and the permanent domain MCP remain Linux-only.
