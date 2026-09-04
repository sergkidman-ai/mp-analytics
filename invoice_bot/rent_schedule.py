# поток: inv
"""invoice_bot/rent_schedule.py — постоянная арендная плата в последний рабочий день месяца.

Раз в сутки крон дёргает этот скрипт; в последний рабочий день месяца он ставит в очередь
черновики по каждой активной строке `rent_plan` (миграция 215), в остальные дни молча выходит.
Дальше платёж живёт общей жизнью очереди: `payment_autosend` отправляет его в банк юрлица
(kind `rent`), документ уходит НЕПОДПИСАННЫМ — деньги двинутся, только когда человек подпишет
платёжку в вебе банка.

График: решение Сергея 04.09.2026 — платим ВПЕРЁД. В последний рабочий день месяца ставим
аренду за СЛЕДУЮЩИЙ месяц, чтобы деньги были у арендодателя до его начала. Прежнее правило
(первый понедельник, за текущий месяц) отменено: по нему платёж приходил уже внутри
оплачиваемого месяца, то есть с просрочкой.

Почему «последний рабочий», а не «последнее число»: 30–31-е попадает на выходные, и банк
проводит платёж уже в следующем месяце — ровно то, от чего уходим. Рабочий день считает
`workcal` (isdayoff.ru: праздники и переносы), 5-дневка.

Крон пропустил свой день (сервер лежал, сеть) — платёж не теряется: условие «сегодня НЕ РАНЬШЕ
последнего рабочего дня месяца», то есть оставшийся хвост выходных догоняет. Второй платёжки
это не рождает — держит `idem_key`.

Суммы и текст назначения в КОДЕ НЕ ЖИВУТ: арендодатель меняет ставку письмом, это правка строки
`rent_plan`, а не деплой. Реквизиты получателя берём из МойСклада (у постоянной аренды счёта нет —
основание договор), поэтому карточка арендодателя с банковскими реквизитами обязательна.

Идемпотентность — ключ `rent:<org_inn>:<YYYY-MM>`, где `YYYY-MM` — ОПЛАЧИВАЕМЫЙ месяц, а не день
постановки: повторный запуск (и ручной прогон рядом с кроновым, и догон через `--month`) второй
платёжки на те же деньги не создаёт.

Запуск:
    ./venv/bin/python invoice_bot/rent_schedule.py --cron      # крон: молчит не в свой день
    ./venv/bin/python invoice_bot/rent_schedule.py --dry-run   # что ушло бы сегодня
    ./venv/bin/python invoice_bot/rent_schedule.py --force     # поставить сейчас (вне графика)
    ./venv/bin/python invoice_bot/rent_schedule.py --force --month 2026-09   # догнать месяц
"""
import os
import sys
import argparse
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import rent_core as rc                           # noqa: E402
import payment_send as psend                     # noqa: E402
import workcal                                   # noqa: E402  рабочий календарь РФ (isdayoff.ru)
from core import db                              # noqa: E402


def month_start(d, shift=0):
    """Первое число месяца `d` со сдвигом на `shift` месяцев (нужен +1 — следующий месяц)."""
    n = d.year * 12 + (d.month - 1) + shift
    return date(n // 12, n % 12 + 1, 1)


def last_working_day(d):
    """Последний рабочий день месяца `d`: от последнего числа назад, пока не рабочий (5-дневка).

    Считает `workcal` — он знает праздники и переносы, а не только субботу с воскресеньем."""
    x = month_start(d, 1) - timedelta(days=1)
    while not workcal.is_working(x):
        x -= timedelta(days=1)
    return x


def is_pay_day(d):
    """День постановки. НЕ равенство, а «не раньше»: если крон пропустил последний рабочий день,
    хвост выходных до конца месяца ещё догонит платёж (дубль закрыт `idem_key`)."""
    return d >= last_working_day(d)


def plans():
    return db.query("""SELECT org_inn, payee_inn, amount::float amount, purpose_tpl, note
                       FROM rent_plan WHERE active ORDER BY org_inn""")


def run(today=None, dry_run=False, force=False, target=None):
    """→ {'day': ..., 'target': ..., 'rows': [...]}; `rows` — по строке на план: что и почему.

    `target` — первое число ОПЛАЧИВАЕМОГО месяца; по умолчанию следующий за сегодняшним."""
    today = today or date.today()
    target = target or month_start(today, 1)
    on_schedule = is_pay_day(today)
    out = {"day": today.isoformat(), "target": f"{target:%Y-%m}",
           "fired": force or on_schedule, "on_schedule": on_schedule, "rows": []}
    if not out["fired"]:
        return out

    month = rc.MONTHS_NOM[target.month - 1]
    for p in plans():
        row = {"org_inn": p["org_inn"], "payee_inn": p["payee_inn"], "amount": p["amount"],
               "purpose": p["purpose_tpl"].format(month=month), "status": None, "draft_id": None}
        try:
            if p["org_inn"] not in psend.BANKS:
                raise RuntimeError(f"нет банковского драйвера для юрлица {p['org_inn']}")
            payee = rc.ms_payee(p["payee_inn"])
            if not payee:
                raise RuntimeError(
                    f"у арендодателя ИНН {p['payee_inn']} нет однозначной карточки с банковскими "
                    f"реквизитами в МС — платить некуда")
            if dry_run:
                row["status"] = "dry_run"
            else:
                row["draft_id"], created = rc.queue_draft(
                    org_inn=p["org_inn"], payee_inn=p["payee_inn"], amount=p["amount"],
                    purpose_text=row["purpose"], payee=payee, kind="rent",
                    idem_key=f"rent:{p['org_inn']}:{target:%Y-%m}",
                    note=f"аренда за {month} {target.year}")
                row["status"] = "queued" if created else "already"
        except Exception as e:                                   # noqa: BLE001
            row["status"] = "error"
            row["error"] = f"{type(e).__name__}: {e}"
        out["rows"].append(row)
    return out


def report(out):
    # Шапка не врёт про график: догон просрочки и ручной прогон помечены отдельно — иначе
    # в TG «последний рабочий день» стояло бы над платежом, поставленным 4-го числа руками.
    when = "последний рабочий день месяца" if out.get("on_schedule") else "ВНЕ ГРАФИКА, вручную"
    L = [f"🏠 Аренда за {out['target']} — постановка {out['day']} ({when})"] if out["fired"] else \
        [f"🏠 Аренда: {out['day']} — не последний рабочий день месяца, платежей нет"]
    mark = {"queued": "✅", "already": "↔️", "dry_run": "🧪", "error": "🛑"}
    for r in out["rows"]:
        who = rc.ORG_TITLE.get(r["org_inn"], r["org_inn"])
        L.append(f"{mark.get(r['status'], '•')} {who} → {rc.rub(r['amount'])}"
                 + (f" · черновик #{r['draft_id']}" if r.get("draft_id") else "")
                 + (f"\n   {r['error']}" if r.get("error") else ""))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Аренда: черновики в последний рабочий день месяца, за следующий месяц")
    ap.add_argument("--cron", action="store_true", help="кроновый прогон: не свой день — тихий выход")
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не ставя в очередь")
    ap.add_argument("--force", action="store_true", help="поставить сейчас, вне графика")
    ap.add_argument("--month", metavar="YYYY-MM",
                    help="за какой месяц платим (по умолчанию следующий); догон просрочки")
    a = ap.parse_args(argv)

    target = None
    if a.month:
        y, m = a.month.split("-")
        target = date(int(y), int(m), 1)
    out = run(dry_run=a.dry_run, force=a.force, target=target)

    # Сверка с выпиской идёт КАЖДЫЙ день, а не только в день постановки: платёжку человек
    # подписывает когда угодно, и висящий 'sent_prod' — единственный признак неподписанной.
    closed = [] if a.dry_run else rc.reconcile()
    for line in closed:
        print(line, flush=True)

    if not out["fired"] and a.cron:
        if closed:
            rc.tg("🏠 Аренда проведена банком:\n" + "\n".join(closed))
        return 0                                   # не свой день — ни лога, ни сводки в TG
    text = report(out)
    if closed:
        text += "\n\nПроведено по выписке:\n" + "\n".join(closed)
    print(text, flush=True)
    # В TG идёт только то, что реально произошло: сухой прогон и «сегодня не тот день» —
    # шум, за который канал перестают читать.
    if out["fired"] and not a.dry_run and out["rows"]:
        rc.tg(text)
    return 1 if any(r["status"] == "error" for r in out["rows"]) else 0


if __name__ == "__main__":
    sys.exit(main())
