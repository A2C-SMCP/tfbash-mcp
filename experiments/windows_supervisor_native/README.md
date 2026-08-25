# Windows supervisor native gate

This isolated harness validates Issue #15's gated Windows process supervisor
against actual Windows 11 x64 Job Object and ConPTY behavior. It never changes
the production runtime selection gate.

Use `scripts/windows_supervisor_lab.py` from macOS for a 1–5 repetition SSH
smoke. The password comes from an owner-only `.env` or a hidden one-time prompt.
The Mac downloads pinned tools once, verifies their SHA-256 values, transfers
them over SSH, and performs the remaining Windows setup without internet or
machine-wide installation.

Normal SSH evidence is hosted-smoke and inconclusive. A separate explicit
`--native-gate` controller option runs exactly 20 fresh native sessions through
SSH; SSH is only the outer control channel and the probe still creates real Job
Objects and ConPTY instances on the Windows target. The deterministic package
also contains `RUN_WINDOWS11_SUPERVISOR.ps1` as an offline fallback. The
evidence evaluator recomputes every check and rejects missing, duplicated,
out-of-order, wrong-version, wrong-architecture, wrong-commit, or incorrectly
promoted results.
