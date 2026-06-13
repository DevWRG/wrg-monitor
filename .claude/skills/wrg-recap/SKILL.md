---
name: wrg-recap
description: Recap eksekutif Bahasa Indonesia dari sesi OpenClaw wrg-monitor tanpa perlu tahu path/sessionId — pilih sesi (terbaru atau per tanggal), parse window per-grup, lalu sintesis TL;DR/Sentimen/Highlight per-grup/Keputusan/Action Items/Isu Terbuka. Use when di proyek wrg-monitor user minta "rekap WRG", "recap grup hari ini/tanggal X", "resume WhatsApp WRG", atau invoke /wrg-recap.
---

# WRG Recap (project)

Pembungkus proyek untuk skill `whatsapp-recap`: tahu lokasi sesi OpenClaw wrg-monitor
(`~/.openclaw/agents/main/sessions/`) sehingga pemanggil tidak perlu path/sessionId.

## Alur

1. **Pilih & parse sesi** (deterministik) dengan resolver:
   ```
   bash .claude/skills/wrg-recap/scripts/resolve_session.sh [latest | YYYY-MM-DD | <sessionId>] [arg parser...]
   bash .claude/skills/wrg-recap/scripts/resolve_session.sh --list      # lihat sesi yang ada
   ```
   - Default `latest` = sesi mtime terbaru. **Untuk rekap satu hari tertentu, pakai tanggal**
     (`YYYY-MM-DD`) — lebih andal daripada `latest`, yang bisa mengarah ke sesi non-rekap.
   - Resolver memanggil parser `whatsapp-recap` dengan `--format wrg` (pecah window
     `--- TGL JAM ---` + `**Grup** (jid@g.us)` + `• item` jadi pesan per-grup, nama grup
     ter-resolve, window historis ter-dedup). Argumen tambahan diteruskan ke parser
     (mis. `--since/--until`, `--out json`).

2. **Baca** output ternormalisasi (markdown per tanggal, label `[Grup · pengirim]`).

3. **Sintesis recap** Bahasa Indonesia dengan struktur:
   `# Recap WhatsApp WRG — <tanggal>` lalu seksi
   **TL;DR · Sentimen · Highlight per-grup · Keputusan · Action Items (Tugas|PIC|Tenggat) · Isu Terbuka**.
   Detail aturan & contoh ada di skill `whatsapp-recap`
   (`~/.claude/skills/whatsapp-recap/SKILL.md`).

## Aturan

- **Jangan mengarang** PIC/tenggat — tulis `—` bila tidak eksplisit. Sentimen harus berbasis isi.
- Pengirim per-item TIDAK tersedia dari window wrg (sudah ter-digest) — jangan kaitkan ucapan
  ke nama orang kecuali jelas dari teks.
- Butuh skill `whatsapp-recap` ter-install (resolver mengeceknya). Override path/agent via env
  `WRG_AGENT`, `WRG_SESSIONS`, `WRG_PARSER`.

## Contoh

```
bash .claude/skills/wrg-recap/scripts/resolve_session.sh 2026-05-17
# -> transkrip per-grup hari itu; agen menulis recap eksekutif sesuai struktur di atas.
```
