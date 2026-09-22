"""Linear CLI over the same JSON-RPC runtime used by the React TUI."""

import sys
import threading
from pathlib import Path

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.document import Document

from reuleauxcoder import __version__
from reuleauxcoder.app.commands.specs import TriggerKind
from reuleauxcoder.app.rpc.client import RuntimeClient
from reuleauxcoder.app.ui_events import UIEvent, UIEventBus
from reuleauxcoder.interfaces.cli.input import CLIInput, PromptAction
from reuleauxcoder.interfaces.cli.images import ImagePaste
from reuleauxcoder.interfaces.cli.registration import CLI_PROFILE
from reuleauxcoder.interfaces.cli.render import show_banner
from reuleauxcoder.domain.images import ChatInput
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def run_repl(
    runtime: RuntimeClient,
    ui_bus: UIEventBus,
    output_coordinator,
    interaction_coordinator,
    startup_events: tuple[UIEvent, ...] = (),
    *,
    history_file: str | None = None,
) -> None:
    show_banner(
        runtime.state.model,
        runtime.info["base_url"],
        __version__,
        startup_events=startup_events,
        workspace=runtime.state.workspace,
        runtime_environment=runtime.info.get("runtime_environment"),
    )
    output = output_coordinator
    output.renderer.restore(runtime.info, runtime.state)
    exited = threading.Event()

    def completed(result):
        if result.control == "exit":
            exited.set()

    runtime.on_completed = completed
    runtime.ready()
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    editor = None
    images = []
    image_number = 0
    image_session = (runtime.state.session_id, runtime.state.session_generation)
    if interactive:
        words = sorted(
            {
                trigger.value
                for action in runtime.catalog.iter_actions(CLI_PROFILE)
                for trigger in action.matching_triggers(
                    CLI_PROFILE, kind=TriggerKind.SLASH
                )
            }
        )
        words.extend(["/attach", "/detach"])
        history_path = Path(history_file or ".rcoder/history").expanduser()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        editor = CLIInput(
            runtime,
            output,
            exited,
            history=FileHistory(str(history_path)),
            completer=WordCompleter(words, sentence=True),
        )
        editor.image_labels = lambda: tuple(label for label, _ in images)

    def add_image(image):
        nonlocal image_number
        image_number += 1
        label = f"[Image #{image_number}]"
        images.append((label, image))
        if editor:
            draft = editor.draft
            editor.draft = Document(
                draft.text[: draft.cursor_position]
                + label
                + " "
                + draft.text[draft.cursor_position :],
                draft.cursor_position + len(label) + 1,
            )
        ui_bus.info(
            f"{label} {image.name} ({image.width}x{image.height}, {image.size_bytes} bytes)"
        )

    try:
        while not exited.is_set():
            output.drain()
            current = (runtime.state.session_id, runtime.state.session_generation)
            if current != image_session:
                if images:
                    ui_bus.info(
                        "Session changed; draft images cleared. Attach them again to use them here."
                    )
                    if editor:
                        text = editor.draft.text
                        for label, _ in images:
                            text = text.replace(label, "")
                        editor.draft = Document(text)
                images.clear()
                image_number = 0
                image_session = current
            try:
                with interaction_coordinator.foreground_input() as available:
                    if not available:
                        break
                    value = editor.read() if editor else input()
            except EOFError:
                break
            except KeyboardInterrupt:
                if runtime.state.running:
                    runtime.interrupt()
                    continue
                break
            output.drain()
            if value is PromptAction.EXIT:
                break
            if value is PromptAction.INTERACTION:
                runtime.pump_interactions()
            elif value is PromptAction.INTERRUPT:
                runtime.interrupt()
            elif value is PromptAction.DETAILS:
                runtime.refresh()
                output.renderer.show_details(runtime.state, startup_events)
            elif value is PromptAction.TOOLS:
                output.renderer.show_tools()
            elif isinstance(value, ImagePaste):
                try:
                    add_image(runtime.attach_image(str(value.path)))
                except (OSError, ValueError, RpcError) as error:
                    ui_bus.warning(str(error))
                    draft = editor.draft
                    editor.draft = Document(
                        draft.text[: draft.cursor_position]
                        + value.text
                        + draft.text[draft.cursor_position :],
                        draft.cursor_position + len(value.text),
                    )
            elif isinstance(value, str) and (value.strip() or images):
                text = value.strip()
                try:
                    command, _, argument = text.partition(" ")
                    if command == "/attach":
                        path = argument.strip()
                        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
                            path = path[1:-1]
                        if not path:
                            raise ValueError(
                                "Usage: /attach <frontend-local image path>"
                            )
                        add_image(runtime.attach_image(path))
                        continue
                    if command == "/detach":
                        if argument.strip() == "all":
                            images.clear()
                        elif any(
                            label == f"[Image #{argument.strip()}]"
                            for label, _ in images
                        ):
                            images[:] = [
                                (label, image)
                                for label, image in images
                                if label != f"[Image #{argument.strip()}]"
                            ]
                        else:
                            raise ValueError("Usage: /detach <image number|all>")
                        ui_bus.info(f"{len(images)} draft images remaining.")
                        if editor:
                            editor.draft = Document(
                                " ".join(label for label, _ in images)
                            )
                        continue
                    if editor and not text.startswith("/"):
                        images[:] = [
                            (label, image) for label, image in images if label in text
                        ]
                    sending_images = bool(images) and not text.startswith("/")
                    submission = (
                        ChatInput(
                            text,
                            tuple(image for _, image in images),
                            *image_session,
                            tuple(label for label, _ in images) if editor else (),
                        )
                        if sending_images
                        else text
                    )
                    admission = runtime.submit(submission)
                except (OSError, ValueError, RpcError) as error:
                    ui_bus.warning(str(error))
                    if editor:
                        editor.draft = Document(value)
                    continue
                if admission.status != "rejected" and sending_images:
                    images.clear()
                    image_number = 0
                if admission.status == "steering":
                    ui_bus.info(f"Queued: {value.strip()}")
                elif admission.status == "rejected":
                    ui_bus.warning(
                        "Input was not accepted; try again when the turn finishes."
                    )
                    if editor:
                        editor.draft = Document(value)
                if not interactive:
                    try:
                        runtime.wait_idle(pump=output.drain)
                    except KeyboardInterrupt:
                        runtime.interrupt()
                        runtime.wait_idle(pump=output.drain)
    finally:
        runtime.shutdown()
        output.drain()
        if runtime.state.exit_saved_session_id:
            ui_bus.success(f"Session saved: {runtime.state.exit_saved_session_id}.")
