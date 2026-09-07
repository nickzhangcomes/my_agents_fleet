# Contributing

Use Python 3.11+ on macOS or Linux. Install the checkout into a virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -t .
python scripts/check_repository.py
python -m build
```

The test suite disables real model metadata collection. Adapter integration tests should use synthetic events or local test executables. Do not require vendor accounts, spend model quota, or persist credentials in CI.

Keep responsibilities separate: CLI argument/event translation belongs in my_agents2; cross-call roles, sessions, records, contracts, and concurrency belong in my_agents_fleet. Preserve unknown values instead of treating unavailable capabilities or costs as zero/false.

For bug reports include Python/OS/CLI versions, minimal configuration, expected/actual behavior, and a redacted error. For a patch explain the concrete behavior change and relevant checks. Never attach real conversations, authentication files, private filesystem paths, or model cache snapshots.
