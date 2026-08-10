# поток: ret
"""Недельный отчёт приёмки: что мы получили за прошлую неделю.

    ./venv/bin/python -m returns_bot.weekly_received                 # штатно, понедельник утром
    ./venv/bin/python -m returns_bot.weekly_received --dry-run       # текст в консоль
    ./venv/bin/python -m returns_bot.weekly_received --to <chat_id>  # одному адресату
    ./venv/bin/python -m returns_bot.weekly_received --week 2026-08-03  # другая неделя

Ежедневная сводка (`daily_push`) отвечает на вопрос «что забрать»; этот отчёт — на вопрос
«что уже приехало и что с этим делать на приёмке». Главное здесь — был ли контакт
с покупателем: коробку, побывавшую у него, надо проверять и переупаковывать.
"""
import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone

from core import db
from returns_bot import tg

MSK = timezone(timedelta(hours=3))          # неделю считаем по Москве, сервер живёт в UTC

# Был ли контакт с покупателем — строго из типа возврата площадки, без догадок.
CONTACT = {
    ("ozon", "ClientReturn"): "yes",        # получил и вернул
    ("ozon", "PartialReturn"): "yes",
    ("ozon", "FullReturn"): "yes",
    ("yandex", "RETURN"): "yes",
    ("wb", "Возврат брака"): "yes",
    ("wb", "Возврат неверного вложения"): "yes",
    ("ozon", "Cancellation"): "no",         # до вручения не дошло
    ("yandex", "UNREDEEMED"): "no",         # не выкупил
}
# Real-FBS отдаёт причину человеческой строкой, единого справочника нет — ловим по словам.
RFBS_YES = ("не подошёл", "не работает", "сломал", "отказался при вручении", "брак")
RFBS_NO = ("отменил", "отмена")

TITLES = {
    "yes": ("🔴 Контакт с покупателем был — вскрыто", "Проверять и переупаковывать."),
    "unknown": ("❓ Площадка не говорит, был ли контакт", "Проверять как вскрытые."),
    "no": ("🟢 Контакта с покупателем не было", "До покупателя не доехало, упаковка целая."),
}
PLAT = {"ozon": "Ozon", "wb": "ВБ", "yandex": "Яндекс"}


def last_week(today=None):
    """Понедельник–воскресенье прошлой недели. Запуск в понедельник утром — окно закрыто."""
    today = today or datetime.now(MSK).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def _bounds(d1, d2):
    return (datetime.combine(d1, time.min, MSK), datetime.combine(d2, time.max, MSK))


def contact(platform, rtype):
    key = CONTACT.get((platform, rtype or ""))
    if key:
        return key
    low = (rtype or "").lower()
    if any(w in low for w in RFBS_YES):
        return "yes"
    if any(w in low for w in RFBS_NO):
        return "no"
    return "unknown"                        # у ВБ признака контакта нет вовсе


def fetch(since, until, removal=False):
    return db.query(f"""
        SELECT r.platform, r.order_number, r.return_id, r.return_type, r.received_at::date d,
               string_agg(COALESCE(i.name, i.offer_id, i.sku)
                          || CASE WHEN i.qty > 1 THEN ' ×' || i.qty ELSE '' END, '; ') tovar
        FROM mp_returns r
        LEFT JOIN mp_return_items i
          ON i.platform=r.platform AND i.account=r.account AND i.return_id=r.return_id
        WHERE r.received_at >= %s AND r.received_at <= %s
          AND r.source {'=' if removal else '<>'} 'ozon_removal'
        GROUP BY 1,2,3,4,5 ORDER BY 5, 1""", _bounds(since, until))


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(since, until):
    rows, removals = fetch(since, until), fetch(since, until, removal=True)
    buckets = {"yes": [], "unknown": [], "no": []}
    for x in rows:
        buckets[contact(x["platform"], x["return_type"])].append(x)

    out = [f"<b>Приёмка: получено за неделю {since:%d.%m}–{until:%d.%m}</b>",
           f"Возвратов от покупателей: {len(rows)}. Коробок вывоза со стока: {len(removals)}.", ""]
    if not rows and not removals:
        return "\n".join(out[:1] + ["За неделю не получено ничего."])

    for key in ("yes", "unknown", "no"):
        b = buckets[key]
        if not b:
            continue
        title, note = TITLES[key]
        out += [f"<b>{title} — {len(b)}</b>", note]
        for x in b:
            out.append(f"• {PLAT.get(x['platform'], x['platform'])} · заказ "
                       f"<code>{_esc(x['order_number']) or '—'}</code> · {x['d']:%d.%m}\n"
                       f"  {_esc(x['tovar'])[:150] or 'состав не пришёл'}\n"
                       f"  <i>{_esc(x['return_type']) or 'тип не указан'}</i>")
        out.append("")

    out.append(f"<b>📦 Вывоз со стока FBO Ozon — {len(removals)}</b>")
    out.append("Наш товар со склада Ozon, у покупателя не был — упаковка заводская, "
               "проверять только тип «Брак».")
    for x in removals:
        out.append(f"• заявка <code>{_esc(x['order_number']) or x['return_id']}</code> · "
                   f"{x['d']:%d.%m}\n  {_esc(x['tovar'])[:150] or 'состав не пришёл'}\n"
                   f"  <i>{_esc(x['return_type'])}</i>")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="недельный отчёт приёмки возвратов")
    ap.add_argument("--to", nargs="*", help="chat_id адресатов (по умолчанию из .env)")
    ap.add_argument("--week", help="любая дата внутри нужной недели, ГГГГ-ММ-ДД")
    ap.add_argument("--dry-run", action="store_true", help="напечатать, не отправлять")
    a = ap.parse_args(argv)

    if a.week:
        d = date.fromisoformat(a.week)
        since, until = d - timedelta(days=d.weekday()), d - timedelta(days=d.weekday()) + timedelta(days=6)
    else:
        since, until = last_week()
    text = build(since, until)

    if a.dry_run:
        print(text)
        return 0
    targets = a.to or tg.notify_ids()
    if not targets:
        print("некому слать: пуст TG_RETURNS_NOTIFY_ID / TG_RETURNS_ALLOWED_IDS")
        return 1
    ok, failed = 0, []
    for chat_id in targets:
        try:                       # один адресат не нажал Start (403) — остальные всё равно получат
            tg.send(chat_id, text)
            ok += 1
        except Exception as e:
            failed.append(f"{chat_id}: {type(e).__name__} {str(e)[:120]}")
    print(f"неделя {since}..{until}: отправлено {ok} из {len(targets)}")
    for f in failed:
        print("  не ушло —", f)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
