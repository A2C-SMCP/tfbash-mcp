from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

import tfbash_mcp.domain.registry as registry_module
import tfbash_mcp.embedded as embedded_module
from tfbash_mcp import (
    EmbeddedShellConfig,
    EmbeddedShellRuntime,
    ToolConcurrencyBudget,
    ToolConcurrencyLimits,
)
from tfbash_mcp.mcp_adapter import ShellToolService
from tfbash_mcp.resource_adapter import SHELL_OVERVIEW_URI


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


def test_embedded_resource_api_subscribes_synchronously_and_cleans_up_on_close() -> None:
    class ResourceService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def __init__(self) -> None:
            self.listeners: set[Callable[[], None]] = set()
            self.listeners_at_shutdown = -1

        def shell_overview_markdown(self) -> str:
            return "# Shell Overview\n\nembedded"

        def subscribe_overview_changes(
            self,
            listener: Callable[[], None],
        ) -> Callable[[], None]:
            self.listeners.add(listener)
            return lambda: self.listeners.discard(listener)

        def shutdown(self) -> None:
            self.listeners_at_shutdown = len(self.listeners)

    async def scenario() -> None:
        service = ResourceService()
        runtime = EmbeddedShellRuntime(
            cast(ShellToolService, service),
            ToolConcurrencyBudget(service.concurrency_limits),
        )
        resource = runtime.list_resources()[0]
        assert str(resource.uri) == SHELL_OVERVIEW_URI
        assert resource.mimeType == "text/markdown"
        assert resource.annotations == types.Annotations(priority=0.8, audience=["assistant"])
        assert resource.meta == {"fullscreen": False}
        content = runtime.read_resource(resource.uri).contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert content.text == "# Shell Overview\n\nembedded"
        with pytest.raises(McpError):
            runtime.read_resource("not a uri")

        callback_threads: list[int] = []
        updates: list[str] = []

        def updated(uri: AnyUrl) -> None:
            callback_threads.append(get_ident())
            updates.append(str(uri))
            assert runtime.list_resources()

        producer_thread = get_ident()
        unsubscribe = runtime.subscribe_resource_updates(updated)
        for listener in tuple(service.listeners):
            listener()
        assert callback_threads == [producer_thread]
        assert updates == [SHELL_OVERVIEW_URI]

        unsubscribe()
        unsubscribe()
        assert not service.listeners

        runtime.subscribe_resource_updates(updated)
        assert service.listeners
        await runtime.aclose()
        assert not service.listeners
        assert service.listeners_at_shutdown == 0
        for listener in tuple(service.listeners):
            listener()
        assert updates == [SHELL_OVERVIEW_URI]

        with pytest.raises(RuntimeError, match="closing or closed"):
            runtime.list_resources()
        with pytest.raises(RuntimeError, match="closing or closed"):
            runtime.read_resource(SHELL_OVERVIEW_URI)
        with pytest.raises(RuntimeError, match="closing or closed"):
            runtime.subscribe_resource_updates(updated)

    anyio.run(scenario)


def test_embedded_resource_unsubscribe_and_close_are_notification_barriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResourceService:
        concurrency_limits = ToolConcurrencyLimits(1, 1, 1, 1)

        def __init__(self) -> None:
            self.listeners: set[Callable[[], None]] = set()

        def shell_overview_markdown(self) -> str:
            return "overview"

        def subscribe_overview_changes(
            self,
            listener: Callable[[], None],
        ) -> Callable[[], None]:
            self.listeners.add(listener)
            return lambda: self.listeners.discard(listener)

        def emit(self) -> None:
            for listener in tuple(self.listeners):
                listener()

        def shutdown(self) -> None:
            pass

    def make_runtime(service: ResourceService) -> EmbeddedShellRuntime:
        return EmbeddedShellRuntime(
            cast(ShellToolService, service),
            ToolConcurrencyBudget(service.concurrency_limits),
        )

    unsubscribe_service = ResourceService()
    unsubscribe_runtime = make_runtime(unsubscribe_service)
    callback_started = Event()
    callback_release = Event()
    callback_calls = 0

    def blocking_callback(_uri: AnyUrl) -> None:
        nonlocal callback_calls
        callback_calls += 1
        callback_started.set()
        assert callback_release.wait(5)

    unsubscribe = unsubscribe_runtime.subscribe_resource_updates(blocking_callback)
    emitter = Thread(target=unsubscribe_service.emit)
    emitter.start()
    assert callback_started.wait(5)
    unsubscribe_started = Event()
    unsubscribe_finished = Event()

    def unsubscribe_concurrently() -> None:
        unsubscribe_started.set()
        unsubscribe()
        unsubscribe_finished.set()

    unsubscriber = Thread(target=unsubscribe_concurrently)
    unsubscriber.start()
    assert unsubscribe_started.wait(5)
    assert not unsubscribe_finished.wait(0.05)
    callback_release.set()
    emitter.join(5)
    unsubscriber.join(5)
    assert not emitter.is_alive()
    assert not unsubscriber.is_alive()
    assert unsubscribe_finished.is_set()
    unsubscribe_service.emit()
    assert callback_calls == 1

    async def close_scenario() -> None:
        await unsubscribe_runtime.aclose()
        close_service = ResourceService()
        close_runtime = make_runtime(close_service)
        close_callback_started = Event()
        close_callback_release = Event()

        def close_blocking_callback(_uri: AnyUrl) -> None:
            close_callback_started.set()
            assert close_runtime.list_resources()
            assert close_runtime.read_resource(SHELL_OVERVIEW_URI).contents
            assert close_callback_release.wait(5)

        close_runtime.subscribe_resource_updates(close_blocking_callback)
        close_emitter = Thread(target=close_service.emit)
        close_emitter.start()
        assert close_callback_started.wait(5)

        claim_started = Event()
        original_claim_close = close_runtime._claim_close

        def observed_claim_close() -> tuple[
            str,
            anyio.Event,
            tuple[Callable[[], None], ...],
        ]:
            claim_started.set()
            return original_claim_close()

        monkeypatch.setattr(close_runtime, "_claim_close", observed_claim_close)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(close_runtime.aclose)
            with anyio.fail_after(5):
                await anyio.to_thread.run_sync(claim_started.wait)
            assert close_runtime._state == "open"
            close_callback_release.set()

        close_emitter.join(5)
        assert not close_emitter.is_alive()
        assert close_runtime._state == "closed"
        assert not close_service.listeners
        close_service.emit()

    anyio.run(close_scenario)


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
        first_resource_updates: list[str] = []
        unsubscribe_first_resources = first.subscribe_resource_updates(
            lambda uri: first_resource_updates.append(str(uri))
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
            assert first_resource_updates
            assert set(first_resource_updates) == {SHELL_OVERVIEW_URI}

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
            first_overview = first.read_resource(SHELL_OVERVIEW_URI).contents[0]
            assert isinstance(first_overview, types.TextResourceContents)
            assert first_open["shell_id"] in first_overview.text
            assert "first" in first_overview.text

            first_list = _payload(await first.call_tool("shell_list"))
            second_list = _payload(await second.call_tool("shell_list"))
            assert first_list["host"]["workspace_root"] == str(first_workspace)
            assert second_list["host"]["workspace_root"] == str(second_workspace)

            await first.aclose()
            with pytest.raises(RuntimeError, match="closing or closed"):
                await first.call_tool("shell_list")
            assert _payload(await second.call_tool("shell_list"))["shells"]
        finally:
            unsubscribe_first_resources()
            await first.aclose()
            await second.aclose()

    anyio.run(scenario)
