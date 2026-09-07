"""测试用的假后端 —— 长得像 my_agents2 的 Agent，但不起进程、不烧额度。

只实现 RecordedAgent 真正会碰的那几个属性/方法。**故意不用 mock 库**：
手写的假件把「这一层到底依赖后端的什么」摆在明面上，
哪天 agents2 改了契约，这个文件会第一个报错 —— 那正是我们想要的信号。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parents[2] / "src"


class FakeAgent:
    """按脚本依次返回 Result。`calls` 记被真正调了几次 —— 重试策略全靠它验。"""

    def __init__(self, script, *, output_schema=None, model="fake-1",
                 effort=None, ignored=(), cwd="/tmp", delay=0.0):
        self.script = list(script)
        self.calls = 0
        self.output_schema = output_schema
        self.model = model
        self.effort = effort
        self.ignored = tuple(ignored)
        self.cwd = cwd
        self.delay = delay
        self.prompts = []
        self.continuations = []
        self._lk = threading.Lock()

    def ask(self, prompt, *, continuation=None):
        with self._lk:
            i = min(self.calls, len(self.script) - 1)
            self.calls += 1
        self.prompts.append(prompt)
        self.continuations.append(continuation)
        if self.delay:
            time.sleep(self.delay)
        item = self.script[i]
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, prompt, *, continuation=None):
        res = self.ask(prompt, continuation=continuation)
        from my_agents2 import Event
        if res.text:
            yield Event(kind="text", text=res.text)
        yield Event(kind="result", text=res.text,
                    raw={"continuation": res.continuation, "cost_usd": res.cost_usd,
                         "usage": res.usage, "structured": res.structured,
                         "is_error": not res.ok})


class FakeCaps:
    """只带 RecordedAgent / contract 会读的那几个能力位。"""

    def __init__(self, *, deny=(), readonly="tool-allowlist", cwd=True, add_dirs=True,
                 system_prompt=True, system_prompt_native=True, structured_output=True):
        self.deny = tuple(deny)
        self.readonly = readonly
        self.cwd = cwd
        self.add_dirs = add_dirs
        self.system_prompt = system_prompt
        self.system_prompt_native = system_prompt_native
        self.structured_output = structured_output
