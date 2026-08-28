from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from mcp import types

import tfbash_mcp.domain.registry as registry_module
import tfbash_mcp.embedded as embedded_module
from tfbash_mcp import (
    EmbeddedShellConfig,
    EmbeddedShellRuntime,
    ToolConcurrencyBudget,
    ToolConcurrencyLimits,
)
from tfbash_mcp.mcp_adapter import ShellToolService


def _payload(result: object) -> dict[str, Any]:
    content = getattr(result, "structuredContent", None)
    if not isinstance(content, dict):
        raise AssertionError(f"tool returned no structured content: {result!r}")
    return cast(dict[str, Any], content)


def _native_shell() -> str:
    if sys.platform == "win32":
        return r"C:\Program Files\PowerShell\7\pwsh.exe"
    return "/bin/bash"


def _native_profile() -> str:
    return "pwsh" if sys.platform == "win32" else "bash"


def test_embedded_config_freezes_environment_snapshot(tmp_path: Path) -> None:
    environment = {"TFBASH_EMBED_MARKER": "first"}
    config = EmbeddedShellConfig(
        workspace_root=str(tmp_path),
        environment=environment,
    )
    environment["TFBASH_EMBED_MARKER"] = "second"

    assert config.environment["TFBASH_EMBED_MARKER"] == "first"
    with pytest.raises(TypeError):
        cast(dict[str, str], config.environment)["NEW"] = "value"


def test_embedded_create_does_not_block_the_host_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    class FakeService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def shutdown(self) -> None:
            pass

    def blocking_build(_config: object) -> ShellToolService:
        started.set()
        assert release.wait(5)
        return cast(ShellToolService, FakeService())

    monkeypatch.setattr(embedded_module, "build_shell_service", blocking_build)

    async def scenario() -> None:
        ticks = 0
        runtime: EmbeddedShellRuntime | None = None

        async def initialize() -> None:
            nonlocal runtime
            runtime = await EmbeddedShellRuntime.create(
                EmbeddedShellConfig(workspace_root=str(tmp_path), environment={})
            )

        async def heartbeat() -> None:
            nonlocal ticks
            await anyio.to_thread.run_sync(started.wait)
            for _ in range(3):
                ticks += 1
                await anyio.sleep(0)
            release.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(initialize)
            tasks.start_soon(heartbeat)

        assert ticks == 3
        assert runtime is not None
        await runtime.aclose()

    anyio.run(scenario)


def test_cancelling_embedded_create_cleans_the_built_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()
    shutdown_calls = 0

    class FakeService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def shutdown(self) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

    def blocking_build(_config: object) -> ShellToolService:
        started.set()
        assert release.wait(5)
        return cast(ShellToolService, FakeService())

    monkeypatch.setattr(embedded_module, "build_shell_service", blocking_build)

    async def scenario() -> None:
        scopes: list[anyio.CancelScope] = []
        returned: list[EmbeddedShellRuntime] = []
        caller_done = anyio.Event()

        async def initialize() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    returned.append(
                        await EmbeddedShellRuntime.create(
                            EmbeddedShellConfig(
                                workspace_root=str(tmp_path),
                                environment={},
                            )
                        )
                    )
                finally:
                    caller_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(initialize)
            await anyio.to_thread.run_sync(started.wait)
            scopes[0].cancel()
            release.set()
            with anyio.fail_after(1):
                await caller_done.wait()

        assert returned == []

    anyio.run(scenario)
    assert shutdown_calls == 1


def test_embedded_close_is_concurrent_idempotent_and_retryable() -> None:
    class FlakyService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()

        def shutdown(self) -> None:
            with self.lock:
                self.calls += 1
                call = self.calls
            if call == 1:
                raise RuntimeError("cleanup failed")

    async def scenario() -> None:
        service = FlakyService()
        runtime = EmbeddedShellRuntime(
            cast(ShellToolService, service),
            ToolConcurrencyBudget(service.concurrency_limits),
        )

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await runtime.aclose()

        with pytest.raises(RuntimeError, match="closing or closed"):
            runtime.list_tools()
        with pytest.raises(RuntimeError, match="closing or closed"):
            await runtime.call_tool("shell_list")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(runtime.aclose)
            tasks.start_soon(runtime.aclose)

        assert service.calls == 2
        await runtime.aclose()
        assert service.calls == 2
        with pytest.raises(RuntimeError, match="closing or closed"):
            runtime.list_tools()

    anyio.run(scenario)


@pytest.mark.parametrize("shutdown_fails", [False, True])
def test_cancelling_close_cannot_strand_the_runtime_in_closing(
    shutdown_fails: bool,
) -> None:
    started = Event()
    release = Event()

    class InterruptibleService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def __init__(self) -> None:
            self.calls = 0
            self.fail = shutdown_fails

        def shutdown(self) -> None:
            self.calls += 1
            started.set()
            assert release.wait(5)
            if self.fail:
                raise RuntimeError("cleanup failed")

    async def scenario() -> None:
        service = InterruptibleService()
        runtime = EmbeddedShellRuntime(
            cast(ShellToolService, service),
            ToolConcurrencyBudget(service.concurrency_limits),
        )
        scopes: list[anyio.CancelScope] = []
        errors: list[str] = []
        owner_done = anyio.Event()

        async def close_owner() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    await runtime.aclose()
                except RuntimeError as error:
                    errors.append(str(error))
                finally:
                    owner_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(close_owner)
            await anyio.to_thread.run_sync(started.wait)
            scopes[0].cancel()
            release.set()
            with anyio.fail_after(1):
                await owner_done.wait()

        assert runtime._state != "closing"
        if shutdown_fails:
            assert errors == ["cleanup failed"]
            service.fail = False
        else:
            assert errors == []
        with anyio.fail_after(1):
            await runtime.aclose()
        assert service.calls == (2 if shutdown_fails else 1)

    anyio.run(scenario)


def test_cancelled_embedded_call_does_not_block_runtime_close() -> None:
    started = Event()
    release = Event()

    class BlockingService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def call(self, _name: str, _arguments: dict[str, object]) -> types.CallToolResult:
            started.set()
            assert release.wait(5)
            return types.CallToolResult(content=[])

        def shutdown(self) -> None:
            release.set()

    async def scenario() -> None:
        service = BlockingService()
        runtime = EmbeddedShellRuntime(
            cast(ShellToolService, service),
            ToolConcurrencyBudget(service.concurrency_limits),
        )
        scopes: list[anyio.CancelScope] = []
        caller_done = anyio.Event()

        async def caller() -> None:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    await runtime.call_tool("shell_list")
                finally:
                    caller_done.set()

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(caller)
            await anyio.to_thread.run_sync(started.wait)
            scopes[0].cancel()
            with anyio.fail_after(1):
                await caller_done.wait()
            with anyio.fail_after(1):
                await runtime.aclose()

    anyio.run(scenario)


@pytest.mark.skipif(
    sys.platform == "win32" and not Path(_native_shell()).exists(),
    reason="PowerShell Core is required for the native Windows embedded test",
)
def test_two_real_embedded_runtimes_keep_project_state_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        registry_module,
        "uuid4",
        lambda: SimpleNamespace(hex="sameidacrossruntimeinstances"),
    )
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()

    async def scenario() -> None:
        first = await EmbeddedShellRuntime.create(
            EmbeddedShellConfig(
                workspace_root=str(first_workspace),
                environment={**os.environ, "TFBASH_EMBED_MARKER": "first"},
                runtime_profile=_native_profile(),
                shell=_native_shell(),
            )
        )
        second = await EmbeddedShellRuntime.create(
            EmbeddedShellConfig(
                workspace_root=str(second_workspace),
                environment={**os.environ, "TFBASH_EMBED_MARKER": "second"},
                runtime_profile=_native_profile(),
                shell=_native_shell(),
            )
        )
        try:
            assert [tool.name for tool in first.list_tools()] == [
                "shell_open",
                "shell_exec",
                "shell_read",
                "shell_write",
                "shell_signal",
                "shell_list",
                "shell_close",
            ]
            first_open = _payload(await first.call_tool("shell_open"))
            second_open = _payload(await second.call_tool("shell_open"))
            assert first_open["shell_id"] == second_open["shell_id"]

            command = (
                "Write-Output $env:TFBASH_EMBED_MARKER"
                if sys.platform == "win32"
                else "printf '%s' \"$TFBASH_EMBED_MARKER\""
            )
            first_exec = _payload(
                await first.call_tool(
                    "shell_exec",
                    {"shell_id": first_open["shell_id"], "command": command},
                )
            )
            second_exec = _payload(
                await second.call_tool(
                    "shell_exec",
                    {"shell_id": second_open["shell_id"], "command": command},
                )
            )
            assert first_exec["output"] == "first"
            assert second_exec["output"] == "second"

            first_list = _payload(await first.call_tool("shell_list"))
            second_list = _payload(await second.call_tool("shell_list"))
            assert first_list["host"]["workspace_root"] == str(first_workspace)
            assert second_list["host"]["workspace_root"] == str(second_workspace)

            await first.aclose()
            with pytest.raises(RuntimeError, match="closing or closed"):
                await first.call_tool("shell_list")
            assert _payload(await second.call_tool("shell_list"))["shells"]
        finally:
            await first.aclose()
            await second.aclose()

    anyio.run(scenario)
