"""失败分类 + 重试策略。

这一层的价值全在「分得对不对」：分错了，要么该重试的没重试（白丢一次产出），
要么不该重试的重试了（白烧一遍钱还是同一个错）。
"""
from __future__ import annotations

import sys
import unittest

from ._fake import AI_ROOT

sys.path.insert(0, str(AI_ROOT))

from my_agents2 import Result                                    # noqa: E402
from my_agents_fleet import AGENT, PLATFORM, classify                  # noqa: E402


class TestFakeSuccess(unittest.TestCase):
    """退出码 0 但其实失败了 —— 后端把错误当正常 stdout 打出来。

    这三条是最阴的：`Result.ok` 是 True，不显式认就会被当成正常产出送进下游
    （真实事故：一句「Not logged in」被当成 agent 的回复插进了评论表）。
    """

    def test_not_logged_in(self):
        f = classify(Result(text="Not logged in · Please run /login", ok=True))
        self.assertEqual(f.code, "not_logged_in")
        self.assertFalse(f.retry, "没登录重试一万次也没用")

    def test_session_limit(self):
        f = classify(Result(text="You've hit your session limit · resets 3:20am", ok=True))
        self.assertEqual(f.code, "session_limit")
        self.assertTrue(f.retry)

    def test_session_limit_in_error_field(self):
        self.assertEqual(
            classify(Result(text="", ok=False, error="usage limit reached")).code,
            "session_limit")

    def test_api_error_in_body(self):
        """实测：claude 会给出 `is_error=true` 但 `subtype="success"`，正文是
           「API Error: 400 …」。两处都不认的话，这句话会被当成 agent 的产出。"""
        f = classify(Result(text="API Error: 400 input_schema does not support oneOf", ok=True))
        self.assertEqual(f.code, "api_error")
        self.assertFalse(f.retry, "schema 非法这种错，重试一万次也一样")

    def test_schema_requested_but_not_delivered(self):
        f = classify(Result(text="我觉得应该是这样的", ok=True), expect_schema=True)
        self.assertEqual(f.code, "agent_error.bad_shape")
        self.assertEqual(f.cls, AGENT)
        self.assertFalse(f.retry, "形状不合是 agent 侧的错，原样重跑还是错")


class TestRealFailures(unittest.TestCase):

    def test_timeout(self):
        f = classify(Result(text="", ok=False, timed_out=True, error="killed"))
        self.assertEqual((f.code, f.cls, f.retry), ("timeout", PLATFORM, True))

    def test_stale_session(self):
        for msg in ("session not found: abc", "no conversation with id x",
                    "failed to resume: expired"):
            with self.subTest(msg=msg):
                self.assertEqual(classify(Result(ok=False, error=msg)).code, "stale_session")

    def test_stale_session_must_not_auto_retry(self):
        """回归：曾经把它标成可重试，结果 RecordedAgent 拿着**同一个死句柄**又跑一遍 ——
           不可能成功，还挡住了会话层「清句柄冷启」这个真正的恢复路径。"""
        f = classify(Result(ok=False, error="session not found: abc"))
        self.assertFalse(f.retry)

    def test_resume_word_alone_is_not_stale(self):
        """裸 'resume' 不算续接失败 —— 否则 'failed to resume download'
           会把一个健康会话误判成失效，然后白白丢掉上下文冷启。"""
        self.assertEqual(classify(Result(ok=False, error="failed to resume download")).code,
                         "backend_crashed")

    def test_prompt_too_large(self):
        f = classify(Result(ok=False, error="Argument list too long"))
        self.assertEqual(f.code, "prompt_too_large")
        self.assertFalse(f.retry, "撞 argv 上限，重试还是超")

    def test_empty_output(self):
        self.assertEqual(classify(Result(text="", ok=True)).code, "agent_error.empty")


class TestSuccess(unittest.TestCase):

    def test_plain_ok(self):
        self.assertIsNone(classify(Result(text="正经产出", ok=True)))

    def test_structured_ok(self):
        self.assertIsNone(
            classify(Result(text='{"a":1}', ok=True, structured={"a": 1}), expect_schema=True))

    def test_structured_present_without_text(self):
        """只有结构化产出、正文为空 → 不算 empty。"""
        self.assertIsNone(classify(Result(text="", ok=True, structured={"a": 1})))


class TestRetryDelay(unittest.TestCase):

    def test_session_limit_backs_off_long(self):
        """额度上限不是「等一下就好」，是「等窗口重置」——
           秒级退避去撞只会把额度墙撞出一串失败记录。"""
        from my_agents_fleet.failures import KINDS, Failure, retry_delay
        self.assertGreaterEqual(retry_delay(Failure(KINDS["session_limit"]), 1), 300)
        self.assertLess(retry_delay(Failure(KINDS["timeout"]), 1), 30)


if __name__ == "__main__":
    unittest.main()
