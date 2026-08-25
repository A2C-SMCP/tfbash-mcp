"""Windows Job Object ownership and identity-fenced process control."""

from __future__ import annotations

import base64
import json
import secrets
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol

from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
    CleanupResult,
    ControlDelivery,
    ControlIntent,
    ProcessOwnership,
    RuntimeName,
    SpawnRequest,
)
from tfbash_mcp.runtime.errors import ProcessControlError


@dataclass(frozen=True, slots=True)
class WindowsProcessIdentity:
    """A PID plus its creation time, retained behind an open process handle."""

    process_id: int
    creation_time_100ns: int


@dataclass(frozen=True, slots=True)
class WindowsProcessHandle:
    """Opaque identity-fenced process handle returned by the native adapter."""

    identity: WindowsProcessIdentity
    value: object


class WindowsProcessApi(Protocol):
    """Small testable Win32 surface used by the production supervisor."""

    def create_kill_on_close_job(self) -> object: ...

    def close_job(self, job: object) -> None: ...

    def create_gate_event(self, name: str) -> object: ...

    def signal_gate_event(self, event: object) -> None: ...

    def close_gate_event(self, event: object) -> None: ...

    def child_gate_is_ready(self, name: str) -> bool: ...

    def open_process(
        self,
        process_id: int,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle: ...

    def duplicate_process(
        self,
        process_id: int,
        native_handle: object,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle: ...

    def open_process_if_alive(self, process_id: int) -> WindowsProcessHandle | None: ...

    def close_process(self, process: WindowsProcessHandle) -> None: ...

    def assign_process(self, job: object, process: WindowsProcessHandle) -> None: ...

    def process_is_in_job(self, job: object, process: WindowsProcessHandle) -> bool: ...

    def process_is_alive(self, process: WindowsProcessHandle) -> bool: ...

    def terminate_process(self, process: WindowsProcessHandle, exit_code: int) -> None: ...

    def wait_processes(
        self,
        processes: tuple[WindowsProcessHandle, ...],
        timeout_ms: int,
    ) -> bool: ...

    def active_job_processes(self, job: object) -> int: ...

    def job_process_ids(
        self,
        job: object,
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]: ...

    def terminate_job(self, job: object, exit_code: int) -> None: ...


InterruptSender = Callable[[int | None], bool]

_GATE_NAME_ENV = "TFBASH_MCP_GATE_NAME"
_GATE_PAYLOAD_ENV = "TFBASH_MCP_GATE_PAYLOAD"
_GATE_TIMEOUT_ENV = "TFBASH_MCP_GATE_TIMEOUT_MS"
_GATE_ENV_KEYS = frozenset(
    key.casefold() for key in (_GATE_NAME_ENV, _GATE_PAYLOAD_ENV, _GATE_TIMEOUT_ENV)
)
_INFRASTRUCTURE_STABLE_SECONDS = 0.1


class _CleanupDeadlineExpired(Exception):
    """Internal conservative deadline signal; never crosses the runtime port."""


class WindowsProcessOwnership:
    """One pre-created Job Object and the exact shell process attached to it."""

    def __init__(
        self,
        *,
        ownership_id: str,
        supervisor_token: object,
        api: WindowsProcessApi,
        job: object,
        gate: object,
        gate_name: str,
        gate_wait_timeout_ms: int,
        shell_ready_timeout_ms: int,
        bootstrap_path: str,
        python_executable: str,
        attach_cleanup_timeout_ms: int,
    ) -> None:
        self._ownership_id = ownership_id
        self._supervisor_token = supervisor_token
        self._api = api
        self._job: object | None = job
        self._gate: object | None = gate
        self._gate_name = gate_name
        self._gate_wait_timeout_ms = gate_wait_timeout_ms
        self._shell_ready_timeout_ms = shell_ready_timeout_ms
        self._bootstrap_path = bootstrap_path
        self._python_executable = python_executable
        self._attach_cleanup_timeout_ms = attach_cleanup_timeout_ms
        self._lock = Lock()
        self._reserve_consumed = False
        self._attachment_pending = False
        self._root: WindowsProcessHandle | None = None
        self._bootstrap: WindowsProcessHandle | None = None
        self._infrastructure: dict[WindowsProcessIdentity, WindowsProcessHandle] = {}
        self._pending_close: dict[WindowsProcessIdentity, WindowsProcessHandle] = {}
        self._attachment_indeterminate = False
        self._attached = False
        self._interrupt_sender: InterruptSender | None = None
        self._job_termination_requested = False
        self._finalized = False

    @property
    def ownership_id(self) -> str:
        return self._ownership_id

    def reserve_spawn(self, request: SpawnRequest) -> SpawnRequest:
        """Replace the target with a trusted bootstrap blocked on our gate."""

        with self._lock:
            if self._reserve_consumed or self._finalized:
                raise ProcessControlError("Windows ownership cannot be reused")
            folded = {key.casefold() for key in request.environment}
            if folded & _GATE_ENV_KEYS:
                raise ProcessControlError("Windows launch environment uses reserved bootstrap keys")
            if self._gate is None:
                raise ProcessControlError("Windows bootstrap gate is closed")
            self._reserve_consumed = True
            self._attachment_pending = True
            payload = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "executable": request.executable,
                        "arguments": list(request.arguments),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).decode("ascii")
            environment = dict(request.environment)
            environment.update(
                {
                    _GATE_NAME_ENV: self._gate_name,
                    _GATE_PAYLOAD_ENV: payload,
                    _GATE_TIMEOUT_ENV: str(self._gate_wait_timeout_ms),
                }
            )
            return SpawnRequest(
                executable=self._python_executable,
                arguments=("-I", "-u", self._bootstrap_path),
                cwd=request.cwd,
                environment=environment,
            )

    def abort_unspawned(self) -> None:
        """Disarm a reservation after transport proves spawn was never called."""

        with self._lock:
            if (
                not self._reserve_consumed
                or not self._attachment_pending
                or self._root is not None
                or self._finalized
            ):
                raise ProcessControlError("Windows spawn reservation cannot be aborted")
            self._attachment_pending = False

    def attach(
        self,
        process_id: int,
        *,
        native_handle: object | None = None,
        release: bool = True,
        deadline: float | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> bool:
        """Identity-fence and assign the spawned shell to the prepared Job Object.

        Any failure after opening the process leaves the exact process handle
        reachable by :meth:`WindowsProcessSupervisor.cleanup`.  A best-effort
        immediate termination is attempted without ever recapturing the PID.
        """

        if process_id <= 0:
            raise ProcessControlError("invalid Windows process attachment")
        with self._lock:
            if not self._attachment_pending or self._finalized:
                raise ProcessControlError("Windows ownership cannot be attached again")
            self._attachment_pending = False
            try:
                if native_handle is None:
                    raise ProcessControlError("spawned Windows process has no exact native handle")
                root = self._api.duplicate_process(
                    process_id,
                    native_handle,
                    assign_to_job=True,
                )
            except Exception as error:
                self._attachment_indeterminate = True
                if isinstance(error, ProcessControlError):
                    raise
                raise ProcessControlError(
                    "failed to retain the spawned Windows process handle"
                ) from error
            self._root = root
            job = self._require_job()
            try:
                self._api.assign_process(job, root)
                if not self._api.process_is_in_job(job, root):
                    raise ProcessControlError(
                        "spawned Windows process was not assigned to its Job Object"
                    )
            except Exception as error:
                self._terminate_failed_attachment(root)
                if isinstance(error, ProcessControlError):
                    raise
                raise ProcessControlError(
                    "failed to assign the spawned Windows process to its Job Object"
                ) from error
            if not release:
                return False
            if not self._release_allowed(deadline, cancel_signal):
                return False
            gate = self._gate
            if gate is None:
                self._terminate_failed_attachment(root)
                raise ProcessControlError("Windows bootstrap gate is closed")
            try:
                self._api.signal_gate_event(gate)
                shell = self._await_shell(
                    root,
                    deadline=deadline,
                    cancel_signal=cancel_signal,
                )
                self._capture_infrastructure(
                    root,
                    shell,
                    deadline=deadline,
                    cancel_signal=cancel_signal,
                )
            except Exception as error:
                self._terminate_failed_attachment(root)
                raise ProcessControlError(
                    "failed to release the assigned Windows bootstrap"
                ) from error
            self._bootstrap = root
            self._root = shell
            self._attached = True
            return True

    def _capture_infrastructure(
        self,
        bootstrap: WindowsProcessHandle,
        shell: WindowsProcessHandle,
        *,
        deadline: float | None,
        cancel_signal: CancellationSignal | None,
    ) -> None:
        """Retain exact handles for trusted ConPTY support processes.

        This runs after the trusted bootstrap has authenticated the PowerShell
        child and before any startup/user input is written.  Job members present
        at this point are runtime infrastructure, such as the ConPTY console
        host, and must survive per-execution descendant cleanup.
        """

        job = self._require_job()
        trusted = {bootstrap.identity, shell.identity}
        capture_deadline = time.monotonic() + self._shell_ready_timeout_ms / 1000
        if deadline is not None:
            capture_deadline = min(capture_deadline, deadline)
        stable_seconds = min(
            _INFRASTRUCTURE_STABLE_SECONDS,
            self._shell_ready_timeout_ms / 2000,
        )
        last_process_ids: tuple[int, ...] | None = None
        stable_since = time.monotonic()
        while True:
            if not self._release_allowed(deadline, cancel_signal):
                raise ProcessControlError("Windows infrastructure capture was cancelled or expired")
            now = time.monotonic()
            if now >= capture_deadline:
                raise ProcessControlError("Windows infrastructure capture expired")
            process_ids = tuple(sorted(self._api.job_process_ids(job, deadline=capture_deadline)))
            if process_ids != last_process_ids:
                last_process_ids = process_ids
                stable_since = now
            for process_id in process_ids:
                if process_id in {
                    bootstrap.identity.process_id,
                    shell.identity.process_id,
                }:
                    continue
                process = self._api.open_process_if_alive(process_id)
                if process is None:
                    continue
                if process.identity in trusted or process.identity in self._infrastructure:
                    self._api.close_process(process)
                    continue
                if not self._api.process_is_in_job(job, process):
                    self._api.close_process(process)
                    raise ProcessControlError(
                        "Windows infrastructure process escaped its Job Object"
                    )
                self._infrastructure[process.identity] = process
            now = time.monotonic()
            if now - stable_since >= stable_seconds:
                return
            time.sleep(min(0.005, capture_deadline - now))

    def _await_shell(
        self,
        bootstrap: WindowsProcessHandle,
        *,
        deadline: float | None,
        cancel_signal: CancellationSignal | None,
    ) -> WindowsProcessHandle:
        ready_deadline = time.monotonic() + self._shell_ready_timeout_ms / 1000
        if deadline is not None:
            ready_deadline = min(ready_deadline, deadline)
        job = self._require_job()
        while self._release_allowed(ready_deadline, cancel_signal):
            for process_id in self._api.job_process_ids(job, deadline=ready_deadline):
                if process_id == bootstrap.identity.process_id:
                    continue
                process = self._api.open_process_if_alive(process_id)
                if process is None:
                    continue
                try:
                    ready_name = (
                        f"{self._gate_name}-child-{process.identity.process_id}-"
                        f"{process.identity.creation_time_100ns}"
                    )
                    if self._api.process_is_in_job(
                        job,
                        process,
                    ) and self._api.child_gate_is_ready(ready_name):
                        if not self._release_allowed(ready_deadline, cancel_signal):
                            self._api.close_process(process)
                            raise ProcessControlError(
                                "Windows bootstrap release was cancelled or expired"
                            )
                        return process
                except Exception:
                    self._api.close_process(process)
                    raise
                self._api.close_process(process)
            time.sleep(min(0.005, max(0.0, ready_deadline - time.monotonic())))
        raise ProcessControlError(
            "Windows bootstrap child Shell readiness was cancelled or expired"
        )

    @staticmethod
    def _release_allowed(
        deadline: float | None,
        cancel_signal: CancellationSignal | None,
    ) -> bool:
        return not (
            (cancel_signal is not None and cancel_signal.is_set())
            or (deadline is not None and time.monotonic() >= deadline)
        )

    def bind_interrupt(self, sender: InterruptSender) -> None:
        """Bind the ConPTY Ctrl-C writer after the session has been constructed."""

        with self._lock:
            if not self._attached or self._finalized or self._interrupt_sender is not None:
                raise ProcessControlError("Windows interrupt channel cannot be bound")
            self._interrupt_sender = sender

    def _terminate_failed_attachment(self, root: WindowsProcessHandle) -> None:
        try:
            job = self._job
            if job is not None and self._api.process_is_in_job(job, root):
                self._api.terminate_job(job, 1)
                self._job_termination_requested = True
            if self._api.process_is_alive(root):
                self._api.terminate_process(root, 1)
                self._api.wait_processes((root,), self._attach_cleanup_timeout_ms)
        except Exception:
            # The exact open handle remains stored on this ownership.  Runtime
            # rollback will retry termination without a PID-reuse race.
            return

    def _require_job(self) -> object:
        if self._job is None:
            raise ProcessControlError("Windows Job Object is closed")
        return self._job


class WindowsProcessSupervisor:
    """Own one non-breakaway Job Object and exact handles for its process tree."""

    runtime_name = RuntimeName.WINDOWS_PWSH

    def __init__(
        self,
        *,
        api: WindowsProcessApi | None = None,
        ownership_id_factory: Callable[[], str] | None = None,
        gate_name_factory: Callable[[], str] | None = None,
        terminate_grace_ms: int = 250,
        attach_cleanup_timeout_ms: int = 1000,
        gate_wait_timeout_ms: int = 10_000,
        shell_ready_timeout_ms: int = 5_000,
        bootstrap_path: str | None = None,
        python_executable: str | None = None,
        ownership_factory: Callable[..., WindowsProcessOwnership] | None = None,
    ) -> None:
        if terminate_grace_ms < 0:
            raise ValueError("terminate grace cannot be negative")
        if attach_cleanup_timeout_ms <= 0:
            raise ValueError("attach cleanup timeout must be positive")
        if gate_wait_timeout_ms <= 0:
            raise ValueError("bootstrap gate timeout must be positive")
        if shell_ready_timeout_ms <= 0:
            raise ValueError("Shell readiness timeout must be positive")
        self._api = api
        self._supervisor_token = object()
        self._ownership_id_factory = ownership_id_factory or (
            lambda: f"owner_{secrets.token_hex(16)}"
        )
        self._gate_name_factory = gate_name_factory or (
            lambda: rf"Local\tfbash-mcp-{secrets.token_hex(32)}"
        )
        self._terminate_grace_ms = terminate_grace_ms
        self._attach_cleanup_timeout_ms = attach_cleanup_timeout_ms
        self._gate_wait_timeout_ms = gate_wait_timeout_ms
        self._shell_ready_timeout_ms = shell_ready_timeout_ms
        self._bootstrap_path = bootstrap_path or str(
            Path(__file__).with_name("windows_bootstrap.py").resolve()
        )
        self._python_executable = python_executable or sys.executable
        self._ownership_factory = ownership_factory or WindowsProcessOwnership

    def prepare(self) -> ProcessOwnership:
        api: WindowsProcessApi | None = None
        job: object | None = None
        gate: object | None = None
        try:
            ownership_id = self._ownership_id_factory()
            if not ownership_id or "\x00" in ownership_id:
                raise ProcessControlError("Windows ownership id is invalid")
            api = self._api or _create_windows_process_api()
            job = api.create_kill_on_close_job()
            gate_name = self._gate_name_factory()
            if (
                not gate_name.startswith("Local\\tfbash-mcp-")
                or "\x00" in gate_name
                or len(gate_name) > 240
            ):
                raise ProcessControlError("Windows bootstrap gate name is invalid")
            gate = api.create_gate_event(gate_name)
            ownership = self._ownership_factory(
                ownership_id=ownership_id,
                supervisor_token=self._supervisor_token,
                api=api,
                job=job,
                gate=gate,
                gate_name=gate_name,
                gate_wait_timeout_ms=self._gate_wait_timeout_ms,
                shell_ready_timeout_ms=self._shell_ready_timeout_ms,
                bootstrap_path=self._bootstrap_path,
                python_executable=self._python_executable,
                attach_cleanup_timeout_ms=self._attach_cleanup_timeout_ms,
            )
            job = None
            gate = None
            return ownership
        except Exception as error:
            cleanup_error: Exception | None = None
            if api is not None and gate is not None:
                try:
                    api.close_gate_event(gate)
                except Exception as close_error:
                    cleanup_error = close_error
            if api is not None and job is not None:
                try:
                    api.close_job(job)
                except Exception as close_error:
                    cleanup_error = cleanup_error or close_error
            if cleanup_error is not None:
                raise ProcessControlError(
                    "failed to prepare and roll back Windows ownership"
                ) from cleanup_error
            if isinstance(error, ProcessControlError):
                raise
            raise ProcessControlError("failed to prepare Windows process ownership") from error

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
        *,
        deadline_ms: int | None = None,
    ) -> ControlDelivery:
        concrete = self._ownership(ownership)
        deadline = None if deadline_ms is None else time.monotonic() + max(0, deadline_ms) / 1000
        with concrete._lock, _translate_native_errors("Windows process control failed"):
            self._check_control_deadline(deadline)
            if concrete._finalized:
                return ControlDelivery(delivered=False)
            self._retry_pending_closes(concrete)
            root = self._require_attached(concrete)
            if intent is ControlIntent.INTERRUPT:
                if not concrete._api.process_is_alive(root):
                    return ControlDelivery(delivered=False)
                return ControlDelivery(delivered=self._send_interrupt(concrete, deadline=deadline))
            if intent is ControlIntent.TERMINATE:
                delivered = (
                    self._send_interrupt(concrete, deadline=deadline)
                    if concrete._api.process_is_alive(root)
                    else False
                )
                descendants = self._job_members(concrete, deadline=deadline)
                try:
                    if descendants and self._terminate_grace_ms:
                        remaining_ms = self._remaining_control_ms(deadline)
                        concrete._api.wait_processes(
                            descendants,
                            min(self._terminate_grace_ms, remaining_ms),
                        )
                    survivors = tuple(
                        process
                        for process in descendants
                        if concrete._api.process_is_alive(process)
                    )
                    for process in reversed(survivors):
                        self._check_control_deadline(deadline)
                        concrete._api.terminate_process(process, 1)
                    return ControlDelivery(delivered=delivered or bool(survivors))
                finally:
                    self._close_processes(concrete, descendants)
            # A forced execution kill cannot be represented inside the
            # PowerShell process itself.  Terminating the Job Object deliberately
            # kills the persistent shell too; #9 observes that and rebuilds it.
            if not concrete._api.job_process_ids(
                concrete._require_job(),
                deadline=deadline,
            ):
                return ControlDelivery(delivered=False)
            self._check_control_deadline(deadline)
            concrete._api.terminate_job(concrete._require_job(), 1)
            concrete._job_termination_requested = True
            return ControlDelivery(delivered=True)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        concrete = self._ownership(ownership)
        with concrete._lock, _translate_native_errors("failed to inspect Windows process liveness"):
            if concrete._finalized:
                return False
            root = self._require_attached(concrete)
            active = len(concrete._api.job_process_ids(concrete._require_job()))
            if concrete._api.process_is_alive(root):
                return True
            if active == 0:
                self._finalize(concrete)
            return False

    def cleanup_execution(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        concrete = self._ownership(ownership)
        if deadline_ms < 0:
            raise ProcessControlError("execution cleanup deadline cannot be negative")
        if deadline_ms == 0:
            return CleanupResult(reaped=False, remaining_managed_processes=1)
        deadline = time.monotonic() + deadline_ms / 1000
        try:
            with (
                concrete._lock,
                _translate_native_errors(
                    "Windows execution cleanup failed",
                    preserve_cleanup_deadline=True,
                ),
            ):
                if concrete._finalized:
                    raise ProcessControlError("cannot clean an execution after Shell cleanup")
                self._require_attached(concrete)
                self._retry_pending_closes(concrete)
                while True:
                    descendants = self._job_members(concrete, deadline=deadline)
                    try:
                        live: list[WindowsProcessHandle] = []
                        for process in descendants:
                            self._check_deadline(deadline)
                            if concrete._api.process_is_alive(process):
                                live.append(process)
                            self._check_deadline(deadline)
                        if not live:
                            remaining = self._remaining_descendants(
                                concrete,
                                deadline=deadline,
                            )
                            return CleanupResult(
                                reaped=remaining == 0,
                                remaining_managed_processes=remaining,
                            )
                        for process in reversed(live):
                            self._check_deadline(deadline)
                            concrete._api.terminate_process(process, 1)
                            self._check_deadline(deadline)
                        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                        concrete._api.wait_processes(tuple(live), remaining_ms)
                    finally:
                        self._close_processes(concrete, descendants)
                    self._check_deadline(deadline)
        except _CleanupDeadlineExpired:
            return CleanupResult(reaped=False, remaining_managed_processes=1)

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        concrete = self._ownership(ownership)
        if deadline_ms < 0:
            raise ProcessControlError("cleanup deadline cannot be negative")
        if deadline_ms == 0:
            with concrete._lock:
                if concrete._finalized:
                    return CleanupResult(reaped=True, remaining_managed_processes=0)
            return CleanupResult(reaped=False, remaining_managed_processes=1)
        deadline = time.monotonic() + deadline_ms / 1000
        try:
            with (
                concrete._lock,
                _translate_native_errors(
                    "Windows Shell cleanup failed",
                    preserve_cleanup_deadline=True,
                ),
            ):
                if concrete._finalized:
                    return CleanupResult(reaped=True, remaining_managed_processes=0)
                job = concrete._require_job()
                root = concrete._root
                if root is None:
                    if concrete._attachment_indeterminate or concrete._attachment_pending:
                        return CleanupResult(reaped=False, remaining_managed_processes=1)
                    self._finalize(concrete)
                    return CleanupResult(reaped=True, remaining_managed_processes=0)
                self._retry_pending_closes(concrete)
                job_has_processes = bool(self._job_process_ids(concrete, deadline=deadline))
                if job_has_processes:
                    concrete._api.terminate_job(job, 1)
                    concrete._job_termination_requested = True
                    self._check_deadline(deadline)
                elif not concrete._job_termination_requested and concrete._api.process_is_alive(
                    root
                ):
                    concrete._api.terminate_process(root, 1)
                    self._check_deadline(deadline)
                while True:
                    active = len(self._job_process_ids(concrete, deadline=deadline))
                    root_alive = concrete._api.process_is_alive(root)
                    self._check_deadline(deadline)
                    if active == 0 and not root_alive:
                        self._finalize(concrete)
                        return CleanupResult(reaped=True, remaining_managed_processes=0)
                    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                    if root_alive:
                        concrete._api.wait_processes((root,), min(remaining_ms, 20))
                    elif remaining_ms:
                        time.sleep(min(0.02, remaining_ms / 1000))
                    self._check_deadline(deadline)
        except _CleanupDeadlineExpired:
            return CleanupResult(reaped=False, remaining_managed_processes=1)

    def _ownership(self, ownership: ProcessOwnership) -> WindowsProcessOwnership:
        if not isinstance(ownership, WindowsProcessOwnership):
            raise ProcessControlError("ownership was not created by the Windows supervisor")
        if ownership._supervisor_token is not self._supervisor_token:
            raise ProcessControlError("ownership belongs to a different Windows supervisor")
        return ownership

    @staticmethod
    def _require_attached(ownership: WindowsProcessOwnership) -> WindowsProcessHandle:
        if not ownership._attached or ownership._root is None:
            raise ProcessControlError("Windows ownership has not been attached")
        return ownership._root

    @staticmethod
    def _send_interrupt(
        ownership: WindowsProcessOwnership,
        *,
        deadline: float | None,
    ) -> bool:
        sender = ownership._interrupt_sender
        if sender is None:
            return False
        try:
            return sender(WindowsProcessSupervisor._remaining_control_ms(deadline))
        except ProcessControlError:
            raise
        except Exception as error:
            raise ProcessControlError("failed to deliver ConPTY Ctrl-C") from error

    @staticmethod
    def _job_members(
        ownership: WindowsProcessOwnership,
        *,
        deadline: float | None = None,
    ) -> tuple[WindowsProcessHandle, ...]:
        members: list[WindowsProcessHandle] = []
        try:
            job = ownership._require_job()
            for process_id in WindowsProcessSupervisor._job_process_ids(
                ownership,
                deadline=deadline,
            ):
                WindowsProcessSupervisor._check_deadline(deadline)
                retained = WindowsProcessSupervisor._persistent_by_pid(ownership).get(process_id)
                if retained is not None and ownership._api.process_is_alive(retained):
                    continue
                process = ownership._api.open_process_if_alive(process_id)
                if process is None:
                    WindowsProcessSupervisor._check_deadline(deadline)
                    continue
                ownership._pending_close[process.identity] = process
                WindowsProcessSupervisor._check_deadline(deadline)
                if process.identity in WindowsProcessSupervisor._persistent_identities(ownership):
                    WindowsProcessSupervisor._close_processes(ownership, (process,))
                elif ownership._api.process_is_in_job(job, process):
                    members.append(process)
                else:
                    WindowsProcessSupervisor._close_processes(ownership, (process,))
            return tuple(members)
        except Exception as error:
            with suppress(ProcessControlError):
                WindowsProcessSupervisor._retry_pending_closes(ownership)
            if isinstance(error, _CleanupDeadlineExpired):
                raise
            raise ProcessControlError("failed to inspect Windows Job members") from error

    @staticmethod
    def _close_processes(
        ownership: WindowsProcessOwnership,
        processes: tuple[WindowsProcessHandle, ...],
    ) -> None:
        first_error: Exception | None = None
        for process in processes:
            try:
                ownership._api.close_process(process)
                ownership._pending_close.pop(process.identity, None)
            except Exception as error:
                first_error = first_error or error
        if first_error is not None:
            raise ProcessControlError("failed to close Windows process handles") from first_error

    @staticmethod
    def _remaining_descendants(
        ownership: WindowsProcessOwnership,
        *,
        deadline: float | None = None,
    ) -> int:
        descendants = WindowsProcessSupervisor._job_members(
            ownership,
            deadline=deadline,
        )
        try:
            return len(descendants)
        finally:
            WindowsProcessSupervisor._close_processes(ownership, descendants)

    @staticmethod
    def _persistent_by_pid(
        ownership: WindowsProcessOwnership,
    ) -> dict[int, WindowsProcessHandle]:
        return {
            process.identity.process_id: process
            for process in WindowsProcessSupervisor._persistent_handles(ownership)
        }

    @staticmethod
    def _persistent_identities(
        ownership: WindowsProcessOwnership,
    ) -> frozenset[WindowsProcessIdentity]:
        return frozenset(
            process.identity for process in WindowsProcessSupervisor._persistent_handles(ownership)
        )

    @staticmethod
    def _persistent_handles(
        ownership: WindowsProcessOwnership,
    ) -> tuple[WindowsProcessHandle, ...]:
        roots = tuple(
            process for process in (ownership._root, ownership._bootstrap) if process is not None
        )
        return (*roots, *ownership._infrastructure.values())

    @staticmethod
    def _job_process_ids(
        ownership: WindowsProcessOwnership,
        *,
        deadline: float | None,
    ) -> tuple[int, ...]:
        WindowsProcessSupervisor._check_deadline(deadline)
        try:
            process_ids = ownership._api.job_process_ids(
                ownership._require_job(),
                deadline=deadline,
            )
        except TimeoutError as error:
            raise _CleanupDeadlineExpired from error
        WindowsProcessSupervisor._check_deadline(deadline)
        return process_ids

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise _CleanupDeadlineExpired

    @staticmethod
    def _check_control_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ProcessControlError("Windows process control deadline expired")

    @staticmethod
    def _remaining_control_ms(deadline: float | None) -> int:
        if deadline is None:
            return 2**31 - 1
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise ProcessControlError("Windows process control deadline expired")
        return remaining

    @staticmethod
    def _retry_pending_closes(ownership: WindowsProcessOwnership) -> None:
        if ownership._pending_close:
            WindowsProcessSupervisor._close_processes(
                ownership,
                tuple(ownership._pending_close.values()),
            )

    @staticmethod
    def _finalize(ownership: WindowsProcessOwnership) -> None:
        first_error: Exception | None = None
        try:
            WindowsProcessSupervisor._retry_pending_closes(ownership)
        except Exception as error:
            first_error = error
        for identity, process in tuple(ownership._infrastructure.items()):
            try:
                ownership._api.close_process(process)
                ownership._infrastructure.pop(identity, None)
            except Exception as error:
                first_error = first_error or error
        if ownership._root is not None:
            try:
                ownership._api.close_process(ownership._root)
                ownership._root = None
            except Exception as error:
                first_error = first_error or error
        if ownership._bootstrap is not None:
            try:
                ownership._api.close_process(ownership._bootstrap)
                ownership._bootstrap = None
            except Exception as error:
                first_error = first_error or error
        if ownership._gate is not None:
            try:
                ownership._api.close_gate_event(ownership._gate)
                ownership._gate = None
            except Exception as error:
                first_error = first_error or error
        if ownership._job is not None:
            try:
                ownership._api.close_job(ownership._job)
                ownership._job = None
            except Exception as error:
                first_error = first_error or error
        if first_error is not None:
            raise ProcessControlError("failed to close Windows ownership handles") from first_error
        ownership._interrupt_sender = None
        ownership._attached = False
        ownership._finalized = True


def _create_windows_process_api() -> WindowsProcessApi:
    try:
        from tfbash_mcp.runtime.windows_win32 import CtypesWindowsProcessApi

        return CtypesWindowsProcessApi()
    except ProcessControlError:
        raise
    except Exception as error:
        raise ProcessControlError("native Windows process APIs are unavailable") from error


@contextmanager
def _translate_native_errors(
    message: str,
    *,
    preserve_cleanup_deadline: bool = False,
) -> Iterator[None]:
    try:
        yield
    except ProcessControlError:
        raise
    except _CleanupDeadlineExpired as error:
        if preserve_cleanup_deadline:
            raise
        raise ProcessControlError(message) from error
    except Exception as error:
        raise ProcessControlError(message) from error
