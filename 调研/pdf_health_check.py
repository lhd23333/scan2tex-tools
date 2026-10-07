# -*- coding: utf-8 -*-
"""PDF 技术体检：判断扫描型/文字型、页面尺寸、图像分辨率、文本层情况。

用法: python pdf_health_check.py
输出: 控制台摘要 + health_report.json (同目录)
"""
import json
import os
import pymupdf

# 目录约定：目标/（质量标杆）、样例/（待处理 PDF）位于脚本上一级目录（项目根）
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = {
    "目标": os.path.join(BASE, "目标"),
    "样例": os.path.join(BASE, "样例"),
}


def sample_indices(n):
    """采样的页下标：前几页 + 中间 + 靠后，避开封面。"""
    idx = [min(n - 1, i) for i in (15, 16, 17, 50, 51, n // 2, n // 2 + 1)]
    seen, out = set(), []
    for i in idx:
        if i not in seen and i < n:
            seen.add(i)
            out.append(i)
    return out


def check(pdf_path):
    doc = pymupdf.open(pdf_path)
    n = len(doc)
    info = {
        "file": os.path.basename(pdf_path),
        "pages": n,
        "is_encrypted": doc.is_encrypted,
        "has_toc": bool(doc.get_toc()),
    }
    if n == 0:
        doc.close()
        return info

    idxs = sample_indices(n)

    # 文本层：采样页的平均字符数（含空白）
    chars = [len(doc[i].get_text().strip()) for i in idxs]
    info["chars_per_page_avg"] = round(sum(chars) / len(chars), 1)
    info["chars_per_page_samples"] = chars

    # 页面尺寸（第一张采样页，单位 pt，1pt=1/72 inch）
    r = doc[idxs[0]].rect
    info["page_size_pt"] = [round(r.width, 1), round(r.height, 1)]
    info["page_size_mm"] = [round(r.width / 72 * 25.4, 1), round(r.height / 72 * 25.4, 1)]

    # 图像对象：每采样页的图像数 + 最大图像的像素尺寸与估算 DPI
    img_rows = []
    for i in idxs[:4]:
        page = doc[i]
        imgs = page.get_images(full=True)
        row = {"page": i + 1, "n_images": len(imgs)}
        if imgs:
            # 取面积最大的图
            big = max(imgs, key=lambda im: im[2] * im[3])
            w, h = big[2], big[3]
            dpi_x = w / (page.rect.width / 72)
            dpi_y = h / (page.rect.height / 72)
            # 取整数页图像且满幅时的 DPI 更有意义
            row.update({
                "max_img_px": [w, h],
                "max_img_bpc": big[4] if len(big) > 4 else None,
                "max_img_cs": big[5] if len(big) > 5 else None,
                "approx_dpi": [round(dpi_x), round(dpi_y)],
            })
        img_rows.append(row)
    info["image_info"] = img_rows

    # 页面文本块 vs 图像混合情况：抽样判断是否纯扫描
    mixed = []
    for i in idxs[:4]:
        page = doc[i]
        mixed.append({
            "page": i + 1,
            "n_textblocks": len(page.get_text("blocks")),
            "has_images": len(page.get_images(full=True)) > 0,
        })
    info["text_vs_image"] = mixed

    doc.close()
    return info


def main():
    report = {}
    for group, d in TARGETS.items():
        report[group] = []
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(d, fn)
            try:
                report[group].append(check(path))
            except Exception as e:  # noqa: BLE001
                report[group].append({"file": fn, "error": str(e)})

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "health_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 控制台摘要
    for group, items in report.items():
        print(f"\n===== {group} =====")
        for it in items:
            if "error" in it:
                print(f"[ERR] {it['file']}: {it['error']}")
                continue
            name = it["file"][:40]
            img = it["image_info"][0] if it.get("image_info") else {}
            print(
                f"{name} | {it['pages']}页 | 页均字符 {it['chars_per_page_avg']} | "
                f"尺寸 {it['page_size_mm']}mm | 图 {img.get('max_img_px')}px @~{img.get('approx_dpi')}dpi "
                f"cs={img.get('max_img_cs')} bpc={img.get('max_img_bpc')}"
            )
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main()
