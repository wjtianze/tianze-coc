"""Supercell 资源 CSV 的解码与读取。

APK 里的 assets/logic/*.csv 是 LZMA "alone" 格式，但头部只写了 4 字节的解压长度，
标准格式需要 8 字节，补齐后即可用 Python 自带的 lzma 解压。
"""
from __future__ import annotations

import csv
import io
import lzma
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


def decode_sc_csv(raw: bytes) -> bytes:
    """解码一个 CSV 资源；已是明文时原样返回。"""
    if raw[:4] == b"Sig:":
        # 19.32 起资源前面加了 "Sig:" + 64 字节签名，其后仍是原来的 LZMA 数据
        raw = raw[4 + 64:]
    if raw[:1] == b"]" and len(raw) > 9:
        fixed = raw[:9] + b"\0\0\0\0" + raw[9:]
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(fixed)
    if raw[:2] == b"SC":
        raise ValueError("SC 容器格式（zstd）的 CSV 暂不支持；请使用 APK 内的 LZMA 版本")
    return raw


@dataclass
class CsvRow:
    """一条逻辑数据（Name 非空的行），后续无 Name 的行作为它的各等级。"""

    name: str
    fields: dict[str, str]
    levels: list[dict[str, str]] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default)

    def flag(self, key: str) -> bool:
        return self.fields.get(key, "").strip().upper() == "TRUE"

    def int(self, key: str, default: int = 0) -> int:
        v = self.fields.get(key, "").strip()
        try:
            return int(v)
        except ValueError:
            return default


@dataclass
class CsvTable:
    name: str
    header: list[str]
    types: list[str]
    rows: list[CsvRow]

    def by_name(self) -> dict[str, CsvRow]:
        return {r.name: r for r in self.rows}


def parse_table(name: str, text: str) -> CsvTable:
    reader = list(csv.reader(io.StringIO(text)))
    header, types = reader[0], reader[1]
    rows: list[CsvRow] = []
    for raw in reader[2:]:
        d = dict(zip(header, raw))
        if d.get("Name"):
            rows.append(CsvRow(d["Name"], d, [d]))
        elif rows:
            # 等级行：空字段继承上一等级
            prev = rows[-1].levels[-1]
            merged = {k: (v if v != "" else prev.get(k, "")) for k, v in d.items()}
            rows[-1].levels.append(merged)
    return CsvTable(name, header, types, rows)


class AssetSource:
    """从 APK 或已解出的目录读取资源。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def read(self, member: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(member)
        return (self.path / member).read_bytes()

    def exists(self, member: str) -> bool:
        if self._zip is not None:
            try:
                self._zip.getinfo(member)
                return True
            except KeyError:
                return False
        return (self.path / member).exists()

    def table(self, member: str) -> CsvTable:
        text = decode_sc_csv(self.read(member)).decode("utf-8", "replace")
        return parse_table(Path(member).stem, text)

    def text(self, member: str) -> str:
        return decode_sc_csv(self.read(member)).decode("utf-8", "replace")

    def build_tag(self) -> str:
        for m in ("assets/build.tag", "build.tag"):
            if self.exists(m):
                return self.read(m).decode().strip()
        return ""


def load_localization(src: AssetSource, lang: str = "cn") -> dict[str, str]:
    """TID -> 文本。cn.csv 的列为 TID, 文本。"""
    out: dict[str, str] = {}
    for member in (f"assets/localization/{lang}.csv", "assets/localization/texts_patch.csv"):
        if not src.exists(member):
            continue
        for row in csv.reader(io.StringIO(src.text(member))):
            if len(row) >= 2 and row[0].startswith("TID_") and row[0] not in out:
                out[row[0]] = row[1]
    return out
