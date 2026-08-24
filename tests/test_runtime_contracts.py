from __future__ import annotations

import ast
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

import pytest

from tfbash_mcp.runtime import (
    CancellationSignal,
    CleanupResult,
    CleanupTimeout,
    CommandFrame,
    ControlDelivery,
    ControlIntent,
    DialectEvent,
    DialectEventKind,
    DialectLaunch,
    DialectName,
    DialectSessionPlan,
    PosixBashProfile,
    ProcessControlError,
    ProcessOwnership,
    ReadStatus,
    RuntimeName,
    RuntimeSession,
    ShellStartRequest,
    SpawnRequest,
    TransportError,
    TransportRead,
    TransportWrite,
    WaitInterest,
    WindowsPwshProfile,
)


@dataclass(frozen=True)
class _Session:
    session_id: str


@dataclass(frozen=True)
class _Ownership:
    ownership_id: str


@dataclass
class _Protocol:
    marker: str
    next_command: int = 0

    def wrap_command(self, command: str) -> CommandFrame:
        self.next_command += 1
        correlation_id = f"command-{self.next_command}"
        return CommandFrame(
            correlation_id,
            f"[{self.marker}:{correlation_id}]{command}".encode(),
        )

    def recovery_input(self) -> bytes:
        return b"recover"

    def begin_finalization(self) -> CommandFrame:
        return CommandFrame("finalize", b"finalize")

    def feed(self, data: bytes) -> tuple[DialectEvent, ...]:
        return (DialectEvent(DialectEventKind.OUTPUT, data=data),)

    def end_of_stream(self) -> tuple[DialectEvent, ...]:
        return ()


@dataclass
class _Dialect:
    runtime_name: RuntimeName
    dialect_name: DialectName
    default_executable: str
    next_session: int = 0

    def prepare_session(
        self,
        request: ShellStartRequest,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> DialectSessionPlan:
        self.next_session += 1
        marker = f"marker-{self.next_session}"
        return DialectSessionPlan(
            launch=DialectLaunch(
                SpawnRequest(
                    request.executable,
                    ("--interactive",),
                    request.cwd,
                    request.environment,
                ),
                f"bootstrap:{marker}".encode(),
            ),
            protocol=_Protocol(marker),
        )


@dataclass
class _Transport:
    runtime_name: RuntimeName
    spawned: list[_Session] = field(default_factory=list)
    writes: list[bytes] = field(default_factory=list)
    closed: set[str] = field(default_factory=set)
    fail_spawn: bool = False
    cancel_during_spawn: Event | None = None
    close_failures: int = 0

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> RuntimeSession:
        if self.fail_spawn:
            raise OSError("spawn failed")
        session = _Session(f"session-{len(self.spawned) + 1}")
        self.spawned.append(session)
        if self.cancel_during_spawn is not None:
            self.cancel_during_spawn.set()
        return session

    def read(self, session: RuntimeSession, max_bytes: int) -> TransportRead:
        return TransportRead(ReadStatus.DATA, b"ready"[:max_bytes])

    def write(self, session: RuntimeSession, data: memoryview) -> TransportWrite:
        payload = bytes(data)
        self.writes.append(payload)
        return TransportWrite(len(payload), would_block=False)

    def wait(
        self,
        session: RuntimeSession,
        interests: frozenset[WaitInterest],
        timeout_ms: int,
    ) -> frozenset[WaitInterest]:
        return interests

    def close(
        self,
        session: RuntimeSession,
        *,
        deadline_ms: int | None = None,
    ) -> None:
        if self.close_failures > 0:
            self.close_failures -= 1
            raise TransportError("injected PTY close failure")
        self.closed.add(session.session_id)


@dataclass
class _Supervisor:
    runtime_name: RuntimeName
    owners: set[str] = field(default_factory=set)
    cleaned: set[str] = field(default_factory=set)
    fail_prepare: bool = False
    cancel_after_prepare: Event | None = None
    cleanup_failures: int = 0

    def prepare(self) -> ProcessOwnership:
        owner = _Ownership(f"owner-{len(self.owners) + 1}")
        self.owners.add(owner.ownership_id)
        if self.fail_prepare:
            self.owners.discard(owner.ownership_id)
            raise OSError("native ownership setup failed")
        if self.cancel_after_prepare is not None:
            self.cancel_after_prepare.set()
        return owner

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
        *,
        deadline_ms: int | None = None,
    ) -> ControlDelivery:
        return ControlDelivery(ownership.ownership_id in self.owners)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        return ownership.ownership_id in self.owners

    def cleanup_execution(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        return CleanupResult(reaped=True, remaining_managed_processes=0)

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
        if self.cleanup_failures > 0:
            self.cleanup_failures -= 1
            return CleanupResult(reaped=False, remaining_managed_processes=1)
        self.owners.discard(ownership.ownership_id)
        self.cleaned.add(ownership.ownership_id)
        return CleanupResult(reaped=True, remaining_managed_processes=0)


def _build_profile(name: RuntimeName):  # type: ignore[no-untyped-def]
    dialect_name = DialectName.BASH if name is RuntimeName.POSIX_BASH else DialectName.PWSH
    executable = "/bin/bash" if name is RuntimeName.POSIX_BASH else "pwsh.exe"
    parts = {
        "dialect": _Dialect(name, dialect_name, executable),
        "transport": _Transport(name),
        "supervisor": _Supervisor(name),
    }
    if name is RuntimeName.POSIX_BASH:
        return PosixBashProfile(**parts)  # type: ignore[arg-type]
    return WindowsPwshProfile(**parts)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", list(RuntimeName))
def test_both_profiles_obey_the_same_port_contract(name: RuntimeName) -> None:
    profile = _build_profile(name)
    request = ShellStartRequest(
        executable=profile.dialect.default_executable,
        cwd="/opaque/native/path",
        environment={"PROJECT": "test"},
        startup_command=None,
    )
    plan = profile.dialect.prepare_session(request)
    managed = profile.open_session(plan.launch.spawn, cleanup_deadline_ms=100)
    session = managed.session
    ownership = managed.ownership

    assert profile.transport.write(session, memoryview(plan.launch.initial_input)).bytes_written > 0
    assert profile.transport.wait(
        session,
        frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT}),
        10,
    ) == frozenset({WaitInterest.READABLE, WaitInterest.PROCESS_EXIT})
    read = profile.transport.read(session, 1024)
    assert read == TransportRead(ReadStatus.DATA, b"ready")
    assert plan.protocol.feed(read.data) == (DialectEvent(DialectEventKind.OUTPUT, data=b"ready"),)
    frame = plan.protocol.wrap_command("echo test")
    assert frame.correlation_id
    assert profile.transport.write(session, memoryview(frame.input_bytes)).bytes_written > 0
    delivery = profile.supervisor.control(ownership, ControlIntent.INTERRUPT)
    assert delivery.delivered is True
    assert profile.supervisor.is_alive(ownership) is True
    assert (
        profile.supervisor.cleanup(
            ownership,
            deadline_ms=100,
        ).reaped
        is True
    )
    profile.transport.close(session)


def test_dialect_launch_and_parser_state_are_paired_per_shell() -> None:
    dialect = _Dialect(RuntimeName.POSIX_BASH, DialectName.BASH, "/bin/bash")
    request = ShellStartRequest("/bin/bash", "/workspace", {}, None)

    first = dialect.prepare_session(request)
    second = dialect.prepare_session(request)

    assert first.launch.initial_input != second.launch.initial_input
    assert (
        first.protocol.wrap_command("true").input_bytes
        != second.protocol.wrap_command("true").input_bytes
    )


def test_spawn_failure_reaps_prepared_ownership() -> None:
    profile = _build_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    transport.fail_spawn = True

    with pytest.raises(TransportError, match="PTY spawn failed") as caught:
        profile.open_session(
            SpawnRequest("/bin/bash", (), "/workspace", {}),
            cleanup_deadline_ms=100,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert supervisor.owners == set()
    assert supervisor.cleaned == {"owner-1"}


def test_cancellation_after_prepare_reaps_ownership_before_spawn() -> None:
    profile = _build_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    cancel_signal = Event()
    supervisor.cancel_after_prepare = cancel_signal

    with pytest.raises(TransportError, match="runtime startup was cancelled"):
        profile.open_session(
            SpawnRequest("/bin/bash", (), "/workspace", {}),
            cleanup_deadline_ms=100,
            cancel_signal=cancel_signal,
        )

    assert transport.spawned == []
    assert supervisor.owners == set()
    assert supervisor.cleaned == {"owner-1"}


def test_cancellation_after_spawn_closes_pty_and_reaps_ownership() -> None:
    profile = _build_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    cancel_signal = Event()
    transport.cancel_during_spawn = cancel_signal

    with pytest.raises(TransportError, match="runtime startup was cancelled"):
        profile.open_session(
            SpawnRequest("/bin/bash", (), "/workspace", {}),
            cleanup_deadline_ms=100,
            cancel_signal=cancel_signal,
        )

    assert [session.session_id for session in transport.spawned] == ["session-1"]
    assert transport.closed == {"session-1"}
    assert supervisor.owners == set()
    assert supervisor.cleaned == {"owner-1"}


def test_failed_startup_rollback_is_retained_and_retried_to_completion() -> None:
    profile = _build_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    cancel_signal = Event()
    transport.cancel_during_spawn = cancel_signal
    transport.close_failures = 1
    supervisor.cleanup_failures = 1

    with pytest.raises(CleanupTimeout, match="ownership cleanup timed out"):
        profile.open_session(
            SpawnRequest("/bin/bash", (), "/workspace", {}),
            cleanup_deadline_ms=100,
            cancel_signal=cancel_signal,
        )

    assert profile.has_pending_startup_cleanup
    assert transport.closed == set()
    assert supervisor.owners == {"owner-1"}

    assert profile.cleanup_pending_startups(deadline_ms=100)
    assert not profile.has_pending_startup_cleanup
    assert transport.closed == {"session-1"}
    assert supervisor.owners == set()
    assert supervisor.cleaned == {"owner-1"}


def test_spawn_thread_start_failure_rolls_back_prepared_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _build_profile(RuntimeName.POSIX_BASH)
    transport = profile.transport
    supervisor = profile.supervisor
    assert isinstance(transport, _Transport)
    assert isinstance(supervisor, _Supervisor)
    original_start = threading.Thread.start

    def injected_start(thread: threading.Thread) -> None:
        if thread.name.startswith("tfbash-spawn-"):
            raise RuntimeError("injected thread exhaustion")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", injected_start)

    with pytest.raises(TransportError, match="isolated PTY spawn"):
        profile.open_session(
            SpawnRequest("/bin/bash", (), "/workspace", {}),
            cleanup_deadline_ms=100,
        )

    assert transport.spawned == []
    assert supervisor.owners == set()
    assert supervisor.cleaned == {"owner-1"}
    assert not profile.has_pending_startup_cleanup


@pytest.mark.parametrize("name", list(RuntimeName))
def test_prepare_failure_has_strong_no_ownership_leak_guarantee(name: RuntimeName) -> None:
    profile = _build_profile(name)
    supervisor = profile.supervisor
    assert isinstance(supervisor, _Supervisor)
    supervisor.fail_prepare = True

    with pytest.raises(
        ProcessControlError,
        match="failed to prepare process ownership",
    ) as caught:
        profile.open_session(
            SpawnRequest("native-shell", (), "/workspace", {}),
            cleanup_deadline_ms=100,
        )

    assert isinstance(caught.value.__cause__, OSError)
    assert supervisor.owners == set()
    assert supervisor.cleaned == set()


@pytest.mark.parametrize(
    "read",
    [
        TransportRead(ReadStatus.WOULD_BLOCK),
        TransportRead(ReadStatus.EOF),
        TransportRead(ReadStatus.DATA, b"x"),
    ],
)
def test_transport_read_states_are_unambiguous(read: TransportRead) -> None:
    assert bool(read.data) is (read.status is ReadStatus.DATA)


def test_runtime_contract_rejects_ambiguous_read_and_event_values() -> None:
    with pytest.raises(ValueError):
        TransportRead(ReadStatus.WOULD_BLOCK, b"unexpected")
    with pytest.raises(ValueError):
        DialectEvent(DialectEventKind.OUTPUT)
    with pytest.raises(ValueError):
        DialectEvent(DialectEventKind.COMMAND_COMPLETE, correlation_id="id")
    with pytest.raises(ValueError):
        CleanupResult(reaped=True, remaining_managed_processes=1)


def test_runtime_boundary_has_no_platform_or_mcp_imports() -> None:
    package = Path(__file__).parents[1] / "src" / "tfbash_mcp" / "runtime"
    imported_roots: set[str] = set()
    forbidden = {
        "ctypes",
        "fcntl",
        "mcp",
        "pexpect",
        "psutil",
        "pywinpty",
        "signal",
        "subprocess",
        "termios",
        "win32api",
        "winpty",
    }
    for source in package.glob("*.py"):
        if source.name in {
            "posix_process.py",
            "posix_pty.py",
            "windows_conpty.py",
            "windows_bootstrap.py",
            "windows_process.py",
            "windows_win32.py",
        }:
            continue
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.partition(".")[0])
    assert imported_roots.isdisjoint(forbidden)


def test_shared_runtime_import_does_not_eagerly_load_posix_adapters() -> None:
    script = """
import signal
import sys

for name in ("SIGCONT", "SIGKILL"):
    if hasattr(signal, name):
        delattr(signal, name)
import tfbash_mcp.runtime

assert "tfbash_mcp.runtime.posix_process" not in sys.modules
assert "tfbash_mcp.runtime.posix_pty" not in sys.modules
assert "tfbash_mcp.runtime.windows_conpty" not in sys.modules
assert "tfbash_mcp.runtime.windows_process" not in sys.modules
assert "tfbash_mcp.runtime.windows_win32" not in sys.modules
assert "winpty" not in sys.modules
assert tfbash_mcp.runtime.RuntimeName.WINDOWS_PWSH.value == "windows-pwsh"
"""
    completed = subprocess.run(
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr


def test_ports_do_not_accept_host_config_or_expose_native_identity_names() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "tfbash_mcp" / "runtime" / "contracts.py"
    ).read_text()
    tree = ast.parse(source)
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    declared_names = {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)} | {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "HostConfig" not in referenced_names
    assert declared_names.isdisjoint({"pid", "process_group", "job_object", "handle", "fd"})
