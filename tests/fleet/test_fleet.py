"""Fleet 门面：配置加载、身份组装、批量派活、持久会话。

这里用**真的 fleet 目录 + 真的 my_agents2 装配**（只是不 .ask()，不烧额度）——
配置怎么落到后端入参上，是这一层最容易出错的地方，mock 掉就白测了。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ._fake import AI_ROOT, FakeAgent

sys.path.insert(0, str(AI_ROOT))

from my_agents2 import Result                                               # noqa: E402
from my_agents_fleet import ContractError, FleetConfigError, load_fleet           # noqa: E402
from my_agents_fleet import fleet as fleet_mod                                    # noqa: E402


def make_fleet(**agent_over) -> Path:
    d = Path(tempfile.mkdtemp()) / "myfleet"
    (d / "agents").mkdir(parents=True)
    (d / "资料").mkdir()
    (d / "global.md").write_text("全队共通的话。\n", encoding="utf-8")
    (d / "agents" / "甲.md").write_text("甲的工作内容。\n", encoding="utf-8")
    cfg = {
        "version": 2,
        "global": {
            "business_scenario": "测试编队",
            "constraints": ["约束一"],
            "shared": {"instructions_file": "global.md"},
            "defaults": {"backend": "claude", "model": "claude-haiku-4-5",
                         "workspace": ".", "max_attempts": 2},
            "state_dir": "state",
        },
        "agents": {"甲": {"intro": "甲的简介", "instructions_file": "agents/甲.md",
                          **agent_over}},
    }
    (d / "fleet.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


class TestLoading(unittest.TestCase):

    def test_missing_fleet_json(self):
        with self.assertRaises(FleetConfigError):
            load_fleet(tempfile.mkdtemp())

    def test_bad_json_says_so(self):
        d = Path(tempfile.mkdtemp())
        (d / "fleet.json").write_text("{ 不是 json", encoding="utf-8")
        with self.assertRaises(FleetConfigError) as cm:
            load_fleet(d)
        self.assertIn("JSON", str(cm.exception))

    def test_missing_instructions_file_fails_at_load(self):
        """加载期就报，别等到真派活才发现引用的长文不存在。"""
        d = make_fleet()
        os.remove(d / "agents" / "甲.md")
        with self.assertRaises(FleetConfigError):
            load_fleet(d)

    def test_unknown_backend_fails_at_load(self):
        with self.assertRaises(FleetConfigError) as cm:
            load_fleet(make_fleet(backend="不存在的后端"))
        self.assertIn("不存在的后端", str(cm.exception))

    def test_state_dir_created_under_fleet(self):
        fl = load_fleet(make_fleet())
        self.assertTrue(Path(fl.state_dir).exists())
        self.assertTrue((Path(fl.state_dir) / "fleet.db").exists())


class TestIdentity(unittest.TestCase):

    def test_prompt_sections_in_fixed_order(self):
        sp = load_fleet(make_fleet()).get("甲").raw.system_prompt
        order = [sp.index(h) for h in ("# 业务场景", "# 共同约束", "# 你手上有什么",
                                       "# 你是谁", "# 你的工作内容")]
        self.assertEqual(order, sorted(order))

    def test_global_md_and_agent_md_both_land(self):
        sp = load_fleet(make_fleet()).get("甲").raw.system_prompt
        self.assertIn("全队共通的话", sp)
        self.assertIn("甲的工作内容", sp)

    def test_display_name_is_fleet_name_not_backend(self):
        """get_agent() 的第一个形参也叫 name，撞过一次参 —— 显示名得是 fleet 里的名字。"""
        self.assertEqual(load_fleet(make_fleet()).get("甲").raw.name, "甲")

    def test_skills_declared_in_config_reach_the_prompt(self):
        sp = load_fleet(make_fleet(skills=["某技能"])).get("甲").raw.system_prompt
        self.assertIn("某技能", sp)

    def test_identity_mode_reflects_backend_caps(self):
        fl = load_fleet(make_fleet())
        self.assertEqual(fl.get("甲").identity_mode, "system")          # claude 是真系统轮
        self.assertEqual(fl.get("甲", backend="codex").identity_mode, "prompt-prefix")


class TestContractWiring(unittest.TestCase):

    def test_untrusted_lands_on_backend_args(self):
        ag = load_fleet(make_fleet(trust="untrusted")).get("甲")
        self.assertEqual(ag.raw.mode, "readonly")
        self.assertEqual(set(ag.raw.deny), {"shell", "write", "network", "spawn"})
        argv = ag.raw._build_argv("x", None)
        i = argv.index("--disallowedTools")
        self.assertIn("WebFetch", argv[i:i + 14])

    def test_untrusted_on_weak_backend_refuses(self):
        with self.assertRaises(ContractError):
            load_fleet(make_fleet(trust="untrusted")).get("甲", backend="agy")

    def test_readonly_assets_force_readonly_and_mount(self):
        ag = load_fleet(make_fleet(assets={"资料": "./资料"})).get("甲")
        self.assertEqual(ag.raw.mode, "readonly")
        self.assertEqual(ag.contract.assets_enforced, "hard")
        self.assertTrue(any(d.endswith("资料") for d in ag.raw.add_dirs))
        self.assertTrue(all(os.path.isabs(d) for d in ag.raw.add_dirs))

    def test_output_schema_reaches_command_line(self):
        ag = load_fleet(make_fleet(output_schema={"type": "object"})).get("甲")
        self.assertIn("--json-schema", ag.raw._build_argv("x", None))

    def test_backend_specific_param_error_names_the_key(self):
        """配置错要说清楚是哪个键 + 哪个后端，别让人读 traceback 猜。"""
        with self.assertRaises(FleetConfigError) as cm:
            load_fleet(make_fleet(extra={"根本没有这个参数": 1})).get("甲")
        self.assertIn("甲", str(cm.exception))


class TestRawEscapeHatch(unittest.TestCase):

    def test_raw_returns_bare_agent(self):
        ag = load_fleet(make_fleet()).get("甲", raw=True)
        self.assertFalse(hasattr(ag, "contract"), "raw 是裸的 agents2 Agent")

    def test_recorded_forwards_unknown_attrs(self):
        ag = load_fleet(make_fleet()).get("甲")
        self.assertTrue(ag.caps.system_prompt)      # 透传给底层
        self.assertTrue(ag.explain())


class TestBatchDispatch(unittest.TestCase):
    """批量派活。`max_concurrent` 只有在并发时才有意义 —— 这是它的配套用法。"""

    def setUp(self):
        self.fl = load_fleet(make_fleet(max_concurrent=2))
        self.fakes = {}
        real_get = self.fl.get

        def patched(name, **kw):
            ag = real_get(name, **kw)
            ag._agent = self.fakes.setdefault(
                name, FakeAgent([Result(text="ok", ok=True, cost_usd=0.01)], delay=0.05))
            return ag
        self.fl.get = patched

    def test_returns_in_input_order(self):
        jobs = [{"agent": "甲", "prompt": f"p{i}", "ref": f"r{i}"} for i in range(5)]
        outs = self.fl.dispatch(jobs, workers=4, trigger="batch")
        self.assertEqual([o.run.ref for o in outs], [f"r{i}" for i in range(5)])
        self.assertTrue(all(o.ok for o in outs))

    def test_agent_limit_caps_real_concurrency(self):
        """workers 开大也不会突破 agent 自己的名额，多出来的排队并留痕。"""
        outs = self.fl.dispatch(
            [{"agent": "甲", "prompt": "p"} for _ in range(6)], workers=6)
        self.assertTrue(any(o.run.waited == "capacity" for o in outs),
                        "超出名额的应当记 waited=capacity")

    def test_one_bad_job_does_not_kill_the_batch(self):
        outs = self.fl.dispatch([{"agent": "甲", "prompt": "ok"},
                                 {"agent": "不存在", "prompt": "x"},
                                 {"agent": "甲", "prompt": "ok"}])
        self.assertEqual([o.ok for o in outs], [True, False, True])

    def test_missing_keys_are_rejected(self):
        out = self.fl.dispatch([{"agent": "甲"}])[0]
        self.assertFalse(out.ok)

    def test_empty_batch(self):
        self.assertEqual(self.fl.dispatch([]), [])


class TestRosterWrite(unittest.TestCase):
    """改名册。只管这份配置的校验和原子写 —— .md 由业务自己写。"""

    def setUp(self):
        self.d = make_fleet()

    def test_upsert_then_visible_after_reload(self):
        (self.d / "agents" / "乙.md").write_text("乙的活。\n", encoding="utf-8")
        load_fleet(self.d).upsert_agent(
            "乙", {"intro": "乙", "instructions_file": "agents/乙.md"}, overwrite=False)
        self.assertIn("乙", load_fleet(self.d).names())

    def test_upsert_rejects_duplicate_when_not_overwriting(self):
        with self.assertRaises(FleetConfigError):
            load_fleet(self.d).upsert_agent("甲", {"intro": "x"}, overwrite=False)

    def test_upsert_preserves_other_agents(self):
        (self.d / "agents" / "乙.md").write_text("乙的活。\n", encoding="utf-8")
        fl = load_fleet(self.d)
        fl.upsert_agent("乙", {"intro": "乙", "instructions_file": "agents/乙.md"})
        self.assertEqual(set(load_fleet(self.d).names()), {"甲", "乙"})

    def test_reread_from_disk_not_memory(self):
        """两个进程各改一个 agent，后写的不能把先写的抹掉 ——
           所以回写前必须从磁盘重读，不能拿内存里的 config。"""
        (self.d / "agents" / "乙.md").write_text("乙。\n", encoding="utf-8")
        stale = load_fleet(self.d)                       # 早就加载好、内存里没有「乙」
        load_fleet(self.d).upsert_agent("乙", {"intro": "乙", "instructions_file": "agents/乙.md"})
        stale.upsert_agent("甲", {"intro": "改过的甲", "instructions_file": "agents/甲.md"})
        self.assertEqual(set(load_fleet(self.d).names()), {"甲", "乙"}, "乙不该被抹掉")

    def test_remove(self):
        fl = load_fleet(self.d)
        self.assertTrue(fl.remove_agent("甲"))
        self.assertFalse(fl.remove_agent("甲"))
        self.assertEqual(load_fleet(self.d).names(), [])

    def test_write_is_atomic(self):
        """写到一半被杀不能留下半个 fleet.json —— 那会让整支队伍下次加载起不来。"""
        fl = load_fleet(self.d)
        fl.upsert_agent("甲", {"intro": "x", "instructions_file": "agents/甲.md"})
        self.assertFalse((self.d / "fleet.json.tmp").exists(), "临时文件该被 rename 掉")
        json.loads((self.d / "fleet.json").read_text(encoding="utf-8"))   # 不炸就算合法


class TestSession(unittest.TestCase):

    def test_history_persists_and_run_is_recorded(self):
        fl = load_fleet(make_fleet())
        fake = FakeAgent([Result(text="记住了", ok=True, continuation="c-1"),
                          Result(text="BLUE-42", ok=True, continuation="c-1")])
        real_get = fl.get

        def patched(name, **kw):
            ag = real_get(name, **kw)
            ag._agent = fake
            return ag
        fl.get = patched

        s = fl.session("甲", "t-1")
        s.ask("记住暗号")
        s.ask("暗号是什么")
        self.assertEqual(len(s.history()), 2)
        self.assertEqual(fake.continuations[1], "c-1", "第二轮该带着句柄续接")
        self.assertEqual(len(fl.history(limit=10)), 2, "会话不豁免留痕")

    def test_fingerprint_change_drops_the_handle(self):
        """执行环境变了还硬续，agent 会带着上一个环境的记忆在新环境干活。"""
        from my_agents_fleet.session import Session
        from my_agents_fleet.store import JsonFileStore, SessionState

        st = JsonFileStore(tempfile.mkdtemp())
        st.save(SessionState(session_id="t", agent_name="甲",
                             continuation="old", fingerprint="AAA"))
        s = Session(None, "甲", "t", st)
        self.assertFalse(s._gate(st.load("t"), "BBB"))
        self.assertTrue(s._gate(st.load("t"), "AAA"))

    def test_unknown_agent_rejected(self):
        with self.assertRaises(FleetConfigError):
            load_fleet(make_fleet()).session("没这个人", "t-1")

    def _patched(self, fake):
        fl = load_fleet(make_fleet())
        real = fl.get

        def patched(name, **kw):
            ag = real(name, **kw)
            ag._agent = fake
            return ag
        fl.get = patched
        return fl

    def test_prompt_can_be_a_function_of_resuming(self):
        """「这轮是不是续接」只有 Session 知道，但它决定 prompt 该不该带背景。
           传函数进去，冷启带背景、续接不带 —— 否则每轮重发一遍，既费 token
           又把模型框成「每次都是新对话」。"""
        fake = FakeAgent([Result(text="a", ok=True, continuation="c-1"),
                          Result(text="b", ok=True, continuation="c-1")])
        s = self._patched(fake).session("甲", "t-fn")
        build = lambda ctx: ("续接版" if ctx.resuming else "冷启版：带背景")
        s.ask(build)
        s.ask(build)
        self.assertIn("冷启版", fake.prompts[0])
        self.assertIn("续接版", fake.prompts[1])

    def test_plain_string_prompt_still_works(self):
        fake = FakeAgent([Result(text="a", ok=True, continuation="c-1")])
        self._patched(fake).session("甲", "t-str").ask("就是一个字符串")
        self.assertIn("就是一个字符串", fake.prompts[0])

    def test_stale_session_rebuilds_prompt_as_cold(self):
        """续接失效 → 冷启重跑时必须**重新组** prompt 把背景补回来。
           原来直接拿续接版重跑，等于冷启动却缺了背景。"""
        fake = FakeAgent([Result(text="", ok=True, continuation="c-1"),
                          Result(text="", ok=False, error="session not found: c-1"),
                          Result(text="ok", ok=True, continuation="c-2")])
        s = self._patched(fake).session("甲", "t-stale")
        build = lambda ctx: ("续接版" if ctx.resuming else "冷启版：带背景")
        s.ask(build)                      # 第 1 轮：冷启
        s.ask(build)                      # 第 2 轮：续接→失效→冷启重跑
        self.assertEqual(len(fake.prompts), 3)
        self.assertIn("续接版", fake.prompts[1], "第2轮先按续接试")
        self.assertIn("冷启版", fake.prompts[2], "失效后重跑要补回背景")

    def test_persisted_history_holds_resolved_string(self):
        """落盘的是解析后的字符串，不是那个函数 —— 冷启兜底重放历史时要能读。"""
        fake = FakeAgent([Result(text="a", ok=True, continuation="c-1")])
        s = self._patched(fake).session("甲", "t-persist")
        s.ask(lambda ctx: "解析出来的内容")
        self.assertEqual(s.history()[0].prompt, "解析出来的内容")


class TestCapacitySharing(unittest.TestCase):

    def test_same_state_dir_shares_one_capacity(self):
        """load_fleet 每次都重读磁盘、每次都造新 Fleet —— 每个实例各带一套信号量的话，
           并发上限形同虚设。"""
        d = make_fleet()
        self.assertIs(load_fleet(d).capacity, load_fleet(d).capacity)

    def test_different_fleets_do_not_share(self):
        self.assertIsNot(load_fleet(make_fleet()).capacity,
                         load_fleet(make_fleet()).capacity)


if __name__ == "__main__":
    unittest.main()
