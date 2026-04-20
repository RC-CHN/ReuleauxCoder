"""Minimal authentication for remote bootstrap and peer sessions."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from reuleauxcoder.extensions.remote_exec.errors import AuthError, RegisterRejectedError


@dataclass
class _TokenEntry:
    token: str
    expires_at: float
    used: bool = False
    peer_id: str | None = None


class TokenManager:
    """Manages bootstrap (one-time) and peer (short-lived) tokens in memory."""

    def __init__(self):
        self._bootstrap: dict[str, _TokenEntry] = {}
        self._peers: dict[str, _TokenEntry] = {}

    # ------------------------------------------------------------------
    # Bootstrap token
    # ------------------------------------------------------------------

    def issue_bootstrap_token(self, ttl_sec: int = 300) -> str:
        """Issue a new one-time bootstrap token."""
        token = "bt_" + secrets.token_urlsafe(32)
        entry = _TokenEntry(
            token=token,
            expires_at=time.time() + ttl_sec,
        )
        self._bootstrap[token] = entry
        return token

    def consume_bootstrap_token(self, token: str) -> bool:
        """Consume a bootstrap token. Returns True if valid and not expired."""
        entry = self._bootstrap.get(token)
        if entry is None:
            return False
        if entry.used:
            return False
        if time.time() > entry.expires_at:
            return False
        entry.used = True
        return True

    # ------------------------------------------------------------------
    # Peer token
    # ------------------------------------------------------------------

    def issue_peer_token(self, peer_id: str, ttl_sec: int = 3600) -> str:
        """Issue a short-lived peer session token."""
        token = "pt_" + secrets.token_urlsafe(32)
        entry = _TokenEntry(
            token=token,
            expires_at=time.time() + ttl_sec,
            peer_id=peer_id,
        )
        self._peers[token] = entry
        return token

    def verify_peer_token(self, token: str) -> str | None:
        """Verify a peer token. Returns peer_id if valid, None otherwise."""
        entry = self._peers.get(token)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            return None
        return entry.peer_id

    def revoke_peer_token(self, token: str) -> None:
        """Revoke a peer token explicitly (e.g. on disconnect)."""
        self._peers.pop(token, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _mask(self, token: str) -> str:
        """Return a masked version of the token safe for logging."""
        if len(token) <= 12:
            return "***"
        return token[:6] + "..." + token[-4:]

    def prune_expired(self) -> int:
        """Remove expired tokens. Returns count of removed entries."""
        now = time.time()
        removed = 0
        for store in (self._bootstrap, self._peers):
            expired = [k for k, v in store.items() if now > v.expires_at]
            for k in expired:
                store.pop(k, None)
                removed += 1
        return removed
