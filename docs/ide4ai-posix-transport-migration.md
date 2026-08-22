# ide4ai → PexpectPosixPtyTransport 迁移对照

固定审阅基线：`A2C-SMCP/ide4ai@20ece038e66e13885e77503e217b23766e60dc86`。
许可证与作者归属见仓库根目录 `NOTICE`。

| ide4ai 原实现 | tfbash-mcp 新位置 | 迁移说明 |
|---|---|---|
| `_init_shell()` 的 `pexpect.spawn(..., encoding="utf-8", echo=False, use_poll=True)` | `PexpectPosixPtyTransport.spawn()` | 保留 pexpect/ptyprocess 已验证的 fork/exec、cwd/env、关闭 echo 和 poll 路径；改为 bytes mode，方言独立做 UTF-8 增量解析。 |
| `_init_shell()` spawn 后固定延时/立即 send | `BOOTSTRAP_REQUIRED` 握手 | `PS1` 在 spawn env 中预置为私有 prompt；worker 读到真实初始 prompt 后才写 bootstrap，避免 canonical input 尚未切换时触发 macOS `MAX_CANON` 截断，不使用固定 sleep。 |
| `shell.sendline()` / `shell.send()` | `PexpectPosixPtyTransport.write()` | 删除 50ms `delaybeforesend` 和阻塞式 send；master fd 设置 `O_NONBLOCK`，显式返回 partial write / would-block。 |
| `shell.expect()` / `shell.before` | `PexpectPosixPtyTransport.read()` + `wait()` | 不迁移同步 matcher；唯一 worker 通过 `poll()` 唤醒并直接 `os.read()`，BashDialect 消费字节。 |
| `_recover_after_timeout()` 的 `sendintr()` | #7 PosixProcessSupervisor + BashDialect recovery probe | transport 不拥有 control；SIGINT/TERM/KILL 由 supervisor 映射，方言只负责恢复 framing。 |
| `_rebuild_shell()` / `close(force=True)` | transport `close()` + #7 supervisor cleanup | transport 只关闭 PTY master；进程组 terminate/kill/reap 不由 pexpect 或 transport 越权执行。 |
| `test_stream_io.py` 的 high-fd `use_poll` 回归 | `tests/test_posix_pty.py` wait/read 集成 | 保留 poll 而非 `select.select()` 的架构约束，并增加 tail drain、partial write/backpressure、single-reader、异常/幂等关闭。 |

公共 `shell_write.eof` 已按 #2 的保守合同移除，且 #12 native Windows 证据仍待回传，因此本 adapter 不新增 POSIX-only VEOF port；这里的 EOF 仅表示 PTY output 端关闭。
