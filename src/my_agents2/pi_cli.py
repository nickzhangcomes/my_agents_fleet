"""PiAgent —— 本地 Pi CLI 后端（transport = CLI）。

    pi --mode json [-p] [--model provider/model] [--thinking L]
       [--append-system-prompt TEXT] [--session ID]

Pi 的 JSON 模式会输出 session / message_update / tool_execution_* / agent_settled
等 NDJSON 事件，本适配器把它们翻译成 my_agents2 的统一 Event。

mode 翻译：
    full     → 加载 Pi 的全部可用工具，并允许当前项目资源（--approve）
    readonly → 只把 read / grep / find / ls 四个只读工具交给模型，并忽略项目资源
               （--tools read,grep,find,ls + --no-approve）

⚠ readonly 是**工具白名单**，不是操作系统沙箱。模型看不到 shell / edit / write，
但 Pi 进程和用户级扩展本身仍运行在当前用户权限下；真要系统级硬隔离仍应选 codex。

模型名使用 Pi 的完整写法，例如：
    aliyun/kimi/kimi-k3
    aliyun/ZHIPU/GLM-5.3
    deepseek/deepseek-v4-pro

不传 model 时沿用 Pi 自己保存的默认 provider / model。
"""
from __future__ import annotations

import json
import os
from typing import Iterator, Optional, Sequence

from ._cli import stream_ndjson
from .base import MODE_READONLY, Agent, Caps, Event, RunHandle


READONLY_TOOLS = ("read", "grep", "find", "ls")
_PATH_KEYS = ("path", "file_path", "target_file", "notebook_path")


def _pi_env() -> dict:
    """Pi 的登录态和 provider 配置都在 ~/.pi/agent；原样保留当前环境。"""
    return dict(os.environ)


def _summary(value: object, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False, default=str)[:limit]


def _tool_command(args: object) -> str:
    """工具参数压成一行；写文件工具优先返回路径，供 artifacts 识别。"""
    if not isinstance(args, dict):
        return _summary(args, 300)
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("command", "query", "pattern"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return _summary(args, 300)


def _content_text(content: object) -> str:
    """Pi 的 content block / tool result → 可读纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _summary(content, 4000)
    out = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
            continue
        if not isinstance(block, dict):
            out.append(_summary(block, 1000))
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
        elif block.get("type") == "image":
            out.append(f"[image: {block.get('mimeType') or 'unknown'}]")
        else:
            out.append(_summary(block, 1000))
    return "\n".join(x for x in out if x)


def _message_blocks(message: object, block_type: str) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content if block_type == "text" else ""
    if not isinstance(content, list):
        return ""
    key = "thinking" if block_type == "thinking" else "text"
    return "".join(
        str(block.get(key) or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == block_type
    )


def _add_usage(total: dict, usage: object) -> None:
    """一轮可能经过多次模型调用（工具前后）；把每次 message usage 累加。"""
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if key == "cost" and isinstance(value, dict):
            dst = total.setdefault("cost", {})
            for cost_key, cost_value in value.items():
                if isinstance(cost_value, (int, float)):
                    dst[cost_key] = dst.get(cost_key, 0) + cost_value
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value


class PiAgent(Agent):
    """本地 Pi CLI 后端。支持 Pi 已配置的全部 provider 与模型。"""

    kind = "cli"
    registry_key = "pi"
    caps = Caps(
        readonly="tool-allowlist",
        effort=True,
        system_prompt=True,
        system_prompt_native=True,
        add_dirs=False,
        cwd=True,
        tool_events=True,
        thought_events=True,
        token_stream=True,
        structured_output=False,
        deny=(),
        cost=True,
        usage=True,
        note="模型写完整 provider/model；readonly 靠只读工具白名单，不是 OS 沙箱；"
             "不传 model 时沿用 Pi 自己的默认模型",
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
        name: str = "pi",
        token_stream: bool = False,
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,
        deny: Optional[Sequence[str]] = None,
        # ── 后端专属（走 extra= 传进来）──
        approve_project: Optional[bool] = None,
        pi_bin: str = "pi",
        extra_args: Optional[Sequence[str]] = None,
    ):
        super().__init__(
            mode=mode,
            cwd=cwd,
            model=model,
            effort=effort,
            timeout=timeout,
            system_prompt=system_prompt,
            add_dirs=add_dirs,
            name=name,
            token_stream=token_stream,
            tool_result_limit=tool_result_limit,
            style=style,
            summary_limit=summary_limit,
            output_schema=output_schema,
            deny=deny,
        )
        self.pi_bin = pi_bin
        self.extra_args = list(extra_args) if extra_args else []
        # 统一词汇包含 Codex 独有的 ultra；Pi CLI 的最高档是 max。
        # 这是 CLI 明确的上限，不是猜某个模型的能力，按库约定只向下夹一档。
        if self.effort == "ultra":
            self.effort = "max"
            self.ignored = self.ignored + ("Pi 不支持 effort=ultra，已夹到 max",)
        self.approve_project = (mode != MODE_READONLY) if approve_project is None else bool(approve_project)
        if self.mode == MODE_READONLY and self.approve_project:
            self.approve_project = False
            self.ignored = self.ignored + (
                "approve_project=True(readonly 下会加载项目代码，已强制关闭)",
            )

    def _build_argv(self, prompt: str, continuation: Optional[str]) -> list:
        argv = [self.pi_bin, "--mode", "json"]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--thinking", self.effort]
        if self.system_prompt:
            argv += ["--append-system-prompt", self.system_prompt]
        if continuation:
            argv += ["--session", continuation]

        # 后端专属参数先放，权限边界最后放：即使 extra_args 重复给了工具参数，
        # readonly 的最终白名单仍然压在最后。
        argv += self.extra_args
        if self.approve_project:
            argv.append("--approve")
        else:
            argv.append("--no-approve")
        if self.mode == MODE_READONLY:
            argv += ["--tools", ",".join(READONLY_TOOLS)]
        argv += ["-p", prompt]
        return argv

    def stream(
        self,
        prompt: str,
        *,
        continuation: Optional[str] = None,
        handle: Optional[RunHandle] = None,
    ) -> Iterator[Event]:
        cwd = self._resolved_cwd()
        argv = self._build_argv(prompt, continuation)
        shown = " ".join("<prompt>" if part == prompt else "<system_prompt>"
                         if part == self.system_prompt and self.system_prompt else part
                         for part in argv)
        yield self._start_log(shown, {"argv_redacted": shown, "cwd": cwd})

        session_id: Optional[str] = continuation
        final_text = ""
        usage_total: dict = {}
        final_error = ""
        result_emitted = False

        def result_event() -> Event:
            cost = usage_total.get("cost") if isinstance(usage_total.get("cost"), dict) else {}
            return Event(
                kind="result",
                text=final_text,
                raw={
                    "continuation": session_id,
                    "usage": usage_total or None,
                    "cost_usd": cost.get("total") if isinstance(cost, dict) else None,
                    "duration_ms": None,
                    "is_error": bool(final_error),
                },
            )

        for item in stream_ndjson(
            argv,
            cwd=cwd,
            env=_pi_env(),
            timeout=self.timeout,
            bin_name=self.pi_bin,
            log_unparsed=True,
            handle=handle,
        ):
            if isinstance(item, Event):
                yield item
                continue
            obj = item
            event_type = obj.get("type", "")

            if event_type == "session":
                session_id = obj.get("id") or session_id
                yield Event(kind="init", text=f"Pi session {session_id or '?'}", raw=obj)
                continue

            if event_type == "message_update":
                delta = obj.get("assistantMessageEvent") or {}
                delta_type = delta.get("type")
                text = delta.get("delta") or ""
                if self.token_stream and text:
                    if delta_type == "text_delta":
                        yield Event(kind="text", text=text, raw=obj)
                    elif delta_type == "thinking_delta":
                        yield Event(kind="thought", text=text, raw=obj)
                continue

            if event_type == "message_end":
                message = obj.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                text = _message_blocks(message, "text")
                thought = _message_blocks(message, "thinking")
                if text:
                    final_text = text
                    if not self.token_stream:
                        yield Event(kind="text", text=text, raw=obj)
                if thought and not self.token_stream:
                    yield Event(kind="thought", text=thought, raw=obj)
                _add_usage(usage_total, message.get("usage"))
                stop_reason = message.get("stopReason")
                if stop_reason in ("error", "aborted"):
                    final_error = message.get("errorMessage") or f"Pi {stop_reason}"
                elif stop_reason == "stop":
                    final_error = ""  # 自动重试后成功，清掉前一次临时错误
                continue

            if event_type == "tool_execution_start":
                yield Event(
                    kind="tool_use",
                    name=obj.get("toolName") or "tool",
                    command=_tool_command(obj.get("args")),
                    raw=obj,
                )
                continue

            if event_type == "tool_execution_end":
                result = obj.get("result")
                text = _content_text(result.get("content") if isinstance(result, dict) else result)
                event = self._tool_result_event(text, is_error=bool(obj.get("isError")), raw=obj)
                event.name = obj.get("toolName") or ""
                yield event
                continue

            if event_type == "agent_settled":
                if final_error:
                    yield Event(kind="error", text=final_error, raw=obj)
                yield result_event()
                result_emitted = True
                continue

            if event_type in (
                "compaction_start", "compaction_end", "auto_retry_start", "auto_retry_end",
                "summarization_retry_scheduled", "summarization_retry_start",
                "summarization_retry_end",
            ):
                yield Event(kind="log", text=f"[Pi {event_type}]", raw=obj)

        # 兼容没有 agent_settled 的旧版 Pi；正常 0.84+ 不走这里。
        if not result_emitted and (session_id or final_text or final_error):
            if final_error:
                yield Event(kind="error", text=final_error)
            yield result_event()
