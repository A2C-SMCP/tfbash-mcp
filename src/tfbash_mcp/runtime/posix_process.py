"""POSIX process-session ownership and semantic process control."""

from __future__ import annotations

import errno
import os
import secrets
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import ClassVar

from tfbash_mcp.runtime.contracts import (
    CleanupResult,
    ControlDelivery,
    ControlIntent,
    ProcessOwnership,
    RuntimeName,
)
from tfbash_mcp.runtime.errors import ProcessControlError


@dataclass(frozen=True, slots=True)
class _ProcessRecord:
    process_id: int
    process_group_id: int
    session_id: int
    state: str

    @property
    def is_zombie(self) -> bool:
        return self.state.startswith("Z")


class _DeadlineExpired(Exception):
    """Internal control-flow marker for a fully consumed cleanup budget."""


class PosixProcessOwnership:
    """Opaque owner for one forkpty-created process session."""

    def __init__(self, *, ownership_id: str, supervisor_token: object) -> None:
        self._ownership_id = ownership_id
        self._supervisor_token = supervisor_token
        self._lock = Lock()
        self._attach_consumed = False
        self._attachment_pending = False
        self._process_id: int | None = None
        self._session_id: int | None = None
        self._shell_process_group_id: int | None = None
        self._terminal_file_descriptor = -1
        self._finalized = False

    @property
    def ownership_id(self) -> str:
        return self._ownership_id

    def child_setup(self) -> None:
        """Rely on forkpty's already-established session and controlling PTY.

        ptyprocess calls this hook after ``forkpty()``.  Calling ``setsid()``
        again there would fail because the child is already a session leader.
        The parent verifies that invariant atomically in :meth:`attach`.
        """

    def reserve(self) -> None:
        """Permanently reserve this owner before fork, rejecting reuse pre-spawn."""

        with self._lock:
            if self._attach_consumed:
                raise ProcessControlError("POSIX ownership cannot be reused")
            self._attach_consumed = True
            self._attachment_pending = True

    def attach(self, process_id: int, terminal_file_descriptor: int) -> None:
        """Attach the exec'd session leader and retain a private foreground observer."""

        if process_id <= 0:
            raise ProcessControlError("invalid POSIX process attachment")
        with self._lock:
            if not self._attachment_pending or terminal_file_descriptor < 0:
                self._terminate_invalid_attachment(process_id)
                raise ProcessControlError("POSIX ownership cannot be attached again")
            self._attachment_pending = False

            # Record the process first.  Every later failure therefore leaves a
            # leader reachable by supervisor cleanup, satisfying the strong
            # attach guarantee required by the PTY transport.
            self._process_id = process_id
            try:
                session_id = os.getsid(process_id)
                process_group_id = os.getpgid(process_id)
            except OSError as error:
                raise ProcessControlError("failed to inspect POSIX process ownership") from error
            self._session_id = session_id
            self._shell_process_group_id = process_group_id
            if session_id != process_id or process_group_id != process_id:
                self._terminate_invalid_attachment(process_id)
                self._mark_finalized()
                raise ProcessControlError("forkpty child did not establish an isolated session")
            try:
                self._terminal_file_descriptor = os.dup(terminal_file_descriptor)
            except OSError as error:
                raise ProcessControlError("failed to retain the POSIX terminal identity") from error

    @staticmethod
    def _terminate_invalid_attachment(process_id: int) -> None:
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise ProcessControlError(
                "failed to terminate an invalid POSIX attachment"
            ) from error
        try:
            os.waitpid(process_id, 0)
        except ChildProcessError:
            pass
        except OSError as error:
            raise ProcessControlError("failed to reap an invalid POSIX attachment") from error

    def _mark_finalized(self) -> None:
        self._finalized = True
        self._attachment_pending = False
        self._process_id = None
        self._session_id = None
        self._shell_process_group_id = None


class PosixProcessSupervisor:
    """Map domain controls to foreground groups and reap one POSIX session."""

    runtime_name = RuntimeName.POSIX_BASH

    _SIGNAL_NAMES: ClassVar[dict[ControlIntent, str]] = {
        ControlIntent.INTERRUPT: "SIGINT",
        ControlIntent.TERMINATE: "SIGTERM",
        ControlIntent.KILL: "SIGKILL",
    }

    def __init__(self, *, ownership_id_factory: Callable[[], str] | None = None) -> None:
        self._supervisor_token = object()
        self._ownership_id_factory = ownership_id_factory or (
            lambda: f"owner_{secrets.token_hex(16)}"
        )

    def prepare(self) -> ProcessOwnership:
        ownership_id = self._ownership_id_factory()
        if not ownership_id:
            raise ProcessControlError("POSIX ownership id cannot be empty")
        return PosixProcessOwnership(
            ownership_id=ownership_id,
            supervisor_token=self._supervisor_token,
        )

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
    ) -> ControlDelivery:
        concrete = self._ownership(ownership)
        with concrete._lock:
            if concrete._finalized:
                return ControlDelivery(delivered=False)
            process_id = self._require_attached(concrete)
            terminal_fd = concrete._terminal_file_descriptor
            if terminal_fd < 0:
                return ControlDelivery(delivered=False)
            try:
                foreground_group = os.tcgetpgrp(terminal_fd)
            except OSError as error:
                if error.errno in {errno.EBADF, errno.ENOTTY, errno.ENXIO, errno.EIO}:
                    return ControlDelivery(delivered=False)
                raise ProcessControlError(
                    "failed to resolve the foreground process group"
                ) from error
            if foreground_group <= 0:
                return ControlDelivery(delivered=False)
            try:
                records = self._session_processes(process_id, timeout_seconds=1.0)
            except _DeadlineExpired as error:
                raise ProcessControlError(
                    "timed out validating the foreground process group"
                ) from error
            if not any(
                record.process_group_id == foreground_group and not record.is_zombie
                for record in records
            ):
                return ControlDelivery(delivered=False)
            native_signal = getattr(signal, self._SIGNAL_NAMES[intent], None)
            if native_signal is None:
                raise ProcessControlError("POSIX process control is unavailable")
            try:
                os.killpg(foreground_group, native_signal)
            except ProcessLookupError:
                return ControlDelivery(delivered=False)
            except OSError as error:
                raise ProcessControlError("failed to deliver POSIX process control") from error
            return ControlDelivery(delivered=True)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        concrete = self._ownership(ownership)
        with concrete._lock:
            if concrete._finalized:
                return False
            process_id = self._require_attached(concrete)
            try:
                records = self._session_processes(process_id, timeout_seconds=1.0)
            except _DeadlineExpired as error:
                raise ProcessControlError(
                    "timed out checking POSIX process ownership"
                ) from error
            if any(not record.is_zombie for record in records):
                return True
            return not self._finalize_leader(concrete)

    def cleanup_execution(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        """Reap live processes in the Shell session without killing its leader."""

        concrete = self._ownership(ownership)
        if deadline_ms < 0:
            raise ProcessControlError("execution cleanup deadline cannot be negative")
        with concrete._lock:
            if concrete._finalized:
                raise ProcessControlError("cannot clean an execution after Shell cleanup")
            process_id = self._require_attached(concrete)
            deadline = time.monotonic() + deadline_ms / 1000
            records = self._snapshot_before_deadline(process_id, deadline)
            if records is None:
                return CleanupResult(reaped=False, remaining_managed_processes=1)
            outstanding = self._execution_records(records, process_id)
            if not outstanding:
                return CleanupResult(reaped=True, remaining_managed_processes=0)

            if not self._signal_execution_records(
                outstanding,
                concrete._shell_process_group_id,
                signal.SIGCONT,
                deadline,
            ):
                return self._execution_cleanup_result(outstanding)
            if not self._signal_execution_records(
                outstanding,
                concrete._shell_process_group_id,
                signal.SIGTERM,
                deadline,
            ):
                return self._execution_cleanup_result(outstanding)

            term_deadline = time.monotonic() + max(
                0.0,
                (deadline - time.monotonic()) / 2,
            )
            outstanding = self._wait_for_execution(
                process_id,
                term_deadline,
                outstanding,
            )
            if outstanding:
                if not self._signal_execution_records(
                    outstanding,
                    concrete._shell_process_group_id,
                    signal.SIGCONT,
                    deadline,
                ):
                    return self._execution_cleanup_result(outstanding)
                if not self._signal_execution_records(
                    outstanding,
                    concrete._shell_process_group_id,
                    signal.SIGKILL,
                    deadline,
                ):
                    return self._execution_cleanup_result(outstanding)
                outstanding = self._wait_for_execution(
                    process_id,
                    deadline,
                    outstanding,
                )
            return self._execution_cleanup_result(outstanding)

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        concrete = self._ownership(ownership)
        if deadline_ms < 0:
            raise ProcessControlError("cleanup deadline cannot be negative")
        with concrete._lock:
            if concrete._finalized:
                self._close_terminal(concrete)
                return CleanupResult(reaped=True, remaining_managed_processes=0)
            if concrete._process_id is None:
                concrete._mark_finalized()
                self._close_terminal(concrete)
                return CleanupResult(reaped=True, remaining_managed_processes=0)
            process_id = self._require_attached(concrete)
            deadline = time.monotonic() + deadline_ms / 1000
            try:
                records = self._snapshot_before_deadline(process_id, deadline)
            except ProcessControlError:
                self._signal_known_groups(concrete, signal.SIGKILL, deadline)
                raise
            if records is None:
                return CleanupResult(reaped=False, remaining_managed_processes=1)
            if not records:
                if self._finalize_leader(concrete):
                    self._close_terminal(concrete)
                    return CleanupResult(reaped=True, remaining_managed_processes=0)
                return CleanupResult(reaped=False, remaining_managed_processes=1)
            if not self._outstanding_records(records, process_id):
                if self._finalize_leader(concrete):
                    self._close_terminal(concrete)
                    return CleanupResult(reaped=True, remaining_managed_processes=0)
                return CleanupResult(reaped=False, remaining_managed_processes=1)

            groups = self._ordered_groups(records, concrete._shell_process_group_id)
            if not self._signal_groups(groups, signal.SIGCONT, deadline):
                return self._incomplete_result(records, process_id)
            if not self._signal_groups(groups, signal.SIGTERM, deadline):
                return self._incomplete_result(records, process_id)

            remaining = records
            remaining_term_time = max(0.0, (deadline - time.monotonic()) / 2)
            term_deadline = min(deadline, time.monotonic() + remaining_term_time)
            remaining = self._wait_for_session(process_id, term_deadline, remaining)
            if self._outstanding_records(remaining, process_id):
                remaining_groups = self._ordered_groups(
                    remaining,
                    concrete._shell_process_group_id,
                )
                if not self._signal_groups(remaining_groups, signal.SIGCONT, deadline):
                    return self._incomplete_result(remaining, process_id)
                if not self._signal_groups(remaining_groups, signal.SIGKILL, deadline):
                    return self._incomplete_result(remaining, process_id)
                remaining = self._wait_for_session(process_id, deadline, remaining)

            outstanding = self._outstanding_records(remaining, process_id)
            if not outstanding:
                if not self._finalize_leader(concrete):
                    return CleanupResult(reaped=False, remaining_managed_processes=1)
                self._close_terminal(concrete)
                return CleanupResult(reaped=True, remaining_managed_processes=0)
            count = len({record.process_id for record in outstanding})
            return CleanupResult(
                reaped=False,
                remaining_managed_processes=count,
            )

    def _ownership(self, ownership: ProcessOwnership) -> PosixProcessOwnership:
        if not isinstance(ownership, PosixProcessOwnership):
            raise ProcessControlError("ownership was not created by the POSIX supervisor")
        if ownership._supervisor_token is not self._supervisor_token:
            raise ProcessControlError("ownership belongs to a different POSIX supervisor")
        return ownership

    @staticmethod
    def _require_attached(ownership: PosixProcessOwnership) -> int:
        if ownership._process_id is None:
            raise ProcessControlError("POSIX ownership has not been attached")
        return ownership._process_id

    @staticmethod
    def _ordered_groups(
        records: list[_ProcessRecord],
        shell_process_group_id: int | None,
    ) -> list[int]:
        groups = {
            record.process_group_id
            for record in records
            if record.process_group_id > 0 and not record.is_zombie
        }
        return sorted(groups, key=lambda group: group == shell_process_group_id)

    @staticmethod
    def _outstanding_records(
        records: list[_ProcessRecord],
        process_id: int,
    ) -> list[_ProcessRecord]:
        return [
            record
            for record in records
            if record.process_id != process_id or not record.is_zombie
        ]

    @classmethod
    def _incomplete_result(
        cls,
        records: list[_ProcessRecord],
        process_id: int,
    ) -> CleanupResult:
        count = len(
            {record.process_id for record in cls._outstanding_records(records, process_id)}
        )
        return CleanupResult(reaped=False, remaining_managed_processes=max(1, count))

    @staticmethod
    def _signal_groups(
        groups: list[int],
        native_signal: int,
        deadline: float | None = None,
    ) -> bool:
        for process_group in groups:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            try:
                os.killpg(process_group, native_signal)
            except ProcessLookupError:
                continue
            except OSError as error:
                raise ProcessControlError("failed to clean up a POSIX process group") from error
        return True

    def _wait_for_session(
        self,
        session_id: int,
        deadline: float,
        last_records: list[_ProcessRecord],
    ) -> list[_ProcessRecord]:
        records = last_records
        while time.monotonic() < deadline:
            snapshot = self._snapshot_before_deadline(session_id, deadline)
            if snapshot is None:
                return records
            records = snapshot
            if not self._outstanding_records(records, session_id):
                return records
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.02, remaining))
        return records

    def _wait_for_execution(
        self,
        session_id: int,
        deadline: float,
        last_records: list[_ProcessRecord],
    ) -> list[_ProcessRecord]:
        outstanding = last_records
        while time.monotonic() < deadline:
            snapshot = self._snapshot_before_deadline(session_id, deadline)
            if snapshot is None:
                return outstanding
            outstanding = self._execution_records(snapshot, session_id)
            if not outstanding:
                return []
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.02, remaining))
        return outstanding

    @staticmethod
    def _execution_records(
        records: list[_ProcessRecord],
        shell_process_id: int,
    ) -> list[_ProcessRecord]:
        return [
            record
            for record in records
            if record.process_id != shell_process_id and not record.is_zombie
        ]

    @staticmethod
    def _execution_cleanup_result(records: list[_ProcessRecord]) -> CleanupResult:
        count = len({record.process_id for record in records})
        return CleanupResult(
            reaped=count == 0,
            remaining_managed_processes=count,
        )

    @classmethod
    def _signal_execution_records(
        cls,
        records: list[_ProcessRecord],
        shell_process_group_id: int | None,
        native_signal: int,
        deadline: float,
    ) -> bool:
        groups = sorted(
            {
                record.process_group_id
                for record in records
                if record.process_group_id > 0
                and record.process_group_id != shell_process_group_id
            }
        )
        if not cls._signal_groups(groups, native_signal, deadline):
            return False
        grouped = set(groups)
        for record in records:
            if record.process_group_id in grouped:
                continue
            if time.monotonic() >= deadline:
                return False
            try:
                os.kill(record.process_id, native_signal)
            except ProcessLookupError:
                continue
            except OSError as error:
                raise ProcessControlError(
                    "failed to clean up a POSIX execution process"
                ) from error
        return True

    def _snapshot_before_deadline(
        self,
        session_id: int,
        deadline: float,
    ) -> list[_ProcessRecord] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return self._session_processes_until(session_id, deadline)
        except _DeadlineExpired:
            return None

    @staticmethod
    def _signal_known_groups(
        ownership: PosixProcessOwnership,
        native_signal: int,
        deadline: float,
    ) -> bool:
        groups: set[int] = set()
        if ownership._terminal_file_descriptor >= 0:
            try:
                foreground_group = os.tcgetpgrp(ownership._terminal_file_descriptor)
            except OSError:
                foreground_group = -1
            if foreground_group > 0:
                groups.add(foreground_group)
        if ownership._shell_process_group_id is not None:
            groups.add(ownership._shell_process_group_id)
        return PosixProcessSupervisor._signal_groups(
            sorted(groups),
            native_signal,
            deadline,
        )

    @staticmethod
    def _session_processes(
        session_id: int,
        *,
        timeout_seconds: float,
    ) -> list[_ProcessRecord]:
        return PosixProcessSupervisor._session_processes_until(
            session_id,
            time.monotonic() + timeout_seconds,
        )

    @staticmethod
    def _session_processes_until(
        session_id: int,
        deadline: float,
    ) -> list[_ProcessRecord]:
        try:
            result = subprocess.run(
                ("ps", "-axo", "pid=,ppid=,pgid=,stat="),
                check=True,
                capture_output=True,
                text=True,
                timeout=max(deadline - time.monotonic(), 0.001),
            )
        except subprocess.TimeoutExpired as error:
            raise _DeadlineExpired from error
        except (OSError, subprocess.SubprocessError) as error:
            raise ProcessControlError("failed to enumerate the POSIX process session") from error

        if time.monotonic() >= deadline:
            raise _DeadlineExpired
        records: list[_ProcessRecord] = []
        for line in result.stdout.splitlines():
            if time.monotonic() >= deadline:
                raise _DeadlineExpired
            fields = line.split(None, 3)
            if len(fields) != 4:
                continue
            try:
                process_id, _parent_id, process_group_id = map(int, fields[:3])
            except ValueError as error:
                raise ProcessControlError("ps returned an invalid POSIX process record") from error
            try:
                native_session_id = os.getsid(process_id)
            except ProcessLookupError:
                continue
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EPERM}:
                    continue
                raise ProcessControlError(
                    "failed to inspect an enumerated POSIX process"
                ) from error
            if time.monotonic() >= deadline:
                raise _DeadlineExpired
            if native_session_id == session_id:
                records.append(
                    _ProcessRecord(
                        process_id=process_id,
                        process_group_id=process_group_id,
                        session_id=native_session_id,
                        state=fields[3],
                    )
                )
        return records

    @staticmethod
    def _finalize_leader(ownership: PosixProcessOwnership) -> bool:
        if ownership._process_id is None:
            return ownership._finalized
        try:
            waited, _ = os.waitpid(ownership._process_id, os.WNOHANG)
        except ChildProcessError:
            ownership._mark_finalized()
            return True
        except OSError as error:
            raise ProcessControlError("failed to reap the POSIX session leader") from error
        if waited not in {0, ownership._process_id}:
            raise ProcessControlError("waitpid returned an unexpected POSIX process")
        if waited == 0:
            return False
        ownership._mark_finalized()
        return True

    @staticmethod
    def _close_terminal(ownership: PosixProcessOwnership) -> None:
        if ownership._terminal_file_descriptor < 0:
            return
        try:
            os.close(ownership._terminal_file_descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise ProcessControlError(
                    "failed to close the POSIX ownership terminal"
                ) from error
        ownership._terminal_file_descriptor = -1
