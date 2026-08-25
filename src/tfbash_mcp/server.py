"""Production composition root and stdio MCP server."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from tfbash_mcp import __version__
from tfbash_mcp.domain import CommandShellManager, ManagerConfig, WorkerConfig
from tfbash_mcp.mcp_adapter import ShellToolService, ToolConcurrencyLimits
from tfbash_mcp.protocol import PlatformName, ProtocolConfig, ToolName, tool_contract_schemas
from tfbash_mcp.runtime import (
    BashDialect,
    ConPtyTransport,
    EnvironmentKind,
    EnvironmentSummary,
    HostProfile,
    PexpectPosixPtyTransport,
    PosixBashProfile,
    PosixProcessSupervisor,
    PowerShellDialect,
    RuntimeBuilders,
    RuntimeConfigurationError,
    RuntimeProfile,
    RuntimeSelection,
    WindowsProcessSupervisor,
    WindowsPwshProfile,
    compose_runtime,
    create_host_config,
)

# Inert descriptor retained for import-time discovery. ``main`` builds the
# configured process instance only after CLI and host validation succeed.
server = Server("tfbash-mcp", version=__version__)


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
    parser.add_argument(
        "--environment-kind",
        choices=tuple(value.value for value in EnvironmentKind),
        default=EnvironmentKind.NONE.value,
    )
    parser.add_argument("--environment-name")
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
    """Freeze host inputs and build one complete runtime before serving MCP."""

    _validate_cross_option_constraints(arguments)
    os_name = operating_system or platform.system()
    cwd = process_cwd or os.getcwd()
    environment = dict(os.environ) if inherited_environment is None else inherited_environment
    host = create_host_config(
        host_profile=HostProfile(arguments.host_profile),
        runtime_selection=RuntimeSelection(arguments.runtime_profile),
        operating_system=os_name,
        process_cwd=cwd,
        inherited_environment=environment,
        workspace_root=arguments.workspace_root,
        default_cwd=arguments.default_cwd,
        default_shell=arguments.shell,
        startup_command=arguments.startup_command,
        environment_summary=EnvironmentSummary(
            kind=EnvironmentKind(arguments.environment_kind),
            name=arguments.environment_name,
        ),
        directory_exists=os.path.isdir,
    )
    composition = compose_runtime(
        host,
        RuntimeBuilders(
            posix_bash=lambda: _build_posix_runtime(shutdown_grace_ms=arguments.shutdown_grace_ms),
            windows_pwsh=lambda: _build_windows_runtime(
                shutdown_grace_ms=arguments.shutdown_grace_ms,
                close_timeout_ms=arguments.close_timeout_ms,
                shell_startup_timeout_ms=arguments.shell_startup_timeout_ms,
                max_read_buffer_bytes=arguments.output_buffer_bytes,
                max_write_buffer_bytes=arguments.max_pending_write_bytes,
            ),
        ),
    )
    executable = host.default_shell or composition.runtime.dialect.default_executable
    shell_version = _probe_shell_version(
        executable,
        windows=host.platform.value == "windows",
        cwd=host.default_cwd,
        environment=dict(host.environment),
        timeout_ms=arguments.shell_startup_timeout_ms,
    )
    protocol_config = ProtocolConfig(
        platform=PlatformName(host.platform.value),
        default_cwd=host.default_cwd,
        shell=executable,
        startup_command=host.startup_command,
        command_yield_ms=arguments.command_yield_ms,
        command_timeout_ms=arguments.command_timeout_ms,
        max_command_bytes=arguments.max_command_bytes,
        output_buffer_bytes=arguments.output_buffer_bytes,
        max_read_bytes=arguments.max_read_bytes,
        max_write_bytes=arguments.max_write_bytes,
    )
    worker_config = WorkerConfig(
        startup_deadline_ms=arguments.shell_startup_timeout_ms,
        recovery_deadline_ms=arguments.recovery_grace_ms,
        cleanup_deadline_ms=arguments.close_timeout_ms,
        job_cleanup_deadline_ms=arguments.job_cleanup_timeout_ms,
        output_quiet_ms=arguments.output_quiet_ms,
        operation_deadline_ms=arguments.close_timeout_ms,
        rebuild_deadline_ms=arguments.shell_startup_timeout_ms,
        max_pending_operations=arguments.max_pending_operations,
        max_pending_write_bytes=arguments.max_pending_write_bytes,
    )
    manager = CommandShellManager(
        profile=composition.runtime,
        config=ManagerConfig(
            max_shells=arguments.max_command_shells,
            max_retained_executions=arguments.max_retained_executions,
            completed_retention_ms=arguments.completed_retention_ms,
            max_output_bytes=arguments.output_buffer_bytes,
            max_read_bytes=arguments.max_read_bytes,
            max_write_bytes=arguments.max_write_bytes,
            max_read_waiters_per_execution=arguments.max_read_waiters_per_execution,
            worker=worker_config,
        ),
    )
    return ShellToolService(
        manager=manager,
        composition=composition,
        protocol_config=protocol_config,
        agent_context=composition.agent_context(shell_version=shell_version),
        directory_exists=os.path.isdir,
        concurrency_limits=ToolConcurrencyLimits(
            wait_threads=arguments.max_command_shells
            * (arguments.max_read_waiters_per_execution + 2),
            control_threads=arguments.max_command_shells * (arguments.max_pending_operations + 1),
            close_threads=arguments.max_command_shells,
            metadata_threads=arguments.max_command_shells + 1,
        ),
    )


def create_server(service: ShellToolService) -> Server[ShellToolService, object]:
    """Register exact V1 contracts and one unified shutdown lifespan."""

    tool_limiters = _create_tool_limiters(service)
    shutdown_limiter = anyio.CapacityLimiter(1)

    @asynccontextmanager
    async def lifespan(_: Server[ShellToolService, object]) -> AsyncIterator[ShellToolService]:
        try:
            yield service
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    service.shutdown,
                    limiter=shutdown_limiter,
                )

    configured = Server[ShellToolService, object](
        "tfbash-mcp",
        version=__version__,
        instructions=service.instructions,
        lifespan=lifespan,
    )
    contracts = tool_contract_schemas(service.protocol_config)
    _redact_sensitive_schema_defaults(contracts)
    descriptions = service.tool_descriptions

    @configured.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool.value,
                description=descriptions[tool.value],
                inputSchema=cast(dict[str, Any], contracts[tool.value]["inputSchema"]),
                outputSchema=cast(dict[str, Any], contracts[tool.value]["outputSchema"]),
                _meta={
                    "tfbash-mcp/errorSchema": cast(
                        dict[str, Any], contracts[tool.value]["errorSchema"]
                    )
                },
            )
            for tool in ToolName
        ]

    @configured.call_tool(validate_input=False)  # type: ignore[misc]
    async def call_tool(name: str, arguments: dict[str, object]) -> types.CallToolResult:
        return await _run_tool_call(service, name, arguments, tool_limiters)

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


def _build_posix_runtime(*, shutdown_grace_ms: int = 3000) -> RuntimeProfile:
    return PosixBashProfile(
        dialect=BashDialect(),
        transport=PexpectPosixPtyTransport(),
        supervisor=PosixProcessSupervisor(shutdown_grace_ms=shutdown_grace_ms),
    )


def _build_windows_runtime(
    *,
    shutdown_grace_ms: int = 3000,
    close_timeout_ms: int = 5000,
    shell_startup_timeout_ms: int = 30_000,
    max_read_buffer_bytes: int = 4 * 1024 * 1024,
    max_write_buffer_bytes: int = 256 * 1024,
) -> RuntimeProfile:
    return WindowsPwshProfile(
        dialect=PowerShellDialect(),
        transport=ConPtyTransport(
            max_read_buffer_bytes=max_read_buffer_bytes,
            max_write_buffer_bytes=max_write_buffer_bytes,
            close_timeout_ms=close_timeout_ms,
        ),
        supervisor=WindowsProcessSupervisor(
            terminate_grace_ms=shutdown_grace_ms,
            attach_cleanup_timeout_ms=close_timeout_ms,
            gate_wait_timeout_ms=shell_startup_timeout_ms,
            shell_ready_timeout_ms=shell_startup_timeout_ms,
        ),
    )


def _redact_sensitive_schema_defaults(
    contracts: dict[str, dict[str, dict[str, object]]],
) -> None:
    open_input = contracts[ToolName.SHELL_OPEN.value]["inputSchema"]
    properties = open_input.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeConfigurationError("shell_open schema has no properties")
    for name in ("shell", "startup_command"):
        field_schema = properties.get(name)
        if isinstance(field_schema, dict):
            field_schema.pop("default", None)


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


def main(argv: Sequence[str] | None = None) -> None:
    """Validate configuration, then serve MCP until stdin EOF or cancellation."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        service = build_service(arguments)
    except (RuntimeConfigurationError, ValueError) as error:
        parser.error(str(error))
    try:
        anyio.run(_run_stdio, create_server(service))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main(sys.argv[1:])
