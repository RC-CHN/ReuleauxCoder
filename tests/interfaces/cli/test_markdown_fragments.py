from reuleauxcoder.interfaces.cli.markdown_fragments import RetainedMarkdownRenderer


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
