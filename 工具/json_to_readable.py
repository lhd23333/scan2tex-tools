# -*- coding: utf-8 -*-
"""MinerU JSON → 阅读友好 Markdown 转换器。

输入: MinerU docvortex.middle JSON（result.json）
输出: 同目录 阅读友好版.md + images/*.png

处理规则:
  page_number / header         → 过滤（书眉页码不进正文）
  paragraph_title + level      → # 级标题 + 自动 TOC
  equation                     → $$ 块（LaTeX 原样，做保守清洗）
  image                        → 调用 mineru read 导出 PNG，图注绑定在图片下
  text                         → 段落

依赖: MinerU 本地服务须在运行（mineru.exe server start）。

用法:
  python 工具/json_to_readable.py <result.json> [--no-images]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

# mineru 可执行文件（mineru read 导出图用）；PATH 里没有 mineru 时用环境变量 MINERU_EXE 指定
MINERU_EXE = os.environ.get("MINERU_EXE", "mineru")
# aside_text 实测是附录页眉变体 / index 是原书手工目录 / footer 是出版社字样
SKIP_TYPES = {"page_number", "header", "aside_text", "index", "footer"}


# ---------- 工具函数 ----------

# SimSun 无字形的特殊字符修正（实测裁定，见 测试记录 02 第 11 条）：
#   下标数字 ₀-₉（U+2080-2089）、上标减号 ⁻、⊖、☐、⌊⌋ —— 中文字体与 LaTeX
#   默认字体都没有字形，必须转命令。数学模式用 LaTeX 命令（clean_equation），
#   文本模式用文本命令/内联公式（json_to_tex.tex_escape）。
#   注：≠≤≥μ∩θ⊕○ 等 SimSun 有字形，走 \xeCJKDeclareCharClass 路由即可。
_SUB_DIGITS = "₀₁₂₃₄₅₆₇₈₉"
MATH_UNICODE_FIX = str.maketrans({
    **{ch: f"_{i}" for i, ch in enumerate(_SUB_DIGITS)},
    "⁻": "^{-}",
    "⊖": r"\ominus ",
    "☐": r"\square ",
    "⌊": r"\lfloor ",
    "⌋": r"\rfloor ",
    # 数学模式内 cmmi10 无字形：Å（"埃"单位，实测 04 缺 2 处）
    "Å": r"\text{\AA} ",
    # 公式内裸 Unicode（文本转入或公式内直接出现）的兜底替换，04 实测集合
    "≈": r"\approx ",
    "∈": r"\in ",
    "∵": r"\because ",
    "β": r"\beta ",
})
TEXT_UNICODE_FIX = {
    **{ch: rf"\textsubscript{{{i}}}" for i, ch in enumerate(_SUB_DIGITS)},
    "⁻": r"\textsuperscript{-}",
    "⊖": r"$\ominus$",      # 成对 $，不破坏 tex_escape_mixed 的分段奇偶
    "☐": r"$\square$",
    "⌊": r"$\lfloor$",
    "⌋": r"$\rfloor$",
}

_LOOP_MIN_LEN = 600  # 短于该长度的内容不做循环检测（正常段落/公式不会这么长）
_LOOP_DROP_BEFORE = 250  # 循环起点在此之前 → 前导不值留，整块判为幻觉


def _loop_chain_len(hay: str, u: str) -> int:
    """单元 u 在 hay 中"跳接链"的最大长度（相邻出现间隔 ≤ 2×len(u) 才算接续）。

    VLM 循环的重复单元几乎连续铺开（间隔≈单元长）；正常文本里凑巧重复
    的短串（公式片段、表格行、伪代码函数名）间隔远大于 2 倍单元长。
    用链长而非裸 count 是 06 二轮修复的核心：首版 count≥3 判据实测把
    44 个块判循环，抽样全是误伤（Strassen 公式、DFT 矩阵、KMP 正文等）。
    """
    L = len(u)
    pos = []
    p = 0
    while True:
        p = hay.find(u, p)
        if p < 0:
            break
        pos.append(p)
        p += 1
    if len(pos) < 2:
        return len(pos)
    best = run = 1
    for k in range(1, len(pos)):
        run = run + 1 if pos[k] - pos[k - 1] <= 2 * L else 1
        best = max(best, run)
    return best


def cut_repetition_loop(s: str) -> str:
    """VLM 重复循环截断（实测 06 缩印本：密集段落/公式处陷入逐字重复，
    单块最多 47709 字符，且把 \\frac/\\left( 折叠重复到括号失配 → 编译连锁错误）。

    06 二轮判据（首版"单元出现≥3次"误伤率极高，详见 _loop_chain_len）：
      跳接链 ≥20（真循环形态），或 出现≥80 次且覆盖全文 ≥30%
      （嵌套/相位漂移使跳接断裂但重复总量极高的循环：实测 p29
       \\frac{1}{2}\\sum 嵌套 chain=1 而 count=344）。
    数字先做等长归一化（\\d→0）再检测：VLM 有"递增编号型"循环
    （实测 p424 'V_{1}:E，设 V_{2}:E…V_{745}'），字面单元永不重复，
    归一化后才可见；等长替换保证截断位置可直接映射回原文。
    6 采样点（0.15 起——0.25 起的旧采样对"块首即循环"的块全盲）×
    单元长 16..64；命中截到最早首现处；循环起点 <250 或截后 <200 字符
    判整块幻觉返回空串；迭代 5 轮收敛。set(u)<4 的填充串跳过。
    """
    if len(s) < _LOOP_MIN_LEN:
        # 主循环判据对短文本不适用，但 two-hit 长片段重复截断仍要跑
        # （06 p137 实测重复块仅 ~80 字符）
        return cut_two_hit_repeat(s)
    cur = s
    norm = re.sub(r"\d", "0", cur)
    cut_made = False
    for _ in range(5):
        n = len(cur)
        best = None
        for frac in (0.15, 0.30, 0.45, 0.60, 0.75, 0.90):
            i = int(n * frac)
            for L in (64, 48, 32, 24, 16):
                if i + L > n:
                    continue
                u = norm[i:i + L]
                if len(set(u)) < 4:
                    continue
                c = norm.count(u)
                if c < 3:
                    continue
                if _loop_chain_len(norm, u) >= 20 or (c >= 80 and c * L >= 0.30 * n):
                    first = norm.find(u)
                    if best is None or first < best:
                        best = first
        if best is None:
            break
        cut_made = True
        if best < _LOOP_DROP_BEFORE:
            cur = ""  # 循环从块首附近开始 → 整块幻觉
            break
        cur = cur[:best].rstrip()
        norm = norm[:best]  # 等长归一化，同步截断保持位置对应
        if len(cur) < _LOOP_MIN_LEN:
            break
    if cut_made and len(cur) < 200:
        return ""
    return cut_two_hit_repeat(cur)


# ---------- P1 质量评估驱动的工具修复（2026-10-08） ----------

_EQ_REF_TAGS: set[str] = set()

_EQ_REF_RE = re.compile(
    r"(?P<pre>式\s*[（(])\s*(?P<a>\d{1,2})\s*[,，]\s?(?P<b>\d{1,3})\s*(?P<bc>[)）])"
)


def set_tag_set(ids) -> None:
    """登记全书公式编号集合（"A.B" 形态），供 fix_eq_refs 交叉验证。"""
    global _EQ_REF_TAGS
    _EQ_REF_TAGS = set(ids)


def collect_tag_ids(pages: list[dict]) -> set[str]:
    """扫描全书 equation 块的 \\tag{A.B} → {"A.B"} 集合。

    equation 块经 clean_equation（含 "\\tag{9, 59}"→"\\tag{9.59}" 逗号
    误识形态的还原，与最终成书编号口径一致）。
    """
    ids: set[str] = set()
    for pg in pages:
        for b in pg.get("blocks", []):
            t = block_text(b)
            if b.get("type") == "equation":
                t = clean_equation(t)
            for m in re.finditer(r"\\tag\s*\{(\d+)\.(\d+)\}", t):
                ids.add(f"{m.group(1)}.{m.group(2)}")
    return ids


def fix_eq_refs(s: str) -> str:
    """正文公式编号引用逗号化还原："式(9, 59)" → "式(9.59)"。

    MinerU 把印刷的 "9.59" 误读为 "9, 59"（03 评估实测 6 页）。只改
    "式(" 前缀 + 编号在全书 tag 表中真实存在的组合——全书 (N, M)
    形态 129 处绝大多数是函数调用/坐标（f(5, 3)、digitalWrite(2, 1)），
    双重条件以免误伤。半角/全角括号与逗号均覆盖，括号风格原样保留。
    """
    if not _EQ_REF_TAGS or "式" not in s:
        return s

    def rep(m: re.Match) -> str:
        key = f"{m.group('a')}.{m.group('b')}"
        if key not in _EQ_REF_TAGS:
            return m.group(0)
        return f"{m.group('pre')}{key}{m.group('bc')}"

    return _EQ_REF_RE.sub(rep, s)


# 列表项/题号开头的块：套题与列表的公共句式模板会"合法地"产生尾部字面重复
# （实测 02 p40 习题"1.41…/1.42…"同问法、05 p104"(c)…"证明模板被误截），
# 此类块不适用 two-hit 截断
_LIST_HEAD_RE = re.compile(r"^\s*(?:第\s*\d+\s*[章节题]|[（(]?[a-zA-Z0-9][)）.．、])")


def cut_two_hit_repeat(s: str) -> str:
    """块内长片段两次重复截断（P1 实测形态，04/06）。

    06 p137 block#6："…如果 n=2 是偶数,则以下从,其它使该项也都会出现两次。
    如果 n 是奇数,除了 f(n/2) 是偶数,则以下从,其它使该项也都会出现两次。"
    ——VLM 幻觉改写下整句重复。主循环判据管"海量循环"（count≥3），
    本判据补 count==2 的长片段尾重复：
      采样点上的 ≥16 字符片段满足——字符种 ≥8、汉字 ≥8（排除专名/
      公式标识符的合法重复）、**字面**非重叠出现恰好 2 次、间隔 ≥10、
      且第二处延伸至块尾附近（剩余 ≤L+12）→ 截断。
      用字面匹配而非数字归一化：归一化会把并列句式中仅下标/编号不同
      的内容抹成相同（实测 03 p136 "(a)…R_1…/(b)…R_4…" 被误截）。
      截断点精化：从采样点向前找最近标点，验证其后仍是重复段（与重复
      位移 d 前的对应位置字面相同）则截到标点后——保留重复段之前的
      独有内容（如 06 p137 "如果 n 是奇数, 除了 f(n/2) 是偶数, "）；
      验证失败退回采样点+标点回退（保守）。
    """
    if len(s) < 70:
        return s
    if _LIST_HEAD_RE.match(s):
        return s  # 列表项/题号块（套题模板重复，非幻觉，见 _LIST_HEAD_RE）
    n = len(s)
    for frac in (0.45, 0.60, 0.75):
        i = int(n * frac)
        for L in (48, 36, 24, 16):
            if i + L > n or n - i > L + 12:
                continue
            u = s[i:i + L]
            if len(set(u)) < 8:
                continue
            if sum(1 for ch in u if "一" <= ch <= "鿿") < 8:
                continue
            first = s.find(u)
            if first < 0 or i - first < L + 10:
                continue
            if s.find(u, first + 1) != i:
                continue
            if s.find(u, i + 1) != -1:
                continue
            d = i - first
            # 候选截断点：i 后向 16 字符（重复段内的第一个边界，如 06 案例
            # 的"是偶数,"逗号在采样点后 1 字符）+ i 前向标点（下限取重复
            # 位移 d——小于 d 的位置无对应段可做验证）
            cands = list(range(i, min(i + 17, n)))
            cands += list(range(i - 1, max(39, d, i - 40) - 1, -1))
            cut = None
            for p in cands:
                if s[p] not in "。！？；，,":
                    continue
                t2 = p + 1
                if (t2 - d >= 0 and t2 + 16 <= n
                        and s[t2:t2 + 16] == s[t2 - d:t2 - d + 16]
                        and t2 >= 40):
                    cut = s[:t2].rstrip()
                    break
            if cut is None or len(cut) < 40:
                cut = s[:i].rstrip()
                p = max(cut.rfind(ch) for ch in "。！？；，,")
                if p >= 40:
                    cut = cut[:p + 1]
            if len(cut) >= 40:
                return cut
    return s


def _norm_index_of(s: str, k: int) -> int:
    """[^汉字/字母/数字] 被去除的归一化串中第 k 个字符在原文 s 的位置。"""
    cnt = 0
    for idx, ch in enumerate(s):
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]", ch):
            if cnt == k:
                return idx
            cnt += 1
    return len(s)


def merge_overlapping_neighbors(pages: list[dict], log: list | None = None) -> int:
    """页内相邻 text 块重叠合并（VLM 块切分缺陷修复，P1 实测 04 p178）。

    形态：同段落被切成两块——前块在段中丢字，后块从丢字处起完整重复到
    段尾（"半截 + 全量重复"）。合并：前块[:后块前缀在前块中的匹配点] +
    后块，保留完整版、衔接处去重。
    判据（双条件，保守）：① 后块归一化开头 20 字符在前块中存在（匹配点
    ≥10）；② 前块归一化结尾包含后块归一化结尾 14 字符（同尾）。两块
    归一化长度均 ≥80 才处理。命中合并后物理删除后块。
    log 非空时逐条追加 (page_idx, 后块 index, 后块开头 40 字) 供验证；
    返回合并处数。
    """
    merged = 0
    for pg in pages:
        blocks = pg.get("blocks", [])
        i = 0
        while i < len(blocks) - 1:
            b1 = blocks[i]
            if b1.get("type") != "text":
                i += 1
                continue
            t1 = block_text(b1)
            n1 = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t1)
            if len(n1) < 80:
                i += 1
                continue
            hit = False
            # \u5141\u8bb8\u8de8 1 \u4e2a\u4e2d\u95f4\u5757\uff08\u5b9e\u6d4b 04 p178\uff1a\u6b63\u6587 7 / \u56fe\u6ce8 8 / \u91cd\u590d 9\uff09
            for j in (i + 1, i + 2):
                if j >= len(blocks):
                    break
                b2 = blocks[j]
                if b2.get("type") != "text" or b2.get("_merge_prev"):
                    continue
                t2 = block_text(b2)
                n2 = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t2)
                if len(n2) < 80:
                    continue
                pos = n1.find(n2[:20])
                if pos >= 10 and n1.endswith(n2[-14:]):
                    cut = _norm_index_of(t1, pos)
                    b1["content"] = t1[:cut] + t2
                    del blocks[j]
                    merged += 1
                    if log is not None:
                        log.append((pg.get("page_idx", 0), b2.get("index"),
                                    t2[:40]))
                    hit = True
                    break
            if hit:
                continue
            i += 1
    return merged


# \text/\textnormal{} 内只含单个数学命令（如 \perp）→ 用 $..$ 包住。
# 实测 06 p54 一处 \textnormal {\perp}：文本模式调用数学命令 → "Missing $"
# 连锁 13 条 error + Display math/eqno 报错。
_MATHCMD_IN_TEXT_RE = re.compile(
    r"(\\(?:text|textnormal|textrm|textup|textit|textsf|texttt|mbox)\s*)"
    r"\{\s*(\\(?:perp|parallel|sim|simeq|approx|cong|equiv|leq|geq|neq|in|notin"
    r"|subset|supset|subseteq|supseteq|cup|cap|times|cdot|pm|mp|ast|star|circ"
    r"|bullet|oplus|ominus|otimes|oslash|sum|prod|int|oint|infty|to|gets"
    r"|rightarrow|leftarrow|leftrightarrow|Rightarrow|Leftarrow|Leftrightarrow"
    r"|vdash|dashv|models|angle|triangle|square|blacksquare|checkmark|hbar|ell"
    r"|wp|Re|Im|aleph|partial|nabla|forall|exists|neg|land|lor"
    r"|emptyset|varnothing)\s*)\}"
)


def _fix_row_break_brackets(s: str) -> str:
    r"""\\ 后紧跟 [ 会被 TeX 当成行距参数（"\\ [ n / b ]" → "Illegal unit of
    measure"/缺数字/\ifdim 连锁，实测 06 三处、每组 6 条错误）：插入 {} 隔断；
    真身是尺寸参数的（\\[2ex]、\\[-3pt]）保持原样。

    尺寸判定用 lookahead（[ 后须是 数字+单位+ ]），不用"配对括号全匹配"——
    嵌套方括号（实测 "\\ [ [ n / b ]"）用 [^\[\]]* 匹配不到，会漏修。
    """
    if "\\\\" not in s:
        return s
    out, pos = [], 0
    for m in re.finditer(r"\\\\\s*\[", s):
        br = m.end() - 1  # '[' 的位置
        out.append(s[pos:br])
        if not re.match(r"\s*(?:[+-]?\d+(?:\.\d+)?\s*)?(?:pt|ex|em|cm|mm|in|sp)\s*\]",
                        s[br + 1:]):
            out.append("{}")
        out.append("[")
        pos = m.end()
    out.append(s[pos:])
    return "".join(out)


def _expand_array_cols(s: str) -> str:
    r"""array 声明列数少于实际使用列数 → 按实际最大列数补足声明。
    实测 06 六处（矩阵行被 VLM 数出多余项）引发 "Extra alignment tab" 连锁 23 条。
    非嵌套优先；嵌套 array 递归处理。"""
    if r"\begin{array}" not in s:
        return s
    res, pos = [], 0
    while True:
        i = s.find(r"\begin{array}", pos)
        if i < 0:
            res.append(s[pos:])
            break
        if s[i + 13:i + 14] != "{":
            res.append(s[pos:i + 13])
            pos = i + 13
            continue
        j = s.find("}", i + 13)
        k = s.find(r"\end{array}", j + 1) if j >= 0 else -1
        if j < 0 or k < 0:
            res.append(s[pos:])
            break
        spec = s[i + 14:j]
        body = s[j + 1:k]
        base = len(re.findall(r"[lcrpmb]", spec))
        maxc = 0
        for row in re.split(r"\\\\", body):
            t = row.strip()
            if not t or t.startswith(r"\hline") or t.startswith(r"\cline"):
                continue
            maxc = max(maxc, row.count("&") + 1)
        letter = (re.findall(r"[lcrpmb]", spec) or ["c"])[0]
        newspec = spec if maxc <= base else " ".join([letter] * maxc)
        res.append(s[pos:i])
        res.append(r"\begin{array}{" + newspec + "}")
        inner = body
        res.append(_expand_array_cols(inner) if r"\begin{array}" in inner else inner)
        res.append(r"\end{array}")
        pos = k + len(r"\end{array}")
    return "".join(res)


def clean_equation(s: str) -> str:
    """保守清洗：合并被拆开的数字、修 \\text { 为 \\text{、数学模式 Unicode 修正。

    另修 MinerU 竖式加法识别问题：`& \\hline`（\\hline 落在 array 单元格中间）
    是非法用法，实测在整书里引发 48 个连锁编译错误（图5.26 加法竖式）。
    \\hline 在行首（`\\\\ \\hline`）是合法的，不动。
    另修相邻重复 \\tag（如 `\\tag{12}\\tag{12.155}`），实测引发 amsmath
    "Multiple \\tag" 编译错误——只保留最后一个（与上下文编号体系吻合者）。

    06 缩印本新增四类修复（详见各辅助函数注释）：循环截断 cut_repetition_loop、
    \\llbracket/\\rrbracket（stmaryrd 命令）→[]、\\ 后接 [ 隔断、array 列数补足、
    \\text{} 内单个数学命令包 $..$。
    """
    # 先截断 VLM 重复循环（可能是几万字符的块，先切掉再跑后续正则）
    s = cut_repetition_loop(s)
    s = s.replace(r"\llbracket", "[").replace(r"\rrbracket", "]")
    s = _fix_row_break_brackets(s)
    s = _expand_array_cols(s)
    if _MATHCMD_IN_TEXT_RE.search(s):
        s = _MATHCMD_IN_TEXT_RE.sub(r"\1{$\2$}", s)
    for _ in range(3):
        s = re.sub(r"(?<=\d) +(?=\d)", "", s)
    s = re.sub(r"\\(text|mathrm|mathbf)\s+\{", r"\\\1{", s)
    s = re.sub(r"&\s*\\hline\s*", "& ", s)
    # 相邻重复 \tag：连续多个只留最后一个（单 tag 原样通过）
    s = re.sub(
        r"(?:\\tag\s*\{[^{}]*\}\s*)+",
        lambda m: re.findall(r"\\tag\s*\{[^{}]*\}", m.group(0))[-1] + " ",
        s,
    )
    # \tag 编号内逗号→点（实测 03 全书 169 处：MinerU 把 "2.19" 误识别成 "2, 19"）
    s = re.sub(r"\\tag\{(\d+)\s*,\s*(\d+)\}", r"\\tag{\1.\2}", s)
    # VLM 造的非 LaTeX 命令：\bmodulo（"x b modulo N" 拼接，实测 05 整书 error 1：
    # ! Undefined control sequence）→ 修正为 \bmod
    s = re.sub(r"\\bmodulo(?![a-zA-Z])", r"\\bmod ", s)
    # 数学模式内的裸 \AA（文本命令，\r 在数学模式 invalid → cmmi10 无 Å 字形缺字，
    # 实测 04 p119 "$20\AA$"）：包 \text 恢复文本模式；已包 \text{\AA} 的不重复处理
    s = s.replace(r"\text{\AA}", "@@AA_PROTECT@@")
    s = s.replace(r"\AA", r"\text{\AA}")
    s = s.replace("@@AA_PROTECT@@", r"\text{\AA}")
    # \text{} 内的裸 & 一律转义（\text 参数里 & 是非法对齐符，实测报
    # "Misplaced alignment tab"——伪代码方程 p305 的 if 条件位运算 &）
    s = re.sub(r"\\text\{([^{}]*)\}",
               lambda m: "\\text{" + re.sub(r"(?<!\\)&", r"\\&", m.group(1)) + "}", s)
    s = s.translate(MATH_UNICODE_FIX)
    return s


def block_text(block: dict) -> str:
    """递归提取块内文本（content 可能是 str / list[dict] 嵌套）。

    关键：equation_inline 子块要还原成 $...$（实测 text 块内混排 66 个行内公式，
    直接拼接会丢 $ 标记导致 LaTeX 裸露在正文里）。
    """
    c = block.get("content")
    if isinstance(c, str):
        return fix_eq_refs(cut_repetition_loop(c))
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "equation_inline":
                    out.append(f"${clean_equation(part.get('content', ''))}$")
                elif ptype == "text":
                    _pc = part.get("content", "")
                    # 嵌套 text part（content 为 list/dict，如 image 块题注结构）
                    out.append(block_text(part) if isinstance(_pc, (list, dict)) else _pc)
                elif isinstance(part.get("content"), (list, str)):
                    out.append(block_text(part))
            elif isinstance(part, str):
                out.append(part)
        return fix_eq_refs(cut_repetition_loop("".join(out)))
    return ""


def title_level(text: str, raw_level) -> int:
    """标题层级修正。VLM 的 level 全部给 2（实测不可靠），按编号启发式重排：
      '第X章'        → 1
      '附录 A xxx'   → 1（附录与章同级）
      '3.5.4 xxx'    → 3（三级编号，如"2.8.1 多路选择器"）
      '3.5 xxx'      → 2（二级编号）
      '1. xxx'       → 3（单级编号小节，如"1. 亚稳态"）
      其余（无编号）  → 原始 level + 1（降一级。无编号加粗标题实测多为
                      节内小标题/栏目名，比 VLM 给的位置再低一档；真章标题
                      由 resolve_chapter_titles 配对短路升 chapter，不经此路）
    """
    t = text.strip()
    if re.match(r"^第\s*\d+\s*(章|部分|篇)", t):
        return 1
    if re.match(r"^附录\s*[A-Z0-9]", t):
        return 1
    if re.match(r"^\d+\.\d+\.\d+", t):   # 2.8.1
        return 3
    if re.match(r"^\d+\.\d+", t):        # 3.5
        return 2
    if re.match(r"^\d+\.\s*\S", t):      # 1. 亚稳态
        return 3
    try:
        return max(1, min(int(raw_level or 2) + 1, 6))
    except (TypeError, ValueError):
        return 3


# —— 标题误标检降（VLM 把"加粗正文/习题题面/页眉残片"误标为 paragraph_title）——
# 判据开发与全量回归：5 本书 2307 个 paragraph_title 命中 52 条，逐条目检全为真误标
# （R1-R4 27 条 + R5 纯章号残片 25 条：03 p665-671 索引、04 各章首页书眉）；
# 编号型标题 1163 个仅命中 10 条（全部是"编号+完整句子"的题面/列表句）。
# 误标在 book.tex 里会排成 \section*/chapter（"第N章"残片走 title_level→1）并进
# 目录，故命中即降级为加粗正文。
_TITLE_NUM_PREFIX_RE = re.compile(
    r"^\s*(\d+\.\d+\.\d+|\d+\.\d+(?![.\d])|\d+\.\s|第\s*\d+\s*[章节篇部]"
    r"|[A-Z]\s*\.\s*\d|附录\s*[A-Z0-9])")


def looks_like_body_title(text: str, bbox=None) -> bool:
    """段落标题块是否为 VLM 误标的正文（命中即降级，不排标题层级、不进目录）。

    判据（任一命中）：
      R1 含句末标点（。！？及半角 !?）——真标题是名词短语，不以句子收尾；
         【编号不豁免】误标主力恰是"编号+完整句"（习题题面"6.22 请将…。"）
      R2 超长且像句子：≥40 字含逗号，或 ≥60 字；编号开头豁免——
         保护"7.6.1 饱和区域中 v_IN 与 v_OUT 的关系"类公式展开的长标题
      R3 顶部超长残片：bbox 页顶 y0<0.065 且 ≥30 字（整条页眉的几何签名）
      R4 列表项：单级编号 "N. " 开头且以逗号/顿号/"以及"结尾（有序列表被拆标）
      R5 纯章号残片："第N章"（无章名）——章首页书眉或书末索引条目被拆标成
         标题块（03 p665-671 索引、04 各章首页书眉实测）；书内正式章标题
         在 JSON 里为 doc_title，或由 resolve_chapter_titles 配对升章级
    """
    t = text.strip()
    if not t:
        return False
    if re.search(r"[。！？!?]", t):                        # R1
        return True
    if not _TITLE_NUM_PREFIX_RE.match(t):                  # R2（编号开头豁免）
        if len(t) >= 60 or (len(t) >= 40 and "，" in t):
            return True
    if bbox:                                               # R3
        try:
            y0 = float(bbox[1])
        except (TypeError, ValueError, IndexError):
            y0 = 1.0
        if y0 < 0.065 and len(t) >= 30:
            return True
    if (re.match(r"^\s*\d+\s*[.、]\s", t)                   # R4
            and re.search(r"(?:[，、]|以及)\s*$", t)):
        return True
    if re.fullmatch(r"第\s*\d+\s*章\s*[:：]?", t):          # R5
        return True
    return False


# —— 章标题重组（"章号被页眉吞掉、章名孤留标题块"的配对修复）——
# 06 实测：章首页书眉 = "第N章 [+书名]"（章号后不接中文），章名大字在同页
# 标题块（y0≈0.10-0.15）；正文书眉 = "第N章 章名"（章号后接中文，非章首页）。
# 配对结果用于把章名标题升为 chapter 级并补回章号（"第 N 章 章名"）。
_CH_HEADER_RE = re.compile(r"第\s*(\d+)\s*章\s*[:：]?\s*(.*)$")
_CH_SKIP_TITLES = {"练习", "思考题", "本章注记", "习题", "问题", "小结", "分析",
                   "本章概要", "本章概述", "参考文献", "索引", "附录注记"}


def chapter_display_title(text: str, n: int) -> str:
    """章标题展示文本：已含"第N章"前缀的规范化，否则合成"第 N 章 "前缀。"""
    m = re.match(r"^第\s*\d+\s*章\s*[:：]?\s*(.*)$", text.strip())
    rest = m.group(1).strip() if m else text.strip()
    return f"第 {n} 章 {rest}".rstrip()


def build_chapter_name_map(pages: list) -> dict[int, str]:
    """正文书眉"第N章 章名" → {章号: 章名}；书眉文本复现 ≥3 次者为正文书眉。

    06 实测：章首页书眉"第N章 Introduction…"（书名款，全书仅 1 次）自然排除；
    部分书眉（"• 第七部分…"）不含"第N章"亦跳过。章名去空格归一（"NP 完全性"
    →"NP完全性"），尾部书眉装饰点（"B树·"）剥除。同章号多候选取复现最高者。
    """
    hcnt: dict[str, int] = {}
    for pg in pages:
        for b in pg.get("blocks", []):
            if b.get("type") in ("header", "page_header", "footer", "page_footer"):
                t = block_text(b).strip()
                if t:
                    hcnt[t] = hcnt.get(t, 0) + 1
    votes: dict[int, dict[str, int]] = {}
    for pg in pages:
        for b in pg.get("blocks", []):
            if b.get("type") not in ("header", "page_header", "footer", "page_footer"):
                continue
            t = block_text(b).strip()
            if hcnt.get(t, 0) < 3:
                continue
            m = _CH_HEADER_RE.match(t)
            if not m:
                continue
            x = re.sub(r"\s+", "", m.group(2)).strip("·•").strip()
            if not x or x.startswith("Introduction"):
                continue
            n = int(m.group(1))
            votes.setdefault(n, {})
            votes[n][x] = votes[n].get(x, 0) + 1
    return {n: max(v, key=v.get) for n, v in votes.items()}


def resolve_chapter_titles(pages: list) -> dict[tuple[int, int], tuple[int, str]]:
    """章首页"章号+章名"配对 → {(page_idx, 块 index): (章号, 章名标题文本)}。

    两层证据：
    ① b 表匹配——paragraph_title 文本（去空格）等于正文书眉章名表某值且
       y0<0.35 → 该页是此章章首页（06 实测：第 12 章"二叉搜索树"章首页
       书眉无章号，靠正文书眉表回配）。
    ② a 款兜底——章首页书眉"第N章 [+书名]"（低频）页内 y0 最小的合资格候选
       （覆盖无正文书眉的书，如 03）。
    每章号只保留一个：b 表优先（取页码最小页），a 款次之（取 y0 最小）。
    候选合格条件：短（≤16 字）、非编号型/附录型开头、非栏目名、
    非正文误标（过 looks_like_body_title）、页面上部（y0<0.35，容"章从页中部起"）。
    """
    chmap = build_chapter_name_map(pages)
    name2no = {v: n for n, v in chmap.items()}

    # 章名已被 doc_title 大标题块覆盖的章号：其 chapter 级已成立，不再做 b 表
    # 匹配/兜底（否则章内同名小节标题会被误升，如 06 p643"NP 完全性"）
    covered: set[int] = set()
    for pg in pages:
        for b in pg.get("blocks", []):
            if b.get("type") == "doc_title":
                n = name2no.get(re.sub(r"\s+", "", block_text(b).strip()))
                if n is not None:
                    covered.add(n)

    # ① b 表匹配（标题剥"第N章"前缀后归一——04 第 6 章章名块实测
    #    "第6章MOS晶体管的物理机理"把章号一起识别进了标题）
    pairs_b: dict[int, tuple[int, str, float, int]] = {}  # 章号 -> (页, 原文, y0, 块 index)
    for pg in pages:
        pi = pg.get("page_idx", 0)
        for b in pg.get("blocks", []):
            if b.get("type") != "paragraph_title":
                continue
            t = block_text(b).strip()
            if not t or b.get("index") is None:
                continue
            key = re.sub(r"^第\d+章", "", re.sub(r"\s+", "", t))
            n = name2no.get(key)
            if n is None or n in covered:
                continue
            y0 = (b.get("bbox") or [0, 1.0])[1]
            if y0 >= 0.35:
                continue
            cur = pairs_b.get(n)
            if cur is None or pi < cur[0]:
                pairs_b[n] = (pi, t, y0, b["index"])

    # ② a 款兜底（排除已被 ① 覆盖的章号）
    hcnt: dict[str, int] = {}
    for pg in pages:
        for b in pg.get("blocks", []):
            if b.get("type") in ("header", "page_header", "footer", "page_footer"):
                t = block_text(b).strip()
                if t:
                    hcnt[t] = hcnt.get(t, 0) + 1
    pairs_a: dict[int, tuple[int, str, float, int]] = {}
    for pg in pages:
        pi = pg.get("page_idx", 0)
        n_ch = None
        for b in pg.get("blocks", []):
            if b.get("type") not in ("header", "page_header", "footer", "page_footer"):
                continue
            t = block_text(b).strip()
            m = _CH_HEADER_RE.match(t)
            if not m or hcnt[t] >= 3:
                continue          # 正文书眉（高频复现）→ 非章首页
            tail = m.group(2).strip()
            if tail[:1] and re.match(r"[一-鿿]", tail[:1]):
                continue          # "第N章 章名" = 正文书眉，非章首页
            n_ch = int(m.group(1))
            break
        if n_ch is None or n_ch in pairs_b or n_ch in covered:
            continue
        cands = []
        for b in pg.get("blocks", []):
            if b.get("type") != "paragraph_title":
                continue
            t = block_text(b).strip()
            if not t or len(t) > 16 or t in _CH_SKIP_TITLES:
                continue
            if re.match(r"^(第|\d|附录|\*)", t):
                continue
            if looks_like_body_title(t, b.get("bbox")) or b.get("index") is None:
                continue
            y0 = (b.get("bbox") or [0, 1.0])[1]
            cands.append((y0, t, b["index"]))
        if not cands:
            continue
        y0, t, idx = min(cands)
        if y0 < 0.35:
            cur = pairs_a.get(n_ch)
            if cur is None or y0 < cur[2]:
                pairs_a[n_ch] = (pi, t, y0, idx)

    # 输出键 (page_idx, 块 index)：调用侧按块精确命中，无需文本再匹配
    out: dict[tuple[int, int], tuple[int, str]] = {}
    for n, (pi, t, _y0, idx) in pairs_b.items():
        out[(pi, idx)] = (n, t)
    for n, (pi, t, _y0, idx) in pairs_a.items():
        out[(pi, idx)] = (n, t)
    return out


def code_text(block: dict) -> str:
    """code 块 → 纯代码文本（取 code_body 子块的 content）。

    实测格式：content 首行是语言名（'SystemVerilog\\n\\nmodule ...'），
    与书里的代码列表标题一致，保留。
    """
    for part in block.get("content", []):
        if isinstance(part, dict) and part.get("type") == "code_body":
            return (part.get("content") or "").rstrip()
    return ""


class _TableHTMLParser(HTMLParser):
    """解析 MinerU 表格 HTML → rows。

    实测标签集：<table><tr><td> + colspan/rowspan 属性 + <eq>行内公式</eq>
    （388 个）+ 极少数 <img>。产出 [[{"text","colspan","rowspan"}, ...], ...]，
    <eq> 内容转 $...$ 混入 cell 文本（转义/提取由调用方按 $ 分段处理）。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self.tail = ""  # </table> 之后的残留文本（实测图注常粘在这）
        self._row = None
        self._cell = None
        self._in_table = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            cell = {"text": "", "colspan": 1, "rowspan": 1}
            for k, v in attrs:
                if k in ("colspan", "rowspan"):
                    try:
                        cell[k] = max(1, int(v))
                    except (TypeError, ValueError):
                        pass
            self._cell = cell
        elif tag == "eq" and self._cell is not None:
            self._cell["text"] += "$"

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "eq" and self._cell is not None:
            self._cell["text"] += "$"

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
        elif not self._in_table:
            self.tail += data


def parse_table_html(block: dict) -> tuple[str, list[list[dict]]] | None:
    """提取 table 块的 (caption, rows)。caption 来自内嵌 table_caption 子块。"""
    cap = ""
    rows = None
    for part in block.get("content", []):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "table_caption":
            cap = block_text(part).strip()
        elif part.get("type") == "table_body":
            parser = _TableHTMLParser()
            parser.feed(part.get("content") or "")
            if parser.rows:
                rows = parser.rows
            # 图注常粘在 </table> 之后（实测"</table>图1.11 数值线和4位二进制数编码"，
            # 这类"表图"被 VLM 判成 table，图号标题藏在 HTML 尾部）
            if not cap:
                m = re.search(r"图\s*[0-9A-Z]+[-.．]\d+[^\n]*", parser.tail)
                if m:
                    cap = re.sub(r"\s+", " ", m.group(0)).strip()
    return (cap, rows) if rows else None


_pdf_cache: dict[str, object] = {}


def auto_crop_zoom(pdf_path: Path) -> float:
    """按首个大嵌入图估算原图 DPI → 裁剪 zoom（像素/点 = DPI/72）。

    实测 2026-10-07：本地裁剪一次 0.04s，mineru read 一次 6.58s（服务忙时更慢），
    且裁剪分辨率更高（319px → 477px 宽）——内容一致（_实验裁剪read 对比）。
    """
    try:
        import pymupdf
        key = str(pdf_path)
        if key not in _pdf_cache:
            _pdf_cache[key] = pymupdf.open(key)
        for page in _pdf_cache[key]:
            for img in page.get_images(full=True):
                if img[2] > 800:  # 跳过装饰小图
                    return max(1.0, round(img[2] / page.rect.width, 2))
    except Exception:  # noqa: BLE001
        pass
    return 3.0


def crop_from_pdf(pdf_path: Path, page_idx: int, bbox: list[float], out_png: Path,
                  zoom: float = 3.0) -> bool:
    """从原 PDF 按归一化 bbox 裁剪渲染 PNG。

    chart 块的位图不在 doclib（实测 chart_body.content 全为空字符串），
    只能回原 PDF 裁剪；zoom=3 对 198 DPI 扫描页约等于 216 DPI 输出。
    """
    if out_png.exists():
        return True
    try:
        import pymupdf
        key = str(pdf_path)
        if key not in _pdf_cache:
            _pdf_cache[key] = pymupdf.open(key)
        doc = _pdf_cache[key]
        page = doc[page_idx]
        w, h = page.rect.width, page.rect.height
        clip = pymupdf.Rect(bbox[0] * w, bbox[1] * h, bbox[2] * w, bbox[3] * h)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
        pix.save(str(out_png))
        return out_png.exists()
    except Exception as e:  # noqa: BLE001 - 裁剪失败不应中断整书转换
        print(f"  [裁剪失败] p{page_idx + 1} {out_png.name}: {e}")
        return False


def image_caption(block: dict) -> str:
    """取图注。规则（实测教训）：优先取 image_body 之后的 caption
    （VLM 遇并排双图时会把两条 caption 塞进同一块：caption_a + body + caption_b，
    紧跟 body 的那条才是本图的图注）；否则取 body 之前的最后一条。"""
    parts = block.get("content", [])
    body_pos = next((i for i, p in enumerate(parts)
                     if isinstance(p, dict) and p.get("type") == "image_body"), None)
    if body_pos is not None:
        for p in parts[body_pos + 1:]:
            if isinstance(p, dict) and p.get("type") == "image_caption":
                return block_text(p)
        for p in reversed(parts[:body_pos]):
            if isinstance(p, dict) and p.get("type") == "image_caption":
                return block_text(p)
    # 无 body 信息时退回第一条
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "image_caption":
            return block_text(part)
    return ""


def caption_filename(caption: str, page_no: int, index: int) -> str:
    """从图注提取 '图X.Y'/'图A.10' 做文件名，失败回退 p{页}-b{块}。

    注意含附录字母章号（A/B/C）——早期版本只认数字章号，导致附录图全部
    用回退名（已导出文件由 tools/rename 映射纠正）。
    """
    m = re.search(r"图\s*([0-9A-Z]+[-.][0-9]+)", caption)
    if m:
        return "图" + m.group(1).replace("-", ".")
    return f"p{page_no}-b{index}"


# "图X.Y" 后跟引用动词 = 正文引用（"图 2.3 所示的组合电路有…"），不是图注
_CAPTION_REF_RE = re.compile(r"^图\s*[0-9A-Z]+[-.．]\d+[a-z]?\s*(所示|给出|显示|为|由|是|中|含|说明)")


def looks_like_caption(t: str) -> bool:
    """短标题式图注（"图2.3 组合逻辑电路"）与正文引用/长段的区分。

    实测教训（整书 02）：容错认领若不设防，会把"图 2.5 所示的组合电路有
    多个输出，称为全加器…"这类正文段整段吞成图注。
    """
    return (bool(re.match(r"^图\s*[0-9A-Z]+[-.．]\d+", t)) and len(t) <= 90
            and not _CAPTION_REF_RE.match(t))


def unique_image_name(cap: str, page_no: int, index: int, used: set) -> str:
    """图文件名（readable 与 tex 共用）：重名（实测'图9.36'出现两次）加块号后缀。"""
    name = caption_filename(cap, page_no, index)
    if name in used:
        name = f"{name}-b{index}"
    used.add(name)
    return name


def figure_info(b: dict, blocks: list, bi: int, cap_map: dict) -> tuple[str, list | None]:
    """image/chart 块的 caption 与（chart 的）裁剪 bbox。

    readable 与 tex 共用同一推导，保证两边图片文件名完全一致。
    chart 的 caption 优先取含"图X.Y"的一条（子图标注如 "a)" 不参与命名）；
    caption 缺失时容错看下一块是否是以"图X.Y"开头的独立文本块。
    """
    bbox = None
    if b.get("type") == "chart":
        parts = b.get("content", [])
        caps = [block_text(x).strip() for x in parts
                if isinstance(x, dict) and x.get("type") == "chart_caption"]
        cap = next((c for c in caps if re.match(r"^图\s*\d", c)),
                   caps[0] if caps else "")
        body_part = next((x for x in parts
                          if isinstance(x, dict) and x.get("type") == "chart_body"), None)
        bbox = (body_part or {}).get("bbox")
    else:
        cap = cap_map.get(b.get("index", 0), "")
    if not cap and bi + 1 < len(blocks):
        nxt = blocks[bi + 1]
        if nxt.get("type") == "text":
            t = block_text(nxt).strip()
            if looks_like_caption(t):
                cap = t
    return cap, bbox


def find_doc_ref(md_path: Path) -> tuple[str, str] | None:
    """从同目录的 mineru markdown 产物里提取 doc hash 与 tier。"""
    if not md_path.exists():
        return None
    m = re.search(r"doc:([0-9a-f]+)/tier:(\w+)", md_path.read_text(encoding="utf-8"))
    return (m.group(1), m.group(2)) if m else None


def export_image(doc_hash: str, tier: str, page_no: int, index: int, out_path: Path,
                 force: bool = False) -> bool:
    """用 mineru read 导出图片块。

    注意: read 的块编号 = JSON index + 1（已用 4 个图片块实测配对）。
    先按 index+1 尝试，失败回退 index（防御 doclib 编号体系变化）。
    """
    if out_path.exists() and not force:
        return True  # 幂等：已导出过
    for block_no in (index + 1, index):
        locator = f"doc:{doc_hash}/tier:{tier}/page:{page_no}/block:{block_no}"
        r = subprocess.run(
            [MINERU_EXE, "read", locator, "-f", "image", "-o", str(out_path)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if r.returncode == 0 and out_path.exists():
            return True
    return False


def infer_missing_captions(blocks: list[dict]) -> dict[int, str]:
    """预扫本页图片 caption；对 caption 缺失的图做【缺号推断】。

    实测案例（p99）：图 3.48 的 caption 被 VLM 错绑到图 3.49 的块里，
    导致 3.48 的图片块 caption 为空；而本页已知图号为 {3.46, 3.47, 3.49}，
    区间内恰好缺 3.48 —— 此时补号是安全的。规则刻意保守：
    仅当【同章、区间恰好缺 1 个号、且有且仅有 1 个空 caption 图】时才推断。
    """
    cap_map: dict[int, str] = {}
    entries = []   # [index, 选中的caption, (major, minor)|None]
    all_caps = []  # (caption, (major, minor)|None) —— 含多caption块的全部
    for b in blocks:
        if b.get("type") != "image":
            continue
        idx = b.get("index", 0)
        cap = image_caption(b).strip()
        m = re.match(r"图\s*(\d+)\s*[-.．]\s*(\d+)", cap)
        nums = (int(m.group(1)), int(m.group(2))) if m else None
        cap_map[idx] = cap
        entries.append([idx, cap, nums])
        for part in b.get("content", []):
            if isinstance(part, dict) and part.get("type") == "image_caption":
                c = block_text(part).strip()
                mm = re.match(r"图\s*(\d+)\s*[-.．]\s*(\d+)", c)
                all_caps.append((c, (int(mm.group(1)), int(mm.group(2))) if mm else None))

    empties = [e for e in entries if not e[1]]
    known = [e[2] for e in entries if e[2]]
    if len(empties) == 1 and known:
        majors = {m for m, _ in known}
        if len(majors) == 1:
            major = majors.pop()
            minors = sorted(n for m, n in known if m == major)
            missing = [n for n in range(minors[0], minors[-1] + 1) if n not in set(minors)]
            if len(missing) == 1:
                # 优先认领"孤儿 caption"（多caption块里未被选中的完整标题,实测案例:
                # p99 图3.48 的标题被 VLM 错绑进图3.49 的块里）
                chosen = {e[1] for e in entries if e[1]}
                orphan = next((c for c, n in all_caps
                               if n == (major, missing[0]) and c not in chosen), None)
                cap = orphan or f"图{major}.{missing[0]}"
                cap_map[empties[0][0]] = cap
                print(f"  [缺号推断] 块 index={empties[0][0]} 补图注: {cap}"
                      + ("（认领孤儿 caption）" if orphan else ""))
    return cap_map


# ---------- 主流程 ----------

def convert(src: Path, with_images: bool = True, force_images: bool = False,
            pdf_path: Path | None = None, crop_images: bool = False,
            crop_zoom: float = 0.0) -> Path:
    data = json.loads(src.read_text(encoding="utf-8"))
    out_dir = src.parent
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    img_names: set[str] = set()

    if crop_images and not pdf_path:
        print("[警告] --crop-images 需要 --pdf；回退 mineru read", file=sys.stderr)
        crop_images = False
    if crop_images and crop_zoom <= 0:
        crop_zoom = auto_crop_zoom(pdf_path)

    doc_ref = find_doc_ref(out_dir / "内容.md")
    if with_images and not doc_ref and not crop_images:
        print("[警告] 未找到文档引用（doc:/tier:），跳过图片导出", file=sys.stderr)
        with_images = False

    body: list[str] = []
    toc: list[tuple[int, str]] = []
    img_count = img_fail = 0
    last_page = None

    pages = sorted(data.get("pages", []), key=lambda p: p.get("page_idx", 0))
    # —— P1 工具修复（2026-10-08）：编号引用还原 + 相邻块重叠合并 ——
    set_tag_set(collect_tag_ids(pages))
    _n_merged = merge_overlapping_neighbors(pages)
    if _n_merged:
        print(f"  [块合并] 相邻块重叠合并 {_n_merged} 处")
    # 章标题配对（正文书眉"第N章 章名"表 + 章首页书眉兜底，见
    # resolve_chapter_titles）；doc_title 章名据此补"第 N 章"前缀
    ch_pairs = resolve_chapter_titles(pages)
    ch_name2no = {v: n for n, v in build_chapter_name_map(pages).items()}
    for page in pages:
        page_idx = page.get("page_idx", 0)
        blocks = sorted(page.get("blocks", []), key=lambda b: b.get("index", 0))
        cap_map = infer_missing_captions(blocks)

        for bi, b in enumerate(blocks):
            btype = b.get("type", "")
            if btype in SKIP_TYPES:
                continue

            if page_idx != last_page and btype in ("text", "paragraph_title"):
                # 正文内容出现时标注 PDF 页码（HTML 注释，不渲染、可溯源）
                body.append(f"\n<!-- p.{page_idx + 1} -->\n")
                last_page = page_idx

            if btype == "doc_title":
                # 前 10 页的 doc_title = 封面/扉页书名（重复书名，过滤）；
                # 之后的是章扉页标题（如"组合逻辑设计"），当章级标题用；
                # 与书眉章名表配上的章名补"第 N 章"前缀（标题样式统一）
                if page_idx >= 10:
                    title = block_text(b).strip()
                    if title:
                        if looks_like_body_title(title, b.get("bbox")):
                            # 误标残片（如 03 第 3 章首页的 doc_title"第3章"，
                            # R5 纯章号）→ 降级加粗正文
                            body.append(f"\n**{title}**\n")
                            continue
                        n_ch = ch_name2no.get(re.sub(r"\s+", "", title))
                        if n_ch is not None:
                            title = chapter_display_title(title, n_ch)
                        body.append(f"\n# {title}\n")
                        toc.append((1, title))

            elif btype == "code":
                code = code_text(b)
                if code:
                    body.append(f"\n```\n{code}\n```\n")

            elif btype == "table":
                parsed = parse_table_html(b)
                if parsed:
                    tcap, trows = parsed
                    if tcap:
                        body.append(f"\n**{tcap}**\n")
                    cols = max(sum(c["colspan"] for c in r) for r in trows)
                    md_rows = []
                    for r in trows:
                        cells = []
                        for c in r:
                            cells.append(re.sub(r"\s+", " ", c["text"]).strip() or " ")
                            cells.extend([""] * (c["colspan"] - 1))
                        md_rows.append("| " + " | ".join(cells) + " |")
                    body.append("\n" + md_rows[0] + "\n|" + "---|" * cols + "\n"
                                + "\n".join(md_rows[1:]) + "\n")

            elif btype == "paragraph_title":
                title = block_text(b).strip()
                if not title:
                    continue
                _pair = ch_pairs.get((page_idx, b.get("index")))
                if _pair is not None:
                    # 章名标题（与书眉章名表/章首页书眉配对成功）→ 章级 +
                    # 合成"第 N 章"前缀（带前缀原文由 chapter_display_title 规范化）
                    _n, _ = _pair
                    title = chapter_display_title(title, _n)
                    body.append(f"\n# {title}\n")
                    toc.append((1, title))
                    continue
                if looks_like_body_title(title, b.get("bbox")):
                    # VLM 误标的加粗正文/题面/页眉 → 降级为加粗段落
                    body.append(f"\n**{title}**\n")
                    continue
                lv = title_level(title, b.get("level"))
                body.append(f"\n{'#' * lv} {title}\n")
                toc.append((lv, title))

            elif btype == "equation":
                eq = clean_equation(block_text(b).strip())
                if eq:
                    body.append(f"\n$$\n{eq}\n$$\n")

            elif btype in ("image", "chart"):
                # caption/bbox 推导与 tex 端共用 figure_info，保证文件名一致
                cap, bbox = figure_info(b, blocks, bi, cap_map)
                name = unique_image_name(cap, page_idx + 1, b.get("index", 0), img_names)
                if with_images:
                    out_png = img_dir / f"{name}.png"
                    if btype == "chart" or crop_images:
                        # bbox 回退链:figure_info（chart 有）→ 块顶层 → image_body 子块
                        crop_bb = bbox or b.get("bbox")
                        if not crop_bb:
                            bp = next((x for x in b.get("content", [])
                                       if isinstance(x, dict)
                                       and x.get("type") == "image_body"), None)
                            crop_bb = (bp or {}).get("bbox")
                        ok = bool(pdf_path and crop_bb) and crop_from_pdf(
                            pdf_path, page_idx, crop_bb, out_png,
                            zoom=crop_zoom if crop_zoom > 0 else 3.0)
                    else:
                        ok = export_image(doc_ref[0], doc_ref[1], page_idx + 1,
                                          b.get("index", 0), out_png, force=force_images)
                    if ok:
                        img_count += 1
                        body.append(f"\n![{cap}](images/{name}.png)\n")
                    else:
                        img_fail += 1
                        body.append(f"\n<!-- 图片导出失败: p{page_idx + 1} b{b.get('index', 0)} -->\n")
                    if cap:
                        body.append(f"\n*{cap}*\n")
                else:
                    body.append(f"\n![{cap}](images/{name}.png)\n")
                    if cap:
                        body.append(f"\n*{cap}*\n")

            elif btype in ("text", "ref_text", "page_footnote"):
                t = block_text(b).strip()
                if t:
                    body.append(f"\n{t}\n")

            else:  # 未知类型原样保留，便于发现
                t = block_text(b).strip()
                if t:
                    body.append(f"\n{t}\n")

    # 组装：标题 + TOC + 正文
    md: list[str] = ["# 内容（阅读友好版）\n"]
    if toc:
        md.append("\n## 目录\n")
        for lv, title in toc:
            md.append(f"{'  ' * (lv - 1)}- {title}")
        md.append("")
    md.append("\n---\n")
    md.extend(body)

    out_md = out_dir / "阅读友好版.md"
    out_md.write_text("\n".join(md), encoding="utf-8")

    print(f"完成: {out_md}")
    print(f"  标题 {len(toc)} 个 | 图片导出 {img_count} 张" + (f" | 失败 {img_fail} 张" if img_fail else ""))
    return out_md


def main():
    ap = argparse.ArgumentParser(description="MinerU JSON → 阅读友好 Markdown")
    ap.add_argument("json_path", help="result.json 路径")
    ap.add_argument("--no-images", action="store_true", help="跳过图片导出（仅生成 md）")
    ap.add_argument("--force-images", action="store_true", help="忽略已存在文件，强制重新导出图片")
    ap.add_argument("--pdf", default=None, help="原书 PDF 路径（chart 类图块裁剪需要）")
    ap.add_argument("--crop-images", action="store_true",
                    help="image 类图块也回原 PDF 裁剪（替代 mineru read；需 --pdf；实测快 ~160 倍且分辨率更高）")
    ap.add_argument("--crop-zoom", type=float, default=0.0,
                    help="裁剪渲染倍率（0=按原图 DPI 自动，如 300DPI→4.17）")
    args = ap.parse_args()
    convert(Path(args.json_path), with_images=not args.no_images,
            force_images=args.force_images,
            pdf_path=Path(args.pdf) if args.pdf else None,
            crop_images=args.crop_images, crop_zoom=args.crop_zoom)


if __name__ == "__main__":
    main()
