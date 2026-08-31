from __future__ import annotations

import errno
import os

import pytest

fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")

from tfbash_mcp.runtime import posix_spawn_bootstrap  # noqa: E402


def test_configure_terminal_claims_unowned_terminal_and_sets_foreground_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    def no_controlling_terminal(_file_descriptor: int) -> int:
        raise OSError(errno.ENOTTY, "not a controlling terminal")

    monkeypatch.setattr(os, "tcgetpgrp", no_controlling_terminal)
    monkeypatch.setattr(os, "getpgrp", lambda: 4321)
    monkeypatch.setattr(
        fcntl,
        "ioctl",
        lambda *arguments: calls.append(("ioctl", *arguments)),
    )
    monkeypatch.setattr(
        os,
        "tcsetpgrp",
        lambda *arguments: calls.append(("tcsetpgrp", *arguments)),
    )

    posix_spawn_bootstrap.configure_terminal()

    assert calls == [
        ("ioctl", 0, termios.TIOCSCTTY, 0),
        ("tcsetpgrp", 0, 4321),
    ]


def test_configure_terminal_preserves_existing_control_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(os, "tcgetpgrp", lambda _file_descriptor: 1234)
    monkeypatch.setattr(os, "getpgrp", lambda: 4321)
    monkeypatch.setattr(
        fcntl,
        "ioctl",
        lambda *arguments: calls.append(("ioctl", *arguments)),
    )
    monkeypatch.setattr(
        os,
        "tcsetpgrp",
        lambda *arguments: calls.append(("tcsetpgrp", *arguments)),
    )

    posix_spawn_bootstrap.configure_terminal()

    assert calls == [("tcsetpgrp", 0, 4321)]


def test_configure_terminal_propagates_unexpected_probe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe(_file_descriptor: int) -> int:
        raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(os, "tcgetpgrp", fail_probe)

    with pytest.raises(OSError) as raised:
        posix_spawn_bootstrap.configure_terminal()

    assert raised.value.errno == errno.EBADF


def test_main_configures_terminal_then_executes_requested_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    environment = {"SAFE": "value"}
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(
        posix_spawn_bootstrap,
        "configure_terminal",
        lambda: calls.append(("configure_terminal",)),
    )
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("chdir", path)))

    def record_execve(path: str, arguments: list[str], env: dict[str, str]) -> None:
        calls.append(("execve", path, arguments, env))

    monkeypatch.setattr(os, "execve", record_execve)

    posix_spawn_bootstrap.main(["/working", "/bin/bash", "--noprofile"])

    assert calls == [
        ("configure_terminal",),
        ("chdir", "/working"),
        ("execve", "/bin/bash", ["/bin/bash", "--noprofile"], environment),
    ]


@pytest.mark.parametrize("arguments", [[], ["/working"]])
def test_main_rejects_incomplete_launch_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit, match="working directory and executable are required"):
        posix_spawn_bootstrap.main(arguments)
