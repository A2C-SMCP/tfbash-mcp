"""Mac-side SSH deployment and verification for the #15 native supervisor probe."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

from experiments.windows_phase0.lab import (
    FIXED_ZIP_TIME,
    JSON_BEGIN,
    JSON_END,
    LabConfig,
    LabError,
    OpenSshTransport,
    PackageInfo,
    RemoteTransport,
    file_sha256,
    new_run_id,
    parse_marked_json,
    repository_root,
)
from experiments.windows_supervisor_native.contracts import (
    RESULT_SCHEMA,
    SCHEMA,
    EvidenceDecision,
    evaluate_evidence,
)

PACKAGE_SCHEMA = "tfbash-windows-supervisor-package/v1"
PACKAGE_PATHS = ("src/tfbash_mcp", "experiments/windows_supervisor_native")
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class SupervisorRunInfo:
    run_id: str
    repetitions: int
    remote_zip: PureWindowsPath
    remote_sha256: str
    experiment_exit_code: int


@dataclass(frozen=True, slots=True)
class SupervisorVerification:
    run_id: str
    source_commit: str
    package_sha256: str
    experiment_exit_code: int
    decision: EvidenceDecision


def resolve_source_commit(repository: Path, source_ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{source_ref}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip().lower()
    if (
        completed.returncode != 0
        or len(commit) != 40
        or any(c not in "0123456789abcdef" for c in commit)
    ):
        raise LabError(f"could not resolve an exact source commit from {source_ref!r}")
    return commit


def build_supervisor_package(repository: Path, output_dir: Path, source_ref: str) -> PackageInfo:
    commit = resolve_source_commit(repository, source_ref)
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", commit, "--", *PACKAGE_PATHS],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise LabError("could not list the supervisor package source tree")
    names = tuple(line for line in listing.stdout.splitlines() if line)
    required = {
        "src/tfbash_mcp/runtime/windows_process.py",
        "src/tfbash_mcp/runtime/windows_conpty.py",
        "experiments/windows_supervisor_native/probe.py",
        "experiments/windows_supervisor_native/RUN_SUPERVISOR_SSH.ps1",
        "experiments/windows_supervisor_native/RUN_WINDOWS11_SUPERVISOR.ps1",
    }
    if not required.issubset(names):
        raise LabError("source commit does not contain the complete supervisor probe")
    payloads: dict[str, bytes] = {}
    for name in names:
        shown = subprocess.run(
            ["git", "show", f"{commit}:{name}"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            raise LabError(f"could not read package file from source commit: {name}")
        payloads[name] = shown.stdout
    payloads["SOURCE_COMMIT.txt"] = (commit + "\n").encode("ascii")
    manifest: dict[str, object] = {
        "schema": PACKAGE_SCHEMA,
        "runner_commit": commit,
        "source_commit": commit,
        "python": "3.12.10",
        "pywinpty": "3.0.5",
        "powershell": "7.6.3",
        "files_sha256": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
        },
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / f".windows-supervisor-{commit[:7]}.tmp.zip"
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(payloads):
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payloads[name])
        archive.comment = f"supervisor-source:{commit}".encode("ascii")
    digest = file_sha256(temporary)
    destination = output_dir / f"windows-supervisor-{commit[:7]}-{digest[:12]}.zip"
    if destination.exists():
        if file_sha256(destination) != digest:
            raise LabError("existing supervisor package has an unexpected checksum")
        temporary.unlink()
    else:
        temporary.replace(destination)
    return PackageInfo(destination, digest, manifest)


class WindowsSupervisorLab:
    def __init__(
        self,
        *,
        repository: Path,
        config: LabConfig,
        transport: RemoteTransport,
    ) -> None:
        self.repository = repository
        self.config = config
        self.transport = transport

    def deploy(self, source_ref: str) -> PackageInfo:
        package = build_supervisor_package(
            self.repository,
            self.config.local_root / "supervisor-packages",
            source_ref,
        )
        source_commit = str(package.manifest["source_commit"])
        remote_packages = self.config.remote_root / "packages"
        self._ensure_remote_directory(remote_packages)
        remote_zip = remote_packages / package.path.name
        self.transport.upload(package.path, remote_zip)
        deploy_script = (
            self.repository
            / "experiments/windows_phase0/infrastructure/Deploy-WindowsLabPackage.ps1"
        )
        remote_control = self.config.remote_root / "control"
        self._ensure_remote_directory(remote_control)
        remote_script = remote_control / deploy_script.name
        self.transport.upload(deploy_script, remote_script)
        with zipfile.ZipFile(package.path) as archive:
            manifest_hash = hashlib.sha256(archive.read("manifest.json")).hexdigest()
        destination = remote_packages / package.sha256
        command = (
            f"& {_ps_literal(str(remote_script))} "
            f"-Archive {_ps_literal(str(remote_zip))} "
            f"-ExpectedPackageSha256 {_ps_literal(package.sha256)} "
            f"-Destination {_ps_literal(str(destination))} "
            f"-ExpectedManifestSha256 {_ps_literal(manifest_hash)} "
            f"-PackageSchema {_ps_literal(PACKAGE_SCHEMA)} "
            f"-RunnerCommit {_ps_literal(source_commit)}"
        )
        result = self.transport.run_powershell(_marked_script(command), timeout_seconds=300)
        if result.exit_code != 0:
            raise LabError(f"supervisor package deployment failed: {result.exit_code}")
        if parse_marked_json(result.output).get("package_sha256") != package.sha256:
            raise LabError("remote supervisor package identity mismatch")
        return package

    def run_remote(
        self,
        package: PackageInfo,
        *,
        evidence_tier: str,
        repetitions: int,
    ) -> SupervisorRunInfo:
        if evidence_tier == "hosted-smoke" and not 1 <= repetitions <= 5:
            raise LabError("supervisor SSH smoke repetitions must be between 1 and 5")
        if evidence_tier == "native-gate" and repetitions != 20:
            raise LabError("the supervisor native gate requires exactly 20 repetitions")
        if evidence_tier not in {"hosted-smoke", "native-gate"}:
            raise LabError("unknown supervisor evidence tier")
        prefix = "supervisor-gate-" if evidence_tier == "native-gate" else "supervisor-"
        run_id = new_run_id().replace("run-", prefix)
        package_root = self.config.remote_root / "packages" / package.sha256
        run_root = self.config.remote_root / "runs" / run_id
        pwsh = self.config.remote_root / "tools/powershell-7.6.3/pwsh.exe"
        python = self.config.remote_root / "tools/python-3.12.10/python.exe"
        run_script = package_root / "experiments/windows_supervisor_native/RUN_SUPERVISOR_SSH.ps1"
        source_commit = str(package.manifest["source_commit"])
        command = (
            f"& {_ps_literal(str(pwsh))} -NoLogo -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -File {_ps_literal(str(run_script))} "
            f"-PackageRoot {_ps_literal(str(package_root))} "
            f"-RunDirectory {_ps_literal(str(run_root))} "
            f"-PowerShellPath {_ps_literal(str(pwsh))} "
            f"-PythonPath {_ps_literal(str(python))} "
            f"-SourceCommit {_ps_literal(source_commit)} "
            f"-PackageSha256 {_ps_literal(package.sha256)} "
            f"-TargetName {_ps_literal(self.config.name)} "
            f"-EvidenceTier {_ps_literal(evidence_tier)} -Repetitions {repetitions}"
        )
        result = self.transport.run_powershell(command, timeout_seconds=5_400)
        if result.exit_code != 0:
            raise LabError(f"supervisor remote run infrastructure failed: {result.exit_code}")
        payload = parse_marked_json(result.output)
        if payload.get("run_id") != run_id or payload.get("launch_channel") != "ssh":
            raise LabError("supervisor remote run returned inconsistent metadata")
        digest = str(payload.get("result_sha256", ""))
        if len(digest) != _SHA256_LENGTH:
            raise LabError("supervisor remote run returned an invalid result checksum")
        return SupervisorRunInfo(
            run_id=run_id,
            repetitions=repetitions,
            remote_zip=PureWindowsPath(str(payload["result_zip"])),
            remote_sha256=digest,
            experiment_exit_code=_required_int(payload, "experiment_exit_code"),
        )

    def collect(self, run: SupervisorRunInfo) -> Path:
        destination = self.config.local_root / "supervisor-runs" / run.run_id / "result.zip"
        self.transport.download(run.remote_zip, destination)
        if file_sha256(destination) != run.remote_sha256:
            raise LabError("downloaded supervisor result checksum mismatch")
        return destination

    def _ensure_remote_directory(self, path: PureWindowsPath) -> None:
        result = self.transport.run_powershell(
            "$ErrorActionPreference='Stop';New-Item -ItemType Directory -Force -Path "
            f"{_ps_literal(str(path))}|Out-Null"
        )
        if result.exit_code != 0:
            raise LabError(f"could not create remote directory: {path}")


def verify_supervisor_result(path: Path) -> SupervisorVerification:
    with tempfile.TemporaryDirectory(prefix="tfbash-supervisor-result-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise LabError("unsafe path in supervisor result archive")
            archive.extractall(root)
        metadata = _read_object(root / "control-metadata.json")
        exit_code = _read_object(root / "exit-code.json")
        evidence = _read_object(root / "supervisor-evidence.json")
        if metadata.get("schema") != RESULT_SCHEMA:
            raise LabError("unexpected supervisor result schema")
        if metadata.get("launch_channel") != "ssh":
            raise LabError("supervisor result did not originate from the SSH controller")
        tier = metadata.get("evidence_tier")
        if tier not in {"hosted-smoke", "native-gate"}:
            raise LabError("remote supervisor evidence has an unknown tier")
        if metadata.get("evidence_complete") is not True:
            raise LabError("remote supervisor evidence is incomplete")
        if evidence.get("schema") != SCHEMA or evidence.get("evidence_tier") != tier:
            raise LabError("supervisor evidence identity mismatch")
        source_commit = str(metadata.get("source_commit", ""))
        environment = evidence.get("environment")
        if (
            not isinstance(environment, Mapping)
            or environment.get("source_commit") != source_commit
        ):
            raise LabError("supervisor evidence source commit mismatch")
        decision = evaluate_evidence(evidence)
        experiment_exit_code = _required_int(exit_code, "experiment_exit_code")
        expected_exit = 0 if decision.contract_passed else 1
        if experiment_exit_code != expected_exit:
            raise LabError("supervisor experiment exit code contradicts its evidence")
        return SupervisorVerification(
            run_id=str(metadata["run_id"]),
            source_commit=source_commit,
            package_sha256=str(metadata["package_sha256"]),
            experiment_exit_code=experiment_exit_code,
            decision=decision,
        )


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise LabError(f"could not read supervisor result file: {path.name}") from error
    if not isinstance(value, dict):
        raise LabError(f"supervisor result file is not an object: {path.name}")
    return cast(dict[str, object], value)


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LabError(f"field must be an integer: {key}")
    return value


def _ps_literal(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise LabError("unsafe PowerShell literal")
    return "'" + value.replace("'", "''") + "'"


def _marked_script(body: str) -> str:
    return (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
        f"Write-Output '{JSON_BEGIN}';"
        f"{body};Write-Output '{JSON_END}'"
    )


def _config(args: argparse.Namespace, root: Path) -> LabConfig:
    if args.env_file is not None:
        return LabConfig.from_env_file(args.env_file, repository_root=root)
    missing = [name for name in ("host", "user") if getattr(args, name) is None]
    if missing:
        raise LabError("one-time mode requires --host and --user")
    password = getpass.getpass("Windows SSH password: ")
    values = {
        "TFBASH_WINDOWS_NAME": args.name or "windows-lab",
        "TFBASH_WINDOWS_HOST": args.host,
        "TFBASH_WINDOWS_PORT": str(args.port),
        "TFBASH_WINDOWS_USER": args.user,
        "TFBASH_WINDOWS_PASSWORD": password,
    }
    if args.remote_root is not None:
        values["TFBASH_WINDOWS_REMOTE_ROOT"] = args.remote_root
    return LabConfig.from_values(
        values,
        repository_root=root,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_argument_group("connection")
    connection.add_argument("--env-file", type=Path)
    connection.add_argument("--host")
    connection.add_argument("--port", type=int, default=22)
    connection.add_argument("--user")
    connection.add_argument("--name")
    connection.add_argument("--remote-root")
    parser.add_argument("--source-ref", default="HEAD")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=3)
    parser.add_argument(
        "--native-gate",
        action="store_true",
        help="run the fixed 20-repetition decision gate through SSH",
    )
    parser.add_argument("--skip-bootstrap", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = repository_root()
    config = _config(args, root)
    transport = OpenSshTransport(config)
    phase0 = __import__("experiments.windows_phase0.lab", fromlist=["WindowsLab"]).WindowsLab(
        repository_root=root,
        config=config,
        transport=transport,
    )
    phase0.preflight()
    if not args.skip_bootstrap:
        phase0.bootstrap()
    lab = WindowsSupervisorLab(repository=root, config=config, transport=transport)
    package = lab.deploy(args.source_ref)
    evidence_tier = "native-gate" if args.native_gate else "hosted-smoke"
    repetitions = 20 if args.native_gate else args.repetitions
    run = lab.run_remote(
        package,
        evidence_tier=evidence_tier,
        repetitions=repetitions,
    )
    archive = lab.collect(run)
    report = verify_supervisor_result(archive)
    print(json.dumps({"archive": str(archive), **asdict(report)}, default=str, indent=2))
    return 0


def cli(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except LabError as error:
        print(f"Windows supervisor lab error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
