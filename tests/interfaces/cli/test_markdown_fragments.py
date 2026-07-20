import pytest

from reuleauxcoder.interfaces.cli.markdown_fragments import (
    RetainedMarkdownRenderer,
    _plain_markdown_fragments,
    _rich_markdown_fragments,
)


def _text(fragments) -> str:
    return "".join(text for _style, text in fragments)


def test_completed_markdown_renders_semantics_without_source_delimiters() -> None:
    renderer = RetainedMarkdownRenderer()
    fragments = renderer.render(
        cell_id="assistant:1",
        revision=1,
        text=(
            "**bold** and `code`\n\n"
            "- one\n- two\n\n"
            "| Name | Value |\n| --- | --- |\n| alpha | 1 |"
        ),
        complete=True,
        width=72,
    )
    rendered = _text(fragments)

    assert "**" not in rendered
    assert "`" not in rendered
    assert "| Name |" not in rendered
    assert "bold" in rendered and "code" in rendered
    assert "• one" in rendered and "alpha" in rendered
    assert any("bold" in style for style, text in fragments if "bold" in text)


def test_active_markdown_only_formats_committed_prefix() -> None:
    renderer = RetainedMarkdownRenderer()
    fragments = renderer.render(
        cell_id="assistant:stream",
        revision=3,
        text="**done**\n\n- stable\n\n**pending",
        complete=False,
        width=60,
    )
    rendered = _text(fragments)

    assert "**done**" not in rendered
    assert "• stable" in rendered
    assert "**pending" in rendered


def test_markdown_cache_is_keyed_by_revision_width_and_theme() -> None:
    renderer = RetainedMarkdownRenderer()
    narrow = renderer.render(
        cell_id="assistant:cache",
        revision=1,
        text="A paragraph that wraps at narrow widths.",
        complete=True,
        width=20,
        theme_revision=0,
    )
    wide = renderer.render(
        cell_id="assistant:cache",
        revision=1,
        text="A paragraph that wraps at narrow widths.",
        complete=True,
        width=80,
        theme_revision=1,
    )

    assert narrow and wide
    assert len(renderer._cache) == 2


def test_markdown_cache_evicts_the_least_recently_used_entry() -> None:
    renderer = RetainedMarkdownRenderer(max_entries=20)
    for index in range(20):
        renderer.render(
            cell_id=f"assistant:{index}",
            revision=1,
            text=f"message {index}",
            complete=True,
            width=80,
        )

    renderer.render(
        cell_id="assistant:0",
        revision=1,
        text="message 0",
        complete=True,
        width=80,
    )
    renderer.render(
        cell_id="assistant:20",
        revision=1,
        text="message 20",
        complete=True,
        width=80,
    )

    cached_cell_ids = {key[0] for key in renderer._cache}
    assert len(renderer._cache) == 20
    assert "assistant:0" in cached_cell_ids
    assert "assistant:20" in cached_cell_ids
    assert "assistant:1" not in cached_cell_ids


@pytest.mark.parametrize(
    "text",
    [
        "A normal answer without markdown.",
        "hello\nworld",
        "first paragraph\n\nsecond paragraph",
        "two  spaces",
        "literal * star",
        "escaped \\* star and &amp; entity",
        "中文与 emoji 🚀",
    ],
)
def test_plain_markdown_fast_path_matches_rich_fragments(text: str) -> None:
    assert _plain_markdown_fragments(text) == _rich_markdown_fragments(text, width=72)


@pytest.mark.parametrize(
    "text",
    [
        "**bold**",
        "`code`",
        "# heading",
        "- list item",
        "[link](https://example.com)",
        "hard break  \nnext",
        "| A | B |\n|---|---|\n| one | two |",
    ],
)
def test_plain_markdown_fast_path_rejects_styled_syntax(text: str) -> None:
    assert _plain_markdown_fragments(text) is None


def test_rich_markdown_wraps_long_cjk_bullets_without_losing_text() -> None:
    source = (
        "- 单 system 提示词协议：所有运行时上下文都是带 provenance 标签的合成"
        " user 消息，供应商边界 fail-close，这条很长很长用来测试换行行为\n"
        "- 第二条也凑得比较长一点，看看窄宽度下能不能正常折行显示完整内容\n"
    )
    fragments = _rich_markdown_fragments(source, width=40)

    rendered = _text(fragments)
    expected = source.replace("- ", "").replace("\n", "")
    kept = "".join(character for character in expected if character in rendered)
    assert kept == expected
    lines = rendered.splitlines()
    assert len(lines) > 2
    assert all(len(line) <= 40 for line in lines)
