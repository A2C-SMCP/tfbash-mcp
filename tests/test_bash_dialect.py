from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pexpect  # type: ignore[import-untyped]
import pytest

from tfbash_mcp.runtime import (
    BashDialect,
    BashProtocol,
    DialectEvent,
    DialectEventKind,
    DialectProtocolError,
    DialectSessionPlan,
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
    dialect = BashDialect(token_factory=_token_factory(*tokens))
    return dialect.prepare_session(
        ShellStartRequest(
            executable="/bin/bash",
            cwd="/workspace",
            environment={"PROJECT": "test"},
            startup_command=startup_command,
        )
    )


def _startup_bytes(
    protocol: BashProtocol,
    *,
    probe: int = 0,
    exit_code: int = 0,
    version: str = "5.2.0",
    cwd: str = "/workspace",
) -> bytes:
    fields = b":".join(
        (
            str(probe).encode(),
            str(exit_code).encode(),
            base64.b64encode(version.encode()),
            base64.b64encode(cwd.encode()),
        )
    )
    return (
        b"ignored initial prompt"
        + protocol._ready_prefix
        + fields
        + b"\x1f"
        + protocol._prompt
        + b" "
    )


def _ready(
    protocol: BashProtocol,
    *,
    probe: int = 0,
    exit_code: int = 0,
    version: str = "5.2.0",
    cwd: str = "/workspace",
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


def _bootstrap(protocol: BashProtocol) -> None:
    assert protocol.feed(protocol._prompt + b" ") == (
        DialectEvent(DialectEventKind.BOOTSTRAP_REQUIRED),
    )


def _command_bytes(
    protocol: BashProtocol,
    correlation_id: str,
    *,
    output: bytes,
    exit_code: int = 0,
    cwd: str = "/workspace",
    include_prompt: bool = True,
) -> bytes:
    token = correlation_id.removeprefix("bash_")
    begin = b"\x1eTFBASH_BEGIN_" + token.encode() + b"\x1f"
    result = (
        b"\x1eTFBASH_END_"
        + token.encode()
        + b":"
        + str(exit_code).encode()
        + b":"
        + base64.b64encode(cwd.encode())
        + b"\x1f"
    )
    prompt = protocol._prompt + b" " if include_prompt else b""
    return b"echoed wrapper" + begin + output + result + prompt


def _feed_in_chunks(
    protocol: BashProtocol,
    payload: bytes,
    sizes: tuple[int, ...],
) -> list[DialectEvent]:
    events: list[DialectEvent] = []
    cursor = 0
    for size in sizes:
        events.extend(protocol.feed(payload[cursor : cursor + size]))
        cursor += size
    events.extend(protocol.feed(payload[cursor:]))
    return events


def test_prepare_session_builds_bash_launch_without_transport() -> None:
    plan = _plan(startup_command="export ACTIVE_ENV=yes")

    assert plan.launch.spawn.arguments == ("--noprofile", "--norc", "-i")
    assert plan.launch.spawn.executable == "/bin/bash"
    assert b"ACTIVE_ENV=yes" not in plan.launch.initial_input
    assert b" --decode" in plan.launch.initial_input
    assert isinstance(plan.protocol, BashProtocol)


def test_each_session_has_private_prompt_and_parser_state() -> None:
    dialect = BashDialect(token_factory=_token_factory("A" * 32, "B" * 32))
    request = ShellStartRequest("/bin/bash", "/workspace", {}, None)

    first = dialect.prepare_session(request)
    second = dialect.prepare_session(request)

    assert cast(BashProtocol, first.protocol)._prompt != cast(BashProtocol, second.protocol)._prompt
    assert first.launch.initial_input != second.launch.initial_input


def test_startup_record_is_incremental_and_reports_version_and_cwd() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _bootstrap(protocol)
    payload = _startup_bytes(protocol, version="5.2.37", cwd="/work/项目")

    events = _feed_in_chunks(protocol, payload, tuple(1 for _ in payload[:-1]))

    assert events == [
        DialectEvent(
            DialectEventKind.READY,
            cwd="/work/项目",
            shell_version="5.2.37",
        )
    ]


def test_startup_fails_closed_for_non_bash_and_failed_command() -> None:
    unsupported = cast(BashProtocol, _plan().protocol)
    _bootstrap(unsupported)
    with pytest.raises(UnsupportedShell, match="compatible Bash"):
        unsupported.feed(_startup_bytes(unsupported, probe=1, exit_code=126, version=""))

    failed = cast(BashProtocol, _plan().protocol)
    _bootstrap(failed)
    with pytest.raises(DialectProtocolError, match="exit code 17"):
        failed.feed(_startup_bytes(failed, exit_code=17))


def test_command_parser_handles_every_control_chunk_split() -> None:
    template = cast(BashProtocol, _plan().protocol)
    _ready(template)
    template_frame = template.wrap_command("printf '你好🙂'")
    template_output = (
        b"prefix\x1b[31mred\x1b[0m\x1b]133;A\x07"
        + template._prompt
        + "你好🙂".encode()
    )
    template_payload = _command_bytes(
        template,
        template_frame.correlation_id,
        output=template_output,
        exit_code=23,
        cwd="/workspace/子目录",
    )

    for split in range(len(template_payload) + 1):
        protocol = cast(BashProtocol, _plan().protocol)
        _ready(protocol)
        frame = protocol.wrap_command("printf '你好🙂'")
        output = (
            b"prefix\x1b[31mred\x1b[0m\x1b]133;A\x07"
            + protocol._prompt
            + "你好🙂".encode()
        )
        payload = _command_bytes(
            protocol,
            frame.correlation_id,
            output=output,
            exit_code=23,
            cwd="/workspace/子目录",
        )
        events = protocol.feed(payload[:split]) + protocol.feed(payload[split:])
        observed_output = b"".join(
            event.data for event in events if event.kind is DialectEventKind.OUTPUT
        )
        completions = [
            event for event in events if event.kind is DialectEventKind.COMMAND_COMPLETE
        ]

        assert observed_output == output, f"output mismatch at split {split}"
        assert completions == [
            DialectEvent(
                DialectEventKind.COMMAND_COMPLETE,
                correlation_id=frame.correlation_id,
                exit_code=23,
                cwd="/workspace/子目录",
            )
        ], f"completion mismatch at split {split}"


def test_completion_waits_for_real_prompt_finalizing_gate() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("true")
    before_prompt = _command_bytes(
        protocol,
        frame.correlation_id,
        output=b"done\n",
        include_prompt=False,
    )

    events = protocol.feed(before_prompt)
    assert all(event.kind is not DialectEventKind.COMMAND_COMPLETE for event in events)
    with pytest.raises(DialectProtocolError, match="not ready"):
        protocol.wrap_command("echo too-early")

    completed = protocol.feed(protocol._prompt + b" ")
    assert completed[-1].kind is DialectEventKind.COMMAND_COMPLETE


def test_large_output_streams_without_growing_control_buffer() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("generate-output")
    token = frame.correlation_id.removeprefix("bash_")
    protocol.feed(b"\x1eTFBASH_BEGIN_" + token.encode() + b"\x1f")

    events = protocol.feed(b"x" * 1_000_000)

    assert sum(len(event.data) for event in events) > 999_900
    assert len(protocol._buffer) < 100


def test_eof_flushes_captured_output_once() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("exec false")
    token = frame.correlation_id.removeprefix("bash_")
    protocol.feed(b"\x1eTFBASH_BEGIN_" + token.encode() + b"\x1fpartial")

    assert protocol.end_of_stream() == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"partial"),
    )
    assert protocol.end_of_stream() == ()
    with pytest.raises(DialectProtocolError, match="closed"):
        protocol.feed(b"late")


def test_finalizing_eof_flushes_all_unconfirmed_prompt_tail() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("true")
    tail = b"late-background-output" * 5
    payload = _command_bytes(
        protocol,
        frame.correlation_id,
        output=b"foreground\n",
        include_prompt=False,
    ) + tail

    before_eof = protocol.feed(payload)
    at_eof = protocol.end_of_stream()
    output = b"".join(
        event.data
        for event in before_eof + at_eof
        if event.kind is DialectEventKind.OUTPUT
    )

    assert output == b"foreground\n" + tail
    assert all(
        event.kind is not DialectEventKind.COMMAND_COMPLETE
        for event in before_eof + at_eof
    )


def test_recovery_probe_resynchronizes_pending_command_and_preserves_output() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    frame = protocol.wrap_command("sleep 30")
    token = frame.correlation_id.removeprefix("bash_")
    protocol.feed(b"\x1eTFBASH_BEGIN_" + token.encode() + b"\x1fpartial")

    recovery = protocol.recovery_input()
    assert b"TFBASH_RECOVER_" in recovery
    recovered_bytes = (
        protocol._prompt
        + b" "
        + b"\x1eTFBASH_RECOVER_"
        + b"C" * 32
        + b":"
        + base64.b64encode(b"/workspace")
        + b"\x1f"
        + protocol._prompt
        + b" "
    )
    events = protocol.feed(recovered_bytes)

    assert b"".join(
        event.data for event in events if event.kind is DialectEventKind.OUTPUT
    ) == b"partial"
    assert events[-1] == DialectEvent(
        DialectEventKind.RECOVERED,
        correlation_id=frame.correlation_id,
        cwd="/workspace",
    )
    assert protocol.wrap_command("echo healthy").correlation_id.startswith("bash_")


def test_recovery_preserves_exact_prompt_and_result_like_user_bytes_at_every_split() -> None:
    template = cast(BashProtocol, _plan().protocol)
    _ready(template)
    frame = template.wrap_command("sleep 30")
    token = frame.correlation_id.removeprefix("bash_")
    template.feed(b"\x1eTFBASH_BEGIN_" + token.encode() + b"\x1f")
    template.recovery_input()
    result_like = (
        b"\x1eTFBASH_END_"
        + token.encode()
        + b":0:"
        + base64.b64encode(b"/forged")
        + b"\x1f"
    )
    user_output = b"user:" + template._prompt + b":tail" + result_like
    suffix = (
        template._prompt
        + b" "
        + b"\x1eTFBASH_RECOVER_"
        + b"C" * 32
        + b":"
        + base64.b64encode(b"/workspace")
        + b"\x1f"
        + template._prompt
        + b" "
    )
    payload = user_output + suffix

    for split in range(len(payload) + 1):
        protocol = cast(BashProtocol, _plan().protocol)
        _ready(protocol)
        current = protocol.wrap_command("sleep 30")
        current_token = current.correlation_id.removeprefix("bash_")
        protocol.feed(b"\x1eTFBASH_BEGIN_" + current_token.encode() + b"\x1f")
        protocol.recovery_input()

        events = protocol.feed(payload[:split]) + protocol.feed(payload[split:])
        output = b"".join(
            event.data for event in events if event.kind is DialectEventKind.OUTPUT
        )
        recovered = [event for event in events if event.kind is DialectEventKind.RECOVERED]

        assert output == user_output, f"output mismatch at recovery split {split}"
        assert recovered == [
            DialectEvent(
                DialectEventKind.RECOVERED,
                correlation_id=current.correlation_id,
                cwd="/workspace",
            )
        ], f"recovery mismatch at split {split}"


def test_command_wrapper_preserves_multiline_and_heredoc_bytes() -> None:
    protocol = cast(BashProtocol, _plan().protocol)
    _ready(protocol)
    command = "export KEEP=持久\npython3 << 'PYEOF'\nprint('你好🙂')\nPYEOF"

    frame = protocol.wrap_command(command)
    match = re.search(
        rb"printf '%s' '([A-Za-z0-9+/=]+)' \| "
        rb'"\$__TFBASH_BASE64_[A-Za-z0-9]+" --decode',
        frame.input_bytes,
    )

    assert match is not None
    assert base64.b64decode(match.group(1)).decode() == command
    assert frame.input_bytes.count(b"\n") == 1


def test_invalid_requests_and_control_records_fail_closed() -> None:
    dialect = BashDialect(token_factory=_token_factory("A" * 32))
    with pytest.raises(UnsupportedShell, match="absolute"):
        dialect.prepare_session(ShellStartRequest("bash", "/workspace", {}, None))
    with pytest.raises(DialectProtocolError, match="NUL-free"):
        dialect.prepare_session(
            ShellStartRequest("/bin/bash", "/workspace", {}, "echo\x00bad")
        )

    protocol = cast(BashProtocol, _plan().protocol)
    _bootstrap(protocol)
    malformed = protocol._ready_prefix + b"0:0:not-base64:bad\x1f" + protocol._prompt
    with pytest.raises(DialectProtocolError, match="shell version"):
        protocol.feed(malformed)

    oversized = cast(BashProtocol, _plan().protocol)
    _bootstrap(oversized)
    with pytest.raises(DialectProtocolError, match="byte limit"):
        oversized.feed(oversized._ready_prefix + b"1" * 70_000 + b"\x1f")

    oversized_integer = cast(BashProtocol, _plan().protocol)
    _bootstrap(oversized_integer)
    invalid_integer = (
        oversized_integer._ready_prefix
        + b"0:"
        + b"9" * 11
        + b":"
        + base64.b64encode(b"5.2")
        + b":"
        + base64.b64encode(b"/workspace")
        + b"\x1f"
    )
    with pytest.raises(DialectProtocolError, match="startup record"):
        oversized_integer.feed(invalid_integer)


def _read_until(
    child: Any,
    protocol: BashProtocol,
    expected: DialectEventKind,
    *,
    timeout_seconds: float = 8,
) -> tuple[list[DialectEvent], DialectEvent]:
    deadline = time.monotonic() + timeout_seconds
    observed: list[DialectEvent] = []
    while time.monotonic() < deadline:
        try:
            chunk = cast(bytes, child.read_nonblocking(size=4096, timeout=0.2))
        except pexpect.TIMEOUT:
            continue
        observed.extend(protocol.feed(chunk))
        for event in observed:
            if event.kind is expected:
                return observed, event
    raise AssertionError(f"did not observe {expected.value}: {observed!r}")


def _wait_for_state(child: Any, protocol: BashProtocol, state_name: str) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            chunk = cast(bytes, child.read_nonblocking(size=4096, timeout=0.2))
        except pexpect.TIMEOUT:
            continue
        protocol.feed(chunk)
        if protocol._state.name == state_name:
            return
    raise AssertionError(f"protocol did not enter {state_name}")


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="Bash is unavailable")
def test_real_bash_keeps_startup_cwd_env_multiline_and_exit_status(tmp_path: Path) -> None:
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    dialect = BashDialect(token_factory=_token_factory("A" * 32, "B" * 32, "C" * 32))
    plan = dialect.prepare_session(
        ShellStartRequest(
            "/bin/bash",
            str(tmp_path),
            dict(os.environ) | {"FROM_HOST": "host-value"},
            "export FROM_STARTUP=startup-value",
        )
    )
    protocol = cast(BashProtocol, plan.protocol)
    child = pexpect.spawn(
        plan.launch.spawn.executable,
        list(plan.launch.spawn.arguments),
        cwd=plan.launch.spawn.cwd,
        env=dict(plan.launch.spawn.environment),
        encoding=None,
        echo=False,
        timeout=8,
    )
    try:
        _read_until(child, protocol, DialectEventKind.BOOTSTRAP_REQUIRED)
        child.send(plan.launch.initial_input)
        _, ready = _read_until(child, protocol, DialectEventKind.READY)
        assert ready.cwd == str(tmp_path)
        assert ready.shell_version

        first = protocol.wrap_command(
            f"cd '{subdirectory}'; export KEEP='持久'; printf '%s' \"$FROM_HOST/$FROM_STARTUP\""
        )
        child.send(first.input_bytes)
        events, completed = _read_until(child, protocol, DialectEventKind.COMMAND_COMPLETE)
        assert completed.exit_code == 0
        assert completed.cwd == str(subdirectory)
        assert b"host-value/startup-value" in b"".join(event.data for event in events)

        second = protocol.wrap_command(
            "python3 << 'PYEOF'\nprint('你好🙂')\nPYEOF\nprintf \'/%s\' \"$KEEP\"\nfalse"
        )
        child.send(second.input_bytes)
        events, completed = _read_until(child, protocol, DialectEventKind.COMMAND_COMPLETE)
        output = b"".join(event.data for event in events if event.kind is DialectEventKind.OUTPUT)
        assert "你好🙂".encode() in output
        assert "/持久".encode() in output
        assert completed.exit_code != 0
        assert completed.cwd == str(subdirectory)
    finally:
        child.close(force=True)


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="Bash is unavailable")
def test_real_bash_ctrl_c_recovery_returns_prompt_and_allows_next_command(
    tmp_path: Path,
) -> None:
    dialect = BashDialect(
        token_factory=_token_factory("A" * 32, "B" * 32, "C" * 32, "D" * 32)
    )
    plan = dialect.prepare_session(
        ShellStartRequest("/bin/bash", str(tmp_path), dict(os.environ), None)
    )
    protocol = cast(BashProtocol, plan.protocol)
    child = pexpect.spawn(
        "/bin/bash",
        list(plan.launch.spawn.arguments),
        cwd=str(tmp_path),
        env=dict(plan.launch.spawn.environment),
        encoding=None,
        echo=False,
        timeout=8,
    )
    try:
        _read_until(child, protocol, DialectEventKind.BOOTSTRAP_REQUIRED)
        child.send(plan.launch.initial_input)
        _read_until(child, protocol, DialectEventKind.READY)
        blocked = protocol.wrap_command("sleep 30")
        child.send(blocked.input_bytes)
        _wait_for_state(child, protocol, "CAPTURING")

        child.sendintr()
        child.send(protocol.recovery_input())
        _, recovered = _read_until(child, protocol, DialectEventKind.RECOVERED)

        assert recovered.correlation_id == blocked.correlation_id
        assert recovered.cwd == str(tmp_path)
        followup = protocol.wrap_command("printf recovered")
        child.send(followup.input_bytes)
        events, completed = _read_until(child, protocol, DialectEventKind.COMMAND_COMPLETE)
        assert completed.exit_code == 0
        assert b"recovered" in b"".join(event.data for event in events)
    finally:
        child.close(force=True)


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="Bash is unavailable")
def test_real_bash_without_base64_reports_unsupported_shell(tmp_path: Path) -> None:
    plan = BashDialect(token_factory=_token_factory("A" * 32)).prepare_session(
        ShellStartRequest("/bin/bash", str(tmp_path), {"PATH": ""}, None)
    )
    protocol = cast(BashProtocol, plan.protocol)
    child = pexpect.spawn(
        "/bin/bash",
        list(plan.launch.spawn.arguments),
        cwd=str(tmp_path),
        env=dict(plan.launch.spawn.environment),
        encoding=None,
        echo=False,
        timeout=8,
    )
    try:
        _read_until(child, protocol, DialectEventKind.BOOTSTRAP_REQUIRED)
        child.send(plan.launch.initial_input)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                chunk = cast(bytes, child.read_nonblocking(size=4096, timeout=0.2))
            except pexpect.TIMEOUT:
                continue
            try:
                protocol.feed(chunk)
            except UnsupportedShell as error:
                assert "compatible Bash" in str(error)
                break
        else:
            raise AssertionError("Bash capability failure record was not observed")
    finally:
        child.close(force=True)
