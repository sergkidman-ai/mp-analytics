# поток: fin
"""ops/upd_weekly.py — новые уведомления о выкупе ВБ → УПД → Наталии в телеграм.

ВБ выкладывает уведомления о выкупе раз в неделю, по понедельникам-вторникам. Поэтому
задание не привязано к дню недели, а запускается ежедневно и отправляет только то, чего
ещё не отправляло: появилось новое уведомление — в тот же день ушло, не появилось —
ни строчки шума. Это надёжнее расписания «по вторникам»: если ВБ задержит выкладку
на день, письмо всё равно уйдёт вовремя.

Что уходит: только короткая сводка — какие уведомления появились и на какую сумму.
Сами XML-файлы в бот не шлём, их забирают кнопкой на странице /reports/upd. Но УПД по
каждому уведомлению всё равно собирается перед отправкой: если документ не собрался
(нет реквизитов, кривые данные), сводка об этом уведомлении не уйдёт и в лог упадёт
ошибка — сообщение «появилось новое» без выгружаемого документа бесполезно.

Старый хвост (уведомления давнее WINDOW_DAYS) намеренно НЕ рассылается: он разбирается
кнопкой на странице /reports/upd. Иначе первый же запуск вывалил бы адресату несколько
десятков файлов за полгода.

Запуск:  venv/bin/python -m ops.upd_weekly --send
         venv/bin/python -m ops.upd_weekly            (сухой прогон, печатает текст сводки)
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")

from core import db  # noqa: E402
from reports.upd_form import make_upd  # noqa: E402
from reports.upd_orgs import SELLERS  # noqa: E402
from reports.upd_page import ACC_LABEL  # noqa: E402

TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
# Адресат — Наталия (@Pro_natalya). В TG_PRC_NOTIFY_ID лежит Сергей, туда эти файлы
# не нужны, поэтому отдельная переменная, а не переиспользование общего списка.
NATALIA_ID = os.getenv("TG_PRC_NATALIA_ID", "1231747786").strip()

# Сколько дней назад считать уведомление «свежим». Неделя плюс запас на праздники
# и на день-два задержки выкладки у ВБ.
WINDOW_DAYS = 14


def _pending(window_days=WINDOW_DAYS):
    """Свежие уведомления, по которым УПД ещё не уходил."""
    return db.query("""SELECT account, doc_number, doc_date, total_with_vat,
                              jsonb_array_length(positions) AS pos
                         FROM wb_redeem_notice
                        WHERE sent_at IS NULL AND upd_status <> 'made' AND doc_date >= %s
                     ORDER BY doc_date, account""", (date.today() - timedelta(days=window_days),))


def _rub(x):
    return f"{float(x):,.2f}".replace(",", " ").replace(".", ",")


def _tg(method, data, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                      data=data, files=files, timeout=120)
    if not r.ok:
        raise RuntimeError(f"telegram {method}: {r.status_code} {r.text[:200]}")
    return r.json()


def run(send=False, window_days=WINDOW_DAYS):
    rows = _pending(window_days)
    ready = [r for r in rows if (SELLERS.get(r["account"]) or {}).get("edi_id")]
    blocked = [r for r in rows if r not in ready]
    for r in blocked:
        print(f"  ⚠ {r['doc_number']} ({r['account']}): нет реквизитов продавца, пропущено",
              flush=True)
    if not ready:
        print("новых уведомлений нет", flush=True)
        return 0

    docs = []
    for r in ready:
        make_upd(r["account"], r["doc_number"])   # проверка собираемости, файл никуда не идёт
        docs.append(r)
        print(f"  {r['doc_number']} от {r['doc_date']:%d.%m.%Y} "
              f"({ACC_LABEL.get(r['account'], r['account'])}): {r['pos']} поз., "
              f"{_rub(r['total_with_vat'])} ₽", flush=True)

    total = sum(float(r["total_with_vat"]) for r in docs)
    head = (f"Уведомления о выкупе ВБ: новых {len(docs)} на {_rub(total)} ₽.\n"
            + "\n".join(f"• {r['doc_number']} от {r['doc_date']:%d.%m.%Y} — "
                        f"{ACC_LABEL.get(r['account'], r['account'])}, {_rub(r['total_with_vat'])} ₽"
                        for r in docs))
    if not send:
        print(f"[сухой прогон] адресат {NATALIA_ID}, уведомлений {len(docs)}", flush=True)
        print("   текст:", head.replace("\n", " / ")[:300], flush=True)
        return len(docs)
    if not TG_TOKEN:
        raise RuntimeError("нет TG_PRC_BOT_TOKEN — отправлять нечем")

    _tg("sendMessage", {"chat_id": NATALIA_ID, "text": head[:3900],
                        "disable_web_page_preview": "true"})
    # Отметку ставим только после успешной отправки сводки: иначе уведомление считалось бы
    # разосланным, а адресат о нём не узнал бы.
    for r in docs:
        db.execute("UPDATE wb_redeem_notice SET sent_at=now() WHERE account=%s AND doc_number=%s",
                   (r["account"], r["doc_number"]))
    print(f"отправлено Наталии: сводка по {len(docs)} уведомлениям", flush=True)
    return len(docs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="реально отправить (иначе сухой прогон)")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--resend", metavar="НОМЕР", help="сбросить отметку отправки у уведомления")
    args = ap.parse_args()
    if args.resend:
        print("сброшено:", db.execute(
            "UPDATE wb_redeem_notice SET sent_at=NULL WHERE doc_number=%s", (args.resend,)))
        return
    run(send=args.send, window_days=args.days)


if __name__ == "__main__":
    main()
