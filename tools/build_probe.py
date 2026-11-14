"""用 Android NDK 编译探针：python tools/build_probe.py [--apk 原版APK]

产物 build/libtianze_probe.so（arm64-v8a）。探针需要 DT_NEEDED libsentry-android.so
（它顶替了 libg.so 里的这一项依赖），因此链接时引用从 APK 中取出的该库。
"""
from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tianze_coc.local_config import LocalConfig  # noqa: E402

SOURCES = ["probe_main.cpp", "engine.cpp", "server.cpp", "netlink.cpp"]
REPLACED_NEEDED = "libsentry-android.so"


def ndk_clang(ndk: Path) -> Path:
    host = {"Windows": "windows-x86_64", "Linux": "linux-x86_64", "Darwin": "darwin-x86_64"}[platform.system()]
    exe = "clang++.exe" if platform.system() == "Windows" else "clang++"
    p = ndk / "toolchains" / "llvm" / "prebuilt" / host / "bin" / exe
    if not p.exists():
        raise SystemExit(f"找不到 NDK clang++: {p}（请在 .env 中设置 TZ_NDK）")
    return p


def main() -> None:
    cfg = LocalConfig.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=cfg.original_apk)
    ap.add_argument("--out", default=str(ROOT / "build" / "libtianze_probe.so"))
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stub_dir = ROOT / "build" / "link-stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / REPLACED_NEEDED
    with zipfile.ZipFile(a.apk) as z:
        stub.write_bytes(z.read(f"lib/arm64-v8a/{REPLACED_NEEDED}"))

    src = ROOT / "probe" / "src"
    cmd = [
        str(ndk_clang(Path(cfg.ndk))), "--target=aarch64-linux-android26",
        "-std=c++17", "-shared", "-fPIC", "-fvisibility=hidden", "-fno-exceptions",
        "-O0" if a.debug else "-O2", "-g" if a.debug else "-s",
        "-Wall", "-Wextra", "-Wno-unused-parameter",
        "-static-libstdc++",
        "-Wl,-soname,libtianze_probe.so", "-Wl,-z,max-page-size=16384", "-Wl,--no-undefined",
        *[str(src / f) for f in SOURCES],
        "-llog",
        "-Wl,--no-as-needed", str(stub),
        "-o", str(out),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"已生成 {out}（{out.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
