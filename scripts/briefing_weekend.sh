#!/bin/bash
# ============================================================
# WRG Monitor — Briefing Weekend untuk Meeting Direktur
# Jalankan manual  : bash briefing_weekend.sh
# Atau otomatis via cron: Sabtu & Minggu jam 07:00
# ============================================================

source "$(dirname "$0")/../config/config.sh"

TANGGAL=$(date '+%Y-%m-%d')
TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
# Explicit 7-day date range. Sebelumnya "Minggu $(date '+%V')" (ISO week number)
# ambigu di B.Indonesia — AI bisa baca "Minggu" sebagai nama hari & hallucinate
# range (e.g. "Minggu 21–22, May 2026"). Pakai date range nyata lebih clear.
if date --version &>/dev/null 2>&1; then
  WEEK_START=$(date -d "6 days ago" '+%-d %B %Y')   # Linux
else
  WEEK_START=$(date -v-6d '+%-d %B %Y')             # macOS
fi
WEEK_END=$(date '+%-d %B %Y')
MINGGU_LABEL="Briefing Mingguan: ${WEEK_START} – ${WEEK_END}"

mkdir -p "$LOG_DIR" "$DATA_DIR/briefing"

log() {
  echo "[$(date '+%H:%M:%S')] [briefing] $1" | tee -a "$LOG_DIR/wrg-monitor.log"
}

# ── Kumpulkan resume 7 hari terakhir ─────────────────────────
kumpulkan_resume_minggu() {
  SEMUA=""
  JUMLAH=0
  for i in {0..6}; do
    if date --version &>/dev/null 2>&1; then
      TGL=$(date -d "-${i} days" '+%Y-%m-%d')    # Linux
    else
      TGL=$(date -v-${i}d '+%Y-%m-%d')           # macOS
    fi
    DIR_RESUME="$DATA_DIR/resume/$TGL"
    if ls "$DIR_RESUME"/resume_*.txt &>/dev/null; then
      SEMUA="${SEMUA}\n\n====== ${TGL} ======\n$(cat "$DIR_RESUME"/resume_*.txt)"
      JUMLAH=$((JUMLAH+1))
    fi
  done
  echo -e "$SEMUA"
  log "Resume tersedia: ${JUMLAH} hari"
}

RESUME_MINGGU=$(kumpulkan_resume_minggu)

if [ -z "$(echo "$RESUME_MINGGU" | tr -d '[:space:]')" ]; then
  log "Tidak ada data resume minggu ini. Briefing dibatalkan."
  exit 0
fi

# ── Inject pola profiles untuk semua grup yang punya profile ─────────────
# data/pola/*.md di-generate nightly oleh pola_komunikasi.sh untuk grup
# dengan ≥5 pesan dalam 7 hari — universe yang sama dengan briefing weekend.
POLA_CONTEXT=""
POLA_COUNT=0
if [ -d "$DATA_DIR/pola" ]; then
  for POLA_FILE in "$DATA_DIR/pola"/*.md; do
    [ -f "$POLA_FILE" ] || continue
    JID=$(basename "$POLA_FILE" .md)
    POLA_CONTEXT="${POLA_CONTEXT}

=== POLA GRUP: ${JID} ===
$(cat "$POLA_FILE")
"
    POLA_COUNT=$((POLA_COUNT + 1))
  done
fi
if [ "$POLA_COUNT" -gt 0 ]; then
  log "Inject pola profile untuk ${POLA_COUNT} grup"
fi

# Member directory — substitusi nomor → nama
MEMBERS_TABLE=""
MEMBERS_COUNT=0
GROUP_DIRECTORY=""
GROUP_DIR_COUNT=0
if [ -f "$DATA_DIR/members.json" ]; then
  MEMBERS_TABLE=$(jq -r '.members[] | select(.name != null and .name != "") | "• \(.phone) → \(.name)"' "$DATA_DIR/members.json" 2>/dev/null)
  MEMBERS_COUNT=$(echo "$MEMBERS_TABLE" | grep -c "^•" 2>/dev/null || echo 0)
  [ "$MEMBERS_COUNT" -gt 0 ] && log "Inject directory ${MEMBERS_COUNT} labeled members"

  GROUP_DIRECTORY=$(jq -r '
    ((.group_directory_user // {}) as $u
    | (.group_directory // {}) as $a
    | ($a + $u)
    | to_entries[]
    | select(.value != "" and .value != null)
    | "• \(.key) → \(.value)")
  ' "$DATA_DIR/members.json" 2>/dev/null)
  GROUP_DIR_COUNT=$(echo "$GROUP_DIRECTORY" | grep -c "^•" 2>/dev/null || echo 0)
  [ "$GROUP_DIR_COUNT" -gt 0 ] && log "Inject directory ${GROUP_DIR_COUNT} named groups"
fi

# Cache-friendly: stable prefix (members + pola + format spec) di atas, RESUME_MINGGU (variable) di akhir.
PROMPT="Kamu adalah asisten eksekutif senior ${NAMA_PERUSAHAAN}.

Konteks: ${KONTEKS_BISNIS}

============================================
DIREKTORI GRUP (substitusi JID grup → nama grup saat menyebut sumber info di briefing:
kalau JID di resume muncul di list ini, GANTI dengan nama grup; kalau tidak ada,
pakai JID apa adanya):
${GROUP_DIRECTORY:-(belum ada group_directory)}

============================================
DIREKTORI MEMBER (substitusi nomor telpon ke nama saat output briefing direktur:
kalau nomor di rekap/resume muncul di list ini, GANTI dengan nama; kalau tidak ada
di list, pakai nomor apa adanya — JANGAN tebak nama):
${MEMBERS_TABLE:-(belum ada members.json)}

============================================
PROFILE POLA KOMUNIKASI per-grup (panduan ekstraksi info per grup; dipakai untuk
memprioritaskan topik yang relevan ke direktur — misal grup sales beda perlakuan
dari grup logistik):
${POLA_CONTEXT:-(belum ada profile pola)}

============================================
Tugas: Buat BRIEFING KOMPREHENSIF dan TERSTRUKTUR untuk sesi meeting ${NAMA_DIREKTUR} di akhir pekan.
Briefing harus cukup lengkap sehingga direktur bisa langsung berdiskusi tanpa perlu membaca chat mentah.

Format output:

BRIEFING DIREKTUR — ${NAMA_PERUSAHAAN}
${MINGGU_LABEL}
Disiapkan: ${TANGGAL}
============================================

A. RINGKASAN EKSEKUTIF
[4-5 kalimat: situasi bisnis minggu ini, tone keseluruhan, highlight utama]

============================================
B. UPDATE SALES & PIPELINE

Prospek Baru Minggu Ini:
• [nama prospek/klien] — [tahap] — [PIC]

Deal yang Maju:
• [detail]

Deal yang Perlu Perhatian / Stuck:
• [detail + alasan + rekomendasi aksi]

Target vs Aktual (jika ada data dari chat):
• [angka/estimasi yang disebut di percakapan]

============================================
C. OPERASIONAL

Koordinasi Berjalan Baik:
• [item]

Bottleneck / Kendala:
• [masalah] — [sudah/belum resolved] — [butuh eskalasi?]

============================================
D. ACTION ITEMS CARRY-OVER
(belum selesai dari minggu ini, perlu dipantau)

• [PIC] → [tugas] | Deadline: [waktu] | Status: [on track/at risk/terlambat]

============================================
E. AGENDA MEETING DENGAN ${NAMA_DIREKTUR}
(urut dari prioritas tertinggi)

1. [TOPIK]
   Konteks: [2-3 kalimat latar belakang]
   Data/fakta: [angka, nama, timeline yang relevan]
   Butuh dari direktur: [keputusan / arahan / informasi / eskalasi]

2. [TOPIK berikutnya]
   ...

============================================
F. PROYEKSI & ANTISIPASI MINGGU DEPAN
• [hal yang perlu disiapkan atau diantisipasi]

============================================
Generated otomatis oleh WRG Monitor
${TANGGAL} | Data dari 30 grup WhatsApp

============================================
DATA INPUT (paling akhir — stable prefix di atas bisa di-cache provider):

Berikut adalah resume harian dari grup WhatsApp tim sales & operasional WRG selama seminggu terakhir (${MINGGU_LABEL}):

${RESUME_MINGGU}"

log "Membuat briefing weekend..."
BOT_NOMOR="+6285168121906"
HASIL=$(call_ai_with_fallback "$BRIEFING_MODEL_PRIMARY" "$BRIEFING_MODEL_FALLBACK" "$PROMPT" "$THINKING_BRIEFING")

if [ -z "$HASIL" ]; then
  log "ERROR: Tidak ada output (primary+fallback gagal). Cek error.log"
  bash "$(dirname "$0")/notif_quota.sh" >/dev/null 2>&1 &
  exit 1
fi

# Force-correct the date line below "BRIEFING DIREKTUR" title — AI sometimes
# rewrites $MINGGU_LABEL with hallucinated content (e.g. adds date ranges).
# Script knows the real label; overwrite line right after BRIEFING DIREKTUR title.
HASIL=$(printf '%s\n' "$HASIL" | awk -v label="$MINGGU_LABEL" '
  /^#?[[:space:]]*BRIEFING DIREKTUR/ { print; getline; print "**" label "**"; next }
  { print }
')

# Defensive: catch any "Minggu N..." or "Minggu N-M, Month Year" phrase AI may
# fabricate anywhere in body (footer, side-comments). Replace with canonical label.
# Word "Minggu" di B.Indonesia ambigu (Week vs Sunday) — AI bias produce ini.
HASIL=$(printf '%s\n' "$HASIL" | sed -E "s/Minggu [0-9]+([–-][0-9]+)?,?[[:space:]]+(January|February|March|April|May|June|July|August|September|October|November|December|Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)[[:space:]]+[0-9]{4}/${MINGGU_LABEL}/g")

OUTPUT_FILE="$DATA_DIR/briefing/briefing_${TIMESTAMP}.txt"
echo "$HASIL" > "$OUTPUT_FILE"

log "Selesai. Disimpan: briefing_${TIMESTAMP}.txt"
log "Path: $OUTPUT_FILE"
echo ""
echo "=== LOKASI FILE ==="
echo "$OUTPUT_FILE"
