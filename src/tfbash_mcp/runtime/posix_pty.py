"""Nonblocking POSIX PTY transport built on pexpect's proven spawn path.

The spawn configuration is adapted from ide4ai's PexpectTerminalEnv at commit
20ece038e66e13885e77503e217b23766e60dc86.  See NOTICE.  Unlike that source,
this adapter never calls ``expect()`` and never interprets shell output.
"""

from __future__ import annotations

import errno
import os
import select
import time
from threading import Lock
from typing import Any, Protocol, runtime_checkable

import pexpect  # type: ignore[import-untyped]

from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
    ProcessOwnership,
    ReadStatus,
    RuntimeName,
    RuntimeSession,
    SpawnRequest,
    TransportRead,
    TransportWrite,
    WaitInterest,
)
from tfbash_mcp.runtime.errors import TransportClosed, TransportError


@runtime_checkable
class PosixSpawnOwnership(ProcessOwnership, Protocol):
    """POSIX-only hook implemented by the process supervisor in #7."""

    def reserve(self) -> None:
        """Atomically consume this ownership before the transport forks."""
        ...

    def child_setup(self) -> None:
        """Establish child-side ownership before exec; must only use async-safe OS calls."""
        ...

    def attach(self, process_id: int, terminal_file_descriptor: int) -> None:
        """Record the exec'd leader before any later transport operation.

        ``terminal_file_descriptor`` is borrowed for this call only; an owner
        that needs foreground-group observation must duplicate it before return.
        Before returning or raising, this method must either make the process
        reachable by supervisor cleanup or terminate and reap it itself.
        """
        ...


class PexpectPosixSession(RuntimeSession):
    """Opaque session; platform identifiers remain private to runtime adapters."""

    def __init__(
        self,
        *,
        session_id: str,
        file_descriptor: int,
        transport_token: object,
    ) -> None:
        self._session_id = session_id
        self._transport_token = transport_token
        self._file_descriptor = file_descriptor
        self._closed = False
        self._close_lock = Lock()
        self._read_guard = Lock()

    @property
    def session_id(self) -> str:
        return self._session_id


class PexpectPosixPtyTransport:
    """The sole nonblocking byte reader/writer for each pexpect PTY."""

    runtime_name = RuntimeName.POSIX_BASH

    def __init__(self) -> None:
        self._transport_token = object()
        self._pending_cleanup_lock = Lock()
        self._pending_file_descriptors: list[int] = []
        self._pending_pexpect_children: list[Any] = []

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> RuntimeSession:
        if not isinstance(ownership, PosixSpawnOwnership):
            raise TransportError("POSIX transport requires prepared POSIX ownership")
        self.retry_failed_spawn_cleanup()
        child: Any | None = None
        transport_fd = -1
        deadline = None if deadline_ms is None else time.monotonic() + deadline_ms / 1000
        try:
            if deadline_ms is not None and deadline_ms <= 0:
                raise TransportError("POSIX PTY spawn deadline expired")
            if cancel_signal is not None and cancel_signal.is_set():
                raise TransportError("POSIX PTY spawn was cancelled")
            ownership.reserve()
            child = pexpect.spawn(
                request.executable,
                list(request.arguments),
                timeout=0,
                maxread=65_536,
                cwd=request.cwd,
                env=dict(request.environment),
                echo=False,
                encoding=None,
                use_poll=True,
                preexec_fn=ownership.child_setup,
            )
            ownership.attach(int(child.pid), int(child.child_fd))
            if cancel_signal is not None and cancel_signal.is_set():
                raise TransportError("POSIX PTY spawn was cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportError("POSIX PTY spawn deadline expired")
            transport_fd = os.dup(child.child_fd)
            os.set_blocking(transport_fd, False)
            self._release_pexpect_master(child)
            return PexpectPosixSession(
                session_id=f"posix_{ownership.ownership_id}",
                file_descriptor=transport_fd,
                transport_token=self._transport_token,
            )
        except Exception as error:
            rollback_errors = self._queue_failed_spawn_cleanup(transport_fd, child)
            if rollback_errors:
                # A failed close retains ownership in the transport.  Retry once
                # immediately so transient failures do not leak until a later call.
                rollback_errors = self._drain_failed_spawn_cleanup()
            if rollback_errors:
                details = "; ".join(str(item) for item in rollback_errors)
                raise TransportError(
                    f"failed to spawn POSIX PTY and rollback failed: {details}"
                ) from error
            if isinstance(error, TransportError):
                raise
            raise TransportError("failed to spawn POSIX PTY") from error

    def retry_failed_spawn_cleanup(self) -> None:
        """Retry resources retained after a failed spawn rollback.

        This adapter-specific recovery hook is deterministic: resources remain
        reachable from the transport until a close succeeds.  Normal spawn also
        calls it before allocating anything new.
        """

        errors = self._drain_failed_spawn_cleanup()
        if errors:
            details = "; ".join(str(item) for item in errors)
            raise TransportError(f"failed to clean up an earlier POSIX spawn: {details}")

    def read(self, session: RuntimeSession, max_bytes: int) -> TransportRead:
        concrete = self._session(session)
        if max_bytes <= 0:
            raise TransportError("max_bytes must be positive")
        if not concrete._read_guard.acquire(blocking=False):
            raise TransportError("concurrent PTY readers violate the single-owner contract")
        try:
            self._require_open(concrete)
            try:
                data = os.read(concrete._file_descriptor, max_bytes)
            except BlockingIOError:
                return TransportRead(ReadStatus.WOULD_BLOCK)
            except OSError as error:
                if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    return TransportRead(ReadStatus.WOULD_BLOCK)
                if error.errno in {errno.EIO, errno.ENXIO}:
                    return TransportRead(ReadStatus.EOF)
                if error.errno == errno.EBADF:
                    raise TransportClosed("POSIX PTY is closed") from error
                raise TransportError("failed to read POSIX PTY") from error
            if not data:
                return TransportRead(ReadStatus.EOF)
            return TransportRead(ReadStatus.DATA, data)
        finally:
            concrete._read_guard.release()

    def write(self, session: RuntimeSession, data: memoryview) -> TransportWrite:
        concrete = self._session(session)
        self._require_open(concrete)
        if not data:
            return TransportWrite(bytes_written=0, would_block=False)
        try:
            written = os.write(concrete._file_descriptor, data)
        except BlockingIOError:
            return TransportWrite(bytes_written=0, would_block=True)
        except OSError as error:
            if error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return TransportWrite(bytes_written=0, would_block=True)
            if error.errno in {errno.EBADF, errno.EIO, errno.ENXIO, errno.EPIPE}:
                raise TransportClosed("POSIX PTY no longer accepts input") from error
            raise TransportError("failed to write POSIX PTY") from error
        return TransportWrite(bytes_written=written, would_block=written < len(data))

    def wait(
        self,
        session: RuntimeSession,
        interests: frozenset[WaitInterest],
        timeout_ms: int,
    ) -> frozenset[WaitInterest]:
        concrete = self._session(session)
        self._require_open(concrete)
        if timeout_ms < 0:
            raise TransportError("timeout_ms cannot be negative")
        if not interests:
            return frozenset()
        mask = 0
        if WaitInterest.READABLE in interests or WaitInterest.PROCESS_EXIT in interests:
            mask |= select.POLLIN | select.POLLHUP | select.POLLERR
        if WaitInterest.WRITABLE in interests:
            mask |= select.POLLOUT
        try:
            poller = select.poll()
            poller.register(concrete._file_descriptor, mask)
            deadline = time.monotonic_ns() // 1_000_000 + timeout_ms
            while True:
                remaining = max(0, deadline - time.monotonic_ns() // 1_000_000)
                try:
                    polled = poller.poll(remaining)
                    break
                except InterruptedError:
                    if remaining == 0:
                        return frozenset()
        except (OSError, ValueError) as error:
            if isinstance(error, OSError) and error.errno in {errno.EBADF, errno.ENXIO}:
                raise TransportClosed("POSIX PTY is closed") from error
            raise TransportError("failed to wait for POSIX PTY readiness") from error
        ready: set[WaitInterest] = set()
        for _, event_mask in polled:
            if event_mask & select.POLLNVAL:
                raise TransportClosed("POSIX poll reported an invalid PTY descriptor")
            if event_mask & select.POLLIN and WaitInterest.READABLE in interests:
                ready.add(WaitInterest.READABLE)
            if event_mask & select.POLLOUT and WaitInterest.WRITABLE in interests:
                ready.add(WaitInterest.WRITABLE)
            if event_mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                if WaitInterest.PROCESS_EXIT in interests:
                    ready.add(WaitInterest.PROCESS_EXIT)
                if WaitInterest.READABLE in interests:
                    ready.add(WaitInterest.READABLE)
        return frozenset(ready)

    def close(
        self,
        session: RuntimeSession,
        *,
        deadline_ms: int | None = None,
    ) -> None:
        concrete = self._session(session)
        with concrete._close_lock:
            if concrete._closed:
                return
            try:
                self._close_native(concrete)
            except OSError as error:
                raise TransportError("failed to close POSIX PTY") from error

    def _session(self, session: RuntimeSession) -> PexpectPosixSession:
        if not isinstance(session, PexpectPosixSession):
            raise TransportError("session was not created by the POSIX transport")
        if session._transport_token is not self._transport_token:
            raise TransportError("session belongs to a different POSIX transport")
        return session

    @staticmethod
    def _require_open(session: PexpectPosixSession) -> None:
        if session._closed:
            raise TransportClosed("POSIX PTY is closed")

    @staticmethod
    def _close_native(session: PexpectPosixSession) -> None:
        try:
            os.close(session._file_descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        session._closed = True
        session._file_descriptor = -1

    def _queue_failed_spawn_cleanup(
        self,
        file_descriptor: int,
        child: Any | None,
    ) -> list[Exception]:
        with self._pending_cleanup_lock:
            if file_descriptor >= 0:
                self._pending_file_descriptors.append(file_descriptor)
            if child is not None and not child.closed:
                self._pending_pexpect_children.append(child)
            return self._drain_failed_spawn_cleanup_locked()

    def _drain_failed_spawn_cleanup(self) -> list[Exception]:
        with self._pending_cleanup_lock:
            return self._drain_failed_spawn_cleanup_locked()

    def _drain_failed_spawn_cleanup_locked(self) -> list[Exception]:
        errors: list[Exception] = []
        remaining_descriptors: list[int] = []
        for file_descriptor in self._pending_file_descriptors:
            try:
                os.close(file_descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    remaining_descriptors.append(file_descriptor)
                    errors.append(error)
        self._pending_file_descriptors = remaining_descriptors

        remaining_children: list[Any] = []
        for child in self._pending_pexpect_children:
            try:
                self._release_pexpect_master(child)
            except Exception as error:
                remaining_children.append(child)
                errors.append(error)
        self._pending_pexpect_children = remaining_children
        return errors

    @staticmethod
    def _release_pexpect_master(child: Any) -> None:
        file_object = child.ptyproc.fileobj
        try:
            file_object.close()
        except Exception:
            if not file_object.closed:
                raise
        if file_object.closed:
            child.child_fd = -1
            child.closed = True
            child.ptyproc.fd = -1
            child.ptyproc.closed = True
