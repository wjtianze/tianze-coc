"""模型输入特征表：用统一的"属性槽位 + 机制标签"描述每一种单位、建筑、陷阱的每一级。

三个模型共用（docs/model_plan.md 第 2、9 节）：模型主要靠属性理解单位，编号嵌入只作补充，
因此数值调整、新单位、新建筑只需重新生成本表，不用改网络结构。

- 属性槽位（NUMERIC）：不同 CSV 的列名不同，按"含义"映射到同一槽位；数值取 log1p 后再按全表最大值归一化到 0~1。
- 机制标签（TAGS）：布尔特征；末尾预留 SPARE_TAGS 个空位，新机制占用空位而不改变向量长度。
- 种类（KINDS）：独热编码。

生成：python -m tianze_coc.features <APK>   ->  data/features.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .assets.csv_codec import AssetSource, CsvRow
from .assets.progression import Progression

SCHEMA_VERSION = 1
DATA_PATH = Path(__file__).resolve().parent / "data" / "features.json"

KINDS = ("troop", "siege", "spell", "hero", "pet", "equipment",
         "defense", "resource", "army_building", "town_hall", "wall", "trap", "other_building")

# 统一属性槽位 -> 各表中可能的列名（取第一个存在且非空的）
NUMERIC: dict[str, tuple[str, ...]] = {
    "hitpoints": ("Hitpoints", "HitPoints"),
    "dps": ("DPS",),
    "damage": ("Damage", "DieDamage"),
    "attack_range": ("AttackRange",),
    "min_attack_range": ("MinAttackRange",),
    "attack_speed": ("AttackSpeed",),
    "speed": ("Speed",),
    "housing": ("HousingSpace",),
    "damage_radius": ("DamageRadius", "Radius"),
    "width": ("Width",),
    "height": ("Height",),
    "trigger_radius": ("TriggerRadius",),
    "num_hits": ("NumberOfHits", "HitCnt", "AttackCount"),
    "duration_ms": ("BoostTimeMS", "DurationMS", "FreezeTimeMS", "InvisibilityTime"),
    "speed_boost": ("SpeedBoost",),
    "damage_boost_pct": ("DamageBoostPercent", "DamageMultiplierPercent"),
    "heal": ("ExtraHealthPermil", "HealOnActivation"),
    "poison_dps": ("PoisonDPS",),
    "secondary_count": ("SecondaryTroopCnt", "NumSpawns", "SummonTroopCount", "DefenderCount"),
    "summon_limit": ("SummonLimit", "UnitsToSpawn"),
    "multi_targets": ("NumMultiTargets",),
    "pushback": ("Pushback",),
    "chain_depth": ("ChainAttackDepth",),
    "jump_height": ("JumpHeightPercent",),
    "wall_cost": ("WallMovementCost",),
    "hero_damage_mult": ("HeroDamageMultiplier",),
    "storage": ("MaxStoredGold", "MaxStoredElixir", "MaxStoredDarkElixir"),
    "housing_alt": ("HousingSpaceAlt",),
    "eject": ("EjectHousingLimit",),
    "dps_lv3": ("DPSLv3",),
    "projectile_bounces": ("ProjectileBounces",),
    "invisibility_radius": ("InvisibilityRadius",),
}

# 统一机制标签 -> 判定函数（输入该级的字段字典）
def _b(row: dict, key: str) -> bool:
    return row.get(key, "").strip().upper() == "TRUE"


def _n(row: dict, key: str) -> float:
    try:
        return float(row.get(key, "") or 0)
    except ValueError:
        return 0.0


TAGS: dict[str, object] = {
    "flying": lambda r: _b(r, "IsFlying"),
    "air_targets": lambda r: _b(r, "AirTargets"),
    "ground_targets": lambda r: _b(r, "GroundTargets"),
    "jumper": lambda r: _b(r, "IsJumper"),
    "underground": lambda r: _b(r, "IsUnderground"),
    "splash": lambda r: _n(r, "DamageRadius") > 0,
    "multi_target": lambda r: _b(r, "MultiTargets") or _b(r, "AttackMultipleBuildings"),
    "hidden": lambda r: _b(r, "Hidden") or (r.get("Visible", "").upper() == "FALSE"),
    "targets_walls": lambda r: r.get("PreferedTargetBuildingClass", "") == "Wall" or _b(r, "DestroyWalls"),
    "targets_defenses": lambda r: r.get("PreferedTargetBuildingClass", "") == "Defense",
    "targets_resources": lambda r: r.get("PreferedTargetBuildingClass", "") == "Resource",
    "secondary_troop": lambda r: bool(r.get("SecondaryTroop")),
    "summons": lambda r: bool(r.get("SummonTroop")),
    "die_damage": lambda r: _n(r, "DieDamage") > 0,
    "heals": lambda r: _n(r, "ExtraHealthPermil") > 0 or _n(r, "HealOnActivation") > 0,
    "freezes": lambda r: _n(r, "FreezeTimeMS") > 0,
    "invisibility": lambda r: _n(r, "InvisibilityTime") > 0 or _n(r, "InvisibilityRadius") > 0,
    "boosts_speed": lambda r: _n(r, "SpeedBoost") > 0,
    "boosts_damage": lambda r: _n(r, "DamageBoostPercent") > 0,
    "poison": lambda r: _n(r, "PoisonDPS") > 0,
    "increasing_damage": lambda r: _b(r, "IncreasingDamage"),
    "chain_attack": lambda r: _n(r, "ChainAttackDepth") > 0,
    "penetrating": lambda r: _b(r, "PenetratingProjectile"),
    "alt_mode": lambda r: _b(r, "HasAltMode") or _b(r, "AltAttackMode"),
    "eject": lambda r: _b(r, "EjectVictims"),
    "air_trigger": lambda r: _b(r, "AirTrigger"),
    "ground_trigger": lambda r: _b(r, "GroundTrigger"),
    "spell_immunity_heroes": lambda r: _b(r, "ImmunityHeroes"),
    "reflects_damage": lambda r: _b(r, "ReflectsDamage"),
    "triggers_traps": lambda r: r.get("TriggersTraps", "TRUE").upper() != "FALSE",
}
SPARE_TAGS = 16  # 预留给以后版本的新机制

BUILDING_KIND = {"Defense": "defense", "Resource": "resource", "Army": "army_building", "Town Hall": "town_hall",
                 "Wall": "wall"}


def _row_levels(row: CsvRow) -> list[dict]:
    return row.levels or [row.fields]


def _vec(level: dict) -> tuple[list[float], list[int]]:
    num = []
    for _name, cols in NUMERIC.items():
        v = 0.0
        for c in cols:
            if level.get(c, "") not in ("", None):
                v = _n(level, c)
                break
        num.append(math.log1p(max(v, 0.0)))
    tags = [1 if f(level) else 0 for f in TAGS.values()] + [0] * SPARE_TAGS
    return num, tags


def build(src: AssetSource, prog: Progression) -> dict:
    tables = {
        "characters": src.table("assets/logic/characters.csv").by_name(),
        "spells": src.table("assets/logic/spells.csv").by_name(),
        "heroes": src.table("assets/logic/heroes.csv").by_name(),
        "pets": src.table("assets/logic/pets.csv").by_name(),
        "character_items": src.table("assets/logic/character_items.csv").by_name(),
        "buildings": src.table("assets/logic/buildings.csv").by_name(),
        "traps": src.table("assets/logic/traps.csv").by_name(),
    }
    table_of = {"troop": "characters", "siege": "characters", "spell": "spells", "hero": "heroes", "pet": "pets",
                "equipment": "character_items"}
    entries: dict[str, dict] = {}
    for gid, u in prog.units.items():
        row = tables[table_of[u["kind"]]].get(u["name"])
        if row is None:
            continue
        levels = [_vec(lv) for lv in _row_levels(row)]
        entries[str(gid)] = {"name": u["name"], "kind": u["kind"], "num": [l[0] for l in levels],
                             "tags": [l[1] for l in levels]}
    for d in prog.data["structures"]:
        tbl = tables["traps"] if d["category"] == "trap" else tables["buildings"]
        row = tbl.get(d["name"])
        if row is None:
            continue
        kind = "trap" if d["category"] == "trap" else "wall" if d["category"] == "wall" else \
            BUILDING_KIND.get(row.get("BuildingClass"), "other_building")
        levels = [_vec(lv) for lv in _row_levels(row)]
        entries[str(d["gid"])] = {"name": d["name"], "kind": kind, "num": [l[0] for l in levels],
                                  "tags": [l[1] for l in levels]}
    # 每个属性槽位按全表最大值归一化
    n = len(NUMERIC)
    mx = [1e-9] * n
    for e in entries.values():
        for lv in e["num"]:
            for i, v in enumerate(lv):
                mx[i] = max(mx[i], v)
    for e in entries.values():
        e["num"] = [[round(v / mx[i], 5) for i, v in enumerate(lv)] for lv in e["num"]]
    return {"schema_version": SCHEMA_VERSION, "build_tag": src.build_tag(), "kinds": list(KINDS),
            "numeric": list(NUMERIC), "tags": list(TAGS) + [f"spare_{i}" for i in range(SPARE_TAGS)],
            "log_max": mx, "entries": entries}


class FeatureTable:
    """查询接口：feature(gid, level) -> 定长向量 = 种类独热 + 属性 + 标签 + [等级/最高等级]。"""

    def __init__(self, data: dict):
        self.data = data
        self.kinds = data["kinds"]
        self.entries = {int(k): v for k, v in data["entries"].items()}
        self.dim = len(self.kinds) + len(data["numeric"]) + len(data["tags"]) + 1

    @classmethod
    def load(cls, path: str | Path | None = None) -> "FeatureTable":
        return cls(json.loads(Path(path or DATA_PATH).read_text(encoding="utf-8")))

    def feature(self, gid: int, level: int = 1) -> list[float]:
        e = self.entries.get(gid)
        if e is None:
            return [0.0] * self.dim  # 未知单位：全零（编号嵌入同样为零），模型只能靠其他上下文
        n = len(e["num"])
        i = max(0, min(level, n) - 1)
        onehot = [1.0 if k == e["kind"] else 0.0 for k in self.kinds]
        return onehot + list(e["num"][i]) + [float(t) for t in e["tags"][i]] + [level / max(1, n)]


def main() -> None:
    ap = argparse.ArgumentParser(description="生成模型输入特征表 features.json")
    ap.add_argument("source")
    ap.add_argument("-o", "--output", default=str(DATA_PATH))
    a = ap.parse_args()
    data = build(AssetSource(a.source), Progression.load())
    Path(a.output).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"写入 {a.output}：{len(data['entries'])} 种，每条向量 {FeatureTable(data).dim} 维")


if __name__ == "__main__":
    main()
