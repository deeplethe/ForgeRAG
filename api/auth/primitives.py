"""
Low-level auth primitives: argon2id passwords + sha256-hashed tokens.

Design notes:
  * Passwords use argon2id (``argon2-cffi``). Modern, sane defaults, no
    hand-tuning for most of us — the library picks reasonable memory/time
    costs based on current hardware guidance.
  * API bearer tokens are 256-bit random values encoded as ``Forge_<b58>``.
    We store only ``sha256(raw)`` hex in the DB. SHA-256 (not argon2) is
    fine here because tokens are full-entropy random, not user-chosen
    passwords — the speed matters for per-request lookup.
  * Session IDs are 256-bit random too, encoded as hex for simple
    URL-safety inside a cookie.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# One shared hasher instance — thread-safe per the argon2-cffi docs.
_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Passwords (argon2id)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Return a self-describing argon2id hash ("$argon2id$v=19$m=...$...")."""
    return _ph.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    """Constant-time verify. Returns False on any mismatch or parse error."""
    try:
        return _ph.verify(stored_hash, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the hash parameters are weaker than current defaults —
    caller should re-hash on next successful login to migrate forward."""
    try:
        return _ph.check_needs_rehash(stored_hash)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tokens + sessions (random + sha256)
# ---------------------------------------------------------------------------


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_encode(raw: bytes) -> str:
    # Tiny base58 encoder — avoid extra dependency for a 32-byte value.
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    # Preserve leading zeros
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


def generate_sk() -> str:
    """Mint a new bearer token: ``Forge_<b58(32 random bytes)>``.
    ~44 chars, URL-safe, no ambiguous 0/O/I/l characters."""
    return "Forge_" + _b58_encode(secrets.token_bytes(32))


def hash_sk(raw: str) -> str:
    """sha256 hex of the raw token — what we store in ``auth_tokens.token_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_prefix(raw: str) -> str:
    """First 8 hex chars of sha256 — UI fingerprint so users can tell
    tokens apart without seeing any of the raw material."""
    return hash_sk(raw)[:8]


def generate_session_id() -> str:
    """64-hex-char session cookie value — 256 bits of entropy."""
    return secrets.token_hex(32)
