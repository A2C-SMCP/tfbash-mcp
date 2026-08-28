"""Public in-process runtime for IDE and SDK hosts."""

from __future__ import annotations

import logging
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType

import anyio
from mcp import types

from tfbash_mcp.composition import ShellRuntimeConfig, build_shell_service
from tfbash_mcp.mcp_adapter import (
    ShellToolService,
    ToolConcurrencyBudget,
    call_tool_async,
    tool_definitions,
)
from tfbash_mcp.runtime import HostProfile, RuntimeSelection

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddedShellConfig:
    """Immutable host inputs for one isolated embedded shell runtime."""

    workspace_root: str
    environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    default_cwd: str | None = None
    runtime_profile: RuntimeSelection | str = RuntimeSelection.AUTO
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
    operating_system: str = field(default_factory=platform.system)
    process_cwd: str = field(default_factory=os.getcwd)

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_profile", RuntimeSelection(self.runtime_profile))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def _runtime_config(self) -> ShellRuntimeConfig:
        runtime_profile = self.runtime_profile
        if not isinstance(runtime_profile, RuntimeSelection):
            raise TypeError("runtime_profile was not normalized")
        return ShellRuntimeConfig(
            host_profile=HostProfile.IDE,
            runtime_profile=runtime_profile,
            operating_system=self.operating_system,
            process_cwd=self.process_cwd,
            environment=self.environment,
            workspace_root=self.workspace_root,
            default_cwd=self.default_cwd,
            shell=self.shell,
            startup_command=self.startup_command,
            shell_startup_timeout_ms=self.shell_startup_timeout_ms,
            command_yield_ms=self.command_yield_ms,
            command_timeout_ms=self.command_timeout_ms,
            recovery_grace_ms=self.recovery_grace_ms,
            job_cleanup_timeout_ms=self.job_cleanup_timeout_ms,
            output_quiet_ms=self.output_quiet_ms,
            max_command_bytes=self.max_command_bytes,
            max_command_shells=self.max_command_shells,
            max_retained_executions=self.max_retained_executions,
            output_buffer_bytes=self.output_buffer_bytes,
            max_read_bytes=self.max_read_bytes,
            max_read_waiters_per_execution=self.max_read_waiters_per_execution,
            max_write_bytes=self.max_write_bytes,
            max_pending_operations=self.max_pending_operations,
            max_pending_write_bytes=self.max_pending_write_bytes,
            completed_retention_ms=self.completed_retention_ms,
            shutdown_grace_ms=self.shutdown_grace_ms,
            close_timeout_ms=self.close_timeout_ms,
        )


class EmbeddedShellRuntime:
    """Async lifecycle wrapper around one independent ShellToolService."""

    def __init__(
        self,
        service: ShellToolService,
        budget: ToolConcurrencyBudget,
    ) -> None:
        self._service = service
        self._budget = budget
        self._state_lock = Lock()
        self._state = "open"
        self._close_lock = anyio.Lock()
        self._close_complete = anyio.Event()
        self._shutdown_limiter = anyio.CapacityLimiter(1)

    @classmethod
    async def create(
        cls,
        config: EmbeddedShellConfig,
        *,
        concurrency_budget: ToolConcurrencyBudget | None = None,
    ) -> EmbeddedShellRuntime:
        """Compose and probe the native runtime without blocking the host loop."""

        return await cls._create_from_runtime_config(
            config._runtime_config(),
            concurrency_budget=concurrency_budget,
        )

    @classmethod
    async def _create_from_runtime_config(
        cls,
        config: ShellRuntimeConfig,
        *,
        concurrency_budget: ToolConcurrencyBudget | None = None,
    ) -> EmbeddedShellRuntime:
        service: ShellToolService
        with anyio.CancelScope(shield=True):
            service = await anyio.to_thread.run_sync(
                build_shell_service,
                config,
            )
        try:
            await anyio.lowlevel.checkpoint_if_cancelled()
            budget = concurrency_budget or ToolConcurrencyBudget(service.concurrency_limits)
            return cls(service, budget)
        except BaseException:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(service.shutdown)
                except BaseException:
                    _LOGGER.exception("embedded runtime cleanup failed during initialization")
            raise

    @property
    def _tool_service(self) -> ShellToolService:
        return self._service

    @property
    def instructions(self) -> str:
        return self._service.instructions

    def list_tools(self) -> tuple[types.Tool, ...]:
        self._require_open()
        return tool_definitions(self._service)

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> types.CallToolResult:
        self._require_open()
        return await call_tool_async(
            self._service,
            name,
            dict(arguments or {}),
            self._budget,
        )

    async def aclose(self) -> None:
        """Close once; failed cleanup remains retryable by a later call."""

        while True:
            async with self._close_lock:
                with self._state_lock:
                    if self._state == "closed":
                        return
                    if self._state in {"open", "close_failed"}:
                        self._state = "closing"
                        owner = True
                        completion = self._close_complete
                    else:
                        owner = False
                        completion = self._close_complete
            if not owner:
                await completion.wait()
                continue
            shutdown_error: BaseException | None = None
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(
                        self._service.shutdown,
                        limiter=self._shutdown_limiter,
                    )
                except BaseException as error:
                    shutdown_error = error
                async with self._close_lock:
                    with self._state_lock:
                        self._state = "close_failed" if shutdown_error is not None else "closed"
                        completion.set()
                        if shutdown_error is not None:
                            self._close_complete = anyio.Event()
            if shutdown_error is not None:
                raise shutdown_error
            await anyio.lowlevel.checkpoint_if_cancelled()
            return

    async def __aenter__(self) -> EmbeddedShellRuntime:
        self._require_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self.aclose()

    def _require_open(self) -> None:
        with self._state_lock:
            if self._state != "open":
                raise RuntimeError("embedded shell runtime is closing or closed")
