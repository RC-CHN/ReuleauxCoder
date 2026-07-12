from __future__ import annotations

from pathlib import Path

import yaml

from reuleauxcoder.extensions.remote_exec.artifacts import MAX_PEER_ARTIFACT_BYTES


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TARGETS = {
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("windows", "amd64"),
    ("windows", "arm64"),
}


def _workflow(name: str) -> tuple[str, dict]:
    source = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def _targets(job: dict) -> set[tuple[str, str]]:
    return {
        (entry["goos"], entry["goarch"])
        for entry in job["strategy"]["matrix"]["include"]
    }


def test_ci_runs_go_contract_tests_and_cross_builds_all_peer_targets() -> None:
    source, workflow = _workflow("ci.yml")
    jobs = workflow["jobs"]

    assert "go test ./..." in source
    assert "go list -m all" in source
    assert _targets(jobs["peer-build"]) == EXPECTED_TARGETS
    assert str(MAX_PEER_ARTIFACT_BYTES) in source
    assert "actions/upload-artifact@" in source
    assert "lsp-integration" in jobs
    assert "typescript@7" in (
        ROOT / "reuleauxcoder" / "extensions" / "lsp" / "registry.py"
    ).read_text(encoding="utf-8")
    assert "@typescript/typescript6@6" in source
    assert "RCODER_RUN_LSP_INTEGRATION" in source


def test_release_publishes_cross_platform_peers_with_checksum_manifest() -> None:
    source, workflow = _workflow("release.yml")
    jobs = workflow["jobs"]

    assert _targets(jobs["peer-artifacts"]) == EXPECTED_TARGETS
    assert str(MAX_PEER_ARTIFACT_BYTES) in source
    assert "go test ./..." in source
    assert "sha256sum rcoder-peer-* > SHA256SUMS" in source
    assert "dist/peer/*" in source
