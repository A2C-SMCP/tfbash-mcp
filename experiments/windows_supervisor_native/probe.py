"""Run the #15 supervisor candidate against real Windows Job and ConPTY APIs."""

from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from experiments.windows_supervisor_native.contracts import (
    NATIVE_GATE_REPETITIONS,
    REQUIRED_CHECKS,
    SCHEMA,
    SSH_SMOKE_MAX_REPETITIONS,
    evaluate_evidence,
)

_PID_PATTERN = re.compile(rb"TFBASH_GRANDCHILD_PID=(\d+)")


class ProbeError(RuntimeError):
    """A native observation could not be completed safely."""


def _write_all(transport: Any, session: Any, payload: bytes, deadline: float) -> None:
    from tfbash_mcp.runtime import WaitInterest

    offset = 0
    while offset < len(payload):
        if time.monotonic() >= deadline:
            raise ProbeError("ConPTY write deadline expired")
        result = transport.write(session, memoryview(payload)[offset:])
        offset += result.bytes_written
        if offset < len(payload):
            transport.wait(session, frozenset({WaitInterest.WRITABLE}), 50)


def _read_until(
    transport: Any,
    session: Any,
    predicate: Any,
    *,
    deadline: float,
) -> bytes:
    from tfbash_mcp.runtime import ReadStatus, WaitInterest

    output = bytearray()
    while time.monotonic() < deadline:
        transport.wait(
            session,
            frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
            50,
        )
        while True:
            result = transport.read(session, 65_536)
            if result.status is ReadStatus.DATA:
                output.extend(result.data)
                if predicate(bytes(output)):
                    return bytes(output)
                continue
            if result.status is ReadStatus.EOF:
                raise ProbeError("ConPTY reached EOF before the expected evidence marker")
            break
    raise ProbeError("ConPTY read deadline expired")


def _await_ready(transport: Any, session: Any, plan: Any, deadline: float) -> None:
    from tfbash_mcp.runtime import DialectEventKind

    _write_all(transport, session, plan.launch.initial_input, deadline)

    def ready(data: bytes) -> bool:
        return any(event.kind is DialectEventKind.READY for event in plan.protocol.feed(data))

    # Feed only each newly read chunk to the stateful protocol.
    from tfbash_mcp.runtime import ReadStatus, WaitInterest

    while time.monotonic() < deadline:
        transport.wait(session, frozenset({WaitInterest.READABLE}), 50)
        result = transport.read(session, 65_536)
        if result.status is ReadStatus.DATA and ready(result.data):
            return
        if result.status is ReadStatus.EOF:
            raise ProbeError("PowerShell exited before its ready record")
    raise ProbeError("PowerShell ready deadline expired")


def _protocol_command(
    transport: Any,
    session: Any,
    protocol: Any,
    command: str,
    deadline: float,
) -> tuple[bytes, int]:
    from tfbash_mcp.runtime import DialectEventKind, ReadStatus, WaitInterest

    frame = protocol.wrap_command(command)
    _write_all(transport, session, frame.input_bytes, deadline)
    output = bytearray()
    exit_code: int | None = None
    while time.monotonic() < deadline and exit_code is None:
        transport.wait(session, frozenset({WaitInterest.READABLE}), 50)
        result = transport.read(session, 65_536)
        if result.status is ReadStatus.EOF:
            raise ProbeError("PowerShell exited before command completion")
        if result.status is not ReadStatus.DATA:
            continue
        for event in protocol.feed(result.data):
            if event.kind is DialectEventKind.OUTPUT:
                output.extend(event.data)
            if (
                event.kind is DialectEventKind.COMMAND_COMPLETE
                and event.correlation_id == frame.correlation_id
            ):
                exit_code = event.exit_code
    if exit_code is None:
        raise ProbeError("PowerShell command completion deadline expired")
    finalization = protocol.begin_finalization()
    _write_all(transport, session, finalization.input_bytes, deadline)
    finalized = False
    while time.monotonic() < deadline and not finalized:
        transport.wait(session, frozenset({WaitInterest.READABLE}), 50)
        result = transport.read(session, 65_536)
        if result.status is ReadStatus.EOF:
            raise ProbeError("PowerShell exited before command finalization")
        if result.status is not ReadStatus.DATA:
            continue
        for event in protocol.feed(result.data):
            if event.kind is DialectEventKind.OUTPUT:
                output.extend(event.data)
            if (
                event.kind is DialectEventKind.FINALIZED
                and event.correlation_id == finalization.correlation_id
            ):
                finalized = True
    if not finalized:
        raise ProbeError("PowerShell finalization deadline expired")
    return bytes(output), exit_code


def _job_ids(ownership: Any) -> tuple[int, ...]:
    job = ownership._job
    if job is None:
        return ()
    return tuple(ownership._api.job_process_ids(job, deadline=time.monotonic() + 2))


def _member(ownership: Any, process_id: int) -> bool:
    process = ownership._api.open_process_if_alive(process_id)
    if process is None:
        return False
    try:
        return bool(ownership._api.process_is_in_job(ownership._job, process))
    finally:
        ownership._api.close_process(process)


def _process_is_dead(ownership: Any, process_id: int) -> bool:
    process = ownership._api.open_process_if_alive(process_id)
    if process is None:
        return True
    ownership._api.close_process(process)
    return False


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _write_marker_script(marker: str) -> str:
    encoded = base64.b64encode(marker.encode()).decode("ascii")
    return (
        "[Console]::Out.WriteLine([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{encoded}')))"
    )


def _run_iteration(iteration: int, pwsh: str) -> dict[str, object]:
    from tfbash_mcp.runtime import (
        ConPtyTransport,
        ControlIntent,
        PowerShellDialect,
        ShellStartRequest,
        WindowsProcessSupervisor,
        WindowsPwshProfile,
    )

    started = time.monotonic()
    checks = {name: False for name in REQUIRED_CHECKS}
    diagnostics: dict[str, object] = {}
    transport = ConPtyTransport(close_timeout_ms=5_000)
    supervisor = WindowsProcessSupervisor(
        python_executable=sys.executable,
        gate_wait_timeout_ms=10_000,
        shell_ready_timeout_ms=5_000,
    )
    dialect = PowerShellDialect()
    profile = WindowsPwshProfile(
        dialect=dialect,
        transport=transport,
        supervisor=supervisor,
    )
    plan = dialect.prepare_session(
        ShellStartRequest(
            executable=pwsh,
            cwd=str(Path.cwd()),
            environment=dict(os.environ),
            startup_command=None,
        )
    )
    managed: Any = None
    tracked_pids: tuple[int, ...] = ()
    try:
        managed = profile.open_session(
            plan.launch.spawn,
            cleanup_deadline_ms=5_000,
            startup_deadline_ms=10_000,
        )
        ownership = managed.ownership
        _await_ready(transport, managed.session, plan, time.monotonic() + 10)
        bootstrap_pid = ownership._bootstrap.identity.process_id
        shell_pid = ownership._root.identity.process_id
        initial_ids = _job_ids(ownership)
        diagnostics.update(
            bootstrap_pid=bootstrap_pid,
            shell_pid=shell_pid,
            initial_job_pids=list(initial_ids),
        )
        checks["bootstrap_in_job"] = bootstrap_pid in initial_ids and _member(
            ownership, bootstrap_pid
        )
        checks["shell_in_job"] = shell_pid in initial_ids and _member(ownership, shell_pid)

        grandchild_script = "Start-Sleep -Seconds 60"
        encoded_grandchild = _encoded(grandchild_script)
        child_script = (
            f"$g=Start-Process -FilePath {_powershell_literal(pwsh)} "
            "-ArgumentList '-NoLogo','-NoProfile','-EncodedCommand',"
            f"'{encoded_grandchild}' "
            "-PassThru;[Console]::Out.WriteLine('TFBASH_GRANDCHILD_PID='+$g.Id);"
            "Start-Sleep -Milliseconds 150"
        )
        command = (
            f"& {_powershell_literal(pwsh)} -NoLogo -NoProfile "
            f"-EncodedCommand {_encoded(child_script)}\r\n"
        )
        _write_all(transport, managed.session, command.encode(), time.monotonic() + 5)
        child_output = _read_until(
            transport,
            managed.session,
            lambda value: _PID_PATTERN.search(value) is not None,
            deadline=time.monotonic() + 10,
        )
        match = _PID_PATTERN.search(child_output)
        if match is None:
            raise ProbeError("grandchild PID marker was not observed")
        grandchild_pid = int(match.group(1))
        diagnostics["grandchild_pid"] = grandchild_pid
        checks["grandchild_in_job"] = grandchild_pid in _job_ids(ownership) and _member(
            ownership, grandchild_pid
        )
        cleanup = supervisor.cleanup_execution(ownership, deadline_ms=5_000)
        checks["execution_cleanup_zero_descendants"] = (
            cleanup.reaped and cleanup.remaining_managed_processes == 0
        )
        checks["shell_survived_execution_cleanup"] = supervisor.is_alive(ownership)

        interrupt_start = f"TFBASH_INTERRUPT_START_{iteration}"
        interrupt_recovered = f"TFBASH_INTERRUPT_RECOVERED_{iteration}"
        interrupt_command = (
            f"{_write_marker_script(interrupt_start)};"
            "Start-Sleep -Seconds 60;[Console]::Out.WriteLine('INTERRUPT_MISSED')\r\n"
        )
        _write_all(
            transport,
            managed.session,
            interrupt_command.encode(),
            time.monotonic() + 5,
        )
        _read_until(
            transport,
            managed.session,
            lambda value: interrupt_start.encode() in value,
            deadline=time.monotonic() + 5,
        )
        delivery = supervisor.control(ownership, ControlIntent.INTERRUPT, deadline_ms=2_000)
        checks["interrupt_delivered"] = delivery.delivered
        recovery_command = f"{_write_marker_script(interrupt_recovered)}\r\n".encode()
        _write_all(transport, managed.session, recovery_command, time.monotonic() + 5)
        recovered_output = _read_until(
            transport,
            managed.session,
            lambda value: interrupt_recovered.encode() in value,
            deadline=time.monotonic() + 10,
        )
        checks["shell_recovered_after_interrupt"] = (
            interrupt_recovered.encode() in recovered_output and supervisor.is_alive(ownership)
        )

        tail_marker = f"TFBASH_TAIL_{iteration}".encode()
        tail_output, exit_code = _protocol_command(
            transport,
            managed.session,
            plan.protocol,
            f"[Console]::Out.WriteLine('{tail_marker.decode()}');& cmd.exe /d /c exit 37",
            time.monotonic() + 10,
        )
        checks["tail_output_preserved"] = tail_marker in tail_output
        checks["exit_code_preserved"] = exit_code == 37
        tracked_pids = _job_ids(ownership)
        transport.close(managed.session, deadline_ms=5_000)
        cleanup = supervisor.cleanup(ownership, deadline_ms=5_000)
        checks["shell_cleanup_zero_residue"] = cleanup.reaped and all(
            _process_is_dead(ownership, process_id) for process_id in tracked_pids
        )
    except Exception as error:
        diagnostics["error"] = f"{type(error).__name__}: {error}"
    finally:
        if managed is not None:
            with suppress(Exception):
                transport.close(managed.session, deadline_ms=2_000)
            try:
                supervisor.cleanup(managed.ownership, deadline_ms=5_000)
            except Exception as error:
                diagnostics.setdefault("cleanup_error", f"{type(error).__name__}: {error}")
    return {
        "iteration": iteration,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "checks": checks,
        "passed": all(checks.values()),
        "diagnostics": diagnostics,
    }


def _environment(pwsh: str, source_commit: str) -> dict[str, object]:
    if os.name != "nt":
        raise ProbeError("the supervisor native probe requires Windows")
    product_type = subprocess.check_output(
        [pwsh, "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).ProductType"],
        text=True,
    ).strip()
    build = int(
        subprocess.check_output(
            [pwsh, "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).BuildNumber"],
            text=True,
        ).strip()
    )
    pwsh_version = subprocess.check_output(
        [pwsh, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
        text=True,
    ).strip()
    return {
        "windows_client": product_type == "1",
        "windows_11": build >= 22000,
        "windows_build": build,
        "os_x64": platform.machine().upper() in {"AMD64", "X86_64"},
        "python_x64": sys.maxsize > 2**32,
        "python_version": platform.python_version(),
        "powershell_version": pwsh_version,
        "pywinpty_version": importlib.metadata.version("pywinpty"),
        "source_commit": source_commit,
    }


def run_probe(
    *,
    evidence_tier: str,
    repetitions: int,
    pwsh: str,
    source_commit: str,
) -> dict[str, object]:
    if evidence_tier == "hosted-smoke" and not 1 <= repetitions <= SSH_SMOKE_MAX_REPETITIONS:
        raise ProbeError("SSH smoke repetitions must be between 1 and 5")
    if evidence_tier == "native-gate" and repetitions != NATIVE_GATE_REPETITIONS:
        raise ProbeError("the native supervisor gate requires exactly 20 repetitions")
    if evidence_tier not in {"hosted-smoke", "native-gate"}:
        raise ProbeError("unknown evidence tier")
    environment = _environment(pwsh, source_commit)
    iterations = [_run_iteration(iteration, pwsh) for iteration in range(1, repetitions + 1)]
    passed = sum(item["passed"] is True for item in iterations)
    contract_passed = passed == repetitions
    decision_ready = evidence_tier == "native-gate" and contract_passed
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "evidence_tier": evidence_tier,
        "repetitions": repetitions,
        "environment": environment,
        "iterations": iterations,
        "summary": {
            "passed_iterations": passed,
            "contract_passed": contract_passed,
            "decision_ready": decision_ready,
            "decision": "pass" if decision_ready else "inconclusive",
        },
    }
    evaluate_evidence(payload)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-tier", choices=("hosted-smoke", "native-gate"), required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--pwsh", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_probe(
        evidence_tier=args.evidence_tier,
        repetitions=args.repetitions,
        pwsh=args.pwsh,
        source_commit=args.source_commit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], sort_keys=True))
    return 0 if cast(Mapping[str, object], payload["summary"])["contract_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
