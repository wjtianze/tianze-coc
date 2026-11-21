"""真机验证：1～最高本逐本随机生成双方账号、阵型、援军、配兵，离线开局并部署，检查能否正常进行。

    python tools/th_sweep.py --home <OwnHomeData录制> --template <EnemyHomeData录制> [--ths 1-18] [--modes normal,esports]

每本输出：开局是否成功、引擎兵力与注入是否一致、防守建筑数是否一致、部署是否执行、战斗能否推进到结束。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc import army as A  # noqa: E402
from tianze_coc import layout as L  # noqa: E402
from tianze_coc import selfplay as S  # noqa: E402
from tianze_coc.account import Account  # noqa: E402
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv  # noqa: E402
from tianze_coc.contracts import ActionV1  # noqa: E402
from tianze_coc.offline_session import OfflineStarter, message_ids  # noqa: E402


def parse_range(s: str) -> list[int]:
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def one(home: Path, tpl_path: Path, th: int, mode: str, seed: int) -> dict:
    rng = random.Random(seed)
    tpl = tpl_path.read_bytes()
    d = Account.from_payload(tpl, "defender")
    d.randomize(th, rng, progress=rng.random())
    att = Account.from_payload(home.read_bytes(), "own")
    att.randomize(th, rng, progress=rng.random())
    lay = L.random_layout(d, rng)
    lay.cc = L.random_defense_cc(d, rng) if d.counts().get(1000014) else []
    army = A.random_army(att, rng)
    m = S.Match(d, att, army, lay, mode=mode)
    errs = S.check(m)
    if errs:
        return {"th": th, "mode": mode, "ok": False, "stage": "校验", "err": errs[:3]}
    payload = S.build_enemy_payload(tpl, m)
    env = TianzeBattleEnv(EnvConfig(fair_view=False, turbo=8), battle_starter=OfflineStarter(
        home, tpl_path, payload_fn=lambda i: payload, allow_types=[10108]))
    res = {"th": th, "mode": mode, "ok": False}
    try:
        obs = env.reset()
        res["army_ok"] = {k: v for k, v in obs.army_units.items() if v > 0} == {k: v for k, v in army.troops.items() if v > 0}
        objs = Counter(e.data_id for e in obs.entities if e.data_id // 1_000_000 == 1)
        want = Counter(s.gid for s in d.structures if s.kind == "buildings")
        res["buildings_ok"] = objs == want
        # 把所有兵种从地图边缘下完，推进到战斗结束
        acts = []
        for g, n in army.troops.items():
            for k in range(min(n, 30)):
                acts.append(ActionV1("troop", g, 1.0 + (k % 40), 1.0))
        done = False
        steps = 0
        executed = 0
        while acts and not done and steps < 400:
            batch, acts = acts[:1], acts[1:]
            obs, info, done = env.step(batch, subticks=2)
            executed += sum(1 for r in info.results if r.status == "executed")
            steps += 1
        t0 = time.time()
        while not done and time.time() - t0 < 120:
            obs, info, done = env.step([], subticks=200)
        res.update(ok=True, executed=executed, finished=done, stars=obs.stars, destruction=obs.destruction)
    except Exception as e:  # noqa: BLE001
        res["err"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            c = env.client
            c.config(lockstep=False)
            c.replay(message_ids()["OwnHomeData"], timeout_ms=20000)
            time.sleep(1)
        except Exception as e:  # noqa: BLE001
            res["cleanup_err"] = str(e)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--ths", default="1-18")
    ap.add_argument("--modes", default="normal")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    for th in parse_range(a.ths):
        for mode in a.modes.split(","):
            r = one(Path(a.home), Path(a.template), th, mode, a.seed * 100 + th)
            print(r, flush=True)


if __name__ == "__main__":
    main()
