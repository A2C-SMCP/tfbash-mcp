# V1 三平台验收矩阵

本文件是父任务 #1 与最终汇聚任务 #11 的可执行验收索引。普通持续集成由
`.github/workflows/v1-integration.yml` 在 macOS 15、Ubuntu 24.04 和 Windows
Server 2025 上执行；Windows release 判定还必须引用真实 Windows 11 Client x64
的原生 SSH gate。Hosted Windows 只负责持续诊断，不能替代 release gate。

## 自动化映射

| 验收面 | 自动化证据 | 执行环境 |
|---|---|---|
| 七工具严格 schema、输入输出 union、错误与平台约束 | `tests/test_protocol.py`、`tests/test_mcp_adapter.py` | 三平台共享测试 |
| MCP initialize、七工具 discovery/call、异步取消与 stdio shutdown | `tests/test_mcp_stdio.py::test_stdio_initialize_lists_and_calls_the_seven_tools`、`test_cancelling_an_inflight_long_call_does_not_block_stdio_shutdown` | 三平台共享测试 |
| Domain 不含平台实现；两个 Profile 服从相同 ports | `tests/test_runtime_contracts.py`、`tests/test_shell_manager.py::test_both_runtime_profiles_reuse_the_same_manager_worker`、`test_worker_has_no_platform_implementation_imports` | 三平台共享测试与 test doubles |
| 多 Shell、状态、cursor/ring buffer、stdin、配额、并发、close/shutdown | `tests/test_domain_*.py`、`tests/test_shell_manager.py` | 常规 `Tests` workflow；关键跨层用例进入三平台矩阵 |
| macOS/Linux 真实 Bash、PTY、前台进程组、控制和回收 | `tests/test_bash_dialect.py`、`tests/test_posix_pty.py`、`tests/test_posix_process.py`、`tests/test_shell_manager.py::test_real_posix_exec_yield_read_timeout_and_reuse` | macOS 15、Ubuntu 24.04 native PTY jobs |
| POSIX HostConfig、标准 venv 环境继承、custom startup、强杀后复用、脱敏 | `tests/test_mcp_stdio.py::test_stdio_posix_host_environment_and_forced_control_end_to_end` | macOS 15、Ubuntu 24.04 native PTY jobs |
| Windows PowerShell/ConPTY/Job Object、gated bootstrap 与生命周期 | `tests/test_powershell_dialect.py`、`tests/test_windows_bootstrap.py`、`tests/test_windows_conpty.py`、`tests/test_windows_process.py`、`tests/test_windows_win32.py` | Windows hosted native ConPTY job + Windows 11 formal gate |
| Windows production stdio、exit code 37、Job kill、可观察 rebuild、startup 重放、close | `tests/test_mcp_stdio.py::test_stdio_uses_the_production_windows_profile_end_to_end` | Windows hosted native ConPTY job |
| standalone/IDE 同 binary、HostConfig 优先级、workspace 元数据与 instructions/description | `tests/test_runtime_config.py::test_host_profiles_share_resolution_and_runtime_descriptions`、`tests/test_server_config.py`、`tests/test_mcp_stdio.py` | 三平台共享测试 |
| 标准 venv 的 `VIRTUAL_ENV` 与 `PATH`/`Path` 注入；Server 不扫描或激活环境 | `tests/test_runtime_config.py::test_standard_venv_is_inherited_without_activation_or_discovery`、两个 production stdio E2E | 三平台矩阵 |
| Conda/custom startup 的方言适配、重建重放与失败收敛 | `tests/test_runtime_config.py::test_custom_startup_is_replayed_by_each_shell_resolution`、`tests/test_shell_manager.py::test_failed_timeout_recovery_observably_rebuilds_and_replays_startup`、两个 production stdio E2E | 三平台矩阵 |
| env value、startup command、解释器路径和 secret 不进入 Agent 可见响应 | `tests/test_protocol.py::test_output_schema_does_not_expose_sensitive_host_fields`、`tests/test_runtime_config.py::test_host_profiles_share_resolution_and_runtime_descriptions`、两个 production stdio E2E | 三平台矩阵 |
| ide4ai prompt/exit/cwd/env/multiline/heredoc/recovery 迁移 | `docs/ide4ai-bash-migration.md`、`docs/ide4ai-posix-transport-migration.md` 及其逐项测试映射 | 文档审计 + POSIX jobs |
| ide4ai 运行时依赖隔离和许可证归属 | `tests/test_package.py::test_distribution_does_not_depend_on_the_complete_ide4ai_runtime`、`test_ide4ai_derivation_notice_retains_baseline_and_mit_terms`、仓库根目录 `NOTICE` | 三平台矩阵 |

## Windows 正式 release gate

当前生效的 gate 是一次新鲜 Windows 11 Client x64 原生会话，不要求固定重复 20
次。会话必须使用待发布精确源码和固定工具链，并同时满足全部 10 项强制检查、
`contract_passed=true`、`decision_ready=true`、`decision=pass`。若同 Shell recovery
失败，只有完成可观察重建且 Job 内零残留才可通过。

已通过证据：

- source commit: `8e0626536aa1509d5919b1c1cb1a674438f21027`
- run id: `supervisor-gate-20260825T093244Z-0ed74ef3`
- package SHA-256: `7fb031328e73f58f2f1e8520c377463d1cb21d9bc579bfde0d1d3fd63b1a6af7`
- result ZIP SHA-256: `887dfcef8e5dad80d9404bbe7a98931e509cebde9f1e3fd35960862827f07ace`
- outcome: `1/1` session、`10/10` mandatory checks、decision `pass`

GitHub-hosted Windows 和手工额外 1–5 次重复只用于观察稳定性，不参与正式判定。
任何 production Windows runtime 变更都必须重新对新的精确 source commit 执行该
release gate。

## 当前集成边界

公共 `shell_write.eof` 已删除，因为 V1 没有可跨 POSIX/Windows 可靠兑现的等价
语义。Windows 11 gate 覆盖关键进程树和控制语义；macOS/Linux/hosted Windows
持续集成覆盖每次变更。#11 只有在三平台 workflow 全绿、Windows 精确源码 gate
有效并且本矩阵仍与父任务验收项逐项对应时才可关闭。
