#!/bin/bash
# поток: mpstats-probe — разведочная обёртка, не production
# mp2.sh <label> <METHOD> <path_from_api_root> [query] [body]
# ответ -> raw/<label>.json; журнал (без токена) -> derived/requests_log.tsv; каждые 10 запросов -> report_api_limit
P=/opt/mp-analytics/data/mpstats_probe/2026-09-14
source /root/.claude/skills/mpstats/scripts/common.sh
load_config
LABEL=$1; M=$2; PATH_=$3; Q=$4; B=$5
URL="https://mpstats.io/api/$PATH_"; [ -n "$Q" ] && URL="$URL?$Q"
CNT_FILE=$P/tmp/count; n=$(( $(cat $CNT_FILE 2>/dev/null || echo 0) + 1 )); echo $n > $CNT_FILE
if [ -f "$P/tmp/STOP" ]; then echo "STOP flag present"; exit 2; fi
if [ "$M" = GET ]; then
  code=$(curl -s -D $P/tmp/h.txt -o $P/raw/$LABEL.json -w '%{http_code}' "$URL" -H "X-Mpstats-TOKEN: $MPSTATS_TOKEN" -H 'Content-Type: application/json')
else
  code=$(curl -s -D $P/tmp/h.txt -o $P/raw/$LABEL.json -w '%{http_code}' -X POST "$URL" -H "X-Mpstats-TOKEN: $MPSTATS_TOKEN" -H 'Content-Type: application/json' --data "$B")
fi
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$n" "$(date +%H:%M:%S)" "$LABEL" "$M" "${URL#https://mpstats.io/api/}" "$B" "$code" >> $P/derived/requests_log.tsv
echo "#$n $LABEL | $M ${URL#https://mpstats.io/api/} | body=${B:0:160} | HTTP $code | $(wc -c <$P/raw/$LABEL.json) B"
[ "$code" = 429 ] && { touch $P/tmp/STOP; echo "429 -> STOP"; }
if (( n % 10 == 0 )); then
  lim=$(curl -s "https://mpstats.io/api/user/report_api_limit" -H "X-Mpstats-TOKEN: $MPSTATS_TOKEN")
  echo "$n $lim" >> $P/derived/limits_log.txt; echo "LIMIT after $n: $lim"
  # стоп: общий use больше числа наших запросов (11 этапа 1 + n) или по площадке осталось <15
  echo "$lim" | python3 -c "
import sys,json;d=json.load(sys.stdin);n=$n
bad=d['use']>n+11 or any(d['available_'+m+'_external']-d['use_'+m+'_external']<15 for m in ('wb','ozon','ym'))
sys.exit(1 if bad else 0)" || { touch $P/tmp/STOP; echo "counter unexpected -> STOP"; }
fi
