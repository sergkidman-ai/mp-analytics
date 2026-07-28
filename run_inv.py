# поток: inv
"""run_inv.py — ежедневный прогон потока inv (банк → учёт).

Сейчас один шаг: **выписка Альфа-Банка за ВЧЕРА → МойСклад** (paymentin/paymentout).
Раз в сутки, утром: к 07:00 МСК банковский день закрыт, все операции проставлены.

Расписание (crontab). ВНИМАНИЕ: **`CRON_TZ` этим cron НЕ поддерживается** (`3.0pl1-137ubuntu3`,
система `Etc/UTC`; проверено 2026-07-28 по syslog — строка `CRON_TZ=Europe/Moscow` стояла и была
инертной, снята). **Время в crontab = UTC**, МСК = UTC+3, т.е. 07:00 МСК = `0 4`.
Запуск — из deploy-worktree на main, а не из основного чекаута (там часто ветка чужого потока):
    0 4 * * * cd /opt/mp-analytics/.deploy/inv && { git checkout --detach -q main 2>/dev/null; \
        /opt/mp-analytics/venv/bin/python run_inv.py; } >> /opt/mp-analytics/alfa_statement.log 2>&1

Счета — в .env: `ALFA_ACCOUNT` (песочница) / `ALFA_ACCOUNT_PROD` (бой), выбор по `ALFA_ENV`;
в каждой можно несколько через запятую.

ЗАПИСЬ В МС — двойной предохранитель:
  1) пишем только при `ALFA_MS_APPLY=1` в .env (по умолчанию dry-run: читаем банк,
     считаем план, ничего не создаём);
  2) при `ALFA_ENV=sandbox` запись ЗАПРЕЩЕНА жёстко, даже с APPLY=1 — контрагенты
     песочницы фейковые, в боевой МС их лить нельзя.
Переключение на бой = `ALFA_ENV=prod` (+ заполненные `ALFA_*_PROD`) и `ALFA_MS_APPLY=1`;
crontab при этом трогать не нужно. Бой включён 2026-07-28 (ОК владельца), отсечка `ALFA_MS_SINCE`.

Повторный прогон за ту же дату безопасен: идемпотентность по банковскому `uuid`
операции → МС `syncId` (см. collectors/alfa_ms.py), дублей не будет.

Ручной запуск:
    ./venv/bin/python run_inv.py                 # за вчера
    ./venv/bin/python run_inv.py 2026-07-21      # за конкретную дату
    ./venv/bin/python run_inv.py --days 7        # добор за последние 7 дней (по вчера)
"""
import os
import sys
import pathlib
import datetime as dt
import traceback

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv                       # noqa: E402

# .env gitignored → в git-worktree его нет; фолбэк на канонический чекаут проекта.
_ENV = BASE_DIR / ".env"
load_dotenv(_ENV if _ENV.exists() else pathlib.Path("/opt/mp-analytics/.env"))

import collectors.alfa_statement as alfa             # noqa: E402
import collectors.alfa_ms as alfa_ms                 # noqa: E402

MSK = dt.timezone(dt.timedelta(hours=3))
# счёт песочницы из доки «Тестирование выписок ЮЛ» — чтобы прогон работал до боя
SANDBOX_ACCOUNT = "40702810102300000001"


def _now():
    return dt.datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S МСК")


def log(msg):
    print(f"[{_now()}] {msg}", flush=True)


def accounts():
    """Счета для выписки. В проме берём ALFA_ACCOUNT_PROD (боевой р/с), в песочнице —
    ALFA_ACCOUNT (тестовые счета банка). Разведены нарочно: так песочный ключ не уйдёт
    по боевому счёту, а боевой — по тестовому, даже если забыть поправить вторую строку."""
    prod = (os.getenv("ALFA_ENV") or "sandbox").lower() != "sandbox"
    raw = (os.getenv("ALFA_ACCOUNT_PROD") if prod else os.getenv("ALFA_ACCOUNT")) or ""
    accs = [a.strip() for a in raw.split(",") if a.strip()]
    if accs:
        return accs
    if not prod:
        return [SANDBOX_ACCOUNT]
    raise SystemExit("нет ALFA_ACCOUNT_PROD в .env — не знаю, по какому боевому счёту брать выписку")


def apply_mode():
    """→ (писать_ли_в_МС, причина). Два предохранителя, см. докстринг модуля."""
    env = (os.getenv("ALFA_ENV") or "sandbox").lower()
    want = os.getenv("ALFA_MS_APPLY") == "1"
    if not want:
        return False, "DRY-RUN (ALFA_MS_APPLY≠1)"
    if env == "sandbox":
        return False, "DRY-RUN ПРИНУДИТЕЛЬНО (ALFA_ENV=sandbox: данные песочницы фейковые)"
    return True, "APPLY (запись в МС)"


def dates(argv):
    """Даты прогона: по умолчанию — вчера по МСК.
    Явная дата позиционным аргументом; `--days N` / `--days=N` — последние N дней по вчера
    (значение флага не должно попасть в позиционные — иначе N примут за дату)."""
    days, pos, skip = 1, [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
        elif a == "--days":
            days = max(1, int(argv[i + 1])) if i + 1 < len(argv) else 1
            skip = True
        elif a.startswith("--days="):
            days = max(1, int(a.split("=", 1)[1]))
        elif not a.startswith("--"):
            pos.append(a)
    if pos:
        return [pos[0]]
    yday = dt.datetime.now(MSK).date() - dt.timedelta(days=1)
    return [(yday - dt.timedelta(days=k)).isoformat() for k in range(days - 1, -1, -1)]


def run_day(account, date, apply):
    """Один счёт × одна дата. → (кол-во операций, stats) либо исключение наружу."""
    res = alfa.fetch_statement(account, date)
    ops = res["normalized"]
    if not ops:
        log(f"счёт {account} {date}: операций нет")
        return 0, None
    stats, _plan = alfa_ms.sync(ops, apply=apply)
    for msg in (stats.get("error_msgs") or [])[:10]:        # почему именно упало — в крон-лог
        log(f"  ✗ {msg}")
    log(f"счёт {account} {date}: операций {len(ops)} | "
        f"приход {stats['paymentin']} / расход {stats['paymentout']} | "
        f"уже в МС {stats['existing']}, до отсечки {stats['before_cutoff']} | "
        f"контрагент: найден {stats['matched']}, создан {stats['created']}, "
        f"будет создан {stats['would_create']} | ошибок {stats['errors']}")
    return len(ops), stats


def main(argv):
    apply, why = apply_mode()
    accs, days = accounts(), dates(argv)
    log(f"старт inv: счетов {len(accs)}, дат {len(days)} ({days[0]}…{days[-1]}) — {why}")

    total_ops = total_err = failed = 0
    for account in accs:
        for date in days:
            try:
                n, stats = run_day(account, date, apply)
                total_ops += n
                total_err += (stats or {}).get("errors", 0)
            except Exception as e:
                failed += 1
                log(f"ОШИБКА счёт {account} {date}: {type(e).__name__}: {e}")
                traceback.print_exc()

    log(f"финиш inv: операций {total_ops}, ошибок записи {total_err}, "
        f"упавших прогонов {failed}")
    # ненулевой код → крон-лог явно покажет проблему
    return 1 if (failed or total_err) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
