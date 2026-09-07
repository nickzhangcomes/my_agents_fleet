"""my_agents2 —— 本地 CLI agent 的统一调用库（1.0 的重写版）。

    from my_agents2 import get_agent

    ag = get_agent("codex", mode="readonly", cwd="/某项目", effort="high")
    print(ag.explain())          # 这个后端到底给了你什么保证
    print(ag.ask("讲清楚这个目录").text)

统一入参（五个 CLI 后端收同一套，换后端零改代码）：
    mode          "full" | "readonly"   —— 权限只有两档
    cwd           工作目录
    model         模型名
    effort        low | medium | high | xhigh | max | ultra
    timeout       秒
    system_prompt 追加的系统提示 / 人设
    add_dirs      额外可访问目录
    output_schema JSON Schema（dict / JSON 串 / 文件路径都收）—— 强制结构化产出，
                  拿 Result.structured（已解析的 dict）。支持的后端见 caps.structured_output；
                  不支持的后端记进 ag.ignored，不会拼进命令行。

后端专属参数走 extra={...}，例：
    get_agent("claude", extra={"mcp_config": "/path/mcp.json"})
    get_agent("grok",   extra={"max_turns": 5})

后端落不了地的统一参数 → 静默走默认（不报错），但记在 ag.ignored 里，
且写进每次 stream 的第一条 log 事件。能力矩阵见 caps_table()。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

from .antigravity import AntigravityAgent
from .base import (ENFORCEMENT, EFFORT_LEVELS, MODE_FULL, MODE_READONLY, MODES,
                   UNIFIED_PARAMS, Agent, Caps, Conversation, Event, Result,
                   RunHandle, TaskResult, TaskSession)
from .models import (EFFORT_ORDER, ModelInfo, effort_options, list_models,
                     models_status, refresh_models_registry)
from .task import STYLES, Artifact, RunStats
from .claude_code import ClaudeCodeAgent
from .codex_cli import CodexAgent
from .grok_code import GrokAgent
from .pi_cli import PiAgent

__all__ = [
    "Agent", "Caps", "Event", "Result", "Conversation", "RunHandle",
    "TaskResult", "TaskSession", "Artifact", "RunStats",
    "ClaudeCodeAgent", "CodexAgent", "GrokAgent", "AntigravityAgent", "PiAgent",
    "get_agent", "list_agents", "caps_table",
    "ModelInfo", "list_models", "effort_options", "models_status", "refresh_models_registry",
    "MODE_FULL", "MODE_READONLY", "MODES", "EFFORT_LEVELS", "EFFORT_ORDER",
    "ENFORCEMENT", "UNIFIED_PARAMS", "STYLES",
]

_CONFIG_PATH = Path(__file__).parent / "config.json"

# impl 名 → 本地 CLI 实现类。新 CLI 后端实现后在这里登记一行即可。
_IMPLS = {
    "claude_code": ClaudeCodeAgent,
    "codex_cli": CodexAgent,
    "grok_code": GrokAgent,
    "antigravity": AntigravityAgent,
    "pi_cli": PiAgent,
}


def _load_config() -> dict:
    path = Path(os.environ.get("MY_AGENTS2_CONFIG", str(_CONFIG_PATH))).expanduser()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"default": "claude", "agents": {}}


def list_agents() -> List[str]:
    """列出 config.json 里登记的 agent 名。"""
    return list(_load_config().get("agents", {}).keys())


def caps_table() -> Dict[str, Caps]:
    """每个预设的能力自述 —— 挑后端前先看这张表。

        for nm, c in caps_table().items():
            print(nm, c.readonly, c.tool_events)
    """
    cfg = _load_config()
    out: Dict[str, Caps] = {}
    for nm, preset in cfg.get("agents", {}).items():
        impl = _IMPLS.get(preset.get("impl", ""))
        if impl is not None:
            out[nm] = impl.caps
    return out


def get_agent(name: str = None, **overrides) -> Agent:
    """按名字取一个 agent（config.json 预设 + 运行时覆盖）。

    name 省略 → 用 config 的 default。
    overrides → 统一入参直接传；后端专属参数放 extra={...}。

        get_agent()                                       # 默认 agent
        get_agent("claude", cwd="/某项目")                 # 覆盖工作目录
        get_agent("codex", mode="readonly", effort="high")  # 只读 + 高推理档
        get_agent("grok", extra={"max_turns": 5})           # 后端专属参数
    """
    cfg = _load_config()
    name = name or cfg.get("default", "claude")
    agents = cfg.get("agents", {})
    if name not in agents:
        raise ValueError(f"未找到 agent: {name}，可选: {list(agents.keys())}")

    # `_` 开头的键是写给人看的注释（JSON 没有注释语法），不传进构造器
    preset = {k: v for k, v in agents[name].items() if not k.startswith("_")}
    impl_key = preset.pop("impl", None) or "claude_code"
    preset.pop("kind", None)        # kind 只是给人看的标签，不传进构造器
    impl = _IMPLS.get(impl_key)
    if impl is None:
        raise ValueError(f"agent '{name}' 的 impl='{impl_key}' 还没实现（见 {impl_key}.py）")

    params = {**preset, **overrides}
    # extra 是「后端专属参数」的命名空间：预设里的和调用时传的合并，展开后直达构造器
    extra = {**(preset.get("extra") or {}), **(overrides.get("extra") or {})}
    params.pop("extra", None)
    params.update(extra)
    params.setdefault("name", name)
    return impl(**params)
