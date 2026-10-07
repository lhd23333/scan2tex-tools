# -*- coding: utf-8 -*-
"""检查 PDF 内嵌图像的原始分辨率与有效 DPI。

用途：判断 MinerU 导出的插图（按页渲染裁剪）相比 PDF 内嵌原图
是否损失了分辨率——若原图明显更大，应直接从 PDF 提取原图。

用法: python 调研/pdf_embed_check.py <书名关键字> <页码...>
例:   python 调研/pdf_embed_check.py 数字设计 99 100
"""
import glob
import sys

import pymupdf

kw = sys.argv[1] if len(sys.argv) > 1 else "数字设计"
pages = [int(p) - 1 for p in sys.argv[2:]] or [98, 99]

_hits = glob.glob(f"样例/*{kw}*.pdf")
if not _hits:
    sys.exit(f"未找到 样例/*{kw}*.pdf —— 请把目标 PDF 放入 样例/ 目录")
pdf_path = _hits[0]
print(f"PDF: {pdf_path}")
doc = pymupdf.open(pdf_path)
for pno in pages:
    page = doc[pno]
    print(f"--- PDF 第 {pno + 1} 页 ({page.rect.width:.0f}x{page.rect.height:.0f} pt) ---")
    for img in page.get_images(full=True):
        xref = img[0]
        info = doc.extract_image(xref)
        rects = page.get_image_rects(xref)
        for r in rects[:1]:
            dpi = info["width"] / (r.width / 72) if r.width else 0
            print(f"  xref={xref} 原图 {info['width']}x{info['height']}px "
                  f"{info['ext']} 显示 {r.width:.0f}x{r.height:.0f}pt -> 有效 {dpi:.0f} DPI")
