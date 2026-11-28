# AGY Codex (v0.3.0)

**面向 Google Antigravity 的统一双模型 Agent：Gemini 负责意图理解与任务推进，OpenAI Codex CLI 承担自主工程实施。**

AGY Codex 是一个 **Antigravity 原生插件 + 可选主 Agent**：
- **默认轻量原生路径**：通过共享 Runtime (`user_cli.py`) 直接驱动 Codex App Server / CLI，支持 **Git 与非 Git 工作区**、原生 Thread 增量续跑（`--resume`）、运行期 `steer`/`queue`/`interrupt` 实时调控与 0 额外模型调用的离线状态查询。
- **按需严格契约模式（Strict Workflow）**：面向高保障代码改动、显式审计或有序计划（`PLAN.md`），提供 `allowed_paths` 字节级基线、独立验收测试与信息隔离的只读双轴复审（插件内置 [agy-codex-reviewer.md](agents/agy-codex-reviewer.md)）。
- **零外部依赖**：全部脚本仅使用 Python 3.10+ 标准库，无需后台常驻守护进程或额外数据库。

---

## 核心特性

- **意图中介与自主推进**：Gemini 作为唯一用户沟通入口，将模糊需求/体验反馈转化为工程目标，自主消化常规决策与伪阻塞纠偏，避免反复催促“继续”。
- **认知独立与双向纠偏**：共享事实而非偏见；Codex 独立核验源码与测试并主动纠正不严谨假设；独立只读审查默认隔离 Worker 自述（仅按需读取文件路径），避免锚定效应。
- **Git 与非 Git 通用运行时**：普通工程目录、中文路径、文档/资料目录均可直接使用；所有运行状态与证据保存在 `~/.gemini/config/agy-codex/workspaces/`（或 Git 仓库 `<git-dir>/superagent/`），绝不污染用户交付目录。
- **原生 Thread 上下文复用**：连续关联任务通过 `--resume last / RUN_ID / THREAD_ID` 复用同一 Codex Thread，避免重复探索与反复解析原始资料；内置 `models` 目录 TTL 缓存（`--refresh` 强制刷新）。
- **实时调控与安全进程管束**：运行期支持无锁 Maildir Inbox（`steer` / `queue` / `interrupt`）；Windows 下通过 Job Object (`KILL_ON_JOB_CLOSE`) 与启动门控管束完整子进程树，取消即清理并释放写租约。
- **可选 Strict 契约模式**：支持脏工作树（`files-v1` 字节基线）、`allowed_paths` 范围控制、独立验收与有限有序计划（`T-01 → T-02 → ...`）断点恢复。

## 架构概览

```text
用户 (自然语言 · 目标 · 体验反馈)
  │
  ▼
AGY 主 Agent (agents/agy-codex.md · Gemini)
  │  意图中介 · 选档路由 · 双向纠偏 · 自主续跑 · 综合交付
  ├─► 默认轻量路径 (user_cli.py: run / status / inbox / models / doctor)
  │     └─► 共享 Runtime (runtime.py) ── Git & 非 Git · 写租约互斥 · 原生 Thread 续跑 · 实时 Inbox
  │           └─► Codex CLI (App Server 优先 / exec 兜底)
  │
  └─► 按需严格模式 (/run-workflow: agy_workflow.py / agy_adapter.py)
        ├─► plans.py / controller.py ── 字节级基线 · 独立测试 · Diff 证据
        └─► 独立只读 Reviewer (agents/agy-codex-reviewer.md，可配置)
```

---

## 环境要求

- **Antigravity**（支持 Plugins、Custom Main Agent、Skills 与 `/run-workflow`）
- **Git**
- **Python 3.10+**（运行安装器与独立诊断 / 冒烟 CLI；仅使用标准库，无需 `pip install`）
- **Codex CLI**（已安装并完成 `codex login`，支持 `exec`、`exec resume`、`--json`、`--output-last-message`、`--output-schema`）

Antigravity 的 `/run-workflow` 默认使用宿主自带的 Python，控制器沿用该解释器。如果需要指定解释器，可按[官方工作流文档](https://antigravity.google/docs/dynamic-workflows/)设置 `ANTIGRAVITY_PYTHON`，并为本插件选择 Python 3.10+。

---

## 安装与启用

### 方式一：Python 安装器（推荐，跨平台）

```bash
git clone https://github.com/Kylinghu/agy-codex.git
cd agy-codex
python install.py --global
```

> **Windows 提示**：运行安装器或独立 CLI 时，如果系统 `PATH` 中的 `python` 指向 Microsoft Store 应用别名或未安装 `py` 启动器，请直接使用已安装的 Python 3.10+ 完整路径（例如 `& "C:\Python312\python.exe" install.py --global`）。工作流内继续使用宿主提供或通过 `ANTIGRAVITY_PYTHON` 指定的解释器。

- **全局安装目标**：`~/.gemini/config/plugins/agy-codex`
- **项目级安装**：`python install.py --project /path/to/your/repo`（安装至 `.agents/plugins/agy-codex`）
- **可恢复卸载**：`python install.py --uninstall`（将插件移出发现目录并保留备份，不删除任何项目任务状态）

### 方式二：官方 CLI 安装

```bash
agy plugin validate --strict ./agy-codex
agy plugin install ./agy-codex
agy plugin list
```

### 在 Antigravity 中启用

1. 打开 **Customizations → Installed**，确认 **AGY Codex** 已启用。
2. 在模型选择器中选择 **Gemini**，打开你的工作区。
3. 根据使用场景选择以下任一种方式启动：

- **方式 A：专用协同会话（切换为 `agy-codex` 主 Agent）**
  - 在输入框下方的 **Agents** 菜单中将主 Agent 从默认的 `Main Agent` 切换为 **`agy-codex`**。
  - 适合整段项目开发：整个会话全程遵循“Gemini 沟通复审 + Codex 唯一写入”的规范，无需每次手动触发；需要切回通用对话时，在 **Agents** 菜单点回 `Main Agent` 即可。
- **方式 B：留在默认 `Main Agent`，按需调用 `/agy-codex`**
  - 如果不想来回切换主 Agent，可以一直停留在默认的 **`Main Agent`**，仅在需要把某个具体任务交给 Codex 实施时，在输入框中输入 **`/agy-codex <你的任务>`**。
  - 插件刻意不附带全局接管规则（`always_on`），避免你在普通问答或轻量对话中也被强制走 Codex 流程。

---

## 日常使用示例

| 你对 AGY Codex 说 | 内部行为 |
| :--- | :--- |
| “修复配置导入失败，兼容已有格式。” | 检查项目约定、生成任务范围与验收命令，交由 Codex 实施并由 Gemini 只读复审。 |
| “按 PLAN.md 的 T-01 到 T-05 顺序完成。” | 冻结计划内任务合同与依赖顺序，在同一次工作流中逐项实施、复审，通过后自动进入下一项。 |
| “继续。” | 优先恢复当前未完成的计划或任务（沿用原 Codex Session 与剩余轮数）。 |
| “进度怎样？” | 只读读取本地任务状态与 `progress.json`，不启动新实现或复审。 |
| “按你推荐的兼容方案做。” | 校验对应待决问题与当前轮次后，记录实际回答并恢复原 Session。 |
| “这个计划不做了，保留已有修改。” | 显式停止计划推进（`stop_plan`），保留已完成的成果与诊断证据。 |

---

## 本地状态与安全边界

- **状态存储**：所有控制器元数据保存在目标仓库的 `<git-dir>/superagent/` 下（包含 `plans/`、`tickets/`、基线快照、独立测试日志与 `diff.patch`），不污染用户工作区文件，也不随插件目录删除而丢失。
- **Git 纪律**：本工具**绝不自动执行** `git add`、`commit`、`push`、`merge`、`reset`、`clean` 或切换分支。
- **范围控制 (`allowed_paths`)**：路径从仓库根开始匹配；支持相对文件、目录前缀（`src/` 或 `src`）、Glob（`tests/test_*.py`、`**/*.ts`）以及整个仓库（`.`）。`*`、`?`、字符组只匹配单个路径段，`**` 匹配零层或多层目录。
- **被 Git 忽略的目标文件**：在 `allowed_paths` 中明确写出文件路径（例如 `local-config.json`），即使文件尚未创建也会跟踪其后续创建、修改和删除。目录前缀或 Glob 不会展开被忽略的依赖／构建目录。
- **轮次上限**：单个任务最多 3 轮实现（`max_rounds <= 3`），禁止通过新建同名任务绕过轮次限制；只读复审格式异常时仅在同一只读会话内纠正一次。

---

## 验证与测试

### 运行离线集成测试套件

```bash
python -B -m unittest discover -s tests -v
```

GitHub Actions 在 Ubuntu、Windows 与 Python 3.10、3.12 的四种组合上运行同一套离线测试，覆盖协议、任务恢复、进程清理和安装行为。测试使用 Codex / AGY 替身，不需要模型账号或密钥；真实模型与宿主的交互通过下面的实机冒烟测试验证。

### 实机冒烟测试（可选）

安装完成后，可在独立新建的临时目录验证本机环境、真实 Codex CLI 写入/恢复以及 AGY 工作流闭环：

```bash
# 1. 环境自检
python ~/.gemini/config/plugins/agy-codex/skills/agy-codex/scripts/user_cli.py --repo /path/to/repo doctor

# 2. 真实 Codex CLI 写入与同 Session 恢复探针（新建空目录）
python ~/.gemini/config/plugins/agy-codex/skills/agy-codex/scripts/smoke.py probe --repo ~/agy-codex-probe

# 3. 初始化独立两步计划测试仓库，并在 Antigravity 中打开继续执行
python ~/.gemini/config/plugins/agy-codex/skills/agy-codex/scripts/smoke.py setup-plan --repo ~/agy-codex-smoke-plan
python ~/.gemini/config/plugins/agy-codex/skills/agy-codex/scripts/smoke.py verify-plan --repo ~/agy-codex-smoke-plan
```

---

## 目录结构

```text
agy-codex/
├── plugin.json                        # Antigravity 原生插件清单 (v0.3.0)
├── install.py                         # 跨平台安装 / 备份 / 卸载器
├── LICENSE                            # MIT License
├── agents/
│   ├── agy-codex.md                   # 可选主 Agent 系统提示与协作规则
│   └── agy-codex-reviewer.md          # 插件内置最小只读 Reviewer 子代理
├── skills/
│   └── agy-codex/
│       ├── SKILL.md                   # v0.3 执行手册与 /agy-codex 入口
│       ├── references/                # 规划、执行、复审、恢复与宿主调用契约
│       └── scripts/                   # 共享 Runtime、轻量 CLI、Strict 控制器与后端适配
└── tests/                             # 离线集成测试与协议回归套件
```

---

## 致谢与许可

- 本项目基于 **MIT License** 开源，详见 [LICENSE](./LICENSE)。
- 规划、任务切片、TDD、代码审查与交接方法论部分改编自 [mattpocock/skills](https://github.com/mattpocock/skills)（MIT License，原许可声明保存在 [mattpocock-license.txt](./skills/agy-codex/references/mattpocock-license.txt)）。

## Codex Worker model configuration

Optional plugin-only JSON configuration (Python 3.10+, no dependencies):

- Global: `~/.gemini/config/agy-codex/config.json` (outside the installed plugin).
- Per worktree: `<git-dir>/superagent/config.json` (overrides global fields).
- Optional `AGY_CODEX_CONFIG` environment variable selects the global file.

Example (replace with model ID and effort supported by your Codex installation):

```json
{"codex": {"model": "gpt-6-luna", "reasoning_effort": "high"}}
```

You can also tell AGY to use a specific Codex model/effort for a task or upgrade
it on continue. Explicit workflow settings override task, plan and plugin defaults.
Defaults are captured when a Ticket is created; a later explicit continue override
is retained for the current Ticket's subsequent Worker calls, never inherited by
later plan steps. Missing settings are left to
Codex. The plugin passes `-m` and `-c model_reasoning_effort=...` to both exec and
exec resume, and records the requested values in each invocation. It does not
edit Codex configuration or change CODEX_HOME, and does not claim to verify the
actual served model or isolate account usage limits.

The App Server adapter below adds native protocol discovery and execution. Its integrated host behavior must still be tested on the target computer.

## App Server backend (v0.2.0)

The existing JSON configuration may specify `"backend": "auto"` (default for
native CLI: App Server first; exec only on pre-thread initialization failure),
`"app-server"` (no fallback) or `"exec"` (compatibility mode).
A model/effort override is still local to the Ticket. The optional
`codex.model_provider` explicitly selects a native Codex provider and is pinned
on subsequent calls once known. A provider ID is not a service identity proof.

Example plugin-only configuration:

```json
{
  "backend": "auto",
  "codex": {"model_provider": "openai"},
  "routing": {
    "strategy": "auto",
    "default_tier": "standard",
    "allowed_providers": ["openai"],
    "allow_upgrade": true,
    "tiers": {
      "economy": {"model": "gpt-6-luna", "reasoning_effort": "low"},
      "standard": {"model": "gpt-6-luna", "reasoning_effort": "high"},
      "heavy": {"model": "gpt-6.2-sol", "reasoning_effort": "xhigh"}
    }
  }
}
```

Use exact IDs supported by your own CLI. Router reads `user_cli.py --repo REPO
models --json` once when choosing a tier. This invokes only discovery RPCs, not
model inference, and reports catalog capabilities and available rate windows.
A catalog is not proof of entitlement or actual cost. Gemini chooses among
configured tiers; no guessed cost or hard-coded automatic upgrades.
`economy` and `quality` select their named default tiers unless overridden;
`manual` uses explicit settings/native defaults. `allow_upgrade` is a Router
policy preference. Explicit workflow `tier` maps to a configured tier, then
explicit `codex` fields win. No native Codex settings are edited.

Both backends retain the existing validation and review pipeline. App Server is
an owned per-round stdio child, never a daemon. A durable submission marker is
written before turn/start. An ambiguous Turn is reconciled via native Thread
history before another call; a missing Turn ID or unconfirmed outcome requires
inspection rather than a duplicate execution. Raw RPC notifications are stored
locally; only selected discovery fields are retained. Test with CLI 0.171.0 or
a compatible v2 schema before adopting another version.

## Live control (App Server only)

While a running workflow owns an active Turn, the main AGY conversation may
submit a clarification independently through the existing command tool:

```text
PYTHON SKILL_DIR/scripts/user_cli.py --repo REPO inbox queue "Preserve the existing input format."
PYTHON SKILL_DIR/scripts/user_cli.py --repo REPO inbox steer "Prioritize the compatibility fix."
PYTHON SKILL_DIR/scripts/user_cli.py --repo REPO inbox interrupt
PYTHON SKILL_DIR/scripts/user_cli.py --repo REPO inbox status --json
```

Use queue for ordinary in-scope clarification, steer for immediate correction,
interrupt to pause execution. No workflow or worktree lock is acquired by
these producers. Each Turn has a unique Maildir publication window: once sealed,
late publication fails explicitly rather than targeting the next task. An
accepted RPC is not proof of implementation. On completion all accepted
clarifications and deferred/unknown delivery records are included in the
review evidence. The main Router should disclose unfulfilled messages.

A precise stale-steer error (code -32600 with "no active turn to steer") is
queued for a supplement Turn. Other RPC errors are not silently replayed.
At most two extra queued Turns are allowed per Ticket, independent of the
existing three implementation/review rounds; each supplement is on the same
Thread before independent validation. A bounded batch holds at most 32 messages.
Messages beyond the budget remain recorded as deferred, not completed.
A genuine scope/acceptance/test change needs interrupt and a new scoped contract,
not a steer/queue instruction. An interrupt preserves changes and pauses the
workflow even if it races completion. Resume uses the original Ticket.

Delivery is explicitly tracked as queued, delivery_unknown, rpc_accepted,
turn_completed, turn_interrupted, turn_failed, rejected or deferred. Unknown
delivery after a crash is not auto-resubmitted. Status exposes the saved
Session, contract summary, last decisions, progress and truncated message text.
For detailed messages inspect the Ticket's inbox evidence. Full live validation
of parallel Antigravity command invocation remains a local host test.
