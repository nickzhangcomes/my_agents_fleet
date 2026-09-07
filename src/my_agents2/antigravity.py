"""AntigravityAgent —— 本地 Antigravity CLI（`agy`）后端（transport = CLI）。

    agy -p "<prompt>" --output-format stream-json
        --dangerously-skip-permissions        ← 恒开，原因见下
        [--mode plan] [--model M] [--effort low|medium|high]
        [--add-dir D ...] [--print-timeout 3600s]
        [--conversation <id>] [--json-schema F]

走本机 agy 登录（Google AI Pro），模型池横跨 Gemini / Claude / GPT-OSS：
    gemini-3.7-flash-{high,medium,low}   gemini-3.6-flash-{high,medium,low}
    gemini-3.5-flash-{high,medium,low}   gemini-3.1-pro-{high,low}
    claude-sonnet-4-6   claude-opus-4-6-thinking   gpt-oss-120b-medium
（`agy models` 看最新列表。不传 --model 就用 agy 会话里的当前模型。）

mode 翻译：
    full     → --dangerously-skip-permissions
    readonly → --dangerously-skip-permissions **+ --mode plan**
               ⚠ 强度 = **计划模式**（agent 层软约束，有 readonly 实施的后端里最弱）：
                 读照常，写变成一份待批准的计划、不落盘。实测 3/3 拦住，但它靠的是
                 模型自觉执行 plan 语义，**不是** codex 那种系统层硬拦。
                 副作用：写类任务的产出是「计划」而不是「结果」。

⚠️ **--disable-slash-commands 会让 readonly 失效**（实测，最坑的一条）：
   plan 模式是走 skill / slash 展开那条路实现的，一旦禁用展开，`--mode plan` 形同虚设、
   写操作真的落盘。对照实验：不带该 flag 4/4 拦住，带上 2/2 没拦住。
   所以本实现把它默认关掉，且 **readonly 下强制关闭并记进 `ag.ignored`**。

⚠️ **为什么 readonly 也必须带 --dangerously-skip-permissions**（实测，反直觉但关键）：
   agy 在 print 模式下的默认权限是 `request-review` —— headless 无人可批，
   于是**连读文件都被拦**，而且拦下来还返回 `status=SUCCESS` + **空 response**。
   所以只读**不能**靠「不给权限」实现，必须靠 --mode plan 拦写。

⚠️ **偶发空响应**：即便一切正常，agy 也可能回 `SUCCESS` + 空 response（权限卡住、
   任务被判定无需动作等）。本实现忠实透传（不编造错误），但会补一条 log 事件点名，
   免得静默。要重试由调用方自己包。

两处「能翻译就翻译」的妥协：
  1. **effort 编在 model slug 里**（gemini-3.7-flash-high）。词典按 family 夹取档位后
     合成完整 slug，此时**不再**拼 --effort。注意 gemini-3.1-pro 只有 high/low，没有 medium。
  2. 没有系统提示 flag → system_prompt 拼成 **prompt 前缀**注入（同 codex）。
"""
from __future__ import annotations

import json
import os
from typing import Iterator, Optional, Sequence

from ._cli import stream_ndjson
from .base import MODE_READONLY, Agent, Caps, Event, RunHandle

# 工具名 → 从 parameters 里取哪个字段当「命令摘要」
_TOOL_ARG = {
    "run_command": "CommandLine",
    "view_file": "AbsolutePath",
    "write_to_file": "TargetFile",
    "replace_file_content": "TargetFile",
    "multi_replace_file_content": "TargetFile",
    "sed_file": "TargetFile",
    "list_dir": "DirectoryPath",
    "grep_search": "Query",
    "find_by_name": "Pattern",
    "search_web": "query",
    "read_url_content": "Url",
    "open_browser_url": "Url",
}


def _agy_env() -> dict:
    """agy 的登录态在自己的配置目录，和 claude / grok 无关；顺手剥掉 CLAUDE_CODE_*。"""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT"):
            env.pop(k, None)
    return env


def _tool_command(name: str, params: dict) -> str:
    key = _TOOL_ARG.get(name)
    if key and isinstance(params, dict) and params.get(key):
        return str(params[key])
    return json.dumps(params, ensure_ascii=False)[:300] if params else ""


class AntigravityAgent(Agent):
    """本地 Antigravity CLI 后端。模型池最杂（Gemini + Claude + GPT-OSS），只读靠计划模式。"""

    kind = "cli"
    registry_key = "agy"
    caps = Caps(
        readonly="plan-mode",
        effort=True, system_prompt=True, system_prompt_native=False, add_dirs=True, cwd=True,
        tool_events=True, thought_events=False, structured_output=True,
        cost=False, usage=True,
        note="effort 档位编在 model slug 里，查 ag.effort_options()；system_prompt 走 prompt 前缀；"
             "readonly=计划模式，写类任务产出的是计划不是结果",
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
        name: str = "agy",
        token_stream: bool = False,      # agy 的 text_delta 是块级，收下但落不了地
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,
        deny: Optional[Sequence[str]] = None,   # 无按类别拒绝的机制，一律进 ignored
        # ── 后端专属（走 extra= 传进来）──
        agent_id: Optional[str] = None,          # --agent
        project: Optional[str] = None,           # --project
        disable_slash_commands: bool = False,    # ⚠ 和 readonly 冲突，见下
        agy_bin: str = "agy",
        extra_args: Optional[Sequence[str]] = None,
    ):
        super().__init__(mode=mode, cwd=cwd, model=model, effort=effort,
                         timeout=timeout, system_prompt=system_prompt,
                         add_dirs=add_dirs, name=name,
                         token_stream=token_stream, tool_result_limit=tool_result_limit,
                         style=style, summary_limit=summary_limit,
                         output_schema=output_schema, deny=deny)
        self.agent_id = agent_id
        self.project = project
        self.agy_bin = agy_bin
        self.extra_args = list(extra_args) if extra_args else []

        # ⚠ 实测：--disable-slash-commands 会让 --mode plan 失效（plan 走的就是 skill 展开那条路），
        #   readonly 下开它 = 只读保证直接消失、写操作真的落盘。这里硬性关掉并留痕。
        self.disable_slash_commands = disable_slash_commands
        if disable_slash_commands and self.mode == MODE_READONLY:
            self.disable_slash_commands = False
            self.ignored = self.ignored + ("disable_slash_commands(会让 readonly 失效，已强制关闭)",)

    def _effective_prompt(self, prompt: str) -> str:
        """agy 没有系统提示 flag → 拼前缀（同 codex 的妥协）。"""
        if not self.system_prompt:
            return prompt
        return f"{self.system_prompt}\n\n---\n\n{prompt}"

    # ── 拼命令 ──
    def _build_argv(self, prompt: str, continuation: Optional[str]) -> list:
        argv = [self.agy_bin, "-p", prompt,
                "--output-format", "stream-json",
                # 恒开：默认的 request-review 在 headless 下连读都拦，还静默回空（见模块 docstring）
                "--dangerously-skip-permissions",
                # agy 自己的 print 超时默认只有 5m，不顶上会先于我们的硬超时把任务砍了
                "--print-timeout", f"{self.timeout}s"]
        if self.mode == MODE_READONLY:
            argv += ["--mode", "plan"]          # 只读靠它拦写，不靠权限模式
        if self.model:
            argv += ["--model", self.model]
        # agy 把档位编在 model slug 里（gemini-3.7-flash-high）。词典已经帮我们合成好 slug 了，
        # 这种情况**不能**再拼 --effort，否则两处指定同一件事。
        if self.effort and not self.effort_in_model:
            argv += ["--effort", self.effort]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        if self.output_schema is not None:
            argv += ["--json-schema", json.dumps(self.output_schema, ensure_ascii=False)]
        if self.agent_id:
            argv += ["--agent", self.agent_id]
        if self.project:
            argv += ["--project", self.project]
        if self.disable_slash_commands:
            argv.append("--disable-slash-commands")
        if continuation:
            argv += ["--conversation", continuation]
        argv += self.extra_args
        return argv

    # ── 流式跑 ──
    def stream(self, prompt: str, *, continuation: Optional[str] = None,
               handle: Optional[RunHandle] = None) -> Iterator[Event]:
        cwd = self._resolved_cwd()
        argv = self._build_argv(self._effective_prompt(prompt), continuation)
        shown = " ".join([argv[0], "-p", "<prompt>"] + argv[3:])
        yield self._start_log(shown, {"argv": argv, "cwd": cwd})

        conv_id: Optional[str] = continuation
        for item in stream_ndjson(argv, cwd=cwd, env=_agy_env(),
                                  timeout=self.timeout, bin_name=self.agy_bin,
                                  handle=handle):
            if isinstance(item, Event):
                yield item
                continue
            for ev in self._translate(item):
                if ev.kind in ("init", "result"):
                    conv_id = (ev.raw or {}).get("continuation") or conv_id
                yield ev

    # ── stream-json 事件 → 归一化 Event ──
    def _translate(self, obj: dict) -> Iterator[Event]:
        kind = obj.get("event")

        if kind == "init":
            info = obj.get("init") or {}
            cid = obj.get("conversation_id")
            yield Event(kind="init",
                        text=f"agy 会话 {cid or '?'} · 目录 {info.get('cwd', '')} "
                             f"· 权限 {info.get('permission_mode', '?')}",
                        raw={"continuation": cid, **info})
            return

        if kind == "step_update":
            s = obj.get("step_update") or {}
            st = s.get("step_type")
            state = s.get("state")
            info = s.get("tool_info") or {}

            if st == "agent_response":
                delta = s.get("text_delta") or ""
                if delta.strip():
                    yield Event(kind="text", text=delta, raw=s)
            elif st == "tool":
                nm = s.get("tool_name") or info.get("name") or "tool"
                if state == "ACTIVE":
                    yield Event(kind="tool_use", name=nm,
                                command=_tool_command(nm, info.get("parameters") or {}), raw=s)
                elif state == "DONE":
                    out = info.get("output")
                    yield self._tool_result_event(
                        out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)[:4000],
                        is_error=bool(s.get("error") or info.get("error")), raw=s)
            # user_input / checkpoint / unknown → 无信息量，丢掉
            return

        if kind == "result":
            r = obj.get("result") or {}
            status = r.get("status")
            text = r.get("response") or ""
            dur = r.get("duration_seconds")
            if status == "SUCCESS" and not text.strip():
                # 实测存在的静默态：忠实透传 ok=True，但点名一句，别让它无声无息
                yield Event(kind="log",
                            text="⚠ agy 返回 SUCCESS 但 response 为空（权限卡住 / 判定无需动作都可能）。",
                            raw=r)
            yield Event(kind="result", text=text,
                        raw={"continuation": r.get("conversation_id"),
                             "usage": r.get("usage"),
                             "cost_usd": None,
                             "duration_ms": int(dur * 1000) if isinstance(dur, (int, float)) else None,
                             "num_turns": r.get("num_turns"),
                             "status": status,
                             "is_error": status not in (None, "SUCCESS")})
            return

        if kind == "error":
            yield Event(kind="error",
                        text=json.dumps(obj.get("error"), ensure_ascii=False)[:500] or "agy 报错",
                        raw=obj)
            return
        # 其它类型忽略
