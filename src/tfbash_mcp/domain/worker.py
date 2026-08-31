"""Platform-neutral per-Shell worker that is the sole Runtime Port caller."""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import NoReturn

from tfbash_mcp.domain.errors import CapacityExceeded, ExecutionNotActive, ShellClosing
from tfbash_mcp.domain.models import Clock, CommandShell, Execution, ExecutionState, ShellState
from tfbash_mcp.domain.registry import ShellRegistry
from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    DialectSessionPlan,
    ReadStatus,
    ShellStartRequest,
    WaitInterest,
)
from tfbash_mcp.runtime.errors import (
    CleanupTimeout,
    ProcessControlError,
    RuntimeBoundaryError,
    StartupHandshakeError,
    TransportError,
)
from tfbash_mcp.runtime.profile import ManagedRuntimeSession, RuntimeProfile

_LOGGER = logging.getLogger(__name__)


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
    operation_deadline_ms: int = 3000
    rebuild_deadline_ms: int = 5000
    max_pending_operations: int = 64
    max_pending_write_bytes: int = 262_144

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
                self.operation_deadline_ms,
                self.rebuild_deadline_ms,
                self.max_pending_operations,
                self.max_pending_write_bytes,
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


class _ActionGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._started = False
        self._cancelled = False

    def try_start(self) -> bool:
        with self._lock:
            if self._cancelled or self._started:
                return False
            self._started = True
            return True

    def cancel_if_pending(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._cancelled = True
            return True


@dataclass(slots=True)
class _WriteInput:
    execution: Execution
    payload: bytes
    gate: _ActionGate
    completed: Event
    accepted_bytes: int | None = None
    error: Exception | None = None
    offset: int = 0
    in_progress: bool = False
    reserved_bytes: int = 0
    operation_reserved: bool = False


@dataclass(slots=True)
class _SignalExecution:
    execution: Execution
    intent: ControlIntent
    gate: _ActionGate
    completed: Event
    delivered: bool | None = None
    error: Exception | None = None
    operation_reserved: bool = False


@dataclass(slots=True)
class CloseRequest:
    reached: Event
    deadline: float


class _Stop:
    pass


_STOP = _Stop()


class _DeadlineExpired(Exception):
    pass


class _ShellRebuildRequired(Exception):
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
        start_request: ShellStartRequest,
        startup_deadline_ms: int,
        startup_cancel_signal: CancellationSignal | None = None,
    ) -> None:
        self._shell = shell
        self._registry = registry
        self._profile = profile
        self._protocol = spawn.protocol
        self._start_request = start_request
        self._config = config
        self._clock = clock
        self._managed = profile.open_session(
            spawn.launch.spawn,
            cleanup_deadline_ms=config.cleanup_deadline_ms,
            startup_deadline_ms=max(0, startup_deadline_ms - clock.monotonic_ms()),
            cancel_signal=startup_cancel_signal,
        )
        self._queue: Queue[_RunCommand | _WriteInput | _SignalExecution | CloseRequest | _Stop] = (
            Queue()
        )
        self._lifecycle_lock = Lock()
        self._stop_call_lock = Lock()
        self._pending_lock = Lock()
        self._pending_operations = 0
        self._pending_write_bytes = 0
        self._stop_event = Event()
        self._close_requested = Event()
        self._close_deadline: float | None = None
        self._stop_requested = False
        self._stop_error: RuntimeBoundaryError | None = None
        self._cleanup_pending = True
        self._thread: Thread | None = None

    def start(self, plan: DialectSessionPlan, *, startup_deadline_ms: int) -> None:
        """Finish the bounded handshake before publishing this worker."""

        if self._thread is not None:
            raise RuntimeError("shell worker is already started")
        cwd = self._await_ready(plan, startup_deadline_ms)
        self._shell.confirm_ready(cwd=cwd)
        thread = Thread(
            target=self._run,
            name=f"tfbash-{self._shell.shell_id}",
            daemon=True,
        )
        self._thread = thread
        try:
            thread.start()
        except Exception as error:
            self._thread = None
            boundary_error = RuntimeBoundaryError("failed to start the Shell worker")
            raise boundary_error from error

    def submit(self, execution: Execution, command: str, *, deadline_ms: int) -> None:
        with self._lifecycle_lock:
            if self._stop_requested:
                raise ShellClosing(f"shell {self._shell.shell_id} is closing")
            self._queue.put(_RunCommand(execution, command, deadline_ms))

    def accept_write(self, execution: Execution, payload: bytes) -> int:
        action = _WriteInput(
            execution,
            payload,
            _ActionGate(),
            Event(),
            reserved_bytes=len(payload),
        )
        self._reserve_and_enqueue(action)
        return len(payload)

    def reserve_signal(
        self,
        execution: Execution,
        intent: ControlIntent,
    ) -> _SignalExecution:
        action = _SignalExecution(execution, intent, _ActionGate(), Event())
        self._reserve_and_enqueue(action)
        return action

    def reserve_close(self, *, deadline_ms: int) -> CloseRequest:
        action = CloseRequest(
            Event(),
            time.monotonic() + max(0, deadline_ms) / 1000,
        )
        with self._lifecycle_lock:
            if self._stop_requested:
                raise ShellClosing(f"shell {self._shell.shell_id} is already closing")
            self._queue.put(action)
            self._close_deadline = action.deadline
            self._close_requested.set()
        return action

    def finish_close(self, action: CloseRequest) -> None:
        dispatch_deadline = max(
            time.monotonic(),
            action.deadline - self._close_cleanup_reserve_ms / 1000,
        )
        action.reached.wait(max(0.0, dispatch_deadline - time.monotonic()))
        if not action.reached.is_set():
            self._registry.cancel_active_for_close(self._shell.shell_id)
            self.request_stop()
        self.stop(deadline_ms=self._remaining_ms(action.deadline))

    def await_signal(self, action: _SignalExecution) -> bool:
        if not action.completed.wait(self._config.operation_deadline_ms / 1000):
            if action.gate.cancel_if_pending():
                raise RuntimeBoundaryError("worker control deadline expired before delivery")
            raise RuntimeBoundaryError("worker control outcome is indeterminate after deadline")
        if action.error is not None:
            raise action.error
        if action.delivered is None:
            raise RuntimeBoundaryError("worker control completed without a result")
        return action.delivered

    def _reserve_and_enqueue(self, action: _WriteInput | _SignalExecution) -> None:
        with self._lifecycle_lock:
            if self._stop_requested:
                raise ShellClosing(f"shell {self._shell.shell_id} is closing")
            with self._pending_lock:
                write_bytes = action.reserved_bytes if isinstance(action, _WriteInput) else 0
                if self._pending_operations >= self._config.max_pending_operations:
                    raise CapacityExceeded("maximum pending Shell operations reached")
                if self._pending_write_bytes + write_bytes > self._config.max_pending_write_bytes:
                    raise CapacityExceeded("maximum pending Shell write bytes reached")
                self._pending_operations += 1
                self._pending_write_bytes += write_bytes
                action.operation_reserved = True
            self._queue.put(action)

    def request_stop(self) -> None:
        """Publish the close fence without waiting for worker or runtime cleanup."""

        with self._lifecycle_lock:
            if not self._stop_requested:
                self._stop_requested = True
                self._stop_event.set()
                if self._thread is not None:
                    self._queue.put(_STOP)

    def stop(self, *, deadline_ms: int | None = None) -> None:
        budget_ms = self._config.cleanup_deadline_ms if deadline_ms is None else deadline_ms
        deadline = time.monotonic() + max(0, budget_ms) / 1000
        self.request_stop()
        if not self._stop_call_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise CleanupTimeout("another Shell cleanup attempt exceeded the stop deadline")
        try:
            with self._lifecycle_lock:
                thread = self._thread
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    raise CleanupTimeout("Shell worker did not stop before cleanup deadline")
            if self._cleanup_pending:
                self._attempt_cleanup(absolute_deadline=deadline)
        finally:
            self._stop_call_lock.release()

    def _run(self) -> None:
        try:
            while True:
                work = self._queue.get()
                if isinstance(work, _Stop):
                    return
                if isinstance(work, CloseRequest):
                    try:
                        self._handle_close(work)
                    except _StopRequested:
                        return
                if isinstance(work, _WriteInput | _SignalExecution):
                    self._reject_action(
                        work,
                        ExecutionNotActive("execution is no longer active"),
                    )
                    if self._stop_event.is_set():
                        return
                    continue
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
                    if self._close_requested.is_set():
                        try:
                            self._drain_to_close(work.execution)
                        except _StopRequested:
                            return
                    try:
                        self._finish_shell_error(work.execution)
                    except Exception as error:
                        self._stop_error = RuntimeBoundaryError(
                            "worker could not seal a failed execution"
                        )
                        self._stop_error.__cause__ = error
                        return
        finally:
            self._fail_pending_actions()
            if not self._stop_requested:
                self._attempt_cleanup(suppress=True)

    def _execute(self, work: _RunCommand) -> None:
        frame = self._protocol.wrap_command(work.command)
        try:
            self._write_all(
                frame.input_bytes,
                deadline_ms=work.deadline_ms,
                active_execution=work.execution,
            )
        except _DeadlineExpired:
            self._recover_timeout(work.execution, frame.correlation_id)
            return
        except _ShellRebuildRequired:
            self._recover_forced_kill(work.execution)
            return
        while True:
            if self._clock.monotonic_ms() >= work.deadline_ms:
                self._recover_timeout(work.execution, frame.correlation_id)
                return
            try:
                self._service_active_actions(
                    work.execution,
                    execution_deadline_ms=work.deadline_ms,
                )
            except _DeadlineExpired:
                self._recover_timeout(work.execution, frame.correlation_id)
                return
            except _ShellRebuildRequired:
                self._recover_forced_kill(work.execution)
                return
            events, eof, _ = self._read_events(work.deadline_ms)
            if self._clock.monotonic_ms() >= work.deadline_ms:
                self._recover_timeout(work.execution, frame.correlation_id)
                return
            try:
                self._service_active_actions(
                    work.execution,
                    execution_deadline_ms=work.deadline_ms,
                )
            except _DeadlineExpired:
                self._recover_timeout(work.execution, frame.correlation_id)
                return
            except _ShellRebuildRequired:
                self._recover_forced_kill(work.execution)
                return
            for event in events:
                if event.kind is DialectEventKind.OUTPUT:
                    work.execution.append_output(event.data)
                elif event.kind is DialectEventKind.COMMAND_COMPLETE:
                    if event.correlation_id != frame.correlation_id:
                        raise RuntimeError("dialect completed a different execution")
                    if not work.execution.begin_finalizing():
                        return
                    self._reject_inactive_actions(work.execution)
                    self._finalize_execution(
                        work.execution,
                        exit_code=event.exit_code,
                        cwd=event.cwd,
                    )
                    return
            if eof:
                raise RuntimeError("PTY reached EOF during execution")

    def _service_active_actions(
        self,
        execution: Execution,
        *,
        execution_deadline_ms: int,
    ) -> None:
        if self._close_requested.is_set():
            self._drain_to_close(execution)
        while True:
            if self._clock.monotonic_ms() >= execution_deadline_ms:
                raise _DeadlineExpired
            try:
                action = self._queue.get_nowait()
            except Empty:
                return
            if isinstance(action, _Stop):
                raise _StopRequested
            if isinstance(action, CloseRequest):
                self._handle_close(action)
            if isinstance(action, _RunCommand):
                raise RuntimeError("multiple commands were admitted to one Shell worker")
            if self._stop_event.is_set():
                self._reject_action(
                    action,
                    ShellClosing(f"shell {self._shell.shell_id} is closing"),
                )
                raise _StopRequested
            if isinstance(action, _WriteInput) and action.in_progress:
                pass
            elif not action.gate.try_start():
                self._release_action(action, complete=True)
                action.completed.set()
                continue
            elif isinstance(action, _WriteInput):
                action.in_progress = True
            fatal_error: Exception | None = None
            execution_expired = False
            rebuild_required = False
            action_complete = True
            try:
                if action.execution is not execution:
                    raise ExecutionNotActive("worker action targeted a different execution")
                execution.require_active_input()
                if isinstance(action, _WriteInput):
                    action_complete = self._advance_write(action)
                else:
                    delivery = self._profile.supervisor.control(
                        self._managed.ownership,
                        action.intent,
                        deadline_ms=self._control_deadline_ms(execution_deadline_ms),
                    )
                    if not delivery.delivered:
                        raise ProcessControlError("semantic control was not delivered")
                    action.delivered = True
                    rebuild_required = delivery.shell_rebuild_required
                    execution_expired = self._clock.monotonic_ms() >= execution_deadline_ms
            except Exception as error:
                action.error = error
                if isinstance(action, _WriteInput) and not isinstance(
                    error,
                    ExecutionNotActive,
                ):
                    fatal_error = error
                action_complete = True
            if action_complete:
                self._release_action(action, complete=True)
                action.completed.set()
            else:
                self._queue.put(action)
                return
            if fatal_error is not None:
                raise fatal_error
            if rebuild_required:
                raise _ShellRebuildRequired
            if execution_expired:
                raise _DeadlineExpired

    def _reject_action(
        self,
        action: _WriteInput | _SignalExecution,
        error: Exception,
    ) -> None:
        if action.gate.try_start():
            action.error = error
        self._release_action(action, complete=True)
        action.completed.set()

    def _release_action(
        self,
        action: _WriteInput | _SignalExecution,
        *,
        delivered_bytes: int = 0,
        complete: bool = False,
    ) -> None:
        with self._pending_lock:
            if isinstance(action, _WriteInput) and action.reserved_bytes:
                released = min(action.reserved_bytes, delivered_bytes)
                action.reserved_bytes -= released
                self._pending_write_bytes -= released
                if complete and action.reserved_bytes:
                    self._pending_write_bytes -= action.reserved_bytes
                    action.reserved_bytes = 0
            if complete and action.operation_reserved:
                self._pending_operations -= 1
                action.operation_reserved = False

    def _advance_write(self, action: _WriteInput) -> bool:
        if action.offset == len(action.payload):
            action.accepted_bytes = len(action.payload)
            return True
        view = memoryview(action.payload)[action.offset :]
        result = self._profile.transport.write(self._managed.session, view)
        if result.bytes_written > len(view):
            raise TransportError("PTY reported writing more bytes than requested")
        if result.bytes_written == 0 and not result.would_block:
            raise TransportError("PTY write made no progress")
        action.offset += result.bytes_written
        if result.bytes_written:
            self._release_action(action, delivered_bytes=result.bytes_written)
        if action.offset == len(action.payload):
            action.accepted_bytes = len(action.payload)
            return True
        return False

    def _fail_pending_actions(self) -> None:
        while True:
            try:
                action = self._queue.get_nowait()
            except Empty:
                return
            if isinstance(action, _WriteInput | _SignalExecution):
                self._reject_action(
                    action,
                    ShellClosing(f"shell {self._shell.shell_id} is closing"),
                )
            elif isinstance(action, CloseRequest):
                self._registry.cancel_active_for_close(self._shell.shell_id)
                action.reached.set()

    def _reject_inactive_actions(self, execution: Execution) -> None:
        while True:
            try:
                action = self._queue.get_nowait()
            except Empty:
                return
            if isinstance(action, _Stop):
                raise _StopRequested
            if isinstance(action, CloseRequest):
                self._handle_close(action)
            if isinstance(action, _RunCommand):
                raise RuntimeError("multiple commands were admitted to one Shell worker")
            self._reject_action(
                action,
                ExecutionNotActive(f"execution {execution.exec_id} is no longer active"),
            )

    def _handle_close(self, action: CloseRequest) -> NoReturn:
        self._registry.cancel_active_for_close(self._shell.shell_id)
        self.request_stop()
        action.reached.set()
        raise _StopRequested

    def _drain_to_close(self, execution: Execution) -> None:
        while True:
            action = self._queue.get_nowait()
            if isinstance(action, _Stop):
                raise _StopRequested
            if isinstance(action, CloseRequest):
                self._handle_close(action)
            if isinstance(action, _RunCommand):
                raise RuntimeError("multiple commands were admitted to one Shell worker")
            if isinstance(action, _WriteInput):
                self._reject_action(
                    action,
                    ShellClosing(f"shell {self._shell.shell_id} is closing"),
                )
                continue
            delivery_budget_ms = self._close_delivery_budget_ms
            if delivery_budget_ms <= 0:
                self._reject_action(
                    action,
                    ShellClosing(f"shell {self._shell.shell_id} close deadline preempted control"),
                )
                continue
            if not action.gate.try_start():
                self._release_action(action, complete=True)
                action.completed.set()
                continue
            try:
                if action.execution is not execution:
                    raise ExecutionNotActive("worker action targeted a different execution")
                execution.require_active_input()
                delivery = self._profile.supervisor.control(
                    self._managed.ownership,
                    action.intent,
                    deadline_ms=min(
                        self._control_budget_ms,
                        delivery_budget_ms,
                    ),
                )
                if not delivery.delivered:
                    raise ProcessControlError("semantic control was not delivered")
                action.delivered = True
            except Exception as error:
                action.error = error
            finally:
                self._release_action(action, complete=True)
                action.completed.set()

    def _service_frame_controls(
        self,
        execution: Execution,
        *,
        execution_deadline_ms: int,
    ) -> None:
        staged_writes: list[_WriteInput] = []
        while True:
            try:
                action = self._queue.get_nowait()
            except Empty:
                for write in staged_writes:
                    self._queue.put(write)
                return
            if isinstance(action, _Stop):
                for write in staged_writes:
                    self._reject_action(
                        write,
                        ShellClosing(f"shell {self._shell.shell_id} is closing"),
                    )
                raise _StopRequested
            if isinstance(action, CloseRequest):
                for write in staged_writes:
                    self._reject_action(
                        write,
                        ShellClosing(f"shell {self._shell.shell_id} is closing"),
                    )
                self._handle_close(action)
            if isinstance(action, _RunCommand):
                for write in staged_writes:
                    self._reject_action(
                        write,
                        ExecutionNotActive("execution command admission was violated"),
                    )
                raise RuntimeError("multiple commands were admitted to one Shell worker")
            if isinstance(action, _WriteInput):
                staged_writes.append(action)
                continue
            if not action.gate.try_start():
                self._release_action(action, complete=True)
                action.completed.set()
                continue
            rebuild_required = False
            try:
                if action.execution is not execution:
                    raise ExecutionNotActive("worker action targeted a different execution")
                execution.require_active_input()
                delivery = self._profile.supervisor.control(
                    self._managed.ownership,
                    action.intent,
                    deadline_ms=self._control_deadline_ms(execution_deadline_ms),
                )
                if not delivery.delivered:
                    raise ProcessControlError("semantic control was not delivered")
                action.delivered = True
                rebuild_required = delivery.shell_rebuild_required
            except Exception as error:
                action.error = error
            finally:
                self._release_action(action, complete=True)
                action.completed.set()
            if rebuild_required:
                for write in staged_writes:
                    self._reject_action(
                        write,
                        ExecutionNotActive(f"execution {execution.exec_id} is no longer active"),
                    )
                raise _ShellRebuildRequired
            if self._clock.monotonic_ms() >= execution_deadline_ms:
                for write in staged_writes:
                    self._queue.put(write)
                raise _DeadlineExpired

    def _recover_timeout(self, execution: Execution, correlation_id: str) -> None:
        if self._close_requested.is_set():
            self._drain_to_close(execution)
        if not execution.begin_finalizing():
            return
        try:
            self._raise_if_stopping()
            delivery = self._profile.supervisor.control(
                self._managed.ownership,
                ControlIntent.INTERRUPT,
                deadline_ms=self._control_budget_ms,
            )
            if not delivery.delivered:
                self._rebuild_after_disruption(execution, ExecutionState.TIMEOUT)
                return
            deadline_ms = self._clock.monotonic_ms() + self._config.recovery_deadline_ms
            self._write_all(
                self._protocol.recovery_input(),
                deadline_ms=deadline_ms,
                active_execution=execution,
            )
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
        except _StopRequested:
            raise
        except Exception:
            pass
        self._rebuild_after_disruption(execution, ExecutionState.TIMEOUT)

    def _recover_forced_kill(self, execution: Execution) -> None:
        if self._close_requested.is_set():
            self._drain_to_close(execution)
        if not execution.begin_finalizing():
            return
        self._reject_inactive_actions(execution)
        self._rebuild_after_disruption(execution, ExecutionState.CANCELLED)

    def _rebuild_after_disruption(
        self,
        execution: Execution,
        terminal_state: ExecutionState,
    ) -> None:
        if terminal_state not in {ExecutionState.TIMEOUT, ExecutionState.CANCELLED}:
            raise ValueError("rebuild requires timeout or cancelled terminal state")
        self._raise_if_stopping()
        self._shell.begin_rebuilding(execution.exec_id)
        deadline = time.monotonic() + self._config.rebuild_deadline_ms / 1000
        previous = self._managed
        try:
            self._check_rebuild_close(execution)
            self._cleanup_for_rebuild(previous, deadline, execution)
            self._check_rebuild_close(execution)
            self._raise_if_stopping()
            remaining_ms = self._remaining_ms(deadline)
            if remaining_ms <= 0:
                raise RuntimeBoundaryError("Shell rebuild expired during old runtime cleanup")
            plan = self._profile.dialect.prepare_session(
                self._start_request,
                deadline_ms=remaining_ms,
                cancel_signal=self._close_requested,
            )
            self._check_rebuild_close(execution)
            self._raise_if_stopping()
            remaining_ms = self._remaining_ms(deadline)
            if remaining_ms <= 0:
                raise RuntimeBoundaryError("Shell rebuild expired during dialect preparation")
            managed = self._profile.open_session(
                plan.launch.spawn,
                cleanup_deadline_ms=min(
                    self._config.cleanup_deadline_ms,
                    remaining_ms,
                ),
                startup_deadline_ms=remaining_ms,
                cancel_signal=self._close_requested,
            )
            self._managed = managed
            self._protocol = plan.protocol
            self._check_rebuild_close(execution)
            self._raise_if_stopping()
            remaining_ms = self._remaining_ms(deadline)
            if remaining_ms <= 0:
                raise RuntimeBoundaryError("Shell rebuild expired before startup handshake")
            cwd = self._await_ready(
                plan,
                self._clock.monotonic_ms() + remaining_ms,
                wall_deadline=deadline,
                active_execution=execution,
            )
        except _StopRequested:
            if self._managed is not previous:
                with suppress(RuntimeBoundaryError):
                    self._cleanup_managed(
                        self._managed,
                        absolute_deadline=self._bounded_close_deadline(deadline),
                    )
            raise
        except Exception:
            _LOGGER.exception(
                "Shell rebuild failed after %s",
                terminal_state.value,
            )
            if self._managed is not previous:
                with suppress(RuntimeBoundaryError):
                    self._cleanup_managed(
                        self._managed,
                        absolute_deadline=self._bounded_close_deadline(deadline),
                    )
            if self._close_requested.is_set():
                self._drain_to_close(execution)
            self._finish_rebuild_error(execution, terminal_state)
            return
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            terminal_state,
            next_shell_state=ShellState.READY,
            cwd=cwd,
            shell_rebuilt=True,
        )

    def _finish_rebuild_error(
        self,
        execution: Execution,
        terminal_state: ExecutionState,
    ) -> None:
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            terminal_state,
            next_shell_state=ShellState.ERROR,
        )

    def _finish_shell_error(self, execution: Execution) -> None:
        self._registry.finish_execution(
            self._shell.shell_id,
            execution.exec_id,
            ExecutionState.SHELL_ERROR,
            next_shell_state=ShellState.ERROR,
        )

    def _await_ready(
        self,
        plan: DialectSessionPlan,
        deadline_ms: int,
        *,
        wall_deadline: float | None = None,
        active_execution: Execution | None = None,
    ) -> str:
        bootstrap_sent = False
        while self._clock.monotonic_ms() < deadline_ms and (
            wall_deadline is None or time.monotonic() < wall_deadline
        ):
            if self._close_requested.is_set() and active_execution is not None:
                self._drain_to_close(active_execution)
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
                    return event.cwd
            if eof:
                break
        phase = "startup-record" if bootstrap_sent else "initial-prompt"
        raise StartupHandshakeError(phase)

    def _read_events(
        self,
        deadline_ms: int,
    ) -> tuple[tuple[DialectEvent, ...], bool, bytes]:
        self._raise_if_stopping()
        remaining_ms = max(0, deadline_ms - self._clock.monotonic_ms())
        timeout_ms = min(self._responsive_slice_ms, remaining_ms)
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

    def _write_all(
        self,
        payload: bytes,
        *,
        deadline_ms: int,
        active_execution: Execution | None = None,
    ) -> None:
        cursor = 0
        view = memoryview(payload)
        attempted_write = False
        while cursor < len(payload):
            if active_execution is not None:
                if self._close_requested.is_set():
                    self._drain_to_close(active_execution)
                elif attempted_write:
                    self._service_frame_controls(
                        active_execution,
                        execution_deadline_ms=deadline_ms,
                    )
            self._raise_if_stopping()
            if self._clock.monotonic_ms() >= deadline_ms:
                raise _DeadlineExpired
            result = self._profile.transport.write(self._managed.session, view[cursor:])
            attempted_write = True
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
                    min(self._responsive_slice_ms, remaining_ms),
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
        wall_deadline = time.monotonic() + self._config.job_cleanup_deadline_ms / 1000
        while True:
            if self._close_requested.is_set():
                self._drain_to_close(execution)
            remaining_ms = min(
                max(0, deadline_ms - self._clock.monotonic_ms()),
                self._remaining_ms(wall_deadline),
            )
            if remaining_ms <= 0:
                raise CleanupTimeout("execution descendant cleanup deadline expired")
            cleanup = self._profile.supervisor.cleanup_execution(
                self._managed.ownership,
                deadline_ms=min(remaining_ms, self._control_budget_ms),
            )
            if cleanup.reaped:
                break
            retry_ms = min(remaining_ms, self._responsive_slice_ms)
            if self._close_requested.wait(retry_ms / 1000):
                self._drain_to_close(execution)

        finalization = self._protocol.begin_finalization()
        self._write_all(
            finalization.input_bytes,
            deadline_ms=deadline_ms,
            active_execution=execution,
        )
        acknowledged = False
        while self._clock.monotonic_ms() < deadline_ms and not acknowledged:
            if self._close_requested.is_set():
                self._drain_to_close(execution)
            events, eof, _ = self._read_events(deadline_ms)
            if eof:
                raise RuntimeBoundaryError("PTY reached EOF during finalization probe")
            for event in events:
                if event.kind is DialectEventKind.OUTPUT:
                    execution.append_output(event.data)
                elif event.kind is DialectEventKind.FINALIZED:
                    if event.correlation_id != finalization.correlation_id:
                        raise RuntimeBoundaryError("dialect finalized a different execution")
                    acknowledged = True
                else:
                    raise RuntimeBoundaryError("unexpected control event during finalization probe")
        if not acknowledged:
            raise CleanupTimeout("execution finalization probe deadline expired")

        quiet_until_ms = min(
            deadline_ms,
            self._clock.monotonic_ms() + self._config.output_quiet_ms,
        )
        while self._clock.monotonic_ms() < quiet_until_ms:
            if self._close_requested.is_set():
                self._drain_to_close(execution)
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

    def _attempt_cleanup(
        self,
        *,
        suppress: bool = False,
        absolute_deadline: float | None = None,
    ) -> None:
        try:
            self._cleanup_runtime(absolute_deadline=absolute_deadline)
        except RuntimeBoundaryError as error:
            self._cleanup_pending = True
            self._stop_error = error
            if not suppress:
                raise
        else:
            self._cleanup_pending = False
            self._stop_error = None

    def _cleanup_runtime(self, *, absolute_deadline: float | None = None) -> None:
        if absolute_deadline is None:
            absolute_deadline = time.monotonic() + self._config.cleanup_deadline_ms / 1000
        self._cleanup_managed(
            self._managed,
            absolute_deadline=absolute_deadline,
        )

    def _cleanup_managed(
        self,
        managed: ManagedRuntimeSession,
        *,
        absolute_deadline: float,
    ) -> None:
        cleanup_deadline_ms = self._remaining_ms(absolute_deadline)
        cleanup_error: RuntimeBoundaryError | None = None
        close_error: RuntimeBoundaryError | None = None
        try:
            result = self._profile.supervisor.cleanup(
                managed.ownership,
                deadline_ms=cleanup_deadline_ms,
            )
            if not result.reaped:
                cleanup_error = CleanupTimeout("shell cleanup deadline expired")
        except RuntimeBoundaryError as error:
            cleanup_error = error
        except Exception as error:
            cleanup_error = ProcessControlError("unexpected shell cleanup failure")
            cleanup_error.__cause__ = error
        try:
            close_deadline_ms = self._remaining_ms(absolute_deadline)
            self._profile.transport.close(
                managed.session,
                deadline_ms=close_deadline_ms,
            )
            if self._remaining_ms(absolute_deadline) <= 0:
                raise CleanupTimeout("PTY close exceeded the shell cleanup deadline")
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

    @staticmethod
    def _remaining_ms(deadline: float) -> int:
        return max(0, int((deadline - time.monotonic()) * 1000))

    @property
    def _responsive_slice_ms(self) -> int:
        return min(
            self._config.io_wait_slice_ms,
            max(1, self._config.cleanup_deadline_ms // 4),
        )

    def _control_deadline_ms(self, execution_deadline_ms: int) -> int:
        return min(
            self._control_budget_ms,
            max(0, execution_deadline_ms - self._clock.monotonic_ms()),
        )

    @property
    def _control_budget_ms(self) -> int:
        return min(
            self._config.operation_deadline_ms,
            max(1, self._config.cleanup_deadline_ms // 3),
        )

    @property
    def _close_cleanup_reserve_ms(self) -> int:
        return max(1, self._config.cleanup_deadline_ms // 2)

    @property
    def _close_delivery_budget_ms(self) -> int:
        if self._close_deadline is None:
            return self._control_budget_ms
        cleanup_deadline = self._close_deadline - self._close_cleanup_reserve_ms / 1000
        return self._remaining_ms(cleanup_deadline)

    def _cleanup_for_rebuild(
        self,
        managed: ManagedRuntimeSession,
        rebuild_deadline: float,
        execution: Execution,
    ) -> None:
        """Retry bounded cleanup slices without shortening the public rebuild budget."""

        while True:
            self._check_rebuild_close(execution)
            phase_deadline = min(
                rebuild_deadline,
                time.monotonic() + self._control_budget_ms / 1000,
            )
            try:
                self._cleanup_managed(managed, absolute_deadline=phase_deadline)
                return
            except CleanupTimeout:
                self._check_rebuild_close(execution)
                remaining_ms = self._remaining_ms(rebuild_deadline)
                if remaining_ms <= 0:
                    raise
                if self._close_requested.wait(min(remaining_ms, self._responsive_slice_ms) / 1000):
                    self._drain_to_close(execution)

    def _bounded_close_deadline(self, fallback_deadline: float) -> float:
        if self._close_deadline is None:
            return fallback_deadline
        return min(fallback_deadline, self._close_deadline)

    def _check_rebuild_close(self, execution: Execution) -> None:
        if self._close_requested.is_set():
            self._drain_to_close(execution)
