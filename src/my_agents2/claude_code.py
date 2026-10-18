"""ClaudeCodeAgent —— 本地 claude code CLI 后端（transport = CLI）。

    claude -p "<prompt>" --output-format stream-json --verbose
           --permission-mode <由 mode 翻译> [--model M] [--effort L]

⚠️ effort 是 **per-model** 的：opus-5/sonnet-5 有 xhigh+max，opus-4-6 有 max 没 xhigh，
   haiku-4-5 **完全不支持** —— 由 models.py 的词典判定，不支持时压根不拼 --effort。
           [--allowedTools ...] [--disallowedTools ...]
           [--append-system-prompt S] [--add-dir D ...]
           [--mcp-config F --strict-mcp-config] [--resume <session_id>]

用本机已登录的 claude，走你自己的订阅额度，不需要 API key。

mode 翻译：
    full     → --permission-mode bypassPermissions（放开全部权限）
    readonly → --permission-mode default + 只读工具白名单
               ⚠ 强度 = **工具白名单**（进程内）：白名单外的工具压根不发给模型。
                 比不上 codex 的操作系统沙箱硬 —— 见 base.ENFORCEMENT。
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Iterator, Optional, Sequence

from ._cli import stream_ndjson
from .base import MODE_READONLY, Agent, Caps, Event, RunHandle

# readonly 白名单：能读能查能联网，碰不了写。改这里等于改 claude 的只读语义。
READONLY_TOOLS = (
    "Read", "Grep", "Glob", "WebFetch", "WebSearch",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(rg:*)", "Bash(find:*)",
    "Bash(git status:*)", "Bash(git log:*)", "Bash(git diff:*)",
)


def _sanitize_env() -> dict:
    """给子 claude 一个干净环境。

    若本进程恰好被「另一个 Claude Code 会话」拉起（例如你让 AI 帮你跑脚本），
    父进程会注入一批 CLAUDE_CODE_* 内部变量和内部 API 网关 ANTHROPIC_BASE_URL，
    子 claude 继承后会认证串味、报 401。这里剥掉，让子 claude 回退到本机正常登录。
    正常终端启动根本没有这些变量，所以不会动你自己设的 ANTHROPIC_API_KEY 等。

    CLAUDE_EFFORT 也一并剥掉：effort 由本库的 --effort 显式控制，
    不让父会话的档位悄悄渗进来。
    """
    env = dict(os.environ)
    inside_cc = bool(env.get("CLAUDE_CODE_ENTRYPOINT") or env.get("CLAUDE_CODE_SESSION_ID"))
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT"):
            env.pop(k, None)
    if inside_cc:
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(k, None)
    return env


# 统一的能力类别 → claude 的工具名方言。这张表就是「翻译」本身，调用方不用知道它存在。
DENY_TOOLS = {
    "shell":   ("Bash", "BashOutput", "KillShell"),
    "write":   ("Write", "Edit", "MultiEdit", "NotebookEdit"),
    "network": ("WebFetch", "WebSearch"),
    "spawn":   ("Task", "Agent"),
}


# 工具名 → 从 input 里取哪个字段当「命令摘要」
_TOOL_ARG = {
    "Bash": "command", "Read": "file_path", "Write": "file_path",
    "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
    "Glob": "pattern", "Grep": "pattern", "WebFetch": "url", "WebSearch": "query",
    "Task": "description", "TodoWrite": None,
}


def _tool_command(name: str, inp: dict) -> str:
    key = _TOOL_ARG.get(name)
    if key and isinstance(inp, dict) and inp.get(key):
        return str(inp[key])
    return json.dumps(inp, ensure_ascii=False)[:300] if inp else ""


class ClaudeCodeAgent(Agent):
    """本地 claude code CLI 后端。统一入参全支持，是能力最全的一个。"""

    kind = "cli"
    registry_key = "claude"
    caps = Caps(
        readonly="tool-allowlist",
        effort=True, system_prompt=True, system_prompt_native=True, add_dirs=True, cwd=True,
        tool_events=True, thought_events=False, token_stream=True,
        structured_output=True,
        deny=("shell", "write", "network", "spawn"),   # --disallowedTools 是硬拦，四类全覆盖
        cost=True, usage=True,
        note="token_stream=True 时走 --include-partial-messages，模型在思考的话还能顺带拿到 thought 事件；"
             "output_schema 走 --json-schema（只收内联 JSON，传路径会被它当非法 JSON 拒掉）",
    )

    def __init__(
        self,
        *,
        # ── 统一入参 ──
        mode: str = "full",
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: int = 3600,
        system_prompt: Optional[str] = None,
        add_dirs: Optional[Sequence[str]] = None,
        name: str = "claude",
        token_stream: bool = False,
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,
        deny: Optional[Sequence[str]] = None,
        # ── 后端专属（走 extra= 传进来）──
        mcp_config: Optional[str] = None,
        permission_mode: Optional[str] = None,       # 显式给就压过 mode 的翻译结果
        allowed_tools: Optional[Sequence[str]] = None,
        disallowed_tools: Optional[Sequence[str]] = None,
        claude_bin: str = "claude",
        extra_args: Optional[Sequence[str]] = None,
    ):
        super().__init__(mode=mode, cwd=cwd, model=model, effort=effort,
                         timeout=timeout, system_prompt=system_prompt,
                         add_dirs=add_dirs, name=name,
                         token_stream=token_stream, tool_result_limit=tool_result_limit,
                         style=style, summary_limit=summary_limit,
                         output_schema=output_schema, deny=deny)
        # 逐 token 模式下 thinking_delta 也会流出来 —— 这个实例确实有 thought 事件了
        if self.token_stream:
            self.caps = replace(self.caps, thought_events=True)
        self.mcp_config = mcp_config
        self.claude_bin = claude_bin
        self.extra_args = list(extra_args) if extra_args else []
        self.disallowed_tools = list(disallowed_tools) if disallowed_tools else []

        # mode → claude 方言（显式传的 permission_mode / allowed_tools 优先）
        if self.mode == MODE_READONLY:
            self.permission_mode = permission_mode or "default"
            self.allowed_tools = list(allowed_tools) if allowed_tools else list(READONLY_TOOLS)
        else:
            self.permission_mode = permission_mode or "bypassPermissions"
            self.allowed_tools = list(allowed_tools) if allowed_tools else None

    # ── 拼命令 ──
    def _build_argv(self, prompt: str, continuation: Optional[str]) -> list:
        argv = [self.claude_bin, "-p", prompt,
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", self.permission_mode]
        if self.token_stream:
            # 逐 token：吐 stream_event（Anthropic SSE），否则只有整块的 assistant 消息
            argv.append("--include-partial-messages")
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.allowed_tools:
            argv += ["--allowedTools", *self.allowed_tools]
        # 始终禁用 AskUserQuestion：headless 无人可答；再并上调用方指定的
        disallowed = list(self.disallowed_tools)
        for cat in self.deny:                    # 统一类别 → claude 工具名
            for t in DENY_TOOLS[cat]:
                if t not in disallowed:
                    disallowed.append(t)
        if "AskUserQuestion" not in disallowed:
            disallowed.append("AskUserQuestion")
        argv += ["--disallowedTools", *disallowed]
        if self.output_schema is not None:
            # 只收内联 JSON —— 传文件路径它会报 "not valid JSON: Unrecognized token '/'"
            argv += ["--json-schema", json.dumps(self.output_schema, ensure_ascii=False)]
        if self.system_prompt:
            argv += ["--append-system-prompt", self.system_prompt]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        if self.mcp_config:
            argv += ["--mcp-config", self.mcp_config, "--strict-mcp-config"]
        if continuation:
            argv += ["--resume", continuation]
        argv += self.extra_args
        if set(DENY_TOOLS).issubset(self.deny):
            # 四类能力都禁止时，不能再从全局 MCP 配置加载另一套执行/联网工具。
            if self.mcp_config:
                raise ValueError('禁止 shell/write/network/spawn 时不能加载 MCP 配置')
            argv += ['--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
        return argv

    # ── 流式跑 ──
    def stream(self, prompt: str, *, continuation: Optional[str] = None,
               handle: Optional[RunHandle] = None) -> Iterator[Event]:
        cwd = self._resolved_cwd()
        argv = self._build_argv(prompt, continuation)
        shown = " ".join([argv[0], "-p", "<prompt>"] + argv[3:])
        yield self._start_log(shown, {"argv": argv, "cwd": cwd})

        for item in stream_ndjson(argv, cwd=cwd, env=_sanitize_env(),
                                  timeout=self.timeout, bin_name=self.claude_bin,
                                  handle=handle):
            if isinstance(item, Event):
                yield item
                continue
            yield from self._translate(item)

    # ── stream-json 事件 → 归一化 Event（一条原始事件可能产 0..N 个）──
    def _translate(self, obj: dict) -> Iterator[Event]:
        t = obj.get("type")

        if t == "system":
            if obj.get("subtype") == "init":
                yield Event(kind="init",
                            text=f"模型 {obj.get('model', '?')} · 目录 {obj.get('cwd', '')}",
                            raw=obj)
            return

        if t == "stream_event":
            # 只有 --include-partial-messages 才有：Anthropic SSE 逐 token 增量
            ev = obj.get("event") or {}
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta" and d.get("text"):
                    yield Event(kind="text", text=d["text"], raw=ev)
                elif dt == "thinking_delta" and d.get("thinking"):
                    yield Event(kind="thought", text=d["thinking"], raw=ev)
            return

        if t == "assistant":
            for p in (obj.get("message") or {}).get("content", []):
                # 逐 token 模式下正文已经由 stream_event 吐过了，这里再吐一次就重了
                if p.get("type") == "text" and not self.token_stream \
                        and (p.get("text") or "").strip():
                    yield Event(kind="text", text=p["text"], raw=p)
                elif p.get("type") == "tool_use":
                    # tool_use 始终从这里出：增量里的 input_json_delta 是碎片，拼不出完整入参
                    nm = p.get("name", "")
                    yield Event(kind="tool_use", name=nm,
                                command=_tool_command(nm, p.get("input") or {}), raw=p)
            return

        if t == "user":
            for p in (obj.get("message") or {}).get("content", []):
                if p.get("type") == "tool_result":
                    c = p.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
                    elif not isinstance(c, str):
                        c = json.dumps(c, ensure_ascii=False)
                    yield self._tool_result_event(c or "", is_error=bool(p.get("is_error")), raw=p)
            return

        if t == "result":
            sub = obj.get("subtype")
            # 传了 --json-schema 时 claude 会额外给一份**已解析**的 structured_output
            # （result 里是同一份的字符串形态），直接透出去，使用方不用自己 json.loads
            yield Event(kind="result", text=obj.get("result") or "",
                        raw={"continuation": obj.get("session_id"),
                             "cost_usd": obj.get("total_cost_usd"),
                             "duration_ms": obj.get("duration_ms"),
                             "usage": obj.get("usage"),
                             "structured": obj.get("structured_output"),
                             "subtype": sub,
                             # ⚠ 两个来源都要看：实测存在 `is_error=true` 但 `subtype="success"`
                             #   的组合（如 API 400 —— 正文是 "API Error: 400 …"）。
                             #   只看 subtype 会把它当成正常产出返回，静默污染下游。
                             "is_error": bool(obj.get("is_error"))
                                         or sub not in (None, "success")})
            return
        # 其它类型忽略
