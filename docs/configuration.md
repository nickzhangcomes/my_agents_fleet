# Configuration / 配置

用 `my-agents-fleet init ./_local/demo` 创建可编辑的配置。完整通用样例在 [examples/basic_fleet](../examples/basic_fleet)。

```json
{
  "version": 2,
  "global": {
    "shared": {"instructions_file": "global.md"},
    "defaults": {"backend": "claude", "mode": "readonly", "workspace": ".", "max_attempts": 1},
    "state_dir": "state"
  },
  "agents": {
    "reviewer": {"intro": "Review project notes", "instructions_file": "agents/reviewer.md"}
  }
}
```

优先顺序：`全局 defaults → 单个角色 → 本轮 get(..., **overrides)`。`extra` 也按此顺序合并。

| 字段 | 含义 |
|---|---|
| `backend` | 预设名：claude / codex / grok / agy / pi |
| `model`, `effort` | 模型及思考投入档位；省略时由 CLI/适配器处理默认值 |
| `mode` | full 或 readonly。full 对部分后端会关闭审批/沙箱，请按业务选择 |
| `workspace`, `add_dirs` | 工作目录与额外目录；workspace 相对 load_fleet 的 base（默认 fleet 目录）解析 |
| `output_schema` | JSON 输出结构。后端不支持时拒绝构造，执行后缺少结构化结果也会失败 |
| `trust` | trusted 或 untrusted；后者要求同时禁用 shell/write/network/spawn，目前只有 Claude 适配器满足 |
| `assets` | 资料路径与 r/w 意图，相对 fleet 目录解析；详见能力边界 |
| `max_attempts` | 普通 ask 的总尝试上限，包含首轮 |
| `timeout` | 单次 CLI 运行的秒数 |
| `max_concurrent` | 每个角色在当前进程的并发数；0 为不设上限 |
| `lock_workspace` | true 锁工作目录，或指定需要独占的目录 |
| `workspace_timeout` | 最多等待目录锁多少秒，0 为立即失败 |
| `skills` | 提示词中的技能声明，需要 CLI 自己安装/提供这些技能 |
| `extra` | 后端专属参数；统一参数不能放在这里覆盖，受约束调用禁止改写权限或执行入口 |

`instructions_file` 相对 fleet 目录。`assets` 和字符串形式的 `lock_workspace` 也相对 fleet 目录。共享 `add_dirs` 和 MCP 文件建议使用绝对路径；当前未统一它们的相对路径解析。

可通过 `upsert_agent(name, entry)` 和 `remove_agent(name)` 改名册。写前验证整份配置，使用跨进程锁和原子替换。删除角色只改名册，不删除业务文件和历史。

## 存储与环境变量

| 位置/变量 | 用途 |
|---|---|
| `<fleet>/state/fleet.db` | SQLite 执行记录 |
| `<fleet>/state/sessions/` | 按角色和会话 ID 隔离的完整历史 |
| `$XDG_CACHE_HOME/my-agents-fleet/models.json` | 默认模型表；XDG_CACHE_HOME 未设置时使用 `~/.cache` |
| `MY_AGENTS2_MODELS_PATH` | 覆盖模型表路径；进程启动前设置 |
| `MY_AGENTS2_CONFIG` | 自己的 CLI 预设 JSON，避免编辑安装目录；未设置时使用包内通用预设 |
| `MY_AGENTS2_AUTO_REFRESH` | 默认 1；0 关闭自动模型采集 |
| `MY_AGENTS2_MODELS_TTL_SECONDS` | 常规复查间隔，默认 21600 秒 |
| `MY_AGENTS2_MODELS_RETRY_SECONDS` | 失败重试/未知模型复查间隔，默认 300 秒 |
| `MY_AGENTS2_MODELS_TIMEOUT_SECONDS` | 单个元数据命令超时，默认 30 秒 |

模型缓存不包含凭据正文，但可能包含本机 CLI 路径和账号可见模型，因此不应提交它。CLI 登录与 provider 配置继续由各 CLI 管理。本项目不会替你安装 CLI、切换账号或写入密钥。
