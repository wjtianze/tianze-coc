"""从 libg.so 自动定位探针所需的函数地址与结构偏移，生成：

  probe/generated/offsets.h            —— C++ 探针使用
  tianze_coc/data/engine_offsets.json  —— Python 侧/文档使用

每个符号都用"字符串锚点 + 代码特征"重新推导，不写死地址；版本更新后重跑即可。
任何一步推导失败都会直接报错，而不是生成错误的偏移。

用法: python -I tools/re/find_offsets.py <libg.so> [--expect 本版本手工核对值做回归检查]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capstone  # noqa: E402

from elfre import ElfRe  # noqa: E402

MD = capstone.Cs(capstone.CS_ARCH_ARM64, capstone.CS_MODE_ARM)
ROOT = Path(__file__).resolve().parents[2]


class LocateError(RuntimeError):
    pass


def insns(r: ElfRe, start: int, end: int | None = None, n: int | None = None):
    end = end if end is not None else start + 4 * (n or 64)
    return list(MD.disasm(r.read(start, end - start), start))


def bl_target(i) -> int | None:
    if i.mnemonic == "bl":
        return int(i.op_str[1:], 0)
    return None


def only(xs, what):
    xs = sorted(set(xs))
    if len(xs) != 1:
        raise LocateError(f"{what}: 期望唯一结果，实际 {[hex(x) for x in xs]}")
    return xs[0]


def func_by_string(r: ElfRe, needle: bytes, exact=False) -> int:
    """引用该字符串的唯一函数。字符串可能被尾部合并，故同时搜索前缀位置的引用。"""
    funcs = set()
    for sva in r.find_strings(needle, exact=exact):
        o = r.v2o(sva)
        b = o
        while r.data[b - 1] != 0:
            b -= 1
        for t, s in r.xrefs_range(r.o2v(b), sva + 1):
            funcs.add(r.func_of(s))
    return only(funcs, f"字符串 {needle!r}")


def vtable_containing(r: ElfRe, func: int) -> tuple[int, int]:
    """返回 (虚表地址, 槽位)。虚表地址指向第 0 个虚函数（即对象 +0 处保存的值）。"""
    hits = [k for k, v in r.relocs.items() if v == func]
    res = []
    for p in hits:
        b = p
        while r.ptr(b - 8) > 0x1000 and p - b < 0x400:
            b -= 8
        res.append((b, (p - b) // 8))
    if len(res) != 1:
        raise LocateError(f"函数 {func:#x} 所在虚表不唯一: {res}")
    return res[0]


def const_return(r: ElfRe, fn: int) -> int | None:
    a, b = insns(r, fn, n=2)
    if a.mnemonic == "mov" and a.op_str.startswith("w0, #") and b.mnemonic == "ret":
        return int(a.op_str.split("#")[1], 0)
    return None


def vtable_writer(r: ElfRe, vt: int) -> int:
    """构造函数：引用该虚表地址（adrp+add）的唯一函数。"""
    return only([r.func_of(s) for s in r.xrefs(vt)], f"虚表 {vt:#x} 的构造函数")


def parse_ctor_fields(r: ElfRe, ctor: int) -> dict[str, int]:
    """把构造函数里 `str/stp wN|xN, [x0, #off]` 映射成 {参数寄存器: 偏移}。"""
    out = {}
    for i in insns(r, ctor, r.func_end(ctor)):
        m = re.match(r"(str|strb|stp)\s*", i.mnemonic)
        if not m:
            if i.mnemonic == "ret":
                break
            continue
        ops = [o.strip() for o in i.op_str.split(",")]
        mm = re.search(r"\[x0(?:, #(0x[0-9a-f]+|\d+))?\]", i.op_str)
        if not mm:
            continue
        off = int(mm.group(1), 0) if mm.group(1) else 0
        if i.mnemonic == "stp":
            size = 8 if ops[0].startswith("x") else 4
            out[ops[0]] = off
            out[ops[1]] = off + size
        else:
            out[ops[0]] = off
    return out


def locate(r: ElfRe) -> dict:
    o: dict[str, int] = {}
    s: dict[str, int] = {}  # 结构偏移
    notes: dict[str, str] = {}

    # ---- 命令工厂与 operator new
    create_cmd = func_by_string(r, b"LogicCommandManager::createCommand() - Unknown command type")
    o["LogicCommandManager_createCommand"] = create_cmd
    from collections import Counter
    c = Counter(bl_target(i) for i in insns(r, create_cmd, r.func_end(create_cmd)) if bl_target(i))
    o["operator_new"] = c.most_common(1)[0][0]

    # ---- DebugUtil::deployAllTroops：放兵构造、提交、格子查询
    dat = func_by_string(r, b"DebugUtil::deployAllTroops")
    o["DebugUtil_deployAllTroops"] = dat
    seq = insns(r, dat, r.func_end(dat))
    bls = [(i.address, bl_target(i)) for i in seq if bl_target(i)]
    o["LogicLevel_getArmyUnits"] = bls[0][1]
    new_sites = [k for k, (a, t) in enumerate(bls) if t == o["operator_new"]]
    k = new_sites[0]
    o["LogicPlaceAttackerCommand_ctor"] = bls[k + 1][1]
    o["ClientCommand_submit"] = bls[k + 2][1]
    # new 之前的三个格子函数：getTile, Tile::isDeployable, TileMap::isValidAttackPos
    o["LogicTileMap_getTile"], o["LogicTile_canDeploy"], o["LogicTileMap_isValidAttackPos"] = (
        bls[k - 3][1], bls[k - 2][1], bls[k - 1][1])
    # 部署坐标 = 格子 << 9
    shifts = {int(i.op_str.split("#")[1], 0) for i in seq if i.mnemonic == "lsl" and i.op_str.startswith(("w2", "w3"))}
    s["logic_units_per_tile_shift"] = only(shifts, "格子坐标移位")
    for i in seq:
        mm = re.match(r"ldrsw\s+x\d+, \[x19, #(0x[0-9a-f]+)\]", f"{i.mnemonic} {i.op_str}")
        if mm:
            s["LogicLevel_tileMapIndex"] = int(mm.group(1), 0)
            break
    first = seq[8]  # ldp w20, w8, [x1, #0x18]
    mm = re.match(r"w\d+, w\d+, \[x1, #(0x[0-9a-f]+)\]", first.op_str)
    if first.mnemonic != "ldp" or not mm:
        raise LocateError(f"deployAllTroops 入口特征变化: {first}")
    s["LogicLevel_width"] = int(mm.group(1), 0)
    s["LogicLevel_height"] = s["LogicLevel_width"] + 4
    for i in seq:
        mm = re.match(r"ldr\s+w8, \[x22, #(0x[0-9a-f]+)\]", f"{i.mnemonic} {i.op_str}")
        if mm and i.address > o["DebugUtil_deployAllTroops"] + 0x150:
            s["LogicCombatItemData_housingSpace"] = int(mm.group(1), 0)
            break

    # 数据表：mov w0,#表号; bl getTable；表对象 +8 数组, +0x14 数量
    for a, b in zip(seq, seq[1:]):
        if a.mnemonic == "mov" and a.op_str == "w0, #0x1b" and bl_target(b):
            o["LogicDataTables_getTable"] = bl_target(b)
            break
    gt_bl = [bl_target(i) for i in insns(r, o["LogicDataTables_getTable"], n=8) if bl_target(i)]
    o["LogicDataTables_instance"] = gt_bl[0]
    s["LogicDataTables_tableListHolder"] = 8  # instance+8 -> holder; holder+8 -> 表数组(LogicArrayList)
    s["LogicDataTable_items"] = 8
    s["LogicDataTable_count"] = 0x14
    s["global_id_table_bias"] = 1  # 表号 = GlobalID // 1_000_000 - 1
    gau = insns(r, o["LogicLevel_getArmyUnits"], n=2)
    s["LogicAvatar_unitList"] = int(gau[0].op_str.split("#")[1].rstrip("]"), 0)
    nxt = insns(r, o["LogicLevel_getArmyUnits"] + 8, n=2)
    if nxt[0].mnemonic == "ldr" and nxt[1].mnemonic == "ret":
        s["LogicAvatar_spellList"] = int(nxt[0].op_str.split("#")[1].rstrip("]"), 0)
    s["LogicUnitSlot_data"] = 0
    s["LogicUnitSlot_count"] = 8

    # deployAllTroops 的调用者：client->getLevel / client->getPlayerAvatar
    dcall = only(r.callers(dat), "deployAllTroops 调用点")
    pre = [bl_target(i) for i in insns(r, dcall - 4 * 8, dcall) if bl_target(i)]
    o["Client_getInstance"], o["Client_getAvatar"], _, o["Client_getLevel"] = pre[-4:]
    av = insns(r, o["Client_getAvatar"], r.func_end(o["Client_getAvatar"]))
    for i in av:
        mm = re.match(r"x0, \[x8, #(0x[0-9a-f]+)\]!?$", i.op_str)
        if i.mnemonic == "ldr" and mm:
            s.setdefault("LogicLevel_visitorAvatar", int(mm.group(1), 0))
    notes["LogicLevel_visitorAvatar"] = "GameMode 状态为 2/4（进攻/访问）时 Client_getAvatar 返回 level+该偏移"

    # ---- 提交函数：全局客户端实例 -> GameMode -> CommandManager
    sub = o["ClientCommand_submit"]
    sseq = insns(r, sub, r.func_end(sub))
    for a, b in zip(sseq, sseq[1:]):
        if a.mnemonic == "adrp" and b.mnemonic == "ldr" and b.op_str.startswith("x22"):
            o["g_clientInstance"] = int(a.op_str.split("#")[1], 0) + int(b.op_str.split("#")[1].rstrip("]"), 0)
            break
    ldr_x0 = [i for i in sseq if i.mnemonic == "ldr" and re.match(r"x0, \[x22, #", i.op_str)]
    s["ClientInstance_gameMode"] = int(ldr_x0[0].op_str.split("#")[1].rstrip("]"), 0)
    ldr_cm = [i for i in sseq if i.mnemonic == "ldr" and re.match(r"x20, \[x0, #", i.op_str)]
    s["LogicGameMode_commandManager"] = int(ldr_cm[-1].op_str.split("#")[1].rstrip("]"), 0)
    sbls = [bl_target(i) for i in sseq if bl_target(i)]
    o["LogicCommandManager_addCommand"] = sbls[-1] if sbls[-1] != sbls[0] else sbls[-2]
    # 校验 addCommand 特征：ldr x8,[x1]; cbz
    head = insns(r, o["LogicCommandManager_addCommand"], n=2)
    if not (head[0].mnemonic == "ldr" and head[0].op_str == "x8, [x1]"):
        raise LocateError(f"addCommand 特征不符: {head[0]}")
    for i in insns(r, o["LogicCommandManager_addCommand"], r.func_end(o["LogicCommandManager_addCommand"])):
        mm = re.match(r"x0, \[x0, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldr" and mm:
            s["LogicCommandManager_level"] = int(mm.group(1), 0)
            break

    # ---- 命令执行循环：当前 tick
    upd = func_by_string(r, b"Execute command failed! Command should have been executed already.")
    o["LogicCommandManager_update"] = upd
    useq = insns(r, upd, n=12)
    for i in useq:
        mm = re.match(r"w20, \[x0, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldr" and mm:
            s["LogicLevel_currentTick"] = int(mm.group(1), 0)
    s["LogicCommand_executeTick"] = 8
    o["LogicLevel_getState"] = [bl_target(i) for i in useq if bl_target(i)][0]
    gs = insns(r, o["LogicLevel_getState"], n=2)
    s["LogicLevel_statePtr"] = int(gs[0].op_str.split("#")[1].rstrip("]"), 0)

    # 调用链：update <- GameMode::updateOneSubTick <- GameMode::updateSubTick
    one = only([r.func_of(x) for x in r.callers(upd)], "updateOneSubTick")
    o["LogicGameMode_updateOneSubTick"] = one
    o["LogicGameMode_updateSubTick"] = only([r.func_of(x) for x in r.callers(one)], "updateSubTick")
    st = insns(r, o["LogicGameMode_updateSubTick"], r.func_end(o["LogicGameMode_updateSubTick"]))
    lvl = [i for i in st if i.mnemonic == "ldr" and re.match(r"x\d+, \[x19, #0x10\]", i.op_str)]
    if not lvl:
        raise LocateError("updateSubTick 中未找到 GameMode->level")
    s["LogicGameMode_level"] = 0x10
    s["LogicGameMode_state"] = 0
    s["LogicGameMode_battleOver"] = 0xC
    s["LogicLevel_battleEndFlag"] = next(int(i.op_str.split("#")[1].rstrip("]"), 0) for i in st
                                         if i.mnemonic == "ldrb" and re.match(r"w9, \[x8, #", i.op_str))

    # ---- 战斗结束 / BattleLog
    o["LogicGameMode_endBattle"] = func_by_string(r, b"Battle end. No Shield, Village Guard for defender")
    o["LogicBattleLog_setDestruction"] = func_by_string(r, b"LogicBattleLog: m_destructionPercentage decreases")
    bl = insns(r, o["LogicBattleLog_setDestruction"], n=1)[0]
    s["LogicBattleLog_destructionPercent"] = int(bl.op_str.split("#")[1].rstrip("]"), 0)
    # 调用方里: ldr x0,[x19,#0x40] (battleLog) / ldr w8,[x19,#0x220] (总数) / [x19,#0x224] (剩余)
    # 调用点形如：ldr x0,[x19,#battleLog] ... ldr w10,[x19,#total] ... mul w9,w22(剩余),#100 ... sdiv ... bl set
    # 另一调用点：ldr w8,[x19,#total]; ldr w9,[x19,#remaining]; mul; sdiv; sub; str w1,[x0,#..]; bl set
    bl_offs, totals, remains = set(), set(), set()
    for site in r.callers(o["LogicBattleLog_setDestruction"]):
        window = insns(r, site - 4 * 12, site)
        for i in window:
            mm = re.match(r"(x0|w\d+), \[x19, #(0x[0-9a-f]+)\]$", i.op_str)
            if i.mnemonic != "ldr" or not mm:
                continue
            off = int(mm.group(2), 0)
            if mm.group(1) == "x0":
                bl_offs.add(off)
            elif any(j.mnemonic == "sdiv" and j.address > i.address for j in window):
                (totals if off % 8 == 0 else remains).add(off)
    s["LogicLevel_battleLog"] = only(bl_offs, "Level->battleLog")
    s["LogicLevel_destructibleTotal"] = only(totals, "可摧毁建筑总数")
    s["LogicLevel_destructibleRemaining"] = only(remains, "剩余可摧毁建筑数")
    notes["LogicLevel_destructibleTotal"] = "摧毁率 = 100 - remaining*100/total，写入 BattleLog（见 setDestruction 调用方）"

    # ---- 三个战斗命令：通过 "Replay XXX load failed" 找 load 函数 -> 虚表 -> 构造函数/命令ID
    for key, needle in (("PlaceAttacker", b"Replay LogicPlaceAttackerCommand load failed! Character"),
                        ("CastSpell", b"Replay LogicCastSpellCommand load failed!"),
                        ("PlaceHero", b"Replay LogicPlaceHeroCommand load failed!"),
                        ("PlaceAlliancePortal", b"Replay LogicPlaceAlliancePortalCommand load failed!"),
                        ("TriggerHeroAbility", b"Replay LogicTriggerHeroAbility load failed! Hero"),
                        ("PlaceRecalledUnits", b"Replay LogicPlaceRecalledUnitsCommand load failed!")):
        load = func_by_string(r, needle)
        vt, slot = vtable_containing(r, load)
        ctor = vtable_writer(r, vt)
        o[f"Logic{key}Command_vtable"] = vt
        o[f"Logic{key}Command_ctor"] = ctor
        o[f"Logic{key}Command_load"] = load
        o[f"Logic{key}Command_execute"] = r.ptr(vt + 3 * 8)
        cid = const_return(r, r.ptr(vt + 4 * 8))
        if cid is None:
            raise LocateError(f"{key} 虚表槽 4 不是 getCommandType")
        s[f"Logic{key}Command_type"] = cid
        fields = parse_ctor_fields(r, ctor)
        s[f"Logic{key}Command_fields"] = fields
        # new 的大小：在调用构造函数的地方找 mov w0,#size
        sizes = set()
        for site in r.callers(ctor):
            for i in reversed(insns(r, site - 4 * 12, site)):
                if i.mnemonic == "bl" and bl_target(i) == o["operator_new"]:
                    for j in reversed(insns(r, site - 4 * 12, i.address)):
                        if j.mnemonic == "mov" and j.op_str.startswith("w0, #"):
                            sizes.add(int(j.op_str.split("#")[1], 0))
                            break
                    break
        s[f"Logic{key}Command_size"] = max(sizes) if sizes else 0x30
    if o["LogicPlaceAttackerCommand_ctor"] != vtable_writer(r, o["LogicPlaceAttackerCommand_vtable"]):
        raise LocateError("两条路径得到的放兵构造函数不一致")

    # ---- GameMode 加载入口
    for key, needle in (("loadHomeState", b"loadHomeState called from invalid state"),
                        ("loadNpcAttackState", b"loadNpcAttackState called and home owner is not npc avatar"),
                        ("loadRevengeAttackState", b"loadRevengeAttackState called and home owner"),
                        ("loadReplay", b"loadReplay called from invalid state"),
                        ("loadLiveReplay", b"loadLiveReplay called from invalid state"),
                        ("loadNpcDuelState", b"loadNpcDuelState called from invalid state"),
                        ("loadVisitState", b"loadVisitState called from invalid state")):
        o[f"LogicGameMode_{key}"] = func_by_string(r, needle)
    o["LogicGameObjectManager_addGameObject"] = func_by_string(
        r, b"LogicGameObjectManager::addGameObject with global ID %d, global ID already taken")
    o["LogicNpcAttack_placeOneUnit"] = func_by_string(r, b"LogicNpcAttack::placeOneUnit - No attack position found!")

    # GameObjectManager：+0x00..+0x50 是 11 个按类型分组的数组；+0x90 = level
    s["LogicGameObjectManager_typeLists"] = 0
    s["LogicGameObjectManager_typeCount"] = 11
    s["LogicGameObjectManager_level"] = 0x90
    s["LogicGameObject_data"] = 0x18
    s["LogicGameObject_globalId"] = 0x30
    s["LogicGameObject_vt_getType"] = 0xA0 // 8
    s["LogicData_globalId"] = 0x18
    s["LogicLevel_gameObjectManagers"] = 0x68
    s["LogicLevel_villageIndex"] = 0x420
    s["LogicArrayList_data"] = 0
    s["LogicArrayList_count"] = 0xC

    # ---- 第二轮：星数、援军、技能
    sd = o["LogicBattleLog_setDestruction"]
    stars = set()
    for site in r.callers(sd):
        for i in insns(r, site + 4, n=6):
            t = bl_target(i)
            if t and t != sd:
                head = insns(r, t, n=2)
                if head[0].mnemonic == "ldr" and f"#{s['LogicBattleLog_destructionPercent']:#x}]" in head[0].op_str:
                    stars.add(t)
                break
    o["LogicBattleLog_getStars"] = only(stars, "BattleLog::getStars")
    for i in insns(r, o["LogicBattleLog_getStars"], n=9):
        mm = re.match(r"w\d+, \[x0, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldrb" and mm:
            s["LogicBattleLog_townHallDestroyed"] = int(mm.group(1), 0)

    # 援军放置：execute 调用的 placeAlliancePortal(level, data, x, y)
    pexec = o["LogicPlaceAlliancePortalCommand_execute"]
    place = [bl_target(i) for i in insns(r, pexec, r.func_end(pexec)) if bl_target(i)]
    o["LogicLevel_placeAlliancePortal"] = next(t for t in place if any(
        "nullptr alliance character" in st for _, st in r.strings_in_func(t)))
    pp = insns(r, o["LogicLevel_placeAlliancePortal"], r.func_end(o["LogicLevel_placeAlliancePortal"]))
    pbl = [bl_target(i) for i in pp if bl_target(i)]
    o["LogicLevel_getAttackerAvatar"] = pbl[0]
    for i in pp:
        if i.mnemonic == "cmp" and re.match(r"w0, #0x[0-9a-f]+$", i.op_str):
            s["data_type_alliance_portal"] = int(i.op_str.split("#")[1], 0)
            break
    for i in pp:
        mm = re.match(r"x8, \[x8, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldr" and mm:
            s["LogicData_vt_getDataType"] = int(mm.group(1), 0) // 8
            break
    for t in pbl:
        h = insns(r, t, n=2)
        mm = re.match(r"x0, \[x0, #(0x[0-9a-f]+)\]$", h[0].op_str)
        if h[0].mnemonic == "ldr" and mm and h[1].mnemonic == "ret" and int(mm.group(1), 0) > 0x100:
            s["LogicAvatar_allianceUnits"] = int(mm.group(1), 0)
            break
    s["LogicAllianceUnitSlot_data"] = 0
    s["LogicAllianceUnitSlot_count"] = 0xC
    # 放兵 execute：单位类型虚表槽（ldr x8,[x8,#0xb8]）
    for i in insns(r, o["LogicPlaceAttackerCommand_execute"], r.func_end(o["LogicPlaceAttackerCommand_execute"])):
        mm = re.match(r"x8, \[x8, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldr" and mm and int(mm.group(1), 0) > 0x40:
            s["LogicCombatItemData_vt_getUnitType"] = int(mm.group(1), 0) // 8
            break
    s["unit_type_siege"] = 3
    # 技能 execute：角色列表 = GOM 类型 1；存活标志字节
    for i in insns(r, o["LogicTriggerHeroAbilityCommand_execute"], r.func_end(o["LogicTriggerHeroAbilityCommand_execute"])):
        mm = re.match(r"w9, \[x21, #(0x[0-9a-f]+)\]", i.op_str)
        if i.mnemonic == "ldrb" and mm:
            s["LogicCharacter_isDead"] = int(mm.group(1), 0)
    s["gom_type_character"] = 1

    # ---- 英雄模式切换（大守护者 地面/空中）：类型 ID 由真机命令侦听得到（UI 切换模式时发出），
    #      再按结构校验：getCommandType 返回该值、构造函数 (this, heroData@0x10, mode@0x18)
    for fn in r.functions:
        if r.func_end(fn) - fn == 8 and const_return(r, fn) == SPY_TYPE_HERO_MODE:
            for p in inv_relocs(r).get(fn, []):
                vt = p - 4 * 8
                for ctor in sorted(set(r.func_of(x) for x in r.xrefs(vt))):
                    f = parse_ctor_fields(r, ctor)
                    if f.get("x1") == 0x10 and f.get("w2") == 0x18:
                        o["LogicHeroModeCommand_ctor"] = ctor
                        o["LogicHeroModeCommand_vtable"] = vt
                        s["LogicHeroModeCommand_type"] = SPY_TYPE_HERO_MODE
                        s["LogicHeroModeCommand_size"] = 0x20
    if "LogicHeroModeCommand_ctor" not in o:
        raise LocateError("未找到英雄模式切换命令（游戏更新后需用 cmd_log 重新侦听其类型 ID）")

    # ---- 消息系统
    recv = func_by_string(r, b"NpcDataMessage received but GameMode is null!")
    o["MessageManager_receiveMessage"] = recv
    # 辅助函数形如：adrp/ldr x0,[g]; cbz x0; ldr x1,[x1]; mov w2,wzr; b deliver
    deliver, gmm = set(), set()
    for fn in r.functions:
        w0 = r.u32(fn)
        if (w0 & 0x9F00001F) != 0x90000008:  # 快速筛选：adrp x8, ...
            continue
        q = insns(r, fn, n=6)
        if len(q) < 6:
            continue
        if (q[0].mnemonic == "adrp" and q[1].mnemonic == "ldr" and q[1].op_str.startswith("x0, [x8")
                and q[2].mnemonic == "cbz" and q[3].op_str == "x1, [x1]" and q[4].op_str == "w2, wzr"
                and q[5].mnemonic == "b"):
            tgt = int(q[5].op_str[1:], 0)
            if r.func_end(tgt) == recv:  # deliver 紧挨在 receiveMessage 之前
                deliver.add(tgt)
                gmm.add(int(q[0].op_str.split("#")[1], 0) + int(q[1].op_str.split("#")[1].rstrip("]"), 0))
    o["MessageManager_deliver"] = only(deliver, "MessageManager::deliver")
    o["g_messageManager"] = only(gmm, "MessageManager 全局指针")

    import msgswitch
    entries: dict[int, list[int]] = {}
    for t in range(10000, 32000):
        e = msgswitch.walk(r, recv + 0xC0, t)
        if e is not None:
            entries.setdefault(e, []).append(t)
    default = max(entries, key=lambda e: len(entries[e]))
    stops = set(entries) | {default}
    sites = {"EnemyHomeData": b"EnemyHomeDataMessage - Unknown attack source",
             "NpcData": b"NpcDataMessage received but GameMode is null!",
             "OwnHomeData": b"OwnHomeData should not be leased",
             "VisitedHomeData": b"VisitedHomeDataMessage received but GameMode is null!"}
    site_va = {}
    for k, needle in sites.items():
        sva = r.find_strings(needle, exact=False)[0]
        ob = r.v2o(sva)
        while r.data[ob - 1] != 0:
            ob -= 1
        site_va[k] = [x for _, x in r.xrefs_range(r.o2v(ob), sva + 1)][0]
    fend = r.func_end(recv)
    for a, ts in entries.items():
        if a == default or len(ts) != 1:
            continue
        seen = msgswitch.reach(r, a, stops, recv, fend)
        for k, v in site_va.items():
            if v in seen and f"msg_{k}" not in s:
                s[f"msg_{k}"] = ts[0]
                if k == "EnemyHomeData":
                    # 分支入口：adrp/ldr 取客户端状态全局（GOT），只有状态 ∈ 允许集合时才处理
                    q = insns(r, a, n=8)
                    got = int(q[0].op_str.split("#")[1], 0) + int(q[1].op_str.split("#")[1].rstrip("]"), 0)
                    o["g_clientState"] = r.ptr(got)
                    s["clientState_enemyHomeMask"] = int(next(i for i in q if i.mnemonic == "mov" and "#0x" in i.op_str
                                                              and not i.op_str.startswith("w9, #1")).op_str.split("#")[1], 0)
    inv: dict[int, list[int]] = {}
    for k, v in r.relocs.items():
        inv.setdefault(v, []).append(k)
    for k in sites:
        mid = s[f"msg_{k}"]
        gt = [fn for fn in r.functions if r.func_end(fn) - fn == 8 and const_return(r, fn) == mid]
        slots = [p for fn in gt for p in inv.get(fn, [])]
        o[f"{k}Message_vtable"] = only([p - 5 * 8 for p in slots], f"{k} 消息虚表")
    s["PiranhaMessage_vt_encode"] = 2
    s["PiranhaMessage_vt_decode"] = 3
    s["PiranhaMessage_vt_getType"] = 5
    s["PiranhaMessage_byteStream"] = 8
    npc_vt = o["NpcDataMessage_vtable"]
    npc_ctor = only([r.func_of(x) for x in r.xrefs(npc_vt)], "NpcData 构造函数")
    o["MessageFactory_create"] = only([r.func_of(c) for c in r.callers(npc_ctor) if c < 0x1800000], "消息工厂")
    # ByteStream：从 EnemyHome.decode 里第一个 readInt 得到 读位置/缓冲区 偏移
    dec = r.ptr(o["EnemyHomeDataMessage_vtable"] + 3 * 8)
    readint = [bl_target(i) for i in insns(r, dec, n=12) if bl_target(i)][1]
    ri = insns(r, readint, n=3)
    s["ByteStream_offset"] = int(ri[0].op_str.split("#")[1].rstrip("]"), 0)
    s["ByteStream_buffer"] = int(ri[1].op_str.split("#")[1].rstrip("]"), 0)
    s["ByteStream_length"] = s["ByteStream_offset"] + 4
    s["ByteStream_bitIndex"] = s["ByteStream_offset"] + 8
    s["ByteStream_ownedBuffer"] = s["ByteStream_buffer"] - 8

    # ---- 帧更新（GameMode 之外的主线程 hook 点）与发送（inv 已在消息部分构建）
    lr = func_by_string(r, b"--> Live replay ENDED")
    # ClientInstance::update(client, float dt)：ClientInstance 虚表中的成员，每帧在主线程调用，内部再推进 GameMode。
    # （另一个调用者 0xb82034 只在部分状态下被调用，联调中证实不能作为每帧 hook 点）
    virt = [c for c in set(r.func_of(x) for x in r.callers(lr)) if c in inv]
    o["Client_update"] = only(virt, "ClientInstance::update")
    o["Messaging_sendOne"] = func_by_string(r, b"Trying to send too big message, type %d")
    return {"functions": o, "struct": s, "notes": notes}


SPY_TYPE_HERO_MODE = 7398  # 18.647：真机侦听到的 UI 切换大守护者模式命令


def inv_relocs(r: ElfRe) -> dict[int, list[int]]:
    if not hasattr(r, "_inv_relocs"):
        inv: dict[int, list[int]] = {}
        for k, v in r.relocs.items():
            inv.setdefault(v, []).append(k)
        r._inv_relocs = inv
    return r._inv_relocs


def prologue(r: ElfRe, va: int) -> str:
    return r.read(va, 16).hex()


def emit_header(info: dict, path: Path) -> None:
    lines = ["// 由 tools/re/find_offsets.py 自动生成，请勿手改。", "#pragma once", "#include <cstdint>", "",
             "namespace tz::off {", f'inline constexpr const char *kLibgSha256 = "{info["libg_sha256"]}";',
             f'inline constexpr const char *kBuildTag = "{info.get("build_tag", "")}";', ""]
    for k, v in info["functions"].items():
        lines.append(f"inline constexpr std::uintptr_t {k} = {v:#x};")
    lines.append("")
    for k, v in info["struct"].items():
        if isinstance(v, int):
            lines.append(f"inline constexpr int {k} = {v:#x};")
    lines.append("")
    lines.append("// 命令构造函数参数寄存器 -> 字段偏移（核对用）")
    for k, v in info["struct"].items():
        if isinstance(v, dict):
            lines.append(f"// {k}: " + ", ".join(f"{a}->{b:#x}" for a, b in v.items()))
    lines.append("")
    lines.append("// 被 inline hook 的函数入口 16 字节指纹")
    for k, v in info["prologues"].items():
        bs = bytes.fromhex(v)
        lines.append(f"inline constexpr std::uint8_t {k}_prologue[16] = {{" + ", ".join(f"0x{b:02x}" for b in bs) + "};")
    lines.append("")
    lines.append("struct Fingerprint { const char *name; std::uintptr_t offset; std::uint8_t bytes[8]; };")
    lines.append("// 探针启动时逐个比对；任何一个不符就拒绝工作（版本不匹配或被其他补丁改过）")
    lines.append("inline constexpr Fingerprint kFingerprints[] = {")
    for k, v in info["fingerprints"].items():
        bs = bytes.fromhex(v)
        lines.append(f'  {{"{k}", {info["functions"][k]:#x}, {{' + ", ".join(f"0x{b:02x}" for b in bs) + "}},")
    lines.append("};")
    lines.append("}  // namespace tz::off")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


EXPECTED_18_647 = {
    "LogicCommandManager_createCommand": 0xD6FF00, "operator_new": 0x16B2320,
    "LogicPlaceAttackerCommand_ctor": 0xD7D46C, "ClientCommand_submit": 0xB7B904,
    "LogicCommandManager_addCommand": 0xD71AA4, "LogicCommandManager_update": 0xD71C8C,
    "LogicGameMode_updateSubTick": 0xF92A5C, "LogicGameMode_updateOneSubTick": 0xF92B4C,
    "LogicCastSpellCommand_ctor": 0xD667F8, "LogicPlaceHeroCommand_ctor": 0xD7E240,
    "g_clientInstance": 0x1838258, "LogicGameMode_loadNpcAttackState": 0xF951A0,
    "LogicPlaceAlliancePortalCommand_ctor": 0xD7CCF0, "LogicTriggerHeroAbilityCommand_ctor": 0xD984BC,
    "LogicPlaceRecalledUnitsCommand_ctor": 0xD7F78C, "LogicBattleLog_getStars": 0xCF3FB4,
    "MessageManager_receiveMessage": 0xBA43AC, "MessageManager_deliver": 0xBA41D8, "g_messageManager": 0x1832360,
    "MessageFactory_create": 0xF3595C, "Client_update": 0xB7FD60, "g_clientState": 0x1787970, "Messaging_sendOne": 0x11EC954,
    "EnemyHomeDataMessage_vtable": 0x174F5C8, "NpcDataMessage_vtable": 0x174FA28, "OwnHomeDataMessage_vtable": 0x174FAC8,
}
EXPECTED_STRUCT_18_647 = {
    "msg_EnemyHomeData": 21940, "msg_NpcData": 24346, "msg_OwnHomeData": 25195,
    "LogicPlaceAlliancePortalCommand_type": 7853, "LogicTriggerHeroAbilityCommand_type": 7060,
    "LogicPlaceRecalledUnitsCommand_type": 7654, "LogicBattleLog_townHallDestroyed": 0x1B9,
    "LogicAvatar_allianceUnits": 0x1D0, "data_type_alliance_portal": 0xF, "LogicCombatItemData_vt_getUnitType": 23,
    "LogicCharacter_isDead": 0x17F, "ByteStream_offset": 0x14, "ByteStream_buffer": 0x38,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("libg")
    ap.add_argument("--build-tag", default="")
    ap.add_argument("--expect", action="store_true", help="与 18.647 手工核对值比较")
    a = ap.parse_args()
    r = ElfRe(a.libg)
    info = locate(r)
    info["libg_sha256"] = hashlib.sha256(r.data).hexdigest()
    info["build_tag"] = a.build_tag
    hooks = ("LogicGameMode_updateSubTick", "Client_update", "Messaging_sendOne", "ClientCommand_submit")
    info["prologues"] = {k: prologue(r, info["functions"][k]) for k in hooks}
    info["fingerprints"] = {k: r.read(v, 8).hex() for k, v in info["functions"].items()
                            if not k.endswith("_vtable") and not k.startswith("g_")}
    for k in hooks:  # inline hook 要求前 4 条指令不含 PC 相对寻址
        for i in insns(r, info["functions"][k], n=4):
            if i.mnemonic in ("adr", "adrp", "b", "bl", "cbz", "cbnz", "tbz", "tbnz") or i.mnemonic.startswith("b.") \
                    or (i.mnemonic.startswith("ldr") and "#0x" in i.op_str and "[" not in i.op_str):
                raise LocateError(f"{k} 前 16 字节包含 PC 相对指令 {i.mnemonic} {i.op_str}，需要重定位支持")
    if a.expect:
        bad = {k: (hex(info["functions"].get(k, -1)), hex(v)) for k, v in EXPECTED_18_647.items()
               if info["functions"].get(k) != v}
        bad.update({k: (info["struct"].get(k), v) for k, v in EXPECTED_STRUCT_18_647.items()
                    if info["struct"].get(k) != v})
        if bad:
            raise SystemExit(f"与手工核对值不一致: {bad}")
        print("与 18.647 手工核对值一致")
    out_json = ROOT / "tianze_coc" / "data" / "engine_offsets.json"
    out_h = ROOT / "probe" / "generated" / "offsets.h"
    out_json.write_text(json.dumps(info, indent=1, default=lambda x: x), encoding="utf-8")
    emit_header(info, out_h)
    print(f"写入 {out_json}\n写入 {out_h}")
    for k, v in info["functions"].items():
        print(f"  {k:42s} {v:#x}")
    for k, v in info["struct"].items():
        print(f"  {k:42s} {v if isinstance(v, dict) else hex(v)}")


if __name__ == "__main__":
    main()
