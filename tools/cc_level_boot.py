"""调试用：以指定部落城堡等级离线启动（复制登录包并改主村数据里的城堡等级），用于在游戏内核对援军上限。

    python tools/cc_level_boot.py <登录包名> <城堡等级>
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tianze_coc import home_payload as hp  # noqa: E402
from tianze_coc.offline_boot import OfflineBoot, bundle_path  # noqa: E402

name, level = sys.argv[1], int(sys.argv[2])
src, dst = bundle_path(name), bundle_path(f"{name}cc{level}")
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst)
own = dst / "25195.bin"
j = hp.extract_home(own.read_bytes())
for b in j["buildings"]:
    if b["data"] == 1000014:
        b["lvl"] = level - 1
own.write_bytes(hp.replace_home_json(own.read_bytes(), j))
OfflineBoot(dst.name[:-len(".login")]).boot()
print(f"已以 {level} 级部落城堡离线启动")
