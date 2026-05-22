#!/bin/bash
# ============================================================
# WRG Monitor — Konfigurasi Utama
# Edit bagian ini sesuai setup lo, lalu simpan
# Semua script lain source file ini — cukup edit di sini saja
# ============================================================

# Direktori output (semua rekap, resume, briefing disimpan di sini)
# Resolve dari lokasi file ini supaya project bisa dipindah tanpa edit config.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/logs"

# Nama perusahaan & direktur (untuk header dokumen)
NAMA_PERUSAHAAN="PT Wahana Rizky Gumilang (WRG)"
NAMA_DIREKTUR="Pak Gilang"

# Konteks bisnis untuk AI — makin spesifik makin akurat rekap-nya
KONTEKS_BISNIS="perusahaan yang bergerak di bidang IVD, alat kesehatan, farmasi.
Grup-grup ini berisi tim sales, teknisi, finance, accounting dan operasional yang membahas prospek, pipeline deals,
follow-up klien, koordinasi pengiriman/proyek, dan administrasi operasional harian."

# Level thinking AI (low | medium | high)
# high = lebih akurat tapi lebih lambat & mahal. Rekomendasi: high untuk 12jam/briefing
THINKING_REKAP="low"
THINKING_RESUME="low"
THINKING_BRIEFING="low"

# ── Model selection per task ─────────────────────────────────
# Format: openrouter/<provider>/<model>. Primary di-coba dulu;
# kalau empty/error, fallback ke yang kedua.
# Strategi: fast non-reasoning model = primary (cepat + output reliable),
# reasoning model (deepseek-r1) = fallback (kalau primary fail / quota).
REKAP_MODEL_PRIMARY="openrouter/google/gemini-2.5-flash-lite"
REKAP_MODEL_FALLBACK="openrouter/deepseek/deepseek-r1"

RESUME_MODEL_PRIMARY="openrouter/anthropic/claude-haiku-4.5"
RESUME_MODEL_FALLBACK="openrouter/deepseek/deepseek-r1"

POLA_MODEL_PRIMARY="openrouter/anthropic/claude-haiku-4.5"
POLA_MODEL_FALLBACK="openrouter/deepseek/deepseek-r1"

BRIEFING_MODEL_PRIMARY="openrouter/anthropic/claude-haiku-4.5"
BRIEFING_MODEL_FALLBACK="openrouter/deepseek/deepseek-r1"

# Direct OpenRouter API call — bypass openclaw agent layer karena:
# (a) gateway block --model override (auth scope), (b) infer model run bug parsing
# reasoning models. API key dibaca dari auth-profiles.json yang sama dipakai openclaw.
#
# Args: $1=model_id (provider/model atau openrouter/provider/model), $2=prompt, $3=max_tokens (default 16000)
call_openrouter() {
  local MODEL="$1"
  local PROMPT="$2"
  local MAX_TOKENS="${3:-16000}"

  # Strip "openrouter/" prefix kalau ada
  MODEL="${MODEL#openrouter/}"

  local KEY="${OPENROUTER_KEY:-$(jq -r '.profiles."openrouter:default".key // empty' ~/.openclaw/agents/main/agent/auth-profiles.json 2>/dev/null)}"
  [ -z "$KEY" ] && { echo "[ai] OPENROUTER_KEY not found" >&2; return 1; }

  local PAYLOAD
  PAYLOAD=$(jq -nc \
    --arg model "$MODEL" \
    --arg prompt "$PROMPT" \
    --argjson maxt "$MAX_TOKENS" \
    '{model: $model, messages: [{role: "user", content: $prompt}], max_tokens: $maxt}')

  local RESP
  RESP=$(curl -sS --max-time 180 -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>>"$LOG_DIR/error.log")

  # Cek error dulu
  local ERR
  ERR=$(printf '%s' "$RESP" | jq -r '.error.message // empty' 2>/dev/null)
  if [ -n "$ERR" ]; then
    echo "[ai] [$MODEL] error: $ERR" >>"$LOG_DIR/error.log"
    return 1
  fi

  printf '%s' "$RESP" | jq -r '.choices[0].message.content // empty' 2>/dev/null
}

# Helper: try primary model, fallback to secondary if empty/error.
# Args: $1=primary, $2=fallback, $3=prompt, $4=thinking_ignored (kept for backcompat)
call_ai_with_fallback() {
  local PRIMARY="$1"
  local FALLBACK="$2"
  local PROMPT="$3"
  # $4 (thinking) ignored — direct API doesn't use openclaw thinking flag

  local OUT
  OUT=$(call_openrouter "$PRIMARY" "$PROMPT" 16000)
  if [ -n "$OUT" ] && [ "${#OUT}" -gt 50 ]; then
    printf '%s' "$OUT"
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] [ai-fallback] primary '$PRIMARY' returned short/empty, retry '$FALLBACK'..." | tee -a "$LOG_DIR/wrg-monitor.log" >&2
  OUT=$(call_openrouter "$FALLBACK" "$PROMPT" 16000)
  printf '%s' "$OUT"
  [ -n "$OUT" ]
}
