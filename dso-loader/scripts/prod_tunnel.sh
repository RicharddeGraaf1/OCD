#!/usr/bin/env bash
# Opent de SSH-tunnel naar de OCD-productiedatabase op apps-1 (Hetzner, Coolify).
#
# De database heeft geen publieke poort: ocd-postgis luistert op poort 5433 van
# de server en de Hetzner Cloud Firewall laat die niet door. Deze tunnel brengt
# hem naar 127.0.0.1:15433 op je laptop (5433 en 5434 zijn lokaal al bezet).
# PROD_DB_URL in dso-loader/.env wijst daarheen. Laat de tunnel open zolang de
# sync of een replicatiescript draait; Ctrl+C sluit hem.
#
# Sleutel: OCD_TUNNEL_SLEUTEL, standaard die van Richard. Een Claude-sessie
# gebruikt ~/.ssh/claude-richard-locuslegis-hetzner.
# Zie docs/railway-naar-hetzner-plan.md, B2.
#
# Valt de verbinding weg, dan bouwt de lus hem na 5 s opnieuw op. Op 2026-10-29
# om 04:14 viel de tunnel midden in de sync ("Connection reset by peer"); zonder
# lus faalt dan de volgende stap die prod raakt.
set -uo pipefail
SLEUTEL="${OCD_TUNNEL_SLEUTEL:-$HOME/.ssh/locuslegis-hetzner}"
echo "Tunnel 127.0.0.1:15433 -> apps-1:5433 (ocd-postgis). Ctrl+C om te sluiten."
while true; do
    ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
        -i "$SLEUTEL" -L 15433:127.0.0.1:5433 root@2.31.94.124
    echo "[$(date +%T)] tunnel weg (exit $?), opnieuw over 5 s"
    sleep 5
done
