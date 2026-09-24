import threading

import pytest

from reuleauxcoder.app.interaction_contracts import ReviewDocument, ReviewRequest, ReviewResponse
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.infrastructure.rpc.peer import RpcError


def test_review_documents_are_paged_and_expire_with_the_interaction(runtime):
    entered, release = threading.Event(), threading.Event()
    wire = []
    request = ReviewRequest(
        "Edit", "Review this proposal",
        documents=(ReviewDocument("0", "/workspace/a.py", True, "base-sha", 100001, 4, "中" * 100001, "next"),),
    )

    def interact(**params):
        wire.append(params)
        entered.set()
        assert release.wait(3)
        return encode(ReviewResponse(True))

    runtime.client.peer.methods["interaction.request"] = interact
    adapter = runtime.server.interactions.adapter
    worker = threading.Thread(target=lambda: adapter.review(request))
    worker.start()
    try:
        assert entered.wait(1)
        metadata = decode(wire[0]["request"]).documents[0]
        assert metadata.before is metadata.after is None
        assert metadata.before_length == 100001
        first = adapter.document(request.request_id, "0", "before")
        assert first == {"text": "中" * 65536, "next_offset": 65536, "complete": False}
        tail = adapter.document(request.request_id, "0", "before", offset=first["next_offset"])
        assert tail["complete"] and len(tail["text"]) == 100001 - 65536
        for invalid in ({"offset": -1}, {"limit": 65537}, {"offset": True}, {"limit": 0}):
            with pytest.raises(RpcError):
                adapter.document(request.request_id, "0", "before", **invalid)
        with pytest.raises(RpcError):
            adapter.document(request.request_id, "../../secret", "before")
        adapter.cancel(request.request_id)
        with pytest.raises(RpcError, match="no longer active"):
            adapter.document(request.request_id, "0", "after")
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    with pytest.raises(RpcError, match="no longer active"):
        adapter.document(request.request_id, "0", "after")
