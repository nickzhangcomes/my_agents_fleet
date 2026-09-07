"""模型 / effort 词典 —— 「哪个 CLI 有哪些模型，每个模型能开哪些 effort 档」。

## 为什么要有这一层

effort **是 per-model 属性，不是 per-backend**。实测：

    codex  gpt-5.6-sol   low medium high xhigh max ultra   ← 六档
    codex  gpt-5.5       low medium high xhigh             ← 没有 max
    grok   grok-4.5      low medium high                   ← 连 xhigh 都没有
    claude opus-4-6      low medium high max               ← 有 max 没 xhigh
    claude haiku-4-5     （完全不支持 effort）
    agy    gemini-3.1-pro  high low                        ← 没有 medium

任何写死的全局映射表都必然和这个对不齐 —— 要么偷偷降档（调用方以为开了 max，实际拿到 high），
要么发出非法值。所以这里不写规则，只**记事实**。

## 使用时自动检查

模型表默认有效 6 小时；本地 CLI / 上游缓存 / Pi 配置变化时提前采集。
失败保留旧表，5 分钟后重试；常驻进程按文件变化重读。只采集模型元数据，不发送模型提示。
可用 MY_AGENTS2_AUTO_REFRESH=0 关闭自动采集，手动刷新仍可用：

    python3 -m my_agents2 --refresh-models

## 五个数据源的可靠性不一样（ModelInfo.source 标着）

    cache   codex / grok —— 读 CLI 自己维护的 models_cache.json，结构化，最稳
    command agy / pi     —— 跑 `agy models` / `pi --list-models`，官方命令输出
    binary  claude       —— 模型表编译进二进制，得 strings + 正则挖压缩 JS。
                            **脆**：版本升级改结构可能断。挖不到保留旧表并标记过期。
"""
from __future__ import annotations

import json
import hashlib
import math
import threading
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ._model_registry import CollectionError, ModelRegistry, epoch, file_stamp, timestamp


def _default_registry_path():
    explicit = os.environ.get('MY_AGENTS2_MODELS_PATH')
    if explicit:
        return Path(explicit).expanduser()
    cache = Path(os.environ.get('XDG_CACHE_HOME') or (Path.home() / '.cache')).expanduser()
    return cache / 'my-agents-fleet' / 'models.json'


REGISTRY_PATH = _default_registry_path()

# 档位从低到高。clamp 靠这个序，别改顺序。
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")


@dataclass
class ModelInfo:
    """一个模型的事实。efforts 的三种状态要分清：

        None  未知 —— 没采到数据，调用方自己看着办（本库透传）
        ()    明确不支持 effort（例如 claude-haiku-4-5）
        (...) 真实支持的档位
    """
    slug: str
    label: str = ""
    efforts: Optional[Tuple[str, ...]] = None
    default_effort: str = ""
    family: str = ""              # agy 专用：档位编在 slug 里，family 是去掉档位后的名
    source: str = ""              # cache | command | binary

    collected_at: Optional[str] = None
    stale: bool = False
    refresh_error: Optional[str] = None

    def supports_effort(self) -> Optional[bool]:
        """True 支持 / False 明确不支持 / None 未知。"""
        return None if self.efforts is None else bool(self.efforts)


# ────────────────────────── 读（使用方用这些） ──────────────────────────

_registry_instance = None
_registry_guard = threading.Lock()


def _seconds(name, default):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and value > 0 else default
    except (ValueError, TypeError):
        return default


def _registry():
    global _registry_instance
    path = Path(REGISTRY_PATH).resolve()
    with _registry_guard:
        if _registry_instance is None or _registry_instance.path != path:
            _registry_instance = ModelRegistry(path, COLLECTORS, _source_signature)
        store = _registry_instance
        store.ttl = _seconds('MY_AGENTS2_MODELS_TTL_SECONDS', 21600)
        store.retry_interval = _seconds('MY_AGENTS2_MODELS_RETRY_SECONDS', 300)
        return store


def _auto_refresh():
    return os.environ.get('MY_AGENTS2_AUTO_REFRESH', '1').lower() not in ('0', 'false', 'off', 'no')


def load_registry(refresh_cache: bool = False) -> dict:
    """读取本地快照；文件变化会自动重读。此底层方法不运行采集器。"""
    return _registry().read(force=refresh_cache)


def models_status(backend: Optional[str] = None, *, refresh: bool = False) -> dict:
    """返回各后端实际采集时间、来源时间、是否过期及最近错误；默认只读。"""
    names = [backend] if backend else list(COLLECTORS)
    store = _registry()
    if refresh and _auto_refresh():
        store.ensure(names)
    result = {name: store.status(name) for name in names}
    return result[backend] if backend else result


def list_models(backend: str, *, refresh: bool = True) -> List[ModelInfo]:
    """查模型，默认先检查有效期和来源变化；refresh=False 只读本地快照。"""
    store = _registry()
    if refresh and _auto_refresh():
        store.ensure([backend])
    entry = store.read().get('backends', {}).get(backend) or {}
    status = store.status(backend)
    return [ModelInfo(slug=m['slug'], label=m.get('label', ''),
                      efforts=tuple(m['efforts']) if m.get('efforts') is not None else None,
                      default_effort=m.get('default_effort', ''), family=m.get('family', ''),
                      source=entry.get('source', ''), collected_at=entry.get('collected_at'),
                      stale=status['stale'], refresh_error=status['last_error'])
            for m in entry.get('models', [])]


def find_model(backend: str, model: Optional[str]) -> Optional[ModelInfo]:
    """按 slug 或 family 找一个模型。"""
    if not model:
        return None
    def match(models):
        for m in models:
            if m.slug == model:
                return m
        for m in models:
            if m.family and m.family == model:
                return m
        return None
    found = match(list_models(backend))
    if found is None and _auto_refresh():
        # 新模型未收录时提前复查，但最多每 5 分钟一次，避免每轮重复启动 CLI。
        store = _registry()
        store.ensure([backend], max_age=store.retry_interval)
        found = match(list_models(backend, refresh=False))
    return found


def effort_options(backend: str, model: Optional[str] = None) -> Optional[Tuple[str, ...]]:
    """这个模型能开哪些 effort 档。

        None  未知（没采集到 / 词典里没这个模型）
        ()    明确不支持
        (...) 真实档位

    model 省略 → 返回该后端所有模型档位的并集（当"这个 CLI 大致支持什么"看）。
    """
    info = find_model(backend, model)
    if info is not None:
        return info.efforts
    if model:
        return None                       # 指名道姓问了但词典里没有 → 未知
    models = list_models(backend)
    if not models:
        return None
    union = {e for m in models for e in (m.efforts or ())}
    return tuple(x for x in EFFORT_ORDER if x in union) if union else None


def resolve_effort(backend: str, model: Optional[str],
                   requested: Optional[str]) -> Tuple[Optional[str], str]:
    """把调用方要的档位落到这个模型真实可用的档上。

    返回 (实际用的档, 说明)。说明非空 = 发生了调整，调用方能在 ag.ignored 里看到。

    夹取规则：**往下夹到最近的可用档**，不往上升（升档要花更多钱和时间，不该替人决定）。
    你要 max 但这个模型最高只到 xhigh → 给 xhigh，而不是砸回 high。
    """
    if not requested:
        return None, ""
    opts = effort_options(backend, model)
    if opts is None:
        return requested, ""                                   # 未知 → 透传，不假装懂
    if not opts:
        return None, f"{model or backend} 不支持 effort，已忽略 effort={requested}"
    if requested in opts:
        return requested, ""
    known_opts = [o for o in opts if o in EFFORT_ORDER]
    if not known_opts:
        return requested, '词典只包含适配器尚不认识的 effort 档位，原样传入所请求档位'
    # 夹取：取 <= requested 的最高档；若比所有档都低，取最低档
    want = EFFORT_ORDER.index(requested) if requested in EFFORT_ORDER else len(EFFORT_ORDER)
    below = [o for o in opts if o in EFFORT_ORDER and EFFORT_ORDER.index(o) <= want]
    picked = max(below, key=EFFORT_ORDER.index) if below else min(known_opts, key=EFFORT_ORDER.index)
    return picked, f"effort={requested} 在 {model or backend} 上不可用，已夹到 {picked}（可用：{'/'.join(opts)}）"


def resolve_model_slug(backend: str, model: Optional[str],
                       effort: Optional[str]) -> Tuple[Optional[str], bool]:
    """agy 专用：它把档位编进 slug（gemini-3.7-flash + high → gemini-3.7-flash-high）。

    返回 (最终 slug, 档位是否已经吃进 slug)。第二个值为 True 时调用方**不该**再拼 --effort。

    ⚠️ 必须在 `resolve_effort()` **之后**调用，传夹取过的档位进来。
       先合成 slug 再夹档会得到自相矛盾的结果（slug 是 -high 但档位说 low）。

    传完整变体 slug + 显式 effort 时，**effort 赢** —— 它是统一入参，调用方可能正在
    拿它做对照实验；slug 后缀只是 agy 的表达方式，不该压过显式意图。
    """
    if not model:
        return model, False
    info = find_model(backend, model)
    if info is None:
        return model, False
    if not info.family:                  # 档位不编在 slug 里（claude-sonnet-4-6 这类）
        return info.slug, False
    if effort:
        target = f"{info.family}-{effort}"
        if any(m.slug == target for m in list_models(backend)):
            return target, True
    return info.slug, True


# ────────────────────────── 写（my_agents2 自己的事） ──────────────────────────

def _effort_rank(e):
    return EFFORT_ORDER.index(e) if e in EFFORT_ORDER else len(EFFORT_ORDER)


def _executable(name):
    exe = shutil.which(name)
    if not exe:
        raise CollectionError(f'找不到 {name} CLI')
    return exe


def _run_metadata(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors='replace',
                              timeout=_seconds('MY_AGENTS2_MODELS_TIMEOUT_SECONDS', 30),
                              stdin=subprocess.DEVNULL, cwd=str(Path.home()))
    except subprocess.TimeoutExpired as exc:
        raise CollectionError('模型元数据命令超时') from exc
    except OSError as exc:
        raise CollectionError('无法启动模型元数据命令') from exc
    if proc.returncode != 0:
        # stderr 可能包含认证信息，只存退出码，不把原文落盘或展示。
        raise CollectionError(f'模型元数据命令失败，退出码 {proc.returncode}')
    return proc.stdout


def _cache_updated_at(data, path):
    value = data.get('fetched_at')
    return value if epoch(value) else timestamp(path.stat().st_mtime)


def _source_signature(backend):
    exe = shutil.which(backend)
    stamps = [file_stamp(exe)] if exe else [[backend, 'missing']]
    if exe:
        # npm 的入口文件有时保持不变，包版本文件同样参与升级检测。
        for parent in list(Path(exe).resolve().parents)[:4]:
            package = parent / 'package.json'
            if package.is_file():
                stamps.append(file_stamp(package))
                break
    if backend in ('codex', 'grok'):
        stamps.append(file_stamp(Path.home() / ('.' + backend) / 'models_cache.json'))
    if backend == 'pi':
        for name in ('models.json', 'auth.json', 'settings.json'):
            stamps.append(file_stamp(Path.home() / '.pi' / 'agent' / name))
    return hashlib.sha256(json.dumps(stamps, ensure_ascii=False).encode()).hexdigest()


def _collect_codex() -> Optional[dict]:
    """codex：读它自己维护的 models_cache.json（CLI 从 API 拉的，会自动刷新）。"""
    p = Path.home() / ".codex" / "models_cache.json"
    if not p.exists():
        raise CollectionError("CLI 模型缓存不存在，请先使用对应 CLI 更新缓存")
    d = json.loads(p.read_text(encoding="utf-8"))
    models = []
    for m in d.get("models", []):
        lv = [x.get("effort") for x in (m.get("supported_reasoning_levels") or []) if x.get("effort")]
        models.append({"slug": m["slug"], "label": m.get("display_name", ""),
                       "efforts": lv if isinstance(m.get("supported_reasoning_levels"), list) else None,
                       "default_effort": m.get("default_reasoning_level", "")})
    return {"source": "cache", "origin": str(p),
            "source_updated_at": _cache_updated_at(d, p), "models": models} if models else None


def _collect_grok() -> Optional[dict]:
    """grok：同样读它的 models_cache.json。"""
    p = Path.home() / ".grok" / "models_cache.json"
    if not p.exists():
        raise CollectionError("CLI 模型缓存不存在，请先使用对应 CLI 更新缓存")
    d = json.loads(p.read_text(encoding="utf-8"))
    models = []
    for slug, v in (d.get("models") or {}).items():
        info = v.get("info") or {}
        raw_efforts = info.get("reasoning_efforts")
        lv = [x.get("value") for x in (raw_efforts or []) if x.get("value")]
        if info.get("supports_reasoning_effort") is False:
            lv = []
        elif not lv:
            lv = None
        models.append({"slug": slug, "label": info.get("name", ""),
                       "efforts": (sorted(set(lv), key=_effort_rank)
                                   if lv else ([] if lv == [] else None)),
                       "default_effort": info.get("reasoning_effort", "")})
    return {"source": "cache", "origin": str(p),
            "source_updated_at": _cache_updated_at(d, p), "models": models} if models else None


_AGY_EFFORT_SUFFIX = re.compile(r"^(?P<family>.+)-(?P<effort>" + "|".join(EFFORT_ORDER) + r")$")


def _collect_agy() -> Optional[dict]:
    """agy：跑 `agy models`。它把档位编在 slug 后缀里（gemini-3.7-flash-high）。"""
    exe = _executable('agy')
    out = _run_metadata([exe, 'models'])

    variants: Dict[str, List[str]] = {}
    rows: List[Tuple[str, str]] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        slug, label = line.split("\t", 1)
        slug, label = slug.strip(), label.strip()
        if not slug or " " in slug:
            continue
        rows.append((slug, label))
        m = _AGY_EFFORT_SUFFIX.match(slug)
        # 只有 display_name 也带 (Low/Medium/High) 才算档位变体，
        # 免得把 claude-sonnet-4-6 (Thinking) 这种误判
        if m and re.search(r"\((?:" + "|".join(EFFORT_ORDER) + r")\)$", label, re.I):
            variants.setdefault(m.group("family"), []).append(m.group("effort"))

    models = []
    for slug, label in rows:
        m = _AGY_EFFORT_SUFFIX.match(slug)
        fam = m.group("family") if (m and m.group("family") in variants) else ""
        eff = variants.get(fam) if fam else None
        models.append({"slug": slug, "label": label, "family": fam,
                       "efforts": (sorted(set(eff), key=EFFORT_ORDER.index) if eff else None),
                       "default_effort": ""})
    return {"source": "command", "origin": f"{exe} models", "models": models} if models else None


def _collect_pi() -> Optional[dict]:
    """Pi：跑 `pi --list-models`，只收当前认证状态下真正可选的模型。

    表格只告诉我们 thinking=yes/no，不公开每个模型的精确 effort 档位：
    - thinking=no  → efforts=[]，明确不支持
    - thinking=yes → efforts=None，档位未知，调用时交给 Pi 自己解析
    """
    exe = _executable('pi')
    out = _run_metadata([exe, '--list-models'])

    models = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[0].lower() == "provider":
            continue
        provider, model = fields[0], fields[1]
        thinking = fields[4].lower()
        if thinking not in ("yes", "no"):
            continue
        slug = f"{provider}/{model}"
        models.append({
            "slug": slug,
            "label": slug,
            "efforts": None if thinking == "yes" else [],
            "default_effort": "",
        })
    return {
        "source": "command",
        "origin": f"{exe} --list-models",
        "models": models,
    } if models else None


# claude 的模型表编译在二进制里，形如：
#   {id:"claude-opus-5",family:"opus",display_name:"Opus 5",...,
#    capabilities:["effort","max_effort","xhigh_effort",...],default_effort:"high"}
_CLAUDE_MODEL_RE = re.compile(
    r'\{id:"(?P<id>[a-z0-9.\-]+)",family:"(?P<family>\w+)",display_name:"(?P<name>[^"]+)"'
    r'.*?capabilities:\[(?P<caps>[^\]]*)\](?:,default_effort:"(?P<def>\w+)")?'
)


def _claude_efforts(caps: Sequence[str]) -> Optional[List[str]]:
    """capabilities 数组 → 真实档位。

        无 "effort"                     → []        明确不支持（如 haiku-4-5）
        "effort"                        → low/medium/high
        + "xhigh_effort"                → 加 xhigh
        + "max_effort"                  → 加 max
    """
    if "effort" not in caps:
        return []
    lv = ["low", "medium", "high"]
    for level in EFFORT_ORDER[3:]:
        if f'{level}_effort' in caps:
            lv.append(level)
    return sorted(set(lv), key=EFFORT_ORDER.index)


def _collect_claude() -> Optional[dict]:
    """claude：模型表编译进二进制，只能 strings + 正则挖。

    ⚠️ 这是五个数据源里唯一**脆**的一个：绑的是压缩 JS 的字面形状，
    claude 一升级改结构就可能断。失败由目录层记录原因并保留旧表，
    不影响其他后端的刷新，也不把失败时间冒充成功采集时间。
    """
    exe = _executable('claude')
    real = os.path.realpath(exe)
    out = _run_metadata(['strings', '-n', '6', real])

    models: Dict[str, dict] = {}
    for m in _CLAUDE_MODEL_RE.finditer(out):
        caps = [c.strip('"') for c in m.group("caps").split(",") if c.strip()]
        models[m.group("id")] = {
            "slug": m.group("id"), "label": m.group("name"),
            "efforts": _claude_efforts(caps),
            "default_effort": m.group("def") or "",
        }
    if not models:
        raise CollectionError('无法解析当前 Claude 程序的模型表，保留旧表')
    return {"source": "binary", "origin": real, "models": list(models.values())}


COLLECTORS = {
    "codex": _collect_codex,
    "grok": _collect_grok,
    "agy": _collect_agy,
    "pi": _collect_pi,
    "claude": _collect_claude,
}


def refresh_models_registry(backends: Optional[Sequence[str]] = None, *, now: str = "") -> dict:
    """强制采集指定后端或全部后端；失败保留旧表，返回每个后端的结果。"""
    if now and not epoch(now):
        raise ValueError('now 必须是有效的 ISO 时间')
    return _registry().ensure(list(COLLECTORS) if backends is None else backends,
                              force=True, now=now)
