"""Approval persistence compatibility wrappers over the persistence layer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)


def get_workspace_config_path() -> Path:
    """Return the writable workspace config path."""
    return WorkspaceConfigStore().path


def approval_rule_to_dict(rule: ApprovalRuleConfig) -> dict:
    """Serialize an approval rule, dropping empty fields."""
    return WorkspaceConfigStore.approval_rule_to_dict(rule)


def save_approval_config(approval: ApprovalConfig, path: Optional[Path] = None) -> Path:
    """Persist approval config into workspace config.yaml."""
    return WorkspaceConfigStore(path).save_approval_config(approval)
