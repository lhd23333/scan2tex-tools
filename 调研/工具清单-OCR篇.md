# PDF → Markdown/LaTeX 开源工具调研（OCR 篇）

> 调研时间：2026-10-07。stars/license/最近提交为 GitHub 实测值；评测分数来自各项目 README/官方文档。
> 环境约束：Windows 11 + RTX 5060 Laptop (8GB VRAM) + Python 3.12。

## 核心结论

1. **"整书一键出 tex"不存在**——2026-10 没有任何开源项目能做到。所有方案本质是：
   高质量 Markdown（公式内嵌 LaTeX）→ 自建脚本套 ctexart preamble → xelatex 编译。
2. **本场景（中文教材）推荐顺序**：
   - **1st MinerU 4.x** —— 中文最强 + Windows 官方支持 + 官方文档明确 8GB VRAM 支持（本机已部署 4.0.10）
   - **2nd marker 2.0** —— 公式质量公开数据最好（arXiv math 83.9），但 Windows 需 WSL2/Docker，门槛高
   - **3rd PaddleOCR-VL / PP-StructureV3** —— 中文公式专项最强（PP-FormulaNet_plus-L Zh-BLEU 90.64），模型小（0.9B）
3. **明确排除**：olmOCR（硬编码 15GB 显存检查 + 无原生 Windows）、nougat（无中文 + 2025-02 起停更）、texify（已归档）、zerox（云端 API）。

## 显存/Windows 兼容性（8GB 关键事实）

| 项目 | 8GB 可行性 | Windows |
|---|---|---|
| MinerU 4.x | ✅ 官方写 standard 档 ≥8GB（basic 档 4GB+） | ✅ 官方支持（ONNX CPU / llama.cpp Vulkan 路径） |
| marker 2.0 | ⚠️ 边缘，建议 `--mode fast` 或 CPU llama.cpp | ⚠️ NVIDIA 路径推荐 Docker/WSL2 |
| PaddleOCR-VL 0.9B / PP-StructureV3 | ✅ 富余 | ✅ 原生支持 |
| dots.ocr (1.7B) | ✅ 4-5GB | ⚠️ 推荐 vLLM → 需 WSL2 |
| DeepSeek-OCR-2 (3B) | ✅ Q4_K_M 约 2.5GB | 社区有 Windows 适配 |
| GOT-OCR2.0 (~580M) | ✅ <4GB | ✅ 但 2025-02 停更 |
| **olmOCR** | ❌ **硬编码 15GB 检查，直接拦住** | ❌ 无原生，需 WSL2 |
| Chandra 2（榜单最高分 85.8） | ⚠️ 需 vLLM | ⚠️ 需 WSL2 |

## 中文公式专项（截图/区域 → LaTeX）

| 项目 | Stars | 最近提交 | License | 说明 |
|---|---|---|---|---|
| Pix2Text | 3.3k | 2026-08（活跃） | MIT | 中英混排+公式，支持整页 PDF→MD |
| PP-FormulaNet_plus-L | — | 活跃 | Apache-2.0 | 中文公式 Zh-BLEU 90.64，PaddleOCR 生态 |
| MixTeX | 1.6k | 仓库存活 | AGPL-3.0 | 仅 Windows、纯 CPU 离线，有 exe |
| TexTeller | 760 | 2025-08 | Apache-2.0 | 图片→LaTeX，有 ONNX/CUDA 后端 |
| LaTeX-OCR (pix2tex) | 16.6k | 2025-01（停滞） | MIT | 公式截图→LaTeX 老牌，只认裁剪好的公式图 |

## 公开榜单（olmOCR-bench overall，注意口径）

Chandra 2: 85.8 > dots.mocr: 83.9 > olmOCR 2: 82.4 > Marker v1.10: 76.5 >
**Marker balanced 2.x: 76.0** > DeepSeek OCR: 75.4 > **MinerU 2.5: 75.2** >
**MinerU pipeline: 72.7** > Qwen3-VL 8B: 64.6 > **docling: 50.3**

⚠️ **保留意见**：olmOCR-bench 是 AllenAI 自建榜、英文 arXiv 为主，**不能代表中文教材表现**。
中文场景应参考 **OmniDocBench**（OpenDataLab）：PaddleOCR-VL-1.6 = 96.33（2026-05），
PP-StructureV3 中文公式 edit distance 0.535 优于 MinerU-0.9.3 的 0.577。

## 各项目明细

### MinerU ★ 本场景首选（本机已部署 4.0.10）
- URL: https://github.com/opendatalab/MinerU ｜ 81.2k stars ｜ 2026-10-06 活跃 ｜ release 4.0.10 (2026-09-29)
- License: "MinerU Open Source License"（Apache-2.0 + 附加条款，商用于 MAU>1亿 或月营收>2000万美元时才需授权；**个人学术无限制**）。注意：网上误传为 AGPL，以 LICENSE.md 为准。
- 档位: flash/basic/standard/advanced；standard 档官方写 ≥8GB VRAM、内存 16GB
- 实测注意: StructEqTable + 高 dpi 易 OOM，降 dpi 到 200 省约 55% 显存
- 输出: Markdown + JSON，公式为 LaTeX

### marker 2.0
- URL: https://github.com/datalab-to/marker（旧 VikParuchuri/marker 已重定向）｜ 40.2k stars ｜ 2026-10-02 活跃
- License: 代码 Apache-2.0；**权重为修改版 OpenRAIL-M**（个人/研究免费，商业自托管需授权）
- Marker 2 起改为本地推理服务器架构；无 `latex` 输出格式选项（公式以 $$ 内嵌 markdown）

### PaddleOCR / PP-StructureV3 / PaddleOCR-VL
- URL: https://github.com/PaddlePaddle/PaddleOCR ｜ 90.7k stars ｜ 2026-09-16 活跃 ｜ Apache-2.0
- 注意: PaddleOCR-VL 无独立仓库（内嵌，0.9B = NaViT + ERNIE-4.5-0.3B）
- 中文公式务必选 **plus 版**（基础版 Zh-BLEU 仅 ~45.7）

### olmOCR（排除）
- 19.7k stars ｜ Apache-2.0 ｜ 但 8GB 不可行（硬编码 15GB 检查 + FP8→FP16 上采样实占 ~20GB）
- 社区 GGUF 量化（Q4_K_M ≈6GB）可绕过但脱离官方管线，不建议

### 其他活跃项目
- Chandha 2（datalab-to/chandra，12.4k）：olmOCR-bench 最高分 85.8，需 vLLM/WSL2
- dots.ocr（studio-dots-ai/dots.ocr，9.2k，MIT）：中文强，1.7B；后继 dots.mocr (3B) 83.9
- docling（68.5k，MIT）：明确支持 Windows，但公式弱（bench 50.3）
- OCRmyPDF（35k，MPL-2.0）：**只加文字层不改外观、不产公式**——是"可搜索化"工具，不是"还原 LaTeX"工具
- GOT-OCR2.0：仓库名带点（`GOT-OCR2.0`），代码 Apache-2.0 但**数据 CC-BY-NC**；2.5 版本不存在
- DeepSeek-OCR-2：3B，Q4_K_M 约 2.5GB，8GB 适配好

## 对项目的执行建议

1. 主力走本机 MinerU（`server start` → 127.0.0.1:15980），standard 档
2. 公式兜底/复核：Pix2Text 或 PP-FormulaNet_plus-L
3. 拼 .tex：自写脚本（ctex + xelatex），这是必要的自研部分
4. 预期管理：单页/章节级 90-95% 自动正确率可期，整书零校对不现实
