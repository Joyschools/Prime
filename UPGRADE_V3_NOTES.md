# Prime Institutional Upgrade v3 — Transformation Pass

## What changed
- Administrator-only creation of ICT accounts. ICT cannot create Administrator or another ICT account.
- Added Admin-only **User Management** section directly after Finance in the institutional hub.
- User Management creates named accounts with username/password, portal role, position/title (HOD, Deputy, Dean, ICT, Teacher/Lecturer, Finance Officer, Librarian, Student/Learner, Parent/Guardian), department and recovery email.
- Accounts remain auditable: deactivation is used instead of hard deletion.
- ICT account-management endpoints cannot target Administrator or ICT accounts.
- Admin and Finance get Finance embedded in the command centre; other offices do not get a Finance navigation link.
- Added explicit Command Centre/Home/back affordances.
- System AI now responds to greetings, knows the institutional module/role model, gives professional guidance, explains the user's current office, and deliberately withholds restricted authority/security links.
- System AI is available as a guided assistant to authenticated roles; API AI remains a separate external-provider feature.
- Library is authenticated-only. Registered users can view approved library content; only authorized library roles can manage catalogue/lending.
- Student/Parent no longer inherit `library.manage`; they receive `library.view`.
- Library supports books, links, documents and picture/image resources.
- Password recovery no longer returns reset tokens to unauthenticated callers. Automatic delivery should use SMTP; a token is only shown in no-SMTP mode to an authenticated user recovering their own account.
- Password creation UI is aligned to an 8-character minimum.

## Validation
- `app.py` Python syntax compilation: PASS.
- Static authority/security assertions: PASS.
- Full Flask runtime boot not available in this build environment because Flask/dependencies are not installed here.
