from io import StringIO

import pytest

from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort
from reuleauxcoder.infrastructure.workspace.text import read_text_page


@pytest.mark.parametrize(
    "separator",
    [
        "\n",
        "\r\n",
        "\r",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    ],
)
@pytest.mark.parametrize("trailing", [True, False])
def test_page_matches_splitlines(separator, trailing):
    text = separator.join(["alpha", "", "中😀", "omega"]) + (
        separator if trailing else ""
    )
    for offset in range(1, 7):
        for limit in (1, 2, 10):
            result = read_text_page(
                StringIO(text), offset=offset, limit=limit, max_chars=1000
            )
            assert result.lines == tuple(
                text.splitlines()[offset - 1 : offset - 1 + limit]
            )
            assert result.has_more == (len(text.splitlines()) > offset - 1 + limit)
            assert result.total_lines == (
                None if result.has_more else len(text.splitlines())
            )


def test_page_stops_reading_after_requested_lines():
    class CountingStream(StringIO):
        read_chars = 0

        def read(self, size=-1):
            assert size > 0
            text = super().read(size)
            self.read_chars += len(text)
            return text

    stream = CountingStream("alpha\n" * 100_000)
    result = read_text_page(stream, offset=1, limit=20, max_chars=1000)
    assert result.lines == ("alpha",) * 20
    assert result.has_more
    assert stream.read_chars == 64 * 1024


def test_page_handles_crlf_at_chunk_boundary_and_skips_long_lines():
    text = "x" * (64 * 1024 - 1) + "\r\nsecond\r\nthird"
    result = read_text_page(StringIO(text), offset=2, limit=2, max_chars=100)
    assert result.lines == ("second", "third")
    assert result.total_lines == 3
    assert not result.has_more


@pytest.mark.parametrize(
    "text",
    ["x" * 200_000, "中😀" * 100_000, "\n" * 200_000],
    ids=["long-ascii-line", "long-unicode-line", "empty-lines"],
)
def test_page_character_budget_bounds_long_and_empty_lines(text):
    result = read_text_page(StringIO(text), offset=1, limit=200_000, max_chars=20)
    assert len("\n".join(result.lines)) <= 20
    assert result.truncated and result.has_more
    assert result.total_lines is None


def test_local_page_preserves_confinement_and_errors(tmp_path):
    workspace = LocalWorkspacePort(tmp_path)
    for path, code in [
        ("missing", "not_found"),
        (".", "not_a_file"),
        ("../outside", "path_outside_workspace"),
    ]:
        with pytest.raises(WorkspaceError) as error:
            workspace.read_text_page(path)
        assert error.value.code.value == code
    with pytest.raises(WorkspaceError):
        workspace.read_text_page("missing", offset=0)
