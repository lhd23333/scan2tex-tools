# -*- coding: utf-8 -*-
"""图片命名映射表：干跑命名逻辑，输出每个图块（页/块号）→ 文件名。

用途：核查 tex 端与导出端（json_to_readable）的命名一致性。两处共用
figure_info / unique_image_name，但**运行中进程不会随磁盘代码更新**——
实测事故：导出进程启动于 caption_filename 支持字母章号之前，附录图（图A.x）
全部算成回退名 p416-b2.png 且幂等跳过，与 tex 端失配（49 张缺图/孤儿对）。

输出 CSV（utf-8-sig，Excel 可直接开）：page,index,type,name,caption
核对方法：check_images 报"缺图/孤儿"时，用本表把孤儿文件（p{页}-b{块}.png）
反查该块应有的新名，确认配对后即可归档旧文件。

用法:
  python 工具/dump_image_map.py <result.json> [-o image_map.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from json_to_readable import (  # noqa: E402
    SKIP_TYPES,
    figure_info,
    infer_missing_captions,
    unique_image_name,
)


def main():
    ap = argparse.ArgumentParser(description="图片块 → 文件名映射 dump")
    ap.add_argument("json_path", help="result.json 路径")
    ap.add_argument("-o", "--out", default=None, help="输出 CSV（默认 json 同目录/image_map.csv）")
    args = ap.parse_args()

    src = Path(args.json_path)
    data = json.loads(src.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else src.parent / "image_map.csv"

    rows = []
    used: set[str] = set()
    pages = sorted(data.get("pages", []), key=lambda p: p.get("page_idx", 0))
    for page in pages:
        page_idx = page.get("page_idx", 0)
        blocks = sorted(page.get("blocks", []), key=lambda b: b.get("index", 0))
        cap_map = infer_missing_captions(blocks)
        for bi, b in enumerate(blocks):
            if b.get("type") not in ("image", "chart"):
                continue
            cap, _bbox = figure_info(b, blocks, bi, cap_map)
            name = unique_image_name(cap, page_idx + 1, b.get("index", 0), used)
            rows.append([page_idx + 1, b.get("index", 0), b.get("type"), name, cap])

    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["page", "index", "type", "name", "caption"])
        w.writerows(rows)
    print(f"共 {len(rows)} 个图块 → {out}")


if __name__ == "__main__":
    main()
