# 📥 Plaud Pipeline — 架构设计与实现原理

> 从智能录音笔的海量会议录音，到可交互的知识库全自动管线。
> 2026 年 5 月，生产级运行中。

📦 开源代码：[github.com/xclgordon/plaud-pipeline](https://github.com/xclgordon/plaud-pipeline)

---

## 快速开始

### 前置条件

- Ubuntu 22.04+ / macOS
- NVIDIA GPU ≥ 8GB（pyannote 分割需要）
- Python 3.11
- HuggingFace token（需接受 [pyannote 模型使用协议](https://huggingface.co/pyannote/speaker-diarization-3.1)）

### 5 分钟跑起来

```bash
git clone https://github.com/xclgordon/plaud-pipeline
cd plaud-pipeline

# 安装依赖
pip install -r requirements.txt

# 配置
cp config/.env.example .env
cp config/org_chart.example.md config/org_chart.md
# 编辑 .env 填入 HF_TOKEN 和 LLM 配置

# 把录音文件丢进去（支持 .ogg / .mp3 / .wav）
cp your-recordings/*.ogg ~/plaud-knowledge-base/recordings/

# 运行
python scripts/pipeline.py

# 启动 Wiki（浏览器打开 http://localhost:8899）
python scripts/wiki_server.py --port 8899
```

### 已知限制

- 需要 NVIDIA GPU，CPU 模式下 pyannote 极慢（实时比约 0.3x）
- 声纹跨录音准确率受录音环境影响，建议搭配外部地面真相（如 Plaud 标签）
- 目前针对中文会议优化，其他语言未测试
- Plaud API 接入为非官方实现，详见 [docs/integrations/plaud.md](docs/integrations/plaud.md)

---

## 一、项目概览

### 这是什么？

一个**全自动化管线**，将会议录音经过文件夹扫描 → 说话人分割 → 身份具名 → AI 纪要合并 → Wiki 知识库生成，最终呈现为 Notion 风格 Web 知识库。

每天自动运行，无需人工干预。核心价值：**把"听过就忘"的会议，变成可检索、可追溯的组织记忆。**

### 与开源方案的差异

GitHub 上有两类相关项目，但**没有一个做全链路**：

| 类型 | 代表项目 | 做了什么 | 缺了什么 |
|------|----------|----------|----------|
| 音频管线 | WhisperX, ownscribe, Speechlib | 录音 → 分割 → 转录 | 无说话人具名漏斗、无知识库生成 |
| 知识库 | SwarmVault, WeKnora, LLM Wiki | 文档 → Wiki → RAG | 无音频处理、无说话人识别 |
| **本方案** | — | **录音 → 具名 → 纪要 → Wiki → RAG** | **全链路闭环** |

---

## 二、核心设计原则

### 1. 每一步独立幂等（Survive Partial Failures）

管线可能在任一步骤中断（GPU OOM / API 超时 / 网络中断）。每步**不依赖上一步的返回值**，而是自行对比输入/输出目录，找出"有输入但没有输出"的文件来处理：

```python
# ✅ 独立检查，而非依赖上一步传来的 new_ids
all_oggs = set(f.replace('.ogg', '') for f in os.listdir(REC_DIR) if f.endswith('.ogg'))
all_diar = set(f.replace('.json', '') for f in os.listdir(DIAR_DIR) if f.endswith('.json'))
needing = all_oggs - all_diar
```

### 2. 身份识别漏斗（Identity Funnel）

说话人具名不依赖单一技术，而是**置信度逐级下降的多层漏斗**：

| 优先级 | 来源 | 置信度 | 方法 |
|--------|------|--------|------|
| 1 | 外部标签匹配 | ~100% | 时间戳对齐具名标注 → spk_id |
| 2 | 声纹传播 | >0.55 cos | CAM++ embedding + 余弦相似度匹配已知声纹库 |
| 3 | LLM 推断 | 中 | LLM + 组织架构图 + 共现关系 + 称呼惯例 |
| 4 | 兜底 | — | spk_N |

### 3. 地面真相优先策略

CAM++ 声纹跨录音不可靠（同一人在不同录音的余弦相似度 0.50–0.65，反而不如不同人在同一录音 0.85–0.97）。**外部标签命中了 95%，声纹传播填补剩余，LLM 处理最后边缘情况。**

### 4. 分割用 pyannote，声纹用 CAM++

CAM++ 的问题在**分割**环节（27s 独白拆成 5 人），但 **embedding 质量不错**：
- **分割** → pyannote community-1（GPU，~28x 实时）
- **声纹提取** → CAM++（192 维 embedding）

---

## 三、系统架构

```
                       ┌─────────────────────────────────────┐
                       │         crontab (22:00 daily)         │
                       └──────────────┬──────────────────────┘
                                      │
    ┌────▼──────────┐    ┌────────────▼───────┐    ┌───────▼────────┐
    │ ① Source Input │    │ ② Diarize           │    │ ③ Name         │
    │ (FolderSource) │───▶│ pyannote +          │───▶│ 3-Phase        │
    │ recordings/    │    │ FunASR (GPU)        │    │ Anchor Naming  │
    └───────────────┘    └────────────────────┘    └───────┬────────┘
                                                           │
    ┌────▼──────┐    ┌────────────────┐    ┌──────────────▼─────────┐
    │ ④ AI Notes│───▶│ ⑤ Notify       │───▶│ ⑥ Wiki Rebuild         │
    │   Merge   │    │ (可配置)        │    │ (only when new)        │
    └──────────┘    └────────────────┘    └──────────────┬─────────┘
                                                         │
                                              ┌──────────▼──────────┐
                                              │ Wiki Server (8899)  │
                                              │ Notion-style UI     │
                                              │ Bigram RAG Q&A      │
                                              │ Speaker Editor      │
                                              └─────────────────────┘
```

---

## 四、说话人具名漏斗详解

### Phase 1: 外部标签匹配（95% 命中率）

通过时间戳将外部具名标注（如 Plaud 标签）对齐到 pyannote 的 spk_id，直接建立映射。**地面真相，100% 可靠。**

### Phase 2: 声纹传播（CAM++ Cosine）

提取已知说话人的 CAM++ 192 维 embedding，余弦相似度 ≥0.55 → 自动标注姓名。

### Phase 3: LLM 推断（兜底）

LLM + 组织架构图 + 共现关系 + 中文称呼惯例（"X总"=姓氏+总，"老X"=熟悉称呼），cap=8 候选。

**结果：94% 片段具名率（56,862 / 60,664）**

---

## 五、说话人分割选型：pyannote vs CAM++

| 维度 | CAM++ (旧) | pyannote community-1 (新) |
|------|------------|---------------------------|
| 分割粒度 | 单人独白拆成 5 个 spk | 正确识别为 1 人 |
| 平均 spk/录音 | 9.8 | 5.4（↓45%） |
| 极端崩坏 | 37 人（实际 ~3 人） | 最差 16 人（实际 ~6 人） |

**选型决策**：准确率 > 速度。垃圾进垃圾出，分割错了后续 pipeline 救不回来。

---

## 六、AI 纪要合并

LLM 输入具名转录 + 原始笔记，输出统一格式纪要：
1. 会议摘要（2-3 句话）
2. 关键决策（表格）
3. 行动项（表格，精准分配到人）
4. 后续跟进
5. 议题要点

**关键价值**：94% 具名率 → 行动项从"某人要做某事"变成"张经理需在周五前提交报价"。

---

## 七、知识库 Wiki 生成

```
ai_notes_merged/*.md
  → Parse（Python regex: 人员/决策/行动项）
  → Aggregate（Python Counter）
  → Synthesize（LLM batch ~26 calls: INDEX + 人物 + 公司）
  → Static（Python: 决策日志 + 行动看板 + 时间线）
  → wiki/
```

---

## 八、Wiki Web 与 RAG 问答

- **Web Server**: 纯 Python `http.server` + `mistune`，零框架依赖
- **RAG**: Bigram 中文分词 → 搜索 132 文件 → Top 6 → LLM 生成回答带引用
- **说话人编辑器**: 转录原文 + HTML5 音频播放器 + 声纹传播

---

## 九、技术栈

| 层次 | 技术 |
|------|------|
| 录音来源 | 任意 OGG / MP3 / WAV 文件（Plaud 接入见 integrations/） |
| 说话人分割 | pyannote community-1 (GPU) |
| ASR | FunASR Paraformer-large |
| 声纹提取 | FunASR CAM++ 192-dim |
| LLM | OpenAI 兼容接口（Claude Sonnet / GPT / 本地 Ollama 均可） |
| 编排 | Python 3.11 + crontab |
| 知识库 UI | HTTP + mistune + CSS |
| 中文搜索 | 自定义 Bigram 分词 |

---

## 十、经验教训

### 1. 声纹技术不可迷信

同一人跨录音 cosine 0.50–0.65，不同人同录音 0.97。阈值 0.65 → 同一人被拆成 17 个簇。**有高质量人工标注就用。**

### 2. 漏斗优于单点方案

每层只做自己擅长的事：外部标签→确定性匹配，声纹→概率匹配，LLM→语义兜底。

### 3. 中文搜索需要 Bigram

无空格中文："广东华为" → ["广东","东华","华为"]，无需引入 jieba。

### 4. LLM JSON 多策略解析

Code fence → bare JSON → trailing comma cleanup。单一策略会静默丢弃结果。

### 5. pyannote 对 OGG 敏感

先 ffmpeg 转 16kHz WAV 最稳妥，避免分割边界漂移。

### 6. GPU 显存管理

54 分钟音频不带 VAD → 43GB → OOM。**始终启用 fsmn-vad 分段处理。**

---

## 十一、总结

> **不要试图用一个完美的算法解决所有问题。**
> **用多层漏斗，每一层只做自己擅长的事。**
> **每一步独立幂等，容忍部分失败。**
> **有高质量人工标注就用，不要为了"纯 AI"而拒绝地面真相。**

最终结果：全自动、可恢复、可交互的会议知识管理系统 — 数百段录音，94% 具名率，0 人工维护。

---

📦 开源代码：[github.com/xclgordon/plaud-pipeline](https://github.com/xclgordon/plaud-pipeline)

_最后更新：2026-05-12_
