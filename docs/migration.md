# Standalone distribution / 独立发行版迁移

项目名和 Python 包名统一为 `my_agents_fleet`，安装包名和终端命令为 `my-agents-fleet`。旧项目的导入语句需改为 `from my_agents_fleet import load_fleet`；底层仍可 `from my_agents2 import get_agent`。两层一起安装，无需手动修改 sys.path，也不依赖外部私有目录。

独立副本的变化：

1. 使用 src 目录布局和可安装的 `my-agents-fleet` 发行包，版本为 `2.0.1a1`。
2. 模型表移到使用者缓存目录，不再写安装包目录。已有模型快照不随源码分发，可在本机重新采集。
3. CLI 预设使用包内通用 config.json；自定义预设通过 `MY_AGENTS2_CONFIG` 指向个人文件。
4. 命令行采用子命令：`my-agents-fleet init/inspect/run/runs/models`。更新导入名后，原有 Python 方法和参数保持一致。
5. 通用示例不依赖个人技能、专属模型名或已存在的会话文件。

已有 fleet 配置可复制到自己的业务目录再加载。先备份需要保留的历史；会话文件只在角色名和完整 session_id 匹配时读取旧格式，下次保存采用新键。缺少环境指纹会带历史重新开启后端会话。不要把业务历史提交到公开仓库。

`run()` 的返回值为 Outcome，底层 TaskResult 在 `out.result`；字段如 status/artifacts/log_path 仍可直接读取。`cost_usd` 和统计 cost 可能为 None，表示没有完整费用信息。

本地发行副本与原来的业务目录是独立文件，后续修复需要明确同步方向；本次整理没有建立自动双向同步。
