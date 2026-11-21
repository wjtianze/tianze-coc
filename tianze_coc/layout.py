"""阵型：表示、合法性校验、网格编码、与村庄 JSON 互转。

摆阵模型的输入是"要摆的东西"（`inventory(account)`：每座建筑/陷阱/城墙的种类、等级、占地），
输出是每一件的坐标（`Layout`）。本模块负责：
- `validate(layout, account)`：每件恰好摆一次、在 44×44 可建造区内、互不重叠；
- `apply(layout, account)`：把坐标写进账号，再由 `Account.apply_to_payload` 写回负载做离线对战（自对弈）；
- `encode_grid()`：编码成 (C, 50, 50) 的张量，给摆阵/进攻模型用；
  `attacker_view=True` 时去掉陷阱等隐藏信息（进攻方看不到，禁止开挂）；
- `random_layout()`：随机合法阵型（基线/数据增广）。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .account import BUILD_MAX, BUILD_MIN, Account

MAP = 50


@dataclass
class Item:
    index: int  # 在 account.structures 中的下标（阵型以此对应到具体某一座）
    gid: int
    level: int
    width: int
    height: int
    category: str  # building / wall / trap
    hidden: bool  # 进攻方是否看不到（陷阱）


@dataclass
class Placement:
    index: int
    x: int
    y: int
    mode: dict = field(default_factory=dict)  # 可选：改装/攻击模式等原始字段（如 {"attack_mode": true}）


@dataclass
class Layout:
    placements: list[Placement]
    # 防守部落城堡援军 [(GlobalID, 数量, 等级 1 起)]；None = 不改。
    # 只有排位战和友谊战可以自定义（常规战与进攻共用，部落战由成员捐赠）。
    cc: list[tuple[int, int, int]] | None = None

    def to_dict(self) -> dict:
        return {"placements": [p.__dict__ for p in self.placements],
                "cc": [list(x) for x in self.cc] if self.cc is not None else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Layout":
        cc = d.get("cc")
        return cls([Placement(int(p["index"]), int(p["x"]), int(p["y"]), dict(p.get("mode", {})))
                    for p in d["placements"]], [tuple(int(v) for v in x) for x in cc] if cc is not None else None)

    @classmethod
    def of(cls, account: Account) -> "Layout":
        """当前账号的阵型。"""
        return cls([Placement(i, s.x, s.y) for i, s in enumerate(account.structures)])


MODE_KEYS = ("attack_mode", "attack_mode_war", "aim_angle", "trap_mode")


def inventory(account: Account) -> list[Item]:
    out = []
    for i, s in enumerate(account.structures):
        d = account.prog.structure(s.gid)
        if d is None:
            continue
        out.append(Item(i, s.gid, s.level, d["width"], d["height"], d["category"], bool(d.get("hidden"))))
    return out


def validate(layout: Layout, account: Account) -> list[str]:
    inv = {it.index: it for it in inventory(account)}
    errs = []
    seen = set()
    occ: dict[tuple[int, int], int] = {}
    for p in layout.placements:
        it = inv.get(p.index)
        if it is None:
            errs.append(f"第 {p.index} 件不存在")
            continue
        if p.index in seen:
            errs.append(f"第 {p.index} 件摆了两次")
            continue
        seen.add(p.index)
        if p.x < BUILD_MIN or p.y < BUILD_MIN or p.x + it.width > BUILD_MAX or p.y + it.height > BUILD_MAX:
            errs.append(f"第 {p.index} 件（{it.gid}）位于 ({p.x},{p.y}) 超出可建造区域 [{BUILD_MIN},{BUILD_MAX})")
            continue
        for dx in range(it.width):
            for dy in range(it.height):
                k = (p.x + dx, p.y + dy)
                if k in occ:
                    errs.append(f"第 {p.index} 件与第 {occ[k]} 件在 ({k[0]},{k[1]}) 重叠")
                    break
                occ[k] = p.index
            else:
                continue
            break
    missing = set(inv) - seen
    if missing:
        errs.append(f"有 {len(missing)} 件没有摆：{sorted(missing)[:10]}")
    if layout.cc is not None:
        errs += validate_defense_cc(layout.cc, account)
    return errs


def validate_defense_cc(entries: list[tuple[int, int, int]], account: Account) -> list[str]:
    """防守援军：只能是兵种（部落战/排位战可用），人口不超过部落城堡容量，
    等级不超过援军等级上限（只取决于本账号部落城堡等级，见 progression.cc_level_cap）。"""
    from .army import capacity
    from .assets.progression import cc_level_cap

    p = account.prog
    cap = capacity(account)
    errs = []
    used = 0
    for g, n, lv in entries:
        u = p.units.get(g)
        if u is None or u["kind"] != "troop":
            errs.append(f"{g} 不能作为防守援军（只能是兵种）")
            continue
        used += u["housing"] * n
        lim = cc_level_cap(p, g, cap.cc_level)
        if lv > lim:
            errs.append(f"防守援军 {u['name_cn']} {lv} 级超过 {cap.cc_level} 级部落城堡的援军上限 {lim} 级")
        if lv < 1:
            errs.append(f"防守援军 {u['name_cn']} 等级无效")
    if used > cap.cc_troops:
        errs.append(f"防守援军人口 {used} 超过部落城堡容量 {cap.cc_troops}")
    return errs


def random_defense_cc(account: Account, rng: random.Random | None = None, kinds: int = 3) -> list[tuple[int, int, int]]:
    """随机合法防守援军（基线/数据增广）：挑几种兵填满城堡，等级取援军上限（实际对局中援军一般就是上限等级）。"""
    from .army import capacity
    from .assets.progression import cc_level_cap

    rng = rng or random.Random()
    p = account.prog
    room = capacity(account).cc_troops
    ccl = capacity(account).cc_level
    pool = [u for g, u in p.units.items() if u["kind"] == "troop" and u["status"] in ("permanent", "super_troop")
            and u["housing"] <= room and cc_level_cap(p, g, ccl) > 0]
    out: dict[int, int] = {}
    for u in rng.sample(pool, min(kinds, len(pool))):
        if room >= u["housing"]:
            n = rng.randint(1, room // u["housing"])
            out[u["gid"]] = out.get(u["gid"], 0) + n
            room -= n * u["housing"]
    return [(g, n, cc_level_cap(p, g, ccl)) for g, n in out.items()]


def apply(layout: Layout, account: Account, check: bool = True) -> Account:
    """把阵型写进账号（原地修改并返回）。"""
    if check:
        errs = validate(layout, account)
        if errs:
            raise ValueError("阵型不合法：" + "；".join(errs[:5]))
    for p in layout.placements:
        s = account.structures[p.index]
        s.x, s.y = p.x, p.y
        for k, v in p.mode.items():
            if k in MODE_KEYS:
                s.raw[k] = v
        # 阵型槽里的旧坐标（l12x 等）与新阵型无关，去掉以免混淆
        for k in [k for k in s.raw if k.startswith("l") and k.endswith("x") and k[1:-1].isdigit()]:
            s.raw.pop(k)
    if layout.cc is not None and account.home is not None:
        account.home["alliance_units"] = [{"id": g, "cnt": n, "lvl": lv - 1} for g, n, lv in layout.cc]
    return account


def random_layout(account: Account, rng: random.Random | None = None, tries: int = 50) -> Layout:
    """随机合法阵型：大建筑先随机落点，城墙最后填空。"""
    rng = rng or random.Random()
    inv = sorted(inventory(account), key=lambda it: (it.category == "wall", -it.width * it.height))
    for _ in range(tries):
        occ: set[tuple[int, int]] = set()
        out = []
        ok = True
        for it in inv:
            cands = [(x, y) for x in range(BUILD_MIN, BUILD_MAX - it.width + 1)
                     for y in range(BUILD_MIN, BUILD_MAX - it.height + 1)]
            rng.shuffle(cands)
            for x, y in cands:
                cells = [(x + i, y + j) for i in range(it.width) for j in range(it.height)]
                if not any(c in occ for c in cells):
                    occ.update(cells)
                    out.append(Placement(it.index, x, y))
                    break
            else:
                ok = False
                break
        if ok:
            return Layout(out)
    raise ValueError("多次尝试仍无法随机摆下全部建筑")


# ---------------------------------------------------------------------- 网格编码
def channel_names(account: Account) -> list[str]:
    """通道：每种建筑/陷阱一个占位通道（值 = 等级 / 最高等级），另加 占用、隐藏 两个通道。"""
    return ["occupied", "hidden"] + [f"{d['gid']}:{d['name']}" for d in account.prog.data["structures"]]


def encode_grid(account: Account, layout: Layout | None = None, attacker_view: bool = False):
    """返回 numpy 数组 (C, 50, 50)。attacker_view：进攻方视角，隐藏陷阱（禁止开挂）。"""
    import numpy as np

    prog = account.prog
    names = channel_names(account)
    index = {d["gid"]: 2 + k for k, d in enumerate(prog.data["structures"])}
    grid = np.zeros((len(names), MAP, MAP), dtype=np.float32)
    pos = {p.index: (p.x, p.y) for p in layout.placements} if layout else {}
    for it in inventory(account):
        if attacker_view and it.hidden:
            continue
        x, y = pos.get(it.index, (account.structures[it.index].x, account.structures[it.index].y))
        d = prog.structure(it.gid)
        val = it.level / max(1, d["max_level"])
        c = index[it.gid]
        grid[c, y:y + it.height, x:x + it.width] = val
        grid[0, y:y + it.height, x:x + it.width] = 1.0
        if it.hidden:
            grid[1, y:y + it.height, x:x + it.width] = 1.0
    return grid


def attacker_view(account: Account) -> list[dict]:
    """进攻方能看到的防守信息：建筑/城墙的种类、等级、位置；不含陷阱。
    特斯拉电磁塔（Hidden=TRUE）同样隐藏；部落城堡里的防守援军在出城前也看不到。"""
    out = []
    for it in inventory(account):
        if it.hidden:
            continue
        s = account.structures[it.index]
        out.append({"gid": it.gid, "level": it.level, "x": s.x, "y": s.y, "w": it.width, "h": it.height,
                    "category": it.category})
    return out
