"""真机验证战斗属性调整（难度模式）注入：同一模板分别以各模式离线开局，部署英雄后比较双方英雄的生命上限。

    python tools/modifier_check.py --home <OwnHomeData录制> --template <EnemyHomeData录制>
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc import battle_modifier as bm  # noqa: E402
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv  # noqa: E402
from tianze_coc.contracts import ActionV1  # noqa: E402
from tianze_coc.offline_session import OfflineStarter, message_ids  # noqa: E402


def measure(home: Path, template: Path, mode: str, hero: int) -> dict:
    payload = bm.set_modifier(template.read_bytes(), mode)
    env = TianzeBattleEnv(EnvConfig(fair_view=False), battle_starter=OfflineStarter(
        home, template, payload_fn=lambda i: payload, allow_types=[10108]))
    try:
        obs = env.reset()
        defenders = {e.data_id: e.fields.get("hp_max") for e in obs.entities
                     if e.data_id // 1_000_000 == 28 and e.fields.get("team") == 1}
        obs, info, _ = env.step([ActionV1("hero", hero, 2.0, 2.0)], subticks=20)
        attackers = [e.fields.get("hp_max") for e in obs.entities
                     if e.data_id == hero and e.fields.get("team") == 0]
        return {"mode": mode, "attacker_hero_hp_max": attackers[:1], "defender_heroes_hp_max": defenders,
                "deploy": [r.status for r in info.results]}
    finally:
        c = env.client
        c.config(lockstep=False)
        c.replay(message_ids()["OwnHomeData"], timeout_ms=20000)
        time.sleep(2)
        c.net_config(offline=False, block_all=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--hero", type=int, default=28000000)
    ap.add_argument("--modes", default="normal,legend3,legend2,legend1,esports")
    a = ap.parse_args()
    rows = [measure(Path(a.home), Path(a.template), m, a.hero) for m in a.modes.split(",")]
    base = rows[0]
    for r in rows:
        print(r)
    b_att = (base["attacker_hero_hp_max"] or [None])[0]
    for r in rows[1:]:
        att = (r["attacker_hero_hp_max"] or [None])[0]
        if b_att and att:
            print(f"{r['mode']}: 进攻方英雄生命上限 = 普通的 {att / b_att:.3f} 倍")
        for g, hp in r["defender_heroes_hp_max"].items():
            bh = base["defender_heroes_hp_max"].get(g)
            if bh and hp:
                print(f"{r['mode']}: 防守英雄 {g} 生命上限 = 普通的 {hp / bh:.3f} 倍")


if __name__ == "__main__":
    main()
