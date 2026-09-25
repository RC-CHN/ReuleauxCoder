"""Exercise bounded probes against local provider transports, never paid models."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from reuleauxcoder.services.config.probe import run_probe


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    captured = []
    response = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            self.send_response(response["status"])
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if response["status"] != 200:
                self.wfile.write(b'{"error":{"message":"secret-server-diagnostic"}}')
                return
            if self.path.endswith("/responses"):
                events = [
                    {
                        "type": "response.output_text.delta",
                        "delta": "OK",
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": "test",
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [],
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    },
                ]
            elif self.path.endswith("/messages"):
                events = [
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "OK"},
                    },
                    {"type": "message_stop"},
                ]
            else:
                events = [
                    {
                        "id": "test",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "test-model",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "OK"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            for event in events:
                self.wfile.write(
                    (
                        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
                    ).encode()
                )
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", captured, response
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize(
    "provider,request_mode",
    [
        ("openai-compatible", "chat-completions"),
        ("openai-compatible", "responses"),
        ("anthropic", "messages"),
    ],
)
def test_probe_uses_selected_protocol_without_workspace_payload(
    endpoint, provider, request_mode, tmp_path
):
    url, captured, _ = endpoint
    layers = [
        (
            "workspace",
            {
                "app": {
                    "api_key": "test-private",
                    "model": "test-model",
                    "base_url": url,
                    "provider": provider,
                    "request_mode": request_mode,
                },
                "prompt": {"system_append": "PRIVATE WORKSPACE CONTEXT"},
            },
        )
    ]
    assert run_probe(layers, "startup")["status"] == "passed"
    assert captured == []
    result = run_probe(layers, "model")
    assert result == {"check": "model", "status": "passed", "code": "ok"}
    assert len(captured) == 1
    assert "PRIVATE WORKSPACE CONTEXT" not in json.dumps(captured)
    assert "test-private" not in json.dumps(captured)
    assert "tools" not in captured[0]
    assert not (tmp_path / ".rcoder").exists()


@pytest.mark.parametrize(
    "status,expected", [(401, "failed"), (404, "failed"), (429, "unknown")]
)
def test_failed_probe_never_leaks_provider_errors_or_retries(
    endpoint, status, expected, tmp_path
):
    url, captured, response = endpoint
    response["status"] = status
    result = run_probe(
        [("workspace", {"app": {"api_key": "test-private", "base_url": url}})], "model"
    )
    assert result["status"] == expected
    assert len(captured) == 1
    assert "secret-server-diagnostic" not in json.dumps(result)
    assert not (tmp_path / ".rcoder").exists()


def test_timeout_is_reported_as_unknown():
    result = run_probe(
        [("workspace", {"app": {"api_key": "test"}})], "startup", timeout=0.001
    )
    assert result == {"check": "startup", "status": "unknown", "code": "timeout"}
