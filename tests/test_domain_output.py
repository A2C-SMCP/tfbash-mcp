from __future__ import annotations

import pytest

from tfbash_mcp.domain import InvalidCursor, InvalidTransition, Utf8OutputBuffer


def test_incremental_utf8_decode_preserves_split_code_points_and_ansi() -> None:
    buffer = Utf8OutputBuffer(64)

    assert buffer.append(b"\xe7\x95") == 0
    assert buffer.write_cursor == 0
    assert buffer.append(b"\x8c\x1b[31mred\x1b[0m") == 15

    window = buffer.read(0, 64)
    assert window.output == "界\x1b[31mred\x1b[0m"
    assert window.next_cursor == 15
    assert window.at_end is True


def test_invalid_and_incomplete_utf8_is_stably_replaced() -> None:
    buffer = Utf8OutputBuffer(64)

    assert buffer.append(b"A\xffB\xe2") == 5
    assert buffer.seal() == 3
    assert buffer.seal() == 0
    assert buffer.read(0, 64).output == "A\ufffdB\ufffd"

    with pytest.raises(InvalidTransition):
        buffer.append(b"late")


def test_ring_eviction_and_reads_keep_code_point_boundaries() -> None:
    buffer = Utf8OutputBuffer(4)
    assert buffer.append("a界b".encode()) == 5

    assert buffer.buffer_start_cursor == 1
    truncated = buffer.read(0, 4)
    assert truncated.output == "界b"
    assert truncated.buffer_start_cursor == 1
    assert truncated.next_cursor == 5
    assert truncated.truncated_before_cursor is True

    with pytest.raises(InvalidCursor, match="code point"):
        buffer.read(2, 4)
    with pytest.raises(InvalidCursor, match="exceeds"):
        buffer.read(6, 4)


def test_read_limit_ends_before_partial_code_point() -> None:
    buffer = Utf8OutputBuffer(64)
    buffer.append("a界b".encode())

    first = buffer.read(0, 2)
    assert first.output == "a"
    assert first.next_cursor == 1
    assert first.at_end is False

    second = buffer.read(first.next_cursor, 4)
    assert second.output == "界b"
    assert second.next_cursor == 5
    assert second.at_end is True


def test_character_tail_is_unicode_safe_and_reports_omitted_output() -> None:
    buffer = Utf8OutputBuffer(64)
    buffer.append("prefix-a界🙂z".encode())

    tail = buffer.tail(4)

    assert tail.output == "a界🙂z"[-4:]
    assert tail.truncated is True


def test_character_tail_reports_complete_short_output() -> None:
    buffer = Utf8OutputBuffer(64)
    buffer.append("你好".encode())

    assert buffer.tail(500).output == "你好"
    assert buffer.tail(500).truncated is False

    with pytest.raises(ValueError, match="positive"):
        buffer.tail(0)


@pytest.mark.parametrize("capacity", [-1, 0, 3])
def test_buffer_capacity_must_hold_one_utf8_code_point(capacity: int) -> None:
    with pytest.raises(ValueError):
        Utf8OutputBuffer(capacity)
