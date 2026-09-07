"""CodexAgent —— 本地 codex CLI 后端（transport = CLI）。

    codex [--search] [--model M] [-c model_reasoning_effort=L]
          <沙箱/审批 由 mode 翻译> --cd <cwd> [--add-dir D ...]
          exec --json [--skip-git-repo-check] [--ignore-user-config] [...]
          [resume <thread_id>] <prompt>

mode 翻译：
    full     → --dangerously-bypass-approvals-and-sandbox（沙箱和审批全关）
    readonly → --sandbox read-only --ask-for-approval never
               ✅ 强度 = **操作系统沙箱**，现有后端里最硬的只读：写操作在系统层就被拒。

两处「能翻译就翻译」的妥协（对齐既定取舍，不报错）：
  1. **effort**：codex 没有 `--effort`，走 `-c model_reasoning_effort=<L>`（实测可用）。
     档位**per-model 不同**（gpt-5.6-sol 到 ultra，gpt-5.5 只到 xhigh），
     由 models.py 的词典按当前模型夹取 —— 这里不写任何映射规则。
  2. **system_prompt**：codex 没有追加系统提示的 flag（它只认 AGENTS.md）。
     这里拼成 **prompt 前缀**注入。语义上落在 user 轮而不是 system 轮，
     效果接近但不等价 —— 已写进 caps.note，`ag.explain()` 看得见。
"""
from __future__ import annotations

import json
import os
from typing import Iterator, Optional, Sequence

from ._cli import stream_ndjson
from .base import MODE_READONLY, Agent, Caps, Event, RunHandle


def _codex_env() -> dict:
    env = dict(os.environ)
    # 嵌套调用必须自己开线程；保留 CODEX_HOME 和登录态。
    env.pop("CODEX_THREAD_ID", None)
    env.setdefault("CODEX_CI", "1")
    return env


def _summary(value: object, limit: int = 400) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


class CodexAgent(Agent):
    """本地 codex CLI 后端。只读最硬（OS 沙箱），自带实时联网搜索。"""

    kind = "cli"
    registry_key = "codex"
    caps = Caps(
        readonly="os-sandbox",
        effort=True, system_prompt=True, system_prompt_native=False, add_dirs=True, cwd=True,
        tool_events=True, thought_events=True, structured_output=True,
        deny=("network",),   # 内建联网工具只有 --search，关掉即拒；shell/write 归 mode 的 OS 沙箱管
        cost=False, usage=True,
        note="effort 档位 per-model，查 ag.effort_options()；system_prompt 走 prompt 前缀注入；"
             "output_schema 走 --output-schema（只收文件路径，本库自动落临时文件）",
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
        name: str = "codex",
        token_stream: bool = False,      # codex 只能整块吐，收下但落不了地（记进 ignored）
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,
        deny: Optional[Sequence[str]] = None,
        # ── 后端专属（走 extra= 传进来）──
        web_search: bool = True,
        ignore_user_config: bool = True,
        ignore_rules: bool = False,
        skip_git_repo_check: bool = True,
        ephemeral: bool = False,
        codex_bin: str = "codex",
        extra_args: Optional[Sequence[str]] = None,
    ):
        super().__init__(mode=mode, cwd=cwd, model=model, effort=effort,
                         timeout=timeout, system_prompt=system_prompt,
                         add_dirs=add_dirs, name=name,
                         token_stream=token_stream, tool_result_limit=tool_result_limit,
                         style=style, summary_limit=summary_limit,
                         output_schema=output_schema, deny=deny)
        self.web_search = web_search and "network" not in self.deny
        self._schema_file: Optional[str] = None   # --output-schema 只收路径，懒落一次临时文件
        self.ignore_user_config = ignore_user_config
        self.ignore_rules = ignore_rules
        self.skip_git_repo_check = skip_git_repo_check
        self.ephemeral = ephemeral
        self.codex_bin = codex_bin
        self.extra_args = list(extra_args) if extra_args else []

    # ── 拼命令 ──
    def _global_argv(self, cwd: str) -> list:
        argv = [self.codex_bin]
        if self.web_search:
            argv.append("--search")
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            # self.effort 已经被 models 词典按当前模型夹过，直接用
            argv += ["-c", f"model_reasoning_effort={self.effort}"]
        if self.mode == MODE_READONLY:
            argv += ["--sandbox", "read-only", "--ask-for-approval", "never"]
        else:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        argv += ["--cd", cwd]
        for d in self.add_dirs:
            argv += ["--add-dir", d]
        return argv

    def _schema_path(self) -> str:
        """把统一入参的 schema(dict) 落成临时文件 —— codex 的 --output-schema 只认路径。
           一个 agent 实例只落一次；实例是无状态可并发复用的，同一份 schema 复用同一个文件没问题。"""
        if self._schema_file is None:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="codex-schema-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.output_schema, f, ensure_ascii=False)
            self._schema_file = path
        return self._schema_file

    def _exec_flags(self) -> list:
        argv = ["--json"]
        if self.skip_git_repo_check:
            argv.append("--skip-git-repo-check")
        if self.output_schema is not None:
            argv += ["--output-schema", self._schema_path()]
        if self.ignore_user_config:
            argv.append("--ignore-user-config")
        if self.ignore_rules:
            argv.append("--ignore-rules")
        if self.ephemeral:
            argv.append("--ephemeral")
        return argv + self.extra_args

    def _effective_prompt(self, prompt: str) -> str:
        """codex 没有系统提示 flag → 拼前缀（见模块 docstring 的妥协 2）。"""
        if not self.system_prompt:
            return prompt
        return f"{self.system_prompt}\n\n---\n\n{prompt}"

    def _build_argv(self, prompt: str, continuation: Optional[str], cwd: str) -> list:
        argv = self._global_argv(cwd) + ["exec"]
        if continuation:
            return argv + ["resume", *self._exec_flags(), continuation, prompt]
        return argv + [*self._exec_flags(), prompt]

    # ── 流式跑 ──
    def stream(self, prompt: str, *, continuation: Optional[str] = None,
               handle: Optional[RunHandle] = None) -> Iterator[Event]:
        cwd = self._resolved_cwd()
        full_prompt = self._effective_prompt(prompt)
        argv = self._build_argv(full_prompt, continuation, cwd)
        redacted = ["<prompt>" if part == full_prompt else part for part in argv]
        yield self._start_log(" ".join(redacted), {"argv_redacted": redacted, "cwd": cwd})

        thread_id: Optional[str] = continuation
        final_text = ""
        turn_failed = False

        for item in stream_ndjson(argv, cwd=cwd, env=_codex_env(),
                                  timeout=self.timeout, bin_name=self.codex_bin,
                                  log_unparsed=True, handle=handle):
            if isinstance(item, Event):
                yield item
                continue
            obj = item

            et = obj.get("type", "")
            if et == "thread.started":
                thread_id = obj.get("thread_id") or thread_id
                yield Event(kind="init", text=f"Codex thread {thread_id or '?'}", raw=obj)
                continue
            if et in ("turn.failed", "error"):
                turn_failed = True
                yield Event(kind="error",
                            text=obj.get("message") or _summary(obj.get("error")) or et, raw=obj)
                continue
            if et == "turn.completed":
                yield Event(kind="result", text=final_text,
                            raw={"continuation": thread_id,
                                 "usage": obj.get("usage"),
                                 "cost_usd": None, "duration_ms": None,
                                 "is_error": turn_failed})
                continue
            if not et.startswith("item."):
                continue

            it = obj.get("item") or {}
            itype = it.get("type", "")
            done = et == "item.completed"
            started = et == "item.started"

            if itype == "agent_message" and done:
                text = it.get("text") or ""
                if text.strip():
                    final_text = text          # 最后一条 agent_message 才是最终答案
                    yield Event(kind="text", text=text, raw=obj)
            elif itype == "reasoning" and done:
                text = it.get("text") or it.get("summary") or ""
                if text:
                    yield Event(kind="thought", text=_summary(text, 2000), raw=obj)
            elif itype == "command_execution":
                if started:
                    yield Event(kind="tool_use", name="Bash",
                                command=_summary(it.get("command")), raw=obj)
                elif done:
                    code = it.get("exit_code")
                    yield self._tool_result_event(
                        _summary(it.get("aggregated_output") or it.get("output"), 4000),
                        is_error=it.get("status") == "failed" or (code not in (None, 0)),
                        raw=obj)
            elif itype == "web_search" and done:
                action = it.get("action") or {}
                q = it.get("query") or action.get("query") or _summary(action)
                yield Event(kind="tool_use", name="WebSearch", command=_summary(q), raw=obj)
            elif itype == "mcp_tool_call":
                nm = it.get("tool") or it.get("name") or "MCP"
                if started:
                    yield Event(kind="tool_use", name=nm,
                                command=_summary(it.get("arguments")), raw=obj)
                elif done:
                    yield self._tool_result_event(
                        _summary(it.get("result"), 4000),
                        is_error=it.get("status") == "failed" or bool(it.get("error")),
                        raw=obj)
            elif itype in ("file_change", "file_changes") and done:
                yield Event(kind="tool_use", name="FileChange",
                            command=_summary(it.get("changes") or it), raw=obj)
            elif itype == "error" and done:
                # 非致命提示（例如 skill 描述被截断）→ 当日志，不打断
                yield Event(kind="log", text=it.get("message") or "Codex item error", raw=obj)
