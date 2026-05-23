# WRG Monitor

Internal dashboard + AI summarization untuk WhatsApp groups PT Wahana Rizky Gumilang (WRG). Auto-rekap pesan tiap 5 jam, daily resume eksekutif jam 22:10, group-pattern profiling, dan members directory dengan roster integration.

## Dashboard

Akses lokal: <http://localhost:8090>

### Bookmark Deep-Link

URL mendukung `?date=YYYY-MM-DD` (opsional) + `#tab` (opsional, salah satu dari `overview`/`rekap`/`resume`/`members`/`pola`). Default = today + overview.

| URL | Apa yang dibuka |
|---|---|
| `localhost:8090/` | Today, tab Overview |
| `localhost:8090/#rekap` | Today, tab Rekap |
| `localhost:8090/#members` | Tab Members directory |
| `localhost:8090/?date=2026-05-22#resume` | Resume tanggal 22 Mei 2026 |
| `localhost:8090/?date=2026-05-20#rekap` | Rekap historis 20 Mei 2026 |
| `localhost:8090/#pola` | Group communication-pattern profiles |

URL auto-update via `history.replaceState` saat ganti tab/date — bookmark current state kapanpun. URL pula bersih untuk default (today + overview = `/`).

## Component

- `scripts/dashboard.py` — single-file Python ThreadingTCPServer, port 8090
- `scripts/rekap.sh` — 5-hourly rekap + 14:00/22:10 resume via OpenRouter AI
- `scripts/list_members.sh` — daily 22:30 rebuild members.json dari openclaw sessions + roster merge
- `scripts/pola_komunikasi.sh` — daily 23:30 group profiling
- `scripts/git_backup_push.sh` — daily 22:40 auto-push `data/members.{json,md}` + `data/roster.json` ke GitHub
- `data/roster.json` — canonical roster (62 entries) — source of truth nama/panggilan/posisi/cabang

## Cron Schedule

```
00 7,12,17,22 * * *  rekap.sh rekap          # 4x/hari rekap kolektif
00 14 * * *           rekap.sh resume         # 14:00 mid-day resume
10 22 * * *           rekap.sh resume         # 22:10 end-of-day resume
05 14,15 22 * * *     notif_tua.sh            # TUA alert
30 22 * * *           list_members.sh         # rebuild members.json
40 22 * * *           git_backup_push.sh      # auto-push ke github
30 23 * * *           pola_komunikasi.sh      # group pattern profile
```

## Repo & Versioning

- GitHub: <https://github.com/DevWRG/wrg-monitor> (private)
- Auto-push members snapshot daily 22:40 (lihat `scripts/git_backup_push.sh`)
- Token auto-refresh dari `gh auth token` ke `~/.config/wrg-monitor/gh-token` (cron-safe, no manual rotation)
