"""Tests for remote execution peer registry."""

from __future__ import annotations

import time

import pytest

from reuleauxcoder.extensions.remote_exec.peer_registry import PeerInfo, PeerRegistry


class TestPeerRegistry:
    def test_register_returns_id(self) -> None:
        reg = PeerRegistry()
        pid = reg.register(meta={"cwd": "/tmp"})
        assert pid
        assert reg.get(pid) is not None

    def test_get_online(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        info = reg.get(pid)
        assert info is not None
        assert info.status == "online"

    def test_get_offline_returns_none(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        reg.mark_disconnected(pid)
        assert reg.get(pid) is None

    def test_update_heartbeat(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        before = reg.get(pid).last_seen_at
        time.sleep(0.02)
        assert reg.update_heartbeat(pid) is True
        after = reg.get(pid).last_seen_at
        assert after > before

    def test_update_heartbeat_unknown_peer(self) -> None:
        reg = PeerRegistry()
        assert reg.update_heartbeat("nope") is False

    def test_mark_disconnected(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        info = reg.mark_disconnected(pid, "timeout")
        assert info is not None
        assert info.status == "offline"
        assert info.meta["disconnect_reason"] == "timeout"

    def test_pick_default_peer_single(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        picked = reg.pick_default_peer()
        assert picked is not None
        assert picked.peer_id == pid

    def test_pick_default_peer_none(self) -> None:
        reg = PeerRegistry()
        assert reg.pick_default_peer() is None

    def test_list_online(self) -> None:
        reg = PeerRegistry()
        p1 = reg.register()
        p2 = reg.register()
        reg.mark_disconnected(p2)
        online = reg.list_online()
        assert len(online) == 1
        assert online[0].peer_id == p1

    def test_prune_stale(self) -> None:
        reg = PeerRegistry(heartbeat_timeout_sec=0.05)
        pid = reg.register()
        time.sleep(0.07)
        stale = reg.prune_stale()
        assert stale == [pid]
        assert reg.get(pid) is None

    def test_remove(self) -> None:
        reg = PeerRegistry()
        pid = reg.register()
        assert reg.remove(pid) is True
        assert reg.get(pid) is None
        assert reg.remove(pid) is False
