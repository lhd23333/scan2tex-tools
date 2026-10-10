#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""黑白扫描化单图 CLI（定选方案固化：背景除法 + 全局 OTSU）。

定选结论见 `实验结论.md`：背景除法（椭圆核闭运算 k=51）+ 全局 OTSU，
零新依赖 / ~0.1 s/张 / MinerU 层面与备选等效。
实现与 `scan2_algos.bgdiv_otsu` 一致（此处内联以便独立使用）。

用法: .venv\\Scripts\\python.exe scripts\\scanify.py <输入图...> [--out-dir <目录>] [--k 51]
输出: <out-dir>/<stem>_scan.png（二值 PNG；out-dir 缺省为各输入图所在目录）
"""
import argparse
import ctypes
import os
import sys

import cv2
import numpy as np

# CPU 占用限制（同 photo2scan）：cv2 默认吃满全核致前台卡顿。
# 默认限 4 线程 + 低于正常优先级；P2S_THREADS 环境变量覆盖（0=不限）。
THREADS = int(os.environ.get("P2S_THREADS") or "4")


def limit_cpu():
    if THREADS <= 0:
        return
    os.environ["OMP_NUM_THREADS"] = str(THREADS)
    cv2.setNumThreads(THREADS)
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        except Exception:
            pass


def imread_gray(p):
    im = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise IOError("读图失败: " + p)
    return im


def imwrite_u(p, img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise IOError("写图失败: " + p)
    buf.tofile(p)


def bgdiv_otsu(gray, k=51):
    """背景除法 + 全局 OTSU：闭运算估计背景 → 归一 → OTSU 二值。"""
    kk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kk)
    norm = cv2.divide(gray, bg, scale=255)
    _, out = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return out


def main():
    ap = argparse.ArgumentParser(description="黑白扫描化（背景除法+OTSU）")
    ap.add_argument("images", nargs="+", help="输入图片路径")
    ap.add_argument("--out-dir", default=None, help="输出目录（缺省=输入图所在目录）")
    ap.add_argument("--k", type=int, default=51, help="背景估计核尺寸（默认 51）")
    a = ap.parse_args()
    limit_cpu()
    for p in a.images:
        gray = imread_gray(p)
        out = bgdiv_otsu(gray, a.k)
        stem = os.path.splitext(os.path.basename(p))[0]
        od = a.out_dir or os.path.dirname(os.path.abspath(p))
        os.makedirs(od, exist_ok=True)
        dst = os.path.join(od, stem + "_scan.png")
        imwrite_u(dst, out)
        print(f"{p} -> {dst} ({out.shape[1]}x{out.shape[0]})")


if __name__ == "__main__":
    main()
