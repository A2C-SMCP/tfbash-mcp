from __future__ import annotations

import errno
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, cast

import pexpect  # type: ignore[import-untyped]
import pytest

import tfbash_mcp.runtime.posix_process as posix_process_module
from tfbash_mcp.runtime import (
    BashDialect,
    BashProtocol,
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    PexpectPosixPtyTransport,
    PexpectPosixSession,
    PosixProcessOwnership,
    PosixProcessSupervisor,
    ProcessControlError,
    ReadStatus,
    ShellStartRequest,
    SpawnRequest,
    TransportError,
    WaitInterest,
)


@dataclass
class _BashRuntime:
    supervisor: PosixProcessSupervisor
    transport: PexpectPosixPtyTransport
    ownership: PosixProcessOwnership
    session: PexpectPosixSession
    protocol: BashProtocol

    def close(self) -> None:
        with suppress(ProcessControlError):
            self.supervisor.cleanup(self.ownership, deadline_ms=3000)
        self.transport.close(self.session)


def _token_factory() -> Callable[[], str]:
    sequence = count(1)
    return lambda: f"{next(sequence):032x}"


def _write_all(
    transport: PexpectPosixPtyTransport,
    session: PexpectPosixSession,
    payload: bytes,
) -> None:
    cursor = 0
    deadline = time.monotonic() + 5
    while cursor < len(payload) and time.monotonic() < deadline:
        result = transport.write(session, memoryview(payload)[cursor:])
        cursor += result.bytes_written
        if result.would_block:
            transport.wait(session, frozenset({WaitInterest.WRITABLE}), 100)
    assert cursor == len(payload)


def _await_event(
    runtime: _BashRuntime,
    kind: DialectEventKind,
    *,
    timeout_seconds: float = 5,
) -> DialectEvent:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runtime.transport.wait(
            runtime.session,
            frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
            100,
        )
        result = runtime.transport.read(runtime.session, 65_536)
        if result.status is ReadStatus.EOF:
            raise AssertionError(f"PTY reached EOF before {kind.value}")
        if result.status is not ReadStatus.DATA:
            continue
        for event in runtime.protocol.feed(result.data):
            if event.kind is kind:
                return event
    raise AssertionError(f"did not observe {kind.value} before the deadline")


def _start_bash(tmp_path: Path) -> _BashRuntime:
    plan = BashDialect(token_factory=_token_factory()).prepare_session(
        ShellStartRequest("/bin/bash", str(tmp_path), dict(os.environ), None)
    )
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "test-owner")
    transport = PexpectPosixPtyTransport()
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    session = cast(PexpectPosixSession, transport.spawn(plan.launch.spawn, ownership))
    runtime = _BashRuntime(
        supervisor,
        transport,
        ownership,
        session,
        cast(BashProtocol, plan.protocol),
    )
    _await_event(runtime, DialectEventKind.BOOTSTRAP_REQUIRED)
    _write_all(transport, session, plan.launch.initial_input)
    _await_event(runtime, DialectEventKind.READY)
    return runtime


def _start_command(runtime: _BashRuntime, command: str) -> None:
    frame = runtime.protocol.wrap_command(command)
    _write_all(runtime.transport, runtime.session, frame.input_bytes)


def _execute(runtime: _BashRuntime, command: str) -> DialectEvent:
    _start_command(runtime, command)
    return _await_event(runtime, DialectEventKind.COMMAND_COMPLETE)


def _wait_for_foreground_job(runtime: _BashRuntime) -> int:
    shell_group = runtime.ownership._shell_process_group_id
    terminal_fd = runtime.ownership._terminal_file_descriptor
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        foreground_group = os.tcgetpgrp(terminal_fd)
        if foreground_group != shell_group:
            return foreground_group
        time.sleep(0.01)
    raise AssertionError("Bash command did not establish a foreground process group")


def _wait_for_leaderless_foreground_job(runtime: _BashRuntime) -> int:
    deadline = time.monotonic() + 3
    session_id = cast(int, runtime.ownership._session_id)
    while time.monotonic() < deadline:
        foreground_group = os.tcgetpgrp(runtime.ownership._terminal_file_descriptor)
        records = runtime.supervisor._session_processes(
            session_id,
            timeout_seconds=1,
        )
        members = [
            record
            for record in records
            if record.process_group_id == foreground_group and not record.is_zombie
        ]
        try:
            os.getsid(foreground_group)
        except ProcessLookupError:
            if members:
                return foreground_group
        time.sleep(0.01)
    raise AssertionError("pipeline process-group leader did not exit before its member")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
@pytest.mark.parametrize("intent", list(ControlIntent))
def test_domain_control_targets_real_bash_foreground_group_and_recovers(
    tmp_path: Path,
    intent: ControlIntent,
) -> None:
    runtime = _start_bash(tmp_path)
    try:
        _start_command(runtime, "sleep 30")
        foreground_group = _wait_for_foreground_job(runtime)
        assert foreground_group != runtime.ownership._process_id

        delivery = runtime.supervisor.control(runtime.ownership, intent)

        assert delivery.delivered
        _write_all(runtime.transport, runtime.session, runtime.protocol.recovery_input())
        recovered = _await_event(runtime, DialectEventKind.RECOVERED)
        assert recovered.correlation_id is not None
        assert runtime.supervisor.is_alive(runtime.ownership)
        assert _execute(runtime, "printf still-alive").exit_code == 0
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
@pytest.mark.parametrize("intent", list(ControlIntent))
def test_control_reaches_pipeline_after_process_group_leader_exits(
    tmp_path: Path,
    intent: ControlIntent,
) -> None:
    runtime = _start_bash(tmp_path)
    try:
        _start_command(runtime, "true | sleep 30")
        foreground_group = _wait_for_leaderless_foreground_job(runtime)

        assert runtime.supervisor.control(runtime.ownership, intent).delivered
        assert os.tcgetpgrp(runtime.ownership._terminal_file_descriptor) == foreground_group
        _write_all(runtime.transport, runtime.session, runtime.protocol.recovery_input())
        assert _await_event(runtime, DialectEventKind.RECOVERED).correlation_id is not None
        assert _execute(runtime, "printf pipeline-recovered").exit_code == 0
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
def test_cleanup_reaps_running_stopped_stubborn_and_disowned_jobs(
    tmp_path: Path,
) -> None:
    runtime = _start_bash(tmp_path)
    stubborn_ready = tmp_path / "stubborn-ready"
    stubborn_script = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(stubborn_ready)!r}).write_text('ready'); "
        "time.sleep(30)"
    )
    stopped_ready = tmp_path / "stopped-ready"
    stopped_script = (
        "import os,pathlib,signal,time; "
        f"pathlib.Path({str(stopped_ready)!r}).write_text('ready'); "
        "os.kill(os.getpid(),signal.SIGSTOP); "
        "time.sleep(30)"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(stubborn_script)} & disown; "
        "sleep 30 & disown; "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(stopped_script)} & disown; "
        f"for _ in {{1..200}}; do test -s {shlex.quote(str(stubborn_ready))} "
        f"&& test -s {shlex.quote(str(stopped_ready))} && break; "
        "sleep 0.01; done; "
        f"test -s {shlex.quote(str(stubborn_ready))} "
        f"&& test -s {shlex.quote(str(stopped_ready))}"
    )
    try:
        assert _execute(runtime, command).exit_code == 0
        session_id = cast(int, runtime.ownership._session_id)
        before = runtime.supervisor._session_processes(
            session_id,
            timeout_seconds=1,
        )
        assert len([record for record in before if not record.is_zombie]) >= 4
        assert any(record.state.startswith("T") for record in before)

        started = time.monotonic()
        result = runtime.supervisor.cleanup(runtime.ownership, deadline_ms=3000)

        assert result.reaped
        assert result.remaining_managed_processes == 0
        assert time.monotonic() - started < 3.5
        assert not runtime.supervisor.is_alive(runtime.ownership)
        assert runtime.supervisor.cleanup(
            runtime.ownership,
            deadline_ms=100,
        ).reaped
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
def test_zero_deadline_is_bounded_and_never_reports_false_success(tmp_path: Path) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "zero-deadline")
    transport = PexpectPosixPtyTransport()
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    request = SpawnRequest(
        sys.executable,
        ("-c", "import time; time.sleep(30)"),
        str(tmp_path),
        dict(os.environ),
    )
    session = cast(PexpectPosixSession, transport.spawn(request, ownership))
    try:
        started = time.monotonic()
        immediate = supervisor.cleanup(ownership, deadline_ms=0)
        assert time.monotonic() - started < 0.2
        assert not immediate.reaped
        assert immediate.remaining_managed_processes > 0
        assert supervisor.cleanup(ownership, deadline_ms=2000).reaped
    finally:
        with suppress(ProcessControlError):
            supervisor.cleanup(ownership, deadline_ms=2000)
        transport.close(session)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
def test_disown_remains_managed_but_setsid_escape_is_explicitly_unsupported(
    tmp_path: Path,
) -> None:
    runtime = _start_bash(tmp_path)
    escaped_pid_file = tmp_path / "escaped.pid"
    escaped_pid: int | None = None
    escape_script = (
        "import os,pathlib,time; child=os.fork(); "
        "child and os._exit(0); os.setsid(); "
        f"pathlib.Path({str(escaped_pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = (
        "sleep 30 >/dev/null 2>&1 & "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(escape_script)} "
        ">/dev/null 2>&1 & disown -a; "
        f"for _ in {{1..200}}; do test -s {shlex.quote(str(escaped_pid_file))} && break; "
        "sleep 0.01; done; "
        f"test -s {shlex.quote(str(escaped_pid_file))}"
    )
    try:
        assert _execute(runtime, command).exit_code == 0
        escaped_pid = int(escaped_pid_file.read_text())
        assert os.getsid(escaped_pid) == escaped_pid
        managed_session = cast(int, runtime.ownership._session_id)
        managed = runtime.supervisor._session_processes(
            managed_session,
            timeout_seconds=1,
        )
        assert all(record.process_id != escaped_pid for record in managed)
        assert len([record for record in managed if not record.is_zombie]) >= 2

        assert runtime.supervisor.cleanup(
            runtime.ownership,
            deadline_ms=3000,
        ).reaped
        os.kill(escaped_pid, 0)
    finally:
        runtime.close()
        if escaped_pid is None and escaped_pid_file.exists():
            escaped_pid = int(escaped_pid_file.read_text())
        if escaped_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(escaped_pid, signal.SIGKILL)


def test_unattached_and_cross_supervisor_ownership_are_rejected() -> None:
    first = PosixProcessSupervisor(ownership_id_factory=lambda: "first")
    second = PosixProcessSupervisor(ownership_id_factory=lambda: "second")
    ownership = first.prepare()

    with pytest.raises(ProcessControlError, match="not been attached"):
        first.is_alive(ownership)
    with pytest.raises(ProcessControlError, match="different POSIX supervisor"):
        second.cleanup(ownership, deadline_ms=0)
    with pytest.raises(ProcessControlError, match="cannot be negative"):
        first.cleanup(ownership, deadline_ms=-1)


def test_control_reports_not_delivered_for_closed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "closed")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    ownership._terminal_file_descriptor = 99

    def closed_terminal(_file_descriptor: int) -> int:
        raise OSError(errno.EBADF, "closed")

    monkeypatch.setattr(os, "tcgetpgrp", closed_terminal)
    assert not supervisor.control(ownership, ControlIntent.INTERRUPT).delivered


@pytest.mark.parametrize(
    ("intent", "expected_signal"),
    [
        (ControlIntent.INTERRUPT, signal.SIGINT),
        (ControlIntent.TERMINATE, signal.SIGTERM),
        (ControlIntent.KILL, signal.SIGKILL),
    ],
)
def test_domain_intents_have_exact_private_posix_signal_mapping(
    monkeypatch: pytest.MonkeyPatch,
    intent: ControlIntent,
    expected_signal: signal.Signals,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "mapping")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    ownership._terminal_file_descriptor = 99
    delivered: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "tcgetpgrp", lambda _file_descriptor: 456)
    monkeypatch.setattr(
        supervisor,
        "_session_processes",
        lambda _session_id, *, timeout_seconds: [
            posix_process_module._ProcessRecord(789, 456, 123, "S")
        ],
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, native_signal: delivered.append((process_group, native_signal)),
    )

    assert supervisor.control(ownership, intent).delivered
    assert delivered == [(456, expected_signal)]


def test_cleanup_orders_cont_term_then_cont_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "ordering")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    records = [
        posix_process_module._ProcessRecord(123, 123, 123, "S"),
        posix_process_module._ProcessRecord(456, 456, 123, "T"),
    ]
    snapshots = [records, []]
    waits = [records, []]
    deliveries: list[tuple[list[int], int]] = []

    def record_delivery(
        groups: list[int],
        native_signal: int,
        _deadline: float | None = None,
    ) -> bool:
        deliveries.append((groups, native_signal))
        return True

    monkeypatch.setattr(
        supervisor,
        "_snapshot_before_deadline",
        lambda _session_id, _deadline: snapshots.pop(0),
    )
    monkeypatch.setattr(
        supervisor,
        "_wait_for_session",
        lambda _session_id, _deadline, _records: waits.pop(0),
    )
    monkeypatch.setattr(
        supervisor,
        "_signal_groups",
        record_delivery,
    )
    monkeypatch.setattr(supervisor, "_finalize_leader", lambda _ownership: True)

    assert supervisor.cleanup(ownership, deadline_ms=1000).reaped
    assert deliveries == [
        ([456, 123], signal.SIGCONT),
        ([456, 123], signal.SIGTERM),
        ([456, 123], signal.SIGCONT),
        ([456, 123], signal.SIGKILL),
    ]


def test_cleanup_retries_transient_nonblocking_leader_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "reap-race")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    attempts = iter([False, True])
    sleeps: list[float] = []

    monkeypatch.setattr(supervisor, "_snapshot_before_deadline", lambda *_args: [])
    monkeypatch.setattr(supervisor, "_finalize_leader", lambda _ownership: next(attempts))
    monkeypatch.setattr(time, "sleep", sleeps.append)

    result = supervisor.cleanup(ownership, deadline_ms=1_000)

    assert result.reaped
    assert sleeps == [0.01]


def test_finalized_ownership_never_reuses_old_native_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "pid-fence")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    zombie = posix_process_module._ProcessRecord(123, 123, 123, "Z")
    snapshots = 0

    def snapshot_once(_session_id: int, *, timeout_seconds: float) -> list[object]:
        nonlocal snapshots
        snapshots += 1
        if snapshots > 1:
            raise AssertionError("finalized ownership reused its old PID")
        return [zombie]

    monkeypatch.setattr(supervisor, "_session_processes", snapshot_once)
    monkeypatch.setattr(os, "waitpid", lambda _process_id, _options: (123, 0))

    assert supervisor.is_alive(ownership) is False
    assert ownership._finalized
    assert ownership._process_id is None
    assert ownership._session_id is None
    assert ownership._shell_process_group_id is None
    assert not supervisor.control(ownership, ControlIntent.KILL).delivered
    assert not supervisor.is_alive(ownership)
    assert supervisor.cleanup(ownership, deadline_ms=1000).reaped
    assert snapshots == 1


def test_terminal_close_failure_stays_finalized_and_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "close-pending")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    read_fd, write_fd = os.pipe()
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    ownership._terminal_file_descriptor = read_fd
    zombie = posix_process_module._ProcessRecord(123, 123, 123, "Z")
    real_close = os.close
    close_attempts = 0

    def fail_once(file_descriptor: int) -> None:
        nonlocal close_attempts
        if file_descriptor == read_fd and close_attempts == 0:
            close_attempts += 1
            raise OSError(errno.EIO, "injected close failure")
        real_close(file_descriptor)

    monkeypatch.setattr(
        supervisor,
        "_snapshot_before_deadline",
        lambda _session_id, _deadline: [zombie],
    )
    monkeypatch.setattr(os, "waitpid", lambda _process_id, _options: (123, 0))
    monkeypatch.setattr(os, "close", fail_once)
    try:
        with pytest.raises(ProcessControlError, match="close the POSIX ownership terminal"):
            supervisor.cleanup(ownership, deadline_ms=1000)
        assert ownership._finalized
        assert ownership._process_id is None
        assert ownership._terminal_file_descriptor == read_fd
        assert supervisor.cleanup(ownership, deadline_ms=1000).reaped
        assert ownership._terminal_file_descriptor == -1
    finally:
        if ownership._terminal_file_descriptor >= 0:
            real_close(read_fd)
        real_close(write_fd)


def test_slow_identity_enumeration_respects_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "slow-enumeration")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    output = "\n".join(f"{process_id} 123 456 S" for process_id in range(1000, 1100))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, output, ""),
    )

    def slow_getsid(_process_id: int) -> int:
        time.sleep(0.01)
        return 123

    forced: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getsid", slow_getsid)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda process_group, native_signal: forced.append((process_group, native_signal)),
    )
    started = time.monotonic()
    result = supervisor.cleanup(ownership, deadline_ms=25)
    elapsed = time.monotonic() - started
    assert not result.reaped
    assert result.remaining_managed_processes > 0
    assert forced == [(123, signal.SIGKILL)]
    assert elapsed < 0.1


def test_large_group_tree_signal_delivery_stops_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "many-groups")
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    ownership._process_id = 123
    ownership._session_id = 123
    ownership._shell_process_group_id = 123
    records = [
        posix_process_module._ProcessRecord(process_id, process_id, 123, "S")
        for process_id in range(1000, 1100)
    ]
    deliveries: list[int] = []
    monkeypatch.setattr(
        supervisor,
        "_snapshot_before_deadline",
        lambda _session_id, _deadline: records,
    )

    def slow_killpg(process_group: int, _native_signal: int) -> None:
        deliveries.append(process_group)
        time.sleep(0.01)

    monkeypatch.setattr(os, "killpg", slow_killpg)
    started = time.monotonic()
    result = supervisor.cleanup(ownership, deadline_ms=25)
    elapsed = time.monotonic() - started
    assert not result.reaped
    assert result.remaining_managed_processes == 100
    assert 1 <= len(deliveries) < 10
    assert elapsed < 0.1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
def test_invalid_forkpty_identity_is_killed_and_reaped_before_attach_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "invalid-identity")
    transport = PexpectPosixPtyTransport()
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    real_getsid = os.getsid
    parent_session_id = real_getsid(0)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "getsid", lambda _process_id: parent_session_id)
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(
                SpawnRequest(
                    sys.executable,
                    ("-c", "import time; time.sleep(30)"),
                    str(tmp_path),
                    dict(os.environ),
                ),
                ownership,
            )
    assert ownership._finalized
    assert ownership._process_id is None
    assert supervisor.cleanup(ownership, deadline_ms=1000).reaped


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
def test_pre_fork_reservation_is_cleanup_safe_when_spawn_never_attaches(
    tmp_path: Path,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "failed-spawn")
    transport = PexpectPosixPtyTransport()
    ownership = cast(PosixProcessOwnership, supervisor.prepare())

    with pytest.raises(TransportError, match="failed to spawn"):
        transport.spawn(
            SpawnRequest(
                str(tmp_path / "missing-executable"),
                (),
                str(tmp_path),
                dict(os.environ),
            ),
            ownership,
        )

    assert ownership._attach_consumed
    assert ownership._attachment_pending
    assert ownership._process_id is None
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped
    assert ownership._finalized


@pytest.mark.skipif(os.name != "posix", reason="POSIX process test")
@pytest.mark.parametrize("finalize_first", [False, True])
def test_active_or_finalized_ownership_reuse_reaps_rejected_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    finalize_first: bool,
) -> None:
    supervisor = PosixProcessSupervisor(ownership_id_factory=lambda: "single-use")
    transport = PexpectPosixPtyTransport()
    ownership = cast(PosixProcessOwnership, supervisor.prepare())
    spawned: list[Any] = []
    real_spawn = pexpect.spawn

    def record_spawn(*args: object, **kwargs: object) -> Any:
        child = real_spawn(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(pexpect, "spawn", record_spawn)
    request = SpawnRequest(
        sys.executable,
        ("-c", "import time; time.sleep(30)"),
        str(tmp_path),
        dict(os.environ),
    )
    first_session = cast(PexpectPosixSession, transport.spawn(request, ownership))
    first_process_id = ownership._process_id
    try:
        if finalize_first:
            assert supervisor.cleanup(ownership, deadline_ms=2000).reaped
            transport.close(first_session)
        with pytest.raises(TransportError, match="failed to spawn"):
            transport.spawn(request, ownership)

        assert len(spawned) == 1
        assert ownership._attach_consumed
        if finalize_first:
            assert ownership._finalized
            assert ownership._process_id is None
            assert supervisor.cleanup(ownership, deadline_ms=100).reaped
        else:
            assert not ownership._finalized
            assert ownership._process_id == first_process_id
            assert supervisor.is_alive(ownership)
    finally:
        with suppress(ProcessControlError):
            supervisor.cleanup(ownership, deadline_ms=2000)
        transport.close(first_session)
