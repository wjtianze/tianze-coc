"""观测 / 配兵请求 / 阵型 -> 定长张量，以及进攻动作的屏蔽与解码（只依赖 numpy）。

进攻模型的输入（docs/model_plan.md 第 2、3 节）：
- entities  [E, F]：场上每个对象一个 token = 特征表向量（属性 + 标签 + 种类 + 等级）
                    + [x/50, y/50, 血量比例, 是否进攻方, 是否已摧毁]；mask [E]
- units     [U, F+3]：本局可用的部署选项（兵种/攻城/法术/英雄/援军/技能/回溯）= 特征 + [剩余数量, 动作类别, 是否可用]
- spatial   [C, 50, 50]：防守建筑占用、血量、进攻单位密度、可部署格子
- glob      [G]：模式向量、双方大本营、时间、星数、摧毁率、剩余兵力比例
- 动作屏蔽：类型 [A]、单位 [U]、位置 [50*50]

等级来源：观测只有数据 ID，等级从对局组装时的账号得到（`LevelLookup`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .contracts import LOGIC_UNITS_PER_TILE, ActionV1, ObservationV1
from .features import FeatureTable

MAP = 50
MAX_ENTITIES = 640
MAX_UNITS = 48
ACTION_TYPES = ("wait", "troop", "siege", "spell", "hero", "cc", "cc_siege", "cc_spell", "ability", "recalled")
WAIT_CHOICES = (1, 5, 20, 60)  # 等待子帧数的离散档
COUNT_CHOICES = (1, 2, 4, 999)  # 一次部署的数量档（999 = 全部）
SPATIAL_CHANNELS = ("def_occupied", "def_hp", "def_is_defense", "att_density", "deployable", "wall")


@dataclass
class LevelLookup:
    """对象等级查询：防守建筑按 (gid, 格子) 匹配，进攻单位按 gid。"""

    structures: dict[tuple[int, int, int], int] = field(default_factory=dict)  # (gid, x, y) -> 等级
    struct_default: dict[int, int] = field(default_factory=dict)  # gid -> 最常见等级
    units: dict[int, int] = field(default_factory=dict)  # gid -> 等级

    @classmethod
    def from_match(cls, defender, attacker) -> "LevelLookup":
        lk = cls()
        from collections import Counter

        per: dict[int, Counter] = {}
        for s in defender.structures:
            lk.structures[(s.gid, s.x, s.y)] = s.level
            per.setdefault(s.gid, Counter())[s.level] += 1
        lk.struct_default = {g: c.most_common(1)[0][0] for g, c in per.items()}
        lk.units = dict(attacker.unit_levels)
        return lk

    def level(self, gid: int, tx: int, ty: int, team: int) -> int:
        if team == 0:
            return self.units.get(gid, 1)
        return self.structures.get((gid, tx, ty), self.struct_default.get(gid, 1))


def mode_vector(prog, mode: str) -> np.ndarray:
    """难度模式的数值向量（docs/model_plan.md 1.2 节）。"""
    from .battle_modifier import MODES

    names = {"esports": "Hard Mode", "legend1": "Minus One", "legend2": "Minus Two", "legend3": "Minus Three"}
    v = np.zeros(11 + len(MODES), dtype=np.float32)
    t = prog.data.get("battle_modifiers", {}).get(names.get(mode, ""), None)
    if t:
        for e in t["effects"]:
            d, h = e["damage_pct"] / 100 - 1, e["hp_pct"] / 100 - 1
            if e["side"] == "defender" and e["target"] == "building":
                v[0], v[1], v[10] = d, h, float(e["spells"])
            elif e["side"] == "defender" and e["target"] == "hero":
                v[2], v[3] = d, h
            elif e["target"] == "guardian":
                v[4], v[5] = d, h
            elif e["side"] == "attacker" and e["target"] == "hero":
                v[6], v[7] = d, h
        v[8], v[9] = t["common_equipment_minus"] / 10, t["epic_equipment_minus"] / 10
    keys = list(MODES)
    v[11 + keys.index(mode if mode in keys else "normal")] = 1.0
    return v


class AttackTensorizer:
    def __init__(self, features: FeatureTable | None = None, prog=None):
        from .assets.progression import Progression

        self.ft = features or FeatureTable.load()
        self.prog = prog or Progression.load()
        self.F = self.ft.dim
        self.E_DIM = self.F + 5
        self.U_DIM = self.F + 3

    # ------------------------------------------------------------------ 观测
    def encode(self, obs: ObservationV1, lk: LevelLookup, mode: str, th_att: int, th_def: int,
               env_state: dict | None = None) -> dict[str, np.ndarray]:
        ents = np.zeros((MAX_ENTITIES, self.E_DIM), dtype=np.float32)
        emask = np.zeros(MAX_ENTITIES, dtype=np.float32)
        spatial = np.zeros((len(SPATIAL_CHANNELS), MAP, MAP), dtype=np.float32)
        occupied = np.zeros((MAP, MAP), dtype=bool)
        for i, e in enumerate(obs.entities[:MAX_ENTITIES]):
            f = e.fields
            x, y = f.get("x"), f.get("y")
            if x is None or y is None:
                continue
            tx, ty = x / LOGIC_UNITS_PER_TILE, y / LOGIC_UNITS_PER_TILE
            team = f.get("team") or 0
            hp, mx = f.get("hp") or 0, f.get("hp_max") or 0
            ratio = hp / mx if mx else 1.0
            ent_def = self.prog.structure(e.data_id) if team == 1 else None
            w = ent_def["width"] if ent_def else 1
            h = ent_def["height"] if ent_def else 1
            gx0, gy0 = int(round(tx - w / 2)), int(round(ty - h / 2))  # 对象坐标是中心点
            lvl = lk.level(e.data_id, gx0, gy0, team)
            ents[i, :self.F] = self.ft.feature(e.data_id, lvl)
            ents[i, self.F:] = [tx / MAP, ty / MAP, ratio, float(team == 0), float(mx > 0 and hp <= 0)]
            emask[i] = 1.0
            if team == 1 and ent_def:
                xs, ys = slice(max(gx0, 0), min(gx0 + w, MAP)), slice(max(gy0, 0), min(gy0 + h, MAP))
                if hp > 0 or mx == 0:
                    spatial[0, ys, xs] = 1.0
                    spatial[1, ys, xs] = ratio
                    spatial[2, ys, xs] = float(self.ft.entries.get(e.data_id, {}).get("kind") == "defense")
                    spatial[5, ys, xs] = float(ent_def["category"] == "wall")
                    occupied[max(gy0 - 1, 0):min(gy0 + h + 1, MAP), max(gx0 - 1, 0):min(gx0 + w + 1, MAP)] = True
            elif team == 0:
                ix, iy = int(tx), int(ty)
                if 0 <= ix < MAP and 0 <= iy < MAP:
                    spatial[3, iy, ix] += 0.1
        spatial[4] = (~occupied).astype(np.float32)  # 建筑周围一格内不能部署
        units, umask, kinds = self._units(obs, lk, env_state or {})
        glob = self._global(obs, mode, th_att, th_def)
        utm = np.zeros((len(ACTION_TYPES), MAX_UNITS), dtype=np.float32)
        for i, (k, _g) in enumerate(kinds):
            utm[ACTION_TYPES.index(k), i] = 1.0
        return {"entities": ents, "entity_mask": emask, "units": units, "unit_mask": umask, "spatial": spatial,
                "glob": glob, "unit_kinds": kinds, "type_mask": self.type_mask(umask, kinds),
                "unit_type_mask": utm, "deploy_mask": spatial[4].reshape(-1).copy()}

    def _units(self, obs: ObservationV1, lk: LevelLookup, st: dict):
        """部署选项列表：每项 = (动作类别, gid)，与 legal_units 对齐。"""
        legal = st.get("legal") or {}
        opts: list[tuple[str, int, int]] = []
        for k, pool in (("troop", obs.army_units), ("siege", obs.army_sieges), ("spell", obs.army_spells)):
            for g, c in pool.items():
                if c > 0 and (not legal or g in legal.get(k, [])):
                    opts.append((k, g, c))
        for g in st.get("heroes_left", []):
            opts.append(("hero", g, 1))
        for k in ("cc", "cc_siege", "cc_spell", "ability"):
            for g in legal.get(k, []):
                opts.append((k, g, obs.cc_units.get(g, 1)))
        units = np.zeros((MAX_UNITS, self.U_DIM), dtype=np.float32)
        umask = np.zeros(MAX_UNITS, dtype=np.float32)
        kinds: list[tuple[str, int]] = []
        for i, (k, g, c) in enumerate(opts[:MAX_UNITS]):
            lvl = obs.cc_levels.get(g, 1) if k.startswith("cc") else lk.units.get(g, 1)
            units[i, :self.F] = self.ft.feature(g, lvl) if g > 1 else 0.0
            units[i, self.F:] = [min(c, 400) / 100.0, ACTION_TYPES.index(k) / len(ACTION_TYPES), 1.0]
            umask[i] = 1.0
            kinds.append((k, g))
        return units, umask, kinds

    def _global(self, obs: ObservationV1, mode: str, th_att: int, th_def: int) -> np.ndarray:
        mv = mode_vector(self.prog, mode)
        tot = sum(obs.army_units.values()) + sum(obs.army_spells.values()) + sum(obs.army_sieges.values())
        g = [th_att / self.prog.th_max, th_def / self.prog.th_max, (th_att - th_def) / self.prog.th_max,
             min(obs.tick, 3600 * 60) / (180 * 60), obs.stars / 3.0, obs.destruction / 100.0, float(obs.th_destroyed),
             min(tot, 400) / 400.0]
        return np.concatenate([mv, np.asarray(g, dtype=np.float32)])

    def glob_dim(self) -> int:
        return len(mode_vector(self.prog, "normal")) + 8

    @staticmethod
    def type_mask(umask: np.ndarray, kinds: list[tuple[str, int]]) -> np.ndarray:
        m = np.zeros(len(ACTION_TYPES), dtype=np.float32)
        m[0] = 1.0  # 等待总是可选
        for k, _g in kinds:
            m[ACTION_TYPES.index(k)] = 1.0
        return m

    # ------------------------------------------------------------------ 动作
    @staticmethod
    def decode(type_idx: int, unit_idx: int, cell: int, count_idx: int, wait_idx: int,
               kinds: list[tuple[str, int]], remaining: int = 1) -> tuple[list[ActionV1], int]:
        """模型输出 -> (动作列表, 推进子帧数)。部署类动作按数量档展开成多个相邻格子的部署。"""
        t = ACTION_TYPES[type_idx]
        if t == "wait" or unit_idx >= len(kinds):
            return [], WAIT_CHOICES[wait_idx]
        k, g = kinds[unit_idx]
        x, y = cell % MAP + 0.5, cell // MAP + 0.5
        if k in ("ability",):
            return [ActionV1("ability", g)], 1
        if k in ("hero", "cc", "cc_siege", "siege"):
            return [ActionV1(k, g, x, y)], 1
        n = min(COUNT_CHOICES[count_idx], max(1, remaining))
        acts = [ActionV1(k, g, x + 0.3 * (i % 3), y + 0.3 * (i // 3 % 3)) for i in range(n)]
        return acts, 1
