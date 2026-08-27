"""Thread-safe in-process registry for Shell and Execution identities."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from threading import RLock
from uuid import uuid4

from tfbash_mcp.domain.errors import (
    CapacityExceeded,
    ExecutionNotFound,
    InvalidTransition,
    ShellClosing,
    ShellNotFound,
)
from tfbash_mcp.domain.models import (
    ChangeSignal,
    Clock,
    CommandShell,
    Execution,
    ExecutionState,
    ShellOverviewSnapshot,
    ShellSnapshot,
    ShellState,
    SystemClock,
)

IdFactory = Callable[[str, int], str]


class ShellRegistry:
    """Own resource identities, capacity, addressing, and completed retention."""

    def __init__(
        self,
        *,
        max_shells: int,
        max_retained_executions: int,
        completed_retention_ms: int,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        if max_shells < 1:
            raise ValueError("max_shells must be positive")
        if max_retained_executions < 0:
            raise ValueError("max_retained_executions must be non-negative")
        if completed_retention_ms < 0:
            raise ValueError("completed_retention_ms must be non-negative")
        self._max_shells = max_shells
        self._max_retained_executions = max_retained_executions
        self._completed_retention_ms = completed_retention_ms
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or _random_identifier
        self._shells: dict[str, CommandShell] = {}
        self._completed: OrderedDict[str, Execution] = OrderedDict()
        self._closing_executions: dict[str, Execution] = {}
        self._next_shell_sequence = 1
        self._next_exec_sequence = 1
        self._overview_change_signal = ChangeSignal()
        self._lock = RLock()

    def create_shell(self, *, cwd: str) -> CommandShell:
        with self._lock:
            if len(self._shells) >= self._max_shells:
                raise CapacityExceeded("maximum command shell count reached")
            shell_id = self._new_identifier("shell")
            shell = CommandShell(
                shell_id=shell_id,
                cwd=cwd,
                clock=self._clock,
                overview_change_signal=self._overview_change_signal,
            )
            self._shells[shell_id] = shell
            self._overview_change_signal.notify()
            return shell

    def get_shell(self, shell_id: str) -> CommandShell:
        with self._lock:
            try:
                return self._shells[shell_id]
            except KeyError as exc:
                raise ShellNotFound(f"shell {shell_id} was not found") from exc

    def start_execution(
        self,
        shell_id: str,
        *,
        max_output_bytes: int,
        change_signal: ChangeSignal | None = None,
    ) -> Execution:
        with self._lock:
            shell = self.get_shell(shell_id)
            exec_id = self._new_identifier("exec")
            execution = Execution(
                shell_id=shell_id,
                exec_id=exec_id,
                max_output_bytes=max_output_bytes,
                clock=self._clock,
                change_signal=change_signal,
                overview_change_signal=self._overview_change_signal,
            )
            shell.start_execution(execution)
            return execution

    def get_execution(self, shell_id: str, exec_id: str) -> Execution:
        with self._lock:
            shell = self.get_shell(shell_id)
            if shell.state is ShellState.CLOSING:
                raise ShellClosing(f"shell {shell_id} is closing")
            active = shell.active_execution
            if active is not None and active.exec_id == exec_id:
                return active
            self._prune_completed()
            execution = self._completed.get(exec_id)
            if execution is None or execution.shell_id != shell_id:
                raise ExecutionNotFound(f"execution {exec_id} was not found for shell {shell_id}")
            return execution

    def finish_execution(
        self,
        shell_id: str,
        exec_id: str,
        state: ExecutionState,
        *,
        next_shell_state: ShellState,
        exit_code: int | None = None,
        cwd: str | None = None,
        shell_rebuilt: bool = False,
    ) -> bool:
        with self._lock:
            shell = self.get_shell(shell_id)
            execution = shell.active_execution
            if execution is None or execution.exec_id != exec_id:
                completed = self._completed.get(exec_id)
                if completed is not None and completed.shell_id == shell_id:
                    return False
                closing = self._closing_executions.get(exec_id)
                if closing is not None and closing.shell_id == shell_id:
                    return False
                raise ExecutionNotFound(f"execution {exec_id} was not found for shell {shell_id}")
            won = shell.finish_execution(
                exec_id,
                state,
                next_shell_state=next_shell_state,
                exit_code=exit_code,
                cwd=cwd,
                shell_rebuilt=shell_rebuilt,
            )
            if won:
                self._completed[exec_id] = execution
                self._prune_completed()
            return won

    def begin_close(self, shell_id: str) -> Execution | None:
        with self._lock:
            return self.get_shell(shell_id).begin_close()

    def cancel_active_for_close(self, shell_id: str) -> bool:
        with self._lock:
            shell = self.get_shell(shell_id)
            execution = shell.active_execution
            won = shell.cancel_active()
            if won and execution is not None:
                self._closing_executions[execution.exec_id] = execution
            return won

    def remove_closed(self, shell_id: str) -> None:
        with self._lock:
            shell = self.get_shell(shell_id)
            if shell.state is not ShellState.CLOSING:
                raise InvalidTransition("shell must cross the close fence before removal")
            if shell.active_execution is not None:
                raise InvalidTransition("active execution must be sealed before shell removal")
            del self._shells[shell_id]
            stale_ids = [
                exec_id
                for exec_id, execution in self._completed.items()
                if execution.shell_id == shell_id
            ]
            for exec_id in stale_ids:
                del self._completed[exec_id]
            closing_ids = [
                exec_id
                for exec_id, execution in self._closing_executions.items()
                if execution.shell_id == shell_id
            ]
            for exec_id in closing_ids:
                del self._closing_executions[exec_id]
            self._overview_change_signal.notify()

    def snapshots(self) -> tuple[ShellSnapshot, ...]:
        with self._lock:
            return tuple(shell.snapshot() for shell in self._shells.values())

    def overview_snapshots(
        self,
        *,
        max_output_characters: int,
    ) -> tuple[ShellOverviewSnapshot, ...]:
        if max_output_characters < 1:
            raise ValueError("max_output_characters must be positive")
        with self._lock:
            self._prune_completed()
            latest_completed: dict[str, Execution] = {}
            for execution in self._completed.values():
                latest_completed[execution.shell_id] = execution
            result: list[ShellOverviewSnapshot] = []
            for shell in self._shells.values():
                selected_execution = shell.active_execution or latest_completed.get(shell.shell_id)
                result.append(
                    ShellOverviewSnapshot(
                        shell=shell.snapshot(),
                        execution=(
                            selected_execution.overview_snapshot(
                                max_output_characters=max_output_characters
                            )
                            if selected_execution is not None
                            else None
                        ),
                    )
                )
            return tuple(result)

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self._overview_change_signal.subscribe(listener)

    def prune_completed(self) -> tuple[str, ...]:
        with self._lock:
            return self._prune_completed()

    def _prune_completed(self) -> tuple[str, ...]:
        now = self._clock.monotonic_ms()
        removed: list[str] = []
        for exec_id, execution in tuple(self._completed.items()):
            completed_at = execution.completed_at_ms
            if completed_at is None:
                raise InvalidTransition("retained execution is not terminal")
            if now - completed_at < self._completed_retention_ms:
                continue
            del self._completed[exec_id]
            removed.append(exec_id)
        while len(self._completed) > self._max_retained_executions:
            exec_id, _ = self._completed.popitem(last=False)
            removed.append(exec_id)
        if removed:
            self._overview_change_signal.notify()
        return tuple(removed)

    def _new_identifier(self, prefix: str) -> str:
        if prefix == "shell":
            sequence = self._next_shell_sequence
            self._next_shell_sequence += 1
        else:
            sequence = self._next_exec_sequence
            self._next_exec_sequence += 1
        candidate = self._id_factory(prefix, sequence)
        _validate_generated_identifier(candidate, prefix)
        if candidate in self._shells or candidate in self._known_execution_ids():
            raise InvalidTransition(f"{prefix} ID factory returned a duplicate identifier")
        return candidate

    def _known_execution_ids(self) -> set[str]:
        active_ids = {
            execution.exec_id
            for shell in self._shells.values()
            if (execution := shell.active_execution) is not None
        }
        return active_ids | set(self._completed) | set(self._closing_executions)


def _random_identifier(prefix: str, sequence: int) -> str:
    return f"{prefix}_{sequence:x}_{uuid4().hex}"


def _validate_generated_identifier(value: str, prefix: str) -> None:
    try:
        size = len(value.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise InvalidTransition(f"{prefix} ID factory returned invalid UTF-8") from exc
    if not 1 <= size <= 128:
        raise InvalidTransition(f"{prefix} ID factory returned an invalid identifier")
