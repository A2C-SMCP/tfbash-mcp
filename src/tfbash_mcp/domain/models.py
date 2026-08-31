"""Shell and Execution state machines with no runtime implementation imports."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from threading import Condition, RLock
from typing import Protocol

from tfbash_mcp.domain.errors import (
    ExecutionNotActive,
    InvalidTransition,
    ShellBusy,
    ShellClosing,
    ShellUnavailable,
)
from tfbash_mcp.domain.output import Utf8OutputBuffer

_LOGGER = logging.getLogger(__name__)


class Clock(Protocol):
    def monotonic_ms(self) -> int: ...

    def wall_time_ms(self) -> int: ...


class SystemClock:
    def monotonic_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def wall_time_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ChangeSignal:
    """A domain-owned condition signal with a monotonic change generation."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._generation = 0
        self._listeners: set[Callable[[], None]] = set()

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def notify(self) -> None:
        with self._condition:
            self._generation += 1
            self._condition.notify_all()
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                # Observers are advisory; a notification bridge failure must not
                # prevent the state transition that produced the notification.
                _LOGGER.exception("domain change listener failed")

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an advisory listener and return its idempotent unsubscribe."""

        with self._condition:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._condition:
                self._listeners.discard(listener)

        return unsubscribe

    def wait_for_change(self, generation: int, timeout_seconds: float | None = None) -> int:
        with self._condition:
            self._condition.wait_for(
                lambda: self._generation > generation,
                timeout=timeout_seconds,
            )
            return self._generation


class ShellState(str, Enum):
    READY = "ready"
    BUSY = "busy"
    REBUILDING = "rebuilding"
    CLOSING = "closing"
    ERROR = "error"


class ExecutionState(str, Enum):
    RUNNING = "running"
    FINALIZING = "finalizing"
    EXITED = "exited"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SHELL_ERROR = "shell_error"

    @property
    def terminal(self) -> bool:
        return self in {
            ExecutionState.EXITED,
            ExecutionState.TIMEOUT,
            ExecutionState.CANCELLED,
            ExecutionState.SHELL_ERROR,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    shell_id: str
    exec_id: str
    status: ExecutionState
    exit_code: int | None
    output: str
    buffer_start_cursor: int
    next_cursor: int
    truncated_before_cursor: bool
    eof: bool
    duration_ms: int | None = None
    cwd: str | None = None
    shell_status: ShellState | None = None
    shell_rebuilt: bool | None = None


@dataclass(frozen=True, slots=True)
class ShellSnapshot:
    shell_id: str
    status: ShellState
    last_known_cwd: str | None
    active_exec_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ExecutionOverviewSnapshot:
    exec_id: str
    status: ExecutionState
    exit_code: int | None
    duration_ms: int | None
    output: str
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class ShellOverviewSnapshot:
    shell: ShellSnapshot
    execution: ExecutionOverviewSnapshot | None


class Execution:
    def __init__(
        self,
        *,
        shell_id: str,
        exec_id: str,
        max_output_bytes: int,
        clock: Clock,
        change_signal: ChangeSignal | None = None,
        overview_change_signal: ChangeSignal | None = None,
    ) -> None:
        _validate_identifier(shell_id)
        _validate_identifier(exec_id)
        self.shell_id = shell_id
        self.exec_id = exec_id
        self._clock = clock
        self._change_signal = change_signal or ChangeSignal()
        self._overview_change_signal = overview_change_signal
        self._created_ms = clock.monotonic_ms()
        self._completed_ms: int | None = None
        self._state = ExecutionState.RUNNING
        self._output = Utf8OutputBuffer(max_output_bytes)
        self._exit_code: int | None = None
        self._duration_ms: int | None = None
        self._cwd: str | None = None
        self._shell_status: ShellState | None = None
        self._shell_rebuilt: bool | None = None
        self._lock = RLock()

    @property
    def state(self) -> ExecutionState:
        with self._lock:
            return self._state

    @property
    def generation(self) -> int:
        return self._change_signal.generation

    @property
    def change_signal(self) -> ChangeSignal:
        return self._change_signal

    @property
    def completed_at_ms(self) -> int | None:
        with self._lock:
            return self._completed_ms

    @property
    def input_active(self) -> bool:
        with self._lock:
            return self._state is ExecutionState.RUNNING

    def append_output(self, raw: bytes) -> int:
        with self._lock:
            if self._state.terminal:
                raise InvalidTransition("terminal execution output is sealed")
            added = self._output.append(raw)
            if added:
                self._changed()
            return added

    def begin_finalizing(self) -> bool:
        with self._lock:
            if self._state is not ExecutionState.RUNNING:
                return False
            self._state = ExecutionState.FINALIZING
            self._changed()
            return True

    def require_active_input(self) -> None:
        if not self.input_active:
            raise ExecutionNotActive(f"execution {self.exec_id} is not active")

    def complete(
        self,
        state: ExecutionState,
        *,
        shell_status: ShellState,
        exit_code: int | None = None,
        cwd: str | None = None,
        shell_rebuilt: bool = False,
    ) -> bool:
        _validate_terminal_result(state, shell_status, exit_code, shell_rebuilt)
        with self._lock:
            if self._state.terminal:
                return False
            self._output.seal()
            completed_ms = self._clock.monotonic_ms()
            self._state = state
            self._completed_ms = completed_ms
            self._duration_ms = max(0, completed_ms - self._created_ms)
            self._exit_code = exit_code
            self._cwd = cwd
            self._shell_status = shell_status
            self._shell_rebuilt = shell_rebuilt
            self._changed()
            return True

    def snapshot(self, *, cursor: int, max_bytes: int) -> ExecutionSnapshot:
        with self._lock:
            window = self._output.read(cursor, max_bytes)
            public_state = (
                ExecutionState.RUNNING if self._state is ExecutionState.FINALIZING else self._state
            )
            return ExecutionSnapshot(
                shell_id=self.shell_id,
                exec_id=self.exec_id,
                status=public_state,
                exit_code=self._exit_code,
                output=window.output,
                buffer_start_cursor=window.buffer_start_cursor,
                next_cursor=window.next_cursor,
                truncated_before_cursor=window.truncated_before_cursor,
                eof=self._state.terminal and window.at_end,
                duration_ms=self._duration_ms,
                cwd=self._cwd,
                shell_status=self._shell_status,
                shell_rebuilt=self._shell_rebuilt,
            )

    def overview_snapshot(self, *, max_output_characters: int) -> ExecutionOverviewSnapshot:
        with self._lock:
            tail = self._output.tail(max_output_characters)
            public_state = (
                ExecutionState.RUNNING if self._state is ExecutionState.FINALIZING else self._state
            )
            return ExecutionOverviewSnapshot(
                exec_id=self.exec_id,
                status=public_state,
                exit_code=self._exit_code,
                duration_ms=self._duration_ms,
                output=tail.output,
                output_truncated=tail.truncated,
            )

    def _changed(self) -> None:
        self._change_signal.notify()
        if self._overview_change_signal is not None:
            self._overview_change_signal.notify()


class CommandShell:
    def __init__(
        self,
        *,
        shell_id: str,
        cwd: str,
        clock: Clock,
        overview_change_signal: ChangeSignal | None = None,
    ) -> None:
        _validate_identifier(shell_id)
        self.shell_id = shell_id
        self._state = ShellState.READY
        self._last_known_cwd: str | None = cwd
        self._active_execution: Execution | None = None
        self._created_at_ms = clock.wall_time_ms()
        self._overview_change_signal = overview_change_signal
        self._lock = RLock()

    @property
    def state(self) -> ShellState:
        with self._lock:
            return self._state

    @property
    def active_execution(self) -> Execution | None:
        with self._lock:
            return self._active_execution

    def confirm_ready(self, *, cwd: str) -> None:
        """Record the runtime-confirmed cwd after the startup handshake."""

        with self._lock:
            if self._state is not ShellState.READY or self._active_execution is not None:
                raise InvalidTransition("only an idle ready shell can confirm startup")
            self._last_known_cwd = cwd
            self._changed()

    def start_execution(self, execution: Execution) -> None:
        with self._lock:
            if self._state is ShellState.CLOSING:
                raise ShellClosing(f"shell {self.shell_id} is closing")
            if self._state in {ShellState.REBUILDING, ShellState.ERROR}:
                raise ShellUnavailable(
                    f"shell {self.shell_id} is unavailable",
                    retryable=self._state is ShellState.REBUILDING,
                )
            if self._state is ShellState.BUSY or self._active_execution is not None:
                raise ShellBusy(f"shell {self.shell_id} is busy")
            if execution.shell_id != self.shell_id:
                raise InvalidTransition("execution belongs to a different shell")
            self._active_execution = execution
            self._state = ShellState.BUSY
            self._changed()

    def begin_rebuilding(self, exec_id: str) -> None:
        with self._lock:
            self._require_active(exec_id)
            if self._state is not ShellState.BUSY:
                raise InvalidTransition("only a busy shell can begin rebuilding")
            self._state = ShellState.REBUILDING
            self._changed()

    def begin_close(self) -> Execution | None:
        with self._lock:
            if self._state is ShellState.CLOSING:
                raise ShellClosing(f"shell {self.shell_id} is already closing")
            self._state = ShellState.CLOSING
            self._changed()
            return self._active_execution

    def mark_error(self) -> None:
        """Mark an idle shell unusable after an out-of-band runtime failure."""

        with self._lock:
            if self._state is ShellState.CLOSING:
                raise ShellClosing(f"shell {self.shell_id} is closing")
            if self._active_execution is not None:
                raise InvalidTransition("an active execution must be sealed as shell_error")
            self._state = ShellState.ERROR
            self._changed()

    def cancel_active(self) -> bool:
        with self._lock:
            if self._state is not ShellState.CLOSING:
                raise InvalidTransition("cancel requires the close admission fence")
            if self._active_execution is None:
                return False
            won = self._active_execution.complete(
                ExecutionState.CANCELLED,
                shell_status=ShellState.CLOSING,
            )
            if won:
                self._active_execution = None
                self._changed()
            return won

    def finish_execution(
        self,
        exec_id: str,
        state: ExecutionState,
        *,
        next_shell_state: ShellState,
        exit_code: int | None = None,
        cwd: str | None = None,
        shell_rebuilt: bool = False,
    ) -> bool:
        if next_shell_state not in {ShellState.READY, ShellState.ERROR}:
            raise InvalidTransition("execution may finish only into ready or error")
        with self._lock:
            execution = self._require_active(exec_id)
            shell_status = (
                ShellState.CLOSING if self._state is ShellState.CLOSING else next_shell_state
            )
            won = execution.complete(
                state,
                shell_status=shell_status,
                exit_code=exit_code,
                cwd=cwd,
                shell_rebuilt=shell_rebuilt,
            )
            if not won:
                return False
            self._active_execution = None
            if self._state is not ShellState.CLOSING:
                self._state = next_shell_state
            if cwd is not None:
                self._last_known_cwd = cwd
            self._changed()
            return True

    def snapshot(self) -> ShellSnapshot:
        with self._lock:
            return ShellSnapshot(
                shell_id=self.shell_id,
                status=self._state,
                last_known_cwd=self._last_known_cwd,
                active_exec_id=(
                    self._active_execution.exec_id
                    if self._active_execution is not None
                    and self._state in {ShellState.BUSY, ShellState.REBUILDING}
                    else None
                ),
                created_at_ms=self._created_at_ms,
            )

    def _require_active(self, exec_id: str) -> Execution:
        if self._active_execution is None or self._active_execution.exec_id != exec_id:
            raise ExecutionNotActive(f"execution {exec_id} is not active in {self.shell_id}")
        return self._active_execution

    def _changed(self) -> None:
        if self._overview_change_signal is not None:
            self._overview_change_signal.notify()


def _validate_identifier(value: str) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("identifier must be valid UTF-8") from exc
    if not 1 <= size <= 128:
        raise ValueError("identifier must contain between 1 and 128 UTF-8 bytes")


def _validate_terminal_result(
    state: ExecutionState,
    shell_status: ShellState,
    exit_code: int | None,
    shell_rebuilt: bool,
) -> None:
    allowed_statuses: dict[ExecutionState, frozenset[ShellState]] = {
        ExecutionState.EXITED: frozenset({ShellState.READY, ShellState.CLOSING}),
        ExecutionState.TIMEOUT: frozenset({ShellState.READY, ShellState.ERROR, ShellState.CLOSING}),
        ExecutionState.CANCELLED: frozenset(
            {ShellState.READY, ShellState.ERROR, ShellState.CLOSING}
        ),
        ExecutionState.SHELL_ERROR: frozenset({ShellState.ERROR, ShellState.CLOSING}),
    }
    if state not in allowed_statuses:
        raise InvalidTransition("completion requires a terminal execution state")
    if shell_status not in allowed_statuses[state]:
        raise InvalidTransition(f"{state.value} cannot produce shell state {shell_status.value}")
    if state is ExecutionState.EXITED:
        if exit_code is None or not 0 <= exit_code <= 4_294_967_295:
            raise InvalidTransition("exited requires a normalized 32-bit exit code")
    elif exit_code is not None:
        raise InvalidTransition(f"{state.value} requires a null exit code")
    if shell_rebuilt and state not in {ExecutionState.TIMEOUT, ExecutionState.CANCELLED}:
        raise InvalidTransition("shell_rebuilt requires timeout or forced-kill recovery")
