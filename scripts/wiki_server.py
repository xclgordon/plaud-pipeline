#!/usr/bin/env python3
"""
Plaud Wiki Server — Notion-style knowledge base browser
═══════════════════════════════════════════════════════
Serves ~/plaud-knowledge-base/wiki/ as a beautiful web interface.
Start: python3 plaud_wiki_server.py [--port 8899]

Open http://localhost:8899 in your browser.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote, urlparse, parse_qs

import mistune

# ══════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════

WIKI_DIR = os.path.expanduser("~/plaud-knowledge-base/wiki")
NOTES_DIR = os.path.expanduser("~/plaud-knowledge-base/ai_notes_merged")
CONCEPTS_DIR = os.path.expanduser("~/plaud-knowledge-base/concepts")
KB_DIR = os.path.expanduser("~/plaud-knowledge-base")
CLAUDE_BIN = os.path.expanduser("~/.nvm/versions/node/v20.20.2/bin/claude")
PORT = 8899

# Speaker editing paths
ANCHOR_DB = os.path.expanduser("~/.hermes/experiments/voiceprints/anchor_db.json")
NAMED_TRANSCRIPTS = os.path.join(KB_DIR, "transcripts_anchor_named")
DIARIZED_TRANSCRIPTS = os.path.join(KB_DIR, "transcripts_diarized")
ORIG_TRANSCRIPTS = os.path.join(KB_DIR, "transcripts")
REC_DIR = os.path.join(KB_DIR, "recordings")

# Searchable directories → label
SEARCH_DIRS = [
    (WIKI_DIR, "wiki"),
    (NOTES_DIR, "会议纪要"),
    (CONCEPTS_DIR, "概念"),
]

# Cache: file_id → (title, content)
_search_cache = {}
_search_cache_mtime = 0

# Sidebar nav structure
NAV = [
    {"emoji": "🏠", "label": "首页", "href": "/", "sort": 0},
    {"emoji": "👤", "label": "人物", "href": "/people/", "sort": 1},
    {"emoji": "🏢", "label": "公司", "href": "/companies/", "sort": 2},
    {"emoji": "✅", "label": "决策日志", "href": "/decisions/", "sort": 3},
    {"emoji": "🔴", "label": "行动项", "href": "/actions/", "sort": 4},
    {"emoji": "📅", "label": "时间线", "href": "/timeline/", "sort": 5},
    {"emoji": "✏️", "label": "编辑说话人", "href": "/speakers/", "sort": 6},
]

# Markdown → HTML
md = mistune.create_markdown(escape=False, plugins=['strikethrough', 'table'])

# ══════════════════════════════════════════════════════════
# RAG: Knowledge Base Search + Ask
# ══════════════════════════════════════════════════════════

def _load_search_index():
    """Load all searchable files into memory cache (lazy refresh)."""
    global _search_cache, _search_cache_mtime
    # Check if any file changed
    newest = 0
    for sdir, _ in SEARCH_DIRS:
        if not os.path.isdir(sdir):
            continue
        for root, _, files in os.walk(sdir):
            for fn in files:
                if fn.endswith('.md'):
                    mtime = os.path.getmtime(os.path.join(root, fn))
                    if mtime > newest:
                        newest = mtime
    if newest <= _search_cache_mtime and _search_cache:
        return _search_cache

    _search_cache = {}
    for sdir, label in SEARCH_DIRS:
        if not os.path.isdir(sdir):
            continue
        for root, _, files in os.walk(sdir):
            for fn in files:
                if fn.endswith('.md'):
                    fpath = os.path.join(root, fn)
                    try:
                        with open(fpath, encoding='utf-8') as f:
                            content = f.read(50000)
                    except Exception:
                        continue
                    # Extract title from first heading
                    title = fn.replace('.md', '').replace('-', ' ')
                    m = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
                    if m:
                        title = m.group(1).strip()
                    rel = os.path.relpath(fpath, sdir)
                    file_id = f"{label}/{rel}"
                    _search_cache[file_id] = {
                        "title": title,
                        "content": content,
                        "label": label,
                        "path": fpath,
                        "rel": rel,
                    }
    _search_cache_mtime = newest
    return _search_cache


def _tokenize_query(query: str) -> list[str]:
    """Split query into searchable tokens for Chinese+English."""
    tokens = []
    # Split on whitespace and Chinese punctuation
    parts = re.split(r'[\s,，、。；：？！\?\!\n]+', query)
    for part in parts:
        part = part.strip().lower()
        if not part:
            continue
        # If contains Chinese, also add bigrams for partial matching
        if re.search(r'[\u4e00-\u9fff]', part) and len(part) >= 2:
            tokens.append(part)  # Full phrase
            # Add bigrams for Chinese
            for i in range(len(part) - 1):
                tokens.append(part[i:i+2])
        else:
            tokens.append(part)
    # Remove duplicates while preserving order
    seen = set()
    result = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def search_kb(query: str, top_k: int = 8) -> list[dict]:
    """Search knowledge base for relevant documents."""
    index = _load_search_index()
    keywords = _tokenize_query(query)
    if not keywords:
        return []

    scored = []
    for file_id, info in index.items():
        content_lower = info["content"].lower()
        score = 0
        for kw in keywords:
            score += content_lower.count(kw)
        if score > 0:
            scored.append((score, file_id, info))
    scored.sort(key=lambda x: -x[0])
    scored = scored[:top_k]

    results = []
    for score, file_id, info in scored:
        snippets = _extract_snippets(info["content"], keywords, max_snippets=3)
        results.append({
            "title": info["title"],
            "label": info["label"],
            "path": info["path"],
            "rel": info["rel"],
            "snippets": snippets,
            "score": score,
        })
    return results


def _extract_snippets(content: str, keywords: list[str], max_snippets: int = 3,
                      context_chars: int = 200) -> list[str]:
    """Extract relevant text snippets around keyword matches."""
    content_lower = content.lower()
    positions = []
    for kw in keywords:
        idx = 0
        while True:
            idx = content_lower.find(kw, idx)
            if idx == -1:
                break
            positions.append(idx)
            idx += len(kw)
    if not positions:
        return [content[:context_chars * 2] + "..."]

    positions.sort()
    # Cluster nearby positions
    clusters = []
    current = [positions[0]]
    for p in positions[1:]:
        if p - current[-1] < context_chars * 2:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    if current:
        clusters.append(current)

    snippets = []
    for cluster in clusters[:max_snippets]:
        center = sum(cluster) // len(cluster)
        start = max(0, center - context_chars // 2)
        end = min(len(content), center + context_chars // 2)
        snippet = content[start:end].strip()
        # Clean up: remove markdown formatting for display
        snippet = re.sub(r'[*#`|>-]', ' ', snippet)
        snippet = re.sub(r'\s+', ' ', snippet)
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."
        snippets.append(snippet)

    return snippets


def ask_claude(question: str, sources: list[dict]) -> dict:
    """Ask Claude to answer based on knowledge base sources."""
    if not sources:
        return {"answer": "知识库中未找到相关内容。", "sources": []}

    # Build context from top sources: re-read full content from paths
    context_parts = []
    for i, s in enumerate(sources[:6]):
        source_id = i + 1
        # Re-read content
        try:
            with open(s["path"], encoding='utf-8') as f:
                content = f.read(5000)
        except Exception:
            content = s.get("snippets", [""])[0]
        context_parts.append(
            f"## 来源 [{source_id}]: {s['title']} ({s['label']})\n"
            f"{content[:3000]}\n"
        )
    context = "\n\n".join(context_parts)

    prompt = f"""你是一个知识库助手，专门回答会议纪要和业务知识。

## 知识库内容

{context}

## 用户问题

{question}

## 回答要求

1. **基于上述知识库内容回答**，不要编造内容
2. **必须在回答中标注引用来源**，使用 `[1]` `[2]` 格式
3. 如果知识库内容不足以回答，说明缺少哪些信息
4. **格式**: 先给出简洁回答，再列出要点；引用要精确到来源编号
5. 用中文回答

## 回答格式示例

**核心结论**: xxx

- 要点1: xxx [1]
- 要点2: xxx [2]

### 来源
[1] 会议纪要 - 2026-04-03 Q1回顾
[2] wiki - 华为生态合作
"""

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--input-format", "text",
             "--max-turns", "3", "--model", "claude-sonnet-4-6"],
            input=prompt, capture_output=True, text=True, timeout=120
        )
        answer = result.stdout.strip()
        if not answer:
            answer = "AI 生成失败，请稍后重试。"
    except subprocess.TimeoutExpired:
        answer = "回答超时，请尝试更具体的问题。"
    except FileNotFoundError:
        answer = "Claude CLI 未找到，请检查路径配置。"
    except Exception as e:
        answer = f"回答生成出错: {e}"

    # Build source list for frontend
    source_list = []
    for i, s in enumerate(sources[:6]):
        # Read full content for inline display
        try:
            with open(s["path"], encoding='utf-8') as f:
                full_text = f.read(10000)
        except Exception:
            full_text = ""

        # Build best link: wiki page for wiki sources, raw file for notes
        if s["label"] == "wiki":
            link = "/" + s["rel"].replace('.md', '')
        else:
            # For 会议纪要 and 概念, link directly to raw source
            link = f"/raw/{s['label']}/{s['rel']}"

        # Extract contextual snippet around keywords (larger window)
        keywords = _tokenize_query("")  # reuse from search context - actually can't, no query here
        # Just show the first 300 chars as preview
        snippet_preview = full_text[:300].strip() if full_text else ""
        # Clean for display
        snippet_preview = re.sub(r'[#*`|>-]', ' ', snippet_preview[:300])
        snippet_preview = re.sub(r'\s+', ' ', snippet_preview).strip()

        source_list.append({
            "id": i + 1,
            "title": s["title"],
            "label": s["label"],
            "snippet": snippet_preview,
            "full": full_text[:5000],  # Full text for inline expansion
            "link": link,
            "snippets": s.get("snippets", [snippet_preview]),
        })

    return {"answer": answer, "sources": source_list}

def get_crumb(path: str) -> list:
    """Generate breadcrumb from path."""
    parts = [p for p in path.strip('/').split('/') if p]
    crumbs = [{"label": "首页", "href": "/"}]
    for i, p in enumerate(parts):
        href = "/" + "/".join(parts[:i+1]) + "/"
        crumbs.append({"label": p.replace('.md', '').replace('-', ' '), "href": href})
    return crumbs

def get_subpages(path: str) -> list:
    """List subpages for a directory."""
    dir_path = os.path.join(WIKI_DIR, path.strip('/'))
    if not os.path.isdir(dir_path):
        return []
    items = []
    for f in sorted(os.listdir(dir_path)):
        if f.endswith('.md') and f != 'INDEX.md':
            name = f.replace('.md', '').replace('-', ' ')
            href = f"/{path.strip('/')}/{f.replace('.md', '')}"
            items.append({"label": name, "href": href, "ext": ".md"})
    return items

def render_markdown(content: str) -> str:
    """Convert markdown to clean HTML."""
    # Strip generation comment
    content = re.sub(r'<!-- Generated:.*?-->\n?', '', content)
    return md(content)

# ══════════════════════════════════════════════════════════
# HTML Template (Notion-inspired)
# ══════════════════════════════════════════════════════════

CSS = """
:root {
    --bg: #ffffff;
    --bg-warm: #f6f5f4;
    --text: rgba(0,0,0,0.92);
    --text-muted: #615d59;
    --text-faint: #a39e98;
    --blue: #0075de;
    --blue-hover: #005bab;
    --blue-bg: #f2f9ff;
    --blue-text: #097fe8;
    --border: rgba(0,0,0,0.08);
    --shadow-card: 0 1px 3px rgba(0,0,0,0.04), 0 3px 7px rgba(0,0,0,0.03), 0 7px 15px rgba(0,0,0,0.02);
    --radius-sm: 4px;
    --radius: 8px;
    --radius-lg: 12px;
    --sidebar-w: 260px;
    --font: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font);
    color: var(--text);
    background: var(--bg-warm);
    line-height: 1.6;
    display: flex;
    min-height: 100vh;
}
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Sidebar */
.sidebar {
    width: var(--sidebar-w);
    background: var(--bg);
    border-right: 1px solid var(--border);
    padding: 24px 20px;
    position: fixed;
    top: 0; left: 0; bottom: 0;
    overflow-y: auto;
    z-index: 10;
}
.sidebar-logo {
    font-size: 18px; font-weight: 700;
    color: var(--text);
    margin-bottom: 8px;
    letter-spacing: -0.3px;
}
.sidebar-sub {
    font-size: 13px; color: var(--text-faint);
    margin-bottom: 28px;
}
.nav-section { margin-bottom: 6px; }
.nav-link {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px;
    border-radius: var(--radius);
    color: var(--text-muted);
    font-size: 14px; font-weight: 500;
    transition: all 0.15s;
}
.nav-link:hover {
    background: var(--bg-warm);
    color: var(--text);
    text-decoration: none;
}
.nav-link.active {
    background: var(--blue-bg);
    color: var(--blue-text);
    font-weight: 600;
}
.nav-emoji { font-size: 16px; width: 22px; text-align: center; flex-shrink: 0; }

/* Subpages in sidebar */
.subpages { margin-left: 32px; margin-bottom: 8px; }
.sub-link {
    display: block;
    padding: 5px 12px;
    border-radius: var(--radius-sm);
    color: var(--text-faint);
    font-size: 13px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sub-link:hover { background: var(--bg-warm); color: var(--text); text-decoration: none; }
.sub-link.active { background: var(--blue-bg); color: var(--blue-text); font-weight: 500; }

/* Main content */
.main {
    margin-left: var(--sidebar-w);
    flex: 1;
    padding: 40px 48px;
    max-width: 900px;
}
.main.full { max-width: 1100px; }

/* Breadcrumb */
.breadcrumb {
    display: flex; gap: 6px; align-items: center;
    font-size: 13px; color: var(--text-faint);
    margin-bottom: 24px;
}
.breadcrumb a { color: var(--text-faint); }
.breadcrumb a:hover { color: var(--text); }
.breadcrumb .sep { color: var(--border); }

/* Typography */
h1 { font-size: 32px; font-weight: 700; letter-spacing: -0.8px; margin-bottom: 8px; line-height: 1.15; }
h2 { font-size: 22px; font-weight: 700; letter-spacing: -0.25px; margin: 32px 0 12px; line-height: 1.25; }
h3 { font-size: 17px; font-weight: 600; margin: 24px 0 8px; color: var(--text-muted); }
h4 { font-size: 15px; font-weight: 600; margin: 16px 0 6px; }
p { margin-bottom: 10px; }
ul, ol { padding-left: 24px; margin-bottom: 12px; }
li { margin-bottom: 4px; }
strong { font-weight: 600; color: rgba(0,0,0,0.95); }

/* Tables */
table {
    width: 100%; border-collapse: collapse;
    margin: 16px 0;
    font-size: 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
}
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: var(--bg-warm); font-weight: 600; font-size: 13px; color: var(--text-muted); }
tr:last-child td { border-bottom: none; }

/* Code */
code {
    font-family: var(--font-mono);
    font-size: 13px;
    background: var(--bg-warm);
    padding: 2px 6px;
    border-radius: var(--radius-sm);
}
pre {
    background: var(--bg-warm);
    padding: 16px;
    border-radius: var(--radius);
    overflow-x: auto;
    margin: 12px 0;
    font-size: 13px;
}
pre code { background: none; padding: 0; }

/* Cards */
.card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 16px;
    box-shadow: var(--shadow-card);
}
.card h3 { margin-top: 0; }

/* Status badges */
.badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 9999px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.125px;
}
.badge-pending { background: #fff3e0; color: #dd5b00; }
.badge-done { background: #e8f5e9; color: #1aae39; }

/* Responsive */
@media (max-width: 768px) {
    .sidebar { width: 100%; position: relative; height: auto; border-right: none; border-bottom: 1px solid var(--border); padding: 16px; }
    .main { margin-left: 0; padding: 24px 20px; }
    .subpages { display: none; }
}

/* ── Chat UI ── */
.chat-btn {
    position: fixed; bottom: 28px; right: 28px;
    width: 52px; height: 52px;
    border-radius: 50%;
    background: var(--blue);
    color: #fff;
    border: none;
    font-size: 24px;
    cursor: pointer;
    box-shadow: 0 4px 16px rgba(0,117,222,0.35);
    z-index: 1000;
    transition: all 0.2s;
    display: flex; align-items: center; justify-content: center;
}
.chat-btn:hover { transform: scale(1.08); box-shadow: 0 6px 20px rgba(0,117,222,0.45); }
.chat-badge {
    position: absolute; top: -4px; right: -4px;
    width: 12px; height: 12px;
    border-radius: 50%;
    background: #ff3b30;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.chat-panel {
    position: fixed; bottom: 92px; right: 28px;
    width: 420px; height: 560px;
    max-height: calc(100vh - 140px);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: 0 8px 40px rgba(0,0,0,0.12);
    z-index: 999;
    display: none;
    flex-direction: column;
    overflow: hidden;
}
.chat-panel.open { display: flex; }
.chat-header {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
    background: var(--bg-warm);
}
.chat-header h3 { font-size: 15px; font-weight: 600; margin: 0; color: var(--text); }
.chat-close {
    background: none; border: none;
    font-size: 20px; color: var(--text-faint);
    cursor: pointer; padding: 4px 8px; border-radius: 4px;
}
.chat-close:hover { background: var(--border); color: var(--text); }
.chat-messages {
    flex: 1; overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 12px;
}
.chat-empty {
    text-align: center; color: var(--text-faint);
    margin-top: 60px; font-size: 14px;
}
.chat-empty .icon { font-size: 40px; margin-bottom: 8px; display: block; }
.msg {
    max-width: 88%; padding: 10px 14px;
    border-radius: 14px;
    font-size: 14px; line-height: 1.55;
    animation: fadeIn 0.25s;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.msg.user {
    align-self: flex-end;
    background: var(--blue); color: #fff;
    border-bottom-right-radius: 4px;
}
.msg.ai {
    align-self: flex-start;
    background: var(--bg-warm);
    border-bottom-left-radius: 4px;
    max-width: 96%;
}
.msg.ai h1, .msg.ai h2, .msg.ai h3 { font-size: 15px; margin: 8px 0 4px; }
.msg.ai p { margin-bottom: 6px; }
.msg.ai ul, .msg.ai ol { padding-left: 18px; margin: 4px 0; }
.msg.ai li { margin-bottom: 2px; }
.msg.ai strong { color: var(--text); }
.msg.ai code { font-size: 12px; }
.msg.loading {
    align-self: flex-start;
    background: var(--bg-warm);
    color: var(--text-faint);
    font-style: italic;
}
.msg.loading::after {
    content: '...';
    animation: dots 1.4s infinite;
}
@keyframes dots {
    0%, 20% { content: '.'; }
    40% { content: '..'; }
    60%, 100% { content: '...'; }
}
.sources {
    margin-top: 10px; padding-top: 8px;
    border-top: 1px solid var(--border);
    display: flex; flex-wrap: wrap; gap: 6px;
}
.source-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    background: var(--blue-bg);
    color: var(--blue-text);
    font-size: 12px; font-weight: 500;
    cursor: pointer;
    text-decoration: none;
    transition: all 0.15s;
    user-select: none;
}
.source-badge:hover {
    background: var(--blue);
    color: #fff;
    text-decoration: none;
}
.source-badge .src-link {
    margin-left: 4px;
    font-size: 10px;
    opacity: 0.7;
}
.source-expand {
    display: none;
    margin: 6px 0 2px;
    padding: 10px 14px;
    background: #fff;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.55;
    color: var(--text-muted);
    max-height: 240px;
    overflow-y: auto;
    white-space: pre-wrap;
}
.source-expand.open { display: block; }
.source-expand .src-title {
    font-weight: 600;
    color: var(--text);
    margin-bottom: 6px;
    font-size: 13px;
}
.source-expand .src-open-link {
    display: inline-block;
    margin-top: 6px;
    font-size: 12px;
    color: var(--blue);
}
.chat-input-wrap {
    padding: 10px 14px;
    border-top: 1px solid var(--border);
    display: flex; gap: 8px;
    background: var(--bg);
}
.chat-input {
    flex: 1;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 20px;
    font-size: 14px;
    font-family: var(--font);
    outline: none;
    transition: border-color 0.15s;
}
.chat-input:focus { border-color: var(--blue); }
.chat-send {
    padding: 8px 16px;
    border-radius: 20px;
    background: var(--blue);
    color: #fff;
    border: none;
    font-size: 14px; font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
}
.chat-send:hover { background: var(--blue-hover); }
.chat-send:disabled { opacity: 0.5; cursor: not-allowed; }

""".strip()

JS = """
// Highlight current nav
(function(){
    const path = window.location.pathname;
    document.querySelectorAll('.nav-link,.sub-link').forEach(el => {
        const href = el.getAttribute('href');
        if (href === path || (href !== '/' && path.startsWith(href))) {
            el.classList.add('active');
        }
    });
})();

// ── Chat Logic ──
(function(){
    const btn = document.getElementById('chat-btn');
    const panel = document.getElementById('chat-panel');
    const close = document.getElementById('chat-close');
    const input = document.getElementById('chat-input');
    const send = document.getElementById('chat-send');
    const msgs = document.getElementById('chat-messages');

    btn.addEventListener('click', () => {
        panel.classList.add('open');
        input.focus();
    });
    close.addEventListener('click', () => panel.classList.remove('open'));

    function addMsg(role, html) {
        const div = document.createElement('div');
        div.className = 'msg ' + role;
        div.innerHTML = html;
        msgs.appendChild(div);
        msgs.scrollTop = msgs.scrollHeight;
        return div;
    }

    function addSources(sources) {
        if (!sources || !sources.length) return '';
        let html = '<div class="sources">';
        sources.forEach(s => {
            const id = s.id;
            const title = s.title || '';
            const label = s.label || '';
            const link = s.link || '#';
            const full = (s.full || '').substring(0, 3000);
            // Escape for HTML
            const escFull = full.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            html += '<span class="source-badge" onclick="var n=this.nextElementSibling;n.classList.toggle(\"open\");">[' + id + '] ' + title.substring(0, 28) + '<a class=\"src-link\" href=\"' + link + '\" target=\"_blank\" onclick=\"event.stopPropagation()\" title=\"' + label + '\">↗</a></span>';
            html += '<div class=\"source-expand\"><div class=\"src-title\">' + title + ' <span style=\"color:var(--text-faint);font-weight:400\">(' + label + ')</span></div>' + escFull + '<br><a class=\"src-open-link\" href=\"' + link + '\" target=\"_blank\">打开完整文件 ↗</a></div>';
        });
        html += '</div>';
        return html;
    }

    async function ask() {
        const q = input.value.trim();
        if (!q) return;
        input.value = '';
        send.disabled = true;

        addMsg('user', q);
        const loading = addMsg('loading', '思考中');

        try {
            const resp = await fetch('/api/ask', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({question: q})
            });
            const data = await resp.json();
            loading.remove();

            let html = data.answer
                .replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')
                .replace(/\\n/g, '<br>')
                .replace(/### (.+)/g, '<h3>$1</h3>')
                .replace(/## (.+)/g, '<h2>$1</h2>')
                .replace(/- (.+)/g, '\\u2022 $1')
                .replace(/\\[([0-9]+)\\]/g, '<sup>[$1]</sup>');
            html += addSources(data.sources);
            addMsg('ai', html);
        } catch(e) {
            loading.remove();
            addMsg('ai', '请求失败: ' + e.message);
        }
        send.disabled = false;
    }

    send.addEventListener('click', ask);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            ask();
        }
    });
})();
""".strip()

def build_page(title: str, body: str, path: str = "/") -> str:
    """Build full HTML page."""
    sidebar = build_sidebar(path)
    crumbs = get_crumb(path)
    breadcrumb_html = ""
    if len(crumbs) > 1:
        breadcrumb_html = '<div class="breadcrumb">' + ''.join(
            f'<a href="{c["href"]}">{c["label"]}</a><span class="sep">/</span>'
            if i < len(crumbs) - 1 else f'<span>{c["label"]}</span>'
            for i, c in enumerate(crumbs)
        ) + '</div>'

    is_full = path.strip('/') in ['']
    main_class = 'main full' if is_full else 'main'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Plaud 知识库</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
{sidebar}
<div class="{main_class}">
{breadcrumb_html}
{body}
</div>

<!-- Chat UI -->
<button id="chat-btn" class="chat-btn" title="Ask the knowledge base">💬</button>
<div id="chat-panel" class="chat-panel">
    <div class="chat-header">
        <h3>💬 知识库问答</h3>
        <button id="chat-close" class="chat-close">×</button>
    </div>
    <div id="chat-messages" class="chat-messages">
        <div class="chat-empty"><span class="icon">📚</span>基于会议纪要和业务档案回答问题<br>试试问：广东华为商业去年多少？</div>
    </div>
    <div class="chat-input-wrap">
        <input id="chat-input" class="chat-input" type="text" placeholder="输入问题..." />
        <button id="chat-send" class="chat-send">发送</button>
    </div>
</div>

<script>{JS}</script>
</body>
</html>"""

def build_sidebar(current_path: str) -> str:
    """Build sidebar navigation."""
    html = '<nav class="sidebar">'
    html += '<div class="sidebar-logo">Plaud 知识库</div>'
    html += '<div class="sidebar-sub">会议知识库</div>'

    for nav in NAV:
        is_active = current_path == nav["href"] or (nav["href"] != "/" and current_path.startswith(nav["href"]))
        html += '<div class="nav-section">'
        html += f'<a href="{nav["href"]}" class="nav-link{" active" if is_active else ""}">'
        html += f'<span class="nav-emoji">{nav["emoji"]}</span>{nav["label"]}</a>'

        # Show subpages if this section is active
        if is_active and nav["href"] != "/":
            subpages = get_subpages(nav["href"].strip('/'))
            if subpages:
                html += '<div class="subpages">'
                for sp in subpages:
                    sp_active = current_path == sp["href"] or current_path.startswith(sp["href"] + "/")
                    html += f'<a href="{sp["href"]}" class="sub-link{" active" if sp_active else ""}">{sp["label"]}</a>'
                html += '</div>'
        html += '</div>'

    html += '</nav>'
    return html

# ══════════════════════════════════════════════════════════
# Page generators
# ══════════════════════════════════════════════════════════

def index_page() -> str:
    """Home page with overview cards."""
    index_path = os.path.join(WIKI_DIR, "INDEX.md")
    body = ""
    if os.path.exists(index_path):
        body = render_markdown(open(index_path).read())

    # Add stat cards
    stats = []
    for d in ["people", "companies", "decisions", "actions"]:
        dpath = os.path.join(WIKI_DIR, d)
        if os.path.isdir(dpath):
            count = len([f for f in os.listdir(dpath) if f.endswith('.md')])
            stats.append({"label": d, "count": count})

    stat_title_map = {"people": "人物", "companies": "公司", "decisions": "决策", "actions": "行动项"}

    if stats:
        body += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:32px">'
        for s in stats:
            body += f'''<div class="card" style="text-align:center;padding:20px">
                <div style="font-size:36px;font-weight:700;color:var(--blue);margin-bottom:4px">{s["count"]}</div>
                <div style="font-size:14px;color:var(--text-muted)">{stat_title_map.get(s["label"], s["label"])}</div>
            </div>'''
        body += '</div>'

    return build_page("Plaud 知识库", body, "/")

def directory_page(path: str) -> str:
    """List directory contents as cards."""
    dir_path = os.path.join(WIKI_DIR, path.strip('/'))
    if not os.path.isdir(dir_path):
        return build_page("404", "<h1>404</h1><p>目录不存在</p>", path)

    files = sorted([f for f in os.listdir(dir_path) if f.endswith('.md')])
    section_name = path.strip('/').replace('/', ' → ')

    # Show INDEX.md content first if it exists
    body = ""
    index_file = os.path.join(dir_path, "INDEX.md")
    if os.path.exists(index_file):
        body = render_markdown(open(index_file).read())

    if not body:
        body = f'<h1>{section_name}</h1>'

    # List subpages
    subs = [f for f in files if f != "INDEX.md"]
    if subs:
        body += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:20px">'
        for f in subs:
            name = f.replace('.md', '').replace('-', ' ')
            href = f"/{path.strip('/')}/{f.replace('.md', '')}"
            body += f'''<a href="{href}" style="text-decoration:none;color:inherit">
                <div class="card" style="cursor:pointer;transition:all 0.15s" onmouseover="this.style.borderColor='var(--blue)'" onmouseout="this.style.borderColor='var(--border)'">
                    <div style="font-size:15px;font-weight:600;margin-bottom:4px">{name}</div>
                    <div style="font-size:13px;color:var(--text-faint)">查看详情 →</div>
                </div>
            </a>'''
        body += '</div>'

    return build_page(section_name, body, f"/{path.strip('/')}/")

def file_page(path: str) -> str:
    """Render a single markdown file."""
    file_path = os.path.join(WIKI_DIR, path.strip('/') + '.md')
    if not os.path.exists(file_path):
        return build_page("404", "<h1>404</h1><p>页面不存在</p>", path)

    content = open(file_path).read()
    body = render_markdown(content)
    title = path.strip('/').split('/')[-1].replace('-', ' ')
    return build_page(title, body, f"/{path.strip('/')}")

def serve_raw(path: str) -> str:
    """Serve raw markdown content for 会议纪要 / 概念 files."""
    # Path format: /raw/{label}/{filename}
    parts = path.strip('/').split('/', 2)
    if len(parts) < 3:
        return build_page("404", "<h1>404</h1><p>无效路径</p>", path)

    label, filename = parts[1], parts[2]
    # Map label to directory
    LABEL_MAP = {"会议纪要": NOTES_DIR, "概念": CONCEPTS_DIR, "wiki": WIKI_DIR}
    sdir = LABEL_MAP.get(label)
    if not sdir:
        return build_page("404", f"<h1>404</h1><p>未知类型: {label}</p>", path)

    fpath = os.path.join(sdir, filename)
    if not os.path.exists(fpath):
        return build_page("404", "<h1>404</h1><p>文件不存在</p>", path)

    content = open(fpath).read()
    body = render_markdown(content)
    title = filename.replace('.md', '').replace('-', ' ')
    return build_page(f"{title} — {label}", body, path)

# ══════════════════════════════════════════════════════════
# Speaker Editor
# ══════════════════════════════════════════════════════════

def _load_naming() -> dict:
    """Load anchor naming DB."""
    if os.path.exists(ANCHOR_DB):
        with open(ANCHOR_DB) as f:
            return json.load(f).get("naming", {})
    return {}

def _save_naming(naming: dict):
    """Save anchor naming DB."""
    db = {
        "version": "1.0", "phase": "manual_edit",
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "naming": naming,
        "total_recordings_named": len(naming),
    }
    os.makedirs(os.path.dirname(ANCHOR_DB), exist_ok=True)
    with open(ANCHOR_DB, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ── Voiceprint Propagation ──

def _propagate_name_via_voiceprint(source_fid: str, old_spk: str, new_name: str) -> list[dict]:
    """Extract voiceprint from source speaker and auto-assign matching speakers.
    Phase 1: within same recording (threshold 0.45, same mic)
    Phase 2: across all recordings (threshold 0.58)
    """
    import tempfile, shutil
    from collections import defaultdict

    try:
        from funasr import AutoModel
        import numpy as np
    except ImportError:
        return [{"error": "FunASR not available"}]

    source_ogg = os.path.join(REC_DIR, f"{source_fid}.ogg")
    if not os.path.exists(source_ogg):
        return [{"error": "源音频不存在"}]

    cam = AutoModel(model="cam++", model_revision="v2.0.2", disable_update=True)
    naming = _load_naming()
    results = []

    # 1. Extract voiceprint from the source speaker
    source_named = os.path.join(NAMED_TRANSCRIPTS, f"{source_fid}.json")
    with open(source_named) as f:
        src_data = json.load(f)
    src_segs = [s for s in src_data.get("segments", []) if s.get("speaker") == new_name]
    if not src_segs:
        return [{"error": f"未找到 {new_name} 的语音片段"}]

    print(f"[propagate] 提取 {new_name} 声纹 ({len(src_segs)} segments from {source_fid[:12]})")
    src_emb = _extract_embeddings(source_ogg, src_segs, cam)
    if src_emb is None:
        return [{"error": "声纹提取失败"}]

    # Phase 1: Match within SAME recording (lower threshold, same mic)
    print(f"[propagate] Phase 1: intra-recording matching for {source_fid[:12]}...")
    fid_naming = naming.get(source_fid, {})
    with open(source_named) as f:
        src_full = json.load(f)
    intra_unnamed = defaultdict(list)
    for seg in src_full.get("segments", []):
        spk = seg.get("speaker", "")
        if spk.startswith("spk_") and spk != old_spk and spk not in fid_naming:
            intra_unnamed[spk].append(seg)

    intra_count = 0
    if intra_unnamed:
        for spk_id, spk_segs in intra_unnamed.items():
            total_dur = sum(s["end_ms"] - s["start_ms"] for s in spk_segs) / 1000
            if total_dur < 3.0:
                continue
            tgt_emb = _extract_embeddings(source_ogg, spk_segs, cam)
            if tgt_emb is None:
                continue
            sim = float(np.dot(src_emb, tgt_emb) / (np.linalg.norm(src_emb) * np.linalg.norm(tgt_emb)))
            if sim >= 0.42:
                if source_fid not in naming:
                    naming[source_fid] = {}
                naming[source_fid][spk_id] = {
                    "name": new_name, "source": "voiceprint_intra",
                    "confidence": round(sim, 3),
                }
                for seg in src_full.get("segments", []):
                    if seg.get("speaker") == spk_id:
                        seg["speaker"] = new_name
                        seg["speaker_source"] = "voiceprint_intra"
                        seg["speaker_confidence"] = round(sim, 3)
                src_full["speaker_map"][spk_id] = new_name
                intra_count += 1
                results.append({"fid": source_fid[:12], "spk": spk_id, "sim": round(sim, 3),
                               "dur_s": round(total_dur), "phase": "intra"})
                print(f"[propagate]  🎙 intra: {spk_id} → {new_name} (sim={sim:.3f})")
        if intra_count > 0:
            with open(source_named, "w") as f:
                json.dump(src_full, f, ensure_ascii=False, indent=2)

    # Phase 2: Cross-recording matching
    print(f"[propagate] Phase 2: cross-recording matching...")
    results = []
    THRESHOLD = 0.58
    naming = _load_naming()

    for fn in sorted(os.listdir(NAMED_TRANSCRIPTS)):
        if not fn.endswith('.json'):
            continue
        target_fid = fn.replace('.json', '')
        if target_fid == source_fid:
            continue

        target_ogg = os.path.join(REC_DIR, f"{target_fid}.ogg")
        target_named = os.path.join(NAMED_TRANSCRIPTS, f"{target_fid}.json")
        if not os.path.exists(target_ogg) or not os.path.exists(target_named):
            continue

        with open(target_named) as f:
            tgt_data = json.load(f)

        fid_naming = naming.get(target_fid, {})
        unnamed = defaultdict(list)
        for seg in tgt_data.get("segments", []):
            spk = seg.get("speaker", "")
            if spk.startswith("spk_") and spk not in fid_naming:
                unnamed[spk].append(seg)
        if not unnamed:
            continue

        for spk_id, spk_segs in unnamed.items():
            total_dur = sum(s["end_ms"] - s["start_ms"] for s in spk_segs) / 1000
            if total_dur < 5.0:
                continue
            tgt_emb = _extract_embeddings(target_ogg, spk_segs, cam)
            if tgt_emb is None:
                continue
            sim = float(np.dot(src_emb, tgt_emb) / (np.linalg.norm(src_emb) * np.linalg.norm(tgt_emb)))
            if sim >= THRESHOLD:
                if target_fid not in naming:
                    naming[target_fid] = {}
                naming[target_fid][spk_id] = {
                    "name": new_name, "source": "voiceprint_propagated",
                    "confidence": round(sim, 3),
                }
                # Update named transcript
                with open(target_named) as f:
                    nd = json.load(f)
                for seg in nd.get("segments", []):
                    if seg.get("speaker") == spk_id:
                        seg["speaker"] = new_name
                        seg["speaker_source"] = "voiceprint_propagated"
                        seg["speaker_confidence"] = round(sim, 3)
                nd["speaker_map"][spk_id] = new_name
                with open(target_named, "w") as f:
                    json.dump(nd, f, ensure_ascii=False, indent=2)
                results.append({"fid": target_fid[:12], "spk": spk_id, "sim": round(sim, 3), "dur_s": round(total_dur)})
                print(f"[propagate] ✅ {target_fid[:12]}... {spk_id} → {new_name} (sim={sim:.3f})")

    _save_naming(naming)
    return results


def _extract_embeddings(audio_path: str, segments: list, cam_model, max_samples: int = 15):
    """Extract CAM++ speaker embedding from audio segments."""
    import tempfile, subprocess as sp
    sorted_segs = sorted(segments, key=lambda s: s["end_ms"] - s["start_ms"], reverse=True)
    samples = sorted_segs[:max_samples]
    samples.sort(key=lambda s: s["start_ms"])
    valid = [s for s in samples if (s["end_ms"] - s["start_ms"]) >= 200]
    if len(valid) < 3:
        return None
    tmp_dir = tempfile.mkdtemp(prefix="vp_prop_")
    try:
        concat_list = os.path.join(tmp_dir, "concat.txt")
        with open(concat_list, "w") as f:
            for i, seg in enumerate(valid):
                seg_wav = os.path.join(tmp_dir, f"s{i}.wav")
                start_s = max(0, seg["start_ms"] / 1000)
                dur = (seg["end_ms"] - seg["start_ms"]) / 1000
                sp.run(["ffmpeg", "-y", "-ss", str(start_s), "-t", str(dur),
                        "-i", audio_path, "-ac", "1", "-ar", "16000", seg_wav], capture_output=True)
                if os.path.exists(seg_wav) and os.path.getsize(seg_wav) > 1000:
                    f.write(f"file '{seg_wav}'\n")
        concat_wav = os.path.join(tmp_dir, "concat.wav")
        sp.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", concat_list, "-ac", "1", "-ar", "16000", concat_wav], capture_output=True)
        if not os.path.exists(concat_wav) or os.path.getsize(concat_wav) < 1000:
            return None
        res = cam_model.generate(input=concat_wav)
        if res and len(res) > 0:
            emb = res[0].get("spk_embedding")
            if emb is not None:
                return emb.detach().cpu().numpy().flatten()
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return None

def speakers_list_page() -> str:
    """List all recordings with speaker coverage."""
    naming = _load_naming()
    items = []
    for fid in sorted(os.listdir(NAMED_TRANSCRIPTS)):
        if not fid.endswith('.json'):
            continue
        fid_key = fid.replace('.json', '')
        named_path = os.path.join(NAMED_TRANSCRIPTS, fid)
        try:
            with open(named_path) as f:
                data = json.load(f)
        except Exception:
            continue

        segs = data.get("segments", [])
        total = len(segs)
        named_count = sum(1 for s in segs if not s.get("speaker", "").startswith("spk_"))
        pct = named_count / total * 100 if total else 0

        # Get display name
        fname = fid.replace('.json', '')
        # Try to get filename from pipeline state
        trans_path = os.path.join(ORIG_TRANSCRIPTS, f"{fid_key}.md")
        if os.path.exists(trans_path):
            with open(trans_path) as f:
                first = f.readline().strip('# \n')
                if '转录' in first:
                    fname = first.replace('# 转录 — ', '').strip()
        dur = data.get("duration", "?")
        items.append({
            "fid": fid_key,
            "name": fname[:60],
            "total": total,
            "named": named_count,
            "pct": round(pct),
            "duration": dur if isinstance(dur, str) else f"{dur:.0f}s" if dur else "?",
        })

    items.sort(key=lambda x: x["pct"])

    body = '<h1>✏️ 编辑说话人</h1><p style="color:var(--text-muted);margin-bottom:20px">点击录音进入编辑——查看转录原文，修改说话人标注</p>'
    body += '<div style="display:grid;gap:8px">'
    COLORS = ["#e8f5e9", "#fff3e0", "#ffebee"]
    for it in items:
        color = COLORS[0] if it["pct"] >= 90 else COLORS[1] if it["pct"] >= 50 else COLORS[2]
        body += f'''<a href="/speakers/{it['fid']}" style="text-decoration:none;color:inherit">
            <div class="card" style="display:flex;align-items:center;gap:16px;padding:14px 20px;cursor:pointer;border-left:4px solid {color}">
                <div style="flex:1;min-width:0">
                    <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{it['name']}</div>
                    <div style="font-size:12px;color:var(--text-faint)">{it['duration']}</div>
                </div>
                <div style="text-align:right">
                    <div style="font-size:20px;font-weight:700;color:{color.replace('e8f5e9','#1aae39').replace('fff3e0','#dd5b00').replace('ffebee','#d32f2f')}">{it['pct']}%</div>
                    <div style="font-size:12px;color:var(--text-faint)">{it['named']}/{it['total']} 具名</div>
                </div>
            </div>
        </a>'''
    body += '</div>'
    return build_page("编辑说话人", body, "/speakers/")

def speaker_edit_page(fid: str) -> str:
    """Show transcript with editable speaker labels."""
    fid_key = fid
    named_path = os.path.join(NAMED_TRANSCRIPTS, f"{fid_key}.json")
    if not os.path.exists(named_path):
        return build_page("404", "<h1>404</h1><p>录音不存在或未处理</p>", f"/speakers/{fid}")

    with open(named_path) as f:
        data = json.load(f)

    naming = _load_naming()
    fid_naming = naming.get(fid_key, {})

    segs = data.get("segments", [])
    total_ms = segs[-1]["end_ms"] if segs else 0

    # Build speaker list for dropdown
    all_names = set()
    for fnaming in naming.values():
        for info in fnaming.values():
            all_names.add(info["name"])
    all_names = sorted(all_names)

    # Group segments by speaker
    groups = []
    current_speaker = None
    current_texts = []
    current_start = 0
    current_end = 0
    for seg in segs:
        spk = seg.get("speaker", "?")
        txt = seg.get("text", "")
        if spk != current_speaker:
            if current_speaker is not None:
                groups.append({"speaker": current_speaker, "text": "".join(current_texts),
                               "start": current_start, "end": current_end})
            current_speaker = spk
            current_texts = [txt]
            current_start = seg.get("start_ms", 0)
            current_end = seg.get("end_ms", 0)
        else:
            current_texts.append(txt)
            current_end = seg.get("end_ms", 0)
    if current_speaker is not None:
        groups.append({"speaker": current_speaker, "text": "".join(current_texts),
                       "start": current_start, "end": current_end})

    # Build HTML
    spk_colors = ["#e3f2fd","#fce4ec","#e8f5e9","#fff3e0","#f3e5f5","#e0f7fa",
                  "#fff8e1","#ede7f6","#e8eaf6","#fbe9e7"]
    color_idx = {}
    def get_color(spk):
        if spk not in color_idx:
            color_idx[spk] = len(color_idx) % len(spk_colors)
        return spk_colors[color_idx[spk]]

    name_options = "".join(f'<option value="{n}">{n}</option>' for n in all_names)

    body = '<h1>✏️ 编辑说话人</h1>'
    body += f'<p style="color:var(--text-muted);margin-bottom:4px">{fid_key[:16]}...</p>'
    # Audio player
    audio_url = f"/audio/{fid_key}.ogg"
    body += f'''<div style="position:sticky;top:0;z-index:100;background:var(--bg-warm);padding:10px 0;margin-bottom:16px;border-bottom:1px solid var(--border)">
        <audio id="audio-player" src="{audio_url}" controls preload="metadata" style="width:100%;height:32px"
               ontimeupdate="highlightPlaying()"></audio>
    </div>'''
    body += '<p style="font-size:13px;color:var(--text-faint);margin-bottom:24px">点击说话人标签修改 · 点击段落文字听录音</p>'

    body += '<div id="transcript">'
    for i, g in enumerate(groups):
        spk = g["speaker"]
        tspk = spk
        is_named = not spk.startswith("spk_")
        info = fid_naming.get(spk, {})
        source = info.get("source", "manual") if is_named else "unknown"
        confidence = info.get("confidence", "")

        ts = f"{int(g['start']/60000):02d}:{int((g['start']%60000)/1000):02d}"
        color = get_color(spk)

        body += f'<div class="seg-block" data-start="{g["start"]}" data-end="{g["end"]}" style="border-left:3px solid {color};margin-bottom:12px;padding:8px 12px;border-radius:0 4px 4px 0;background:{color}20;cursor:pointer" onclick="playSegment({g["start"]}, {g["end"]}, this)" title="点击播放此段录音">'
        body += f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
        body += f'<span class="seg-ts" style="font-size:12px;color:var(--text-faint);font-family:mono">{ts}</span>'

        # Speaker label - clickable
        badge_color = color if is_named else "#ccc"
        body += f'<span class="spk-label" data-seg="{i}" data-current="{spk}" '
        body += f'style="display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:12px;background:{badge_color};font-size:13px;font-weight:600;cursor:pointer;transition:all 0.15s" '
        body += f'onclick="event.stopPropagation();editSpeaker({i}, this)" title="点击修改">'
        body += f'{spk} <span style="font-size:10px;opacity:0.5">✎</span></span>'

        if source:
            body += f'<span style="font-size:10px;color:var(--text-faint)">{source}</span>'
        body += '</div>'
        body += f'<div class="seg-text" style="font-size:14px;line-height:1.6;color:var(--text)">{g["text"][:500]}</div>'
        body += '</div>'

    body += '</div>'

    # Inline edit modal (hidden)
    body += f'''<div id="edit-modal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.3);z-index:2000;align-items:center;justify-content:center">
    <div class="card" style="width:360px">
        <h3 style="margin-top:0">修改说话人</h3>
        <p style="font-size:13px;color:var(--text-muted);margin-bottom:12px">录音: {fid_key[:16]}... · 段落: <span id="modal-seg-id"></span></p>
        <input id="modal-input" type="text" list="speaker-list" style="width:100%;padding:10px;border:1px solid var(--border);border-radius:8px;font-size:14px;margin-bottom:8px" placeholder="输入名字或选择已有...">
        <datalist id="speaker-list">{name_options}</datalist>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text-muted);margin-bottom:12px;cursor:pointer">
            <input type="checkbox" id="propagate-check" style="width:16px;height:16px">
            根据声纹自动匹配其他录音中的相同说话人
        </label>
        <div style="display:flex;gap:8px;justify-content:flex-end">
            <button onclick="closeModal()" style="padding:8px 16px;border:1px solid var(--border);border-radius:6px;background:none;cursor:pointer">取消</button>
            <button onclick="saveSpeaker()" style="padding:8px 16px;border:none;border-radius:6px;background:var(--blue);color:#fff;cursor:pointer;font-weight:600">保存</button>
        </div>
    </div>
</div>'''

    # JS for editing
    edit_js = f"""
var currentSeg = -1;
var currentLabel = null;
var fid = '{fid_key}';
var speakers = {json.dumps(list(all_names), ensure_ascii=False)};
var activeBlock = null;

function playSegment(startMs, endMs, block) {{
    var audio = document.getElementById('audio-player');
    audio.currentTime = startMs / 1000;
    audio.play();
    // Highlight active block
    if (activeBlock) activeBlock.style.boxShadow = '';
    block.style.boxShadow = '0 0 0 2px var(--blue)';
    activeBlock = block;
    // Auto-stop after segment
    clearTimeout(window._segTimeout);
    var dur = (endMs - startMs) / 1000 + 2;
    window._segTimeout = setTimeout(function() {{
        if (activeBlock) activeBlock.style.boxShadow = '';
        activeBlock = null;
    }}, dur * 1000);
}}

function highlightPlaying() {{
    // placeholder
}}

function editSpeaker(idx, el) {{
    currentSeg = idx;
    currentLabel = el;
    document.getElementById('modal-seg-id').textContent = idx;
    document.getElementById('modal-input').value = el.getAttribute('data-current');
    document.getElementById('edit-modal').style.display = 'flex';
    document.getElementById('modal-input').focus();
}}

function closeModal() {{
    document.getElementById('edit-modal').style.display = 'none';
    currentSeg = -1;
}}

async function saveSpeaker() {{
    var newName = document.getElementById('modal-input').value.trim();
    if (!newName || currentSeg < 0) return closeModal();
    var oldSpk = currentLabel.getAttribute('data-current');

    // Check if propagate checkbox is ticked
    var doPropagate = document.getElementById('propagate-check') ? document.getElementById('propagate-check').checked : false;

    var resp = await fetch('/api/speakers', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{fid: fid, old_spk: oldSpk, new_name: newName, propagate: doPropagate}})
    }});
    var result = await resp.json();
    if (result.ok) {{
        currentLabel.setAttribute('data-current', newName);
        currentLabel.innerHTML = newName + ' <span style="font-size:10px;opacity:0.5">✎</span>';
        currentLabel.style.background = '#c8e6c9';

        // Show propagation results
        if (result.propagated && result.propagated.length > 0) {{
            var msg = '✅ 声纹传播成功！\\n\\n';
            result.propagated.forEach(function(p) {{
                msg += '📋 ' + p.fid + '... → ' + p.spk + ' (相似度: ' + (p.sim*100).toFixed(0) + '%)\\n';
            }});
            alert(msg);
        }} else if (doPropagate) {{
            alert('未在其他录音中找到匹配声纹');
        }}
        setTimeout(function() {{ location.reload(); }}, 500);
    }} else {{
        alert('保存失败: ' + (result.error || '未知错误'));
    }}
    closeModal();
}}

document.getElementById('edit-modal').addEventListener('click', function(e) {{
    if (e.target === this) closeModal();
}});
"""
    body += f'<script>{edit_js}</script>'
    return build_page(f"编辑: {fid_key[:16]}...", body, f"/speakers/{fid}")

# ══════════════════════════════════════════════════════════
# HTTP Server
# ══════════════════════════════════════════════════════════

class WikiHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip('/')

        # Route
        if path == "":
            html = index_page()
        elif path.startswith("/raw/"):
            # Serve raw markdown source files
            html = serve_raw(path)
        elif path.startswith("/speakers"):
            # Speaker editor
            fid = path.replace("/speakers", "").strip('/')
            html = speaker_edit_page(fid) if fid else speakers_list_page()
        elif path.startswith("/audio/"):
            # Serve audio file
            self._serve_audio(path)
            return
        else:
            # Check if it's a directory
            dir_path = os.path.join(WIKI_DIR, path.strip('/'))
            if os.path.isdir(dir_path):
                html = directory_page(path)
            else:
                html = file_page(path)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip('/')

        if path == "/api/ask":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                question = data.get("question", "").strip()
            except Exception:
                self._json_response({"error": "无效的请求"}, 400)
                return

            if not question:
                self._json_response({"error": "问题不能为空"}, 400)
                return

            # Search + RAG
            sources = search_kb(question)
            result = ask_claude(question, sources)
            self._json_response(result)
        elif path == "/api/speakers":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                fid = data.get("fid", "")
                old_spk = data.get("old_spk", "")
                new_name = data.get("new_name", "").strip()
            except Exception:
                self._json_response({"ok": False, "error": "无效请求"}, 400)
                return

            if not fid or not old_spk or not new_name:
                self._json_response({"ok": False, "error": "缺少参数"}, 400)
                return

            # Update naming DB
            naming = _load_naming()
            if fid not in naming:
                naming[fid] = {}
            naming[fid][old_spk] = {
                "name": new_name,
                "source": "manual",
                "confidence": 1.0,
            }
            _save_naming(naming)

            # Also regenerate the named transcript
            named_path = os.path.join(NAMED_TRANSCRIPTS, f"{fid}.json")
            if os.path.exists(named_path):
                with open(named_path) as f:
                    ndata = json.load(f)
                for seg in ndata.get("segments", []):
                    if seg.get("speaker") == old_spk:
                        seg["speaker"] = new_name
                        seg["speaker_source"] = "manual"
                        seg["speaker_confidence"] = 1.0
                sm = ndata.get("speaker_map", {})
                sm[old_spk] = new_name
                ndata["speaker_map"] = sm
                with open(named_path, "w") as f:
                    json.dump(ndata, f, ensure_ascii=False, indent=2)

            # Auto-propagate via voiceprint if requested
            propagate = data.get("propagate", False)
            propagated = []
            if propagate:
                try:
                    propagated = _propagate_name_via_voiceprint(fid, old_spk, new_name)
                except Exception as e:
                    propagated = [{"error": str(e)}]

            self._json_response({"ok": True, "new_name": new_name, "propagated": propagated})
        else:
            self._json_response({"error": "Not found"}, 404)

    def _json_response(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _serve_audio(self, path: str):
        """Serve OGG audio files from recordings directory."""
        fname = path.replace("/audio/", "").lstrip('/')
        fpath = os.path.join(REC_DIR, fname)
        if not os.path.exists(fpath):
            self.send_response(404)
            self.end_headers()
            return
        fsize = os.path.getsize(fpath)
        # Support range requests for seeking
        range_header = self.headers.get("Range", "")
        if range_header:
            m = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else fsize - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "audio/ogg")
                self.end_headers()
                with open(fpath, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
                return
        self.send_response(200)
        self.send_header("Content-Type", "audio/ogg")
        self.send_header("Content-Length", str(fsize))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(fpath, "rb") as f:
            self.wfile.write(f.read())

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {args[0]}", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Plaud Wiki Server")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port (default: {PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    args = parser.parse_args()

    if not os.path.isdir(WIKI_DIR):
        print(f"❌ Wiki 目录不存在: {WIKI_DIR}")
        print("   请先运行 plaud_kb_generate.py 生成知识库")
        sys.exit(1)

    server = HTTPServer((args.host, args.port), WikiHandler)
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  Plaud Wiki Server                       ║")
    print(f"║  http://localhost:{args.port}                 ║")
    print(f"║  Wiki: {WIKI_DIR}")
    print(f"╚══════════════════════════════════════════╝")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Bye")
        server.shutdown()

if __name__ == "__main__":
    main()
