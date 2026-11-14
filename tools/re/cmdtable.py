"""把 LogicCommandManager::createCommand 的比较树/跳转表解析为 {命令ID: (对象大小, 构造函数, 虚表)}。

用法: python -I cmdtable.py libg.so [createCommand地址] [operator new地址]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capstone  # noqa: E402

from elfre import ElfRe  # noqa: E402

MD = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)


def ins_at(r, pc):
    return next(MD.disasm(r.read(pc, 4), pc))


def find_vtable(r, start, limit=80):
    """在 [start, start+limit 条指令) 中找 `adrp/ldr(GOT) + add #0x10` 或 `adrp/add + add #0x10` 写入的虚表。"""
    regs = {}
    pc = start
    for _ in range(limit):
        i = ins_at(r, pc)
        m, ops = i.mnemonic, [o.strip() for o in i.op_str.split(",")]
        if m == "adrp":
            regs[ops[0]] = ("page", int(ops[1][1:], 0))
        elif m == "ldr" and len(ops) == 3 and ops[1].startswith("[") and ops[1][1:] in regs and regs[ops[1][1:]][0] == "page":
            got = regs[ops[1][1:]][1] + int(ops[2].rstrip("]")[1:], 0)
            regs[ops[0]] = ("val", r.ptr(got))
        elif m == "add" and len(ops) == 3 and ops[2].startswith("#") and ops[1] in regs:
            kind, v = regs[ops[1]]
            regs[ops[0]] = ("val", v + int(ops[2][1:], 0))
        elif m == "str" and len(ops) == 2 and ops[1] == "[x0]" and ops[0] in regs and regs[ops[0]][0] == "val":
            return regs[ops[0]][1]
        elif m in ("ret",):
            break
        pc += 4
    return None


def walk(r, f, cid, new_addr):
    regs = {}
    pc = f
    flags = None
    for _ in range(600):
        i = ins_at(r, pc)
        m, op = i.mnemonic, i.op_str
        ops = [o.strip() for o in op.split(",")]
        if m == "mov" and len(ops) == 2 and ops[1].startswith("#"):
            regs[ops[0][1:]] = int(ops[1][1:], 0)
        elif m == "mov" and ops[0] == "w1" and ops[1] == "w0":
            pass
        elif m == "cmp":
            def val(x):
                if x.startswith("#"):
                    return int(x[1:], 0)
                if x in ("w0", "w1"):
                    return cid
                return regs.get(x[1:])
            flags = (val(ops[0]), val(ops[1]))
        elif m == "sub" and ops[1] in ("w0", "w1") and ops[2].startswith("#"):
            regs[ops[0][1:]] = cid - int(ops[2][1:], 0)
        elif m == "adrp":
            regs[ops[0][1:]] = int(ops[1][1:], 0)
        elif m == "add" and len(ops) == 3 and ops[2].startswith("#") and ops[0].startswith("x") and ops[1][1:] in regs:
            regs[ops[0][1:]] = regs[ops[1][1:]] + int(ops[2][1:], 0)
        elif m == "adr":
            regs[ops[0][1:]] = int(ops[1][1:], 0)
        elif m in ("ldrh", "ldrb") and re.match(r"w\d+, \[x\d+, x\d+(, lsl #1)?\]$", op):
            base = regs[ops[1].strip("[")[1:]]
            idx = regs[ops[2].split("]")[0][1:]]
            regs[ops[0][1:]] = (int.from_bytes(r.read(base + idx * 2, 2), "little") if m == "ldrh"
                                else r.read(base + idx, 1)[0])
        elif m == "add" and "lsl #2" in op:
            regs[ops[0][1:]] = regs[ops[1][1:]] + (regs[ops[2][1:]] << 2)
        elif m == "br":
            pc = regs[ops[0][1:]]
            continue
        elif m.startswith("b."):
            a, b = flags
            ua, ub = a & 0xFFFFFFFF, b & 0xFFFFFFFF
            take = {"eq": a == b, "ne": a != b, "gt": a > b, "le": a <= b, "lt": a < b, "ge": a >= b,
                    "hi": ua > ub, "ls": ua <= ub, "hs": ua >= ub, "lo": ua < ub}[m[2:]]
            if take:
                pc = int(op[1:], 0)
                continue
        elif m == "b":
            pc = int(op[1:], 0)
            continue
        elif m == "bl":
            tgt = int(op[1:], 0)
            if tgt != new_addr:
                return None  # 默认分支（报错/返回空）
            size = regs.get("0")
            # new 之后：要么 bl 构造函数，要么内联写虚表
            p2 = pc + 4
            ctor = None
            for _ in range(6):
                j = ins_at(r, p2)
                if j.mnemonic == "bl":
                    ctor = int(j.op_str[1:], 0)
                    break
                if j.mnemonic in ("ret", "b"):
                    break
                p2 += 4
            vt = find_vtable(r, ctor, 120) if ctor else None
            if vt is None:
                vt = find_vtable(r, pc + 4, 60)
            return size, ctor, vt
        elif m == "ret":
            return None
        pc += 4
    return None


def table(r, f, new_addr, ids=range(0, 30000)):
    out = {}
    for cid in ids:
        res = walk(r, f, cid, new_addr)
        if res:
            out[cid] = res
    return out


def method_returning_const(r, fn):
    """形如 `mov w0,#imm; ret` 的小函数 -> imm。"""
    try:
        a, b = ins_at(r, fn), ins_at(r, fn + 4)
    except StopIteration:
        return None
    if a.mnemonic == "mov" and a.op_str.startswith("w0, #") and b.mnemonic == "ret":
        return int(a.op_str.split("#")[1], 0)
    return None


if __name__ == "__main__":
    r = ElfRe(sys.argv[1])
    f = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0xD6FF00
    new = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x16B2320
    t = table(r, f, new)
    for k, (size, ctor, vt) in sorted(t.items()):
        slots = []
        if vt:
            for s in range(12):
                p = r.ptr(vt + 8 * s)
                c = method_returning_const(r, p) if p else None
                slots.append(f"{s}:{c}" if c is not None else "")
        print(k, size, hex(ctor) if ctor else "-", hex(vt) if vt else "-", " ".join(x for x in slots if x))
