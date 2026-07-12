import json

from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.context.summary import (
    build_summary_document,
    extract_key_info,
    flatten_messages,
    generate_summary,
    project_summary_input,
    validate_summary_document,
)


class DummyResponse:
    def __init__(self, content: str):
        self.content = content


class DummyLLM:
    def __init__(self, content):
        self.contents = list(content) if isinstance(content, list) else [content]
        self.calls = []
        self.call_kwargs = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        self.call_kwargs.append(kwargs)
        index = min(len(self.calls) - 1, len(self.contents) - 1)
        return DummyResponse(self.contents[index])


class FailingLLM:
    def chat(self, messages, **kwargs):
        raise RuntimeError("boom")


def test_flatten_messages_truncates_and_formats_roles() -> None:
    text = flatten_messages(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "x" * 10},
            {"role": "tool", "content": ""},
        ],
        truncate=5,
    )
    assert text == "[user] hello\n[assistant] xx\n…\nxx"


def test_extract_key_info_collects_files_errors_and_decisions() -> None:
    summary = extract_key_info(
        [
            {"content": "Edited src/main.py and docs/readme.md"},
            {"content": "Error: failed to parse config"},
            {"content": "Decision: use planner mode"},
        ]
    )
    assert "src/main.py" in summary
    assert "docs/readme.md" in summary
    assert "Error: failed to parse config" in summary
    assert "Decision: use planner mode" in summary


def test_extract_key_info_returns_fallback_when_nothing_found() -> None:
    assert extract_key_info([{"content": "hello world"}]) == "(no extractable context)"


def test_generate_summary_uses_llm_when_available() -> None:
    enrichment = build_summary_document([])
    enrichment["progress"]["completed"] = ["Validated the checkpoint schema"]
    llm = DummyLLM(json.dumps(enrichment))
    result = generate_summary([{"role": "user", "content": "hello"}], llm=llm)
    document = json.loads(result)
    assert document["user_intent"]["explicit_requests"] == [
        {"event_ref": "message:0", "text": "hello"}
    ]
    assert document["progress"]["completed"] == [
        "Validated the checkpoint schema"
    ]
    assert len(llm.calls) == 1
    assert llm.call_kwargs[0]["max_output_tokens"] == 4096


def test_generate_summary_repairs_invalid_model_output_once() -> None:
    repaired = build_summary_document([])
    repaired["progress"]["completed"] = ["repaired valid JSON"]
    llm = DummyLLM(["not json", json.dumps(repaired)])

    document = json.loads(generate_summary([], llm=llm))

    assert document["progress"]["completed"] == ["repaired valid JSON"]
    assert len(llm.calls) == 2
    assert llm.call_kwargs[1]["max_output_tokens"] == 2048


def test_generate_summary_falls_back_when_llm_fails() -> None:
    result = generate_summary(
        [{"content": "Decision: continue with tests in app.py"}],
        llm=FailingLLM(),
    )
    assert "app.py" in result
    assert "Decision" in result


def test_summary_projection_never_splits_a_tool_batch() -> None:
    messages = []
    for index in range(3):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": f"call-{index}", "function": {"name": "read_file"}}
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call-{index}",
                    "content": "result",
                },
            ]
        )

    projected = project_summary_input(messages, max_rounds=2)

    assert "1 older complete API rounds" in projected[0]["content"]
    assert [item.get("tool_call_id") for item in projected] == [
        None,
        None,
        "call-1",
        None,
        "call-2",
    ]


def test_summary_validation_rejects_wrong_nested_types() -> None:
    document = build_summary_document([])
    document["scope"]["summarized_rounds"] = "3"
    assert validate_summary_document(document) is False


def test_summary_projection_compacts_one_oversized_round_without_splitting_it() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "image_url", "id": "diagram"},
                {"type": "text", "text": "inspect image"},
            ],
            "tool_calls": [
                {
                    "id": "call",
                    "function": {
                        "name": "shell",
                        "arguments": "x" * 10_000,
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "y" * 10_000},
    ]

    projected = project_summary_input(messages, max_chars=1_500)

    assert len(projected) == 2
    assert projected[0]["tool_calls"][0]["id"] == "call"
    assert projected[1]["tool_call_id"] == "call"
    assert "image_url source=diagram" in projected[0]["content"]
    assert "payload omitted" in projected[1]["content"]


def test_deterministic_summary_uses_ledger_provenance_and_control_facts() -> None:
    ledger = HistoryLedger(session_id="session", agent_id="root")
    user_event = ledger.append_message(
        {"role": "user", "content": "必须保留真实历史"}, source="user_input"
    )
    ledger.append(
        "approval_requested",
        {"request_id": "approval-1", "tool_name": "edit_file"},
    )
    ledger.append(
        "subagent_job_changed",
        {
            "job_id": "sj_active",
            "status": "running",
            "task": "verify history",
            "worktree_path": "/tmp/worktree",
        },
    )
    ledger.append("context_checkpoint", {"checkpoint_id": "cp_previous"})

    document = json.loads(
        generate_summary(
            [{"role": "user", "content": "必须保留真实历史"}],
            history_events=ledger.events,
        )
    )

    assert document["user_intent"]["explicit_requests"][0]["event_ref"] == user_event.event_id
    assert "approval-1: edit_file" in document["agent_state"]["pending_approvals"]
    assert any("sj_active: running" in item for item in document["agent_state"]["active_subagents"])
    assert document["code_state"]["worktrees_and_commits"] == [
        "sj_active: /tmp/worktree"
    ]
    assert document["provenance"]["source_checkpoint_ids"] == ["cp_previous"]
