from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.parametrize(
    ("server_arguments", "expected_mode"),
    [
        ([], "standalone"),
        (["--host-profile", "ide", "--workspace-root", str(Path.cwd())], "ide"),
    ],
)
def test_stdio_initialize_lists_and_calls_the_seven_tools(
    server_arguments: list[str], expected_mode: str
) -> None:
    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tfbash_mcp", *server_arguments],
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.instructions is not None
            assert "bash dialect" in initialized.instructions

            listed = await session.list_tools()
            assert [tool.name for tool in listed.tools] == [
                "shell_open",
                "shell_exec",
                "shell_read",
                "shell_write",
                "shell_signal",
                "shell_list",
                "shell_close",
            ]
            assert all(tool.outputSchema is not None for tool in listed.tools)
            assert all(
                tool.meta is not None and "tfbash-mcp/errorSchema" in tool.meta
                for tool in listed.tools
            )
            write_schema = next(
                tool.inputSchema for tool in listed.tools if tool.name == "shell_write"
            )
            assert "eof" not in str(write_schema)

            context_result = await session.call_tool("shell_list", {})
            context = cast(dict[str, Any], context_result.structuredContent)
            assert context_result.isError is False
            assert context["runtime"]["dialect"] == "bash"
            assert context["host"]["mode"] == expected_mode
            assert context["runtime"]["default_cwd"] == str(Path.cwd())

            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])
            executed = await session.call_tool(
                "shell_exec",
                {"shell_id": shell_id, "command": "printf mcp-e2e", "yield_ms": 2_000},
            )
            execution = cast(dict[str, Any], executed.structuredContent)
            assert execution["status"] == "exited"
            assert execution["output"] == "mcp-e2e"

            closed = await session.call_tool("shell_close", {"shell_id": shell_id})
            closed_content = cast(dict[str, Any], closed.structuredContent)
            assert closed_content["cleanup_complete"] is True

    anyio.run(scenario)


def test_cancelling_an_inflight_long_call_does_not_block_stdio_shutdown() -> None:
    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tfbash_mcp"],
            cwd=Path.cwd(),
        )
        context_started = time.monotonic()
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])

            async def long_call() -> None:
                await session.call_tool(
                    "shell_exec",
                    {"shell_id": shell_id, "command": "sleep 30", "yield_ms": 60_000},
                )

            async with anyio.create_task_group() as calls:
                calls.start_soon(long_call)
                for _ in range(50):
                    listed = await session.call_tool("shell_list", {})
                    content = cast(dict[str, Any], listed.structuredContent)
                    if content["shells"][0]["active_exec_id"] is not None:
                        break
                    await anyio.sleep(0.01)
                else:
                    raise AssertionError("long execution did not become active")
                cancel_started = time.monotonic()
                calls.cancel_scope.cancel()
            assert time.monotonic() - cancel_started < 1

        assert time.monotonic() - context_started < 6

    anyio.run(scenario)


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows ConPTY")
def test_stdio_uses_the_production_windows_profile_end_to_end() -> None:
    async def scenario() -> None:
        pwsh = os.environ["PHASE0_PWSH"]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "tfbash_mcp",
                "--runtime-profile",
                "windows-pwsh",
                "--shell",
                pwsh,
                "--close-timeout-ms",
                "10000",
            ],
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.instructions is not None
            assert "pwsh dialect" in initialized.instructions

            context_result = await session.call_tool("shell_list", {})
            context = cast(dict[str, Any], context_result.structuredContent)
            assert context_result.isError is False
            assert context["runtime"]["platform"] == "windows"
            assert context["runtime"]["dialect"] == "pwsh"

            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])
            executed = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": 'cmd.exe /d /c "echo mcp-windows-e2e & exit /b 37"',
                    "yield_ms": 5_000,
                },
            )
            execution = cast(dict[str, Any], executed.structuredContent)
            assert execution["status"] == "exited"
            assert cast(str, execution["output"]).strip() == "mcp-windows-e2e"
            assert execution["exit_code"] == 37

            running = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": "Start-Sleep -Seconds 30",
                    "yield_ms": 0,
                    "timeout_ms": 30_000,
                },
            )
            running_content = cast(dict[str, Any], running.structuredContent)
            assert running_content["status"] == "running"
            exec_id = cast(str, running_content["exec_id"])
            killed = await session.call_tool(
                "shell_signal",
                {"shell_id": shell_id, "exec_id": exec_id, "signal": "kill"},
            )
            killed_content = cast(dict[str, Any], killed.structuredContent)
            assert killed_content["status"] == "delivered"

            for _ in range(100):
                listed = await session.call_tool("shell_list", {})
                listed_content = cast(dict[str, Any], listed.structuredContent)
                shells = cast(list[dict[str, Any]], listed_content["shells"])
                if shells[0]["status"] == "ready" and shells[0]["active_exec_id"] is None:
                    break
                await anyio.sleep(0.05)
            else:
                raise AssertionError("Windows Shell did not rebuild after a forced Job kill")

            recovered = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": "Write-Output mcp-windows-rebuilt",
                    "yield_ms": 5_000,
                },
            )
            recovered_content = cast(dict[str, Any], recovered.structuredContent)
            assert recovered_content["status"] == "exited"
            assert cast(str, recovered_content["output"]).strip() == "mcp-windows-rebuilt"

            closed = await session.call_tool("shell_close", {"shell_id": shell_id})
            closed_content = cast(dict[str, Any], closed.structuredContent)
            assert closed_content["cleanup_complete"] is True

    anyio.run(scenario)
