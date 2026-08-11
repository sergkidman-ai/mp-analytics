# поток: inv
"""invoice_bot/rent_core.py — общее ядро арендных платежей (миграция 215).

Два входа, одна очередь:
  * `rent_schedule.py`  — постоянная арендная плата, первый понедельник месяца (`rent_plan`);
  * `rent_invoice.py`   — компенсация коммунальных услуг по счёту из почты (папка «Аренда»).

Здесь то, что у них общее: постановка черновика в `payment_draft_queue` с ключом
идемпотентности, сверка реквизитов получателя с карточкой МойСклада и уведомление в платёжный
бот. Банк не трогаем: черновик уезжает штатным `payment_autosend` → `payment_send`, банк
выбирается по `org_inn` (Альфа — Цифровой Квадрат, Сбер — Дисквэр).

Почему у аренды свой путь, а не `po_payment_watch`: у арендных платежей нет заказа поставщику
в МойСкладе — основанием служит договор либо счёт арендодателя, а назначение платежа задано
текстом (номер договора, площадь, ставка НДС), а не собирается из номеров заказов.
"""
import os
import sys
import json
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)
import psycopg2.extras                           # noqa: E402  Json для jsonb-колонки payee
from ms import get                               # noqa: E402
from core import db                              # noqa: E402

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Наши юрлица-плательщики. Держим здесь только для человекочитаемых сообщений: выбор банка —
# дело `payment_send.BANKS`, дублировать маршрутизацию нельзя (разъедется).
ORG_TITLE = {"7807355364": "ООО «Цифровой квадрат»", "7811803918": "ООО «Дисквэр»"}


def _json(obj):
    """jsonb-обёртка для колонки `payee` (реквизиты из счёта)."""
    return psycopg2.extras.Json(obj, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def rub(x):
    """Русский вид суммы: 56 907.65 ₽ (запятая как разделитель разрядов читается по-английски)."""
    return f"{float(x):,.2f}".replace(",", " ") + " ₽"


def ru_date_words(d):
    """05 августа 2026 — как арендодатель пишет дату счёта и как мы писали её в платежах руками."""
    return f"{d.day:02d} {MONTHS_GEN[d.month - 1]} {d.year}"


def tg(msg):
    """Сводка в платёжный бот (`TG_PAY_BOT_TOKEN` / `TG_PAY_NOTIFY_ID`) — тот же адресат, что у
    `payment_autosend`: суммы и получатели идут узкому кругу, а не в общий канал invoice-bot.
    Бот не настроен → молча пишем в лог: постановку платежа это ронять не должно."""
    token = os.getenv("TG_PAY_BOT_TOKEN", "").strip()
    ids = [x.strip() for x in os.getenv("TG_PAY_NOTIFY_ID", "").split(",") if x.strip()]
    if not (token and ids):
        print("TG: платёжный бот не настроен — сводка не отправлена", flush=True)
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for uid in ids:
        try:
            data = urllib.parse.urlencode({"chat_id": uid, "text": msg}).encode()
            urllib.request.urlopen(api, data=data, timeout=20).read()
        except Exception as e:                                   # noqa: BLE001
            print(f"TG {uid}: не доставлено ({type(e).__name__})", flush=True)


# ── реквизиты получателя ─────────────────────────────────────────────────────────────────────
def ms_payee(inn):
    """Реквизиты арендодателя из МойСклада (правило 1: МС — источник правды) или None.

    Для арендных платежей это КОНТРОЛЬ, а не источник: в платёжку идут реквизиты из счёта
    (решение Сергея 11.08.2026). Карточек у ИНН может быть несколько — тогда сверять не с чем,
    возвращаем None и полагаемся на счёт."""
    rows = get(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if len(rows) != 1:
        return None
    cp = rows[0]
    accs = get(f"/entity/counterparty/{cp['id']}/accounts").get("rows", [])
    acc = next((a for a in accs if a.get("isDefault")), accs[0] if accs else None)
    if not acc:
        return None
    return {
        "payeeName": cp.get("legalTitle") or cp.get("name"), "payeeInn": inn, "payeeKpp": cp.get("kpp"),
        "payeeAccount": acc.get("accountNumber"), "payeeBankBic": acc.get("bic"),
        "payeeBankCorrAccount": acc.get("correspondentAccount"),
    }


def payee_mismatch(payee, inn):
    """Расхождения реквизитов из счёта с карточкой МС — списком строк, пустой список = сходится.

    Сверяем ТОЛЬКО то, куда уйдут деньги: счёт, БИК, корсчёт, КПП. Название юрлица не сверяем —
    в МС оно записано в своей орфографии («АО "КУРГАНМАШЗАВОД"» против «АО «Курганмашзавод»»),
    и расхождение регистра не повод останавливать платёж.

    Карточки в МС нет (или их несколько) — сверять не с чем: возвращаем пустой список и платим
    по счёту. Это осознанно: арендодатель не поставщик товаров, его карточка может быть не
    заведена, а счёт при этом настоящий."""
    ms = ms_payee(inn)
    if not ms:
        return []
    bad = []
    for k, ru in (("payeeAccount", "расчётный счёт"), ("payeeBankBic", "БИК"),
                  ("payeeBankCorrAccount", "корсчёт"), ("payeeKpp", "КПП")):
        a, b = (payee.get(k) or "").strip(), (ms.get(k) or "").strip()
        if a and b and a != b:
            bad.append(f"{ru}: в счёте {a}, в МС {b}")
    return bad


# ── очередь черновиков ───────────────────────────────────────────────────────────────────────
def queue_draft(org_inn, payee_inn, amount, purpose_text, payee, kind, idem_key, note=None):
    """Поставить арендный платёж в `payment_draft_queue`.

    → (draft_id, created): `created=False` — такой платёж уже стоит в очереди (или уже отправлен),
    второй раз не ставим. Идемпотентность держит уникальный индекс по `idem_key` (миграция 215):
    повторный запуск планировщика и повторно прочитанное письмо не могут родить вторую платёжку
    на те же деньги, даже если проверка «уже есть» и вставка разойдутся по времени."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO payment_draft_queue
                             (org_inn, inn, kind, amount, status, payee, purpose_text, idem_key, note)
                           VALUES (%s, %s, %s, %s, 'planned', %s, %s, %s, %s)
                           ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING
                           RETURNING id""",
                        (org_inn, payee_inn, kind, round(float(amount), 2),
                         _json(payee) if payee else None, purpose_text, idem_key, note))
            row = cur.fetchone()
            if row:
                return (row[0] if not isinstance(row, dict) else row["id"]), True
            cur.execute("SELECT id FROM payment_draft_queue WHERE idem_key=%s", (idem_key,))
            row = cur.fetchone()
            return (row[0] if not isinstance(row, dict) else row["id"]), False
