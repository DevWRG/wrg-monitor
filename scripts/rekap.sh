#!/bin/bash
# ============================================================
# WRG Monitor — Rekap & Resume (v4)
# Data source raw : ~/.openclaw/tmp/wrg-monitor/messages/<date>/<jid>.jsonl
# Data source rekap: data/rekap/<date>/rekap_*.txt (for resume mode)
#
# Modes:
#   bash rekap.sh rekap    -> 3-hour window dari JSONL → ringkasan
#   bash rekap.sh resume   -> 7-hour window dari rekap files → resume eksekutif
#
# Window dijalankan 07:00-22:00 WIB lewat cron (lihat setup.sh).
# ============================================================

set -e
source "$(dirname "$0")/../config/config.sh"

MODE="${1:-rekap}"
BOT_NOMOR="+6285168121906"
MESSAGES_DIR="$DATA_DIR/messages"

# WRG_FAKE_NOW="YYYY-MM-DD HH:MM" overrides "now" — used by backfill.sh untuk
# regenerate rekap historis seolah-olah dijalankan di waktu tersebut.
if [ -n "$WRG_FAKE_NOW" ]; then
  # macOS BSD date
  NOW_S=$(date -j -f "%Y-%m-%d %H:%M" "$WRG_FAKE_NOW" "+%s" 2>/dev/null \
        || date -d "$WRG_FAKE_NOW" "+%s")
  TANGGAL=$(echo "$WRG_FAKE_NOW" | awk '{print $1}')
  JAM=$(echo "$WRG_FAKE_NOW" | awk '{print $2}')
  TIMESTAMP="${TANGGAL}_$(echo "$JAM" | tr -d ':')"
else
  NOW_S=$(date +%s)
  TANGGAL=$(date '+%Y-%m-%d')
  JAM=$(date '+%H:%M')
  TIMESTAMP=$(date '+%Y-%m-%d_%H%M')
fi
NOW_MS=$((NOW_S * 1000))

# Yesterday relative to TANGGAL (works for both real & fake now)
if date --version &>/dev/null 2>&1; then
  YESTERDAY=$(date -d "$TANGGAL -1 day" '+%Y-%m-%d')
else
  YESTERDAY=$(date -j -v-1d -f "%Y-%m-%d" "$TANGGAL" "+%Y-%m-%d")
fi

mkdir -p "$LOG_DIR" "$DATA_DIR/rekap/$TANGGAL" "$DATA_DIR/resume/$TANGGAL"
log() { echo "[$(date '+%H:%M:%S')] [$MODE] $1" | tee -a "$LOG_DIR/wrg-monitor.log"; }

# ── Member directory: phone → name substitution table ────────
# Hanya include members yang punya name terisi (auto_name dari pushName,
# atau user-edited override). AI pakai untuk substitusi nomor → nama.
build_members_table() {
  local FILE="$DATA_DIR/members.json"
  [ ! -f "$FILE" ] && return
  jq -r '
    .members[]
    | select(.name != null and .name != "")
    | "• \(.phone) → \(.name)"
  ' "$FILE" 2>/dev/null
}
MEMBERS_TABLE=$(build_members_table)
MEMBERS_COUNT=$(echo "$MEMBERS_TABLE" | grep -c "^•" 2>/dev/null || echo 0)

# ── Group directory: JID → nama grup (auto-detected + user override) ───
# AI pakai untuk substitusi JID → nama grup di header per-grup output.
# Merge: group_directory (auto) + group_directory_user (manual). User wins.
build_group_directory() {
  local FILE="$DATA_DIR/members.json"
  [ ! -f "$FILE" ] && return
  jq -r '
    ((.group_directory_user // {}) as $u
    | (.group_directory // {}) as $a
    | ($a + $u)
    | to_entries[]
    | select(.value != "" and .value != null)
    | "• \(.key) → \(.value)")
  ' "$FILE" 2>/dev/null
}
GROUP_DIRECTORY=$(build_group_directory)
GROUP_DIR_COUNT=$(echo "$GROUP_DIRECTORY" | grep -c "^•" 2>/dev/null || echo 0)

# ── Ignored groups: Research grup di-skip dari rekap & resume ──
# Match by name (regex /research/i) ATAU JID hardcoded. JID hardcoded
# menjaga grup Research yang belum di-label tetap ke-filter.
build_ignored_jids() {
  local FILE="$DATA_DIR/members.json"
  echo "120363409252019573@g.us"
  [ ! -f "$FILE" ] && return
  jq -r '
    ((.group_directory_user // {}) as $u
    | (.group_directory // {}) as $a
    | ($a + $u)
    | to_entries[]
    | select(.value != null and (.value | test("research"; "i")))
    | .key)
  ' "$FILE" 2>/dev/null
}
IGNORED_JIDS_JSON=$(build_ignored_jids | jq -R . | jq -sc 'unique')
IGNORED_COUNT=$(echo "$IGNORED_JIDS_JSON" | jq 'length')

# ── Resolve mode ──────────────────────────────────────────────
case "$MODE" in
  rekap|2jam|3jam)  MODE_KIND="rekap" ;;
  resume|7jam|12jam) MODE_KIND="resume" ;;
  *)
    echo "Usage: $0 [rekap|resume]"
    exit 1
    ;;
esac

# ── REKAP: 5 jam dari JSONL (extended dari 3h supaya 4 firings/day cover full active window) ──
if [ "$MODE_KIND" = "rekap" ]; then
  WINDOW_MS=$((5 * 60 * 60 * 1000))
  SINCE_MS=$((NOW_MS - WINDOW_MS))

  if [ ! -d "$MESSAGES_DIR" ]; then
    log "ERROR: $MESSAGES_DIR not found. Patch openclaw belum aktif?"
    exit 1
  fi

  CANDIDATES=$(ls "$MESSAGES_DIR/$TANGGAL"/*.jsonl 2>/dev/null; ls "$MESSAGES_DIR/$YESTERDAY"/*.jsonl 2>/dev/null) || true
  if [ -z "$CANDIDATES" ]; then
    log "Belum ada data pesan di window. Skip."
    exit 0
  fi

  log "Membaca $(echo "$CANDIDATES" | wc -l | tr -d ' ') file pesan, filter sejak ts_ms >= $SINCE_MS"

  SEMUA_PESAN=$(echo "$CANDIDATES" | xargs cat 2>/dev/null | jq -rc \
      --argjson since "$SINCE_MS" \
      --argjson ignored "$IGNORED_JIDS_JSON" '
    select(.ts_ms >= $since)
    | select(.chat_type == "group")
    | select(.group_jid as $j | $ignored | index($j) | not)
    | { jid: .group_jid, ts: .ts_ms, sender: (.sender_name // .sender), body: (.body // "<no-body>"), media: .media_type }
  ' | jq -sc 'sort_by(.ts) | .[] | "[\(.jid)] [\(.ts | tostring)] \(.sender): \(.body)\(if .media then " <media:\(.media)>" else "" end)"' \
    | sed 's/^"//;s/"$//')

  if [ "$IGNORED_COUNT" -gt 0 ]; then
    log "Filter $IGNORED_COUNT grup Research/ignored dari window: $(echo "$IGNORED_JIDS_JSON" | jq -r 'join(", ")')"
  fi

  JUMLAH=$(echo "$SEMUA_PESAN" | grep -c "^\[" 2>/dev/null || true)
  GRUP_AKTIF=$(echo "$SEMUA_PESAN" | grep -oE '^\[[^]]+\]' | sort -u | wc -l | tr -d ' ')

  if [ -z "$SEMUA_PESAN" ] || [ "$JUMLAH" = "0" ]; then
    log "Tidak ada pesan dalam window."
    exit 0
  fi

  log "$JUMLAH pesan dari $GRUP_AKTIF grup. Kirim ke AI..."

  # ── Inject pola profiles for active groups ─────────────────
  # For each active JID in window, if data/pola/<jid>.md exists, include it as context.
  ACTIVE_JIDS=$(echo "$SEMUA_PESAN" | grep -oE '^\[[^]]+\]' | sed 's/^\[//;s/\]$//' | sort -u)
  POLA_CONTEXT=""
  POLA_COUNT=0
  while IFS= read -r JID; do
    [ -z "$JID" ] && continue
    POLA_FILE="$DATA_DIR/pola/${JID}.md"
    if [ -f "$POLA_FILE" ]; then
      POLA_CONTEXT="${POLA_CONTEXT}

=== POLA GRUP: ${JID} ===
$(cat "$POLA_FILE")
"
      POLA_COUNT=$((POLA_COUNT + 1))
    fi
  done <<< "$ACTIVE_JIDS"
  if [ "$POLA_COUNT" -gt 0 ]; then
    log "Inject pola profile untuk $POLA_COUNT dari $GRUP_AKTIF grup aktif"
  fi
  if [ "$MEMBERS_COUNT" -gt 0 ]; then
    log "Inject directory $MEMBERS_COUNT labeled members"
  fi
  if [ "$GROUP_DIR_COUNT" -gt 0 ]; then
    log "Inject directory $GROUP_DIR_COUNT named groups"
  fi

  # Cache-friendly prompt order — stable prefix di atas (members, pola, format spec),
  # variable content (messages) di akhir. Provider yang support prompt caching
  # bisa cache prefix across back-to-back firings, save up to ~70% input cost.
  PROMPT="Kamu adalah asisten internal ${NAMA_PERUSAHAAN}.
Konteks: ${KONTEKS_BISNIS}

============================================
DIREKTORI GRUP (substitusi JID grup → nama grup saat output: kalau JID di pesan
muncul di list ini, GANTI dengan nama grup di header section per-grup; kalau tidak
ada di list, pakai JID apa adanya):
${GROUP_DIRECTORY:-(belum ada group_directory)}

============================================
DIREKTORI MEMBER (substitusi nomor telpon ke nama saat output: kalau nomor di pesan
muncul di list ini, GANTI dengan nama; kalau tidak ada di list, pakai nomor seperti
apa adanya — JANGAN tebak nama):
${MEMBERS_TABLE:-(belum ada members.json)}

============================================
PROFILE POLA KOMUNIKASI per-grup (gunakan sebagai panduan style & ekstraksi info per grup; jika kosong/tidak ada untuk suatu grup, pakai default sense):
${POLA_CONTEXT:-(belum ada profile pola)}

============================================
TUGAS: Buat REKAP RINGKAS dengan struktur EKSAK seperti di bawah.

PER GRUP — daftar poin penting + ACTION items (PIC + tugas + deadline kalau ada).

DETEKSI KONFIRMASI: untuk setiap REQUEST/APPROVAL/PERTANYAAN yang ditujukan ke PIC tertentu
(via @mention, panggilan langsung 'pak X', 'bu Y', atau request kolektif '@all'), match dengan
reply dari PIC tersebut DALAM SAMA WINDOW. Pola reply yang dihitung sebagai konfirmasi:
- Eksplisit: 'OK', 'siap', 'noted', 'setuju', 'ya', 'bisa', 'jalan', 'lanjut', 'acc'
- Action-taken: PIC sudah mulai/selesai tugas yang diminta
- Quote-reply ke message tersebut dengan jawaban substantif
Pola yang TIDAK dihitung sebagai konfirmasi: emoji reaction saja, 'oh', 'siap nanti', 'akan dicek', 'menyusul'.

Format output EKSAK:

REKAP WRG | ${JAM} WIB | ${TANGGAL}
============================================
[nama grup dari DIREKTORI GRUP — kalau JID ada di direktori, WAJIB pakai nama; kalau tidak ada, tulis JID apa adanya]
• poin penting
→ ACTION: [PIC] - [tugas] [deadline jika ada]

============================================
KONFIRMASI STATUS (5 jam terakhir)

✓ SUDAH DIKONFIRMASI:
• [topik singkat] | dari: [requester] | ke: [PIC] | confirm by: [siapa yang reply] @ [jam]
(jika tidak ada, tulis 'Tidak ada')

⏳ MENUNGGU KONFIRMASI:
• [topik singkat] | dari: [requester] | ke: [PIC yang dituju, atau '@all'] | sejak: [jam request] | status: [reason kalau tahu, misal 'belum dijawab' / 'menyusul / 'pending data']
(jika tidak ada, tulis 'Tidak ada')

============================================
URGENT: [item urgent, atau 'Tidak ada']
GRUP AKTIF: ${GRUP_AKTIF} dari $(ls "$MESSAGES_DIR/$TANGGAL" 2>/dev/null | wc -l | tr -d ' ') grup hari ini

============================================
DATA INPUT (paling akhir — stable prefix di atas bisa di-cache provider):

Pesan dari ${GRUP_AKTIF} grup WhatsApp WRG (5 jam terakhir, urut waktu, format [grup_jid] [timestamp_ms] sender: body):

${SEMUA_PESAN}"

  OUT="$DATA_DIR/rekap/$TANGGAL/rekap_${TIMESTAMP}.txt"
  HASIL=$(call_ai_with_fallback "$REKAP_MODEL_PRIMARY" "$REKAP_MODEL_FALLBACK" "$PROMPT" "$THINKING_REKAP")
  if [ -z "$HASIL" ]; then
    log "ERROR: AI tidak return apa-apa (primary+fallback gagal). Cek $LOG_DIR/error.log"
    bash "$(dirname "$0")/notif_quota.sh" >/dev/null 2>&1 &
    exit 1
  fi
  # Dedup yang safe: kalau header 'REKAP WRG |' muncul >1×, ambil section TERAKHIR yang
  # punya footer valid (mengandung 'GRUP AKTIF:'). Kalau tidak ada section utuh, pakai raw.
  HASIL=$(printf '%s' "$HASIL" | python3 -c '
import sys, re
s = sys.stdin.read()
positions = [m.start() for m in re.finditer(r"REKAP WRG \|", s)]
if len(positions) <= 1:
    sys.stdout.write(s); sys.exit()
for i in range(len(positions) - 1, -1, -1):
    section = s[positions[i] : positions[i+1] if i+1 < len(positions) else len(s)]
    if "GRUP AKTIF:" in section:
        sys.stdout.write(section); sys.exit()
sys.stdout.write(s)
')
  echo "$HASIL" > "$OUT"
  log "Selesai. Disimpan: $OUT ($(echo "$HASIL" | wc -c | tr -d ' ') bytes)"
  exit 0
fi

# ── RESUME: 7 jam dari REKAP files ────────────────────────────
if [ "$MODE_KIND" = "resume" ]; then
  WINDOW_S=$((7 * 60 * 60))
  CUTOFF_S=$((NOW_S - WINDOW_S))

  REKAP_FILES=$(ls "$DATA_DIR/rekap/$TANGGAL"/rekap_*.txt 2>/dev/null; ls "$DATA_DIR/rekap/$YESTERDAY"/rekap_*.txt 2>/dev/null) || true

  if [ -z "$REKAP_FILES" ]; then
    log "Belum ada rekap di window. Skip."
    exit 0
  fi

  # Filter rekap files by mtime (mtime within last 7h)
  RECENT_REKAPS=""
  for f in $REKAP_FILES; do
    [ -f "$f" ] || continue
    MTIME=$(stat -f %m "$f")
    if [ "$MTIME" -ge "$CUTOFF_S" ]; then
      RECENT_REKAPS="$RECENT_REKAPS $f"
    fi
  done

  RECENT_REKAPS=$(echo "$RECENT_REKAPS" | xargs -n1 | sort -u)
  if [ -z "$RECENT_REKAPS" ]; then
    log "Tidak ada rekap baru dalam 7 jam. Skip."
    exit 0
  fi

  JUMLAH=$(echo "$RECENT_REKAPS" | wc -l | tr -d ' ')
  log "Menggabungkan $JUMLAH rekap (mtime < ${WINDOW_S}s lalu)"

  GABUNGAN=""
  for f in $RECENT_REKAPS; do
    HEAD=$(basename "$f" | sed 's/rekap_//;s/.txt//;s/_/ /')
    GABUNGAN="${GABUNGAN}"$'\n\n--- '"${HEAD}"$' ---\n'"$(cat "$f")"
  done

  # Cache-friendly order: stable prefix (members + tasks + format) di atas, GABUNGAN (variable) di akhir.
  PROMPT="Kamu adalah parser & summarizer internal ${NAMA_PERUSAHAAN}.

ATURAN OUTPUT (WAJIB):
- Output PLAIN TEXT saja. JANGAN pakai markdown header ## atau ###. JANGAN pakai tabel pipe |.
- JANGAN bikin format 'BRIEFING DIREKTUR' atau struktur A/B/C/D/E/F.
- Output WAJIB diawali tepat dengan baris: 'RESUME EKSEKUTIF WRG'
- Pakai EKSAK 8 section bernomor: 1., 2., 3., 4., 5., 6., 7., 8.
- Section 7 dan 8 WAJIB ada walau isinya 'Tidak ada'.
- Section header format: '1. SITUASI UMUM' (tanpa ##, tanpa bold). Bullet pakai '•' (bukan '-' atau '*').

Konteks: ${KONTEKS_BISNIS}

============================================
DIREKTORI GRUP (substitusi JID → nama grup saat output: kalau JID di rekap muncul di
list ini, GANTI dengan nama grup; kalau tidak ada di list, pakai JID apa adanya):
${GROUP_DIRECTORY:-(belum ada group_directory)}

============================================
DIREKTORI MEMBER (substitusi nomor telpon ke nama saat output: kalau nomor di rekap
muncul di list ini, GANTI dengan nama; kalau tidak ada di list, pakai nomor seperti
apa adanya — JANGAN tebak nama):
${MEMBERS_TABLE:-(belum ada members.json)}

============================================
STAKEHOLDER LIST untuk routing di section 7 (DIREKTUR) dan section 8 (HOD):
- DIREKTUR (${NAMA_DIREKTUR}): keputusan strategis, eskalasi, deal besar, klien VIP, konflik lintas-dept
- HOD Business IVD: reagen lab, diagnostic kit (vacullab/gem/probest/intec dll)
- HOD Business Medical: alat medis non-diagnostik, hospital equipment
- HOD Sales West Indonesia Area: Jakarta, Banten, Jabar, Jateng, Sumatera, Kalbar
- HOD Sales East Indonesia Area: Jatim, Bali, NTT, NTB, Sulawesi, Maluku, Papua, Kaltim/Kalsel
- HOD Aftersales: keluhan klien, service kontrak, repair/warranty
- HOD Finance & Supply Chain: faktur, pembayaran, warehouse, stok, pengiriman
- HOD Accounting: tax, faktur pajak, jurnal, audit
- HOD Business Development & General Affair: training, HR, event, partnership, GA

============================================
TUGAS: Sintesis jadi resume operasional dengan format EKSAK di bawah. Hilangkan duplikasi, konsolidasi action items, naik level abstraksi.

PENTING — TRACKING KONFIRMASI lintas rekap:
- Tarik semua entri 'MENUNGGU KONFIRMASI' dari setiap rekap.
- Cek rekap-rekap berikutnya: apakah item itu di-confirm di window berikutnya? Jika ya, MOVE ke 'TERKONFIRMASI BARU'.
- Jika sampai rekap terakhir masih pending, MASUK ke 'OUTSTANDING — perlu follow-up'.
- Hitung berapa lama item sudah pending (selisih waktu request vs sekarang).
- Tandai item dengan umur >4 jam sebagai 'TUA' supaya prioritas follow-up.

Format output EKSAK:

RESUME EKSEKUTIF WRG
${TANGGAL} | ${JAM} WIB | 7 Jam Terakhir (dari ${JUMLAH} rekap)
============================================
1. SITUASI UMUM
[2-3 kalimat]

2. PIPELINE & SALES UPDATE
[deal maju, prospek baru, follow-up klien]

3. ACTION ITEMS OUTSTANDING
[• PIC → tugas | deadline | status]

4. KONFIRMASI TRACKING
✓ TERKONFIRMASI BARU (request di rekap awal → confirmed di rekap berikutnya):
• [topik] | requester → PIC | request jam X, confirm jam Y (lag Z menit)

⏳ OUTSTANDING — masih menunggu (urutkan dari paling tua):
• [topik] | dari [requester] | ke [PIC/grup] | sejak jam X (umur Y jam Z menit) [TUA jika >4 jam] | status: [reason]
(jika kosong, tulis 'Tidak ada')

5. KENDALA & ISU OPERASIONAL
[masalah belum resolved]

6. KEPUTUSAN YANG SUDAH DIAMBIL
[keputusan final lintas grup]

7. UNTUK DIBAHAS DENGAN ${NAMA_DIREKTUR}
[topik butuh arahan direktur, prioritas tinggi dulu — termasuk item OUTSTANDING TUA]
• [topik singkat] — [konteks 1-2 kalimat] — [butuh: keputusan/arahan/eskalasi]
(jika tidak ada item, tulis 'Tidak ada')

8. UNTUK HOD (Head of Department)
[items operasional yang relevan untuk HOD masing-masing. Format bullet:
'• [HOD <nama-hod>] <topik singkat> — <konteks/aksi yang perlu>']
• [HOD Business IVD] <item>
• [HOD Sales East Indonesia Area] <item>
• [HOD Finance & Supply Chain] <item>
(satu item bisa satu HOD; ikut prefix [HOD ...] persis seperti di STAKEHOLDER LIST.
Tulis sebanyak yang relevan. Jika kosong total, tulis 'Tidak ada')

============================================
Generated: $(date '+%Y-%m-%d %H:%M') WIB

============================================
DATA INPUT (paling akhir — stable prefix di atas bisa di-cache provider):

Berikut ${JUMLAH} rekap WhatsApp WRG dari 7 jam terakhir (${TANGGAL}):

${GABUNGAN}"

  OUT="$DATA_DIR/resume/$TANGGAL/resume_${TIMESTAMP}.txt"
  HASIL=$(call_ai_with_fallback "$RESUME_MODEL_PRIMARY" "$RESUME_MODEL_FALLBACK" "$PROMPT" "$THINKING_RESUME")
  if [ -z "$HASIL" ]; then
    log "ERROR: AI tidak return apa-apa (primary+fallback gagal). Cek $LOG_DIR/error.log"
    bash "$(dirname "$0")/notif_quota.sh" >/dev/null 2>&1 &
    exit 1
  fi
  # Dedup yang safe (sama dengan rekap mode): ambil section terakhir yang punya footer 'Generated:'.
  HASIL=$(printf '%s' "$HASIL" | python3 -c '
import sys, re
s = sys.stdin.read()
positions = [m.start() for m in re.finditer(r"RESUME EKSEKUTIF WRG", s)]
if len(positions) <= 1:
    sys.stdout.write(s); sys.exit()
for i in range(len(positions) - 1, -1, -1):
    section = s[positions[i] : positions[i+1] if i+1 < len(positions) else len(s)]
    if "Generated:" in section:
        sys.stdout.write(section); sys.exit()
sys.stdout.write(s)
')
  echo "$HASIL" > "$OUT"
  log "Selesai. Disimpan: $OUT ($(echo "$HASIL" | wc -c | tr -d ' ') bytes)"
  exit 0
fi
