#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拍照图 / PDF → 黑白扫描化成品：拍照线一键预处理入口（影印书处理）。

链路（2026-10-08 定选 final3 + 扫描化）:
  图片: 原图 →[方向归一 orient_auto]→[L1: deskew+安全透视/bbox 兜底]→[黑白扫描化]→ <名>_scan.png
  PDF : 拆页 →[方向抽样判定·整本统一]→[逐页 L1]→[逐页黑白扫描化]→ 拼装 → <名>_scan.pdf
仅支持平整页（用户决策）：弯曲输入如实透传，不做弯曲矫正。

PDF 方向策略：整书通常同向，抽样 min(8, 页数) 页做四象限 OCR 投票出一个统一
rot90 次数（无损）；抽样不一致时取多数并告警。避免数百页逐页判定（4×OCR/页）
带来的小时级耗时。局限：假设页间方向一致（同一拍摄姿势的连续拍照书 / 扫描仪
输出）；混合方向的 PDF 请按图片分批处理。

依赖: 调研\\扫描化实验\\.venv（cv2 + rapidocr + pymupdf 全覆盖）。

用法: <该 venv>\\python.exe 工具\\photo2scan.py <图片或 PDF...> --out-dir <目录> [--clean]
"""
import argparse
import ctypes
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
import pymupdf

# CPU 占用限制：cv2/onnxruntime 默认吃满全核致前台卡顿。默认限 4 线程 +
# 低于正常优先级；P2S_THREADS 环境变量覆盖（0=不限）。
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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "调研", "扫描化实验", ".venv", "Scripts", "python.exe")
ORIENT = os.path.join(ROOT, "调研", "矫正实验", "scripts", "orient_auto.py")
GEOM = os.path.join(ROOT, "调研", "矫正实验", "scripts", "geom_traditional.py")
SCANIFY = os.path.join(ROOT, "调研", "扫描化实验", "scripts", "scanify.py")

MAX_EDGE = 2200   # PDF 拆页渲染长边上限（px）：兼顾 MinerU 质量与 rapidocr 耗时
MAX_ZOOM = 2.0    # PDF 拆页渲染最大放大倍率（小页如 72pt 草稿可放大）
PROBE_PAGES = 8   # 方向判定抽样页数（整书同向假设的多数证据）
PDF_DPI = 300     # 拼装页尺寸的 dpi 语义（px -> pt：px*72/300，MinerU 按此还原像素尺度）


def imread_u(p):
    im = cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise IOError("读图失败: " + p)
    return im


def imwrite_u(p, img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise IOError("写图失败: " + p)
    buf.tofile(p)


def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.stderr.write(f"[FAIL] {what}\n  cmd: {' '.join(cmd)}\n"
                         f"  stdout: {(r.stdout or '')[-500:]}\n  stderr: {(r.stderr or '')[-500:]}\n")
        sys.exit(1)
    return r


# ---------- 图片分支 ----------

def process_after_orient(img, out_dir, clean):
    stem = os.path.splitext(os.path.basename(img))[0]
    ext = os.path.splitext(img)[1].lower() or ".jpg"
    orient = os.path.join(out_dir, f"{stem}_orient{ext}")
    if not os.path.exists(orient):
        sys.stderr.write(f"orient 输出缺失: {orient}\n")
        sys.exit(1)
    # 2) L1（deskew + 安全透视/bbox 兜底）
    run([PY, GEOM, orient, out_dir], f"L1 {stem}")
    l1 = os.path.join(out_dir, f"{stem}_orient_L1.jpg")
    if not os.path.exists(l1):
        sys.stderr.write(f"L1 输出缺失: {l1}\n")
        sys.exit(1)
    # 3) 黑白扫描化 + 成品重命名
    run([PY, SCANIFY, l1, "--out-dir", out_dir], f"scan {stem}")
    scan_mid = os.path.join(out_dir, f"{stem}_orient_L1_scan.png")
    final = os.path.join(out_dir, f"{stem}_scan.png")
    if not os.path.exists(scan_mid):
        sys.stderr.write(f"扫描化输出缺失: {scan_mid}\n")
        sys.exit(1)
    os.replace(scan_mid, final)
    if clean:
        for p in (orient, l1, l1[:-4] + ".json"):
            if os.path.exists(p):
                os.remove(p)
    print(f"OK  {img} -> {final}", flush=True)


def process_images(images, out_dir, clean):
    # 方向归一：批量单次调用（rapidocr 模型只加载一次）
    print(f"[orient] {len(images)} 张（四象限 OCR，批量）", flush=True)
    run([PY, ORIENT, *images, "--apply", "--out-dir", out_dir], "orient batch")
    for i, img in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {os.path.basename(img)}", flush=True)
        process_after_orient(img, out_dir, clean)


# ---------- PDF 分支 ----------

def probe_uniform_rotation(page_paths, pages_dir):
    """抽样页四象限 OCR 投票，返回整本统一 rot90 次数（保守: 失败/无证据返回 0）。"""
    n = len(page_paths)
    m = min(PROBE_PAGES, n)
    if m == 1:
        idxs = [0]
    else:
        idxs = sorted({round(i * (n - 1) / (m - 1)) for i in range(m)})
    sample = [page_paths[i] for i in idxs]
    probe_dir = os.path.join(pages_dir, "_probe")
    os.makedirs(probe_dir, exist_ok=True)
    print(f"[probe] 方向抽样 {len(sample)}/{n} 页（四象限 OCR）", flush=True)
    run([PY, ORIENT, *sample, "--apply", "--out-dir", probe_dir], "orient probe")
    ks = []
    for p in sample:
        stem = os.path.splitext(os.path.basename(p))[0]
        op = os.path.join(probe_dir, f"{stem}_orient.png")
        if not os.path.exists(op):
            continue
        src, dst = imread_u(p), imread_u(op)
        # 容差比对：orient_auto 对 .jpg 路径用 JPEG q95 编码，正确旋转的
        # 平均差实测 ~0.5-0.8 灰阶（量化噪声），错误旋转 ≥30，阈值 5 安全居中
        best_k, best_diff = None, None
        for k in range(4):
            r = np.rot90(src, k)
            if r.shape != dst.shape:
                continue
            d = float(np.mean(np.abs(r.astype(np.int16) - dst.astype(np.int16))))
            if best_diff is None or d < best_diff:
                best_k, best_diff = k, d
        if best_k is not None and best_diff < 5.0:
            ks.append(best_k)
        else:
            print(f"[probe] 警告：{os.path.basename(p)} 旋转关系无法确认"
                  f"（best diff={best_diff}）", flush=True)
    if not ks:
        print("[probe] 未能判定方向，按不旋转处理（k=0）", flush=True)
        return 0
    vals, counts = np.unique(ks, return_counts=True)
    k = int(vals[counts.argmax()])
    if len(vals) > 1:
        dist = {int(v): int(c) for v, c in zip(vals, counts)}
        print(f"[probe] 警告：抽样方向不一致 {dist}，取多数 k={k}", flush=True)
    return k


def process_pdf(pdf, out_dir, clean):
    stem = os.path.splitext(os.path.basename(pdf))[0]
    pages_dir = os.path.join(out_dir, f"{stem}_pages")
    os.makedirs(pages_dir, exist_ok=True)
    # 1) 拆页（长边 MAX_EDGE 内，小页最多放大 MAX_ZOOM）
    doc = pymupdf.open(pdf)
    n = len(doc)
    print(f"[pdf] 拆页 {n} 页", flush=True)
    page_paths = []
    for i, page in enumerate(doc):
        pw, ph = page.rect.width, page.rect.height
        zoom = min(MAX_ZOOM, MAX_EDGE / max(pw, ph))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        p = os.path.join(pages_dir, f"p{i + 1:04d}.png")
        pix.save(p)
        page_paths.append(p)
    doc.close()
    # 2) 整本统一方向（无损 rot90，就地写回）
    k = probe_uniform_rotation(page_paths, pages_dir)
    print(f"[probe] 整本统一旋转 k={k}", flush=True)
    if k:
        for p in page_paths:
            imwrite_u(p, np.ascontiguousarray(np.rot90(imread_u(p), k)))
    # 3) 逐页 L1 + 黑白扫描化
    for i, p in enumerate(page_paths, 1):
        print(f"[{i}/{n}] {os.path.basename(p)}", flush=True)
        run([PY, GEOM, p, pages_dir], f"L1 {os.path.basename(p)}")
        l1 = p[:-4] + "_L1.jpg"
        if not os.path.exists(l1):
            sys.stderr.write(f"L1 输出缺失: {l1}\n")
            sys.exit(1)
        run([PY, SCANIFY, l1, "--out-dir", pages_dir], f"scan {os.path.basename(p)}")
        scan_mid = p[:-4] + "_L1_scan.png"
        if not os.path.exists(scan_mid):
            sys.stderr.write(f"扫描化输出缺失: {scan_mid}\n")
            sys.exit(1)
        os.replace(scan_mid, p[:-4] + "_scan.png")
    # 4) 拼装扫描化 PDF（页尺寸按 300dpi 语义折算 pt，MinerU 按此还原像素尺度）
    print("[pdf] 拼装扫描化 PDF", flush=True)
    out = pymupdf.open()
    for p in page_paths:
        sp = p[:-4] + "_scan.png"
        pix = pymupdf.Pixmap(sp)
        w_pt, h_pt = pix.width * 72 / PDF_DPI, pix.height * 72 / PDF_DPI
        pg = out.new_page(width=w_pt, height=h_pt)
        pg.insert_image(pymupdf.Rect(0, 0, w_pt, h_pt), filename=sp)
    dst = os.path.join(out_dir, f"{stem}_scan.pdf")
    out.save(dst)
    out.close()
    if clean:
        shutil.rmtree(pages_dir, ignore_errors=True)
    print(f"OK  {pdf} -> {dst}", flush=True)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="拍照图/PDF → 黑白扫描化成品（final3 链路）")
    ap.add_argument("inputs", nargs="+", help="输入图片或 PDF 路径")
    ap.add_argument("--out-dir", required=True, help="输出目录（成品与中间产物）")
    ap.add_argument("--clean", action="store_true",
                    help="完成后删除中间产物（图片: _orient/_L1；PDF: <名>_pages/ 目录）")
    a = ap.parse_args()
    limit_cpu()
    os.makedirs(a.out_dir, exist_ok=True)
    images = [p for p in a.inputs if os.path.splitext(p)[1].lower() in (".jpg", ".jpeg", ".png")]
    pdfs = [p for p in a.inputs if os.path.splitext(p)[1].lower() == ".pdf"]
    unknown = [p for p in a.inputs if p not in images and p not in pdfs]
    if unknown:
        sys.stderr.write("不支持的输入类型: " + ", ".join(unknown) + "（支持 jpg/jpeg/png/pdf）\n")
        sys.exit(1)
    if images:
        process_images(images, a.out_dir, a.clean)
    for pdf in pdfs:
        process_pdf(pdf, a.out_dir, a.clean)


if __name__ == "__main__":
    main()
