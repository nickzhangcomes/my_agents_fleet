"""会话持久化 —— 「上次聊到哪了」的落盘。

和 Run 记录分开的原因：两者回答的问题不同。
    会话历史 = 内容（说过什么）      → 要喂回给模型，续不上时靠它重建上下文
    Run 记录 = 过程（跑成什么样）    → 给人看的运营数据，不进模型

所以会话这边**必须**存正文（不存就没法重建），Run 那边**坚决不存**（存了就是泄漏面）。
两边的留存策略也该不同：会话可以按 session 清理，Run 要长期留着看趋势。
"""
from __future__ import annotations

import json
import hashlib
import threading
from contextlib import contextmanager
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional

from ._files import atomic_json, file_lock

_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def safe_filename(s: str, *, max_len: int = 120, fallback: str = "item") -> str:
    """session_id 直接当文件名会被路径穿越（`../..`）和非法字符坑到，先洗一遍。"""
    out = _UNSAFE.sub("-", str(s or "")).strip("-.") or fallback
    return out[:max_len]


@dataclass
class Turn:
    """一轮对话。`continuation` 是这轮结束时后端给的会话句柄。"""
    prompt: str = ""
    text: str = ""
    ok: bool = True
    continuation: Optional[str] = None


@dataclass
class SessionState:
    """一个会话的全部落盘状态。"""
    session_id: str
    agent_name: str = ""
    continuation: Optional[str] = None     # 后端会话句柄（缓存，可能失效）
    cwd: str = ""
    fingerprint: Optional[str] = None      # 执行环境指纹，变了就不能续（见 session.py）
    turns: List[Turn] = field(default_factory=list)   # ← 真相源

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "agent_name": self.agent_name,
                "continuation": self.continuation, "cwd": self.cwd,
                "fingerprint": self.fingerprint,
                "turns": [t.__dict__ for t in self.turns]}

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        return cls(session_id=d.get("session_id", ""), agent_name=d.get("agent_name", ""),
                   continuation=d.get("continuation"), cwd=d.get("cwd", ""),
                   fingerprint=d.get("fingerprint"),
                   turns=[Turn(**t) for t in d.get("turns", [])])


class SessionStore(ABC):
    """自定义存储实现 load/save；跨进程使用时还须覆盖 transaction。"""

    @abstractmethod
    def load(self, session_id: str) -> Optional[SessionState]: ...

    @abstractmethod
    def save(self, state: SessionState) -> None: ...

    def for_agent(self, agent_name: str):
        return _ScopedStore(self, agent_name)

    @contextmanager
    def transaction(self, session_id: str):
        # 自定义存储的默认保障只覆盖当前进程；文件存储在下方实现跨进程锁。
        with _STORE_GUARD:
            lock = _STORE_LOCKS.setdefault((id(self), session_id), threading.RLock())
        with lock:
            yield


_STORE_GUARD = threading.Lock()
_STORE_LOCKS = {}


class _ScopedStore(SessionStore):
    """为旧的自定义 SessionStore 增加角色命名空间。"""
    def __init__(self, store, agent):
        self.store, self.agent = store, agent

    def _key(self, sid):
        return json.dumps([self.agent, sid], ensure_ascii=False, separators=(',', ':'))

    def load(self, sid):
        state = self.store.load(self._key(sid))
        if (state is not None and state.agent_name == self.agent
                and state.session_id == self._key(sid)):
            return replace(state, session_id=sid)
        legacy = self.store.load(sid)
        if legacy and legacy.session_id == sid and legacy.agent_name == self.agent:
            return legacy
        return None

    def save(self, state):
        if state.agent_name != self.agent:
            raise ValueError('会话角色与存储命名空间不匹配')
        self.store.save(replace(state, session_id=self._key(state.session_id)))

    def transaction(self, sid):
        return self.store.transaction(self._key(sid))


class JsonFileStore(SessionStore):
    """角色 + 完整会话 ID 的哈希作为文件键；旧文件只在身份匹配时读取。"""

    def __init__(self, root, *, agent_name=None):
        self.root = Path(os.path.expanduser(str(root)))
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent_name = agent_name

    def for_agent(self, agent_name):
        return JsonFileStore(self.root, agent_name=agent_name)

    def _path(self, session_id: str) -> Path:
        key = json.dumps([self.agent_name, session_id], ensure_ascii=False)
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.root / f"{safe_filename(session_id, max_len=40)}--{digest}.json"

    def transaction(self, session_id):
        return file_lock(self.root / '.locks' / (self._path(session_id).stem + '.lock'))

    def load(self, session_id: str) -> Optional[SessionState]:
        with self.transaction(session_id):
            p = self._path(session_id)
            candidates = [p]
            if not p.exists():
                # 保留旧文件，不批量重写。下一次 save 才写新格式。
                candidates.append(self.root / f"{safe_filename(session_id)}.json")
                if self.agent_name is not None:
                    candidates.append(JsonFileStore(self.root)._path(session_id))
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                try:
                    state = SessionState.from_dict(json.loads(candidate.read_text(encoding='utf-8')))
                except (OSError, ValueError, TypeError, AttributeError) as e:
                    raise ValueError(f'会话文件损坏，已保留原文件: {candidate}') from e
                if state.session_id != session_id:
                    continue
                if self.agent_name is not None and state.agent_name != self.agent_name:
                    continue
                return state
            return None

    def save(self, state: SessionState) -> None:
        if self.agent_name is not None and state.agent_name != self.agent_name:
            raise ValueError('会话角色与存储命名空间不匹配')
        with self.transaction(state.session_id):
            atomic_json(self._path(state.session_id), state.to_dict())
