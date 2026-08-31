"""Zsh dialect using the shared POSIX-shell framing engine."""

from __future__ import annotations

from collections.abc import Callable

from tfbash_mcp.runtime.bash import BashDialect
from tfbash_mcp.runtime.contracts import DialectName


class ZshDialect(BashDialect):
    """Launch zsh without user or global startup files and with private framing."""

    dialect_name = DialectName.ZSH
    default_executable = "/bin/zsh"

    def __init__(
        self,
        *,
        token_factory: Callable[[], str] | None = None,
        max_control_bytes: int = 65_536,
        default_executable: str = "/bin/zsh",
        windows_paths: bool = False,
    ) -> None:
        super().__init__(
            token_factory=token_factory,
            max_control_bytes=max_control_bytes,
            dialect_name=DialectName.ZSH,
            default_executable=default_executable,
            launch_arguments=("-f", "-d", "-i"),
            version_variable="ZSH_VERSION",
            windows_paths=windows_paths,
            shell_prelude=(
                "unsetopt ZLE PROMPT_CR PROMPT_SP 2>/dev/null || :; stty -echo 2>/dev/null || :; "
            ),
        )
