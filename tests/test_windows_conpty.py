from __future__ import annotations

import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tfbash_mcp.runtime import (
    CleanupResult,
    ConPtySession,
    ConPtyTransport,
    ControlDelivery,
    ControlIntent,
    PowerShellDialect,
    ProcessOwnership,
    ReadStatus,
    RuntimeName,
    SpawnRequest,
    TransportClosed,
    TransportError,
    WaitInterest,
    WindowsPwshProfile,
)
from tfbash_mcp.runtime.windows_conpty import _create_pywinpty_conpty


class _Ownership:
    def __init__(self, ownership_id: str = "owner-1") -> None:
        self._ownership_id = ownership_id
        self.reserve_calls = 0
        self.attached: list[int] = []
        self.attach_error: Exception | None = None

    @property
    def ownership_id(self) -> str:
        return self._ownership_id

    def reserve(self) -> None:
        self.reserve_calls += 1
        if self.reserve_calls > 1:
            raise RuntimeError("ownership reused")

    def attach(self, process_id: int) -> None:
        self.attached.append(process_id)
        if self.attach_error is not None:
            raise self.attach_error


class _WrongOwnership:
    @property
    def ownership_id(self) -> str:
        return "wrong"


class _Supervisor:
    runtime_name = RuntimeName.WINDOWS_PWSH

    def __init__(self) -> None:
        self.owner = _Ownership()
        self.cleaned: list[str] = []

    def prepare(self) -> ProcessOwnership:
        return self.owner

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
        *,
        deadline_ms: int | None = None,
    ) -> ControlDelivery:
        return ControlDelivery(delivered=False)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        return False

    def cleanup_execution(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        return CleanupResult(reaped=True, remaining_managed_processes=0)

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        self.cleaned.append(ownership.ownership_id)
        return CleanupResult(reaped=True, remaining_managed_processes=0)


class _FakePty:
    def __init__(self, *, pid: int = 4321) -> None:
        self.pid: int | None = pid
        self.spawn_result = True
        self.spawn_error: Exception | None = None
        self.spawn_calls: list[tuple[str, str | None, str | None, str | None]] = []
        self.spawn_entered = threading.Event()
        self.spawn_gate = threading.Event()
        self.spawn_gate.set()
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.cancel_calls = 0
        self.read_calls = 0
        self.max_active_readers = 0
        self.write_error: Exception | None = None
        self.read_error: Exception | None = None
        self.resize_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.write_entered = threading.Event()
        self.write_gate = threading.Event()
        self.write_gate.set()
        self.resize_entered = threading.Event()
        self.resize_gate = threading.Event()
        self.resize_gate.set()
        self.cancel_releases_write = True
        self.cancel_releases_read = True
        self._condition = threading.Condition()
        self._chunks: deque[str] = deque()
        self._alive = True
        self._eof = False
        self._cancelled = False
        self._active_readers = 0

    def spawn(
        self,
        appname: str,
        *,
        cmdline: str | None = None,
        cwd: str | None = None,
        env: str | None = None,
    ) -> bool:
        self.spawn_calls.append((appname, cmdline, cwd, env))
        self.spawn_entered.set()
        self.spawn_gate.wait()
        if self.spawn_error is not None:
            raise self.spawn_error
        return self.spawn_result

    def read(self, *, blocking: bool = False) -> str:
        assert blocking is True
        with self._condition:
            self.read_calls += 1
            self._active_readers += 1
            self.max_active_readers = max(self.max_active_readers, self._active_readers)
            try:
                while (
                    not self._chunks
                    and not self._eof
                    and not self._cancelled
                    and self.read_error is None
                ):
                    self._condition.wait()
                if self._chunks:
                    return self._chunks.popleft()
                if self.read_error is not None:
                    raise self.read_error
                return ""
            finally:
                self._active_readers -= 1

    def write(self, value: str) -> int:
        self.write_entered.set()
        self.write_gate.wait()
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(value)
        return 0

    def set_size(self, columns: int, rows: int) -> None:
        self.resize_entered.set()
        self.resize_gate.wait()
        if self.resize_error is not None:
            raise self.resize_error
        self.sizes.append((columns, rows))

    def cancel_io(self) -> None:
        self.cancel_calls += 1
        with self._condition:
            if self.cancel_releases_read:
                self._cancelled = True
            if self.cancel_releases_write:
                self.write_gate.set()
            self._condition.notify_all()
        if self.cancel_error is not None:
            raise self.cancel_error

    def iseof(self) -> bool:
        with self._condition:
            return self._eof

    def isalive(self) -> bool:
        with self._condition:
            return self._alive

    def emit(self, text: str) -> None:
        with self._condition:
            self._chunks.append(text)
            self._condition.notify_all()

    def finish(self) -> None:
        with self._condition:
            self._alive = False
            self._eof = True
            self._condition.notify_all()

    def fail_read(self, error: Exception) -> None:
        with self._condition:
            self.read_error = error
            self._condition.notify_all()

    def release_reader(self) -> None:
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()


def _request(**changes: object) -> SpawnRequest:
    values: dict[str, object] = {
        "executable": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "arguments": ("-NoLogo", "-NoProfile", "value with spaces"),
        "cwd": r"C:\workspace",
        "environment": {"Path": r"C:\tools", "TF_VALUE": "你好🙂"},
    }
    values.update(changes)
    return SpawnRequest(**values)  # type: ignore[arg-type]


def _spawn(
    fake: _FakePty | None = None,
    **transport_options: object,
) -> tuple[ConPtyTransport, ConPtySession, _FakePty, _Ownership]:
    selected = fake or _FakePty()
    transport = ConPtyTransport(
        pty_factory=lambda _columns, _rows: selected,
        **transport_options,  # type: ignore[arg-type]
    )
    owner = _Ownership()
    session = cast(ConPtySession, transport.spawn(_request(), owner, deadline_ms=1000))
    return transport, session, selected, owner


def test_profile_cancels_blocked_conpty_spawn_and_reaps_its_late_owner() -> None:
    fake = _FakePty()
    fake.spawn_gate.clear()
    supervisor = _Supervisor()
    profile = WindowsPwshProfile(
        dialect=PowerShellDialect(token_factory=lambda: "A" * 32),
        transport=ConPtyTransport(pty_factory=lambda _columns, _rows: fake),
        supervisor=supervisor,
    )
    cancel_signal = threading.Event()
    errors: list[Exception] = []

    def open_session() -> None:
        try:
            profile.open_session(
                _request(),
                cleanup_deadline_ms=200,
                startup_deadline_ms=1000,
                cancel_signal=cancel_signal,
            )
        except Exception as error:
            errors.append(error)

    caller = threading.Thread(target=open_session)
    caller.start()
    assert fake.spawn_entered.wait(1)

    cancel_signal.set()
    caller.join(0.2)

    assert not caller.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TransportError)
    assert "cancelled" in str(errors[0])
    assert profile.has_pending_startup_cleanup

    fake.spawn_gate.set()
    assert profile.cleanup_pending_startups(deadline_ms=500)
    assert not profile.has_pending_startup_cleanup
    assert supervisor.cleaned == ["owner-1"]
    assert fake.cancel_calls == 1


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.001)


def _drain(transport: ConPtyTransport, session: ConPtySession) -> bytes:
    output = bytearray()
    while True:
        result = transport.read(session, 2)
        if result.status is ReadStatus.DATA:
            output.extend(result.data)
            continue
        if result.status is ReadStatus.EOF:
            return bytes(output)
        transport.wait(session, frozenset({WaitInterest.READABLE}), 100)


def test_spawn_binds_owner_and_passes_exact_windows_launch_values() -> None:
    dimensions: list[tuple[int, int]] = []
    fake = _FakePty()

    def factory(columns: int, rows: int) -> _FakePty:
        dimensions.append((columns, rows))
        return fake

    transport = ConPtyTransport(
        columns=132,
        rows=51,
        pty_factory=factory,
    )
    owner = _Ownership("spawn")

    session = cast(ConPtySession, transport.spawn(_request(), owner, deadline_ms=1000))

    assert transport.runtime_name is RuntimeName.WINDOWS_PWSH
    assert session.session_id == "windows_spawn"
    assert dimensions == [(132, 51)]
    assert owner.reserve_calls == 1
    assert owner.attached == [4321]
    assert fake.spawn_calls == [
        (
            '"C:\\Program Files\\PowerShell\\7\\pwsh.exe"',
            '-NoLogo -NoProfile "value with spaces"',
            r"C:\workspace",
            "Path=C:\\tools\x00TF_VALUE=你好🙂\x00",
        )
    ]
    transport.close(session)
    assert fake.cancel_calls == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"executable": ""}, "executable"),
        ({"executable": 'C:\\bad"path\\pwsh.exe'}, "executable"),
        ({"cwd": "bad\x00cwd"}, "cwd"),
        ({"arguments": ("ok", "bad\x00argument")}, "arguments"),
        ({"environment": {"BAD=KEY": "value"}}, "environment"),
        ({"environment": {"Path": "one", "PATH": "two"}}, "unique"),
    ],
)
def test_spawn_rejects_ambiguous_native_values_before_reserving_owner(
    change: dict[str, object],
    message: str,
) -> None:
    fake = _FakePty()
    transport = ConPtyTransport(pty_factory=lambda _columns, _rows: fake)
    owner = _Ownership()

    with pytest.raises(TransportError, match=message):
        transport.spawn(_request(**change), owner)

    assert owner.reserve_calls == 0
    assert not fake.spawn_calls


def test_spawn_rejects_wrong_owner_and_cancels_after_attachment_failure() -> None:
    fake = _FakePty()
    transport = ConPtyTransport(pty_factory=lambda _columns, _rows: fake)
    with pytest.raises(TransportError, match="prepared Windows ownership"):
        transport.spawn(_request(), cast(ProcessOwnership, _WrongOwnership()))

    owner = _Ownership()
    owner.attach_error = RuntimeError("injected attach failure")
    with pytest.raises(TransportError, match="failed to spawn ConPTY"):
        transport.spawn(_request(), owner)
    assert owner.attached == [4321]
    assert fake.cancel_calls == 1


@pytest.mark.parametrize("raises", [False, True])
def test_failed_spawn_attaches_any_exposed_process_before_rollback(raises: bool) -> None:
    fake = _FakePty()
    fake.spawn_result = False
    if raises:
        fake.spawn_error = OSError("injected native spawn failure")
    transport = ConPtyTransport(pty_factory=lambda _columns, _rows: fake)
    owner = _Ownership()

    with pytest.raises(TransportError, match="spawn"):
        transport.spawn(_request(), owner)

    assert owner.attached == [4321]
    assert fake.cancel_calls == 1


def test_incremental_utf8_write_and_zero_native_return_are_successful() -> None:
    transport, session, fake, _ = _spawn()
    encoded = "你🙂".encode()

    first = transport.write(session, memoryview(encoded[:2]))
    second = transport.write(session, memoryview(encoded[2:5]))
    third = transport.write(session, memoryview(encoded[5:]))

    assert [first.bytes_written, second.bytes_written, third.bytes_written] == [2, 3, 2]
    _wait_until(lambda: "".join(fake.writes) == "你🙂")
    assert transport.wait(session, frozenset({WaitInterest.WRITABLE}), 0) == frozenset(
        {WaitInterest.WRITABLE}
    )
    transport.close(session)


def test_bounded_write_queue_reports_partial_progress_and_wakes_after_drain() -> None:
    fake = _FakePty()
    fake.write_gate.clear()
    transport, session, _, _ = _spawn(fake, max_write_buffer_bytes=4)

    accepted = transport.write(session, memoryview(b"123456789"))
    assert accepted.bytes_written == 7
    assert accepted.would_block is True
    assert fake.write_entered.wait(1)
    assert transport.write(session, memoryview(b"x")).would_block is True
    assert transport.wait(session, frozenset({WaitInterest.WRITABLE}), 10) == frozenset()

    fake.write_gate.set()
    assert transport.wait(session, frozenset({WaitInterest.WRITABLE}), 1000) == frozenset(
        {WaitInterest.WRITABLE}
    )
    _wait_until(lambda: fake.writes == ["1234567"])
    transport.close(session)


def test_reader_progresses_while_native_writer_is_blocked() -> None:
    fake = _FakePty()
    fake.write_gate.clear()
    transport, session, _, _ = _spawn(fake, max_write_buffer_bytes=4)
    transport.write(session, memoryview(b"1234567"))
    assert fake.write_entered.wait(1)

    fake.emit("read while blocked 你好🙂")
    assert transport.wait(session, frozenset({WaitInterest.READABLE}), 1000) == frozenset(
        {WaitInterest.READABLE}
    )
    result = transport.read(session, 4096)
    assert result.data.decode() == "read while blocked 你好🙂"

    fake.write_gate.set()
    transport.close(session)


@pytest.mark.parametrize("iteration", range(5))
def test_process_exit_is_published_only_after_quick_exit_tail_is_drained(iteration: int) -> None:
    transport, session, fake, _ = _spawn()
    chunks = [f"head-{iteration}-", "中文", "🙂-TAIL"]
    for chunk in chunks:
        fake.emit(chunk)
    fake.finish()

    ready = transport.wait(
        session,
        frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
        1000,
    )
    assert ready == frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT})
    assert _drain(transport, session).decode() == "".join(chunks)
    assert fake.max_active_readers == 1
    transport.close(session)


def test_native_read_exception_after_process_exit_is_normal_eof() -> None:
    transport, session, fake, _ = _spawn()
    fake.emit("tail")
    fake.read_error = OSError("pywinpty raises at normal end")
    fake.finish()

    _wait_until(lambda: session._reader_done)
    assert _drain(transport, session) == b"tail"
    transport.close(session)


def test_wait_publishes_buffered_tail_before_later_reader_failure() -> None:
    transport, session, fake, _ = _spawn()
    fake.emit("TAIL")
    _wait_until(lambda: bytes(session._read_buffer) == b"TAIL")
    fake.fail_read(OSError("injected live reader failure"))
    _wait_until(lambda: session._reader_done)

    ready = transport.wait(
        session,
        frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
        100,
    )
    assert ready == frozenset({WaitInterest.READABLE})
    assert transport.read(session, 10).data == b"TAIL"
    with pytest.raises(TransportError, match="background I/O"):
        transport.wait(session, frozenset({WaitInterest.READABLE}), 100)
    transport.close(session)


def test_terminal_queries_split_across_reads_use_the_single_writer() -> None:
    transport, session, fake, _ = _spawn()
    fake.emit("prefix\x1b[")
    fake.emit("5n-middle\x1b[6")
    fake.emit("n-suffix")

    _wait_until(lambda: fake.writes == ["\x1b[0n", "\x1b[1;1R"])
    assert fake.max_active_readers == 1
    transport.close(session)


def test_terminal_query_responses_preserve_request_order() -> None:
    transport, session, fake, _ = _spawn()
    fake.emit("\x1b[6n\x1b[5n")

    _wait_until(lambda: fake.writes == ["\x1b[1;1R", "\x1b[0n"])
    transport.close(session)


def test_terminal_response_queue_is_bounded_when_native_writer_stalls() -> None:
    fake = _FakePty()
    fake.write_gate.clear()
    transport, session, _, _ = _spawn(fake, max_write_buffer_bytes=4)
    fake.emit("\x1b[5n\x1b[5n")

    _wait_until(lambda: session._reader_done)
    assert transport.wait(session, frozenset({WaitInterest.READABLE}), 10) == frozenset(
        {WaitInterest.READABLE}
    )
    assert transport.read(session, 100).data == b"\x1b[5n\x1b[5n"
    with pytest.raises(TransportError, match="background I/O"):
        transport.wait(session, frozenset({WaitInterest.READABLE}), 10)
    fake.write_gate.set()
    transport.close(session)


def test_terminal_response_preempts_remaining_chunks_of_a_long_write() -> None:
    fake = _FakePty()
    fake.write_gate.clear()
    transport, session, _, _ = _spawn(fake, max_write_buffer_bytes=10_000)
    payload = b"a" * 9000
    assert transport.write(session, memoryview(payload)).bytes_written == len(payload)
    assert fake.write_entered.wait(1)

    fake.emit("\x1b[5n")
    _wait_until(lambda: bool(session._priority_writes))
    fake.write_gate.set()
    _wait_until(lambda: len(fake.writes) == 4)

    assert fake.writes[0] == "a" * 4096
    assert fake.writes[1] == "\x1b[0n"
    assert "".join((fake.writes[0], *fake.writes[2:])) == payload.decode()
    transport.close(session)


def test_concurrent_public_reader_is_rejected() -> None:
    transport, session, _, _ = _spawn()
    assert session._read_guard.acquire(blocking=False)
    try:
        with pytest.raises(TransportError, match="concurrent ConPTY readers"):
            transport.read(session, 1)
    finally:
        session._read_guard.release()
    transport.close(session)


def test_read_buffer_overflow_and_background_write_failure_fail_closed() -> None:
    transport, session, fake, _ = _spawn(max_read_buffer_bytes=3)
    fake.emit("four")
    _wait_until(lambda: session._reader_done)
    with pytest.raises(TransportError, match="background I/O"):
        transport.read(session, 10)
    transport.close(session)

    failing = _FakePty()
    failing.write_error = OSError("injected write failure")
    transport, session, _, _ = _spawn(failing)
    transport.write(session, memoryview(b"input"))
    _wait_until(lambda: session._writer_done)
    with pytest.raises(TransportError, match="background I/O"):
        transport.wait(session, frozenset({WaitInterest.WRITABLE}), 100)
    transport.close(session)


def test_resize_is_serialized_and_native_failure_is_reported() -> None:
    transport, session, fake, _ = _spawn()
    transport.resize(session, columns=160, rows=60)
    assert fake.sizes == [(160, 60)]
    fake.resize_error = OSError("injected resize failure")
    with pytest.raises(TransportError, match="failed to resize"):
        transport.resize(session, columns=80, rows=24)
    transport.close(session)


def test_queued_resize_timeout_is_cancelled_without_late_side_effect() -> None:
    fake = _FakePty()
    fake.write_gate.clear()
    transport, session, _, _ = _spawn(fake, close_timeout_ms=20)
    transport.write(session, memoryview(b"blocked"))
    assert fake.write_entered.wait(1)

    with pytest.raises(TransportError, match="did not start"):
        transport.resize(session, columns=200, rows=70)
    fake.write_gate.set()
    _wait_until(lambda: fake.writes == ["blocked"])
    assert fake.sizes == []
    transport.close(session)


def test_active_resize_timeout_seals_indeterminate_session() -> None:
    fake = _FakePty()
    fake.resize_gate.clear()
    transport, session, _, _ = _spawn(fake, close_timeout_ms=20)

    with pytest.raises(TransportError, match="indeterminate"):
        transport.resize(session, columns=200, rows=70)
    assert fake.resize_entered.is_set()
    with pytest.raises(TransportError, match="background I/O"):
        transport.wait(session, frozenset({WaitInterest.READABLE}), 10)

    fake.resize_gate.set()
    _wait_until(lambda: session._writer_done)
    transport.close(session)


def test_close_is_idempotent_and_rejects_later_io() -> None:
    transport, session, fake, _ = _spawn()
    transport.close(session)
    transport.close(session)

    assert fake.cancel_calls == 1
    assert session._reader is not None and not session._reader.is_alive()
    assert session._writer is not None and not session._writer.is_alive()
    with pytest.raises(TransportClosed):
        transport.read(session, 1)
    with pytest.raises(TransportClosed):
        transport.write(session, memoryview(b"x"))


def test_close_timeout_retains_native_session_for_retry() -> None:
    fake = _FakePty()
    fake.cancel_releases_read = False
    transport, session, _, _ = _spawn(fake, close_timeout_ms=20)

    with pytest.raises(TransportError, match="did not stop"):
        transport.close(session)
    assert session._pty is fake
    assert session._closed is False

    fake.release_reader()
    _wait_until(lambda: session._reader_done)
    transport.close(session)
    assert session._closed is True
    assert fake.cancel_calls == 1


def test_cancel_failure_is_reported_and_retained_for_retry() -> None:
    fake = _FakePty()
    fake.cancel_error = OSError("injected cancel failure")
    fake.cancel_releases_read = False
    transport, session, _, _ = _spawn(fake, close_timeout_ms=20)

    with pytest.raises(TransportError, match="cancel"):
        transport.close(session)
    assert session._pty is fake
    fake.cancel_error = None
    fake.cancel_releases_read = True
    transport.close(session)
    assert session._closed is True
    assert fake.cancel_calls == 2


def test_cancel_error_is_harmless_when_io_threads_have_already_stopped() -> None:
    fake = _FakePty()
    fake.cancel_error = OSError("native endpoint ended during cancellation")
    transport, session, _, _ = _spawn(fake)

    transport.close(session)

    assert session._closed is True
    assert fake.cancel_calls == 1


def test_default_factory_requires_pinned_version_and_conpty_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[int, int, object]] = []

    def constructor(columns: int, rows: int, *, backend: object) -> _FakePty:
        created.append((columns, rows, backend))
        return _FakePty()

    module = SimpleNamespace(
        Backend=SimpleNamespace(ConPTY=object()),
        PTY=constructor,
    )
    monkeypatch.setattr(
        "tfbash_mcp.runtime.windows_conpty.metadata.version",
        lambda _name: "3.0.5",
    )
    monkeypatch.setattr(
        "tfbash_mcp.runtime.windows_conpty.import_module",
        lambda _name: module,
    )

    assert isinstance(_create_pywinpty_conpty(90, 30), _FakePty)
    assert created == [(90, 30, module.Backend.ConPTY)]

    monkeypatch.setattr(
        "tfbash_mcp.runtime.windows_conpty.metadata.version",
        lambda _name: "3.0.4",
    )
    with pytest.raises(TransportError, match="unsupported pywinpty version"):
        _create_pywinpty_conpty(90, 30)


def test_module_has_no_eager_winpty_import_and_exposes_no_eof_input_api() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "tfbash_mcp" / "runtime" / "windows_conpty.py"
    ).read_text()
    assert "from winpty" not in source
    assert "import winpty" not in source
    assert "winpty" not in sys.modules
    assert not hasattr(ConPtyTransport, "send_eof")
