"""探针的 JSON 行协议客户端。"""
from __future__ import annotations

import json
import socket
from typing import Any


class ProbeError(RuntimeError):
    pass


class ProbeClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 27650, timeout: float = 30.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._seq = 0

    def connect(self) -> "ProbeClient":
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def __enter__(self) -> "ProbeClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, op: str, **kw: Any) -> dict:
        if self._sock is None:
            self.connect()
        self._seq += 1
        req = {"op": op, "seq": self._seq, **kw}
        self._sock.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode())
        while b"\n" not in self._buf:
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                self.close()
                raise ProbeError("探针连接已断开")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        resp = json.loads(line)
        if resp.get("seq") not in (None, self._seq):  # 每个请求都带 seq，用于发现错位
            raise ProbeError(f"响应序号不符: {resp.get('seq')} != {self._seq}")
        if not resp.get("ok", False):
            raise ProbeError(resp.get("error") or resp.get("init_error") or "未知错误")
        return resp

    # 便捷方法
    def hello(self) -> dict:
        return self.call("hello")

    def status(self) -> dict:
        return self.call("status")

    def config(self, **kw: Any) -> dict:
        return self.call("config", **kw)

    def step(self, subticks: int, actions: list[dict] | None = None, objects: bool = True,
             timeout_ms: int = 10000) -> dict:
        return self.call("step", subticks=subticks, actions=actions or [], objects=objects, timeout_ms=timeout_ms)

    def observe(self, objects: bool = True, timeout_ms: int = 5000) -> dict:
        return self.call("observe", objects=objects, timeout_ms=timeout_ms)

    def objects_raw(self, timeout_ms: int = 5000) -> dict:
        return self.call("objects_raw", timeout_ms=timeout_ms)

    def cmd_log(self, clear: bool = False) -> list[dict]:
        """命令侦听：经过客户端提交函数的命令（含游戏 UI 发出的）。"""
        return self.call("cmd_log", clear=clear)["commands"]

    # ---- 消息录制 / 重放 / 外发过滤
    def records(self) -> list[dict]:
        return self.call("records")["records"]

    def record_get(self, msg_type: int) -> bytes:
        return bytes.fromhex(self.call("record_get", type=msg_type)["hex"])

    def record_put(self, msg_type: int, payload: bytes) -> None:
        self.call("record_put", type=msg_type, hex=payload.hex())

    def replay(self, msg_type: int, timeout_ms: int = 10000) -> dict:
        return self.call("replay", type=msg_type, timeout_ms=timeout_ms)

    def net_config(self, **kw: Any) -> dict:
        return self.call("net_config", **kw)

    def net_log(self) -> dict:
        return self.call("net_log")

    def peek(self, addr: int, length: int = 64) -> bytes | None:
        h = self.call("peek", addr=addr, len=length).get("hex")
        return bytes.fromhex(h) if h else None
