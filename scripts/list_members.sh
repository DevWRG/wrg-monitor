#!/bin/bash
# ============================================================
# WRG Monitor — List Members across all WhatsApp groups
# Sumber data:
#  (1) openclaw session JSONLs — runtime-context "group_members"
#      (paling rich: ada nama + nomor untuk SEMUA member, bukan cuma yg
#       pernah kirim pesan)
#  (2) data/messages/*.jsonl sender_name occurrences — display name
#      yang kelihatan saat orang kirim pesan
# Output:
#  data/members.json — machine-readable mapping, Pak/Mbak edit name di sini
#  data/members.md   — human-readable table, gampang review
# ============================================================

set -e
source "$(dirname "$0")/../config/config.sh"

OUT_JSON="$DATA_DIR/members.json"
OUT_MD="$DATA_DIR/members.md"
SESSIONS_INDEX="$HOME/.openclaw/agents/main/sessions/sessions.json"

# Existing members.json (preserve user-edited names if any)
EXISTING="{}"
if [ -f "$OUT_JSON" ]; then
  EXISTING=$(jq -c '.' "$OUT_JSON" 2>/dev/null || echo "{}")
fi

# Daily backup — sebelum rebuild, snapshot members.json yang sedang aktif.
# Disimpan di data/backups/ dengan timestamp. Auto-prune file >30 hari supaya
# disk gak penuh. Kalau ada bug kembali wipe data, restore dari sini.
if [ -f "$OUT_JSON" ]; then
  BACKUP_DIR="$DATA_DIR/backups"
  mkdir -p "$BACKUP_DIR"
  BACKUP_FILE="$BACKUP_DIR/members-$(date '+%Y-%m-%d_%H%M').json"
  cp -p "$OUT_JSON" "$BACKUP_FILE"
  echo "✓ Backup: $(basename "$BACKUP_FILE")"
  # Prune backup >30 hari
  find "$BACKUP_DIR" -name 'members-*.json' -type f -mtime +30 -delete 2>/dev/null
fi

# Step 1 — collect raw group_members listings from session JSONLs
# Format dalam session: "Name (+62xxx), +62yyy, Name (+62zzz), ..."
RAW_LINES=$(jq -r '
  to_entries[]
  | select(.key|test("whatsapp:group"))
  | .value.sessionFile + "\t" + (.key | sub("^agent:main:whatsapp:group:"; ""))
' "$SESSIONS_INDEX" 2>/dev/null)

PARSED=$(mktemp)
trap 'rm -f $PARSED' EXIT
echo "[]" > "$PARSED"

GROUP_COUNT=0
while IFS=$'\t' read -r SESSION_FILE GROUP_JID; do
  [ -z "$SESSION_FILE" ] || [ ! -f "$SESSION_FILE" ] && continue
  GROUP_COUNT=$((GROUP_COUNT + 1))
  # Find newest runtime-context entry. JSONL files have one JSON object per line,
  # but a content field can include embedded newlines. Use jq slurp to read all
  # entries as a single array, then pick the last runtime-context.
  RC_CONTENT=$(jq -rs '
    [.[] | select(.type=="custom_message" and .customType=="openclaw.runtime-context")]
    | last
    | .content // ""
  ' "$SESSION_FILE" 2>/dev/null)
  [ -z "$RC_CONTENT" ] || [ "$RC_CONTENT" = "null" ] && continue

  MEMBERS_STR=$(printf '%s' "$RC_CONTENT" | grep -oE '"group_members": "[^"]+"' | head -1 | sed 's/^"group_members":[[:space:]]*"//; s/"$//')
  GROUP_NAME=$(printf '%s' "$RC_CONTENT" | grep -oE '"group_subject": "[^"]+"' | head -1 | sed 's/^"group_subject":[[:space:]]*"//; s/"$//')
  [ -z "$MEMBERS_STR" ] && continue

  # Split by ", " — each entry is "Name (+62xxx)" or "+62yyy"
  # Save into PARSED as JSON entries
  echo "$MEMBERS_STR" | tr ',' '\n' | while IFS= read -r ENTRY; do
    ENTRY=$(echo "$ENTRY" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$ENTRY" ] && continue
    # Match "Name (+62xxx)" or just "+62yyy" or "+11540744925281"
    PHONE=$(echo "$ENTRY" | grep -oE '\+[0-9]+' | head -1)
    NAME=$(echo "$ENTRY" | sed -E 's/\s*\(\+[0-9]+\)$//; s/\+[0-9]+//' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -z "$PHONE" ] && continue
    jq --arg p "$PHONE" --arg n "$NAME" --arg g "$GROUP_JID" --arg gs "$GROUP_NAME" '. + [{phone:$p, name:$n, group:$g, group_subject:$gs}]' "$PARSED" > "$PARSED.tmp" && mv "$PARSED.tmp" "$PARSED"
  done
done <<< "$RAW_LINES"

echo "Parsed $GROUP_COUNT group sessions"
echo "Raw entries: $(jq 'length' "$PARSED")"

# Step 2 — augment with sender_name from captured JSONLs (pushName per occurrence)
SENDER_NAMES=$(find -L "$DATA_DIR/messages" -name '*.jsonl' -type f -exec cat {} + 2>/dev/null | \
  jq -sc '
    map(select(.sender_name != null and .sender_name != ""))
    | group_by(.sender_name)
    | map({sender_name: .[0].sender_name, count: length, groups: (map(.group_jid) | unique | map(select(. != null)))})
    | sort_by(-.count)
  ' 2>/dev/null || echo "[]")

# Step 3 — consolidate phone → {name, groups[], group_subjects[], source}
# Merge with existing (preserve user-edited "name" field if present)
jq -n \
  --argjson parsed "$(cat "$PARSED")" \
  --argjson existing "$EXISTING" \
  --argjson sender_names "$SENDER_NAMES" '
{
  generated_at: (now | strftime("%Y-%m-%dT%H:%M:%S%z")),
  source_summary: {
    runtime_context_entries: ($parsed | length),
    distinct_phones: ($parsed | map(.phone) | unique | length),
    distinct_sender_names: ($sender_names | length)
  },
  group_directory: (
    $parsed
    | map(select(.group != "" and .group_subject != ""))
    | group_by(.group)
    | map({key: .[0].group, value: (.[0].group_subject)})
    | from_entries
  ),
  group_directory_user: ($existing.group_directory_user // {}),
  members: (
    $parsed
    | group_by(.phone)
    | map(
        . as $entries
        | $entries[0].phone as $p
        | ($entries | map(.name) | map(select(. != "")) | group_by(.) | map({n: .[0], c: length}) | sort_by(-.c) | .[0].n // "") as $auto
        | (($existing.members // []) | map(select(.phone == $p)) | .[0]) as $prev
        | ($prev.name // "") as $user_name
        | ($prev.notes // "") as $user_notes
        | {
            phone: $p,
            auto_name: $auto,
            name: (if $user_name != "" then $user_name else $auto end),
            user_labeled: ($user_name != ""),
            groups: ($entries | map(.group) | unique),
            group_subjects: ($entries | map(.group_subject) | unique | map(select(. != ""))),
            appearance_count: ($entries | length)
          }
          + (if $user_notes != "" then {notes: $user_notes} else {} end)
      )
    | sort_by(-.appearance_count)
  ),
  sender_names_from_messages: $sender_names
}
' > "$OUT_JSON"

echo "Wrote $OUT_JSON"

# Step 3.5 — Resolve LIDs to real phone numbers
# WhatsApp users dengan privacy on muncul sebagai LID (15-17 digit). Setelah user
# tersebut di-add ke contact phone owner, openclaw cache mapping di
# ~/.openclaw/credentials/whatsapp/default/lid-mapping-<LID>_reverse.json
# Step ini iterate members.json, resolve LID → real phone, merge kalau real-nya
# udah ada entry, atau rename kalau belum.
WRG_MEMBERS_JSON="$OUT_JSON" WRG_EXISTING_JSON="$EXISTING" python3 <<'PYEOF'
import json, os, glob, datetime, sys
LID_DIR = os.path.expanduser("~/.openclaw/credentials/whatsapp/default")
JSON_PATH = os.environ["WRG_MEMBERS_JSON"]
# EXISTING is the pre-rebuild snapshot (passed as JSON string).
# Contains user-edited labels keyed by REAL phone — we need to restore those
# when renaming LID → real phone, otherwise user's manual labels get wiped.
try:
    existing = json.loads(os.environ.get("WRG_EXISTING_JSON", "{}") or "{}")
except Exception:
    existing = {}
existing_by_phone = {m["phone"]: m for m in (existing.get("members") or [])}

lid_map = {}
for f in glob.glob(f"{LID_DIR}/lid-mapping-*_reverse.json"):
    lid = os.path.basename(f).replace("lid-mapping-", "").replace("_reverse.json", "")
    try:
        with open(f) as fp:
            real = json.load(fp)
        if isinstance(real, str):
            lid_map[lid] = real
    except Exception:
        continue

try:
    with open(JSON_PATH) as f:
        data = json.load(f)
except Exception as e:
    print(f"  [lid-resolve] skip — cannot read {JSON_PATH}: {e}", file=sys.stderr)
    sys.exit(0)

members = data.get("members", [])
by_phone = {m["phone"]: m for m in members}

resolved = merged = renamed = restored_labels = 0
for m in list(members):
    p_clean = m["phone"].lstrip("+")
    if p_clean not in lid_map:
        continue
    real_raw = lid_map[p_clean]
    real = real_raw if real_raw.startswith("+") else "+" + real_raw

    target = by_phone.get(real)
    if target is None:
        # Rename only — but restore label from EXISTING (user's prior edit on real phone)
        old_key = m["phone"]
        m["phone"] = real
        # If the real phone was previously labeled by user, restore name + notes
        prev = existing_by_phone.get(real)
        if prev and prev.get("name") and not m.get("name"):
            m["name"] = prev["name"]
            m["auto_name"] = prev.get("auto_name", m.get("auto_name", ""))
            m["user_labeled"] = prev.get("user_labeled", True)
            if prev.get("notes"):
                m["notes"] = prev["notes"]
            restored_labels += 1
        by_phone.pop(old_key, None)
        by_phone[real] = m
        renamed += 1
    else:
        # Merge into existing real-phone entry
        target["groups"] = sorted(set(target.get("groups", []) + m.get("groups", [])))
        target["group_subjects"] = sorted(set(target.get("group_subjects", []) + m.get("group_subjects", [])))
        target["appearance_count"] = target.get("appearance_count", 0) + m.get("appearance_count", 0)
        if not target.get("name") and m.get("name"):
            target["name"] = m["name"]
            target["auto_name"] = m.get("auto_name", "")
            target["user_labeled"] = m.get("user_labeled", False)
        members.remove(m)
        by_phone.pop(m["phone"], None)
        merged += 1
    resolved += 1

if resolved:
    data["members"] = members
    data["last_lid_resolve"] = datetime.datetime.now().isoformat(timespec="seconds")
    data["lid_resolve_summary"] = {"resolved": resolved, "merged": merged, "renamed": renamed, "restored_labels": restored_labels}
    tmp = JSON_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, JSON_PATH)
    print(f"  [lid-resolve] resolved={resolved} (renamed={renamed}, merged={merged}, labels-restored={restored_labels}) — {len(members)} members remain")
else:
    print(f"  [lid-resolve] no LIDs to resolve ({len(lid_map)} mappings available)")
PYEOF

# Step 3.6 — Merge canonical roster (data/roster.json)
# Roster = official director-provided list with nama/panggilan/posisi/cabang.
# For existing members whose phone matches roster, enrich with roster fields.
# For roster phones NOT in current members, add as PHANTOM (appearance_count=0)
# so dashboard surfaces them as "belum kedetect" and survives daily rebuild.
ROSTER_PATH="$DATA_DIR/roster.json"
if [ -f "$ROSTER_PATH" ]; then
  WRG_MEMBERS_JSON="$OUT_JSON" WRG_ROSTER_JSON="$ROSTER_PATH" WRG_EXISTING_JSON="$EXISTING" python3 <<'PYEOF'
import json, os, datetime
MJSON = os.environ["WRG_MEMBERS_JSON"]
RJSON = os.environ["WRG_ROSTER_JSON"]
# Pre-rebuild snapshot — used to preserve user-edited notes on phantom entries
# (phantoms have no session data, so jq pass can't carry their notes through).
try:
    existing = json.loads(os.environ.get("WRG_EXISTING_JSON", "{}") or "{}")
except Exception:
    existing = {}
existing_by_phone = {m["phone"]: m for m in (existing.get("members") or [])}
with open(MJSON) as f: data = json.load(f)
with open(RJSON) as f: roster = json.load(f)
members = data.get("members", [])
by_phone = {m["phone"]: m for m in members}

enriched = phantomed = 0
for r in roster.get("entries", []):
    phone = r["phone"]
    m = by_phone.get(phone)
    if m is None:
        prev = existing_by_phone.get(phone) or {}
        ph = {
            "phone": phone,
            "auto_name": "",
            "name": prev.get("name") or r["nama"],
            "user_labeled": True,
            "groups": [],
            "group_subjects": [],
            "appearance_count": 0,
            "roster_source": True,
            "panggilan": r["panggilan"],
            "posisi": r["posisi"],
            "cabang": r["cabang"],
        }
        # Preserve user notes from prior snapshot
        if prev.get("notes"):
            ph["notes"] = prev["notes"]
        members.append(ph)
        phantomed += 1
    else:
        m["roster_source"] = True
        m["panggilan"] = r["panggilan"]
        m["posisi"] = r["posisi"]
        m["cabang"] = r["cabang"]
        # If current name is empty or just the auto-detected pushName, prefer roster nama.
        # Preserve real user-labeled overrides (where user set a different name on purpose).
        if not m.get("user_labeled") or not m.get("name"):
            m["name"] = r["nama"]
            m["user_labeled"] = True
        enriched += 1

# Sort: real members first (by appearance desc), phantoms at bottom
members.sort(key=lambda x: (1 if x.get("appearance_count", 0) == 0 else 0, -x.get("appearance_count", 0)))
data["members"] = members
data["roster_merge_at"] = datetime.datetime.now().isoformat(timespec="seconds")
data["roster_summary"] = {"enriched": enriched, "phantomed": phantomed, "roster_total": len(roster.get("entries", []))}
tmp = MJSON + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
os.replace(tmp, MJSON)
print(f"  [roster-merge] enriched={enriched} phantomed={phantomed}")
PYEOF
else
  echo "  [roster-merge] skip — $ROSTER_PATH not found"
fi

TOTAL=$(jq '.members | length' "$OUT_JSON")
LABELED=$(jq '[.members[] | select(.name != "" and .name != .auto_name)] | length' "$OUT_JSON")
PHANTOMS=$(jq '[.members[] | select(.appearance_count == 0)] | length' "$OUT_JSON")
echo "Total distinct phones: $TOTAL"
echo "Manually labeled (overriding auto): $LABELED"
echo "Roster phantoms (not yet in any group): $PHANTOMS"

# Step 4 — markdown output for review
{
  echo "# Members Directory"
  echo ""
  echo "_Generated: $(date '+%Y-%m-%d %H:%M WIB') · $TOTAL distinct phones across $GROUP_COUNT groups_"
  echo ""
  echo "Edit \`data/members.json\` field \`name\` untuk override auto-detected name."
  echo "Format JSON: \`{\"phone\":\"+62...\", \"name\":\"Pak Anu\", ...}\` — script ini idempotent, ulang-jalankan untuk merge."
  echo ""
  echo "## Member List (sorted by appearance count; phantoms = roster-only di akhir)"
  echo ""
  echo "| Phone | Auto Name | Current Name | Panggilan | Posisi | Cabang | Groups | Appearances |"
  echo "|---|---|---|---|---|---|---:|---:|"
  jq -r '
    .members[]
    | "| `\(.phone)` | \(if .auto_name == "" then "—" else .auto_name end) | \(if .roster_source then "**" + .name + "** 📋" elif .user_labeled then "**" + .name + "**" elif .name == "" then "_(unlabeled)_" else .name end) | \(.panggilan // "—") | \(.posisi // "—") | \(.cabang // "—") | \(.groups | length) | \(.appearance_count) |"
  ' "$OUT_JSON"
  echo ""
  echo "## Sender Display Names (from captured messages)"
  echo ""
  echo "These are pushName values seen in chat — useful kalau auto_name di atas kosong."
  echo ""
  echo "| Display Name | Msg Count | Groups |"
  echo "|---|---:|---|"
  jq -r '
    .sender_names_from_messages[]
    | "| \(.sender_name) | \(.count) | \(.groups | length) |"
  ' "$OUT_JSON"
} > "$OUT_MD"

echo "Wrote $OUT_MD"
echo ""
echo "Review: $OUT_MD"
echo "Edit names in: $OUT_JSON"
