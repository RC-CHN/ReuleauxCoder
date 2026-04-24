"""In-memory peer registry with heartbeat tracking."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PeerInfo:
    """Runtime information about a connected peer."""

    peer_id: str
    connected_at: float
    last_seen_at: float
    capabilities: list[str] = field(default_factory=list)
    cwd: str = "."
    workspace_root: str | None = None
    status: str = "online"
    meta: dict[str, Any] = field(default_factory=dict)
    conn: Any = None  # opaque transport handle


class PeerRegistry:
    """Registry of connected peers. MVP: single peer default selection."""

    def __init__(self, heartbeat_timeout_sec: float = 30.0):
        self._peers: dict[str, PeerInfo] = {}
        self._heartbeat_timeout_sec = heartbeat_timeout_sec

    def register(
        self,
        meta: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> str:
        """Register a new peer and return its generated peer_id."""
        peer_id = str(uuid.uuid4())
        now = time.time()
        info = PeerInfo(
            peer_id=peer_id,
            connected_at=now,
            last_seen_at=now,
            capabilities=list(meta.get("capabilities", [])) if meta else [],
            cwd=meta.get("cwd", ".") if meta else ".",
            workspace_root=meta.get("workspace_root") if meta else None,
            status="online",
            meta=meta or {},
            conn=conn,
        )
        self._peers[peer_id] = info
        return peer_id

    def update_heartbeat(self, peer_id: str) -> bool:
        """Update last_seen_at for a peer. Returns False if peer not found."""
        info = self._peers.get(peer_id)
        if info is None:
            return False
        info.last_seen_at = time.time()
        if info.status != "online":
            info.status = "online"
        return True

    def mark_disconnected(
        self, peer_id: str, reason: str = "unknown"
    ) -> PeerInfo | None:
        """Mark a peer as offline. Returns the peer info if found."""
        info = self._peers.get(peer_id)
        if info is None:
            return None
        info.status = "offline"
        info.meta["disconnect_reason"] = reason
        return info

    def get(self, peer_id: str) -> PeerInfo | None:
        """Get peer info. Returns None if not found or marked offline."""
        info = self._peers.get(peer_id)
        if info is None or info.status != "online":
            return None
        return info

    def pick_default_peer(self) -> PeerInfo | None:
        """MVP: return the single online peer, or None."""
        online = [p for p in self._peers.values() if p.status == "online"]
        if not online:
            return None
        if len(online) == 1:
            return online[0]
        # MVP 不处理多 peer 选择，返回第一个
        return online[0]

    def list_online(self) -> list[PeerInfo]:
        """Return all currently online peers."""
        return [p for p in self._peers.values() if p.status == "online"]

    def prune_stale(self) -> list[str]:
        """Mark peers as offline if heartbeat timed out. Returns affected peer_ids."""
        now = time.time()
        stale: list[str] = []
        for peer_id, info in self._peers.items():
            if (
                info.status == "online"
                and (now - info.last_seen_at) > self._heartbeat_timeout_sec
            ):
                info.status = "offline"
                info.meta["disconnect_reason"] = "heartbeat_timeout"
                stale.append(peer_id)
        return stale

    def remove(self, peer_id: str) -> bool:
        """Permanently remove a peer from registry."""
        return self._peers.pop(peer_id, None) is not None
