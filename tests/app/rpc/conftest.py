from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.interaction_contracts import (
    ConfirmResponse,
    InputTextResponse,
    ReviewResponse,
)
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.interfaces.entrypoint.rpc import connect_local


class FakeLLM:
    model = "test-model"
    debug_trace = False

    def reconfigure(self, **values):
        self.__dict__.update(values)


@pytest.fixture
def runtime(tmp_path, request):
    config = Config(
        api_key="test", session_auto_save=False, history_file=str(tmp_path / "history")
    )
    loop = SimpleNamespace(run=lambda: "done")
    agent = Agent(FakeLLM(), tools=[], config=config, loop=loop)
    agent.current_session_id = "test-session"
    frontend = UIEventBus()
    interactor = SimpleNamespace(
        confirm=lambda request: ConfirmResponse(True),
        input_text=lambda request: InputTextResponse("answer"),
        review=lambda request: ReviewResponse(True, action="allow_once"),
        cancel=lambda request_id: None,
    )
    ctx = SimpleNamespace(
        agent=agent,
        config=config,
        ui_bus=UIEventBus(),
        action_registry=create_builtin_action_registry(),
        sessions_dir=tmp_path,
        session_exit_time=None,
        skills_service=None,
    )
    connection = connect_local(
        ctx,
        UIProfile("tui", "TUI", frozenset(UICapability)),
        frontend,
        interactor,
        activate=getattr(request, "param", True),
    )
    try:
        yield SimpleNamespace(
            client=connection.client,
            server=connection.server,
            agent=agent,
            loop=loop,
            bus=frontend,
            interactor=interactor,
            config=config,
        )
    finally:
        connection.close()
        agent.unbind_session_persistence()
