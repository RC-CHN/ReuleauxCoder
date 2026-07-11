"""Tests for remote execution token management."""

from __future__ import annotations

import time

from reuleauxcoder.extensions.remote_exec.auth import TokenManager


class TestBootstrapToken:
    def test_issue_and_consume(self) -> None:
        tm = TokenManager()
        token = tm.issue_bootstrap_token(ttl_sec=300)
        assert token.startswith("bt_")
        assert tm.consume_bootstrap_token(token) is True

    def test_consume_twice_fails(self) -> None:
        tm = TokenManager()
        token = tm.issue_bootstrap_token(ttl_sec=300)
        assert tm.consume_bootstrap_token(token) is True
        assert tm.consume_bootstrap_token(token) is False

    def test_expired_token_fails(self) -> None:
        tm = TokenManager()
        token = tm.issue_bootstrap_token(ttl_sec=0)
        time.sleep(0.05)
        assert tm.consume_bootstrap_token(token) is False

    def test_unknown_token_fails(self) -> None:
        tm = TokenManager()
        assert tm.consume_bootstrap_token("bt_nope") is False

    def test_no_plaintext_in_mask(self) -> None:
        tm = TokenManager()
        token = tm.issue_bootstrap_token()
        masked = tm._mask(token)
        assert token not in masked
        assert "..." in masked


class TestPeerToken:
    def test_issue_and_verify(self) -> None:
        tm = TokenManager()
        token = tm.issue_peer_token("peer-1", ttl_sec=300)
        assert token.startswith("pt_")
        assert tm.verify_peer_token(token) == "peer-1"

    def test_expired_peer_token(self) -> None:
        tm = TokenManager()
        token = tm.issue_peer_token("peer-1", ttl_sec=0)
        time.sleep(0.05)
        assert tm.verify_peer_token(token) is None

    def test_revoke_peer_token(self) -> None:
        tm = TokenManager()
        token = tm.issue_peer_token("peer-1", ttl_sec=300)
        tm.revoke_peer_token(token)
        assert tm.verify_peer_token(token) is None

    def test_unknown_peer_token(self) -> None:
        tm = TokenManager()
        assert tm.verify_peer_token("pt_nope") is None

    def test_refresh_extends_same_token_lease(self, monkeypatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(
            "reuleauxcoder.extensions.remote_exec.auth.time.time", lambda: now[0]
        )
        tm = TokenManager()
        token = tm.issue_peer_token("peer-1", ttl_sec=10)
        now[0] = 1009.0

        assert tm.refresh_peer_token(token, ttl_sec=20) == "peer-1"
        now[0] = 1028.0
        assert tm.verify_peer_token(token) == "peer-1"

    def test_refresh_allows_bounded_grace_only(self, monkeypatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(
            "reuleauxcoder.extensions.remote_exec.auth.time.time", lambda: now[0]
        )
        tm = TokenManager()
        token = tm.issue_peer_token("peer-1", ttl_sec=10)
        now[0] = 1015.0
        assert tm.refresh_peer_token(token, ttl_sec=10, grace_sec=5) == "peer-1"

        second = tm.issue_peer_token("peer-2", ttl_sec=10)
        now[0] = 1031.0
        assert tm.refresh_peer_token(second, ttl_sec=10, grace_sec=5) is None


class TestPruneExpired:
    def test_removes_expired_both_stores(self) -> None:
        tm = TokenManager()
        bt = tm.issue_bootstrap_token(ttl_sec=0)
        pt = tm.issue_peer_token("p1", ttl_sec=0)
        time.sleep(0.05)
        removed = tm.prune_expired()
        assert removed == 2
        assert tm.consume_bootstrap_token(bt) is False
        assert tm.verify_peer_token(pt) is None

    def test_keeps_valid(self) -> None:
        tm = TokenManager()
        bt = tm.issue_bootstrap_token(ttl_sec=3600)
        pt = tm.issue_peer_token("p1", ttl_sec=3600)
        removed = tm.prune_expired()
        assert removed == 0
        assert tm.consume_bootstrap_token(bt) is True
        assert tm.verify_peer_token(pt) == "p1"
