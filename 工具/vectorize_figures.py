# -*- coding: utf-8 -*-
"""批量矢量化插图：MinerU 导出的扫描 PNG → 矢量 PDF/SVG。

管线（调研 + 三轮实测选定，见 调研/工具清单-图片矢量化篇.md 第八节）：
  灰度 → mkbitmap(-f 4 -s 3 -t 0.45) → potrace（-b pdf / -b svg）

关键：mkbitmap 的 -s 3 在阈值化前先平滑放大 3 倍，抗锯齿边缘变亚像素级——
这是"字形不走样、线条不加肥"的核心（vtracer 各模式实测不达标已弃用）。

用法:
  python 工具/vectorize_figures.py <images目录> [--format pdf|svg|both]
产物: 与 PNG 同目录同名 .pdf/.svg（tex 的 \\includegraphics 可直接引用 PDF）

灰度依赖图跳过：mkbitmap 含高通滤波，平坦灰块（噪声容限阴影带、层次图高亮行）会被
归白消失；照片/彩图二值化即毁（详见 README「矢量/位图双轨」）。用 --skip-list
传名单（每行一个文件名，# 注释、空行忽略；见 detect_photos.py 的筛查流程）。skip
名单里的图若已存在矢量产物会打 WARN——tex 引用 PDF 优先，此时需手动删除矢量产物
才对清单生效。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

# 外部工具（PATH 里没有时用环境变量 MKBITMAP / POTRACE 指定）
MKBITMAP = os.environ.get("MKBITMAP", "mkbitmap")
POTRACE = os.environ.get("POTRACE", "potrace")

# potrace 参数（工程图取向）：斑点过滤 3px、角点阈值 1.0、拟合容差 0.2
POTRACE_ARGS = ["--turdsize", "3", "--alphamax", "1.0",
                "--opttolerance", "0.2", "--longcurve"]


def imread_u(p: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def write_pgm(p: Path, gray: np.ndarray) -> None:
    """手写 PGM(P5)——mkbitmap 只吃 PNM/BMP"""
    h, w = gray.shape[:2]
    with open(p, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        f.write(gray.tobytes())


def run(cmd: list[str], step: str) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"{step} 失败 rc={r.returncode}: {r.stderr[:200]}")
    return r


def vectorize_one(png: Path, tmp: Path, stem: str, fmts: list[str],
                  threshold: str) -> list[Path]:
    """单张 PNG → [输出路径...]。C 程序（mkbitmap/potrace）全程 ASCII 临时名。"""
    img = imread_u(png)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pgm = tmp / f"{stem}.pgm"
    pbm = tmp / f"{stem}.pbm"
    write_pgm(pgm, img)
    run([MKBITMAP, "-f", "4", "-s", "3", "-t", threshold, str(pgm), "-o", str(pbm)],
        "mkbitmap")
    outs = []
    for fmt in fmts:
        tr = tmp / f"{stem}.{fmt}"
        run([POTRACE, str(pbm), "-b", fmt, "-o", str(tr)] + POTRACE_ARGS, "potrace")
        out = png.with_suffix(f".{fmt}")
        shutil.move(str(tr), str(out))  # TEMP 与项目可能跨盘，不能 os.replace
        outs.append(out)
    return outs


def main():
    ap = argparse.ArgumentParser(description="批量矢量化插图（mkbitmap+potrace）")
    ap.add_argument("images_dir", help="含 PNG 插图的目录")
    ap.add_argument("--format", default="pdf", choices=["pdf", "svg", "both"],
                    help="输出格式（默认 pdf，供 LaTeX）")
    ap.add_argument("--threshold", default="0.45",
                    help="mkbitmap 阈值（透印多时调高到 0.5）")
    ap.add_argument("--force", action="store_true", help="已存在也重新生成")
    ap.add_argument("--skip-list", default=None,
                    help="照片清单文本（每行一个文件名，# 注释；这些图跳过矢量化）")
    args = ap.parse_args()

    d = Path(args.images_dir)
    # 中间文件放系统 TEMP（不进 images/，避免污染插图目录）
    tmp = Path(tempfile.gettempdir()) / "vec_tmp"
    tmp.mkdir(exist_ok=True)
    pngs = sorted(d.glob("*.png"))
    if not pngs:
        print(f"未找到 PNG: {d}")
        return
    fmts = ["pdf", "svg"] if args.format == "both" else [args.format]

    skip_set: set[str] = set()
    if args.skip_list:
        for ln in Path(args.skip_list).read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                skip_set.add(Path(ln).stem)

    ok = fail = skip = photo = 0
    for i, png in enumerate(pngs):
        outs = [png.with_suffix(f".{f}") for f in fmts]
        if png.stem in skip_set:
            photo += 1
            stale = [o.name for o in outs if o.exists()]
            if stale:
                print(f"[WARN] {png.name} 在照片清单但已有矢量产物 {stale}——"
                      f"tex 会优先引用它，需删产物才生效")
            continue
        if all(o.exists() for o in outs) and not args.force:
            ok += len(outs)
            skip += 1
            continue
        try:
            made = vectorize_one(png, tmp, f"img{i:03d}", fmts, args.threshold)
            ok += len(made)
            sizes = ", ".join(f"{m.suffix} {m.stat().st_size / 1024:.1f}KB" for m in made)
            print(f"{png.name} → {sizes}")
        except RuntimeError as e:
            fail += 1
            print(f"[FAIL] {png.name}: {e}")

    print(f"\n完成: 产出 {ok}、失败 {fail}"
          + (f"、跳过（已存在）{skip}" if skip else "")
          + (f"、跳过（照片清单）{photo}" if photo else ""))


if __name__ == "__main__":
    main()
