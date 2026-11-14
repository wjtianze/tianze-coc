"""读取仓库根目录的 .env（KEY=VALUE），环境变量优先。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tianze_coc" / "data"


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class LocalConfig:
    adb: str = "adb"
    device: str = ""  # adb -s 序列号，例如 127.0.0.1:16384
    package: str = "com.atrasis.original"
    probe_port: int = 27650
    host_port: int = 27650
    ndk: str = ""
    build_tools: str = ""  # Android SDK build-tools 目录（zipalign / apksigner）
    java_home: str = ""
    keystore: str = str(ROOT / "build" / "tianze-debug.keystore")
    original_apk: str = str(ROOT / "user-provided" / "atrasis.apk")

    @classmethod
    def load(cls) -> "LocalConfig":
        env = {**_load_env_file(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("TZ_")}}
        c = cls()
        mapping = {
            "TZ_ADB": "adb", "TZ_DEVICE": "device", "TZ_PACKAGE": "package", "TZ_PROBE_PORT": "probe_port",
            "TZ_HOST_PORT": "host_port", "TZ_NDK": "ndk", "TZ_BUILD_TOOLS": "build_tools", "TZ_JAVA_HOME": "java_home",
            "TZ_KEYSTORE": "keystore", "TZ_ORIGINAL_APK": "original_apk",
        }
        for k, attr in mapping.items():
            if env.get(k):
                cur = getattr(c, attr)
                setattr(c, attr, int(env[k]) if isinstance(cur, int) else env[k])
        return c

    def adb_cmd(self, *args: str) -> list[str]:
        cmd = [self.adb]
        if self.device:
            cmd += ["-s", self.device]
        return cmd + list(args)
