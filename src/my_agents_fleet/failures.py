"""失败分类 —— 一次执行挂了，是**环境**的问题还是 **agent** 的问题。

照 Multica 的分法（它的失败码分平台侧 `runtime_offline`/`timeout` 和工具侧 `agent_error.*`）：

    平台侧的错  → 自动重试。环境问题，重试大概率能好。
    agent 侧的错 → 不自动重试。是「它理解错了/做错了」，原样重跑大概率还错，该停下来给人看。

**重试不是通用策略，是按失败性质决定的。** 无差别重试的代价很实：
一次重试 = 重新烧一遍模型钱 + 重新等一遍时长，而且可能把已经产出的中间物（图、文件）白扔。

⚠️ 三个「退出码 0 但其实失败了」的坑（后端把错误当正常 stdout 打出来，
   `Result.ok` 是 True）—— 不在这里显式认出来，就会被当成正常产出送进下游：
     · `Not logged in · Please run /login`
     · `You've hit your session limit · resets 3:20am`
     · 要了 output_schema 却没拿到 structured（模型没按 schema 走）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ── 两个类别（Multica 同款分法）──
PLATFORM = "platform"    # 环境/后端的问题
AGENT = "agent"          # agent 自己产出的问题


@dataclass(frozen=True)
class FailKind:
    """一种失败的完整说明：归哪类、要不要自动重试、人话是什么。

    `retry` 单独一个字段而不是「platform 就重试」—— 因为有例外：
    prompt 超 argv 上限属于平台侧，但重试一万次也一样超。
    """
    code: str
    cls: str
    retry: bool
    hint: str


KINDS = {k.code: k for k in (
    # ── 平台侧 ──
    FailKind('cancelled', PLATFORM, False, '调用方主动停止，本轮结果可能不完整'),
    FailKind('config_error', PLATFORM, False, '任务或角色配置无效，请修正配置后再运行'),
    FailKind("timeout", PLATFORM, True,
             "被超时杀掉。要么加大 timeout，要么这活本身就太重（比如会话内画图）"),
    FailKind("session_limit", PLATFORM, True,
             "撞到订阅额度上限，要等窗口重置。重试要退避到重置之后，不然只是白撞"),
    FailKind("not_logged_in", PLATFORM, False,
             "后端 CLI 没登录。重试没用，去终端跑一次登录"),
    FailKind("stale_session", PLATFORM, False,
             "续接句柄失效（会话过期/换机器）。恢复办法是**清掉句柄冷启**，不是原样重试 —— "
             "拿着同一个死句柄再跑一次不可能成功。Session.ask 会自动这么做；"
             "自己管句柄的调用方，收到这个码就把存的句柄丢掉、下一轮冷启"),
    FailKind("prompt_too_large", PLATFORM, False,
             "prompt 撞到 ~1MB 的 argv 上限。改成把大内容写文件、prompt 里给路径"),
    FailKind("backend_crashed", PLATFORM, True,
             "后端进程非正常退出。多半是瞬时故障"),
    FailKind("api_error", PLATFORM, False,
             "模型 API 直接报错（如 schema 非法、请求超限）。看错误码本身，重试通常无用"),
    FailKind("workspace_busy", PLATFORM, False,
             "工作目录被另一个 run 占着，等锁超时。加大 workspace_timeout，"
             "或者把这个 agent 的调度错开（同一个目录本来就只该一个人写）"),

    # ── agent 侧：不自动重试 ──
    FailKind('agent_error.incomplete', AGENT, False, '任务未完整交付，需要验收或补充指令'),
    FailKind("agent_error.bad_shape", AGENT, False,
             "要了 output_schema 但没拿到合规产出。八成是 schema 描述得不清楚，改 schema 或改指令"),
    FailKind("agent_error.empty", AGENT, False,
             "跑完了但产出是空的。多半是指令没说清，或者它判断「无需动作」"),
    FailKind("agent_error.reported", AGENT, False,
             "agent 自己报了错。先看它说了什么，别盲目重跑"),
)}

# 后端把这两句当正常 stdout 打出来（退出码 0），必须按原文认
_NOT_LOGGED_IN = re.compile(r"^\s*not logged in", re.I)
_SESSION_LIMIT = re.compile(
    r"^\s*(?:(?:you(?:'ve| have)\s+)?hit your session limit\b|usage limit reached\b"
    r"|rate limit(?:ed| exceeded| reached)\b|(?:error[: ]+)?(?:http\s+)?429\b)", re.I)
# 'resume' 要和 not found/expired 共现才算续接失败，避免裸 'resume' 误伤
# （例如 'failed to resume download'）把健康会话误判成失效
_STALE = re.compile(
    r"(no conversation|session[^\n]*(not found|expired|invalid)"
    r"|resume[^\n]*(not found|failed|invalid|expired)|no such session)", re.I)
_TOO_LARGE = re.compile(r"(argument list too long|prompt too large|e2big)", re.I)
# 「API Error: 400 …」这类：后端把它当正文吐出来，claude 甚至可能同时给 subtype=success
_API_ERROR = re.compile(r"^\s*API Error:\s*\d{3}", re.I)


@dataclass
class Failure:
    """一次失败的判定结果。`kind` 带着分类和重试决策，`detail` 是给人看的原文片段。"""
    kind: FailKind
    detail: str = ""

    @property
    def code(self) -> str:
        return self.kind.code

    @property
    def cls(self) -> str:
        return self.kind.cls

    @property
    def retry(self) -> bool:
        return self.kind.retry

    def __str__(self) -> str:
        return f"{self.code}({self.cls}) {self.detail[:120]}"


def classify(result, *, expect_schema: bool = False) -> Optional[Failure]:
    """判定一次 my_agents2 Result 是否失败、属于哪一类。成功返回 None。

    顺序有讲究：**先认那几个「假成功」**（ok=True 但正文其实是错误信息），
    再看 ok/timed_out，最后才看产出本身合不合契约。反过来的话，
    一句「Not logged in」会被当成正常产出直接送进下游。
    """
    text = (getattr(result, "text", "") or "").strip()
    err = (getattr(result, "error", "") or "").strip()
    blob = f"{err}\n{text}"

    if any((getattr(ev, 'raw', None) or {}).get('stopped')
           for ev in (getattr(result, 'events', None) or [])):
        return Failure(KINDS['cancelled'], err[:200])

    # ① 假成功：退出码 0，但正文是错误信息
    if _NOT_LOGGED_IN.search(text):
        return Failure(KINDS["not_logged_in"], text[:200])
    if _SESSION_LIMIT.search(err) or _SESSION_LIMIT.search(text):
        return Failure(KINDS["session_limit"], blob.strip()[:200])
    if _API_ERROR.search(text) or _API_ERROR.search(err):
        return Failure(KINDS["api_error"], (text or err).strip()[:200])

    # ② 真失败
    if getattr(result, "timed_out", False):
        return Failure(KINDS["timeout"], err[:200])
    if not getattr(result, "ok", True):
        if _STALE.search(blob):
            return Failure(KINDS["stale_session"], blob.strip()[:200])
        if _TOO_LARGE.search(blob):
            return Failure(KINDS["prompt_too_large"], blob.strip()[:200])
        if err:
            return Failure(KINDS["backend_crashed"], err[:200])
        return Failure(KINDS["agent_error.reported"], text[:200])

    # ③ 跑成功了，但产出不符合契约
    if getattr(result, 'status', None) == 'partial':
        return Failure(KINDS['agent_error.incomplete'], text[:200])
    if expect_schema and getattr(result, "structured", None) is None:
        return Failure(KINDS["agent_error.bad_shape"], text[:200])
    if not text and getattr(result, "structured", None) is None:
        return Failure(KINDS["agent_error.empty"], "")

    return None


def retry_delay(failure: Failure, attempt: int) -> float:
    """下一次重试前等多久（秒）。attempt 从 1 起算。

    额度上限单独给一档长退避 —— 它不是「等一下就好」，是「等窗口重置」，
    按秒级退避去撞只是把额度墙撞出更多失败记录。
    """
    if failure.code == "session_limit":
        return 300.0 * attempt
    return min(2.0 ** attempt, 30.0)
