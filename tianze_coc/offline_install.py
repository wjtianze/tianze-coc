"""安装注入后的 APK 并建立端口转发：python -m tianze_coc.offline_install [--apk build/tianze-coc.apk]

注意：重新签名后的 APK 与原版签名不同。若模拟器上已装有原版私服客户端，必须先卸载
（会清空该应用的本地数据，需要用户确认，脚本不会自动卸载）。
本脚本只操作 TZ_PACKAGE 指定的包，不会碰模拟器上的官方部落冲突。
"""
from __future__ import annotations

import argparse
import subprocess

from .local_config import ROOT, LocalConfig

OFFICIAL_PACKAGES = ("com.supercell.clashofclans", "com.tencent.tmgp.supercell.clashofclans")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, encoding="utf-8", errors="replace")


def forward(cfg: LocalConfig) -> None:
    run(cfg.adb_cmd("forward", f"tcp:{cfg.host_port}", f"tcp:{cfg.probe_port}"))
    print(f"已转发 PC 127.0.0.1:{cfg.host_port} -> 模拟器 127.0.0.1:{cfg.probe_port}")


def main() -> None:
    cfg = LocalConfig.load()
    if cfg.package in OFFICIAL_PACKAGES:
        raise SystemExit("拒绝操作官方客户端包名")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=str(ROOT / "build" / "tianze-coc.apk"))
    ap.add_argument("--forward-only", action="store_true")
    a = ap.parse_args()
    if not a.forward_only:
        installed = run(cfg.adb_cmd("shell", "pm", "list", "packages", cfg.package), check=False).stdout
        r = run(cfg.adb_cmd("install", "-r", a.apk), check=False)
        print(r.stdout + r.stderr)
        if r.returncode != 0:
            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in (r.stdout + r.stderr) and cfg.package in installed:
                raise SystemExit(f"模拟器上的 {cfg.package} 签名不同。请确认后手动卸载（会清空其数据）：\n"
                                 f"  adb uninstall {cfg.package}")
            raise SystemExit("安装失败")
    forward(cfg)


if __name__ == "__main__":
    main()
