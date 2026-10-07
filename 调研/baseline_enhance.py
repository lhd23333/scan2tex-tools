# -*- coding: utf-8 -*-
"""基线增强实验：验证传统 OpenCV 路线（对比度拉伸 / 自适应二值化）的实际效果。

对两本最差的扫描教材各取一页（300 DPI 渲染）：
  A. CLAHE 对比度增强（保留灰度）
  B. 自适应二值化 - Gemini 脚本原参数 (blockSize=21, C=10)
  C. 自适应二值化 - 调参版 (先去噪, blockSize=61, C=15)

用法: python baseline_enhance.py
输出: baseline/ 目录
"""
import os
import cv2
import numpy as np
import pymupdf

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 脚本上一级（项目根）
OUT = os.path.join(BASE, "调研", "baseline")
os.makedirs(OUT, exist_ok=True)

JOBS = [
    ("算法概论", "样例", "算法概论", 99),
    ("微电子学", "样例", "微电子学基础", 99),
]

TARGET_W = 1200  # 输出图宽度

for short, group, kw, pno in JOBS:
    d = os.path.join(BASE, group)
    cands = [f for f in os.listdir(d) if f.lower().endswith(".pdf") and kw in f]
    if not cands:
        print(f"[SKIP] {short}")
        continue
    doc = pymupdf.open(os.path.join(d, cands[0]))
    page = doc[min(pno, len(doc) - 1)]
    pix = page.get_pixmap(dpi=300)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if pix.n >= 3 else img.copy()
    doc.close()

    def save(name, im):
        scale = TARGET_W / im.shape[1]
        small = cv2.resize(im, (TARGET_W, int(im.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        p = os.path.join(OUT, f"{short}_{name}.png")
        # 注意: cv2.imwrite 不支持中文路径(静默失败), 改用 imencode + tofile
        ok, buf = cv2.imencode(".png", small)
        if not ok:
            raise RuntimeError(f"PNG 编码失败: {p}")
        buf.tofile(p)
        print(f"{p}  ({small.shape[1]}x{small.shape[0]})")

    # A. CLAHE 对比度增强（保留灰度，对 OCR 友好）
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    a = clahe.apply(gray)
    a = cv2.bilateralFilter(a, 5, 30, 30)
    save("A_clahe", a)

    # B. Gemini 原参数二值化（先在灰度上做轻度去噪）
    den = cv2.medianBlur(gray, 3)
    b = cv2.adaptiveThreshold(den, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                              cv2.THRESH_BINARY, 21, 10)
    save("B_binary_gemini", b)

    # C. 调参版：更大窗口、更强去噪
    den2 = cv2.fastNlMeansDenoising(gray, h=10)
    c = cv2.adaptiveThreshold(den2, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                              cv2.THRESH_BINARY, 61, 15)
    save("C_binary_tuned", c)

    # D. 背景估计除法 + 全局 OTSU（针对透印/不均匀底色的经典解法）
    #    大核闭运算估计纸面背景, 相除归一化后透印(比正文浅)被压掉, 再全局阈值
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (101, 101))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, k)
    norm = cv2.divide(gray, bg, scale=255)
    _, d = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    save("D_bgdiv_otsu", d)
