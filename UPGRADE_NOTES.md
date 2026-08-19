# Institution OS Upgrade — 17 August 2026

This build upgrades the existing school portal toward a unified institutional platform.

## Included in this upgrade

- **People & Access** replaces the shallow Employees view with an institutional directory.
- Admin can create Administrator, ICT, Finance, Teacher/Lecturer, Student/Pupil, Parent/Guardian and Librarian accounts.
- **Role vs position**: account role controls the office; title and department describe the organizational position.
- **Department layer** with automatic baseline provisioning of Communications and Computer Studies for TVET/College/University/Mixed institution modes.
- **Institution configuration** for institution type and terminology such as Student/Pupil/Learner, Teacher/Lecturer, Term/Semester, Class/Cohort and Department/School/Faculty.
- Expanded person profile fields, including optional DOB, gender, reference ID, address, emergency contact, blood group, medical notes and accountability notes.
- **Guardian relationship layer** allowing Parent/Guardian accounts to be linked to learners without relying on a single name/phone field.
- Existing accounts can be opened and edited.
- **Archive/restore** preserves historical records instead of deleting account history.
- **Administrator password reset** with 8-character minimum temporary-password enforcement and hashed storage.
- ICT can manage ordinary institutional accounts but cannot create or modify Administrator/ICT accounts.
- Professionalized Admin first-look dashboard and People & Access presentation.
- Search/filtering across the people directory.

## Integrity checks performed

- Python syntax compilation passed for `app.py` and `wsgi.py`.
- Static assertions confirmed the new routes, permission boundaries, migration fields and institutional configuration features are present.
- A copy of the shipped SQLite database was migrated with the new schema subset; original users/students remained intact and `PRAGMA foreign_key_check` reported no violations.
- Final ZIP integrity test passed.

## Runtime note

The available execution environment for this packaging pass does not have Flask and the application's runtime dependencies installed, so a live Flask server boot was not performed here.


## Institution OS expansion v2
- Real System Help centre and role-scoped help articles.
- OpenAI Responses API and Chat Completions API adapters; key stays in OPENAI_API_KEY server environment.
- AI usage audit log.
- Library catalogue supports class/grade, subject, cover images, YouTube links, websites, source names and uploaded digital resources.
- Separate complete learner and employee directories.
- Institution mode labels (Primary, Secondary, TVET, College, University, Mixed) remain configurable through Administration.
- Finance ledger supports locked Income/Expense/Payroll/Adjustment transactions; only Admin can reverse a posted transaction.
- Full JSON system backup with database records, settings and included uploaded assets; JSON restore plus SQLite rollback.
- User portal QR opens the institution login/landing page without storing plaintext credentials.
- Advanced admin theme engine: colors, typography, radius, sidebar/header colors, navigation and bounded custom CSS.

## V16 final reception QR behavior

- Reception staff now have the same institutional staff QR identity as other staff, including Admin accounts.
- On desktop/computer reception workspaces, the logged-in person's QR is displayed for display/printing.
- On phones, the desktop QR card is hidden and the reception workspace remains scanner-first for check-in/out.
- Reception scanning accepts the same staff QR generated from the user profile/attendance QR endpoint, including Admin.
- Registered staff continue to be resolved from the QR record rather than treated as generic Admin/anonymous users.
