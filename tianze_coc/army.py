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


def housing(army: Army, prog: Progression) -> dict[str, int]:
    h = lambda m: sum(prog.units[g]["housing"] * n for g, n in m.items() if g in prog.units)  # noqa: E731
    return {"troops": h(army.troops), "spells": h(army.spells), "sieges": sum(army.sieges.values())}


def validate(army: Army, account: Account) -> list[str]:
    """返回违规说明列表（空 = 合法）。account 提供单位等级与建筑等级（容量）。"""
    p = account.prog
    errs: list[str] = []
    cap = capacity(account)
    lv = account.unit_levels

    def unit(g: int, kinds: tuple[str, ...], what: str) -> dict | None:
        u = p.units.get(g)
        if u is None or u["kind"] not in kinds:
            errs.append(f"{what} {g} 不是部落战/排位战可携带的{'/'.join(kinds)}")
            return None
        return u

    supers = set()
    for g, n in army.troops.items():
        u = unit(g, ("troop",), "兵种")
        if u is None or n <= 0:
            continue
        if u["status"] == "super_troop":
            supers.add(g)  # 超级兵等级随原兵种；激活本身不在账号等级表里
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
    hs = housing(army, p)
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
        if l < 1 or l > u["max_level"]:
            errs.append(f"援军 {u['name_cn']} 等级 {l} 无效")
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


def random_army(account: Account, rng, fill: float = 1.0, max_kinds: int = 6) -> Army:
    """随机合法配兵（基线 / 数据增广）：按容量随机挑几种兵和法术。"""
    p = account.prog
    cap = capacity(account)
    lv = account.unit_levels
    army = Army()
    troops = [u for g, u in p.units.items() if u["kind"] == "troop" and u["status"] == "permanent" and lv.get(g, 0) > 0]
    spells = [u for g, u in p.units.items() if u["kind"] == "spell" and lv.get(g, 0) > 0]
    sieges = [u for g, u in p.units.items() if u["kind"] == "siege" and lv.get(g, 0) > 0]
    room = int(cap.troops * fill)
    picks = rng.sample(troops, min(len(troops), rng.randint(1, max_kinds)))
    # 按随机权重把人口分给选中的兵种，再用最小人口的兵种把剩余填满（配兵应当接近满人口）
    weights = [rng.random() + 0.1 for _ in picks]
    tot_w = sum(weights)
    for u, w in zip(picks, weights):
        share = int(room * w / tot_w)
        n = share // u["housing"]
        if n > 0:
            army.troops[u["gid"]] = army.troops.get(u["gid"], 0) + n
    used = sum(p.units[g]["housing"] * n for g, n in army.troops.items())
    filler = sorted(picks, key=lambda u: u["housing"])
    for u in filler:
        k = (room - used) // u["housing"]
        if k > 0:
            army.troops[u["gid"]] = army.troops.get(u["gid"], 0) + k
            used += k * u["housing"]
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
    return army
