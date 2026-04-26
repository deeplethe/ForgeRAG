# Authentication & Session Management

ForgeRAG ships with a minimal, self-contained auth layer. It is designed for
**single-operator deployments** (one admin, zero multi-tenancy) but the schema
is multi-user ready so nothing has to be redesigned if that changes later.

**TL;DR:**

- Password + session cookie for the web UI.
- `Authorization: Bearer <sk>` for CLI / SDK / curl — tokens stored in DB.
- `auth.enabled=false` disables everything (dev on loopback only).
- First boot with `auth.enabled=true` auto-creates an `admin` user with a
  **fixed initial password** + a **random bootstrap SK** and prints both to
  stdout. The web UI forces a password change on first login.

Path filtering (`path_filter=/scope`) is a retrieval-scope knob, **not** an
access-control boundary — see [path-denormalization.md](path-denormalization.md).
Any authenticated principal can ingest/query anywhere today.

---

## Enabling auth

In your yaml config (defaults shown, override as needed):

```yaml
auth:
  enabled: true                # default false
  mode: db                     # "db" | "forwarded"
  initial_password: forgerag   # applied once at auto-bootstrap
  session_cookie_name: forgerag_session
  session_cookie_secure: true  # set false only for http://localhost dev
  password_change_revokes_other_sessions: true
  public_paths:
    - /api/v1/health           # always bypass auth
  # mode=forwarded only:
  # forwarded_user_header: X-Forwarded-User
```

Restart the backend. On the first boot after the auth tables are empty you'll
see a banner like:

```
================================================================
  FIRST-RUN ADMIN CREATED
  username:   admin
  password:   forgerag        ← change on first web login
  bootstrap SK: Forge_9VUpg4Tb1GUDwCXo1utymqy7FX5sdeeKr66hp3h7J1Nk
================================================================
```

Copy the SK **now**; it cannot be recovered from the DB.

## Two modes

### `mode: db` (default)

ForgeRAG owns auth. Users / tokens / sessions live in Postgres tables
(`auth_users`, `auth_tokens`, `auth_sessions`). Login via
`POST /api/v1/auth/login` issues an opaque 64-char session id, set as an
HttpOnly cookie. Bearer tokens authenticate CLI/SDK calls.

### `mode: forwarded`

Deploy ForgeRAG behind an OAuth / SSO reverse proxy (oauth2-proxy, Authelia,
Cloudflare Access…). The middleware trusts the configured
`forwarded_user_header` (default `X-Forwarded-User`) and auto-provisions an
`auth_users` row on first seen username. **Make sure only the proxy can reach
the backend** — the header is taken at face value.

Login / password / tokens routes are still available in forwarded mode so
internal tooling can mint SKs for CLI scripts even though humans never see
the password form.

---

## Credentials

### Session cookie (web UI)

Set by `POST /api/v1/auth/login` (`{"username","password"}`). HttpOnly, no
TTL. A session is invalidated when:

- User clicks "Sign out" (`POST /api/v1/auth/logout`)
- User changes password and
  `password_change_revokes_other_sessions=true` — all **other** sessions
  are revoked, the current one is kept.
- User clicks "Sign out other devices" (`POST /api/v1/auth/sessions/sign-out-others`)
- Admin / user revokes it explicitly (`DELETE /api/v1/auth/sessions/{id}`)

Sessions are **not** revoked on new login. Multiple devices can stay signed in.

### Bearer token / SK (CLI / SDK)

Format: `Forge_<44 base58 chars>`. Stored as SHA-256 hash plus an 8-char
`hash_prefix` (for UI display). Raw token value is only ever returned **once**
by `POST /api/v1/auth/tokens` or the bootstrap banner — lose it and you rotate.

Tokens can optionally expire (`expires_days` at creation).

---

## HTTP endpoints

All endpoints live under `/api/v1/auth/`. See
[api-reference.md](api-reference.md) for the full schema; summary:

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/login` | — | `{username, password}` → Set-Cookie, returns principal |
| POST | `/logout` | session | revoke current session |
| POST | `/change-password` | session or bearer | `{old_password, new_password}` |
| GET  | `/me` | any | current principal (user_id, role, via, must_change_password) |
| GET  | `/tokens` | any | list SKs (metadata only, no raw token) |
| POST | `/tokens` | any | `{name, expires_days?}` → `{token, token_id, ...}` (raw token **once**) |
| DELETE | `/tokens/{id}` | any | revoke |
| PATCH  | `/tokens/{id}` | any | `{name?, expires_days?}` |
| GET | `/sessions` | session | list active sessions (`is_current=true` flags this one) |
| DELETE | `/sessions/{id}` | session | revoke one; can't be the current |
| POST | `/sessions/sign-out-others` | session | revoke all except current |

Everything else under `/api/v1/` requires either a session cookie or a bearer
SK, except `/api/v1/health` (and anything listed in `auth.public_paths`).

On missing / invalid credentials the middleware returns
`401 Unauthorized` with `WWW-Authenticate: Bearer`.

---

## Web UI

- `/login` — username + password (admin user is prefilled).
- On successful login the router pushes to `?redirect=` (default `/chat`).
  The page reloads so SSE and cached composables pick up the fresh cookie.
- If `must_change_password=true` (first login), a forced `ChangePasswordModal`
  appears on top of the app — no Cancel, no current-password field, blocks the
  app until a new password is set.
- `/tokens` — the **Tokens & Sessions** page:
  - Create / rename / revoke SKs. New tokens are revealed **once** in an
    amber banner — copy it then.
  - View active web sessions with UA / IP / times; revoke individual
    sessions or "Sign out other devices".
  - "Change Password" button re-opens the modal in non-forced mode (asks
    for current password; revokes other sessions when saved).
- Sidebar footer shows the current username and a "Sign out" button.

The frontend installs a single 401 interceptor in `web/src/api/client.js`.
When any authed call returns 401, the router redirects to
`/login?redirect=<current>`. Login-form 401s are swallowed so the form can
surface "Incorrect username or password" inline instead of self-redirecting.

---

## CLI

The `forgerag` CLI reads `FORGERAG_API_TOKEN` from the environment by default.
Override with `--token`.

```bash
# From the bootstrap banner or a Tokens page reveal:
export FORGERAG_API_TOKEN='Forge_9VUpg4Tb1GUDwCXo1utymqy7FX5sdeeKr66hp3h7J1Nk'

forgerag auth whoami                     # print current principal JSON
forgerag auth list-tokens                # tab-separated table
forgerag auth create-token --name laptop # prints raw token once
forgerag auth revoke-token <token_id>
forgerag auth logout                     # revoke session only (cookie-based)

# Locked out? Direct DB reset, bypasses HTTP:
forgerag auth reset-password --username admin
```

`reset-password` clears `must_change_password` so the operator can log in
normally after the reset. It runs inside the backend process's DB config
— run it on a host that can reach the relational store.

---

## Operator playbook

### Lost the admin password

```bash
forgerag auth reset-password --username admin
# prompts for new password interactively
```

### Lost all SKs

Log in via the web UI, go to **Tokens & Sessions**, click **+ New Token**.
If you can't log in either, reset the password first.

### Rotate the admin password

Either the web UI (`Change Password` button on the Tokens page) or:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/change-password \
  -H "Authorization: Bearer $FORGERAG_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password":"forgerag","new_password":"something-stronger"}'
```

### Suspect a leaked SK

```bash
forgerag auth list-tokens              # find the token_id by hash_prefix
forgerag auth revoke-token <token_id>
```

Clients using that SK will start getting 401 immediately.

### Suspect a hijacked session

Web UI → Tokens & Sessions → "Sign out other devices". Or:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/sessions/sign-out-others \
  -H "Cookie: forgerag_session=$YOUR_COOKIE"
```

For extra safety, rotate the password too — this will also revoke other
sessions when `password_change_revokes_other_sessions=true`.

---

## Security notes

- **Password hashing:** argon2id (`argon2-cffi`). `needs_rehash` runs on
  every successful login; hashes are transparently upgraded if the
  parameters change.
- **Token hashing:** SHA-256. Tokens themselves are 256 bits of CSPRNG
  entropy (`secrets.token_bytes(32)`) encoded with a custom base58 → no
  external deps — `Forge_` prefix + 44 chars.
- **Session ids:** 64 hex chars (`secrets.token_hex(32)`). Opaque,
  server-side state only — no JWT, no signing key to rotate.
- **Cookie flags:** `HttpOnly`, `SameSite=Lax`, `Secure` when
  `session_cookie_secure=true`. Disable `Secure` **only** for local http dev.
- **OTel:** authenticated requests set `enduser.id`, `enduser.role`, and
  `auth.via` attributes on the request span so traces are attributable.
- **Rate limiting / lockout:** not implemented. Put a reverse proxy in
  front for production (nginx `limit_req`, Cloudflare, etc.).
- **Admin role:** today only the `admin` role exists; the `role` column
  is reserved for future scopes. All authenticated principals have full
  access — there is no per-path ACL. Path filtering is a retrieval-scope
  selector, not a permission check. See
  [path-denormalization.md](path-denormalization.md).

---

## Database schema

Auto-created by `Store.ensure_schema()` on startup (same path as the rest of
the ORM). A matching alembic migration lives at
`alembic/versions/20260424_auth_tables.py` for production deploys that gate
schema changes on explicit migrations.

### `auth_users`
`user_id` (pk), `username` (unique), `password_hash`, `must_change_password`,
`password_changed_at`, `role`, `is_active`, `created_at`, `last_login_at`.

### `auth_tokens`
`token_id` (pk), `user_id` (fk), `name`, `token_hash` (sha256 hex),
`hash_prefix` (8 chars for UI), `role`, `created_at`, `last_used_at`,
`expires_at`, `revoked_at`.

### `auth_sessions`
`session_id` (pk, 64 chars), `user_id` (fk), `created_at`, `last_seen_at`,
`ip`, `user_agent`, `revoked_at`.

Revocation is soft — rows stay around for audit. A periodic cleanup of rows
where `revoked_at < now() - 30d` is fine to add if the tables grow; ForgeRAG
does not run one automatically.
