"""Immutable HostConfig and the process-level Runtime Profile selector."""

from __future__ import annotations

import ntpath
import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from tfbash_mcp.runtime.contracts import RuntimeName
from tfbash_mcp.runtime.errors import RuntimeConfigurationError
from tfbash_mcp.runtime.profile import RuntimePlatform, RuntimeProfile


class RuntimeSelection(str, Enum):
    AUTO = "auto"
    POSIX_BASH = "posix-bash"
    WINDOWS_PWSH = "windows-pwsh"


class HostProfile(str, Enum):
    STANDALONE = "standalone"
    IDE = "ide"


class EnvironmentKind(str, Enum):
    NONE = "none"
    PYTHON_VENV = "python-venv"
    CONDA = "conda"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class EnvironmentSummary:
    kind: EnvironmentKind = EnvironmentKind.NONE
    name: str | None = None

    def __post_init__(self) -> None:
        if self.name is not None and (not self.name.strip() or "\x00" in self.name):
            raise RuntimeConfigurationError("environment name must be non-empty and NUL-free")


@dataclass(frozen=True, slots=True)
class HostConfig:
    """Process-frozen host inputs, kept outside every Runtime Port."""

    host_profile: HostProfile
    workspace_root: str
    default_cwd: str
    runtime_profile: RuntimeName
    default_shell: str | None
    startup_command: str | None
    environment: Mapping[str, str]
    environment_summary: EnvironmentSummary

    def __post_init__(self) -> None:
        windows = self.runtime_profile is RuntimeName.WINDOWS_PWSH
        path_module = ntpath if windows else posixpath
        for label, value in (
            ("workspace_root", self.workspace_root),
            ("default_cwd", self.default_cwd),
        ):
            if not value or "\x00" in value or not path_module.isabs(value):
                raise RuntimeConfigurationError(f"{label} must be a NUL-free native absolute path")
        if self.default_shell is not None and (
            not self.default_shell
            or "\x00" in self.default_shell
            or not path_module.isabs(self.default_shell)
        ):
            raise RuntimeConfigurationError(
                "default shell must be a NUL-free native absolute path"
            )
        if self.startup_command is not None and (
            not self.startup_command or "\x00" in self.startup_command
        ):
            raise RuntimeConfigurationError("startup command must be non-empty and NUL-free")
        _validate_environment(
            self.environment,
            windows=windows,
        )
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def diagnostics(self) -> dict[str, object]:
        """Return the only host metadata safe for Agent-visible responses."""

        return {
            "mode": self.host_profile.value,
            "workspace_root": self.workspace_root,
            "environment": {
                "kind": self.environment_summary.kind.value,
                "name": self.environment_summary.name,
            },
        }


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    host: HostConfig
    runtime: RuntimeProfile

    def __post_init__(self) -> None:
        if self.host.runtime_profile is not self.runtime.name:
            raise RuntimeConfigurationError("HostConfig and RuntimeProfile must match")


@dataclass(frozen=True, slots=True)
class RuntimeBuilders:
    posix_bash: Callable[[], RuntimeProfile]
    windows_pwsh: Callable[[], RuntimeProfile]


def resolve_runtime(selection: RuntimeSelection, *, operating_system: str) -> RuntimeName:
    """Resolve once using only the OS; host/IDE/env/command are intentionally absent."""

    platform = _runtime_platform(operating_system)
    if selection is RuntimeSelection.AUTO:
        return (
            RuntimeName.WINDOWS_PWSH
            if platform is RuntimePlatform.WINDOWS
            else RuntimeName.POSIX_BASH
        )
    selected = RuntimeName(selection.value)
    expected = (
        RuntimePlatform.WINDOWS
        if selected is RuntimeName.WINDOWS_PWSH
        else RuntimePlatform.POSIX
    )
    if platform is not expected:
        raise RuntimeConfigurationError(
            f"{selected.value} is incompatible with {operating_system}"
        )
    return selected


def create_host_config(
    *,
    host_profile: HostProfile,
    runtime_selection: RuntimeSelection,
    operating_system: str,
    process_cwd: str,
    inherited_environment: Mapping[str, str],
    workspace_root: str | None = None,
    default_cwd: str | None = None,
    default_shell: str | None = None,
    startup_command: str | None = None,
    environment_summary: EnvironmentSummary | None = None,
    directory_exists: Callable[[str], bool],
) -> HostConfig:
    runtime = resolve_runtime(runtime_selection, operating_system=operating_system)
    if host_profile is HostProfile.IDE and workspace_root is None:
        raise RuntimeConfigurationError("IDE hosts must provide workspace_root")
    workspace = process_cwd if workspace_root is None else workspace_root
    cwd = workspace if default_cwd is None else default_cwd
    windows = runtime is RuntimeName.WINDOWS_PWSH
    for label, value in (("workspace_root", workspace), ("default_cwd", cwd)):
        path_module = ntpath if windows else posixpath
        if not path_module.isabs(value) or not directory_exists(value):
            raise RuntimeConfigurationError(
                f"{label} must be an existing native absolute directory"
            )
    return HostConfig(
        host_profile=host_profile,
        workspace_root=workspace,
        default_cwd=cwd,
        runtime_profile=runtime,
        default_shell=default_shell,
        startup_command=startup_command,
        environment=inherited_environment,
        environment_summary=environment_summary or EnvironmentSummary(),
    )


def compose_runtime(host: HostConfig, builders: RuntimeBuilders) -> RuntimeComposition:
    """Build exactly one complete profile without passing HostConfig into its ports."""

    builder = (
        builders.posix_bash
        if host.runtime_profile is RuntimeName.POSIX_BASH
        else builders.windows_pwsh
    )
    runtime = builder()
    return RuntimeComposition(host=host, runtime=runtime)


def _runtime_platform(operating_system: str) -> RuntimePlatform:
    normalized = operating_system.casefold()
    if normalized == "windows":
        return RuntimePlatform.WINDOWS
    if normalized in {"darwin", "linux"}:
        return RuntimePlatform.POSIX
    raise RuntimeConfigurationError(f"unsupported operating system: {operating_system}")


def _validate_environment(environment: Mapping[str, str], *, windows: bool) -> None:
    normalized: set[str] = set()
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value:
            raise RuntimeConfigurationError("environment keys and values must be NUL-free strings")
        comparable = key.casefold() if windows else key
        if comparable in normalized:
            raise RuntimeConfigurationError("duplicate environment key for selected platform")
        normalized.add(comparable)
