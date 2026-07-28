from reuleauxcoder.infrastructure.process.buffer import BoundedTextBuffer


def test_buffer_retains_bounded_utf8_tail_with_monotonic_offsets() -> None:
    buffer = BoundedTextBuffer(6)
    buffer.append("ab")
    buffer.append("你")
    buffer.append("cd")

    retained = buffer.retained()

    assert retained.text == "b你cd"
    assert retained.next_offset == 5
    assert retained.truncated is True
    assert buffer.total_bytes == 7


def test_buffer_poll_cap_advances_without_losing_remaining_text() -> None:
    buffer = BoundedTextBuffer(64)
    buffer.append("abcdef")

    first = buffer.read_after(0, max_bytes=3)
    second = buffer.read_after(first.next_offset, max_bytes=3)

    assert first.text == "abc"
    assert first.next_offset == 3
    assert first.truncated is True
    assert second.text == "def"
    assert second.next_offset == 6
    assert second.truncated is False


def test_buffer_reports_when_requested_cursor_was_overwritten() -> None:
    buffer = BoundedTextBuffer(4)
    buffer.append("old")
    buffer.append("new")

    result = buffer.read_after(0, max_bytes=64)

    assert result.text == "dnew"
    assert result.next_offset == 6
    assert result.truncated is True
