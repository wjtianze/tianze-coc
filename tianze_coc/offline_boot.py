"""完全离线启动：不登录私服的游戏服务器，用录制的登录消息直接进入主村。

原理（18.647 真机验证，见 probe/src/offline_boot.h）：
1. 启动前设置系统属性 debug.tianze.offline_boot=1。探针加载时 hook libc 的 connect，
   把发往游戏端口（9339）的连接改到探针自己的本地假服务器；启动器的 HTTPS 等其他连接照常放行。
2. 客户端连上假服务器、发出握手后，Python 把录制的登录消息写入探针，
   探针把登录状态设为"已发登录、握手已验证"，再在服务线程上按原顺序同步投递这些消息
   （此时帧循环还没运行，不能用每帧重放）。
3. 之后客户端每次发保活请求，探针自动投递一条保活回复。

好处：多个模拟器可以共用同一份登录包，不会在私服上登录、互相挤下线，也不给私服增加任何负担。

用法：
    # 一次性：正常联网登录一次，录下登录包（含账号信息，存放在 data/records/ 下，不进仓库）
    python -m tianze_coc.offline_boot capture th18
    # 之后每次启动
    python -m tianze_coc.offline_boot boot th18
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .local_config import DATA, LocalConfig
from .native_client import ProbeClient, ProbeError

BUNDLE_DIR = DATA / "records"
KEEPALIVE_REQ, KEEPALIVE_RESP = 10108, 20108  # 18.647；版本更新后用 capture 时的收包日志重新确认
GAME_PORT = 9339
CLIENT_HELLO = 10100


def bundle_path(name: str) -> Path:
    return BUNDLE_DIR / f"{name}.login"


def capture(client: ProbeClient, name: str) -> Path:
    """在一次正常联网登录之后调用：按收包日志的顺序保存所有登录消息。"""
    inbox = client.inbox()
    order: list[int] = []
    for _ms, t, length, replayed in inbox:
        if replayed or t in order or t == KEEPALIVE_RESP:
            continue
        order.append(t)
    d = bundle_path(name)
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for t in order:
        try:
            (d / f"{t}.bin").write_bytes(client.record_get(t))
            saved.append(t)
        except ProbeError:
            pass  # 长度为 0 的消息没有录制，跳过
    meta = {"order": saved, "game_port": GAME_PORT, "keepalive_req": KEEPALIVE_REQ,
            "keepalive_resp": KEEPALIVE_RESP, "captured": time.strftime("%Y-%m-%d %H:%M:%S")}
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return d


@dataclass
class OfflineBoot:
    name: str
    timeout_s: float = 120.0

    def _adb(self, *args: str, timeout: float = 30) -> str:
        lc = LocalConfig.load()
        return subprocess.run(lc.adb_cmd(*args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout).stdout

    def boot(self) -> ProbeClient:
        lc = LocalConfig.load()
        d = bundle_path(self.name)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        self._adb("shell", "setprop", "debug.tianze.offline_boot", "1")
        self._adb("shell", "am", "force-stop", lc.package)
        self._adb("shell", "monkey", "-p", lc.package, "-c", "android.intent.category.LAUNCHER", "1")
        self._adb("forward", f"tcp:{lc.host_port}", f"tcp:{lc.probe_port}")
        deadline = time.monotonic() + self.timeout_s
        c = self._connect(deadline)
        c.call("offline_boot", enable=True, game_port=meta["game_port"], keepalive_req=meta["keepalive_req"],
               keepalive_resp=meta["keepalive_resp"])
        # 等客户端连上假服务器并发出握手
        while True:
            if c.call("offline_boot")["redirected"] > 0 and CLIENT_HELLO in c.net_log().get("recent", []):
                break
            self._check(deadline, "客户端连接游戏服务器")
            time.sleep(0.2)
        for t in meta["order"]:
            c.record_put(t, (d / f"{t}.bin").read_bytes())
        c.net_config(offline=True, block_all=True, allow_types=[], deliver_mode=1)
        c.call("login_prep")
        for t in meta["order"]:
            r = c.replay_now(t)
            if not str(r.get("status", "")).startswith("ok"):
                raise ProbeError(f"投递登录消息 {t} 失败: {r}")
        while c.status().get("gm_state") != 1:
            self._check(deadline, "进入主村")
            time.sleep(0.3)
        return c

    def _connect(self, deadline: float) -> ProbeClient:
        lc = LocalConfig.load()
        while True:
            try:
                c = ProbeClient(port=lc.host_port, timeout=5).connect()
                c.status()
                return c
            except (OSError, ProbeError):
                self._check(deadline, "连接探针")
                time.sleep(0.3)

    @staticmethod
    def _check(deadline: float, what: str) -> None:
        if time.monotonic() > deadline:
            raise ProbeError(f"离线启动：等待{what}超时")


def main() -> None:
    ap = argparse.ArgumentParser(description="完全离线启动")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("capture", help="正常联网登录后录下登录包")
    p1.add_argument("name")
    p2 = sub.add_parser("boot", help="用登录包离线启动客户端")
    p2.add_argument("name")
    p3 = sub.add_parser("online", help="关闭离线启动（下次启动恢复联网登录）")
    a = ap.parse_args()
    lc = LocalConfig.load()
    if a.cmd == "capture":
        c = ProbeClient(port=lc.host_port, timeout=10).connect()
        print("已保存登录包：", capture(c, a.name))
    elif a.cmd == "boot":
        t0 = time.time()
        c = OfflineBoot(a.name).boot()
        print(f"离线启动完成，用时 {time.time() - t0:.1f} 秒；状态 {c.status()}")
    else:
        subprocess.run(lc.adb_cmd("shell", "setprop", "debug.tianze.offline_boot", "0"))
        print("已关闭离线启动，下次启动游戏会正常联网登录")


if __name__ == "__main__":
    main()
