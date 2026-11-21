"""游戏"导出村庄数据"JSON 与 Account 的互转：摆阵模型、配兵模型的输入（及随机账号的输出）都用这种格式。

格式（游戏内复制村庄数据得到，等级一律 1 起）：
    {"tag", "timestamp",
     "buildings": [{"data", "lvl", "cnt"?, "supercharge"?, "gear_up"?, "timer"?, "types"?(精制台模块), ...}],
     "traps": [...同上...], "units"/"siege_machines"/"spells"/"heroes"/"pets"/"equipment": [{"data", "lvl", "timer"?}],
     "guardians": [{"data", "lvl"}], "helpers": [...], "building_variants": [大本营模式], "boosts": {...},
     "decos"/"obstacles"/"skins"/"sceneries"/"house_parts"/...（装饰类），以及夜世界的 *2 字段}

约定：
- buildings/traps 里 "cnt" 表示同等级的数量（缺省 1）；"lvl": 0 是正在建造的建筑：它已占地、合并建筑已消耗原料，
  所以作为 1 级建筑进入 Account 并标记 raw["construction"]=True（导出时写回 lvl 0）；精制台等没有 lvl 字段的按 1 级。
- "supercharge" = 超级充能小等级（内部村庄 JSON 的 minilvl），"gear_up" = 已改装（内部 gearup）。
- 导出格式没有坐标：建筑先自动摆到空地上，正式坐标由摆阵模型给出（layout.Layout）。
- 不认识的字段原样保存在 Account.export_extra 里，导出时写回（夜世界、装饰、皮肤等）。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .account import TH_GID, Account
from .assets.progression import Progression

UNIT_LISTS = {"units": ("troop", "squad"), "siege_machines": ("siege",), "spells": ("spell",), "heroes": ("hero",),
              "pets": ("pet",), "equipment": ("equipment",)}
STRUCT_LISTS = ("buildings", "traps")
HANDLED = set(UNIT_LISTS) | set(STRUCT_LISTS) | {"guardians", "building_variants"}
TH_VARIANT_SLOTS = ("0", "ch", "lg", "lb")  # 大本营条目 building_variant 的各阵型槽（真机录制）


def load(src: str | Path | dict) -> dict:
    if isinstance(src, dict):
        return src
    return json.loads(Path(src).read_text(encoding="utf-8"))


def to_account(src: str | Path | dict, prog: Progression | None = None) -> Account:
    """导出 JSON -> Account（建筑自动摆放到空地；单位等级、超级充能、改装、守卫、大本营模式都保留）。"""
    data = load(src)
    prog = prog or Progression.load()
    acc = Account({"buildings": [], "traps": [], "obstacles": []}, {}, prog)
    acc.export_extra = {k: v for k, v in data.items() if k not in HANDLED}
    acc.pending = []  # 正在建造（lvl 0）的建筑
    acc.unknown = []  # 规则表里没有（禁用/夜世界等）的条目
    entries = [(kind, e) for kind in STRUCT_LISTS for e in data.get(kind, [])]
    th = next((e for _, e in entries if e.get("data") == TH_GID), None)
    acc.add_structure(TH_GID, int(th["lvl"]) if th else 1, x=3, y=3)
    # 大建筑先放，城墙最后填缝
    def size(e):
        d = prog.structure(e.get("data"))
        return (d or {}).get("category") == "wall", -((d or {}).get("width", 1) * (d or {}).get("height", 1))
    for kind, e in sorted(entries, key=lambda ke: size(ke[1])):
        gid = e.get("data")
        if gid == TH_GID:
            continue
        d = prog.structure(gid)
        if d is None:
            acc.unknown.append(e)
            continue
        building = "lvl" in e and int(e["lvl"]) <= 0
        if building:
            acc.pending.append(e)
        for _ in range(int(e.get("cnt", 1))):
            s = acc.add_structure(gid, max(1, int(e.get("lvl", 1))))
            if building:
                s.raw["construction"] = True
            s.supercharge = int(e.get("supercharge", 0) or 0)
            for k, v in e.items():
                if k == "gear_up":
                    s.raw["gearup"] = v
                elif k not in ("data", "lvl", "cnt", "supercharge", "construction"):
                    s.raw[k] = v  # timer（升级中）、types（精制台模块）等原样保留
    for key, kinds in UNIT_LISTS.items():
        for e in data.get(key, []):
            u = prog.units.get(e.get("data"))
            if u is not None and u["kind"] in kinds:
                acc.set_unit_level(e["data"], int(e["lvl"]))
            else:
                acc.unknown.append(e)
    # 守卫、大本营模式写进内部村庄 JSON 对应字段
    if data.get("guardians"):
        acc.home["guardian_manager"] = {"m_pGuardians": [{"m_pGuardian": g["data"], "m_pGuardianUpgradeLevel": int(g["lvl"]) - 1}
                                                         for g in data["guardians"]]}
    variants = data.get("building_variants") or []
    if variants:
        th_s = next(s for s in acc.structures if s.gid == TH_GID)
        th_s.raw["building_variant"] = {k: variants[0] for k in TH_VARIANT_SLOTS}
    return acc


def from_account(acc: Account, tag: str = "", timestamp: int = 0) -> dict:
    """Account -> 导出 JSON（按 编号/等级/充能/改装 聚合计数）。"""
    prog = acc.prog
    out: dict = {"tag": tag, "timestamp": timestamp}
    extra = dict(getattr(acc, "export_extra", {}) or {})
    for kind in STRUCT_LISTS:
        groups: Counter = Counter()
        singles = []
        for s in acc.structures:
            if s.kind != kind:
                continue
            special = {k: v for k, v in s.raw.items() if k in ("timer", "types", "helper_recurrent")}
            if special or s.raw.get("construction"):  # 升级中/建造中/精制台等带额外字段的单独列出
                e = {"data": s.gid, **({} if "types" in special else {"lvl": 0 if s.raw.get("construction") else s.level}),
                     **special}
                if s.supercharge:
                    e["supercharge"] = s.supercharge
                if s.raw.get("gearup"):
                    e["gear_up"] = s.raw["gearup"]
                singles.append(e)
            else:
                groups[(s.gid, s.level, s.supercharge, int(s.raw.get("gearup", 0) or 0))] += 1
        entries = singles + [dict({"data": g, "lvl": lv, "cnt": n}, **({"supercharge": sc} if sc else {}),
                                  **({"gear_up": gu} if gu else {}))
                             for (g, lv, sc, gu), n in sorted(groups.items())]
        out[kind] = entries
    unknown = getattr(acc, "unknown", [])
    for key, kinds in UNIT_LISTS.items():
        out[key] = [{"data": g, "lvl": lv} for g, lv in sorted(acc.unit_levels.items())
                    if lv > 0 and g in prog.units and prog.units[g]["kind"] in kinds]
        # 规则表里没有的（禁用装备等）原样写回到同类列表
        out[key] += [e for e in unknown if e.get("data", 0) // 1_000_000 == {"units": 4, "siege_machines": 4, "spells": 26,
                     "heroes": 28, "pets": 73, "equipment": 90}[key] and key == _list_of(e, prog)]
    gm = (acc.home or {}).get("guardian_manager") or {}
    if gm.get("m_pGuardians"):
        out["guardians"] = [{"data": g["m_pGuardian"], "lvl": int(g.get("m_pGuardianUpgradeLevel", 0)) + 1}
                            for g in gm["m_pGuardians"]]
    th_s = next((s for s in acc.structures if s.gid == TH_GID), None)
    bv = (th_s.raw.get("building_variant") or {}) if th_s else {}
    if bv:
        out["building_variants"] = sorted(set(bv.values()))
    for k, v in extra.items():
        out.setdefault(k, v)
    return out


def _list_of(e: dict, prog: Progression) -> str:
    t = e.get("data", 0) // 1_000_000
    return {26: "spells", 28: "heroes", 73: "pets", 90: "equipment"}.get(t, "units")


def summary(acc: Account) -> dict:
    """调试用：大本营、建筑数、充能数、单位种数。"""
    return {"th": acc.th, "structures": len(acc.structures), "supercharged": sum(1 for s in acc.structures if s.supercharge),
            "units": sum(1 for v in acc.unit_levels.values() if v > 0), "pending": len(getattr(acc, "pending", [])),
            "unknown": len(getattr(acc, "unknown", []))}
