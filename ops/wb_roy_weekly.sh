#!/bin/bash
# поток: mkt — недельный прогон «Роя» (понедельник).
# Порядок: выгрузка воронки за две полные недели (оба аккаунта) → раскладка по цветам
# → применение ТОЛЬКО понижающей половины (🔴 −10 %, 🟤 к полу 7.30) + вывод ⚫ в пол.
# ПОДЪЁМ ЗЕЛЁНЫХ НЕ АВТОМАТИЗИРОВАН: разгон ставок 12–15.08.2026 стоил денег, зелёные
# поднимаются только по слову Сергея, из CSV, который этот же прогон и кладёт.
set -u
cd /opt/mp-analytics || exit 1
PY=./venv/bin/python
END=$(date -d 'last sunday' +%F)
LOG=/opt/mp-analytics/wb_roy_weekly.log
{
  echo "=== $(date -Is) · неделя по $END ==="
  $PY -m ops.wb_roy_weeks --end "$END"                     || echo "!! воронка acc1 не собралась"
  $PY -m ops.wb_roy_weeks --account wb_acc2 --end "$END"   || echo "!! воронка acc2 не собралась"
  $PY -m ops.wb_week_compare --end "$END"                  || echo "!! сравнение недель не вышло"
  $PY -m ops.wb_breadth --end "$END"                       || echo "!! ширина acc1 не посчиталась"
  $PY -m ops.wb_breadth --account wb_acc2 --end "$END"     || echo "!! ширина acc2 не посчиталась"
  $PY -m ops.wb_roy_profile --end "$END"                   || echo "!! профиль не построился"
  CSV="docs/reports/mkt_roy_profile_${END}.csv"
  if [ -s "$CSV" ]; then
    $PY -m ops.wb_roy_apply "$CSV" --only floor,down --blackout --apply --notify
  else
    echo "!! нет $CSV — ставки не трогаем"
  fi
} >> "$LOG" 2>&1
