from __future__ import annotations

import ast
import errno
import hashlib
import os
import select
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, cast

import pexpect  # type: ignore[import-untyped]
import pytest

from tfbash_mcp.runtime import (
    BashDialect,
    BashProtocol,
    DialectEventKind,
    PexpectPosixPtyTransport,
    PexpectPosixSession,
    PosixBashProfile,
    PosixProcessSupervisor,
    ReadStatus,
    ShellStartRequest,
    SpawnRequest,
    TransportClosed,
    TransportError,
    WaitInterest,
)


@dataclass
class _Ownership:
    ownership_id: str
    process_id: int | None = None
    fail_attach: bool = False

    def reserve(self) -> None:
        return

    def child_setup(self) -> None:
        return

    def attach(self, process_id: int, _terminal_file_descriptor: int) -> None:
        self.process_id = process_id
        if self.fail_attach:
            raise OSError("attach failed")

    def reap(self) -> None:
        if self.process_id is None:
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(self.process_id, os.WNOHANG)
            except ChildProcessError:
                return
            if waited == self.process_id:
                return
            time.sleep(0.01)
        with suppress(ProcessLookupError):
            os.kill(self.process_id, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(self.process_id, 0)


def test_profile_cancels_a_blocked_real_posix_spawn_and_retains_late_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_spawn_started = Event()
    native_spawn_release = Event()

    def blocked_spawn(*args: object, **kwargs: object) -> object:
        native_spawn_started.set()
        native_spawn_release.wait()
        raise OSError("injected late native spawn failure")

    monkeypatch.setattr(pexpect, "spawn", blocked_spawn)
    profile = PosixBashProfile(
        dialect=BashDialect(token_factory=lambda: "A" * 32),
        transport=PexpectPosixPtyTransport(),
        supervisor=PosixProcessSupervisor(),
    )
    cancel_signal = Event()
    errors: list[Exception] = []

    def open_session() -> None:
        try:
            profile.open_session(
                SpawnRequest("/bin/bash", (), "/workspace", {}),
                cleanup_deadline_ms=200,
                startup_deadline_ms=1000,
                cancel_signal=cancel_signal,
            )
        except Exception as error:
            errors.append(error)

    caller = Thread(target=open_session)
    caller.start()
    assert native_spawn_started.wait(1)

    cancel_signal.set()
    caller.join(0.2)

    assert not caller.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TransportError)
    assert "cancelled" in str(errors[0])
    assert profile.has_pending_startup_cleanup

    native_spawn_release.set()
    assert profile.cleanup_pending_startups(deadline_ms=500)
    assert not profile.has_pending_startup_cleanup


@dataclass
class _SelfCleaningFailedOwnership:
    ownership_id: str
    attempted_process_id: int | None = None

    def reserve(self) -> None:
        return

    def child_setup(self) -> None:
        return

    def attach(self, process_id: int, _terminal_file_descriptor: int) -> None:
        self.attempted_process_id = process_id
        with suppress(ProcessLookupError):
            os.kill(process_id, signal.SIGKILL)
        with suppress(ChildProcessError):
            os.waitpid(process_id, 0)
        raise OSError("attach failed after self-cleanup")


def _python_request(script: str) -> SpawnRequest:
    return SpawnRequest(
        executable=sys.executable,
        arguments=("-c", script),
        cwd=os.getcwd(),
        environment=dict(os.environ),
    )


def _synthetic_session(
    transport: PexpectPosixPtyTransport,
    file_descriptor: int = 99,
) -> PexpectPosixSession:
    session = object.__new__(PexpectPosixSession)
    session._session_id = "synthetic"
    session._transport_token = transport._transport_token
    session._file_descriptor = file_descriptor
    session._closed = False
    session._close_lock = Lock()
    session._read_guard = Lock()
    return session


def _write_all(
    transport: PexpectPosixPtyTransport,
    session: PexpectPosixSession,
    payload: bytes,
) -> None:
    cursor = 0
    deadline = time.monotonic() + 5
    while cursor < len(payload) and time.monotonic() < deadline:
        result = transport.write(session, memoryview(payload)[cursor:])
        cursor += result.bytes_written
        if result.would_block:
            transport.wait(session, frozenset({WaitInterest.WRITABLE}), 100)
    assert cursor == len(payload)


def _drain(
    transport: PexpectPosixPtyTransport,
    session: PexpectPosixSession,
    *,
    timeout_seconds: float = 5,
) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        transport.wait(
            session,
            frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
            100,
        )
        while True:
            result = transport.read(session, 65_536)
            if result.status is ReadStatus.DATA:
                output.extend(result.data)
                continue
            if result.status is ReadStatus.EOF:
                return bytes(output)
            break
    raise AssertionError("PTY did not reach EOF before the deadline")


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_transport_drives_bash_dialect_without_expect(tmp_path: Path) -> None:
    plan = BashDialect(token_factory=iter(("A" * 32, "B" * 32)).__next__).prepare_session(
        ShellStartRequest("/bin/bash", str(tmp_path), dict(os.environ), None)
    )
    protocol = cast(BashProtocol, plan.protocol)
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("bash")
    session = cast(PexpectPosixSession, transport.spawn(plan.launch.spawn, owner))
    try:
        deadline = time.monotonic() + 5
        bootstrap_required = False
        while time.monotonic() < deadline and not bootstrap_required:
            transport.wait(session, frozenset({WaitInterest.READABLE}), 100)
            initial_prompt = transport.read(session, 4096)
            if initial_prompt.status is ReadStatus.DATA:
                bootstrap_required = any(
                    event.kind is DialectEventKind.BOOTSTRAP_REQUIRED
                    for event in protocol.feed(initial_prompt.data)
                )
        assert bootstrap_required
        _write_all(transport, session, plan.launch.initial_input)
        deadline = time.monotonic() + 5
        ready = False
        while time.monotonic() < deadline and not ready:
            transport.wait(session, frozenset({WaitInterest.READABLE}), 100)
            result = transport.read(session, 4096)
            if result.status is ReadStatus.DATA:
                ready = any(
                    event.kind is DialectEventKind.READY for event in protocol.feed(result.data)
                )
        assert ready

        frame = protocol.wrap_command("printf transport-ok")
        _write_all(transport, session, frame.input_bytes)
        output = bytearray()
        completed = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not completed:
            transport.wait(session, frozenset({WaitInterest.READABLE}), 100)
            result = transport.read(session, 4096)
            if result.status is ReadStatus.DATA:
                for event in protocol.feed(result.data):
                    output.extend(event.data)
                    completed |= event.kind is DialectEventKind.COMMAND_COMPLETE
        assert completed
        assert b"transport-ok" in output
    finally:
        transport.close(session)
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_fast_exit_tail_is_drained_completely_twenty_times() -> None:
    expected = b"tail:" + b"x" * 131_072 + b":sentinel"
    for iteration in range(20):
        transport = PexpectPosixPtyTransport()
        owner = _Ownership(f"tail-{iteration}")
        request = _python_request("import os; os.write(1, " + repr(expected) + ")")
        session = cast(PexpectPosixSession, transport.spawn(request, owner))
        try:
            assert _drain(transport, session) == expected
        finally:
            transport.close(session)
            owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_backpressure_reports_partial_write_while_read_and_close_progress() -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("backpressure")
    request = _python_request(
        "import os,termios,time; "
        "a=termios.tcgetattr(0); a[3]&=~termios.ICANON; "
        "termios.tcsetattr(0,termios.TCSANOW,a); "
        "os.write(1,b'READY'); time.sleep(30)"
    )
    session = cast(PexpectPosixSession, transport.spawn(request, owner))
    try:
        assert WaitInterest.READABLE in transport.wait(
            session,
            frozenset({WaitInterest.READABLE}),
            1000,
        )
        read = transport.read(session, 4096)
        assert read.status is ReadStatus.DATA
        assert b"READY" in read.data

        saw_backpressure = False
        payload = memoryview(b"z" * 1_048_576)
        for _ in range(64):
            result = transport.write(session, payload)
            if result.would_block:
                saw_backpressure = True
                break
        assert saw_backpressure

        started = time.monotonic()
        transport.close(session)
        assert time.monotonic() - started < 0.5
    finally:
        transport.close(session)
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_cursor_retries_deliver_large_payload_exactly_once() -> None:
    payload = b"".join(hashlib.sha256(index.to_bytes(4, "big")).digest() for index in range(65_536))
    expected_digest = hashlib.sha256(payload).hexdigest().encode()
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("cursor-delivery")
    script = f"""
import hashlib
import os
import time
import tty

n = {len(payload)}
tty.setraw(0)
os.write(1, b"READY")
time.sleep(0.2)
data = bytearray()
while len(data) < n:
    chunk = os.read(0, n - len(data))
    if not chunk:
        break
    data.extend(chunk)
os.write(1, b"DIGEST:" + hashlib.sha256(data).hexdigest().encode())
"""
    session = cast(PexpectPosixSession, transport.spawn(_python_request(script), owner))
    try:
        assert WaitInterest.READABLE in transport.wait(
            session, frozenset({WaitInterest.READABLE}), 1000
        )
        assert b"READY" in transport.read(session, 4096).data
        _write_all(transport, session, payload)
        output = _drain(transport, session, timeout_seconds=10)
        assert b"DIGEST:" + expected_digest in output
    finally:
        transport.close(session)
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_close_is_idempotent_and_closed_operations_fail() -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("close")
    session = cast(
        PexpectPosixSession,
        transport.spawn(_python_request("import time; time.sleep(30)"), owner),
    )
    transport.close(session)
    transport.close(session)
    try:
        with pytest.raises(TransportClosed):
            transport.read(session, 1)
        with pytest.raises(TransportClosed):
            transport.write(session, memoryview(b"x"))
        with pytest.raises(TransportClosed):
            transport.wait(session, frozenset({WaitInterest.READABLE}), 0)
    finally:
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_attach_failure_closes_transport_resources() -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("attach-failure", fail_attach=True)
    try:
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(_python_request("import time; time.sleep(30)"), owner)
    finally:
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_attach_may_self_clean_before_raising_without_orphan() -> None:
    transport = PexpectPosixPtyTransport()
    owner = _SelfCleaningFailedOwnership("self-cleaning-attach")
    with pytest.raises(TransportError, match="failed to spawn"):
        transport.spawn(_python_request("import time; time.sleep(30)"), owner)
    assert owner.attempted_process_id is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(owner.attempted_process_id, os.WNOHANG)


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_post_attach_configuration_failure_retains_owner_and_closes_dup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("post-attach-failure")
    duplicated: list[int] = []
    real_dup = os.dup

    def record_dup(file_descriptor: int) -> int:
        result = real_dup(file_descriptor)
        duplicated.append(result)
        return result

    def fail_set_blocking(_file_descriptor: int, _blocking: bool) -> None:
        raise OSError(errno.EIO, "injected configuration failure")

    monkeypatch.setattr(os, "dup", record_dup)
    monkeypatch.setattr(os, "set_blocking", fail_set_blocking)
    try:
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(_python_request("import time; time.sleep(30)"), owner)
        assert owner.process_id is not None
        assert len(duplicated) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(duplicated[0])
        assert closed.value.errno == errno.EBADF
    finally:
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_failed_spawn_retries_duplicate_fd_close_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("duplicate-close-retry")
    duplicated: list[int] = []
    real_dup = os.dup
    real_close = os.close
    close_attempts = 0

    def record_dup(file_descriptor: int) -> int:
        result = real_dup(file_descriptor)
        duplicated.append(result)
        return result

    def fail_set_blocking(_file_descriptor: int, _blocking: bool) -> None:
        raise OSError(errno.EIO, "injected configuration failure")

    def fail_duplicate_close_once(file_descriptor: int) -> None:
        nonlocal close_attempts
        if duplicated and file_descriptor == duplicated[0] and close_attempts == 0:
            close_attempts += 1
            raise OSError(errno.EIO, "injected duplicate close failure")
        if duplicated and file_descriptor == duplicated[0]:
            close_attempts += 1
        real_close(file_descriptor)

    monkeypatch.setattr(os, "dup", record_dup)
    monkeypatch.setattr(os, "set_blocking", fail_set_blocking)
    monkeypatch.setattr(os, "close", fail_duplicate_close_once)
    try:
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(_python_request("import time; time.sleep(30)"), owner)
        assert close_attempts == 2
        assert transport._pending_file_descriptors == []
        with pytest.raises(OSError) as closed:
            os.fstat(duplicated[0])
        assert closed.value.errno == errno.EBADF
    finally:
        owner.reap()


@pytest.mark.skipif(os.name != "posix", reason="POSIX PTY test")
def test_failed_spawn_retries_pexpect_master_close_without_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = PexpectPosixPtyTransport()
    owner = _Ownership("pexpect-close-retry")
    spawned: list[Any] = []
    release_attempts = 0
    real_spawn = pexpect.spawn
    real_release = PexpectPosixPtyTransport._release_pexpect_master

    def record_spawn(*args: object, **kwargs: object) -> Any:
        child = real_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    def fail_set_blocking(_file_descriptor: int, _blocking: bool) -> None:
        raise OSError(errno.EIO, "injected configuration failure")

    def fail_master_close_once(child: Any) -> None:
        nonlocal release_attempts
        release_attempts += 1
        if release_attempts == 1:
            raise OSError(errno.EIO, "injected pexpect close failure")
        real_release(child)

    monkeypatch.setattr(pexpect, "spawn", record_spawn)
    monkeypatch.setattr(os, "set_blocking", fail_set_blocking)
    monkeypatch.setattr(
        PexpectPosixPtyTransport,
        "_release_pexpect_master",
        staticmethod(fail_master_close_once),
    )
    try:
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(_python_request("import time; time.sleep(30)"), owner)
        assert release_attempts == 2
        assert len(spawned) == 1
        child = spawned[0]
        assert child.closed
        assert child.ptyproc.fileobj.closed
        assert transport._pending_pexpect_children == []
    finally:
        owner.reap()


def test_session_cannot_cross_transport_or_have_concurrent_reader() -> None:
    transport = PexpectPosixPtyTransport()
    other = PexpectPosixPtyTransport()
    owner = _Ownership("ownership")
    session = cast(
        PexpectPosixSession,
        transport.spawn(_python_request("import time; time.sleep(30)"), owner),
    )
    try:
        with pytest.raises(TransportError, match="different POSIX transport"):
            other.read(session, 1)
        assert session._read_guard.acquire(blocking=False)
        try:
            with pytest.raises(TransportError, match="concurrent PTY readers"):
                transport.read(session, 1)
        finally:
            session._read_guard.release()
    finally:
        transport.close(session)
        owner.reap()


def test_transport_source_never_uses_blocking_expect_or_select_select() -> None:
    source_path = Path(__file__).parents[1] / "src" / "tfbash_mcp" / "runtime" / "posix_pty.py"
    tree = ast.parse(source_path.read_text())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "expect" not in calls
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "select"
        and node.func.attr == "select"
        for node in ast.walk(tree)
    )


def test_read_maps_platform_would_block_without_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)

    def would_block(_descriptor: int, _size: int) -> bytes:
        raise OSError(errno.EAGAIN, "try again")

    monkeypatch.setattr(os, "read", would_block)
    assert transport.read(session, 10).status is ReadStatus.WOULD_BLOCK


def test_write_reports_positive_partial_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 3)
    result = transport.write(session, memoryview(b"abcdef"))
    assert result.bytes_written == 3
    assert result.would_block


def test_write_maps_platform_would_block(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)

    def would_block(_descriptor: int, _data: memoryview) -> int:
        raise OSError(errno.EAGAIN, "try again")

    monkeypatch.setattr(os, "write", would_block)
    result = transport.write(session, memoryview(b"abcdef"))
    assert result.bytes_written == 0
    assert result.would_block


def test_close_failure_is_observable_and_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = PexpectPosixPtyTransport()
    read_fd, write_fd = os.pipe()
    session = _synthetic_session(transport, read_fd)
    real_close = os.close
    attempts = 0

    def fail_once(file_descriptor: int) -> None:
        nonlocal attempts
        if file_descriptor == read_fd and attempts == 0:
            attempts += 1
            raise OSError(errno.EIO, "injected close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(os, "close", fail_once)
    try:
        with pytest.raises(TransportError, match="failed to close"):
            transport.close(session)
        assert not session._closed
        assert session._file_descriptor == read_fd
        transport.close(session)
        assert session._closed
        assert session._file_descriptor == -1
    finally:
        if not session._closed:
            real_close(read_fd)
        real_close(write_fd)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ValueError("invalid registration"), TransportError),
        (OSError(errno.EBADF, "closed"), TransportClosed),
    ],
)
def test_wait_maps_registration_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: type[Exception],
) -> None:
    class _Poller:
        def register(self, _descriptor: int, _mask: int) -> None:
            raise failure

    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)
    monkeypatch.setattr(select, "poll", _Poller)
    with pytest.raises(expected):
        transport.wait(session, frozenset({WaitInterest.READABLE}), 0)


def test_wait_maps_poll_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Poller:
        def register(self, _descriptor: int, _mask: int) -> None:
            return

        def poll(self, _timeout: int) -> list[tuple[int, int]]:
            raise OSError(errno.EIO, "poll failed")

    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)
    monkeypatch.setattr(select, "poll", _Poller)
    with pytest.raises(TransportError, match="failed to wait"):
        transport.wait(session, frozenset({WaitInterest.READABLE}), 0)


def test_pollnval_is_closed_not_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Poller:
        def register(self, _descriptor: int, _mask: int) -> None:
            return

        def poll(self, _timeout: int) -> list[tuple[int, int]]:
            return [(99, select.POLLNVAL)]

    transport = PexpectPosixPtyTransport()
    session = _synthetic_session(transport)
    monkeypatch.setattr(select, "poll", _Poller)
    with pytest.raises(TransportClosed, match="invalid PTY descriptor"):
        transport.wait(
            session,
            frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
            0,
        )
