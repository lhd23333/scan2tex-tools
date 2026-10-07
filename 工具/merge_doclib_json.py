# -*- coding: utf-8 -*-
"""合并 MinerU doclib 分片 JSON 为完整 result.json。

背景：doclib 按解析批次缓存分片（文件名形如 "1-98,103-491_时间戳.json"）。
同一 PDF 先测过几页、再整书解析时，已解析的页会复用旧缓存——最新的分片
反而是"残缺"的（如整书分片缺 99-102）。本脚本扫描全部编号分片，选页数最多
的分片为主干（保留其全部顶层字段），用其余分片补齐缺页后输出单一完整 JSON。

用法:
  python 工具/merge_doclib_json.py <doclib文档目录> -o result.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 分片文件名: "1-98,103-491_1791365183907.json"（页范围 1-based，允许逗号和区间）
_FRAG_RE = re.compile(r"^([\d,\-]+)_\d+\.json$")


def is_fragment(name: str) -> bool:
    return _FRAG_RE.match(name) is not None


def main():
    ap = argparse.ArgumentParser(description="合并 MinerU doclib 分片 JSON")
    ap.add_argument("docdir", help="doclib 文档目录（含 *_*.json 分片的目录）")
    ap.add_argument("-o", "--out", required=True, help="输出 result.json 路径")
    args = ap.parse_args()

    d = Path(args.docdir)
    frags = [f for f in sorted(d.rglob("*.json")) if is_fragment(f.name)]
    if not frags:
        raise SystemExit(f"未找到分片 JSON: {d}")

    loaded = []
    for f in frags:
        data = json.loads(f.read_text(encoding="utf-8"))
        loaded.append((f, data))
    loaded.sort(key=lambda t: len(t[1].get("pages", [])))

    main_name, main_data = loaded[-1]  # 主干 = 页数最多的分片
    print(f"主干: {main_name.name}（{len(main_data.get('pages', []))} 页）")

    pages: dict[int, dict] = {}
    layout: dict[int, dict] = {}
    ext = main_data.get("extensions", {}).get("docvortex_layout", {})
    for lp in ext.get("pages", []):
        layout[lp.get("page_idx", 0)] = lp

    # 先写补充分片（少→多，多页者覆盖），主干页最终覆盖
    for f, data in loaded[:-1] + [(main_name, main_data)]:
        added = 0
        for p in data.get("pages", []):
            pi = p.get("page_idx", 0)
            if pi not in pages:
                added += 1
            pages[pi] = p
        e = data.get("extensions", {}).get("docvortex_layout", {})
        for lp in e.get("pages", []):
            layout.setdefault(lp.get("page_idx", 0), lp)
        if f is not main_name:
            print(f"  补充: {f.name}（{len(data.get('pages', []))} 页，新页 {added}）")

    # 主干里缺的页 = 补充来源的有效页；检查最终连续性
    idxs = sorted(pages)
    gaps = [i for i in range(idxs[0], idxs[-1] + 1) if i not in pages] if idxs else []

    out = dict(main_data)
    out["pages"] = [pages[i] for i in idxs]
    if layout:
        out.setdefault("extensions", {})["docvortex_layout"] = {
            "pages": [layout[i] for i in sorted(layout)]}
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"输出: {args.out} 共 {len(pages)} 页"
          f"（page_idx {idxs[0]}..{idxs[-1]}）")
    if gaps:
        print(f"⚠ 缺页: {gaps}")


if __name__ == "__main__":
    main()
