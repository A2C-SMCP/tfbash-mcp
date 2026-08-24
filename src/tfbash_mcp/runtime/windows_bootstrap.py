"""Trusted Windows launch gate used by the ConPTY process supervisor.

This file is executed directly with isolated Python.  It intentionally uses
only the standard library and never imports the tfbash_mcp package.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from ctypes import wintypes
from typing import Any, NoReturn

_GATE_NAME_ENV = "TFBASH_MCP_GATE_NAME"
_GATE_PAYLOAD_ENV = "TFBASH_MCP_GATE_PAYLOAD"
_GATE_TIMEOUT_ENV = "TFBASH_MCP_GATE_TIMEOUT_MS"
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_ALREADY_EXISTS = 183


def _target(environment: Mapping[str, str]) -> tuple[list[str], int]:
    encoded = environment[_GATE_PAYLOAD_ENV]
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"executable", "arguments"}:
        raise ValueError("invalid bootstrap target")
    executable = payload["executable"]
    arguments = payload["arguments"]
    if (
        not isinstance(executable, str)
        or not executable
        or "\x00" in executable
        or not isinstance(arguments, list)
        or any(not isinstance(value, str) or "\x00" in value for value in arguments)
    ):
        raise ValueError("invalid bootstrap target")
    timeout_ms = int(environment[_GATE_TIMEOUT_ENV])
    if timeout_ms <= 0 or timeout_ms > 60_000:
        raise ValueError("invalid bootstrap gate timeout")
    return [executable, *arguments], timeout_ms


def _wait_for_gate(name: str, timeout_ms: int) -> bool:
    if os.name != "nt":
        raise OSError("Windows bootstrap requires Win32")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    event = kernel.OpenEventW(_SYNCHRONIZE, False, name)
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        result = int(kernel.WaitForSingleObject(event, timeout_ms))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    finally:
        kernel.CloseHandle(event)


def _ignore_console_interrupts() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sigbreak: Any = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.SIG_IGN)


def _child_creation_time(child: Any) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.GetProcessId.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessId.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = int(child._handle)
    if int(kernel.GetProcessId(handle)) != int(child.pid):
        raise OSError("bootstrap child process handle does not match its PID")
    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    if not kernel.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return int(created.dwLowDateTime) | (int(created.dwHighDateTime) << 32)


def _create_child_ready_event(
    name: str,
    process_id: int,
    creation_time_100ns: int,
) -> tuple[Any, Any]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.CreateEventW.argtypes = (
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    event = kernel.CreateEventW(
        None,
        True,
        True,
        f"{name}-child-{process_id}-{creation_time_100ns}",
    )
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:  # type: ignore[attr-defined]
        kernel.CloseHandle(event)
        raise OSError("bootstrap child readiness event already exists")
    return kernel, event


def _run(
    environment: Mapping[str, str],
    *,
    gate_waiter: Any = _wait_for_gate,
    process_factory: Any = subprocess.Popen,
    ready_event_factory: Any = _create_child_ready_event,
    interrupt_ignorer: Any = _ignore_console_interrupts,
    identity_factory: Any = _child_creation_time,
) -> int:
    target, timeout_ms = _target(environment)
    gate_name = environment[_GATE_NAME_ENV]
    if not gate_name.startswith("Local\\tfbash-mcp-") or "\x00" in gate_name:
        raise ValueError("invalid bootstrap gate name")
    if not gate_waiter(gate_name, timeout_ms):
        return 124
    child_environment = dict(environment)
    for key in tuple(child_environment):
        if key.casefold() in {
            _GATE_NAME_ENV.casefold(),
            _GATE_PAYLOAD_ENV.casefold(),
            _GATE_TIMEOUT_ENV.casefold(),
        }:
            del child_environment[key]
    child = process_factory(target, env=child_environment)
    kernel: Any = None
    ready_event: Any = None
    try:
        # The target inherited the default Ctrl-C disposition.  Only the
        # bootstrap ignores later interrupts so it can keep pywinpty alive.
        interrupt_ignorer()
        creation_time = identity_factory(child)
        kernel, ready_event = ready_event_factory(
            gate_name,
            int(child.pid),
            creation_time,
        )
        return int(child.wait())
    finally:
        if ready_event is not None:
            kernel.CloseHandle(ready_event)


def _fail(message: str) -> NoReturn:
    print(f"tfbash-mcp Windows bootstrap failed: {message}", file=sys.stderr)
    raise SystemExit(125)


def main() -> int:
    try:
        return _run(os.environ)
    except Exception as error:
        _fail(type(error).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
