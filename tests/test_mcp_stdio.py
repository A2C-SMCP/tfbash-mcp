from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from tfbash_mcp.mcp_adapter import SHELL_OVERVIEW_URI

EXPECTED_TOOL_TAGS = {
    "shell_open": ["BuildIn", "Create"],
    "shell_exec": ["BuildIn", "Create", "Read", "Update", "Delete"],
    "shell_read": ["BuildIn", "Read"],
    "shell_write": ["BuildIn", "Create", "Read", "Update", "Delete"],
    "shell_signal": ["BuildIn", "Update"],
    "shell_list": ["BuildIn", "Read"],
    "shell_close": ["BuildIn", "Delete"],
}


async def _read_until_terminal(
    session: ClientSession,
    *,
    shell_id: str,
    exec_id: str,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    cursor = 0
    output = ""
    deadline = anyio.current_time() + timeout_seconds
    last: dict[str, Any] = {}
    while anyio.current_time() < deadline:
        remaining_ms = max(1, int((deadline - anyio.current_time()) * 1000))
        result = await session.call_tool(
            "shell_read",
            {
                "shell_id": shell_id,
                "exec_id": exec_id,
                "cursor": cursor,
                "wait_ms": min(5_000, remaining_ms),
            },
        )
        if result.structuredContent is None:
            raise AssertionError(f"shell_read returned no structured result: {result.content}")
        last = result.structuredContent
        cursor = cast(int, last["next_cursor"])
        output += cast(str, last["output"])
        if last["status"] != "running":
            return {**last, "output": output}
    raise AssertionError(f"execution did not become terminal: {last}")


async def _read_until_output(
    session: ClientSession,
    *,
    shell_id: str,
    exec_id: str,
    expected: str,
    timeout_seconds: float = 10,
) -> dict[str, Any]:
    cursor = 0
    output = ""
    deadline = anyio.current_time() + timeout_seconds
    last: dict[str, Any] = {}
    while anyio.current_time() < deadline:
        remaining_ms = max(1, int((deadline - anyio.current_time()) * 1000))
        result = await session.call_tool(
            "shell_read",
            {
                "shell_id": shell_id,
                "exec_id": exec_id,
                "cursor": cursor,
                "wait_ms": min(5_000, remaining_ms),
            },
        )
        if result.structuredContent is None:
            raise AssertionError(f"shell_read returned no structured result: {result.content}")
        last = result.structuredContent
        cursor = cast(int, last["next_cursor"])
        output += cast(str, last["output"])
        if expected in output:
            return last
        if last["status"] != "running":
            break
    raise AssertionError(f"execution did not produce {expected!r}: {last}")


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
        resource_updates: list[str] = []
        resource_updated = anyio.Event()

        async def message_handler(message: Any) -> None:
            if isinstance(message, types.ServerNotification) and isinstance(
                message.root,
                types.ResourceUpdatedNotification,
            ):
                resource_updates.append(str(message.root.params.uri))
                resource_updated.set()

        runtime_arguments: list[str]
        if sys.platform == "win32":
            runtime_arguments = [
                "--runtime-profile",
                "windows-pwsh",
                "--shell",
                os.environ["PHASE0_PWSH"],
            ]
            expected_dialect = "pwsh"
            command = "Write-Output mcp-e2e"
            stdin_command = (
                "Start-Sleep -Seconds 2; $value = [Console]::ReadLine(); "
                'Write-Output "stdin:$value"'
            )
            stdin_text = "form-ready\r\n"
        else:
            runtime_arguments = []
            expected_dialect = "bash"
            command = "printf mcp-e2e"
            stdin_command = "IFS= read -r value; printf 'stdin:%s' \"$value\""
            stdin_text = "form-ready\n"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tfbash_mcp", *runtime_arguments, *server_arguments],
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(
                read_stream,
                write_stream,
                message_handler=message_handler,
            ) as session,
        ):
            initialized = await session.initialize()
            assert initialized.instructions is not None
            assert f"{expected_dialect} dialect" in initialized.instructions
            assert initialized.capabilities.resources is not None
            assert initialized.capabilities.resources.subscribe is True

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
            assert all(tool.inputSchema.get("type") == "object" for tool in listed.tools)
            assert all(
                tool.outputSchema is not None and tool.outputSchema.get("type") == "object"
                for tool in listed.tools
            )
            assert all(
                tool.meta is not None and "tfbash-mcp/errorSchema" in tool.meta
                for tool in listed.tools
            )
            assert {
                tool.name: cast(dict[str, Any], tool.meta)["a2c_tool_meta"]["tags"]
                for tool in listed.tools
            } == EXPECTED_TOOL_TAGS
            write_schema = next(
                tool.inputSchema for tool in listed.tools if tool.name == "shell_write"
            )
            assert list(write_schema["properties"]) == ["shell_id", "exec_id", "text"]
            assert write_schema["required"] == ["shell_id", "exec_id", "text"]
            assert "oneOf" not in write_schema
            assert "data_base64" not in str(write_schema)
            assert "eof" not in str(write_schema)

            resources = await session.list_resources()
            assert len(resources.resources) == 1
            overview_resource = resources.resources[0]
            assert str(overview_resource.uri) == SHELL_OVERVIEW_URI
            assert overview_resource.mimeType == "text/markdown"
            assert overview_resource.annotations is not None
            assert overview_resource.annotations.audience == ["assistant"]
            assert overview_resource.annotations.priority == 0.8
            assert overview_resource.meta == {"fullscreen": False}
            empty_overview = await session.read_resource(overview_resource.uri)
            empty_content = empty_overview.contents[0]
            assert isinstance(empty_content, types.TextResourceContents)
            assert "No active Shells." in empty_content.text
            with pytest.raises(McpError):
                await session.read_resource(AnyUrl("window://io.github.a2c-smcp.tfbash/unknown"))
            await session.subscribe_resource(overview_resource.uri)

            context_result = await session.call_tool("shell_list", {})
            context = cast(dict[str, Any], context_result.structuredContent)
            assert context_result.isError is False
            assert context["runtime"]["dialect"] == expected_dialect
            assert context["host"]["mode"] == expected_mode
            assert context["runtime"]["default_cwd"] == str(Path.cwd())

            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])
            with anyio.fail_after(2):
                await resource_updated.wait()
            assert resource_updates[-1] == SHELL_OVERVIEW_URI
            executed = await session.call_tool(
                "shell_exec",
                {"shell_id": shell_id, "command": command, "yield_ms": 2_000},
            )
            execution = cast(dict[str, Any], executed.structuredContent)
            if execution["status"] == "running":
                execution = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, execution["exec_id"]),
                )
            assert execution["status"] == "exited"
            assert cast(str, execution["output"]).strip() == "mcp-e2e"

            overview = await session.read_resource(overview_resource.uri)
            overview_content = overview.contents[0]
            assert isinstance(overview_content, types.TextResourceContents)
            assert shell_id in overview_content.text
            assert "mcp-e2e" in overview_content.text

            waiting = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": stdin_command,
                    "yield_ms": 0,
                    "timeout_ms": 10_000,
                },
            )
            waiting_content = cast(dict[str, Any], waiting.structuredContent)
            assert waiting_content["status"] == "running"
            exec_id = cast(str, waiting_content["exec_id"])
            written = await session.call_tool(
                "shell_write",
                {"shell_id": shell_id, "exec_id": exec_id, "text": stdin_text},
            )
            written_content = cast(dict[str, Any], written.structuredContent)
            assert written.isError is False
            assert written_content["accepted_bytes"] == len(stdin_text.encode())
            after_write = await _read_until_terminal(
                session,
                shell_id=shell_id,
                exec_id=exec_id,
            )
            assert "stdin:form-ready" in cast(str, after_write["output"])

            await anyio.sleep(0.3)
            await session.unsubscribe_resource(overview_resource.uri)
            update_count = len(resource_updates)

            closed = await session.call_tool("shell_close", {"shell_id": shell_id})
            closed_content = cast(dict[str, Any], closed.structuredContent)
            assert closed_content["cleanup_complete"] is True
            await anyio.sleep(0.2)
            assert len(resource_updates) == update_count
            closed_overview = await session.read_resource(overview_resource.uri)
            closed_content_item = closed_overview.contents[0]
            assert isinstance(closed_content_item, types.TextResourceContents)
            assert "No active Shells." in closed_content_item.text

    anyio.run(scenario)


def test_cancelling_an_inflight_long_call_does_not_block_stdio_shutdown() -> None:
    async def scenario() -> None:
        runtime_arguments: list[str]
        if sys.platform == "win32":
            runtime_arguments = [
                "--runtime-profile",
                "windows-pwsh",
                "--shell",
                os.environ["PHASE0_PWSH"],
            ]
            long_command = "Start-Sleep -Seconds 30"
        else:
            runtime_arguments = []
            long_command = "sleep 30"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tfbash_mcp", *runtime_arguments],
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
                    {"shell_id": shell_id, "command": long_command, "yield_ms": 60_000},
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

        # Windows may spend its bounded shutdown budget proving Job cleanup,
        # but cancellation must not wait for the 30-second user command.
        assert time.monotonic() - context_started < 20

    anyio.run(scenario)


@pytest.mark.skipif(sys.platform == "win32", reason="requires a native POSIX PTY")
def test_stdio_posix_host_environment_and_forced_control_end_to_end() -> None:
    async def scenario() -> None:
        startup_command = "export TFBASH_STARTUP_REPLAY=ready"
        inherited_secret = "inherited-without-agent-disclosure"
        virtual_environment = str(Path.cwd() / ".test-venv")
        inherited_path = f"{Path(virtual_environment) / 'bin'}{os.pathsep}{os.environ['PATH']}"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "tfbash_mcp",
                "--host-profile",
                "ide",
                "--workspace-root",
                str(Path.cwd()),
                "--environment-kind",
                "python-venv",
                "--environment-name",
                "integration-venv",
                "--startup-command",
                startup_command,
                "--close-timeout-ms",
                "10000",
            ],
            env={
                **os.environ,
                "PATH": inherited_path,
                "VIRTUAL_ENV": virtual_environment,
                "TFBASH_ENV_SECRET": inherited_secret,
            },
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.call_tool("shell_list", {})
            context = cast(dict[str, Any], listed.structuredContent)
            assert context["host"] == {
                "mode": "ide",
                "workspace_root": str(Path.cwd()),
                "environment": {
                    "kind": "python-venv",
                    "name": "integration-venv",
                },
            }
            visible = repr(context)
            assert virtual_environment not in visible
            assert inherited_secret not in visible
            assert startup_command not in visible
            assert sys.executable not in visible

            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])
            environment_probe = (
                f'test "$PATH" = {shlex.quote(inherited_path)} '
                '&& test -n "$VIRTUAL_ENV" '
                '&& test "$TFBASH_ENV_SECRET" = "inherited-without-agent-disclosure" '
                '&& test "$TFBASH_STARTUP_REPLAY" = "ready" '
                "&& printf posix-host-ready"
            )
            first = await session.call_tool(
                "shell_exec",
                {"shell_id": shell_id, "command": environment_probe, "yield_ms": 5_000},
            )
            first_content = cast(dict[str, Any], first.structuredContent)
            if first_content["status"] == "running":
                first_content = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, first_content["exec_id"]),
                )
            assert first_content["status"] == "exited"
            assert first_content["exit_code"] == 0
            assert first_content["output"] == "posix-host-ready"

            # The streaming parser retains a possible result-marker suffix between reads.
            control_ready = "control-ready-" + "x" * 128
            running = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": f"printf {control_ready}; sleep 30",
                    "yield_ms": 0,
                    "timeout_ms": 30_000,
                },
            )
            running_content = cast(dict[str, Any], running.structuredContent)
            assert running_content["status"] == "running"
            exec_id = cast(str, running_content["exec_id"])
            await _read_until_output(
                session,
                shell_id=shell_id,
                exec_id=exec_id,
                expected="control-ready",
            )
            killed = await session.call_tool(
                "shell_signal",
                {"shell_id": shell_id, "exec_id": exec_id, "signal": "kill"},
            )
            assert cast(dict[str, Any], killed.structuredContent)["status"] == "delivered"
            terminal_content = await _read_until_terminal(
                session,
                shell_id=shell_id,
                exec_id=exec_id,
            )
            assert terminal_content["status"] == "exited"
            assert terminal_content["shell_status"] == "ready"
            assert terminal_content["shell_rebuilt"] is False

            replayed = await session.call_tool(
                "shell_exec",
                {"shell_id": shell_id, "command": environment_probe, "yield_ms": 5_000},
            )
            replayed_content = cast(dict[str, Any], replayed.structuredContent)
            if replayed_content["status"] == "running":
                replayed_content = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, replayed_content["exec_id"]),
                )
            assert replayed_content["status"] == "exited"
            assert replayed_content["exit_code"] == 0
            assert replayed_content["output"] == "posix-host-ready"

            closed = await session.call_tool("shell_close", {"shell_id": shell_id})
            assert cast(dict[str, Any], closed.structuredContent)["cleanup_complete"] is True

    anyio.run(scenario)


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows ConPTY")
def test_stdio_uses_the_production_windows_profile_end_to_end() -> None:
    async def scenario() -> None:
        pwsh = os.environ["PHASE0_PWSH"]
        startup_command = "$env:TFBASH_STARTUP_REPLAY='ready'"
        inherited_secret = "inherited-without-agent-disclosure"
        virtual_environment = str(Path.cwd() / ".test-venv")
        path_key = next(key for key in os.environ if key.casefold() == "path")
        inherited_path = (
            f"{Path(virtual_environment) / 'Scripts'}{os.pathsep}{os.environ[path_key]}"
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "tfbash_mcp",
                "--runtime-profile",
                "windows-pwsh",
                "--shell",
                pwsh,
                "--host-profile",
                "ide",
                "--workspace-root",
                str(Path.cwd()),
                "--environment-kind",
                "python-venv",
                "--environment-name",
                "integration-venv",
                "--startup-command",
                startup_command,
                "--close-timeout-ms",
                "10000",
            ],
            env={
                **os.environ,
                path_key: inherited_path,
                "VIRTUAL_ENV": virtual_environment,
                "TFBASH_ENV_SECRET": inherited_secret,
            },
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
            assert context["host"] == {
                "mode": "ide",
                "workspace_root": str(Path.cwd()),
                "environment": {
                    "kind": "python-venv",
                    "name": "integration-venv",
                },
            }
            visible = repr(context)
            assert virtual_environment not in visible
            assert inherited_secret not in visible
            assert startup_command not in visible
            assert pwsh not in visible

            opened = await session.call_tool("shell_open", {})
            opened_content = cast(dict[str, Any], opened.structuredContent)
            shell_id = cast(str, opened_content["shell_id"])
            environment_probe = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": (
                        "Write-Output ((($env:Path.Split(';') -contains "
                        "\"$env:VIRTUAL_ENV\\Scripts\").ToString()) + ':' + "
                        "(($env:TFBASH_ENV_SECRET -eq "
                        "'inherited-without-agent-disclosure').ToString()) + ':' + "
                        "(($env:TFBASH_STARTUP_REPLAY -eq 'ready').ToString()))"
                    ),
                    "yield_ms": 5_000,
                },
            )
            environment_content = cast(dict[str, Any], environment_probe.structuredContent)
            if environment_content["status"] == "running":
                environment_content = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, environment_content["exec_id"]),
                )
            assert environment_content["status"] == "exited"
            assert environment_content["exit_code"] == 0
            assert cast(str, environment_content["output"]).strip() == "True:True:True"

            executed = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": 'cmd.exe /d /c "echo mcp-windows-e2e & exit /b 37"',
                    "yield_ms": 5_000,
                },
            )
            execution = cast(dict[str, Any], executed.structuredContent)
            if execution["status"] == "running":
                execution = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, execution["exec_id"]),
                )
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

            terminal_content = await _read_until_terminal(
                session,
                shell_id=shell_id,
                exec_id=exec_id,
            )
            assert terminal_content["status"] == "cancelled"
            assert terminal_content["shell_status"] == "ready"
            assert terminal_content["shell_rebuilt"] is True

            recovered = await session.call_tool(
                "shell_exec",
                {
                    "shell_id": shell_id,
                    "command": (
                        'Write-Output ("mcp-windows-rebuilt:" + '
                        "($env:TFBASH_STARTUP_REPLAY -eq 'ready'))"
                    ),
                    "yield_ms": 5_000,
                },
            )
            recovered_content = cast(dict[str, Any], recovered.structuredContent)
            if recovered_content["status"] == "running":
                recovered_content = await _read_until_terminal(
                    session,
                    shell_id=shell_id,
                    exec_id=cast(str, recovered_content["exec_id"]),
                )
            assert recovered_content["status"] == "exited"
            assert cast(str, recovered_content["output"]).strip() == "mcp-windows-rebuilt:True"

            closed = await session.call_tool("shell_close", {"shell_id": shell_id})
            closed_content = cast(dict[str, Any], closed.structuredContent)
            assert closed_content["cleanup_complete"] is True

    anyio.run(scenario)
