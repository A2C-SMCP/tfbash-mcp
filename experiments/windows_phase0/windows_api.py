"""Minimal Win32 ownership primitives used only by the Phase 0 experiment."""

from __future__ import annotations

import ctypes
import os
import subprocess
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from time import monotonic
from typing import Final

TH32CS_SNAPPROCESS: Final = 0x00000002
INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
PROCESS_TERMINATE: Final = 0x0001
PROCESS_SET_QUOTA: Final = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
SYNCHRONIZE: Final = 0x00100000
WAIT_OBJECT_0: Final = 0x00000000
WAIT_TIMEOUT: Final = 0x00000102
INFINITE: Final = 0xFFFFFFFF
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
ERROR_NO_MORE_FILES: Final = 18


class FILETIME(ctypes.Structure):
    _fields_ = (("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD))


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    )


class IO_COUNTERS(ctypes.Structure):
    _fields_ = tuple(
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    )


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """PID plus creation time, preventing control after PID reuse."""

    pid: int
    creation_time_100ns: int


@dataclass(frozen=True, slots=True)
class ProcessEntry:
    """One Toolhelp process snapshot entry."""

    pid: int
    parent_pid: int
    executable: str


class OwnedHandle:
    """Small deterministic wrapper around a Win32 HANDLE."""

    def __init__(self, kernel32: ctypes.WinDLL, value: int) -> None:
        self._kernel32 = kernel32
        self.value = value

    def close(self) -> None:
        if self.value:
            self._kernel32.CloseHandle(ctypes.c_void_p(self.value))
            self.value = 0

    def __enter__(self) -> OwnedHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise RuntimeError("Win32 experiment primitives require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.WaitForMultipleObjects.argtypes = (
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.BOOL),
    )
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    return kernel32


def _handle_value(handle: object) -> int:
    value = ctypes.cast(handle, ctypes.c_void_p).value
    return 0 if value is None else value


def _raise_last_error(message: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{message}: {ctypes.FormatError(error)}")


def _filetime_value(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def open_process(pid: int, access: int) -> OwnedHandle:
    """Open a process using an explicit least-privilege access mask."""

    kernel32 = _kernel32()
    raw = kernel32.OpenProcess(access, False, pid)
    value = _handle_value(raw)
    if not value:
        _raise_last_error(f"OpenProcess({pid}) failed")
    return OwnedHandle(kernel32, value)


def process_identity(pid: int) -> ProcessIdentity:
    """Capture a process identity that remains safe across PID reuse."""

    with open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION) as process:
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not process._kernel32.GetProcessTimes(
            ctypes.c_void_p(process.value),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            _raise_last_error(f"GetProcessTimes({pid}) failed")
    return ProcessIdentity(pid=pid, creation_time_100ns=_filetime_value(created))


def identity_is_alive(identity: ProcessIdentity) -> bool:
    """Return true only if the same process creation identity is still alive."""

    try:
        current = process_identity(identity.pid)
    except OSError:
        return False
    return current == identity


def process_snapshot() -> tuple[ProcessEntry, ...]:
    """Take one Toolhelp snapshot without repeatedly polling process state."""

    kernel32 = _kernel32()
    raw = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    value = _handle_value(raw)
    if not value or value == INVALID_HANDLE_VALUE:
        _raise_last_error("CreateToolhelp32Snapshot failed")

    entries: list[ProcessEntry] = []
    with OwnedHandle(kernel32, value) as snapshot:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(ctypes.c_void_p(snapshot.value), ctypes.byref(entry)):
            _raise_last_error("Process32FirstW failed")
        while True:
            entries.append(
                ProcessEntry(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                    executable=entry.szExeFile,
                )
            )
            if not kernel32.Process32NextW(ctypes.c_void_p(snapshot.value), ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != ERROR_NO_MORE_FILES:
                    _raise_last_error("Process32NextW failed")
                break
    return tuple(entries)


def descendant_identities(root: ProcessIdentity) -> tuple[ProcessIdentity, ...]:
    """Resolve the current Toolhelp descendant tree behind a creation-time fence."""

    if not identity_is_alive(root):
        return ()
    children: dict[int, list[int]] = {}
    for entry in process_snapshot():
        children.setdefault(entry.parent_pid, []).append(entry.pid)

    pending = list(children.get(root.pid, []))
    descendants: list[ProcessIdentity] = []
    while pending:
        pid = pending.pop()
        pending.extend(children.get(pid, []))
        try:
            descendants.append(process_identity(pid))
        except OSError:
            continue
    return tuple(descendants)


def wait_for_exit(identities: Iterable[ProcessIdentity], timeout_seconds: float) -> bool:
    """Wait on process handles; PID reuse is treated as the original process having exited."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    kernel32 = _kernel32()
    handles: list[OwnedHandle] = []
    try:
        for identity in identities:
            if not identity_is_alive(identity):
                continue
            try:
                handle = open_process(identity.pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
            except OSError:
                continue
            handles.append(handle)

        if not handles:
            return True
        if len(handles) > 64:
            raise ValueError("WaitForMultipleObjects supports at most 64 process handles")
        values = (wintypes.HANDLE * len(handles))(
            *(ctypes.c_void_p(handle.value) for handle in handles)
        )
        milliseconds = min(round(timeout_seconds * 1000), INFINITE - 1)
        result = kernel32.WaitForMultipleObjects(len(handles), values, True, milliseconds)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        _raise_last_error("WaitForMultipleObjects failed")
    finally:
        for handle in handles:
            handle.close()
    return False


def taskkill_tree(
    root: ProcessIdentity, *, force: bool, timeout_seconds: float
) -> dict[str, object]:
    """Terminate an identity-fenced Toolhelp tree using the candidate A mechanism."""

    discovered = descendant_identities(root)
    if not identity_is_alive(root):
        return {"returncode": 0, "discovered": len(discovered), "all_exited": True}
    command = ["taskkill", "/PID", str(root.pid), "/T"]
    if force:
        command.append("/F")
    started = monotonic()
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    identities = (root, *discovered)
    all_exited = wait_for_exit(identities, max(0.0, timeout_seconds - (monotonic() - started)))
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "discovered": len(discovered),
        "all_exited": all_exited,
        "survivors": [identity.pid for identity in identities if identity_is_alive(identity)],
    }


class KillOnCloseJob:
    """Candidate B: a non-breakaway Job Object with kill-on-close semantics."""

    def __init__(self, name: str) -> None:
        self._kernel32 = _kernel32()
        raw = self._kernel32.CreateJobObjectW(None, name)
        value = _handle_value(raw)
        if not value:
            _raise_last_error("CreateJobObjectW failed")
        self._handle = OwnedHandle(self._kernel32, value)

        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            ctypes.c_void_p(self._handle.value),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            _raise_last_error("SetInformationJobObject failed")

    def assign_pid(self, pid: int) -> ProcessIdentity:
        """Assign the spawned shell before any user/startup command is sent."""

        identity = process_identity(pid)
        access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        with open_process(pid, access) as process:
            if not self._kernel32.AssignProcessToJobObject(
                ctypes.c_void_p(self._handle.value), ctypes.c_void_p(process.value)
            ):
                _raise_last_error(f"AssignProcessToJobObject({pid}) failed")
            in_job = wintypes.BOOL()
            if not self._kernel32.IsProcessInJob(
                ctypes.c_void_p(process.value),
                ctypes.c_void_p(self._handle.value),
                ctypes.byref(in_job),
            ):
                _raise_last_error(f"IsProcessInJob({pid}) failed")
            if not in_job.value:
                raise RuntimeError(f"process {pid} was not associated with the created Job Object")
        return identity

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle.value and not self._kernel32.TerminateJobObject(
            ctypes.c_void_p(self._handle.value), exit_code
        ):
            _raise_last_error("TerminateJobObject failed")

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> KillOnCloseJob:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class NamedManualResetEvent:
    """Named event used by the fixture instead of a sleep-loop."""

    def __init__(self, name: str) -> None:
        self._kernel32 = _kernel32()
        raw = self._kernel32.CreateEventW(None, True, False, name)
        value = _handle_value(raw)
        if not value:
            _raise_last_error("CreateEventW failed")
        self._handle = OwnedHandle(self._kernel32, value)
        self.name = name

    def set(self) -> None:
        if not self._kernel32.SetEvent(ctypes.c_void_p(self._handle.value)):
            _raise_last_error("SetEvent failed")

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> NamedManualResetEvent:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
