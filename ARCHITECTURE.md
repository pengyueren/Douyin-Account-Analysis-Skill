# 抖音账号诊断 CLI 工具集

> 一个独立的 Python CLI 工具集，用于抖音创作者账号诊断。
> 可作为 Claude Code Skill 集成，也可被 Codex、Windsurf、Trae 等其他 agent 通过命令行调用。

## 项目定位

```
┌──────────────────────────────────────────────────────┐
│                   Agent 层 (可选)                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────┐ │
│  │Claude    │  │Codex     │  │Windsurf  │  │Trae  │ │
│  │Code Skill│  │Rule      │  │Rule      │  │Rule  │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──┬───┘ │
│       └──────────────┴──────────────┴───────────┘    │
│                          │ 通过命令行调用 CLI 工具     │
├─────────────────────────┼────────────────────────────┤
│              CLI 接口层 (通用)                        │
│  bridge/videoagent_bridge.py                         │
│  输出: JSON 到 stdout (任何 agent 都可消费)           │
├─────────────────────────────────────────────────────┤
│              数据采集层 (Bridge)                      │
│  mediacrawler_runner → 抖音数据                       │
│  video_analyzer → 视频下载+转写+多模态分析            │
├─────────────────────────────────────────────────────┤
│              分析层 (Diagnosis)                       │
│  SKILL.md → 6 维度分析框架 + 6 段内容拆解             │
│  references/ → 赛道基准数据 + 改进操作手册            │
├─────────────────────────────────────────────────────┤
│              持久化层 (Storage)                       │
│  store/accounts/{昵称}/ → 全链路数据保存             │
└─────────────────────────────────────────────────────┘
```

## 技术栈

| 层 | 技术 | 用途 |
|---|------|------|
| 数据采集 | **MediaCrawler** (CLI subprocess) | 抖音创作者主页数据抓取 |
| 视频下载 | **yt-dlp** | 下载抖音视频 / 音频 |
| 媒体处理 | **ffmpeg** | 视频转码、音频提取、关键帧截取 |
| 语音转写 | **faster-whisper** (small model) | 视频口播文案转写 |
| 多模态分析 | **OpenAI SDK** → Seed 2.0 Responses API | 视频画面+文案综合分析 |
| 持久化 | 纯 Python (json) | 本地文件存储 |
| Agent 集成 | 命令行 JSON 输出 | 任何 agent 可调用 |

## 目录结构

```
diagnosis/
├── bridge/                         # 数据采集层
│   ├── videoagent_bridge.py        # CLI 入口 (main)
│   ├── mediacrawler_runner.py      # MediaCrawler 调用器
│   ├── video_analyzer.py           # 视频分析 (下载→转写→多模态)
│
├── references/                     # 诊断知识库
│   ├── account-analysis-framework.md  # 6 维度分析框架
│   ├── benchmark-data.md              # 赛道基准数据
│   └── operation_manual.md            # 可落地改进操作手册
│
├── distillation/                   # 知识蒸馏 (研究过程，非运行时依赖)
│   ├── batch_audio_extract.py      # 批量音频提取（研究脚本，非核心 CLI）
│   ├── cross_validation_report.md  # 114 条视频交叉验证报告
│   ├── search_terms.md             # 搜索词策略
│   ├── synthesized/                # 蒸馏合成输出
│   └── raw_data/                   # 原始采集数据
│
├── store/                          # 持久化存储
│   ├── storage.py                  # AccountStorage 工具模块
│   └── accounts/{昵称}/            # 每个账号的数据目录
│       ├── raw_data/               # 原始抓取数据
│       ├── analysis/               # 视频分析结果
│       └── reports/                # 诊断报告 (HTML)
│
├── SKILL.md                        # Claude Code 集成指南
├── ARCHITECTURE.md                 # 本文档
├── .env.example                    # 环境变量模板
└── README.md                       # 项目说明
```

## 核心实现说明

### 数据采集 (Bridge 层)

通过 `subprocess` 调用 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) CLI，读取其输出的 JSONL 文件并按 `sec_uid` 过滤。

```
fetch-creator 流程:
  1. 解析创作者主页 URL → sec_uid
  2. 检查当日缓存 (JSONL 文件 + sec_uid 匹配)
  3. 缓存命中 → 直接读缓存
  4. 缓存未命中 → subprocess 调用 MediaCrawler CLI (600s 超时)
  5. 读取 CLI 输出 → 按 sec_uid 过滤 → 去重 → 标准化字段
  6. 自动持久化到 store/accounts/{昵称}/raw_data/
  7. 数据充分性判定 (时间跨度 / 密度 / 缺口)
```

抓取无硬性上限，CLI 跑到 600 秒超时为止，能抓多少抓多少。自动评估数据覆盖充分性。

### 视频分析 (VideoAnalyzer)

```
analyze 流程:
  1. yt-dlp 下载视频 (限 90 秒, 50MB)
  2. ffmpeg 提取音频 (16kHz WAV) + 关键帧 (每 10 秒一张)
  3. faster-whisper small 转写音频 → 口播文案
  4. 可选: 多模态 LLM 分析 (画面+文案 → 结构化分析)
  5. 清理临时文件
  6. 返回 VideoAnalysisResult
```

### 多模态分析提供者

当前实现使用 **字节跳动 Seed 2.0** (ark.cn-beijing.volces.com) 的 Responses API，通过 `openai` Python SDK 调用。

**配置方式**：在 `.env` 中设置以下变量，或实例化 `VideoAnalyzer` 时传入 config：

```python
analyzer = VideoAnalyzer({
    "seed2_api_key": "your_key",
    "seed2_base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "seed2_model": "your_model_name",
})
```

**降级路径**：

| 配置状态 | 行为 |
|---------|------|
| 配了 seed2_api_key + seed2_model | 完整多模态分析 (画面+文案) |
| 未配 seed2，但有 faster-whisper | 仅文案转写，无视觉分析 |
| 两者都无 | 仅视频下载+帧提取，无分析 |

**扩展至其他 LLM**：`ContentAnalyzer` 当前用 OpenAI SDK 的 Responses API，不适合直接替换为其他提供者。如需支持 GPT-4o、Claude、Gemini 等，需在 `ContentAnalyzer` 层做抽象：

```
ContentAnalyzer (ABC)
├── Seed2Analyzer    ← 当前实现
├── OpenAIAnalyzer   ← chat.completions + gpt-4o
├── ClaudeAnalyzer   ← Anthropic Messages API
└── GeminiAnalyzer   ← Google Generative AI SDK
```

配置 `LLM_PROVIDER=seed2|openai|claude|gemini` 切换。

### 数据持久化

每次 `fetch-creator` 自动保存：
- `raw_data/creator_{date}.json` — 抓取元信息 + 数据充分性评估
- `raw_data/videos_{date}.json` — 去重后的标准化视频列表

每次 `analyze-video` / `extract-audio` 自动保存（需指定 account_name）：
- `analysis/transcripts/{video_id}.txt` — 口播文案
- `analysis/video_analysis/{video_id}.json` — 分析结果

最终 HTML 报告保存至：
- `reports/diagnosis_{date}.html`

### 诊断框架 (SKILL.md)

目前仅支持**抖音**平台。分析覆盖 6 个维度：

1. **赛道定位** — 主赛道、目标人群、人设、垂直度
2. **内容策略** — 内容类型、钩子、结构、CTA
3. **数据表现** — 代理指标（藏赞比/转赞比/赞评比）、流量池层级
4. **运营细节** — 发布规律、标题 SEO、标签策略
5. **商业化** — 变现路径、商单占比
6. **平台适配** — 规则遵从、算法适配、搜索流量

备用指标详见 `references/benchmark-data.md`。
