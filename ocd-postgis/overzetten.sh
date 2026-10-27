#!/usr/bin/env bash
# Zet de OCD-database over van Railway naar de PostGIS-container op apps-1.
#
# Gebruik (op apps-1, als root):
#   export SRC_DSN='postgresql://postgres:<wachtwoord>@<proxy-host>:<poort>/railway'
#   ./overzetten.sh <container> [schema ...]
#
#   <container>  de PostGIS-container van Coolify (docker ps), gebouwd uit
#                ocd-postgis/Dockerfile, met POSTGRES_DB=railway
#   [schema ...] alleen deze schema's (om te proeven); zonder: alles
#
# Werkwijze, zodat er nooit een tweede volledige kopie op de schijf staat
# (docs/railway-naar-hetzner-plan.md, B3):
#   1. extensies en database-instellingen (search_path) zoals op de bron
#   2. pre-data: schema's, tabellen, types, functies, views (zonder indexen)
#   3. per schema de data: dump in mapformaat, terugzetten, map weg
#   4. post-data: indexen, sleutels, triggers, materialized views, parallel
#   5. ANALYZE, en per tabel het aantal rijen vergelijken met de bron
#
# De pg_dump en pg_restore van de container zelf worden gebruikt (versie 17).
# Het wachtwoord van de bron gaat alleen via de omgeving mee, nooit als
# argument (dan stond het in de proceslijst).
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash op Windows: /tmp niet ombuigen

C="${1:?geef de naam van de PostGIS-container}"; shift
: "${SRC_DSN:?zet SRC_DSN: Railway, via de TCP-proxy}"
DB="${DST_DB:-railway}"
J_DUMP="${J_DUMP:-4}"
J_RESTORE="${J_RESTORE:-6}"
LOG="overzetten-$(date +%Y%m%d-%H%M%S).log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
in_c() { docker exec -i -e SRC_DSN -e PGUSER=postgres "$C" "$@"; }
bron() { in_c psql "$SRC_DSN" -v ON_ERROR_STOP=1 -At -c "$1"; }
doel() { in_c psql -d "$DB" -v ON_ERROR_STOP=1 -At -c "$1"; }

# pg_restore meldt ook fouten die geen fouten zijn: het image van postgis maakt
# de schema's tiger en topology zelf al aan, en stap 1 maakt de schema's van de
# extensies (VOORAF_SCHEMAS). Alle andere fouten stoppen alles.
VOORAF_SCHEMAS=""
herstel() {
  local wat="$1"; shift
  local uit
  uit=$(in_c pg_restore -d "$DB" "$@" 2>&1) || true
  echo "$uit" >> "$LOG"
  local echt
  echt=$(echo "$uit" | grep 'error:' \
    | grep -v -E "schema \"(tiger|topology${VOORAF_SCHEMAS:+|$VOORAF_SCHEMAS})\" already exists" \
    | grep -v -E 'errors ignored on restore' || true)
  if [ -n "$echt" ]; then
    log "FOUT bij $wat:"; echo "$echt" | head -20 | tee -a "$LOG"; exit 1
  fi
}

if [ $# -gt 0 ]; then
  SCHEMAS="$*"
else
  # Van klein naar groot: wat misgaat, gaat dan vroeg mis.
  SCHEMAS=$(bron "
    select n.nspname from pg_namespace n join pg_class c on c.relnamespace = n.oid
    where n.nspname !~ '^pg_' and n.nspname not in
          ('information_schema','tiger','tiger_data','topology')
      and c.relkind in ('r','m','p')
    group by n.nspname order by sum(pg_total_relation_size(c.oid))")
fi
SCHEMAS=$(echo $SCHEMAS)
N_ARGS=""
if [ $# -gt 0 ]; then for s in $SCHEMAS; do N_ARGS="$N_ARGS -n $s"; done; fi
log "Bron: $(bron "select pg_size_pretty(pg_database_size(current_database()))"), schema's: $SCHEMAS"

log "1. Extensies en instellingen"
# Elke extensie in hetzelfde schema als op de bron: op prod staan pg_trgm en
# vector in p2p, en indexen verwijzen naar p2p.gin_trgm_ops. In public
# aanmaken laat de post-data dan vallen.
for regel in $(bron "select e.extname || ':' || n.nspname from pg_extension e
                     join pg_namespace n on n.oid = e.extnamespace
                     where e.extname <> 'plpgsql' order by 1"); do
  e="${regel%%:*}"; ns="${regel#*:}"
  if [ -z "$(doel "select 1 from pg_namespace where nspname = '$ns'")" ]; then
    doel "create schema \"$ns\"" >/dev/null
    VOORAF_SCHEMAS="${VOORAF_SCHEMAS:+$VOORAF_SCHEMAS|}$ns"
  fi
  doel "create extension if not exists \"$e\" with schema \"$ns\"" >/dev/null
  op=$(doel "select n.nspname from pg_extension x join pg_namespace n on n.oid = x.extnamespace where x.extname = '$e'")
  if [ "$op" != "$ns" ]; then log "FOUT: extensie $e staat in $op, op de bron in $ns"; exit 1; fi
done
bron "select format('alter database %I set %s',
              '$DB',
              case when split_part(c, '=', 1) = 'search_path'
                   then 'search_path to ' || substr(c, length('search_path=') + 1)
                   else format('%s = %L', split_part(c, '=', 1), substr(c, strpos(c, '=') + 1)) end)
      from pg_db_role_setting s join pg_database d on d.oid = s.setdatabase,
           unnest(s.setconfig) c
      where d.datname = current_database() and s.setrole = 0" \
| while read -r sql; do [ -n "$sql" ] && log "   $sql" && doel "$sql" >/dev/null; done

log "2. Pre-data"
in_c pg_dump "$SRC_DSN" --section=pre-data $N_ARGS -Fc -f /tmp/pre.dump
herstel pre-data /tmp/pre.dump
in_c rm -f /tmp/pre.dump

log "3. Data, per schema"
for s in $SCHEMAS; do
  t0=$(date +%s)
  in_c pg_dump "$SRC_DSN" --section=data -n "$s" -Fd -j "$J_DUMP" -f "/tmp/data-$s"
  grootte=$(in_c du -sh "/tmp/data-$s" | cut -f1)
  herstel "data $s" -j "$J_RESTORE" --disable-triggers "/tmp/data-$s"
  in_c rm -rf "/tmp/data-$s"
  log "   $s: dump $grootte, $(( $(date +%s) - t0 )) s"
done

log "4. Post-data (indexen, sleutels, materialized views)"
in_c pg_dump "$SRC_DSN" --section=post-data $N_ARGS -Fc -f /tmp/post.dump
herstel post-data -j "$J_RESTORE" /tmp/post.dump
in_c rm -f /tmp/post.dump

log "5. ANALYZE en rijen tellen"
doel "analyze" >/dev/null
verschil=0
for s in $SCHEMAS; do
  for t in $(bron "select quote_ident(schemaname)||'.'||quote_ident(tablename) from pg_tables where schemaname = '$s' order by 1"); do
    a=$(bron "select count(*) from $t"); b=$(doel "select count(*) from $t")
    if [ "$a" != "$b" ]; then log "   VERSCHIL $t: bron $a, doel $b"; verschil=1; fi
  done
done
[ "$verschil" = 0 ] && log "   alle tabellen gelijk"
log "Klaar. Doel: $(doel "select pg_size_pretty(pg_database_size(current_database()))"). Log: $LOG"
exit "$verschil"
