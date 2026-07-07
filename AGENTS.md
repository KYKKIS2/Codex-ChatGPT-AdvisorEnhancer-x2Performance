# AGENTS.md

This repository bundles Codex skills under `codex-skill/`. The setup scripts install every folder in `codex-skill/` into the user's Codex skills directory, so future sessions can use them automatically after Codex restarts.

## Skill Routing

Use bundled skills when the user request matches their domain. Prefer the most specific skill first, and combine skills when a web task crosses design, implementation, database, security, and deployment.

- Use `external-advisor` for architecture, strategy, planning, tradeoff analysis, critique, verification, and important judgment-heavy decisions.
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

## Web Build Workflow

For serious website/app work:

1. Identify stack and constraints from the repo before choosing tools.
2. Use `frontend-design` or Figma skills for visual direction and implementation.
3. Use framework/backend/database skills for correctness and performance.
4. Use Playwright/screenshot skills to verify desktop and mobile behavior.
5. Use security skills before shipping auth, user data, or exposed backend changes.
6. Use deployment skills only after local verification is clean.

Do not use every skill on every task. Use the smallest set that materially improves the result.
