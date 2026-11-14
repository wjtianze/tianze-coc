"""新战斗操作（援军/技能/攻城机器/回溯）、星数、村庄 JSON 替换与离线开局流程（模拟探针）。"""
import json
import struct
import tempfile
import unittest
from pathlib import Path

from tianze_coc.assets.unit_catalog import UnitCatalog
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv
from tianze_coc.contracts import ActionV1
from tianze_coc.fake_probe import FakeEngine, serve
from tianze_coc.home_payload import encode_compressed_string, find_compressed_json, replace_home_json
from tianze_coc.native_client import ProbeClient
from tianze_coc.offline_session import OfflineStarter, message_ids

CATALOG = UnitCatalog.load()
HOME = {"buildings": [{"data": 1000001, "lvl": 9, "x": 20, "y": 20}], "obstacles": []}


def payload_with(home: dict) -> bytes:
    return b"\x00\x00\x00\x07header" + encode_compressed_string(json.dumps(home)) + b"\x01\x02tail-avatar-bytes"


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.srv, port = serve(self.engine)
        self.env = TianzeBattleEnv(EnvConfig(battle_wait_timeout_s=2), client=ProbeClient(port=port), catalog=CATALOG)

    def tearDown(self):
        self.env.close()
        self.srv.shutdown()


class NewActionsTest(EnvCase):
    def test_clan_castle_once(self):
        obs = self.env.reset()
        self.assertIn(4000006, obs.cc_units)
        self.assertEqual(self.env.legal_units(obs)["cc"], [1])
        _, info, _ = self.env.step([ActionV1("cc", x=5, y=5)])
        self.assertEqual(info.results[0].status, "executed")
        _, info, _ = self.env.step([ActionV1("cc", x=6, y=6)])
        self.assertIn("已经部署过", info.rejected_locally[0][1])

    def test_cc_siege(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("cc_siege", 4000052, 5, 5)])
        self.assertEqual(info.results[0].status, "executed")
        _, info, _ = self.env.step([ActionV1("cc_siege", 4000052, 5, 5)])
        self.assertIn("一局只能部署一台", info.rejected_locally[0][1])

    def test_only_one_siege_per_battle(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("siege", 4000051, 5, 5)])
        self.assertEqual(info.results[0].status, "executed")
        self.assertEqual(self.env.legal_units()["cc_siege"], [])
        _, info, _ = self.env.step([ActionV1("cc_siege", 4000052, 6, 6)])
        self.assertIn("一局只能部署一台", info.rejected_locally[0][1])

    def test_siege_counts_from_siege_list(self):
        obs = self.env.reset()
        self.assertEqual(obs.army_sieges, {4000051: 1})
        _, info, _ = self.env.step([ActionV1("siege", 4000051, 5, 5)])
        self.assertEqual(info.results[0].status, "executed")

    def test_ability_requires_deployment(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("ability", 28000000)])
        self.assertIn("尚未部署", info.rejected_locally[0][1])
        self.env.step([ActionV1("hero", 28000000, 5, 5)])
        self.assertIn(28000000, self.env.legal_units()["ability"])
        _, info, _ = self.env.step([ActionV1("ability", 28000000)])
        self.assertEqual(info.results[0].status, "executed")

    def test_siege_release_is_ability(self):  # noqa: D102
        self.env.reset()
        self.env.step([ActionV1("siege", 4000051, 5, 5)])
        _, info, _ = self.env.step([ActionV1("ability", 4000051)])
        self.assertEqual(info.results[0].status, "executed")

    def test_cc_spell_level_from_observation(self):
        obs = self.env.reset()
        self.assertEqual(obs.cc_levels[26000003], 5)
        self.assertEqual(self.env.legal_units(obs)["cc_spell"], [26000003])
        _, info, _ = self.env.step([ActionV1("cc_spell", 26000003, 6, 6)])
        self.assertEqual(info.results[0].status, "executed")
        _, info, _ = self.env.step([ActionV1("cc_spell", 26000003, 6, 6)])
        self.assertIn("没有该法术", info.rejected_locally[0][1])

    def test_hero_mode_rules(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("hero_mode", 28000000, flag=1)])
        self.assertIn("没有可切换的模式", info.rejected_locally[0][1])
        _, info, _ = self.env.step([ActionV1("hero_mode", 28000002, flag=1)])
        self.assertEqual(info.results[0].status, "executed")
        self.env.step([ActionV1("hero", 28000002, 5, 5)])
        _, info, _ = self.env.step([ActionV1("hero_mode", 28000002, flag=0)])
        self.assertIn("不能再切换模式", info.rejected_locally[0][1])

    def test_recalled_passthrough(self):
        self.env.reset()
        _, info, _ = self.env.step([ActionV1("recalled", x=5, y=5, flag=0, extra=2)])
        self.assertEqual(info.results[0].status, "executed")

    def test_stars(self):
        self.env.reset()
        self.env.step([ActionV1("troop", 4000000, 5, 5)] * 8)
        obs = None
        for _ in range(3000):
            obs, _, done = self.env.step(subticks=10)
            if done:
                break
        self.assertEqual(obs.destruction, 100)
        self.assertTrue(obs.th_destroyed)
        self.assertEqual(obs.stars, 3)


class HomePayloadTest(unittest.TestCase):
    def test_find_and_replace(self):
        p = payload_with(HOME)
        found = find_compressed_json(p)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].obj, HOME)
        new = {"buildings": [{"data": 1000008, "lvl": 3, "x": 1, "y": 2}] * 50}
        q = replace_home_json(p, new)
        self.assertEqual(find_compressed_json(q)[0].obj, new)
        self.assertTrue(q.startswith(b"\x00\x00\x00\x07header"))
        self.assertTrue(q.endswith(b"tail-avatar-bytes"))

    def test_inconsistent_prefix_is_ignored(self):
        p = bytearray(payload_with(HOME))
        i = p.find(b"\x78\xda")
        struct.pack_into(">i", p, i - 8, 3)  # 破坏长度前缀
        self.assertEqual(find_compressed_json(bytes(p)), [])
        with self.assertRaises(ValueError):
            replace_home_json(bytes(p), HOME)

    def test_non_home_json_ignored(self):
        p = b"xxxxxxxx" + encode_compressed_string(json.dumps({"foo": 1}))
        self.assertEqual(find_compressed_json(p), [])
        self.assertEqual(len(find_compressed_json(p, require_key=None)), 1)


class OfflineStarterTest(unittest.TestCase):
    def test_reset_via_replay_with_layout_rotation(self):
        engine = FakeEngine()
        ids = message_ids()
        engine.home_type, engine.enemy_type = ids["OwnHomeData"], ids["EnemyHomeData"]
        engine.gm_state = 1  # 在主村
        engine.battle.in_battle = False
        srv, port = serve(engine)
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "acct.OwnHomeData.bin"
            enemy = Path(d) / "acct.EnemyHomeData.bin"
            home.write_bytes(b"own-home-bytes")
            enemy.write_bytes(payload_with(HOME))
            layouts = [{"buildings": [{"data": 1000001, "x": i, "y": i}]} for i in range(3)]
            starter = OfflineStarter(home, enemy, layouts=layouts)
            env = TianzeBattleEnv(EnvConfig(battle_wait_timeout_s=2), client=ProbeClient(port=port), catalog=CATALOG,
                                  battle_starter=starter)
            try:
                for rnd in range(4):
                    obs = env.reset()
                    self.assertTrue(obs.in_battle)
                    self.assertEqual(obs.army_units[4000000], 10)  # 每局都是全新军队
                    sent = find_compressed_json(engine.records[ids["EnemyHomeData"]])[0].obj
                    self.assertEqual(sent, layouts[rnd % 3])
                    env.step([ActionV1("troop", 4000000, 5, 5)])
                    self.assertTrue(engine.net["offline"])
            finally:
                env.close()
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class LayoutToolsTest(unittest.TestCase):
    def test_extract_shift_replace(self):
        from tianze_coc.home_payload import extract_home, shift_layout
        p = payload_with({"buildings": [{"data": 1000001, "x": 20, "y": 20}], "obstacles": [{"data": 8000000, "x": 1, "y": 1}]})
        home = extract_home(p)
        moved = shift_layout(home, dx=-14)
        self.assertEqual(moved["buildings"][0]["x"], 6)
        self.assertEqual(moved["obstacles"], [])
        self.assertEqual(extract_home(replace_home_json(p, moved)), moved)
        with self.assertRaises(ValueError):
            shift_layout(home, dx=-30)
