#!/bin/bash
# поток: mkt
# Ежедневный цикл разгона ставок Ozon acc1: снять вчерашний день -> сделать шаг +10 %.
# Шаг сам не выполнится, если вчерашний расход превысил потолок (--max-spend).
set -u
cd /opt/mp-analytics/.claude/worktrees/mkt-ozon || exit 1
LOG=/opt/mp-analytics/logs/ozon_bid_ramp.log
mkdir -p /opt/mp-analytics/logs
{
  echo "=== $(date -Is) ==="
  ./venv/bin/python tools/ozon_bid_ramp.py snapshot --date "$(date -d yesterday +%F)"
  ./venv/bin/python tools/ozon_bid_ramp.py step --apply --max-spend 8000
} >> "$LOG" 2>&1
