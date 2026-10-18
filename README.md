# my_agents_fleet

本地 agent 的统一管理工具。

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="macOS / Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <img alt="Status alpha" src="https://img.shields.io/badge/status-alpha-orange">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green">
</p>

## 这是什么

你电脑上可能装了好几个 agent：Claude Code、Codex、Grok……它们各自独立，每次用都要重新交代背景，用完就忘，做过什么也没有记录。

my_agents_fleet 用管理一个团队的方式管理它们。团队里每个人有明确的身份和分工，有各自的权限，记得之前的工作，做过的事有记录可查。这里的每个 agent 也一样：

| 团队成员有 | agent 在这里有 | 怎么配置 |
|---|---|---|
| **身份**：是谁、负责什么 | 一段职责说明，每次工作时自动带上 | `intro`、`instructions_file` |
| **能力**：用什么方式做事 | 用哪个 agent 工具、哪个模型、多大推理强度 | `backend`、`model`、`effort` |
| **权限**：能动哪些东西 | 只读，还是可以改文件、跑命令；在哪个目录工作 | `mode`、`workspace` |
| **记忆**：记得之前聊过什么 | 用会话编号保存对话，下次接着聊 | `--session` |
| **工作记录**：做了什么、结果如何 | 每次任务的结果、耗时、费用、失败原因 | `state/fleet.db` |
| **团队规范**：所有人都要遵守 | 所有 agent 共用的说明 | `global.md` |

你的角色是团队负责人：决定把什么任务交给谁、按什么顺序做、结果交给谁接手。这个工具不替你做这些决定，它负责让每个 agent 按设定的身份和权限工作，并把过程记下来。

## 支持的 agent 工具

| 工具 | 本机命令 | 配置里写 |
|---|---|---|
| Claude Code | `claude` | `claude` |
| Codex CLI | `codex` | `codex` |
| Grok CLI | `grok` | `grok` |
| Antigravity CLI | `agy` | `agy` |
| Pi CLI | `pi` | `pi` |

装了哪个就能用哪个，至少需要一个，并且已经在该工具里登录。登录、账号和计费都由各工具自己管理，本项目不涉及。

## 安装

环境要求：macOS 或 Linux，Python 3.11 及以上。

**交给 AI 助手安装。** 把下面这段话发给 Claude Code、Codex 或其他能读网页、能执行命令的 AI 助手：

```text
帮我安装这个项目。按它 README 里「给 AI 助手的安装说明」一节逐步执行：
https://github.com/nickzhangcomes/my_agents_fleet
```

它会完成安装，创建一个示例团队，并告诉你本机有哪些 agent 工具可用。安装过程不会发起模型调用。

**手动安装。** 任选一种：

```bash
uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
git clone https://github.com/nickzhangcomes/my_agents_fleet.git
cd my_agents_fleet
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
```

运行 `my-agents-fleet --version`，能输出版本号即安装成功。

## 快速开始

以下输出来自一次实际运行，省略了无关字段和本机路径。

**1. 建一个团队，并检查环境**

```bash
my-agents-fleet init ./my-fleet
my-agents-fleet doctor ./my-fleet
```

```text
本机的 agent 工具
名称    命令    位置
------  ------  ----------------------
claude  claude  /usr/local/bin/claude
codex   codex   未找到
grok    grok    未找到
agy     agy     未找到
pi      pi      未找到

团队：<目录>/my-fleet
agent       工具    可用  说明
----------  ------  ----  ----------------------
reviewer    claude  ✓     只读（工具白名单）
summarizer  claude  ✓     只读（工具白名单）
```

示例团队里有两个 agent：`reviewer` 负责审阅，`summarizer` 负责摘要，都用 Claude Code，都只有只读权限。`doctor` 会列出本机装了哪些 agent 工具，并检查每个 agent 的配置能不能用。这两条命令都不会调用模型。

**2. 交给 reviewer 一个任务**

`data/sample.md` 是示例自带的一段项目笔记。

```bash
my-agents-fleet run ./my-fleet reviewer "阅读 data/sample.md，用一句话说明现状，再给一个下一步建议。" --session review-1
```

> **现状**：原型目前只做完了文件导入，搜索还停在计划阶段，下一个里程碑是一次小规模可用性测试。
>
> **下一步建议**：先不要开发搜索功能，用已经能跑通的导入功能做那次可用性测试（找 3–5 个人，观察他们导入完成后想做什么）。……

**3. 接着上次的话题继续**

```bash
my-agents-fleet run ./my-fleet reviewer "把你刚才的下一步建议拆成三步，每步一句话。" --session review-1
```

> 1. 招 3–5 名目标用户，约定一个 30 分钟的可用性测试时段。
> 2. 让他们只用现有的导入功能，观察并记录导入完成后他们的第一个动作。
> 3. 汇总这些动作，得出搜索功能要满足的具体需求，再决定怎么做。

两次用的是同一个会话编号 `review-1`，所以 reviewer 记得「刚才的建议」是哪一条。

**4. 看工作记录**

```bash
my-agents-fleet runs ./my-fleet
```

```text
agent     次数  成功  失败  取消  费用     平均耗时
--------  ----  ----  ----  ----  -------  --------
reviewer  2     2     0     0     $0.4819  22.5s
```

需要给程序处理时加 `--json`。

## 配置一个 agent

一个团队就是一个目录：

```text
my-fleet/
├── fleet.json          团队名单：有哪些 agent，各自的设置
├── global.md           团队规范：所有 agent 共用的说明
├── agents/
│   ├── reviewer.md     reviewer 的详细职责说明
│   └── summarizer.md   summarizer 的详细职责说明
├── data/sample.md      示例资料
└── state/              首次运行后生成，保存记忆和工作记录（已加入 .gitignore）
```

`fleet.json` 里，每个 agent 是 `agents` 下的一项：

```json
"reviewer": {
  "backend": "claude",
  "effort": "high",
  "mode": "readonly",
  "workspace": "../my-project",
  "intro": "审查代码，按严重程度列出问题，不修改代码。",
  "instructions_file": "agents/reviewer.md",
  "timeout": 300
}
```

| 字段 | 说明 |
|---|---|
| `backend` | 用哪个 agent 工具，默认 `claude` |
| `model` | 用哪个模型，不填则用该工具的默认模型 |
| `effort` | 推理强度，如 `low`、`high` |
| `mode` | `readonly` 只读；`full` 可以改文件、跑命令 |
| `workspace` | 工作目录，相对团队目录 |
| `intro` | 一句话职责说明 |
| `instructions_file` | 详细职责说明文件 |
| `timeout` | 单次任务的超时秒数 |

写在 `global.defaults` 里的字段对所有 agent 生效，单个 agent 的设置会覆盖它。配置每次运行都会重新读取，修改后立即生效。完整字段见[配置说明](docs/configuration.md)。

### 身份是怎么传给 agent 的

每次任务开始时，工具会把以下内容按顺序拼成系统提示发给 agent：团队背景（`business_scenario`、`global.md`）→ 团队约束（`constraints`）→ 可以访问的目录 → 「你是谁」（`intro`）→ 「你的工作内容」（该 agent 的说明文件）→ 可用技能（`skills`）。

### 权限有多严格

不同 agent 工具实现只读权限的方式不同，严格程度也不同：

| 工具 | 只读的实现方式 | 严格程度 |
|---|---|---|
| Codex | 操作系统沙箱 | 最严格，系统层面禁止写入 |
| Claude Code | 工具白名单 | 进程内强制 |
| Pi | 工具白名单 | 进程内强制 |
| Grok | 拒绝规则 | 进程内强制 |
| Antigravity | 计划模式 | 最宽松，依赖模型自觉 |

**注意：** `mode: "full"` 下，Codex 会关闭沙箱和确认提示，Claude Code 会跳过权限确认。只给需要改代码的 agent 开启，并且只在你信任的目录里使用。

### 记忆怎么保存

加上 `--session <编号>`，对话就会保存下来，下次用同一个编号可以接着聊。

- 记忆按 agent 区分：`dev` 的 `login` 和 `reviewer` 的 `login` 是两段不同的对话。
- 不加 `--session` 的任务是一次性的，不保存。
- 用 `my-agents-fleet sessions` 列出所有会话；`sessions <目录> <agent> <编号>` 查看某段会话的完整内容；加 `--reset` 清空这段会话的记忆。
- 如果 agent 的工具、目录、权限、模型或职责说明改了，它不会直接续上旧对话，而是开一段新对话，并把之前的内容摘要作为背景带上，避免它带着旧环境的认知在新环境里工作。

### 工作记录里有什么

每次任务都会在 `state/fleet.db` 记录：哪个 agent、成功与否、耗时、费用、失败原因。

- 失败分两类：**环境问题**（超时、未登录、额度不足、工具崩溃）和**交付问题**（输出不完整、格式不对、空输出）。超时、额度不足和崩溃会按 `max_attempts` 自动重试。
- 目前只有 Claude Code 提供费用数据，其他工具的费用显示为 `null`，表示未知，不是零。

## 示例：三个 agent 协作开发

假设项目代码在 `~/code/ledger`，你想组一个三人小组：

| agent | 工具 | 权限 | 分工 |
|---|---|---|---|
| `architect` | Claude Code（高推理强度） | 只读 | 拆分需求、指出风险，不写代码 |
| `dev` | Codex | 可写 | 写代码、跑测试 |
| `reviewer` | Claude Code | 只读 | 审查代码，不修改 |

先建团队目录：

```bash
my-agents-fleet init ~/code/ledger-team
```

把 `~/code/ledger-team/fleet.json` 换成：

```json
{
  "version": 2,
  "global": {
    "shared": {"instructions_file": "global.md"},
    "defaults": {
      "workspace": "../ledger",
      "mode": "readonly",
      "max_attempts": 2,
      "max_concurrent": 1,
      "timeout": 300
    },
    "state_dir": "state"
  },
  "agents": {
    "architect": {
      "backend": "claude",
      "effort": "high",
      "intro": "把需求拆成可执行的步骤，指出风险，不写代码。"
    },
    "dev": {
      "backend": "codex",
      "mode": "full",
      "timeout": 900,
      "intro": "按给定步骤实现功能，完成后运行测试。"
    },
    "reviewer": {
      "backend": "claude",
      "intro": "审查代码，按严重程度列出问题和风险，不修改代码。"
    }
  }
}
```

在 `global.md` 里写团队规范，例如「用中文回答；不确定时明确说明」。示例自带的 `agents/` 和 `data/` 用不到，可以删掉。

然后像带团队一样安排工作：

```bash
T=~/code/ledger-team

# architect 先拆需求
my-agents-fleet run $T architect "阅读 docs/登录需求.md，拆成开发步骤，标出风险。" --session login

# dev 按步骤实现（会修改项目文件）
my-agents-fleet run $T dev "阅读 docs/登录需求.md，实现登录接口并补充测试。token 需要设置过期时间。" --session login

# reviewer 审查
my-agents-fleet run $T reviewer "审查 src/auth/ 中新增的登录接口，按严重程度列出问题。" --session login

# 把审查意见转给 dev；同一个会话里，dev 记得自己刚才写了什么
my-agents-fleet run $T dev "密码错误时的提示会暴露用户是否存在，请修改并重新运行测试。" --session login
```

agent 之间不会直接沟通。像最后一步，reviewer 的意见是你转达给 dev 的。

## 命令

| 命令 | 作用 | 是否调用模型 |
|---|---|---|
| `my-agents-fleet init <目录>` | 在新目录或空目录里创建示例团队 | 否 |
| `my-agents-fleet inspect <目录>` | 列出团队里的 agent | 否 |
| `my-agents-fleet doctor [目录]` | 检查本机的 agent 工具；给出目录时，同时检查每个 agent 能否使用 | 否 |
| `my-agents-fleet run <目录> <agent> "<任务>" [--session 编号]` | 交给某个 agent 一个任务，输出结果；失败时退出码为 1 | **是** |
| `my-agents-fleet runs <目录> [--agent 名字] [--since 日期] [--json]` | 按 agent 汇总工作记录 | 否 |
| `my-agents-fleet sessions <目录> [agent 编号] [--reset]` | 列出会话，查看或清空某段会话 | 否 |
| `my-agents-fleet models [工具] [--cached]` | 列出某个工具可用的模型和推理强度 | 否 |

任务内容较长时，可以写在文件里：`run <目录> <agent> --file task.md`；也可以从管道传入：`cat task.md | my-agents-fleet run <目录> <agent> -`。

`run` 的结果输出到标准输出，重试等提示输出到标准错误，可以直接用管道处理结果。

`models` 会调用本机的 agent 工具读取模型列表，但不发起推理；加 `--cached` 只读缓存。

安装后还会多一个 `my-agents2` 命令，用于调试底层的工具适配层（如 `my-agents2 --caps` 查看各工具支持的功能），日常用不到。

## 在 Python 中使用

适合需要批量分派任务，或者想让 agent 之间自动交接的场景。

```python
from my_agents_fleet import load_fleet

fleet = load_fleet('~/code/ledger-team')

# 同时交给两个 agent，结果按传入顺序返回；一个失败不影响另一个
results = fleet.dispatch([
    {'agent': 'reviewer',  'prompt': '审查 src/auth/，按严重程度列出问题。'},
    {'agent': 'architect', 'prompt': '对照 docs/登录需求.md，检查 src/auth/ 是否有遗漏。'},
], workers=2)
for r in results:
    print(r.ok, r.text, r.failure)

# 接着 dev 在 login 会话里的工作
out = fleet.session('dev', 'login').ask('把刚才的改动总结成一条 commit message。')
print(out.ok, out.text, out.cost_usd)

# 查工作记录
fleet.stats(since='2026-10-01')
fleet.history(agent='dev', failed_only=True)
```

## 不做什么

- 不替你分配任务、安排顺序，agent 之间不会自动传话。
- 没有定时任务和后台队列，进程中断后不会自动恢复。
- 不安装 agent 工具，不处理登录和 API key。
- 不是安全沙箱：只读的严格程度取决于工具本身；`untrusted` 模式限制的是工具权限，不能替代网络隔离。
- 面向个人本地使用，没有多用户权限管理。

## 常见问题

**费用为什么显示 `null`？**
有部分任务拿不到费用数据（目前只有 Claude Code 提供）。`null` 表示未知，`0` 才表示零。

**数据存在哪里？**
记忆和工作记录在团队目录的 `state/` 下，`init` 已将其加入 `.gitignore`；模型列表缓存在 `~/.cache/my-agents-fleet/`。都不要提交到公开仓库，详见[数据与安全](SECURITY.md)。

**把示例里的 `summarizer` 换成 Grok 或 Pi 后报错？**
它设置了 `output_schema`（要求按固定格式输出 JSON），Grok 和 Pi 暂不支持，删掉这个字段即可。

**改了配置之后，agent 好像不记得之前的对话了？**
工具、目录、权限、模型或职责说明改变后，旧对话不会直接续接，而是带着摘要开一段新对话。这是有意的设计。

**`my_agents_fleet` 和 `my_agents2` 是什么关系？**
`my_agents2` 负责和各个 agent 工具打交道（统一参数、转换权限、解析输出）；`my_agents_fleet` 建在它上面，负责身份、记忆和工作记录。两者一起安装，通常只用 `my_agents_fleet`。详见[架构说明](docs/architecture.md)。

## 给 AI 助手的安装说明

> 如果你是 AI 助手（Claude Code、Codex 等），用户把这个仓库的链接发给了你，请按以下步骤执行。

1. **检查环境。** 运行 `python3 --version`，要求 3.11 或更高；系统须为 macOS 或 Linux。不满足时告知用户并停止。
2. **安装。** 按顺序选择第一个可用的方式，不要重复安装：
   - 有 `uv`：`uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - 有 `pipx`：`pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - 都没有：`git clone https://github.com/nickzhangcomes/my_agents_fleet.git ~/my_agents_fleet`，在该目录中运行 `python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .`。完成后告知用户，之后每次使用前需先运行 `source ~/my_agents_fleet/.venv/bin/activate`。
3. **验证。** 运行 `my-agents-fleet --version`，输出版本号即成功。
4. **创建示例团队。** 运行 `my-agents-fleet init ~/my-fleet`。目标目录必须不存在或为空；若 `~/my-fleet` 已存在且非空，跳过 `init` 或换一个目录名。这一步不会调用模型。
5. **检查可用的 agent 工具。** 运行 `my-agents-fleet doctor ~/my-fleet`，它会列出本机装了哪些 agent 工具，并检查示例团队里的 agent 能否使用。这一步不会调用模型。
6. **汇报并停止。** 告诉用户：安装已完成；哪些 agent 工具可用；示例团队位于 `~/my-fleet`，包含 `reviewer` 和 `summarizer` 两个 agent，默认使用 Claude Code。然后询问用户接下来想做什么。
7. **禁止事项：** 不要替用户登录任何 agent 工具；不要创建或修改 API key；未经用户明确同意，不要运行 `my-agents-fleet run`，它会发起实际的模型调用并产生费用。

## 更多文档

- [配置说明](docs/configuration.md)：全部字段、存储位置、环境变量
- [能力与限制](docs/capabilities.md)：各 agent 工具的支持情况
- [模型发现](docs/model-discovery.md)：模型列表的来源和更新方式
- [数据与安全](SECURITY.md)：本地保存的数据及注意事项
- [架构说明](docs/architecture.md) · [参与开发](CONTRIBUTING.md) · [更新记录](CHANGELOG.md)

许可证：[Apache-2.0](LICENSE)。各 agent 工具及其服务遵循各自的条款。
