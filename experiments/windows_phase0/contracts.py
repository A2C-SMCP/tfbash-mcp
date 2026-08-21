"""Pre-registered result contract for the Windows Phase 0 experiment."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, TextIO


class EnvironmentTier(str, Enum):
    """Evidence strength of an experiment execution environment."""

    HOSTED_SMOKE = "hosted-smoke"
    NATIVE_GATE = "native-gate"


class Outcome(str, Enum):
    """Outcome of one pre-registered experiment observation."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class Decision(str, Enum):
    """Decision supported by the collected evidence."""

    GO = "go"
    NO_GO = "no-go"
    INCONCLUSIVE = "inconclusive"


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
    evidence_complete: bool
    contract_passed: bool
    decision_ready: bool
    decision: Decision


class WaitSignal(Protocol):
    """Minimal event interface used by the backpressure decision helper."""

    def wait(self, timeout: float | None = None) -> bool: ...


def observe_backpressure(
    writer_started: WaitSignal,
    writer_done: WaitSignal,
    *,
    start_deadline_seconds: float,
    establish_deadline_seconds: float,
) -> bool:
    """Prove a writer started and remained pending for a bounded interval."""

    if not writer_started.wait(start_deadline_seconds):
        raise RuntimeError("backpressure writer did not start")
    return not writer_done.wait(establish_deadline_seconds)


def prepare_output_directory(path: Path) -> None:
    """Create a fresh evidence directory and reject any pre-existing contents."""

    if path.exists():
        if not path.is_dir():
            raise RuntimeError(f"evidence output path is not a directory: {path}")
        if next(path.iterdir(), None) is not None:
            raise RuntimeError(f"evidence output directory must be empty: {path}")
        return
    path.mkdir(parents=True)


def validate_toolchain(
    *,
    python_version: str,
    python_architecture: str,
    pywinpty_version: str,
    uv_version: str,
) -> None:
    """Fail closed when evidence is produced by a different experiment toolchain."""

    if python_version != "3.12.10":
        raise RuntimeError(f"Python 3.12.10 is required, observed {python_version}")
    if python_architecture.upper() not in {"AMD64", "X86_64"}:
        raise RuntimeError(f"x64 Python is required, observed architecture {python_architecture}")
    if pywinpty_version != "3.0.5":
        raise RuntimeError(f"pywinpty 3.0.5 is required, observed {pywinpty_version}")
    if not uv_version.startswith("uv 0.8.17 "):
        raise RuntimeError(f"uv 0.8.17 is required, observed {uv_version}")


def validate_environment(windows: dict[str, object], tier: EnvironmentTier) -> None:
    """Validate the non-localized native evidence boundary."""

    version = str(windows["PowerShell"])
    os_architecture = str(windows["RuntimeOSArchitecture"])
    process_architecture = str(windows["RuntimeProcessArchitecture"])
    if not version.startswith("7.6."):
        raise RuntimeError(f"PowerShell 7.6.x is required, observed {version}")
    if os_architecture != "X64" or process_architecture != "X64":
        raise RuntimeError(
            "x64 OS and PowerShell process are required, observed "
            f"OS={os_architecture}, process={process_architecture}"
        )
    if tier is EnvironmentTier.NATIVE_GATE:
        product_type = int(windows["ProductType"])
        parts = str(windows["Version"]).split(".")
        try:
            build = int(parts[2])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"unrecognized Windows version: {windows['Version']}") from exc
        if product_type != 1 or build < 22000:
            raise RuntimeError(
                "native gate requires Windows 11 client x64 "
                f"(ProductType=1, build>=22000), observed ProductType={product_type}, "
                f"build={build}"
            )


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
        expected_iterations = set(range(1, rule.required_runs + 1))
        observed_iterations = {value.iteration for value in values}
        complete = len(values) == rule.required_runs and observed_iterations == expected_iterations
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

    evidence_complete = all(evaluation.complete for evaluation in evaluations)
    contract_passed = all(evaluation.accepted for evaluation in evaluations)
    native = environment_tier is EnvironmentTier.NATIVE_GATE
    decision_ready = native and evidence_complete
    if not decision_ready:
        decision = Decision.INCONCLUSIVE
    elif contract_passed:
        decision = Decision.GO
    else:
        decision = Decision.NO_GO
    return DecisionSummary(
        environment_tier=environment_tier,
        gates=tuple(evaluations),
        evidence_complete=evidence_complete,
        contract_passed=contract_passed,
        decision_ready=decision_ready,
        decision=decision,
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
        "evidence_complete": summary.evidence_complete,
        "contract_passed": summary.contract_passed,
        "decision_ready": summary.decision_ready,
        "decision": summary.decision.value,
        "gates": [asdict(gate) for gate in summary.gates],
    }
