# -*- coding: utf-8 -*-
"""核对 book.tex 的图片引用与 images/ 目录实际文件。

两个方向都查：
  缺图：tex 引用了但文件不存在（编译会报 File not found）
  孤儿：文件在但没被任何 \\includegraphics 引用（改名/重导出遗留，可安全删除）

孤儿判定注意：矢量化产物（图X.Y.pdf）与其位图源（图X.Y.png）通常成套存在，
tex 只引用其一（pdf 优先）——对应 PNG 不算孤儿（是矢量化的源文件）。

用法:
  python 工具/check_images.py <book.tex> <images目录> [--delete]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="核对 tex 图片引用")
    ap.add_argument("tex_path")
    ap.add_argument("images_dir")
    ap.add_argument("--delete", action="store_true", help="删除孤儿文件（默认只报告）")
    args = ap.parse_args()

    tex = Path(args.tex_path).read_text(encoding="utf-8")
    refs = {m.group(1) for m in re.finditer(r"\\includegraphics\[[^\]]*\]\{([^}]+)\}", tex)}
    ref_stems = {Path(r).stem for r in refs}

    d = Path(args.images_dir)
    files = sorted(f for f in d.iterdir() if f.suffix.lower() in (".png", ".pdf", ".svg"))

    missing = [r for r in sorted(refs) if not (d / r).exists()]
    orphans = []
    for f in files:
        if f.stem in ref_stems:
            continue
        # 矢量化源位图：同名 PDF 被引用时 PNG 保留
        if f.suffix.lower() == ".png" and (d / f"{f.stem}.pdf").exists():
            continue
        orphans.append(f)

    print(f"引用 {len(refs)} 个 | 目录文件 {len(files)} 个")
    if missing:
        print(f"\n缺图 {len(missing)} 个（编译会报错）:")
        for r in missing[:20]:
            print(f"  {r}")
    else:
        print("\n缺图: 无")
    if orphans:
        print(f"\n孤儿 {len(orphans)} 个（可删）:")
        for f in orphans[:30]:
            print(f"  {f.name}  {f.stat().st_size / 1024:.0f}KB")
        if args.delete:
            for f in orphans:
                f.unlink()
            print(f"已删除 {len(orphans)} 个")
    else:
        print("孤儿: 无")


if __name__ == "__main__":
    main()
