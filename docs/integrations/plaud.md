# Plaud API 接入指南

> ⚠️ Plaud API 为非官方逆向接口，可能随时变更。使用风险自负。

## 概述

Plaud NotePin 录音笔将录音自动上传到 Plaud 云端。通过非官方 API，可以将录音、转录和 AI 笔记批量下载到本地，然后交给本管线处理。

## 获取 Token

1. 打开 [Plaud Web App](https://app.plaud.ai) 并登录
2. 打开浏览器开发者工具 (F12) → Network 标签
3. 刷新页面，找到任意 API 请求（如 `/user/me`）
4. 复制请求头中的 `Authorization: Bearer <token>`
5. 保存 token 到 `~/.hermes/plaud_token`

## 同步脚本

以下脚本将 Plaud 云端录音同步到本地目录：

```python
#!/usr/bin/env python3
"""Sync Plaud cloud → local recordings/ + transcripts/ + ai_notes/"""
import json, os, gzip, requests
from datetime import datetime
from pathlib import Path

API_BASE = "https://api.plaud.ai"
BASE = os.path.expanduser("~/plaud-knowledge-base")

def main():
    token = open(Path.home() / ".hermes" / "plaud_token").read().strip()
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    
    # ... (完整实现见原 plaud_daily_pipeline.py step1_sync)

if __name__ == "__main__":
    main()
```

完整实现见原 `plaud_daily_pipeline.py` 的 `step1_sync()` 函数（已从开源版移除）。

## 使用流程

```bash
# 1. 先用同步脚本下载云端的录音
python plaud_sync.py

# 2. 然后跑主管线
python scripts/pipeline.py
```

## 已知限制

- 接口非官方，可能变动
- 大文件下载可能超时
- 部分录音可能因云端处理未完成而缺少转录/笔记
