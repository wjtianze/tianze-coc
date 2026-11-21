"""三个模型（摆阵 / 配兵 / 进攻）前置接口的离线测试：规则表、修改器、阵型、配兵、命令编码、公平观测。

只依赖仓库内的 data/progression.json；用到录制负载的测试在没有录制时跳过（录制含账号信息，不进仓库）。
"""
import random
import unittest
from pathlib import Path

from tianze_coc import attacker_payload as ap
from tianze_coc import avatar_codec as ac
from tianze_coc import home_control as hc
from tianze_coc import layout as L
from tianze_coc.account import TH_GID, Account
from tianze_coc.army import Army, HeroPick, capacity, validate
from tianze_coc.assets.progression import Progression
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv
from tianze_coc.contracts import EntityV1, ObservationV1

REC = Path(__file__).resolve().parent.parent / "tianze_coc" / "data" / "records"
OWN = REC / "th19_after_ops.OwnHomeData.bin"
ENEMY = REC / "th19_friendly.EnemyHomeData.bin"
P = Progression.load()


def gid(name: str) -> int:
    return P.structure(name)["gid"]


def ugid(name: str) -> int:
    return next(g for g, u in P.units.items() if u["name"] == name)


def blank(th: int) -> Account:
    a = Account({"buildings": [], "traps": [], "obstacles": []}, {}, P)
    a.add_structure(TH_GID, th)
    return a


class VIntTest(unittest.TestCase):
    def test_roundtrip_and_game_bytes(self):
        for v in (0, 1, 63, 64, -1, -63, -64, -100, 500000005, 31000000, -500000000):
            e = ac.write_vint(v)
            self.assertEqual(ac.read_vint(e, 0), (v, len(e)))
        self.assertEqual(ac.write_vint(-1).hex(), "7f")  # 游戏 UI 命令的执行 tick
        self.assertEqual(ac.write_vint(500000005).hex(), "8594ebdc03")  # 建筑编号


class CommandEncodingTest(unittest.TestCase):
    def test_encoders_reproduce_captured_ui_bytes(self):
        fns = {"move": hc.enc_move, "loadout_remove": hc.enc_loadout_remove, "loadout_add": hc.enc_loadout_add,
               "loadout_add_squad": hc.enc_loadout_add, "set_equipment": hc.enc_set_equipment,
               "set_equipment_slot1": hc.enc_set_equipment, "set_pet": hc.enc_set_pet, "set_hero": hc.enc_set_hero,
               "hero_mode": hc.enc_hero_mode, "defense_cc": hc.enc_defense_cc}
        for k, (args, want) in hc.CAPTURED.items():
            self.assertEqual(fns[k](*args).hex(), want, k)


class ProgressionRulesTest(unittest.TestCase):
    def test_lab_caps_troop_level(self):
        pekka = ugid("PEKKA")
        cap = P.max_unit_level(pekka, {"Laboratory": 13, "Barracks": 19})
        self.assertEqual(cap, 10)  # 皮卡 10 级需要 13 级实验室，11 级需要 14 级
        self.assertEqual(P.max_unit_level(pekka, {"Laboratory": 16, "Barracks": 9}), 0)  # 兵营未解锁

    def test_hero_needs_th_and_hall(self):
        bk = ugid("Barbarian King")
        self.assertEqual(P.max_unit_level(bk, {"Town Hall": 3, "Hero Hall": 12}), 0)
        self.assertGreater(P.max_unit_level(bk, {"Town Hall": 18, "Hero Hall": 12}), 100)

    def test_counts_th18(self):
        self.assertEqual(P.max_count("Firespitter", 18), 2)
        self.assertEqual(P.max_count("Wall", 18), 325)

    def test_counts_th19(self):
        self.assertEqual(P.th_max, 19)
        self.assertEqual(P.max_count("Wall", 19), 350)
        for n, c in (("Spiky Tower", 2), ("Firecracker Tower", 2), ("Guardian Building", 1), ("Crafting Station", 1)):
            self.assertEqual((P.max_count(n, 18), P.max_count(n, 19)), (0, c), n)  # 19.32 起精制台在 19 本
        self.assertEqual(P.max_count("Squad Barracks", 8), 1)
        squad = P.units[ugid("Barcher Squad")]
        self.assertEqual((squad["kind"], squad["unlock"]), ("squad", {"Squad Barracks": 1}))
        self.assertEqual(P.max_unit_level(ugid("Barcher Squad"), {"Squad Barracks": 1, "Laboratory": 16}), 1)
        self.assertEqual(P.max_unit_level(ugid("Barcher Squad"), {"Squad Barracks": 1, "Laboratory": 17}), 3)


class AccountEditorTest(unittest.TestCase):
    def test_max_out_is_legal(self):
        for th in (9, 14, 18, 19):
            a = blank(th)
            a.max_out()
            self.assertEqual(a.validate(), [], th)

    def test_random_accounts_are_legal(self):
        for th in (7, 12, 16, 18, 19):
            a = blank(th)
            a.randomize(th, random.Random(th), progress=0.5)
            self.assertEqual(a.validate(), [], th)

    def test_random_level_distribution_steady_rush(self):
        """稳本：多数等级在"上一本满级 ~ 本本满级"；速本：只有关键建筑（军事类）跟上，防御等停在更早。"""
        from tianze_coc.account import RUSH, STEADY, _is_key

        def ratio(style, key):
            hit = tot = 0
            for i in range(15):
                a = blank(19)
                a.randomize(19, random.Random(i), style=style)
                self.assertEqual(a.validate(), [])
                for s in a.structures:
                    d = P.structure(s.gid)
                    prev = P.max_structure_level(s.gid, 18)
                    if d is None or s.gid == TH_GID or prev <= 0 or _is_key(d) != key:
                        continue
                    tot += 1
                    hit += s.level >= prev
            return hit / tot

        self.assertGreater(ratio(STEADY, False), 0.8)
        self.assertGreater(ratio(STEADY, True), 0.8)
        self.assertLess(ratio(RUSH, False), 0.4)
        self.assertGreater(ratio(RUSH, True), 0.8)

    def test_random_counts_between_prev_and_cur_max(self):
        """每种建筑（含城墙、陷阱、合并原料的等效数量）都在 [上一本上限, 本本上限] 之间。"""
        for th in (3, 8, 13, 17, 19):
            for i in range(8):
                a = blank(th)
                a.randomize(th, random.Random(100 * th + i))
                c = a.counts()
                used = {}
                for d in P.data["structures"]:
                    for inp, k in (d.get("merge") or {}).get("inputs", {}).items():
                        used[P.structure(inp)["gid"]] = used.get(P.structure(inp)["gid"], 0) + k * c.get(d["gid"], 0)
                for d in P.data["structures"]:
                    if d["gid"] == TH_GID or P.max_structure_level(d["gid"], th) <= 0:
                        continue
                    hi = d["max_count"][th - 1]
                    lo = min(d["max_count"][th - 2], hi) if P.max_structure_level(d["gid"], th - 1) > 0 else 0
                    n = c.get(d["gid"], 0) + used.get(d["gid"], 0)
                    self.assertTrue(lo <= n <= hi, (th, d["name"], lo, n, hi))
        a = blank(19)
        a.max_out()
        walls = [s for s in a.structures if s.gid == gid("Wall")]
        for s in walls[324:]:  # 只剩 324 段，少于 18 本上限 325
            a.structures.remove(s)
        self.assertTrue(any(e.code == "not_all_built" and e.gid == gid("Wall") for e in a.validate()))  # 城墙也要建齐

    def test_low_levels_per_item(self):
        """每项独立地有机会取很低的等级：同一账号里可以有满级熔岩猎犬 + 1 级气球。"""
        hound, balloon = ugid("Lava Hound"), ugid("Balloon")
        lows = combo = 0
        for i in range(300):
            a = blank(19)
            a.randomize(19, random.Random(i), style="steady")
            lows += a.unit_levels.get(balloon) == 1
            combo += a.unit_levels.get(balloon) == 1 and a.unit_levels.get(hound) == P.units[hound]["max_level"]
        self.assertGreater(lows, 0)
        self.assertGreater(combo, 0)

    def test_hero_hall_maxed_before_th_up(self):
        """英雄殿堂没升满不能升大本营：t 本时殿堂 ≥ t-1 本满级；随机账号大多直接升到本本满级。"""
        hh = gid("Hero Hall")
        full = 0
        for i in range(40):
            a = blank(16)
            a.randomize(16, random.Random(i))
            lv = max(s.level for s in a.structures if s.gid == hh)
            self.assertGreaterEqual(lv, P.max_structure_level(hh, 15))
            full += lv == P.max_structure_level(hh, 16)
        self.assertGreater(full, 25)
        a = blank(16)
        a.max_out()
        next(s for s in a.structures if s.gid == hh).level = P.max_structure_level(hh, 15) - 1
        self.assertTrue(any(e.code == "not_maxed" for e in a.validate()))

    def test_lab_violation(self):
        a = blank(18)
        a.max_out()
        lab = next(s for s in a.structures if s.gid == gid("Laboratory"))
        lab.level = 13
        errs = a.validate()
        self.assertTrue(any(e.code == "unit_level" and e.gid == ugid("PEKKA") for e in errs))

    def test_all_built_rule(self):
        a = blank(18)
        a.max_out()
        fs = [s for s in a.structures if s.gid == gid("Firespitter")]
        a.structures.remove(fs[0])  # 18 本只有一座火焰喷射器
        errs = a.validate()
        self.assertTrue(any(e.code == "not_all_built" and e.gid == gid("Firespitter") for e in errs))
        self.assertFalse(any(e.code == "not_all_built" for e in a.validate(require_all_built=False)))

    def test_merge_rule_th19(self):
        """19 本：箭塔是多人箭塔、复合机械塔、烟花炮手塔三种合并建筑的原料；投石炮被烟花炮手塔消耗。"""
        a = blank(19)
        a.max_out()
        self.assertEqual(a.counts().get(gid("Archer Tower"), 0), 0)
        self.assertEqual(a.counts().get(gid("Scattershot"), 0), 0)
        a.add_structure(gid("Scattershot"))
        self.assertTrue(any(e.code == "merge_inputs" for e in a.validate()))

    def test_merge_rule(self):
        a = blank(18)
        a.max_out()
        a.add_structure(gid("Archer Tower"))  # 多人箭塔已占用原料，再加箭塔超限
        self.assertTrue(any(e.code == "merge_inputs" for e in a.validate()))


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.a = blank(18)
        self.a.max_out()

    def test_random_layout_valid_and_hidden_channel(self):
        lay = L.random_layout(self.a, random.Random(1))
        self.assertEqual(L.validate(lay, self.a), [])
        L.apply(lay, self.a)
        g = L.encode_grid(self.a)
        v = L.encode_grid(self.a, attacker_view=True)
        self.assertGreater(g[1].sum(), 0)
        self.assertEqual(v[1].sum(), 0)  # 进攻方视角看不到陷阱/特斯拉
        self.assertFalse(any(x["category"] == "trap" for x in L.attacker_view(self.a)))

    def test_overlap_detected(self):
        lay = L.Layout.of(self.a)
        lay.placements[1].x, lay.placements[1].y = lay.placements[0].x, lay.placements[0].y
        self.assertTrue(any("重叠" in e for e in L.validate(lay, self.a)))

    def test_defense_cc(self):
        cap = capacity(self.a).cc_troops
        ok = [(ugid("Barbarian"), cap, 1)]
        self.assertEqual(L.validate_defense_cc(ok, self.a), [])
        self.assertTrue(L.validate_defense_cc([(ugid("Barbarian"), cap + 1, 1)], self.a))
        self.assertTrue(L.validate_defense_cc([(ugid("Barbarian"), 1, 99)], self.a))
        cc = L.random_defense_cc(self.a, random.Random(3))
        self.assertEqual(L.validate_defense_cc(cc, self.a), [])

    def test_move_planner_handles_swaps(self):
        ctl = hc.HomeController.__new__(hc.HomeController)
        cur = {1: (10, 10, 3, 3), 2: (20, 10, 3, 3)}
        moves = ctl.plan_layout(cur, {1: (20, 10), 2: (10, 10)}, set())
        pos = {1: (10, 10), 2: (20, 10)}
        for k, x, y in moves:  # 逐步检查：任何一步都不能和别的建筑重叠
            pos[k] = (x, y)
            a, b = pos[1], pos[2]
            self.assertFalse(abs(a[0] - b[0]) < 3 and abs(a[1] - b[1]) < 3)
        self.assertEqual(pos, {1: (20, 10), 2: (10, 10)})


class ArmyTest(unittest.TestCase):
    def setUp(self):
        self.a = blank(18)
        self.a.max_out()

    def test_capacity_th18(self):
        c = capacity(self.a)
        self.assertEqual((c.troops, c.spells, c.sieges, c.hero_slots), (352, 11, 3, 4))

    def test_capacity_th19(self):
        a = blank(19)
        a.max_out()
        c = capacity(a)
        self.assertEqual((c.troops, c.spells, c.sieges, c.hero_slots, c.cc_level), (352, 11, 3, 4, 15))

    def test_rules(self):
        bk, aq, gw = ugid("Barbarian King"), ugid("Archer Queen"), ugid("Grand Warden")
        good = Army({ugid("Barbarian"): 352}, {}, {}, [HeroPick(gw, mode=1)])
        self.assertEqual(validate(good, self.a), [])
        self.assertTrue(validate(Army({ugid("Barbarian"): 353}), self.a))  # 超人口
        self.assertTrue(validate(Army(heroes=[HeroPick(bk, mode=1)]), self.a))  # 野蛮人之王没有第二模式
        item_aq = next(g for g, u in P.units.items() if u["kind"] == "equipment" and aq in u["heroes"])
        self.assertTrue(validate(Army(heroes=[HeroPick(bk, equipment=[item_aq])]), self.a))  # 装备不属于该英雄
        pet = next(g for g, u in P.units.items() if u["kind"] == "pet")
        self.assertTrue(validate(Army(heroes=[HeroPick(bk, pet=pet), HeroPick(aq, pet=pet)]), self.a))  # 战宠重复

    def test_random_army_legal(self):
        from tianze_coc.army import random_army

        rng = random.Random(0)
        seen_super = 0
        for _ in range(30):
            army = random_army(self.a, rng)
            self.assertEqual(validate(army, self.a), [])
            k = sum(1 for g in army.troops if P.units[g]["status"] == "super_troop")
            self.assertLessEqual(k, 2)
            seen_super += k > 0
        self.assertGreater(seen_super, 0)

    def test_squads_in_army(self):
        a = blank(19)
        a.max_out()
        sq = ugid("Barcher Squad")
        self.assertEqual(validate(Army({sq: 2, ugid("Barbarian"): 322}), a), [])  # 15×2 + 322 = 352
        self.assertTrue(validate(Army({sq: 2, ugid("Barbarian"): 323}), a))  # 超人口
        # 种类数不限（用户确认）；人口按等级：满级 PEKKA 雷霆小队 3 级人口 44（1 级 46）
        allsq = {g: 1 for g, u in P.units.items() if u["kind"] == "squad"}
        pe = ugid("PEKKA Etitan Squad")
        self.assertEqual(a.unit_levels[pe], 3)
        from tianze_coc.army import housing
        self.assertEqual(housing(Army({pe: 1}), P, a.unit_levels)["troops"], 44)
        used = housing(Army(dict(allsq)), P, a.unit_levels)["troops"]
        self.assertEqual(validate(Army({**allsq, ugid("Barbarian"): 352 - used}), a), [])

    def test_random_army_includes_squads(self):
        from tianze_coc.army import random_army

        a = blank(19)
        a.max_out()
        rng = random.Random(1)
        seen = 0
        for _ in range(30):
            army = random_army(a, rng)
            self.assertEqual(validate(army, a), [])
            seen += any(P.units[g]["kind"] == "squad" for g in army.troops)
        self.assertGreater(seen, 0)

    def test_army_respects_army_buildings(self):
        """配兵受进攻方军事建筑约束：解锁（训练营/法术工厂/攻城工坊/战宠小屋/小队训练营…）、
        容量（兵营/法术工厂/攻城工坊/部落城堡）、等级上限（实验室/战宠小屋/铁匠铺/部落城堡）。"""
        from tianze_coc.army import random_army
        from tianze_coc.assets.progression import cc_level_cap

        for th in (3, 7, 11, 15, 19):
            for i in range(4):
                a = blank(th)
                a.randomize(th, random.Random(th * 10 + i))
                self.assertIn("Army Camp", a.army_buildings())
                blv, cap = a.building_levels(), capacity(a)
                army = random_army(a, random.Random(i))
                self.assertEqual(validate(army, a), [])
                for g in list(army.troops) + list(army.spells) + list(army.sieges) + [h.pet for h in army.heroes if h.pet]:
                    u = P.units[g]
                    if not u.get("derived_level"):
                        self.assertGreater(P.max_unit_level(g, blv), 0, (th, u["name"]))
                        self.assertLessEqual(a.unit_levels.get(g, 0), P.max_unit_level(g, blv), (th, u["name"]))
                h = {"troops": 0, "spells": 0}
                from tianze_coc.army import housing
                h = housing(army, P, a.unit_levels)
                self.assertLessEqual(h["troops"], cap.troops)
                self.assertLessEqual(h["spells"], cap.spells)
                self.assertLessEqual(len(army.sieges), cap.sieges)
                for g, n, lv in army.cc:
                    self.assertLessEqual(lv, cc_level_cap(P, g, cap.cc_level))
        # 建筑不够时直接拒绝：没有攻城工坊就不能带攻城机器
        a = blank(19)
        a.max_out()
        a.structures = [s for s in a.structures if s.gid != gid("Siege Workshop")]
        self.assertTrue(any("未解锁" in e for e in validate(Army(sieges={ugid("Wall Wrecker"): 1}), a)))

    def test_super_troops(self):
        sb, sa, sg = ugid("Super Barbarian"), ugid("Super Archer"), ugid("Super Giant")
        self.assertEqual(validate(Army({sb: 10, sa: 10}), self.a), [])
        self.assertTrue(validate(Army({sb: 10, sa: 10, sg: 2}), self.a))  # 配兵最多 2 种超级兵
        self.a.set_unit_level(ugid("Barbarian"), 7)
        self.assertTrue(validate(Army({sb: 10}), self.a))  # 超级野蛮人要 8 级野蛮人
        self.a.set_unit_level(ugid("Barbarian"), 8)
        self.assertEqual(validate(Army({sb: 10}), self.a), [])


class FairViewTest(unittest.TestCase):
    def test_traps_hidden_tesla_until_damaged(self):
        env = TianzeBattleEnv.__new__(TianzeBattleEnv)
        env.cfg = EnvConfig()
        env._revealed = set()
        from tianze_coc.battle_env import _hidden_structure_ids

        env._hidden_data = _hidden_structure_ids()
        tesla, bomb, cannon = gid("Hidden Tesla"), gid("Giant Bomb"), gid("Cannon")
        ents = [EntityV1(0, 1, tesla, {"hp": 100, "hp_max": 100}), EntityV1(0, 2, bomb, {}),
                EntityV1(0, 3, cannon, {"hp": 50, "hp_max": 100})]
        obs = ObservationV1(0, True, False, 0, 3, 3, 50, 50, {}, {}, list(ents),
                            raw={"objects": [{"gid": 1}, {"gid": 2}, {"gid": 3}]})
        out = env._fair(obs)
        self.assertEqual([e.global_id for e in out.entities], [3])
        self.assertEqual([o["gid"] for o in out.raw["objects"]], [3])
        ents[0].fields["hp"] = 80  # 特斯拉受伤 = 现身
        out = env._fair(ObservationV1(0, True, False, 0, 3, 3, 50, 50, {}, {}, list(ents)))
        self.assertEqual(sorted(e.global_id for e in out.entities), [1, 3])


@unittest.skipUnless(OWN.exists() and ENEMY.exists(), "没有本地录制（录制含账号信息，不进仓库）")
class RecordedPayloadTest(unittest.TestCase):
    def test_account_roundtrip(self):
        b = OWN.read_bytes()
        a = Account.from_payload(b, "own")
        a.max_out()
        a2 = Account.from_payload(a.apply_to_payload(b, "own"), "own")
        self.assertEqual(a2.unit_levels, a.unit_levels)
        self.assertEqual(len(a2.structures), len(a.structures))

    def test_attacker_fields_roundtrip_and_patch(self):
        b = ENEMY.read_bytes()
        self.assertTrue(ap.roundtrip_ok(b))
        nb = ap.patch(b, troops={4000000: 10}, spells={26000000: 2}, sieges={4000051: 1}, heroes=[28000000],
                      cc=[(4000005, 2, 12)])
        d = ap.describe(nb)
        self.assertEqual((d["troops"], d["spells"], d["sieges"], d["heroes"], d["cc"]),
                         ({4000000: 10}, {26000000: 2}, {4000051: 1}, [28000000], [(4000005, 2, 12)]))
        self.assertTrue(ap.roundtrip_ok(nb))

    def test_attacker_army_is_army_all(self):
        """19.32：防守方也有完整列表链（取最后一条为进攻方）；本局军队以 army_all 为准（真机核对）。"""
        b = ENEMY.read_bytes()
        self.assertGreaterEqual(len(ac.find_chains(b)), 1)
        f = ap.parse(b)
        self.assertIsNotNone(f.army_all)
        nb = ap.patch(b, heroes=[28000000])
        self.assertEqual(ap.parse(nb).army_all.entries, f.army_all.entries)  # 只改英雄不动军队


if __name__ == "__main__":
    unittest.main()


class ClanCastleLevelCapTest(unittest.TestCase):
    def test_against_game(self):
        """6、7 级城堡的游戏内显示（请求增援 -> 编辑）。"""
        from tianze_coc.assets.progression import cc_level_cap

        cc6 = {"Barbarian": 9, "Giant": 9, "Wall Breaker": 8, "Wizard": 9, "Dragon": 7, "Baby Dragon": 6,
               "Electro Dragon": 3, "Archer": 9, "Goblin": 8, "Balloon": 8, "Healer": 5, "PEKKA": 8, "Miner": 6,
               "Yeti": 2, "Minion": 8, "Witch": 5, "Bowler": 4, "Hog Rider": 9, "Golem": 9, "Lava Hound": 5,
               "Lightning": 9, "Wall Wrecker": 2, "Battle Blimp": 2, "Stone Slammer": 2,
               "Super Barbarian": 9 - 4, "Sneaky Goblin": 8 - 4, "Inferno Dragon": 6, "Super Bowler": 4}
        for n, v in cc6.items():
            self.assertEqual(cc_level_cap(P, ugid(n), 6), v, n)
        self.assertEqual(cc_level_cap(P, ugid("Siege Barracks"), 6), 0)  # 6 级城堡要不了攻城训练营
        cc7 = {"Dragon": 8, "Balloon": 9, "Battle Blimp": 3, "Siege Barracks": 2}
        for n, v in cc7.items():
            self.assertEqual(cc_level_cap(P, ugid(n), 7), v, n)
        self.assertEqual(cc_level_cap(P, ugid("Barbarian"), 1), 4)  # 官方例子：1 级城堡最多 4 级野蛮人
        for n, c in {"Sneaky Goblin": 4, "Super Barbarian": 5, "Super Archer": 5, "Super Wall Breaker": 5,
                     "Super Giant": 6, "Inferno Dragon": 6, "Super Bowler": 6, "Super Miner": 7,
                     "Super Hog Rider": 7, "Super Yeti": 7}.items():  # 与官方公告 4/5/6 级一致
            self.assertEqual(cc_level_cap(P, ugid(n), c - 1), 0, n)
            self.assertGreater(cc_level_cap(P, ugid(n), c), 0, n)
        # 6 级城堡（容量 35）请求界面实际列出的超级兵：等级上限 > 0 且人口 <= 35
        room = P.structure("Clan Castle")["housing"]["troops"][5]
        shown = {u["name"] for g, u in P.units.items() if u["status"] == "super_troop"
                 and cc_level_cap(P, g, 6) > 0 and u["housing"] <= room}
        self.assertEqual(shown, {"Super Barbarian", "Super Archer", "Super Giant", "Sneaky Goblin",
                                 "Super Wall Breaker", "Rocket Balloon", "Super Wizard", "Inferno Dragon",
                                 "Super Minion", "Super Valkyrie", "Super Bowler"})
        self.assertEqual(cc_level_cap(P, ugid("Freeze"), 2), 0)  # 冰冻 1 级要 7 级实验室
        self.assertGreater(cc_level_cap(P, ugid("Freeze"), 3), 0)
        # 超级兵门槛：原兵种上限 >= MinOriginalLevel + 1（隐秘哥布林要 7 级哥布林）
        first = lambda n: next(c for c in range(1, 15) if cc_level_cap(P, ugid(n), c) > 0)
        self.assertEqual(cc_level_cap(P, ugid("Goblin"), first("Sneaky Goblin")), 7)
        self.assertLess(cc_level_cap(P, ugid("Goblin"), first("Sneaky Goblin") - 1), 7)

    def test_defense_cc_uses_cap(self):
        from tianze_coc.army import capacity
        from tianze_coc.assets.progression import cc_level_cap

        a = blank(10)
        a.max_out()
        self.assertEqual(capacity(a).cc_level, 6)
        self.assertEqual(L.validate_defense_cc([(ugid("Dragon"), 1, 7)], a), [])
        self.assertTrue(L.validate_defense_cc([(ugid("Dragon"), 1, 8)], a))
        cc = L.random_defense_cc(a, random.Random(1))
        self.assertTrue(cc and all(lv == cc_level_cap(P, g, 6) for g, _n, lv in cc))


class BattleModifierTest(unittest.TestCase):
    def test_set_get_on_synthetic_payload(self):
        from tianze_coc import battle_modifier as bm

        p = b"\x01\x02\x03" + (0).to_bytes(4, "big")
        self.assertEqual(bm.mode_of(p), "normal")
        for m in bm.ORDER:
            q = bm.set_modifier(p, m)
            self.assertEqual(bm.mode_of(q), m)
            self.assertEqual(len(q), len(p))
        with self.assertRaises(ValueError):
            bm.set_modifier(b"\x00" + (12345678).to_bytes(4, "big"), "esports")  # 末尾不是属性调整字段

    def test_table_in_progression(self):
        t = P.data["battle_modifiers"]
        hard = t["Hard Mode"]
        self.assertEqual((hard["common_equipment_minus"], hard["epic_equipment_minus"]), (3, 6))
        att = [e for e in t["Minus One"]["effects"] if e["side"] == "attacker" and e["target"] == "hero"][0]
        self.assertEqual((att["damage_pct"], att["hp_pct"]), (80, 80))
        # 19.32 难度上调（真机测得进攻方英雄生命上限比例 0.90/0.85/0.80 与表一致）
        hard_att = [e for e in hard["effects"] if e["side"] == "attacker" and e["target"] == "hero"][0]
        hard_bld = [e for e in hard["effects"] if e["side"] == "defender" and e["target"] == "building"][0]
        self.assertEqual((hard_att["hp_pct"], hard_bld["damage_pct"]), (75, 130))
        m3 = [e for e in t["Minus Three"]["effects"] if e["side"] == "attacker" and e["target"] == "hero"][0]
        self.assertEqual(m3["hp_pct"], 90)

    @unittest.skipUnless(ENEMY.exists(), "没有本地录制")
    def test_recorded_friendly_mode(self):
        from tianze_coc import battle_modifier as bm

        b = ENEMY.read_bytes()
        self.assertEqual(bm.mode_of(b), "esports")  # 19.32 录制的是选了"电竞"的友谊战
        self.assertEqual(bm.mode_of(bm.set_modifier(b, "normal")), "normal")


class VillageExportTest(unittest.TestCase):
    """游戏"导出村庄数据"JSON <-> Account（摆阵、配兵模型的输入格式）。"""

    @staticmethod
    def key(acc):
        return sorted((s.gid, s.level, s.supercharge, bool(s.raw.get("construction"))) for s in acc.structures)

    def test_random_accounts_roundtrip(self):
        from tianze_coc import village_export as ve

        for th in (2, 9, 14, 18, 19):
            a = blank(th)
            a.randomize(th, random.Random(th))
            d = ve.from_account(a)
            self.assertTrue(all("lvl" in e and "cnt" in e for e in d["buildings"] if "types" not in e))
            b = ve.to_account(d)
            self.assertEqual(self.key(b), self.key(a), th)
            self.assertEqual({g: v for g, v in b.unit_levels.items() if v}, {g: v for g, v in a.unit_levels.items() if v})
            self.assertEqual(b.validate(), [], th)

    @unittest.skipUnless((REC / "export_sample.json").exists(), "没有本地导出样例（含玩家标签，不进仓库）")
    def test_game_export_sample(self):
        from tianze_coc import village_export as ve

        a = ve.to_account(REC / "export_sample.json")
        self.assertEqual(a.th, 19)
        self.assertEqual(a.validate(), [])  # 建造中的烟花炮手塔已消耗箭塔和投石炮，必须计入
        self.assertEqual(sum(1 for s in a.structures if s.supercharge), 2)
        self.assertEqual(sum(1 for s in a.structures if s.gid == gid("Wall")), 350)
        b = ve.to_account(ve.from_account(a))
        self.assertEqual(self.key(b), self.key(a))
