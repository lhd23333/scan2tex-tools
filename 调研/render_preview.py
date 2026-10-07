# -*- coding: utf-8 -*-
"""渲染各 PDF 代表性页面为 PNG（150 DPI），并抽取文字层样本。

用法: python render_preview.py
输出: preview/ 目录下 PNG + text_samples.txt
"""
import os
import pymupdf

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 脚本上一级（项目根）
OUT = os.path.join(BASE, "调研", "preview")
os.makedirs(OUT, exist_ok=True)

# (短名, 目录, 页码关键字, 0-based页下标)
JOBS = [
    ("目标_复分析", "目标", "复分析", 99),
    ("样例_微电子学", "样例", "微电子学基础", 99),
    ("样例_数字设计RISC-V", "样例", "数字设计和计算机体系结构", 99),
    ("样例_模拟数字电路", "样例", "模拟和数字电子电路基础", 99),
    ("样例_算法导论", "样例", "算法导论", 99),
    ("样例_算法概论", "样例", "算法概论", 99),
]

text_out = []
for short, group, kw, page_no in JOBS:
    d = os.path.join(BASE, group)
    cands = [f for f in os.listdir(d) if f.lower().endswith(".pdf") and kw in f]
    if not cands:
        print(f"[SKIP] {short}: 未找到匹配 {kw}")
        continue
    path = os.path.join(d, cands[0])
    doc = pymupdf.open(path)
    pno = min(page_no, len(doc) - 1)
    page = doc[pno]
    pix = page.get_pixmap(dpi=150)
    png = os.path.join(OUT, f"{short}.png")
    pix.save(png)

    txt = page.get_text().strip()
    text_out.append(f"\n{'='*60}\n【{short}】第 {pno+1} 页, 字符数={len(txt)}\n{'='*60}\n{txt[:400]}")
    print(f"{short}: 渲染第 {pno+1} 页 -> {png}  ({pix.width}x{pix.height}, 文本层 {len(txt)} 字符)")
    doc.close()

with open(os.path.join(OUT, "text_samples.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(text_out))
print("\n文本样本 -> preview/text_samples.txt")
