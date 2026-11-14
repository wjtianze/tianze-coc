"""解析 MessageManager::receiveMessage 的消息类型比较树：消息ID -> 处理分支入口。"""
import os, sys, bisect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capstone
from elfre import ElfRe
MD = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)

def walk(r, start, t, limit=200):
    regs, flags, pc = {}, None, start
    for _ in range(limit):
        i = next(MD.disasm(r.read(pc, 4), pc)); m, ops = i.mnemonic, [o.strip() for o in i.op_str.split(',')]
        if m == 'mov' and len(ops) == 2 and ops[1].startswith('#'):
            regs[ops[0]] = int(ops[1][1:], 0)
        elif m in ('ldr', 'blr') and pc < start + 16:
            pass
        elif m == 'cmp':
            a = t if ops[0] == 'w0' else regs.get(ops[0])
            b = int(ops[1][1:], 0) if ops[1].startswith('#') else (t if ops[1] == 'w0' else regs.get(ops[1]))
            if a is None or b is None: return pc
            flags = (a, b)
        elif m == 'sub' and ops[1] == 'w0' and ops[2].startswith('#'):
            regs[ops[0]] = t - int(ops[2][1:], 0)
        elif m.startswith('b.') and flags:
            a, b = flags; ua, ub = a & 0xffffffff, b & 0xffffffff
            take = {'eq': a == b, 'ne': a != b, 'gt': a > b, 'le': a <= b, 'lt': a < b, 'ge': a >= b,
                    'hi': ua > ub, 'ls': ua <= ub, 'hs': ua >= ub, 'lo': ua < ub}[m[2:]]
            if take: pc = int(i.op_str[1:], 0); continue
        elif m == 'b':
            pc = int(i.op_str[1:], 0); continue
        else:
            return pc  # 进入处理分支
        pc += 4
    return None

if __name__ == '__main__':
    r = ElfRe(sys.argv[1])
    start = int(sys.argv[2], 0); lo, hi = int(sys.argv[3]), int(sys.argv[4])
    entries = {}
    for t in range(lo, hi):
        e = walk(r, start, t)
        if e is not None: entries.setdefault(e, []).append(t)
    common = max(entries, key=lambda e: len(entries[e]))  # 默认分支
    for e, ts in sorted(entries.items()):
        if e != common: print(hex(e), ts)


def reach(r, entry, stops, fstart, fend, limit=6000):
    """从 entry 沿函数内控制流 BFS（不跨越其他分支入口），返回到达的地址集合。"""
    seen, todo = set(), [entry]
    while todo and len(seen) < limit:
        pc = todo.pop()
        while fstart <= pc < fend and pc not in seen:
            if pc != entry and pc in stops:
                break
            seen.add(pc)
            i = next(MD.disasm(r.read(pc, 4), pc))
            m = i.mnemonic
            if m == 'b':
                pc = int(i.op_str[1:], 0); continue
            if m.startswith('b.') or m in ('cbz', 'cbnz', 'tbz', 'tbnz'):
                todo.append(int(i.op_str.split('#')[-1], 0))
            if m in ('ret', 'br'):
                break
            pc += 4
    return seen
