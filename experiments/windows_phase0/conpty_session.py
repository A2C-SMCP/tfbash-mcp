"""Single-reader low-level pywinpty session for the Phase 0 experiment."""

from __future__ import annotations

import base64
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Protocol


class PtyLike(Protocol):
    """Subset of the low-level pywinpty PTY used by the experiment."""

    pid: int | None

    def write(self, value: str) -> int: ...

    def read(self, *, blocking: bool) -> str: ...

    def cancel_io(self) -> None: ...

    def iseof(self) -> bool: ...

    def isalive(self) -> bool: ...


def powershell_literal(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal."""

    return "'" + value.replace("'", "''") + "'"


def _encoded_script_invocation(script: str) -> str:
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        ". ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}'))))\r\n"
    )


@dataclass(frozen=True, slots=True)
class CommandTicket:
    """Markers and output cursor for one in-flight PowerShell command."""

    cursor: int
    begin_marker: str
    end_marker: str
    prompt_marker: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Output and normalized exit code from one completed command."""

    output: str
    exit_code: int
    duration_ms: int
    raw_output: str


def parse_command_output(raw_output: str, ticket: CommandTicket) -> tuple[str, int]:
    """Extract only command output from the marker-delimited PTY transcript."""

    begin_offset = raw_output.find(ticket.begin_marker)
    if begin_offset < 0:
        raise RuntimeError("PowerShell prompt arrived without a begin marker")
    output_start = begin_offset + len(ticket.begin_marker)
    if raw_output.startswith("\r\n", output_start):
        output_start += 2
    elif raw_output.startswith("\n", output_start):
        output_start += 1
    else:
        raise RuntimeError("PowerShell begin marker was not line-delimited")

    marker_offset = raw_output.find(ticket.end_marker, output_start)
    if marker_offset < 0:
        raise RuntimeError("PowerShell prompt arrived without an exit marker")
    code_start = marker_offset + len(ticket.end_marker)
    code_text = raw_output[code_start:].splitlines()[0].strip()
    if not code_text.isdigit():
        raise RuntimeError(f"invalid PowerShell exit marker payload: {code_text!r}")
    prompt_offset = raw_output.find(ticket.prompt_marker, code_start + len(code_text))
    if prompt_offset < 0:
        raise RuntimeError("PowerShell exit marker arrived without the following prompt")
    return raw_output[output_start:marker_offset], int(code_text)


class ConPtySession:
    """A persistent PowerShell process with exactly one PTY reader."""

    def __init__(self, pwsh: str, *, before_bootstrap: Callable[[int], None] | None = None) -> None:
        self._pwsh = pwsh
        self._before_bootstrap = before_bootstrap
        self._condition = threading.Condition()
        self._output = ""
        self._reader_error: str | None = None
        self._reader_done = False
        self._pty: PtyLike | None = None
        self._reader: threading.Thread | None = None
        self._prompt_marker = f"__TF_PROMPT_{uuid.uuid4().hex}__"
        self._terminal_query_tail = ""

    @property
    def pid(self) -> int:
        pty = self._require_pty()
        pid = pty.pid
        if pid is None:
            raise RuntimeError("ConPTY did not expose a spawned process PID")
        return int(pid)

    @property
    def prompt_marker(self) -> str:
        return self._prompt_marker

    def start(self, timeout_seconds: float = 15.0) -> None:
        """Spawn, establish ownership, start the reader, and bootstrap UTF-8/prompt state."""

        from winpty import PTY, Backend

        pty = PTY(120, 40, backend=Backend.ConPTY)
        args = "-NoLogo -NoProfile -NoExit"
        if not pty.spawn(self._pwsh, cmdline=args):
            raise RuntimeError("pywinpty returned false while spawning PowerShell")
        self._pty = pty
        if self._before_bootstrap is not None:
            self._before_bootstrap(self.pid)

        self._reader = threading.Thread(
            target=self._reader_main,
            name=f"phase0-conpty-reader-{self.pid}",
            daemon=True,
        )
        self._reader.start()

        ready_marker = f"__TF_READY_{uuid.uuid4().hex}__"
        bootstrap = "\n".join(
            (
                "$utf8 = [Text.UTF8Encoding]::new($false)",
                "[Console]::InputEncoding = $utf8",
                "[Console]::OutputEncoding = $utf8",
                "$global:OutputEncoding = $utf8",
                f"function global:prompt {{ {powershell_literal(self._prompt_marker)} }}",
                f"[Console]::Out.WriteLine({powershell_literal(ready_marker)})",
            )
        )
        cursor = self.checkpoint()
        self.write(_encoded_script_invocation(bootstrap))
        self.wait_for_text(ready_marker, cursor, timeout_seconds)
        self.wait_for_text(self._prompt_marker, cursor, timeout_seconds)

    def checkpoint(self) -> int:
        with self._condition:
            return len(self._output)

    def output_since(self, cursor: int) -> str:
        with self._condition:
            return self._output[cursor:]

    def wait_for_text(self, needle: str, cursor: int, timeout_seconds: float) -> str:
        """Wait on reader notifications until text arrives or the deadline expires."""

        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while needle not in self._output[cursor:]:
                if self._reader_error is not None:
                    raise RuntimeError(f"ConPTY reader failed: {self._reader_error}")
                if self._reader_done:
                    raise EOFError(f"ConPTY closed before marker arrived: {needle}")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for ConPTY marker: {needle}")
                self._condition.wait(remaining)
            return self._output[cursor:]

    def wait_for_eof(self, timeout_seconds: float) -> bool:
        deadline = monotonic() + timeout_seconds
        with self._condition:
            while not self._reader_done:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def write(self, value: str, *, on_enter: Callable[[], None] | None = None) -> int:
        pty = self._require_pty()
        if on_enter is not None:
            on_enter()
        return int(pty.write(value))

    def start_script(self, script: str) -> CommandTicket:
        token = uuid.uuid4().hex
        begin = f"__TF_BEGIN_{token}__"
        end_prefix = f"__TF_END_{token}__="
        script_payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        wrapper = "\n".join(
            (
                f"[Console]::Out.WriteLine({powershell_literal(begin)})",
                "$global:LASTEXITCODE = 0",
                "$__tf_success = $true",
                "try {",
                "  . ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString("
                f"      [Convert]::FromBase64String('{script_payload}'))))",
                "  $__tf_success = $?",
                "} catch {",
                "  $__tf_success = $false",
                "  [Console]::Out.WriteLine('__TF_EXCEPTION__=' + $_.Exception.GetType().FullName)",
                "}",
                "if (($null -ne $LASTEXITCODE) -and ($LASTEXITCODE -ne 0)) {",
                "  $__tf_exit = [uint32]$LASTEXITCODE",
                "} elseif ($__tf_success) {",
                "  $__tf_exit = [uint32]0",
                "} else {",
                "  $__tf_exit = [uint32]1",
                "}",
                f"[Console]::Out.WriteLine({powershell_literal(end_prefix)} + $__tf_exit)",
            )
        )
        cursor = self.checkpoint()
        self.write(_encoded_script_invocation(wrapper))
        return CommandTicket(cursor, begin, end_prefix, self._prompt_marker)

    def await_script(self, ticket: CommandTicket, timeout_seconds: float) -> CommandResult:
        started = monotonic()
        # The marker prefix and its numeric payload may span separate PTY reads.
        # The following prompt proves that the complete marker line has arrived.
        raw_output = self.wait_for_text(ticket.prompt_marker, ticket.cursor, timeout_seconds)
        output, exit_code = parse_command_output(raw_output, ticket)
        return CommandResult(
            output=output,
            exit_code=exit_code,
            duration_ms=round((monotonic() - started) * 1000),
            raw_output=raw_output,
        )

    def run_script(self, script: str, timeout_seconds: float = 15.0) -> CommandResult:
        return self.await_script(self.start_script(script), timeout_seconds)

    def interrupt(self) -> int:
        return self.write("\x03")

    def cancel_reader(self) -> None:
        pty = self._require_pty()
        pty.cancel_io()

    def close(
        self,
        timeout_seconds: float = 5.0,
        *,
        process_exited: Callable[[], bool] | None = None,
    ) -> None:
        if self._pty is None:
            return
        if not self._reader_done:
            with suppress(Exception):
                self.write("exit\r\n")
            if not self.wait_for_eof(timeout_seconds):
                with suppress(Exception):
                    self.cancel_reader()
                if not self.wait_for_eof(timeout_seconds):
                    raise TimeoutError("ConPTY reader did not terminate after cancellation")
        pty = self._require_pty()
        if process_exited is not None:
            if not process_exited():
                raise RuntimeError("ConPTY process identity remained alive after close")
        elif pty.isalive():
            raise RuntimeError("ConPTY process remained alive after close")
        self._pty = None

    def _require_pty(self) -> PtyLike:
        if self._pty is None:
            raise RuntimeError("ConPTY session has not been started or is already closed")
        return self._pty

    def _reader_main(self) -> None:
        pty = self._require_pty()
        try:
            while True:
                chunk = str(pty.read(blocking=True))
                if chunk:
                    with self._condition:
                        self._output += chunk
                        self._condition.notify_all()
                    self._answer_terminal_queries(chunk)
                    continue
                if pty.iseof() or not pty.isalive():
                    break
        except Exception as exc:
            if not pty.iseof() and pty.isalive():
                with self._condition:
                    self._reader_error = repr(exc)
        finally:
            with self._condition:
                self._reader_done = True
                self._condition.notify_all()

    def _answer_terminal_queries(self, chunk: str) -> None:
        # ConPTY can block application startup while waiting for terminal-status replies.
        combined = self._terminal_query_tail + chunk
        for _ in range(combined.count("\x1b[5n")):
            self.write("\x1b[0n")
        for _ in range(combined.count("\x1b[6n")):
            self.write("\x1b[1;1R")
        self._terminal_query_tail = combined[-3:]

    def __enter__(self) -> ConPtySession:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
