# Windows V1 Phase 0 experiment report

Status: native Windows 11 Client x64 release gate complete; decision `pass`.

## Current decision

Candidate B — pywinpty 3.0.5 / native ConPTY with gated bootstrap and a
non-breakaway kill-on-close Job Object — is enabled as the production Windows
V1 profile. The current formal gate is one complete session on a fresh Windows
11 Client x64 against the exact source commit. All 10 mandatory checks must
pass together with `contract_passed=true`, `decision_ready=true`, and
`decision=pass`. Hosted Windows and optional 1–5 repetition runs are diagnostic
smoke only; fixed 20-repetition evidence is no longer a release prerequisite.

The latest formal evidence passed:

- Source commit: `098420b001952bf7592d3ad3da8c515b2f7429e7`
- Run: `supervisor-gate-20260825T044840Z-dfdb00fc`
- Package SHA-256: `e8b5315df5d114d73503ae091a3437d6f9b9beb243287457f811eb570b5dc038`
- Result ZIP SHA-256: `fd362ef66b76cfa7277350e00255a010f71ebb100b09fb3ae82758637fe3f7b0`
- Result: `1/1` session, `10/10` mandatory checks, decision `pass`

The public `shell_write.eof` field remains removed: a Windows-only candidate
behavior is not a portable V1 input contract. Any later production Windows
runtime change must rerun the same formal gate against its new exact source
commit.

## Historical hosted-smoke decision (superseded)

The low-level pywinpty 3.0.5 / native ConPTY transport is a **hosted-smoke
No-Go for the V1 shell contract**. The complete hosted matrix reproduced four
transport failures: rapid-exit tail loss, interrupt/rebuild outside the
three-second deadline, cross-execution late-output contamination, and failure
to establish the pre-registered backpressure state. Hosted Windows Server
cannot make the final architecture decision, so the formal result remains
`inconclusive` until the unchanged reviewed runner completes on Windows 11 x64.

For process ownership, **Candidate B (non-breakaway kill-on-close Job Object)
remains the provisional choice**. Candidate B passed all 80 lifecycle
observations. Candidate A (Toolhelp identities plus `taskkill /T`) passed kill,
close, and shutdown, but its graceful terminate action missed the lifecycle
deadline in all 20 runs and required verified forced cleanup. Candidate B still
has a documented spawn-to-assignment race because pywinpty exposes the shell
PID only after spawn; production work must close or explicitly bound that gap.

At that point issues #2, #13, #14, and #15 were blocked pending native evidence.
That blocking condition is now resolved by the formal evidence above.

## Reproducibility

- Reviewed runner commit: `61e36d30ac70893b5dd9bdf0745ef3ae1e50f0d7`
- Independent code review: `APPROVE`
- GitHub Actions run: [32498409587](https://github.com/A2C-SMCP/tfbash-mcp/actions/runs/32498409587)
- Environment: Windows Server 2025 Datacenter `10.0.26100`, X64
- Python: `3.12.10` X64
- PowerShell: `7.6.3` X64
- pywinpty: `3.0.5`
- uv: `0.8.17`
- Observations: `268/268`; every pre-registered gate is complete
- Hosted outcome: `contract_passed=false`, `decision_ready=false`,
  `decision=inconclusive`

The run artifact is `windows-phase0-hosted-1`. File hashes:

| File | SHA-256 |
|---|---|
| `environment.json` | `4b0948f540e54a795b229abc7f490f9ce0000e0a577c7a602fe5305cf1cb05b1` |
| `observations.jsonl` | `1bb0b96d3ebee1a3fe490017b3a0930b24ccfb2a0f01a41846a7acd4a8415b3b` |
| `summary.json` | `1d17f7beed7b0564c0062829681a66ece36436f6cef9fcaa263657b37fdbda5b` |
| `summary.md` | `c739b52e324fef5c85fc065e70241d4c376a19a3da87c42b4e0b8e2a4534f74e` |

`summary.json` embeds the first two hashes, and both match the downloaded
files. `environment.json` records the full reviewed commit and the actual
Windows-checkout runner script hash.

## Hosted results

| Gate | Result | Evidence |
|---|---:|---|
| Unicode | 1/1 | Exact Chinese, emoji, and accented text matched |
| Persistent cwd/env | 1/1 | State and equivalent TEMP directory survived across commands |
| Multiline | 1/1 | Exact multiline result and exit code matched |
| Real exit code | 1/1 | External exit code `37` observed |
| Text stdin | 1/1 | UTF-8 input and ConPTY echo matched exactly |
| NUL stdin probe | 1/1 | NUL + CRLF and caret echo matched; API remains Unicode-string based |
| Long command/yield | 0/1 | Ctrl-C did not restore the prompt; verified forced cleanup was required |
| Backpressure/control | 0/1 | 16 MiB write attempt returned `0` and ceased pending before pressure was established; control path not exercised |
| Rapid-exit tail | 0/20 | Exactly 12,344 of 262,144 payload characters arrived; sentinel absent 20/20 |
| Interrupt/rebuild <=3s | 0/20 | Prompt recovery and bounded rebuild both missed the deadline |
| Timeout cleanup/rebuild | 20/20 | Original identities exited and replacement probe succeeded |
| Candidate EOF | 20/20 | Ctrl-Z + CRLF (`1a0d0a`) ended stdin and preserved the shell |
| Unique terminal/no late output | 0/20 | Flushed late token contaminated the next execution 20/20 |
| Toolhelp terminate | 0/20 | Graceful path exhausted the deadline; close then required forced cleanup |
| Toolhelp kill/close/shutdown | 60/60 | No tracked managed survivor; expected rebuild state matched |
| Job terminate/kill/close/shutdown | 80/80 | No tracked managed survivor; expected rebuild state matched |

The EOF result is Windows-only hosted evidence. The public `shell_write.eof`
field must remain unfrozen until native Windows 11 and the POSIX Bash profile
both pass the same 20/20 semantic contract.

The NUL probe does not prove arbitrary-byte stdin. pywinpty accepts a Unicode
string and converts it to UTF-8, so invalid UTF-8 byte sequences cannot be
represented faithfully. Issue #2 must not freeze a raw-byte input contract
against this transport without a byte-capable path.

## Historical Windows 11 x64 native handoff (superseded)

Execution package:
`artifacts/handoff/windows-phase0-native-runner-61e36d3.zip`

- Package SHA-256:
  `eec2d10e6e1b6dd13555986994b5cc33b512c361057bfe01afa46374034fc157`
- Source commit embedded in the ZIP:
  `61e36d30ac70893b5dd9bdf0745ef3ae1e50f0d7`
- ZIP integrity: verified
- Extracted runner SHA-256:
  `cd30538121939eff99d94313d9ec8d4e6a4e1e4cf0a4147f17277b72fdeee133`
- Extracted platform-independent contract tests: `40/40` passed

On a Windows 11 x64 client with PowerShell 7.6.x x64 and uv 0.8.17, extract
the package into a new directory and run its top-level entry point:

```powershell
./RUN_WINDOWS11.ps1
```

This historical wrapper used Python 3.12.10, pywinpty 3.0.5, `native-gate`, 20
repetitions, and explicitly recorded the reviewed commit even though the ZIP
had no `.git` directory. It used `uv --no-project`, so the minimal experiment
archive was not installed as the production package. It is retained for audit
history and does not define the current gate.

The old handoff requested the complete `artifacts/windows-phase0-native`
directory and returned zero only for Go. It has been superseded by the
SSH-first one-session supervisor gate described in the current decision.

## Superseded evidence

Commit `5b33c4653aab30627de421a13643cd907bef9b12` and the historical
`windows-phase0-native-runner-449726f.zip` are superseded. Later reviews found
decision, late-output, cleanup, backpressure, and identity-fencing weaknesses;
neither may be used for the native gate.
