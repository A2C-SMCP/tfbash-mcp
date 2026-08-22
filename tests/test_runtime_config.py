from __future__ import annotations

from dataclasses import dataclass

import pytest

from tfbash_mcp.runtime import (
    DialectName,
    EnvironmentKind,
    EnvironmentSummary,
    HostConfig,
    HostProfile,
    PosixBashProfile,
    RuntimeBuilders,
    RuntimeConfigurationError,
    RuntimeName,
    RuntimeSelection,
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
def test_explicit_incompatible_profile_fails(
    selection: RuntimeSelection,
    operating_system: str,
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="incompatible"):
        resolve_runtime(selection, operating_system=operating_system)


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
        environment_summary=EnvironmentSummary(EnvironmentKind.CONDA, "analysis"),
        directory_exists=lambda path: path.startswith("/"),
    )
    source_environment["TOKEN"] = "changed"

    assert host.environment["TOKEN"] == "secret"
    with pytest.raises(TypeError):
        host.environment["NEW"] = "value"  # type: ignore[index]
    assert host.diagnostics() == {
        "mode": "ide",
        "workspace_root": "/workspace",
        "environment": {"kind": "conda", "name": "analysis"},
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
            workspace_root=r"C:\workspace",
            default_cwd=r"C:\workspace",
            runtime_profile=RuntimeName.WINDOWS_PWSH,
            default_shell=None,
            startup_command=None,
            environment={"PATH": "first", "Path": "second"},
            environment_summary=EnvironmentSummary(),
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
            workspace_root="/workspace",
            default_cwd="/workspace",
            runtime_profile=RuntimeName.POSIX_BASH,
            default_shell=values["default_shell"],
            startup_command=values["startup_command"],
            environment={},
            environment_summary=EnvironmentSummary(),
        )
