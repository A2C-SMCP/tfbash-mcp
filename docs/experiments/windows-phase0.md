# Windows V1 Phase 0 experiment report

Status: hosted smoke complete; native Windows 11 gate pending.

## Decision

The current transport candidate, pywinpty 3.0.5 over native ConPTY, is a
**hosted-smoke No-Go for the V1 contract**. It loses the rapid-exit output tail,
does not recover interrupt within the three-second contract (including the
allowed rebuild path), and cannot preserve control responsiveness under
backpressure. These failures block freezing the Windows transport contract.

For process ownership alone, **Candidate B (kill-on-close Job Object) is the
provisional choice**. Both candidates reclaimed the managed three-level tree in
all 80 lifecycle observations, but the Job Object cases completed in roughly
4.4–5.3 seconds versus 9.5–19.9 seconds for Toolhelp plus `taskkill /T`.
Candidate A's graceful terminate path escalated to forced cleanup in all 20
runs. Production design must still close pywinpty's spawn-to-assignment race;
the library exposes the PID only after spawn and cannot create the shell
suspended inside the Job.

This is not the issue #12 final decision. GitHub-hosted Windows Server is only
repeatability evidence. The unchanged runner must still run on Windows 11 x64
before the contracts for issues #2, #13, #14, and #15 are frozen.

## Reproducibility

- Runner commit: `5b33c4653aab30627de421a13643cd907bef9b12`
- GitHub Actions run: [32485179280](https://github.com/A2C-SMCP/tfbash-mcp/actions/runs/32485179280)
- Environment: Windows Server 2025 Datacenter `10.0.26100`, AMD64
- Python: `3.12.10`
- PowerShell: `7.6.3`
- pywinpty: `3.0.5`
- Observations: `268/268`; every pre-registered gate is complete

The run artifact is named `windows-phase0-hosted-1`. File hashes:

| File | SHA-256 |
|---|---|
| `environment.json` | `665a433c8084a181e6072d5fa8e1e652eec2257ed1a36b4047b34773ea07103a` |
| `observations.jsonl` | `465d6cbe9da86abacdf5713052010fed27e49d604e0cfa0ee85e163ae2336755` |
| `summary.json` | `076f6f8a6d6f8248ae0badb70978710cea00f29b236aee84e7447dba7c94a090` |
| `summary.md` | `9aebb39f2ace42bde49d7f9fcc8896ec95b3e18c41d87cc6060f610d1f79f95c` |

## Results

| Gate | Result | Evidence |
|---|---:|---|
| Unicode | 1/1 | Exact Chinese, emoji, and accented text matched |
| Persistent cwd/env | 1/1 | State survived across commands |
| Multiline | 1/1 | PowerShell multiline wrapper preserved behavior |
| Real exit code | 1/1 | Observed external exit code `37` |
| Text stdin | 1/1 | UTF-8 input round-tripped |
| NUL stdin probe | 1/1 | NUL plus CRLF round-tripped, but the API still accepts `str`, not arbitrary bytes |
| Long command/yield | 0/1 | Ctrl-C did not restore the prompt before the deadline |
| Backpressure/control | 0/1 | Control write returned `0`; no recovery by 3 seconds; forced cleanup succeeded |
| Rapid-exit tail | 0/20 | Sentinel missing in every run; only 26,139–27,711 of 262,144 payload characters arrived |
| Interrupt/rebuild <=3s | 0/20 | Every run timed out; rebuild could not become ready inside the remaining deadline |
| Timeout cleanup/rebuild | 20/20 | Old managed tree exited and replacement shell probe succeeded |
| Candidate EOF | 20/20 | Ctrl-Z plus CRLF (`1a0d0a`) ended foreground stdin and preserved the shell |
| Unique terminal/no late output | 20/20 | One terminal marker and no cross-execution tail |
| Toolhelp terminate/kill/close/shutdown | 80/80 | No reported managed survivor; terminate escalated 20/20 |
| Job terminate/kill/close/shutdown | 80/80 | No reported managed survivor |

The EOF result is Windows-only hosted evidence. The public `shell_write.eof`
field must remain unfrozen until native Windows 11 and the POSIX Bash profile
both pass the same 20/20 semantic contract.

The NUL probe does not prove arbitrary-byte stdin. pywinpty's public write API
accepts a Unicode string and internally converts it to UTF-8; invalid UTF-8 byte
sequences therefore cannot be represented faithfully. Issue #2 must not freeze
the base64 raw-byte contract against this transport without a byte-capable
input path.

## Native gate handoff

Run the exact commit above on Windows 11 x64 with Python 3.12 and PowerShell
7.6.x:

```powershell
uv run --python 3.12.10 --with pywinpty==3.0.5 -- `
  python -m experiments.windows_phase0.runner `
  --environment-tier native-gate `
  --repetitions 20 `
  --output-dir artifacts/windows-phase0-native
```

Return the complete `artifacts/windows-phase0-native` directory without
editing it. The native run is decision-ready only when `summary.json` reports
`"decision_ready": true`; a nonzero process exit is expected when any fixed
gate fails and must not prevent returning the raw artifact.
