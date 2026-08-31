from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import cast

import pytest

from tfbash_mcp.runtime import (
    DialectEvent,
    DialectEventKind,
    DialectProtocolError,
    DialectSessionPlan,
    PowerShellDialect,
    PowerShellProtocol,
    ShellStartRequest,
    UnsupportedShell,
)


def _token_factory(*tokens: str) -> Callable[[], str]:
    remaining = iter(tokens)
    return lambda: next(remaining)


def _plan(
    *,
    startup_command: str | None = None,
    tokens: tuple[str, ...] = ("A" * 32, "B" * 32, "C" * 32, "D" * 32),
) -> DialectSessionPlan:
    return PowerShellDialect(token_factory=_token_factory(*tokens)).prepare_session(
        ShellStartRequest(
            executable=r"C:\Program Files\PowerShell\7\pwsh.exe",
            cwd=r"C:\workspace",
            environment={"PROJECT": "test"},
            startup_command=startup_command,
        )
    )


def _bootstrap(protocol: PowerShellProtocol) -> None:
    payload = b"terminal setup" + protocol._launch_marker + protocol._prompt
    assert protocol.feed(payload) == (DialectEvent(DialectEventKind.BOOTSTRAP_REQUIRED),)


def _startup_bytes(
    protocol: PowerShellProtocol,
    *,
    probe: int = 0,
    exit_code: int = 0,
    version: str = "7.6.3",
    cwd: str = r"C:\workspace",
    include_prompt: bool = True,
) -> bytes:
    fields = b":".join(
        (
            str(probe).encode(),
            str(exit_code).encode(),
            base64.b64encode(version.encode()),
            base64.b64encode(cwd.encode()),
        )
    )
    prompt = protocol._prompt if include_prompt else b""
    return b"bootstrap echo" + protocol._ready_prefix + fields + b"\x1f" + prompt


def _ready(
    protocol: PowerShellProtocol,
    *,
    probe: int = 0,
    exit_code: int = 0,
    version: str = "7.6.3",
    cwd: str = r"C:\workspace",
) -> DialectEvent:
    _bootstrap(protocol)
    events = protocol.feed(
        _startup_bytes(
            protocol,
            probe=probe,
            exit_code=exit_code,
            version=version,
            cwd=cwd,
        )
    )
    assert len(events) == 1
    assert events[0].kind is DialectEventKind.READY
    return events[0]


def _command_bytes(
    protocol: PowerShellProtocol,
    correlation_id: str,
    *,
    output: bytes,
    exit_code: int = 0,
    cwd: str = r"C:\workspace",
    include_prompt: bool = True,
) -> bytes:
    token = correlation_id.removeprefix("pwsh_")
    begin = b"\x1eTFPWSH_BEGIN_" + token.encode() + b"\x1f"
    result = (
        b"\x1eTFPWSH_END_"
        + token.encode()
        + b":"
        + str(exit_code).encode()
        + b":"
        + base64.b64encode(cwd.encode())
        + b"\x1f"
    )
    prompt = protocol._prompt if include_prompt else b""
    return b"rendered encoded wrapper\r\n" + begin + output + result + prompt


def _control_echo(input_bytes: bytes, *, row: int = 4) -> bytes:
    line = input_bytes.rstrip(b"\r\n")
    return f"\x1b[{row};18H".encode() + line + f"\x1b[{row};99H".encode() + b"\r\n"


def _feed_at_split(
    protocol: PowerShellProtocol,
    payload: bytes,
    split: int,
) -> tuple[DialectEvent, ...]:
    return protocol.feed(payload[:split]) + protocol.feed(payload[split:])


def _feed_bytewise(
    protocol: PowerShellProtocol,
    payload: bytes,
) -> tuple[DialectEvent, ...]:
    events: list[DialectEvent] = []
    for byte in payload:
        events.extend(protocol.feed(bytes((byte,))))
    return tuple(events)


def test_prepare_session_builds_pinned_noninteractive_pwsh_launch() -> None:
    plan = _plan(startup_command="$env:ACTIVE_ENV='yes'")
    arguments = plan.launch.spawn.arguments

    assert arguments[:4] == ("-NoLogo", "-NoProfile", "-NoExit", "-NonInteractive")
    assert arguments[4] == "-EncodedCommand"
    launch_script = base64.b64decode(arguments[5]).decode("utf-16-le")
    assert "function global:prompt" in launch_script
    assert "TFPWSH_LAUNCH_" in launch_script
    assert "ACTIVE_ENV" not in plan.launch.initial_input.decode("ascii")
    assert isinstance(plan.protocol, PowerShellProtocol)


@pytest.mark.parametrize("token_length", [16, 32, 64])
def test_posix_launch_uses_console_input_without_native_line_editing(token_length: int) -> None:
    session_token = "A" * token_length
    plan = PowerShellDialect(
        token_factory=_token_factory(session_token),
        default_executable="/usr/bin/pwsh",
        windows_paths=False,
    ).prepare_session(
        ShellStartRequest(
            executable="/usr/bin/pwsh",
            cwd="/workspace",
            environment={"PROJECT": "test"},
            startup_command=None,
        )
    )

    launch_script = base64.b64decode(plan.launch.spawn.arguments[5]).decode("utf-16-le")
    assert "[Console]::In.ReadLine()" in launch_script
    assert "[ScriptBlock]::Create($__tf_input)" in launch_script
    assert "/bin/stty -echo -icanon min 1 time 0" in launch_script
    assert "TFPWSH_LAUNCH_" in launch_script
    input_lines = plan.launch.initial_input.splitlines()
    assert len(input_lines) > 2
    assert max(map(len, input_lines)) < 256
    payload_chunks = re.findall(
        rb"__TFPWSH_CHUNK_"
        + re.escape(session_token.encode())
        + rb":[SA]:"
        + re.escape(session_token.encode())
        + rb":([A-Za-z0-9+/=]+)",
        plan.launch.initial_input,
    )
    reconstructed = base64.b64decode(b"".join(payload_chunks)).decode("utf-8")
    assert "TFPWSH_READY_" in reconstructed
    assert "function global:prompt" in reconstructed
    assert (
        input_lines[-1]
        == b"__TFPWSH_CHUNK_" + session_token.encode() + b":X:" + session_token.encode() + b":"
    )
    assert "$__tf_prompt_required=$false" in launch_script
    assert (
        "$null=$__tf_payload.Clear();$__tf_payload_token='';"
        "try{. ([ScriptBlock]::Create($__tf_input))}" in launch_script
    )
    assert (
        "$__tf_chunk='';$__tf_chunk_data='';$__tf_chunk_op='';"
        "$__tf_chunk_token='';$__tf_separator=-1;$__tf_input='';"
        "$__tf_input=[Console]::In.ReadLine()" in launch_script
    )


def test_posix_large_command_chunks_do_not_require_intermediate_prompts() -> None:
    session_token = "A" * 32
    command_token = "B" * 32
    plan = PowerShellDialect(
        token_factory=_token_factory(session_token, command_token),
        default_executable="/usr/bin/pwsh",
        windows_paths=False,
    ).prepare_session(
        ShellStartRequest(
            executable="/usr/bin/pwsh",
            cwd="/workspace",
            environment={"PROJECT": "test"},
            startup_command=None,
        )
    )
    protocol = cast(PowerShellProtocol, plan.protocol)
    _ready(protocol, cwd="/workspace")
    command = "x" * 262_144

    frame = protocol.wrap_command(command)

    lines = frame.input_bytes.splitlines()
    prefix = f"__TFPWSH_CHUNK_{session_token}:".encode()
    assert len(lines) > 500
    assert max(map(len, lines)) < 256
    assert all(line.startswith(prefix) for line in lines)
    assert lines[0].startswith(prefix + f"S:{command_token}:".encode())
    assert all(line.startswith(prefix + f"A:{command_token}:".encode()) for line in lines[1:-1])
    assert lines[-1] == prefix + f"X:{command_token}:".encode()
    encoded_chunks = [line.rsplit(b":", 1)[-1] for line in lines[:-1]]
    private_script = base64.b64decode(b"".join(encoded_chunks)).decode("utf-8")
    assert base64.b64encode(command.encode()).decode() in private_script


def test_default_executable_is_drive_qualified_pwsh_7() -> None:
    assert PowerShellDialect.default_executable == r"C:\Program Files\PowerShell\7\pwsh.exe"


def test_each_session_has_private_prompt_control_function_and_state() -> None:
    dialect = PowerShellDialect(token_factory=_token_factory("A" * 32, "B" * 32))
    request = ShellStartRequest(
        r"C:\PowerShell\pwsh.exe",
        r"C:\workspace",
        {},
        None,
    )

    first = cast(PowerShellProtocol, dialect.prepare_session(request).protocol)
    second = cast(PowerShellProtocol, dialect.prepare_session(request).protocol)

    assert first._prompt != second._prompt
    assert first._control_function != second._control_function
    assert first._launch_marker != second._launch_marker


def test_startup_record_is_incremental_and_reports_unicode_cwd() -> None:
    template = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(template)
    payload = _startup_bytes(template, version="7.6.3", cwd=r"C:\工作区\项目")

    events: list[DialectEvent] = []
    for byte in payload:
        events.extend(template.feed(bytes((byte,))))

    assert events == [
        DialectEvent(
            DialectEventKind.READY,
            cwd=r"C:\工作区\项目",
            shell_version="7.6.3",
        )
    ]


def test_startup_fails_closed_for_wrong_runtime_encoding_and_command() -> None:
    incompatible = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(incompatible)
    with pytest.raises(UnsupportedShell, match="admitted PowerShell runtime"):
        incompatible.feed(_startup_bytes(incompatible, probe=1))

    encoding = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(encoding)
    with pytest.raises(UnsupportedShell, match="UTF-8"):
        encoding.feed(_startup_bytes(encoding, probe=2))

    failed = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(failed)
    with pytest.raises(DialectProtocolError, match="exit code 17"):
        failed.feed(_startup_bytes(failed, exit_code=17))


def test_command_parser_handles_every_marker_split_and_uint32_exit() -> None:
    template = cast(PowerShellProtocol, _plan().protocol)
    _ready(template)
    template_frame = template.wrap_command("Write-Output '你好🙂'")
    template_output = (
        b"prefix\x1b[31mred\x1b[0m\x1b]133;A\x07" + template._prompt + "你好🙂".encode()
    )
    template_payload = _command_bytes(
        template,
        template_frame.correlation_id,
        output=template_output,
        exit_code=4_294_967_295,
        cwd=r"C:\workspace\子目录",
    )

    for split in range(len(template_payload) + 1):
        protocol = cast(PowerShellProtocol, _plan().protocol)
        _ready(protocol)
        frame = protocol.wrap_command("Write-Output '你好🙂'")
        output = b"prefix\x1b[31mred\x1b[0m\x1b]133;A\x07" + protocol._prompt + "你好🙂".encode()
        payload = _command_bytes(
            protocol,
            frame.correlation_id,
            output=output,
            exit_code=4_294_967_295,
            cwd=r"C:\workspace\子目录",
        )
        events = _feed_at_split(protocol, payload, split)

        assert (
            b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
            == output
        ), f"output mismatch at split {split}"
        assert [event for event in events if event.kind is DialectEventKind.COMMAND_COMPLETE] == [
            DialectEvent(
                DialectEventKind.COMMAND_COMPLETE,
                correlation_id=frame.correlation_id,
                exit_code=4_294_967_295,
                cwd=r"C:\workspace\子目录",
            )
        ], f"completion mismatch at split {split}"


def test_completion_waits_for_private_prompt() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("$true")
    before_prompt = _command_bytes(
        protocol,
        frame.correlation_id,
        output=b"done\r\n",
        include_prompt=False,
    )

    events = protocol.feed(before_prompt)
    assert all(event.kind is not DialectEventKind.COMMAND_COMPLETE for event in events)
    with pytest.raises(DialectProtocolError, match="not ready"):
        protocol.wrap_command("Write-Output too-early")

    completed = protocol.feed(protocol._prompt)
    assert completed[-1].kind is DialectEventKind.COMMAND_COMPLETE


def test_finalization_strips_conpty_input_echo_and_preserves_late_output_at_every_split() -> None:
    template = cast(PowerShellProtocol, _plan().protocol)
    _ready(template)
    command = template.wrap_command("Start-Job { 'late' }")
    template.feed(_command_bytes(template, command.correlation_id, output=b"FIRST"))
    finalization = template.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    payload = (
        b"ONTERM\r\n"
        + _control_echo(finalization.input_bytes)
        + b"\x1eTFPWSH_FINALIZE_"
        + token.encode()
        + b"\x1f\r\njob-finished\r\n"
        + template._prompt
    )

    for split in range(len(payload) + 1):
        protocol = cast(PowerShellProtocol, _plan().protocol)
        _ready(protocol)
        current = protocol.wrap_command("Start-Job { 'late' }")
        protocol.feed(_command_bytes(protocol, current.correlation_id, output=b"FIRST"))
        current_finalization = protocol.begin_finalization()
        events = _feed_at_split(protocol, payload, split)

        assert (
            b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
            == b"ONTERM\r\njob-finished\r\n"
        ), f"output mismatch at split {split}"
        assert events[-1].kind is DialectEventKind.FINALIZED
        assert [event for event in events if event.kind is DialectEventKind.FINALIZED] == [
            DialectEvent(
                DialectEventKind.FINALIZED,
                correlation_id=current_finalization.correlation_id,
            )
        ]


def test_finalization_supports_transport_with_input_echo_disabled() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")

    events = protocol.feed(
        b"late" + b"\x1eTFPWSH_FINALIZE_" + token.encode() + b"\x1f" + protocol._prompt
    )

    assert events == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"late"),
        DialectEvent(DialectEventKind.FINALIZED, correlation_id=finalization.correlation_id),
    )


def test_recovery_requires_private_record_and_prompt_and_preserves_output() -> None:
    template = cast(PowerShellProtocol, _plan().protocol)
    _ready(template)
    frame = template.wrap_command("Start-Sleep 30")
    token = frame.correlation_id.removeprefix("pwsh_")
    template.feed(b"\x1eTFPWSH_BEGIN_" + token.encode() + b"\x1fpartial")
    recovery_input = template.recovery_input()
    recovery_token = recovery_input.decode().split()[-1].strip()
    payload = (
        template._prompt
        + _control_echo(recovery_input)
        + b"\x1eTFPWSH_RECOVER_BEGIN_"
        + recovery_token.encode()
        + b"\x1f"
        + b"between-recovery-records\r\n"
        + b"\x1eTFPWSH_RECOVER_END_"
        + recovery_token.encode()
        + b":"
        + base64.b64encode(rb"C:\workspace")
        + b"\x1f\r\n"
        + template._prompt
    )

    for split in range(len(payload) + 1):
        protocol = cast(PowerShellProtocol, _plan().protocol)
        _ready(protocol)
        current = protocol.wrap_command("Start-Sleep 30")
        current_token = current.correlation_id.removeprefix("pwsh_")
        protocol.feed(b"\x1eTFPWSH_BEGIN_" + current_token.encode() + b"\x1fpartial")
        protocol.recovery_input()
        events = _feed_at_split(protocol, payload, split)

        assert (
            b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
            == b"partialbetween-recovery-records\r\n"
        ), f"output mismatch at split {split}"
        assert events[-1].kind is DialectEventKind.RECOVERED
        assert [event for event in events if event.kind is DialectEventKind.RECOVERED] == [
            DialectEvent(
                DialectEventKind.RECOVERED,
                correlation_id=current.correlation_id,
                cwd=r"C:\workspace",
            )
        ]


def test_same_chunk_prompt_tail_is_emitted_before_terminal_events() -> None:
    finalized_protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(finalized_protocol)
    command = finalized_protocol.wrap_command("$true")
    finalized_protocol.feed(_command_bytes(finalized_protocol, command.correlation_id, output=b""))
    finalization = finalized_protocol.begin_finalization()
    finalization_token = finalization.correlation_id.removeprefix("finalize_")
    finalized_events = finalized_protocol.feed(
        b"\x1eTFPWSH_FINALIZE_"
        + finalization_token.encode()
        + b"\x1f\r\n"
        + finalized_protocol._prompt
        + b"tail-after-finalization-prompt\r\n"
    )

    assert finalized_events == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"tail-after-finalization-prompt\r\n"),
        DialectEvent(DialectEventKind.FINALIZED, correlation_id=finalization.correlation_id),
    )

    recovered_protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(recovered_protocol)
    recovered_command = recovered_protocol.wrap_command("Start-Sleep 30")
    command_token = recovered_command.correlation_id.removeprefix("pwsh_")
    recovered_protocol.feed(b"\x1eTFPWSH_BEGIN_" + command_token.encode() + b"\x1fpartial")
    recovery_input = recovered_protocol.recovery_input()
    recovery_token = recovery_input.decode().split()[-1].strip()
    recovered_events = recovered_protocol.feed(
        b"\x1eTFPWSH_RECOVER_BEGIN_"
        + recovery_token.encode()
        + b"\x1f"
        + b"\x1eTFPWSH_RECOVER_END_"
        + recovery_token.encode()
        + b":"
        + base64.b64encode(rb"C:\workspace")
        + b"\x1f\r\n"
        + recovered_protocol._prompt
        + b"tail-after-recovery-prompt\r\n"
    )

    assert recovered_events[-2:] == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"tail-after-recovery-prompt\r\n"),
        DialectEvent(
            DialectEventKind.RECOVERED,
            correlation_id=recovered_command.correlation_id,
            cwd=r"C:\workspace",
        ),
    )


def test_recovery_does_not_claim_success_from_prompt_without_private_record() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("Start-Sleep 30")
    token = frame.correlation_id.removeprefix("pwsh_")
    protocol.feed(b"\x1eTFPWSH_BEGIN_" + token.encode() + b"\x1fpartial")
    recovery_input = protocol.recovery_input()

    events = protocol.feed(protocol._prompt + _control_echo(recovery_input) + protocol._prompt)

    assert all(event.kind is not DialectEventKind.RECOVERED for event in events)
    with pytest.raises(DialectProtocolError, match="not ready"):
        protocol.wrap_command("Write-Output false-positive")


def test_control_echo_allows_inline_vt_sequences_without_leaking_private_input() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    echo = finalization.input_bytes.rstrip(b"\r\n")
    mutated_echo = echo[:9] + b"\x1b[31m" + echo[9:41] + b"\x1b[0m" + echo[41:]
    payload = (
        b"visible-before\r\n\x1b[4;18H"
        + mutated_echo
        + b"\x1b[4;99H\r\n"
        + b"\x1eTFPWSH_FINALIZE_"
        + token.encode()
        + b"\x1f\r\n"
        + protocol._prompt
    )

    events = _feed_bytewise(protocol, payload)

    assert events == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"visible-before\r\n"),
        DialectEvent(DialectEventKind.FINALIZED, correlation_id=finalization.correlation_id),
    )


def test_finalization_preserves_output_between_echo_and_first_marker_bytewise() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    payload = (
        _control_echo(finalization.input_bytes)
        + b"late-after-echo\r\n"
        + b"\x1eTFPWSH_FINALIZE_"
        + token.encode()
        + b"\x1f\r\n"
        + protocol._prompt
    )

    events = _feed_bytewise(protocol, payload)

    assert (
        b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        == b"late-after-echo\r\n"
    )
    assert events[-1] == DialectEvent(
        DialectEventKind.FINALIZED,
        correlation_id=finalization.correlation_id,
    )


def test_recovery_preserves_output_between_echo_and_both_markers_bytewise() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("Start-Sleep 30")
    command_token = command.correlation_id.removeprefix("pwsh_")
    protocol.feed(b"\x1eTFPWSH_BEGIN_" + command_token.encode() + b"\x1fpartial")
    recovery_input = protocol.recovery_input()
    recovery_token = recovery_input.decode().split()[-1].strip()
    payload = (
        protocol._prompt
        + _control_echo(recovery_input)
        + b"late-after-echo\r\n"
        + b"\x1eTFPWSH_RECOVER_BEGIN_"
        + recovery_token.encode()
        + b"\x1f"
        + b"late-between-markers\r\n"
        + b"\x1eTFPWSH_RECOVER_END_"
        + recovery_token.encode()
        + b":"
        + base64.b64encode(rb"C:\workspace")
        + b"\x1f\r\n"
        + protocol._prompt
    )

    events = _feed_bytewise(protocol, payload)

    assert (
        b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        == b"partiallate-after-echo\r\nlate-between-markers\r\n"
    )
    assert events[-1] == DialectEvent(
        DialectEventKind.RECOVERED,
        correlation_id=command.correlation_id,
        cwd=r"C:\workspace",
    )


def test_eof_preserves_output_after_recognized_control_echo() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    late_output = b"late-after-echo-before-eof\r\n" * 4

    events = (
        _feed_bytewise(
            protocol,
            _control_echo(finalization.input_bytes) + late_output,
        )
        + protocol.end_of_stream()
    )

    assert (
        b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        == late_output
    )


def test_unterminated_osc_cannot_hide_finalization_marker_bytewise() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    unterminated_osc = b"\x1b]0;unterminated"
    payload = (
        finalization.input_bytes.rstrip(b"\r\n")
        + unterminated_osc
        + b"\x1eTFPWSH_FINALIZE_"
        + token.encode()
        + b"\x1f\r\n"
        + protocol._prompt
    )

    events = _feed_bytewise(protocol, payload)

    assert (
        b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        == unterminated_osc
    )
    assert events[-1] == DialectEvent(
        DialectEventKind.FINALIZED,
        correlation_id=finalization.correlation_id,
    )


def test_unterminated_osc_cannot_hide_recovery_marker_bytewise() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("Start-Sleep 30")
    command_token = command.correlation_id.removeprefix("pwsh_")
    protocol.feed(b"\x1eTFPWSH_BEGIN_" + command_token.encode() + b"\x1fpartial")
    recovery_input = protocol.recovery_input()
    recovery_token = recovery_input.decode().split()[-1].strip()
    unterminated_osc = b"\x1b]2;unterminated"
    payload = (
        protocol._prompt
        + recovery_input.rstrip(b"\r\n")
        + unterminated_osc
        + b"\x1eTFPWSH_RECOVER_BEGIN_"
        + recovery_token.encode()
        + b"\x1f"
        + b"\x1eTFPWSH_RECOVER_END_"
        + recovery_token.encode()
        + b":"
        + base64.b64encode(rb"C:\workspace")
        + b"\x1f\r\n"
        + protocol._prompt
    )

    events = _feed_bytewise(protocol, payload)

    assert (
        b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        == b"partial" + unterminated_osc
    )
    assert events[-1] == DialectEvent(
        DialectEventKind.RECOVERED,
        correlation_id=command.correlation_id,
        cwd=r"C:\workspace",
    )


def test_eof_fails_closed_for_private_marker_hidden_in_unterminated_osc() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    marker = b"\x1eTFPWSH_FINALIZE_" + token.encode() + b"\x1f"
    _feed_bytewise(
        protocol,
        finalization.input_bytes.rstrip(b"\r\n") + b"\x1b]0;unterminated" + marker[:24],
    )

    with pytest.raises(DialectProtocolError, match="private.*fragment"):
        protocol.end_of_stream()
    assert protocol.end_of_stream() == ()


def test_control_echo_fails_closed_when_private_input_is_truncated_before_marker() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    token = finalization.correlation_id.removeprefix("finalize_")
    truncated_echo = finalization.input_bytes.rstrip(b"\r\n")[:-5]

    with pytest.raises(DialectProtocolError, match="ambiguous.*echo"):
        protocol.feed(
            truncated_echo + b"\x1eTFPWSH_FINALIZE_" + token.encode() + b"\x1f" + protocol._prompt
        )


def test_control_echo_fails_closed_on_eof_with_private_fragment() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = protocol.wrap_command("$true")
    protocol.feed(_command_bytes(protocol, command.correlation_id, output=b""))
    finalization = protocol.begin_finalization()
    protocol.feed(finalization.input_bytes[:24])

    with pytest.raises(DialectProtocolError, match="ambiguous.*echo"):
        protocol.end_of_stream()
    assert protocol.end_of_stream() == ()


def test_large_output_streams_without_growing_control_buffer() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("1..100000")
    token = frame.correlation_id.removeprefix("pwsh_")
    protocol.feed(b"\x1eTFPWSH_BEGIN_" + token.encode() + b"\x1f")

    events = protocol.feed(b"x" * 1_000_000)

    assert sum(len(event.data) for event in events) > 999_900
    assert len(protocol._buffer) < 100


def test_eof_flushes_unfinished_user_output_once_without_control_echo() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("throw 'failed'")
    token = frame.correlation_id.removeprefix("pwsh_")
    protocol.feed(b"\x1eTFPWSH_BEGIN_" + token.encode() + b"\x1fpartial")

    assert protocol.end_of_stream() == (DialectEvent(DialectEventKind.OUTPUT, data=b"partial"),)
    assert protocol.end_of_stream() == ()
    with pytest.raises(DialectProtocolError, match="closed"):
        protocol.feed(b"late")


def test_command_wrapper_preserves_multiline_pipeline_and_unicode_as_utf8_base64() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    command = "$env:KEEP='持久'\n1..3 | ForEach-Object { \"你好🙂/$($_)\" }"

    frame = protocol.wrap_command(command)
    decoded = frame.input_bytes.decode("ascii")
    outer_match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", decoded)

    assert outer_match is not None
    outer_script = base64.b64decode(outer_match.group(1)).decode("utf-8")
    inner_blobs = re.findall(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", outer_script)
    assert any(base64.b64decode(blob).decode("utf-8") == command for blob in inner_blobs)
    assert frame.input_bytes.count(b"\n") == 1
    assert b"LASTEXITCODE" not in command.encode("utf-8")
    assert "$global:LASTEXITCODE=0" in outer_script


def test_command_wrapper_prioritizes_current_powershell_success_over_stale_native_exit() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("cmd /c exit 23; Write-Output recovered")
    outer_script = base64.b64decode(
        re.search(
            r"FromBase64String\('([A-Za-z0-9+/=]+)'\)",
            frame.input_bytes.decode("ascii"),
        ).group(1)  # type: ignore[union-attr]
    ).decode("utf-8")

    success_branch = "if($__tf_success){$__tf_rc=[uint32]0}"
    native_branch = "elseif(($__tf_native -ne 0)"
    assert outer_script.index(success_branch) < outer_script.index(native_branch)
    assert "+[Environment]::NewLine+'$__tf_success=$?'" in outer_script


def test_invalid_paths_environment_utf8_and_records_fail_closed() -> None:
    dialect = PowerShellDialect(token_factory=_token_factory("A" * 32))
    with pytest.raises(UnsupportedShell, match="absolute"):
        dialect.prepare_session(ShellStartRequest("pwsh.exe", r"C:\workspace", {}, None))
    with pytest.raises(UnsupportedShell, match="absolute"):
        dialect.prepare_session(ShellStartRequest(r"C:\pwsh.exe", r"\workspace", {}, None))
    with pytest.raises(DialectProtocolError, match="unique ignoring case"):
        dialect.prepare_session(
            ShellStartRequest(
                r"C:\pwsh.exe",
                r"C:\workspace",
                {"Path": "one", "PATH": "two"},
                None,
            )
        )
    with pytest.raises(DialectProtocolError, match="valid UTF-8"):
        dialect.prepare_session(
            ShellStartRequest(
                r"C:\pwsh.exe",
                r"C:\workspace",
                {},
                "Write-Output '\ud800'",
            )
        )

    malformed = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(malformed)
    with pytest.raises(DialectProtocolError, match="shell version"):
        malformed.feed(
            malformed._ready_prefix
            + b"0:0:not-base64:"
            + base64.b64encode(rb"C:\workspace")
            + b"\x1f"
        )

    oversized = cast(PowerShellProtocol, _plan().protocol)
    _bootstrap(oversized)
    with pytest.raises(DialectProtocolError, match="byte limit"):
        oversized.feed(oversized._ready_prefix + b"1" * 70_000 + b"\x1f")


def test_exit_code_over_uint32_fails_closed() -> None:
    protocol = cast(PowerShellProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("native.exe")
    with pytest.raises(DialectProtocolError, match="uint32"):
        protocol.feed(
            _command_bytes(
                protocol,
                frame.correlation_id,
                output=b"",
                exit_code=4_294_967_296,
            )
        )
