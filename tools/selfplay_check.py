"""真机验证：自对弈负载（防守方账号/阵型/援军 + 进攻方配兵）离线开局后，引擎里的数据是否与注入一致。

    python tools/selfplay_check.py --home runs/caps/own_after6.bin --template tianze_coc/data/records/th18_friendly.EnemyHomeData.bin

结束后自动回主村并恢复联网。
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--th", type=int, default=16)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    tpl = Path(a.template).read_bytes()
    d = Account.from_payload(tpl, "defender")
    att_tpl = Account.from_payload(tpl, "attacker")
    d.unit_levels.update({g: l for g, l in att_tpl.unit_levels.items() if g not in d.unit_levels})
    d.randomize(a.th, rng, progress=0.6)
    lay = L.random_layout(d, rng)
    lay.cc = L.random_defense_cc(d, rng)
    own = Account.from_payload(Path(a.home).read_bytes(), "own")
    own.max_out()
    army = A.random_army(own, rng)
    payload = S.build_enemy_payload(tpl, S.Match(d, own, army, lay))
    print("注入：防守方", a.th, "本，建筑", len(d.structures), "座；援军", lay.cc)
    print("注入：配兵", army.troops, army.spells, army.sieges, [(h.gid, h.pet) for h in army.heroes])

    env = TianzeBattleEnv(EnvConfig(fair_view=False), battle_starter=OfflineStarter(
        Path(a.home), Path(a.template), payload_fn=lambda i: payload, allow_types=[10108]))
    ok = True
    try:
        obs = env.reset()
        print("开局：tick", obs.tick, "可摧毁", obs.destructible_total)
        for name, got, want in (("兵种", obs.army_units, army.troops), ("法术", obs.army_spells, army.spells),
                                ("攻城", obs.army_sieges, army.sieges)):
            g = {k: v for k, v in got.items() if v > 0}
            same = g == {k: v for k, v in want.items() if v > 0}
            ok &= same
            print(f"  {name}：{'一致' if same else '不一致'} 引擎={g}")
        cc = {k: v for k, v in obs.cc_units.items() if v > 0}
        print("  进攻方援军（模板原样）：", cc)
        # 防守建筑：按数据 ID 计数，与注入的（去掉陷阱/城墙后）比较
        objs = Counter(e.data_id for e in obs.entities if e.data_id // 1_000_000 == 1)
        want = Counter(s.gid for s in d.structures if s.kind == "buildings")
        same = objs == want
        ok &= same
        print(f"  防守建筑：{'一致' if same else '不一致'}（引擎 {sum(objs.values())} / 注入 {sum(want.values())}）")
        if not same:
            print("   差异：", {k: (objs.get(k, 0), want.get(k, 0)) for k in set(objs) | set(want) if objs.get(k, 0) != want.get(k, 0)})
        traps = sum(1 for e in obs.entities if e.data_id // 1_000_000 == 12)
        print("  陷阱对象数（fair_view 关闭时可见）：", traps, "注入", sum(1 for s in d.structures if s.kind == "traps"))
        # 公平视角下陷阱应全部消失
        env.cfg.fair_view = True
        hero = army.heroes[0].gid
        obs2, info, _ = env.step([ActionV1("hero", hero, 2.0, 2.0)], subticks=30)
        print("  部署英雄", hero, [r.status for r in info.results], "；公平视角陷阱数",
              sum(1 for e in obs2.entities if e.data_id // 1_000_000 == 12))
    finally:
        c = env.client
        c.config(lockstep=False)
        ids = message_ids()
        c.replay(ids["OwnHomeData"], timeout_ms=20000)
        time.sleep(2)
        c.net_config(offline=False, block_all=False)
        print("已回主村并恢复联网")
    print("结论：", "全部一致" if ok else "存在不一致")


if __name__ == "__main__":
    main()
