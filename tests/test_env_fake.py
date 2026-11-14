"""用模拟探针测试中间层：协议、锁步、规则校验。不需要模拟器。"""
import unittest

from tianze_coc.assets.unit_catalog import UnitCatalog
from tianze_coc.battle_env import EnvConfig, EnvError, TianzeBattleEnv
from tianze_coc.contracts import ActionV1
from tianze_coc.fake_probe import FakeBattle, FakeEngine, serve
from tianze_coc.native_client import ProbeClient

CATALOG = UnitCatalog.load()


def make_env(battle=None):
    engine = FakeEngine(battle)
    srv, port = serve(engine)
    env = TianzeBattleEnv(EnvConfig(battle_wait_timeout_s=2), client=ProbeClient(port=port), catalog=CATALOG)
    return env, engine, srv


class EnvTest(unittest.TestCase):
    def setUp(self):
        self.env, self.engine, self.srv = make_env()

    def tearDown(self):
        self.env.close()
        self.srv.shutdown()

    def test_reset_configures_lockstep_and_allowlist(self):
        obs = self.env.reset()
        self.assertTrue(self.engine.lockstep)
        self.assertIn(4000098, self.engine.allowed)  # 超级野猪骑士：允许
        self.assertNotIn(4000030, self.engine.allowed)  # 寒冰法师：限时，禁止
        self.assertEqual(obs.army_units[4000000], 10)
        self.assertAlmostEqual(obs.width_tiles, 50.0)

    def test_deploy_and_step(self):
        self.env.reset()
        obs, info, done = self.env.step([ActionV1("troop", 4000000, 10.5, 3.25)], subticks=5)
        self.assertEqual([r.status for r in info.results], ["executed"])
        self.assertEqual(obs.army_units[4000000], 9)
        self.assertEqual(obs.tick, 5)
        self.assertFalse(done)

    def test_coordinates_converted_to_logic_units(self):
        self.assertEqual(ActionV1("troop", 1, 10.5, 3.25).to_wire(7), {"id": 7, "kind": "troop", "data": 1,
                                                                       "x": 5376, "y": 1664, "flag": 0, "extra": 0})

    def test_banned_unit_rejected_locally(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("troop", 4000030, 5, 5)])
        self.assertEqual(len(info.rejected_locally), 1)
        self.assertIn("temporary_event", info.rejected_locally[0][1])
        self.assertEqual(info.results, [])

    def test_probe_side_allowlist_is_second_line(self):
        self.env.reset()
        c = self.env.client
        r = c.step(1, [{"id": 99, "kind": "troop", "data": 4000030, "x": 100, "y": 100}])
        self.assertEqual(r["results"][0]["reason"], "unit_not_allowed")

    def test_kind_mismatch_and_out_of_map(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("spell", 4000000, 5, 5), ActionV1("troop", 4000000, 60, 5)])
        reasons = [r for _, r in info.rejected_locally]
        self.assertTrue(any("不符" in r for r in reasons))
        self.assertTrue(any("超出地图" in r for r in reasons))

    def test_no_remaining_units(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("siege", 4000051, 5, 5)])
        self.assertEqual(info.results[0].status, "executed")
        _, info, _ = self.env.step([ActionV1("siege", 4000051, 5, 5)])
        self.assertIn("一局只能部署一台", info.rejected_locally[0][1])
        for _ in range(3):
            self.env.step([ActionV1("spell", 26000000, 5, 5)])
        _, info, _ = self.env.step([ActionV1("spell", 26000000, 5, 5)])
        self.assertIn("没有剩余", info.rejected_locally[0][1])

    def test_hero_once(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("hero", 28000000, 5, 5)])
        self.assertEqual(info.results[0].status, "executed")
        _, info, _ = self.env.step([ActionV1("hero", 28000000, 6, 6)])
        self.assertIn("已经部署过", info.rejected_locally[0][1])

    def test_battle_ends(self):
        self.env.reset()
        self.env.step([ActionV1("troop", 4000000, 5, 5)] * 5)
        done = False
        for _ in range(2000):
            obs, _, done = self.env.step(subticks=10)
            if done:
                break
        self.assertTrue(done)
        self.assertTrue(obs.battle_over)


class ArmyRuleTest(unittest.TestCase):
    def test_army_with_event_troop_refused(self):
        b = FakeBattle()
        b.army_units[4000030] = 2  # 寒冰法师（限时）
        env, _, srv = make_env(b)
        try:
            with self.assertRaises(EnvError) as cm:
                env.reset()
            self.assertIn("限时", str(cm.exception))
        finally:
            env.close()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
