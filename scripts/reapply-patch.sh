#!/bin/bash
# ============================================================
# WRG Monitor — Re-apply openclaw inbound-message tap
# Jalankan setelah openclaw di-update (pnpm/npm i -g openclaw).
# Idempotent: cek dulu, hanya patch kalau belum ada.
# ============================================================

set -e

PLUGIN_DIR="$HOME/.openclaw/npm/node_modules/@openclaw/whatsapp/dist"
TARGET="$(ls "$PLUGIN_DIR"/monitor-*.js 2>/dev/null | head -1)"
if [ -z "$TARGET" ]; then
  echo "Could not find monitor-*.js under $PLUGIN_DIR" >&2
  exit 1
fi

IMPORT_LINE='import { appendFileSync as _wrgAppend, mkdirSync as _wrgMkdir } from "node:fs"; // WRG_MONITOR_TAP_V1 import'
SENTINEL="WRG_MONITOR_TAP_V1"

if grep -q "$SENTINEL" "$TARGET"; then
  echo "✓ Patch already present in $(basename "$TARGET")"
  exit 0
fi

echo "Patching $(basename "$TARGET")..."
cp "$TARGET" "${TARGET}.wrg-orig"

python3 - "$TARGET" <<'PY'
import sys, re
path = sys.argv[1]
with open(path, "r") as f:
    src = f.read()

# Add the fs import on a new line right after the first import statement.
import_line = ('import { appendFileSync as _wrgAppend, mkdirSync as _wrgMkdir } '
               'from "node:fs"; // WRG_MONITOR_TAP_V1 import\n')
m_imp = re.search(r"^(import [^\n]+\n)", src, flags=re.MULTILINE)
if not m_imp:
    sys.exit("could not find any import statement to anchor")
src = src[:m_imp.end()] + import_line + src[m_imp.end():]

# Inject the tap right after the inboundLogger.info(..., "inbound message") call.
inject_marker = re.compile(
    r'(\t\tinboundLogger\.info\(\{\n'
    r'(?:\t\t\t[^\n]+\n)+'
    r'\t\t\}, "inbound message"\);\n)'
)
patch = (
    "\t\t/* WRG_MONITOR_TAP_V1 — appends every inbound message to "
    "~/.openclaw/tmp/wrg-monitor/messages/<date>/<jid>.jsonl. Reapply after openclaw upgrade. */\n"
    "\t\ttry {\n"
    "\t\t\tconst _ts = typeof timestamp === \"number\" ? timestamp : Date.now();\n"
    "\t\t\tconst _date = new Date(_ts).toISOString().slice(0, 10);\n"
    "\t\t\tconst _dir = \"/Users/development/.openclaw/tmp/wrg-monitor/messages/\" + _date;\n"
    "\t\t\t_wrgMkdir(_dir, { recursive: true });\n"
    "\t\t\tconst _gid = inbound.from;\n"
    "\t\t\tconst _safe = String(_gid).replace(/[^A-Za-z0-9._@-]/g, \"_\");\n"
    "\t\t\t_wrgAppend(_dir + \"/\" + _safe + \".jsonl\", JSON.stringify({\n"
    "\t\t\t\tts: new Date(_ts).toISOString(),\n"
    "\t\t\t\tts_ms: _ts,\n"
    "\t\t\t\tchat_type: String(_gid).endsWith(\"@g.us\") ? \"group\" : \"direct\",\n"
    "\t\t\t\tgroup_jid: String(_gid).endsWith(\"@g.us\") ? _gid : null,\n"
    "\t\t\t\tsender: inbound.from,\n"
    "\t\t\t\tsender_name: msg.pushName ?? null,\n"
    "\t\t\t\tto: self.e164 ?? \"me\",\n"
    "\t\t\t\tbody: enriched.body,\n"
    "\t\t\t\tmedia_type: enriched.mediaType ?? null,\n"
    "\t\t\t\tmedia_path: enriched.mediaPath ?? null,\n"
    "\t\t\t\tmedia_filename: enriched.mediaFileName ?? null,\n"
    "\t\t\t\tmessage_id: inbound.id ?? null,\n"
    "\t\t\t\tmentioned_jids: Array.isArray(mentionedJids) ? mentionedJids : []\n"
    "\t\t\t}) + \"\\n\");\n"
    "\t\t} catch (_wrgErr) { try { process.stderr.write(\"[WRG_TAP_ERR] \" + String(_wrgErr && _wrgErr.message || _wrgErr) + \"\\n\"); } catch (_) {} }\n"
)
m = inject_marker.search(src)
if not m:
    sys.exit("could not find inboundLogger.info(..., \"inbound message\") block; "
             "openclaw whatsapp plugin layout changed — patch needs manual update")
src = src[:m.end()] + patch + src[m.end():]

with open(path, "w") as f:
    f.write(src)
print("patch applied")
PY

if grep -q "$SENTINEL" "$TARGET" && node --check "$TARGET" >/dev/null 2>&1; then
  echo "✓ Patch applied and validates. Restart gateway:"
  echo "  GW_PID=\$(ps -axo pid,command | grep -E 'openclaw/dist/index.js gateway' | grep -v grep | awk '{print \$1}' | head -1)"
  echo "  kill -9 \"\$GW_PID\" && sleep 3 && openclaw gateway start"
else
  echo "✗ Patch verification failed; restoring backup"
  mv "${TARGET}.wrg-orig" "$TARGET"
  exit 1
fi
