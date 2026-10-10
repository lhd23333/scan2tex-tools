# -*- coding: utf-8 -*-
r"""EPUB → ctexbook LaTeX/PDF（排版规则与 json_to_tex 同源）。

面向「扫描 OCR 转 EPUB」形态（calibre 产物）：正文文本层 + 行内小图（公式 /
外文碎片——OCR 认不出的部分）+ toc.ncx 层级目录。与 json_to_tex 共用 ctexbook
排版体系（PREAMBLE / tex_escape_mixed / 星号章节 + 手写目录 / 封面模板）：

  标题 → \part*/\chapter*/...（编号用原文自带的"第一章/(1)"，不用 LaTeX 自动
        编号）+ \addcontentsline 进目录；\part 对齐"卷"、\chapter 对齐"册/序言"
  脚注 → 正文 <sup> 链接 + 文末注文 → LaTeX \footnote（编号由 LaTeX 重排）
  图片 → 行内碎片按 300dpi 语义缩放（\raisebox 基线沉降）内嵌；独立成段的大图
        （表格/插页扫描）居中，宽度按 300dpi 语义并封顶版心
  正文 → tex_escape_mixed 转义（与 json_to_tex 同一实现，$..$ 行内公式保护）

层级映射（ncx 深度 → LaTeX）：1→\part，2→\chapter，3→\section，4→\subsection，
5→\subsubsection；第 5 层内按编号形态动态定级："1./Ⅰ." 留在 5，"A./B." 到
\paragraph，"（1）/（a）" 到 \subparagraph（跟随前一形态动态嵌套）。

用法:
  .venv\Scripts\python.exe 工具\epub_to_tex.py <书.epub> [-o 输出目录]
        [--title 书名] [--author 作者] [--toc-depth 3] [--no-pdf] [--no-cover]
        [--limit N] [--xelatex PATH]

产物: 输出目录/book.tex + images/（全部插图与封面）+ book.pdf（默认 xelatex
两遍）。边界: 仅解析 EPUB2 toc.ncx；无 ncx 时退化为纯粗体段 + 编号模式定级。
"""
from __future__ import annotations

import argparse
import html as _html
import posixpath
import re
import shutil
import subprocess
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import json_to_tex as jt  # noqa: E402  复用 PREAMBLE/转义/封面模板/目录块

# 层级 → LaTeX 章节命令（1=卷 … 7=最深层）
LEVEL_CMD = {1: "part", 2: "chapter", 3: "section", 4: "subsection",
             5: "subsubsection", 6: "paragraph", 7: "subparagraph"}
# 图片尺寸换算：px → pt（扫描 300dpi 语义：1px = 72/300 pt）。行内碎片与
# 独立图按同一比例渲染即保持原书比例；行内图高度封顶防奇形碎片撑行。
PX_TO_PT = 0.25
INLINE_MAX_PT = 20.0
FIGURE_MAX_PT = 455.0  # ≈ A4 2.5cm 边距下版心宽

# 附加导言区：段落级标题改独立行样式（ctex 默认 run-in 与正文同行，与
# json_to_tex 的各级标题观感不一致）
EXTRA_PREAMBLE = r"""% —— EPUB 附加 ——
% 超长脚注（如序言注 1，3000+ 字符）用 LaTeX 原生 \footnote 时不能跨页，
% 会挤出一整页空白（实测 280 页版第 12 页）；bigfoot 允许脚注跨页续排，
% 与纸质书排版一致（注文起于标题页底、次页续完），实测空页消失且 0 报错。
\usepackage{bigfoot}
% 段落级标题独立成行（ctex 默认 run-in 与正文同行）
\ctexset{
  paragraph = {runin = false, format = {\raggedright\bfseries},
               beforeskip = 1.2ex, afterskip = 0.7ex},
  subparagraph = {runin = false, format = {\raggedright\bfseries},
                  beforeskip = 1.0ex, afterskip = 0.6ex},
}"""

_VOID = {"img", "br", "hr", "meta", "link", "input", "col", "area", "base",
         "wbr", "source", "track", "embed", "param"}


# --------------------------------------------------------------------------
# 迷你 DOM（stdlib html.parser；calibre 产物是规整 XHTML，容错解析即可）
# --------------------------------------------------------------------------
class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []


class _Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#doc")
        self.stack = [self.root]
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if tag in ("script", "style"):
            return
        self.stack[-1].children.append(
            _Node(tag, {k.lower(): (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip:
                self._skip -= 1
            return
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if not self._skip and data:
            self.stack[-1].children.append(data)


def parse_html(text: str) -> _Node:
    p = _Dom()
    try:
        p.feed(text)
    except Exception:
        pass
    return p.root


def _find(node: _Node, tag: str) -> _Node | None:
    for ch in node.children:
        if not isinstance(ch, str):
            if ch.tag == tag:
                return ch
            r = _find(ch, tag)
            if r is not None:
                return r
    return None


def _iter_desc(node: _Node):
    for ch in node.children:
        if not isinstance(ch, str):
            yield ch
            yield from _iter_desc(ch)


def _text_of(node: _Node) -> str:
    buf = []

    def walk(n):
        for ch in n.children:
            if isinstance(ch, str):
                buf.append(ch)
            else:
                walk(ch)

    walk(node)
    return "".join(buf)


# --------------------------------------------------------------------------
# 文本清洗与转义
# --------------------------------------------------------------------------
def _clean_text(s: str) -> str:
    s = (s.replace("\xa0", " ").replace("\u200b", "").replace("\xad", "-")
          .replace("\u2009", " ").replace("\u202f", " "))
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return re.sub(r"[ \t\r\n\f]+", " ", s)


_MARKER_RE = re.compile(r"[\[（(][0-9A-Za-z]{1,4}[\]）)]")


def _norm_label(s: str) -> str:
    """匹配归一键：去空白与 [N]/(N)/（N）注码。"""
    s = _MARKER_RE.sub("", s)
    return re.sub(r"[\s\u3000]+", "", s)


def _plain_text(node: _Node, skip_sup: bool = True) -> str:
    """节点可见文本（默认跳过 sup 注码），供标题匹配/目录标签用。"""
    buf = []

    def walk(n):
        for ch in n.children:
            if isinstance(ch, str):
                buf.append(ch)
            elif ch.tag == "img":
                continue
            elif ch.tag == "sup" and skip_sup:
                continue
            elif ch.tag == "br":
                buf.append(" ")
            else:
                walk(ch)

    walk(node)
    return _clean_text("".join(buf)).strip()


def _bold_stats(node: _Node) -> tuple[int, int]:
    """(可见文本长度, 粗体范围内长度)；注码与图片不计入。"""
    total = [0]
    bold = [0]

    def walk(n, in_bold):
        for ch in n.children:
            if isinstance(ch, str):
                L = len(ch.strip())
                if L:
                    total[0] += L
                    if in_bold:
                        bold[0] += L
            elif ch.tag in ("sup", "img"):
                continue
            else:
                walk(ch, in_bold or ("bold" in ch.attrs.get("class", "")))

    walk(node, False)
    return total[0], bold[0]


def _pure_bold(node: _Node) -> bool:
    total, bold = _bold_stats(node)
    return total > 0 and bold / total >= 0.95


def _has_img(node: _Node) -> bool:
    return any(True for _ in _iter_desc(node) if _.tag == "img")


# --------------------------------------------------------------------------
# EPUB 读取
# --------------------------------------------------------------------------
def _read_opf(zf: zipfile.ZipFile) -> tuple[dict, list, dict]:
    """→ (manifest id→href, spine href 列表, metadata)。"""
    container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
    m = re.search(r'full-path="([^"]+)"', container)
    opf_path = m.group(1) if m else "content.opf"
    opf = zf.read(opf_path).decode("utf-8", "replace")

    manifest: dict[str, str] = {}
    for m in re.finditer(r"<item\b[^>]*/>", opf):
        tag = m.group(0)
        iid = re.search(r'id="([^"]*)"', tag)
        href = re.search(r'href="([^"]*)"', tag)
        if iid and href:
            manifest[iid.group(1)] = href.group(1)
    spine = [manifest[s] for s in re.findall(r'<itemref\s[^>]*idref="([^"]+)"', opf)
             if s in manifest]

    base = posixpath.dirname(opf_path)
    if base:
        manifest = {k: posixpath.normpath(posixpath.join(base, v))
                    for k, v in manifest.items()}
        spine = [posixpath.normpath(posixpath.join(base, v)) for v in spine]

    meta = {}
    for key, pat in (("title", r"<dc:title[^>]*>(.*?)</dc:title>"),
                     ("creator", r"<dc:creator[^>]*>(.*?)</dc:creator>")):
        m = re.search(pat, opf, re.S)
        meta[key] = _html.unescape(m.group(1)).strip() if m else ""
    return manifest, spine, meta


def _read_ncx(zf: zipfile.ZipFile) -> list[tuple[int, str, str]]:
    """→ [(深度, 标题, src)]；src 形如 'text/part0001.html' 或 '…#fileposN'。

    无 toc.ncx（EPUB3 或损坏）时返回空表，走纯粗体段定级退化路径。
    """
    path = next((n for n in zf.namelist() if n.lower().endswith(".ncx")), None)
    if path is None:
        return []
    raw = zf.read(path).decode("utf-8", "replace")
    import xml.etree.ElementTree as ET

    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    entries: list[tuple[int, str, str]] = []
    base = posixpath.dirname(path)

    def walk(el, depth):
        for c in el.findall("n:navPoint", ns):
            label = c.findtext("n:navLabel/n:text", "", ns).strip()
            src = c.find("n:content", ns).get("src", "")
            if "#" in src:
                f, _, frag = src.partition("#")
                f = posixpath.normpath(posixpath.join(base, f)) if f else ""
                src = f + "#" + frag
            elif src and base:
                src = posixpath.normpath(posixpath.join(base, src))
            entries.append((depth, label, src))
            walk(c, depth + 1)

    navmap = root.find("n:navMap", ns)
    if navmap is not None:
        walk(navmap, 1)
    return entries


def _refine_levels(entries: list[tuple[int, str, str]]) -> dict:
    """给 ncx 条目定级（含第 5 层动态嵌套），返回 {(文件,#锚): (深度, 标题)} 与
    文件级 {(文件): (深度, 标题)}。第 5 层形态："1./Ⅰ."→5；"A./B."→6；
    "（1）/（a）"→紧随字母节时 7、紧随数字节时 6、链式保持。"""
    anchors: dict[tuple[str, str], tuple[int, str]] = {}
    files: dict[str, tuple[int, str]] = {}
    prev_kind = None   # None | "num" | "letter" | "paren"
    paren_depth = 6
    for depth, label, src in entries:
        if depth != 5:
            prev_kind, paren_depth = None, 6
            d = depth
        else:
            t = label.strip()
            if re.match(r"^（?\(?[0-9a-zA-Z]{1,3}[）)]", t) or re.match(r"^[（(]", t):
                kind = "paren"
            elif re.match(r"^[A-Za-z]\.", t):
                kind = "letter"
            elif re.match(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\.", t) or re.match(r"^\d+\.", t):
                kind = "num"
            else:
                kind = None
            if kind == "num":
                d = 5
            elif kind == "letter":
                d = 6
            elif kind == "paren":
                if prev_kind == "letter":
                    d = paren_depth = 7
                elif prev_kind == "paren":
                    d = paren_depth
                else:
                    d = paren_depth = 6
            else:
                d = 5
            if kind:
                prev_kind = kind
        if "#" in src:
            f, _, frag = src.partition("#")
            anchors[(f, frag)] = (d, label)
        else:
            files[src] = (d, label)
    return anchors, files


def _extract_assets(zf: zipfile.ZipFile, images_dir: Path) -> tuple[int, str | None]:
    """抽出全部 images/* 图片与封面 → images_dir；返回 (图片数, 封面文件名)。"""
    images_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    names = zf.namelist()
    for name in names:
        if name.startswith("images/") and name.lower().endswith(
                (".jpg", ".jpeg", ".png", ".gif")):
            (images_dir / posixpath.basename(name)).write_bytes(zf.read(name))
            n += 1
    cover = None
    for cand in ("cover.jpeg", "cover.jpg", "cover.png",
                 "images/cover.jpeg", "images/cover.jpg"):
        if cand in names:
            ext = posixpath.splitext(cand)[1].lower()
            dst = images_dir / ("_cover" + ext)
            dst.write_bytes(zf.read(cand))
            cover = dst.name
            break
    return n, cover


def _img_size(path: Path) -> tuple[int, int] | None:
    """JPEG/PNG 像素尺寸（读文件头，不依赖任何图像库）。"""
    try:
        data = path.read_bytes()[:65536]
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24:
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 <= n:
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            return None
        i += 2 + seg
    return None


# --------------------------------------------------------------------------
# 转换主体
# --------------------------------------------------------------------------
class _Book:
    def __init__(self, epub: Path, out_dir: Path, *, title: str = "",
                 author: str = "", toc_depth: int = 3, use_cover: bool = True,
                 limit: int = 0):
        self.epub = epub
        self.out_dir = out_dir
        self.images_dir = out_dir / "images"
        self.title_opt, self.author_opt = title, author
        self.toc_depth = toc_depth
        self.use_cover = use_cover
        self.limit = limit
        self.body: list[str] = []
        self.stats = {"h": 0, "footnote": 0, "fb_fallback": 0, "inline_img": 0,
                      "figure": 0, "para": 0, "bold_plain": 0, "note_skip": 0,
                      "stray": 0}
        self._wh_cache: dict[str, tuple[int, int] | None] = {}
        self.cur_file = ""

    # ---- 图片 ----
    def _wh(self, fname: str):
        if fname not in self._wh_cache:
            self._wh_cache[fname] = _img_size(self.images_dir / fname)
        return self._wh_cache[fname]

    def _inline_img(self, fname: str) -> str:
        wh = self._wh(fname)
        self.stats["inline_img"] += 1
        if wh:
            hp = min(max(wh[1] * PX_TO_PT, 4.0), INLINE_MAX_PT)
            return (r"\raisebox{-0.2\height}{\includegraphics[height="
                    + ("%.1f" % hp) + "pt]{" + fname + "}}")
        return r"\includegraphics[height=1em]{" + fname + "}"

    def _figure(self, fname: str) -> str:
        wh = self._wh(fname)
        self.stats["figure"] += 1
        if wh:
            wp = min(wh[0] * PX_TO_PT, FIGURE_MAX_PT)
            spec = "width=%.1fpt" % wp
        else:
            spec = r"width=0.8\linewidth"
        return ("\n\\begin{center}\n\\includegraphics[" + spec + "]{"
                + fname + "}\n\\end{center}\n")

    # ---- 富文本渲染（行内） ----
    def _rich(self, node: _Node, note: bool = False) -> str:
        out: list[str] = []
        for ch in node.children:
            if isinstance(ch, str):
                t = _clean_text(ch)
                if t:
                    out.append(jt.tex_escape_mixed(t))
                continue
            tag = ch.tag
            if tag == "img":
                f = posixpath.basename(ch.attrs.get("src", ""))
                if f:
                    out.append(self._inline_img(f))
            elif tag == "br":
                if "".join(out).strip():
                    out.append(r"\\ ")
            elif tag == "sup":
                out.append(self._sup(ch, note))
            elif tag in ("span", "small", "i", "em", "b", "strong", "u",
                         "a", "font"):
                inner = self._rich(ch, note)
                if not inner.strip():
                    continue
                cls = ch.attrs.get("class", "")
                if "bold" in cls:
                    out.append(r"\textbf{" + inner + "}")
                elif "underline" in cls:
                    out.append(r"\underline{" + inner + "}")
                else:
                    out.append(inner)
            elif tag in ("p", "div", "section", "h1", "h2", "h3", "h4", "h5",
                         "h6", "body"):
                out.append(self._rich(ch, note))
            # 其余标签（head/title 等）忽略
        return "".join(out)

    def _sup(self, sup: _Node, note: bool) -> str:
        """<sup>：带链接且目标可解析 → \\footnote{}；否则上标文本兜底。"""
        a = next((d for d in _iter_desc(sup)
                  if d.tag == "a" and d.attrs.get("href")), None)
        if a is not None:
            href = a.attrs["href"]
            if not note and "#" in href:
                tf, _, tid = href.partition("#")
                tf = (posixpath.normpath(posixpath.join(
                    posixpath.dirname(self.cur_file), tf)) if tf else self.cur_file)
                target = self.note_nodes.get((tf, tid))
                if target is not None:
                    self.stats["footnote"] += 1
                    return r"\footnote{" + self._render_note(target) + "}"
            # 目标缺失 / 脚注套脚注 / 无片段 → 上标标记文本
            mtxt = _clean_text(_text_of(a).strip() or _text_of(sup).strip())
            if not mtxt:
                return ""
            self.stats["fb_fallback"] += 1
            return r"\textsuperscript{" + jt.tex_escape_mixed(mtxt) + "}"
        inner = self._rich(sup, note)
        return r"\textsuperscript{" + inner + "}" if inner.strip() else ""

    def _render_note(self, p: _Node) -> str:
        """脚注段渲染：去前导回链 <a>（"(N)"/"[N]" 反链），其余照常。"""
        ch = list(p.children)
        while ch and isinstance(ch[0], str) and not ch[0].strip():
            ch.pop(0)
        if ch and not isinstance(ch[0], str) and ch[0].tag == "a":
            ch.pop(0)
        wrapper = _Node("span")
        wrapper.children = ch
        return self._rich(wrapper, note=True)

    # ---- 标题 ----
    def _emit_heading(self, depth: int, rich: str, toc_label: str) -> None:
        d = max(1, min(7, int(depth)))
        cmd = LEVEL_CMD[d]
        rich = " ".join(rich.split()).strip()
        if d == 1 and toc_label.strip() == "总目录":
            # 原书总目录页：正文保留，不进生成目录（与 LaTeX 目录重复）
            self.body.append("\n\\chapter*{总目录}\n")
            self.stats["h"] += 1
            return
        if not rich:
            rich = jt.tex_escape_mixed(toc_label)
        self.body.append("\n\\" + cmd + "*{" + rich + "}\n")
        self.body.append("\\addcontentsline{toc}{" + cmd + "}{"
                         + jt.tex_escape_mixed(toc_label) + "}\n")
        self.stats["h"] += 1

    # ---- 主流程 ----
    def run(self) -> dict:
        with zipfile.ZipFile(self.epub) as zf:
            manifest, spine, meta = _read_opf(zf)
            entries = _read_ncx(zf)
            anchors, file_entries = _refine_levels(entries)
            n_img, cover = _extract_assets(zf, self.images_dir)

            title_s = (self.title_opt or meta.get("title") or self.epub.stem).strip()
            author_s = (self.author_opt or meta.get("creator")
                        or "EPUB 结构化转换").strip()

            if self.limit:
                spine = spine[:self.limit]

            # 阶段 A：解析全部 XHTML，建 id 索引与脚注目标索引
            trees: dict[str, _Node] = {}
            id_map: dict[tuple[str, str], _Node] = {}
            for f in spine:
                if not f.lower().endswith((".html", ".xhtml", ".htm")):
                    continue
                try:
                    text = zf.read(f).decode("utf-8", "replace")
                except KeyError:
                    continue
                tree = parse_html(text)
                body = _find(tree, "body")
                if body is None:
                    continue
                trees[f] = body

                # id 索引：内联元素（calibre 把脚注回链 id 挂在段内 <a> 上）
                # 归属到所在段落，否则渲染目标只得到回链标记
                def index_ids(n, cur_p):
                    for ch in n.children:
                        if isinstance(ch, str):
                            continue
                        did = ch.attrs.get("id")
                        if did:
                            host = (ch if ch.tag in ("p", "div", "li",
                                                     "blockquote", "body")
                                    else cur_p)
                            id_map[(f, did)] = host if host is not None else ch
                        index_ids(ch, ch if ch.tag == "p" else cur_p)

                index_ids(body, None)

            self.note_nodes: dict[tuple[str, str], _Node] = {}
            for f, body in trees.items():
                for d in _iter_desc(body):
                    if d.tag != "sup":
                        continue
                    a = next((x for x in _iter_desc(d)
                              if x.tag == "a" and x.attrs.get("href")), None)
                    if a is None or "#" not in a.attrs["href"]:
                        continue
                    tf, _, tid = a.attrs["href"].partition("#")
                    tf = (posixpath.normpath(posixpath.join(
                        posixpath.dirname(f), tf)) if tf else f)
                    tgt = id_map.get((tf, tid))
                    if tgt is not None:
                        self.note_nodes[(tf, tid)] = tgt

            # 阶段 B：按 spine 顺序走读
            n_files = len(trees)
            for i, f in enumerate(trees, 1):
                self.cur_file = f
                self._walk_file(f, trees[f], anchors, file_entries)
                if i % 20 == 0 or i == n_files:
                    print(f"  [{i}/{n_files}] {f}")

        # 组装 tex
        cover_rel = ("images/" + cover) if (cover and self.use_cover) else None
        pre = jt.PREAMBLE
        if cover_rel:
            # 整书模式 graphicspath 指向 images/，但 _cover_block 需要能被
            # graphicspath 解析的相对名
            pre = pre.replace("\\maketitle",
                              jt._cover_block(title_s, author_s, cover))
        toc_pkgs, toc_block = jt._toc_parts(1)
        depth = max(-1, min(5, self.toc_depth))
        toc_block = "\\setcounter{tocdepth}{%d}\n" % depth + toc_block
        tex = (pre.replace("@@TOC_PKGS@@\n", toc_pkgs)
                  .replace("@@TOC_BLOCK@@", toc_block)
                  .replace("@@TITLE@@", jt.tex_escape(title_s))
                  .replace("@@AUTHOR@@", jt.tex_escape(author_s)))
        tex = tex.replace("\\begin{document}",
                          EXTRA_PREAMBLE + "\n\\begin{document}", 1)
        tex += "".join(self.body) + jt.POSTAMBLE
        tex_path = self.out_dir / "book.tex"
        tex_path.write_text(tex, encoding="utf-8")

        self.stats["images_total"] = n_img
        self.stats["title"] = title_s
        self.stats["author"] = author_s
        print(f"  标题: {title_s} | 作者: {author_s}")
        print(f"  标题 {self.stats['h']} | 脚注 {self.stats['footnote']}"
              f"（兜底 {self.stats['fb_fallback']}）"
              f" | 行内图 {self.stats['inline_img']} | 独立图 {self.stats['figure']}"
              f" | 段落 {self.stats['para']} | 粗体段 {self.stats['bold_plain']}"
              f" | 脚注段跳过 {self.stats['note_skip']}")
        print(f"完成: {tex_path}")
        return self.stats

    def _walk_file(self, f: str, body: _Node, anchors: dict,
                   file_entries: dict) -> None:
        # 顶层块序列（p / 展开的 div；mbp_pagebreak 跳过）
        blocks: list[tuple[str, _Node]] = []

        def rec(n):
            for ch in n.children:
                if isinstance(ch, str):
                    if ch.strip():
                        self.stats["stray"] += 1
                    continue
                if ch.tag == "p":
                    blocks.append(("p", ch))
                elif ch.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    blocks.append(("p", ch))
                elif ch.tag == "div":
                    if "mbp_pagebreak" in ch.attrs.get("class", ""):
                        continue
                    rec(ch)

        rec(body)

        entry = file_entries.get(f)
        first_bold = next((i for i, (k, n) in enumerate(blocks)
                           if k == "p" and _pure_bold(n)), None)
        if entry is not None and first_bold is None:
            # 无粗体段（扉页图类文件）→ 标题落在文件头
            self._emit_heading(entry[0], jt.tex_escape_mixed(entry[1]), entry[1])
            entry = None

        for i, (k, n) in enumerate(blocks):
            pid = n.attrs.get("id", "")
            if pid and (f, pid) in anchors:
                d, label = anchors[(f, pid)]
                rich = self._rich(n).strip() or jt.tex_escape_mixed(label)
                toc = label or _plain_text(n)
                self._emit_heading(d, rich, toc)
                continue
            if pid and (f, pid) in self.note_nodes:
                self.stats["note_skip"] += 1
                continue
            rich = self._rich(n).strip()
            text_plain = _plain_text(n)
            has_img = _has_img(n)
            if _pure_bold(n):
                if entry is not None and i == first_bold:
                    # 文件级 ncx 条目：首个粗体段即该文件标题
                    rich = rich or jt.tex_escape_mixed(entry[1])
                    self._emit_heading(entry[0], rich, entry[1])
                    entry = None
                    continue
                if rich:
                    self.body.append("\n\\noindent\\textbf{" + rich + "}\n")
                    self.stats["bold_plain"] += 1
                continue
            if not text_plain and has_img:
                # 纯图段（扉页照片/表格扫描等）→ 独立居中图
                for d in _iter_desc(n):
                    if d.tag == "img":
                        fn = posixpath.basename(d.attrs.get("src", ""))
                        if fn and (self.images_dir / fn).is_file():
                            self.body.append(self._figure(fn))
                continue
            if not rich:
                continue
            self.body.append("\n" + rich + "\n")
            self.stats["para"] += 1

        if entry is not None:
            # 兜底：文件级条目未被首粗体段消费（首粗体带 ncx 锚等少见情形）
            self._emit_heading(entry[0], jt.tex_escape_mixed(entry[1]), entry[1])


# --------------------------------------------------------------------------
# 编译
# --------------------------------------------------------------------------
def _find_xelatex(user: str) -> str | None:
    if user:
        return user
    w = shutil.which("xelatex")
    if w:
        return w
    import os
    la = os.environ.get("LOCALAPPDATA", "")
    for cand in (
        rf"{la}\Programs\MiKTeX\miktex\bin\x64\xelatex.exe",
        r"C:\Program Files\MiKTeX\miktex\bin\x64\xelatex.exe",
        r"C:\texlive\2024\bin\windows\xelatex.exe",
        r"C:\texlive\2025\bin\windows\xelatex.exe",
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def compile_pdf(out_dir: Path, xelatex: str | None, runs: int = 2) -> bool:
    exe = _find_xelatex(xelatex or "")
    if exe is None:
        print("[警告] 未找到 xelatex，跳过 PDF 编译（book.tex 已生成，可手动编译）")
        return False
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    for run in (1, 2) if runs == 2 else (1,):
        print(f"  xelatex 第 {run} 遍…")
        try:
            r = subprocess.run(
                [exe, "-interaction=nonstopmode", "book.tex"],
                cwd=out_dir, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=3600, creationflags=flags)
        except subprocess.TimeoutExpired:
            print("[错误] xelatex 超时（1h）")
            return False
        if r.returncode != 0:
            tail = "\n".join((r.stdout or "").splitlines()[-30:])
            print(f"[错误] xelatex 第 {run} 遍失败（exit {r.returncode}）：\n{tail}")
            return False
    pdf = out_dir / "book.pdf"
    if pdf.is_file():
        print(f"PDF: {pdf}（{pdf.stat().st_size / 1e6:.1f} MB）")
        return True
    print("[错误] xelatex 正常退出但未生成 book.pdf")
    return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return s[:80] or "book"


def main() -> int:
    p = argparse.ArgumentParser(
        description="EPUB → ctexbook LaTeX/PDF（排版规则与 json_to_tex 同源）")
    p.add_argument("epub", type=Path, help="输入 EPUB 文件")
    p.add_argument("-o", "--out-dir", type=Path, default=None,
                   help="输出目录（默认 <EPUB 同目录>/<书名>_tex/）")
    p.add_argument("--title", default="", help="书名（默认取 EPUB dc:title）")
    p.add_argument("--author", default="", help="作者（默认取 EPUB dc:creator）")
    p.add_argument("--toc-depth", type=int, default=3,
                   help="生成目录深度：0=章，3=节（默认），5=全部 7 级")
    p.add_argument("--no-cover", action="store_true", help="不使用封面图")
    p.add_argument("--no-pdf", action="store_true", help="只生成 tex，不编译 PDF")
    p.add_argument("--limit", type=int, default=0,
                   help="只转换前 N 个 spine 文件（调试用）")
    p.add_argument("--xelatex", default="", help="xelatex 可执行文件路径")
    args = p.parse_args()

    epub = args.epub.resolve()
    if not epub.is_file():
        print(f"[错误] 找不到输入文件: {epub}")
        return 2
    if args.out_dir is None:
        with zipfile.ZipFile(epub) as zf:
            _, _, meta = _read_opf(zf)
        stem = _safe_name(meta.get("title") or epub.stem)
        out_dir = epub.parent / (stem + "_tex")
    else:
        out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"输入: {epub}")
    print(f"输出: {out_dir}")
    book = _Book(epub, out_dir, title=args.title, author=args.author,
                 toc_depth=args.toc_depth, use_cover=not args.no_cover,
                 limit=args.limit)
    book.run()

    if not args.no_pdf:
        ok = compile_pdf(out_dir, args.xelatex)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
