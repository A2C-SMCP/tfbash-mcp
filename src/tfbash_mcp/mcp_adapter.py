"""Strict MCP adapter for the seven public shell tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast

import anyio
from mcp import types
from pydantic import BaseModel

from tfbash_mcp.domain import (
    CapacityExceeded,
    ExecutionNotActive,
    ExecutionNotFound,
    InvalidCursor,
    InvalidTransition,
    ShellBusy,
    ShellClosing,
    ShellNotFound,
    ShellOverviewSnapshot,
    ShellSnapshot,
    ShellUnavailable,
)
from tfbash_mcp.domain import (
    ExecutionSnapshot as DomainExecutionSnapshot,
)
from tfbash_mcp.protocol import (
    ERROR_CODES_BY_TOOL,
    ErrorCode,
    ErrorEnvelope,
    ProtocolConfig,
    ProtocolValidationError,
    ShellCloseInput,
    ShellExecInput,
    ShellListInput,
    ShellOpenInput,
    ShellReadInput,
    ShellSignalInput,
    ShellWriteInput,
    ToolName,
    make_error,
    model_to_wire,
    tool_contract_schemas,
    validate_tool_input,
    validate_tool_output,
)
from tfbash_mcp.runtime import (
    STARTUP_COMMAND_UNSET,
    AgentContext,
    ControlIntent,
    RuntimeBoundaryError,
    RuntimeComposition,
    RuntimeConfigurationError,
    ShellOpenOverrides,
    ShellStartRequest,
    UnsupportedShell,
)

_LOGGER = logging.getLogger(__name__)

SHELL_OVERVIEW_URI = "window://io.github.a2c-smcp.tfbash/shell-overview"
SHELL_OVERVIEW_OUTPUT_CHARACTERS = 500

TOOL_TAGS: dict[ToolName, tuple[str, ...]] = {
    ToolName.SHELL_OPEN: ("BuildIn", "Create"),
    ToolName.SHELL_EXEC: ("BuildIn", "Create", "Read", "Update", "Delete"),
    ToolName.SHELL_READ: ("BuildIn", "Read"),
    ToolName.SHELL_WRITE: ("BuildIn", "Create", "Read", "Update", "Delete"),
    ToolName.SHELL_SIGNAL: ("BuildIn", "Update"),
    ToolName.SHELL_LIST: ("BuildIn", "Read"),
    ToolName.SHELL_CLOSE: ("BuildIn", "Delete"),
}


@dataclass(frozen=True, slots=True)
class ToolConcurrencyLimits:
    wait_threads: int = 256
    control_threads: int = 256
    close_threads: int = 8
    metadata_threads: int = 16

    def __post_init__(self) -> None:
        if (
            min(
                self.wait_threads,
                self.control_threads,
                self.close_threads,
                self.metadata_threads,
            )
            <= 0
        ):
            raise ValueError("tool thread limits must be positive")


class ToolConcurrencyBudget:
    """Thread budget shareable by runtimes on one AnyIO backend/event loop."""

    def __init__(self, limits: ToolConcurrencyLimits) -> None:
        self._wait = anyio.CapacityLimiter(limits.wait_threads)
        self._control = anyio.CapacityLimiter(limits.control_threads)
        self._close = anyio.CapacityLimiter(limits.close_threads)
        self._metadata = anyio.CapacityLimiter(limits.metadata_threads)

    def limiter_for(self, tool: ToolName) -> anyio.CapacityLimiter:
        if tool in {ToolName.SHELL_OPEN, ToolName.SHELL_EXEC, ToolName.SHELL_READ}:
            return self._wait
        if tool in {ToolName.SHELL_WRITE, ToolName.SHELL_SIGNAL}:
            return self._control
        if tool is ToolName.SHELL_CLOSE:
            return self._close
        return self._metadata


class ShellManager(Protocol):
    """The domain operations consumed by the protocol adapter."""

    def open_shell(self, request: ShellStartRequest) -> ShellSnapshot: ...

    def exec(
        self,
        shell_id: str,
        command: str,
        *,
        yield_ms: int,
        timeout_ms: int,
        max_output_bytes: int,
    ) -> DomainExecutionSnapshot: ...

    def read(
        self,
        shell_id: str,
        exec_id: str,
        *,
        cursor: int,
        max_bytes: int,
        wait_ms: int,
    ) -> DomainExecutionSnapshot: ...

    def write(self, shell_id: str, exec_id: str, data: bytes) -> int: ...

    def signal(self, shell_id: str, exec_id: str, intent: ControlIntent) -> bool: ...

    def close_shell(self, shell_id: str) -> bool: ...

    def snapshots(self) -> tuple[ShellSnapshot, ...]: ...

    def overview_snapshots(
        self,
        *,
        max_output_characters: int,
    ) -> tuple[ShellOverviewSnapshot, ...]: ...

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    def shutdown(self) -> None: ...


class ShellToolService:
    """Translate validated V1 requests to the platform-neutral Shell Domain."""

    def __init__(
        self,
        *,
        manager: ShellManager,
        composition: RuntimeComposition,
        protocol_config: ProtocolConfig,
        agent_context: AgentContext,
        directory_exists: Callable[[str], bool],
        concurrency_limits: ToolConcurrencyLimits | None = None,
    ) -> None:
        self._manager = manager
        self._composition = composition
        self._config = protocol_config
        self._agent_context = agent_context
        self._directory_exists = directory_exists
        self._concurrency_limits = concurrency_limits or ToolConcurrencyLimits()

    @property
    def protocol_config(self) -> ProtocolConfig:
        return self._config

    @property
    def instructions(self) -> str:
        return self._composition.instructions(
            shell_version=self._agent_context.runtime.shell_version,
        )

    @property
    def tool_descriptions(self) -> dict[str, str]:
        return dict(self._composition.tool_descriptions())

    @property
    def concurrency_limits(self) -> ToolConcurrencyLimits:
        return self._concurrency_limits

    def call(self, tool_name: str, arguments: dict[str, object]) -> types.CallToolResult:
        try:
            tool = ToolName(tool_name)
        except ValueError:
            return _plain_error_result("Unknown tool.")
        try:
            request = validate_tool_input(tool, arguments, config=self._config)
        except ProtocolValidationError as error:
            return _error_result(error.envelope)
        try:
            result = self._dispatch(tool, request, arguments)
        except Exception as error:
            envelope = self._map_error(tool, error, request)
            if envelope is None or envelope.error.code not in ERROR_CODES_BY_TOOL[tool]:
                _LOGGER.error(
                    "unexpected %s while handling %s",
                    type(error).__name__,
                    tool.value,
                )
                return _plain_error_result("The tool failed unexpectedly.")
            return _error_result(envelope)
        try:
            output = validate_tool_output(tool, result, config=self._config)
        except Exception as error:
            _LOGGER.error(
                "invalid internal %s output while handling %s",
                type(error).__name__,
                tool.value,
            )
            return _plain_error_result("The tool failed unexpectedly.")
        return _success_result(cast(BaseModel, output))

    def shutdown(self) -> None:
        self._manager.shutdown()

    def shell_overview_markdown(self) -> str:
        snapshots = self._manager.overview_snapshots(
            max_output_characters=SHELL_OVERVIEW_OUTPUT_CHARACTERS,
        )
        return _overview_markdown(self._agent_context.diagnostics(), snapshots)

    def subscribe_overview_changes(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self._manager.subscribe_overview_changes(listener)

    def _dispatch(
        self,
        tool: ToolName,
        request: object,
        raw_arguments: dict[str, object],
    ) -> dict[str, object]:
        if tool is ToolName.SHELL_OPEN:
            open_request = cast(ShellOpenInput, request)
            startup_command = (
                open_request.startup_command
                if "startup_command" in raw_arguments
                else STARTUP_COMMAND_UNSET
            )
            start = self._composition.resolve_shell_start(
                ShellOpenOverrides(
                    cwd=open_request.cwd,
                    environment=open_request.env,
                    startup_command=startup_command,
                ),
                directory_exists=self._directory_exists,
            )
            snapshot = self._manager.open_shell(start)
            if snapshot.last_known_cwd is None:
                raise InvalidTransition("opened shell has no confirmed cwd")
            return {
                "shell_id": snapshot.shell_id,
                "status": "ready",
                "cwd": snapshot.last_known_cwd,
                "dialect": self._config.dialect.value,
            }
        if tool is ToolName.SHELL_EXEC:
            exec_request = cast(ShellExecInput, request)
            return _execution_to_wire(
                self._manager.exec(
                    exec_request.shell_id,
                    exec_request.command,
                    yield_ms=exec_request.yield_ms,
                    timeout_ms=exec_request.timeout_ms,
                    max_output_bytes=exec_request.max_output_bytes,
                )
            )
        if tool is ToolName.SHELL_READ:
            read_request = cast(ShellReadInput, request)
            return _execution_to_wire(
                self._manager.read(
                    read_request.shell_id,
                    read_request.exec_id,
                    cursor=read_request.cursor,
                    max_bytes=read_request.max_bytes,
                    wait_ms=read_request.wait_ms,
                )
            )
        if tool is ToolName.SHELL_WRITE:
            write_request = cast(ShellWriteInput, request)
            payload = write_request.text.encode("utf-8")
            accepted = self._manager.write(write_request.shell_id, write_request.exec_id, payload)
            return {
                "shell_id": write_request.shell_id,
                "exec_id": write_request.exec_id,
                "status": "accepted",
                "accepted_bytes": accepted,
            }
        if tool is ToolName.SHELL_SIGNAL:
            signal_request = cast(ShellSignalInput, request)
            self._manager.signal(
                signal_request.shell_id,
                signal_request.exec_id,
                ControlIntent(signal_request.signal.value),
            )
            return {
                "shell_id": signal_request.shell_id,
                "exec_id": signal_request.exec_id,
                "status": "delivered",
                "signal": signal_request.signal.value,
            }
        if tool is ToolName.SHELL_LIST:
            cast(ShellListInput, request)
            context = self._agent_context.diagnostics()
            return {
                **context,
                "shells": [_shell_to_wire(item) for item in self._manager.snapshots()],
            }
        if tool is ToolName.SHELL_CLOSE:
            close_request = cast(ShellCloseInput, request)
            cleanup_complete = self._manager.close_shell(close_request.shell_id)
            return {
                "shell_id": close_request.shell_id,
                "status": "closed",
                "cleanup_complete": cleanup_complete,
            }
        raise AssertionError(f"unhandled tool {tool.value}")

    def _map_error(
        self,
        tool: ToolName,
        error: Exception,
        request: object | None,
    ) -> ErrorEnvelope | None:
        shell_id = getattr(request, "shell_id", None)
        exec_id = getattr(request, "exec_id", None)
        details = {"shell_id": shell_id, "exec_id": exec_id}
        if isinstance(error, ValueError | RuntimeConfigurationError):
            return make_error(ErrorCode.INVALID_ARGUMENT, "The request is invalid.", **details)
        if isinstance(error, CapacityExceeded):
            return make_error(
                ErrorCode.RESOURCE_LIMIT,
                "A configured resource limit was reached.",
                **details,
            )
        if isinstance(error, ShellNotFound):
            return make_error(ErrorCode.SHELL_NOT_FOUND, "The shell was not found.", **details)
        if isinstance(error, ShellBusy):
            return make_error(ErrorCode.SHELL_BUSY, "The shell is busy.", **details)
        if isinstance(error, ShellClosing):
            return make_error(ErrorCode.SHELL_CLOSING, "The shell is closing.", **details)
        if isinstance(error, ShellUnavailable):
            return make_error(
                ErrorCode.SHELL_UNAVAILABLE,
                "The shell is temporarily unavailable.",
                retryable=error.retryable,
                **details,
            )
        if isinstance(error, ExecutionNotFound):
            return make_error(ErrorCode.EXEC_NOT_FOUND, "The execution was not found.", **details)
        if isinstance(error, ExecutionNotActive):
            return make_error(ErrorCode.EXEC_NOT_ACTIVE, "The execution is not active.", **details)
        if isinstance(error, InvalidCursor):
            return make_error(ErrorCode.INVALID_CURSOR, "The output cursor is invalid.", **details)
        if isinstance(error, UnsupportedShell):
            return make_error(ErrorCode.UNSUPPORTED_SHELL, "The shell is unsupported.", **details)
        if isinstance(error, RuntimeBoundaryError):
            if tool is ToolName.SHELL_OPEN:
                return make_error(ErrorCode.SHELL_START_FAILED, "The shell failed to start.")
            return make_error(
                ErrorCode.SHELL_UNAVAILABLE,
                "The shell runtime is unavailable.",
                retryable=False,
                **details,
            )
        return None


def tool_definitions(service: ShellToolService) -> tuple[types.Tool, ...]:
    """Build fresh public MCP tool definitions from the shared V1 contracts."""

    contracts = tool_contract_schemas(service.protocol_config)
    _redact_sensitive_schema_defaults(contracts)
    descriptions = service.tool_descriptions
    return tuple(
        types.Tool(
            name=tool.value,
            description=descriptions[tool.value],
            inputSchema=cast(dict[str, Any], contracts[tool.value]["inputSchema"]),
            outputSchema=cast(dict[str, Any], contracts[tool.value]["outputSchema"]),
            _meta={
                "tfbash-mcp/errorSchema": cast(
                    dict[str, Any], contracts[tool.value]["errorSchema"]
                ),
                "a2c_tool_meta": {"tags": list(TOOL_TAGS[tool])},
            },
        )
        for tool in ToolName
    )


async def call_tool_async(
    service: ShellToolService,
    name: str,
    arguments: dict[str, object],
    budget: ToolConcurrencyBudget,
) -> types.CallToolResult:
    """Run one synchronous Domain call without blocking the host event loop."""

    try:
        tool = ToolName(name)
    except ValueError:
        tool = ToolName.SHELL_LIST
    return await anyio.to_thread.run_sync(
        service.call,
        name,
        arguments,
        abandon_on_cancel=True,
        limiter=budget.limiter_for(tool),
    )


def _redact_sensitive_schema_defaults(
    contracts: dict[str, dict[str, dict[str, object]]],
) -> None:
    open_input = contracts[ToolName.SHELL_OPEN.value]["inputSchema"]
    properties = open_input.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeConfigurationError("shell_open schema has no properties")
    startup_schema = properties.get("startup_command")
    if isinstance(startup_schema, dict):
        startup_schema.pop("default", None)


def _execution_to_wire(snapshot: DomainExecutionSnapshot) -> dict[str, object]:
    result: dict[str, object] = {
        "shell_id": snapshot.shell_id,
        "exec_id": snapshot.exec_id,
        "status": snapshot.status.value,
        "exit_code": snapshot.exit_code,
        "output": snapshot.output,
        "buffer_start_cursor": snapshot.buffer_start_cursor,
        "next_cursor": snapshot.next_cursor,
        "truncated_before_cursor": snapshot.truncated_before_cursor,
        "eof": snapshot.eof,
    }
    if snapshot.status.terminal:
        result.update(
            duration_ms=snapshot.duration_ms,
            cwd=snapshot.cwd,
            shell_status=(
                snapshot.shell_status.value if snapshot.shell_status is not None else None
            ),
            shell_rebuilt=snapshot.shell_rebuilt,
        )
    return result


def _shell_to_wire(snapshot: ShellSnapshot) -> dict[str, object]:
    return {
        "shell_id": snapshot.shell_id,
        "status": snapshot.status.value,
        "last_known_cwd": snapshot.last_known_cwd,
        "active_exec_id": snapshot.active_exec_id,
        "created_at_ms": snapshot.created_at_ms,
    }


def _success_result(output: BaseModel) -> types.CallToolResult:
    wire = model_to_wire(output)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_json_text(wire))],
        structuredContent=wire,
        isError=False,
    )


def _error_result(error: ErrorEnvelope) -> types.CallToolResult:
    wire = model_to_wire(error)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_json_text(wire))],
        structuredContent=wire,
        isError=True,
    )


def _plain_error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
    )


def _json_text(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _overview_markdown(
    context: dict[str, object],
    snapshots: tuple[ShellOverviewSnapshot, ...],
) -> str:
    runtime = cast(dict[str, object], context["runtime"])
    host = cast(dict[str, object], context["host"])
    lines = [
        "# Shell Overview",
        "",
        f"- Platform: {_inline(runtime['platform'])}",
        f"- Dialect: {_inline(runtime['dialect'])}",
        f"- Workspace: {_inline(host['workspace_root'])}",
        f"- Shells: {len(snapshots)}",
        "",
    ]
    if not snapshots:
        lines.append("No active Shells.")
        return "\n".join(lines)

    lines.extend(
        [
            "| Shell ID | Status | CWD | Created | Execution ID | "
            "Execution status | Exit code | Duration |",
            "|---|---|---|---|---|---|---:|---:|",
        ]
    )
    for snapshot in snapshots:
        shell = snapshot.shell
        execution = snapshot.execution
        lines.append(
            "| "
            + " | ".join(
                (
                    _table_inline(shell.shell_id),
                    shell.status.value,
                    _table_inline(shell.last_known_cwd),
                    _format_created_at(shell.created_at_ms),
                    _table_inline(execution.exec_id if execution is not None else None),
                    execution.status.value if execution is not None else "—",
                    (
                        str(execution.exit_code)
                        if execution and execution.exit_code is not None
                        else "—"
                    ),
                    (
                        f"{execution.duration_ms} ms"
                        if execution and execution.duration_ms is not None
                        else "—"
                    ),
                )
            )
            + " |"
        )

    for snapshot in snapshots:
        execution = snapshot.execution
        lines.extend(["", f"## {_inline(snapshot.shell.shell_id)} — recent output", ""])
        if execution is None:
            lines.append("No retained execution.")
            continue
        if execution.output_truncated:
            lines.extend(["_Earlier output was truncated._", ""])
        if execution.output:
            lines.append(_code_block(execution.output))
        else:
            lines.append("No output captured.")
    return "\n".join(lines)


def _inline(value: object | None) -> str:
    if value is None:
        return "—"
    return _code_span(_visible_inline_text(str(value)))


def _table_inline(value: object | None) -> str:
    if value is None:
        return "—"
    text = _visible_inline_text(str(value)).replace("|", r"\|")
    return _code_span(text)


def _code_span(text: str) -> str:
    delimiter = "`" * (_longest_run(text, "`") + 1)
    if len(delimiter) == 1:
        return f"{delimiter}{text}{delimiter}"
    return f"{delimiter} {text} {delimiter}"


def _visible_inline_text(value: str) -> str:
    rendered: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character == "\r":
            rendered.append(r"\r")
        elif character == "\n":
            rendered.append(r"\n")
        elif character == "\t":
            rendered.append(r"\t")
        elif codepoint < 32 or codepoint == 127:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _code_block(value: str) -> str:
    fence = "`" * max(3, _longest_run(value, "`") + 1)
    trailing_newline = "" if value.endswith("\n") else "\n"
    return f"{fence}text\n{value}{trailing_newline}{fence}"


def _longest_run(value: str, character: str) -> int:
    longest = current = 0
    for item in value:
        if item == character:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _format_created_at(milliseconds: int) -> str:
    created = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return created.isoformat(timespec="milliseconds").replace("+00:00", "Z")
