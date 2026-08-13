#!/bin/bash
# поток: mkt
# Ежедневный цикл разгона ставок Ozon: снять вчерашний день по ОБОИМ аккаунтам ->
# сделать шаг +10 % по acc1.
# Шаг сам не выполнится, если вчерашний расход превысил потолок (--max-spend).
#
# ЗАМОРОЗКА (13.08.2026, по команде Сергея): если существует файл /etc/mp-ozon-ramp-frozen,
# снимки собираются как обычно, а ШАГ НЕ ДЕЛАЕТСЯ. Причина заморозки — цели ставок посчитаны
# от маржи `margin_own_live`, которая завышает её на 10,6 п.п.; пока это не пересчитано,
# дальше поднимать ставки нельзя. Снять заморозку = удалить файл (только по команде Сергея).
# Тот же файл проверяет и /usr/local/sbin/mp-ozon-bid-ramp — на случай, если запустится
# другая копия этого скрипта (worktree/main).
#
# acc2 разгона НЕ имеет и остаётся контрольной группой: снимки по нему нужны именно для
# сравнения «разгоняем / не разгоняем».
set -u
# Корень берём от расположения самого скрипта: файл живёт и в общем чекауте,
# и в worktree, и запускаться должен из своего дерева, а не из чужого.
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
LOG=/opt/mp-analytics/logs/ozon_bid_ramp.log
FREEZE=/etc/mp-ozon-ramp-frozen
mkdir -p /opt/mp-analytics/logs
YDAY="$(date -d yesterday +%F)"
{
  echo "=== $(date -Is) ==="
  for ACC in oz_acc1 oz_acc2; do
    echo "[$ACC]"
    ./venv/bin/python tools/ozon_bid_ramp.py snapshot --account "$ACC" --date "$YDAY"
  done
  if [ -f "$FREEZE" ]; then
    echo "разгон ЗАМОРОЖЕН ($FREEZE) — шаг не делается; снимки собраны"
  else
    ./venv/bin/python tools/ozon_bid_ramp.py step --apply --max-spend 8000
  fi
} >> "$LOG" 2>&1
