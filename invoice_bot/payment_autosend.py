# поток: inv
"""invoice_bot/payment_autosend.py — автоотправка ЗАПЛАНИРОВАННЫХ черновиков платёжек в банк.

Тонкая обвязка над `payment_send.send_draft`: своей банковской логики не содержит, банк по
каждому черновику выбирает диспетчер по `payment_draft_queue.org_inn` (Альфа — Цифровой Квадрат,
Сбер — Дисквэр). Отправляется НЕПОДПИСАННЫЙ документ: в банке появляется черновик, деньги без
подписи человека не двигаются. Автоподписание — запрет потока inv.

Расписание (крон, UTC; МСК = UTC+3):
  07:55 МСК  --kinds advance,deferred_batch   — после выписки и прогона `po_payment_watch`
  17:00 МСК  --kinds prepayment_order         — после дневного прогона поллера по предоплате

Рубильник: `PAYMENT_AUTOSEND=1` в `.env`. Без него скрипт только показывает, что ушло бы, и
ничего не отправляет — крон при этом трогать не надо. Поверх продолжают действовать гейты банков
(`ALFA_PAYMENT_APPLY` / `SBER_PAYMENT_APPLY`): при снятом гейте банка отправка всё равно сухая.

Повтор безопасен: берём только `status='planned'`, а внутри `send_draft` есть advisory-лок и
детерминированный `externalId` (uuid5 от id черновика) — повторный прогон отправит 0 строк.

Запуск:
    ./venv/bin/python invoice_bot/payment_autosend.py --org all --kinds advance,deferred_batch
    ./venv/bin/python invoice_bot/payment_autosend.py --org all --dry-run
"""
import os
import sys
import time
import argparse
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import payment_draft as pdr                      # noqa: E402
import payment_send as psend                     # noqa: E402
from core import db                              # noqa: E402

PAUSE_SEC = 1.0        # пауза между платёжками: банку хватает, а прогон всё равно секундный
KINDS_RU = {"prepayment_order": "предоплата по счёту",
            "deferred_batch": "отсрочка (пачка)",
            "advance": "аванс"}


def enabled():
    return os.getenv("PAYMENT_AUTOSEND", "").strip().lower() in ("1", "true", "yes", "on")


def _org_title(inn):
    entry = psend.BANKS.get(inn)
    return entry[1] if entry else inn


def _names(rows):
    """ИНН поставщика → имя из условий оплаты (в очереди имени нет). Одним запросом."""
    inns = sorted({r["inn"] for r in rows})
    if not inns:
        return {}
    got = db.query("SELECT DISTINCT inn, name FROM supplier_payment_terms WHERE inn = ANY(%s)",
                   (inns,))
    return {r["inn"]: r["name"] for r in got if r.get("name")}


def tg(msg):
    """Сводка прогона в ОТДЕЛЬНЫЙ платёжный бот (`TG_PAY_BOT_TOKEN` / `TG_PAY_NOTIFY_ID`).
    Общий бот invoice-bot сюда не подставляется намеренно: его канал читают и другие люди,
    а платёжная сводка — суммы и получатели — предназначена только Сергею. Бот не настроен →
    сводка не уходит никуда (отправку платежей это не роняет)."""
    token = os.getenv("TG_PAY_BOT_TOKEN", "").strip()
    ids = [x.strip() for x in os.getenv("TG_PAY_NOTIFY_ID", "").split(",") if x.strip()]
    if not (token and ids):
        print("TG: платёжный бот не настроен (TG_PAY_BOT_TOKEN/TG_PAY_NOTIFY_ID) — сводка "
              "не отправлена", flush=True)
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for uid in ids:
        try:
            data = urllib.parse.urlencode({"chat_id": uid, "text": msg}).encode()
            urllib.request.urlopen(api, data=data, timeout=20).read()
        except Exception as e:                                   # noqa: BLE001
            print(f"TG {uid}: не доставлено ({type(e).__name__})", flush=True)


def autosend(org_inns=None, kinds=None, dry_run=None, limit=None, actor="cron", notify=True):
    """Отправить все запланированные черновики выбранных юрлиц и оснований.

    → {"total", "sent", "errors", "skipped", "amount", "rows"}
      `amount` — сумма РЕАЛЬНО отправленного (не всей выборки).

    `dry_run=None` — решает гейт банка; при снятом `PAYMENT_AUTOSEND` принудительно True."""
    orgs = list(org_inns) if org_inns else list(psend.BANKS)
    rows = []
    for org in orgs:
        rows += pdr.load_drafts(org_inn=org, only_planned=True)
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    rows.sort(key=lambda r: (r["created_at"], r["id"]))    # старые долги уходят первыми
    if limit:
        rows = rows[:limit]

    dry = True if (dry_run is None and not enabled()) else dry_run
    if dry is True and not enabled():
        print("PAYMENT_AUTOSEND не выставлен — прогон СУХОЙ, в банк ничего не уйдёт", flush=True)

    names = _names(rows)
    out = {"total": len(rows), "sent": 0, "errors": 0, "skipped": 0, "amount": 0.0, "rows": []}
    for i, r in enumerate(rows):
        who = names.get(r["inn"], r["inn"])
        amount = float(r["amount"])
        try:
            res = psend.send_draft(r["id"], dry_run=dry, actor=actor)
        except Exception as e:                               # noqa: BLE001 — строка не роняет прогон
            res = {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}"}
        status = res.get("status") or "ошибка"
        if res.get("ok") and status in ("sent_prod", "sent_sandbox"):
            out["sent"] += 1
            out["amount"] += amount
        elif status == "dry_run":
            out["skipped"] += 1
        else:
            out["errors"] += 1
        out["rows"].append(f"#{r['id']} {who} · {amount:,.2f}₽ · "
                           f"{KINDS_RU.get(r['kind'], r['kind'])} → {status}"
                           f"{' — ' + str(res.get('error')) if res.get('error') else ''}")
        if i + 1 < len(rows) and dry is not True:      # dry=None → решает гейт банка, пауза нужна
            time.sleep(PAUSE_SEC)

    head = (f"платёжки: всего {out['total']}, отправлено {out['sent']} "
            f"на {out['amount']:,.2f}₽, сухих {out['skipped']}, ошибок {out['errors']}")
    print(head, flush=True)
    for line in out["rows"][:20]:
        print("  " + line, flush=True)
    if len(out["rows"]) > 20:
        print(f"  …ещё {len(out['rows']) - 20} строк", flush=True)

    # Молчим, когда сказать нечего: пустой прогон и сухая прогонка в TG не идут.
    if notify and (out["sent"] or out["errors"]):
        orgs_ru = ", ".join(_org_title(o) for o in orgs)
        tg(f"🏦 Черновики в банк ({orgs_ru})\n{head}\n\n" + "\n".join(out["rows"][:20])
           + "\n\nПодписать вручную в интернет-банке.")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="all", help="ИНН нашего юрлица или 'all' (по умолчанию)")
    ap.add_argument("--kinds", help="основания через запятую: prepayment_order,deferred_batch,advance")
    ap.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только показать")
    ap.add_argument("--limit", type=int, help="взять не больше N черновиков (тест)")
    ap.add_argument("--no-tg", action="store_true", help="без уведомления в Telegram")
    a = ap.parse_args(argv)

    orgs = None if a.org == "all" else [a.org]
    if orgs and orgs[0] not in psend.BANKS:
        sys.exit(f"неизвестное юрлицо {a.org}; знаем: {', '.join(psend.BANKS)}")
    kinds = [k.strip() for k in a.kinds.split(",")] if a.kinds else None
    res = autosend(org_inns=orgs, kinds=kinds, dry_run=True if a.dry_run else None,
                   limit=a.limit, notify=not a.no_tg)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
