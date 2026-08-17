# Presentation Mode — 17 August 2026

This build temporarily enables non-blocking dashboard entry for the presentation.

Direct office entry paths create a harmless presentation identity when needed, so the dashboard can be viewed without a login:
- /admin and the hidden admin path
- /ict
- /finance
- /teacher
- /student
- /parent
- /librarian
- direct *-dashboard routes

The existing authentication system, Render Admin credentials, User Management, permissions, AI, database, and other architecture are preserved. Real authentication remains available at /login.

To restore normal authentication after the presentation, set:
DEMO_PRESENTATION_MODE=0

Or remove the variable and change the code default from 1 to 0 in a later hardening build.
