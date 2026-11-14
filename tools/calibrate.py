"""对象字段校准：在设备上找出游戏对象的坐标、血量等字段偏移，写入 tianze_coc/data/layout.json。

    python tools/calibrate.py dump  snap1.json      # 对当前战场所有对象做内存快照（含一级指针目标）
    python tools/calibrate.py dump  snap2.json      # 过几秒（有建筑被攻击后）再来一次
    python tools/calibrate.py positions snap1.json  # 推测坐标字段
    python tools/calibrate.py hp snap1.json snap2.json   # 推测血量字段（持续下降的整数）
    python tools/calibrate.py write --field x=0x48 --field y=0x4c --field hp=0x70,0x10

快照只读内存，使用探针的 objects_raw + peek（非法地址返回空，不会崩溃）。
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tianze_coc.local_config import DATA, LocalConfig  # noqa: E402
from tianze_coc.native_client import ProbeClient  # noqa: E402

OBJ_BYTES = 0x200
PTR_BYTES = 0x100
TILE = 512


def dump(out: Path) -> None:
    c = ProbeClient(port=LocalConfig.load().host_port).connect()
    raw = c.objects_raw()["raw"]["objects"]
    snap = []
    for o in raw:
        mem = c.peek(o["ptr"], OBJ_BYTES) or b""
        ptrs = {}
        for off in range(0, len(mem) - 7, 8):
            p = struct.unpack_from("<Q", mem, off)[0]
            if 0x10000 < p < (1 << 48) and p % 8 == 0 and off >= 8:
                tgt = c.peek(p, PTR_BYTES)
                if tgt:
                    ptrs[off] = tgt.hex()
        snap.append({"t": o["t"], "gid": o["gid"], "data": o["data"], "ptr": o["ptr"], "mem": mem.hex(), "ptrs": ptrs})
    out.write_text(json.dumps(snap), encoding="utf-8")
    print(f"{len(snap)} 个对象 -> {out}")


def ints(hexs: str) -> list[int]:
    b = bytes.fromhex(hexs)
    return [struct.unpack_from("<i", b, i)[0] for i in range(0, len(b) - 3, 4)]


def positions(snap_path: Path, obj_type: int = 0) -> None:
    snap = [o for o in json.loads(snap_path.read_text(encoding="utf-8")) if o["t"] == obj_type]
    if not snap:
        raise SystemExit("快照中没有该类型的对象")
    score = defaultdict(int)
    values = defaultdict(set)
    for o in snap:
        for i, v in enumerate(ints(o["mem"])):
            if 0 <= v < 60 * TILE:
                score[i * 4] += 1
                values[i * 4].add(v)
    cands = [(off, score[off], len(values[off])) for off in score
             if score[off] == len(snap) and len(values[off]) > max(2, len(snap) // 4)]
    print(f"类型 {obj_type} 共 {len(snap)} 个对象；全部落在 0..60 格且取值分散的 int32 偏移：")
    for off, _s, nv in sorted(cands):
        sample = [ints(o["mem"])[off // 4] / TILE for o in snap[:6]]
        print(f"  +{off:#05x}  不同取值 {nv:3d}  示例(格) {sample}")
    print("坐标通常是相邻的两个偏移（x, y）。")


def hp(a_path: Path, b_path: Path) -> None:
    a = {o["gid"]: o for o in json.loads(a_path.read_text(encoding="utf-8"))}
    b = {o["gid"]: o for o in json.loads(b_path.read_text(encoding="utf-8"))}
    dec = defaultdict(int)
    inc = defaultdict(int)
    for gid in a.keys() & b.keys():
        pairs = [((), ints(a[gid]["mem"]), ints(b[gid]["mem"]))]
        for off, h in a[gid]["ptrs"].items():
            if off in b[gid]["ptrs"]:
                pairs.append(((int(off),), ints(h), ints(b[gid]["ptrs"][off])))
        for chain, x, y in pairs:
            for i, (u, v) in enumerate(zip(x, y)):
                key = chain + (i * 4,)
                if 0 < v < u < 1_000_000:
                    dec[key] += 1
                elif v > u:
                    inc[key] += 1
    print("在多个对象上只减不增的字段（路径：一级偏移[, 指针目标内偏移]）：")
    for key, n in sorted(dec.items(), key=lambda kv: -kv[1])[:20]:
        if inc.get(key, 0) == 0:
            print(f"  {'->'.join(hex(k) for k in key):16s} 下降对象数 {n}")


def write(fields: list[str]) -> None:
    out = []
    for f in fields:
        name, path = f.split("=", 1)
        out.append({"name": name, "path": [int(x, 0) for x in path.split(",")]})
    p = DATA / "layout.json"
    p.write_text(json.dumps({"object_fields": out}, indent=1), encoding="utf-8")
    print(f"写入 {p}: {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump"); d.add_argument("out", type=Path)
    p = sub.add_parser("positions"); p.add_argument("snap", type=Path); p.add_argument("--type", type=int, default=0)
    h = sub.add_parser("hp"); h.add_argument("a", type=Path); h.add_argument("b", type=Path)
    w = sub.add_parser("write"); w.add_argument("--field", action="append", required=True)
    a = ap.parse_args()
    if a.cmd == "dump":
        dump(a.out)
    elif a.cmd == "positions":
        positions(a.snap, a.type)
    elif a.cmd == "hp":
        hp(a.a, a.b)
    else:
        write(a.field)


if __name__ == "__main__":
    main()
