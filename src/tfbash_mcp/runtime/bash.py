"""Pure Bash dialect framing, adapted from ide4ai's PexpectTerminalEnv.

Upstream: https://github.com/A2C-SMCP/ide4ai/blob/20ece038e66e13885e77503e217b23766e60dc86/ide4ai/environment/terminal/pexpect_terminal_env.py
Original author metadata: JQQ <jqq1716@gmail.com>.  The upstream project
declares the MIT license in its pyproject.toml.  See NOTICE for provenance.

This adaptation retains the per-instance random prompt/exit-code delimiter and
base64+eval multi-line strategy, but replaces blocking pexpect control flow with
an incremental byte parser that owns no PTY or process object.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto

from tfbash_mcp.runtime.contracts import (
    CommandFrame,
    DialectEvent,
    DialectEventKind,
    DialectLaunch,
    DialectName,
    DialectProtocol,
    DialectSessionPlan,
    RuntimeName,
    ShellStartRequest,
    SpawnRequest,
)
from tfbash_mcp.runtime.errors import DialectProtocolError, UnsupportedShell

_RECORD_SEPARATOR = b"\x1e"
_UNIT_SEPARATOR = b"\x1f"
_INTEGER = re.compile(rb"-?[0-9]{1,10}\Z")


class _ParserState(Enum):
    STARTING = auto()
    READING_STARTUP = auto()
    READY = auto()
    WAITING_BEGIN = auto()
    CAPTURING = auto()
    READING_RESULT = auto()
    FINALIZING = auto()
    FINDING_RECOVERY = auto()
    READING_RECOVERY = auto()
    CLOSED = auto()


@dataclass(frozen=True, slots=True)
class _PendingCommand:
    correlation_id: str
    begin_marker: bytes
    result_prefix: bytes


@dataclass(frozen=True, slots=True)
class _PendingResult:
    correlation_id: str
    exit_code: int
    cwd: str


@dataclass(frozen=True, slots=True)
class _PendingRecovery:
    correlation_id: str
    record_prefix: bytes


class BashDialect:
    """Create isolated Bash launch/parser pairs without importing pexpect."""

    runtime_name = RuntimeName.POSIX_BASH
    dialect_name = DialectName.BASH
    default_executable = "/bin/bash"

    def __init__(
        self,
        *,
        token_factory: Callable[[], str] | None = None,
        max_control_bytes: int = 65_536,
    ) -> None:
        if max_control_bytes < 1024:
            raise ValueError("max_control_bytes must be at least 1024")
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._max_control_bytes = max_control_bytes

    def prepare_session(self, request: ShellStartRequest) -> DialectSessionPlan:
        _validate_start_request(request)
        session_token = _validate_token(self._token_factory())
        protocol = BashProtocol(
            session_token=session_token,
            token_factory=self._token_factory,
            max_control_bytes=self._max_control_bytes,
        )
        launch = DialectLaunch(
            spawn=SpawnRequest(
                executable=request.executable,
                arguments=("--noprofile", "--norc", "-i"),
                cwd=request.cwd,
                environment=request.environment,
            ),
            initial_input=protocol.initial_input(request.startup_command),
        )
        return DialectSessionPlan(launch=launch, protocol=protocol)


class BashProtocol(DialectProtocol):
    """Incrementally parse one Bash session's private framing protocol."""

    def __init__(
        self,
        *,
        session_token: str,
        token_factory: Callable[[], str],
        max_control_bytes: int,
    ) -> None:
        self._session_token = session_token
        self._token_factory = token_factory
        self._max_control_bytes = max_control_bytes
        self._prompt = f"__TFBASH_PROMPT_{session_token}__>".encode()
        self._ready_prefix = _RECORD_SEPARATOR + f"TFBASH_READY_{session_token}:".encode()
        self._base64_variable = f"__TFBASH_BASE64_{session_token}"
        self._buffer = bytearray()
        self._state = _ParserState.STARTING
        self._pending_command: _PendingCommand | None = None
        self._pending_result: _PendingResult | None = None
        self._pending_recovery: _PendingRecovery | None = None

    def initial_input(self, startup_command: str | None) -> bytes:
        decoder = f'"${self._base64_variable}"'
        startup = ":" if startup_command is None else _encoded_eval(startup_command, decoder)
        prompt = self._prompt.decode("ascii")
        ready = self._ready_prefix[1:].decode("ascii")
        script = (
            "unset PROMPT_COMMAND; "
            f"PS1='{prompt} '; "
            f"{self._base64_variable}=$(command -v base64 2>/dev/null || :); "
            "if [ -z \"${BASH_VERSION:-}\" ] || "
            f"[ -z \"${self._base64_variable}\" ]; then "
            "__tfbash_probe=1; __tfbash_rc=126; "
            "__tfbash_version_b64=''; __tfbash_cwd_b64=''; "
            "else __tfbash_probe=0; "
            f"readonly {self._base64_variable}; "
            f"{startup}; __tfbash_rc=$?; fi; "
            "unset PROMPT_COMMAND; "
            f"PS1='{prompt} '; "
            "if [ \"$__tfbash_probe\" -eq 0 ]; then "
            "__tfbash_cwd=$(pwd -P); "
            f"__tfbash_version_b64=$(printf '%s' \"${{BASH_VERSION:-}}\" | "
            f'"${self._base64_variable}"); '
            "__tfbash_version_b64=${__tfbash_version_b64//$'\\n'/}; "
            "__tfbash_version_b64=${__tfbash_version_b64//$'\\r'/}; "
            "__tfbash_cwd_b64=$(printf '%s' \"$__tfbash_cwd\" | "
            f'"${self._base64_variable}"); '
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\n'/}; "
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\r'/}; fi; "
            f"printf '\\036{ready}%d:%d:%s:%s\\037' "
            '"$__tfbash_probe" "$__tfbash_rc" '
            '"$__tfbash_version_b64" "$__tfbash_cwd_b64"'
        )
        return script.encode("utf-8") + b"\n"

    def wrap_command(self, command: str) -> CommandFrame:
        if self._state is not _ParserState.READY or self._pending_command is not None:
            raise DialectProtocolError("Bash protocol is not ready for another command")
        if not command or "\x00" in command:
            raise DialectProtocolError("command must be non-empty and NUL-free")
        command_token = _validate_token(self._token_factory())
        correlation_id = f"bash_{command_token}"
        begin_marker = (
            _RECORD_SEPARATOR
            + f"TFBASH_BEGIN_{command_token}".encode()
            + _UNIT_SEPARATOR
        )
        result_prefix = _RECORD_SEPARATOR + f"TFBASH_END_{command_token}:".encode()
        self._pending_command = _PendingCommand(
            correlation_id=correlation_id,
            begin_marker=begin_marker,
            result_prefix=result_prefix,
        )
        self._state = _ParserState.WAITING_BEGIN

        prompt = self._prompt.decode("ascii")
        begin = begin_marker[1:-1].decode("ascii")
        end = result_prefix[1:].decode("ascii")
        decoder = f'"${self._base64_variable}"'
        script = (
            f"printf '\\036{begin}\\037'; "
            f"{_encoded_eval(command, decoder)}; "
            "__tfbash_rc=$?; "
            "unset PROMPT_COMMAND; "
            f"PS1='{prompt} '; "
            "__tfbash_cwd=$(pwd -P); "
            "__tfbash_cwd_b64=$(printf '%s' \"$__tfbash_cwd\" | "
            f'"${self._base64_variable}"); '
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\n'/}; "
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\r'/}; "
            f"printf '\\036{end}%d:%s\\037' "
            '"$__tfbash_rc" "$__tfbash_cwd_b64"'
        )
        return CommandFrame(correlation_id=correlation_id, input_bytes=script.encode() + b"\n")

    def recovery_input(self) -> bytes:
        """Return a private probe that re-synchronizes after semantic interrupt."""

        pending = self._require_pending_command()
        if self._pending_recovery is not None:
            raise DialectProtocolError("Bash recovery is already in progress")
        recovery_token = _validate_token(self._token_factory())
        record_prefix = _RECORD_SEPARATOR + f"TFBASH_RECOVER_{recovery_token}:".encode()
        self._pending_recovery = _PendingRecovery(pending.correlation_id, record_prefix)
        self._pending_result = None
        self._state = _ParserState.FINDING_RECOVERY
        prompt = self._prompt.decode("ascii")
        prefix = record_prefix[1:].decode("ascii")
        script = (
            "unset PROMPT_COMMAND; "
            f"PS1='{prompt} '; "
            "__tfbash_cwd=$(pwd -P); "
            "__tfbash_cwd_b64=$(printf '%s' \"$__tfbash_cwd\" | "
            f'"${self._base64_variable}"); '
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\n'/}; "
            "__tfbash_cwd_b64=${__tfbash_cwd_b64//$'\\r'/}; "
            f"printf '\\036{prefix}%s\\037' \"$__tfbash_cwd_b64\""
        )
        return script.encode() + b"\n"

    def feed(self, data: bytes) -> tuple[DialectEvent, ...]:
        if self._state is _ParserState.CLOSED:
            raise DialectProtocolError("cannot feed a closed Bash protocol")
        if data:
            self._buffer.extend(data)
        events: list[DialectEvent] = []
        while self._advance(events):
            pass
        return tuple(events)

    def end_of_stream(self) -> tuple[DialectEvent, ...]:
        if self._state is _ParserState.CLOSED:
            return ()
        events: list[DialectEvent] = []
        if self._state in {
            _ParserState.CAPTURING,
            _ParserState.FINALIZING,
            _ParserState.FINDING_RECOVERY,
        } and self._buffer:
            events.append(DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer)))
        self._buffer.clear()
        self._pending_command = None
        self._pending_result = None
        self._pending_recovery = None
        self._state = _ParserState.CLOSED
        return tuple(events)

    def _advance(self, events: list[DialectEvent]) -> bool:
        if self._state is _ParserState.STARTING:
            return self._find_startup_record()
        if self._state is _ParserState.READING_STARTUP:
            return self._read_startup(events)
        if self._state is _ParserState.READY:
            self._trim_idle_buffer()
            return False
        if self._state is _ParserState.WAITING_BEGIN:
            pending = self._require_pending_command()
            if not self._discard_through(pending.begin_marker):
                return False
            self._state = _ParserState.CAPTURING
            return True
        if self._state is _ParserState.CAPTURING:
            return self._read_output(events)
        if self._state is _ParserState.READING_RESULT:
            return self._read_result()
        if self._state is _ParserState.FINALIZING:
            return self._read_prompt(events)
        if self._state is _ParserState.FINDING_RECOVERY:
            return self._find_recovery_record(events)
        if self._state is _ParserState.READING_RECOVERY:
            return self._read_recovery_record()
        return False

    def _find_startup_record(self) -> bool:
        marker_at = self._buffer.find(self._ready_prefix)
        if marker_at < 0:
            self._retain_marker_overlap(self._ready_prefix)
            return False
        del self._buffer[: marker_at + len(self._ready_prefix)]
        self._state = _ParserState.READING_STARTUP
        return True

    def _read_startup(self, events: list[DialectEvent]) -> bool:
        record = self._take_record()
        if record is None:
            return False
        fields = record.split(b":", 3)
        if (
            len(fields) != 4
            or not _INTEGER.fullmatch(fields[0])
            or not _INTEGER.fullmatch(fields[1])
        ):
            raise DialectProtocolError("invalid Bash startup record")
        probe, startup_rc = int(fields[0]), int(fields[1])
        if probe != 0:
            raise UnsupportedShell("executable did not report a compatible Bash runtime")
        version = _decode_text_field(fields[2], "shell version")
        cwd = _decode_cwd(fields[3])
        if not version:
            raise UnsupportedShell("Bash runtime reported an empty version")
        if startup_rc != 0:
            raise DialectProtocolError(f"startup command failed with exit code {startup_rc}")
        self._pending_result = _PendingResult("", 0, cwd)
        self._startup_version = version
        self._state = _ParserState.FINALIZING
        return True

    def _read_output(self, events: list[DialectEvent]) -> bool:
        pending = self._require_pending_command()
        marker_at = self._buffer.find(pending.result_prefix)
        if marker_at >= 0:
            if marker_at:
                events.append(
                    DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:marker_at]))
                )
            del self._buffer[: marker_at + len(pending.result_prefix)]
            self._state = _ParserState.READING_RESULT
            return True
        safe_bytes = len(self._buffer) - len(pending.result_prefix) + 1
        if safe_bytes > 0:
            events.append(
                DialectEvent(
                    DialectEventKind.OUTPUT,
                    data=bytes(self._buffer[:safe_bytes]),
                )
            )
            del self._buffer[:safe_bytes]
        return False

    def _read_result(self) -> bool:
        record = self._take_record()
        if record is None:
            return False
        fields = record.split(b":", 1)
        if len(fields) != 2 or not _INTEGER.fullmatch(fields[0]):
            raise DialectProtocolError("invalid Bash command result record")
        pending = self._require_pending_command()
        self._pending_result = _PendingResult(
            correlation_id=pending.correlation_id,
            exit_code=int(fields[0]),
            cwd=_decode_cwd(fields[1]),
        )
        self._state = _ParserState.FINALIZING
        return True

    def _read_prompt(self, events: list[DialectEvent]) -> bool:
        prompt_at = self._buffer.find(self._prompt)
        if prompt_at < 0:
            safe_bytes = len(self._buffer) - len(self._prompt) + 1
            if safe_bytes > 0:
                if self._pending_command is not None:
                    events.append(
                        DialectEvent(
                            DialectEventKind.OUTPUT,
                            data=bytes(self._buffer[:safe_bytes]),
                        )
                    )
                del self._buffer[:safe_bytes]
            return False
        if prompt_at and self._pending_command is not None:
            events.append(
                DialectEvent(
                    DialectEventKind.OUTPUT,
                    data=bytes(self._buffer[:prompt_at]),
                )
            )
        del self._buffer[: prompt_at + len(self._prompt)]
        if self._buffer.startswith(b" "):
            del self._buffer[:1]
        result = self._require_pending_result()
        if self._pending_command is None:
            events.append(
                DialectEvent(
                    DialectEventKind.READY,
                    cwd=result.cwd,
                    shell_version=self._startup_version,
                )
            )
        elif self._pending_recovery is not None:
            events.append(
                DialectEvent(
                    DialectEventKind.RECOVERED,
                    correlation_id=self._pending_recovery.correlation_id,
                    cwd=result.cwd,
                )
            )
        else:
            events.append(
                DialectEvent(
                    DialectEventKind.COMMAND_COMPLETE,
                    correlation_id=result.correlation_id,
                    exit_code=result.exit_code,
                    cwd=result.cwd,
                )
            )
        self._pending_command = None
        self._pending_result = None
        self._pending_recovery = None
        self._state = _ParserState.READY
        return True

    def _find_recovery_record(self, events: list[DialectEvent]) -> bool:
        recovery = self._require_pending_recovery()
        marker_at = self._buffer.find(recovery.record_prefix)
        if marker_at < 0:
            safe_bytes = len(self._buffer) - self._max_control_bytes
            if safe_bytes > 0:
                visible = bytes(self._buffer[:safe_bytes])
                if visible:
                    events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
                del self._buffer[:safe_bytes]
            return False
        visible = self._strip_one_terminal_prompt(bytes(self._buffer[:marker_at]))
        if visible:
            events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
        del self._buffer[: marker_at + len(recovery.record_prefix)]
        self._state = _ParserState.READING_RECOVERY
        return True

    def _read_recovery_record(self) -> bool:
        record = self._take_record()
        if record is None:
            return False
        recovery = self._require_pending_recovery()
        self._pending_result = _PendingResult(
            correlation_id=recovery.correlation_id,
            exit_code=0,
            cwd=_decode_cwd(record),
        )
        self._state = _ParserState.FINALIZING
        return True

    def _strip_one_terminal_prompt(self, data: bytes) -> bytes:
        """Remove only the actual prompt immediately before a recovery frame."""

        prompt_with_space = self._prompt + b" "
        if data.endswith(prompt_with_space):
            return data[: -len(prompt_with_space)]
        if data.endswith(self._prompt):
            return data[: -len(self._prompt)]
        return data

    def _take_record(self) -> bytes | None:
        end_at = self._buffer.find(_UNIT_SEPARATOR)
        if end_at < 0:
            if len(self._buffer) > self._max_control_bytes:
                raise DialectProtocolError("Bash control record exceeded its byte limit")
            return None
        if end_at > self._max_control_bytes:
            raise DialectProtocolError("Bash control record exceeded its byte limit")
        record = bytes(self._buffer[:end_at])
        del self._buffer[: end_at + 1]
        return record

    def _discard_through(self, marker: bytes) -> bool:
        marker_at = self._buffer.find(marker)
        if marker_at < 0:
            self._retain_marker_overlap(marker)
            return False
        del self._buffer[: marker_at + len(marker)]
        return True

    def _retain_marker_overlap(self, marker: bytes) -> None:
        keep = min(len(self._buffer), len(marker) - 1)
        if len(self._buffer) > keep:
            del self._buffer[: len(self._buffer) - keep]

    def _trim_idle_buffer(self) -> None:
        if len(self._buffer) > len(self._prompt):
            del self._buffer[: len(self._buffer) - len(self._prompt)]

    def _require_pending_command(self) -> _PendingCommand:
        if self._pending_command is None:
            raise DialectProtocolError("missing Bash command framing state")
        return self._pending_command

    def _require_pending_result(self) -> _PendingResult:
        if self._pending_result is None:
            raise DialectProtocolError("missing Bash result framing state")
        return self._pending_result

    def _require_pending_recovery(self) -> _PendingRecovery:
        if self._pending_recovery is None:
            raise DialectProtocolError("missing Bash recovery framing state")
        return self._pending_recovery


def _encoded_eval(command: str, decoder: str) -> str:
    blob = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return f'eval "$(printf \'%s\' \'{blob}\' | {decoder} --decode)"'


def _decode_text_field(field: bytes, label: str) -> str:
    try:
        decoded = base64.b64decode(field, validate=True)
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise DialectProtocolError(f"invalid {label} in Bash control record") from error


def _decode_cwd(field: bytes) -> str:
    cwd = _decode_text_field(field, "cwd")
    if not posixpath.isabs(cwd) or "\x00" in cwd:
        raise DialectProtocolError("Bash reported an invalid cwd")
    return cwd


def _validate_token(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", token):
        raise ValueError("dialect tokens must contain 16-64 ASCII alphanumeric characters")
    return token


def _validate_start_request(request: ShellStartRequest) -> None:
    for label, value in (("executable", request.executable), ("cwd", request.cwd)):
        if not value or "\x00" in value or not posixpath.isabs(value):
            raise UnsupportedShell(f"Bash {label} must be a NUL-free POSIX absolute path")
    if request.startup_command is not None and (
        not request.startup_command or "\x00" in request.startup_command
    ):
        raise DialectProtocolError("startup command must be non-empty and NUL-free")
    _validate_environment(request.environment)


def _validate_environment(environment: Mapping[str, str]) -> None:
    for key, value in environment.items():
        if not key or "\x00" in key or "\x00" in value:
            raise DialectProtocolError("environment keys and values must be NUL-free")
