# 📥 Plaud Pipeline

> 从会议录音到可交互知识库的全自动管线 — 录音 → 分割 → 具名 → 纪要 → Wiki → RAG

一个**全自动化管线**，将会议录音经过说话人分割、身份具名、AI 纪要合并、知识库 Wiki 生成，最终呈现为 Notion 风格的 Web 知识库，支持全文搜索和 RAG 问答。

```
录音(.ogg)  →  [①扫描]  →  [②分割]  →  [③具名]  →  [④纪要]  →  [⑥Wiki]  →  🌐 Web 知识库
                                  pyannote    3-phase     LLM        LLM        + RAG Q&A
                                  + FunASR    漏斗                     合成        + 说话人编辑器
```

---

## 特性

- 🎙️ **GPU 加速说话人分割** — pyannote community-1 + FunASR Paraformer，~28x 实时
- 🏷️ **94% 说话人具名率** — 3 阶段漏斗：Plaud 标签 → 声纹传播 → LLM 推断
- 📝 **结构化 AI 纪要** — 含决策表、行动项（精准分配到人）、议题要点
- 🏠 **Notion 风格 Wiki** — 人物档案、客户档案、决策日志、行动项看板、时间线
- 💬 **RAG 知识库问答** — Bigram 中文搜索 + LLM 答案生成（带引用来源）
- 🔄 **幂等自愈** — 每步独立检查输入/输出，中断后重跑不丢进度

---

## 前置条件

- **OS**: Ubuntu 22.04+ / macOS (Apple Silicon)
- **GPU**: NVIDIA GPU ≥ 8GB 显存（pyannote 需要；Apple MPS 也可用）
- **Python**: 3.11+
- **ffmpeg**: `apt install ffmpeg`
- **HuggingFace**: 注册账号并接受 [pyannote 模型使用协议](https://huggingface.co/pyannote/speaker-diarization-community-1)
- **LLM**: [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) 或 OpenAI 兼容接口

---

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/yourname/plaud-pipeline
cd plaud-pipeline

# 2. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. 配置
cp config/.env.example .env
cp config/org_chart.example.md config/org_chart.md
# 编辑 .env 填入你的 LLM 配置
# 编辑 config/org_chart.md 填入你的组织架构

# 4. 丢录音进 recordings/
cp /path/to/your/meeting.ogg recordings/

# 5. 运行管线
python scripts/pipeline.py

# 6. 启动 Wiki 浏览
python scripts/wiki_server.py --port 8899
# 打开 http://localhost:8899
```

---

## 管线步骤

| 步骤 | 功能 | 技术 | 输入 → 输出 |
|------|------|------|-------------|
| ① 扫描 | 检测新录音 | 文件系统扫描 | `recordings/*.ogg` → new IDs |
| ② 分割 | 说话人分割 + ASR | pyannote + FunASR (GPU) | `.ogg` → `transcripts_diarized/*.json` |
| ③ 具名 | 说话人身份识别 | 3-phase 漏斗 | diarized → `transcripts_anchor_named/*.json` |
| ④ 纪要 | AI 合并纪要 | LLM | named + Plaud notes → `ai_notes_merged/*.md` |
| ⑤ 通知 | 每日摘要推送 | Telegram Bot | → 推送消息 |
| ⑥ Wiki | 知识库生成 | Python + LLM | 纪要 → `wiki/` |

---

## 配置说明

| 环境变量 | 必需 | 说明 |
|----------|------|------|
| `CLAUDE_BIN` | 是 | Claude CLI 路径（默认: `claude`） |
| `TELEGRAM_BOT_TOKEN` | 否 | Telegram Bot Token（不设则跳过通知） |
| `TELEGRAM_HOME_CHANNEL` | 否 | Telegram 频道 ID |
| `PLAUD_DATA_DIR` | 否 | 数据目录（默认: `~/plaud-knowledge-base`） |
| `PLAUD_CONFIG_DIR` | 否 | 配置目录（默认: `~/plaud-pipeline/config`） |
| `HF_HUB_OFFLINE` | 否 | 设为 `1` 使用离线模型缓存 |

---

## 已知限制

- **GPU 必需** — CPU 模式下 pyannote 极慢（>100x 实时）
- **中文 only** — FunASR Paraformer 专为中文优化
- **声纹局限** — CAM++ 跨录音相似度 0.50-0.65，同录音不同人可达 0.97。不能纯靠声纹
- **Plaud API** — 非官方接口，见 `docs/integrations/plaud.md`。本仓库只提供 FolderSource

---

## 文档

- [架构设计与实现原理](docs/architecture.md)
- [Plaud API 接入指南](docs/integrations/plaud.md)

---

## 协议

Apache License 2.0 — 详见 [LICENSE](LICENSE)
