from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mcp import types

from tfbash_mcp.domain import (
    ExecutionOverviewSnapshot,
    ExecutionSnapshot,
    ExecutionState,
    ShellBusy,
    ShellOverviewSnapshot,
    ShellSnapshot,
    ShellState,
    ShellUnavailable,
)
from tfbash_mcp.mcp_adapter import SHELL_OVERVIEW_OUTPUT_CHARACTERS, ShellToolService
from tfbash_mcp.protocol import PlatformName, ProtocolConfig
from tfbash_mcp.runtime import (
    BashDialect,
    ControlIntent,
    EnvironmentSummary,
    HostProfile,
    PexpectPosixPtyTransport,
    PosixBashProfile,
    PosixProcessSupervisor,
    RuntimeBuilders,
    RuntimeSelection,
    ShellStartRequest,
    compose_runtime,
    create_host_config,
)


@dataclass
class FakeManager:
    starts: list[ShellStartRequest] = field(default_factory=list)
    writes: list[tuple[str, str, bytes]] = field(default_factory=list)
    exec_error: Exception | None = None
    close_error: Exception | None = None
    shutdown_calls: int = 0
    overview_items: tuple[ShellOverviewSnapshot, ...] = ()
    overview_max_characters: int | None = None

    def open_shell(self, request: ShellStartRequest) -> ShellSnapshot:
        self.starts.append(request)
        return ShellSnapshot("shell_1", ShellState.READY, request.cwd, None, 100)

    def exec(
        self,
        shell_id: str,
        command: str,
        *,
        yield_ms: int,
        timeout_ms: int,
        max_output_bytes: int,
    ) -> ExecutionSnapshot:
        del command, yield_ms, timeout_ms, max_output_bytes
        if self.exec_error is not None:
            raise self.exec_error
        return ExecutionSnapshot(
            shell_id=shell_id,
            exec_id="exec_1",
            status=ExecutionState.EXITED,
            exit_code=0,
            output="ok",
            buffer_start_cursor=0,
            next_cursor=2,
            truncated_before_cursor=False,
            eof=True,
            duration_ms=1,
            cwd="/workspace",
            shell_status=ShellState.READY,
            shell_rebuilt=False,
        )

    def read(
        self,
        shell_id: str,
        exec_id: str,
        *,
        cursor: int,
        max_bytes: int,
        wait_ms: int,
    ) -> ExecutionSnapshot:
        del cursor, max_bytes, wait_ms
        return ExecutionSnapshot(
            shell_id=shell_id,
            exec_id=exec_id,
            status=ExecutionState.RUNNING,
            exit_code=None,
            output="",
            buffer_start_cursor=0,
            next_cursor=0,
            truncated_before_cursor=False,
            eof=False,
        )

    def write(self, shell_id: str, exec_id: str, data: bytes) -> int:
        self.writes.append((shell_id, exec_id, data))
        return len(data)

    def signal(self, shell_id: str, exec_id: str, intent: ControlIntent) -> bool:
        del shell_id, exec_id, intent
        return True

    def close_shell(self, shell_id: str) -> bool:
        del shell_id
        if self.close_error is not None:
            raise self.close_error
        return True

    def snapshots(self) -> tuple[ShellSnapshot, ...]:
        return (ShellSnapshot("shell_1", ShellState.READY, "/workspace", None, 100),)

    def overview_snapshots(
        self,
        *,
        max_output_characters: int,
    ) -> tuple[ShellOverviewSnapshot, ...]:
        self.overview_max_characters = max_output_characters
        return self.overview_items

    def subscribe_overview_changes(
        self,
        listener: Callable[[], None],
    ) -> Callable[[], None]:
        del listener
        return lambda: None

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def make_service(manager: FakeManager) -> ShellToolService:
    host = create_host_config(
        host_profile=HostProfile.STANDALONE,
        runtime_selection=RuntimeSelection.POSIX_BASH,
        operating_system="linux",
        process_cwd="/workspace",
        inherited_environment={"TFBASH_TEST_SECRET": "must-not-leak"},
        startup_command="printf configured",
        environment_summary=EnvironmentSummary(),
        directory_exists=lambda _: True,
    )
    composition = compose_runtime(
        host,
        RuntimeBuilders(
            posix_bash=lambda: PosixBashProfile(
                dialect=BashDialect(),
                transport=PexpectPosixPtyTransport(),
                supervisor=PosixProcessSupervisor(),
            ),
            windows_pwsh=lambda: (_ for _ in ()).throw(AssertionError("not selected")),
        ),
    )
    config = ProtocolConfig(
        platform=PlatformName.LINUX,
        default_cwd="/workspace",
        shell="/bin/bash",
        startup_command="printf configured",
    )
    return ShellToolService(
        manager=manager,
        composition=composition,
        protocol_config=config,
        agent_context=composition.agent_context(shell_version="5.2.0"),
        directory_exists=lambda _: True,
    )


def test_open_preserves_omitted_and_explicit_null_startup_semantics() -> None:
    manager = FakeManager()
    service = make_service(manager)

    omitted = service.call("shell_open", {})
    explicit_null = service.call("shell_open", {"startup_command": None})

    assert omitted.isError is False
    assert explicit_null.isError is False
    assert [request.startup_command for request in manager.starts] == [
        "printf configured",
        None,
    ]


def test_successes_and_expected_errors_are_structured_contract_results() -> None:
    manager = FakeManager()
    service = make_service(manager)

    execution = service.call("shell_exec", {"shell_id": "shell_1", "command": "true"})
    read = service.call(
        "shell_read",
        {"shell_id": "shell_1", "exec_id": "exec_1", "cursor": 0},
    )
    write = service.call(
        "shell_write",
        {"shell_id": "shell_1", "exec_id": "exec_1", "text": "你好"},
    )
    signal = service.call(
        "shell_signal",
        {"shell_id": "shell_1", "exec_id": "exec_1", "signal": "interrupt"},
    )
    close = service.call("shell_close", {"shell_id": "shell_1"})
    invalid = service.call("shell_read", {"shell_id": "shell_1"})
    manager.exec_error = ShellBusy("private runtime detail")
    busy = service.call("shell_exec", {"shell_id": "shell_1", "command": "true"})

    assert execution.isError is False
    assert execution.structuredContent is not None
    assert execution.structuredContent["status"] == "exited"
    assert read.structuredContent is not None
    assert read.structuredContent["status"] == "running"
    assert write.structuredContent is not None
    assert write.structuredContent["accepted_bytes"] == 6
    assert manager.writes == [("shell_1", "exec_1", "你好".encode())]
    assert signal.structuredContent is not None
    assert signal.structuredContent["status"] == "delivered"
    assert close.structuredContent is not None
    assert close.structuredContent["cleanup_complete"] is True
    assert invalid.isError is True
    assert invalid.structuredContent is not None
    assert invalid.structuredContent["error"]["code"] == "invalid_argument"
    assert busy.isError is True
    assert busy.structuredContent is not None
    assert busy.structuredContent["error"] == {
        "code": "shell_busy",
        "message": "The shell is busy.",
        "retryable": True,
        "shell_id": "shell_1",
    }


def test_shell_list_exposes_context_but_not_inherited_environment_values() -> None:
    service = make_service(FakeManager())

    result = service.call("shell_list", {})

    assert result.isError is False
    assert result.structuredContent is not None
    assert result.structuredContent["runtime"]["shell_version"] == "5.2.0"
    assert result.structuredContent["host"]["environment"] == {"kind": "none"}
    assert "must-not-leak" not in _text(result)


def test_shell_overview_renders_context_status_and_unicode_tail_as_markdown() -> None:
    manager = FakeManager(
        overview_items=(
            ShellOverviewSnapshot(
                shell=ShellSnapshot(
                    "shell_1",
                    ShellState.READY,
                    "/workspace/a|b",
                    None,
                    1_700_000_000_000,
                ),
                execution=ExecutionOverviewSnapshot(
                    exec_id="exec_1",
                    status=ExecutionState.EXITED,
                    exit_code=0,
                    duration_ms=12,
                    output="尾部<ok>",
                    output_truncated=True,
                ),
            ),
        )
    )
    service = make_service(manager)

    markdown = service.shell_overview_markdown()

    assert manager.overview_max_characters == SHELL_OVERVIEW_OUTPUT_CHARACTERS == 500
    assert "# Shell Overview" in markdown
    assert "<code>shell_1</code>" in markdown
    assert "<code>/workspace/a&#124;b</code>" in markdown
    assert "2023-11-14T22:13:20.000Z" in markdown
    assert "Earlier output was truncated" in markdown
    assert "<pre>尾部&lt;ok&gt;</pre>" in markdown


def test_empty_shell_overview_has_explicit_placeholder() -> None:
    service = make_service(FakeManager())

    markdown = service.shell_overview_markdown()

    assert "- Shells: 0" in markdown
    assert "No active Shells." in markdown


def test_unknown_and_unexpected_failures_do_not_expose_exception_text() -> None:
    manager = FakeManager(exec_error=RuntimeError("secret-internal-detail"))
    service = make_service(manager)

    unknown = service.call("not_a_tool", {})
    unexpected = service.call("shell_exec", {"shell_id": "shell_1", "command": "true"})

    assert unknown.isError is True
    assert _text(unknown) == "Unknown tool."
    assert unexpected.isError is True
    assert _text(unexpected) == "The tool failed unexpectedly."
    assert "secret-internal-detail" not in _text(unexpected)


def test_shell_unavailable_retryability_is_frozen_by_domain_state() -> None:
    manager = FakeManager()
    service = make_service(manager)

    manager.exec_error = ShellUnavailable("rebuilding detail", retryable=True)
    rebuilding = service.call("shell_exec", {"shell_id": "shell_1", "command": "true"})
    manager.exec_error = ShellUnavailable("error detail", retryable=False)
    failed = service.call("shell_exec", {"shell_id": "shell_1", "command": "true"})

    assert rebuilding.structuredContent is not None
    assert rebuilding.structuredContent["error"]["retryable"] is True
    assert failed.structuredContent is not None
    assert failed.structuredContent["error"]["retryable"] is False


def test_out_of_matrix_domain_error_becomes_a_private_implementation_failure() -> None:
    manager = FakeManager(close_error=ShellBusy("must not become shell_busy"))
    service = make_service(manager)

    result = service.call("shell_close", {"shell_id": "shell_1"})

    assert result.isError is True
    assert result.structuredContent is None
    assert _text(result) == "The tool failed unexpectedly."


def test_shutdown_delegates_to_the_domain_manager() -> None:
    manager = FakeManager()
    service = make_service(manager)

    service.shutdown()

    assert manager.shutdown_calls == 1


def _text(result: types.CallToolResult) -> str:
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    return content.text
