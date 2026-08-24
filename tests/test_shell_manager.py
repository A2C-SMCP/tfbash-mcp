from __future__ import annotations

import ast
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, Event, Lock, Thread

import pytest

from tfbash_mcp.domain import (
    CapacityExceeded,
    CommandShellManager,
    ExecutionState,
    ManagerConfig,
    ShellBusy,
    ShellState,
    WorkerConfig,
)
from tfbash_mcp.runtime import (
    BashDialect,
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
    ) -> DialectSessionPlan:
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

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int | None = None,
    ) -> RuntimeSession:
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
                        tuple(self.parallel_commands)
                        if len(self.parallel_commands) == 2
                        else ()
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
            elif command not in {"hang", "slow-wrap"}:
                raise AssertionError(f"unknown fake command: {command}")
        elif payload.startswith(b"recover:"):
            correlation_id = payload.decode().split(":", 1)[1]
            concrete.continuous_correlation_id = None
            concrete.push(f"recovered:{correlation_id}:/recovered".encode())
        elif payload.startswith(b"finalize:"):
            correlation_id = payload.decode().split(":", 1)[1]
            concrete.push(f"finalized:{correlation_id}:ok".encode())
        elif payload == b"no-ready":
            pass
        else:
            raise AssertionError(f"unknown fake input: {payload!r}")
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

    def close(self, session: RuntimeSession) -> None:
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

    def prepare(self) -> ProcessOwnership:
        self.sequence += 1
        return _Ownership(f"owner-{self.sequence}")

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
    ) -> ControlDelivery:
        self.controls.append(intent)
        return ControlDelivery(delivered=True)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        return True

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
        self.cleanup_calls += 1
        if self.cleanup_calls <= self.cleanup_failures:
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
    assert supervisor.cleanup_calls == 2

    manager.shutdown()
    assert supervisor.cleanup_calls == 3


def test_startup_deadline_begins_before_dialect_preparation_and_retains_cleanup() -> None:
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
    while supervisor.cleanup_calls < 1 and time.monotonic() < cleanup_deadline:
        time.sleep(0.001)
    assert supervisor.cleanup_calls == 1
    with pytest.raises(CapacityExceeded, match="ownership count"):
        manager.open_shell(_request(profile.dialect.default_executable))

    manager.shutdown()
    assert supervisor.cleanup_calls == 2


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
    with pytest.raises(RuntimeBoundaryError, match="startup deadline"):
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

    with pytest.raises(CleanupTimeout, match="open attempt"):
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
