"""Platform-neutral Runtime Ports used by the Shell worker.

These contracts deliberately contain no MCP response types or platform-native
process/terminal identifiers.  A ShellWorker is the sole caller for one
``RuntimeSession`` and therefore the sole reader of its PTY.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class RuntimeName(str, Enum):
    POSIX_BASH = "posix-bash"
    WINDOWS_PWSH = "windows-pwsh"


class DialectName(str, Enum):
    BASH = "bash"
    PWSH = "pwsh"


class ControlIntent(str, Enum):
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"
    KILL = "kill"


class ReadStatus(str, Enum):
    DATA = "data"
    WOULD_BLOCK = "would_block"
    EOF = "eof"


class WaitInterest(str, Enum):
    READABLE = "readable"
    WRITABLE = "writable"
    PROCESS_EXIT = "process_exit"


class DialectEventKind(str, Enum):
    BOOTSTRAP_REQUIRED = "bootstrap_required"
    OUTPUT = "output"
    READY = "ready"
    RECOVERED = "recovered"
    COMMAND_COMPLETE = "command_complete"


@dataclass(frozen=True, slots=True)
class ShellStartRequest:
    """Resolved per-shell settings; HostConfig itself never crosses a port."""

    executable: str
    cwd: str
    environment: Mapping[str, str]
    startup_command: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    executable: str
    arguments: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True, slots=True)
class DialectLaunch:
    spawn: SpawnRequest
    initial_input: bytes


@dataclass(frozen=True, slots=True)
class DialectSessionPlan:
    """A launch and its private framing state, created as one atomic pair."""

    launch: DialectLaunch
    protocol: DialectProtocol


@dataclass(frozen=True, slots=True)
class CommandFrame:
    correlation_id: str
    input_bytes: bytes


@dataclass(frozen=True, slots=True)
class DialectEvent:
    kind: DialectEventKind
    data: bytes = b""
    correlation_id: str | None = None
    exit_code: int | None = None
    cwd: str | None = None
    shell_version: str | None = None

    def __post_init__(self) -> None:
        if self.kind is DialectEventKind.OUTPUT:
            if not self.data or any(
                value is not None
                for value in (
                    self.correlation_id,
                    self.exit_code,
                    self.cwd,
                    self.shell_version,
                )
            ):
                raise ValueError("output events contain only non-empty data")
            return
        if self.data:
            raise ValueError("control events cannot contain output data")
        if self.kind is DialectEventKind.BOOTSTRAP_REQUIRED:
            if any(
                value is not None
                for value in (
                    self.correlation_id,
                    self.exit_code,
                    self.cwd,
                    self.shell_version,
                )
            ):
                raise ValueError("bootstrap events cannot contain result fields")
            return
        if self.kind is DialectEventKind.READY:
            if self.correlation_id is not None or self.exit_code is not None:
                raise ValueError("ready events contain only the confirmed cwd")
            if self.cwd is None or self.shell_version is None:
                raise ValueError("ready events require cwd and shell_version")
            return
        if self.kind is DialectEventKind.RECOVERED:
            if self.correlation_id is None or self.cwd is None:
                raise ValueError("recovered events require correlation_id and cwd")
            if self.exit_code is not None or self.shell_version is not None:
                raise ValueError("recovered events cannot contain exit_code or shell_version")
            return
        if self.shell_version is not None:
            raise ValueError("command completion cannot contain shell_version")
        if self.correlation_id is None or self.exit_code is None:
            raise ValueError("command completion requires correlation_id and exit_code")


@dataclass(frozen=True, slots=True)
class TransportRead:
    status: ReadStatus
    data: bytes = b""

    def __post_init__(self) -> None:
        if (self.status is ReadStatus.DATA) != bool(self.data):
            raise ValueError("only DATA reads may contain non-empty bytes")


@dataclass(frozen=True, slots=True)
class TransportWrite:
    bytes_written: int
    would_block: bool

    def __post_init__(self) -> None:
        if self.bytes_written < 0:
            raise ValueError("bytes_written cannot be negative")


@dataclass(frozen=True, slots=True)
class ControlDelivery:
    delivered: bool


@dataclass(frozen=True, slots=True)
class CleanupResult:
    reaped: bool
    remaining_managed_processes: int

    def __post_init__(self) -> None:
        if self.remaining_managed_processes < 0:
            raise ValueError("remaining_managed_processes cannot be negative")
        if self.reaped != (self.remaining_managed_processes == 0):
            raise ValueError("reaped must agree with the remaining process count")


@runtime_checkable
class RuntimeSession(Protocol):
    """Opaque session identity shared by transport and supervisor.

    Concrete adapters keep fd/HANDLE/PID/process-group/Job Object details private.
    """

    @property
    def session_id(self) -> str: ...


@runtime_checkable
class ProcessOwnership(Protocol):
    """Opaque process ownership prepared before a child can be spawned."""

    @property
    def ownership_id(self) -> str: ...


class DialectProtocol(Protocol):
    """Stateful framing parser owned by the same worker as the PTY."""

    def wrap_command(self, command: str) -> CommandFrame: ...

    def recovery_input(self) -> bytes: ...

    def feed(self, data: bytes) -> tuple[DialectEvent, ...]: ...

    def end_of_stream(self) -> tuple[DialectEvent, ...]: ...


class ShellDialect(Protocol):
    """Creates shell framing state; never spawns or reads a PTY."""

    @property
    def runtime_name(self) -> RuntimeName: ...

    @property
    def dialect_name(self) -> DialectName: ...

    @property
    def default_executable(self) -> str: ...

    def prepare_session(self, request: ShellStartRequest) -> DialectSessionPlan: ...


class PtyTransport(Protocol):
    """Owns PTY I/O and handle closure, but not framing or Execution state."""

    @property
    def runtime_name(self) -> RuntimeName: ...

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
    ) -> RuntimeSession:
        """Spawn inside a prepared ownership boundary.

        On failure, the implementation closes every PTY resource it allocated;
        the composition helper separately asks the supervisor to reap ownership.
        """
        ...

    def read(self, session: RuntimeSession, max_bytes: int) -> TransportRead: ...

    def write(self, session: RuntimeSession, data: memoryview) -> TransportWrite: ...

    def wait(
        self,
        session: RuntimeSession,
        interests: frozenset[WaitInterest],
        timeout_ms: int,
    ) -> frozenset[WaitInterest]: ...

    def close(self, session: RuntimeSession) -> None: ...


class ProcessSupervisor(Protocol):
    """Owns semantic control and managed-descendant cleanup.

    ``delivered`` reports only control delivery.  Execution completion remains a
    domain decision after the worker observes output/process state.
    """

    @property
    def runtime_name(self) -> RuntimeName: ...

    def prepare(self) -> ProcessOwnership:
        """Create an active, empty ownership boundary before process creation.

        This operation has a strong failure guarantee: if it raises, it has
        released every resource allocated by that attempt and has left no active
        ownership for the caller to clean up.
        """
        ...

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
    ) -> ControlDelivery: ...

    def is_alive(self, ownership: ProcessOwnership) -> bool: ...

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult: ...
