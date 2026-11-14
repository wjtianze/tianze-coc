"""TianzeBattleEnv：部落冲突进攻战斗环境（中间层）。

    env = TianzeBattleEnv()
    obs = env.reset()                    # 等待一场进攻战斗开始（由游戏 UI 或外部脚本发起），进入锁步
    obs, info, done = env.step([ActionV1("troop", 4000000, 10.5, 3.0)])

锁步：探针冻结引擎逻辑，每次 step 推进 decision_subticks 个子帧，动作在第一个子帧注入，
与游戏自带 DebugUtil::deployAllTroops 走同一条命令提交路径。

单位规则（见 docs/units.md）：只能部署"部落战/排位战可携带"的单位（永久 + 超级兵）；
限时兵种/法术在 Python 侧与探针侧两层拒绝。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .assets.unit_catalog import TEMPORARY_STATUSES, UnitCatalog
from .contracts import LOGIC_UNITS_PER_TILE, ActionResultV1, ActionV1, ObservationV1
from .local_config import DATA, LocalConfig
from .native_client import ProbeClient, ProbeError

KIND_FOR_CATALOG = {"troop": "troop", "siege": "siege", "spell": "spell", "hero": "hero", "cc_siege": "siege",
                    "cc_spell": "spell"}
ABILITY_KINDS = ("hero", "siege", "troop")  # 技能命令按单位数据查找场上第一个存活的该单位
DUAL_MODE_HEROES = {28000002}  # 大守护者：地面/空中两种模式（heroes.csv HasAltMode）


class EnvError(RuntimeError):
    pass


@dataclass
class EnvConfig:
    decision_subticks: int = 5
    turbo: int = 4  # 每个渲染帧最多推进的子帧数（越大越快）
    submit_mode: str = "client"  # client：同游戏 UI 路径；direct：直接 addCommand（不同步服务器）
    include_objects: bool = True
    battle_wait_timeout_s: float = 600.0
    step_timeout_ms: int = 15000
    layout_path: Path = field(default_factory=lambda: DATA / "layout.json")


@dataclass
class StepInfo:
    results: list[ActionResultV1]
    rejected_locally: list[tuple[ActionV1, str]]
    completed: bool


class TianzeBattleEnv:
    def __init__(self, config: EnvConfig | None = None, client: ProbeClient | None = None,
                 catalog: UnitCatalog | None = None, battle_starter: Callable[["TianzeBattleEnv"], None] | None = None):
        self.cfg = config or EnvConfig()
        lc = LocalConfig.load()
        self.client = client or ProbeClient(port=lc.host_port)
        self.catalog = catalog or UnitCatalog.load()
        self.battle_starter = battle_starter
        self._next_action_id = 1
        self._heroes_deployed: set[int] = set()
        self._deployed_units: set[int] = set()
        self._cc_deployed = False
        self._siege_deployed = False  # 规则：一局只能部署一台攻城机器（自己的或援军的）
        self.last_obs: ObservationV1 | None = None

    # ------------------------------------------------------------ 生命周期
    def handshake(self) -> dict:
        h = self.client.hello()
        if not h.get("ready"):
            raise EnvError(f"探针未就绪: {h.get('init_error')}；指纹不符: {h.get('fingerprint_mismatches')}")
        return h

    def _object_fields(self) -> list[dict]:
        p = Path(self.cfg.layout_path)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8")).get("object_fields", [])

    def reset(self) -> ObservationV1:
        self.handshake()
        self.client.config(lockstep=False, reset_counters=True)
        if self.battle_starter is not None:
            self.battle_starter(self)
        deadline = time.monotonic() + self.cfg.battle_wait_timeout_s
        while not self.client.status().get("in_battle"):
            if time.monotonic() > deadline:
                raise EnvError("等待进攻战斗开始超时（请在游戏内发起一场进攻，或提供 battle_starter）")
            time.sleep(0.25)
        self.client.config(
            lockstep=True,
            turbo=self.cfg.turbo,
            submit_mode=self.cfg.submit_mode,
            allowed_ids=self.catalog.allowed_deploy_ids(),
            object_fields=self._object_fields(),
            reset_counters=True,
        )
        self._heroes_deployed.clear()
        self._deployed_units.clear()
        self._cc_deployed = False
        self._siege_deployed = False
        obs = ObservationV1.from_wire(self.client.observe(objects=self.cfg.include_objects)["obs"])
        self._check_army_rules(obs)
        self.last_obs = obs
        return obs

    def close(self) -> None:
        try:
            self.client.config(lockstep=False)
        except (ProbeError, OSError):
            pass
        self.client.close()

    # ------------------------------------------------------------ 规则
    def _check_army_rules(self, obs: ObservationV1) -> None:
        # 兵力列表包含所有单位类型（数量为 0 的也在），只检查实际携带的
        pools = [g for d in (obs.army_units, obs.army_spells, obs.army_sieges, obs.cc_units) for g, c in d.items() if c > 0]
        banned = [g for g in pools if (u := self.catalog.get(g)) is not None and u.status in TEMPORARY_STATUSES]
        if banned:
            names = [f"{self.catalog.get(g).name_cn or self.catalog.get(g).name}({g})" for g in banned]
            self.client.config(lockstep=False)
            raise EnvError("军队中含有禁用的限时单位：" + "、".join(names))

    def validate(self, a: ActionV1, obs: ObservationV1) -> str | None:
        """返回拒绝原因；None 表示通过。"""
        in_map = 0 <= a.x < obs.width_tiles and 0 <= a.y < obs.height_tiles
        if a.kind == "cc":
            if self._cc_deployed:
                return "部落援军已经部署过"
            if not any(c > 0 and (u := self.catalog.get(g)) is not None and u.kind == "troop"
                       for g, c in obs.cc_units.items()):
                return "部落城堡中没有援军"
            return None if in_map else "坐标超出地图"
        if a.kind == "recalled":
            return None if in_map else "坐标超出地图"
        u = self.catalog.get(a.unit)
        if u is None:
            return "未知单位"
        if not u.allowed:
            return f"单位不可用（{u.status}）"
        if a.kind == "hero_mode":
            if a.unit not in DUAL_MODE_HEROES:
                return "该英雄没有可切换的模式"
            if a.unit in self._heroes_deployed:
                return "英雄已部署，不能再切换模式"
            return None if a.flag in (0, 1) else "模式只能是 0（地面）或 1（空中）"
        if a.kind == "ability":
            if u.kind not in ABILITY_KINDS:
                return f"{u.kind} 没有可触发的技能"
            if a.unit not in self._deployed_units:
                return "该单位尚未部署，无法触发技能"
            return None
        if KIND_FOR_CATALOG.get(a.kind) != u.kind:
            return f"动作类型 {a.kind} 与单位类型 {u.kind} 不符"
        if not in_map:
            return "坐标超出地图"
        if a.kind in ("siege", "cc_siege") and self._siege_deployed:
            return "本局已部署过攻城机器（一局只能部署一台）"
        if a.kind == "troop" and obs.army_units.get(a.unit, 0) <= 0:
            return "军队中没有剩余该单位"
        if a.kind == "siege" and obs.army_sieges.get(a.unit, obs.army_units.get(a.unit, 0)) <= 0:
            return "军队中没有剩余该攻城机器"
        if a.kind == "cc_siege" and obs.cc_units.get(a.unit, 0) <= 0:
            return "部落城堡中没有该攻城机器"
        if a.kind == "cc_spell" and obs.cc_units.get(a.unit, 0) <= 0:
            return "部落援军中没有该法术"
        if a.kind == "spell" and obs.army_spells.get(a.unit, 0) <= 0:
            return "没有剩余该法术"
        if a.kind == "hero" and a.unit in self._heroes_deployed:
            return "该英雄已经部署过"
        return None

    def legal_units(self, obs: ObservationV1 | None = None) -> dict[str, list[int]]:
        obs = obs or self.last_obs
        if obs is None:
            return {}
        return {
            "troop": [g for g, c in obs.army_units.items() if c > 0 and self.catalog.is_allowed(g)
                      and self.catalog.get(g).kind == "troop"],
            "siege": [] if self._siege_deployed else [
                g for g, c in (obs.army_sieges or obs.army_units).items() if c > 0
                and self.catalog.is_allowed(g) and self.catalog.get(g).kind == "siege"],
            "cc_siege": [] if self._siege_deployed else [
                g for g, c in obs.cc_units.items() if c > 0 and self.catalog.is_allowed(g)
                and self.catalog.get(g).kind == "siege"],
            "cc_spell": [g for g, c in obs.cc_units.items() if c > 0 and self.catalog.is_allowed(g)
                         and self.catalog.get(g).kind == "spell"],
            "cc": [1] if not self._cc_deployed and any(
                c > 0 and (u := self.catalog.get(g)) is not None and u.kind == "troop" for g, c in obs.cc_units.items()) else [],
            "ability": sorted(g for g in self._deployed_units if self.catalog.is_allowed(g)),
            "spell": [g for g, c in obs.army_spells.items() if c > 0 and self.catalog.is_allowed(g)],
        }

    # ------------------------------------------------------------ 推进
    def step(self, actions: list[ActionV1] | None = None, subticks: int | None = None
             ) -> tuple[ObservationV1, StepInfo, bool]:
        obs = self.last_obs
        if obs is None:
            raise EnvError("请先 reset()")
        wire, local_rej, sent = [], [], {}
        for a in actions or []:
            why = self.validate(a, obs)
            if why:
                local_rej.append((a, why))
                continue
            if a.kind == "cc_spell" and not a.extra:  # 援军法术按 数据+等级 匹配，等级取自观测
                a = ActionV1(a.kind, a.unit, a.x, a.y, a.flag, obs.cc_levels.get(a.unit, 0))
            aid = self._next_action_id
            self._next_action_id += 1
            wire.append(a.to_wire(aid))
            sent[aid] = a
            if a.kind == "hero":
                self._heroes_deployed.add(a.unit)
            if a.kind == "cc":
                self._cc_deployed = True
            if a.kind in ("siege", "cc_siege"):
                self._siege_deployed = True
        resp = self.client.step(subticks or self.cfg.decision_subticks, wire, objects=self.cfg.include_objects,
                                timeout_ms=self.cfg.step_timeout_ms)
        results = [ActionResultV1.from_wire(r) for r in resp.get("results", [])]
        for r in results:
            a = sent.get(r.id)
            if a is None:
                continue
            if r.status == "rejected":  # 失败则允许重试
                if a.kind == "hero":
                    self._heroes_deployed.discard(a.unit)
                if a.kind == "cc":
                    self._cc_deployed = False
                if a.kind in ("siege", "cc_siege"):
                    self._siege_deployed = False
            elif r.status == "executed" and a.kind in ("troop", "siege", "hero", "cc_siege"):
                self._deployed_units.add(a.unit)
        if resp.get("obs") is None:
            raise EnvError("探针未返回观测（超时）")
        obs = ObservationV1.from_wire(resp["obs"])
        self.last_obs = obs
        done = obs.battle_over or not obs.in_battle
        return obs, StepInfo(results, local_rej, bool(resp.get("completed"))), done


def tiles_to_logic(t: float) -> int:
    return int(round(t * LOGIC_UNITS_PER_TILE))
