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
    ExecutionSnapshot,
    ExecutionState,
    ShellSnapshot,
    ShellState,
    SystemClock,
)
from tfbash_mcp.domain.output import OutputSlice, Utf8OutputBuffer
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
    "ExecutionSnapshot",
    "ExecutionState",
    "IdFactory",
    "InvalidCursor",
    "InvalidTransition",
    "ManagerConfig",
    "OutputSlice",
    "ShellBusy",
    "ShellClosing",
    "ShellNotFound",
    "ShellRegistry",
    "ShellSnapshot",
    "ShellState",
    "ShellUnavailable",
    "ShellWorker",
    "SystemClock",
    "Utf8OutputBuffer",
    "WorkerConfig",
]
