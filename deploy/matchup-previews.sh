#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# StatChat matchup preview generation — runs midday to generate
# tonight's matchup preview cards in the notable events feed.
# ──────────────────────────────────────────────────────────────────────

set -uo pipefail

set -a
source /opt/statchat/.env
set +a

VENV=/opt/statchat/venv/bin/python3
DB=/data/baseball_stats_full.db

$VENV -c "
import sqlite3, sys, os
sys.path.insert(0, '/opt/statchat/repo/backend')
from services.notable_events import detect_matchup_previews
from datetime import date

conn = sqlite3.connect('$DB')
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA busy_timeout=5000')

season = date.today().year
events = detect_matchup_previews(conn, season)
print(f'Generated {len(events)} matchup previews')

for e in events:
    conn.execute('''
        INSERT OR IGNORE INTO notable_events
        (detection_type, game_date, headline, description, player_names, priority)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (e['detection_type'], e['game_date'], e['headline'],
          e.get('description', ''), e.get('player_names', ''), e.get('priority', 5)))
    print(f'  {e[\"headline\"]}')

conn.commit()
conn.close()
"
