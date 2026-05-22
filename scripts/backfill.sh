#!/bin/bash
# ============================================================
# WRG Monitor — Backfill rekap & resume untuk tanggal yang ke-skip
# (mis. karena cron TCC-blocked atau OpenRouter limit)
#
# Usage:
#   bash backfill.sh 2026-05-14
#   bash backfill.sh 2026-05-13 2026-05-14
#   bash backfill.sh --dry-run 2026-05-14       (preview, no AI call)
#   bash backfill.sh --skip-rekap 2026-05-14    (cuma resume)
#
# Strategi:
#   Untuk tiap tanggal yang dikasih, fire rekap pada slot 07/10/13/16/19/22 WIB,
#   lalu resume pada slot 14/21 WIB — mengikuti jadwal cron normal.
#   Idempotent: kalau output file untuk slot tertentu sudah ada (>0 bytes),
#   slot itu di-skip.
# ============================================================

# Note: no `set -e` — backfill should continue past individual slot failures
# (mis. OpenRouter quota hit di tengah loop). Idempotent re-run lanjut dari
# slot yang belum punya output file.
source "$(dirname "$0")/../config/config.sh"

SCRIPT_DIR="$(dirname "$0")"
DRY_RUN=""
SKIP_REKAP=""
SKIP_RESUME=""
DATES=()

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-rekap) SKIP_REKAP=1 ;;
    --skip-resume) SKIP_RESUME=1 ;;
    --help|-h)
      sed -n '4,12p' "$0"
      exit 0
      ;;
    *) DATES+=("$arg") ;;
  esac
done

if [ ${#DATES[@]} -eq 0 ]; then
  echo "Usage: $0 [--dry-run] [--skip-rekap|--skip-resume] YYYY-MM-DD [YYYY-MM-DD ...]" >&2
  exit 1
fi

REKAP_SLOTS=(07:00 12:00 17:00 22:00)
RESUME_SLOTS=(14:00 22:10)

log() { printf "[%s] [backfill] %s\n" "$(date '+%H:%M:%S')" "$*"; }
ts_to_filename() { echo "$1" | tr -d ':'; }

total_calls=0
total_skipped=0
total_no_data=0
total_future=0

NOW_EPOCH=$(date +%s)

# Helper — convert "YYYY-MM-DD HH:MM" → epoch (BSD date / GNU fallback)
slot_to_epoch() {
  local DATE_HM="$1"
  date -j -f "%Y-%m-%d %H:%M" "$DATE_HM" "+%s" 2>/dev/null \
    || date -d "$DATE_HM" "+%s"
}

for D in "${DATES[@]}"; do
  log "──── $D ────"

  MSG_DIR="$DATA_DIR/messages/$D"
  if [ ! -d "$MSG_DIR" ]; then
    log "  no captured messages for $D, skip entire date"
    continue
  fi
  MSG_COUNT=$(cat "$MSG_DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
  log "  captured: $MSG_COUNT messages across $(ls "$MSG_DIR" | wc -l | tr -d ' ') groups"

  mkdir -p "$DATA_DIR/rekap/$D" "$DATA_DIR/resume/$D"

  if [ -z "$SKIP_REKAP" ]; then
    for slot in "${REKAP_SLOTS[@]}"; do
      FN=$(ts_to_filename "$slot")
      OUT="$DATA_DIR/rekap/$D/rekap_${D}_${FN}.txt"
      if [ -f "$OUT" ] && [ -s "$OUT" ]; then
        log "  rekap $slot — already exists, skip"
        total_skipped=$((total_skipped + 1))
        continue
      fi
      SLOT_EPOCH=$(slot_to_epoch "$D $slot")
      if [ "$SLOT_EPOCH" -gt "$NOW_EPOCH" ]; then
        log "  rekap $slot — in the future, skip (cron will fire naturally)"
        total_future=$((total_future + 1))
        continue
      fi
      log "  rekap $slot — running..."
      if [ -n "$DRY_RUN" ]; then
        echo "    [DRY] WRG_FAKE_NOW=\"$D $slot\" bash $SCRIPT_DIR/rekap.sh rekap"
      else
        OUTPUT=$(WRG_FAKE_NOW="$D $slot" bash "$SCRIPT_DIR/rekap.sh" rekap 2>&1)
        if echo "$OUTPUT" | grep -qE "Tidak ada pesan dalam window|Belum ada data pesan"; then
          log "    no data in window — skip"
          total_no_data=$((total_no_data + 1))
        elif echo "$OUTPUT" | grep -q "Selesai"; then
          BYTES=$(echo "$OUTPUT" | grep "Selesai" | grep -oE '\([0-9]+ bytes\)' || echo "")
          log "    ✓ saved $BYTES"
          total_calls=$((total_calls + 1))
        else
          log "    ✗ failed:"
          echo "$OUTPUT" | tail -3 | sed 's/^/        /'
        fi
      fi
    done
  fi

  if [ -z "$SKIP_RESUME" ]; then
    for slot in "${RESUME_SLOTS[@]}"; do
      FN=$(ts_to_filename "$slot")
      OUT="$DATA_DIR/resume/$D/resume_${D}_${FN}.txt"
      if [ -f "$OUT" ] && [ -s "$OUT" ]; then
        log "  resume $slot — already exists, skip"
        total_skipped=$((total_skipped + 1))
        continue
      fi
      SLOT_EPOCH=$(slot_to_epoch "$D $slot")
      if [ "$SLOT_EPOCH" -gt "$NOW_EPOCH" ]; then
        log "  resume $slot — in the future, skip"
        total_future=$((total_future + 1))
        continue
      fi
      log "  resume $slot — running..."
      if [ -n "$DRY_RUN" ]; then
        echo "    [DRY] WRG_FAKE_NOW=\"$D $slot\" bash $SCRIPT_DIR/rekap.sh resume"
      else
        OUTPUT=$(WRG_FAKE_NOW="$D $slot" bash "$SCRIPT_DIR/rekap.sh" resume 2>&1)
        if echo "$OUTPUT" | grep -qE "Belum ada rekap"; then
          log "    no rekap in window — skip"
          total_no_data=$((total_no_data + 1))
        elif echo "$OUTPUT" | grep -q "Selesai"; then
          BYTES=$(echo "$OUTPUT" | grep "Selesai" | grep -oE '\([0-9]+ bytes\)' || echo "")
          log "    ✓ saved $BYTES"
          total_calls=$((total_calls + 1))
        else
          log "    ✗ failed:"
          echo "$OUTPUT" | tail -3 | sed 's/^/        /'
        fi
      fi
    done
  fi
done

log "════════════════════════════════════════"
log "Summary: ${total_calls} AI calls completed · ${total_skipped} already-existing skipped · ${total_no_data} no-data slots · ${total_future} future-slots skipped"
