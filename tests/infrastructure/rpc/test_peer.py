import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from reuleauxcoder.infrastructure.rpc.peer import RpcError, RpcPeer
from reuleauxcoder.infrastructure.rpc.transport import MemoryTransport, StreamTransport


@pytest.fixture
def peers():
    left, right = MemoryTransport.pair()
    client, server = RpcPeer(left), RpcPeer(right)
    client.start()
    server.start()
    try:
        yield client, server
    finally:
        client.close()
        server.close()


def test_reverse_request_and_concurrent_response_correlation(peers):
    client, server = peers
    client.methods["confirm"] = lambda value: f"yes:{value}"
    server.methods["ask"] = lambda value: server.request(
        "confirm", {"value": value}, timeout=2
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(
            pool.map(
                lambda value: client.request("ask", {"value": value}, timeout=2),
                range(8),
            )
        )
    assert replies == [f"yes:{value}" for value in range(8)]


def test_method_params_and_unserializable_result_return_errors(peers, caplog):
    client, server = peers
    server.methods["bad_result"] = lambda: object()
    for method, params, code in (
        ("missing", {}, -32601),
        ("bad_result", {"extra": 1}, -32602),
        ("bad_result", {}, -32603),
    ):
        with pytest.raises(RpcError) as error:
            client.request(method, params, timeout=2)
        assert error.value.code == code
    assert "RPC handler failed: bad_result" in caplog.text


def test_disconnect_releases_pending_request(peers):
    client, server = peers
    entered, release = threading.Event(), threading.Event()
    server.methods["wait"] = lambda: (entered.set(), release.wait(2))[1]
    with ThreadPoolExecutor(max_workers=1) as pool:
        reply = pool.submit(client.request, "wait")
        assert entered.wait(2)
        try:
            server.close()
            with pytest.raises(ConnectionError):
                reply.result(timeout=2)
        finally:
            release.set()


def test_batch_notifications_have_no_reply_and_invalid_json_recovers():
    raw, transport = MemoryTransport.pair()
    calls = []
    peer = RpcPeer(
        transport, methods={"echo": lambda value: calls.append(value) or value}
    )
    peer.start()
    try:
        raw.send(
            json.dumps(
                [
                    {"jsonrpc": "2.0", "method": "echo", "params": ["notification"]},
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "echo",
                        "params": ["request"],
                    },
                ]
            )
        )
        assert json.loads(raw.receive()) == [
            {"jsonrpc": "2.0", "id": 7, "result": "request"}
        ]
        assert calls == ["notification", "request"]
        raw.send("{")
        assert json.loads(raw.receive())["error"]["code"] == -32700
        raw.send("[]")
        assert json.loads(raw.receive())["error"]["code"] == -32600
    finally:
        peer.close()


def test_stdio_framing_preserves_unicode_and_escaped_newlines():
    buffer = io.BytesIO()
    transport = StreamTransport(buffer, buffer)
    message = json.dumps({"text": "中文\nnext line"}, ensure_ascii=False)
    transport.send(message)
    buffer.seek(0)
    assert transport.receive() == message
    assert transport.receive() is None
    with pytest.raises(ValueError, match="Truncated"):
        StreamTransport(io.BytesIO(b'{"incomplete":'), io.BytesIO()).receive()


def test_close_completion_waits_for_inflight_write_and_transport_cleanup():
    writing, release_write = threading.Event(), threading.Event()
    closing, release_close = threading.Event(), threading.Event()

    class Transport:
        def send(self, message):
            writing.set()
            assert release_write.wait(2)

        def close(self):
            closing.set()
            assert release_close.wait(2)

    peer = RpcPeer(Transport())
    with ThreadPoolExecutor(max_workers=2) as pool:
        send = pool.submit(peer.notify, "update")
        assert writing.wait(2)
        close = pool.submit(peer.close)
        try:
            assert peer.closed.wait(2)
            assert not closing.is_set()
            release_write.set()
            send.result(timeout=2)
            assert closing.wait(2)
            # Another owner's close returns while the first closer is still
            # cleaning up; wait_closed must distinguish these two states.
            peer.close()
            with pytest.raises(TimeoutError):
                peer.wait_closed(timeout=0)
        finally:
            release_write.set()
            release_close.set()
        close.result(timeout=2)
    peer.wait_closed(timeout=2)
