"""根据游戏 CSV 判定每个可部署单位在模拟器中是否可用。

总规则：**部落对战和排位战能带的，模拟器里就能用**。依据是游戏自身的提示文本
（cn.csv）："限时兵种和法术无法在排位战或部落对战中使用"
（TID_ARMY_DISCLAIMER_MISSING_EVENT_TROOPS_SPELLS_INFO_SCREEN），以及对超级兵只要求
"请启用或移除军队配置中的超级兵种"（TID_ARMY_DISCLAIMER_MISSING_SUPER_TROOPS）。

判定只依赖 APK 自带数据，不靠手写名单：

* 永久 (permanent)              : 主村庄、有生产建筑、未被禁用/弃用、所需建筑等级在该建筑等级上限内。 —— 可用
* 超级兵 (super_troop)          : EnabledBySuperLicence=TRUE，在超级兵营激活后可带入部落战/排位战。 —— 可用
* 限时 (temporary_event)        : EnabledByCalendar=TRUE，限时活动兵种/法术。 —— 禁用
* 未上线 (unreleased)           : 所需兵营/工厂等级超过该建筑在本版本中的最高等级。
* 内部 (internal)               : Deprecated / DisableProduction / 无生产建筑（派生单位、陷阱、防守用、原型等）。
* 夜世界 (builder_base)         : VillageType=1。

英雄 (heroes.csv) 与战宠 (pets.csv) 是长期解锁、不可被活动临时授予的单位，归为永久；
其中夜世界英雄 (VillageType=1) 排除。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .csv_codec import AssetSource, CsvRow, load_localization

PERMANENT = "permanent"
TEMP_EVENT = "temporary_event"
SUPER_TROOP = "super_troop"
UNRELEASED = "unreleased"
INTERNAL = "internal"
BUILDER_BASE = "builder_base"

# 模拟器中允许部署的状态（= 部落战/排位战可携带）
ALLOWED_STATUSES = frozenset({PERMANENT, SUPER_TROOP})
TEMPORARY_STATUSES = frozenset({TEMP_EVENT})

# CSV 表序号 -> GlobalID 前缀（GlobalID = 表序号 * 1_000_000 + 行号）
TABLE_CHARACTERS = 4
TABLE_SPELLS = 26
TABLE_HEROES = 28

PRODUCTION_KIND = {
    "Barracks": "troop",
    "Dark Barracks": "troop",
    "Siege Workshop": "siege",
    "Spell Factory": "spell",
    "Dark Spell Factory": "spell",
}

# 生产建筑 -> 该建筑在 buildings.csv 中的名字（法术工厂在 buildings.csv 里叫 Spell Forge / Mini Spell Factory）
BUILDING_FOR_PRODUCTION = {
    "Barracks": ("Barracks",),
    "Dark Barracks": ("Dark Barracks",),
    "Siege Workshop": ("Siege Workshop",),
    "Spell Factory": ("Spell Forge", "Spell Factory"),
    "Dark Spell Factory": ("Mini Spell Factory", "Dark Spell Factory"),
}


@dataclass
class UnitEntry:
    name: str
    global_id: int
    kind: str  # troop / siege / spell / hero / pet
    status: str
    reason: str
    housing_space: int = 0
    max_level: int = 0
    production_building: str = ""
    required_building_level: int = 0
    tid: str = ""
    name_cn: str = ""
    flags: dict[str, bool] = field(default_factory=dict)

    @property
    def permanent(self) -> bool:
        return self.status == PERMANENT

    @property
    def allowed(self) -> bool:
        """部落战/排位战可携带 => 模拟器可用。"""
        return self.status in ALLOWED_STATUSES

    @property
    def deployable_kind(self) -> bool:
        return self.kind in ("troop", "siege", "spell", "hero")


def _global_id(row: CsvRow, table: int, index: int) -> int:
    gid = row.int("GlobalID", -1)
    return gid if gid > 0 else table * 1_000_000 + index


def _building_max_levels(src: AssetSource) -> dict[str, int]:
    out = {}
    for row in src.table("assets/logic/buildings.csv").rows:
        out[row.name] = len(row.levels)
    return out


def _required_level(row: CsvRow, kind: str) -> int:
    key = "SpellForgeLevel" if kind == "spell" else "BarrackLevel"
    return row.int(key, 0)


def classify_combat_item(row: CsvRow, kind_hint: str, building_levels: dict[str, int]) -> tuple[str, str, str]:
    """返回 (kind, status, reason)。"""
    prod = row.get("ProductionBuilding")
    kind = PRODUCTION_KIND.get(prod, kind_hint)
    if row.get("VillageType") not in ("", "0"):
        return kind, BUILDER_BASE, "VillageType=%s（夜世界）" % row.get("VillageType")
    if row.flag("EnabledByCalendar"):
        return kind, TEMP_EVENT, "EnabledByCalendar=TRUE（限时兵种/法术，部落战与排位战不可用）"
    if row.flag("EnabledBySuperLicence"):
        return kind, SUPER_TROOP, "EnabledBySuperLicence=TRUE（超级兵，激活后部落战/排位战可用）"
    if row.flag("Deprecated"):
        return kind, INTERNAL, "Deprecated=TRUE"
    if row.flag("DisableProduction"):
        return kind, INTERNAL, "DisableProduction=TRUE（派生/防守/内部单位）"
    if row.flag("IsSecondaryTroop"):
        return kind, INTERNAL, "IsSecondaryTroop=TRUE（只能由其他单位派生）"
    if prod not in PRODUCTION_KIND:
        return kind, INTERNAL, "没有生产建筑"
    need = _required_level(row, kind)
    names = BUILDING_FOR_PRODUCTION.get(prod, (prod,))
    cap = max((building_levels.get(n, 0) for n in names), default=0)
    if cap and need > cap:
        return kind, UNRELEASED, f"需要 {prod} {need} 级，本版本最高 {cap} 级"
    return kind, PERMANENT, f"{prod} {need} 级可生产"


def build_catalog(src: AssetSource, lang: str = "cn") -> dict:
    loc = load_localization(src, lang)
    building_levels = _building_max_levels(src)
    entries: list[UnitEntry] = []

    for table_id, member, kind_hint in (
        (TABLE_CHARACTERS, "assets/logic/characters.csv", "troop"),
        (TABLE_SPELLS, "assets/logic/spells.csv", "spell"),
    ):
        for i, row in enumerate(src.table(member).rows):
            kind, status, reason = classify_combat_item(row, kind_hint, building_levels)
            entries.append(
                UnitEntry(
                    name=row.name,
                    global_id=_global_id(row, table_id, i),
                    kind=kind,
                    status=status,
                    reason=reason,
                    housing_space=row.int("HousingSpace"),
                    max_level=len(row.levels),
                    production_building=row.get("ProductionBuilding"),
                    required_building_level=_required_level(row, kind),
                    tid=row.get("TID"),
                    name_cn=loc.get(row.get("TID"), ""),
                    flags={k: row.flag(k) for k in ("Deprecated", "DisableProduction", "IsSecondaryTroop",
                                                    "EnabledByCalendar", "EnabledBySuperLicence", "IsFlying")
                           if k in row.fields},
                )
            )

    th_scale = {int(r.name): int(r.get("ScaleByTHPercent") or 100)
                 for r in src.table("assets/logic/townhall_levels.csv").rows if r.name.isdigit()}
    for i, row in enumerate(src.table("assets/logic/heroes.csv").rows):
        vt = row.get("VillageType")
        status, reason = (BUILDER_BASE, "VillageType=1（夜世界英雄）") if vt not in ("", "0") else (
            (TEMP_EVENT, "EnabledByCalendar=TRUE") if row.flag("EnabledByCalendar") else (PERMANENT, "英雄（长期解锁）"))
        if row.flag("ScaleByTH") and status == PERMANENT:
            req = [int(lv.get("RequiredTownHallLevel") or 0) for lv in row.levels]
            first = min((t for t in req if t), default=1)
            scaled = [f"{t}本{p}%" for t, p in th_scale.items() if p < 100 and t >= first]
            reason += "；属性按大本营缩放（ScaleByTH）：" + " ".join(scaled) + "，其后 100%"
        entries.append(UnitEntry(row.name, _global_id(row, TABLE_HEROES, i), "hero", status, reason,
                                 row.int("HousingSpace"), len(row.levels), "", 0, row.get("TID"),
                                 loc.get(row.get("TID"), "")))

    if src.exists("assets/logic/pets.csv"):
        # 战宠在 CSV 中全部 DisableProduction=TRUE（它们不训练），因此用宠物小屋等级判断。
        # pets.csv 没有 GlobalID 列，global_id 记为 -1，运行时由探针按名字解析。
        pets = src.table("assets/logic/pets.csv")
        pet_house_cap = building_levels.get("Pet House", 0)
        seen_tid: set[str] = set()
        for i, row in enumerate(pets.rows):
            gid = 73_000_000 + i  # pets.csv 无 GlobalID 列；真机确认前缀 73（野蛮人之王的莱希 = 73000000）
            need = row.int("LaboratoryLevel", 0)  # 宠物小屋等级存放在 LaboratoryLevel 列
            if row.flag("Deprecated"):
                status, reason = INTERNAL, "Deprecated=TRUE"
            elif row.flag("EnabledByCalendar"):
                status, reason = TEMP_EVENT, "EnabledByCalendar=TRUE"
            elif row.get("TID") in seen_tid:
                status, reason = INTERNAL, "与前一战宠共用 TID，是其派生形态"
            elif pet_house_cap and need > pet_house_cap:
                status, reason = UNRELEASED, f"需要宠物小屋 {need} 级，本版本最高 {pet_house_cap} 级"
            else:
                status, reason = PERMANENT, f"宠物小屋 {need} 级解锁（随英雄出场）"
            if status == PERMANENT:
                seen_tid.add(row.get("TID"))
            entries.append(UnitEntry(row.name, gid, "pet", status, reason, 0, len(row.levels), "", 0,
                                     row.get("TID"), loc.get(row.get("TID"), "")))

    # 英雄装备：未弃用、且归属主村英雄的装备可带入部落战/排位战。
    if src.exists("assets/logic/character_items.csv"):
        home_heroes = {e.name for e in entries if e.kind == "hero" and e.status == PERMANENT}
        seen_tid: set[str] = set()
        for row in src.table("assets/logic/character_items.csv").rows:
            owners = [x for x in row.get("AllowedCharacters").split(";") if x]
            if row.flag("Deprecated"):
                status, reason = INTERNAL, "Deprecated=TRUE"
            elif not owners or not all(o in home_heroes for o in owners):
                status, reason = INTERNAL, f"归属 {owners} 不是主村英雄"
            elif row.get("TID") in seen_tid:
                status, reason = INTERNAL, "与其他装备共用 TID（内部变体）"
            else:
                status, reason = PERMANENT, "英雄装备（" + "、".join(owners) + "）"
            if status == PERMANENT:
                seen_tid.add(row.get("TID"))
            entries.append(UnitEntry(row.name, -1, "equipment", status, reason, 0, len(row.levels), ";".join(owners),
                                     0, row.get("TID"), loc.get(row.get("TID"), ""),
                                     flags={"epic": row.get("Rarity") == "EPIC"}))

    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    return {
        "schema": "tianze-coc/unit-catalog/v1",
        "th_scale_percent": th_scale,
        "build_tag": src.build_tag(),
        "summary": by_status,
        "units": [asdict(e) for e in entries],
    }


class UnitCatalog:
    """运行时查询接口：环境用它拒绝临时角色。"""

    def __init__(self, data: dict):
        self.data = data
        self.units = [UnitEntry(**u) for u in data["units"]]
        self.by_id = {u.global_id: u for u in self.units}
        self.by_name = {u.name: u for u in self.units}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "UnitCatalog":
        path = Path(path) if path else Path(__file__).resolve().parent.parent / "data" / "unit_catalog.json"
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, key: int | str) -> UnitEntry | None:
        return self.by_id.get(key) if isinstance(key, int) else self.by_name.get(key)

    def is_allowed(self, key: int | str) -> bool:
        u = self.get(key)
        return bool(u and u.allowed)

    def allowed(self, kind: str | None = None) -> list[UnitEntry]:
        return [u for u in self.units if u.allowed and (kind is None or u.kind == kind)]

    def allowed_deploy_ids(self) -> list[int]:
        """可直接部署（兵种/攻城机器/法术/英雄）的 GlobalID，下发给探针做二次校验。"""
        return sorted(u.global_id for u in self.units if u.allowed and u.deployable_kind and u.global_id > 0)

    def temporary(self) -> list[UnitEntry]:
        return [u for u in self.units if u.status in TEMPORARY_STATUSES]
