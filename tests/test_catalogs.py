"""角色目录规则测试（需要原版 APK；找不到时跳过）。运行：python -m unittest discover tests"""
import os
import unittest
from pathlib import Path

from tianze_coc.assets.csv_codec import AssetSource
from tianze_coc.assets.defense_catalog import build_defense_catalog
from tianze_coc.assets.unit_catalog import UnitCatalog, build_catalog
from tianze_coc.local_config import LocalConfig

CANDIDATES = [os.environ.get("TZ_ORIGINAL_APK", ""), LocalConfig.load().original_apk,
              str(Path.home() / "Downloads" / "com.atrasis.original-18.647.3.apk")]
APK = next((p for p in CANDIDATES if p and Path(p).is_file()), None)


@unittest.skipIf(APK is None, "未找到原版 APK")
class UnitCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = UnitCatalog(build_catalog(AssetSource(APK)))

    def test_super_troops_allowed(self):
        for name in ("Super Barbarian", "Inferno Dragon", "Ice Hound", "Sneaky Goblin"):
            self.assertTrue(self.cat.is_allowed(name), name)

    def test_event_units_banned(self):
        for name in ("Ice Wizard", "Battle Ram", "Barbarian Kicker", "Santas Surprise", "Totem Thrower"):
            u = self.cat.get(name)
            self.assertEqual(u.status, "temporary_event", name)
            self.assertFalse(u.allowed, name)

    def test_permanent_core(self):
        for name in ("Barbarian", "Ruin Witch", "Meteor Golem", "Air Troop Launcher", "AngrySpell", "Dragon Duke"):
            self.assertTrue(self.cat.is_allowed(name), name)

    def test_secondary_units_not_deployable(self):
        for name in ("Golemite", "Lava Pup", "Skeleton"):
            self.assertFalse(self.cat.is_allowed(name), name)

    def test_counts(self):
        by = {}
        for u in self.cat.units:
            if u.allowed:
                by[u.kind] = by.get(u.kind, 0) + 1
        self.assertEqual(by, {"troop": 49, "siege": 9, "spell": 18, "hero": 6, "equipment": 43, "pet": 12})

    def test_king_th_scaling(self):
        self.assertIn("4本50%", self.cat.get("Barbarian King").reason)
        self.assertIn("5本75%", self.cat.get("Barbarian King").reason)


@unittest.skipIf(APK is None, "未找到原版 APK")
class DefenseCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = build_defense_catalog(AssetSource(APK))
        cls.by = {e["name"]: e for e in d["entries"]}

    def test_eagle_artillery_th11_to_16(self):
        e = self.by["Eagle Artillery"]
        self.assertEqual((e["th_min"], e["th_max"]), (11, 16))

    def test_clan_castle_starts_th3(self):
        self.assertEqual(self.by["Clan Castle"]["th_min"], 3)

    def test_seasonal_latest_only(self):
        allowed = {n for n, e in self.by.items() if e["category"] == "seasonal" and e["status"] == "allowed"}
        self.assertEqual(allowed, {"Inferno Candle", "Headhunter Tower", "Cake Thrower"})

    def test_event_traps_excluded(self):
        for n in ("FreezeBomb", "ShrinkTrap", "Halloweenbomb", "SantaTrap"):
            self.assertEqual(self.by[n]["status"], "excluded", n)

    def test_guardians_th18(self):
        for n in ("InfernoArtillery", "MeleeAreaaaa", "Logger"):
            self.assertEqual((self.by[n]["th_min"], self.by[n]["th_max"]), (18, 18), n)

    def test_th_weapons_levels(self):
        for th in range(12, 17):
            self.assertEqual(self.by[f"Townhall{th}"]["max_level"], 1)
        self.assertEqual(self.by["Townhall17"]["max_level"], 5)
        self.assertEqual(self.by["Townhall18"]["status"], "excluded")  # 18 本改为守卫

    def test_gear_up_gate(self):
        self.assertIn("需本建筑 7 级", self.by["Cannon"]["note"])
        self.assertIn("需本建筑 10 级", self.by["Archer Tower"]["note"])
        self.assertIn("需本建筑 8 级", self.by["Mortar"]["note"])


if __name__ == "__main__":
    unittest.main()
