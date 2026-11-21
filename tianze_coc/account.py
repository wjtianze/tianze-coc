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

# avatar 中的等级列表 -> 写回时由它负责的单位种类
# （小队是 characters.csv 里的行，等级与兵种同在 unit_levels；19.32 待有小队的账号录制后复核）
LEVEL_LISTS = {"unit_levels": ("troop", "squad"), "siege_levels": ("siege",), "spell_levels": ("spell",),
               "hero_levels": ("hero",), "pet_levels": ("pet",), "equipment_levels": ("equipment",)}


@dataclass
class Structure:
    """村庄中的一座建筑/陷阱/一段城墙。level 为 1 起。"""

    gid: int
    level: int
    x: int
    y: int
    kind: str = "buildings"  # 村庄 JSON 中所在的列表：buildings / traps
    raw: dict = field(default_factory=dict)  # 原始 JSON（保留其他字段：改装模式、各阵型槽坐标等）
    supercharge: int = 0  # 超级充能小等级（村庄 JSON 的 minilvl；导出格式里叫 supercharge）

    def to_json(self) -> dict:
        d = dict(self.raw)
        d["data"] = self.gid
        d["lvl"] = self.level - 1
        d["x"], d["y"] = self.x, self.y
        if self.supercharge > 0:
            d["minilvl"] = self.supercharge
        else:
            d.pop("minilvl", None)
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
                    self.structures.append(Structure(b["data"], int(b.get("lvl", 0)) + 1, b["x"], b["y"], kind, dict(b),
                                                     int(b.get("minilvl", 0) or 0)))
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
            for name in LEVEL_LISTS:
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

    def army_buildings(self) -> dict[str, list[int]]:
        """军事建筑（buildings.csv 中 BuildingClass=Army：兵营、训练营、暗黑训练营、法术工厂、攻城工坊、部落城堡、
        实验室、英雄殿堂、战宠小屋、铁匠铺、小队训练营……）-> 各座等级。配兵的解锁、容量与等级上限都由它们决定，
        也是配兵模型的输入。"""
        out: dict[str, list[int]] = {}
        for s in self.structures:
            d = self.prog.structure(s.gid)
            if d and d.get("building_class") == "Army":
                out.setdefault(d["name"], []).append(s.level)
        return {k: sorted(v) for k, v in out.items()}

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
                if s.supercharge:
                    sc = d.get("supercharge")
                    if not sc or th < sc["th"] or s.level != sc["level"] or not 0 < s.supercharge <= sc["max"]:
                        out.append(Violation("supercharge", f"{d['name_cn']} 超级充能 {s.supercharge} 不合法（需大本营 ≥ "
                                             f"{sc['th'] if sc else '—'}、建筑达到 {sc['level'] if sc else '—'} 级、最多 {sc['max'] if sc else 0} 级）", s.gid))
            if require_all_built and th >= 2:
                # 游戏规定先建齐当前本的建筑才能升本：每种建筑（含城墙、陷阱）不少于上一本上限；
                # 合并原料按等效数量（自身 + 已合并进成品的份数）计；被大本营合并消耗的取较小值。
                for d in p.data["structures"]:
                    if d["gid"] == TH_GID or p.max_structure_level(d["gid"], th - 1) <= 0:
                        continue  # 上一本还不能建（例如 1～2 本的部落城堡只是废墟）
                    need = min(d["max_count"][th - 2], d["max_count"][th - 1])
                    have = counts.get(d["gid"], 0) + used.get(d["gid"], 0)
                    if have < need:
                        extra = f"（含合并进成品的 {used[d['gid']]} 座）" if used.get(d["gid"]) else ""
                        out.append(Violation("not_all_built", f"升到 {th} 本前需先建齐 {need} 座{d['name_cn']}（{th - 1} 本上限），"
                                             f"现有 {have} 座{extra}", d["gid"]))
                # 升本前必须升满的建筑（英雄殿堂）：不低于上一本满级
                for name in p.rules.get("maxed_before_th_up", []):
                    d = p.structure(name)
                    need = p.max_structure_level(name, th - 1) if d else 0
                    have = max((s.level for s in self.structures if d and s.gid == d["gid"]), default=0)
                    if need and have < need:
                        out.append(Violation("not_maxed", f"升到 {th} 本前需先把{d['name_cn']}升到 {need} 级（{th - 1} 本满级），"
                                             f"现为 {have} 级", d["gid"]))
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
        raw.pop("minilvl", None)  # 复用条目时不继承原有的超级充能
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
                rng: random.Random | None = None, supercharge: bool = True) -> None:
        """一键满级：大本营 th（默认当前），所有建筑建满并升到最高级（可超级充能的充满），所有单位升到最高级。"""
        if th is not None:
            self.set_th(th)
        t = self.th
        for d in _placement_order(self.prog):
            if d["gid"] == TH_GID or (d["category"] == "wall" and not walls) or (d["category"] == "trap" and not traps):
                continue
            # 合并建筑：成品按上限建；原料在 _fix_merges 中按占用扣减
            n = self._target_count(d, t) if self.prog.max_structure_level(d["gid"], t) > 0 else 0
            self.set_structure_count(d["gid"], n, rng=rng)
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
        for s in self.structures:
            s.supercharge = self.supercharge_cap(s) if supercharge else 0

    def supercharge_cap(self, s: Structure) -> int:
        """该建筑当前能充的超级充能小等级数（0 = 不能充：没有充能、大本营不够或建筑未达本本满级）。"""
        d = self.prog.structure(s.gid)
        sc = (d or {}).get("supercharge")
        if not sc or self.th < sc["th"] or s.level != sc["level"]:
            return 0  # 需大本营 ≥ 所需等级，且建筑处在可充能的那一级（buildings.csv 的 MiniLevels）
        return sc["max"]

    def randomize(self, th: int, rng: random.Random | None = None, require_all_built: bool = True,
                  progress: float | None = None, style: str | None = None) -> None:
        """随机生成一个 th 本的合法账号：建筑数量/等级、单位等级都在规则内随机。

        数量（用户要求，游戏规定先建齐当前本的建筑才能升本）：每种建筑（含城墙、陷阱）的数量在
        "上一本上限 ~ 本本上限"之间；合并建筑的原料按"自身 + 已合并进成品的份数"计算（见 _random_counts）。
        等级分布模拟真实玩家：
        - 稳本 STEADY（多数）：每项等级主要落在"上一本满级 ~ 本本满级"之间，整体进度 progress 决定偏向哪一端。
        - 速本 RUSH（少数，概率 RUSH_PROB）：只有关键建筑（大本营与军事类：兵营、实验室、部落城堡、英雄殿堂等）
          按稳本升级；防御、资源、城墙、陷阱停留在 RUSH_LAG 本以前到上一本之间。单位等级由实验室等建筑决定。
        - 全满 FULL（概率 FULL_MAX_PROB）：建筑建满升满、单位满级、可超级充能的充满。
        - 超级充能：达到本本满级的可充能建筑各自独立地以一定概率充 1~满 个小等级。
        - 不论稳本速本，每个建筑、每个单位都**各自独立**以 LOW_TAIL 的概率取更低的等级（最低可到 1 级），
          所以会出现"满级熔岩猎犬 + 1 级气球"这类组合（见 _pick_level）。
        progress ∈ [0,1]：本本内的发育进度（None 则每个账号随机一个）。style：None 则按 RUSH_PROB 随机。"""
        rng = rng or random.Random()
        p = rng.random() if progress is None else progress
        if style is None and rng.random() < FULL_MAX_PROB:
            style = FULL
        self.style = style or (RUSH if rng.random() < RUSH_PROB else STEADY)
        if self.style == FULL:  # 一部分账号全满（建筑、单位、超级充能），用户要求
            self._raw_pool = {}
            for s in self.structures:
                self._raw_pool.setdefault(s.gid, []).append(s.raw)
            self.structures = []
            self.add_structure(TH_GID, th, rng=rng)
            self.max_out(th, rng=rng)
            return
        # 复用原有条目（保留编号和大本营/建筑的附加字段，例如 18 本大本营的守卫配置）；
        # 联调发现全新生成的大本营条目会让客户端在开局时崩溃。
        self._raw_pool = {}
        for s in self.structures:
            self._raw_pool.setdefault(s.gid, []).append(s.raw)
        self.structures = []
        self.add_structure(TH_GID, th, rng=rng)
        t = th
        counts = self._random_counts(t, rng, p, require_all_built)
        for d in _placement_order(self.prog):
            n = counts.get(d["gid"], 0)
            if d["gid"] == TH_GID or n <= 0:
                continue
            cap = self.prog.max_structure_level(d["gid"], t)
            prev = self.prog.max_structure_level(d["gid"], t - 1) if t >= 2 else 0
            if self.style == RUSH and not _is_key(d) and prev > 0:
                # 速本的非关键建筑：停在 RUSH_LAG 本以前 ~ 上一本满级之间
                cap = prev
                prev = self.prog.max_structure_level(d["gid"], max(1, t - RUSH_LAG))
            for _ in range(n):
                if d["name"] in self.prog.rules.get("maxed_before_th_up", []):
                    # 英雄殿堂：升本前必须升满，所以至少是上一本满级；并且大多直接升到本本满级（用户要求）
                    full = self.prog.max_structure_level(d["gid"], t)
                    floor_ = self.prog.max_structure_level(d["gid"], t - 1) if t >= 2 else 1
                    lv = full if rng.random() < HERO_HALL_MAX_PROB else rng.randint(max(1, min(floor_, full)), full)
                else:
                    lv = _pick_level(rng, prev, cap, p)
                self.add_structure(d["gid"], lv, rng=rng)
        blv = self.building_levels()
        # 上一本时各建筑能达到的等级（单位等级下限：上一本建筑满级时单位能到的等级）
        blv_prev = {k: min(v, self.prog.max_structure_level(k, t - 1) if t >= 2 else 0) for k, v in blv.items()}
        blv_prev["Town Hall"] = t - 1
        self.unit_levels = {}
        for gid, u in self.prog.units.items():
            if u.get("derived_level"):
                continue
            cap = self.prog.max_unit_level(gid, blv)
            if u["kind"] == "equipment" and not any(self.prog.max_unit_level(h, blv) > 0 for h in u.get("heroes", [])):
                cap = 0
            if cap > 0:
                prev = self.prog.max_unit_level(gid, blv_prev) if t >= 2 else 0
                self.set_unit_level(gid, _pick_level(rng, prev, cap, p))
        # 超级充能：达到本本满级的可充能建筑，每座独立以 SUPERCHARGE_PROB（随进度增加）充 1~满 个小等级
        for s in self.structures:
            cap_sc = self.supercharge_cap(s)
            if cap_sc and rng.random() < SUPERCHARGE_PROB * (0.5 + p):
                s.supercharge = max(1, min(cap_sc, round(1 + rng.random() ** (1 - p * 0.8) * (cap_sc - 1))))

    def _count_range(self, d: dict, t: int, require_all_built: bool = True) -> tuple[int, int]:
        """该建筑在 t 本的数量区间 [上一本上限, 本本上限]（合并原料为"等效数量"：自身 + 已合并进成品的份数）。
        上一本还不能建（废墟、新解锁）时下限为 0；大本营升级时被合并消耗的（如天鹰火炮）上限会更小，取两者较小值。"""
        if self.prog.max_structure_level(d["gid"], t) <= 0:
            return 0, 0
        hi = d["max_count"][t - 1]
        lo = 0
        if require_all_built and t >= 2 and self.prog.max_structure_level(d["gid"], t - 1) > 0:
            lo = min(d["max_count"][t - 2], hi)
        return lo, hi

    def _random_counts(self, t: int, rng: random.Random, p: float, require_all_built: bool = True) -> dict[int, int]:
        """先定合并成品的数量，再定原料：原料的等效数量（自身 + Σ份数×成品数）落在 [上一本上限, 本本上限]。"""
        out: dict[int, int] = {}
        consumed: dict[str, int] = {}
        structs = self.prog.data["structures"]
        for d in structs:  # 合并成品
            if d["gid"] == TH_GID or not d.get("merge"):
                continue
            lo, hi = self._count_range(d, t, require_all_built)
            n = _pick(rng, lo, hi, p) if hi > 0 else 0
            out[d["gid"]] = n
            for inp, k in d["merge"]["inputs"].items():
                consumed[inp] = consumed.get(inp, 0) + k * n
        for d in structs:  # 其余建筑（含合并原料、城墙、陷阱）
            if d["gid"] == TH_GID or d.get("merge"):
                continue
            lo, hi = self._count_range(d, t, require_all_built)
            used = consumed.get(d["name"], 0)
            lo = max(lo, used)
            if hi <= 0 or hi < lo:
                out[d["gid"]] = max(0, hi - used)
                continue
            if self.style == RUSH and not _is_key(d):
                p_n = p / 2
            else:
                p_n = p
            out[d["gid"]] = _pick(rng, lo, hi, p_n) - used
        return out

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
        if side == "defender" and self.home is not None and "alliance_units" in self.home:
            # 防守部落城堡援军：引擎读的是防守方 avatar 里的援军列表，不是村庄 JSON 的 alliance_units（联调发现）。
            # 必须在替换 JSON 之前、按原负载的内容定位（定位依赖原 JSON 里的援军）。
            span = defender_cc_span(out)
            if span is None:
                raise ValueError("负载中没有找到防守方援军列表")
            ents = [(u["id"], u["cnt"], u["lvl"]) for u in self.home["alliance_units"]]
            out = out[:span[0]] + encode_cc_list(ents) + out[span[1]:]
        if side in ("own", "defender") and self.home is not None:
            out = hp.replace_home_json(out, self.home_json(clear_obstacles))
        if side in ("own", "attacker"):
            chain = ac.find_chain(out)
            reps = []
            for name, kinds in LEVEL_LISTS.items():
                sl = chain.get(name)
                if sl is None:
                    continue
                reps.append((sl, _merge_levels(sl.entries, self.unit_levels, self.prog, kinds)))
            out = ac.splice(out, reps)
        elif side == "defender":
            lists = _defender_lists(out)
            reps = [(sl, _merge_levels(sl.entries, self.unit_levels, self.prog, (kind,))) for kind, sl in lists.items()]
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


STEADY, RUSH, FULL = "steady", "rush", "full"
FULL_MAX_PROB = 0.1  # 全满账号（建筑、单位、超级充能全满）的比例
SUPERCHARGE_PROB = 0.4  # 满级可充能建筑被充能的基础概率（乘以 0.5+进度）
RUSH_PROB = 0.2  # 速本玩家占比（较少）
RUSH_LAG = 3  # 速本非关键建筑最低停在几本以前的满级
LOW_TAIL = 0.1  # 每个建筑/单位独立地取"低于上一本满级"等级的概率
HERO_HALL_MAX_PROB = 0.75  # 英雄殿堂直接升到本本满级的概率（其余按一般规则随机）


def _is_key(d: dict) -> bool:
    """速本也会优先升级的关键建筑：大本营与军事类建筑（兵营、实验室、部落城堡、英雄殿堂、铁匠铺……）。"""
    return d.get("building_class") in ("Town Hall", "Army")


def _pick_level(rng: random.Random, prev: int, cap: int, progress: float) -> int:
    """等级：主要在 [上一本满级, cap] 之间、按进度偏向一端。每项独立地以 LOW_TAIL 概率取更低的等级：
    一半贴近上一本满级（三角分布），一半在 [1, 上一本满级-1] 均匀，所以 1 级这种很低的等级也有机会出现。"""
    if cap <= 0:
        return 0
    lo = max(1, min(prev, cap))
    if lo > 1 and rng.random() < LOW_TAIL:
        if rng.random() < 0.5:
            return max(1, min(lo - 1, round(rng.triangular(1, lo - 1, lo - 1))))
        return rng.randint(1, lo - 1)
    if cap == lo:
        return cap
    x = min(1.0, max(0.0, rng.gauss(progress, 0.22)))
    return lo + round(x * (cap - lo))


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


def _merge_levels(entries: list[tuple[int, int]], levels: dict[int, int], prog: Progression, kinds: tuple[str, ...]):
    """按原列表顺序写回等级；属于该种类的新单位追加在末尾；等级为 0 的移除（原列表中不认识的条目保持原样）。"""
    out = []
    seen = set()
    for gid, v in entries:
        seen.add(gid)
        u = prog.units.get(gid)
        if u is None or u["kind"] not in kinds:
            out.append((gid, v))  # 夜世界/内部单位等：保持原样
            continue
        lv = levels.get(gid, 0)
        if lv > 0:
            out.append((gid, lv - 1))
    for gid, lv in sorted(levels.items()):
        u = prog.units.get(gid)
        if gid in seen or u is None or u["kind"] not in kinds or lv <= 0 or u.get("derived_level"):
            continue
        out.append((gid, lv - 1))
    return out


def encode_cc_list(entries: list[tuple[int, int, int]]) -> bytes:
    """防守方援军列表：int32 条数 + 每条 {int32 GlobalID, VInt 数量, VInt 等级-1}。"""
    import struct

    out = bytearray(struct.pack(">i", len(entries)))
    for g, n, lv0 in entries:
        out += struct.pack(">i", g) + ac.write_vint(n) + ac.write_vint(lv0)
    return bytes(out)


def _parse_cc_list(buf: bytes, o: int):
    import struct

    if o + 4 > len(buf):
        return None
    n = struct.unpack_from(">i", buf, o)[0]
    if not 0 <= n <= 64:
        return None
    p = o + 4
    ents = []
    try:
        for _ in range(n):
            g = struct.unpack_from(">i", buf, p)[0]
            if g // 1_000_000 not in (4, 26) or g % 1_000_000 > 999:
                return None
            c, p = ac.read_vint(buf, p + 4)
            lv, p = ac.read_vint(buf, p)
            if not (0 < c <= 300 and 0 <= lv < 60):
                return None
            ents.append((g, c, lv))
    except (IndexError, struct.error):
        return None
    return p, ents


def defender_cc_span(payload: bytes) -> tuple[int, int] | None:
    """防守方 avatar 中援军列表的字节区间（18.647：紧跟在英雄状态列表之后，见 docs/models.md）。
    先按模板 JSON 的 alliance_units 内容定位；为空时取防守方区域里最后一个 tbl28 列表之后的位置。"""
    found = hp.find_compressed_json(payload, require_key=None)
    start = found[-1].data_end if found else 0
    try:
        end = ac.find_chain(payload).lists["unit_counts"].start
    except ValueError:
        end = len(payload)
    try:
        home = hp.extract_home(payload)
        want = [(u["id"], u["cnt"], u["lvl"]) for u in home.get("alliance_units", [])]
    except ValueError:
        want = []
    if want:
        pat = encode_cc_list(want)
        i = payload.find(pat, start, end)
        if i >= 0:
            return i, i + len(pat)
    # 结构定位：防守方区域内连续的 tbl28 列表（英雄等级/生命/状态……）之后紧跟援军列表
    o, last_end = start, None
    while o < end - 4:
        sl = ac.parse_list(payload, o, 28)
        if sl and sl.entries and sl.end <= end:
            last_end = sl.end
            o = sl.end
            continue
        o += 1
    if last_end is not None:
        r = _parse_cc_list(payload, last_end)
        if r is not None:
            return last_end, r[0]
    return None


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
            sl = ac.find_equipment_list(payload, o, end)
            if sl is not None:
                out["equipment"] = sl
            break
        o += 1
    return out
