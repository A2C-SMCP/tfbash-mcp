"""Evidence contract shared by the Windows probe and Mac-side verifier."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

SCHEMA = "tfbash-windows-supervisor-evidence/v1"
RESULT_SCHEMA = "tfbash-windows-supervisor-result/v1"
NATIVE_GATE_REPETITIONS = 20
SSH_SMOKE_MAX_REPETITIONS = 5
REQUIRED_CHECKS = (
    "bootstrap_in_job",
    "shell_in_job",
    "grandchild_in_job",
    "execution_cleanup_zero_descendants",
    "shell_survived_execution_cleanup",
    "interrupt_delivered",
    "shell_recovered_after_interrupt",
    "tail_output_preserved",
    "exit_code_preserved",
    "shell_cleanup_zero_residue",
)


class EvidenceError(ValueError):
    """The evidence is incomplete, inconsistent, or not decision-ready."""


@dataclass(frozen=True, slots=True)
class EvidenceDecision:
    repetitions: int
    passed_iterations: int
    contract_passed: bool
    decision_ready: bool
    decision: str


def evaluate_evidence(payload: Mapping[str, object]) -> EvidenceDecision:
    if payload.get("schema") != SCHEMA:
        raise EvidenceError("unexpected supervisor evidence schema")
    tier = payload.get("evidence_tier")
    if tier not in {"hosted-smoke", "native-gate"}:
        raise EvidenceError("unexpected supervisor evidence tier")
    repetitions = _required_int(payload, "repetitions")
    if tier == "hosted-smoke" and not 1 <= repetitions <= SSH_SMOKE_MAX_REPETITIONS:
        raise EvidenceError("SSH smoke repetitions must be between 1 and 5")
    if tier == "native-gate" and repetitions != NATIVE_GATE_REPETITIONS:
        raise EvidenceError("the native supervisor gate requires exactly 20 repetitions")
    environment = payload.get("environment")
    if not isinstance(environment, Mapping):
        raise EvidenceError("environment evidence is missing")
    required_environment = {
        "windows_native": True,
        "os_x64": True,
        "python_x64": True,
        "powershell_version": "7.6.3",
        "pywinpty_version": "3.0.5",
    }
    for key, expected in required_environment.items():
        if environment.get(key) != expected:
            raise EvidenceError(f"environment gate failed: {key}")
    if tier == "native-gate":
        for key in ("windows_client", "windows_11"):
            if environment.get(key) is not True:
                raise EvidenceError(f"environment gate failed: {key}")
    iterations = payload.get("iterations")
    if not isinstance(iterations, Sequence) or isinstance(iterations, str):
        raise EvidenceError("iteration evidence is missing")
    if len(iterations) != repetitions:
        raise EvidenceError("iteration evidence count differs from repetitions")
    passed = 0
    for expected_iteration, raw in enumerate(iterations, 1):
        if not isinstance(raw, Mapping):
            raise EvidenceError(f"iteration {expected_iteration} is not an object")
        if _required_int(raw, "iteration") != expected_iteration:
            raise EvidenceError("iteration evidence is duplicated or out of order")
        checks = raw.get("checks")
        if not isinstance(checks, Mapping) or set(checks) != set(REQUIRED_CHECKS):
            raise EvidenceError(f"iteration {expected_iteration} has an incomplete check matrix")
        iteration_passed = all(checks[name] is True for name in REQUIRED_CHECKS)
        if raw.get("passed") is not iteration_passed:
            raise EvidenceError(f"iteration {expected_iteration} pass flag is inconsistent")
        if iteration_passed:
            passed += 1
    contract_passed = passed == repetitions
    decision_ready = tier == "native-gate" and contract_passed
    decision = "pass" if decision_ready else "inconclusive"
    expected_summary = {
        "passed_iterations": passed,
        "contract_passed": contract_passed,
        "decision_ready": decision_ready,
        "decision": decision,
    }
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        raise EvidenceError("evidence summary is missing")
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            raise EvidenceError(f"evidence summary is inconsistent: {key}")
    return EvidenceDecision(
        repetitions=repetitions,
        passed_iterations=passed,
        contract_passed=contract_passed,
        decision_ready=decision_ready,
        decision=decision,
    )


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvidenceError(f"field must be an integer: {key}")
    return value
