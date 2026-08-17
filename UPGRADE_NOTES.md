# Prime Institutional Upgrade v2 — 17 August 2026

This build moves the institutional layer from a transition screen toward a unified application shell.

## Major changes
- Finance is embedded in the command centre for Admin and Finance roles. Users see live income/balance figures, recent payments and a payment-posting form without leaving the dashboard. A detailed Finance route remains available for deep workflows.
- Added two AI modes:
  - **System AI** — built-in, deterministic portal guidance. It can explain where features live and start password recovery without seeing or storing user passwords.
  - **API AI** — existing OpenAI-compatible provider gateway remains available for richer study/creation tasks when enabled and configured.
- Added public System AI login help so a locked-out user can ask for recovery before authenticating.
- Added secure one-time password reset tokens with 20-minute expiry and optional SMTP email delivery (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS`). Reset links are invalidated after use.
- Expanded library taxonomy for books, study links and image/picture resources. Existing file uploads already support PDF/DOC/DOCX/PNG/JPG/JPEG/WEBP.
- Improved desktop and phone behavior for the institutional hub, Finance workspace, AI controls and tables.
- Existing institutional terminology, people/profile, permissions, communications, notifications, leadership summaries, theme bridge, PWA and other Prime features remain intact.

## Security notes
- AI never receives password hashes or raw passwords.
- Recovery tokens are stored as SHA-256 hashes and expire automatically.
- Public recovery responses do not confirm whether a username exists.
- The normal authenticated password-change workflow remains available.

## Deployment
The app creates the required new SQLite tables automatically at startup through `init_db()`.
The bundled database also contains the new recovery/AI tables; the remaining application-managed tables continue to be created/migrated at startup.

A live Flask boot test was not possible in the build environment because Flask and the project's runtime dependencies are not installed there. Python syntax and ZIP integrity were verified.
