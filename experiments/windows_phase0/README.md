# Windows V1 Phase 0 experiment

This directory contains the disposable experiment runner for issue #12. It is
not production runtime code. The experiment compares two ownership strategies
on the same native ConPTY transport:

- Candidate A: Toolhelp process identities plus `taskkill /T`.
- Candidate B: a non-breakaway Job Object with kill-on-close.

Both candidates use the low-level `winpty.PTY` API from pywinpty 3.0.5. The
runner uses blocking reads, Windows events, condition variables, and explicit
deadlines. It does not use a fixed polling loop.

## Evidence tiers

`hosted-smoke` runs on GitHub-hosted Windows and exercises the full matrix, but
cannot approve the architecture because the hosted image is Windows Server.
`native-gate` requires Windows 11 x64, PowerShell 7.6.x, 20 successful runs for
every repeated gate, no surviving managed descendants, and a unique terminal
state with no late output.

## Run the native gate

Install Python 3.12, [uv](https://docs.astral.sh/uv/), and PowerShell 7.6.x on a
Windows 11 x64 machine. From the repository root, run:

```powershell
./experiments/windows_phase0/run_native.ps1
```

The script is equivalent to:

```powershell
uv run --python 3.12 --with pywinpty==3.0.5 -- `
  python -m experiments.windows_phase0.runner `
  --environment-tier native-gate `
  --repetitions 20 `
  --output-dir artifacts/windows-phase0-native
```

If `pwsh.exe` is not on `PATH`, add `--pwsh C:\path\to\pwsh.exe`. The command
returns zero only when the native gate is complete and every acceptance rule
passes. It can take several minutes because the lifecycle matrix contains 260
repeated observations across the two ownership candidates and runtime gates.

Return the complete `artifacts/windows-phase0-native` directory as a zip. It
contains:

- `environment.json`: OS, architecture, Python, PowerShell, and pywinpty
  versions; it does not capture environment-variable values.
- `observations.jsonl`: append-only raw observations, flushed after every case.
- `summary.json`: machine-readable gate evaluation.
- `summary.md`: compact human-readable result table.

Do not edit a failed artifact. Return it as-is so failures and tracebacks remain
reproducible evidence. The runner attempts to release every fixture event and
terminate every managed process tree even when a scenario fails.

## Local contract tests

The result-contract tests are platform independent:

```shell
uv run --with pytest pytest experiments/windows_phase0/test_contracts.py
```
