"""静态扫描 libg：所有命令类的 命令类型 -> 虚表 / 对象大小。

客户端 UI 发出的命令（移动建筑、改配兵、换装备……）不在 LogicCommandManager::createCommand 工厂里
（工厂只负责服务器下发的命令），因此通用命令通道需要直接按虚表构造对象：
    operator new(size) -> 清零 -> 写虚表 -> 执行 tick = -1 -> decode(字节)

命令虚表的特征：槽 4（getCommandType）是 `mov w0,#类型; ret`，槽 5/6 是 encode/decode，槽 7 是所有命令共用的基类函数。
对象大小取自构造函数（写虚表的函数）调用点前的 `mov w0,#大小; bl operator new`；找不到时为 0（探针会按上限分配）。

用法：python -I tools/re/cmd_vtables.py libg.so  ->  tianze_coc/data/command_types.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from elfre import ElfRe  # noqa: E402
from cmdtable import MD, method_returning_const  # noqa: E402


def _disasm_iter(r: ElfRe, va: int, n: int):
    return MD.disasm(r.read(va, 4 * n), va)


ElfRe.disasm_iter = _disasm_iter

ROOT = Path(__file__).resolve().parents[2]


def scan(r: ElfRe, encode_base: int, decode_base: int, operator_new: int, base_slot7: int = 0,
         lo: int = 1000, hi: int = 20000) -> dict:
    inv: dict[int, list[int]] = {}
    for k, v in r.relocs.items():
        inv.setdefault(v, []).append(k)
    out = {}
    fset = set(r.functions)
    for fn in r.functions:
        if r.func_end(fn) - fn != 8:
            continue
        t = method_returning_const(r, fn)
        if t is None or not lo <= t <= hi:
            continue
        for slot_addr in inv.get(fn, []):
            vt = slot_addr - 4 * 8
            enc, dec, exe = r.ptr(vt + 5 * 8), r.ptr(vt + 6 * 8), r.ptr(vt + 3 * 8)
            if not (enc and dec and exe):
                continue
            slot7_ok = bool(base_slot7) and r.ptr(vt + 7 * 8) == base_slot7
            if not slot7_ok and (enc not in fset or dec not in fset):
                continue
            # encode/decode 第一个 BL 应是基类 encode/decode（与放兵命令一致）
            def first_bl(f):
                for i in r.disasm_iter(f, 12):
                    if i.mnemonic == "bl":
                        return int(i.op_str[1:], 0)
                    if i.mnemonic in ("ret", "b", "br"):
                        return None
                return None
            fb_e, fb_d = first_bl(enc), first_bl(dec)
            is_cmd = (base_slot7 and r.ptr(vt + 7 * 8) == base_slot7) or fb_e == encode_base or fb_d == decode_base or (
                _tail_calls(r, enc, encode_base) or _tail_calls(r, dec, decode_base))
            if not is_cmd:
                continue
            size = _alloc_size(r, vt, operator_new)
            out.setdefault(t, []).append({"vtable": vt, "size": size})
    return out


def _tail_calls(r: ElfRe, f: int, target: int) -> bool:
    for i in r.disasm_iter(f, 6):
        if i.mnemonic == "b" and i.op_str.startswith("#") and int(i.op_str[1:], 0) == target:
            return True
    return False


def _alloc_size(r: ElfRe, vt: int, operator_new: int) -> int:
    """找写入该虚表的函数（构造函数或内联构造的调用方），取其前面最近的 mov w0,#size; bl new。"""
    sizes = set()
    for x in r.xrefs(vt - 0x10) + r.xrefs(vt):
        f = r.func_of(x)
        if f is None:
            continue
        seq = list(r.disasm_iter(f, (r.func_end(f) - f) // 4))
        # 1) 内联构造：同一函数内 new 之后写虚表
        idx = next((k for k, i in enumerate(seq) if i.address >= x), None)
        if idx is not None:
            for k in range(idx, max(0, idx - 40), -1):
                i = seq[k]
                if i.mnemonic == "bl" and int(i.op_str[1:], 0) == operator_new:
                    for j in range(k - 1, max(0, k - 8), -1):
                        if seq[j].mnemonic == "mov" and seq[j].op_str.startswith("w0, #"):
                            sizes.add(int(seq[j].op_str.split("#")[1], 0))
                            break
                    break
        # 2) 构造函数：在其调用点前找
        for site in r.callers(f):
            g = r.func_of(site)
            if g is None:
                continue
            pre = list(r.disasm_iter(max(g, site - 4 * 16), (site - max(g, site - 4 * 16)) // 4))
            for k in range(len(pre) - 1, -1, -1):
                i = pre[k]
                if i.mnemonic == "bl" and int(i.op_str[1:], 0) == operator_new:
                    for j in range(k - 1, max(-1, k - 8), -1):
                        if pre[j].mnemonic == "mov" and pre[j].op_str.startswith("w0, #"):
                            sizes.add(int(pre[j].op_str.split("#")[1], 0))
                            break
                    break
    return max(sizes) if sizes else 0


def main() -> None:
    libg = sys.argv[1]
    r = ElfRe(libg)
    off = json.loads((ROOT / "tianze_coc" / "data" / "engine_offsets.json").read_text(encoding="utf-8"))
    fx = off["functions"]
    # 大多数命令类虚表的槽 7 是同一个基类函数：先按"首个 BL 是基类 encode/decode"严格扫描，
    # 在结果里统计最常见的槽 7，再用它补上不调用基类 encode 的命令（如编辑阵型保存）。
    from collections import Counter
    strict = scan(r, fx["LogicCommand_encodeBase"], fx["LogicCommand_decodeBase"], fx["operator_new"])
    slot7 = Counter(r.ptr(e["vtable"] + 7 * 8) for v in strict.values() for e in v).most_common(1)[0][0]
    res = scan(r, fx["LogicCommand_encodeBase"], fx["LogicCommand_decodeBase"], fx["operator_new"], slot7)
    data = {"build_tag": off.get("build_tag", ""), "libg_sha256": off.get("libg_sha256", ""),
            "commands": {str(t): v for t, v in sorted(res.items())}}
    out = ROOT / "tianze_coc" / "data" / "command_types.json"
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    dup = {t: v for t, v in res.items() if len(v) > 1}
    print(f"写入 {out}：{len(res)} 种命令；同一类型多个虚表 {len(dup)} 种；无大小 {sum(1 for v in res.values() if not v[0]['size'])} 种")


if __name__ == "__main__":
    main()
