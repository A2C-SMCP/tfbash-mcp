# ide4ai → BashDialect 迁移对照

固定审阅基线：`A2C-SMCP/ide4ai@20ece038e66e13885e77503e217b23766e60dc86`。
许可证与作者归属见仓库根目录 `NOTICE`。

| ide4ai 原符号/测试 | tfbash-mcp 新位置 | 迁移说明 |
|---|---|---|
| `PexpectTerminalEnv.__init__` 中 `_prompt`、`_rc_start/_rc_end` | `BashDialect.prepare_session`、`BashProtocol` | 保留每实例随机哨兵思想；改为每 Shell 128-bit token，并为每个 Execution 再生成独立 token。 |
| `_init_shell()` 的 PS1/bootstrap/startup command | `BashProtocol.initial_input` | 保留受控 PS1 和 startup command；新增 Bash/base64 能力探针、真实 startup exit code、版本与 cwd framing。 |
| `_to_single_line()` | `_encoded_eval()` | 保留 base64 + `eval` 在当前 Shell 执行的策略；所有命令统一包装，以便安全追加 exit/cwd marker。 |
| `_extract_exit_code()` | `BashProtocol._read_result()` | 从字符串 regex 改为随机二进制 record separator + 增量 bytes parser，退出码与普通输出、ANSI/OSC 分离。 |
| `_execute_command()` 的 prompt/exit-code 时序 | `BashProtocol.feed()` 状态机 | 删除同步 `pexpect.expect()`；按 begin → output → result → prompt finalizing 逐块解析。 |
| `_recover_after_timeout()` / `_rebuild_shell()` | `BashProtocol.recovery_input()` + Runtime Ports/后续 ShellWorker | interrupt/kill 归 ProcessSupervisor；方言在 interrupt 后发送独立随机 recovery probe，只有 recovery record + 真实 prompt 都到达才报告 `RECOVERED`，否则由 Worker 用原 ShellStartRequest 重建。 |
| `test_persistent_session`、`test_change_directory` | `tests/test_bash_dialect.py` 真实 Bash framing 用例 | 验证 export/cd 跨命令持久化及 cwd 回传。 |
| `TestPexpectTerminalMultiline` | `tests/test_bash_dialect.py` multiline/heredoc 用例 | 迁移多行、heredoc、失败末行退出码和状态持久化。 |
| `TestPexpectTerminalPromptDesync` | `tests/test_bash_dialect.py` marker 碰撞/分块用例 | 验证旧固定 prompt、相似 marker、ANSI/OSC 和所有 chunk 分割不会污染 framing。 |
| `TestPexpectTerminalRecovery` | 本任务的 finalizing/recovery 纯测试；真实控制留给 #7/#8/#9 | 本任务不复制 pexpect/进程控制；验证 interrupt 后 wrapper result + prompt 可收敛，硬恢复由上层重新创建 protocol。 |

未迁移 Gym、`BaseTerminalEnv`、CommandFilter、IDE action/observation、命令历史、渲染和 MCP envelope；这些均不属于 ShellDialect 职责。
