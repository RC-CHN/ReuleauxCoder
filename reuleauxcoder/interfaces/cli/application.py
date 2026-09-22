"""CLI view lifecycle over a connected runtime, independent of backend ownership."""

import time

from reuleauxcoder.interfaces.cli.output import CLIOutputCoordinator
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.presentation import PresentationPolicy


def run_cli(runtime, ui_bus, registration, *, prompt=None, history_file=None):
    renderer = CLIRenderer(
        view_registry=registration.view_registry,
        policy=PresentationPolicy.from_mapping(runtime.info["presentation"]),
        root_agent_id=runtime.state.agent_id,
        live_activity=bool(prompt),
    )
    output = CLIOutputCoordinator(renderer)
    ui_bus.subscribe(output.on_ui_event)
    try:
        if runtime.info["host_mode"]:
            for event in runtime.info["startup_events"]:
                ui_bus.emit(event)
            ui_bus.info("Remote relay host mode active. Press Ctrl+C to stop.")
            try:
                while not runtime.peer.closed.is_set():
                    time.sleep(0.1)
                    output.drain()
            except KeyboardInterrupt:
                pass
            return
        if not runtime.info["model_configured"]:
            ui_bus.error("No API key found in config.yaml.")
            output.drain()
            return 1
        if prompt:
            runtime.submit(prompt)
            runtime.wait_idle(pump=output.drain)
            output.drain()
        else:
            run_repl(
                runtime,
                ui_bus,
                output,
                registration.interactor,
                runtime.info["startup_events"],
                history_file=history_file,
            )
    finally:
        ui_bus.unsubscribe(output.on_ui_event)
        output.close()
