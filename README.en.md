# my_agents_fleet

<p align="center"><strong>Manage Claude Code, Codex, Grok and other AI agents from one place: assign roles, hand out tasks, keep records.</strong></p>

<p align="center">
  <a href="README.md">中文</a> · English
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="macOS / Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey">
  <img alt="Status alpha" src="https://img.shields.io/badge/status-alpha-orange">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-green">
</p>

> More than one agent on your machine? This project turns them from a scattered bunch into a real team: **unified management, role assignment, task assignment**, working together on one project.

## What problem it solves

Using Claude Code or Codex today probably looks like this:

- Want Codex to write and Claude to review? Two windows, copy, paste, repeat.
- Every new terminal is a stranger. Where you left off yesterday, what it suggested last time: gone.
- Ten tasks later: which ones worked, which failed, what did it all cost? Nobody kept track.

That is a **temp worker**: called in, does the job, leaves. No name, no file. my_agents_fleet turns them into a proper team and does three things:

| Three things | In plain words | For example |
|---|---|---|
| **Unified management** | Every AI tool goes on one roster and answers to one command | Claude Code, Codex and Grok all on the roster; who does what, visible at a glance |
| **Role assignment** | Give each AI a name, a job, permissions and a model | "dev" is Codex and may edit code; "reviewer" is Claude and may only read |
| **Task assignment** | Hand a task to a named role; conversations have IDs so you can continue them; every run is recorded | "dev, build the login endpoint", "reviewer, find the holes". Who did what, did it work, what it cost: all on file |

## Install: just send the link to your AI

No commands to type. Open Claude Code (or Codex, or any AI assistant that can read a web page and run commands) and send it this:

```text
Install this project for me. Follow the section "Install instructions for AI agents" in its README, step by step:
https://github.com/nickzhangcomes/my_agents_fleet
```

It will download, install, set up a sample team, and come back to tell you it is done and which AI tools on your machine are ready to use. Installing is only download and configure. It does not run tasks on your AI accounts.

<details>
<summary>Prefer to do it yourself?</summary>

You need macOS or Linux and Python 3.11 or newer. Pick one:

```bash
# Recommended: install as a global command (uv or pipx, whichever you have)
uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"
```

```bash
# Or: clone the source and install it into its own environment
git clone https://github.com/nickzhangcomes/my_agents_fleet.git
cd my_agents_fleet
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
```

Run `my-agents-fleet --version`. If it prints a version number, you are set.

</details>

## Five minutes after installing

Everything below comes from one real run. Only my local paths and a few irrelevant fields were removed. The run itself was in Chinese, so the two replies are translated.

### 1. Create a small team

```bash
my-agents-fleet init ./my-fleet
my-agents-fleet inspect ./my-fleet
```

```text
fleet @ <your folder>/my-fleet
  reviewer       claude   -                      active   Review project notes and ident
  summarizer     claude   -                      active   Summarize the supplied text.
State directory: <your folder>/my-fleet/state
```

Two roles come built in: `reviewer` and `summarizer`. Both use Claude Code in read-only mode by default. Neither step calls an AI, and nothing is spent.

### 2. Hand out the first task

`my-fleet/data/sample.md` is a three-sentence project note that ships with the template. Ask the reviewer to look at it:

```bash
my-agents-fleet run ./my-fleet reviewer "Read data/sample.md. State the current status in one sentence, then suggest one next step." --session review-1
```

> **Status**: The prototype only has file import done. Search is still at the planning stage, and the next milestone is a small usability test.
>
> **Next step**: Don't rush into building search. Use the one path that already works, importing, to run that small usability test now (find 3–5 people and watch what they instinctively want to do right after importing). The reasoning chain: import is the only feature that works end to end → testing it gets you real user behavior after import → that behavior tells you directly what search needs to be, instead of building search on a guess and redoing it later.
>
> (A third paragraph about the limits of this advice is omitted.)

### 3. Keep talking. It remembers.

```bash
my-agents-fleet run ./my-fleet reviewer "Split the next step you just suggested into three steps, one sentence each, no explanation." --session review-1
```

> 1. Recruit 3–5 target users and book a 30-minute usability test slot.
> 2. Have them use only the existing import feature, and observe and record their first action after import completes.
> 3. Collect those actions, derive the concrete requirements search must meet, then decide how to build it.

"The step you just suggested": it knows which one. The trick is the `--session review-1` ID. Same ID, same conversation, whether a day or a week has passed.

### 4. Check the work log

```bash
my-agents-fleet runs ./my-fleet
```

```text
"agent": "reviewer",
"runs": 2,          # 2 tasks handed out
"ok": 2,            # both succeeded
"failed": 0,
"cost": 0.4819,     # two real Claude calls, USD 0.48 in total
"avg_ms": 22455     # 22 seconds each on average
```

If cost shows `null`, part of it could not be determined. That is not zero. Only `0` means zero.

## Example: one person, three AIs, one project

Say you are building a budgeting app and the code lives in `~/code/ledger`. Create a team next to it:

```bash
my-agents-fleet init ~/code/ledger-team
```

Replace `~/code/ledger-team/fleet.json` with the file below. Three roles, three different setups:

| Role | Who | Access | Job |
|---|---|---|---|
| `architect` | Claude Code, `effort` set to `high` | read-only | Break the requirement into steps, flag risks, write no code |
| `dev` | Codex | read-write | Write code and run tests inside the project folder |
| `reviewer` | Claude Code | read-only | Find bugs and security holes, never edit code |

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
      "intro": "Break the requirement into executable steps and flag risks. Do not write code."
    },
    "dev": {
      "backend": "codex",
      "mode": "full",
      "timeout": 900,
      "intro": "Implement the steps you are given, then run the tests."
    },
    "reviewer": {
      "backend": "claude",
      "intro": "Review code. List problems and risks by severity. Do not change code."
    }
  }
}
```

`global.md` holds the rules every role follows, for example "When unsure, say so instead of guessing."

**One warning:** a role with `mode` set to `full` really edits files. In that mode Codex runs with its sandbox and approval prompts switched off, and Claude Code skips permission checks. So give write access only to `dev`, only inside a project folder you trust, and keep every other role read-only.

Then hand out work at your own pace:

```bash
# 1. The architect thinks first (read-only, touches no code)
my-agents-fleet run ~/code/ledger-team architect "Read docs/login-spec.md. Break it into development steps and flag the risks." --session login

# 2. The dev builds it (read-write, really edits project files)
my-agents-fleet run ~/code/ledger-team dev "Read docs/login-spec.md and implement the login endpoint with tests. Make sure tokens expire." --session login

# 3. The reviewer picks it apart (read-only)
my-agents-fleet run ~/code/ledger-team reviewer "Review the new login endpoint in src/auth/. List problems by severity." --session login

# 4. Send the review back to the dev. Same ID, so it remembers what it just wrote
my-agents-fleet run ~/code/ledger-team dev "The reviewer says the wrong-password message reveals whether a user exists. Fix it and rerun the tests." --session login
```

Conversation IDs are kept per role: all three can use `login` without mixing.

You decide the order and whether to send work back. The tool makes no decisions and never feeds the architect's output to the dev on its own. You are the project manager. They are the team.

Each role can also set:

- `backend`: which vendor's tool
- `model`: which model; leave it out to use that tool's own default
- `effort`: how hard to think, a quick answer or a longer deliberation
- `mode`: `readonly` can look but not change files, `full` lifts that
- `workspace`: which folder to work in
- `timeout`: how many seconds one run may take

All fields are listed in [Configuration](docs/configuration.md). The `summarizer` in the starter template carries an `output_schema` (a fixed JSON shape for its reply). Grok and Pi do not support that yet, so remove that block if you move it to one of them.

Not sure which models and effort levels you can pick? Ask:

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

Two labels in that listing are still in Chinese: 数据源 means "source" and 默认 means "default".

## If you write a little Python: dispatch in batches

```python
from my_agents_fleet import load_fleet

fleet = load_fleet('~/code/ledger-team')

# After the dev is done, have two read-only roles look at once, each with its own result
results = fleet.dispatch([
    {'agent': 'reviewer',  'prompt': 'Review src/auth/. List problems by severity.'},
    {'agent': 'architect', 'prompt': 'Compare src/auth/ against docs/login-spec.md. What is missing?'},
], workers=2)
for r in results:
    print(r.ok, r.text)

# Continue one role's conversation
session = fleet.session('dev', 'login')
out = session.ask('Summarize the changes you just made as a commit message.')
print(out.ok, out.text)
```

One failure does not sink the others. The failed one keeps its record and reason in `r.failure`.

## Supported AI tools

| Tool | Command on your machine | Name in config |
|---|---|---|
| Claude Code | `claude` | `claude` |
| Codex CLI | `codex` | `codex` |
| Grok CLI | `grok` | `grok` |
| Antigravity CLI | `agy` | `agy` |
| Pi CLI | `pi` | `pi` |

Whatever you have installed works, and one is enough. **Logins, accounts, and billing stay with each tool.** This project never touches them.

## 🤖 Install instructions for AI agents

> If you are an AI agent (Claude Code, Codex, or similar) and a user sent you the link to this repository, follow these steps.

1. **Check the environment.** Run `python3 --version`; 3.11 or newer is required. The OS must be macOS or Linux. If either check fails, tell the user and stop. Do not force it.
2. **Install.** Try in this order and use the first one available. Do not install twice:
   - `uv` available: `uv tool install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - `pipx` available: `pipx install "git+https://github.com/nickzhangcomes/my_agents_fleet.git"`
   - Neither: `git clone https://github.com/nickzhangcomes/my_agents_fleet.git ~/my_agents_fleet`, then inside that directory run `python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .`. Afterwards tell the user they need `source ~/my_agents_fleet/.venv/bin/activate` before the command is available.
3. **Verify.** `my-agents-fleet --version` printing a version number means the install succeeded.
4. **Create a sample team.** `my-agents-fleet init ~/my-fleet`, then `my-agents-fleet inspect ~/my-fleet`. The target must be a new or empty directory; if `~/my-fleet` already exists and is not empty, skip `init` or pick another name. Neither step calls an AI.
5. **See which AI tools the user has.** Check whether each of `claude`, `codex`, `grok`, `agy`, `pi` is on PATH.
6. **Report, then stop.** Tell the user three things: it is installed; which AI tools are available; the sample team is at `~/my-fleet` with two roles, `reviewer` and `summarizer`, both using Claude Code by default. Then ask what they would like each role to do.
7. **Do not:** log the user into any AI tool; create or modify any API key; make real model calls without the user's explicit consent (they cost money).

## More

- [Configuration](docs/configuration.md): what every field means
- [Capabilities and limits](docs/capabilities.md): what each tool supports
- [Model discovery](docs/model-discovery.md): where the model list comes from and how it refreshes
- [Data and security](SECURITY.md): what is stored locally, and what not to publish
- [Contributing](CONTRIBUTING.md) · [Architecture](docs/architecture.md) · [Changelog](CHANGELOG.md)

Licensed under [Apache-2.0](LICENSE). The AI tools and services keep their own terms.
