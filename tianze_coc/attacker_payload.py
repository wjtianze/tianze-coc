"""EnemyHomeData 中进攻方 avatar 的"本局配兵"字段：解析与改写（离线自对弈时注入任意配兵）。

除 avatar_codec 中的数量列表（unit_counts / spell_counts / siege_counts）外，进攻方 avatar 还有
（18.647 真机录制中定位，均以内容/结构锚定，不依赖绝对偏移）：

- army_all：  int32 n + n×{int32 GlobalID, VInt 数量}，兵种+法术+攻城机器的合并清单，内容与三个数量列表之并相同；
- pets：      int32 n + n×{int32 英雄, int32 战宠或 0}，英雄携带的战宠；
- items_hero：VInt n + n×{int32 装备, int32 英雄}，英雄当前装备；
- items_slot：VInt n + n×{int32 装备, VInt 槽位(0/1)}，紧跟 items_hero；
- heroes：    VInt n + n×{int32 英雄}，本局出战英雄；
- cc：        heroes 之后：VInt（含义未确认，原样保留）+ VInt n + n×{int32 GlobalID, VInt 数量, VInt 等级}，部落援军。

格式结论来自同一套配兵的 4 份录制，**改写结果需在真机上用 observe 的 army 字段验证**（见 tools/live_army_test.py）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import avatar_codec as ac


@dataclass
class Span:
    start: int
    end: int


@dataclass
class AttackerFields:
    chain: ac.AvatarLists
    army_all: ac.SlotList | None
    pets: tuple[Span, list[tuple[int, int]]] | None
    items_hero: tuple[Span, list[tuple[int, int]]] | None
    items_slot: tuple[Span, list[tuple[int, int]]] | None
    heroes: tuple[Span, list[int]] | None
    cc: tuple[Span, int, list[tuple[int, int, int]]] | None  # (区间, 前导 VInt, [(gid, 数量, 等级)])


def _be(buf: bytes, o: int) -> int:
    return struct.unpack_from(">i", buf, o)[0]


def _tbl(gid: int) -> int:
    return gid // 1_000_000


def _parse_pairs_i32(buf: bytes, o: int, t1: int, t2: int | None) -> tuple[Span, list[tuple[int, int]]] | None:
    if o + 4 > len(buf):
        return None
    n = _be(buf, o)
    if not 1 <= n <= 16 or o + 4 + 8 * n > len(buf):
        return None
    out = []
    for k in range(n):
        a, b = _be(buf, o + 4 + 8 * k), _be(buf, o + 8 + 8 * k)
        if _tbl(a) != t1 or a % 1_000_000 > 999 or not (b == 0 or (t2 is not None and _tbl(b) == t2)):
            return None
        out.append((a, b))
    return Span(o, o + 4 + 8 * n), out


def _parse_vlist(buf: bytes, o: int, second: str, t1: int, t2: int | None = None):
    """VInt n + n×{int32 (表 t1), second}，second 为 'i32'（表 t2）或 'vint'。"""
    try:
        n, p = ac.read_vint(buf, o)
    except IndexError:
        return None
    if not 1 <= n <= 64:
        return None
    out = []
    for _ in range(n):
        if p + 5 > len(buf):
            return None
        a = _be(buf, p)
        if _tbl(a) != t1 or a % 1_000_000 > 999:
            return None
        p += 4
        if second == "i32":
            b = _be(buf, p)
            if t2 is not None and _tbl(b) != t2:
                return None
            p += 4
        else:
            b, p = ac.read_vint(buf, p)
        out.append((a, b))
    return Span(o, p), out


def parse(buf: bytes) -> AttackerFields:
    chain = ac.find_chain(buf)
    eq = chain.get("equipment_levels")
    lo = chain["pet_levels"].end
    hi = eq.start if eq else len(buf)
    # army_all：内容 = 三个数量列表之并
    want = sorted(chain["unit_counts"].entries + chain["spell_counts"].entries + chain["siege_counts"].entries)
    army_all = None
    pets = None
    for o in range(lo, hi - 4):
        if army_all is None and want:
            sl = ac.parse_list(buf, o)
            if sl and sl.entries and sorted(sl.entries) == want:
                army_all = sl
        if pets is None:
            r = _parse_pairs_i32(buf, o, 28, 73)
            if r and any(b for _, b in r[1]):
                pets = r
    items_hero = items_slot = heroes = cc = None
    if eq is not None:
        p = eq.end
        items_hero = _parse_vlist(buf, p, "i32", 90, 28)
        if items_hero:
            items_slot = _parse_vlist(buf, items_hero[0].end, "vint", 90)
        start = items_slot[0].end if items_slot else p
        for o in range(start, min(len(buf), start + 512)):
            r = _parse_vlist_heroes(buf, o)
            if r:
                heroes = r
                cc = _parse_cc(buf, r[0].end)
                break
    return AttackerFields(chain, army_all, pets, items_hero, items_slot, heroes, cc)


def _parse_vlist_heroes(buf: bytes, o: int):
    n, p = ac.read_vint(buf, o)
    if not 1 <= n <= 8 or p + 4 * n > len(buf):
        return None
    hs = [_be(buf, p + 4 * k) for k in range(n)]
    if not all(_tbl(h) == 28 and h % 1_000_000 < 64 for h in hs) or len(set(hs)) != n:
        return None
    return Span(o, p + 4 * n), hs


def _parse_cc(buf: bytes, o: int):
    try:
        lead, p = ac.read_vint(buf, o)
        n, p = ac.read_vint(buf, p)
        if not 0 <= n <= 64:
            return None
        out = []
        for _ in range(n):
            g = _be(buf, p)
            if _tbl(g) not in (4, 26):
                return None
            c, p = ac.read_vint(buf, p + 4)
            lv, p = ac.read_vint(buf, p)
            out.append((g, c, lv))
        return Span(o, p), lead, out
    except (IndexError, struct.error):
        return None


# ---------------------------------------------------------------------- 编码
def _enc_vlist(entries, second: str) -> bytes:
    out = bytearray(ac.write_vint(len(entries)))
    for a, b in entries:
        out += struct.pack(">i", a) + (struct.pack(">i", b) if second == "i32" else ac.write_vint(b))
    return bytes(out)


def _enc_pets(entries) -> bytes:
    return struct.pack(">i", len(entries)) + b"".join(struct.pack(">ii", a, b) for a, b in entries)


def _enc_heroes(hs) -> bytes:
    return ac.write_vint(len(hs)) + b"".join(struct.pack(">i", h) for h in hs)


def _enc_cc(lead: int, entries) -> bytes:
    out = bytearray(ac.write_vint(lead) + ac.write_vint(len(entries)))
    for g, c, lv in entries:
        out += struct.pack(">i", g) + ac.write_vint(c) + ac.write_vint(lv)
    return bytes(out)


def roundtrip_ok(buf: bytes) -> bool:
    """解析出的每个字段重新编码后与原字节一致（格式自检）。"""
    f = parse(buf)
    checks = []
    if f.army_all:
        checks.append(f.army_all.encode() == buf[f.army_all.start:f.army_all.end])
    if f.pets:
        checks.append(_enc_pets(f.pets[1]) == buf[f.pets[0].start:f.pets[0].end])
    if f.items_hero:
        checks.append(_enc_vlist(f.items_hero[1], "i32") == buf[f.items_hero[0].start:f.items_hero[0].end])
    if f.items_slot:
        checks.append(_enc_vlist(f.items_slot[1], "vint") == buf[f.items_slot[0].start:f.items_slot[0].end])
    if f.heroes:
        checks.append(_enc_heroes(f.heroes[1]) == buf[f.heroes[0].start:f.heroes[0].end])
    if f.cc:
        checks.append(_enc_cc(f.cc[1], f.cc[2]) == buf[f.cc[0].start:f.cc[0].end])
    return bool(checks) and all(checks)


def patch(buf: bytes, *, troops: dict[int, int] | None = None, spells: dict[int, int] | None = None,
          sieges: dict[int, int] | None = None, heroes: list[int] | None = None,
          pets: dict[int, int] | None = None, equipment: dict[int, list[int]] | None = None,
          cc: list[tuple[int, int, int]] | None = None) -> bytes:
    """改写进攻方本局配兵。参数为 None 的字段保持不变。
    troops/spells/sieges：{GlobalID: 数量}；heroes：出战英雄列表；pets：{英雄: 战宠}；
    equipment：{英雄: [装备, 装备]}；cc：[(GlobalID, 数量, 等级(1 起))]。"""
    f = parse(buf)
    reps: list[tuple[int, int, bytes]] = []
    ch = f.chain
    new_t = list(troops.items()) if troops is not None else ch["unit_counts"].entries
    new_s = list(spells.items()) if spells is not None else ch["spell_counts"].entries
    new_g = list(sieges.items()) if sieges is not None else ch["siege_counts"].entries
    for name, ents in (("unit_counts", new_t), ("spell_counts", new_s), ("siege_counts", new_g)):
        sl = ch[name]
        reps.append((sl.start, sl.end, ac.encode_list([(g, n) for g, n in ents if n > 0])))
    if f.army_all is not None:
        allents = [(g, n) for g, n in new_t + new_s + new_g if n > 0]
        reps.append((f.army_all.start, f.army_all.end, ac.encode_list(allents)))
    if pets is not None:
        if f.pets is None:
            raise ValueError("负载中没有找到战宠分配字段")
        cur = dict(f.pets[1])
        for h in cur:
            cur[h] = pets.get(h, 0)
        for h, pet in pets.items():
            cur[h] = pet
        reps.append((f.pets[0].start, f.pets[0].end, _enc_pets(list(cur.items()))))
    if equipment is not None:
        if f.items_hero is None or f.items_slot is None:
            raise ValueError("负载中没有找到英雄装备字段")
        ih = [(i, h) for i, h in f.items_hero[1] if h not in equipment]
        for h, items in equipment.items():
            ih += [(i, h) for i in items]
        slot_of = {}
        for h, items in equipment.items():
            for k, i in enumerate(items):
                slot_of[i] = k
        old_slot = dict(f.items_slot[1])
        isl = [(i, slot_of.get(i, old_slot.get(i, 0))) for i, _ in ih]
        reps.append((f.items_hero[0].start, f.items_hero[0].end, _enc_vlist(ih, "i32")))
        reps.append((f.items_slot[0].start, f.items_slot[0].end, _enc_vlist(isl, "vint")))
    if heroes is not None:
        if f.heroes is None:
            raise ValueError("负载中没有找到出战英雄字段")
        reps.append((f.heroes[0].start, f.heroes[0].end, _enc_heroes(heroes)))
    if cc is not None:
        if f.cc is None:
            raise ValueError("负载中没有找到部落援军字段")
        reps.append((f.cc[0].start, f.cc[0].end, _enc_cc(f.cc[1], [(g, c, lv - 1) for g, c, lv in cc])))
    reps.sort()
    out = bytearray()
    pos = 0
    for s, e, b in reps:
        if s < pos:
            raise ValueError("改写区间重叠")
        out += buf[pos:s] + b
        pos = e
    out += buf[pos:]
    return bytes(out)


def describe(buf: bytes) -> dict:
    f = parse(buf)
    return {
        "troops": dict(f.chain["unit_counts"].entries), "spells": dict(f.chain["spell_counts"].entries),
        "sieges": dict(f.chain["siege_counts"].entries),
        "army_all_found": f.army_all is not None,
        "pets": dict(f.pets[1]) if f.pets else None,
        "equipment": _group(f.items_hero[1]) if f.items_hero else None,
        "heroes": f.heroes[1] if f.heroes else None,
        "cc": [(g, c, lv + 1) for g, c, lv in f.cc[2]] if f.cc else None,
    }


def _group(pairs):
    out: dict[int, list[int]] = {}
    for i, h in pairs:
        out.setdefault(h, []).append(i)
    return out
