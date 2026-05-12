#!/usr/bin/env python3
"""
Plaud Daily Pipeline — 每日全流程自动化
═══════════════════════════════════════════════════════════
① 云端同步     → recordings/ + transcripts/ + ai_notes/
② FunASR 分割  → transcripts_diarized/
③ Anchor 具名  → transcripts_anchor_named/
④ AI 笔记合并  → ai_notes_merged/ (具名转录 + Plaud笔记 → Claude)
⑤ Telegram 通知
⑥ 知识库重建  → wiki/ (有人物/公司/决策/行动页，仅新纪要时触发)

运行: ~/funasr-venv/bin/python3 plaud_daily_pipeline.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════

load_dotenv(Path.home() / ".hermes" / ".env")

BASE = os.path.expanduser("~/plaud-knowledge-base")
REC_DIR = os.path.join(BASE, "recordings")
TRAN_DIR = os.path.join(BASE, "transcripts")
NOTES_DIR = os.path.join(BASE, "ai_notes")
DIAR_DIR = os.path.join(BASE, "transcripts_diarized")
NAMED_DIR = os.path.join(BASE, "transcripts_anchor_named")
MERGED_DIR = os.path.join(BASE, "ai_notes_merged")
IMAGES_DIR = os.path.join(BASE, "images")

SCRIPTS = os.path.expanduser("~/.hermes/scripts")
VP_DIR = os.path.expanduser("~/.hermes/experiments/voiceprints")
ANCHOR_DB = os.path.join(VP_DIR, "anchor_db.json")
STATE_FILE = os.path.join(BASE, "sync_state.json")
PIPELINE_STATE = os.path.join(BASE, "pipeline_state.json")

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Notification: Telegram Bot (optional — set env vars to enable)
TELEGRAM_BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_HOME_CHANNEL", "")
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "")

for d in [REC_DIR, TRAN_DIR, NOTES_DIR, DIAR_DIR, NAMED_DIR, MERGED_DIR, IMAGES_DIR]:
    os.makedirs(d, exist_ok=True)

# ══════════════════════════════════════════════════════════
# Organization chart (loaded from config file)
# ══════════════════════════════════════════════════════════

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
# Utilities
# ══════════════════════════════════════════════════════════

def log(msg: str, end: str = "\n"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", end=end, flush=True)

def load_pipeline_state() -> dict:
    if os.path.exists(PIPELINE_STATE):
        with open(PIPELINE_STATE) as f:
            return json.load(f)
    return {"last_run": None, "files": {}}

def save_pipeline_state(ps: dict):
    ps["last_run"] = datetime.now().isoformat()
    with open(PIPELINE_STATE, "w") as f:
        json.dump(ps, f, indent=2, ensure_ascii=False)

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT or not TELEGRAM_CHAT:
        log("⚠ Telegram 配置缺失，跳过通知")
        return False
    try:
        proxies = {"https": TELEGRAM_PROXY, "http": TELEGRAM_PROXY} if TELEGRAM_PROXY else None
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": message, "parse_mode": "HTML"},
            proxies=proxies, timeout=15,
        )
        return resp.json().get("ok", False)
    except Exception as e:
        log(f"⚠ Telegram 发送失败: {e}")
        return False

def ms_to_str(ts_ms):
    if not ts_ms:
        return "unknown"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d_%H-%M-%S")

def ms_to_dur(ms):
    s = ms // 1000
    return f"{s//60}分{s%60}秒" if s else "?"

# ══════════════════════════════════════════════════════════
# Step ①: Plaud Cloud Sync
# ══════════════════════════════════════════════════════════

def step1_sync(ps: dict) -> list[str]:
    """Scan recordings/ folder for new files. Returns list of NEW file IDs.

    Users place .ogg recordings in recordings/ (required).
    Optionally, matching transcripts (*.md) in transcripts/ and AI notes in
    ai_notes/ will be picked up automatically.

    For Plaud API integration, see docs/integrations/plaud.md.
    """
    log("=" * 60)
    log("Step ①: 文件夹扫描")
    log("=" * 60)

    # Scan recordings/
    all_oggs = sorted(
        f.replace('.ogg', '') for f in os.listdir(REC_DIR)
        if f.endswith('.ogg') and os.path.getsize(os.path.join(REC_DIR, f)) > 1000
    )
    if not all_oggs:
        log("ℹ recordings/ 为空，跳过")
        return []

    # Load sync state
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    synced = state.get("synced_files", {})

    # Find new recordings
    new_fids = [fid for fid in all_oggs if fid not in synced]
    log(f"recordings/ 共 {len(all_oggs)} 文件，新增 {len(new_fids)}")

    if not new_fids:
        state["total_files"] = len(all_oggs)
        state["last_sync"] = datetime.now().isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return []

    # Catalog new files
    for i, fid in enumerate(new_fids):
        ogg_path = os.path.join(REC_DIR, f"{fid}.ogg")
        fname = fid[:24]
        size_mb = os.path.getsize(ogg_path) / (1024 * 1024)

        # Get duration via ffprobe
        try:
            probe = subprocess.check_output(
                f"ffprobe -v quiet -show_entries format=duration -of json '{ogg_path}'",
                shell=True
            ).decode()
            dur_s = float(json.loads(probe)["format"]["duration"])
            dur_str = f"{int(dur_s//60)}分{int(dur_s%60)}秒"
        except Exception:
            dur_s = 0
            dur_str = "?"

        # Check for matching transcript / AI notes
        has_trans = os.path.exists(os.path.join(TRAN_DIR, f"{fid}.md"))
        has_notes = os.path.exists(os.path.join(NOTES_DIR, f"{fid}.md"))

        log(f"  [{i+1}/{len(new_fids)}] {fname}... ({dur_str}, {size_mb:.1f}MB)"
            f" {'📝' if has_trans else ''}{'🤖' if has_notes else ''}")

        synced[fid] = {
            "file_name": f"{fid}.ogg",
            "duration_ms": int(dur_s * 1000),
            "has_audio": True,
            "has_transcript": has_trans,
            "has_notes": has_notes,
        }

    # Save state
    state["synced_files"] = synced
    state["total_files"] = len(all_oggs)
    state["last_sync"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    log(f"✅ 扫描完成: {len(new_fids)} 新文件")
    return new_fids

# ══════════════════════════════════════════════════════════
# Step ②: pyannote Diarization + FunASR Transcription
# ══════════════════════════════════════════════════════════
#
# 2026-05-11: Replaced FunASR CAM++ speaker model with pyannote community-1.
# pyannote handles diarization (speaker segmentation + clustering),
# FunASR paraformer handles ASR transcription.
# Results are aligned by time overlap.
# Testing showed pyannote correctly identifies 1 speaker for a solo
# recording that FunASR CAM++ split into 5 — massive improvement.

def step2_diarize(new_ids: list[str], ps: dict) -> list[str]:
    """Run pyannote diarization + FunASR transcription. Returns IDs that were successfully processed."""
    log("=" * 60)
    log("Step ②: pyannote 分割 + FunASR 转录")
    log("=" * 60)

    from funasr import AutoModel
    import torch
    from pyannote.audio import Pipeline

    # Ensure offline mode — HF is unreachable on this network
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    # Find ALL files needing diarization (not just from step 1 — handles partial failures)
    all_oggs = set(f.replace('.ogg', '') for f in os.listdir(REC_DIR) if f.endswith('.ogg'))
    all_diar = set(f.replace('.json', '') for f in os.listdir(DIAR_DIR) if f.endswith('.json'))
    needing = sorted(all_oggs - all_diar)
    todo = []
    for fid in needing:
        ogg_path = os.path.join(REC_DIR, f"{fid}.ogg")
        if os.path.exists(ogg_path):
            todo.append((fid, ogg_path))

    if not todo:
        log("无新录音需处理")
        return []

    # --- Load models ---
    log("加载 pyannote 分割模型...")
    t0 = time.time()
    pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")
    pipe.to(torch.device("cuda"))
    log(f"pyannote 就绪 ({time.time()-t0:.1f}s)")

    log("加载 FunASR 转录模型...")
    t0 = time.time()
    asr_model = AutoModel(
        model="paraformer-zh", model_revision="v2.0.4",
        vad_model="fsmn-vad", vad_model_revision="v2.0.4",
        punc_model="ct-punc-c", punc_model_revision="v2.0.4",
        disable_update=True,
    )
    log(f"FunASR 就绪 ({time.time()-t0:.1f}s)")

    success = []
    for i, (fid, path) in enumerate(todo):
        # Get duration
        try:
            out = subprocess.check_output(
                f"ffprobe -v quiet -show_entries format=duration -of json {path}",
                shell=True).decode()
            dur = float(json.loads(out)["format"]["duration"])
        except Exception:
            dur = 0

        log(f"  [{i+1}/{len(todo)}] {fid[:12]}... ({dur:.0f}s)", end=" ")

        try:
            t_start = time.time()

            # --- Phase A: pyannote diarization ---
            # Convert to WAV if needed (pyannote struggles with OGG chunk boundaries)
            wav_path = path
            if path.endswith('.ogg'):
                tmp_wav = os.path.join(tempfile.gettempdir(), f"plaud_{fid[:12]}.wav")
                subprocess.run([
                    "ffmpeg", "-y", "-i", path, "-ar", "16000", "-ac", "1", tmp_wav
                ], capture_output=True)
                if os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 1000:
                    wav_path = tmp_wav
                else:
                    log("音频转换失败", end=" ")
                    continue
                t_conv = time.time()

            diar_out = pipe(wav_path)
            diar = diar_out.speaker_diarization
            pyannote_spk_map = {}
            pyannote_segs = []
            spk_counter = 0
            for seg, _, label in diar.itertracks(yield_label=True):
                if label not in pyannote_spk_map:
                    pyannote_spk_map[label] = str(spk_counter)
                    spk_counter += 1
                pyannote_segs.append({
                    "start_s": seg.start,
                    "end_s": seg.end,
                    "spk": pyannote_spk_map[label],
                })
            t_diar = time.time()

            # --- Phase B: FunASR transcription ---
            asr_res = asr_model.generate(input=path, batch_size_s=300, sentence_timestamp=True)
            t_asr = time.time()

            # --- Phase C: Align ---
            asr_segs = asr_res[0].get("sentence_info", [])
            segments = []
            for asr_seg in asr_segs:
                ts = asr_seg.get("timestamp", [[0, 0]])[0]
                asr_start_s = ts[0] / 1000.0
                asr_end_s = ts[1] / 1000.0
                text = asr_seg.get("text", "")

                # Find pyannote speaker with max overlap
                best_spk = "?"
                best_overlap = 0
                for ps_seg in pyannote_segs:
                    overlap_start = max(asr_start_s, ps_seg["start_s"])
                    overlap_end = min(asr_end_s, ps_seg["end_s"])
                    overlap = max(0, overlap_end - overlap_start)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_spk = ps_seg["spk"]

                # Fallback: if no overlap, assign to nearest speaker by time
                if best_spk == "?" and pyannote_segs:
                    mid = (asr_start_s + asr_end_s) / 2
                    best_spk = min(pyannote_segs,
                        key=lambda ps: min(abs(mid - ps["start_s"]), abs(mid - ps["end_s"]))
                    )["spk"]

                segments.append({
                    "spk": best_spk,
                    "text": text,
                    "start_ms": ts[0],
                    "end_ms": ts[1],
                })

            elapsed = time.time() - t_start
            spk_count = len(set(s["spk"] for s in segments))

            out_path = os.path.join(DIAR_DIR, f"{fid}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "file": os.path.basename(path),
                    "duration_s": dur,
                    "processing_time_s": round(elapsed, 1),
                    "engine": "pyannote+funasr",
                    "speakers": sorted(set(s["spk"] for s in segments)),
                    "num_segments": len(segments),
                    "segments": segments,
                }, f, ensure_ascii=False, indent=2)

            log(f"✅ {elapsed:.1f}s | {len(segments)}段 {spk_count}人")
            success.append(fid)

            if fid not in ps["files"]:
                ps["files"][fid] = {}
            ps["files"][fid]["diarized"] = True
            ps["files"][fid]["file_name"] = os.path.basename(path)

        except Exception as e:
            log(f"❌ {e}")

    log(f"✅ 分割完成: {len(success)}/{len(todo)}")
    return success

# ══════════════════════════════════════════════════════════
# Step ③: Anchor Naming (Plaud labels → Voiceprint → LLM)
# ══════════════════════════════════════════════════════════

def parse_plaud_blocks(fpath: str) -> list[dict]:
    if not os.path.exists(fpath):
        return []
    with open(fpath) as f:
        text = f.read()
    meta_keys = {'日期', '时长', '转录引擎', '文件', '格式'}
    blocks = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        m = re.match(r'^\*\*(.+?)\*\*\s*`(\d+):(\d+)`$', lines[i])
        if m:
            name = m.group(1).strip()
            ts = int(m.group(2)) * 60 + int(m.group(3))
            if name in meta_keys or ':' in name:
                i += 1; continue
            text_lines = []
            i += 1
            while i < len(lines):
                if re.match(r'^\*\*(.+?)\*\*\s*`(\d+):(\d+)`$', lines[i]):
                    break
                if lines[i].strip() and not lines[i].startswith('!['):
                    text_lines.append(lines[i].strip())
                i += 1
            blocks.append({"name": name, "ts_sec": ts, "text": ' '.join(text_lines)})
        else:
            i += 1
    return blocks

def cosine_sim(a, b):
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

def extract_voiceprint(audio_path, segments, cam_model, tmp_dir, max_samples=15):
    sorted_segs = sorted(segments, key=lambda s: s["end_ms"] - s["start_ms"], reverse=True)
    samples = sorted_segs[:max_samples]
    samples.sort(key=lambda s: s["start_ms"])
    valid = [s for s in samples if (s["end_ms"] - s["start_ms"]) >= 200]
    if len(valid) < 3 or sum(s["end_ms"] - s["start_ms"] for s in valid) / 1000 < 2.0:
        return None

    concat_list = os.path.join(tmp_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for i, seg in enumerate(valid):
            seg_wav = os.path.join(tmp_dir, f"s{i}.wav")
            if not os.path.exists(seg_wav):
                start_s = max(0, seg["start_ms"] / 1000)
                dur = (seg["end_ms"] - seg["start_ms"]) / 1000
                subprocess.run([
                    "ffmpeg", "-y", "-ss", str(start_s), "-t", str(dur),
                    "-i", audio_path, "-ac", "1", "-ar", "16000", seg_wav
                ], capture_output=True)
            if os.path.exists(seg_wav) and os.path.getsize(seg_wav) > 1000:
                f.write(f"file '{seg_wav}'\n")
    concat_wav = os.path.join(tmp_dir, "concat.wav")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-ac", "1", "-ar", "16000", concat_wav
    ], capture_output=True)
    if not os.path.exists(concat_wav) or os.path.getsize(concat_wav) < 1000:
        return None

    res = cam_model.generate(input=concat_wav)
    if res and len(res) > 0:
        emb = res[0].get("spk_embedding")
        if emb is not None:
            return emb.cpu().numpy().flatten()
    return None

def step3_naming(diarized_ids: list[str], ps: dict) -> list[str]:
    """Run 3-phase anchor naming. Returns IDs that gained new names."""
    log("=" * 60)
    log("Step ③: Anchor 说话人具名")
    log("=" * 60)

    # Load CAM++
    from funasr import AutoModel
    cam = AutoModel(model="cam++", model_revision="v2.0.2", disable_update=True)
    log("CAM++ 就绪")

    # Load existing anchor DB
    naming = {}
    if os.path.exists(ANCHOR_DB):
        with open(ANCHOR_DB) as f:
            naming = json.load(f).get("naming", {})

    # Get all diarized files (not just new ones, for voiceprint propagation)
    all_diar = sorted([f.replace('.json', '') for f in os.listdir(DIAR_DIR) if f.endswith('.json')])
    new_set = set(diarized_ids)

    # Phase 1: Plaud Direct Naming
    log("Phase 1: Plaud 直接命名")
    p1_named = 0
    for fid in all_diar:
        plaud_path = os.path.join(TRAN_DIR, f"{fid}.md")
        diar_path = os.path.join(DIAR_DIR, f"{fid}.json")
        if not os.path.exists(plaud_path) or not os.path.exists(diar_path):
            continue

        blocks = parse_plaud_blocks(plaud_path)
        real_blocks = [b for b in blocks if not re.match(r'^Speaker\s+\d+$', b["name"])]
        if not real_blocks:
            continue

        with open(diar_path) as f:
            diar = json.load(f)
        diar_segs = diar.get("segments", [])

        spk_name_votes = defaultdict(Counter)
        for block in real_blocks:
            name, ts = block["name"], block["ts_sec"]
            best_seg, best_dist = None, float('inf')
            for seg in diar_segs:
                dist = abs(seg["start_ms"] / 1000 - ts)
                if dist < 15 and dist < best_dist:
                    best_seg, best_dist = seg, dist
            if best_seg:
                spk_name_votes[str(best_seg["spk"])][name] += 1

        for spk_id, name_counts in spk_name_votes.items():
            total_votes = sum(name_counts.values())
            best_name, best_votes = name_counts.most_common(1)[0]
            if best_votes / total_votes >= 0.5 and total_votes >= 2:
                if fid not in naming:
                    naming[fid] = {}
                if spk_id not in naming[fid]:
                    naming[fid][spk_id] = {
                        "name": best_name, "source": "plaud",
                        "confidence": round(best_votes / total_votes, 2),
                        "votes": total_votes,
                    }
                    p1_named += 1

    log(f"  Phase 1 新增: {p1_named} 个命名")

    # Phase 2: Voiceprint Propagation
    log("Phase 2: 声纹传播")
    speaker_segments = defaultdict(list)
    for fid, spk_names in naming.items():
        diar_path = os.path.join(DIAR_DIR, f"{fid}.json")
        if not os.path.exists(diar_path):
            continue
        with open(diar_path) as f:
            diar = json.load(f)
        segs = diar.get("segments", [])
        for spk_id, info in spk_names.items():
            name = info["name"]
            spk_segs = [s for s in segs if str(s["spk"]) == spk_id]
            for s in spk_segs:
                speaker_segments[name].append((fid, s))

    voiceprints = {}
    for name, seg_list in speaker_segments.items():
        by_fid = defaultdict(list)
        for fid, seg in seg_list:
            by_fid[fid].append(seg)
        embeddings = []
        safe_name = name.replace('/', '-').replace('\\', '-')[:20]
        tmp_root = tempfile.mkdtemp(prefix=f"vp_{safe_name}_")
        try:
            for fid in list(by_fid.keys())[:3]:
                ogg = os.path.join(REC_DIR, f"{fid}.ogg")
                if not os.path.exists(ogg):
                    continue
                emb = extract_voiceprint(ogg, by_fid[fid], cam, tmp_root)
                if emb is not None:
                    embeddings.append(emb)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
        if embeddings:
            voiceprints[name] = np.mean(embeddings, axis=0)
            log(f"  {name}: centroid from {len(embeddings)} recordings")

    if voiceprints:
        p2_named = 0
        for fid in all_diar:
            diar_path = os.path.join(DIAR_DIR, f"{fid}.json")
            ogg_path = os.path.join(REC_DIR, f"{fid}.ogg")
            if not os.path.exists(diar_path) or not os.path.exists(ogg_path):
                continue
            with open(diar_path) as f:
                diar = json.load(f)
            segs = diar.get("segments", [])
            existing = set(naming.get(fid, {}).keys())
            unnamed_groups = defaultdict(list)
            for seg in segs:
                spk = str(seg["spk"])
                if spk not in existing:
                    unnamed_groups[spk].append(seg)
            if not unnamed_groups:
                continue
            tmp_root = tempfile.mkdtemp(prefix=f"vp2_{fid[:8]}_")
            try:
                for spk_id, spk_segs in unnamed_groups.items():
                    total_dur = sum(s["end_ms"] - s["start_ms"] for s in spk_segs) / 1000
                    if total_dur < 3.0:
                        continue
                    emb = extract_voiceprint(ogg_path, spk_segs, cam, tmp_root)
                    if emb is None:
                        continue
                    best_match, best_sim = None, 0
                    for vp_name, vp_centroid in voiceprints.items():
                        sim = cosine_sim(emb, vp_centroid)
                        if sim > best_sim:
                            best_sim, best_match = sim, vp_name
                    if best_match and best_sim >= 0.55:
                        if fid not in naming:
                            naming[fid] = {}
                        if spk_id not in naming[fid]:
                            naming[fid][spk_id] = {
                                "name": best_match, "source": "voiceprint",
                                "confidence": round(best_sim, 3),
                                "similarity": round(best_sim, 3),
                            }
                            p2_named += 1
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)
        log(f"  Phase 2 新增: {p2_named} 个命名")

    # Phase 3: LLM Inference
    log("Phase 3: LLM 身份推断")
    p3_named = 0
    batch_count = 0
    for fid in all_diar:
        diar_path = os.path.join(DIAR_DIR, f"{fid}.json")
        if not os.path.exists(diar_path):
            continue
        with open(diar_path) as f:
            diar = json.load(f)
        segs = diar.get("segments", [])
        existing = set(naming.get(fid, {}).keys())

        unnamed_groups = defaultdict(list)
        for seg in segs:
            spk = str(seg["spk"])
            if spk not in existing:
                unnamed_groups[spk].append(seg)

        candidates = []
        for spk_id, spk_segs in unnamed_groups.items():
            total_dur = sum(s["end_ms"] - s["start_ms"] for s in spk_segs) / 1000
            if total_dur >= 10.0:
                texts = [s["text"][:150] for s in spk_segs[:15] if s["text"].strip()]
                candidates.append({
                    "spk_id": spk_id,
                    "duration_s": round(total_dur),
                    "samples": texts[:10],
                })
        if not candidates:
            continue

        named_here = []
        if fid in naming:
            for info in naming[fid].values():
                if info["name"] not in named_here:
                    named_here.append(info["name"])

        candidates_text = ""
        for c in candidates:
            samples_text = " | ".join(c["samples"][:5])
            candidates_text += (f"## 未知说话人 spk_{c['spk_id']}\n"
                                f"- 发言时长: {c['duration_s']}s\n"
                                f"- 发言样本: {samples_text[:500]}\n\n")

        prompt = f"""{ORG_CHART}

---

## 录音 {fid[:16]}...
## 已识别人物: {', '.join(named_here) if named_here else '无'}

## 待识别说话人

{candidates_text}

---

## 任务

对每个未知说话人，推断其身份。注意：
1. **优先判断内/外部**: 如果与团队负责人及其下属讨论业务细节→可能是内部中层；如果讨论客户需求/产品方案→可能是外部客户/合作伙伴
2. **从发言推断公司**: 提及"我们公司""我们银行""我们部门"→外部；提及"我客户""我们团队""谢总"→内部
3. **角色线索**: "汇报下项目进度"→执行层；"这个方案你们觉得怎么样"→客户方决策者
4. 无法判断时用 "外部-公司-角色" 格式标注，完全无法判断填 spk_UNKNOWN

请用JSON数组回复:
```json
[
  {{"spk_id": "0", "name": "姓名或描述", "company": "公司", "role": "角色", 
    "internal": true/false, "confidence": "high/medium/low", "evidence": "依据"}},
  ...
]
```"""

        if len(prompt) > 5000:
            # Cap candidates to avoid token limits
            candidates_text_short = ""
            for c in candidates[:8]:
                samples_text = " | ".join(c["samples"][:3])
                candidates_text_short += (f"## spk_{c['spk_id']}\n"
                                          f"- {c['duration_s']}s\n"
                                          f"- 样本: {samples_text[:300]}\n\n")
            prompt = f"""{ORG_CHART}

## 录音 {fid[:16]}...
## 已识别: {', '.join(named_here) if named_here else '无'}

## 待识别

{candidates_text_short}

## 任务: 推断每人身份。JSON数组回复:
```json
[{{"spk_id": "0", "name": "...", "confidence": "high/medium/low"}}]
```"""

        try:
            result = subprocess.run(
                [CLAUDE_BIN, "--print", "--input-format", "text",
                 "--max-turns", "5", "--model", "claude-sonnet-4-6"],
                input=prompt, capture_output=True, text=True, timeout=300
            )
            output = result.stdout.strip()
            if not output:
                continue

            json_str = None
            m = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', output)
            if m:
                json_str = m.group(1)
            else:
                m = re.search(r'\[[\s\S]*\]', output)
                if m:
                    json_str = m.group(0)
            if not json_str:
                continue

            idents = json.loads(json_str)
            for ident in idents:
                spk_id = str(ident.get("spk_id", ""))
                name = ident.get("name", "")
                if not spk_id or not name or name.startswith("spk_UNKNOWN"):
                    continue
                if fid not in naming:
                    naming[fid] = {}
                if spk_id not in naming[fid]:
                    naming[fid][spk_id] = {
                        "name": name, "source": "llm",
                        "confidence": ident.get("confidence", "low"),
                        "role": ident.get("role", ""),
                        "company": ident.get("company", ""),
                        "internal": ident.get("internal", False),
                    }
                    p3_named += 1
            batch_count += 1
        except Exception as e:
            log(f"  LLM 失败 {fid[:12]}: {e}")

    log(f"  Phase 3 新增: {p3_named} 命名 ({batch_count} 批次)")

    # Save DB
    db = {
        "version": "1.0", "phase": "daily_pipeline",
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "naming": naming,
        "total_recordings_named": len(naming),
    }
    name_counts = Counter()
    for fid_data in naming.values():
        for info in fid_data.values():
            name_counts[info["name"]] += 1
    db["speaker_coverage"] = dict(name_counts.most_common())
    with open(ANCHOR_DB, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    # Generate named transcripts
    total_named_segs = 0
    total_anon = 0
    for fid in all_diar:
        diar_path = os.path.join(DIAR_DIR, f"{fid}.json")
        if not os.path.exists(diar_path):
            continue
        with open(diar_path) as f:
            diar = json.load(f)
        fid_names = naming.get(fid, {})

        named_segs = []
        for seg in diar.get("segments", []):
            spk = str(seg["spk"])
            info = fid_names.get(spk)
            seg_copy = dict(seg)
            if info:
                seg_copy["speaker"] = info["name"]
                seg_copy["speaker_source"] = info["source"]
                seg_copy["speaker_confidence"] = info.get("confidence")
                total_named_segs += 1
            else:
                seg_copy["speaker"] = f"spk_{spk}"
                seg_copy["speaker_source"] = "unknown"
                total_anon += 1
            named_segs.append(seg_copy)

        out_json = os.path.join(NAMED_DIR, f"{fid}.json")
        with open(out_json, "w") as f:
            json.dump({
                "file": diar.get("file", fid),
                "duration": diar.get("duration"),
                "segments": named_segs,
                "speaker_map": {spk: info["name"] for spk, info in fid_names.items()},
            }, f, ensure_ascii=False, indent=2)

    total = total_named_segs + total_anon
    pct = total_named_segs / total * 100 if total else 0
    log(f"✅ 具名完成: {total_named_segs}/{total} segments ({pct:.0f}%)")

    # Track newly named
    newly_named = [fid for fid in all_diar if fid in naming and fid in new_set]
    return newly_named

# ══════════════════════════════════════════════════════════
# Step ④: AI Notes Merge (Named Transcript + Plaud Notes → Claude)
# ══════════════════════════════════════════════════════════

def step4_ai_notes(named_ids: list[str], ps: dict) -> list[dict]:
    """Generate merged AI notes from named transcripts + Plaud notes."""
    log("=" * 60)
    log("Step ④: AI 笔记合并生成")
    log("=" * 60)

    # Find ALL files with named transcripts but missing merged notes
    all_named = set(f.replace('.json', '') for f in os.listdir(NAMED_DIR) if f.endswith('.json'))
    all_merged = set(f.replace('.md', '') for f in os.listdir(MERGED_DIR) if f.endswith('.md'))
    needing = all_named - all_merged
    log(f"需生成纪要: {len(needing)} 个")

    results = []
    for fid in needing:
        named_path = os.path.join(NAMED_DIR, f"{fid}.json")
        plaud_notes_path = os.path.join(NOTES_DIR, f"{fid}.md")
        merged_path = os.path.join(MERGED_DIR, f"{fid}.md")

        if not os.path.exists(named_path):
            continue

        # Load named transcript
        with open(named_path) as f:
            named_data = json.load(f)

        # Build readable transcript with names
        transcript_lines = []
        cur_spk = None
        for seg in named_data.get("segments", []):
            spk = seg["speaker"]
            if spk != cur_spk:
                cur_spk = spk
                ts = f"{int(seg['start_ms']/60000):02d}:{int((seg['start_ms']%60000)/1000):02d}"
                transcript_lines.append(f"\n**{spk}** `{ts}`")
            transcript_lines.append(seg["text"])
        transcript_text = "\n".join(transcript_lines)

        # Load Plaud notes
        plaud_text = ""
        if os.path.exists(plaud_notes_path):
            with open(plaud_notes_path) as f:
                raw = f.read()
                # Strip header lines
                lines = raw.split('\n')
                content_start = 0
                for i, line in enumerate(lines):
                    if line.startswith('---'):
                        content_start = i + 1
                        break
                plaud_text = '\n'.join(lines[content_start:]).strip()

        # Get speakers info
        speaker_map = named_data.get("speaker_map", {})
        speakers_list = [f"{name} (source: {named_data.get('segments', [])[0].get('speaker_source', '?') if any(s.get('speaker') == name for s in named_data.get('segments', [])) else '?'})"
                         for name in speaker_map.values()]
        speakers_str = "\n".join(f"- {s}" for s in speakers_list) if speakers_list else "无具名说话人"

        # Get file info
        fname = named_data.get("file", fid)
        ps_file = ps.get("files", {}).get(fid, {})
        dur = named_data.get("duration", "?")

        # Build prompt
        prompt = f"""{ORG_CHART}

---

## 会议信息
- 文件名: {fname}
- 时长: {dur}
- 识别说话人:
{speakers_str}

## 具名转录（带说话人身份）
```
{transcript_text[:8000]}
```

## Plaud AI 笔记
{plaud_text[:3000] if plaud_text else '(无 Plaud 笔记)'}

---

## 任务：生成完整会议纪要

你是一位专业的会议记录分析师。基于上述具名转录（带说话人身份）和 Plaud AI 笔记，生成以下结构的会议纪要：

### 1. 会议摘要
2-3句话概括会议目的和核心结果。

### 2. 关键决策
列举会议中做出的重要决定，标注决策人。

### 3. 行动项
列出所有待办事项，**必须标注负责人**（利用具名信息）。格式: `- [ ] [责任人] 具体任务`

### 4. 后续跟进
下次会议时间或需要持续关注的事项。

### 5. 议题要点
按议题分段，总结每个议题的讨论要点和结论。

---

## 输出格式要求

会议纪要正文后，请追加一个 `<!--LIGHTWEIGHT-->` 标记，之后写 3-5 条精简要点（每行一条，适合手机阅读，每条 ≤80 字符）：

<!--LIGHTWEIGHT-->
- 📋 [要点1]
- ✅ [决策/行动]
- 👤 [负责人] 任务
- 📅 [后续]
"""

        try:
            result = subprocess.run(
                [CLAUDE_BIN, "--print", "--input-format", "text",
                 "--max-turns", "3", "--model", "claude-sonnet-4-6"],
                input=prompt, capture_output=True, text=True, timeout=300
            )
            output = result.stdout.strip()
            if not output:
                log(f"  ⚠ {fid[:12]}: Claude 无输出")
                continue

            # Split lightweight from full
            lightweight = ""
            full_minutes = output
            lw_match = re.search(r'<!--LIGHTWEIGHT-->\s*\n(.*)', output, re.DOTALL)
            if lw_match:
                full_minutes = output[:lw_match.start()].strip()
                lightweight = lw_match.group(1).strip()

            # Save full minutes
            file_name = ps.get("files", {}).get(fid, {}).get("file_name", fname)
            with open(merged_path, "w", encoding="utf-8") as f:
                f.write(f"# 会议纪要 — {file_name}\n\n")
                f.write(f"**日期**: {ms_to_str(ps.get('files', {}).get(fid, {}).get('start_time', 0))}\n")
                f.write(f"**时长**: {ms_to_dur(ps.get('files', {}).get(fid, {}).get('duration', 0))}\n\n")
                f.write(f"---\n\n{full_minutes}\n")

            # Extract file title from name
            title = file_name.rsplit('.', 1)[0][:30] if file_name else fid[:12]

            results.append({
                "fid": fid,
                "title": title,
                "lightweight": lightweight,
                "chars": len(full_minutes),
            })

            if fid not in ps["files"]:
                ps["files"][fid] = {}
            ps["files"][fid]["merged_notes"] = True
            log(f"  ✅ {fid[:12]}: {len(full_minutes)} 字符")

        except subprocess.TimeoutExpired:
            log(f"  ⚠ {fid[:12]}: Claude 超时")
        except Exception as e:
            log(f"  ❌ {fid[:12]}: {e}")

    log(f"✅ AI 笔记完成: {len(results)} 篇")
    return results

# ══════════════════════════════════════════════════════════
# Step ⑤: Telegram Notification
# ══════════════════════════════════════════════════════════

def step5_notify(new_ids: list[str], notes_results: list[dict]):
    """Send Telegram summary."""
    log("=" * 60)
    log("Step ⑤: Telegram 通知")
    log("=" * 60)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"📥 <b>Plaud 每日同步完成</b> ({now})"]

    if new_ids:
        lines.append(f"🆕 新增 {len(new_ids)} 个录音")
    else:
        lines.append("☁️ 无新录音")

    if notes_results:
        lines.append("")
        lines.append("📋 <b>会议纪要</b>")
        for r in notes_results[:3]:
            lines.append(f"▸ <b>{r['title']}</b>")
            if r.get("lightweight"):
                for lw_line in r["lightweight"].strip().split('\n')[:4]:
                    lw_line = lw_line.strip()
                    if lw_line:
                        lines.append(f"  {lw_line}")
        if len(notes_results) > 3:
            lines.append(f"  ...共 {len(notes_results)} 篇完整纪要")

    if new_ids and not notes_results:
        lines.append("📝 新录音已处理，查看知识库")

    if not new_ids and not notes_results:
        lines.append("✅ 知识库已是最新")

    lines.append(f"\n📂 ~/plaud-knowledge-base/")

    message = "\n".join(lines)
    send_telegram(message)

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

def main():
    log("╔══════════════════════════════════════════════╗")
    log("║  Plaud Daily Pipeline                        ║")
    log(f"║  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                            ║")
    log("╚══════════════════════════════════════════════╝")

    t_total = time.time()
    ps = load_pipeline_state()

    # ① Sync
    new_ids = step1_sync(ps)
    save_pipeline_state(ps)

    # ② FunASR Diarization (checks all recordings missing diarization)
    diarized = step2_diarize(new_ids, ps)
    save_pipeline_state(ps)

    # ③ Anchor Naming (uses DB, skips already-named)
    named = step3_naming(diarized, ps)
    save_pipeline_state(ps)

    # ④ AI Notes Merge (checks all named files missing merged notes)
    notes_results = step4_ai_notes(named, ps)
    save_pipeline_state(ps)

    # ⑤ Notify (always sends summary)
    step5_notify(new_ids, notes_results)

    # ⑥ Knowledge Base (only if new notes were generated)
    if notes_results:
        step6_kb_generate()

    log(f"⏱ 总耗时: {(time.time()-t_total)/60:.1f} min")
    log("✅ 全流程完成")

# ══════════════════════════════════════════════════════════
# Step ⑥: Knowledge Base Wiki Generation
# ══════════════════════════════════════════════════════════

def step6_kb_generate():
    """Rebuild wiki knowledge base from merged AI notes."""
    log("=" * 60)
    log("Step ⑥: 知识库 Wiki 重建")
    log("=" * 60)

    kb_script = os.path.join(SCRIPTS, "plaud_kb_generate.py")
    try:
        result = subprocess.run(
            [sys.executable, "-u", kb_script],
            capture_output=True, text=True, timeout=900,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}
        )
        # Show key lines from output
        for line in result.stdout.split('\n'):
            if any(kw in line for kw in ['✅ 共', '📊', '👤', '🏢', '📂', '📌']):
                log(f"  {line.strip()}")
        if result.returncode != 0:
            log(f"  ⚠ 知识库生成异常: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("  ⚠ 知识库生成超时")
    except Exception as e:
        log(f"  ❌ 知识库生成失败: {e}")

if __name__ == "__main__":
    main()
