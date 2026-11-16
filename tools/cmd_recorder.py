"""持续记录游戏 UI 发出的命令（带时间戳），用于摸清新机制的命令格式。

    python tools/cmd_recorder.py runs/cmd_capture.jsonl

每秒读取一次探针的命令侦听（读后清空），每条命令一行 JSON：
{"t": 本机时间, "tick", "type", "ours", "data", "enc"(encode 字节), "hex"(对象前 0x40 字节)}。
探针断开（游戏重启）时自动重连。Ctrl+C 结束。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc.native_client import ProbeClient, ProbeError  # noqa: E402


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "runs/cmd_capture.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    c = None
    n = 0
    with out.open("a", encoding="utf-8") as f:
        while True:
            try:
                if c is None:
                    c = ProbeClient(timeout=5).connect()
                    c.cmd_log(clear=True)
                    print(time.strftime("%H:%M:%S"), "已连接探针，开始记录", flush=True)
                for e in c.cmd_log(clear=True):
                    e = {"t": time.strftime("%H:%M:%S"), **e}
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    f.flush()
                    n += 1
                    print(e["t"], "type", e["type"], "data", e["data"], "enc", e.get("enc", "")[:64], flush=True)
            except (OSError, ProbeError) as ex:
                if c is not None:
                    print(time.strftime("%H:%M:%S"), "探针断开：", ex, flush=True)
                c = None
                time.sleep(2)
                continue
            time.sleep(1)


if __name__ == "__main__":
    main()
