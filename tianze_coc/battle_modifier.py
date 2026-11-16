"""战斗属性调整（游戏的难度模式）：普通 / 传奇杯 3 / 传奇杯 2 / 传奇杯 1 / 电竞。

数值定义在 assets/logic/battle_modifiers.csv（GlobalID 表 92，每种模式一行起头、后接若干子行）。
EnemyHomeData 负载的**最后 4 字节**是本局属性调整的 GlobalID（int32 大端；0 = 无调整）——
18.647 真机录制对比得出：同一账号的普通友谊战为 0，选"电竞"的友谊战为 92000000。
"""
from __future__ import annotations

import struct

TABLE = 92
MODES = {
    "normal": 0,                 # 普通（无调整）
    "esports": 92_000_000,       # 电竞（Hard Mode）
    "legend1": 92_000_001,       # 传奇杯 1（Minus One）
    "legend2": 92_000_002,       # 传奇杯 2（Minus Two）
    "legend3": 92_000_003,       # 传奇杯 3（Minus Three）
}
MODE_CN = {"normal": "普通", "legend3": "传奇杯3", "legend2": "传奇杯2", "legend1": "传奇杯1", "esports": "电竞"}
# 训练时的模式顺序（难度由低到高）
ORDER = ("normal", "legend3", "legend2", "legend1", "esports")


def gid_of(mode: str | int) -> int:
    if isinstance(mode, int):
        if mode != 0 and mode // 1_000_000 != TABLE:
            raise ValueError(f"不是战斗属性调整的 GlobalID: {mode}")
        return mode
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode}，可选 {list(MODES)}")
    return MODES[mode]


def get_modifier(payload: bytes) -> int:
    v = struct.unpack_from(">i", payload, len(payload) - 4)[0]
    if v != 0 and v // 1_000_000 != TABLE:
        raise ValueError(f"负载末尾不是属性调整字段（读到 {v}）；格式可能已变化")
    return v


def mode_of(payload: bytes) -> str:
    g = get_modifier(payload)
    return next((k for k, v in MODES.items() if v == g), str(g))


def set_modifier(payload: bytes, mode: str | int) -> bytes:
    get_modifier(payload)  # 先确认格式
    return payload[:-4] + struct.pack(">i", gid_of(mode))


def load_table(src) -> dict:
    """从 APK 读出各模式的增益（百分比）。注意：这张表的子行里空格表示"不适用"，
    不能用通用解析器的"空格继承上一行"规则，因此按原始行读取。"""
    import csv
    import io

    rows = list(csv.reader(io.StringIO(src.text("assets/logic/battle_modifiers.csv"))))
    head = rows[0]
    out: dict = {}
    cur = None
    for raw in rows[2:]:
        r = dict(zip(head, raw))
        if r.get("Name"):
            cur = out.setdefault(r["Name"], {"tid": r.get("TID", ""), "effects": [],
                                             "common_equipment_minus": int(r.get("CommonEquipmentMinusLevels") or 0),
                                             "epic_equipment_minus": int(r.get("EpicEquipmentMinusLevels") or 0)})
        if cur is None:
            continue
        t = lambda k: r.get(k, "").upper() == "TRUE"  # noqa: E731
        target = "building" if r.get("BuildingClass") else "hero" if t("Heroes") else "guardian" if t("Guardians") else "other"
        cur["effects"].append({"side": "attacker" if t("Attacker") else "defender", "target": target,
                               "building_class": r.get("BuildingClass", ""), "spells": t("Spells"),
                               "damage_pct": int(r.get("DamageMultiplier") or 100),
                               "hp_pct": int(r.get("HPMultiplier") or 100)})
    return out
