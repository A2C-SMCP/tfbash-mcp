# tfbash-mcp

面向 Agent 系统的基础 MCP 工具集合。项目希望为不同 Agent 提供一组稳定、可组合、低心智负担的通用能力，例如命令执行、文件读写与检索；具体能力优先复用成熟实现，只有现有方案无法满足核心约束时才考虑适配或自研。

> 当前状态：macOS/Linux 与 Windows 11 x64 均已提供七个持久 Shell 工具及 stdio
> MCP Server。Windows Runtime 使用 PowerShell 7.6、native ConPTY 和 gated-bootstrap
> Job Object 进程所有权。

## 为什么做这个项目

Agent 经常需要离开纯文本推理，操作真实工作区或调用本机程序。程序员会把 Shell 作为主要工作界面；产品、运营、研究等通用岗位也会通过 Shell 调用脚本、转换文件、批处理数据或诊断环境。

现有 MCP 工具在能力范围和运行模型上差异很大：有的只支持一次性命令，有的依赖 PTY 或常驻守护进程，有的内置审批和沙箱，有的缺少可靠的长命令、增量输出与受管进程清理。tfbash-mcp 先定义真实场景和可验收的能力合同，再决定直接采用、包装适配还是自研。

## 设计原则

- **先场景，后选型**：不因熟悉某种语言、框架或现有项目而倒推需求。
- **优先复用**：选型顺序是 Adopt → Wrap → Build；复用必须以满足关键行为为前提。
- **小而正交**：基础工具保持清晰边界，避免把 Agent 编排、业务工作流或 UI 塞进工具层。
- **显式状态**：长任务、持久 Shell、输出游标、退出状态和资源生命周期都应可观察；需要保留 cwd/env 的 Shell 必须显式创建和寻址。
- **协议与实现语言无关**：工具合同不暴露 Python、pexpect、Computer 或客户端内部概念；V1 使用 Python + pexpect 和 stdio 传输。
- **信任部署环境**：第一阶段不内置 approval、sandbox、命令策略或目录边界。
- **可控资源**：内存、磁盘、会话、进程和临时文件必须有明确上限、所有权与清理规则。

## 第一阶段：Bash 工具

第一阶段聚焦 Unix 环境（Linux、macOS）下的独立通用 Bash MCP Server。目标同时覆盖：

- 程序员的构建、测试、代码检索、服务启动和日志观察；
- 通用岗位通过已安装 CLI 或脚本完成文件处理、数据转换和环境诊断；
- 多个可寻址的持久 Command Shell 分别保留 cwd、环境和激活状态，并为每条命令返回准确退出状态；
- pexpect PTY 的 stdout/stderr 作为 combined output 返回，并为每次执行维护有界增量输出；
- 短命令由 `shell_exec` 一次返回；长命令超过 `yield_ms` 后返回 `running`，继续在原 Shell 中执行；
- 长命令通过 `shell_read` 增量读取输出，通过 `shell_write` 写入 stdin，通过 `shell_signal` 中断或终止；
- 持久 Command Shell 支持创建、执行、增量读取、写入、信号、列举和关闭，不再暴露独立的后台 Job 与 Terminal 工具组；
- Shell 和 Execution 归属于 MCP 服务进程，跨工具调用保持，但不承诺跨服务重启恢复；
- 一个 tfrobot-client Computer 对应一个由 SDK 管理的 MCP 进程，客户端只保留启用开关和运行状态。

第一阶段的持久 Shell 会使用 PTY，但只提供命令导向的文本流，不提供全屏 TUI 屏幕模型、原始终端 Session 或 resize。后续若出现 xterm.js、REPL 或全屏 TUI 的明确集成方，再在同一 Shell 资源模型上增加 Terminal mode。同时不包含命令审批、进程沙箱、跨服务重启恢复，以及与 MCP Tasks 的强绑定。

相关文档：

- [通用 Bash MCP Server 需求与架构说明](docs/bash-tool-requirements.md)：定义独立 MCP 边界、工具合同、ide4ai 复用范围、SDK/客户端职责和验收标准。

## 规划中的基础能力

当前路线只表达探索顺序，不代表已经决定自研：

1. 持久 Shell 与长命令 Execution；
2. 文件读取、写入和补丁式修改；
3. 文件发现与文本检索；
4. 基于真实 Agent 工作流补充的其他小型基础工具。

每项能力都应先形成独立需求和候选调研，并记录 Adopt、Wrap 或 Build 的决策依据。不同能力可以采用不同来源，只要对 Agent 暴露的错误模型、生命周期和结果结构保持一致。

## 仓库结构

```text
.
├── .github/workflows/tests.yml  # Python 3.10–3.12 CI
├── pyproject.toml               # 包元数据、依赖与工具配置
├── src/tfbash_mcp/              # MCP Server 包与入口
├── tests/                       # 自动化测试
├── README.md
├── docs/
│   └── bash-tool-requirements.md
└── reference/
    ├── ide4ai/                   # pexpect 持久 Shell 基线
    ├── codex/                    # 进程生命周期参考
    ├── pi/                       # Agent Shell 参考
    └── deepseek-harness/         # Agent Runtime 参考
```

`reference/` 是本地调研检出目录，体量较大且包含多个第三方 Git 仓库，因此不会纳入
`tfbash-mcp` 的版本控制或发行包。

## 本地开发

项目支持 Python 3.10–3.12，并使用 [uv](https://docs.astral.sh/uv/) 管理环境：

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
```

启动 stdio Server：

```bash
uv run tfbash-mcp
```

使用 MCP Inspector 2.2 从当前本地源码启动并检查工具 discovery：

```bash
npx -y @modelcontextprotocol/inspector@2.2.0 --web \
  uv \
  --directory "$PWD" \
  run tfbash-mcp \
  --runtime-profile auto \
  --host-profile ide \
  --workspace-root "$PWD"
```

该命令必须在包含最终实现的分支或 worktree 中执行。连接后运行 `Tools` →
`List Tools`，应恰好显示 `shell_open`、`shell_exec`、`shell_read`、`shell_write`、
`shell_signal`、`shell_list` 和 `shell_close`。Server 默认使用 stdio，无需再传一个
可能被 Inspector 当成自身选项的 `--transport stdio`。

Server 注册 `shell_open`、`shell_exec`、`shell_read`、`shell_write`、
`shell_signal`、`shell_list` 和 `shell_close`。默认使用当前目录作为 workspace root 和
default cwd，并按宿主系统自动选择 Runtime Profile。IDE 集成应显式传入工作区：

```bash
uv run tfbash-mcp \
  --host-profile ide \
  --workspace-root /absolute/path/to/workspace
```

运行参数可通过 `uv run tfbash-mcp --help` 查看。配置优先级为单次 `shell_open` 参数、
进程级 CLI/HostConfig、Runtime Profile 默认值。继承的环境变量只用于启动 Shell；
`shell_list` 仅返回环境类型和可选名称，不返回环境变量名、值、启动命令或其他密钥材料。

服务在 stdin EOF、客户端断开或取消时走同一个有界 shutdown 路径，关闭全部 Shell 及其
受管进程。`auto` 在 macOS/Linux 选择完整的 `PosixBashProfile`，在 Windows 11 x64
选择完整的 `WindowsPwshProfile`；不会按单次工具调用混用方言或 transport。

## 参与决策

V1 的多 Shell + Execution 工具模型和双 Runtime Profile 已实现并通过验收。后续协议或
Runtime 变更仍须：

1. 对齐文档中的硬性门槛和未决项；
2. 对受影响的真实 PTY/ConPTY 路径执行可复现实验；
3. 确认 ide4ai 代码抽取的许可证保留方式、适配成本、长期维护面和退出策略；
4. 把实验结果和最终实现偏差回写到 RFC，再进入编码与验收。
