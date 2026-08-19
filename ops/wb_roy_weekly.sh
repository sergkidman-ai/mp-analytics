#!/bin/bash
# поток: mkt — недельный прогон «Роя» (понедельник).
# Порядок: выгрузка воронки за две полные недели (оба аккаунта) → раскладка по цветам
# → применение всех четырёх правил: 🟢 +10 %, 🔴 −10 %, 🟤 к полу 7.30, ⚫ в пол.
# С 19.08.2026 прогон работает на ОБОИХ аккаунтах (решение Сергея): на acc2 после посадки
# пула ширины (205 ставок 7.30→10.90) появилось что понижать, стратегии держим одинаковыми.
# ПОДЪЁМ ЗЕЛЁНЫХ АВТОМАТИЗИРОВАН 19.08.2026 по команде Сергея (до этого шёл руками — разгон
# 12–15.08 стоил денег). Ограничители: зелёным считается только позиция, у которой ДРР профиля
# ниже СВОЕГО потолка (маржа − 25 %), шаг +10 %, потолок ставки 25 ₽, и не более --max-up 150
# позиций за прогон (отбор по запасу ₽/нед до потолка ДРР) — чтобы сбой снимка не превратился
# в разгон всего каталога. Раз в неделю, не чаще.
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
  $PY -m ops.wb_roy_profile --account wb_acc2 --end "$END" || echo "!! профиль acc2 не построился"
  CSV="docs/reports/mkt_roy_profile_${END}.csv"
  if [ -s "$CSV" ]; then
    $PY -m ops.wb_roy_apply "$CSV" --blackout --max-up 150 --apply --notify
  else
    echo "!! нет $CSV — ставки acc1 не трогаем"
  fi
  # acc2 «ДисКвэр»: те же четыре правила и тот же ограничитель подъёма.
  # Удаление ⚫ из кампаний остаётся ручным — в API ВБ нет метода снять номенклатуру.
  CSV2="docs/reports/mkt_roy_profile_${END}_wb_acc2.csv"
  if [ -s "$CSV2" ]; then
    $PY -m ops.wb_roy_apply "$CSV2" --account wb_acc2 --blackout --max-up 150 --apply --notify
  else
    echo "!! нет $CSV2 — ставки acc2 не трогаем"
  fi
} >> "$LOG" 2>&1
