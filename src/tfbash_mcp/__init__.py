"""A general-purpose Bash MCP server for agent systems."""

from typing import TYPE_CHECKING

__version__ = "0.2.0"

if TYPE_CHECKING:
    from tfbash_mcp.embedded import EmbeddedShellConfig, EmbeddedShellRuntime
    from tfbash_mcp.mcp_adapter import ToolConcurrencyBudget, ToolConcurrencyLimits

__all__ = [
    "EmbeddedShellConfig",
    "EmbeddedShellRuntime",
    "ToolConcurrencyBudget",
    "ToolConcurrencyLimits",
    "__version__",
]


def __getattr__(name: str) -> object:
    """Load the embedded API lazily so runtime contracts remain platform-neutral."""

    if name in {"EmbeddedShellConfig", "EmbeddedShellRuntime"}:
        from tfbash_mcp import embedded

        return getattr(embedded, name)
    if name in {"ToolConcurrencyBudget", "ToolConcurrencyLimits"}:
        from tfbash_mcp import mcp_adapter

        return getattr(mcp_adapter, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
