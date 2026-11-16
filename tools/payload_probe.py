"""真机试探：重放一条 EnemyHomeData，看能否正常进入战斗（崩溃则自动重启游戏）。

    from tools.payload_probe import try_payload
    try_payload(payload_bytes, home_bytes) -> ("ok", obs) / ("crash", None) / ("timeout", None)
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc.local_config import LocalConfig  # noqa: E402
from tianze_coc.native_client import ProbeClient, ProbeError  # noqa: E402
from tianze_coc.offline_session import message_ids  # noqa: E402


def adb(*args: str) -> str:
    lc = LocalConfig.load()
    return subprocess.run([str(lc.adb), "-s", lc.device, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def restart_game(wait: float = 60) -> ProbeClient:
    lc = LocalConfig.load()
    adb("shell", "am", "force-stop", lc.package)
    time.sleep(1)
    adb("shell", "monkey", "-p", lc.package, "-c", "android.intent.category.LAUNCHER", "1")
    adb("forward", f"tcp:{lc.host_port}", f"tcp:{lc.probe_port}")
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            c = ProbeClient(port=lc.host_port, timeout=5).connect()
            if any(r["type"] == message_ids()["OwnHomeData"] for r in c.records()) and c.status()["gm_state"] == 1:
                return c
        except (OSError, ProbeError):
            pass
        time.sleep(2)
    raise RuntimeError("游戏重启后未回到主村")


def try_payload(payload: bytes, home: bytes, wait_s: float = 15) -> tuple[str, dict | None]:
    ids = message_ids()
    lc = LocalConfig.load()
    try:
        c = ProbeClient(port=lc.host_port, timeout=10).connect()
        c.status()
    except (OSError, ProbeError):
        c = restart_game()
    c.net_config(offline=True, block_all=True, allow_types=[10108], deliver_mode=1)
    c.record_put(ids["OwnHomeData"], home)
    c.record_put(ids["EnemyHomeData"], payload)
    try:
        c.replay(ids["EnemyHomeData"], timeout_ms=10000)
        t0 = time.time()
        while time.time() - t0 < wait_s:
            st = c.status()
            if st.get("in_battle"):
                time.sleep(1.5)
                obs = c.observe(objects=True)["obs"]
                c.replay(ids["OwnHomeData"], timeout_ms=10000)
                time.sleep(2)
                c.net_config(offline=False, block_all=False)
                return "ok", obs
            time.sleep(0.3)
        return "timeout", None
    except (OSError, ProbeError):
        restart_game()
        return "crash", None
