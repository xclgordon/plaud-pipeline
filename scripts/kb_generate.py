#!/usr/bin/env python3
"""
Plaud Knowledge Base Generator — Layer 1: Structured Wiki
═══════════════════════════════════════════════════════════
从 ai_notes_merged/ 解析结构化纪要，生成：
  people/      👤 人物档案
  companies/   🏢 客户/合作方
  projects/    📋 项目追踪
  decisions/   ✅ 决策日志
  actions/     🔴 行动项看板
  timeline/    📅 会议时间线
  INDEX.md     🏠 知识库入口

运行: ~/funasr-venv/bin/python3 plaud_kb_generate.py
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════

BASE = os.path.expanduser("~/plaud-knowledge-base")
NOTES_DIR = os.path.join(BASE, "ai_notes_merged")
KB_DIR = os.path.join(BASE, "wiki")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

for sub in ["people", "companies", "projects", "decisions", "actions", "timeline"]:
    os.makedirs(os.path.join(KB_DIR, sub), exist_ok=True)

def _load_org_chart() -> str:
    """Load org chart from config/org_chart.md, fall back to example."""
    config_dir = os.environ.get("PLAUD_CONFIG_DIR", os.path.join(Path.home(), "plaud-pipeline", "config"))
    for name in ["org_chart.md", "org_chart.example.md"]:
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    return ""

ORG_CHART = _load_org_chart()

# ══════════════════════════════════════════════════════════
# Step 1: Parse all notes into structured records
# ══════════════════════════════════════════════════════════

def parse_note(filepath: str) -> dict:
    """Parse a merged AI note into structured data."""
    with open(filepath, encoding="utf-8") as f:
        text = f.read()

    fid = os.path.basename(filepath).replace(".md", "")
    fname = fid[:24]

    # Extract date
    date_match = re.search(r'(?:日期|会议日期)[：:]\s*(.+?)$', text, re.MULTILINE)
    date = date_match.group(1).strip() if date_match else "unknown"

    # Extract participants
    participants_match = re.search(r'(?:参与人员|参会人|出席)[：:]\s*(.+?)$', text, re.MULTILINE)
    participants = []
    if participants_match:
        # Filter out non-person participants
        noise_words = {"双方", "各方", "团队", "大家", "内部团队", "全体", "相关人员",
                       "[下属]", "[下属负责人]", "各参与者", "内部业务代表",
                       "内部销售", "相关销售", "HR", "小龙", "内部高管"}
        raw = [p.strip().replace('**', '').replace('（', '(').replace('）', ')')
               for p in participants_match.group(1).split("、")]
        participants = [p for p in raw
                        if p and p not in noise_words
                        and not p.startswith('spk_')
                        and not p.startswith('[')
                        and len(p) >= 2]

    # Extract meeting summary
    summary = ""
    m = re.search(r'(?:会议摘要|1\.\s*会议摘要)\s*\n+(.+?)(?=\n(?:###|---|##))', text, re.DOTALL)
    if m:
        summary = m.group(1).strip()

    # Extract decisions (table format: | # | 决策 | 决策人 |)
    decisions = []
    # Find the decisions section
    dec_section = ""
    m = re.search(r'(?:关键决策|2\.\s*关键决策)\s*\n+(.*?)(?=\n(?:###|---|##)\s*(?:3\.|行动项))', text, re.DOTALL)
    if m:
        dec_section = m.group(1)
        for line in dec_section.split('\n'):
            # Match table rows: | 1 | content | person |
            row = re.match(r'\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|', line)
            if row:
                decisions.append({
                    "num": row.group(1),
                    "content": row.group(2).strip(),
                    "person": row.group(3).strip(),
                })
            # Also match list items: - [ ] **Person** task
            else:
                dec_item = re.match(r'[-*]\s+(?:\[\s?\]\s+)?\*\*(.+?)\*\*\s*(.+)', line)
                if dec_item:
                    decisions.append({
                        "num": str(len(decisions) + 1),
                        "content": dec_item.group(2).strip(),
                        "person": dec_item.group(1).strip(),
                    })

    # Extract action items: - [ ] **Person** task
    actions = []
    act_section = ""
    m = re.search(r'(?:行动项|3\.\s*行动项)\s*\n+(.*?)(?=\n(?:###|---|##)\s*(?:4\.|后续))', text, re.DOTALL)
    if m:
        act_section = m.group(1)
        for line in act_section.split('\n'):
            act_match = re.match(r'[-*]\s+(?:\[\s?\]\s+)?\*\*(.+?)\*\*\s*(.+)', line)
            if act_match:
                actions.append({
                    "person": act_match.group(1).strip(),
                    "task": act_match.group(2).strip(),
                    "status": "pending",
                })

    # Extract follow-up
    followup = ""
    m = re.search(r'(?:后续跟进|4\.\s*后续跟进)\s*\n+(.*?)(?=\n(?:###|---|##)\s*(?:5\.|议题))', text, re.DOTALL)
    if m:
        followup = m.group(1).strip()

    # Extract topics
    topics = []
    topic_section = ""
    m = re.search(r'(?:议题要点|5\.\s*议题要点).*?\n+(.*?)$', text, re.DOTALL)
    if m:
        topic_section = m.group(1)
        # Find topic headers: #### 议题一：xxx or ### 议题一：xxx
        for t_match in re.finditer(r'(?:#{3,4})\s*(?:议题[一二三四五六七八九十\d]+|[\d.]+\s*)?[：:]\s*(.+?)\n+(.*?)(?=\n(?:#{3,4})\s*(?:议题|[\d.])|$)', topic_section, re.DOTALL):
            topics.append({
                "title": t_match.group(1).strip(),
                "content": t_match.group(2).strip()[:500],
            })

    # Get full text (stripped of header)
    full_text = ""
    m = re.search(r'^---\s*\n(.*)', text, re.DOTALL)
    if m:
        full_text = m.group(1).strip()

    return {
        "fid": fid,
        "file": fname,
        "date": date,
        "participants": participants,
        "summary": summary,
        "decisions": decisions,
        "actions": actions,
        "followup": followup,
        "topics": topics,
        "full_text": full_text,
    }


# ══════════════════════════════════════════════════════════
# Step 2: Aggregate across documents
# ══════════════════════════════════════════════════════════

def aggregate(all_notes: list[dict]) -> dict:
    """Aggregate entities across all notes."""
    people = defaultdict(lambda: {
        "meetings": [],
        "decisions": [],
        "actions": [],
        "mentions": [],
    })
    companies = defaultdict(lambda: {
        "mentions": [],
        "projects": [],
        "people": set(),
    })
    projects = defaultdict(lambda: {
        "mentions": [],
        "people": set(),
        "decisions": [],
    })

    noise_people = {"双方", "各方", "团队", "大家", "内部团队", "全体", "相关人员",
                    "内部销售", "相关销售", "内部高管", "内部业务代表",
                    "各参与者", "全体与会者", "相关方", "行政-助理",
                    "深圳ICT负责人", "周老大", "未知", "-", "", "无", "HR", "小龙"}

    def clean_name(name: str) -> str | None:
        """Normalize and filter a person name. Returns None if noise."""
        if not name:
            return None
        # Remove spk_ suffix: "张三 - spk_3" → "张三"
        name = re.sub(r'\s*[-–—]\s*spk_\d+.*$', '', name)
        # Remove brackets: "[张三]" → "张三"
        name = re.sub(r'^[\[【](.+?)[\]】]$', r'\1', name)
        name = name.strip()
        if not name:
            return None
        # Filter noise
        if name in noise_people:
            return None
        if re.match(r'^(spk_\d+|\[.+\]|【.+】|.{0,2}待定.{0,2}|.{0,2}待确认.{0,2})$', name):
            return None
        if len(name) < 2:
            return None
        return name

    for note in all_notes:
        fid, fname, date = note["fid"], note["file"], note["date"]

        # Track people
        for p in note["participants"]:
            name = clean_name(p)
            if name:
                people[name]["meetings"].append(f"[{date}] {fid}")

        for d in note["decisions"]:
            name = clean_name(d["person"])
            if name:
                people[name]["decisions"].append({
                    "meeting": fid,
                    "date": date,
                    "content": d["content"],
                })

        for a in note["actions"]:
            name = clean_name(a["person"])
            if name:
                people[name]["actions"].append({
                    "meeting": fid,
                    "date": date,
                    "task": a["task"],
                })

        # Track companies/projects from topics and decisions
        all_text = note.get("full_text", "")[:2000]
        for topic in note.get("topics", []):
            all_text += " " + topic.get("title", "") + " " + topic.get("content", "")[:500]

        # Use simple keyword detection for companies/projects
        company_keywords = ["科技", "银行", "证券", "保险", "基金", "集团", "有限", "股份",
                           "阿里", "腾讯", "百度", "字节", "深信服", "奇安信"]
        for kw in company_keywords:
            if kw in all_text:
                companies[kw]["mentions"].append(fid)
                for p in note["participants"]:
                    name = clean_name(p)
                    if name:
                        companies[kw]["people"].add(name)

        # Extract project names
        project_patterns = [
            r'(?:项目|订单|方案)[：:\s]*[""\u201c]?([^""\u201d，,\n]{2,20})[""\u201d]?',
            r'(\w+)(?:项目|订单|交付)',
        ]
        for pat in project_patterns:
            for m in re.finditer(pat, all_text):
                proj = m.group(1).strip()
                if len(proj) >= 2 and proj not in company_keywords:
                    projects[proj]["mentions"].append(fid)
                    for p in note["participants"]:
                        name = clean_name(p)
                        if name:
                            projects[proj]["people"].add(name)

    # Convert sets to lists
    for c in companies.values():
        c["people"] = list(c["people"])
    for p in projects.values():
        p["people"] = list(p["people"])

    return {
        "people": dict(people),
        "companies": dict(companies),
        "projects": dict(projects),
    }


# ══════════════════════════════════════════════════════════
# Step 3: Call Claude for cross-document synthesis
# ══════════════════════════════════════════════════════════

def claude_synthesize(prompt: str, timeout: int = 120) -> str:
    """Call Claude for synthesis. Returns response text."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--input-format", "text",
             "--max-turns", "3", "--model", "claude-sonnet-4-6"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"  ⚠ Claude error: {e}")
        return ""


def generate_wiki_pages(all_notes: list[dict], agg: dict) -> dict:
    """Generate wiki pages using Claude synthesis."""
    pages = {}

    # ── Summarize meetings ──
    meetings_summary = ""
    for n in all_notes[:50]:
        meetings_summary += f"## {n['date'][:16]} | {n['fid'][:12]}\n"
        meetings_summary += f"参与: {', '.join(n['participants'][:5])}\n"
        meetings_summary += f"摘要: {n['summary'][:200]}\n"
        decs = '; '.join(d['content'][:80] for d in n['decisions'][:3])
        acts = '; '.join(f"{a['person']}: {a['task'][:60]}" for a in n['actions'][:3])
        meetings_summary += f"决策: {decs}\n"
        meetings_summary += f"行动: {acts}\n\n"

    # ── Generate Index page ──
    prompt = f"""{ORG_CHART}

## 任务：为 Plaud 知识库生成首页

基于以下会议纪要汇总，生成知识库首页 (INDEX.md)，用中文：

{meetings_summary[:8000]}

请输出 index 页面的 markdown 内容，包含：
1. 知识库概述（1-2句）
2. 人物速览（列出 TOP 10 活跃人物，每人1行简介含角色和会议数）
3. 关键客户/合作方（列出主要公司，每公司1行简介）
4. 最近动态（最近 5 场会议一句话摘要）
5. 导航区块（链接到 people/, companies/, projects/, decisions/, actions/, timeline/）

直接输出 markdown 正文：
"""
    index_md = claude_synthesize(prompt)
    if index_md:
        # Strip code fences if present
        index_md = re.sub(r'^```(?:markdown|md)?\s*\n', '', index_md)
        index_md = re.sub(r'\n```\s*$', '', index_md)
        pages["INDEX"] = index_md
        print(f"  ✅ INDEX.md ({len(index_md)} 字符)")

    # ── Generate people pages (batch) ──
    top_people = sorted(agg["people"].items(),
                       key=lambda x: len(x[1]["meetings"]) + len(x[1]["actions"]),
                       reverse=True)[:15]
    for name, data in top_people:
        meetings = "\n".join(f"- {m}" for m in data["meetings"][:10])
        decisions = "\n".join(f"- [{d['date'][:10]}] {d['content'][:100]}" for d in data["decisions"][:8])
        actions = "\n".join(f"- [ ] {a['task'][:120]}" for a in data["actions"][:8])

        prompt = f"""{ORG_CHART}

## 任务：为「{name}」生成人物档案页

背景信息：
- 参会记录：{len(data['meetings'])} 场
- 参与决策：{len(data['decisions'])} 条
- 行动项：{len(data['actions'])} 条

会议列表：
{meetings[:500]}

决策记录：
{decisions[:500]}

行动项：
{actions[:500]}

请生成人物档案 markdown（用中文），格式：
## {name}
- **角色**: 推断
- **活跃度**: 高/中/低
- **参会记录**: N场
### 关键决策
### 行动项总览
### 关联人物/客户

直接输出 markdown：
"""
        result = claude_synthesize(prompt)
        if result:
            safe_name = name.replace('/', '-').replace('\\', '-')[:30]
            pages[f"people/{safe_name}"] = result
            print(f"  ✅ {safe_name}")

    # ── Generate company pages ──
    for company, data in sorted(agg["companies"].items(), key=lambda x: len(x[1]["mentions"]), reverse=True)[:10]:
        if len(data["mentions"]) < 2:
            continue
        mentions = "\n".join(f"- {fid[:12]}" for fid in data["mentions"][:15])
        related_people = ", ".join(data["people"][:10])

        # Gather topic context
        contexts = []
        for n in all_notes:
            if n["fid"] in data["mentions"]:
                for t in n.get("topics", []):
                    if company in (t.get("title", "") + t.get("content", "")):
                        contexts.append(f"- {t['title']}: {t['content'][:200]}")

        prompt = f"""{ORG_CHART}

## 任务：为「{company}」生成客户/合作方档案

出现会议: {len(data['mentions'])} 场
关联人物: {related_people}

相关议题摘录:
{chr(10).join(contexts[:5]) if contexts else '(暂无)'}

请生成 markdown 档案（中文）：
## {company}
- **类型**: 客户/合作方/厂商
- **关联人物**: {related_people}
- **出现频率**: {len(data['mentions'])} 场会议
### 关键事项
### 决策历史
### 风险/关注点

直接输出 markdown：
"""
        result = claude_synthesize(prompt)
        if result:
            pages[f"companies/{company}"] = result
            print(f"  ✅ {company}")

    # ── Generate full decisions log ──
    all_decisions = []
    for n in all_notes:
        for d in n["decisions"]:
            all_decisions.append({
                "date": n["date"],
                "meeting": n["fid"][:12],
                "content": d["content"],
                "person": d["person"],
            })

    decs_text = "\n".join(
        f"- [{d['date'][:10]}] **{d['person']}**: {d['content'][:150]}"
        for d in sorted(all_decisions, key=lambda x: x["date"], reverse=True)[:30]
    )
    pages["decisions/INDEX"] = f"""# 决策日志

按时间倒序排列。

{decs_text}

---
*共 {len(all_decisions)} 条决策，来自 {len(all_notes)} 场会议*
"""
    print(f"  ✅ decisions/INDEX.md ({len(all_decisions)} 条决策)")

    # ── Generate actions board ──
    all_actions = []
    for n in all_notes:
        for a in n["actions"]:
            all_actions.append({
                "date": n["date"],
                "meeting": n["fid"][:12],
                "person": a["person"],
                "task": a["task"],
                "status": "pending",
            })

    # Group by person
    by_person = defaultdict(list)
    for a in all_actions:
        by_person[a["person"]].append(a)

    acts_text = ""
    for person, items in sorted(by_person.items(), key=lambda x: len(x[1]), reverse=True)[:20]:
        acts_text += f"\n### {person}\n"
        for a in items[:8]:
            acts_text += f"- [ ] [{a['date'][:10]}] {a['task'][:120]}\n"

    pages["actions/INDEX"] = f"""# 行动项看板

**总计**: {len(all_actions)} 条待办事项

{acts_text}

---
*共 {len(by_person)} 人有待办*
"""
    print(f"  ✅ actions/INDEX.md ({len(all_actions)} 条行动项)")

    # ── Generate timeline ──
    timeline = []
    for n in sorted(all_notes, key=lambda x: x["date"]):
        timeline.append({
            "date": n["date"][:16],
            "fid": n["fid"][:12],
            "summary": n["summary"][:150],
            "participants": n["participants"][:5],
            "dec_count": len(n["decisions"]),
            "act_count": len(n["actions"]),
        })

    tl_text = ""
    for t in timeline:
        participants_str = ", ".join(t["participants"][:5])
        tl_text += f"## {t['date']}\n"
        tl_text += f"参与: {participants_str}  |  {t['dec_count']}决策 {t['act_count']}行动\n"
        tl_text += f"{t['summary']}\n\n"

    pages["timeline/INDEX"] = f"""# 会议时间线

共 {len(timeline)} 场会议。

{tl_text}

---
*按时间排列*
"""
    print(f"  ✅ timeline/INDEX.md ({len(timeline)} 场会议)")

    return pages


# ══════════════════════════════════════════════════════════
# Step 4: Write all files
# ══════════════════════════════════════════════════════════

def write_pages(pages: dict):
    """Write all generated pages to disk."""
    for path, content in pages.items():
        full_path = os.path.join(KB_DIR, f"{path}.md")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            # Add header
            f.write(f"<!-- Generated: {datetime.now().isoformat()} -->\n\n")
            f.write(content)

    print(f"\n📂 知识库已生成: {KB_DIR}/")
    print(f"   共 {len(pages)} 个页面")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════╗")
    print("║  Plaud Knowledge Base Generator              ║")
    print("╚══════════════════════════════════════════════╝")

    # Step 1: Parse
    note_files = sorted([f for f in os.listdir(NOTES_DIR) if f.endswith('.md')])
    print(f"\n📖 解析 {len(note_files)} 篇纪要...")
    all_notes = []
    for f in note_files:
        note = parse_note(os.path.join(NOTES_DIR, f))
        all_notes.append(note)

    print(f"   ✅ 共 {len(all_notes)} 篇")
    total_dec = sum(len(n["decisions"]) for n in all_notes)
    total_act = sum(len(n["actions"]) for n in all_notes)
    print(f"   📊 {total_dec} 条决策, {total_act} 条行动项")

    # Step 2: Aggregate
    print(f"\n🔗 聚合实体...")
    agg = aggregate(all_notes)
    print(f"   👤 {len(agg['people'])} 人")
    print(f"   🏢 {len(agg['companies'])} 个公司/客户")
    print(f"   📋 {len(agg['projects'])} 个项目")

    # Step 3: Generate wiki pages
    print(f"\n🤖 生成 Wiki 页面...")
    pages = generate_wiki_pages(all_notes, agg)

    # Step 4: Write
    write_pages(pages)

    # Print navigation
    print(f"\n📌 入口: {KB_DIR}/INDEX.md")
    print(f"   👤 {KB_DIR}/people/")
    print(f"   🏢 {KB_DIR}/companies/")
    print(f"   ✅ {KB_DIR}/decisions/")
    print(f"   🔴 {KB_DIR}/actions/")
    print(f"   📅 {KB_DIR}/timeline/")

if __name__ == "__main__":
    main()
