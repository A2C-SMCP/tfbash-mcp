"""Least-privilege ctypes bindings for Windows process and Job Object ownership."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any, Final, cast

from tfbash_mcp.runtime.windows_process import (
    WindowsProcessHandle,
    WindowsProcessIdentity,
)

_PROCESS_TERMINATE: Final = 0x0001
_PROCESS_SET_QUOTA: Final = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_SYNCHRONIZE: Final = 0x00100000
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 0x102
_WAIT_FAILED: Final = 0xFFFFFFFF
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x2000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS: Final = 1
_JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS: Final = 3
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
_ERROR_INVALID_PARAMETER: Final = 87
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_FILE_NOT_FOUND: Final = 2
_ERROR_MORE_DATA: Final = 234
_ERROR_ALREADY_EXISTS: Final = 183
_DWORD = ctypes.c_uint32


class _FILETIME(ctypes.Structure):
    _fields_ = (("low", _DWORD), ("high", _DWORD))


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = tuple(
        (name, ctypes.c_ulonglong)
        for name in (
            "read_operations",
            "write_operations",
            "other_operations",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    )


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = (
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", _DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", _DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", _DWORD),
        ("scheduling_class", _DWORD),
    )


class _EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = (
        ("basic", _BASIC_LIMITS),
        ("io", _IO_COUNTERS),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    )


class _BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = (
        ("total_user_time", ctypes.c_longlong),
        ("total_kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong),
        ("period_kernel_time", ctypes.c_longlong),
        ("total_page_fault_count", _DWORD),
        ("total_processes", _DWORD),
        ("active_processes", _DWORD),
        ("terminated_processes", _DWORD),
    )


class CtypesWindowsProcessApi:
    """Win32 implementation retaining handles as the PID-reuse fence."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 process APIs require Windows")
        win_dll = cast(Any, ctypes.__dict__["WinDLL"])
        kernel = win_dll("kernel32", use_last_error=True)
        self._kernel = kernel
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.argtypes = ()
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessId.argtypes = (wintypes.HANDLE,)
        kernel.GetProcessId.restype = wintypes.DWORD
        kernel.DuplicateHandle.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        )
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.IsProcessInJob.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        )
        kernel.IsProcessInJob.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD

    def create_kill_on_close_job(self) -> object:
        value = self._handle_value(self._kernel.CreateJobObjectW(None, None))
        if not value:
            self._raise_last_error("CreateJobObjectW failed")
        limits = _EXTENDED_LIMITS()
        limits.basic.flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            ctypes.c_void_p(value),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self._kernel.CloseHandle(ctypes.c_void_p(value))
            self._raise_last_error("SetInformationJobObject failed")
        return value

    def create_gate_event(self, name: str) -> object:
        value = self._handle_value(self._kernel.CreateEventW(None, True, False, name))
        if not value:
            self._raise_last_error("CreateEventW for bootstrap gate failed")
        if _get_last_error() == _ERROR_ALREADY_EXISTS:
            self._close_handle(value)
            raise OSError("Windows bootstrap gate name already exists")
        return value

    def signal_gate_event(self, event: object) -> None:
        if not self._kernel.SetEvent(ctypes.c_void_p(self._job_value(event))):
            self._raise_last_error("SetEvent for bootstrap gate failed")

    def close_gate_event(self, event: object) -> None:
        self._close_handle(self._job_value(event))

    def child_gate_is_ready(self, name: str) -> bool:
        value = self._handle_value(self._kernel.OpenEventW(_SYNCHRONIZE, False, name))
        if not value:
            code = _get_last_error()
            if code == _ERROR_FILE_NOT_FOUND:
                return False
            self._raise_error(code, "OpenEventW for child readiness failed")
        try:
            result = self._kernel.WaitForSingleObject(ctypes.c_void_p(value), 0)
            if result == _WAIT_OBJECT_0:
                return True
            if result == _WAIT_TIMEOUT:
                return False
            self._raise_last_error("WaitForSingleObject for child readiness failed")
        finally:
            self._close_handle(value)
        return False

    def close_job(self, job: object) -> None:
        self._close_handle(self._job_value(job))

    def open_process(
        self,
        process_id: int,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle:
        access = _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
        if assign_to_job:
            access |= _PROCESS_SET_QUOTA
        value = self._handle_value(self._kernel.OpenProcess(access, False, process_id))
        if not value:
            self._raise_last_error(f"OpenProcess({process_id}) failed")
        try:
            identity = self._identity(value, process_id)
        except Exception:
            self._close_handle(value)
            raise
        return WindowsProcessHandle(identity, value)

    def duplicate_process(
        self,
        process_id: int,
        native_handle: object,
        *,
        assign_to_job: bool = False,
    ) -> WindowsProcessHandle:
        source = self._job_value(native_handle)
        access = _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE
        if assign_to_job:
            access |= _PROCESS_SET_QUOTA
        current = self._kernel.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not self._kernel.DuplicateHandle(
            current,
            ctypes.c_void_p(source),
            current,
            ctypes.byref(duplicate),
            access,
            False,
            0,
        ):
            self._raise_last_error("DuplicateHandle for spawned process failed")
        value = self._handle_value(duplicate)
        try:
            actual_process_id = int(self._kernel.GetProcessId(ctypes.c_void_p(value)))
            if actual_process_id == 0:
                self._raise_last_error("GetProcessId for duplicated process handle failed")
            if actual_process_id != process_id:
                raise OSError("duplicated process handle does not match the spawned PID")
            identity = self._identity(value, process_id)
        except Exception:
            self._close_handle(value)
            raise
        return WindowsProcessHandle(identity, value)

    def open_process_if_alive(self, process_id: int) -> WindowsProcessHandle | None:
        try:
            return self.open_process(process_id)
        except OSError as error:
            if self._missing_process(error):
                return None
            raise

    def close_process(self, process: WindowsProcessHandle) -> None:
        self._close_handle(self._process_value(process))

    def assign_process(self, job: object, process: WindowsProcessHandle) -> None:
        if not self._kernel.AssignProcessToJobObject(
            ctypes.c_void_p(self._job_value(job)),
            ctypes.c_void_p(self._process_value(process)),
        ):
            self._raise_last_error("AssignProcessToJobObject failed")

    def process_is_in_job(self, job: object, process: WindowsProcessHandle) -> bool:
        result = wintypes.BOOL()
        if not self._kernel.IsProcessInJob(
            ctypes.c_void_p(self._process_value(process)),
            ctypes.c_void_p(self._job_value(job)),
            ctypes.byref(result),
        ):
            self._raise_last_error("IsProcessInJob failed")
        return bool(result.value)

    def process_is_alive(self, process: WindowsProcessHandle) -> bool:
        value = self._process_value(process)
        if self._identity(value, process.identity.process_id) != process.identity:
            return False
        result = self._kernel.WaitForSingleObject(ctypes.c_void_p(value), 0)
        if result == _WAIT_TIMEOUT:
            return True
        if result == _WAIT_OBJECT_0:
            return False
        if result == _WAIT_FAILED:
            self._raise_last_error("WaitForSingleObject liveness check failed")
        raise OSError(f"unexpected WaitForSingleObject result: {result}")

    def terminate_process(self, process: WindowsProcessHandle, exit_code: int) -> None:
        if not self.process_is_alive(process):
            return
        if not self._kernel.TerminateProcess(
            ctypes.c_void_p(self._process_value(process)),
            exit_code,
        ):
            code = _get_last_error()
            if code == _ERROR_ACCESS_DENIED and not self.process_is_alive(process):
                return
            self._raise_error(code, "TerminateProcess failed")

    def wait_processes(
        self,
        processes: tuple[WindowsProcessHandle, ...],
        timeout_ms: int,
    ) -> bool:
        if timeout_ms < 0:
            raise ValueError("process wait timeout cannot be negative")
        deadline = time.monotonic() + timeout_ms / 1000
        for process in processes:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            result = self._kernel.WaitForSingleObject(
                ctypes.c_void_p(self._process_value(process)),
                remaining,
            )
            if result == _WAIT_TIMEOUT:
                return False
            if result != _WAIT_OBJECT_0:
                self._raise_last_error("WaitForSingleObject failed")
        return True

    def active_job_processes(self, job: object) -> int:
        accounting = _BASIC_ACCOUNTING()
        if not self._kernel.QueryInformationJobObject(
            ctypes.c_void_p(self._job_value(job)),
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            self._raise_last_error("QueryInformationJobObject failed")
        return int(accounting.active_processes)

    def job_process_ids(
        self,
        job: object,
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]:
        capacity = max(8, self.active_job_processes(job) + 4)
        for _attempt in range(16):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Windows Job process enumeration deadline expired")
            size = 8 + capacity * ctypes.sizeof(ctypes.c_size_t)
            buffer = ctypes.create_string_buffer(size)
            if self._kernel.QueryInformationJobObject(
                ctypes.c_void_p(self._job_value(job)),
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS,
                buffer,
                size,
                None,
            ):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Windows Job process enumeration deadline expired")
                count = int.from_bytes(buffer.raw[4:8], "little")
                values = (ctypes.c_size_t * count).from_buffer(buffer, 8)
                return tuple(int(value) for value in values)
            code = _get_last_error()
            if code != _ERROR_MORE_DATA:
                self._raise_error(code, "QueryInformationJobObject process list failed")
            assigned = int.from_bytes(buffer.raw[:4], "little")
            capacity = max(capacity * 2, assigned + 4)
        raise OSError("Windows Job process list did not stabilize")

    def terminate_job(self, job: object, exit_code: int) -> None:
        if not self._kernel.TerminateJobObject(
            ctypes.c_void_p(self._job_value(job)),
            exit_code,
        ):
            self._raise_last_error("TerminateJobObject failed")

    def _identity(self, handle: int, process_id: int) -> WindowsProcessIdentity:
        created = _FILETIME()
        exited = _FILETIME()
        kernel = _FILETIME()
        user = _FILETIME()
        if not self._kernel.GetProcessTimes(
            ctypes.c_void_p(handle),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            self._raise_last_error("GetProcessTimes failed")
        return WindowsProcessIdentity(
            process_id,
            (int(created.high) << 32) | int(created.low),
        )

    def _close_handle(self, value: int) -> None:
        if value and not self._kernel.CloseHandle(ctypes.c_void_p(value)):
            self._raise_last_error("CloseHandle failed")

    @staticmethod
    def _process_value(process: WindowsProcessHandle) -> int:
        if not isinstance(process.value, int) or process.value <= 0:
            raise ValueError("invalid Windows process handle")
        return process.value

    @staticmethod
    def _job_value(job: object) -> int:
        if not isinstance(job, int) or job <= 0:
            raise ValueError("invalid Windows Job Object handle")
        return job

    @staticmethod
    def _handle_value(handle: object) -> int:
        if isinstance(handle, int):
            return handle
        value = ctypes.cast(cast(Any, handle), ctypes.c_void_p).value
        return 0 if value is None else int(value)

    @staticmethod
    def _missing_process(error: OSError) -> bool:
        return getattr(error, "winerror", error.errno) == _ERROR_INVALID_PARAMETER

    @staticmethod
    def _raise_last_error(message: str) -> None:
        CtypesWindowsProcessApi._raise_error(_get_last_error(), message)

    @staticmethod
    def _raise_error(code: int, message: str) -> None:
        format_error = cast(Any, ctypes.__dict__["FormatError"])
        raise OSError(code, f"{message}: {format_error(code)}")


def _get_last_error() -> int:
    getter = cast(Any, ctypes.__dict__["get_last_error"])
    return int(getter())
