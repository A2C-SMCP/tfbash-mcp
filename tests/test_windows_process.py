from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import cast

import pytest

from tfbash_mcp.runtime import (
    ControlIntent,
    ProcessControlError,
    SpawnRequest,
    WindowsProcessHandle,
    WindowsProcessIdentity,
    WindowsProcessOwnership,
    WindowsProcessSupervisor,
)


@dataclass
class _Process:
    process_id: int
    created: int
    parent_id: int | None
    alive: bool = True
    job: int | None = None


class _FakeApi:
    def __init__(self) -> None:
        self.next_job = 1
        self.next_created = 100
        self.jobs: set[int] = set()
        self.processes: dict[int, _Process] = {}
        self.closed_jobs: list[int] = []
        self.gates: dict[int, str] = {}
        self.closed_gates: list[int] = []
        self.signaled_gates: list[int] = []
        self.closed_processes: list[WindowsProcessIdentity] = []
        self.terminated_processes: list[WindowsProcessIdentity] = []
        self.terminated_jobs: list[int] = []
        self.fail_create = False
        self.fail_assign = False
        self.fail_membership_check = False
        self.fail_open = False
        self.open_delay = 0.0
        self.fail_terminate_process = False
        self.close_failures: dict[WindowsProcessIdentity, int] = {}
        self.job_termination_stuck = False
        self.enumeration_error: Exception | None = None
        self.enumeration_delay = 0.0
        self.fail_signal_gate = False
        self.signal_hook: Callable[[], None] | None = None
        self.assign_hook: Callable[[], None] | None = None
        self.armed_shell: tuple[int, int] | None = None
        self.ready_children: dict[int, int] = {}

    def arm_shell(self, bootstrap_id: int, shell_id: int) -> None:
        self.armed_shell = (bootstrap_id, shell_id)

    def add_process(self, process_id: int, *, parent_id: int | None = None) -> _Process:
        parent = self.processes.get(parent_id) if parent_id is not None else None
        process = _Process(
            process_id,
            self.next_created,
            parent_id,
            job=None if parent is None else parent.job,
        )
        self.next_created += 1
        self.processes[process_id] = process
        return process

    def create_kill_on_close_job(self) -> object:
        if self.fail_create:
            raise OSError("create failed")
        job = self.next_job
        self.next_job += 1
        self.jobs.add(job)
        return job

    def close_job(self, job: object) -> None:
        value = cast(int, job)
        self.jobs.remove(value)
        self.closed_jobs.append(value)
        for process in self.processes.values():
            if process.job == value:
                process.alive = False

    def create_gate_event(self, name: str) -> object:
        gate = 10_000 + len(self.gates)
        self.gates[gate] = name
        return gate

    def signal_gate_event(self, event: object) -> None:
        if self.fail_signal_gate:
            raise OSError("signal failed")
        self.signaled_gates.append(cast(int, event))
        if self.armed_shell is not None:
            bootstrap_id, shell_id = self.armed_shell
            shell = self.add_process(shell_id, parent_id=bootstrap_id)
            self.ready_children[shell_id] = shell.created
            self.armed_shell = None
        if self.signal_hook is not None:
            self.signal_hook()

    def close_gate_event(self, event: object) -> None:
        value = cast(int, event)
        self.gates.pop(value)
        self.closed_gates.append(value)

    def child_gate_is_ready(self, name: str) -> bool:
        try:
            identity = name.rpartition("-child-")[2]
            process_id_text, creation_text = identity.split("-", 1)
            process_id = int(process_id_text)
            creation_time = int(creation_text)
        except ValueError:
            return False
        process = self.processes.get(process_id)
        return process is not None and self.ready_children.get(process_id) == creation_time

    def open_process(
        self,
        process_id: int,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle:
        assert assign_to_job in {True, False}
        if self.fail_open:
            raise PermissionError("open denied")
        process = self.processes[process_id]
        if not process.alive:
            raise OSError("missing process")
        identity = WindowsProcessIdentity(process.process_id, process.created)
        return WindowsProcessHandle(identity, identity)

    def duplicate_process(
        self,
        process_id: int,
        native_handle: object,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle:
        assert native_handle == f"handle-{process_id}"
        return self.open_process(process_id, assign_to_job=assign_to_job)

    def open_process_if_alive(self, process_id: int) -> WindowsProcessHandle | None:
        process = self.processes.get(process_id)
        if process is None or not process.alive:
            return None
        if self.open_delay:
            time.sleep(self.open_delay)
        return self.open_process(process_id)

    def close_process(self, process: WindowsProcessHandle) -> None:
        remaining = self.close_failures.get(process.identity, 0)
        if remaining:
            self.close_failures[process.identity] = remaining - 1
            raise OSError("close failed")
        self.closed_processes.append(process.identity)

    def assign_process(self, job: object, process: WindowsProcessHandle) -> None:
        if self.fail_assign:
            raise OSError("assign failed")
        current = self._matching(process)
        if current is None:
            raise OSError("stale process")
        current.job = cast(int, job)
        if self.assign_hook is not None:
            self.assign_hook()

    def process_is_in_job(self, job: object, process: WindowsProcessHandle) -> bool:
        if self.fail_membership_check:
            raise OSError("membership failed")
        current = self._matching(process)
        return current is not None and current.job == job

    def process_is_alive(self, process: WindowsProcessHandle) -> bool:
        current = self._matching(process)
        return current is not None and current.alive

    def terminate_process(self, process: WindowsProcessHandle, exit_code: int) -> None:
        assert exit_code == 1
        if self.fail_terminate_process:
            raise OSError("terminate failed")
        current = self._matching(process)
        if current is not None:
            current.alive = False
            self.terminated_processes.append(process.identity)

    def wait_processes(
        self,
        processes: tuple[WindowsProcessHandle, ...],
        timeout_ms: int,
    ) -> bool:
        assert timeout_ms >= 0
        return all(not self.process_is_alive(process) for process in processes)

    def active_job_processes(self, job: object) -> int:
        return sum(process.alive and process.job == job for process in self.processes.values())

    def job_process_ids(
        self,
        job: object,
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]:
        if self.enumeration_error is not None:
            raise self.enumeration_error
        if self.enumeration_delay:
            time.sleep(self.enumeration_delay)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("enumeration deadline")
        return tuple(
            process.process_id
            for process in self.processes.values()
            if process.alive and process.job == job
        )

    def terminate_job(self, job: object, exit_code: int) -> None:
        assert exit_code == 1
        value = cast(int, job)
        self.terminated_jobs.append(value)
        if self.job_termination_stuck:
            return
        for process in self.processes.values():
            if process.job == value:
                process.alive = False

    def _matching(self, handle: WindowsProcessHandle) -> _Process | None:
        current = self.processes.get(handle.identity.process_id)
        if current is None or current.created != handle.identity.creation_time_100ns:
            return None
        return current


def _attached(
    *,
    api: _FakeApi | None = None,
    terminate_grace_ms: int = 0,
) -> tuple[WindowsProcessSupervisor, WindowsProcessOwnership, _FakeApi]:
    selected = api or _FakeApi()
    selected.add_process(99)
    selected.arm_shell(99, 100)
    supervisor = WindowsProcessSupervisor(
        api=selected,
        ownership_id_factory=lambda: "test-owner",
        terminate_grace_ms=terminate_grace_ms,
    )
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))
    ownership.attach(99, native_handle="handle-99")
    return supervisor, ownership, selected


def test_prepare_attach_and_cross_supervisor_ownership_fence() -> None:
    supervisor, ownership, api = _attached()

    assert ownership.ownership_id == "test-owner"
    assert api.processes[100].job == 1
    with pytest.raises(ProcessControlError, match="reused"):
        ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))
    with pytest.raises(ProcessControlError, match="different Windows supervisor"):
        WindowsProcessSupervisor(api=api).is_alive(ownership)


def test_prepare_failure_has_no_active_ownership() -> None:
    api = _FakeApi()
    api.fail_create = True
    supervisor = WindowsProcessSupervisor(api=api)

    with pytest.raises(ProcessControlError, match="prepare Windows process ownership"):
        supervisor.prepare()

    assert api.jobs == set()


def test_prepare_translates_id_factory_failure_and_rolls_back_constructor_failure() -> None:
    api = _FakeApi()

    def fail_id() -> str:
        raise OSError("entropy unavailable")

    with pytest.raises(ProcessControlError, match="prepare Windows process ownership"):
        WindowsProcessSupervisor(api=api, ownership_id_factory=fail_id).prepare()
    assert api.jobs == set()

    def fail_ownership(**_kwargs: object) -> WindowsProcessOwnership:
        raise OSError("construction failed")

    with pytest.raises(ProcessControlError, match="prepare Windows process ownership"):
        WindowsProcessSupervisor(api=api, ownership_factory=fail_ownership).prepare()
    assert api.jobs == set()
    assert api.closed_jobs == [1]


def test_attach_duplicates_the_native_spawn_handle() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.arm_shell(100, 101)
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    ownership.attach(100, native_handle="handle-100")

    assert api.processes[100].job == 1


def test_reserved_spawn_is_gated_and_released_only_after_job_membership() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.arm_shell(100, 101)
    supervisor = WindowsProcessSupervisor(
        api=api,
        gate_name_factory=lambda: r"Local\tfbash-mcp-test-gate",
        bootstrap_path=r"C:\tfbash\windows_bootstrap.py",
        python_executable=r"C:\Python\python.exe",
        gate_wait_timeout_ms=4321,
        shell_ready_timeout_ms=50,
    )
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    original = SpawnRequest(
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        ("-NoLogo", "value with spaces"),
        r"C:\workspace",
        {"Path": r"C:\tools", "SECRET": "retained"},
    )

    wrapped = ownership.reserve_spawn(original)

    assert wrapped.executable == r"C:\Python\python.exe"
    assert wrapped.arguments == ("-I", "-u", r"C:\tfbash\windows_bootstrap.py")
    assert wrapped.cwd == original.cwd
    assert wrapped.environment["TFBASH_MCP_GATE_NAME"] == r"Local\tfbash-mcp-test-gate"
    assert wrapped.environment["TFBASH_MCP_GATE_TIMEOUT_MS"] == "4321"
    assert api.signaled_gates == []

    ownership.attach(100, native_handle="handle-100")

    assert api.processes[100].job == 1
    assert api.processes[101].job == 1
    assert api.signaled_gates == [10_000]
    assert api.closed_gates == []


def test_gate_release_failure_terminates_attached_bootstrap_without_release() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.fail_signal_gate = True
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="release the assigned"):
        ownership.attach(100, native_handle="handle-100")

    assert not api.processes[100].alive
    assert api.signaled_gates == []


def test_cancel_flipped_during_assignment_prevents_gate_release() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.arm_shell(100, 101)
    cancelled = Event()
    api.assign_hook = cancelled.set
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    released = ownership.attach(
        100,
        native_handle="handle-100",
        cancel_signal=cancelled,
    )

    assert not released
    assert api.signaled_gates == []
    assert 101 not in api.processes


def test_shell_ready_wait_uses_caller_startup_deadline() -> None:
    api = _FakeApi()
    api.add_process(100)
    supervisor = WindowsProcessSupervisor(api=api, shell_ready_timeout_ms=5_000)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))
    started = time.monotonic()

    with pytest.raises(ProcessControlError, match="release the assigned"):
        ownership.attach(
            100,
            native_handle="handle-100",
            deadline=time.monotonic() + 0.02,
        )

    assert time.monotonic() - started < 0.1
    assert not api.processes[100].alive


def test_missing_native_spawn_handle_is_indeterminate_and_never_recaptured_by_pid() -> None:
    api = _FakeApi()
    api.add_process(100)
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="no exact native handle"):
        ownership.attach(100)

    result = supervisor.cleanup(ownership, deadline_ms=100)
    assert not result.reaped
    assert api.processes[100].alive
    assert api.signaled_gates == []


def test_failed_native_spawn_attachment_never_releases_the_bootstrap_gate() -> None:
    api = _FakeApi()
    api.add_process(100)
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    ownership.attach(100, native_handle="handle-100", release=False)

    assert api.processes[100].job == 1
    assert api.signaled_gates == []
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped


def test_unready_job_child_cannot_be_promoted_to_shell() -> None:
    api = _FakeApi()
    api.add_process(100)
    unrelated = api.add_process(101)
    unrelated.job = 1
    supervisor = WindowsProcessSupervisor(api=api, shell_ready_timeout_ms=5)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="release the assigned"):
        ownership.attach(100, native_handle="handle-100")

    assert not api.processes[100].alive
    assert not unrelated.alive


def test_ready_event_cannot_authenticate_a_reused_child_pid() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.arm_shell(100, 101)

    def reuse_child_pid() -> None:
        api.processes[101].alive = False
        api.add_process(101, parent_id=100)

    api.signal_hook = reuse_child_pid
    supervisor = WindowsProcessSupervisor(api=api, shell_ready_timeout_ms=5)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="release the assigned"):
        ownership.attach(100, native_handle="handle-100")

    assert not api.processes[100].alive
    assert not api.processes[101].alive


def test_reserved_bootstrap_environment_key_is_rejected_case_insensitively() -> None:
    api = _FakeApi()
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())

    with pytest.raises(ProcessControlError, match="reserved bootstrap keys"):
        ownership.reserve_spawn(
            SpawnRequest(
                "pwsh.exe",
                (),
                r"C:\workspace",
                {"tfbash_mcp_gate_name": "injected"},
            )
        )


def test_unattached_attempt_is_indeterminate_until_transport_proves_no_spawn() -> None:
    api = _FakeApi()
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    pending = supervisor.cleanup(ownership, deadline_ms=100)
    assert not pending.reaped

    ownership.abort_unspawned()
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped


def test_interrupt_is_delivered_only_after_bound_conpty_writer_accepts_it() -> None:
    supervisor, ownership, _ = _attached()
    attempts: list[str] = []
    deadlines: list[int | None] = []

    assert not supervisor.control(ownership, ControlIntent.INTERRUPT).delivered

    def send_interrupt(_deadline_ms: int | None) -> bool:
        attempts.append("ctrl-c")
        deadlines.append(_deadline_ms)
        return True

    ownership.bind_interrupt(send_interrupt)

    assert supervisor.control(
        ownership,
        ControlIntent.INTERRUPT,
        deadline_ms=100,
    ).delivered
    assert attempts == ["ctrl-c"]
    assert deadlines[0] is not None and 0 < deadlines[0] <= 100

    with pytest.raises(ProcessControlError, match="deadline expired"):
        supervisor.control(ownership, ControlIntent.INTERRUPT, deadline_ms=0)


def test_control_translates_deadline_crossed_during_job_enumeration() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)
    api.enumeration_delay = 0.01

    with pytest.raises(ProcessControlError, match="Windows process control failed"):
        supervisor.control(
            ownership,
            ControlIntent.TERMINATE,
            deadline_ms=1,
        )


def test_terminate_graces_with_interrupt_then_kills_only_descendants() -> None:
    supervisor, ownership, api = _attached(terminate_grace_ms=20)
    api.add_process(200, parent_id=100)
    api.add_process(300, parent_id=200)
    escaped = api.add_process(400)
    interrupts: list[bool] = []

    def send_interrupt(_deadline_ms: int | None) -> bool:
        interrupts.append(True)
        return True

    ownership.bind_interrupt(send_interrupt)

    delivery = supervisor.control(ownership, ControlIntent.TERMINATE)

    assert delivery.delivered
    assert interrupts == [True]
    assert api.processes[100].alive
    assert not api.processes[200].alive
    assert not api.processes[300].alive
    assert escaped.alive


def test_kill_terminates_the_job_and_requires_observable_shell_rebuild() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)

    assert supervisor.control(ownership, ControlIntent.KILL).delivered

    assert not api.processes[100].alive
    assert not api.processes[200].alive
    assert api.terminated_jobs == [1]
    assert not supervisor.is_alive(ownership)


@pytest.mark.parametrize("intent", [ControlIntent.TERMINATE, ControlIntent.KILL])
def test_tree_control_still_reaps_descendants_after_external_root_exit(
    intent: ControlIntent,
) -> None:
    supervisor, ownership, api = _attached()
    child = api.add_process(200, parent_id=100)
    api.processes[100].alive = False

    delivery = supervisor.control(ownership, intent)

    assert delivery.delivered
    assert not child.alive


def test_execution_cleanup_reaps_new_descendants_and_preserves_shell() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)
    api.add_process(300, parent_id=200)

    result = supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert result.reaped
    assert result.remaining_managed_processes == 0
    assert api.processes[100].alive
    assert not api.processes[200].alive
    assert not api.processes[300].alive
    assert supervisor.is_alive(ownership)


def test_execution_cleanup_preserves_identity_fenced_conpty_infrastructure() -> None:
    api = _FakeApi()
    infrastructure: list[_Process] = []

    def create_infrastructure() -> None:
        infrastructure.append(api.add_process(150, parent_id=99))

    api.signal_hook = create_infrastructure
    supervisor, ownership, api = _attached(api=api)
    descendant = api.add_process(200, parent_id=100)

    result = supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert result.reaped
    assert infrastructure[0].alive
    assert not descendant.alive
    assert WindowsProcessIdentity(150, infrastructure[0].created) in ownership._infrastructure


def test_reused_infrastructure_pid_does_not_escape_execution_cleanup() -> None:
    api = _FakeApi()
    infrastructure: list[_Process] = []

    def create_infrastructure() -> None:
        infrastructure.append(api.add_process(150, parent_id=99))

    api.signal_hook = create_infrastructure
    supervisor, ownership, api = _attached(api=api)
    original_identity = WindowsProcessIdentity(150, infrastructure[0].created)
    infrastructure[0].alive = False
    replacement = api.add_process(150, parent_id=100)

    result = supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert result.reaped
    assert not replacement.alive
    assert WindowsProcessIdentity(150, replacement.created) in api.terminated_processes
    assert original_identity not in api.terminated_processes


def test_execution_cleanup_never_crosses_job_boundary_for_an_escaped_descendant() -> None:
    supervisor, ownership, api = _attached()
    escaped = api.add_process(200, parent_id=100)
    escaped.job = None

    result = supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert result.reaped
    assert escaped.alive
    assert WindowsProcessIdentity(200, escaped.created) not in api.terminated_processes


def test_execution_cleanup_zero_deadline_is_non_destructive_and_retryable() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)

    immediate = supervisor.cleanup_execution(ownership, deadline_ms=0)
    retried = supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert not immediate.reaped
    assert immediate.remaining_managed_processes == 1
    assert retried.reaped


def test_shell_cleanup_terminates_job_closes_handles_and_is_idempotent() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)

    result = supervisor.cleanup(ownership, deadline_ms=100)

    assert result.reaped
    assert api.terminated_jobs == [1]
    assert api.closed_jobs == [1]
    assert api.closed_processes == [
        WindowsProcessIdentity(100, 101),
        WindowsProcessIdentity(99, 100),
    ]
    assert api.closed_gates == [10_000]
    assert supervisor.cleanup(ownership, deadline_ms=0).reaped


def test_shell_cleanup_waits_for_asynchronous_job_termination_without_rekilling_root() -> None:
    class AsynchronousJobApi(_FakeApi):
        def terminate_job(self, job: object, exit_code: int) -> None:
            assert exit_code == 1
            self.terminated_jobs.append(cast(int, job))

        def job_process_ids(
            self,
            job: object,
            *,
            deadline: float | None = None,
        ) -> tuple[int, ...]:
            if self.terminated_jobs:
                for process in self.processes.values():
                    if process.job == job:
                        process.alive = False
            return super().job_process_ids(job, deadline=deadline)

        def terminate_process(self, process: WindowsProcessHandle, exit_code: int) -> None:
            if self.terminated_jobs and process.identity.process_id == 100:
                raise AssertionError("Job-owned root must not be terminated a second time")
            super().terminate_process(process, exit_code)

    supervisor, ownership, api = _attached(api=AsynchronousJobApi())

    assert supervisor.cleanup(ownership, deadline_ms=100).reaped
    assert api.terminated_jobs == [1]


def test_zero_deadline_and_stuck_job_retain_ownership_for_retry() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)
    api.job_termination_stuck = True

    immediate = supervisor.cleanup(ownership, deadline_ms=0)
    incomplete = supervisor.cleanup(ownership, deadline_ms=1)

    assert not immediate.reaped
    assert not incomplete.reaped
    assert api.closed_jobs == []
    api.job_termination_stuck = False
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped


def test_failed_assignment_keeps_exact_process_reachable_for_runtime_rollback() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.fail_assign = True
    api.fail_terminate_process = True
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="assign"):
        ownership.attach(100, native_handle="handle-100")

    api.fail_terminate_process = False
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped
    assert not api.processes[100].alive


def test_unretainable_spawn_never_reports_cleanup_success() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.fail_open = True
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="retain"):
        ownership.attach(100, native_handle="handle-100")

    result = supervisor.cleanup(ownership, deadline_ms=100)
    assert not result.reaped
    assert result.remaining_managed_processes == 1
    assert api.processes[100].alive
    assert api.closed_jobs == []


def test_assignment_verification_failure_still_terminates_job_descendants() -> None:
    api = _FakeApi()
    api.add_process(100)
    api.fail_membership_check = True
    api.fail_terminate_process = True
    supervisor = WindowsProcessSupervisor(api=api)
    ownership = cast(WindowsProcessOwnership, supervisor.prepare())
    ownership.reserve_spawn(SpawnRequest("pwsh.exe", (), r"C:\workspace", {}))

    with pytest.raises(ProcessControlError, match="assign"):
        ownership.attach(100, native_handle="handle-100")

    child = api.add_process(200, parent_id=100)
    api.fail_terminate_process = False
    assert child.job == 1
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped
    assert not child.alive
    assert api.terminated_jobs == [1]


def test_failed_descendant_handle_close_is_retained_and_retried() -> None:
    supervisor, ownership, api = _attached()
    child = api.add_process(200, parent_id=100)
    sibling = api.add_process(300, parent_id=100)
    child_identity = WindowsProcessIdentity(200, child.created)
    sibling_identity = WindowsProcessIdentity(300, sibling.created)
    api.close_failures[child_identity] = 1

    with pytest.raises(ProcessControlError, match="close Windows process handles"):
        supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert child_identity not in api.closed_processes
    assert sibling_identity in api.closed_processes
    assert supervisor.cleanup_execution(ownership, deadline_ms=100).reaped
    assert api.closed_processes.count(child_identity) == 1
    assert api.closed_processes.count(sibling_identity) == 1


def test_repeated_terminate_retries_pending_close_before_opening_new_handles() -> None:
    supervisor, ownership, api = _attached()
    child = api.add_process(200, parent_id=100)
    identity = WindowsProcessIdentity(200, child.created)
    api.close_failures[identity] = 1

    with pytest.raises(ProcessControlError, match="close Windows process handles"):
        supervisor.control(ownership, ControlIntent.TERMINATE)

    assert not child.alive
    assert not supervisor.control(ownership, ControlIntent.TERMINATE).delivered
    assert api.closed_processes.count(identity) == 1
    assert supervisor.cleanup(ownership, deadline_ms=100).reaped


def test_execution_cleanup_bounds_slow_enumeration_and_is_retryable() -> None:
    supervisor, ownership, api = _attached()
    api.add_process(200, parent_id=100)
    api.enumeration_delay = 0.02

    started = time.monotonic()
    result = supervisor.cleanup_execution(ownership, deadline_ms=1)

    assert time.monotonic() - started < 0.1
    assert not result.reaped
    assert result.remaining_managed_processes >= 1
    api.enumeration_delay = 0
    assert supervisor.cleanup_execution(ownership, deadline_ms=100).reaped


def test_handle_opened_across_deadline_is_registered_closed_and_retryable() -> None:
    supervisor, ownership, api = _attached()
    child = api.add_process(200, parent_id=100)
    identity = WindowsProcessIdentity(200, child.created)
    api.open_delay = 0.02

    result = supervisor.cleanup_execution(ownership, deadline_ms=1)

    assert not result.reaped
    assert identity in api.closed_processes
    api.open_delay = 0
    assert supervisor.cleanup_execution(ownership, deadline_ms=100).reaped


def test_retained_handle_never_controls_a_reused_pid() -> None:
    supervisor, ownership, api = _attached()
    api.processes[100].alive = False
    replacement = api.add_process(100)

    assert supervisor.cleanup(ownership, deadline_ms=100).reaped
    assert replacement.alive
    assert WindowsProcessIdentity(100, replacement.created) not in api.terminated_processes


def test_indeterminate_process_enumeration_fails_closed() -> None:
    supervisor, ownership, api = _attached()
    api.enumeration_error = PermissionError("access denied")

    with pytest.raises(ProcessControlError, match="inspect Windows Job"):
        supervisor.cleanup_execution(ownership, deadline_ms=100)

    assert api.processes[100].alive


@pytest.mark.parametrize("deadline", (-1,))
def test_negative_cleanup_deadlines_are_rejected(deadline: int) -> None:
    supervisor, ownership, _ = _attached()

    with pytest.raises(ProcessControlError, match="deadline"):
        supervisor.cleanup_execution(ownership, deadline_ms=deadline)
    with pytest.raises(ProcessControlError, match="deadline"):
        supervisor.cleanup(ownership, deadline_ms=deadline)
