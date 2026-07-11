from reuleauxcoder.app.commands.view_models import (
    MCPServersViewModel,
    SessionsViewModel,
    view_model_from_payload,
)


def test_sessions_payload_round_trip_uses_typed_model() -> None:
    payload = {
        "fingerprint": "local",
        "show_all": False,
        "sessions": [
            {
                "id": "s1",
                "model": "gpt",
                "saved_at": "today",
                "preview": "hello",
                "fingerprint": "local",
            }
        ],
    }

    model = view_model_from_payload("sessions", payload)

    assert isinstance(model, SessionsViewModel)
    assert model.sessions[0].session_id == "s1"
    assert model.to_payload() == payload


def test_mcp_payload_round_trip_uses_typed_model() -> None:
    payload = {
        "servers": [
            {"name": "demo", "enabled": True, "runtime_connected": False}
        ]
    }

    model = view_model_from_payload("mcp_servers", payload)

    assert isinstance(model, MCPServersViewModel)
    assert model.servers[0].name == "demo"
    assert model.to_payload() == payload
