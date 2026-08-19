# School Portal System

A local-first Flask school portal with interconnected Admin, ICT, Finance, Teacher, Student and Parent workspaces.

## Local startup

For a normal Python setup, run `py -m pip install -r requirements.txt` once, then `py app.py`. The project also includes a bundled pypdf fallback so the finance PDF features do not fail at startup when pypdf was not installed separately.

## Local role entry points
- `/` — public role selector (Teacher, Student, Parent)
- `/teacher` — Teacher dashboard
- `/student` — Student dashboard
- `/parent` — Parent dashboard
- `/admin` — hidden Admin entry
- `/finance` — hidden Finance entry
- `/ict` — hidden ICT entry

Authentication starts disabled for non-Administrator users. The Administrator always authenticates. After setup, Admin can enable or disable the login page from Users → Login Page Access; disabling it sends every non-Administrator role directly to its dashboard, while enabling it inserts the login page before those dashboards.

## Core additions
- Teacher assignment posting and submission collection
- Student assignment uploads (Word, PDF, PNG/JPG/WebP)
- Parent child-linked academic/fee view
- Teacher-parent messaging
- Google Meet entry point
- ICT-wide branding, colors, logo and navigation blueprint controls
- Existing pupil, payment, examination, backups and exports preserved


Copyright © 2026 Toror Technology and Innovations Ltd. All rights reserved.

## Finance, Results & Document Verification

The Finance workspace now supports:
- Posting student payments with optional receipt/proof uploads.
- Automatic recalculation of the student's outstanding balance and Paid/Pending status.
- A configurable result-download balance threshold (default: KES 500).
- Finance approval or rejection of submitted exam-result batches before release.
- Admin override for result release when necessary.
- Official result PDF generation with a QR verification code.
- Exam-card generation or upload. Uploaded PDF/image cards receive a verified copy with an embedded QR where supported.
- Public `/verify/<token>` verification pages for issued result and exam-card documents.

Student and Parent dashboards show the current finance release state and only expose the official result download when the batch is Finance-approved and the student's balance is within the configured threshold.


## Authentication

Fresh installations begin with a one-time Administrator registration at `/register`. Once the Administrator account is created, registration disappears and only login remains. Public role login is available for Teacher, Student, and Parent; `/admin`, `/ict`, and `/finance` are direct role login entry points. Admin can create or disable any non-System account. ICT can create ICT, Finance, Teacher, Student, and Parent accounts but cannot create or disable Administrator accounts.


Wrap-up changes: passwords are minimum 4 characters with show/hide controls; Admin can enable/disable authentication globally and manage before-login public sections (institution, history, achievements, owners, developer, company).
## Final authentication behavior
- New installations begin with the non-Administrator **Login Page disabled**.
- Admin always requires username/password authentication.
- Admin → Users contains **Login Page Access** with Enable/Disable controls.
- Changing the setting requires confirmation plus the current Administrator password.
- When disabled, Teacher, Student, Parent, ICT, Finance and Librarian entries go directly to their dashboards.
- When enabled, those same roles must authenticate before the dashboard is opened.
- The Public Information page no longer changes authentication; it only controls the pre-login information sections.


## 2026 production-hardening additions

- Administrator entry uses the non-advertised route configured in `ADMIN_LOGIN_PATH`; legacy `/admin` no longer exposes the administrator entry point.
- Passwordless role entry is automatically restricted to roles with exactly one active account; multiple accounts require credentials.
- Student, parent, document and student-API access is checked against the authenticated user's relationship/role.
- Finance receipts are stored under a protected upload path rather than as anonymously retrievable files.
- Database restore now validates SQLite integrity and required core tables before replacement and attempts an automatic rollback if migration fails.
- Secure session cookies are enabled automatically on Render/when `COOKIE_SECURE=1`.

## Render persistence / heartbeat

For production on Render, attach a persistent disk mounted at `/var/data`. The app automatically keeps the live SQLite database, session secret, and uploads there when the disk is present. You can also set `DATA_DIR` or `PERSISTENT_DATA_DIR` explicitly.

Optional environment variables: `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ADMIN_NAME`, `PULSE_PEER_URL` (defaults to `https://breathe-xozy.onrender.com`), `PULSE_TIMEOUT_SECONDS`, and `PULSE_ALLOWED_CALLBACK_HOSTS` (comma-separated HTTPS hosts allowed for an incoming `reply_to`).

Heartbeat endpoints: `/pulse_receiver` and `/pulse`. They acknowledge incoming pulses with JSON and schedule a reply to the configured peer.

## V9 presentation polish
- Dedicated Student and Parent dashboards.
- Teacher-published live class links appear automatically for learners in the assigned class.
- Driver workspace remains available from the staff/transport side of the platform.
- Institution-branded long-form footer with configurable legal name, description, contact and optional platform-provider credit.
- ICT can control the portal footer presentation alongside the existing theme engine.

### V16 reception QR behavior
Reception uses one institutional staff QR identity per person. On a computer, the logged-in reception workspace displays the signed-in person's QR for display or printing. On a phone, the QR card is hidden and the workspace remains scanner-first so the receptionist can scan another staff member's QR for check-in/check-out. The same QR generated for an Admin or other registered staff member is accepted by the reception scanner; Admin is no longer treated as an invalid staff QR target.

## Prime V17 – Reception / Secretary Workspace

V17 adds the secretary/reception workspace separation, PC-first staff attendance control, institution attendance schedule settings, immediate Admin attendance notifications and CSV export, a whole-staff hub for meetings and duties, teacher duty/meeting visibility, and per-user workspace color/font preferences. Reception has no Admin command-centre navigation and Admin-only routes remain protected by role checks.
