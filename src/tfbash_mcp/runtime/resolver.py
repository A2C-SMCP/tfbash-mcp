"""Cross-platform shell discovery and capability-first process selection."""

from __future__ import annotations

import glob
import ntpath
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath

from tfbash_mcp.runtime.config import NativePlatform, RuntimeSelection
from tfbash_mcp.runtime.contracts import DialectName, RuntimeName
from tfbash_mcp.runtime.errors import RuntimeConfigurationError


@dataclass(frozen=True, slots=True)
class ShellResolution:
    runtime: RuntimeName
    dialect: DialectName
    executable: str
    version: str
    edition: str | None
    source: str


class RoutingCleanupError(RuntimeConfigurationError):
    """A candidate could not prove cleanup, so routing must stop."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    dialect: DialectName
    executable: str
    source: str


def resolve_shell(
    selection: RuntimeSelection,
    *,
    platform: NativePlatform,
    explicit_shell: str | None,
    cwd: str,
    environment: Mapping[str, str],
    timeout_ms: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    admit: Callable[[ShellResolution], None] | None = None,
) -> ShellResolution:
    """Return the first admitted native shell, or one redacted aggregate error."""

    explicit = explicit_shell is not None
    if explicit_shell is not None and _looks_like_wsl(explicit_shell):
        raise RuntimeConfigurationError(
            "WSL shells are not supported; use native PowerShell or Git Bash on Windows"
        )
    candidates = (
        _explicit_candidate(explicit_shell, selection, platform)
        if explicit_shell is not None
        else _discovered_candidates(selection, platform, environment)
    )
    failures: list[str] = []
    for candidate in _deduplicate(candidates):
        try:
            version, edition = _identity_probe(
                candidate,
                cwd=cwd,
                environment=environment,
                timeout_ms=timeout_ms,
                runner=runner,
            )
            if candidate.dialect is DialectName.PWSH:
                if edition == "Desktop" and platform is not NativePlatform.WINDOWS:
                    raise RuntimeConfigurationError("Windows PowerShell is Windows-only")
                if edition == "Desktop" and _version_prefix(version) != (5, 1):
                    raise RuntimeConfigurationError(
                        "Windows PowerShell Desktop must be version 5.1"
                    )
                if not explicit and "-" in version:
                    raise RuntimeConfigurationError(
                        "prerelease PowerShell requires an explicit --shell"
                    )
            _capability_probe(
                candidate,
                platform=platform,
                cwd=cwd,
                environment=environment,
                timeout_ms=timeout_ms,
                runner=runner,
            )
            resolution = ShellResolution(
                runtime=_runtime_name(platform, candidate.dialect),
                dialect=candidate.dialect,
                executable=_absolute_executable(candidate.executable, platform),
                version=version,
                edition=edition,
                source=candidate.source,
            )
            if admit is not None:
                admit(resolution)
            return resolution
        except RoutingCleanupError as error:
            raise RuntimeConfigurationError(
                "shell candidate cleanup could not be proven; automatic routing stopped"
            ) from error
        except Exception as error:
            failures.append(f"{candidate.dialect.value}: {type(error).__name__}")
    requested = "automatic shell routing" if selection is RuntimeSelection.AUTO else selection.value
    detail = ", ".join(failures) if failures else "no candidates were discovered"
    raise RuntimeConfigurationError(f"{requested} found no compatible native shell ({detail})")


def _explicit_candidate(
    path: str,
    selection: RuntimeSelection,
    platform: NativePlatform,
) -> tuple[_Candidate, ...]:
    absolute = (
        PureWindowsPath(path).is_absolute()
        if platform is NativePlatform.WINDOWS
        else os.path.isabs(path)
    )
    if not absolute:
        raise RuntimeConfigurationError("--shell must be a native absolute path")
    dialect = _infer_dialect(path)
    if selection is not RuntimeSelection.AUTO and dialect.value != selection.value:
        raise RuntimeConfigurationError("--shell does not match --runtime-profile")
    return (_Candidate(dialect, path, "explicit"),)


def _discovered_candidates(
    selection: RuntimeSelection,
    platform: NativePlatform,
    environment: Mapping[str, str],
) -> tuple[_Candidate, ...]:
    order: tuple[DialectName, ...] = (
        (DialectName.PWSH, DialectName.BASH)
        if platform is NativePlatform.WINDOWS
        else (
            (DialectName.ZSH, DialectName.BASH)
            if platform is NativePlatform.MACOS
            else (DialectName.BASH, DialectName.ZSH)
        )
    )
    if selection is not RuntimeSelection.AUTO:
        order = (DialectName(selection.value),)
    candidates: list[_Candidate] = []
    for dialect in order:
        candidates.extend(_candidates_for(dialect, platform, environment))
    return tuple(candidates)


def _candidates_for(
    dialect: DialectName,
    platform: NativePlatform,
    environment: Mapping[str, str],
) -> Iterable[_Candidate]:
    path = next(
        (value for key, value in environment.items() if key.casefold() == "path"),
        None,
    )
    if platform is not NativePlatform.WINDOWS:
        system_path = f"/bin/{dialect.value}"
        if dialect in {DialectName.BASH, DialectName.ZSH} and os.path.isfile(system_path):
            yield _Candidate(dialect, system_path, "system")
        discovered = shutil.which(dialect.value, path=path or "")
        if discovered is not None:
            yield _Candidate(dialect, discovered, "PATH")
        if dialect is DialectName.PWSH:
            common_paths = (
                "/usr/bin/pwsh",
                "/usr/local/bin/pwsh",
                "/opt/homebrew/bin/pwsh",
                *glob.glob("/opt/microsoft/powershell/*/pwsh"),
            )
            for executable in common_paths:
                if os.path.isfile(executable):
                    yield _Candidate(dialect, executable, "PowerShell install")
        return
    if dialect is DialectName.PWSH:
        discovered = shutil.which("pwsh.exe", path=path or "")
        if discovered is not None:
            yield _Candidate(dialect, discovered, "PATH")
        versions = glob.glob(r"C:\Program Files\PowerShell\*\pwsh.exe")
        for executable in sorted(versions, key=_version_sort_key, reverse=True):
            yield _Candidate(dialect, executable, "PowerShell install")
        yield from _powershell_registry_candidates()
        desktop = os.path.expandvars(r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
        if os.path.isfile(desktop):
            yield _Candidate(dialect, desktop, "Windows PowerShell")
        return
    executable_name = f"{dialect.value}.exe"
    discovered = shutil.which(executable_name, path=path or "")
    if discovered is not None:
        yield _Candidate(dialect, discovered, "PATH")
    roots = [
        os.path.expandvars(r"%ProgramFiles%\Git\bin"),
        os.path.expandvars(r"%ProgramFiles%\Git\usr\bin"),
        os.path.expandvars(r"%LocalAppData%\Programs\Git\bin"),
        os.path.expandvars(r"%ProgramFiles%\MSYS2\usr\bin"),
        r"C:\msys64\usr\bin",
    ]
    for root in roots:
        executable = os.path.join(root, executable_name)
        if os.path.isfile(executable):
            yield _Candidate(dialect, executable, "Git Bash/MSYS2")


def _identity_probe(
    candidate: _Candidate,
    *,
    cwd: str,
    environment: Mapping[str, str],
    timeout_ms: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str | None]:
    if candidate.dialect is DialectName.PWSH:
        script = (
            "$e=[string]$PSVersionTable.PSEdition;"
            "$v=[string]$PSVersionTable.PSVersion;"
            "$p=[string]$PSVersionTable.PSVersion.PreReleaseLabel;"
            "[Console]::Out.Write($e+'|'+$v+'|'+$p)"
        )
        command = [
            candidate.executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
    else:
        command = [candidate.executable, "--version"]
    completed = runner(
        command,
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=timeout_ms / 1000,
    )
    if completed.returncode != 0:
        raise RuntimeConfigurationError("identity probe returned a failure")
    output = completed.stdout.strip()
    if candidate.dialect is DialectName.PWSH:
        fields = output.split("|", 2)
        if len(fields) != 3 or fields[0] not in {"Core", "Desktop"} or not fields[1]:
            raise RuntimeConfigurationError("PowerShell identity was not recognized")
        version = fields[1] + (f"-{fields[2]}" if fields[2] else "")
        return version, fields[0]
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    expected = "bash" if candidate.dialect is DialectName.BASH else "zsh"
    if expected not in first_line.casefold():
        raise RuntimeConfigurationError("shell identity did not match its candidate name")
    return first_line, None


def _capability_probe(
    candidate: _Candidate,
    *,
    platform: NativePlatform,
    cwd: str,
    environment: Mapping[str, str],
    timeout_ms: int,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    marker = "TFBASH_CAPABILITY_中文🙂"
    probe_environment = dict(environment)
    probe_environment["TFBASH_ROUTING_PROBE"] = "环境中文🙂"
    if candidate.dialect is DialectName.PWSH:
        script = (
            "$u=[Text.UTF8Encoding]::new($false);"
            "[Console]::InputEncoding=$u;[Console]::OutputEncoding=$u;"
            f"[Console]::Out.WriteLine('{marker}');"
            "[Console]::Out.WriteLine('line1');[Console]::Out.WriteLine('line2');"
            "[Console]::Out.WriteLine($env:TFBASH_ROUTING_PROBE);"
            "[Console]::Out.WriteLine([string](Get-Location).Path);exit 37"
        )
        command = [
            candidate.executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
    else:
        cwd_command = 'cygpath -am "$PWD"' if platform is NativePlatform.WINDOWS else "pwd -P"
        script = (
            "if printf '' | base64 --decode >/dev/null 2>&1; then :; "
            "elif printf '' | base64 -D >/dev/null 2>&1; then :; else exit 125; fi; "
            f"printf '%s\\n' '{marker}' line1 line2 \"$TFBASH_ROUTING_PROBE\"; "
            f"{cwd_command}; (exit 37)"
        )
        arguments = (
            ["--noprofile", "--norc", "-c"]
            if candidate.dialect is DialectName.BASH
            else ["-f", "-d", "-c"]
        )
        command = [candidate.executable, *arguments, script]
    completed = runner(
        command,
        cwd=cwd,
        env=probe_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=timeout_ms / 1000,
    )
    lines = [line.rstrip("\r") for line in completed.stdout.splitlines()]
    expected = [marker, "line1", "line2", "环境中文🙂"]
    reported_cwd = lines[-1] if len(lines) == len(expected) + 1 else ""
    if (
        completed.returncode != 37
        or lines[:-1] != expected
        or not native_paths_equal(
            reported_cwd,
            cwd,
            windows=platform is NativePlatform.WINDOWS,
        )
    ):
        raise RuntimeConfigurationError("shell failed the required capability probe")


def _infer_dialect(path: str) -> DialectName:
    name = PureWindowsPath(path).name.casefold()
    name = re.sub(r"\.(exe|cmd|bat)$", "", name)
    if name in {"pwsh", "powershell"}:
        return DialectName.PWSH
    if name == "bash":
        return DialectName.BASH
    if name == "zsh":
        return DialectName.ZSH
    raise RuntimeConfigurationError("--shell must name bash, zsh, pwsh, or powershell")


def _runtime_name(platform: NativePlatform, dialect: DialectName) -> RuntimeName:
    del dialect
    return (
        RuntimeName.WINDOWS_PWSH if platform is NativePlatform.WINDOWS else RuntimeName.POSIX_BASH
    )


def _absolute_executable(executable: str, platform: NativePlatform) -> str:
    if platform is NativePlatform.WINDOWS:
        return str(PureWindowsPath(executable))
    return os.path.abspath(executable)


def native_paths_equal(left: str, right: str, *, windows: bool) -> bool:
    """Compare native paths while accepting Windows slash and case variants."""

    if windows:
        return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))
    return os.path.realpath(left) == os.path.realpath(right)


def _deduplicate(candidates: Iterable[_Candidate]) -> tuple[_Candidate, ...]:
    seen: set[str] = set()
    result: list[_Candidate] = []
    for candidate in candidates:
        key = os.path.normcase(candidate.executable)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def _version_sort_key(path: str) -> tuple[int, ...]:
    match = re.search(r"PowerShell\\([^\\]+)\\pwsh\.exe$", path, re.IGNORECASE)
    if match is None:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", match.group(1)))


def _version_prefix(version: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _powershell_registry_candidates() -> Iterable[_Candidate]:
    if os.name != "nt":
        return ()
    try:
        from importlib import import_module

        winreg = import_module("winreg")

        root = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\PowerShellCore\InstalledVersions",
        )
    except (ImportError, OSError):
        return ()
    candidates: list[_Candidate] = []
    try:
        index = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, subkey_name) as subkey:
                    install_location, _ = winreg.QueryValueEx(subkey, "InstallLocation")
            except OSError:
                continue
            if isinstance(install_location, str):
                executable = os.path.join(install_location, "pwsh.exe")
                if os.path.isfile(executable):
                    candidates.append(_Candidate(DialectName.PWSH, executable, "registry"))
    finally:
        winreg.CloseKey(root)
    return tuple(candidates)


def _looks_like_wsl(path: str) -> bool:
    folded = path.casefold().replace("/", "\\")
    return (
        "wsl.exe" in folded
        or folded.startswith("\\\\wsl$")
        or folded.startswith("\\\\wsl.localhost")
    )
