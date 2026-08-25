"""Three-level Windows process tree fixture for ownership experiments."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
from ctypes import wintypes

EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000


def _kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise RuntimeError("tree fixture requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _report(role: str, host: str, port: int) -> None:
    payload = json.dumps(
        {"role": role, "pid": os.getpid(), "parent_pid": os.getppid()}, sort_keys=True
    ).encode("utf-8")
    with socket.create_connection((host, port), timeout=10.0) as connection:
        connection.sendall(payload + b"\n")


def _spawn(role: str, args: argparse.Namespace) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(__file__),
            "--role",
            role,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--event",
            args.event,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _wait_for_release(name: str) -> None:
    kernel32 = _kernel32()
    handle = kernel32.OpenEventW(SYNCHRONIZE | EVENT_MODIFY_STATE, False, name)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"OpenEventW failed: {ctypes.FormatError(error)}")
    try:
        result = kernel32.WaitForSingleObject(handle, INFINITE)
        if result != WAIT_OBJECT_0:
            error = ctypes.get_last_error()
            raise OSError(error, f"WaitForSingleObject failed: {ctypes.FormatError(error)}")
    finally:
        kernel32.CloseHandle(handle)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("parent", "child", "grandchild"), required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--event", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _report(args.role, args.host, args.port)
    child: subprocess.Popen[bytes] | None = None
    if args.role == "parent":
        child = _spawn("child", args)
    elif args.role == "child":
        child = _spawn("grandchild", args)
    try:
        _wait_for_release(args.event)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)


if __name__ == "__main__":
    main()
