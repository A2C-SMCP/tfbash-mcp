"""Emit a token only after the runner releases a named Win32 event."""

from __future__ import annotations

import argparse
import ctypes
import os
from ctypes import wintypes

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
INFINITE = 0xFFFFFFFF


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--before-wait-token", required=True)
    parser.add_argument("--after-wait-token", required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("late-output fixture requires Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenEventW(SYNCHRONIZE, False, args.event)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"OpenEventW failed: {ctypes.FormatError(error)}")
    try:
        print(args.before_wait_token, flush=True)
        result = kernel32.WaitForSingleObject(handle, INFINITE)
        if result != WAIT_OBJECT_0:
            error = ctypes.get_last_error()
            raise OSError(error, f"WaitForSingleObject failed: {ctypes.FormatError(error)}")
        print(args.after_wait_token, flush=True)
    finally:
        kernel32.CloseHandle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
