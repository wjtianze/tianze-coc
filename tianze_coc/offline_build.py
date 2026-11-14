"""把探针注入私服 APK：python -m tianze_coc.offline_build --probe build/libtianze_probe.so

步骤：
1. 校验原版 APK 的 libg.so 与 find_offsets.py 生成偏移时用的是同一份（SHA-256）。
2. 把 libg.so 的 DT_NEEDED "libsentry-android.so" 原地改名为 "libtianze_probe.so"
   （新名字更短，用 NUL 填充，不移动任何 ELF 结构）；探针自身再 DT_NEEDED 原库。
3. 重新打包：替换 libg.so、加入探针、去掉 armeabi-v7a（强制 64 位）、去掉旧签名。
4. zipalign + apksigner 签名（需要 SDK build-tools 与 JDK）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

from .local_config import DATA, ROOT, LocalConfig

LIBG = "lib/arm64-v8a/libg.so"
PROBE_NAME = "libtianze_probe.so"
REPLACED = "libsentry-android.so"


class BuildError(RuntimeError):
    pass


def _dynamic(data: bytes) -> tuple[dict[str, int], list[int], callable]:
    """返回 (单值 tag, DT_NEEDED 偏移列表, vaddr->文件偏移函数)。"""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise BuildError("不是 64 位小端 ELF")
    phoff, = struct.unpack_from("<Q", data, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    loads, dyn = [], None
    for i in range(phnum):
        p_type, _flags, off, vaddr, _pa, filesz, _memsz, _align = struct.unpack_from("<IIQQQQQQ", data, phoff + i * phentsize)
        if p_type == 1:
            loads.append((vaddr, off, filesz))
        elif p_type == 2:
            dyn = (off, filesz)
    if dyn is None:
        raise BuildError("没有 PT_DYNAMIC")

    def v2o(va: int) -> int:
        for vaddr, off, filesz in loads:
            if vaddr <= va < vaddr + filesz:
                return off + va - vaddr
        raise BuildError(f"地址 {va:#x} 不在任何 PT_LOAD 中")

    tags: dict[str, int] = {}
    needed: list[int] = []
    names = {5: "DT_STRTAB", 10: "DT_STRSZ", 0x6FFFFFFE: "DT_VERNEED", 0x6FFFFFFF: "DT_VERNEEDNUM"}
    off, size = dyn
    for i in range(size // 16):
        tag, val = struct.unpack_from("<qQ", data, off + i * 16)
        if tag == 0:
            break
        if tag == 1:
            needed.append(val)
        elif tag in names:
            tags[names[tag]] = val
    return tags, needed, v2o


def patch_needed(data: bytes, old: str = REPLACED, new: str = PROBE_NAME) -> bytes:
    if len(new) > len(old):
        raise BuildError("新库名不能比旧库名长")
    tags, needed, v2o = _dynamic(data)
    strtab = v2o(tags["DT_STRTAB"])

    def s_at(o: int) -> str:
        end = data.index(b"\0", strtab + o)
        return data[strtab + o:end].decode()

    names = [s_at(o) for o in needed]
    if new in names:
        raise BuildError("libg.so 已经被注入过")
    hits = [o for o in needed if s_at(o) == old]
    if len(hits) != 1:
        raise BuildError(f"DT_NEEDED 中找不到唯一的 {old}: {names}")
    # 版本需求表若引用该文件名，改名会破坏符号版本解析
    if "DT_VERNEED" in tags:
        p = v2o(tags["DT_VERNEED"])
        for _ in range(tags.get("DT_VERNEEDNUM", 0)):
            _ver, _cnt, file_off, _aux, nxt = struct.unpack_from("<HHIII", data, p)
            if s_at(file_off) == old:
                raise BuildError(f"{old} 出现在 DT_VERNEED 中，不能原地改名")
            if not nxt:
                break
            p += nxt
    o = strtab + hits[0]
    out = bytearray(data)
    out[o:o + len(old)] = new.encode() + b"\0" * (len(old) - len(new))
    return bytes(out)


def expected_libg_sha256() -> str:
    info = json.loads((DATA / "engine_offsets.json").read_text(encoding="utf-8"))
    return info["libg_sha256"]


def repack(src_apk: Path, dst_apk: Path, libg: bytes, probe: bytes, strip_v7: bool = True) -> None:
    with zipfile.ZipFile(src_apk) as zin, zipfile.ZipFile(dst_apk, "w") as zout:
        libg_info = zin.getinfo(LIBG)
        for info in zin.infolist():
            n = info.filename
            if n.startswith("META-INF/") and n.rsplit(".", 1)[-1].upper() in ("SF", "RSA", "DSA", "EC", "MF"):
                continue
            if strip_v7 and n.startswith("lib/armeabi-v7a/"):
                continue
            data = libg if n == LIBG else zin.read(n)
            ni = zipfile.ZipInfo(n, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            zout.writestr(ni, data)
        pi = zipfile.ZipInfo(f"lib/arm64-v8a/{PROBE_NAME}", date_time=libg_info.date_time)
        pi.compress_type = libg_info.compress_type  # 与 libg 保持一致（extractNativeLibs 语义不变）
        zout.writestr(pi, probe)


def sign(cfg: LocalConfig, unsigned: Path, out: Path) -> None:
    bt = Path(cfg.build_tools)
    ext = ".exe" if os.name == "nt" else ""
    zipalign = bt / f"zipalign{ext}"
    apksigner = bt / ("apksigner.bat" if os.name == "nt" else "apksigner")
    for t in (zipalign, apksigner):
        if not t.exists():
            raise BuildError(f"找不到 {t}（请在 .env 设置 TZ_BUILD_TOOLS）")
    env = dict(os.environ)
    if cfg.java_home:
        env["JAVA_HOME"] = cfg.java_home
        env["PATH"] = str(Path(cfg.java_home) / "bin") + os.pathsep + env.get("PATH", "")
    ks = Path(cfg.keystore)
    if not ks.exists():
        ks.parent.mkdir(parents=True, exist_ok=True)
        keytool = Path(cfg.java_home) / "bin" / f"keytool{ext}" if cfg.java_home else Path("keytool")
        subprocess.run([str(keytool), "-genkeypair", "-keystore", str(ks), "-storepass", "tianze", "-keypass", "tianze",
                        "-alias", "tianze", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=tianze-coc local build"], check=True, env=env)
    aligned = unsigned.with_suffix(".aligned.apk")
    subprocess.run([str(zipalign), "-p", "-f", "4", str(unsigned), str(aligned)], check=True)
    subprocess.run([str(apksigner), "sign", "--ks", str(ks), "--ks-pass", "pass:tianze", "--ks-key-alias", "tianze",
                    "--out", str(out), str(aligned)], check=True, env=env)
    aligned.unlink(missing_ok=True)


def build(cfg: LocalConfig, src_apk: Path, probe_path: Path, out_apk: Path, skip_sign: bool = False) -> Path:
    with zipfile.ZipFile(src_apk) as z:
        libg = z.read(LIBG)
    got = hashlib.sha256(libg).hexdigest()
    want = expected_libg_sha256()
    if got != want:
        raise BuildError(f"libg.so 版本不符：{got}，偏移表对应 {want}。请先对该版本重跑 tools/re/find_offsets.py")
    patched = patch_needed(libg)
    probe = probe_path.read_bytes()
    out_apk.parent.mkdir(parents=True, exist_ok=True)
    unsigned = out_apk.with_suffix(".unsigned.apk")
    repack(src_apk, unsigned, patched, probe)
    if skip_sign:
        shutil.move(unsigned, out_apk)
    else:
        sign(cfg, unsigned, out_apk)
        unsigned.unlink(missing_ok=True)
    return out_apk


def main() -> None:
    cfg = LocalConfig.load()
    ap = argparse.ArgumentParser(description="把探针注入私服 APK")
    ap.add_argument("--apk", default=cfg.original_apk)
    ap.add_argument("--probe", default=str(ROOT / "build" / PROBE_NAME))
    ap.add_argument("--out", default=str(ROOT / "build" / "tianze-coc.apk"))
    ap.add_argument("--skip-sign", action="store_true", help="只打包不签名（用于检查）")
    a = ap.parse_args()
    out = build(cfg, Path(a.apk), Path(a.probe), Path(a.out), a.skip_sign)
    print(f"已生成 {out}")


if __name__ == "__main__":
    main()
