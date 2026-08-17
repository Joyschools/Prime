# Administrator Login Hotfix — 2026-08-17

This build restores the production Admin authentication contract.

## Source of truth
When the Render environment contains:
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

those credentials are authoritative for the Administrator account.

`ADMIN_NAME` is optional and is only used as the display name; otherwise the configured username is used.

## Compatibility behavior
The application provisions/synchronizes the Admin account during startup **and** performs a direct compatibility check during the Admin login request. This means an existing/stale SQLite database cannot reject the valid Render Admin credentials because of an old role, password hash, or migration state.

On successful environment-credential login, the matching user is forced to:
- role = `Admin`
- active = `1`
- password hash = current `ADMIN_PASSWORD`

No plaintext password is stored in the source code or database.

## Important
Keep the existing Render variables. Do not hard-code them into the project:
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- optional `ADMIN_NAME`
