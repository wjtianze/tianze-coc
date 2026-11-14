"""模拟探针：实现与真实探针相同的 JSON 行协议，用一个极简战斗模型代替游戏引擎。

只用于测试中间层（协议、锁步、规则校验），不代表任何真实战斗数值。
    python -m tianze_coc.fake_probe --port 27650
"""
from __future__ import annotations

import argparse
import json
import socketserver
import threading
from dataclasses import dataclass, field

LOGIC_TILE = 512


@dataclass
class FakeBattle:
    army_units: dict[int, int] = field(default_factory=lambda: {4000000: 10, 4000011: 5, 4000098: 2})
    army_spells: dict[int, int] = field(default_factory=lambda: {26000000: 3})
    army_sieges: dict[int, int] = field(default_factory=lambda: {4000051: 1})
    cc_units: dict[int, int] = field(default_factory=lambda: {4000006: 3, 4000052: 1, 26000003: 1})  # 法师+攻城飞艇+弹跳
    th_destroyed: bool = False
    width: int = 50 * LOGIC_TILE
    height: int = 50 * LOGIC_TILE
    total: int = 40
    remaining: int = 40
    tick: int = 0
    deployed: int = 0
    max_ticks: int = 3600
    in_battle: bool = True

    @property
    def over(self) -> bool:
        return self.remaining == 0 or self.tick >= self.max_ticks


class FakeEngine:
    def __init__(self, battle: FakeBattle | None = None):
        self.battle = battle or FakeBattle()
        self.lock = threading.Lock()
        self.lockstep = False
        self.allowed: set[int] = set()
        self.object_fields: list[dict] = []
        self.results: list[dict] = []
        self.turbo = 1
        self.submit_mode = "client"
        self.heroes: set[int] = set()
        self.deployed: set[int] = set()
        self.cc_used = False
        self.records: dict[int, bytes] = {}
        self.gm_state = 2
        self.net = {"offline": False, "block_all": False}
        self.home_type, self.enemy_type = 25195, 21940

    def _apply(self, a: dict) -> dict:
        b = self.battle
        r = {"id": a["id"], "status": "rejected", "code": 0, "tick": b.tick}
        gid = int(a["data"])
        kind = a["kind"]
        if kind == "hero_mode":
            r["status"] = "executed"
            return r
        if kind not in ("cc", "recalled") and (not self.allowed or gid not in self.allowed):
            r["reason"] = "unit_not_allowed"
            return r
        if kind != "ability" and not (0 <= a["x"] < b.width and 0 <= a["y"] < b.height):
            r["reason"] = "out_of_map"
            return r
        if kind == "cc":
            if self.cc_used or not any(c for g, c in b.cc_units.items() if g != 4000052):
                r.update(code=-4, reason="engine_execute")
                return r
            self.cc_used = True
            b.deployed += 1
            r["status"] = "executed"
            return r
        if kind == "ability":
            if gid not in self.deployed:
                r.update(code=-1, reason="engine_execute")
                return r
            r["status"] = "executed"
            return r
        if kind == "recalled":
            r["status"] = "executed"
            return r
        if kind == "troop":
            pool = b.army_units
        elif kind == "siege":
            pool = b.army_sieges
        elif kind in ("cc_siege", "cc_spell"):
            pool = b.cc_units
        elif a["kind"] == "spell":
            pool = b.army_spells
        else:
            if gid in self.heroes:
                r.update(code=-3, reason="engine_execute")
                return r
            self.heroes.add(gid)
            pool = None
        if pool is not None:
            if pool.get(gid, 0) <= 0:
                r.update(code=-2, reason="engine_execute")
                return r
            pool[gid] -= 1
        b.deployed += 1
        self.deployed.add(gid)
        r["status"] = "executed"
        return r

    def _advance(self, n: int) -> None:
        b = self.battle
        for _ in range(n):
            if b.over:
                break
            b.tick += 1
            if b.deployed and b.tick % 20 == 0 and b.remaining > 0:
                b.remaining = max(0, b.remaining - min(b.deployed, 2))
                if b.remaining <= b.total // 3:
                    b.th_destroyed = True

    def snapshot(self, objects: bool) -> dict:
        b = self.battle
        d = {
            "gm_state": self.gm_state, "in_battle": b.in_battle, "battle_over": b.over, "tick": b.tick,
            "level_state": 5, "width": b.width, "height": b.height, "battle_end_flag": b.over,
            "destructible_total": b.total, "destructible_remaining": b.remaining,
            "destruction": 100 - b.remaining * 100 // b.total,
            "stars": (2 if b.remaining == 0 else int(100 - b.remaining * 100 // b.total > 49)) + int(b.th_destroyed),
            "th_destroyed": b.th_destroyed,
            "army": {"units": [[g, c] for g, c in b.army_units.items()],
                     "spells": [[g, c] for g, c in b.army_spells.items()],
                     "sieges": [[g, c] for g, c in b.army_sieges.items()],
                     "cc": [[g, c, 5] for g, c in b.cc_units.items()]},
        }
        if objects:
            d["objects"] = [{"t": 0, "gid": 500000000 + i, "data": 1000001} for i in range(b.remaining)]
        return d

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        out: dict = {"seq": req.get("seq"), "ok": True}
        with self.lock:
            if op == "hello":
                out.update(protocol=1, ready=True, build_tag="fake", fingerprint_mismatches=[])
            elif op == "status":
                out.update(ready=True, in_battle=self.battle.in_battle, lockstep=self.lockstep, gm_state=self.gm_state)
            elif op == "config":
                self.lockstep = req.get("lockstep", self.lockstep)
                self.turbo = req.get("turbo", self.turbo)
                self.submit_mode = req.get("submit_mode", self.submit_mode)
                if "allowed_ids" in req:
                    self.allowed = set(int(x) for x in req["allowed_ids"])
                if "object_fields" in req:
                    self.object_fields = req["object_fields"]
                if req.get("reset_counters"):
                    self.results.clear()
                out.update(lockstep=self.lockstep, allowed_count=len(self.allowed))
            elif op == "step":
                res = [self._apply(a) for a in req.get("actions", [])]
                self._advance(int(req.get("subticks", 1)))
                out.update(completed=True, results=res, obs=self.snapshot(req.get("objects", True)))
            elif op == "record_put":
                self.records[int(req["type"])] = bytes.fromhex(req["hex"])
            elif op == "records":
                out["records"] = [{"type": t, "len": len(b), "seq": 1, "from_python": True} for t, b in self.records.items()]
            elif op == "record_get":
                out["hex"] = self.records[int(req["type"])].hex()
            elif op == "replay":
                t = int(req["type"])
                if t not in self.records:
                    out = {"seq": req.get("seq"), "ok": False, "error": "no record for this message type"}
                elif t == self.home_type:
                    self.gm_state, self.battle.in_battle = 1, False
                    out["status"] = "ok"
                else:
                    self.battle = FakeBattle()
                    self.heroes.clear(); self.deployed.clear(); self.cc_used = False
                    self.gm_state = 2
                    out["status"] = "ok"
            elif op == "net_config":
                for k in ("offline", "block_all"):
                    if k in req:
                        self.net[k] = req[k]
                out.update(self.net)
            elif op == "observe":
                out.update(obs=self.snapshot(req.get("objects", True)), results=[])
            else:
                out = {"seq": req.get("seq"), "ok": False, "error": f"unknown op: {op}"}
        return out


def serve(engine: FakeEngine, port: int = 0) -> tuple[socketserver.ThreadingTCPServer, int]:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                if not line.strip():
                    continue
                resp = engine.handle(json.loads(line))
                self.wfile.write((json.dumps(resp) + "\n").encode())
                self.wfile.flush()

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=27650)
    a = ap.parse_args()
    srv, port = serve(FakeEngine(), a.port)
    print(f"模拟探针监听 127.0.0.1:{port}，Ctrl+C 退出")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
