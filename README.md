# scan2tex — MinerU 结果 → 可编译 LaTeX 书 / 阅读友好 Markdown

把扫描教材（影印书）经 [MinerU](https://github.com/opendatalab/MinerU) 解析后的
`result.json`，转换成：

- **`book.tex`**：ctexbook 书稿，xelatex 直接编译——公式带原书编号
  （`equation*` + 文本自带 `\tag`）、插图矢量/位图双轨、表格三档列宽 +
  跨页 longtable、图文混排（连续多图一行 / 图文侧排）
- **`阅读友好版.md`** + `images/`：带目录的 Markdown 阅读版

在 5 本真实扫描教材（96–300 DPI、321–798 页、含缩印本与透印低清彩扫）上整书
验证：**全部编译 error 0 / 缺字 0**（567 / 738 / 374 / 262 / 865 页）。

## 工作流

```
扫描 PDF ─ 预增强(可选) ─> 增强 PDF ─ MinerU 解析 ─> result.json
                                                        │
                                     ┌──────────────────┴───────────────┐
                                     ▼                                  ▼
                            json_to_readable.py                  json_to_tex.py
                                     │                                  │
                              阅读友好版.md                    book.tex + images/
                                                                        │
                                  插图矢量化(可选)              xelatex ×2
                                                                        ▼
                                                                     book.pdf
```

## 环境要求

| 组件 | 用途 | 备注 |
|---|---|---|
| Python 3.10+ | 全部工具 | 依赖：`pymupdf` `opencv-python-headless` `numpy` |
| [MinerU](https://github.com/opendatalab/MinerU) | PDF → `result.json`（本工具链不负责 OCR） | 本地部署或官方 API 均可 |
| mkbitmap + potrace | 插图矢量化（可选） | mkbitmap 与 potrace 同源（potrace 工具集）；各包管理器可装 |
| TeX Live / MiKTeX | xelatex 编译 `book.tex` | 需 ctex、amsmath、longtable、fancyvrb、graphicx 等常用宏包 |

```bash
pip install pymupdf opencv-python-headless numpy
```

## 快速开始

```bash
# 1. 预处理增强（可选；低 DPI / 灰底透印的扫描件收益明显）
python 工具/preprocess_scan.py "样例/扫描件.pdf" -o 处理输出/扫描件_增强.pdf --scale 2 --binarize

# 2. MinerU 解析 → result.json（解析命令以 MinerU 文档为准；本工具接受 docvortex.middle 结构）
mineru parse "处理输出/扫描件_增强.pdf" -p "1-400" --tier standard -o result.json

# 3. 导出阅读友好 Markdown + 图片（--crop-images 从 PDF 按 bbox 本地裁剪，比逐张 mineru read 快约 100 倍）
python 工具/json_to_readable.py result.json --pdf "处理输出/扫描件_增强.pdf" --crop-images

# 4.（可选）插图矢量化：线稿 PNG → 矢量 PDF（照片/灰块图按筛查清单跳过）
python 工具/detect_photos.py 处理输出/images          # 初筛 + 缩略图拼贴，目检后填跳过清单
python 工具/vectorize_figures.py 处理输出/images --skip-list photos_skip.txt

# 5. 导出 LaTeX 书稿并编译
python 工具/json_to_tex.py result.json -o 处理输出/book.tex
cd 处理输出 && xelatex -interaction=nonstopmode book.tex && xelatex -interaction=nonstopmode book.tex
```

## 工具清单

### 转换主链

| 工具 | 作用 |
|---|---|
| `工具/json_to_readable.py` | `result.json` → 阅读友好 Markdown + 图片导出（标题层级 / 自动目录 / 图注修复 / 重复块合并 / 幻觉循环截断 / 章节标题配对） |
| `工具/json_to_tex.py` | `result.json` → ctexbook LaTeX（标题、公式、图表、图文混排全量规则；与上者共用解析内核） |

### 输入侧

| 工具 | 作用 |
|---|---|
| `工具/preprocess_scan.py` | 扫描增强：上采样 + 背景除法 OTSU 二值化（去灰底/透印）→ 重建同版式 PDF |
| `工具/merge_doclib_json.py` | 合并 MinerU doclib 分片 JSON（先测几页再整书解析时，分片需合并成完整结果） |

### 插图侧

| 工具 | 作用 |
|---|---|
| `工具/vectorize_figures.py` | 线稿 PNG → 矢量 PDF/SVG（mkbitmap + potrace；tex 自动优先引用矢量版） |
| `工具/detect_photos.py` | 照片/灰块图筛查（数值初筛 + 缩略图拼贴目检，产出矢量化跳过清单） |
| `工具/check_images.py` | 核对 tex 图片引用与 `images/` 实际文件（缺图/孤儿双向检查） |
| `工具/dump_image_map.py` | 图块 → 文件名映射 CSV（导出端与 tex 端命名一致性核查） |

### 体检与调研（`调研/`）

| 工具 | 作用 |
|---|---|
| `调研/pdf_health_check.py` | 素材体检：扫描/原生、分辨率、文本层、页面尺寸 |
| `调研/render_preview.py` | 样页渲染 PNG + 文本层抽样 |
| `调研/baseline_enhance.py` | 预处理方案对比实验（CLAHE / 自适应阈值 / 背景除法） |
| `调研/pdf_embed_check.py` | PDF 内嵌图原始分辨率与有效 DPI 检查 |
| `调研/*.md` | 技术调研与选型：OCR、图像增强、插图矢量化，含本地实测结论 |

## 处理规则要点

- **标题体系**：星号章节命令 + `addcontentsline` 进目录（编号用 MinerU 文本自带的，
  与原书 "3.5.4" 体系一致，避免 LaTeX 自动编号冲突）；标题误标检降（加粗正文/习题
  题面/页眉残片被误标为标题时降级为正文）；章标题配对（章名大字被识别为普通标题、
  章号被页眉吞掉时，按正文书眉表回配章号）
- **公式**：`equation*` + 文本自带 `\tag{3.19}`；保守清洗——单公式双 `\tag`、
  编号逗号化（`\tag{2, 19}`→`\tag{2.19}`）、非法 `\hline`、缺字形字符裁定表
  （SimSun 字形路由 / 转命令 / CJKmath 三类处置）
- **插图**：宽度按块 bbox 占页比例（经全书版心标定换算，避免系统性偏小）；
  连续图链式收集成一行（N 图一行、超宽等比压缩）；图文侧排（bbox 横向分离 +
  纵向重叠配对，两栏首行 `\vspace{0pt}` 顶对齐）；矢量 PDF 优先引用
- **表格**：列格式三档（自然宽 / 水填折行 / 等比缩放），超页高表自动
  longtable 跨页；单元格内公式与 `multirow` 的叠印防护
- **正文去重**：块间"半截+全量重复"合并（图注块间隔也支持）；块内两遍重复
  截断（字面匹配 + 截断点标点精化 + 列表题号块白名单）；幻觉循环整块截断
  （数字归一化 + 跳接链判据）
- **编号引用还原**：正文引用"式(10, 124)"→"式(10.124)"，经全书 `\tag` 集合
  交叉验证，避免误改真实含义的 "(a, b)" 型内容

## 目录约定

工具按相对路径工作（脚本内不写死绝对路径）：

```
项目根/
├── 工具/          转换工具
├── 调研/          体检脚本与调研文档
├── 样例/          待处理扫描 PDF（自行放入，不入版本控制）
├── 目标/          质量标杆 PDF（可选）
└── 处理输出/      中间产物与导出
```

MinerU 可执行文件、mkbitmap、potrace 默认从 PATH 查找；不在 PATH 时用环境变量
`MINERU_EXE` / `MKBITMAP` / `POTRACE` 指定完整路径。

## 实测数据（5 本扫描教材整书）

| 项目 | 结果 |
|---|---|
| 扫描质量 | 96–300 DPI；含透印低清彩扫与缩印小字本 |
| 规模 | 321–798 页/本，MinerU standard 整书解析 |
| 产出 | 每本 500–1500 张插图（矢量为主 + 位图保留）+ 千级公式 + 百级表格 |
| 编译 | **5/5 全部 error 0、缺字 0**（567 / 738 / 374 / 262 / 865 页） |
| 质量水位 | 正文错字率 0.18–0.95‰（缩印本 20.75‰ 单列，需系统校对）；公式为主要失分点（下标字形混淆 o↔0、I↔l/1） |

> OCR 精度取决于 MinerU 及其模型；本工具链解决的是"识别层输出 → 可读 → 可编译"
> 的工程还原：结构、排版、去重、编号一致性。

## 免责声明

- 本项目**不包含**任何受版权保护的书籍内容、图片或解析产物。请仅处理你拥有
  合法使用权（或已获授权）的材料，并遵守当地法律与平台条款。
- OCR 结果可能存在错误（尤其公式与缩印小字），重要场景请人工校对。

## License

[MIT](LICENSE)
