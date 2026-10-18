# 更新记录

## 2.0.1a2

- 新增 `doctor` 命令：检查本机装了哪些 agent 工具，以及团队里每个 agent 的配置能否使用，不调用模型。
- 新增 `sessions` 命令：列出会话、查看某段会话的完整内容、用 `--reset` 清空会话记忆。
- `run` 支持用 `--file` 从文件读取任务，或用 `-` 从标准输入读取。
- `runs` 默认输出对齐的表格，费用未知时明确标出；原来的 JSON 输出改为 `--json`。
- 重试提示改为输出到标准错误，不再混进 `run` 的结果里，便于用管道处理输出。
- 重写 README，只保留中文版。
- 删除面向旧项目的迁移说明和发布前的内部记录，清理代码注释里的历史版本对比。
- 新增测试，保证 `examples/basic_fleet` 与内置模板保持一致。

## 2.0.1a1

- Adopt `my_agents_fleet` as the project and Python package name, with `my-agents-fleet` as the distribution and console command.
- Package my_agents_fleet and its my_agents2 dependency in one installable distribution.
- Preserve role-isolated sessions, atomic roster updates, recorded execution, cancellation, workspace locks, and cost completeness.
- Include use-triggered model discovery with per-backend freshness and failure reporting.
- Move generated model caches out of the installed package into a user cache directory.
- Add neutral starter templates, a no-account offline demo, console entry points, documentation, offline tests, and CI configuration.
