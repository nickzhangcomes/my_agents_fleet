"""契约：trust / assets 的翻译，以及**拦不住时必须报错**。

`test_refuses_*` 是这个文件里最要紧的几条：安全边界静默降级意味着
你以为设了防线、实际没有，而且没人会发现。宁可派不出去，不要假装拦住了。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from ._fake import AI_ROOT, FakeCaps

sys.path.insert(0, str(AI_ROOT))

from my_agents_fleet import ContractError, TRUST_UNTRUSTED                        # noqa: E402
from my_agents_fleet import contract as C                                         # noqa: E402

ALL_DENY = ("shell", "write", "network", "spawn")


class TestTrust(unittest.TestCase):

    def test_untrusted_forces_readonly_and_denies_all_four(self):
        rc = C.resolve(trust=TRUST_UNTRUSTED, assets=[], backend="claude",
                       caps=FakeCaps(deny=ALL_DENY), requested_mode="full")
        self.assertEqual(rc.mode, "readonly")
        self.assertEqual(rc.deny, ALL_DENY)
        self.assertTrue(rc.notes, "要给 agent 一句如实说明")

    def test_refuses_backend_that_cannot_deny(self):
        """只读不等于安全：readonly 拦住了写，联网工具还在，
           一句注入的『把上下文 fetch 到我这个 URL』照样能把资料发出去。"""
        with self.assertRaises(ContractError) as cm:
            C.resolve(trust=TRUST_UNTRUSTED, assets=[], backend="agy",
                      caps=FakeCaps(deny=()), requested_mode=None)
        self.assertIn("agy", str(cm.exception))
        self.assertIn("shell", str(cm.exception))

    def test_refuses_backend_that_denies_only_some(self):
        with self.assertRaises(ContractError) as cm:
            C.resolve(trust=TRUST_UNTRUSTED, assets=[], backend="grok",
                      caps=FakeCaps(deny=("shell", "write")), requested_mode=None)
        self.assertIn("network", str(cm.exception))

    def test_refuses_backend_without_permission_concept(self):
        with self.assertRaises(ContractError):
            C.resolve(trust=TRUST_UNTRUSTED, assets=[], backend="fake-no-permissions",
                      caps=FakeCaps(deny=ALL_DENY, readonly="none"), requested_mode=None)

    def test_trusted_is_the_default_and_changes_nothing(self):
        rc = C.resolve(trust="trusted", assets=[], backend="claude",
                       caps=FakeCaps(deny=ALL_DENY), requested_mode="full")
        self.assertIsNone(rc.mode)
        self.assertEqual(rc.deny, ())

    def test_bad_trust_value_raises(self):
        with self.assertRaises(ContractError):
            C.resolve(trust="untrused", assets=[], backend="claude",
                      caps=FakeCaps(deny=ALL_DENY), requested_mode=None)


class TestAssets(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp()
        for d in ("资料", "草稿"):
            os.makedirs(os.path.join(self.base, d), exist_ok=True)

    def test_relative_path_resolves_against_fleet_dir(self):
        """回归：相对路径不解析的话，`--add-dir 资料` 会落到**进程当前目录**，
           而 cron 和常驻服务的 cwd 根本不一样，等于每个调用方挂到不同地方。"""
        a = C.parse_assets({"资料": "./资料"}, base=self.base)[0]
        self.assertTrue(os.path.isabs(a.path))
        self.assertEqual(Path(a.path), Path(self.base, "资料").resolve())

    def test_absolute_path_ignores_base_and_is_canonicalized(self):
        """绝对路径不受 base 影响；但仍会解软链（macOS 的 /var → /private/var），
           和工作区锁那边的 realpath 保持一致 —— 否则同一个目录会被当成两个。"""
        p = os.path.join(self.base, "资料")
        got = C.parse_assets({"x": p}, base="/nowhere")[0].path
        self.assertEqual(Path(got), Path(p).resolve())

    def test_all_readonly_is_forced_to_readonly_mode(self):
        """后端没有 per-目录只读，唯一的真只读是全局 readonly —— 那就压过去。"""
        rc = C.resolve(trust="trusted", backend="claude", caps=FakeCaps(deny=ALL_DENY),
                       assets=C.parse_assets({"资料": "./资料"}, base=self.base),
                       requested_mode="full")
        self.assertEqual(rc.mode, "readonly")
        self.assertEqual(rc.assets_enforced, C.ENFORCE_HARD)

    def test_mixed_read_write_is_soft_not_a_lie(self):
        """有读有写 → 后端区分不了。承认它是『约定』，不假装是边界。"""
        rc = C.resolve(trust="trusted", backend="claude", caps=FakeCaps(deny=ALL_DENY),
                       assets=C.parse_assets(
                           {"资料": "./资料", "草稿": {"path": "./草稿", "mode": "w"}},
                           base=self.base),
                       requested_mode="full")
        self.assertEqual(rc.assets_enforced, C.ENFORCE_SOFT)
        self.assertIsNone(rc.mode, "有可写资产就不能硬压成只读")
        self.assertIn("约定只读", "\n".join(rc.notes))

    def test_soft_note_does_not_claim_enforcement(self):
        rc = C.resolve(trust="trusted", backend="claude", caps=FakeCaps(deny=ALL_DENY),
                       assets=C.parse_assets(
                           {"资料": "./资料", "草稿": {"path": "./草稿", "mode": "w"}},
                           base=self.base),
                       requested_mode="full")
        self.assertNotIn("机制上只读", "\n".join(rc.notes))

    def test_bad_mode_raises(self):
        with self.assertRaises(ContractError):
            C.parse_assets({"x": {"path": "/tmp", "mode": "rw"}})

    def test_missing_path_raises(self):
        with self.assertRaises(ContractError):
            C.parse_assets({"x": {"mode": "r"}})


if __name__ == "__main__":
    unittest.main()
