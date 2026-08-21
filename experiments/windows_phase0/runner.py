"""Execute the pre-registered Windows V1 Phase 0 experiment.

Run as ``python -m experiments.windows_phase0.runner --help`` on Windows.
The script intentionally uses pywinpty's low-level PTY API rather than the
high-level PtyProcess compatibility wrapper, whose fixed sleeps would pollute
the event-driven transport and lifecycle observations.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from importlib.metadata import version as package_version
from pathlib import Path
from time import monotonic

from experiments.windows_phase0.conpty_session import ConPtySession, powershell_literal
from experiments.windows_phase0.contracts import (
    EnvironmentTier,
    JsonlRecorder,
    Observation,
    Outcome,
    evaluate_gates,
    observe_backpressure,
    prepare_output_directory,
    summary_payload,
    validate_environment,
    validate_toolchain,
)
from experiments.windows_phase0.windows_api import (
    KillOnCloseJob,
    NamedManualResetEvent,
    ProcessExitMonitor,
    ProcessIdentity,
    descendant_identities,
    identity_is_alive,
    is_missing_process_error,
    process_identity,
    taskkill_tree,
    wait_for_exit,
)

EXPERIMENT_DIR = Path(__file__).resolve().parent
TREE_FIXTURE = EXPERIMENT_DIR / "tree_fixture.py"
LATE_OUTPUT_FIXTURE = EXPERIMENT_DIR / "late_output_fixture.py"
CONTROL_DEADLINE_SECONDS = 3.0
SOFT_INTERRUPT_SECONDS = 0.75
TREE_DEADLINE_SECONDS = 10.0
LIFECYCLE_DEADLINE_SECONDS = 30.0
TAIL_BYTES = 262_144
BACKPRESSURE_BYTES = 16 * 1024 * 1024
BACKPRESSURE_ESTABLISH_SECONDS = 0.25
VT_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _normalized_output(value: str) -> str:
    return VT_SEQUENCE.sub("", value).replace("\r\n", "\n")


def _resolve_runner_commit(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    completed = subprocess.run(
        ["git", "-C", str(EXPERIMENT_DIR.parents[1]), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _command_version(command: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        return "unavailable"
    completed = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _python_command(source: str, *arguments: str) -> str:
    pieces = ["&", powershell_literal(sys.executable), "-u", "-c", powershell_literal(source)]
    pieces.extend(powershell_literal(argument) for argument in arguments)
    return " ".join(pieces)


def _exception_details(exc: BaseException) -> dict[str, object]:
    return {
        "exception_type": type(exc).__name__,
        "exception": str(exc),
        "traceback": traceback.format_exc(),
    }


class ExperimentLog:
    """Keep in-memory observations and append every one to durable JSONL."""

    def __init__(self, recorder: JsonlRecorder) -> None:
        self._recorder = recorder
        self.observations: list[Observation] = []

    def add(
        self,
        scenario: str,
        iteration: int,
        passed: bool,
        duration_ms: int,
        details: dict[str, object],
    ) -> None:
        observation = Observation(
            scenario=scenario,
            iteration=iteration,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            duration_ms=duration_ms,
            details=details,
        )
        self.observations.append(observation)
        self._recorder.record(observation)
        print(
            f"[{observation.outcome.value.upper()}] {scenario} "
            f"iteration={iteration} duration_ms={duration_ms}",
            flush=True,
        )

    def failed(self, scenario: str, iteration: int, started: float, exc: BaseException) -> None:
        self.add(
            scenario,
            iteration,
            False,
            round((monotonic() - started) * 1000),
            _exception_details(exc),
        )


class TreeReporter:
    """Event-driven TCP collector for the three fixture process identities."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(3)
        host, port = self._socket.getsockname()
        self.host = str(host)
        self.port = int(port)

    def collect(self, count: int, timeout_seconds: float) -> tuple[dict[str, object], ...]:
        self._socket.settimeout(timeout_seconds)
        reports: list[dict[str, object]] = []
        for _ in range(count):
            connection, _address = self._socket.accept()
            with connection:
                stream = connection.makefile("rb")
                line = stream.readline(16_384)
                if not line.endswith(b"\n"):
                    raise RuntimeError("fixture report was truncated")
                report = json.loads(line)
                if not isinstance(report, dict):
                    raise RuntimeError("fixture report was not an object")
                reports.append(report)
        return tuple(reports)

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> TreeReporter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _safe_kill_session(session: ConPtySession) -> None:
    """Force cleanup for a failed gate; unknown or incomplete cleanup is fatal."""

    identity: ProcessIdentity | None = None
    try:
        identity = process_identity(session.pid)
    except OSError as exc:
        if not is_missing_process_error(exc):
            raise
    except RuntimeError:
        pass
    try:
        if identity is not None:
            cleanup = taskkill_tree(identity, force=True, timeout_seconds=5.0)
            if not bool(cleanup["all_exited"]):
                raise RuntimeError(f"emergency session cleanup was incomplete: {cleanup}")
    finally:
        with suppress(Exception):
            session.cancel_reader()


def _close_session_verified(session: ConPtySession) -> None:
    """Close a session and fail the gate if identity-fenced forced cleanup was needed."""

    try:
        identity = process_identity(session.pid)
    except OSError as exc:
        if not is_missing_process_error(exc):
            raise
        identity = None
    except RuntimeError:
        identity = None

    try:
        session.close(
            process_exited=(
                (lambda: True) if identity is None else (lambda: not identity_is_alive(identity))
            )
        )
    except Exception as close_error:
        cleanup: dict[str, object] | None = None
        if identity is not None:
            cleanup = taskkill_tree(identity, force=True, timeout_seconds=5.0)
            if not bool(cleanup["all_exited"]):
                raise RuntimeError(
                    f"session close failed and forced cleanup was incomplete: {cleanup}"
                ) from close_error
        raise RuntimeError(
            f"session close failed and required forced cleanup: {cleanup}"
        ) from close_error

    if identity is not None and identity_is_alive(identity):
        cleanup = taskkill_tree(identity, force=True, timeout_seconds=5.0)
        raise RuntimeError(f"session remained alive after close: {cleanup}")


def _with_session(
    operation: Callable[[ConPtySession], dict[str, object]], pwsh: str
) -> dict[str, object]:
    session = ConPtySession(pwsh)
    try:
        session.start()
        return operation(session)
    finally:
        _close_session_verified(session)


def run_basic_contracts(log: ExperimentLog, pwsh: str) -> None:
    scenarios: tuple[tuple[str, Callable[[ConPtySession], dict[str, object]]], ...] = (
        ("unicode", _unicode_scenario),
        ("persistent_state", _persistent_state_scenario),
        ("multiline", _multiline_scenario),
        ("real_exit_code", _exit_code_scenario),
        ("text_stdin", _text_stdin_scenario),
        ("raw_nul_stdin", _raw_nul_scenario),
        ("long_command_yield", _long_command_yield_scenario),
        ("backpressure_control", _backpressure_scenario),
    )
    for scenario, operation in scenarios:
        started = monotonic()
        try:
            details = _with_session(operation, pwsh)
            passed = bool(details.pop("passed"))
            log.add(scenario, 1, passed, round((monotonic() - started) * 1000), details)
        except Exception as exc:
            log.failed(scenario, 1, started, exc)


def _unicode_scenario(session: ConPtySession) -> dict[str, object]:
    expected = "中文😀 café"
    result = session.run_script(f"[Console]::Out.WriteLine({powershell_literal(expected)})")
    return {
        "passed": result.exit_code == 0 and _normalized_output(result.output) == expected + "\n",
        "expected": expected,
        "observed": _normalized_output(result.output),
        "exit_code": result.exit_code,
    }


def _persistent_state_scenario(session: ConPtySession) -> dict[str, object]:
    token = uuid.uuid4().hex
    setup = session.run_script(
        f"$env:TF_PHASE0_STATE = {powershell_literal(token)}; "
        "$global:TF_PHASE0_EXPECTED_CWD = (Resolve-Path $env:TEMP).Path; "
        "Set-Location $global:TF_PHASE0_EXPECTED_CWD"
    )
    result = session.run_script(
        "[ordered]@{State=$env:TF_PHASE0_STATE; Cwd=(Get-Location).Path; "
        "ExpectedCwd=$global:TF_PHASE0_EXPECTED_CWD} | ConvertTo-Json -Compress"
    )
    observed = json.loads(_normalized_output(result.output).strip())
    cwd = str(observed["Cwd"])
    expected_cwd = str(observed["ExpectedCwd"])
    return {
        "passed": (
            setup.exit_code == 0
            and result.exit_code == 0
            and observed["State"] == token
            and os.path.samefile(cwd, expected_cwd)
        ),
        "setup_exit_code": setup.exit_code,
        "probe_exit_code": result.exit_code,
        "observed": observed,
    }


def _multiline_scenario(session: ConPtySession) -> dict[str, object]:
    expected = "line-1|line-2|line-3"
    result = session.run_script(
        "$values = @(\n  'line-1'\n  'line-2'\n  'line-3'\n)\n"
        "[Console]::Out.WriteLine(($values -join '|'))"
    )
    observed = _normalized_output(result.output)
    return {
        "passed": result.exit_code == 0 and observed == expected + "\n",
        "expected": expected,
        "observed": observed,
        "exit_code": result.exit_code,
    }


def _exit_code_scenario(session: ConPtySession) -> dict[str, object]:
    result = session.run_script("& $env:ComSpec /d /c exit 37")
    return {
        "passed": result.exit_code == 37 and _normalized_output(result.output) == "",
        "observed_exit_code": result.exit_code,
        "observed_output": _normalized_output(result.output),
    }


def _text_stdin_scenario(session: ConPtySession) -> dict[str, object]:
    ready = f"READY_{uuid.uuid4().hex}"
    done = f"TEXT_{uuid.uuid4().hex}="
    expected = "输入😀"
    source = (
        "import base64,sys\n"
        f"print({ready!r}, flush=True)\n"
        "data=sys.stdin.buffer.readline()\n"
        f"print({done!r}+base64.b64encode(data).decode('ascii'), flush=True)"
    )
    ticket = session.start_script(_python_command(source))
    session.wait_for_text(ready, ticket.cursor, 5.0)
    session.write(expected + "\r\n")
    result = session.await_script(ticket, 5.0)
    encoded = base64.b64encode((expected + "\r\n").encode()).decode()
    observed = _normalized_output(result.output)
    expected_output = f"{ready}\n{expected}\n{done}{encoded}\n"
    return {
        "passed": result.exit_code == 0 and observed == expected_output,
        "expected_base64": encoded,
        "observed": observed,
        "exit_code": result.exit_code,
    }


def _raw_nul_scenario(session: ConPtySession) -> dict[str, object]:
    ready = f"READY_{uuid.uuid4().hex}"
    done = f"RAW_{uuid.uuid4().hex}="
    source = (
        "import base64,sys\n"
        f"print({ready!r}, flush=True)\n"
        "data=sys.stdin.buffer.readline()\n"
        f"print({done!r}+base64.b64encode(data).decode('ascii'), flush=True)"
    )
    ticket = session.start_script(_python_command(source))
    session.wait_for_text(ready, ticket.cursor, 5.0)
    session.write("\x00\r\n")
    result = session.await_script(ticket, 5.0)
    expected = base64.b64encode(b"\x00\r\n").decode()
    observed = _normalized_output(result.output)
    expected_output = f"{ready}\n^@\n{done}{expected}\n"
    return {
        "passed": result.exit_code == 0 and observed == expected_output,
        "expected_base64": expected,
        "observed": observed,
        "exit_code": result.exit_code,
        "api_constraint": "pywinpty accepts Unicode strings, not an arbitrary byte buffer",
    }


def _long_command_yield_scenario(session: ConPtySession) -> dict[str, object]:
    ready = f"READY_{uuid.uuid4().hex}"
    source = f"import threading\nprint({ready!r}, flush=True)\nthreading.Event().wait(60)"
    ticket = session.start_script(_python_command(source))
    session.wait_for_text(ready, ticket.cursor, 5.0)
    yielded_running = False
    try:
        session.await_script(ticket, 0.1)
    except TimeoutError:
        yielded_running = True
    session.interrupt()
    result = session.await_script(ticket, CONTROL_DEADLINE_SECONDS)
    return {
        "passed": yielded_running and result.exit_code != 0,
        "yielded_running": yielded_running,
        "post_interrupt_exit_code": result.exit_code,
    }


def _backpressure_scenario(session: ConPtySession) -> dict[str, object]:
    ready = f"READY_{uuid.uuid4().hex}"
    source = f"import threading\nprint({ready!r}, flush=True)\nthreading.Event().wait(60)"
    ticket = session.start_script(_python_command(source))
    session.wait_for_text(ready, ticket.cursor, 5.0)
    shell_identity = process_identity(session.pid)
    write_result: dict[str, object] = {}
    writer_started = threading.Event()
    write_done = threading.Event()
    control_result: dict[str, object] = {}
    control_done = threading.Event()
    payload = "x" * BACKPRESSURE_BYTES

    def write_payload() -> None:
        try:
            write_result["accepted_characters"] = session.write(
                payload,
                on_enter=writer_started.set,
            )
        except Exception as exc:
            write_result["write_error"] = repr(exc)
        finally:
            write_done.set()

    writer = threading.Thread(target=write_payload, name="phase0-backpressure-writer", daemon=True)
    writer.start()
    backpressure_established = observe_backpressure(
        writer_started,
        write_done,
        start_deadline_seconds=1.0,
        establish_deadline_seconds=BACKPRESSURE_ESTABLISH_SECONDS,
    )

    def send_control() -> None:
        try:
            control_result["accepted_characters"] = session.interrupt()
        except Exception as exc:
            control_result["control_error"] = repr(exc)
        finally:
            control_done.set()

    started = monotonic()
    if backpressure_established:
        controller = threading.Thread(
            target=send_control,
            name="phase0-backpressure-controller",
            daemon=True,
        )
        controller.start()
        control_finished = control_done.wait(CONTROL_DEADLINE_SECONDS)
    else:
        control_finished = False
    control_call_ms = round((monotonic() - started) * 1000)
    recovered = False
    exit_code: int | None = None
    if control_finished:
        remaining = max(0.0, CONTROL_DEADLINE_SECONDS - (monotonic() - started))
        try:
            result = session.await_script(ticket, remaining)
            exit_code = result.exit_code
            recovered = result.exit_code != 0
        except (EOFError, RuntimeError, TimeoutError) as exc:
            control_result["recovery_error"] = repr(exc)
    if not recovered:
        control_result["cleanup"] = taskkill_tree(
            shell_identity,
            force=True,
            timeout_seconds=TREE_DEADLINE_SECONDS,
        )
        with suppress(Exception):
            session.cancel_reader()
    recovery_ms = round((monotonic() - started) * 1000)
    writer_finished = write_done.wait(1.0)
    return {
        "passed": (
            backpressure_established and control_finished and recovery_ms <= 3000 and recovered
        ),
        "backpressure_established": backpressure_established,
        "payload_characters": BACKPRESSURE_BYTES,
        "control_call_ms": control_call_ms,
        "recovery_ms": recovery_ms,
        "control_finished": control_finished,
        "recovered": recovered,
        "exit_code": exit_code,
        "writer_finished": writer_finished,
        **control_result,
        **write_result,
    }


def _late_fixture_command(event_name: str, before_wait_token: str, after_wait_token: str) -> str:
    return " ".join(
        (
            "&",
            powershell_literal(sys.executable),
            "-u",
            powershell_literal(str(LATE_OUTPUT_FIXTURE)),
            "--event",
            powershell_literal(event_name),
            "--before-wait-token",
            powershell_literal(before_wait_token),
            "--after-wait-token",
            powershell_literal(after_wait_token),
        )
    )


def _unique_terminal_scenario(pwsh: str) -> dict[str, object]:
    session = ConPtySession(pwsh)
    late_identity: ProcessIdentity | None = None
    with (
        TreeReporter() as late_ack_reporter,
        NamedManualResetEvent(f"Local\\tfbash-late-{uuid.uuid4().hex}") as late_release,
        NamedManualResetEvent(f"Local\\tfbash-next-{uuid.uuid4().hex}") as next_finish,
    ):
        late_ready = f"LATE_READY_{uuid.uuid4().hex}"
        late_token = f"LATE_OUTPUT_{uuid.uuid4().hex}"
        spawn_marker = f"LATE_PID_{uuid.uuid4().hex}="
        next_ready = f"NEXT_READY_{uuid.uuid4().hex}"
        next_done = f"NEXT_DONE_{uuid.uuid4().hex}"
        source = (
            "import subprocess,sys\n"
            "process=subprocess.Popen([sys.executable,'-u',sys.argv[1],"
            "'--event',sys.argv[2],'--before-wait-token',sys.argv[3],"
            "'--after-wait-token',sys.argv[4],'--ack-host',sys.argv[5],"
            "'--ack-port',sys.argv[6]],stdin=subprocess.DEVNULL)\n"
            f"print({spawn_marker!r}+str(process.pid), flush=True)"
        )
        try:
            session.start()
            first_ticket = session.start_script(
                _python_command(
                    source,
                    str(LATE_OUTPUT_FIXTURE),
                    late_release.name,
                    late_ready,
                    late_token,
                    late_ack_reporter.host,
                    str(late_ack_reporter.port),
                )
            )
            session.wait_for_text(late_ready, first_ticket.cursor, 5.0)
            ready_ack = late_ack_reporter.collect(1, 5.0)[0]
            first_result = session.await_script(first_ticket, 5.0)
            match = re.search(re.escape(spawn_marker) + r"(\d+)", first_result.output)
            if match is None:
                raise RuntimeError("late-output child PID marker was missing")
            launcher_pid = int(match.group(1))
            if ready_ack.get("status") != "ready" or ready_ack.get("token") != late_token:
                raise RuntimeError(f"invalid late-output ready acknowledgement: {ready_ack}")
            late_identity = process_identity(int(ready_ack["pid"]))

            with ProcessExitMonitor(late_identity) as exit_monitor:
                next_ticket = session.start_script(
                    _late_fixture_command(next_finish.name, next_ready, next_done)
                )
                session.wait_for_text(next_ready, next_ticket.cursor, 5.0)
                late_release.set()
                flushed_ack = late_ack_reporter.collect(1, 5.0)[0]
                late_exit_code = exit_monitor.wait(5.0)
            if late_exit_code is None:
                raise TimeoutError("late-output child did not exit after release")
            next_finish.set()
            next_result = session.await_script(next_ticket, 5.0)
            barrier_token = f"BARRIER_{uuid.uuid4().hex}"
            barrier_result = session.run_script(
                f"[Console]::Out.WriteLine({powershell_literal(barrier_token)})"
            )

            first_terminal_count = first_result.raw_output.count(first_ticket.end_marker)
            next_terminal_count = next_result.raw_output.count(next_ticket.end_marker)
            late_contaminated_next = late_token in next_result.output
            late_contaminated_barrier = late_token in barrier_result.output
            transcript = session.output_since(first_ticket.cursor)
            transcript_token_count = transcript.count(late_token)
            ready_ack_valid = ready_ack == {
                "pid": late_identity.pid,
                "status": "ready",
                "token": late_token,
            }
            flushed_ack_valid = flushed_ack == {
                "pid": late_identity.pid,
                "status": "stdout-flushed",
                "token": late_token,
            }
            passed = (
                first_result.exit_code == 0
                and next_result.exit_code == 0
                and barrier_result.exit_code == 0
                and _normalized_output(barrier_result.output) == barrier_token + "\n"
                and first_terminal_count == 1
                and next_terminal_count == 1
                and next_result.output.count(next_ready) == 1
                and next_result.output.count(next_done) == 1
                and ready_ack_valid
                and flushed_ack_valid
                and late_exit_code == 0
                and transcript_token_count == 1
                and not late_contaminated_next
                and not late_contaminated_barrier
            )
            return {
                "passed": passed,
                "first_terminal_count": first_terminal_count,
                "next_terminal_count": next_terminal_count,
                "next_ready_count": next_result.output.count(next_ready),
                "next_done_count": next_result.output.count(next_done),
                "late_contaminated_next_execution": late_contaminated_next,
                "late_contaminated_barrier_execution": late_contaminated_barrier,
                "late_ready_ack": ready_ack,
                "late_ready_ack_valid": ready_ack_valid,
                "late_stdout_ack": flushed_ack,
                "late_stdout_ack_valid": flushed_ack_valid,
                "late_exit_code": late_exit_code,
                "transcript_token_count": transcript_token_count,
                "launcher_pid": launcher_pid,
                "late_pid": late_identity.pid,
                "contract": "output emitted after terminal must not enter the next execution",
            }
        finally:
            late_release.set()
            next_finish.set()
            if late_identity is not None:
                with suppress(OSError):
                    wait_for_exit((late_identity,), 2.0)
                if identity_is_alive(late_identity):
                    with suppress(OSError):
                        taskkill_tree(late_identity, force=True, timeout_seconds=2.0)
            _close_session_verified(session)


def run_tail_and_terminal_gates(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    for iteration in range(1, repetitions + 1):
        started = monotonic()
        tail = f"TAIL_{iteration}_{uuid.uuid4().hex}"
        session = ConPtySession(pwsh)
        session_closed = False
        try:
            session.start()
            source = f"import sys\nsys.stdout.write('x'*{TAIL_BYTES}+{tail!r})\nsys.stdout.flush()"
            ticket = session.start_script(_python_command(source))
            result = session.await_script(ticket, 15.0)
            tail_count = result.output.count(tail)
            exact_output = result.output == "x" * TAIL_BYTES + tail
            details = {
                "tail_count": tail_count,
                "output_characters": len(result.output),
                "expected_characters": TAIL_BYTES + len(tail),
                "exact_output": exact_output,
                "exit_code": result.exit_code,
            }
            _close_session_verified(session)
            session_closed = True
            log.add(
                "tail_drain",
                iteration,
                result.exit_code == 0 and tail_count == 1 and exact_output,
                round((monotonic() - started) * 1000),
                details,
            )
        except Exception as exc:
            log.failed("tail_drain", iteration, started, exc)
            _safe_kill_session(session)
        finally:
            if not session_closed:
                _safe_kill_session(session)

    for iteration in range(1, repetitions + 1):
        started = monotonic()
        try:
            details = _unique_terminal_scenario(pwsh)
            passed = bool(details.pop("passed"))
            log.add(
                "unique_terminal_state",
                iteration,
                passed,
                round((monotonic() - started) * 1000),
                details,
            )
        except Exception as exc:
            log.failed("unique_terminal_state", iteration, started, exc)


def run_interrupt_gate(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    for iteration in range(1, repetitions + 1):
        started = monotonic()
        session = ConPtySession(pwsh)
        rebuilt: ConPtySession | None = None
        session_closed = False
        try:
            session.start()
            ready = f"READY_{uuid.uuid4().hex}"
            source = f"import threading\nprint({ready!r}, flush=True)\nthreading.Event().wait(60)"
            ticket = session.start_script(_python_command(source))
            session.wait_for_text(ready, ticket.cursor, 5.0)
            shell_identity = process_identity(session.pid)
            control_started = monotonic()
            accepted = session.interrupt()
            prompt_recovered = False
            shell_rebuilt = False
            cleanup: dict[str, object] | None = None
            probe_seen = False
            try:
                session.wait_for_text(
                    session.prompt_marker,
                    ticket.cursor,
                    SOFT_INTERRUPT_SECONDS,
                )
                remaining = CONTROL_DEADLINE_SECONDS - (monotonic() - control_started)
                if remaining > 0:
                    probe = f"INTERRUPT_PROBE_{uuid.uuid4().hex}"
                    result = session.run_script(
                        f"[Console]::Out.WriteLine({powershell_literal(probe)})",
                        remaining,
                    )
                    probe_seen = probe in result.output
                    prompt_recovered = probe_seen
            except (EOFError, RuntimeError, TimeoutError):
                remaining = max(0.0, CONTROL_DEADLINE_SECONDS - (monotonic() - control_started))
                cleanup = taskkill_tree(
                    shell_identity,
                    force=True,
                    timeout_seconds=remaining,
                )
                with suppress(Exception):
                    session.cancel_reader()
                remaining = CONTROL_DEADLINE_SECONDS - (monotonic() - control_started)
                if remaining > 0 and bool(cleanup["all_exited"]):
                    rebuilt = ConPtySession(pwsh)
                    rebuilt.start(timeout_seconds=remaining)
                    remaining = CONTROL_DEADLINE_SECONDS - (monotonic() - control_started)
                    if remaining > 0:
                        probe = f"REBUILT_INTERRUPT_{uuid.uuid4().hex}"
                        result = rebuilt.run_script(
                            f"[Console]::Out.WriteLine({powershell_literal(probe)})",
                            remaining,
                        )
                        probe_seen = probe in result.output
                        shell_rebuilt = probe_seen
            control_ms = round((monotonic() - control_started) * 1000)
            if rebuilt is not None:
                _close_session_verified(rebuilt)
                rebuilt = None
            _close_session_verified(session)
            session_closed = True
            log.add(
                "interrupt_recovery",
                iteration,
                accepted > 0 and control_ms <= 3000 and (prompt_recovered or shell_rebuilt),
                round((monotonic() - started) * 1000),
                {
                    "accepted_characters": accepted,
                    "control_ms": control_ms,
                    "prompt_recovered": prompt_recovered,
                    "shell_rebuilt": shell_rebuilt,
                    "probe_seen": probe_seen,
                    "cleanup": cleanup,
                },
            )
        except Exception as exc:
            log.failed("interrupt_recovery", iteration, started, exc)
            _safe_kill_session(session)
            if rebuilt is not None:
                _safe_kill_session(rebuilt)
        finally:
            if not session_closed:
                _safe_kill_session(session)


def _attempt_eof(pwsh: str, control: str) -> tuple[bool, dict[str, object]]:
    session = ConPtySession(pwsh)
    passed = False
    details: dict[str, object]
    try:
        session.start()
        ready = f"READY_{uuid.uuid4().hex}"
        done = f"EOF_{uuid.uuid4().hex}="
        source = (
            "import sys\n"
            f"print({ready!r}, flush=True)\n"
            "data=sys.stdin.buffer.read()\n"
            f"print({done!r}+str(len(data)), flush=True)"
        )
        ticket = session.start_script(_python_command(source))
        session.wait_for_text(ready, ticket.cursor, 5.0)
        session.write(control)
        result = session.await_script(ticket, CONTROL_DEADLINE_SECONDS)
        probe = f"PROBE_{uuid.uuid4().hex}"
        post = session.run_script(f"[Console]::Out.WriteLine({powershell_literal(probe)})")
        passed = done + "0" in result.output and probe in post.output
        details = {
            "control_hex": control.encode("utf-8").hex(),
            "stdin_bytes": 0 if done + "0" in result.output else None,
            "shell_probe_seen": probe in post.output,
        }
    except Exception as exc:
        with suppress(Exception):
            session.interrupt()
        details = {"control_hex": control.encode("utf-8").hex(), **_exception_details(exc)}
    try:
        _close_session_verified(session)
    except Exception as close_error:
        passed = False
        details["close_failure"] = _exception_details(close_error)
    return passed, details


def run_eof_gate(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    candidates = ("\x1a\r\n", "\x04", "\x1a")
    selected: str | None = None
    probes: list[dict[str, object]] = []
    for candidate in candidates:
        passed, details = _attempt_eof(pwsh, candidate)
        details["passed"] = passed
        probes.append(details)
        if passed:
            selected = candidate
            break

    for iteration in range(1, repetitions + 1):
        started = monotonic()
        if selected is None:
            log.add(
                "eof_preserves_shell",
                iteration,
                False,
                0,
                {"candidate_probes": probes, "selected": None},
            )
            continue
        passed, details = _attempt_eof(pwsh, selected)
        details["selected_control_hex"] = selected.encode("utf-8").hex()
        if iteration == 1:
            details["candidate_probes"] = probes
        log.add(
            "eof_preserves_shell",
            iteration,
            passed,
            round((monotonic() - started) * 1000),
            details,
        )


def run_timeout_rebuild_gate(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    for iteration in range(1, repetitions + 1):
        started = monotonic()
        session = ConPtySession(pwsh)
        session_closed = False
        try:
            session.start()
            ready = f"READY_{uuid.uuid4().hex}="
            source = (
                "import os,signal,threading\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                f"print({ready!r}+str(os.getpid()), flush=True)\n"
                "threading.Event().wait(60)"
            )
            ticket = session.start_script(_python_command(source))
            output = session.wait_for_text(ready, ticket.cursor, 5.0)
            match = re.search(re.escape(ready) + r"(\d+)", output)
            if match is None:
                raise RuntimeError("timeout fixture PID marker was missing")
            child_identity = process_identity(int(match.group(1)))
            session.interrupt()
            soft_recovered = True
            try:
                session.await_script(ticket, 0.5)
            except TimeoutError:
                soft_recovered = False
            shell_identity = process_identity(session.pid)
            cleanup = taskkill_tree(shell_identity, force=True, timeout_seconds=5.0)
            session.cancel_reader()
            _close_session_verified(session)
            session_closed = True

            rebuilt = ConPtySession(pwsh)
            try:
                rebuilt.start()
                token = f"REBUILT_{uuid.uuid4().hex}"
                probe = rebuilt.run_script(f"[Console]::Out.WriteLine({powershell_literal(token)})")
                rebuilt_ok = token in probe.output
            finally:
                _close_session_verified(rebuilt)
            passed = (
                not soft_recovered
                and bool(cleanup["all_exited"])
                and not identity_is_alive(child_identity)
                and rebuilt_ok
            )
            log.add(
                "timeout_rebuild",
                iteration,
                passed,
                round((monotonic() - started) * 1000),
                {
                    "soft_recovered": soft_recovered,
                    "cleanup": cleanup,
                    "child_alive": identity_is_alive(child_identity),
                    "rebuilt_probe": rebuilt_ok,
                },
            )
        except Exception as exc:
            log.failed("timeout_rebuild", iteration, started, exc)
            _safe_kill_session(session)
        finally:
            if not session_closed:
                _safe_kill_session(session)


def _tree_command(reporter: TreeReporter, event_name: str) -> str:
    return " ".join(
        (
            "&",
            powershell_literal(sys.executable),
            "-u",
            powershell_literal(str(TREE_FIXTURE)),
            "--role parent",
            "--host",
            powershell_literal(reporter.host),
            "--port",
            str(reporter.port),
            "--event",
            powershell_literal(event_name),
        )
    )


def _reported_identities(
    reports: Iterable[dict[str, object]],
) -> tuple[dict[str, ProcessIdentity], bool]:
    by_role: dict[str, dict[str, object]] = {str(report["role"]): report for report in reports}
    if set(by_role) != {"parent", "child", "grandchild"}:
        raise RuntimeError(f"unexpected process-tree roles: {sorted(by_role)}")
    identities = {role: process_identity(int(report["pid"])) for role, report in by_role.items()}
    parent_descendants = {identity.pid for identity in descendant_identities(identities["parent"])}
    child_descendants = {identity.pid for identity in descendant_identities(identities["child"])}
    hierarchy = (
        identities["child"].pid in parent_descendants
        and identities["grandchild"].pid in parent_descendants
        and identities["grandchild"].pid in child_descendants
    )
    return identities, hierarchy


def _rebuild_probe(pwsh: str, deadline: float) -> bool:
    remaining = deadline - monotonic()
    if remaining <= 0:
        return False
    rebuilt = ConPtySession(pwsh)
    try:
        rebuilt.start(timeout_seconds=remaining)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        token = f"LIFECYCLE_REBUILT_{uuid.uuid4().hex}"
        result = rebuilt.run_script(
            f"[Console]::Out.WriteLine({powershell_literal(token)})",
            remaining,
        )
        return result.exit_code == 0 and _normalized_output(result.output) == token + "\n"
    finally:
        _close_session_verified(rebuilt)


def _run_toolhelp_tree_once(pwsh: str, action: str) -> dict[str, object]:
    session = ConPtySession(pwsh)
    session_closed = False
    with (
        TreeReporter() as reporter,
        NamedManualResetEvent(f"Local\\tfbash-phase0-{uuid.uuid4().hex}") as release,
    ):
        try:
            session.start()
            shell_identity = process_identity(session.pid)
            session.start_script(_tree_command(reporter, release.name))
            reports = reporter.collect(3, 10.0)
            identities, hierarchy = _reported_identities(reports)
            discovered = descendant_identities(shell_identity)
            reported_descendants = {identity.pid for identity in identities.values()}
            toolhelp_pids = {identity.pid for identity in discovered}
            detected = reported_descendants <= toolhelp_pids

            control_started = monotonic()
            deadline = control_started + LIFECYCLE_DEADLINE_SECONDS
            force = action != "terminate"
            result = taskkill_tree(
                shell_identity,
                force=force,
                timeout_seconds=max(0.001, deadline - monotonic()),
            )
            escalated = False
            if not bool(result["all_exited"]) and monotonic() < deadline:
                escalated = True
                result = taskkill_tree(
                    shell_identity,
                    force=True,
                    timeout_seconds=max(0.001, deadline - monotonic()),
                )
            all_identities = (shell_identity, *identities.values())
            all_exited = wait_for_exit(
                all_identities,
                max(0.0, deadline - monotonic()),
            )
            session.cancel_reader()
            _close_session_verified(session)
            session_closed = True
            survivors = [identity.pid for identity in all_identities if identity_is_alive(identity)]
            expected_rebuild = action in {"terminate", "kill"}
            shell_rebuilt = _rebuild_probe(pwsh, deadline) if expected_rebuild else False
            control_ms = round((monotonic() - control_started) * 1000)
            return {
                "passed": (
                    hierarchy
                    and detected
                    and all_exited
                    and not survivors
                    and shell_rebuilt == expected_rebuild
                ),
                "hierarchy_valid": hierarchy,
                "toolhelp_detected_reported_tree": detected,
                "toolhelp_descendants": sorted(toolhelp_pids),
                "reported_pids": {role: identity.pid for role, identity in identities.items()},
                "shell_pid": shell_identity.pid,
                "escalated": escalated,
                "taskkill": result,
                "all_exited": all_exited,
                "survivors": survivors,
                "expected_rebuild": expected_rebuild,
                "shell_rebuilt": shell_rebuilt,
                "session_closed_before_evaluation": session_closed,
                "control_ms": control_ms,
            }
        finally:
            release.set()
            if not session_closed:
                try:
                    session.close()
                except Exception:
                    _safe_kill_session(session)


def _run_job_tree_once(pwsh: str, action: str) -> dict[str, object]:
    job_name = f"Local\\tfbash-phase0-job-{uuid.uuid4().hex}"
    with (
        KillOnCloseJob(job_name) as job,
        TreeReporter() as reporter,
        NamedManualResetEvent(f"Local\\tfbash-phase0-{uuid.uuid4().hex}") as release,
    ):
        assigned: dict[str, ProcessIdentity] = {}

        def assign(pid: int) -> None:
            assigned["shell"] = job.assign_pid(pid)

        session = ConPtySession(pwsh, before_bootstrap=assign)
        session_closed = False
        try:
            session.start()
            session.start_script(_tree_command(reporter, release.name))
            reports = reporter.collect(3, 10.0)
            identities, hierarchy = _reported_identities(reports)
            all_identities = (assigned["shell"], *identities.values())

            control_started = monotonic()
            deadline = control_started + LIFECYCLE_DEADLINE_SECONDS
            if action in {"close", "shutdown"}:
                job.close()
            else:
                job.terminate(137 if action == "kill" else 143)
            all_exited = wait_for_exit(
                all_identities,
                max(0.0, deadline - monotonic()),
            )
            session.cancel_reader()
            _close_session_verified(session)
            session_closed = True
            survivors = [identity.pid for identity in all_identities if identity_is_alive(identity)]
            expected_rebuild = action in {"terminate", "kill"}
            shell_rebuilt = _rebuild_probe(pwsh, deadline) if expected_rebuild else False
            control_ms = round((monotonic() - control_started) * 1000)
            return {
                "passed": (
                    hierarchy and all_exited and not survivors and shell_rebuilt == expected_rebuild
                ),
                "hierarchy_valid": hierarchy,
                "reported_pids": {role: identity.pid for role, identity in identities.items()},
                "shell_pid": assigned["shell"].pid,
                "all_exited": all_exited,
                "survivors": survivors,
                "expected_rebuild": expected_rebuild,
                "shell_rebuilt": shell_rebuilt,
                "session_closed_before_evaluation": session_closed,
                "control_ms": control_ms,
                "ownership_boundary": (
                    "processes explicitly breaking away from the Job are unsupported"
                ),
            }
        finally:
            release.set()
            if not session_closed:
                try:
                    session.close()
                except Exception:
                    _safe_kill_session(session)


def run_tree_gates(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    candidates: tuple[tuple[str, Callable[[str, str], dict[str, object]]], ...] = (
        ("toolhelp", _run_toolhelp_tree_once),
        ("job", _run_job_tree_once),
    )
    for candidate, operation in candidates:
        for action in ("terminate", "kill", "close", "shutdown"):
            scenario = f"{candidate}_{action}_tree"
            for iteration in range(1, repetitions + 1):
                started = monotonic()
                try:
                    details = operation(pwsh, action)
                    passed = bool(details.pop("passed"))
                    log.add(
                        scenario,
                        iteration,
                        passed,
                        round((monotonic() - started) * 1000),
                        details,
                    )
                except Exception as exc:
                    log.failed(scenario, iteration, started, exc)


def collect_environment(pwsh: str, tier: EnvironmentTier, runner_commit: str) -> dict[str, object]:
    command = " ".join(
        (
            "$os = Get-CimInstance Win32_OperatingSystem;",
            "[ordered]@{",
            "Caption=$os.Caption; Version=$os.Version; OSArchitecture=$os.OSArchitecture;",
            "ProductType=$os.ProductType; PowerShell=$PSVersionTable.PSVersion.ToString();",
            "RuntimeOSArchitecture=[Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString();",
            "RuntimeProcessArchitecture=[Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()",
            "} | ConvertTo-Json -Compress",
        )
    )
    completed = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=True,
    )
    windows = json.loads(completed.stdout.strip())
    if not isinstance(windows, dict):
        raise RuntimeError("PowerShell environment probe did not return an object")
    python_version = platform.python_version()
    python_architecture = platform.machine()
    pywinpty_version = package_version("pywinpty")
    uv_version = _command_version("uv")
    environment = {
        "environment_tier": tier.value,
        "python": python_version,
        "python_architecture": python_architecture,
        "pywinpty": pywinpty_version,
        "uv": uv_version,
        "platform": platform.platform(),
        "runner_commit": runner_commit,
        "runner_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "windows": windows,
    }
    validate_environment(windows, tier)
    validate_toolchain(
        python_version=python_version,
        python_architecture=python_architecture,
        pywinpty_version=pywinpty_version,
        uv_version=uv_version,
    )
    return environment


def write_summary(
    output_dir: Path,
    tier: EnvironmentTier,
    environment: dict[str, object],
    observations: list[Observation],
) -> None:
    summary = evaluate_gates(observations, tier)
    payload = summary_payload(summary)
    payload["environment"] = environment
    evidence_files = {
        "environment.json": _file_sha256(output_dir / "environment.json"),
        "observations.jsonl": _file_sha256(output_dir / "observations.jsonl"),
    }
    payload["evidence_files_sha256"] = evidence_files
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Windows Phase 0 experiment summary",
        "",
        f"- Environment tier: `{tier.value}`",
        f"- Evidence complete: `{summary.evidence_complete}`",
        f"- Contract passed: `{summary.contract_passed}`",
        f"- Decision ready: `{summary.decision_ready}`",
        f"- Decision: `{summary.decision.value}`",
        f"- environment.json SHA-256: `{evidence_files['environment.json']}`",
        f"- observations.jsonl SHA-256: `{evidence_files['observations.jsonl']}`",
        "",
        "| Scenario | Passed | Failed | Skipped | Required | Accepted |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for gate in summary.gates:
        lines.append(
            f"| {gate.scenario} | {gate.passed} | {gate.failed} | {gate.skipped} | "
            f"{gate.required_passes}/{gate.required_runs} | {gate.accepted} |"
        )
    lines.extend(
        (
            "",
            "> Hosted Windows is smoke/repetition evidence only. `decision_ready` can become true ",
            "> only for a complete native Windows 11 x64 run.",
            "",
        )
    )
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pwsh",
        default=shutil.which("pwsh"),
        help="Path to PowerShell 7.6.x (defaults to pwsh on PATH)",
    )
    parser.add_argument(
        "--environment-tier",
        choices=tuple(tier.value for tier in EnvironmentTier),
        required=True,
    )
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument(
        "--runner-commit",
        help="Exact source commit; defaults to GITHUB_SHA or git rev-parse HEAD",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/windows-phase0"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows Phase 0 must run on Windows")
    if args.pwsh is None:
        raise RuntimeError("PowerShell 7.6.x was not found")
    if args.repetitions <= 0:
        raise ValueError("repetitions must be positive")
    tier = EnvironmentTier(args.environment_tier)
    output_dir: Path = args.output_dir.resolve()
    prepare_output_directory(output_dir)
    environment = collect_environment(
        str(args.pwsh),
        tier,
        _resolve_runner_commit(args.runner_commit),
    )
    (output_dir / "environment.json").write_text(
        json.dumps(environment, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    raw_path = output_dir / "observations.jsonl"
    with JsonlRecorder(raw_path) as recorder:
        log = ExperimentLog(recorder)
        run_basic_contracts(log, str(args.pwsh))
        run_tail_and_terminal_gates(log, str(args.pwsh), args.repetitions)
        run_interrupt_gate(log, str(args.pwsh), args.repetitions)
        run_eof_gate(log, str(args.pwsh), args.repetitions)
        run_timeout_rebuild_gate(log, str(args.pwsh), args.repetitions)
        run_tree_gates(log, str(args.pwsh), args.repetitions)

    write_summary(output_dir, tier, environment, log.observations)
    summary = evaluate_gates(log.observations, tier)
    return 0 if summary.contract_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
