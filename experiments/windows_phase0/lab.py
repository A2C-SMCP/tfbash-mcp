"""Reusable Mac-side control plane for the Windows Phase 0 experiment."""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol, cast

import pexpect  # type: ignore[import-untyped]

from experiments.windows_phase0.contracts import (
    GATE_RULES,
    DecisionSummary,
    EnvironmentTier,
    Observation,
    Outcome,
    evaluate_gates,
    summary_payload,
)

RUNNER_COMMIT = "61e36d30ac70893b5dd9bdf0745ef3ae1e50f0d7"
RUNNER_FILES = (
    "experiments/__init__.py",
    "experiments/windows_phase0/README.md",
    "experiments/windows_phase0/__init__.py",
    "experiments/windows_phase0/conpty_session.py",
    "experiments/windows_phase0/contracts.py",
    "experiments/windows_phase0/late_output_fixture.py",
    "experiments/windows_phase0/runner.py",
    "experiments/windows_phase0/test_contracts.py",
    "experiments/windows_phase0/tree_fixture.py",
    "experiments/windows_phase0/windows_api.py",
)
PACKAGE_CONTROL_FILES = (
    "RUN_SSH_SMOKE.ps1",
    "RUN_WINDOWS11.ps1",
)
PACKAGE_SCHEMA = "tfbash-windows-lab-package/v1"
RESULT_SCHEMA = "tfbash-windows-lab-result/v1"
FIXED_ZIP_TIME = (2026, 8, 24, 0, 0, 0)
DEFAULT_SSH_REPETITIONS = 3
EXPECTED_OBSERVATIONS = sum(
    rule.required_runs if rule.required_runs == 1 else DEFAULT_SSH_REPETITIONS
    for rule in GATE_RULES
)
EXPECTED_GATES = 21
JSON_BEGIN = "TFBASH_JSON_BEGIN"
JSON_END = "TFBASH_JSON_END"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_DNS_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SCP_AUTHENTICATED = re.compile(r'(?i)authenticated to .+ using "(?:password|keyboard-interactive)"')
TOOL_ARCHIVES = (
    (
        "uv-0.8.17.zip",
        "https://github.com/astral-sh/uv/releases/download/0.8.17/uv-x86_64-pc-windows-msvc.zip",
        "0d051779fbcb173b183efeae1c3e96148764fd82709bbbf0966df3efe48b67c5",
    ),
    (
        "powershell-7.6.3.zip",
        "https://github.com/PowerShell/PowerShell/releases/download/v7.6.3/PowerShell-7.6.3-win-x64.zip",
        "07ddb0d00b660459560ef82a9841da7705b27cd5dcca5a0d7b025a98eca29eca",
    ),
    (
        "python-3.12.10-embed-amd64.zip",
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
        "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3",
    ),
    (
        "pywinpty-3.0.5-cp312-cp312-win_amd64.whl",
        "https://files.pythonhosted.org/packages/45/34/942cc95ca4e26489875aa8a95192766247a687379ec29543eebe73ec945f/pywinpty-3.0.5-cp312-cp312-win_amd64.whl",
        "d62946adf14b15b54c0b8d785f93fe18b04da23f4ad59e2e8c4612646e9abd23",
    ),
)


class LabError(RuntimeError):
    """Fail-closed Windows Lab infrastructure error."""


@dataclass(frozen=True, slots=True)
class LabConfig:
    name: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    remote_root: PureWindowsPath
    local_root: Path
    known_hosts: Path

    @classmethod
    def from_env_file(cls, path: Path, *, repository_root: Path) -> LabConfig:
        return cls.from_values(load_env_file(path), repository_root=repository_root)

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, str],
        *,
        repository_root: Path,
    ) -> LabConfig:
        """Validate connection values from a secret file or one-time prompt."""

        required = {
            "TFBASH_WINDOWS_HOST",
            "TFBASH_WINDOWS_USER",
            "TFBASH_WINDOWS_PASSWORD",
        }
        missing = sorted(required - values.keys())
        if missing:
            raise LabError(f"missing required .env values: {', '.join(missing)}")
        name = values.get("TFBASH_WINDOWS_NAME", "windows-lab")
        if not _SAFE_NAME.fullmatch(name):
            raise LabError("TFBASH_WINDOWS_NAME must be a safe 1-64 character identifier")
        host = _normalize_host(values["TFBASH_WINDOWS_HOST"])
        user = values["TFBASH_WINDOWS_USER"]
        if not _SAFE_USER.fullmatch(user):
            raise LabError("Windows Lab user contains unsafe characters")
        try:
            port = int(values.get("TFBASH_WINDOWS_PORT", "22"))
        except ValueError as error:
            raise LabError("TFBASH_WINDOWS_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise LabError("TFBASH_WINDOWS_PORT must be between 1 and 65535")
        password = values["TFBASH_WINDOWS_PASSWORD"]
        if any(character in password for character in ("\x00", "\r", "\n")):
            raise LabError("SSH password cannot contain NUL, CR, or LF")
        remote_root = PureWindowsPath(
            values.get(
                "TFBASH_WINDOWS_REMOTE_ROOT",
                rf"C:\Users\{user}\tfbash-windows-lab",
            )
        )
        if not remote_root.is_absolute():
            raise LabError("TFBASH_WINDOWS_REMOTE_ROOT must be a drive-qualified or UNC path")
        local_root = repository_root / "artifacts" / "windows-lab" / name
        return cls(
            name=name,
            host=host,
            port=port,
            user=user,
            password=password,
            remote_root=remote_root,
            local_root=local_root,
            known_hosts=local_root / "known_hosts",
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    output: str


class RemoteTransport(Protocol):
    def run_powershell(self, script: str, *, timeout_seconds: int = 120) -> CommandResult: ...

    def upload(self, local_path: Path, remote_path: PureWindowsPath) -> None: ...

    def download(self, remote_path: PureWindowsPath, local_path: Path) -> None: ...


class OpenSshTransport:
    """System OpenSSH transport with password prompts handled outside argv/logs."""

    def __init__(self, config: LabConfig) -> None:
        self._config = config
        config.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        config.known_hosts.touch(mode=0o600, exist_ok=True)
        os.chmod(config.known_hosts, 0o600)

    def run_powershell(self, script: str, *, timeout_seconds: int = 120) -> CommandResult:
        connected = "TFBASH_SSH_CONNECTED_" + secrets.token_hex(16)
        wrapped = f"Write-Output {_ps_literal(connected)}; {script}"
        encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
        args = [*self._ssh_options(), "-p", str(self._config.port), self._ssh_destination()]
        args.extend(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ]
        )
        return self._authenticated_process(
            "ssh",
            args,
            connected_pattern=re.compile(re.escape(connected)),
            timeout_seconds=timeout_seconds,
        )

    def upload(self, local_path: Path, remote_path: PureWindowsPath) -> None:
        if not local_path.is_file():
            raise LabError(f"upload source is not a file: {local_path}")
        args = [*self._scp_options(), "-v", "-P", str(self._config.port)]
        args.extend([str(local_path), f"{self._scp_destination()}:{_scp_path(remote_path)}"])
        result = self._authenticated_process(
            "scp",
            args,
            connected_pattern=SCP_AUTHENTICATED,
            timeout_seconds=300,
        )
        if result.exit_code != 0:
            raise LabError(f"scp upload failed with exit code {result.exit_code}")

    def download(self, remote_path: PureWindowsPath, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        args = [*self._scp_options(), "-v", "-P", str(self._config.port)]
        args.extend([f"{self._scp_destination()}:{_scp_path(remote_path)}", str(local_path)])
        result = self._authenticated_process(
            "scp",
            args,
            connected_pattern=SCP_AUTHENTICATED,
            timeout_seconds=300,
        )
        if result.exit_code != 0:
            raise LabError(f"scp download failed with exit code {result.exit_code}")

    def remove_known_host(self) -> bool:
        host = self._known_host_target()
        if not self._known_host_exists(host):
            return False
        completed = subprocess.run(
            ["ssh-keygen", "-R", host, "-f", str(self._config.known_hosts)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise LabError("ssh-keygen could not update the dedicated known_hosts file")
        if self._known_host_exists(host):
            raise LabError("ssh-keygen reported success but the dedicated host key remains")
        return True

    def _authenticated_process(
        self,
        program: str,
        args: Sequence[str],
        *,
        connected_pattern: re.Pattern[str],
        timeout_seconds: int,
    ) -> CommandResult:
        child: Any = pexpect.spawn(
            program,
            list(args),
            encoding="utf-8",
            codec_errors="replace",
            timeout=timeout_seconds,
            echo=False,
        )
        output: list[str] = []
        password_sent = False
        authentication_patterns = [
            re.compile(r"(?i)(?:^|[\r\n])[^\r\n]*password:\s*$"),
            re.compile(r"(?i)permission denied"),
            re.compile(r"(?i)remote host identification has changed"),
            connected_pattern,
            pexpect.EOF,
            pexpect.TIMEOUT,
        ]
        while True:
            matched = child.expect(authentication_patterns)
            before = child.before or ""
            output.append(before)
            if matched == 0:
                if password_sent:
                    child.close(force=True)
                    raise LabError("SSH authentication requested the password more than once")
                password_sent = True
                child.sendline(self._config.password)
                continue
            if matched == 1:
                child.close(force=True)
                raise LabError("SSH authentication was denied")
            if matched == 2:
                child.close(force=True)
                raise LabError(
                    "SSH host key changed; inspect the host and run trust-host --replace"
                )
            if matched == 3:
                if not password_sent:
                    child.close(force=True)
                    raise LabError(
                        f"{program} connected without using the configured Windows Lab password"
                    )
                output.append(child.after or "")
                matched = child.expect([pexpect.EOF, pexpect.TIMEOUT])
                output.append(child.before or "")
                if matched == 1:
                    child.close(force=True)
                    raise LabError(f"{program} exceeded its {timeout_seconds}s deadline")
                child.close()
                exit_code = child.exitstatus
                if exit_code is None:
                    exit_code = 128 + int(child.signalstatus or 0)
                return CommandResult(exit_code=exit_code, output="".join(output))
            if matched == 5:
                child.close(force=True)
                raise LabError(f"{program} exceeded its {timeout_seconds}s deadline")
            child.close()
            raise LabError(
                f"{program} ended before SSH authentication completed: "
                f"{_remote_failure_details(''.join(output))}"
            )

    def _ssh_destination(self) -> str:
        return f"{self._config.user}@{self._config.host}"

    def _scp_destination(self) -> str:
        host = f"[{self._config.host}]" if ":" in self._config.host else self._config.host
        return f"{self._config.user}@{host}"

    def _known_host_target(self) -> str:
        if self._config.port == 22:
            return self._config.host
        return f"[{self._config.host}]:{self._config.port}"

    def _known_host_exists(self, host: str) -> bool:
        completed = subprocess.run(
            ["ssh-keygen", "-F", host, "-f", str(self._config.known_hosts)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise LabError("ssh-keygen could not inspect the dedicated known_hosts file")
        return completed.returncode == 0 and bool(completed.stdout.strip())

    def _ssh_options(self) -> list[str]:
        return [
            "-o",
            f"UserKnownHostsFile={self._config.known_hosts}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "PreferredAuthentications=keyboard-interactive,password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "PasswordAuthentication=yes",
            "-o",
            "KbdInteractiveAuthentication=yes",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
        ]

    def _scp_options(self) -> list[str]:
        return self._ssh_options()


@dataclass(frozen=True, slots=True)
class PackageInfo:
    path: Path
    sha256: str
    manifest: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RunInfo:
    run_id: str
    repetitions: int
    remote_zip: PureWindowsPath
    remote_sha256: str
    experiment_exit_code: int


@dataclass(frozen=True, slots=True)
class VerificationReport:
    run_id: str
    repetitions: int
    package_sha256: str
    observations: int
    gates: int
    contract_passed: bool
    experiment_exit_code: int


class WindowsLab:
    def __init__(
        self,
        *,
        repository_root: Path,
        config: LabConfig,
        transport: RemoteTransport,
    ) -> None:
        self.repository_root = repository_root
        self.config = config
        self.transport = transport

    def preflight(self) -> Mapping[str, object]:
        result = self.transport.run_powershell(_preflight_script())
        if result.exit_code != 0:
            raise LabError(f"Windows preflight failed with exit code {result.exit_code}")
        payload = parse_marked_json(result.output)
        windows = cast(Mapping[str, object], payload.get("windows"))
        if not isinstance(windows, Mapping):
            raise LabError("preflight did not return Windows metadata")
        if _required_int(windows, "ProductType") != 1:
            raise LabError("Windows Lab requires a Windows client OS (ProductType=1)")
        if _required_int(windows, "BuildNumber") < 22000:
            raise LabError("Windows Lab requires Windows 11 build 22000 or newer")
        if str(windows.get("OSArchitecture")) != "X64":
            raise LabError("Windows Lab requires an x64 operating system")
        if str(windows.get("ProcessArchitecture")) != "X64":
            raise LabError("Windows Lab requires an x64 OpenSSH PowerShell process")
        return payload

    def bootstrap(self) -> Mapping[str, object]:
        control_dir = self.config.remote_root / "control"
        remote_downloads = self.config.remote_root / "downloads"
        self._ensure_remote_directory(control_dir)
        self._ensure_remote_directory(remote_downloads)
        local_downloads = self.config.local_root / "downloads"
        for name, url, sha256 in TOOL_ARCHIVES:
            local_archive = _download_verified_archive(local_downloads / name, url, sha256)
            remote_archive = remote_downloads / name
            if not self._remote_file_matches(remote_archive, sha256):
                self.transport.upload(local_archive, remote_archive)
        local_script = _infrastructure_dir(self.repository_root) / "Bootstrap-WindowsLab.ps1"
        remote_script = control_dir / local_script.name
        self.transport.upload(local_script, remote_script)
        result = self.transport.run_powershell(
            f"& {_ps_literal(str(remote_script))} -RemoteRoot "
            f"{_ps_literal(str(self.config.remote_root))}"
        )
        if result.exit_code != 0:
            raise LabError(
                f"Windows bootstrap failed with exit code {result.exit_code}: "
                f"{_remote_failure_details(result.output)}"
            )
        return parse_marked_json(result.output)

    def deploy(self) -> PackageInfo:
        package = build_handoff_package(self.repository_root, self.config.local_root / "packages")
        with zipfile.ZipFile(package.path) as archive:
            manifest_sha256 = hashlib.sha256(archive.read("manifest.json")).hexdigest()
        remote_packages = self.config.remote_root / "packages"
        self._ensure_remote_directory(remote_packages)
        remote_zip = remote_packages / package.path.name
        self.transport.upload(package.path, remote_zip)
        remote_extract = remote_packages / package.sha256
        deploy_script = _infrastructure_dir(self.repository_root) / "Deploy-WindowsLabPackage.ps1"
        remote_deploy_script = self.config.remote_root / "control" / deploy_script.name
        self.transport.upload(deploy_script, remote_deploy_script)
        command = (
            f"& {_ps_literal(str(remote_deploy_script))} "
            f"-Archive {_ps_literal(str(remote_zip))} "
            f"-ExpectedPackageSha256 {_ps_literal(package.sha256)} "
            f"-Destination {_ps_literal(str(remote_extract))} "
            f"-ExpectedManifestSha256 {_ps_literal(manifest_sha256)} "
            f"-PackageSchema {_ps_literal(PACKAGE_SCHEMA)} "
            f"-RunnerCommit {_ps_literal(RUNNER_COMMIT)}"
        )
        result = self.transport.run_powershell(_marked_script(command))
        if result.exit_code != 0:
            raise LabError(
                f"Windows package deployment failed with exit code {result.exit_code}: "
                f"{_remote_failure_details(result.output)}"
            )
        deployed = parse_marked_json(result.output)
        if deployed.get("package_sha256") != package.sha256:
            raise LabError("remote deployment returned the wrong package hash")
        return package

    def run_ssh_smoke(
        self,
        package: PackageInfo,
        *,
        run_id: str | None = None,
        repetitions: int = DEFAULT_SSH_REPETITIONS,
    ) -> RunInfo:
        if not 1 <= repetitions <= 5:
            raise LabError("SSH smoke repetitions must be between 1 and 5")
        selected_run_id = run_id or new_run_id()
        if not _SAFE_NAME.fullmatch(selected_run_id):
            raise LabError("run_id must be a safe 1-64 character identifier")
        package_root = self.config.remote_root / "packages" / package.sha256
        run_root = self.config.remote_root / "runs" / selected_run_id
        uv_path = self.config.remote_root / "tools" / "uv-0.8.17" / "uv.exe"
        pwsh_path = self.config.remote_root / "tools" / "powershell-7.6.3" / "pwsh.exe"
        python_path = self.config.remote_root / "tools" / "python-3.12.10" / "python.exe"
        run_script = package_root / "RUN_SSH_SMOKE.ps1"
        command = (
            f"& {_ps_literal(str(pwsh_path))} -NoLogo -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -File {_ps_literal(str(run_script))} "
            f"-PackageRoot {_ps_literal(str(package_root))} "
            f"-RunDirectory {_ps_literal(str(run_root))} "
            f"-UvPath {_ps_literal(str(uv_path))} "
            f"-PowerShellPath {_ps_literal(str(pwsh_path))} "
            f"-PythonPath {_ps_literal(str(python_path))} "
            f"-RunnerCommit {_ps_literal(RUNNER_COMMIT)} "
            f"-PackageSha256 {_ps_literal(package.sha256)} "
            f"-TargetName {_ps_literal(self.config.name)} "
            f"-Repetitions {repetitions}"
        )
        result = self.transport.run_powershell(command, timeout_seconds=5_400)
        if result.exit_code != 0:
            raise LabError(
                f"SSH smoke infrastructure failed with exit code {result.exit_code}: "
                f"{_remote_failure_details(result.output)}"
            )
        payload = parse_marked_json(result.output)
        if payload.get("launch_channel") != "ssh" or payload.get("run_id") != selected_run_id:
            raise LabError("SSH smoke returned inconsistent run metadata")
        remote_sha256 = str(payload["result_sha256"])
        _validate_run_identity(selected_run_id, remote_sha256)
        observed_repetitions = _required_int(payload, "repetitions")
        if observed_repetitions != repetitions:
            raise LabError("SSH smoke returned a different repetition count")
        return RunInfo(
            run_id=selected_run_id,
            repetitions=observed_repetitions,
            remote_zip=PureWindowsPath(str(payload["result_zip"])),
            remote_sha256=remote_sha256,
            experiment_exit_code=_required_int(payload, "experiment_exit_code"),
        )

    def collect(self, run: RunInfo) -> Path:
        _validate_run_identity(run.run_id, run.remote_sha256)
        local_runs = (self.config.local_root / "runs").resolve()
        local_dir = (local_runs / run.run_id).resolve()
        try:
            local_dir.relative_to(local_runs)
        except ValueError as error:
            raise LabError("run_id escapes the local Windows Lab artifacts directory") from error
        local_zip = local_dir / "result.zip"
        self.transport.download(run.remote_zip, local_zip)
        actual = file_sha256(local_zip)
        if actual != run.remote_sha256:
            raise LabError(
                f"downloaded result checksum mismatch: expected {run.remote_sha256}, got {actual}"
            )
        return local_zip

    def run_all(
        self, *, repetitions: int = DEFAULT_SSH_REPETITIONS
    ) -> tuple[RunInfo, Path, VerificationReport]:
        self.preflight()
        self.bootstrap()
        package = self.deploy()
        run = self.run_ssh_smoke(package, repetitions=repetitions)
        result_zip = self.collect(run)
        report = verify_result_archive(result_zip)
        if (
            report.run_id != run.run_id
            or report.repetitions != run.repetitions
            or report.experiment_exit_code != run.experiment_exit_code
            or report.package_sha256 != package.sha256
        ):
            raise LabError("collected result metadata does not match the SSH run")
        return run, result_zip, report

    def _ensure_remote_directory(self, directory: PureWindowsPath) -> None:
        result = self.transport.run_powershell(
            "$ErrorActionPreference='Stop'; "
            f"New-Item -ItemType Directory -Force -Path {_ps_literal(str(directory))} "
            "| Out-Null"
        )
        if result.exit_code != 0:
            raise LabError(f"could not create remote directory: {directory}")

    def _remote_file_matches(self, path: PureWindowsPath, expected_sha256: str) -> bool:
        script = f"""
$path = {_ps_literal(str(path))}
$sha256 = if (Test-Path -LiteralPath $path -PathType Leaf) {{
    (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}} else {{ $null }}
[ordered]@{{sha256=$sha256}} | ConvertTo-Json -Compress
"""
        result = self.transport.run_powershell(_marked_script(script), timeout_seconds=300)
        if result.exit_code != 0:
            raise LabError(
                f"could not validate remote archive {path.name}: "
                f"{_remote_failure_details(result.output)}"
            )
        return parse_marked_json(result.output).get("sha256") == expected_sha256


def _normalize_host(value: str) -> str:
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError as error:
        if ":" in candidate or not _SAFE_DNS_HOST.fullmatch(candidate):
            raise LabError(
                "Windows Lab host must be a DNS name, IPv4 address, or IPv6 address"
            ) from error
        return candidate.lower()


def _validate_run_identity(run_id: str, remote_sha256: str) -> None:
    if not _SAFE_NAME.fullmatch(run_id):
        raise LabError("run_id must be a safe 1-64 character identifier")
    if not _SHA256.fullmatch(remote_sha256):
        raise LabError("remote result SHA-256 must contain exactly 64 hexadecimal characters")


def _remote_failure_details(output: str) -> str:
    normalized = " ".join(output.replace("\x00", "").split())
    if not normalized:
        return "remote process returned no diagnostic output"
    return normalized[-2000:]


def load_env_file(path: Path) -> dict[str, str]:
    """Parse the controlled .env subset, preserving quoted whitespace passwords."""

    if not path.is_file():
        raise LabError(f"environment file does not exist: {path}")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LabError(f"environment file must be owner-only; run: chmod 600 {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw_value = raw_line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY.fullmatch(key):
            raise LabError(f"invalid .env assignment on line {line_number}")
        value = _parse_env_value(raw_value, line_number=line_number)
        if key in values:
            raise LabError(f"duplicate .env key on line {line_number}: {key}")
        values[key] = value
    return values


def _parse_env_value(raw_value: str, *, line_number: int) -> str:
    value = raw_value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise LabError(f"invalid double-quoted .env value on line {line_number}") from error
        if not isinstance(decoded, str):
            raise LabError(f".env value must be a string on line {line_number}")
        return decoded
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise LabError(f"invalid single-quoted .env value on line {line_number}")
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def build_handoff_package(repository_root: Path, output_dir: Path) -> PackageInfo:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, bytes] = {}
    for name in RUNNER_FILES:
        completed = subprocess.run(
            ["git", "show", f"{RUNNER_COMMIT}:{name}"],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise LabError(f"could not read reviewed runner file {name} from {RUNNER_COMMIT}")
        payloads[name] = completed.stdout
    infrastructure = _infrastructure_dir(repository_root)
    for name in PACKAGE_CONTROL_FILES:
        payloads[name] = (infrastructure / name).read_bytes()
    payloads["SOURCE_COMMIT.txt"] = (RUNNER_COMMIT + "\n").encode("ascii")
    file_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
    manifest: dict[str, object] = {
        "schema": PACKAGE_SCHEMA,
        "runner_commit": RUNNER_COMMIT,
        "python": "3.12.10",
        "pywinpty": "3.0.5",
        "uv": "0.8.17",
        "powershell": "7.6.3",
        "files_sha256": file_hashes,
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary = output_dir / f".windows-phase0-lab-{RUNNER_COMMIT[:7]}.tmp.zip"
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name in sorted(payloads):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payloads[name])
        archive.comment = f"reviewed-runner:{RUNNER_COMMIT}".encode("ascii")
    digest = file_sha256(temporary)
    destination = output_dir / f"windows-phase0-lab-{RUNNER_COMMIT[:7]}-{digest[:12]}.zip"
    if destination.exists():
        if file_sha256(destination) != digest:
            raise LabError(f"existing package hash changed unexpectedly: {destination}")
        temporary.unlink()
    else:
        temporary.replace(destination)
    return PackageInfo(path=destination, sha256=digest, manifest=manifest)


def verify_result_archive(path: Path) -> VerificationReport:
    if not path.is_file():
        raise LabError(f"result archive does not exist: {path}")
    with tempfile.TemporaryDirectory(prefix="tfbash-windows-lab-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(path) as archive:
            _safe_extract(archive, root)
        metadata = _read_json_object(root / "control-metadata.json")
        exit_code = _read_json_object(root / "exit-code.json")
        evidence = root / "evidence"
        required = (
            evidence / "environment.json",
            evidence / "observations.jsonl",
            evidence / "summary.json",
            evidence / "summary.md",
            root / "terminal.log",
        )
        missing = [str(item.relative_to(root)) for item in required if not item.is_file()]
        if missing:
            raise LabError(f"result archive is missing required files: {', '.join(missing)}")
        if metadata.get("schema") != RESULT_SCHEMA or metadata.get("launch_channel") != "ssh":
            raise LabError("result archive is not an SSH Windows Lab result")
        if metadata.get("runner_commit") != RUNNER_COMMIT:
            raise LabError("result metadata contains an unexpected runner commit")
        if metadata.get("evidence_tier") != "hosted-smoke":
            raise LabError("result metadata must use the hosted-smoke evidence tier")
        if metadata.get("evidence_complete") is not True or metadata.get("missing_evidence") != []:
            raise LabError("result metadata reports incomplete evidence")
        package_sha256 = str(metadata.get("package_sha256", ""))
        if not _SHA256.fullmatch(package_sha256):
            raise LabError("result metadata contains an invalid package SHA-256")
        repetitions = _required_int(metadata, "repetitions")
        if not 1 <= repetitions <= 5:
            raise LabError("result metadata contains an invalid SSH repetition count")
        summary = _read_json_object(evidence / "summary.json")
        environment = _read_json_object(evidence / "environment.json")
        if summary.get("environment_tier") != "hosted-smoke":
            raise LabError("SSH results must use the hosted-smoke evidence tier")
        if summary.get("decision_ready") is not False or summary.get("decision") != "inconclusive":
            raise LabError("SSH evidence must remain inconclusive and never decision-ready")
        hashes = summary.get("evidence_files_sha256")
        if not isinstance(hashes, Mapping):
            raise LabError("summary is missing evidence file hashes")
        for name in ("environment.json", "observations.jsonl"):
            if hashes.get(name) != file_sha256(evidence / name):
                raise LabError(f"evidence hash mismatch for {name}")
        lines = (evidence / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        expected_observations = _expected_ssh_observations(repetitions)
        if len(lines) != expected_observations:
            raise LabError(f"expected {expected_observations} observations, found {len(lines)}")
        observations: list[Observation] = []
        for line_number, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise LabError(f"invalid observation JSON on line {line_number}") from error
            if not isinstance(value, dict):
                raise LabError(f"observation {line_number} is not an object")
            observations.append(_parse_observation(value, line_number=line_number))
        gates = summary.get("gates")
        if not isinstance(gates, list) or len(gates) != EXPECTED_GATES:
            raise LabError(f"summary must contain exactly {EXPECTED_GATES} gates")
        _validate_ssh_observation_matrix(observations, repetitions)
        evaluated = evaluate_gates(observations, EnvironmentTier.HOSTED_SMOKE)
        _validate_evaluated_summary(summary, evaluated)
        if summary.get("environment") != environment:
            raise LabError("summary environment does not match environment.json")
        if environment.get("runner_commit") != RUNNER_COMMIT:
            raise LabError("result was produced by an unexpected runner commit")
        run_id = str(metadata.get("run_id", ""))
        if not _SAFE_NAME.fullmatch(run_id):
            raise LabError("result archive contains an invalid run_id")
        experiment_exit_code = _required_int(exit_code, "experiment_exit_code")
        if experiment_exit_code not in {0, 1}:
            raise LabError("experiment exit code must be 0 or 1")
        expected_exit_code = 0 if evaluated.contract_passed else 1
        if experiment_exit_code != expected_exit_code:
            raise LabError("experiment exit code contradicts the recomputed contract result")
        return VerificationReport(
            run_id=run_id,
            repetitions=repetitions,
            package_sha256=package_sha256,
            observations=len(lines),
            gates=len(gates),
            contract_passed=bool(summary.get("contract_passed")),
            experiment_exit_code=experiment_exit_code,
        )


def parse_marked_json(output: str) -> dict[str, object]:
    start = output.rfind(JSON_BEGIN)
    end = output.rfind(JSON_END)
    if start < 0 or end <= start:
        raise LabError("remote output did not contain a complete JSON envelope")
    raw = output[start + len(JSON_BEGIN) : end].strip()
    if raw.startswith("#< CLIXML"):
        raw = raw.removeprefix("#< CLIXML").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        payload = _json_line_from_clixml(raw)
        if payload is None:
            raise LabError(
                "remote JSON envelope was invalid: " + _remote_failure_details(output)
            ) from error
    if not isinstance(payload, dict):
        raise LabError("remote JSON envelope must contain an object")
    return cast(dict[str, object], payload)


def _json_line_from_clixml(raw: str) -> object | None:
    if "<Objs " not in raw and "#< CLIXML" not in raw:
        return None
    candidates: list[object] = []
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            candidates.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return candidates[0] if len(candidates) == 1 else None


def new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{secrets.token_hex(4)}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified_archive(path: Path, url: str, expected_sha256: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and file_sha256(path) == expected_sha256:
        return path
    temporary = path.with_name(f".{path.name}.download-{secrets.token_hex(8)}")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        actual = file_sha256(temporary)
        if actual != expected_sha256:
            raise LabError(
                f"controller download checksum mismatch for {path.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
        temporary.replace(path)
    except (OSError, urllib.error.URLError) as error:
        raise LabError(f"controller could not download pinned tool archive: {path.name}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return path


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _preflight_script() -> str:
    body = r"""
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
$osArchitecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
$processArchitecture = [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
[ordered]@{
    windows = [ordered]@{
        Caption = $os.Caption
        Version = $os.Version
        BuildNumber = [int]$os.BuildNumber
        ProductType = [int]$os.ProductType
        OSArchitecture = $osArchitecture
        ProcessArchitecture = $processArchitecture
    }
    ssh = [ordered]@{
        User = [Environment]::UserName
        SessionName = $env:SESSIONNAME
        Client = $env:SSH_CLIENT
    }
    existing = [ordered]@{
        PowerShell = $PSVersionTable.PSVersion.ToString()
        Pwsh = if (Get-Command pwsh -ErrorAction SilentlyContinue) {
            (Get-Command pwsh).Source
        } else { $null }
        Uv = if (Get-Command uv -ErrorAction SilentlyContinue) {
            (Get-Command uv).Source
        } else { $null }
    }
} | ConvertTo-Json -Depth 5 -Compress
"""
    return _marked_script(body)


def _marked_script(body: str) -> str:
    return (
        f"$ErrorActionPreference='Stop'; Write-Output '{JSON_BEGIN}'; "
        f"{body}; Write-Output '{JSON_END}'"
    )


def _ps_literal(value: str) -> str:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise LabError("PowerShell literal contains a forbidden control character")
    return "'" + value.replace("'", "''") + "'"


def _scp_path(path: PureWindowsPath) -> str:
    value = path.as_posix()
    if any(character in value for character in ("\x00", "\r", "\n", "'", '"')):
        raise LabError("remote transfer path contains unsafe characters")
    return value


def _infrastructure_dir(root: Path) -> Path:
    return root / "experiments" / "windows_phase0" / "infrastructure"


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LabError(f"could not read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise LabError(f"JSON payload is not an object: {path}")
    return cast(dict[str, object], value)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LabError(f"JSON field must be an integer: {key}")
    return value


def _parse_observation(payload: Mapping[str, object], *, line_number: int) -> Observation:
    scenario = payload.get("scenario")
    details = payload.get("details")
    if not isinstance(scenario, str) or not scenario:
        raise LabError(f"observation {line_number} has an invalid scenario")
    if not isinstance(details, dict):
        raise LabError(f"observation {line_number} has invalid details")
    try:
        outcome = Outcome(payload.get("outcome"))
    except (TypeError, ValueError) as error:
        raise LabError(f"observation {line_number} has an invalid outcome") from error
    iteration = _required_int(payload, "iteration")
    duration_ms = _required_int(payload, "duration_ms")
    if iteration < 1 or duration_ms < 0:
        raise LabError(f"observation {line_number} has invalid numeric fields")
    return Observation(
        scenario=scenario,
        iteration=iteration,
        outcome=outcome,
        duration_ms=duration_ms,
        details=cast(dict[str, object], details),
    )


def _validate_evaluated_summary(actual: Mapping[str, object], evaluated: DecisionSummary) -> None:
    expected = summary_payload(evaluated)
    for key in (
        "environment_tier",
        "evidence_complete",
        "contract_passed",
        "decision_ready",
        "decision",
        "gates",
    ):
        if actual.get(key) != expected[key]:
            raise LabError(f"summary does not match raw observations: {key}")


def _expected_ssh_observations(repetitions: int) -> int:
    return sum(
        rule.required_runs if rule.required_runs == 1 else repetitions for rule in GATE_RULES
    )


def _validate_ssh_observation_matrix(observations: Sequence[Observation], repetitions: int) -> None:
    for rule in GATE_RULES:
        expected_runs = rule.required_runs if rule.required_runs == 1 else repetitions
        values = [value for value in observations if value.scenario == rule.scenario]
        iterations = {value.iteration for value in values}
        if len(values) != expected_runs or iterations != set(range(1, expected_runs + 1)):
            raise LabError(f"SSH observation matrix is incomplete or duplicated: {rule.scenario}")


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    for member in archive.infolist():
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise LabError(f"unsafe path in result archive: {member.filename}")
    archive.extractall(destination)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "bootstrap", "deploy"):
        subparsers.add_parser(command)
    for command in ("run", "all"):
        smoke = subparsers.add_parser(command)
        smoke.add_argument(
            "--repetitions",
            type=int,
            default=DEFAULT_SSH_REPETITIONS,
            choices=range(1, 6),
        )
    collect = subparsers.add_parser("collect")
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--remote-sha256", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("archive", type=Path)
    trust = subparsers.add_parser("trust-host")
    trust.add_argument("--replace", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = repository_root()
    if args.command == "verify":
        report = verify_result_archive(args.archive)
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
        return 0
    config = LabConfig.from_env_file(args.env_file, repository_root=root)
    transport = OpenSshTransport(config)
    if args.command == "trust-host":
        removed = transport.remove_known_host()
        if removed:
            print("Removed the old dedicated host key. The next connection uses accept-new.")
        else:
            print("No dedicated host key existed for this target; nothing was removed.")
        return 0
    lab = WindowsLab(repository_root=root, config=config, transport=transport)
    if args.command == "preflight":
        print(json.dumps(lab.preflight(), indent=2, sort_keys=True))
    elif args.command == "bootstrap":
        print(json.dumps(lab.bootstrap(), indent=2, sort_keys=True))
    elif args.command == "deploy":
        package = lab.deploy()
        print(json.dumps({"package": str(package.path), "sha256": package.sha256}, indent=2))
    elif args.command == "run":
        package = lab.deploy()
        run = lab.run_ssh_smoke(package, repetitions=args.repetitions)
        print(json.dumps(asdict(run), default=str, indent=2, sort_keys=True))
    elif args.command == "collect":
        remote_zip = config.remote_root / "runs" / f"{args.run_id}.zip"
        run = RunInfo(args.run_id, DEFAULT_SSH_REPETITIONS, remote_zip, args.remote_sha256, -1)
        print(lab.collect(run))
    elif args.command == "all":
        run, path, report = lab.run_all(repetitions=args.repetitions)
        print(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "repetitions": run.repetitions,
                    "result": str(path),
                    "experiment_exit_code": report.experiment_exit_code,
                    "contract_passed": report.contract_passed,
                    "decision": "inconclusive",
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        raise AssertionError(f"unhandled command: {args.command}")
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except LabError as error:
        print(f"Windows Lab error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
