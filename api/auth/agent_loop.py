"""
Agent-loopback bearer — deterministic, per-user, no DB row.

The Claude Agent SDK's bundled CLI calls back into our LLM proxy +
MCP server during a turn. Those endpoints sit behind the same auth
middleware as everything else, and the agent's outbound HTTP from
a subprocess can't carry the user's web session cookie. So each
chat turn needs to hand the SDK a bearer that authenticates as the
current user.

Older design: mint a fresh ``AuthToken`` row at first chat per
backend process, cache the raw value on AppState, reuse for the
process lifetime. Problem: every process restart re-minted, and
those rows accumulated forever (44 ``agent-loop`` rows after a few
weeks of dev). The hash-only DB columns mean we couldn't recover
the raw on restart, so the only way to "reuse" was to keep raw in
memory and lose it on restart.

This module replaces that with a derive-don't-store scheme:

  Token = ``aloop_<user_id>_<HMAC_SHA256(server_secret, user_id)[:32]>``

  * No DB row — verifying the token is HMAC + a user lookup.
  * Restart-safe — the secret is loaded from disk (auto-generated
    on first read) so the same user always gets the same bearer.
  * Constant-time — HMAC comparison via ``hmac.compare_digest``.
  * Revocable — rotate the server secret (drops ALL agent-loop
    tokens at once; coarse but acceptable for an internal cred).

The auth middleware short-circuits on the ``aloop_`` prefix BEFORE
the normal token-hash lookup, so this code path never touches the
``auth_tokens`` table.

Threat model: someone with a copy of ``storage/.agent_loop_secret``
can mint any user's agent-loop bearer. Treat the file like any
other server-side credential — file mode 0600, gitignored,
included in backup-but-don't-share lists. If it leaks, regenerate
(``rm + restart`` regenerates lazily). Compromise of an in-process
``AppState._agent_token_cache`` was the same threat surface in the
old design — net security posture unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path

# Token prefix — distinct enough that the normal token-hash path
# can recognise + skip these. ``aloop_`` is short, lowercase,
# unlikely to collide with user-generated token names.
TOKEN_PREFIX = "aloop_"

# How many hex chars of HMAC we keep on the wire. 32 hex chars = 128
# bits — well past brute-force territory for the lifetime of any
# server-side secret.
_HMAC_HEX_LEN = 32

# In-memory cache of the loaded secret. Read once per process. The
# lock prevents two concurrent ensure_secret() calls from each
# generating a different secret on a fresh install.
_SECRET_LOCK = threading.Lock()
_SECRET: bytes | None = None


# Storage location — the project's ``storage/`` root, sibling of
# ``forgerag.db`` etc. Hard-coded path keeps this module independent
# of agent / answering config; the server secret isn't a user-visible
# artifact and doesn't need to follow the configurable storage roots.
_SECRET_FILE = Path("./storage/.agent_loop_secret").resolve()


def _ensure_secret() -> bytes:
    """Load the persisted server secret, generating it on first run.

    Returns the raw bytes. File mode is set to 0600 on creation so
    only the backend process owner can read it (Linux/macOS;
    Windows ACLs are best-effort — the file is still gitignored).
    """
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    with _SECRET_LOCK:
        if _SECRET is not None:
            return _SECRET
        path = _SECRET_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _SECRET = path.read_bytes().strip()
            if not _SECRET:
                # Empty file → treat as missing, regenerate
                _SECRET = None
        if _SECRET is None:
            _SECRET = secrets.token_bytes(32)
            path.write_bytes(_SECRET)
            try:
                # 0600 — owner read/write only. POSIX-only; on
                # Windows os.chmod has limited effect but the file
                # is still .-prefixed + gitignored.
                os.chmod(path, 0o600)
            except OSError:
                pass
        return _SECRET


def mint_token(user_id: str) -> str:
    """Return the deterministic agent-loop bearer for ``user_id``.

    Same input → same output. Idempotent. Doesn't hit the DB.
    """
    secret = _ensure_secret()
    mac = hmac.new(secret, user_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{TOKEN_PREFIX}{user_id}_{mac[:_HMAC_HEX_LEN]}"


def parse_and_verify(raw_token: str) -> str | None:
    """Return the user_id if ``raw_token`` is a valid agent-loop
    bearer; ``None`` otherwise.

    The auth middleware uses this BEFORE the normal token-hash
    lookup so the ``auth_tokens`` table never gets queried for
    these tokens.
    """
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        return None
    rest = raw_token[len(TOKEN_PREFIX):]
    # Format: ``<user_id>_<hmac_hex>``. user_id is hex too in
    # practice (uuid4().hex[:N]) but we don't enforce that — split
    # on the LAST underscore so any future user_id format that
    # happens to contain underscores still parses.
    if "_" not in rest:
        return None
    user_id, mac_hex = rest.rsplit("_", 1)
    if not user_id or len(mac_hex) != _HMAC_HEX_LEN:
        return None
    secret = _ensure_secret()
    expected = hmac.new(
        secret, user_id.encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:_HMAC_HEX_LEN]
    if not hmac.compare_digest(expected, mac_hex):
        return None
    return user_id


def reset_secret_for_tests() -> None:
    """Test hook — clears the in-memory cache so the next call
    re-reads from disk. Production code never calls this."""
    global _SECRET
    with _SECRET_LOCK:
        _SECRET = None
