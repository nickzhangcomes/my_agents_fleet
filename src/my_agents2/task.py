"""编队派活用的零件 —— 交付物识别、workspace 快照、姿态与交付契约模板。

场景：主进程当 team leader，把活派给一个个子 agent。leader 要一直活着跟老大对话，
所以**它的 context 是稀缺资源** —— 子 agent 干的活可以很重，回给 leader 的必须很轻：
一句话摘要 + 交付物路径 + 日志路径，正文和过程全部落盘。

本模块不 import base，避免循环依赖；TaskResult 在 base.py 里（它继承 Result）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ── 姿态：构造期追加进 system_prompt，对 ask / stream / run 一律生效 ──
STYLES = ("delivery", "dialogue")

STYLE_POSTURE = {
    "delivery": (
        "【工作姿态】你是一支团队里的执行成员，对接的是一个主进程（不是人）。"
        "少说多做：把产出真正写成文件落盘，不要把大段内容贴在回复里。"
        "回复只用来交接——说清楚你做了什么、关键产物在哪。做不到就直说做不到，不要假装完成。"
    ),
    "dialogue": (
        "【工作姿态】你在跟人实时对话，过程本身就是价值。"
        "边做边说明你的思路和判断依据，让对方能跟上你怎么想的。"
        "结论要完整、可直接使用，不要只回一句「做完了」。"
    ),
}

# ── 交付契约：run() 里按 task 拼进 brief（workspace 是 per-task 的，构造期定不了）──
_DELIVERY_CONTRACT = """\

---
【交付要求】
- 所有产出写到 `{workspace}/` 下，用真实文件名，不要只在回复里贴内容。
- 完成后回复**不超过 {limit} 字**，只说：做了什么 + 关键文件是哪几个。
- 没做成就直接说没做成、卡在哪，不要编。"""

_CONTEXT_BLOCK = """\

---
【参考资料】以下目录可读，需要什么自己去读，不要假设内容：
{dirs}"""


def build_task_prompt(brief: str, workspace: Optional[str], context_dirs: Sequence[str],
                      style: Optional[str], summary_limit: int) -> str:
    """把 brief 拼成完整任务书。姿态已经在 system_prompt 里了，这里只加 per-task 的部分。"""
    parts = [brief]
    if context_dirs:
        parts.append(_CONTEXT_BLOCK.format(dirs="\n".join(f"- {d}" for d in context_dirs)))
    if workspace and style != "dialogue":
        parts.append(_DELIVERY_CONTRACT.format(workspace=workspace, limit=summary_limit))
    return "".join(parts)


# ── 交付物 ──
@dataclass
class Artifact:
    """一件交付物。leader 拿到的是**路径**，不是内容。"""
    path: str
    action: str = "written"        # written | modified | deleted
    size: int = 0
    source: str = "scan"           # scan（目录快照 diff）| event（工具事件）| both

    def __repr__(self) -> str:
        return f"<Artifact {self.action} {self.path} {self.size}B via {self.source}>"


# 各后端「写文件」工具名 → 从 Event.command 直接拿路径
WRITE_TOOLS = {
    # claude
    "Write", "Edit", "MultiEdit", "NotebookEdit",
    # agy
    "write_to_file", "replace_file_content", "multi_replace_file_content",
    "sed_file", "notebook_edit",
    # grok（工具名和内部 rawOutput.type 对不上：toolName=write 而 type=SearchReplace，
    #       以工具名为准，这里登记的是 toolName）
    "write", "write_file", "edit_file", "search_replace", "apply_patch",
    # codex 的 FileChange 例外：command 是 changes 的 JSON 摘要，路径提不可靠 —— 靠扫描兜底
}

_SKIP_NAMES = {".agent-log.jsonl", ".DS_Store"}
# 构建噪音，不是交付物
_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".pytest_cache"}


def real(path: Optional[str]) -> str:
    """统一用 realpath 解软链。

    ⚠ 必须是 realpath 不是 abspath：macOS 上 /tmp → /private/tmp 是软链，
    CLI 报回来的工具事件路径已经解过链，而我们扫描用的是原始路径 ——
    两边不一致会导致**同一个文件被算成两件交付物**，还会误判成「写到 workspace 之外」。
    """
    return os.path.realpath(path) if path else ""


def snapshot(root: Optional[str]) -> Dict[str, Tuple[float, int]]:
    """给 workspace 拍个快照：path → (mtime, size)。用来分辨「agent 写的」和「本来就有的」。"""
    out: Dict[str, Tuple[float, int]] = {}
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(real(root)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
        for fn in filenames:
            if fn in _SKIP_NAMES:
                continue
            p = real(os.path.join(dirpath, fn))
            try:
                st = os.stat(p)
                out[p] = (st.st_mtime, st.st_size)
            except OSError:
                continue
    return out


def diff_snapshots(before: Dict[str, Tuple[float, int]],
                   after: Dict[str, Tuple[float, int]]) -> List[Artifact]:
    """跑前跑后对比，得到 agent 真正动过的文件。"""
    arts: List[Artifact] = []
    for p, (mt, sz) in after.items():
        if p not in before:
            arts.append(Artifact(path=p, action="written", size=sz, source="scan"))
        elif before[p] != (mt, sz):
            arts.append(Artifact(path=p, action="modified", size=sz, source="scan"))
    for p in before:
        if p not in after:
            arts.append(Artifact(path=p, action="deleted", size=0, source="scan"))
    return arts


def merge_artifacts(scanned: List[Artifact], event_paths: Sequence[str]) -> List[Artifact]:
    """扫描结果 ∪ 事件抓取。

    两条来源互补：
      - 扫描：全后端通用（连 grok 都覆盖），但只看得见 workspace 里面
      - 事件：精确到「哪个工具动的」，还能发现**写到 workspace 之外**的文件
    交集标 source="both"，只在事件里出现的（= 写到 workspace 外）单独列出来，
    因为那往往是 agent 没守约定，leader 该知道。
    """
    by_path = {a.path: a for a in scanned}
    for raw_path in event_paths:
        p = real(raw_path)
        if not p:
            continue
        if p in by_path:
            by_path[p].source = "both"
        else:
            size = 0
            try:
                size = os.path.getsize(p)
            except OSError:
                pass
            by_path[p] = Artifact(path=p, action="written", size=size, source="event")
    return sorted(by_path.values(), key=lambda a: a.path)


@dataclass
class RunStats:
    """跑一趟的过程统计 —— 全在 leader 的 context 之外，只留数字。"""
    events: int = 0
    tool_calls: int = 0
    errors: int = 0
    outside_workspace: List[str] = field(default_factory=list)
