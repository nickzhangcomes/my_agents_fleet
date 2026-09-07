"""Fleet —— 一支常驻队伍的门面。

    fleet = load_fleet("/你的业务项目/某处/myfleet")
    ag    = fleet.get("哲学老师")                  # → RecordedAgent（自动留痕）
    out   = ag.ask("讲讲黑格尔", ref="episode:37", trigger="cron")
    out.structured                                # 要了 schema 就是 dict
    fleet.stats(since="2026-08-01")               # 这个月各 agent 跑成什么样

★ **只用 my_agents2 的公开 API**（`get_agent` / `caps_table` / `list_agents`）。
  1.0 曾经去摸 `my_agents._load_config()` / `._IMPLS`，再用 `inspect.signature`
  猜每个后端的构造器收哪些参数，然后一串
  `if "append_system_prompt" in accepted: … elif "system_prompt_override" in accepted: …`
  —— 等于**中台要背下每个后端的参数名**，后端一改名中台就崩。

  2.0 里这些全没了：统一入参五个后端收同一套，能力查 `caps` 显式声明。
  后端专属参数（mcp_config 之类）走 `extra`；受约束调用禁止覆盖权限。
"""
from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from my_agents2 import caps_table, get_agent, list_agents

from . import contract as C
from . import identity as I
from ._files import atomic_json, file_lock
from .capacity import Capacity
from .dispatch import Outcome, RecordedAgent
from .failures import KINDS, PLATFORM as C_PLATFORM, Failure
from .runs import STATE_FAILED, Run, RunStore, new_run_id
from .session import Session
from .store import JsonFileStore

# agents2 的统一入参 + 通用构造参数（原样转发；后端专属的走 extra）
_PASSTHROUGH = {"mode", "model", "effort", "timeout", "token_stream",
                "tool_result_limit", "style", "summary_limit", "output_schema", "deny"}


class FleetConfigError(ValueError):
    """配置本身有问题（引用的文件不存在、后端不认识、参数后端不收…）。加载期就该报出来。"""


_CAPACITIES: Dict[str, Capacity] = {}
_CAP_GUARD = threading.Lock()


def _lock_target(setting, cwd: str, base: str) -> Optional[str]:
    """`lock_workspace` 三种写法：`true` 锁自己的工作目录 · 字符串锁指定目录 · 不写不锁。

    默认不锁：加锁会让并发派活退化成串行，**得是你明确要的**才加。
    """
    if setting is True:
        return cwd
    if isinstance(setting, str) and setting.strip():
        p = Path(os.path.expanduser(setting))
        return str((p if p.is_absolute() else Path(base) / p).resolve())
    return None


def _capacity_for(lock_dir) -> Capacity:
    """按锁目录取进程内单例的 Capacity（见上面为什么不能每个 Fleet 各带一套）。"""
    key = str(Path(lock_dir).resolve())
    with _CAP_GUARD:
        if key not in _CAPACITIES:
            _CAPACITIES[key] = Capacity(key)
        return _CAPACITIES[key]


class Fleet:
    """一份 fleet 配置实例化后的对象：全局段 + agent 名册 + 状态目录。"""

    def __init__(self, config: dict, fleet_dir: Path, base: str, *, _validate_only=False):
        if not isinstance(config, dict):
            raise FleetConfigError('fleet.json 顶层必须是对象')
        for key in ('global', 'agents'):
            if not isinstance(config.get(key, {}), dict):
                raise FleetConfigError(f'{key} 必须是对象')
        global_cfg = config.get('global', {})
        for key in ('defaults', 'shared'):
            if not isinstance(global_cfg.get(key, {}), dict):
                raise FleetConfigError(f'global.{key} 必须是对象')
        for name, entry in config.get('agents', {}).items():
            if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
                raise FleetConfigError('角色名必须非空，每个角色配置必须是对象')
        self.config = config
        self.fleet_dir = Path(fleet_dir)
        self.base = base
        self._global = config.get("global", {}) or {}
        self._agents = config.get("agents", {}) or {}

        # 状态区（Run 库 + 会话）—— 照 oss 的分法：配置可 review、状态不进配置
        state_dir = self._global.get("state_dir") or "state"
        self.state_dir = self._resolve(state_dir)
        self.runs = None if _validate_only else RunStore(Path(self.state_dir) / "fleet.db")
        self.sessions_dir = self._resolve(self._global.get("sessions_dir")
                                          or str(Path(self.state_dir) / "sessions"))
        # 容量/锁必须**跨 Fleet 实例共享**：load_fleet 每次都新读磁盘、每次都造新 Fleet，
        # 每个实例各带一套信号量的话并发上限等于形同虚设。按状态目录做进程内单例。
        self.capacity = None if _validate_only else _capacity_for(Path(self.state_dir) / "locks")

        # 加载期就把 .md 读出来 + 校验后端存在 —— 别等到真派活才发现配错了
        shared = self._global.get("shared", {}) or {}
        self._global_md = self._read_rel(shared.get("instructions_file"))
        self._agent_md: Dict[str, str] = {
            n: self._read_rel((a or {}).get("instructions_file"))
            for n, a in self._agents.items()}

        known = set(list_agents())
        for n, a in self._agents.items():
            b = (a or {}).get("backend") or self._defaults().get("backend") or "claude"
            if b not in known:
                raise FleetConfigError(
                    f"agent '{n}' 的 backend='{b}' 不在 my_agents2 预设里，可选: {sorted(known)}")

    # ── 路径 / 文件 ──
    def _resolve(self, rel: Optional[str]) -> str:
        if not rel:
            return str(self.fleet_dir)
        p = Path(os.path.expanduser(rel))
        return str(p if p.is_absolute() else self.fleet_dir / p)

    def _read_rel(self, rel: Optional[str]) -> str:
        if not rel:
            return ""
        p = Path(os.path.expanduser(rel))
        p = p if p.is_absolute() else self.fleet_dir / p
        if not p.exists():
            raise FleetConfigError(f"引用的长文不存在: {p}")
        return p.read_text(encoding="utf-8")

    def _defaults(self) -> dict:
        return self._global.get("defaults", {}) or {}

    # ── 名册 ──
    def names(self, *, active_only: bool = False) -> List[str]:
        return [n for n, a in self._agents.items()
                if not active_only or (a or {}).get("status", "active") == "active"]

    def entry(self, name: str) -> dict:
        if name not in self._agents:
            raise FleetConfigError(f"fleet 里没有 agent: {name}，可选: {self.names()}")
        e = {**self._defaults(), **(self._agents[name] or {})}
        e['extra'] = {**(self._defaults().get('extra') or {}),
                      **(self._agents[name].get('extra') or {})}
        return e

    def describe(self) -> str:
        rows = []
        for n in self.names():
            e = self.entry(n)
            rows.append(f"  {n:14} {e.get('backend', 'claude'):8} {e.get('model') or '-':22} "
                        f"{e.get('status', 'active'):8} {e.get('intro', '')[:30]}")
        return f"fleet @ {self.fleet_dir}\n" + "\n".join(rows)

    # ── 取一个 agent ──
    def get(self, name: str, *, raw: bool = False, session_id: str = None, **overrides):
        """装配好一个 agent。默认返回 `RecordedAgent`（每次调用自动落 Run）。

        `raw=True` 显式绕过运行记录与并发治理；做流式界面通常直接用默认实例的 stream。
        """
        entry = self.entry(name)
        e = {**entry, **overrides}
        e['extra'] = {**(entry.get('extra') or {}), **(overrides.get('extra') or {})}
        protected = _PASSTHROUGH | {'cwd', 'add_dirs', 'system_prompt', 'name'}
        conflicts = sorted(protected.intersection(e['extra']))
        if conflicts:
            raise FleetConfigError(f'extra 不能覆盖统一参数 {conflicts}，请放在角色配置顶层')
        for key, minimum in [('max_attempts', 1), ('max_concurrent', 0)]:
            try:
                value = int(e.get(key, minimum) if e.get(key) is not None else minimum)
            except (ValueError, TypeError, OverflowError) as ex:
                raise FleetConfigError(f'{key} 必须是整数') from ex
            if isinstance(e.get(key), bool) or (isinstance(e.get(key), float) and e[key] != value):
                raise FleetConfigError(f'{key} 必须是整数')
            if value < minimum:
                raise FleetConfigError(f'{key} 必须 >= {minimum}')
        for key, minimum in [('timeout', 0), ('workspace_timeout', 0)]:
            if e.get(key) is not None:
                try:
                    value = float(e[key])
                except (ValueError, TypeError) as ex:
                    raise FleetConfigError(f'{key} 必须是数字') from ex
                if not math.isfinite(value) or value < minimum or (key == 'timeout' and value == 0):
                    raise FleetConfigError(f'{key} 数值无效')
                e[key] = value
        backend = e.get("backend") or "claude"
        caps = caps_table().get(backend)
        if caps is None:
            raise FleetConfigError(f"backend='{backend}' 不在 my_agents2 预设里")

        if e.get('output_schema') is not None and not caps.structured_output:
            raise FleetConfigError(f"角色 '{name}' 要求结构化输出，但后端 '{backend}' 尚未接入该能力")
        if e.get('mode') == 'readonly' and caps.readonly == 'none':
            raise C.ContractError(f"后端 '{backend}' 无法落实只读要求")

        cwd = I.resolve_workspace(e.get("workspace"), self.base)
        shared = self._global.get("shared", {}) or {}
        mcp = I.merge_mcp_config(shared.get("mcp_config"), e.get("mcp_config"),
                                 cache_dir=str(Path(self.state_dir) / "mcp"))

        # ── 契约：翻译不下去的安全项直接抛，不静默降级 ──
        assets = C.parse_assets(e.get("assets"), base=str(self.fleet_dir))
        rc = C.resolve(trust=e.get("trust", C.TRUST_TRUSTED), assets=assets,
                       caps=caps, backend=backend, requested_mode=e.get("mode"))
        constrained = rc.mode == 'readonly' or e.get('mode') == 'readonly' or bool(e.get('deny'))
        if constrained:
            unsafe = {'permission_mode', 'allowed_tools', 'deny_rules', 'allow', 'extra_args'}
            unsafe.update(k for k in e['extra'] if k.endswith('_bin'))
            conflicts = sorted(k for k in unsafe if e['extra'].get(k))
            if conflicts:
                raise C.ContractError(f'受约束调用不能用 extra 改写权限或执行入口: {conflicts}')
        if rc.trust == C.TRUST_UNTRUSTED and (mcp or e['extra'].get('mcp_config')):
            raise C.ContractError('不可信输入不能挂载未受能力拒绝规则约束的 MCP 工具')
        add_dirs = I.merge_add_dirs(shared.get("add_dirs"),
                                    list(e.get("add_dirs") or []) + rc.asset_dirs)

        # 后端落不了地的就不往下传（agents2 会记进 ignored，但 cwd/add_dirs 提前剔掉更干净）
        eff_cwd = cwd if caps.cwd else None
        eff_dirs = add_dirs if caps.add_dirs else []

        sp = I.compose_system_prompt(
            business_scenario=self._global.get("business_scenario", "") or "",
            global_md=self._global_md,
            constraints=self._global.get("constraints"),
            caps_note=I.caps_note(eff_cwd, eff_dirs, bool(mcp), extra_notes=rc.notes),
            intro=e.get("intro", "") or "",
            work_md=self._agent_md.get(name, ""),
            skills=e.get("skills") or shared.get("skills"))

        kwargs = {k: v for k, v in e.items() if k in _PASSTHROUGH and v is not None}
        if rc.mode:
            kwargs["mode"] = rc.mode        # 契约压过配置里的 mode（只往严的方向压）
        if rc.deny:
            kwargs["deny"] = list(dict.fromkeys(list(e.get("deny") or []) + list(rc.deny)))
        # ⚠ 不能传 name= —— get_agent() 的第一个形参就叫 name（= 预设名），会撞参。
        #   显示名在构造后直接打到实例上。
        kwargs.update(cwd=eff_cwd, add_dirs=eff_dirs or None, system_prompt=sp or None)
        extra = dict(e.get("extra") or {})
        if mcp:
            extra.setdefault("mcp_config", mcp)   # 后端专属：中台原样转发，不解释
        if extra:
            kwargs["extra"] = extra

        try:
            ag = get_agent(backend, **kwargs)
        except TypeError as ex:
            # 配置错误说清楚是哪个键 + 哪个后端，别让调用方去读 traceback 猜
            raise FleetConfigError(
                f"agent '{name}' 的参数后端 '{backend}' 收不下：{ex}。"
                f"后端专属参数请放进该 agent 的 extra 段。") from ex
        if rc.mode and ag.mode != rc.mode:
            raise C.ContractError('实际权限与契约不匹配')
        actual_deny = getattr(ag, 'deny_capabilities', ag.deny)
        wanted_deny = set(kwargs.get('deny') or [])
        if not wanted_deny.issubset(actual_deny):
            raise C.ContractError(f'后端未落实要求禁止的能力: {sorted(wanted_deny - set(actual_deny))}')
        rc.deny = tuple(actual_deny)
        ag.name = name          # 显示名 = fleet 里的名字（盖掉预设默认的 backend 名）

        if raw:
            return ag

        return RecordedAgent(
            ag, store=self.runs, name=name, backend=backend,
            fingerprint=I.fingerprint(agent=name, backend=backend,
                                      cwd=eff_cwd, extra_dirs=eff_dirs,
                                      settings={'mode': ag.mode, 'deny': list(actual_deny),
                                                'model': ag.model, 'system_prompt': ag.system_prompt,
                                                'extra': extra}),
            identity_mode=(I.IDENTITY_SYSTEM if caps.system_prompt_native
                           else I.IDENTITY_PREFIX if caps.system_prompt
                           else I.IDENTITY_NONE),
            session_id=session_id,
            max_attempts=int(e.get("max_attempts") or 3),
            contract=rc, capacity=self.capacity,
            max_concurrent=int(e.get("max_concurrent") or 0),
            workspace=_lock_target(e.get("lock_workspace"), cwd, str(self.fleet_dir)),
            workspace_timeout=float(e.get("workspace_timeout") or 0),
            logger=print)

    def spawn_all(self, **overrides) -> Dict[str, object]:
        return {n: self.get(n, **overrides) for n in self.names(active_only=True)}

    # ── 批量派活 ──
    def dispatch(self, jobs, *, workers: int = None, trigger: str = None) -> List:
        """一批活并发跑完，**按传入顺序**返回 Outcome 列表。

            fleet.dispatch([
                {"agent": "标题手", "prompt": "…", "ref": "post:1"},
                {"agent": "调研员", "prompt": "…", "ref": "post:2", "model": "claude-opus-5"},
            ], workers=4, trigger="batch")

        `agent` / `prompt` 必填；`ref` / `trigger` 进 Run 记录；其余键当 `get()` 的 override。

        两层限流，别混：
            `workers`         这个线程池开多大（传输层并发）
            `max_concurrent`  每个 agent 自己的名额（fleet.json 里配，真正的闸门）
        所以 workers 开大不会突破 agent 的上限 —— 多出来的会在信号量上排队，
        并且会在 Run 里记一笔 `waited=capacity`。

        **一个活炸了不会带塌整批**：异常会被收成一条失败的 Outcome，其余照跑完。
        """
        jobs = list(jobs)
        if not jobs:
            return []
        n = workers or min(8, len(jobs))

        def one(job: dict):
            # ⚠ 整个函数体都在 try 里，**校验也算**：一条写错的活（少了 agent/prompt）
            #   如果在 try 之外抛，会顺着 ThreadPoolExecutor 冒到 map 上，把整批带塌 ——
            #   正好违反这个方法承诺的「一个活炸了不影响其余」。
            name = ref = tg = None
            try:
                job = dict(job)
                name = job.pop("agent", None)
                prompt = job.pop("prompt", None)
                ref = job.pop("ref", None)
                tg = job.pop("trigger", None) or trigger
                if not name or prompt is None:
                    raise FleetConfigError(
                        f"批量派活的每条都要有 agent 和 prompt，收到: {dict(job)}")
                return self.get(name, **job).ask(prompt, ref=ref, trigger=tg)
            except Exception as e:                        # noqa: BLE001
                run = getattr(e, "run", None)
                if run is None:
                    code = 'config_error' if isinstance(e, (ValueError, TypeError)) else 'backend_crashed'
                    run = self.runs.begin(Run(
                        run_id=new_run_id(str(name or '?')), agent=str(name or '?'),
                        ref=ref, trigger=tg, state=STATE_FAILED, fail_class=C_PLATFORM,
                        fail_code=code, error=f"{type(e).__name__}: {e}"[:400]))
                    self.runs.finish(run)
                return Outcome(result=None, run=run, attempts=[run],
                               failure=Failure(KINDS.get(run.fail_code, KINDS['backend_crashed']),
                                               f"{type(e).__name__}: {e}"[:200]))

        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(one, jobs))

    # ── 改名册（给带 UI 的业务用）──
    #    刻意只管**这份配置**的校验和原子写：.md 长文由业务自己写。
    #    理由：文件叫什么名、内容什么格式，是业务的事 —— 引擎替它命名反而添乱
    #    （1.0 的 create_agent 会按 safe_filename 生成带 hash 的新文件名，
    #     把业务想要的干净命名 agents/<handle>.md 晾在一边）。
    def upsert_agent(self, name: str, entry: dict, *, overwrite: bool = True) -> "Fleet":
        """新增或改一个 agent 条目，落盘。`entry` 就是 fleet.json 里那一段。"""
        name = str(name or "").strip()
        if not name:
            raise FleetConfigError("agent 名字不能为空")
        if not isinstance(entry, dict):
            raise FleetConfigError("entry 必须是 dict")
        with file_lock(self.fleet_dir / '.fleet.lock'):
            raw = self._read_raw()
            if not overwrite and name in raw.get('agents', {}):
                raise FleetConfigError(f"agent '{name}' 已存在")
            raw.setdefault('agents', {})[name] = {k: v for k, v in entry.items() if v is not None}
            self._write_raw(raw)
        return self

    def remove_agent(self, name: str) -> bool:
        """只改名册，保留会话和业务文件。"""
        with file_lock(self.fleet_dir / '.fleet.lock'):
            raw = self._read_raw()
            if name not in raw.get('agents', {}):
                return False
            del raw['agents'][name]
            self._write_raw(raw)
            return True

    def _read_raw(self) -> dict:
        return json.loads((self.fleet_dir / 'fleet.json').read_text(encoding='utf-8'))

    def _write_raw(self, raw: dict) -> None:
        """整份配置验证通过后才原子替换，失败保留原文件。"""
        candidate = Fleet(raw, self.fleet_dir, self.base, _validate_only=True)
        for name in candidate.names():
            candidate.get(name, raw=True)
        atomic_json(self.fleet_dir / 'fleet.json', raw)
        for attr in ('config', '_global', '_agents', '_global_md', '_agent_md'):
            setattr(self, attr, getattr(candidate, attr))

    # ── 持久会话 ──
    def session(self, name: str, session_id: str, *, store=None,
                context_providers=None, logger=None, **overrides) -> Session:
        """开一个跨进程的持久会话。和 `get(name).conversation()` 的区别：
           conversation 只在进程内记句柄；session 落盘，换进程/重启也能续，
           续不上还能用历史重建。"""
        self.entry(name)   # 名字不存在就早报错
        return Session(self, name, session_id,
                       store or JsonFileStore(self.sessions_dir),
                       context_providers=context_providers,
                       overrides=overrides, logger=logger)

    # ── 回头看 ──
    def stats(self, **kw) -> List[dict]:
        """按 agent 汇总：跑几次 / 成几次 / 失败分平台侧还是 agent 侧 / 花多少 / 平均多久。"""
        return self.runs.stats(**kw)

    def fail_breakdown(self, **kw) -> List[dict]:
        """失败码分布 —— 「失败率 12%」没用，「12% 里 9% 是超时」才动得了手。"""
        return self.runs.fail_breakdown(**kw)

    def history(self, **kw):
        """按 agent / ref / 状态 / 时间查 Run 记录。"""
        return self.runs.query(**kw)


def load_fleet(fleet_dir, base: str = None) -> Fleet:
    """读一个 fleet 目录 → Fleet。

        某个fleet/
        ├── fleet.json        全局段 + agent 名册（可 review 的配置）
        ├── global.md         全队共通的长文
        ├── agents/<名>.md    每个角色的工作内容
        └── state/            Run 库 + 会话（运行状态，gitignore）

    每次调用都从磁盘重读 —— 「磁盘即事实源」：改完人设立刻生效，不用管缓存失效。
    读几个小文件，开销可忽略。
    """
    d = Path(os.path.expanduser(str(fleet_dir))).resolve()
    f = d / "fleet.json"
    if not f.exists():
        raise FleetConfigError(f"找不到 fleet.json: {f}")
    try:
        cfg = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise FleetConfigError(f"fleet.json 不是合法 JSON: {e}") from e
    return Fleet(cfg, d, base=str(base or d))
