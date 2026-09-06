#!/usr/bin/env bash
# Draait backfill-sttr-xml tot het venster sluit, en herstart na een
# infrastructuur-hik (Docker-VM omgevallen, DB-connectie weg).
# i2a.sttr_bestand is het checkpoint: herstarten kost alleen de ~122
# lijst-calls om terug te lopen naar waar hij was.
#
# Stopt definitief bij:
#   - budget bereikt of alles binnen (exit 0, geen groei meer)
#   - einde venster (06:00)
#   - DienstWijktAf (herhaalde 503) -- dan is de dienst zelf het probleem
#     en moeten we NIET automatisch terugkomen.
set -u
cd "$(dirname "$0")/.."

EIND_UUR=${EIND_UUR:-6}
MAX_HERSTART=${MAX_HERSTART:-6}
BUDGET=${BUDGET:-25000}

herstart=0
while :; do
  u=$(date +%H); u=${u#0}; u=${u:-0}
  if [ "$u" -ge "$EIND_UUR" ] && [ "$u" -lt 22 ]; then
    echo "[$(date +%H:%M)] venster gesloten -- gestopt"; break
  fi

  echo "[$(date +%H:%M)] poging $((herstart+1)) start"
  PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m src.cli \
      backfill-sttr-xml --tempo 1.0 --budget "$BUDGET" --overdag \
      > "scripts/.backfill_poging_$((herstart+1)).log" 2>&1
  rc=$?
  tail -3 "scripts/.backfill_poging_$((herstart+1)).log"

  # LET OP: de loader vangt DienstWijktAf zelf af en eindigt dus met rc=0.
  # De exitcode zegt daarom niets over of de dienst ons heeft weggestuurd --
  # dat staat alleen in de log, achter "Gestopt:".
  if grep -q "Gestopt:" "scripts/.backfill_poging_$((herstart+1)).log"; then
    echo "[$(date +%H:%M)] dienst wijkt af -- NIET herstarten"; break
  fi
  if [ $rc -eq 0 ]; then
    echo "[$(date +%H:%M)] netjes klaar (rc=0)"; break
  fi

  herstart=$((herstart+1))
  if [ "$herstart" -ge "$MAX_HERSTART" ]; then
    echo "[$(date +%H:%M)] $MAX_HERSTART herstarts op -- gestopt"; break
  fi
  echo "[$(date +%H:%M)] rc=$rc (infra) -- 120s pauze, dan verder"
  sleep 120
done
