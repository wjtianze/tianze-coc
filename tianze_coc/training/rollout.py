"""对局采集：每个采集进程连一个模拟器（探针端口），不断组装对局、用当前策略打完、回传轨迹。

多机部署：每台模拟器机器上的探针端口通过 adb forward（或 SSH 隧道）映射到训练机，
训练机按 `--envs host:port,host:port,...` 启动对应数量的采集进程（docs/model_plan.md 第 8 节）。

对局由 `MatchSampler` 生成：双方大本营独立抽样（含跨本与极端差距）、发育程度随机、阵型随机或来自摆阵模型、
配兵随机或来自配兵模型、难度模式按课程比例抽样（docs/model_plan.md 1b、1c、7 节）。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import army as army_mod
from .. import battle_modifier
from .. import layout as layout_mod
from .. import selfplay
from ..account import Account
from ..battle_env import EnvConfig, TianzeBattleEnv
from ..native_client import ProbeClient
from ..offline_session import OfflineStarter, message_ids
from ..tensorize import ACTION_TYPES, COUNT_CHOICES, MAP, AttackTensorizer, LevelLookup


@dataclass
class Curriculum:
    """课程：同本比例、最大大本营差、各模式权重随训练进度变化。"""

    same_th: float = 0.7
    max_th_gap: int = 18
    mode_weights: dict[str, float] = field(default_factory=lambda: {"normal": 1.0})
    th_range: tuple[int, int] = (1, 18)
    layout_source: str = "random"  # random / rule / model


@dataclass
class MatchSampler:
    template: bytes  # 任意一份 EnemyHomeData 录制（只作消息骨架）
    own_home: bytes  # 任意一份 OwnHomeData（进攻方账号骨架）
    curriculum: Curriculum = field(default_factory=Curriculum)
    rng: random.Random = field(default_factory=random.Random)
    layout_fn: object = None  # 可选：摆阵模型 (account, mode, rng) -> Layout
    army_fn: object = None  # 可选：配兵模型 (account, mode, defender_or_None, rng) -> Army

    def sample(self) -> tuple[selfplay.Match, dict]:
        c = self.curriculum
        r = self.rng
        lo, hi = c.th_range
        th_a = r.randint(lo, hi)
        if r.random() < c.same_th:
            th_d = th_a
        else:
            th_d = max(lo, min(hi, th_a + r.randint(-c.max_th_gap, c.max_th_gap)))
        d = Account.from_payload(self.template, "defender")
        d.randomize(th_d, r, progress=r.random())
        a = Account.from_payload(self.own_home, "own")
        a.randomize(th_a, r, progress=r.random())
        modes, weights = zip(*c.mode_weights.items())
        mode = r.choices(modes, weights)[0]
        lay = self.layout_fn(d, mode, r) if self.layout_fn else layout_mod.random_layout(d, r)
        lay.cc = layout_mod.random_defense_cc(d, r) if d.counts().get(1000014) else []
        arm = self.army_fn(a, mode, None, r) if self.army_fn else army_mod.random_army(a, r)
        return selfplay.Match(d, a, arm, lay, mode=mode), {"th_a": th_a, "th_d": th_d, "mode": mode}


def reward_fn(prev, obs, done: bool) -> float:
    """星数优先（docs/model_plan.md 1c 节）：过程奖励 = 摧毁率增量；终局 = 星数 × 大权重。"""
    r = (obs.destruction - prev.destruction) / 100.0
    r += (obs.stars - prev.stars) * 1.0
    if done:
        r += obs.stars * 2.0 + (3.0 if obs.stars == 3 else 0.0)
    return r


@dataclass
class Trajectory:
    obs: list[dict]  # 每步张量（不含 unit_kinds）
    actions: list[dict]
    logp: list[float]
    values: list[float]
    rewards: list[float]
    info: dict


class Worker:
    """一个模拟器上的采集器。policy_fn(ob_batch, state) -> (actions, logp, value, state)。"""

    def __init__(self, host: str, port: int, sampler: MatchSampler, home_path: Path, template_path: Path,
                 max_steps: int = 400):
        self.client = ProbeClient(host, port, timeout=30)
        self.sampler = sampler
        self.home_path, self.template_path = home_path, template_path
        self.tz = AttackTensorizer()
        self.max_steps = max_steps

    def run_episode(self, policy_fn) -> Trajectory:
        match, info = self.sampler.sample()
        payload = selfplay.build_enemy_payload(self.sampler.template, match, strict=False)
        env = TianzeBattleEnv(EnvConfig(fair_view=True, turbo=8), client=self.client,
                              battle_starter=OfflineStarter(self.home_path, self.template_path,
                                                            payload_fn=lambda i: payload, allow_types=[10108]))
        lk = LevelLookup.from_match(match.defender, match.attacker)
        heroes_left = [h.gid for h in match.army.heroes]
        traj = Trajectory([], [], [], [], [], info)
        state = None
        try:
            obs = env.reset()
            for _ in range(self.max_steps):
                st = {"legal": env.legal_units(obs), "heroes_left": [h for h in heroes_left
                                                                     if h not in env._heroes_deployed]}
                ob = self.tz.encode(obs, lk, match.mode, info["th_a"], info["th_d"], st)
                kinds = ob.pop("unit_kinds")
                act, logp, value, state = policy_fn(ob, state)
                remaining = 1
                if act["type"] > 0 and act["unit"] < len(kinds):
                    k, g = kinds[act["unit"]]
                    pool = {"troop": obs.army_units, "spell": obs.army_spells, "siege": obs.army_sieges}.get(k, {})
                    remaining = pool.get(g, 1)
                acts, sub = self.tz.decode(act["type"], act["unit"], act["cell"], act["count"], act["wait"], kinds,
                                           remaining)
                prev = obs
                obs, _info, done = env.step(acts, subticks=sub)
                traj.obs.append(ob)
                traj.actions.append(act)
                traj.logp.append(logp)
                traj.values.append(value)
                traj.rewards.append(reward_fn(prev, obs, done))
                if done:
                    break
            else:
                # 步数用完：把剩余时间一次推进到底
                while not obs.battle_over and obs.in_battle:
                    prev = obs
                    obs, _, done = env.step([], subticks=600)
                    traj.rewards[-1] += reward_fn(prev, obs, done)
                    if done:
                        break
            traj.info.update(stars=obs.stars, destruction=obs.destruction)
        finally:
            try:
                self.client.config(lockstep=False)
                self.client.replay(message_ids()["OwnHomeData"], timeout_ms=20000)
            except Exception:  # noqa: BLE001
                pass
        return traj


def stack(obs_list: list[dict]) -> dict[str, np.ndarray]:
    return {k: np.stack([o[k] for o in obs_list]) for k in obs_list[0]}
