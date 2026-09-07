"""命令行冒烟测试：

    python3 -m my_agents2 --caps                        # 先看能力矩阵，挑后端
    python3 -m my_agents2 "你的话"                       # 用默认 agent
    python3 -m my_agents2 "你的话" codex                 # 指定 agent
    python3 -m my_agents2 "你的话" codex readonly high   # 指定 mode 和 effort

Install this project first: python -m pip install -e .
"""
import sys
import unicodedata

from . import caps_table, get_agent, list_agents
from .base import ENFORCEMENT


def _w(s: str) -> int:
    """终端显示宽度：中文和 ✅ 占两列，f-string 的 :<n 按字符数算会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def print_caps() -> None:
    cols = [("agent", 10), ("只读实施", 17), ("effort", 8), ("工具事件", 10),
            ("推理事件", 10), ("逐token", 9), ("成本", 6), ("系统提示", 10)]
    print("".join(_pad(h, w) for h, w in cols))
    print("-" * sum(w for _, w in cols))
    for nm, c in caps_table().items():
        y = lambda b: "✅" if b else "— "   # noqa: E731
        cells = [nm, c.readonly, y(c.effort), y(c.tool_events), y(c.thought_events),
                 y(c.token_stream), y(c.cost), y(c.system_prompt)]
        print("".join(_pad(v, w) for v, (_, w) in zip(cells, cols)))
    print()
    for k, v in ENFORCEMENT.items():
        print(f"  {_pad(k, 17)}{v}")
    print()
    print("  场景1 编队派活 → run()，看「工具事件」（抓交付物）")
    print("  场景2 chat box → conversation().stream()，看「逐token」（打字机效果）")


def print_models(backend: str = "", *, cached: bool = False) -> None:
    """使用方挑入参用这张表：哪个 CLI 有哪些模型、每个模型能开哪些 effort。"""
    from . import list_models, models_status
    for name in ([backend] if backend else ["claude", "codex", "grok", "agy", "pi"]):
        ms = list_models(name, refresh=not cached)
        status = models_status(name)
        print(f"── {name} · 最后成功采集：{status['collected_at'] or '从未采集'}")
        if status['source_updated_at']:
            print(f"   CLI 缓存时间：{status['source_updated_at']}")
        if status['stale']:
            print('   ⚠ ' + '；'.join(status['stale_reasons']))
        if not ms:
            print('   没有可用模型表；这表示未知，不代表 CLI 没有模型。\n')
            continue
        src = ms[0].source
        print(f"── {name}（数据源 {src}{'，脆：挖二进制' if src == 'binary' else ''}）")
        slug_width = max(26, min(52, max(_w(m.slug) for m in ms) + 2))
        for m in ms:
            eff = "未知" if m.efforts is None else ("不支持 effort" if not m.efforts
                                                   else "/".join(m.efforts))
            star = f"  默认={m.default_effort}" if m.default_effort else ""
            print(f"   {_pad(m.slug, slug_width)}{_pad(eff, 34)}{star}")
        print()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("--caps", "-c"):
        print_caps()
        return
    if args and args[0] == "--models":
        names = [a for a in args[1:] if a != '--cached']
        print_models(names[0] if names else '', cached='--cached' in args)
        return
    if args and args[0] == '--models-status':
        import json
        from . import models_status
        print(json.dumps(models_status(args[1] if len(args) > 1 else None), ensure_ascii=False, indent=2))
        return
    if args and args[0] == "--refresh-models":
        from . import refresh_models_registry
        rep = refresh_models_registry(args[1:] or None)
        for k, v in rep.items():
            print(f"  {_pad(k, 10)}{v}")
        if any(not value.startswith('ok ') for value in rep.values()):
            raise SystemExit(1)
        return
    if not args or args[0] in ('--help', '-h'):
        print('用法: python3 -m my_agents2 "你的话" [agent名] [full|readonly] [effort]')
        print("      python3 -m my_agents2 --caps             各后端能力矩阵")
        print("      python3 -m my_agents2 --models [后端]     可选模型 + 每个模型的 effort 档位")
        print("      python3 -m my_agents2 --models --cached 只看缓存，不触发采集")
        print("      python3 -m my_agents2 --models-status   查看各后端时效和错误（JSON）")
        print("      python3 -m my_agents2 --refresh-models [后端]  强制重新采集")
        print("可选 agent:", ", ".join(list_agents()))
        return

    prompt = args[0]
    name = args[1] if len(args) > 1 else None
    kwargs = {}
    if len(args) > 2:
        kwargs["mode"] = args[2]
    if len(args) > 3:
        kwargs["effort"] = args[3]

    ag = get_agent(name, **kwargs)
    print(f"# {ag.explain()}\n")

    for ev in ag.stream(prompt):
        if ev.kind == "log":
            print(f"[cmd]  {ev.text}")
        elif ev.kind == "init":
            print(f"[init] {ev.text}")
        elif ev.kind == "thought":
            print(f"[想]   {ev.text[:200]}")
        elif ev.kind == "text":
            print(f"[说]   {ev.text}")
        elif ev.kind == "tool_use":
            print(f"[工具] {ev.name}: {ev.command}")
        elif ev.kind == "tool_result":
            tag = " 报错" if ev.is_error else ""
            print(f"[输出{tag}] {ev.text[:200]}")
        elif ev.kind == "result":
            print(f"\n=== 结果 ===\n{ev.text}")
        elif ev.kind == "error":
            print(f"[错误] {ev.text}")


if __name__ == "__main__":
    main()
