"""Strict, runtime-neutral wire contracts for the seven V1 shell tools.

The protocol layer owns JSON validation and schemas only. It deliberately has
no dependency on the shell domain, MCP adapter, PTY implementation, or native
process APIs.
"""

from __future__ import annotations

import base64
import binascii
import os
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, TypeAlias, cast

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    model_validator,
)


class PlatformName(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


class DialectName(str, Enum):
    BASH = "bash"
    PWSH = "pwsh"


class HostMode(str, Enum):
    STANDALONE = "standalone"
    IDE = "ide"


class EnvironmentKind(str, Enum):
    NONE = "none"
    PYTHON_VENV = "python-venv"
    CONDA = "conda"
    CUSTOM = "custom"


def _default_platform() -> PlatformName:
    if os.name == "nt":
        return PlatformName.WINDOWS
    if sys.platform == "darwin":
        return PlatformName.MACOS
    return PlatformName.LINUX


def _default_shell(platform: PlatformName) -> str:
    if platform is PlatformName.WINDOWS:
        return r"C:\Program Files\PowerShell\7\pwsh.exe"
    return "/bin/bash"


def _native_default_shell() -> str:
    return _default_shell(_default_platform())


def _is_native_absolute_path(value: str, platform: PlatformName) -> bool:
    if platform is PlatformName.WINDOWS:
        normalized = value.replace("/", "\\")
        if normalized.startswith("\\\\.\\"):
            return False
        return PureWindowsPath(value).is_absolute()
    return PurePosixPath(value).is_absolute()


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    """Runtime facts and limits needed to resolve and validate wire values."""

    platform: PlatformName = field(default_factory=_default_platform)
    default_cwd: str = field(default_factory=os.getcwd)
    shell: str = field(default_factory=_native_default_shell)
    startup_command: str | None = None
    command_yield_ms: int = 10_000
    command_timeout_ms: int = 120_000
    max_command_bytes: int = 262_144
    output_buffer_bytes: int = 4_194_304
    max_read_bytes: int = 65_536
    max_write_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not isinstance(self.platform, PlatformName):
            raise TypeError("platform must be a PlatformName")
        if self.max_command_bytes < 1:
            raise ValueError("max_command_bytes must be positive")
        if self.output_buffer_bytes < 4_096:
            raise ValueError("output_buffer_bytes must be at least 4096")
        if self.max_read_bytes < 4:
            raise ValueError("max_read_bytes must be at least 4")
        if self.max_write_bytes < 1:
            raise ValueError("max_write_bytes must be positive")
        if not 0 <= self.command_yield_ms <= 60_000:
            raise ValueError("command_yield_ms must be between 0 and 60000")
        if not 1 <= self.command_timeout_ms <= 86_400_000:
            raise ValueError("command_timeout_ms must be between 1 and 86400000")
        for name, value in (("default_cwd", self.default_cwd), ("shell", self.shell)):
            if "\x00" in value or not _is_native_absolute_path(value, self.platform):
                raise ValueError(f"{name} must be an absolute {self.platform.value} path")
        if self.startup_command is not None:
            if not self.startup_command or "\x00" in self.startup_command:
                raise ValueError("startup_command must be non-empty and contain no U+0000")
            if len(self.startup_command.encode("utf-8")) > self.max_command_bytes:
                raise ValueError("startup_command exceeds max_command_bytes")

    @property
    def dialect(self) -> DialectName:
        if self.platform is PlatformName.WINDOWS:
            return DialectName.PWSH
        return DialectName.BASH


DEFAULT_PROTOCOL_CONFIG = ProtocolConfig()


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )


def _protocol_config(info: ValidationInfo) -> ProtocolConfig:
    if isinstance(info.context, ProtocolConfig):
        return info.context
    return DEFAULT_PROTOCOL_CONFIG


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("must be valid UTF-8") from exc


def _without_nul(value: str) -> str:
    _utf8(value)
    if "\x00" in value:
        raise ValueError("must not contain U+0000")
    return value


def _non_empty_string(value: str) -> str:
    _without_nul(value)
    if not value:
        raise ValueError("must not be empty")
    return value


def _identifier(value: str) -> str:
    size = len(_utf8(value))
    if not 1 <= size <= 128:
        raise ValueError("must contain between 1 and 128 UTF-8 bytes")
    return value


def _native_absolute_path(value: str, info: ValidationInfo) -> str:
    _without_nul(value)
    platform = _protocol_config(info).platform
    if not _is_native_absolute_path(value, platform):
        raise ValueError(f"must be an absolute {platform.value} path")
    return value


def _configured_limit(info: ValidationInfo, name: str, fallback: int) -> int:
    return cast(int, getattr(_protocol_config(info), name, fallback))


def _command(value: str, info: ValidationInfo) -> str:
    _non_empty_string(value)
    maximum = _configured_limit(info, "max_command_bytes", 262_144)
    if len(_utf8(value)) > maximum:
        raise ValueError(f"must not exceed {maximum} UTF-8 bytes")
    return value


def _write_text(value: str, info: ValidationInfo) -> str:
    _utf8(value)
    maximum = _configured_limit(info, "max_write_bytes", 65_536)
    if len(_utf8(value)) > maximum:
        raise ValueError(f"must not exceed {maximum} UTF-8 bytes")
    return value


def _base64_payload(value: str, info: ValidationInfo) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("must be canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("must be canonical base64")
    maximum = _configured_limit(info, "max_write_bytes", 65_536)
    if len(raw) > maximum:
        raise ValueError(f"decoded value must not exceed {maximum} bytes")
    return value


def _env_key(value: str) -> str:
    _without_nul(value)
    first_is_valid = bool(value) and (
        value[0] == "_" or (value[0].isascii() and value[0].isalpha())
    )
    if not first_is_valid:
        raise ValueError("must match [A-Za-z_][A-Za-z0-9_]*")
    if not all(
        character.isascii() and (character.isalnum() or character == "_") for character in value[1:]
    ):
        raise ValueError("must match [A-Za-z_][A-Za-z0-9_]*")
    return value


def _env_value(value: str) -> str:
    _without_nul(value)
    if len(_utf8(value)) > 32_768:
        raise ValueError("must not exceed 32768 UTF-8 bytes")
    return value


Identifier: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128),
    AfterValidator(_identifier),
]
NoNulString: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[^\x00]*$"),
    AfterValidator(_without_nul),
]
NonEmptyString: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, pattern=r"^[^\x00]*$"),
    AfterValidator(_non_empty_string),
]
NativeAbsolutePath: TypeAlias = Annotated[str, AfterValidator(_native_absolute_path)]
Command: TypeAlias = Annotated[str, StringConstraints(min_length=1), AfterValidator(_command)]
WriteText: TypeAlias = Annotated[str, AfterValidator(_write_text)]
Base64Payload: TypeAlias = Annotated[str, AfterValidator(_base64_payload)]
EnvKey: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
    AfterValidator(_env_key),
]
EnvValue: TypeAlias = Annotated[
    str,
    StringConstraints(pattern=r"^[^\x00]*$"),
    AfterValidator(_env_value),
]
Environment: TypeAlias = Annotated[dict[EnvKey, EnvValue], Field(max_length=256)]


class ToolName(str, Enum):
    SHELL_OPEN = "shell_open"
    SHELL_EXEC = "shell_exec"
    SHELL_READ = "shell_read"
    SHELL_WRITE = "shell_write"
    SHELL_SIGNAL = "shell_signal"
    SHELL_LIST = "shell_list"
    SHELL_CLOSE = "shell_close"


class ShellOpenInput(_StrictModel):
    cwd: NativeAbsolutePath = DEFAULT_PROTOCOL_CONFIG.default_cwd
    env: Environment = Field(default_factory=dict)
    shell: NativeAbsolutePath = DEFAULT_PROTOCOL_CONFIG.shell
    startup_command: Command | None = DEFAULT_PROTOCOL_CONFIG.startup_command

    @model_validator(mode="after")
    def validate_windows_environment(self, info: ValidationInfo) -> ShellOpenInput:
        if _protocol_config(info).platform is not PlatformName.WINDOWS:
            return self
        folded_keys = [key.casefold() for key in self.env]
        if len(folded_keys) != len(set(folded_keys)):
            raise ValueError("env keys must be unique ignoring case on Windows")
        return self


class ShellExecInput(_StrictModel):
    shell_id: Identifier
    command: Command
    yield_ms: int = Field(default=10_000, ge=0, le=60_000)
    timeout_ms: int = Field(default=120_000, ge=1, le=86_400_000)
    max_output_bytes: int = Field(default=4_194_304, ge=4_096)

    @model_validator(mode="after")
    def validate_output_limit(self, info: ValidationInfo) -> ShellExecInput:
        maximum = _configured_limit(info, "output_buffer_bytes", 4_194_304)
        if self.max_output_bytes > maximum:
            raise ValueError(f"max_output_bytes must not exceed {maximum}")
        return self


class ShellReadInput(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier
    cursor: int = Field(ge=0)
    max_bytes: int = Field(default=65_536, ge=4)
    wait_ms: int = Field(default=0, ge=0, le=60_000)

    @model_validator(mode="after")
    def validate_read_limit(self, info: ValidationInfo) -> ShellReadInput:
        maximum = _configured_limit(info, "max_read_bytes", 65_536)
        if self.max_bytes > maximum:
            raise ValueError(f"max_bytes must not exceed {maximum}")
        return self


class _WriteInput(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier


class ShellWriteTextInput(_WriteInput):
    text: WriteText


class ShellWriteBase64Input(_WriteInput):
    data_base64: Base64Payload


# EOF remains deliberately absent from the public V1 schema until native
# Windows 11 and POSIX evidence prove identical persistent-shell semantics.
ShellWriteInput: TypeAlias = ShellWriteTextInput | ShellWriteBase64Input


class SignalName(str, Enum):
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"
    KILL = "kill"


class ShellSignalInput(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier
    signal: SignalName = Field(strict=False)


class ShellListInput(_StrictModel):
    pass


class ShellCloseInput(_StrictModel):
    shell_id: Identifier


class ShellStatus(str, Enum):
    READY = "ready"
    BUSY = "busy"
    REBUILDING = "rebuilding"
    CLOSING = "closing"
    ERROR = "error"


class ShellOpenResult(_StrictModel):
    shell_id: Identifier
    status: Literal["ready"]
    cwd: NativeAbsolutePath
    dialect: DialectName = Field(strict=False)

    @model_validator(mode="after")
    def validate_runtime_dialect(self, info: ValidationInfo) -> ShellOpenResult:
        if self.dialect is not _protocol_config(info).dialect:
            raise ValueError("dialect must match the selected Runtime Profile")
        return self


class RuntimeContext(_StrictModel):
    platform: PlatformName = Field(strict=False)
    dialect: DialectName = Field(strict=False)
    shell_version: NonEmptyString
    default_cwd: NativeAbsolutePath

    @model_validator(mode="after")
    def validate_runtime_profile(self, info: ValidationInfo) -> RuntimeContext:
        config = _protocol_config(info)
        if self.platform is not config.platform or self.dialect is not config.dialect:
            raise ValueError("runtime metadata must match the selected Runtime Profile")
        return self


class EnvironmentSummary(_StrictModel):
    kind: EnvironmentKind = Field(strict=False)
    name: NoNulString | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_name(cls, value: object) -> object:
        if isinstance(value, dict) and "name" in value and value["name"] is None:
            raise ValueError("name must be omitted rather than null")
        return value


class HostContext(_StrictModel):
    mode: HostMode = Field(strict=False)
    workspace_root: NativeAbsolutePath
    environment: EnvironmentSummary


class ShellListItem(_StrictModel):
    shell_id: Identifier
    status: ShellStatus = Field(strict=False)
    last_known_cwd: NativeAbsolutePath | None
    active_exec_id: Identifier | None
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_active_execution(self) -> ShellListItem:
        may_be_active = self.status in {ShellStatus.BUSY, ShellStatus.REBUILDING}
        if may_be_active != (self.active_exec_id is not None):
            raise ValueError("active_exec_id must be set exactly when the shell has active work")
        return self


class ShellListResult(_StrictModel):
    runtime: RuntimeContext
    host: HostContext
    shells: list[ShellListItem]


class ShellCloseResult(_StrictModel):
    shell_id: Identifier
    status: Literal["closed"]
    cleanup_complete: bool


class _ExecutionBase(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier
    output: str
    buffer_start_cursor: int = Field(ge=0)
    next_cursor: int = Field(ge=0)
    truncated_before_cursor: bool
    eof: bool

    @model_validator(mode="after")
    def validate_cursor_window(self) -> _ExecutionBase:
        _utf8(self.output)
        if self.buffer_start_cursor > self.next_cursor:
            raise ValueError("buffer_start_cursor must not exceed next_cursor")
        return self


class RunningExecutionSnapshot(_ExecutionBase):
    status: Literal["running"]
    exit_code: None
    eof: Literal[False]


class _TerminalExecutionSnapshot(_ExecutionBase):
    duration_ms: int = Field(ge=0)
    cwd: NativeAbsolutePath | None
    shell_rebuilt: bool


class ExitedExecutionSnapshot(_TerminalExecutionSnapshot):
    status: Literal["exited"]
    exit_code: int = Field(ge=0, le=4_294_967_295)
    shell_status: Literal["ready", "closing"]

    @model_validator(mode="after")
    def validate_platform_exit_code(self, info: ValidationInfo) -> ExitedExecutionSnapshot:
        if _protocol_config(info).platform is not PlatformName.WINDOWS and self.exit_code > 255:
            raise ValueError("POSIX exit_code must not exceed 255")
        return self


class TimeoutExecutionSnapshot(_TerminalExecutionSnapshot):
    status: Literal["timeout"]
    exit_code: None
    shell_status: Literal["ready", "error", "closing"]


class CancelledExecutionSnapshot(_TerminalExecutionSnapshot):
    status: Literal["cancelled"]
    exit_code: None
    shell_status: Literal["ready", "error", "closing"]


class ShellErrorExecutionSnapshot(_TerminalExecutionSnapshot):
    status: Literal["shell_error"]
    exit_code: None
    shell_status: Literal["error", "closing"]


ExecutionSnapshot: TypeAlias = Annotated[
    RunningExecutionSnapshot
    | ExitedExecutionSnapshot
    | TimeoutExecutionSnapshot
    | CancelledExecutionSnapshot
    | ShellErrorExecutionSnapshot,
    Field(discriminator="status"),
]


class ShellWriteResult(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier
    status: Literal["accepted"]
    accepted_bytes: int = Field(ge=0)


class ShellSignalResult(_StrictModel):
    shell_id: Identifier
    exec_id: Identifier
    status: Literal["delivered"]
    signal: SignalName = Field(strict=False)


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_CURSOR = "invalid_cursor"
    UNSUPPORTED_SHELL = "unsupported_shell"
    SHELL_START_FAILED = "shell_start_failed"
    SHELL_NOT_FOUND = "shell_not_found"
    SHELL_BUSY = "shell_busy"
    SHELL_CLOSING = "shell_closing"
    SHELL_UNAVAILABLE = "shell_unavailable"
    EXEC_NOT_FOUND = "exec_not_found"
    EXEC_NOT_ACTIVE = "exec_not_active"
    RESOURCE_LIMIT = "resource_limit"


RETRYABLE_BY_CODE: Mapping[ErrorCode, bool | None] = MappingProxyType(
    {
        ErrorCode.INVALID_ARGUMENT: False,
        ErrorCode.INVALID_CURSOR: False,
        ErrorCode.UNSUPPORTED_SHELL: False,
        ErrorCode.SHELL_START_FAILED: False,
        ErrorCode.SHELL_NOT_FOUND: False,
        ErrorCode.SHELL_BUSY: True,
        ErrorCode.SHELL_CLOSING: False,
        ErrorCode.SHELL_UNAVAILABLE: None,
        ErrorCode.EXEC_NOT_FOUND: False,
        ErrorCode.EXEC_NOT_ACTIVE: False,
        ErrorCode.RESOURCE_LIMIT: True,
    }
)


ERROR_CODES_BY_TOOL: Mapping[ToolName, frozenset[ErrorCode]] = MappingProxyType(
    {
        ToolName.SHELL_OPEN: frozenset(
            {
                ErrorCode.INVALID_ARGUMENT,
                ErrorCode.UNSUPPORTED_SHELL,
                ErrorCode.SHELL_START_FAILED,
                ErrorCode.RESOURCE_LIMIT,
            }
        ),
        ToolName.SHELL_LIST: frozenset({ErrorCode.INVALID_ARGUMENT}),
        ToolName.SHELL_CLOSE: frozenset(
            {ErrorCode.INVALID_ARGUMENT, ErrorCode.SHELL_NOT_FOUND, ErrorCode.SHELL_CLOSING}
        ),
        ToolName.SHELL_EXEC: frozenset(
            {
                ErrorCode.INVALID_ARGUMENT,
                ErrorCode.SHELL_NOT_FOUND,
                ErrorCode.SHELL_BUSY,
                ErrorCode.SHELL_CLOSING,
                ErrorCode.SHELL_UNAVAILABLE,
            }
        ),
        ToolName.SHELL_READ: frozenset(
            {
                ErrorCode.INVALID_ARGUMENT,
                ErrorCode.INVALID_CURSOR,
                ErrorCode.SHELL_NOT_FOUND,
                ErrorCode.SHELL_CLOSING,
                ErrorCode.EXEC_NOT_FOUND,
                ErrorCode.RESOURCE_LIMIT,
            }
        ),
        ToolName.SHELL_WRITE: frozenset(
            {
                ErrorCode.INVALID_ARGUMENT,
                ErrorCode.SHELL_NOT_FOUND,
                ErrorCode.SHELL_CLOSING,
                ErrorCode.SHELL_UNAVAILABLE,
                ErrorCode.EXEC_NOT_FOUND,
                ErrorCode.EXEC_NOT_ACTIVE,
                ErrorCode.RESOURCE_LIMIT,
            }
        ),
        ToolName.SHELL_SIGNAL: frozenset(
            {
                ErrorCode.INVALID_ARGUMENT,
                ErrorCode.SHELL_NOT_FOUND,
                ErrorCode.SHELL_CLOSING,
                ErrorCode.SHELL_UNAVAILABLE,
                ErrorCode.EXEC_NOT_FOUND,
                ErrorCode.EXEC_NOT_ACTIVE,
                ErrorCode.RESOURCE_LIMIT,
            }
        ),
    }
)


class ErrorDetails(_StrictModel):
    code: ErrorCode = Field(strict=False)
    message: NonEmptyString
    retryable: bool
    shell_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    exec_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_ids(cls, value: object) -> object:
        if isinstance(value, dict):
            for key in ("shell_id", "exec_id"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} must be omitted rather than null")
        return value

    @model_validator(mode="after")
    def validate_retryable(self) -> ErrorDetails:
        expected = RETRYABLE_BY_CODE[self.code]
        if expected is not None and self.retryable is not expected:
            raise ValueError(f"retryable must be {expected} for {self.code.value}")
        return self


class ErrorEnvelope(_StrictModel):
    error: ErrorDetails


class ProtocolValidationError(ValueError):
    """A request that did not satisfy its public wire contract."""

    def __init__(self, validation_error: ValidationError) -> None:
        first = validation_error.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(part) for part in first["loc"])
        message = str(first["msg"])
        if location:
            message = f"{location}: {message}"
        self.envelope = make_error(ErrorCode.INVALID_ARGUMENT, message)
        super().__init__(message)


ToolInput: TypeAlias = (
    ShellOpenInput
    | ShellExecInput
    | ShellReadInput
    | ShellWriteInput
    | ShellSignalInput
    | ShellListInput
    | ShellCloseInput
)
ToolOutput: TypeAlias = (
    ShellOpenResult
    | ExecutionSnapshot
    | ShellWriteResult
    | ShellSignalResult
    | ShellListResult
    | ShellCloseResult
)


_INPUT_ADAPTERS: dict[ToolName, TypeAdapter[object]] = {
    ToolName.SHELL_OPEN: TypeAdapter(ShellOpenInput),
    ToolName.SHELL_EXEC: TypeAdapter(ShellExecInput),
    ToolName.SHELL_READ: TypeAdapter(ShellReadInput),
    ToolName.SHELL_WRITE: TypeAdapter(ShellWriteInput),
    ToolName.SHELL_SIGNAL: TypeAdapter(ShellSignalInput),
    ToolName.SHELL_LIST: TypeAdapter(ShellListInput),
    ToolName.SHELL_CLOSE: TypeAdapter(ShellCloseInput),
}

_OUTPUT_ADAPTERS: dict[ToolName, TypeAdapter[object]] = {
    ToolName.SHELL_OPEN: TypeAdapter(ShellOpenResult),
    ToolName.SHELL_EXEC: TypeAdapter(ExecutionSnapshot),
    ToolName.SHELL_READ: TypeAdapter(ExecutionSnapshot),
    ToolName.SHELL_WRITE: TypeAdapter(ShellWriteResult),
    ToolName.SHELL_SIGNAL: TypeAdapter(ShellSignalResult),
    ToolName.SHELL_LIST: TypeAdapter(ShellListResult),
    ToolName.SHELL_CLOSE: TypeAdapter(ShellCloseResult),
}


def _configured_payload(tool: ToolName, value: object, config: ProtocolConfig) -> object:
    if not isinstance(value, dict):
        return value
    payload = dict(value)
    defaults: dict[ToolName, dict[str, object]] = {
        ToolName.SHELL_OPEN: {
            "cwd": config.default_cwd,
            "env": {},
            "shell": config.shell,
            "startup_command": config.startup_command,
        },
        ToolName.SHELL_EXEC: {
            "yield_ms": config.command_yield_ms,
            "timeout_ms": config.command_timeout_ms,
            "max_output_bytes": config.output_buffer_bytes,
        },
        ToolName.SHELL_READ: {"max_bytes": config.max_read_bytes, "wait_ms": 0},
    }
    for key, default in defaults.get(tool, {}).items():
        payload.setdefault(key, default)
    return payload


def validate_tool_input(
    tool: ToolName | str,
    value: object,
    *,
    config: ProtocolConfig = DEFAULT_PROTOCOL_CONFIG,
) -> ToolInput:
    """Validate and resolve a request using the selected runtime defaults."""

    tool_name = ToolName(tool)
    try:
        result = _INPUT_ADAPTERS[tool_name].validate_python(
            _configured_payload(tool_name, value, config), context=config
        )
    except ValidationError as exc:
        raise ProtocolValidationError(exc) from exc
    return cast(ToolInput, result)


def validate_tool_output(
    tool: ToolName | str,
    value: object,
    *,
    config: ProtocolConfig = DEFAULT_PROTOCOL_CONFIG,
) -> ToolOutput:
    """Validate an adapter/domain result against the same public contract."""

    tool_name = ToolName(tool)
    result = _OUTPUT_ADAPTERS[tool_name].validate_python(value, context=config)
    return cast(ToolOutput, result)


def make_error(
    code: ErrorCode,
    message: str,
    *,
    shell_id: str | None = None,
    exec_id: str | None = None,
    retryable: bool | None = None,
) -> ErrorEnvelope:
    """Construct a contract-valid error and enforce the fixed retry policy."""

    expected = RETRYABLE_BY_CODE[code]
    if retryable is None:
        if expected is None:
            raise ValueError("shell_unavailable requires an explicit retryable value")
        retryable = expected
    details: dict[str, object] = {"code": code, "message": message, "retryable": retryable}
    if shell_id is not None:
        details["shell_id"] = shell_id
    if exec_id is not None:
        details["exec_id"] = exec_id
    return ErrorEnvelope(error=ErrorDetails.model_validate(details))


def model_to_wire(model: BaseModel) -> dict[str, object]:
    """Serialize a protocol model to its canonical JSON-compatible object."""

    return cast(dict[str, object], model.model_dump(mode="json"))


_POSIX_ABSOLUTE_PATH_PATTERN = r"^/"
_PATH_PROPERTY_NAMES = frozenset(
    {"cwd", "default_cwd", "last_known_cwd", "shell", "workspace_root"}
)
_IDENTIFIER_PROPERTY_NAMES = frozenset({"shell_id", "exec_id", "active_exec_id"})
_PROTOCOL_SCHEMA_VOCABULARY = "https://github.com/A2C-SMCP/tfbash-mcp/schema/v1"


def _canonical_base64_pattern(maximum_decoded_bytes: int) -> str:
    """Return a canonical base64 regex whose decoded payload fits the byte limit."""

    full_group = r"[A-Za-z0-9+/]{4}"
    one_byte = r"[A-Za-z0-9+/][AQgw]=="
    two_bytes = r"[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]="
    full_groups, remainder = divmod(maximum_decoded_bytes, 3)
    alternatives: list[str] = []
    if full_groups:
        alternatives.extend(
            [
                rf"(?:{full_group}){{0,{full_groups - 1}}}(?:{one_byte}|{two_bytes})?",
                rf"(?:{full_group}){{{full_groups}}}",
            ]
        )
    else:
        alternatives.append("")
    if remainder >= 1:
        alternatives.append(rf"(?:{full_group}){{{full_groups}}}{one_byte}")
    if remainder >= 2:
        alternatives.append(rf"(?:{full_group}){{{full_groups}}}{two_bytes}")
    return rf"^(?:{'|'.join(alternatives)})$"


def _update_string_variants(schema: dict[str, object], **updates: object) -> None:
    if schema.get("type") == "string":
        schema.update(updates)
    for keyword in ("anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    _update_string_variants(variant, **updates)


def _validate_utf8_max_bytes(
    _validator: object, limit: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if not isinstance(limit, int) or not isinstance(instance, str):
        return
    try:
        size = len(instance.encode("utf-8"))
    except UnicodeEncodeError:
        yield JsonSchemaValidationError("value is not valid UTF-8")
        return
    if size > limit:
        yield JsonSchemaValidationError(f"UTF-8 payload exceeds {limit} bytes")


def _validate_utf8(
    _validator: object, enabled: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if enabled is not True or not isinstance(instance, str):
        return
    try:
        instance.encode("utf-8")
    except UnicodeEncodeError:
        yield JsonSchemaValidationError("value is not valid UTF-8")


def _validate_native_absolute_path(
    _validator: object, platform: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if not isinstance(instance, str) or not isinstance(platform, str):
        return
    try:
        platform_name = PlatformName(platform)
    except ValueError:
        yield JsonSchemaValidationError(f"unknown native platform {platform}")
        return
    if not _is_native_absolute_path(instance, platform_name):
        yield JsonSchemaValidationError(f"value must be an absolute {platform} path")


def _is_strict_integer(_checker: object, instance: object) -> bool:
    return isinstance(instance, int) and not isinstance(instance, bool)


def _validate_decoded_max_bytes(
    _validator: object, limit: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if not isinstance(limit, int) or not isinstance(instance, str):
        return
    try:
        decoded = base64.b64decode(instance, validate=True)
    except (binascii.Error, ValueError):
        return
    if len(decoded) > limit:
        yield JsonSchemaValidationError(f"decoded payload exceeds {limit} bytes")


def _validate_case_insensitive_unique_keys(
    _validator: object, enabled: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if enabled is not True or not isinstance(instance, dict):
        return
    keys = [key.casefold() for key in instance if isinstance(key, str)]
    if len(keys) != len(set(keys)):
        yield JsonSchemaValidationError("object keys must be unique ignoring case")


def _validate_field_less_than_or_equal(
    _validator: object, fields: object, instance: object, _schema: object
) -> Iterator[JsonSchemaValidationError]:
    if (
        not isinstance(fields, list)
        or len(fields) != 2
        or not all(isinstance(field, str) for field in fields)
        or not isinstance(instance, dict)
    ):
        return
    left_name, right_name = cast(list[str], fields)
    left = instance.get(left_name)
    right = instance.get(right_name)
    if (
        isinstance(left, int)
        and not isinstance(left, bool)
        and isinstance(right, int)
        and not isinstance(right, bool)
        and left > right
    ):
        yield JsonSchemaValidationError(f"{left_name} must not exceed {right_name}")


_ProtocolSchemaValidator = cast(Any, validators.extend)(
    Draft202012Validator,
    {
        "x-validUtf8": _validate_utf8,
        "x-utf8-maxBytes": _validate_utf8_max_bytes,
        "x-decoded-maxBytes": _validate_decoded_max_bytes,
        "x-caseInsensitiveUniqueKeys": _validate_case_insensitive_unique_keys,
        "x-fieldLessThanOrEqual": _validate_field_less_than_or_equal,
        "x-nativeAbsolutePath": _validate_native_absolute_path,
    },
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_strict_integer),
)


def validate_schema_instance(schema: Mapping[str, object], instance: object) -> None:
    """Validate an instance using Draft 2020-12 plus required protocol vocabulary.

    JSON Schema cannot natively compare UTF-8 byte length, object keys ignoring
    case, or two numeric properties. Those assertions are therefore first-class,
    mandatory vocabulary keywords rather than advisory annotations.
    """

    Draft202012Validator.check_schema(schema)
    _ProtocolSchemaValidator(schema).validate(instance)


def _strip_null_variant(schema: dict[str, object]) -> None:
    variants = schema.get("anyOf")
    if not isinstance(variants, list):
        return
    non_null = [
        variant
        for variant in variants
        if not (isinstance(variant, dict) and variant.get("type") == "null")
    ]
    if len(non_null) != 1:
        return
    title = schema.get("title")
    schema.clear()
    schema.update(cast(dict[str, object], non_null[0]))
    if title is not None:
        schema["title"] = title


def _enrich_wire_schema(schema: object, config: ProtocolConfig) -> None:
    """Add runtime constraints that Pydantic cannot express in its core schema."""

    if isinstance(schema, list):
        for item in schema:
            _enrich_wire_schema(item, config)
        return
    if not isinstance(schema, dict):
        return

    if schema.get("type") == "string":
        schema["x-validUtf8"] = True

    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                continue
            if name in _PATH_PROPERTY_NAMES:
                pattern = (
                    r"^[^\x00]*$"
                    if config.platform is PlatformName.WINDOWS
                    else rf"(?={_POSIX_ABSOLUTE_PATH_PATTERN})^[^\x00]*$"
                )
                _update_string_variants(
                    property_schema,
                    pattern=pattern,
                    **{
                        "x-nativeAbsolutePath": config.platform.value,
                        "x-platform": config.platform.value,
                    },
                )
            elif name in _IDENTIFIER_PROPERTY_NAMES:
                _update_string_variants(
                    property_schema,
                    maxLength=128,
                    **{"x-utf8-maxBytes": 128},
                )
            elif name in {"command", "startup_command"}:
                _update_string_variants(
                    property_schema,
                    maxLength=config.max_command_bytes,
                    pattern=r"^[^\x00]*$",
                    **{"x-utf8-maxBytes": config.max_command_bytes},
                )
            elif name == "text":
                _update_string_variants(
                    property_schema,
                    maxLength=config.max_write_bytes,
                    **{"x-utf8-maxBytes": config.max_write_bytes},
                )
            elif name == "data_base64":
                encoded_maximum = ((config.max_write_bytes + 2) // 3) * 4
                _update_string_variants(
                    property_schema,
                    pattern=_canonical_base64_pattern(config.max_write_bytes),
                    maxLength=encoded_maximum,
                    contentEncoding="base64",
                    **{"x-decoded-maxBytes": config.max_write_bytes},
                )
            elif name == "env" and config.platform is PlatformName.WINDOWS:
                property_schema["x-caseInsensitiveUniqueKeys"] = True

        if schema.get("title") == "EnvironmentSummary":
            name_schema = properties.get("name")
            if isinstance(name_schema, dict):
                _strip_null_variant(name_schema)

        if schema.get("title") == "ShellListItem":
            active_identifier = {
                "maxLength": 128,
                "minLength": 1,
                "type": "string",
                "x-utf8-maxBytes": 128,
            }
            schema["oneOf"] = [
                {
                    "properties": {
                        "status": {"enum": ["busy", "rebuilding"]},
                        "active_exec_id": active_identifier,
                    },
                    "required": ["status", "active_exec_id"],
                },
                {
                    "properties": {
                        "status": {"enum": ["ready", "closing", "error"]},
                        "active_exec_id": {"type": "null"},
                    },
                    "required": ["status", "active_exec_id"],
                },
            ]

        if {"buffer_start_cursor", "next_cursor"}.issubset(properties):
            schema["x-fieldLessThanOrEqual"] = [
                "buffer_start_cursor",
                "next_cursor",
            ]

    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, dict):
        schema["additionalProperties"] = False
        for value_schema in pattern_properties.values():
            if isinstance(value_schema, dict):
                _update_string_variants(
                    value_schema,
                    maxLength=32_768,
                    **{"x-utf8-maxBytes": 32_768},
                )

    for value in schema.values():
        _enrich_wire_schema(value, config)


def _bind_output_profile(tool: ToolName, schema: dict[str, object], config: ProtocolConfig) -> None:
    if tool is ToolName.SHELL_OPEN:
        properties = cast(dict[str, dict[str, object]], schema["properties"])
        properties["dialect"] = {
            "const": config.dialect.value,
            "title": "Dialect",
            "type": "string",
        }
    elif tool is ToolName.SHELL_LIST:
        definitions = cast(dict[str, dict[str, object]], schema["$defs"])
        runtime_properties = cast(
            dict[str, dict[str, object]], definitions["RuntimeContext"]["properties"]
        )
        runtime_properties["platform"] = {
            "const": config.platform.value,
            "title": "Platform",
            "type": "string",
        }
        runtime_properties["dialect"] = {
            "const": config.dialect.value,
            "title": "Dialect",
            "type": "string",
        }


def _error_contract_schema(tool: ToolName, config: ProtocolConfig) -> dict[str, object]:
    branches: list[dict[str, object]] = []
    identifier_schema: dict[str, object] = {
        "minLength": 1,
        "maxLength": 128,
        "type": "string",
        "x-utf8-maxBytes": 128,
    }
    for code in sorted(ERROR_CODES_BY_TOOL[tool], key=lambda item: item.value):
        expected_retryable = RETRYABLE_BY_CODE[code]
        retryable_schema: dict[str, object] = {"type": "boolean"}
        if expected_retryable is not None:
            retryable_schema["const"] = expected_retryable
        branches.append(
            {
                "additionalProperties": False,
                "properties": {
                    "code": {"const": code.value, "type": "string"},
                    "message": {
                        "minLength": 1,
                        "pattern": r"^[^\x00]*$",
                        "type": "string",
                    },
                    "retryable": retryable_schema,
                    "shell_id": identifier_schema,
                    "exec_id": identifier_schema,
                },
                "required": ["code", "message", "retryable"],
                "type": "object",
            }
        )
    result: dict[str, object] = {
        "additionalProperties": False,
        "properties": {"error": {"oneOf": branches}},
        "required": ["error"],
        "title": f"{tool.value} error envelope",
        "type": "object",
    }
    _enrich_wire_schema(result, config)
    return result


def _set_exit_code_maximum(schema: dict[str, object], maximum: int) -> None:
    definitions = cast(dict[str, dict[str, object]], schema.get("$defs", {}))
    exited = definitions.get("ExitedExecutionSnapshot")
    if exited is None:
        return
    properties = cast(dict[str, dict[str, object]], exited["properties"])
    properties["exit_code"]["maximum"] = maximum


def _set_mcp_object_root(schema: dict[str, object]) -> None:
    """Keep composed model schemas compatible with the MCP Tool wire shape."""

    declared_type = schema.get("type")
    if declared_type not in (None, "object"):
        raise TypeError(f"MCP Tool schema root must be an object, got {declared_type!r}")
    schema["type"] = "object"


def _schema_contains_reference_keyword(schema: object) -> bool:
    if isinstance(schema, dict):
        return bool({"$ref", "$dynamicRef"} & schema.keys()) or any(
            _schema_contains_reference_keyword(value) for value in schema.values()
        )
    if isinstance(schema, list):
        return any(_schema_contains_reference_keyword(value) for value in schema)
    return False


def _flatten_shell_write_input_schema(schema: dict[str, object]) -> dict[str, object]:
    """Expose fields for the exact closed-union shape used by ``shell_write``."""

    if set(schema) != {"$defs", "anyOf", "type"} or schema.get("type") != "object":
        raise TypeError("shell_write union root contains unsupported schema keywords")

    definitions = schema.get("$defs")
    branches = schema.get("anyOf")
    if not isinstance(definitions, dict) or not isinstance(branches, list) or not branches:
        raise TypeError("object union schema must contain $defs and non-empty anyOf")

    variants: list[dict[str, object]] = []
    referenced_definitions: set[str] = set()
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != {"$ref"}:
            raise TypeError("shell_write union branches must contain only $ref")
        reference = branch.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise TypeError("object union branch must reference a local $defs schema")
        definition_name = reference.removeprefix("#/$defs/")
        referenced_definitions.add(definition_name)
        variant = definitions.get(definition_name)
        if not isinstance(variant, dict):
            raise TypeError(f"object union branch references missing schema {reference!r}")
        if set(variant) != {
            "additionalProperties",
            "properties",
            "required",
            "title",
            "type",
        }:
            raise TypeError("shell_write union variants contain unsupported schema keywords")
        if variant.get("type") != "object" or variant.get("additionalProperties") is not False:
            raise TypeError("object union variants must be closed object schemas")
        variants.append(cast(dict[str, object], variant))
    if set(definitions) != referenced_definitions:
        raise TypeError("shell_write union contains unreferenced or nested definitions")

    required_by_variant: list[list[str]] = []
    merged_properties: dict[str, object] = {}
    for variant in variants:
        properties = variant.get("properties")
        required = variant.get("required")
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or not all(isinstance(name, str) for name in required)
        ):
            raise TypeError("object union variants must declare properties and required fields")
        required_names = cast(list[str], required)
        if set(properties) != set(required_names):
            raise TypeError("form-compatible object union variants cannot have optional fields")
        required_by_variant.append(required_names)
        for name, property_schema in properties.items():
            if _schema_contains_reference_keyword(property_schema):
                raise TypeError("shell_write union properties cannot contain reference keywords")
            if name in merged_properties and merged_properties[name] != property_schema:
                raise TypeError(f"object union property {name!r} has conflicting schemas")
            merged_properties[name] = property_schema

    common_required = [
        name
        for name in required_by_variant[0]
        if all(name in required for required in required_by_variant[1:])
    ]
    exclusive_required = [
        [name for name in required if name not in common_required]
        for required in required_by_variant
    ]
    if any(len(required) != 1 for required in exclusive_required):
        raise TypeError("shell_write union variants must each have one exclusive required field")
    exclusive_names = [required[0] for required in exclusive_required]
    if len(set(exclusive_names)) != len(exclusive_names):
        raise TypeError("shell_write union variants must have unique exclusive required fields")

    return {
        "additionalProperties": False,
        "properties": merged_properties,
        "required": common_required,
        "oneOf": [{"required": required} for required in exclusive_required],
        "title": "ShellWriteInput",
        "type": "object",
    }


def tool_contract_schemas(
    config: ProtocolConfig = DEFAULT_PROTOCOL_CONFIG,
) -> dict[str, dict[str, dict[str, object]]]:
    """Return deterministic input/output schemas for all seven registered tools."""

    contracts: dict[str, dict[str, dict[str, object]]] = {}
    for tool in ToolName:
        input_schema = _INPUT_ADAPTERS[tool].json_schema()
        _set_mcp_object_root(input_schema)
        if tool is ToolName.SHELL_WRITE:
            input_schema = _flatten_shell_write_input_schema(input_schema)
        properties = cast(dict[str, dict[str, object]], input_schema.get("properties", {}))
        configured_defaults = cast(dict[str, object], _configured_payload(tool, {}, config))
        for key, default in configured_defaults.items():
            if key in properties:
                properties[key]["default"] = default
        if tool is ToolName.SHELL_EXEC:
            properties["max_output_bytes"]["maximum"] = config.output_buffer_bytes
        elif tool is ToolName.SHELL_READ:
            properties["max_bytes"]["maximum"] = config.max_read_bytes

        _enrich_wire_schema(input_schema, config)
        input_schema["x-requiredVocabulary"] = _PROTOCOL_SCHEMA_VOCABULARY

        output_schema = _OUTPUT_ADAPTERS[tool].json_schema()
        _set_mcp_object_root(output_schema)
        maximum_exit_code = 4_294_967_295 if config.platform is PlatformName.WINDOWS else 255
        _set_exit_code_maximum(output_schema, maximum_exit_code)
        _bind_output_profile(tool, output_schema, config)
        _enrich_wire_schema(output_schema, config)
        output_schema["x-requiredVocabulary"] = _PROTOCOL_SCHEMA_VOCABULARY
        error_schema = _error_contract_schema(tool, config)
        error_schema["x-requiredVocabulary"] = _PROTOCOL_SCHEMA_VOCABULARY
        contracts[tool.value] = {
            "inputSchema": input_schema,
            "outputSchema": output_schema,
            "errorSchema": error_schema,
        }
    return contracts
