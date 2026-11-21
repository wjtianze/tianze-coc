"""真机动作测试：离线重放一局，依次执行一组动作，打印引擎结果、场上角色与命令侦听记录。

    python tools/live_action_test.py --plan cc_siege      # 内置方案见 PLANS
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tianze_coc.battle_env import EnvConfig, TianzeBattleEnv  # noqa: E402
from tianze_coc.contracts import ActionV1  # noqa: E402
from tianze_coc.local_config import LocalConfig  # noqa: E402
from tianze_coc.native_client import ProbeClient  # noqa: E402
from tianze_coc.offline_session import OfflineStarter  # noqa: E402

R = ROOT / "tianze_coc" / "data" / "records"
PLANS = {
    "cc_siege": [
        ("援军攻城机器 攻城飞艇", ActionV1("cc_siege", 4000052, 4.0, 25.0)),
        ("再下自己的攻城机器（环境应拒绝）", ActionV1("siege", 4000062, 4.0, 30.0)),
        ("攻城飞艇释放", ActionV1("ability", 4000052)),
        ("援军法术 弹跳", ActionV1("cc_spell", 26000003, 10.0, 25.0)),
        ("援军法术 冰冻", ActionV1("cc_spell", 26000005, 12.0, 25.0)),
        ("英雄 飞龙公爵", ActionV1("hero", 28000007, 4.0, 20.0)),
        ("技能 飞龙公爵", ActionV1("ability", 28000007)),
    ],
    "modes_recall": [
        ("大守护者切空中模式", ActionV1("hero_mode", 28000002, flag=1)),
        ("部署大守护者", ActionV1("hero", 28000002, 4.0, 22.0)),
        ("部署后再切模式（环境应拒绝）", ActionV1("hero_mode", 28000002, flag=0)),
        ("部署飞龙", ActionV1("troop", 4000008, 4.0, 28.0)),
        ("回溯法术", ActionV1("spell", 26000053, 5.5, 28.0)),
        ("重新部署回溯单位", ActionV1("recalled", x=4.0, y=32.0, flag=0, extra=0)),
    ],
    "th19_new": [
        ("小队 野蛮人弓箭手小队", ActionV1("troop", 4000195, 4.0, 25.0)),
        ("再下一个小队", ActionV1("troop", 4000195, 4.0, 30.0)),
        ("英勇法术", ActionV1("spell", 26000153, 6.0, 27.0)),
        ("英雄 野蛮人之王", ActionV1("hero", 28000000, 4.0, 20.0)),
        ("技能 野蛮人之王", ActionV1("ability", 28000000)),
    ],
    "siege_release": [
        ("攻城钻机", ActionV1("siege", 4000092, 4.0, 25.0)),
        ("攻城钻机释放", ActionV1("ability", 4000092)),
        ("英雄 飞盾战神", ActionV1("hero", 28000004, 4.0, 20.0)),
        ("技能 飞盾战神", ActionV1("ability", 28000004)),
    ],
}


def wait_home(timeout: float = 150) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if ProbeClient(timeout=5).connect().status().get("gm_state") == 1:
                time.sleep(4)
                return
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit("等待主村超时")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="cc_siege", choices=list(PLANS))
    ap.add_argument("--launch", action="store_true", help="先启动客户端并等待主村")
    ap.add_argument("--after", type=int, default=80, help="每个动作后推进的子帧数")
    a = ap.parse_args()
    cfg = LocalConfig.load()
    if a.launch:
        subprocess.run(cfg.adb_cmd("shell", "monkey", "-p", cfg.package, "-c", "android.intent.category.LAUNCHER", "1"),
                       capture_output=True)
        wait_home()
    starter = OfflineStarter(R / "th19_after_ops.OwnHomeData.bin", R / "th19_friendly.EnemyHomeData.bin",
                             enemy_kind="EnemyHomeData", block_outgoing=True, allow_types=[10108], timeout_s=40)
    env = TianzeBattleEnv(EnvConfig(include_objects=True, turbo=8, battle_wait_timeout_s=40), battle_starter=starter)
    env.client.cmd_log(clear=True)
    obs = env.reset()
    print("开局 tick", obs.tick, "可用", {k: v for k, v in env.legal_units(obs).items() if v})
    for name, act in PLANS[a.plan]:
        obs, info, _ = env.step([act], subticks=10)
        obs, info2, _ = env.step(subticks=a.after)
        res = [(r.status, r.code) for r in info.results + info2.results]
        chars = sorted({e.data_id for e in obs.entities if e.object_type == 1})
        print(f"{name}: 本地拒绝={[r for _, r in info.rejected_locally]} 引擎={res}")
        print("    场上角色", chars, "援军剩余", {g: c for g, c in obs.cc_units.items() if c})
    print("命令侦听", [(e["tick"], e["type"], e["data"], e["ours"]) for e in env.client.cmd_log()])
    env.client.config(lockstep=False)


if __name__ == "__main__":
    main()
