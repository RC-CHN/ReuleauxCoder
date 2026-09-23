import io
import json
from types import SimpleNamespace

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.session.models import MAX_RECENT_CONVERSATION_BYTES
from reuleauxcoder.infrastructure.rpc.peer import RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import StreamTransport
from reuleauxcoder.interfaces.entrypoint.rpc import create_server


def test_initialize_frames_oversized_restored_content_without_losing_canonical_text(
    tmp_path, monkeypatch
):
    config = Config(api_key="test", session_auto_save=False)
    agent = Agent(
        SimpleNamespace(model="test", debug_trace=False), tools=[], config=config
    )
    text = "中文🙂\"\\\n" * 1_500_000
    assert len(text.encode("utf-8")) > StreamTransport.MAX_MESSAGE_BYTES
    agent.messages.extend([
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": text},
    ])
    # Token accounting is independent of the restore-preview transport budget.
    monkeypatch.setattr(agent.context, "predict_request_tokens", lambda messages: 0)
    output = io.BytesIO()
    peer = RpcPeer(StreamTransport(io.BytesIO(), output))
    profile = UIProfile("tui", "TUI", frozenset(UICapability))
    ctx = SimpleNamespace(
        agent=agent, config=config, ui_bus=UIEventBus(),
        action_registry=create_builtin_action_registry(), sessions_dir=tmp_path,
        session_exit_time=None, skills_service=None,
    )
    server = create_server(ctx, peer, profile)
    try:
        peer._respond({
            "jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {"version": 1, "profile": encode(profile)},
        })
        frame = output.getvalue()
        assert len(frame) < StreamTransport.MAX_MESSAGE_BYTES
        info = decode(json.loads(frame)["result"])
        preview = info["recent_conversation"]
        assert (
            len(json.dumps(preview, ensure_ascii=False).encode("utf-8"))
            < MAX_RECENT_CONVERSATION_BYTES
        )
        assert preview[0]["content"] == "Question"
        assert "Preview truncated" in preview[-1]["content"]
        assert agent.messages[-1]["content"] == text
        assert not peer.closed.is_set()
    finally:
        server.shutdown()
        peer.close()
        agent.unbind_session_persistence()
