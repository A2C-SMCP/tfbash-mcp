from __future__ import annotations

import json

from experiments.windows_phase0.contracts import (
    GATE_RULES,
    EnvironmentTier,
    JsonlRecorder,
    Observation,
    Outcome,
    evaluate_gates,
    summary_payload,
)


def _passing_observations() -> list[Observation]:
    return [
        Observation(rule.scenario, iteration, Outcome.PASS, 1, {})
        for rule in GATE_RULES
        for iteration in range(1, rule.required_runs + 1)
    ]


def test_hosted_evidence_never_marks_the_windows_11_decision_ready() -> None:
    summary = evaluate_gates(_passing_observations(), EnvironmentTier.HOSTED_SMOKE)

    assert summary.all_observed_gates_pass
    assert not summary.decision_ready


def test_native_evidence_requires_every_pre_registered_observation() -> None:
    observations = _passing_observations()
    observations.pop()

    summary = evaluate_gates(observations, EnvironmentTier.NATIVE_GATE)

    assert not summary.all_observed_gates_pass
    assert not summary.decision_ready
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
    assert not summary.decision_ready


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
