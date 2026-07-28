"""Keyboard routing for the production terminal UI."""

from __future__ import annotations

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from reuleauxcoder.interfaces.interactions import ConfirmRequest, ReviewRequest


def build_key_bindings(host) -> KeyBindings:
    bindings = KeyBindings()
    transcript_arrow_scroll = Condition(host._should_route_arrows_to_transcript)

    def review_stage() -> str:
        return str(
            getattr(
                getattr(host.interactor, "review_state", None),
                "stage",
                "review",
            )
        )

    binary_interaction_active = Condition(
        lambda: isinstance(host.interactor.active_request, ConfirmRequest)
        or (
            isinstance(host.interactor.active_request, ReviewRequest)
            and review_stage() == "review"
        )
    )
    review_default_active = Condition(
        lambda: isinstance(host.interactor.active_request, ReviewRequest)
        and review_stage() == "review"
    )
    review_scope_active = Condition(
        lambda: isinstance(host.interactor.active_request, ReviewRequest)
        and review_stage() == "scope"
    )
    review_subpage_active = Condition(
        lambda: isinstance(host.interactor.active_request, ReviewRequest)
        and review_stage() != "review"
    )

    @bindings.add("c-c")
    def _ctrl_c(event) -> None:
        if host.interactor.cancel_active():
            # Cancelling a modal approval must not discard the chat draft
            # that was present before the request took over the input lane.
            return
        if host.input_buffer.text:
            host.input_buffer.reset()
            host.exit_confirm = False
            return
        if host.running:
            if host.cancelling:
                host._prepare_forced_exit("forced CLI exit during active turn")
                host._closed = True
                event.app.exit()
            else:
                host.cancelling = True
                host.agent.request_stop()
                queued_steering = host._queued_steering()
                queued_commands = host._queued_commands()
                if queued_commands and queued_steering:
                    message = (
                        "Cancelling the current turn. Queued commands will run "
                        "next; queued steers will be discarded."
                    )
                elif queued_commands:
                    message = (
                        "Cancelling the current turn. Queued commands will run next."
                    )
                elif queued_steering:
                    message = (
                        "Cancelling the current turn. Queued steers will be discarded."
                    )
                else:
                    message = "Cancelling the current turn…"
                host.ui_bus.warning(message)
            return
        if host.exit_confirm:
            host._closed = True
            event.app.exit()
        else:
            host.exit_confirm = True
            host.invalidate()

    @bindings.add("escape")
    def _escape(event) -> None:  # noqa: ARG001
        host.exit_confirm = False
        host.invalidate()

    @bindings.add("pageup")
    def _page_up(event) -> None:  # noqa: ARG001
        host._scroll_transcript(-host._transcript_page_size())

    @bindings.add("pagedown")
    def _page_down(event) -> None:  # noqa: ARG001
        host._scroll_transcript(host._transcript_page_size())

    @bindings.add("up", filter=transcript_arrow_scroll)
    def _alternate_scroll_up(event) -> None:  # noqa: ARG001
        host._scroll_transcript(-3)

    @bindings.add("down", filter=transcript_arrow_scroll)
    def _alternate_scroll_down(event) -> None:  # noqa: ARG001
        host._scroll_transcript(3)

    @bindings.add("home")
    def _history_start(event) -> None:  # noqa: ARG001
        host._follow_transcript = False
        host._transcript_scroll = 0
        host.transcript_pane.vertical_scroll = 0
        host.invalidate()

    @bindings.add("end")
    def _history_end(event) -> None:  # noqa: ARG001
        host._follow_transcript = True
        host.invalidate()

    selection_active = Condition(lambda: host.selection_host.active)

    @bindings.add("up", filter=selection_active)
    def _selection_up(event) -> None:  # noqa: ARG001
        host.selection_host.move(-1)

    @bindings.add("down", filter=selection_active)
    def _selection_down(event) -> None:  # noqa: ARG001
        host.selection_host.move(1)

    @bindings.add("enter", filter=selection_active)
    def _selection_enter(event) -> None:  # noqa: ARG001
        host.selection_host.confirm()

    @bindings.add("escape", filter=selection_active)
    def _selection_escape(event) -> None:  # noqa: ARG001
        host.selection_host.close()

    popup_visible = Condition(lambda: bool(host._popup_candidates()))

    @bindings.add("up", filter=popup_visible)
    def _popup_up(event) -> None:  # noqa: ARG001
        candidates = host._popup_candidates()
        if candidates:
            host._popup_index = (host._popup_index - 1) % len(candidates)
            host.invalidate()

    @bindings.add("down", filter=popup_visible)
    def _popup_down(event) -> None:  # noqa: ARG001
        candidates = host._popup_candidates()
        if candidates:
            host._popup_index = (host._popup_index + 1) % len(candidates)
            host.invalidate()

    @bindings.add("tab", filter=popup_visible)
    def _popup_tab(event) -> None:  # noqa: ARG001
        host._popup_adopt()

    @bindings.add("escape", filter=popup_visible)
    def _popup_escape(event) -> None:  # noqa: ARG001
        host._popup_dismissed = True
        host.invalidate()

    @bindings.add("y", filter=binary_interaction_active)
    def _interaction_yes(event) -> None:  # noqa: ARG001
        host.interactor.submit("y")
        host.invalidate()

    @bindings.add("n", filter=binary_interaction_active)
    def _interaction_no(event) -> None:  # noqa: ARG001
        host.interactor.submit("n")
        host.invalidate()

    @bindings.add("s", filter=review_default_active)
    def _interaction_session(event) -> None:  # noqa: ARG001
        host.interactor.submit("s")
        host.invalidate()

    @bindings.add("f", filter=review_default_active)
    def _interaction_feedback(event) -> None:  # noqa: ARG001
        host.interactor.submit("f")
        host.invalidate()

    @bindings.add("up", filter=review_scope_active)
    def _review_scope_up(event) -> None:  # noqa: ARG001
        host.interactor.move_review_selection(-1)

    @bindings.add("down", filter=review_scope_active)
    def _review_scope_down(event) -> None:  # noqa: ARG001
        host.interactor.move_review_selection(1)

    @bindings.add("escape", filter=review_subpage_active)
    def _review_scope_back(event) -> None:  # noqa: ARG001
        host.interactor.back_review()

    for number in "123456789":

        @bindings.add(number, filter=review_scope_active)
        def _review_scope_number(event, value=number) -> None:  # noqa: B023
            host.interactor.submit(value)
            host.invalidate()

    @bindings.add("f2")
    def _toggle_header(event) -> None:  # noqa: ARG001
        host.session_header_expanded = not host.session_header_expanded
        host.invalidate()

    return bindings



__all__ = ["build_key_bindings"]
