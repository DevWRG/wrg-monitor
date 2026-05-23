#!/bin/bash
# ============================================================
# WRG Monitor — Pola Komunikasi per Grup (v1)
# Analisis pola komunikasi per grup WhatsApp dari 7 hari terakhir.
# Output: profile markdown per grup di data/pola/<group_jid>.md
# Jadwal: daily 23:30 WIB (lihat setup.sh)
#
# Tujuan:
# - Mengenali jam aktif, top sender, topik dominan, tone, distribusi tipe pesan
# - Profile bisa dipakai sebagai konteks tambahan untuk rekap di iterasi berikut
# ============================================================

# Note: no `set -e` — pola loop should continue past individual group failures
# (mis. OpenRouter quota hit mid-batch). Idempotent re-run lanjut dari yang
# belum punya pola, dan fingerprint check skip yang udah up-to-date.
source "$(dirname "$0")/../config/config.sh"

NOW_S=$(date +%s)
TANGGAL=$(date '+%Y-%m-%d')
TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
BOT_NOMOR="+6285168121906"
MESSAGES_DIR="$DATA_DIR/messages"
POLA_DIR="$DATA_DIR/pola"
WINDOW_DAYS=7
MIN_MESSAGES=5                   # skip groups with < N msgs in window
SAMPLE_MAX=120                   # max sample messages to send to AI

mkdir -p "$POLA_DIR" "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] [pola] $1" | tee -a "$LOG_DIR/wrg-monitor.log"; }

if [ ! -d "$MESSAGES_DIR" ]; then
  log "ERROR: $MESSAGES_DIR not found. Patch openclaw belum aktif?"
  exit 1
fi

# Build list of dates within window (today minus 0..N days)
DATES=()
for i in $(seq 0 $((WINDOW_DAYS - 1))); do
  if date --version &>/dev/null 2>&1; then
    DATES+=("$(date -d "-${i} days" '+%Y-%m-%d')")
  else
    DATES+=("$(date -v-${i}d '+%Y-%m-%d')")
  fi
done

# Collect all JSONL files in window into a bash array (avoids word-splitting bugs)
ALL_FILES=()
for d in "${DATES[@]}"; do
  for f in "$MESSAGES_DIR/$d"/*.jsonl; do
    [ -f "$f" ] && ALL_FILES+=("$f")
  done
done

if [ ${#ALL_FILES[@]} -eq 0 ]; then
  log "Tidak ada data dalam $WINDOW_DAYS hari terakhir. Skip."
  exit 0
fi

log "Membaca ${#ALL_FILES[@]} file pesan…"

# Distinct group_jids active in window.
# NOTE: do NOT name this variable GROUPS — bash has a readonly array GROUPS=(<user gids>),
# and assigning to it is silently ignored, so $GROUPS would return the user's primary GID.
GROUP_JIDS=$(cat "${ALL_FILES[@]}" | jq -r 'select(.chat_type == "group") | .group_jid' | sort -u)
TOTAL_GROUP_JIDS=$(printf "%s\n" "$GROUP_JIDS" | grep -c "@g.us" || true)
log "Found $TOTAL_GROUP_JIDS grup aktif dalam $WINDOW_DAYS hari terakhir"

CUTOFF_S=$((NOW_S - WINDOW_DAYS * 86400))
CUTOFF_MS=$((CUTOFF_S * 1000))

PROCESSED=0
SKIPPED=0
AI_FAILURES=0

while IFS= read -r JID; do
  [ -z "$JID" ] && continue

  # Aggregate this group's messages across the window
  GROUP_MSGS=$(cat "${ALL_FILES[@]}" | jq -c --arg jid "$JID" --argjson since "$CUTOFF_MS" '
    select(.group_jid == $jid and .ts_ms >= $since)
  ')
  COUNT=$(echo "$GROUP_MSGS" | grep -c '^{' || true)

  if [ "$COUNT" -lt "$MIN_MESSAGES" ]; then
    log "  skip $JID ($COUNT msgs < $MIN_MESSAGES threshold)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Fingerprint optimization: skip kalau pola udah ada DAN sedikit/tidak ada
  # pesan baru sejak last pola generation. Threshold: ≥3 new msgs untuk
  # warrant regenerate.
  OUT_EXISTING="$POLA_DIR/${JID}.md"
  NEW_SINCE_POLA_THRESHOLD=3
  if [ -f "$OUT_EXISTING" ]; then
    POLA_MTIME=$(stat -f %m "$OUT_EXISTING")
    POLA_MTIME_MS=$((POLA_MTIME * 1000))
    NEW_COUNT=$(echo "$GROUP_MSGS" | jq --argjson pm "$POLA_MTIME_MS" 'select(.ts_ms > $pm)' | grep -c '^{' || true)
    if [ "$NEW_COUNT" -lt "$NEW_SINCE_POLA_THRESHOLD" ]; then
      log "  skip $JID ($COUNT total / $NEW_COUNT new since last pola — unchanged)"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
    log "  analyzing $JID ($COUNT msgs, $NEW_COUNT new since last pola)..."
  fi

  # Compute stats locally (fast, no LLM)
  STATS=$(echo "$GROUP_MSGS" | jq -s --argjson w "$WINDOW_DAYS" '{
    total: length,
    days_window: $w,
    per_day_avg: ((length | tonumber) / $w | floor),
    top_senders: ([.[] | (.sender_name // .sender)] | group_by(.) | map({sender: .[0], count: length}) | sort_by(-.count) | .[0:5]),
    active_hours: ([.[] | (.ts_ms / 1000 | strftime("%H"))] | group_by(.) | map({hour: .[0], count: length}) | sort_by(-.count) | .[0:8]),
    media_breakdown: ([.[] | .media_type // "text"] | group_by(.) | map({type: .[0], count: length}) | sort_by(-.count)),
    first_ts: (min_by(.ts_ms).ts_ms),
    last_ts: (max_by(.ts_ms).ts_ms)
  }')

  # Sample messages (sorted, chronological), truncated bodies
  SAMPLE=$(echo "$GROUP_MSGS" | jq -s --argjson n "$SAMPLE_MAX" 'sort_by(.ts_ms) | .[-$n:] | map("[\(.ts_ms | tostring)] \(.sender_name // .sender): \(.body[0:200])") | join("\n")' -r 2>/dev/null)

  # "analyzing..." sudah di-log di check fingerprint above (kalau ada existing),
  # kalau pola belum ada (first-time), log di sini.
  if [ ! -f "$OUT_EXISTING" ]; then
    log "  analyzing $JID ($COUNT msgs, first-time)..."
  fi

  # Resolve nama grup dari members.json (user override > auto)
  GROUP_NAME=$(jq -r --arg j "$JID" '
    ((.group_directory_user // {})[$j] // (.group_directory // {})[$j] // "")
  ' "$DATA_DIR/members.json" 2>/dev/null)
  GROUP_LABEL="${GROUP_NAME:+$GROUP_NAME ($JID)}"
  GROUP_LABEL="${GROUP_LABEL:-$JID}"

  PROMPT="Kamu adalah analis komunikasi internal ${NAMA_PERUSAHAAN}.
Konteks: ${KONTEKS_BISNIS}

Berikut data per grup WhatsApp \"$GROUP_LABEL\" dari $WINDOW_DAYS hari terakhir:

Statistik (JSON):
$STATS

Sample pesan terakhir (urut waktu, body dipotong 200 char):
$SAMPLE

Buat PROFILE POLA KOMUNIKASI grup ini. Format markdown:

# Pola Komunikasi: $GROUP_LABEL

## Identitas Grup
- Nama: ${GROUP_NAME:-(tebak dari subject/sample, atau '?')}
- Tipe: [internal sales / lapangan / customer / vendor / dll]
- Total pesan ($WINDOW_DAYS hari): $COUNT
- Generated: $TIMESTAMP

## Jam Aktif
[2-3 kalimat tentang pola jam, peak hours]

## Top Senders
[Top 5 sender dan kontribusi mereka]

## Topik Dominan
[3-5 topik utama yang dibahas, dengan contoh]

## Tone & Style Komunikasi
[Formal/casual? Bahasa? Pakai jargon teknis? Banyak emoji?]

## Distribusi Tipe Pesan
[Text vs image vs PDF vs dokumen — bagaimana dominasinya]

## Karakter Khusus / Pola Operasional
[Misal: rutin lapor harian, koordinasi pengiriman, tagihan, dll]

## Rekomendasi untuk Rekap AI
[3-5 bullet: hal apa yang HARUS di-flag asisten saat bikin rekap grup ini. Misal:
  - Selalu ekstrak nomor PO/faktur jika disebut
  - Tag urgensi berdasarkan keyword X/Y/Z
  - Bedakan info FYI vs request action
  - dll, spesifik untuk grup ini]

============================================
Output: SELESAI"

  OUT="$POLA_DIR/${JID}.md"
  HASIL=$(call_ai_with_fallback "$POLA_MODEL_PRIMARY" "$POLA_MODEL_FALLBACK" "$PROMPT" "$THINKING_BRIEFING")
  if [ -z "$HASIL" ]; then
    log "  ✗ AI no-output for $JID"
    AI_FAILURES=$((AI_FAILURES + 1))
    continue
  fi
  # Force-correct "Generated:" line — AI tends to hallucinate the date even
  # though prompt has the real $TIMESTAMP expanded (treats it as placeholder
  # to "reinvent"). Script knows the real time; overwrite any AI value.
  HASIL=$(printf '%s\n' "$HASIL" | sed -E "s/^- Generated: .*$/- Generated: $TIMESTAMP/")
  echo "$HASIL" > "$OUT"
  PROCESSED=$((PROCESSED + 1))
  log "  ✓ saved $OUT"
done <<< "$GROUP_JIDS"

log "Done. Profiled $PROCESSED grup, skipped $SKIPPED (under threshold). Output: $POLA_DIR"

# Trigger quota check kalau banyak failures (≥3 berturut-turut indikasi quota habis,
# bukan grup-specific issue). Anti-spam via cooldown di notif_quota.sh.
if [ "${AI_FAILURES:-0}" -ge 3 ]; then
  log "$AI_FAILURES AI failures terdeteksi → cek quota..."
  bash "$(dirname "$0")/notif_quota.sh" >/dev/null 2>&1 &
fi
