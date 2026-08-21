"""Pre-registered result contract for the Windows Phase 0 experiment."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO


class EnvironmentTier(str, Enum):
    """Evidence strength of an experiment execution environment."""

    HOSTED_SMOKE = "hosted-smoke"
    NATIVE_GATE = "native-gate"


class Outcome(str, Enum):
    """Outcome of one pre-registered experiment observation."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Observation:
    """One independently auditable scenario observation."""

    scenario: str
    iteration: int
    outcome: Outcome
    duration_ms: int
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class GateRule:
    """The fixed pass rule for one decision-changing scenario."""

    scenario: str
    required_passes: int
    required_runs: int
    native_required: bool = True


GATE_RULES: tuple[GateRule, ...] = (
    GateRule("unicode", 1, 1),
    GateRule("persistent_state", 1, 1),
    GateRule("multiline", 1, 1),
    GateRule("real_exit_code", 1, 1),
    GateRule("text_stdin", 1, 1),
    GateRule("raw_nul_stdin", 1, 1),
    GateRule("long_command_yield", 1, 1),
    GateRule("backpressure_control", 1, 1),
    GateRule("tail_drain", 20, 20),
    GateRule("interrupt_recovery", 20, 20),
    GateRule("timeout_rebuild", 20, 20),
    GateRule("eof_preserves_shell", 20, 20),
    GateRule("unique_terminal_state", 20, 20),
    GateRule("toolhelp_terminate_tree", 20, 20),
    GateRule("toolhelp_kill_tree", 20, 20),
    GateRule("toolhelp_close_tree", 20, 20),
    GateRule("toolhelp_shutdown_tree", 20, 20),
    GateRule("job_terminate_tree", 20, 20),
    GateRule("job_kill_tree", 20, 20),
    GateRule("job_close_tree", 20, 20),
    GateRule("job_shutdown_tree", 20, 20),
)


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    """Evaluation of one gate against collected observations."""

    scenario: str
    passed: int
    failed: int
    skipped: int
    required_passes: int
    required_runs: int
    complete: bool
    accepted: bool


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """Aggregate decision status without over-claiming hosted evidence."""

    environment_tier: EnvironmentTier
    gates: tuple[GateEvaluation, ...]
    all_observed_gates_pass: bool
    decision_ready: bool


def evaluate_gates(
    observations: Iterable[Observation], environment_tier: EnvironmentTier
) -> DecisionSummary:
    """Evaluate observations against the immutable gate table."""

    grouped: dict[str, list[Observation]] = {}
    for observation in observations:
        grouped.setdefault(observation.scenario, []).append(observation)

    evaluations: list[GateEvaluation] = []
    for rule in GATE_RULES:
        values = grouped.get(rule.scenario, [])
        passed = sum(value.outcome is Outcome.PASS for value in values)
        failed = sum(value.outcome is Outcome.FAIL for value in values)
        skipped = sum(value.outcome is Outcome.SKIP for value in values)
        complete = len(values) == rule.required_runs
        accepted = complete and passed >= rule.required_passes and failed == 0 and skipped == 0
        evaluations.append(
            GateEvaluation(
                scenario=rule.scenario,
                passed=passed,
                failed=failed,
                skipped=skipped,
                required_passes=rule.required_passes,
                required_runs=rule.required_runs,
                complete=complete,
                accepted=accepted,
            )
        )

    all_pass = all(evaluation.accepted for evaluation in evaluations)
    native = environment_tier is EnvironmentTier.NATIVE_GATE
    return DecisionSummary(
        environment_tier=environment_tier,
        gates=tuple(evaluations),
        all_observed_gates_pass=all_pass,
        decision_ready=all_pass and native,
    )


class JsonlRecorder:
    """Append-only recorder that flushes each observation immediately."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: TextIO = path.open("w", encoding="utf-8", newline="\n")

    def record(self, observation: Observation) -> None:
        payload = asdict(observation)
        payload["outcome"] = observation.outcome.value
        self._stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> JsonlRecorder:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def summary_payload(summary: DecisionSummary) -> dict[str, object]:
    """Return a stable JSON-compatible representation of a summary."""

    return {
        "environment_tier": summary.environment_tier.value,
        "all_observed_gates_pass": summary.all_observed_gates_pass,
        "decision_ready": summary.decision_ready,
        "gates": [asdict(gate) for gate in summary.gates],
    }
