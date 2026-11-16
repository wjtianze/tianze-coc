"""成长规则表：判断一个账号（建筑等级/数量、兵种等级）是否合法所需的全部数据。

从 APK 的 CSV 生成 `data/progression.json`，供修改器（tianze_coc.account）、摆阵（tianze_coc.layout）
和配兵（tianze_coc.army）使用。所有等级在本模块与 JSON 中一律为 **1 起**（游戏内显示的等级）。

规则来源：
- 建筑/陷阱/城墙：每级所需大本营（TownHallLevel），每个大本营等级的数量上限（townhall_levels.csv，
  已扣除升本合并消耗，见 defense_catalog），占地 Width×Height；
  合并建筑（MergeRequirement，如多人箭塔 = 2 座满级箭塔）：原料数 + 份数×成品数 ≤ 原料上限。
- 兵种/攻城机器：第 L 级需要实验室 LaboratoryLevel[L]；解锁需要生产建筑 BarrackLevel。
- 法术：第 L 级需要实验室 LaboratoryLevel[L]；解锁需要法术工厂等级 SpellForgeLevel。
- 英雄：第 L 级需要大本营 RequiredTownHallLevel[L] 且英雄大厅 RequiredHeroTavernLevel[L]。
- 战宠：第 L 级需要战宠小屋 LaboratoryLevel[L]。
- 英雄装备：第 L 级需要铁匠铺 RequiredBlacksmithLevel[L]；只能给 AllowedCharacters 中的英雄使用。
- 升本"先建齐"规则（用户要求，可关闭）：大本营 T 级时，每种普通建筑的数量 ≥ T-1 本的数量上限。
  注意：18.647 的 globals.csv 中 REQUIRE_ALL_BUILDINGS_PLACED_FOR_TOWN_HALL_UPGRADE = FALSE，
  因此这是比游戏本身更严格的规则；城墙、陷阱、合并建筑及其原料不参与此规则。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .csv_codec import AssetSource, CsvRow, load_localization
from .defense_catalog import build_defense_catalog
from .unit_catalog import ALLOWED_STATUSES, build_catalog

SCHEMA = "tianze-coc/progression/v1"
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "progression.json"

TABLE_BUILDINGS = 1
TABLE_TRAPS = 12
TABLE_PETS = 73
TABLE_ITEMS = 90


def _int(v: str, default: int = 0) -> int:
    try:
        return int(str(v).strip() or default)
    except ValueError:
        return default


def _expand(changes: dict, th_max: int) -> list[int]:
    """{等级: 值} 变化点 -> 长度 th_max 的列表（下标 0 = 1 本）。"""
    out, cur = [], 0
    ch = {int(k): v for k, v in changes.items()}
    for t in range(1, th_max + 1):
        cur = ch.get(t, cur)
        out.append(cur)
    return out


def _per_level(row: CsvRow, key: str) -> list[int]:
    return [_int(lv.get(key, "")) for lv in row.levels]


def build_progression(src: AssetSource, lang: str = "cn") -> dict:
    loc = load_localization(src, lang)
    dcat = build_defense_catalog(src, lang)
    th_max = dcat["th_max"]
    bld_rows = src.table("assets/logic/buildings.csv")
    trap_rows = src.table("assets/logic/traps.csv")
    bld = {r.name: (i, r) for i, r in enumerate(bld_rows.rows)}
    trp = {r.name: (i, r) for i, r in enumerate(trap_rows.rows)}

    # ---- 合并规则
    merges: dict[str, dict] = {}
    for name, (_, row) in bld.items():
        req = row.levels[0].get("MergeRequirement", "") if row.levels else ""
        if not req or name == "Town Hall":
            continue
        parts = [p.split(":") for p in req.split(";") if p]
        inputs: dict[str, int] = {}
        levels: dict[str, int] = {}
        geared: dict[str, bool] = {}
        for p in parts:
            inputs[p[0]] = inputs.get(p[0], 0) + 1
            levels[p[0]] = _int(p[1]) if len(p) > 1 else 0
            geared[p[0]] = len(p) > 2 and p[2] == "1"  # 原料须为已改装版本（如复合机械塔 = 改装箭塔 + 改装加农炮）
        merges[name] = {"inputs": inputs, "input_levels": levels, "input_geared": geared}
    merge_related = set(merges) | {i for m in merges.values() for i in m["inputs"]}

    structures = []
    for e in dcat["entries"]:
        if e["category"] not in ("building", "wall", "trap") or e["status"] != "allowed":
            continue
        table = trp if e["category"] == "trap" else bld
        if e["name"] not in table:
            continue
        idx, row = table[e["name"]]
        gid = _int(row.get("GlobalID")) or ((TABLE_TRAPS if e["category"] == "trap" else TABLE_BUILDINGS) * 1_000_000 + idx)
        max_count = _expand(e["max_count"], th_max)
        level_th = _per_level(row, "TownHallLevel")
        if e["name"] == "Town Hall":
            level_th = list(range(1, len(row.levels) + 1))  # 大本营 L 级就是 L 本
        s = {
            "gid": gid, "name": e["name"], "name_cn": e["name_cn"], "category": e["category"],
            "width": _int(row.get("Width"), 1), "height": _int(row.get("Height"), 1),
            "max_level": len(row.levels), "level_th": level_th, "max_count": max_count,
            "hidden": e["category"] == "trap" or row.flag("Hidden"),
        }
        if e["category"] == "building":
            hs = {"troops": _per_level(row, "HousingSpace"), "spells": _per_level(row, "HousingSpaceAlt"),
                  "sieges": _per_level(row, "HousingSpaceSiege")}
            s["housing"] = {k: v for k, v in hs.items() if any(v)}
            s["building_class"] = row.get("BuildingClass")
        if e["name"] in merges:
            s["merge"] = merges[e["name"]]
        # 升本"先建齐"规则适用范围
        s["require_built_on_th_up"] = (e["category"] == "building" and e["name"] not in merge_related
                                       and e["name"] != "Town Hall" and "被合并消耗" not in (e.get("note") or ""))
        structures.append(s)

    # ---- 进攻单位
    ucat = build_catalog(src, lang)
    chars = src.table("assets/logic/characters.csv").by_name()
    spells = src.table("assets/logic/spells.csv").by_name()
    heroes_t = src.table("assets/logic/heroes.csv")
    heroes = heroes_t.by_name()
    pets = src.table("assets/logic/pets.csv").by_name()
    items_t = src.table("assets/logic/character_items.csv")
    items = items_t.by_name()
    item_gid = {r.name: TABLE_ITEMS * 1_000_000 + i for i, r in enumerate(items_t.rows)}
    hero_gid = {r.name: 28_000_000 + i for i, r in enumerate(heroes_t.rows)}
    units = []
    for u in ucat["units"]:
        if u["status"] not in ALLOWED_STATUSES:
            continue
        kind = u["kind"]
        d = {"gid": u["global_id"], "name": u["name"], "name_cn": u["name_cn"], "kind": kind,
             "status": u["status"], "housing": u.get("housing_space", 0), "max_level": u.get("max_level", 0)}
        if kind in ("troop", "siege"):
            row = chars[u["name"]]
            d["levels_req"] = [{"Laboratory": v} for v in _per_level(row, "LaboratoryLevel")]
            d["unlock"] = {u["production_building"]: u["required_building_level"]} if u.get("production_building") else {}
            if u["status"] == "super_troop":
                d["derived_level"] = True  # 超级兵等级随原兵种，不单独升级
        elif kind == "spell":
            row = spells[u["name"]]
            d["levels_req"] = [{"Laboratory": v} for v in _per_level(row, "LaboratoryLevel")]
            d["unlock"] = {u["production_building"]: u["required_building_level"]} if u.get("production_building") else {}
        elif kind == "hero":
            row = heroes[u["name"]]
            d["levels_req"] = [{"Town Hall": t, "Hero Hall": h} for t, h in
                               zip(_per_level(row, "RequiredTownHallLevel"), _per_level(row, "RequiredHeroTavernLevel"))]
            d["item_slots"] = _int(row.get("ItemSlotCount"), 2)
            d["has_alt_mode"] = row.flag("HasAltMode")
        elif kind == "pet":
            row = pets[u["name"]]
            d["levels_req"] = [{"Pet House": v} for v in _per_level(row, "LaboratoryLevel")]
        elif kind == "equipment":
            row = items[u["name"]]
            d["gid"] = item_gid[u["name"]]  # 单位目录里装备没有 GlobalID 列，按行号计算
            allowed = [x.strip() for x in row.get("AllowedCharacters").split(";") if x.strip()]
            d["heroes"] = [hero_gid[h] for h in allowed if h in hero_gid]
            d["rarity"] = row.get("Rarity")
            d["levels_req"] = [{"Blacksmith": b} for b in _per_level(row, "RequiredBlacksmithLevel")]
        else:
            continue
        d["max_level"] = len(d["levels_req"])
        units.append(d)

    # ---- 障碍物占地（阵型控制时避开）
    obstacles = {}
    for i, r in enumerate(src.table("assets/logic/obstacles.csv").rows):
        obstacles[str(8_000_000 + i)] = [_int(r.get("Width"), 1), _int(r.get("Height"), 1)]

    # ---- 容量
    g = {}
    for r in src.table("assets/logic/globals.csv").rows:
        g[r.name] = r.fields
    return {
        "schema": SCHEMA, "build_tag": src.build_tag(), "th_max": th_max,
        "structures": structures, "units": units, "obstacles": obstacles,
        "rules": {
            "hero_slots": 4,  # 进攻时最多 4 名英雄（active_army_loadout 的英雄槽数）
            "max_super_troop_kinds": 2,  # 同时最多激活 2 种超级兵
            "map_tiles": 50,
            "require_all_built_global": g.get("REQUIRE_ALL_BUILDINGS_PLACED_FOR_TOWN_HALL_UPGRADE", {}).get("BooleanValue", ""),
        },
    }


class Progression:
    """progression.json 的查询接口。"""

    def __init__(self, data: dict):
        self.data = data
        self.th_max: int = data["th_max"]
        self.structures = {s["gid"]: s for s in data["structures"]}
        self.structures_by_name = {s["name"]: s for s in data["structures"]}
        self.units = {u["gid"]: u for u in data["units"]}
        self.rules = data["rules"]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Progression":
        return cls(json.loads(Path(path or DATA_PATH).read_text(encoding="utf-8")))

    def structure(self, key: int | str) -> dict | None:
        return self.structures.get(key) if isinstance(key, int) else self.structures_by_name.get(key)

    def max_count(self, key: int | str, th: int) -> int:
        s = self.structure(key)
        return s["max_count"][th - 1] if s and 1 <= th <= self.th_max else 0

    def max_structure_level(self, key: int | str, th: int) -> int:
        """大本营 th 时该建筑能达到的最高等级（0 = 不能建造）。"""
        s = self.structure(key)
        if not s:
            return 0
        return max((i + 1 for i, t in enumerate(s["level_th"]) if t <= th), default=0)

    def max_unit_level(self, gid: int, buildings: dict[str, int], hero_levels: dict[int, int] | None = None) -> int:
        """给定各建筑等级（名称 -> 最高等级，1 起），该单位能达到的最高等级（0 = 未解锁）。"""
        u = self.units.get(gid)
        if not u:
            return 0
        for b, need in (u.get("unlock") or {}).items():
            if buildings.get(b, 0) < need:
                return 0
        best = 0
        for i, req in enumerate(u["levels_req"]):
            if all(buildings.get(b, 0) >= need for b, need in req.items()):
                best = i + 1
            else:
                break
        return best


def main() -> None:
    ap = argparse.ArgumentParser(description="从 APK 生成成长规则表 progression.json")
    ap.add_argument("source", help="APK 文件或已解包目录")
    ap.add_argument("-o", "--output", default=str(DATA_PATH))
    a = ap.parse_args()
    data = build_progression(AssetSource(a.source))
    Path(a.output).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {a.output}：建筑/陷阱 {len(data['structures'])} 种，进攻单位 {len(data['units'])} 种")


if __name__ == "__main__":
    main()
