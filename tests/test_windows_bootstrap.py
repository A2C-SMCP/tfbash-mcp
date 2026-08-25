from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from tfbash_mcp.runtime.windows_bootstrap import _run


def _environment() -> dict[str, str]:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "executable": r"C:\Program Files\PowerShell\7\pwsh.exe",
                "arguments": ["-NoLogo", "value with spaces", "你好🙂"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).decode()
    return {
        "Path": r"C:\tools",
        "SECRET": "retained",
        "TFBASH_MCP_GATE_NAME": r"Local\tfbash-mcp-test",
        "TFBASH_MCP_GATE_PAYLOAD": payload,
        "TFBASH_MCP_GATE_TIMEOUT_MS": "4321",
    }


class _Child:
    pid = 4321

    def wait(self) -> int:
        return 37


class _Kernel:
    def __init__(self) -> None:
        self.closed: list[object] = []

    def CloseHandle(self, event: object) -> None:
        self.closed.append(event)


def _ready_event(
    _name: str,
    _process_id: int,
    _creation_time: int,
) -> tuple[_Kernel, object]:
    return _Kernel(), "ready"


def test_timeout_exits_without_starting_target() -> None:
    starts: list[object] = []

    result = _run(
        _environment(),
        gate_waiter=lambda name, timeout: (name, timeout) == ("never", 0),
        process_factory=lambda *args, **kwargs: starts.append((args, kwargs)),
        ready_event_factory=_ready_event,
        interrupt_ignorer=lambda: None,
        identity_factory=lambda _child: 9876,
    )

    assert result == 124
    assert starts == []


def test_release_starts_exact_target_and_strips_private_control_values() -> None:
    waits: list[tuple[str, int]] = []
    starts: list[tuple[list[str], Mapping[str, str]]] = []
    order: list[str] = []

    def wait(name: str, timeout_ms: int) -> bool:
        waits.append((name, timeout_ms))
        return True

    def start(target: list[str], *, env: Mapping[str, str]) -> Any:
        starts.append((target, env))
        return _Child()

    def ready(name: str, process_id: int, creation_time: int) -> tuple[_Kernel, object]:
        assert (name, process_id, creation_time) == (
            r"Local\tfbash-mcp-test",
            4321,
            9876,
        )
        order.append("ready")
        return _Kernel(), "ready"

    def identity(_child: object) -> int:
        order.append("identity")
        return 9876

    result = _run(
        _environment(),
        gate_waiter=wait,
        process_factory=start,
        ready_event_factory=ready,
        interrupt_ignorer=lambda: order.append("ignore"),
        identity_factory=identity,
    )

    assert result == 37
    assert waits == [(r"Local\tfbash-mcp-test", 4321)]
    assert starts[0][0] == [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "-NoLogo",
        "value with spaces",
        "你好🙂",
    ]
    assert starts[0][1] == {"Path": r"C:\tools", "SECRET": "retained"}
    assert order == ["ignore", "identity", "ready"]


def test_interrupt_ignore_failure_never_publishes_child_readiness() -> None:
    ready_calls: list[object] = []

    def fail_ignore() -> None:
        raise OSError("cannot ignore bootstrap interrupt")

    def ready(*arguments: object) -> tuple[_Kernel, object]:
        ready_calls.append(arguments)
        return _Kernel(), "ready"

    try:
        _run(
            _environment(),
            gate_waiter=lambda _name, _timeout: True,
            process_factory=lambda *_args, **_kwargs: _Child(),
            ready_event_factory=ready,
            interrupt_ignorer=fail_ignore,
            identity_factory=lambda _child: 9876,
        )
    except OSError as error:
        assert "cannot ignore" in str(error)
    else:
        raise AssertionError("bootstrap must fail when interrupt ignore fails")

    assert ready_calls == []
