#!/bin/bash
# ============================================================
# WRG Monitor — Notif WA kalau OpenRouter quota habis
#
# Dipanggil oleh script AI (rekap/resume/briefing/pola) ketika
# AI return empty output, atau standalone via cron.
# Anti-spam via cooldown 4 jam di data/state/notified-quota.json.
#
# Usage:
#   bash notif_quota.sh              → cek error.log, kirim notif kalau ada
#   bash notif_quota.sh --dry-run    → cetak payload tanpa kirim
#   bash notif_quota.sh --force      → bypass cooldown (manual test)
# ============================================================

source "$(dirname "$0")/../config/config.sh"

OWNER_NUMBER="+6285733048855"
BOT_NUMBER="+6285168121906"
COOLDOWN_HOURS=4
STATE_DIR="$DATA_DIR/state"
STATE_FILE="$STATE_DIR/notified-quota.json"
mkdir -p "$STATE_DIR" "$LOG_DIR"

DRY=""
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
  esac
done

log() { echo "[$(date '+%H:%M:%S')] [notif-quota] $1" | tee -a "$LOG_DIR/wrg-monitor.log"; }

# Cek error.log untuk 403/quota dalam 100 baris terakhir (cover beberapa AI calls baru)
ERROR_LOG="$LOG_DIR/error.log"
if [ ! -f "$ERROR_LOG" ]; then
  log "error.log belum ada. Skip."
  exit 0
fi
RECENT_403=$(tail -100 "$ERROR_LOG" 2>/dev/null | grep -cE "Key limit exceeded|403 .*limit" || true)
RECENT_403=${RECENT_403:-0}
if [ "$RECENT_403" -eq 0 ]; then
  log "Tidak ada quota error baru. Skip."
  exit 0
fi

# Cooldown check (skip kalau --force)
NOW=$(date +%s)
LAST_SENT=$(jq -r '.last_sent_ts // 0' "$STATE_FILE" 2>/dev/null || echo 0)
LAST_SENT=${LAST_SENT:-0}
ELAPSED=$((NOW - LAST_SENT))
COOLDOWN_S=$((COOLDOWN_HOURS * 3600))
if [ -z "$FORCE" ] && [ "$ELAPSED" -lt "$COOLDOWN_S" ]; then
  REMAINING_MIN=$(( (COOLDOWN_S - ELAPSED) / 60 ))
  log "Cooldown aktif ($((ELAPSED/60))m elapsed, ${REMAINING_MIN}m sisa). Skip."
  exit 0
fi

# Last successful rekap age (untuk context di pesan)
LATEST_REKAP=$(ls -t "$DATA_DIR/rekap"/*/rekap_*.txt 2>/dev/null | head -1)
REKAP_INFO="(belum ada rekap sukses)"
if [ -n "$LATEST_REKAP" ]; then
  REKAP_AGE_S=$(( NOW - $(stat -f %m "$LATEST_REKAP") ))
  REKAP_AGE_H=$(( REKAP_AGE_S / 3600 ))
  REKAP_INFO="Rekap terakhir sukses: ${REKAP_AGE_H}j lalu ($(basename "$LATEST_REKAP"))"
fi

MSG="🚨 WRG Monitor — OpenRouter Quota Habis

Monthly limit OpenRouter sudah ke-exceed.
Rekap/resume/briefing tidak bisa jalan sampai topup.

📊 ${REKAP_INFO}
🔥 403 errors di log: ${RECENT_403}/100 baris terakhir
⏰ Detected: $(date '+%H:%M WIB · %d %b')

🔗 Topup: https://openrouter.ai/settings/keys

(Notif anti-spam: next check ${COOLDOWN_HOURS}j lagi)"

if [ -n "$DRY" ]; then
  echo "=== DRY RUN — payload ==="
  echo "to: $OWNER_NUMBER"
  echo "---"
  echo "$MSG"
  exit 0
fi

log "Kirim notif quota ke $OWNER_NUMBER..."
SEND_OUT=$(openclaw message send \
  --channel whatsapp \
  --target "$OWNER_NUMBER" \
  --text "$MSG" 2>&1)
SEND_RC=$?

if [ "$SEND_RC" -ne 0 ]; then
  log "ERROR kirim WA: $SEND_OUT"
  exit 1
fi

# Update state
cat > "$STATE_FILE" <<EOF
{
  "last_sent_ts": $NOW,
  "last_sent_iso": "$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')",
  "recent_403_count": $RECENT_403,
  "cooldown_hours": $COOLDOWN_HOURS
}
EOF
log "✓ Notif terkirim, state updated."
