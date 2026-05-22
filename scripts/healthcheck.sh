#!/bin/bash
# ============================================================
# WRG Monitor — Health Check
# Verify semua komponen up & jalan benar. Output color-coded.
# Exit code: 0 = all OK, 1 = ada FAIL, 2 = cuma WARN
# ============================================================

source "$(dirname "$0")/../config/config.sh"

# Color setup (skip if not tty)
if [ -t 1 ]; then
  G='\033[32m'; Y='\033[33m'; R='\033[31m'; B='\033[36m'; D='\033[2m'; N='\033[0m'
else
  G=''; Y=''; R=''; B=''; D=''; N=''
fi

OK=0
WARN=0
FAIL=0

pass()  { printf "  ${G}✓${N} %s ${D}%s${N}\n" "$1" "${2:-}"; OK=$((OK+1)); }
warn()  { printf "  ${Y}⚠${N} %s ${D}%s${N}\n" "$1" "${2:-}"; WARN=$((WARN+1)); }
fail()  { printf "  ${R}✗${N} %s ${D}%s${N}\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
hdr()   { printf "\n${B}━━━ %s ━━━${N}\n" "$1"; }

NOW_S=$(date +%s)

# ── Deps & env ────────────────────────────────────────────────
hdr "Dependencies"
for cmd in openclaw python3 jq /usr/bin/curl shasum; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "$cmd available" "($(command -v "$cmd"))"
  else
    fail "$cmd NOT FOUND on PATH" "install dulu"
  fi
done

# ── openclaw config ───────────────────────────────────────────
hdr "openclaw Config"
if openclaw config validate >/dev/null 2>&1; then
  pass "openclaw.json valid"
else
  fail "openclaw.json validation failed" "openclaw config validate"
fi
ALLOW_FROM=$(jq -r '.channels.whatsapp.allowFrom | length' /Users/development/.openclaw/openclaw.json 2>/dev/null)
[ "$ALLOW_FROM" -ge 2 ] && pass "whatsapp.allowFrom has $ALLOW_FROM entries" || warn "whatsapp.allowFrom has $ALLOW_FROM entries (expected 2: bot + owner)"

# ── openclaw gateway ──────────────────────────────────────────
hdr "openclaw Gateway"
GW_PID=$(ps -axo pid,command 2>&1 | grep -E "openclaw/dist/index.js gateway" | grep -v grep | awk '{print $1}' | head -1)
if [ -n "$GW_PID" ]; then
  GW_START=$(ps -p "$GW_PID" -o lstart= 2>/dev/null | xargs -I{} date -j -f "%a %b %d %T %Y" "{}" "+%s" 2>/dev/null)
  if [ -n "$GW_START" ]; then
    UPTIME=$((NOW_S - GW_START))
    pass "gateway running" "PID $GW_PID, uptime ${UPTIME}s"
  else
    pass "gateway running" "PID $GW_PID"
  fi
else
  fail "gateway not running" "openclaw gateway start"
fi
if lsof -i :18789 -t >/dev/null 2>&1; then
  pass "gateway listening on :18789"
else
  fail "nothing on :18789"
fi
LATEST_WA=$(grep "Listening for personal WhatsApp" /Users/development/.openclaw/logs/gateway.log 2>/dev/null | tail -1)
if [ -n "$LATEST_WA" ]; then
  WA_TS=$(echo "$LATEST_WA" | grep -oE '^[0-9-]+T[0-9:]+' | head -1)
  pass "whatsapp listener active" "last: $WA_TS"
else
  warn "no recent 'Listening for personal WhatsApp' in gateway.log"
fi
LAST_INBOUND=$(grep "Inbound message" /Users/development/.openclaw/logs/gateway.log 2>/dev/null | tail -1)
if [ -n "$LAST_INBOUND" ]; then
  INB_TS=$(echo "$LAST_INBOUND" | grep -oE '^[0-9-]+T[0-9:]+')
  INB_EPOCH=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$INB_TS" "+%s" 2>/dev/null || echo 0)
  AGE_MIN=$(( (NOW_S - INB_EPOCH) / 60 ))
  if [ "$AGE_MIN" -lt 60 ]; then
    pass "last inbound message" "${AGE_MIN}m ago"
  elif [ "$AGE_MIN" -lt 240 ]; then
    warn "last inbound ${AGE_MIN}m ago" "(quiet window — bisa wajar)"
  else
    warn "last inbound ${AGE_MIN}m ago" "(>4h — check WA connection)"
  fi
else
  warn "no Inbound message logged yet"
fi

# ── openclaw patch ────────────────────────────────────────────
hdr "openclaw Patch"
MONITOR_JS="$HOME/.openclaw/npm/node_modules/@openclaw/whatsapp/dist/monitor-C5_C_RGJ.js"
if [ -f "$MONITOR_JS" ]; then
  if grep -q "WRG_MONITOR_TAP_V1" "$MONITOR_JS"; then
    pass "patch sentinel present" "WRG_MONITOR_TAP_V1"
  else
    fail "patch missing in monitor-*.js" "run scripts/reapply-patch.sh"
  fi
  if [ -f "$MONITOR_JS.wrg-orig" ]; then
    pass "patch backup exists" "$(basename "$MONITOR_JS").wrg-orig"
  else
    warn "no backup file" "manual rollback won't be possible"
  fi
else
  fail "monitor-*.js not found" "openclaw whatsapp plugin missing"
fi

# ── Data paths ────────────────────────────────────────────────
hdr "Data Paths"
MESSAGES_TARGET="$HOME/.openclaw/tmp/wrg-monitor/messages"
if [ -L "$DATA_DIR/messages" ]; then
  ACTUAL=$(readlink "$DATA_DIR/messages")
  if [ "$ACTUAL" = "$MESSAGES_TARGET" ]; then
    pass "data/messages → openclaw tap" "$ACTUAL"
  else
    warn "data/messages symlink wrong target" "$ACTUAL"
  fi
elif [ -d "$DATA_DIR/messages" ]; then
  warn "data/messages is a dir, not symlink" "patch writes to $MESSAGES_TARGET"
else
  fail "data/messages missing"
fi

for d in rekap resume briefing pola state; do
  if [ -d "$DATA_DIR/$d" ] && [ -w "$DATA_DIR/$d" ]; then
    COUNT=$(find "$DATA_DIR/$d" -type f 2>/dev/null | wc -l | tr -d ' ')
    pass "data/$d writable" "$COUNT files"
  else
    fail "data/$d missing or not writable"
  fi
done

# ── Capture activity ──────────────────────────────────────────
hdr "Capture Activity"
TODAY=$(date '+%Y-%m-%d')
TODAY_DIR="$MESSAGES_TARGET/$TODAY"
if [ -d "$TODAY_DIR" ]; then
  FILES=$(ls "$TODAY_DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
  LINES=$(cat "$TODAY_DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
  if [ "$FILES" -gt 0 ]; then
    pass "today captures" "$FILES files, $LINES msgs"
    LATEST=$(ls -t "$TODAY_DIR"/*.jsonl 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
      LATEST_AGE=$(( (NOW_S - $(stat -f %m "$LATEST")) / 60 ))
      if [ "$LATEST_AGE" -lt 60 ]; then
        pass "most recent capture" "${LATEST_AGE}m ago"
      elif [ "$LATEST_AGE" -lt 180 ]; then
        warn "most recent capture ${LATEST_AGE}m ago"
      else
        warn "most recent capture ${LATEST_AGE}m ago" "(>3h, check WA)"
      fi
    fi
  else
    warn "no capture today" "wait for messages"
  fi
else
  warn "no $TODAY directory yet"
fi

# ── Cron entries ──────────────────────────────────────────────
hdr "Cron"
EXPECTED_PATTERNS=("rekap.sh rekap" "rekap.sh resume" "notif_tua.sh" "pola_komunikasi.sh" "briefing_weekend.sh")
CRON_OUT=$(crontab -l 2>/dev/null)
for pat in "${EXPECTED_PATTERNS[@]}"; do
  if echo "$CRON_OUT" | grep -q "$pat"; then
    pass "cron entry for '$pat'"
  else
    fail "cron missing '$pat'" "run setup.sh"
  fi
done

# ── Scripts ───────────────────────────────────────────────────
hdr "Scripts"
for script in rekap.sh briefing_weekend.sh pola_komunikasi.sh notif_tua.sh reapply-patch.sh dashboard.py healthcheck.sh; do
  path="$(dirname "$0")/$script"
  if [ -f "$path" ]; then
    if [ -x "$path" ]; then
      pass "$script executable"
    else
      warn "$script not executable" "chmod +x"
    fi
  else
    fail "$script missing"
  fi
done

# ── Recent rekap/resume runs ──────────────────────────────────
hdr "Recent Runs"
for kind in rekap resume; do
  KDIR="$DATA_DIR/$kind/$TODAY"
  if [ -d "$KDIR" ]; then
    COUNT=$(ls "$KDIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
    LATEST=$(ls -t "$KDIR"/*.txt 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
      AGE_MIN=$(( (NOW_S - $(stat -f %m "$LATEST")) / 60 ))
      MAX_GAP=$([ "$kind" = "rekap" ] && echo 240 || echo 480) # 4h or 8h
      if [ "$AGE_MIN" -lt "$MAX_GAP" ]; then
        pass "$kind today" "$COUNT runs, last ${AGE_MIN}m ago"
      else
        warn "$kind: last run ${AGE_MIN}m ago" "(>${MAX_GAP}m, cron may not be firing)"
      fi
    else
      warn "no $kind today yet"
    fi
  fi
done

# ── Dashboard ─────────────────────────────────────────────────
hdr "Dashboard"
DASH_PID=$(lsof -t -i :8090 2>/dev/null)
if [ -n "$DASH_PID" ]; then
  pass "dashboard running" "PID $DASH_PID on :8090"
  if /usr/bin/curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8090/ 2>/dev/null | grep -q 200; then
    pass "dashboard responds 200"
  else
    fail "dashboard not responding"
  fi
else
  fail "dashboard not running" "bash scripts/dashboard.py or use Login Item"
fi
LOGIN_ITEMS=$(osascript -e 'tell application "System Events" to get name of every login item' 2>/dev/null)
if echo "$LOGIN_ITEMS" | grep -q "start-dashboard"; then
  pass "login item registered" "auto-start on next login"
else
  warn "login item not registered" "dashboard won't auto-start after reboot"
fi

# ── AI Quota (OpenRouter) ─────────────────────────────────────
hdr "AI Quota"
ERROR_LOG="$LOG_DIR/error.log"
if [ ! -f "$ERROR_LOG" ]; then
  warn "error.log belum ada" "AI belum pernah dipanggil"
else
  RECENT_403=$(tail -100 "$ERROR_LOG" 2>/dev/null | grep -cE "Key limit exceeded|403 .*limit" || true)
  RECENT_403=${RECENT_403:-0}
  if [ "$RECENT_403" -eq 0 ]; then
    pass "no recent quota errors" "tail -100 error.log clean"
  elif [ "$RECENT_403" -lt 5 ]; then
    warn "$RECENT_403 quota errors in recent log" "topup soon di https://openrouter.ai/settings/keys"
  else
    fail "$RECENT_403 quota errors in recent log" "OpenRouter monthly cap hit — topup wajib"
  fi
fi

# Last successful AI artifact (rekap atau briefing)
LATEST_REKAP=$(ls -t "$DATA_DIR/rekap"/*/rekap_*.txt 2>/dev/null | head -1)
if [ -n "$LATEST_REKAP" ]; then
  REKAP_AGE_H=$(( ( $(date +%s) - $(stat -f %m "$LATEST_REKAP") ) / 3600 ))
  REKAP_NAME=$(basename "$LATEST_REKAP")
  if [ "$REKAP_AGE_H" -lt 8 ]; then
    pass "last successful rekap" "${REKAP_AGE_H}h ago ($REKAP_NAME)"
  elif [ "$REKAP_AGE_H" -lt 24 ]; then
    warn "last rekap ${REKAP_AGE_H}h ago" "$REKAP_NAME — slot ke-skip?"
  else
    fail "last rekap ${REKAP_AGE_H}h ago" "AI pipeline likely dead, cek quota + gateway"
  fi
else
  warn "belum ada rekap sukses" "system fresh atau total failure"
fi

# Notif quota state
NOTIF_STATE="$DATA_DIR/state/notified-quota.json"
if [ -f "$NOTIF_STATE" ]; then
  LAST_NOTIF=$(jq -r '.last_sent_iso // "-"' "$NOTIF_STATE" 2>/dev/null)
  LAST_NOTIF_TS=$(jq -r '.last_sent_ts // 0' "$NOTIF_STATE" 2>/dev/null)
  COOLDOWN_AGE_H=$(( ( $(date +%s) - LAST_NOTIF_TS ) / 3600 ))
  if [ "$COOLDOWN_AGE_H" -lt 4 ]; then
    warn "quota notif sent ${COOLDOWN_AGE_H}h ago" "owner sudah dapat WA; cooldown 4j aktif"
  else
    pass "quota notif state" "last: $LAST_NOTIF"
  fi
fi

# Crontab PATH check (cron tanpa PATH → openclaw not found)
if crontab -l 2>/dev/null | head -3 | grep -q '^PATH=.*homebrew'; then
  pass "crontab PATH includes /opt/homebrew/bin"
else
  fail "crontab missing PATH= line" "tambah 'PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin' di top crontab"
fi

# ── Disk space ────────────────────────────────────────────────
hdr "Disk"
DISK_AVAIL=$(df -h "$DATA_DIR" | tail -1 | awk '{print $4}')
DISK_PCT=$(df "$DATA_DIR" | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PCT" -lt 85 ]; then
  pass "disk: $DISK_AVAIL free" "($DISK_PCT% used)"
elif [ "$DISK_PCT" -lt 95 ]; then
  warn "disk: $DISK_AVAIL free" "($DISK_PCT% used, clean up soon)"
else
  fail "disk: $DISK_AVAIL free" "($DISK_PCT% used, critical)"
fi

# ── Summary ───────────────────────────────────────────────────
printf "\n${B}━━━ Summary ━━━${N}\n"
printf "  ${G}OK${N}: %d   ${Y}WARN${N}: %d   ${R}FAIL${N}: %d\n\n" "$OK" "$WARN" "$FAIL"

if [ "$FAIL" -gt 0 ]; then
  exit 1
elif [ "$WARN" -gt 0 ]; then
  exit 2
else
  exit 0
fi
