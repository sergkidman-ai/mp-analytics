# поток: inv
"""collectors/bank_txn_store.py — банковская выписка обеих фирм → Postgres (таблица `bank_txn`).

Зачем отдельно от `bank_ms`: тот кладёт деньги в МойСклад (учёт), а здесь мы храним выписку
КАК ЕСТЬ, чтобы человек мог разметить платежи статьями операционных расходов на дашборде
(`/opex/statements`). Ничего не фильтруем и не выбрасываем: зарплата, самозанятые, поставщики,
приход от маркетплейсов — всё ложится в таблицу. Платежи, которым статья не присвоена, просто
висят нераспределёнными и в итог опер. расходов не входят (решение Сергея 03.08.2026).

Банконезависимо: на вход идёт нормализованная запись `normalize()` любого банка
(`collectors/alfa_statement.py`, `collectors/sber_statement.py` — ключи совпадают).

Идемпотентность: натуральный ключ `(bank, nk)`, где `nk` — uuid операции банка, а если банк его
не дал — md5 реквизитов (счёт|дата|сумма|номер|назначение). Повторный прогон за ту же дату
не плодит строк.

Отсечка `OPEX_STMT_SINCE` (по умолчанию 2026-08-01): операции раньше неё не храним — история
до августа в разметку не входит.
"""
import hashlib
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))                     # core.db

import psycopg2.extras                                   # noqa: E402  Json для jsonb-колонки

import core.db as db                                     # noqa: E402

SINCE = os.getenv("OPEX_STMT_SINCE", "2026-08-01")       # раньше этой даты не храним


def _json(obj):
    """jsonb-обёртка: суммы у Сбера приходят Decimal, штатный json.dumps на них падает."""
    return psycopg2.extras.Json(
        obj, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def _d(v):
    """Дата из ответа банка: '2026-08-01T09:12:33+03:00' → '2026-08-01'. Пусто → None."""
    s = (v or "")[:10]
    return s if len(s) == 10 else None


def name_key(name):
    """Нормализованное имя контрагента — фолбэк-ключ правила, когда ИНН в выписке пуст
    (часть платежей физлицам). Регистр, кавычки и пробелы съедаются: «ООО "Ромашка"» и
    «ООО Ромашка» дают один ключ."""
    return re.sub(r"[^0-9a-zа-яё]+", "", (name or "").lower())


def _nk(op):
    """Натуральный ключ операции: uuid банка, иначе хеш реквизитов."""
    if op.get("uuid"):
        return op["uuid"]
    src = "|".join(str(op.get(k) or "") for k in
                   ("account", "operation_date", "amount", "document_number", "purpose"))
    return "h:" + hashlib.md5(src.encode("utf-8")).hexdigest()


def store(ops, bank, org_inn, account=None, since=None, raws=None):
    """Записать операции выписки в `bank_txn` и сразу применить к новым правила разметки.

    ops     — список нормализованных записей (`normalize()` банка);
    account — наш счёт, если в записи его нет (у Альфы `normalize` его не кладёт);
    raws    — параллельный список сырых операций банка (необязательно; сырьё есть на диске
              в incoming/, здесь оно только для отладки конкретной строки).

    → stats: seen / stored (новых) / dup (уже были) / before_cutoff / ruled (размечено правилом).
    """
    since = since or SINCE
    stats = {"seen": len(ops), "stored": 0, "dup": 0, "before_cutoff": 0, "ruled": 0}
    rows = []
    for i, op in enumerate(ops):
        day = _d(op.get("operation_date")) or _d(op.get("document_date"))
        if not day:
            continue
        if since and day < since:
            stats["before_cutoff"] += 1
            continue
        raw = (raws[i] if raws and i < len(raws) else None) or op
        rows.append((
            op.get("bank") or bank, org_inn, op.get("account") or account or "", _nk(op),
            op.get("uuid"), op.get("transaction_id"), op.get("direction"), op.get("amount"),
            op.get("currency"), day, _d(op.get("document_date")), op.get("document_number"),
            op.get("purpose"), op.get("counterparty_name"), op.get("counterparty_inn"),
            op.get("counterparty_kpp"), op.get("counterparty_account"),
            op.get("counterparty_bic"), _json(raw),
        ))
    if not rows:
        return stats

    new_ids = []
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    INSERT INTO bank_txn (bank, org_inn, account, nk, txn_uuid, transaction_id,
                        direction, amount, currency, operation_date, document_date,
                        document_number, purpose, cp_name, cp_inn, cp_kpp, cp_account, cp_bic, raw)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (bank, nk) DO NOTHING
                    RETURNING id""", r)
                got = cur.fetchone()
                if got:
                    new_ids.append(got[0])
                else:
                    stats["dup"] += 1
    stats["stored"] = len(new_ids)
    stats["ruled"] = apply_rules(new_ids) if new_ids else 0
    return stats


def apply_rules(txn_ids=None):
    """Разметить неразмеченные расходы по запомненным правилам (`opex_rule`).

    Правило ищется по ИНН контрагента, а если ИНН пуст — по нормализованному имени.
    У правила может быть фрагмент назначения (`purpose_like`, миграция 210): пусто — правило
    на всего контрагента, заполнено — только на платежи с этим фрагментом в назначении.
    Если платёж подходит под несколько правил, побеждает САМОЕ СПЕЦИФИЧНОЕ — выбор живёт
    во вью `opex_rule_match` (миграция 211), чтобы счётчики и удаление правил считали так же.

    Уже размеченное (в т.ч. руками) не трогаем. txn_ids=None → пройтись по всем неразмеченным.
    → сколько строк разметили."""
    where = "AND t.id = ANY(%s)" if txn_ids else ""
    params = ([list(txn_ids)] if txn_ids else [])
    sql = f"""
        INSERT INTO bank_txn_opex (txn_id, category_id, spread_months, start_month, source)
        SELECT m.txn_id, m.category_id, m.spread_months,
               -- статьи «за прошлые периоды» (налоги) разносятся НАЗАД: период заканчивается
               -- месяцем перед платежом, значит start = месяц платежа − N (миграция 213)
               CASE WHEN c.spread_back
                    THEN (date_trunc('month', t.operation_date)
                          - (m.spread_months || ' month')::interval)::date
                    ELSE date_trunc('month', t.operation_date)::date END, 'rule'
        FROM opex_rule_match m
        JOIN bank_txn t ON t.id = m.txn_id
        JOIN opex_category c ON c.id = m.category_id
        LEFT JOIN bank_txn_opex a ON a.txn_id = m.txn_id
        WHERE a.txn_id IS NULL {where}
        ON CONFLICT (txn_id) DO NOTHING"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def summary(stats, label):
    """Одна строка для крон-лога."""
    return (f"{label}: в БД новых {stats['stored']}, уже было {stats['dup']}, "
            f"до отсечки {stats['before_cutoff']}, размечено правилом {stats['ruled']}")
