import json
from types import SimpleNamespace

from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.llm.context_messages import synthetic_user_message
from reuleauxcoder.extensions.tools.builtin.context import ContextInspectTool


def test_context_inspect_reports_compacted_view_and_withheld_images() -> None:
    checkpoint = CompactionCheckpoint.create(
        trigger="automatic",
        strategy=["summarize"],
        source_history_version=4,
        replacement_history=[{"role": "user", "content": "summary"}],
        tokens_before=900,
        tokens_after=300,
        preserved_rounds=5,
        cache_epoch=2,
    )
    context = SimpleNamespace(
        checkpoints=(checkpoint,),
        history_version=5,
        cache_epoch=2,
        latest_usage=None,
        request_input_limit=1_000,
        get_context_tokens=lambda messages: 300,
    )
    messages = [
        synthetic_user_message("context_summary", "summary", source="test"),
        {
            "role": "user",
            "content": [{"type": "image", "variant_id": "v1", "attachment_id": "a1"}],
        },
    ]
    tool = ContextInspectTool()
    tool.bind_agent(
        SimpleNamespace(
            state=SimpleNamespace(messages=messages),
            context=context,
            llm=SimpleNamespace(model="text-only", support_modal=("text",)),
            current_session_id="session-1",
        )
    )

    outcome = tool.execute()
    payload = json.loads(outcome.content)

    assert outcome.success
    assert payload["current_view"]["synthetic_messages"] == {"context_summary": 1}
    assert payload["compaction"]["latest_checkpoint"]["preserved_rounds"] == 5
    assert "replacement_history" not in payload["compaction"]["latest_checkpoint"]
    assert payload["replay"] == {
        "history_version": 5,
        "cache_epoch": 2,
        "latest_actual_prompt_tokens": None,
    }
    assert payload["images"] == {
        "canonical_parts": 1,
        "supports_image_input": False,
        "withheld_from_requests": True,
        "reason": "current model does not support image input",
    }
