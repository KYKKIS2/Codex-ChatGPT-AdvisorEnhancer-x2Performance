# PRD: Goal Research Mode

## Problem

The current advisor lanes are effective for focused critique and one-shot
repository review, but they do not maintain a durable, evidence-grounded model
of a broad implementation goal across multiple Codex changes.

This creates a failure mode where the implementation is internally correct and
its existing tests pass, yet it does not satisfy the real goal. For example, a
data or ML pipeline can silently discard identity, ordering, relative-time,
relational, or long-history information before a model sees it. Reviewers can
spend time tuning labels, losses, or architecture without first proving what
information survives the complete raw-data-to-evaluation path.

Adding more unstructured agents does not solve this. Independent agents can
share the same framing error, lose prior discoveries in long conversations,
or synthesize away important disagreement. The missing capability is a bounded
controller that preserves verified claims, contradictions, decisions, and
acceptance evidence independently of chat memory.

## Goal

Add an opt-in `goal-research` mode to the installed `external-advisor` skill.
The mode must guide a foreground Codex Goal through bounded, repeatable
implementation iterations while keeping repository-aware ChatGPT advisors
mechanically read-only and Codex as the only editor and local command runner.

The first complete vertical slice must:

- freeze a versioned goal contract and multidimensional acceptance matrix
- independently trace every original goal clause through acceptance criteria,
  hypotheses, implementation packets, and final evidence
- ground the goal in a specific repository and sanitized workspace snapshot
- collect independent, structured repository evidence from a small fixed set
  of repo-aware advisor functions
- add at most two bounded task-specific specialists when an explicit unresolved
  question cannot be answered by the core functions
- preserve typed claims, uncertainty, and contradictions in an append-only run
  history
- maintain a bounded portfolio containing a leading explanation, a credible
  alternative, and a null or measurement explanation
- perform clean-room repository remapping before the first implementation,
  after defined drift/failure triggers, and before overall completion
- permit exactly one coordinator-mediated, claim-ID challenge round
- produce one hypothesis-scoped implementation packet for Codex
- wait explicitly for Codex implementation and local verification evidence
- independently inspect the post-change snapshot
- close the iteration as `accepted`, `rejected`, `inconclusive`, or
  `escalated`
- complete the overall goal only when every required acceptance dimension has
  fresh evidence and no critical contradiction remains unresolved

The mode must catch a seeded missing-information-path defect without being
special-cased to one ML repository or passing through generic warnings.

## Users / Stakeholders

- Primary user: a developer running a long foreground Codex Goal that benefits
  from deeper repository-aware reasoning.
- Primary orchestrator and editor: Codex.
- Read-only reviewers: ChatGPT advisor agents using the existing sanitized
  DevSpace/MCP path.
- Maintainers: future Codex sessions evolving the `external-advisor` skill.

## Terminology

- **Goal contract**: immutable versioned statement of outcome, constraints,
  non-goals, risks, acceptance dimensions, and budgets for one run.
- **Snapshot**: exact source-checkout and sanitized-workspace identities used
  to ground an artifact.
- **Claim**: typed statement with provenance, confidence, and support or
  contradiction links.
- **Evidence**: one of repository observation, advisor inference, proposed
  experiment, Codex local result, or independent audit result.
- **Implementation packet**: one bounded hypothesis, rationale, permitted
  scope, required checks, expected signals, rejection criteria, and unresolved
  risks for Codex.
- **Goal-fidelity trace**: mapping from each original goal clause to acceptance
  criteria, active hypotheses, implementation work, and current evidence.
- **Clean-room remap**: independent inspection that receives the original goal
  and current snapshot but not inherited conclusions, role reports, or the
  current synthesis recommendation.
- **Hypothesis record**: stable explanation with predictions, evidence for and
  against, status, discriminating checks, and explicit retry conditions.
- **Iteration outcome**: `accepted`, `rejected`, `inconclusive`, or
  `escalated`. An accepted iteration is not the same as a completed goal.
- **Run projection**: replaceable current-state summary derived from the
  append-only event log. It is not authoritative state.

## Required User Flow

1. Codex creates or selects a goal contract and acceptance matrix for a Git
   repository.
2. An independent goal-fidelity steward validates that every original clause is
   represented and that no proxy metric or implementation convenience has
   silently replaced the requested outcome. It cannot rewrite the contract.
3. `goal-research` validates the contract, budgets, repository identity,
   connector readiness, and private state location before any repo-aware turn.
4. Three independent repo-aware core functions inspect the same frozen goal and
   snapshot without seeing each other's conclusions:
   - cartographer: maps the implemented system and information/data/control
     paths relevant to the goal
   - falsifier: searches for missing inputs, invalid assumptions, proxy
     metrics, leakage, and ways existing tests can pass while the goal fails
   - verifier: maps acceptance dimensions to evidence and discriminating local
     checks
5. A clean-room cartography pass independently rebuilds the problem/system map
   from the original goal and current repository before the first packet.
6. The controller may select at most two temporary task-specific specialists,
   but only when each is justified by an explicit unresolved question, expected
   evidence, and reason the core functions are insufficient.
7. Their outputs are validated and normalized into typed claims, evidence,
   contradictions, proposed checks, and unknowns.
8. The controller maintains at most three active competing explanations and
   selects the cheapest discriminating investigation before implementation.
9. The controller runs one bounded prompt-only challenge round over explicit
   claim IDs. Agents do not converse directly and cannot open an unbounded
   debate.
10. A prompt-only synthesis preserves unresolved contradictions and emits one
   implementation packet. Prose alone cannot advance the controller.
11. The controller enters an explicit waiting state. Codex inspects the original
   checkout, makes only the packet's hypothesis-scoped change, and runs the
   required local checks.
12. Codex records a structured implementation and verification receipt. The
   controller independently captures the changed snapshot and validates that
   the receipt matches the packet and current checkout.
13. A fresh repo-aware post-change reviewer inspects the actual resulting
   snapshot and evidence rather than only the original plan.
14. Results return to the epistemic loop, updating hypotheses, contradictions,
    goal-fidelity coverage, and the research ledger before another packet.
15. Before completion, a blind repo-aware auditor starts from the original goal
    and final snapshot without seeing the synthesis recommendation, then checks
    for omitted clauses, inherited framing errors, and unsupported completion.
16. The controller records the iteration outcome, updates the acceptance
    matrix, and either creates the next bounded iteration, completes the goal,
    or stops in a visible blocked/escalated state.

## Requirements

### Entry Point And Compatibility

- Add a dedicated `scripts/goal_research.py` entry point with explicit
  subcommands for initialization, advancing one safe controller step,
  recording Codex implementation/verification evidence, status, and resume.
- The mode must be explicitly selected. Do not change the current default
  `router.py` behavior in v1.
- Missing, invalid, or absent `goal-research` selection must leave all existing
  advisor lanes unchanged.
- The new controller must reuse existing advisor wrappers, coordinator/FIFO,
  disposable workers, model policy, transport recovery, project binding,
  sanitized workspace generation, MCP evidence verification, timeout rules,
  and checkpoint/resume behavior through narrow interfaces.
- Do not duplicate or replace `advisor.py`, `conclave.py`, `advisor_agent.py`,
  `agent_conclave.py`, `verifier_loop.py`, or `router.py`.
- If the verified repo-aware connector is unavailable, stale, or unsafe, the
  run must stop in a clear blocked state. It must not silently downgrade a
  grounding or post-change inspection to prompt-only advice.

### Goal And Acceptance Contract

- A goal contract must include a schema version, goal ID and version, objective,
  non-goals, constraints, allowed implementation scope, acceptance dimensions,
  required evidence, iteration budget, advisor-turn budget, and escalation
  conditions.
- Goal initialization must fail closed when required acceptance criteria are
  missing, contradictory, or not verifiable.
- Goal amendments create a new version. They never mutate an accepted goal
  version in place and must invalidate downstream packets, approvals, and
  reports that were bound to the old version.
- Acceptance must support hard invariants, quantitative checks, qualitative
  repository evidence, unresolved unknowns, and explicit waivers.
- A waiver must identify the user decision and affected acceptance dimension;
  an advisor cannot grant one.

### Epistemic And Delivery Loops

- The controller must keep two explicit loops rather than treating research and
  implementation as one undifferentiated sequence.
- The epistemic loop maintains goal fidelity, clean-room system maps,
  information adequacy, competing hypotheses, contradictions, specialists, and
  the next cheapest discriminating investigation.
- The delivery loop issues one Codex packet, records implementation and local
  evidence, performs a fresh post-change audit, and returns its results to the
  epistemic loop.
- A delivery result cannot silently rewrite the original goal, retire a
  contradiction, or become accepted evidence outside its bound hypothesis and
  snapshot.
- The epistemic loop must be revisited after every iteration, even when local
  tests pass, so the system asks whether the new evidence changes the problem
  framing rather than only whether the patch worked as specified.

### Goal Fidelity And Clean-Room Re-grounding

- Maintain a goal-fidelity trace from every original goal clause to acceptance
  criteria, active hypotheses, packets, implementation receipts, and fresh
  evidence.
- A goal-fidelity steward must be independent of the main synthesizer and may
  flag drift, omission, proxy substitution, or incompatible acceptance rules.
  It cannot silently amend the goal or grant a waiver.
- Clean-room remaps receive the original goal, declared constraints, and exact
  current snapshot, but no prior role conclusions, synthesis recommendation,
  implementation packet, or accepted hypothesis labels.
- A clean-room remap is mandatory before the first implementation packet and
  before overall completion.
- An additional remap is triggered after two related rejected/inconclusive
  iterations, a material architecture or allowed-scope change, a critical
  unresolved contradiction, or explicit goal amendment.
- Remapping is bounded. Repeated unchanged remaps do not create a loop; their
  findings are normalized once and either change the hypothesis set, record
  agreement, or escalate.
- Overall completion requires a final blind repo-aware audit that starts from
  the original goal and final snapshot and does not see the current synthesis
  recommendation before producing its own findings.

### Event-Sourced State

- Store private run state under
  `.codex-advisor/goal-research-runs/<run-id>/` with private permissions and
  atomic writes.
- Keep one append-only, sequence-checked event log as authoritative state.
- Every event must include schema version, event ID, sequence number, previous
  event digest, run ID, goal version, iteration ID when applicable, prior and
  next state, actor, snapshot IDs, referenced artifact IDs, budget effects,
  timestamp, and failure reason when applicable.
- Derive `status.json` or equivalent projections from the event log. Deleting
  or corrupting a projection must be recoverable; deleting, truncating,
  contradicting, or corrupting authoritative events must fail closed.
- Replaying the same accepted input must be idempotent. Unknown submission
  outcomes must be reconciled through existing GET-only recovery before any
  advisor turn is repeated.
- Resume must reject changed goal versions, incompatible schemas, unexpected
  repository identity, stale workspace bindings, duplicate events, event gaps,
  or mutually exclusive terminal states.

### Evidence And Freshness

- Every role report and normalized artifact must be bound to run ID, goal
  version, iteration ID, original-checkout fingerprint, sanitized workspace
  generation/fingerprint, and artifact schema version.
- Evidence classes must remain distinct:
  - repository observation
  - advisor inference
  - proposed experiment
  - Codex local result
  - independent audit result
- Repository observations must identify inspected paths and line or symbol
  locations when available. Advisor inferences must not be promoted to verified
  repository facts.
- V1 must use conservative snapshot invalidation: after a source change, old
  repository-grounded reports cannot approve the new snapshot. Selective
  semantic reuse is deferred.
- Contradictions are first-class artifacts with stable IDs and links to the
  conflicting claims. Synthesis, resume, and reporting must not erase them.
- No controller transition may depend on conversational memory that is absent
  from the durable artifact bundle.

### Bounded Advisor Collaboration

- V1 always uses three core epistemic functions: cartographer, falsifier, and
  verifier, plus an independent goal-fidelity steward at required gates. These
  functions are explicit contracts, not a generic plugin framework.
- Initial reports must be independent and use isolated repo-aware advisor
  conversations.
- The controller may add zero, one, or two temporary specialists from a bounded
  catalog such as data/ML/causality, architecture/integration,
  security/reliability, performance, or domain workflow. Each selection must
  name one unresolved question, expected evidence, selection rationale, and
  stopping condition.
- Specialists cannot expand the goal, implementation scope, or call budget.
  Their reports use the same evidence and snapshot contracts as core roles.
- Tests must include both a specialist-needed case and a case where specialist
  selection is unjustified and therefore rejected.
- Role output must follow a strict machine-readable schema. Malformed,
  incomplete, ungrounded, or tool-unverified output fails that role.
- The challenge round is coordinator-mediated and addresses explicit claim IDs.
  It is executed at most once per iteration and is prompt-only over bounded,
  sanitized artifacts.
- Agents may not send arbitrary prompts directly to each other, create nested
  conclaves, or continue debate after the bounded round.
- Synthesis is prompt-only and may recommend a packet only from validated
  artifacts. It cannot claim new repository inspection.
- Default call and iteration budgets must be conservative and explicit. Budget
  exhaustion produces an `escalated` or blocked outcome, never an endless loop.

### Hypothesis And Research Memory

- Keep at most three active explanations per unresolved decision: a leading
  hypothesis, a credible alternative, and a null or measurement explanation.
- Every hypothesis has a stable ID, mechanism, predicted observations,
  falsifying observations, supporting/opposing evidence IDs, status, and
  explicit retry conditions.
- The controller selects the cheapest investigation that discriminates between
  active explanations before issuing an implementation packet when practical.
- Only one hypothesis-scoped Codex change may be active at a time. Competing
  explanations do not create parallel implementation branches.
- Preserve rejected, retired, inconclusive, and supported hypotheses in the
  event history. Do not call a hypothesis proven merely because one test passed.
- Before accepting a new hypothesis, compare its normalized mechanism,
  predictions, and evidence target with prior attempts. A semantically repeated
  failed idea is rejected unless a recorded repository, data, assumption, or
  experimental condition changed enough to satisfy its retry conditions.
- Research memory is run-local and artifact-based. Cross-run semantic memory,
  hidden chat recall, and automatic reuse of old conclusions remain out of
  scope.

### Implementation And Verification Boundary

- Codex remains the only component allowed to edit the original checkout or
  run local project commands.
- An implementation packet represents one hypothesis and verification plan,
  not an arbitrary line-count or file-count limit.
- The packet must include permitted scope, forbidden scope, evidence basis,
  required checks, expected signals, rejection criteria, rollback guidance,
  and open contradictions.
- The controller must preserve pre-existing dirty worktree changes. It may
  capture and compare them, but it must not reset, checkout, commit, revert,
  delete, or overwrite user work automatically.
- Codex implementation and verification receipts must reference the packet,
  baseline snapshot, changed paths, commands run, exit results, and retained
  evidence artifacts.
- A fresh post-change repo-aware audit is required before an iteration can be
  accepted.
- Rejected or inconclusive iterations are recorded but not automatically
  reverted. Codex or the user decides how to handle the working tree.

### State Transitions And Completion

The minimum guarded flow is:

```text
GOAL_FROZEN
-> GOAL_FIDELITY_CHECK
-> CLEAN_ROOM_GROUNDING
-> HYPOTHESIS_SET_READY
-> CHALLENGE
-> PACKET_READY
-> WAITING_FOR_CODEX
-> WAITING_FOR_LOCAL_VERIFICATION
-> POST_CHANGE_AUDIT
-> ITERATION_CLOSED
-> EPISTEMIC_REFRESH
-> NEXT_ITERATION | FINAL_CLEAN_ROOM_AUDIT | BLOCKED
-> GOAL_COMPLETED | BLOCKED
```

- Only the controller may append transition events.
- Every transition must validate required artifacts, identifiers, budgets,
  snapshot freshness, and the allowed prior state.
- `accepted`, `rejected`, `inconclusive`, and `escalated` are mutually exclusive
  iteration outcomes.
- `GOAL_COMPLETED` requires fresh evidence for every required acceptance
  dimension, all hard invariants passing, no unresolved critical contradiction,
  complete goal-clause traceability, a passing blind final audit, budgets and
  stopping rules evaluated, and any waivers explicitly approved by the user.
- Metric improvement, passing existing tests, advisor consensus, or an accepted
  iteration alone must never complete the goal.

### Information-Path And Exploitability Grounding

- The cartographer schema must support a machine-readable path through:

```text
raw source
-> selection/filtering
-> feature or representation construction
-> tensor/message/request boundary
-> masking/truncation/order/defaults
-> model or processing components
-> aggregation/pooling/compression
-> outputs/heads
-> loss or decision logic
-> evaluation
```

- Every relevant raw field family, relationship, and pipeline stage must be
  classified as `retained`, `transformed`, `aggregated`,
  `excluded_with_justification`, `unavailable`, or `unexplained`.
- `unexplained` required information blocks packet acceptance until resolved or
  explicitly escalated.
- The manifest must identify collisions, cardinality collapse, masking,
  truncation, defaults, ordering loss, identity loss, relational loss,
  compression ratios, train/evaluation mismatches, and potential leakage when
  applicable.
- For every goal-relevant information family, assess these distinct layers:
  - source availability: the information exists at the decision point
  - pipeline preservation: transformations do not silently remove or corrupt it
  - representation/distinguishability: different relevant states remain
    distinguishable after encoding, aggregation, or compression
  - learnability: supervision, capacity, optimization, sample support, and
    architecture make the signal practically learnable
  - utilization: outputs or decisions measurably depend on the information
    rather than merely carrying it in an unused representation
  - evaluation validity: tests and metrics can detect whether the information
    improves the actual requested outcome
  - causal/operational validity: gains are not explained by leakage, shortcuts,
    unavailable future data, or a deployment mismatch
- Being present in a raw field, tensor, hidden state, attention map, gradient,
  or probe is not by itself evidence that the system can use the information
  correctly for the goal.
- Recommended probes can include recoverability tests, feature ablations,
  identity- or order-destruction controls, oracle/residual tests, and
  long-sequence stress tests, optimization/supervision diagnostics,
  counterfactual decision-sensitivity checks, and leakage-safe temporal or
  causal controls. No single probe is treated as proof of model or system
  quality.

### Seeded Benchmark

- Add a compact benchmark family under `tests/fixtures/goal-research/` where
  ordinary implementation tests still pass despite defects at different
  epistemic layers.
- Add a negative fixture where an exclusion is intentional, justified, and
  correctly represented.
- Cover dropped source information, destructive representation/compression,
  unlearnable or weakly supervised signal, present-but-unused information,
  objective/evaluation mismatch, shortcut/leakage, long-context failure,
  goal-proxy drift, and plausible competing causes without implementing a
  production ML system.
- The positive benchmark passes only when the mode identifies an exact
  unsupported loss boundary with repository evidence, leaves a contradiction
  or blocking unknown when appropriate, and proposes discriminating tests.
- Generic text such as "information may be lost" cannot satisfy the benchmark.
- The negative benchmark must not produce a critical unsupported-loss finding.
- Add a benchmark where inherited reports endorse the wrong framing and a
  clean-room remap must recover the omitted issue.
- Add a benchmark where a metric improves while an original qualitative goal
  clause regresses; goal-fidelity and final-audit gates must block completion.
- Add a repeated-hypothesis benchmark and require a changed condition before a
  failed idea can be retried.
- Offline tests must validate schemas and state transitions deterministically.
  Final end-to-end acceptance also requires one live repo-aware run against the
  sanitized fixture. If a verified connector is unavailable, the Goal remains
  blocked rather than claiming completion.

### Documentation And Installation

- Document `goal-research` as a fifth, explicit lane in README without
  redefining existing lane defaults.
- Update the installed `external-advisor` skill instructions with concise
  selection, lifecycle, safety, and resume guidance.
- Add only the necessary new scripts to Linux and Windows setup completeness
  checks. Do not otherwise refactor setup.
- Run `./setup.sh` after implementation and verify the installed skill matches
  the repository copy for new and changed skill files.

## Acceptance Criteria

- Existing prompt-only advisor, prompt-only conclave, repo-aware advisor,
  repo-aware conclave, router, verifier, project binding, coordinator, timeout,
  safety, and setup behavior remain compatible and their targeted regressions
  pass.
- `goal_research.py` exposes deterministic help, initialization, one-step
  advance, Codex receipt, status, and resume behavior without contacting
  ChatGPT during help, status, schema validation, replay, or offline tests.
- An initialized run has a versioned goal contract, acceptance matrix,
  repository/snapshot identity, explicit budgets, private state directory, and
  valid first event.
- Event replay reconstructs the same state; duplicate, missing, malformed,
  truncated, reordered, or contradictory events fail closed.
- Resume is tested from every durable boundary, including unknown advisor
  submission state, waiting-for-Codex, waiting-for-verification, and
  post-change audit.
- Goal amendment and source-snapshot changes invalidate all downstream approval
  artifacts that are no longer fresh.
- Three core initial role reports are independent, schema-valid,
  exact-conversation tool-verified, and snapshot-bound.
- Goal-fidelity traces cover every original clause, and a proxy improvement
  cannot hide an omitted or regressed qualitative requirement.
- Mandatory first/final clean-room remaps receive no inherited conclusions and
  can introduce a grounded contradiction that blocks the current framing.
- Temporary specialists are limited to two, require an explicit unresolved
  question and stopping condition, and are omitted when the core roles suffice.
- The active hypothesis set never exceeds three; each explanation has
  discriminating predictions, opposing evidence, status, and retry conditions.
- A semantically repeated failed hypothesis is rejected unless the recorded
  retry condition proves a meaningful input or assumption changed.
- Exactly one claim-ID challenge round is enforced; unrestricted or recursive
  agent dialogue is impossible through the controller.
- Contradictions survive normalization, synthesis, status projection, resume,
  post-change audit, and final reporting.
- The controller emits one implementation packet and refuses a second packet
  until the current iteration reaches a terminal outcome.
- The controller never edits project source files, runs project commands on its
  own initiative, exposes shell/mutation tools to advisors, or performs Git
  commit/reset/revert/checkout operations.
- Packet and verification receipts cannot be replayed against the wrong goal,
  iteration, baseline snapshot, or current checkout.
- Budget exhaustion, malformed advisor output, connector unavailability,
  denied MCP activity, stale snapshots, and event corruption stop visibly and
  do not produce completion.
- The positive seeded fixture produces a grounded unsupported-information-loss
  finding and discriminating tests; the justified negative fixture does not
  produce a false critical finding.
- Benchmarks distinguish absent, destructively represented, unlearnable,
  unused, spuriously used, and evaluation-invalid information rather than
  collapsing them into one generic finding.
- A clean-room benchmark recovers from inherited false framing, and a
  goal-proxy benchmark blocks completion despite an improving numeric metric.
- A blind final repo-aware completion audit inspects the original goal and
  final snapshot without seeing the synthesis recommendation first.
- Overall completion is impossible while a required acceptance dimension lacks
  fresh evidence, an original goal clause lacks traceability, the blind audit
  fails, or a critical contradiction remains unresolved.
- The README and installed `external-advisor` skill explain when and how to use
  the new lane without suggesting that advisors edit the repo or replace Codex
  verification.
- `./setup.sh` installs the new files, and repository/installed copies pass
  parity checks.
- No HAR, cookies, tokens, passwords, `.env` values, private keys, transcripts,
  connector state, fixture-generated runtime files, or unrelated user data are
  added to Git.

## Non-Goals

- Do not implement or modify any Pump.fun, Transformer, trading, or other target
  project as part of this feature.
- Do not add a detached background controller or autonomous `codex exec`
  workers in v1.
- Do not grant ChatGPT edit, patch, shell, command, commit, or worktree tools.
- Do not let advisors modify the original checkout or sanitized snapshot.
- Do not add unrestricted agent-to-agent chat, unbounded/ad hoc role spawning,
  nested conclaves, or recursive research loops.
- Do not build a generic workflow engine, plugin ontology, semantic knowledge
  graph, or generalized multi-agent framework.
- Do not implement a scalar-only hill climber or automatically keep/revert Git
  commits.
- Do not redesign existing advisor memory, router classification, g4f transport,
  worker coordination, DevSpace patching, authentication, or project binding.
- Do not add selective semantic claim reuse, adaptive budgets, open-ended role
  catalogs, or full claim dependency graphs in v1.
- Do not deploy, commit, push, publish, or alter ChatGPT account settings as
  part of the implementation Goal.

## Constraints

- Preserve every existing public CLI flag, environment variable, state path,
  model route, timeout rule, provider contract, project binding, and safety
  boundary unless this PRD explicitly introduces a new opt-in contract.
- Use Python standard library and existing repository helpers unless a new
  dependency is demonstrably required and approved.
- Keep implementation changes concentrated in new `goal_research*` modules,
  new tests/fixtures, narrow setup completeness additions, and focused README
  and skill documentation.
- Avoid changing `router.py`, `advisor.py`, `conclave.py`, `advisor_agent.py`,
  `agent_conclave.py`, `verifier_loop.py`, coordinator code, runtime patches, or
  vendor code unless a verified blocker is documented in the plan first and
  the change is covered by dedicated regressions.
- Preserve existing dirty worktree changes. Read and merge around them; never
  revert, overwrite, reformat, or absorb unrelated modifications.
- Keep all run artifacts ignored and private under `.codex-advisor/`.
- Continue using generated read-only sanitized workspaces and exact MCP
  evidence. Never expose the original checkout through a new shortcut.
- Keep remote concurrency and request pacing under the existing coordinator.
- Prompt-only challenge and synthesis calls must preserve existing unlimited
  timeout semantics unless the operator explicitly supplies a positive
  deadline.

## Resolved Assumptions

- V1 is foreground and driven by a Codex Goal; it does not need a detached
  daemon or background Codex worker.
- Codex validates and versions the user goal contract; the independent steward
  detects fidelity drift but cannot rewrite the contract.
- Three core repo-aware functions, one goal-fidelity steward, and at most two
  justified temporary specialists provide the bounded first target.
- At most three competing explanations are active for one unresolved decision,
  and only one implementation hypothesis is delivered to Codex at a time.
- Clean-room remapping is mandatory before the first packet and final
  completion, with bounded event-triggered remaps in between.
- Conservative whole-snapshot invalidation is safer than selective claim reuse
  in v1.
- The existing sanitized DevSpace connector is the only repo-aware tool bridge
  used by this mode.
- The first benchmark proves defect discovery and test design, not production
  model improvement.

## Open Questions

None block implementation. Record new product, safety, or compatibility
questions in the ExecPlan and stop for user input only when they materially
change this scope.
