"""Validated all-or-nothing Runtime Profile composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tfbash_mcp.runtime.contracts import (
    DialectName,
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
    RuntimeName.POSIX_BASH: (RuntimePlatform.POSIX, DialectName.BASH),
    RuntimeName.WINDOWS_PWSH: (RuntimePlatform.WINDOWS, DialectName.PWSH),
}


@dataclass(frozen=True, slots=True)
class ManagedRuntimeSession:
    session: RuntimeSession
    ownership: ProcessOwnership


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    name: RuntimeName
    platform: RuntimePlatform
    dialect: ShellDialect
    transport: PtyTransport
    supervisor: ProcessSupervisor

    def __post_init__(self) -> None:
        expected_platform, expected_dialect = _EXPECTED_IDENTITY[self.name]
        if self.platform is not expected_platform:
            raise RuntimeConfigurationError(
                f"{self.name.value} requires platform {expected_platform.value}"
            )
        components = (self.dialect, self.transport, self.supervisor)
        if any(component.runtime_name is not self.name for component in components):
            raise RuntimeConfigurationError("runtime components from different profiles cannot mix")
        if self.dialect.dialect_name is not expected_dialect:
            raise RuntimeConfigurationError(
                f"{self.name.value} requires dialect {expected_dialect.value}"
            )

    def open_session(
        self,
        request: SpawnRequest,
        *,
        cleanup_deadline_ms: int,
    ) -> ManagedRuntimeSession:
        """Spawn under pre-established ownership and roll back on failure."""

        try:
            ownership = self.supervisor.prepare()
        except RuntimeBoundaryError:
            raise
        except Exception as prepare_error:
            raise ProcessControlError("failed to prepare process ownership") from prepare_error
        try:
            session = self.transport.spawn(request, ownership)
        except Exception as spawn_error:
            try:
                cleanup = self.supervisor.cleanup(
                    ownership,
                    deadline_ms=cleanup_deadline_ms,
                )
            except RuntimeBoundaryError:
                raise
            except Exception as cleanup_error:
                raise ProcessControlError(
                    "spawn failed and ownership cleanup raised an unexpected error"
                ) from cleanup_error
            if not cleanup.reaped:
                raise CleanupTimeout(
                    "spawn failed and ownership cleanup timed out"
                ) from spawn_error
            if isinstance(spawn_error, RuntimeBoundaryError):
                raise
            raise TransportError("PTY spawn failed") from spawn_error
        return ManagedRuntimeSession(session=session, ownership=ownership)


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
