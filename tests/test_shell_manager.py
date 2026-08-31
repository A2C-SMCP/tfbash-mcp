from __future__ import annotations

import ast
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier, Condition, Event, Lock, Thread

import pytest

from tfbash_mcp.domain import (
    CapacityExceeded,
    CommandShellManager,
    Execution,
    ExecutionNotActive,
    ExecutionState,
    ManagerConfig,
    ShellBusy,
    ShellClosing,
    ShellState,
    WorkerConfig,
)
from tfbash_mcp.runtime import (
    BashDialect,
    CancellationSignal,
    CleanupResult,
    CleanupTimeout,
    CommandFrame,
    ControlDelivery,
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    DialectLaunch,
    DialectName,
    DialectSessionPlan,
    PexpectPosixPtyTransport,
    PosixBashProfile,
    PosixProcessSupervisor,
    ProcessControlError,
    ProcessOwnership,
    ReadStatus,
    RuntimeBoundaryError,
    RuntimeName,
    RuntimeSession,
    ShellStartRequest,
    SpawnRequest,
    TransportRead,
    TransportWrite,
    WaitInterest,
    WindowsPwshProfile,
)


@dataclass(frozen=True)
class _Ownership:
    ownership_id: str


@dataclass
class _Clock:
    monotonic: int = 0
    wall: int = 1_000

    def monotonic_ms(self) -> int:
        return self.monotonic

    def wall_time_ms(self) -> int:
        return self.wall


@dataclass
class _Session:
    session_id: str
    records: deque[bytes] = field(default_factory=lambda: deque([b"prompt"]))
    condition: Condition = field(default_factory=Condition)
    closed: bool = False
    continuous_correlation_id: str | None = None
    awaiting_input_correlation_id: str | None = None

    def push(self, *records: bytes) -> None:
        with self.condition:
            self.records.extend(records)
            self.condition.notify_all()


@dataclass
class _Protocol:
    sequence: int = 0
    pending_correlation_id: str | None = None

    def wrap_command(self, command: str) -> CommandFrame:
        if command == "slow-wrap":
            time.sleep(0.05)
        self.sequence += 1
        self.pending_correlation_id = f"exec-{self.sequence}"
        return CommandFrame(
            self.pending_correlation_id,
            f"run:{self.pending_correlation_id}:{command}".encode(),
        )

    def recovery_input(self) -> bytes:
        assert self.pending_correlation_id is not None
        return f"recover:{self.pending_correlation_id}".encode()

    def begin_finalization(self) -> CommandFrame:
        assert self.pending_correlation_id is not None
        return CommandFrame(
            f"finalize-{self.pending_correlation_id}",
            f"finalize:{self.pending_correlation_id}".encode(),
        )

    def feed(self, data: bytes) -> tuple[DialectEvent, ...]:
        if data == b"prompt":
            return (DialectEvent(DialectEventKind.BOOTSTRAP_REQUIRED),)
        if data == b"ready":
            return (
                DialectEvent(
                    DialectEventKind.READY,
                    cwd="/runtime-confirmed",
                    shell_version="test-1",
                ),
            )
        kind, correlation_id, payload = data.decode().split(":", 2)
        if kind == "out":
            return (DialectEvent(DialectEventKind.OUTPUT, data=payload.encode()),)
        if kind == "done":
            return (
                DialectEvent(
                    DialectEventKind.COMMAND_COMPLETE,
                    correlation_id=correlation_id,
                    exit_code=int(payload),
                    cwd="/after-command",
                ),
            )
        if kind == "recovered":
            return (
                DialectEvent(
                    DialectEventKind.RECOVERED,
                    correlation_id=correlation_id,
                    cwd=payload,
                ),
            )
        if kind == "finalized":
            return (
                DialectEvent(
                    DialectEventKind.FINALIZED,
                    correlation_id=f"finalize-{correlation_id}",
                ),
            )
        raise AssertionError(f"unknown fake record: {data!r}")

    def end_of_stream(self) -> tuple[DialectEvent, ...]:
        return ()


@dataclass
class _Dialect:
    runtime_name: RuntimeName
    prepare_started: Event = field(default_factory=Event)
    prepare_release: Event = field(default_factory=Event)
    prepared_requests: list[ShellStartRequest] = field(default_factory=list)

    @property
    def dialect_name(self) -> DialectName:
        if self.runtime_name is RuntimeName.POSIX_BASH:
            return DialectName.BASH
        return DialectName.PWSH

    @property
    def default_executable(self) -> str:
        if self.runtime_name is RuntimeName.POSIX_BASH:
            return "/fake/bash"
        return "C:\\fake\\pwsh.exe"

    def prepare_session(
        self,
        request: ShellStartRequest,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> DialectSessionPlan:
        self.prepared_requests.append(request)
        if request.startup_command == "blocked-start":
            self.prepare_started.set()
            self.prepare_release.wait()
        if request.startup_command == "slow-start":
            time.sleep(0.05)
        initial_input = b"no-ready" if request.startup_command == "never-ready" else b"bootstrap"
        return DialectSessionPlan(
            DialectLaunch(
                SpawnRequest(request.executable, (), request.cwd, request.environment),
                initial_input,
            ),
            _Protocol(),
        )


@dataclass
class _Transport:
    runtime_name: RuntimeName
    sessions: dict[str, _Session] = field(default_factory=dict)
    parallel_commands: list[tuple[_Session, str]] = field(default_factory=list)
    parallel_lock: Lock = field(default_factory=Lock)
    spawn_started: Event = field(default_factory=Event)
    spawn_release: Event = field(default_factory=Event)
    ready_hook: Callable[[], None] | None = None
    write_blocked: Event = field(default_factory=Event)
    close_called: Event = field(default_factory=Event)
    command_started: Event = field(default_factory=Event)
    user_inputs: list[bytes] = field(default_factory=list)
    user_input_chunks: list[bytes] = field(default_factory=list)
    user_write_requires_control: bool = False
    user_write_unblocked: Event = field(default_factory=Event)
    suppress_recovery: bool = False
    fail_spawn_after: int | None = None
    block_rebuild_spawn: bool = False

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> RuntimeSession:
        if self.fail_spawn_after is not None and len(self.sessions) >= self.fail_spawn_after:
            raise OSError("injected rebuild spawn failure")
        if self.block_rebuild_spawn and self.sessions:
            self.spawn_started.set()
            if not self.spawn_release.wait(
                None if deadline_ms is None else max(0, deadline_ms) / 1000
            ):
                raise RuntimeBoundaryError("injected rebuild spawn deadline")
            if cancel_signal is not None and cancel_signal.is_set():
                raise RuntimeBoundaryError("injected rebuild spawn cancellation")
        if request.environment.get("BLOCK_SPAWN") == "1":
            self.spawn_started.set()
            self.spawn_release.wait()
        session = _Session(f"session-{len(self.sessions) + 1}")
        self.sessions[session.session_id] = session
        return session

    def read(self, session: RuntimeSession, max_bytes: int) -> TransportRead:
        concrete = self._session(session)
        with concrete.condition:
            if concrete.records:
                record = concrete.records.popleft()
                if record == b"ready" and self.ready_hook is not None:
                    hook = self.ready_hook
                    self.ready_hook = None
                    hook()
                return TransportRead(ReadStatus.DATA, record)
            if concrete.continuous_correlation_id is not None:
                return TransportRead(
                    ReadStatus.DATA,
                    f"out:{concrete.continuous_correlation_id}:x".encode(),
                )
            if concrete.closed:
                return TransportRead(ReadStatus.EOF)
            return TransportRead(ReadStatus.WOULD_BLOCK)

    def write(self, session: RuntimeSession, data: memoryview) -> TransportWrite:
        concrete = self._session(session)
        payload = bytes(data)
        if payload == b"bootstrap":
            concrete.push(b"ready")
        elif payload.startswith(b"run:"):
            _, correlation_id, command = payload.decode().split(":", 2)
            if command == "short":
                concrete.push(
                    f"out:{correlation_id}:short-output".encode(),
                    f"done:{correlation_id}:0".encode(),
                )
            elif command == "long":
                Thread(
                    target=self._complete_later,
                    args=(concrete, correlation_id, 0.2),
                    daemon=True,
                ).start()
            elif command == "parallel":
                with self.parallel_lock:
                    self.parallel_commands.append((concrete, correlation_id))
                    ready = (
                        tuple(self.parallel_commands) if len(self.parallel_commands) == 2 else ()
                    )
                for ready_session, ready_correlation_id in ready:
                    ready_session.push(
                        f"out:{ready_correlation_id}:parallel-output".encode(),
                        f"done:{ready_correlation_id}:0".encode(),
                    )
            elif command == "large":
                concrete.push(
                    f"out:{correlation_id}:{'x' * 5000}".encode(),
                    f"done:{correlation_id}:0".encode(),
                )
            elif command == "flood":
                concrete.continuous_correlation_id = correlation_id
            elif command == "blocked-write":
                self.write_blocked.set()
                return TransportWrite(0, would_block=True)
            elif command == "await-input":
                concrete.awaiting_input_correlation_id = correlation_id
                self.command_started.set()
            elif command == "await-partial-input":
                concrete.awaiting_input_correlation_id = correlation_id
                self.user_write_requires_control = True
                self.command_started.set()
            elif command == "hang":
                self.command_started.set()
            elif command not in {"hang", "slow-wrap"}:
                raise AssertionError(f"unknown fake command: {command}")
        elif payload.startswith(b"recover:"):
            correlation_id = payload.decode().split(":", 1)[1]
            concrete.continuous_correlation_id = None
            if not self.suppress_recovery:
                concrete.push(f"recovered:{correlation_id}:/recovered".encode())
        elif payload.startswith(b"finalize:"):
            correlation_id = payload.decode().split(":", 1)[1]
            concrete.push(f"finalized:{correlation_id}:ok".encode())
        elif payload == b"no-ready":
            pass
        else:
            self.user_inputs.append(payload)
            if self.user_write_requires_control and not self.user_write_unblocked.is_set():
                if not self.user_input_chunks:
                    chunk = payload[:2]
                    self.user_input_chunks.append(chunk)
                    return TransportWrite(len(chunk), would_block=True)
                return TransportWrite(0, would_block=True)
            self.user_input_chunks.append(payload)
            if concrete.awaiting_input_correlation_id is not None:
                correlation_id = concrete.awaiting_input_correlation_id
                concrete.awaiting_input_correlation_id = None
                concrete.push(f"done:{correlation_id}:0".encode())
        return TransportWrite(len(payload), would_block=False)

    def wait(
        self,
        session: RuntimeSession,
        interests: frozenset[WaitInterest],
        timeout_ms: int,
    ) -> frozenset[WaitInterest]:
        concrete = self._session(session)
        with concrete.condition:
            if not concrete.records and not concrete.closed:
                concrete.condition.wait(timeout_ms / 1000)
            return frozenset({WaitInterest.READABLE}) if concrete.records else frozenset()

    def close(
        self,
        session: RuntimeSession,
        *,
        deadline_ms: int | None = None,
    ) -> None:
        concrete = self._session(session)
        concrete.closed = True
        concrete.push()
        self.close_called.set()

    @staticmethod
    def _complete_later(session: _Session, correlation_id: str, delay: float) -> None:
        time.sleep(delay)
        session.push(
            f"out:{correlation_id}:delayed-output".encode(),
            f"done:{correlation_id}:0".encode(),
        )

    def _session(self, session: RuntimeSession) -> _Session:
        return self.sessions[session.session_id]


@dataclass
class _Supervisor:
    runtime_name: RuntimeName
    sequence: int = 0
    controls: list[ControlIntent] = field(default_factory=list)
    cleanup_failures: int = 0
    cleanup_calls: int = 0
    cleanup_lock: Lock = field(default_factory=Lock)
    control_delivered: bool = True
    control_delay_s: float = 0
    control_hook: Callable[[], None] | None = None
    cleanup_execution_delay_s: float = 0
    cleanup_execution_started: Event = field(default_factory=Event)
    cleanup_execution_calls: int = 0
    cleanup_barrier: Barrier | None = None

    def prepare(self) -> ProcessOwnership:
        self.sequence += 1
        return _Ownership(f"owner-{self.sequence}")

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
        *,
        deadline_ms: int | None = None,
    ) -> ControlDelivery:
        self.controls.append(intent)
        if self.control_delay_s:
            allowed_s = (
                self.control_delay_s
                if deadline_ms is None
                else min(self.control_delay_s, max(0, deadline_ms) / 1000)
            )
            time.sleep(allowed_s)
            if allowed_s < self.control_delay_s:
                return ControlDelivery(delivered=False)
        if self.control_hook is not None:
            self.control_hook()
        return ControlDelivery(
            delivered=self.control_delivered,
            shell_rebuild_required=(
                self.control_delivered
                and self.runtime_name is RuntimeName.WINDOWS_PWSH
                and intent is ControlIntent.KILL
            ),
        )

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        return True

    def cleanup_execution(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        self.cleanup_execution_calls += 1
        self.cleanup_execution_started.set()
        if self.cleanup_execution_delay_s:
            allowed_s = min(
                self.cleanup_execution_delay_s,
                max(0, deadline_ms) / 1000,
            )
            time.sleep(allowed_s)
            self.cleanup_execution_delay_s -= allowed_s
            if self.cleanup_execution_delay_s > 0:
                return CleanupResult(reaped=False, remaining_managed_processes=1)
        return CleanupResult(reaped=True, remaining_managed_processes=0)

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        with self.cleanup_lock:
            self.cleanup_calls += 1
            cleanup_call = self.cleanup_calls
        if self.cleanup_barrier is not None:
            self.cleanup_barrier.wait(max(0, deadline_ms) / 1000)
        if cleanup_call <= self.cleanup_failures:
            return CleanupResult(reaped=False, remaining_managed_processes=1)
        return CleanupResult(reaped=True, remaining_managed_processes=0)


def _fake_profile(name: RuntimeName) -> PosixBashProfile | WindowsPwshProfile:
    dialect = _Dialect(name)
    transport = _Transport(name)
    supervisor = _Supervisor(name)
    if name is RuntimeName.POSIX_BASH:
        return PosixBashProfile(
            dialect=dialect,
            transport=transport,
            supervisor=supervisor,
        )
    return WindowsPwshProfile(
        dialect=dialect,
        transport=transport,
        supervisor=supervisor,
    )


def _request(executable: str, cwd: str = "/requested") -> ShellStartRequest:
    return ShellStartRequest(executable, cwd, {}, None)


@pytest.mark.parametrize("runtime_name", list(RuntimeName))
def test_both_runtime_profiles_reuse_the_same_manager_worker(
    runtime_name: RuntimeName,
) -> None:
    profile = _fake_profile(runtime_name)
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        assert shell.last_known_cwd == "/runtime-confirmed"

        result = manager.exec(
            shell.shell_id,
            "short",
            yield_ms=1000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )

        assert result.status is ExecutionState.EXITED
        assert result.output == "short-output"
        assert result.exit_code == 0
        assert result.cwd == "/after-command"
        assert result.shell_status is ShellState.READY
        assert result.eof is True
    finally:
        manager.shutdown()


def test_yield_busy_condition_wakeup_and_waiter_quota() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(max_read_waiters_per_execution=1),
    )
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        running = manager.exec(
            shell.shell_id,
            "long",
            yield_ms=0,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        assert running.status is ExecutionState.RUNNING

        with pytest.raises(ShellBusy):
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=0,
                timeout_ms=1000,
                max_output_bytes=4096,
            )

        reads: list[tuple[float, ExecutionState, str, int]] = []

        def wait_for_output() -> None:
            started = time.monotonic()
            result = manager.read(
                shell.shell_id,
                running.exec_id,
                cursor=running.next_cursor,
                max_bytes=1024,
                wait_ms=1000,
            )
            reads.append(
                (
                    time.monotonic() - started,
                    result.status,
                    result.output,
                    result.next_cursor,
                )
            )

        waiter = Thread(target=wait_for_output)
        waiter.start()
        deadline = time.monotonic() + 0.1
        while waiter.is_alive():
            try:
                manager.read(
                    shell.shell_id,
                    running.exec_id,
                    cursor=running.next_cursor,
                    max_bytes=1024,
                    wait_ms=1,
                )
            except CapacityExceeded:
                break
            if time.monotonic() >= deadline:
                pytest.fail("read waiter did not acquire its quota slot")
        else:
            pytest.fail("long execution completed before waiter quota was exercised")

        waiter.join(timeout=1)
        assert not waiter.is_alive()
        assert len(reads) == 1
        elapsed, status, output, next_cursor = reads[0]
        assert elapsed == pytest.approx(0.2, abs=0.12)
        assert status in {ExecutionState.RUNNING, ExecutionState.EXITED}
        assert output == "delayed-output"

        # The completed waiter released its slot.
        final = manager.read(
            shell.shell_id,
            running.exec_id,
            cursor=next_cursor,
            max_bytes=1024,
            wait_ms=1000,
        )
        assert final.eof is True
    finally:
        manager.shutdown()


def test_active_write_and_signal_are_serialized_without_claiming_completion() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        running = manager.exec(
            shell.shell_id,
            "long",
            yield_ms=0,
            timeout_ms=1000,
            max_output_bytes=4096,
        )

        assert manager.write(shell.shell_id, running.exec_id, b"") == 0
        assert manager.write(shell.shell_id, running.exec_id, b"stdin\n") == 6
        assert manager.signal(
            shell.shell_id,
            running.exec_id,
            ControlIntent.INTERRUPT,
        )
        write_deadline = time.monotonic() + 0.1
        while not transport.user_inputs and time.monotonic() < write_deadline:
            time.sleep(0.001)
        assert transport.user_inputs == [b"stdin\n"]
        assert supervisor.controls == [ControlIntent.INTERRUPT]
        assert (
            manager.read(
                shell.shell_id,
                running.exec_id,
                cursor=0,
                max_bytes=1024,
                wait_ms=0,
            ).status
            is ExecutionState.RUNNING
        )
    finally:
        manager.shutdown()


def test_stale_execution_input_and_signal_never_affect_the_current_command() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        stale = manager.exec(
            shell.shell_id,
            "short",
            yield_ms=1000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        current = manager.exec(
            shell.shell_id,
            "long",
            yield_ms=0,
            timeout_ms=1000,
            max_output_bytes=4096,
        )

        with pytest.raises(ExecutionNotActive):
            manager.write(shell.shell_id, stale.exec_id, b"stale")
        with pytest.raises(ExecutionNotActive):
            manager.signal(shell.shell_id, stale.exec_id, ControlIntent.KILL)

        assert current.status is ExecutionState.RUNNING
        assert transport.user_inputs == []
        assert supervisor.controls == []
    finally:
        manager.shutdown()


def test_busy_close_preempts_backpressure_and_wakes_accepted_reader_as_cancelled() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=20)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert transport.write_blocked.wait(1)
    results: list[ExecutionState] = []

    def wait_for_terminal() -> None:
        results.append(
            manager.read(
                shell.shell_id,
                running.exec_id,
                cursor=0,
                max_bytes=1024,
                wait_ms=1000,
            ).status
        )

    waiter = Thread(target=wait_for_terminal)
    waiter.start()
    assert manager.close_shell(shell.shell_id)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert results == [ExecutionState.CANCELLED]
    assert manager.snapshots() == ()


def test_accepted_queued_write_can_be_discarded_by_close_before_pty_delivery() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=10, operation_deadline_ms=20)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert transport.write_blocked.wait(1)

    assert manager.write(shell.shell_id, running.exec_id, b"must-not-run") == 12

    assert manager.close_shell(shell.shell_id)
    assert transport.user_inputs == []


def test_write_admission_bounds_pending_operation_and_byte_capacity() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                io_wait_slice_ms=10,
                max_pending_operations=3,
                max_pending_write_bytes=5,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert transport.write_blocked.wait(1)

    assert manager.write(shell.shell_id, running.exec_id, b"1234") == 4
    with pytest.raises(CapacityExceeded, match="write bytes"):
        manager.write(shell.shell_id, running.exec_id, b"56")
    assert manager.write(shell.shell_id, running.exec_id, b"") == 0
    assert manager.write(shell.shell_id, running.exec_id, b"") == 0
    with pytest.raises(CapacityExceeded, match="operations"):
        manager.write(shell.shell_id, running.exec_id, b"")

    worker = manager._worker(shell.shell_id)
    assert worker._pending_operations == 3
    assert worker._pending_write_bytes == 4
    assert manager.close_shell(shell.shell_id)
    assert worker._pending_operations == 0
    assert worker._pending_write_bytes == 0


def test_accepted_write_delivery_uses_execution_deadline_not_signal_wait_deadline() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                io_wait_slice_ms=5,
                operation_deadline_ms=10,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "await-input",
        yield_ms=0,
        timeout_ms=1000,
        max_output_bytes=4096,
    )
    assert transport.command_started.wait(1)

    assert manager.write(shell.shell_id, running.exec_id, b"input") == 5
    completed = manager.read(
        shell.shell_id,
        running.exec_id,
        cursor=0,
        max_bytes=4096,
        wait_ms=1000,
    )

    assert completed.status is ExecutionState.EXITED
    assert transport.user_inputs == [b"input"]
    assert manager.close_shell(shell.shell_id)


def test_partial_user_write_yields_to_read_and_queued_signal_before_resuming() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_hook = transport.user_write_unblocked.set
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=5)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "await-partial-input",
        yield_ms=0,
        timeout_ms=1000,
        max_output_bytes=4096,
    )
    assert transport.command_started.wait(1)

    assert manager.write(shell.shell_id, running.exec_id, b"abcdef") == 6
    assert manager.signal(
        shell.shell_id,
        running.exec_id,
        ControlIntent.INTERRUPT,
    )
    completed = manager.read(
        shell.shell_id,
        running.exec_id,
        cursor=0,
        max_bytes=4096,
        wait_ms=1000,
    )

    assert completed.status is ExecutionState.EXITED
    assert transport.user_input_chunks == [b"ab", b"cdef"]
    assert supervisor.controls == [ControlIntent.INTERRUPT]
    assert manager.close_shell(shell.shell_id)


def test_cancelled_queued_signal_retains_reservation_until_worker_dequeues_it() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                io_wait_slice_ms=10,
                operation_deadline_ms=20,
                max_pending_operations=1,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)
    frame_service_started = Event()
    frame_service_release = Event()
    original_frame_service = worker._service_frame_controls

    def paused_frame_service(
        active: Execution,
        *,
        execution_deadline_ms: int,
    ) -> None:
        frame_service_started.set()
        frame_service_release.wait(1)
        original_frame_service(
            active,
            execution_deadline_ms=execution_deadline_ms,
        )

    worker._service_frame_controls = paused_frame_service  # type: ignore[assignment]
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert frame_service_started.wait(1)

    with pytest.raises(RuntimeBoundaryError, match="before delivery"):
        manager.signal(shell.shell_id, running.exec_id, ControlIntent.INTERRUPT)
    with pytest.raises(CapacityExceeded, match="operations"):
        manager.signal(shell.shell_id, running.exec_id, ControlIntent.INTERRUPT)

    assert worker._pending_operations == 1
    frame_service_release.set()
    dequeue_deadline = time.monotonic() + 1
    while worker._pending_operations and time.monotonic() < dequeue_deadline:
        time.sleep(0.001)
    assert worker._pending_operations == 0
    assert manager.close_shell(shell.shell_id)


def test_command_frame_backpressure_still_services_queued_signal() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=5)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert transport.write_blocked.wait(1)

    assert manager.signal(
        shell.shell_id,
        running.exec_id,
        ControlIntent.INTERRUPT,
    )

    assert supervisor.controls == [ControlIntent.INTERRUPT]
    assert manager.close_shell(shell.shell_id)


def test_slow_signal_delivery_cannot_chain_past_execution_deadline() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    transport = profile.transport
    assert isinstance(supervisor, _Supervisor)
    assert isinstance(transport, _Transport)
    supervisor.control_delay_s = 0.05
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                io_wait_slice_ms=5,
                operation_deadline_ms=1000,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=30,
        max_output_bytes=4096,
    )
    assert transport.command_started.wait(1)
    outcomes: list[bool | Exception] = []

    def signal() -> None:
        try:
            outcomes.append(
                manager.signal(shell.shell_id, running.exec_id, ControlIntent.TERMINATE)
            )
        except Exception as error:
            outcomes.append(error)

    signalers = [Thread(target=signal) for _ in range(4)]
    for signaler in signalers:
        signaler.start()
    for signaler in signalers:
        signaler.join(1)
    completed = manager.read(
        shell.shell_id,
        running.exec_id,
        cursor=0,
        max_bytes=4096,
        wait_ms=1000,
    )

    assert completed.status is ExecutionState.TIMEOUT
    assert supervisor.controls.count(ControlIntent.TERMINATE) == 1
    assert supervisor.controls.count(ControlIntent.INTERRUPT) == 1
    assert outcomes.count(True) == 0
    assert sum(isinstance(outcome, ProcessControlError) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ExecutionNotActive) for outcome in outcomes) == 3
    assert manager.close_shell(shell.shell_id)


def test_close_fence_wins_timeout_recovery_without_delivering_interrupt() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=100,
        max_output_bytes=4096,
    )
    worker = manager._worker(shell.shell_id)
    execution = manager._registry.get_execution(shell.shell_id, running.exec_id)
    recovery_started = Event()
    recovery_release = Event()
    original_recover = worker._recover_timeout

    def paused_recovery(active: Execution, correlation_id: str) -> None:
        recovery_started.set()
        recovery_release.wait(1)
        original_recover(active, correlation_id)

    worker._recover_timeout = paused_recovery  # type: ignore[assignment]
    assert recovery_started.wait(1)
    close_result: list[bool] = []
    closer = Thread(target=lambda: close_result.append(manager.close_shell(shell.shell_id)))
    closer.start()
    cancel_deadline = time.monotonic() + 1
    while execution.state is not ExecutionState.CANCELLED and time.monotonic() < cancel_deadline:
        time.sleep(0.001)
    recovery_release.set()
    closer.join(1)

    assert close_result == [True]
    assert execution.state is ExecutionState.CANCELLED
    assert supervisor.controls == []
    manager.shutdown()


def test_signal_admitted_before_close_marker_is_delivered_in_fifo_order() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)
    service_started = Event()
    service_release = Event()
    original_service = worker._service_active_actions

    def paused_service(
        active: Execution,
        *,
        execution_deadline_ms: int,
    ) -> None:
        service_started.set()
        service_release.wait(1)
        original_service(
            active,
            execution_deadline_ms=execution_deadline_ms,
        )

    worker._service_active_actions = paused_service  # type: ignore[assignment]
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    execution = manager._registry.get_execution(shell.shell_id, running.exec_id)
    assert service_started.wait(1)
    signal_result: list[bool] = []
    close_result: list[bool] = []
    signaler = Thread(
        target=lambda: signal_result.append(
            manager.signal(shell.shell_id, running.exec_id, ControlIntent.TERMINATE)
        )
    )
    signaler.start()
    signal_deadline = time.monotonic() + 1
    while worker._pending_operations != 1 and time.monotonic() < signal_deadline:
        time.sleep(0.001)
    closer = Thread(target=lambda: close_result.append(manager.close_shell(shell.shell_id)))
    closer.start()
    service_release.set()
    signaler.join(1)
    closer.join(1)

    assert signal_result == [True]
    assert close_result == [True]
    assert supervisor.controls == [ControlIntent.TERMINATE]
    assert execution.state is ExecutionState.CANCELLED
    manager.shutdown()


def test_close_global_deadline_bounds_multiple_slow_queued_signals() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_delay_s = 0.05
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(cleanup_deadline_ms=120)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)
    service_started = Event()
    service_release = Event()
    original_service = worker._service_active_actions

    def paused_service(
        active: Execution,
        *,
        execution_deadline_ms: int,
    ) -> None:
        service_started.set()
        service_release.wait(1)
        original_service(
            active,
            execution_deadline_ms=execution_deadline_ms,
        )

    worker._service_active_actions = paused_service  # type: ignore[assignment]
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert service_started.wait(1)
    signal_outcomes: list[bool | Exception] = []

    def signal() -> None:
        try:
            signal_outcomes.append(
                manager.signal(shell.shell_id, running.exec_id, ControlIntent.TERMINATE)
            )
        except Exception as error:
            signal_outcomes.append(error)

    signalers = [Thread(target=signal) for _ in range(4)]
    for signaler in signalers:
        signaler.start()
    pending_deadline = time.monotonic() + 1
    while worker._pending_operations < 4 and time.monotonic() < pending_deadline:
        time.sleep(0.001)
    close_result: list[bool] = []
    started = time.monotonic()
    closer = Thread(target=lambda: close_result.append(manager.close_shell(shell.shell_id)))
    closer.start()
    service_release.set()
    for signaler in signalers:
        signaler.join(1)
    closer.join(1)

    assert close_result == [True]
    assert time.monotonic() - started < 0.2
    assert len(supervisor.controls) <= 2
    assert len(signal_outcomes) == 4
    assert any(isinstance(outcome, ShellClosing) for outcome in signal_outcomes)
    assert transport.close_called.is_set()
    manager.shutdown()


def test_closing_owner_still_consumes_capacity_until_stop_finishes() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(max_shells=1),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)
    stop_started = Event()
    stop_release = Event()
    original_stop = worker.stop

    def blocked_stop(*, deadline_ms: int | None = None) -> None:
        stop_started.set()
        stop_release.wait(1)
        original_stop(deadline_ms=deadline_ms)

    worker.stop = blocked_stop  # type: ignore[method-assign]
    close_result: list[bool] = []
    closer = Thread(target=lambda: close_result.append(manager.close_shell(shell.shell_id)))
    closer.start()
    assert stop_started.wait(1)

    with pytest.raises(CapacityExceeded, match="ownership count"):
        manager.open_shell(_request(profile.dialect.default_executable))

    stop_release.set()
    closer.join(1)
    assert close_result == [True]
    manager.shutdown()


def test_cleanup_attempts_pty_close_even_when_shared_deadline_is_exhausted() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    manager = CommandShellManager(profile=profile)
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)

    with pytest.raises(CleanupTimeout, match="PTY close exceeded"):
        worker._cleanup_managed(worker._managed, absolute_deadline=time.monotonic())

    assert transport.close_called.is_set()
    assert manager.close_shell(shell.shell_id)
    manager.shutdown()


def test_slow_control_is_bounded_so_close_retains_cleanup_budget() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_delay_s = 0.2
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(cleanup_deadline_ms=50)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    signal_errors: list[Exception] = []

    def signal() -> None:
        try:
            manager.signal(
                shell.shell_id,
                running.exec_id,
                ControlIntent.TERMINATE,
            )
        except Exception as error:
            signal_errors.append(error)

    signaler = Thread(target=signal)
    signaler.start()
    control_deadline = time.monotonic() + 1
    while not supervisor.controls and time.monotonic() < control_deadline:
        time.sleep(0.001)

    started = time.monotonic()
    assert manager.close_shell(shell.shell_id)

    assert time.monotonic() - started < 0.15
    assert supervisor.cleanup_calls >= 1
    assert transport.close_called.is_set()
    signaler.join(1)
    assert signal_errors
    manager.shutdown()


def test_close_preempts_bounded_execution_cleanup_and_keeps_cancelled_terminal() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.cleanup_execution_delay_s = 0.2
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(cleanup_deadline_ms=60)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "short",
        yield_ms=0,
        timeout_ms=1000,
        max_output_bytes=4096,
    )
    execution = manager._registry.get_execution(shell.shell_id, running.exec_id)
    assert supervisor.cleanup_execution_started.wait(1)

    started = time.monotonic()
    assert manager.close_shell(shell.shell_id)

    assert time.monotonic() - started < 0.15
    assert execution.state is ExecutionState.CANCELLED
    assert transport.close_called.is_set()
    manager.shutdown()


def test_execution_cleanup_retries_slices_within_its_total_job_deadline() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    supervisor.cleanup_execution_delay_s = 0.08
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                cleanup_deadline_ms=60,
                job_cleanup_deadline_ms=500,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))

    completed = manager.exec(
        shell.shell_id,
        "short",
        yield_ms=1000,
        timeout_ms=1000,
        max_output_bytes=4096,
    )

    assert completed.status is ExecutionState.EXITED
    assert supervisor.cleanup_execution_calls >= 2
    assert manager.close_shell(shell.shell_id)


def test_close_reports_incomplete_cleanup_and_reaper_retries_retained_owner() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    supervisor.cleanup_failures = 2
    manager = CommandShellManager(profile=profile)
    shell = manager.open_shell(_request(profile.dialect.default_executable))

    assert not manager.close_shell(shell.shell_id)
    cleanup_deadline = time.monotonic() + 1
    while supervisor.cleanup_calls < 3 and time.monotonic() < cleanup_deadline:
        time.sleep(0.001)
    assert supervisor.cleanup_calls == 3

    manager.shutdown()
    assert supervisor.cleanup_calls == 3


def test_shutdown_preempts_active_write_backpressure_within_one_io_slice() -> None:
    profile = _fake_profile(RuntimeName.WINDOWS_PWSH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=20)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    assert running.status is ExecutionState.RUNNING
    assert transport.write_blocked.wait(1)

    errors: list[Exception] = []
    started = time.monotonic()

    def shutdown() -> None:
        try:
            manager.shutdown()
        except Exception as error:
            errors.append(error)

    shutdown_thread = Thread(target=shutdown)
    shutdown_thread.start()
    shutdown_thread.join(timeout=1)

    assert not shutdown_thread.is_alive()
    assert time.monotonic() - started < 0.5
    assert errors == []
    assert transport.close_called.is_set()
    assert supervisor.cleanup_calls == 1


def test_shutdown_runs_all_shell_cleanup_in_parallel_under_one_deadline() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    supervisor.cleanup_barrier = Barrier(2)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            max_shells=2,
            worker=WorkerConfig(cleanup_deadline_ms=200),
        ),
    )
    manager.open_shell(_request(profile.dialect.default_executable))
    manager.open_shell(_request(profile.dialect.default_executable))

    manager.shutdown()

    assert supervisor.cleanup_calls == 2
    assert manager._pending_cleanup == {}


def test_shutdown_thread_start_failure_retains_worker_for_next_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    original_start = Thread.start
    failure_injected = False

    def injected_start(thread: Thread) -> None:
        nonlocal failure_injected
        if thread.name.startswith("tfbash-shutdown-") and not failure_injected:
            failure_injected = True
            raise RuntimeError("injected thread exhaustion")
        original_start(thread)

    monkeypatch.setattr(Thread, "start", injected_start)

    with pytest.raises(RuntimeBoundaryError, match="failed to start cleanup"):
        manager.shutdown()

    assert tuple(manager._pending_cleanup) == (shell.shell_id,)
    manager.shutdown()
    assert manager._pending_cleanup == {}
    assert supervisor.cleanup_calls == 1


def test_worker_thread_start_failure_cleans_runtime_before_open_returns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    original_start = Thread.start

    def injected_start(thread: Thread) -> None:
        if thread.name.startswith("tfbash-shell_"):
            raise RuntimeError("injected worker thread exhaustion SECRET_SENTINEL")
        original_start(thread)

    monkeypatch.setattr(Thread, "start", injected_start)
    caplog.set_level(logging.ERROR, logger="tfbash_mcp.domain.manager")

    with pytest.raises(RuntimeBoundaryError, match="failed to start the Shell worker"):
        manager.open_shell(_request(profile.dialect.default_executable))

    assert supervisor.cleanup_calls == 1
    assert transport.close_called.is_set()
    assert manager._pending_cleanup == {}
    records = [
        record
        for record in caplog.records
        if record.name == "tfbash_mcp.domain.manager" and record.levelno == logging.ERROR
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "stage=startup-handshake" in message
    assert "error_chain=RuntimeBoundaryError<-RuntimeError" in message
    assert "SECRET_SENTINEL" not in message
    manager.shutdown()


def test_shutdown_fences_active_shell_before_waiting_for_blocked_open() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    dialect = profile.dialect
    assert isinstance(transport, _Transport)
    assert isinstance(dialect, _Dialect)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            max_shells=2,
            worker=WorkerConfig(
                startup_deadline_ms=1000,
                cleanup_deadline_ms=40,
                io_wait_slice_ms=10,
            ),
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "blocked-write",
        yield_ms=0,
        timeout_ms=60_000,
        max_output_bytes=4096,
    )
    execution = manager._registry.get_execution(shell.shell_id, running.exec_id)
    assert transport.write_blocked.wait(1)
    open_errors: list[Exception] = []

    def blocked_open() -> None:
        try:
            manager.open_shell(
                ShellStartRequest(
                    profile.dialect.default_executable,
                    "/requested",
                    {},
                    "blocked-start",
                )
            )
        except Exception as error:
            open_errors.append(error)

    opener = Thread(target=blocked_open)
    opener.start()
    assert dialect.prepare_started.wait(1)

    with pytest.raises(CleanupTimeout, match="open attempt"):
        manager.shutdown()

    assert execution.state is ExecutionState.CANCELLED
    assert transport.close_called.is_set()
    dialect.prepare_release.set()
    opener.join(1)
    assert open_errors
    manager.shutdown()


def test_shutdown_reports_concurrent_close_still_in_progress() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(cleanup_deadline_ms=30)),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    worker = manager._worker(shell.shell_id)
    stop_started = Event()
    stop_release = Event()
    original_stop = worker.stop

    def blocked_stop(*, deadline_ms: int | None = None) -> None:
        stop_started.set()
        stop_release.wait(1)
        original_stop(deadline_ms=deadline_ms)

    worker.stop = blocked_stop  # type: ignore[method-assign]
    close_result: list[bool] = []
    closer = Thread(target=lambda: close_result.append(manager.close_shell(shell.shell_id)))
    closer.start()
    assert stop_started.wait(1)

    with pytest.raises(CleanupTimeout, match="concurrent Shell close"):
        manager.shutdown()

    stop_release.set()
    closer.join(1)
    assert close_result == [True]
    manager.shutdown()


def test_different_shells_execute_in_parallel_and_timeout_shell_recovers() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(profile=profile)
    try:
        first = manager.open_shell(_request(profile.dialect.default_executable))
        second = manager.open_shell(_request(profile.dialect.default_executable))
        results: list[ExecutionState] = []

        def execute(shell_id: str) -> None:
            results.append(
                manager.exec(
                    shell_id,
                    "parallel",
                    yield_ms=1000,
                    timeout_ms=1000,
                    max_output_bytes=4096,
                ).status
            )

        threads = [Thread(target=execute, args=(item.shell_id,)) for item in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        assert all(not thread.is_alive() for thread in threads)
        assert results == [ExecutionState.EXITED, ExecutionState.EXITED]

        timed_out = manager.exec(
            first.shell_id,
            "hang",
            yield_ms=1000,
            timeout_ms=30,
            max_output_bytes=4096,
        )
        assert timed_out.status is ExecutionState.TIMEOUT
        assert timed_out.shell_status is ShellState.READY
        assert (
            manager.exec(
                first.shell_id,
                "short",
                yield_ms=1000,
                timeout_ms=1000,
                max_output_bytes=4096,
            ).status
            is ExecutionState.EXITED
        )
    finally:
        manager.shutdown()


def test_failed_timeout_recovery_observably_rebuilds_and_replays_startup() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    dialect = profile.dialect
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    assert isinstance(dialect, _Dialect)
    supervisor.control_delivered = False
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(rebuild_deadline_ms=1000)),
    )
    try:
        request = ShellStartRequest(
            profile.dialect.default_executable,
            "/replayed",
            {"REPLAY": "yes"},
            "replay-me",
        )
        shell = manager.open_shell(request)
        timed_out = manager.exec(
            shell.shell_id,
            "hang",
            yield_ms=1000,
            timeout_ms=20,
            max_output_bytes=4096,
        )

        assert timed_out.status is ExecutionState.TIMEOUT
        assert timed_out.shell_status is ShellState.READY
        assert timed_out.shell_rebuilt is True
        assert timed_out.cwd == "/runtime-confirmed"
        assert supervisor.sequence == 2
        assert supervisor.cleanup_calls == 1
        assert len(transport.sessions) == 2
        assert dialect.prepared_requests == [request, request]
        assert (
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=1000,
                timeout_ms=1000,
                max_output_bytes=4096,
            ).status
            is ExecutionState.EXITED
        )
    finally:
        manager.shutdown()


def test_failed_rebuild_seals_timeout_as_error_without_false_rebuilt_flag() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_delivered = False
    transport.fail_spawn_after = 1
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        timed_out = manager.exec(
            shell.shell_id,
            "hang",
            yield_ms=1000,
            timeout_ms=20,
            max_output_bytes=4096,
        )

        assert timed_out.status is ExecutionState.TIMEOUT
        assert timed_out.shell_status is ShellState.ERROR
        assert timed_out.shell_rebuilt is False
        assert supervisor.cleanup_calls >= 2
    finally:
        manager.shutdown()


def test_forced_kill_observably_rebuilds_shell() -> None:
    profile = _fake_profile(RuntimeName.WINDOWS_PWSH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(rebuild_deadline_ms=1000)),
    )
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        running = manager.exec(
            shell.shell_id,
            "hang",
            yield_ms=0,
            timeout_ms=60_000,
            max_output_bytes=4096,
        )
        assert transport.command_started.wait(1)

        assert manager.signal(shell.shell_id, running.exec_id, ControlIntent.KILL)
        killed = manager.read(
            shell.shell_id,
            running.exec_id,
            cursor=0,
            max_bytes=4096,
            wait_ms=1000,
        )

        assert killed.status is ExecutionState.CANCELLED
        assert killed.shell_status is ShellState.READY
        assert killed.shell_rebuilt is True
        assert supervisor.sequence == 2
        assert supervisor.cleanup_calls == 1
        assert len(transport.sessions) == 2
        assert (
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=1000,
                timeout_ms=1000,
                max_output_bytes=4096,
            ).status
            is ExecutionState.EXITED
        )
    finally:
        manager.shutdown()


def test_failed_forced_kill_rebuild_seals_cancelled_as_error() -> None:
    profile = _fake_profile(RuntimeName.WINDOWS_PWSH)
    transport = profile.transport
    assert isinstance(transport, _Transport)
    transport.fail_spawn_after = 1
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        running = manager.exec(
            shell.shell_id,
            "hang",
            yield_ms=0,
            timeout_ms=60_000,
            max_output_bytes=4096,
        )
        assert transport.command_started.wait(1)

        assert manager.signal(shell.shell_id, running.exec_id, ControlIntent.KILL)
        killed = manager.read(
            shell.shell_id,
            running.exec_id,
            cursor=0,
            max_bytes=4096,
            wait_ms=1000,
        )

        assert killed.status is ExecutionState.CANCELLED
        assert killed.shell_status is ShellState.ERROR
        assert killed.shell_rebuilt is False
    finally:
        manager.shutdown()


def test_close_during_bounded_rebuild_spawn_retains_and_cleans_new_owner() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_delivered = False
    transport.block_rebuild_spawn = True
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                cleanup_deadline_ms=120,
                rebuild_deadline_ms=1000,
            )
        ),
    )
    shell = manager.open_shell(_request(profile.dialect.default_executable))
    running = manager.exec(
        shell.shell_id,
        "hang",
        yield_ms=0,
        timeout_ms=20,
        max_output_bytes=4096,
    )
    execution = manager._registry.get_execution(shell.shell_id, running.exec_id)
    assert transport.spawn_started.wait(1)

    started = time.monotonic()
    assert manager.close_shell(shell.shell_id)

    assert time.monotonic() - started < 0.2
    assert execution.state is ExecutionState.CANCELLED
    assert supervisor.sequence == 2
    assert manager._pending_cleanup == {}
    assert profile.has_pending_startup_cleanup
    shutdown_started = time.monotonic()
    with pytest.raises(CleanupTimeout, match="startup rollback"):
        manager.shutdown()
    assert time.monotonic() - shutdown_started < 0.2
    assert profile.has_pending_startup_cleanup

    transport.spawn_release.set()
    manager.shutdown()
    assert supervisor.cleanup_calls >= 3
    assert not profile.has_pending_startup_cleanup


def test_rebuild_spawn_may_use_more_than_one_control_slice_within_total_deadline() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    supervisor.control_delivered = False
    transport.block_rebuild_spawn = True
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                cleanup_deadline_ms=120,
                rebuild_deadline_ms=500,
            )
        ),
    )
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))

        def release_after_more_than_one_control_slice() -> None:
            time.sleep(0.12)
            transport.spawn_release.set()

        Thread(target=release_after_more_than_one_control_slice, daemon=True).start()
        timed_out = manager.exec(
            shell.shell_id,
            "hang",
            yield_ms=1000,
            timeout_ms=20,
            max_output_bytes=4096,
        )

        assert timed_out.status is ExecutionState.TIMEOUT
        assert timed_out.shell_status is ShellState.READY
        assert timed_out.shell_rebuilt is True
        assert supervisor.sequence == 2
    finally:
        manager.shutdown()


def test_exec_rejects_invalid_read_size_before_reserving_the_shell() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        with pytest.raises(ValueError, match="max_output_bytes"):
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=0,
                timeout_ms=1000,
                max_output_bytes=0,
            )
        assert (
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=1000,
                timeout_ms=1000,
                max_output_bytes=4096,
            ).status
            is ExecutionState.EXITED
        )
    finally:
        manager.shutdown()


def test_execution_deadline_bounds_slow_wrapping_and_continuous_reads() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(max_reads_per_cycle=4)),
    )
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        for command in ("slow-wrap", "flood"):
            result = manager.exec(
                shell.shell_id,
                command,
                yield_ms=1000,
                timeout_ms=10,
                max_output_bytes=4096,
            )
            assert result.status is ExecutionState.TIMEOUT
            assert result.shell_status is ShellState.READY
    finally:
        manager.shutdown()


def test_per_execution_output_capacity_is_enforced() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    manager = CommandShellManager(profile=profile, config=ManagerConfig(max_output_bytes=8192))
    try:
        shell = manager.open_shell(_request(profile.dialect.default_executable))
        result = manager.exec(
            shell.shell_id,
            "large",
            yield_ms=1000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        assert result.status is ExecutionState.EXITED
        assert len(result.output.encode()) == 4096
        assert result.buffer_start_cursor == 904
        assert result.truncated_before_cursor is True
        with pytest.raises(ValueError, match="max_output_bytes"):
            manager.exec(
                shell.shell_id,
                "short",
                yield_ms=1000,
                timeout_ms=1000,
                max_output_bytes=8193,
            )
    finally:
        manager.shutdown()


def test_failed_cleanup_remains_reachable_for_shutdown_retry() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(profile=profile)
    manager.open_shell(_request(profile.dialect.default_executable))
    supervisor.cleanup_failures = 2

    with pytest.raises(Exception, match="cleanup deadline"):
        manager.shutdown()
    assert supervisor.cleanup_calls == 1

    with pytest.raises(Exception, match="cleanup deadline"):
        manager.shutdown()
    assert supervisor.cleanup_calls == 2

    manager.shutdown()
    assert supervisor.cleanup_calls == 3


def test_startup_deadline_begins_before_dialect_preparation_and_retains_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="tfbash_mcp.domain.manager")
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    slow_manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            max_shells=1,
            worker=WorkerConfig(startup_deadline_ms=10),
        ),
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="startup deadline"):
        slow_manager.open_shell(
            ShellStartRequest(
                profile.dialect.default_executable,
                "/requested",
                {},
                "slow-start",
            )
        )
    assert time.monotonic() - started < 0.04
    assert slow_manager.snapshots() == ()
    assert supervisor.cleanup_calls == 0
    slow_manager.shutdown()

    profile = _fake_profile(RuntimeName.POSIX_BASH)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    supervisor.cleanup_failures = 1
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            max_shells=1,
            worker=WorkerConfig(startup_deadline_ms=10),
        ),
    )
    with pytest.raises(RuntimeError, match="startup deadline"):
        manager.open_shell(
            ShellStartRequest(
                profile.dialect.default_executable,
                "/requested",
                {},
                "never-ready",
            )
        )
    cleanup_deadline = time.monotonic() + 1
    while supervisor.cleanup_calls < 2 and time.monotonic() < cleanup_deadline:
        time.sleep(0.001)
    assert supervisor.cleanup_calls == 2
    replacement = manager.open_shell(_request(profile.dialect.default_executable))
    assert manager.close_shell(replacement.shell_id)
    assert any(
        "stage=startup-handshake" in record.getMessage()
        and "handshake_phase=startup-record" in record.getMessage()
        for record in caplog.records
        if record.name == "tfbash_mcp.domain.manager"
    )

    manager.shutdown()
    assert supervisor.cleanup_calls == 3


def test_shutdown_cancels_blocked_open_before_it_can_spawn() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    dialect = profile.dialect
    supervisor = profile.supervisor
    assert isinstance(dialect, _Dialect)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                startup_deadline_ms=1000,
                cleanup_deadline_ms=30,
            )
        ),
    )
    errors: list[Exception] = []

    def blocked_open() -> None:
        try:
            manager.open_shell(
                ShellStartRequest(
                    profile.dialect.default_executable,
                    "/requested",
                    {},
                    "blocked-start",
                )
            )
        except Exception as error:
            errors.append(error)

    opener = Thread(target=blocked_open)
    opener.start()
    assert dialect.prepare_started.wait(1)

    with pytest.raises(CleanupTimeout, match="open attempt"):
        manager.shutdown()
    assert supervisor.sequence == 0

    dialect.prepare_release.set()
    opener.join(timeout=1)
    assert not opener.is_alive()
    assert errors
    manager.shutdown()
    assert supervisor.sequence == 0


def test_startup_deadline_is_rechecked_atomically_before_publication() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    clock = _Clock()
    transport.ready_hook = lambda: setattr(clock, "monotonic", 1000)
    manager = CommandShellManager(
        profile=profile,
        clock=clock,
        config=ManagerConfig(worker=WorkerConfig(startup_deadline_ms=1000)),
    )

    with pytest.raises(RuntimeBoundaryError, match="expired before publication"):
        manager.open_shell(_request(profile.dialect.default_executable))

    assert manager.snapshots() == ()
    assert supervisor.cleanup_calls == 1
    manager.shutdown()


def test_open_and_shutdown_bound_a_blocked_native_spawn() -> None:
    profile = _fake_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            worker=WorkerConfig(
                startup_deadline_ms=40,
                cleanup_deadline_ms=30,
            )
        ),
    )

    started = time.monotonic()
    with pytest.raises(RuntimeBoundaryError, match="deadline"):
        manager.open_shell(
            ShellStartRequest(
                profile.dialect.default_executable,
                "/requested",
                {"BLOCK_SPAWN": "1"},
                None,
            )
        )
    assert time.monotonic() - started < 0.1
    assert transport.spawn_started.is_set()
    assert supervisor.sequence == 1

    with pytest.raises(CleanupTimeout, match="startup rollback"):
        manager.shutdown()
    transport.spawn_release.set()
    cleanup_deadline = time.monotonic() + 1
    while supervisor.cleanup_calls < 1 and time.monotonic() < cleanup_deadline:
        time.sleep(0.001)
    manager.shutdown()
    assert supervisor.cleanup_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="requires the POSIX runtime profile")
def test_real_posix_exec_yield_read_timeout_and_reuse(tmp_path: Path) -> None:
    profile = PosixBashProfile(
        dialect=BashDialect(),
        transport=PexpectPosixPtyTransport(),
        supervisor=PosixProcessSupervisor(),
    )
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(worker=WorkerConfig(io_wait_slice_ms=20, recovery_deadline_ms=1000)),
    )
    try:
        shell = manager.open_shell(
            ShellStartRequest("/bin/bash", str(tmp_path), dict(os.environ), None)
        )
        running = manager.exec(
            shell.shell_id,
            "printf begin; sleep 0.15; printf end",
            yield_ms=10,
            timeout_ms=2000,
            max_output_bytes=4096,
        )
        assert running.status is ExecutionState.RUNNING

        output = running.output
        cursor = running.next_cursor
        while True:
            completed = manager.read(
                shell.shell_id,
                running.exec_id,
                cursor=cursor,
                max_bytes=1024,
                wait_ms=1000,
            )
            output += completed.output
            cursor = completed.next_cursor
            if completed.status.terminal:
                break
        assert completed.exec_id == running.exec_id
        assert completed.status is ExecutionState.EXITED
        assert output == "beginend"

        timed_out = manager.exec(
            shell.shell_id,
            "(sleep 0.2; printf TIMEOUT_LATE) & sleep 30",
            yield_ms=1000,
            timeout_ms=50,
            max_output_bytes=4096,
        )
        assert timed_out.status is ExecutionState.TIMEOUT
        assert timed_out.shell_status is ShellState.READY

        reused = manager.exec(
            shell.shell_id,
            "sleep 0.3; printf reused",
            yield_ms=1000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        assert reused.status is ExecutionState.EXITED
        assert reused.output == "reused"
    finally:
        manager.shutdown()


@pytest.mark.skipif(os.name != "posix", reason="requires the POSIX runtime profile")
def test_real_posix_finalization_prevents_cross_execution_late_output(tmp_path: Path) -> None:
    profile = PosixBashProfile(
        dialect=BashDialect(),
        transport=PexpectPosixPtyTransport(),
        supervisor=PosixProcessSupervisor(),
    )
    manager = CommandShellManager(profile=profile)
    try:
        shell = manager.open_shell(
            ShellStartRequest("/bin/bash", str(tmp_path), dict(os.environ), None)
        )
        ready_file = tmp_path / "background-ready"
        first = manager.exec(
            shell.shell_id,
            (
                "(trap 'printf ONTERM; exit 0' TERM; "
                f": > '{ready_file}'; while :; do sleep 1; done) & "
                f"while [ ! -f '{ready_file}' ]; do :; done; printf FIRST"
            ),
            yield_ms=3000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        assert first.status is ExecutionState.EXITED
        assert "FIRST" in first.output
        assert "ONTERM" in first.output

        second = manager.exec(
            shell.shell_id,
            "sleep 0.3; printf SECOND",
            yield_ms=1000,
            timeout_ms=1000,
            max_output_bytes=4096,
        )
        assert second.status is ExecutionState.EXITED
        assert second.output == "SECOND"
        assert "LATE" not in second.output
    finally:
        manager.shutdown()


def test_worker_has_no_platform_implementation_imports() -> None:
    source = Path("src/tfbash_mcp/domain/worker.py").read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }

    assert not imported & {"os", "pexpect", "signal", "subprocess"}
    assert all("posix" not in name and "windows" not in name for name in imported)
