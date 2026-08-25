from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from experiments.windows_phase0.lab import LabError
from experiments.windows_supervisor_native.contracts import (
    REQUIRED_CHECKS,
    RESULT_SCHEMA,
    SCHEMA,
    EvidenceError,
    evaluate_evidence,
)
from experiments.windows_supervisor_native.lab import verify_supervisor_result


def _evidence(*, tier: str = "hosted-smoke", repetitions: int = 3) -> dict[str, object]:
    iterations = [
        {
            "iteration": iteration,
            "duration_ms": 100,
            "checks": {name: True for name in REQUIRED_CHECKS},
            "passed": True,
            "diagnostics": {},
        }
        for iteration in range(1, repetitions + 1)
    ]
    decision_ready = tier == "native-gate" and repetitions == 20
    return {
        "schema": SCHEMA,
        "evidence_tier": tier,
        "repetitions": repetitions,
        "environment": {
            "windows_client": True,
            "windows_11": True,
            "os_x64": True,
            "python_x64": True,
            "powershell_version": "7.6.3",
            "pywinpty_version": "3.0.5",
            "source_commit": "a" * 40,
        },
        "iterations": iterations,
        "summary": {
            "passed_iterations": repetitions,
            "contract_passed": True,
            "decision_ready": decision_ready,
            "decision": "pass" if decision_ready else "inconclusive",
        },
    }


def test_ssh_smoke_can_pass_contract_but_never_unlock_production() -> None:
    decision = evaluate_evidence(_evidence())

    assert decision.contract_passed
    assert not decision.decision_ready
    assert decision.decision == "inconclusive"


def test_native_gate_requires_exactly_twenty_complete_iterations() -> None:
    decision = evaluate_evidence(_evidence(tier="native-gate", repetitions=20))

    assert decision.decision_ready
    assert decision.decision == "pass"


@pytest.mark.parametrize("repetitions", [1, 5, 19, 21])
def test_native_gate_rejects_any_non_twenty_repetition_count(repetitions: int) -> None:
    payload = _evidence(tier="native-gate", repetitions=repetitions)

    with pytest.raises(EvidenceError, match="exactly 20"):
        evaluate_evidence(payload)


def test_evaluator_recomputes_each_check_instead_of_trusting_summary() -> None:
    payload = _evidence()
    iterations = payload["iterations"]
    assert isinstance(iterations, list)
    iterations[1]["checks"]["grandchild_in_job"] = False

    with pytest.raises(EvidenceError, match="pass flag is inconsistent"):
        evaluate_evidence(payload)


def test_verifier_binds_result_to_source_commit_and_keeps_ssh_inconclusive(
    tmp_path: Path,
) -> None:
    evidence = _evidence()
    archive = tmp_path / "result.zip"
    metadata = {
        "schema": RESULT_SCHEMA,
        "run_id": "supervisor-test",
        "launch_channel": "ssh",
        "evidence_tier": "hosted-smoke",
        "source_commit": "a" * 40,
        "package_sha256": "b" * 64,
        "repetitions": 3,
        "evidence_complete": True,
    }
    with zipfile.ZipFile(archive, "w") as result:
        result.writestr("control-metadata.json", json.dumps(metadata))
        result.writestr("exit-code.json", json.dumps({"experiment_exit_code": 0}))
        result.writestr("supervisor-evidence.json", json.dumps(evidence))
        result.writestr("terminal.log", "native probe output")

    report = verify_supervisor_result(archive)

    assert report.source_commit == "a" * 40
    assert report.decision.contract_passed
    assert not report.decision.decision_ready


def test_verifier_rejects_source_commit_mismatch(tmp_path: Path) -> None:
    evidence = _evidence()
    archive = tmp_path / "result.zip"
    metadata = {
        "schema": RESULT_SCHEMA,
        "run_id": "supervisor-test",
        "launch_channel": "ssh",
        "evidence_tier": "hosted-smoke",
        "source_commit": "c" * 40,
        "package_sha256": "b" * 64,
        "evidence_complete": True,
    }
    with zipfile.ZipFile(archive, "w") as result:
        result.writestr("control-metadata.json", json.dumps(metadata))
        result.writestr("exit-code.json", json.dumps({"experiment_exit_code": 0}))
        result.writestr("supervisor-evidence.json", json.dumps(evidence))

    with pytest.raises(LabError, match="source commit mismatch"):
        verify_supervisor_result(archive)
