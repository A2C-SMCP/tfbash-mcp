from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tfbash_mcp.domain import (
    CapacityExceeded,
    ExecutionNotFound,
    ExecutionState,
    InvalidTransition,
    ShellBusy,
    ShellClosing,
    ShellNotFound,
    ShellRegistry,
    ShellState,
)


@dataclass
class FakeClock:
    monotonic: int = 0
    wall: int = 10_000

    def monotonic_ms(self) -> int:
        return self.monotonic

    def wall_time_ms(self) -> int:
        return self.wall

    def advance(self, milliseconds: int) -> None:
        self.monotonic += milliseconds
        self.wall += milliseconds


@dataclass
class SequentialIds:
    counts: dict[str, int] = field(default_factory=dict)

    def __call__(self, prefix: str, sequence: int) -> str:
        self.counts[prefix] = sequence
        return f"{prefix}_{sequence}"


def _registry(
    clock: FakeClock,
    *,
    max_shells: int = 2,
    max_retained: int = 2,
    retention_ms: int = 100,
) -> ShellRegistry:
    return ShellRegistry(
        max_shells=max_shells,
        max_retained_executions=max_retained,
        completed_retention_ms=retention_ms,
        clock=clock,
        id_factory=SequentialIds(),
    )


def _finish(registry: ShellRegistry, shell_id: str, exec_id: str) -> None:
    assert registry.finish_execution(
        shell_id,
        exec_id,
        ExecutionState.EXITED,
        next_shell_state=ShellState.READY,
        exit_code=0,
    )


def test_registry_retains_forced_kill_completion_on_ready_shell() -> None:
    registry = _registry(FakeClock())
    shell = registry.create_shell(cwd="/one")
    execution = registry.start_execution(shell.shell_id, max_output_bytes=64)

    assert registry.finish_execution(
        shell.shell_id,
        execution.exec_id,
        ExecutionState.CANCELLED,
        next_shell_state=ShellState.READY,
        cwd="/rebuilt",
        shell_rebuilt=True,
    )

    completed = registry.get_execution(shell.shell_id, execution.exec_id).snapshot(
        cursor=0,
        max_bytes=64,
    )
    assert completed.status is ExecutionState.CANCELLED
    assert completed.shell_status is ShellState.READY
    assert completed.shell_rebuilt is True
    assert registry.get_shell(shell.shell_id).state is ShellState.READY


def test_registry_capacity_identity_and_close_reuse() -> None:
    clock = FakeClock()
    registry = _registry(clock)
    first = registry.create_shell(cwd="/one")
    second = registry.create_shell(cwd="/two")
    assert [item.shell_id for item in registry.snapshots()] == ["shell_1", "shell_2"]

    with pytest.raises(CapacityExceeded):
        registry.create_shell(cwd="/three")
    registry.begin_close(first.shell_id)
    registry.remove_closed(first.shell_id)
    replacement = registry.create_shell(cwd="/three")
    assert replacement.shell_id == "shell_3"

    with pytest.raises(ShellNotFound):
        registry.get_shell(first.shell_id)
    assert registry.get_shell(second.shell_id) is second


def test_registry_enforces_single_active_and_cross_shell_addressing() -> None:
    clock = FakeClock()
    registry = _registry(clock)
    first = registry.create_shell(cwd="/one")
    second = registry.create_shell(cwd="/two")
    execution = registry.start_execution(first.shell_id, max_output_bytes=64)

    with pytest.raises(ShellBusy):
        registry.start_execution(first.shell_id, max_output_bytes=64)
    with pytest.raises(ExecutionNotFound):
        registry.get_execution(second.shell_id, execution.exec_id)

    _finish(registry, first.shell_id, execution.exec_id)
    assert registry.get_execution(first.shell_id, execution.exec_id) is execution
    next_execution = registry.start_execution(first.shell_id, max_output_bytes=64)
    assert next_execution.exec_id != execution.exec_id


def test_completed_retention_applies_ttl_before_global_count() -> None:
    clock = FakeClock()
    registry = _registry(clock, max_retained=2, retention_ms=100)
    first = registry.create_shell(cwd="/one")
    second = registry.create_shell(cwd="/two")

    one = registry.start_execution(first.shell_id, max_output_bytes=64)
    _finish(registry, first.shell_id, one.exec_id)
    clock.advance(10)
    two = registry.start_execution(second.shell_id, max_output_bytes=64)
    _finish(registry, second.shell_id, two.exec_id)
    clock.advance(10)
    three = registry.start_execution(first.shell_id, max_output_bytes=64)
    _finish(registry, first.shell_id, three.exec_id)

    with pytest.raises(ExecutionNotFound):
        registry.get_execution(first.shell_id, one.exec_id)
    assert registry.get_execution(second.shell_id, two.exec_id) is two
    assert registry.get_execution(first.shell_id, three.exec_id) is three

    clock.advance(100)
    assert set(registry.prune_completed()) == {two.exec_id, three.exec_id}
    with pytest.raises(ExecutionNotFound):
        registry.get_execution(second.shell_id, two.exec_id)


def test_overview_prefers_active_then_latest_retained_execution_tail() -> None:
    clock = FakeClock()
    registry = _registry(clock, max_retained=3, retention_ms=100)
    shell = registry.create_shell(cwd="/one")
    first = registry.start_execution(shell.shell_id, max_output_bytes=64)
    first.append_output(b"first-output")
    _finish(registry, shell.shell_id, first.exec_id)
    second = registry.start_execution(shell.shell_id, max_output_bytes=64)
    second.append_output("active-界🙂-tail".encode())

    active = registry.overview_snapshots(max_output_characters=6)[0]
    assert active.shell.active_exec_id == second.exec_id
    assert active.execution is not None
    assert active.execution.exec_id == second.exec_id
    assert active.execution.output == "active-界🙂-tail"[-6:]
    assert active.execution.output_truncated is True

    _finish(registry, shell.shell_id, second.exec_id)
    completed = registry.overview_snapshots(max_output_characters=500)[0]
    assert completed.shell.active_exec_id is None
    assert completed.execution is not None
    assert completed.execution.exec_id == second.exec_id
    assert completed.execution.output == "active-界🙂-tail"

    clock.advance(100)
    expired = registry.overview_snapshots(max_output_characters=500)[0]
    assert expired.execution is None


def test_overview_change_subscription_covers_lifecycle_state_and_output() -> None:
    registry = _registry(FakeClock())
    notifications = 0

    def changed() -> None:
        nonlocal notifications
        notifications += 1

    unsubscribe = registry.subscribe_overview_changes(changed)
    shell = registry.create_shell(cwd="/one")
    after_create = notifications
    execution = registry.start_execution(shell.shell_id, max_output_bytes=64)
    after_start = notifications
    execution.append_output(b"output")
    after_output = notifications
    _finish(registry, shell.shell_id, execution.exec_id)
    after_finish = notifications
    registry.begin_close(shell.shell_id)
    registry.remove_closed(shell.shell_id)

    assert 0 < after_create < after_start < after_output < after_finish < notifications
    unsubscribe()
    after_unsubscribe = notifications
    registry.create_shell(cwd="/two")
    assert notifications == after_unsubscribe


def test_close_cancellation_wins_and_late_completion_is_noop() -> None:
    clock = FakeClock()
    registry = _registry(clock)
    shell = registry.create_shell(cwd="/one")
    execution = registry.start_execution(shell.shell_id, max_output_bytes=64)

    assert registry.begin_close(shell.shell_id) is execution
    with pytest.raises(ShellClosing):
        registry.get_execution(shell.shell_id, execution.exec_id)
    assert execution.state.value == "running"
    assert registry.cancel_active_for_close(shell.shell_id) is True
    assert registry.prune_completed() == ()
    assert not registry.finish_execution(
        shell.shell_id,
        execution.exec_id,
        ExecutionState.EXITED,
        next_shell_state=ShellState.READY,
        exit_code=0,
    )
    assert execution.state.value == "cancelled"

    registry.remove_closed(shell.shell_id)
    with pytest.raises(ShellNotFound):
        registry.get_execution(shell.shell_id, execution.exec_id)


def test_close_fence_rejects_reads_of_retained_executions() -> None:
    clock = FakeClock()
    registry = _registry(clock)
    shell = registry.create_shell(cwd="/one")
    execution = registry.start_execution(shell.shell_id, max_output_bytes=64)
    _finish(registry, shell.shell_id, execution.exec_id)
    assert registry.get_execution(shell.shell_id, execution.exec_id) is execution

    registry.begin_close(shell.shell_id)
    with pytest.raises(ShellClosing):
        registry.get_execution(shell.shell_id, execution.exec_id)


def test_shell_must_cross_close_fence_before_removal() -> None:
    registry = _registry(FakeClock())
    shell = registry.create_shell(cwd="/one")
    with pytest.raises(InvalidTransition):
        registry.remove_closed(shell.shell_id)

    execution = registry.start_execution(shell.shell_id, max_output_bytes=64)
    registry.begin_close(shell.shell_id)
    with pytest.raises(InvalidTransition, match="active execution"):
        registry.remove_closed(shell.shell_id)
    assert registry.cancel_active_for_close(shell.shell_id)
    assert execution.state is ExecutionState.CANCELLED
    registry.remove_closed(shell.shell_id)


def test_domain_package_has_no_runtime_or_platform_process_imports() -> None:
    forbidden_roots = {
        "ctypes",
        "os",
        "pexpect",
        "pty",
        "pywinpty",
        "signal",
        "subprocess",
        "winpty",
    }
    domain_root = Path(__file__).parents[1] / "src" / "tfbash_mcp" / "domain"
    imported_roots: set[str] = set()
    for path in domain_root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_roots.add(node.module.partition(".")[0])
    assert imported_roots.isdisjoint(forbidden_roots)
