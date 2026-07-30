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
import collectors.alfa_link as alfa_link             # noqa: E402

MSK = dt.timezone(dt.timedelta(hours=3))
# Окно добора привязок. Предоплата уходит РАНЬШЕ поставки (Феррет: платёж 28.07, приёмка 29.07),
# поэтому попытки в момент записи платежа мало — возвращаемся к непривязанным ещё N дней.
# 14 дней с запасом кроют лаг «оплатили счёт → привезли товар → провели приёмку».
LINK_LOOKBACK_DAYS = 14
# Авансы с неизрасходованным остатком добираем на широком окне: поставка приходит намного позже
# денег. Стоит МС-запрос только по авансам с реальным остатком, остальное отсеивается локально.
# 90 дней — решение Сергея 2026-07-30 (180 признано избыточным). Цена решения: аванс старше 90
# дней перестаёт добираться автоматически — на 30.07 это №59530 КОМПАНИЯ РМ от 14.04 (остаток
# 15 000 ₽); такие разбираются руками.
ADVANCE_SWEEP_DAYS = 90
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
    for msg in (stats.get("link_msgs") or [])[:10]:         # платежи, требующие ручной привязки
        log(f"  ⚠ привязка вручную: {msg}")
    link_part = f"привязано к приёмкам {stats.get('linked', 0)}"
    if stats.get("link_errors"):
        link_part += f", на ручную {stats['link_errors']}"
    log(f"счёт {account} {date}: операций {len(ops)} | "
        f"приход {stats['paymentin']} / расход {stats['paymentout']} | "
        f"уже в МС {stats['existing']}, до отсечки {stats['before_cutoff']}, "
        f"игнор {stats.get('ignored', 0)} | "
        f"контрагент: найден {stats['matched']}, создан {stats['created']}, "
        f"будет создан {stats['would_create']} | {link_part} | ошибок {stats['errors']}")
    return len(ops), stats


def relink(apply):
    """Добор привязок в ДВА захода — оба на каждом прогоне, идемпотентно:

      1. **свежее окно** `LINK_LOOKBACK_DAYS` — полный разбор всех платежей: в момент записи
         привязывать бывает не к чему (предоплата уходит раньше поставки);
      2. **авансы с неизрасходованным остатком** за `ADVANCE_SWEEP_DAYS` — правило Сергея
         2026-07-30. Аванс не «разбирается» один раз: пока остаток не исчерпан, платёж на каждом
         прогоне проверяется заново и добирает новые неоплаченные приёмки. Окно шире свежего,
         потому что поставка может прийти месяцами позже денег (у аванса №498 остаток висел с 22.07).

    Почему второй заход только про авансы: остаток у обычного платежа = частичная привязка руками
    владельца, наша логика частичную не пишет вообще — туда лезть нельзя.

    Стоимость шире окна почти нулевая: гейт по таблице поставщиков и исчерпанные авансы считаются
    из уже полученных `operations`, в МС идут только авансы с реальным остатком.

    Сбой добора не должен ронять прогон — деньги в МС уже записаны, привязка вторична."""
    today = dt.datetime.now(MSK).date()
    fresh_since = (today - dt.timedelta(days=LINK_LOOKBACK_DAYS)).isoformat()
    sweep_since = (today - dt.timedelta(days=ADVANCE_SWEEP_DAYS)).isoformat()
    rows = alfa_link.payments_since(sweep_since)          # одна выборка на оба захода
    fresh = [p for p in rows if (p.get("moment") or "")[:10] >= fresh_since]
    older_open = alfa_link.open_advances(
        [p for p in rows if (p.get("moment") or "")[:10] < fresh_since])
    stats, lines = alfa_link.link_new(fresh + older_open, apply=apply)
    for msg in lines[:10]:
        log(f"  ⚠ привязка вручную: {msg}")
    done = stats["linked"] if apply else stats["would_link"]
    log(f"добор привязок с {fresh_since}: платежей {len(fresh)} "
        f"(не поставщики {stats.get('not_supplier', 0)}) + авансов с остатком с {sweep_since}: "
        f"{len(older_open)} | {'привязано' if apply else 'привязалось бы'} {done}, "
        f"уже закрыто {stats['already']}, ждут поставки {stats['no_match']}, "
        f"на ручную {stats['partial'] + stats['errors']}"
        + (f", авансы отложены {stats['advance_off']}" if stats.get("advance_off") else ""))


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

    try:
        relink(apply)
    except Exception as e:
        log(f"ОШИБКА добора привязок: {type(e).__name__}: {e}")   # не роняем прогон: деньги записаны

    log(f"финиш inv: операций {total_ops}, ошибок записи {total_err}, "
        f"упавших прогонов {failed}")
    # ненулевой код → крон-лог явно покажет проблему
    return 1 if (failed or total_err) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
