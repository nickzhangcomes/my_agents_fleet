# Architecture / 架构

`Application → Fleet role → RecordedAgent → my_agents2 adapter → local CLI process → events/result`

两层一起打包，但职责分开：

| 层 | 负责什么 | 主要代码 |
|---|---|---|
| 配置与身份 | 全局约束、角色说明、参数覆盖 | `my_agents_fleet/fleet.py`, `identity.py` |
| 持久会话 | 按角色隔离、整轮加锁、续接与恢复 | `session.py`, `store.py` |
| 执行治理 | 运行记录、失败分类、重试、取消 | `dispatch.py`, `failures.py`, `runs.py` |
| 并发协调 | 进程内角色名额、跨进程目录锁 | `capacity.py`, `_files.py` |
| 契约 | 输入可信度、资料读写、输出要求 | `contract.py` |
| CLI 适配 | 把统一参数和事件翻译成 CLI 的格式 | `my_agents2/*_cli.py`, `claude_code.py`, `grok_code.py`, `antigravity.py` |
| 模型发现 | 采集模型与思考档位、缓存时效 | `models.py`, `_model_registry.py` |

状态分三处：`fleet.json + Markdown` 保存角色配置；fleet 的 `state/` 保存会话和 SQLite 运行记录；使用者缓存目录保存可重新采集的模型表。

`Session.ask` 的顺序：`锁定角色+会话ID → 读取历史 → 核对环境指纹 → 执行并记 Run → 保存历史 → 释放锁`。环境指纹涵盖角色、后端、目录、权限、模型和身份提示；变更后不复用旧后端句柄，历史仍可重放。

并发限制在共享状态目录的 Fleet 实例间复用，作用域为当前进程；工作目录锁可协调共享同一锁目录的多个进程。不同 fleet 默认不共享目录锁。

`Outcome` 包含最后结果、最后一次 Run、全部尝试和失败分类。文件任务字段可直接读取，也可从 `out.result` 获取底层 `TaskResult`。持久会话恢复失败句柄时，前后两次尝试的费用都计入结果。
