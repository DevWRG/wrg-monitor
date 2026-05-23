#!/bin/bash
# ============================================================
# WRG Monitor — Daily Git Backup Push
# Stage data/members.json + members.md + roster.json kalau ada perubahan,
# commit dengan timestamp, push ke origin/main.
# Dipanggil oleh cron pukul 22:40 (10 menit setelah list_members.sh selesai).
# ============================================================

set -eo pipefail

# Inject PATH untuk cron context (macos gotcha: launchd/cron PATH minimal)
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Cron context tidak punya akses ke osxkeychain (no TTY/login session) — gh auth
# git-credential bakal return empty. Workaround: export GH_TOKEN dari file 600
# yang gh credential helper akan pakai bypassing keychain.
TOKEN_FILE="$HOME/.config/wrg-monitor/gh-token"
if [ -f "$TOKEN_FILE" ]; then
  export GH_TOKEN="$(cat "$TOKEN_FILE")"
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

TS=$(date '+%Y-%m-%d %H:%M:%S WIB')
LOG_PREFIX="[git-backup $(date '+%H:%M:%S')]"

FILES=(data/members.json data/members.md data/roster.json)

# Pastikan file existing (jangan force commit kalau script lain belum sempat update)
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "$LOG_PREFIX skip — $f not found"
    exit 0
  fi
done

# Stage
git add "${FILES[@]}"

# Check apakah ada perubahan staged
if git diff --cached --quiet; then
  echo "$LOG_PREFIX no changes to commit"
  exit 0
fi

# Bikin commit
SUMMARY=$(git diff --cached --shortstat)
git commit -m "daily snapshot $TS

$SUMMARY"

# Push — kalau gagal (network/auth), commit tetep aman di local, retry besok
if git push origin main 2>&1; then
  echo "$LOG_PREFIX pushed: $SUMMARY"
else
  RC=$?
  echo "$LOG_PREFIX push failed (rc=$RC) — commit tertinggal lokal, akan retry"
  exit $RC
fi
