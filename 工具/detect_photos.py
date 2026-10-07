# -*- coding: utf-8 -*-
"""照片筛查：从插图目录挑出"连续色调照片"候选，供目检后跳过矢量化。

背景：mkbitmap+potrace 二值化会毁掉照片（连续灰阶细节丢失），但纯扫描线稿
（含灰色填充块的框图，如噪声容限图）可安全矢量化。实测结论：单一数值特征
无法可靠区分二者——高对比老照片（如图1.3 差分机）的 Otsu 类间方差占比 η
(0.892) 与线稿（0.80~0.90）重叠。故本工具"宽进"初筛（宁多框勿漏），产出：

  candidates.txt      候选清单（编号 mid eta 文件名，按 mid 降序）
  预览_照片筛查_NN.png  64 格缩略图拼贴（黑底白字编号）——目检对照清单勾真照片

随后把确认的照片逐行写进 photos_skip.txt，交给
vectorize_figures.py --skip-list 跳过。

用法:
  python 工具/detect_photos.py <images目录> [--mid 0.12] [--eta 0.88]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import cv2
import numpy as np

CELL = 150   # 缩略图格子边长（像素）
COLS = 8


def imread_u(p: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def features(img: np.ndarray) -> tuple[float, float]:
    """(中间调占比, Otsu 类间方差占比 η)。

    mid：灰度 [64,192] 占比（连续色调的直接信号；灰填充块会抬高它，故只作初筛）
    eta：1.0 = 黑白离散双峰（线稿典型），越低越接近连续分布
    """
    hist = cv2.calcHist([img], [0], None, [256], [0, 256]).ravel()
    total = img.size
    mid = float(hist[64:192].sum() / total)
    pn = hist / hist.sum()
    omega = np.cumsum(pn)
    mu = np.cumsum(pn * np.arange(256))
    mu_t = mu[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b2 = (mu_t * omega - mu) ** 2 / (omega * (1 - omega))
    sigma_b2 = np.nan_to_num(sigma_b2)
    sigma_t2 = float(((np.arange(256) - mu_t) ** 2 * pn).sum())
    eta = float(sigma_b2.max() / sigma_t2) if sigma_t2 > 0 else 1.0
    return mid, eta


def make_montages(items: list[tuple[int, str, float, float, Path]],
                  out_dir: Path, per_page: int) -> list[Path]:
    """缩略图拼贴（编号在格左上角）。cv2 画不了中文文件名 → 用编号对照清单。"""
    pages = []
    for start in range(0, len(items), per_page):
        chunk = items[start:start + per_page]
        rows = math.ceil(len(chunk) / COLS)
        canvas = np.full((rows * CELL, COLS * CELL, 3), 255, np.uint8)
        for i, (n, _name, _mid, _eta, path) in enumerate(chunk):
            img = imread_u(path)
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            h, w = img.shape[:2]
            s = (CELL - 8) / max(h, w)
            nh, nw = max(1, int(h * s)), max(1, int(w * s))
            small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            r, c = divmod(i, COLS)
            y0, x0 = r * CELL + (CELL - nh) // 2, c * CELL + (CELL - nw) // 2
            canvas[y0:y0 + nh, x0:x0 + nw] = small
            label = str(n)
            cv2.rectangle(canvas, (c * CELL, r * CELL),
                          (c * CELL + 12 + 10 * len(label), r * CELL + 18), (0, 0, 0), -1)
            cv2.putText(canvas, label, (c * CELL + 4, r * CELL + 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
        out = out_dir / f"预览_照片筛查_{len(pages) + 1:02d}.png"
        ok, buf = cv2.imencode(".png", canvas)  # 中文路径：imwrite 会静默失败
        if not ok:
            raise RuntimeError(f"imencode 失败: {out}")
        buf.tofile(str(out))
        pages.append(out)
    return pages


def main():
    ap = argparse.ArgumentParser(description="照片筛查（初筛 + 缩略图拼贴）")
    ap.add_argument("images_dir", help="含 PNG 插图的目录")
    ap.add_argument("--mid", type=float, default=0.12,
                    help="中间调占比阈值（超过则入候选，默认 0.12）")
    ap.add_argument("--eta", type=float, default=0.88,
                    help="Otsu η 阈值（低于则入候选，默认 0.88）")
    ap.add_argument("--per-page", type=int, default=64, help="每页拼贴格数（默认 64）")
    ap.add_argument("--out", default=None, help="输出目录（默认 images 的上一级）")
    args = ap.parse_args()

    d = Path(args.images_dir)
    out_dir = Path(args.out) if args.out else d.parent
    pngs = sorted(d.glob("*.png"))
    if not pngs:
        print(f"未找到 PNG: {d}")
        return

    candidates = []
    for p in pngs:
        img = imread_u(p)
        if img is None:
            print(f"[WARN] 读取失败: {p.name}")
            continue
        if img.ndim == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mid, eta = features(img)
        if mid > args.mid or eta < args.eta:
            candidates.append((mid, eta, p))

    candidates.sort(key=lambda t: -t[0])  # mid 降序：最可疑的排前面

    lines = ["# 照片筛查候选清单（detect_photos.py 生成）",
             f"# 目录: {d}",
             f"# 初筛: mid>{args.mid} 或 eta<{args.eta} | 候选 {len(candidates)}/{len(pngs)}",
             "# 编号  mid    eta    文件名"]
    items = []
    for n, (mid, eta, p) in enumerate(candidates, 1):
        items.append((n, p.name, mid, eta, p))
        lines.append(f"{n:>4}  {mid:.3f}  {eta:.3f}  {p.name}")
    (out_dir / "candidates.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    pages = make_montages(items, out_dir, args.per_page) if items else []
    print(f"候选 {len(candidates)}/{len(pngs)} 张 → {out_dir / 'candidates.txt'}")
    for pg in pages:
        print(f"  拼贴: {pg.name}")
    print("目检拼贴对照清单勾出真照片 → 写 photos_skip.txt → vectorize_figures.py --skip-list")


if __name__ == "__main__":
    main()
