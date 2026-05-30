#!/usr/bin/env python3
"""WRG Monitor Dashboard — serves rekap & resume files in browser.

Usage:
    python3 scripts/dashboard.py [--port 8090] [--bind 127.0.0.1]

Buka http://localhost:8090 di browser.
"""
from __future__ import annotations

import argparse
import datetime
import html
import http.server
import json
import os
import re
import socketserver
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
REKAP_DIR = DATA_DIR / "rekap"
RESUME_DIR = DATA_DIR / "resume"
POLA_DIR = DATA_DIR / "pola"
BRIEFING_DIR = DATA_DIR / "briefing"
ASSETS_DIR = PROJECT_DIR / "assets"

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".gif": "image/gif",
    ".ico": "image/x-icon",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MEMBERS_FILE = DATA_DIR / "members.json"
FILE_TS_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{4})\.txt$")


def parse_filename_time(path: Path) -> datetime.datetime | None:
    m = FILE_TS_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1) + "_" + m.group(2), "%Y-%m-%d_%H%M")
    except ValueError:
        return None


def list_dates() -> list[str]:
    dates = set()
    for base in (REKAP_DIR, RESUME_DIR):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and DATE_RE.match(child.name):
                dates.add(child.name)
    return sorted(dates, reverse=True)


def list_files(base: Path, date: str) -> list[dict]:
    day_dir = base / date
    if not day_dir.is_dir():
        return []
    kind = "rekap" if base.name == "rekap" else ("resume" if base.name == "resume" else "other")
    out = []
    for f in sorted(day_dir.glob("*.txt")):
        when = parse_filename_time(f)
        content = f.read_text(encoding="utf-8", errors="replace")
        item = {
            "name": f.name,
            "path": str(f.relative_to(PROJECT_DIR)),
            "time": when.strftime("%H:%M") if when else "?",
            "mtime": f.stat().st_mtime,
            "size": f.stat().st_size,
            "content": content,
            "kind": kind,
        }
        if kind == "rekap":
            item["parsed"] = parse_rekap_structured(content)
        elif kind == "resume":
            item["parsed"] = parse_resume_structured(content)
        out.append(item)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def latest_resume(date: str) -> dict | None:
    items = list_files(RESUME_DIR, date)
    return items[0] if items else None


# ── KONFIRMASI parser ──────────────────────────────────────────
_KE_RE = re.compile(r"\bke:\s*([^|]+)")
_AGE_RE = re.compile(r"umur\s+(\d+)\s+jam(?:\s+(\d+)\s+menit)?")
_JID_RE = re.compile(r"^(\d[\d\-]*@g\.us)(?:\s*\((.+?)\))?\s*$")
# rekap.sh writes "LABEL (JID@g.us)" — label-first is the primary format
_LABEL_JID_RE = re.compile(r"^(.+?)\s*\((\d[\d\-]*@g\.us)\)\s*$")
_PLACEHOLDER_TOPICS = {"tidak ada", "tidak ada outstanding"}


def _parse_group_header(line: str):
    """Parse 'LABEL (JID)' (rekap-style) or 'JID' / 'JID (label)' (legacy).
    Returns (jid, label) or (None, None)."""
    m = _LABEL_JID_RE.match(line)
    if m:
        return m.group(2), m.group(1).strip()
    m = _JID_RE.match(line)
    if m:
        return m.group(1), (m.group(2) or "").strip()
    return None, None


def _is_placeholder_bullet(entry: dict) -> bool:
    """Filter out '• Tidak ada' / '• Tidak ada outstanding' placeholder bullets."""
    topic = (entry.get("topic") or "").strip().lower()
    return topic in _PLACEHOLDER_TOPICS


def _parse_bullet_line(line: str):
    """Detect bullet lines. Returns (level, text) — level 0 = top '•',
    indented '-' = level computed from leading spaces (4-space indent per level).
    Returns (None, None) for non-bullet lines."""
    stripped = line.strip()
    if not stripped:
        return None, None
    if stripped.startswith("•"):
        return 0, stripped.lstrip("•").strip()
    if stripped.startswith("-"):
        indent = len(line) - len(line.lstrip(" "))
        level = max(1, indent // 4)
        return level, stripped.lstrip("-").strip()
    return None, None
_REKAP_HEADER_RE = re.compile(r"^REKAP WRG\s*\|\s*(\S+)\s*WIB\s*\|\s*(\S+)")
_RESUME_HEADER_RE = re.compile(r"^RESUME EKSEKUTIF WRG\s*$")
_SECTION_RE = re.compile(r"^(\d+)\.\s+(.+)")


def truncate_at_footer(content: str, kind: str) -> str:
    """Cut everything after the natural footer to remove prompt-template leakage."""
    lines = content.splitlines()
    if kind == "rekap":
        for i, ln in enumerate(lines):
            if ln.strip().startswith("GRUP AKTIF"):
                return "\n".join(lines[: i + 1])
    elif kind == "resume":
        for i, ln in enumerate(lines):
            if ln.strip().startswith("Generated:"):
                return "\n".join(lines[: i + 1])
    return content


# === LID resolver: @<14-20 digit LID> → @<name> or @<+phone> ===
_LID_DIR = Path.home() / ".openclaw" / "credentials" / "whatsapp" / "default"
_LID_MENTION_RE = re.compile(r"@(\d{10,20})")
_lid_cache = {"map": None, "loaded_at": 0.0}
_LID_CACHE_TTL = 300  # 5 minutes


def _load_lid_resolver() -> dict:
    """Build {lid_digits: display_name_or_phone}. Cached 5min for performance.
    Layer order (first wins): data/lid_labels.json override → members.json name
    via LID→phone lookup → +<phone> fallback when no name match."""
    now = time.time()
    if _lid_cache["map"] is not None and (now - _lid_cache["loaded_at"]) < _LID_CACHE_TTL:
        return _lid_cache["map"]
    # phone (no plus) → name
    phone_to_name = {}
    try:
        with open(MEMBERS_FILE) as f:
            data = json.load(f)
        for m in data.get("members", []):
            ph = re.sub(r"\D", "", m.get("phone") or "")
            nm = (m.get("name") or "").strip()
            if ph and nm:
                phone_to_name[ph] = nm
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # User-override layer: data/lid_labels.json (authoritative untuk LID yang
    # tidak ke-link ke phone yang ada di members.json)
    lid_override = {}
    labels_path = DATA_DIR / "lid_labels.json"
    if labels_path.is_file():
        try:
            with open(labels_path) as f:
                raw = json.load(f) or {}
            lid_override = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            pass
    # lid → phone, then resolve to name if possible
    out = {}
    if _LID_DIR.is_dir():
        for f in _LID_DIR.glob("lid-mapping-*_reverse.json"):
            lid = f.name.replace("lid-mapping-", "").replace("_reverse.json", "")
            try:
                with open(f) as fp:
                    val = json.load(fp)
                if isinstance(val, str):
                    # Priority: user override > members.json name > +phone fallback
                    out[lid] = lid_override.get(lid) or phone_to_name.get(val) or ("+" + val)
            except (OSError, json.JSONDecodeError):
                continue
    # Add overrides for LIDs with no reverse mapping file (rare)
    for lid, name in lid_override.items():
        if lid not in out:
            out[lid] = name
    _lid_cache["map"] = out
    _lid_cache["loaded_at"] = now
    return out


def resolve_lids_in_text(text: str) -> str:
    """Replace @<lid> mentions in text with @<resolved-name-or-phone>."""
    if not text or "@" not in text:
        return text
    lid_map = _load_lid_resolver()
    if not lid_map:
        return text
    def _sub(m):
        return "@" + lid_map[m.group(1)] if m.group(1) in lid_map else m.group(0)
    return _LID_MENTION_RE.sub(_sub, text)


def parse_kv_bullet(line: str) -> dict:
    """Parse '• topic | k1: v1 | k2: v2 | ...' into {topic, fields, raw, tua}.
    Auto-resolves @<LID> mentions in topic and field values to @<Name>."""
    raw = line.strip()
    body = raw.lstrip("•").strip()
    parts = [p.strip() for p in body.split("|")]
    fields = {}
    topic = parts[0] if parts else body
    for p in parts[1:]:
        m = re.match(r"^([\w\s]+?):\s*(.+)$", p)
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            fields[key] = resolve_lids_in_text(m.group(2).strip())
    return {
        "topic": resolve_lids_in_text(topic.replace("[TUA]", "").strip()),
        "fields": fields,
        "tua": "[TUA]" in raw,
        "raw": raw,
    }


def parse_rekap_structured(content: str) -> dict:
    """Parse a rekap file into {header, groups[], konfirmasi{confirmed[], pending[]}, footer}."""
    content = truncate_at_footer(content, "rekap")
    out = {
        "header": None,
        "groups": [],
        "konfirmasi": {"confirmed": [], "pending": []},
        "footer": {"urgent": None, "grup_aktif": None},
    }
    state = "head"  # head → groups → konfirmasi(confirmed|pending) → footer
    current_group = None

    def flush_group():
        nonlocal current_group
        if current_group and (current_group["bullets"] or current_group["actions"]):
            out["groups"].append(current_group)
        current_group = None

    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if state == "head":
            m = _REKAP_HEADER_RE.match(stripped)
            if m:
                out["header"] = {"time": m.group(1), "date": m.group(2)}
                state = "groups"
                continue
        if stripped.startswith("==="):
            flush_group()
            continue
        # Konfirmasi section markers
        if "SUDAH DIKONFIRMASI" in stripped:
            flush_group(); state = "confirmed"; continue
        if "MENUNGGU KONFIRMASI" in stripped:
            flush_group(); state = "pending"; continue
        if stripped.startswith("URGENT:"):
            flush_group(); state = "footer"
            out["footer"]["urgent"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("GRUP AKTIF"):
            out["footer"]["grup_aktif"] = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
            continue

        # In groups section
        if state == "groups":
            jid, label = _parse_group_header(stripped)
            if jid:
                flush_group()
                current_group = {"jid": jid, "label": label, "bullets": [], "actions": []}
                continue
            if current_group:
                lvl, text = _parse_bullet_line(line)
                if lvl is not None:
                    current_group["bullets"].append({"text": text, "level": lvl})
                    continue
            if stripped.startswith("→ ACTION") and current_group:
                act = stripped.replace("→ ACTION:", "").strip()
                # split "PIC - tugas"
                pic, _, task = act.partition(" - ")
                current_group["actions"].append({"pic": pic.strip(), "task": task.strip() or act})
                continue
        # In konfirmasi section
        if state in ("confirmed", "pending") and stripped.startswith("•"):
            entry = parse_kv_bullet(stripped)
            if _is_placeholder_bullet(entry):
                continue  # skip "• Tidak ada" placeholders
            out["konfirmasi"][state].append(entry)
            continue
    flush_group()
    return out


def parse_briefing_structured(content: str) -> dict:
    """Parse weekend briefing .txt into {header, sections[{id,title,body}], footer}.
    Sections marked by '## A. TITLE' / '## B. TITLE' / ... / '## RINGKASAN REKOMENDASI'.
    Strips '====' separator lines and final 'Generated otomatis...' footer block."""
    lines = content.splitlines()
    header_lines = []
    sections = []
    current = None
    footer_lines = []
    in_footer = False
    for line in lines:
        stripped = line.strip()
        # Skip separator lines like "==============="
        if stripped.startswith("===") and set(stripped) == {"="}:
            continue
        if line.startswith("## "):
            if current:
                sections.append(current)
            title = line[3:].strip()
            # Extract short id: "A. RINGKASAN..." → "A", "RINGKASAN REKOMENDASI..." → "Rekomendasi"
            m = re.match(r"^([A-Z])\.\s+(.+)$", title)
            if m:
                sid = m.group(1)
                short_title = m.group(2)
            elif "RINGKASAN REKOMENDASI" in title.upper():
                sid = "Rek"
                short_title = "Ringkasan Rekomendasi"
            else:
                sid = title[:3]
                short_title = title
            current = {"id": sid, "title": title, "short": short_title, "body": []}
            in_footer = False
            continue
        # Detect transition to footer (after last section, lines like "**Generated otomatis...")
        if current is None and not header_lines:
            # Pre-header (rarely happens)
            pass
        if current is None:
            header_lines.append(line)
            continue
        # Within a section: check if we entered footer territory
        if "Generated otomatis" in stripped or "Generated by" in stripped:
            in_footer = True
        if in_footer:
            footer_lines.append(line)
        else:
            current["body"].append(line)
    if current:
        sections.append(current)
    # Clean up bodies — strip leading/trailing blank lines
    for s in sections:
        while s["body"] and not s["body"][0].strip():
            s["body"].pop(0)
        while s["body"] and not s["body"][-1].strip():
            s["body"].pop()
        s["body"] = "\n".join(s["body"])
    # Parse header lines (first 3 typically: title, label, disiapkan)
    header_text = "\n".join(l for l in header_lines if l.strip())
    title = ""
    label = ""
    disiapkan = ""
    for ln in header_lines:
        s = ln.strip().lstrip("#").strip()
        if not s: continue
        if s.startswith("BRIEFING DIREKTUR") or "BRIEFING DIREKTUR" in s:
            title = s
        elif "Briefing" in s or "Minggu" in s:
            label = s.strip("*").strip()
        elif s.startswith("**Disiapkan") or s.startswith("Disiapkan"):
            disiapkan = s.strip("*").replace("Disiapkan:", "").strip()
    return {
        "header": {"title": title, "label": label, "disiapkan": disiapkan, "raw": header_text},
        "sections": sections,
        "footer": "\n".join(footer_lines).strip(),
    }


def parse_pola_markdown(content: str) -> dict:
    """Parse pola .md into sections by '## Section Title' headers."""
    sections = []
    current = None
    lines = content.splitlines()
    # Skip top-matter (everything before first ## ) into "header" field
    header_lines = []
    for line in lines:
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = {"title": line[3:].strip(), "body": []}
        elif current is not None:
            current["body"].append(line)
        else:
            header_lines.append(line)
    if current:
        sections.append(current)
    # Clean up bodies — strip leading/trailing blank lines
    for s in sections:
        while s["body"] and not s["body"][0].strip():
            s["body"].pop(0)
        while s["body"] and not s["body"][-1].strip():
            s["body"].pop()
        s["body"] = "\n".join(s["body"])
    return {
        "header": "\n".join(header_lines).strip(),
        "sections": sections,
    }


def parse_resume_structured(content: str) -> dict:
    """Parse a resume file into {header, sections{1..8}, konfirmasi, hod_routing}."""
    content = truncate_at_footer(content, "resume")
    out = {
        "header": None,
        "sections": [],  # [{num, title, paragraph, bullets, hod_groups?}]
        "konfirmasi": {"confirmed": [], "outstanding": []},
        "generated": None,
    }
    state = "head"
    current = None
    sub = None  # within section 4: "confirmed" or "outstanding"
    hod_current = None  # within section 8: currently-collecting HOD label

    def push_current():
        nonlocal current
        if current:
            out["sections"].append(current)
            current = None

    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if state == "head":
            if _RESUME_HEADER_RE.match(stripped):
                state = "meta"
                continue
        if state == "meta" and stripped.startswith("==="):
            state = "sections"; continue
        if state == "meta":
            # 2026-05-13 | 18:21 WIB | 7 Jam Terakhir (dari 8 rekap)
            parts = [p.strip() for p in stripped.split("|")]
            out["header"] = {"date": parts[0] if parts else "", "time": (parts[1] if len(parts) > 1 else ""), "scope": (parts[2] if len(parts) > 2 else "")}
            continue
        if stripped.startswith("Generated:"):
            out["generated"] = stripped.split(":", 1)[1].strip()
            push_current()
            break
        if stripped.startswith("==="):
            push_current()
            continue
        m = _SECTION_RE.match(stripped)
        if m:
            push_current()
            current = {"num": int(m.group(1)), "title": m.group(2).strip(), "paragraph": "", "bullets": []}
            if int(m.group(1)) == 8:
                current["hod_groups"] = []  # [{label, bullets}]
            sub = None
            hod_current = None
            continue
        if not current:
            continue
        # Section 4 sub-headers
        if current["num"] == 4:
            if "TERKONFIRMASI" in stripped or "SUDAH DIKONFIRMASI" in stripped:
                sub = "confirmed"; continue
            if "OUTSTANDING" in stripped or "MENUNGGU" in stripped:
                sub = "outstanding"; continue
            if stripped.startswith("•") and sub:
                entry = parse_kv_bullet(stripped)
                if _is_placeholder_bullet(entry):
                    continue  # skip "• Tidak ada" placeholders
                out["konfirmasi"][sub].append(entry)
                continue
        # Section 8 HOD — bullets diawali "[HOD <label>] <body>"
        if current["num"] == 8:
            if stripped.startswith("•"):
                rest = stripped.lstrip("•").strip()
                # Match "[HOD <label>] <body>"
                m_hod = re.match(r'^\[HOD([^\]]*)\]\s*(.*)$', rest)
                if m_hod:
                    label = "HOD" + m_hod.group(1).rstrip()
                    body = m_hod.group(2).strip()
                    # Find or create group with this label
                    existing = next((g for g in current["hod_groups"] if g["label"] == label), None)
                    if existing is None:
                        existing = {"label": label, "bullets": []}
                        current["hod_groups"].append(existing)
                    if body:
                        existing["bullets"].append({"text": body, "level": 0})
                    continue
                # Bullet without HOD prefix — fall through to normal bullets
            elif stripped.lower().startswith("tidak ada"):
                # Section 8 totally empty
                continue
        # Normal bullets — support indented sub-bullets
        lvl, btext = _parse_bullet_line(line)
        if lvl is not None:
            current["bullets"].append({"text": btext, "level": lvl})
            continue
        # Free paragraph (e.g., Situasi Umum)
        if not stripped.startswith("•"):
            current["paragraph"] = (current["paragraph"] + " " + stripped).strip()
    push_current()
    return out


def parse_rekap_stats(content: str) -> dict:
    """Count confirmed/pending bullets in a rekap's KONFIRMASI section + extract PIC + age."""
    confirmed = 0
    pending: list[dict] = []
    section = None  # None | "confirmed" | "pending"
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "SUDAH DIKONFIRMASI" in line or "TERKONFIRMASI BARU" in line:
            section = "confirmed"; continue
        if "MENUNGGU KONFIRMASI" in line or "OUTSTANDING" in line:
            section = "pending"; continue
        if line.startswith("=") or re.match(r"^\d+\.\s", line) or line.startswith("URGENT:") or line.startswith("GRUP AKTIF") or line.startswith("Generated"):
            section = None; continue
        if not line.startswith("•"):
            continue
        if section == "confirmed":
            confirmed += 1
        elif section == "pending":
            ke_m = _KE_RE.search(line)
            pic = ke_m.group(1).strip() if ke_m else "(unknown)"
            age_m = _AGE_RE.search(line)
            age_h = (int(age_m.group(1)) + (int(age_m.group(2) or 0) / 60)) if age_m else None
            pending.append({"pic": pic, "age_h": age_h, "tua": "[TUA]" in line})
    return {"confirmed": confirmed, "pending": pending, "pending_count": len(pending)}


def aggregate_stats(rekaps: list[dict], resumes: list[dict]) -> dict:
    """Build timeline + top-PIC + age-histogram from parsed rekaps/resumes."""
    timeline = []
    for r in sorted(rekaps, key=lambda x: x["mtime"]):  # chronological for timeline
        s = parse_rekap_stats(r["content"])
        timeline.append({"time": r["time"], "confirmed": s["confirmed"], "pending": s["pending_count"]})

    # Top pending PICs — aggregate from the LATEST resume (cross-rekap view).
    pic_counts: dict[str, int] = {}
    age_buckets = {"<1h": 0, "1-2h": 0, "2-4h": 0, "4-8h": 0, ">8h": 0}
    tua_count = 0
    source = resumes[0] if resumes else (rekaps[0] if rekaps else None)
    if source:
        s = parse_rekap_stats(source["content"])
        for p in s["pending"]:
            pic_counts[p["pic"]] = pic_counts.get(p["pic"], 0) + 1
            if p["tua"]:
                tua_count += 1
            if p["age_h"] is None:
                continue
            h = p["age_h"]
            if h < 1: age_buckets["<1h"] += 1
            elif h < 2: age_buckets["1-2h"] += 1
            elif h < 4: age_buckets["2-4h"] += 1
            elif h < 8: age_buckets["4-8h"] += 1
            else: age_buckets[">8h"] += 1

    top_pic = sorted(pic_counts.items(), key=lambda x: -x[1])[:6]
    total_confirmed = sum(t["confirmed"] for t in timeline)
    total_pending_now = (source and parse_rekap_stats(source["content"])["pending_count"]) or 0

    return {
        "timeline": timeline,
        "top_pic": [{"pic": k, "count": v} for k, v in top_pic],
        "age_buckets": age_buckets,
        "totals": {
            "rekap_count": len(rekaps),
            "resume_count": len(resumes),
            "confirmed_today": total_confirmed,
            "pending_now": total_pending_now,
            "tua_now": tua_count,
        },
    }


def merge_rekap_day(rekaps: list[dict]) -> dict | None:
    """Merge all rekap firings within a day into a single structured view.

    Strategy:
      - Groups: dedupe by JID; accumulate unique bullets + actions across firings.
      - Konfirmasi.confirmed: union across all rekaps (dedupe by topic).
      - Konfirmasi.pending: take from LATEST rekap only (most up-to-date).
      - Footer: from latest rekap.
    """
    if not rekaps:
        return None
    chronological = sorted(rekaps, key=lambda r: r["mtime"])
    latest = chronological[-1]
    merged = {
        "count": len(rekaps),
        "first_time": None,
        "last_time": None,
        "date": None,
        "groups": [],
        "konfirmasi": {"confirmed": [], "pending": []},
        "footer": {},
    }
    by_jid: dict[str, dict] = {}
    seen_confirmed_topics: set[str] = set()

    for r in chronological:
        p = r.get("parsed") or {}
        h = p.get("header") or {}
        if h.get("date") and not merged["date"]:
            merged["date"] = h["date"]
        if r is chronological[0]:
            merged["first_time"] = r.get("time")
        merged["last_time"] = r.get("time")

        # Merge groups
        for g in p.get("groups", []):
            jid = g["jid"]
            if jid not in by_jid:
                by_jid[jid] = {"jid": jid, "label": g.get("label", ""), "bullets": [], "actions": [], "firings": []}
            entry = by_jid[jid]
            if g.get("label") and not entry["label"]:
                entry["label"] = g["label"]
            # Bullets are now dicts {text, level}. Dedup by (text, level).
            existing_keys = set()
            for b in entry["bullets"]:
                if isinstance(b, dict):
                    existing_keys.add((b.get("text", ""), b.get("level", 0)))
                else:  # legacy string (defensive)
                    existing_keys.add((b, 0))
            for b in g.get("bullets", []):
                if isinstance(b, dict):
                    key = (b.get("text", ""), b.get("level", 0))
                else:
                    key = (b, 0)
                    b = {"text": b, "level": 0}
                if key not in existing_keys:
                    entry["bullets"].append(b)
                    existing_keys.add(key)
            existing_actions = {(a["pic"], a["task"]) for a in entry["actions"]}
            for a in g.get("actions", []):
                key = (a["pic"], a["task"])
                if key not in existing_actions:
                    entry["actions"].append(a)
                    existing_actions.add(key)
            if r.get("time") not in entry["firings"]:
                entry["firings"].append(r.get("time"))

        # Accumulate confirmed (dedupe by topic)
        for c in p.get("konfirmasi", {}).get("confirmed", []):
            topic_key = c.get("topic", "").strip().lower()
            if topic_key and topic_key not in seen_confirmed_topics:
                seen_confirmed_topics.add(topic_key)
                merged["konfirmasi"]["confirmed"].append(c)

    # Pending from latest rekap only
    latest_p = latest.get("parsed") or {}
    merged["konfirmasi"]["pending"] = latest_p.get("konfirmasi", {}).get("pending", [])
    merged["footer"] = latest_p.get("footer", {})
    merged["groups"] = sorted(by_jid.values(), key=lambda g: g["jid"])
    return merged


def merge_resume_day(resumes: list[dict]) -> dict | None:
    """Daily collective resume = latest resume (already does 7h cross-rekap sync).

    Just adds metadata about how many firings happened that day.
    """
    if not resumes:
        return None
    latest = max(resumes, key=lambda r: r["mtime"])
    return {
        "count": len(resumes),
        "first_time": min(r["time"] for r in resumes if r.get("time")),
        "last_time": latest.get("time"),
        "parsed": latest.get("parsed"),
    }


def latest_mtime_in_dir(base: Path) -> float | None:
    """Find the most recent mtime among all .txt or .md files in base/<date>/ (1 level deep)."""
    if not base.is_dir():
        return None
    latest = 0.0
    # Top-level files (briefing has files directly, not date-grouped)
    for f in base.glob("*.txt"):
        if f.is_file():
            latest = max(latest, f.stat().st_mtime)
    for f in base.glob("*.md"):
        if f.is_file():
            latest = max(latest, f.stat().st_mtime)
    # Nested by date
    for d in base.iterdir():
        if d.is_dir() and DATE_RE.match(d.name):
            for f in d.iterdir():
                if f.is_file():
                    latest = max(latest, f.stat().st_mtime)
    return latest if latest > 0 else None


def latest_message_capture() -> tuple[float | None, int]:
    """Find latest message capture mtime + count of messages today."""
    today = datetime.date.today().isoformat()
    msg_today = DATA_DIR / "messages" / today
    if not msg_today.is_dir():
        return None, 0
    latest = 0.0
    count = 0
    for f in msg_today.glob("*.jsonl"):
        if f.is_file():
            latest = max(latest, f.stat().st_mtime)
            # rough line count
            try:
                with open(f) as fh:
                    count += sum(1 for _ in fh)
            except OSError:
                pass
    return (latest if latest > 0 else None), count


def get_lan_ip() -> str:
    """Best-effort detect LAN IP."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def system_status() -> dict:
    """Snapshot for dashboard status widget."""
    import subprocess

    now = datetime.datetime.now().timestamp()
    last_capture, capture_count = latest_message_capture()
    out = {
        "rekap": {"mtime": latest_mtime_in_dir(REKAP_DIR)},
        "resume": {"mtime": latest_mtime_in_dir(RESUME_DIR)},
        "pola": {"mtime": latest_mtime_in_dir(POLA_DIR)},
        "briefing": {"mtime": latest_mtime_in_dir(DATA_DIR / "briefing")},
        "capture": {"mtime": last_capture, "count_today": capture_count},
        "now": now,
    }
    # Cost from openclaw
    try:
        result = subprocess.run(
            ["openclaw", "gateway", "usage-cost"],
            capture_output=True, text=True, timeout=5,
        )
        # Parse "Total: $X.XX · NNNk tokens"
        for line in result.stdout.splitlines():
            m = re.search(r"Total:\s*\$([\d.]+)\s*·\s*(\S+)", line)
            if m:
                out["cost"] = {"total_usd": float(m.group(1)), "tokens": m.group(2)}
                break
            m = re.search(r"Latest day:\s*(\S+)\s*·\s*\$([\d.]+)\s*·\s*(\S+)", line)
            if m and "cost" in out:
                out["cost"]["today_date"] = m.group(1)
                out["cost"]["today_usd"] = float(m.group(2))
                out["cost"]["today_tokens"] = m.group(3)
    except Exception:
        pass
    return out


def weekly_stats(end_date: str, days: int = 7) -> list[dict]:
    """Per-day rollup of confirmed/pending/tua across last `days` days (ending at end_date)."""
    try:
        end = datetime.date.fromisoformat(end_date)
    except ValueError:
        end = datetime.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (end - datetime.timedelta(days=i)).isoformat()
        rekaps = list_files(REKAP_DIR, d)
        resumes = list_files(RESUME_DIR, d)
        if not rekaps and not resumes:
            out.append({"date": d, "confirmed": 0, "pending": 0, "tua": 0, "has_data": False})
            continue
        st = aggregate_stats(rekaps, resumes)
        out.append({
            "date": d,
            "confirmed": st["totals"]["confirmed_today"],
            "pending": st["totals"]["pending_now"],
            "tua": st["totals"]["tua_now"],
            "has_data": True,
        })
    return out


def latest_briefing_meta() -> dict | None:
    """Return latest briefing file metadata + Section A excerpt (compact, ~1KB)
    for Overview card preview. Returns None if no briefings exist."""
    if not BRIEFING_DIR.is_dir():
        return None
    files = sorted(BRIEFING_DIR.glob("briefing_*.txt"), reverse=True)
    if not files:
        return None
    f = files[0]
    m = re.match(r"briefing_(\d{4}-\d{2}-\d{2})_(\d{4})\.txt$", f.name)
    if not m:
        return None
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = parse_briefing_structured(content)
    # Excerpt of Section A (Ringkasan Eksekutif) — first ~500 chars
    section_a = next((s for s in parsed["sections"] if s.get("id") == "A"), None)
    excerpt = ""
    if section_a:
        body = section_a["body"].strip()
        excerpt = body[:500] + ("…" if len(body) > 500 else "")
    return {
        "filename": f.name,
        "date": m.group(1),
        "time": m.group(2)[:2] + ":" + m.group(2)[2:],
        "size": f.stat().st_size,
        "mtime": int(f.stat().st_mtime),
        "label": parsed["header"].get("label", ""),
        "disiapkan": parsed["header"].get("disiapkan", ""),
        "section_count": len(parsed["sections"]),
        "ringkasan_excerpt": excerpt,
    }


def fetch_data(date: str) -> dict:
    rekaps = list_files(REKAP_DIR, date)
    resumes = list_files(RESUME_DIR, date)
    daily = aggregate_stats(rekaps, resumes)
    return {
        "date": date,
        "dates": list_dates(),
        "rekap": rekaps,
        "resume": resumes,
        "latest_resume": resumes[0] if resumes else None,
        "latest_briefing": latest_briefing_meta(),
        "collective_rekap": merge_rekap_day(rekaps),
        "collective_resume": merge_resume_day(resumes),
        "stats": {**daily, "weekly": weekly_stats(date)},
        "system_status": system_status(),
        "lan_ip": get_lan_ip(),
        "now": datetime.datetime.now().isoformat(timespec="seconds"),
    }


INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f0f4f8">
<title>WRG Monitor — Wahana LifeLine</title>
<link rel="icon" type="image/png" href="/assets/logo-wahana-lifeline.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ═══════════════════════════════════════════════════════════════════
   ADMINATOR-INSPIRED DESIGN TOKENS
   Light + dark variant via data-theme attribute on <html>
   ═══════════════════════════════════════════════════════════════════ */
:root, [data-theme="light"] {
  /* Surfaces — Adminator palette */
  --bg-page:        #f0f4f8;
  --bg-panel:       #ffffff;
  --bg-sidebar:     #ffffff;
  --bg-topbar:      #ffffff;
  --bg-soft:        #f1f5f9;
  --bg-hover:       #f8fafc;
  --bg-muted:       #f1f5f9;
  /* Text — Adminator slate scale */
  --text-primary:   #1e293b;
  --text-secondary: #64748b;
  --text-muted:     #94a3b8;
  /* Sidebar text (now white sidebar) — same as body text in light mode */
  --text-on-dark:   #64748b;          /* inactive nav */
  --text-on-dark-active: #c2410c;     /* active nav = WRG orange */
  --text-on-dark-muted:  #94a3b8;
  /* Brand accent — WRG orange (retained) */
  --accent:         #c2410c;
  --accent-soft:    rgba(194, 65, 12, 0.10);
  --accent-strong:  #9a3208;
  /* Status colors — Adminator tailwind-style */
  --info:           #0ea5e9;
  --info-soft:      #f0f9ff;
  --ok:             #10b981;
  --ok-soft:        #ecfdf5;
  --warn:           #f59e0b;
  --warn-soft:      #fffbeb;
  --danger:         #ef4444;
  --danger-soft:    #fef2f2;
  /* Borders & shadow — Adminator */
  --border:         #e4e8ef;
  --border-soft:    #eef1f5;
  --shadow-sm:      0 1px 2px 0 rgba(15, 23, 42, 0.04);
  --shadow-md:      0 1px 3px 0 rgba(15, 23, 42, 0.06), 0 1px 2px -1px rgba(15, 23, 42, 0.04);
  --shadow-lg:      0 10px 15px -3px rgba(15, 23, 42, 0.08), 0 4px 6px -4px rgba(15, 23, 42, 0.05);
  /* Geometry */
  --radius-sm:      4px;
  --radius:         8px;
  --radius-lg:      12px;
  --sidebar-width:  220px;
  --sidebar-width-collapsed: 60px;
  --topbar-height:  56px;
}
[data-theme="dark"] {
  --bg-page:        #0b1120;
  --bg-panel:       #141b2d;
  --bg-sidebar:     #141b2d;
  --bg-topbar:      #141b2d;
  --bg-soft:        #1a2237;
  --bg-hover:       #1c2438;
  --bg-muted:       #1a2237;
  --text-primary:   #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted:     #64748b;
  --text-on-dark:   #94a3b8;          /* inactive nav in dark */
  --text-on-dark-active: #fb923c;     /* active = light orange (dark mode visible) */
  --text-on-dark-muted:  #64748b;
  --accent:         #fb923c;
  --accent-soft:    rgba(251, 146, 60, 0.12);
  --accent-strong:  #c2410c;
  --info:           #38bdf8;
  --info-soft:      #0d2232;
  --ok:             #34d399;
  --ok-soft:        #0f2a20;
  --warn:           #fbbf24;
  --warn-soft:      #2b1f08;
  --danger:         #f87171;
  --danger-soft:    #2b1414;
  --border:         #222c42;
  --border-soft:    #1a2237;
  --shadow-sm:      0 1px 2px 0 rgba(0, 0, 0, 0.4);
  --shadow-md:      0 1px 3px 0 rgba(0, 0, 0, 0.3), 0 1px 2px -1px rgba(0, 0, 0, 0.25);
  --shadow-lg:      0 10px 15px -3px rgba(0, 0, 0, 0.5), 0 4px 6px -4px rgba(0, 0, 0, 0.4);
}

/* Font stack — ikuti Adminator template:
   Inter untuk body, Inter Tight untuk headings, JetBrains Mono untuk code/numerics.
   System fallback supaya tetep crispy kalau Google Fonts gagal load. */
:root {
  --font-sans:    'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-display: 'Inter Tight', 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --font-mono:    'JetBrains Mono', ui-monospace, "SF Mono", "Menlo", "Monaco", "Consolas", monospace;
}

* { box-sizing: border-box; }
body {
  font-family: var(--font-sans);
  margin: 0;
  background: var(--bg-page);
  color: var(--text-primary);
  line-height: 1.5;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
h1, h2, h3, h4 {
  font-family: var(--font-sans);
  font-weight: 700;
}
h1, h2, h3, h4, .topbar-title, .sidebar-brand {
  font-family: var(--font-display);
  font-weight: 700;
  letter-spacing: -0.01em;
}
code, pre, .mono, .phone, .stat strong {
  font-family: var(--font-mono);
}

/* ═══ App shell (sidebar + main) ═══ */
.app-shell {
  display: flex;
  min-height: 100vh;
}
.sidebar {
  width: var(--sidebar-width);
  background: var(--bg-sidebar);
  color: var(--text-on-dark);
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0; left: 0; bottom: 0;
  z-index: 10;
  transition: width 0.2s ease;
  border-right: 1px solid var(--border);
}
.sidebar-brand {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-soft);
  min-height: var(--topbar-height);
  overflow: hidden;
}
.brand-logo {
  height: 32px;
  width: auto;
  max-width: 100%;
  object-fit: contain;
  object-position: left center;
  transition: max-width 0.2s ease;
}
/* When sidebar collapsed: clip logo to show only the colored-square icon */
.app-shell.sidebar-collapsed .sidebar-brand {
  padding: 10px 8px;
  justify-content: center;
}
.app-shell.sidebar-collapsed .brand-logo {
  max-width: 36px;
  object-fit: cover;
  object-position: left center;
}
.sidebar-nav {
  display: flex;
  flex-direction: column;
  padding: 16px 10px;
  gap: 2px;
  flex: 1;
}
.sidebar-nav button {
  display: flex;
  align-items: center;
  gap: 12px;
  background: transparent;
  border: none;
  color: var(--text-on-dark);
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13.5px;
  font-weight: 500;
  font-family: inherit;
  text-align: left;
  transition: background 0.12s, color 0.12s, border-left-color 0.12s;
  border-left: 3px solid transparent;
}
.sidebar-nav button:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.sidebar-nav button.active {
  background: var(--accent-soft);
  color: var(--accent);
  border-left-color: var(--accent);
  font-weight: 600;
}
.sidebar-nav .nav-icon { font-size: 16px; width: 18px; text-align: center; }
.sidebar-footer {
  padding: 10px;
  border-top: 1px solid var(--border-soft);
}
.sidebar-footer button {
  display: flex;
  align-items: center;
  gap: 10px;
  background: transparent;
  border: none;
  color: var(--text-on-dark-muted);
  padding: 8px 14px;
  cursor: pointer;
  font-size: 13px;
  width: 100%;
  border-radius: var(--radius-sm);
  font-family: inherit;
}
.sidebar-footer button:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.app-main {
  flex: 1;
  margin-left: var(--sidebar-width);
  min-width: 0;
  transition: margin-left 0.2s ease;
}
.app-shell.sidebar-collapsed .sidebar { width: var(--sidebar-width-collapsed); }
.app-shell.sidebar-collapsed .app-main { margin-left: var(--sidebar-width-collapsed); }
.app-shell.sidebar-collapsed .brand-text,
.app-shell.sidebar-collapsed .nav-label { display: none; }
.app-shell.sidebar-collapsed .sidebar-nav button { justify-content: center; padding: 10px 8px; }
.app-shell.sidebar-collapsed .sidebar-footer button { justify-content: center; }

.topbar {
  height: var(--topbar-height);
  background: var(--bg-topbar);
  border-bottom: 1px solid var(--border);
  padding: 0 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 5;
}
.topbar-title {
  font-family: var(--font-sans);
  font-size: 17px;
  font-weight: 700;
  margin: 0;
  color: var(--text-primary);
}
.topbar-spacer { flex: 1; }
#sidebar-toggle {
  background: transparent;
  border: none;
  font-size: 18px;
  cursor: pointer;
  color: var(--text-secondary);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
}
#sidebar-toggle:hover { background: var(--bg-hover); }
#theme-icon { font-size: 15px; }
/* Topbar.meta + buttons (right side) */
.topbar .meta {
  font-size: 12px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
.topbar #date-picker,
.topbar #refresh-btn,
.topbar #qr-btn {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  cursor: pointer;
  font-family: inherit;
}
.topbar #date-picker { font-family: var(--font-mono); }
.topbar #refresh-btn:hover,
.topbar #qr-btn:hover { background: var(--bg-hover); border-color: var(--accent); color: var(--accent); }
.tab-panel { display: none; padding: 20px; }
.tab-panel.active { display: block; }

/* Search bar */
#search-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 8px;
}
#search-input {
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 13px;
  padding: 4px 0;
  outline: none;
  min-width: 200px;
  font-family: inherit;
}
#search-input::placeholder { color: var(--text-muted); }
#search-clear {
  background: transparent;
  color: var(--text-secondary);
  border: none;
  padding: 2px 4px;
  font-size: 14px;
  cursor: pointer;
  display: none;
  min-height: auto;
}
#search-wrap.has-query #search-clear { display: inline-block; }
#search-stat { font-size: 11px; color: #0e7c66; padding-right: 4px; }
mark.hit {
  background: rgba(252, 211, 77, 0.55);
  color: inherit;
  padding: 0 2px;
  border-radius: 2px;
  font-weight: 600;
}
mark.hit.mark-current {
  background: #d97706;
  color: var(--text-primary);
  outline: 2px solid #dc2626;
  outline-offset: 1px;
  animation: pulse-hit 0.7s ease-out;
}
@keyframes pulse-hit {
  0% { box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.5); }
  100% { box-shadow: 0 0 0 14px rgba(220, 38, 38, 0); }
}
.card.search-hidden { display: none !important; }

/* Search navigation buttons */
.search-nav {
  background: transparent;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 2px 4px;
  font-size: 13px;
  min-height: auto;
}
.search-nav:hover { color: var(--text-primary); }
.search-nav:disabled { opacity: 0.3; cursor: default; }

/* Members tab */
.members-toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.members-filter {
  display: flex;
  gap: 4px;
  background: var(--bg-soft);
  padding: 4px;
  border-radius: var(--radius);
  border: 1px solid var(--border);
}
.members-filter button {
  background: transparent;
  color: var(--text-secondary);
  border: none;
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.members-filter button:hover:not(.active) {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.members-filter button.active {
  background: var(--accent);
  color: #ffffff;
  font-weight: 600;
  box-shadow: var(--shadow-sm);
}
.members-search {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 6px 12px;
  border-radius: 4px;
  font-size: 13px;
  min-width: 200px;
}
.members-stat { font-size: 12px; color: var(--text-secondary); margin-left: auto; }

/* Adminator default table — clean, minimal border, hover highlight, uppercase header */
.members-table {
  width: 100%;
  border-collapse: collapse;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  font-size: 13px;
  box-shadow: var(--shadow-sm);
}
.members-table thead {
  background: var(--bg-soft);
}
.members-table th, .members-table td {
  padding: 12px 16px;
  text-align: left;
  border-bottom: 1px solid var(--border-soft);
}
.members-table th {
  font-size: 11px;
  text-transform: uppercase;
  color: var(--text-muted);
  font-weight: 600;
  letter-spacing: 0.6px;
  background: var(--bg-soft);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.members-table tbody tr { transition: background 0.1s; }
.members-table tbody tr:hover { background: var(--bg-hover); }
.members-table tbody tr:last-child td { border-bottom: none; }
.members-table .phone {
  font-family: var(--font-mono);
  color: var(--info);
  font-size: 12.5px;
  white-space: nowrap;
}
.members-table .name {
  color: var(--text-primary);
}
.members-table .name.user-labeled {
  color: var(--ok);
  font-weight: 600;
}
.members-table .name.unlabeled {
  color: var(--text-muted);
  font-style: italic;
}
.members-table .auto-name {
  color: var(--text-secondary);
  font-size: 12px;
}
.members-table .num {
  text-align: right;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.members-table .badge-labeled {
  background: #5a7a1a;
  color: var(--text-primary);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 700;
  margin-left: 6px;
}
.members-table .badge-roster {
  background: #2d5b8e;
  color: #f0f4fa;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
  margin-left: 6px;
  cursor: help;
}
.members-table tr.phantom-row { background: var(--warn-soft); }
.members-table tr.phantom-row:hover { background: var(--warn-soft); filter: brightness(0.97); }
.members-table tr.phantom-row .badge-roster { background: var(--warn); }
.members-table td.name-cell {
  cursor: pointer;
  position: relative;
  transition: background 0.15s;
}
.members-table td.name-cell:hover {
  background: var(--bg-soft);
}
.members-table td.name-cell::after {
  content: '✏️';
  opacity: 0;
  margin-left: 6px;
  font-size: 11px;
  transition: opacity 0.15s;
}
.members-table td.name-cell:hover::after { opacity: 0.4; }
.members-table .name-edit {
  width: 100%;
  background: var(--bg-panel);
  border: 1px solid #5a7a1a;
  color: var(--text-primary);
  padding: 4px 8px;
  font-size: 13px;
  border-radius: 3px;
  font-family: inherit;
  outline: none;
}
.members-table td.flash-success {
  background: rgba(90, 122, 26, 0.18);
  animation: flash-fade 1s ease-out;
}
.members-table td.flash-error {
  background: rgba(220, 38, 38, 0.18);
  animation: flash-fade 1s ease-out;
}
@keyframes flash-fade {
  0% { background-color: rgba(90, 122, 26, 0.35); }
  100% { background-color: transparent; }
}
.edit-hint {
  font-size: 11px;
  color: var(--text-muted);
  font-style: italic;
  margin-left: 8px;
}

/* QR modal */
#qr-modal { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; }
.qr-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.5); }
.qr-content {
  position: relative;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 24px;
  text-align: center;
  box-shadow: 0 10px 40px rgba(0,0,0,0.35);
  max-width: 360px;
}
.qr-content h3 { margin: 0 0 14px; color: var(--ok); font-size: 16px; }
.qr-content #qr-canvas { background: #fff; padding: 12px; border-radius: 4px; display: inline-block; }
.qr-url { font-family: "SF Mono", monospace; color: #0066cc; margin: 14px 0 4px; font-size: 13px; }
.qr-hint { color: var(--text-secondary); font-size: 12px; margin: 6px 0 14px; }
#qr-close { background: #b5c0cd; color: var(--text-primary); border: none; padding: 8px 18px; border-radius: 4px; cursor: pointer; }
#qr-close:hover { background: var(--bg-hover); }

/* Member detail modal */
#member-modal { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; padding: 20px; }
.member-modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.5); }
.member-modal-content {
  position: relative;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 22px 26px;
  width: 100%;
  max-width: 540px;
  max-height: 80vh;
  overflow-y: auto;
  box-shadow: 0 10px 40px rgba(0,0,0,0.35);
}
.mm-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid #b5c0cd;
}
.mm-head h3 { margin: 0; color: var(--ok); font-size: 16px; }
.mm-close { background: transparent; color: var(--text-secondary); border: none; font-size: 22px; cursor: pointer; padding: 0 4px; }
.mm-close:hover { color: var(--text-primary); }
.mm-section { margin-bottom: 14px; }
.mm-section:last-child { margin-bottom: 0; }
.mm-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #0e7c66;
  margin-bottom: 4px;
}
.mm-value { color: var(--text-primary); font-size: 14px; }
.mm-value code {
  font-family: "SF Mono", monospace;
  color: #0066cc;
  background: rgba(0, 102, 204, 0.08);
  padding: 2px 8px;
  border-radius: 3px;
}
.mm-name-edit {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 13px;
  width: 100%;
  font-family: inherit;
}
.mm-name-edit:focus { outline: none; border-color: var(--ok); }
.mm-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 4px;
  margin-left: 6px;
}
.mm-badge.labeled { background: #5a7a1a; color: var(--text-primary); }
.mm-badge.auto { background: #3a7ab8; color: #fff; }
.mm-badge.unlabeled { background: #8a9aab; color: #fff; }
.mm-groups { display: flex; flex-direction: column; gap: 6px; }
.mm-group-row {
  background: var(--bg-panel);
  border-left: 3px solid #3a7ab8;
  padding: 6px 10px;
  border-radius: 0 3px 3px 0;
  font-size: 12.5px;
  display: flex;
  justify-content: space-between;
  gap: 10px;
}
.mm-group-row code {
  font-family: "SF Mono", monospace;
  color: #0066cc;
  font-size: 11.5px;
}
.mm-group-row .gs { color: var(--text-secondary); font-style: italic; font-size: 11px; }
.mm-save-status {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
  min-height: 14px;
}
.mm-save-status.ok { color: var(--ok); }
.mm-save-status.err { color: #dc2626; }

.mm-notes-edit {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 13px;
  width: 100%;
  font-family: inherit;
  min-height: 70px;
  resize: vertical;
  line-height: 1.5;
}
.mm-notes-edit:focus { outline: none; border-color: var(--ok); }
.members-table .note-icon {
  font-size: 11px;
  margin-left: 6px;
  cursor: help;
  opacity: 0.7;
}
.members-table .note-icon:hover { opacity: 1; }

/* System Status row */
.status-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.status-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--ok);
  border-radius: var(--radius);
  padding: 12px 16px;
  box-shadow: var(--shadow-sm);
  transition: box-shadow 0.15s, transform 0.15s;
}
.status-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}
.status-card.fresh { border-left-color: var(--ok); }
.status-card.late { border-left-color: var(--warn); }
.status-card.overdue { border-left-color: var(--danger); }
.status-card.info { border-left-color: var(--info); }
.status-label {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--text-muted);
  margin-bottom: 4px;
}
.status-value {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.status-sub {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 2px;
}
.status-card.fresh .status-value { color: var(--ok); }
.status-card.late .status-value { color: var(--warn); }
.status-card.overdue .status-value { color: var(--danger); }
.status-card.info .status-value { color: var(--info); }
@media (max-width: 480px) {
  .status-row { grid-template-columns: repeat(2, 1fr); }
  .status-value { font-size: 13px; }
}

@media (max-width: 768px) {
  .members-table { font-size: 12px; }
  .members-table th, .members-table td { padding: 8px 10px; }
  .members-table .phone { font-size: 11px; }
  .members-toolbar { flex-direction: column; align-items: stretch; }
  .members-search { width: 100%; min-width: 0; }
  .members-stat { margin-left: 0; }
  /* On mobile, hide auto_name column to fit */
  .members-table th.col-auto, .members-table td.col-auto { display: none; }
  .members-table th.col-groups, .members-table td.col-groups { display: none; }
}

/* Pola tab cards */
.pola-grid { display: grid; gap: 14px; grid-template-columns: 1fr; }
.pola-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.pola-card-head {
  padding: 10px 14px;
  background: var(--bg-soft);
  border-bottom: 1px solid #b5c0cd;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  font-size: 13px;
}
.pola-card-head code {
  color: #0066cc;
  font-family: "SF Mono", monospace;
  font-size: 12px;
  background: rgba(0, 102, 204, 0.08);
  padding: 2px 8px;
  border-radius: 3px;
}
.pola-title { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.pola-name {
  font-size: 14px;
  font-weight: 600;
  color: #b8860b;
  letter-spacing: 0.2px;
  cursor: pointer;
  border-radius: 3px;
  padding: 1px 4px;
  margin-left: -4px;
  transition: background 0.12s;
}
.pola-name:hover { background: rgba(184, 134, 11, 0.12); }
.pola-name-unknown { color: var(--text-secondary); font-style: italic; font-weight: 500; }
.pola-name-unknown:hover { background: rgba(138, 154, 171, 0.15); color: var(--text-primary); }
.pola-name-badge { color: #0e7c66; font-size: 11px; margin-left: 6px; opacity: 0.75; }
.pola-name-input {
  font-size: 14px;
  font-weight: 600;
  color: #b8860b;
  background: var(--bg-panel);
  border: 1px solid #b8860b;
  border-radius: 3px;
  padding: 3px 8px;
  min-width: 280px;
  font-family: inherit;
  outline: none;
}
.pola-name-input::placeholder { color: var(--text-muted); font-weight: 400; font-style: italic; font-size: 12px; }
.pola-jid {
  font-size: 11px;
  color: #0066cc;
  font-family: "SF Mono", monospace;
  background: rgba(0, 102, 204, 0.08);
  padding: 1px 6px;
  border-radius: 3px;
  align-self: flex-start;
}
.pola-card-head .pola-meta {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  margin-left: 12px;
}
.pola-card-body {
  padding: 14px;
  display: none;
}
.pola-card.open .pola-card-body { display: block; }
.pola-card.open .pola-card-head::after { content: "▾"; color: var(--text-secondary); margin-left: 8px; }
.pola-card:not(.open) .pola-card-head::after { content: "▸"; color: var(--text-secondary); margin-left: 8px; }
.pola-section {
  margin-bottom: 14px;
  padding: 10px 12px;
  background: var(--bg-panel);
  border-left: 3px solid #0e7c66;
  border-radius: 0 4px 4px 0;
}
.pola-section:last-child { margin-bottom: 0; }
.pola-section-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #0e7c66;
  margin-bottom: 6px;
}
.pola-section-body {
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.55;
  white-space: pre-wrap;
}
.pola-section-body strong { color: var(--text-primary); }
@media (max-width: 768px) {
  #search-wrap { width: 100%; }
  #search-input { min-width: 0; flex: 1; }
}
select, button {
  background: #b5c0cd;
  color: var(--text-primary);
  border: 1px solid var(--border);
  padding: 6px 12px;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
}
button:hover { background: var(--bg-hover); }
main {
  padding: 20px 24px;
  max-width: 1600px;
  margin: 0 auto;
  background: var(--bg-page);
  color: var(--text-primary);
}
/* Adminator default card — white bg, subtle border + shadow, no fill section-head */
.section {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 20px;
  overflow: hidden;
  box-shadow: var(--shadow-md);
}
.section-head {
  padding: 16px 20px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-soft);
  font-weight: 600;
  font-size: 15px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--text-primary);
  font-family: var(--font-display);
  letter-spacing: -0.01em;
}
.section-head .meta { font-family: var(--font-sans); font-weight: 400; font-size: 12px; color: var(--text-muted); }
.section-body { padding: 18px 20px; }
.cards { display: grid; grid-template-columns: 1fr; gap: 12px; }
.card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  transition: box-shadow 0.15s;
  box-shadow: var(--shadow-sm);
}
.card:hover { box-shadow: var(--shadow-md); }
.card.collective { box-shadow: var(--shadow-md); }
.card-head {
  padding: 12px 16px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-soft);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
}
.card.resume .card-head,
.card.rekap .card-head { background: var(--bg-panel); }
.card-head .badge {
  font-size: 10.5px;
  padding: 3px 10px;
  border-radius: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.card.resume .badge { background: var(--info-soft); color: var(--info); border: 1px solid var(--info); }
.card.rekap .badge { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent); }
.card-body {
  padding: 14px 16px;
  font-family: var(--font-sans);
  font-size: 13.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 600px;
  overflow-y: auto;
  color: var(--text-primary);
  background: var(--bg-panel);
}
.toggle {
  cursor: pointer;
  background: none;
  border: none;
  color: var(--text-secondary);
  font-size: 18px;
  padding: 0 4px;
}
.collapsed .card-body { display: none; }

/* KONFIRMASI highlighting */
.line-confirmed { color: var(--ok); }
.line-pending { color: var(--warn); }
.line-tua { color: #dc2626; font-weight: 600; }
.line-section-konfirmasi { color: #0e7c66; font-weight: 600; }
.line-section-urgent { color: #dc2626; font-weight: 600; }
.line-header { color: var(--warn); font-weight: 600; }
.line-separator { color: #aab4c0; }
.line-jid { color: #0066cc; }
.line-action { color: var(--warn); }

.empty {
  text-align: center;
  color: var(--text-secondary);
  padding: 40px 20px;
  font-style: italic;
}
.stat-row {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.stat { background: var(--bg-panel); padding: 6px 12px; border-radius: 4px; }
.stat strong { color: var(--text-primary); font-size: 14px; }
.stat.alert strong { color: #dc2626; }
.stat.ok strong { color: var(--ok); }

/* Charts */
.charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.chart-card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 16px;
}
.chart-title {
  font-size: 12px;
  font-weight: 600;
  color: #0e7c66;
  margin-bottom: 12px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.chart-svg { width: 100%; height: auto; display: block; }
.chart-svg .axis { stroke: var(--border); stroke-width: 1; }
.chart-svg .axis-label { fill: var(--text-secondary); font-size: 10px; font-family: var(--font-mono); }
.chart-svg .bar-confirmed { fill: var(--ok); }
.chart-svg .bar-pending { fill: var(--warn); }
.chart-svg .bar-tua { fill: var(--danger); }
.chart-svg .bar-pic { fill: var(--info); }
.chart-svg .grid { stroke: var(--border-soft); stroke-width: 1; stroke-dasharray: 2,3; }
.chart-svg .value-label { fill: var(--text-primary); font-size: 10px; font-family: var(--font-mono); }
.chart-svg .bar-tua-text { fill: var(--danger); }
.chart-empty { color: var(--text-secondary); font-size: 12px; padding: 20px; text-align: center; font-style: italic; }

/* Parsed rekap/resume rendering */
.parsed-body { padding: 14px; }
.parsed-section { margin-bottom: 18px; }
.parsed-section:last-child { margin-bottom: 0; }
.parsed-section-title {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: #0e7c66;
  border-bottom: 1px solid #b5c0cd;
  padding-bottom: 6px;
  margin-bottom: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.section-count {
  background: #b5c0cd;
  color: var(--text-secondary);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  letter-spacing: 0;
  text-transform: none;
  font-weight: 500;
}

.group-block {
  background: var(--bg-panel);
  border-left: 3px solid #3a7ab8;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-radius: 0 4px 4px 0;
}
.group-header {
  font-size: 12px;
  margin-bottom: 6px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.group-header code {
  color: #0066cc;
  font-size: 11.5px;
  font-family: "SF Mono", monospace;
  background: rgba(0, 102, 204, 0.08);
  padding: 1px 6px;
  border-radius: 3px;
}
.group-label {
  color: var(--text-secondary);
  font-size: 11px;
  font-style: italic;
}

/* === Briefing tab === */
.briefing-toolbar {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; margin-bottom: 12px;
  background: var(--bg-soft); border: 1px solid var(--border); border-radius: 6px;
}
.briefing-label { font-weight: 600; color: var(--info); font-size: 13px; }
#briefing-file-picker {
  padding: 5px 10px; font-size: 13px; font-family: ui-monospace, monospace;
  border: 1px solid var(--border); border-radius: 4px; background: #fff;
}
.briefing-meta { color: var(--text-secondary); font-size: 12px; margin-left: auto; font-style: italic; }
.briefing-card {
  background: #fff; border: 1px solid var(--border); border-radius: 6px; padding: 14px;
}
/* Briefing section tabs — Adminator-style card-grid dengan colored letter chips
   Layout: 4 cols × 2 rows untuk 8 sections (consistent, no scroll). Mobile: 2 cols. */
.briefing-section-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 20px;
  padding: 0;
  border-bottom: none;
}
@media (max-width: 900px) {
  .briefing-section-tabs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 480px) {
  .briefing-section-tabs { grid-template-columns: 1fr; }
}
.briefing-section-tabs .section-tab {
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  padding: 10px 12px;
  cursor: pointer;
  font-family: inherit;
  border-radius: var(--radius);
  text-align: left;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
  min-width: 0;  /* allow flex child to shrink below content width */
  overflow: hidden;
}
.briefing-section-tabs .section-tab:hover {
  border-color: var(--text-muted);
  box-shadow: var(--shadow-sm);
}
.tab-letter {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 13px;
  flex-shrink: 0;
  letter-spacing: -0.02em;
  transition: background 0.15s, color 0.15s;
}
/* Color variants — soft bg + colored text (inactive state) */
.tab-accent .tab-letter { background: var(--accent-soft); color: var(--accent); }
.tab-info   .tab-letter { background: var(--info-soft);   color: var(--info); }
.tab-ok     .tab-letter { background: var(--ok-soft);     color: var(--ok); }
.tab-warn   .tab-letter { background: var(--warn-soft);   color: var(--warn); }
.tab-danger .tab-letter { background: var(--danger-soft); color: var(--danger); }
.briefing-tab-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.25;
  letter-spacing: -0.01em;
  /* Allow 2 lines max; ellipsis kalau lebih */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
  flex: 1;
  min-width: 0;
}
/* Active state — letter chip filled, card border + bg accent */
.briefing-section-tabs .section-tab.active {
  border-color: currentColor;
  box-shadow: var(--shadow-md);
}
.tab-accent.active { background: var(--accent-soft); }
.tab-info.active   { background: var(--info-soft); }
.tab-ok.active     { background: var(--ok-soft); }
.tab-warn.active   { background: var(--warn-soft); }
.tab-danger.active { background: var(--danger-soft); }
.tab-accent.active .tab-letter { background: var(--accent); color: #fff; }
.tab-info.active   .tab-letter { background: var(--info);   color: #fff; }
.tab-ok.active     .tab-letter { background: var(--ok);     color: #fff; }
.tab-warn.active   .tab-letter { background: var(--warn);   color: #fff; }
.tab-danger.active .tab-letter { background: var(--danger); color: #fff; }
.tab-accent.active { border-color: var(--accent); }
.tab-info.active   { border-color: var(--info); }
.tab-ok.active     { border-color: var(--ok); }
.tab-warn.active   { border-color: var(--warn); }
.tab-danger.active { border-color: var(--danger); }
.tab-accent.active .briefing-tab-title { color: var(--accent); }
.tab-info.active   .briefing-tab-title { color: var(--info); }
.tab-ok.active     .briefing-tab-title { color: var(--ok); }
.tab-warn.active   .briefing-tab-title { color: var(--warn); }
.tab-danger.active .briefing-tab-title { color: var(--danger); }
.briefing-section-title {
  font-size: 16px; font-weight: 700; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.4px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border-soft); margin-bottom: 12px;
}
.briefing-section-body { font-size: 13.5px; line-height: 1.65; color: var(--text-primary); }
.briefing-p { margin: 6px 0 10px 0; }
.briefing-h3, .briefing-h4 {
  color: var(--info); margin: 16px 0 6px 0; font-weight: 700;
}
.briefing-h3 { font-size: 14.5px; }
.briefing-h4 { font-size: 13.5px; color: var(--text-secondary); }
.briefing-bullets, .briefing-ol {
  margin: 6px 0 10px 0; padding-left: 22px;
}
.briefing-bullets li, .briefing-ol li { margin-bottom: 4px; }
/* Briefing table — match members-table Adminator pattern */
.briefing-table {
  border-collapse: collapse; width: 100%; margin: 10px 0 14px 0;
  font-size: 13px; background: var(--bg-panel);
  border: 1px solid var(--border); border-radius: var(--radius);
  overflow: hidden;
}
.briefing-table th, .briefing-table td {
  padding: 10px 14px; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--border-soft);
}
.briefing-table tbody tr:last-child td { border-bottom: none; }
.briefing-table th {
  background: var(--bg-soft); font-weight: 600; color: var(--text-muted);
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
.briefing-table tbody tr { transition: background 0.1s; }
.briefing-table tbody tr:hover { background: var(--bg-hover); }
.briefing-section code {
  background: var(--bg-soft); padding: 1px 5px; border-radius: 3px;
  font-size: 12px; color: var(--accent);
}

/* === Briefing card di Overview === */
.briefing-overview-card .section-head {
  color: var(--accent); border-left: 3px solid #c2410c;
  padding-left: 10px;
}
.briefing-overview-meta {
  color: var(--text-secondary); font-size: 12px; margin-bottom: 10px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border-soft);
}
.briefing-overview-excerpt {
  background: var(--bg-soft); border: 1px solid var(--border-soft); border-radius: 4px;
  padding: 12px 14px; margin: 10px 0;
  font-size: 13px; line-height: 1.6; color: var(--text-primary);
}
.briefing-excerpt-label {
  font-size: 11px; font-weight: 700; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 6px;
}
.briefing-open-btn {
  background: #c2410c; color: #fff; border: none;
  padding: 8px 16px; border-radius: 4px; cursor: pointer;
  font-size: 13px; font-weight: 600; font-family: inherit;
  transition: background 0.15s;
}
.briefing-open-btn:hover { background: #9a3208; }
.group-bullets {
  margin: 4px 0 4px 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.55;
}
.group-bullets li { margin-bottom: 3px; }
.bullet-sub {
  margin: 2px 0 2px 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-size: 12.5px;
  list-style-type: '— ';
}
.bullet-sub .bullet-sub {
  font-size: 12px;
  color: var(--text-muted);
  list-style-type: '· ';
}
.action-row {
  background: rgba(217, 119, 6, 0.08);
  border-left: 2px solid #d97706;
  padding: 5px 10px;
  margin: 6px 0 0 0;
  font-size: 12.5px;
  border-radius: 0 3px 3px 0;
}
.action-pic {
  color: var(--warn);
  font-weight: 600;
}

.konf-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 900px) { .konf-grid { grid-template-columns: 1fr; } }

.hod-summary {
  font-size: 12px;
  color: #0e7c66;
  margin-bottom: 10px;
  padding: 6px 10px;
  background: rgba(14, 124, 102, 0.08);
  border-radius: 4px;
  display: inline-block;
}
.hod-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}
@media (max-width: 900px) { .hod-grid { grid-template-columns: 1fr; } }
.hod-card {
  background: var(--bg-panel);
  border-left: 3px solid #b8860b;
  border-radius: 4px;
  padding: 10px 14px;
}
.hod-card.hod-empty-card {
  border-left-color: #b5c0cd;
  opacity: 0.55;
}
.hod-label {
  font-size: 12px;
  font-weight: 700;
  color: #b8860b;
  letter-spacing: 0.3px;
  margin-bottom: 8px;
  text-transform: uppercase;
}
.hod-empty-card .hod-label { color: var(--text-muted); }
.hod-count {
  color: #0e7c66;
  font-weight: 600;
  font-size: 11px;
}
.hod-empty {
  color: var(--text-muted);
  font-style: italic;
  font-size: 12px;
  padding: 4px 0;
}
.hod-card .resume-bullets { margin: 0; padding-left: 18px; }
.hod-card .resume-bullets li { margin-bottom: 4px; font-size: 13px; }

.konf-item {
  background: var(--bg-panel);
  border-left: 3px solid #5a7a1a;
  padding: 8px 12px;
  margin-bottom: 6px;
  border-radius: 0 4px 4px 0;
}
.konf-item.pending { border-left-color: var(--warn); }
.konf-item.tua { border-left-color: #dc2626; background: rgba(220, 38, 38, 0.08); }
.konf-topic {
  font-size: 13px;
  color: var(--text-primary);
  margin-bottom: 4px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.badge-tua {
  background: #dc2626;
  color: var(--text-primary);
  font-size: 9px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 3px;
  letter-spacing: 0.5px;
}
.konf-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  font-size: 11.5px;
}
.chip {
  background: var(--bg-soft);
  color: var(--text-primary);
  padding: 2px 8px;
  border-radius: 10px;
  font-family: "SF Mono", monospace;
  font-size: 11px;
}
.chip-key {
  color: var(--text-secondary);
  margin-right: 4px;
  text-transform: lowercase;
}

.resume-section { margin-bottom: 14px; }
.resume-section-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--warn);
  margin-bottom: 6px;
}
.resume-paragraph {
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.6;
  padding-left: 4px;
}
.resume-bullets {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.6;
}
.resume-bullets li { margin-bottom: 4px; }
.resume-bullets strong { color: var(--warn); }

/* Inner tabs for resume sections */
.section-tabs {
  display: flex;
  gap: 4px;
  overflow-x: auto;
  margin-bottom: 14px;
  padding-bottom: 4px;
  border-bottom: 1px solid #b5c0cd;
  scrollbar-width: thin;
}
.section-tabs::-webkit-scrollbar { height: 4px; }
.section-tabs::-webkit-scrollbar-thumb { background: var(--bg-hover); border-radius: 2px; }
.section-tab {
  background: transparent;
  border: 1px solid transparent;
  color: var(--text-secondary);
  padding: 7px 12px;
  border-radius: 6px 6px 0 0;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  position: relative;
  bottom: -1px;
  transition: background 0.15s, color 0.15s;
}
.section-tab:hover { background: #c4cdd9; color: var(--text-primary); }
.section-tab.active {
  background: var(--bg-panel);
  color: var(--warn);
  border-color: var(--border);
  border-bottom-color: #ffffff;
  font-weight: 600;
}
.tab-count {
  background: #b5c0cd;
  color: var(--text-secondary);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  font-weight: 600;
  min-width: 16px;
  text-align: center;
}
.section-tab.active .tab-count { background: var(--bg-hover); color: var(--warn); }
.tab-count.alert { background: #dc2626; color: var(--text-primary); }
.section-panel { animation: fadein 0.15s ease-out; }
@keyframes fadein { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }
.konf-col-title { font-size: 12px; margin-bottom: 6px; font-weight: 600; }
.konf-col-title.ok { color: var(--ok); }
.konf-col-title.pending { color: var(--warn); }

/* Actions aggregated panel — grouped by PIC */
.action-pic-block {
  background: var(--bg-panel);
  border-left: 3px solid #d97706;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-radius: 0 4px 4px 0;
}
.action-pic-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--warn);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.action-row-flat {
  font-size: 12.5px;
  color: var(--text-primary);
  padding: 5px 0;
  border-top: 1px solid #d4dbe5;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.action-source {
  font-size: 11px;
  opacity: 0.55;
}
.action-source code {
  font-family: "SF Mono", monospace;
  background: rgba(0, 102, 204, 0.06);
  padding: 1px 5px;
  border-radius: 3px;
  color: #0066cc;
}

@media (max-width: 768px) {
  .section-tabs { gap: 2px; }
  .section-tab { padding: 8px 10px; font-size: 11.5px; min-height: 36px; }
  .tab-count { font-size: 9.5px; padding: 1px 5px; }
}

.footer-row {
  padding: 10px 16px;
  background: var(--bg-soft);
  border-top: 1px solid var(--border);
  font-family: var(--font-sans);
  font-size: 12.5px;
  color: var(--text-secondary);
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.footer-row .urgent-alert { color: var(--danger); font-weight: 600; }

.view-toggle {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 3px;
  cursor: pointer;
  margin-left: 8px;
}
.view-toggle.active { background: var(--bg-hover); color: var(--text-primary); }

/* ── Mobile: auto-collapse sidebar (≤768px) ───────────────── */
@media (max-width: 768px) {
  body { font-size: 14px; }
  .sidebar {
    width: var(--sidebar-width-collapsed);
  }
  .sidebar .brand-text,
  .sidebar .nav-label { display: none; }
  .sidebar-nav button { justify-content: center; padding: 10px 8px; }
  .sidebar-footer button { justify-content: center; }
  .app-main { margin-left: var(--sidebar-width-collapsed); }
  .topbar { padding: 0 12px; gap: 8px; flex-wrap: wrap; height: auto; min-height: var(--topbar-height); padding-top: 8px; padding-bottom: 8px; }
  .topbar-title { font-size: 15px; }
  .topbar select, .topbar button { min-height: 34px; padding: 6px 10px; font-size: 12.5px; }
  #last-update, #auto-status { font-size: 11px; }
  .tab-panel { padding: 12px 14px; }
  main { padding: 12px 14px; }
  .stat-row { gap: 8px; }
  .stat { padding: 5px 10px; font-size: 11px; flex: 1 1 auto; }
  .charts { grid-template-columns: 1fr; gap: 12px; }
  .konf-grid { grid-template-columns: 1fr; }
  .card-body { padding: 10px 12px; font-size: 12px; max-height: 70vh; }
  .card-head { padding: 8px 12px; font-size: 12px; }
  .group-block { padding: 8px 10px; }
  .group-bullets { padding-left: 16px; font-size: 12.5px; }
  .konf-item { padding: 7px 10px; }
  .konf-topic { font-size: 12.5px; }
  .chip { font-size: 10.5px; padding: 2px 6px; }
  .parsed-section { margin-bottom: 14px; }
  .chart-card { padding: 10px 12px; }
  .chart-title { font-size: 10.5px; }
  .section-head { padding: 10px 12px; font-size: 13px; flex-wrap: wrap; gap: 8px; }
  .section-body { padding: 12px; }
}

/* ── Compact mobile (≤480px) ──────────────────────────────── */
@media (max-width: 480px) {
  main { padding: 10px; }
  .stat { font-size: 10.5px; padding: 4px 8px; }
  .stat strong { font-size: 12px; }
  .card-body { font-size: 11.5px; }
  .group-header code { font-size: 10.5px; }
  .group-label { font-size: 10px; }
  .chip { font-size: 10px; }
  /* Hide the auto-refresh indicator on tiny screens to save header space */
  #auto-status { display: none; }
  /* Make the date/refresh row wrap below tabs */
  header > select, header > button:not([data-tab]) { flex: 1 1 calc(50% - 8px); }
}
</style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-brand">
      <img src="/assets/logo-wahana-lifeline.png" alt="Wahana LifeLine" class="brand-logo">
    </div>
    <nav class="sidebar-nav tabs">
      <button data-tab="overview" class="active"><span class="nav-icon">📊</span><span class="nav-label">Overview</span></button>
      <button data-tab="rekap"><span class="nav-icon">📝</span><span class="nav-label">Rekap</span></button>
      <button data-tab="resume"><span class="nav-icon">📑</span><span class="nav-label">Resume</span></button>
      <button data-tab="members"><span class="nav-icon">👥</span><span class="nav-label">Members</span></button>
      <button data-tab="pola"><span class="nav-icon">🧬</span><span class="nav-label">Pola</span></button>
      <button data-tab="briefing"><span class="nav-icon">📋</span><span class="nav-label">Briefing</span></button>
    </nav>
    <div class="sidebar-footer">
      <button id="theme-toggle" title="Toggle light/dark"><span id="theme-icon">🌙</span> <span class="nav-label">Theme</span></button>
    </div>
  </aside>
  <div class="app-main">
    <header class="topbar">
      <button id="sidebar-toggle" title="Toggle sidebar">☰</button>
      <h1 class="topbar-title" id="topbar-title">Overview</h1>
      <div class="topbar-spacer"></div>
      <div id="search-wrap" style="display:none">
        <input id="search-input" type="search" placeholder="Cari di rekap/resume…" autocomplete="off">
        <button class="search-nav" id="search-prev" title="Match sebelumnya (Shift+Enter)" disabled>‹</button>
        <button class="search-nav" id="search-next" title="Match berikutnya (Enter)" disabled>›</button>
        <button id="search-clear" title="Reset (Esc)">✕</button>
        <span class="meta" id="search-stat"></span>
      </div>
      <select id="date-picker"></select>
      <button id="refresh-btn" title="Refresh sekarang">⟳ Refresh</button>
      <button id="qr-btn" title="Scan QR untuk akses dari HP">📱</button>
      <span class="meta" id="last-update">—</span>
      <span class="meta" id="auto-status">auto-refresh: 60s</span>
    </header>
    <main>
      <div id="qr-modal" style="display:none">
        <div class="qr-backdrop"></div>
        <div class="qr-content">
          <h3>Akses dari HP</h3>
          <div id="qr-canvas"></div>
          <p class="qr-url"></p>
          <p class="qr-hint">Scan dengan kamera HP / WhatsApp scanner. HP harus di WiFi yang sama.</p>
          <button id="qr-close">Close</button>
        </div>
      </div>
      <div id="member-modal" style="display:none">
        <div class="member-modal-backdrop"></div>
        <div class="member-modal-content"></div>
      </div>
      <div id="panel-overview" class="tab-panel active"><div class="empty">Loading…</div></div>
      <div id="panel-rekap" class="tab-panel"><div class="empty">Loading…</div></div>
      <div id="panel-resume" class="tab-panel"><div class="empty">Loading…</div></div>
      <div id="panel-members" class="tab-panel"><div class="empty">Loading…</div></div>
      <div id="panel-pola" class="tab-panel"><div class="empty">Loading…</div></div>
      <div id="panel-briefing" class="tab-panel"><div class="empty">Loading…</div></div>
    </main>
  </div>
</div>

<script>
let currentDate = null;
let refreshTimer = null;
let lanIp = '';

function showQrModal() {
  const port = location.port || '80';
  const host = (lanIp && lanIp !== '127.0.0.1') ? lanIp : location.hostname;
  const url = location.protocol + '//' + host + ':' + port + '/';
  const modal = document.getElementById('qr-modal');
  const canvas = document.getElementById('qr-canvas');
  const urlEl = modal.querySelector('.qr-url');
  // Use qrserver.com API for QR generation (offline fallback = text URL only)
  const qrSrc = 'https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=' + encodeURIComponent(url);
  canvas.innerHTML = '<img src="' + qrSrc + '" alt="QR" width="240" height="240" onerror="this.style.display=\'none\'">';
  urlEl.textContent = url;
  modal.style.display = 'flex';
}

function hideQrModal() {
  const modal = document.getElementById('qr-modal');
  if (modal) modal.style.display = 'none';
}

function showMemberDetail(phone) {
  const member = membersData?.members?.find(m => m.phone === phone);
  if (!member) return;
  const modal = document.getElementById('member-modal');
  const content = modal.querySelector('.member-modal-content');
  // Resolve group subjects: members.json has `group_subjects` (array, possibly with empties).
  // Pair JIDs with subjects by position; show subject if non-empty.
  const groups = member.groups || [];
  const subjects = member.group_subjects || [];
  // group_subjects might be deduped from across all entries; not 1:1 with groups.
  // Just list groups, and append subjects as separate aside if any
  const groupsHtml = groups.length === 0
    ? '<div style="color:#4a5568;font-style:italic">(tidak ada grup terdaftar)</div>'
    : '<div class="mm-groups">' +
        groups.map(g => '<div class="mm-group-row"><code>' + escapeHtml(g) + '</code></div>').join('') +
      '</div>' +
      (subjects.length > 0
        ? '<div class="mm-value" style="font-size:12px;color:#4a5568;margin-top:8px">Known subjects: ' +
            subjects.map(s => '<em>' + escapeHtml(s) + '</em>').join(', ') + '</div>'
        : '');

  // Detect badge
  let badge;
  if (member.user_labeled) badge = '<span class="mm-badge labeled">USER-LABELED</span>';
  else if (member.auto_name) badge = '<span class="mm-badge auto">AUTO</span>';
  else badge = '<span class="mm-badge unlabeled">UNLABELED</span>';

  content.innerHTML =
    '<div class="mm-head">' +
      '<h3>Member Detail</h3>' +
      '<button class="mm-close" title="Close (Esc)">✕</button>' +
    '</div>' +
    '<div class="mm-section">' +
      '<div class="mm-label">Phone</div>' +
      '<div class="mm-value"><code>' + escapeHtml(member.phone) + '</code>' + badge + '</div>' +
    '</div>' +
    '<div class="mm-section">' +
      '<div class="mm-label">Name (klik untuk edit)</div>' +
      '<input type="text" class="mm-name-edit" value="' + (member.name || '').replace(/"/g, '&quot;') + '" placeholder="(unlabeled — kosongin untuk reset)">' +
      '<div class="mm-save-status"></div>' +
    '</div>' +
    (member.auto_name
      ? '<div class="mm-section">' +
          '<div class="mm-label">Auto-detected</div>' +
          '<div class="mm-value">' + escapeHtml(member.auto_name) + '</div>' +
        '</div>'
      : '') +
    (member.roster_source
      ? '<div class="mm-section">' +
          '<div class="mm-label">Roster (' + ((member.appearance_count||0)===0 ? '👻 phantom — belum kedetect di group' : '📋 cocok dengan roster resmi') + ')</div>' +
          '<div class="mm-value">' +
            'Panggilan: <b>' + escapeHtml(member.panggilan || '—') + '</b> · ' +
            'Posisi: <b>' + escapeHtml(member.posisi || '—') + '</b> · ' +
            'Cabang: <b>' + escapeHtml(member.cabang || '—') + '</b>' +
          '</div>' +
        '</div>'
      : '') +
    '<div class="mm-section">' +
      '<div class="mm-label">Notes (role, dept, alias, dll · auto-save)</div>' +
      '<textarea class="mm-notes-edit" placeholder="catatan bebas — mis. Sales Jember, vendor reagen, dll">' +
        escapeHtml(member.notes || '') +
      '</textarea>' +
      '<div class="mm-notes-status mm-save-status"></div>' +
    '</div>' +
    '<div class="mm-section">' +
      '<div class="mm-label">Appearance Count</div>' +
      '<div class="mm-value">' + (member.appearance_count || 0) + ' runtime-context entries</div>' +
    '</div>' +
    '<div class="mm-section">' +
      '<div class="mm-label">Groups (' + groups.length + ')</div>' +
      groupsHtml +
    '</div>';

  modal.style.display = 'flex';

  // Hook handlers
  const closeBtn = content.querySelector('.mm-close');
  if (closeBtn) closeBtn.addEventListener('click', hideMemberModal);
  modal.querySelector('.member-modal-backdrop').addEventListener('click', hideMemberModal);
  // Name input — save on blur or Enter
  const input = content.querySelector('.mm-name-edit');
  const status = content.querySelector('.mm-save-status');
  let saveTimer = null;
  const save = async () => {
    const newName = input.value.trim();
    const old = member.name || '';
    if (newName === old) {
      status.textContent = ''; status.className = 'mm-save-status'; return;
    }
    status.textContent = 'saving…'; status.className = 'mm-save-status';
    const r = await saveMemberName(phone, newName);
    if (r?.ok) {
      member.name = newName;
      member.user_labeled = Boolean(newName) && newName !== (member.auto_name || '');
      status.textContent = '✓ saved'; status.className = 'mm-save-status ok';
      // Refresh members panel in background
      renderMembersPanel();
      setTimeout(() => { status.textContent = ''; status.className = 'mm-save-status'; }, 1500);
    } else {
      status.textContent = '✗ ' + (r?.error || 'failed'); status.className = 'mm-save-status err';
    }
  };
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); save(); }
    else if (e.key === 'Escape') { hideMemberModal(); }
  });
  input.addEventListener('blur', () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 100);
  });
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);

  // Notes textarea — auto-save on blur
  const notesEl = content.querySelector('.mm-notes-edit');
  const notesStatus = content.querySelector('.mm-notes-status');
  if (notesEl) {
    let notesTimer = null;
    const saveNotes = async () => {
      const newNotes = notesEl.value;
      const old = member.notes || '';
      if (newNotes === old) { notesStatus.textContent = ''; notesStatus.className = 'mm-save-status mm-notes-status'; return; }
      notesStatus.textContent = 'saving…'; notesStatus.className = 'mm-save-status mm-notes-status';
      const r = await saveMemberField(phone, { notes: newNotes });
      if (r?.ok) {
        member.notes = newNotes;
        notesStatus.textContent = '✓ saved'; notesStatus.className = 'mm-save-status ok mm-notes-status';
        renderMembersPanel(); // update note icon in table
        setTimeout(() => { notesStatus.textContent = ''; notesStatus.className = 'mm-save-status mm-notes-status'; }, 1500);
      } else {
        notesStatus.textContent = '✗ ' + (r?.error || 'failed'); notesStatus.className = 'mm-save-status err mm-notes-status';
      }
    };
    notesEl.addEventListener('blur', () => {
      clearTimeout(notesTimer);
      notesTimer = setTimeout(saveNotes, 100);
    });
    notesEl.addEventListener('keydown', e => {
      if (e.key === 'Escape') { hideMemberModal(); }
      // Ctrl/Cmd+Enter to save
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); saveNotes(); }
    });
  }
}

function hideMemberModal() {
  const modal = document.getElementById('member-modal');
  if (modal) modal.style.display = 'none';
}

async function fetchData(date) {
  const r = await fetch('/api/data?date=' + encodeURIComponent(date));
  return await r.json();
}

function highlightLine(line) {
  // Order matters — more specific first.
  if (/\[TUA\]/.test(line)) return ['line-tua', line];
  if (/^✓ SUDAH DIKONFIRMASI|^✓ TERKONFIRMASI/.test(line)) return ['line-section-konfirmasi', line];
  if (/^⏳ MENUNGGU KONFIRMASI|^⏳ OUTSTANDING/.test(line)) return ['line-pending', line];
  if (/^URGENT:/.test(line)) return ['line-section-urgent', line];
  if (/^=+$/.test(line)) return ['line-separator', line];
  if (/^REKAP WRG|^RESUME EKSEKUTIF WRG|^\d+\.\s/.test(line)) return ['line-header', line];
  if (/^[\w-]+@g\.us$/.test(line)) return ['line-jid', line];
  if (/^→ ACTION:/.test(line)) return ['line-action', line];
  if (/^•\s.*\bconfirm by\b/.test(line)) return ['line-confirmed', line];
  if (/^•\s.*\bsejak\b/.test(line)) return ['line-pending', line];
  return [null, line];
}

function renderContent(content) {
  const lines = content.split('\n');
  let out = '';
  for (const line of lines) {
    const [cls, txt] = highlightLine(line);
    const escaped = txt.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    out += cls ? '<span class="' + cls + '">' + escaped + '</span>\n' : (escaped + '\n');
  }
  return out;
}

// Build nested <ul><li> HTML from flat [{text, level}] bullets (legacy strings
// also accepted — coerced to level 0). renderText callback can return marked-up
// HTML for the text portion (used by resume for **bold** processing).
function renderBulletsNested(items, options) {
  if (!items || !items.length) return '';
  options = options || {};
  const topClass = options.className || 'group-bullets';
  const renderText = options.renderText || (t => escapeHtml(t));
  const norm = items.map(it => (typeof it === 'string') ? {text: it, level: 0} : it);
  // Build tree using level-aware stack
  const root = { children: [] };
  const stack = [{ node: root, level: -1 }];
  for (const it of norm) {
    while (stack[stack.length - 1].level >= it.level) stack.pop();
    const node = { text: it.text, children: [] };
    stack[stack.length - 1].node.children.push(node);
    stack.push({ node: node, level: it.level });
  }
  function ren(nodes, isTop) {
    return '<ul class="' + (isTop ? topClass : 'bullet-sub') + '">' +
      nodes.map(n => '<li>' + renderText(n.text) +
        (n.children.length ? ren(n.children, false) : '') + '</li>').join('') +
    '</ul>';
  }
  return ren(root.children, true);
}

// Format millisecond epoch timestamps (13-digit integer string) to human readable.
// Returns "DD/MM HH:MM · Xh ago" so user sees absolute + relative at a glance.
function formatMsTimestamp(ms) {
  const n = (typeof ms === 'number') ? ms : parseInt(ms, 10);
  if (!Number.isFinite(n) || n < 1e12) return String(ms);
  const d = new Date(n);
  if (isNaN(d.getTime())) return String(ms);
  const abs = d.toLocaleString('id-ID', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  const ageS = Math.max(0, Math.floor((Date.now() - n) / 1000));
  let rel;
  if (ageS < 60) rel = ageS + 's ago';
  else if (ageS < 3600) rel = Math.floor(ageS / 60) + 'm ago';
  else if (ageS < 86400) rel = Math.floor(ageS / 3600) + 'h ago';
  else rel = Math.floor(ageS / 86400) + 'd ago';
  return abs + ' · ' + rel;
}

function renderKonfList(items, type /* 'confirmed' | 'pending' | 'outstanding' */) {
  if (!items.length) return '<div class="empty" style="padding:8px;text-align:left">Tidak ada</div>';
  const cls = type === 'confirmed' ? '' : 'pending';
  return items.map(it => {
    const fieldsHtml = Object.entries(it.fields || {}).map(([k, v]) => {
      // Detect ms-epoch values (13-digit integer) and format human-readable
      const displayVal = /^\d{13}$/.test(String(v)) ? formatMsTimestamp(v) : v;
      return '<span class="chip"><span class="chip-key">' + escapeHtml(k.replace(/_/g, ' ')) + '</span>' + escapeHtml(displayVal) + '</span>';
    }).join('');
    return '<div class="konf-item ' + cls + (it.tua ? ' tua' : '') + '">' +
      '<div class="konf-topic">' + escapeHtml(it.topic) + (it.tua ? ' <span class="badge-tua">TUA</span>' : '') + '</div>' +
      (fieldsHtml ? '<div class="konf-fields">' + fieldsHtml + '</div>' : '') +
    '</div>';
  }).join('');
}

function renderActionsAggregated(allActions) {
  if (!allActions.length) {
    return '<div class="empty" style="padding:12px;text-align:left">Tidak ada action items</div>';
  }
  // Group by PIC
  const byPic = {};
  allActions.forEach(a => {
    const pic = a.pic || '(unassigned)';
    if (!byPic[pic]) byPic[pic] = [];
    byPic[pic].push(a);
  });
  // Sort PICs by action count (most first)
  const sorted = Object.entries(byPic).sort((a, b) => b[1].length - a[1].length);
  return sorted.map(([pic, actions]) =>
    '<div class="action-pic-block">' +
      '<div class="action-pic-name">' + escapeHtml(pic) +
        ' <span class="tab-count">' + actions.length + '</span>' +
      '</div>' +
      actions.map(a =>
        '<div class="action-row-flat">' + escapeHtml(a.task) +
          (a.group ? ' <span class="action-source"><code>' + escapeHtml(a.group) + '</code></span>' : '') +
        '</div>'
      ).join('') +
    '</div>'
  ).join('');
}

function renderRekapParsed(parsed, cardId) {
  cardId = cardId || 'rekap-anon';
  const groupsCount = parsed.groups.length;
  // Aggregate all actions across groups
  const allActions = [];
  parsed.groups.forEach(g => {
    (g.actions || []).forEach(a => allActions.push({...a, group: g.jid}));
  });
  const konf = parsed.konfirmasi || {confirmed: [], pending: []};
  const tuaCount = (konf.pending || []).filter(p => p.tua).length;

  const tabs = [
    {id: 'grup', label: 'Grup', count: groupsCount, alert: false},
    {id: 'actions', label: 'Actions', count: allActions.length, alert: false},
    {id: 'confirmed', label: '✓ Confirmed', count: konf.confirmed.length, alert: false},
    {id: 'pending', label: '⏳ Pending', count: konf.pending.length, alert: tuaCount > 0},
  ];

  const tabsHtml = tabs.map((t, i) =>
    '<button class="section-tab' + (i === 0 ? ' active' : '') + '"' +
      ' data-card="' + cardId + '" data-section="' + t.id + '">' +
      t.label + (t.count > 0 ? ' <span class="tab-count' + (t.alert ? ' alert' : '') + '">' + t.count + '</span>' : '') +
    '</button>'
  ).join('');

  // Panel: Grup
  const groupsHtml = parsed.groups.map(g => {
    const bulletsHtml = renderBulletsNested(g.bullets, {className: 'group-bullets'});
    const actions = (g.actions || []).map(a =>
      '<div class="action-row"><span class="action-pic">' + escapeHtml(a.pic) + '</span> — ' + escapeHtml(a.task) + '</div>'
    ).join('');
    return '<div class="group-block">' +
      '<div class="group-header"><code>' + escapeHtml(g.jid) + '</code>' +
      (g.label ? '<span class="group-label">' + escapeHtml(g.label) + '</span>' : '') + '</div>' +
      bulletsHtml +
      actions +
    '</div>';
  }).join('') || '<div class="empty" style="padding:12px;text-align:left">Tidak ada grup aktif</div>';

  // Panel: Konfirmasi (two cols — show inside Confirmed/Pending tabs separately)
  const panelGrup = groupsHtml;
  const panelActions = renderActionsAggregated(allActions);
  const panelConfirmed = renderKonfList(konf.confirmed, 'confirmed');
  const panelPending = renderKonfList(konf.pending, 'pending');

  const panels = [
    {id: 'grup', html: panelGrup},
    {id: 'actions', html: panelActions},
    {id: 'confirmed', html: panelConfirmed},
    {id: 'pending', html: panelPending},
  ];
  const panelsHtml = panels.map((p, i) =>
    '<div class="section-panel" data-card="' + cardId + '" data-section="' + p.id + '"' +
      (i === 0 ? '' : ' style="display:none"') + '>' + p.html + '</div>'
  ).join('');

  return '<div class="parsed-body">' +
    '<div class="section-tabs">' + tabsHtml + '</div>' +
    '<div class="section-panels">' + panelsHtml + '</div>' +
  '</div>' +
  (parsed.footer ? '<div class="footer-row">' +
    (parsed.footer.urgent ? '<span class="' + (parsed.footer.urgent.toLowerCase() === 'tidak ada' ? '' : 'urgent-alert') + '">URGENT: ' + escapeHtml(parsed.footer.urgent) + '</span>' : '') +
    (parsed.footer.grup_aktif ? '<span>Grup aktif: ' + escapeHtml(parsed.footer.grup_aktif) + '</span>' : '') +
  '</div>' : '');
}

const RESUME_TAB_SHORT = {
  1: 'Situasi',
  2: 'Pipeline',
  3: 'Action',
  4: 'Konfirmasi',
  5: 'Kendala',
  6: 'Keputusan',
  7: '🎯 Direktur',
  8: '📬 HOD',
};

function renderResumeParsed(parsed, cardId) {
  // Text-only renderer (used inside renderBulletsNested for **bold** support)
  const renderBulletText = t => escapeHtml(t).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  const sections = parsed.sections || [];
  if (!sections.length) {
    return '<div class="parsed-body"><div class="empty">Resume kosong</div></div>';
  }
  // Build tabs and panels
  const tabsHtml = sections.map((s, i) => {
    let countVal = 0;
    let countClass = '';
    if (s.num === 4) {
      const k = parsed.konfirmasi || {confirmed: [], outstanding: []};
      countVal = k.confirmed.length + k.outstanding.length;
      // Mark count as alert if there are outstanding items with TUA
      const tuaCount = (k.outstanding || []).filter(it => it.tua).length;
      if (tuaCount > 0) countClass = ' alert';
    } else if (s.num === 8 && Array.isArray(s.hod_groups)) {
      countVal = s.hod_groups.reduce((n, g) => n + g.bullets.length, 0);
    } else if (s.bullets.length) {
      countVal = s.bullets.length;
    }
    const countHtml = countVal > 0 ? ' <span class="tab-count' + countClass + '">' + countVal + '</span>' : '';
    const label = RESUME_TAB_SHORT[s.num] || s.title.split(' ')[0];
    return '<button class="section-tab' + (i === 0 ? ' active' : '') +
      '" data-card="' + cardId + '" data-section="' + s.num + '">' +
      s.num + '. ' + label + countHtml +
      '</button>';
  }).join('');

  const panelsHtml = sections.map((s, i) => {
    let body = '';
    if (s.paragraph) {
      body = '<div class="resume-paragraph">' +
        escapeHtml(s.paragraph).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>') +
      '</div>';
    }
    if (s.num === 4) {
      const konf = parsed.konfirmasi || {confirmed: [], outstanding: []};
      body += '<div class="konf-grid">' +
        '<div>' +
          '<div class="konf-col-title ok">✓ Terkonfirmasi Baru (' + konf.confirmed.length + ')</div>' +
          renderKonfList(konf.confirmed, 'confirmed') +
        '</div>' +
        '<div>' +
          '<div class="konf-col-title pending">⏳ Outstanding (' + konf.outstanding.length + ')</div>' +
          renderKonfList(konf.outstanding, 'outstanding') +
        '</div>' +
      '</div>';
    } else if (s.num === 8 && Array.isArray(s.hod_groups) && s.hod_groups.length) {
      // HOD routing — render per-HOD sub-cards (only HOD yang punya item)
      const totalItems = s.hod_groups.reduce((n, g) => n + g.bullets.length, 0);
      body += '<div class="hod-summary">📬 ' + totalItems + ' item ke ' + s.hod_groups.length + ' HOD</div>';
      body += '<div class="hod-grid">' + s.hod_groups.map(g => {
        const bulletsHtml = renderBulletsNested(g.bullets, {className: 'resume-bullets', renderText: renderBulletText});
        return '<div class="hod-card">' +
          '<div class="hod-label">' + escapeHtml(g.label) +
            ' <span class="hod-count">(' + g.bullets.length + ')</span>' +
          '</div>' +
          bulletsHtml +
        '</div>';
      }).join('') + '</div>';
    } else if (s.bullets.length) {
      body += renderBulletsNested(s.bullets, {className: 'resume-bullets', renderText: renderBulletText});
    }
    if (!body) body = '<div class="empty" style="padding:12px;text-align:left">Tidak ada item</div>';
    return '<div class="section-panel" data-card="' + cardId + '" data-section="' + s.num + '"' +
      (i === 0 ? '' : ' style="display:none"') + '>' +
      '<div class="resume-section-title">' + s.num + '. ' + escapeHtml(s.title) + '</div>' +
      body +
    '</div>';
  }).join('');

  return '<div class="parsed-body">' +
    '<div class="section-tabs">' + tabsHtml + '</div>' +
    '<div class="section-panels">' + panelsHtml + '</div>' +
  '</div>' +
  (parsed.generated ? '<div class="footer-row"><span>Generated: ' + escapeHtml(parsed.generated) + '</span></div>' : '');
}

function makeCard(kind, item) {
  const id = kind + '-' + item.name;
  const head = '<div class="card-head">' +
    '<span><span class="badge">' + kind.toUpperCase() + '</span> ' + item.time + ' WIB <small style="opacity:.6">(' + item.size + ' B)</small></span>' +
    '<span>' +
      '<button class="view-toggle active" data-mode="parsed" data-target="' + id + '">parsed</button>' +
      '<button class="view-toggle" data-mode="raw" data-target="' + id + '">raw</button>' +
      '<button class="toggle" data-target="' + id + '" title="Collapse/expand">▾</button>' +
    '</span></div>';
  let parsedHtml = '';
  if (item.parsed) {
    parsedHtml = kind === 'rekap' ? renderRekapParsed(item.parsed, id) : renderResumeParsed(item.parsed, id);
  }
  const body = '<div class="card-body" id="' + id + '">' +
    '<div class="view-parsed">' + (parsedHtml || '<div class="empty">No parsed data</div>') + '</div>' +
    '<div class="view-raw" style="display:none">' + renderContent(item.content) + '</div>' +
  '</div>';
  return '<div class="card ' + kind + '">' + head + body + '</div>';
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderTimelineChart(timeline) {
  if (!timeline.length) return '<div class="chart-empty">Belum ada rekap hari ini</div>';
  const W = 360, H = 180, PAD_L = 32, PAD_B = 28, PAD_T = 12, PAD_R = 8;
  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_B - PAD_T;
  const maxV = Math.max(1, ...timeline.flatMap(t => [t.confirmed, t.pending]));
  const groupW = innerW / timeline.length;
  const barW = Math.min(14, groupW * 0.4);
  let bars = '', labels = '';
  timeline.forEach((t, i) => {
    const cx = PAD_L + groupW * i + groupW / 2;
    const cHeight = (t.confirmed / maxV) * innerH;
    const pHeight = (t.pending / maxV) * innerH;
    bars += `<rect class="bar-confirmed" x="${cx - barW - 1}" y="${PAD_T + innerH - cHeight}" width="${barW}" height="${cHeight}"/>`;
    bars += `<rect class="bar-pending" x="${cx + 1}" y="${PAD_T + innerH - pHeight}" width="${barW}" height="${pHeight}"/>`;
    labels += `<text class="axis-label" x="${cx}" y="${H - 12}" text-anchor="middle">${t.time}</text>`;
    if (t.confirmed > 0) labels += `<text class="value-label" x="${cx - barW/2 - 1}" y="${PAD_T + innerH - cHeight - 3}" text-anchor="middle">${t.confirmed}</text>`;
    if (t.pending > 0) labels += `<text class="value-label" x="${cx + barW/2 + 1}" y="${PAD_T + innerH - pHeight - 3}" text-anchor="middle">${t.pending}</text>`;
  });
  // Y-axis ticks
  let yTicks = '';
  for (let i = 0; i <= 3; i++) {
    const v = Math.round((maxV * i) / 3);
    const y = PAD_T + innerH - (v / maxV) * innerH;
    yTicks += `<line class="grid" x1="${PAD_L}" y1="${y}" x2="${W - PAD_R}" y2="${y}"/>`;
    yTicks += `<text class="axis-label" x="${PAD_L - 4}" y="${y + 3}" text-anchor="end">${v}</text>`;
  }
  // Legend
  const legend = `
    <rect class="bar-confirmed" x="${PAD_L}" y="2" width="8" height="8"/>
    <text class="axis-label" x="${PAD_L + 11}" y="10">confirmed</text>
    <rect class="bar-pending" x="${PAD_L + 80}" y="2" width="8" height="8"/>
    <text class="axis-label" x="${PAD_L + 91}" y="10">pending</text>`;
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${yTicks}${bars}${labels}${legend}
    <line class="axis" x1="${PAD_L}" y1="${PAD_T + innerH}" x2="${W - PAD_R}" y2="${PAD_T + innerH}"/>
  </svg>`;
}

function renderPicChart(top) {
  if (!top.length) return '<div class="chart-empty">Tidak ada pending item</div>';
  const W = 360, H = Math.max(80, top.length * 26 + 16), PAD_L = 110, PAD_R = 20;
  const innerW = W - PAD_L - PAD_R;
  const maxV = Math.max(1, ...top.map(t => t.count));
  let html = '';
  top.forEach((t, i) => {
    const y = 14 + i * 26;
    const w = (t.count / maxV) * innerW;
    html += `<text class="axis-label" x="${PAD_L - 6}" y="${y + 11}" text-anchor="end">${escapeHtml(t.pic.length > 18 ? t.pic.slice(0, 17) + '…' : t.pic)}</text>`;
    html += `<rect class="bar-pic" x="${PAD_L}" y="${y}" width="${w}" height="16" rx="2"/>`;
    html += `<text class="value-label" x="${PAD_L + w + 4}" y="${y + 12}">${t.count}</text>`;
  });
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${html}</svg>`;
}

function renderWeeklyChart(weekly) {
  if (!weekly.length) return '<div class="chart-empty">Tidak ada data 7 hari</div>';
  const W = 760, H = 220, PAD_L = 36, PAD_B = 36, PAD_T = 16, PAD_R = 12;
  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_B - PAD_T;
  const maxV = Math.max(1, ...weekly.flatMap(d => [d.confirmed, d.pending]));
  const groupW = innerW / weekly.length;
  const barW = Math.min(18, groupW * 0.32);
  let bars = '', labels = '', yTicks = '';

  for (let i = 0; i <= 4; i++) {
    const v = Math.round((maxV * i) / 4);
    const y = PAD_T + innerH - (v / maxV) * innerH;
    yTicks += `<line class="grid" x1="${PAD_L}" y1="${y}" x2="${W - PAD_R}" y2="${y}"/>`;
    yTicks += `<text class="axis-label" x="${PAD_L - 4}" y="${y + 3}" text-anchor="end">${v}</text>`;
  }

  weekly.forEach((d, i) => {
    const cx = PAD_L + groupW * i + groupW / 2;
    const cHeight = (d.confirmed / maxV) * innerH;
    const pHeight = (d.pending / maxV) * innerH;
    bars += `<rect class="bar-confirmed" x="${cx - barW - 2}" y="${PAD_T + innerH - cHeight}" width="${barW}" height="${cHeight}"><title>${d.date}: confirmed ${d.confirmed}</title></rect>`;
    bars += `<rect class="bar-pending" x="${cx + 2}" y="${PAD_T + innerH - pHeight}" width="${barW}" height="${pHeight}"><title>${d.date}: pending ${d.pending}</title></rect>`;
    if (d.tua > 0) {
      const tuaY = PAD_T + innerH - pHeight - 8;
      bars += `<text class="value-label bar-tua-text" x="${cx + 2 + barW/2}" y="${tuaY}" text-anchor="middle">⚠${d.tua}</text>`;
    }
    // Date label: just MM-DD (last 5 chars)
    const dateShort = d.date.slice(5).replace('-', '/');
    labels += `<text class="axis-label" x="${cx}" y="${H - 18}" text-anchor="middle">${dateShort}</text>`;
    // Day-of-week label
    const dow = ['Min','Sen','Sel','Rab','Kam','Jum','Sab'][new Date(d.date).getDay()];
    labels += `<text class="axis-label" x="${cx}" y="${H - 4}" text-anchor="middle" opacity="0.6">${dow}</text>`;
    if (!d.has_data) {
      labels += `<text class="axis-label" x="${cx}" y="${PAD_T + innerH/2}" text-anchor="middle" opacity="0.3" font-style="italic">no data</text>`;
    }
  });

  const legend = `
    <rect class="bar-confirmed" x="${PAD_L}" y="2" width="9" height="9"/>
    <text class="axis-label" x="${PAD_L + 12}" y="10">confirmed</text>
    <rect class="bar-pending" x="${PAD_L + 90}" y="2" width="9" height="9"/>
    <text class="axis-label" x="${PAD_L + 102}" y="10">pending</text>
    <text class="axis-label bar-tua-text" x="${PAD_L + 165}" y="10">⚠ = TUA count</text>`;
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${yTicks}${bars}${labels}${legend}
    <line class="axis" x1="${PAD_L}" y1="${PAD_T + innerH}" x2="${W - PAD_R}" y2="${PAD_T + innerH}"/>
  </svg>`;
}

function renderAgeChart(buckets) {
  const labels = ['<1h', '1-2h', '2-4h', '4-8h', '>8h'];
  const values = labels.map(l => buckets[l] || 0);
  if (values.every(v => v === 0)) return '<div class="chart-empty">Tidak ada pending item</div>';
  const W = 360, H = 180, PAD_L = 32, PAD_B = 28, PAD_T = 12, PAD_R = 8;
  const innerW = W - PAD_L - PAD_R, innerH = H - PAD_B - PAD_T;
  const maxV = Math.max(1, ...values);
  const groupW = innerW / labels.length;
  let html = '';
  values.forEach((v, i) => {
    const cx = PAD_L + groupW * i + groupW / 2;
    const h = (v / maxV) * innerH;
    const isTua = i >= 3; // 4-8h and >8h are TUA
    const cls = isTua ? 'bar-tua' : 'bar-pending';
    html += `<rect class="${cls}" x="${cx - groupW/3}" y="${PAD_T + innerH - h}" width="${groupW * 2/3}" height="${h}" rx="2"/>`;
    html += `<text class="axis-label" x="${cx}" y="${H - 12}" text-anchor="middle">${labels[i]}</text>`;
    if (v > 0) html += `<text class="value-label" x="${cx}" y="${PAD_T + innerH - h - 3}" text-anchor="middle">${v}</text>`;
  });
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${html}
    <line class="axis" x1="${PAD_L}" y1="${PAD_T + innerH}" x2="${W - PAD_R}" y2="${PAD_T + innerH}"/>
  </svg>`;
}

function formatAge(mtime, nowEpoch) {
  if (!mtime) return { text: '—', class: 'overdue' };
  const ageS = Math.max(0, nowEpoch - mtime);
  const ageM = Math.floor(ageS / 60);
  if (ageM < 60) return { text: ageM + 'm ago', class: ageM < 30 ? 'fresh' : (ageM < 120 ? 'late' : 'overdue') };
  const ageH = Math.floor(ageM / 60);
  if (ageH < 24) return { text: ageH + 'h ago', class: ageH < 5 ? 'late' : 'overdue' };
  return { text: Math.floor(ageH / 24) + 'd ago', class: 'overdue' };
}

// Known cron schedule (HH or HH.MM as decimal fraction of hour)
const CRON_SCHEDULE = [
  { label: 'Rekap', times: [7, 12, 17, 22] },                  // 07/12/17/22
  { label: 'Resume', times: [14, 22 + 10/60] },                // 14:00 & 22:10
  { label: 'Notif', times: [14 + 5/60, 22 + 15/60] },          // 14:05 & 22:15
  { label: 'Members', times: [22.5] },                          // 22:30
  { label: 'Pola', times: [23.5] },                             // 23:30
];

function computeNextFiring() {
  const now = new Date();
  let candidates = [];
  for (const sched of CRON_SCHEDULE) {
    for (const t of sched.times) {
      const h = Math.floor(t);
      const m = Math.round((t - h) * 60);
      const fire = new Date(now);
      fire.setHours(h, m, 0, 0);
      if (fire <= now) fire.setDate(fire.getDate() + 1); // tomorrow
      candidates.push({ label: sched.label, time: fire });
    }
  }
  candidates.sort((a, b) => a.time - b.time);
  return candidates[0];
}

function nextFiringText() {
  const next = computeNextFiring();
  if (!next) return { text: '—', sub: '' };
  const now = new Date();
  const diffMs = next.time - now;
  const diffMin = Math.floor(diffMs / 60000);
  const hh = String(next.time.getHours()).padStart(2, '0');
  const mm = String(next.time.getMinutes()).padStart(2, '0');
  const timeStr = hh + ':' + mm;
  const sameDay = next.time.toDateString() === now.toDateString();
  let countdown;
  if (diffMin < 60) countdown = 'in ' + diffMin + 'm';
  else countdown = 'in ' + Math.floor(diffMin / 60) + 'h ' + (diffMin % 60) + 'm';
  return {
    text: next.label + ' @ ' + timeStr,
    sub: countdown + (sameDay ? '' : ' (besok)'),
  };
}

function renderSystemStatus(status) {
  if (!status) return '';
  const now = status.now;
  const rekap = formatAge(status.rekap?.mtime, now);
  const resume = formatAge(status.resume?.mtime, now);
  const pola = formatAge(status.pola?.mtime, now);
  const capture = formatAge(status.capture?.mtime, now);
  const captureCount = status.capture?.count_today || 0;
  const cost = status.cost || {};
  const nextFire = nextFiringText();

  const card = (label, ageObj, sub, classOverride) =>
    '<div class="status-card ' + (classOverride || ageObj.class) + '">' +
      '<div class="status-label">' + label + '</div>' +
      '<div class="status-value">' + ageObj.text + '</div>' +
      (sub ? '<div class="status-sub">' + sub + '</div>' : '') +
    '</div>';

  const infoCard = (label, value, sub) =>
    '<div class="status-card info">' +
      '<div class="status-label">' + label + '</div>' +
      '<div class="status-value">' + value + '</div>' +
      (sub ? '<div class="status-sub">' + sub + '</div>' : '') +
    '</div>';

  let html = '<div class="status-row">';
  html += card('Last Rekap', rekap);
  html += card('Last Resume', resume);
  html += card('Last Pola', pola);
  html += card('Last Capture', capture, captureCount + ' msgs today');
  if (cost.today_usd !== undefined) {
    html += infoCard('Cost Today', '$' + cost.today_usd.toFixed(2), (cost.today_tokens || '') + ' tokens');
  } else if (cost.total_usd !== undefined) {
    html += infoCard('Cost 30d', '$' + cost.total_usd.toFixed(2), (cost.tokens || '') + ' tokens');
  }
  html += infoCard('Next Firing', nextFire.text, nextFire.sub);
  html += '</div>';
  return html;
}

function renderOverview(data) {
  const st = data.stats || {};
  const t = st.totals || {};
  const weekly = st.weekly || [];
  const weekConfirmed = weekly.reduce((s, d) => s + d.confirmed, 0);
  const weekTua = weekly.reduce((s, d) => s + d.tua, 0);
  const sysStatusHtml = renderSystemStatus(data.system_status);
  const stats = '<div class="stat-row">' +
    '<span class="stat">Rekap hari ini: <strong>' + (t.rekap_count || 0) + '</strong></span>' +
    '<span class="stat">Resume: <strong>' + (t.resume_count || 0) + '</strong></span>' +
    '<span class="stat ok">Confirmed today: <strong>' + (t.confirmed_today || 0) + '</strong></span>' +
    '<span class="stat">Pending now: <strong>' + (t.pending_now || 0) + '</strong></span>' +
    '<span class="stat ' + (t.tua_now > 0 ? 'alert' : '') + '">TUA now: <strong>' + (t.tua_now || 0) + '</strong></span>' +
    '<span class="stat">7-day confirmed: <strong>' + weekConfirmed + '</strong></span>' +
    '<span class="stat ' + (weekTua > 0 ? 'alert' : '') + '">7-day TUA total: <strong>' + weekTua + '</strong></span>' +
    '</div>';

  const charts =
    '<div class="chart-card" style="margin-bottom:16px"><div class="chart-title">7 Hari Terakhir — Confirmed vs Pending</div>' + renderWeeklyChart(weekly) + '</div>' +
    '<div class="charts">' +
      '<div class="chart-card"><div class="chart-title">Hari ini — Confirmed vs Pending per rekap</div>' + renderTimelineChart(st.timeline || []) + '</div>' +
      '<div class="chart-card"><div class="chart-title">Top PIC dengan Outstanding</div>' + renderPicChart(st.top_pic || []) + '</div>' +
      '<div class="chart-card"><div class="chart-title">Umur Item Pending</div>' + renderAgeChart(st.age_buckets || {}) + '</div>' +
    '</div>';

  // Latest weekend briefing card — compact preview + link ke tab Briefing
  let briefingPreview = '';
  if (data.latest_briefing) {
    const b = data.latest_briefing;
    const sizeKb = (b.size / 1024).toFixed(1);
    // Calculate age in days
    const briefDate = new Date(b.date);
    const ageDays = Math.floor((Date.now() - briefDate.getTime()) / 86400000);
    const ageText = ageDays === 0 ? 'hari ini' : ageDays === 1 ? '1 hari lalu' : ageDays + ' hari lalu';
    briefingPreview = '<div class="section briefing-overview-card">' +
      '<div class="section-head">📋 Briefing Direktur Terbaru ' +
        '<span class="meta">' + escapeHtml(b.date) + ' · ' + escapeHtml(b.time) + ' WIB · ' + ageText + '</span>' +
      '</div>' +
      '<div class="section-body">' +
        '<div class="briefing-overview-meta">' +
          '<strong>' + escapeHtml(b.label || 'Briefing Mingguan') + '</strong>' +
          (b.disiapkan ? ' · Disiapkan ' + escapeHtml(b.disiapkan) : '') +
          ' · ' + b.section_count + ' sections · ' + sizeKb + ' KB' +
        '</div>' +
        (b.ringkasan_excerpt ? '<div class="briefing-overview-excerpt"><div class="briefing-excerpt-label">A. Ringkasan Eksekutif</div>' +
          renderInlineMd(b.ringkasan_excerpt) + '</div>' : '') +
        '<button class="briefing-open-btn" onclick="switchTab(\'briefing\')">📋 Buka Briefing Lengkap →</button>' +
      '</div>' +
    '</div>';
  }

  // Latest resume preview at bottom of overview
  let resumePreview = '';
  if (data.latest_resume) {
    resumePreview = '<div class="section">' +
      '<div class="section-head">Resume Terbaru <span class="meta">' + data.latest_resume.time + ' WIB</span></div>' +
      '<div class="section-body"><div class="cards">' + makeCard('resume', data.latest_resume) + '</div></div>' +
      '</div>';
  }

  document.getElementById('panel-overview').innerHTML = sysStatusHtml + stats + charts + briefingPreview + resumePreview;
}

function makeCollectiveCard(kind, parsed, meta) {
  // Like makeCard but for a synthetic "collective day" — no per-firing time, shows date + count.
  const id = kind + '-collective';
  const head = '<div class="card-head">' +
    '<span><span class="badge">KOLEKTIF</span> ' + meta.date + ' · ' + meta.count + ' firings' +
    (meta.first_time && meta.last_time && meta.first_time !== meta.last_time
      ? ' (' + meta.first_time + ' → ' + meta.last_time + ' WIB)' : '') +
    '</span>' +
    '<button class="toggle" data-target="' + id + '" title="Collapse/expand">▾</button></div>';
  const parsedHtml = kind === 'rekap' ? renderRekapParsed(parsed, id) : renderResumeParsed(parsed, id);
  const body = '<div class="card-body" id="' + id + '">' + parsedHtml + '</div>';
  return '<div class="card ' + kind + ' collective">' + head + body + '</div>';
}

function renderRekapPanel(data) {
  let html = '';
  if (!data.rekap.length) {
    html = '<div class="empty">Belum ada rekap untuk tanggal ' + data.date + '</div>';
    document.getElementById('panel-rekap').innerHTML = html;
    return;
  }

  // Collective view at top
  if (data.collective_rekap) {
    const c = data.collective_rekap;
    html += '<div class="section">' +
      '<div class="section-head">Rekap Kolektif Hari Ini — ' + data.date + '</div>' +
      '<div class="section-body"><div class="cards">' +
        makeCollectiveCard('rekap', c, {date: data.date, count: c.count, first_time: c.first_time, last_time: c.last_time}) +
      '</div></div>' +
    '</div>';
  }

  // Individual firings (collapsed by default)
  const individuals = data.rekap.map(r => {
    const card = makeCard('rekap', r);
    return card.replace('<div class="card rekap">', '<div class="card rekap collapsed">').replace('▾</button>', '▸</button>');
  }).join('');
  html += '<div class="section">' +
    '<div class="section-head">Per-Firing (' + data.rekap.length + ') <span class="meta">klik card untuk expand</span></div>' +
    '<div class="section-body"><div class="cards">' + individuals + '</div></div>' +
  '</div>';

  document.getElementById('panel-rekap').innerHTML = html;
}

function renderResumePanel(data) {
  let html = '';
  if (!data.resume.length) {
    html = '<div class="empty">Belum ada resume untuk tanggal ' + data.date + '</div>';
    document.getElementById('panel-resume').innerHTML = html;
    return;
  }

  // Collective view at top (= latest resume + count meta)
  if (data.collective_resume && data.collective_resume.parsed) {
    const c = data.collective_resume;
    html += '<div class="section">' +
      '<div class="section-head">Resume Kolektif Hari Ini — ' + data.date + '</div>' +
      '<div class="section-body"><div class="cards">' +
        makeCollectiveCard('resume', c.parsed, {date: data.date, count: c.count, first_time: c.first_time, last_time: c.last_time}) +
      '</div></div>' +
    '</div>';
  }

  // Individual firings (collapsed by default)
  const individuals = data.resume.map(r => {
    const card = makeCard('resume', r);
    return card.replace('<div class="card resume">', '<div class="card resume collapsed">').replace('▾</button>', '▸</button>');
  }).join('');
  html += '<div class="section">' +
    '<div class="section-head">Per-Firing (' + data.resume.length + ') <span class="meta">klik card untuk expand</span></div>' +
    '<div class="section-body"><div class="cards">' + individuals + '</div></div>' +
  '</div>';

  document.getElementById('panel-resume').innerHTML = html;
}

let membersData = null;
let membersFilter = 'all';
let membersSearchQuery = '';

let polaData = null;
let polaSearchQuery = '';

let briefingData = null;       // {files: [...], current: {filename, raw, parsed, size}}
let briefingActiveSection = null;
let briefingSelectedFile = '';

async function loadBriefings(filename) {
  const q = filename ? '?file=' + encodeURIComponent(filename) : '';
  try {
    const r = await fetch('/api/briefings' + q);
    briefingData = await r.json();
    if (briefingData.current) briefingSelectedFile = briefingData.current.filename;
  } catch (e) {
    briefingData = {files: [], current: null};
  }
  renderBriefingPanel();
}

function renderBriefingPanel() {
  const panel = document.getElementById('panel-briefing');
  if (!panel) return;
  if (!briefingData) {
    panel.innerHTML = '<div class="empty">Loading briefings…</div>';
    return;
  }
  const files = briefingData.files || [];
  const cur = briefingData.current;
  if (!files.length) {
    panel.innerHTML = '<div class="empty">Belum ada briefing weekend. Cron jalan Sabtu/Minggu pagi 07:00.</div>';
    return;
  }
  // File picker dropdown
  const optionsHtml = files.map(f => {
    const sel = (f.filename === briefingSelectedFile) ? ' selected' : '';
    const sizeKb = (f.size / 1024).toFixed(1);
    return '<option value="' + escapeHtml(f.filename) + '"' + sel + '>' +
      f.date + ' · ' + f.time + ' WIB · ' + sizeKb + 'KB</option>';
  }).join('');
  const picker = '<div class="briefing-toolbar">' +
    '<label class="briefing-label">Briefing:</label>' +
    '<select id="briefing-file-picker">' + optionsHtml + '</select>' +
    (cur ? '<span class="briefing-meta">' + escapeHtml(cur.parsed.header.label || '') +
      (cur.parsed.header.disiapkan ? ' · Disiapkan ' + escapeHtml(cur.parsed.header.disiapkan) : '') + '</span>' : '') +
  '</div>';

  if (!cur) {
    panel.innerHTML = picker + '<div class="empty">Belum ada konten untuk file ini.</div>';
  } else {
    const sections = cur.parsed.sections || [];
    if (!briefingActiveSection && sections.length) briefingActiveSection = sections[0].id;
    // Color cycle per section — Adminator-style colored letter chips
    const sectionColors = ['accent', 'info', 'ok', 'warn', 'danger', 'accent', 'info', 'ok'];
    // Sub-tabs (card-grid dengan letter chip + title)
    const tabsHtml = sections.map((s, idx) => {
      const active = (s.id === briefingActiveSection) ? ' active' : '';
      const colorVar = sectionColors[idx % sectionColors.length];
      return '<button class="section-tab tab-' + colorVar + active + '" data-section="' + escapeHtml(s.id) + '" title="' + escapeHtml(s.title) + '">' +
        '<span class="tab-letter">' + escapeHtml(s.id) + '</span>' +
        '<span class="briefing-tab-title">' + escapeHtml(s.short) + '</span>' +
      '</button>';
    }).join('');
    // Section panels
    const panelsHtml = sections.map(s => {
      const display = (s.id === briefingActiveSection) ? '' : ' style="display:none"';
      return '<div class="briefing-section" data-section="' + escapeHtml(s.id) + '"' + display + '>' +
        '<div class="briefing-section-title">' + escapeHtml(s.title) + '</div>' +
        '<div class="briefing-section-body">' + renderBriefingMarkdown(s.body) + '</div>' +
      '</div>';
    }).join('');
    panel.innerHTML = picker +
      '<div class="briefing-card">' +
        '<div class="briefing-section-tabs">' + tabsHtml + '</div>' +
        '<div class="briefing-panels">' + panelsHtml + '</div>' +
      '</div>';
  }

  // Hook picker change
  const picSel = panel.querySelector('#briefing-file-picker');
  if (picSel) {
    picSel.addEventListener('change', e => {
      briefingSelectedFile = e.target.value;
      briefingActiveSection = null;  // reset to first section of new file
      loadBriefings(briefingSelectedFile);
    });
  }
  // Hook sub-tab clicks
  panel.querySelectorAll('.briefing-section-tabs .section-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      briefingActiveSection = btn.dataset.section;
      renderBriefingPanel();
    });
  });
}

// Minimal markdown-to-HTML renderer for briefing section bodies.
// Handles: **bold**, ### **headings**, tables (| ... |), bullets •/-, dashed separators ---, numbered lists, blank-line paragraphs.
function renderBriefingMarkdown(text) {
  if (!text) return '';
  const lines = text.split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const stripped = line.trim();
    // Horizontal rule
    if (stripped === '---' || stripped === '***') {
      out.push('<hr>');
      i++; continue;
    }
    // Table: line starts with | and next line is | --- |
    if (stripped.startsWith('|') && i + 1 < lines.length && lines[i+1].trim().match(/^\|[\s:|\-]+\|$/)) {
      // Header row
      const headers = stripped.slice(1, -1).split('|').map(c => c.trim());
      let tableHtml = '<table class="briefing-table"><thead><tr>' +
        headers.map(h => '<th>' + renderInlineMd(h) + '</th>').join('') +
      '</tr></thead><tbody>';
      i += 2; // skip header + separator
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        const cells = lines[i].trim().slice(1, -1).split('|').map(c => c.trim());
        tableHtml += '<tr>' + cells.map(c => '<td>' + renderInlineMd(c) + '</td>').join('') + '</tr>';
        i++;
      }
      tableHtml += '</tbody></table>';
      out.push(tableHtml);
      continue;
    }
    // Sub-heading: ### or ### **title**
    if (stripped.startsWith('### ')) {
      out.push('<h4 class="briefing-h4">' + renderInlineMd(stripped.slice(4)) + '</h4>');
      i++; continue;
    }
    if (stripped.startsWith('## ')) {
      out.push('<h3 class="briefing-h3">' + renderInlineMd(stripped.slice(3)) + '</h3>');
      i++; continue;
    }
    // Bullet list (•, -, ✅, 🔴, 🟡, 🟢)
    if (/^[•\-✅🔴🟡🟢]/.test(stripped)) {
      const items = [];
      while (i < lines.length && /^[•\-✅🔴🟡🟢]/.test(lines[i].trim())) {
        const sline = lines[i].trim();
        items.push('<li>' + renderInlineMd(sline.replace(/^[•\-]\s*/, '')) + '</li>');
        i++;
      }
      out.push('<ul class="briefing-bullets">' + items.join('') + '</ul>');
      continue;
    }
    // Numbered item (1., 2., …) — preserve as <ol> in paragraph form
    if (/^\d+\.\s+/.test(stripped)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push('<li>' + renderInlineMd(lines[i].trim().replace(/^\d+\.\s+/, '')) + '</li>');
        i++;
      }
      out.push('<ol class="briefing-ol">' + items.join('') + '</ol>');
      continue;
    }
    // Blank line
    if (!stripped) {
      out.push('');  // paragraph separator
      i++; continue;
    }
    // Default: paragraph (collect consecutive non-special lines)
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^[#|•\-✅🔴🟡🟢]/.test(lines[i].trim()) && !/^\d+\.\s+/.test(lines[i].trim()) && lines[i].trim() !== '---') {
      buf.push(renderInlineMd(lines[i].trim()));
      i++;
    }
    if (buf.length) out.push('<p class="briefing-p">' + buf.join(' ') + '</p>');
  }
  return out.join('\n');
}

function renderInlineMd(s) {
  return escapeHtml(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

async function loadPola() {
  try {
    const r = await fetch('/api/pola');
    const j = await r.json();
    polaData = j.pola || [];
  } catch (e) {
    polaData = [];
  }
  renderPolaPanel();
}

function renderMarkdownBody(text) {
  // Minimal MD rendering: bold via **x**, bullets, preserve newlines via white-space:pre-wrap
  return escapeHtml(text).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function renderPolaPanel() {
  const panel = document.getElementById('panel-pola');
  if (!panel) return;
  if (!polaData) {
    panel.innerHTML = '<div class="empty">Loading pola…</div>';
    return;
  }
  let filtered = polaData;
  if (polaSearchQuery) {
    const q = polaSearchQuery.toLowerCase();
    filtered = filtered.filter(p =>
      p.jid.toLowerCase().includes(q) ||
      (p.subject || '').toLowerCase().includes(q) ||
      p.content.toLowerCase().includes(q)
    );
  }
  const toolbar = '<div class="members-toolbar">' +
    '<input type="search" class="members-search" placeholder="Cari nama grup / JID / isi pola…" value="' + escapeHtml(polaSearchQuery) + '">' +
    '<span class="members-stat">' + filtered.length + '/' + polaData.length + ' grup profile</span>' +
  '</div>';
  if (filtered.length === 0) {
    panel.innerHTML = toolbar + '<div class="empty">Tidak ada pola yang match</div>';
    return;
  }
  const cards = filtered.map((p, i) => {
    const sections = (p.parsed?.sections || []).map(s =>
      '<div class="pola-section">' +
        '<div class="pola-section-title">' + escapeHtml(s.title) + '</div>' +
        '<div class="pola-section-body">' + renderMarkdownBody(s.body) + '</div>' +
      '</div>'
    ).join('');
    const ts = new Date(p.mtime * 1000).toLocaleString('id-ID', { dateStyle: 'short', timeStyle: 'short' });
    const openClass = i === 0 ? ' open' : '';
    const userBadge = p.subject_user_labeled ? '<span class="pola-name-badge" title="Nama manual (di-set dari dashboard)">✏</span>' : '';
    const nameSpan = p.subject
      ? '<span class="pola-name" data-jid="' + escapeHtml(p.jid) + '" title="Klik untuk edit nama grup">' + escapeHtml(p.subject) + userBadge + '</span>'
      : '<span class="pola-name pola-name-unknown" data-jid="' + escapeHtml(p.jid) + '" title="Klik untuk set nama grup">(klik untuk set nama)</span>';
    const titleHtml = '<div class="pola-title">' + nameSpan + '<code class="pola-jid">' + escapeHtml(p.jid) + '</code></div>';
    return '<div class="pola-card' + openClass + '">' +
      '<div class="pola-card-head">' + titleHtml + '<span class="pola-meta">' + (p.parsed?.sections?.length || 0) + ' sections · ' + ts + '</span></div>' +
      '<div class="pola-card-body">' + sections + '</div>' +
    '</div>';
  }).join('');
  panel.innerHTML = toolbar + '<div class="pola-grid">' + cards + '</div>';
  // Search handler
  const searchInp = panel.querySelector('.members-search');
  if (searchInp) {
    searchInp.addEventListener('input', e => {
      polaSearchQuery = e.target.value;
      renderPolaPanel();
    });
  }
  // Expand/collapse (skip when click bermula di .pola-name — itu edit handler)
  panel.querySelectorAll('.pola-card-head').forEach(head => {
    head.addEventListener('click', (ev) => {
      if (ev.target.closest('.pola-name')) return;
      head.closest('.pola-card').classList.toggle('open');
    });
  });
  // Inline edit nama grup (input swap, lebih reliable dari window.prompt)
  panel.querySelectorAll('.pola-name').forEach(span => {
    span.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (span.dataset.editing === '1') return;
      span.dataset.editing = '1';
      const jid = span.dataset.jid;
      const current = span.classList.contains('pola-name-unknown') ? '' : (polaData.find(x => x.jid === jid)?.subject || '');
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.value = current;
      inp.className = 'pola-name-input';
      inp.placeholder = 'Nama grup (Enter=simpan, Esc=batal, kosongkan=hapus)';
      const restore = (newSubject) => {
        // newSubject === undefined → no change (cancel)
        if (newSubject === undefined) {
          renderPolaPanel();
          return;
        }
        fetch('/api/groups/update', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({jid, subject: newSubject})
        }).then(r => r.json()).then(j => {
          if (!j.ok) alert('Gagal simpan: ' + (j.error || 'unknown'));
          return loadPola();
        }).catch(e => alert('Gagal: ' + e.message));
      };
      let done = false;
      inp.addEventListener('keydown', (ke) => {
        if (ke.key === 'Enter') { ke.preventDefault(); done = true; restore(inp.value.trim()); }
        else if (ke.key === 'Escape') { ke.preventDefault(); done = true; restore(undefined); }
      });
      inp.addEventListener('blur', () => {
        if (done) return;
        done = true;
        const v = inp.value.trim();
        if (v === current) restore(undefined);
        else restore(v);
      });
      span.replaceWith(inp);
      inp.focus();
      inp.select();
    });
  });
}

async function loadMembers() {
  try {
    const r = await fetch('/api/members');
    membersData = await r.json();
  } catch (e) {
    membersData = { members: [], sender_names_from_messages: [] };
  }
  renderMembersPanel();
}

function renderMembersPanel() {
  const panel = document.getElementById('panel-members');
  if (!panel) return;
  if (!membersData) {
    panel.innerHTML = '<div class="empty">Loading members…</div>';
    return;
  }
  const all = membersData.members || [];
  // Filter
  let filtered = all;
  if (membersFilter === 'labeled') filtered = all.filter(m => m.user_labeled);
  else if (membersFilter === 'unlabeled') filtered = all.filter(m => !m.name || m.name === '');
  else if (membersFilter === 'autoonly') filtered = all.filter(m => !m.user_labeled && m.auto_name);
  else if (membersFilter === 'noted') filtered = all.filter(m => m.notes && m.notes.trim());
  else if (membersFilter === 'roster') filtered = all.filter(m => m.roster_source);
  else if (membersFilter === 'phantom') filtered = all.filter(m => m.roster_source && (m.appearance_count || 0) === 0);
  // Search
  if (membersSearchQuery) {
    const q = membersSearchQuery.toLowerCase();
    filtered = filtered.filter(m =>
      (m.phone || '').toLowerCase().includes(q) ||
      (m.name || '').toLowerCase().includes(q) ||
      (m.auto_name || '').toLowerCase().includes(q) ||
      (m.panggilan || '').toLowerCase().includes(q) ||
      (m.posisi || '').toLowerCase().includes(q) ||
      (m.cabang || '').toLowerCase().includes(q)
    );
  }

  const labeledCount = all.filter(m => m.user_labeled).length;
  const autoCount = all.filter(m => !m.user_labeled && m.auto_name).length;
  const unlabeledCount = all.filter(m => !m.name || m.name === '').length;
  const notedCount = all.filter(m => m.notes && m.notes.trim()).length;
  const rosterCount = all.filter(m => m.roster_source).length;
  const phantomCount = all.filter(m => m.roster_source && (m.appearance_count || 0) === 0).length;

  const toolbar = '<div class="members-toolbar">' +
    '<div class="members-filter">' +
      '<button data-filter="all"' + (membersFilter === 'all' ? ' class="active"' : '') + '>All (' + all.length + ')</button>' +
      '<button data-filter="labeled"' + (membersFilter === 'labeled' ? ' class="active"' : '') + '>Labeled (' + labeledCount + ')</button>' +
      '<button data-filter="autoonly"' + (membersFilter === 'autoonly' ? ' class="active"' : '') + '>Auto-only (' + autoCount + ')</button>' +
      '<button data-filter="unlabeled"' + (membersFilter === 'unlabeled' ? ' class="active"' : '') + '>Unlabeled (' + unlabeledCount + ')</button>' +
      '<button data-filter="noted"' + (membersFilter === 'noted' ? ' class="active"' : '') + '>📝 Noted (' + notedCount + ')</button>' +
      '<button data-filter="roster"' + (membersFilter === 'roster' ? ' class="active"' : '') + ' title="Member yang ada di roster resmi">📋 Roster (' + rosterCount + ')</button>' +
      '<button data-filter="phantom"' + (membersFilter === 'phantom' ? ' class="active"' : '') + ' title="Di roster tapi belum kedetect di group manapun">👻 Phantom (' + phantomCount + ')</button>' +
    '</div>' +
    '<input type="search" class="members-search" placeholder="Cari phone / nama…" value="' + escapeHtml(membersSearchQuery) + '">' +
    '<span class="members-stat">' + filtered.length + ' shown <span class="edit-hint">· klik nama untuk edit langsung</span></span>' +
  '</div>';

  let tableRows = '';
  if (filtered.length === 0) {
    tableRows = '<tr><td colspan="6" style="text-align:center;padding:30px;color:#4a5568;font-style:italic">Tidak ada member yang match</td></tr>';
  } else {
    tableRows = filtered.map(m => {
      const isLabeled = m.user_labeled;
      const hasAuto = m.auto_name && m.auto_name !== '';
      const isPhantom = m.roster_source && (m.appearance_count || 0) === 0;
      let nameCell;
      if (isLabeled) {
        nameCell = '<span class="name user-labeled">' + escapeHtml(m.name) + '</span><span class="badge-labeled">USER</span>';
      } else if (hasAuto) {
        nameCell = '<span class="name">' + escapeHtml(m.name || m.auto_name) + '</span>';
      } else {
        nameCell = '<span class="name unlabeled">(unlabeled)</span>';
      }
      // Append roster badge (panggilan + posisi/cabang hint)
      if (m.roster_source) {
        const pangText = m.panggilan ? escapeHtml(m.panggilan) : '';
        const posCab = [m.posisi, m.cabang].filter(Boolean).map(escapeHtml).join(' · ');
        const rosterTitle = posCab ? ' title="' + posCab + '"' : '';
        nameCell += '<span class="badge-roster"' + rosterTitle + '>' +
          (isPhantom ? '👻 ' : '📋 ') + (pangText || 'ROSTER') + '</span>';
      }
      const groupsCount = (m.groups || []).length;
      const noteIcon = m.notes && m.notes.trim()
        ? '<span class="note-icon" title="' + escapeHtml(m.notes.slice(0, 200)) + '">📝</span>'
        : '';
      const rowClass = isPhantom ? ' class="phantom-row"' : '';
      return '<tr data-phone="' + escapeHtml(m.phone) + '"' + rowClass + '>' +
        '<td><span class="phone">' + escapeHtml(m.phone) + '</span></td>' +
        '<td class="name-cell" data-phone="' + escapeHtml(m.phone) + '" title="Klik untuk edit">' + nameCell + noteIcon + '</td>' +
        '<td class="col-auto auto-name">' + escapeHtml(m.auto_name || '—') + '</td>' +
        '<td class="col-groups num">' + groupsCount + '</td>' +
        '<td class="num">' + (m.appearance_count || 0) + '</td>' +
      '</tr>';
    }).join('');
  }

  panel.innerHTML = toolbar +
    '<table class="members-table">' +
      '<thead><tr>' +
        '<th>Phone</th>' +
        '<th>Name</th>' +
        '<th class="col-auto">Auto-detected</th>' +
        '<th class="col-groups num" title="Number of groups">Grup</th>' +
        '<th class="num" title="Total appearances in runtime-context">Appearances</th>' +
      '</tr></thead>' +
      '<tbody>' + tableRows + '</tbody>' +
    '</table>';

  // Attach filter button handlers
  panel.querySelectorAll('.members-filter button').forEach(btn => {
    btn.addEventListener('click', () => {
      membersFilter = btn.dataset.filter;
      renderMembersPanel();
    });
  });
  // Search input
  const searchInp = panel.querySelector('.members-search');
  if (searchInp) {
    searchInp.addEventListener('input', e => {
      membersSearchQuery = e.target.value;
      renderMembersPanel();
    });
  }
  // Inline edit handlers on Name cells
  panel.querySelectorAll('td.name-cell').forEach(cell => {
    cell.addEventListener('click', e => {
      e.stopPropagation();
      startEditName(cell);
    });
  });
  // Row click → open detail modal (excluding name cell which handles inline edit)
  panel.querySelectorAll('tr[data-phone]').forEach(row => {
    row.style.cursor = 'pointer';
    row.addEventListener('click', e => {
      if (e.target.closest('.name-cell')) return;
      const phone = row.dataset.phone;
      if (phone) showMemberDetail(phone);
    });
  });
}

async function saveMemberField(phone, fields) {
  // fields can include `name`, `notes`, or both
  const r = await fetch('/api/members/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phone, ...fields })
  });
  return r.json();
}
// Back-compat alias used by inline name-cell editor
const saveMemberName = (phone, name) => saveMemberField(phone, { name });

function startEditName(cell) {
  if (cell.querySelector('.name-edit')) return; // already editing
  const phone = cell.dataset.phone;
  if (!phone || !membersData) return;
  const member = membersData.members.find(m => m.phone === phone);
  if (!member) return;
  const currentName = member.name || '';
  const originalHTML = cell.innerHTML;

  // Replace with input
  cell.innerHTML = '<input type="text" class="name-edit" value="' + (currentName ? currentName.replace(/"/g, '&quot;') : '') + '" placeholder="nama (kosong = unlabeled)">';
  const input = cell.querySelector('.name-edit');
  input.focus();
  input.select();

  let isFinishing = false;

  const finish = async (commit) => {
    if (isFinishing) return;
    isFinishing = true;
    const newName = input.value.trim();
    if (!commit || newName === currentName) {
      cell.innerHTML = originalHTML;
      return;
    }
    // Show pending state
    input.disabled = true;
    input.style.opacity = '0.6';
    const result = await saveMemberName(phone, newName);
    if (result && result.ok) {
      // Update local state
      member.name = newName;
      const autoName = member.auto_name || '';
      member.user_labeled = Boolean(newName) && newName !== autoName;
      // Re-render JUST this row's cell with new state
      let nameCell;
      if (member.user_labeled) {
        nameCell = '<span class="name user-labeled">' + escapeHtml(member.name) + '</span><span class="badge-labeled">USER</span>';
      } else if (autoName) {
        nameCell = '<span class="name">' + escapeHtml(member.name || autoName) + '</span>';
      } else {
        nameCell = '<span class="name unlabeled">(unlabeled)</span>';
      }
      cell.innerHTML = nameCell;
      cell.classList.add('flash-success');
      setTimeout(() => cell.classList.remove('flash-success'), 1000);
    } else {
      cell.innerHTML = originalHTML;
      cell.classList.add('flash-error');
      setTimeout(() => cell.classList.remove('flash-error'), 1000);
      console.error('Save failed:', result?.error || 'unknown');
    }
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      finish(true);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      finish(false);
    }
  });
  input.addEventListener('blur', () => finish(true));
}

function updatePageTitle(data) {
  const tua = data?.stats?.totals?.tua_now || 0;
  const base = 'WRG Monitor';
  document.title = tua > 0 ? '⏳ ' + tua + ' TUA · ' + base : base;
  // Favicon dot — small SVG inline as data URI
  let faviconHref;
  if (tua > 0) {
    // Red dot favicon with count
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
        '<rect width="64" height="64" fill="#e2e8ef"/>' +
        '<circle cx="32" cy="32" r="26" fill="#dc2626"/>' +
        '<text x="32" y="44" text-anchor="middle" font-family="Arial,sans-serif" font-size="34" font-weight="700" fill="#fff">' + tua + '</text>' +
      '</svg>';
    faviconHref = 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
  } else {
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
        '<rect width="64" height="64" fill="#e2e8ef"/>' +
        '<text x="32" y="48" text-anchor="middle" font-size="44">🦞</text>' +
      '</svg>';
    faviconHref = 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
  }
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement('link');
    link.rel = 'icon';
    document.head.appendChild(link);
  }
  link.href = faviconHref;
}

function render(data) {
  updatePageTitle(data);
  renderOverview(data);
  renderRekapPanel(data);
  renderResumePanel(data);

  // Attach collapse handlers
  document.querySelectorAll('.toggle').forEach(btn => {
    btn.addEventListener('click', e => {
      const card = e.target.closest('.card');
      card.classList.toggle('collapsed');
      e.target.textContent = card.classList.contains('collapsed') ? '▸' : '▾';
    });
  });
  // Parsed/raw view switcher
  document.querySelectorAll('.view-toggle').forEach(btn => {
    btn.addEventListener('click', e => {
      const card = btn.closest('.card');
      const mode = btn.dataset.mode;
      card.querySelector('.view-parsed').style.display = mode === 'parsed' ? '' : 'none';
      card.querySelector('.view-raw').style.display = mode === 'raw' ? '' : 'none';
      card.querySelectorAll('.view-toggle').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
    });
  });
  // Resume inner-section tab switcher
  document.querySelectorAll('.section-tab').forEach(btn => {
    btn.addEventListener('click', e => {
      const cardId = btn.dataset.card;
      const sectionNum = btn.dataset.section;
      // Toggle active state for tabs of this card
      document.querySelectorAll('.section-tab[data-card="' + cardId + '"]').forEach(b => {
        b.classList.toggle('active', b.dataset.section === sectionNum);
      });
      // Show/hide panels for this card
      document.querySelectorAll('.section-panel[data-card="' + cardId + '"]').forEach(p => {
        p.style.display = p.dataset.section === sectionNum ? '' : 'none';
      });
    });
  });
}

function captureUIState() {
  // Tab state per card
  const tabs = {};
  document.querySelectorAll('.section-tab.active').forEach(btn => {
    const cardId = btn.dataset.card;
    const sectionId = btn.dataset.section;
    if (cardId && sectionId) tabs[cardId] = sectionId;
  });
  // Collapsed cards (by card body id)
  const collapsed = [];
  document.querySelectorAll('.card.collapsed .card-body').forEach(body => {
    if (body.id) collapsed.push(body.id);
  });
  // View mode toggle (parsed vs raw) per card
  const viewMode = {};
  document.querySelectorAll('.view-toggle.active').forEach(btn => {
    const card = btn.closest('.card');
    const bodyId = card?.querySelector('.card-body')?.id;
    if (bodyId) viewMode[bodyId] = btn.dataset.mode;
  });
  return {
    tabs,
    collapsed,
    viewMode,
    scrollY: window.scrollY,
    // Focus preservation (search input)
    focusedSearch: document.activeElement?.id === 'search-input',
  };
}

function restoreUIState(state) {
  // Tab state — direct DOM manipulation, no click handler re-entry
  for (const [cardId, sectionId] of Object.entries(state.tabs || {})) {
    document.querySelectorAll('.section-tab[data-card="' + cardId + '"]').forEach(b => {
      b.classList.toggle('active', b.dataset.section === sectionId);
    });
    document.querySelectorAll('.section-panel[data-card="' + cardId + '"]').forEach(p => {
      p.style.display = p.dataset.section === sectionId ? '' : 'none';
    });
  }
  // Collapsed cards
  (state.collapsed || []).forEach(bodyId => {
    const body = document.getElementById(bodyId);
    const card = body?.closest('.card');
    if (card) {
      card.classList.add('collapsed');
      const toggle = card.querySelector('.toggle');
      if (toggle) toggle.textContent = '▸';
    }
  });
  // View mode (raw vs parsed)
  for (const [bodyId, mode] of Object.entries(state.viewMode || {})) {
    const body = document.getElementById(bodyId);
    const card = body?.closest('.card');
    if (!card) continue;
    const parsed = card.querySelector('.view-parsed');
    const raw = card.querySelector('.view-raw');
    if (parsed) parsed.style.display = mode === 'parsed' ? '' : 'none';
    if (raw) raw.style.display = mode === 'raw' ? '' : 'none';
    card.querySelectorAll('.view-toggle').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  }
  // Scroll
  if (typeof state.scrollY === 'number') {
    window.scrollTo({ top: state.scrollY, behavior: 'instant' });
  }
  // Refocus search input if it was focused
  if (state.focusedSearch) {
    const inp = document.getElementById('search-input');
    if (inp) {
      const v = inp.value;
      inp.focus();
      // Restore caret position to end
      inp.setSelectionRange(v.length, v.length);
    }
  }
}

async function loadAndRender(date) {
  const uiState = captureUIState();
  const data = await fetchData(date);
  if (data.lan_ip) lanIp = data.lan_ip;
  const picker = document.getElementById('date-picker');
  picker.innerHTML = data.dates.map(d => '<option' + (d === date ? ' selected' : '') + '>' + d + '</option>').join('');
  render(data);
  document.getElementById('last-update').textContent = 'updated ' + new Date().toLocaleTimeString('id-ID');
  // Restore UI state (tabs, collapsed, view mode, scroll, focus) before re-applying search.
  // Use requestAnimationFrame so DOM is settled.
  requestAnimationFrame(() => {
    restoreUIState(uiState);
    // Re-apply current search after restore
    const inp = document.getElementById('search-input');
    if (inp && inp.value) applySearch(inp.value);
  });
}

// Tab labels for topbar title
const TAB_TITLES = {
  overview: 'Overview',
  rekap: 'Rekap',
  resume: 'Resume',
  members: 'Members Directory',
  pola: 'Pola Komunikasi',
  briefing: '📋 Briefing Direktur',
};

function switchTab(name) {
  document.querySelectorAll('nav.tabs button').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.id === 'panel-' + name);
  });
  // Sync topbar title
  const titleEl = document.getElementById('topbar-title');
  if (titleEl) titleEl.textContent = TAB_TITLES[name] || name;
  // Persist tab + date in URL via replaceState (no history pollution)
  syncUrlFromState();
  // Search bar visible only on Rekap/Resume
  const searchWrap = document.getElementById('search-wrap');
  if (searchWrap) {
    searchWrap.style.display = (name === 'rekap' || name === 'resume') ? '' : 'none';
    if (name !== 'rekap' && name !== 'resume') {
      // Reset search when switching away from rekap/resume
      const inp = document.getElementById('search-input');
      if (inp && inp.value) { inp.value = ''; applySearch(''); }
    } else {
      // Re-apply on tab change (cards may have changed)
      const inp = document.getElementById('search-input');
      if (inp) applySearch(inp.value);
    }
  }
  // Load members if switching to Members tab
  if (name === 'members') {
    loadMembers();
  }
  if (name === 'pola') {
    loadPola();
  }
  if (name === 'briefing') {
    loadBriefings();
  }
}

function highlightTextNodes(root, query) {
  // Remove old <mark> wrappers first
  root.querySelectorAll('mark.hit').forEach(m => {
    const txt = document.createTextNode(m.textContent);
    m.parentNode.replaceChild(txt, m);
  });
  // Normalize adjacent text nodes after unwrapping
  root.normalize();
  if (!query) return;
  const re = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => {
      if (!n.nodeValue || !re.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
      // Skip text inside <script>, <style>, and our own <mark>
      const p = n.parentNode;
      if (!p) return NodeFilter.FILTER_REJECT;
      const tag = p.nodeName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'MARK') return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const toReplace = [];
  while (walker.nextNode()) toReplace.push(walker.currentNode);
  toReplace.forEach(node => {
    const html = node.nodeValue.replace(re, '<mark class="hit">$1</mark>');
    const tmp = document.createElement('span');
    tmp.innerHTML = html;
    while (tmp.firstChild) node.parentNode.insertBefore(tmp.firstChild, node);
    node.parentNode.removeChild(node);
  });
}

let currentMatchIndex = 0;

function getAllMatches() {
  const activePanel = document.querySelector('.tab-panel.active');
  if (!activePanel) return [];
  return Array.from(activePanel.querySelectorAll('.card:not(.search-hidden) mark.hit'));
}

function scrollToMark(mark) {
  if (!mark) return;
  // If inside a hidden section-panel, switch to its tab
  const panel = mark.closest('.section-panel');
  if (panel && panel.dataset.section && getComputedStyle(panel).display === 'none') {
    const tabBtn = document.querySelector('.section-tab[data-card="' + panel.dataset.card + '"][data-section="' + panel.dataset.section + '"]');
    if (tabBtn) tabBtn.click();
  }
  // Uncollapse the containing card if collapsed
  const card = mark.closest('.card');
  if (card && card.classList.contains('collapsed')) {
    card.classList.remove('collapsed');
    const toggle = card.querySelector('.toggle');
    if (toggle) toggle.textContent = '▾';
  }
  // Clear previous 'current' marker
  document.querySelectorAll('mark.hit.mark-current').forEach(m => m.classList.remove('mark-current'));
  mark.classList.add('mark-current');
  // Defer scroll until DOM/layout updates settle
  requestAnimationFrame(() => {
    mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
}

function updateSearchNavButtons(matches) {
  const prev = document.getElementById('search-prev');
  const next = document.getElementById('search-next');
  if (prev) prev.disabled = matches.length === 0;
  if (next) next.disabled = matches.length === 0;
}

function jumpToMatch(direction) {
  const matches = getAllMatches();
  if (!matches.length) {
    updateSearchNavButtons(matches);
    return;
  }
  if (direction !== 0) {
    currentMatchIndex = (currentMatchIndex + direction + matches.length) % matches.length;
  } else {
    // direction 0 = jump to current (or first if out of range)
    if (currentMatchIndex >= matches.length) currentMatchIndex = 0;
  }
  scrollToMark(matches[currentMatchIndex]);
  const stat = document.getElementById('search-stat');
  if (stat) stat.textContent = (currentMatchIndex + 1) + '/' + matches.length + ' match';
  updateSearchNavButtons(matches);
}

function applySearch(rawQuery) {
  const query = (rawQuery || '').trim();
  const wrap = document.getElementById('search-wrap');
  if (wrap) wrap.classList.toggle('has-query', query.length > 0);
  const activePanel = document.querySelector('.tab-panel.active');
  if (!activePanel || !['panel-rekap', 'panel-resume'].includes(activePanel.id)) {
    document.getElementById('search-stat').textContent = '';
    updateSearchNavButtons([]);
    return;
  }
  let total = 0, hits = 0;
  activePanel.querySelectorAll('.card').forEach(card => {
    total++;
    const txt = card.textContent.toLowerCase();
    const matches = !query || txt.includes(query.toLowerCase());
    card.classList.toggle('search-hidden', !matches);
    if (matches) hits++;
    highlightTextNodes(card, matches ? query : '');
  });
  const stat = document.getElementById('search-stat');
  if (stat) stat.textContent = query ? hits + '/' + total + ' match (card)' : '';
  currentMatchIndex = 0;
  if (query) {
    requestAnimationFrame(() => jumpToMatch(0));
  } else {
    updateSearchNavButtons([]);
  }
}

// Sync URL bar with currentDate + active tab. Clean URL for defaults
// (today + overview) — keeps shareable links short.
function syncUrlFromState() {
  const today = new Date().toISOString().slice(0, 10);
  const activeBtn = document.querySelector('nav.tabs button.active');
  const tab = activeBtn ? activeBtn.dataset.tab : 'overview';
  const params = new URLSearchParams();
  if (currentDate && currentDate !== today) params.set('date', currentDate);
  const qs = params.toString() ? '?' + params.toString() : '';
  const hash = (tab && tab !== 'overview') ? '#' + tab : '';
  history.replaceState(null, '', location.pathname + qs + hash);
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
}

function start() {
  // Apply persisted theme + sidebar state EARLY (before render to avoid flash)
  const savedTheme = localStorage.getItem('wrg.theme') || 'light';
  applyTheme(savedTheme);
  const savedCollapsed = localStorage.getItem('wrg.sidebar.collapsed') === '1';
  if (savedCollapsed) document.querySelector('.app-shell').classList.add('sidebar-collapsed');

  // Parse initial state from URL: ?date=YYYY-MM-DD overrides today
  const today = new Date().toISOString().slice(0, 10);
  const urlParams = new URLSearchParams(location.search);
  const urlDate = urlParams.get('date');
  const validDateRe = /^\d{4}-\d{2}-\d{2}$/;
  currentDate = (urlDate && validDateRe.test(urlDate)) ? urlDate : today;
  loadAndRender(currentDate);

  // Theme toggle
  const themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) themeBtn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    localStorage.setItem('wrg.theme', next);
  });
  // Sidebar collapse
  const sbToggle = document.getElementById('sidebar-toggle');
  if (sbToggle) sbToggle.addEventListener('click', () => {
    const shell = document.querySelector('.app-shell');
    shell.classList.toggle('sidebar-collapsed');
    localStorage.setItem('wrg.sidebar.collapsed', shell.classList.contains('sidebar-collapsed') ? '1' : '0');
  });

  document.getElementById('date-picker').addEventListener('change', e => {
    currentDate = e.target.value;
    syncUrlFromState();
    loadAndRender(currentDate);
  });
  document.getElementById('refresh-btn').addEventListener('click', () => loadAndRender(currentDate));

  document.querySelectorAll('nav.tabs button').forEach(b => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });

  // Search input
  const searchInput = document.getElementById('search-input');
  const searchClear = document.getElementById('search-clear');
  let searchTimer = null;
  if (searchInput) {
    searchInput.addEventListener('input', e => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => applySearch(e.target.value), 200);
    });
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        e.target.value = '';
        applySearch('');
      } else if (e.key === 'Enter') {
        e.preventDefault();
        jumpToMatch(e.shiftKey ? -1 : 1);
      }
    });
  }
  if (searchClear) {
    searchClear.addEventListener('click', () => {
      if (searchInput) { searchInput.value = ''; searchInput.focus(); }
      applySearch('');
    });
  }
  const searchPrev = document.getElementById('search-prev');
  const searchNext = document.getElementById('search-next');
  if (searchPrev) searchPrev.addEventListener('click', () => jumpToMatch(-1));
  if (searchNext) searchNext.addEventListener('click', () => jumpToMatch(1));

  // QR modal
  const qrBtn = document.getElementById('qr-btn');
  const qrModal = document.getElementById('qr-modal');
  const qrClose = document.getElementById('qr-close');
  if (qrBtn) qrBtn.addEventListener('click', showQrModal);
  if (qrClose) qrClose.addEventListener('click', hideQrModal);
  if (qrModal) qrModal.querySelector('.qr-backdrop').addEventListener('click', hideQrModal);
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (qrModal && qrModal.style.display !== 'none') hideQrModal();
    const mm = document.getElementById('member-modal');
    if (mm && mm.style.display !== 'none') hideMemberModal();
  });

  // Initial tab from URL hash
  const initialTab = (location.hash || '#overview').slice(1);
  if (['overview', 'rekap', 'resume', 'members', 'pola', 'briefing'].includes(initialTab)) {
    switchTab(initialTab);
  }

  // Auto-refresh
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => loadAndRender(currentDate), 60000);
}

start();
</script>
</body>
</html>
"""


def update_group_subject(jid: str, subject: str) -> tuple[bool, str]:
    """Atomic update: data/members.json → group_directory_user[jid] = subject.
    Manual group rename. Stored separately from auto group_directory so
    list_members.sh re-runs preserve user overrides."""
    if not MEMBERS_FILE.exists():
        return False, "members.json not found"
    try:
        with open(MEMBERS_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"failed to read: {e}"
    user_map = data.get("group_directory_user", {}) or {}
    s = subject.strip()
    if s:
        user_map[jid] = s
    else:
        user_map.pop(jid, None)
    data["group_directory_user"] = user_map
    data["last_user_edit"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = MEMBERS_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(MEMBERS_FILE)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, f"failed to write: {e}"
    return True, ""


def update_member_name(phone: str, name: str | None = None, notes: str | None = None) -> tuple[bool, str]:
    """Atomic update: data/members.json → set member.name and/or notes.
    Returns (ok, error_message)."""
    if not MEMBERS_FILE.exists():
        return False, "members.json not found"
    try:
        with open(MEMBERS_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"failed to read: {e}"
    updated = False
    for m in data.get("members", []):
        if m.get("phone") == phone:
            if name is not None:
                name_s = name.strip()
                m["name"] = name_s
                auto = m.get("auto_name", "")
                m["user_labeled"] = bool(name_s) and name_s != auto
            if notes is not None:
                m["notes"] = notes  # preserve formatting incl. newlines
            updated = True
            break
    if not updated:
        return False, "phone not found in members.json"
    data["last_user_edit"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = MEMBERS_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(MEMBERS_FILE)
    except Exception as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False, f"failed to write: {e}"
    return True, ""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/members/update":
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(body) if body else {}
                phone = (payload.get("phone") or "").strip()
                # name and notes are both optional; partial updates supported
                name = payload.get("name") if "name" in payload else None
                notes = payload.get("notes") if "notes" in payload else None
                if not phone:
                    return self._send(400, "application/json", b'{"ok":false,"error":"phone required"}')
                if name is None and notes is None:
                    return self._send(400, "application/json", b'{"ok":false,"error":"name or notes required"}')
                ok, err = update_member_name(phone, name, notes)
                if ok:
                    return self._send(200, "application/json", b'{"ok":true}')
                return self._send(400, "application/json", json.dumps({"ok": False, "error": err}).encode())
            except json.JSONDecodeError:
                return self._send(400, "application/json", b'{"ok":false,"error":"invalid JSON"}')
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        if parsed.path == "/api/groups/update":
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(body) if body else {}
                jid = (payload.get("jid") or "").strip()
                subject = payload.get("subject", "")
                if not jid:
                    return self._send(400, "application/json", b'{"ok":false,"error":"jid required"}')
                ok, err = update_group_subject(jid, subject)
                if ok:
                    return self._send(200, "application/json", b'{"ok":true}')
                return self._send(400, "application/json", json.dumps({"ok": False, "error": err}).encode())
            except json.JSONDecodeError:
                return self._send(400, "application/json", b'{"ok":false,"error":"invalid JSON"}')
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        return self._send(404, "text/plain", b"not found")

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            return self._send(200, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8"))
        # Static asset serving (logos, etc.) — safe path resolution, no traversal
        if parsed.path.startswith("/assets/"):
            rel = parsed.path[len("/assets/"):]
            # Resolve + verify it's still under ASSETS_DIR (no .. traversal)
            try:
                target = (ASSETS_DIR / rel).resolve()
                if not str(target).startswith(str(ASSETS_DIR.resolve())) or not target.is_file():
                    return self._send(404, "text/plain", b"not found")
                mime = _MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream")
                with open(target, "rb") as f:
                    body = f.read()
                return self._send(200, mime, body)
            except (OSError, ValueError):
                return self._send(404, "text/plain", b"not found")
        if parsed.path == "/api/data":
            qs = parse_qs(parsed.query)
            date = (qs.get("date") or [datetime.date.today().isoformat()])[0]
            if not DATE_RE.match(date):
                return self._send(400, "text/plain", b"bad date")
            body = json.dumps(fetch_data(date)).encode("utf-8")
            return self._send(200, "application/json", body)
        if parsed.path == "/api/dates":
            body = json.dumps({"dates": list_dates()}).encode("utf-8")
            return self._send(200, "application/json", body)
        if parsed.path == "/api/members":
            try:
                with open(MEMBERS_FILE, "r") as f:
                    body = f.read().encode("utf-8")
                return self._send(200, "application/json", body)
            except FileNotFoundError:
                return self._send(200, "application/json", b'{"members":[],"sender_names_from_messages":[]}')
        if parsed.path == "/api/briefings":
            # List all briefing files + content of latest (or requested file via ?file=...)
            qs = parse_qs(parsed.query)
            req_file = (qs.get("file") or [""])[0]
            files = []
            if BRIEFING_DIR.is_dir():
                for f in sorted(BRIEFING_DIR.glob("briefing_*.txt"), reverse=True):
                    m = re.match(r"briefing_(\d{4}-\d{2}-\d{2})_(\d{4})\.txt$", f.name)
                    if m:
                        files.append({
                            "filename": f.name,
                            "date": m.group(1),
                            "time": m.group(2)[:2] + ":" + m.group(2)[2:],
                            "size": f.stat().st_size,
                            "mtime": int(f.stat().st_mtime),
                        })
            current = None
            target_name = req_file if req_file else (files[0]["filename"] if files else "")
            if target_name and (BRIEFING_DIR / target_name).is_file():
                p = BRIEFING_DIR / target_name
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                    current = {
                        "filename": p.name,
                        "raw": content,
                        "parsed": parse_briefing_structured(content),
                        "size": p.stat().st_size,
                    }
                except OSError:
                    pass
            body = json.dumps({"files": files, "current": current}).encode("utf-8")
            return self._send(200, "application/json", body)
        if parsed.path == "/api/pola":
            group_auto = {}
            group_user = {}
            try:
                with open(MEMBERS_FILE, "r") as mf:
                    mdata = json.load(mf)
                    group_auto = mdata.get("group_directory", {}) or {}
                    group_user = mdata.get("group_directory_user", {}) or {}
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            out = []
            if POLA_DIR.is_dir():
                for f in sorted(POLA_DIR.glob("*.md")):
                    jid = f.stem
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    user_subj = group_user.get(jid, "")
                    auto_subj = group_auto.get(jid, "")
                    out.append({
                        "jid": jid,
                        "subject": user_subj or auto_subj,
                        "subject_user_labeled": bool(user_subj),
                        "mtime": f.stat().st_mtime,
                        "size": f.stat().st_size,
                        "content": content,
                        "parsed": parse_pola_markdown(content),
                    })
            out.sort(key=lambda x: -x["mtime"])
            body = json.dumps({"pola": out}).encode("utf-8")
            return self._send(200, "application/json", body)
        return self._send(404, "text/plain", b"not found")

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Silence default access logs; uncomment for debugging.
        pass


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    """Threaded — satu client lambat / socket idle gak boleh block client lain.
    daemon_threads=True supaya pending request gak nahan shutdown."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--bind", default="127.0.0.1")
    args = p.parse_args()

    with ReusableTCPServer((args.bind, args.port), Handler) as srv:
        url = f"http://{args.bind}:{args.port}/"
        print(f"WRG Monitor Dashboard listening at {url}")
        print(f"  PROJECT_DIR  = {PROJECT_DIR}")
        print(f"  REKAP_DIR    = {REKAP_DIR}")
        print(f"  RESUME_DIR   = {RESUME_DIR}")
        print(f"  Open the URL in your browser. Press Ctrl-C to stop.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nshutdown")


if __name__ == "__main__":
    main()
