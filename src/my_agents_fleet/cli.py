"""Command-line entry points for the standalone distribution."""
from __future__ import annotations

import argparse
import json
import shutil
from importlib import resources
from pathlib import Path

from . import __version__, load_fleet


def _init(directory):
    destination = Path(directory).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f'Destination must be a new or empty directory: {destination}')
    with resources.as_file(resources.files('my_agents_fleet').joinpath('templates/basic')) as source:
        shutil.copytree(source, destination, dirs_exist_ok=True)
    print(f'Created fleet: {destination}')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='my-agents-fleet', description='Manage a local fleet of agent CLIs.')
    parser.add_argument('--version', action='version', version=__version__)
    subs = parser.add_subparsers(dest='command', required=True)
    init = subs.add_parser('init', help='Create a neutral, read-only starter fleet')
    init.add_argument('directory')
    inspect = subs.add_parser('inspect', help='Read fleet configuration without starting an agent')
    inspect.add_argument('directory')
    run = subs.add_parser('run', help='Run a prompt using an installed, authenticated CLI')
    run.add_argument('directory')
    run.add_argument('agent')
    run.add_argument('prompt')
    run.add_argument('--session', help='Persist and resume this conversation ID')
    runs = subs.add_parser('runs', help='Print execution statistics as JSON')
    runs.add_argument('directory')
    runs.add_argument('--agent')
    runs.add_argument('--since')
    models = subs.add_parser('models', help='Inspect available models and effort levels')
    models.add_argument('backend', nargs='?', default='')
    models.add_argument('--cached', action='store_true', help='Do not run metadata collectors')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            _init(args.directory)
        elif args.command == 'models':
            from my_agents2.__main__ import print_models
            print_models(args.backend, cached=args.cached)
        else:
            fleet = load_fleet(args.directory)
            if args.command == 'inspect':
                print(fleet.describe())
                print(f'State directory: {fleet.state_dir}')
            elif args.command == 'runs':
                filters = {k: getattr(args, k) for k in ('agent', 'since') if getattr(args, k)}
                print(json.dumps({'stats': fleet.stats(**filters), 'failures': fleet.fail_breakdown(**filters)},
                                 ensure_ascii=False, indent=2))
            else:
                agent = (fleet.session(args.agent, args.session) if args.session else fleet.get(args.agent))
                out = agent.ask(args.prompt, trigger='cli')
                print(json.dumps(out.structured, ensure_ascii=False, indent=2) if out.structured is not None else out.text)
                if not out.ok:
                    parser.exit(1, f'{out.failure}\n')
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f'{exc}\n')
