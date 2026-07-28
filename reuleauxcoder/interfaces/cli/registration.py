"""CLI UI registration and composition helpers."""

from __future__ import annotations

from reuleauxcoder.interfaces.cli.interactor import CLIUIInteractor
from reuleauxcoder.interfaces.cli.views.registry import create_cli_view_registry
from reuleauxcoder.interfaces.events import UIEventBus
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile, UIRegistration
from reuleauxcoder.app.runtime.interactions import InteractionCoordinator


CLI_PROFILE = UIProfile(
    ui_id="cli",
    display_name="Command Line Interface",
    capabilities=frozenset(
        {
            UICapability.TEXT_INPUT,
            UICapability.STREAM_OUTPUT,
            UICapability.TEXT_SELECT,
            UICapability.DIFF_REVIEW,
            UICapability.SECURE_TEXT_INPUT,
        }
    ),
)

REMOTE_CLI_PROFILE = UIProfile(
    ui_id="cli",
    display_name="Remote Command Line Interface",
    capabilities=CLI_PROFILE.capabilities - {UICapability.SECURE_TEXT_INPUT},
)


def create_cli_registration(ui_bus: UIEventBus) -> UIRegistration:
    """Build the CLI UI registration for the current process."""
    return UIRegistration(
        profile=CLI_PROFILE,
        view_registry=create_cli_view_registry(),
        interactor=InteractionCoordinator(CLIUIInteractor(ui_bus)),
    )
