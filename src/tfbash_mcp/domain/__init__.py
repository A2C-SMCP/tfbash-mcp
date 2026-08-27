"""Platform-independent Shell Domain."""

from tfbash_mcp.domain.errors import (
    CapacityExceeded,
    DomainError,
    ExecutionNotActive,
    ExecutionNotFound,
    InvalidCursor,
    InvalidTransition,
    ShellBusy,
    ShellClosing,
    ShellNotFound,
    ShellUnavailable,
)
from tfbash_mcp.domain.manager import CommandShellManager, ManagerConfig
from tfbash_mcp.domain.models import (
    ChangeSignal,
    Clock,
    CommandShell,
    Execution,
    ExecutionOverviewSnapshot,
    ExecutionSnapshot,
    ExecutionState,
    ShellOverviewSnapshot,
    ShellSnapshot,
    ShellState,
    SystemClock,
)
from tfbash_mcp.domain.output import OutputSlice, OutputTail, Utf8OutputBuffer
from tfbash_mcp.domain.registry import IdFactory, ShellRegistry
from tfbash_mcp.domain.worker import ShellWorker, WorkerConfig

__all__ = [
    "CapacityExceeded",
    "ChangeSignal",
    "Clock",
    "CommandShell",
    "CommandShellManager",
    "DomainError",
    "Execution",
    "ExecutionNotActive",
    "ExecutionNotFound",
    "ExecutionOverviewSnapshot",
    "ExecutionSnapshot",
    "ExecutionState",
    "IdFactory",
    "InvalidCursor",
    "InvalidTransition",
    "ManagerConfig",
    "OutputSlice",
    "OutputTail",
    "ShellBusy",
    "ShellClosing",
    "ShellNotFound",
    "ShellOverviewSnapshot",
    "ShellRegistry",
    "ShellSnapshot",
    "ShellState",
    "ShellUnavailable",
    "ShellWorker",
    "SystemClock",
    "Utf8OutputBuffer",
    "WorkerConfig",
]
