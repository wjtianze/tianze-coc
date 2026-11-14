"""在录制的村庄消息负载里查找/替换村庄 JSON，用于把同一条“敌方村庄数据”换成任意阵型。

Supercell ByteStream 的压缩字符串（推测格式，代码会逐项校验）：
    int32 大端  N = 4 + len(zlib 数据)
    int32 小端  解压后的字节数
    zlib 数据
只在三者完全一致、且解压内容是含 "buildings" 的 JSON 对象时才认定为村庄 JSON。
"""
from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass

ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")


@dataclass
class CompressedJson:
    prefix_offset: int  # 大端长度字段的位置
    data_offset: int  # zlib 数据起点
    data_end: int
    text: str

    @property
    def obj(self) -> dict:
        return json.loads(self.text)


def find_compressed_json(payload: bytes, require_key: str | None = "buildings") -> list[CompressedJson]:
    out: list[CompressedJson] = []
    i = 8
    while True:
        hits = [payload.find(h, i) for h in ZLIB_HEADERS]
        hits = [h for h in hits if h >= 0]
        if not hits:
            break
        p = min(hits)
        i = p + 1
        if p < 8:
            continue
        total = struct.unpack_from(">i", payload, p - 8)[0]
        raw_len = struct.unpack_from("<i", payload, p - 4)[0]
        if total < 6 or raw_len <= 0 or p - 4 + total > len(payload):
            continue
        d = zlib.decompressobj()
        try:
            raw = d.decompress(payload[p:p - 4 + total])
        except zlib.error:
            continue
        if not d.eof or len(raw) != raw_len or d.unused_data:
            continue
        try:
            text = raw.decode("utf-8")
            obj = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict) or (require_key and require_key not in obj):
            continue
        out.append(CompressedJson(p - 8, p, p - 4 + total, text))
        i = p - 4 + total
    return out


def encode_compressed_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    comp = zlib.compress(raw, 9)
    return struct.pack(">i", len(comp) + 4) + struct.pack("<i", len(raw)) + comp


def replace_home_json(payload: bytes, new_home: dict | str, index: int = 0) -> bytes:
    """把第 index 个村庄 JSON 换成 new_home，返回新负载。"""
    found = find_compressed_json(payload)
    if len(found) <= index:
        raise ValueError(f"负载中只找到 {len(found)} 个村庄 JSON")
    c = found[index]
    text = new_home if isinstance(new_home, str) else json.dumps(new_home, separators=(",", ":"))
    return payload[:c.prefix_offset] + encode_compressed_string(text) + payload[c.data_end:]


# ---- 阵型工具 ---------------------------------------------------------------
LAYOUT_KEYS = ("buildings", "traps", "decos", "obstacles")
MAP_TILES = 50  # 主村地图边长（逻辑宽度 25600 / 512）


def extract_home(payload: bytes) -> dict:
    """取出负载中的村庄 JSON（OwnHomeData / EnemyHomeData / NpcData 均可）。"""
    found = find_compressed_json(payload)
    if not found:
        raise ValueError("负载中没有村庄 JSON")
    return found[0].obj


def shift_layout(home: dict, dx: int = 0, dy: int = 0, clear_obstacles: bool = True) -> dict:
    """整体平移建筑/陷阱/装饰；越界时报错。障碍物默认清空（部落战阵型也没有障碍物）。"""
    import copy

    new = copy.deepcopy(home)
    if clear_obstacles:
        new["obstacles"] = []
    for k in LAYOUT_KEYS:
        for b in new.get(k, []):
            if "x" in b:
                b["x"] += dx
                b["y"] += dy
                if not (0 <= b["x"] < MAP_TILES and 0 <= b["y"] < MAP_TILES):
                    raise ValueError(f"{k} {b.get('data')} 平移后越界: ({b['x']}, {b['y']})")
    return new
