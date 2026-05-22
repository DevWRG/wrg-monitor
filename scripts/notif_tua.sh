#!/bin/bash
# ============================================================
# WRG Monitor — Notifikasi item TUA via WhatsApp
# Baca resume terbaru, ekstrak item OUTSTANDING [TUA], push ke
# owner WA number. Idempotent via state file untuk hindari spam.
#
# Mode:
#   bash notif_tua.sh              → kirim notif (real)
#   bash notif_tua.sh --dry-run    → cuma cetak payload, gak kirim
# ============================================================

set -e
source "$(dirname "$0")/../config/config.sh"

OWNER_NUMBER="+6285733048855"
TANGGAL=$(date '+%Y-%m-%d')
JAM=$(date '+%H:%M')
STATE_DIR="$DATA_DIR/state"
STATE_FILE="$STATE_DIR/notified-tua.json"
mkdir -p "$STATE_DIR" "$LOG_DIR"

DRY=""
if [ "$1" = "--dry-run" ]; then DRY="--dry-run"; fi

log() { echo "[$(date '+%H:%M:%S')] [notif-tua] $1" | tee -a "$LOG_DIR/wrg-monitor.log"; }

LATEST_RESUME=$(ls -t "$DATA_DIR/resume/$TANGGAL"/resume_*.txt 2>/dev/null | head -1)
if [ -z "$LATEST_RESUME" ]; then
  log "Tidak ada resume hari ini. Skip."
  exit 0
fi

# Ekstrak section OUTSTANDING (dari header "⏳ OUTSTANDING" sampai section berikutnya / "Generated:")
TUA_RAW=$(awk '
  /^⏳ OUTSTANDING/ { capture=1; next }
  capture && (/^[0-9]+\.\s/ || /^Generated:/ || /^=====/) { exit }
  capture { print }
' "$LATEST_RESUME" | grep '\[TUA\]' || true)

if [ -z "$TUA_RAW" ]; then
  log "Tidak ada item TUA di resume terbaru. Skip."
  # Update state to clear previous (resolved items)
  echo "[]" > "$STATE_FILE"
  exit 0
fi

# Hash signature buat dedupe — topic-only (sebelum '|')
CURRENT_SIG=$(echo "$TUA_RAW" | sed 's/^•[[:space:]]*//; s/[[:space:]]*|.*$//' | sort -u | shasum -a 256 | awk '{print $1}')
PREV_SIG=""
if [ -f "$STATE_FILE" ]; then
  PREV_SIG=$(jq -r '.signature // ""' "$STATE_FILE" 2>/dev/null || echo "")
fi

if [ "$CURRENT_SIG" = "$PREV_SIG" ] && [ -z "$DRY" ]; then
  log "Set item TUA sama dengan notif sebelumnya. Skip (anti-spam)."
  exit 0
fi

COUNT=$(echo "$TUA_RAW" | wc -l | tr -d ' ')
TOP5=$(echo "$TUA_RAW" | head -5 | sed 's/^•[[:space:]]*/• /')

# Build pesan — WhatsApp markdown (* untuk bold)
MSG="*🚨 ${COUNT} Item TUA — Perlu Follow-Up*
_${TANGGAL} ${JAM} WIB | dari Resume Eksekutif_

${TOP5}"

if [ "$COUNT" -gt 5 ]; then
  MSG="${MSG}

_…+$((COUNT - 5)) item lainnya. Lihat dashboard untuk lengkap:_"
else
  MSG="${MSG}

_Detail lengkap di dashboard:_"
fi
MSG="${MSG}
http://127.0.0.1:8090/#resume"

log "Kirim notif ke ${OWNER_NUMBER} (${COUNT} TUA items, dry=${DRY:-no})"

openclaw message send \
  --channel whatsapp \
  --target "$OWNER_NUMBER" \
  --message "$MSG" \
  $DRY \
  2>>"$LOG_DIR/error.log" | tee -a "$LOG_DIR/wrg-monitor.log"

# Save state (only on non-dry-run)
if [ -z "$DRY" ]; then
  printf '{"signature":"%s","sent_at":"%s","count":%s}\n' "$CURRENT_SIG" "$(date -Iseconds)" "$COUNT" > "$STATE_FILE"
fi

log "Selesai."
