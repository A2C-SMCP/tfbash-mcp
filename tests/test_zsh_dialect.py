from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest

from tfbash_mcp.runtime import DialectName, ShellStartRequest, ZshDialect
from tfbash_mcp.server import build_parser, build_service


def test_zsh_launch_disables_startup_files_and_prompt_hooks() -> None:
    dialect = ZshDialect(token_factory=lambda: "a" * 32)
    plan = dialect.prepare_session(
        ShellStartRequest(
            executable="/bin/zsh",
            cwd="/workspace",
            environment={"PATH": "/usr/bin:/bin"},
            startup_command=None,
        )
    )

    assert dialect.dialect_name is DialectName.ZSH
    assert plan.launch.spawn.arguments == ("-f", "-d", "-i")
    assert b"unsetopt ZLE PROMPT_CR PROMPT_SP" in plan.launch.initial_input
    assert b"${ZSH_VERSION:-}" in plan.launch.initial_input


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")
def test_real_zsh_preserves_unicode_multiline_cwd_and_exit_status() -> None:
    service = build_service(
        build_parser().parse_args(["--runtime-profile", "zsh", "--close-timeout-ms", "5000"]),
        process_cwd=str(Path.cwd()),
    )
    try:
        opened = service.call("shell_open", {})
        assert not opened.isError
        assert opened.structuredContent is not None
        shell_id = cast(str, opened.structuredContent["shell_id"])
        result = service.call(
            "shell_exec",
            {
                "shell_id": shell_id,
                "command": "printf '第一行\\n第二行🙂\\n'; (exit 37)",
                "yield_ms": 10_000,
                "timeout_ms": 5_000,
                "max_output_bytes": 4_194_304,
            },
        )
        assert result.structuredContent is not None
        payload = result.structuredContent
        assert payload["output"] == "第一行\r\n第二行🙂\r\n"
        assert payload["exit_code"] == 37
        assert payload["cwd"] == str(Path.cwd())
    finally:
        service.shutdown()
