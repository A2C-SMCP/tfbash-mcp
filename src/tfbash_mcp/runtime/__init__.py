"""Platform-neutral Runtime Ports and process-level composition."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from tfbash_mcp.runtime.bash import BashDialect, BashProtocol
from tfbash_mcp.runtime.config import (
    STARTUP_COMMAND_UNSET,
    AgentContext,
    AgentHostContext,
    AgentRuntimeContext,
    EnvironmentKind,
    EnvironmentSummary,
    HostConfig,
    HostProfile,
    NativePlatform,
    RuntimeBuilders,
    RuntimeComposition,
    RuntimeSelection,
    ShellOpenOverrides,
    compose_runtime,
    create_host_config,
    resolve_runtime,
)
from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
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
from tfbash_mcp.runtime.powershell import PowerShellDialect, PowerShellProtocol
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
    from tfbash_mcp.runtime.windows_conpty import (
        ConPtyLike,
        ConPtySession,
        ConPtyTransport,
        WindowsSpawnOwnership,
    )

_LAZY_POSIX_EXPORTS = {
    "PexpectPosixPtyTransport": "tfbash_mcp.runtime.posix_pty",
    "PexpectPosixSession": "tfbash_mcp.runtime.posix_pty",
    "PosixSpawnOwnership": "tfbash_mcp.runtime.posix_pty",
    "PosixProcessOwnership": "tfbash_mcp.runtime.posix_process",
    "PosixProcessSupervisor": "tfbash_mcp.runtime.posix_process",
}

_LAZY_WINDOWS_EXPORTS = {
    "ConPtyLike": "tfbash_mcp.runtime.windows_conpty",
    "ConPtySession": "tfbash_mcp.runtime.windows_conpty",
    "ConPtyTransport": "tfbash_mcp.runtime.windows_conpty",
    "WindowsSpawnOwnership": "tfbash_mcp.runtime.windows_conpty",
}


def __getattr__(name: str) -> Any:
    module_name = (_LAZY_POSIX_EXPORTS | _LAZY_WINDOWS_EXPORTS).get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "STARTUP_COMMAND_UNSET",
    "AgentContext",
    "AgentHostContext",
    "AgentRuntimeContext",
    "BashDialect",
    "BashProtocol",
    "CancellationSignal",
    "CleanupResult",
    "CleanupTimeout",
    "CommandFrame",
    "ConPtyLike",
    "ConPtySession",
    "ConPtyTransport",
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
    "NativePlatform",
    "PexpectPosixPtyTransport",
    "PexpectPosixSession",
    "PosixBashProfile",
    "PosixProcessOwnership",
    "PosixProcessSupervisor",
    "PosixSpawnOwnership",
    "PowerShellDialect",
    "PowerShellProtocol",
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
    "ShellOpenOverrides",
    "ShellStartRequest",
    "SpawnRequest",
    "TransportClosed",
    "TransportError",
    "TransportRead",
    "TransportWrite",
    "UnsupportedShell",
    "WaitInterest",
    "WindowsPwshProfile",
    "WindowsSpawnOwnership",
    "compose_runtime",
    "create_host_config",
    "resolve_runtime",
]
