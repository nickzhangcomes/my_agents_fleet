"""Command-line entry points for the standalone distribution."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import unicodedata
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__, load_fleet


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def _table(headers, rows) -> str:
    """按显示宽度对齐的纯文本表格（中文按两列算）。"""
    cells = [[str(c) for c in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(_width(row[i]) for row in cells) for i in range(len(headers))]
    lines = []
    for n, row in enumerate(cells):
        lines.append('  '.join(c + ' ' * (widths[i] - _width(c)) for i, c in enumerate(row)).rstrip())
        if n == 0:
            lines.append('  '.join('-' * w for w in widths))
    return '\n'.join(lines)


# ── init ──

def _init(directory):
    destination = Path(directory).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f'Destination must be a new or empty directory: {destination}')
    with resources.as_file(resources.files('my_agents_fleet').joinpath('templates/basic')) as source:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    print(f'Created fleet: {destination}')


# ── run ──

def _read_prompt(args, parser) -> str:
    sources = [args.prompt is not None and args.prompt != '-', args.prompt == '-', args.file is not None]
    if sum(sources) != 1:
        parser.error('run 需要且只能有一个任务来源：直接写任务文字、用 - 从标准输入读取，或用 --file 指定文件')
    if args.file is not None:
        return Path(args.file).expanduser().read_text(encoding='utf-8')
    if args.prompt == '-':
        return sys.stdin.read()
    return args.prompt


# ── runs ──

def _fmt_cost(row) -> str:
    if row.get('cost') is not None:
        return f"${row['cost']:.4f}"
    if row.get('known_cost') is not None:
        return f"≥ ${row['known_cost']:.4f}（{row['cost_unknown_runs']} 次未知）"
    return '未知'


def _print_runs(stats, failures) -> None:
    if not stats:
        print('还没有运行记录。')
        return
    rows = [(s['agent'], s['runs'], s['ok'], s['failed'], s['cancelled'], _fmt_cost(s),
             '-' if s['avg_ms'] is None else f"{s['avg_ms'] / 1000:.1f}s") for s in stats]
    print(_table(('agent', '次数', '成功', '失败', '取消', '费用', '平均耗时'), rows))
    if failures:
        print()
        print(_table(('失败类型', '失败码', '次数'),
                     [('环境问题' if f['fail_class'] == 'platform' else '交付问题',
                       f['fail_code'], f['n']) for f in failures]))


# ── sessions ──

def _session_files(fleet):
    root = Path(fleet.sessions_dir)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append((path, data))
    return out


def _sessions(fleet, args) -> int:
    from .store import JsonFileStore, SessionState

    if args.agent_name is None:
        rows = []
        for path, data in _session_files(fleet):
            agent, sid = data.get('agent_name', ''), data.get('session_id', '')
            if args.agent and agent != args.agent:
                continue
            turns = data.get('turns') or []
            if not turns:                    # 已清空的会话不再列出
                continue
            updated = datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
            rows.append((agent, sid, len(turns), updated))
        if not rows:
            print('还没有保存的会话。')
        else:
            print(_table(('agent', '会话编号', '轮数', '最后更新'), rows))
        return 0

    if args.session_id is None:
        raise ValueError('查看或清空会话需要同时给出 agent 和会话编号')
    fleet.entry(args.agent_name)            # agent 不存在时提前报错
    store = JsonFileStore(fleet.sessions_dir).for_agent(args.agent_name)
    if args.reset:
        store.save(SessionState(session_id=args.session_id, agent_name=args.agent_name))
        print(f'已清空会话：{args.agent_name} / {args.session_id}')
        return 0
    state = store.load(args.session_id)
    if state is None or not state.turns:
        print(f'没有找到会话内容：{args.agent_name} / {args.session_id}')
        return 0
    for i, turn in enumerate(state.turns, 1):
        flag = '' if turn.ok else '（失败）'
        print(f'── 第 {i} 轮{flag}')
        print(f'问：{turn.prompt.strip()}')
        print(f'答：{turn.text.strip()}')
        print()
    return 0


# ── doctor ──

_READONLY_LABEL = {'os-sandbox': '系统沙箱', 'tool-allowlist': '工具白名单',
                   'deny-rules': '拒绝规则', 'plan-mode': '计划模式，较弱', 'none': '无限制'}


def _binary_of(agent) -> str:
    for key, value in vars(agent).items():
        if key.endswith('_bin') and isinstance(value, str):
            return value
    return ''


def _doctor(directory) -> int:
    from my_agents2 import get_agent, list_agents

    print('本机的 agent 工具')
    tool_rows, available = [], {}
    for name in list_agents():
        try:
            binary = _binary_of(get_agent(name))
        except Exception as exc:              # noqa: BLE001 预设本身有问题也要报出来
            tool_rows.append((name, '-', f'预设无效：{exc}'))
            available[name] = False
            continue
        path = shutil.which(binary) if binary else None
        available[name] = bool(path)
        tool_rows.append((name, binary or '-', path or '未找到'))
    print(_table(('名称', '命令', '位置'), tool_rows))

    if directory is None:
        if not any(available.values()):
            print('\n没有找到任何可用的 agent 工具。')
            return 1
        return 0

    fleet = load_fleet(directory)
    print(f'\n团队：{fleet.fleet_dir}')
    rows, problems = [], 0
    for name in fleet.names():
        entry = fleet.entry(name)
        backend = entry.get('backend') or 'claude'
        status = entry.get('status', 'active')
        if status != 'active':
            rows.append((name, backend, '跳过', f'状态为 {status}'))
            continue
        try:
            agent = fleet.get(name, raw=True)
        except ValueError as exc:
            problems += 1
            rows.append((name, backend, '✗', f'配置不可用：{exc}'))
            continue
        binary = _binary_of(agent)
        if binary and not shutil.which(binary):
            problems += 1
            rows.append((name, backend, '✗', f'找不到命令 {binary}'))
            continue
        note = [f'只读（{_READONLY_LABEL.get(agent.caps.readonly, agent.caps.readonly)}）' if agent.mode == 'readonly' else '可写']
        if agent.ignored:
            note.append('该工具会忽略：' + '、'.join(agent.ignored))
        rows.append((name, backend, '✓', '；'.join(note)))
    print(_table(('agent', '工具', '可用', '说明'), rows))
    if problems:
        print(f'\n有 {problems} 个 agent 当前无法使用。')
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='my-agents-fleet', description='本地 agent 的统一管理工具。')
    parser.add_argument('--version', action='version', version=__version__)
    subs = parser.add_subparsers(dest='command', required=True)

    init = subs.add_parser('init', help='在新目录或空目录里创建示例团队')
    init.add_argument('directory')

    inspect = subs.add_parser('inspect', help='列出团队里的 agent（不调用模型）')
    inspect.add_argument('directory')

    doctor = subs.add_parser('doctor', help='检查本机的 agent 工具和团队里每个 agent 是否可用（不调用模型）')
    doctor.add_argument('directory', nargs='?')

    run = subs.add_parser('run', help='交给某个 agent 一个任务（会调用模型）')
    run.add_argument('directory')
    run.add_argument('agent')
    run.add_argument('prompt', nargs='?', help='任务内容；写 - 表示从标准输入读取')
    run.add_argument('--file', help='从文件读取任务内容')
    run.add_argument('--session', help='会话编号：保存这段对话，之后可以接着聊')

    runs = subs.add_parser('runs', help='按 agent 汇总工作记录')
    runs.add_argument('directory')
    runs.add_argument('--agent')
    runs.add_argument('--since', help='只统计这个时间之后的记录，如 2026-10-01')
    runs.add_argument('--json', action='store_true', help='输出原始 JSON')

    sessions = subs.add_parser('sessions', help='列出、查看或清空会话')
    sessions.add_argument('directory')
    sessions.add_argument('agent_name', nargs='?', help='要查看的 agent')
    sessions.add_argument('session_id', nargs='?', help='要查看的会话编号')
    sessions.add_argument('--agent', help='列表时只显示这个 agent 的会话')
    sessions.add_argument('--reset', action='store_true', help='清空指定会话的记忆')

    models = subs.add_parser('models', help='列出 agent 工具可用的模型和推理强度')
    models.add_argument('backend', nargs='?', default='')
    models.add_argument('--cached', action='store_true', help='只读缓存，不运行工具采集')

    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            _init(args.directory)
        elif args.command == 'models':
            from my_agents2.__main__ import print_models
            print_models(args.backend, cached=args.cached)
        elif args.command == 'doctor':
            return _doctor(args.directory)
        else:
            fleet = load_fleet(args.directory)
            if args.command == 'inspect':
                print(fleet.describe())
                print(f'State directory: {fleet.state_dir}')
            elif args.command == 'runs':
                filters = {k: getattr(args, k) for k in ('agent', 'since') if getattr(args, k)}
                stats, failures = fleet.stats(**filters), fleet.fail_breakdown(**filters)
                if args.json:
                    print(json.dumps({'stats': stats, 'failures': failures},
                                     ensure_ascii=False, indent=2))
                else:
                    _print_runs(stats, failures)
            elif args.command == 'sessions':
                return _sessions(fleet, args)
            else:
                prompt = _read_prompt(args, parser)
                if not prompt.strip():
                    raise ValueError('任务内容为空')
                agent = (fleet.session(args.agent, args.session) if args.session else fleet.get(args.agent))
                out = agent.ask(prompt, trigger='cli')
                print(json.dumps(out.structured, ensure_ascii=False, indent=2) if out.structured is not None else out.text)
                if not out.ok:
                    parser.exit(1, f'{out.failure}\n')
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f'{exc}\n')
