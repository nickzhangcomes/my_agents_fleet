"""GrokAgent —— 本地 grok build CLI 后端（transport = CLI）。

    grok -p "<prompt>" --output-format streaming-json --no-auto-update
         --always-approve [--model M] [--reasoning-effort L]
         [--deny RULE ...] [--allow RULE ...] [--rules R]
         [--max-turns N] [-r <session_id>]

用本机已登录的 grok（`grok login`，缓存在 ~/.grok/auth.json）或 XAI_API_KEY。

mode 翻译：
    full     → --always-approve（全自动批准）
    readonly → --always-approve + --deny Bash --deny Edit --deny Write
               ⚠ 强度 = **语义拒绝规则**（进程内）。deny 优先级最高，全自动批准下仍生效。
                 必须用语义规则、不能按工具 id 拉黑：grok 的写工具有
                 search_replace / apply_patch / write_file / edit_file 一堆，枚举必漏。

—— 和 claude / codex 的硬差异（使用方最该知道的）——
1. **没有成本数字**：Result.cost_usd / duration_ms 恒为 None（usage 有，见下）。
2. **MCP 不支持单次注入**：只从 ~/.grok/config.toml 读（`grok mcp add` 管理），
   全局默认加载 —— 所以本类不收 mcp_config。add_dirs 同理，grok 没有对应 flag。

—— 工具事件（2026-08-15 起支持）——
上游现在会吐 `tool_call` / `tool_call_update`，本类翻译成统一的 tool_use / tool_result。
（老版本的 streaming-json 只有 thought / text / end / error，当年的结论是「看不见工具调用」，
  已作废；grok 装回旧版的话这两类事件不出现，解析分支自然空转，不会挂。）

三个坑，改这段前先看清楚：
  a. **一次调用会来好几条 update**：status 依次是 null → in_progress → completed，
     只在终态吐一次 tool_result，否则一个 Bash 能吐出三条重复输出。
  b. **update 里没有工具名**，只有 toolCallId —— 名字得从前面那条 tool_call 记下来。
  c. **`rawOutput["output"]` 是逐字节的整数数组**，几百行输出能撑成几十万个数字，
     千万别当文本用。可读输出在 `content[]` 和 `output_for_prompt` 里。

X 搜索这类「后端工具」还有个额外错位：查询词不在 tool_call 的 rawInput 里
（那儿只有 `{variant: XSearch}`），要等 update 的 `rawOutput.input` 才出现 ——
所以它的 tool_use.command 是空的，query 落在 tool_result 正文里。

另：`--always-approve` 是当前文档里的名字，老的 `--yolo` 已从 --help 移除（仍可用的兼容别名）。
2.0 用文档名，免得哪天上游删了别名。
"""
from __future__ import annotations

import json
import os
from typing import Iterator, List, Optional, Sequence

from ._cli import stream_ndjson
from .base import MODE_READONLY, Agent, Caps, Event, RunHandle

# readonly 的语义规则：一行堵住所有改写路径
READONLY_DENY = ("Bash", "Edit", "Write")

# 统一能力类别 → grok 的 --deny 规则。联网走它内建的 X 检索，没有独立可拒的工具名，故不认领 network。
DENY_RULES = {"shell": ("Bash",), "write": ("Edit", "Write")}

# tool_call_update 的终态。只在这些状态吐 tool_result —— 中间态（null / in_progress）会重复来
_TERMINAL = ("completed", "failed", "error", "cancelled", "aborted")
# rawInput 里的路径键。**必须排在 command 前面**：task.WRITE_TOOLS 靠 Event.command
# 直接当交付物路径用，写文件的调用要是把 command 摘成别的东西，artifacts 就退化成只能扫目录
_PATH_KEYS = ("file_path", "target_file", "path", "notebook_path")


def _tool_name(obj: dict) -> str:
    """工具名。X 搜索的 toolName 是 `X search:` —— 带个尾冒号，去掉。"""
    return (obj.get("toolName") or obj.get("title") or "tool").strip().rstrip(":").strip()


def _tool_command(raw_input) -> str:
    """rawInput → 一句人能看的入参摘要（写文件时 = 路径，见 _PATH_KEYS 的注释）。"""
    if not isinstance(raw_input, dict):
        return ""
    for k in _PATH_KEYS:
        v = raw_input.get(k)
        if isinstance(v, str) and v.strip():
            return v
    cmd = raw_input.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    # XSearch 那种 `{variant, backend}` 落到这里 —— 没有可读入参，原样压成一行
    return json.dumps(raw_input, ensure_ascii=False)[:300] if raw_input else ""


def _tool_output(obj: dict):
    """tool_call_update → (工具输出, 是否报错)。

    ⚠ 不碰 `rawOutput["output"]`（逐字节整数数组，见模块 docstring 坑 c）。
    """
    ro = obj.get("rawOutput") if isinstance(obj.get("rawOutput"), dict) else {}
    is_error = str(obj.get("status") or "").lower() in ("failed", "error", "cancelled")
    code = ro.get("exit_code")
    if isinstance(code, int) and code != 0:
        is_error = True
    if ro.get("timed_out"):
        is_error = True

    texts, diffs = [], []
    for c in obj.get("content") or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "diff":
            diffs.append(f"[diff] {c.get('path') or ''}")
        else:
            inner = c.get("content")
            t = inner.get("text") if isinstance(inner, dict) else None
            if t:
                texts.append(t)
    if texts:
        return "\n".join(texts), is_error

    out = ro.get("output_for_prompt")
    if isinstance(out, str) and out.strip():
        return out, is_error
    edits = ro.get("EditsApplied")
    if isinstance(edits, dict):
        v = edits.get("tool_output_for_prompt") or edits.get("tool_output_for_prompt_concise")
        if isinstance(v, str) and v.strip():
            return v, is_error
    if diffs:
        return "\n".join(diffs), is_error
    # 后端工具（X 搜索等）：结果直接进模型上下文，不回传给我们 —— 入参本身才是那条信息
    if ro.get("name") and ro.get("input"):
        return f'{ro["name"]} {ro["input"]}', is_error
    return "", is_error


def _grok_env() -> dict:
    """给子 grok 一个干净环境。

    grok 的认证（~/.grok/auth.json / XAI_API_KEY / GROK_*）和 claude 完全独立，
    这里顺手剥掉无害的 CLAUDE_CODE_* 即可，**保留** XAI_API_KEY / GROK_HOME。
    另外关掉自动更新，免得更新提示污染输出。
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") or k in ("CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT"):
            env.pop(k, None)
    env.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    return env


class GrokAgent(Agent):
    """本地 grok build CLI 后端。换个模型脑子用，但流里看不见工具调用。"""

    kind = "cli"
    registry_key = "grok"
    caps = Caps(
        readonly="deny-rules",
        effort=True, system_prompt=True, system_prompt_native=True, add_dirs=False, cwd=True,
        tool_events=True, thought_events=True, token_stream=True,
        deny=("shell", "write"),        # --deny 是硬拦；network 见 DENY_RULES 上方注释
        cost=False, usage=True,
        note="Result 没有 cost（usage 有）；不支持 add_dirs 和单次 MCP 注入",
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
        add_dirs: Optional[Sequence[str]] = None,   # 收下只为签名统一，实际忽略（见 caps）
        name: str = "grok",
        token_stream: bool = False,
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,                          # 收下只为签名统一，实际忽略（见 caps）
        deny: Optional[Sequence[str]] = None,
        # ── 后端专属（走 extra= 传进来）──
        allow: Optional[Sequence[str]] = None,       # 放行规则：Bash(npm*) / Read(src/**)
        deny_rules: Optional[Sequence[str]] = None,  # grok 原生拒绝规则（工具名/模式），
                                                     # 显式给就压过 mode 和统一入参 deny 的翻译结果
        system_prompt_override: Optional[str] = None,  # 整段替换系统提示（system_prompt 是追加）
        max_turns: Optional[int] = None,
        grok_bin: str = "grok",
        extra_args: Optional[Sequence[str]] = None,
    ):
        super().__init__(mode=mode, cwd=cwd, model=model, effort=effort,
                         timeout=timeout, system_prompt=system_prompt,
                         add_dirs=add_dirs, name=name,
                         token_stream=token_stream, tool_result_limit=tool_result_limit,
                         style=style, summary_limit=summary_limit,
                         output_schema=output_schema, deny=deny)
        self.allow = list(allow) if allow else []
        self.system_prompt_override = system_prompt_override
        self.max_turns = max_turns
        self.grok_bin = grok_bin
        self.extra_args = list(extra_args) if extra_args else []

        # 三个来源合成最终的 --deny 规则集，优先级从高到低：
        #   ① deny_rules  后端原生规则（显式给就全听它的）
        #   ② deny        统一能力类别（基类已按 caps 过滤，这里翻译成 grok 工具名）
        #   ③ mode=readonly 的默认只读规则
        # 注意：基类把 self.deny 设成了「类别元组」，这里要覆写成 grok 的规则列表 —— 顺序不能反。
        cats = self.deny
        if deny_rules:
            self.deny = list(deny_rules)
        else:
            rules = [t for c in cats for t in DENY_RULES.get(c, ())]
            if self.mode == MODE_READONLY:
                rules += [t for t in READONLY_DENY if t not in rules]
            self.deny = rules
        self.deny_capabilities = cats     # 保留类别原文，给 Run 记录/排查用

    # ── 拼命令 ──
    def _build_argv(self, prompt: str, continuation: Optional[str]) -> list:
        argv = [self.grok_bin, "-p", prompt,
                "--output-format", "streaming-json", "--no-auto-update",
                "--always-approve"]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--reasoning-effort", self.effort]
        for rule in self.allow:
            argv += ["--allow", rule]
        for rule in self.deny:
            argv += ["--deny", rule]
        if self.system_prompt:
            argv += ["--rules", self.system_prompt]
        if self.system_prompt_override:
            argv += ["--system-prompt-override", self.system_prompt_override]
        if self.max_turns is not None:
            argv += ["--max-turns", str(self.max_turns)]
        if continuation:
            argv += ["--resume", continuation]
        argv += self.extra_args
        return argv

    # ── 流式跑 ──
    def stream(self, prompt: str, *, continuation: Optional[str] = None,
               handle: Optional[RunHandle] = None) -> Iterator[Event]:
        cwd = self._resolved_cwd()
        argv = self._build_argv(prompt, continuation)
        shown = " ".join([argv[0], "-p", "<prompt>"] + argv[3:])
        yield self._start_log(shown, {"argv": argv, "cwd": cwd})

        # grok 的 thought / text 本来就是逐 token 流出来的。
        #   token_stream=False（默认）：把连续同类的合并成一个 Event，对齐 claude 的整块粒度
        #   token_stream=True         ：**原样直出**，chat box 才有打字机效果
        # ⚠ 合并模式下连续 text 会一直攒到「类型切换」或「end」才吐 —— 对实时渲染是致命的，
        #   所以做 chat box 必须开 token_stream。
        buf_kind: Optional[str] = None
        buf: List[str] = []
        final_text: List[str] = []
        tool_names: dict = {}        # toolCallId → 工具名（update 里没名字，坑 b）
        tool_done: set = set()       # 已吐过 tool_result 的 toolCallId（坑 a）
        usage_total: dict = {}       # usage 每轮来一条，累加成一份总账挂到 result 上

        def flush() -> Iterator[Event]:
            nonlocal buf_kind, buf
            if buf_kind and buf:
                yield Event(kind=buf_kind, text="".join(buf))
            buf_kind, buf = None, []

        for item in stream_ndjson(argv, cwd=cwd, env=_grok_env(),
                                  timeout=self.timeout, bin_name=self.grok_bin,
                                  handle=handle):
            if isinstance(item, Event):     # helper 报的错（超时 / 非零退出）
                yield from flush()
                yield item
                continue
            obj = item

            t = obj.get("type")
            if t in ("text", "thought"):
                chunk = obj.get("data", "")
                if t == "text":
                    final_text.append(chunk)
                if self.token_stream:
                    if chunk:
                        yield Event(kind=t, text=chunk, raw=obj)   # 原样直出，不攒
                    continue
                if buf_kind != t:
                    yield from flush()
                    buf_kind = t
                buf.append(chunk)
            elif t == "tool_call":
                yield from flush()
                name = _tool_name(obj)
                tool_names[obj.get("toolCallId")] = name
                yield Event(kind="tool_use", name=name,
                            command=_tool_command(obj.get("rawInput")), raw=obj)
            elif t == "tool_call_update":
                cid = obj.get("toolCallId")
                if str(obj.get("status") or "").lower() not in _TERMINAL or cid in tool_done:
                    continue                      # 中间态 / 重复终态，都不吐（坑 a）
                tool_done.add(cid)
                yield from flush()
                text, is_err = _tool_output(obj)
                ev = self._tool_result_event(text, is_error=is_err, raw=obj)
                ev.name = tool_names.get(cid, "")
                yield ev
            elif t == "usage":
                u = obj.get("usage")
                if isinstance(u, dict):
                    for k, v in u.items():
                        if isinstance(v, int):
                            usage_total[k] = usage_total.get(k, 0) + v
                continue                          # 攒起来，随 result 一次性给出
            elif t == "end":
                yield from flush()
                yield Event(kind="result", text="".join(final_text),
                            raw={"continuation": obj.get("sessionId"),
                                 "stop_reason": obj.get("stopReason"),
                                 "request_id": obj.get("requestId"),
                                 "cost_usd": None, "duration_ms": None,
                                 "usage": usage_total or None,
                                 "is_error": False})
            elif t == "error":
                yield from flush()
                yield Event(kind="error", text=obj.get("message") or "grok 报错", raw=obj)
            elif t == "available_commands":
                continue                     # 开场的工具/命令清单，几千字符，没信息量
            else:
                # max_turns_reached / auto_compact_* / 未知类型 → 当日志透出，不打断
                yield from flush()
                yield Event(kind="log", text=f"[{t}] " + json.dumps(obj, ensure_ascii=False)[:300],
                            raw=obj)

        yield from flush()
