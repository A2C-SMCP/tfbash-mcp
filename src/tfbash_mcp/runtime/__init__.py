"""Platform-neutral Runtime Ports and process-level composition."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from tfbash_mcp.runtime.bash import BashDialect, BashProtocol
from tfbash_mcp.runtime.config import (
    EnvironmentKind,
    EnvironmentSummary,
    HostConfig,
    HostProfile,
    RuntimeBuilders,
    RuntimeComposition,
    RuntimeSelection,
    compose_runtime,
    create_host_config,
    resolve_runtime,
)
from tfbash_mcp.runtime.contracts import (
    CleanupResult,
    CommandFrame,
    ControlDelivery,
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    DialectLaunch,
    DialectName,
    DialectProtocol,
    DialectSessionPlan,
    ProcessOwnership,
    ProcessSupervisor,
    PtyTransport,
    ReadStatus,
    RuntimeName,
    RuntimeSession,
    ShellDialect,
    ShellStartRequest,
    SpawnRequest,
    TransportRead,
    TransportWrite,
    WaitInterest,
)
from tfbash_mcp.runtime.errors import (
    CleanupTimeout,
    DialectProtocolError,
    ProcessControlError,
    RuntimeBoundaryError,
    RuntimeConfigurationError,
    TransportClosed,
    TransportError,
    UnsupportedShell,
)
from tfbash_mcp.runtime.profile import (
    ManagedRuntimeSession,
    PosixBashProfile,
    RuntimePlatform,
    RuntimeProfile,
    WindowsPwshProfile,
)

if TYPE_CHECKING:
    from tfbash_mcp.runtime.posix_process import (
        PosixProcessOwnership,
        PosixProcessSupervisor,
    )
    from tfbash_mcp.runtime.posix_pty import (
        PexpectPosixPtyTransport,
        PexpectPosixSession,
        PosixSpawnOwnership,
    )

_LAZY_POSIX_EXPORTS = {
    "PexpectPosixPtyTransport": "tfbash_mcp.runtime.posix_pty",
    "PexpectPosixSession": "tfbash_mcp.runtime.posix_pty",
    "PosixSpawnOwnership": "tfbash_mcp.runtime.posix_pty",
    "PosixProcessOwnership": "tfbash_mcp.runtime.posix_process",
    "PosixProcessSupervisor": "tfbash_mcp.runtime.posix_process",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_POSIX_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value

__all__ = [
    "BashDialect",
    "BashProtocol",
    "CleanupResult",
    "CleanupTimeout",
    "CommandFrame",
    "ControlDelivery",
    "ControlIntent",
    "DialectEvent",
    "DialectEventKind",
    "DialectLaunch",
    "DialectName",
    "DialectProtocol",
    "DialectProtocolError",
    "DialectSessionPlan",
    "EnvironmentKind",
    "EnvironmentSummary",
    "HostConfig",
    "HostProfile",
    "ManagedRuntimeSession",
    "PexpectPosixPtyTransport",
    "PexpectPosixSession",
    "PosixBashProfile",
    "PosixProcessOwnership",
    "PosixProcessSupervisor",
    "PosixSpawnOwnership",
    "ProcessControlError",
    "ProcessOwnership",
    "ProcessSupervisor",
    "PtyTransport",
    "ReadStatus",
    "RuntimeBoundaryError",
    "RuntimeBuilders",
    "RuntimeComposition",
    "RuntimeConfigurationError",
    "RuntimeName",
    "RuntimePlatform",
    "RuntimeProfile",
    "RuntimeSelection",
    "RuntimeSession",
    "ShellDialect",
    "ShellStartRequest",
    "SpawnRequest",
    "TransportClosed",
    "TransportError",
    "TransportRead",
    "TransportWrite",
    "UnsupportedShell",
    "WaitInterest",
    "WindowsPwshProfile",
    "compose_runtime",
    "create_host_config",
    "resolve_runtime",
]
