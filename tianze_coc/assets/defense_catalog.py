"""防守方目录：阵型里可能出现的一切（建筑、城墙、陷阱、大本营武器、守护者、季节性防御、
防守英雄、部落援军），并标注每项的可用限制（大本营等级区间、数量上限、等级上限）。

可用区间来自 townhall_levels.csv：每列是一种建筑/陷阱，每行是一个大本营等级，单元格是可建数量。
数量 > 0 的大本营等级即为可用区间。等级上限来自 buildings.csv / traps.csv 中每一级的 TownHallLevel。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .csv_codec import AssetSource, CsvRow, CsvTable, load_localization
from .unit_catalog import ALLOWED_STATUSES, UnitCatalog, build_catalog


@dataclass
class DefenseEntry:
    category: str  # building / wall / trap / th_weapon / spell_tower_mode / th_variant / guardian / seasonal / hero / cc_troop / other
    name: str
    name_cn: str
    th_min: int | None = None
    th_max: int | None = None
    max_count: dict[int, int] = field(default_factory=dict)  # 大本营等级 -> 数量上限（只记录变化点）
    max_level: int = 0
    level_th: dict[int, int] = field(default_factory=dict)  # 等级 -> 所需大本营（只记录变化点）
    note: str = ""
    status: str = "allowed"  # allowed / excluded / review


def _th_columns(th: CsvTable) -> dict[str, dict[int, int]]:
    """列名 -> {大本营等级: 数量}。"""
    # 空单元格表示沿用上一级的值（Supercell CSV 的惯例），不是 0。
    out: dict[str, dict[int, int]] = {}
    prev: dict[str, int] = {}
    rows = sorted((r for r in th.rows if r.name.strip().isdigit()), key=lambda r: int(r.name))
    for row in rows:
        lvl = int(row.name)
        for k, v in row.fields.items():
            if k == "Name":
                continue
            v = v.strip()
            if v == "":
                val = prev.get(k, 0)
            else:
                try:
                    val = int(v)
                except ValueError:
                    continue
            prev[k] = val
            out.setdefault(k, {})[lvl] = val
    return out


def _range(counts: dict[int, int]) -> tuple[int | None, int | None, dict[int, int]]:
    lv = sorted(l for l, c in counts.items() if c > 0)
    if not lv:
        return None, None, {}
    changes, prev = {}, None
    for l in sorted(counts):
        c = counts[l]
        if c != prev and l >= lv[0]:
            changes[l] = c
        prev = c
    return lv[0], lv[-1], changes


def _level_th(row: CsvRow, key: str = "TownHallLevel") -> dict[int, int]:
    out, prev = {}, None
    for i, lv in enumerate(row.levels, 1):
        try:
            t = int(lv.get(key, "") or 0)
        except ValueError:
            t = 0
        if t != prev:
            out[i] = t
        prev = t
    return out


def fmt_range(e: DefenseEntry) -> str:
    if e.th_min is None:
        return "—"
    return f"{e.th_min}" if e.th_min == e.th_max else f"{e.th_min}~{e.th_max}"


def build_defense_catalog(src: AssetSource, lang: str = "cn") -> dict:
    loc = load_localization(src, lang)
    th_table = src.table("assets/logic/townhall_levels.csv")
    th_max_level = max(int(r.name) for r in th_table.rows if r.name.isdigit())
    cols = _th_columns(th_table)
    out: list[DefenseEntry] = []

    def tid(row: CsvRow) -> str:
        return loc.get(row.get("TID"), "")

    chars_tid = {r.name: r.get("TID") for r in src.table("assets/logic/characters.csv").rows}

    # ---- 合并规则（MergeRequirement，格式 "建筑:等级:改装状态;..."）
    bld_table = src.table("assets/logic/buildings.csv")
    bld = bld_table.by_name()
    consumed_at_th: dict[str, list[int]] = {}  # 被大本营升级消耗的建筑 -> [大本营等级...]
    merged_from: dict[str, str] = {}
    for row in bld_table.rows:
        for i, lv in enumerate(row.levels, 1):
            req = lv.get("MergeRequirement", "")
            if not req or (i > 1 and req == row.levels[i - 2].get("MergeRequirement", "")):
                continue
            parts = [x.split(":") for x in req.split(";") if x]
            if row.name == "Town Hall":
                for p in parts:
                    consumed_at_th.setdefault(p[0], []).append(i)
            elif i == 1:
                desc = []
                for p in parts:
                    cn = loc.get(bld[p[0]].get("TID"), p[0]) if p[0] in bld else p[0]
                    desc.append(f"{p[1]}级{cn}" + ("（已改装）" if len(p) > 2 and p[2] == "1" else ""))
                merged_from[row.name] = "由 " + " + ".join(desc) + " 合并而成"

    # ---- 建筑与城墙
    for row in bld_table.rows:
        if row.get("VillageType") not in ("", "0"):
            continue
        cls = row.get("BuildingClass")
        counts = cols.get(row.name)
        if row.name == "Town Hall":
            counts = {l: 1 for l in range(1, th_max_level + 1)}
        counts = dict(counts or {})
        for th_lv in consumed_at_th.get(row.name, []):
            # 大本营升到 th_lv 时消耗一座；从该级起数量减一
            for l in list(counts):
                if l >= th_lv:
                    counts[l] = max(0, counts[l] - 1)
        lo, hi, chg = _range(counts)
        e = DefenseEntry("wall" if cls == "Wall" else "building", row.name, tid(row), lo, hi, chg, len(row.levels),
                         _level_th(row))
        first_lv_th = e.level_th.get(1, 0)
        if e.th_min is not None and first_lv_th > e.th_min:
            e.th_min = first_lv_th  # 例如部落城堡：低本时只是废墟，1 级需要 3 本
        if row.name in consumed_at_th:
            e.note = f"大本营升到 {min(consumed_at_th[row.name])} 级时被合并消耗"
        if row.name in merged_from:
            e.note = merged_from[row.name]
        if row.flag("EnabledByCalendar"):
            e.status, e.note = "excluded", "EnabledByCalendar=TRUE（限时活动建筑）"
        elif cls in ("Npc", "Npc Town Hall"):
            e.status, e.note = "excluded", "单人关卡（哥布林地图）专用"
        elif row.name.startswith(("Unused", "Town Hall 1")) and cls != "Town Hall":
            e.status, e.note = "excluded", "内部占位/展示用"
        elif cls == "NonFunctional" and not counts:
            e.status, e.note = "excluded", "旧版英雄祭坛（已由英雄大厅取代），大本营等级表中无建造数量"
        elif lo is None:
            e.status, e.note = "excluded", "大本营等级表中任何等级的建造数量都是 0"
        if row.get("GearUpBuilding") and cls != "Npc":
            # 夜世界条件默认已满足；唯一门槛是主世界建筑等级：第一个改装费用 > 0 的等级。
            gate = next((i for i, lv in enumerate(row.levels, 1) if int(lv.get("GearUpCost") or 0) > 0), None)
            g = cols.get(row.name + "_gearup", {})
            gmax = max(g.values()) if g else 0
            alt = loc.get(row.get("AlternateModeTID"), "") or "改装模式"
            if gate:
                need_th = int(row.levels[gate - 1].get("TownHallLevel") or 0)
                e.note = (e.note + "；" if e.note else "") + (
                    f"可改装为{alt}：需本建筑 {gate} 级（{need_th} 本可升到该级），最多改装 {gmax} 座；夜世界条件视为已满足")
        if row.name == "Builders Hut":
            e.note = (e.note + "；" if e.note else "") + "高等级建筑工人小屋会出战（防守建筑工人）"
        if row.name == "Crafting Station":
            e.note = "放置当季精工防御（见“精工防御”）"
        if row.name == "Guardian Building":
            e.note = "承载守卫（见“守卫”）；19 本起大本营本身不再住守卫"
        if row.name == "Squad Barracks":
            e.note = "生产小队（进攻方单位，见单位表）；防守时只是普通建筑"
        out.append(e)

    # ---- 陷阱
    for row in src.table("assets/logic/traps.csv").rows:
        if row.get("VillageType") not in ("", "0"):
            continue
        counts = cols.get(row.name, {})
        lo, hi, chg = _range(counts)
        e = DefenseEntry("trap", row.name, tid(row), lo, hi, chg, len(row.levels), _level_th(row))
        if lo is not None and e.level_th.get(1, 0) > lo:
            e.th_min = e.level_th[1]
        if row.flag("EnabledByCalendar"):
            e.status, e.note = "excluded", "EnabledByCalendar=TRUE（限时活动陷阱）"
        elif row.flag("Disabled"):
            e.status, e.note = "excluded", "Disabled=TRUE"
        elif "SinglePlayer" in row.name:
            e.status, e.note = "excluded", "单人关卡专用"
        elif lo is None:
            e.status, e.note = "excluded", "大本营等级表中无建造数量（活动/废弃陷阱）"
        out.append(e)

    # ---- 大本营武器 / 法术塔模式
    for row in src.table("assets/logic/weapons.csv").rows:
        name_cn = loc.get(row.get("TID"), "")
        if row.name.startswith("Townhall") and row.name[8:].isdigit():
            th = int(row.name[8:])
            dps = [lv.get("DPS", "") for lv in row.levels]
            rng = row.get("AttackRange")
            e = DefenseEntry("th_weapon", row.name, name_cn, th, th, {th: 1}, len(row.levels), {},
                             f"{th} 本大本营自带武器，共 {len(row.levels)} 级；DPS {'/'.join(dps)}，射程 {int(rng) / 100:g} 格")
            th_lv = bld["Town Hall"].levels[th - 1] if th <= len(bld["Town Hall"].levels) else {}
            if th_lv.get("HousesGuardians", "").upper() == "TRUE":
                e.status, e.note = "excluded", f"{th} 本没有大本营武器，改为守卫（见“守卫”）；该行数据未使用"
            out.append(e)
        elif row.name.startswith("SpellTower"):
            out.append(DefenseEntry("spell_tower_mode", row.name, name_cn, *_range(cols.get("Spell Tower", {})),
                                    max_level=len(row.levels), note="法术塔可切换的法术模式"))
        elif row.name.startswith("Goblin") or row.name == "Unused":
            out.append(DefenseEntry("th_weapon", row.name, name_cn, status="excluded", note="单人关卡/未使用"))

    # ---- 守护者
    if src.exists("assets/logic/guardians.csv"):
        for row in src.table("assets/logic/guardians.csv").rows:
            e = DefenseEntry("guardian", row.name, loc.get(row.get("TID"), ""), max_level=len(row.levels),
                             note=f"守护单位：{row.get('CharacterDatas')}")
            th_rows = bld["Town Hall"].levels
            g_th = {i for i, lv in enumerate(th_rows, 1) if lv.get("HousesGuardians", "").upper() == "TRUE"}
            # 19.32 起 19 本的大本营不再住守卫（HousesGuardians=FALSE），改由新建筑"守卫神殿"（Guardian Building）承载
            gb = {l for l, c in cols.get("Guardian Building", {}).items() if c > 0}
            if row.flag("Deprecated"):
                e.status, e.note = "excluded", "Deprecated=TRUE（未上线/已移除）"
            else:
                g_all = g_th | gb
                e.th_min, e.th_max = (min(g_all), max(g_all)) if g_all else (None, None)
                where = []
                if g_th:
                    where.append("大本营 HousesGuardians=TRUE 的等级（" + "、".join(f"{x}本" for x in sorted(g_th)) + "）由大本营承载")
                if gb:
                    where.append("、".join(f"{x}本" for x in sorted(gb)) + "由守卫神殿承载")
                e.note += "；" + "；".join(where) + "；防守时从可用守卫中选 1 个出战"
            out.append(e)

    # ---- 大本营模式（19.32 新增，building_variants.csv）：野蛮人/弓箭手/法师大本营，三选一
    if src.exists("assets/logic/building_variants.csv"):
        for row in src.table("assets/logic/building_variants.csv").rows:
            target = row.get("Building")
            # MinBuildingLevel/MaxBuildingLevel 是 0 起的建筑等级序号（18 = 19 级大本营）
            lo_b, hi_b = row.int("MinBuildingLevel") + 1, row.int("MaxBuildingLevel") + 1
            e = DefenseEntry("th_variant", row.name, loc.get(row.get("TID"), ""), lo_b, hi_b, {lo_b: 1},
                             max_level=len(row.levels))
            need = row.get("RequiredCharacter")
            req = (f"需要本账号 {loc.get(chars_tid.get(need, ''), need)}（{need}）达到 {row.int('RequiredCharacterLevel')} 级才能切换"
                   if need else "默认模式，无解锁条件")
            abil = [lv.get("SpecialAbilities") for lv in row.levels if lv.get("SpecialAbilities")]
            e.note = f"{target} {lo_b} 级的可切换模式，防守方三选一；{req}；能力：{'、'.join(abil)}"
            if row.flag("Deprecated"):
                e.status, e.note = "excluded", "Deprecated=TRUE"
            out.append(e)

    # ---- 季节性防御（工艺站）
    if src.exists("assets/logic/seasonal_defense_archetypes.csv"):
        cs = cols.get("Crafting Station", {})
        lo, hi, _ = _range(cs)
        abil = src.table("assets/logic/special_abilities.csv").by_name()
        season_of: dict[str, list[str]] = {}
        for srow in src.table("assets/logic/seasonal_defense.csv").rows:
            for lv in srow.levels:  # 同一赛季的三个选项分在连续三行
                for arch in lv.get("Archetypes", "").split(";"):
                    if arch.strip():
                        season_of.setdefault(arch.strip(), []).append(srow.name)
        seasons_order = [r.name for r in src.table("assets/logic/seasonal_defense.csv").rows]
        latest = seasons_order[-1] if seasons_order else ""
        def snake(n: str) -> str:
            n = n.replace(" ", "")
            n = n[2:] if n.startswith("SD") else n
            return re.sub(r"(?<!^)(?=[A-Z])", "_", n).upper()
        for row in src.table("assets/logic/seasonal_defense_archetypes.csv").rows:
            name_cn = loc.get("TID_BUILDING_" + snake(row.name), "")
            if not name_cn:
                mods = row.get("Modules").split(";")
                hp = next((m for m in mods if m.endswith("HPModule")), "")
                for pre in ("TID_SEAONAL_DEFENSE_", "TID_SEASONAL_DEFENSE_"):
                    t = loc.get(pre + snake(hp.replace("HPModule", "")) + "_MODULE_HP", "")
                    if t:
                        name_cn = t.replace("生命值", "")
                        break
            seasons = season_of.get(row.name, [])
            e = DefenseEntry("seasonal", row.name, name_cn, lo, hi, {lo: 1} if lo else {})
            if latest in seasons:
                e.note = f"最新赛季 {latest} 的精工防御，防守方三选一（每个阵型 1 座精制台）"
            elif seasons:
                e.status, e.note = "excluded", f"属于旧赛季 {','.join(seasons)}，模拟器只用最新赛季 {latest}"
            else:
                e.status, e.note = "excluded", "不在任何赛季配置中（未上线）"
            out.append(e)

    # ---- 防守英雄
    th_scale = {int(r.name): int(r.get("ScaleByTHPercent") or 100) for r in th_table.rows if r.name.isdigit()}
    for row in src.table("assets/logic/heroes.csv").rows:
        if row.get("VillageType") not in ("", "0"):
            continue
        lt = _level_th(row, "RequiredTownHallLevel") if "RequiredTownHallLevel" in row.fields else {}
        lo = min((t for t in lt.values() if t), default=None)
        out.append(DefenseEntry("hero", row.name, loc.get(row.get("TID"), ""), lo, th_max_level if lo else None,
                                max_level=len(row.levels), level_th=lt,
                                note="英雄大厅中的防守英雄（携带装备，但战宠不参与防守）" + (
                                    "；属性按大本营缩放（ScaleByTH）：" + " ".join(
                                        f"{t}本{p}%" for t, p in th_scale.items() if p < 100 and (lo or 0) <= t)
                                    + "，6 本起 100%" if row.flag("ScaleByTH") else "")))

    # ---- 部落援军（防守）：可用兵种中允许捐赠的；攻城机器与法术不参与防守
    units = build_catalog(src, lang)
    cat = UnitCatalog(units)
    chars = src.table("assets/logic/characters.csv").by_name()
    for u in cat.units:
        if u.status not in ALLOWED_STATUSES or u.kind != "troop":
            continue
        row = chars.get(u.name)
        if row is not None and row.flag("DisableDonate"):
            continue
        out.append(DefenseEntry("cc_troop", u.name, u.name_cn, note="部落城堡援军（超级兵）" if u.status == "super_troop"
                                else "部落城堡援军"))

    summary: dict[str, int] = {}
    for e in out:
        key = f"{e.category}:{e.status}"
        summary[key] = summary.get(key, 0) + 1
    return {"schema": "tianze-coc/defense-catalog/v1", "build_tag": src.build_tag(), "th_max": th_max_level,
            "summary": summary, "entries": [asdict(e) for e in out]}


CATEGORY_CN = {"building": "建筑", "wall": "城墙", "trap": "陷阱", "th_weapon": "大本营武器",
               "spell_tower_mode": "法术塔模式", "th_variant": "大本营模式", "guardian": "守卫", "seasonal": "精工防御（季节性防御）",
               "hero": "防守英雄", "cc_troop": "部落援军"}


def to_markdown(cat: dict) -> str:
    L = [f"# 防守方角色表（版本 {cat['build_tag']}，最高 {cat['th_max']} 本）", "",
         "由 `python -m tianze_coc.assets.defense_catalog` 从 APK 的 CSV 自动生成。",
         "“大本营”列为可建造的大本营等级区间；“数量”列格式为 `本数:数量`，只列出变化点；"
         "“等级”列为最高等级，括号内为 `等级:所需大本营` 的变化点。", ""]
    for cat_key, title in CATEGORY_CN.items():
        rows = [e for e in cat["entries"] if e["category"] == cat_key and e["status"] != "excluded"]
        if not rows:
            continue
        L += [f"## {title}（{len(rows)}）", ""]
        if cat_key == "cc_troop":
            L.append("、".join(e["name_cn"] for e in rows))
            L.append("")
            continue
        L += ["| 中文名 | 内部名 | 大本营 | 数量 | 等级 | 备注 |", "| --- | --- | --- | --- | --- | --- |"]
        for e in rows:
            cnt = " ".join(f"{k}:{v}" for k, v in e["max_count"].items())
            lv = str(e["max_level"]) + (" (" + " ".join(f"{k}:{v}" for k, v in e["level_th"].items() if v) + ")"
                                        if e["level_th"] else "")
            rng = "—" if e["th_min"] is None else (f"{e['th_min']}" if e["th_min"] == e["th_max"]
                                                  else f"{e['th_min']}~{e['th_max']}")
            mark = "【待确认】" if e["status"] == "review" else ""
            L.append(f"| {e['name_cn']} | {e['name']} | {rng} | {cnt} | {lv} | {mark}{e['note']} |")
        L.append("")
    ex = [e for e in cat["entries"] if e["status"] == "excluded"]
    L += [f"## 排除项（{len(ex)}）", "", "| 类别 | 中文名 | 内部名 | 原因 |", "| --- | --- | --- | --- |"]
    for e in ex:
        L.append(f"| {CATEGORY_CN.get(e['category'], e['category'])} | {e['name_cn']} | {e['name']} | {e['note']} |")
    return "\n".join(L) + "\n"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="从 APK 生成防守方角色表")
    ap.add_argument("source")
    ap.add_argument("-o", "--output", default=str(Path(__file__).resolve().parent.parent / "data" / "defense_catalog.json"))
    ap.add_argument("--markdown")
    a = ap.parse_args()
    cat = build_defense_catalog(AssetSource(a.source))
    Path(a.output).write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {a.output}: {cat['summary']}")
    if a.markdown:
        Path(a.markdown).write_text(to_markdown(cat), encoding="utf-8")
        print(f"写入 {a.markdown}")


if __name__ == "__main__":
    main()
