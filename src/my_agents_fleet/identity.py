"""身份组装 —— 「你是谁」这半边。

一个 CLI 子进程不等于一个 agent。子进程只是载具；**名字、职责、边界、风格**才是身份，
由这里从「全局段 + 单 agent 段」拼成一段系统提示注入进去。

组装顺序固定：业务场景 → 共同约束 → 手上有什么 → 你是谁 → 你的工作内容。
前三段来自全局（全队共享），后两段来自这个 agent 自己。

⚠️ 身份是**声明式**的：人写、可 review、跨调用不变。三样东西**不该**进这里：
     · 本次要做什么   → 那是工单的 brief，写进来身份就跟着每次任务漂
     · 运行时状态     → 会话历史和 Run 记录才是它的位置
     · 大段资料       → 挂目录让 agent 自己读（prompt 有 ~1MB 的 argv 墙）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

# 人设是怎么进到模型里的 —— 记进 Run 表，因为这直接决定人设强度
IDENTITY_SYSTEM = "system"          # 走后端的系统提示位（claude/grok）
IDENTITY_PREFIX = "prompt-prefix"   # 后端没有系统提示位，拼成 user 轮前缀（codex/agy）
IDENTITY_NONE = "none"              # 后端不支持系统提示——人设没生效，必须留痕


def compose_system_prompt(
    *,
    business_scenario: str = "",
    global_md: str = "",
    constraints: Optional[Sequence[str]] = None,
    caps_note: str = "",
    intro: str = "",
    work_md: str = "",
    skills: Optional[Sequence[str]] = None,
) -> str:
    """全局背景 + 约束 + 手上的资源 + 本 agent 简介 + 工作内容 → 一段系统提示。
       只输出有内容的小节；全空返回 ""。"""
    parts: List[str] = []

    bg = "\n\n".join(x for x in (business_scenario.strip(), global_md.strip()) if x)
    if bg:
        parts.append("# 业务场景\n" + bg)

    cons = [str(c).strip() for c in (constraints or []) if c and str(c).strip()]
    if cons:
        parts.append("# 共同约束\n" + "\n".join(f"- {c}" for c in cons))

    sk = [str(s).strip() for s in (skills or []) if str(s).strip()]
    note = caps_note.strip()
    if sk:
        note = (note + "\n\n" if note else "") + skill_note(sk)
    if note:
        parts.append("# 你手上有什么\n" + note)

    if intro.strip():
        parts.append("# 你是谁\n" + intro.strip())

    if work_md.strip():
        parts.append("# 你的工作内容\n" + work_md.strip())

    return "\n\n".join(parts)


def skill_note(skills: Sequence[str]) -> str:
    """把「这个角色能用哪些 skill」渲染成一句声明。

    ⚠️ **诚实说明这条做到了什么、没做到什么。**
       没做到：本机 claude CLI 没有 per-skill 的挂载 flag（只有 `--disable-slash-commands`
               一刀切关掉全部），所以这里**仍然是提示词层面的声明**，不是机制挂载。
       做到了：声明从各 agent 的 .md 正文里挪到了配置。于是 ——
               ① 全队可复用：一个 skill 写一次，多个 agent 共挂（Multica 说的「能力会累积」）
               ② 改一处生效：不用去 N 个 .md 里各改一遍
               ③ 可查：这个角色到底被授予了哪些能力，看配置就知道，不用读长文找
       将来 CLI 给了 per-skill flag，只要改这一个函数就能升级成机制。
    """
    return ("你可以使用这些 skill（用 /名字 调用）：\n"
            + "\n".join(f"- {s}" for s in skills))


def caps_note(cwd: Optional[str], extra_dirs: Sequence[str], has_mcp: bool,
              extra_notes: Optional[Sequence[str]] = None) -> str:
    """如实告诉 agent 这轮手上有什么。

    ⚠️ 措辞刻意不写「只读」——挂进去的目录能不能写由后端权限决定，
       不是 add-dir 决定的。**在提示词里管一个提示词管不住的边界，是自欺。**
       真要只读，去 mode/权限那一层做。
    """
    lines: List[str] = []
    if cwd:
        lines.append(f"本轮主工作目录：{cwd}")
    if extra_dirs:
        lines.append("本轮额外开放的目录：")
        lines += [f"- {d}" for d in extra_dirs]
    if has_mcp:
        lines.append("已接入共享 MCP 工具（运行时里可直接调用）。")
    for n in (extra_notes or []):          # 契约的如实说明（trust / assets 落地到什么程度）
        if n and n.strip():
            lines.append(n.strip())
    return "\n".join(lines)


def merge_add_dirs(shared: Optional[Sequence[str]],
                   agent: Optional[Sequence[str]]) -> List[str]:
    """共享目录 + 本 agent 目录，保序去重。"""
    out: List[str] = []
    for d in list(shared or []) + list(agent or []):
        if d and d not in out:
            out.append(d)
    return out


def resolve_workspace(workspace: Optional[str], base: str) -> str:
    """工作区 → cwd：绝对路径原样；相对路径相对 base 解析；空 → base。"""
    if not workspace:
        return base
    p = Path(os.path.expanduser(workspace))
    return str(p) if p.is_absolute() else str((Path(base) / p).resolve())


def merge_mcp_config(shared_path: Optional[str], agent_path: Optional[str],
                     cache_dir: Optional[str] = None) -> Optional[str]:
    """后端的 mcp_config 收的是**文件路径**。两个都给就合并后落一个缓存文件。

    文件名 = 合并结果的哈希（内容寻址）：同一份配置只落一个文件、可复用。
    不这么做的话每次 get() 都 mkstemp 一个新临时文件，多轮会话能把 /tmp 堆爆。
    """
    paths = [p for p in (shared_path, agent_path) if p]
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    merged = {"mcpServers": {}}
    for p in paths:
        try:
            data = json.loads(Path(os.path.expanduser(p)).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"读 MCP 配置失败 {p}: {e}") from e
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if servers is None and isinstance(data, dict):
            servers = data
        if isinstance(servers, dict):
            merged["mcpServers"].update(servers)
    blob = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True)
    h = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    base = Path(os.path.expanduser(cache_dir)) if cache_dir else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"mcp_{h}.json"
    if not out.exists():
        out.write_text(blob, encoding="utf-8")
    return str(out)


def fingerprint(*, agent: str, backend: str, cwd: Optional[str],
                extra_dirs: Sequence[str], settings: Optional[dict] = None) -> str:
    """执行环境指纹：角色、后端、目录，以及调用方给出的有效配置。

    两个用途：① Run 表里留证据，事后能答「那次是在什么环境跑的」；
             ② 会话续接的守门 —— 环境变了就不能接着上次的会话（见 session.py）。
    """
    blob = json.dumps({"agent": agent, "backend": backend, "cwd": cwd or "",
                       "dirs": sorted(str(d) for d in extra_dirs), "settings": settings or {}},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
