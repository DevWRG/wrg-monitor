#!/bin/bash
# ============================================================
# WRG Monitor — Setup Installer
# Jalankan SEKALI: bash setup.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo "========================================"
echo "  WRG Monitor — Setup"
echo "========================================"
echo ""

# 1. Permissions
chmod +x "$SCRIPT_DIR/rekap.sh"
chmod +x "$SCRIPT_DIR/briefing_weekend.sh"
chmod +x "$SCRIPT_DIR/setup.sh"
chmod +x "$SCRIPT_DIR/pola_komunikasi.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/list_members.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/notif_tua.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/healthcheck.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/backfill.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/reapply-patch.sh" 2>/dev/null || true
echo "✓ Script permissions OK"

# 2. Direktori
mkdir -p "$BASE_DIR/logs" \
         "$BASE_DIR/data/rekap" \
         "$BASE_DIR/data/resume" \
         "$BASE_DIR/data/briefing" \
         "$BASE_DIR/data/pola"
echo "✓ Direktori output siap"

# 3. Timezone
TZ_SEKARANG=$(timedatectl 2>/dev/null | grep "Time zone" | awk '{print $3}')
if [ "$TZ_SEKARANG" != "Asia/Jakarta" ]; then
  echo "⚠ Timezone server: $TZ_SEKARANG"
  echo "  Rekap dijadwalkan WIB. Untuk set timezone:"
  echo "  sudo timedatectl set-timezone Asia/Jakarta"
else
  echo "✓ Timezone WIB (Asia/Jakarta) OK"
fi

# 4. Cek openclaw tersedia
if ! command -v openclaw &>/dev/null; then
  echo "✗ openclaw tidak ditemukan di PATH. Install dulu sebelum lanjut."
  exit 1
fi
echo "✓ openclaw ditemukan: $(openclaw --version 2>/dev/null | head -1)"

# 5. Backup crontab lama
crontab -l 2>/dev/null > /tmp/crontab_wrg_backup_$(date +%Y%m%d).txt
echo "✓ Backup crontab → /tmp/crontab_wrg_backup_$(date +%Y%m%d).txt"

# 6. Pasang cron jobs
# Window aktif: 07:00-22:00 WIB
CRON_REKAP="0 7,12,17,22 * * * $SCRIPT_DIR/rekap.sh rekap >> $BASE_DIR/logs/cron.log 2>&1"
CRON_RESUME="0 14 * * * $SCRIPT_DIR/rekap.sh resume >> $BASE_DIR/logs/cron.log 2>&1
10 22 * * * $SCRIPT_DIR/rekap.sh resume >> $BASE_DIR/logs/cron.log 2>&1"
CRON_NOTIF="5 14 * * * $SCRIPT_DIR/notif_tua.sh >> $BASE_DIR/logs/cron.log 2>&1
15 22 * * * $SCRIPT_DIR/notif_tua.sh >> $BASE_DIR/logs/cron.log 2>&1"
CRON_MEMBERS="30 22 * * * $SCRIPT_DIR/list_members.sh >> $BASE_DIR/logs/cron.log 2>&1"
CRON_POLA="30 23 * * * $SCRIPT_DIR/pola_komunikasi.sh >> $BASE_DIR/logs/cron.log 2>&1"
CRON_SAB="0 7 * * 6 $SCRIPT_DIR/briefing_weekend.sh >> $BASE_DIR/logs/cron.log 2>&1"
CRON_MIN="0 7 * * 0 $SCRIPT_DIR/briefing_weekend.sh >> $BASE_DIR/logs/cron.log 2>&1"

# Hapus entry WRG lama kalau ada, tambah yang baru
(crontab -l 2>/dev/null | grep -v "wrg-monitor\|WRG Monitor") | crontab -
# Pastikan PATH ada di top crontab — openclaw, node, dst. live di /opt/homebrew/bin
# (homebrew npm). Cron default PATH hanya /usr/bin:/bin, jadi `openclaw: command
# not found` kalau line ini tidak ada.
if ! crontab -l 2>/dev/null | head -3 | grep -q '^PATH='; then
  EXISTING=$(crontab -l 2>/dev/null)
  (echo "PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"; echo "$EXISTING") | crontab -
  echo "✓ Injected PATH= di top crontab"
fi
(
  crontab -l 2>/dev/null
  echo ""
  echo "# ── WRG Monitor ──────────────────────────────"
  echo "# Rekap (5 jam window) — 07/12/17/22 WIB (4x/day)"
  echo "$CRON_REKAP"
  echo "# Resume (7 jam dari rekap) — 14:00 & 22:10 WIB"
  echo "$CRON_RESUME"
  echo "# Notif TUA via WA — 14:05 & 22:15 (5 min after resume)"
  echo "$CRON_NOTIF"
  echo "# Member directory refresh — daily 22:30 WIB"
  echo "$CRON_MEMBERS"
  echo "# Pola komunikasi per grup — daily 23:30 WIB"
  echo "$CRON_POLA"
  echo "# Briefing weekend — Sabtu 07:00 WIB"
  echo "$CRON_SAB"
  echo "# Briefing weekend — Minggu 07:00 WIB"
  echo "$CRON_MIN"
  echo "# ─────────────────────────────────────────────"
) | crontab -

echo "✓ Cron jobs terpasang"

# 7. Tampilkan ringkasan
echo ""
echo "========================================"
echo "  Jadwal Aktif (window 07:00-22:00 WIB)"
echo "========================================"
echo "  Rekap (5 jam)   : 07/12/17/22 WIB (4x/day)"
echo "  Resume (7 jam)  : 14:00 & 22:10 WIB"
echo "  Notif TUA → WA  : 14:05 & 22:15 WIB"
echo "  Member refresh  : 22:30 WIB (daily)"
echo "  Pola Komunikasi : 23:30 WIB (daily)"
echo "  Briefing        : Sabtu & Minggu 07:00 WIB"
echo ""
echo "  Output disimpan di:"
echo "  $BASE_DIR/data/rekap/     ← rekap per 3 jam"
echo "  $BASE_DIR/data/resume/    ← resume per 7 jam"
echo "  $BASE_DIR/data/pola/      ← profile pola komunikasi per grup"
echo "  $BASE_DIR/data/briefing/  ← briefing weekend"
echo "  $BASE_DIR/data/messages/  ← raw inbound (symlink ke openclaw tap)"
echo "  $BASE_DIR/logs/           ← log sistem"
echo ""
echo "========================================"
echo "  Cek & Test"
echo "========================================"
echo "  Test rekap   : bash $SCRIPT_DIR/rekap.sh rekap"
echo "  Test resume  : bash $SCRIPT_DIR/rekap.sh resume"
echo "  Test pola    : bash $SCRIPT_DIR/pola_komunikasi.sh"
echo "  Test briefing: bash $SCRIPT_DIR/briefing_weekend.sh"
echo "  Monitor log  : tail -f $BASE_DIR/logs/wrg-monitor.log"
echo "  Lihat crontab: crontab -l"
echo ""
