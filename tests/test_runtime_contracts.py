from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tfbash_mcp.runtime import (
    CleanupResult,
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

    def prepare_session(self, request: ShellStartRequest) -> DialectSessionPlan:
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

    def spawn(
        self,
        request: SpawnRequest,
        ownership: ProcessOwnership,
    ) -> RuntimeSession:
        if self.fail_spawn:
            raise OSError("spawn failed")
        session = _Session(f"session-{len(self.spawned) + 1}")
        self.spawned.append(session)
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

    def close(self, session: RuntimeSession) -> None:
        self.closed.add(session.session_id)


@dataclass
class _Supervisor:
    runtime_name: RuntimeName
    owners: set[str] = field(default_factory=set)
    cleaned: set[str] = field(default_factory=set)
    fail_prepare: bool = False

    def prepare(self) -> ProcessOwnership:
        owner = _Ownership(f"owner-{len(self.owners) + 1}")
        self.owners.add(owner.ownership_id)
        if self.fail_prepare:
            self.owners.discard(owner.ownership_id)
            raise OSError("native ownership setup failed")
        return owner

    def control(
        self,
        ownership: ProcessOwnership,
        intent: ControlIntent,
    ) -> ControlDelivery:
        return ControlDelivery(ownership.ownership_id in self.owners)

    def is_alive(self, ownership: ProcessOwnership) -> bool:
        return ownership.ownership_id in self.owners

    def cleanup(
        self,
        ownership: ProcessOwnership,
        *,
        deadline_ms: int,
    ) -> CleanupResult:
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
    assert plan.protocol.feed(read.data) == (
        DialectEvent(DialectEventKind.OUTPUT, data=b"ready"),
    )
    frame = plan.protocol.wrap_command("echo test")
    assert frame.correlation_id
    assert profile.transport.write(session, memoryview(frame.input_bytes)).bytes_written > 0
    delivery = profile.supervisor.control(ownership, ControlIntent.INTERRUPT)
    assert delivery.delivered is True
    assert profile.supervisor.is_alive(ownership) is True
    assert profile.supervisor.cleanup(
        ownership,
        deadline_ms=100,
    ).reaped is True
    profile.transport.close(session)


def test_dialect_launch_and_parser_state_are_paired_per_shell() -> None:
    dialect = _Dialect(RuntimeName.POSIX_BASH, DialectName.BASH, "/bin/bash")
    request = ShellStartRequest("/bin/bash", "/workspace", {}, None)

    first = dialect.prepare_session(request)
    second = dialect.prepare_session(request)

    assert first.launch.initial_input != second.launch.initial_input
    assert first.protocol.wrap_command("true").input_bytes != second.protocol.wrap_command(
        "true"
    ).input_bytes


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
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.partition(".")[0])
    assert imported_roots.isdisjoint(forbidden)


def test_ports_do_not_accept_host_config_or_expose_native_identity_names() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "tfbash_mcp" / "runtime" / "contracts.py"
    ).read_text()
    tree = ast.parse(source)
    referenced_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    declared_names = {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    } | {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "HostConfig" not in referenced_names
    assert declared_names.isdisjoint({"pid", "process_group", "job_object", "handle", "fd"})
