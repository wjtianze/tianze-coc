"""配兵：表示、容量计算、合法性校验、与游戏格式互转。

配兵模型的输出 = 一个 `Army`；本模块负责：
- `capacity(account)`：按建筑等级算出兵营人口、法术容量、攻城机器容量、英雄槽、部落城堡容量；
- `validate(army, account)`：只能用部落战/排位战可携带的单位、必须已解锁、人口/容量不超、
  超级兵最多 2 种、英雄最多 4 名且不重复、装备属于该英雄且已拥有、每名英雄最多 2 件装备、
  战宠已解锁且不重复、模式切换只对有双模式的英雄（大守护者）有效、援军不超过城堡容量与等级上限；
- `to_loadout_json()`：转为村庄 JSON 中 active_army_loadout 的格式（私服当前配兵）；
- `apply_to_enemy_payload()`：写入 EnemyHomeData 的进攻方字段，离线开局时使用任意配兵（自对弈）。

攻城机器可以带多台（受攻城工坊容量限制），但一局只能部署一台——这条在战斗环境里强制（battle_env）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import attacker_payload
from .account import Account
from .assets.progression import Progression

CC_GID_NAME = "Clan Castle"


@dataclass
class HeroPick:
    gid: int
    equipment: list[int] = field(default_factory=list)  # 最多 item_slots 件
    pet: int = 0  # 0 = 不带战宠
    mode: int = 0  # 0 地面 1 空中（仅大守护者）


@dataclass
class Army:
    troops: dict[int, int] = field(default_factory=dict)  # GlobalID -> 数量
    spells: dict[int, int] = field(default_factory=dict)
    sieges: dict[int, int] = field(default_factory=dict)
    heroes: list[HeroPick] = field(default_factory=list)
    cc: list[tuple[int, int, int]] = field(default_factory=list)  # (GlobalID, 数量, 等级 1 起)：兵种/法术/攻城机器

    def to_dict(self) -> dict:
        return {"troops": self.troops, "spells": self.spells, "sieges": self.sieges,
                "heroes": [h.__dict__ for h in self.heroes], "cc": [list(x) for x in self.cc]}

    @classmethod
    def from_dict(cls, d: dict) -> "Army":
        ik = lambda m: {int(k): int(v) for k, v in (m or {}).items()}  # noqa: E731
        return cls(ik(d.get("troops")), ik(d.get("spells")), ik(d.get("sieges")),
                   [HeroPick(int(h["gid"]), [int(x) for x in h.get("equipment", [])], int(h.get("pet", 0)),
                             int(h.get("mode", 0))) for h in d.get("heroes", [])],
                   [tuple(int(v) for v in x) for x in d.get("cc", [])])


@dataclass
class Capacity:
    troops: int
    spells: int
    sieges: int
    hero_slots: int
    cc_troops: int
    cc_spells: int
    cc_sieges: int
    cc_level: int


def capacity(account: Account) -> Capacity:
    p = account.prog
    tot = {"troops": 0, "spells": 0, "sieges": 0}
    cc = {"troops": 0, "spells": 0, "sieges": 0}
    cc_level = 0
    for s in account.structures:
        d = p.structure(s.gid)
        if not d or "housing" not in d:
            continue
        for k, arr in d["housing"].items():
            v = arr[min(s.level, len(arr)) - 1]
            if d["name"] == CC_GID_NAME:
                cc[k] += v
                cc_level = max(cc_level, s.level)
            else:
                tot[k] += v
    return Capacity(tot["troops"], tot["spells"], tot["sieges"], p.rules["hero_slots"],
                    cc["troops"], cc["spells"], cc["sieges"], cc_level)


def unit_housing(prog: Progression, gid: int, level: int = 0) -> int:
    """单位人口；19.32 起部分单位（PEKKA 雷霆小队、飞龙宝宝小队）人口随等级变化，给出等级（1 起）时按该级取。"""
    u = prog.units[gid]
    hl = u.get("housing_levels")
    if hl and level > 0:
        return hl[min(level, len(hl)) - 1]
    return u["housing"]


def housing(army: Army, prog: Progression, levels: dict[int, int] | None = None) -> dict[str, int]:
    lv = levels or {}
    h = lambda m: sum(unit_housing(prog, g, lv.get(g, 0)) * n for g, n in m.items() if g in prog.units)  # noqa: E731
    return {"troops": h(army.troops), "spells": h(army.spells), "sieges": sum(army.sieges.values())}


def validate(army: Army, account: Account) -> list[str]:
    """返回违规说明列表（空 = 合法）。account 提供单位等级与建筑等级（容量）。"""
    p = account.prog
    errs: list[str] = []
    cap = capacity(account)
    lv = account.unit_levels

    # 有村庄时直接按军事建筑判断解锁（训练营/暗黑训练营/法术工厂/攻城工坊/小队训练营/战宠小屋/英雄殿堂/铁匠铺……）
    blv = account.building_levels() if account.home is not None else None

    def unit(g: int, kinds: tuple[str, ...], what: str) -> dict | None:
        u = p.units.get(g)
        if u is None or u["kind"] not in kinds:
            errs.append(f"{what} {g} 不是部落战/排位战可携带的{'/'.join(kinds)}")
            return None
        if blv is not None and not u.get("derived_level") and p.max_unit_level(g, blv) <= 0:
            need = "、".join(f"{b} {n} 级" for b, n in ((u.get("unlock") or {}) | (u["levels_req"][0] if u["levels_req"] else {})).items()
                            if b != "Laboratory")
            errs.append(f"{u['name_cn'] or u['name']} 在当前建筑下未解锁（需要 {need}）")
            return None
        return u

    supers = set()
    for g, n in army.troops.items():
        u = unit(g, ("troop", "squad"), "兵种/小队")  # 小队（19.32）与兵种同槽、占兵营人口，种类数不限（用户确认）
        if u is None or n <= 0:
            continue
        if u["status"] == "super_troop":
            supers.add(g)  # 超级兵等级随原兵种；激活本身不在账号等级表里
            o = super_original(p, g)
            if o is None or lv.get(o, 0) < u.get("min_original_level", 0) + 1:
                errs.append(f"{u['name_cn']} 需要原兵种达到 {u.get('min_original_level', 0) + 1} 级才能激活")
        elif lv.get(g, 0) <= 0:
            errs.append(f"{u['name_cn']} 未解锁")
    if len(supers) > p.rules["max_super_troop_kinds"]:
        errs.append(f"超级兵最多同时激活 {p.rules['max_super_troop_kinds']} 种，配了 {len(supers)} 种")
    for g, n in army.spells.items():
        u = unit(g, ("spell",), "法术")
        if u and n > 0 and lv.get(g, 0) <= 0:
            errs.append(f"{u['name_cn']} 未解锁")
    for g, n in army.sieges.items():
        u = unit(g, ("siege",), "攻城机器")
        if u and n > 0 and lv.get(g, 0) <= 0:
            errs.append(f"{u['name_cn']} 未解锁")
    hs = housing(army, p, lv)
    if hs["troops"] > cap.troops:
        errs.append(f"兵种人口 {hs['troops']} 超过兵营容量 {cap.troops}")
    if hs["spells"] > cap.spells:
        errs.append(f"法术容量 {hs['spells']} 超过 {cap.spells}")
    if hs["sieges"] > cap.sieges:
        errs.append(f"攻城机器 {hs['sieges']} 台超过攻城工坊容量 {cap.sieges}")
    if len(army.heroes) > cap.hero_slots:
        errs.append(f"英雄最多 {cap.hero_slots} 名")
    seen_h, seen_pet, seen_item = set(), set(), set()
    for h in army.heroes:
        u = unit(h.gid, ("hero",), "英雄")
        if u is None:
            continue
        if h.gid in seen_h:
            errs.append(f"{u['name_cn']} 重复")
        seen_h.add(h.gid)
        if lv.get(h.gid, 0) <= 0:
            errs.append(f"{u['name_cn']} 未解锁")
        if len(h.equipment) > u.get("item_slots", 2):
            errs.append(f"{u['name_cn']} 最多 {u.get('item_slots', 2)} 件装备")
        for it in h.equipment:
            e = p.units.get(it)
            if e is None or e["kind"] != "equipment":
                errs.append(f"装备 {it} 不存在或不可用")
                continue
            if h.gid not in e.get("heroes", []):
                errs.append(f"{e['name_cn']} 不能给 {u['name_cn']} 使用")
            if lv.get(it, 0) <= 0:
                errs.append(f"{e['name_cn']} 未拥有")
            if it in seen_item:
                errs.append(f"{e['name_cn']} 重复装备")
            seen_item.add(it)
        if h.pet:
            pu = p.units.get(h.pet)
            if pu is None or pu["kind"] != "pet":
                errs.append(f"战宠 {h.pet} 不存在")
            elif lv.get(h.pet, 0) <= 0:
                errs.append(f"{pu['name_cn']} 未解锁")
            if h.pet in seen_pet:
                errs.append(f"战宠 {h.pet} 重复携带")
            seen_pet.add(h.pet)
        if h.mode and not u.get("has_alt_mode"):
            errs.append(f"{u['name_cn']} 没有第二模式")
    # 部落援军
    ct = cs = cg = 0
    for g, n, l in army.cc:
        u = p.units.get(g)
        if u is None or u["kind"] not in ("troop", "spell", "siege"):
            errs.append(f"援军 {g} 不可用")
            continue
        from .assets.progression import cc_level_cap

        lim = cc_level_cap(p, g, cap.cc_level)
        if l < 1 or l > lim:
            errs.append(f"援军 {u['name_cn']} {l} 级超过 {cap.cc_level} 级部落城堡的援军上限 {lim} 级")
        if u["kind"] == "troop":
            ct += u["housing"] * n
        elif u["kind"] == "spell":
            cs += u["housing"] * n
        else:
            cg += n
    if ct > cap.cc_troops or cs > cap.cc_spells or cg > cap.cc_sieges:
        errs.append(f"援军超过部落城堡容量（兵 {ct}/{cap.cc_troops} 法术 {cs}/{cap.cc_spells} 攻城 {cg}/{cap.cc_sieges}）")
    return errs


def to_loadout_json(army: Army) -> dict:
    """村庄 JSON 的 active_army_loadout 格式（slots_by_type：0 兵种 1 法术 2 攻城机器 3 未用 4 英雄）。
    槽位 1/2 的键名按槽位 0 推断，私服接口以真机抓到的命令为准。"""
    return {"m_type": 1, "m_customName": "", "slots_by_type": [
        [{"id": g, "m_count": n} for g, n in army.troops.items() if n > 0],
        [{"id": g, "m_count": n} for g, n in army.spells.items() if n > 0],
        [{"id": g, "m_count": n} for g, n in army.sieges.items() if n > 0],
        [],
        [{"id": h.gid, "m_itemDatas": list(h.equipment), "m_heroMode": h.mode} for h in army.heroes],
    ]}


def from_loadout_json(d: dict) -> Army:
    s = d.get("slots_by_type", [])
    get = lambda i: {x["id"]: x.get("m_count", 0) for x in (s[i] if i < len(s) else []) if x.get("id")}  # noqa: E731
    heroes = [HeroPick(x["id"], list(x.get("m_itemDatas", [])), 0, x.get("m_heroMode", 0))
              for x in (s[4] if len(s) > 4 else []) if x.get("id")]
    return Army(get(0), get(1), get(2), heroes)


def apply_to_enemy_payload(payload: bytes, army: Army) -> bytes:
    """把配兵写入 EnemyHomeData 的进攻方字段（离线开局用）。不做合法性校验，调用方先 validate。"""
    return attacker_payload.patch(
        payload, troops=army.troops, spells=army.spells, sieges=army.sieges,
        heroes=[h.gid for h in army.heroes], pets={h.gid: h.pet for h in army.heroes},
        equipment={h.gid: h.equipment for h in army.heroes if h.equipment}, cc=list(army.cc))


def from_enemy_payload(payload: bytes) -> Army:
    d = attacker_payload.describe(payload)
    pets = d["pets"] or {}
    eq = d["equipment"] or {}
    heroes = [HeroPick(h, eq.get(h, []), pets.get(h, 0)) for h in (d["heroes"] or [])]
    return Army(d["troops"], d["spells"], d["sieges"], heroes, d["cc"] or [])


def random_cc(account: Account, rng) -> list[tuple[int, int, int]]:
    """随机进攻援军：按部落城堡容量配满兵种/法术/攻城机器，等级取援军上限。"""
    from .assets.progression import cc_level_cap

    p = account.prog
    cap = capacity(account)
    ccl = cap.cc_level
    out: list[tuple[int, int, int]] = []
    for kind, room in (("troop", cap.cc_troops), ("spell", cap.cc_spells), ("siege", cap.cc_sieges)):
        pool = [u for g, u in p.units.items() if u["kind"] == kind and u["status"] in ("permanent", "super_troop")
                and cc_level_cap(p, g, ccl) > 0 and (u["housing"] if kind != "siege" else 1) <= room]
        rng.shuffle(pool)
        for u in pool[:3]:
            h = u["housing"] if kind != "siege" else 1
            if room < h:
                continue
            n = rng.randint(1, room // h)
            out.append((u["gid"], n, cc_level_cap(p, u["gid"], ccl)))
            room -= n * h
    return out


def super_original(prog, gid: int) -> int | None:
    """超级兵的原兵种 GlobalID。"""
    name = prog.units[gid].get("original")
    return next((g for g, x in prog.units.items() if x["name"] == name), None)


def can_boost(account: Account, gid: int) -> bool:
    """本账号能否激活该超级兵：原兵种等级 >= MinOriginalLevel + 1（MinOriginalLevel 是 0 起的）。"""
    p = account.prog
    o = super_original(p, gid)
    return o is not None and account.unit_levels.get(o, 0) >= p.units[gid].get("min_original_level", 0) + 1


def random_army(account: Account, rng, fill: float = 1.0, max_kinds: int = 6, cc: bool = True) -> Army:
    """随机合法配兵（基线 / 数据增广）：按容量随机挑几种兵和法术。"""
    return _random_army(account, rng, fill, max_kinds, cc)


def _random_army(account: Account, rng, fill: float = 1.0, max_kinds: int = 6, cc: bool = True) -> Army:
    """随机合法配兵（基线 / 数据增广）：按容量随机挑几种兵和法术。"""
    p = account.prog
    cap = capacity(account)
    lv = account.unit_levels
    army = Army()
    # 兵种与小队（19.32：小队种类数不限，与兵种一起参与随机）
    troops = [u for g, u in p.units.items() if u["kind"] in ("troop", "squad") and u["status"] == "permanent"
              and lv.get(g, 0) > 0]
    hs = lambda u: unit_housing(p, u["gid"], lv.get(u["gid"], 0))  # noqa: E731
    boostable = [u for g, u in p.units.items() if u["kind"] == "troop" and u["status"] == "super_troop"
                 and can_boost(account, g)]
    # 超级兵：随机 0~2 种（同时最多激活 max_super_troop_kinds 种）
    k = rng.randint(0, min(p.rules["max_super_troop_kinds"], len(boostable)))
    supers = rng.sample(boostable, k)
    spells = [u for g, u in p.units.items() if u["kind"] == "spell" and lv.get(g, 0) > 0]
    sieges = [u for g, u in p.units.items() if u["kind"] == "siege" and lv.get(g, 0) > 0]
    room = int(cap.troops * fill)
    picks = rng.sample(troops, min(len(troops), max(1, rng.randint(1, max_kinds) - k))) + supers
    # 按随机权重把人口分给选中的兵种，再用最小人口的兵种把剩余填满（配兵应当接近满人口）
    weights = [rng.random() + 0.1 for _ in picks]
    tot_w = sum(weights)
    for u, w in zip(picks, weights):
        share = int(room * w / tot_w)
        n = share // hs(u)
        if n > 0:
            army.troops[u["gid"]] = army.troops.get(u["gid"], 0) + n
    used = sum(unit_housing(p, g, lv.get(g, 0)) * n for g, n in army.troops.items())
    filler = sorted(picks, key=hs)
    for u in filler:
        k = (room - used) // hs(u)
        if k > 0:
            army.troops[u["gid"]] = army.troops.get(u["gid"], 0) + k
            used += k * hs(u)
    room = cap.spells
    rng.shuffle(spells)
    for u in spells:
        if room >= u["housing"]:
            n = rng.randint(1, room // u["housing"])
            army.spells[u["gid"]] = n
            room -= n * u["housing"]
    for u in rng.sample(sieges, min(len(sieges), cap.sieges)):
        army.sieges[u["gid"]] = 1
    heroes = [g for g, u in p.units.items() if u["kind"] == "hero" and lv.get(g, 0) > 0]
    pets = [g for g, u in p.units.items() if u["kind"] == "pet" and lv.get(g, 0) > 0]
    rng.shuffle(pets)
    for g in rng.sample(heroes, min(len(heroes), cap.hero_slots)):
        items = [i for i, u in p.units.items() if u["kind"] == "equipment" and g in u.get("heroes", []) and lv.get(i, 0) > 0]
        army.heroes.append(HeroPick(g, rng.sample(items, min(2, len(items))), pets.pop() if pets else 0,
                                    rng.randint(0, 1) if p.units[g].get("has_alt_mode") else 0))
    if cc:
        army.cc = random_cc(account, rng)
    return army
