# Bundled Skills

The setup scripts install every folder under `codex-skill/` into the user's Codex skills directory. This repo intentionally bundles skills that help Codex build, verify, secure, and deploy professional websites and apps.

## Local Project Skills

- `external-advisor`: project-scoped ChatGPT advisor/conclave workflow.
- `prepare-goal`: planning scaffold and `/goal` prompt preparation.

## Official OpenAI Skills

Source: `openai/skills`, curated skills.

- `figma`
- `figma-code-connect-components`
- `figma-create-design-system-rules`
- `figma-create-new-file`
- `figma-generate-design`
- `figma-generate-library`
- `figma-implement-design`
- `figma-use`
- `playwright`
- `playwright-interactive`
- `screenshot`
- `vercel-deploy`
- `netlify-deploy`
- `cloudflare-deploy`
- `render-deploy`
- `sentry`
- `security-best-practices`
- `security-threat-model`

## Additional Public Skills

- `frontend-design`: from `vipulgupta2048/codex-skills`, MIT license included in the skill folder.
- `web-design-guidelines`: from `vercel-labs/agent-skills`, Vercel web interface guideline review skill.
- `vercel-react-best-practices`: from `vercel-labs/agent-skills`, MIT-licensed React/Next.js performance guidance.
- `supabase`: from `supabase/agent-skills`, MIT license.
- `supabase-postgres-best-practices`: from `supabase/agent-skills`, MIT license.

## Maintenance

- Do not commit HAR files, cookies, tokens, local `.codex-advisor/` state, or `vendor/`.
- When updating bundled skills, review `SKILL.md` metadata and scan for secrets before committing.
- Restart Codex after setup or skill changes so new skill metadata is discovered.
