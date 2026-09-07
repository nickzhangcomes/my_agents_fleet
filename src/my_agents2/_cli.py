"""跑本地 CLI 子进程的公共骨架 —— claude / codex / grok / agy / pi 五个后端共用。

1.0 里这段逻辑抄了三份，结果三份的收尾判断不一致（两份带 `and err_lines`，
一份不带），超时被杀时静默返回空成功。这里收成一处，只写对一次。

统一处理：找不到命令 / 硬超时（可区分）/ 调用方中止杀进程 / 非零退出。
"""
from __future__ import annotations

import json
import subprocess
import threading
from typing import Iterator, Optional, Sequence, Union

from .base import Event, RunHandle


def stream_ndjson(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict,
    timeout: int,
    bin_name: str,
    log_unparsed: bool = False,
    handle: Optional[RunHandle] = None,
) -> Iterator[Union[dict, Event]]:
    """跑一个吐 NDJSON 的子进程，逐行 yield 解析好的 dict。

    出错时 yield 的是 Event（调用方 `isinstance(item, Event)` 分流后直接透出）。

    和 1.0 的差别，全在收尾：
      - 非零退出 **无条件**报 error（不再要求 stderr 非空）
      - 超时打 `raw["timed_out"]=True`，让上层能把「超时」和「跑挂了」分开
      - 调用方 break 中止时不再往正在关闭的生成器里塞事件（1.0 会 RuntimeError）
    """
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except FileNotFoundError:
        yield Event(kind="error", text=f"找不到 {bin_name} 命令，确认它在 PATH 里。")
        return
    except OSError as e:
        # 最常见的是 E2BIG：prompt 太大撑爆 argv（本机上限约 1MB，含环境变量）。
        # 别让它作为未捕获异常炸到调用方 —— 本库所有失败路径都返回 ok=False，这条也不例外。
        hint = ""
        if getattr(e, "errno", None) == 7:      # E2BIG
            hint = ("\nprompt 超过了系统的 argv 上限（约 1MB）。把大段内容写成文件、"
                    "prompt 里只给路径 —— 五个 CLI 后端都能读绝对路径。")
        yield Event(kind="error", text=f"起不了 {bin_name} 进程：{e}{hint}",
                    raw={"errno": getattr(e, "errno", None)})
        return

    # 登记「怎么杀我」，让别的线程能中止（chat box 的停止按钮）
    if handle is not None:
        handle._attach(proc.kill)

    state = {"timed_out": False}

    def on_timeout() -> None:
        state["timed_out"] = True
        proc.kill()

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()

    err_lines: list = []

    def drain_err() -> None:
        try:
            for ln in proc.stderr:
                if ln.strip():
                    err_lines.append(ln.rstrip())
        except Exception:  # noqa: BLE001 —— stderr 读挂了不该影响主流
            pass

    et = threading.Thread(target=drain_err, daemon=True)
    et.start()

    read_complete = False
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if log_unparsed:
                    yield Event(kind="log", text=line[:500])
        read_complete = True
    finally:
        # close / 键盘中断 / 读取异常都要回收进程；正常读完则仍由超时器兜住 wait。
        if not read_complete and proc.poll() is None:
            proc.kill()
        try:
            code = proc.wait()
        finally:
            timer.cancel()
            et.join(timeout=1)
            proc.stdout.close()
            proc.stderr.close()
    if handle is not None and handle.stopped:
        # 调用方主动中止：进程当然是非零退出，别报成「跑挂了」误导人
        yield Event(kind="error", text=f"{bin_name} 被调用方中止（RunHandle.stop）。",
                    raw={"stopped": True, "exit_code": code})
    elif state["timed_out"]:
        yield Event(kind="error",
                    text=f"{bin_name} 超时被杀（timeout={timeout}s），本轮结果不完整。",
                    raw={"timed_out": True, "exit_code": code})
    elif code != 0:
        detail = "\n".join(err_lines[-8:]) or f"{bin_name} 非零退出（exit={code}），stderr 为空。"
        yield Event(kind="error", text=detail, raw={"exit_code": code})
