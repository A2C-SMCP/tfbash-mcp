"""Production composition root and stdio MCP server."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from threading import Event, Lock
from typing import Any, Protocol

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from tfbash_mcp import __version__
from tfbash_mcp.composition import (
    ShellRuntimeConfig,
    build_shell_service,
)
from tfbash_mcp.composition import (
    _build_posix_runtime as _composition_build_posix_runtime,
)
from tfbash_mcp.composition import (
    _build_windows_runtime as _composition_build_windows_runtime,
)
from tfbash_mcp.composition import (
    _probe_managed_candidate as _composition_probe_managed_candidate,
)
from tfbash_mcp.embedded import EmbeddedShellRuntime
from tfbash_mcp.mcp_adapter import (
    ShellToolService,
    ToolConcurrencyBudget,
    call_tool_async,
    tool_definitions,
)
from tfbash_mcp.mcp_adapter import (
    _redact_sensitive_schema_defaults as _adapter_redact_sensitive_schema_defaults,
)
from tfbash_mcp.protocol import ToolName
from tfbash_mcp.resource_adapter import SHELL_OVERVIEW_URI, ShellResourceAdapter
from tfbash_mcp.runtime import (
    HostProfile,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimeSelection,
    ShellResolution,
)

# Inert descriptor retained for import-time discovery. ``main`` builds the
# configured process instance only after CLI and host validation succeed.
server = Server("tfbash-mcp", version=__version__)

_OVERVIEW_NOTIFICATION_DEBOUNCE_SECONDS = 0.1

# Private compatibility exports retained for existing integrators and tests.
_build_posix_runtime = _composition_build_posix_runtime
_build_windows_runtime = _composition_build_windows_runtime
_redact_sensitive_schema_defaults = _adapter_redact_sensitive_schema_defaults


class _ResourceUpdateSession(Protocol):
    async def send_resource_updated(self, uri: AnyUrl) -> None: ...


class _ShellOverviewServer(Server[ShellToolService, object]):
    """Low-level Server with the subscription capability required by Desktop."""

    def get_capabilities(
        self,
        notification_options: NotificationOptions,
        experimental_capabilities: dict[str, dict[str, Any]],
    ) -> types.ServerCapabilities:
        capabilities = super().get_capabilities(
            notification_options,
            experimental_capabilities,
        )
        resources = capabilities.resources
        if resources is None:
            return capabilities
        return capabilities.model_copy(
            update={
                "resources": types.ResourcesCapability(
                    subscribe=True,
                    listChanged=resources.listChanged,
                )
            }
        )


class _OverviewSubscriptionHub:
    """Bridge synchronous Domain events to subscribed async MCP sessions."""

    def __init__(self) -> None:
        self._changed = Event()
        self._stopped = Event()
        self._sessions: dict[int, _ResourceUpdateSession] = {}
        self._lock = Lock()
        self._unsubscribe_changes: Callable[[], None] | None = None

    def connect(self, resources: ShellResourceAdapter) -> None:
        if self._unsubscribe_changes is not None:
            raise RuntimeError("overview subscription hub is already connected")
        self._stopped.clear()
        self._changed.clear()
        self._unsubscribe_changes = resources.subscribe_updates(lambda _uri: self._changed.set())

    def subscribe(self, session: _ResourceUpdateSession) -> None:
        with self._lock:
            self._sessions[id(session)] = session

    def unsubscribe(self, session: _ResourceUpdateSession) -> None:
        with self._lock:
            self._sessions.pop(id(session), None)

    def stop(self) -> None:
        if self._unsubscribe_changes is not None:
            self._unsubscribe_changes()
            self._unsubscribe_changes = None
        with self._lock:
            self._sessions.clear()
        self._stopped.set()
        self._changed.set()

    async def run(self) -> None:
        resource_uri = AnyUrl(SHELL_OVERVIEW_URI)
        while True:
            await anyio.to_thread.run_sync(self._changed.wait)
            self._changed.clear()
            if self._stopped.is_set():
                return
            await anyio.sleep(_OVERVIEW_NOTIFICATION_DEBOUNCE_SECONDS)
            # All events observed during the debounce window are represented by the
            # latest Resource contents and therefore need only one notification.
            self._changed.clear()
            if self._stopped.is_set():
                return
            with self._lock:
                sessions = tuple(self._sessions.values())
            for session in sessions:
                try:
                    await session.send_resource_updated(resource_uri)
                except Exception:
                    self.unsubscribe(session)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tfbash-mcp")
    parser.add_argument("--transport", choices=("stdio",), default="stdio")
    parser.add_argument(
        "--runtime-profile",
        choices=tuple(value.value for value in RuntimeSelection),
        default=RuntimeSelection.AUTO.value,
    )
    parser.add_argument(
        "--host-profile",
        choices=tuple(value.value for value in HostProfile),
        default=HostProfile.STANDALONE.value,
    )
    parser.add_argument("--workspace-root")
    parser.add_argument("--default-cwd")
    parser.add_argument("--shell")
    parser.add_argument("--shell-startup-timeout-ms", type=_positive_integer, default=30_000)
    parser.add_argument("--command-yield-ms", type=int, default=10_000)
    parser.add_argument("--command-timeout-ms", type=_positive_integer, default=120_000)
    parser.add_argument("--recovery-grace-ms", type=_positive_integer, default=1_000)
    parser.add_argument("--job-cleanup-timeout-ms", type=_positive_integer, default=3_000)
    parser.add_argument("--output-quiet-ms", type=_positive_integer, default=50)
    parser.add_argument("--max-command-bytes", type=_positive_integer, default=262_144)
    parser.add_argument("--max-command-shells", type=_positive_integer, default=8)
    parser.add_argument("--max-retained-executions", type=_positive_integer, default=128)
    parser.add_argument("--output-buffer-bytes", type=_positive_integer, default=4_194_304)
    parser.add_argument("--max-read-bytes", type=_positive_integer, default=65_536)
    parser.add_argument("--max-read-waiters-per-execution", type=_positive_integer, default=32)
    parser.add_argument("--max-write-bytes", type=_positive_integer, default=65_536)
    parser.add_argument("--max-pending-operations", type=_positive_integer, default=128)
    parser.add_argument("--max-pending-write-bytes", type=_positive_integer, default=262_144)
    parser.add_argument("--completed-retention-ms", type=_positive_integer, default=600_000)
    parser.add_argument("--shutdown-grace-ms", type=_positive_integer, default=3_000)
    parser.add_argument("--close-timeout-ms", type=_positive_integer, default=5_000)
    parser.add_argument("--startup-command")
    return parser


def build_service(
    arguments: argparse.Namespace,
    *,
    operating_system: str | None = None,
    process_cwd: str | None = None,
    inherited_environment: dict[str, str] | None = None,
) -> ShellToolService:
    """Compatibility wrapper around the shared stdio/embedded composition root."""

    return build_shell_service(
        _runtime_config_from_arguments(
            arguments,
            operating_system=operating_system,
            process_cwd=process_cwd,
            inherited_environment=inherited_environment,
        )
    )


def _runtime_config_from_arguments(
    arguments: argparse.Namespace,
    *,
    operating_system: str | None = None,
    process_cwd: str | None = None,
    inherited_environment: dict[str, str] | None = None,
) -> ShellRuntimeConfig:
    return ShellRuntimeConfig(
        host_profile=HostProfile(arguments.host_profile),
        runtime_profile=RuntimeSelection(arguments.runtime_profile),
        operating_system=operating_system or platform.system(),
        process_cwd=process_cwd or os.getcwd(),
        environment=(dict(os.environ) if inherited_environment is None else inherited_environment),
        workspace_root=arguments.workspace_root,
        default_cwd=arguments.default_cwd,
        shell=arguments.shell,
        startup_command=arguments.startup_command,
        shell_startup_timeout_ms=arguments.shell_startup_timeout_ms,
        command_yield_ms=arguments.command_yield_ms,
        command_timeout_ms=arguments.command_timeout_ms,
        recovery_grace_ms=arguments.recovery_grace_ms,
        job_cleanup_timeout_ms=arguments.job_cleanup_timeout_ms,
        output_quiet_ms=arguments.output_quiet_ms,
        max_command_bytes=arguments.max_command_bytes,
        max_command_shells=arguments.max_command_shells,
        max_retained_executions=arguments.max_retained_executions,
        output_buffer_bytes=arguments.output_buffer_bytes,
        max_read_bytes=arguments.max_read_bytes,
        max_read_waiters_per_execution=arguments.max_read_waiters_per_execution,
        max_write_bytes=arguments.max_write_bytes,
        max_pending_operations=arguments.max_pending_operations,
        max_pending_write_bytes=arguments.max_pending_write_bytes,
        completed_retention_ms=arguments.completed_retention_ms,
        shutdown_grace_ms=arguments.shutdown_grace_ms,
        close_timeout_ms=arguments.close_timeout_ms,
    )


def create_server(
    service: ShellToolService,
    *,
    shutdown_on_exit: bool = True,
) -> Server[ShellToolService, object]:
    """Register exact V1 contracts and one unified shutdown lifespan."""

    tool_budget = ToolConcurrencyBudget(service.concurrency_limits)
    shutdown_limiter = anyio.CapacityLimiter(1)
    resources = ShellResourceAdapter(service)
    overview_hub = _OverviewSubscriptionHub()

    @asynccontextmanager
    async def lifespan(_: Server[ShellToolService, object]) -> AsyncIterator[ShellToolService]:
        overview_hub.connect(resources)
        try:
            async with anyio.create_task_group() as notifications:
                notifications.start_soon(overview_hub.run)
                try:
                    yield service
                finally:
                    overview_hub.stop()
        finally:
            if shutdown_on_exit:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(
                        service.shutdown,
                        limiter=shutdown_limiter,
                    )

    configured = _ShellOverviewServer(
        "tfbash-mcp",
        version=__version__,
        instructions=service.instructions,
        lifespan=lifespan,
    )

    @configured.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[types.Tool]:
        return list(tool_definitions(service))

    @configured.call_tool(validate_input=False)  # type: ignore[misc]
    async def call_tool(name: str, arguments: dict[str, object]) -> types.CallToolResult:
        return await call_tool_async(service, name, arguments, tool_budget)

    @configured.list_resources()  # type: ignore[no-untyped-call,misc]
    async def list_resources() -> list[types.Resource]:
        return list(resources.list_resources())

    @configured.read_resource()  # type: ignore[no-untyped-call,misc]
    async def read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        result = resources.read_resource(uri)
        content = result.contents[0]
        if not isinstance(content, types.TextResourceContents):
            raise TypeError("Shell Overview Resource must contain text")
        return [
            ReadResourceContents(
                content=content.text,
                mime_type=content.mimeType,
            )
        ]

    @configured.subscribe_resource()  # type: ignore[no-untyped-call,misc]
    async def subscribe_resource(uri: AnyUrl) -> None:
        resources.validate_resource_uri(uri)
        overview_hub.subscribe(configured.request_context.session)

    @configured.unsubscribe_resource()  # type: ignore[no-untyped-call,misc]
    async def unsubscribe_resource(uri: AnyUrl) -> None:
        resources.validate_resource_uri(uri)
        overview_hub.unsubscribe(configured.request_context.session)

    return configured


def _create_tool_limiters(
    service: ShellToolService,
) -> dict[ToolName, anyio.CapacityLimiter]:
    limits = service.concurrency_limits
    wait_limiter = anyio.CapacityLimiter(limits.wait_threads)
    control_limiter = anyio.CapacityLimiter(limits.control_threads)
    close_limiter = anyio.CapacityLimiter(limits.close_threads)
    metadata_limiter = anyio.CapacityLimiter(limits.metadata_threads)
    return {
        ToolName.SHELL_OPEN: wait_limiter,
        ToolName.SHELL_EXEC: wait_limiter,
        ToolName.SHELL_READ: wait_limiter,
        ToolName.SHELL_WRITE: control_limiter,
        ToolName.SHELL_SIGNAL: control_limiter,
        ToolName.SHELL_LIST: metadata_limiter,
        ToolName.SHELL_CLOSE: close_limiter,
    }


async def _run_tool_call(
    service: ShellToolService,
    name: str,
    arguments: dict[str, object],
    limiters: dict[ToolName, anyio.CapacityLimiter],
) -> types.CallToolResult:
    try:
        tool = ToolName(name)
    except ValueError:
        tool = ToolName.SHELL_LIST
    return await anyio.to_thread.run_sync(
        service.call,
        name,
        arguments,
        abandon_on_cancel=True,
        limiter=limiters[tool],
    )


def _probe_managed_candidate(
    resolution: ShellResolution,
    *,
    arguments: argparse.Namespace,
    cwd: str,
    environment: dict[str, str],
) -> None:
    operating_system = "Windows" if resolution.runtime is RuntimeName.WINDOWS_PWSH else "Linux"
    _composition_probe_managed_candidate(
        resolution,
        config=_runtime_config_from_arguments(
            arguments,
            operating_system=operating_system,
            process_cwd=cwd,
            inherited_environment=environment,
        ),
        cwd=cwd,
        environment=environment,
    )


def _probe_shell_version(
    executable: str,
    *,
    windows: bool,
    cwd: str,
    environment: dict[str, str],
    timeout_ms: int,
) -> str:
    command = (
        [executable, "-NoLogo", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]
        if windows
        else [executable, "--version"]
    )
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_ms / 1000,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("configured shell version probe failed") from error
    output = completed.stdout if completed.returncode == 0 else ""
    version = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not version:
        raise RuntimeConfigurationError("configured shell did not report a version")
    return version


def _validate_cross_option_constraints(arguments: argparse.Namespace) -> None:
    if not 0 <= arguments.command_yield_ms <= 60_000:
        raise RuntimeConfigurationError("command-yield-ms must be between 0 and 60000")
    if arguments.close_timeout_ms <= arguments.shutdown_grace_ms:
        raise RuntimeConfigurationError("close-timeout-ms must exceed shutdown-grace-ms")
    if arguments.output_quiet_ms >= arguments.job_cleanup_timeout_ms:
        raise RuntimeConfigurationError(
            "output-quiet-ms must be shorter than job-cleanup-timeout-ms"
        )


async def _run_stdio(configured: Server[ShellToolService, object]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await configured.run(
            read_stream,
            write_stream,
            configured.create_initialization_options(),
        )


async def _run_stdio_runtime(config: ShellRuntimeConfig) -> None:
    runtime = await EmbeddedShellRuntime._create_from_runtime_config(config)
    async with runtime:
        await _run_stdio(create_server(runtime._tool_service, shutdown_on_exit=False))


def main(argv: Sequence[str] | None = None) -> None:
    """Validate configuration, then serve MCP until stdin EOF or cancellation."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        anyio.run(_run_stdio_runtime, _runtime_config_from_arguments(arguments))
    except (RuntimeConfigurationError, ValueError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main(sys.argv[1:])
