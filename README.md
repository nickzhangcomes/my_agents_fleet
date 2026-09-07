# my_agents_fleet

<p align="center"><strong>把 Claude Code、Codex、Grok 这些各家的 AI agent 统一管起来：分角色、派任务、留记录。</strong></p>

<p align="center">
  中文 · <a href="README.en.md">English</a>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="macOS / Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <img alt="Status alpha" src="https://img.shields.io/badge/status-alpha-orange">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green">
</p>

> 你电脑里装了不止一个 agent？这个项目把它们从散兵组织成一支真正的团队：**统一管理、角色分配、任务分配**，共同做好一个项目。

## 它解决什么

现在你用 Claude Code 或 Codex，大概是这样：

- 想让 Codex 写、Claude 审，得开两个窗口，来回复制粘贴。
- 每开一个新终端，它就是个陌生人。昨天聊到哪、上次给过什么建议，全忘了。
- 跑了十个任务，哪个成了、哪个挂了、一共花了多少钱？没人记。

这就是**临时工**：叫来干活，干完就散，不问名字。my_agents_fleet 把它们编成一支正式团队，做三件事：

| 三件事 | 大白话 | 比如 |
|---|---|---|
| **统一管理** | 各家的 AI 工具放进同一份名册，用同一条命令调用 | Claude Code、Codex、Grok 都在名册上，谁负责什么，一眼看清 |
| **角色分配** | 给每个 AI 定名字、定岗位、定权限、定模型 | 「开发」用 Codex，能改代码；「审查员」用 Claude，只能看不能改 |
| **任务分配** | 把活派给指定角色；对话有编号能接着聊；每次执行都有记录 | 「开发，实现登录接口」「审查员，找找漏洞」。谁做了什么、成没成、花了多少，都记着 |

## 安装：把链接发给你的 AI 就行

不用自己敲命令。打开 Claude Code（或 Codex，或任何能读网页、能跑命令的 AI 助手），把下面这段话发给它：

```text
帮我安装这个项目。按它 README 里「给 AI 助手的安装说明」那一节，一步一步做：
https://github.com/nickzhangcomes/my_agents_fleet
```

它会自己下载、安装、建一支示例团队，然后回来告诉你：装好了，你电脑上有哪几个 AI 工具能用。安装过程只是下载和配置，不会拿你的 AI 账号去跑任务。

<details>
<summary>想自己动手？点开看</summary>

需要 macOS 或 Linux，Python 3.11 或更新。任选一种：

```bash
# 推荐：装成一个全局命令（有 uv 用 uv，有 pipx 用 pipx）
uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
# 或者：下载源码，装进它自己的小环境里
git clone https://github.com/nickzhangcomes/my_agents_fleet.git
cd my_agents_fleet
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
```

装好后运行 `my-agents-fleet --version`，能打印出版本号就对了。

</details>

## 装好之后，5 分钟走一遍

下面的输出来自一次真实运行，只删掉了跟你无关的字段和我的本机路径。

### 1. 建一支小团队

```bash
my-agents-fleet init ./my-fleet
my-agents-fleet inspect ./my-fleet
```

```text
fleet @ <你的目录>/my-fleet
  reviewer       claude   -                      active   Review project notes and ident
  summarizer     claude   -                      active   Summarize the supplied text.
State directory: <你的目录>/my-fleet/state
```

自带两个角色：审阅员 `reviewer` 和整理员 `summarizer`，默认都用 Claude Code，只读模式。这两步不会调用 AI，不花钱。

### 2. 派第一个活

`my-fleet/data/sample.md` 是一份自带的三句话项目笔记。让审阅员看看：

```bash
my-agents-fleet run ./my-fleet reviewer "阅读 data/sample.md，用一句话说明现状，再给一个下一步建议。" --session review-1
```

> **现状**：原型目前只做完了文件导入，搜索还停在计划阶段，下一个里程碑是一次小规模可用性测试。
>
> **下一步建议**：先别急着开搜索功能——用现有的"能导入"这一条路径，把那场小规模可用性测试跑起来（找 3–5 个人，看他们导入完之后第一反应想干什么）。理由链条：导入是唯一能跑通的功能 → 测试它能拿到真实用户在导入后的行为 → 这个行为直接告诉你搜索该做成什么样，省得先按猜想把搜索做出来再返工。
>
> （后面还有一段「说明边界」，略）

### 3. 接着聊，它记得

```bash
my-agents-fleet run ./my-fleet reviewer "把你刚才的下一步建议拆成三步，每步一句话，别加解释。" --session review-1
```

> 1. 招 3–5 名目标用户，约定一个 30 分钟的可用性测试时段。
> 2. 让他们只用现有的导入功能，观察并记录导入完成后他们的第一个动作。
> 3. 汇总这些动作，得出搜索功能要满足的具体需求，再决定怎么做。

「你刚才的建议」，它知道是哪条。秘诀就是 `--session review-1` 这个编号：同一个编号就是同一段对话，隔一天、隔一周都能接上。

### 4. 看考勤

```bash
my-agents-fleet runs ./my-fleet
```

```text
"agent": "reviewer",
"runs": 2,          # 派了 2 次活
"ok": 2,            # 2 次都成了
"failed": 0,
"cost": 0.4819,     # 两次真实调用 Claude，一共 0.48 美元
"avg_ms": 22455     # 平均每次 22 秒
```

费用如果显示 `null`，意思是「有一部分算不清」，不是零；显示 `0` 才是零。

## 例子：一个人带三个 AI 做一个项目

假设你在做一个记账应用，代码在 `~/code/ledger`。在旁边建一支团队：

```bash
my-agents-fleet init ~/code/ledger-team
```

把 `~/code/ledger-team/fleet.json` 换成下面这份，三个角色、三种配置：

| 角色 | 用谁 | 权限 | 干什么 |
|---|---|---|---|
| `architect` 架构师 | Claude Code，`effort` 开到 `high` | 只读 | 把需求拆成步骤，标出风险，不写代码 |
| `dev` 开发 | Codex | 可写 | 在项目目录里写代码、跑测试 |
| `reviewer` 审查员 | Claude Code | 只读 | 挑毛病、找安全问题，不许改代码 |

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
      "intro": "先把需求拆成可执行的步骤，指出风险，不写代码。"
    },
    "dev": {
      "backend": "codex",
      "mode": "full",
      "timeout": 900,
      "intro": "按给定的步骤实现功能，改完自己跑测试。"
    },
    "reviewer": {
      "backend": "claude",
      "intro": "审查代码，只列问题和风险，按严重程度排序，不改代码。"
    }
  }
}
```

`global.md` 里写所有角色都要遵守的规则，比如「用中文回答；不确定的地方直说，不要编」。

**一个提醒：** `mode` 是 `full` 的角色会真的改文件，而且 Codex 在这个模式下沙箱和确认弹窗都是关着的，Claude Code 则是跳过权限确认。所以只给「开发」开可写，只在你信得过的项目目录里用；其他角色一律只读。

然后按你自己的节奏派活：

```bash
# 1. 架构师先想清楚（只读，不会动代码）
my-agents-fleet run ~/code/ledger-team architect "读 docs/登录需求.md，拆成开发步骤，标出风险点。" --session login

# 2. 开发照着做（可写，会真的改项目文件）
my-agents-fleet run ~/code/ledger-team dev "读 docs/登录需求.md，实现登录接口，加测试并跑通。注意 token 要有过期时间。" --session login

# 3. 审查员挑毛病（只读）
my-agents-fleet run ~/code/ledger-team reviewer "审查 src/auth/ 里新写的登录接口，列出问题，按严重程度排序。" --session login

# 4. 把审查意见丢回给开发。同一个编号，它记得自己刚写了什么
my-agents-fleet run ~/code/ledger-team dev "审查员说密码错误的提示会泄露用户是否存在。改掉，重新跑测试。" --session login
```

对话编号是按角色分开记的：三个角色都叫 `login`，各记各的，不会串。

谁先谁后、要不要返工，是你在安排。工具不替你做决定，也不会自己把架构师的输出塞给开发。你是项目经理，它们是团队。

每个角色可以单独设的东西：

- `backend`：用哪家的工具
- `model`：用哪个模型，不写就用那个工具自己的默认
- `effort`：思考投入档位，让它多想一会儿还是快点答
- `mode`：`readonly` 只能看不能改文件，`full` 放开
- `workspace`：在哪个文件夹干活
- `timeout`：一次最多跑多少秒

全部字段见[配置说明](docs/configuration.md)。模板自带的 `summarizer` 带了 `output_schema`（要求按固定 JSON 格式返回），Grok 和 Pi 目前不支持，换成它们时把那一段删掉。

不确定有哪些模型、哪些档位能选？问它：

```bash
my-agents-fleet models codex
```

```text
── codex（数据源 cache）
   gpt-5.6-sol               low/medium/high/xhigh/max/ultra     默认=low
   gpt-5.5                   low/medium/high/xhigh               默认=medium
   gpt-5.4-mini              low/medium/high/xhigh               默认=medium
   …
```

## 会写一点 Python 的话，能批量派活

```python
from my_agents_fleet import load_fleet

fleet = load_fleet('~/code/ledger-team')

# 开发写完后，让两个只读角色同时看，各自出结果
results = fleet.dispatch([
    {'agent': 'reviewer',  'prompt': '审查 src/auth/，列出问题，按严重程度排序。'},
    {'agent': 'architect', 'prompt': '对照 docs/登录需求.md，检查 src/auth/ 有没有漏做的。'},
], workers=2)
for r in results:
    print(r.ok, r.text)

# 接着某个角色的对话往下聊
session = fleet.session('dev', 'login')
out = session.ask('把你刚才的改动总结成一段 commit message。')
print(out.ok, out.text)
```

一个失败不会拖垮其他的。失败的那个会留下记录和原因（在 `r.failure` 里）。

## 支持哪些 AI 工具

| 工具 | 你电脑上的命令 | 配置里写 |
|---|---|---|
| Claude Code | `claude` | `claude` |
| Codex CLI | `codex` | `codex` |
| Grok CLI | `grok` | `grok` |
| Antigravity CLI | `agy` | `agy` |
| Pi CLI | `pi` | `pi` |

装了哪个就能用哪个，只装一个也行。**登录、账号、付费都还在各家工具自己那儿**，这个项目不碰。

## 🤖 给 AI 助手的安装说明

> 如果你是一个 AI 助手（Claude Code、Codex 等），用户把这个仓库的链接发给了你，请按下面的步骤执行。

1. **检查环境。** 运行 `python3 --version`，需要 3.11 或更高；系统需要 macOS 或 Linux。不满足就直接告诉用户，不要硬装。
2. **安装。** 按顺序尝试，哪个可用就用哪个，不要重复安装：
   - 有 `uv`：`uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - 有 `pipx`：`pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - 都没有：`git clone https://github.com/nickzhangcomes/my_agents_fleet.git ~/my_agents_fleet`，然后在该目录里 `python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .`。装完告诉用户：以后要先 `source ~/my_agents_fleet/.venv/bin/activate` 才能用这条命令。
3. **验证。** `my-agents-fleet --version` 能打印版本号即安装成功。
4. **建一支示例团队。** `my-agents-fleet init ~/my-fleet`，再 `my-agents-fleet inspect ~/my-fleet`。目标目录必须是新建或空的；如果 `~/my-fleet` 已存在且非空，跳过 `init` 或换一个名字。这两步不会调用任何 AI。
5. **看看用户有哪些 AI 工具。** 逐个检查 `claude`、`codex`、`grok`、`agy`、`pi` 是否在 PATH 里。
6. **汇报，然后停下。** 告诉用户三件事：装好了；哪几个 AI 工具可用；示例团队在 `~/my-fleet`，里面有 `reviewer` 和 `summarizer` 两个角色，默认都用 Claude Code。然后问用户想让谁干什么。
7. **不要做：** 不要替用户登录任何 AI 工具；不要创建或修改任何 API key；没有用户明确同意，不要发起真实的模型调用（会花钱）。

## 更多

- [配置说明](docs/configuration.md)：每个字段是什么意思
- [能力与边界](docs/capabilities.md)：各家工具的支持情况
- [模型发现](docs/model-discovery.md)：模型列表从哪来、多久更新
- [数据与安全](SECURITY.md)：本地存了什么，哪些别往公开仓库放
- [参与开发](CONTRIBUTING.md) · [架构说明](docs/architecture.md) · [更新记录](CHANGELOG.md)

许可证：[Apache-2.0](LICENSE)。各 AI 工具和服务遵循它们各自的条款。
