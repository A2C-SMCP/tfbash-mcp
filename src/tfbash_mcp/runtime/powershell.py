"""Pure PowerShell Desktop/Core dialect framing for native terminal backends.

The dialect owns only launch construction and an incremental byte parser.  It
does not import pywinpty or any Win32 API and never owns a ConPTY handle or a
process identity.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import PurePosixPath, PureWindowsPath

from tfbash_mcp.runtime.contracts import (
    CancellationSignal,
    CommandFrame,
    DialectEvent,
    DialectEventKind,
    DialectLaunch,
    DialectName,
    DialectProtocol,
    DialectSessionPlan,
    ShellStartRequest,
    SpawnRequest,
)
from tfbash_mcp.runtime.errors import DialectProtocolError, UnsupportedShell

_RECORD_SEPARATOR = b"\x1e"
_UNIT_SEPARATOR = b"\x1f"
_UNSIGNED_INTEGER = re.compile(rb"[0-9]{1,10}\Z")
_VT_SEQUENCE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_MAX_WINDOWS_EXIT_CODE = 4_294_967_295
_CONTROL_ECHO_PREFIX_BYTES = 256
_PRIVATE_FRAGMENT_BYTES = 8
_POSIX_INPUT_CHUNK_CHARACTERS = 512


class _ParserState(Enum):
    AWAITING_LAUNCH = auto()
    AWAITING_INITIAL_PROMPT = auto()
    STARTING = auto()
    READING_STARTUP = auto()
    READY = auto()
    WAITING_BEGIN = auto()
    CAPTURING = auto()
    READING_RESULT = auto()
    FINALIZING = auto()
    AWAITING_FINALIZATION = auto()
    FINDING_FINALIZATION = auto()
    FINALIZATION_PROMPT = auto()
    FINDING_RECOVERY_BEGIN = auto()
    FINDING_RECOVERY_RESULT = auto()
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
    begin_marker: bytes
    result_prefix: bytes
    input_echo: bytes


@dataclass(frozen=True, slots=True)
class _PendingFinalization:
    correlation_id: str
    marker: bytes
    input_echo: bytes


class PowerShellDialect:
    """Create isolated PowerShell launch/parser pairs without a ConPTY import."""

    dialect_name = DialectName.PWSH
    default_executable = r"C:\Program Files\PowerShell\7\pwsh.exe"

    def __init__(
        self,
        *,
        token_factory: Callable[[], str] | None = None,
        max_control_bytes: int = 65_536,
        default_executable: str = r"C:\Program Files\PowerShell\7\pwsh.exe",
        windows_paths: bool = True,
    ) -> None:
        if max_control_bytes < 1024:
            raise ValueError("max_control_bytes must be at least 1024")
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._max_control_bytes = max_control_bytes
        self.default_executable = default_executable
        self._windows_paths = windows_paths

    def prepare_session(
        self,
        request: ShellStartRequest,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> DialectSessionPlan:
        if deadline_ms is not None and deadline_ms <= 0:
            raise DialectProtocolError("PowerShell session preparation deadline expired")
        if cancel_signal is not None and cancel_signal.is_set():
            raise DialectProtocolError("PowerShell session preparation was cancelled")
        _validate_start_request(request, windows=self._windows_paths)
        session_token = _validate_token(self._token_factory())
        protocol = PowerShellProtocol(
            session_token=session_token,
            token_factory=self._token_factory,
            max_control_bytes=self._max_control_bytes,
            windows_paths=self._windows_paths,
        )
        if cancel_signal is not None and cancel_signal.is_set():
            raise DialectProtocolError("PowerShell session preparation was cancelled")
        launch_script = (
            protocol._prompt_definition()
            + ";"
            + _write_static_record(protocol._launch_marker[1:-1].decode("ascii"))
        )
        if not self._windows_paths:
            prompt = _powershell_literal(protocol._prompt.decode("ascii"))
            chunk_prefix = _powershell_literal(f"__TFPWSH_CHUNK_{session_token}:")
            launch_script += (
                f";$__tf_chunk_prefix={chunk_prefix};"
                "$__tf_payload='';$__tf_payload_token='';"
                "& /bin/stty -echo -icanon min 1 time 0;"
                "if($LASTEXITCODE -ne 0){throw 'failed to configure the POSIX terminal'};"
                f"[Console]::Out.Write({prompt});"
                "$__tf_input=[Console]::In.ReadLine();"
                "while($null -ne $__tf_input){"
                "$__tf_prompt_required=$true;"
                "if($__tf_input.StartsWith($__tf_chunk_prefix,[StringComparison]::Ordinal)){"
                "$__tf_chunk=$__tf_input.Substring($__tf_chunk_prefix.Length);"
                "$__tf_separator=$__tf_chunk.IndexOf(':',2);"
                "if($__tf_separator -gt 2){"
                "$__tf_chunk_op=$__tf_chunk.Substring(0,1);"
                "$__tf_chunk_token=$__tf_chunk.Substring(2,$__tf_separator-2);"
                "$__tf_chunk_data=$__tf_chunk.Substring($__tf_separator+1);"
                "if(($__tf_chunk_op -ceq 'S') -and "
                "($__tf_chunk_token -cmatch '^[A-Za-z0-9]{16,64}$')){"
                "$__tf_payload_token=$__tf_chunk_token;$__tf_payload=$__tf_chunk_data;"
                "$__tf_prompt_required=$false"
                "}elseif(($__tf_chunk_op -ceq 'A') -and "
                "($__tf_chunk_token -ceq $__tf_payload_token)){"
                "$__tf_payload+=$__tf_chunk_data;$__tf_prompt_required=$false"
                "}elseif(($__tf_chunk_op -ceq 'X') -and "
                "($__tf_chunk_token -ceq $__tf_payload_token)){"
                "$__tf_payload_to_run=$__tf_payload;"
                "$__tf_payload='';$__tf_payload_token='';"
                "try{. ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString("
                "[Convert]::FromBase64String($__tf_payload_to_run))))}"
                "catch{[Console]::Error.WriteLine([string]$_)}"
                "finally{$__tf_payload_to_run=''}"
                "}else{$__tf_payload='';$__tf_payload_token=''}"
                "}else{$__tf_payload='';$__tf_payload_token=''}"
                "}else{"
                "$__tf_payload='';$__tf_payload_token='';"
                "try{. ([ScriptBlock]::Create($__tf_input))}"
                "catch{[Console]::Error.WriteLine([string]$_)}"
                "};"
                f"if($__tf_prompt_required){{[Console]::Out.Write({prompt})}};"
                "$__tf_chunk='';$__tf_chunk_data='';$__tf_chunk_op='';"
                "$__tf_chunk_token='';$__tf_separator=-1;$__tf_input='';"
                "$__tf_input=[Console]::In.ReadLine()}"
            )
        launch = DialectLaunch(
            spawn=SpawnRequest(
                executable=request.executable,
                arguments=(
                    "-NoLogo",
                    "-NoProfile",
                    "-NoExit",
                    "-NonInteractive",
                    "-EncodedCommand",
                    _encoded_command(launch_script),
                ),
                cwd=request.cwd,
                environment=request.environment,
            ),
            initial_input=protocol.initial_input(request.startup_command),
        )
        return DialectSessionPlan(launch=launch, protocol=protocol)


class PowerShellProtocol(DialectProtocol):
    """Incrementally parse one PowerShell session's private framing protocol."""

    def __init__(
        self,
        *,
        session_token: str,
        token_factory: Callable[[], str],
        max_control_bytes: int,
        windows_paths: bool = True,
    ) -> None:
        self._session_token = session_token
        self._token_factory = token_factory
        self._max_control_bytes = max_control_bytes
        self._windows_paths = windows_paths
        self._line_ending = b"\r\n" if windows_paths else b"\n"
        self._prompt = f"__TFPWSH_PROMPT_{session_token}__> ".encode()
        self._launch_marker = (
            _RECORD_SEPARATOR + f"TFPWSH_LAUNCH_{session_token}".encode() + _UNIT_SEPARATOR
        )
        self._ready_prefix = _RECORD_SEPARATOR + f"TFPWSH_READY_{session_token}:".encode()
        self._control_function = f"__TFPWSH_CONTROL_{session_token}"
        self._buffer = bytearray()
        self._state = _ParserState.AWAITING_LAUNCH
        self._pending_command: _PendingCommand | None = None
        self._pending_result: _PendingResult | None = None
        self._pending_recovery: _PendingRecovery | None = None
        self._pending_finalization: _PendingFinalization | None = None
        self._startup_version = ""
        self._control_echo_seen = False
        self._control_echo_suffix_pending = False
        self._prompt_separator_pending = False

    def initial_input(self, startup_command: str | None) -> bytes:
        startup_block = ""
        if startup_command is not None:
            startup_block = (
                "$global:LASTEXITCODE=0;$__tf_success=$true;"
                "try{" + _dot_source_utf8(startup_command, "$__tf_success") + "}"
                "catch{$__tf_success=$false};"
                + _exit_code_assignment("$__tf_rc", "$__tf_success", windows=self._windows_paths)
            )
        script = (
            "$__tf_probe=0;$__tf_rc=[uint32]0;"
            "$__tf_version=[string]$PSVersionTable.PSVersion;"
            "if(($PSVersionTable.PSEdition -cne 'Core') -and "
            "($PSVersionTable.PSEdition -cne 'Desktop')){$__tf_probe=1};"
            "if($__tf_probe -eq 0){try{" + _encoding_assignment() + "}catch{$__tf_probe=2}};"
            "if($__tf_probe -eq 0){" + startup_block + "};"
            "if($__tf_probe -eq 0){try{"
            + _encoding_assignment()
            + ";"
            + self._prompt_definition()
            + ";"
            + self._control_function_definition()
            + "}catch{$__tf_probe=2}};"
            + _cwd_assignment("$__tf_cwd_b64")
            + ";"
            + _write_dynamic_record(
                self._ready_prefix[1:].decode("ascii"),
                (
                    "$__tf_probe.ToString([Globalization.CultureInfo]::InvariantCulture)"
                    "+':' + $__tf_rc.ToString([Globalization.CultureInfo]::InvariantCulture)"
                    "+':' + " + _base64_expression("$__tf_version") + "+':' + $__tf_cwd_b64"
                ),
            )
        )
        return _encoded_invocation(
            script,
            self._session_token,
            self._line_ending,
            chunk_session_token=(None if self._windows_paths else self._session_token),
        )

    def wrap_command(self, command: str) -> CommandFrame:
        if self._state is not _ParserState.READY or self._pending_command is not None:
            raise DialectProtocolError("PowerShell protocol is not ready for another command")
        _validate_command(command)
        token = _validate_token(self._token_factory())
        correlation_id = f"pwsh_{token}"
        begin_marker = _RECORD_SEPARATOR + f"TFPWSH_BEGIN_{token}".encode() + _UNIT_SEPARATOR
        result_prefix = _RECORD_SEPARATOR + f"TFPWSH_END_{token}:".encode()
        script = (
            _write_static_record(begin_marker[1:-1].decode("ascii"))
            + ";$global:LASTEXITCODE=0;$__tf_success=$true;"
            "try{" + _dot_source_utf8(command, "$__tf_success") + "}"
            "catch{$__tf_success=$false;[Console]::Error.WriteLine([string]$_)};"
            + _exit_code_assignment("$__tf_rc", "$__tf_success", windows=self._windows_paths)
            + ";"
            + _encoding_assignment()
            + ";"
            + self._prompt_definition()
            + ";"
            + self._control_function_definition()
            + ";"
            + _cwd_assignment("$__tf_cwd_b64")
            + ";"
            + _write_dynamic_record(
                result_prefix[1:].decode("ascii"),
                (
                    "$__tf_rc.ToString([Globalization.CultureInfo]::InvariantCulture)"
                    "+':' + $__tf_cwd_b64"
                ),
            )
        )
        frame = CommandFrame(
            correlation_id,
            _encoded_invocation(
                script,
                token,
                self._line_ending,
                chunk_session_token=(None if self._windows_paths else self._session_token),
            ),
        )
        self._pending_command = _PendingCommand(correlation_id, begin_marker, result_prefix)
        self._state = _ParserState.WAITING_BEGIN
        return frame

    def recovery_input(self) -> bytes:
        pending = self._require_pending_command()
        if self._pending_recovery is not None:
            raise DialectProtocolError("PowerShell recovery is already in progress")
        token = _validate_token(self._token_factory())
        begin_marker = (
            _RECORD_SEPARATOR + f"TFPWSH_RECOVER_BEGIN_{token}".encode() + _UNIT_SEPARATOR
        )
        result_prefix = _RECORD_SEPARATOR + f"TFPWSH_RECOVER_END_{token}:".encode()
        input_bytes = f"{self._control_function} R {token}".encode("ascii") + self._line_ending
        self._pending_recovery = _PendingRecovery(
            pending.correlation_id,
            begin_marker,
            result_prefix,
            input_bytes.rstrip(b"\r\n"),
        )
        self._pending_result = None
        self._control_echo_seen = False
        self._control_echo_suffix_pending = False
        self._state = _ParserState.FINDING_RECOVERY_BEGIN
        return input_bytes

    def begin_finalization(self) -> CommandFrame:
        normal_completion = (
            self._state is _ParserState.AWAITING_FINALIZATION and self._pending_command is not None
        )
        recovered_completion = self._state is _ParserState.READY and self._pending_command is None
        if (
            not (normal_completion or recovered_completion)
            or self._pending_finalization is not None
        ):
            raise DialectProtocolError("PowerShell protocol is not awaiting finalization")
        token = _validate_token(self._token_factory())
        correlation_id = f"finalize_{token}"
        marker = _RECORD_SEPARATOR + f"TFPWSH_FINALIZE_{token}".encode() + _UNIT_SEPARATOR
        input_bytes = f"{self._control_function} F {token}".encode("ascii") + self._line_ending
        self._pending_finalization = _PendingFinalization(
            correlation_id,
            marker,
            input_bytes.rstrip(b"\r\n"),
        )
        self._control_echo_seen = False
        self._control_echo_suffix_pending = False
        self._state = _ParserState.FINDING_FINALIZATION
        return CommandFrame(correlation_id, input_bytes)

    def feed(self, data: bytes) -> tuple[DialectEvent, ...]:
        if self._state is _ParserState.CLOSED:
            raise DialectProtocolError("cannot feed a closed PowerShell protocol")
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
        try:
            if (
                self._state
                in {
                    _ParserState.CAPTURING,
                    _ParserState.FINALIZING,
                    _ParserState.AWAITING_FINALIZATION,
                }
                and self._buffer
            ):
                events.append(DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer)))
            elif self._state in {
                _ParserState.FINDING_FINALIZATION,
                _ParserState.FINALIZATION_PROMPT,
                _ParserState.FINDING_RECOVERY_BEGIN,
                _ParserState.FINDING_RECOVERY_RESULT,
                _ParserState.READING_RECOVERY,
            }:
                visible = self._visible_control_prefix_at_eof()
                if visible:
                    events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
        finally:
            self._buffer.clear()
            self._pending_command = None
            self._pending_result = None
            self._pending_recovery = None
            self._pending_finalization = None
            self._control_echo_suffix_pending = False
            self._state = _ParserState.CLOSED
        return tuple(events)

    def _advance(self, events: list[DialectEvent]) -> bool:
        if self._state is _ParserState.AWAITING_LAUNCH:
            return self._find_launch_marker()
        if self._state is _ParserState.AWAITING_INITIAL_PROMPT:
            return self._read_initial_prompt(events)
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
        if self._state is _ParserState.AWAITING_FINALIZATION:
            return False
        if self._state is _ParserState.FINDING_FINALIZATION:
            return self._find_finalization(events)
        if self._state is _ParserState.FINALIZATION_PROMPT:
            return self._read_finalization_prompt(events)
        if self._state is _ParserState.FINDING_RECOVERY_BEGIN:
            return self._find_recovery_begin(events)
        if self._state is _ParserState.FINDING_RECOVERY_RESULT:
            return self._find_recovery_result(events)
        if self._state is _ParserState.READING_RECOVERY:
            return self._read_recovery_record()
        return False

    def _find_launch_marker(self) -> bool:
        if not self._discard_through(self._launch_marker):
            return False
        self._state = _ParserState.AWAITING_INITIAL_PROMPT
        return True

    def _read_initial_prompt(self, events: list[DialectEvent]) -> bool:
        prompt_at = self._buffer.find(self._prompt)
        if prompt_at < 0:
            self._retain_marker_overlap(self._prompt)
            return False
        del self._buffer[: prompt_at + len(self._prompt)]
        events.append(DialectEvent(DialectEventKind.BOOTSTRAP_REQUIRED))
        self._state = _ParserState.STARTING
        return True

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
            or not _UNSIGNED_INTEGER.fullmatch(fields[0])
            or not _UNSIGNED_INTEGER.fullmatch(fields[1])
        ):
            raise DialectProtocolError("invalid PowerShell startup record")
        probe = int(fields[0])
        startup_rc = _parse_exit_code(fields[1], "startup")
        if probe == 1:
            raise UnsupportedShell("executable did not report an admitted PowerShell runtime")
        if probe == 2:
            raise UnsupportedShell("PowerShell could not establish the UTF-8 shell contract")
        if probe != 0:
            raise DialectProtocolError("invalid PowerShell compatibility probe result")
        version = _decode_text_field(fields[2], "shell version")
        cwd = _decode_cwd(fields[3], windows=self._windows_paths)
        if not version:
            raise UnsupportedShell("PowerShell runtime reported an empty version")
        if startup_rc != 0:
            raise DialectProtocolError(f"startup command failed with exit code {startup_rc}")
        self._pending_result = _PendingResult("", 0, cwd)
        self._startup_version = version
        self._prompt_separator_pending = True
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
                DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:safe_bytes]))
            )
            del self._buffer[:safe_bytes]
        return False

    def _read_result(self) -> bool:
        record = self._take_record()
        if record is None:
            return False
        fields = record.split(b":", 1)
        if len(fields) != 2 or not _UNSIGNED_INTEGER.fullmatch(fields[0]):
            raise DialectProtocolError("invalid PowerShell command result record")
        pending = self._require_pending_command()
        self._pending_result = _PendingResult(
            pending.correlation_id,
            _parse_exit_code(fields[0], "command"),
            _decode_cwd(fields[1], windows=self._windows_paths),
        )
        self._prompt_separator_pending = True
        self._state = _ParserState.FINALIZING
        return True

    def _read_prompt(self, events: list[DialectEvent]) -> bool:
        if not self._consume_prompt_separator():
            return False
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
                DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:prompt_at]))
            )
        tail = bytes(self._buffer[prompt_at + len(self._prompt) :])
        self._buffer.clear()
        if tail and self._pending_command is not None:
            events.append(DialectEvent(DialectEventKind.OUTPUT, data=tail))
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
            self._state = _ParserState.AWAITING_FINALIZATION
            return True
        self._pending_command = None
        self._pending_result = None
        self._pending_recovery = None
        self._state = _ParserState.READY
        return True

    def _find_finalization(self, events: list[DialectEvent]) -> bool:
        pending = self._require_pending_finalization()
        if not self._find_control_marker(
            events,
            marker=pending.marker,
            input_echo=pending.input_echo,
            strip_terminal_prompt=False,
        ):
            return False
        self._prompt_separator_pending = True
        self._state = _ParserState.FINALIZATION_PROMPT
        return True

    def _read_finalization_prompt(self, events: list[DialectEvent]) -> bool:
        pending = self._require_pending_finalization()
        if not self._consume_prompt_separator():
            return False
        prompt_at = self._buffer.find(self._prompt)
        if prompt_at < 0:
            safe_bytes = len(self._buffer) - len(self._prompt) + 1
            if safe_bytes > 0:
                visible = bytes(self._buffer[:safe_bytes])
                if visible:
                    events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
                del self._buffer[:safe_bytes]
            return False
        if prompt_at:
            events.append(
                DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:prompt_at]))
            )
        tail = bytes(self._buffer[prompt_at + len(self._prompt) :])
        self._buffer.clear()
        if tail:
            events.append(DialectEvent(DialectEventKind.OUTPUT, data=tail))
        events.append(
            DialectEvent(DialectEventKind.FINALIZED, correlation_id=pending.correlation_id)
        )
        self._pending_command = None
        self._pending_result = None
        self._pending_finalization = None
        self._state = _ParserState.READY
        return True

    def _find_recovery_begin(self, events: list[DialectEvent]) -> bool:
        recovery = self._require_pending_recovery()
        if not self._find_control_marker(
            events,
            marker=recovery.begin_marker,
            input_echo=recovery.input_echo,
            strip_terminal_prompt=True,
        ):
            return False
        self._state = _ParserState.FINDING_RECOVERY_RESULT
        return True

    def _find_recovery_result(self, events: list[DialectEvent]) -> bool:
        recovery = self._require_pending_recovery()
        marker_at = self._buffer.find(recovery.result_prefix)
        if marker_at >= 0:
            if marker_at:
                events.append(
                    DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:marker_at]))
                )
            del self._buffer[: marker_at + len(recovery.result_prefix)]
            self._state = _ParserState.READING_RECOVERY
            return True
        safe_bytes = len(self._buffer) - len(recovery.result_prefix) + 1
        if safe_bytes > 0:
            events.append(
                DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:safe_bytes]))
            )
            del self._buffer[:safe_bytes]
        return False

    def _read_recovery_record(self) -> bool:
        record = self._take_record()
        if record is None:
            return False
        recovery = self._require_pending_recovery()
        self._pending_result = _PendingResult(
            recovery.correlation_id,
            0,
            _decode_cwd(record, windows=self._windows_paths),
        )
        self._prompt_separator_pending = True
        self._state = _ParserState.FINALIZING
        return True

    def _find_control_marker(
        self,
        events: list[DialectEvent],
        *,
        marker: bytes,
        input_echo: bytes,
        strip_terminal_prompt: bool,
    ) -> bool:
        data = bytes(self._buffer)
        marker_at = data.find(marker)
        if self._control_echo_seen and not self._consume_control_echo_suffix(marker_at=marker_at):
            return False
        data = bytes(self._buffer)
        marker_at = data.find(marker)
        if not self._control_echo_seen:
            echo_span = _find_control_echo_span(data, input_echo, marker_at)
            if echo_span is not None:
                echo_at, echo_end = echo_span
                render_start = _control_render_start(data, echo_at)
                visible = data[:render_start]
                if strip_terminal_prompt:
                    visible = self._strip_one_terminal_prompt(visible)
                if visible:
                    events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
                del self._buffer[:echo_end]
                self._control_echo_seen = True
                self._control_echo_suffix_pending = True
                return self._find_control_marker(
                    events,
                    marker=marker,
                    input_echo=input_echo,
                    strip_terminal_prompt=strip_terminal_prompt,
                )
        if marker_at >= 0:
            visible = bytes(self._buffer[:marker_at])
            if not self._control_echo_seen:
                self._raise_for_private_echo_fragment(visible, input_echo)
                if strip_terminal_prompt:
                    visible = self._strip_one_terminal_prompt(visible)
            if visible:
                events.append(DialectEvent(DialectEventKind.OUTPUT, data=visible))
            del self._buffer[: marker_at + len(marker)]
            self._control_echo_seen = False
            self._control_echo_suffix_pending = False
            return True
        if self._control_echo_seen:
            safe_bytes = len(self._buffer) - len(marker) + 1
            if safe_bytes > 0:
                events.append(
                    DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:safe_bytes]))
                )
                del self._buffer[:safe_bytes]
            return False
        keep = len(input_echo) + len(marker) + _CONTROL_ECHO_PREFIX_BYTES
        safe_bytes = len(self._buffer) - keep
        if safe_bytes > 0:
            self._raise_for_private_echo_fragment(bytes(self._buffer[:safe_bytes]), input_echo)
            events.append(
                DialectEvent(DialectEventKind.OUTPUT, data=bytes(self._buffer[:safe_bytes]))
            )
            del self._buffer[:safe_bytes]
        return False

    def _visible_control_prefix_at_eof(self) -> bytes:
        data = bytes(self._buffer)
        pending: _PendingRecovery | _PendingFinalization | None
        pending = self._pending_recovery or self._pending_finalization
        if pending is None:
            return data
        private_values = [self._prompt]
        expected_marker: bytes | None = None
        if isinstance(pending, _PendingFinalization):
            expected_marker = pending.marker
        elif self._state is _ParserState.FINDING_RECOVERY_BEGIN:
            expected_marker = pending.begin_marker
        elif self._state is _ParserState.FINDING_RECOVERY_RESULT:
            expected_marker = pending.result_prefix
        elif self._state is _ParserState.READING_RECOVERY:
            if data:
                raise DialectProtocolError("incomplete private PowerShell recovery record at EOF")
            return b""
        if expected_marker is not None:
            private_values.append(expected_marker)
        if self._control_echo_seen:
            marker_at = data.find(expected_marker) if expected_marker is not None else -1
            self._consume_control_echo_suffix(marker_at=marker_at, end_of_stream=True)
            visible = bytes(self._buffer)
            self._raise_for_private_protocol_fragment(visible, private_values)
            return visible
        echo_span = _find_control_echo_span(data, pending.input_echo, -1)
        if echo_span is None:
            self._raise_for_private_echo_fragment(data, pending.input_echo)
            self._raise_for_private_protocol_fragment(data, private_values)
            return data
        echo_at, _ = echo_span
        visible = data[: _control_render_start(data, echo_at)]
        self._raise_for_private_protocol_fragment(visible, private_values)
        if self._pending_recovery is not None:
            return self._strip_one_terminal_prompt(visible)
        return visible

    def _raise_for_private_echo_fragment(self, data: bytes, input_echo: bytes) -> None:
        normalized = _without_vt_sequences(data)
        control_token = input_echo.rsplit(b" ", 1)[-1]
        identifiers = (
            self._control_function.encode("ascii"),
            self._session_token.encode("ascii"),
            control_token,
        )
        if any(_contains_fragment(normalized, value) for value in identifiers):
            raise DialectProtocolError("ambiguous PowerShell control input echo")

    def _raise_for_private_protocol_fragment(
        self,
        data: bytes,
        private_values: list[bytes],
    ) -> None:
        if any(_contains_fragment(data, value) for value in private_values):
            raise DialectProtocolError("private PowerShell protocol fragment at EOF")

    def _consume_prompt_separator(self) -> bool:
        if not self._prompt_separator_pending:
            return True
        if not self._buffer:
            return False
        if self._buffer[0] == 13:
            if len(self._buffer) == 1:
                return False
            if self._buffer[1] == 10:
                del self._buffer[:2]
        elif self._buffer[0] == 10:
            del self._buffer[:1]
        self._prompt_separator_pending = False
        return True

    def _consume_control_echo_suffix(
        self,
        *,
        marker_at: int,
        end_of_stream: bool = False,
    ) -> bool:
        if not self._control_echo_suffix_pending:
            return True
        marker_boundary = marker_at if marker_at >= 0 else len(self._buffer)
        if marker_at == 0:
            self._control_echo_suffix_pending = False
            return True
        if not self._buffer:
            if end_of_stream:
                self._control_echo_suffix_pending = False
                return True
            return False
        data = bytes(self._buffer[:marker_boundary])
        suffix_end = 0
        while match := _VT_SEQUENCE.match(data, suffix_end):
            suffix_end = match.end()
        remaining = data[suffix_end:]
        if not remaining:
            if end_of_stream or marker_at >= 0:
                self._control_echo_suffix_pending = False
                return True
            if len(data) > _CONTROL_ECHO_PREFIX_BYTES:
                raise DialectProtocolError("PowerShell control echo suffix exceeded its byte limit")
            return False
        if _is_incomplete_vt_sequence(remaining):
            if end_of_stream or marker_at >= 0:
                self._control_echo_suffix_pending = False
                return True
            if len(data) > _CONTROL_ECHO_PREFIX_BYTES:
                raise DialectProtocolError("PowerShell control echo suffix exceeded its byte limit")
            return False
        if remaining.startswith(b"\r\n"):
            del self._buffer[: suffix_end + 2]
        elif remaining.startswith(b"\n"):
            del self._buffer[: suffix_end + 1]
        elif remaining == b"\r" and not end_of_stream:
            return False
        else:
            self._control_echo_suffix_pending = False
            return True
        self._control_echo_suffix_pending = False
        return True

    def _strip_one_terminal_prompt(self, data: bytes) -> bytes:
        if data.endswith(self._prompt):
            return data[: -len(self._prompt)]
        return data

    def _take_record(self) -> bytes | None:
        end_at = self._buffer.find(_UNIT_SEPARATOR)
        if end_at < 0:
            if len(self._buffer) > self._max_control_bytes:
                raise DialectProtocolError("PowerShell control record exceeded its byte limit")
            return None
        if end_at > self._max_control_bytes:
            raise DialectProtocolError("PowerShell control record exceeded its byte limit")
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

    def _prompt_definition(self) -> str:
        prompt = self._prompt.decode("ascii")
        return f"function global:prompt{{{_powershell_literal(prompt)}}}"

    def _control_function_definition(self) -> str:
        begin = "TFPWSH_RECOVER_BEGIN_"
        end = "TFPWSH_RECOVER_END_"
        return (
            f"function global:{self._control_function}{{param([string]$Kind,[string]$Token);"
            "if($Kind -ceq 'F'){"
            + _write_dynamic_record("TFPWSH_FINALIZE_", "$Token")
            + ";"
            + _encoding_assignment()
            + ";"
            + self._prompt_definition()
            + ";return};if($Kind -cne 'R'){throw 'invalid control kind'};"
            + _write_dynamic_record(begin, "$Token")
            + ";"
            + _encoding_assignment()
            + ";"
            + self._prompt_definition()
            + ";"
            + _cwd_assignment("$__tf_control_cwd")
            + ";"
            + _write_dynamic_record(end, "$Token + ':' + $__tf_control_cwd")
            + "}"
        )

    def _require_pending_command(self) -> _PendingCommand:
        if self._pending_command is None:
            raise DialectProtocolError("missing PowerShell command framing state")
        return self._pending_command

    def _require_pending_result(self) -> _PendingResult:
        if self._pending_result is None:
            raise DialectProtocolError("missing PowerShell result framing state")
        return self._pending_result

    def _require_pending_recovery(self) -> _PendingRecovery:
        if self._pending_recovery is None:
            raise DialectProtocolError("missing PowerShell recovery framing state")
        return self._pending_recovery

    def _require_pending_finalization(self) -> _PendingFinalization:
        if self._pending_finalization is None:
            raise DialectProtocolError("missing PowerShell finalization framing state")
        return self._pending_finalization


def _encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _encoded_invocation(
    script: str,
    token: str,
    line_ending: bytes,
    *,
    chunk_session_token: str | None = None,
) -> bytes:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    sentinel = f"TFPWSH_INPUT_{token}"
    invocation = (
        f"$null={_powershell_literal(sentinel)};. ([ScriptBlock]::Create("
        "[Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}'))))"
    ).encode("ascii")
    if chunk_session_token is not None and len(payload) > _POSIX_INPUT_CHUNK_CHARACTERS:
        private_script = f"$null={_powershell_literal(sentinel)};{script}"
        payload = base64.b64encode(private_script.encode("utf-8")).decode("ascii")
        wire_prefix = f"__TFPWSH_CHUNK_{chunk_session_token}:"
        lines = [
            f"{wire_prefix}{'S' if offset == 0 else 'A'}:{token}:"
            f"{payload[offset : offset + _POSIX_INPUT_CHUNK_CHARACTERS]}"
            for offset in range(0, len(payload), _POSIX_INPUT_CHUNK_CHARACTERS)
        ]
        lines.append(f"{wire_prefix}X:{token}:")
        return line_ending.join(line.encode("ascii") for line in lines) + line_ending
    return invocation + line_ending


def _dot_source_utf8(script: str, status_target: str | None = None) -> str:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    source = f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))"
    if status_target is not None:
        source += "+[Environment]::NewLine+" + _powershell_literal(f"{status_target}=$?")
    return ". ([ScriptBlock]::Create(" + source + "))"


def _encoding_assignment() -> str:
    return (
        "$__tf_utf8=[Text.UTF8Encoding]::new($false);"
        "[Console]::InputEncoding=$__tf_utf8;"
        "[Console]::OutputEncoding=$__tf_utf8;"
        "$global:OutputEncoding=$__tf_utf8;"
        "if(([Console]::InputEncoding.WebName -cne 'utf-8') -or "
        "([Console]::OutputEncoding.WebName -cne 'utf-8') -or "
        "($global:OutputEncoding.WebName -cne 'utf-8')){throw 'UTF-8 bootstrap failed'}"
    )


def _exit_code_assignment(target: str, success: str, *, windows: bool = True) -> str:
    maximum = _MAX_WINDOWS_EXIT_CODE if windows else 255
    return (
        "$__tf_native=[int64]$global:LASTEXITCODE;"
        "if($__tf_native -lt 0){$__tf_native+=4294967296};"
        f"if({success}){{{target}=[uint32]0}}"
        f"elseif(($__tf_native -ne 0) -and ($__tf_native -le {maximum}))"
        f"{{{target}=[uint32]$__tf_native}}else{{{target}=[uint32]1}}"
    )


def _cwd_assignment(target: str) -> str:
    return (
        "$__tf_location=Get-Location;"
        "if($__tf_location.Provider.Name -ceq 'FileSystem'){"
        f"{target}=" + _base64_expression("[string]$__tf_location.Path") + f"}}else{{{target}=''}}"
    )


def _base64_expression(value: str) -> str:
    return f"[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes({value}))"


def _write_static_record(value: str) -> str:
    return (
        "[Console]::Out.Write(([char]30).ToString()+"
        + _powershell_literal(value)
        + "+([char]31).ToString())"
    )


def _write_dynamic_record(prefix: str, value: str) -> str:
    return (
        "[Console]::Out.Write(([char]30).ToString()+"
        + _powershell_literal(prefix)
        + "+"
        + value
        + "+([char]31).ToString())"
    )


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _control_render_start(data: bytes, echo_at: int) -> int:
    start = echo_at
    while True:
        previous = None
        for match in _VT_SEQUENCE.finditer(data, 0, start):
            if match.end() == start:
                previous = match
        if previous is None:
            return start
        start = previous.start()


def _find_control_echo_span(
    data: bytes,
    input_echo: bytes,
    marker_at: int,
) -> tuple[int, int] | None:
    """Find the last input echo before a marker, allowing complete VT sequences."""
    limit = len(data) if marker_at < 0 else marker_at
    candidate = data.find(input_echo[:1], 0, limit)
    found: tuple[int, int] | None = None
    while candidate >= 0:
        raw_at = candidate
        echo_at = 0
        while raw_at < limit and echo_at < len(input_echo):
            vt_match = _VT_SEQUENCE.match(data, raw_at, limit)
            if vt_match is not None:
                raw_at = vt_match.end()
                continue
            if data[raw_at] != input_echo[echo_at]:
                break
            raw_at += 1
            echo_at += 1
        if echo_at == len(input_echo):
            found = (candidate, raw_at)
        candidate = data.find(input_echo[:1], candidate + 1, limit)
    return found


def _without_vt_sequences(data: bytes) -> bytes:
    return _VT_SEQUENCE.sub(b"", data)


def _is_incomplete_vt_sequence(data: bytes) -> bool:
    if not data.startswith(b"\x1b"):
        return False
    if len(data) == 1:
        return True
    if data[1] == 91:  # CSI
        intermediates = False
        for value in data[2:]:
            if not intermediates and 48 <= value <= 63:
                continue
            if 32 <= value <= 47:
                intermediates = True
                continue
            return False
        return True
    if data[1] == 93:  # OSC
        escape_at = data.find(b"\x1b", 2)
        return b"\x07" not in data[2:] and (escape_at < 0 or escape_at == len(data) - 1)
    return False


def _contains_fragment(data: bytes, private_value: bytes) -> bool:
    fragment_size = min(_PRIVATE_FRAGMENT_BYTES, len(private_value))
    return any(
        private_value[offset : offset + fragment_size] in data
        for offset in range(len(private_value) - fragment_size + 1)
    )


def _parse_exit_code(field: bytes, label: str) -> int:
    if not _UNSIGNED_INTEGER.fullmatch(field):
        raise DialectProtocolError(f"invalid PowerShell {label} exit code")
    value = int(field)
    if value > _MAX_WINDOWS_EXIT_CODE:
        raise DialectProtocolError(f"PowerShell {label} exit code exceeded uint32")
    return value


def _decode_text_field(field: bytes, label: str) -> str:
    try:
        decoded = base64.b64decode(field, validate=True)
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise DialectProtocolError(f"invalid {label} in PowerShell control record") from error


def _decode_cwd(field: bytes, *, windows: bool = True) -> str:
    cwd = _decode_text_field(field, "cwd")
    absolute = PureWindowsPath(cwd).is_absolute() if windows else PurePosixPath(cwd).is_absolute()
    if not cwd or "\x00" in cwd or not absolute:
        raise DialectProtocolError("PowerShell reported an invalid cwd")
    return cwd


def _validate_token(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", token):
        raise ValueError("dialect tokens must contain 16-64 ASCII alphanumeric characters")
    return token


def _validate_command(command: str) -> None:
    if not command or "\x00" in command:
        raise DialectProtocolError("command must be non-empty and NUL-free")
    _validate_utf8(command, "command")


def _validate_start_request(request: ShellStartRequest, *, windows: bool = True) -> None:
    for label, value in (("executable", request.executable), ("cwd", request.cwd)):
        _validate_utf8(value, label)
        absolute = (
            PureWindowsPath(value).is_absolute() if windows else PurePosixPath(value).is_absolute()
        )
        if not value or "\x00" in value or not absolute:
            path_kind = "Windows" if windows else "POSIX"
            raise UnsupportedShell(
                f"PowerShell {label} must be a NUL-free native {path_kind} absolute path"
            )
    if request.startup_command is not None:
        _validate_command(request.startup_command)
    _validate_environment(request.environment, windows=windows)


def _validate_environment(environment: Mapping[str, str], *, windows: bool = True) -> None:
    normalized: set[str] = set()
    for key, value in environment.items():
        _validate_utf8(key, "environment key")
        _validate_utf8(value, "environment value")
        if not key or "\x00" in key or "\x00" in value:
            raise DialectProtocolError("environment keys and values must be NUL-free")
        comparable = key.casefold() if windows else key
        if comparable in normalized:
            raise DialectProtocolError("PowerShell environment keys must be unique ignoring case")
        normalized.add(comparable)


def _validate_utf8(value: str, label: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DialectProtocolError(f"{label} must be valid UTF-8") from error
