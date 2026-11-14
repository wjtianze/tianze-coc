"""Minimal static-analysis helpers for the stripped ARM64 libg.so.

No external disassembler is required: function boundaries come from
.eh_frame_hdr, and string/data cross references come from a vectorised
ADRP + ADD / LDR scan.  Capstone is only used for pretty printing.

Usage (as a library):
    re = ElfRe("libg.so")
    for s in re.find_strings(b"LogicGameMode::loadLiveReplay"):
        for x in re.xrefs(s):
            print(hex(x), hex(re.func_of(x)))
"""
from __future__ import annotations

import bisect
import struct
from functools import cached_property
from pathlib import Path

import numpy as np
from elftools.elf.elffile import ELFFile


def _sx(value, bits):
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


class ElfRe:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        with open(self.path, "rb") as fh:
            elf = ELFFile(fh)
            self.segments = [
                (s["p_vaddr"], s["p_offset"], s["p_filesz"], s["p_memsz"], s["p_flags"])
                for s in elf.iter_segments()
                if s["p_type"] == "PT_LOAD"
            ]
            self.eh_hdr = next(
                ((s["p_vaddr"], s["p_offset"], s["p_filesz"]) for s in elf.iter_segments() if s["p_type"] == "PT_GNU_EH_FRAME"),
                None,
            )

    # ---------------------------------------------------------------- mapping
    def v2o(self, va: int) -> int | None:
        for vaddr, off, filesz, _memsz, _f in self.segments:
            if vaddr <= va < vaddr + filesz:
                return off + va - vaddr
        return None

    def o2v(self, off: int) -> int | None:
        for vaddr, o, filesz, _memsz, _f in self.segments:
            if o <= off < o + filesz:
                return vaddr + off - o
        return None

    def read(self, va: int, n: int) -> bytes:
        o = self.v2o(va)
        return self.data[o : o + n]

    def u64(self, va: int) -> int:
        return struct.unpack("<Q", self.read(va, 8))[0]

    def u32(self, va: int) -> int:
        return struct.unpack("<I", self.read(va, 4))[0]

    @cached_property
    def relocs(self) -> dict[int, int]:
        """vaddr -> resolved value for R_AARCH64_RELATIVE (1027) and ABS64/GLOB_DAT to local symbols."""
        out: dict[int, int] = {}
        with open(self.path, "rb") as fh:
            elf = ELFFile(fh)
            dyn = next(s for s in elf.iter_segments() if s["p_type"] == "PT_DYNAMIC")
            tags = {t.entry.d_tag: t.entry.d_val for t in dyn.iter_tags()}
        symtab = tags["DT_SYMTAB"]
        for start_tag, size_tag in (("DT_RELA", "DT_RELASZ"), ("DT_JMPREL", "DT_PLTRELSZ")):
            if start_tag not in tags:
                continue
            off = self.v2o(tags[start_tag])
            n = tags[size_tag] // 24
            arr = np.frombuffer(self.data, dtype="<u8", count=n * 3, offset=off).reshape(-1, 3)
            for r_off, r_info, r_add in arr.tolist():
                typ = r_info & 0xFFFFFFFF
                if typ == 1027:  # RELATIVE
                    out[r_off] = r_add
                elif typ in (257, 1025, 1026):  # ABS64, GLOB_DAT, JUMP_SLOT
                    sym = r_info >> 32
                    so = self.v2o(symtab + sym * 24)
                    st_value = struct.unpack_from("<Q", self.data, so + 8)[0]
                    shndx = struct.unpack_from("<H", self.data, so + 6)[0]
                    if shndx != 0 and st_value:
                        out[r_off] = st_value + (r_add if typ == 257 else 0)
        return out

    def ptr(self, va: int) -> int:
        """Pointer-sized value as it is after the dynamic loader ran."""
        return self.relocs.get(va, self.u64(va))

    def cstr(self, va: int, limit: int = 256) -> str:
        raw = self.read(va, limit)
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    @property
    def exec_segments(self):
        return [s for s in self.segments if s[4] & 1]

    # -------------------------------------------------------------- functions
    @cached_property
    def functions(self) -> list[int]:
        """Sorted function start addresses from the .eh_frame_hdr search table."""
        va, off, _size = self.eh_hdr
        d = self.data
        version, eh_ptr_enc, fde_count_enc, table_enc = d[off : off + 4]
        assert version == 1 and table_enc == 0x3B, (version, hex(table_enc))
        pos = off + 4
        # eh_frame_ptr (sdata4 pcrel usually) then fde_count (udata4)
        pos += 4
        count = struct.unpack_from("<I", d, pos)[0]
        pos += 4
        table = np.frombuffer(d, dtype="<i4", count=count * 2, offset=pos).reshape(-1, 2)
        starts = (table[:, 0].astype(np.int64) + va) & 0xFFFFFFFFFFFF
        return sorted(set(int(x) for x in starts))

    def func_of(self, va: int) -> int | None:
        i = bisect.bisect_right(self.functions, va) - 1
        return self.functions[i] if i >= 0 else None

    def func_end(self, start: int) -> int:
        i = bisect.bisect_right(self.functions, start)
        return self.functions[i] if i < len(self.functions) else start + 0x1000

    # ------------------------------------------------------------------ xrefs
    @cached_property
    def _ref_index(self) -> tuple[np.ndarray, np.ndarray]:
        """(target, site) pairs for ADRP followed by ADD/LDR/STR on the same register."""
        targets, sites = [], []
        for vaddr, off, filesz, _m, _f in self.exec_segments:
            w = np.frombuffer(self.data, dtype="<u4", count=filesz // 4, offset=off).astype(np.int64)
            pc = vaddr + np.arange(len(w), dtype=np.int64) * 4
            is_adrp = (w & 0x9F000000) == 0x90000000
            idx = np.nonzero(is_adrp)[0]
            ww = w[idx]
            imm = (((ww >> 5) & 0x7FFFF) << 2) | ((ww >> 29) & 3)
            imm = np.where(imm & (1 << 20), imm - (1 << 21), imm) << 12
            page = (pc[idx] & ~0xFFF) + imm
            rd = ww & 31
            for k in range(1, 17):
                j = idx + k
                ok = j < len(w)
                jj = j[ok]
                nw = w[jj]
                rn = (nw >> 5) & 31
                same = rn == rd[ok]
                # ADD Xd, Xn, #imm{, lsl 12}
                add = ((nw & 0xFF800000) == 0x91000000) & same
                add_imm = ((nw >> 10) & 0xFFF) << (((nw >> 22) & 1) * 12)
                # LDR/STR Xt, [Xn, #imm*8]
                ldr64 = (((nw & 0xFFC00000) == 0xF9400000) | ((nw & 0xFFC00000) == 0xF9000000)) & same
                ldr_imm = ((nw >> 10) & 0xFFF) * 8
                # LDR Wt/ LDRB / STR W variants ignored: rarely address strings
                tgt = np.where(add, page[ok] + add_imm, np.where(ldr64, page[ok] + ldr_imm, -1))
                m = tgt >= 0
                targets.append(tgt[m])
                sites.append(pc[jj][m])
        t = np.concatenate(targets)
        s = np.concatenate(sites)
        order = np.argsort(t, kind="stable")
        return t[order], s[order]

    def xrefs(self, target: int) -> list[int]:
        t, s = self._ref_index
        lo = np.searchsorted(t, target, "left")
        hi = np.searchsorted(t, target, "right")
        return sorted(set(int(x) for x in s[lo:hi]))

    def xrefs_range(self, lo_va: int, hi_va: int) -> list[tuple[int, int]]:
        t, s = self._ref_index
        lo = np.searchsorted(t, lo_va, "left")
        hi = np.searchsorted(t, hi_va, "left")
        return [(int(a), int(b)) for a, b in zip(t[lo:hi], s[lo:hi])]

    @cached_property
    def _bl_index(self) -> tuple[np.ndarray, np.ndarray]:
        """(callee, site) pairs for every BL / B in executable segments."""
        cs, ss = [], []
        for vaddr, off, filesz, _m, _f in self.exec_segments:
            w = np.frombuffer(self.data, dtype="<u4", count=filesz // 4, offset=off).astype(np.int64)
            pc = vaddr + np.arange(len(w), dtype=np.int64) * 4
            m = ((w & 0xFC000000) == 0x94000000) | ((w & 0xFC000000) == 0x14000000)
            imm = w[m] & 0x3FFFFFF
            imm = np.where(imm & (1 << 25), imm - (1 << 26), imm) * 4
            cs.append(pc[m] + imm)
            ss.append(pc[m])
        c = np.concatenate(cs)
        s = np.concatenate(ss)
        o = np.argsort(c, kind="stable")
        return c[o], s[o]

    def callers(self, callee: int) -> list[int]:
        c, s = self._bl_index
        lo = np.searchsorted(c, callee, "left")
        hi = np.searchsorted(c, callee, "right")
        return sorted(set(int(x) for x in s[lo:hi]))

    def callees(self, func: int) -> list[int]:
        out = []
        for va in range(func, self.func_end(func), 4):
            w = self.u32(va)
            if (w & 0xFC000000) == 0x94000000:
                out.append(va + _sx(w & 0x3FFFFFF, 26) * 4)
        return out

    # ---------------------------------------------------------------- strings
    def find_strings(self, needle: bytes, exact: bool = True) -> list[int]:
        out, start = [], 0
        while True:
            i = self.data.find(needle, start)
            if i < 0:
                break
            start = i + 1
            if exact and (self.data[i - 1] != 0 or self.data[i + len(needle)] != 0):
                continue
            va = self.o2v(i)
            if va is not None:
                out.append(va)
        return out

    def string_funcs(self, needle: bytes, exact: bool = True) -> list[tuple[int, int]]:
        """[(site, function_start)] for every code reference to the string."""
        res = []
        for sva in self.find_strings(needle, exact):
            for x in self.xrefs(sva):
                res.append((x, self.func_of(x)))
        return res

    def strings_in_func(self, func: int) -> list[tuple[int, str]]:
        out = []
        end = self.func_end(func)
        t, s = self._ref_index
        mask = (s >= func) & (s < end)
        for tgt, site in sorted(zip(t[mask].tolist(), s[mask].tolist()), key=lambda p: p[1]):
            o = self.v2o(tgt)
            if o is None:
                continue
            raw = self.data[o : o + 120].split(b"\0", 1)[0]
            if len(raw) >= 4 and all(32 <= c < 127 for c in raw):
                out.append((site, raw.decode()))
        return out

    # ------------------------------------------------------------- printing
    def disasm(self, va: int, n: int = 40) -> str:
        import capstone

        md = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
        lines = []
        for ins in md.disasm(self.read(va, n * 4), va):
            lines.append(f"{ins.address:#010x}  {ins.mnemonic:8s} {ins.op_str}")
        return "\n".join(lines)
