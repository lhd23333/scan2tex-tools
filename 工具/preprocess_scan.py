# -*- coding: utf-8 -*-
"""整书扫描预处理:光栅化 PDF → 增强 → 重建 PDF(供 MinerU 解析)

核心能力:
  1. 上采样(--scale):对低 DPI 扫描放大,让小字更清晰
     (实测 96-150 DPI 的低清扫描收益明显;300 DPI 通常无需)
  2. 背景除法 + 全局 OTSU 二值化(--binarize):去底色/去透印,输出纯净黑白
     复用 调研/baseline_enhance.py 方法 D:大核椭圆闭运算估计纸面背景 → 相除归一化 → OTSU
  3. 重建 PDF:页面物理尺寸不变(与原件版式坐标一致),图像按新分辨率嵌入

用法示例:
  # 抽样实验(处理第 16-25 页),同时输出「原图 | 处理后」对比 PNG 供目检
  python 工具/preprocess_scan.py "样例/扫描件.pdf" ^
      -o 处理输出/扫描件_增强_样页.pdf --pages 16-25 ^
      --scale 2 --binarize --debug-dir 处理输出/对比

  # 整书
  python 工具/preprocess_scan.py "样例/扫描件.pdf" ^
      -o 处理输出/扫描件_增强.pdf --scale 2 --binarize

参数:
  input            输入 PDF 路径
  -o/--output      输出 PDF 路径(父目录自动创建)
  --pages          页码范围 '16-25' / '16,20,25' / 'all'(默认 all)
  --scale          上采样倍数(默认 1,不放大)
  --binarize       启用背景除法+OTSU 二值化(默认关;开启时输出 1bit PNG)
  --dpi            光栅化 DPI(默认 auto:按首页嵌入图像像素/页面尺寸估算原生 DPI)
  --bg-kernel      背景估计闭运算核边长(默认 auto = odd(round(输出DPI/3))≈0.34 英寸)
  --debug-dir      输出抽样页「原图|处理后」对比 PNG 目录(目检用)
  --debug-pages    抽样页码 '50,52,54'(默认取处理页范围前两页)
"""
import argparse
from pathlib import Path

import cv2
import fitz  # pymupdf
import numpy as np


def parse_pages(spec: str, n_pages: int) -> list:
    """'16-25' / '16,20,25' / 'all' → 0-based 页码列表"""
    if not spec or spec.lower() == "all":
        return list(range(n_pages))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n_pages]


def auto_dpi(doc) -> int:
    """按第一张「大图」的嵌入图像像素宽/页面宽估算原生 DPI"""
    for page in doc:
        for img in page.get_images(full=True):
            px_w = img[2]
            if px_w > 800:  # 跳过图章/装饰小图
                return max(1, round(px_w / page.rect.width * 72))
    return 300


def auto_bg_kernel(out_dpi: float) -> int:
    """背景估计核:约 0.34 英寸(101px@300DPI,与 baseline_enhance.py 一致)"""
    k = max(11, round(out_dpi / 3))
    return k + 1 if k % 2 == 0 else k


def to_gray(pix) -> np.ndarray:
    if pix.n >= 3:
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        return cv2.cvtColor(img[:, :, :3], cv2.COLOR_RGB2GRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def process_page(page, dpi: int, scale: int, binarize: bool, bg_k: int) -> np.ndarray:
    gray = to_gray(page.get_pixmap(dpi=dpi))
    if scale > 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    if binarize:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (bg_k, bg_k))
        bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, k)
        norm = cv2.divide(gray, bg, scale=255)
        _, gray = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return gray


def encode_img(gray: np.ndarray, binarize: bool) -> bytes:
    if binarize:
        ok, buf = cv2.imencode(".png", gray, [cv2.IMWRITE_PNG_BILEVEL, 1])
    else:
        ok, buf = cv2.imencode(".jpg", gray, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("图像编码失败")
    return buf.tobytes()


def main():
    ap = argparse.ArgumentParser(description="整书扫描预处理(供 MinerU)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--pages", default="all")
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--binarize", action="store_true")
    ap.add_argument("--dpi", type=int, default=0, help="0=auto")
    ap.add_argument("--bg-kernel", type=int, default=0, help="0=auto")
    ap.add_argument("--debug-dir", default="")
    ap.add_argument("--debug-pages", default="")
    args = ap.parse_args()

    src = fitz.open(args.input)
    n = src.page_count
    pages = parse_pages(args.pages, n)
    dpi = args.dpi or auto_dpi(src)
    out_dpi = dpi * args.scale
    bg_k = args.bg_kernel or auto_bg_kernel(out_dpi)
    print(f"输入: {args.input} ({n} 页)")
    print(f"处理: {len(pages)} 页 | 光栅化 {dpi}DPI ×{args.scale} → {out_dpi}DPI"
          f" | 二值化 {'开' if args.binarize else '关'}"
          f"{f' (背景核 {bg_k})' if args.binarize else ''}")

    out = fitz.open()
    for i, pno in enumerate(pages):
        page = src[pno]
        gray = process_page(page, dpi, args.scale, args.binarize, bg_k)
        data = encode_img(gray, args.binarize)
        np_ = out.new_page(width=page.rect.width, height=page.rect.height)  # 物理尺寸不变
        np_.insert_image(np_.rect, stream=data)
        if (i + 1) % 25 == 0 or i + 1 == len(pages):
            print(f"  {i+1}/{len(pages)} 页")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    out.save(args.output, deflate=True)
    size_mb = Path(args.output).stat().st_size / 1024 / 1024
    print(f"输出: {args.output} ({size_mb:.1f} MB)")

    if args.debug_dir:  # 目检用:「原图 | 处理后」左右拼图
        ddir = Path(args.debug_dir)
        ddir.mkdir(parents=True, exist_ok=True)
        dp = parse_pages(args.debug_pages, n) if args.debug_pages else pages[:2]
        for pno in dp:
            page = src[pno]
            raw = to_gray(page.get_pixmap(dpi=dpi))
            proc = process_page(page, dpi, args.scale, args.binarize, bg_k)
            H = 1200
            rs = lambda x: cv2.resize(x, None, fx=H / x.shape[0], fy=H / x.shape[0],
                                      interpolation=cv2.INTER_AREA)
            sep = np.full((H, 6), 128, np.uint8)
            canvas = np.hstack([rs(raw), sep, rs(proc)])
            p = ddir / f"对比_p{pno+1}.png"
            ok, buf = cv2.imencode(".png", canvas)
            buf.tofile(str(p))
            print(f"  对比图: {p}")

    out.close()
    src.close()


if __name__ == "__main__":
    main()
