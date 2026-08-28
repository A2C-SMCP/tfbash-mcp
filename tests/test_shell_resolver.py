from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from importlib import import_module
from pathlib import Path

import pytest

import tfbash_mcp.runtime.resolver as resolver_module
from tfbash_mcp.runtime import (
    DialectName,
    NativePlatform,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimeSelection,
    ShellResolution,
    resolve_shell,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _pwsh_runner(
    identity: str,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "TFBASH_CAPABILITY" in command[-1]:
            cwd = str(kwargs["cwd"])
            return subprocess.CompletedProcess(
                command,
                37,
                stdout=f"TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n{cwd}\n",
                stderr="",
            )
        return _completed(identity)

    return run


def _bash_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    if "TFBASH_CAPABILITY" in command[-1]:
        cwd = str(kwargs["cwd"])
        return subprocess.CompletedProcess(
            command,
            37,
            stdout=f"TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n{cwd}\n",
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
    ("platform", "expected"),
    [
        (NativePlatform.MACOS, [DialectName.ZSH, DialectName.BASH]),
        (NativePlatform.LINUX, [DialectName.BASH, DialectName.ZSH]),
        (NativePlatform.WINDOWS, [DialectName.PWSH, DialectName.BASH]),
    ],
)
def test_auto_discovery_uses_platform_candidate_order(
    monkeypatch: pytest.MonkeyPatch,
    platform: NativePlatform,
    expected: list[DialectName],
) -> None:
    seen: list[DialectName] = []

    def candidates_for(
        dialect: DialectName,
        _platform: NativePlatform,
        _environment: object,
    ) -> tuple[resolver_module._Candidate, ...]:
        seen.append(dialect)
        return (resolver_module._Candidate(dialect, f"/{dialect.value}", "test"),)

    monkeypatch.setattr(resolver_module, "_candidates_for", candidates_for)

    candidates = resolver_module._discovered_candidates(
        RuntimeSelection.AUTO,
        platform,
        {},
    )

    assert seen == expected
    assert [candidate.dialect for candidate in candidates] == expected


def test_explicit_profile_discovers_only_requested_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[DialectName] = []

    def candidates_for(
        dialect: DialectName,
        _platform: NativePlatform,
        _environment: object,
    ) -> tuple[resolver_module._Candidate, ...]:
        seen.append(dialect)
        return ()

    monkeypatch.setattr(resolver_module, "_candidates_for", candidates_for)

    resolver_module._discovered_candidates(
        RuntimeSelection.ZSH,
        NativePlatform.WINDOWS,
        {},
    )

    assert seen == [DialectName.ZSH]


def test_posix_discovery_prefers_system_shell_before_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os.path, "isfile", lambda path: path == "/bin/bash")
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "/usr/local/bin/bash")

    candidates = tuple(
        resolver_module._candidates_for(
            DialectName.BASH,
            NativePlatform.LINUX,
            {"PATH": "/usr/local/bin:/usr/bin"},
        )
    )

    assert [(candidate.executable, candidate.source) for candidate in candidates] == [
        ("/bin/bash", "system"),
        ("/usr/local/bin/bash", "PATH"),
    ]


def test_posix_powershell_discovery_checks_path_and_common_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "/custom/bin/pwsh")
    monkeypatch.setattr(
        "tfbash_mcp.runtime.resolver.glob.glob",
        lambda _pattern: ["/opt/microsoft/powershell/7/pwsh"],
    )
    monkeypatch.setattr(
        os.path,
        "isfile",
        lambda path: path in {"/usr/bin/pwsh", "/opt/microsoft/powershell/7/pwsh"},
    )

    candidates = tuple(resolver_module._candidates_for(DialectName.PWSH, NativePlatform.LINUX, {}))

    assert [candidate.executable for candidate in candidates] == [
        "/custom/bin/pwsh",
        "/usr/bin/pwsh",
        "/opt/microsoft/powershell/7/pwsh",
    ]


def test_windows_powershell_discovery_orders_core_registry_and_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = resolver_module._Candidate(
        DialectName.PWSH,
        r"D:\PowerShell\pwsh.exe",
        "registry",
    )
    desktop = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: r"C:\Path\pwsh.exe")
    monkeypatch.setattr(
        "tfbash_mcp.runtime.resolver.glob.glob",
        lambda _pattern: [
            r"C:\Program Files\PowerShell\7.4.9\pwsh.exe",
            r"C:\Program Files\PowerShell\7.10.1\pwsh.exe",
        ],
    )
    monkeypatch.setattr(
        resolver_module,
        "_powershell_registry_candidates",
        lambda: (registry,),
    )
    monkeypatch.setattr(os.path, "expandvars", lambda _path: desktop)
    monkeypatch.setattr(os.path, "isfile", lambda path: path == desktop)

    candidates = tuple(
        resolver_module._candidates_for(DialectName.PWSH, NativePlatform.WINDOWS, {})
    )

    assert [candidate.executable for candidate in candidates] == [
        r"C:\Path\pwsh.exe",
        r"C:\Program Files\PowerShell\7.10.1\pwsh.exe",
        r"C:\Program Files\PowerShell\7.4.9\pwsh.exe",
        r"D:\PowerShell\pwsh.exe",
        desktop,
    ]


def test_windows_posix_shell_discovery_checks_path_and_common_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: r"D:\bin\zsh.exe")
    monkeypatch.setattr(os.path, "expandvars", lambda path: path)
    monkeypatch.setattr(
        os.path,
        "isfile",
        lambda path: "msys64" in path.casefold() and path.endswith("zsh.exe"),
    )

    candidates = tuple(resolver_module._candidates_for(DialectName.ZSH, NativePlatform.WINDOWS, {}))

    assert candidates[0].executable == r"D:\bin\zsh.exe"
    assert candidates[0].source == "PATH"
    assert any(candidate.source == "Git Bash/MSYS2" for candidate in candidates[1:])


def test_windows_registry_discovery_closes_root_and_ignores_invalid_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKey:
        def __init__(self, name: str) -> None:
            self.name = name

        def __enter__(self) -> FakeKey:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeWinreg:
        HKEY_LOCAL_MACHINE = object()

        def __init__(self) -> None:
            self.root = FakeKey("root")
            self.closed = False

        def OpenKey(self, root: object, name: str) -> FakeKey:
            if root is self.HKEY_LOCAL_MACHINE:
                return self.root
            return FakeKey(name)

        def EnumKey(self, _root: FakeKey, index: int) -> str:
            if index < 2:
                return ("valid", "missing")[index]
            raise OSError

        def QueryValueEx(self, key: FakeKey, _name: str) -> tuple[str, None]:
            if key.name == "missing":
                raise OSError
            return (r"C:\RegistryPowerShell", None)

        def CloseKey(self, root: FakeKey) -> None:
            assert root is self.root
            self.closed = True

    fake_winreg = FakeWinreg()
    real_import_module = import_module
    monkeypatch.setattr("tfbash_mcp.runtime.resolver.os.name", "nt")
    monkeypatch.setattr(
        "importlib.import_module",
        lambda name: fake_winreg if name == "winreg" else real_import_module(name),
    )
    monkeypatch.setattr(os.path, "isfile", lambda _path: True)

    candidates = tuple(resolver_module._powershell_registry_candidates())

    assert [candidate.executable for candidate in candidates] == [
        os.path.join(r"C:\RegistryPowerShell", "pwsh.exe")
    ]
    assert fake_winreg.closed is True


def test_identity_and_capability_failures_fall_back_without_leaking_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "/secret/customer/pwsh"
    monkeypatch.setattr(
        resolver_module,
        "_discovered_candidates",
        lambda *args, **kwargs: (
            resolver_module._Candidate(DialectName.PWSH, secret, "test"),
            resolver_module._Candidate(DialectName.BASH, "/bin/bash", "test"),
        ),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == secret:
            raise OSError(f"cannot execute {secret}")
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


def test_capability_probe_rejects_wrong_cwd_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "_discovered_candidates",
        lambda *args, **kwargs: (
            resolver_module._Candidate(DialectName.ZSH, "/bin/zsh", "test"),
            resolver_module._Candidate(DialectName.BASH, "/bin/bash", "test"),
        ),
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/bin/zsh" and "TFBASH_CAPABILITY" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                37,
                stdout="TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/wrong\n",
                stderr="",
            )
        if command[0] == "/bin/zsh":
            return _completed("zsh 5.9")
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
    ("stdout", "returncode"),
    [
        ("TFBASH_CAPABILITY_中文🙂 line1 line2 环境中文🙂 /workspace\n", 37),
        ("wrong-marker\nline1\nline2\n环境中文🙂\n/workspace\n", 37),
        ("TFBASH_CAPABILITY_中文🙂\nline1\nline2\nwrong-env\n/workspace\n", 37),
        ("TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/extra\n/workspace\n", 37),
        ("TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n/workspace\n", 0),
    ],
)
def test_capability_probe_requires_exact_values_and_line_boundaries(
    stdout: str,
    returncode: int,
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "TFBASH_CAPABILITY" in command[-1]:
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")
        return _completed("GNU bash, version 5.2.37")

    with pytest.raises(RuntimeConfigurationError, match="found no compatible"):
        resolve_shell(
            RuntimeSelection.BASH,
            platform=NativePlatform.LINUX,
            explicit_shell="/bin/bash",
            cwd="/workspace",
            environment={},
            timeout_ms=1_000,
            runner=run,
        )


def test_windows_auto_falls_through_every_candidate_stage_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_core = r"C:\Path\pwsh.exe"
    install_core = r"C:\Program Files\PowerShell\7.10.1\pwsh.exe"
    registry_core = r"D:\RegistryPowerShell\pwsh.exe"
    desktop = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    candidates = (
        resolver_module._Candidate(DialectName.PWSH, path_core, "PATH"),
        resolver_module._Candidate(DialectName.PWSH, install_core, "PowerShell install"),
        resolver_module._Candidate(DialectName.PWSH, registry_core, "registry"),
        resolver_module._Candidate(DialectName.PWSH, desktop, "Windows PowerShell"),
        resolver_module._Candidate(DialectName.BASH, git_bash, "Git Bash/MSYS2"),
    )
    monkeypatch.setattr(
        resolver_module,
        "_discovered_candidates",
        lambda *args, **kwargs: candidates,
    )
    identity_attempts: list[str] = []
    admitted: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        executable = command[0]
        capability = "TFBASH_CAPABILITY" in command[-1]
        if not capability:
            identity_attempts.append(executable)
        if executable == path_core:
            raise OSError("PATH Core is broken")
        if executable == install_core:
            return _completed("unrecognized PowerShell output")
        if executable == registry_core:
            if capability:
                return subprocess.CompletedProcess(
                    command,
                    37,
                    stdout=(
                        f"TFBASH_CAPABILITY_中文🙂\nline1\nline2\nwrong-env\n{kwargs['cwd']}\n"
                    ),
                    stderr="",
                )
            return _completed("Core|7.9.0|")
        if executable == desktop:
            return _pwsh_runner("Desktop|5.1.22621.4391|")(command, **kwargs)
        return _bash_runner(command, **kwargs)

    def admit(resolution: ShellResolution) -> None:
        executable = resolution.executable
        admitted.append(executable)
        if executable == desktop:
            raise RuntimeConfigurationError("managed Desktop admission failed")

    resolution = resolve_shell(
        RuntimeSelection.AUTO,
        platform=NativePlatform.WINDOWS,
        explicit_shell=None,
        cwd=r"C:\workspace",
        environment={},
        timeout_ms=1_000,
        runner=run,
        admit=admit,
    )

    assert identity_attempts == [
        path_core,
        install_core,
        registry_core,
        desktop,
        git_bash,
    ]
    assert admitted == [desktop, git_bash]
    assert resolution.dialect is DialectName.BASH
    assert resolution.executable == git_bash


def test_windows_cleanup_failure_stops_before_git_bash_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    monkeypatch.setattr(
        resolver_module,
        "_discovered_candidates",
        lambda *args, **kwargs: (
            resolver_module._Candidate(DialectName.PWSH, desktop, "Windows PowerShell"),
            resolver_module._Candidate(DialectName.BASH, git_bash, "Git Bash/MSYS2"),
        ),
    )
    seen: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(command[0])
        return _pwsh_runner("Desktop|5.1.22621.4391|")(command, **kwargs)

    def fail_cleanup(_resolution: object) -> None:
        raise resolver_module.RoutingCleanupError("native handle remains")

    with pytest.raises(RuntimeConfigurationError, match="automatic routing stopped"):
        resolve_shell(
            RuntimeSelection.AUTO,
            platform=NativePlatform.WINDOWS,
            explicit_shell=None,
            cwd=r"C:\workspace",
            environment={},
            timeout_ms=1_000,
            runner=run,
            admit=fail_cleanup,
        )

    assert git_bash not in seen


def test_no_compatible_shell_error_is_redacted() -> None:
    secret = "/secret/customer/bash"

    with pytest.raises(RuntimeConfigurationError) as captured:
        resolve_shell(
            RuntimeSelection.BASH,
            platform=NativePlatform.LINUX,
            explicit_shell=secret,
            cwd="/workspace",
            environment={},
            timeout_ms=1_000,
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(OSError(secret)),
        )

    assert isinstance(captured.value.__cause__, OSError)

    assert secret not in str(captured.value)
    assert "OSError" in str(captured.value)


def test_candidate_deduplication_and_windows_path_comparison() -> None:
    candidates = resolver_module._deduplicate(
        (
            resolver_module._Candidate(DialectName.BASH, "/bin/bash", "system"),
            resolver_module._Candidate(DialectName.BASH, "/bin/bash", "PATH"),
        )
    )

    assert len(candidates) == 1
    assert resolver_module.native_paths_equal(
        r"C:\Workspace\Project",
        "c:/workspace/project",
        windows=True,
    )


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
        if "TFBASH_CAPABILITY" in command[-1]:
            cwd = str(kwargs["cwd"])
            return subprocess.CompletedProcess(
                command,
                37,
                stdout=f"TFBASH_CAPABILITY_中文🙂\nline1\nline2\n环境中文🙂\n{cwd}\n",
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


def test_explicit_shell_must_match_the_requested_profile() -> None:
    with pytest.raises(RuntimeConfigurationError, match="does not match"):
        resolve_shell(
            RuntimeSelection.ZSH,
            platform=NativePlatform.LINUX,
            explicit_shell="/bin/bash",
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
