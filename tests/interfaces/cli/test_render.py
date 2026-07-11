from __future__ import annotations

import pytest

from reuleauxcoder.interfaces.cli.render import CLIRenderer, _find_committed_boundary


@pytest.fixture
def renderer() -> CLIRenderer:
    return CLIRenderer()


# ── _find_committed_boundary — markdown-it block commitment ───────────


def test_boundary_no_blocks_returns_none() -> None:
    """Single line with no markdown blocks → nothing to commit."""
    assert _find_committed_boundary("hello world") is None


def test_boundary_one_block_returns_none() -> None:
    """Only one complete paragraph → nothing to commit (keep last block)."""
    assert _find_committed_boundary("hello world\n\n") is None


def test_boundary_two_paragraphs_commits_first() -> None:
    """Two paragraphs → first paragraph committed, second kept pending."""
    text = "first paragraph.\n\nsecond paragraph."
    b = _find_committed_boundary(text)
    assert b is not None
    assert text[:b].strip() == "first paragraph."


def test_boundary_incomplete_code_fence_commits_only_prior_blocks() -> None:
    """Opening code fence without closing — prior blocks are safe to commit.

    markdown-it treats an unclosed fence as a single block
    extending to EOF.  The heading *before* the fence is the
    second-to-last block and can be safely flushed; the fence
    itself (the last block) stays pending until its closing ```.
    This prevents the <black_tile> artifact — the incomplete
    fence is never rendered alone.
    """
    text = """### header

```
code line 1

code line 2
"""
    b = _find_committed_boundary(text)
    # heading is committed, incomplete fence stays pending
    assert b is not None
    flushed = text[:b]
    assert "### header" in flushed
    assert "```" not in flushed
    assert "code line 1" not in flushed


def test_boundary_complete_code_block_commits_header() -> None:
    """Header → complete code block → header should be committed."""
    text = """### header

```
code
```

more text"""
    b = _find_committed_boundary(text)
    assert b is not None
    flushed = text[:b]
    assert "### header" in flushed
    assert "code" in flushed
    assert "more text" not in flushed


def test_boundary_code_block_with_internal_double_newline() -> None:
    """Code block with \\n\\n inside → treated as one atomic block.

    The double-newline inside a fenced code block must NOT split the
    block — it would create an orphaned opening fence followed by a
    stray closing fence.
    """
    text = """### commit msg

```
refactor(x): summary

- bullet one
- bullet two
```

after"""
    b = _find_committed_boundary(text)
    assert b is not None
    flushed = text[:b]
    # The entire commit section (header + complete code block) is committed
    assert "refactor(x): summary" in flushed
    assert "- bullet one" in flushed
    assert "- bullet two" in flushed
    assert "after" not in flushed


def test_boundary_multiple_code_blocks() -> None:
    """Multiple complete code blocks → all but last block committed."""
    text = """preamble

```
block1
```

middle

```
block2
```
"""
    b = _find_committed_boundary(text)
    assert b is not None
    flushed = text[:b]
    assert "preamble" in flushed
    assert "block1" in flushed
    assert "middle" in flushed
    # block2 is the last block — kept pending
    assert "block2" not in flushed


# ── Streaming integration — _flush_completed_paragraphs ────────────────


def test_flush_does_not_split_code_fence(renderer: CLIRenderer) -> None:
    """A code block with internal \\n\\n stays intact across stream flushes.

    When streamed token-by-token, the opening ``` + content + internal
    \\n\\n should not trigger a premature flush that orphans the fence.
    Only after the closing ``` arrives should the block be rendered.
    """
    tokens = [
        "### ",
        "commit\n",
        "\n",
        "```\n",
        "refactor(x): summary\n",
        "\n",
        "- bullet\n",
        "```\n",
        "\n",
        "done",
    ]

    blocks = []
    original = renderer.render_content_markdown

    def capture(text: str) -> None:
        blocks.append(text)

    renderer.render_content_markdown = capture  # type: ignore[method-assign]
    try:
        for t in tokens:
            renderer._render_token(t)
        renderer._flush_remaining_content()

        # Every flushed block must have balanced ``` fences.
        for blk in blocks:
            assert blk.count("```") % 2 == 0, (
                f"Unbalanced code fence in flushed block:\n{blk!r}"
            )

        full = "".join(blocks)
        assert "refactor(x): summary" in full
        assert "- bullet" in full
        assert "done" in full
    finally:
        renderer.render_content_markdown = original  # type: ignore[method-assign]
