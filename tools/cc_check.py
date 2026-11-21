"""真机验证防守部落城堡援军注入：在城堡旁边下兵引出援军，检查出城的防守兵种是否就是注入的那几种。

    python tools/cc_check.py --home <OwnHomeData> --template <EnemyHomeData> [--th 12]
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

CC_GID = 1000014


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--th", type=int, default=12)
    a = ap.parse_args()
    rng = random.Random(3)
    tpl = Path(a.template).read_bytes()
    d = Account.from_payload(tpl, "defender")
    d.max_out(th=a.th)
    # 只留大本营和部落城堡，城堡放在左下角附近，便于引出援军
    d.structures = [s for s in d.structures if s.gid in (1000001, CC_GID)]
    for s in d.structures:
        s.x, s.y = (8, 8) if s.gid == CC_GID else (30, 30)
    lay = L.Layout.of(d)
    giant, goblin = 4000003, 4000002
    lay.cc = [(giant, 2, 3), (goblin, 5, 2)]  # 低等级、易辨认的组合
    att = Account.from_payload(Path(a.home).read_bytes(), "own")
    att.max_out()
    army = A.Army(troops={4000000: 40})
    payload = S.build_enemy_payload(tpl, S.Match(d, att, army, lay), strict=False)
    env = TianzeBattleEnv(EnvConfig(fair_view=False, turbo=8), battle_starter=OfflineStarter(
        Path(a.home), Path(a.template), payload_fn=lambda i: payload, allow_types=[10108]))
    seen: Counter = Counter()
    try:
        env.reset()
        for k in range(40):
            env.step([ActionV1("troop", 4000000, 3.0 + (k % 8) * 0.3, 3.0 + (k // 8) * 0.3)], subticks=2)
        for _ in range(10):
            obs, _, done = env.step([], subticks=60)
            for e in obs.entities:
                if e.fields.get("team") == 1 and e.data_id // 1_000_000 == 4:
                    seen[e.global_id] = e.data_id
            if done:
                break
        kinds = Counter(seen.values())
        print("注入：", lay.cc)
        print("出城的防守兵种：", dict(kinds))
        ok = set(kinds) == {giant, goblin} and kinds.get(giant, 0) <= 2 and kinds.get(goblin, 0) <= 5
        print("结论：", "一致" if ok else "不一致")
    finally:
        c = env.client
        c.config(lockstep=False)
        c.replay(message_ids()["OwnHomeData"], timeout_ms=20000)
        time.sleep(1)


if __name__ == "__main__":
    main()
