"""Bounded normalized UTF-8 output storage."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from threading import RLock

from tfbash_mcp.domain.errors import InvalidCursor, InvalidTransition


@dataclass(frozen=True, slots=True)
class OutputSlice:
    """A cursor-addressed window over normalized execution output."""

    output: str
    buffer_start_cursor: int
    next_cursor: int
    truncated_before_cursor: bool
    at_end: bool


@dataclass(frozen=True, slots=True)
class OutputTail:
    """The most recent Unicode characters from normalized output."""

    output: str
    truncated: bool


class Utf8OutputBuffer:
    """A byte-bounded UTF-8 buffer that never cuts a code point."""

    def __init__(self, capacity_bytes: int) -> None:
        if capacity_bytes < 4:
            raise ValueError("capacity_bytes must be at least 4")
        self._capacity_bytes = capacity_bytes
        self._data = bytearray()
        self._write_cursor = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._sealed = False
        self._lock = RLock()

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    @property
    def write_cursor(self) -> int:
        with self._lock:
            return self._write_cursor

    @property
    def buffer_start_cursor(self) -> int:
        with self._lock:
            return self._write_cursor - len(self._data)

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def append(self, raw: bytes) -> int:
        """Incrementally decode raw PTY bytes and return normalized bytes added."""

        with self._lock:
            if self._sealed:
                raise InvalidTransition("cannot append output after the buffer is sealed")
            return self._append_text(self._decoder.decode(raw, final=False))

    def seal(self) -> int:
        """Flush an incomplete UTF-8 sequence as U+FFFD and seal idempotently."""

        with self._lock:
            if self._sealed:
                return 0
            added = self._append_text(self._decoder.decode(b"", final=True))
            self._sealed = True
            return added

    def read(self, cursor: int, max_bytes: int) -> OutputSlice:
        if cursor < 0:
            raise InvalidCursor("cursor must be non-negative")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        with self._lock:
            if cursor > self._write_cursor:
                raise InvalidCursor("cursor exceeds the current write cursor")
            start_cursor = self._write_cursor - len(self._data)
            truncated = cursor < start_cursor
            effective_cursor = start_cursor if truncated else cursor
            offset = effective_cursor - start_cursor
            if offset < len(self._data) and _is_continuation_byte(self._data[offset]):
                raise InvalidCursor("cursor splits a UTF-8 code point")

            end = min(len(self._data), offset + max_bytes)
            while end > offset and end < len(self._data) and _is_continuation_byte(self._data[end]):
                end -= 1
            chunk = bytes(self._data[offset:end])
            next_cursor = effective_cursor + len(chunk)
            return OutputSlice(
                output=chunk.decode("utf-8"),
                buffer_start_cursor=start_cursor,
                next_cursor=next_cursor,
                truncated_before_cursor=truncated,
                at_end=next_cursor == self._write_cursor,
            )

    def tail(self, max_characters: int) -> OutputTail:
        """Return at most the final ``max_characters`` Unicode code points."""

        if max_characters < 1:
            raise ValueError("max_characters must be positive")
        with self._lock:
            # A UTF-8 code point occupies at most four bytes. Decoding this bounded
            # suffix recovers the requested character tail without materializing a
            # potentially multi-megabyte retained buffer.
            start = max(0, len(self._data) - max_characters * 4)
            while start < len(self._data) and _is_continuation_byte(self._data[start]):
                start += 1
            candidate = bytes(self._data[start:]).decode("utf-8")
            output = candidate[-max_characters:]
            return OutputTail(
                output=output,
                truncated=self._write_cursor > len(output.encode("utf-8")),
            )

    def _append_text(self, text: str) -> int:
        encoded = text.encode("utf-8")
        if not encoded:
            return 0
        self._data.extend(encoded)
        self._write_cursor += len(encoded)
        overflow = len(self._data) - self._capacity_bytes
        if overflow > 0:
            trim_at = overflow
            while trim_at < len(self._data) and _is_continuation_byte(self._data[trim_at]):
                trim_at += 1
            del self._data[:trim_at]
        return len(encoded)


def _is_continuation_byte(value: int) -> bool:
    return value & 0b1100_0000 == 0b1000_0000
