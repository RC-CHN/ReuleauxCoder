"""Tests for remote execution token management."""

from __future__ import annotations

import time

import pytest

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
