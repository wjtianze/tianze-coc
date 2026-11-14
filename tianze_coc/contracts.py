"""环境与探针之间的数据契约（版本 V1）。坐标统一使用"格子"浮点数，换算到引擎逻辑单位由环境负责。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LOGIC_UNITS_PER_TILE = 1 << 9  # 来自 DebugUtil::deployAllTroops：逻辑坐标 = 格子 << 9

# troop/siege/spell/hero：部署；cc：部落援军（传送门）；cc_siege：部落城堡中的攻城机器；
# ability：英雄/兵种技能或攻城机器释放（只需 unit）；recalled：回溯法术收回后重新部署（flag/extra 透传）
# hero_mode：英雄模式切换（大守护者 flag=0 地面 / 1 空中），只能在该英雄部署前使用
ActionKind = Literal["troop", "siege", "spell", "hero", "cc", "cc_siege", "cc_spell", "ability", "recalled", "hero_mode"]


@dataclass(frozen=True)
class ActionV1:
    """一次部署。unit 为 GlobalID（见 data/unit_catalog.json），x/y 为格子坐标。"""

    kind: ActionKind
    unit: int = 0  # cc 不需要
    x: float = 0.0  # ability 不需要
    y: float = 0.0
    flag: int = 0
    extra: int = 0  # cc_siege：城堡中第几台攻城机器

    def to_wire(self, action_id: int) -> dict[str, Any]:
        return {
            "id": action_id,
            "kind": self.kind,
            "data": self.unit,
            "x": int(round(self.x * LOGIC_UNITS_PER_TILE)),
            "y": int(round(self.y * LOGIC_UNITS_PER_TILE)),
            "flag": self.flag,
            "extra": self.extra,
        }


@dataclass
class ActionResultV1:
    id: int
    status: str  # submitted / executed / rejected
    code: int = 0
    reason: str = ""
    tick: int = -1

    @classmethod
    def from_wire(cls, d: dict) -> "ActionResultV1":
        return cls(int(d.get("id", 0)), str(d.get("status", "")), int(d.get("code", 0)), str(d.get("reason", "")),
                   int(d.get("tick", -1)))


@dataclass
class EntityV1:
    object_type: int  # GameObjectManager 中的类型分组序号（0..10）
    global_id: int  # 对象全局 ID
    data_id: int  # 对应 CSV 数据的 GlobalID（建筑/兵种等）
    fields: dict[str, int | None] = field(default_factory=dict)  # 校准后的附加字段（坐标、血量等）


@dataclass
class ObservationV1:
    tick: int
    in_battle: bool
    battle_over: bool
    destruction: int  # 0..100
    destructible_total: int
    destructible_remaining: int
    width_tiles: float
    height_tiles: float
    army_units: dict[int, int]
    army_spells: dict[int, int]
    entities: list[EntityV1]
    gm_state: int = -1
    stars: int = 0
    th_destroyed: bool = False
    army_sieges: dict[int, int] = field(default_factory=dict)
    army_type2: dict[int, int] = field(default_factory=dict)
    cc_units: dict[int, int] = field(default_factory=dict)  # 部落援军（兵种/法术/攻城机器）：GlobalID -> 数量
    cc_levels: dict[int, int] = field(default_factory=dict)  # 部落援军：GlobalID -> 等级
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_wire(cls, d: dict) -> "ObservationV1":
        army = d.get("army") or {}
        ents = [EntityV1(int(o.get("t", -1)), int(o.get("gid", -1)), int(o.get("data", -1)),
                         {k: v for k, v in o.items() if k not in ("t", "gid", "data", "ptr", "vt")})
                for o in d.get("objects") or []]
        return cls(
            tick=int(d.get("tick", -1)),
            in_battle=bool(d.get("in_battle", False)),
            battle_over=bool(d.get("battle_over", False)),
            destruction=int(d.get("destruction", 0)),
            destructible_total=int(d.get("destructible_total", 0)),
            destructible_remaining=int(d.get("destructible_remaining", 0)),
            width_tiles=d.get("width", 0) / LOGIC_UNITS_PER_TILE,
            height_tiles=d.get("height", 0) / LOGIC_UNITS_PER_TILE,
            army_units={int(g): int(c) for g, c in army.get("units", [])},
            army_spells={int(g): int(c) for g, c in army.get("spells", [])},
            entities=ents,
            gm_state=int(d.get("gm_state", -1)),
            stars=int(d.get("stars", 0)),
            th_destroyed=bool(d.get("th_destroyed", False)),
            army_sieges={int(g): int(c) for g, c in army.get("sieges", [])},
            army_type2={int(g): int(c) for g, c in army.get("type2", [])},
            cc_units={int(e[0]): int(e[1]) for e in army.get("cc", [])},
            cc_levels={int(e[0]): int(e[2]) for e in army.get("cc", []) if len(e) > 2},
            raw=d,
        )
