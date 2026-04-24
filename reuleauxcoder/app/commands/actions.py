"""Action registry entrypoint.

This module intentionally keeps no static action table.
Use create_builtin_action_registry() for explicit instantiation.
Previously ACTION_REGISTRY was a module-level singleton built eagerly at import time.
"""

from __future__ import annotations

from reuleauxcoder.app.commands.loader import create_builtin_action_registry
