"""Concurrent Shell service for open, exec, and cursor reads."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Lock, Thread

from tfbash_mcp.domain.errors import CapacityExceeded, ShellNotFound
from tfbash_mcp.domain.models import (
    Clock,
    CommandShell,
    Execution,
    ExecutionSnapshot,
    ExecutionState,
    ShellOverviewSnapshot,
    ShellSnapshot,
    ShellState,
    SystemClock,
)
from tfbash_mcp.domain.registry import ShellRegistry
from tfbash_mcp.domain.worker import CloseRequest, ShellWorker, WorkerConfig
from tfbash_mcp.runtime.contracts import ControlIntent, ShellStartRequest
from tfbash_mcp.runtime.errors import CleanupTimeout, RuntimeBoundaryError
from tfbash_mcp.runtime.profile import RuntimeProfile


@dataclass(frozen=True, slots=True)
class ManagerConfig:
    max_shells: int = 8
    max_retained_executions: int = 128
    completed_retention_ms: int = 300_000
    max_output_bytes: int = 1_048_576
    max_read_bytes: int = 65_536
    max_write_bytes: int = 65_536
    max_read_waiters_per_execution: int = 8
    reaper_retry_ms: int = 100
    worker: WorkerConfig = field(default_factory=WorkerConfig)

    def __post_init__(self) -> None:
        if self.max_output_bytes < 4096:
            raise ValueError("max_output_bytes must be at least 4096")
        if self.max_read_bytes < 4:
            raise ValueError("max_read_bytes must be at least 4")
        if self.max_write_bytes < 1:
            raise ValueError("max_write_bytes must be positive")
        if self.max_read_waiters_per_execution < 1:
            raise ValueError("max_read_waiters_per_execution must be positive")
        if self.reaper_retry_ms < 1:
            raise ValueError("reaper_retry_ms must be positive")


@dataclass(slots=True)
class _OpenAttempt:
    attempt_id: int
    request: ShellStartRequest
    deadline_ms: int
    cancelled: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    snapshot: ShellSnapshot | None = None
    error: Exception | None = None


@dataclass(slots=True)
class _ShutdownCleanupAttempt:
    shell_id: str
    worker: ShellWorker
    close_action: CloseRequest | None
    done: Event = field(default_factory=Event)
    error: Exception | None = None


class CommandShellManager:
    """Coordinate registry admission while workers run independently per Shell."""

    def __init__(
        self,
        *,
        profile: RuntimeProfile,
        config: ManagerConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._profile = profile
        self._config = config or ManagerConfig()
        self._clock = clock or SystemClock()
        self._registry = ShellRegistry(
            max_shells=self._config.max_shells,
            max_retained_executions=self._config.max_retained_executions,
            completed_retention_ms=self._config.completed_retention_ms,
            clock=self._clock,
        )
        self._workers: dict[str, ShellWorker] = {}
        self._pending_cleanup: dict[str, ShellWorker] = {}
        self._reaping: dict[str, ShellWorker] = {}
        self._read_waiters: dict[str, int] = {}
        self._lock = Lock()
        self._closed = False
        self._open_attempts: dict[int, _OpenAttempt] = {}
        self._next_open_attempt = 1
        self._reaper_wakeup = Event()
        self._reaper_stop = Event()
        self._reaper_thread = Thread(
            target=self._reaper_main,
            name="tfbash-cleanup-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

    def open_shell(self, request: ShellStartRequest) -> ShellSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeError("shell manager is shut down")
            owned_slots = (
                len(self._workers)
                + len(self._pending_cleanup)
                + len(self._reaping)
                + len(self._open_attempts)
            )
            if owned_slots >= self._config.max_shells:
                raise CapacityExceeded("maximum command shell ownership count reached")
            attempt_id = self._next_open_attempt
            self._next_open_attempt += 1
            attempt = _OpenAttempt(
                attempt_id=attempt_id,
                request=request,
                deadline_ms=(self._clock.monotonic_ms() + self._config.worker.startup_deadline_ms),
            )
            self._open_attempts[attempt_id] = attempt
        open_thread = Thread(
            target=self._run_open_attempt,
            args=(attempt,),
            name=f"tfbash-open-{attempt_id}",
            daemon=True,
        )
        try:
            open_thread.start()
        except Exception as error:
            with self._lock:
                self._open_attempts.pop(attempt_id, None)
            boundary_error = RuntimeBoundaryError("failed to start the Shell open attempt")
            raise boundary_error from error
        attempt.done.wait(self._config.worker.startup_deadline_ms / 1000)
        with self._lock:
            if not attempt.done.is_set():
                attempt.cancelled.set()
                raise RuntimeBoundaryError("shell startup deadline expired")
            if attempt.error is not None:
                raise attempt.error
            if attempt.snapshot is None:
                raise RuntimeBoundaryError("shell open attempt completed without a result")
            return attempt.snapshot

    def _run_open_attempt(self, attempt: _OpenAttempt) -> None:
        shell: CommandShell | None = None
        worker: ShellWorker | None = None
        try:
            plan = self._profile.dialect.prepare_session(
                attempt.request,
                deadline_ms=max(0, attempt.deadline_ms - self._clock.monotonic_ms()),
                cancel_signal=attempt.cancelled,
            )
            if attempt.cancelled.is_set() or self._clock.monotonic_ms() >= attempt.deadline_ms:
                raise RuntimeBoundaryError(
                    "shell startup deadline expired during dialect preparation"
                )
            shell = self._registry.create_shell(cwd=attempt.request.cwd)
            worker = ShellWorker(
                shell=shell,
                registry=self._registry,
                profile=self._profile,
                config=self._config.worker,
                clock=self._clock,
                spawn=plan,
                start_request=attempt.request,
                startup_deadline_ms=attempt.deadline_ms,
                startup_cancel_signal=attempt.cancelled,
            )
            if attempt.cancelled.is_set():
                raise RuntimeBoundaryError("shell startup was cancelled during spawn")
            worker.start(plan, startup_deadline_ms=attempt.deadline_ms)
            with self._lock:
                if (
                    self._closed
                    or attempt.cancelled.is_set()
                    or self._clock.monotonic_ms() >= attempt.deadline_ms
                ):
                    raise RuntimeBoundaryError(
                        "shell startup was cancelled or expired before publication"
                    )
                self._workers[shell.shell_id] = worker
                attempt.snapshot = shell.snapshot()
                self._open_attempts.pop(attempt.attempt_id, None)
                attempt.done.set()
                return
        except Exception as error:
            cleanup_error: RuntimeBoundaryError | None = None
            if shell is not None:
                self._registry.begin_close(shell.shell_id)
            if worker is not None:
                try:
                    worker.stop()
                except RuntimeBoundaryError as worker_cleanup_error:
                    cleanup_error = worker_cleanup_error
            if shell is not None:
                self._registry.remove_closed(shell.shell_id)
            with self._lock:
                if cleanup_error is not None and worker is not None and shell is not None:
                    self._pending_cleanup[shell.shell_id] = worker
                    self._reaper_wakeup.set()
                attempt.error = error
                self._open_attempts.pop(attempt.attempt_id, None)
                attempt.done.set()

    def exec(
        self,
        shell_id: str,
        command: str,
        *,
        yield_ms: int,
        timeout_ms: int,
        max_output_bytes: int,
    ) -> ExecutionSnapshot:
        if not command:
            raise ValueError("command cannot be empty")
        if (
            yield_ms < 0
            or timeout_ms <= 0
            or not 4096 <= max_output_bytes <= self._config.max_output_bytes
        ):
            raise ValueError(
                "yield_ms and timeout_ms are invalid, or max_output_bytes is outside limits"
            )
        deadline_ms = self._clock.monotonic_ms() + timeout_ms
        execution: Execution | None = None
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("shell manager is shut down")
                execution = self._registry.start_execution(
                    shell_id,
                    max_output_bytes=max_output_bytes,
                )
                try:
                    worker = self._workers[shell_id]
                except KeyError as error:
                    raise ShellNotFound(f"shell {shell_id} has no worker") from error
                worker.submit(
                    execution,
                    command,
                    deadline_ms=deadline_ms,
                )
        except Exception:
            if execution is not None:
                self._registry.finish_execution(
                    shell_id,
                    execution.exec_id,
                    ExecutionState.SHELL_ERROR,
                    next_shell_state=ShellState.ERROR,
                )
            raise
        assert execution is not None
        self._wait_for_terminal(execution, yield_ms)
        return execution.snapshot(cursor=0, max_bytes=max_output_bytes)

    def read(
        self,
        shell_id: str,
        exec_id: str,
        *,
        cursor: int,
        max_bytes: int,
        wait_ms: int,
    ) -> ExecutionSnapshot:
        if wait_ms < 0 or not 4 <= max_bytes <= self._config.max_read_bytes:
            raise ValueError("wait_ms or max_bytes is outside configured limits")
        with self._lock:
            execution = self._registry.get_execution(shell_id, exec_id)
        snapshot = execution.snapshot(cursor=cursor, max_bytes=max_bytes)
        if wait_ms == 0 or snapshot.eof or self._has_data(snapshot, cursor):
            return snapshot
        self._acquire_waiter(exec_id)
        try:
            deadline = time.monotonic() + wait_ms / 1000
            generation = execution.generation
            while True:
                snapshot = execution.snapshot(cursor=cursor, max_bytes=max_bytes)
                if snapshot.eof or self._has_data(snapshot, cursor):
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
                generation = execution.change_signal.wait_for_change(generation, remaining)
        finally:
            self._release_waiter(exec_id)

    def write(self, shell_id: str, exec_id: str, data: bytes) -> int:
        if len(data) > self._config.max_write_bytes:
            raise ValueError("write exceeds the configured byte limit")
        with self._lock:
            execution = self._registry.get_execution(shell_id, exec_id)
            execution.require_active_input()
            try:
                worker = self._workers[shell_id]
            except KeyError as error:
                raise ShellNotFound(f"shell {shell_id} has no worker") from error
            return worker.accept_write(execution, data)

    def signal(
        self,
        shell_id: str,
        exec_id: str,
        intent: ControlIntent,
    ) -> bool:
        if not isinstance(intent, ControlIntent):
            raise ValueError("intent must be a semantic control value")
        with self._lock:
            execution = self._registry.get_execution(shell_id, exec_id)
            execution.require_active_input()
            try:
                worker = self._workers[shell_id]
            except KeyError as error:
                raise ShellNotFound(f"shell {shell_id} has no worker") from error
            action = worker.reserve_signal(execution, intent)
        return worker.await_signal(action)

    def close_shell(self, shell_id: str) -> bool:
        with self._lock:
            try:
                worker = self._workers[shell_id]
            except KeyError as error:
                raise ShellNotFound(f"shell {shell_id} has no worker") from error
            self._registry.begin_close(shell_id)
            close_action = worker.reserve_close(deadline_ms=self._config.worker.cleanup_deadline_ms)
            del self._workers[shell_id]
            self._reaping[shell_id] = worker
        cleanup_complete = True
        try:
            worker.finish_close(close_action)
        except Exception:
            cleanup_complete = False
        finally:
            with self._lock:
                self._reaping.pop(shell_id, None)
                if not cleanup_complete:
                    self._pending_cleanup[shell_id] = worker
                    self._reaper_wakeup.set()
            self._registry.remove_closed(shell_id)
        return cleanup_complete

    def snapshots(self) -> tuple[ShellSnapshot, ...]:
        return self._registry.snapshots()

    def overview_snapshots(
        self,
        *,
        max_output_characters: int,
    ) -> tuple[ShellOverviewSnapshot, ...]:
        return self._registry.overview_snapshots(
            max_output_characters=max_output_characters,
        )

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self._registry.subscribe_overview_changes(listener)

    def shutdown(self) -> None:
        shutdown_deadline = time.monotonic() + self._config.worker.cleanup_deadline_ms / 1000
        with self._lock:
            if (
                self._closed
                and not self._workers
                and not self._pending_cleanup
                and not self._reaping
                and not self._open_attempts
                and not self._profile.has_pending_startup_cleanup
            ):
                return
            self._closed = True
            attempts = tuple(self._open_attempts.values())
            for attempt in attempts:
                attempt.cancelled.set()
            close_actions: dict[str, CloseRequest] = {}
            for shell_id, worker in self._workers.items():
                self._registry.begin_close(shell_id)
                close_actions[shell_id] = worker.reserve_close(
                    deadline_ms=max(
                        0,
                        int((shutdown_deadline - time.monotonic()) * 1000),
                    )
                )
            background_workers = {
                id(worker): worker
                for worker in (
                    *self._pending_cleanup.values(),
                    *self._reaping.values(),
                )
            }
            for worker in background_workers.values():
                worker.request_stop()
            self._reaper_stop.set()
            self._reaper_wakeup.set()
        unfinished_attempts = []
        for attempt in attempts:
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            if not attempt.done.wait(remaining):
                unfinished_attempts.append(attempt)
        self._reaper_thread.join(max(0.0, shutdown_deadline - time.monotonic()))
        reaper_unfinished = self._reaper_thread.is_alive()
        with self._lock:
            workers = tuple(self._workers.items())
            self._workers.clear()
            pending = tuple(self._pending_cleanup.items())
            self._pending_cleanup.clear()
        first_error: Exception | None = (
            CleanupTimeout("open attempt did not stop before shutdown deadline")
            if unfinished_attempts
            else None
        )
        if reaper_unfinished and first_error is None:
            first_error = CleanupTimeout("cleanup reaper did not stop before shutdown deadline")
        active_shell_ids = {shell_id for shell_id, _ in workers}
        cleanup_attempts = tuple(
            _ShutdownCleanupAttempt(
                shell_id,
                worker,
                close_actions.get(shell_id) if shell_id in active_shell_ids else None,
            )
            for shell_id, worker in (*workers, *pending)
        )
        with self._lock:
            for cleanup_attempt in cleanup_attempts:
                self._reaping[cleanup_attempt.shell_id] = cleanup_attempt.worker
        for cleanup_attempt in cleanup_attempts:
            cleanup_thread = Thread(
                target=self._run_shutdown_cleanup,
                args=(cleanup_attempt, shutdown_deadline),
                name=f"tfbash-shutdown-{cleanup_attempt.shell_id}",
                daemon=True,
            )
            try:
                cleanup_thread.start()
            except Exception as error:
                cleanup_attempt.error = RuntimeBoundaryError(
                    f"failed to start cleanup for Shell {cleanup_attempt.shell_id}"
                )
                cleanup_attempt.error.__cause__ = error
                if cleanup_attempt.close_action is not None:
                    self._registry.remove_closed(cleanup_attempt.shell_id)
                with self._lock:
                    if self._reaping.get(cleanup_attempt.shell_id) is cleanup_attempt.worker:
                        self._reaping.pop(cleanup_attempt.shell_id, None)
                    self._pending_cleanup[cleanup_attempt.shell_id] = cleanup_attempt.worker
                cleanup_attempt.done.set()
        for cleanup_attempt in cleanup_attempts:
            remaining = max(0.0, shutdown_deadline - time.monotonic())
            if not cleanup_attempt.done.wait(remaining):
                if first_error is None:
                    first_error = CleanupTimeout(
                        f"Shell {cleanup_attempt.shell_id} cleanup exceeded shutdown deadline"
                    )
            elif cleanup_attempt.error is not None and first_error is None:
                first_error = cleanup_attempt.error
        startup_cleanup_complete = self._profile.cleanup_pending_startups(
            deadline_ms=max(
                0,
                int((shutdown_deadline - time.monotonic()) * 1000),
            )
        )
        if not startup_cleanup_complete and first_error is None:
            first_error = CleanupTimeout(
                "runtime startup rollback did not finish before shutdown deadline"
            )
        while time.monotonic() < shutdown_deadline:
            with self._lock:
                if not self._reaping:
                    break
            time.sleep(0.001)
        with self._lock:
            concurrent_cleanup_unfinished = bool(self._reaping or self._pending_cleanup)
        if concurrent_cleanup_unfinished and first_error is None:
            first_error = CleanupTimeout(
                "concurrent Shell close cleanup did not finish before shutdown deadline"
            )
        if first_error is not None:
            raise first_error

    def _run_shutdown_cleanup(
        self,
        attempt: _ShutdownCleanupAttempt,
        shutdown_deadline: float,
    ) -> None:
        try:
            if attempt.close_action is not None:
                attempt.worker.finish_close(attempt.close_action)
            else:
                attempt.worker.stop(
                    deadline_ms=max(
                        0,
                        int((shutdown_deadline - time.monotonic()) * 1000),
                    )
                )
        except Exception as error:
            attempt.error = error
        finally:
            if attempt.close_action is not None:
                self._registry.remove_closed(attempt.shell_id)
            with self._lock:
                if self._reaping.get(attempt.shell_id) is attempt.worker:
                    self._reaping.pop(attempt.shell_id, None)
                if attempt.error is not None:
                    self._pending_cleanup[attempt.shell_id] = attempt.worker
            attempt.done.set()

    def _reaper_main(self) -> None:
        while not self._reaper_stop.is_set():
            self._reaper_wakeup.wait()
            self._reaper_wakeup.clear()
            while not self._reaper_stop.is_set():
                with self._lock:
                    if not self._pending_cleanup:
                        break
                    shell_id, worker = next(iter(self._pending_cleanup.items()))
                    del self._pending_cleanup[shell_id]
                    self._reaping[shell_id] = worker
                cleanup_complete = True
                try:
                    worker.stop(deadline_ms=self._config.reaper_retry_ms)
                except Exception:
                    cleanup_complete = False
                with self._lock:
                    self._reaping.pop(shell_id, None)
                    if not cleanup_complete:
                        self._pending_cleanup[shell_id] = worker
                if not cleanup_complete:
                    if self._reaper_stop.wait(self._config.reaper_retry_ms / 1000):
                        return
                    self._reaper_wakeup.set()
                    break

    def _worker(self, shell_id: str) -> ShellWorker:
        with self._lock:
            try:
                return self._workers[shell_id]
            except KeyError as error:
                raise ShellNotFound(f"shell {shell_id} has no worker") from error

    @staticmethod
    def _wait_for_terminal(execution: Execution, yield_ms: int) -> None:
        deadline = time.monotonic() + yield_ms / 1000
        generation = execution.generation
        while not execution.state.terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            generation = execution.change_signal.wait_for_change(generation, remaining)

    @staticmethod
    def _has_data(snapshot: ExecutionSnapshot, requested_cursor: int) -> bool:
        return snapshot.truncated_before_cursor or snapshot.next_cursor > requested_cursor

    def _acquire_waiter(self, exec_id: str) -> None:
        with self._lock:
            current = self._read_waiters.get(exec_id, 0)
            if current >= self._config.max_read_waiters_per_execution:
                raise CapacityExceeded("maximum read waiters reached for execution")
            self._read_waiters[exec_id] = current + 1

    def _release_waiter(self, exec_id: str) -> None:
        with self._lock:
            remaining = self._read_waiters[exec_id] - 1
            if remaining:
                self._read_waiters[exec_id] = remaining
            else:
                del self._read_waiters[exec_id]
