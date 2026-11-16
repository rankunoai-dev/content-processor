# Team accounts (sign-in, roles, sessions, access tokens)

`src/modules/accounts/`. Off until the first admin is created; from then on every request
needs a signed-in account or a personal access token.

## Roles

| Role | May |
|---|---|
| viewer | every `GET` (pages, reports, downloads), `POST /api/mcp/call` (read-only tools) |
| reviewer | also review decisions: `POST /api/runs/{run}/decisions`, `.../gaps/findings/{id}/status`, `.../sites/{site}/runs/{run}/verdicts` (`access.REVIEW_PATHS`) |
| editor | also every other change (projects, websites, addresses, scrapes, pulls, builds, analyses) |
| admin | also `/api/accounts...` and `POST /api/projects/{id}/delete` |

`access.required_role` decides from method and path, before any route runs (`install_accounts` adds the gate middleware); anything not listed needs editor (secure by default). An account with a temporary password may only use `/api/auth/...` until it chooses its own. The request then runs with `security.ACTING` set, so every project unlock check (`TokenSigner.check`) passes: project passwords no longer apply, and a new project gets an unguessable password nobody needs. The app's own files are always served (the page shows sign-in).

## Accounts and sessions

- `store.AccountStore` (`<app data>/accounts/accounts.json`): user name (lower case), display name, role, scrypt hash, `must_change_password`, `disabled`, `session_epoch`, created at/by, last sign-in. Passwords at least 10 characters. A missing user name costs the same time to check as a wrong password.
- Sessions (`access.SessionSigner`): an HttpOnly, SameSite=Lax cookie `cp_session` (Secure behind https, `X-Forwarded-Proto` honoured), HMAC-signed with a key derived from the installation secret (`TokenSigner.subkey("session")`), holding user name, epoch and expiry (`CP_SESSION_HOURS`, default 12). Raising the epoch (password change or reset, disable, role change) signs the account out everywhere; changing one's own password keeps the current device signed in.
- Wrong passwords: the project `AttemptLimiter`, per user name and per client address (5 free, then doubling waits).
- Guards: the last enabled admin cannot be demoted or disabled; nobody disables their own account.
- Personal access tokens (`tokens.json`, SHA-256 hashes only; `cpat_` prefix): act with their own role, at most the account's; revoked, or the account disabled, they stop at once. The MCP connector sends `CP_APP_TOKEN` from its environment as `Authorization: Bearer`.

## API

`POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` (`accounts`: whether sign-in is on; `account`), `POST /api/auth/password`; admins: `GET/POST /api/accounts`, `POST /api/accounts/{user}/update` (name, role, disabled), `POST /api/accounts/{user}/password` (temporary password).

## Command line

`accounts create-admin | add | list | reset-password | token | tokens | revoke-token` (passwords typed twice without echo, or one line on standard input for scripts; the first admin chooses their own password; tokens shown once).

## Server

`webapi` refuses to listen beyond this machine (`CP_BIND_HOST` other than localhost) while no account exists. With accounts it says "sign-in is required". `CP_ALLOWED_HOSTS` and `CP_ALLOWED_ORIGINS` still need the public host name.

## Web app

`AuthGate` (sign-in page, forced first password change), `UserMenu` (name and role, change password, sign out, Accounts for admins), `AccountsPage` (add, role, enable, reset). With accounts, `useProjectToken` returns a placeholder for editors and admins and null for others, so every existing "needs the project unlocked" control follows the role; the lock badge says "Your role (Viewer) cannot change projects"; new projects take no password and the owner defaults to the signed-in person; deleting a project is for admins. A 401 anywhere asks who is signed in again (the gate then shows sign-in).

## Not done yet

- Reviewing verdicts of project scrapes in the app (next).
- Password reset by email (admins reset passwords instead).
- Per-project membership (every editor may change every project).
