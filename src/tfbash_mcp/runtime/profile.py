"""Validated all-or-nothing Runtime Profile composition."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from itertools import count
from threading import Event, Lock, Thread

from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
    ProcessOwnership,
    ProcessSupervisor,
    PtyTransport,
    RuntimeName,
    RuntimeSession,
    ShellDialect,
    SpawnRequest,
)
from tfbash_mcp.runtime.errors import (
    CleanupTimeout,
    ProcessControlError,
    RuntimeBoundaryError,
    RuntimeConfigurationError,
    TransportError,
)


class RuntimePlatform(str, Enum):
    POSIX = "posix"
    WINDOWS = "windows"


_EXPECTED_IDENTITY = {
    RuntimeName.POSIX_BASH: RuntimePlatform.POSIX,
    RuntimeName.WINDOWS_PWSH: RuntimePlatform.WINDOWS,
}


@dataclass(frozen=True, slots=True)
class ManagedRuntimeSession:
    session: RuntimeSession
    ownership: ProcessOwnership


@dataclass(slots=True)
class _SpawnAttempt:
    attempt_id: int
    ownership: ProcessOwnership
    cleanup_deadline_ms: int
    lock: Lock = field(default_factory=Lock)
    completed: Event = field(default_factory=Event)
    wakeup: Event = field(default_factory=Event)
    rollback_finished: Event = field(default_factory=Event)
    session: RuntimeSession | None = None
    spawn_error: Exception | None = None
    abandoned: bool = False
    published: bool = False
    rollback_running: bool = False
    close_complete: bool = False
    cleanup_complete: bool = False
    rollback_error: RuntimeBoundaryError | None = None


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: RuntimeName
    platform: RuntimePlatform
    dialect: ShellDialect
    transport: PtyTransport
    supervisor: ProcessSupervisor
    _spawn_attempts: dict[int, _SpawnAttempt] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _spawn_attempts_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _spawn_attempt_ids: Iterator[int] = field(
        default_factory=lambda: count(1),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        expected_platform = _EXPECTED_IDENTITY[self.name]
        if self.platform is not expected_platform:
            raise RuntimeConfigurationError(
                f"{self.name.value} requires platform {expected_platform.value}"
            )
        backend_name = (
            RuntimeName.WINDOWS_PWSH
            if self.platform is RuntimePlatform.WINDOWS
            else RuntimeName.POSIX_BASH
        )
        if any(
            component.runtime_name is not backend_name
            for component in (self.transport, self.supervisor)
        ):
            raise RuntimeConfigurationError("runtime transport and supervisor backends cannot mix")

    def open_session(
        self,
        request: SpawnRequest,
        *,
        cleanup_deadline_ms: int,
        startup_deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> ManagedRuntimeSession:
        """Spawn under ownership with cancellable isolation and retained rollback."""

        if startup_deadline_ms is not None and startup_deadline_ms <= 0:
            raise TransportError("PTY spawn deadline expired before ownership preparation")
        startup_deadline = (
            None if startup_deadline_ms is None else time.monotonic() + startup_deadline_ms / 1000
        )
        if not self.cleanup_pending_startups(deadline_ms=cleanup_deadline_ms):
            raise CleanupTimeout("an earlier runtime startup rollback is still pending")

        try:
            self._raise_if_cancelled(cancel_signal)
            ownership = self.supervisor.prepare()
        except RuntimeBoundaryError:
            raise
        except Exception as prepare_error:
            raise ProcessControlError("failed to prepare process ownership") from prepare_error

        attempt = _SpawnAttempt(
            attempt_id=next(self._spawn_attempt_ids),
            ownership=ownership,
            cleanup_deadline_ms=cleanup_deadline_ms,
        )
        with self._spawn_attempts_lock:
            self._spawn_attempts[attempt.attempt_id] = attempt

        initial_error = self._startup_abort_error(cancel_signal, startup_deadline)
        if initial_error is not None:
            self._complete_without_spawn(attempt, initial_error)
        else:
            spawn_thread = Thread(
                target=self._run_spawn_attempt,
                args=(attempt, request, startup_deadline, cancel_signal),
                name=f"tfbash-spawn-{attempt.attempt_id}",
                daemon=True,
            )
            try:
                spawn_thread.start()
            except Exception as error:
                launch_error = TransportError("failed to start the isolated PTY spawn")
                launch_error.__cause__ = error
                self._complete_without_spawn(attempt, launch_error)

        abort_error = self._wait_for_spawn(attempt, cancel_signal, startup_deadline)
        if abort_error is not None:
            rollback_now = self._abandon_attempt(attempt)
            if rollback_now:
                rollback_error = self._rollback_spawn_attempt(
                    attempt,
                    deadline_ms=cleanup_deadline_ms,
                )
                if rollback_error is not None:
                    raise rollback_error from abort_error
            raise abort_error

        with attempt.lock:
            spawn_error = attempt.spawn_error
            session = attempt.session
        if spawn_error is not None:
            rollback_now = self._abandon_attempt(attempt)
            rollback_error = (
                self._rollback_spawn_attempt(attempt, deadline_ms=cleanup_deadline_ms)
                if rollback_now
                else None
            )
            if rollback_error is not None:
                raise rollback_error from spawn_error
            if isinstance(spawn_error, RuntimeBoundaryError):
                raise spawn_error
            raise TransportError("PTY spawn failed") from spawn_error
        if session is None:
            impossible = TransportError("PTY spawn completed without a session")
            rollback_now = self._abandon_attempt(attempt)
            if rollback_now:
                rollback_error = self._rollback_spawn_attempt(
                    attempt,
                    deadline_ms=cleanup_deadline_ms,
                )
                if rollback_error is not None:
                    raise rollback_error from impossible
            raise impossible

        with attempt.lock:
            attempt.published = True
        self._remove_spawn_attempt(attempt)
        return ManagedRuntimeSession(session=session, ownership=ownership)

    @property
    def has_pending_startup_cleanup(self) -> bool:
        with self._spawn_attempts_lock:
            attempts = tuple(self._spawn_attempts.values())
        for attempt in attempts:
            with attempt.lock:
                if attempt.abandoned:
                    return True
        return False

    def cleanup_pending_startups(self, *, deadline_ms: int) -> bool:
        """Retry abandoned spawn/rollback state without losing native ownership."""

        deadline = time.monotonic() + max(0, deadline_ms) / 1000
        with self._spawn_attempts_lock:
            attempts = tuple(self._spawn_attempts.values())
        for attempt in attempts:
            with attempt.lock:
                abandoned = attempt.abandoned
            if not abandoned:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            if not attempt.completed.wait(remaining):
                return False
            rollback_now = self._claim_rollback(attempt)
            if rollback_now:
                self._rollback_spawn_attempt(
                    attempt,
                    deadline_ms=max(0, int((deadline - time.monotonic()) * 1000)),
                )
            else:
                remaining = max(0.0, deadline - time.monotonic())
                attempt.rollback_finished.wait(remaining)
            if self._contains_spawn_attempt(attempt):
                return False
        return not self.has_pending_startup_cleanup

    @staticmethod
    def _raise_if_cancelled(cancel_signal: CancellationSignal | None) -> None:
        if cancel_signal is not None and cancel_signal.is_set():
            raise TransportError("runtime startup was cancelled")

    @staticmethod
    def _startup_abort_error(
        cancel_signal: CancellationSignal | None,
        startup_deadline: float | None,
    ) -> TransportError | None:
        if cancel_signal is not None and cancel_signal.is_set():
            return TransportError("runtime startup was cancelled")
        if startup_deadline is not None and time.monotonic() >= startup_deadline:
            return TransportError("PTY spawn deadline expired")
        return None

    def _wait_for_spawn(
        self,
        attempt: _SpawnAttempt,
        cancel_signal: CancellationSignal | None,
        startup_deadline: float | None,
    ) -> TransportError | None:
        while True:
            with attempt.lock:
                if attempt.completed.is_set():
                    return None
            abort_error = self._startup_abort_error(cancel_signal, startup_deadline)
            if abort_error is not None:
                return abort_error
            wait_seconds = 0.01
            if startup_deadline is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0.0, startup_deadline - time.monotonic()),
                )
            attempt.wakeup.wait(wait_seconds)

    def _run_spawn_attempt(
        self,
        attempt: _SpawnAttempt,
        request: SpawnRequest,
        startup_deadline: float | None,
        cancel_signal: CancellationSignal | None,
    ) -> None:
        session: RuntimeSession | None = None
        spawn_error: Exception | None = None
        try:
            remaining_ms = (
                None
                if startup_deadline is None
                else max(0, int((startup_deadline - time.monotonic()) * 1000))
            )
            if remaining_ms is not None and remaining_ms <= 0:
                raise TransportError("PTY spawn deadline expired")
            session = self.transport.spawn(
                request,
                attempt.ownership,
                deadline_ms=remaining_ms,
                cancel_signal=cancel_signal,
            )
            self._raise_if_cancelled(cancel_signal)
        except Exception as error:
            spawn_error = error
        rollback_now = False
        with attempt.lock:
            attempt.session = session
            attempt.spawn_error = spawn_error
            attempt.completed.set()
            attempt.wakeup.set()
            if attempt.abandoned and not attempt.rollback_running:
                attempt.rollback_running = True
                attempt.rollback_finished.clear()
                rollback_now = True
        if rollback_now:
            self._rollback_spawn_attempt(
                attempt,
                deadline_ms=attempt.cleanup_deadline_ms,
            )

    @staticmethod
    def _complete_without_spawn(attempt: _SpawnAttempt, error: Exception) -> None:
        with attempt.lock:
            attempt.spawn_error = error
            attempt.completed.set()
            attempt.wakeup.set()

    @staticmethod
    def _abandon_attempt(attempt: _SpawnAttempt) -> bool:
        with attempt.lock:
            attempt.abandoned = True
            if attempt.completed.is_set() and not attempt.rollback_running:
                attempt.rollback_running = True
                attempt.rollback_finished.clear()
                return True
            return False

    @staticmethod
    def _claim_rollback(attempt: _SpawnAttempt) -> bool:
        with attempt.lock:
            if attempt.rollback_running:
                return False
            attempt.rollback_running = True
            attempt.rollback_finished.clear()
            return True

    def _rollback_spawn_attempt(
        self,
        attempt: _SpawnAttempt,
        *,
        deadline_ms: int,
    ) -> RuntimeBoundaryError | None:
        deadline = time.monotonic() + max(0, deadline_ms) / 1000
        close_error: RuntimeBoundaryError | None = None
        cleanup_error: RuntimeBoundaryError | None = None
        with attempt.lock:
            session = attempt.session
            close_complete = attempt.close_complete
            cleanup_complete = attempt.cleanup_complete
        if session is None:
            close_complete = True
        elif not close_complete:
            try:
                self.transport.close(
                    session,
                    deadline_ms=max(0, int((deadline - time.monotonic()) * 1000)),
                )
                close_complete = True
            except RuntimeBoundaryError as error:
                close_error = error
            except Exception as error:
                close_error = TransportError("spawn rollback raised while closing its PTY")
                close_error.__cause__ = error
        if not cleanup_complete:
            try:
                cleanup = self.supervisor.cleanup(
                    attempt.ownership,
                    deadline_ms=max(0, int((deadline - time.monotonic()) * 1000)),
                )
                cleanup_complete = cleanup.reaped
                if not cleanup.reaped:
                    cleanup_error = CleanupTimeout("spawn failed and ownership cleanup timed out")
            except RuntimeBoundaryError as error:
                cleanup_error = error
            except Exception as error:
                cleanup_error = ProcessControlError(
                    "spawn failed and ownership cleanup raised an unexpected error"
                )
                cleanup_error.__cause__ = error
        rollback_error = cleanup_error or close_error
        if cleanup_error is not None and close_error is not None:
            cleanup_error.__context__ = close_error
        with attempt.lock:
            attempt.close_complete = close_complete
            attempt.cleanup_complete = cleanup_complete
            attempt.rollback_error = rollback_error
            attempt.rollback_running = False
            attempt.rollback_finished.set()
        if close_complete and cleanup_complete:
            self._remove_spawn_attempt(attempt)
            return None
        return rollback_error or CleanupTimeout("runtime startup rollback remains pending")

    def _remove_spawn_attempt(self, attempt: _SpawnAttempt) -> None:
        with self._spawn_attempts_lock:
            if self._spawn_attempts.get(attempt.attempt_id) is attempt:
                self._spawn_attempts.pop(attempt.attempt_id, None)

    def _contains_spawn_attempt(self, attempt: _SpawnAttempt) -> bool:
        with self._spawn_attempts_lock:
            return self._spawn_attempts.get(attempt.attempt_id) is attempt


class PosixBashProfile(RuntimeProfile):
    def __init__(
        self,
        *,
        dialect: ShellDialect,
        transport: PtyTransport,
        supervisor: ProcessSupervisor,
    ) -> None:
        super().__init__(
            name=RuntimeName.POSIX_BASH,
            platform=RuntimePlatform.POSIX,
            dialect=dialect,
            transport=transport,
            supervisor=supervisor,
        )


class WindowsPwshProfile(RuntimeProfile):
    def __init__(
        self,
        *,
        dialect: ShellDialect,
        transport: PtyTransport,
        supervisor: ProcessSupervisor,
    ) -> None:
        super().__init__(
            name=RuntimeName.WINDOWS_PWSH,
            platform=RuntimePlatform.WINDOWS,
            dialect=dialect,
            transport=transport,
            supervisor=supervisor,
        )


class NativeRuntimeProfile(RuntimeProfile):
    """Compose any supported dialect with the host-native terminal backend."""

    def __init__(
        self,
        *,
        name: RuntimeName,
        platform: RuntimePlatform,
        dialect: ShellDialect,
        transport: PtyTransport,
        supervisor: ProcessSupervisor,
    ) -> None:
        super().__init__(
            name=name,
            platform=platform,
            dialect=dialect,
            transport=transport,
            supervisor=supervisor,
        )
