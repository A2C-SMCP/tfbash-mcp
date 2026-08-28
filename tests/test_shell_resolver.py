from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import tfbash_mcp.runtime.resolver as resolver_module
from tfbash_mcp.runtime import (
    DialectName,
    NativePlatform,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimeSelection,
    resolve_shell,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _pwsh_runner(
    identity: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "TFBASH_CAPABILITY" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                37,
                stdout="TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/workspace\n",
                stderr="",
            )
        return _completed(identity)

    return run


def _bash_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    del kwargs
    if "TFBASH_CAPABILITY" in command[-1]:
        return subprocess.CompletedProcess(
            command,
            37,
            stdout="TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/workspace\n",
            stderr="",
        )
    return _completed("GNU bash, version 5.2.37")


def test_explicit_powershell_desktop_is_admitted_on_windows() -> None:
    resolution = resolve_shell(
        RuntimeSelection.PWSH,
        platform=NativePlatform.WINDOWS,
        explicit_shell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        cwd=r"C:\workspace",
        environment={},
        timeout_ms=1_000,
        runner=_pwsh_runner("Desktop|5.1.22621.4391|"),
    )

    assert resolution.runtime is RuntimeName.WINDOWS_PWSH
    assert resolution.dialect is DialectName.PWSH
    assert resolution.edition == "Desktop"
    assert resolution.version.startswith("5.1")


def test_powershell_desktop_other_than_51_is_rejected() -> None:
    with pytest.raises(RuntimeConfigurationError, match="found no compatible"):
        resolve_shell(
            RuntimeSelection.PWSH,
            platform=NativePlatform.WINDOWS,
            explicit_shell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            cwd=r"C:\workspace",
            environment={},
            timeout_ms=1_000,
            runner=_pwsh_runner("Desktop|4.0.0|"),
        )


def test_explicit_powershell_core_is_admitted_on_posix() -> None:
    resolution = resolve_shell(
        RuntimeSelection.PWSH,
        platform=NativePlatform.LINUX,
        explicit_shell="/opt/microsoft/powershell/7/pwsh",
        cwd="/workspace",
        environment={},
        timeout_ms=1_000,
        runner=_pwsh_runner("Core|7.8.0|"),
    )

    assert resolution.runtime is RuntimeName.POSIX_BASH
    assert resolution.dialect is DialectName.PWSH
    assert resolution.version == "7.8.0"


def test_auto_rejects_powershell_prerelease_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "_discovered_candidates",
        lambda *args, **kwargs: (
            resolver_module._Candidate(DialectName.PWSH, "/opt/pwsh-preview", "test"),
            resolver_module._Candidate(DialectName.BASH, "/bin/bash", "test"),
        ),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0].endswith("pwsh-preview"):
            return _pwsh_runner("Core|7.8.0|rc.1")(command, **kwargs)
        return _bash_runner(command, **kwargs)

    resolution = resolve_shell(
        RuntimeSelection.AUTO,
        platform=NativePlatform.LINUX,
        explicit_shell=None,
        cwd="/workspace",
        environment={},
        timeout_ms=1_000,
        runner=run,
    )

    assert resolution.dialect is DialectName.BASH


@pytest.mark.parametrize(
    "path",
    [r"C:\Windows\System32\wsl.exe", r"\\wsl$\Ubuntu\bin\bash"],
)
def test_wsl_is_rejected_with_an_actionable_diagnostic(path: str) -> None:
    with pytest.raises(RuntimeConfigurationError, match="WSL shells are not supported"):
        resolve_shell(
            RuntimeSelection.AUTO,
            platform=NativePlatform.WINDOWS,
            explicit_shell=path,
            cwd=r"C:\workspace",
            environment={},
            timeout_ms=1_000,
        )


def test_macos_auto_prefers_system_zsh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "isfile", lambda path: path in {"/bin/zsh", "/bin/bash"})
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    seen: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "TFBASH_CAPABILITY" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                37,
                stdout="TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/workspace\n",
                stderr="",
            )
        seen.append(command[0])
        return _completed("zsh 5.9 (arm64-apple-darwin)")

    resolution = resolve_shell(
        RuntimeSelection.AUTO,
        platform=NativePlatform.MACOS,
        explicit_shell=None,
        cwd=str(Path.cwd()),
        environment={},
        timeout_ms=1_000,
        runner=run,
    )

    assert seen == ["/bin/zsh"]
    assert resolution.dialect is DialectName.ZSH


def test_explicit_shell_is_strict_and_does_not_fallback() -> None:
    with pytest.raises(RuntimeConfigurationError, match="found no compatible"):
        resolve_shell(
            RuntimeSelection.BASH,
            platform=NativePlatform.LINUX,
            explicit_shell="/secret/bash",
            cwd="/workspace",
            environment={},
            timeout_ms=1_000,
            runner=lambda *args, **kwargs: _completed("not the requested shell"),
        )


def test_explicit_shell_requires_a_native_absolute_path() -> None:
    with pytest.raises(RuntimeConfigurationError, match="native absolute path"):
        resolve_shell(
            RuntimeSelection.BASH,
            platform=NativePlatform.LINUX,
            explicit_shell="bash",
            cwd="/workspace",
            environment={},
            timeout_ms=1_000,
        )


def test_cleanup_failure_stops_auto_routing() -> None:
    def fail_cleanup(_resolution: object) -> None:
        raise resolver_module.RoutingCleanupError("native handle remains")

    with pytest.raises(RuntimeConfigurationError, match="automatic routing stopped"):
        resolve_shell(
            RuntimeSelection.BASH,
            platform=NativePlatform.LINUX,
            explicit_shell="/bin/bash",
            cwd="/workspace",
            environment={},
            timeout_ms=1_000,
            runner=_bash_runner,
            admit=fail_cleanup,
        )
