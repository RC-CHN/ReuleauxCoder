"""Cleanup orchestration for remote peers."""

from __future__ import annotations

import logging

from reuleauxcoder.extensions.remote_exec.server import RelayServer

logger = logging.getLogger(__name__)


def request_peer_cleanup(relay_server: RelayServer, peer_id: str) -> tuple[bool, list[str], str | None]:
    """Request cleanup on a remote peer.

    Returns:
        Tuple of (ok, removed_items, error_message).
    """
    try:
        result = relay_server.request_cleanup(peer_id, timeout_sec=10)
        if result.ok:
            logger.info("Cleanup succeeded for peer %s", peer_id)
        else:
            logger.warning("Cleanup failed for peer %s: %s", peer_id, result.error_message)
        return result.ok, result.removed_items, result.error_message
    except Exception as e:
        logger.warning("Cleanup request exception for peer %s: %s", peer_id, e)
        return False, [], str(e)


def cleanup_all_peers(relay_server: RelayServer) -> dict[str, tuple[bool, str | None]]:
    """Best-effort cleanup for all currently online peers.

    Returns a mapping of peer_id -> (ok, error_message).
    """
    results: dict[str, tuple[bool, str | None]] = {}
    for peer in relay_server.registry.list_online():
        ok, _, err = request_peer_cleanup(relay_server, peer.peer_id)
        results[peer.peer_id] = (ok, err)
    return results
