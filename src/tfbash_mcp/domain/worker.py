"""Platform-neutral per-Shell worker that is the sole Runtime Port caller."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Queue
from threading import Event, Lock, Thread

from tfbash_mcp.domain.models import Clock, CommandShell, Execution, ExecutionState, ShellState
from tfbash_mcp.domain.registry import ShellRegistry
from tfbash_mcp.runtime.contracts import (
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    DialectSessionPlan,
    ReadStatus,
    WaitInterest,
)
from tfbash_mcp.runtime.errors import (
    CleanupTimeout,
    ProcessControlError,
    RuntimeBoundaryError,
    TransportError,
)
from tfbash_mcp.runtime.profile import RuntimeProfile


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    startup_deadline_ms: int = 5000
    recovery_deadline_ms: int = 3000
    cleanup_deadline_ms: int = 3000
    job_cleanup_deadline_ms: int = 3000
    output_quiet_ms: int = 50
    io_wait_slice_ms: int = 100
    read_chunk_bytes: int = 65_536
    max_reads_per_cycle: int = 64

    def __post_init__(self) -> None:
        if (
            min(
                self.startup_deadline_ms,
                self.recovery_deadline_ms,
                self.cleanup_deadline_ms,
                self.job_cleanup_deadline_ms,
                self.output_quiet_ms,
                self.io_wait_slice_ms,
                self.read_chunk_bytes,
                self.max_reads_per_cycle,
            )
            <= 0
        ):
            raise ValueError("worker deadlines, wait slice, and read size must be positive")
        if self.output_quiet_ms >= self.job_cleanup_deadline_ms:
            raise ValueError("output quiet interval must be shorter than job cleanup deadline")


@dataclass(frozen=True, slots=True)
class _RunCommand:
    execution: Execution
    command: str
    deadline_ms: int


class _Stop:
    pass


_STOP = _Stop()


class _DeadlineExpired(Exception):
    pass


class _StopRequested(Exception):
    pass


class ShellWorker:
    """Serialize all operations for one persistent command Shell."""

    def __init__(
        self,
        *,
        shell: CommandShell,
        registry: ShellRegistry,
        profile: RuntimeProfile,
        config: WorkerConfig,
        clock: Clock,
        spawn: DialectSessionPlan,
        startup_deadline_ms: int,
    ) -> None:
        self._shell = shell
        self._registry = registry
        self._profile = profile
        self._protocol = spawn.protocol
        self._config = config
        self._clock = clock
        self._managed = profile.open_session(
            spawn.launch.spawn,
            cleanup_deadline_ms=config.cleanup_deadline_ms,
            startup_deadline_ms=max(0, startup_deadline_ms - clock.monotonic_ms()),
        )
        self._queue: Queue[_RunCommand | _Stop] = Queue()
        self._lifecycle_lock = Lock()
        self._stop_event = Event()
        self._stop_requested = False
        self._stop_error: RuntimeBoundaryError | None = None
        self._cleanup_pending = True
        self._thread: Thread | None = None

    def start(self, plan: DialectSessionPlan, *, startup_deadline_ms: int) -> None:
        """Finish the bounded handshake before publishing this worker."""

        if self._thread is not None:
            raise RuntimeError("shell worker is already started")
        self._await_ready(plan, startup_deadline_ms)
        thread = Thread(
            target=self._run,
            name=f"tfbash-{self._shell.shell_id}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def submit(self, execution: Execution, command: str, *, deadline_ms: int) -> None:
        with self._lifecycle_lock:
            if self._stop_requested:
                raise RuntimeError("shell worker is stopped")
            self._queue.put(_RunCommand(execution, command, deadline_ms))

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._stop_requested:
                self._stop_requested = True
                self._stop_event.set()
                if self._thread is not None:
                    self._queue.put(_STOP)
            thread = self._thread
        if thread is not None:
            thread.join()
        if self._cleanup_pending:
            self._attempt_cleanup()

    def _run(self) -> None:
        try:
            while True:
                work = self._queue.get()
                if isinstance(work, _Stop):
                    return
                try:
                    self._execute(work)
                except _StopRequested:
                    try:
                        self._finish_shell_error(work.execution)
                    except Exception as error:
                        self._stop_error = RuntimeBoundaryError(
                            "worker could not seal an execution during shutdown"
                        )
                        self._stop_error.__cause__ = error
                    return
                except Exception:
                    try:
                        self._finish_shell_error(work.execution)
                    except Exception as error:
                        self._stop_error = RuntimeBoundaryError(
                            "worker could not seal a failed execution"
                        )
                        self._stop_error.__cause__ = error
                        return
        finally:
            self._attempt_cleanup(suppress=True)

    def _execute(self, work: _RunCommand) -> None:
        frame = self._protocol.wrap_command(work.command)
        try:
            self._write_all(frame.input_bytes, deadline_ms=work.deadline_ms)
        except _DeadlineExpired:
            self._recover_timeout(work.execution, frame.correlation_id)
            return
        while True:
            if self._clock.monotonic_ms() >= work.deadline_ms:
                self._recover_timeout(work.execution, frame.correlation_id)
                return
            events, eof, _ = self._read_events(work.deadline_ms)
            for event in events:
                if event.kind is DialectEventKind.OUTPUT:
                    work.execution.append_output(event.data)
                elif event.kind is DialectEventKind.COMMAND_COMPLETE:
                    if event.correlation_id != frame.correlation_id:
                        raise RuntimeError("dialect completed a different execution")
                    work.execution.begin_finalizing()
                    self._finalize_execution(
                        work.execution,
                        exit_code=event.exit_code,
                        cwd=event.cwd,
                    )
                    return
            if eof:
                raise RuntimeError("PTY reached EOF during execution")

    def _recover_timeout(self, execution: Execution, correlation_id: str) -> None:
        execution.begin_finalizing()
        delivery = self._profile.supervisor.control(
            self._managed.ownership,
            ControlIntent.INTERRUPT,
        )
        if not delivery.delivered:
            self._finish_timeout_error(execution)
            return
        deadline_ms = self._clock.monotonic_ms() + self._config.recovery_deadline_ms
        try:
            self._write_all(self._protocol.recovery_input(), deadline_ms=deadline_ms)
        except _DeadlineExpired:
            self._finish_timeout_error(execution)
            return
        while self._clock.monotonic_ms() < deadline_ms:
            events, eof, _ = self._read_events(deadline_ms)
            for event in events:
                if event.kind is DialectEventKind.OUTPUT:
                    execution.append_output(event.data)
                elif event.kind is DialectEventKind.RECOVERED:
                    if event.correlation_id != correlation_id:
                        raise RuntimeError("dialect recovered a different execution")
                    self._cleanup_and_quiet(execution)
                    self._registry.finish_execution(
                        self._shell.shell_id,
                        execution.exec_id,
                        ExecutionState.TIMEOUT,
                        next_shell_state=ShellState.READY,
                        cwd=event.cwd,
                    )
                    return
            if eof:
                break
        self._finish_timeout_error(execution)

    def _finish_timeout_error(self, execution: Execution) -> None:
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            ExecutionState.TIMEOUT,
            next_shell_state=ShellState.ERROR,
        )

    def _finish_shell_error(self, execution: Execution) -> None:
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            ExecutionState.SHELL_ERROR,
            next_shell_state=ShellState.ERROR,
        )

    def _await_ready(self, plan: DialectSessionPlan, deadline_ms: int) -> None:
        bootstrap_sent = False
        while self._clock.monotonic_ms() < deadline_ms:
            events, eof, _ = self._read_events(deadline_ms)
            for event in events:
                if event.kind is DialectEventKind.BOOTSTRAP_REQUIRED:
                    if bootstrap_sent:
                        raise RuntimeError("dialect requested bootstrap more than once")
                    try:
                        self._write_all(plan.launch.initial_input, deadline_ms=deadline_ms)
                    except _DeadlineExpired as error:
                        raise RuntimeBoundaryError(
                            "shell startup write exceeded its deadline"
                        ) from error
                    bootstrap_sent = True
                elif event.kind is DialectEventKind.READY:
                    if event.cwd is None:
                        raise RuntimeError("dialect ready event omitted cwd")
                    self._shell.confirm_ready(cwd=event.cwd)
                    return
            if eof:
                break
        raise RuntimeError("shell did not become ready before startup deadline")

    def _read_events(
        self,
        deadline_ms: int,
    ) -> tuple[tuple[DialectEvent, ...], bool, bytes]:
        self._raise_if_stopping()
        remaining_ms = max(0, deadline_ms - self._clock.monotonic_ms())
        timeout_ms = min(self._config.io_wait_slice_ms, remaining_ms)
        self._profile.transport.wait(
            self._managed.session,
            frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
            timeout_ms,
        )
        self._raise_if_stopping()
        events: list[DialectEvent] = []
        unclaimed = bytearray()
        for _ in range(self._config.max_reads_per_cycle):
            if self._clock.monotonic_ms() >= deadline_ms:
                return tuple(events), False, bytes(unclaimed)
            result = self._profile.transport.read(
                self._managed.session,
                self._config.read_chunk_bytes,
            )
            if result.status is ReadStatus.DATA:
                parsed = self._protocol.feed(result.data)
                events.extend(parsed)
                if not parsed:
                    unclaimed.extend(result.data)
                continue
            if result.status is ReadStatus.EOF:
                events.extend(self._protocol.end_of_stream())
                return tuple(events), True, bytes(unclaimed)
            return tuple(events), False, bytes(unclaimed)
        return tuple(events), False, bytes(unclaimed)

    def _write_all(self, payload: bytes, *, deadline_ms: int) -> None:
        cursor = 0
        view = memoryview(payload)
        while cursor < len(payload):
            self._raise_if_stopping()
            if self._clock.monotonic_ms() >= deadline_ms:
                raise _DeadlineExpired
            result = self._profile.transport.write(self._managed.session, view[cursor:])
            if result.bytes_written > len(payload) - cursor:
                raise TransportError("PTY reported writing more bytes than requested")
            if result.bytes_written == 0 and not result.would_block:
                raise TransportError("PTY write made no progress")
            cursor += result.bytes_written
            if result.would_block:
                remaining_ms = max(0, deadline_ms - self._clock.monotonic_ms())
                self._profile.transport.wait(
                    self._managed.session,
                    frozenset({WaitInterest.WRITABLE}),
                    min(self._config.io_wait_slice_ms, remaining_ms),
                )
                self._raise_if_stopping()

    def _raise_if_stopping(self) -> None:
        if self._stop_event.is_set():
            raise _StopRequested

    def _finalize_execution(
        self,
        execution: Execution,
        *,
        exit_code: int | None,
        cwd: str | None,
    ) -> None:
        self._cleanup_and_quiet(execution)
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            ExecutionState.EXITED,
            next_shell_state=ShellState.READY,
            exit_code=exit_code,
            cwd=cwd,
        )

    def _cleanup_and_quiet(self, execution: Execution) -> None:
        deadline_ms = self._clock.monotonic_ms() + self._config.job_cleanup_deadline_ms
        remaining_ms = max(0, deadline_ms - self._clock.monotonic_ms())
        cleanup = self._profile.supervisor.cleanup_execution(
            self._managed.ownership,
            deadline_ms=remaining_ms,
        )
        if not cleanup.reaped:
            raise CleanupTimeout("execution descendant cleanup deadline expired")

        finalization = self._protocol.begin_finalization()
        self._write_all(finalization.input_bytes, deadline_ms=deadline_ms)
        acknowledged = False
        while self._clock.monotonic_ms() < deadline_ms and not acknowledged:
            events, eof, _ = self._read_events(deadline_ms)
            if eof:
                raise RuntimeBoundaryError("PTY reached EOF during finalization probe")
            for event in events:
                if event.kind is DialectEventKind.OUTPUT:
                    execution.append_output(event.data)
                elif event.kind is DialectEventKind.FINALIZED:
                    if event.correlation_id != finalization.correlation_id:
                        raise RuntimeBoundaryError(
                            "dialect finalized a different execution"
                        )
                    acknowledged = True
                else:
                    raise RuntimeBoundaryError(
                        "unexpected control event during finalization probe"
                    )
        if not acknowledged:
            raise CleanupTimeout("execution finalization probe deadline expired")

        quiet_until_ms = min(
            deadline_ms,
            self._clock.monotonic_ms() + self._config.output_quiet_ms,
        )
        while self._clock.monotonic_ms() < quiet_until_ms:
            events, eof, unclaimed = self._read_events(quiet_until_ms)
            if eof:
                raise RuntimeBoundaryError("PTY reached EOF during execution finalization")
            had_output = bool(unclaimed)
            if unclaimed:
                execution.append_output(unclaimed)
            for event in events:
                if event.kind is not DialectEventKind.OUTPUT:
                    raise RuntimeBoundaryError("unexpected control event during finalization")
                execution.append_output(event.data)
                had_output = True
            if had_output:
                quiet_until_ms = min(
                    deadline_ms,
                    self._clock.monotonic_ms() + self._config.output_quiet_ms,
                )
        if self._clock.monotonic_ms() >= deadline_ms:
            raise CleanupTimeout("execution output quiet deadline expired")

    def _attempt_cleanup(self, *, suppress: bool = False) -> None:
        try:
            self._cleanup_runtime()
        except RuntimeBoundaryError as error:
            self._cleanup_pending = True
            self._stop_error = error
            if not suppress:
                raise
        else:
            self._cleanup_pending = False
            self._stop_error = None

    def _cleanup_runtime(self) -> None:
        cleanup_error: RuntimeBoundaryError | None = None
        close_error: RuntimeBoundaryError | None = None
        try:
            result = self._profile.supervisor.cleanup(
                self._managed.ownership,
                deadline_ms=self._config.cleanup_deadline_ms,
            )
            if not result.reaped:
                cleanup_error = CleanupTimeout("shell cleanup deadline expired")
        except RuntimeBoundaryError as error:
            cleanup_error = error
        except Exception as error:
            cleanup_error = ProcessControlError("unexpected shell cleanup failure")
            cleanup_error.__cause__ = error
        try:
            self._profile.transport.close(self._managed.session)
        except RuntimeBoundaryError as error:
            close_error = error
        except Exception as error:
            close_error = TransportError("unexpected PTY close failure")
            close_error.__cause__ = error
        if cleanup_error is not None:
            if close_error is not None:
                cleanup_error.__context__ = close_error
            raise cleanup_error
        if close_error is not None:
            raise close_error
