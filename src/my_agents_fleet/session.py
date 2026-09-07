"""持久多轮会话 —— 「上次聊到哪了」这半边。

agents2 给的是**能续**，这里补的是**续得住**：

    agents2:   conversation()          进程内多轮，句柄在内存，进程一死就没
               ask(continuation=句柄)   能跨进程续，但句柄得调用方自己存

    这一层补： 句柄落盘
               续接守门 —— 执行环境变了就不续（不然会串到别的目录的记忆里）
               续不上的兜底 —— 把落盘历史灌回去重建上下文
               每轮上下文注入 —— context_providers

核心是三个角色分工：
    后端 session  当**缓存**   —— 省 token、续上下文，但它可能过期、可能换机器就没
    落盘历史      当**真相源** —— 缓存没了照样能重建
    守门 + 兜底                —— 环境变了不续；续不上就清掉句柄带历史起新
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from . import failures as F
from .store import SessionState, SessionStore, Turn


@dataclass
class TurnContext:
    """传给每个 context provider 的本轮上下文。"""
    session_id: str
    agent_name: str
    prompt: str
    history: List[Turn]           # 此前所有轮（落盘历史）
    resuming: bool                # 本轮是否在续接后端会话
    cwd: str
    fingerprint: Optional[str] = None


def history_recap(ctx: TurnContext, max_turns: int = 20, max_chars: int = 1200) -> str:
    """默认 provider：冷启时把落盘历史灌回去；续接时让位给后端会话记忆（返回空）。

    为什么续接时不灌：后端 session 里已经有完整记忆了，再灌一遍是**双份上下文**——
    既费 token，又可能让模型把「历史摘要」当成新的一轮对话内容。
    """
    if ctx.resuming or not ctx.history:
        return ""
    turns = ctx.history[-max_turns:]
    dropped = len(ctx.history) - len(turns)
    lines = ["（这是同一会话的延续。后端会话记忆此刻不可用，以下为落盘历史，请据此重建上下文：）"]
    if dropped > 0:
        lines.append(f"…（更早的 {dropped} 轮已省略）")
    for i, t in enumerate(turns, 1):
        q = " ".join((t.prompt or "").split())[:max_chars]
        a = " ".join((t.text or "").split())[:max_chars]
        lines.append(f"[{i}] 你被问：{q}")
        lines.append(f"[{i}] 你答：{a}")
    return "\n".join(lines)


class Session:
    """一个 (agent, session_id) 的持久会话。每轮都会落一条 Run（会话不豁免留痕）。"""

    def __init__(self, fleet, agent_name: str, session_id: str, store: SessionStore, *,
                 context_providers: Optional[List[Callable[[TurnContext], str]]] = None,
                 overrides: Optional[dict] = None, logger=None):
        self.fleet = fleet
        self.agent_name = agent_name
        self.session_id = session_id
        self.store = store.for_agent(agent_name)
        self.context_providers = (context_providers
                                  if context_providers is not None else [history_recap])
        self.overrides = overrides or {}
        self._log = logger or (lambda m: None)

    def _state(self) -> SessionState:
        return self.store.load(self.session_id) or SessionState(
            session_id=self.session_id, agent_name=self.agent_name)

    def _gate(self, state: SessionState, fingerprint: Optional[str]) -> bool:
        """续接守门：角色、后端、目录、权限、模型或身份提示变了就不续。

        为什么必须守：后端会话句柄绑着当时的工作目录和权限。环境换了还硬续，
        agent 会带着**上一个环境的记忆**在新环境里干活 —— 它以为自己还在旧目录。
        丢掉句柄起新的代价只是多花点 token（历史会由 history_recap 灌回去），
        串台的代价是产出全错。
        """
        if not state.continuation or state.agent_name != self.agent_name:
            return False
        if not state.fingerprint or state.fingerprint != fingerprint:
            self._log(f"[session {self.session_id}] 执行环境变了，丢弃旧会话句柄，起新")
            return False
        return True

    def _effective_prompt(self, ctx: TurnContext) -> str:
        blocks: List[str] = []
        for p in self.context_providers:
            try:
                b = p(ctx)
            except Exception as e:                # noqa: BLE001 provider 出错不该炸掉整轮
                self._log(f"[session] context provider {getattr(p, '__name__', p)} 出错: {e}")
                b = ""
            if b and b.strip():
                blocks.append(b.strip())
        block = "\n\n".join(blocks)
        return (block + "\n\n---\n\n" + ctx.prompt) if block else ctx.prompt

    def _persist(self, state, prompt, text, cont, ok, cwd, fingerprint):
        state.agent_name = self.agent_name
        state.cwd = cwd
        state.fingerprint = fingerprint
        if cont:            # 拿到新句柄才更新；没拿到（比如报错）保留旧的，下轮还能再试
            state.continuation = cont
        state.turns.append(Turn(prompt=prompt, text=text or "", ok=ok, continuation=cont))
        self.store.save(state)

    def _turn(self, state, resuming, cwd, fp, prompt):
        """组本轮的 TurnContext + 最终 prompt。

        `prompt` 可以是**字符串**，也可以是 `callable(TurnContext) -> str`。
        给可调用的意义：**这轮是不是续接，只有 Session 自己知道**，而这件事往往决定
        prompt 该怎么写 —— 冷启要把背景（帖子正文 / 工单原文 / 上文）带上，
        续接则不必重复，后端会话里已经有了。调用方拿不到这个信号就只能每轮都按最坏情况写，
        于是每轮重发一遍背景，既费 token 又把模型框成「每次都是新对话」。
        """
        ctx = TurnContext(self.session_id, self.agent_name, "",
                          state.turns, resuming, cwd, fp)
        ctx.prompt = prompt(ctx) if callable(prompt) else prompt
        return ctx

    def ask(self, prompt, *, ref: str = None, trigger: str = None):
        """在这个会话里跑一轮：续接 → 跑 → 续不上就带历史重来 → 落盘。返回 Outcome。

        `prompt`：字符串，或 `callable(TurnContext) -> str`（见 `_turn`）。
        """
        with self.store.transaction(self.session_id):
            return self._ask_locked(prompt, ref=ref, trigger=trigger)

    def _ask_locked(self, prompt, *, ref=None, trigger=None):
        state = self._state()
        ag = self.fleet.get(self.agent_name, session_id=self.session_id, **self.overrides)
        cwd = getattr(ag, "cwd", "") or ""
        fp = getattr(ag, "fingerprint", None)
        resuming = self._gate(state, fp)
        if not resuming:
            state.continuation = None

        ctx = self._turn(state, resuming, cwd, fp, prompt)
        out = ag.ask(self._effective_prompt(ctx),
                     continuation=state.continuation if resuming else None,
                     ref=ref, trigger=trigger)

        # 续接没生效 → 清掉死句柄、带历史冷启一次。
        # 先清 state.continuation 再重跑：万一这次也没拿到新句柄，下轮不会再撞同一个死会话。
        if resuming and out.failure and out.failure.code == "stale_session":
            self._log(f"[session {self.session_id}] 续接失效，清句柄、带历史起新")
            state.continuation = None
            # ★ 用 resuming=False 重新组一次 prompt —— 传的是函数的话，这里会把背景补回来。
            #   原来直接拿续接版的 prompt 重跑，等于冷启动却缺了背景。
            ctx = self._turn(state, False, cwd, fp, prompt)
            previous_attempts = list(out.attempts)
            out = ag.ask(self._effective_prompt(ctx), continuation=None,
                         ref=ref, trigger=trigger)
            out.attempts = previous_attempts + out.attempts

        # 落盘存**解析后**的 prompt（不是那个函数）——冷启兜底重放历史时要能读
        self._persist(state, ctx.prompt, out.text, out.continuation, out.ok, cwd, fp)
        return out

    def history(self) -> List[Turn]:
        with self.store.transaction(self.session_id):
            return self._state().turns

    def reset(self) -> None:
        """清空这个会话：句柄和历史都丢。下次是彻底的新会话。"""
        with self.store.transaction(self.session_id):
            self.store.save(SessionState(session_id=self.session_id,
                                         agent_name=self.agent_name))
