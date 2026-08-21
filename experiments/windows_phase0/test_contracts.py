from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from experiments.windows_phase0 import runner, windows_api
from experiments.windows_phase0.conpty_session import (
    CommandTicket,
    ConPtySession,
    parse_command_output,
)
from experiments.windows_phase0.contracts import (
    GATE_RULES,
    Decision,
    EnvironmentTier,
    JsonlRecorder,
    Observation,
    Outcome,
    evaluate_gates,
    observe_backpressure,
    prepare_output_directory,
    summary_payload,
    validate_environment,
    validate_toolchain,
)


def _passing_observations() -> list[Observation]:
    return [
        Observation(rule.scenario, iteration, Outcome.PASS, 1, {})
        for rule in GATE_RULES
        for iteration in range(1, rule.required_runs + 1)
    ]


def test_hosted_evidence_never_marks_the_windows_11_decision_ready() -> None:
    summary = evaluate_gates(_passing_observations(), EnvironmentTier.HOSTED_SMOKE)

    assert summary.evidence_complete
    assert summary.contract_passed
    assert not summary.decision_ready
    assert summary.decision is Decision.INCONCLUSIVE


def test_native_evidence_requires_every_pre_registered_observation() -> None:
    observations = _passing_observations()
    observations.pop()

    summary = evaluate_gates(observations, EnvironmentTier.NATIVE_GATE)

    assert not summary.evidence_complete
    assert not summary.contract_passed
    assert not summary.decision_ready
    assert summary.decision is Decision.INCONCLUSIVE
    assert not summary.gates[-1].complete


def test_one_failure_rejects_a_gate_even_when_the_run_count_is_complete() -> None:
    observations = _passing_observations()
    target = next(item for item in observations if item.scenario == "tail_drain")
    observations[observations.index(target)] = Observation(
        target.scenario,
        target.iteration,
        Outcome.FAIL,
        target.duration_ms,
        {"sentinel_seen": False},
    )

    summary = evaluate_gates(observations, EnvironmentTier.NATIVE_GATE)
    tail = next(gate for gate in summary.gates if gate.scenario == "tail_drain")

    assert tail.failed == 1
    assert not tail.accepted
    assert summary.evidence_complete
    assert summary.decision_ready
    assert summary.decision is Decision.NO_GO


def test_native_complete_passing_evidence_is_a_go() -> None:
    summary = evaluate_gates(_passing_observations(), EnvironmentTier.NATIVE_GATE)

    assert summary.evidence_complete
    assert summary.contract_passed
    assert summary.decision_ready
    assert summary.decision is Decision.GO


def test_duplicate_iteration_does_not_make_a_gate_complete() -> None:
    observations = _passing_observations()
    final_rule = GATE_RULES[-1]
    final_observations = [item for item in observations if item.scenario == final_rule.scenario]
    final_observations[-1] = Observation(
        final_rule.scenario,
        final_observations[-2].iteration,
        Outcome.PASS,
        1,
        {},
    )
    observations = [
        item for item in observations if item.scenario != final_rule.scenario
    ] + final_observations

    summary = evaluate_gates(observations, EnvironmentTier.NATIVE_GATE)

    assert not summary.evidence_complete
    assert not summary.gates[-1].complete
    assert summary.decision is Decision.INCONCLUSIVE


def test_jsonl_recorder_flushes_a_machine_readable_observation(tmp_path) -> None:
    path = tmp_path / "raw.jsonl"
    observation = Observation("unicode", 1, Outcome.PASS, 4, {"text": "中文😀"})

    with JsonlRecorder(path) as recorder:
        recorder.record(observation)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "details": {"text": "中文😀"},
        "duration_ms": 4,
        "iteration": 1,
        "outcome": "pass",
        "scenario": "unicode",
    }


def test_summary_payload_contains_no_enum_instances() -> None:
    summary = evaluate_gates(_passing_observations(), EnvironmentTier.NATIVE_GATE)

    encoded = json.dumps(summary_payload(summary), sort_keys=True)

    assert '"decision_ready": true' in encoded


def test_command_output_parser_excludes_transport_markers_and_prompt() -> None:
    ticket = CommandTicket(0, "BEGIN", "END=", "PROMPT")
    raw = "terminal-noise\r\nBEGIN\r\n中文😀\r\nEND=37\r\nPROMPT"

    output, exit_code = parse_command_output(raw, ticket)

    assert output == "中文😀\r\n"
    assert exit_code == 37


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        ("END=0\r\nPROMPT", "begin marker"),
        ("BEGIN\r\nPROMPT", "exit marker"),
        ("BEGIN\r\nEND=nope\r\nPROMPT", "exit marker payload"),
    ),
)
def test_command_output_parser_rejects_incomplete_protocol(raw: str, message: str) -> None:
    ticket = CommandTicket(0, "BEGIN", "END=", "PROMPT")

    with pytest.raises(RuntimeError, match=message):
        parse_command_output(raw, ticket)


class _FakePty:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def isalive(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("query", "response"),
    (("\x1b[5n", "\x1b[0n"), ("\x1b[6n", "\x1b[1;1R")),
)
def test_terminal_query_response_handles_every_chunk_boundary(query: str, response: str) -> None:
    for split_at in range(1, len(query)):
        session = ConPtySession("pwsh")
        fake = _FakePty()
        session._pty = fake  # type: ignore[assignment]

        session._answer_terminal_queries(query[:split_at])
        session._answer_terminal_queries(query[split_at:])

        assert fake.writes == [response]


def test_close_rejects_a_live_process_even_after_reader_stopped() -> None:
    session = ConPtySession("pwsh")
    session._pty = _FakePty()  # type: ignore[assignment]
    session._reader_done = True

    with pytest.raises(RuntimeError, match="remained alive"):
        session.close()


def test_write_entry_probe_runs_immediately_before_transport_write() -> None:
    session = ConPtySession("pwsh")
    fake = _FakePty()
    session._pty = fake  # type: ignore[assignment]
    entered: list[bool] = []

    session.write("payload", on_enter=lambda: entered.append(not fake.writes))

    assert entered == [True]
    assert fake.writes == ["payload"]


def test_open_matching_process_treats_only_missing_pid_as_dead(monkeypatch) -> None:
    identity = windows_api.ProcessIdentity(123, 456)

    def missing(*_args: object, **_kwargs: object) -> None:
        raise OSError(windows_api.ERROR_INVALID_PARAMETER, "missing")

    monkeypatch.setattr(windows_api, "open_process", missing)
    assert windows_api._open_matching_process(identity, 0) is None


def test_open_matching_process_propagates_indeterminate_query_failure(monkeypatch) -> None:
    identity = windows_api.ProcessIdentity(123, 456)

    def access_denied(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "access denied")

    monkeypatch.setattr(windows_api, "open_process", access_denied)
    with pytest.raises(OSError, match="access denied"):
        windows_api._open_matching_process(identity, 0)


def _windows_environment(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "PowerShell": "7.6.3",
        "RuntimeOSArchitecture": "X64",
        "RuntimeProcessArchitecture": "X64",
        "ProductType": 1,
        "Version": "10.0.26100",
    }
    values.update(overrides)
    return values


def test_native_environment_uses_non_localized_windows_11_x64_identity() -> None:
    validate_environment(_windows_environment(), EnvironmentTier.NATIVE_GATE)


@pytest.mark.parametrize(
    "overrides",
    (
        {"RuntimeOSArchitecture": "Arm64"},
        {"RuntimeProcessArchitecture": "X86"},
        {"ProductType": 3},
        {"Version": "10.0.19045"},
    ),
)
def test_native_environment_rejects_wrong_architecture_or_server_or_windows_10(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError):
        validate_environment(
            _windows_environment(**overrides),
            EnvironmentTier.NATIVE_GATE,
        )


class _WaitSignal:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.deadlines: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.deadlines.append(timeout)
        return self.result


def test_backpressure_requires_started_writer_that_remains_pending() -> None:
    started = _WaitSignal(True)
    pending = _WaitSignal(False)

    established = observe_backpressure(
        started,
        pending,
        start_deadline_seconds=1.0,
        establish_deadline_seconds=0.25,
    )

    assert established
    assert started.deadlines == [1.0]
    assert pending.deadlines == [0.25]


def test_backpressure_is_not_established_when_writer_already_finished() -> None:
    assert not observe_backpressure(
        _WaitSignal(True),
        _WaitSignal(True),
        start_deadline_seconds=1.0,
        establish_deadline_seconds=0.25,
    )


def test_output_directory_must_be_empty_to_prevent_stale_decisions(tmp_path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    stale_summary = output / "summary.json"
    stale_summary.write_text('{"decision_ready":true}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be empty"):
        prepare_output_directory(output)

    assert stale_summary.read_text(encoding="utf-8") == '{"decision_ready":true}'


def test_output_directory_is_created_when_absent(tmp_path) -> None:
    output = tmp_path / "evidence"

    prepare_output_directory(output)

    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_toolchain_contract_accepts_only_pinned_x64_versions() -> None:
    validate_toolchain(
        python_version="3.12.10",
        python_architecture="AMD64",
        pywinpty_version="3.0.5",
        uv_version="uv 0.8.17 (build)",
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"python_version": "3.12.11"},
        {"python_architecture": "ARM64"},
        {"pywinpty_version": "3.0.6"},
        {"uv_version": "uv 0.8.18 (build)"},
    ),
)
def test_toolchain_contract_rejects_unpinned_or_non_x64_versions(
    overrides: dict[str, str],
) -> None:
    values = {
        "python_version": "3.12.10",
        "python_architecture": "AMD64",
        "pywinpty_version": "3.0.5",
        "uv_version": "uv 0.8.17 (build)",
        **overrides,
    }
    with pytest.raises(RuntimeError):
        validate_toolchain(**values)


class _SuccessfulEofSession:
    def start(self) -> None:
        pass

    def start_script(self, _script: str) -> SimpleNamespace:
        return SimpleNamespace(cursor=0)

    def wait_for_text(self, _needle: str, _cursor: int, _timeout_seconds: float) -> str:
        return "ready"

    def write(self, _value: str) -> int:
        return 1

    def await_script(self, _ticket: SimpleNamespace, _timeout_seconds: float) -> SimpleNamespace:
        return SimpleNamespace(output="EOF_b=0\r\n", exit_code=0)

    def run_script(self, _script: str) -> SimpleNamespace:
        return SimpleNamespace(output="PROBE_c\r\n", exit_code=0)


def test_eof_gate_cannot_pass_when_verified_session_close_fails(monkeypatch) -> None:
    session = _SuccessfulEofSession()
    values = iter(("a", "b", "c"))
    close_attempts: list[object] = []

    monkeypatch.setattr(runner, "ConPtySession", lambda _pwsh: session)
    monkeypatch.setattr(
        runner.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(values)),
    )

    def close_failure(observed: object) -> None:
        close_attempts.append(observed)
        raise RuntimeError("forced cleanup required")

    monkeypatch.setattr(runner, "_close_session_verified", close_failure)

    passed, details = runner._attempt_eof("pwsh", "\x1a\r\n")

    assert not passed
    assert close_attempts == [session]
    assert details["close_failure"]["exception"] == "forced cleanup required"
