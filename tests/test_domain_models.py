from __future__ import annotations

from dataclasses import dataclass

import pytest

from tfbash_mcp.domain import (
    ChangeSignal,
    CommandShell,
    Execution,
    ExecutionNotActive,
    ExecutionState,
    InvalidTransition,
    ShellBusy,
    ShellClosing,
    ShellState,
    ShellUnavailable,
)


@dataclass
class FakeClock:
    monotonic: int = 100
    wall: int = 1_000

    def monotonic_ms(self) -> int:
        return self.monotonic

    def wall_time_ms(self) -> int:
        return self.wall

    def advance(self, milliseconds: int) -> None:
        self.monotonic += milliseconds
        self.wall += milliseconds


def _execution(
    clock: FakeClock, *, exec_id: str = "exec_1", change_signal: ChangeSignal | None = None
) -> Execution:
    return Execution(
        shell_id="shell_1",
        exec_id=exec_id,
        max_output_bytes=64,
        clock=clock,
        change_signal=change_signal,
    )


def test_execution_finalizing_is_publicly_running_but_rejects_input() -> None:
    clock = FakeClock()
    change_signal = ChangeSignal()
    execution = _execution(clock, change_signal=change_signal)

    assert execution.append_output(b"user\n") == 5
    assert execution.begin_finalizing() is True
    assert execution.begin_finalizing() is False
    assert execution.state is ExecutionState.FINALIZING
    assert execution.input_active is False
    with pytest.raises(ExecutionNotActive):
        execution.require_active_input()

    during_cleanup = execution.snapshot(cursor=0, max_bytes=64)
    assert during_cleanup.status is ExecutionState.RUNNING
    assert during_cleanup.eof is False

    assert execution.append_output(b"cleanup\n") == 8
    clock.advance(25)
    assert execution.complete(
        ExecutionState.EXITED,
        shell_status=ShellState.READY,
        exit_code=0,
        cwd="/workspace/project",
    )
    final = execution.snapshot(cursor=0, max_bytes=64)
    assert final.status is ExecutionState.EXITED
    assert final.output == "user\ncleanup\n"
    assert final.duration_ms == 25
    assert final.cwd == "/workspace/project"
    assert final.eof is True
    assert execution.generation == 4
    assert change_signal.generation == 4
    assert change_signal.wait_for_change(4, timeout_seconds=0) == 4


def test_terminal_completion_is_first_writer_wins_cas() -> None:
    clock = FakeClock()
    execution = _execution(clock)

    assert execution.complete(
        ExecutionState.TIMEOUT,
        shell_status=ShellState.READY,
    )
    assert not execution.complete(
        ExecutionState.SHELL_ERROR,
        shell_status=ShellState.ERROR,
    )
    assert execution.state is ExecutionState.TIMEOUT
    with pytest.raises(InvalidTransition):
        execution.append_output(b"late")


def test_completion_flushes_incomplete_utf8_and_eof_tracks_read_window() -> None:
    clock = FakeClock()
    execution = _execution(clock)
    assert execution.append_output(b"A\xe2") == 1
    assert execution.complete(
        ExecutionState.SHELL_ERROR,
        shell_status=ShellState.ERROR,
    )

    partial = execution.snapshot(cursor=0, max_bytes=1)
    assert partial.output == "A"
    assert partial.eof is False
    tail = execution.snapshot(cursor=partial.next_cursor, max_bytes=4)
    assert tail.output == "\ufffd"
    assert tail.eof is True


@pytest.mark.parametrize(
    ("state", "shell_state", "exit_code"),
    [
        (ExecutionState.RUNNING, ShellState.READY, None),
        (ExecutionState.EXITED, ShellState.ERROR, 0),
        (ExecutionState.EXITED, ShellState.READY, None),
        (ExecutionState.TIMEOUT, ShellState.READY, 1),
        (ExecutionState.SHELL_ERROR, ShellState.READY, None),
    ],
)
def test_invalid_terminal_state_matrix_is_rejected(
    state: ExecutionState, shell_state: ShellState, exit_code: int | None
) -> None:
    with pytest.raises(InvalidTransition):
        _execution(FakeClock()).complete(
            state,
            shell_status=shell_state,
            exit_code=exit_code,
        )


def test_shell_rebuilt_is_reserved_for_disruption_recovery() -> None:
    with pytest.raises(InvalidTransition, match="timeout or forced-kill"):
        _execution(FakeClock()).complete(
            ExecutionState.EXITED,
            shell_status=ShellState.READY,
            exit_code=0,
            shell_rebuilt=True,
        )

    execution = _execution(FakeClock())
    assert execution.complete(
        ExecutionState.TIMEOUT,
        shell_status=ShellState.READY,
        shell_rebuilt=True,
    )
    assert execution.snapshot(cursor=0, max_bytes=4).shell_rebuilt is True

    killed = _execution(FakeClock())
    assert killed.complete(
        ExecutionState.CANCELLED,
        shell_status=ShellState.READY,
        shell_rebuilt=True,
    )
    assert killed.snapshot(cursor=0, max_bytes=4).shell_rebuilt is True


def test_shell_enforces_single_active_execution_and_normal_completion() -> None:
    clock = FakeClock()
    shell = CommandShell(shell_id="shell_1", cwd="/workspace", clock=clock)
    first = _execution(clock)
    shell.start_execution(first)

    assert shell.snapshot().status is ShellState.BUSY
    assert shell.snapshot().active_exec_id == "exec_1"
    with pytest.raises(ShellBusy):
        shell.start_execution(_execution(clock, exec_id="exec_2"))

    assert shell.finish_execution(
        "exec_1",
        ExecutionState.EXITED,
        next_shell_state=ShellState.READY,
        exit_code=7,
        cwd="/workspace/next",
    )
    snapshot = shell.snapshot()
    assert snapshot.status is ShellState.READY
    assert snapshot.active_exec_id is None
    assert snapshot.last_known_cwd == "/workspace/next"

    shell.mark_error()
    assert shell.state is ShellState.ERROR
    with pytest.raises(ShellUnavailable):
        shell.start_execution(_execution(clock, exec_id="exec_3"))


def test_close_fence_and_terminal_cas_cover_both_forced_interleavings() -> None:
    clock = FakeClock()

    natural_first = CommandShell(shell_id="shell_1", cwd="/workspace", clock=clock)
    natural_execution = _execution(clock)
    natural_first.start_execution(natural_execution)
    assert natural_first.begin_close() is natural_execution
    assert natural_first.snapshot().active_exec_id is None
    assert natural_first.finish_execution(
        "exec_1",
        ExecutionState.EXITED,
        next_shell_state=ShellState.READY,
        exit_code=0,
    )
    assert natural_execution.snapshot(cursor=0, max_bytes=4).shell_status is ShellState.CLOSING
    assert natural_first.cancel_active() is False

    cancel_first = CommandShell(shell_id="shell_1", cwd="/workspace", clock=clock)
    cancelled_execution = _execution(clock)
    cancel_first.start_execution(cancelled_execution)
    cancel_first.begin_close()
    assert cancel_first.cancel_active() is True
    assert not cancelled_execution.complete(
        ExecutionState.EXITED,
        shell_status=ShellState.CLOSING,
        exit_code=0,
    )
    assert cancelled_execution.state is ExecutionState.CANCELLED
    with pytest.raises(ExecutionNotActive):
        cancel_first.finish_execution(
            "exec_1",
            ExecutionState.EXITED,
            next_shell_state=ShellState.READY,
            exit_code=0,
        )
    with pytest.raises(ShellClosing):
        cancel_first.begin_close()
