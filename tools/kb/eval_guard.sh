#!/usr/bin/env bash
# поток: infra
# Fail-closed преамбула для ЛЮБОГО eval/probe-прогона (J0/J1, gold-set, замеры пола харнесса).
#
# Зачем. 19.08 посреди K0 бинарь claude оказался 500-байтным огрызком: шла установка
# версии 2.1.235, PATH указывал на недописанный файл. Прогон в таком состоянии не падает
# громко — он тихо даёт мусорные цифры. Ни один замер не должен стартовать без проверки.
#
# Использование:
#   bash tools/kb/eval_guard.sh 2.1.235   || exit 1
#   EXPECT=2.1.235 bash tools/kb/eval_guard.sh || exit 1
#
# Возврат: 0 — мерить можно; !=0 — прогон запрещён (CLI не тот, версия не та, бинарь битый).
# Скрипт ничего не меняет: только читает PATH, размер файла и вывод --version.

set -u
EXPECT="${1:-${EXPECT:-}}"
BIN="$(command -v claude || true)"

fail() { echo "EVAL-GUARD STOP: $*" >&2; exit 1; }

[ -n "$BIN" ] || fail "claude не найден в PATH"

REAL="$(readlink -f "$BIN")"
SZ="$(stat -c %s "$REAL" 2>/dev/null || echo 0)"
[ "$SZ" -gt 10000 ] || fail "бинарь подозрительно мал (${SZ} Б, $REAL) — вероятно идёт установка/обновление"

VER="$(timeout 60 claude --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ -n "$VER" ] || fail "claude --version не отвечает или вывод не разбирается"

if [ -n "$EXPECT" ] && [ "$VER" != "$EXPECT" ]; then
  fail "версия $VER != ожидаемой $EXPECT — пол харнесса недействителен, пересними пол ДО сравнения"
fi

echo "EVAL-GUARD OK: claude $VER ($REAL, ${SZ} Б)"
