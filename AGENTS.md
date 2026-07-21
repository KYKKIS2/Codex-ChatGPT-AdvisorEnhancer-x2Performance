# AGENTS.md

This repository bundles Codex skills under `codex-skill/`. The setup scripts install every folder in `codex-skill/` into the user's Codex skills directory, so future sessions can use them automatically after Codex restarts.

## Skill Routing

Use bundled skills when the user request matches their domain. Prefer the most specific skill first, and combine skills when a web task crosses design, implementation, database, security, and deployment.

- Use `external-advisor` for architecture, strategy, planning, tradeoff analysis, critique, verification, and important judgment-heavy decisions. Prefer `router.py --execute`; it uses a repo-aware single agent or agent conclave only after the current DevSpace URL has completed a verified ChatGPT MCP turn. A locally healthy but not-yet-added connector stays on prompt-only advisor paths.
- Use `prepare-goal` when the user wants durable planning files or a ready `/goal` prompt before long autonomous work.
- Use `frontend-design` for distinctive, production-grade visual UI, page design, layout direction, polished HTML/CSS/React interfaces, and avoiding generic AI-looking web output.
- Use `web-design-guidelines` when reviewing UI/UX/accessibility or auditing a site against web interface best practices.
- Use `figma`, `figma-use`, `figma-implement-design`, `figma-generate-design`, `figma-generate-library`, `figma-create-new-file`, `figma-create-design-system-rules`, and `figma-code-connect-components` for Figma-driven design, design-system, component-library, or design-to-code work.
- Use `playwright`, `playwright-interactive`, and `screenshot` for browser-based UI verification, responsive checks, screenshot comparison, visual QA, and end-to-end testing.
- Use `vercel-react-best-practices` when writing, reviewing, or optimizing React/Next.js code, data fetching, rendering, bundle size, or app performance.
- Use `supabase` for Supabase app/backend/auth/storage/realtime/edge-function work.
- Use `supabase-postgres-best-practices` for Postgres schemas, queries, indexing, RLS, migrations, and database performance.
- Use `security-best-practices` and `security-threat-model` when web/backend/database changes touch auth, sessions, user data, payments, secrets, permissions, public APIs, file uploads, or deployment exposure.
- Use `vercel-deploy`, `netlify-deploy`, `cloudflare-deploy`, or `render-deploy` when preparing or performing deployment on those platforms.
- Use `sentry` when adding, debugging, or improving production error monitoring and observability.

## Advisor Repository Maintenance

- Keep advisor implementation changes under `codex-skill/external-advisor/`, runtime patches under `patches/`, and executable regression entrypoints under `tests/`.
- Use the skill wrappers and managed g4f supervisor for live checks. The default transient mode creates one isolated worker per admitted call, closes it afterward, serializes turns to the same conversation across state files, and upgrades a first-turn state lock to the returned conversation id before persistence. An unknown-conversation first turn temporarily owns every known remote slot until that binding is established, preventing a cross-state lock-upgrade deadlock; normal configured concurrency resumes afterward. A separate machine-wide FIFO uses the running supervisor's authoritative remote capacity, keeps FIFO order through disposable-worker startup and request preflight, paces immediately before each actual turn submission, retries idempotent remote reads with backoff, and temporarily degrades to one turn after HTTP 429. Each non-idempotent turn-submission POST is attempted exactly once per wrapper invocation; every POST-side error fails closed instead of risking a duplicate ChatGPT branch. Do not post directly to a control or transient worker port.
- Repo-aware calls use `--timeout 0 --queue-timeout 0` by default so long MCP/Pro turns return when finished instead of failing at 900 seconds. Unlimited completion waiting starts only after the current prompt or stream is observed within the bounded acceptance window. Keep explicit positive deadlines only for deliberate bounded diagnostics.
- Prompt-only Pro Extended calls also default to timeout `0` when neither `--timeout` nor `ADVISOR_TIMEOUT` is supplied. The router and prompt-only conclave must preserve that unlimited completion wait through every subprocess layer; an explicit positive value is the only normal way to impose an operator deadline.
- Prompt-only transport is verbatim by default: Codex controls the bounded prompt, generated context pack, and explicitly selected context, and the wrappers must not silently redact or block them. `ADVISOR_PROMPT_PROTECTION=true` is an opt-in compatibility mode. Repo-aware agent paths remain mechanically read-only and must always retain sanitized-workspace, secret-scan, and denied-path controls.
- Repo-aware roles and synthesis are checkpointed before submission. If the local Codex process stops, submitted ChatGPT agents can continue online. Resume the original run with `agent_conclave.py --resume-run <run-directory>`; recovery is GET-only for submitted turns, never blindly replays ambiguous work, and launches only roles whose journal proves submission never began. Never start two resumptions for the same run; the run lock must remain intact.
- Repo-aware reviews must remain mechanically read-only, use proxy-free and redirect-free loopback transport with local listeners bound to `127.0.0.1`, and operate on generated sanitized workspaces. Sanitized plans are built under their generation lock, verified with repeated source-tree/Git scans, and reject non-regular entries. Do not weaken endpoint, denied-path, workspace-identity, current-turn evidence, redaction, or mutation checks to make a failing test pass. Sanitization is secret-focused defense in depth, not a general PII classifier.
- Treat `.codex-advisor/`, HAR files, cookies, auth state, local connector state, worker manifests, generated workspaces, logs, and environment files as private local runtime data. Keep them ignored and uncommitted.
- Before a release, inspect the exact staged diff, probe ignore rules, and scan the staged export for secrets. Test fixtures may use explicit fake values, but real credentials or captured session material must never enter Git.

## Web Build Workflow

For serious website/app work:

1. Identify stack and constraints from the repo before choosing tools.
2. Use `frontend-design` or Figma skills for visual direction and implementation.
3. Use framework/backend/database skills for correctness and performance.
4. Use Playwright/screenshot skills to verify desktop and mobile behavior.
5. Use security skills before shipping auth, user data, or exposed backend changes.
6. Use deployment skills only after local verification is clean.

Do not use every skill on every task. Use the smallest set that materially improves the result.
