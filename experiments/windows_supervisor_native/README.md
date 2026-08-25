# Windows supervisor native gate

This isolated harness validates Issue #15's gated Windows process supervisor
against actual Windows 11 x64 Job Object and ConPTY behavior. It never changes
the production runtime selection gate.

Use `scripts/windows_supervisor_lab.py` from macOS for a 1–5 repetition SSH
smoke. The password comes from an owner-only `.env` or a hidden one-time prompt.
The Mac downloads pinned tools once, verifies their SHA-256 values, transfers
them over SSH, and performs the remaining Windows setup without internet or
machine-wide installation.

SSH evidence is always hosted-smoke and inconclusive. The deterministic package
contains `RUN_WINDOWS11_SUPERVISOR.ps1` for the fixed 20-repetition formal gate.
The evidence evaluator recomputes every check and rejects missing, duplicated,
out-of-order, wrong-version, wrong-architecture, wrong-commit, or incorrectly
promoted results.
