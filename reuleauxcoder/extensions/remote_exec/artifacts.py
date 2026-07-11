"""Integrity and maintenance budgets for downloadable peer artifacts."""

from __future__ import annotations

from hashlib import sha256


MAX_PEER_ARTIFACT_BYTES = 20 * 1024 * 1024
PEER_ARTIFACT_SHA256_HEADER = "X-ReuleauxCoder-SHA256"


def peer_artifact_sha256(content: bytes) -> str:
    return sha256(content).hexdigest()


def validate_peer_artifact_size(content: bytes) -> None:
    if len(content) > MAX_PEER_ARTIFACT_BYTES:
        raise ValueError(
            "peer artifact exceeds size budget "
            f"({len(content)} > {MAX_PEER_ARTIFACT_BYTES} bytes)"
        )
