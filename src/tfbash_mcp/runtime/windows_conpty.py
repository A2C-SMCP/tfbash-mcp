"""Event-driven Windows ConPTY transport backed by pywinpty 3.0.5.

The low-level pywinpty calls can block while releasing the GIL.  They are
therefore confined to exactly one reader thread and one writer thread per
session.  The ShellWorker only exchanges bytes through bounded in-memory
buffers and condition-based readiness notifications.
"""

from __future__ import annotations

import codecs
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module, metadata
from threading import Condition, Event, Lock, Thread
from typing import Protocol, runtime_checkable

from tfbash_mcp.runtime.contracts import (
    ProcessOwnership,
    ReadStatus,
    RuntimeName,
    RuntimeSession,
    SpawnRequest,
    TransportRead,
    TransportWrite,
    WaitInterest,
)
from tfbash_mcp.runtime.errors import TransportClosed, TransportError

_PYWINPTY_VERSION = "3.0.5"
_TERMINAL_STATUS_QUERY = "\x1b[5n"
_CURSOR_POSITION_QUERY = "\x1b[6n"
_TERMINAL_STATUS_RESPONSE = "\x1b[0n"
_CURSOR_POSITION_RESPONSE = "\x1b[1;1R"
_NATIVE_WRITE_CHUNK_BYTES = 4096


@runtime_checkable
class WindowsSpawnOwnership(ProcessOwnership, Protocol):
    """Windows-only hooks implemented by the process supervisor in #15."""

    def reserve(self) -> None:
        """Atomically consume this ownership before process creation."""
        ...

    def attach(self, process_id: int) -> None:
        """Record the process before any later transport operation.

        Before returning or raising, this method must either make the process
        reachable by supervisor cleanup or terminate and reap it itself.
        """
        ...


class ConPtyLike(Protocol):
    """The pywinpty surface used by the production adapter."""

    pid: int | None

    def spawn(
        self,
        appname: str,
        *,
        cmdline: str | None = None,
        cwd: str | None = None,
        env: str | None = None,
    ) -> bool: ...

    def read(self, *, blocking: bool = False) -> str: ...

    def write(self, value: str) -> int: ...

    def set_size(self, columns: int, rows: int) -> None: ...

    def cancel_io(self) -> None: ...

    def iseof(self) -> bool: ...

    def isalive(self) -> bool: ...


ConPtyFactory = Callable[[int, int], ConPtyLike]


@dataclass(frozen=True, slots=True)
class _WriteItem:
    text: str
    accepted_bytes: int


@dataclass(slots=True)
class _ResizeItem:
    columns: int
    rows: int
    completed: Event
    error: Exception | None = None
    started: bool = False


class ConPtySession(RuntimeSession):
    """Opaque ConPTY session with bounded buffers and private I/O threads."""

    def __init__(
        self,
        *,
        session_id: str,
        pty: ConPtyLike,
        transport_token: object,
        max_read_buffer_bytes: int,
        max_write_buffer_bytes: int,
    ) -> None:
        self._session_id = session_id
        self._pty: ConPtyLike | None = pty
        self._transport_token = transport_token
        self._max_read_buffer_bytes = max_read_buffer_bytes
        self._max_write_buffer_bytes = max_write_buffer_bytes
        self._condition = Condition()
        self._close_lock = Lock()
        self._read_guard = Lock()
        self._read_buffer = bytearray()
        self._write_queue: deque[_WriteItem] = deque()
        self._priority_writes: deque[_WriteItem] = deque()
        self._resize_queue: deque[_ResizeItem] = deque()
        self._write_buffered_bytes = 0
        self._priority_write_buffered_bytes = 0
        self._input_decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._terminal_query_tail = ""
        self._reader_error: Exception | None = None
        self._writer_error: Exception | None = None
        self._reader_done = False
        self._writer_done = False
        self._native_write_active = False
        self._closing = False
        self._closed = False
        self._reader: Thread | None = None
        self._writer: Thread | None = None

    @property
    def session_id(self) -> str:
        return self._session_id


class ConPtyTransport:
    """Nonblocking byte transport for Windows 11 native ConPTY sessions."""

    runtime_name = RuntimeName.WINDOWS_PWSH

    def __init__(
        self,
        *,
        columns: int = 120,
        rows: int = 40,
        max_read_buffer_bytes: int = 4 * 1024 * 1024,
        max_write_buffer_bytes: int = 256 * 1024,
        close_timeout_ms: int = 3000,
        pty_factory: ConPtyFactory | None = None,
    ) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("ConPTY dimensions must be positive")
        if max_read_buffer_bytes <= 0:
            raise ValueError("ConPTY read buffer must be positive")
        if max_write_buffer_bytes < 4:
            raise ValueError("ConPTY write buffer must hold one UTF-8 scalar")
        if close_timeout_ms <= 0:
            raise ValueError("ConPTY close timeout must be positive")
        self._columns = columns
        self._rows = rows
        self._max_read_buffer_bytes = max_read_buffer_bytes
        self._max_write_buffer_bytes = max_write_buffer_bytes
        self._close_timeout_ms = close_timeout_ms
        self._pty_factory = pty_factory or _create_pywinpty_conpty
        self._transport_token = object()

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int | None = None,
    ) -> RuntimeSession:
        if not isinstance(ownership, WindowsSpawnOwnership):
            raise TransportError("ConPTY transport requires prepared Windows ownership")
        _validate_spawn_request(request)
        if deadline_ms is not None and deadline_ms <= 0:
            raise TransportError("ConPTY spawn deadline expired")
        deadline = None if deadline_ms is None else time.monotonic() + deadline_ms / 1000
        pty: ConPtyLike | None = None
        try:
            ownership.reserve()
            pty = self._pty_factory(self._columns, self._rows)
            command_line = subprocess.list2cmdline(request.arguments) if request.arguments else None
            application_name = subprocess.list2cmdline((request.executable,))
            try:
                spawned = pty.spawn(
                    application_name,
                    cmdline=command_line,
                    cwd=request.cwd,
                    env=_windows_environment_block(request.environment),
                )
            except Exception as spawn_error:
                process_id = _valid_process_id(pty.pid)
                if process_id is not None:
                    ownership.attach(process_id)
                raise TransportError("pywinpty raised while spawning the process") from spawn_error
            process_id = _valid_process_id(pty.pid)
            if process_id is not None:
                ownership.attach(process_id)
            if not spawned:
                raise TransportError("pywinpty did not spawn the requested process")
            if process_id is None:
                raise TransportError("ConPTY did not expose a valid process id")
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportError("ConPTY spawn deadline expired")
            session = ConPtySession(
                session_id=f"windows_{ownership.ownership_id}",
                pty=pty,
                transport_token=self._transport_token,
                max_read_buffer_bytes=self._max_read_buffer_bytes,
                max_write_buffer_bytes=self._max_write_buffer_bytes,
            )
            self._start_threads(session)
            return session
        except Exception as error:
            if pty is not None:
                try:
                    pty.cancel_io()
                except Exception as cleanup_error:
                    raise TransportError(
                        "failed to spawn ConPTY and cancel its native I/O"
                    ) from cleanup_error
            if isinstance(error, TransportError):
                raise
            raise TransportError("failed to spawn ConPTY") from error

    def read(self, session: RuntimeSession, max_bytes: int) -> TransportRead:
        concrete = self._session(session)
        if max_bytes <= 0:
            raise TransportError("max_bytes must be positive")
        if not concrete._read_guard.acquire(blocking=False):
            raise TransportError("concurrent ConPTY readers violate the single-owner contract")
        try:
            with concrete._condition:
                self._require_open_locked(concrete)
                if concrete._read_buffer:
                    count = min(max_bytes, len(concrete._read_buffer))
                    data = bytes(concrete._read_buffer[:count])
                    del concrete._read_buffer[:count]
                    concrete._condition.notify_all()
                    return TransportRead(ReadStatus.DATA, data)
                self._raise_io_failure_locked(concrete)
                if concrete._reader_done:
                    return TransportRead(ReadStatus.EOF)
                return TransportRead(ReadStatus.WOULD_BLOCK)
        finally:
            concrete._read_guard.release()

    def write(self, session: RuntimeSession, data: memoryview) -> TransportWrite:
        concrete = self._session(session)
        with concrete._condition:
            self._require_open_locked(concrete)
            self._raise_io_failure_locked(concrete)
            if not data:
                return TransportWrite(bytes_written=0, would_block=False)

            # Three bytes are reserved for a UTF-8 scalar split across calls.  This
            # prevents a partial scalar at the nominal buffer edge from deadlocking
            # the caller that must supply its remaining byte(s).
            hard_limit = concrete._max_write_buffer_bytes + 3
            available = hard_limit - concrete._write_buffered_bytes
            if available <= 0:
                return TransportWrite(bytes_written=0, would_block=True)
            accepted = min(len(data), available)
            payload = bytes(data[:accepted])
            before_pending = len(concrete._input_decoder.getstate()[0])
            try:
                text = concrete._input_decoder.decode(payload, final=False)
            except UnicodeDecodeError as error:
                concrete._writer_error = TransportError(
                    "ConPTY input is not a valid incremental UTF-8 stream"
                )
                concrete._condition.notify_all()
                raise concrete._writer_error from error
            after_pending = len(concrete._input_decoder.getstate()[0])
            emitted_bytes = before_pending + accepted - after_pending
            concrete._write_buffered_bytes += accepted
            if text:
                chunks = _split_utf8_chunks(text)
                if sum(chunk_bytes for _, chunk_bytes in chunks) != emitted_bytes:
                    raise TransportError("ConPTY UTF-8 write accounting diverged")
                concrete._write_queue.extend(
                    _WriteItem(chunk, chunk_bytes) for chunk, chunk_bytes in chunks
                )
            concrete._condition.notify_all()
            return TransportWrite(
                bytes_written=accepted,
                would_block=accepted < len(data),
            )

    def wait(
        self,
        session: RuntimeSession,
        interests: frozenset[WaitInterest],
        timeout_ms: int,
    ) -> frozenset[WaitInterest]:
        concrete = self._session(session)
        if timeout_ms < 0:
            raise TransportError("timeout_ms cannot be negative")
        if not interests:
            return frozenset()
        if not interests.issubset(frozenset(WaitInterest)):
            raise TransportError("unknown ConPTY wait interest")
        deadline = time.monotonic() + timeout_ms / 1000
        with concrete._condition:
            while True:
                self._require_open_locked(concrete)
                ready = self._ready_locked(concrete, interests)
                # Accepted output has precedence over a later background error.
                # ShellWorker waits before every read, so raising here would make
                # the already-buffered quick-exit tail unreachable.
                if WaitInterest.READABLE in ready and concrete._read_buffer:
                    return ready
                self._raise_io_failure_locked(concrete)
                if ready:
                    return ready
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return frozenset()
                concrete._condition.wait(remaining)

    def resize(self, session: RuntimeSession, *, columns: int, rows: int) -> None:
        """Serialize an adapter-specific resize through the native writer thread."""

        if columns <= 0 or rows <= 0:
            raise TransportError("ConPTY dimensions must be positive")
        concrete = self._session(session)
        item = _ResizeItem(columns, rows, Event())
        with concrete._condition:
            self._require_open_locked(concrete)
            self._raise_io_failure_locked(concrete)
            concrete._resize_queue.append(item)
            concrete._condition.notify_all()
        if not item.completed.wait(self._close_timeout_ms / 1000):
            with concrete._condition:
                if not item.completed.is_set() and not item.started:
                    concrete._resize_queue.remove(item)
                    raise TransportError("ConPTY resize did not start before its deadline")
                if not item.completed.is_set():
                    failure = TransportError(
                        "ConPTY resize outcome is indeterminate after its deadline"
                    )
                    concrete._writer_error = failure
                    concrete._condition.notify_all()
                    raise failure
        if item.error is not None:
            raise TransportError("failed to resize ConPTY") from item.error

    def close(self, session: RuntimeSession) -> None:
        concrete = self._session(session)
        with concrete._close_lock:
            with concrete._condition:
                if concrete._closed:
                    return
                concrete._closing = True
                concrete._write_queue.clear()
                concrete._priority_writes.clear()
                concrete._write_buffered_bytes = 0
                concrete._priority_write_buffered_bytes = 0
                for resize in concrete._resize_queue:
                    resize.error = TransportClosed("ConPTY closed before resize")
                    resize.completed.set()
                concrete._resize_queue.clear()
                concrete._condition.notify_all()
                pty = concrete._pty
                reader = concrete._reader
                writer = concrete._writer
                needs_cancel = not concrete._reader_done or concrete._native_write_active
            cancel_error: Exception | None = None
            if pty is not None and needs_cancel:
                try:
                    pty.cancel_io()
                except Exception as error:
                    cancel_error = error
            deadline = time.monotonic() + self._close_timeout_ms / 1000
            for thread in (reader, writer):
                if thread is not None:
                    thread.join(max(0.0, deadline - time.monotonic()))
            live = [
                thread.name
                for thread in (reader, writer)
                if thread is not None and thread.is_alive()
            ]
            if live:
                if cancel_error is not None:
                    raise TransportError("failed to cancel ConPTY native I/O") from cancel_error
                raise TransportError(
                    "ConPTY I/O thread did not stop before close deadline: " + ", ".join(live)
                )
            with concrete._condition:
                concrete._closed = True
                concrete._pty = None
                concrete._condition.notify_all()

    def _start_threads(self, session: ConPtySession) -> None:
        writer = Thread(
            target=self._writer_main,
            args=(session,),
            name=f"tfbash-conpty-writer-{session.session_id}",
            daemon=True,
        )
        reader = Thread(
            target=self._reader_main,
            args=(session,),
            name=f"tfbash-conpty-reader-{session.session_id}",
            daemon=True,
        )
        session._writer = writer
        session._reader = reader
        writer.start()
        try:
            reader.start()
        except Exception:
            with session._condition:
                session._closing = True
                session._condition.notify_all()
            writer.join(self._close_timeout_ms / 1000)
            raise

    def _reader_main(self, session: ConPtySession) -> None:
        pty = self._require_pty(session)
        try:
            while True:
                chunk = pty.read(blocking=True)
                if chunk:
                    encoded = chunk.encode("utf-8")
                    with session._condition:
                        if session._closing:
                            return
                        if (
                            len(session._read_buffer) + len(encoded)
                            > session._max_read_buffer_bytes
                        ):
                            raise TransportError("ConPTY read buffer capacity exceeded")
                        session._read_buffer.extend(encoded)
                        self._queue_terminal_responses_locked(session, chunk)
                        session._condition.notify_all()
                    continue
                if pty.iseof() or not pty.isalive():
                    return
                raise TransportError("ConPTY returned an empty live read")
        except Exception as error:
            ended = False
            with suppress(Exception):
                ended = pty.iseof() or not pty.isalive()
            with session._condition:
                if not session._closing and not ended:
                    session._reader_error = error
                    session._condition.notify_all()
        finally:
            with session._condition:
                session._reader_done = True
                session._condition.notify_all()

    def _writer_main(self, session: ConPtySession) -> None:
        pty = self._require_pty(session)
        try:
            while True:
                item: _WriteItem | _ResizeItem
                with session._condition:
                    while not (
                        session._closing
                        or session._writer_error is not None
                        or session._priority_writes
                        or session._resize_queue
                        or session._write_queue
                    ):
                        session._condition.wait()
                    if session._closing or session._writer_error is not None:
                        return
                    if session._priority_writes:
                        item = session._priority_writes.popleft()
                    elif session._resize_queue:
                        item = session._resize_queue.popleft()
                        item.started = True
                    else:
                        item = session._write_queue.popleft()
                    session._native_write_active = True
                if isinstance(item, _ResizeItem):
                    try:
                        pty.set_size(item.columns, item.rows)
                    except Exception as error:
                        item.error = error
                        raise
                    finally:
                        with session._condition:
                            session._native_write_active = False
                            session._condition.notify_all()
                        item.completed.set()
                    continue
                # pywinpty 3.0.5 returns zero after successful writes, including
                # non-ASCII input.  Only an exception indicates native failure.
                succeeded = False
                try:
                    pty.write(item.text)
                    succeeded = True
                finally:
                    with session._condition:
                        session._native_write_active = False
                        if item.accepted_bytes and succeeded and not session._closing:
                            # The outer exception handler seals the session if the
                            # native call failed, so capacity is only released when
                            # this write returned normally.
                            session._write_buffered_bytes -= item.accepted_bytes
                        elif not item.accepted_bytes and succeeded and not session._closing:
                            session._priority_write_buffered_bytes -= len(item.text.encode("utf-8"))
                        session._condition.notify_all()
        except Exception as error:
            with session._condition:
                if not session._closing:
                    session._writer_error = error
                    for resize in session._resize_queue:
                        resize.error = error
                        resize.completed.set()
                    session._resize_queue.clear()
                    session._condition.notify_all()
        finally:
            with session._condition:
                session._writer_done = True
                session._condition.notify_all()

    @staticmethod
    def _queue_terminal_responses_locked(session: ConPtySession, chunk: str) -> None:
        combined = session._terminal_query_tail + chunk
        cursor = 0
        while cursor < len(combined):
            candidates = [
                (position, response)
                for query, response in (
                    (_TERMINAL_STATUS_QUERY, _TERMINAL_STATUS_RESPONSE),
                    (_CURSOR_POSITION_QUERY, _CURSOR_POSITION_RESPONSE),
                )
                if (position := combined.find(query, cursor)) >= 0
            ]
            if not candidates:
                break
            position, response = min(candidates, key=lambda candidate: candidate[0])
            response_bytes = len(response.encode("utf-8"))
            if (
                session._priority_write_buffered_bytes + response_bytes
                > session._max_write_buffer_bytes + 3
            ):
                raise TransportError("ConPTY terminal response buffer capacity exceeded")
            session._priority_writes.append(_WriteItem(response, 0))
            session._priority_write_buffered_bytes += response_bytes
            cursor = position + len(_TERMINAL_STATUS_QUERY)
        session._terminal_query_tail = combined[-3:]

    def _session(self, session: RuntimeSession) -> ConPtySession:
        if not isinstance(session, ConPtySession):
            raise TransportError("session was not created by the ConPTY transport")
        if session._transport_token is not self._transport_token:
            raise TransportError("session belongs to a different ConPTY transport")
        return session

    @staticmethod
    def _require_pty(session: ConPtySession) -> ConPtyLike:
        pty = session._pty
        if pty is None:
            raise TransportClosed("ConPTY is closed")
        return pty

    @staticmethod
    def _require_open_locked(session: ConPtySession) -> None:
        if session._closing or session._closed:
            raise TransportClosed("ConPTY is closed")

    @staticmethod
    def _raise_io_failure_locked(session: ConPtySession) -> None:
        error = session._reader_error or session._writer_error
        if error is not None:
            raise TransportError("ConPTY background I/O failed") from error

    @staticmethod
    def _ready_locked(
        session: ConPtySession,
        interests: frozenset[WaitInterest],
    ) -> frozenset[WaitInterest]:
        ready: set[WaitInterest] = set()
        if WaitInterest.READABLE in interests and (session._read_buffer or session._reader_done):
            ready.add(WaitInterest.READABLE)
        if (
            WaitInterest.WRITABLE in interests
            and session._reader_error is None
            and session._writer_error is None
            and session._write_buffered_bytes < session._max_write_buffer_bytes + 3
        ):
            ready.add(WaitInterest.WRITABLE)
        # Reader completion is the tail-drain barrier: process exit is not
        # published until the sole reader has consumed all native output.
        if (
            WaitInterest.PROCESS_EXIT in interests
            and session._reader_done
            and session._reader_error is None
        ):
            ready.add(WaitInterest.PROCESS_EXIT)
        return frozenset(ready)


def _create_pywinpty_conpty(columns: int, rows: int) -> ConPtyLike:
    try:
        installed = metadata.version("pywinpty")
    except metadata.PackageNotFoundError as error:
        raise TransportError("pywinpty 3.0.5 is required for Windows ConPTY") from error
    if installed != _PYWINPTY_VERSION:
        raise TransportError(
            f"unsupported pywinpty version {installed!r}; expected {_PYWINPTY_VERSION}"
        )
    try:
        module = import_module("winpty")
        return module.PTY(columns, rows, backend=module.Backend.ConPTY)  # type: ignore[no-any-return]
    except Exception as error:
        raise TransportError("failed to create native ConPTY") from error


def _validate_spawn_request(request: SpawnRequest) -> None:
    if not request.executable or "\x00" in request.executable or '"' in request.executable:
        raise TransportError(
            "ConPTY executable must be non-empty and contain neither NUL nor quote"
        )
    if not request.cwd or "\x00" in request.cwd:
        raise TransportError("ConPTY cwd must be non-empty and NUL-free")
    if any("\x00" in argument for argument in request.arguments):
        raise TransportError("ConPTY arguments must be NUL-free")
    folded_keys: set[str] = set()
    for key, value in request.environment.items():
        if not key or "\x00" in key or "=" in key or "\x00" in value:
            raise TransportError("ConPTY environment entries are invalid")
        folded = key.casefold()
        if folded in folded_keys:
            raise TransportError("ConPTY environment keys must be unique ignoring case")
        folded_keys.add(folded)


def _windows_environment_block(environment: Mapping[str, str]) -> str:
    return "\x00".join(f"{key}={value}" for key, value in environment.items()) + "\x00"


def _valid_process_id(value: int | None) -> int | None:
    if value is None:
        return None
    process_id = int(value)
    return process_id if process_id > 0 else None


def _split_utf8_chunks(text: str) -> tuple[tuple[str, int], ...]:
    chunks: list[tuple[str, int]] = []
    start = 0
    buffered_bytes = 0
    for index, character in enumerate(text):
        character_bytes = len(character.encode("utf-8"))
        if buffered_bytes and buffered_bytes + character_bytes > _NATIVE_WRITE_CHUNK_BYTES:
            chunks.append((text[start:index], buffered_bytes))
            start = index
            buffered_bytes = 0
        buffered_bytes += character_bytes
    if buffered_bytes:
        chunks.append((text[start:], buffered_bytes))
    return tuple(chunks)
