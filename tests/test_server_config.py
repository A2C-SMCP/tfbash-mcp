from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import anyio
import pytest
from pydantic import AnyUrl

import tfbash_mcp.composition as composition_module
import tfbash_mcp.embedded as embedded_module
import tfbash_mcp.server as server_module
from tfbash_mcp.domain import (
    CommandShellManager,
    ExecutionSnapshot,
    ExecutionState,
    ShellSnapshot,
    ShellState,
)
from tfbash_mcp.mcp_adapter import ToolConcurrencyLimits
from tfbash_mcp.protocol import PlatformName, ProtocolConfig, ToolName, tool_contract_schemas
from tfbash_mcp.runtime import (
    ConPtyTransport,
    DialectName,
    PosixProcessSupervisor,
    PowerShellDialect,
    RuntimeConfigurationError,
    RuntimeName,
    ShellResolution,
    ShellStartRequest,
    WindowsProcessSupervisor,
    WindowsPwshProfile,
)
from tfbash_mcp.runtime.resolver import RoutingCleanupError
from tfbash_mcp.server import (
    _build_posix_runtime,
    _build_windows_runtime,
    _create_tool_limiters,
    _OverviewSubscriptionHub,
    _probe_managed_candidate,
    _probe_shell_version,
    _redact_sensitive_schema_defaults,
    _run_tool_call,
    _validate_cross_option_constraints,
    build_parser,
    build_service,
)


def _test_resolution(*, windows: bool = False) -> ShellResolution:
    return ShellResolution(
        runtime=RuntimeName.WINDOWS_PWSH if windows else RuntimeName.POSIX_BASH,
        dialect=DialectName.PWSH if windows else DialectName.BASH,
        executable=r"C:\Program Files\PowerShell\7\pwsh.exe" if windows else "/bin/bash",
        version="7.6.3" if windows else "5.2",
        edition="Core" if windows else None,
        source="test",
    )


def test_overview_subscription_hub_coalesces_burst_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_OVERVIEW_NOTIFICATION_DEBOUNCE_SECONDS", 0.01)

    class FakeOverviewService:
        def __init__(self) -> None:
            self.listeners: set[Callable[[], None]] = set()

        def subscribe_overview_changes(
            self,
            listener: Callable[[], None],
        ) -> Callable[[], None]:
            self.listeners.add(listener)
            return lambda: self.listeners.discard(listener)

    class FakeSession:
        def __init__(self) -> None:
            self.updates: list[str] = []

        async def send_resource_updated(self, uri: AnyUrl) -> None:
            self.updates.append(str(uri))

    async def scenario() -> None:
        service = FakeOverviewService()
        session = FakeSession()
        hub = _OverviewSubscriptionHub()
        hub.connect(cast(Any, service))
        hub.subscribe(session)

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(hub.run)
            for _ in range(10):
                for listener in tuple(service.listeners):
                    listener()
            await anyio.sleep(0.05)
            assert session.updates == ["window://io.github.a2c-smcp.tfbash/shell-overview"]
            hub.stop()

        assert not service.listeners

    anyio.run(scenario)


def test_cli_accepts_only_stdio_and_positive_resource_limits() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--transport", "http"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-command-shells", "0"])


def test_cli_has_no_language_specific_environment_metadata_options() -> None:
    parser = build_parser()
    destinations = {action.dest for action in parser._actions}

    assert "environment_kind" not in destinations
    assert "environment_name" not in destinations


def test_stdio_entry_creates_the_shared_embedded_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object()
    configured_server = object()
    observed: list[object] = []

    class FakeRuntime:
        @property
        def _tool_service(self) -> object:
            return service

        async def __aenter__(self) -> FakeRuntime:
            return self

        async def __aexit__(self, *args: object) -> None:
            observed.append("closed")

    async def fake_create(_config: object) -> FakeRuntime:
        observed.append("created")
        return FakeRuntime()

    def fake_create_server(received: object, *, shutdown_on_exit: bool) -> object:
        assert received is service
        assert not shutdown_on_exit
        return configured_server

    async def fake_run_stdio(received: object) -> None:
        assert received is configured_server
        observed.append("served")

    monkeypatch.setattr(
        embedded_module.EmbeddedShellRuntime,
        "_create_from_runtime_config",
        staticmethod(fake_create),
    )
    monkeypatch.setattr(server_module, "create_server", fake_create_server)
    monkeypatch.setattr(server_module, "_run_stdio", fake_run_stdio)

    config = server_module._runtime_config_from_arguments(build_parser().parse_args([]))
    anyio.run(server_module._run_stdio_runtime, config)

    assert observed == ["created", "served", "closed"]


def test_cli_cross_option_deadlines_are_validated() -> None:
    parser = build_parser()
    too_short_close = parser.parse_args(
        ["--shutdown-grace-ms", "5000", "--close-timeout-ms", "5000"]
    )
    too_long_quiet = parser.parse_args(
        ["--output-quiet-ms", "3000", "--job-cleanup-timeout-ms", "3000"]
    )

    with pytest.raises(RuntimeConfigurationError, match="close-timeout-ms"):
        _validate_cross_option_constraints(too_short_close)
    with pytest.raises(RuntimeConfigurationError, match="output-quiet-ms"):
        _validate_cross_option_constraints(too_long_quiet)


def test_ide_host_requires_an_explicit_workspace_root() -> None:
    arguments = build_parser().parse_args(["--host-profile", "ide"])

    with pytest.raises(RuntimeConfigurationError, match="workspace_root"):
        build_service(
            arguments,
            operating_system="linux",
            process_cwd=str(Path.cwd()),
            inherited_environment={},
        )


def test_windows_runtime_composes_the_native_profile_after_issue_15_gate() -> None:
    runtime = _build_windows_runtime(
        shutdown_grace_ms=1234,
        close_timeout_ms=5000,
        shell_startup_timeout_ms=6789,
        max_read_buffer_bytes=123_456,
        max_write_buffer_bytes=65_432,
    )

    assert isinstance(runtime, WindowsPwshProfile)
    assert isinstance(runtime.dialect, PowerShellDialect)
    assert isinstance(runtime.transport, ConPtyTransport)
    assert isinstance(runtime.supervisor, WindowsProcessSupervisor)
    assert runtime.transport._max_read_buffer_bytes == 123_456
    assert runtime.transport._max_write_buffer_bytes == 65_432
    assert runtime.transport._close_timeout_ms == 5000
    assert runtime.supervisor._terminate_grace_ms == 1234
    assert runtime.supervisor._attach_cleanup_timeout_ms == 5000
    assert runtime.supervisor._gate_wait_timeout_ms == 6789
    assert runtime.supervisor._shell_ready_timeout_ms == 6789


def test_build_service_selects_the_production_windows_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        composition_module,
        "resolve_shell",
        lambda *args, **kwargs: _test_resolution(windows=True),
    )
    arguments = build_parser().parse_args(
        [
            "--runtime-profile",
            "pwsh",
            "--shell",
            r"C:\Program Files\PowerShell\7\pwsh.exe",
        ]
    )

    service = build_service(
        arguments,
        operating_system="Windows",
        process_cwd=r"C:\workspace",
        inherited_environment={"Path": r"C:\Windows\System32"},
    )

    manager = cast(CommandShellManager, service._manager)
    assert isinstance(manager._profile, WindowsPwshProfile)
    assert service._composition.host.runtime_profile.value == "windows-pwsh"
    service.shutdown()


def test_shell_version_probe_reports_a_redacted_configuration_error() -> None:
    missing = str(Path.cwd() / "secret-shell-name-that-does-not-exist")

    with pytest.raises(RuntimeConfigurationError) as captured:
        _probe_shell_version(
            missing,
            windows=False,
            cwd=str(Path.cwd()),
            environment={},
            timeout_ms=100,
        )

    assert str(captured.value) == "configured shell version probe failed"
    assert "secret-shell-name" not in str(captured.value)


@pytest.mark.parametrize(
    ("status", "exit_code", "cwd", "output"),
    [
        ("running", 37, "/workspace", "TFBASH_MANAGED_中文🙂\nline1\nline2\n环境中文🙂\n"),
        ("exited", 0, "/workspace", "TFBASH_MANAGED_中文🙂\nline1\nline2\n环境中文🙂\n"),
        ("exited", 37, "/wrong-workspace", "TFBASH_MANAGED_中文🙂\nline1\nline2\n环境中文🙂\n"),
        ("exited", 37, "/workspace", "TFBASH_MANAGED_中文🙂 line1 line2 环境中文🙂\n"),
        ("exited", 37, "/workspace", "wrong-marker\nline1\nline2\n环境中文🙂\n"),
        ("exited", 37, "/workspace", "TFBASH_MANAGED_中文🙂\nline1\nline2\nwrong-env\n"),
    ],
)
def test_managed_probe_requires_exact_contract_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    exit_code: int,
    cwd: str,
    output: str,
) -> None:
    closed: list[str] = []
    shutdown: list[bool] = []

    class FakeManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def open_shell(self, request: ShellStartRequest) -> SimpleNamespace:
            assert request.startup_command is None
            return SimpleNamespace(shell_id="shell_1")

        def exec(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status=SimpleNamespace(value=status),
                exit_code=exit_code,
                cwd=cwd,
                output=output,
            )

        def close_shell(self, shell_id: str) -> None:
            closed.append(shell_id)

        def shutdown(self) -> None:
            shutdown.append(True)

    monkeypatch.setattr(composition_module, "CommandShellManager", FakeManager)

    with pytest.raises(RuntimeConfigurationError, match="managed shell capability"):
        _probe_managed_candidate(
            _test_resolution(),
            arguments=build_parser().parse_args([]),
            cwd="/workspace",
            environment={},
        )

    assert closed == ["shell_1"]
    assert shutdown == [True]


def test_managed_probe_cleanup_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown: list[bool] = []

    class FakeManager:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def open_shell(self, _request: object) -> SimpleNamespace:
            return SimpleNamespace(shell_id="shell_1")

        def exec(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status=SimpleNamespace(value="exited"),
                exit_code=37,
                cwd="/workspace",
                output="TFBASH_MANAGED_中文🙂\nline1\nline2\n环境中文🙂\n",
            )

        def close_shell(self, _shell_id: str) -> None:
            raise OSError("native handle remains")

        def shutdown(self) -> None:
            shutdown.append(True)

    monkeypatch.setattr(composition_module, "CommandShellManager", FakeManager)

    with pytest.raises(RoutingCleanupError, match="cleanup failed"):
        _probe_managed_candidate(
            _test_resolution(),
            arguments=build_parser().parse_args([]),
            cwd="/workspace",
            environment={},
        )

    assert shutdown == [True]


def test_agent_visible_schema_omits_shell_and_startup_command_defaults() -> None:
    secret_shell = "/secret/interpreter"
    secret_startup = "export TOKEN=topsecret"
    contracts = tool_contract_schemas(
        ProtocolConfig(
            platform=PlatformName.LINUX,
            default_cwd="/workspace",
            shell=secret_shell,
            startup_command=secret_startup,
        )
    )

    _redact_sensitive_schema_defaults(contracts)

    serialized = json.dumps(contracts)
    open_properties = contracts[ToolName.SHELL_OPEN.value]["inputSchema"]["properties"]
    assert isinstance(open_properties, dict)
    assert "shell" not in open_properties
    assert "default" not in open_properties["startup_command"]
    assert secret_shell not in serialized
    assert secret_startup not in serialized


def test_close_timeout_is_hard_deadline_and_shutdown_grace_configures_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        composition_module,
        "resolve_shell",
        lambda *args, **kwargs: _test_resolution(),
    )
    arguments = build_parser().parse_args(
        ["--close-timeout-ms", "5000", "--shutdown-grace-ms", "1234"]
    )
    service = build_service(
        arguments,
        operating_system="linux",
        process_cwd="/workspace",
        inherited_environment={},
    )
    manager = cast(CommandShellManager, service._manager)
    runtime = _build_posix_runtime(shutdown_grace_ms=1234)
    supervisor = runtime.supervisor
    assert isinstance(supervisor, PosixProcessSupervisor)

    assert manager._config.worker.cleanup_deadline_ms == 5000
    assert supervisor.shutdown_grace_ms == 1234
    service.shutdown()


def test_saturated_wait_lane_cannot_starve_close_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(
        composition_module,
        "resolve_shell",
        lambda *args, **kwargs: _test_resolution(),
    )

    class BlockingExecManager:
        def __init__(self) -> None:
            self.active = Event()
            self.release = Event()

        def exec(self, *args: object, **kwargs: object) -> ExecutionSnapshot:
            del args, kwargs
            self.active.set()
            assert self.release.wait(5)
            return ExecutionSnapshot(
                shell_id="shell_1",
                exec_id="exec_1",
                status=ExecutionState.CANCELLED,
                exit_code=None,
                output="",
                buffer_start_cursor=0,
                next_cursor=0,
                truncated_before_cursor=False,
                eof=True,
                duration_ms=1,
                cwd=None,
                shell_status=ShellState.CLOSING,
                shell_rebuilt=False,
            )

        def close_shell(self, shell_id: str) -> bool:
            assert shell_id == "shell_1"
            self.release.set()
            return True

        def snapshots(self) -> tuple[ShellSnapshot, ...]:
            return (
                ShellSnapshot(
                    "shell_1",
                    ShellState.BUSY,
                    str(Path.cwd()),
                    "exec_1" if self.active.is_set() else None,
                    1,
                ),
            )

        def shutdown(self) -> None:
            self.release.set()

    async def scenario() -> None:
        arguments = build_parser().parse_args(
            ["--close-timeout-ms", "5000", "--shutdown-grace-ms", "100"]
        )
        service = build_service(
            arguments,
            operating_system="linux",
            process_cwd="/workspace",
            inherited_environment={},
        )
        service.shutdown()
        manager = BlockingExecManager()
        service._manager = cast(Any, manager)
        service._concurrency_limits = ToolConcurrencyLimits(
            wait_threads=1,
            control_threads=1,
            close_threads=1,
            metadata_threads=1,
        )
        limiters = _create_tool_limiters(service)

        async def long_execution() -> None:
            await _run_tool_call(
                service,
                "shell_exec",
                {"shell_id": "shell_1", "command": "sleep 30", "yield_ms": 60_000},
                limiters,
            )

        async with anyio.create_task_group() as calls:
            calls.start_soon(long_execution)
            await anyio.to_thread.run_sync(manager.active.wait)

            close_started = time.monotonic()
            closed = await _run_tool_call(
                service,
                "shell_close",
                {"shell_id": "shell_1"},
                limiters,
            )
            assert time.monotonic() - close_started < 5.5
            assert closed.structuredContent is not None
            assert closed.structuredContent["status"] == "closed"

        manager.shutdown()

    anyio.run(scenario)
