"""实时控制私服账号：阵型、配兵、英雄装备/战宠、防守部落城堡援军。

原理：探针的通用命令通道（`ProbeClient.cmd`）在主线程按"命令类型 + encode 字节"构造客户端命令，
走与游戏 UI 相同的提交路径——客户端本地执行，并随 EndClientTurn 发给私服，私服按同样逻辑执行并保存。

命令格式由真机侦听（tools/cmd_recorder.py）抓到的 UI 命令逐字节确认（19.32.7，`v` = VInt）。
19.32 的命令号全部重新编号，多数命令的字段顺序也变了（18.647 的值见 git 历史）：

| 类型 | 作用 | 字段 | 验证 |
| --- | --- | --- | --- |
| 5488 | 普通模式移动建筑 | v tick(-1), v 0, v 建筑id, v x, v y | ✅ UI 样本与重登后服务器坐标一致 |
| 6565 | 从配兵移除 | v tick, v 0, v 军队, v 槽(0兵/小队 1法术 2攻城), v 序号, v 数量 | ✅ 探针发出，重登一致 |
| 6623 | 向配兵加入 | v tick, v 军队, v 1, v 槽, v 0, i32 GlobalID | ✅ 探针发出，重登一致；小队放在槽 0 |
| 5603 | 换英雄装备 | v tick, v 装备槽, v 1, v 英雄槽, v 0, i32 装备 | ✅ 给 3 号英雄（大守护者）换装备，重登一致 |
| 5417 | 换战宠 | v tick, v 英雄槽, v 1, v 0, i32 战宠 | ✅ 给 3 号英雄换战宠，重登一致 |
| 6944 | 换出战英雄 | v tick, v 英雄槽, v 0, i32 英雄, v 1 | ✅ 2 号槽换英雄，重登一致 |
| 5621 | 英雄模式（大守护者地面/空中） | v tick, v 军队, v 0, v 英雄槽, v 模式 | ✅ 军队写 0 时本地"执行"但无效果；写 1 后重登一致 |
| 6679 | 设置防守援军 | v tick, v 1, v n, n×{v 1, i32 GlobalID, v 数量, v 等级-1}, v 模式(1 排位 0 友谊) | 格式与 18.647 相同，UI 样本 |
| 6425 | 编辑器内摆放 | v tick, v 0, v 建筑id, v 0, v x, v y | 只有 UI 样本，未单独验证 |

"军队"：1 = 当前配兵（active_army_loadout，m_type 1），3 = 友谊战军队（friendly_army_loadout，m_type 3）。
编辑器的"全部移除 / 保存"在 19.32 中尚未确认（候选 6980 / 6486 / 5695），本模块不使用。

**注意：同一批 EndClientTurn 里只要有一条服务器认为非法的命令（例如给没有双模式的英雄切模式），
整批修改都会被服务器丢弃**（客户端本地仍显示已修改，重登后恢复）。所以发送前一定先校验。

英雄槽 = active_army_loadout 中英雄列表的下标（0~3）。命令在客户端执行后要等下一条 EndClientTurn
（19.32 为 10307；18.647 为 19033）发出才会到达服务器，`run()` 默认等它发出再返回——过早关闭游戏会丢失修改。

规则（用户确认）：防守援军只有 **排位战** 和 **友谊战** 可以自定义；常规战的援军与进攻共用，
部落战的防守援军由其他成员捐赠，都不能单独设置。
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import avatar_codec as ac
from . import home_payload as hp
from .account import BUILD_MAX, BUILD_MIN, Account
from .army import Army
from .native_client import ProbeClient, ProbeError

MSG_OWN_HOME = 25195  # 18.647 / 19.32 相同；见 engine_offsets.json 的 msg_OwnHomeData

CMD_MOVE = 5488
CMD_LOADOUT_REMOVE = 6565
CMD_LOADOUT_ADD = 6623
CMD_SET_EQUIPMENT = 5603
CMD_SET_PET = 5417
CMD_SET_HERO = 6944
CMD_HERO_MODE = 5621
MSG_END_CLIENT_TURN = 10307  # 与 18.647 的 19033 是同一个消息类（虚函数逐条一致，三元组按虚表顺序对应）
CMD_SET_DEFENSE_CC = 6679
CMD_EDITOR_PLACE = 6425
CMD_EDITOR_REMOVE_ALL = None  # 19.32 未确认
CMD_EDITOR_SAVE = None  # 19.32 未确认

SLOT_TROOP, SLOT_SPELL, SLOT_SIEGE = 0, 1, 2  # 小队与兵种同在 SLOT_TROOP
ARMY_ACTIVE, ARMY_FRIENDLY = 1, 3
CC_RANKED, CC_FRIENDLY = 1, 0


def v(x: int) -> bytes:
    return ac.write_vint(x)


def i32(x: int) -> bytes:
    return struct.pack(">i", x)


# ---------------------------------------------------------------------- 编码
def enc_move(building_id: int, x: int, y: int) -> bytes:
    return v(-1) + v(0) + v(building_id) + v(x) + v(y)


def enc_loadout_remove(slot: int, index: int, count: int, army: int = ARMY_ACTIVE) -> bytes:
    return v(-1) + v(0) + v(army) + v(slot) + v(index) + v(count)


def enc_loadout_add(slot: int, gid: int, army: int = ARMY_ACTIVE) -> bytes:
    return v(-1) + v(army) + v(1) + v(slot) + v(0) + i32(gid)


def enc_set_equipment(hero_index: int, item_slot: int, item: int) -> bytes:
    return v(-1) + v(item_slot) + v(1) + v(hero_index) + v(0) + i32(item)


def enc_set_pet(hero_index: int, pet: int) -> bytes:
    return v(-1) + v(hero_index) + v(1) + v(0) + i32(pet)


def enc_set_hero(hero_index: int, hero: int) -> bytes:
    return v(-1) + v(hero_index) + v(0) + i32(hero) + v(1)


def enc_hero_mode(hero_index: int, mode: int, army: int = ARMY_ACTIVE) -> bytes:
    return v(-1) + v(army) + v(0) + v(hero_index) + v(mode)


def enc_defense_cc(entries: list[tuple[int, int, int]], mode: int) -> bytes:
    """entries: [(GlobalID, 数量, 等级 1 起)]；mode: CC_RANKED / CC_FRIENDLY。"""
    out = v(-1) + v(1) + v(len(entries))
    for g, n, lv in entries:
        out += v(1) + i32(g) + v(n) + v(lv - 1)
    return out + v(mode)


# 侦听到的 UI 原始字节（回归测试：编码器必须逐字节复现）
CAPTURED = {  # 19.32.7 UI 原始字节（runs/cmd_capture_th19.jsonl）
    "move": ((500000010, 18, 33), "7f008a94ebdc031221"),
    "loadout_remove": ((SLOT_SPELL, 2, 1), "7f0001010201"),
    "loadout_add": ((SLOT_SIEGE, 4000087), "7f01010200003d0957"),
    "loadout_add_squad": ((SLOT_TROOP, 4000195, ARMY_FRIENDLY), "7f03010000003d09c3"),
    "set_equipment": ((0, 0, 90000008), "7f00010000055d4a88"),
    "set_equipment_slot1": ((0, 1, 90000014), "7f01010000055d4a8e"),
    "set_pet": ((0, 73000008), "7f0001000459e448"),
    "set_hero": ((2, 28000006), "7f020001ab3f0601"),
    "hero_mode": ((3, 1), "7f01000301"),
    "defense_cc": (([(4000006, 15, 15)], CC_FRIENDLY), "7f010101003d09060f0e00"),
}


# ---------------------------------------------------------------------- 控制器
@dataclass
class Result:
    sent: int
    executed: int
    rejected: list[dict]

    @property
    def ok(self) -> bool:
        return not self.rejected and self.executed == self.sent


class HomeController:
    def __init__(self, client: ProbeClient | None = None):
        self.c = client or ProbeClient().connect()

    # ---- 状态
    def own_home_payload(self) -> bytes:
        """登录时录下的 OwnHomeData（注意：之后本地发出的命令不会更新它；重登即可拿到服务器最新状态）。"""
        return self.c.record_get(MSG_OWN_HOME)

    def account(self) -> Account:
        return Account.from_payload(self.own_home_payload(), "own")

    def home_json(self) -> dict:
        return hp.extract_home(self.own_home_payload())

    # ---- 底层：批量发送并等待执行结果
    def _sent(self, msg_type: int) -> int:
        return dict((t, n) for t, n in self.c.net_log()["sent"]).get(msg_type, 0)

    def run(self, cmds: list[tuple[int, bytes]], timeout: float = 60.0, flush: bool = True) -> Result:
        """提交并等待引擎执行结果；flush=True 时再等一条 EndClientTurn 发出（修改到达服务器）。"""
        self.c.cmd_results(clear=True)
        ids = [self.c.cmd(t, b) for t, b in cmds]
        want = set(ids)
        done: dict[int, dict] = {}
        t0 = time.time()
        while want - set(done) and time.time() - t0 < timeout:
            for r in self.c.cmd_results(clear=True)["results"]:
                if r["status"] != "submitted":
                    done[r["id"]] = r
            time.sleep(0.05)
        if flush and cmds:
            n0 = self._sent(MSG_END_CLIENT_TURN)
            t1 = time.time()
            while self._sent(MSG_END_CLIENT_TURN) <= n0 and time.time() - t1 < 30:
                time.sleep(0.2)
        rejected = [r for r in done.values() if r["status"] != "executed"]
        rejected += [{"id": i, "status": "timeout"} for i in want - set(done)]
        return Result(len(ids), sum(1 for r in done.values() if r["status"] == "executed"), rejected)

    # ---- 阵型（普通模式逐座移动；自动安排顺序，必要时先挪到临时空地）
    def plan_layout(self, current: dict[int, tuple[int, int, int, int]], target: dict[int, tuple[int, int]],
                    blocked: set[tuple[int, int]]) -> list[tuple[int, int, int]]:
        """current: {建筑id: (x, y, w, h)}；target: {建筑id: (x, y)}；blocked: 障碍物占的格子。
        返回移动序列 [(id, x, y)]，保证每一步的落点都是空的。"""
        pos = {k: (x, y) for k, (x, y, _, _) in current.items()}
        size = {k: (w, h) for k, (_, _, w, h) in current.items()}

        def cells(k, p):
            w, h = size[k]
            return {(p[0] + i, p[1] + j) for i in range(w) for j in range(h)}

        def occupied(exclude):
            occ = set(blocked)
            for k, p in pos.items():
                if k != exclude:
                    occ |= cells(k, p)
            return occ

        moves = []
        todo = [k for k in target if tuple(target[k]) != pos.get(k)]
        stall = 0
        while todo:
            progressed = False
            for k in list(todo):
                if not (cells(k, target[k]) & occupied(k)):
                    pos[k] = tuple(target[k])
                    moves.append((k, *target[k]))
                    todo.remove(k)
                    progressed = True
            if progressed:
                stall = 0
                continue
            # 死锁：把挡住第一个待移建筑的某座建筑先挪到临时空地
            stall += 1
            if stall > len(current) + 5:
                raise RuntimeError("阵型移动无法排出顺序（空地不足）")
            k = todo[0]
            need = cells(k, target[k])
            blocker = next((b for b, p in pos.items() if b != k and cells(b, p) & need), None)
            if blocker is None:
                raise RuntimeError(f"建筑 {k} 的目标位置 {target[k]} 压在障碍物上（需先清除障碍物，或换一个阵型）")
            occ = occupied(blocker) | need
            w, h = size[blocker]
            spot = next(((x, y) for y in range(BUILD_MIN, BUILD_MAX - h + 1) for x in range(BUILD_MIN, BUILD_MAX - w + 1)
                         if not ({(x + i, y + j) for i in range(w) for j in range(h)} & occ)), None)
            if spot is None:
                raise RuntimeError("阵型移动：找不到临时空地")
            pos[blocker] = spot
            moves.append((blocker, *spot))
            if blocker not in todo and blocker in target:
                todo.append(blocker)
        return moves

    def set_layout(self, account: Account) -> Result:
        """把私服账号的当前阵型改成 account 中各建筑的坐标（account 需来自本账号，建筑 id 对应）。"""
        home = self.home_json()
        prog = account.prog
        cur = {}
        for b in home.get("buildings", []) + home.get("traps", []):
            d = prog.structure(b["data"])
            w, h = (d["width"], d["height"]) if d else (1, 1)
            cur[b["id"]] = (b["x"], b["y"], w, h)
        target = {int(s.raw["id"]): (s.x, s.y) for s in account.structures if "id" in s.raw and int(s.raw["id"]) in cur}
        blocked = obstacle_cells(home, prog)
        moves = self.plan_layout(cur, target, blocked)
        return self.run([(CMD_MOVE, enc_move(k, x, y)) for k, x, y in moves])

    # ---- 配兵
    def set_army(self, army: Army, current: dict | None = None) -> Result:
        """把当前配兵（active_army_loadout）改成 army 的兵种/法术/攻城机器，装备与战宠按英雄序号设置。
        current：当前 loadout JSON（默认取登录时的录制；连续调用时请传入上次的目标或重登）。"""
        lo = current or self.home_json()["active_army_loadout"]
        slots = lo["slots_by_type"]
        cmds: list[tuple[int, bytes]] = []
        for slot, want in ((SLOT_TROOP, army.troops), (SLOT_SPELL, army.spells), (SLOT_SIEGE, army.sieges)):
            have = [(x["id"], x.get("m_count", 0)) for x in (slots[slot] if slot < len(slots) else [])]
            # 先整体清空（每种一条：序号 0、数量 = 该项数量），再逐个加入
            for _gid, n in have:
                if n > 0:
                    cmds.append((CMD_LOADOUT_REMOVE, enc_loadout_remove(slot, 0, n)))
            for gid, n in want.items():
                cmds += [(CMD_LOADOUT_ADD, enc_loadout_add(slot, gid))] * n
        have_h = slots[4] if len(slots) > 4 else []
        for hi, h in enumerate(army.heroes):
            cur = have_h[hi] if hi < len(have_h) else {}
            if cur.get("id") != h.gid:
                cmds.append((CMD_SET_HERO, enc_set_hero(hi, h.gid)))
            # 装备/战宠一律发送：登录时的录制可能已过时（玩家之后手动改过）。
            # 目标与现状相同时引擎会拒绝该命令（无变化），结果记在 Result.rejected 里，属正常。
            for k, item in enumerate(h.equipment):
                cmds.append((CMD_SET_EQUIPMENT, enc_set_equipment(hi, k, item)))
            if h.pet:
                cmds.append((CMD_SET_PET, enc_set_pet(hi, h.pet)))
            # 只对有双模式的英雄（大守护者）发模式命令：服务器对同一批里任何一条非法命令会整批拒收
            if army_has_alt_mode(h.gid) and (h.mode or cur.get("m_heroMode", 0)):
                cmds.append((CMD_HERO_MODE, enc_hero_mode(hi, h.mode)))
        return self.run(cmds, timeout=120)

    # ---- 防守部落城堡援军（只有排位战和友谊战可以自定义）
    def set_defense_cc(self, entries: list[tuple[int, int, int]], mode: int = CC_RANKED) -> Result:
        if mode not in (CC_RANKED, CC_FRIENDLY):
            raise ValueError("防守援军只能为排位战(1)或友谊战(0)设置；常规战与进攻共用，部落战由成员捐赠")
        return self.run([(CMD_SET_DEFENSE_CC, enc_defense_cc(entries, mode))])


def army_has_alt_mode(hero: int) -> bool:
    from .assets.progression import Progression

    u = Progression.load().units.get(hero)
    return bool(u and u.get("has_alt_mode"))


def obstacle_cells(home: dict, prog=None) -> set[tuple[int, int]]:
    """障碍物（树、石头等）占的格子，尺寸取自 obstacles.csv（progression.json 的 obstacles）。"""
    from .assets.progression import Progression

    sizes = (prog or Progression.load()).data.get("obstacles", {})
    out = set()
    for o in home.get("obstacles", []):
        w, h = sizes.get(str(o["data"]), (2, 2))
        out |= {(o["x"] + i, o["y"] + j) for i in range(w) for j in range(h)}
    return out


def defense_cc_validate(entries: list[tuple[int, int, int]], account: Account) -> list[str]:
    """防守援军：只能是可捐赠的兵种（不含法术/攻城机器），数量按城堡兵种容量，等级不超过本账号该兵种等级。"""
    from .army import capacity

    p = account.prog
    cap = capacity(account)
    errs = []
    used = 0
    for g, n, lv in entries:
        u = p.units.get(g)
        if u is None or u["kind"] != "troop":
            errs.append(f"{g} 不能作为防守援军")
            continue
        used += u["housing"] * n
        own = account.unit_levels.get(g, 0)
        if u["status"] != "super_troop" and lv > own:
            errs.append(f"{u['name_cn']} 等级 {lv} 超过本账号的 {own} 级")
    if used > cap.cc_troops:
        errs.append(f"防守援军人口 {used} 超过部落城堡容量 {cap.cc_troops}")
    return errs


__all__ = ["HomeController", "ProbeError", "enc_move", "enc_loadout_add", "enc_loadout_remove",
           "enc_set_equipment", "enc_set_pet", "enc_set_hero", "enc_hero_mode", "enc_defense_cc", "defense_cc_validate", "CC_RANKED", "CC_FRIENDLY"]
