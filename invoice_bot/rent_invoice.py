# поток: inv
"""invoice_bot/rent_invoice.py — счёт арендодателя за коммунальные услуги → черновик платёжки.

Движок почтовой папки «Аренда» (`MAIL_FOLDER_RENT`), подключается в `mail_poller` тем же
контрактом, что счета поставщиков и УПД: `process(path, create=True)` + `format_report(res)`.

Отличие от счетов поставщиков: заказ поставщику в МойСкладе НЕ создаём. Арендодатель не продаёт
нам товар, приходовать нечего — счёт сразу становится платежом. Реквизиты получателя берём
ИЗ СЧЁТА (решение Сергея 11.08.2026), карточка МС при этом остаётся контролем: расхождение по
счёту/БИК/корсчёту — стоп и разбор руками, а не платёж «куда-нибудь».

Предохранители (без них скрипт не платит):
  * наше юрлицо-плательщик определяется по строке «Покупатель» счёта и обязано быть в
    `payment_send.BANKS` — иначе непонятно, из какого банка платить;
  * сумма не выше порога `rent_utility_guard` (решение Сергея: > 10 000 ₽ — стоп, алерт в TG);
  * повторно прочитанное письмо не плодит вторую платёжку — ключ `util:<org_inn>:<номер счёта>`.

Черновик встаёт в очередь со статусом `planned` и уезжает в банк штатным `payment_autosend`
(kind `rent_utility`). Документ уходит НЕПОДПИСАННЫМ: деньги двинутся, только когда человек
подпишет платёжку в вебе банка.

Запуск вручную (разбор без записи — по умолчанию):
    ./venv/bin/python invoice_bot/rent_invoice.py счёт.pdf
    ./venv/bin/python invoice_bot/rent_invoice.py счёт.pdf --create
"""
import os
import re
import sys
import argparse
import subprocess
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import rent_core as rc                           # noqa: E402
import payment_send as psend                     # noqa: E402
from core import db                              # noqa: E402

LOG_KIND = "rent"                 # метка для `proc_log` (mail_poller)
NOTIFY_MAIL_BOT = False           # в общий бот invoice-bot платежи не шлём (решение Сергея
                                  # 11.08.2026): суммы и получатели идут только в платёжный бот —
                                  # сводкой автоотправки, а стопы — из `process` ниже.
PURPOSE_MAX = 210                 # ограничение поля «Назначение платежа» в платёжном поручении РФ
MONTHS = {m: i + 1 for i, m in enumerate(rc.MONTHS_GEN)}
MONTHS.update({m: i + 1 for i, m in enumerate(rc.MONTHS_NOM)})


def pdf_text(path):
    """Текст счёта с сохранением раскладки: реквизиты в шапке стоят колонками, и без `-layout`
    номер счёта склеивается с БИК соседней колонки."""
    r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pdftotext вернул {r.returncode}: {(r.stderr or '')[:200]}")
    return r.stdout


def _inn(s):
    """ИНН из строки: 10 цифр (юрлицо) или 12 (ИП). Границы обязательны — иначе выкусим
    первые 10 цифр из двадцатизначного расчётного счёта."""
    m = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})\b", s)
    return m.group(1) if m else None


def _amount(s):
    """Денежная сумма из строки: «2 834,72» → 2834.72. Берём последнюю — в строке «Итого»
    впереди может стоять что угодно, итог всегда справа."""
    nums = re.findall(r"\d[\d  ]*[.,]\d{2}", s)
    return float(nums[-1].replace(" ", "").replace(" ", "").replace(",", ".")) if nums else None


def parse(text):
    """Счёт на оплату (печатная форма 1С) → поля платежа.

    Разбираем по форме документа, а не по конкретному арендодателю: шапка с реквизитами банка
    получателя, строка «Счет на оплату № N от <дата>», «Поставщик»/«Покупатель» с ИНН, «Итого»
    и признак НДС. Наименование услуги берём из первой позиции таблицы — в нём стоит период
    («Компенсация потребляемых коммунальных услуг п.2.2.18 (Июль 2026)»), и он же идёт
    в назначение платежа.
    """
    head, _, body = text.partition("Счет на оплату")
    if not body:
        raise ValueError("это не счёт на оплату: нет строки «Счет на оплату»")

    m = re.match(r"\s*№?\s*([0-9A-Za-zА-Яа-я/\-]+)\s+от\s+(\d{1,2})\s+([А-Яа-я]+)\s+(\d{4})", body)
    if not m:
        m2 = re.match(r"\s*№?\s*([0-9A-Za-zА-Яа-я/\-]+)\s+от\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", body)
        if not m2:
            raise ValueError("не разобрать номер и дату счёта")
        number, d, mo, y = m2.group(1), int(m2.group(2)), int(m2.group(3)), int(m2.group(4))
    else:
        number, d, y = m.group(1), int(m.group(2)), int(m.group(4))
        mo = MONTHS.get(m.group(3).lower())
        if not mo:
            raise ValueError(f"не разобрать месяц в дате счёта: {m.group(3)!r}")
    inv_date = date(y, mo, d)

    # Реквизиты получателя — из ШАПКИ (до строки «Счет на оплату»): там банк получателя, его БИК,
    # корсчёт и расчётный счёт. Двадцатизначные номера различаем по началу: корсчёт — 301…
    accs = re.findall(r"\b(\d{20})\b", head)
    corr = next((a for a in accs if a.startswith("301")), None)
    acct = next((a for a in accs if not a.startswith("301")), None)
    bic = (re.search(r"БИК\s*:?\s*(\d{9})", head) or [None, None])[1]

    seller = re.search(r"Поставщик\s*:?\s*(.+)", body)
    buyer = re.search(r"Покупатель\s*:?\s*(.+)", body)
    if not (seller and buyer):
        raise ValueError("в счёте нет строки «Поставщик» или «Покупатель»")
    payee_inn = _inn(seller.group(1)) or _inn(head)
    org_inn = _inn(buyer.group(1))
    kpp = (re.search(r"КПП\s*:?\s*(\d{9})", seller.group(1)) or
           re.search(r"КПП\s*:?\s*(\d{9})", head) or [None, None])[1]
    payee_name = re.split(r",\s*ИНН", seller.group(1))[0].strip()

    total = None
    for line in body.splitlines():
        if re.match(r"\s*(Итого|Всего к оплате)\b", line, re.I):
            total = _amount(line)
            if total:
                break
    if total is None:
        raise ValueError("не найдена итоговая сумма счёта")

    # НДС: «Без налога (НДС)» / «Без НДС» — не облагается; иначе берём формулировку счёта.
    vat_free = bool(re.search(r"Без\s+(налога|НДС)", body, re.I))
    m_vat = re.search(r"[Вв]\s*(?:т\.?\s*ч\.?|том числе)[^\n]*НДС[^\n]*", body)
    vat_text = "НДС не облагается." if vat_free else (m_vat.group(0).strip() if m_vat else "")

    # Наименование услуги — первая строка таблицы позиций: начинается с номера по порядку,
    # дальше текст. Хвост с количеством/ценой/суммой отрезаем по первому числовому столбцу.
    service = ""
    for line in body.splitlines():
        m_it = re.match(r"\s*1\s{2,}(\D.+)", line)
        if m_it:
            service = re.split(r"\s{2,}\d", m_it.group(1))[0].strip()
            break

    return {"number": number, "date": inv_date, "amount": total,
            "org_inn": org_inn, "payee_inn": payee_inn, "payee_name": payee_name,
            "service": service, "vat_text": vat_text,
            "payee": {"payeeName": payee_name, "payeeInn": payee_inn, "payeeKpp": kpp,
                      "payeeAccount": acct, "payeeBankBic": bic, "payeeBankCorrAccount": corr}}


def purpose(inv):
    """Назначение платежа: услуга с периодом + основание + НДС — ровно как владелец писал
    в ручных платежах («Компенсация потребляемых коммунальных услуг п.2.2.18 (Июль 2026)
    по сч. 229 от 05 августа 2026. НДС не облагается.»).

    Не влезли в 210 знаков — режем НАИМЕНОВАНИЕ УСЛУГИ: номер счёта и оговорка по НДС обязаны
    уцелеть целиком, иначе бухгалтерия арендодателя не разнесёт платёж, а платёж придётся
    уточнять письмом."""
    tail = f" по сч. {inv['number']} от {rc.ru_date_words(inv['date'])}."
    vat = f" {inv['vat_text']}" if inv["vat_text"] else ""
    service = inv["service"] or "Оплата по счёту"
    text = f"{service}{tail}{vat}"
    if len(text) > PURPOSE_MAX:
        keep = PURPOSE_MAX - len(tail) - len(vat)
        text = f"{service[:max(keep, 0)].rstrip(' ,.')}{tail}{vat}"
    return text


def guard_max(payee_inn):
    """Порог суммы коммунального счёта. Порога на арендодателя нет → платим без ограничения
    (сознательно: правило заводится строкой в `rent_utility_guard`, а не правкой кода)."""
    r = db.query("SELECT max_amount::float m FROM rent_utility_guard WHERE payee_inn=%s", (payee_inn,))
    return r[0]["m"] if r else None


def process(src, create=True):
    """Контракт движка для `mail_poller`: разобрать файл и (при `create`) поставить платёж
    в очередь.

    Успех молчит: черновик назовёт по имени сводка автоотправки в платёжном боте, и дублировать
    её письмом-отчётом незачем. А вот СТОП говорит сразу — иначе счёт, который предохранитель
    не пропустил, останется незамеченным до срока оплаты. Ручной разбор (`create=False`) не
    пишет никому: это отладочный прогон."""
    res = _process(src, create=create)
    if create and (res.get("stop") or res.get("error")):
        rc.tg("🏠 Счёт аренды: платёж НЕ создан\n" + format_report(res))
    return res


def _process(src, create=True):
    """Разбор и постановка. Исключения наружу не выпускаем — почтовый цикл не должен падать
    из-за одного кривого вложения."""
    res = {"ok": False, "created": False, "stop": False, "error": None, "warns": [],
           "inv": {}, "draft_id": None}
    try:
        inv = parse(pdf_text(src))
        res["inv"] = {"number": inv["number"], "date": inv["date"].isoformat(),
                      "amount": inv["amount"], "org_inn": inv["org_inn"],
                      "payee_inn": inv["payee_inn"], "payee_name": inv["payee_name"],
                      "service": inv["service"]}
        res["purpose"] = purpose(inv)

        if not inv["org_inn"] or inv["org_inn"] not in psend.BANKS:
            res["stop"] = True
            res["error"] = (f"счёт выставлен на ИНН {inv['org_inn'] or '?'} — это не наше юрлицо "
                            f"с банковским доступом, платить не из чего")
            return res
        if not inv["payee_inn"]:
            res["stop"] = True
            res["error"] = "в счёте не найден ИНН получателя"
            return res
        miss = [k for k in ("payeeAccount", "payeeBankBic", "payeeBankCorrAccount")
                if not inv["payee"].get(k)]
        if miss:
            res["stop"] = True
            res["error"] = f"в счёте не разобраны реквизиты получателя: {', '.join(miss)}"
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
            purpose_text=res["purpose"], payee=inv["payee"], kind="rent_utility",
            idem_key=f"util:{inv['org_inn']}:{inv['number']}",
            note=f"коммуналка по счёту {inv['number']} от {inv['date'].isoformat()}")
        res["draft_id"], res["created"] = draft_id, created
        if not created:
            res["warns"].append(f"счёт {inv['number']} уже стоял в очереди (черновик #{draft_id}) "
                                f"— второй платёж не создан")
    except Exception as e:                                       # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def format_report(res):
    inv, L = res.get("inv", {}), []
    head = (f"🏠 Счёт аренды № {inv.get('number')} от {inv.get('date')} | "
            f"{inv.get('payee_name')} | {rc.rub(inv['amount'])}" if inv.get("number")
            else "🏠 Счёт аренды")
    L.append(head)
    if inv.get("org_inn"):
        L.append(f"Плательщик: {rc.ORG_TITLE.get(inv['org_inn'], inv['org_inn'])}")
    if res.get("error"):
        L.append(("🛑 " if res.get("stop") else "❌ ") + res["error"])
        return "\n".join(L)
    if res.get("purpose"):
        L.append(f"Назначение: {res['purpose']}")
    if res.get("created"):
        L.append(f"✅ Черновик #{res['draft_id']} в очереди — уйдёт в банк ближайшим прогоном "
                 f"(подписываешь в банке ты)")
    elif res.get("draft_id"):
        L.append(f"↔️ Уже в очереди: черновик #{res['draft_id']}")
    for w in res.get("warns", []):
        L.append(f"⚠️ {w}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Счёт арендодателя → черновик платёжки")
    ap.add_argument("path", help="PDF счёта")
    ap.add_argument("--create", action="store_true", help="поставить платёж в очередь (иначе разбор)")
    a = ap.parse_args()
    res = process(a.path, create=a.create)
    print(format_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
