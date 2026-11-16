"""账号状态：解析 / 修改器 / 合法性校验。

一个账号 = 村庄 JSON（建筑、陷阱及其等级/坐标）+ avatar 二进制里的单位等级
（兵种、攻城机器、法术、英雄、战宠、英雄装备）。本模块：

- 从 OwnHomeData（自己）或 EnemyHomeData（防守方村庄 / 进攻方单位等级）解析出 `Account`；
- 修改器：改大本营、建筑等级/数量、单位等级，一键满级、随机生成合法账号；
- 合法性校验 `validate()`：数量/等级不超过大本营上限、兵种等级不超过实验室上限、
  英雄等级受大本营+英雄大厅限制、战宠受战宠小屋、装备受铁匠铺、合并建筑占用原料、
  （可关闭）升本前须先建齐上一本的所有建筑；
- 写回负载 `apply_to_payload()`，用于离线开局（训练时可以任意构造双方的账号）。

**所有等级都是 1 起的游戏等级**（村庄 JSON 的 lvl 和 avatar 里的值都是 0 起，在边界处换算）；
单位等级 0 表示未解锁。修改器只能改本地/离线数据；要改私服上的真实账号，需要私服本身支持（见 docs/models.md）。
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field

from . import avatar_codec as ac
from . import home_payload as hp
from .assets.progression import Progression

TH_GID = 1000001
WALL_GID = 1000010
BUILD_MIN, BUILD_MAX = 3, 47  # 主村可建造区域：格子 [3, 47)，即 44×44

# avatar 中的等级列表 <-> 单位种类
LEVEL_LISTS = {"troop": "unit_levels", "siege": "siege_levels", "spell": "spell_levels", "hero": "hero_levels",
               "pet": "pet_levels", "equipment": "equipment_levels"}


@dataclass
class Structure:
    """村庄中的一座建筑/陷阱/一段城墙。level 为 1 起。"""

    gid: int
    level: int
    x: int
    y: int
    kind: str = "buildings"  # 村庄 JSON 中所在的列表：buildings / traps
    raw: dict = field(default_factory=dict)  # 原始 JSON（保留其他字段：改装模式、各阵型槽坐标等）

    def to_json(self) -> dict:
        d = dict(self.raw)
        d["data"] = self.gid
        d["lvl"] = self.level - 1
        d["x"], d["y"] = self.x, self.y
        return d


@dataclass
class Violation:
    code: str
    message: str
    gid: int | None = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class Account:
    def __init__(self, home: dict | None, unit_levels: dict[int, int], prog: Progression | None = None):
        self.prog = prog or Progression.load()
        self.home = copy.deepcopy(home) if home is not None else None
        self.structures: list[Structure] = []
        if home is not None:
            for kind in ("buildings", "traps"):
                for b in home.get(kind, []):
                    if "x" not in b or "data" not in b:
                        continue
                    self.structures.append(Structure(b["data"], int(b.get("lvl", 0)) + 1, b["x"], b["y"], kind, dict(b)))
        self.unit_levels: dict[int, int] = dict(unit_levels)  # gid -> 1 起等级

    # ------------------------------------------------------------------ 解析
    @classmethod
    def from_payload(cls, payload: bytes, side: str = "own", prog: Progression | None = None) -> "Account":
        """side：own（OwnHomeData：自己的村庄 + 自己的单位）、
        defender（EnemyHomeData：防守方村庄 + 防守英雄）、attacker（EnemyHomeData：进攻方单位等级，无村庄）。"""
        home = hp.extract_home(payload) if side in ("own", "defender") else None
        levels: dict[int, int] = {}
        if side in ("own", "attacker"):
            chain = ac.find_chain(payload)
            for name in LEVEL_LISTS.values():
                sl = chain.get(name)
                if sl:
                    for gid, v in sl.entries:
                        levels[gid] = v + 1
        else:
            for sl in _defender_lists(payload).values():
                for gid, v in sl.entries:
                    levels[gid] = v + 1
        return cls(home, levels, prog)

    # ------------------------------------------------------------------ 查询
    @property
    def th(self) -> int:
        return max((s.level for s in self.structures if s.gid == TH_GID), default=0)

    def counts(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for s in self.structures:
            out[s.gid] = out.get(s.gid, 0) + 1
        return out

    def building_levels(self) -> dict[str, int]:
        """建筑名 -> 最高等级（用于单位等级上限判断）；含 'Town Hall'。"""
        out: dict[str, int] = {}
        for s in self.structures:
            d = self.prog.structure(s.gid)
            if d:
                out[d["name"]] = max(out.get(d["name"], 0), s.level)
        return out

    def unit_cap(self, gid: int, buildings: dict[str, int] | None = None) -> int:
        """该单位在当前建筑等级下的最高合法等级。"""
        return self.prog.max_unit_level(gid, buildings if buildings is not None else self.building_levels())

    # ------------------------------------------------------------------ 校验
    def validate(self, require_all_built: bool = True, check_units: bool = True,
                 check_structures: bool = True) -> list[Violation]:
        p = self.prog
        out: list[Violation] = []
        th = self.th
        counts = self.counts()
        if check_structures and self.home is not None:
            if counts.get(TH_GID, 0) != 1:
                out.append(Violation("th_count", f"大本营数量为 {counts.get(TH_GID, 0)}，应为 1", TH_GID))
            for gid, n in counts.items():
                d = p.structure(gid)
                if d is None:
                    out.append(Violation("unknown_structure", f"未知或禁用的建筑/陷阱 {gid}", gid))
                    continue
                cap = p.max_count(gid, th)
                if n > cap:
                    out.append(Violation("too_many", f"{th} 本 {d['name_cn']} 最多 {cap} 个，现有 {n} 个", gid))
            # 合并建筑：同一种原料可能被多种成品占用（如箭塔 -> 多人箭塔、复合机械塔），合计不能超过原料上限
            used_by: dict[int, list[str]] = {}
            used: dict[int, int] = {}
            for g2, n2 in counts.items():
                d2 = p.structure(g2)
                for inp, k in ((d2 or {}).get("merge") or {}).get("inputs", {}).items():
                    di = p.structure(inp)
                    if di:
                        used[di["gid"]] = used.get(di["gid"], 0) + k * n2
                        used_by.setdefault(di["gid"], []).append(f"{d2['name_cn']}×{n2}（每座 {k} 座）")
            for ig, extra in used.items():
                di = p.structure(ig)
                total = counts.get(ig, 0) + extra
                if total > p.max_count(ig, th):
                    out.append(Violation("merge_inputs", f"{di['name_cn']} 现有 {counts.get(ig, 0)} 座，另有 "
                                         f"{'、'.join(used_by[ig])} 由它合并而成，合计 {total} 超过上限 "
                                         f"{p.max_count(ig, th)}", ig))
            for s in self.structures:
                d = p.structure(s.gid)
                if d is None:
                    continue
                cap = p.max_structure_level(s.gid, th)
                if not 1 <= s.level <= cap:
                    out.append(Violation("structure_level", f"{d['name_cn']} {s.level} 级超过 {th} 本上限 {cap} 级", s.gid))
            if require_all_built and th >= 2:
                for d in p.data["structures"]:
                    if not d["require_built_on_th_up"]:
                        continue
                    need = min(d["max_count"][th - 2], d["max_count"][th - 1])
                    have = counts.get(d["gid"], 0)
                    if have < need:
                        out.append(Violation("not_all_built", f"升到 {th} 本前需先建齐 {need} 座{d['name_cn']}（{th - 1} 本上限），"
                                             f"现有 {have} 座", d["gid"]))
            out += self.overlaps()
        if check_units:
            blv = self.building_levels() if self.home is not None else None
            for gid, lv in self.unit_levels.items():
                u = p.units.get(gid)
                if u is None:
                    continue  # 夜世界/内部单位等不参与校验
                if u.get("derived_level"):
                    continue
                if lv > u["max_level"]:
                    out.append(Violation("unit_level", f"{u['name_cn']} {lv} 级超过最高 {u['max_level']} 级", gid))
                elif blv is not None:
                    cap = p.max_unit_level(gid, blv)
                    if lv > cap:
                        out.append(Violation("unit_level", f"{u['name_cn']} {lv} 级超过当前建筑允许的 {cap} 级"
                                             f"（{_req_text(u, cap + 1)}）", gid))
        return out

    def overlaps(self) -> list[Violation]:
        out = []
        occ: dict[tuple[int, int], Structure] = {}
        for s in self.structures:
            d = self.prog.structure(s.gid)
            w, h = (d["width"], d["height"]) if d else (1, 1)
            if s.x < BUILD_MIN or s.y < BUILD_MIN or s.x + w > BUILD_MAX or s.y + h > BUILD_MAX:
                out.append(Violation("out_of_bounds", f"{d['name_cn'] if d else s.gid} 位于 ({s.x},{s.y}) 超出可建造区域", s.gid))
            for dx in range(w):
                for dy in range(h):
                    k = (s.x + dx, s.y + dy)
                    if k in occ:
                        o = occ[k]
                        out.append(Violation("overlap", f"({k[0]},{k[1]}) 处 {s.gid} 与 {o.gid} 重叠", s.gid))
                        break
                    occ[k] = s
                else:
                    continue
                break
        return out

    def is_legal(self, **kw) -> bool:
        return not self.validate(**kw)

    # ------------------------------------------------------------------ 修改器
    def set_th(self, level: int) -> None:
        if not 1 <= level <= self.prog.th_max:
            raise ValueError(f"大本营等级应在 1~{self.prog.th_max}")
        ths = [s for s in self.structures if s.gid == TH_GID]
        if not ths:
            self.add_structure(TH_GID, level)
        for s in ths:
            s.level = level

    def set_structure_level(self, gid: int, level: int | None = None) -> None:
        """把某种建筑全部设为 level（None = 当前大本营允许的最高等级）。"""
        cap = self.prog.max_structure_level(gid, self.th)
        lv = cap if level is None else level
        for s in self.structures:
            if s.gid == gid:
                s.level = lv

    def set_structure_count(self, gid: int, n: int, level: int | None = None, rng: random.Random | None = None) -> None:
        """增删到 n 座；新建的放在空地上（level None = 最高等级）。"""
        cur = [s for s in self.structures if s.gid == gid]
        for s in cur[n:]:
            self.structures.remove(s)
        for _ in range(n - len(cur)):
            self.add_structure(gid, level, rng=rng)

    def add_structure(self, gid: int, level: int | None = None, x: int | None = None, y: int | None = None,
                      rng: random.Random | None = None) -> Structure:
        d = self.prog.structure(gid)
        if d is None:
            raise ValueError(f"未知或禁用的建筑/陷阱 {gid}")
        lv = level if level is not None else max(1, self.prog.max_structure_level(gid, max(self.th, 1)))
        if x is None or y is None:
            x, y = self.free_spot(d["width"], d["height"])
        kind = "traps" if d["category"] == "trap" else "buildings"
        pool = getattr(self, "_raw_pool", {}).get(gid)
        raw = dict(pool.pop(0)) if pool else {"data": gid, "id": self._next_id(kind)}
        s = Structure(gid, lv, x, y, kind, raw)
        self.structures.append(s)
        return s

    def set_unit_level(self, gid: int, level: int) -> None:
        if level <= 0:
            self.unit_levels.pop(gid, None)
        else:
            self.unit_levels[gid] = level

    def clamp_units(self) -> None:
        """把所有单位等级压到当前建筑允许的范围内（不合法的部分直接修正）。"""
        blv = self.building_levels()
        for gid in list(self.unit_levels):
            u = self.prog.units.get(gid)
            if u is None or u.get("derived_level"):
                continue
            cap = self.prog.max_unit_level(gid, blv)
            self.set_unit_level(gid, min(self.unit_levels[gid], cap))

    def max_out(self, th: int | None = None, walls: bool = True, traps: bool = True,
                rng: random.Random | None = None) -> None:
        """一键满级：大本营 th（默认当前），所有建筑建满并升到最高级，所有单位升到最高级。"""
        if th is not None:
            self.set_th(th)
        t = self.th
        for d in _placement_order(self.prog):
            if d["gid"] == TH_GID or (d["category"] == "wall" and not walls) or (d["category"] == "trap" and not traps):
                continue
            # 合并建筑：成品按上限建；原料在 _fix_merges 中按占用扣减
            self.set_structure_count(d["gid"], self._target_count(d, t), rng=rng)
            self.set_structure_level(d["gid"])
        self._fix_merges()
        blv = self.building_levels()
        for gid, u in self.prog.units.items():
            if u.get("derived_level"):
                continue
            cap = self.prog.max_unit_level(gid, blv)
            if u["kind"] == "equipment":
                heroes_ok = any(self.prog.max_unit_level(h, blv) > 0 for h in u.get("heroes", []))
                cap = cap if heroes_ok else 0
            self.set_unit_level(gid, cap)

    def randomize(self, th: int, rng: random.Random | None = None, require_all_built: bool = True,
                  progress: float | None = None) -> None:
        """随机生成一个 th 本的合法账号：建筑数量/等级、单位等级都在规则内随机。
        progress ∈ [0,1] 控制整体发育程度（None 则每项独立随机）。"""
        rng = rng or random.Random()
        # 复用原有条目（保留编号和大本营/建筑的附加字段，例如 18 本大本营的守卫配置）；
        # 联调发现全新生成的大本营条目会让客户端在开局时崩溃。
        self._raw_pool = {}
        for s in self.structures:
            self._raw_pool.setdefault(s.gid, []).append(s.raw)
        self.structures = []
        self.add_structure(TH_GID, th, rng=rng)
        t = th
        for d in _placement_order(self.prog):
            if d["gid"] == TH_GID:
                continue
            hi = self._target_count(d, t)
            lo = min(d["max_count"][t - 2], hi) if (require_all_built and t >= 2 and d["require_built_on_th_up"]) else 0
            if hi <= 0:
                continue
            n = hi if progress is None and d["category"] == "wall" else _pick(rng, lo, hi, progress)
            cap = self.prog.max_structure_level(d["gid"], t)
            floor_lv = max(1, self.prog.max_structure_level(d["gid"], t - 1) // 2) if t >= 2 else 1
            for _ in range(n):
                self.add_structure(d["gid"], _pick(rng, min(floor_lv, cap), cap, progress), rng=rng)
        self._fix_merges()
        blv = self.building_levels()
        self.unit_levels = {}
        for gid, u in self.prog.units.items():
            if u.get("derived_level"):
                continue
            cap = self.prog.max_unit_level(gid, blv)
            if u["kind"] == "equipment" and not any(self.prog.max_unit_level(h, blv) > 0 for h in u.get("heroes", [])):
                cap = 0
            if cap > 0:
                self.set_unit_level(gid, _pick(rng, 1, cap, progress))

    def _target_count(self, d: dict, t: int) -> int:
        """建满时的数量：合并原料要给成品留出份额。"""
        n = d["max_count"][t - 1]
        for p in self.prog.data["structures"]:
            m = p.get("merge")
            if m and d["name"] in m["inputs"]:
                n -= m["inputs"][d["name"]] * p["max_count"][t - 1]
        return max(0, n)

    def _fix_merges(self) -> None:
        """合并建筑占用原料：原料数 + Σ(份数×成品数) 不超过原料上限，超出时减少原料。"""
        t = self.th
        counts = self.counts()
        used: dict[int, int] = {}
        for d in self.prog.data["structures"]:
            n = counts.get(d["gid"], 0)
            for inp, k in ((d.get("merge") or {}).get("inputs") or {}).items():
                di = self.prog.structure(inp)
                if di and n:
                    used[di["gid"]] = used.get(di["gid"], 0) + k * n
        for ig, extra in used.items():
            allowed = max(0, self.prog.max_count(ig, t) - extra)
            if counts.get(ig, 0) > allowed:
                self.set_structure_count(ig, allowed)

    # ------------------------------------------------------------------ 布局辅助
    def occupancy(self) -> set[tuple[int, int]]:
        occ = set()
        for s in self.structures:
            d = self.prog.structure(s.gid)
            w, h = (d["width"], d["height"]) if d else (1, 1)
            occ.update((s.x + i, s.y + j) for i in range(w) for j in range(h))
        return occ

    def free_spot(self, w: int, h: int) -> tuple[int, int]:
        """逐行首次适配（紧凑，满建时不会因碎片化放不下）。新建筑的位置只是占位，真正的阵型由摆阵接口决定。"""
        occ = self.occupancy()
        for y in range(BUILD_MIN, BUILD_MAX - h + 1):
            for x in range(BUILD_MIN, BUILD_MAX - w + 1):
                if all((x + i, y + j) not in occ for i in range(w) for j in range(h)):
                    return x, y
        raise ValueError(f"地图上没有 {w}×{h} 的空地")

    def _next_id(self, kind: str) -> int:
        base = 504000000 if kind == "traps" else 500000000
        used = {int(s.raw.get("id", 0)) for s in self.structures}
        i = base
        while i in used:
            i += 1
        return i

    # ------------------------------------------------------------------ 写回
    def home_json(self, clear_obstacles: bool = False) -> dict:
        """生成新的村庄 JSON（保留原 JSON 的其他字段）。"""
        home = copy.deepcopy(self.home) if self.home is not None else {}
        for kind in ("buildings", "traps"):
            home[kind] = [s.to_json() for s in self.structures if s.kind == kind]
        if clear_obstacles:
            home["obstacles"] = []
        self._sync_summary(home)
        return home

    # EnemyHomeData 的防守方 JSON 带有一组等级/容量汇总字段，随修改器同步（只更新原本存在的键）
    _SUMMARY = {"town_hall_lvl": "Town Hall", "castle_lvl": "Clan Castle", "lab_lvl": "Laboratory",
                "ps_lvl": "Pet House", "b_lvl": "Barracks", "db_lvl": "Dark Barracks", "s_lvl": "Spell Factory",
                "ds_lvl": "Dark Spell Factory", "w_lvl": "Siege Workshop", "bs_lvl": "Blacksmith", "ht_lvl": "Hero Hall"}

    def _sync_summary(self, home: dict) -> None:
        blv = self.building_levels()
        for k, name in self._SUMMARY.items():
            if k in home:
                home[k] = blv.get(name, 0) - 1
        if "castle_total" in home:
            from .army import capacity

            cap = capacity(self)
            home["castle_total"], home["castle_total_sp"], home["castle_total_sm"] = cap.cc_troops, cap.cc_spells, cap.cc_sieges
            if "alliance_units" in home:
                used = sum(self.prog.units[u["id"]]["housing"] * u["cnt"] for u in home["alliance_units"]
                           if u["id"] in self.prog.units and self.prog.units[u["id"]]["kind"] == "troop")
                home["castle_used"] = min(used, cap.cc_troops)
        if "hero_upgrade" in home:
            heroes = [g for g, u in self.prog.units.items() if u["kind"] == "hero" and self.unit_levels.get(g, 0) > 0]
            home["hero_upgrade"] = [{"id": g, "cnt": self.unit_levels[g] - 1} for g in heroes]
        if "character_item_upgrades" in home:
            items = [g for g, u in self.prog.units.items() if u["kind"] == "equipment" and self.unit_levels.get(g, 0) > 0]
            home["character_item_upgrades"] = [{"id": g, "cnt": self.unit_levels[g] - 1} for g in items]

    def apply_to_payload(self, payload: bytes, side: str = "own", clear_obstacles: bool = False) -> bytes:
        """把本账号写回录制负载。side 含义同 from_payload。"""
        out = payload
        if side in ("own", "defender") and self.home is not None:
            out = hp.replace_home_json(out, self.home_json(clear_obstacles))
        if side in ("own", "attacker"):
            chain = ac.find_chain(out)
            reps = []
            for kind, name in LEVEL_LISTS.items():
                sl = chain.get(name)
                if sl is None:
                    continue
                reps.append((sl, _merge_levels(sl.entries, self.unit_levels, self.prog, kind)))
            out = ac.splice(out, reps)
        elif side == "defender":
            lists = _defender_lists(out)
            reps = [(sl, _merge_levels(sl.entries, self.unit_levels, self.prog, kind)) for kind, sl in lists.items()]
            out = ac.splice(out, reps)
        return out

    def summary(self) -> dict:
        c = self.counts()
        names = {}
        for gid, n in sorted(c.items()):
            d = self.prog.structure(gid)
            names[d["name_cn"] if d else str(gid)] = n
        return {"th": self.th, "structures": len(self.structures), "counts": names,
                "units": {self.prog.units[g]["name_cn"]: lv for g, lv in sorted(self.unit_levels.items()) if g in self.prog.units}}


# ---------------------------------------------------------------------- 内部工具
def _placement_order(prog: Progression) -> list[dict]:
    """大建筑先放，城墙最后（填缝），减少放不下的情况。"""
    return sorted(prog.data["structures"], key=lambda d: (d["category"] == "wall", -d["width"] * d["height"]))


def _pick(rng: random.Random, lo: int, hi: int, progress: float | None) -> int:
    if hi <= lo:
        return max(lo, hi) if hi >= lo else hi
    if progress is None:
        return rng.randint(lo, hi)
    centre = lo + progress * (hi - lo)
    v = round(rng.gauss(centre, max(0.5, (hi - lo) * 0.15)))
    return max(lo, min(hi, v))


def _req_text(u: dict, level: int) -> str:
    if level - 1 < len(u["levels_req"]):
        req = u["levels_req"][level - 1]
        return f"{level} 级需要 " + "、".join(f"{k} {v} 级" for k, v in req.items())
    return ""


def _merge_levels(entries: list[tuple[int, int]], levels: dict[int, int], prog: Progression, kind: str):
    """按原列表顺序写回等级；属于该种类的新单位追加在末尾；等级为 0 的移除（原列表中不认识的条目保持原样）。"""
    out = []
    seen = set()
    for gid, v in entries:
        seen.add(gid)
        u = prog.units.get(gid)
        if u is None or u["kind"] != kind:
            out.append((gid, v))  # 夜世界/内部单位等：保持原样
            continue
        lv = levels.get(gid, 0)
        if lv > 0:
            out.append((gid, lv - 1))
    for gid, lv in sorted(levels.items()):
        u = prog.units.get(gid)
        if gid in seen or u is None or u["kind"] != kind or lv <= 0 or u.get("derived_level"):
            continue
        out.append((gid, lv - 1))
    return out


def _defender_lists(payload: bytes) -> dict[str, ac.SlotList]:
    """EnemyHomeData 中防守方 avatar 的英雄等级与装备等级列表（位于进攻方列表链之前）。"""
    found = hp.find_compressed_json(payload, require_key=None)
    start = found[-1].data_end if found else 0
    try:
        end = ac.find_chain(payload).lists["unit_counts"].start
    except ValueError:
        end = len(payload)
    out: dict[str, ac.SlotList] = {}
    o = start
    while o < end - 4:
        if "hero" not in out:
            sl = ac.parse_list(payload, o, 28)
            if sl and sl.entries and sl.end <= end:
                out["hero"] = sl
                o = sl.end
                continue
        elif "equipment" not in out:
            sl = ac.parse_list(payload, o, ac.TABLE_EQUIPMENT)
            if sl and len(sl.entries) >= 2 and sl.end <= end:
                out["equipment"] = sl
                break
        o += 1
    return out
