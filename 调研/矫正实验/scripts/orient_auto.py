#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""方向归一（四象限 + 180°，无损旋转版）：cls-off OCR 穷举选向

背景：修复轮发现 L1 的四边形透视裁剪在 gk03 上退化（假四边形切掉页面底部
表格），而方向判定本身正确。故把"方向归一"从 L1 中解耦为独立无损步骤：
对 4 个象限旋转各做一次 cls-off OCR，取可读字符数最高者（其 180° 版必然
更低，无需另测）。象限+180 一步到位，全程 np.rot90 无损。

注意局限：页面文字过少（最优方向 <50 字）时判 uncertain，默认不旋转。
用法（需 rapidocr 解释器）:
  python orient_auto.py <图片...> [--apply --out-dir <目录>] [--report]
"""
import argparse
import ctypes
import json
import os
import shutil
import sys

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

# CPU 占用限制（同 photo2scan）：cv2/onnxruntime 默认吃满全核致前台卡顿。
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
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite_u(path, img, quality=95):
    # 按扩展名选择编码：.png 走无损（修复历史缺陷——原实现硬编码 .jpg 编码，
    # 给 .png 路径写 JPEG 内容，与"全程 np.rot90 无损"的声明不符）；
    # 其余扩展名走 JPEG q95（历史行为，图片链依赖不变）
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        ok, buf = cv2.imencode(".png", img)
    else:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError("imencode failed: " + path)
    buf.tofile(path)


def ocr_chars(engine, img):
    res, _ = engine(img)
    if not res:
        return 0
    return sum(len(r[1]) for r in res)


def pick_orientation(engine, img, min_evidence=50):
    """返回 (k, scores)；k=np.rot90 次数 0-3，scores=[k0,k90,k180,k270]

    实测（2026-10-08 图解确认）：rapidocr 对某一侧 90° 旋转页仍能读出真实文本
    （检测/裁切会把高瘦文本框转正），反方向分数约为正方向的 0.77-0.88×；
    另一侧 90° 与 180° 则近零。故决策取 argmax 仍稳定（6/6 与已知方向一致），
    但边距可能薄（实测 1.14-1.23×），main() 里另记 confident 标记供人工复核。
    """
    scores = []
    for k in range(4):
        im = np.ascontiguousarray(np.rot90(img, k)) if k else img
        scores.append(ocr_chars(engine, im))
    best_k = int(np.argmax(scores))
    if scores[best_k] < min_evidence:
        return None, scores
    return best_k, scores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    limit_cpu()
    ocr_kw = ({"intra_op_num_threads": THREADS, "inter_op_num_threads": 1}
              if THREADS > 0 else {})
    engine = RapidOCR(use_cls=False, **ocr_kw)
    for p in args.inputs:
        img = imread_u(p)
        if img is None:
            print(json.dumps({"input": p, "error": "read failed"}, ensure_ascii=False))
            continue
        k, scores = pick_orientation(engine, img)
        top2 = sorted(scores, reverse=True)[:2]
        # 领先次优 <1.15× 说明边距薄（反方向 90° 基线高），标记低置信
        confident = k is not None and top2[0] >= 1.15 * top2[1]
        info = {"input": os.path.basename(p),
                "scores_0_90_180_270": scores,
                "decision": "uncertain" if k is None else f"rot90*{k}",
                "confident": bool(confident)}
        if args.apply and k is not None:
            stem = os.path.splitext(os.path.basename(p))[0]
            ext = os.path.splitext(p)[1] or ".jpg"
            op = os.path.join(args.out_dir, f"{stem}_orient{ext}")
            if k == 0:
                shutil.copyfile(p, op)  # 位保真，避免无谓重编码
            else:
                imwrite_u(op, np.ascontiguousarray(np.rot90(img, k)))
            info["output"] = op
        if not args.report:
            info.pop("scores_0_90_180_270", None)
        print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
