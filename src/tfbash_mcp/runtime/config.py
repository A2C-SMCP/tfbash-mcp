"""Immutable HostConfig and the process-level Runtime Profile selector."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Final

from tfbash_mcp.runtime.contracts import DialectName, RuntimeName, ShellStartRequest
from tfbash_mcp.runtime.errors import RuntimeConfigurationError
from tfbash_mcp.runtime.profile import RuntimePlatform, RuntimeProfile


class RuntimeSelection(str, Enum):
    AUTO = "auto"
    POSIX_BASH = "posix-bash"
    WINDOWS_PWSH = "windows-pwsh"


class NativePlatform(str, Enum):
    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"


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
        if self.name is not None:
            _validate_utf8(self.name, label="environment name")
            if not self.name.strip() or "\x00" in self.name:
                raise RuntimeConfigurationError("environment name must be non-empty and NUL-free")


@dataclass(frozen=True, slots=True)
class HostConfig:
    """Process-frozen host inputs, kept outside every Runtime Port."""

    host_profile: HostProfile
    platform: NativePlatform
    workspace_root: str
    default_cwd: str
    runtime_profile: RuntimeName
    default_shell: str | None = field(repr=False)
    startup_command: str | None = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    environment_summary: EnvironmentSummary

    def __post_init__(self) -> None:
        windows = self.platform is NativePlatform.WINDOWS
        expected_runtime = RuntimeName.WINDOWS_PWSH if windows else RuntimeName.POSIX_BASH
        if self.runtime_profile is not expected_runtime:
            raise RuntimeConfigurationError("native platform and Runtime Profile must match")
        for label, value in (
            ("workspace_root", self.workspace_root),
            ("default_cwd", self.default_cwd),
        ):
            _validate_native_path(value, windows=windows, label=label)
        if self.default_shell is not None:
            _validate_native_path(
                self.default_shell,
                windows=windows,
                label="default shell",
            )
        if self.startup_command is not None:
            _validate_utf8(self.startup_command, label="startup command")
            if not self.startup_command or "\x00" in self.startup_command:
                raise RuntimeConfigurationError("startup command must be non-empty and NUL-free")
        _validate_environment(
            self.environment,
            windows=windows,
        )
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def diagnostics(self) -> dict[str, object]:
        """Return the only host metadata safe for Agent-visible responses."""

        environment: dict[str, object] = {
            "kind": self.environment_summary.kind.value,
        }
        if self.environment_summary.name is not None:
            environment["name"] = self.environment_summary.name
        return {
            "mode": self.host_profile.value,
            "workspace_root": self.workspace_root,
            "environment": environment,
        }


class _StartupCommandUnset:
    __slots__ = ()


STARTUP_COMMAND_UNSET: Final = _StartupCommandUnset()


@dataclass(frozen=True, slots=True)
class ShellOpenOverrides:
    """Explicit shell_open values before HostConfig defaults are applied."""

    cwd: str | None = None
    environment: Mapping[str, str] | None = None
    executable: str | None = None
    startup_command: str | None | _StartupCommandUnset = STARTUP_COMMAND_UNSET

    def __post_init__(self) -> None:
        if self.environment is not None:
            object.__setattr__(
                self,
                "environment",
                MappingProxyType(dict(self.environment)),
            )


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    platform: NativePlatform
    dialect: DialectName
    shell_version: str
    default_cwd: str

    def __post_init__(self) -> None:
        windows = self.platform is NativePlatform.WINDOWS
        expected_dialect = DialectName.PWSH if windows else DialectName.BASH
        if self.dialect is not expected_dialect:
            raise RuntimeConfigurationError("native platform and command dialect must match")
        _validate_utf8(self.shell_version, label="shell version")
        if not self.shell_version or "\x00" in self.shell_version:
            raise RuntimeConfigurationError("shell version must be non-empty and NUL-free")
        _validate_native_path(self.default_cwd, windows=windows, label="default cwd")

    def diagnostics(self) -> dict[str, str]:
        return {
            "platform": self.platform.value,
            "dialect": self.dialect.value,
            "shell_version": self.shell_version,
            "default_cwd": self.default_cwd,
        }


@dataclass(frozen=True, slots=True)
class AgentHostContext:
    mode: HostProfile
    workspace_root: str
    environment: EnvironmentSummary

    def __post_init__(self) -> None:
        _validate_utf8(self.workspace_root, label="workspace root")
        if not self.workspace_root or "\x00" in self.workspace_root:
            raise RuntimeConfigurationError("workspace root must be non-empty and NUL-free")

    def diagnostics(self) -> dict[str, object]:
        environment: dict[str, object] = {"kind": self.environment.kind.value}
        if self.environment.name is not None:
            environment["name"] = self.environment.name
        return {
            "mode": self.mode.value,
            "workspace_root": self.workspace_root,
            "environment": environment,
        }


@dataclass(frozen=True, slots=True)
class AgentContext:
    runtime: AgentRuntimeContext
    host: AgentHostContext

    def __post_init__(self) -> None:
        _validate_native_path(
            self.host.workspace_root,
            windows=self.runtime.platform is NativePlatform.WINDOWS,
            label="workspace root",
        )

    def diagnostics(self) -> dict[str, object]:
        return {
            "runtime": self.runtime.diagnostics(),
            "host": self.host.diagnostics(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    host: HostConfig
    runtime: RuntimeProfile

    def __post_init__(self) -> None:
        if self.host.runtime_profile is not self.runtime.name:
            raise RuntimeConfigurationError("HostConfig and RuntimeProfile must match")

    def resolve_shell_start(
        self,
        overrides: ShellOpenOverrides | None = None,
        *,
        directory_exists: Callable[[str], bool],
    ) -> ShellStartRequest:
        """Apply shell_open > HostConfig > Runtime Profile precedence once."""

        explicit = overrides or ShellOpenOverrides()
        cwd = self.host.default_cwd if explicit.cwd is None else explicit.cwd
        executable = (
            explicit.executable
            if explicit.executable is not None
            else self.host.default_shell or self.runtime.dialect.default_executable
        )
        startup_command = (
            self.host.startup_command
            if explicit.startup_command is STARTUP_COMMAND_UNSET
            else explicit.startup_command
        )
        if not isinstance(startup_command, str | None):
            raise RuntimeConfigurationError("invalid startup command override")
        windows = self.host.platform is NativePlatform.WINDOWS
        _validate_native_path(cwd, windows=windows, label="cwd")
        if not directory_exists(cwd):
            raise RuntimeConfigurationError("cwd must be an existing native absolute directory")
        _validate_native_path(executable, windows=windows, label="shell")
        if startup_command is not None:
            _validate_utf8(startup_command, label="startup command")
            if not startup_command or "\x00" in startup_command:
                raise RuntimeConfigurationError("startup command must be non-empty and NUL-free")
        environment = _merge_environment(
            self.host.environment,
            explicit.environment or {},
            windows=windows,
        )
        return ShellStartRequest(
            executable=executable,
            cwd=cwd,
            environment=environment,
            startup_command=startup_command,
        )

    def agent_context(self, *, shell_version: str) -> AgentContext:
        return AgentContext(
            runtime=AgentRuntimeContext(
                platform=self.host.platform,
                dialect=self.runtime.dialect.dialect_name,
                shell_version=shell_version,
                default_cwd=self.host.default_cwd,
            ),
            host=AgentHostContext(
                mode=self.host.host_profile,
                workspace_root=self.host.workspace_root,
                environment=self.host.environment_summary,
            ),
        )

    def instructions(self) -> str:
        dialect = self.runtime.dialect.dialect_name.value
        return (
            f"Commands use the {dialect} dialect on {self.host.platform.value}. "
            f"The default working directory is {self.host.default_cwd}. "
            "The workspace root is context, not a filesystem sandbox boundary. "
            "Use shell_list for authoritative runtime and host metadata."
        )

    def tool_descriptions(self) -> Mapping[str, str]:
        dialect = self.runtime.dialect.dialect_name.value
        descriptions = {
            "shell_open": f"Open a persistent {dialect} command shell.",
            "shell_exec": f"Execute one {dialect} command in a persistent shell.",
            "shell_read": "Read incremental output from an execution cursor.",
            "shell_write": "Write input bytes to the active execution.",
            "shell_signal": "Send a portable control intent to the active execution.",
            "shell_list": "List shells with authoritative runtime and redacted host context.",
            "shell_close": "Close a persistent shell and its managed process tree.",
        }
        return MappingProxyType(descriptions)


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
        RuntimePlatform.WINDOWS if selected is RuntimeName.WINDOWS_PWSH else RuntimePlatform.POSIX
    )
    if platform is not expected:
        raise RuntimeConfigurationError(f"{selected.value} is incompatible with {operating_system}")
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
    platform = _native_platform(operating_system)
    runtime = resolve_runtime(runtime_selection, operating_system=operating_system)
    if host_profile is HostProfile.IDE and workspace_root is None:
        raise RuntimeConfigurationError("IDE hosts must provide workspace_root")
    workspace = process_cwd if workspace_root is None else workspace_root
    cwd = workspace if default_cwd is None else default_cwd
    windows = platform is NativePlatform.WINDOWS
    for label, value in (("workspace_root", workspace), ("default_cwd", cwd)):
        _validate_native_path(value, windows=windows, label=label)
        if not directory_exists(value):
            raise RuntimeConfigurationError(
                f"{label} must be an existing native absolute directory"
            )
    return HostConfig(
        host_profile=host_profile,
        platform=platform,
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
    platform = _native_platform(operating_system)
    if platform is NativePlatform.WINDOWS:
        return RuntimePlatform.WINDOWS
    return RuntimePlatform.POSIX


def _native_platform(operating_system: str) -> NativePlatform:
    normalized = operating_system.casefold()
    if normalized == "windows":
        return NativePlatform.WINDOWS
    if normalized == "darwin":
        return NativePlatform.MACOS
    if normalized == "linux":
        return NativePlatform.LINUX
    raise RuntimeConfigurationError(f"unsupported operating system: {operating_system}")


def _validate_environment(environment: Mapping[str, str], *, windows: bool) -> None:
    normalized: set[str] = set()
    for key, value in environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeConfigurationError("environment keys and values must be strings")
        _validate_utf8(key, label="environment key")
        _validate_utf8(value, label="environment value")
        if "\x00" in key + value:
            raise RuntimeConfigurationError("environment keys and values must be NUL-free strings")
        comparable = key.casefold() if windows else key
        if comparable in normalized:
            raise RuntimeConfigurationError("duplicate environment key for selected platform")
        normalized.add(comparable)


def _validate_native_path(value: str, *, windows: bool, label: str) -> None:
    _validate_utf8(value, label=label)
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not value or "\x00" in value or not path.is_absolute():
        raise RuntimeConfigurationError(f"{label} must be a NUL-free native absolute path")


def _validate_utf8(value: str, *, label: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RuntimeConfigurationError(f"{label} must be valid UTF-8") from error


def _merge_environment(
    inherited: Mapping[str, str],
    explicit: Mapping[str, str],
    *,
    windows: bool,
) -> Mapping[str, str]:
    _validate_environment(explicit, windows=windows)
    merged = dict(inherited)
    if windows:
        inherited_keys = {key.casefold(): key for key in merged}
        for key, value in explicit.items():
            previous = inherited_keys.get(key.casefold())
            if previous is not None:
                del merged[previous]
            merged[key] = value
            inherited_keys[key.casefold()] = key
    else:
        merged.update(explicit)
    return MappingProxyType(merged)
