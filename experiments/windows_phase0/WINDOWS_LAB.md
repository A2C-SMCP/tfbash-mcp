# SSH-first Windows Lab

This control plane makes the reviewed Windows Phase 0 runner repeatable across
replaceable Windows 11 hosts. The Mac stores connection details in a local
`.env`; no password is written to argv, logs, the remote host, Git, or evidence.

## Configure

Copy `.env.example` to `.env`. Double-quote whitespace-only passwords:

```dotenv
TFBASH_WINDOWS_NAME=windows-lab-01
TFBASH_WINDOWS_HOST=192.168.50.215
TFBASH_WINDOWS_PORT=23
TFBASH_WINDOWS_USER=llg
TFBASH_WINDOWS_PASSWORD=" "
TFBASH_WINDOWS_REMOTE_ROOT=C:\Users\llg\tfbash-windows-lab
```

`.env` is ignored by Git. This profile intentionally uses password-only SSH;
key/passphrase authentication needs a separate credential model and is not
silently mixed with the login password. Replace weak passwords after initial
provisioning. On macOS/Linux the controller requires `chmod 600 .env`.
It uses a dedicated `known_hosts` file and
OpenSSH `accept-new`; a changed key fails closed. After independently checking
a replaced host, remove only that lab's stored key with:

```shell
uv run --group windows-lab python scripts/windows_lab.py --env-file .env trust-host --replace
```

## Run

The one-command path performs preflight, user-scoped portable tool bootstrap,
deterministic package deployment, a three-repetition/47-observation SSH smoke,
artifact collection, and local verification:

```shell
uv run --group windows-lab python scripts/windows_lab.py --env-file .env all
```

Repetitions are a per-run option, not connection state. Choose 1–5 without
editing `.env`, for example `all --repetitions 1`. The default is 3. The formal
native gate remains fixed at 20 repetitions.

Individual stages are also available:

```shell
uv run --group windows-lab python scripts/windows_lab.py --env-file .env preflight
uv run --group windows-lab python scripts/windows_lab.py --env-file .env bootstrap
uv run --group windows-lab python scripts/windows_lab.py --env-file .env deploy
uv run --group windows-lab python scripts/windows_lab.py --env-file .env run --repetitions 3
uv run --group windows-lab python scripts/windows_lab.py verify artifacts/windows-lab/TARGET/runs/RUN/result.zip
```

For a one-off target, no `.env` is required. Connection settings may be passed
as non-secret flags and the password is read from a hidden prompt; it is never
placed in argv or shell history:

```shell
uv run --group windows-lab python scripts/windows_supervisor_lab.py \
  --host 192.168.50.215 --port 23 --user llg --repetitions 1
```

The remote layout is isolated under `TFBASH_WINDOWS_REMOTE_ROOT`. The pinned
toolchain includes portable PowerShell 7.6.3, uv 0.8.17, CPython 3.12.10 x64,
and the pywinpty 3.0.5 CPython 3.12 x64 wheel. The Mac downloads and verifies
all four artifacts, then uploads them through SSH so the Windows target needs
no internet access. Windows rehashes every upload, constructs the Python
environment offline, and compares installed trees file-for-file with freshly
expanded trusted archives on every bootstrap. Package reuse also
requires an exact locally trusted manifest hash and file set;
no machine-wide PATH, execution policy, package manager, service, or registry
setting is changed.

Remote PowerShell is launched with process-scoped `-ExecutionPolicy Bypass` so
machines that block `.ps1` by policy need no interactive setup; persistent
machine and user execution-policy settings remain untouched.
The controller uses Windows PowerShell only as the SSH bootstrap shell; the
matrix itself runs under the pinned portable PowerShell 7.

## Evidence boundary

An SSH launch always invokes the reviewed runner with `hosted-smoke`. Even on a
real Windows 11 x64 host, the result remains `decision_ready=false` and
`decision=inconclusive`. The local verifier rejects an SSH archive that claims
otherwise. This full matrix is useful infrastructure and repeatability
evidence, but it does not silently replace Issue #12's interactive desktop
native gate.

The deterministic handoff still includes `RUN_WINDOWS11.ps1` for that final
gate. It is the only script allowed to request `native-gate` evidence.

## Issue #15 supervisor candidate

The #15 verifier has its own package and evidence schema. It packages the full
`src/tfbash_mcp` tree and the probe from an exact Git commit, deploys it through
the same offline SSH toolchain, and runs the production
`WindowsProcessSupervisor`, `ConPtyTransport`, and `PowerShellDialect` together.
It does not enable the fail-closed production builder.

Run a replaceable-host smoke from the Mac with either connection mode:

```shell
uv run --group windows-lab python scripts/windows_supervisor_lab.py \
  --env-file .env --source-ref feature/issue-15-windows-native-gate \
  --repetitions 3
```

The controller performs preflight, pinned/offline bootstrap, deterministic
deployment, SSH execution, collection, and local evidence recomputation. Use
`--skip-bootstrap` only after the same remote root has already passed bootstrap.
The 1–5 repetition SSH result may prove that the candidate works on that host,
but it remains `decision=inconclusive` by contract.

After a smoke passes, the Mac can run #15's fixed formal gate without an
engineer operating the Windows desktop:

```shell
uv run --group windows-lab python scripts/windows_supervisor_lab.py \
  --env-file .env --source-ref feature/issue-15-windows-native-gate \
  --skip-bootstrap --native-gate
```

`--native-gate` ignores the smoke repetition option and always runs exactly 20
fresh sessions. This is valid native evidence: the probe creates and inspects
real Job Objects, processes, named events, and ConPTY instances on the target;
SSH is only the outer control channel. The local verifier still recomputes the
full evidence matrix before accepting `decision_ready=true`.

Each repetition independently checks:

- the trusted bootstrap and real PowerShell are members of the pre-created Job;
- a rapidly created grandchild cannot escape the Job;
- execution cleanup removes descendants while preserving the persistent Shell;
- ConPTY Ctrl-C is delivered and the same Shell accepts the next command;
- quick command tail output and exit code 37 both survive framing/finalization;
- Shell close and Job cleanup leave no tracked process alive.

The extracted package also contains
`experiments/windows_supervisor_native/RUN_WINDOWS11_SUPERVISOR.ps1`. The formal
native gate is hard-coded to exactly 20 repetitions and is the only path that
can produce `decision_ready=true`; the Mac `--native-gate` path enforces the
same contract. An engineer is needed only if the target's policy blocks
SSH/user-scoped execution or for #12's separate interactive-desktop impact
check. #15 installation, formal execution, collection, and verification stay
Mac-controlled.
