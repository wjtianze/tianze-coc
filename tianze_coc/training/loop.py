"""最小训练循环：证明 环境 → 策略 → 奖励 → 更新 全链路可以在真实游戏上跑起来。

    python -m tianze_coc.training.loop --episodes 30 --home records/x.OwnHomeData.bin --enemy records/x.NpcData.bin

策略：对每个兵种在离散部署网格（grid×grid 个格点）上的 softmax（无状态，REINFORCE + 基线）。
它只是占位实现；GPU 上的神经网络策略实现同样的 `act / update` 接口即可替换。

每局：离线重放开局 → 依次部署军队中的每个单位（位置由策略采样）→ 锁步推进到战斗结束 →
奖励 = 星数 + 摧毁率/100 → 更新策略。每局写一行 JSON 日志（episodes.jsonl），定期保存参数。
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..battle_env import EnvConfig, TianzeBattleEnv
from ..contracts import ActionV1, ObservationV1
from ..local_config import DATA, ROOT
from ..offline_session import OfflineStarter


@dataclass
class GridPolicy:
    grid: int = 8
    lr: float = 0.5
    logits: dict[int, np.ndarray] = field(default_factory=dict)  # 单位 GlobalID -> [grid*grid]
    baseline: float = 0.0
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))

    def _probs(self, unit: int) -> np.ndarray:
        z = self.logits.setdefault(unit, np.zeros(self.grid * self.grid))
        p = np.exp(z - z.max())
        return p / p.sum()

    def act(self, unit: int, obs: ObservationV1) -> tuple[int, float, float]:
        cell = int(self.rng.choice(self.grid * self.grid, p=self._probs(unit)))
        gx, gy = divmod(cell, self.grid)
        step_x = obs.width_tiles / self.grid
        step_y = obs.height_tiles / self.grid
        x = (gx + self.rng.uniform(0.2, 0.8)) * step_x
        y = (gy + self.rng.uniform(0.2, 0.8)) * step_y
        return cell, float(x), float(y)

    def update(self, choices: list[tuple[int, int]], reward: float) -> None:
        adv = reward - self.baseline
        self.baseline += 0.1 * (reward - self.baseline)
        for unit, cell in choices:
            p = self._probs(unit)
            g = -p
            g[cell] += 1.0
            self.logits[unit] += self.lr * adv * g

    def save(self, path: Path) -> None:
        np.savez(path, baseline=self.baseline, **{str(k): v for k, v in self.logits.items()})


def run_episode(env: TianzeBattleEnv, policy: GridPolicy, max_steps: int = 4000) -> dict:
    t0 = time.time()
    obs = env.reset()
    plan = [(kind, g) for kind, pool in (("troop", obs.army_units), ("siege", obs.army_sieges),
                                          ("spell", obs.army_spells)) for g, c in pool.items()
            for _ in range(c) if env.catalog.is_allowed(g)]
    choices, actions = [], []
    for kind, g in plan:
        cell, x, y = policy.act(g, obs)
        choices.append((g, cell))
        actions.append(ActionV1(kind, g, x, y))
    executed = rejected = 0
    obs, info, done = env.step(actions, subticks=max(5, len(actions)))
    rejected += len(info.rejected_locally)
    n = 0
    while not done and n < max_steps:
        executed += sum(r.status == "executed" for r in info.results)
        rejected += sum(r.status == "rejected" for r in info.results)
        obs, info, done = env.step(subticks=40)
        n += 1
    executed += sum(r.status == "executed" for r in info.results)
    reward = obs.stars + obs.destruction / 100.0
    policy.update(choices, reward)
    return {"reward": reward, "stars": obs.stars, "destruction": obs.destruction, "ticks": obs.tick,
            "deployed": len(actions), "executed": executed, "rejected": rejected,
            "seconds": round(time.time() - t0, 2), "done": done}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", type=Path, default=DATA / "records" / "tutorial.OwnHomeData.bin")
    ap.add_argument("--enemy", type=Path, default=DATA / "records" / "tutorial.NpcData.bin")
    ap.add_argument("--enemy-kind", default="NpcData")
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--turbo", type=int, default=16)
    ap.add_argument("--out", type=Path, default=ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    starter = OfflineStarter(a.home, a.enemy, enemy_kind=a.enemy_kind, block_outgoing=True, allow_types=[10108])
    env = TianzeBattleEnv(EnvConfig(include_objects=False, turbo=a.turbo), battle_starter=starter)
    policy = GridPolicy()
    log = (a.out / "episodes.jsonl").open("w", encoding="utf-8")
    try:
        for ep in range(a.episodes):
            r = run_episode(env, policy)
            r["episode"] = ep
            r["baseline"] = round(policy.baseline, 3)
            log.write(json.dumps(r) + "\n")
            log.flush()
            print(r)
            if (ep + 1) % 10 == 0:
                policy.save(a.out / "policy.npz")
    finally:
        policy.save(a.out / "policy.npz")
        env.close()
        log.close()


if __name__ == "__main__":
    main()
