# 通用 Shell MCP Server 需求与架构说明

| 项目 | 内容 |
|---|---|
| 状态 | Draft / 待评审 |
| 日期 | 2026-08-21 |
| 主实现仓库 | `tfbash-mcp` |
| 首个集成方 | `tfrobot-client` |
| 决策 | 独立 MCP Server；客户端不内置 Shell/PTY 实现 |
| V1 实现 | Python 3.10–3.12、MCP Python SDK、uv/uvx；POSIX 使用 pexpect，Windows 使用经实验选定的 ConPTY transport |
| 0.2.0 平台 | macOS/Linux：Bash、Zsh、PowerShell Core + POSIX PTY；Windows 11 x64：PowerShell 5.1/Core、Git Bash、显式 MSYS2 Zsh + native ConPTY |
| V1 宿主 | standalone 与 IDE 共用同一 Server/工具合同；宿主只负责显式启动配置和生命周期 |
| 合同状态 | 七个工具名、字段与领域模型已冻结；`shell_write` 仅接受 UTF-8 `text`，任意二进制 stdin 与 EOF control 未进入 V1 |
| 信任模型 | 可信本机环境，不提供 approval、sandbox 或命令策略 |

> 本文取代“在 tfrobot-client 内实现 Rust Embedded Bash Provider”的旧方案。Shell 能力被定义为可被任意 MCP Client 使用的独立 Server。`tfbash-mcp` 是当前仓库与包名，不表示公开合同只支持 Bash；0.2.0 支持 Bash、Zsh 与 PowerShell 方言，并将其同宿主原生 PTY/ConPTY 后端组合。tfrobot-client 只为每个 Computer 提供启用开关，并把该 Server 的进程与连接生命周期交给 A2C-SMCP SDK 管理。

## 1. 背景与决策

Agent 需要执行构建、测试、脚本和开发服务；短命令应快速得到结果，长命令应能持续读取输出和接受控制。该能力不属于 Tauri 客户端本身，也不应依赖 tfrobot-client 的 Computer、Tauri Event 或 Rust 类型。

目标架构调整为：

1. `tfbash-mcp` 提供独立、通用的 Shell MCP Server；
2. MCP 工具合同与调用方语言、UI 框架和 Computer 模型无关；
3. V1 使用 Python 实现；POSIX Runtime 使用 pexpect，Windows Runtime 使用经实验选定的 ConPTY transport，但协议层和 Shell 领域层不暴露具体库、fd、HANDLE 或进程枚举概念；
4. standalone workspace 或一个 Computer 启动一个 Shell MCP 进程，进程内状态天然属于该宿主实例；
5. tfrobot-client 只保存开关并向 SDK 声明该 MCP，不管理 Shell、Execution、PTY 或输出缓冲；
6. SDK 负责 MCP 的安装命令、拉起、连接、状态、停止和进程回收。

“语言无关”指 MCP 工具合同和集成方式不依赖实现语言，并不表示当前版本没有 Python 运行时依赖。未来可以用 Rust、Go 等语言重写 Server，只要保持工具合同兼容，客户端和 SDK 无需变化。0.2.0 明确承诺一个 Server 进程只选择一个方言与原生终端后端组合，不能在运行中按工具调用切换。

## 2. 目标

### 2.1 产品目标

1. 任意标准 MCP Client 都能以 standalone 方式配置并使用 Shell MCP，IDE 也能将同一 Server 作为受管 stdio MCP 启动。
2. 短命令能够同步执行，并返回明确的退出状态、输出、耗时和当前目录。
3. 调用方可以创建并显式寻址多个命令 Shell；每个 Shell 跨多次工具调用独立保留 cwd、环境变量和虚拟环境状态。
4. 同一个执行入口同时覆盖短命令和长命令：短命令一次返回，超过等待窗口的命令返回可继续读取的 Execution。
5. 长命令支持增量读取输出、写入 stdin、发送信号和终止，不需要切换到另一套后台 Job 工具。
6. 一个 MCP 进程内的 Command Shell 和 Execution 可跨 MCP 工具调用保持，直到显式关闭、记录过期或 Server 退出。
7. 输出、Command Shell 和 Execution 都有稳定性上限及确定的回收行为。

### 2.2 解耦目标

1. `tfbash-mcp` 不依赖 tfrobot-client、Tauri、A2C-SMCP Computer 类型或 UI 事件。
2. MCP 工具参数中不出现 `computer_id`、`runtime_generation` 或客户端内部路径。
3. tfrobot-client 不实现 pexpect、进程树管理、输出缓冲或 Shell Registry。
4. SDK 或 IDE 把 Shell MCP 当作普通受管 stdio MCP，不为其引入专用传输协议。
5. 一个 Computer 对应一个 MCP 实例；跨 Computer 隔离由进程边界和 SDK Computer 实例保证。
6. Agent 在构造命令前能够稳定获知当前平台、Shell 方言和默认工作目录，而不是从工具名、路径样式或环境变量猜测。

## 3. 范围边界

### 3.1 V1 范围

- macOS、Linux 本机环境：Bash、Zsh 或 PowerShell Core + POSIX PTY；
- Windows 11 x64 本机环境：PowerShell Desktop 5.1/Core、Git Bash 或显式 MSYS2 Zsh + native ConPTY；
- stdio MCP 传输；
- uv/uvx 安装与启动；
- 一个 Server 实例可创建多个显式寻址的持久命令 Shell；
- 前台命令执行；
- 每个 Shell 同时最多一个活动 Execution；
- 短命令同步结果与长命令 `running` 状态自动分流；
- Execution 增量输出、stdin、信号、中断和关闭；
- 进程退出、超时、Server shutdown 时的完整回收；
- MCP 进程内持久化，不跨 MCP 进程重启恢复；
- 无固定间隔轮询：等待输出使用阻塞事件/条件变量和可选 wait timeout。
- Server 启动时选择 `auto`、`bash`、`zsh` 或 `pwsh` Runtime Profile；
- standalone 与 IDE Host Profile；显式 workspace root、稳定 Agent runtime context 和动态工具说明。

### 3.2 明确不提供

- Approval 或执行前确认；
- OS sandbox；
- 命令白名单、黑名单、风险分级或 PolicyEngine；
- 工作区根目录越界限制；
- 网络、环境变量、凭据或宿主机权限隔离；
- 跨 MCP Server 的 Shell/Execution 管理；
- Shell/Execution 跨 Server 重启恢复；
- 独立后台 Job 资源与 `bash_start`/`bash_output`/`bash_write`/`bash_kill` 工具组；
- 原始终端 Session、全屏 TUI 屏幕模型、方向键协议和 resize；
- SSH、串口、容器远程终端；
- tmux/screen 协议；
- Tauri 专用事件或终端 UI；
- CMD、Windows 10 和未经验证的 Windows ARM64；
- WSL（包括显式 `--shell wsl.exe` 和 native Windows 的隐式 fallback）；
- Python IDE、LSP、文件编辑、活动文件/选区采集、Gym Environment。

资源限额、超时、输出上限和进程树回收属于稳定性要求，不属于安全策略。

## 4. 总体架构

```text
standalone MCP Client 或 IDE Host
              │ 显式 HostConfig：host_profile、workspace_root、runtime_profile
              │ MCP stdio
              ▼
       ShellMCPServer
       ├── MCP Tool Adapter          七工具 schema / DTO / error envelope
       └── Shell Domain
           ├── CommandShellManager   Registry、配额与并发
           ├── ShellWorker           每个 Shell 唯一运行时 owner
           ├── Shell / Execution     平台无关状态机
           ├── ExecutionOutputBuffer cursor、retention 与事件通知
           └── ShutdownCoordinator   统一生命周期
              │
              │ RuntimeProfile（Server 启动时选定）
              ▼
       Runtime Ports
       ├── ShellDialect              命令包装、prompt/退出码解析与恢复
       ├── PtyTransport              spawn、非阻塞 read/write、output EOF 检测与 close
       └── ProcessSupervisor         interrupt、terminate、kill 与回收
              │
              ▼
       0.2.0 Runtime Composition（每个进程只选一个）
       ├── ShellDialect             Bash / Zsh / PowerShell
       ├── PtyTransport             POSIX PTY / Windows ConPTY
       └── ProcessSupervisor        POSIX process group / Windows Job
```

这里的 `RuntimeProfile` 是 Server 内部组合根，不是每次工具调用的选择字段。0.2.0 将方言与原生终端后端解耦，由 Server 启动配置 `auto|bash|zsh|pwsh` 一次选定；`auto` 按操作系统定义的候选顺序执行身份、能力和真实受管 PTY 探针，不根据 IDE、`TERM_PROGRAM` 或命令内容猜测。同一进程内的全部 Shell 使用同一组合。

依赖方向固定为：MCP Adapter → Shell Domain → Runtime Ports；具体 Bash/PowerShell、pexpect/ConPTY 和平台进程监管实现反向实现 Runtime Ports。Shell Domain 不得导入具体 transport，不得保存 fd、HANDLE、POSIX process group、Windows Job Object 或平台 PID 枚举细节；Runtime 实现也不得自行构造 MCP error envelope 或修改 Shell/Execution 状态机。

三个 Runtime Port 的职责边界如下：

- `ShellDialect` 负责启动能力探针、命令包装、随机定界符、prompt/退出码解析、startup command、finalizing 和 Shell 恢复；Bash、Zsh 与 PowerShell 可分别同宿主的原生 PTY/ConPTY 后端组合；
- `PtyTransport` 负责 PTY 的创建、非阻塞字节读写、output EOF 检测、就绪等待与底层句柄关闭；它不解释命令输出，也不拥有 Execution；
- `ProcessSupervisor` 负责把领域层的 `interrupt`、`terminate`、`kill` 和 `cleanup` 语义映射到平台进程控制机制，并维护受管进程所有权边界；领域层不直接调用 `killpg`、`jobs -p` 或未来的 Windows API。

只抽取上述确实存在平台差异的边界，不为某个平台不能兑现的能力提供空实现。公开控制合同使用 `interrupt`、`terminate`、`kill` 领域意图：POSIX 可映射为 signal/process group，Windows 可映射为控制台中断、受管进程树终止和强制回收；具体系统调用不渗透到上层。

### 4.1 Host Profile、workspace 与 Agent 感知

`HostProfile` 与 `RuntimeProfile` 正交。`standalone` 和 `ide` 不产生两套 Server，也不改变 Shell 执行语义。Server 启动时把宿主提供的参数解析为一份进程内不可变的 `HostConfig`；它至少包含 `host_profile`、`workspace_root`、`default_cwd`、`runtime_profile`、默认 Shell、默认 startup command 和供子进程继承的环境。原始环境变量只用于创建子进程，不属于 Agent 可见合同。

| Host Profile | workspace 来源 | 宿主职责 | Server 不承担 |
|---|---|---|---|
| `standalone` | 显式 `--workspace-root`；省略时为 Server 启动 cwd | 用户或通用 MCP Client 提供 workspace、默认 cwd、进程环境、可选 startup command，并启动/停止 Server | 猜测编辑器、活动文件、选区或项目虚拟环境 |
| `ide` | IDE/SDK 启动时显式传入 workspace root | 一 workspace/Computer 一进程；解析所选解释器/项目环境，注入进程环境或方言专属 startup command；管理生命周期和诊断展示 | 连接 IDE API、扫描 `.venv`/Poetry/Conda、读取标签页或把编辑器状态并入 Shell Domain |

宿主环境初始化遵守以下边界：

- 标准 Python venv 由 IDE/launcher 解析，优先通过 `VIRTUAL_ENV` 与预置后的 `PATH`/`Path` 注入 Server 进程，使新 Shell 直接继承；Server 不自行查找 `.venv`，也不以执行 `Activate.ps1` 作为 Windows 默认路径；
- Conda、direnv 或自定义工具若不能只靠环境变量表达，可由宿主提供与当前 Runtime Profile 方言一致的 `startup_command`；该命令在初次 open 和自动重建时都必须执行，失败则本次 `shell_open`/rebuild 失败；
- `shell_open` 显式字段逐字段覆盖 `HostConfig`，`HostConfig` 再覆盖 Runtime Profile 默认值。环境按“Server 继承环境 → 宿主注入 → `shell_open.env`”逐层合并；Windows 按大小写不敏感的 key 语义处理；
- Server 不从环境变量推断语言、包管理器或虚拟环境类型，也不向 Agent 回显环境变量值、startup command 或解释器绝对路径。

Agent 不得从 `tfbash-mcp` 名称、路径分隔符或 `TERM_PROGRAM` 猜测运行环境。Server 必须通过互相补强的稳定渠道提供 runtime context：

1. MCP `server/discover.instructions`；兼容旧协议时使用等价的 initialization instructions；
2. 根据进程固定 Runtime Profile 生成的工具 description，明确 command dialect、路径风格和默认工作目录；
3. `shell_list` 顶层固定返回 `runtime` 与 `host` 元数据；`host` 包含 mode 和 workspace，`shell_open` 返回所属 `dialect`；
4. 可选提供 `shell://runtime` resource 作为诊断补充，但不能把资源是否被 Client 注入模型上下文作为正确性的前提。

MCP roots 在协议版本 `2026-07-28` 已 deprecated，V1 不新增对 roots 的依赖。workspace root 使用 Server configuration 显式传递；它是默认 cwd 和相关上下文，不因本项目的可信本机模型而自动成为 sandbox 边界。

### 4.2 实例与所有权

- standalone client 为每个 workspace、SDK 为每个启用 Shell 的 Computer 拉起一个独立 MCP 进程。
- MCP Server 不知道 Computer 的存在，也不接收 `computer_id`。
- Server 内的 Shell ID 和 Execution ID 只需在当前进程内唯一。
- Server 退出时，所有命令 Shell 及其活动 Execution 必须被回收。
- 上游 Agent/SMCP 连接短暂断开时，只要 SDK 保持本地 MCP 进程，Command Shell 和活动 Execution 可以继续运行。
- Computer 停止、用户关闭开关、SDK shutdown 或应用退出时，SDK 停止 MCP，Server 执行统一清理。

### 4.3 一个 Shell 只有一个 PTY owner

同一个 PTY 不能同时由 prompt matcher 和自由 reader 消费，否则会发生抢读和协议失步。每个 Command Shell 必须由唯一 `ShellWorker` 持有一个 Runtime Profile 创建的 PTY session：worker 既驱动 `ShellDialect` 识别随机 prompt/退出码定界符，也把定界符之前的命令输出写入当前 `ExecutionOutputBuffer`。`shell_read` 只读取缓冲，不直接读取 `PtyTransport`；POSIX session 由 pexpect 持有，Windows session 由经实验选定的 ConPTY transport 持有。

长命令不会迁移到另一进程或“后台 Session”：超过 `yield_ms` 后，原命令继续占用原 Shell，Shell 保持 `busy`，调用方通过 `exec_id` 增量读取和控制。需要并行命令时创建或使用另一个 `shell_id`。

V1 不暴露自由流式 raw Terminal。若后续有全屏 TUI 或 xterm.js 场景，应在同一 Shell Registry 中增加明确的 Terminal mode，并继续保证每个 PTY 只有一个 owner；不得重新建立平行的 Terminal Registry。

## 5. MCP 工具合同

工具名统一使用小写 snake_case。所有时间均使用毫秒，输出游标使用单调递增的整数。

### 5.1 工具总览

V1 只暴露一个 Shell 资源模型，共七个工具：

- `shell_open`
- `shell_exec`
- `shell_read`
- `shell_write`
- `shell_signal`
- `shell_list`
- `shell_close`

七个工具名、字段、响应 union 和 Shell/Execution 领域模型均已在 Windows Phase 0 实验后冻结。实验确认语义化的控制、退出码、路径和 runtime context 字段可跨两个 Runtime Profile 兑现；任意二进制 stdin 与 EOF control 未通过同义性门槛，因此未进入 V1 公共合同。

除七个工具外，Server 暴露一个固定的 A2C-SMCP Desktop Window Resource：
`window://io.github.a2c-smcp.tfbash/shell-overview`。`resources/list` 将其声明为面向
assistant、priority 0.8、非 fullscreen 的 `text/markdown` 资源；`resources/read` 返回
当前 Registry 一致性快照。每个 Shell 展示自身字段，并优先展示活动 Execution；空闲时展示
仍在既有 retention 内的最近完成 Execution。输出仅取末尾 500 个 Unicode 字符，超限明确
标记，不扩大 buffer 或 retention。Server 必须声明 `resources.subscribe=true`，Shell 生命周期、
Execution 状态与输出变化通过专用事件信号触发合并后的 `ResourceUpdatedNotification`，不得
使用定时轮询。关闭的 Shell 与已淘汰 Execution 不进入概览，`shell_list` 合同保持不变。

工具的 Server 声明层 `_meta.a2c_tool_meta.tags` 固定如下，并保留其它 `_meta`：

| 工具 | tags |
|---|---|
| `shell_open` | `BuildIn, Create` |
| `shell_exec` | `BuildIn, Create, Read, Update, Delete` |
| `shell_read` | `BuildIn, Read` |
| `shell_write` | `BuildIn, Create, Read, Update, Delete` |
| `shell_signal` | `BuildIn, Update` |
| `shell_list` | `BuildIn, Read` |
| `shell_close` | `BuildIn, Delete` |

`shell_id` 标识持久执行环境；`exec_id` 标识该 Shell 中一次具体命令及其输出。一个 Shell 同时最多有一个活动 Execution，但可以保留多个尚未过期的已完成 Execution 供读取。

#### 共同 schema 规则

- 所有工具输入和结构化输出都是 JSON object，schema 统一设置 `additionalProperties: false`；未知字段、类型不符、超出范围和不允许的 `null` 均返回 `invalid_argument`；
- 下表标为“必填”的字段必须出现；可选字段省略时使用表中默认值。只有明确标为 nullable 的 `shell_open.startup_command`、`active_exec_id`、`last_known_cwd`、Execution snapshot 的 `exit_code` 与 `cwd` 可以为 `null`；
- `shell_id`、`exec_id` 是 1–128 字节的非空 UTF-8 字符串；整数只接受 JSON integer，不接受浮点数或字符串转换；
- 所有进入平台原生 path/env 或 Shell command 的公共字符串，即 `cwd`、`startup_command`、`command` 以及 `env` 的 key/value，都禁止 U+0000，违反时返回 `invalid_argument`；进程级 `--shell` 在 MCP 注册前校验；`shell_write.text` 必须是合法 UTF-8，并按编码后的字节数执行输入上限；
- `shell_open`、`shell_exec`、`shell_read`、`shell_write` 和 `shell_signal` 的字段合同以下文表格为准；`shell_list` 的输入严格为 `{}`；`shell_close` 只有一个必填的 `shell_id` 字段；
- `env` 省略时不增加覆盖项；提供时以 key 覆盖 Server 进程继承的环境。最多 256 项，key 必须匹配 `[A-Za-z_][A-Za-z0-9_]*`，每个 UTF-8 value 不超过 32768 字节；Windows Profile 按不区分大小写的环境变量语义拒绝 `PATH`/`Path` 这类重复 key；
- `yield_ms`、`timeout_ms`、`wait_ms` 和 `duration_ms` 使用单调时钟；只有 `created_at_ms` 是 Unix epoch wall-clock timestamp。

协议 schema 使用 JSON Schema Draft 2020-12，并要求识别
`https://github.com/A2C-SMCP/tfbash-mcp/schema/v1` vocabulary。该 vocabulary 的
`x-validUtf8`、`x-utf8-maxBytes`、`x-nativeAbsolutePath`、`x-caseInsensitiveUniqueKeys`
与 `x-fieldLessThanOrEqual` 分别断言 UTF-8 有效性/编码后字节上限、
平台原生绝对路径、Windows object key 大小写不敏感唯一性和字段间数值顺序；该 vocabulary
还把 `integer` 收紧为不接受浮点数的 JSON/Python integer。它们不是可忽略的 annotation。标准 JSON
Schema vocabulary 无法等价表达这些条件，协议消费者必须使用公开的
`validate_schema_instance`，服务端则继续通过对应 DTO validator 执行同一约束。

### 5.2 `shell_open` 与 Shell 管理

`shell_open` 创建持久 Command Shell，输入初始 cwd、环境和可选启动命令。Shell 程序已在
Server 启动时选定，不能按会话覆盖：

```json
{
  "cwd": "/workspace",
  "env": {
    "PROJECT_ENV": "development"
  },
  "startup_command": "source .venv/bin/activate"
}
```

输入字段：

| 字段 | 类型 | 必填 | 默认值与约束 |
|---|---|---|---|
| `cwd` | string | 否 | `HostConfig.default_cwd`；必须是启动时存在且可进入的平台原生绝对目录 |
| `env` | object<string,string> | 否 | `{}`；按共同规则覆盖 HostConfig 已冻结的 Server/宿主环境 |
| `startup_command` | string 或 null | 否 | Server `startup_command`；显式 `null` 表示不运行启动命令，字符串不能为空且不超过 `max_command_bytes` |

```json
{
  "shell_id": "shell_01",
  "status": "ready",
  "cwd": "/workspace",
  "dialect": "bash"
}
```

`shell_list` 返回 Registry 一致性快照：

```json
{
  "runtime": {
    "platform": "windows",
    "dialect": "pwsh",
    "shell_version": "7.6.4",
    "default_cwd": "C:\\work\\project"
  },
  "host": {
    "mode": "ide",
    "workspace_root": "C:\\work\\project"
  },
  "shells": [
    {
      "shell_id": "shell_01",
      "status": "busy",
      "last_known_cwd": "C:\\work\\project",
      "active_exec_id": "exec_02",
      "created_at_ms": 1787115600000
    }
  ]
}
```

`shell_open` 成功结果固定包含 `shell_id`、`status="ready"`、最终 `cwd` 和 `dialect="bash|zsh|pwsh"`。`shell_list` 顶层固定包含 `runtime`、`host` 和 `shells`：`runtime` 至少给出 `platform`、`dialect`、`shell_version`、`default_cwd`；`host` 固定给出 `mode="standalone|ide"` 与 `workspace_root`。不得返回原始 env、startup command、解释器绝对路径或任何 secret。每个 Shell 项固定包含 `shell_id`、`status`、`last_known_cwd`、`active_exec_id` 和 `created_at_ms`；`last_known_cwd` 在尚未确认或故障时为 `null`，`active_exec_id` 仅在 `busy`/`rebuilding` 且仍有活动 Execution 时为字符串，其他状态固定为 `null`，字段不得省略。

`shell_close` 输入 `{"shell_id":"shell_01"}`。它先把 Shell 标记为 `closing`，终止活动 Execution 的受管前台执行树和仍在 Runtime Profile 所有权边界内的子孙，关闭 PTY 并从 Registry 移除。正常清理返回 `{"shell_id":"shell_01","status":"closed","cleanup_complete":true}`；若 `close_timeout_ms` 到期，Server 必须至少执行平台强制回收、关闭 PTY、移除 Registry 并返回相同结构但 `cleanup_complete=false`，异步 reaper 只做非阻塞收尾，不能重新暴露该 Shell。

管理约束：

- Server 启动时不创建隐式默认 Shell；
- Server 启动时对候选程序执行身份、退出码、Unicode、多行、cwd/env 和真实受管 PTY/ConPTY 能力探针；PowerShell Desktop 5.1、稳定 Core、Bash 与 Zsh 按平台组合准入，CMD、WSL 或未通过探针的程序被拒绝；
- `shell_open` 只有在初始 prompt 和 `startup_command` 成功后才注册并返回 `ready`。spawn、初始 prompt、能力探针和 startup command 共用一个从 open 被接受起计算的 `shell_startup_timeout_ms` 总 deadline；启动命令非零、deadline 到期、EOF 或 spawn 失败统一清理受管进程并返回 `shell_start_failed`，不占用 Shell 配额；
- Shell Registry 状态为 `ready`、`busy`、`rebuilding`、`closing` 或 `error`；
- Registry 创建、注册、关闭、列举和容量计数由 manager 级锁保护，但持锁时不得执行阻塞 PTY 操作；
- 并发 `shell_open` 不得突破 `max_command_shells`；
- 当前 Registry 中不存在的 `shell_id` 返回 `shell_not_found`。

### 5.3 `shell_exec`：短命令与长命令统一入口

输入：

```json
{
  "shell_id": "shell_01",
  "command": "cd project && uv run pytest",
  "yield_ms": 10000,
  "timeout_ms": 120000,
  "max_output_bytes": 1048576
}
```

`command` 由进程选定的方言解释：上例属于 Bash/Zsh；PowerShell 应使用 `Set-Location project; uv run pytest`。Windows 也可能选择 Git Bash 或显式 MSYS2 Zsh，POSIX 也可能选择 PowerShell Core。Server 不翻译方言，也不根据命令内容切换。

输入字段：

| 字段 | 类型 | 必填 | 默认值与约束 |
|---|---|---|---|
| `shell_id` | string | 是 | 必须命中当前 Registry 中的 `ready` Shell |
| `command` | string | 是 | 非空 UTF-8 字符串，编码后不超过 `max_command_bytes` |
| `yield_ms` | integer | 否 | `command_yield_ms`；范围 0–60000，0 表示创建后立即 yield |
| `timeout_ms` | integer | 否 | `command_timeout_ms`；范围 1–86400000，从 Execution 创建起计时 |
| `max_output_bytes` | integer | 否 | `output_buffer_bytes`；范围 4096–`output_buffer_bytes` |

`yield_ms` 是本次 MCP 调用等待命令结果的时间窗口，不会终止命令；`timeout_ms` 是命令允许运行的总时长。二者必须独立。

命令在 `yield_ms` 内结束时，一次返回终态 snapshot；输出仍受有界缓冲约束，超大输出可能带截断标记：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_01",
  "status": "exited",
  "exit_code": 0,
  "output": "42\n",
  "buffer_start_cursor": 0,
  "next_cursor": 3,
  "truncated_before_cursor": false,
  "eof": true,
  "cwd": "/workspace/project",
  "duration_ms": 1532,
  "shell_status": "ready",
  "shell_rebuilt": false
}
```

命令超过 `yield_ms` 仍在运行时，返回已有输出和可继续寻址的 `exec_id`，命令继续占用原 Shell：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "status": "running",
  "exit_code": null,
  "output": "server starting...\n",
  "buffer_start_cursor": 0,
  "next_cursor": 19,
  "truncated_before_cursor": false,
  "eof": false
}
```

执行约束：

- `shell_exec` 只接受 `ready` Shell，并在启动命令前原子地把它切换为 `busy`；busy Shell 返回 `shell_busy`；
- `max_output_bytes` 指定该 Execution 的 ring buffer 容量；省略时使用 `output_buffer_bytes`，有效范围为 4096 到 Server 配置上限，超过上限返回 `invalid_argument`；
- 命令中的 cwd/env 修改使用当前方言的原生语法，并只影响指定 Shell 的后续调用；
- V1 的受支持执行模型是前台命令。Runtime Profile 必须在 Shell 回到 `ready` 前识别并清理仍属于当前 Execution 的受管子孙：POSIX supervisor 按受管 session/process group/后代身份清理，Windows 使用经实验确定的 process-tree/Job Object 所有权；清理期间的输出仍属于当前 Execution，清理失败则以 `shell_error` 封存并把 Shell 置为 `error`；
- `disown`、`nohup`、`setsid`、double-fork、PowerShell 脱离进程或其他逃离 Runtime Profile 所有权边界的 daemonization 明确不受 V1 支持；可信调用方必须用前台长命令配合 `shell_exec`/`shell_read`，或为并行任务创建另一个 Shell。Server 不承诺发现、控制或回收已逃离所有权边界的进程，也不承诺这类命令的迟到输出隔离；
- POSIX PTY 与 ConPTY 都返回 combined terminal text output，V1 不承诺 stdout/stderr 原始分流；
- Execution 状态为 `running`、`exited`、`timeout`、`cancelled` 或 `shell_error`；只要当前方言恢复受控 prompt 并返回可信退出码，无论此前是否投递过信号，都归类为 `exited`，非零退出码仍属于 `exited`；
- `shell_exec` 和 `shell_read` 共享同一 Execution snapshot 字段：`shell_id`、`exec_id`、`status`、`exit_code`、`output`、`buffer_start_cursor`、`next_cursor`、`truncated_before_cursor`、`eof`；终态还应返回 `duration_ms`、`cwd`、`shell_status` 和 `shell_rebuilt`。其中 `cwd` 是命令结束后最后一次成功确认的目录；若 Shell 在确认前关闭或故障则为 `null`，不得为获取它继续操作失效 PTY；
- `exec_id` 在 Server 进程内唯一，后续 read/write/signal 必须同时匹配所属 `shell_id`，防止跨 Shell 误用；
- Execution 与 Shell 的终态映射必须遵守 5.6 的状态转换表，不能假定任意 Execution 终态都会使 Shell 恢复 `ready`；已完成 Execution 的输出按 retention 和数量上限保留。

#### 内部 finalizing gate

driver 识别到用户命令的退出码/prompt 定界符后、完成受管子孙 cleanup 和 output quiet barrier 前，Execution 对外仍返回 `running`，但内部进入 `finalizing`：

- 进入 `finalizing` 的 CAS 同时关闭 input gate、取消命令 `timeout_ms` deadline、丢弃尚未投递到 PTY 的 queued write 并释放容量；这些 write 已经返回 `accepted`，不再承诺投递；
- 此后新的或尚未求值的 `shell_write`/`shell_signal` 固定返回 `exec_not_active`，绝不能把用户输入或信号作用于方言 prompt、内部 cleanup 命令或下一条 Execution；
- job cleanup、reap 和 output quiet 必须在 `job_cleanup_timeout_ms` 总 deadline 内完成，其中 quiet 表示 PTY 连续 `output_quiet_ms` 无新字节；成功后才封存为 `exited`，deadline 到期则封存为 `shell_error` 并把 Shell 置为 `error`；
- close/shutdown 仍可通过保留控制通道抢占 `finalizing`，并按 5.6 的终态 CAS 规则产生 `cancelled` 或保留已先封存的终态。

Snapshot 中所有共同字段都必须出现，终态附加字段只在终态出现：

| 字段 | 类型 | 出现条件与约束 |
|---|---|---|
| `shell_id` / `exec_id` | string | 始终出现，遵守共同 ID 约束 |
| `status` | enum | 始终出现，取下表五种值 |
| `exit_code` | integer 或 null | 始终出现；只有 `exited` 非空。Bash/Zsh 与 POSIX PowerShell 为 0–255；Windows PowerShell 将原生 32-bit 退出码规范化为 0–4294967295 |
| `output` | string | 始终出现，是本次 cursor 窗口内的规范化 UTF-8 文本 |
| `buffer_start_cursor` / `next_cursor` | integer | 始终出现，非负且 `buffer_start_cursor <= next_cursor <= write_cursor` |
| `truncated_before_cursor` / `eof` | boolean | 始终出现 |
| `duration_ms` | integer | 仅终态出现，非负；从 Execution 创建到终态封存，包含 finalizing 或 timeout recovery 时间 |
| `cwd` | string 或 null | 仅终态出现，语义见上文 |
| `shell_status` | enum | 仅终态出现，`ready/error/closing` |
| `shell_rebuilt` | boolean | 仅终态出现 |

具体状态取值矩阵如下：

| `status` | `exit_code` | `duration_ms`/`cwd`/`shell_status`/`shell_rebuilt` | 判定规则 |
|---|---|---|---|
| `running` | `null` | 不出现 | 命令尚未到达终态 |
| `exited` | 平台规范化 integer | 必须出现；通常 `shell_status=ready` | 方言 prompt 与退出码定界符完整；包括控制投递后 Shell 最终返回的状态码 |
| `timeout` | `null` | 必须出现；`shell_status=ready/error` | `timeout_ms` 到期，由 Server 发起恢复或重建 |
| `cancelled` | `null` | 必须出现；`shell_status=ready/error/closing` | close/shutdown 的取消 CAS 胜出，或强制终止后 Shell 重建成功/失败 |
| `shell_error` | `null` | 必须出现；通常 `shell_status=error` | 无法取得可信 `$?` 或 Shell/worker 故障 |

若 close admission fence 先发生、自然终态 CAS 后发生但先于 close cancellation CAS，则保留自然终态，同时该 snapshot 的 `shell_status=closing`；因此 `exited`、`timeout` 或 `shell_error` 也可能与 `closing` 组合。`shell_rebuilt` 始终是 boolean。`eof` 不由 `status` 单独决定，而只表示终态输出是否已经读到当前末尾。

### 5.4 `shell_read`：增量输出与最终结果

输入：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "cursor": 19,
  "max_bytes": 65536,
  "wait_ms": 30000
}
```

输入字段：

| 字段 | 类型 | 必填 | 默认值与约束 |
|---|---|---|---|
| `shell_id` | string | 是 | 必须与 Execution 所属 Shell 一致 |
| `exec_id` | string | 是 | 必须命中活动或尚在 retention 内的 Execution |
| `cursor` | integer | 是 | 范围 0–Execution 当前 `write_cursor`，边界规则见下文 |
| `max_bytes` | integer | 否 | `max_read_bytes`；范围 4–`max_read_bytes` |
| `wait_ms` | integer | 否 | 0；范围 0–60000，0 表示不等待 |

运行中增量结果：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "status": "running",
  "exit_code": null,
  "output": "listening on :8080\n",
  "buffer_start_cursor": 0,
  "next_cursor": 38,
  "truncated_before_cursor": false,
  "eof": false
}
```

最终结果：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "status": "exited",
  "exit_code": 0,
  "output": "shutdown complete\n",
  "buffer_start_cursor": 0,
  "next_cursor": 56,
  "truncated_before_cursor": false,
  "eof": true,
  "cwd": "/workspace/project",
  "duration_ms": 42000,
  "shell_status": "ready",
  "shell_rebuilt": false
}
```

读取约束：

- `wait_ms` 表示等待“新输出或状态变化”的最长时间，由 condition/event 唤醒；不得使用固定 sleep-loop 轮询；
- `wait_ms>0` 的调用必须先为该 Execution 原子预留一个 waiter slot；同时等待数达到 `max_read_waiters_per_execution` 时返回 `resource_limit`，响应、取消或异常退出都必须释放 slot。`wait_ms=0` 不占 waiter slot；
- 等待超时只返回空增量和当前 `running` 状态，不影响命令；
- PTY 字节先经过有状态增量 UTF-8 decoder；非法序列在写入 buffer 前稳定替换为 U+FFFD。cursor 是这份规范化 UTF-8 combined output 的单调逻辑字节位置；
- `buffer_start_cursor` 是当前 ring buffer 中最早仍可读取的逻辑位置。请求 cursor 小于它时，从 `buffer_start_cursor` 返回并设置 `truncated_before_cursor=true`；`next_cursor` 按实际返回窗口末尾计算；
- `max_bytes` 有效范围为 4 到 `max_read_bytes`；返回窗口必须在 UTF-8 code point 边界结束，可以少于 `max_bytes`。Server 无需保存历史 cursor 集合：`cursor` 只要是 0 到当前 `write_cursor` 的整数即可；小于 `buffer_start_cursor` 时按截断规则读取，等于它时从该位置正常读取；仍在保留区内但不落在 UTF-8 code point 边界时返回 `invalid_cursor`；
- ANSI/OSC 字节作为普通 UTF-8 文本保留，V1 不执行 ANSI 清洗，也不承诺单次 chunk 包含完整控制序列；调用方按 cursor 顺序拼接即可；
- Execution 进入终态且本次读取到当前输出末尾时才返回 `eof=true`；
- `max_retained_executions` 是 Server 全局已完成 Execution 上限。清理时先删除 TTL 已到期记录；仍超限则按完成时间从旧到新淘汰，活动 Execution 永不因该上限淘汰；淘汰后返回 `exec_not_found`。

### 5.5 `shell_write`、`shell_signal` 与关闭

`shell_write` 只向当前活动 Execution 的 stdin 写入 UTF-8 文本：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "text": "yes\n"
}
```

输入字段：

| 字段 | 类型 | 必填 | 语义与约束 |
|---|---|---|---|
| `shell_id` | string | 是 | 必须与活动 Execution 所属 Shell 一致 |
| `exec_id` | string | 是 | 必须是该 Shell 当前活动 Execution |
| `text` | string | 是 | UTF-8 编码后不超过 `max_write_bytes` |

V1 不提供任意二进制 stdin 或 EOF control；`data_base64`、`eof` 及其他未知字段统一返回 `invalid_argument`。Server 必须在入队时原子预留该 Shell 的操作数和输入字节容量；超过 `max_pending_operations` 或 `max_pending_write_bytes` 时返回 `resource_limit`，不能部分接受。完整文本 payload 成功进入这条有界 Server 队列后立即返回：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "status": "accepted",
  "accepted_bytes": 4
}
```

`accepted_bytes` 是 `text` 以 UTF-8 编码并进入队列后的字节数。`status=accepted` 只保证 Server 已有界缓存输入，不保证子进程已经读取，close/cancel 可能丢弃尚未写入 PTY 的尾部；需要应用级确认时，调用方必须读取命令自身的确认输出。

`shell_signal` 向活动 Execution 发送平台无关的控制意图：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "signal": "interrupt"
}
```

`shell_signal` 输入固定包含必填的 `shell_id`、`exec_id` 和 `signal`，其中 `signal` 只能是 `interrupt`、`terminate` 或 `kill`。名称描述领域意图，不承诺存在同名操作系统 signal。

worker 求值时再次确认 `exec_id` 仍为活动 Execution，并在控制请求成功投递到该时刻的受管前台工作后返回：

```json
{
  "shell_id": "shell_01",
  "exec_id": "exec_02",
  "status": "delivered",
  "signal": "interrupt"
}
```

控制约束：

- `interrupt` 是协作式中断当前前台工作：POSIX 映射到前台进程组中断，Windows 映射到 ConPTY/控制台 Ctrl-C 路径；`terminate` 和 `kill` 分别请求有宽限与强制的受管执行树回收，不能只处理直接子进程；
- `shell_signal` 入队时原子预留一个 pending operation；队列已满返回 `resource_limit`。MCP 调用等待 worker 实际尝试投递后才返回 `delivered` 或结构化错误；
- `delivered` 只表示 OS 接受信号投递，不代表进程已经退出；prompt driver 无法仅凭方言退出码区分真实 signal wait status 与显式同值退出，因此信号后的可信 prompt 仍统一产生 `exited`，最终状态必须通过 Execution snapshot 判断；
- write/signal 的 `exec_id` 不是当前活动 Execution 时返回 `exec_not_active`；
- 每个 `ShellWorker` 通过单一操作队列串行处理 exec、write、signal、close 和内部恢复，任何 MCP handler 不直接操作 PTY；
- `shell_close` 可以关闭 busy Shell：它原子地拒绝新操作，取消活动 Execution，并在 `close_timeout_ms` 总 deadline 内按 terminate → `shutdown_grace_ms` → kill 回收受管进程；deadline 到期按 5.2 的 `cleanup_complete=false` 合同有界返回；
- 命令超时先发送 `interrupt`，并最多等待 `recovery_grace_ms` 恢复 prompt；恢复失败时重建底层 Shell，保持逻辑 `shell_id`，重新应用初始配置和启动命令，重建受 `shell_startup_timeout_ms` 限制，并在结果中返回 `shell_rebuilt=true`；
- 自动重建失败时 Shell 进入 `error`，拒绝 `shell_exec`，但仍可 `shell_close`；V1 不提供公开 `shell_reset`，调用方需要全新状态时 close 后重新 open。

### 5.6 执行状态流

短命令和长命令使用同一状态机：

```text
Shell ready
    │ shell_exec：创建 exec_id，Shell → busy
    ▼
Execution running
    ├── yield_ms 内结束 ──► shell_exec 返回终态 snapshot
    │
    └── yield_ms 到期 ───► shell_exec 返回 running + cursor
                               │
                               ├── shell_read：事件等待增量输出/状态变化
                               ├── shell_write：写入 stdin
                               ├── shell_signal：中断或终止
                               └── worker 检测退出/超时/关闭/故障
                                      ├── prompt 可用 ─────► Shell ready
                                      ├── Shell 不可恢复 ──► Shell error
                                      └── shell_close ─────► Shell closing → removed
```

Execution 与 Shell 状态按下表转换：

| 触发 | Execution 终态 | Shell 后续状态 | 关键结果字段 |
|---|---|---|---|
| 命令返回，定界符完整且 finalizing cleanup 成功 | `exited` | `ready` | `exit_code` 非空，`shell_rebuilt=false` |
| 信号投递后定界符完整且 finalizing cleanup 成功 | `exited` | `ready` | `exit_code` 使用 Bash 返回值；不声称获得真实 signal wait status |
| job cleanup/output quiet deadline 到期 | `shell_error` | `error` | 丢弃待输入并保留已收集输出；仅允许 close |
| `timeout_ms` 到期，Ctrl-C 后恢复 prompt | `timeout` | `ready` | `shell_rebuilt=false` |
| `timeout_ms` 到期，软恢复失败但重建成功 | `timeout` | `ready` | `shell_rebuilt=true`，临时 cwd/env 状态可能丢失 |
| timeout 后重建失败 | `timeout` | `error` | `shell_status=error`；仅允许 close |
| Shell 意外 EOF、spawn/worker 故障 | `shell_error` | `error` | 保留已收集输出；仅允许 close |
| `shell_close` 或 Server shutdown | `cancelled` | `closing` → removed | 唤醒已接受的 waiter；关闭后不保留 Execution |

表中的 `ready/error` 是没有并发 close 时的通常后续状态；若 close admission fence 已先把 Shell 置为 `closing`，则按下节 CAS 规则保留先到达的 Execution 终态，但 Shell 不得逆向恢复为 `ready/error`。

Execution 到达终态不依赖调用方持续读取；`ShellWorker` 始终负责消费 PTY、识别退出定界符并封存结果。调用方稍后可在 retention 窗口内用 `exec_id` 读取尾部输出和最终状态。多个长命令需要多个 Shell，同一个 Shell 不会在活动 Execution 尚未结束时接受下一条命令。

#### close 并发线性化

- manager 接受 `shell_close` 时原子地把 Shell state 切换为 `closing`，该点是新操作的 admission fence；此后新发起的 exec/read/write/signal/close 返回 `shell_closing`；
- write/signal 的状态检查、队列容量预留和入队必须与 admission fence 使用同一 per-Shell mutex 临界区，保证不存在“检查时 ready、入队时已 closing”的穿越操作；
- admission fence 之前已接受的 write 已经得到 `accepted` 响应；worker 只在 PTY 可写时分片推进，close control 可以抢占并丢弃尚未投递的尾部。此前已入队且尚未响应的 signal 在 close operation 前按 FIFO 求值；若求值时 Execution 仍活动则投递并响应，若命令已经自然结束则返回 `exec_not_active`，不得悬挂；
- worker 开始执行 close operation 时，在同一临界区对 Execution 做一次终态 CAS：若它仍为 `running`，则 `running → cancelled`，后续 prompt/EOF 事件不得覆盖；若它已经先进入 `exited`、`timeout` 或 `shell_error`，则保留先到达的自然终态，不改写为 `cancelled`；
- close 封存最后输出并广播 condition/event；admission fence 前已在等待的 `shell_exec` 和 `shell_read` 持有 Execution 引用，即使 Registry 随后移除，也必须返回 CAS 实际胜出的终态 snapshot；
- close 在进程树回收、Execution 封存和 waiter 唤醒之后从 Registry 移除 Shell 并返回 `closed`；移除后新调用返回 `shell_not_found`。

### 5.7 统一错误合同

参数校验、ID 查找或状态检查导致操作未被接受时，MCP result 设置 `isError=true`，并在 structured content 返回统一 envelope：

```json
{
  "error": {
    "code": "shell_busy",
    "message": "shell shell_01 already has an active execution",
    "shell_id": "shell_01",
    "exec_id": "exec_02",
    "retryable": true
  }
}
```

错误对象固定包含 `code`、`message` 和 `retryable`；只有请求中存在且已通过格式校验的 ID 才回显对应 `shell_id`/`exec_id`。V1 错误码和重试语义固定如下：

| `code` | 触发条件 | `retryable` |
|---|---|---|
| `invalid_argument` | 缺字段、未知字段、类型或范围不合法 | `false` |
| `invalid_cursor` | cursor 超过 write cursor 或位于保留区内的 UTF-8 code point 中间 | `false` |
| `unsupported_shell` | executable 不兼容已选 Runtime Profile 或未通过方言能力探针 | `false` |
| `shell_start_failed` | spawn、初始 prompt 或 startup command 失败 | `false` |
| `shell_not_found` | 当前 Registry 无此 Shell | `false` |
| `shell_busy` | `shell_exec` 命中活动 Execution | `true` |
| `shell_closing` | admission fence 后访问正在关闭的 Shell | `false` |
| `shell_unavailable` | Shell 为 `rebuilding` 或 `error`；仅 rebuilding 时为 `true` | 取决于当前状态 |
| `exec_not_found` | Execution 不存在、已淘汰或不属于给定 Shell | `false` |
| `exec_not_active` | write/signal 在求值时 Execution 已终止或不是当前活动项 | `false` |
| `resource_limit` | Shell 数量、read waiter slot、队列操作数或输入字节容量不足 | `true` |

`retryable=true` 仅表示在不修改参数的情况下等待状态/容量变化后重试可能成功，不表示 Server 自动重试。

各工具允许返回的业务错误如下；未列出的异常只能作为实现故障记录诊断，不能临时发明新的公开错误码：

| 工具 | 允许的 `code` |
|---|---|
| `shell_open` | `invalid_argument`、`unsupported_shell`、`shell_start_failed`、`resource_limit` |
| `shell_list` | `invalid_argument`（输入不是严格的空 object） |
| `shell_close` | `invalid_argument`、`shell_not_found`、`shell_closing` |
| `shell_exec` | `invalid_argument`、`shell_not_found`、`shell_busy`、`shell_closing`、`shell_unavailable` |
| `shell_read` | `invalid_argument`、`invalid_cursor`、`shell_not_found`、`shell_closing`、`exec_not_found`、`resource_limit` |
| `shell_write` | `invalid_argument`、`shell_not_found`、`shell_closing`、`shell_unavailable`、`exec_not_found`、`exec_not_active`、`resource_limit` |
| `shell_signal` | `invalid_argument`、`shell_not_found`、`shell_closing`、`shell_unavailable`、`exec_not_found`、`exec_not_active`、`resource_limit` |

一旦 `shell_exec` 已创建 `exec_id`，命令的非零退出、信号投递后的退出、timeout、cancelled 和 `shell_error` 都作为 Execution snapshot 返回，`isError=false`，以便保留部分输出与终态；只有工具操作本身未被接受时才使用错误 envelope。

## 6. 并发、输出与生命周期

### 6.1 V1 pexpect transport 线程模型

本节是 `PexpectPosixPtyTransport` 的 V1 实现约束，不是 Shell Domain 的公共接口。pexpect 是阻塞接口，MCP async handler 禁止直接调用阻塞的 `expect()`。

- 每个 Command Shell 由专属 `ShellWorker` 持有 pexpect spawn 和操作队列，exec、write、signal、close 及内部恢复都由该 worker 串行执行；
- `CommandShellManager` 使用独立 Registry 锁保护 Shell 的创建、注册、关闭、列举快照和容量计数，不在持有 Registry 锁时执行阻塞 PTY 操作；
- MCP handler 只提交操作或订阅 Execution 事件，不直接读写 PTY；
- Execution 运行期间，worker 必须用 `poll/select` 同时等待 PTY 可读、操作队列唤醒和命令 deadline；不得用一次阻塞到命令结束的 `expect()` 阻塞 write/signal/close；
- PTY master 必须使用 nonblocking write。worker 只在 writable 事件到达时按保存的 offset 分片推进 queued input，同时继续处理 PTY read、signal、close 和 deadline；EAGAIN/partial write 不能阻塞 worker，close/shutdown 可抢占并释放未投递 input；
- prompt 和退出码定界符由 worker 对增量字节流解析，不能另起 reader 与 worker 竞争同一 fd；
- worker 把输出写入当前 Execution 的线程安全 ring buffer，并通过 condition/event 唤醒 `shell_exec`/`shell_read` 等待者；
- worker 在 command delimiter 后执行内部 `finalizing` gate；finalizing 不接受用户 input/signal，所有清理和 quiet wait 都受独立总 deadline 约束；
- 不同 Shell 拥有不同 worker，可以真实并行运行。

### 6.2 V1 ConPTY transport 线程模型

`ConPtyTransport` 必须复用与 POSIX 相同的单 owner `ShellWorker` 和操作队列，不得把 pywinpty 或 Win32 callback 直接暴露给 MCP handler：

- Phase 0 对比 pywinpty 3.0.5 的 ConPTY 路径与必要的 Windows Job Object/process inspection 组合，固定版本、后端和编码配置；
- PowerShell 启动时固定 UTF-8 input/output encoding，安装随机 prompt/退出码 marker，并验证受限环境下 bootstrap 失败会 fail closed，而不是静默退回主机 code page；
- PTY reader、writer、process waiter 和 supervisor 事件统一汇入 worker；快速退出必须 drain 完整尾部输出并产生唯一终态；
- Windows 不伪造 POSIX foreground process group 或 stdin-wait 事实；无法证明的 readiness 只能使用受控 prompt marker、输出事件和有界 timeout；
- Phase 0 已验证 interrupt、terminate、kill 和 shutdown 的跨平台行为并冻结相应合同；EOF control 未通过同义性门槛，未进入 V1 公共合同。

### 6.3 输出缓冲

每个 Execution 使用独立的有界 UTF-8 byte ring buffer：

- PTY 原始字节先经过有状态 UTF-8 decoder，规范化后再写入 buffer；
- cursor 是从 Execution 创建开始累计写入的规范化 UTF-8 字节位置，buffer 淘汰必须保持 code point 边界；
- 每次结果都返回 `buffer_start_cursor`；读取过旧 cursor 时从该位置开始并返回 `truncated_before_cursor=true`；
- 单次读取受 `max_bytes` 上限约束并在 code point 边界结束；
- 缓冲容量、单次读取上限、Server 全局完成记录数量和 retention time 可配置；
- 对受支持的前台命令及 Runtime Profile 可发现的受管子孙，Execution 完成并通过 cleanup/quiet barrier 后 buffer 被封存；后续命令使用新的 `exec_id` 和 buffer，不会串流；脱离受管所有权边界的 daemonization 按 5.3 明确不受支持；
- V1 原样保留规范化文本中的 ANSI/OSC，不执行有状态终端清洗或屏幕解释。

### 6.4 进程树回收

- POSIX Shell 运行在独立 process group/session；Windows Shell 使用 Phase 0 选定的 Job Object 或带创建时间身份围栏的进程树监管，不能把 shell pid 冒充真实 foreground process group；
- `shell_signal` 和 timeout 恢复作用于活动 Execution 的受管前台工作，具体映射遵守 5.5 的领域控制语义；
- POSIX supervisor 在恢复 `ready` 前按受管 session/process group/后代身份清理；Windows supervisor 必须对 shell 与当前可观察子孙做身份围栏、分层回收和存活复核；
- V1 对 Runtime Profile 所捕获的受管 descendant 承诺完整回收；显式逃离 session/Job Object/受管树的进程不在所有权边界内，具体限制遵守 5.3；
- Server 收到宿主终止、stdin EOF、正常退出或异常关闭时执行同一 `ShutdownCoordinator`；
- `ShutdownCoordinator` 并行关闭所有 Shell，并以 `close_timeout_ms` 作为整个 shutdown 的全局 cleanup deadline；到期后执行平台强制回收、关闭所有 PTY/fd/HANDLE 并允许进程退出，不能无限等待 reap；
- cleanup 必须幂等。

## 7. 现有实现与外部参考审查

### 7.1 ide4ai：可以直接保留思想与测试

审阅基线：`reference/ide4ai` commit `20ece038e66e13885e77503e217b23766e60dc86`。现有终端和 BashTool 测试共 36 个，全部通过。

| ide4ai 能力 | 结论 | 在 tfbash-mcp 中的用法 |
|---|---|---|
| `pexpect.spawn` + cwd + `echo=False` + `use_poll=True` | 复用 | 作为 Unix PTY 启动基线 |
| 实例级随机 PS1 prompt sentinel | 复用 | 防止命令输出碰撞 prompt 导致失步 |
| 实例级随机退出码定界符 | 复用 | 准确提取 `$?`，不从普通输出猜测 |
| `_to_single_line()` 多行/base64 包装 | 复用并补测试 | 保持 heredoc、多行命令和当前 Shell 状态 |
| per-call timeout | 复用 | 映射到 `shell_exec.timeout_ms`，与 `yield_ms` 分离 |
| Ctrl-C 软恢复 → 重建 Shell 硬恢复 | 复用并增强结果 | 增加 `shell_rebuilt`、保持逻辑 `shell_id` 并提示状态丢失 |
| `StepResult` 结构化结果 | 增强 | 改为协议 DTO，统一使用 `status`、`exit_code` 和 `shell_rebuilt` 等协议字段；信号投递结果与命令终态分开 |
| `clean_output()` | V1 不复用 | 增量 cursor 与 chunk 边界优先；原样保留 ANSI/OSC 文本 |
| 持久 cwd/env/venv 测试 | 复用 | 改写为 MCP 端到端验收测试 |
| prompt 碰撞、退出码、多行、超时恢复测试 | 复用 | 作为回归测试基线 |

### 7.2 ide4ai：需要继续封装或重构

| 缺口 | 当前情况 | 需要增加 |
|---|---|---|
| async 非阻塞 | async BashTool 直接调用同步 `ide.step()` | `ShellWorker`、操作队列、事件等待、取消与 shutdown 协调 |
| 长命令 | `run_in_background` 只写入 metadata | `shell_exec` 超过 `yield_ms` 返回 `running`，原 Execution 继续运行 |
| 持续输入 | 只有 `sendline(command)` | 活动 Execution 的 UTF-8 文本 stdin 和 semantic signal |
| 增量输出 | 命令结束后一次性读取 `shell.before` | per-Execution byte cursor、ring buffer、截断标记、事件等待 |
| Shell Registry | 只有 IDE 内部 terminal list，MCP 不可寻址 | `shell_id`、`exec_id`、list、close、数量与 retention 上限 |
| 并发正确性 | 无 per-shell operation owner | 每个 Shell 一个 PTY owner worker；跨 Shell 并发，同 Shell 单活动 Execution |
| 进程树终止 | 依赖 `pexpect.close(force=True)` | 明确受管 process group/Bash jobs 的 TERM/KILL 升级，并收窄 session-escaping daemon 的承诺 |
| 输出容量 | BashTool 最后按字符截取 30000 | 收集阶段即有界、按 byte 计数、游标截断语义 |
| Shell 重建可观察性 | 重建后只 best-effort 恢复 cwd | 返回 `shell_rebuilt`；保持逻辑 `shell_id`；重新应用初始配置和启动命令；声明临时状态丢失 |
| 退出模型 | `exit_code=-1` 混合多种失败 | `running/exited/timeout/cancelled/shell_error`；信号后的 Bash 返回仍为 `exited`，`eof` 单独表示输出已读完 |
| MCP 返回格式 | `str(dict)` 作为 text content | 稳定 JSON 序列化与 `isError`/结构化错误合同 |
| Server shutdown | `__del__`/`atexit` 为主 | 显式 async lifespan、信号、stdin EOF、幂等 cleanup |

### 7.3 ide4ai：不应带入 tfbash-mcp

- `PythonIDE`、`PyWorkspace`、LSP、Gym action/observation；
- `PyIDESingleton`：一个 Computer 一个 MCP 进程已经提供实例隔离；
- `BaseMCPServer` 对 `IDE` 的抽象依赖；
- `CommandFilterConfig`、PolicyEngine、`dangerously_disable_sandbox`；
- `change_dir()` 的工作区子目录限制；
- Docker、Paramiko、文件编辑和窗口资源；
- SSE/Streamable HTTP 的首期依赖；
- `BashInput.args` 拼接协议：V1 直接接收完整 `command` 字符串，避免 shell quoting 被二次解释；
- 当前 `run_in_background` 字段及说明，因为它没有真实实现。

### 7.4 ide4ai：推荐的复用方式

不建议让 `tfbash-mcp` 在运行时依赖完整 `ide4ai` 包。ide4ai 的依赖包含 Gym、Docker、Paramiko、LSP Workspace 和 HTTP Server，对独立 Bash MCP 过重。

推荐从 MIT 代码中抽取并保留版权/许可证声明，形成最小终端内核：

```text
tfbash_mcp/
├── server.py
├── config.py
├── tools/
│   └── shell.py
└── shell/
    ├── domain/
    │   ├── command_shell.py
    │   ├── command_shell_manager.py
    │   ├── shell_worker.py
    │   ├── execution.py
    │   ├── execution_output_buffer.py
    │   ├── result.py
    │   └── utf8_decoder.py
    └── runtime/
        ├── profile.py
        ├── shell_dialect.py
        ├── pty_transport.py
        ├── process_supervisor.py
        ├── bash_dialect.py
        ├── pexpect_posix_pty.py
        ├── posix_process.py
        ├── powershell_dialect.py
        ├── conpty_windows.py
        └── windows_process.py
```

抽取时以 `PexpectTerminalEnv` 的经过验证逻辑为基线，但不继续继承 `BaseTerminalEnv`，也不通过 `IDE.step()` 中转。

### 7.5 DeepSeek Harness：V1 Windows 分层参考与限制

审阅基线更新为 `reference/deepseek-harness` tag `dsh-v0.1.1-rc.1`、commit `528c682e061696f5a160f363f236ecbf53cbd006`。其最新实现把 owner-scoped `TerminalSessionService`、本地 subprocess/PTY provider、`shellDialect=bash|pwsh` 启动合同和六个模型可见 terminal tools 分层；PowerShell 路径使用 node-pty/ConPTY、受控 prompt/OSC marker、UTF-8 encoding bootstrap、Windows Toolhelp32 进程身份与 `taskkill` 分层回收。

应复用的不是 TypeScript/node-pty 代码，而是边界设计：terminal seam 不持有 tool schema/prompt policy，bash/pwsh 共享 readiness 消费层，平台进程监管由 provider 拥有，模型可见工具单独负责有界结果和使用指导。

其限制同样是本项目的实验重点：Windows 无精确 stdin-wait/真实 POSIX foreground group，SIGINT 实际通过 Ctrl-C 输入交付，TERM/KILL 通过 `taskkill` 近似，受限 PowerShell 可能拒绝 `[Console]::` encoding/prompt bootstrap，关闭后还需额外存活探测防止 node-pty exit event 缺失。因此不能直接照搬其 POSIX signal 名称或宣称 EOF、控制和清理已经等价。

Windows V1 选型阶段评估的候选 Runtime Profile 为：

```text
WindowsPwshProfile
├── PowerShellDialect
├── ConPtyTransport             候选：pywinpty 或经验证的等价实现
└── WindowsProcessSupervisor    候选：Windows Job Object；对比进程枚举/taskkill
```

进入 Windows V1 实现前已执行最小可判别 Phase 0 实验，验证了：

1. cwd/env 持久化、随机定界符、真实退出码、Unicode 和大输出不会导致协议失步；
2. 长命令 yield 后可继续增量读取，读取由事件唤醒而不是固定 sleep-loop；
3. UTF-8 文本 stdin 可持续写入；原始字节 stdin 与 EOF control 未能跨两个 Runtime Profile 等价兑现，因此未进入 V1；
4. interrupt、terminate、kill 能覆盖活动前台进程及受管子孙，并能区分“请求已接受”和 Execution 最终状态；
5. timeout 软恢复、Shell 重建、busy close、Server shutdown 都在 deadline 内完成且不遗留受管进程；
6. 同一组平台无关领域测试可分别运行在 `PosixBashProfile` 和候选 `WindowsPwshProfile` 上，平台专属测试只验证 transport/dialect/supervisor 细节。

实验比较了：A）pywinpty 3.0.5 + ConPTY + Toolhelp/process-creation identity + `taskkill`；B）同一 ConPTY transport + Windows Job Object 所有权与强制回收。Codex/DeepSeek 仅作为行为与分层参考，不作为第三个 Python 候选。

正式 Windows release gate 已按实际生产风险收敛为：在一台新鲜 Windows 11 Client x64 上，针对待发布精确 source commit 执行 1 个完整 native session，并要求 UTF-8/中文/emoji、快速退出尾部、exit code、控制、可观察重建、Job 回收、唯一终态和无跨 Execution 迟到输出等全部 10 项强制检查通过，同时满足 `contract_passed=true`、`decision_ready=true`、`decision=pass`。若同 Shell recovery 失败，只有重建成功且 Job 内零残留才可通过。GitHub-hosted Windows 和额外 1–5 次重复只作持续诊断，不再把固定 20 次作为 release 判定前提。

精确源码 `8e0626536aa1509d5919b1c1cb1a674438f21027` 已在 run `supervisor-gate-20260825T093244Z-0ed74ef3` 通过 1/1 session、10/10 mandatory checks。候选 B（ConPTY + gated bootstrap + non-breakaway kill-on-close Job Object）因此进入 production profile；公共 `shell_write.eof` 因无法跨两个 Profile 等价兑现，已在 schema 冻结前删除。后续任何 production Windows runtime 变更都必须重新对新的精确 source commit 执行同一 release gate。

### 7.6 Codex：native Windows 与 IDE context 参考

审阅基线：`reference/codex` `origin/main` commit `536f86e5cc9ec1ff38457d099bf320b9d08eeeba`。Codex 将 PowerShell/CMD/Bash 等 Shell 方言检测与命令参数构造分离，PTY 层在 Windows 使用 ConPTY，在工具 description 中提供 Windows 专属指引；native Windows 与 WSL 是显式环境选择，不互相做不可见 fallback。

Codex 的 IDE context 通过专用 Unix socket/Windows named pipe 获取 active file、selection 和 open tabs，再由 Agent host 合入 prompt；这条 IPC 与 shell/PTY transport 独立。tfbash-mcp 因此只接受显式 HostConfig/workspace root 并提供 runtime context，不复制 Codex IDE IPC，也不通过终端环境变量推断 IDE。

## 8. tfrobot-client 与 SDK 集成合同

### 8.1 客户端职责

tfrobot-client 只负责：

1. 在每个 Computer 设置中显示“启用 Shell MCP”开关；
2. 持久化该布尔值；
3. 开启时向 SDK 声明/启用固定 Shell MCP descriptor；
4. 关闭时请求 SDK 停止并禁用该 MCP；
5. 展示 SDK 提供的启动中、运行中、失败、已停止状态和诊断。

客户端不得：

- 直接 spawn Python、uvx 或 Shell MCP；
- 持有 pexpect 对象；
- 保存 Shell/Execution Registry；
- 解释 PTY 输出或实现进程树回收；
- 给工具调用注入 `computer_id`；
- 为 Shell MCP 建立专用协议旁路。

### 8.2 SDK 职责

SDK 负责：

- 保存或物化该 Computer 的 stdio MCP 声明；
- 解析运行命令、参数、cwd 和环境；
- 启动、连接、重启、停止和回收 MCP 子进程；
- 提供 runtime status、stderr 诊断和 MCP capability/tool discovery；
- 保证同一 Computer 同一 Bash descriptor 只有一个活动进程；
- Computer shutdown 时停止 MCP；
- 把普通 MCP tool call 和 notification 能力暴露给上层。

现有 rust-sdk 已具备可复用的通用基础：`StdioServerParameters` 已包含 `command/args/env/cwd`，`Computer` 已提供 `mount_server`、`add_or_update_server`、`remove_server`、`start_mcp_client`、`stop_mcp_client`、`get_server_status` 和 inventory 查询。当前主要 SDK 缺口不是进程管理，而是 `McpOwnership` 只有 User/Plugin，尚无适合产品内置能力的 System ownership；长命令使用普通 MCP tool call + `shell_read(wait_ms)` 即可，V1 不要求 SDK 实现专用 Terminal 通道或 MCP Tasks。

建议 descriptor：

```json
{
  "bundle_id": "tfrobot.tfbash-mcp",
  "transport": "stdio",
  "command": "uvx",
  "args": [
    "--from",
    "tfbash-mcp==<pinned-version>",
    "tfbash-mcp",
    "--transport",
    "stdio",
    "--runtime-profile",
    "auto",
    "--host-profile",
    "ide",
    "--workspace-root",
    "<computer-workspace>"
  ]
}
```

正式发布必须固定 package version；不能永远跟随 `latest`。包名和 entry point 在发布前最终确认。

### 8.3 开关行为

| 场景 | 期望行为 |
|---|---|
| 新建 Computer | 使用产品默认值创建开关，不隐式复用其他 Computer 的进程 |
| 开启 | SDK upsert descriptor → enable → start；成功后工具可发现 |
| 关闭 | SDK stop；Server 回收全部 Shell/Execution；descriptor 保留为 disabled 或由 SDK 重建 |
| 应用重启 | 根据开关重新声明并启动新的 MCP；旧 Shell 和 Execution 不恢复 |
| MCP 启动失败 | Computer 其他 MCP 和主连接不应整体失败；单独显示 Shell MCP 诊断 |
| Computer 删除 | SDK 停止 MCP，并清理该 Computer 对应声明和运行状态 |

## 9. 配置

Server 配置只描述自身运行环境：

```text
--transport stdio
--runtime-profile auto|bash|zsh|pwsh
--host-profile standalone|ide
--workspace-root <path>
--default-cwd <optional-path>
--shell <optional-compatible-executable>
--shell-startup-timeout-ms 30000
--command-yield-ms 10000
--command-timeout-ms 120000
--recovery-grace-ms 1000
--job-cleanup-timeout-ms 3000
--output-quiet-ms 50
--max-command-bytes 262144
--max-command-shells 8
--max-retained-executions 128
--output-buffer-bytes 4194304
--max-read-bytes 65536
--max-read-waiters-per-execution 32
--max-write-bytes 65536
--max-pending-operations 128
--max-pending-write-bytes 262144
--completed-retention-ms 600000
--shutdown-grace-ms 3000
--close-timeout-ms 5000
--startup-command <optional>
```

Server 在启动时把上述参数、启动 cwd 和继承环境冻结为 `HostConfig`。`default_cwd` 省略时等于 `workspace_root`；`--shell` 是进程级严格覆盖，失败时不回退，且不会出现在 `shell_open` schema 中。`default_cwd` 和 `startup_command` 可由 `shell_open` 覆盖；`shell_open.startup_command=null` 表示仅为该 Shell 禁用宿主默认 startup command。

`active_venv_cmd` 改名为通用的 `startup_command`；它可以加载 Conda、direnv 或其他需要 Shell 命令的项目环境，不把协议绑定到 Python。标准 Python venv 应由 standalone launcher 或 IDE 解析后，通过 MCP stdio descriptor/process environment 注入 `VIRTUAL_ENV` 并把 venv 的 `bin` 或 `Scripts` 目录预置到 `PATH`/`Path`；这部分环境随 Server 进程继承，不作为可能泄密的 CLI 参数，也不依赖 PowerShell execution policy。Server 不扫描项目寻找虚拟环境。

0.2.0 公开组合级 `--runtime-profile auto|bash|zsh|pwsh`，不公开 backend 参数。方言与 OS 解耦，Server 根据候选程序能力推断并组合 POSIX PTY 或 Windows ConPTY 后端。`auto` 在 macOS 优先系统 zsh，在 Linux 优先系统 Bash，在 Windows 依次尝试稳定 PowerShell Core、Windows PowerShell 5.1 和 Git Bash；显式方言可在任意存在兼容原生实现的平台使用。WSL 明确拒绝。`--host-profile` 只影响 workspace/config 来源、环境初始化责任和诊断元数据。

`--workspace-root` 与显式 `--default-cwd` 必须是启动时存在且可进入的平台原生绝对路径。`standalone` 省略 workspace 时使用 Server 启动 cwd；`ide` 必须由宿主显式提供 workspace。继承的环境值和 startup command 永不回显。V1 不通过 deprecated MCP roots、`TERM_PROGRAM`、VS Code/Cursor 环境变量或当前进程 cwd 的偶然变化推断 IDE workspace。

`shell_startup_timeout_ms` 是 open/rebuild 从 spawn 到 startup 完成的总 deadline；`recovery_grace_ms` 是 timeout 后 soft recovery 上限；`job_cleanup_timeout_ms` 覆盖平台受管子孙枚举、terminate/kill、reap/liveness verification 和 quiet barrier，`output_quiet_ms` 是其中要求的连续无输出窗口；`close_timeout_ms` 是 close 及全局 shutdown cleanup 的硬上限，必须大于 `shutdown_grace_ms`。这些配置都必须为正整数，Server 启动时拒绝不满足关系的配置。

`max_command_bytes` 限制单条命令和启动命令的 UTF-8 编码长度。`output_buffer_bytes` 是单个 Execution buffer 的 Server 级容量上限，`shell_exec.max_output_bytes` 只能在 4096 到该上限之间缩小容量；`max_read_bytes` 限制单次 `shell_read`，`max_read_waiters_per_execution` 限制每个活动 Execution 的阻塞 read 数，因此全局 waiter 数还受到 `max_command_shells` 的乘积约束。`max_write_bytes` 限制单次 `text` 的 UTF-8 编码字节数；`max_pending_operations` 限制每个 Shell 尚未完成的 write/signal 总数，`max_pending_write_bytes` 限制其中尚未投递到 PTY 的 input payload 总字节数，write 分片投递、操作失败或取消时必须相应释放预留容量。close 和内部 recovery 使用不受该配额阻塞的保留控制通道，保证洪泛或 PTY backpressure 时仍可清理。`max_retained_executions` 是 Server 全局已完成记录上限，与 `completed_retention_ms` 共同按 5.4 的顺序淘汰。

## 10. 验收标准

### 10.1 通用 MCP

- 可通过标准 stdio MCP Client 独立启动、initialize、list_tools 和 call_tool；
- 不导入 tfrobot-client 或 A2C-SMCP Computer 类型；
- 删除或替换 Python 实现时工具 schema 可以保持兼容；
- 工具入参不含 Computer 标识。
- 七个工具拒绝缺失必填字段、未知字段、非法 `null`、类型错误和范围越界；`shell_write` 缺少 `text` 或携带 `data_base64`、`eof` 时必须拒绝；所有成功与错误响应符合 5.x 的字段表和矩阵。
- standalone 与 IDE 启动相同 Server binary；在相同 Runtime Profile 和 workspace 下产生相同 Shell 行为。
- standalone/IDE 可通过不同 `HostConfig` 设置默认 cwd、继承环境和 startup command；IDE 提供标准 Python venv 时使用 `VIRTUAL_ENV` + `PATH`/`Path`，Server 不扫描环境或依赖 `Activate.ps1`。
- Agent 在首次构造命令前可从 instructions/tool description 获知方言，并可从 `shell_list.runtime/host` 获取权威平台、方言、workspace 和默认 cwd；任何 env value、startup command、解释器绝对路径或 secret 均不可见。

### 10.2 多持久 Command Shell

- 可以创建多个 Shell，获得不同 `shell_id`，并通过 list、close 管理其生命周期；
- 对同一 `shell_id` 连续执行方言原生命令可以证明 cwd/env 保持：Bash 使用 `cd/export/pwd`，PowerShell 使用 `Set-Location/$env:/Get-Location`；
- 不同 Shell 的 cwd/env 互不影响，关闭后的 `shell_id` 不能继续使用；
- 返回真实退出码，输出中的随机数字、ANSI/OSC 或 prompt 相似文本不能污染退出码；
- 多行命令和 heredoc 不截断、不失步；
- 同一 Shell 同时只运行一个 Execution，busy 时拒绝新的 `shell_exec`，不同 Shell 可以并行推进；
- 超时命令不会毒死该 Shell 的后续调用；若自动重建 Shell，逻辑 `shell_id` 保持不变且结果明确标记；
- 自动重建失败后 Shell 进入 `error`，不能执行命令但仍可 close；
- 并发创建 Shell 时也不能突破数量上限；达到上限后拒绝继续创建，关闭 Shell 后容量可被复用。

### 10.3 短命令与长命令

- 短命令在 `yield_ms` 内结束时由 `shell_exec` 一次返回输出、真实退出码、cwd 和 `eof=true`；
- 长命令超过 `yield_ms` 时由同一个 `shell_exec` 返回 `running`、`exec_id`、已有输出和 cursor，命令继续在原 Shell 中运行；
- `yield_ms` 到期不会终止命令，只有 `timeout_ms` 到期才触发超时恢复；
- `shell_read` 可从 cursor 增量观察 `running` → `exited/timeout/cancelled/shell_error`，并在新输出或状态变化时由事件唤醒；
- 用户命令退出后的 internal finalizing 对外仍是 `running`，但 write/signal 返回 `exec_not_active`；queued input 被丢弃且不能进入 cleanup 或下一 Execution；
- cursor 落后于 ring buffer 时返回截断标记；Execution 完成后在 retention 与数量上限内仍能读取结果；
- 同一个 Shell 的连续 Execution 使用不同 `exec_id`，输出不会串流。
- Runtime Profile 所捕获的后台子孙会在 Shell 恢复 `ready` 前被清理，迟到输出仍归入原 Execution；POSIX `disown`/`setsid` 和 Windows 脱离受管树等 escape 明确按不支持合同处理。

### 10.4 输入、信号与输出控制

- `shell_write` 只接受 UTF-8 `text` 并向活动 Execution 写入其编码字节；V1 不提供任意二进制 stdin 或 EOF control；
- `shell_signal` 可向活动 Execution 发送 `interrupt`、`terminate` 或 `kill`，所有受支持方言/原生后端组合均满足相同领域结果；
- write/signal 携带错误、过期或非活动 `exec_id` 时不会影响当前命令；
- 单次 write 和 per-Shell pending operation/input bytes 都受配置上限约束；并发洪泛超限时原子返回 `resource_limit`，出队、失败或 close 后容量可复用；
- `shell_close` 可关闭 busy Shell，并终止 Runtime Profile 捕获的受管执行树；V1 不承诺回收显式逃离平台所有权边界的 daemon；
- `shell_read(wait_ms)` 使用事件等待，不通过固定 sleep-loop 反复探测；
- 同一 Execution 的阻塞 read waiter 不超过配置上限，超限返回 `resource_limit`，完成或取消后 slot 可复用；
- V1 不要求 raw VT 字节流、方向键语义、resize 或全屏 TUI 渲染。

### 10.5 生命周期

- 在 5.3 定义的 V1 受管进程边界内，宿主终止、正常 shutdown 和异常取消都不会遗留 Command Shell 或受管子孙；
- cleanup 可重复调用；
- open、timeout recovery、job cleanup/quiet、close 和 shutdown 都在配置的总 deadline 内结束；close 超限有界返回 `cleanup_complete=false`，Server shutdown 不等待无界 reap；
- SDK 对同一 Computer 不会重复启动两个相同 Shell MCP；
- 开关关闭后工具不可用，重新开启得到全新 Server，以及全新的 Shell 和 Execution 状态。

## 11. 测试要求

1. 迁移 ide4ai 的 prompt、退出码、cwd/env、venv、多行、heredoc、超时恢复和 EOF 测试，作为 POSIX 基线；
2. 新增多 Command Shell 的创建、寻址、状态隔离、同 Shell 单活动 Execution、跨 Shell 并发、close 与等待中 exec/read 及已接受 write/signal 的线性化、close cancellation CAS 前后自然退出两种强制 interleaving、自动重建失败、并发创建数量上限、close deadline 的 `cleanup_complete=false` 和全局 shutdown deadline 测试；
3. 新增短命令终态 snapshot、超大短命令截断、长命令 yield 后继续运行、`yield_ms`/`timeout_ms` 分离、exec_id 隔离、cursor/ring buffer 淘汰、伪造/过期/UTF-8 中间 cursor、UTF-8/ANSI 跨 chunk、非法 UTF-8 在 output EOF 时 decoder flush、事件等待、阻塞 read waiter 配额/释放、UTF-8 文本 stdin、semantic control、平台后台子孙 cleanup、cleanup/quiet deadline、所有权 escape、TTL 与数量上限同时触发的测试；
4. 为七工具 schema/error contract 增加缺字段、未知字段、非法 null、平台原生 path/env/command 边界字段中的 U+0000、Windows env key 大小写冲突、类型/范围边界、`shell_write.text` UTF-8 字节上限、已移除 `shell_open.shell`/`data_base64`/`eof` 的拒绝行为、每种响应 union 和 retryable 映射测试；增加 finalizing gate 拒绝 write/signal 且输入不跨 Execution、write 单次上限、pending ops/bytes 洪泛、原子拒绝、PTY 长时间不可写时 signal/close/shutdown 仍可推进、未投递尾部释放及 close 后容量复用测试；
5. 使用真实 pexpect/PTY 做集成测试，不能把承载 PTY 契约的一层全部 mock 掉；
6. 使用标准 MCP stdio client 做端到端测试；
7. 在 macOS、Linux 和 Windows 11 x64 CI 分别执行真实 PTY 集成矩阵；Windows 关键进程树/控制场景还需至少一个真实 native runner 复核；
8. 增加 tfrobot-client + SDK 集成测试，验证开关、descriptor、启动状态和停止回收；
9. 终端 UI 若后续接入，先单独评审 Terminal mode 的真实场景、raw byte 与 resize 合同，不建立平行 Registry；
10. Shell Domain 测试使用 Runtime Port test double 验证平台无关状态机；另加依赖边界检查，禁止 `shell/domain` 导入 pexpect、pywinpty 或平台进程 API。承载真实 PTY、控制和进程树语义的测试必须覆盖 POSIX 与 Windows 原生后端及主要方言组合，不能以 test double 代替；
11. standalone/IDE 组合测试验证同一 binary、HostConfig 优先级、显式 workspace/default cwd、标准 Python venv 的 `VIRTUAL_ENV` + `PATH`/`Path` 注入、Conda/custom startup command 的方言适配与 rebuild 重放、初始化失败清理、runtime instructions、动态工具 description 和 `shell_list.runtime/host` 脱敏摘要；并验证 Server 不扫描 `.venv`/Poetry/Conda、不依赖 `Activate.ps1`、MCP roots 或编辑器标识环境变量，且不回显 env value/startup command/secret。

## 12. 实施顺序

1. Phase 0 在真实 Windows 11 x64 上对比 ConPTY transport 与 Windows ProcessSupervisor 候选，验证 Unicode、快速退出尾部、control、EOF、进程树和 shutdown；
2. 根据实验结果冻结第 5 节七工具字段、协议 DTO、错误合同以及 Shell/Execution 平台无关状态机；
3. 定义最小 `ShellDialect`、`PtyTransport`、`ProcessSupervisor` Runtime Ports，并由 composition root 独立组合方言与宿主原生终端后端；
4. 从 ide4ai 抽取 `BashDialect`、`PexpectPosixPtyTransport` 所需的最小逻辑和既有回归测试；
5. 参考 DeepSeek/Codex 的边界实现 `PowerShellDialect`、`ConPtyTransport` 与 `WindowsProcessSupervisor`，不复制其模型工具合同；
6. 建立独立 Shell MCP entry point、HostConfig、runtime context、`CommandShellManager` 和显式 `shell_id` 工具合同；
7. 实现 `ShellWorker`、per-Execution byte ring buffer、事件通知和 `exec_id`；
8. 实现 `shell_exec` 的短命令一次返回与长命令 yield 闭环；
9. 实现 read、write、semantic control、close、两个 ProcessSupervisor 和统一 shutdown；
10. 完成 Runtime Port 合同测试、真实 POSIX/Windows PTY 集成测试、standalone/IDE 组合测试和 stdio MCP 端到端测试；
11. 发布固定版本的 uv 包；
12. 在 SDK 中增加 System ownership，并接入受管 descriptor；
13. 在 tfrobot-client 增加每 Computer 开关和状态展示；
14. 有明确使用方后再评审 Terminal mode 或 MCP Tasks 集成。

## 13. 关键决策摘要

| 决策项 | 结论 |
|---|---|
| Shell 实现放在哪里 | `tfbash-mcp` 独立 MCP Server；当前包名是历史命名，不限制 PowerShell Profile |
| tfrobot-client 是否内置执行引擎 | 否，只保留开关和状态 |
| 谁拉起进程 | standalone MCP Client 或 IDE/A2C-SMCP SDK |
| 是否一进程服务多个 Computer | 否，一个 Computer 一个 MCP 进程 |
| 工具是否携带 Computer ID | 否 |
| V1 实现语言 | Python，但 MCP 合同语言无关 |
| 0.2.0 平台范围 | macOS/Linux：Bash、Zsh、PowerShell Core + POSIX PTY；Windows 11 x64：PowerShell 5.1/Core、Git Bash、显式 MSYS2 Zsh + native ConPTY |
| 七工具合同 | 工具名、字段与领域模型已冻结；`shell_write` 仅含必填 `shell_id`、`exec_id`、`text`，任意二进制 stdin 与 EOF control 已在 V1 冻结前删除 |
| 平台分层 | Shell Domain 只依赖 `ShellDialect`、`PtyTransport`、`ProcessSupervisor` Runtime Ports |
| Runtime Profile 选择 | 0.2.0 启动时选择 `auto|bash|zsh|pwsh`，一个进程只使用一个方言与原生后端组合 |
| Windows 路线 | V1 已选定 pywinpty/ConPTY + Job Object 受管进程树，并已通过 Windows 11 原生 gate 冻结合同 |
| Host Profile | `standalone|ide` 共用 Server 与七工具；以不可变 HostConfig 表达 workspace/default cwd、宿主环境和 startup command 差异 |
| 环境初始化 | IDE/launcher 负责解析；标准 Python venv 注入 `VIRTUAL_ENV` + `PATH`/`Path`，Conda/custom 可用方言专属 startup command；Server 不扫描项目环境 |
| Agent 感知 | instructions + 动态工具描述 + `shell_list.runtime/host` 脱敏摘要；不暴露 env/startup command/secret，resource 仅作补充，不依赖 MCP roots |
| ide4ai 复用方式 | 抽取 pexpect 核心与测试，不依赖完整 IDE4AI |
| 持久 Command Shell 数量 | 每个 Server 可创建多个，由 `shell_id` 显式寻址并受数量上限约束 |
| 短/长命令 | 统一使用 `shell_exec`；超过 `yield_ms` 返回 `running` 和 `exec_id` |
| 后台 Job 工具 | 不提供；长命令保持为原 Shell 的活动 Execution |
| 交互终端 | V1 不提供独立 Terminal Registry/tool group；有明确场景后在同一 Shell 模型增加 mode |
| approval/sandbox/命令策略 | 不提供 |
| Shell/Execution 持久范围 | MCP 进程内；不跨 Server 重启 |
