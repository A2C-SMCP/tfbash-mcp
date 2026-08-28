from __future__ import annotations

from dataclasses import dataclass

import pytest

from tfbash_mcp.protocol import (
    PlatformName,
    ProtocolConfig,
    ShellListResult,
    ToolName,
    validate_tool_output,
)
from tfbash_mcp.runtime import (
    AgentContext,
    DialectName,
    HostConfig,
    HostProfile,
    NativePlatform,
    PosixBashProfile,
    RuntimeBuilders,
    RuntimeComposition,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimeSelection,
    ShellOpenOverrides,
    WindowsPwshProfile,
    compose_runtime,
    create_host_config,
    resolve_runtime,
)


@dataclass
class _Component:
    runtime_name: RuntimeName


@dataclass
class _Dialect(_Component):
    dialect_name: DialectName
    default_executable: str = "shell"


def _profile(name: RuntimeName):  # type: ignore[no-untyped-def]
    dialect_name = DialectName.BASH if name is RuntimeName.POSIX_BASH else DialectName.PWSH
    arguments = {
        "dialect": _Dialect(name, dialect_name),
        "transport": _Component(name),
        "supervisor": _Component(name),
    }
    if name is RuntimeName.POSIX_BASH:
        return PosixBashProfile(**arguments)  # type: ignore[arg-type]
    return WindowsPwshProfile(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("operating_system", ["Darwin", "Linux"])
def test_auto_selects_posix_using_only_operating_system(operating_system: str) -> None:
    assert (
        resolve_runtime(RuntimeSelection.AUTO, operating_system=operating_system)
        is RuntimeName.POSIX_BASH
    )


def test_auto_selects_windows_using_only_operating_system() -> None:
    assert (
        resolve_runtime(RuntimeSelection.AUTO, operating_system="Windows")
        is RuntimeName.WINDOWS_PWSH
    )


@pytest.mark.parametrize(
    ("selection", "operating_system"),
    [
        (RuntimeSelection.POSIX_BASH, "Windows"),
        (RuntimeSelection.WINDOWS_PWSH, "Linux"),
    ],
)
def test_explicit_dialects_use_the_native_backend(
    selection: RuntimeSelection,
    operating_system: str,
) -> None:
    expected = RuntimeName.WINDOWS_PWSH if operating_system == "Windows" else RuntimeName.POSIX_BASH
    assert resolve_runtime(selection, operating_system=operating_system) is expected


def test_host_config_is_frozen_and_diagnostics_are_redacted() -> None:
    source_environment = {"PATH": "/safe/bin", "TOKEN": "secret"}
    host = create_host_config(
        host_profile=HostProfile.IDE,
        runtime_selection=RuntimeSelection.AUTO,
        operating_system="Linux",
        process_cwd="/process",
        inherited_environment=source_environment,
        workspace_root="/workspace",
        default_cwd="/workspace/project",
        default_shell="/bin/bash",
        startup_command="source env.sh",
        directory_exists=lambda path: path.startswith("/"),
    )
    source_environment["TOKEN"] = "changed"

    assert host.environment["TOKEN"] == "secret"
    with pytest.raises(TypeError):
        host.environment["NEW"] = "value"  # type: ignore[index]
    assert host.diagnostics() == {
        "mode": "ide",
        "workspace_root": "/workspace",
    }
    rendered = repr(host.diagnostics())
    assert "secret" not in rendered
    assert "source env.sh" not in rendered
    assert "/bin/bash" not in rendered


def test_standalone_defaults_workspace_and_cwd_to_process_cwd() -> None:
    host = create_host_config(
        host_profile=HostProfile.STANDALONE,
        runtime_selection=RuntimeSelection.POSIX_BASH,
        operating_system="Darwin",
        process_cwd="/workspace",
        inherited_environment={},
        directory_exists=lambda path: path == "/workspace",
    )

    assert host.workspace_root == "/workspace"
    assert host.default_cwd == "/workspace"
    assert host.platform is NativePlatform.MACOS


def test_ide_requires_explicit_workspace() -> None:
    with pytest.raises(RuntimeConfigurationError, match="must provide"):
        create_host_config(
            host_profile=HostProfile.IDE,
            runtime_selection=RuntimeSelection.AUTO,
            operating_system="Linux",
            process_cwd="/workspace",
            inherited_environment={},
            directory_exists=lambda _: True,
        )


@pytest.mark.parametrize(
    ("workspace_root", "default_cwd"),
    [("", None), ("/workspace", "")],
)
def test_empty_host_paths_are_not_treated_as_omitted(
    workspace_root: str,
    default_cwd: str | None,
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="absolute"):
        create_host_config(
            host_profile=HostProfile.IDE,
            runtime_selection=RuntimeSelection.POSIX_BASH,
            operating_system="Linux",
            process_cwd="/process",
            inherited_environment={},
            workspace_root=workspace_root,
            default_cwd=default_cwd,
            directory_exists=lambda _: True,
        )


def test_windows_paths_and_environment_use_windows_semantics() -> None:
    host = create_host_config(
        host_profile=HostProfile.STANDALONE,
        runtime_selection=RuntimeSelection.WINDOWS_PWSH,
        operating_system="Windows",
        process_cwd=r"C:\workspace",
        inherited_environment={"Path": r"C:\bin"},
        directory_exists=lambda path: path.startswith("C:\\"),
    )
    assert host.runtime_profile is RuntimeName.WINDOWS_PWSH

    with pytest.raises(RuntimeConfigurationError, match="duplicate environment"):
        HostConfig(
            host_profile=HostProfile.STANDALONE,
            platform=NativePlatform.WINDOWS,
            workspace_root=r"C:\workspace",
            default_cwd=r"C:\workspace",
            runtime_profile=RuntimeName.WINDOWS_PWSH,
            default_shell=None,
            startup_command=None,
            environment={"PATH": "first", "Path": "second"},
        )


@pytest.mark.parametrize(
    ("workspace_root", "default_cwd", "default_shell"),
    [
        (r"\workspace", None, None),
        (r"C:\workspace", r"\project", None),
        (r"C:\workspace", None, r"\PowerShell\pwsh.exe"),
    ],
)
def test_windows_rejects_drive_relative_rooted_paths(
    workspace_root: str,
    default_cwd: str | None,
    default_shell: str | None,
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="absolute"):
        create_host_config(
            host_profile=HostProfile.IDE,
            runtime_selection=RuntimeSelection.WINDOWS_PWSH,
            operating_system="Windows",
            process_cwd=r"C:\process",
            inherited_environment={},
            workspace_root=workspace_root,
            default_cwd=default_cwd,
            default_shell=default_shell,
            directory_exists=lambda _: True,
        )


def test_compose_runtime_calls_only_the_selected_complete_builder() -> None:
    calls: list[RuntimeName] = []

    def build(name: RuntimeName):  # type: ignore[no-untyped-def]
        def selected():  # type: ignore[no-untyped-def]
            calls.append(name)
            return _profile(name)

        return selected

    host = create_host_config(
        host_profile=HostProfile.STANDALONE,
        runtime_selection=RuntimeSelection.POSIX_BASH,
        operating_system="Linux",
        process_cwd="/workspace",
        inherited_environment={},
        directory_exists=lambda _: True,
    )
    composition = compose_runtime(
        host,
        RuntimeBuilders(
            posix_bash=build(RuntimeName.POSIX_BASH),
            windows_pwsh=build(RuntimeName.WINDOWS_PWSH),
        ),
    )

    assert calls == [RuntimeName.POSIX_BASH]
    assert composition.host is host
    assert composition.runtime.name is RuntimeName.POSIX_BASH


def test_profile_rejects_mixed_components() -> None:
    with pytest.raises(RuntimeConfigurationError, match="cannot mix"):
        PosixBashProfile(
            dialect=_Dialect(RuntimeName.POSIX_BASH, DialectName.BASH),  # type: ignore[arg-type]
            transport=_Component(RuntimeName.WINDOWS_PWSH),  # type: ignore[arg-type]
            supervisor=_Component(RuntimeName.POSIX_BASH),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_shell", "/bin/ba\x00sh"),
        ("startup_command", "source\x00 env.sh"),
    ],
)
def test_host_config_rejects_nul_in_native_shell_values(field: str, value: str) -> None:
    values: dict[str, str | None] = {"default_shell": None, "startup_command": None}
    values[field] = value
    with pytest.raises(RuntimeConfigurationError, match="NUL-free"):
        HostConfig(
            host_profile=HostProfile.STANDALONE,
            platform=NativePlatform.LINUX,
            workspace_root="/workspace",
            default_cwd="/workspace",
            runtime_profile=RuntimeName.POSIX_BASH,
            default_shell=values["default_shell"],
            startup_command=values["startup_command"],
            environment={},
        )


def _composition(
    *,
    host_profile: HostProfile = HostProfile.IDE,
    operating_system: str = "Linux",
    environment: dict[str, str] | None = None,
    startup_command: str | None = "conda activate analytics",
) -> RuntimeComposition:
    runtime_name = (
        RuntimeName.WINDOWS_PWSH if operating_system == "Windows" else RuntimeName.POSIX_BASH
    )
    workspace = r"C:\workspace" if operating_system == "Windows" else "/workspace"
    shell = r"C:\PowerShell\pwsh.exe" if operating_system == "Windows" else "/bin/bash"
    host = create_host_config(
        host_profile=host_profile,
        runtime_selection=RuntimeSelection(runtime_name.value),
        operating_system=operating_system,
        process_cwd=workspace,
        inherited_environment=environment or {},
        workspace_root=workspace,
        default_shell=shell,
        startup_command=startup_command,
        directory_exists=lambda path: path == workspace,
    )
    return compose_runtime(
        host,
        RuntimeBuilders(
            posix_bash=lambda: _profile(RuntimeName.POSIX_BASH),
            windows_pwsh=lambda: _profile(RuntimeName.WINDOWS_PWSH),
        ),
    )


def test_shell_open_precedence_and_explicit_null_startup() -> None:
    composition = _composition(environment={"PATH": "/host/bin", "TOKEN": "secret"})
    defaults = composition.resolve_shell_start(directory_exists=lambda path: path == "/workspace")
    explicit = composition.resolve_shell_start(
        ShellOpenOverrides(
            cwd="/project",
            environment={"PATH": "/request/bin", "LOCAL": "yes"},
            startup_command=None,
        ),
        directory_exists=lambda path: path in {"/workspace", "/project"},
    )

    assert defaults.cwd == "/workspace"
    assert defaults.executable == "/bin/bash"
    assert defaults.startup_command == "conda activate analytics"
    assert dict(defaults.environment) == {"PATH": "/host/bin", "TOKEN": "secret"}
    assert explicit.cwd == "/project"
    assert explicit.executable == "/bin/bash"
    assert explicit.startup_command is None
    assert dict(explicit.environment) == {
        "PATH": "/request/bin",
        "TOKEN": "secret",
        "LOCAL": "yes",
    }


def test_windows_environment_override_is_case_insensitive() -> None:
    composition = _composition(
        operating_system="Windows",
        environment={"Path": r"C:\venv\Scripts;C:\Windows", "TOKEN": "secret"},
    )

    request = composition.resolve_shell_start(
        ShellOpenOverrides(environment={"PATH": r"C:\request", "TOKEN": "override"}),
        directory_exists=lambda path: path == r"C:\workspace",
    )

    assert dict(request.environment) == {
        "PATH": r"C:\request",
        "TOKEN": "override",
    }


@pytest.mark.parametrize(
    ("operating_system", "path_key", "venv_root", "bin_path"),
    [
        ("Linux", "PATH", "/workspace/.venv", "/workspace/.venv/bin:/usr/bin"),
        (
            "Windows",
            "Path",
            r"C:\workspace\.venv",
            r"C:\workspace\.venv\Scripts;C:\Windows",
        ),
    ],
)
def test_standard_venv_is_inherited_without_activation_or_discovery(
    operating_system: str,
    path_key: str,
    venv_root: str,
    bin_path: str,
) -> None:
    checked_paths: list[str] = []
    composition = _composition(
        operating_system=operating_system,
        environment={"VIRTUAL_ENV": venv_root, path_key: bin_path},
        startup_command=None,
    )
    workspace = r"C:\workspace" if operating_system == "Windows" else "/workspace"

    def existing_workspace(path: str) -> bool:
        checked_paths.append(path)
        return path == workspace

    request = composition.resolve_shell_start(directory_exists=existing_workspace)

    assert request.environment["VIRTUAL_ENV"] == venv_root
    assert request.environment[path_key] == bin_path
    assert request.startup_command is None
    assert checked_paths == [workspace]
    assert "Activate.ps1" not in repr(request)


def test_host_profiles_share_resolution_and_runtime_descriptions() -> None:
    standalone = _composition(host_profile=HostProfile.STANDALONE)
    ide = _composition(host_profile=HostProfile.IDE)

    standalone_request = standalone.resolve_shell_start(
        directory_exists=lambda path: path == "/workspace"
    )
    ide_request = ide.resolve_shell_start(directory_exists=lambda path: path == "/workspace")

    assert standalone_request == ide_request
    assert standalone.instructions() == ide.instructions()
    assert standalone.tool_descriptions() == ide.tool_descriptions()
    assert "bash" in standalone.instructions()
    assert "bash" in standalone.tool_descriptions()["shell_exec"]


def test_agent_context_is_authoritative_and_redacted() -> None:
    composition = _composition(environment={"PATH": "/venv/bin:/usr/bin", "TOKEN": "top-secret"})

    context = composition.agent_context(shell_version="5.2.37")

    assert isinstance(context, AgentContext)
    assert context.diagnostics() == {
        "runtime": {
            "platform": "linux",
            "dialect": "bash",
            "shell_version": "5.2.37",
            "default_cwd": "/workspace",
        },
        "host": {
            "mode": "ide",
            "workspace_root": "/workspace",
        },
    }
    visible = (
        repr(context.diagnostics())
        + composition.instructions()
        + repr(composition.tool_descriptions())
        + repr(context)
        + repr(composition.host)
    )
    for secret in ("top-secret", "conda activate analytics", "/bin/bash"):
        assert secret not in visible

    validated = validate_tool_output(
        ToolName.SHELL_LIST,
        {**context.diagnostics(), "shells": []},
        config=ProtocolConfig(
            platform=PlatformName.LINUX,
            default_cwd="/workspace",
            shell="/bin/bash",
        ),
    )
    assert isinstance(validated, ShellListResult)
    assert validated.shells == []


def test_custom_startup_is_replayed_by_each_shell_resolution() -> None:
    composition = _composition(startup_command="source /opt/project-env")

    first = composition.resolve_shell_start(directory_exists=lambda _: True)
    rebuilt = composition.resolve_shell_start(directory_exists=lambda _: True)

    assert first.startup_command == "source /opt/project-env"
    assert rebuilt == first


def test_shell_open_rejects_missing_cwd_and_invalid_explicit_values() -> None:
    composition = _composition()

    with pytest.raises(RuntimeConfigurationError, match="existing"):
        composition.resolve_shell_start(directory_exists=lambda _: False)
    with pytest.raises(RuntimeConfigurationError, match="duplicate environment"):
        _composition(operating_system="Windows").resolve_shell_start(
            ShellOpenOverrides(environment={"Path": "first", "PATH": "second"}),
            directory_exists=lambda _: True,
        )
    with pytest.raises(RuntimeConfigurationError, match="absolute"):
        _composition(operating_system="Windows").resolve_shell_start(
            ShellOpenOverrides(cwd=r"\project"),
            directory_exists=lambda _: True,
        )
    with pytest.raises(RuntimeConfigurationError, match="valid UTF-8"):
        composition.resolve_shell_start(
            ShellOpenOverrides(startup_command="bad\ud800command"),
            directory_exists=lambda _: True,
        )


@pytest.mark.parametrize("shell_version", ["", "7\x00.4"])
def test_agent_context_rejects_invalid_shell_version(shell_version: str) -> None:
    with pytest.raises(RuntimeConfigurationError, match="shell version"):
        _composition().agent_context(shell_version=shell_version)


@pytest.mark.parametrize("field", ["workspace", "shell_version"])
def test_agent_visible_context_rejects_invalid_utf8(field: str) -> None:
    invalid = "bad\ud800value"
    if field == "shell_version":
        with pytest.raises(RuntimeConfigurationError, match="valid UTF-8"):
            _composition().agent_context(shell_version=invalid)
        return
    with pytest.raises(RuntimeConfigurationError, match="valid UTF-8"):
        create_host_config(
            host_profile=HostProfile.IDE,
            runtime_selection=RuntimeSelection.POSIX_BASH,
            operating_system="Linux",
            process_cwd="/process",
            inherited_environment={},
            workspace_root=f"/{invalid}",
            directory_exists=lambda _: True,
        )
