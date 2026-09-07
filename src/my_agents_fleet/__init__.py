"""my_agents_fleet —— 一支常驻队伍的运转（架在 my_agents2 之上）。

    from my_agents_fleet import load_fleet

    fleet = load_fleet("/你的业务项目/某处/myfleet")
    out   = fleet.get("哲学老师").ask("讲讲黑格尔", ref="episode:37", trigger="cron")
    out.structured                              # 配了 output_schema 就是 dict
    fleet.stats(since="2026-08-01")             # 这个月各 agent 跑成什么样

## 和 my_agents2 的分界：一次 vs 持续

    my_agents2   一次进程调用    派个活给 CLI 子进程，跑完、说实话
    my_agents_fleet    持续存在        谁是谁、记得什么、一直以来跑成什么样

判断一个能力该放哪层，问一句：**这件事跨不跨「一次调用」？**

    超时了要不要报错     不跨 → my_agents2
    这后端的只读有多硬   不跨 → my_agents2
    上次聊到哪了         跨   → my_agents_fleet
    这个角色是谁         跨   → my_agents_fleet
    这个月烧了多少钱     跨   → my_agents_fleet

## 这一层拥有的四件事

    身份    人写的、声明式的「你是谁」        → identity.py
    会话    跨进程的「上次聊到哪了」          → session.py / store.py
    痕迹    每次执行的 Run 记录（不可关闭）   → runs.py / dispatch.py
    治理    失败分类 + 按类重试               → failures.py

## 有意不做

    工作对象（Issue）—— 形状随业务变，中台定义不了。用 Run 的 `ref` 挂载点代替：
                        业务塞 "episode:philosophy:37"，中台能答「这个 ref 跑过几次、
                        失败几次、花了多少」，而不需要知道 episode 是什么。
    调度 / cron      —— 什么时候派活是业务的事。这一层只保证「塞进来的活能跑完、挂了能查」。
    编排 / workflow  —— my_agents2 的 task() 已覆盖返工闭环。
    多人 / 权限      —— 单人本地库。
"""
from __future__ import annotations

from .capacity import Capacity, WorkspaceBusy
from .contract import (ENFORCE_HARD, ENFORCE_NONE, ENFORCE_SOFT, TRUST_TRUSTED,
                       TRUST_UNTRUSTED, Asset, ContractError, ResolvedContract)
from .dispatch import Outcome, RecordedAgent
from .failures import AGENT, KINDS, PLATFORM, Failure, FailKind, classify
from .fleet import Fleet, FleetConfigError, load_fleet
from .identity import (IDENTITY_NONE, IDENTITY_PREFIX, IDENTITY_SYSTEM,
                       compose_system_prompt, fingerprint)
from .runs import Run, RunStore
from .session import Session, TurnContext, history_recap
from .store import JsonFileStore, SessionState, SessionStore, Turn

__all__ = [
    "load_fleet", "Fleet", "FleetConfigError",
    "RecordedAgent", "Outcome",
    "Run", "RunStore",
    "Session", "TurnContext", "history_recap",
    "SessionStore", "JsonFileStore", "SessionState", "Turn",
    "classify", "Failure", "FailKind", "KINDS", "PLATFORM", "AGENT",
    "compose_system_prompt", "fingerprint",
    "IDENTITY_SYSTEM", "IDENTITY_PREFIX", "IDENTITY_NONE",
    "Asset", "ResolvedContract", "ContractError",
    "TRUST_TRUSTED", "TRUST_UNTRUSTED",
    "ENFORCE_HARD", "ENFORCE_SOFT", "ENFORCE_NONE",
    "Capacity", "WorkspaceBusy",
]

__version__ = "2.0.1a1"
