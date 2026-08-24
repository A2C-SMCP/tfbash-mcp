from __future__ import annotations

import ctypes
from typing import cast

import pytest

from tfbash_mcp.runtime.windows_process import (
    WindowsProcessHandle,
    WindowsProcessIdentity,
)
from tfbash_mcp.runtime.windows_win32 import _BASIC_ACCOUNTING, CtypesWindowsProcessApi


class _WaitKernel:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, int]] = []

    def WaitForSingleObject(self, handle: object, timeout_ms: int) -> int:
        self.calls.append((handle, timeout_ms))
        return self.result


class _StableIdentityApi(CtypesWindowsProcessApi):
    def _identity(self, handle: int, process_id: int) -> WindowsProcessIdentity:
        return WindowsProcessIdentity(process_id, 100)


class _DuplicateKernel:
    def __init__(self, process_id: int) -> None:
        self.process_id = process_id
        self.closed: list[int] = []

    def GetCurrentProcess(self) -> int:
        return -1

    def DuplicateHandle(self, *arguments: object) -> int:
        target = arguments[3]
        target._obj.value = 5678  # type: ignore[attr-defined]
        return 1

    def GetProcessId(self, _handle: object) -> int:
        return self.process_id

    def CloseHandle(self, handle: object) -> int:
        self.closed.append(cast(int, handle.value))  # type: ignore[attr-defined]
        return 1


class _ReadyKernel:
    def __init__(self, wait_result: int) -> None:
        self.wait_result = wait_result
        self.closed: list[int] = []

    def OpenEventW(self, _access: int, _inherit: bool, _name: str) -> int:
        return 2468

    def WaitForSingleObject(self, _handle: object, timeout_ms: int) -> int:
        assert timeout_ms == 0
        return self.wait_result

    def CloseHandle(self, handle: object) -> int:
        self.closed.append(cast(int, handle.value))  # type: ignore[attr-defined]
        return 1


def _api(result: int) -> tuple[_StableIdentityApi, _WaitKernel]:
    api = object.__new__(_StableIdentityApi)
    kernel = _WaitKernel(result)
    api._kernel = cast(object, kernel)
    return api, kernel


def test_liveness_uses_wait_state_not_ambiguous_exit_code_259() -> None:
    process = WindowsProcessHandle(WindowsProcessIdentity(42, 100), 1234)
    exited, exited_kernel = _api(0)
    running, running_kernel = _api(0x102)

    assert not exited.process_is_alive(process)
    assert running.process_is_alive(process)
    assert exited_kernel.calls[0][1] == 0
    assert running_kernel.calls[0][1] == 0


def test_duplicate_handle_must_belong_to_the_reported_spawn_pid() -> None:
    api = object.__new__(_StableIdentityApi)
    kernel = _DuplicateKernel(process_id=99)
    api._kernel = cast(object, kernel)

    with pytest.raises(OSError, match="does not match"):
        api.duplicate_process(42, 1234, assign_to_job=True)

    assert kernel.closed == [5678]


def test_job_accounting_layout_matches_win32_field_order() -> None:
    assert _BASIC_ACCOUNTING.total_page_fault_count.offset == 32
    assert _BASIC_ACCOUNTING.total_processes.offset == 36
    assert _BASIC_ACCOUNTING.active_processes.offset == 40
    assert _BASIC_ACCOUNTING.terminated_processes.offset == 44
    assert ctypes.sizeof(_BASIC_ACCOUNTING) == 48


def test_child_readiness_requires_a_signaled_named_event() -> None:
    signaled = object.__new__(CtypesWindowsProcessApi)
    signaled_kernel = _ReadyKernel(0)
    signaled._kernel = cast(object, signaled_kernel)
    pending = object.__new__(CtypesWindowsProcessApi)
    pending_kernel = _ReadyKernel(0x102)
    pending._kernel = cast(object, pending_kernel)

    assert signaled.child_gate_is_ready(r"Local\tfbash-mcp-token-child-42-100")
    assert not pending.child_gate_is_ready(r"Local\tfbash-mcp-token-child-42-100")
    assert signaled_kernel.closed == [2468]
    assert pending_kernel.closed == [2468]
