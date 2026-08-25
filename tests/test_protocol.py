from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import TypeAdapter, ValidationError

from tfbash_mcp.protocol import (
    ERROR_CODES_BY_TOOL,
    RETRYABLE_BY_CODE,
    CancelledExecutionSnapshot,
    DialectName,
    EnvironmentSummary,
    ErrorCode,
    ExitedExecutionSnapshot,
    PlatformName,
    ProtocolConfig,
    ProtocolValidationError,
    RunningExecutionSnapshot,
    ShellCloseInput,
    ShellErrorExecutionSnapshot,
    ShellExecInput,
    ShellListItem,
    ShellListResult,
    ShellOpenInput,
    ShellOpenResult,
    ShellSignalInput,
    ShellWriteBase64Input,
    ShellWriteTextInput,
    SignalName,
    TimeoutExecutionSnapshot,
    ToolName,
    make_error,
    model_to_wire,
    tool_contract_schemas,
    validate_schema_instance,
    validate_tool_input,
    validate_tool_output,
)

POSIX_CONFIG = ProtocolConfig(
    platform=PlatformName.MACOS,
    default_cwd="/workspace",
    shell="/bin/bash",
)
WINDOWS_CONFIG = ProtocolConfig(
    platform=PlatformName.WINDOWS,
    default_cwd=r"C:\work\project",
    shell=r"C:\Program Files\PowerShell\7\pwsh.exe",
)


def _assert_model_objects_are_closed(schema: object) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            assert schema.get("additionalProperties") is False
        for value in schema.values():
            _assert_model_objects_are_closed(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_model_objects_are_closed(value)


def _validate_schema(schema: dict[str, object], payload: object) -> None:
    Draft202012Validator.check_schema(schema)
    validate_schema_instance(schema, payload)


def _terminal_payload(*, exit_code: int = 0, cwd: str = "/workspace") -> dict[str, object]:
    return {
        "shell_id": "shell_1",
        "exec_id": "exec_1",
        "status": "exited",
        "exit_code": exit_code,
        "output": "ok\n",
        "buffer_start_cursor": 0,
        "next_cursor": 3,
        "truncated_before_cursor": False,
        "eof": True,
        "duration_ms": 1,
        "cwd": cwd,
        "shell_status": "ready",
        "shell_rebuilt": False,
    }


@pytest.mark.parametrize("platform", list(PlatformName))
def test_every_tool_has_strict_input_and_output_schema(platform: PlatformName) -> None:
    config = WINDOWS_CONFIG if platform is PlatformName.WINDOWS else POSIX_CONFIG
    contracts = tool_contract_schemas(config)

    assert set(contracts) == {tool.value for tool in ToolName}
    for contract in contracts.values():
        _assert_model_objects_are_closed(contract["inputSchema"])
        _assert_model_objects_are_closed(contract["outputSchema"])
        _assert_model_objects_are_closed(contract["errorSchema"])

    write_schema = cast(
        dict[str, Any], contracts[ToolName.SHELL_WRITE.value]["inputSchema"]
    )
    assert "oneOf" in write_schema
    assert len(write_schema["oneOf"]) == 2
    assert "eof" not in json.dumps(write_schema)


def test_real_json_schema_enforces_wire_input_constraints() -> None:
    config = ProtocolConfig(
        platform=PlatformName.LINUX,
        default_cwd="/workspace",
        shell="/bin/bash",
        max_command_bytes=4,
        max_write_bytes=2,
    )
    contracts = tool_contract_schemas(config)
    open_schema = contracts[ToolName.SHELL_OPEN.value]["inputSchema"]
    exec_schema = contracts[ToolName.SHELL_EXEC.value]["inputSchema"]
    write_schema = contracts[ToolName.SHELL_WRITE.value]["inputSchema"]

    _validate_schema(open_schema, {"cwd": "/tmp", "env": {"OK": "yes"}})
    _validate_schema(exec_schema, {"shell_id": "s", "command": "true"})
    _validate_schema(
        write_schema, {"shell_id": "s", "exec_id": "e", "data_base64": "AA=="}
    )

    invalid_pairs = [
        (open_schema, {"cwd": "relative"}),
        (open_schema, {"env": {"A-B": "value"}}),
        (exec_schema, {"shell_id": "s", "command": "12345"}),
        (write_schema, {"shell_id": "s", "exec_id": "e", "data_base64": "bad"}),
        (write_schema, {"shell_id": "s", "exec_id": "e", "data_base64": "AB=="}),
    ]
    for schema, payload in invalid_pairs:
        with pytest.raises(JsonSchemaValidationError):
            _validate_schema(schema, payload)


def test_required_schema_vocabulary_enforces_nonstandard_runtime_constraints() -> None:
    tiny_posix = ProtocolConfig(
        platform=PlatformName.LINUX,
        default_cwd="/workspace",
        shell="/bin/bash",
        max_command_bytes=4,
        max_write_bytes=2,
    )
    posix = tool_contract_schemas(tiny_posix)
    windows = tool_contract_schemas(WINDOWS_CONFIG)
    running_with_reversed_cursors = {
        "shell_id": "s",
        "exec_id": "e",
        "status": "running",
        "exit_code": None,
        "output": "",
        "buffer_start_cursor": 2,
        "next_cursor": 1,
        "truncated_before_cursor": False,
        "eof": False,
    }
    invalid_pairs = [
        (
            posix[ToolName.SHELL_EXEC.value]["inputSchema"],
            {"shell_id": "s", "command": "界界"},
        ),
        (
            posix[ToolName.SHELL_WRITE.value]["inputSchema"],
            {"shell_id": "s", "exec_id": "e", "text": "界"},
        ),
        (
            posix[ToolName.SHELL_WRITE.value]["inputSchema"],
            {"shell_id": "s", "exec_id": "e", "data_base64": "AAAA"},
        ),
        (
            windows[ToolName.SHELL_OPEN.value]["inputSchema"],
            {"env": {"PATH": "one", "Path": "two"}},
        ),
        (
            windows[ToolName.SHELL_OPEN.value]["inputSchema"],
            {"cwd": r"\\.\COM1"},
        ),
        (
            posix[ToolName.SHELL_CLOSE.value]["inputSchema"],
            {"shell_id": "界" * 43},
        ),
        (
            posix[ToolName.SHELL_EXEC.value]["inputSchema"],
            {"shell_id": "s", "command": "a\x00b"},
        ),
        (
            posix[ToolName.SHELL_OPEN.value]["inputSchema"],
            {"cwd": "/tmp/\x00bad"},
        ),
        (
            posix[ToolName.SHELL_EXEC.value]["outputSchema"],
            running_with_reversed_cursors,
        ),
        (
            posix[ToolName.SHELL_EXEC.value]["inputSchema"],
            {"shell_id": "s", "command": "\ud800"},
        ),
        (
            posix[ToolName.SHELL_EXEC.value]["outputSchema"],
            {**running_with_reversed_cursors, "buffer_start_cursor": 0, "output": "\ud800"},
        ),
        (
            posix[ToolName.SHELL_READ.value]["inputSchema"],
            {"shell_id": "s", "exec_id": "e", "cursor": 1.0},
        ),
        (
            posix[ToolName.SHELL_EXEC.value]["inputSchema"],
            {"shell_id": "s", "command": "true", "yield_ms": 1.0},
        ),
    ]
    for schema, payload in invalid_pairs:
        vocabulary = schema["x-requiredVocabulary"]
        assert isinstance(vocabulary, str)
        assert vocabulary.endswith("/schema/v1")
        with pytest.raises(JsonSchemaValidationError):
            _validate_schema(schema, payload)

    with pytest.raises(ProtocolValidationError):
        validate_tool_input(
            ToolName.SHELL_EXEC,
            {"shell_id": "s", "command": "界界"},
            config=tiny_posix,
        )
    with pytest.raises(ValidationError):
        validate_tool_output(
            ToolName.SHELL_EXEC,
            running_with_reversed_cursors,
            config=tiny_posix,
        )
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(
            ToolName.SHELL_OPEN,
            {"cwd": r"\\.\COM1"},
            config=WINDOWS_CONFIG,
        )


def test_real_json_schema_binds_output_to_runtime_profile() -> None:
    windows = tool_contract_schemas(WINDOWS_CONFIG)
    open_schema = windows[ToolName.SHELL_OPEN.value]["outputSchema"]
    valid_open = {
        "shell_id": "s",
        "status": "ready",
        "cwd": r"C:\work\project",
        "dialect": "pwsh",
    }
    _validate_schema(open_schema, valid_open)
    with pytest.raises(JsonSchemaValidationError):
        _validate_schema(open_schema, {**valid_open, "dialect": "bash"})

    list_schema = windows[ToolName.SHELL_LIST.value]["outputSchema"]
    valid_list: dict[str, Any] = {
        "runtime": {
            "platform": "windows",
            "dialect": "pwsh",
            "shell_version": "7.5.0",
            "default_cwd": r"C:\work\project",
        },
        "host": {
            "mode": "standalone",
            "workspace_root": r"C:\work\project",
            "environment": {"kind": "none"},
        },
        "shells": [],
    }
    _validate_schema(list_schema, valid_list)
    with pytest.raises(JsonSchemaValidationError):
        _validate_schema(
            list_schema,
            {**valid_list, "runtime": {**valid_list["runtime"], "platform": "linux"}},
        )
    with pytest.raises(JsonSchemaValidationError):
        _validate_schema(
            list_schema,
            {
                **valid_list,
                "shells": [
                    {
                        "shell_id": "s",
                        "status": "busy",
                        "last_known_cwd": r"C:\work\project",
                        "active_exec_id": None,
                        "created_at_ms": 1,
                    }
                ],
            },
        )


def test_contract_schema_snapshot() -> None:
    snapshots = {
        "posix-bash": tool_contract_schemas(
            ProtocolConfig(
                platform=PlatformName.LINUX,
                default_cwd="/workspace",
                shell="/bin/bash",
            )
        ),
        "windows-pwsh": tool_contract_schemas(WINDOWS_CONFIG),
    }
    snapshot_path = Path(__file__).with_name("snapshots") / "protocol-v1.json"
    assert json.loads(snapshot_path.read_text()) == snapshots


@pytest.mark.parametrize("tool", list(ToolName))
def test_unknown_fields_are_rejected(tool: ToolName) -> None:
    valid: dict[ToolName, dict[str, object]] = {
        ToolName.SHELL_OPEN: {},
        ToolName.SHELL_EXEC: {"shell_id": "shell_1", "command": "true"},
        ToolName.SHELL_READ: {"shell_id": "shell_1", "exec_id": "exec_1", "cursor": 0},
        ToolName.SHELL_WRITE: {"shell_id": "shell_1", "exec_id": "exec_1", "text": ""},
        ToolName.SHELL_SIGNAL: {
            "shell_id": "shell_1",
            "exec_id": "exec_1",
            "signal": "interrupt",
        },
        ToolName.SHELL_LIST: {},
        ToolName.SHELL_CLOSE: {"shell_id": "shell_1"},
    }
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(tool, {**valid[tool], "unknown": True}, config=POSIX_CONFIG)


@pytest.mark.parametrize("payload", [None, [], "{}", 1])
def test_tool_input_must_be_an_object(payload: object) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(ToolName.SHELL_LIST, payload, config=POSIX_CONFIG)


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        (ToolName.SHELL_EXEC, {"shell_id": "s", "command": "true", "yield_ms": None}),
        (ToolName.SHELL_READ, {"shell_id": "s", "exec_id": "e", "cursor": None}),
        (ToolName.SHELL_CLOSE, {"shell_id": None}),
        (ToolName.SHELL_SIGNAL, {"shell_id": "s", "exec_id": "e", "signal": None}),
    ],
)
def test_illegal_null_is_rejected(tool: ToolName, payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(tool, payload, config=POSIX_CONFIG)


@pytest.mark.parametrize("invalid_integer", [True, 1.0, "1"])
def test_integer_fields_are_strict(invalid_integer: object) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(
            ToolName.SHELL_READ,
            {"shell_id": "s", "exec_id": "e", "cursor": invalid_integer},
            config=POSIX_CONFIG,
        )


@pytest.mark.parametrize(
    ("tool", "payload"),
    [
        (ToolName.SHELL_OPEN, {"cwd": "/tmp/\x00bad"}),
        (ToolName.SHELL_OPEN, {"shell": "/bin/\x00bash"}),
        (ToolName.SHELL_OPEN, {"startup_command": "echo \x00"}),
        (ToolName.SHELL_OPEN, {"env": {"OK": "bad\x00"}}),
        (ToolName.SHELL_EXEC, {"shell_id": "s", "command": "echo \x00"}),
    ],
)
def test_native_strings_reject_nul(tool: ToolName, payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(tool, payload, config=POSIX_CONFIG)


@pytest.mark.parametrize("key", ["", "1ABC", "A-B", "é", "A.B"])
def test_environment_keys_use_portable_identifier_grammar(key: str) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(ToolName.SHELL_OPEN, {"env": {key: "value"}}, config=POSIX_CONFIG)


def test_windows_environment_rejects_case_insensitive_duplicates() -> None:
    with pytest.raises(ProtocolValidationError, match="unique ignoring case"):
        validate_tool_input(
            ToolName.SHELL_OPEN,
            {"env": {"PATH": "one", "Path": "two"}},
            config=WINDOWS_CONFIG,
        )

    result = validate_tool_input(
        ToolName.SHELL_OPEN,
        {"env": {"PATH": "one", "Path": "two"}},
        config=POSIX_CONFIG,
    )
    assert isinstance(result, ShellOpenInput)
    assert result.env == {"PATH": "one", "Path": "two"}


def test_platform_native_absolute_paths_are_contextual() -> None:
    windows = validate_tool_input(ToolName.SHELL_OPEN, {}, config=WINDOWS_CONFIG)
    assert isinstance(windows, ShellOpenInput)
    assert windows.cwd == r"C:\work\project"

    windows_schema = tool_contract_schemas(WINDOWS_CONFIG)[ToolName.SHELL_OPEN.value][
        "inputSchema"
    ]
    for unc_path in (r"\\server\share", "//server/share"):
        unc_result = validate_tool_input(
            ToolName.SHELL_OPEN, {"cwd": unc_path}, config=WINDOWS_CONFIG
        )
        assert isinstance(unc_result, ShellOpenInput)
        assert unc_result.cwd == unc_path
        _validate_schema(windows_schema, {"cwd": unc_path})

    with pytest.raises(ProtocolValidationError, match="absolute windows path"):
        validate_tool_input(ToolName.SHELL_OPEN, {"cwd": "/workspace"}, config=WINDOWS_CONFIG)
    with pytest.raises(ProtocolValidationError, match="absolute macos path"):
        validate_tool_input(
            ToolName.SHELL_OPEN,
            {"cwd": r"C:\work\project"},
            config=POSIX_CONFIG,
        )


def test_identifiers_use_utf8_byte_limits() -> None:
    valid = "界" * 42
    result = validate_tool_input(ToolName.SHELL_CLOSE, {"shell_id": valid})
    assert isinstance(result, ShellCloseInput)
    assert result.shell_id == valid
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(ToolName.SHELL_CLOSE, {"shell_id": "界" * 43})


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        ({"shell_id": "s", "exec_id": "e", "text": "yes\n"}, ShellWriteTextInput),
        ({"shell_id": "s", "exec_id": "e", "data_base64": "AA=="}, ShellWriteBase64Input),
    ],
)
def test_write_one_of_accepts_exactly_one_variant(
    payload: dict[str, object], expected_type: type[object]
) -> None:
    assert isinstance(validate_tool_input(ToolName.SHELL_WRITE, payload), expected_type)


@pytest.mark.parametrize(
    "payload",
    [
        {"shell_id": "s", "exec_id": "e"},
        {"shell_id": "s", "exec_id": "e", "text": "x", "data_base64": "eA=="},
        {"shell_id": "s", "exec_id": "e", "data_base64": "not-base64"},
        {"shell_id": "s", "exec_id": "e", "data_base64": "AB=="},
        {"shell_id": "s", "exec_id": "e", "eof": True},
    ],
)
def test_write_one_of_rejects_conflicts_invalid_base64_and_provisional_eof(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ProtocolValidationError):
        validate_tool_input(ToolName.SHELL_WRITE, payload)


def test_semantic_signal_names_do_not_expose_posix_symbols() -> None:
    for signal in SignalName:
        result = validate_tool_input(
            ToolName.SHELL_SIGNAL,
            {"shell_id": "s", "exec_id": "e", "signal": signal.value},
        )
        assert isinstance(result, ShellSignalInput)
        assert result.signal is signal

    for invalid_signal in ["SIGINT", "SIGTERM", "SIGKILL", "ctrl_c"]:
        with pytest.raises(ProtocolValidationError):
            validate_tool_input(
                ToolName.SHELL_SIGNAL,
                {"shell_id": "s", "exec_id": "e", "signal": invalid_signal},
            )


def test_runtime_defaults_limits_and_explicit_null_are_resolved() -> None:
    config = ProtocolConfig(
        platform=PlatformName.MACOS,
        default_cwd="/workspace",
        shell="/custom/bash",
        startup_command="source env",
        command_yield_ms=7,
        command_timeout_ms=11,
        output_buffer_bytes=8_192,
        max_read_bytes=1_024,
        max_write_bytes=2,
    )
    open_input = validate_tool_input(ToolName.SHELL_OPEN, {}, config=config)
    assert isinstance(open_input, ShellOpenInput)
    assert open_input.cwd == "/workspace"
    assert open_input.startup_command == "source env"
    explicit_null = validate_tool_input(
        ToolName.SHELL_OPEN, {"startup_command": None}, config=config
    )
    assert isinstance(explicit_null, ShellOpenInput)
    assert explicit_null.startup_command is None

    exec_input = validate_tool_input(
        ToolName.SHELL_EXEC, {"shell_id": "s", "command": "true"}, config=config
    )
    assert isinstance(exec_input, ShellExecInput)
    assert exec_input.yield_ms == 7
    assert exec_input.timeout_ms == 11
    assert exec_input.max_output_bytes == 8_192

    with pytest.raises(ProtocolValidationError):
        validate_tool_input(
            ToolName.SHELL_WRITE,
            {"shell_id": "s", "exec_id": "e", "text": "abc"},
            config=config,
        )


@pytest.mark.parametrize(
    ("status", "model", "shell_status"),
    [
        ("running", RunningExecutionSnapshot, None),
        ("exited", ExitedExecutionSnapshot, "ready"),
        ("timeout", TimeoutExecutionSnapshot, "error"),
        ("cancelled", CancelledExecutionSnapshot, "ready"),
        ("shell_error", ShellErrorExecutionSnapshot, "error"),
    ],
)
def test_execution_snapshot_union_is_discriminated(
    status: str, model: type[object], shell_status: str | None
) -> None:
    payload: dict[str, object] = {
        "shell_id": "s",
        "exec_id": "e",
        "status": status,
        "exit_code": 0 if status == "exited" else None,
        "output": "ok\n",
        "buffer_start_cursor": 0,
        "next_cursor": 3,
        "truncated_before_cursor": False,
        "eof": status != "running",
    }
    if shell_status is not None:
        payload.update(
            duration_ms=1,
            cwd="/workspace",
            shell_status=shell_status,
            shell_rebuilt=False,
        )
    result = validate_tool_output(ToolName.SHELL_EXEC, payload, config=POSIX_CONFIG)
    assert isinstance(result, model)


@pytest.mark.parametrize(
    ("status", "shell_status"),
    [
        ("exited", "error"),
        ("timeout", "busy"),
        ("shell_error", "ready"),
    ],
)
def test_terminal_snapshot_rejects_status_specific_shell_state(
    status: str, shell_status: str
) -> None:
    payload = _terminal_payload()
    payload.update(
        status=status,
        exit_code=0 if status == "exited" else None,
        shell_status=shell_status,
    )
    with pytest.raises(ValidationError):
        validate_tool_output(ToolName.SHELL_READ, payload, config=POSIX_CONFIG)


def test_running_snapshot_rejects_terminal_fields() -> None:
    with pytest.raises(ValidationError):
        RunningExecutionSnapshot.model_validate(
            {
                "shell_id": "s",
                "exec_id": "e",
                "status": "running",
                "exit_code": None,
                "output": "",
                "buffer_start_cursor": 0,
                "next_cursor": 0,
                "truncated_before_cursor": False,
                "eof": False,
                "duration_ms": 1,
            }
        )


def test_exit_code_range_depends_on_runtime_platform() -> None:
    assert isinstance(
        validate_tool_output(
            ToolName.SHELL_EXEC,
            _terminal_payload(exit_code=255),
            config=POSIX_CONFIG,
        ),
        ExitedExecutionSnapshot,
    )
    with pytest.raises(ValidationError, match="POSIX exit_code"):
        validate_tool_output(
            ToolName.SHELL_EXEC,
            _terminal_payload(exit_code=256),
            config=POSIX_CONFIG,
        )

    windows_payload = _terminal_payload(
        exit_code=4_294_967_295,
        cwd=r"C:\work\project",
    )
    assert isinstance(
        validate_tool_output(ToolName.SHELL_EXEC, windows_payload, config=WINDOWS_CONFIG),
        ExitedExecutionSnapshot,
    )
    with pytest.raises(ValidationError):
        validate_tool_output(
            ToolName.SHELL_EXEC,
            {**windows_payload, "exit_code": 4_294_967_296},
            config=WINDOWS_CONFIG,
        )


def test_shell_open_and_list_results_expose_only_runtime_context() -> None:
    open_result = validate_tool_output(
        ToolName.SHELL_OPEN,
        {
            "shell_id": "shell_1",
            "status": "ready",
            "cwd": "/workspace",
            "dialect": "bash",
        },
        config=POSIX_CONFIG,
    )
    assert isinstance(open_result, ShellOpenResult)
    assert open_result.dialect is DialectName.BASH

    list_result = validate_tool_output(
        ToolName.SHELL_LIST,
        {
            "runtime": {
                "platform": "macos",
                "dialect": "bash",
                "shell_version": "5.2.37",
                "default_cwd": "/workspace",
            },
            "host": {
                "mode": "ide",
                "workspace_root": "/workspace",
                "environment": {"kind": "python-venv", "name": ".venv"},
            },
            "shells": [],
        },
        config=POSIX_CONFIG,
    )
    assert isinstance(list_result, ShellListResult)
    serialized = list_result.model_dump(mode="json", exclude_unset=True)
    assert serialized["host"]["environment"] == {"kind": "python-venv", "name": ".venv"}

    with pytest.raises(ValidationError):
        validate_tool_output(
            ToolName.SHELL_LIST,
            {
                **serialized,
                "host": {**serialized["host"], "startup_command": "secret"},
            },
            config=POSIX_CONFIG,
        )

    with pytest.raises(ValidationError, match="selected Runtime Profile"):
        validate_tool_output(
            ToolName.SHELL_OPEN,
            {
                "shell_id": "shell_1",
                "status": "ready",
                "cwd": "/workspace",
                "dialect": "pwsh",
            },
            config=POSIX_CONFIG,
        )
    with pytest.raises(ValidationError, match="selected Runtime Profile"):
        validate_tool_output(
            ToolName.SHELL_LIST,
            {
                **serialized,
                "runtime": {**serialized["runtime"], "platform": "linux"},
            },
            config=POSIX_CONFIG,
        )


@pytest.mark.parametrize(
    ("status", "active_exec_id"),
    [
        ("ready", None),
        ("busy", "exec_1"),
        ("rebuilding", "exec_1"),
        ("closing", None),
        ("error", None),
    ],
)
def test_shell_list_active_execution_invariant(
    status: str, active_exec_id: str | None
) -> None:
    item = ShellListItem.model_validate(
        {
            "shell_id": "shell_1",
            "status": status,
            "last_known_cwd": "/workspace",
            "active_exec_id": active_exec_id,
            "created_at_ms": 1,
        },
        context=POSIX_CONFIG,
    )
    assert item.active_exec_id == active_exec_id


@pytest.mark.parametrize(
    ("status", "active_exec_id"),
    [("busy", None), ("rebuilding", None), ("ready", "exec_1"), ("error", "exec_1")],
)
def test_shell_list_rejects_inconsistent_active_execution(
    status: str, active_exec_id: str | None
) -> None:
    with pytest.raises(ValidationError, match="exactly when"):
        ShellListItem.model_validate(
            {
                "shell_id": "shell_1",
                "status": status,
                "last_known_cwd": "/workspace",
                "active_exec_id": active_exec_id,
                "created_at_ms": 1,
            },
            context=POSIX_CONFIG,
        )


def test_environment_display_name_is_optional_and_omitted_not_null() -> None:
    summary = EnvironmentSummary.model_validate({"kind": "none"})
    assert summary.model_dump(mode="json") == {"kind": "none"}
    assert json.loads(summary.model_dump_json()) == {"kind": "none"}
    assert model_to_wire(summary) == {"kind": "none"}
    with pytest.raises(ValidationError):
        EnvironmentSummary.model_validate({"kind": "none", "name": None})


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_retryable_mapping_is_fixed(code: ErrorCode) -> None:
    expected = RETRYABLE_BY_CODE[code]
    if expected is None:
        for retryable in (False, True):
            actual = make_error(code, "state dependent", retryable=retryable)
            assert actual.error.retryable is retryable
    else:
        assert make_error(code, "failure").error.retryable is expected
        with pytest.raises(ValidationError):
            make_error(code, "failure", retryable=not expected)


def test_error_ids_are_omitted_unless_validated() -> None:
    without_ids = make_error(ErrorCode.SHELL_BUSY, "busy")
    assert without_ids.model_dump(mode="json") == {
        "error": {"code": "shell_busy", "message": "busy", "retryable": True}
    }
    assert json.loads(without_ids.model_dump_json()) == model_to_wire(without_ids)
    with pytest.raises(ValidationError):
        make_error(ErrorCode.SHELL_BUSY, "busy", shell_id="")


def test_error_codes_are_frozen_per_tool() -> None:
    expected = {
        ToolName.SHELL_OPEN: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.UNSUPPORTED_SHELL,
            ErrorCode.SHELL_START_FAILED,
            ErrorCode.RESOURCE_LIMIT,
        },
        ToolName.SHELL_EXEC: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.SHELL_NOT_FOUND,
            ErrorCode.SHELL_BUSY,
            ErrorCode.SHELL_CLOSING,
            ErrorCode.SHELL_UNAVAILABLE,
        },
        ToolName.SHELL_READ: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.INVALID_CURSOR,
            ErrorCode.SHELL_NOT_FOUND,
            ErrorCode.SHELL_CLOSING,
            ErrorCode.EXEC_NOT_FOUND,
            ErrorCode.RESOURCE_LIMIT,
        },
        ToolName.SHELL_WRITE: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.SHELL_NOT_FOUND,
            ErrorCode.SHELL_CLOSING,
            ErrorCode.SHELL_UNAVAILABLE,
            ErrorCode.EXEC_NOT_FOUND,
            ErrorCode.EXEC_NOT_ACTIVE,
            ErrorCode.RESOURCE_LIMIT,
        },
        ToolName.SHELL_SIGNAL: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.SHELL_NOT_FOUND,
            ErrorCode.SHELL_CLOSING,
            ErrorCode.SHELL_UNAVAILABLE,
            ErrorCode.EXEC_NOT_FOUND,
            ErrorCode.EXEC_NOT_ACTIVE,
            ErrorCode.RESOURCE_LIMIT,
        },
        ToolName.SHELL_LIST: {ErrorCode.INVALID_ARGUMENT},
        ToolName.SHELL_CLOSE: {
            ErrorCode.INVALID_ARGUMENT,
            ErrorCode.SHELL_NOT_FOUND,
            ErrorCode.SHELL_CLOSING,
        },
    }
    assert {tool: set(codes) for tool, codes in ERROR_CODES_BY_TOOL.items()} == expected


def test_real_json_schema_enforces_each_tools_error_contract() -> None:
    contracts = tool_contract_schemas(POSIX_CONFIG)
    for tool, allowed_codes in ERROR_CODES_BY_TOOL.items():
        schema = contracts[tool.value]["errorSchema"]
        for code in allowed_codes:
            expected = RETRYABLE_BY_CODE[code]
            retryable_values = (False, True) if expected is None else (expected,)
            for retryable in retryable_values:
                payload = {
                    "error": {
                        "code": code.value,
                        "message": "failure",
                        "retryable": retryable,
                    }
                }
                _validate_schema(schema, payload)
            if expected is not None:
                with pytest.raises(JsonSchemaValidationError):
                    _validate_schema(
                        schema,
                        {
                            "error": {
                                "code": code.value,
                                "message": "failure",
                                "retryable": not expected,
                            }
                        },
                    )

        forbidden_code = next(code for code in ErrorCode if code not in allowed_codes)
        with pytest.raises(JsonSchemaValidationError):
            _validate_schema(
                schema,
                {
                    "error": {
                        "code": forbidden_code.value,
                        "message": "failure",
                        "retryable": False,
                    }
                },
            )
        first_code = next(iter(allowed_codes))
        first_retryable = RETRYABLE_BY_CODE[first_code]
        with pytest.raises(JsonSchemaValidationError):
            _validate_schema(
                schema,
                {
                    "error": {
                        "code": first_code.value,
                        "message": "failure",
                        "retryable": False if first_retryable is None else first_retryable,
                        "shell_id": None,
                    }
                },
            )


def test_platform_specific_schema_defaults_and_exit_code_maximum() -> None:
    posix = cast(dict[str, Any], tool_contract_schemas(POSIX_CONFIG))
    windows = cast(dict[str, Any], tool_contract_schemas(WINDOWS_CONFIG))

    assert posix["shell_open"]["inputSchema"]["properties"]["cwd"]["default"] == "/workspace"
    assert (
        windows["shell_open"]["inputSchema"]["properties"]["cwd"]["default"]
        == r"C:\work\project"
    )
    for tool in ["shell_exec", "shell_read"]:
        posix_exited = posix[tool]["outputSchema"]["$defs"]["ExitedExecutionSnapshot"]
        windows_exited = windows[tool]["outputSchema"]["$defs"]["ExitedExecutionSnapshot"]
        assert posix_exited["properties"]["exit_code"]["maximum"] == 255
        assert windows_exited["properties"]["exit_code"]["maximum"] == 4_294_967_295


def test_output_schema_does_not_expose_sensitive_host_fields() -> None:
    schema_text = json.dumps(tool_contract_schemas(POSIX_CONFIG)["shell_list"]["outputSchema"])
    for forbidden in ["env_value", "startup_command", "interpreter_path", "secret"]:
        assert forbidden not in schema_text


def test_union_adapter_remains_usable_by_domain_layer() -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(RunningExecutionSnapshot | ExitedExecutionSnapshot)
    result = adapter.validate_python(
        {
            "shell_id": "s",
            "exec_id": "e",
            "status": "running",
            "exit_code": None,
            "output": "",
            "buffer_start_cursor": 0,
            "next_cursor": 0,
            "truncated_before_cursor": False,
            "eof": False,
        }
    )
    assert isinstance(result, RunningExecutionSnapshot)
