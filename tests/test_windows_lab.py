from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from experiments.windows_phase0.contracts import (
    GATE_RULES,
    EnvironmentTier,
    Observation,
    Outcome,
    evaluate_gates,
    summary_payload,
)
from experiments.windows_phase0.lab import (
    EXPECTED_GATES,
    EXPECTED_OBSERVATIONS,
    JSON_BEGIN,
    JSON_END,
    RESULT_SCHEMA,
    RUNNER_COMMIT,
    SCP_AUTHENTICATED,
    TOOL_ARCHIVES,
    CommandResult,
    LabConfig,
    LabError,
    OpenSshTransport,
    RunInfo,
    WindowsLab,
    build_handoff_package,
    cli,
    load_env_file,
    parse_marked_json,
    verify_result_archive,
)


def test_env_file_preserves_quoted_space_password(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "# local secret",
                "TFBASH_WINDOWS_NAME=lab-01",
                "TFBASH_WINDOWS_HOST=192.168.50.215",
                "TFBASH_WINDOWS_PORT=23",
                "TFBASH_WINDOWS_USER=llg",
                'TFBASH_WINDOWS_PASSWORD=" "',
                r"TFBASH_WINDOWS_REMOTE_ROOT=C:\Users\llg\tfbash-windows-lab",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    values = load_env_file(env_file)
    config = LabConfig.from_env_file(env_file, repository_root=tmp_path)

    assert values["TFBASH_WINDOWS_PASSWORD"] == " "
    assert config.password == " "
    assert config.port == 23
    assert str(config.remote_root) == r"C:\Users\llg\tfbash-windows-lab"
    assert "password" not in repr(config)


@pytest.mark.parametrize(
    "line",
    [
        "NOT AN ASSIGNMENT",
        "1INVALID=value",
        'PASSWORD="unterminated',
        "DUPLICATE=first\nDUPLICATE=second",
    ],
)
def test_env_file_rejects_invalid_assignments(tmp_path: Path, line: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(line, encoding="utf-8")
    env_file.chmod(0o600)

    with pytest.raises(LabError):
        load_env_file(env_file)


def test_lab_config_rejects_drive_relative_remote_root(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TFBASH_WINDOWS_HOST=host",
                "TFBASH_WINDOWS_USER=user",
                "TFBASH_WINDOWS_PASSWORD=secret",
                r"TFBASH_WINDOWS_REMOTE_ROOT=\lab",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    with pytest.raises(LabError, match="drive-qualified"):
        LabConfig.from_env_file(env_file, repository_root=tmp_path)


@pytest.mark.parametrize(
    ("configured", "normalized"),
    [("EXAMPLE.com", "example.com"), ("[2001:0db8::1]", "2001:db8::1"), ("::1", "::1")],
)
def test_lab_config_normalizes_dns_and_ipv6_hosts(
    tmp_path: Path, configured: str, normalized: str
) -> None:
    config = _config(tmp_path, host=configured)

    assert config.host == normalized


def test_cli_reports_missing_env_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli(["--env-file", str(tmp_path / "missing.env"), "preflight"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Windows Lab error:" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode check")
def test_env_file_rejects_group_or_world_readable_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TFBASH_WINDOWS_PASSWORD=secret\n", encoding="utf-8")
    env_file.chmod(0o644)

    with pytest.raises(LabError, match="chmod 600"):
        load_env_file(env_file)


def test_marked_json_uses_last_complete_envelope() -> None:
    output = (
        f'banner {JSON_BEGIN}\n{{"old": true}}\n{JSON_END}\n'
        f'noise {JSON_BEGIN}\n{{"value": 7}}\n{JSON_END}\n'
    )

    assert parse_marked_json(output) == {"value": 7}


def test_ssh_authentication_stops_matching_prompts_after_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _FakeChild(
        [
            (0, "login", "password:"),
            (3, "\r\n", "TFBASH_SSH_CONNECTED_test"),
            (0, "\r\nremote password:\r\nPermission denied\r\n", ""),
        ]
    )
    transport = OpenSshTransport(_config(tmp_path))
    monkeypatch.setattr(
        "experiments.windows_phase0.lab.pexpect.spawn", lambda *args, **kwargs: child
    )

    result = transport._authenticated_process(
        "ssh",
        [],
        connected_pattern=re.compile("TFBASH_SSH_CONNECTED_test"),
        timeout_seconds=10,
    )

    assert result.exit_code == 0
    assert "Permission denied" in result.output
    assert child.sent == [" "]
    assert child.pattern_counts == [6, 6, 2]


def test_ssh_authentication_rejects_a_repeated_password_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _FakeChild([(0, "", "password:"), (0, "", "password:")])
    transport = OpenSshTransport(_config(tmp_path))
    monkeypatch.setattr(
        "experiments.windows_phase0.lab.pexpect.spawn", lambda *args, **kwargs: child
    )

    with pytest.raises(LabError, match="more than once"):
        transport._authenticated_process(
            "ssh",
            [],
            connected_pattern=re.compile("connected"),
            timeout_seconds=10,
        )

    assert child.sent == [" "]


def test_ssh_authentication_rejects_connection_without_configured_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = _FakeChild([(3, "", "TFBASH_SSH_CONNECTED_test")])
    transport = OpenSshTransport(_config(tmp_path))
    monkeypatch.setattr(
        "experiments.windows_phase0.lab.pexpect.spawn", lambda *args, **kwargs: child
    )

    with pytest.raises(LabError, match="without using the configured"):
        transport._authenticated_process(
            "ssh",
            [],
            connected_pattern=re.compile("TFBASH_SSH_CONNECTED_test"),
            timeout_seconds=10,
        )

    assert child.sent == []
    assert child.pattern_counts == [6]


def test_scp_connected_pattern_only_accepts_password_methods() -> None:
    assert SCP_AUTHENTICATED.search('Authenticated to host using "password"')
    assert SCP_AUTHENTICATED.search('Authenticated to host using "keyboard-interactive"')
    assert not SCP_AUTHENTICATED.search('Authenticated to host using "publickey"')
    assert not SCP_AUTHENTICATED.search("Authenticated to host using none")


def test_ssh_options_force_password_only_authentication(tmp_path: Path) -> None:
    options = OpenSshTransport(_config(tmp_path))._ssh_options()

    assert "PreferredAuthentications=keyboard-interactive,password" in options
    assert "PubkeyAuthentication=no" in options
    assert "NumberOfPasswordPrompts=1" in options


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("host.example", 22, "host.example"),
        ("192.0.2.1", 23, "[192.0.2.1]:23"),
        ("::1", 23, "[::1]:23"),
    ],
)
def test_known_host_target_handles_ports_and_ipv6(
    tmp_path: Path, host: str, port: int, expected: str
) -> None:
    transport = OpenSshTransport(_config(tmp_path, host=host, port=port))

    assert transport._known_host_target() == expected


def test_remove_known_host_verifies_hashed_entry_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "# Host found\n|1|hash key\n", ""),
            subprocess.CompletedProcess([], 0, "removed\n", ""),
            subprocess.CompletedProcess([], 1, "", ""),
        ]
    )

    def fake_run(args: list[str], **kwargs: object) -> object:
        calls.append(args)
        return next(responses)

    monkeypatch.setattr("experiments.windows_phase0.lab.subprocess.run", fake_run)
    transport = OpenSshTransport(_config(tmp_path, port=22))

    assert transport.remove_known_host()
    assert [call[1:3] for call in calls] == [["-F", "host"], ["-R", "host"], ["-F", "host"]]


def test_handoff_package_is_deterministic_and_self_describing(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]

    first = build_handoff_package(root, tmp_path / "first")
    second = build_handoff_package(root, tmp_path / "second")

    assert first.sha256 == second.sha256
    assert first.manifest["runner_commit"] == RUNNER_COMMIT
    with zipfile.ZipFile(first.path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert {"RUN_SSH_SMOKE.ps1", "RUN_WINDOWS11.ps1", "SOURCE_COMMIT.txt"} <= names
        assert manifest["schema"] == "tfbash-windows-lab-package/v1"
        for name, expected in manifest["files_sha256"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected


def test_windows_toolchain_is_fully_mac_supplied_and_runner_stays_offline() -> None:
    names = {name for name, _, _ in TOOL_ARCHIVES}
    assert names == {
        "uv-0.8.17.zip",
        "powershell-7.6.3.zip",
        "python-3.12.10-embed-amd64.zip",
        "pywinpty-3.0.5-cp312-cp312-win_amd64.whl",
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", sha256) for _, _, sha256 in TOOL_ARCHIVES)

    root = Path(__file__).resolve().parents[1]
    bootstrap = (
        root / "experiments/windows_phase0/infrastructure/Bootstrap-WindowsLab.ps1"
    ).read_text(encoding="utf-8")
    runner = (root / "experiments/windows_phase0/infrastructure/RUN_SSH_SMOKE.ps1").read_text(
        encoding="utf-8"
    )
    assert "Invoke-WebRequest" not in bootstrap
    assert "uv run" not in runner
    assert "& $PythonPath -c $pythonLauncher" in runner


def test_deploy_anchors_reuse_to_the_local_manifest_and_exact_file_set(tmp_path: Path) -> None:
    transport = _DeployTransport()
    root = Path(__file__).resolve().parents[1]
    lab = WindowsLab(repository_root=root, config=_config(tmp_path), transport=transport)

    package = lab.deploy()

    command = next(script for script in transport.scripts if "ExpectedManifestSha256" in script)
    verification_script = (
        root / "experiments/windows_phase0/infrastructure/Deploy-WindowsLabPackage.ps1"
    ).read_text(encoding="utf-8")
    with zipfile.ZipFile(package.path) as archive:
        manifest_hash = hashlib.sha256(archive.read("manifest.json")).hexdigest()
    assert manifest_hash in command
    assert "manifest checksum mismatch" in verification_script.lower()
    assert "unexpected or missing file" in verification_script.lower()
    assert "unsafe path" in verification_script.lower()


def test_verify_accepts_complete_inconclusive_ssh_result(tmp_path: Path) -> None:
    archive = _result_archive(tmp_path)

    report = verify_result_archive(archive)

    assert report.run_id == "run-test"
    assert report.observations == EXPECTED_OBSERVATIONS
    assert report.gates == EXPECTED_GATES
    assert report.experiment_exit_code == 1
    assert report.package_sha256 == "a" * 64
    assert not report.contract_passed


def test_verify_accepts_one_repetition_for_a_single_run_smoke(tmp_path: Path) -> None:
    archive = _result_archive(tmp_path, repetitions=1)

    report = verify_result_archive(archive)

    assert report.repetitions == 1
    assert report.observations == 21


def test_verify_rejects_ssh_result_that_claims_decision_ready(tmp_path: Path) -> None:
    archive = _result_archive(tmp_path, decision_ready=True, decision="no-go")

    with pytest.raises(LabError, match="never decision-ready"):
        verify_result_archive(archive)


def test_verify_rejects_modified_raw_evidence(tmp_path: Path) -> None:
    archive = _result_archive(tmp_path, corrupt_observations=True)

    with pytest.raises(LabError, match="hash mismatch"):
        verify_result_archive(archive)


def test_verify_rejects_zip_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../escape", "bad")

    with pytest.raises(LabError, match="unsafe path"):
        verify_result_archive(archive)


def test_verify_rejects_exit_code_that_contradicts_recomputed_gates(tmp_path: Path) -> None:
    archive = _result_archive(tmp_path, experiment_exit_code=0)

    with pytest.raises(LabError, match="contradicts"):
        verify_result_archive(archive)


@pytest.mark.parametrize(
    "metadata_override",
    [
        {"runner_commit": "0" * 40},
        {"evidence_tier": "windows11-native"},
        {"evidence_complete": False, "missing_evidence": ["summary.json"]},
    ],
)
def test_verify_rejects_inconsistent_control_metadata(
    tmp_path: Path, metadata_override: dict[str, object]
) -> None:
    archive = _result_archive(tmp_path, metadata_override=metadata_override)

    with pytest.raises(LabError):
        verify_result_archive(archive)


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "../../../../target", "/absolute", r"windows\separator", "x" * 65],
)
def test_collect_rejects_unsafe_run_id_before_download(tmp_path: Path, run_id: str) -> None:
    transport = _RecordingTransport()
    lab = WindowsLab(repository_root=tmp_path, config=_config(tmp_path), transport=transport)
    run = RunInfo(run_id, 3, lab.config.remote_root / "result.zip", "a" * 64, 1)

    with pytest.raises(LabError, match="run_id"):
        lab.collect(run)

    assert transport.downloads == []


def test_collect_rejects_invalid_remote_checksum_before_download(tmp_path: Path) -> None:
    transport = _RecordingTransport()
    lab = WindowsLab(repository_root=tmp_path, config=_config(tmp_path), transport=transport)
    run = RunInfo("run-valid", 3, lab.config.remote_root / "result.zip", "not-a-sha", 1)

    with pytest.raises(LabError, match="64 hexadecimal"):
        lab.collect(run)

    assert transport.downloads == []


def _result_archive(
    tmp_path: Path,
    *,
    decision_ready: bool = False,
    decision: str = "inconclusive",
    corrupt_observations: bool = False,
    experiment_exit_code: int = 1,
    metadata_override: dict[str, object] | None = None,
    repetitions: int = 3,
) -> Path:
    root = tmp_path / "result"
    evidence = root / "evidence"
    evidence.mkdir(parents=True)
    environment = {"runner_commit": RUNNER_COMMIT}
    observation_values = [
        Observation(
            rule.scenario,
            iteration,
            Outcome.FAIL if rule is GATE_RULES[0] and iteration == 1 else Outcome.PASS,
            1,
            {},
        )
        for rule in GATE_RULES
        for iteration in range(
            1, (rule.required_runs if rule.required_runs == 1 else repetitions) + 1
        )
    ]
    observations = "".join(
        json.dumps(
            {
                "scenario": value.scenario,
                "iteration": value.iteration,
                "outcome": value.outcome.value,
                "duration_ms": value.duration_ms,
                "details": value.details,
            }
        )
        + "\n"
        for value in observation_values
    )
    environment_path = evidence / "environment.json"
    observations_path = evidence / "observations.jsonl"
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    observations_path.write_text(observations, encoding="utf-8")
    summary = {
        **summary_payload(evaluate_gates(observation_values, EnvironmentTier.HOSTED_SMOKE)),
        "decision_ready": decision_ready,
        "decision": decision,
        "environment": environment,
        "evidence_files_sha256": {
            "environment.json": _sha256(environment_path),
            "observations.jsonl": _sha256(observations_path),
        },
    }
    (evidence / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (evidence / "summary.md").write_text("# summary\n", encoding="utf-8")
    metadata = {
        "schema": RESULT_SCHEMA,
        "run_id": "run-test",
        "launch_channel": "ssh",
        "runner_commit": RUNNER_COMMIT,
        "package_sha256": "a" * 64,
        "repetitions": repetitions,
        "evidence_tier": "hosted-smoke",
        "evidence_complete": True,
        "missing_evidence": [],
    }
    metadata.update(metadata_override or {})
    (root / "control-metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    (root / "exit-code.json").write_text(
        json.dumps({"experiment_exit_code": experiment_exit_code}), encoding="utf-8"
    )
    (root / "terminal.log").write_text("runner output\n", encoding="utf-8")
    if corrupt_observations:
        observations_path.write_text(observations + "{}\n", encoding="utf-8")
    archive = tmp_path / "result.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stream.write(path, path.relative_to(root).as_posix())
    return archive


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path, *, host: str = "host", port: int = 23) -> LabConfig:
    env_file = tmp_path / f"config-{len(list(tmp_path.glob('config-*')))}.env"
    env_file.write_text(
        "\n".join(
            (
                f"TFBASH_WINDOWS_HOST={host}",
                f"TFBASH_WINDOWS_PORT={port}",
                "TFBASH_WINDOWS_USER=user",
                'TFBASH_WINDOWS_PASSWORD=" "',
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return LabConfig.from_env_file(env_file, repository_root=tmp_path)


class _FakeChild:
    def __init__(self, events: list[tuple[int, str, str]]) -> None:
        self._events = iter(events)
        self.before = ""
        self.after = ""
        self.exitstatus: int | None = 0
        self.signalstatus: int | None = None
        self.sent: list[str] = []
        self.pattern_counts: list[int] = []

    def expect(self, patterns: list[object]) -> int:
        self.pattern_counts.append(len(patterns))
        matched, self.before, self.after = next(self._events)
        return matched

    def sendline(self, value: str) -> None:
        self.sent.append(value)

    def close(self, force: bool = False) -> None:
        del force


class _RecordingTransport:
    def __init__(self) -> None:
        self.downloads: list[tuple[PureWindowsPath, Path]] = []

    def run_powershell(self, script: str, *, timeout_seconds: int = 120) -> CommandResult:
        del script, timeout_seconds
        return CommandResult(0, "")

    def upload(self, local_path: Path, remote_path: PureWindowsPath) -> None:
        del local_path, remote_path

    def download(self, remote_path: PureWindowsPath, local_path: Path) -> None:
        self.downloads.append((remote_path, local_path))


class _DeployTransport(_RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []

    def run_powershell(self, script: str, *, timeout_seconds: int = 120) -> CommandResult:
        del timeout_seconds
        self.scripts.append(script)
        if "ExpectedManifestSha256" not in script:
            return CommandResult(0, "")
        package_hash = re.search(r"-ExpectedPackageSha256 '([0-9a-f]{64})'", script)
        assert package_hash is not None
        output = (
            f"{JSON_BEGIN}\n"
            + json.dumps({"package_sha256": package_hash.group(1), "package_root": "C:\\lab"})
            + f"\n{JSON_END}\n"
        )
        return CommandResult(0, output)
