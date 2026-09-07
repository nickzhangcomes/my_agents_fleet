"""my_agents2 本地 CLI 契约层 —— 统一入参 + 统一事件 + 能力自述。

和 1.0 的四处不同（其余保持同构，Event / Result / Conversation 语义不变）：

1. **统一入参**：五个 CLI 后端收同一套参数，换后端零改调用代码 ——
   `mode / cwd / model / effort / timeout / system_prompt / add_dirs`。
   后端专属参数（mcp_config / output_schema / max_turns …）走 `extra={...}`。

2. **权限只有两档**：`mode="full"`（放开该后端全部权限）| `mode="readonly"`（只读）。
   各后端翻译成自己的方言，调用方不用记 permission_mode / sandbox / yolo 三套词汇。

3. **翻译不了就走默认，不报错**（既定取舍）。但**留痕可查**：落不了地的参数记在
   `ag.ignored` 里，并写进每次 stream 的第一条 log 事件 —— 静默降级不等于无声无息。

4. **失败路径是诚实的**：超时 / 进程非零退出 / 中途崩溃都会得到 `ok=False`；
   见过 error 之后不会被后到的 result 洗回 True；没拿到 result 事件时，
   已经流出来的正文也不会丢。

⚠️ **不同后端的 readonly 强度不一样**（`ag.caps.readonly` 可查，`ag.explain()` 人话版）：
      codex  = 操作系统沙箱   ← 最硬，系统层拒绝写
      claude = 工具白名单     ← 进程内，白名单外的工具不发给模型
      pi     = 工具白名单     ← 进程内，只给 read/grep/find/ls
      grok   = 语义拒绝规则   ← 进程内，按 Bash/Edit/Write 语义硬拦
      agy    = 计划模式       ← agent 层软约束
"""
from __future__ import annotations

import json
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

from .models import (EFFORT_ORDER, ModelInfo, effort_options, list_models, models_status,
                     resolve_effort, resolve_model_slug)
from .task import (STYLE_POSTURE, STYLES, Artifact, RunStats, WRITE_TOOLS,
                   build_task_prompt, diff_snapshots, merge_artifacts, real, snapshot)

# ── 权限：只有两档 ──
MODE_FULL = "full"
MODE_READONLY = "readonly"
MODES = (MODE_FULL, MODE_READONLY)

# ── effort：统一词汇 = 各家实际档位的并集（codex 的 gpt-5.6-sol 一直到 ultra）──
# ⚠ 这只是**拼写校验**用的全集，不代表哪个模型都支持这么多档。
#   真实档位是 per-model 的，查 models.py 的词典（ag.effort_options()）。
EFFORT_LEVELS = EFFORT_ORDER

# ── readonly 的实施方式：名字统一了，强度没统一，使用方必须知道 ──
ENFORCEMENT = {
    "os-sandbox": "操作系统沙箱（最硬：系统层拒绝写）",
    "tool-allowlist": "工具白名单（进程内：白名单外的工具不发给模型）",
    "deny-rules": "语义拒绝规则（进程内：按 Bash/Edit/Write 语义硬拦）",
    "plan-mode": "计划模式（agent 层软约束，四种里最弱：读照常，写变成待批准的计划；靠模型自觉，非硬拦）",
    "none": "无（该后端没有权限概念，mode 被忽略）",
}

# ── 能力拒绝：统一词汇 ──
# 为什么用「类别」不用工具名：工具名是方言（claude 叫 WebFetch，grok 叫别的），
# 调用方不该背方言。类别是多个后端共有的概念，各后端自己翻译。
# 和 mode 的关系：mode 管「能不能写」，deny 管「能不能干某一类事」——
# 只读不等于安全：readonly 下 WebFetch 照样能把上下文里的东西发出去。
DENY_CAPABILITIES = ("shell", "write", "network", "spawn")
DENY_MEANING = {
    "shell": "执行命令",
    "write": "写文件",
    "network": "联网取/发（外发通道）",
    "spawn": "派生子 agent",
}

# 统一入参清单（分界线：多个后端共有的概念才进来）
UNIFIED_PARAMS = ("mode", "cwd", "model", "effort", "timeout", "system_prompt",
                  "add_dirs", "output_schema", "deny")


def normalize_schema(value) -> Optional[dict]:
    """把 output_schema 归一成 dict —— 收 dict / JSON 串 / 文件路径三种写法。

    为什么要归一：各后端的方言恰好相反 —— claude 的 `--json-schema` 只收**内联 JSON**
    （传路径会报 "not valid JSON: Unrecognized token '/'"），codex 的 `--output-schema`
    只收**文件路径**。使用方不该记这个，统一收 dict，各后端自己渲染成自己要的形态。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    s = str(value).strip()
    if s.startswith("{"):
        return json.loads(s)                       # 内联 JSON 串
    p = Path(os.path.expanduser(s))
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))   # 文件路径
    raise ValueError(f"output_schema 既不是 JSON 也不是存在的文件: {s[:80]!r}")


@dataclass(frozen=True)
class Caps:
    """后端能力自述 —— 使用方据此判断「这个后端到底能不能干这事」，不用去翻源码。

        ag = get_agent("grok")
        if not ag.caps.tool_events:
            print("这个后端看不见工具调用，别指望做进度界面")
        if not ag.caps.token_stream:
            print("这个后端只能整块吐，chat box 做不出打字机效果")
    """
    readonly: str = "none"          # ENFORCEMENT 的 key：只读靠什么实施
    effort: bool = False            # 能不能调推理档位
    system_prompt: bool = False     # 能不能注入系统提示 / 人设
    system_prompt_native: bool = False  # 是真的系统轮，还是拼成 user 轮前缀（人设强度差一档）
    add_dirs: bool = False          # 能不能追加可访问目录
    cwd: bool = False               # 有没有工作目录概念
    tool_events: bool = False       # 流里看得见 tool_use / tool_result 吗
    thought_events: bool = False    # 流里看得见推理过程吗
    token_stream: bool = False      # 能不能逐 token 吐（chat box 打字机效果）
    structured_output: bool = False # 能不能强制产出符合 JSON Schema（Result.structured）
    deny: tuple = ()                # 这个后端**真能硬拦**哪几类能力，见 DENY_CAPABILITIES
    cost: bool = False              # Result.cost_usd 有值吗
    usage: bool = False             # Result.usage 有值吗
    note: str = ""                  # 一句话补充说明（翻译上的妥协写在这）


class RunHandle:
    """一次运行的把手 —— 给**别的线程**一个中止入口（chat box 的「停止」按钮）。

    `stream()` + `break` 只能在消费循环**内部**中止；UI 上点停止是另一个线程，
    没有这个把手就够不着正在跑的子进程。

        h = RunHandle()
        # 渲染线程
        for ev in ag.stream(prompt, handle=h): render(ev)
        # UI 线程
        h.stop()

    多轮场景直接用 `Conversation.stop()`，它内部就是这个。
    """

    def __init__(self) -> None:
        self._killer = None          # 无参可调用：终止本次运行
        self._stopped = False
        self._lock = threading.Lock()

    def _attach(self, killer) -> None:
        """后端在真正跑起来后登记「怎么杀我」。"""
        with self._lock:
            self._killer = killer
            if self._stopped:        # stop() 先于进程起来 → 一起来就杀
                self._fire()

    def _fire(self) -> None:
        try:
            if self._killer:
                self._killer()
        except Exception:  # noqa: BLE001 —— 中止是尽力而为，别把异常抛给调用方
            pass

    def stop(self) -> None:
        """中止这次运行。可从任意线程调用，可重复调用。"""
        with self._lock:
            self._stopped = True
            self._fire()

    @property
    def stopped(self) -> bool:
        return self._stopped


# ── 归一化事件：所有后端的 stream() 都只吐这几种 kind ──
@dataclass
class Event:
    """一次 agent 运行过程中的一个事件。所有后端都翻译成这套统一 kind。"""
    kind: str                              # init | text | thought | tool_use | tool_result | result | error | log
    text: str = ""                         # text/result 正文；error 消息；tool_result 输出
    name: str = ""                         # tool_use 的工具名
    command: str = ""                      # tool_use 的入参摘要（命令 / 路径 / query）
    is_error: bool = False                 # tool_result 是否报错
    truncated: bool = False                # text 被 tool_result_limit 截断过
    full_len: int = 0                      # 截断前的原始长度（truncated 时才有意义）
    raw: Optional[dict] = None             # 原始事件，需要时下钻

    def __repr__(self) -> str:
        head = self.name or (self.text[:50] if self.text else "")
        return f"<Event {self.kind} {head!r}>"


@dataclass
class Result:
    """一次 ask() 的聚合结果。"""
    text: str = ""                         # 最终结果文本
    continuation: Optional[str] = None     # 续接句柄：对应本地 CLI 的会话 id
    ok: bool = True
    error: str = ""
    timed_out: bool = False                # 是被超时杀掉的，还是自己跑挂的 —— 区分得开
    structured: Optional[dict] = None      # 传了 output_schema 才有：已解析的结构化产出
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    usage: Optional[dict] = None
    events: List[Event] = field(default_factory=list)   # 全过程事件，复盘用

    @property
    def session_id(self) -> Optional[str]:   # 习惯叫法的别名
        return self.continuation


@dataclass
class TaskResult(Result):
    """`run()` 的结果 —— 给 team leader 看的交接单。

    和 Result 的区别在**取舍**：Result 是「把能给的都给你」，TaskResult 是
    「只给 leader 该进 context 的」——
        summary   一句话，leader 读这个
        artifacts 交付物**路径**清单，不是内容
        log_path  全过程落盘位置，出事了才去读
        events    默认**空**（都在 log_path 里），省得长任务把 leader 的内存撑爆
    """
    status: str = "done"                    # done | partial | failed | stopped
    summary: str = ""                       # 短摘要（= text 的截断版）
    artifacts: List["Artifact"] = field(default_factory=list)
    log_path: Optional[str] = None
    workspace: Optional[str] = None
    stats: Optional["RunStats"] = None

    def __repr__(self) -> str:
        return (f"<TaskResult {self.status} artifacts={len(self.artifacts)} "
                f"summary={self.summary[:40]!r}>")


class Agent(ABC):
    """本地 CLI agent 的统一接口。

    子类只需实现 stream()：把自己后端的流式输出翻译成归一化 Event。
    ask() / conversation() 由基类基于 stream() 默认实现，子类不用重写。
    """
    kind: str = "abstract"     # 抽象基类；具体本地 CLI 后端声明为 cli
    caps: Caps = Caps()        # 子类覆盖，声明自己的能力
    registry_key: str = ""     # 在 models.json 词典里的键；空 = 这个后端没有模型词典

    def __init__(
        self,
        *,
        mode: str = MODE_FULL,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: int = 3600,
        system_prompt: Optional[str] = None,
        add_dirs: Optional[Sequence[str]] = None,
        name: str = "agent",
        token_stream: bool = False,
        tool_result_limit: int = 0,
        style: Optional[str] = None,
        summary_limit: int = 300,
        output_schema=None,
        deny: Optional[Sequence[str]] = None,
    ):
        # 这几个是「写错了」，不是「后端不支持」—— 该炸就炸，别静默
        if mode not in MODES:
            raise ValueError(f"mode 只能是 {MODES}，收到 {mode!r}")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ValueError(f"effort 只能是 {EFFORT_LEVELS}，收到 {effort!r}")
        if style is not None and style not in STYLES:
            raise ValueError(f"style 只能是 {STYLES} 或 None，收到 {style!r}")
        bad = [d for d in (deny or []) if d not in DENY_CAPABILITIES]
        if bad:
            raise ValueError(f"deny 只能是 {DENY_CAPABILITIES} 里的，收到 {bad}")

        self.mode = mode
        self.cwd = cwd or str(Path.home())
        self.timeout = timeout
        # 写错的 schema 该炸（同 mode/effort/style 一档）；后端不支持才是「落不了地」，见下面 ignored
        self.output_schema = normalize_schema(output_schema)
        # 只保留这个后端**真能硬拦**的类别；拦不住的进 ignored（下面），绝不假装拦住了
        wanted = tuple(dict.fromkeys(deny or ()))
        self.deny = tuple(d for d in wanted if d in self.caps.deny)
        self.deny_unenforced = tuple(d for d in wanted if d not in self.caps.deny)

        # ── 模型 + effort：查词典，按这个模型的真实档位落地 ──
        # 不写任何映射规则。词典说什么就是什么；词典没有就透传，绝不假装懂。
        self.requested_effort = effort
        if self.registry_key:
            # 顺序要紧：**先**按这个模型（或 agy 的 family）的真实档位夹取，
            # **再**用夹完的档位合成 slug。反过来会得到 slug=-high 但档位=low 的自相矛盾。
            self.effort, effort_note = resolve_effort(self.registry_key, model, effort)
            self.model, self.effort_in_model = resolve_model_slug(
                self.registry_key, model, self.effort)
        else:
            self.model = model
            self.effort, effort_note = effort, ""
            self.effort_in_model = False
        self.style = style
        self.summary_limit = summary_limit
        # 姿态语句追加进 system_prompt → ask / stream / run 一律生效，不用额外机制
        if style:
            posture = STYLE_POSTURE[style]
            system_prompt = f"{system_prompt}\n\n{posture}" if system_prompt else posture
        self.system_prompt = system_prompt
        self.add_dirs = list(add_dirs) if add_dirs else []
        self.name = name
        # 逐 token 吐（chat box 打字机效果）；后端不支持就照常整块吐
        self.token_stream = bool(token_stream) and self.caps.token_stream
        # tool_result 正文截断长度，0 = 不截（chat box 里工具输出动辄几百行）
        self.tool_result_limit = int(tool_result_limit or 0)

        # 传了、但这个后端落不了地的统一参数 → 静默走默认（既定取舍），但记下来可查
        ignored: List[str] = []
        if self.registry_key:
            catalog = models_status(self.registry_key)
            if catalog['stale']:
                ignored.append('模型词典未确认最新：' + '；'.join(catalog['stale_reasons']))
        if mode == MODE_READONLY and self.caps.readonly == "none":
            ignored.append("mode=readonly")
        if effort and not self.caps.effort:
            ignored.append("effort")
        elif effort_note:
            ignored.append(effort_note)      # 档位被词典夹过 / 该模型不支持 —— 留痕可查
        if system_prompt and not self.caps.system_prompt:
            ignored.append("system_prompt")
        if self.add_dirs and not self.caps.add_dirs:
            ignored.append("add_dirs")
        if cwd and not self.caps.cwd:
            ignored.append("cwd")
        if token_stream and not self.caps.token_stream:
            ignored.append("token_stream(该后端只能整块吐)")
        if self.output_schema is not None and not self.caps.structured_output:
            # 不支持就别拼进命令行（会当成未知 flag 炸掉），退回「靠提示词求它输出 JSON」的老路
            ignored.append("output_schema(该后端不支持结构化输出)")
            self.output_schema = None
        if self.deny_unenforced:
            # ⚠ 这条比别的 ignored 更要紧：拦不住的是**安全边界**，不是效果打折。
            #    使用方（如 my_agents_fleet 的 trust）该据此拒绝派活，而不是照跑。
            ignored.append(f"deny={list(self.deny_unenforced)}(该后端拦不住这几类)")
        self.ignored: tuple = tuple(ignored)

    # ── 模型词典（使用方按自己诉求挑入参用这两个）──
    def list_models(self) -> List[ModelInfo]:
        """这个后端有哪些模型可选。默认检查模型表有效期，必要时刷新。

            for m in get_agent("codex").list_models():
                print(m.slug, m.efforts)
        """
        return list_models(self.registry_key) if self.registry_key else []

    def effort_options(self, model: Optional[str] = None) -> Optional[Tuple[str, ...]]:
        """某个模型能开哪些 effort 档（省略 model 就用本实例当前的）。

            None  未知     ()  明确不支持     (...)  真实档位
        """
        if not self.registry_key:
            return None
        return effort_options(self.registry_key, model if model is not None else self.model)

    # ── 能力自述 ──
    def explain(self) -> str:
        """一句话说清这个实例的权限强度和被忽略的参数。

        每次 stream() 的第一条 log 事件都会带上它 —— 使用方想不知道都难。
        """
        bits = [f"{self.name}({self.kind}) mode={self.mode}"]
        if self.mode == MODE_READONLY:
            bits.append("只读靠 " + ENFORCEMENT.get(self.caps.readonly, self.caps.readonly))
        if self.effort:
            bits.append(f"effort={self.effort}")
        if self.ignored:
            bits.append("该后端忽略了: " + ", ".join(self.ignored))
        if self.caps.note:
            bits.append(self.caps.note)
        return " · ".join(bits)

    # ── 子类实现 ──
    @abstractmethod
    def stream(self, prompt: str, *, continuation: Optional[str] = None,
               handle: Optional[RunHandle] = None) -> Iterator[Event]:
        """喂一句话，流式吐归一化 Event。子类必须实现。

        continuation: 上一轮 Result.continuation，传入即续接同一会话（多轮）。
        handle:       给别的线程留的中止入口，见 RunHandle。
        """
        raise NotImplementedError

    # ── 基类默认实现 ──
    def ask(self, prompt: str, *, continuation: Optional[str] = None,
            handle: Optional[RunHandle] = None) -> Result:
        """一次性拿结果：内部跑 stream() 收尾，聚合成 Result。"""
        res = Result()
        streamed: List[str] = []
        saw_result = False

        for ev in self.stream(prompt, continuation=continuation, handle=handle):
            res.events.append(ev)
            if ev.kind == "text":
                streamed.append(ev.text)
            elif ev.kind == "result":
                saw_result = True
                r = ev.raw or {}
                res.text = ev.text
                res.continuation = r.get("continuation") or res.continuation
                res.cost_usd = r.get("cost_usd", res.cost_usd)
                res.duration_ms = r.get("duration_ms", res.duration_ms)
                res.usage = r.get("usage", res.usage)
                res.structured = r.get("structured", res.structured)
                if r.get("is_error"):
                    res.ok = False          # 只往 False 掉，见过 error 就不会被洗回 True
            elif ev.kind == "error":
                res.ok = False
                res.error = ev.text or res.error
                if (ev.raw or {}).get("timed_out"):
                    res.timed_out = True

        # 中途崩了拿不到 result 事件 → 至少把已经流出来的正文保住，别返回空字符串
        if not saw_result and streamed:
            res.text = "".join(streamed)

        # 兜底：要了 schema、后端却没单独回一份解析好的（codex/agy 只把 JSON 塞在正文里）
        # → 在这里统一解析一次，使用方永远只看 res.structured，不用管是哪个后端。
        if self.output_schema is not None and res.structured is None and res.text:
            try:
                v = json.loads(res.text.strip())
                if isinstance(v, dict):
                    res.structured = v
            except (ValueError, TypeError):
                pass                        # 解析不出来就留 None —— 由使用方决定这算不算失败
        return res

    def conversation(self) -> "Conversation":
        """开一个多轮会话，自动记住并续接 continuation。chat box 用它。"""
        return Conversation(self)

    def task(self, *, workspace: Optional[str] = None,
             context_dirs: Sequence[str] = ()) -> "TaskSession":
        """开一个可返工的任务闭环（`run()` 的多轮版）。leader 派活用它。"""
        return TaskSession(self, workspace=workspace, context_dirs=context_dirs)

    # ── 编队派活：leader 用这个 ──
    def run(
        self,
        brief: str,
        *,
        workspace: Optional[str] = None,
        context_dirs: Sequence[str] = (),
        continuation: Optional[str] = None,
        handle: Optional[RunHandle] = None,
        keep_events: bool = False,
    ) -> TaskResult:
        """派一个活，拿一张**交接单**（不是一堆正文）。

            r = ag.run("实现登录接口", workspace="/runs/t01")
            r.status      # done | partial | failed | stopped
            r.summary     # 一句话
            r.artifacts   # [<Artifact written /runs/t01/auth.py 2048B via both>]
            r.log_path    # 全过程在这，出事了才去读

        和 ask() 的区别：ask 是裸聚合（全给你），run 是**为 leader 的 context 做减法**——
        过程落盘、正文截断、交付物只给路径。长任务 / 多子 agent 编队用这个。

        workspace: 交付目录。库负责建（**不静默回退**）+ 跑前跑后快照 diff。
        context_dirs: 参考资料目录，挂给 agent 自己去读（传路径，不是把内容塞 prompt）。
        keep_events: 默认 False —— 事件只落盘不进内存。要复盘就读 log_path。
        """
        import copy

        # 全程 realpath：软链两边不一致会让同一个文件被算成两件交付物（见 task.real）
        if workspace:
            os.makedirs(workspace, exist_ok=True)   # 先建再解链，否则软链解不出来
        ws = real(workspace) if workspace else None
        ctx = [real(d) for d in context_dirs]

        # 不改 self：agent 实例要能被并发复用，每次 run 走自己的浅拷贝
        runner = self
        if ws or ctx:
            runner = copy.copy(self)
            if ws:
                runner.cwd = ws                  # 上面已 makedirs：建不出来就抛错，绝不像 cwd 那样偷偷回退 home
            if ctx:
                runner.add_dirs = list(self.add_dirs) + ctx

        before = snapshot(ws)
        prompt = build_task_prompt(brief, ws, ctx, self.style, self.summary_limit)
        log_path = os.path.join(ws, ".agent-log.jsonl") if ws else None

        stats = RunStats()
        event_paths: List[str] = []
        streamed: List[str] = []
        res = TaskResult(workspace=ws, log_path=log_path)
        saw_result = False

        # 追加而不是覆盖：返工闭环会在同一个 workspace 里跑好几轮，
        # 用 "w" 会让最后一轮把前面几轮的日志抹掉 —— 而排查「前几轮为什么没过」
        # 恰恰是返工场景最需要的。每轮开头插一条分隔记录。
        log_fh = open(log_path, "a", encoding="utf-8") if log_path else None
        if log_fh:
            attempt = _count_attempts(log_path)
            log_fh.write(json.dumps(
                {"kind": "_run", "attempt": attempt, "brief": brief[:300],
                 "continuation_in": continuation, "at": _now_iso()},
                ensure_ascii=False) + "\n")

        try:
            for ev in runner.stream(prompt, continuation=continuation, handle=handle):
                stats.events += 1
                if log_fh:                       # 过程落盘，不进 leader 的 context
                    log_fh.write(json.dumps(
                        {"kind": ev.kind, "name": ev.name, "command": ev.command,
                         "text": ev.text, "is_error": ev.is_error},
                        ensure_ascii=False, default=str) + "\n")
                if keep_events:
                    res.events.append(ev)

                if ev.kind == "text":
                    streamed.append(ev.text)
                elif ev.kind == "tool_use":
                    stats.tool_calls += 1
                    if ev.name in WRITE_TOOLS and ev.command:
                        event_paths.append(ev.command)
                elif ev.kind == "result":
                    saw_result = True
                    r = ev.raw or {}
                    res.text = ev.text
                    res.continuation = r.get("continuation") or res.continuation
                    res.cost_usd = r.get("cost_usd", res.cost_usd)
                    res.duration_ms = r.get("duration_ms", res.duration_ms)
                    res.usage = r.get("usage", res.usage)
                    res.structured = r.get("structured", res.structured)
                    if r.get("is_error"):
                        res.ok = False
                elif ev.kind == "error":
                    stats.errors += 1
                    res.ok = False
                    res.error = ev.text or res.error
                    if (ev.raw or {}).get("timed_out"):
                        res.timed_out = True
        finally:
            if log_fh:
                log_fh.close()

        if not saw_result and streamed:
            res.text = "".join(streamed)

        if self.output_schema is not None and res.structured is None and res.text:
            try:
                value = json.loads(res.text.strip())
                if isinstance(value, dict):
                    res.structured = value
            except (ValueError, TypeError):
                pass

        # 交付物 = 目录快照 diff ∪ 工具事件（互补：扫描覆盖全后端，事件能抓到写到 ws 之外的）
        res.artifacts = merge_artifacts(diff_snapshots(before, snapshot(ws)), event_paths)
        if ws:
            stats.outside_workspace = [a.path for a in res.artifacts
                                       if not a.path.startswith(ws + os.sep)]
        res.stats = stats
        res.summary = (res.text or "")[:self.summary_limit].strip()

        # status 是**启发式**，不是保证 —— agent 说「做完了」不代表真做完，leader 该抽查还得抽查
        if handle is not None and handle.stopped:
            res.status = "stopped"
        elif continuation and _looks_stale_session(res.error):
            # 会话句柄失效（机器重启 / 会话过期 / 换了机器）和「任务真的失败了」是两回事：
            # 前者重跑一次冷启动就好，后者重跑还是错。分开报，让 leader 能对症下药。
            # **不自动冷启重试** —— 返工轮的 brief 是「验收没过：xxx」，脱离上下文重跑会产出垃圾。
            res.status = "stale_session"
        elif not res.ok:
            res.status = "failed"
        elif ws and self.style != "dialogue" and not res.artifacts and not continuation:
            # 首轮说好了要交付物却一个文件都没落 → partial。
            # **返工轮不算**：agent 判断「上轮已经对了，不用改」是合理结果，
            # 这时 artifacts 为空是 done 不是 partial，误判会触发无谓的下一轮返工。
            res.status = "partial"
        else:
            res.status = "done"
        return res

    # ── 给子类用的小工具 ──
    def _resolved_cwd(self) -> str:
        return self.cwd if Path(self.cwd).is_dir() else str(Path.home())

    def _clip(self, text: str) -> Tuple[str, bool, int]:
        """按 tool_result_limit 截断工具输出。返回 (正文, 是否截断, 原始长度)。"""
        n = len(text or "")
        if self.tool_result_limit and n > self.tool_result_limit:
            return text[:self.tool_result_limit], True, n
        return text or "", False, n

    def _tool_result_event(self, text: str, *, is_error: bool = False,
                           raw: Optional[dict] = None) -> Event:
        """统一出口：所有后端的 tool_result 都从这里出，截断策略只写一次。"""
        clipped, cut, n = self._clip(text)
        return Event(kind="tool_result", text=clipped, is_error=is_error,
                     truncated=cut, full_len=n, raw=raw)

    def _start_log(self, shown: str, raw: dict) -> Event:
        """每次 stream 的第一条事件：命令 + 能力自述（含被忽略的参数）。"""
        return Event(kind="log", text=f"$ {shown}\n  ↳ {self.explain()}",
                     raw={**raw, "mode": self.mode, "ignored": list(self.ignored),
                          "readonly_enforcement": self.caps.readonly})


# 各后端 resume 失败时吐的话。启发式匹配，认不出就退回 status="failed"（仍然是对的，只是不够具体）。
_STALE_MARKERS = (
    "no conversation found", "session id", "no rollout found",   # claude / codex
    "thread/resume", "conversation not found", "session not found",
)


def _looks_stale_session(err: str) -> bool:
    e = (err or "").lower()
    return bool(e) and any(m in e for m in _STALE_MARKERS)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _count_attempts(log_path: str) -> int:
    """数这个 workspace 已经跑过几轮（日志里的 _run 分隔记录）。"""
    try:
        with open(log_path, encoding="utf-8") as f:
            return sum(1 for ln in f if '"kind": "_run"' in ln or '"kind":"_run"' in ln) + 1
    except OSError:
        return 1


class TaskSession:
    """一个活的返工闭环 —— `run()` 的多轮版，相当于任务模式的 Conversation。

    `run()` 本身收/返 continuation，手动串也能用；但每个 leader 都要重写一遍
    「记住句柄 → 再派一次 → 累积交付物」这段循环。这里包掉。

        t = ag.task(workspace="/runs/t01")
        r = t.run("实现登录接口")
        while not 验收通过(r) and t.attempts < 3:
            r = t.run("验收没过：<哪条没达标 + 期望是什么>")   # 自动续同一个 session
        t.all_artifacts      # 三轮累积下来的全部交付物（按路径去重，保留最新状态）
        t.stop()             # 别的线程叫停当前这轮
    """

    def __init__(self, agent: "Agent", *, workspace: Optional[str] = None,
                 context_dirs: Sequence[str] = ()):
        self.agent = agent
        self.workspace = workspace
        self.context_dirs = list(context_dirs)
        self.continuation: Optional[str] = None
        self.attempts = 0
        self.history: List[TaskResult] = []
        self._handle: Optional[RunHandle] = None
        # 一个 TaskSession = 一条任务线，天然是串行的。加锁把「同一个 session 被并发 run」
        # 从静默打架（两个进程抢同一个 continuation + 同一个 workspace）变成排队。
        # 并发派**不同**的活照旧免费：那是不同的 TaskSession，各有各的锁。
        self._lock = threading.Lock()

    def stop(self) -> None:
        """中止当前这一轮。可从别的线程调。"""
        if self._handle:
            self._handle.stop()

    def run(self, brief: str, **kw) -> TaskResult:
        with self._lock:
            self._handle = RunHandle()
            r = self.agent.run(brief, workspace=self.workspace,
                               context_dirs=self.context_dirs,
                               continuation=self.continuation, handle=self._handle, **kw)
            self.attempts += 1
            self.history.append(r)
            # 拿到新句柄就更新；没拿到（报错/超时/会话失效）保留旧的，下一轮还能接着试
            if r.continuation:
                self.continuation = r.continuation
            return r

    @property
    def all_artifacts(self) -> List[Artifact]:
        """各轮交付物合并，按路径去重、保留最后一轮的状态。"""
        merged = {}
        for r in self.history:
            for a in r.artifacts:
                merged[a.path] = a
        return sorted(merged.values(), key=lambda a: a.path)


class Conversation:
    """多轮对话：记住上一轮的 continuation，下一轮自动带上。chat box 就用它。

        c = agent.conversation()
        for ev in c.stream("帮我看这个 bug"): render(ev)
        for ev in c.stream("那第二个方案呢"): render(ev)   # 自动续接
        c.stop()                                          # UI 线程点「停止」
    """
    def __init__(self, agent: Agent):
        self.agent = agent
        self.continuation: Optional[str] = None
        self._handle: Optional[RunHandle] = None

    def stop(self) -> None:
        """中止当前这一轮。可从别的线程调（chat box 的停止按钮）。"""
        if self._handle:
            self._handle.stop()

    def ask(self, prompt: str) -> Result:
        self._handle = RunHandle()
        res = self.agent.ask(prompt, continuation=self.continuation, handle=self._handle)
        if res.continuation:
            self.continuation = res.continuation
        return res

    def stream(self, prompt: str) -> Iterator[Event]:
        self._handle = RunHandle()
        for ev in self.agent.stream(prompt, continuation=self.continuation,
                                    handle=self._handle):
            if ev.kind == "result":
                cont = (ev.raw or {}).get("continuation")
                if cont:
                    self.continuation = cont
            yield ev
