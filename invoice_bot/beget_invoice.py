# поток: inv
"""invoice_bot/beget_invoice.py — счёт хостинга «Бегет» → черновик платёжки СРАЗУ.

Движок почтовой папки «Бегет» (`MAIL_FOLDER_BEGET`), тот же контракт, что у счетов
поставщиков, УПД и аренды: `process(path, create=True)` + `format_report(res)`.

Зачем отдельный движок, а не `rent_invoice`: Бегет печатает собственную форму, а не
печатную форму 1С. В ней нет ни строки «Счет на оплату», ни «Поставщик»/«Покупатель»,
реквизиты получателя стоят слитно (`ИНН/КПП: 7801451618/780601001`), а расчётный счёт
подписан латинской «c» (`р/c:`). Общий разбор на этом спотыкается, а натягивать на него
чужую форму — значит сломать разбор аренды.

Чем Бегет отличается от аренды по сути:
  * счёт приходит нерегулярно — как только кончается баланс хостинга, и оплатить его надо
    в три банковских дня, поэтому черновик уезжает в банк СРАЗУ (решение Сергея 25.09.2026),
    не дожидаясь прогона `payment_autosend` в 07:55/17:00 МСК;
  * в назначении обязан стоять номер аккаунта у Бегета — без него платёж не зачислится
    на баланс (предупреждение в самом счёте). Нет аккаунта в назначении — стоп.

Предохранители (без них скрипт не платит):
  * юрлицо-плательщик берётся из счёта и обязано быть в `payment_send.BANKS`;
  * реквизиты получателя — из счёта, карточка МС остаётся контролем (`payee_mismatch`);
  * сумма не выше порога из `rent_utility_guard` (у Бегета 20 000 ₽: счета были 5 880 и
    7 780 ₽, порог ловит ошибку на порядок, а не отменяет платёж);
  * повторно прочитанное письмо не плодит вторую платёжку — ключ `beget:<org_inn>:<номер>`;
  * общий рубильник `PAYMENT_AUTOSEND`: снят — черновик ждёт в очереди, в банк не уходит.

Документ уходит НЕПОДПИСАННЫМ: деньги двинутся, только когда человек подпишет платёжку
в вебе банка.

Запуск вручную (разбор без записи — по умолчанию):
    ./venv/bin/python invoice_bot/beget_invoice.py счёт.pdf
    ./venv/bin/python invoice_bot/beget_invoice.py счёт.pdf --create
"""
import os
import re
import sys
import argparse
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import rent_core as rc                           # noqa: E402
import payment_send as psend                     # noqa: E402
import payment_autosend as pauto                 # noqa: E402
from rent_invoice import pdf_text                # noqa: E402  разбор PDF общий, форма — своя
from core import db                              # noqa: E402

LOG_KIND = "beget"                # метка для `proc_log` (mail_poller)
NOTIFY_MAIL_BOT = False           # суммы и получатели — только в платёжный бот (правило 11.08.2026)
PURPOSE_MAX = 210                 # ограничение поля «Назначение платежа» в платёжном поручении РФ
KIND = "hosting"                  # основание платежа в `payment_draft_queue`
GUARD_DEFAULT = 20000.0           # порог суммы, если строки поставщика нет в `rent_utility_guard`


def _money(s):
    """Денежная сумма из строки: «7 780.00» → 7780.0. Берём последнюю — в строке «Всего
    к оплате» сумма прописью стоит слева, цифрами всегда справа."""
    nums = re.findall(r"\d[\d  ]*[.,]\d{2}", s)
    return float(nums[-1].replace(" ", "").replace(" ", "").replace(",", ".")) if nums else None


def parse(text):
    """Счёт «Бегета» → поля платежа."""
    m = re.search(r"Счет\s*№\s*(\S+)\s+от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not m:
        raise ValueError("это не счёт Бегета: нет строки «Счет № … от …»")
    number = m.group(1).lstrip("№")
    inv_date = date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
    head, body = text[:m.start()], text[m.start():]

    # Получатель — в шапке, ИНН и КПП одной строкой через дробь.
    mp = re.search(r"ИНН\s*/\s*КПП\s*:?\s*(\d{10}|\d{12})\s*/\s*(\d{9})", head)
    if not mp:
        raise ValueError("в шапке счёта не найдена строка «ИНН/КПП» получателя")
    payee_inn, payee_kpp = mp.group(1), mp.group(2)
    # «р/c» у Бегета — с латинской «c», корсчёт подписан «к/счет». На случай, если подписи
    # съедет, страхуемся признаком корсчёта: он начинается с 301.
    acct = (re.search(r"р\s*/\s*[сc]\s*:?\s*(\d{20})", head) or [None, None])[1]
    corr = (re.search(r"к\s*/\s*(?:с|счет|сч)\s*:?\s*(\d{20})", head) or [None, None])[1]
    if not (acct and corr):
        accs = re.findall(r"\b(\d{20})\b", head)
        corr = corr or next((a for a in accs if a.startswith("301")), None)
        acct = acct or next((a for a in accs if not a.startswith("301")), None)
    bic = (re.search(r"БИК\s*:?\s*(\d{9})", head) or [None, None])[1]
    payee_name = next((ln.strip() for ln in head.splitlines() if ln.strip()), "")

    # Плательщик — блок ниже шапки получателя: ИНН стоит отдельной строкой, без «/КПП».
    tail = head[mp.end():]
    org_inn = (re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})\b", tail) or [None, None])[1]

    # Услуга и сумма: строка позиции с суммой справа, затем строка НДС, затем итог.
    service, amount, vat_text = "", None, ""
    for line in body.splitlines():
        if re.match(r"\s*Всего\s+к\s+оплате", line, re.I):
            amount = _money(line)
            continue
        mv = re.search(r"[Вв]\s*том\s*числе\s*НДС\s*\(?\s*(\d{1,2})\s*%\)?\s*(.*)$", line)
        if mv:
            vat_sum = _money(mv.group(2) or "")
            vat_text = (f"В том числе НДС {mv.group(1)}%, {vat_sum:.2f} руб."
                        if vat_sum else f"В том числе НДС {mv.group(1)}%.")
            continue
        if not service and re.search(r"\d[\d  ]*[.,]\d{2}\s*$", line) and re.search(r"[А-Яа-я]", line):
            service = re.split(r"\s{2,}\d", line.strip())[0].strip()
    if amount is None:
        raise ValueError("не найдена строка «Всего к оплате» с суммой")

    # Номер аккаунта — из наименования услуги: без него Бегет не зачислит платёж на баланс.
    account = (re.search(r"аккаунт[а-я]*\s+([A-Za-z0-9_.\-]+)", service) or [None, None])[1]

    return {"number": number, "date": inv_date, "amount": amount, "org_inn": org_inn,
            "payee_inn": payee_inn, "payee_name": payee_name, "service": service,
            "vat_text": vat_text, "account": account,
            "payee": {"payeeName": payee_name, "payeeInn": payee_inn, "payeeKpp": payee_kpp,
                      "payeeAccount": acct, "payeeBankBic": bic, "payeeBankCorrAccount": corr}}


def purpose(inv):
    """Назначение платежа — слово в слово как в прошедшем через банк платеже 26.06.2026:
    «Пополнение хостингового счёта аккаунта pronat (договор номер 22264) по сч. №11661378
    от 22.06.2026. В том числе НДС 22%, 1060.33 руб.»

    Не влезли в 210 знаков — режем НАИМЕНОВАНИЕ УСЛУГИ, но не с начала: номер аккаунта стоит
    в нём и обязан уцелеть, как и номер счёта с оговоркой по НДС."""
    tail = f" по сч. №{inv['number']} от {inv['date'].strftime('%d.%m.%Y')}."
    vat = f" {inv['vat_text']}" if inv["vat_text"] else ""
    service = inv["service"] or "Пополнение хостингового счёта"
    text = f"{service}{tail}{vat}"
    if len(text) > PURPOSE_MAX:
        keep = PURPOSE_MAX - len(tail) - len(vat)
        text = f"{service[:max(keep, 0)].rstrip(' ,.')}{tail}{vat}"
    return text


def guard_max(payee_inn):
    """Порог суммы счёта. Таблица общая с коммунальными счетами (`rent_utility_guard`):
    порог — свойство получателя, а не вида услуги, и заводить вторую такую же таблицу незачем.
    Строки нет — берём `GUARD_DEFAULT`, а не «без ограничения»: хостинг автоматический, и
    счёт на порядок больше обычного должен упереться в стоп, даже если строку забыли завести."""
    r = db.query("SELECT max_amount::float m FROM rent_utility_guard WHERE payee_inn=%s", (payee_inn,))
    return r[0]["m"] if r else GUARD_DEFAULT


def process(src, create=True):
    """Контракт движка для `mail_poller`: разобрать счёт, поставить платёж и увезти его в банк.

    Говорим в платёжный бот в обоих случаях — и когда черновик ушёл, и когда предохранитель
    не пропустил: счёт Бегета приходит по факту опустевшего баланса, и промолчать о нём
    нельзя ни в ту, ни в другую сторону."""
    res = _process(src, create=create)
    if create:
        rc.tg(("🖥 Счёт хостинга: платёж НЕ создан\n" if (res.get("stop") or res.get("error"))
               else "🖥 Счёт хостинга\n") + format_report(res))
    return res


def _process(src, create=True):
    """Разбор и постановка. Исключения наружу не выпускаем — почтовый цикл не должен падать
    из-за одного кривого вложения."""
    res = {"ok": False, "created": False, "stop": False, "error": None, "warns": [],
           "inv": {}, "draft_id": None, "sent": None}
    try:
        inv = parse(pdf_text(src))
        res["inv"] = {"number": inv["number"], "date": inv["date"].isoformat(),
                      "amount": inv["amount"], "org_inn": inv["org_inn"],
                      "payee_inn": inv["payee_inn"], "payee_name": inv["payee_name"],
                      "service": inv["service"], "account": inv["account"]}
        res["purpose"] = purpose(inv)

        if not inv["org_inn"] or inv["org_inn"] not in psend.BANKS:
            res["stop"] = True
            res["error"] = (f"счёт выставлен на ИНН {inv['org_inn'] or '?'} — это не наше юрлицо "
                            f"с банковским доступом, платить не из чего")
            return res
        miss = [k for k in ("payeeAccount", "payeeBankBic", "payeeBankCorrAccount")
                if not inv["payee"].get(k)]
        if miss:
            res["stop"] = True
            res["error"] = f"в счёте не разобраны реквизиты получателя: {', '.join(miss)}"
            return res
        if not inv["account"]:
            res["stop"] = True
            res["error"] = ("в наименовании услуги нет номера аккаунта — без него Бегет не "
                            "зачислит платёж на баланс, разберись руками")
            return res

        bad = rc.payee_mismatch(inv["payee"], inv["payee_inn"])
        if bad:
            res["stop"] = True
            res["error"] = "реквизиты счёта расходятся с карточкой МС — " + "; ".join(bad)
            return res

        cap = guard_max(inv["payee_inn"])
        if cap is not None and inv["amount"] > cap:
            res["stop"] = True
            res["error"] = (f"сумма {rc.rub(inv['amount'])} выше порога {rc.rub(cap)} — "
                            f"черновик не создан, разберись руками")
            return res

        res["ok"] = True
        if not create:
            return res

        draft_id, created = rc.queue_draft(
            org_inn=inv["org_inn"], payee_inn=inv["payee_inn"], amount=inv["amount"],
            purpose_text=res["purpose"], payee=inv["payee"], kind=KIND,
            idem_key=f"beget:{inv['org_inn']}:{inv['number']}",
            note=f"хостинг по счёту {inv['number']} от {inv['date'].isoformat()}")
        res["draft_id"], res["created"] = draft_id, created
        if not created:
            res["warns"].append(f"счёт {inv['number']} уже стоял в очереди (черновик #{draft_id}) "
                                f"— второй платёж не создан")
            return res

        # Сразу в банк, не дожидаясь суточного прогона: у счёта три банковских дня.
        if not pauto.enabled():
            res["warns"].append("PAYMENT_AUTOSEND снят — черновик ждёт в очереди, в банк не ушёл")
            return res
        out = psend.send_draft(draft_id, actor="beget")
        res["sent"] = out
        if not out.get("ok"):
            res["warns"].append(f"в банк не ушёл: {out.get('error')} — черновик остался "
                                f"в очереди, уйдёт ближайшим прогоном автоотправки")
    except Exception as e:                                       # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def format_report(res):
    inv, L = res.get("inv", {}), []
    L.append(f"🖥 Счёт Бегета № {inv.get('number')} от {inv.get('date')} | {rc.rub(inv['amount'])}"
             if inv.get("number") else "🖥 Счёт Бегета")
    if inv.get("org_inn"):
        L.append(f"Плательщик: {rc.ORG_TITLE.get(inv['org_inn'], inv['org_inn'])}")
    if res.get("error"):
        L.append(("🛑 " if res.get("stop") else "❌ ") + res["error"])
        return "\n".join(L)
    if res.get("purpose"):
        L.append(f"Назначение: {res['purpose']}")
    sent = res.get("sent") or {}
    if res.get("created") and sent.get("status") in ("sent_prod", "sent_sandbox"):
        L.append(f"✅ Черновик #{res['draft_id']} в банке — подписываешь ты")
    elif res.get("created"):
        L.append(f"✅ Черновик #{res['draft_id']} в очереди")
    elif res.get("draft_id"):
        L.append(f"↔️ Уже в очереди: черновик #{res['draft_id']}")
    for w in res.get("warns", []):
        L.append(f"⚠️ {w}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Счёт хостинга «Бегет» → черновик платёжки")
    ap.add_argument("path", help="PDF счёта")
    ap.add_argument("--create", action="store_true", help="поставить платёж и увезти в банк")
    a = ap.parse_args()
    res = process(a.path, create=a.create) if a.create else _process(a.path, create=False)
    print(format_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
