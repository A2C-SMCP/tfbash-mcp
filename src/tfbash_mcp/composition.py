"""Shared composition root for stdio and in-process hosts."""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from tfbash_mcp.domain import CommandShellManager, ManagerConfig, WorkerConfig
from tfbash_mcp.mcp_adapter import ShellToolService, ToolConcurrencyLimits
from tfbash_mcp.protocol import DialectName as ProtocolDialectName
from tfbash_mcp.protocol import PlatformName, ProtocolConfig
from tfbash_mcp.runtime import (
    BashDialect,
    ConPtyTransport,
    HostProfile,
    NativePlatform,
    NativeRuntimeProfile,
    PexpectPosixPtyTransport,
    PosixBashProfile,
    PosixProcessSupervisor,
    PowerShellDialect,
    RuntimeBuilders,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimePlatform,
    RuntimeProfile,
    RuntimeSelection,
    ShellResolution,
    ShellStartRequest,
    WindowsProcessSupervisor,
    WindowsPwshProfile,
    ZshDialect,
    compose_runtime,
    create_host_config,
    resolve_shell,
)
from tfbash_mcp.runtime.contracts import DialectName as RuntimeDialectName
from tfbash_mcp.runtime.resolver import RoutingCleanupError, native_paths_equal


@dataclass(frozen=True, slots=True)
class ShellRuntimeConfig:
    """Frozen inputs used to compose one isolated shell runtime instance."""

    host_profile: HostProfile
    runtime_profile: RuntimeSelection
    operating_system: str
    process_cwd: str
    environment: Mapping[str, str] = field(repr=False)
    workspace_root: str | None = None
    default_cwd: str | None = None
    shell: str | None = field(default=None, repr=False)
    startup_command: str | None = field(default=None, repr=False)
    shell_startup_timeout_ms: int = 30_000
    command_yield_ms: int = 10_000
    command_timeout_ms: int = 120_000
    recovery_grace_ms: int = 1_000
    job_cleanup_timeout_ms: int = 3_000
    output_quiet_ms: int = 50
    max_command_bytes: int = 262_144
    max_command_shells: int = 8
    max_retained_executions: int = 128
    output_buffer_bytes: int = 4_194_304
    max_read_bytes: int = 65_536
    max_read_waiters_per_execution: int = 32
    max_write_bytes: int = 65_536
    max_pending_operations: int = 128
    max_pending_write_bytes: int = 262_144
    completed_retention_ms: int = 600_000
    shutdown_grace_ms: int = 3_000
    close_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


def build_shell_service(config: ShellRuntimeConfig) -> ShellToolService:
    """Validate frozen inputs and build one complete runtime before serving calls."""

    _validate_config(config)
    if config.host_profile is HostProfile.IDE and config.workspace_root is None:
        raise RuntimeConfigurationError("IDE hosts must provide workspace_root")
    probe_cwd = config.default_cwd or config.workspace_root or config.process_cwd
    if not os.path.isdir(probe_cwd):
        raise RuntimeConfigurationError("default_cwd must be an existing native absolute directory")
    native_platform = {
        "windows": NativePlatform.WINDOWS,
        "darwin": NativePlatform.MACOS,
        "linux": NativePlatform.LINUX,
    }.get(config.operating_system.casefold())
    if native_platform is None:
        raise RuntimeConfigurationError(
            f"unsupported operating system: {config.operating_system}"
        )
    if (
        native_platform is NativePlatform.WINDOWS
        and platform.system().casefold() == "windows"
        and platform.machine().casefold() not in {"amd64", "x86_64"}
    ):
        raise RuntimeConfigurationError("Windows runtime currently requires an x64 process")
    environment = dict(config.environment)
    resolution = resolve_shell(
        config.runtime_profile,
        platform=native_platform,
        explicit_shell=config.shell,
        cwd=probe_cwd,
        environment=environment,
        timeout_ms=config.shell_startup_timeout_ms,
        admit=lambda candidate: _probe_managed_candidate(
            candidate,
            config=config,
            cwd=probe_cwd,
            environment=environment,
        ),
    )
    host = create_host_config(
        host_profile=config.host_profile,
        runtime_selection=config.runtime_profile,
        operating_system=config.operating_system,
        process_cwd=config.process_cwd,
        inherited_environment=environment,
        workspace_root=config.workspace_root,
        default_cwd=config.default_cwd,
        default_shell=resolution.executable,
        startup_command=config.startup_command,
        resolved_runtime=resolution.runtime,
        directory_exists=os.path.isdir,
    )
    composition = compose_runtime(
        host,
        RuntimeBuilders(
            posix_bash=lambda: _build_posix_runtime(
                shutdown_grace_ms=config.shutdown_grace_ms,
                dialect=resolution.dialect,
                executable=resolution.executable,
            ),
            windows_pwsh=lambda: _build_windows_runtime(
                shutdown_grace_ms=config.shutdown_grace_ms,
                close_timeout_ms=config.close_timeout_ms,
                shell_startup_timeout_ms=config.shell_startup_timeout_ms,
                max_read_buffer_bytes=config.output_buffer_bytes,
                max_write_buffer_bytes=config.max_pending_write_bytes,
                dialect=resolution.dialect,
                executable=resolution.executable,
            ),
        ),
    )
    executable = host.default_shell or composition.runtime.dialect.default_executable
    shell_version = resolution.version + (f" ({resolution.edition})" if resolution.edition else "")
    protocol_config = ProtocolConfig(
        platform=PlatformName(host.platform.value),
        dialect=ProtocolDialectName(resolution.dialect.value),
        default_cwd=host.default_cwd,
        shell=executable,
        startup_command=host.startup_command,
        command_yield_ms=config.command_yield_ms,
        command_timeout_ms=config.command_timeout_ms,
        max_command_bytes=config.max_command_bytes,
        output_buffer_bytes=config.output_buffer_bytes,
        max_read_bytes=config.max_read_bytes,
        max_write_bytes=config.max_write_bytes,
    )
    worker_config = WorkerConfig(
        startup_deadline_ms=config.shell_startup_timeout_ms,
        recovery_deadline_ms=config.recovery_grace_ms,
        cleanup_deadline_ms=config.close_timeout_ms,
        job_cleanup_deadline_ms=config.job_cleanup_timeout_ms,
        output_quiet_ms=config.output_quiet_ms,
        operation_deadline_ms=config.close_timeout_ms,
        rebuild_deadline_ms=config.shell_startup_timeout_ms,
        max_pending_operations=config.max_pending_operations,
        max_pending_write_bytes=config.max_pending_write_bytes,
    )
    manager = CommandShellManager(
        profile=composition.runtime,
        config=ManagerConfig(
            max_shells=config.max_command_shells,
            max_retained_executions=config.max_retained_executions,
            completed_retention_ms=config.completed_retention_ms,
            max_output_bytes=config.output_buffer_bytes,
            max_read_bytes=config.max_read_bytes,
            max_write_bytes=config.max_write_bytes,
            max_read_waiters_per_execution=config.max_read_waiters_per_execution,
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
            wait_threads=config.max_command_shells
            * (config.max_read_waiters_per_execution + 2),
            control_threads=config.max_command_shells * (config.max_pending_operations + 1),
            close_threads=config.max_command_shells,
            metadata_threads=config.max_command_shells + 1,
        ),
    )


def _build_posix_runtime(
    *,
    shutdown_grace_ms: int = 3000,
    dialect: RuntimeDialectName = RuntimeDialectName.BASH,
    executable: str | None = None,
) -> RuntimeProfile:
    shell_dialect = _dialect(dialect, executable=executable, windows=False)
    transport = PexpectPosixPtyTransport()
    supervisor = PosixProcessSupervisor(shutdown_grace_ms=shutdown_grace_ms)
    if dialect is RuntimeDialectName.BASH:
        return PosixBashProfile(
            dialect=shell_dialect,
            transport=transport,
            supervisor=supervisor,
        )
    return NativeRuntimeProfile(
        name=RuntimeName.POSIX_BASH,
        platform=RuntimePlatform.POSIX,
        dialect=shell_dialect,
        transport=transport,
        supervisor=supervisor,
    )


def _build_windows_runtime(
    *,
    shutdown_grace_ms: int = 3000,
    close_timeout_ms: int = 5000,
    shell_startup_timeout_ms: int = 30_000,
    max_read_buffer_bytes: int = 4 * 1024 * 1024,
    max_write_buffer_bytes: int = 256 * 1024,
    dialect: RuntimeDialectName = RuntimeDialectName.PWSH,
    executable: str | None = None,
) -> RuntimeProfile:
    shell_dialect = _dialect(dialect, executable=executable, windows=True)
    transport = ConPtyTransport(
        max_read_buffer_bytes=max_read_buffer_bytes,
        max_write_buffer_bytes=max_write_buffer_bytes,
        close_timeout_ms=close_timeout_ms,
    )
    supervisor = WindowsProcessSupervisor(
        terminate_grace_ms=shutdown_grace_ms,
        attach_cleanup_timeout_ms=close_timeout_ms,
        gate_wait_timeout_ms=shell_startup_timeout_ms,
        shell_ready_timeout_ms=shell_startup_timeout_ms,
    )
    if dialect is RuntimeDialectName.PWSH:
        return WindowsPwshProfile(
            dialect=shell_dialect,
            transport=transport,
            supervisor=supervisor,
        )
    return NativeRuntimeProfile(
        name=RuntimeName.WINDOWS_PWSH,
        platform=RuntimePlatform.WINDOWS,
        dialect=shell_dialect,
        transport=transport,
        supervisor=supervisor,
    )


def _dialect(
    dialect: RuntimeDialectName,
    *,
    executable: str | None,
    windows: bool,
) -> BashDialect | ZshDialect | PowerShellDialect:
    if dialect is RuntimeDialectName.BASH:
        return BashDialect(
            default_executable=executable
            or (r"C:\Program Files\Git\bin\bash.exe" if windows else "/bin/bash"),
            windows_paths=windows,
        )
    if dialect is RuntimeDialectName.ZSH:
        return ZshDialect(
            default_executable=executable
            or (r"C:\msys64\usr\bin\zsh.exe" if windows else "/bin/zsh"),
            windows_paths=windows,
        )
    return PowerShellDialect(
        default_executable=executable
        or (r"C:\Program Files\PowerShell\7\pwsh.exe" if windows else "/usr/bin/pwsh"),
        windows_paths=windows,
    )


def _probe_managed_candidate(
    resolution: ShellResolution,
    *,
    config: ShellRuntimeConfig,
    cwd: str,
    environment: dict[str, str],
) -> None:
    """Admit one candidate through the real managed PTY/ConPTY lifecycle."""

    windows = resolution.runtime is RuntimeName.WINDOWS_PWSH
    profile = (
        _build_windows_runtime(
            shutdown_grace_ms=config.shutdown_grace_ms,
            close_timeout_ms=config.close_timeout_ms,
            shell_startup_timeout_ms=config.shell_startup_timeout_ms,
            max_read_buffer_bytes=config.output_buffer_bytes,
            max_write_buffer_bytes=config.max_pending_write_bytes,
            dialect=resolution.dialect,
            executable=resolution.executable,
        )
        if windows
        else _build_posix_runtime(
            shutdown_grace_ms=config.shutdown_grace_ms,
            dialect=resolution.dialect,
            executable=resolution.executable,
        )
    )
    probe_environment = dict(environment)
    probe_environment["TFBASH_MANAGED_PROBE"] = "环境中文🙂"
    manager = CommandShellManager(
        profile=profile,
        config=ManagerConfig(
            max_shells=1,
            max_retained_executions=1,
            completed_retention_ms=1_000,
            max_output_bytes=65_536,
            max_read_bytes=65_536,
            max_write_bytes=65_536,
            max_read_waiters_per_execution=1,
            worker=WorkerConfig(
                startup_deadline_ms=config.shell_startup_timeout_ms,
                recovery_deadline_ms=config.recovery_grace_ms,
                cleanup_deadline_ms=config.close_timeout_ms,
                job_cleanup_deadline_ms=config.job_cleanup_timeout_ms,
                output_quiet_ms=config.output_quiet_ms,
                operation_deadline_ms=config.close_timeout_ms,
                rebuild_deadline_ms=config.shell_startup_timeout_ms,
                max_pending_operations=8,
                max_pending_write_bytes=65_536,
            ),
        ),
    )
    shell_id: str | None = None
    try:
        opened = manager.open_shell(
            ShellStartRequest(
                executable=resolution.executable,
                cwd=cwd,
                environment=probe_environment,
                startup_command=None,
            )
        )
        shell_id = opened.shell_id
        marker = "TFBASH_MANAGED_中文🙂"
        if resolution.dialect is RuntimeDialectName.PWSH:
            native_exit = "& $env:ComSpec /d /c exit 37" if windows else "& /bin/sh -c 'exit 37'"
            command = (
                f"Write-Output '{marker}'; Write-Output 'line1'; Write-Output 'line2'; "
                "Write-Output $env:TFBASH_MANAGED_PROBE; "
                f"{native_exit}"
            )
        else:
            command = f"printf '%s\\n' '{marker}' line1 line2 \"$TFBASH_MANAGED_PROBE\"; (exit 37)"
        result = manager.exec(
            shell_id,
            command,
            yield_ms=config.shell_startup_timeout_ms,
            timeout_ms=config.shell_startup_timeout_ms,
            max_output_bytes=65_536,
        )
        if (
            result.status.value != "exited"
            or result.exit_code != 37
            or result.cwd is None
            or not native_paths_equal(result.cwd, cwd, windows=windows)
            or result.output.splitlines() != [marker, "line1", "line2", "环境中文🙂"]
        ):
            raise RuntimeConfigurationError("managed shell capability probe failed")
    finally:
        cleanup_error: Exception | None = None
        try:
            if shell_id is not None:
                manager.close_shell(shell_id)
        except Exception as error:
            cleanup_error = error
        try:
            manager.shutdown()
        except Exception as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise RoutingCleanupError("managed candidate cleanup failed") from cleanup_error


def _validate_config(config: ShellRuntimeConfig) -> None:
    positive_values = {
        "shell-startup-timeout-ms": config.shell_startup_timeout_ms,
        "command-timeout-ms": config.command_timeout_ms,
        "recovery-grace-ms": config.recovery_grace_ms,
        "job-cleanup-timeout-ms": config.job_cleanup_timeout_ms,
        "output-quiet-ms": config.output_quiet_ms,
        "max-command-bytes": config.max_command_bytes,
        "max-command-shells": config.max_command_shells,
        "max-retained-executions": config.max_retained_executions,
        "output-buffer-bytes": config.output_buffer_bytes,
        "max-read-bytes": config.max_read_bytes,
        "max-read-waiters-per-execution": config.max_read_waiters_per_execution,
        "max-write-bytes": config.max_write_bytes,
        "max-pending-operations": config.max_pending_operations,
        "max-pending-write-bytes": config.max_pending_write_bytes,
        "completed-retention-ms": config.completed_retention_ms,
        "shutdown-grace-ms": config.shutdown_grace_ms,
        "close-timeout-ms": config.close_timeout_ms,
    }
    invalid = next((name for name, value in positive_values.items() if value <= 0), None)
    if invalid is not None:
        raise RuntimeConfigurationError(f"{invalid} must be positive")
    if not 0 <= config.command_yield_ms <= 60_000:
        raise RuntimeConfigurationError("command-yield-ms must be between 0 and 60000")
    if config.close_timeout_ms <= config.shutdown_grace_ms:
        raise RuntimeConfigurationError("close-timeout-ms must exceed shutdown-grace-ms")
    if config.output_quiet_ms >= config.job_cleanup_timeout_ms:
        raise RuntimeConfigurationError(
            "output-quiet-ms must be shorter than job-cleanup-timeout-ms"
        )
