# Privacy Statement

## TL;DR

OpenCraig is self-hosted software. It runs on your infrastructure;
your data never leaves your network unless you point it at an
external LLM provider, in which case the data flow is **between you
and that provider** — not through us. There is no telemetry, no
usage analytics, no error reporting, no update check, no "phone home"
of any kind originating from this codebase.

DeepLethe (the company building OpenCraig) has no operational visibility
into your deployment. We can't see your users, documents, or
queries. If your deployment has problems, we know nothing about them
unless you tell us.

## What stays on your server

* **All uploaded documents** — originals, parsed blocks, chunks,
  every artifact from the ingestion pipeline.
* **All accounts** — auth_users, sessions, tokens, password hashes
  (argon2id, never the plaintext).
* **All conversations + messages** — history, citations, audit trail.
* **The knowledge graph** — entities + relations extracted during
  ingestion live in your Neo4j instance; nothing leaves it.
* **The audit log** — every folder/document/share mutation, with
  the actor's user_id stamped in.
* **All search indexes** — vector embeddings (in pgvector or Chroma),
  BM25 index files, tree path summaries.

## What leaves your server (only when you configure it)

* **LLM API calls.** OpenCraig's whole job is to call language
  models. Whoever you point it at — OpenAI, Anthropic, DeepSeek,
  SiliconFlow, Ollama on your own GPU, anything else — sees:
  - Document chunks (during embedding)
  - User queries + retrieved context (during chat answer
    generation, query understanding, KG extraction, reranking)
  - LiteLLM-formatted prompts that we control
* **Nothing else.** No analytics ping, no version-check, no
  "was this answer helpful" feedback going to us.

To keep ALL data on-prem, configure a self-hosted endpoint as your
LLM provider. The setup wizard's **Ollama** preset does this with
one click — chat + embeddings via your own Ollama server, zero
external network dependency.

## What we don't do

| Category | Status |
|---|---|
| Telemetry / usage statistics | ❌ never |
| Crash / error reporting to us | ❌ never |
| Update check ("phone home") | ❌ never |
| Anonymous user counting | ❌ never |
| Bundled third-party trackers in the web UI | ❌ never |
| External CDN font / script loads | ❌ everything bundled |
| Bundled-dependency telemetry (litellm, ChromaDB, HuggingFace) | 🔇 disabled at startup; see `api/state.py` |

## Audit it yourself

Trust but verify:

```bash
# Should produce zero results.
grep -rEn "sentry_sdk|posthog|mixpanel|amplitude|segment\.io|datadoghq" \
  api/ ingestion/ persistence/ config/ web/src/

# Confirm telemetry-off env vars are set early.
grep -A 8 "Privacy: turn off bundled-dependency telemetry" api/state.py
```

If you ever find a network call we don't account for here, please
open a GitHub issue tagged `privacy`. We treat that as a bug, not
a feature request.

## Data retention (configurable per deploy)

| Data | Default | How to change |
|---|---|---|
| Trash bin | Items older than 30 days auto-purged | `trash.retention_days` in config |
| Audit log | Kept indefinitely | `DELETE FROM audit_log WHERE ...` (admin SQL) |
| Sessions | Browser-close OR 30 days, whichever first | `session_cookie_*` config + cookie max-age |
| Embedding cache | Persists in `storage/embedding_cache.pkl` | Delete the file to clear |
| LLM cache | `storage/llm_cache/` | Delete the directory to clear |

## Right to deletion (GDPR / 个人信息保护法)

Admin can hard-delete a user via Settings → Users → ⋯ → Delete.
The cascade:

| Table | Behaviour |
|---|---|
| `conversations` | CASCADE — deleted along with the user's chat history |
| `documents.user_id` | SET NULL — the document content stays (it's a workspace asset, not personal data); attribution is dropped |
| `files.user_id` | SET NULL — same reasoning |
| `audit_log.actor_id` | Kept as-is — audit trail integrity outranks attribution scrubbing. Admins can SQL-delete specific rows if compelled by a regulator |
| `folders.shared_with` | Swept — the user's grant entries are stripped from every folder they had access to |
| `auth_tokens` | CASCADE — every API token issued is invalidated |
| `auth_sessions` | CASCADE — every active login is killed |

For a full "right to be forgotten" request, **also** delete the
documents the user uploaded that they own (Settings → Users
surfaces the per-user document list as a follow-up cleanup pass).

## Subprocessors

We have none. OpenCraig is a self-hosted product. Your contract
relationship is:

* You ↔ DeepLethe — for source code (AGPLv3) or commercial license.
* You ↔ Whichever LLM provider you point us at — direct, per their
  terms.

DeepLethe does not see, store, or process any of your operational data.

## Encryption

* **At rest**: Application-level encryption is **not** implemented
  (and shouldn't be — it's the wrong layer). Use filesystem-level
  encryption: LUKS / dm-crypt on Linux, BitLocker on Windows. The
  Postgres + Neo4j volumes inherit whatever the host filesystem
  uses.
* **In transit, internal**: Bolt to Neo4j is unencrypted by default.
  Enable TLS via `NEO4J_dbms_ssl_*` for production deployments
  spanning hosts; for single-host docker compose deploys all traffic
  is over the local docker network and never reaches the wire.
* **In transit, external (browser)**: Deploy behind an HTTPS reverse
  proxy (nginx / Caddy / Cloudflare Tunnel). Plain HTTP is for
  localhost development only — auth cookies are
  `session_cookie_secure: true` by default to enforce this.
* **In transit, LLM API**: TLS via the provider's HTTPS endpoint.

## Threat model: what THIS document is and isn't

This page documents what OpenCraig the software does (and doesn't)
with your data. It does NOT cover:

* Your operating-system-level controls.
* Your network controls (firewalls, VPN, ingress).
* Your LLM provider's privacy posture (read theirs).
* Your operator's behaviour (a malicious admin with shell access
  can read everything; that's the unavoidable cost of self-host).

For a single-tenant compliance deployment (biotech / law / national
labs), the standard practice is:

1. Run on hardware your security team owns.
2. LUKS the disk.
3. Use a self-hosted Ollama / vLLM / on-prem LLM gateway as the
   provider so no document text crosses the network boundary.
4. Reverse-proxy with TLS + your SSO of choice (forwarded-auth
   mode integrates with oauth2-proxy / Authelia).
5. Stream audit_log to your SIEM via the daily backup or a
   read-replica.

## Updating this page

This document tracks the codebase, not aspirations. If we change
what data leaves the user's server, **this file must be updated
in the same commit** as the code change. Reviewers should reject
PRs that introduce a network call without a corresponding edit
here.
