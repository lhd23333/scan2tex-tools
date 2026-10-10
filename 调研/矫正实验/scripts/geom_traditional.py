#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""传统几何矫正链 L1 v2（零深度学习依赖：opencv + numpy）

v2 修正：方向检测改在"透视归一化后的页面"上做（v1 在全图上被桌面/手写/插图干扰，大面积误判）。
流水线:
  1. 页面亮区四边形检测 -> 透视归一化（失败则亮区 bbox 轻裁兜底）
  2. 归一化页面上：双假设(0°/90°)投影角搜索 -> 行方向判定 + 倾斜角
  3. np.rot90 象限旋转（无损）+ 残余小角度插值旋转

用法:
  python geom_traditional.py <输入图> <输出目录> [--debug] [--max-dim 1600]

输出:
  <输出目录>/<stem>_L1.jpg          矫正结果
  <输出目录>/<stem>_L1.json         检测参数（角度/四边形/耗时）
  <输出目录>/debug/<stem>_L1_*.jpg  调试图（--debug）
"""
import argparse
import ctypes
import json
import os
import sys
import time

import cv2
import numpy as np

BORDER = cv2.BORDER_REPLICATE

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


def imread_u(path):
    """支持中文路径的读图"""
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite_u(path, img, quality=95):
    """支持中文路径的写图（cv2.imwrite 在中文路径下静默失败）"""
    ext = os.path.splitext(path)[1].lower() or ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext in (".jpg", ".jpeg") else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise IOError("imencode failed: " + path)
    buf.tofile(path)


def text_mask(gray):
    """文字为白(255)的掩码"""
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def bin_mask(page, area_frac_max=0.005, min_area=3):
    """文字掩码并过滤超大块（插图/黑边）与小噪点，供方向分析用"""
    g = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
    m = text_mask(g)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m)
    a_page = page.shape[0] * page.shape[1]
    keep = np.zeros_like(m)
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if min_area <= a <= area_frac_max * a_page:
            keep[lab == i] = 255
    return keep


def order_quad(pts):
    """四角排序: 左上/右上/右下/左下"""
    pts = np.asarray(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def find_page_quad(img):
    """检测页面亮区四边形。返回 (quad 4x2 float32 或 None, area_ratio)"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (7, 7), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, 0.0
    c = max(cnts, key=cv2.contourArea)
    area_ratio = cv2.contourArea(c) / float(img.shape[0] * img.shape[1])
    if area_ratio < 0.20:
        return None, area_ratio
    peri = cv2.arcLength(c, True)
    for eps in (0.02, 0.03, 0.05):
        approx = cv2.approxPolyDP(c, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2).astype(np.float32), area_ratio
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    approx = cv2.approxPolyDP(hull, 0.05 * peri, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        return approx.reshape(4, 2).astype(np.float32), area_ratio
    return None, area_ratio


def quad_is_sane(quad):
    """拒绝病态四边形：对边长度比失衡（>2 倍）说明检测到的是外框/斜跨畸形区域，
    强行透视会引入严重剪切（en01 实例：假四边形被拉直产生 27° 伪旋转）。"""
    tl, tr, br, bl = order_quad(quad)
    top = np.linalg.norm(tr - tl)
    bottom = np.linalg.norm(br - bl)
    left = np.linalg.norm(bl - tl)
    right = np.linalg.norm(br - tr)
    ratio_h = max(top, bottom) / max(1e-6, min(top, bottom))
    ratio_v = max(left, right) / max(1e-6, min(left, right))
    return ratio_h <= 2.0 and ratio_v <= 2.0


def quad_to_rect(img, quad):
    """四点透视矫正, 输出尺寸按对边最大长度估计"""
    tl, tr, br, bl = order_quad(quad)
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    w, h = int(round(w)), int(round(h))
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype=np.float32), dst)
    return cv2.warpPerspective(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=BORDER)


def crop_page_bbox(img, margin=0.005):
    """按页面亮区最大轮廓 bbox 轻裁（透视失败时的兜底去桌面边）"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (7, 7), 0)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31))
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, k)
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return img
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) / float(img.shape[0] * img.shape[1]) < 0.20:
        return img
    x, y, w, h = cv2.boundingRect(c)
    H, W = img.shape[:2]
    mx, my = int(w * margin), int(h * margin)
    return img[max(0, y - my):min(H, y + h + my), max(0, x - mx):min(W, x + w + mx)]


def score_angle(mask, angle):
    """旋转 angle 后的行投影 std（越大说明文字行越贴合水平）"""
    h, w = mask.shape
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    rot = cv2.warpAffine(mask, m, (w, h), flags=cv2.INTER_NEAREST,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return float(rot.sum(axis=1, dtype=np.float64).std())


def find_best_angle(mask, center, span=35.0, step=0.5):
    best_a, best_s = center, -1.0
    for a in np.arange(center - span, center + span + 1e-9, step):
        s = score_angle(mask, float(a))
        if s > best_s:
            best_a, best_s = float(a), s
    return best_a, best_s


def refine_angle(mask, angle, span=2.0, step=0.1):
    best_a, best_s = angle, score_angle(mask, angle)
    for a in np.arange(angle - span, angle + span + 1e-9, step):
        s = score_angle(mask, float(a))
        if s > best_s:
            best_a, best_s = float(a), s
    return best_a, best_s


def process(path, outdir, max_dim=1600, debug=False):
    t0 = time.time()
    info = {"input": os.path.basename(path)}
    img = imread_u(path)
    if img is None:
        raise IOError("cannot read: " + path)
    H, W = img.shape[:2]
    info["input_size"] = [W, H]
    stem = os.path.splitext(os.path.basename(path))[0]
    dbg_dir = os.path.join(outdir, "debug")
    if debug:
        os.makedirs(dbg_dir, exist_ok=True)

    def dbg_save(tag, im):
        if debug:
            imwrite_u(os.path.join(dbg_dir, f"{stem}_L1_{tag}.jpg"), im, 90)

    # ---- 1. 页面四边形透视归一化 ----
    sc = min(1.0, max_dim / float(max(H, W)))
    small = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
    quad_s, area_ratio = find_page_quad(small)
    info["quad_area_ratio"] = round(float(area_ratio), 3)
    if quad_s is not None and not quad_is_sane(quad_s):
        info["quad_rejected"] = "edge-ratio>2"
        quad_s = None
    if quad_s is not None:
        if debug:
            vis = small.copy()
            cv2.polylines(vis, [order_quad(quad_s).astype(np.int32)], True, (0, 0, 255), 4)
            dbg_save("quad", vis)
        quad = quad_s / sc
        info["quad"] = [[round(float(x), 1), round(float(y), 1)] for x, y in order_quad(quad)]
        page = quad_to_rect(img, quad)
        info["mode"] = "perspective"
    else:
        page = crop_page_bbox(img)
        info["mode"] = "bbox-crop"

    # ---- 2. 方向判定 + 倾斜角（在归一化页面上，双假设搜索） ----
    sc2 = min(1.0, 900.0 / float(max(page.shape[:2])))
    pg = cv2.resize(page, None, fx=sc2, fy=sc2, interpolation=cv2.INTER_AREA)
    m = bin_mask(pg)
    a0, s0 = find_best_angle(m, 0.0)
    a90, s90 = find_best_angle(m, 90.0)
    a0, s0 = refine_angle(m, a0)
    a90, s90 = refine_angle(m, a90)
    if s90 > s0:
        theta, quad_turns, rem = a90, 1, a90 - 90.0
    else:
        theta, quad_turns, rem = a0, 0, a0
    info["theta_deg"] = round(theta, 2)
    info["score_0"] = round(s0, 1)
    info["score_90"] = round(s90, 1)
    info["low_confidence"] = bool(max(s0, s90) < 1.10 * min(s0, s90))

    # ---- 3. 应用：象限旋转（无损）+ 残余小角度 ----
    out = np.ascontiguousarray(np.rot90(page, quad_turns)) if quad_turns else page
    if abs(rem) > 0.15:
        h, w = out.shape[:2]
        m_rot = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), rem, 1.0)
        out = cv2.warpAffine(out, m_rot, (w, h), flags=cv2.INTER_CUBIC, borderMode=BORDER)

    imwrite_u(os.path.join(outdir, f"{stem}_L1.jpg"), out, 95)
    dbg_save("final", out if max(out.shape[:2]) < 1400 else cv2.resize(out, None, fx=0.6, fy=0.6))
    info["output_size"] = [out.shape[1], out.shape[0]]
    info["elapsed_s"] = round(time.time() - t0, 2)
    with open(os.path.join(outdir, f"{stem}_L1.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(json.dumps(info, ensure_ascii=False))
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("outdir")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--max-dim", type=int, default=1600)
    args = ap.parse_args()
    limit_cpu()
    os.makedirs(args.outdir, exist_ok=True)
    process(args.input, args.outdir, args.max_dim, args.debug)


if __name__ == "__main__":
    main()
