"""Action registry entrypoint.

This module intentionally keeps no static action table.
Builtin command extensions are dynamically loaded through the loader.
"""

from __future__ import annotations

from reuleauxcoder.app.commands.loader import create_builtin_action_registry

ACTION_REGISTRY = create_builtin_action_registry()
