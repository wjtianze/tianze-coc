"""python -m tianze_coc.assets <APK或解包目录> [-o 输出json] [--markdown 输出md]"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .csv_codec import AssetSource
from .unit_catalog import ALLOWED_STATUSES, TEMPORARY_STATUSES, build_catalog

STATUS_CN = {
    "permanent": "永久",
    "temporary_event": "限时（禁用）",
    "super_troop": "超级兵",
    "unreleased": "未上线",
    "internal": "内部/不可部署",
    "builder_base": "夜世界",
}
KIND_CN = {"troop": "兵种", "siege": "攻城机器", "spell": "法术", "hero": "英雄", "equipment": "英雄装备", "pet": "战宠"}


def to_markdown(cat: dict) -> str:
    lines = [f"# 角色可用性表（版本 {cat['build_tag']}）", "",
             "由 `python -m tianze_coc.assets` 从 APK 的 CSV 自动生成。",
             "规则：部落对战和排位战能带的，模拟器里就能用。依据游戏提示文本："
             "“限时兵种和法术无法在排位战或部落对战中使用”；超级兵激活后可用。", ""]
    lines.append("| 状态 | 数量 |\n| --- | --- |")
    for k, v in sorted(cat["summary"].items()):
        lines.append(f"| {STATUS_CN.get(k, k)} | {v} |")
    for title, pred in (("可用角色（部落战/排位战可携带）", lambda u: u["status"] in ALLOWED_STATUSES),
                        ("禁用角色（限时兵种/法术）", lambda u: u["status"] in TEMPORARY_STATUSES)):
        lines += ["", f"## {title}", "", "| 类型 | GlobalID | 名称 | 中文名 | 人口 | 状态 | 依据 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for u in sorted((u for u in cat["units"] if pred(u)), key=lambda u: (u["kind"], u["global_id"])):
            lines.append(f"| {KIND_CN.get(u['kind'], u['kind'])} | {u['global_id']} | {u['name']} | {u['name_cn']} | "
                         f"{u['housing_space']} | {STATUS_CN.get(u['status'], u['status'])} | {u['reason']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="从 APK 生成永久/临时角色表")
    ap.add_argument("source", help="APK 文件或已解包目录")
    ap.add_argument("-o", "--output", default=str(Path(__file__).resolve().parent.parent / "data" / "unit_catalog.json"))
    ap.add_argument("--markdown", help="同时输出 Markdown 表")
    ap.add_argument("--lang", default="cn")
    a = ap.parse_args()
    cat = build_catalog(AssetSource(a.source), a.lang)
    Path(a.output).write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {a.output}: {cat['summary']}")
    if a.markdown:
        Path(a.markdown).write_text(to_markdown(cat), encoding="utf-8")
        print(f"写入 {a.markdown}")


if __name__ == "__main__":
    main()
