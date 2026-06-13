#!/usr/bin/env bash
# Resolve a wrg-monitor OpenClaw session and emit a per-group normalized transcript
# via the whatsapp-recap parser (wrg mode). Wraps the fixed session path so callers
# never need the full path / sessionId.
#
# Usage:
#   resolve_session.sh [latest | YYYY-MM-DD | <sessionId> | <path.jsonl>] [extra parser args...]
#   resolve_session.sh --list
#
# Env overrides:
#   WRG_AGENT     OpenClaw agent dir name           (default: main)
#   WRG_SESSIONS  sessions directory                (default: ~/.openclaw/agents/$WRG_AGENT/sessions)
#   WRG_PARSER    path to parse_whatsapp.py         (default: ~/.claude/skills/whatsapp-recap/scripts/parse_whatsapp.py)
set -euo pipefail

AGENT="${WRG_AGENT:-main}"
SESS_DIR="${WRG_SESSIONS:-$HOME/.openclaw/agents/$AGENT/sessions}"
PARSER="${WRG_PARSER:-$HOME/.claude/skills/whatsapp-recap/scripts/parse_whatsapp.py}"

[ -d "$SESS_DIR" ] || { echo "error: sessions dir not found: $SESS_DIR" >&2; exit 1; }
[ -f "$PARSER" ]   || { echo "error: parser not found: $PARSER (install the whatsapp-recap skill)" >&2; exit 1; }

# collect non-trajectory session files
sessions=()
for f in "$SESS_DIR"/*.jsonl; do
  case "$f" in *.trajectory.jsonl) continue;; esac
  [ -e "$f" ] && sessions+=("$f")
done
[ "${#sessions[@]}" -gt 0 ] || { echo "error: no session .jsonl files in $SESS_DIR" >&2; exit 1; }

sel="${1:-latest}"; shift || true

list_sessions() {
  printf '%-38s  %-10s  %5s  %s\n' "sessionId" "first-date" "msgs" "mtime"
  for f in "${sessions[@]}"; do
    id="$(basename "$f" .jsonl)"
    d="$(grep -om1 '"timestamp":"[0-9-]\{10\}' "$f" 2>/dev/null | head -1 | grep -o '[0-9-]\{10\}$' || echo '?')"
    n="$(grep -c '"type":"message"' "$f" 2>/dev/null)" || n=0
    mt="$(date -r "$f" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
    printf '%-38s  %-10s  %5s  %s\n' "$id" "$d" "$n" "$mt"
  done
}

pick_latest() { ls -t "${sessions[@]}" | head -1; }

pick_by_date() {  # arg: YYYY-MM-DD -> session with most messages that day
  local date="$1" best="" bestn=0 n
  for f in "${sessions[@]}"; do
    n="$(grep -c "\"timestamp\":\"$date" "$f" 2>/dev/null)" || n=0
    if [ "$n" -gt "$bestn" ]; then bestn="$n"; best="$f"; fi
  done
  [ -n "$best" ] || { echo "error: no session with messages on $date" >&2; exit 1; }
  echo "$best"
}

case "$sel" in
  --list|list-sessions) list_sessions; exit 0;;
  latest|"")            FILE="$(pick_latest)";;
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) FILE="$(pick_by_date "$sel")";;
  */*|*.jsonl)          FILE="$sel";;
  *)                    FILE="$SESS_DIR/$sel.jsonl";;
esac

[ -f "$FILE" ] || { echo "error: session file not found: $FILE" >&2; exit 1; }
echo "# sesi: $(basename "$FILE")  (agent: $AGENT)" >&2
exec python3 "$PARSER" "$FILE" --format wrg "$@"
