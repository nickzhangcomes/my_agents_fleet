# Model discovery / 模型与 effort 自动发现

`使用指定模型或查询列表 → 检查来源与有效期 → 需要时采集 → 合并写入个人缓存 → 读取新表`。

默认使用时每 6 小时复查；CLI 安装文件、软链接、npm 包版本文件、Codex/Grok 缓存或 Pi 配置变化，会在下次使用时提前刷新。未收录模型最多每 5 分钟复查一次。没有后台计时器，无人使用时不会主动执行命令。

Codex/Grok 读取它们自己维护的模型缓存；agy 执行 `agy models`；Pi 执行 `pi --list-models`；Claude 从已安装程序的模型表中提取信息。元数据采集不发送任务提示，但命令本身可能查询厂商服务。

Pi 的 thinking=yes 不公开每个模型的精确档位，所以仍为未知。Claude 程序内部格式变化可能使提取失败。缓存与本机可见列表不保证当前账号一定能调用，也不保证与厂商发布时间同步。

失败保留旧表，记录原因，默认 5 分钟后允许重试；单个元数据命令默认最多等 30 秒。保存成功采集时间、最近尝试时间和上游缓存时间，避免把读取旧缓存说成更新成功。

常驻进程按文件变化重读；更新后的档位用于新建 Agent，已经创建的 Agent 保持参数。升级库代码后，业务进程需重启一次，之后模型表变化无需重启。

```python
from my_agents2 import list_models, models_status, refresh_models_registry

rows = list_models('codex')
print(models_status('codex'))  # 只读状态
print(list_models('pi', refresh=False))
refresh_models_registry(['pi'])
```

`ModelInfo.efforts`：None 是未知，空 tuple 是明确不支持，有值则为采集到的档位。`supports_effort()` 同样返回 None / False / True。`stale`、`collected_at`、`refresh_error` 说明数据时效。适配器不认识的新档位不会被编造成现有档位。

```bash
my-agents2 --models --cached
my-agents2 --models-status
my-agents2 --refresh-models codex grok
```

强制刷新有任一指定后端失败时退出码为 1，其余成功结果照常保存。损坏缓存会先保留为 `.corrupt-*` 文件再重建。普通错误只记录类别或退出码，不保存 CLI 的 stderr 原文。
