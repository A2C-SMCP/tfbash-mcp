"""Execute the pre-registered Windows V1 Phase 0 experiment.

Run with ``uv run experiments/windows_phase0/runner.py --help`` on Windows.
The script intentionally uses pywinpty's low-level PTY API rather than the
high-level PtyProcess compatibility wrapper, whose fixed sleeps would pollute
the event-driven transport and lifecycle observations.
"""

from __future__ import annotations

import argparse
import base64
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
from pathlib import Path
from time import monotonic

import winpty

from experiments.windows_phase0.conpty_session import ConPtySession, powershell_literal
from experiments.windows_phase0.contracts import (
    EnvironmentTier,
    JsonlRecorder,
    Observation,
    Outcome,
    evaluate_gates,
    summary_payload,
)
from experiments.windows_phase0.windows_api import (
    KillOnCloseJob,
    NamedManualResetEvent,
    ProcessIdentity,
    descendant_identities,
    identity_is_alive,
    process_identity,
    taskkill_tree,
    wait_for_exit,
)

EXPERIMENT_DIR = Path(__file__).resolve().parent
TREE_FIXTURE = EXPERIMENT_DIR / "tree_fixture.py"
CONTROL_DEADLINE_SECONDS = 3.0
TREE_DEADLINE_SECONDS = 10.0
TAIL_BYTES = 262_144


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
    try:
        identity = process_identity(session.pid)
    except (OSError, RuntimeError):
        return
    with suppress(OSError):
        taskkill_tree(identity, force=True, timeout_seconds=5.0)
    with suppress(Exception):
        session.cancel_reader()


def _with_session(
    operation: Callable[[ConPtySession], dict[str, object]], pwsh: str
) -> dict[str, object]:
    session = ConPtySession(pwsh)
    try:
        session.start()
        return operation(session)
    finally:
        try:
            session.close()
        except Exception:
            _safe_kill_session(session)


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
        "passed": expected in result.output,
        "expected": expected,
        "exit_code": result.exit_code,
    }


def _persistent_state_scenario(session: ConPtySession) -> dict[str, object]:
    token = uuid.uuid4().hex
    session.run_script(
        f"$env:TF_PHASE0_STATE = {powershell_literal(token)}; Set-Location $env:TEMP"
    )
    result = session.run_script(
        "[Console]::Out.WriteLine('STATE=' + $env:TF_PHASE0_STATE); "
        "[Console]::Out.WriteLine('CWD=' + (Get-Location).Path)"
    )
    return {
        "passed": f"STATE={token}" in result.output and "CWD=" in result.output,
        "state_seen": f"STATE={token}" in result.output,
        "cwd_seen": "CWD=" in result.output,
    }


def _multiline_scenario(session: ConPtySession) -> dict[str, object]:
    expected = "line-1|line-2|line-3"
    result = session.run_script(
        "$values = @(\n  'line-1'\n  'line-2'\n  'line-3'\n)\n"
        "[Console]::Out.WriteLine(($values -join '|'))"
    )
    return {"passed": expected in result.output, "expected": expected}


def _exit_code_scenario(session: ConPtySession) -> dict[str, object]:
    result = session.run_script("& $env:ComSpec /d /c exit 37")
    return {"passed": result.exit_code == 37, "observed_exit_code": result.exit_code}


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
    return {"passed": done + encoded in result.output, "expected_base64": encoded}


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
    return {
        "passed": done + expected in result.output,
        "expected_base64": expected,
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
    write_result: dict[str, object] = {}
    write_done = threading.Event()

    def write_payload() -> None:
        try:
            write_result["accepted_characters"] = session.write("x" * 1_048_576)
        except Exception as exc:
            write_result["write_error"] = repr(exc)
        finally:
            write_done.set()

    writer = threading.Thread(target=write_payload, name="phase0-backpressure-writer", daemon=True)
    writer.start()
    started = monotonic()
    session.interrupt()
    result = session.await_script(ticket, CONTROL_DEADLINE_SECONDS)
    control_ms = round((monotonic() - started) * 1000)
    writer_finished = write_done.wait(1.0)
    return {
        "passed": control_ms <= 3000 and result.exit_code != 0,
        "control_ms": control_ms,
        "writer_finished": writer_finished,
        **write_result,
    }


def run_tail_and_terminal_gates(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    session = ConPtySession(pwsh)
    try:
        session.start()
        previous_tail: str | None = None
        for iteration in range(1, repetitions + 1):
            started = monotonic()
            tail = f"TAIL_{iteration}_{uuid.uuid4().hex}"
            try:
                source = (
                    f"import sys\nsys.stdout.write('x'*{TAIL_BYTES}+{tail!r})\nsys.stdout.flush()"
                )
                ticket = session.start_script(_python_command(source))
                result = session.await_script(ticket, 15.0)
                tail_count = result.output.count(tail)
                log.add(
                    "tail_drain",
                    iteration,
                    tail_count == 1,
                    round((monotonic() - started) * 1000),
                    {"tail_count": tail_count, "output_characters": len(result.output)},
                )

                next_token = f"NEXT_{uuid.uuid4().hex}"
                next_result = session.run_script(
                    f"[Console]::Out.WriteLine({powershell_literal(next_token)})"
                )
                unique = (
                    result.output.count(ticket.end_marker) == 1
                    and next_result.output.count(next_token) == 1
                    and (previous_tail is None or previous_tail not in result.output)
                    and tail not in next_result.output
                )
                log.add(
                    "unique_terminal_state",
                    iteration,
                    unique,
                    round((monotonic() - started) * 1000),
                    {
                        "terminal_marker_count": result.output.count(ticket.end_marker),
                        "next_marker_count": next_result.output.count(next_token),
                        "late_tail_seen": tail in next_result.output,
                    },
                )
                previous_tail = tail
            except Exception as exc:
                log.failed("tail_drain", iteration, started, exc)
                log.failed("unique_terminal_state", iteration, started, exc)
                _safe_kill_session(session)
                return
    finally:
        try:
            session.close()
        except Exception:
            _safe_kill_session(session)


def run_interrupt_gate(log: ExperimentLog, pwsh: str, repetitions: int) -> None:
    for iteration in range(1, repetitions + 1):
        started = monotonic()
        session = ConPtySession(pwsh)
        try:
            session.start()
            ready = f"READY_{uuid.uuid4().hex}"
            source = f"import threading\nprint({ready!r}, flush=True)\nthreading.Event().wait(60)"
            ticket = session.start_script(_python_command(source))
            session.wait_for_text(ready, ticket.cursor, 5.0)
            control_started = monotonic()
            accepted = session.interrupt()
            result = session.await_script(ticket, CONTROL_DEADLINE_SECONDS)
            control_ms = round((monotonic() - control_started) * 1000)
            log.add(
                "interrupt_recovery",
                iteration,
                accepted > 0 and control_ms <= 3000 and result.exit_code != 0,
                round((monotonic() - started) * 1000),
                {"accepted_characters": accepted, "control_ms": control_ms},
            )
        except Exception as exc:
            log.failed("interrupt_recovery", iteration, started, exc)
            _safe_kill_session(session)
        finally:
            try:
                session.close()
            except Exception:
                _safe_kill_session(session)


def _attempt_eof(pwsh: str, control: str) -> tuple[bool, dict[str, object]]:
    session = ConPtySession(pwsh)
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
        return passed, {
            "control_hex": control.encode("utf-8").hex(),
            "stdin_bytes": 0 if done + "0" in result.output else None,
            "shell_probe_seen": probe in post.output,
        }
    except Exception as exc:
        with suppress(Exception):
            session.interrupt()
        return False, {"control_hex": control.encode("utf-8").hex(), **_exception_details(exc)}
    finally:
        try:
            session.close()
        except Exception:
            _safe_kill_session(session)


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

            rebuilt = ConPtySession(pwsh)
            try:
                rebuilt.start()
                token = f"REBUILT_{uuid.uuid4().hex}"
                probe = rebuilt.run_script(f"[Console]::Out.WriteLine({powershell_literal(token)})")
                rebuilt_ok = token in probe.output
            finally:
                rebuilt.close()
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
            with suppress(Exception):
                session.close()


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
    hierarchy = (
        int(by_role["child"]["parent_pid"]) == identities["parent"].pid
        and int(by_role["grandchild"]["parent_pid"]) == identities["child"].pid
    )
    return identities, hierarchy


def _run_toolhelp_tree_once(pwsh: str, action: str) -> dict[str, object]:
    session = ConPtySession(pwsh)
    with (
        TreeReporter() as reporter,
        NamedManualResetEvent(f"Local\\tfbash-phase0-{uuid.uuid4().hex}") as release,
    ):
        try:
            session.start()
            ticket = session.start_script(_tree_command(reporter, release.name))
            reports = reporter.collect(3, 10.0)
            identities, hierarchy = _reported_identities(reports)
            root = identities["parent"]
            discovered = descendant_identities(root)
            reported_descendants = {identities["child"].pid, identities["grandchild"].pid}
            toolhelp_pids = {identity.pid for identity in discovered}
            detected = reported_descendants <= toolhelp_pids

            force = action in {"kill", "close", "shutdown"}
            result = taskkill_tree(root, force=force, timeout_seconds=TREE_DEADLINE_SECONDS)
            escalated = False
            if not bool(result["all_exited"]):
                escalated = True
                result = taskkill_tree(root, force=True, timeout_seconds=TREE_DEADLINE_SECONDS)
            session.await_script(ticket, 5.0)
            all_exited = all(not identity_is_alive(identity) for identity in identities.values())
            return {
                "passed": hierarchy and detected and all_exited and bool(result["all_exited"]),
                "hierarchy_valid": hierarchy,
                "toolhelp_detected_reported_tree": detected,
                "toolhelp_descendants": sorted(toolhelp_pids),
                "reported_pids": {role: identity.pid for role, identity in identities.items()},
                "escalated": escalated,
                "taskkill": result,
            }
        finally:
            release.set()
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
        try:
            session.start()
            ticket = session.start_script(_tree_command(reporter, release.name))
            reports = reporter.collect(3, 10.0)
            identities, hierarchy = _reported_identities(reports)
            all_identities = (assigned["shell"], *identities.values())

            soft_recovered = False
            if action == "terminate":
                session.interrupt()
                try:
                    session.await_script(ticket, 0.5)
                    soft_recovered = True
                except TimeoutError:
                    pass
            if action in {"close", "shutdown"}:
                job.close()
            else:
                job.terminate(137 if action == "kill" else 143)
            all_exited = wait_for_exit(all_identities, TREE_DEADLINE_SECONDS)
            session.cancel_reader()
            survivors = [identity.pid for identity in all_identities if identity_is_alive(identity)]
            return {
                "passed": hierarchy and all_exited and not survivors,
                "hierarchy_valid": hierarchy,
                "reported_pids": {role: identity.pid for role, identity in identities.items()},
                "shell_pid": assigned["shell"].pid,
                "soft_recovered": soft_recovered,
                "all_exited": all_exited,
                "survivors": survivors,
                "ownership_boundary": (
                    "processes explicitly breaking away from the Job are unsupported"
                ),
            }
        finally:
            release.set()
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


def collect_environment(pwsh: str, tier: EnvironmentTier) -> dict[str, object]:
    command = " ".join(
        (
            "$os = Get-CimInstance Win32_OperatingSystem;",
            "[ordered]@{",
            "Caption=$os.Caption; Version=$os.Version; OSArchitecture=$os.OSArchitecture;",
            "ProductType=$os.ProductType; PowerShell=$PSVersionTable.PSVersion.ToString()",
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
    environment = {
        "environment_tier": tier.value,
        "python": platform.python_version(),
        "python_architecture": platform.machine(),
        "pywinpty": winpty.__version__,
        "platform": platform.platform(),
        "windows": windows,
    }
    version = str(windows["PowerShell"])
    architecture = str(windows["OSArchitecture"])
    caption = str(windows["Caption"])
    if not version.startswith("7.6."):
        raise RuntimeError(f"PowerShell 7.6.x is required, observed {version}")
    if "64-bit" not in architecture:
        raise RuntimeError(f"x64 Windows is required, observed {architecture}")
    if tier is EnvironmentTier.NATIVE_GATE and "Windows 11" not in caption:
        raise RuntimeError(f"native gate requires Windows 11, observed {caption}")
    return environment


def write_summary(
    output_dir: Path,
    tier: EnvironmentTier,
    environment: dict[str, object],
    observations: list[Observation],
) -> bool:
    summary = evaluate_gates(observations, tier)
    payload = summary_payload(summary)
    payload["environment"] = environment
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Windows Phase 0 experiment summary",
        "",
        f"- Environment tier: `{tier.value}`",
        f"- All observed gates pass: `{summary.all_observed_gates_pass}`",
        f"- Decision ready: `{summary.decision_ready}`",
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
    return summary.decision_ready


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
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = collect_environment(str(args.pwsh), tier)
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

    decision_ready = write_summary(output_dir, tier, environment, log.observations)
    summary = evaluate_gates(log.observations, tier)
    if tier is EnvironmentTier.HOSTED_SMOKE:
        return 0 if summary.all_observed_gates_pass else 1
    return 0 if decision_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
