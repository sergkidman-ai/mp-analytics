# поток: inv
"""invoice_bot/rent_schedule.py — постоянная арендная плата в первый понедельник месяца.

Раз в сутки крон дёргает этот скрипт; в первый понедельник месяца он ставит в очередь
черновики по каждой активной строке `rent_plan` (миграция 215), в остальные дни молча выходит.
Дальше платёж живёт общей жизнью очереди: `payment_autosend` отправляет его в банк юрлица
(kind `rent`), документ уходит НЕПОДПИСАННЫМ — деньги двинутся, только когда человек подпишет
платёжку в вебе банка.

Почему не «1-е число»: решение Сергея 11.08.2026 — первый понедельник. Так платёж не попадает
на выходные и банк проводит его в тот же день (фактические платежи 06.07 и 03.08 — понедельники).

Суммы и текст назначения в КОДЕ НЕ ЖИВУТ: арендодатель меняет ставку письмом, это правка строки
`rent_plan`, а не деплой. Реквизиты получателя берём из МойСклада (у постоянной аренды счёта нет —
основание договор), поэтому карточка арендодателя с банковскими реквизитами обязательна.

Идемпотентность — ключ `rent:<org_inn>:<YYYY-MM>`: повторный запуск в тот же день (и ручной
прогон рядом с кроновым) второй платёжки на те же деньги не создаёт.

Запуск:
    ./venv/bin/python invoice_bot/rent_schedule.py --cron      # крон: молчит не в свой день
    ./venv/bin/python invoice_bot/rent_schedule.py --dry-run   # что ушло бы сегодня
    ./venv/bin/python invoice_bot/rent_schedule.py --force     # поставить сейчас (вне графика)
"""
import os
import sys
import argparse
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import rent_core as rc                           # noqa: E402
import payment_send as psend                     # noqa: E402
from core import db                              # noqa: E402


def is_first_monday(d):
    """Первый понедельник месяца: понедельник (weekday 0), выпавший на число ≤ 7."""
    return d.weekday() == 0 and d.day <= 7


def plans():
    return db.query("""SELECT org_inn, payee_inn, amount::float amount, purpose_tpl, note
                       FROM rent_plan WHERE active ORDER BY org_inn""")


def run(today=None, dry_run=False, force=False):
    """→ {'day': ..., 'rows': [...]}; `rows` — по строке на план: что сделали и почему."""
    today = today or date.today()
    out = {"day": today.isoformat(), "fired": force or is_first_monday(today), "rows": []}
    if not out["fired"]:
        return out

    month = rc.MONTHS_NOM[today.month - 1]
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
                    idem_key=f"rent:{p['org_inn']}:{today:%Y-%m}",
                    note=f"аренда за {month} {today.year}")
                row["status"] = "queued" if created else "already"
        except Exception as e:                                   # noqa: BLE001
            row["status"] = "error"
            row["error"] = f"{type(e).__name__}: {e}"
        out["rows"].append(row)
    return out


def report(out):
    L = [f"🏠 Аренда, первый понедельник ({out['day']})"] if out["fired"] else \
        [f"🏠 Аренда: {out['day']} — не первый понедельник, платежей нет"]
    mark = {"queued": "✅", "already": "↔️", "dry_run": "🧪", "error": "🛑"}
    for r in out["rows"]:
        who = rc.ORG_TITLE.get(r["org_inn"], r["org_inn"])
        L.append(f"{mark.get(r['status'], '•')} {who} → {rc.rub(r['amount'])}"
                 + (f" · черновик #{r['draft_id']}" if r.get("draft_id") else "")
                 + (f"\n   {r['error']}" if r.get("error") else ""))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Аренда: черновики в первый понедельник месяца")
    ap.add_argument("--cron", action="store_true", help="кроновый прогон: не свой день — тихий выход")
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не ставя в очередь")
    ap.add_argument("--force", action="store_true", help="поставить сейчас, вне графика")
    a = ap.parse_args(argv)

    out = run(dry_run=a.dry_run, force=a.force)

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
