# -*- coding: utf-8 -*-
r"""MinerU JSON → ctexbook LaTeX 转换器（tex 交付版）。

与 json_to_readable.py 共用解析内核（公式清洗 / 图注修复 / 标题层级），
输出从 Markdown 换成可编译的 LaTeX 书籍源码：

  标题  → 星号版章节命令（\section* 等）——编号用 MinerU 文本自带的（与原书一致，
          避免 LaTeX 自动编号与原书 3.5.4 体系冲突）+ \addcontentsline 进目录
  公式  → equation* + （文本自带）\tag{3.19}；行内公式 $..$ 原样进数学模式
  图片  → figure 环境 + \includegraphics + 文本图注（不加 \caption 以免重复编号）
  正文  → LaTeX 特殊字符转义（公式部分不转义）
  页码  → % p.N 注释

用法:
  python 工具/json_to_tex.py <result.json> [-o book.tex]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from json_to_readable import (  # noqa: E402
    SKIP_TYPES,
    TEXT_UNICODE_FIX,
    _CAPTION_REF_RE,
    block_text,
    build_chapter_name_map,
    chapter_display_title,
    clean_equation,
    code_text,
    collect_tag_ids,
    cut_repetition_loop,
    figure_info,
    fix_eq_refs,
    infer_missing_captions,
    looks_like_body_title,
    looks_like_caption,
    merge_overlapping_neighbors,
    parse_table_html,
    resolve_chapter_titles,
    set_tag_set,
    title_level,
    unique_image_name,
)

TEX_CMD = {1: "chapter", 2: "section", 3: "subsection", 4: "subsubsection"}

TEX_ESCAPE = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


_URL_RE = re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# 超长 ASCII 连块（无空白的代码串/粘连机器文本），LaTeX 在其中无断点
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%{}]{13,}")

# 目录剔除阈值：同名标题出现 >= _TOC_SKIP_MIN_COUNT 次且 <= _TOC_SKIP_MAX_LEN 字时
# "保留标题样式、不进目录"（06 实测"练习"×132、"思考题"×36——栏目名在目录里
# 复读无导航价值；正文标题样式照旧，只省 addcontentsline）
_TOC_SKIP_MIN_COUNT = 5
_TOC_SKIP_MAX_LEN = 12


def _soften_long_tokens(s: str) -> str:
    r"""在"不可断处"插占位断点（转义后变 \allowbreak），修复 overfull。

    两类实测触发的不可断（最小复现定位，见测试记录第 12 条）：
    1. 超长 ASCII 连块（≥13 字符）：无空白机器串/粘连文本，LaTeX 在其中
       无断点——60-2681pt 级 overfull 的主来源；
    2. `_`（\textunderscore）链："STD\_LOGIC\_VECTOR" 类词在 xeTeX 下
       断行异常（k≥2 处 \_ 共存即触发 78pt+ overfull，vG 实验证明
       \_ 后插 \allowbreak 完全清零）。
    """
    s = s.replace("_", "_\x00")
    return _LONG_TOKEN_RE.sub(
        lambda m: "\x00".join(re.findall(r".{1,12}", m.group(0))), s)


def _tex_escape_basic(s: str) -> str:
    s = _soften_long_tokens(s)
    out = "".join(TEX_ESCAPE.get(ch, ch) for ch in s)
    # SimSun 无字形的字符转文本命令（转义后做，避免 \textsubscript 的 {} 被二次转义）
    for ch, rep in TEXT_UNICODE_FIX.items():
        out = out.replace(ch, rep)
    return out.replace("\x00", r"\allowbreak ")


def tex_escape(s: str) -> str:
    r"""转义文本；URL 用 \url{} 包裹（xurl 允许任意断行）。

    正文/表格里 URL 常与中文标点粘连（"…/rvfpga/）。"），按 ASCII 字符集
    精确截取 URL 本体，尾部句读（. , ; :）归还普通文本。\url 内不转义。
    """
    if "http" not in s and "www." not in s:
        return _tex_escape_basic(s)
    parts, last = [], 0
    for m in _URL_RE.finditer(s):
        u = m.group(0)
        trail = ""
        while u and u[-1] in ".,;:":
            trail = u[-1] + trail
            u = u[:-1]
        if not u:
            continue
        parts.append(_tex_escape_basic(s[last:m.start()]))
        parts.append(r"\url{" + u + "}")
        parts.append(_tex_escape_basic(trail))
        last = m.end()
    parts.append(_tex_escape_basic(s[last:]))
    return "".join(parts)


def tex_escape_mixed(s: str) -> str:
    r"""转义但保护 $...$ 段（表格单元格/标题里混排行内公式）。

    $ 不成对时**整体按文本转义**（\$）：实测表格里有 ASCII 字符表单元格 "$"
    与美元金额 "$100"（1 个 $），若按公式处理会打开未闭合的数学模式，把后续
    单元格的 & 变成"Misplaced alignment tab"错误。
    """
    parts = s.split("$")
    if len(parts) % 2 == 0:  # $ 个数为奇数（不成对）→ 全部文本
        return tex_escape(s)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 0:
            out.append(tex_escape(p))
        else:
            # 公式段前后缀 \allowbreak：行内公式盒与相邻文本/中文紧贴时
            # xeTeX 无断点（实测 "$imm_{11:0}$upimm:" 类 55pt overfull），
            # 零宽断点不影响渲染。后缀必须带 {}：\allowbreak 后紧跟字母/汉字
            # 会被当作一个控制序列名吞掉（\allowbreakupimm → Undefined）。
            out.append(r"\allowbreak$" + clean_equation(p) + r"$\allowbreak{}")
    return "".join(out)


def _display_width(s: str) -> int:
    """文本显示宽（CJK/全角=2，其余=1），与 wrap_code_line 同口径。"""
    return sum(2 if ord(c) > 127 else 1 for c in s)


def _min_break_width(s: str) -> int:
    """可折行时的最小列宽需求（display 单位）= 最长不可断块宽。

    CJK/全角字符每字可断（视作断点）；ASCII 词/数字串/代码串不可断。
    """
    best = cur = 0
    for ch in s:
        if ord(ch) > 127 or ch.isspace():
            best = max(best, cur)
            cur = 0
        else:
            cur += 1
    return max(best, cur)


# 版心与字形宽度估算（ctexbook 11pt A4：版心约 425pt，ASCII≈5.5pt，
# tabcolsep 默认 6pt×2=12pt/列，竖线约 4pt/列）。用于判断表格是否超宽。
_PAGE_PT = 425.0
_CHAR_PT = 5.5
_SEP_PT = 12.0
_RULE_PT = 4.0
# ASCII 不可断块估计上限：超此长度的块实测都是 VLM 粘连文本/机器串（硬断可行）；
# 含 URL 的格放宽到 40（\url{} 允许任意断行）
_MB_CAP = 20
_URL_CAP = 40

_LINE_PT = 13.6        # 11pt 正文行距（baselineskip）
_ROW_EXTRA_PT = 2.0    # 每行分摊的 \hline 与格上下 padding
# 整表估计高度超此阈值 → 改 longtable 跨页。tabular 不可断页，超高的表尾部
# 直接溢出版心压到页脚/页码（实测 7404 数据手册表高 759pt > 版心 700pt，
# 尾部 3 行叠在页码上）。版心高约 700pt，留 100pt 给估算误差。
_LONGTABLE_H_PT = 600.0


def _est_table_height(rows, ncol: int, mode: str, col_pt=None) -> float:
    """整表渲染高度粗估（pt），仅用于 longtable 判定。

    c：内容不折行按一行；p：按每格显示宽/列宽估折行数取行内最大。
    不要求精确——判定阈值留 100pt 余量，偏保守（高估无副作用，低估才漏判）。
    """
    h = 0.0
    for r in rows:
        lines = 1
        if mode == "p" and col_pt:
            ci = 0
            for c in r:
                span = min(c["colspan"], ncol - ci)
                if span == 1:
                    wpt = col_pt[ci]
                else:  # 跨列格按 span 列总宽（含列间 tabcolsep）
                    wpt = sum(col_pt[ci:ci + span]) + _SEP_PT * (span - 1)
                if wpt > 1:
                    tw = _display_width(re.sub(r"\s+", " ", c["text"]).strip()) * _CHAR_PT
                    lines = max(lines, int(tw / wpt) + 1)
                ci += span
        h += lines * _LINE_PT + _ROW_EXTRA_PT
    return h


def _fig_width_frac(b: dict, fallback_bbox: list | None,
                    page_span: float = 0.80) -> float | None:
    """按块 bbox 宽度算插图宽度占版心比例（原书页宽占比 → 版心宽占比）。

    bbox 是**整页宽**占比（含页边距），而插入宽度乘的是 \\linewidth（版心宽）
    ——直接相乘会把所有元素系统性缩小 ~20%（版心约占页宽 0.80）并使并排
    元素间的间隙被撑大。page_span = 版心宽占页宽比例（由 convert 按 text
    块 x0 的 p10 与 x1 的 p90 标定，实测本书 0.796）。

    只给 width 不给 height：导出图与 bbox 同裁，图片像素长宽比即 bbox 区域
    比例，故高度自动等比（bbox 高 ≤1.0 时图高 ≤20cm < 版心高 24.7cm）。
    实测原硬编码 width=0.8\\linewidth 把 bbox 宽 0.3 的小图放大 2.6 倍
    （图1.32 非门占了大半页）。
    """
    bb = b.get("bbox") or fallback_bbox
    if not bb or len(bb) != 4:
        return None
    w = bb[2] - bb[0]
    if not (0.03 <= w <= 1.0) or page_span <= 0:
        return None  # 异常 bbox → 调用方回退旧参数
    return min(1.0, w / page_span)  # 版心系；满版图 cap 到全宽


def _can_side_by_side(a: dict, b: dict) -> bool:
    """原书里左右并排的两个图块（同页、纵向重叠 ≥55%、横向分离）。

    blocks 为单页列表，相邻块必同页。实测教材"a)…b)…"式双图常被 VLM 切成
    两个 image 块，各自一行会白占两倍版面。
    """
    ba, bb = a.get("bbox"), b.get("bbox")
    if not ba or not bb:
        return False
    ha, hb = ba[3] - ba[1], bb[3] - bb[1]
    if ha <= 0 or hb <= 0:
        return False
    ov = min(ba[3], bb[3]) - max(ba[1], bb[1])
    if ov / min(ha, hb) < 0.55:
        return False
    return ba[2] <= bb[0] + 0.02 or bb[2] <= ba[0] + 0.02


# VLM 偶发把题面标成 paragraph_title（实测"7.2 请重复习题7.1的问题…"，
# 而同页同构的"7.12"却是 text）——降级为正文段落可消 33-42pt 标题空档
# 并防止几百道题污染目录。真节标题（"1.4.4 字节、半字和字"）不会被匹配。
_EXERCISE_VERB_RE = re.compile(r"请|重复|假设|下列|求|证明|解释|计算")


def _is_exercise_like(title: str) -> bool:
    if not re.match(r"^\d+\.\d+(?![.\d])", title):
        return False
    if re.fullmatch(r"\d+\.\d+\s*习题", title):
        return False  # 纯"X.Y 习题"节标题（习题区的节名）
    if "现实世界视角" in title:
        return False  # 该书每章末固定栏目，是真节标题（如"7.8 现实世界视角:…"）
    if "习题" in title:
        return True
    return len(title) >= 12 and bool(_EXERCISE_VERB_RE.search(title))


# 第 1 章标题"数字系统"在 JSON 里是 paragraph_title 且无"第1章"前缀
# （第 2 章起均为 doc_title），按无编号默认会被降成 section 级——
# 特判升为 chapter，保持全书章级一致
_CHAPTER_TITLE_OVERRIDES = {"数字系统"}


def table_to_tex(block: dict) -> str:
    """table 块 → tabular。

    MinerU 的 table_body 是 <tr><td> HTML（实测含 colspan/rowspan 与 <eq> 行内
    公式），转成 \\multicolumn/\\multirow；单元格文本按 $ 分段转义。

    列格式三档（c 列不折行，长内容会把表推出纸外，整书实测最大溢出 2681pt）：
    - c：整格宽估算 ≤ 版心 → 原样（多数数据表）；
    - p：整格超宽、但折行需求（最长不可断块）放得下 → p{宽} 折行列，列宽
      水填分配：每列先保 need，余量按 (need+12) 权重分配 \\linewidth；
    - resize：列过多/块过长物理放不下（如 32 列位编号表）→ \\resizebox 等比
      缩放到 \\linewidth，同时把 tabcolsep 收到 2pt 减少缩放倍率（保字号）。

    注意 p{宽} 是**内容区宽**（不含两侧 tabcolsep），故预算 = 版心 - 12×列数。
    """
    parsed = parse_table_html(block)
    if not parsed:
        return ""
    cap, rows = parsed
    ncol = max(sum(c["colspan"] for c in r) for r in rows)

    # 各列：整格宽 col_w（不可折假设）与最小可断宽 col_min（折行后需求）
    col_w = [0.0] * ncol
    col_min = [0.0] * ncol
    for r in rows:
        ci = 0
        for c in r:
            span = min(c["colspan"], ncol - ci)
            t = re.sub(r"\s+", " ", c["text"]).strip()
            w = _display_width(t)
            if w <= 24:
                mw = w
            else:
                mw = min(_min_break_width(t), _URL_CAP if "http" in t else _MB_CAP)
            if span == 1:
                col_w[ci] = max(col_w[ci], w)
                col_min[ci] = max(col_min[ci], mw)
            else:  # 跨列单元格：宽需求按 span 平摊
                per, pm = w / span, mw / span
                for k in range(span):
                    col_w[ci + k] = max(col_w[ci + k], per)
                    col_min[ci + k] = max(col_min[ci + k], pm)
            ci += c["colspan"]

    w_c = sum(col_w) * _CHAR_PT + (_SEP_PT + _RULE_PT) * ncol
    # 每列最小内容区宽（下限 1 个 CJK 字宽 11pt——纯中文列 min_break 为 0）
    need = [max(m * _CHAR_PT, 11.0) + 6 for m in col_min]
    avail = _PAGE_PT - _SEP_PT * ncol           # p 化后内容区总预算
    if w_c <= _PAGE_PT:
        mode = "c"
    elif sum(need) <= avail * 1.35:
        mode = "p"   # 允许至多 35% 超预算（超则按比例压缩，个别长块微溢出）
    else:
        mode = "resize"

    out = []
    if cap:
        out.append(f"{{\\small {tex_escape_mixed(cap)}}}\\\\[2pt]")

    # 超页大表用 longtable 跨页（tabular 不可断页，会尾溢压页码）；resize 表
    # 已被 resizebox 定死尺寸，保持 tabular 不动
    longtable = False
    if mode == "p":
        extra = avail - sum(need)
        if extra >= 0:  # 水填：每列先保 need，余量按 (need+12) 权重分配
            wsum = sum(n_ + _SEP_PT for n_ in need) or 1.0
            pt = [n_ + extra * (n_ + _SEP_PT) / wsum for n_ in need]
        else:           # 超预算：按 need 比例压缩
            pt = [n_ * avail / sum(need) for n_ in need]
        fracs = [p_ / _PAGE_PT for p_ in pt]

        def span_frac(i: int, span: int) -> float:
            f = sum(fracs[i:i + span]) + _SEP_PT * (span - 1) / _PAGE_PT
            return min(f, 0.97)

        longtable = _est_table_height(rows, ncol, "p", pt) > _LONGTABLE_H_PT
        spec = "|" + "|".join(f"p{{{f:.3f}\\linewidth}}" for f in fracs) + "|"
        out.append(f"\\begin{{{'longtable' if longtable else 'tabular'}}}{{{spec}}}")
    elif mode == "resize":
        out.append("\\resizebox{\\linewidth}{!}{%")
        out.append("\\setlength{\\tabcolsep}{2pt}%")
        out.append("\\begin{tabular}{|" + "c|" * ncol + "}")
    else:
        longtable = _est_table_height(rows, ncol, "c") > _LONGTABLE_H_PT
        out.append(f"\\begin{{{'longtable' if longtable else 'tabular'}}}{{|" + "c|" * ncol + "}")

    out.append("\\hline")
    for r in rows:
        cells = []
        ci = 0
        for c in r:
            t = tex_escape_mixed(re.sub(r"\s+", " ", c["text"]).strip())
            span = c["colspan"]
            if c["rowspan"] > 1:
                if mode == "p":
                    # p 列折行场景不放 multirow：折行文本高度可能超 n 行总高，
                    # multirow 不检查高度必然叠印（实测图A.8 数据手册表：`=`、
                    # 显式宽度两版均叠）。内容留首行（被跨行本无此格，列不错位）。
                    pass
                else:
                    t = f"\\multirow{{{c['rowspan']}}}{{*}}{{{t}}}"
            if span > 1:
                if mode == "p":
                    wf = span_frac(ci, span)
                    t = f"\\multicolumn{{{span}}}{{p{{{wf:.3f}\\linewidth}}|}}{{{t}}}"
                else:
                    t = f"\\multicolumn{{{span}}}{{c|}}{{{t}}}"
            cells.append(t)
            ci += span
        out.append(" & ".join(cells) + " \\\\")
        out.append("\\hline")
    if mode == "resize":
        out.append("\\end{tabular}")
        out.append("}")
    else:
        out.append("\\end{longtable}" if longtable else "\\end{tabular}")
    text = "\n".join(out)
    # longtable 塞进 center 会丢掉跨页断行能力，用哨兵让调用处不再包 center
    return ("% LONGTABLE\n" + text) if longtable else text


def wrap_code_line(line: str, limit: int = 76) -> str:
    """verbatim 不自动折行：显示宽（ASCII=1、CJK=2）超限的行走硬折。

    limit=76：\\small 等宽（lmtt ~9pt）在 15cm 行宽下约 78 字符/行；
    实测最长 273 字符（VLM 把中文说明误并入代码块）必须折，否则溢出页面。
    """
    if sum(2 if ord(c) > 127 else 1 for c in line) <= limit:
        return line
    out, cur, w = [], [], 0
    for ch in line:
        cw = 2 if ord(ch) > 127 else 1
        if w + cw > limit:
            out.append("".join(cur))
            cur, w = [], 0
        cur.append(ch)
        w += cw
    out.append("".join(cur))
    return "\n".join(out)


def split_code_caption(code: str) -> tuple[str, str]:
    """剥离误粘在代码里的图注。

    实测"…infinite loop 50 00210063图7.64 测试程序riscvtest.s"（图注粘连
    在代码行尾）与独立成行的"图 4.18 sync 综合后的电路"——原书里代码列表
    本身就是"图"，VLM 把图号标题并进了 code_body。从尾往前扫 12 行找。
    """
    lines = code.split("\n")
    n = len(lines)
    for i in range(n - 1, max(-1, n - 13), -1):
        s = lines[i].rstrip()
        if not s:
            continue
        m = re.search(r"图\s*[0-9A-Z]+[-.．]\d+[a-z]?\s+\S.{0,60}$", s)
        if m and not _CAPTION_REF_RE.match(m.group(0)):
            cap = m.group(0).strip()
            rest = s[:m.start()].rstrip()
            new_lines = lines[:i] + ([rest] if rest else [])
            return "\n".join(new_lines).rstrip(), cap
    return code, ""


# verbatim 内字符 → ASCII 近似（只收 lmmono10 无字形的字符，实测 05 伪代码 28 处缺字：
# ⌊⌋≡∘⁰ⁿ⁻₀ʲ₂∫∉⊂∪α●）。不能用 $..$+mathescape（fvextra 实现会把非数学段包进 \text{}，
# ^ 触发 "Missing $ inserted" 连锁，实测 05 一轮 80 error）；ASCII 化零风险且契合伪代码语境。
# ∈ ω ≤ ≥ ² ¹ → · 等 lmmono 自带字形的不动，保持代码块原样。
# 06 追加（CLRS 伪代码实测缺字）：∅11 δ4 ₄₁∑3 ₐ2 ⊗2 ᵢ₈₇₆₅ ρ ⊃ 各若干
VERBATIM_UNICODE_FIX = str.maketrans({
    "π": "pi", "γ": "gamma", "α": "alpha",
    "●": "*", "∘": "o", "≡": "==",
    "∫": "int", "∉": "not in", "⊂": "subset",
    "∪": "union", "◎": "(o)", "⟩": ">",
    "☑": "[x]", "⌊": "[", "⌋": "]",
    "ʲ": "^j", "ⁿ": "^n", "⁰": "^0", "⁻": "^-",
    "₀": "_0", "₂": "_2",
    "∅": "{}", "δ": "delta", "ρ": "rho", "⊃": "superset",
    "∑": "sum", "⊗": "(x)",
    "₁": "_1", "₃": "_3", "₄": "_4", "₅": "_5", "₆": "_6", "₇": "_7",
    "₈": "_8", "₉": "_9", "ₐ": "_a", "ᵢ": "_i",
})


def code_prepare(block: dict) -> tuple[str, str]:
    """code 块 → (verbatim 文本, 剥离出的图注)：尾巴剥图注 + 超宽行预折。

    另修 VLM 误识：Verilog 参数化实例 `#(12)` 被识别成 `⌊(12)`（实测 3 处，
    兼治 verbatim 里 ⌊ 的字体缺字）。
    """
    raw, cap = split_code_caption(code_text(block))
    if not raw:
        return "", cap
    if len(raw) > 600:
        raw = cut_repetition_loop(raw)  # 伪代码极少循环，同批防御
        if not raw:
            return "", cap
    raw = re.sub(r"⌊(?=\()", "#", raw)
    # 字体缺字字符 → ASCII 近似（先替换后折行：替换会改变显示宽，宁可多折一行）
    raw = raw.translate(VERBATIM_UNICODE_FIX)
    return "\n".join(wrap_code_line(ln) for ln in raw.split("\n")), cap


def _block_segments(block: dict) -> list[tuple[str, str, str]]:
    """block → [(原始文本, 转义输出, 段类型)]。

    06 二轮：循环截断须在原始文本流上判定（转义产物 \\textbackslash{}、
    \\_\\allowbreak 等在公式里天然高频重复，在其上判定会误删正常内容），
    故每个段同时保留原文与输出，供截断点向输出映射。
    """
    c = block.get("content")
    if isinstance(c, str):
        return [(c, tex_escape(fix_eq_refs(c)), "text")]
    if not isinstance(c, list):
        return []
    segs: list[tuple[str, str, str]] = []
    for part in c:
        if isinstance(part, dict):
            ptype = part.get("type")
            pc = part.get("content", "")
            if ptype == "equation_inline" and isinstance(pc, str):
                eq = clean_equation(pc)
                # 脚注记号上标（如 $^{①}$）：数学字体里没有带圈数字/罗马数字
                # 字形，转文本模式上标（走 CJK 字体，可正常渲染）
                m = re.fullmatch(r"\^\{([⓪①-⑳Ⅰ-Ⅻⅰ-ⅿ]+)\}", eq.strip())
                out = f"\\textsuperscript{{{m.group(1)}}}" if m else f"${eq}$"
                segs.append((pc, out, "eq"))
            elif isinstance(pc, str):
                segs.append((pc, tex_escape(fix_eq_refs(pc)), "text"))
            elif isinstance(pc, (list, dict)):
                segs.extend(_block_segments(part))
        elif isinstance(part, str):
            segs.append((part, tex_escape(fix_eq_refs(part)), "text"))
    return segs


def block_text_tex(block: dict) -> str:
    """与 json_to_readable.block_text 同构，但 text 子块做 LaTeX 转义、
    equation_inline 子块原样进 $..$（公式内容不能转义）。

    06 二轮修复：循环截断判定改在原始文本流上（见 _block_segments），
    截断点按段边界映射回输出——部分覆盖的 text 段取前缀转义，
    部分覆盖的 eq 段整体丢弃（防切出半截 $..$ 对引发 Missing $）。
    """
    segs = _block_segments(block)
    if not segs:
        return ""
    raw_full = "".join(r for r, _, _ in segs)
    raw_cut = cut_repetition_loop(raw_full)
    if raw_cut == raw_full:
        return "".join(o for _, o, _ in segs)
    k = len(raw_cut)
    out = []
    acc = 0
    for r, o, kind in segs:
        if acc + len(r) <= k:
            out.append(o)
            acc += len(r)
        else:
            if kind == "text" and k > acc:
                out.append(tex_escape(raw_cut[acc:]))
            break
    return "".join(out)


PREAMBLE = r"""\documentclass[11pt,openany]{ctexbook}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{graphicx}
\usepackage{multirow}  % 表格 rowspan
\usepackage{fancyvrb}
% 代码块：小号等宽（fancyvrb 无自动折行，超宽行由转换器预折，见 wrap_code_line）
\DefineVerbatimEnvironment{verbatim}{Verbatim}{fontsize=\small}
\usepackage{longtable}  % 超页大表跨页（tabular 不可断页，会溢出版心压页码）
\usepackage{float}
\usepackage{geometry}
\geometry{a4paper,margin=2.5cm}
\usepackage[hidelinks]{hyperref}  % hidelinks: 链接无红框
\usepackage{xurl}  % 表格/正文里的长 URL 允许任意断行
\usepackage{extarrows}  % \xlongequal 等长箭头（MinerU 公式实测用到，见 测试记录 03）
\usepackage{newunicodechar}
% 文本模式裸 Unicode 希腊字母/符号 → 数学模式（Latin Modern 罗马体无这些字形。
% 实测缺字：03 φ1 λ1 ω2 ➢2；04 ≈1 β1 ∈1 ∵1。已走 CJK 字体路由的 μθ 等维持不动）
\newunicodechar{φ}{\ensuremath{\varphi}}
\newunicodechar{λ}{\ensuremath{\lambda}}
\newunicodechar{ω}{\ensuremath{\omega}}
\newunicodechar{➢}{\ensuremath{\blacktriangleright}}
\newunicodechar{≈}{\ensuremath{\approx}}
\newunicodechar{β}{\ensuremath{\beta}}
\newunicodechar{∈}{\ensuremath{\in}}
\newunicodechar{∵}{\ensuremath{\because}}
% 05 算法概论实测缺字（正文 lmroman）：π13 γ2 ●1 ☑1 ◎1 ⟩1（方括号中为同批防御性映射）
\newunicodechar{π}{\ensuremath{\pi}}
\newunicodechar{γ}{\ensuremath{\gamma}}
\newunicodechar{α}{\ensuremath{\alpha}}
\newunicodechar{●}{\ensuremath{\bullet}}
\newunicodechar{∘}{\ensuremath{\circ}}
\newunicodechar{≡}{\ensuremath{\equiv}}
\newunicodechar{∫}{\ensuremath{\int}}
\newunicodechar{∉}{\ensuremath{\notin}}
\newunicodechar{⊂}{\ensuremath{\subset}}
\newunicodechar{∪}{\ensuremath{\cup}}
\newunicodechar{◎}{\ensuremath{\circledcirc}}
\newunicodechar{⟩}{\ensuremath{\rangle}}
\newunicodechar{☑}{\ensuremath{\checkmark}}
\newunicodechar{ʲ}{\ensuremath{^{j}}}
\newunicodechar{ⁿ}{\ensuremath{^{n}}}
\newunicodechar{⁰}{\ensuremath{^{0}}}
\newunicodechar{⁻}{\ensuremath{^{-}}}
\newunicodechar{₀}{\ensuremath{_{0}}}
\newunicodechar{₂}{\ensuremath{_{2}}}
% 06 算法导论实测缺字（正文 lmroman，缩印小字更多生僻符号）：◇10 ■3 ε4 δ3
% ⊗4 ⇒2 ι ↘ ▪ □ ⊢ ⊠ ⊉ ≲ ≫ ∆ ∅ ★ σ 各1；∑ ρ ᵢ ₐ 为编码侧同批防御
\newunicodechar{◇}{\ensuremath{\lozenge}}
\newunicodechar{ε}{\ensuremath{\varepsilon}}
\newunicodechar{■}{\ensuremath{\blacksquare}}
\newunicodechar{δ}{\ensuremath{\delta}}
\newunicodechar{⊗}{\ensuremath{\otimes}}
\newunicodechar{⇒}{\ensuremath{\Rightarrow}}
\newunicodechar{ι}{\ensuremath{\iota}}
\newunicodechar{↘}{\ensuremath{\searrow}}
\newunicodechar{▪}{\ensuremath{\blacksquare}}
\newunicodechar{□}{\ensuremath{\square}}
\newunicodechar{⊢}{\ensuremath{\vdash}}
\newunicodechar{⊠}{\ensuremath{\boxtimes}}
\newunicodechar{⊉}{\ensuremath{\nsupseteq}}
\newunicodechar{≲}{\ensuremath{\lesssim}}
\newunicodechar{≫}{\ensuremath{\gg}}
\newunicodechar{∆}{\ensuremath{\Delta}}
\newunicodechar{∅}{\ensuremath{\varnothing}}
\newunicodechar{★}{\ensuremath{\bigstar}}
\newunicodechar{σ}{\ensuremath{\sigma}}
\newunicodechar{∑}{\ensuremath{\sum}}
\newunicodechar{ρ}{\ensuremath{\rho}}
\newunicodechar{ᵢ}{\ensuremath{_{i}}}
\newunicodechar{ₐ}{\ensuremath{_{a}}}
% 06 二轮编译残余缺字（lmroman10，逻辑/比较符号）
\newunicodechar{∧}{\ensuremath{\wedge}}
\newunicodechar{≪}{\ensuremath{\ll}}
\newunicodechar{⊆}{\ensuremath{\subseteq}}
\graphicspath{{images/}}

% 罗马数字（"情况 I/II/III" 的 II III 等）与带圈数字（表格里的 ①②③）
% 交给中文字体渲染，否则被路由到 Latin Modern 导致缺字
\xeCJKDeclareCharClass{CJK}{`Ⅰ,`Ⅱ,`Ⅲ,`Ⅳ,`Ⅴ,`Ⅵ,`Ⅶ,`Ⅷ,`Ⅸ,`Ⅹ,`Ⅺ,`Ⅻ,%
`⓪,`①,`②,`③,`④,`⑤,`⑥,`⑦,`⑧,`⑨,`⑩,`⑪,`⑫,`⑬,`⑭,`⑮,`⑯,`⑰,`⑱,`⑲,`⑳}
% 数学符号（SimSun 有字形但默认路由到 Latin Modern → 整书实测缺字：
% ≠17 ≤10 ≥10 μ12 ∩1 θ1 ⊕2 ○1 处，见 测试记录 02 第 11 条）
\xeCJKDeclareCharClass{CJK}{`≠,`≤,`≥,`μ,`∩,`θ,`⊕,`○}
% 数学模式内的中文/标点走 CJK 字体（公式混入的顿号、汉字，实测 70+ 处缺字）
\xeCJKsetup{CJKmath=true}
\pagestyle{plain}

% —— 紧凑版面（用户要求；实测 book 类 flushbottom 把页面不满处的竖直
% 间隙全部拉伸，编译 log 有 278 个 Underfull \vbox badness 10000）——
\raggedbottom  % 页不满时底部留白，不拉伸段间/标题间距
\setlength{\parskip}{0pt}  % 段间距显式归零（book 默认 0pt plus 1pt 微弹性）
\AtBeginDocument{\linespread{1.2}\selectfont}  % 行距 1.3→1.2（ctexbook 默认 1.3）
% 各级标题前后间距压缩（ctex 默认带弹性，配合 raggedbottom 改固定值）；
% 样式对齐原书：原书章/节标题全部靠左（ctex 默认 chapter/section 居中，
% 与原书不符——原书 p45 章扉页、p28 节标题均为靠左加粗），format 为
% 完整替换故显式重写字重与字号（章 \zihao{2}、节 \large，同 ctex 默认量级）
\ctexset{
  chapter = {format = {\raggedright\bfseries\zihao{2}},
             beforeskip = 20pt, afterskip = 28pt},
  section = {format = {\raggedright\bfseries\large},
             beforeskip = 2.0ex, afterskip = 1.0ex},
  subsection = {beforeskip = 1.7ex, afterskip = 0.9ex},
  subsubsection = {beforeskip = 1.3ex, afterskip = 0.7ex},
}

\title{@@TITLE@@}
\author{MinerU 结构化转换}
\date{\today}

\begin{document}
\maketitle
\tableofcontents
\clearpage

"""

POSTAMBLE = "\n\\end{document}\n"

# 强终止符：出现在段尾说明该段已完结，不参与跨页合并
# 注意不含逗号/顿号——宁漏勿错（漏合只是两段分开，错合是语义串台）
SENT_END = set("。！？：；!?:;”’」』))】*")

# 后块以此开头 = 新的题号/列表项（习题页防误报：实测把"1.33题尾"与"1.34题头"合并）
_LIST_ITEM_RE = re.compile(r"^\s*(?:第\s*\d+\s*[章节题]|[（(]?[a-zA-Z0-9][)）.．、])")


def tag_cross_page_merges(pages_by_idx: dict) -> None:
    """跨页断段合并：给"跨页续接的后块"打 _merge_prev 标记。

    两路信号取并：
      原生 continues_prev 字段（VLM 直接判断，实测 135 处）
      bbox 启发式（前页 index 最大 text 块贴页底 >0.85 且尾无强终止符 +
                 后页首块为 text 且贴页顶 <0.15，实测 4 页样本 p100→p101 成功）
    防线：后块以题号/列表项开头（"1.34 …"、"（b）…"）时不合并——
    原生与启发式在习题页都会误报（"1.33题尾"贴页底 + "1.34题头"贴页顶）。
    """
    idxs = sorted(pages_by_idx)
    for a, b in zip(idxs, idxs[1:]):
        if b != a + 1:
            continue
        pa, pb = pages_by_idx[a], pages_by_idx[b]
        va = [x for x in pa.get("blocks", []) if x.get("type") not in SKIP_TYPES]
        vb = [x for x in pb.get("blocks", []) if x.get("type") not in SKIP_TYPES]
        texts_a = [x for x in va if x.get("type") == "text"]
        if not texts_a or not vb:
            continue
        prev = max(texts_a, key=lambda x: x.get("index", 0))
        nxt = min(vb, key=lambda x: x.get("index", 0))
        if nxt.get("type") != "text":
            continue
        nt = block_text(nxt).strip()
        if not nt or _LIST_ITEM_RE.match(nt):
            continue
        pt = block_text(prev).strip()
        py2 = (prev.get("bbox") or [0, 0, 1, 1])[3]
        ny1 = (nxt.get("bbox") or [0, 0, 1, 1])[1]
        native = bool(nxt.get("continues_prev"))
        heur = bool(pt) and pt[-1] not in SENT_END and py2 > 0.85 and ny1 < 0.15
        if native or heur:
            nxt["_merge_prev"] = True
            print(f"  [跨页合并{'·原生' if native else ''}] p{a + 1}尾…{pt[-12:]!r} "
                  f"+ p{b + 1}首 {nt[:12]!r}…")


def convert(src: Path, out_tex: Path) -> Path:
    data = json.loads(src.read_text(encoding="utf-8"))

    body: list[str] = []
    last_page = None
    stats = {"title": 0, "equation": 0, "image": 0, "text": 0, "code": 0, "table": 0}
    used_names: set[str] = set()

    pages = sorted(data.get("pages", []), key=lambda p: p.get("page_idx", 0))
    tag_cross_page_merges({p.get("page_idx", 0): p for p in pages})

    # —— P1 工具修复（2026-10-08）：编号引用还原 + 相邻块重叠合并 ——
    set_tag_set(collect_tag_ids(pages))
    _n_merged = merge_overlapping_neighbors(pages)
    if _n_merged:
        print(f"  [块合并] 相邻块重叠合并 {_n_merged} 处")

    # —— 章标题配对（正文书眉"第N章 章名"表 + 章首页书眉兜底）——
    # 见 json_to_readable.resolve_chapter_titles；命中块升 chapter 级并合成
    # "第 N 章"前缀，doc_title 章名同步补号（两类标题样式统一）
    _ch_pairs = resolve_chapter_titles(pages)
    _ch_name2no = {v: n for n, v in build_chapter_name_map(pages).items()}
    if _ch_pairs:
        print(f"  [章标题] 配对 {len(_ch_pairs)} 个章名标题")

    # —— 重复栏目名的目录剔除名单（预扫描一遍；阈值见 _TOC_SKIP_* 注释）——
    _title_counts: dict[str, int] = {}
    for _pg in pages:
        for _b in _pg.get("blocks", []):
            if _b.get("type") == "paragraph_title":
                _t = block_text(_b).strip()
                if _t:
                    _title_counts[_t] = _title_counts.get(_t, 0) + 1
    _TOC_SKIP_TITLES = {t for t, c in _title_counts.items()
                        if c >= _TOC_SKIP_MIN_COUNT and len(t) <= _TOC_SKIP_MAX_LEN}
    if _TOC_SKIP_TITLES:
        print(f"  [目录剔除] 重复栏目名（>={_TOC_SKIP_MIN_COUNT} 次）: "
              f"{sorted(_TOC_SKIP_TITLES)}")

    # —— 版心标定（页宽系 → 版心系换算基准，见 _fig_width_frac docstring）——
    # 左缘取 text 块 x0 的 p10（避开列表缩进等右偏样本）、右缘取 x1 的 p90
    _x0s, _x1s = [], []
    for _pg in pages:
        for _b in _pg.get("blocks", []):
            if _b.get("type") in ("text", "ref_text") and _b.get("bbox"):
                _x0s.append(_b["bbox"][0])
                _x1s.append(_b["bbox"][2])
    if len(_x0s) >= 50:
        _x0s.sort()
        _x1s.sort()
        text_span = _x1s[int(len(_x1s) * 0.90)] - _x0s[int(len(_x0s) * 0.10)]
    else:
        text_span = 0.80
    if not (0.50 <= text_span <= 0.95):
        text_span = 0.80  # 异常兜底（正常书籍版心约占页宽 0.7-0.9）
    print(f"  版心标定: 版心宽/页宽 = {text_span:.3f}")

    last_para_idx = None
    for page in pages:
        page_idx = page.get("page_idx", 0)
        blocks = sorted(page.get("blocks", []), key=lambda b: b.get("index", 0))
        cap_map = infer_missing_captions(blocks)
        skip_bis: set[int] = set()  # 已被他块消费的图块（图文配对/图图并排）

        # —— 图文侧排配对（原书版式：左栏文字段落 + 右栏小图，实测 p26/p34
        # 等页块顺序恒为"先文后图"；MinerU 的 text 块 bbox 只框到文字栏宽，
        # 与侧图天然 x 分离）。双向唯一才配（该 text 只匹配 1 图、该图只匹配
        # 1 text），多候选全放弃——图谱错段比不还原更糟。
        fig_pair: dict[int, int] = {}  # id(text 块) → image 块的 bi
        if len(blocks) > 1:
            _ts = [t for t in blocks if t.get("type") == "text"
                   and not t.get("_merge_prev") and (t.get("bbox") or None)
                   and len(block_text(t).strip()) >= 20  # 短块不配（与统计口径一致）
                   and not looks_like_caption(block_text(t).strip())]
            _ims = [im for im in blocks if im.get("type") in ("image", "chart")
                    and (im.get("bbox") or None)]
            if _ts and _ims:
                _t2i: dict[int, list[int]] = {}
                _i2t: dict[int, list[int]] = {}
                for t in _ts:
                    tb = t["bbox"]
                    th = tb[3] - tb[1]
                    if th <= 0:
                        continue
                    for im in _ims:
                        ib = im["bbox"]
                        ih = ib[3] - ib[1]
                        if ih <= 0:
                            continue
                        ov = min(tb[3], ib[3]) - max(tb[1], ib[1])
                        if ov / min(th, ih) < 0.5:
                            continue
                        xsep = ib[0] - tb[2] if ib[0] > tb[2] else tb[0] - ib[2]
                        if xsep < 0.03:
                            continue
                        _t2i.setdefault(id(t), []).append(id(im))
                        _i2t.setdefault(id(im), []).append(id(t))
                _id2bi = {id(im): bi2 for bi2, im in enumerate(blocks)}
                for t in _ts:
                    cands = _t2i.get(id(t), [])
                    # 双向唯一 + 文字在图上文位置之前才配（图若先被独立渲染便无法回收）
                    if (len(cands) == 1 and len(_i2t.get(cands[0], [])) == 1
                            and _id2bi[id(t)] < _id2bi[cands[0]]):
                        fig_pair[id(t)] = _id2bi[cands[0]]

        for bi, b in enumerate(blocks):
            if bi in skip_bis:
                continue
            btype = b.get("type", "")
            if btype in SKIP_TYPES:
                continue

            if page_idx != last_page:
                body.append(f"\n% p.{page_idx + 1}\n")
                last_page = page_idx

            if btype == "doc_title":
                # 前 10 页的 doc_title = 封面/扉页书名（过滤）；之后是章扉页标题
                if page_idx >= 10:
                    title = block_text_tex(b).strip()
                    if title:
                        if looks_like_body_title(block_text(b).strip(), b.get("bbox")):
                            # 误标残片（如 03 第 3 章首页的 doc_title"第3章"，
                            # R5 纯章号）→ 降级加粗正文，不进目录
                            t2 = tex_escape_mixed(block_text(b).strip())
                            body.append(f"\n\\noindent\\textbf{{{t2}}}\n")
                            last_para_idx = len(body) - 1
                            stats["text"] += 1
                        else:
                            # 与书眉章名表配上的章名补"第 N 章"前缀（样式统一）
                            _n = _ch_name2no.get(
                                re.sub(r"\s+", "", block_text(b).strip()))
                            if _n is not None:
                                title = tex_escape_mixed(
                                    chapter_display_title(block_text(b).strip(), _n))
                            cmd = TEX_CMD[1]
                            body.append(f"\n\\{cmd}*{{{title}}}\n")
                            body.append(f"\\addcontentsline{{toc}}{{{cmd}}}"
                                        f"{{{title}}}\n")
                            stats["title"] += 1

            elif btype == "code":
                code, cc = code_prepare(b)
                if code:
                    body.append("\n\\begin{verbatim}\n" + code + "\n\\end{verbatim}\n")
                    stats["code"] += 1
                if cc:
                    body.append(f"\n{{\\small {tex_escape_mixed(cc)}}}\n")

            elif btype == "table":
                tbl = table_to_tex(b)
                if tbl:
                    if tbl.startswith("% LONGTABLE"):
                        body.append("\n" + tbl[len("% LONGTABLE\n"):] + "\n")
                        stats["longtable"] = stats.get("longtable", 0) + 1
                    else:
                        body.append(f"\n\\begin{{center}}\n{tbl}\n\\end{{center}}\n")
                    stats["table"] += 1

            elif btype == "paragraph_title":
                raw_title = block_text(b).strip()
                if not raw_title:
                    continue
                if page_idx <= 14:
                    # 封面/版权页/前言区（第 1 章开始前，实测 page_idx 0-14）的
                    # "标题"全是排版元素：书名、作者/译者名、栏目名（注意/特点/
                    # 致谢）、软件名（Quartus 等）——共 26 条。保留为标题会污染
                    # 目录并制造标题空档 → 降级为加粗正文
                    t = tex_escape_mixed(raw_title)
                    body.append(f"\n\\noindent\\textbf{{{t}}}\n")
                    last_para_idx = len(body) - 1
                    stats["text"] += 1
                    continue
                _pair = _ch_pairs.get((page_idx, b.get("index")))
                if _pair is not None:
                    # 章名标题（与正文书眉章名表/章首页书眉配对成功）→ 章级；
                    # 合成"第 N 章"前缀（原文已带前缀的由 chapter_display_title
                    # 规范化，如"第6章MOS晶体管的物理机理"）
                    _n, _ = _pair
                    title = tex_escape_mixed(chapter_display_title(raw_title, _n))
                    body.append(f"\n\\chapter*{{{title}}}\n")
                    body.append(f"\\addcontentsline{{toc}}{{chapter}}{{{title}}}\n")
                    stats["title"] += 1
                    continue
                if _is_exercise_like(raw_title):
                    # VLM 误判的题面 → 正文段落（题号加粗），消标题空档、不进目录
                    m2 = re.match(r"^(\d+\.\d+)\s+", raw_title)
                    if m2:
                        t = (f"\\noindent\\textbf{{{m2.group(1)}}} "
                             + tex_escape_mixed(raw_title[m2.end():]))
                    else:
                        t = tex_escape_mixed(raw_title)
                    body.append(f"\n{t}\n")
                    last_para_idx = len(body) - 1
                    stats["text"] += 1
                    continue
                if looks_like_body_title(raw_title, b.get("bbox")):
                    # VLM 误标的加粗正文/题面/页眉残片 → 降级为加粗正文，不进目录
                    # （判据与全量回归记录见 json_to_readable.looks_like_body_title）
                    t = tex_escape_mixed(raw_title)
                    body.append(f"\n\\noindent\\textbf{{{t}}}\n")
                    last_para_idx = len(body) - 1
                    stats["text"] += 1
                    continue
                title = block_text_tex(b).strip()
                if not title:
                    continue
                lv = title_level(block_text(b).strip(), b.get("level"))
                if raw_title in _CHAPTER_TITLE_OVERRIDES:
                    lv = 1
                cmd = TEX_CMD.get(lv, "subsubsection")
                body.append(f"\n\\{cmd}*{{{title}}}\n")
                if raw_title not in _TOC_SKIP_TITLES:
                    body.append(f"\\addcontentsline{{toc}}{{{cmd}}}{{{title}}}\n")
                stats["title"] += 1

            elif btype == "equation":
                eq = clean_equation(block_text(b).strip())  # 公式：不转义
                if eq:
                    body.append(f"\n\\begin{{equation*}}\n{eq}\n\\end{{equation*}}\n")
                    stats["equation"] += 1

            elif btype in ("image", "chart"):
                # caption/命名推导与 readable 端共用（figure_info/unique_image_name），
                # chart 的位图由 readable 的 --pdf 裁剪导出，此处引用方式一致
                # —— 图图并排：链式收集原书同一视觉行的连续图块（相邻两两横向
                #    分离 + 纵向重叠 ≥55%，见 _can_side_by_side），2 图及以上
                #    同行渲染；"a) b) c)"式三并排因此得以还原（旧逻辑只认固定
                #    双图，第 3 图被迫独立成行）。链式断裂处（如 2x2 布局的
                #    上一行右图→下一行左图）自然分组成新行，即"排满换行"。
                group = [b]
                j = bi + 1
                while (j < len(blocks) and j not in skip_bis
                       and blocks[j].get("type") in ("image", "chart")
                       and _can_side_by_side(blocks[j - 1], blocks[j])):
                    group.append(blocks[j])
                    j += 1
                figs = []
                for gi, gb in enumerate(group):
                    gcap, gb_bbox = figure_info(gb, blocks, bi + gi, cap_map)
                    gwf = _fig_width_frac(gb, gb_bbox, text_span)
                    if gwf is None:
                        if gi == 0:
                            # 首图无有效宽度：不成组，降级单图（回退双约束参数，见下）
                            gname = unique_image_name(gcap, page_idx + 1, gb.get("index", 0), used_names)
                            gext = "pdf" if (src.parent / "images" / f"{gname}.pdf").exists() else "png"
                            figs.append((gname, gext, gcap, None))
                        break  # 无有效宽度的图不并入并排行（该图及后续按单图处理）
                    gname = unique_image_name(gcap, page_idx + 1, gb.get("index", 0), used_names)
                    gext = "pdf" if (src.parent / "images" / f"{gname}.pdf").exists() else "png"
                    figs.append((gname, gext, gcap, gwf))
                body.append("\n\\begin{figure}[H]\n\\centering\n")  # [H] 钉住原位（忠实原书版面）
                if len(figs) == 1:
                    gname, gext, gcap, gwf = figs[0]
                    # 按原图占原书页宽比例插入（旧版硬拉 0.8\linewidth 把小图放大 2-3 倍）；
                    # 无有效 bbox 回退旧双约束参数（宽扁受宽限、竖长条受高限）
                    opts = (f"width={gwf:.3f}\\linewidth" if gwf is not None
                            else "width=0.8\\linewidth,height=0.8\\textheight,keepaspectratio")
                    body.append(f"\\includegraphics[{opts}]{{{gname}.{gext}}}\n\n")
                    if gcap:
                        # 图注可能混排行内公式（实测"带无关项 $X$ 的…"），$ 段保留
                        body.append(f"{{\\small {tex_escape_mixed(gcap)}}}\n")
                    stats["image"] += 1
                else:
                    # 各图按原比例占位、紧挨无间隔、整行居中（旧版 \hfill 两端分布，
                    # 用户反馈"分居两端"改居中）；合计 >0.97 时等比压缩到版心内
                    # （原书并排图不拆行——压缩比拆散更忠实原版面）
                    ws = [f[3] if f[3] is not None else 0.30 for f in figs]
                    if sum(ws) > 0.97:
                        kc = 0.97 / sum(ws)
                        ws = [w * kc for w in ws]
                    for idx2, ((gname, gext, gcap, _), fw2) in enumerate(zip(figs, ws)):
                        body.append(f"\\begin{{minipage}}[t]{{{fw2:.3f}\\linewidth}}\\centering\n")
                        body.append(f"\\includegraphics[width=\\linewidth]{{{gname}.{gext}}}\n\n")
                        if gcap:
                            body.append(f"{{\\small {tex_escape_mixed(gcap)}}}\n")
                        # %\n 抑制行尾空格（否则 minipage 之间多出一个词间空隙）
                        body.append("\\end{minipage}%\n" if idx2 < len(figs) - 1
                                    else "\\end{minipage}\n")
                    for kk in range(len(figs)):
                        skip_bis.add(bi + kk)  # 全组成员标记：防图文配对把侧图二次消费
                    stats["image"] += len(figs)
                    stats["side_by_side"] = stats.get("side_by_side", 0) + 1
                    if len(figs) >= 3:
                        stats["multi3"] = stats.get("multi3", 0) + 1
                body.append("\\end{figure}\n")

            elif btype in ("text", "ref_text", "page_footnote"):
                raw_t = block_text(b).strip()
                if btype == "text" and looks_like_caption(raw_t):
                    # 独立成块的图注（实测"图3.3 SR锁存器原理图"被 VLM 判成正文）
                    # ——小字呈现保住图号体系；不参与跨页合并
                    body.append(f"\n{{\\small {tex_escape_mixed(raw_t)}}}\n")
                    continue
                # —— 图文侧排：与本页右（或左）栏小图配对同行（配对表见页首预处理）——
                im_bi = fig_pair.get(id(b))
                if im_bi is not None and im_bi in skip_bis:
                    im_bi = None  # 图已被图图并排消费 → 退回普通段落
                if im_bi is not None:
                    t = block_text_tex(b).strip()
                    if t:
                        im = blocks[im_bi]
                        gcap, gbb = figure_info(im, blocks, im_bi, cap_map)
                        gname = unique_image_name(gcap, page_idx + 1, im.get("index", 0), used_names)
                        gext = "pdf" if (src.parent / "images" / f"{gname}.pdf").exists() else "png"
                        wf_img = _fig_width_frac(im, gbb, text_span)
                        if wf_img is None:
                            wf_img = 0.30  # 图无有效 bbox → 保守默认宽
                        tb = b["bbox"]
                        # 文字列宽同样换算到版心系（原书文字栏宽占比 ÷ 版心占比）
                        wf_txt = max(0.20, (tb[2] - tb[0]) / text_span)
                        if wf_txt + wf_img > 0.97:
                            k = 0.97 / (wf_txt + wf_img)
                            wf_txt, wf_img = wf_txt * k, wf_img * k
                        # \vspace{0pt}：顶对齐 hack——\includegraphics 的基线在图底边，
                        # 双 minipage[t] 按基线对齐会把图整体抬高一个图高、文字压到图
                        # 中下部（图左侧上方大片空白）；首行插 0pt 垂直空隙使两栏顶边
                        # 对齐（最小复现 _repro_align.tex 已验证，B 写法）
                        mtxt = (f"\\begin{{minipage}}[t]{{{wf_txt:.3f}\\linewidth}}\\vspace{{0pt}}\n"
                                f"{t}\n\\end{{minipage}}")
                        mimg = (f"\\begin{{minipage}}[t]{{{wf_img:.3f}\\linewidth}}\\vspace{{0pt}}\\centering\n"
                                f"\\includegraphics[width=\\linewidth]{{{gname}.{gext}}}\n")
                        if gcap:
                            mimg += f"\n{{\\small {tex_escape_mixed(gcap)}}}\n"
                        mimg += "\\end{minipage}\n"
                        if im["bbox"][0] < tb[0]:  # 原书图在左 → 图 minipage 在前
                            pair_tex = f"\n\\noindent{mimg}\\hfill{mtxt}\n"
                        else:
                            pair_tex = f"\n\\noindent{mtxt}\\hfill{mimg}\n"
                        body.append(pair_tex)
                        skip_bis.add(im_bi)
                        last_para_idx = None  # 配对块不可作为跨页合并的拼接目标
                        stats["text"] += 1
                        stats["image"] += 1
                        stats["text_fig"] = stats.get("text_fig", 0) + 1
                        continue
                t = block_text_tex(b).strip()
                if t:
                    if b.get("_merge_prev") and last_para_idx is not None:
                        # 跨页断段：接回上一段（去段尾换行后拼接）
                        body[last_para_idx] = body[last_para_idx].rstrip("\n") + t + "\n"
                    else:
                        body.append(f"\n{t}\n")
                    last_para_idx = len(body) - 1
                    stats["text"] += 1

            else:  # 未知类型原样保留
                t = block_text_tex(b).strip()
                if t:
                    body.append(f"\n{t}\n")

    title = src.parent.name or "MinerU 转换书稿"
    # 用 replace 而非 % 格式化：PREAMBLE 里 LaTeX 的 % 注释不会被 Python 误解析
    tex = PREAMBLE.replace("@@TITLE@@", tex_escape(title)) + "".join(body) + POSTAMBLE
    out_tex.write_text(tex, encoding="utf-8")
    print(f"完成: {out_tex}")
    print(f"  标题 {stats['title']} | 公式 {stats['equation']} | "
          f"图 {stats['image']}（并排 {stats.get('side_by_side', 0)} 组，"
          f"其中 3+ 图 {stats.get('multi3', 0)} 行 | "
          f"图文侧排 {stats.get('text_fig', 0)} 对） | "
          f"代码 {stats['code']} | 表 {stats['table']} | 段落 {stats['text']}")
    return out_tex


def main():
    ap = argparse.ArgumentParser(description="MinerU JSON → ctexbook LaTeX")
    ap.add_argument("json_path", help="result.json 路径")
    ap.add_argument("-o", "--out", default=None, help="输出 .tex 路径（默认 json 同目录/book.tex）")
    args = ap.parse_args()
    src = Path(args.json_path)
    out = Path(args.out) if args.out else src.parent / "book.tex"
    convert(src, out)


if __name__ == "__main__":
    main()
