# Prime Institutional Upgrade — 2026.08

This build adds an institutional layer on top of the existing school workflows.

## New capabilities

- Unified institutional command hub at `/upgrade-hub` for every authenticated role.
- Global theme layer using the institution's login colors across the new experience.
- Institution mode and terminology: School, College, University/TVET, Training Centre; configurable learner/staff/class/academic-period labels.
- Curated typography controls.
- First-class people profiles with profile pictures, department, job title, contact fields, authority level, activation/deactivation and audit trail.
- Granular permission catalog with role defaults plus per-user extra permissions.
- Executive/leadership-style summarized metrics.
- Direct communications foundation: conversations, members, messages, announcements and in-app notifications.
- Digital library resource links alongside the existing physical/digital library.
- AI study-assistant gateway using an OpenAI-compatible API shape. Provider credentials can be supplied through environment variables rather than the browser.
- AI usage audit table for future governance/limits.
- New institutional database entities for departments and communications.
- Existing Finance, Results, Examinations, Library, Elections and legacy role workspaces remain available.
- Stronger password minimum (8 characters in registration/profile update paths touched by this upgrade).

## AI configuration

Preferred environment variables:

- `AI_API_URL` — OpenAI-compatible chat-completions endpoint.
- `AI_API_KEY` — server-side API key.
- `AI_MODEL` — model name.

These are intentionally not exposed to client JavaScript.

The UI also stores provider metadata in `school_settings`; using environment variables is recommended for secrets.

## Compatibility note

The new layer uses additive SQLite tables/columns and does not require an immediate destructive database replacement. Existing records are preserved.

## Recommended next engineering phase

The next phase should move the growing Flask application into modular packages (`people`, `academics`, `finance`, `library`, `communications`, `ai`, `security`, etc.) and replace the historical `CREATE TABLE IF NOT EXISTS` evolution with versioned migrations.
