"""村庄消息中 avatar（玩家数据）二进制的解析与改写。

服务器下发的 OwnHomeData / EnemyHomeData 在村庄 JSON 之后是 LogicClientAvatar 的二进制编码。
其中我们关心的是一组"数据槽列表"，格式（真机录制逐字节核对）：

    int32 大端   条目数 n
    n × { int32 大端 GlobalID,  VInt 值 }

VInt 为 Supercell 变长整数：首字节低 6 位为数值、0x40 为符号位、0x80 为续位；之后每字节 7 位。

进攻方 avatar 中这些列表按固定顺序紧挨着出现（空列表只写 int32 0）：

    0 unit_counts    兵种数量          （GlobalID 4xxxxxx）
    1 unit_levels    兵种/攻城机器等级  （4xxxxxx，值 = 游戏等级 - 1）
    2 spell_counts   法术数量          （26xxxxxx）
    3 spell_levels   法术等级          （26xxxxxx）
    4 hero_levels    英雄等级          （28xxxxxx）
    5 siege_counts   攻城机器数量       （4xxxxxx）
    6 siege_levels   攻城机器等级       （4xxxxxx）
    7 pet_levels     战宠等级          （73xxxxxx）

装备等级是之后出现的第一个 90xxxxxx 列表，但它的**条目数是 VInt**（不是 int32），紧跟在一个
int32 计数的史诗装备 GlobalID 列表之后（19.32 联调发现：18.647 录制里史诗列表为空 `00000000`，
int32 解析恰好与 "VInt 计数" 重合，掩盖了这一点）。EnemyHomeData 里先是防守方 avatar（字段较少，
没有这条完整的链），后是进攻方 avatar，18.647 中"第一条完整的链"就是进攻方；19.32 起防守方也有一条（数量为空，在前），因此取最后一条。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

CHAIN = ("unit_counts", "unit_levels", "spell_counts", "spell_levels", "hero_levels",
         "siege_counts", "siege_levels", "pet_levels")
CHAIN_TABLES = (4, 4, 26, 26, 28, 4, 4, 73)
TABLE_EQUIPMENT = 90


def read_vint(buf: bytes, o: int) -> tuple[int, int]:
    """Supercell VInt：首字节 低 6 位数值 + 0x40 符号 + 0x80 续位，之后每字节 7 位；负数为补码（-1 = 0x7f）。"""
    b = buf[o]
    neg = b & 0x40
    v = b & 0x3F
    o += 1
    sh = 6
    if b & 0x80:
        while True:
            c = buf[o]
            o += 1
            v |= (c & 0x7F) << sh
            sh += 7
            if not c & 0x80:
                break
    if neg:
        v -= 1 << sh
    return v, o


def write_vint(v: int) -> bytes:
    neg = v < 0
    first = (v & 0x3F) | (0x40 if neg else 0)
    v >>= 6
    done = (lambda x: x == -1) if neg else (lambda x: x == 0)
    out = bytearray()
    if not done(v):
        first |= 0x80
    out.append(first)
    while not done(v):
        c = v & 0x7F
        v >>= 7
        out.append(c | (0 if done(v) else 0x80))
    return bytes(out)


@dataclass
class SlotList:
    start: int  # 条目数字段的位置
    end: int  # 列表之后第一个字节
    entries: list[tuple[int, int]] = field(default_factory=list)
    vint_count: bool = False  # 条目数用 VInt 编码（装备等级列表）

    def __hash__(self) -> int:
        return hash((self.start, self.end))

    def as_dict(self) -> dict[int, int]:
        return dict(self.entries)

    def encode(self, entries: list[tuple[int, int]] | None = None) -> bytes:
        e = self.entries if entries is None else entries
        return encode_list_v(e) if self.vint_count else encode_list(e)


def encode_list(entries: list[tuple[int, int]]) -> bytes:
    out = bytearray(struct.pack(">i", len(entries)))
    for gid, v in entries:
        out += struct.pack(">i", gid) + write_vint(v)
    return bytes(out)


def encode_list_v(entries: list[tuple[int, int]]) -> bytes:
    out = bytearray(write_vint(len(entries)))
    for gid, v in entries:
        out += struct.pack(">i", gid) + write_vint(v)
    return bytes(out)


def parse_list_v(buf: bytes, o: int, table: int | None = None, max_n: int = 400) -> SlotList | None:
    """同 parse_list，但条目数是 VInt。"""
    try:
        n, p = read_vint(buf, o)
    except IndexError:
        return None
    if not 0 <= n <= max_n:
        return None
    out = []
    try:
        for _ in range(n):
            if p + 5 > len(buf):
                return None
            gid = struct.unpack_from(">i", buf, p)[0]
            t, idx = divmod(gid, 1_000_000)
            if gid <= 0 or idx >= 5000 or (table is not None and t != table):
                return None
            v, p = read_vint(buf, p + 4)
            out.append((gid, v))
    except IndexError:
        return None
    return SlotList(o, p, out, vint_count=True)


def find_equipment_list(buf: bytes, start: int, end: int | None = None) -> SlotList | None:
    """start 之后第一个（VInt 计数的）装备等级列表，至少 2 条。"""
    for q in range(start, (end if end is not None else len(buf)) - 4):
        sl = parse_list_v(buf, q, TABLE_EQUIPMENT)
        if sl is not None and len(sl.entries) >= 2 and (end is None or sl.end <= end):
            return sl
    return None


def parse_list(buf: bytes, o: int, table: int | None = None, max_n: int = 400) -> SlotList | None:
    """在 o 处尝试解析一个数据槽列表；table 给定时要求所有 GlobalID 属于该表。"""
    if o + 4 > len(buf):
        return None
    n = struct.unpack_from(">i", buf, o)[0]
    if not 0 <= n <= max_n:
        return None
    p = o + 4
    out = []
    try:
        for _ in range(n):
            if p + 5 > len(buf):
                return None
            gid = struct.unpack_from(">i", buf, p)[0]
            t, idx = divmod(gid, 1_000_000)
            if gid <= 0 or idx >= 5000 or (table is not None and t != table):
                return None
            v, p = read_vint(buf, p + 4)
            out.append((gid, v))
    except IndexError:
        return None
    return SlotList(o, p, out)


@dataclass
class AvatarLists:
    lists: dict[str, SlotList]

    def __getitem__(self, k: str) -> SlotList:
        return self.lists[k]

    def get(self, k: str) -> SlotList | None:
        return self.lists.get(k)


def _chain_at(buf: bytes, o: int) -> tuple[dict[str, SlotList], int] | None:
    lists = {}
    p = o
    for name, table in zip(CHAIN, CHAIN_TABLES):
        sl = parse_list(buf, p, table)
        if sl is None:
            return None
        lists[name] = sl
        p = sl.end
    # 等级列表必须非空（满足条件的随机字节串几乎不可能同时满足）
    if not lists["unit_levels"].entries:
        return None
    return lists, p


def find_chain(buf: bytes, start: int = 0) -> AvatarLists:
    """找到**最后一条**完整的列表链（见模块说明）。

    18.647 的 EnemyHomeData 里只有进攻方有完整的链；19.32 起防守方 avatar 也带一条（数量列表为空），
    排在进攻方之前，所以取最后一条：OwnHomeData 里只有自己的一条，EnemyHomeData 里最后一条是进攻方。"""
    best = None
    o = start
    n = len(buf) - 4 * len(CHAIN)
    while o < n:
        r = _chain_at(buf, o)
        if r is None:
            o += 1
            continue
        best = r
        o = r[1]
    if best is None:
        raise ValueError("负载中没有找到 avatar 数据槽列表链")
    lists, p = best
    eq = find_equipment_list(buf, p)
    if eq is not None:
        lists["equipment_levels"] = eq
    return AvatarLists(lists)


def find_chains(buf: bytes) -> list[int]:
    """所有完整列表链的起点（调试/核对用）。"""
    out, o, n = [], 0, len(buf) - 4 * len(CHAIN)
    while o < n:
        r = _chain_at(buf, o)
        if r is None:
            o += 1
        else:
            out.append(o)
            o = r[1]
    return out


def splice(buf: bytes, replacements: dict[SlotList, list[tuple[int, int]]] | list[tuple[SlotList, list]]) -> bytes:
    """把若干列表替换为新的条目，返回新负载（列表长度可变）。"""
    items = replacements.items() if isinstance(replacements, dict) else replacements
    items = sorted(items, key=lambda kv: kv[0].start)
    out = bytearray()
    pos = 0
    for sl, entries in items:
        if sl.start < pos:
            raise ValueError("替换区间重叠")
        out += buf[pos:sl.start] + sl.encode(entries)
        pos = sl.end
    out += buf[pos:]
    return bytes(out)
