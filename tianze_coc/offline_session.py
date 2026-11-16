"""离线开局：录制服务器下发的村庄/战斗消息，之后在本地重放来反复开局，不依赖服务器。

流程
1. record：正常登录私服，在游戏里正常发起一次进攻（或单人关卡）。探针在解码时自动录下
   OwnHomeData（主村）与 EnemyHomeData / NpcData（敌方村庄）的原始负载；`save_records()` 存到磁盘。
2. 训练时：`OfflineStarter` 作为 TianzeBattleEnv 的 battle_starter：
   - 若当前不在主村，重放 OwnHomeData 回主村；
   - 重放敌方村庄消息（可先用 home_payload 换成任意阵型）→ 客户端照常加载进攻状态；
   - 离线模式下按配置拦截外发消息，不向私服提交战斗结果。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .home_payload import replace_home_json
from .local_config import DATA
from .native_client import ProbeClient, ProbeError

RECORD_DIR = DATA / "records"


def message_ids() -> dict[str, int]:
    s = json.loads((DATA / "engine_offsets.json").read_text(encoding="utf-8"))["struct"]
    return {k[4:]: v for k, v in s.items() if k.startswith("msg_")}


def save_records(client: ProbeClient, name: str, directory: Path = RECORD_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    ids = {v: k for k, v in message_ids().items()}
    out = []
    for r in client.records():
        payload = client.record_get(r["type"])
        p = directory / f"{name}.{ids.get(r['type'], r['type'])}.bin"
        p.write_bytes(payload)
        out.append(p)
    return out


def load_record(client: ProbeClient, path: Path, msg_type: int | None = None) -> int:
    if msg_type is None:
        kind = path.name.split(".")[-2]
        msg_type = message_ids()[kind]
    client.record_put(msg_type, path.read_bytes())
    return msg_type


@dataclass
class OfflineStarter:
    """TianzeBattleEnv(battle_starter=OfflineStarter(...)) 使用。"""

    home_record: Path
    enemy_record: Path
    enemy_kind: str = "EnemyHomeData"  # 或 "NpcData"
    layouts: list[dict] = field(default_factory=list)  # 非空时每局轮换替换村庄 JSON
    payload_fn: "Callable[[int], bytes] | None" = None  # 给定时每局调用 payload_fn(局号) 生成敌方负载（自对弈）
    block_outgoing: bool = True
    allow_types: list[int] = field(default_factory=list)  # 离线时仍放行的外发消息（例如保活）
    home_state: int = 1
    timeout_s: float = 60.0
    _round: int = 0

    def __call__(self, env) -> None:
        c: ProbeClient = env.client
        ids = message_ids()
        # deliver_mode=1：直接调用 MessageManager::receiveMessage（联调验证：deliver/改道桩都不会触发模式切换）
        c.net_config(offline=self.block_outgoing, block_all=self.block_outgoing, allow_types=self.allow_types,
                     deliver_mode=1)
        home_type = ids["OwnHomeData"]
        enemy_type = ids[self.enemy_kind]
        c.record_put(home_type, self.home_record.read_bytes())
        payload = self.payload_fn(self._round) if self.payload_fn else self.enemy_record.read_bytes()
        if self.layouts:
            payload = replace_home_json(payload, self.layouts[self._round % len(self.layouts)])
        self._round += 1
        c.record_put(enemy_type, payload)
        if c.status().get("gm_state") != self.home_state:
            self._replay(c, home_type)
            self._wait(c, lambda st: st.get("gm_state") == self.home_state, "回到主村")
        self._replay(c, enemy_type)

    def _replay(self, c: ProbeClient, msg_type: int) -> None:
        r = c.replay(msg_type, timeout_ms=int(self.timeout_s * 1000))
        if not str(r.get("status", "")).startswith("ok"):
            raise ProbeError(f"重放消息 {msg_type} 失败: {r}")

    def _wait(self, c: ProbeClient, pred, what: str) -> None:
        deadline = time.monotonic() + self.timeout_s
        while not pred(c.status()):
            if time.monotonic() > deadline:
                raise ProbeError(f"等待{what}超时")
            time.sleep(0.1)
