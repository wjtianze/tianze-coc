"""自对弈对局组装：把 摆阵模型 / 配兵模型 的输出拼成一条可离线重放的 EnemyHomeData。

    match = Match(defender=防守方账号, layout=阵型(含防守援军), attacker=进攻方账号, army=配兵)
    payload = build_enemy_payload(模板录制, match)
    env = TianzeBattleEnv(battle_starter=OfflineStarter(home, enemy, payload_fn=lambda i: payload))

模板录制只提供消息骨架（任意一份真实的 EnemyHomeData，例如友谊战录制）；双方的建筑、等级、阵型、
援军、配兵全部被替换。双方都先用 Account.validate / layout.validate / army.validate 校验合法性。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import army as army_mod
from . import layout as layout_mod
from .account import Account
from .army import Army


@dataclass
class Match:
    defender: Account
    attacker: Account
    army: Army
    layout: layout_mod.Layout | None = None  # None = 用 defender 当前坐标


def check(m: Match, require_all_built: bool = True) -> list[str]:
    errs = [f"防守方：{v}" for v in m.defender.validate(require_all_built=require_all_built)]
    if m.layout is not None:
        errs += [f"阵型：{e}" for e in layout_mod.validate(m.layout, m.defender)]
    errs += [f"进攻方：{v}" for v in m.attacker.validate(require_all_built=require_all_built,
                                                     check_structures=m.attacker.home is not None)]
    errs += [f"配兵：{e}" for e in army_mod.validate(m.army, m.attacker)]
    return errs


def build_enemy_payload(template: bytes, m: Match, strict: bool = True) -> bytes:
    if strict:
        errs = check(m)
        if errs:
            raise ValueError("对局不合法：" + "；".join(errs[:8]))
    d = m.defender
    if m.layout is not None:
        layout_mod.apply(m.layout, d, check=False)
    p = d.apply_to_payload(template, "defender", clear_obstacles=True)
    p = m.attacker.apply_to_payload(p, "attacker")
    return army_mod.apply_to_enemy_payload(p, m.army)
