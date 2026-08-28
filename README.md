# tfbash-mcp

面向 Agent 系统的基础 MCP 工具集合。项目希望为不同 Agent 提供一组稳定、可组合、低心智负担的通用能力，例如命令执行、文件读写与检索；具体能力优先复用成熟实现，只有现有方案无法满足核心约束时才考虑适配或自研。

> 当前版本：`0.2.0`。macOS、Linux 与 Windows 11 x64 均提供七个持久 Shell 工具及
> stdio MCP Server，并可作为 Python 运行时嵌入 IDE/SDK 宿主。两种入口共享同一套七工具
> 合同和 Shell Domain。运行时会发现并探测 Bash、Zsh 或 PowerShell，再与 POSIX PTY
> 或 Windows ConPTY/Job Object 原生后端组合。

## 为什么做这个项目

Agent 经常需要离开纯文本推理，操作真实工作区或调用本机程序。程序员会把 Shell 作为主要工作界面；产品、运营、研究等通用岗位也会通过 Shell 调用脚本、转换文件、批处理数据或诊断环境。

现有 MCP 工具在能力范围和运行模型上差异很大：有的只支持一次性命令，有的依赖 PTY 或常驻守护进程，有的内置审批和沙箱，有的缺少可靠的长命令、增量输出与受管进程清理。tfbash-mcp 先定义真实场景和可验收的能力合同，再决定直接采用、包装适配还是自研。

## 设计原则

- **先场景，后选型**：不因熟悉某种语言、框架或现有项目而倒推需求。
- **优先复用**：选型顺序是 Adopt → Wrap → Build；复用必须以满足关键行为为前提。
- **小而正交**：基础工具保持清晰边界，避免把 Agent 编排、业务工作流或 UI 塞进工具层。
- **显式状态**：长任务、持久 Shell、输出游标、退出状态和资源生命周期都应可观察；需要保留 cwd/env 的 Shell 必须显式创建和寻址。
- **协议与实现语言无关**：工具合同不暴露 Python、pexpect、Computer 或客户端内部概念；V1 使用 Python + pexpect，默认提供 stdio adapter，也支持 Python 宿主进程内注册。
- **信任部署环境**：第一阶段不内置 approval、sandbox、命令策略或目录边界。
- **可控资源**：内存、磁盘、会话、进程和临时文件必须有明确上限、所有权与清理规则。

## 持久 Shell 工具

当前实现覆盖 macOS、Linux 和 Windows 的共享 Shell Runtime，并提供独立 stdio MCP Server 与 Python 嵌入入口。目标同时覆盖：

- 程序员的构建、测试、代码检索、服务启动和日志观察；
- 通用岗位通过已安装 CLI 或脚本完成文件处理、数据转换和环境诊断；
- 多个可寻址的持久 Command Shell 分别保留 cwd、环境和激活状态，并为每条命令返回准确退出状态；
- pexpect PTY 的 stdout/stderr 作为 combined output 返回，并为每次执行维护有界增量输出；
- 短命令由 `shell_exec` 一次返回；长命令超过 `yield_ms` 后返回 `running`，继续在原 Shell 中执行；
- 长命令通过 `shell_read` 增量读取输出，通过 `shell_write` 写入 stdin，通过 `shell_signal` 中断或终止；
- 持久 Command Shell 支持创建、执行、增量读取、写入、信号、列举和关闭，不再暴露独立的后台 Job 与 Terminal 工具组；
- Shell 和 Execution 归属于具体 Runtime 实例，跨工具调用保持，但不承诺跨实例重建恢复；
- 一个 tfrobot-client Computer 对应一个由 SDK 管理的 stdio 进程或嵌入运行时实例，客户端只保留启用开关和运行状态。

第一阶段的持久 Shell 会使用 PTY，但只提供命令导向的文本流，不提供全屏 TUI 屏幕模型、原始终端 Session 或 resize。后续若出现 xterm.js、REPL 或全屏 TUI 的明确集成方，再在同一 Shell 资源模型上增加 Terminal mode。同时不包含命令审批、进程沙箱、跨服务重启恢复，以及与 MCP Tasks 的强绑定。

相关文档：

- [通用 Shell MCP Server 与嵌入运行时需求及架构说明](docs/bash-tool-requirements.md)：定义共享 Runtime、stdio/宿主 adapter 边界、工具合同、ide4ai 复用范围、SDK/客户端职责和验收标准。

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

## CI、PR 合并与 PyPI 发布

Pull Request 和 `main` 分支使用同一套 `Tests` 质量门禁，在 Python 3.10、3.11、3.12
上执行 `ruff check`、`ruff format --check`、`mypy` 和完整测试。为确保检查失败时不能合并，
仓库管理员需要在 GitHub 的 `main` branch ruleset 中启用 **Require a pull request before
merging** 和 **Require status checks to pass**，并把 `Python 3.10`、`Python 3.11`、
`Python 3.12` 三个检查设为 required。

PyPI 发布由 [publish workflow](.github/workflows/publish.yml) 完成。它只在 GitHub Release
进入 `published` 状态时触发，并先复用上述完整质量门禁；全部通过后才校验 release tag、
构建并验证 wheel/sdist、安装 wheel 做 smoke test，最后通过 PyPI Trusted Publishing 上传。
发布 Job 使用 GitHub OIDC 短期凭证，仓库不保存 PyPI API Token。

首次发布前需要完成一次配置：

1. 在 GitHub 仓库 **Settings → Environments** 创建名为 `pypi` 的 Environment；建议增加
   required reviewers，并将 deployment branches/tags 限制为 release tags。
2. `tfbash-mcp` 尚未存在于 PyPI 时，在 PyPI 的 **Publishing** 页面创建 pending Trusted
   Publisher；项目名填写 `tfbash-mcp`，Owner 填写 `A2C-SMCP`，Repository 填写
   `tfbash-mcp`，Workflow name 填写 `publish.yml`，Environment 填写 `pypi`。首次成功发布
   后 pending publisher 会自动成为该项目的正式 publisher。
3. 按上一段配置 GitHub `main` branch ruleset，确保 PR 不能绕过质量门禁合并。

每次发布都必须先在 PR 中更新 `pyproject.toml` 的版本并通过 required checks。合并到
`main` 后创建与版本完全一致的 `v<version>` GitHub Release，例如：

```bash
gh release create v0.2.0 --target main --generate-notes
```

Release tag 与 `pyproject.toml` 版本不一致，或 tag 指向的提交不属于 `main` 时，发布会在
上传 PyPI 前失败。PyPI 上的版本文件不可覆盖，因此每次重发都必须使用新版本号。

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
可能被 Inspector 当成自身选项的 `--transport stdio`。选择 `shell_write` 时，Inspector
应只显示必填的 `shell_id`、`exec_id` 和 `text` 三个输入字段。

Server 注册 `shell_open`、`shell_exec`、`shell_read`、`shell_write`、
`shell_signal`、`shell_list` 和 `shell_close`。默认使用当前目录作为 workspace root 和
default cwd，并按宿主系统自动选择 Runtime Profile。IDE 集成应显式传入工作区：

```bash
uv run tfbash-mcp \
  --host-profile ide \
  --workspace-root /absolute/path/to/workspace
```

IDE4AI 等 Python 宿主也可以不启动子进程，直接创建进程内运行时：

```python
from tfbash_mcp import EmbeddedShellConfig, EmbeddedShellRuntime

config = EmbeddedShellConfig(
    workspace_root="/absolute/path/to/workspace",
    environment=project_environment,
)

async with await EmbeddedShellRuntime.create(config) as runtime:
    tools = runtime.list_tools()
    opened = await runtime.call_tool("shell_open")
```

`EmbeddedShellConfig` 会复制环境映射；修改原字典不会改变已经创建的配置。初始化、工具调用
和关闭均通过异步 API 执行，不阻塞宿主事件循环。每个 `EmbeddedShellRuntime` 拥有独立的
Shell/Execution Registry；IDE4AI 负责先按项目选择对应实例，再注册 `list_tools()` 返回的
工具。嵌入 API 本身不创建第二个 MCP Server，也不注册 Shell Overview Resource。需要跨实例
限制线程数时，宿主可以把同一个 `ToolConcurrencyBudget` 传给多个 `create()` 调用；这些
实例必须运行在同一个 AnyIO backend 和宿主事件循环中，budget 不支持跨线程、跨事件循环
或跨 asyncio/Trio backend 共享。

`aclose()` 可并发、幂等调用；清理失败会向调用方报错并允许重试。运行时进入关闭状态后拒绝
新工具调用。工具调用被取消时等待方立即返回，底层工作线程按既有
`abandon_on_cancel=True` 语义退出等待，并由 Shell 生命周期/关闭流程完成资源回收。

Server 同时暴露 `window://io.github.a2c-smcp.tfbash/shell-overview` Markdown Resource，
供 A2C-SMCP Desktop 展示当前所有 Shell 的 ID、状态、cwd、最近 Execution 状态和末尾
500 个 Unicode 字符输出。该 Resource 支持订阅，并在 Shell 生命周期、Execution 状态或
输出变化时通过事件驱动的 `ResourceUpdatedNotification` 刷新；它是上下文补充，不替代
`shell_list`。七个工具还通过 `Tool._meta.a2c_tool_meta.tags` 声明 `BuildIn` 和对应 CRUD
能力标签，Computer 的更高优先级配置仍可按 A2C-SMCP v0.4.0 合并规则覆盖。

运行参数可通过 `uv run tfbash-mcp --help` 查看。`--runtime-profile` 仅接受
`auto|bash|zsh|pwsh`。`--shell` 是 stdio Runtime 实例级严格覆盖：只探测该程序，不成功时不回退；
`shell_open` 不再接受 `shell` 字段，因此一个 Runtime 实例内所有 Shell 始终使用同一版本。
继承的环境变量只用于启动 Shell；
`shell_list` 仅返回 Runtime、Host 和 Shell 状态，不返回环境变量名、值、启动命令或其他密钥材料。

服务在 stdin EOF、客户端断开或取消时走同一个有界 shutdown 路径，关闭全部 Shell 及其
受管进程。`auto` 的候选顺序为：macOS 系统 zsh → 其他 zsh → 系统 Bash → 其他
Bash；Linux 系统 Bash → 其他 Bash → 系统 zsh → 其他 zsh；Windows 稳定版
PowerShell Core → Windows PowerShell 5.1 → Git Bash。显式 `bash|zsh|pwsh` 可在任意
拥有兼容原生实现的平台使用。Windows Bash/Zsh 使用 MSYS 路径语义执行命令，但 MCP cwd
字段仍返回 `C:/...` Windows 路径。WSL 不在 `0.2.0` 范围内，传入 WSL 程序会得到明确错误。

Zsh 不引入新的 Python 包；Bash、Zsh、PowerShell 与 Git Bash/MSYS2 都是可选的系统
程序。使用 `uvx` 时仍只需满足项目声明的 Python/uv 与对应平台依赖；Windows 的 ConPTY
实现继续由条件依赖 `pywinpty` 提供。

## 参与决策

0.2.0 的多 Shell + Execution 工具模型和跨平台方言/后端组合已实现。后续协议或
Runtime 变更仍须：

1. 对齐文档中的硬性门槛和未决项；
2. 对受影响的真实 PTY/ConPTY 路径执行可复现实验；
3. 确认 ide4ai 代码抽取的许可证保留方式、适配成本、长期维护面和退出策略；
4. 把实验结果和最终实现偏差回写到 RFC，再进入编码与验收。
