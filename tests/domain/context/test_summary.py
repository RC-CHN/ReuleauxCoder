from reuleauxcoder.domain.context.summary import extract_key_info, flatten_messages, generate_summary


class DummyResponse:
    def __init__(self, content: str):
        self.content = content


class DummyLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return DummyResponse(self.content)


class FailingLLM:
    def chat(self, messages):
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
    assert text == "[user] hello\n[assistant] xxxxx"


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
    llm = DummyLLM("compressed summary")
    result = generate_summary([{"role": "user", "content": "hello"}], llm=llm)
    assert result == "compressed summary"
    assert len(llm.calls) == 1


def test_generate_summary_falls_back_when_llm_fails() -> None:
    result = generate_summary(
        [{"content": "Decision: continue with tests in app.py"}],
        llm=FailingLLM(),
    )
    assert "app.py" in result
    assert "Decision" in result
