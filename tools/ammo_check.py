"""真机对照实验：需要装填弹药的防御（天鹰火炮、地狱塔、X 连弩、投石炮）在村庄 JSON 里缺少 ammo 字段时是否哑火。

同一阵型、同一军队、同一部署序列，分别以"不写 ammo"和"写满 ammo"离线开打，比较摧毁率、星数、进攻方剩余单位。

    python tools/ammo_check.py --home <OwnHomeData> --template <EnemyHomeData> [--th 16]
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc import army as A  # noqa: E402
from tianze_coc import layout as L  # noqa: E402
from tianze_coc import selfplay as S  # noqa: E402
from tianze_coc.account import Account  # noqa: E402
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv  # noqa: E402
from tianze_coc.contracts import ActionV1  # noqa: E402
from tianze_coc.offline_session import OfflineStarter, message_ids  # noqa: E402


def run(home: Path, tpl_path: Path, payload: bytes, actions: list[ActionV1]) -> dict:
    env = TianzeBattleEnv(EnvConfig(fair_view=False, turbo=8), battle_starter=OfflineStarter(
        home, tpl_path, payload_fn=lambda i: payload, allow_types=[10108]))
    try:
        env.reset()
        for a in actions:
            obs, _, done = env.step([a], subticks=2)
        for _ in range(4):  # 部署完后再推进约 13 秒
            obs, _, done = env.step([], subticks=200)
            if done:
                break
        alive = sum(1 for e in obs.entities if e.fields.get("team") == 0 and (e.fields.get("hp") or 0) > 0)
        return {"destruction": obs.destruction, "stars": obs.stars, "attackers_alive": alive, "tick": obs.tick}
    finally:
        c = env.client
        c.config(lockstep=False)
        c.replay(message_ids()["OwnHomeData"], timeout_ms=20000)
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--th", type=int, default=16)
    ap.add_argument("--only-ammo", action="store_true", help="阵型只保留大本营和需要弹药的防御")
    ap.add_argument("--eagle-only", action="store_true", help="与 --only-ammo 合用：只保留大本营和天鹰火炮")
    a = ap.parse_args()
    rng = random.Random(11)
    tpl = Path(a.template).read_bytes()
    d = Account.from_payload(tpl, "defender")
    d.max_out(th=a.th)
    if a.only_ammo:
        # 只保留大本营和需要装填弹药的防御，放在地图中央；进攻方伤亡只可能来自这些防御
        keep = {1000001} | ({1000031} if a.eagle_only else {x["gid"] for x in d.prog.data["structures"] if x.get("ammo")})
        d.structures = [s for s in d.structures if s.gid in keep]
        x0 = 20
        for i, s in enumerate(d.structures):
            s.x, s.y = x0 + 5 * (i % 4), 22 + 5 * (i // 4)
    lay = L.Layout.of(d) if a.only_ammo else L.random_layout(d, rng)
    att = Account.from_payload(Path(a.home).read_bytes(), "own")
    att.max_out()
    if a.only_ammo:
        unit, n = 4000000, 250  # 野蛮人：250 人口，超过天鹰苏醒门槛 200；放在左下角远离天鹰（最小射程 7 格以外）
    else:
        unit, n = 4000003, 60
    army = A.Army(troops={unit: n})
    acts = [ActionV1("troop", unit, 1.0 + (k % 25) * 0.4, 1.0 + (k // 25) * 0.4) for k in range(n)]
    prog = d.prog
    results = {}
    for label, with_ammo in (("不写 ammo", False), ("写满 ammo", True)):
        dd = Account(d.home, d.unit_levels, prog)
        dd.structures = [type(s)(s.gid, s.level, s.x, s.y, s.kind, dict(s.raw)) for s in d.structures]
        L.apply(lay, dd, check=False)
        for s in dd.structures:
            spec = prog.structure(s.gid) or {}
            s.raw.pop("ammo", None)
            if with_ammo and spec.get("ammo"):
                s.raw["ammo"] = spec["ammo"][s.level - 1]
        payload = S.build_enemy_payload(tpl, S.Match(dd, att, army, None), strict=False)
        results[label] = run(Path(a.home), Path(a.template), payload, acts)
        print(label, results[label], flush=True)


if __name__ == "__main__":
    main()
